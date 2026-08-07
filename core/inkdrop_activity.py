#!/usr/bin/env python3
"""Bounded, durable projection of InkDrop's current work.

The projection deliberately reads only SQLite evidence. Download-client polling is
performed by workers and persisted in ``download_tasks.raw_json``; HTTP reads must
never turn into client network calls or hold a write transaction.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from core import inkdrop_staged_projection


SUPPORTED_SORTS = {
    "last_updated",
    "stage",
    "client",
    "progress",
    "eta",
    "age",
    "series_title",
    "next_retry",
}
SUPPORTED_DIRECTIONS = {"asc", "desc"}
SUPPORTED_FACETS = {"stage", "client", "source", "provider", "needs_user", "stalled"}
CLIENT_ALIASES = {
    "qbittorrent": "qbittorrent",
    "qbit": "qbittorrent",
    "qb": "qbittorrent",
    "sabnzbd": "sabnzbd",
    "sab": "sabnzbd",
    "slskd": "slskd",
    "soulseek": "slskd",
    "direct": "direct_download",
    "direct_download": "direct_download",
    "http": "direct_download",
    "https": "direct_download",
    "transmission": "transmission",
    "deluge": "deluge",
    "nzbget": "nzbget",
}
ACTIVE_TASK_STATES = {"queued", "downloading", "import_ready", "importing"}
IMPORTER_RUNNING_STATUSES = {
    "importing",
    "import_busy",
    "copying",
    "writing_metadata",
    "metadata_writing",
    "archive_extracting",
}
VALIDATING_STATUSES = {"validating", "archive_inspection", "preview_importable"}
READY_STATUSES = {"staged_file_ready", "ready_import", "import_ready"}
VISIBLE_STATUSES = {"library_visible", "kavita_verified", "komga_visible"}
VISIBILITY_PENDING_STATUSES = {
    "pending",
    "unknown",
    "verification_pending",
    "waiting_for_library_scan",
    "waiting_for_kavita_scan",
    "imported_not_resolved",
}
VISIBILITY_TIMEOUT_STATUSES = {"scan_timeout", "timeout", "library_scan_timeout", "kavita_scan_timeout"}
IMPORT_FRESH_SECONDS = 6 * 60 * 60
TRANSFER_FRESH_SECONDS = 12 * 60 * 60
STAGED_ATTEMPT_STATUSES = {"staged", "staged_file_ready", "preview_importable", "ready_import", "downloaded"}


def _connect(db_path):
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=1")
    con.execute("pragma busy_timeout=2000")
    return con


def _raw(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _raw_list(value):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _text(*values):
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _number(*values):
    for value in values:
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _latest_number(*values):
    numbers = []
    for value in values:
        number = _number(value)
        if number is not None:
            numbers.append(number)
    return max(numbers) if numbers else None


def _boolean(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def canonical_client_id(value):
    """Normalize UI/filter aliases without confusing source/provider/client."""
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return ""
    if text in CLIENT_ALIASES:
        return CLIENT_ALIASES[text]
    compact = text.replace("_", "")
    if compact in {"qbittorrent", "qbit", "qb"}:
        return "qbittorrent"
    if compact in {"sabnzbd", "sab"}:
        return "sabnzbd"
    if compact in {"slskd", "soulseek"}:
        return "slskd"
    if compact in {"direct", "directdownload", "http", "https"}:
        return "direct_download"
    if compact in {"transmission", "deluge", "nzbget"}:
        return compact
    return text


def completion_dimensions(row, task_raw=None, import_raw=None):
    """Keep managed-file truth independent from reader visibility."""
    row = row if isinstance(row, dict) else {}
    task_raw = task_raw if isinstance(task_raw, dict) else {}
    import_raw = import_raw if isinstance(import_raw, dict) else {}
    task_state = _text(row.get("task_state")).lower()
    task_status = _text(row.get("task_status")).lower()
    import_status = _text(row.get("import_status")).lower()
    visibility = _text(row.get("library_visibility_status")).lower()
    completion_truth = _text(row.get("completion_truth")).lower()
    dest_path = _text(row.get("dest_path"))
    local_path = _text(row.get("local_path"), task_raw.get("detected_path"), task_raw.get("source_path"))
    file_present = bool(local_path or dest_path) and task_status not in {"missing_file", "transfer_missing_stale"}
    archive_valid = None
    if "archive_valid" in import_raw:
        archive_valid = _boolean(import_raw.get("archive_valid"))
    elif import_status in {"bad_archive", "invalid_archive"}:
        archive_valid = False
    elif task_state in {"import_ready", "importing"} or dest_path:
        archive_valid = True
    folder_imported = _boolean(row.get("folder_imported")) or completion_truth == "folder" or bool(dest_path)
    folder_verified = folder_imported and (
        _boolean(row.get("import_verified"))
        or import_status in {"verified", "folder_verified", *VISIBLE_STATUSES}
        or completion_truth in {"folder", "library"}
    )
    scan_requested = _boolean(import_raw.get("reader_scan_requested") or import_raw.get("scan_requested")) or visibility in VISIBILITY_PENDING_STATUSES | VISIBILITY_TIMEOUT_STATUSES
    scan_completed = _boolean(import_raw.get("reader_scan_completed") or import_raw.get("scan_completed")) or visibility in VISIBLE_STATUSES
    reader_visible = visibility in VISIBLE_STATUSES or import_status in VISIBLE_STATUSES
    visibility_error = _text(import_raw.get("reader_visibility_error"), import_raw.get("library_visibility_error")) or None
    visibility_required = _boolean(row.get("library_visibility_required"))
    policy_satisfied = reader_visible if visibility_required else (folder_verified or reader_visible)
    if reader_visible:
        state = "reader_visible"
    elif visibility_error:
        state = "reader_visibility_unavailable"
    elif scan_requested and scan_completed:
        state = "reader_visibility_mismatch"
    elif visibility in VISIBILITY_TIMEOUT_STATUSES:
        state = "reader_visibility_timeout"
    elif scan_requested:
        state = "reader_visibility_pending"
    elif folder_verified:
        state = "folder_verified"
    elif folder_imported:
        state = "imported"
    elif file_present:
        state = "downloaded"
    else:
        state = "missing"
    return {
        "state": state,
        "file_present": file_present,
        "archive_valid": archive_valid,
        "imported_to_managed_folder": folder_imported,
        "folder_verified": folder_verified,
        "reader_scan_requested": scan_requested,
        "reader_scan_completed": scan_completed,
        "reader_visible": reader_visible,
        "reader_visibility_checked_at": _number(import_raw.get("reader_visibility_checked_at"), import_raw.get("library_visibility_checked_at")),
        "reader_visibility_error": visibility_error,
        "completion_policy_satisfied": policy_satisfied,
    }


def _stage(row, task_raw, import_raw, now):
    queue_state = _text(row.get("queue_state")).lower()
    task_state = _text(row.get("task_state")).lower()
    task_status = _text(row.get("task_status")).lower()
    import_status = _text(row.get("import_status")).lower()
    visibility = _text(row.get("library_visibility_status")).lower()
    retry_at = _number(row.get("retry_after"))
    needs_user = queue_state in {"needs_you", "blocked", "failed"}
    if needs_user:
        return "needs_user", "InkDrop needs you to decide before it can carry on."
    if visibility in VISIBLE_STATUSES or import_status in VISIBLE_STATUSES:
        return "completed", "The file is in your library and your reader can see it."
    if visibility in VISIBILITY_PENDING_STATUSES:
        return "reader_verification", "InkDrop is waiting for your reader to pick the file up."
    if visibility in VISIBILITY_TIMEOUT_STATUSES or import_status in VISIBILITY_TIMEOUT_STATUSES:
        return "reader_verification", "Your reader has not picked the file up yet. InkDrop keeps checking; the import still counts as done."
    if _boolean(row.get("import_verified")) or queue_state in {"verified", "completed", "satisfied"}:
        return "completed", "Done. Nothing left to do here."
    if task_state == "import_ready" or task_status in READY_STATUSES:
        return "ready_to_import", "The download finished. InkDrop will check the file and import it."
    if task_status in VALIDATING_STATUSES:
        return "validating", "InkDrop is checking the downloaded file."
    if task_state == "importing" and task_status in IMPORTER_RUNNING_STATUSES:
        return "importing", "InkDrop is writing the file into your library."
    if import_status in {"reader_scan_requested", "scan_requested"}:
        return "reader_scan", "InkDrop asked your reader to scan, and will check afterwards that the file turned up."
    transfer_state = _text(task_raw.get("transfer_state"), task_raw.get("slskd_transfer_state"), task_status).lower()
    if task_state == "downloading" or transfer_state in {"downloading", "transferring", "in_progress"}:
        return "downloading", "Download client is transferring the file."
    if task_state == "queued" or transfer_state in {"queued", "remote_queued", "started_waiting", "waiting_for_slot"}:
        return "client_queued", "Your download client took the job and is waiting for its turn."
    attempt_status = _text(row.get("attempt_status")).lower()
    if attempt_status in {"grab_sent", "handoff", "accepted", "candidate_accepted"}:
        return "handoff", "InkDrop picked a release and is handing it to your download client."
    if queue_state == "searching" or attempt_status in {"searching", "probe_running", "querying"}:
        return "searching", "InkDrop is searching the current source."
    if retry_at and retry_at > now:
        return "retry_scheduled", "InkDrop will retry automatically at the scheduled time."
    return "queued", "InkDrop will search for this on its next pass."


def _work_freshness(row, task_raw, attempt_raw, now):
    queue_state = _text(row.get("queue_state")).lower()
    task_state = _text(row.get("task_state")).lower()
    task_status = _text(row.get("task_status")).lower()
    attempt_status = _text(row.get("attempt_status")).lower()
    import_work = queue_state == "importing" and (
        task_state in {"import_ready", "importing"}
        or task_status in READY_STATUSES | IMPORTER_RUNNING_STATUSES | VALIDATING_STATUSES | VISIBILITY_PENDING_STATUSES
    )
    transfer_work = queue_state in {"source_wait", "downloading"} and task_state in {"queued", "downloading"}
    orphaned_staged = bool(
        queue_state == "importing"
        and not row.get("task_id")
        and not row.get("import_id")
        and attempt_status in STAGED_ATTEMPT_STATUSES
        and _text(
            row.get("attempt_save_path"),
            attempt_raw.get("staged_path"),
            attempt_raw.get("source_path"),
            attempt_raw.get("detected_path"),
        )
    )
    if not (import_work or transfer_work or orphaned_staged):
        return {"state": "not_current_work", "stale": False}
    evidence = _latest_number(
        task_raw.get("heartbeat_at"),
        task_raw.get("last_seen_in_client_at"),
        task_raw.get("client_updated_at"),
        task_raw.get("last_progress_at"),
        task_raw.get("progress_updated_at"),
        task_raw.get("observed_at"),
        task_raw.get("polled_at"),
        row.get("task_updated_at"),
        row.get("task_completed_at"),
        row.get("task_started_at"),
        row.get("attempt_completed_at") if orphaned_staged else None,
        row.get("attempt_started_at") if orphaned_staged else None,
        row.get("queue_updated_at") if orphaned_staged else None,
    )
    max_age = IMPORT_FRESH_SECONDS if import_work or orphaned_staged else TRANSFER_FRESH_SECONDS
    age = max(0.0, now - evidence) if evidence else None
    stale = age is None or age > max_age
    return {
        "state": "stale" if stale else "fresh",
        "stale": stale,
        "kind": "orphaned_staged_import" if orphaned_staged else ("import" if import_work else "transfer"),
        "evidence_at": evidence,
        "age_seconds": age,
        "max_age_seconds": max_age,
        "artifact_preserved": orphaned_staged,
    }


CURRENT_SQL = """
select q.id queue_id,q.wanted_id,q.series_id,q.issue_id,q.state queue_state,1 active,
       q.current_source,q.retry_after,q.last_event,q.updated_at queue_updated_at,q.created_at queue_created_at,
       s.title series_title,i.issue_number,i.title issue_title,
       sa.id attempt_id,sa.source attempt_source,sa.provider_id attempt_provider_id,
       sa.provider attempt_provider,sa.protocol attempt_protocol,sa.status attempt_status,
       sa.download_client attempt_download_client,sa.lifecycle_phase attempt_lifecycle_phase,
       sa.started_at attempt_started_at,sa.completed_at attempt_completed_at,sa.save_path attempt_save_path,sa.raw_json attempt_raw_json,
       (
         select json_group_array(json_object(
           'id', evidence.id,'source', evidence.source,'status', evidence.status,
           'save_path', evidence.save_path,'started_at', evidence.started_at,
           'completed_at', evidence.completed_at,'raw', json(case when json_valid(evidence.raw_json) then evidence.raw_json else '{}' end)
         ))
         from (
           select x.* from source_attempts x where x.queue_id=q.id
           order by coalesce(x.completed_at,x.started_at,0) desc,x.id desc limit 12
         ) evidence
       ) attempt_evidence_json,
       dt.id task_id,dt.source task_source,dt.provider_id task_provider_id,
       dt.provider task_provider,dt.protocol task_protocol,dt.download_client task_download_client,
       dt.external_id client_item_id,dt.status task_status,dt.state task_state,dt.progress task_progress,
       dt.size_bytes,dt.started_at task_started_at,dt.updated_at task_updated_at,dt.completed_at task_completed_at,
       dt.local_path,dt.raw_json task_raw_json,
       ir.id import_id,ir.status import_status,ir.verified import_verified,ir.folder_imported,
       ir.completion_truth,ir.library_visibility_required,ir.library_visibility_status,
       ir.library_visibility_provider,ir.source_path,ir.dest_path,ir.created_at import_created_at,
       ir.raw_json import_raw_json
from queue_items q
join series s on s.id=q.series_id
left join issues i on i.id=q.issue_id
left join source_attempts sa on sa.id=(
  select x.id from source_attempts x where x.queue_id=q.id
  order by coalesce(x.completed_at,x.started_at,0) desc,x.id desc limit 1
)
left join download_tasks dt on dt.id=(
  select x.id from download_tasks x where x.queue_id=q.id
  order by max(
             coalesce(x.updated_at,0),coalesce(x.completed_at,0),coalesce(x.started_at,0),
             coalesce(cast(json_extract(x.raw_json,'$.heartbeat_at') as real),0),
             coalesce(cast(json_extract(x.raw_json,'$.last_seen_in_client_at') as real),0),
             coalesce(cast(json_extract(x.raw_json,'$.client_updated_at') as real),0),
             coalesce(cast(json_extract(x.raw_json,'$.last_progress_at') as real),0),
             coalesce(cast(json_extract(x.raw_json,'$.progress_updated_at') as real),0),
             coalesce(cast(json_extract(x.raw_json,'$.observed_at') as real),0),
             coalesce(cast(json_extract(x.raw_json,'$.polled_at') as real),0)
           ) desc,
           case lower(coalesce(x.state,'')) when 'downloading' then 0 when 'import_ready' then 1 when 'importing' then 2 when 'queued' then 3 else 4 end,
           coalesce(x.updated_at,x.completed_at,x.started_at,0) desc,x.id desc limit 1
)
left join import_results ir on ir.id=(
  select x.id from import_results x where x.queue_id=q.id
  order by coalesce(x.created_at,0) desc,x.id desc limit 1
)
where q.active=1
"""


def _row_contract(row, now=None):
    now = float(now or time.time())
    row = dict(row)
    task_raw = _raw(row.pop("task_raw_json", None))
    attempt_raw = _raw(row.pop("attempt_raw_json", None))
    attempt_evidence = _raw_list(row.pop("attempt_evidence_json", None))
    import_raw = _raw(row.pop("import_raw_json", None))
    stage, next_action = _stage(row, task_raw, import_raw, now)
    freshness = _work_freshness(row, task_raw, attempt_raw, now)
    staged = inkdrop_staged_projection.classify(row, attempt_evidence, now=now)
    if staged:
        freshness = {
            "state": "stale",
            "stale": True,
            "kind": "orphaned_staged_import",
            "evidence_at": now - float(staged.get("age_seconds") or 0),
            "age_seconds": staged.get("age_seconds"),
            "max_age_seconds": inkdrop_staged_projection.STALE_SECONDS,
            "artifact_preserved": True,
            "recoverable": True,
            "source_attempt_id": staged.get("source_attempt_id"),
        }
    if freshness.get("stale"):
        stage = "needs_user"
        next_action = (
            "Import the file that is already staged. Do not download it again or throw it away."
            if freshness.get("kind") == "orphaned_staged_import"
            else "Check what the download client and the importer last reported, then retry."
        )
    updated = _number(row.get("task_updated_at"), row.get("import_created_at"), row.get("attempt_completed_at"), row.get("attempt_started_at"), row.get("queue_updated_at"), row.get("queue_created_at"))
    started = _number(row.get("task_started_at"), row.get("attempt_started_at"), row.get("queue_created_at"))
    bytes_total = _number(task_raw.get("bytes_total"), task_raw.get("size"), row.get("size_bytes"))
    bytes_completed = _number(task_raw.get("bytes_completed"), task_raw.get("downloaded"))
    progress = _number(task_raw.get("percent_complete"), task_raw.get("progress"), row.get("task_progress"))
    if progress is not None and 0 <= progress <= 1:
        progress *= 100
    if bytes_completed is None and bytes_total is not None and progress is not None:
        bytes_completed = bytes_total * progress / 100
    client = _text(row.get("task_download_client"), row.get("attempt_download_client"), row.get("task_source"))
    canonical_client = canonical_client_id(client)
    transfer_state = _text(task_raw.get("transfer_state"), task_raw.get("slskd_transfer_state"), row.get("task_status")) or None
    error = _text(task_raw.get("failure_reason"), task_raw.get("error")) or None
    stalled_reason = _text(task_raw.get("stalled_reason")) or None
    stalled = bool(stalled_reason or task_raw.get("stalled") or freshness.get("stale"))
    if freshness.get("stale") and not stalled_reason:
        stalled_reason = "orphaned_staged_import" if freshness.get("kind") == "orphaned_staged_import" else "stale_work_evidence"
    source_order = attempt_raw.get("source_order") if isinstance(attempt_raw.get("source_order"), list) else []
    source = _text(row.get("task_source"), row.get("attempt_source"), row.get("current_source"))
    ladder_position = source_order.index(source) + 1 if source and source in source_order else None
    completion = completion_dimensions(row, task_raw, import_raw)
    protocol = _text(row.get("task_protocol"), row.get("attempt_protocol"), task_raw.get("protocol"), attempt_raw.get("protocol")) or None
    provider_id = _text(row.get("task_provider_id"), row.get("attempt_provider_id")) or None
    provider_name = _text(row.get("task_provider"), row.get("attempt_provider")) or None
    child_source_id = _text(
        task_raw.get("child_source_id"),
        task_raw.get("indexer_id"),
        task_raw.get("extension_id"),
        attempt_raw.get("child_source_id"),
        attempt_raw.get("indexer_id"),
        attempt_raw.get("extension_id"),
    ) or None
    child_source_name = _text(
        task_raw.get("child_source_name"),
        task_raw.get("indexer"),
        task_raw.get("indexer_name"),
        task_raw.get("extension_name"),
        task_raw.get("source_site"),
        attempt_raw.get("child_source_name"),
        attempt_raw.get("indexer"),
        attempt_raw.get("indexer_name"),
        attempt_raw.get("extension_name"),
        attempt_raw.get("source_site"),
    ) or None
    if not child_source_name and provider_name and provider_id and provider_name.strip().lower() != provider_id.strip().lower():
        child_source_name = provider_name
    return {
        "activity_id": f"queue:{row['queue_id']}",
        "queue_id": row.get("queue_id"),
        "wanted_id": row.get("wanted_id"),
        "series_id": row.get("series_id"),
        "issue_id": row.get("issue_id"),
        "display_title": _text(row.get("series_title"), row.get("issue_title")),
        "issue_or_volume": _text(row.get("issue_number")) or None,
        "stage": stage,
        "state": _text(row.get("task_state"), row.get("queue_state")),
        "provider": provider_name,
        "provider_id": provider_id,
        "child_source_id": child_source_id,
        "child_source_name": child_source_name,
        "protocol": protocol,
        "source": source or None,
        "source_ladder_position": ladder_position,
        "download_client": client or None,
        "client": canonical_client or None,
        "client_item_id": row.get("client_item_id") or None,
        "transfer_state": transfer_state,
        "progress_kind": "determinate" if progress is not None or (bytes_completed is not None and bytes_total) else "indeterminate",
        "percent_complete": progress,
        "bytes_completed": bytes_completed,
        "bytes_total": bytes_total,
        "download_rate": _number(task_raw.get("download_rate"), task_raw.get("speed")),
        "eta_seconds": _number(task_raw.get("eta_seconds"), task_raw.get("eta")),
        "elapsed_seconds": max(0, now - started) if started else None,
        "last_updated_at": updated,
        "import_stage": _text(row.get("import_status"), task_raw.get("import_stage")) or None,
        "next_action": next_action,
        "next_retry_at": _number(row.get("retry_after")),
        "needs_user": stage == "needs_user",
        "stalled": stalled,
        "stalled_reason": stalled_reason,
        "freshness": freshness,
        "error": error,
        "ownership_evidence": {
            "queue": bool(row.get("queue_id")),
            "source_attempt": row.get("attempt_id"),
            "download_task": row.get("task_id"),
            "import_result": row.get("import_id"),
            "completion": completion,
        },
    }


def _sort_value(item, sort_key, now):
    if sort_key == "stage": return item.get("stage") or ""
    if sort_key == "client": return (item.get("client") or canonical_client_id(item.get("download_client")) or "").lower()
    if sort_key == "progress": return item.get("percent_complete") if item.get("percent_complete") is not None else -1
    if sort_key == "eta": return item.get("eta_seconds") if item.get("eta_seconds") is not None else float("inf")
    if sort_key == "age": return now - float(item.get("last_updated_at") or 0)
    if sort_key == "series_title": return (item.get("display_title") or "").lower()
    if sort_key == "next_retry": return item.get("next_retry_at") if item.get("next_retry_at") is not None else float("inf")
    return float(item.get("last_updated_at") or 0)


def _project_all(db_path, now=None):
    now = float(now or time.time())
    with closing(_connect(db_path)) as con:
        rows = con.execute(CURRENT_SQL).fetchall()
    return [_row_contract(row, now) for row in rows]


def activity_current(db_path, *, limit=80, offset=0, sort="last_updated", direction="desc", filters=None):
    limit = max(1, min(int(limit or 80), 200))
    offset = max(0, int(offset or 0))
    requested_sort = str(sort or "last_updated").strip().lower()
    requested_direction = str(direction or "desc").strip().lower()
    applied_sort = requested_sort if requested_sort in SUPPORTED_SORTS else "last_updated"
    applied_direction = requested_direction if requested_direction in SUPPORTED_DIRECTIONS else "desc"
    requested_filters = dict(filters or {})
    applied_filters = {}
    unsupported_filter_values = {}
    for k, v in requested_filters.items():
        if k not in SUPPORTED_FACETS or v in (None, ""):
            continue
        if k == "client":
            canonical = canonical_client_id(v)
            if not canonical:
                unsupported_filter_values[k] = v
                continue
            applied_filters[k] = canonical
        else:
            applied_filters[k] = v
    now = time.time()
    items = _project_all(db_path, now)
    for key, value in applied_filters.items():
        if key in {"needs_user", "stalled"}:
            expected = _boolean(value)
            items = [item for item in items if bool(item.get(key)) == expected]
        elif key == "client":
            expected = canonical_client_id(value)
            items = [item for item in items if canonical_client_id(item.get("client") or item.get("download_client")) == expected]
        else:
            expected = str(value).strip().lower()
            items = [item for item in items if str(item.get(key) or "").strip().lower() == expected]
    items.sort(key=lambda item: (_sort_value(item, applied_sort, now), item.get("activity_id") or ""), reverse=applied_direction == "desc")
    total = len(items)
    page = items[offset:offset + limit]
    return {
        "ok": True,
        "schema": "inkdrop.activity_current.v2",
        "generated_at": now,
        "bounded": True,
        "activity": page,
        "total_count": total,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + len(page) if offset + len(page) < total else None,
        "requested_sort": requested_sort,
        "applied_sort": applied_sort,
        "requested_direction": requested_direction,
        "applied_direction": applied_direction,
        "requested_filters": requested_filters,
        "applied_filters": applied_filters,
        "unsupported_filters": sorted(set(requested_filters) - SUPPORTED_FACETS),
        "unsupported_filter_values": unsupported_filter_values,
        "supported_sorts": sorted(SUPPORTED_SORTS),
        "supported_facets": sorted(SUPPORTED_FACETS),
    }


def activity_detail(db_path, activity_id):
    queue_id = str(activity_id or "").removeprefix("queue:").strip()
    if not queue_id:
        return None
    with closing(_connect(db_path)) as con:
        row = con.execute(CURRENT_SQL + " and q.id=?", (queue_id,)).fetchone()
    return _row_contract(row) if row else None


def activity_summary(db_path):
    generated_at = time.time()
    items = _project_all(db_path, generated_at)
    with closing(_connect(db_path)) as con:
        latest_success = con.execute(
            """
            select id,status,created_at,queue_id,series_id,issue_id
            from import_results
            where verified=1
            order by coalesce(created_at,0) desc,id desc
            limit 1
            """
        ).fetchone()
    counts = {key: 0 for key in ("searching", "client_queued", "downloading", "ready_to_import", "importing", "reader_verification", "retry_scheduled", "needs_user")}
    clients = {
        "qbittorrent": {"active": 0, "queued": 0},
        "sabnzbd": {"active": 0, "queued": 0},
        "slskd": {"active": 0, "remote_queued": 0},
        "direct_download": {"active": 0},
        "transmission": {"active": 0, "queued": 0},
        "deluge": {"active": 0, "queued": 0},
        "nzbget": {"active": 0, "queued": 0},
    }
    for item in items:
        stage = item.get("stage")
        if stage in counts: counts[stage] += 1
        key = canonical_client_id(item.get("client") or item.get("download_client"))
        if not key: continue
        if key not in clients:
            clients[key] = {"active": 0, "queued": 0}
        if key == "slskd" and stage == "client_queued": clients[key]["remote_queued"] += 1
        elif key in {"qbittorrent", "sabnzbd"} and stage == "client_queued": clients[key]["queued"] += 1
        elif key in {"transmission", "deluge", "nzbget"} and stage == "client_queued": clients[key]["queued"] += 1
        else: clients[key]["active"] += 1
    future_retry_times = sorted(
        float(item["next_retry_at"])
        for item in items
        if item.get("next_retry_at") is not None and float(item["next_retry_at"]) > generated_at
    )
    overdue_retry_times = sorted(
        float(item["next_retry_at"])
        for item in items
        if item.get("next_retry_at") is not None and float(item["next_retry_at"]) <= generated_at
    )
    scheduler_state = {
        "schema": "inkdrop.scheduler_summary.v1",
        "scheduler_state": "work_available" if items else "idle",
        "scheduler_heartbeat_at": generated_at,
        "scheduler_heartbeat_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(generated_at)),
        "current_job": None,
        "current_job_started_at": None,
        "last_completed_job": None,
        "last_completed_at": None,
        "next_job": "source_ladder" if future_retry_times else None,
        "next_run_at": future_retry_times[0] if future_retry_times else None,
        "due_now": bool(overdue_retry_times),
        "overdue": bool(overdue_retry_times),
        "overdue_seconds": int(generated_at - overdue_retry_times[0]) if overdue_retry_times else 0,
        "late_job_count": len(overdue_retry_times),
        "blocked_reason": None,
        "worker_healthy": True,
        "source_of_truth": "queue_retry_after",
    }
    return {
        "ok": True,
        "schema": "inkdrop.activity_summary.v2",
        "generated_at": generated_at,
        "worker_state": "work_available" if items else "idle",
        "active_total": len(items),
        **counts,
        "clients": clients,
        "last_successful_action": (
            {
                "kind": "verified_import",
                "id": latest_success["id"],
                "status": latest_success["status"],
                "at": latest_success["created_at"],
                "queue_id": latest_success["queue_id"],
                "series_id": latest_success["series_id"],
                "issue_id": latest_success["issue_id"],
            }
            if latest_success
            else None
        ),
        "next_scheduled_worker_run": scheduler_state["next_run_at"],
        "scheduler": scheduler_state,
    }
