#!/usr/bin/env python3
"""Bounded, read-only acquisition funnel diagnostics."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _rows(con, sql, params=()):
    return [dict(row) for row in con.execute(sql, params)]


def _concrete_safe_candidate_count(
    safe_count,
    candidate_identity,
    _download_url_hash,
    download_client,
    raw_json,
    linked_task_ready,
    status,
    source,
):
    """Return the safe count only when the same attempt has concrete evidence."""
    try:
        safe_count = max(0, int(safe_count or 0))
    except (TypeError, ValueError):
        safe_count = 0
    if not safe_count:
        return 0
    status = str(status or "").strip().lower()
    source = str(source or "").strip().lower()
    if source == "queue_activity":
        return 0
    if bool(linked_task_ready):
        return safe_count
    client = str(download_client or "").strip()
    if status != "sent" and str(candidate_identity or "").strip():
        return safe_count
    try:
        raw = json.loads(raw_json or "{}") if not isinstance(raw_json, dict) else raw_json
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        return 0
    candidates = []
    for value in (raw.get("candidate"), (raw.get("raw") or {}).get("candidate") if isinstance(raw.get("raw"), dict) else None):
        if isinstance(value, dict):
            candidates.append(value)
    for candidate in candidates:
        if status != "sent" and str(candidate.get("candidate_identity") or candidate.get("provider_candidate_identity") or "").strip():
            return safe_count
        locator = candidate.get("download_url") or candidate.get("magnet_url") or candidate.get("source_path")
        candidate_client = candidate.get("download_client") or client
        if str(locator or "").strip() and str(candidate_client or "").strip():
            return safe_count
    seeds = []
    for value in (raw.get("download_task_seed"), (raw.get("raw") or {}).get("download_task_seed") if isinstance(raw.get("raw"), dict) else None):
        if isinstance(value, dict):
            seeds.append(value)
    for seed in seeds:
        seed_identity = seed.get("candidate_identity") or seed.get("external_id")
        seed_client = seed.get("download_client") or client
        if str(seed_identity or "").strip() and str(seed_client or "").strip():
            return safe_count
    return 0


def _group(con, table, key, cutoff, timestamp, extra=""):
    return _rows(
        con,
        f"""select coalesce(nullif(trim({key}),''),'unknown') as key, count(*) as count
        from {table}
        where coalesce({timestamp},0)>=? {extra}
        group by coalesce(nullif(trim({key}),''),'unknown')
        order by count(*) desc, key limit 50""",
        (cutoff,),
    )


def _scalar(con, sql, params=()):
    row = con.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def _media_group(con, table, cutoff=None, timestamp=None, where="1=1"):
    params = () if cutoff is None else (cutoff,)
    time_clause = "" if cutoff is None else f"and coalesce({timestamp},0)>=?"
    return _rows(
        con,
        f"""select coalesce(nullif(trim(s.media_type),''),'unknown') as key,count(*) as count
        from {table} t left join series s on s.id=t.series_id
        where {where} {time_clause}
        group by coalesce(nullif(trim(s.media_type),''),'unknown') order by count(*) desc""",
        params,
    )


def build_acquisition_funnel(db_path, hours=12, now=None):
    path = Path(db_path)
    now = time.time() if now is None else float(now)
    hours = max(1, min(int(hours or 12), 168))
    cutoff = now - hours * 3600
    started = time.perf_counter()
    if not path.exists():
        return {"ok": False, "error": "InkDrop state database not found", "hours": hours}
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2.0) as con:
        con.row_factory = sqlite3.Row
        con.execute("pragma query_only=1")
        con.execute("pragma busy_timeout=1500")
        eligible = {
            "active_queue_items": _scalar(con, "select count(*) from queue_items where active=1"),
            "active_wanted_items": _scalar(con, "select count(*) from wanted_items where status in ('wanted','in_progress','grabbed','searching','blocked')"),
            "retry_due": _scalar(con, "select count(*) from queue_items where active=1 and retry_after is not null and retry_after<=?", (now,)),
            "retry_unscheduled": _scalar(con, "select count(*) from queue_items where active=1 and state='queued' and retry_after is null"),
            "currently_claimed": _scalar(con, "select count(*) from queue_claims where expires_at>?", (now,)),
            "active_queue_by_media_type": _media_group(con, "queue_items", where="t.active=1"),
        }
        attempts = {
            "total": _scalar(con, "select count(*) from source_attempts where coalesce(completed_at,started_at,0)>=?", (cutoff,)),
            "distinct_queue_items": _scalar(con, "select count(distinct queue_id) from source_attempts where coalesce(completed_at,started_at,0)>=?", (cutoff,)),
            "provider_calls_started": _scalar(con, "select count(*) from source_attempts where coalesce(completed_at,started_at,0)>=? and status='searching'", (cutoff,)),
            "provider_results_with_candidate_identity": _scalar(con, "select count(*) from source_attempts where coalesce(completed_at,started_at,0)>=? and nullif(trim(candidate_identity),'') is not null", (cutoff,)),
            "by_media_type": _media_group(con, "source_attempts", cutoff, "coalesce(t.completed_at,t.started_at,0)"),
            "by_source": _group(con, "source_attempts", "source", cutoff, "coalesce(completed_at,started_at,0)"),
            "by_provider": _group(con, "source_attempts", "coalesce(provider_id,provider)", cutoff, "coalesce(completed_at,started_at,0)"),
            "by_status": _group(con, "source_attempts", "status", cutoff, "coalesce(completed_at,started_at,0)"),
            "by_outcome": _group(con, "source_attempts", "outcome", cutoff, "coalesce(completed_at,started_at,0)"),
            "top_failures": _group(con, "source_attempts", "failure_reason", cutoff, "coalesce(completed_at,started_at,0)", "and nullif(trim(failure_reason),'') is not null"),
        }
        candidates = {
            "normalized": _scalar(con, "select count(*) from source_attempts where coalesce(completed_at,started_at,0)>=? and nullif(trim(candidate_identity),'') is not null", (cutoff,)),
            "safe_accepted": _scalar(con, "select count(*) from source_attempts where coalesce(completed_at,started_at,0)>=? and nullif(trim(candidate_identity),'') is not null and outcome in ('productive','in_progress')", (cutoff,)),
            "rejected_with_identity": _scalar(con, "select count(*) from source_attempts where coalesce(completed_at,started_at,0)>=? and nullif(trim(candidate_identity),'') is not null and outcome in ('no_candidate','problem')", (cutoff,)),
            "productive_or_in_progress": _scalar(con, "select count(*) from source_attempts where coalesce(completed_at,started_at,0)>=? and outcome in ('productive','in_progress')", (cutoff,)),
            "no_candidate_or_problem": _scalar(con, "select count(*) from source_attempts where coalesce(completed_at,started_at,0)>=? and outcome in ('no_candidate','problem')", (cutoff,)),
            "retry_later": _scalar(con, "select count(*) from source_attempts where coalesce(completed_at,started_at,0)>=? and outcome='retry_later'", (cutoff,)),
        }
        downloads = {
            "created": _scalar(con, "select count(*) from download_tasks where coalesce(started_at,updated_at,0)>=?", (cutoff,)),
            "updated": _scalar(con, "select count(*) from download_tasks where coalesce(updated_at,started_at,0)>=?", (cutoff,)),
            "distinct_queue_items": _scalar(con, "select count(distinct queue_id) from download_tasks where coalesce(updated_at,started_at,0)>=?", (cutoff,)),
            "handoffs_accepted": _scalar(con, "select count(*) from download_tasks where coalesce(started_at,updated_at,0)>=? and nullif(trim(external_id),'') is not null", (cutoff,)),
            "started": _scalar(con, "select count(*) from download_tasks where coalesce(updated_at,started_at,0)>=? and state in ('downloading','import_ready','importing','verified')", (cutoff,)),
            "completed": _scalar(con, "select count(*) from download_tasks where coalesce(updated_at,started_at,0)>=? and state in ('import_ready','importing','verified')", (cutoff,)),
            "by_media_type": _media_group(con, "download_tasks", cutoff, "coalesce(t.updated_at,t.started_at,0)"),
            "by_client": _group(con, "download_tasks", "coalesce(download_client,protocol,source)", cutoff, "coalesce(updated_at,started_at,0)"),
            "by_state": _group(con, "download_tasks", "state", cutoff, "coalesce(updated_at,started_at,0)"),
            "by_status": _group(con, "download_tasks", "status", cutoff, "coalesce(updated_at,started_at,0)"),
        }
        imports = {
            "results": _scalar(con, "select count(*) from import_results where coalesce(created_at,0)>=?", (cutoff,)),
            "verified": _scalar(con, "select count(*) from import_results where coalesce(created_at,0)>=? and verified=1", (cutoff,)),
            "folder_imported": _scalar(con, "select count(*) from import_results where coalesce(created_at,0)>=? and folder_imported=1", (cutoff,)),
            "by_media_type": _media_group(con, "import_results", cutoff, "t.created_at"),
            "by_status": _group(con, "import_results", "status", cutoff, "created_at"),
            "by_outcome": _group(con, "import_results", "outcome", cutoff, "created_at"),
        }
        completion = {
            "wanted_satisfied": _scalar(con, "select count(*) from wanted_items where updated_at>=? and status='satisfied'", (cutoff,)),
            "media_files_first_seen": _scalar(con, "select count(*) from media_files where first_seen_at>=?", (cutoff,)),
            "media_files_seen": _scalar(con, "select count(*) from media_files where last_seen_at>=?", (cutoff,)),
        }
        current_queue = {
            "by_state": _rows(con, "select state as key,count(*) as count from queue_items where active=1 group by state order by count(*) desc"),
            "touched_in_window": _scalar(con, "select count(*) from queue_items q where q.active=1 and (exists(select 1 from source_attempts sa where sa.queue_id=q.id and coalesce(sa.completed_at,sa.started_at,0)>=?) or exists(select 1 from download_tasks dt where dt.queue_id=q.id and coalesce(dt.updated_at,dt.started_at,0)>=?) or exists(select 1 from import_results ir where ir.queue_id=q.id and coalesce(ir.created_at,0)>=?))", (cutoff, cutoff, cutoff)),
            "untouched_in_window": _scalar(con, "select count(*) from queue_items q where q.active=1 and not exists(select 1 from source_attempts sa where sa.queue_id=q.id and coalesce(sa.completed_at,sa.started_at,0)>=?) and not exists(select 1 from download_tasks dt where dt.queue_id=q.id and coalesce(dt.updated_at,dt.started_at,0)>=?) and not exists(select 1 from import_results ir where ir.queue_id=q.id and coalesce(ir.created_at,0)>=?)", (cutoff, cutoff, cutoff)),
            "age_buckets": _rows(con, """select case when ?-coalesce(created_at,?)<3600 then 'under_1h' when ?-coalesce(created_at,?)<21600 then '1h_to_6h' when ?-coalesce(created_at,?)<86400 then '6h_to_24h' when ?-coalesce(created_at,?)<604800 then '1d_to_7d' else 'over_7d' end as key,count(*) count from queue_items where active=1 group by key order by count(*) desc""", (now,now,now,now,now,now,now,now)),
            "oldest": _rows(con, "select id,series_id,issue_id,state,current_source,retry_after,created_at,updated_at from queue_items where active=1 order by coalesce(updated_at,created_at,0) asc limit 20"),
        }
    return {
        "ok": True,
        "window": {"hours": hours, "cutoff": cutoff, "generated_at": now},
        "eligible": eligible,
        "claims": {
            "current": eligible["currently_claimed"],
            "durable_proxy_distinct_queue_items": attempts["distinct_queue_items"],
            "note": "queue_claims are leases and are deleted on release; source-attempt queue IDs are the durable claim/work proxy",
        },
        "source_attempts": attempts,
        "candidates": candidates,
        "downloads": downloads,
        "imports": imports,
        "completion": completion,
        "current_queue": current_queue,
        "query_elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def acquisition_funnel(db_path, hours=12, cache_seconds=300):
    path = Path(db_path)
    key = (str(path.resolve()), max(1, min(int(hours or 12), 168)))
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and now - cached[0] < max(0, int(cache_seconds or 0)):
            return {**cached[1], "cached": True, "cache_age_seconds": round(now - cached[0], 2)}
    result = build_acquisition_funnel(path, hours=key[1], now=now)
    with _CACHE_LOCK:
        _CACHE[key] = (now, result)
    return {**result, "cached": False, "cache_age_seconds": 0.0}


_MISSING_STATUSES = ("wanted", "in_progress")
_VISIBLE_STATUSES = {"library_visible", "visible", "confirmed"}
_TRANSFER_COMPLETE_STATES = {"import_ready", "importing", "verified"}

_RECOVERY_BUCKETS = (
    "never_searched",
    "due_for_search",
    "provider_planned",
    "provider_called",
    "provider_completed_with_results",
    "provider_completed_with_zero_results",
    "provider_timed_out",
    "provider_failed",
    "malformed_provider_response",
    "results_normalized",
    "all_candidates_rejected",
    "safe_candidate_available",
    "candidate_selected",
    "handoff_attempted",
    "handoff_acknowledged",
    "transfer_active",
    "transfer_stalled",
    "transfer_completed",
    "artifact_missing",
    "artifact_rejected",
    "artifact_quarantined",
    "ready_to_import",
    "imported",
    "reader_scan_pending",
    "reader_visible",
    "completion_recorded",
)


def _indexed(rows, key):
    indexed = {}
    for row in rows:
        if row.get(key) is not None:
            indexed.setdefault(row[key], row)
    return indexed


def _token(value, salt):
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()[:20]


def _age_bucket(created_at, now):
    age = max(0.0, now - float(created_at or now))
    if age < 86400:
        return "under_1d"
    if age < 604800:
        return "1d_to_7d"
    if age < 2592000:
        return "7d_to_30d"
    return "over_30d"


def _first_divergence(unit, queue, attempt, download, imported, media):
    has_queue = bool(queue)
    has_import = bool(imported and (imported["verified"] or imported["folder_imported"])) or bool(media)
    reader_visible = bool(imported and imported["reader_visible"])
    transfer_complete = bool(
        has_import
        or (download and download["transfer_complete"])
    )
    handed_off = bool(transfer_complete or (download and download["acknowledged_count"]))
    safely_accepted = bool(
        handed_off
        or (attempt and attempt["concrete_safe_candidate_count"])
    )
    candidate_returned = bool(
        safely_accepted
        or (attempt and attempt["candidate_detected_count"])
    )
    provider_called = bool(candidate_returned or (attempt and attempt["provider_called_count"]))
    if not has_queue:
        return "wanted_not_queued"
    if not provider_called:
        if attempt and attempt["deferred_count"]:
            return "provider_deferred_or_health_wait"
        return "queued_provider_not_called"
    if not candidate_returned:
        return "provider_no_candidate_evidence"
    if not safely_accepted:
        return "candidate_not_safely_accepted"
    if not handed_off:
        return "accepted_not_handed_off"
    if not transfer_complete:
        if download and download["active_acknowledged_count"]:
            return "handoff_active_transfer_pending"
        if download and download["retryable_failed_count"]:
            return "handoff_retryable_failed"
        if download and download["retired_stale_count"]:
            return "handoff_retired_or_stale"
        return "handoff_missing_watcher_reconciliation_evidence"
    if not has_import:
        return "transfer_complete_not_imported"
    if not reader_visible:
        return "imported_reader_visibility_unproven"
    return "reader_visible_wanted_stale"


def _primary_recovery_bucket(unit, queue, attempt, download, imported, media, now):
    """Return one and only one current recovery state for a missing wanted unit.

    Downstream durable evidence wins over upstream failures. This is important
    for partial provider success: a useful child-indexer result is retained even
    when a sibling timed out or failed.
    """
    attempt = attempt or {}
    download = download or {}
    imported = imported or {}
    has_import = bool(imported.get("imported_count")) or bool(media)
    visible = bool(imported.get("reader_visible"))
    completion = bool(imported.get("completion_recorded"))

    if visible and completion:
        return "completion_recorded"
    if visible:
        return "reader_visible"
    if has_import and imported.get("reader_scan_pending"):
        return "reader_scan_pending"
    if has_import:
        return "imported"
    if download.get("ready_to_import_count"):
        return "ready_to_import"
    if download.get("artifact_quarantined_count"):
        return "artifact_quarantined"
    if download.get("artifact_rejected_count"):
        return "artifact_rejected"
    if download.get("artifact_missing_count"):
        return "artifact_missing"
    if download.get("transfer_complete"):
        return "transfer_completed"
    if download.get("stalled_count"):
        return "transfer_stalled"
    if download.get("active_acknowledged_count"):
        return "transfer_active"
    if download.get("acknowledged_count"):
        return "handoff_acknowledged"
    if download.get("handoff_attempted_count"):
        return "handoff_attempted"
    if attempt.get("candidate_selected_count"):
        return "candidate_selected"
    if attempt.get("concrete_safe_candidate_count"):
        return "safe_candidate_available"
    if attempt.get("all_candidates_rejected"):
        return "all_candidates_rejected"
    if attempt.get("normalized_count"):
        return "results_normalized"
    if attempt.get("provider_result_count"):
        return "provider_completed_with_results"
    if attempt.get("malformed_count"):
        return "malformed_provider_response"
    if attempt.get("timeout_count"):
        return "provider_timed_out"
    if attempt.get("failure_count"):
        return "provider_failed"
    if attempt.get("zero_result_count"):
        return "provider_completed_with_zero_results"
    if attempt.get("provider_in_progress_count") or attempt.get("provider_called_count"):
        return "provider_called"
    if not attempt.get("attempt_count"):
        return "never_searched"
    if queue and (
        (queue.get("retry_after") is not None and float(queue["retry_after"]) <= now)
        or (queue.get("retry_after") is None and queue.get("state") in ("queued", "retry_later"))
    ):
        return "due_for_search"
    return "provider_planned"


def _stratified_cohort(units, target):
    if not units or int(target or 0) <= 0:
        return []
    target = max(1, min(int(target or 200), len(units)))
    groups = defaultdict(list)
    for unit in units:
        key = (
            unit["media_type"],
            unit["unit_type"],
            unit["first_divergence"],
            unit["pack_evidence"],
            unit["age_bucket"],
        )
        groups[key].append(unit)
    for rows in groups.values():
        rows.sort(key=lambda row: (row["created_at"], row["wanted_id"]))
    ordered = sorted(groups, key=lambda key: (-len(groups[key]), key))
    chosen = []
    offset = 0
    while len(chosen) < target:
        progressed = False
        for key in ordered:
            rows = groups[key]
            if offset < len(rows):
                chosen.append(rows[offset])
                progressed = True
                if len(chosen) >= target:
                    break
        if not progressed:
            break
        offset += 1
    return chosen


def _directive_cohort(units, target):
    """Build disjoint required strata, then fill any remaining target slots."""
    quotas = [
        ("western_collected_edition", 25, lambda u: u["media_type"] == "comic" and u["collection_evidence"] != "none"),
        ("authoritative_manga_volume", 25, lambda u: u["media_type"] == "manga" and u["identity_unit_type"] == "volume"),
        ("manga_chapter", 55, lambda u: u["media_type"] == "manga" and u["identity_unit_type"] == "chapter"),
        ("difficult_identity", 10, lambda u: u["difficult_identity"] != "none"),
        ("pack_related", 25, lambda u: u["pack_evidence"] == "pack"),
        ("western_single", 60, lambda u: u["media_type"] == "comic" and u["unit_type"] == "issue" and u["collection_evidence"] == "none"),
    ]
    chosen = []
    used = set()
    shortfalls = {}
    for label, quota, predicate in quotas:
        pool = [u for u in units if u["wanted_id"] not in used and predicate(u)]
        if label == "western_collected_edition":
            authoritative = [u for u in pool if u["collection_evidence"] == "authoritative_identity"]
            labelled = [u for u in pool if u["collection_evidence"] != "authoritative_identity"]
            pool = _stratified_cohort(authoritative, min(quota, len(authoritative)))
            pool += _stratified_cohort(labelled, max(0, quota - len(pool)))
        else:
            pool = _stratified_cohort(pool, min(quota, len(pool))) if pool else []
        if len(pool) < quota:
            shortfalls[label] = {"required": quota, "available_disjoint": len(pool)}
        for unit in pool:
            unit["cohort_stratum"] = label
            used.add(unit["wanted_id"])
            chosen.append(unit)
    remaining = [u for u in units if u["wanted_id"] not in used]
    for unit in _stratified_cohort(remaining, max(0, int(target) - len(chosen))):
        unit["cohort_stratum"] = "additional_stratified"
        chosen.append(unit)
    return chosen, shortfalls


def _mixed_recovery_cohort(units, target=50):
    """Select a bounded, disjoint recovery cohort across the whole pipeline."""
    strata = (
        ("search_coverage", {"never_searched", "due_for_search", "provider_planned", "provider_called"}),
        ("provider_outcome", {
            "provider_completed_with_results", "provider_completed_with_zero_results",
            "provider_timed_out", "provider_failed", "malformed_provider_response",
        }),
        ("candidate_decision", {
            "results_normalized", "all_candidates_rejected", "safe_candidate_available", "candidate_selected",
        }),
        ("handoff_transfer_artifact", {
            "handoff_attempted", "handoff_acknowledged", "transfer_active", "transfer_stalled",
            "transfer_completed", "artifact_missing", "artifact_rejected", "artifact_quarantined",
        }),
        ("import_reader", {
            "ready_to_import", "imported", "reader_scan_pending", "reader_visible", "completion_recorded",
        }),
    )
    target = max(1, min(int(target or 50), len(units))) if units else 0
    quota = max(1, target // len(strata)) if target else 0
    selected = []
    used = set()
    shortfalls = {}
    for label, buckets in strata:
        pool = [unit for unit in units if unit["wanted_id"] not in used and unit["primary_loss_bucket"] in buckets]
        # Prefer distinct series within each stratum before taking another unit
        # from the same title; this avoids one deep backlog monopolizing a pass.
        pool.sort(key=lambda row: (row["created_at"], row["wanted_id"]))
        distinct = []
        repeated = []
        seen_series = set()
        for unit in pool:
            if unit["series_id"] in seen_series:
                repeated.append(unit)
            else:
                seen_series.add(unit["series_id"])
                distinct.append(unit)
        chosen = (distinct + repeated)[:quota]
        if len(chosen) < quota:
            shortfalls[label] = {"target": quota, "available_disjoint": len(chosen)}
        for unit in chosen:
            unit["recovery_cohort_stratum"] = label
            used.add(unit["wanted_id"])
            selected.append(unit)
    remaining = [unit for unit in units if unit["wanted_id"] not in used]
    for unit in _stratified_cohort(remaining, max(0, target - len(selected))):
        unit["recovery_cohort_stratum"] = "balanced_fill"
        selected.append(unit)
    return selected, shortfalls


def build_missing_backlog_accounting(
    db_path, cohort_size=200, recovery_cohort_size=50, now=None, token_salt="private-audit"
):
    """Classify the current monitored missing backlog from durable evidence only.

    This function opens SQLite in read-only/query-only mode. It never imports
    application code, invokes providers, enqueues work, or reads configuration
    and secret tables.
    """
    path = Path(db_path)
    now = time.time() if now is None else float(now)
    if not path.exists():
        return {"ok": False, "error": "InkDrop state database not found"}
    started = time.perf_counter()
    snapshot_stat = path.stat()
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5.0) as con:
        con.row_factory = sqlite3.Row
        con.create_function(
            "inkdrop_concrete_safe_candidate", 8, _concrete_safe_candidate_count
        )
        con.execute("pragma query_only=1")
        con.execute("pragma busy_timeout=3000")
        con.execute("begin")
        canonicalization = dict(
            con.execute(
                """with eligible as (
                     select w.*,
                            (select q.state from queue_items q where q.wanted_id=w.id order by q.updated_at desc,q.id desc limit 1) latest_queue_state,
                            (select json_extract(q.raw_json,'$.superseded_by_queue_id') from queue_items q where q.wanted_id=w.id order by q.updated_at desc,q.id desc limit 1) superseded_by_queue_id
                     from wanted_items w join series s on s.id=w.series_id and s.monitored=1
                     join issues i on i.id=w.issue_id and i.monitored=1
                     where w.status in ('wanted','in_progress')
                   )
                   select (select count(*) from eligible) eligible_wanted_rows,
                          (select count(*) from eligible e where not (
                            e.latest_queue_state='superseded_duplicate' and
                            exists(select 1 from queue_items target where target.id=e.superseded_by_queue_id)
                          )) canonical_unique_units,
                          (select count(*) from eligible e where e.latest_queue_state='superseded_duplicate' and
                            exists(select 1 from queue_items target where target.id=e.superseded_by_queue_id)
                          ) duplicate_alias_rows_excluded,
                          (select count(*) from eligible e where e.latest_queue_state='superseded_duplicate' and
                            exists(select 1 from queue_items target where target.id=e.superseded_by_queue_id)
                          ) excluded_rows_latest_superseded_duplicate,
                          (select count(*) from eligible e where not (
                              e.latest_queue_state='superseded_duplicate' and
                              exists(select 1 from queue_items target where target.id=e.superseded_by_queue_id)
                            ) and not exists(select 1 from queue_items active_queue where active_queue.wanted_id=e.id and active_queue.active=1)
                          ) canonical_units_without_active_queue,
                          (select count(*) from eligible e where e.latest_queue_state='superseded_duplicate' and
                            not exists(select 1 from queue_items target where target.id=e.superseded_by_queue_id)
                          ) unresolved_superseded_without_canonical"""
            ).fetchone()
        )
        units = _rows(
            con,
            """with eligible as (
                 select w.*,
                        (select q.state from queue_items q where q.wanted_id=w.id order by q.updated_at desc,q.id desc limit 1) latest_queue_state,
                        (select json_extract(q.raw_json,'$.superseded_by_queue_id') from queue_items q where q.wanted_id=w.id order by q.updated_at desc,q.id desc limit 1) superseded_by_queue_id
                 from wanted_items w join series ms on ms.id=w.series_id and ms.monitored=1
                 join issues mi on mi.id=w.issue_id and mi.monitored=1
                 where w.status in ('wanted','in_progress')
               )
               select w.id wanted_id,w.series_id,w.issue_id,w.status wanted_status,
                      w.reason,w.created_at,w.updated_at,s.media_type,s.title series_title,
                      i.issue_number,i.title unit_title,
                      iu.unit_type identity_unit_type,
                      coalesce(iu.unit_type,
                        nullif(json_extract(w.raw_json,'$.unitType'),''),
                        case when s.media_type='comic' then 'issue' else 'unknown' end
                      ) unit_type,
                      case
                        when s.media_type='comic' and iu.unit_type='volume' then 'authoritative_identity'
                        when s.media_type='comic' and (
                          lower(coalesce(i.title,'')) like '%volume%'
                          or lower(coalesce(i.title,'')) glob '*vol. [0-9]*'
                          or lower(coalesce(i.title,'')) glob '*vol [0-9]*'
                          or lower(coalesce(i.title,'')) like '%tpb%'
                          or lower(coalesce(i.title,'')) like '%trade paperback%'
                          or lower(coalesce(i.title,'')) like '%omnibus%'
                          or lower(coalesce(i.title,'')) like '%collection%'
                          or lower(coalesce(i.title,'')) like '%collected%'
                          or lower(coalesce(i.title,'')) like '%graphic novel%'
                        ) then 'provider_metadata_label'
                        else 'none'
                      end collection_evidence,
                      case when iu.id is null and s.media_type='manga' then 'unit_type_unclassified'
                           else 'none' end difficult_identity,
                      case when json_extract(w.raw_json,'$.pack_fanout') is not null
                                  or json_extract(w.raw_json,'$.partial_pack_file_ready') is not null
                           then 1 else 0 end wanted_pack_evidence
               from eligible w
               join series s on s.id=w.series_id and s.monitored=1
               join issues i on i.id=w.issue_id and i.monitored=1
               left join identity_units iu on iu.legacy_issue_id=w.issue_id
               where not (w.latest_queue_state='superseded_duplicate' and
                 exists(select 1 from queue_items target where target.id=w.superseded_by_queue_id))
               order by w.created_at,w.id""",
        )
        queues = _indexed(
            _rows(
                con,
                """select q.wanted_id,q.id queue_id,q.state,q.retry_after,q.created_at,q.updated_at,
                          coalesce(nullif(json_extract(q.raw_json,'$.unitType'),''),'') queue_unit_type
                   from queue_items q
                   join wanted_items w on w.id=q.wanted_id and w.status in ('wanted','in_progress')
                   join series s on s.id=w.series_id and s.monitored=1
                   where q.active=1
                   order by q.updated_at desc""",
            ),
            "wanted_id",
        )
        attempts = _indexed(
            _rows(
                con,
                """with raw_attempts as (
                     select q.wanted_id,sa.*,
                            max(
                              coalesce(cast(json_extract(sa.raw_json,'$.last_slskd_detected_count') as integer),0),
                              coalesce(cast(json_extract(sa.raw_json,'$.last_slskd_candidate_count') as integer),0),
                              coalesce(cast(json_extract(sa.raw_json,'$.detected_count') as integer),0),
                              coalesce(cast(json_extract(sa.raw_json,'$.candidate_count') as integer),0),
                              coalesce(cast(json_extract(sa.raw_json,'$.result_count') as integer),0),
                              coalesce(cast(json_extract(sa.raw_json,'$.results_count') as integer),0)
                            ) result_count,
                            max(
                              coalesce(cast(json_extract(sa.raw_json,'$.last_slskd_auto_grab_safe_count') as integer),0),
                              coalesce(cast(json_extract(sa.raw_json,'$.auto_grab_safe_count') as integer),0),
                              coalesce(cast(json_extract(sa.raw_json,'$.safe_candidate_count') as integer),0),
                              coalesce(cast(json_extract(sa.raw_json,'$.pack_auto_approved') as integer),0)
                            ) safe_count
                     from queue_items q join source_attempts sa on sa.queue_id=q.id
                     join wanted_items w on w.id=q.wanted_id and w.status in ('wanted','in_progress')
                     join series s on s.id=w.series_id and s.monitored=1
                     where q.active=1
                       and coalesce(sa.source,'')<>'queue_activity'
                       and coalesce(sa.outcome,'')<>'historical'
                       and coalesce(sa.status,'')<>'coalesced_retry_duplicate'
                   ), ranked as (
                     select raw_attempts.*,
                            row_number() over (
                              partition by wanted_id,coalesce(nullif(provider_id,''),nullif(provider,''),nullif(source,''),'unknown')
                              order by coalesce(completed_at,started_at,0) desc,id desc
                            ) evidence_rank
                     from raw_attempts
                   ), relevant as (
                     select * from ranked where evidence_rank=1
                   )
                   select relevant.wanted_id,count(relevant.id) attempt_count,
                          sum(case when status not in ('provider_wait','provider_unavailable','health_backoff','retry_scheduled','retry_pending','retry_cooling_down')
                                    and lifecycle_phase not in ('provider_wait','observed')
                                    and started_at is not null then 1 else 0 end) provider_called_count,
                          sum(case when status in ('searching','available') or lifecycle_phase='searching'
                                    then 1 else 0 end) provider_in_progress_count,
                          max(result_count) candidate_detected_count,
                          max(safe_count) safe_candidate_count,
                          max(inkdrop_concrete_safe_candidate(
                            safe_count,candidate_identity,download_url_hash,download_client,raw_json,
                            exists(select 1 from download_tasks linked
                              where linked.source_attempt_id=relevant.id and (
                                nullif(trim(linked.external_id),'') is not null or
                                nullif(trim(linked.candidate_identity),'') is not null
                              )),status,coalesce(source,provider_id,provider,'')
                          )) concrete_safe_candidate_count,
                          sum(case when result_count>0 then 1 else 0 end) provider_result_count,
                          sum(case when status='searched_no_candidates' or lifecycle_phase='searched_no_candidates'
                                    then 1 else 0 end) zero_result_count,
                          sum(case when status='timeout' or lower(coalesce(failure_reason,'')) like '%timed out%'
                                    or lower(coalesce(failure_reason,'')) like '%timeout%' then 1 else 0 end) timeout_count,
                          sum(case when lower(coalesce(failure_reason,'')) like '%malformed%'
                                    or lower(coalesce(failure_reason,'')) like '%invalid payload%'
                                    or lower(coalesce(failure_reason,'')) like '%no payload%'
                                    or lower(coalesce(status,'')) in ('malformed_response','invalid_response')
                                   then 1 else 0 end) malformed_count,
                          sum(case when (status in ('error','failed') or lifecycle_phase='failed_candidate' or outcome='problem')
                                    and status<>'blocked'
                                    and lower(coalesce(failure_reason,'')) not like '%timed out%'
                                    and lower(coalesce(failure_reason,'')) not like '%timeout%'
                                    and lower(coalesce(failure_reason,'')) not like '%malformed%'
                                    and lower(coalesce(failure_reason,'')) not like '%invalid payload%'
                                    and lower(coalesce(failure_reason,'')) not like '%no payload%'
                                   then 1 else 0 end) failure_count,
                          sum(case when result_count>0
                                    or (nullif(trim(candidate_identity),'') is not null
                                        and nullif(trim(title),'') is not null
                                        and status in ('available','accepted','sent','downloading','download_started','blocked','review','rejected','wrong_unit_quarantined'))
                                   then 1 else 0 end) normalized_count,
                          sum(case when (status in ('blocked','review','rejected','wrong_unit_quarantined')
                                         or lifecycle_phase in ('failed_candidate','manual_review'))
                                    and (nullif(trim(candidate_identity),'') is not null or result_count>0)
                                   then 1 else 0 end) rejected_candidate_count,
                          case when max(inkdrop_concrete_safe_candidate(
                                       safe_count,candidate_identity,download_url_hash,download_client,raw_json,
                                       exists(select 1 from download_tasks linked
                                         where linked.source_attempt_id=relevant.id and (
                                           nullif(trim(linked.external_id),'') is not null or
                                           nullif(trim(linked.candidate_identity),'') is not null
                                         )),status,coalesce(source,provider_id,provider,'')))=0
                                     and sum(case when (status in ('blocked','review','rejected','wrong_unit_quarantined')
                                                       or lifecycle_phase in ('failed_candidate','manual_review'))
                                                  and (nullif(trim(candidate_identity),'') is not null or result_count>0)
                                                  then 1 else 0 end)>0
                               then 1 else 0 end all_candidates_rejected,
                          sum(case when (
                                      status in ('sent','downloading','download_started','started_waiting','transfer_in_progress','staged_file_ready','importing','verified')
                                      or lifecycle_phase in ('downloading','staged_or_importing','verified')
                                    ) and (
                                      nullif(trim(candidate_identity),'') is not null
                                      or exists(select 1 from download_tasks linked
                                        where linked.source_attempt_id=relevant.id
                                          and nullif(trim(linked.external_id),'') is not null)
                                    )
                                   then 1 else 0 end) candidate_selected_count,
                          sum(case when lifecycle_phase in ('provider_wait','retry_later','observed','searching')
                                    or status in ('provider_wait','provider_unavailable','health_backoff','retry_scheduled','retry_pending','retry_cooling_down')
                                   then 1 else 0 end) deferred_count,
                          sum(case when outcome in ('no_candidate','problem','retry_later') then 1 else 0 end) nonproductive_count,
                          count(distinct coalesce(nullif(provider_id,''),nullif(provider,''),nullif(source,''))) provider_count,
                          max(coalesce(completed_at,started_at,0)) latest_attempt_at
                   from relevant
                   group by relevant.wanted_id""",
            ),
            "wanted_id",
        )
        downloads = _indexed(
            _rows(
                con,
                """select q.wanted_id,count(dt.id) task_count,
                          sum(case when coalesce(dt.status,'') not in ('provider_wait','provider_unavailable','stale_provider_wait_retired')
                                    and (nullif(trim(dt.external_id),'') is not null
                                         or (dt.source_attempt_id is not null and dt.status in
                                           ('sent','download_started','started_waiting','downloading','transfer_in_progress','transfer_settling','completed_in_client','staged_file_ready','ready_to_import')))
                                   then 1 else 0 end) handoff_attempted_count,
                          sum(case when nullif(trim(dt.external_id),'') is not null then 1 else 0 end) acknowledged_count,
                          sum(case when nullif(trim(dt.external_id),'') is not null
                                    and dt.state in ('queued','downloading')
                                    and coalesce(dt.lifecycle_phase,'') in ('downloading','active','handoff','')
                                    and coalesce(dt.status,'') not in ('provider_wait','provider_unavailable')
                                   then 1 else 0 end) active_acknowledged_count,
                          sum(case when nullif(trim(dt.external_id),'') is not null and dt.state='failed' and dt.retry_eligible=1
                                   then 1 else 0 end) retryable_failed_count,
                          sum(case when nullif(trim(dt.external_id),'') is not null and (
                                      dt.state='retired' or dt.status like '%stale%' or dt.status like '%superseded%'
                                      or dt.outcome like '%stale%' or dt.lifecycle_phase='retired'
                                    ) then 1 else 0 end) retired_stale_count,
                          sum(case when (nullif(trim(dt.external_id),'') is not null
                                         or nullif(trim(dt.local_path),'') is not null)
                                    and (dt.state in ('import_ready','importing','verified')
                                    or dt.status in ('completed','completed_in_client','transfer_complete','transfer_succeeded_missing_stage')
                                    ) then 1 else 0 end) transfer_complete,
                          sum(case when nullif(trim(dt.external_id),'') is not null
                                    and (dt.status like '%stalled%' or dt.state='stalled'
                                         or dt.failure_reason like '%stalled%') then 1 else 0 end) stalled_count,
                          sum(case when (nullif(trim(dt.local_path),'') is not null or nullif(trim(dt.save_path),'') is not null)
                                    and (dt.status in ('staged_file_ready','ready_to_import')
                                         or dt.state='import_ready') then 1 else 0 end) ready_to_import_count,
                          sum(case when (nullif(trim(dt.local_path),'') is not null or dt.completed_at is not null)
                                    and (dt.status like '%quarantin%' or dt.state='quarantined'
                                         or dt.failure_reason like '%quarantin%') then 1 else 0 end) artifact_quarantined_count,
                          sum(case when (nullif(trim(dt.local_path),'') is not null or dt.completed_at is not null)
                                    and (dt.status in ('bad_archive','invalid_archive','preview','sample','wrong_unit','wrong_series_or_subseries','supplemental_cover_only','preview_not_importable')
                                    or dt.failure_reason like '%bad_archive%'
                                    or dt.failure_reason like '%invalid_archive%'
                                    or dt.failure_reason like '%preview%'
                                    or dt.failure_reason like '%sample%'
                                    or dt.failure_reason like '%wrong_unit%')
                                   then 1 else 0 end) artifact_rejected_count,
                          sum(case when nullif(trim(dt.external_id),'') is not null
                                    and (dt.status in ('stale_no_local_file','transfer_succeeded_missing_stage','completed_client_path_missing_archive','missing_file')
                                    or dt.failure_reason like '%missing_file%'
                                    or dt.failure_reason like '%missing_archive%'
                                    or dt.failure_reason like '%path_missing%')
                                   then 1 else 0 end) artifact_missing_count,
                          sum(case when dt.completed_at is not null then 1 else 0 end) completed_at_count,
                          max(coalesce(cast(json_extract(dt.raw_json,'$.pack_auto_approved') as integer),0)) pack_evidence,
                          max(coalesce(dt.updated_at,dt.started_at,0)) latest_download_at
                   from queue_items q join download_tasks dt on dt.queue_id=q.id
                   join wanted_items w on w.id=q.wanted_id and w.status in ('wanted','in_progress')
                   join series s on s.id=w.series_id and s.monitored=1
                   where q.active=1 group by q.wanted_id""",
            ),
            "wanted_id",
        )
        imports = _indexed(
            _rows(
                con,
                """select q.wanted_id,count(ir.id) import_result_count,
                           sum(case when ir.verified=1 or ir.folder_imported=1 then 1 else 0 end) import_count,
                           max(ir.verified) verified,
                           max(ir.folder_imported) folder_imported,
                           max(case when ir.library_visibility_status in ('library_visible','visible','confirmed') then 1 else 0 end) reader_visible,
                           max(case when (ir.verified=1 or ir.folder_imported=1)
                                     and ir.library_visibility_status='pending' then 1 else 0 end) reader_scan_pending,
                           max(case when ir.completion_truth='library'
                                     and ir.library_visibility_status in ('library_visible','visible','confirmed')
                                    then 1 else 0 end) completion_recorded,
                           max(ir.created_at) latest_import_at
                   from queue_items q join import_results ir on ir.queue_id=q.id
                   join wanted_items w on w.id=q.wanted_id and w.status in ('wanted','in_progress')
                   join series s on s.id=w.series_id and s.monitored=1
                   where q.active=1 group by q.wanted_id""",
            ),
            "wanted_id",
        )
        media = _indexed(
            _rows(
                con,
                """select w.id wanted_id,count(mf.id) media_count
                   from wanted_items w join series s on s.id=w.series_id and s.monitored=1
                   join media_files mf on mf.series_id=w.series_id and mf.issue_id=w.issue_id
                   where w.status in ('wanted','in_progress') and mf.active=1 and mf.status='present'
                   group by w.id""",
            ),
            "wanted_id",
        )
        manual_oracle = _indexed(
            _rows(
                con,
                """select w.id wanted_id,count(distinct mc.id) accepted_automatic_candidates,
                          max(case when mc.confidence_tier='high' then 1 else 0 end) high_confidence
                   from wanted_items w join series s on s.id=w.series_id and s.monitored=1
                   join issues i on i.id=w.issue_id and i.monitored=1
                   left join identity_units iu on iu.legacy_issue_id=w.issue_id
                   join manual_search_runs mr on mr.issue_id=w.issue_id or (mr.unit_id is not null and mr.unit_id=iu.id)
                   join manual_search_candidates mc on mc.run_id=mr.id
                   where w.status in ('wanted','in_progress') and mc.accepted=1
                     and mc.acquisition_capability='automatic'
                   group by w.id""",
            ),
            "wanted_id",
        )
        manual_oracle_global = dict(
            con.execute(
                """select count(*) accepted_automatic_candidates,
                          sum(case when confidence_tier='high' then 1 else 0 end) high_confidence_candidates
                   from manual_search_candidates
                   where accepted=1 and acquisition_capability='automatic'"""
            ).fetchone()
        )
    for unit in units:
        queue = queues.get(unit["wanted_id"])
        if unit["unit_type"] == "unknown" and queue and queue["queue_unit_type"]:
            unit["unit_type"] = queue["queue_unit_type"]
        attempt = attempts.get(unit["wanted_id"])
        download = downloads.get(unit["wanted_id"])
        imported = imports.get(unit["wanted_id"])
        unit["first_divergence"] = _first_divergence(
            unit, queue, attempt, download, imported, media.get(unit["wanted_id"])
        )
        unit["primary_loss_bucket"] = _primary_recovery_bucket(
            unit, queue, attempt, download, imported, media.get(unit["wanted_id"]), now
        )
        unit["pack_evidence"] = "pack" if unit["wanted_pack_evidence"] or (download and download["pack_evidence"]) else "single_or_unknown"
        unit["age_bucket"] = _age_bucket(unit["created_at"], now)
        unit["queue_id"] = queue["queue_id"] if queue else None
        unit["attempt_count"] = int((attempt or {}).get("attempt_count") or 0)
        unit["provider_called_count"] = int((attempt or {}).get("provider_called_count") or 0)
        unit["provider_in_progress_count"] = int((attempt or {}).get("provider_in_progress_count") or 0)
        unit["provider_result_count"] = int((attempt or {}).get("provider_result_count") or 0)
        unit["zero_result_count"] = int((attempt or {}).get("zero_result_count") or 0)
        unit["timeout_count"] = int((attempt or {}).get("timeout_count") or 0)
        unit["failure_count"] = int((attempt or {}).get("failure_count") or 0)
        unit["malformed_count"] = int((attempt or {}).get("malformed_count") or 0)
        unit["normalized_count"] = int((attempt or {}).get("normalized_count") or 0)
        unit["rejected_candidate_count"] = int((attempt or {}).get("rejected_candidate_count") or 0)
        unit["candidate_selected_count"] = int((attempt or {}).get("candidate_selected_count") or 0)
        unit["candidate_detected_count"] = int((attempt or {}).get("candidate_detected_count") or 0)
        unit["safe_candidate_count"] = int((attempt or {}).get("safe_candidate_count") or 0)
        unit["concrete_safe_candidate_count"] = int((attempt or {}).get("concrete_safe_candidate_count") or 0)
        unit["provider_count"] = int((attempt or {}).get("provider_count") or 0)
        unit["download_task_count"] = int((download or {}).get("task_count") or 0)
        unit["handoff_attempted_count"] = int((download or {}).get("handoff_attempted_count") or 0)
        unit["acknowledged_count"] = int((download or {}).get("acknowledged_count") or 0)
        unit["active_acknowledged_count"] = int((download or {}).get("active_acknowledged_count") or 0)
        unit["retryable_failed_count"] = int((download or {}).get("retryable_failed_count") or 0)
        unit["retired_stale_count"] = int((download or {}).get("retired_stale_count") or 0)
        unit["import_count"] = int((imported or {}).get("import_count") or 0)
        unit["manual_oracle_candidates"] = int((manual_oracle.get(unit["wanted_id"]) or {}).get("accepted_automatic_candidates") or 0)
        unit["manual_oracle_high_confidence"] = int((manual_oracle.get(unit["wanted_id"]) or {}).get("high_confidence") or 0)
    cohort, cohort_shortfalls = _directive_cohort(units, cohort_size)
    recovery_cohort, recovery_cohort_shortfalls = _mixed_recovery_cohort(
        units, recovery_cohort_size
    )
    stages = [
        "monitored_missing",
        "wanted",
        "scheduled",
        "provider_called",
        "candidate_returned",
        "safely_accepted",
        "automatic_handoff",
        "transfer_completed",
        "imported",
        "reader_visible",
    ]
    divergence_stage = {
        "wanted_not_queued": 2,
        "provider_deferred_or_health_wait": 3,
        "queued_provider_not_called": 3,
        "provider_no_candidate_evidence": 4,
        "candidate_not_safely_accepted": 5,
        "accepted_not_handed_off": 6,
        "handoff_active_transfer_pending": 7,
        "handoff_retryable_failed": 7,
        "handoff_retired_or_stale": 7,
        "handoff_missing_watcher_reconciliation_evidence": 7,
        "transfer_complete_not_imported": 8,
        "imported_reader_visibility_unproven": 9,
        "reader_visible_wanted_stale": 10,
    }
    funnel = {}
    for index, stage in enumerate(stages):
        funnel[stage] = sum(divergence_stage[u["first_divergence"]] > index for u in units)
    def counts(key, rows=units):
        return dict(sorted(Counter(row[key] for row in rows).items(), key=lambda pair: (-pair[1], pair[0])))
    primary_loss_buckets = Counter(unit["primary_loss_bucket"] for unit in units)
    primary_loss_buckets = {bucket: int(primary_loss_buckets.get(bucket, 0)) for bucket in _RECOVERY_BUCKETS}
    known_available = [unit for unit in units if unit["manual_oracle_candidates"]]
    known_funnel = {}
    for index, stage in enumerate(stages):
        known_funnel[stage] = sum(divergence_stage[u["first_divergence"]] > index for u in known_available)
    aggregate = {
        "ok": True,
        "snapshot": {
            "generated_at": now,
            "generated_at_iso": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
            "database_bytes": snapshot_stat.st_size,
            "database_mtime": snapshot_stat.st_mtime,
            "database_mtime_iso": dt.datetime.fromtimestamp(
                snapshot_stat.st_mtime, dt.timezone.utc
            ).isoformat(),
            "query_mode": "sqlite_uri_mode_ro_plus_query_only",
        },
        "scope": {
            "definition": "monitored series + monitored issue + wanted status wanted|in_progress, excluding a superseded_duplicate alias only when its durable superseded_by_queue_id target exists",
            "complete_missing_backlog": len(units),
            "cohort_target": int(cohort_size),
            "cohort_size": len(cohort),
        },
        "canonicalization": {key: int(value or 0) for key, value in canonicalization.items()},
        "funnel": funnel,
        "primary_loss_buckets": primary_loss_buckets,
        "primary_loss_bucket_total": sum(primary_loss_buckets.values()),
        "first_divergence": counts("first_divergence"),
        "by_media_type": counts("media_type"),
        "by_unit_type": counts("unit_type"),
        "by_wanted_status": counts("wanted_status"),
        "by_age_bucket": counts("age_bucket"),
        "pack_evidence": counts("pack_evidence"),
        "cohort": {
            "by_required_stratum": counts("cohort_stratum", cohort),
            "by_collection_evidence": counts("collection_evidence", cohort),
            "by_difficult_identity": counts("difficult_identity", cohort),
            "by_media_type": counts("media_type", cohort),
            "by_unit_type": counts("unit_type", cohort),
            "by_first_divergence": counts("first_divergence", cohort),
            "by_primary_loss_bucket": counts("primary_loss_bucket", cohort),
            "by_age_bucket": counts("age_bucket", cohort),
            "pack_evidence": counts("pack_evidence", cohort),
            "shortfalls": cohort_shortfalls,
        },
        "recovery_cohort": {
            "target": int(recovery_cohort_size),
            "size": len(recovery_cohort),
            "by_stratum": counts("recovery_cohort_stratum", recovery_cohort),
            "by_primary_loss_bucket": counts("primary_loss_bucket", recovery_cohort),
            "by_media_type": counts("media_type", recovery_cohort),
            "shortfalls": recovery_cohort_shortfalls,
        },
        "known_available_manual_oracle": {
            "definition": "current missing units with an exact issue/unit Manual Search candidate accepted for automatic acquisition",
            "global_accepted_candidate_rows": int(manual_oracle_global.get("accepted_automatic_candidates") or 0),
            "global_high_confidence_candidate_rows": int(manual_oracle_global.get("high_confidence_candidates") or 0),
            "unit_count": len(known_available),
            "accepted_candidate_rows": sum(unit["manual_oracle_candidates"] for unit in known_available),
            "high_confidence_units": sum(bool(unit["manual_oracle_high_confidence"]) for unit in known_available),
            "automatic_funnel": known_funnel,
            "automatic_first_divergence": counts("first_divergence", known_available),
        },
        "limitations": [
            "candidate_returned means an explicit durable candidate/result count; candidate_identity and productive outcomes are not result proof",
            "safely_accepted requires concrete candidate identity, a replay-capable locator plus client, or linked task evidence; availability counters alone are telemetry",
            "provider_called excludes provider_wait, health backoff, and other deferred-only observations",
            "automatic_handoff requires a client external_id or downstream import evidence; task_count alone is not handoff proof",
            "reader visibility uses durable import visibility status only; no reader API call or scan was performed",
            "primary_loss_buckets are mutually exclusive and their total must equal complete_missing_backlog",
            "partial provider results take precedence over sibling timeout/failure evidence",
            "unknown unit type is retained when durable identity and queue evidence do not classify it",
            "pack evidence is counted only from durable wanted/download pack fields",
        ],
        "query_elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    private = []
    for unit in cohort:
        private.append(
            {
                "unit_token": _token(unit["wanted_id"], token_salt),
                "wanted_id": unit["wanted_id"],
                "series_id": unit["series_id"],
                "issue_id": unit["issue_id"],
                "series_title": unit["series_title"],
                "unit_title": unit["unit_title"],
                "issue_number": unit["issue_number"],
                "media_type": unit["media_type"],
                "unit_type": unit["unit_type"],
                "wanted_status": unit["wanted_status"],
                "reason": unit["reason"],
                "age_bucket": unit["age_bucket"],
                "pack_evidence": unit["pack_evidence"],
                "cohort_stratum": unit["cohort_stratum"],
                "collection_evidence": unit["collection_evidence"],
                "difficult_identity": unit["difficult_identity"],
                "first_divergence": unit["first_divergence"],
                "primary_loss_bucket": unit["primary_loss_bucket"],
                "queue_id": unit["queue_id"],
                "attempt_count": unit["attempt_count"],
                "provider_called_count": unit["provider_called_count"],
                "provider_in_progress_count": unit["provider_in_progress_count"],
                "provider_result_count": unit["provider_result_count"],
                "zero_result_count": unit["zero_result_count"],
                "timeout_count": unit["timeout_count"],
                "failure_count": unit["failure_count"],
                "malformed_count": unit["malformed_count"],
                "normalized_count": unit["normalized_count"],
                "rejected_candidate_count": unit["rejected_candidate_count"],
                "candidate_selected_count": unit["candidate_selected_count"],
                "candidate_detected_count": unit["candidate_detected_count"],
                "safe_candidate_count": unit["safe_candidate_count"],
                "concrete_safe_candidate_count": unit["concrete_safe_candidate_count"],
                "provider_count": unit["provider_count"],
                "download_task_count": unit["download_task_count"],
                "handoff_attempted_count": unit["handoff_attempted_count"],
                "acknowledged_count": unit["acknowledged_count"],
                "active_acknowledged_count": unit["active_acknowledged_count"],
                "retryable_failed_count": unit["retryable_failed_count"],
                "retired_stale_count": unit["retired_stale_count"],
                "import_count": unit["import_count"],
                "manual_oracle_candidates": unit["manual_oracle_candidates"],
                "manual_oracle_high_confidence": unit["manual_oracle_high_confidence"],
            }
        )
    private_recovery = []
    for unit in recovery_cohort:
        private_recovery.append(
            {
                "unit_token": _token(unit["wanted_id"], token_salt),
                "wanted_id": unit["wanted_id"],
                "series_id": unit["series_id"],
                "issue_id": unit["issue_id"],
                "series_title": unit["series_title"],
                "unit_title": unit["unit_title"],
                "issue_number": unit["issue_number"],
                "media_type": unit["media_type"],
                "unit_type": unit["unit_type"],
                "primary_loss_bucket": unit["primary_loss_bucket"],
                "recovery_cohort_stratum": unit["recovery_cohort_stratum"],
                "queue_id": unit["queue_id"],
            }
        )
    return {
        "aggregate": aggregate,
        "private_cohort": private,
        "private_recovery_cohort": private_recovery,
    }


def _inside_git_worktree(path):
    current = path.resolve().parent
    for parent in (current, *current.parents):
        if (parent / ".git").exists():
            return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only InkDrop acquisition recall accounting")
    parser.add_argument("db_path")
    parser.add_argument("--cohort-size", type=int, default=200)
    parser.add_argument("--recovery-cohort-size", type=int, default=50)
    parser.add_argument("--private-output", type=Path)
    parser.add_argument(
        "--private-json-stdout",
        action="store_true",
        help="emit aggregate plus private cohort for a protected caller to capture",
    )
    parser.add_argument("--token-salt", default="private-audit")
    args = parser.parse_args(argv)
    report = build_missing_backlog_accounting(
        args.db_path,
        cohort_size=args.cohort_size,
        recovery_cohort_size=args.recovery_cohort_size,
        token_salt=args.token_salt,
    )
    if not report.get("aggregate"):
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2
    if args.private_output:
        if _inside_git_worktree(args.private_output):
            parser.error("--private-output must be outside a Git worktree")
        args.private_output.parent.mkdir(parents=True, exist_ok=True)
        args.private_output.write_text(
            json.dumps(
                {
                    "schema": "inkdrop.acquisition_recall.private_cohort.v2",
                    "aggregate": report["aggregate"],
                    "cohort": report["private_cohort"],
                    "recovery_cohort": report["private_recovery_cohort"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    output = report if args.private_json_stdout else report["aggregate"]
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
