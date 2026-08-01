#!/usr/bin/env python3
"""Dry-run-first reconciliation for duplicate external issue identities."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path


CHILD_TABLES = ("wanted_items", "queue_items", "source_attempts", "download_tasks", "import_results", "history_events")
NATIVE_PROVIDERS = {"comicvine", "mangadex"}


def _json(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _key(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _edition(issue, series):
    raw = _json(issue.get("raw_json"))
    return {
        "series_id": issue.get("series_id"),
        "number": str(issue.get("normalized_number") or issue.get("issue_number") or "").strip().lstrip("0") or "0",
        "title": _key(issue.get("title")),
        "year": raw.get("year") or raw.get("release_year") or series.get("year"),
        "volume": raw.get("volume") or raw.get("volume_number") or raw.get("volumeNumber"),
        "edition": _key(raw.get("edition") or raw.get("format") or raw.get("unit_type")),
    }


def _equivalence(left, right, series):
    a, b = _edition(left, series), _edition(right, series)
    reasons = []
    conflicts = []
    if a["series_id"] != b["series_id"]: conflicts.append("different_series")
    if a["number"] != b["number"]: conflicts.append("different_issue_number")
    if a["title"] and b["title"] and a["title"] != b["title"]: conflicts.append("different_title")
    for field in ("year", "volume", "edition"):
        if a[field] not in (None, "") and b[field] not in (None, "") and str(a[field]) != str(b[field]):
            conflicts.append(f"different_{field}")
    left_raw, right_raw = _json(left.get("raw_json")), _json(right.get("raw_json"))
    left_ids = {str(left.get("metadata_id") or ""), str(left.get("kapowarr_issue_id") or ""), str(left_raw.get("id") or ""), str(left_raw.get("kapowarrIssueId") or "")}
    right_ids = {str(right.get("metadata_id") or ""), str(right.get("kapowarr_issue_id") or ""), str(right_raw.get("id") or ""), str(right_raw.get("kapowarrIssueId") or "")}
    linked = bool((left_ids - {""}) & (right_ids - {""}))
    if linked: reasons.append("cross_linked_external_identity")
    if a["title"] and a["title"] == b["title"]: reasons.append("same_title")
    if a["number"] == b["number"]: reasons.append("same_issue_number")
    if conflicts: return "conflict", reasons, conflicts
    if linked and "same_issue_number" in reasons: return "equivalent", reasons, []
    return "uncertain", reasons, []


def _row(con, issue_id):
    row = con.execute("select * from issues where id=?", (issue_id,)).fetchone()
    return dict(row) if row else None


def _evidence(con, issue):
    output = {"issue": {k: issue.get(k) for k in issue if k != "raw_json"}, "links": {}}
    for table in CHILD_TABLES:
        columns = {row[1] for row in con.execute(f"pragma table_info({table})")}
        order = "created_at desc" if "created_at" in columns else "rowid desc"
        rows = con.execute(f"select * from {table} where issue_id=? order by {order} limit 100", (issue["id"],)).fetchall()
        output["links"][table] = [dict(row) for row in rows]
    return output


def reconciliation_plan(db_path, issue_ids):
    ids = [str(value or "").strip() for value in issue_ids if str(value or "").strip()]
    if len(ids) != 2:
        raise ValueError("exactly two issue IDs are required")
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=1")
    try:
        issues = [_row(con, issue_id) for issue_id in ids]
        if not all(issues):
            missing = [ids[index] for index, issue in enumerate(issues) if not issue]
            return {"ok": False, "classification": "missing", "missing_issue_ids": missing}
        series_row = con.execute("select * from series where id=?", (issues[0]["series_id"],)).fetchone()
        series = dict(series_row) if series_row else {}
        classification, reasons, conflicts = _equivalence(issues[0], issues[1], series)
        ranked = sorted(issues, key=lambda row: (str(row.get("metadata_provider") or "").lower() in NATIVE_PROVIDERS, bool(row.get("metadata_id")), -float(row.get("created_at") or 0)), reverse=True)
        canonical, duplicate = ranked[0], ranked[1]
        return {
            "ok": True,
            "dry_run": True,
            "classification": classification,
            "reasons": reasons,
            "conflicts": conflicts,
            "series": {k: series.get(k) for k in ("id", "title", "year", "publisher", "metadata_provider", "metadata_id")},
            "canonical_issue_id": canonical["id"],
            "duplicate_issue_id": duplicate["id"],
            "canonical": _evidence(con, canonical),
            "duplicate": _evidence(con, duplicate),
            "apply_allowed": classification == "equivalent",
            "proposed_actions": [
                "Preserve both external identities as linked aliases",
                "Re-parent wanted, queue, source, task, import, and history evidence",
                "Retire the duplicate issue record without deleting it",
                "Write one reconciliation audit event",
            ] if classification == "equivalent" else ["Retain explicit identity conflict for operator review"],
        }
    finally:
        con.close()


def apply_reconciliation(db_path, issue_ids, *, backup_path):
    plan = reconciliation_plan(db_path, issue_ids)
    if not plan.get("apply_allowed"):
        return {**plan, "applied": False, "reason": "equivalence_not_proven"}
    db_path, backup_path = Path(db_path), Path(backup_path)
    if backup_path.exists() or backup_path.resolve() == db_path.resolve():
        raise ValueError("backup path must be new and different from the database")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(db_path, timeout=30)
    backup = sqlite3.connect(backup_path, timeout=30)
    source.backup(backup)
    backup.close()
    canonical, duplicate = plan["canonical_issue_id"], plan["duplicate_issue_id"]
    now = time.time()
    source.row_factory = sqlite3.Row
    try:
        source.execute("pragma foreign_keys=on")
        source.execute("begin immediate")
        canonical_row = _row(source, canonical)
        duplicate_row = _row(source, duplicate)
        duplicate_raw = _json(duplicate_row.get("raw_json"))
        if duplicate_raw.get("canonical_issue_id") == canonical:
            source.rollback()
            return {**plan, "applied": False, "idempotent": True, "backup_path": str(backup_path)}
        canonical_raw = _json(canonical_row.get("raw_json"))
        aliases = canonical_raw.get("linked_external_identities")
        aliases = aliases if isinstance(aliases, list) else []
        alias = {"issue_id": duplicate, "provider": duplicate_row.get("metadata_provider"), "metadata_id": duplicate_row.get("metadata_id"), "kapowarr_issue_id": duplicate_row.get("kapowarr_issue_id")}
        if alias not in aliases: aliases.append(alias)
        canonical_raw["linked_external_identities"] = aliases
        duplicate_raw.update({"retired_duplicate": True, "canonical_issue_id": canonical, "retired_at": now})
        source.execute("update issues set raw_json=?,updated_at=? where id=?", (json.dumps(canonical_raw, separators=(",", ":"), sort_keys=True), now, canonical))
        source.execute("update issues set monitored=0,raw_json=?,updated_at=? where id=?", (json.dumps(duplicate_raw, separators=(",", ":"), sort_keys=True), now, duplicate))
        counts = {}
        for table in CHILD_TABLES:
            if table == "history_events":
                cur = source.execute("update history_events set issue_id=? where issue_id=?", (canonical, duplicate))
            else:
                cur = source.execute(f"update {table} set issue_id=? where issue_id=?", (canonical, duplicate))
            counts[table] = max(0, int(cur.rowcount or 0))
        event_id = f"identity-reconcile:{canonical}:{int(now * 1000)}"
        source.execute(
            "insert into history_events(id,entity_type,entity_id,series_id,issue_id,event_type,source,message,created_at,raw_json,outcome,display_phase) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "issue", canonical, canonical_row["series_id"], canonical, "issue_identity_reconciled", "inkdrop_issue_identity", "Equivalent external issue identities reconciled", now, json.dumps({"canonical_issue_id": canonical, "retired_issue_id": duplicate, "counts": counts}, separators=(",", ":"), sort_keys=True), "repaired", "identity_repair"),
        )
        source.commit()
        return {**plan, "dry_run": False, "applied": True, "backup_path": str(backup_path), "reparented": counts, "audit_event_id": event_id}
    except Exception:
        source.rollback()
        raise
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(description="Inspect or reconcile two InkDrop issue identities")
    parser.add_argument("--db", required=True)
    parser.add_argument("--issue-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error("--apply requires --backup")
    result = apply_reconciliation(args.db, args.issue_id, backup_path=args.backup) if args.apply else reconciliation_plan(args.db, args.issue_id)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
