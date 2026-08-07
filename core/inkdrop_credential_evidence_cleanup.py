#!/usr/bin/env python3
"""Bounded cleanup for credentials accidentally persisted in evidence rows."""

from __future__ import annotations

import json
import re
from pathlib import Path

from core import inkdrop_state


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
    ("bad_source_candidates", "title"),
    ("bad_source_candidates", "normalized_title"),
    ("review_exceptions", "reason"),
    ("review_exceptions", "next_action"),
    ("review_exceptions", "activity_summary"),
    ("download_tasks", "failure_reason"),
)
NEEDLES = tuple(sorted(inkdrop_state.SENSITIVE_EVIDENCE_CREDENTIAL_KEYS))

# Historical rows written before bad_source_candidate_payload() scrubbed
# source_path/title *before* normalize_bad_source_candidate_title() stripped
# punctuation: a credential's "=" separator survives as a bare space
# ("apikey 01fda537...") in already-normalized text, which the "=", ":"
# separator scrub_credential_query_params() requires can no longer match.
# Bounded to this one-time cleanup pass (not the general evidence scrubbers,
# which stay conservative to avoid over-redacting ordinary failure_reason
# prose) -- requires a 16+ character unbroken token, which no real comic
# title/normalized failure text is going to produce immediately after one of
# these keywords by coincidence.
_NORMALIZED_CREDENTIAL_RE = re.compile(
    r"(?i)\b(apikey|api_key|api-key|token|access_token|refresh_token|password|passwd|passkey|rsskey|secret)"
    r"([ \t]+)([0-9a-zA-Z_-]{16,})\b"
)


def _scrub_normalized_credential_text(text):
    return _NORMALIZED_CREDENTIAL_RE.sub(r"\1\2redacted", str(text or ""))


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
                new = _scrub_normalized_credential_text(new)
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


def backfill_all_persisted_credentials(db_path):
    """One-time, unbounded sweep of every currently-dirty row.

    cleanup_persisted_credentials() is designed for small, continuous
    maintenance-cycle batches and only advances a forward rowid cursor per
    (table, column) -- once that cursor has swept past a row without
    matching it (as happened here: the pre-fix scrubber didn't recognize
    this leak shape, so the cursor moved on without cleaning it), the row
    is never revisited until the cursor completes a full lap. For a known,
    bounded backlog of already-identified rows, re-querying "still dirty"
    directly and cleaning every match in one pass is simpler and faster
    than waiting for the cursor to lap the table again. Does not touch or
    reset the maintenance job's cursors, so the regular incremental job
    keeps sweeping forward from wherever it already was.
    """
    result = {"ok": True, "examined": 0, "changed": 0, "malformed": 0, "concurrent_skips": 0, "remaining_dirty": 0}
    with inkdrop_state.connect(Path(db_path)) as con:
        inkdrop_state.init_schema(con)
        targets = [*(('json', *target) for target in JSON_TARGETS), *(('text', table, column, False) for table, column in TEXT_TARGETS)]
        for kind, table, column, operational in targets:
            if not inkdrop_state.table_exists(con, table) or column not in _columns(con, table):
                continue
            while True:
                rows = con.execute(
                    f"select rowid, {column} from {table} where lower({column}) like '%apikey%' or lower({column}) like '%api_key%' order by rowid limit 500"
                ).fetchall()
                if not rows:
                    break
                progressed = False
                for row in rows:
                    result["examined"] += 1
                    old = row[column]
                    if not _contains_candidate(old):
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
                    new = _scrub_normalized_credential_text(new)
                    if new == old:
                        # Matched a NEEDLE substring but neither scrubber changed
                        # it (e.g. "api_keys" as an unrelated field name) --
                        # already safe, not an infinite-loop risk.
                        continue
                    changed = con.execute(
                        f"update {table} set {column}=? where rowid=? and {column}=?",
                        (new, row["rowid"], old),
                    ).rowcount
                    result["changed"] += int(bool(changed))
                    result["concurrent_skips"] += int(not changed)
                    progressed = progressed or bool(changed)
                con.commit()
                if not progressed:
                    # Nothing in this batch actually changed the stored value
                    # (all remaining matches are safe placeholders/field-name
                    # false positives) -- stop instead of re-selecting the same
                    # 500 rows forever.
                    break
        for table, column in (
            ("download_tasks", "raw_json"), ("source_attempts", "raw_json"), ("bad_source_candidates", "raw_json"),
            ("bad_source_candidates", "source_path"), ("bad_source_candidates", "title"), ("bad_source_candidates", "normalized_title"),
        ):
            if not inkdrop_state.table_exists(con, table) or column not in _columns(con, table):
                continue
            row = con.execute(
                f"select count(*) as c from {table} where lower({column}) like '%apikey%' or lower({column}) like '%api_key%'"
            ).fetchone()
            result["remaining_dirty"] += int(row["c"] or 0)
    return result
