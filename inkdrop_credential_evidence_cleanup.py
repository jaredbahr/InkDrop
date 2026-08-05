#!/usr/bin/env python3
"""Bounded cleanup for credentials accidentally persisted in evidence rows."""

from __future__ import annotations

import json
from pathlib import Path

import inkdrop_state


JSON_TARGETS = (
    ("source_attempts", "raw_json", True),
    ("history_events", "raw_json", False),
    ("queue_items", "raw_json", False),
    ("bad_source_candidates", "raw_json", False),
    ("allowed_source_candidates", "raw_json", False),
    ("review_exceptions", "raw_json", False),
    ("download_tasks", "raw_json", True),
    ("series", "raw_json", True),
    ("import_results", "raw_json", True),
)
TEXT_TARGETS = (
    ("source_attempts", "failure_reason"),
    ("history_events", "message"),
    ("queue_items", "last_event"),
    ("bad_source_candidates", "reason"),
    ("bad_source_candidates", "source_path"),
    ("review_exceptions", "reason"),
    ("review_exceptions", "next_action"),
    ("review_exceptions", "activity_summary"),
    ("download_tasks", "failure_reason"),
)
NEEDLES = tuple(sorted(inkdrop_state.SENSITIVE_EVIDENCE_CREDENTIAL_KEYS))


def _columns(con, table):
    return {str(row["name"]) for row in con.execute(f"pragma table_info({table})")}


def _contains_candidate(value):
    text = str(value or "").lower()
    return any(needle in text for needle in NEEDLES)


def _cursor_key(table, column):
    return f"credential_cleanup_cursor_v1:{table}:{column}"


def _cursor(con, table, column):
    row = con.execute("select value from schema_meta where key=?", (_cursor_key(table, column),)).fetchone()
    try:
        return max(0, int(row["value"] if row else 0))
    except (TypeError, ValueError):
        return 0


def _save_cursor(con, table, column, rowid):
    con.execute(
        "insert into schema_meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value",
        (_cursor_key(table, column), str(max(0, int(rowid or 0)))),
    )


def cleanup_persisted_credentials(db_path, *, batch_size=100):
    """Scrub at most batch_size rows using short compare-and-swap updates."""
    budget = max(1, int(batch_size or 100))
    remaining = budget
    result = {"ok": True, "examined": 0, "changed": 0, "malformed": 0, "concurrent_skips": 0}
    with inkdrop_state.connect(Path(db_path)) as con:
        inkdrop_state.init_schema(con)
        targets = [*(('json', *target) for target in JSON_TARGETS), *(('text', table, column, False) for table, column in TEXT_TARGETS)]
        base_quota, extra = divmod(budget, len(targets))
        for index, (kind, table, column, operational) in enumerate(targets):
            quota = base_quota + (1 if index < extra else 0)
            if quota <= 0 or not inkdrop_state.table_exists(con, table) or column not in _columns(con, table):
                continue
            cursor = _cursor(con, table, column)
            rows = con.execute(
                f"select rowid, {column} from {table} where rowid>? order by rowid limit ?",
                (cursor, quota),
            ).fetchall()
            retry_current_row = False
            for row in rows:
                remaining -= 1
                result["examined"] += 1
                old = row[column]
                if not _contains_candidate(old):
                    cursor = row["rowid"]
                    continue
                if kind == "json":
                    try:
                        value = json.loads(old or "{}")
                    except (TypeError, ValueError):
                        result["malformed"] += 1
                        new = inkdrop_state.json_dumps({"credential_cleanup": "malformed_evidence_redacted"})
                    else:
                        safe = (
                            inkdrop_state.credential_safe_operational_payload(value)
                            if operational
                            else inkdrop_state.privacy_safe_evidence_payload(value)
                        )
                        new = inkdrop_state.json_dumps(safe)
                else:
                    new = inkdrop_state.scrub_credential_query_params(str(old or ""))
                if new == old:
                    cursor = row["rowid"]
                    continue
                changed = con.execute(
                    f"update {table} set {column}=? where rowid=? and {column}=?",
                    (new, row["rowid"], old),
                ).rowcount
                result["changed"] += int(bool(changed))
                result["concurrent_skips"] += int(not changed)
                if not changed:
                    retry_current_row = True
                    break
                cursor = row["rowid"]
            if len(rows) < quota and not retry_current_row:
                cursor = 0
            _save_cursor(con, table, column, cursor)
        con.commit()
    result["batch_size"] = budget
    result["budget_remaining"] = remaining
    return result
