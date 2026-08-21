#!/usr/bin/env python3
"""Report and repair drift between InkDrop's database and the real library on disk.

Composes the existing managed-library scan/ledger machinery in inkdrop_state.py
(untracked files, dangling ledger rows, on-disk duplicate files) with checks that
don't exist anywhere else yet: two active files claiming the same issue, a file
that no longer sits under its series' current library folder, and settings rows
still carrying the pre-fix personal readarr defaults. Report mode never writes.
Repair mode only ever performs the one already-safe, idempotent action InkDrop's
own scheduler already runs periodically (refreshing media_files present/missing
status from what is actually on disk); everything else is report-only because it
requires a human to pick which file or row is the keeper.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from core import inkdrop_runtime_config
from core import inkdrop_state

# Each category already caps how many GROUPS it reports (limit=2000) and records
# `truncated` when it hits that. Nothing capped the members *inside* a group: a
# single multi-file conflict returned every media_files row for that issue, and a
# duplicate-number group every issues row, each carrying a full path or title. So
# the document's worst-case size was set by the data rather than by us -- 2,000
# groups times an unbounded member list. The two bounds are independent, and so
# are their truncation flags: a run can be complete in groups and truncated in
# members, and the report has to be able to say that.
MEMBER_DETAIL_LIMIT = 50

LEGACY_QBIT_EBOOKS_CATEGORY = "readarr"
LEGACY_QBIT_EBOOKS_SAVE_PATH = "/downloads/readarr"


def _rows(con, sql, params=()):
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def _capped(con, count_sql, count_params, group_sql, group_params, limit, build_item):
    """Run a `having count(*) > 1`-style query without silently swallowing overflow.

    Returns {items, total, truncated} so a report can never present a truncated
    list as if it were the whole picture -- the group count is always queried
    separately, unbounded, before the (possibly limit-bounded) detail query.
    """
    total = con.execute(count_sql, count_params).fetchone()[0]
    groups = _rows(con, group_sql, (*group_params, limit))
    items = [build_item(group) for group in groups]
    return {"items": items, "total": int(total or 0), "truncated": int(total or 0) > len(items)}


def multi_file_issue_conflicts(con, limit=2000, member_limit=MEMBER_DETAIL_LIMIT):
    """Active media_files rows that disagree about which file satisfies one issue.

    media_files.normalized_path is unique, so two present files for the same
    issue only happens when a re-acquire, a rename, or a folder move left the
    old file behind instead of retiring it. Neither file is deleted here -- a
    human needs to look at both and decide which one is real.
    """
    not_removed = inkdrop_state.series_not_removed_sql("s")

    def build_item(group):
        files = _rows(
            con,
            """
            select path, normalized_path, status, size_bytes, mtime, last_seen_at
            from media_files
            where issue_id = ? and active = 1
            order by last_seen_at desc
            limit ?
            """,
            (group["issue_id"], member_limit),
        )
        # file_count comes from the unbounded group query, so the true total
        # survives even when the member list below is cut.
        total_files = int(group.get("file_count") or len(files))
        return {
            "category": "multi_file_issue_conflict",
            "requires_human_review": True,
            "series_id": group.get("series_id"),
            "series_title": group.get("series_title"),
            "media_type": group.get("series_media_type"),
            "issue_id": group.get("issue_id"),
            "issue_number": group.get("issue_number"),
            "file_count": total_files,
            "files": files,
            "files_truncated": total_files > len(files),
        }

    return _capped(
        con,
        f"""
        select count(*) from (
            select mf.issue_id
            from media_files mf
            left join series s on s.id = mf.series_id
            where mf.active = 1 and coalesce(mf.issue_id, '') != '' and {not_removed}
            group by mf.issue_id
            having count(distinct mf.normalized_path) > 1
        )
        """,
        (),
        f"""
        select mf.issue_id, mf.series_id, s.title as series_title, s.media_type as series_media_type,
               i.issue_number, i.normalized_number,
               count(distinct mf.normalized_path) as file_count
        from media_files mf
        left join series s on s.id = mf.series_id
        left join issues i on i.id = mf.issue_id
        where mf.active = 1 and coalesce(mf.issue_id, '') != '' and {not_removed}
        group by mf.issue_id
        having count(distinct mf.normalized_path) > 1
        order by file_count desc, mf.issue_id
        limit ?
        """,
        (),
        limit,
        build_item,
    )


def naming_scheme_drift(con, limit=2000):
    """Active files that no longer live under their series' current library_path.

    This is not a duplicate check -- it catches the case where a series was
    renamed, merged, or had its library path re-templated, but the physical
    file (or InkDrop's record of it) never followed. Flagged only; moving a
    file on a real library needs a human to confirm the new home is correct.
    """
    series_rows = _rows(
        con,
        f"""
        select id, title, media_type, library_path
        from series s
        where coalesce(library_path, '') != '' and {inkdrop_state.series_not_removed_sql("s")}
        """,
    )
    series_by_id = {row["id"]: row for row in series_rows}
    if not series_by_id:
        return {"items": [], "total": 0, "truncated": False}
    file_rows = _rows(
        con,
        """
        select mf.path, mf.normalized_path, mf.series_id, mf.issue_id, mf.status
        from media_files mf
        where mf.active = 1 and coalesce(mf.series_id, '') != ''
        """,
    )
    findings = []
    for row in file_rows:
        series = series_by_id.get(row["series_id"])
        if not series:
            continue
        expected_prefix = inkdrop_state.media_file_normalized_path(series.get("library_path"))
        if not expected_prefix:
            continue
        actual = str(row.get("normalized_path") or "")
        if not actual or inkdrop_state.path_under_any_root(actual, [expected_prefix]):
            continue
        findings.append(
            {
                "category": "naming_scheme_drift",
                "requires_human_review": True,
                "series_id": row.get("series_id"),
                "series_title": series.get("title"),
                "issue_id": row.get("issue_id"),
                "file_path": row.get("path"),
                "expected_library_path": series.get("library_path"),
            }
        )
    return {
        "items": findings[:limit],
        "total": len(findings),
        "truncated": len(findings) > limit,
    }


def duplicate_issue_number_rows(con, limit=2000, member_limit=MEMBER_DETAIL_LIMIT):
    """Same series + same normalized issue number recorded as two distinct issue rows.

    There is no unique index on issues(series_id, normalized_number), so this can
    happen; InkDrop already reconciles the downstream wanted/queue fallout for
    known unit-collision cases (manga volume vs. chapter), but the duplicate
    issue rows themselves are never merged. Reported for a human, not auto-merged,
    since collapsing two issue rows can orphan history tied to whichever one is
    dropped.
    """
    not_removed = inkdrop_state.series_not_removed_sql("s")

    def build_item(group):
        issue_rows = _rows(
            con,
            """
            select id, issue_number, title, metadata_provider, metadata_id, created_at
            from issues
            where series_id = ? and normalized_number = ?
            order by created_at
            limit ?
            """,
            (group["series_id"], group["normalized_number"], member_limit),
        )
        total_rows = int(group.get("row_count") or len(issue_rows))
        return {
            "category": "duplicate_issue_number_row",
            "requires_human_review": True,
            "series_id": group.get("series_id"),
            "series_title": group.get("series_title"),
            "normalized_number": group.get("normalized_number"),
            "row_count": total_rows,
            "issues": issue_rows,
            "issues_truncated": total_rows > len(issue_rows),
        }

    return _capped(
        con,
        f"""
        select count(*) from (
            select i.series_id
            from issues i
            left join series s on s.id = i.series_id
            where coalesce(i.normalized_number, '') != '' and {not_removed}
            group by i.series_id, i.normalized_number
            having count(*) > 1
        )
        """,
        (),
        f"""
        select i.series_id, s.title as series_title, i.normalized_number,
               count(*) as row_count
        from issues i
        left join series s on s.id = i.series_id
        where coalesce(i.normalized_number, '') != '' and {not_removed}
        group by i.series_id, i.normalized_number
        having count(*) > 1
        order by row_count desc, i.series_id
        limit ?
        """,
        (),
        limit,
        build_item,
    )


def legacy_settings_drift(con):
    """Persisted settings still carrying the pre-fix personal readarr defaults.

    bd92b072 changed the fallback default (used when a setting is absent), but
    an install that already had the literal old value written to storage keeps
    it forever -- the new default only helps fresh installs. This only detects
    the leftover value; clearing it needs a migration in inkdrop_state.py,
    which is out of scope here (see the top-level report's "known_gaps").
    """
    findings = []
    provider_rows = _rows(
        con,
        """
        select id, provider_type, display_name, settings_json
        from provider_configs
        where coalesce(settings_json, '') != ''
        """,
    )
    for row in provider_rows:
        settings = inkdrop_state.json_loads(row.get("settings_json") or "{}", {})
        if not isinstance(settings, dict):
            continue
        hits = {}
        if str(settings.get("ebooks_category") or "").strip() == LEGACY_QBIT_EBOOKS_CATEGORY:
            hits["ebooks_category"] = settings.get("ebooks_category")
        if str(settings.get("ebooks_save_path") or "").strip() == LEGACY_QBIT_EBOOKS_SAVE_PATH:
            hits["ebooks_save_path"] = settings.get("ebooks_save_path")
        if hits:
            findings.append(
                {
                    "category": "legacy_readarr_default_setting",
                    "requires_human_review": True,
                    "provider_config_id": row.get("id"),
                    "provider_type": row.get("provider_type"),
                    "display_name": row.get("display_name"),
                    "fields": hits,
                    "note": "Needs a data migration in inkdrop_state.py to clear; not fixed by this tool.",
                }
            )
    return findings


def build_reconciliation_report(db_path, *, max_files=50000, sample_limit=50, include_disk_scan=True):
    path = Path(db_path)
    if not path.exists():
        return {"ok": False, "reason": "state_db_missing", "db_path": str(path)}
    with inkdrop_state.connect_read(path, timeout_seconds=10, busy_timeout_ms=10000) as con:
        report = {
            "ok": True,
            "schema": "inkdrop.library_reconciliation_report.v1",
            "checked_at": time.time(),
            "db_path": str(path),
        }
        if include_disk_scan:
            report["disk_scan"] = inkdrop_state.managed_library_scan_audit_from_connection(
                con,
                max_files=max_files,
                sample_limit=sample_limit,
                include_frontend=False,
                include_cleanup_plan=False,
            )
        report["media_file_inventory"] = inkdrop_state.media_file_inventory_rollup(con)
        report["multi_file_issue_conflicts"] = multi_file_issue_conflicts(con)
        report["naming_scheme_drift"] = naming_scheme_drift(con)
        report["duplicate_issue_number_rows"] = duplicate_issue_number_rows(con)
        report["legacy_settings_drift"] = legacy_settings_drift(con)
        report["summary"] = {
            "untracked_files_on_disk": report.get("disk_scan", {}).get("untracked_files", 0) if include_disk_scan else None,
            "dangling_ledger_rows": report.get("disk_scan", {}).get("missing_ledger_files", 0) if include_disk_scan else None,
            "on_disk_duplicate_file_groups": report.get("disk_scan", {}).get("actual_duplicate_file_groups", 0) if include_disk_scan else None,
            "multi_file_issue_conflicts": report["multi_file_issue_conflicts"]["total"],
            "naming_scheme_drift_files": report["naming_scheme_drift"]["total"],
            "duplicate_issue_number_groups": report["duplicate_issue_number_rows"]["total"],
            "legacy_settings_findings": len(report["legacy_settings_drift"]),
        }
        truncated = [
            key
            for key in ("multi_file_issue_conflicts", "naming_scheme_drift", "duplicate_issue_number_rows")
            if report[key]["truncated"]
        ]
        report["known_gaps"] = [
            "legacy_readarr_default_setting findings are report-only: clearing them needs a "
            "data migration inside inkdrop_state.py (not editable by this tool).",
        ]
        if truncated:
            report["known_gaps"].append(
                f"truncated at their per-run limit, true totals are higher: {', '.join(truncated)} "
                "(see each section's own total/truncated fields)."
            )
        # Group truncation and member truncation are separate facts. A run can
        # report every group it found and still have cut the file list inside
        # one of them, so this is counted independently rather than folded into
        # the flag above -- a reader must not take a complete group list as a
        # complete document.
        member_truncated = {
            key: sum(
                1
                for item in report[key]["items"]
                if item.get("files_truncated") or item.get("issues_truncated")
            )
            for key in ("multi_file_issue_conflicts", "duplicate_issue_number_rows")
        }
        member_truncated = {key: count for key, count in member_truncated.items() if count}
        report["summary"]["groups_with_truncated_members"] = sum(member_truncated.values())
        if member_truncated:
            detail = ", ".join(f"{key}: {count} group(s)" for key, count in sorted(member_truncated.items()))
            report["known_gaps"].append(
                f"per-group member lists capped at {MEMBER_DETAIL_LIMIT}; the full member list was cut "
                f"for {detail}. Each affected item carries its true count and a *_truncated flag."
            )
        return report


def refresh_media_file_ledger(db_path, *, timeout_seconds=10.0, busy_timeout_ms=10000):
    """The one safe, idempotent repair action: recompute present/missing from disk.

    This calls the same inkdrop_state.sync_managed_media_files that already runs
    inside every periodic sync_state pass -- it only ever sets a media_files row's
    status/active flag to match whether the file it points at exists right now.
    It never deletes rows, never touches series/issues, and never picks between
    two candidate files.
    """
    path = Path(db_path)
    if not path.exists():
        return {"ok": False, "reason": "state_db_missing", "db_path": str(path)}
    now = time.time()
    with inkdrop_state.connect(path, timeout_seconds=timeout_seconds, busy_timeout_ms=busy_timeout_ms) as con:
        inkdrop_state.init_schema(con)
        result = inkdrop_state.sync_managed_media_files(con, now=now)
        con.commit()
    return {"ok": True, "action": "refresh_media_file_ledger", **result}


def run_repair(db_path, *, apply=False, max_files=50000, sample_limit=50):
    report = build_reconciliation_report(db_path, max_files=max_files, sample_limit=sample_limit)
    if not report.get("ok"):
        return report
    result = {
        "ok": True,
        "mode": "repair",
        "applied": bool(apply),
        "report": report,
    }
    if apply:
        result["safe_repair"] = refresh_media_file_ledger(db_path)
    else:
        result["safe_repair"] = {
            "ok": True,
            "action": "refresh_media_file_ledger",
            "dry_run": True,
            "note": "Pass --apply to actually refresh present/missing status from disk.",
        }
    result["flagged_for_human_review"] = {
        "untracked_files_on_disk": report.get("summary", {}).get("untracked_files_on_disk"),
        "on_disk_duplicate_file_groups": report.get("summary", {}).get("on_disk_duplicate_file_groups"),
        "multi_file_issue_conflicts": report.get("summary", {}).get("multi_file_issue_conflicts"),
        "naming_scheme_drift_files": report.get("summary", {}).get("naming_scheme_drift_files"),
        "duplicate_issue_number_groups": report.get("summary", {}).get("duplicate_issue_number_groups"),
        "legacy_settings_findings": report.get("summary", {}).get("legacy_settings_findings"),
    }
    return result


def _print_human_summary(payload):
    summary = payload.get("summary") or payload.get("report", {}).get("summary") or {}
    print("InkDrop library reconciliation")
    print("-" * 40)
    if not payload.get("ok"):
        # A failure has to stay visible in the logs -- the fix for the leak is
        # not to go quiet when the job breaks. `reason` is a fixed vocabulary
        # ("state_db_missing"), never interpolated user data, so it says what
        # happened without reprinting the absolute path that `payload["db_path"]`
        # carries for the cache file's benefit.
        print(f"status: FAILED ({payload.get('reason') or 'unknown_error'})", file=sys.stderr)
        return
    for key, value in summary.items():
        print(f"{key}: {value}")
    if payload.get("mode") == "repair":
        safe = payload.get("safe_repair", {})
        print("-" * 40)
        if safe.get("dry_run"):
            print("repair: dry run only (pass --apply to refresh the ledger)")
        else:
            print(f"repair: refreshed ledger -- {safe}")


def write_cache_file(cache_path, payload, params):
    """Write the same {completed_at, params, report} envelope the web admin
    action's background thread writes, so a scheduled run and a human clicking
    "run" in System > Advanced converge on one cached-status file.
    """
    completed = time.time()
    envelope = {
        "completed_at": completed,
        "completed_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(completed)),
        "params": params,
        "report": payload,
    }
    cache_path = Path(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    # This file is the only place the path-bearing detail is allowed to live,
    # so it is created 0600 by its owner rather than written at the umask's
    # discretion and fixed up afterwards -- there is no window where it is
    # readable and then narrowed. mkstemp also gives a unique name: the old
    # fixed ".{name}.tmp" meant two runs (the scheduled job and a human
    # clicking "run" in System > Advanced, which share this writer) could
    # interleave on one temp path.
    fd, temp_name = tempfile.mkstemp(dir=str(cache_path.parent), prefix=f".{cache_path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, indent=2, sort_keys=True, default=str) + "\n")
        os.replace(temp_name, cache_path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("report", "repair"), default="report")
    parser.add_argument("--state-db", default=None, help="Path to inkdrop-state.sqlite3; defaults to the runtime config's state DB")
    parser.add_argument("--max-files", type=int, default=50000)
    parser.add_argument("--sample-limit", type=int, default=50)
    parser.add_argument("--apply", action="store_true", help="repair mode only: actually refresh the media_files ledger from disk")
    parser.add_argument("--json", action="store_true", help="print the full JSON report to stdout; ignored when --cache-file is given, because the detail then belongs to the cache file only")
    parser.add_argument("--cache-file", default=None, help="report mode only: write the result to this path in the System > Advanced cache-file shape. Supplying it makes stdout counts-only.")
    args = parser.parse_args()

    db_path = Path(args.state_db) if args.state_db else inkdrop_runtime_config.state_db_path()

    if args.mode == "report":
        payload = build_reconciliation_report(db_path, max_files=args.max_files, sample_limit=args.sample_limit)
        if args.cache_file:
            write_cache_file(args.cache_file, payload, {"max_files": args.max_files, "sample_limit": args.sample_limit})
    else:
        payload = run_repair(db_path, apply=args.apply, max_files=args.max_files, sample_limit=args.sample_limit)

    # The scheduled run is a container process: whatever this prints lands in
    # ordinary Docker log retention, which is a different audience and a
    # different lifetime from the authenticated cache file. The report carries
    # series titles, filenames, source and expected library paths and the
    # absolute state-DB path, so when a cache file is being written the detail
    # goes there and stdout gets counts only.
    #
    # This is a property of the CLI rather than of one scheduler entry on
    # purpose. Removing --json from the scheduled command fixes today; making
    # --cache-file authoritative means re-adding it later cannot re-open this,
    # which is the failure this shape is guarding against.
    detail_to_stdout = bool(args.json) and not args.cache_file
    if args.json and args.cache_file:
        print(
            "library reconciliation: full detail written to the cache file; "
            "stdout is counts-only (--json ignored alongside --cache-file)",
            file=sys.stderr,
        )

    if detail_to_stdout:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _print_human_summary(payload)

    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
