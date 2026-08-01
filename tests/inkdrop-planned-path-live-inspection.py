#!/usr/bin/env python3
"""Inspect import rows for post-default Media Management destination evidence.

This is read-only. It accepts either an InkDrop imports endpoint JSON export or a
URL to fetch with urllib, then reports whether recent imports prove planned-path
application instead of merely previewing a planned path.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.request import urlopen


def load_payload(args):
    if args.input:
        return json.loads(Path(args.input).read_text(encoding="utf-8"))
    if args.url:
        with urlopen(args.url, timeout=float(args.timeout)) as response:  # noqa: S310 - operator-supplied diagnostic URL
            return json.loads(response.read().decode("utf-8", errors="replace"))
    return json.load(sys.stdin)


def response_rows(payload):
    payload = payload if isinstance(payload, dict) else {}
    view = payload.get("view") if isinstance(payload.get("view"), dict) else payload
    rows = view.get("rows") if isinstance(view.get("rows"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def normalized_path(value):
    return str(value or "").strip().replace("\\", "/").rstrip("/")


def path_under(path_value, root_value):
    path = normalized_path(path_value).lower()
    root = normalized_path(root_value).lower()
    return bool(path and root and (path == root or path.startswith(root + "/")))


def row_planned_evidence(row):
    recorded = row.get("media_management_recorded_evidence") if isinstance(row.get("media_management_recorded_evidence"), dict) else {}
    recorded_preview = recorded.get("media_management_preview") if isinstance(recorded.get("media_management_preview"), dict) else {}
    recorded_decision = recorded.get("media_management_destination_decision") if isinstance(recorded.get("media_management_destination_decision"), dict) else {}
    live_preview = row.get("media_management_preview") if isinstance(row.get("media_management_preview"), dict) else {}
    live_decision = row.get("media_management_destination_decision") if isinstance(row.get("media_management_destination_decision"), dict) else {}
    preview = recorded_preview or live_preview
    decision = recorded_decision or live_decision
    nested = preview.get("media_management_destination_decision") if isinstance(preview.get("media_management_destination_decision"), dict) else {}
    if not decision and nested:
        decision = nested
    planned_path = normalized_path(
        decision.get("planned_path")
        or preview.get("planned_path")
        or preview.get("selected_import_dest_path")
    )
    selected_path = normalized_path(
        decision.get("selected_dest_path")
        or preview.get("selected_import_dest_path")
        or planned_path
    )
    dest_path = normalized_path(row.get("dest_path") or row.get("dest") or preview.get("existing_dest_path"))
    source_path = normalized_path(row.get("source_path") or row.get("source"))
    status = str(preview.get("planned_path_apply_status") or "").strip().lower()
    applied = (
        decision.get("applied") is True
        or preview.get("planned_path_applied") is True
        or status in {"selected", "replayed_selected"}
    )
    already_target = status == "already_target" or bool(dest_path and planned_path and dest_path.lower() == planned_path.lower())
    root = normalized_path(preview.get("root"))
    under_root = path_under(dest_path, root) if root and dest_path else None
    selected_matches_dest = bool(selected_path and dest_path and selected_path.lower() == dest_path.lower())
    planned_matches_dest = bool(planned_path and dest_path and planned_path.lower() == dest_path.lower())
    preview_only = preview.get("preview_only")
    evidence_source = "recorded" if recorded_preview or recorded_decision else "live_preview"
    return {
        "id": row.get("id") or row.get("import_id"),
        "queue_id": row.get("queue_id"),
        "series": row.get("series"),
        "issue_number": row.get("issue_number"),
        "created_at_iso": row.get("created_at_iso"),
        "source_path": source_path,
        "dest_path": dest_path,
        "planned_path": planned_path,
        "selected_path": selected_path,
        "root": root,
        "status": status,
        "decision_reason": decision.get("reason"),
        "decision_applied": decision.get("applied"),
        "preview_planned_applied": preview.get("planned_path_applied"),
        "applied": applied,
        "already_target": already_target,
        "selected_matches_dest": selected_matches_dest,
        "planned_matches_dest": planned_matches_dest,
        "under_root": under_root,
        "preview_only": preview_only,
        "evidence_source": evidence_source,
    }


def inspect_rows(rows, *, min_applied=1):
    evidence = [row_planned_evidence(row) for row in rows]
    with_planned = [item for item in evidence if item.get("planned_path")]
    recorded_evidence = [item for item in with_planned if item.get("evidence_source") == "recorded"]
    live_preview_evidence = [item for item in with_planned if item.get("evidence_source") == "live_preview"]
    applied = [
        item for item in with_planned
        if item.get("applied") and item.get("selected_matches_dest")
    ]
    recorded_applied = [
        item for item in recorded_evidence
        if item.get("applied") and item.get("selected_matches_dest")
    ]
    already_target = [
        item for item in with_planned
        if item.get("already_target") and item.get("planned_matches_dest")
    ]
    eligible_not_applied = [
        item for item in with_planned
        if item.get("status") in {"eligible", "selected", "replayed_selected"}
        and not item.get("selected_matches_dest")
    ]
    recorded_eligible_not_applied = [
        item for item in recorded_evidence
        if item.get("status") in {"eligible", "selected", "replayed_selected"}
        and not item.get("selected_matches_dest")
    ]
    live_preview_eligible_not_applied = [
        item for item in live_preview_evidence
        if item.get("status") in {"eligible", "selected", "replayed_selected"}
        and not item.get("selected_matches_dest")
    ]
    outside_root = [
        item for item in with_planned
        if item.get("under_root") is False
    ]
    recorded_outside_root = [
        item for item in recorded_evidence
        if item.get("under_root") is False
    ]
    live_preview_outside_root = [
        item for item in live_preview_evidence
        if item.get("under_root") is False
    ]
    selected_paths = [
        item.get("selected_path") or item.get("planned_path") or item.get("dest_path")
        for item in with_planned
        if item.get("selected_path") or item.get("planned_path") or item.get("dest_path")
    ]
    recorded_selected_paths = [
        item.get("selected_path") or item.get("planned_path") or item.get("dest_path")
        for item in recorded_evidence
        if item.get("selected_path") or item.get("planned_path") or item.get("dest_path")
    ]
    live_preview_selected_paths = [
        item.get("selected_path") or item.get("planned_path") or item.get("dest_path")
        for item in live_preview_evidence
        if item.get("selected_path") or item.get("planned_path") or item.get("dest_path")
    ]
    duplicate_destinations = [
        path for path, count in Counter(selected_paths).items()
        if path and count > 1
    ]
    recorded_duplicate_destinations = [
        path for path, count in Counter(recorded_selected_paths).items()
        if path and count > 1
    ]
    live_preview_duplicate_destinations = [
        path for path, count in Counter(live_preview_selected_paths).items()
        if path and count > 1
    ]
    status_counts = Counter(item.get("status") or "" for item in with_planned)
    evidence_source_counts = Counter(item.get("evidence_source") or "" for item in with_planned)
    min_recorded_applied = max(1, int(min_applied))
    recorded_ok = (
        len(recorded_applied) >= min_recorded_applied
        and not recorded_eligible_not_applied
        and not recorded_outside_root
        and not recorded_duplicate_destinations
    )
    ok = (
        len(applied) >= int(min_applied)
        and not eligible_not_applied
        and not outside_root
        and not duplicate_destinations
    )
    return {
        "ok": ok,
        "status": "ready" if ok else "not_ready",
        "inspected_rows": len(rows),
        "planned_evidence_rows": len(with_planned),
        "applied_rows": len(applied),
        "already_target_rows": len(already_target),
        "eligible_not_applied_rows": len(eligible_not_applied),
        "outside_configured_root_rows": len(outside_root),
        "unique_destination_count": len(set(selected_paths)),
        "duplicate_destination_count": len(duplicate_destinations),
        "recorded_ok": recorded_ok,
        "recorded_evidence_rows": len(recorded_evidence),
        "recorded_applied_rows": len(recorded_applied),
        "recorded_eligible_not_applied_rows": len(recorded_eligible_not_applied),
        "recorded_outside_configured_root_rows": len(recorded_outside_root),
        "recorded_duplicate_destination_count": len(recorded_duplicate_destinations),
        "legacy_preview_only_rows": len(live_preview_evidence),
        "legacy_preview_eligible_not_applied_rows": len(live_preview_eligible_not_applied),
        "legacy_preview_outside_configured_root_rows": len(live_preview_outside_root),
        "legacy_preview_duplicate_destination_count": len(live_preview_duplicate_destinations),
        "status_counts": dict(status_counts),
        "evidence_source_counts": dict(evidence_source_counts),
        "samples": {
            "applied": applied[:5],
            "recorded_applied": recorded_applied[:5],
            "recorded_eligible_not_applied": recorded_eligible_not_applied[:5],
            "already_target": already_target[:5],
            "eligible_not_applied": eligible_not_applied[:10],
            "outside_configured_root": outside_root[:5],
            "duplicate_destinations": duplicate_destinations[:10],
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="Path to an exported InkDrop imports JSON response")
    parser.add_argument("--url", help="InkDrop imports endpoint URL to fetch read-only")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--min-applied", type=int, default=1)
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when evidence is not ready")
    args = parser.parse_args(argv)
    payload = load_payload(args)
    result = inspect_rows(response_rows(payload), min_applied=args.min_applied)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if args.strict and not result.get("ok") else 0


if __name__ == "__main__":
    raise SystemExit(main())
