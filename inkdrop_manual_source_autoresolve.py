#!/usr/bin/env python3
import argparse
import calendar
import importlib.util
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import inkdrop_runtime_config
import inkdrop_internal_jobs
import inkdrop_artifact_acceptance
import inkdrop_completed_import

try:
    import inkdrop_state
except Exception:
    inkdrop_state = None


STATE_DIR = inkdrop_runtime_config.state_dir()
CONFIG_DIR = inkdrop_runtime_config.config_dir()
LOG_DIR = inkdrop_runtime_config.log_dir()
STAGING_DIR = inkdrop_runtime_config.staging_dir()
ACTIONS_FILE = STATE_DIR / "manual-review-actions.json"
PROBE_STATUS_FILE = STATE_DIR / "slskd-source-probe-status.json"
PROBE_CACHE_FILE = STATE_DIR / "slskd-source-probe-cache.json"
STATUS_FILE = STATE_DIR / "manual-source-autoresolve-status.json"
LOG_FILE = LOG_DIR / "manual-source-autoresolve.log"
IMPORT_STATUS_FILE = STATE_DIR / "import-status.json"
SERIES_AUTOPILOT_QUEUE_FILE = STATE_DIR / "series-autopilot-queue.json"
MANUAL_SOURCE_QUEUE_SYNC_FILE = STATE_DIR / "manual-source-queue-sync-pending.json"
SLSKD_LEARNING_FILE = STATE_DIR / "slskd-auto-grab-learning.json"
SLSKD_AUTO_GRAB_AUDIT_LOG = LOG_DIR / "slskd-auto-grab-audit.jsonl"
SLSKD_CONFIG = Path(os.environ.get("INKDROP_SLSKD_CONFIG") or CONFIG_DIR / "slskd" / "slskd.yml")
PROBE_SCRIPT = Path(
    os.environ.get("INKDROP_SLSKD_SOURCE_PROBE_SCRIPT")
    or Path(__file__).resolve().with_name("inkdrop_slskd_source_probe.py")
)
KAVITA_DB = inkdrop_runtime_config.kavita_db_path()
COMIC_ROOT = Path(os.environ.get("INKDROP_COMIC_ROOT") or "/library/comics")
MANGA_ROOT = Path(os.environ.get("INKDROP_MANGA_ROOT") or "/library/manga")
KAVITA_COMIC_ROOT = os.environ.get("INKDROP_KAVITA_COMIC_ROOT") or "/data/comics"
KAVITA_MANGA_ROOT = os.environ.get("INKDROP_KAVITA_MANGA_ROOT") or "/data/manga"
SLSKD_DOWNLOAD_ROOT = Path(os.environ.get("INKDROP_SLSKD_DOWNLOAD_ROOT") or STAGING_DIR / "slskd")
LOCK_DIR = inkdrop_runtime_config.lock_dir()
SLSKD_SOURCE_PROBE_LOCK = LOCK_DIR / "inkdrop-slskd-source-probe.lock"
SERIES_AUTOPILOT_LOCK = LOCK_DIR / "inkdrop-series-autopilot.lock"


def python_command():
    return os.environ.get("PYTHON_BIN") or sys.executable or "python3"


def _inkdrop_web_endpoint(env_key, route):
    configured = str(os.environ.get(env_key) or "").strip()
    if configured:
        return configured
    base_url = inkdrop_runtime_config.worker_web_base_url()
    return f"{base_url}{route}"


MANUAL_SOURCE_IMPORT_ROUTE = "/api/manual-source/import-detected"
DEFAULT_MANUAL_SOURCE_IMPORT_API_URL = (
    f"{inkdrop_runtime_config.worker_web_base_url()}{MANUAL_SOURCE_IMPORT_ROUTE}"
)
API_URL = _inkdrop_web_endpoint("INKDROP_MANUAL_SOURCE_IMPORT_API_URL", "/api/manual-source/import-detected")
MARK_WAITING_API_URL = _inkdrop_web_endpoint("INKDROP_MARK_WAITING_API_URL", "/api/manual-source/mark-waiting")
SLSKD_BASE_URL = os.environ.get("INKDROP_SLSKD_API_BASE_URL") or ""
TRANSFER_SETTLE_SECONDS = 180
SLSKD_TRANSFER_SUFFIX_MAX_CANDIDATES = 16
# A locator with N segments has N-2 eligible suffixes after retaining at least
# parent + leaf. Never accept a match unless every eligible suffix was checked.
SLSKD_TRANSFER_SUFFIX_MAX_SEGMENTS = SLSKD_TRANSFER_SUFFIX_MAX_CANDIDATES + 2
INKDROP_STATE_DB = STATE_DIR / (inkdrop_state.STATE_DB_NAME if inkdrop_state else "inkdrop-state.sqlite3")


def env_int(name, default):
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(60, value)


def env_int_between(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(int(minimum), min(int(value), int(maximum)))


def require_slskd_base_url():
    base_url = str(SLSKD_BASE_URL or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("SLSKD API base URL is not configured; set INKDROP_SLSKD_API_BASE_URL before watching SLSKD transfers.")
    if not base_url.startswith(("http://", "https://")):
        base_url = "http://" + base_url
    if not base_url.endswith("/api/v0"):
        base_url = base_url.rstrip("/") + "/api/v0"
    return base_url


SLSKD_WAITING_NO_TRANSFER_STALE_SECONDS = env_int("INKDROP_SLSKD_NO_TRANSFER_STALE_SECONDS", 30 * 60)
SLSKD_WAITING_REMOTE_QUEUE_STALE_SECONDS = env_int("INKDROP_SLSKD_REMOTE_QUEUE_STALE_SECONDS", 10 * 60)
SLSKD_WAITING_LOCAL_QUEUE_STALE_SECONDS = env_int("INKDROP_SLSKD_LOCAL_QUEUE_STALE_SECONDS", 8 * 60)
SLSKD_WAITING_REMOTE_QUEUE_FALLBACK_STALE_SECONDS = env_int("INKDROP_SLSKD_REMOTE_QUEUE_FALLBACK_STALE_SECONDS", 5 * 60)
SLSKD_WAITING_LOCAL_QUEUE_FALLBACK_STALE_SECONDS = env_int("INKDROP_SLSKD_LOCAL_QUEUE_FALLBACK_STALE_SECONDS", 4 * 60)
SLSKD_WAITING_REMOTE_QUEUE_ACTIVE_USER_STALE_SECONDS = env_int("INKDROP_SLSKD_REMOTE_QUEUE_ACTIVE_USER_STALE_SECONDS", 45 * 60)
SLSKD_WAITING_ZERO_PROGRESS_STALE_SECONDS = env_int("INKDROP_SLSKD_ZERO_PROGRESS_STALE_SECONDS", 45 * 60)
SLSKD_WAITING_UNKNOWN_STALE_SECONDS = env_int("INKDROP_SLSKD_UNKNOWN_STALE_SECONDS", 90 * 60)
SLSKD_STALL_SETTING_KEY = "automation.queue_watchdog_slskd_stale_minutes"
SLSKD_STALL_MIN_MINUTES = 5
SLSKD_STALL_MAX_MINUTES = 1440
SQLITE_BUSY_TIMEOUT_SECONDS = 60
SQLITE_BUSY_TIMEOUT_MS = SQLITE_BUSY_TIMEOUT_SECONDS * 1000
DB_IMPORT_RETRY_STATUSES = {
    "import_busy",
    "staged_file_ready",
    "preview_importable",
    "ready_import",
    "transfer_succeeded_missing_stage",
}
DB_IMPORT_RETRY_DOWNLOAD_CLIENT_BLOCKLIST = {
    "inkdrop_direct",
    "inkdrop_external_tool",
    "inkdrop_local_pack",
    "inkdrop_page_pack",
    "qbittorrent",
    "sabnzbd",
}
DB_IMPORT_RETRY_LIMIT = 20
DEFERRED_QUEUE_SYNC_TTL_SECONDS = 48 * 3600
DEFERRED_QUEUE_SYNC_MAX_ITEMS = 80
SLSKD_RETRY_PENDING_TTL_SECONDS = 24 * 3600
SLSKD_RETRY_PENDING_MAX_ROWS = 100
SLSKD_RETRY_PENDING_COOLDOWN_SECONDS = env_int("INKDROP_SLSKD_RETRY_PENDING_COOLDOWN_SECONDS", 10 * 60)
SLSKD_SOURCE_PROBE_LOCK_WAIT_SECONDS = env_int_between("INKDROP_SLSKD_SOURCE_PROBE_LOCK_WAIT_SECONDS", 5, 0, 10)
CONTEXT_FIELDS = (
    "search_query",
    "year",
    "watch_year",
    "watch_publisher",
    "publisher",
    "volume_id",
    "kapowarr_id",
    "comicvine_id",
    "watch_id",
    "queue_identity",
    "autopilot_queue_key",
    "legacy_key",
    "source",
    "autopilot_queue",
    "slskd_transfer_id",
    "last_slskd_transfer_id",
)
SLSKD_ACTIVE_FIELDS = (
    "last_slskd_status",
    "last_slskd_candidate_count",
    "last_slskd_detected_count",
    "last_slskd_failed_candidate_count",
    "last_slskd_auto_grab_safe_count",
    "last_slskd_auto_grab_review_count",
    "last_slskd_auto_grab_blocked_count",
    "last_slskd_autopick_status",
    "last_slskd_autoresolve_status",
    "last_slskd_autoresolve_reason",
    "last_slskd_autoresolve_at",
    "last_slskd_autoresolve_at_iso",
    "last_slskd_waiting_review_id",
    "last_slskd_transfer_id",
    "last_slskd_transfer_state",
    "last_slskd_transfer_requested_at",
    "last_slskd_transfer_started_at",
    "last_slskd_transfer_ended_at",
    "last_slskd_transfer_percent",
    "last_slskd_transfer_bytes_transferred",
    "last_slskd_transfer_bytes_remaining",
    "last_slskd_transfer_average_speed",
    "last_slskd_transfer_attempts",
)


def now():
    return time.time()


def library_frontend_note(text):
    value = str(text or "").strip()
    if not value:
        return ""
    replacements = (
        ("Kavita verified imported file", "Library visibility verified imported file"),
        ("Kavita scan", "library scan"),
        ("kavita scan", "library scan"),
        ("Kavita verification", "library visibility"),
        ("kavita verification", "library visibility"),
        ("visible in Kavita", "visible in a library frontend"),
        ("verified in Kavita", "has library visibility evidence"),
    )
    for old, new in replacements:
        value = value.replace(old, new)
    return value


def utc_stamp(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or now()))


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def save_actions(actions):
    write_json(ACTIONS_FILE, actions)


def retire_manual_source_bad_candidates(actions, review_id, reason, resolved_record=None):
    if not isinstance(actions, dict) or not str(review_id or ""):
        return []
    bad = actions.get("manual_source_bad_candidates")
    if not isinstance(bad, dict):
        return []
    removed = bad.pop(str(review_id), None)
    if not removed:
        return []
    if not isinstance(removed, list):
        removed = [removed]
    actions["manual_source_bad_candidates"] = bad
    history = actions.setdefault("manual_source_bad_candidates_cleared", [])
    if not isinstance(history, list):
        history = []
    record = resolved_record if isinstance(resolved_record, dict) else {}
    history.append({
        "review_id": str(review_id),
        "reason": reason,
        "removed_count": len(removed),
        "series": record.get("series"),
        "issue": record.get("issue"),
        "ts": now(),
        "ts_iso": utc_stamp(),
    })
    actions["manual_source_bad_candidates_cleared"] = history[-100:]
    return removed


def sqlite_connect(path):
    conn = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    conn.execute(f"pragma busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def log(event, **payload):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": now(), "event": event, **payload}, sort_keys=True) + "\n")


def signal_process_group(proc, sig):
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        elif sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def run_process_group(cmd, *, timeout, env=None):
    proc = subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.output or ""
        stderr = exc.stderr or ""
        signal_process_group(proc, signal.SIGTERM)
        try:
            extra_stdout, extra_stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            signal_process_group(proc, signal.SIGKILL)
            try:
                extra_stdout, extra_stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                extra_stdout, extra_stderr = "", "\nprocess group did not exit after SIGKILL"
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output=f"{stdout}{extra_stdout or ''}",
            stderr=f"{stderr}{extra_stderr or ''}",
        )
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def sync_inkdrop_queue_state(reason="manual_source_queue_update"):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    try:
        summary = inkdrop_state.sync_queue_state(STATE_DIR, INKDROP_STATE_DB, mode="queue")
        synced = summary.get("synced") if isinstance(summary, dict) else {}
        log(
            "inkdrop_state_queue_sync",
            reason=reason,
            ok=bool(isinstance(summary, dict) and summary.get("ok")),
            synced=synced if isinstance(synced, dict) else {},
        )
        return summary
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log("inkdrop_state_queue_sync_failed", reason=reason, error=error)
        return {"ok": False, "reason": "inkdrop_state_queue_sync_failed", "error": error}


def export_inkdrop_queue_state(reason="manual_source_queue_export"):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    try:
        summary = inkdrop_state.export_series_autopilot_queue_json(
            STATE_DIR,
            INKDROP_STATE_DB,
            reason=reason,
        )
        log(
            "inkdrop_state_queue_export",
            reason=reason,
            ok=bool(isinstance(summary, dict) and summary.get("ok")),
            exported_count=(summary or {}).get("exported_count") if isinstance(summary, dict) else None,
        )
        return summary
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log("inkdrop_state_queue_export_failed", reason=reason, error=error)
        return {"ok": False, "reason": "inkdrop_state_queue_export_failed", "error": error}


def first_text(*values):
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def nested_text(row, parent, key):
    value = row.get(parent) if isinstance(row, dict) else {}
    if isinstance(value, dict):
        return value.get(key)
    return None


def native_source_for_row(row):
    row = row if isinstance(row, dict) else {}
    recovery = row.get("recovery") if isinstance(row.get("recovery"), dict) else {}
    bad_candidate = recovery.get("bad_candidate") if isinstance(recovery.get("bad_candidate"), dict) else {}
    marker = " ".join(
        str(value or "").lower()
        for value in (
            row.get("path"),
            row.get("source"),
            row.get("candidate_source"),
            row.get("slskd_transfer_id"),
            row.get("username"),
            bad_candidate.get("source"),
            bad_candidate.get("candidate_source"),
            bad_candidate.get("slskd_transfer_id"),
            bad_candidate.get("username"),
        )
    )
    return (
        "slskd"
        if "slskd" in marker
        or row.get("username")
        or row.get("slskd_transfer_id")
        or bad_candidate.get("username")
        or bad_candidate.get("slskd_transfer_id")
        else "manual_source"
    )


def native_attempt_from_autoresolve_row(row, reason):
    row = row if isinstance(row, dict) else {}
    source = native_source_for_row(row)
    status = first_text(row.get("status"), row.get("transfer_status"), "checked")
    filename = first_text(row.get("filename"), Path(str(row.get("path") or "")).name)
    detail = first_text(
        row.get("verification_pending_note"),
        row.get("reason"),
        row.get("error"),
        nested_text(row, "live", "note"),
        nested_text(row, "preview", "note"),
        status.replace("_", " "),
    )
    ts = numeric_ts(row.get("ts")) or now()
    attempt = {
        "source": source,
        "provider": first_text(row.get("username"), "SLSKD" if source == "slskd" else "Manual Source"),
        "protocol": "soulseek" if source == "slskd" else "local",
        "download_client": "SLSKD" if source == "slskd" else "manual",
        "status": status,
        "reason": detail,
        "query": row.get("search_query"),
        "title": first_text(filename, row.get("series")),
        "filename": filename,
        "path": row.get("path"),
        "series": row.get("series"),
        "issue": row.get("issue"),
        "review_id": row.get("review_id"),
        "score": row.get("score") or row.get("candidate_score"),
        "username": row.get("username"),
        "transfer_id": row.get("slskd_transfer_id"),
        "transfer_state": row.get("slskd_transfer_state"),
        "kind": "manual_source_autoresolve",
        "sync_reason": reason,
        "ts": ts,
    }
    live = row.get("live") if isinstance(row.get("live"), dict) else {}
    preview = row.get("preview") if isinstance(row.get("preview"), dict) else {}
    if live:
        attempt["live_state"] = live.get("state")
        attempt["live_resolved"] = bool(live.get("resolved"))
    if preview:
        attempt["preview_state"] = preview.get("state")
        attempt["preview_imported_count"] = preview.get("imported_count")
    transfer = row.get("transfer") if isinstance(row.get("transfer"), dict) else {}
    destinations = []
    resolution = row.get("manual_source_resolution") if isinstance(row.get("manual_source_resolution"), dict) else {}
    resolution_destinations = resolution.get("destinations") if isinstance(resolution.get("destinations"), list) else []
    for value in resolution_destinations:
        if value:
            destinations.append(value)
    imported_rows = row.get("imported") if isinstance(row.get("imported"), list) else []
    for entry in imported_rows:
        if isinstance(entry, dict) and entry.get("dest"):
            destinations.append(entry.get("dest"))
    if destinations:
        attempt["destinations"] = destinations
        attempt["dest_path"] = destinations[0]
    transfer_field_map = {
        "requestedAt": "transfer_requested_at",
        "startedAt": "transfer_started_at",
        "endedAt": "transfer_ended_at",
        "percentComplete": "transfer_percent",
        "bytesTransferred": "transfer_bytes_transferred",
        "bytesRemaining": "transfer_bytes_remaining",
        "averageSpeed": "transfer_average_speed",
        "attempts": "transfer_attempts",
    }
    for source_key, target_key in transfer_field_map.items():
        if transfer.get(source_key) not in (None, ""):
            attempt[target_key] = transfer.get(source_key)
    for source_key, target_key in (
        ("slskd_transfer_requested_at", "transfer_requested_at"),
        ("transfer_percentComplete", "transfer_percent"),
        ("transfer_bytesTransferred", "transfer_bytes_transferred"),
        ("transfer_bytesRemaining", "transfer_bytes_remaining"),
        ("transfer_averageSpeed", "transfer_average_speed"),
    ):
        if row.get(source_key) not in (None, ""):
            attempt[target_key] = row.get(source_key)
    return attempt, ts


def record_native_autoresolve_attempts(result, reason="manual_source_autoresolve"):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing", "attempted": 0}
    rows = queue_sync_rows(result)
    all_rows = [*(rows.get("processed") or []), *(rows.get("skipped") or [])]
    attempted = 0
    recorded = 0
    failed = 0
    deferred = 0
    items = []
    deferred_replay = []
    for row in all_rows:
        queue_id = str((row or {}).get("autopilot_queue_key") or "").strip()
        if not queue_id:
            continue
        attempted += 1
        attempt, ts = native_attempt_from_autoresolve_row(row, reason)
        attempt_id = f"manual-source:{queue_id}:{attempt.get('status')}:{row.get('review_id') or ''}:{int(ts)}"
        try:
            outcome = inkdrop_state.record_queue_source_attempt(
                INKDROP_STATE_DB,
                queue_id,
                attempt,
                attempt_id=attempt_id,
                started_at=ts,
                completed_at=ts,
            )
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower():
                raise
            failed += 1
            deferred += 1
            items.append(
                {
                    "queue_id": queue_id,
                    "status": attempt.get("status"),
                    "source": attempt.get("source"),
                    "ok": False,
                    "deferred": True,
                    "reason": "state_db_locked",
                    "error": str(exc),
                }
            )
            deferred_replay.append(
                {
                    "queue_id": queue_id,
                    "attempt": attempt,
                    "attempt_id": attempt_id,
                    "started_at": ts,
                    "completed_at": ts,
                }
            )
            continue
        compact = {
            "queue_id": queue_id,
            "status": attempt.get("status"),
            "source": attempt.get("source"),
            "ok": bool(outcome.get("ok")) if isinstance(outcome, dict) else False,
        }
        if isinstance(outcome, dict) and outcome.get("ok"):
            recorded += 1
            compact["state"] = outcome.get("state")
        else:
            failed += 1
            compact["reason"] = (outcome or {}).get("reason") if isinstance(outcome, dict) else "record_failed"
        items.append(compact)
    summary = {
        "ok": failed == 0,
        "attempted": attempted,
        "recorded": recorded,
        "failed": failed,
        "deferred": deferred,
        "items": items[:20],
    }
    if deferred:
        summary["defer_reason"] = "state_db_locked"
        deferred_result = dict(result or {})
        deferred_result["native_attempt_replay"] = deferred_replay
        summary["durable_replay"] = persist_deferred_autopilot_queue_sync(
            deferred_result,
            f"{reason}_native_attempt_db_locked",
        )
    if attempted:
        log("inkdrop_state_autoresolve_attempts", reason=reason, **summary)
    return summary


def auto_grab_audit(event, **payload):
    SLSKD_AUTO_GRAB_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SLSKD_AUTO_GRAB_AUDIT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": now(), "event": event, **payload}, sort_keys=True) + "\n")


def load_probe_module(path):
    spec = importlib.util.spec_from_file_location("inkdrop_slskd_probe", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_probe_module(probe):
    apply_settings = getattr(probe, "apply_slskd_provider_settings", None)
    if not callable(apply_settings):
        raise RuntimeError("SLSKD probe does not expose apply_slskd_provider_settings")
    try:
        settings = apply_settings()
    except Exception as exc:
        raise RuntimeError(f"SLSKD provider settings could not be applied: {exc}") from exc
    if not isinstance(settings, dict) or not str(settings.get("download_root") or "").strip():
        raise RuntimeError("SLSKD provider settings did not supply a download root")
    return probe, settings


def load_configured_probe_module(path):
    return configure_probe_module(load_probe_module(path))


def terminal_false_duplicate_review_attempt_evidence(db_path=INKDROP_STATE_DB, limit=500, repair_stale_ownership=False):
    """Return only exact, inactive SLSKD false-duplicate handoff evidence."""
    if not Path(db_path).exists() or inkdrop_state is None:
        return {}
    now_ts = now()
    try:
        limit = max(1, min(int(limit or 500), 5000))
    except (TypeError, ValueError):
        limit = 500
    connection = inkdrop_state.connect(db_path) if repair_stale_ownership else inkdrop_state.connect_read(db_path)
    with connection as con:
        if repair_stale_ownership:
            con.execute("begin immediate")
        rows = con.execute(
            """
            select dt.id as task_id, dt.queue_id, dt.wanted_id, dt.series_id, dt.issue_id,
                   dt.source_attempt_id, dt.candidate_identity as task_candidate_identity,
                   dt.raw_json as task_raw_json,
                   sa.candidate_identity as attempt_candidate_identity,
                   q.state as queue_state, q.active as queue_active,
                   q.current_source as queue_current_source, q.raw_json as queue_raw_json,
                   wi.status as wanted_status
            from download_tasks dt
            join source_attempts sa on sa.id=dt.source_attempt_id
            join queue_items q on q.id=dt.queue_id
            join wanted_items wi on wi.id=q.wanted_id
            where q.active=1
              and lower(coalesce(q.state,'')) in ('queued','searching')
              and lower(coalesce(wi.status,'')) in ('wanted','missing','queued','searching','in_progress')
              and lower(trim(coalesce(q.current_source,''))) in ('','slskd')
              and dt.wanted_id=q.wanted_id
              and dt.series_id=q.series_id
              and dt.issue_id=q.issue_id
              and sa.queue_id=q.id
              and sa.wanted_id=q.wanted_id
              and sa.series_id=q.series_id
              and sa.issue_id=q.issue_id
              and lower(coalesce(dt.source,''))='slskd'
              and lower(coalesce(dt.download_client,''))='slskd'
              and lower(coalesce(sa.source,''))='slskd'
              and lower(coalesce(sa.download_client,''))='slskd'
              and lower(coalesce(sa.lifecycle_phase,''))='failed_candidate'
              and lower(coalesce(sa.status,'')) in (
                    'transfer_failed','transfer_stalled','transfer_succeeded_missing_stage',
                    'transfer_stale_unknown','transfer_missing_stale','staged_file_missing_path',
                    'preview_not_importable','candidate_failed','download_api_error',
                    'download_preflight_api_error','failed_download','bad_archive',
                    'stale_no_local_file','wrong_series_or_subseries','superseded_active_candidate',
                    'stale_failed_transfer_cleared','waiting_record_missing','error','failed'
                  )
              and lower(coalesce(dt.status,''))='preview_not_importable'
              and lower(coalesce(dt.state,''))='failed'
              and lower(coalesce(dt.lifecycle_phase,''))='failed_candidate'
              and lower(coalesce(dt.failure_reason,''))='already_verified_duplicate'
              and trim(coalesce(dt.candidate_identity,''))<>''
              and dt.candidate_identity=sa.candidate_identity
              and json_valid(coalesce(dt.raw_json,''))
              and trim(coalesce(json_extract(dt.raw_json,'$.review_id'),''))<>''
              and trim(coalesce(json_extract(dt.raw_json,'$.filename'),''))<>''
              and json_extract(dt.raw_json,'$.candidate_identity')=dt.candidate_identity
              and not exists (
                    select 1 from queue_claims qc
                    where qc.queue_id=q.id and coalesce(qc.expires_at,0)>?
                  )
              and not exists (
                    select 1 from download_tasks active_dt
                    where active_dt.queue_id=q.id and active_dt.id<>dt.id
                      and (
                        lower(coalesce(active_dt.state,'')) in ('queued','downloading','import_ready','importing')
                        or lower(coalesce(active_dt.lifecycle_phase,'')) in ('downloading','staged_or_importing','verifying')
                        or lower(coalesce(active_dt.status,'')) in (
                          'sent','download_started','started_waiting','already_downloading','waiting_for_transfer',
                          'transfer_in_progress','transfer_settling','waiting_for_staged_file','staged_file_settling',
                          'staged_file_ready','preview_importable','ready_import','import_busy','verification_pending',
                          'imported_not_resolved'
                        )
                      )
                  )
              and not exists (
                    select 1 from source_attempts active_sa
                    where active_sa.queue_id=q.id and active_sa.id<>sa.id
                      and (
                        lower(coalesce(active_sa.lifecycle_phase,'')) in ('downloading','staged_or_importing','verifying')
                        or lower(coalesce(active_sa.display_phase,'')) in ('transferring','staged_or_importing','verifying')
                        or lower(coalesce(active_sa.status,'')) in (
                          'sent','download_started','started_waiting','already_downloading','waiting_for_transfer',
                          'transfer_in_progress','transfer_settling','waiting_for_staged_file','staged_file_settling',
                          'staged_file_ready','preview_importable','ready_import','import_busy','verification_pending',
                          'imported_not_resolved'
                        )
                      )
                  )
              and not exists (
                    select 1 from import_results active_ir
                    where active_ir.queue_id=q.id
                      and lower(coalesce(active_ir.status,'')) in (
                        'importing','import_busy','verification_pending','waiting_for_library_scan',
                        'waiting_for_kavita_scan','imported_not_resolved'
                      )
                  )
            order by coalesce(dt.completed_at,dt.updated_at,dt.started_at,0),dt.id
            limit ?
            """,
            (now_ts, limit),
        ).fetchall()
        evidence = {}
        repaired_queues = set()
        for row in rows:
            strict_completion = False
            verified_rows = con.execute(
                "select * from import_results where queue_id=? and coalesce(verified,0)=1 order by coalesce(created_at,0) desc,id desc",
                (row["queue_id"],),
            ).fetchall()
            for import_row in verified_rows:
                if inkdrop_state.import_result_strict_completion_valid(
                    con, import_row, row["series_id"], row["issue_id"]
                ):
                    strict_completion = True
                    break
            if strict_completion:
                continue
            raw = json_object(row["task_raw_json"])
            review_id = str(raw.get("review_id") or "").strip()
            evidence_id = f"download_task:{row['task_id']}|source_attempt:{row['source_attempt_id']}"
            evidence.setdefault(review_id, []).append(evidence_id)
            if (
                repair_stale_ownership
                and str(row["queue_current_source"] or "").strip().lower() == "slskd"
                and row["queue_id"] not in repaired_queues
            ):
                queue_raw = json_object(row["queue_raw_json"])
                for key in ("last_slskd_waiting_review_id", "last_slskd_transfer_id", "slskd_transfer_id"):
                    queue_raw.pop(key, None)
                queue_raw.update({
                    "state": "queued",
                    "current_source": None,
                    "last_event": "Terminal SLSKD false-duplicate retired; row returned to automatic search",
                    "last_slskd_autoresolve_status": "transfer_failed",
                    "last_slskd_autoresolve_reason": "already_verified_duplicate was not strict completion proof",
                    "next_automatic_step": "slskd_try_next_candidate",
                    "needs_user": False,
                    "slskd_terminal_false_duplicate_reconciled": True,
                    "slskd_terminal_false_duplicate_reconciled_at": now_ts,
                    "slskd_terminal_false_duplicate_reconciled_at_iso": utc_stamp(now_ts),
                    "updated_at": now_ts,
                    "updated_at_iso": utc_stamp(now_ts),
                })
                con.execute(
                    """
                    update queue_items
                    set state='queued',current_source=null,active=1,
                        last_event=?,updated_at=?,outcome='recovering',display_phase='queued',raw_json=?
                    where id=? and active=1 and lower(trim(coalesce(current_source,'')))='slskd'
                    """,
                    (queue_raw["last_event"], now_ts, json.dumps(queue_raw, sort_keys=True), row["queue_id"]),
                )
                con.execute(
                    "update wanted_items set status='wanted',updated_at=? where id=? and lower(coalesce(status,''))='in_progress'",
                    (now_ts, row["wanted_id"]),
                )
                con.execute(
                    """
                    insert or ignore into history_events(
                        id,entity_type,entity_id,series_id,issue_id,event_type,source,message,
                        outcome,display_phase,created_at,raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        inkdrop_state.stable_id("slskd_terminal_false_duplicate_reconciled", row["queue_id"], evidence_id),
                        "queue_item", row["queue_id"], row["series_id"], row["issue_id"],
                        "slskd_terminal_false_duplicate_reconciled", "slskd", queue_raw["last_event"],
                        "recovering", "queued", now_ts,
                        json.dumps({"review_id": review_id, "evidence_id": evidence_id, "previous_current_source": "slskd"}, sort_keys=True),
                    ),
                )
                repaired_queues.add(row["queue_id"])
        if repair_stale_ownership:
            con.commit()
        return evidence


def reconcile_terminal_false_duplicate_review_attempts(probe, db_path=INKDROP_STATE_DB):
    lock_handle, lock_reason = acquire_series_autopilot_lock()
    summary = {
        "review_count": 0,
        "evidence_count": 0,
        "retired_count": 0,
        "rows": [],
        "deferred": lock_handle is None,
        "lock": str(SERIES_AUTOPILOT_LOCK),
    }
    if lock_handle is None:
        summary["reason"] = lock_reason or "series_autopilot_lock_unavailable"
        return summary
    try:
        evidence = terminal_false_duplicate_review_attempt_evidence(db_path, repair_stale_ownership=True)
        for review_id, evidence_ids in evidence.items():
            mutation = probe.retire_auto_grab_review_attempts(
                review_id,
                evidence_ids,
                reason="Authoritative resolver retired terminal already-verified false-duplicate handoff evidence",
            )
            summary["rows"].append(mutation)
            summary["review_count"] += 1
            summary["evidence_count"] += len(evidence_ids)
            summary["retired_count"] += int((mutation or {}).get("retired_count") or 0)
        return summary
    finally:
        release_series_autopilot_lock(lock_handle)


def waiting_records(actions):
    waiting = actions.get("manual_source_waiting") if isinstance(actions, dict) else {}
    if not isinstance(waiting, dict):
        return {}
    return {
        str(review_id): record
        for review_id, record in waiting.items()
        if review_id and isinstance(record, dict)
    }


def json_object(value):
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def db_import_retry_records(min_age_seconds=120, limit=DB_IMPORT_RETRY_LIMIT):
    if not INKDROP_STATE_DB.exists():
        return {}
    now_ts = now()
    min_age = max(0, int(min_age_seconds or 0))
    status_values = sorted(DB_IMPORT_RETRY_STATUSES)
    placeholders = ",".join(["?"] * len(status_values))
    conn = None
    try:
        conn = sqlite_connect(INKDROP_STATE_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                dt.id as download_task_id,
                dt.queue_id,
                dt.wanted_id,
                dt.series_id as task_series_id,
                dt.issue_id as task_issue_id,
                dt.source_attempt_id,
                dt.source,
                dt.provider,
                dt.protocol,
                dt.download_client,
                dt.external_id,
                dt.title as task_title,
                dt.status as task_status,
                dt.state as task_state,
                dt.local_path,
                dt.size_bytes,
                dt.progress,
                dt.started_at,
                dt.updated_at,
                dt.completed_at,
                dt.raw_json as task_raw_json,
                q.id as queue_row_id,
                q.series_id as queue_series_id,
                q.issue_id as queue_issue_id,
                q.state as queue_state,
                q.query as queue_query,
                q.raw_json as queue_raw_json,
                s.title as series_title,
                s.year as series_year,
                s.publisher as series_publisher,
                s.metadata_id as series_metadata_id,
                i.issue_number,
                i.normalized_number,
                i.title as issue_title
            from download_tasks dt
            join queue_items q on q.id = dt.queue_id
            left join series s on s.id = coalesce(dt.series_id, q.series_id)
            left join issues i on i.id = coalesce(dt.issue_id, q.issue_id)
            where q.active = 1
              and lower(coalesce(q.state, '')) not in ('verified','satisfied','superseded_duplicate','blocked','failed')
              and (dt.state in ('import_ready','importing') or lower(coalesce(dt.status,''))='transfer_succeeded_missing_stage')
              and lower(coalesce(dt.status, '')) in ({placeholders})
              and (
                    lower(coalesce(dt.source, '')) in ('slskd','manual','manual_source')
                    or lower(coalesce(dt.download_client, '')) in ('slskd','soulseek')
                  )
              and lower(coalesce(dt.source, '')) not in ('local_pack','download_client')
              and lower(coalesce(dt.provider_id, '')) <> 'local_pack'
              and lower(coalesce(dt.download_client, '')) not in ({",".join(["?"] * len(DB_IMPORT_RETRY_DOWNLOAD_CLIENT_BLOCKLIST))})
              and not exists (select 1 from import_results ir where ir.queue_id=q.id and coalesce(ir.verified,0)=1)
              and (lower(coalesce(dt.status,''))<>'transfer_succeeded_missing_stage' or not exists (select 1 from source_attempts bad where bad.queue_id=q.id
                    and lower(coalesce(bad.status,'')) in ('preview_not_importable','quality_rejected','staged_file_mismatch','staged_file_low_confidence','identity_mismatch','wrong_series_or_subseries','transfer_failed','transfer_stalled')
                    and (
                         trim(coalesce(bad.candidate_identity,''))=''
                         or trim(coalesce(dt.candidate_identity,''))=''
                         or bad.candidate_identity=dt.candidate_identity
                    )
                    and coalesce(bad.completed_at,bad.started_at,0)>=coalesce(dt.completed_at,dt.updated_at,dt.started_at,0)))
              and not exists (select 1 from download_tasks winner where winner.queue_id=q.id
                    and lower(coalesce(winner.status,'')) in ('import_busy','staged_file_ready','preview_importable','ready_import','transfer_succeeded_missing_stage','preview_not_importable','quality_rejected','staged_file_mismatch','staged_file_low_confidence','identity_mismatch','wrong_series_or_subseries','transfer_failed','transfer_stalled')
                    and (
                         lower(coalesce(winner.status,'')) in ('import_busy','staged_file_ready','preview_importable','ready_import','transfer_succeeded_missing_stage')
                         or trim(coalesce(winner.candidate_identity,''))=''
                         or trim(coalesce(dt.candidate_identity,''))=''
                         or winner.candidate_identity=dt.candidate_identity
                    )
                    and (coalesce(winner.completed_at,winner.updated_at,winner.started_at,0)>coalesce(dt.completed_at,dt.updated_at,dt.started_at,0)
                         or (coalesce(winner.completed_at,winner.updated_at,winner.started_at,0)=coalesce(dt.completed_at,dt.updated_at,dt.started_at,0)
                             and (case when lower(coalesce(winner.status,'')) in ('preview_not_importable','quality_rejected','staged_file_mismatch','staged_file_low_confidence','identity_mismatch','wrong_series_or_subseries','transfer_failed','transfer_stalled') then 3 when lower(coalesce(winner.status,''))='transfer_succeeded_missing_stage' then 1 else 2 end
                                  > case when lower(coalesce(dt.status,''))='transfer_succeeded_missing_stage' then 1 else 2 end
                                  or (case when lower(coalesce(winner.status,''))='transfer_succeeded_missing_stage' then 1 else 2 end=case when lower(coalesce(dt.status,''))='transfer_succeeded_missing_stage' then 1 else 2 end and winner.id>dt.id)))))
              and ((lower(coalesce(dt.status,''))<>'transfer_succeeded_missing_stage' and trim(coalesce(dt.local_path, ''))<>'') or (
                    lower(coalesce(dt.status,''))='transfer_succeeded_missing_stage'
                    and lower(coalesce(dt.source,'')) in ('slskd','soulseek')
                    and trim(coalesce(dt.external_id,''))<>''
                    and json_valid(coalesce(dt.raw_json,'{{}}'))
                    and not exists (select 1
                        from json_each(json_array(json_extract(dt.raw_json,'$.transfer_state'),json_extract(dt.raw_json,'$.transfer.state'),json_extract(dt.raw_json,'$.transfer.stateDescription'),json_extract(dt.raw_json,'$.slskd_transfer.state'),json_extract(dt.raw_json,'$.slskd_transfer.stateDescription'))) state
                        join json_each('["timedout","failed","cancelled","canceled","rejected","stalled","error"]') terminal
                        where lower(coalesce(state.value,'')) like '%'||terminal.value||'%')
                    and (replace(lower(coalesce(json_extract(dt.raw_json,'$.transfer_state'),json_extract(dt.raw_json,'$.transfer.state'),json_extract(dt.raw_json,'$.transfer.stateDescription'),json_extract(dt.raw_json,'$.slskd_transfer.state'),json_extract(dt.raw_json,'$.slskd_transfer.stateDescription'),'')),' ','')='completed,succeeded'
                         or (coalesce(json_extract(dt.raw_json,'$.transfer_percent'),json_extract(dt.raw_json,'$.transfer.percentComplete'),json_extract(dt.raw_json,'$.slskd_transfer.percentComplete'),dt.progress,-1)>=100
                             and coalesce(json_extract(dt.raw_json,'$.transfer_bytes_remaining'),json_extract(dt.raw_json,'$.transfer.bytesRemaining'),json_extract(dt.raw_json,'$.slskd_transfer.bytesRemaining'),-1)=0))
                  ))
              and (? - coalesce(dt.updated_at, dt.completed_at, dt.started_at, 0)) >= ?
            order by row_number() over (
                         partition by case when lower(coalesce(dt.status,''))='transfer_succeeded_missing_stage' then 1 else 0 end
                         order by coalesce(dt.updated_at,dt.completed_at,dt.started_at,0),dt.id
                     ),
                     case when lower(coalesce(dt.status,''))='transfer_succeeded_missing_stage' then 1 else 0 end
            limit ?
            """,
            [
                *status_values,
                *sorted(DB_IMPORT_RETRY_DOWNLOAD_CLIENT_BLOCKLIST),
                now_ts,
                min_age,
                max(1, int(limit or DB_IMPORT_RETRY_LIMIT)),
            ],
        ).fetchall()
    except sqlite3.Error as exc:
        log("db_import_retry_records_failed", error=f"{type(exc).__name__}: {exc}")
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    records = {}
    for row in rows:
        task_raw = json_object(row["task_raw_json"])
        queue_raw = json_object(row["queue_raw_json"])
        historical_missing = str(row["task_status"] or "").lower() == "transfer_succeeded_missing_stage"
        transfer = task_raw.get("transfer") or task_raw.get("slskd_transfer") or {}
        local_path = first_text(
            row["local_path"],
            task_raw.get("local_path"),
            task_raw.get("localPath"),
            task_raw.get("staged_path"),
            task_raw.get("stagedPath"),
            task_raw.get("path"),
        )
        if not local_path and not historical_missing:
            continue
        review_id = first_text(
            task_raw.get("review_id"),
            task_raw.get("last_slskd_waiting_review_id"),
            queue_raw.get("last_slskd_waiting_review_id"),
            queue_raw.get("review_id"),
            f"db-import-retry-{row['download_task_id']}",
        )
        if historical_missing:
            review_id = f"historical-missing-stage-{row['queue_id']}"
        series = first_text(
            row["series_title"],
            task_raw.get("series"),
            task_raw.get("query"),
            queue_raw.get("series"),
            queue_raw.get("query"),
            row["queue_query"],
        )
        issue = first_text(
            row["issue_number"],
            row["normalized_number"],
            task_raw.get("issue"),
            queue_raw.get("issue"),
        )
        filename = first_text(
            transfer.get("filename") if isinstance(transfer, dict) else None,
            task_raw.get("filename"),
            task_raw.get("candidate_filename"),
            task_raw.get("candidate_filename_leaf"),
            row["task_title"],
            local_path,
        )
        if historical_missing:
            transfer = dict(transfer) if isinstance(transfer, dict) else {}
            transfer.setdefault("id", row["external_id"])
            transfer.setdefault("filename", filename)
            transfer.setdefault("state", "Completed, Succeeded")
        record = {
            "review_id": review_id,
            "series": series,
            "query": series or row["queue_query"],
            "issue": issue,
            "reason": "db_import_retry",
            "source": "inkdrop_download_task_retry",
            "autoresolve_source": "inkdrop_download_task_retry",
            "candidate_source": "slskd_probe",
            "autopilot_queue": True,
            "autopilot_queue_key": row["queue_id"],
            "queue_key": row["queue_id"],
            "queue_identity": first_text(task_raw.get("queue_identity"), queue_raw.get("queue_identity")),
            "filename": filename,
            "filename_leaf": filename_leaf(filename or local_path),
            "candidate_filename_leaf": filename_leaf(filename or local_path),
            "local_path": local_path,
            "path": local_path,
            "detected_path": local_path,
            "username": first_text(row["provider"], task_raw.get("username"), task_raw.get("provider")),
            "candidate_score": first_text(task_raw.get("candidate_score"), task_raw.get("score")),
            "candidate_size": first_text(row["size_bytes"], task_raw.get("candidate_size"), task_raw.get("size")),
            "slskd_transfer_id": first_text(row["external_id"], task_raw.get("slskd_transfer_id"), task_raw.get("transfer_id")),
            "slskd_transfer": transfer if historical_missing and isinstance(transfer, dict) else None,
            "historical_false_missing_stage": historical_missing,
            "db_download_task_id": row["download_task_id"],
            "download_task_id": row["download_task_id"],
            "source_attempt_id": row["source_attempt_id"],
            "external_id": row["external_id"],
            "db_retry_status": row["task_status"],
            "db_retry_state": row["task_state"],
            "db_retry_age_seconds": int(now_ts - float(row["updated_at"] or row["completed_at"] or row["started_at"] or now_ts)),
            "ts": row["updated_at"] or row["completed_at"] or row["started_at"] or now_ts,
        }
        for key in CONTEXT_FIELDS:
            if key == "source":
                continue
            value = first_text(task_raw.get(key), queue_raw.get(key))
            if value:
                record[key] = value
        if row["series_year"]:
            record.setdefault("year", row["series_year"])
            record.setdefault("watch_year", row["series_year"])
        if row["series_publisher"]:
            record.setdefault("publisher", row["series_publisher"])
            record.setdefault("watch_publisher", row["series_publisher"])
        if row["series_metadata_id"]:
            record.setdefault("comicvine_id", row["series_metadata_id"])
        records[str(review_id)] = record
    if records:
        log("db_import_retry_records_found", count=len(records), statuses=status_values)
    return records


def hidden_review_ids(actions):
    if not isinstance(actions, dict):
        return set()
    return set(actions.get("ignored") or []) | set(actions.get("approved") or []) | set(actions.get("bad") or [])


def status_items(status):
    items = status.get("items") if isinstance(status, dict) else {}
    return items if isinstance(items, dict) else {}


def autopilot_queue_items():
    queue = read_json(SERIES_AUTOPILOT_QUEUE_FILE, {}) or {}
    items = queue.get("items") if isinstance(queue, dict) else {}
    return items if isinstance(items, dict) else {}


def autopilot_queue_item_for_record(record, review_id="", queue_items=None):
    if not isinstance(record, dict):
        return None
    items = queue_items if isinstance(queue_items, dict) else autopilot_queue_items()
    for key in (
        record.get("autopilot_queue_key"),
        record.get("queue_key"),
        record.get("legacy_key"),
    ):
        key = str(key or "").strip()
        if key and isinstance(items.get(key), dict):
            return items[key]
    review_key = str(review_id or record.get("review_id") or "").strip()
    if review_key:
        for item in items.values():
            if isinstance(item, dict) and str(item.get("last_slskd_waiting_review_id") or "") == review_key:
                return item
    series_key = normalize_key(record.get("series") or record.get("query"))
    issue_key = normalize_key(record.get("issue"))
    identity = str(record.get("queue_identity") or "").strip()
    if not series_key or not issue_key:
        return None
    for item in items.values():
        if not isinstance(item, dict):
            continue
        if identity and str(item.get("queue_identity") or "") != identity:
            continue
        if normalize_key(item.get("series")) != series_key:
            continue
        if normalize_key(item.get("issue")) != issue_key:
            continue
        return item
    return None


def autopilot_queue_item_verified(record, review_id="", queue_items=None):
    item = autopilot_queue_item_for_record(record, review_id=review_id, queue_items=queue_items)
    return isinstance(item, dict) and item.get("state") == "verified"


def durable_autopilot_queue_item_verified(record):
    """Fence retries against newer durable queue truth than the JSON projection."""
    if not isinstance(record, dict) or not INKDROP_STATE_DB.exists():
        return False
    queue_id = str(record.get("autopilot_queue_key") or record.get("queue_key") or "").strip()
    series_key = normalize_key(record.get("series") or record.get("query"))
    issue_key = normalize_key(record.get("issue"))
    if not queue_id or not series_key or not issue_key:
        return False
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{INKDROP_STATE_DB.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select state, series_id, raw_json from queue_items where id=? limit 1",
            (queue_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        log("durable_verified_retry_fence_failed", queue_id=queue_id, error=f"{type(exc).__name__}: {exc}")
        return False
    finally:
        if conn is not None:
            conn.close()
    if not row or str(row["state"] or "").strip().lower() not in {"verified", "satisfied", "superseded_duplicate"}:
        return False
    raw = json_object(row["raw_json"])
    durable_series_key = normalize_key(raw.get("series") or raw.get("series_title"))
    durable_issue_key = normalize_key(raw.get("issue") or raw.get("issue_number") or raw.get("chapter"))
    if durable_series_key != series_key or durable_issue_key != issue_key:
        return False
    identity = str(record.get("queue_identity") or "").strip()
    durable_identity = str(raw.get("queue_identity") or row["series_id"] or "").strip()
    return not identity or durable_identity == identity


def prune_verified_probe_records(queue_items=None):
    items_by_key = queue_items if isinstance(queue_items, dict) else autopilot_queue_items()
    removed = 0
    for path, nested in ((PROBE_CACHE_FILE, False), (PROBE_STATUS_FILE, True)):
        data = read_json(path, {}) or {}
        records = status_items(data) if nested else (data if isinstance(data, dict) else {})
        if not isinstance(records, dict):
            continue
        stale = [
            str(review_id)
            for review_id, entry in records.items()
            if isinstance(entry, dict) and autopilot_queue_item_verified(entry, review_id=review_id, queue_items=items_by_key)
        ]
        if not stale:
            continue
        for review_id in stale:
            records.pop(review_id, None)
        if nested:
            data["items"] = records
            data["verified_queue_pruned_count"] = int(data.get("verified_queue_pruned_count") or 0) + len(stale)
            data["verified_queue_pruned_at"] = now()
            data["verified_queue_pruned_at_iso"] = utc_stamp()
            write_json(path, data)
        else:
            write_json(path, records)
        removed += len(stale)
    return removed


def clear_verified_action_records(actions, queue_items=None):
    if not isinstance(actions, dict):
        return 0
    items_by_key = queue_items if isinstance(queue_items, dict) else autopilot_queue_items()
    cleared = 0
    now_ts = now()
    for field, history_field in (
        ("manual_source_waiting", "manual_source_waiting_cleared"),
        ("manual_source_retry_pending", "manual_source_retry_pending_cleared"),
    ):
        records = actions.get(field)
        if not isinstance(records, dict):
            continue
        stale = [
            (str(review_id), record)
            for review_id, record in records.items()
            if isinstance(record, dict) and autopilot_queue_item_verified(record, review_id=review_id, queue_items=items_by_key)
        ]
        if not stale:
            continue
        history = actions.setdefault(history_field, [])
        if not isinstance(history, list):
            history = []
        for review_id, record in stale:
            records.pop(review_id, None)
            retire_manual_source_bad_candidates(actions, review_id, "autopilot_queue_verified", record)
            history.append({
                "review_id": review_id,
                "previous": record,
                "reason": "autopilot_queue_verified",
                "detail": "watched-series queue already verified this issue",
                "ts": now_ts,
                "ts_iso": utc_stamp(now_ts),
            })
            cleared += 1
        actions[field] = records
        actions[history_field] = history[-100:]
    if cleared:
        save_actions(actions)
    return cleared


def ready_detected_records(status, actions):
    hidden = hidden_review_ids(actions)
    out = {}
    waiting = set(waiting_records(actions))
    items = dict(status_items(status))
    queue_items = autopilot_queue_items()
    for review_id, entry in items.items():
        review_id = str(review_id or "")
        if not review_id or review_id in hidden or review_id in waiting or not isinstance(entry, dict):
            continue
        if autopilot_queue_item_verified(entry, review_id=review_id, queue_items=queue_items):
            continue
        if int(entry.get("detected_count") or 0) <= 0:
            continue
        out[review_id] = {
            "series": entry.get("series"),
            "query": entry.get("series") or entry.get("query"),
            "issue": entry.get("issue"),
            "reason": entry.get("reason") or "no_safe_source",
            "source": "ready_detected",
        }
    return out


def refresh_probe_rows(probe, records, default_reason="no_safe_source"):
    cache = read_json(PROBE_CACHE_FILE, {}) or {}
    status = read_json(PROBE_STATUS_FILE, {}) or {}
    items = dict(status_items(status))
    refreshed = []
    for review_id, record in records.items():
        item = {
            "review_id": review_id,
            "series": record.get("series"),
            "query": record.get("series") or record.get("query"),
            "issue": record.get("issue"),
            "reason": record.get("reason") or default_reason,
        }
        for key in CONTEXT_FIELDS:
            value = record.get(key)
            if value not in (None, ""):
                item[key] = value
        entry = dict(cache.get(review_id) or items.get(review_id) or {})
        entry.update({
            "review_id": review_id,
            "series": item.get("series"),
            "issue": item.get("issue"),
            "reason": entry.get("reason") or item.get("reason") or default_reason,
        })
        for key in CONTEXT_FIELDS:
            value = item.get(key)
            if value not in (None, ""):
                entry[key] = value
        targeted = targeted_waiting_detected_files(probe, item, record)
        if targeted:
            entry["staged_scan_at"] = now()
            entry["staged_scan_at_iso"] = utc_stamp()
            entry["detected_count"] = len(targeted)
            entry["detected_files"] = targeted
            entry["status"] = "staged_file_ready"
        else:
            entry = probe.attach_staged_detection(entry, item)
        if not int(entry.get("detected_count") or 0):
            entry["status"] = entry.get("status") or "manual_source_no_staged_file"
        entry["autoresolve_source"] = record.get("source") or "waiting"
        cache[review_id] = entry
        items[review_id] = entry
        refreshed.append(entry)

    if isinstance(status, dict):
        status = dict(status)
    else:
        status = {}
    status["items"] = items
    status["generated_at"] = now()
    status["generated_at_iso"] = utc_stamp()
    status["manual_source_autoresolve_refresh_count"] = len(refreshed)
    available = [
        item for item in items.values()
        if int((item or {}).get("candidate_count") or 0) > 0 or int((item or {}).get("detected_count") or 0) > 0
    ]
    staged = [item for item in items.values() if int((item or {}).get("detected_count") or 0) > 0]
    status["available_review_count"] = len(available)
    status["candidate_count"] = sum(int((item or {}).get("candidate_count") or 0) for item in available)
    status["staged_review_count"] = len(staged)
    status["detected_file_count"] = sum(int((item or {}).get("detected_count") or 0) for item in staged)
    status["policy"] = (
        "SLSKD probe cache may be refreshed for Manual Source waiting rows and exact Ready to Import rows. "
        "The autoresolver imports only refreshed detected files via the existing web import endpoint, "
        "after a preview proves the detected file is importable."
    )
    write_json(PROBE_CACHE_FILE, cache)
    write_json(PROBE_STATUS_FILE, status)
    return refreshed, status


def refresh_waiting_probe_rows(probe, waiting):
    return refresh_probe_rows(probe, waiting, default_reason="no_safe_source")


def stable_detected_file(row, min_age_seconds):
    path = Path(str((row or {}).get("path") or ""))
    try:
        if not path.is_file():
            return False, "file missing"
        age = now() - path.stat().st_mtime
    except OSError as exc:
        return False, f"stat failed: {exc}"
    if age < min_age_seconds:
        return False, f"file age {int(age)}s below {int(min_age_seconds)}s"
    return True, "stable"


def filename_leaf(value):
    parts = [part for part in re.split(r"[\\/]+", str(value or "")) if part]
    return parts[-1] if parts else str(value or "")


def filename_key(value):
    leaf = filename_leaf(value)
    stem = re.sub(r"\.[a-z0-9]{1,6}$", "", leaf, flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()


def normalize_key(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def extension_for(value):
    match = re.search(r"(\.[a-z0-9]{1,8})$", filename_leaf(value), flags=re.I)
    return match.group(1).lower() if match else ""


def path_segments(value):
    return [part for part in re.split(r"[\\/]+", str(value or "")) if part]


def unique_paths(paths):
    out = []
    seen = set()
    for path in paths:
        try:
            key = str(Path(path).resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(Path(path))
    return out


def safe_child_path(root, parts):
    clean = []
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        if (
            text in {".", ".."}
            or text.startswith(("/", "\\"))
            or re.match(r"^[a-zA-Z]:", text)
            or any(segment in {".", ".."} for segment in path_segments(text))
        ):
            return None
        clean.append(text)
    if not clean:
        return None
    try:
        path = Path(root).joinpath(*clean)
        path.resolve().relative_to(Path(root).resolve())
        return path
    except (OSError, ValueError):
        return None


def contained_child_path(root, candidate):
    """Resolve one candidate only when it remains below the configured root."""

    try:
        resolved_root = Path(root).resolve()
        resolved_candidate = Path(candidate).resolve()
        resolved_candidate.relative_to(resolved_root)
        return resolved_candidate
    except (OSError, RuntimeError, ValueError):
        return None


def safe_transfer_path_segments(value):
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", text):
        return None
    segments = path_segments(text)
    if any(segment in {".", ".."} for segment in segments):
        return None
    return segments


def unique_exact_transfer_suffix(root, segments):
    """Return one exact contained path after stripping remote-only prefixes.

    This deliberately performs no recursive or basename search.  Only bounded
    trailing suffixes containing at least a parent directory and leaf are
    checked, and ambiguity fails closed.
    """

    segments = list(segments or [])
    suffix_candidate_count = max(0, len(segments) - 2)
    if (
        len(segments) < 3
        or len(segments) > SLSKD_TRANSFER_SUFFIX_MAX_SEGMENTS
        or suffix_candidate_count > SLSKD_TRANSFER_SUFFIX_MAX_CANDIDATES
    ):
        return None
    if any(
        not str(segment or "").strip()
        or str(segment).strip() in {".", ".."}
        or str(segment).startswith(("/", "\\"))
        or re.match(r"^[a-zA-Z]:", str(segment))
        for segment in segments
    ):
        return None
    matches = []
    seen = set()
    attempts = 0
    # start=0 is the normal mirrored path and is checked by the caller.  A
    # suffix repair must strip at least one prefix and retain parent + leaf.
    for start in range(1, len(segments) - 1):
        if attempts >= SLSKD_TRANSFER_SUFFIX_MAX_CANDIDATES:
            break
        attempts += 1
        path = safe_child_path(root, segments[start:])
        candidate = contained_child_path(root, path) if path is not None else None
        if candidate is None or not candidate.is_file():
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        matches.append(candidate)
        if len(matches) > 1:
            return None
    return matches[0] if len(matches) == 1 else None


def generic_slskd_segment(value):
    return normalize_key(value) in {
        "books",
        "book",
        "comics",
        "comic",
        "current series",
        "image",
        "manga",
        "soulseek shared folder",
    }


def likely_slskd_stage_dirs(probe, record):
    root = Path(getattr(probe, "SLSKD_DOWNLOAD_ROOT", SLSKD_DOWNLOAD_ROOT))
    dirs = [root]
    series = str((record or {}).get("series") or "").strip()
    if series:
        dirs.append(root / series)
    filename = str((record or {}).get("filename") or "")
    for part in path_segments(filename)[:-1]:
        if generic_slskd_segment(part):
            continue
        dirs.append(root / part)
    series_words = [word for word in normalize_key(series).split() if len(word) > 2][:3]
    if series_words and root.exists():
        try:
            for idx, child in enumerate(root.iterdir()):
                if idx >= 300:
                    break
                if child.is_dir():
                    child_key = normalize_key(child.name)
                    if all(word in child_key for word in series_words):
                        dirs.append(child)
        except OSError:
            pass
    return unique_paths(path for path in dirs if path.exists())


def detected_is_slskd_file(detected):
    path = str((detected or {}).get("path") or "").strip()
    if not path:
        return False
    root = SLSKD_DOWNLOAD_ROOT
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def slskd_candidate_recovery_allowed(record, detected):
    if str((record or {}).get("candidate_source") or "") == "slskd_probe":
        return True
    if detected_is_slskd_file(detected):
        return True
    return False


def candidate_matches_waiting_record(candidate, record):
    if not isinstance(candidate, dict) or not isinstance(record, dict):
        return False
    record_user = normalize_key(record.get("username") or "")
    candidate_user = normalize_key(candidate.get("username") or "")
    if record_user and candidate_user and record_user != candidate_user:
        return False
    record_values = {
        filename_key(record.get("filename")),
        filename_key(record.get("filename_leaf")),
        filename_key(record.get("candidate_filename_leaf")),
    }
    candidate_values = {
        filename_key(candidate.get("filename")),
        filename_key(candidate.get("path")),
        filename_key(candidate.get("filename_leaf")),
        filename_key(candidate.get("remote_filename")),
    }
    return bool({value for value in record_values if value} & {value for value in candidate_values if value})


def detected_row_from_path(probe, item, root, path, *, record=None, source="slskd_downloads", direct_transfer=False):
    try:
        stat = path.stat()
    except OSError:
        return None
    details = probe.staged_match_details(path, root, item)
    if not details.get("matched") and record:
        path_key = filename_key(path.name)
        expected_values = [
            (record or {}).get("filename"),
            (record or {}).get("filename_leaf"),
            (record or {}).get("candidate_filename_leaf"),
        ]
        expected_leafs = [filename_leaf(value) for value in expected_values if filename_leaf(value)]
        expected_keys = [filename_key(value) for value in expected_values if filename_key(value)]
        leaf_matches_selected_candidate = (
            path.name in expected_leafs
            or (
                path_key
                and any(
                    path_key == key or (len(key) >= 8 and (key in path_key or path_key in key))
                    for key in expected_keys
                )
            )
        )
        context_filename = str((record or {}).get("filename") or "")
        if leaf_matches_selected_candidate and context_filename and hasattr(probe, "item_match_details"):
            context_details = probe.item_match_details(context_filename, item)
            if context_details.get("matched"):
                details = dict(context_details)
                details["match_basis"] = "waiting_candidate_context"
                details["match_text"] = context_filename
                reasons = list(details.get("reasons") or [])
                reasons.append("staged filename matched selected SLSKD candidate")
                if direct_transfer:
                    reasons.append("completed SLSKD transfer path matched selected candidate")
                details["reasons"] = list(dict.fromkeys(str(value) for value in reasons if value))
    if not details.get("matched"):
        return None
    return {
        "source": source,
        "root": str(root),
        "path": str(path),
        "filename": path.name,
        "size": int(stat.st_size),
        "mtime": stat.st_mtime,
        "mtime_iso": utc_stamp(stat.st_mtime),
        "extension": extension_for(path.name),
        "score": int(details.get("score") or 0),
        "match_reasons": list(details.get("reasons") or []),
        "match_penalties": list(details.get("penalties") or []),
        "match_basis": details.get("match_basis") or "filename",
        "match_text": details.get("match_text") or path.name,
        "targeted_waiting_match": True,
        "direct_transfer_match": bool(direct_transfer),
    }


def candidate_can_be_retry_fallback(candidate):
    if not isinstance(candidate, dict):
        return False
    auto_grab = candidate.get("auto_grab") if isinstance(candidate.get("auto_grab"), dict) else {}
    if candidate.get("manual_source_bad_candidate") or auto_grab.get("previous_failure"):
        return False
    verdict = str(auto_grab.get("verdict") or "").strip()
    if verdict == "blocked":
        return False
    if verdict == "auto_grab_safe" or auto_grab.get("autopick_eligible"):
        return True
    # The probe has a retry-fallback path for exact candidates that were not
    # first choice. Count them here so we can fail over faster when they exist.
    return verdict == "needs_review"


def viable_cached_slskd_fallback_count(record):
    review_id = str((record or {}).get("review_id") or "").strip()
    if not review_id:
        return 0
    cache = read_json(PROBE_CACHE_FILE, {}) or {}
    entry = cache.get(review_id) if isinstance(cache, dict) else None
    if not isinstance(entry, dict):
        return 0
    count = 0
    for candidate in entry.get("candidates") or []:
        if not candidate_can_be_retry_fallback(candidate):
            continue
        if candidate_matches_waiting_record(candidate, record):
            continue
        count += 1
    return count


def targeted_waiting_detected_files(probe, item, record):
    if str((record or {}).get("candidate_source") or "") != "slskd_probe":
        return []
    expected_values = [
        (record or {}).get("filename"),
        (record or {}).get("filename_leaf"),
        (record or {}).get("candidate_filename_leaf"),
        (record or {}).get("local_path"),
        (record or {}).get("path"),
        (record or {}).get("detected_path"),
    ]
    expected_leafs = []
    expected_keys = []
    for value in expected_values:
        leaf = filename_leaf(value)
        key = filename_key(value)
        if leaf and leaf not in expected_leafs:
            expected_leafs.append(leaf)
        if key and key not in expected_keys:
            expected_keys.append(key)
    if not expected_leafs and not expected_keys:
        return []

    root = Path(getattr(probe, "SLSKD_DOWNLOAD_ROOT", SLSKD_DOWNLOAD_ROOT))
    candidates = []
    for value in (
        (record or {}).get("local_path"),
        (record or {}).get("path"),
        (record or {}).get("detected_path"),
        (record or {}).get("import_path"),
        (record or {}).get("staged_path"),
    ):
        text = str(value or "").strip()
        if not text:
            continue
        candidate = Path(text)
        if candidate.is_file():
            candidates.append(candidate)
    for directory in likely_slskd_stage_dirs(probe, record):
        for leaf in expected_leafs:
            candidate = directory / leaf
            if candidate.is_file():
                candidates.append(candidate)
        if candidates:
            continue
        scanned = 0
        try:
            for path in directory.rglob("*"):
                scanned += 1
                if scanned > 500:
                    break
                if not path.is_file():
                    continue
                path_key = filename_key(path.name)
                if path_key and any(path_key == key or (len(key) >= 8 and (key in path_key or path_key in key)) for key in expected_keys):
                    candidates.append(path)
        except OSError:
            continue

    rows = []
    for path in unique_paths(candidates):
        row = detected_row_from_path(probe, item, root, path, record=record)
        if not row:
            continue
        rows.append(row)
    rows.sort(key=lambda row: (int(row.get("score") or 0), float(row.get("mtime") or 0)), reverse=True)
    return rows[:8]


def transfer_local_path_candidates(probe, record, transfer):
    if not isinstance(transfer, dict) or transfer.get("status") != "transfer_succeeded":
        return []
    root = Path(getattr(probe, "SLSKD_DOWNLOAD_ROOT", SLSKD_DOWNLOAD_ROOT))
    filename_value = transfer.get("filename") or record.get("filename")
    transfer_segments = safe_transfer_path_segments(filename_value)
    directory_segments = safe_transfer_path_segments(transfer.get("directory"))
    if transfer_segments is None or directory_segments is None:
        return []
    leaf = filename_leaf(filename_value)
    if not leaf:
        return []
    remote_segments = transfer_segments if len(transfer_segments) > 1 else [*directory_segments, leaf]
    paths = []
    # Basename/series guesses are only valid when SLSKD supplied no remote
    # directory context. A qualified remote locator must converge through an
    # exact mirrored path or one unique exact suffix.
    if len(remote_segments) <= 1:
        base_dirs = [root]
        series = str((record or {}).get("series") or "").strip()
        if series:
            series_dir = safe_child_path(root, [series])
            if series_dir:
                base_dirs.append(series_dir)
        for directory in unique_paths(base_dirs):
            paths.append(directory / leaf)
    if len(transfer_segments) > 1:
        relative = safe_child_path(root, transfer_segments)
        if relative:
            paths.append(relative)
    if directory_segments:
        relative = safe_child_path(root, [*directory_segments, leaf])
        if relative:
            paths.append(relative)
    suffix_match = unique_exact_transfer_suffix(root, remote_segments)
    if suffix_match is not None:
        paths.append(suffix_match)
    contained = []
    for path in unique_paths(paths):
        candidate = contained_child_path(root, path)
        if candidate is not None and candidate.is_file():
            contained.append(candidate)
    if len(remote_segments) > 1 and len(contained) != 1:
        return []
    return contained


def completed_transfer_detected_files(probe, item, record, transfer, *, candidates=None):
    root = Path(getattr(probe, "SLSKD_DOWNLOAD_ROOT", SLSKD_DOWNLOAD_ROOT))
    rows = []
    paths = transfer_local_path_candidates(probe, record, transfer) if candidates is None else list(candidates)
    for path in paths:
        path = contained_child_path(root, path)
        if path is None:
            continue
        row = detected_row_from_path(
            probe,
            item,
            root,
            path,
            record=record,
            source="slskd_completed_transfer",
            direct_transfer=True,
        )
        if row:
            rows.append(row)
    rows.sort(key=lambda row: (int(row.get("score") or 0), float(row.get("mtime") or 0)), reverse=True)
    return rows[:4]


def completed_transfer_rejection_evidence(probe, record, transfer, detected, *, candidates=None):
    """Expose one exact completed artifact rejected by the item matcher.

    A completed transfer can point to a file that exists while still being the
    wrong artifact for the wanted item.  Treat that as candidate failure, not
    as a missing staging file.  Ambiguous or uncontained paths fail closed.
    """

    if detected:
        return None
    root = Path(getattr(probe, "SLSKD_DOWNLOAD_ROOT", SLSKD_DOWNLOAD_ROOT))
    paths = transfer_local_path_candidates(probe, record, transfer) if candidates is None else list(candidates)
    if len(paths) != 1:
        return None
    path = contained_child_path(root, paths[0])
    if path is None or not path.is_file():
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "source": "slskd_completed_transfer_rejected",
        "root": str(root),
        "path": str(path),
        "filename": path.name,
        "size": int(stat.st_size),
        "mtime": stat.st_mtime,
        "mtime_iso": utc_stamp(stat.st_mtime),
        "extension": extension_for(path.name),
        "direct_transfer_match": True,
        "artifact_acceptance_rejected": True,
    }


def merge_detected_file_rows(*groups):
    rows = []
    seen = set()
    for group in groups:
        for row in group or []:
            if not isinstance(row, dict):
                continue
            key = str(row.get("path") or row.get("filename") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(row)
    rows.sort(
        key=lambda row: (
            1 if row.get("direct_transfer_match") or row.get("source") == "slskd_completed_transfer" else 0,
            int(row.get("score") or 0),
            float(row.get("mtime") or 0),
        ),
        reverse=True,
    )
    return rows


def slskd_learning_path_style(value):
    parts = path_segments(value)
    if not parts:
        return ""
    ext = extension_for(parts[-1])
    parent = normalize_key(parts[-2]) if len(parts) >= 2 else "root"
    context = ""
    for part in reversed(parts[:-1]):
        key = normalize_key(part)
        words = set(key.split())
        if words & {"book", "books", "cbz", "cbr", "comic", "comics", "graphic", "manga", "manhwa", "manhua", "novel", "novels", "scan", "scans"}:
            context = key
            break
    return "|".join(part for part in (context, parent, ext) if part)


def slskd_learning_payload():
    data = read_json(SLSKD_LEARNING_FILE, {}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("users", {})
    data.setdefault("path_styles", {})
    data.setdefault("extensions", {})
    return data


def update_learning_bucket(bucket, key, success, example):
    if not key:
        return
    row = bucket.setdefault(key, {})
    row["successes"] = int(row.get("successes") or 0)
    row["failures"] = int(row.get("failures") or 0)
    field = "successes" if success else "failures"
    row[field] += 1
    row["last_success_at" if success else "last_failure_at"] = now()
    row["last_success_at_iso" if success else "last_failure_at_iso"] = utc_stamp()
    examples = row.setdefault("examples", [])
    if not isinstance(examples, list):
        examples = []
    examples.insert(0, example)
    row["examples"] = examples[:10]


def record_slskd_learning(record, detected, success, reason, review_id):
    if not isinstance(record, dict):
        return
    username = normalize_key(record.get("username") or "")
    source = str(record.get("candidate_source") or "").strip()
    if not username and source != "slskd_probe":
        return
    candidate_filename = record.get("filename") or record.get("candidate_filename") or ""
    detected_path = (detected or {}).get("path") if isinstance(detected, dict) else ""
    filename = candidate_filename or detected_path
    path_style = slskd_learning_path_style(candidate_filename or detected_path)
    ext = extension_for(candidate_filename or detected_path)
    example = {
        "review_id": review_id,
        "series": record.get("series") or (detected or {}).get("series"),
        "issue": record.get("issue") or (detected or {}).get("issue"),
        "filename": filename_leaf(filename),
        "reason": reason,
        "success": bool(success),
        "ts": now(),
        "ts_iso": utc_stamp(),
    }
    data = slskd_learning_payload()
    update_learning_bucket(data["users"], username, success, example)
    update_learning_bucket(data["path_styles"], path_style, success, example)
    update_learning_bucket(data["extensions"], ext, success, example)
    write_json(SLSKD_LEARNING_FILE, data)
    log("slskd_learning_update", success=bool(success), reason=reason, review_id=review_id, username=username, path_style=path_style, extension=ext)


def record_slskd_learning_from_row(live, source, row, record, detected, review_id):
    if not live or source != "waiting" or not isinstance(row, dict):
        return
    status = str(row.get("status") or "")
    if status == "resolved":
        record_slskd_learning(record, detected, True, "verified import", review_id)
        return
    live_status = row.get("live") if isinstance(row.get("live"), dict) else {}
    note_key = str(live_status.get("note") or row.get("verification_pending_note") or "").lower()
    if status in {"imported_not_resolved", "verification_pending"} and (
        "waiting for kavita scan" in note_key
        or "waiting for kapowarr" in note_key
    ):
        return
    verification = row.get("verification") if isinstance(row.get("verification"), dict) else {}
    try:
        failure_count = int(live_status.get("verification_failure_count") or verification.get("failure_count") or 0)
    except (TypeError, ValueError):
        failure_count = 0
    if status in {"imported_not_resolved", "preview_not_importable"}:
        record_slskd_learning(record, detected, False, row.get("reason") or status, review_id)
    elif candidate_failure_is_actionable(status):
        record_slskd_learning(record, detected, False, row.get("reason") or status, review_id)
    elif status == "verification_pending" and failure_count > 0:
        record_slskd_learning(record, detected, False, row.get("reason") or "verification failed", review_id)


def manual_source_row_already_verified(row):
    if not isinstance(row, dict):
        return False
    live = row.get("live") if isinstance(row.get("live"), dict) else {}
    text = " ".join(
        str(value or "")
        for value in (
            row.get("verification_pending_note"),
            row.get("note"),
            row.get("reason"),
            live.get("note"),
            live.get("reason"),
        )
    ).lower()
    return (
        "already verified in kavita" in text
        or "already visible in kavita" in text
        or "already present and visible in kavita" in text
        or "canonical file is already visible in kavita" in text
    )


def classify_candidate_failure(reason):
    detail = str(reason or "").strip()
    lowered = detail.lower()
    status_key = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if not detail:
        return {"candidate_bad": False, "reason": "", "kind": "", "label": "", "detail": ""}
    non_failure_statuses = {
        "already_present",
        "already_present_clearable",
        "already_verified",
        "import_busy",
        "imported",
        "imported_not_resolved",
        "preview_already_present",
        "preview_importable",
        "ready_import",
        "resolved",
        "staged_file_ready",
        "transient_error",
        "transient_resolver_error",
        "verification_pending",
        "verified",
        "verified_clearable",
        "queue_satisfied",
        "queue_verified",
    }
    if status_key in non_failure_statuses:
        return {
            "candidate_bad": False,
            "reason": status_key,
            "kind": "non_failure_status",
            "label": "Non-failure import status",
            "detail": detail,
        }
    transient_markers = (
        "another import worker",
        "command failed: 75",
        "connection refused",
        "connection aborted",
        "connection reset",
        "could not read slskd transfer status",
        "database is busy",
        "database is locked",
        "database schema is locked",
        "database table is locked",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "import worker is already running",
        "import_busy",
        "lock busy",
        "resource temporarily unavailable",
        "remote end closed connection",
        "remotedisconnected",
        "requested url returned error: 500",
        "sqlite operationalerror",
        "sqlite3.operationalerror",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "transient_error",
        "transient resolver",
        "transient_resolver_error",
        "transfer lookup failed",
        "urlopen error",
    )
    if any(marker in lowered for marker in transient_markers):
        return {
            "candidate_bad": False,
            "reason": "transient_resolver_error",
            "kind": "transient",
            "label": "Temporary resolver problem",
            "detail": detail,
        }
    waiting_markers = (
        "waiting for kapowarr",
        "waiting for kavita scan",
        "waiting for import verification",
        "visible in kavita",
    )
    if any(marker in lowered for marker in waiting_markers):
        return {
            "candidate_bad": False,
            "reason": "verification_pending",
            "kind": "pending_verification",
            "label": "Waiting for verification",
            "detail": detail,
        }
    if "wrong language" in lowered or "non-english" in lowered or "language source" in lowered:
        reason_key, kind, label = "wrong_language_source", "language", "Wrong language source"
    elif "transfer stalled" in lowered or "zero progress" in lowered or "queued remotely" in lowered or "queued locally" in lowered:
        reason_key, kind, label = "slskd_transfer_stalled", "transfer", "SLSKD transfer stalled"
    elif "transfer disappeared" in lowered or "completed but no staged file" in lowered:
        reason_key, kind, label = "slskd_transfer_missing_staged_file", "transfer", "SLSKD transfer did not stage a file"
    elif "slskd transfer failed" in lowered or "transfer failed" in lowered:
        reason_key, kind, label = "slskd_transfer_failed", "transfer", "SLSKD transfer failed"
    elif "different kapowarr volume" in lowered or "identity_mismatch" in lowered:
        reason_key, kind, label = "identity_mismatch", "match", "Verified against a different Kapowarr volume"
    elif (
        "candidate filename" in lowered
        or "staged file did not match" in lowered
        or "does not match" in lowered
        or "different series" in lowered
        or "different titled series" in lowered
        or "related different series" in lowered
    ):
        reason_key, kind, label = "staged_file_mismatch", "match", "Staged file did not match candidate"
    elif "match score" in lowered or "match has penalties" in lowered or "missing title" in lowered or "missing issue" in lowered:
        reason_key, kind, label = "staged_file_low_confidence", "match", "Staged file match was too weak"
    elif (
        "bad archive" in lowered
        or "crc" in lowered
        or "corrupt" in lowered
        or "not a zip" in lowered
        or "not a rar" in lowered
        or "unpack" in lowered
        or "unsupported archive" in lowered
    ):
        reason_key, kind, label = "bad_archive", "archive", "Archive could not be read"
    elif "preview_not_importable" in lowered or "preview did not find" in lowered or "no files were imported" in lowered or "not importable" in lowered:
        reason_key, kind, label = "preview_not_importable", "import", "Preview found nothing importable"
    elif (
        "verification failed" in lowered
        or "verification failure" in lowered
        or "verification reported" in lowered
        or "blocked by" in lowered and "verification" in lowered
        or "not_clearable" in lowered
    ):
        reason_key, kind, label = "import_verification_failed", "verification", "Import verification failed"
    elif "error" in lowered or lowered.startswith("http 4") or lowered.startswith("http 5"):
        reason_key, kind, label = "resolver_error", "resolver", "Resolver errored on this candidate"
    else:
        reason_key, kind, label = "candidate_failed", "candidate", "Candidate failed"
    return {
        "candidate_bad": True,
        "reason": reason_key,
        "kind": kind,
        "label": label,
        "detail": detail,
    }


def manual_source_bad_candidate_key(record, detected):
    values = [
        str((record or {}).get("username") or "").strip().lower(),
        str((record or {}).get("filename") or "").replace("\\", "/").strip().lower(),
        filename_leaf((record or {}).get("filename") or "").lower(),
        str((detected or {}).get("path") or "").replace("\\", "/").strip().lower(),
        filename_leaf((detected or {}).get("path") or (detected or {}).get("filename") or "").lower(),
    ]
    raw = "|".join(value for value in values if value)
    return normalize_key(raw)


def mark_manual_source_candidate_bad(review_id, record, detected, reason, transfer=None):
    failure = classify_candidate_failure(reason)
    if not failure.get("candidate_bad"):
        log(
            "manual_source_candidate_bad_skipped",
            review_id=review_id,
            reason=failure.get("reason"),
            detail=failure.get("detail") or reason,
            filename=(record or {}).get("filename") or (record or {}).get("candidate_filename"),
            username=(record or {}).get("username"),
        )
        return None
    actions = read_json(ACTIONS_FILE, {}) or {}
    if not isinstance(actions, dict):
        actions = {}
    bad = actions.setdefault("manual_source_bad_candidates", {})
    if not isinstance(bad, dict):
        bad = {}
        actions["manual_source_bad_candidates"] = bad
    rows = bad.setdefault(str(review_id), [])
    if not isinstance(rows, list):
        rows = []
    transfer = transfer if isinstance(transfer, dict) else {}
    if not transfer and isinstance((record or {}).get("slskd_transfer"), dict):
        transfer = (record or {}).get("slskd_transfer") or {}
    entry = {
        "review_id": str(review_id),
        "series": (record or {}).get("series"),
        "issue": (record or {}).get("issue"),
        "username": (record or {}).get("username"),
        "filename": (record or {}).get("filename") or (record or {}).get("candidate_filename"),
        "filename_leaf": (record or {}).get("filename_leaf") or filename_leaf((record or {}).get("filename") or ""),
        "candidate_score": (record or {}).get("candidate_score"),
        "candidate_size": (record or {}).get("candidate_size"),
        "detected_path": (detected or {}).get("path") if isinstance(detected, dict) else None,
        "detected_filename": (detected or {}).get("filename") if isinstance(detected, dict) else None,
        "reason": failure.get("reason") or str(reason or ""),
        "failure_kind": failure.get("kind"),
        "failure_label": failure.get("label"),
        "detail": failure.get("detail") if failure.get("detail") != failure.get("reason") else "",
        "slskd_transfer_id": first_present((record or {}).get("slskd_transfer_id"), transfer.get("id")),
        "slskd_transfer_state": first_present(
            transfer.get("stateDescription"),
            transfer.get("state"),
            (record or {}).get("slskd_transfer_state"),
        ),
        "slskd_transfer_requested_at": first_present(
            transfer.get("requestedAt"),
            (record or {}).get("slskd_transfer_requested_at"),
        ),
        "ts": now(),
        "ts_iso": utc_stamp(),
    }
    for key in CONTEXT_FIELDS:
        value = (record or {}).get(key)
        if value not in (None, ""):
            entry[key] = value
    entry["candidate_key"] = manual_source_bad_candidate_key(record, detected)
    updated_existing = False
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("candidate_key") or "") != entry["candidate_key"]:
            continue
        merged = dict(row)
        merged.update({key: value for key, value in entry.items() if value not in (None, "")})
        rows[index] = merged
        updated_existing = True
        break
    if not updated_existing:
        rows.insert(0, entry)
    bad[str(review_id)] = rows[:20]

    waiting = actions.setdefault("manual_source_waiting", {})
    previous_waiting = None
    if isinstance(waiting, dict):
        previous_waiting = waiting.pop(str(review_id), None)
    history = actions.setdefault("manual_source_waiting_cleared", [])
    if not isinstance(history, list):
        history = []
    history.append({
        "review_id": str(review_id),
        "previous": previous_waiting,
        "reason": "autopick_candidate_failed",
        "detail": failure.get("detail") or reason,
        "ts": now(),
        "ts_iso": utc_stamp(),
    })
    actions["manual_source_waiting_cleared"] = history[-100:]
    save_actions(actions)
    log(
        "manual_source_candidate_bad",
        review_id=review_id,
        reason=entry.get("reason"),
        detail=entry.get("detail"),
        filename=entry.get("filename"),
        username=entry.get("username"),
    )
    auto_grab_audit(
        "candidate_failed",
        review_id=review_id,
        reason=entry.get("reason"),
        detail=entry.get("detail"),
        failure_kind=entry.get("failure_kind"),
        failure_label=entry.get("failure_label"),
        series=entry.get("series"),
        issue=entry.get("issue"),
        filename=entry.get("filename"),
        username=entry.get("username"),
        candidate_score=entry.get("candidate_score"),
        detected_path=entry.get("detected_path"),
        candidate_key=entry.get("candidate_key"),
    )
    if inkdrop_state is not None and INKDROP_STATE_DB.exists():
        try:
            durable = inkdrop_state.record_bad_source_candidate(
                INKDROP_STATE_DB,
                source="slskd",
                provider=entry.get("username") or "slskd",
                protocol="soulseek",
                series=entry.get("series"),
                title=entry.get("detected_filename") or entry.get("filename_leaf") or entry.get("filename"),
                source_path=entry.get("detected_path") or entry.get("filename"),
                reason=entry.get("reason") or "candidate_failed",
                raw=entry,
                seen_at=entry.get("ts"),
            )
            entry["durable_bad_source_candidate"] = durable
            log(
                "manual_source_durable_bad_candidate",
                review_id=review_id,
                ok=bool(isinstance(durable, dict) and durable.get("ok")),
                candidate_id=(durable or {}).get("candidate_id") if isinstance(durable, dict) else None,
                reason=entry.get("reason"),
            )
        except Exception as exc:
            entry["durable_bad_source_candidate"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            log(
                "manual_source_durable_bad_candidate_failed",
                review_id=review_id,
                reason=entry.get("reason"),
                error=entry["durable_bad_source_candidate"]["error"],
            )
    return entry


def run_next_slskd_autopick(args, record, review_id=None):
    series = str((record or {}).get("series") or (record or {}).get("query") or "").strip()
    if not series:
        return {"started": False, "reason": "missing series"}
    review_id = str(review_id or (record or {}).get("review_id") or "").strip()

    def run_probe(label, *, wait_seconds, max_queries, probe_budget_seconds, timeout_seconds):
        probe_cmd = [
            python_command(),
            str(args.probe_script),
            "--series",
            series,
            "--max-total",
            "1",
            "--max-per-series",
            "1",
            "--wait-seconds",
            str(wait_seconds),
            "--max-queries",
            str(max_queries),
            "--probe-budget-seconds",
            str(probe_budget_seconds),
            "--cooldown-hours",
            "24",
            "--force",
            "--auto-grab-live",
            "--auto-grab-max",
            "1",
        ]
        if review_id:
            probe_cmd.extend(["--review-id", review_id])
        cmd = [
            "/usr/bin/flock",
            "-E",
            "75",
            "-w",
            str(SLSKD_SOURCE_PROBE_LOCK_WAIT_SECONDS),
            str(SLSKD_SOURCE_PROBE_LOCK),
            *probe_cmd,
        ]
        probe_env = os.environ.copy()
        if SLSKD_BASE_URL:
            probe_env["INKDROP_SLSKD_API_BASE_URL"] = str(SLSKD_BASE_URL).strip()
        if SLSKD_CONFIG:
            probe_env["INKDROP_SLSKD_CONFIG"] = str(SLSKD_CONFIG)
        probe_env.setdefault("INKDROP_CONFIG_DIR", str(CONFIG_DIR))
        probe_env.setdefault("INKDROP_STATE_DIR", str(STATE_DIR))
        probe_env.setdefault("KAVITA_ACQUIRE_STATE_DIR", str(STATE_DIR))
        try:
            proc = run_process_group(cmd, timeout=timeout_seconds, env=probe_env)
        except subprocess.TimeoutExpired as exc:
            return {
                "started": False,
                "status": "timeout",
                "series": series,
                "review_id": review_id,
                "retry_scope": label,
                "error": f"SLSKD retry probe exceeded {exc.timeout:.0f}s and will be retried later.",
            }
        except Exception as exc:
            return {
                "started": False,
                "status": "error",
                "series": series,
                "review_id": review_id,
                "retry_scope": label,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if proc.returncode == 75:
            return {
                "started": False,
                "status": "busy",
                "returncode": proc.returncode,
                "series": series,
                "review_id": review_id,
                "retry_scope": label,
                "reason": "SLSKD probe is already running; the row remains eligible for the next retry pass.",
            }
        parsed = {}
        if proc.stdout.strip():
            try:
                parsed = json.loads(proc.stdout)
            except ValueError:
                parsed = {}
        auto = parsed.get("auto_grab") if isinstance(parsed, dict) else {}
        auto = auto if isinstance(auto, dict) else {}
        summary = {
            "started": int(auto.get("started_count") or 0) > 0,
            "status": "ok" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "series": series,
            "review_id": review_id,
            "retry_scope": label,
            "candidate_count": auto.get("candidate_count"),
            "selected_count": auto.get("selected_count"),
            "started_count": auto.get("started_count"),
            "bad_candidate_skipped_count": auto.get("bad_candidate_skipped_count"),
            "attempt_limit_skipped_count": auto.get("attempt_limit_skipped_count"),
        }
        if proc.returncode != 0:
            summary["stderr"] = proc.stderr[-1000:]
        return summary

    retry = run_probe(
        "same_review_quick",
        wait_seconds=8,
        max_queries=3,
        probe_budget_seconds=90,
        timeout_seconds=165,
    )
    if not retry_probe_exhausted(retry):
        return retry

    broadened = run_probe(
        "same_review_broadened",
        wait_seconds=10,
        max_queries=5,
        probe_budget_seconds=180,
        timeout_seconds=285,
    )
    broadened["broadened_retry"] = True
    broadened["previous_retry"] = retry
    return broadened


def retry_probe_should_remain_pending(retry):
    if not isinstance(retry, dict):
        return False
    if retry.get("started") or int(retry.get("started_count") or 0) > 0:
        return False
    status = str(retry.get("status") or "").strip()
    if status in {"busy", "timeout", "error", "deferred"}:
        return True
    return False


def retry_pending_blocked_by(retry):
    if not isinstance(retry, dict):
        return "manual_source_retry"
    status = str(retry.get("status") or "").strip().lower()
    if status == "busy":
        return "slskd_source_probe_lock"
    if status == "timeout":
        return "slskd_source_probe_timeout"
    if status == "error":
        return "slskd_source_probe_error"
    if status == "deferred":
        return "completed_work_reconciliation"
    return "manual_source_retry"


def retry_pending_remaining_seconds(record, now_ts=None):
    if not isinstance(record, dict):
        return 0
    now_ts = now() if now_ts is None else now_ts
    next_retry = numeric_ts(record.get("next_retry_after") or record.get("next_retry_at"))
    if next_retry:
        return max(0, int(next_retry - now_ts))
    last_retry = record.get("last_retry") if isinstance(record.get("last_retry"), dict) else {}
    if retry_probe_should_remain_pending(last_retry):
        ts = numeric_ts(record.get("ts"))
        if ts:
            return max(0, int((ts + SLSKD_RETRY_PENDING_COOLDOWN_SECONDS) - now_ts))
    return 0


def retry_probe_exhausted(retry):
    if not isinstance(retry, dict) or retry.get("started") or int(retry.get("started_count") or 0) > 0:
        return False
    if str(retry.get("status") or "") != "ok":
        return False
    try:
        candidate_count = int(retry.get("candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    try:
        selected_count = int(retry.get("selected_count") or 0)
    except (TypeError, ValueError):
        selected_count = 0
    return candidate_count <= 0 or selected_count <= 0


def retry_pending_records(actions):
    pending = actions.get("manual_source_retry_pending") if isinstance(actions, dict) else {}
    if not isinstance(pending, dict):
        return {}
    now_ts = now()
    out = {}
    for review_id, record in pending.items():
        if not review_id or not isinstance(record, dict):
            continue
        ts = numeric_ts(record.get("ts"))
        if ts and now_ts - ts > SLSKD_RETRY_PENDING_TTL_SECONDS:
            continue
        out[str(review_id)] = record
    return out


def save_retry_pending_record(review_id, record, bad_entry, retry, reason):
    review_id = str(review_id or "").strip()
    if not review_id or not isinstance(record, dict):
        return None
    actions = read_json(ACTIONS_FILE, {}) or {}
    if not isinstance(actions, dict):
        actions = {}
    pending = retry_pending_records(actions)
    now_ts = now()
    retry_status = str((retry or {}).get("status") or "").strip().lower() if isinstance(retry, dict) else ""
    cooldown_seconds = (
        SLSKD_RETRY_PENDING_COOLDOWN_SECONDS
        if retry_probe_should_remain_pending(retry) and retry_status != "deferred"
        else 0
    )
    next_retry_after = now_ts + cooldown_seconds if cooldown_seconds else now_ts
    entry = {
        "review_id": review_id,
        "series": record.get("series") or record.get("query"),
        "query": record.get("series") or record.get("query"),
        "issue": record.get("issue"),
        "reason": reason,
        "bad_candidate": bad_entry if isinstance(bad_entry, dict) else None,
        "last_retry": retry if isinstance(retry, dict) else None,
        "retry_count": int((pending.get(review_id) or {}).get("retry_count") or 0) + 1,
        "worker_owner": "manual_source_autoresolve",
        "blocked_by": retry_pending_blocked_by(retry),
        "cooldown_seconds": cooldown_seconds,
        "next_retry_after": next_retry_after,
        "next_retry_after_iso": utc_stamp(next_retry_after),
        "ts": now_ts,
        "ts_iso": utc_stamp(now_ts),
    }
    for field in CONTEXT_FIELDS:
        value = record.get(field)
        if value not in (None, ""):
            entry[field] = value
    pending[review_id] = entry
    ordered = sorted(pending.items(), key=lambda item: numeric_ts((item[1] or {}).get("ts")))
    actions["manual_source_retry_pending"] = dict(ordered[-SLSKD_RETRY_PENDING_MAX_ROWS:])
    save_actions(actions)
    log("manual_source_retry_pending_saved", review_id=review_id, reason=reason, retry=retry)
    return entry


def clear_retry_pending_record(review_id, reason, retry=None):
    review_id = str(review_id or "").strip()
    if not review_id:
        return None
    actions = read_json(ACTIONS_FILE, {}) or {}
    if not isinstance(actions, dict):
        return None
    pending = actions.get("manual_source_retry_pending") if isinstance(actions.get("manual_source_retry_pending"), dict) else {}
    removed = pending.pop(review_id, None)
    actions["manual_source_retry_pending"] = pending
    history = actions.setdefault("manual_source_retry_pending_cleared", [])
    if not isinstance(history, list):
        history = []
    if removed:
        history.append({
            "review_id": review_id,
            "reason": reason,
            "series": removed.get("series"),
            "issue": removed.get("issue"),
            "retry": retry if isinstance(retry, dict) else None,
            "ts": now(),
            "ts_iso": utc_stamp(),
        })
        actions["manual_source_retry_pending_cleared"] = history[-100:]
    save_actions(actions)
    if removed:
        log("manual_source_retry_pending_cleared", review_id=review_id, reason=reason, retry=retry)
    return removed


def retry_pending_status_row(review_id, record, retry, status, reason):
    row = waiting_status_row(
        review_id,
        record,
        reason,
        source="retry_pending",
        status=status,
    )
    row["recovery"] = {
        "bad_candidate": record.get("bad_candidate") if isinstance(record.get("bad_candidate"), dict) else None,
        "retry_probe": retry,
    }
    for field in ("worker_owner", "blocked_by", "next_retry_after", "next_retry_after_iso", "cooldown_seconds"):
        if isinstance(record, dict) and record.get(field) not in (None, ""):
            row[field] = record.get(field)
    return row


def process_pending_slskd_retries(args, result, actions, limit=2):
    if not args.live:
        return []
    pending = retry_pending_records(actions)
    if not pending:
        return []
    waiting = waiting_records(actions)
    resolved_ids = {
        str(row.get("review_id") or "")
        for row in actions.get("manual_source_resolved") or []
        if isinstance(row, dict) and row.get("review_id")
    } if isinstance(actions, dict) else set()
    rows = []
    now_ts = now()
    ordered_pending = sorted(
        pending.items(),
        key=lambda item: (
            1 if retry_pending_remaining_seconds(item[1], now_ts=now_ts) > 0 else 0,
            numeric_ts((item[1] or {}).get("next_retry_after")) or numeric_ts((item[1] or {}).get("ts")),
        ),
    )
    processed = 0
    for review_id, record in ordered_pending:
        if processed >= limit:
            break
        if durable_autopilot_queue_item_verified(record):
            clear_retry_pending_record(review_id, "durable_queue_verified")
            continue
        if review_id in waiting:
            clear_retry_pending_record(review_id, "already_waiting")
            continue
        if review_id in resolved_ids:
            clear_retry_pending_record(review_id, "already_resolved")
            continue
        remaining_seconds = retry_pending_remaining_seconds(record, now_ts=now_ts)
        if remaining_seconds > 0:
            result["retry_cooldown_count"] = int(result.get("retry_cooldown_count") or 0) + 1
            current_next = int(numeric_ts(record.get("next_retry_after") or record.get("next_retry_at")) or (now_ts + remaining_seconds))
            row = retry_pending_status_row(
                review_id,
                record,
                record.get("last_retry") if isinstance(record.get("last_retry"), dict) else {},
                "retry_cooling_down",
                f"next SLSKD retry is scheduled in {remaining_seconds}s",
            )
            row["retry_remaining_seconds"] = remaining_seconds
            row["next_retry_after"] = current_next
            row["next_retry_after_iso"] = utc_stamp(current_next)
            rows.append(row)
            processed += 1
            continue
        retry = run_next_slskd_autopick(args, record, review_id=review_id)
        result["retry_probe_count"] = int(result.get("retry_probe_count") or 0) + 1
        if retry.get("broadened_retry"):
            result["retry_broadened_count"] = int(result.get("retry_broadened_count") or 0) + 1
        started_count = int(retry.get("started_count") or 0)
        result["retry_started_count"] = int(result.get("retry_started_count") or 0) + started_count
        if retry.get("started") or started_count > 0:
            clear_retry_pending_record(review_id, "retry_started", retry=retry)
            row = retry_pending_status_row(
                review_id,
                record,
                retry,
                "retry_started_after_failure",
                "next SLSKD candidate started after previous failure",
            )
        elif retry_probe_should_remain_pending(retry):
            save_retry_pending_record(review_id, record, record.get("bad_candidate"), retry, record.get("reason") or "retry still pending")
            row = retry_pending_status_row(
                review_id,
                record,
                retry,
                "retry_pending",
                retry.get("reason") or retry.get("error") or "waiting to retry next SLSKD candidate",
            )
        elif retry_probe_exhausted(retry):
            clear_retry_pending_record(review_id, "retry_exhausted", retry=retry)
            row = retry_pending_status_row(
                review_id,
                record,
                retry,
                "retry_exhausted",
                "no alternate SLSKD candidate remained after previous failure",
            )
        else:
            clear_retry_pending_record(review_id, "retry_finished_without_start", retry=retry)
            row = retry_pending_status_row(
                review_id,
                record,
                retry,
                "retry_not_started",
                retry.get("reason") or "SLSKD retry finished without starting a candidate",
            )
        rows.append(row)
        processed += 1
    if rows:
        result["skipped"].extend(rows)
    return rows


def candidate_failure_is_actionable(reason):
    return bool(classify_candidate_failure(reason).get("candidate_bad"))


def actionable_import_skip_reason(row):
    if not isinstance(row, dict):
        return ""
    values = [
        row.get("detail"),
        row.get("skip_reason"),
        row.get("reason"),
        row.get("event"),
    ]
    for value in values:
        text = str(value or "").strip()
        if text and candidate_failure_is_actionable(text):
            return text
    action = str(row.get("action_needed") or "").strip().lower()
    skip_reason = str(row.get("skip_reason") or "").strip()
    if action == "retry_another_source" and skip_reason and candidate_failure_is_actionable(skip_reason):
        return skip_reason
    return ""


def import_result_actionable_skip_reason(import_result):
    if not isinstance(import_result, dict):
        return ""
    for row in import_result.get("skipped") or []:
        reason = actionable_import_skip_reason(row)
        if reason:
            return reason
    return ""


def import_response_actionable_skip_reason(response):
    result = (response or {}).get("result") if isinstance(response, dict) else {}
    result = result if isinstance(result, dict) else {}
    return import_result_actionable_skip_reason(result.get("import_result") or {})


def recovery_failure_reason(row):
    if not isinstance(row, dict):
        return ""
    status = str(row.get("status") or "").strip()
    direct_reason = str(row.get("reason") or "").strip()
    if direct_reason and candidate_failure_is_actionable(direct_reason):
        return direct_reason
    if candidate_failure_is_actionable(status):
        return status
    skipped_reason = import_result_actionable_skip_reason({"skipped": row.get("skipped") or []})
    if skipped_reason:
        return skipped_reason
    if status == "preview_not_importable":
        return status if candidate_failure_is_actionable(status) else ""
    if status == "error":
        reason = row.get("error") or "resolver error"
        return reason if candidate_failure_is_actionable(reason) else ""
    if status in {"imported_not_resolved", "verification_pending"}:
        live = row.get("live") if isinstance(row.get("live"), dict) else {}
        note = str(live.get("note") or row.get("verification_pending_note") or "").strip()
        if live.get("state") == "identity_mismatch":
            return note or "verification matched different Kapowarr volume"
        note_key = note.lower()
        if "waiting for kavita scan" in note_key or "waiting for kapowarr" in note_key:
            return ""
        verification = row.get("verification") if isinstance(row.get("verification"), dict) else {}
        try:
            failure_count = int(live.get("verification_failure_count") or verification.get("failure_count") or 0)
        except (TypeError, ValueError):
            failure_count = 0
        if failure_count > 0:
            reason = note or "verification failed"
            return reason if candidate_failure_is_actionable(reason) else ""
    return ""


def recover_failed_waiting_candidate(args, result, review_id, record, detected, reason, transfer=None):
    if not args.live:
        return None
    if not isinstance(record, dict) or not slskd_candidate_recovery_allowed(record, detected):
        return None
    failure = classify_candidate_failure(reason)
    if not failure.get("candidate_bad"):
        result["transient_failure_count"] = int(result.get("transient_failure_count") or 0) + 1
        log(
            "manual_source_candidate_recovery_deferred",
            review_id=review_id,
            reason=failure.get("reason"),
            detail=failure.get("detail") or reason,
        )
        return None
    try:
        probe = load_probe_module(args.probe_script)
        mark_terminal = getattr(probe, "record_auto_grab_terminal_attempt", None)
        if callable(mark_terminal):
            # The long-running series worker owns this lock while it searches.
            # Import reconciliation must not wait behind that provider work.
            # The durable bad-candidate and retry-pending records below retain
            # duplicate protection when this optional attempt summary is busy.
            mark_terminal(review_id, record, "transfer_failed", reason, blocking=False)
    except Exception as exc:
        log("manual_source_terminal_attempt_state_failed", review_id=review_id, error=str(exc))
    bad_entry = mark_manual_source_candidate_bad(review_id, record, detected, reason, transfer=transfer)
    cancel_result = cancel_failed_slskd_transfer(record, transfer)
    if cancel_result:
        result["cancelled_transfer_count"] = int(result.get("cancelled_transfer_count") or 0) + int(bool(cancel_result.get("cancelled")))
        log(
            "manual_source_failed_candidate_cancel",
            review_id=review_id,
            series=(record or {}).get("series"),
            issue=(record or {}).get("issue"),
            cancel=cancel_result,
        )
    if getattr(args, "_reconcile_existing_work_before_retry", False):
        retry = {
            "started": False,
            "status": "deferred",
            "series": record.get("series") or record.get("query"),
            "review_id": review_id,
            "reason": (
                "Replacement search deferred until completed transfers and staged files "
                "have been reconciled."
            ),
        }
        result["retry_deferred_count"] = int(result.get("retry_deferred_count") or 0) + 1
    else:
        retry = run_next_slskd_autopick(args, record, review_id=review_id)
        result["retry_probe_count"] = int(result.get("retry_probe_count") or 0) + 1
    result["bad_candidate_count"] = int(result.get("bad_candidate_count") or 0) + 1
    if retry.get("broadened_retry"):
        result["retry_broadened_count"] = int(result.get("retry_broadened_count") or 0) + 1
    result["retry_started_count"] = int(result.get("retry_started_count") or 0) + int(retry.get("started_count") or 0)
    if retry_probe_should_remain_pending(retry):
        saved_retry = save_retry_pending_record(review_id, record, bad_entry, retry, reason)
        if saved_retry:
            result["retry_pending_count"] = len(retry_pending_records(read_json(ACTIONS_FILE, {}) or {}))
    recovery = {"bad_candidate": bad_entry, "retry_probe": retry}
    if cancel_result:
        recovery["cancel_transfer"] = cancel_result
    log("manual_source_candidate_recovery", review_id=review_id, reason=reason, retry=retry)
    auto_grab_audit(
        "retry_after_failure",
        review_id=review_id,
        reason=reason,
        series=(record or {}).get("series"),
        issue=(record or {}).get("issue"),
        bad_candidate=bad_entry,
        retry_probe=retry,
    )
    return recovery


def parse_slskd_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    text = text.rstrip("Z")
    text = text.split(".", 1)[0]
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def transfer_recently_ended(row, seconds=TRANSFER_SETTLE_SECONDS):
    ended = parse_slskd_time((row or {}).get("endedAt"))
    return ended is not None and 0 <= now() - ended < seconds


def age_seconds_from_record(record):
    if not isinstance(record, dict):
        return 0
    for key in ("ts", "started_at", "requested_at"):
        try:
            ts = float(record.get(key) or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts > 0:
            return max(0, now() - ts)
    for key in ("ts_iso", "started_at_iso", "requested_at_iso"):
        ts = parse_slskd_time(record.get(key))
        if ts:
            return max(0, now() - ts)
    return 0


def transfer_age_seconds(transfer):
    if not isinstance(transfer, dict):
        return 0
    for key in ("requestedAt", "startedAt", "enqueuedAt"):
        ts = parse_slskd_time(transfer.get(key))
        if ts:
            return max(0, now() - ts)
    return 0


def compact_duration(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def transfer_zero_progress(transfer):
    if not isinstance(transfer, dict):
        return False
    if inkdrop_state is not None and hasattr(inkdrop_state, "slskd_download_task_zero_progress"):
        return inkdrop_state.slskd_download_task_zero_progress(transfer)
    observed_numeric_zero = False
    for key in ("bytesTransferred", "percentComplete", "averageSpeed"):
        value = transfer.get(key)
        if isinstance(value, bool):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            return False
        if numeric == 0:
            observed_numeric_zero = True
    return observed_numeric_zero


def transfer_has_progress(transfer):
    if not isinstance(transfer, dict):
        return False
    try:
        transferred = int(transfer.get("bytesTransferred") or 0)
    except (TypeError, ValueError):
        transferred = 0
    try:
        percent = float(transfer.get("percentComplete") or 0)
    except (TypeError, ValueError):
        percent = 0
    try:
        speed = int(transfer.get("averageSpeed") or 0)
    except (TypeError, ValueError):
        speed = 0
    return transferred > 0 or percent > 0 or speed > 0


def transfer_queued_remotely(transfer):
    if not isinstance(transfer, dict):
        return False
    state = f"{transfer.get('state') or ''} {transfer.get('stateDescription') or ''}".lower()
    return "queued" in state and "remotely" in state


def transfer_queued_locally(transfer):
    if not isinstance(transfer, dict):
        return False
    state = f"{transfer.get('state') or ''} {transfer.get('stateDescription') or ''}".lower()
    return "queued" in state and "locally" in state


def slskd_stall_policy(db_path=None):
    """Resolve the active zero-progress stall gate without changing queue waits."""
    enabled = True
    seconds = SLSKD_WAITING_ZERO_PROGRESS_STALE_SECONDS
    source = "environment" if str(os.environ.get("INKDROP_SLSKD_ZERO_PROGRESS_STALE_SECONDS") or "").strip() else "default"
    path = Path(db_path or INKDROP_STATE_DB)
    if inkdrop_state is not None and path.exists():
        try:
            with inkdrop_state.connect_read(path) as con:
                return inkdrop_state.slskd_active_stall_policy(con)
        except (OSError, TypeError, ValueError):
            pass
    seconds = max(SLSKD_STALL_MIN_MINUTES * 60, min(int(seconds), SLSKD_STALL_MAX_MINUTES * 60))
    return {
        "enabled": bool(enabled),
        "minutes": seconds // 60,
        "seconds": seconds,
        "source": source,
        "setting_key": SLSKD_STALL_SETTING_KEY,
        "minimum_minutes": SLSKD_STALL_MIN_MINUTES,
        "maximum_minutes": SLSKD_STALL_MAX_MINUTES,
        "applies_to": "active_zero_progress_transfers",
        "queued_waiting_excluded": True,
    }


def slskd_progress_candidate_identity(transfer):
    if not isinstance(transfer, dict):
        return None
    peer = normalize_key(transfer.get("username") or "")
    stable_id = normalize_key(transfer.get("candidate_identity") or "")
    if peer and stable_id:
        return peer, "candidate_identity", stable_id
    locator = str(transfer.get("remote_path") or transfer.get("path") or "").strip()
    if not locator:
        filename = str(transfer.get("filename") or "").strip()
        if "/" in filename or "\\" in filename:
            locator = filename
    normalized_locator = normalize_key(locator.replace("\\", "/"))
    if peer and normalized_locator:
        return peer, "full_locator", normalized_locator
    return None


def same_user_active_transfer_context(transfer, download_status):
    if not isinstance(transfer, dict) or not isinstance(download_status, dict):
        return {}
    username = str(transfer.get("username") or "").strip().lower()
    transfer_id = str(transfer.get("id") or "").strip().lower()
    if not username:
        return {}
    active = []
    same_candidate_active = []
    queued = 0
    for row in download_status.get("transfers") or []:
        if not isinstance(row, dict):
            continue
        if username != str(row.get("username") or "").strip().lower():
            continue
        if transfer_id and transfer_id == str(row.get("id") or "").strip().lower():
            continue
        status = transfer_state_status(row)
        if status != "transfer_in_progress":
            continue
        if transfer_queued_remotely(row) or transfer_queued_locally(row):
            queued += 1
            continue
        if transfer_has_progress(row):
            compact = compact_transfer(row)
            compact["status"] = status
            active.append(compact)
            current_identity = slskd_progress_candidate_identity(transfer)
            other_identity = slskd_progress_candidate_identity(row)
            if current_identity and current_identity == other_identity:
                same_candidate_active.append(compact)
    if not active and not queued:
        return {}
    return {
        "same_user_active_transfer_count": len(active),
        "same_user_queued_transfer_count": queued,
        "same_user_active_transfer": active[0] if active else None,
        "same_candidate_active_transfer_count": len(same_candidate_active),
        "same_candidate_active_transfer": same_candidate_active[0] if same_candidate_active else None,
    }


def queued_transfer_stale_seconds(record, transfer, *, local=False):
    same_candidate_progressing = int((transfer or {}).get("same_candidate_active_transfer_count") or 0) > 0
    if same_candidate_progressing:
        return SLSKD_WAITING_REMOTE_QUEUE_ACTIVE_USER_STALE_SECONDS
    fallback_count = viable_cached_slskd_fallback_count(record)
    if local:
        base = SLSKD_WAITING_LOCAL_QUEUE_STALE_SECONDS
        fallback = SLSKD_WAITING_LOCAL_QUEUE_FALLBACK_STALE_SECONDS
    else:
        base = SLSKD_WAITING_REMOTE_QUEUE_STALE_SECONDS
        fallback = SLSKD_WAITING_REMOTE_QUEUE_FALLBACK_STALE_SECONDS
    if fallback_count > 0:
        return min(base, fallback)
    return base


def stale_waiting_failure_reason(record, transfer=None, stall_policy=None):
    if str((record or {}).get("candidate_source") or "") != "slskd_probe":
        return ""
    wait_age = age_seconds_from_record(record)
    transfer = transfer if isinstance(transfer, dict) else None
    status = str((transfer or {}).get("status") or "")
    if not transfer:
        if wait_age >= SLSKD_WAITING_NO_TRANSFER_STALE_SECONDS:
            return f"SLSKD transfer disappeared before staging after {compact_duration(wait_age)}"
        return ""
    transfer_age = transfer_age_seconds(transfer) or wait_age
    age = max(wait_age, transfer_age)
    remote_queue_stale_seconds = queued_transfer_stale_seconds(record, transfer, local=False)
    if (
        status == "transfer_in_progress"
        and transfer_queued_remotely(transfer)
        and transfer_zero_progress(transfer)
        and age >= remote_queue_stale_seconds
    ):
        return f"SLSKD transfer queued remotely with no progress after {compact_duration(age)}"
    local_queue_stale_seconds = queued_transfer_stale_seconds(record, transfer, local=True)
    if (
        status == "transfer_in_progress"
        and transfer_queued_locally(transfer)
        and transfer_zero_progress(transfer)
        and age >= local_queue_stale_seconds
    ):
        return f"SLSKD transfer queued locally with no progress after {compact_duration(age)}"
    effective_stall = stall_policy if isinstance(stall_policy, dict) else slskd_stall_policy()
    if (
        status == "transfer_in_progress"
        and not transfer_queued_remotely(transfer)
        and not transfer_queued_locally(transfer)
        and transfer_zero_progress(transfer)
        and effective_stall.get("enabled") is not False
        and age >= int(effective_stall.get("seconds") or SLSKD_WAITING_ZERO_PROGRESS_STALE_SECONDS)
    ):
        return f"SLSKD transfer stalled with no progress after {compact_duration(age)}"
    if status == "transfer_unknown" and age >= SLSKD_WAITING_UNKNOWN_STALE_SECONDS:
        return f"SLSKD transfer state stayed unknown after {compact_duration(age)}"
    return ""


def slskd_api_key():
    try:
        text = SLSKD_CONFIG.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    match = re.search(r"^\s*key:\s*([^\s#]+)", text, flags=re.M)
    return match.group(1) if match else ""


def slskd_get_json(path):
    key = slskd_api_key()
    if not key:
        raise RuntimeError("slskd API key not found")
    request = urllib.request.Request(require_slskd_base_url() + path, headers={"X-API-Key": key})
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else None


def slskd_request_json(method, path, payload=None, timeout=20):
    key = slskd_api_key()
    if not key:
        raise RuntimeError("slskd API key not found")
    data = None
    headers = {"X-API-Key": key}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(require_slskd_base_url() + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def cancel_failed_slskd_transfer(record, transfer):
    transfer = transfer if isinstance(transfer, dict) else {}
    if not transfer or transfer.get("recorded_snapshot"):
        return {}
    status = str(transfer.get("status") or "")
    if status in {"transfer_succeeded", "transfer_settling"}:
        return {"cancelled": False, "reason": f"{status} is not cancelled"}
    username = str(transfer.get("username") or (record or {}).get("username") or "").strip()
    transfer_id = str(transfer.get("id") or (record or {}).get("slskd_transfer_id") or "").strip()
    if not username or not transfer_id:
        return {"cancelled": False, "reason": "missing username or transfer id"}
    path = f"/transfers/downloads/{quote(username, safe='')}/{quote(transfer_id, safe='')}"
    try:
        response = slskd_request_json("DELETE", path, timeout=15)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"cancelled": False, "status": "not_found", "reason": "transfer already absent"}
        return {"cancelled": False, "status": "error", "error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"cancelled": False, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
    log(
        "slskd_transfer_cancelled",
        username=username,
        transfer_id=transfer_id,
        filename=transfer.get("filename") or (record or {}).get("filename"),
        status=status,
    )
    return {
        "cancelled": True,
        "status": "cancelled",
        "username": username,
        "transfer_id": transfer_id,
        "filename": transfer.get("filename") or (record or {}).get("filename"),
        "response": response,
    }


def slskd_download_transfers():
    try:
        payload = slskd_get_json("/transfers/downloads") or []
    except Exception as exc:
        log("slskd_transfer_lookup_error", error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "transfers": []}
    transfers = []
    for user_group in payload if isinstance(payload, list) else []:
        username = str((user_group or {}).get("username") or "")
        for directory in (user_group or {}).get("directories") or []:
            for row in (directory or {}).get("files") or []:
                if not isinstance(row, dict):
                    continue
                transfer = dict(row)
                transfer.setdefault("username", username)
                transfer.setdefault("directory", (directory or {}).get("directory"))
                transfers.append(transfer)
    return {"ok": True, "transfers": transfers}


def compact_transfer(row):
    if not isinstance(row, dict):
        return {}
    return {
        "id": row.get("id"),
        "username": row.get("username"),
        "filename": row.get("filename"),
        # SLSKD reports the containing directory beside the file row. Keep it
        # through the compact status projection so a completed transfer can be
        # resolved at <download root>/<directory>/<filename>.
        "directory": row.get("directory"),
        "remote_path": row.get("remote_path") or row.get("path"),
        "state": row.get("state"),
        "stateDescription": row.get("stateDescription"),
        "bytesTransferred": row.get("bytesTransferred"),
        "bytesRemaining": row.get("bytesRemaining"),
        "percentComplete": row.get("percentComplete"),
        "averageSpeed": row.get("averageSpeed"),
        "attempts": row.get("attempts"),
        "requestedAt": row.get("requestedAt"),
        "endedAt": row.get("endedAt"),
    }


def transfer_state_status(row):
    state = f"{row.get('state') or ''} {row.get('stateDescription') or ''}".lower()
    # Mirrors inkdrop_slskd_source_probe.slskd_transfer_failed()'s token list.
    # A timed-out or stalled transfer still reports "Completed" in SLSKD's
    # state text, so without these it fell into the succeeded branch below
    # and was treated as a finished download with nothing to stage.
    if any(
        token in state
        for token in (
            "errored", "failed", "cancelled", "canceled", "aborted", "rejected", "denied",
            "timedout", "timed out", "timeout", "stalled",
        )
    ):
        return "transfer_failed"
    if "succeeded" in state or ("completed" in state and not any(token in state for token in ("error", "fail"))):
        if transfer_recently_ended(row):
            return "transfer_settling"
        return "transfer_succeeded"
    try:
        percent = float(row.get("percentComplete") or 0)
    except (TypeError, ValueError):
        percent = 0
    try:
        remaining = int(row.get("bytesRemaining") or 0)
    except (TypeError, ValueError):
        remaining = 0
    if remaining > 0 or 0 < percent < 100 or any(token in state for token in ("requested", "queued", "progress", "initial", "remotely", "locally")):
        return "transfer_in_progress"
    return "transfer_unknown"


def waiting_transfer_status(record, download_status=None):
    recorded_transfer = (record or {}).get("slskd_transfer") if isinstance((record or {}).get("slskd_transfer"), dict) else {}
    expected_id = str((record or {}).get("slskd_transfer_id") or recorded_transfer.get("id") or "").strip().lower()
    filename = str((record or {}).get("filename") or (record or {}).get("filename_leaf") or "").strip()
    if not filename and not expected_id:
        return None
    expected = filename_key(filename)
    if not expected and not expected_id:
        return None
    username = str((record or {}).get("username") or "").strip().lower()
    if download_status is None:
        download_status = slskd_download_transfers()
    if not download_status.get("ok"):
        return {
            "status": "transfer_lookup_error",
            "error": download_status.get("error") or "could not read slskd transfers",
        }
    matches = []
    for row in download_status.get("transfers") or []:
        if username and username != str(row.get("username") or "").strip().lower():
            continue
        if expected_id and str(row.get("id") or "").strip().lower() == expected_id:
            matches.append(row)
            continue
        if filename_key(row.get("filename")) == expected:
            matches.append(row)
    if not matches:
        recorded_status = transfer_state_status(recorded_transfer) if recorded_transfer else ""
        if recorded_status in {"transfer_failed", "transfer_succeeded"}:
            transfer = compact_transfer(recorded_transfer)
            transfer["status"] = recorded_status
            transfer["recorded_snapshot"] = True
            return transfer
        return None
    matches.sort(key=lambda row: str(row.get("requestedAt") or row.get("endedAt") or ""), reverse=True)
    row = matches[0]
    status = transfer_state_status(row)
    transfer = compact_transfer(row)
    transfer["status"] = status
    transfer.update(same_user_active_transfer_context(transfer, download_status))
    return transfer


def waiting_status_row(review_id, record, reason, *, source="waiting", status=None, transfer=None, detected=None, path=None, **extra):
    record = record if isinstance(record, dict) else {}
    row = {
        "review_id": str(review_id or ""),
        "source": source or "waiting",
        "series": record.get("series"),
        "issue": record.get("issue"),
        "reason": reason,
        "status": status,
        "filename": record.get("filename") or record.get("filename_leaf") or record.get("candidate_filename_leaf"),
        "username": record.get("username"),
        "candidate_source": record.get("candidate_source"),
        "candidate_score": record.get("candidate_score") or record.get("score"),
        "slskd_transfer_id": record.get("slskd_transfer_id"),
        "slskd_transfer_state": record.get("slskd_transfer_state"),
        "slskd_transfer_requested_at": record.get("slskd_transfer_requested_at"),
    }
    record_source = record.get("source")
    if record_source and record_source != row["source"]:
        row["record_source"] = record_source
    for key in CONTEXT_FIELDS:
        if key == "source":
            continue
        value = record.get(key)
        if value not in (None, ""):
            row[key] = value
    if path:
        row["path"] = path
    if isinstance(transfer, dict) and transfer:
        row["transfer"] = transfer
        row["transfer_status"] = transfer.get("status")
        row["slskd_transfer_id"] = row.get("slskd_transfer_id") or transfer.get("id")
        row["slskd_transfer_state"] = (
            transfer.get("stateDescription")
            or transfer.get("state")
            or row.get("slskd_transfer_state")
        )
        row["slskd_transfer_requested_at"] = transfer.get("requestedAt") or row.get("slskd_transfer_requested_at")
        for key in ("percentComplete", "bytesTransferred", "bytesRemaining", "averageSpeed"):
            if transfer.get(key) not in (None, ""):
                row[f"transfer_{key}"] = transfer.get(key)
    if isinstance(detected, dict) and detected:
        row["detected"] = {
            key: detected.get(key)
            for key in ("path", "filename", "score", "mtime", "size")
            if detected.get(key) not in (None, "")
        }
        if not row.get("path") and detected.get("path"):
            row["path"] = detected.get("path")
    for key, value in extra.items():
        if value not in (None, ""):
            row[key] = value
    return {
        key: value
        for key, value in row.items()
        if value not in (None, "", [], {})
    }


def cancel_superseded_slskd_transfer(result, review_id, record, row, detected=None):
    if not isinstance(result, dict) or not isinstance(record, dict) or not isinstance(row, dict):
        return {}
    if str(record.get("candidate_source") or "") != "slskd_probe":
        return {}
    transfer = waiting_transfer_status(record)
    if not transfer:
        return {}
    status = str(transfer.get("status") or "")
    if status in {"transfer_succeeded", "transfer_settling"}:
        return {"cancelled": False, "reason": f"{status} is already terminal enough to leave alone"}
    cancel = cancel_failed_slskd_transfer(record, transfer)
    if not cancel:
        return {}
    row["superseded_transfer_cancel"] = cancel
    result["cancelled_transfer_count"] = int(result.get("cancelled_transfer_count") or 0) + int(bool(cancel.get("cancelled")))
    log(
        "manual_source_superseded_transfer_cancel",
        review_id=review_id,
        series=(record or {}).get("series"),
        issue=(record or {}).get("issue"),
        imported_path=(detected or {}).get("path") if isinstance(detected, dict) else None,
        transfer=transfer,
        cancel=cancel,
    )
    auto_grab_audit(
        "superseded_transfer_cancel",
        review_id=review_id,
        series=(record or {}).get("series"),
        issue=(record or {}).get("issue"),
        imported_path=(detected or {}).get("path") if isinstance(detected, dict) else None,
        transfer_id=transfer.get("id"),
        transfer_status=status,
        cancel=cancel,
    )
    return cancel


def waiting_filename_match(row, record):
    expected_values = [
        str((record or {}).get("filename") or "").strip(),
        str((record or {}).get("filename_leaf") or "").strip(),
        str((record or {}).get("candidate_filename_leaf") or "").strip(),
    ]
    expected_values = [value for value in expected_values if value]
    if not expected_values:
        return True, "no waiting candidate filename recorded"
    actual_values = [row.get("filename"), row.get("path")]
    usable_expected = []
    for expected in expected_values:
        expected_key = filename_key(expected)
        if expected_key:
            usable_expected.append((expected, expected_key))
    if not usable_expected:
        return True, "waiting candidate filename was not usable"
    for expected, expected_key in usable_expected:
        for actual in actual_values:
            actual_key = filename_key(actual)
            if not actual_key:
                continue
            if actual_key == expected_key:
                return True, "matched waiting candidate filename"
            if len(expected_key) >= 8 and (expected_key in actual_key or actual_key in expected_key):
                return True, "matched waiting candidate filename"
    return False, f"detected file does not match marked waiting candidate filename: {filename_leaf(usable_expected[0][0])}"


def auto_import_quality(row, source, item=None, probe_module=None):
    source_identity_gate = inkdrop_artifact_acceptance.source_identity_acceptance(
        " ".join(
            str(value or "")
            for value in (
                (row or {}).get("path"),
                (row or {}).get("filename"),
                (item or {}).get("filename"),
                (item or {}).get("candidate_filename"),
                (item or {}).get("filename_leaf"),
                (item or {}).get("candidate_filename_leaf"),
            )
            if value
        ),
        item,
    )
    if not source_identity_gate.get("ok"):
        return False, source_identity_gate.get("reason") or "source identity rejected"
    if probe_module is not None and hasattr(probe_module, "source_language_blocker"):
        language_values = [
            (row or {}).get("path"),
            (row or {}).get("filename"),
            (item or {}).get("filename"),
            (item or {}).get("candidate_filename"),
            (item or {}).get("filename_leaf"),
            (item or {}).get("candidate_filename_leaf"),
        ]
        for value in language_values:
            if not value:
                continue
            blocker = probe_module.source_language_blocker(str(value))
            if blocker:
                return False, f"wrong language source: {blocker}"
    if probe_module is not None and hasattr(probe_module, "unexpected_series_subtitle_blocker"):
        blocker = probe_module.unexpected_series_subtitle_blocker(
            (row or {}).get("path") or (row or {}).get("filename") or "",
            item or {},
        )
        if blocker:
            return False, blocker
    try:
        score = int((row or {}).get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    reasons = [str(value) for value in ((row or {}).get("match_reasons") or []) if value]
    penalties = [str(value) for value in ((row or {}).get("match_penalties") or []) if value]
    has_issue_evidence = any(
        "issue/part token" in value
        or "issue range" in value
        or "book/volume token" in value
        or "issue title" in value
        for value in reasons
    )
    has_title_evidence = any("title words" in value for value in reasons)
    has_issue_title_evidence = any("issue title" in value for value in reasons)
    if source != "ready_detected":
        if score < 45:
            return False, f"match score {score} below waiting auto-import threshold"
        if penalties:
            return False, "match has penalties: " + "; ".join(penalties[:3])
        if not has_title_evidence:
            return False, "missing title match reason"
        if not has_issue_evidence:
            return False, "missing issue/part, volume, or issue-title evidence"
        return True, "waiting detected quality gate passed"
    ready_threshold = 45 if (has_title_evidence and has_issue_evidence) or has_issue_title_evidence else 60
    if score < ready_threshold:
        return False, f"match score {score} below ready auto-import threshold"
    if penalties:
        return False, "match has penalties: " + "; ".join(penalties[:3])
    if not has_title_evidence:
        return False, "missing title match reason"
    if not has_issue_evidence:
        return False, "missing issue/part, volume, or issue-title evidence"
    return True, "ready detected quality gate passed"


def compact_skip_reason(reason):
    reason = str(reason or "unknown").strip() or "unknown"
    lowered = reason.lower()
    if reason == "no detected staged file":
        return "no staged file detected yet"
    if lowered.startswith("file age "):
        return "detected file is still settling"
    if "candidate filename" in lowered:
        return "staged file does not match the marked waiting candidate"
    if reason == "slskd transfer failed":
        return "SLSKD transfer failed"
    if reason == "slskd transfer in progress":
        return "SLSKD transfer is still running"
    if "transfer stalled" in lowered or "queued remotely" in lowered or "queued locally" in lowered:
        return "SLSKD transfer stalled; trying the next candidate"
    if "disappeared before staging" in lowered:
        return "SLSKD transfer disappeared before staging"
    if "state stayed unknown" in lowered:
        return "SLSKD transfer stayed unknown"
    if reason == "slskd transfer settling":
        return "SLSKD transfer completed and is waiting for the staging scan"
    if reason == "slskd transfer completed but no staged file detected":
        return "SLSKD says the transfer completed, but InkDrop cannot see the staged file"
    if reason == "slskd transfer lookup failed":
        return "could not read SLSKD transfer status"
    if "match score" in lowered:
        return "match score is below the auto-import threshold"
    if "match has penalties" in lowered:
        return "match has title/issue penalties"
    if "missing title" in lowered:
        return "missing title evidence"
    if "missing issue" in lowered or "missing issue/part" in lowered:
        return "missing issue/part or volume evidence"
    return reason


def current_waiting_count():
    actions = read_json(ACTIONS_FILE, {}) or {}
    return len(waiting_records(actions))


def first_present(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def acquire_series_autopilot_lock():
    try:
        SERIES_AUTOPILOT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"lock_directory_failed:{type(exc).__name__}"
    try:
        handle = SERIES_AUTOPILOT_LOCK.open("a+b")
    except OSError as exc:
        return None, f"lock_open_failed:{type(exc).__name__}"
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        handle.close()
        return None, "series_autopilot_lock_busy"
    except OSError as exc:
        handle.close()
        if getattr(exc, "winerror", None) in {33, 36}:
            return None, "series_autopilot_lock_busy"
        return None, f"lock_failed:{type(exc).__name__}"
    return handle, ""


def release_series_autopilot_lock(handle):
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def numeric_ts(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def queue_sync_rows(result):
    rows = {"processed": [], "skipped": []}
    if not isinstance(result, dict):
        return rows
    for bucket in ("processed", "skipped"):
        for row in result.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            if row.get("autopilot_queue") or row.get("autopilot_queue_key"):
                rows[bucket].append(row)
    return rows


def persist_deferred_autopilot_queue_sync(result, reason):
    rows = queue_sync_rows(result)
    row_count = sum(len(rows[bucket]) for bucket in rows)
    if row_count <= 0:
        return {"status": "empty", "pending_count": 0}
    now_ts = now()
    try:
        data = read_json(MANUAL_SOURCE_QUEUE_SYNC_FILE, {}) or {}
        entries = data.get("items") if isinstance(data, dict) else []
        if not isinstance(entries, list):
            entries = []
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            created_at = numeric_ts(entry.get("created_at") or entry.get("ts"))
            if created_at and now_ts - created_at > DEFERRED_QUEUE_SYNC_TTL_SECONDS:
                continue
            kept.append(entry)
        sync_id = f"{int(now_ts * 1000)}-{os.getpid()}"
        native_attempt_replay = [
            dict(row)
            for row in (result.get("native_attempt_replay") or [])
            if isinstance(row, dict)
            and row.get("queue_id")
            and isinstance(row.get("attempt"), dict)
        ]
        entry = {
            "id": sync_id,
            "source": "manual_source_autoresolve",
            "reason": reason or "series_autopilot_lock_unavailable",
            "created_at": now_ts,
            "created_at_iso": utc_stamp(now_ts),
            "row_count": row_count,
            "result": {
                "state": result.get("state"),
                "status_label": result.get("status_label"),
                "processed": rows["processed"],
                "skipped": rows["skipped"],
                "native_attempt_replay": native_attempt_replay,
            },
        }
        kept.append(entry)
        kept = kept[-DEFERRED_QUEUE_SYNC_MAX_ITEMS:]
        payload = {
            "schema_version": 1,
            "updated_at": now_ts,
            "updated_at_iso": utc_stamp(now_ts),
            "items": kept,
        }
        write_json(MANUAL_SOURCE_QUEUE_SYNC_FILE, payload)
        db_sync = {"ok": False, "reason": "inkdrop_state_unavailable"}
        if inkdrop_state is not None:
            try:
                db_sync = inkdrop_state.record_deferred_queue_sync(
                    INKDROP_STATE_DB,
                    id=sync_id,
                    source=entry["source"],
                    reason=entry["reason"],
                    created_at=now_ts,
                    row_count=row_count,
                    result=entry["result"],
                )
            except Exception as exc:
                db_sync = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        log("manual_source_queue_sync_deferred", reason=reason, sync_id=sync_id, row_count=row_count, db_sync=db_sync)
        return {"status": "stored", "id": sync_id, "row_count": row_count, "pending_count": len(kept), "db_sync": db_sync}
    except Exception as exc:
        log("manual_source_queue_sync_defer_failed", reason=reason, error=f"{type(exc).__name__}: {exc}")
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "row_count": row_count}


def sync_autopilot_queue_from_result(result):
    lock_handle, lock_reason = acquire_series_autopilot_lock()
    if lock_handle is None:
        deferred = persist_deferred_autopilot_queue_sync(result, lock_reason)
        return [{
            "status": "queue_sync_deferred",
            "deferred": True,
            "reason": lock_reason or "series_autopilot_lock_unavailable",
            "lock": str(SERIES_AUTOPILOT_LOCK),
            "pending_sync": deferred,
        }]
    try:
        return sync_autopilot_queue_from_result_unlocked(result)
    finally:
        release_series_autopilot_lock(lock_handle)


def sync_autopilot_and_native_queue_from_result(result, reason="manual_source_autoresolve"):
    queue_updates = sync_autopilot_queue_from_result(result)
    if isinstance(result, dict):
        result["queue_updates"] = queue_updates
        native_attempts = record_native_autoresolve_attempts(result, reason=reason)
        if native_attempts.get("attempted"):
            result["inkdrop_state_source_attempts"] = native_attempts
        if native_attempts.get("recorded"):
            result["inkdrop_state_queue_export"] = export_inkdrop_queue_state(reason=f"{reason}_source_attempts")
    direct_updates = [
        update
        for update in (queue_updates or [])
        if isinstance(update, dict) and update.get("queue_key")
    ]
    if direct_updates and isinstance(result, dict) and not result.get("inkdrop_state_queue_export"):
        result["inkdrop_state_queue_sync"] = sync_inkdrop_queue_state(reason=reason)
    return queue_updates


def sync_autopilot_queue_from_result_unlocked(result):
    queue = read_json(SERIES_AUTOPILOT_QUEUE_FILE, {}) or {}
    items = queue.get("items") if isinstance(queue, dict) else {}
    if not isinstance(items, dict):
        return []
    updates = []
    now_ts = now()
    actions = read_json(ACTIONS_FILE, {}) or {}
    current_waiting = waiting_records(actions)

    def match_queue_key(row):
        direct = str((row or {}).get("autopilot_queue_key") or "").strip()
        if direct and direct in items:
            return direct
        series_key = normalize_key((row or {}).get("series") or (row or {}).get("query"))
        issue_key = normalize_key((row or {}).get("issue"))
        identity = str((row or {}).get("queue_identity") or "").strip()
        matches = []
        for key, item in items.items():
            if normalize_key((item or {}).get("series")) != series_key:
                continue
            if normalize_key((item or {}).get("issue")) != issue_key:
                continue
            if identity and str((item or {}).get("queue_identity") or "") != identity:
                continue
            matches.append(key)
        return matches[0] if len(matches) == 1 else ""

    def destinations_for(row):
        resolution = row.get("manual_source_resolution") if isinstance(row.get("manual_source_resolution"), dict) else {}
        destinations = resolution.get("destinations") if isinstance(resolution.get("destinations"), list) else []
        if destinations:
            return destinations
        live = row.get("live") if isinstance(row.get("live"), dict) else {}
        destinations = live.get("destinations") if isinstance(live.get("destinations"), list) else []
        if destinations:
            return destinations
        imported = row.get("imported") if isinstance(row.get("imported"), list) else []
        return [entry.get("dest") for entry in imported if isinstance(entry, dict) and entry.get("dest")]

    def clear_verified_slskd_activity(item):
        for key in SLSKD_ACTIVE_FIELDS:
            item.pop(key, None)
        if item.get("current_source") == "slskd":
            item["current_source"] = None

    def clear_failed_slskd_activity(item):
        for key in (
            "last_slskd_status",
            "last_slskd_candidate_count",
            "last_slskd_detected_count",
            "last_slskd_failed_candidate_count",
            "last_slskd_auto_grab_safe_count",
            "last_slskd_auto_grab_review_count",
            "last_slskd_auto_grab_blocked_count",
            "last_slskd_autopick_status",
            "last_slskd_waiting_review_id",
            "last_slskd_transfer_id",
            "last_slskd_transfer_state",
            "last_slskd_transfer_requested_at",
            "last_slskd_transfer_started_at",
            "last_slskd_transfer_ended_at",
            "last_slskd_transfer_percent",
            "last_slskd_transfer_bytes_transferred",
            "last_slskd_transfer_bytes_remaining",
            "last_slskd_transfer_average_speed",
            "last_slskd_transfer_attempts",
        ):
            item.pop(key, None)
        item["slskd_active_cleared_at"] = now_ts
        item["slskd_active_cleared_at_iso"] = utc_stamp(now_ts)

    def current_waiting_record_for(row, queue_key):
        review_id = str((row or {}).get("review_id") or "").strip()
        if review_id and isinstance(current_waiting.get(review_id), dict):
            return current_waiting.get(review_id)
        series_key = normalize_key((row or {}).get("series") or (row or {}).get("query"))
        issue_key = normalize_key((row or {}).get("issue"))
        identity = str((row or {}).get("queue_identity") or "").strip()
        for record in current_waiting.values():
            if not isinstance(record, dict):
                continue
            direct = str(record.get("autopilot_queue_key") or "").strip()
            if direct and direct == queue_key:
                return record
            if series_key and normalize_key(record.get("series") or record.get("query")) != series_key:
                continue
            if issue_key and normalize_key(record.get("issue")) != issue_key:
                continue
            if identity and str(record.get("queue_identity") or "") != identity:
                continue
            return record
        return None

    def waiting_supersedes_bad_candidate(row, queue_key, bad_candidate):
        if not isinstance(bad_candidate, dict):
            return None
        record = current_waiting_record_for(row, queue_key)
        if record and numeric_ts(record.get("ts")) >= numeric_ts(bad_candidate.get("ts")):
            return record
        return None

    def apply_waiting_record(item, record):
        transfer = record.get("slskd_transfer") if isinstance(record.get("slskd_transfer"), dict) else {}
        started_at = numeric_ts(record.get("ts")) or now()
        item["state"] = "downloading"
        item["current_source"] = "slskd"
        item["last_slskd_waiting_review_id"] = record.get("review_id") or item.get("last_slskd_waiting_review_id")
        if not item.get("download_started_at"):
            item["download_started_at"] = started_at
            item["download_started_at_iso"] = utc_stamp(started_at)
        item["last_download_started_at"] = started_at
        item["last_download_started_at_iso"] = utc_stamp(started_at)
        item["last_slskd_candidate"] = record.get("filename") or item.get("last_slskd_candidate")
        item["last_slskd_user"] = record.get("username") or item.get("last_slskd_user")
        item["last_slskd_score"] = record.get("score") or record.get("candidate_score") or item.get("last_slskd_score")
        item["last_slskd_transfer_id"] = (
            record.get("slskd_transfer_id")
            or transfer.get("id")
            or item.get("last_slskd_transfer_id")
        )
        item["last_slskd_transfer_state"] = (
            record.get("slskd_transfer_state")
            or transfer.get("state")
            or transfer.get("stateDescription")
            or item.get("last_slskd_transfer_state")
        )
        item["last_slskd_transfer_requested_at"] = (
            record.get("slskd_transfer_requested_at")
            or transfer.get("requestedAt")
            or item.get("last_slskd_transfer_requested_at")
        )
        if item.get("last_slskd_autoresolve_status") in {
            "candidate_failed",
            "retry_pending",
            "retry_exhausted",
            "retry_not_started",
        }:
            item.pop("last_slskd_autoresolve_status", None)
            item.pop("last_slskd_autoresolve_reason", None)
        item["last_event"] = "SLSKD candidate started; waiting for download"
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        item["updated_at"] = now()
        item["updated_at_iso"] = utc_stamp(item["updated_at"])

    def update_transfer_fields(item, row):
        transfer = row.get("transfer") if isinstance(row.get("transfer"), dict) else {}
        item["last_slskd_transfer_id"] = first_present(row.get("slskd_transfer_id"), transfer.get("id"), item.get("last_slskd_transfer_id"))
        item["last_slskd_transfer_state"] = first_present(
            row.get("slskd_transfer_state"),
            transfer.get("state"),
            transfer.get("stateDescription"),
            item.get("last_slskd_transfer_state"),
        )
        item["last_slskd_transfer_requested_at"] = first_present(
            row.get("slskd_transfer_requested_at"),
            transfer.get("requestedAt"),
            item.get("last_slskd_transfer_requested_at"),
        )
        for source_key, target_key in (
            ("transfer_percentComplete", "last_slskd_transfer_percent"),
            ("transfer_bytesTransferred", "last_slskd_transfer_bytes_transferred"),
            ("transfer_bytesRemaining", "last_slskd_transfer_bytes_remaining"),
            ("transfer_averageSpeed", "last_slskd_transfer_average_speed"),
        ):
            if row.get(source_key) not in (None, ""):
                item[target_key] = row.get(source_key)
        for source_key, target_key in (
            ("percentComplete", "last_slskd_transfer_percent"),
            ("bytesTransferred", "last_slskd_transfer_bytes_transferred"),
            ("bytesRemaining", "last_slskd_transfer_bytes_remaining"),
            ("averageSpeed", "last_slskd_transfer_average_speed"),
            ("attempts", "last_slskd_transfer_attempts"),
        ):
            if transfer.get(source_key) not in (None, ""):
                item[target_key] = transfer.get(source_key)

    for row in [*(result.get("processed") or []), *(result.get("skipped") or [])]:
        if not isinstance(row, dict):
            continue
        if not row.get("autopilot_queue") and not row.get("autopilot_queue_key"):
            continue
        queue_key = match_queue_key(row)
        item = items.get(queue_key) if queue_key else None
        if not isinstance(item, dict):
            continue
        status = str(row.get("status") or "").strip()
        if item.get("state") == "verified" and status not in {"resolved", "already_verified"} and not row.get("manual_source_resolved"):
            continue
        superseding_wait = current_waiting_record_for(row, queue_key)
        if superseding_wait and numeric_ts(superseding_wait.get("ts")) >= numeric_ts(row.get("ts")):
            apply_waiting_record(item, superseding_wait)
            updates.append({"queue_key": queue_key, "series": item.get("series"), "issue": item.get("issue"), "state": item.get("state"), "status": status})
            continue
        update_transfer_fields(item, row)
        item["last_slskd_autoresolve_status"] = status or row.get("transfer_status")
        item["last_slskd_autoresolve_at"] = now_ts
        item["last_slskd_autoresolve_at_iso"] = utc_stamp(now_ts)
        item["last_slskd_waiting_review_id"] = row.get("review_id") or item.get("last_slskd_waiting_review_id")
        item["last_slskd_candidate"] = row.get("filename") or item.get("last_slskd_candidate")
        item["last_slskd_user"] = row.get("username") or item.get("last_slskd_user")
        item["last_manual_source_status"] = status

        recovery = row.get("recovery") if isinstance(row.get("recovery"), dict) else {}
        already_verified_row = manual_source_row_already_verified(row)
        if status in {"resolved", "already_verified"} or row.get("manual_source_resolved") or already_verified_row:
            item["state"] = "verified"
            item["current_source"] = None
            item["completed_at"] = now_ts
            item["completed_at_iso"] = utc_stamp(now_ts)
            item["verified_at"] = now_ts
            item["verified_at_iso"] = utc_stamp(now_ts)
            item["last_event"] = "Library visibility verified imported file" if already_verified_row else "manual source import verified"
            item["last_import_status"] = "library_visible"
            destinations = destinations_for(row)
            if destinations:
                item["last_import_dest"] = destinations[0]
                item["imported_path"] = destinations[0]
            item.pop("retry_after", None)
            item.pop("retry_after_iso", None)
            item.pop("needs_you_reason", None)
            clear_verified_slskd_activity(item)
        elif recovery.get("bad_candidate"):
            if item.get("state") != "verified":
                retry = recovery.get("retry_probe") if isinstance(recovery.get("retry_probe"), dict) else {}
                retry_started = int(retry.get("started_count") or 0) > 0 or bool(retry.get("started"))
                retry_pending = (not retry_started) and retry_probe_should_remain_pending(retry)
                retry_exhausted = (not retry_started) and retry_probe_exhausted(retry)
                superseding_wait = waiting_supersedes_bad_candidate(row, queue_key, recovery.get("bad_candidate"))
                if superseding_wait:
                    apply_waiting_record(item, superseding_wait)
                    updates.append({"queue_key": queue_key, "series": item.get("series"), "issue": item.get("issue"), "state": item.get("state"), "status": status})
                    continue
                item["state"] = "downloading" if retry_started else "searching"
                item["current_source"] = "slskd" if retry_started else None
                if retry_started:
                    item["last_event"] = "failed SLSKD candidate marked bad; next candidate started"
                    retry_status = "retry_started_after_failure"
                elif retry_pending:
                    item["last_event"] = "SLSKD worker busy; retrying next candidate soon"
                    retry_status = "retry_pending"
                elif retry_exhausted:
                    item["last_event"] = "SLSKD candidates exhausted; continuing source ladder"
                    retry_status = "retry_exhausted"
                else:
                    item["last_event"] = "failed SLSKD candidate marked bad; retrying next candidate"
                    retry_status = "candidate_failed"
                retry_reason = retry.get("reason") or retry.get("error") or row.get("reason")
                item["last_slskd_autoresolve_status"] = retry_status
                item["last_slskd_autoresolve_reason"] = retry_reason
                item["last_slskd_autoresolve_at"] = now_ts
                item["last_slskd_autoresolve_at_iso"] = utc_stamp(now_ts)
                clear_failed_slskd_activity(item)
                item["last_slskd_autoresolve_status"] = retry_status
                item["last_slskd_autoresolve_reason"] = retry_reason
                item["last_slskd_autoresolve_at"] = now_ts
                item["last_slskd_autoresolve_at_iso"] = utc_stamp(now_ts)
                item.pop("retry_after", None)
                item.pop("retry_after_iso", None)
                item.pop("needs_you_reason", None)
        elif status in {"verification_pending", "imported_not_resolved"}:
            item["state"] = "importing"
            item["current_source"] = "slskd"
            item["last_event"] = library_frontend_note(row.get("verification_pending_note")) or "imported file is waiting for library scan retry"
            item["last_reconcile_state"] = status
            item.pop("retry_after", None)
            item.pop("retry_after_iso", None)
            item.pop("needs_you_reason", None)
        elif status == "import_busy":
            item["state"] = "importing"
            item["current_source"] = "slskd"
            item["last_event"] = "import worker busy; retrying verified import"
            item["last_reconcile_state"] = status
            item.pop("retry_after", None)
            item.pop("retry_after_iso", None)
            item.pop("needs_you_reason", None)
        elif status in {"transfer_in_progress", "transfer_settling", "waiting_for_staged_file", "transfer_unknown", "transfer_lookup_error", "staged_file_settling"}:
            if item.get("state") != "verified":
                item["state"] = "downloading"
                item["current_source"] = "slskd"
                transfer = row.get("transfer") if isinstance(row.get("transfer"), dict) else {}
                percent = row.get("transfer_percentComplete") or transfer.get("percentComplete")
                if percent not in (None, ""):
                    try:
                        item["last_event"] = f"SLSKD transfer {float(percent):.0f}% complete"
                    except (TypeError, ValueError):
                        item["last_event"] = "SLSKD transfer in progress; waiting for download"
                else:
                    item["last_event"] = row.get("reason") or "SLSKD transfer in progress; waiting for download"
                item.pop("retry_after", None)
                item.pop("retry_after_iso", None)
                item.pop("needs_you_reason", None)
        elif status in {"transfer_failed", "transfer_stalled", "transfer_succeeded_missing_stage", "transfer_stale_unknown", "transfer_missing_stale"}:
            if item.get("state") != "verified":
                item["state"] = "searching"
                item["current_source"] = "slskd"
                item["last_event"] = row.get("reason") or "SLSKD transfer failed; retrying next candidate"
                item.pop("needs_you_reason", None)
        elif status == "transient_error":
            if item.get("state") != "verified":
                item["state"] = "downloading"
                item["current_source"] = "slskd"
                item["last_event"] = "manual source resolver temporary error; retrying"
                item.pop("retry_after", None)
                item.pop("retry_after_iso", None)
                item.pop("needs_you_reason", None)
        updates.append({"queue_key": queue_key, "series": item.get("series"), "issue": item.get("issue"), "state": item.get("state"), "status": status})
    if updates:
        write_json(SERIES_AUTOPILOT_QUEUE_FILE, queue)
    return updates


def finish_result_summary(result):
    skipped = [row for row in (result.get("skipped") or []) if isinstance(row, dict)]
    processed = [row for row in (result.get("processed") or []) if isinstance(row, dict)]
    skip_reason_counts = {}
    for row in skipped:
        reason = str(row.get("reason") or "unknown").strip() or "unknown"
        skip_reason_counts[reason] = skip_reason_counts.get(reason, 0) + 1
    processed_status_counts = {}
    for row in processed:
        status = str(row.get("status") or "unknown").strip() or "unknown"
        processed_status_counts[status] = processed_status_counts.get(status, 0) + 1
    skipped_status_counts = {}
    for row in skipped:
        status = str(row.get("status") or "").strip()
        if not status:
            continue
        skipped_status_counts[status] = skipped_status_counts.get(status, 0) + 1

    result["skipped_count"] = len(skipped)
    result["processed_count"] = len(processed)
    result["skip_reason_counts"] = dict(sorted(skip_reason_counts.items(), key=lambda item: (-item[1], item[0])))
    result["processed_status_counts"] = dict(sorted(processed_status_counts.items(), key=lambda item: (-item[1], item[0])))
    result["skipped_status_counts"] = dict(sorted(skipped_status_counts.items(), key=lambda item: (-item[1], item[0])))

    top_skip_reason = ""
    top_skip_reason_count = 0
    if skip_reason_counts:
        top_skip_reason, top_skip_reason_count = sorted(skip_reason_counts.items(), key=lambda item: (-item[1], item[0]))[0]
    result["top_skip_reason"] = top_skip_reason
    result["top_skip_reason_count"] = top_skip_reason_count

    result["waiting_count"] = current_waiting_count()
    waiting_count = int(result.get("waiting_count") or 0)
    ready_count = int(result.get("ready_detected_count") or 0)
    eligible_count = int(result.get("eligible_count") or 0)
    previewed_count = int(result.get("previewed_count") or 0)
    imported_count = int(result.get("imported_count") or 0)
    resolved_count = int(result.get("resolved_count") or 0)
    bad_candidate_count = int(result.get("bad_candidate_count") or 0)
    cancelled_transfer_count = int(result.get("cancelled_transfer_count") or 0)
    retry_probe_count = int(result.get("retry_probe_count") or 0)
    retry_broadened_count = int(result.get("retry_broadened_count") or 0)
    retry_started_count = int(result.get("retry_started_count") or 0)
    retry_pending_count = int(result.get("retry_pending_count") or 0)
    retry_cooldown_count = max(int(result.get("retry_cooldown_count") or 0), skipped_status_counts.get("retry_cooling_down", 0))
    retry_exhausted_count = skipped_status_counts.get("retry_exhausted", 0) + processed_status_counts.get("retry_exhausted", 0)
    preview_importable_count = processed_status_counts.get("preview_importable", 0)
    verification_pending_count = processed_status_counts.get("verification_pending", 0)
    import_busy_count = processed_status_counts.get("import_busy", 0)
    transient_error_count = processed_status_counts.get("transient_error", 0)
    retry_busy_count = 0
    for row in [*processed, *skipped]:
        recovery = row.get("recovery") if isinstance(row.get("recovery"), dict) else {}
        retry_probe = recovery.get("retry_probe") if isinstance(recovery.get("retry_probe"), dict) else {}
        if retry_probe.get("status") == "busy":
            retry_busy_count += 1
    remote_queue_count = 0
    local_queue_count = 0
    zero_progress_count = 0
    remote_queue_retry_seconds = 0
    for row in skipped:
        transfer = row.get("transfer") if isinstance(row.get("transfer"), dict) else {}
        if not transfer or str(row.get("status") or "") != "transfer_in_progress":
            continue
        if transfer_zero_progress(transfer):
            zero_progress_count += 1
        if transfer_queued_remotely(transfer):
            remote_queue_count += 1
            stale_seconds = queued_transfer_stale_seconds(row, transfer, local=False)
            remaining = max(0, int(stale_seconds - (transfer_age_seconds(transfer) or 0)))
            if remaining and (remote_queue_retry_seconds <= 0 or remaining < remote_queue_retry_seconds):
                remote_queue_retry_seconds = remaining
        elif transfer_queued_locally(transfer):
            local_queue_count += 1

    if result.get("ok") is False:
        state = "error"
        label = "SLSKD import resolver error"
        next_action = "Open the resolver log before trusting automated SLSKD imports."
    elif resolved_count:
        state = "resolved"
        label = "SLSKD import resolved"
        next_action = f"{resolved_count} row{' was' if resolved_count == 1 else 's were'} resolved by import verification or already-present proof."
    elif retry_started_count:
        state = "watching"
        label = "Trying next SLSKD candidate"
        cancel_text = (
            f" {cancelled_transfer_count} stale transfer{' was' if cancelled_transfer_count == 1 else 's were'} cancelled."
            if cancelled_transfer_count
            else ""
        )
        if bad_candidate_count:
            next_action = (
                f"{bad_candidate_count} failed candidate{' was' if bad_candidate_count == 1 else 's were'} marked bad; "
                f"{retry_started_count} next-best SLSKD download{' was' if retry_started_count == 1 else 's were'} started."
                f"{cancel_text}"
            )
        else:
            retry_word = "retry was" if retry_started_count == 1 else "retries were"
            next_action = (
                f"{retry_started_count} pending SLSKD {retry_word} "
                f"started with the next-best candidate.{cancel_text}"
            )
    elif bad_candidate_count and retry_busy_count:
        state = "watching"
        label = "Waiting to retry SLSKD"
        next_action = (
            f"{bad_candidate_count} failed candidate{' was' if bad_candidate_count == 1 else 's were'} marked bad. "
            f"SLSKD probe {'is' if retry_busy_count == 1 else 'workers are'} busy; "
            "the next automation pass will try the next best candidate."
        )
    elif retry_cooldown_count:
        state = "watching"
        label = "SLSKD retry cooling down"
        retry_word = "retry is" if retry_cooldown_count == 1 else "retries are"
        next_action = (
            f"{retry_cooldown_count} failed SLSKD candidate {retry_word} parked behind a retry cooldown. "
            "InkDrop will try the next best candidate automatically when the cooldown expires."
        )
    elif retry_busy_count or retry_pending_count:
        state = "watching"
        label = "Waiting to retry SLSKD"
        count = retry_busy_count or retry_pending_count
        retry_word = "retry is" if count == 1 else "retries are"
        next_action = (
            f"{count} failed SLSKD candidate {retry_word} pending because "
            "SLSKD probing is busy or still settling. InkDrop will keep trying the next best candidate automatically."
        )
    elif retry_exhausted_count:
        state = "watching"
        label = "SLSKD candidates exhausted"
        broadened_text = (
            f" after {retry_broadened_count} widened SLSKD recheck"
            f"{'' if retry_broadened_count == 1 else 's'}"
            if retry_broadened_count
            else ""
        )
        next_action = (
            f"{retry_exhausted_count} SLSKD row"
            f"{' has' if retry_exhausted_count == 1 else 's have'} no alternate candidate{broadened_text} after the failed download. "
            "InkDrop is returning the row to the normal source ladder and scheduled retry pool."
        )
    elif bad_candidate_count and retry_probe_count:
        state = "watching"
        label = "Retrying SLSKD candidates"
        cancel_text = (
            f" {cancelled_transfer_count} stale transfer{' was' if cancelled_transfer_count == 1 else 's were'} cancelled."
            if cancelled_transfer_count
            else ""
        )
        next_action = (
            f"{bad_candidate_count} failed candidate{' was' if bad_candidate_count == 1 else 's were'} marked bad. "
            "InkDrop will retry SLSKD on the next pass if another safe candidate remains."
            f"{cancel_text}"
        )
    elif bad_candidate_count:
        state = "blocked"
        label = "SLSKD candidates exhausted or waiting"
        next_action = (
            f"{bad_candidate_count} failed candidate{' was' if bad_candidate_count == 1 else 's were'} marked bad. "
            "No next candidate started; the row remains in Action Queue if all candidates are exhausted."
        )
    elif import_busy_count:
        state = "watching"
        label = "Import worker busy"
        next_action = (
            f"{import_busy_count} staged SLSKD file{' is' if import_busy_count == 1 else 's are'} previewed and ready. "
            "The live import lock is busy; the next automation pass will retry without asking you."
        )
    elif transient_error_count:
        state = "watching"
        label = "Temporary import problem"
        next_action = (
            f"{transient_error_count} staged SLSKD file{' hit' if transient_error_count == 1 else 's hit'} "
            "a temporary resolver/import problem. InkDrop will retry the same file on the next automation pass."
        )
    elif verification_pending_count:
        state = "watching"
        label = "Waiting for library scan"
        note = ""
        for row in processed:
            if row.get("status") == "verification_pending":
                note = library_frontend_note(row.get("verification_pending_note"))
                if note:
                    break
        if "kavita scan" in note.lower() or "library scan" in note.lower():
            next_action = library_frontend_note(note)
        else:
            label = "SLSKD import verification pending"
            next_action = library_frontend_note(note) or "A copied SLSKD/manual file is waiting for import verification to clear."
    elif imported_count:
        unresolved = processed_status_counts.get("imported_not_resolved", 0)
        if unresolved:
            state = "watching"
            label = "SLSKD import verification pending"
            note = ""
            for row in processed:
                if row.get("status") == "imported_not_resolved":
                    live = row.get("live") if isinstance(row.get("live"), dict) else {}
                    note = str(live.get("note") or "").strip()
                    if note:
                        break
            next_action = note or "A file was copied, but verification did not clear the row yet."
        else:
            state = "imported"
            label = "SLSKD import copied"
            next_action = "Review any row that did not clear after import verification."
    elif previewed_count:
        if preview_importable_count:
            state = "previewed"
            label = "SLSKD staged file previewed"
            next_action = "Rows that previewed as importable can be imported through the verified endpoint."
        else:
            state = "blocked"
            label = "SLSKD staged file preview blocked"
            next_action = "A staged file was detected, but the verified preview rejected it. Open the row before importing."
    elif eligible_count:
        state = "checking"
        label = "Auto-resolver checking staged files"
        next_action = "InkDrop is previewing eligible staged files before any live import."
    elif processed:
        state = "blocked"
        label = "SLSKD import watcher needs attention"
        next_action = "A staged file reached the resolver but did not clear preview/import verification. Open the row before importing."
    elif waiting_count or ready_count:
        waiting_parts = []
        if waiting_count:
            waiting_parts.append(f"{waiting_count} waiting")
        if ready_count:
            waiting_parts.append(f"{ready_count} ready")
        label = "Watching SLSKD transfers"
        in_progress_count = skip_reason_counts.get("slskd transfer in progress", 0)
        transfer_settling_count = skip_reason_counts.get("slskd transfer settling", 0)
        file_settling_count = sum(
            count
            for reason, count in skip_reason_counts.items()
            if reason.startswith("file age ") and " below " in reason
        )
        no_stage_count = skip_reason_counts.get("no detected staged file", 0)
        normal_wait_count = in_progress_count + transfer_settling_count + file_settling_count + no_stage_count
        if ready_count and not waiting_count and skipped and no_stage_count == len(skipped):
            state = "idle"
            label = "SLSKD transfer watcher idle"
            next_action = (
                "No current staged SLSKD file needs action. "
                "InkDrop will keep rechecking sources automatically."
            )
        elif skipped and normal_wait_count == len(skipped):
            state = "watching"
            if remote_queue_count and remote_queue_count == in_progress_count:
                label = "Waiting on Soulseek queue"
            elif local_queue_count and local_queue_count == in_progress_count:
                label = "Waiting on SLSKD queue"
            elif in_progress_count:
                label = "SLSKD download running"
            elif transfer_settling_count:
                label = "SLSKD download settling"
            elif file_settling_count:
                label = "Staged file settling"
            parts = []
            if in_progress_count:
                actively_downloading_count = max(0, in_progress_count - remote_queue_count - local_queue_count)
                if remote_queue_count:
                    retry_text = (
                        f"; stale zero-progress queues retry in about {compact_duration(remote_queue_retry_seconds)}"
                        if remote_queue_retry_seconds
                        else "; stale zero-progress queues retry automatically"
                    )
                    parts.append(
                        f"{remote_queue_count} SLSKD transfer"
                        f"{' is' if remote_queue_count == 1 else 's are'} queued by the remote Soulseek user{retry_text}"
                    )
                if local_queue_count:
                    parts.append(
                        f"{local_queue_count} SLSKD transfer"
                        f"{' is' if local_queue_count == 1 else 's are'} waiting in the local SLSKD queue"
                    )
                if actively_downloading_count:
                    parts.append(
                        f"{actively_downloading_count} SLSKD transfer"
                        f"{' is' if actively_downloading_count == 1 else 's are'} still downloading"
                    )
            if transfer_settling_count:
                parts.append(
                    f"{transfer_settling_count} SLSKD transfer"
                    f"{' is' if transfer_settling_count == 1 else 's are'} settling"
                )
            if file_settling_count:
                parts.append(
                    f"{file_settling_count} staged file"
                    f"{' is' if file_settling_count == 1 else 's are'} inside the settle window"
                )
            if no_stage_count:
                parts.append(
                    f"{no_stage_count} row"
                    f"{' has' if no_stage_count == 1 else 's have'} no staged file yet"
                )
            next_action = (
                f"{'; '.join(parts)}. "
                "InkDrop will preview and import automatically after the staged file appears and verifies."
            )
        elif top_skip_reason:
            reason_text = compact_skip_reason(top_skip_reason)
            if top_skip_reason == "no detected staged file" and top_skip_reason_count == len(skipped):
                state = "watching"
                next_action = (
                    f"{'; '.join(waiting_parts)}; no staged file has appeared yet. "
                    "InkDrop will keep watching SLSKD staging and the manual comics inbox."
                )
            elif top_skip_reason == "slskd transfer in progress" and top_skip_reason_count == len(skipped):
                state = "watching"
                if remote_queue_count == top_skip_reason_count and zero_progress_count:
                    label = "Waiting on Soulseek queue"
                    retry_text = (
                        f" Stale zero-progress queues retry in about {compact_duration(remote_queue_retry_seconds)}."
                        if remote_queue_retry_seconds
                        else " Stale zero-progress queues retry automatically."
                    )
                    next_action = (
                        f"{top_skip_reason_count} SLSKD transfer"
                        f"{' is' if top_skip_reason_count == 1 else 's are'} queued by the remote Soulseek user."
                        f"{retry_text} InkDrop will preview and import only after the staged file appears and verifies."
                    )
                elif local_queue_count == top_skip_reason_count and zero_progress_count:
                    label = "Waiting on SLSKD queue"
                    next_action = (
                        f"{top_skip_reason_count} SLSKD transfer"
                        f"{' is' if top_skip_reason_count == 1 else 's are'} waiting in the local SLSKD queue. "
                        "InkDrop will retry stale zero-progress transfers automatically."
                    )
                else:
                    label = "SLSKD download running"
                    next_action = (
                        f"{top_skip_reason_count} SLSKD transfer"
                        f"{' is' if top_skip_reason_count == 1 else 's are'} still downloading. "
                        "InkDrop will preview and import only after the staged file appears and verifies."
                    )
            elif top_skip_reason == "slskd transfer settling" and top_skip_reason_count == len(skipped):
                state = "watching"
                label = "SLSKD download settling"
                next_action = (
                    f"{top_skip_reason_count} manual SLSKD transfer"
                    f"{' just finished' if top_skip_reason_count == 1 else 's just finished'}. "
                    "InkDrop is waiting for the staged file to settle before preview/import."
                )
            elif top_skip_reason.startswith("file age ") and " below " in top_skip_reason and top_skip_reason_count == len(skipped):
                state = "watching"
                label = "Staged file settling"
                next_action = (
                    f"{top_skip_reason_count} staged file"
                    f"{' is' if top_skip_reason_count == 1 else 's are'} present but still settling. "
                    "InkDrop will preview and import automatically after the settle window."
                )
            else:
                state = "blocked"
                label = "SLSKD import watcher needs attention"
                next_action = f"{top_skip_reason_count} row{' is' if top_skip_reason_count == 1 else 's are'} blocked: {reason_text}."
        else:
            state = "watching"
            next_action = f"{'; '.join(waiting_parts)}; waiting for a staged file to appear."
    else:
        state = "idle"
        label = "SLSKD transfer watcher idle"
        next_action = "No action needed. Autopilot marks SLSKD downloads to watch, then imports verified staged files automatically."

    result["state"] = state
    result["status_label"] = label
    result["next_action"] = next_action
    result["updated_at"] = now()
    result["updated_at_iso"] = utc_stamp(result["updated_at"])
    record_worker_activity_snapshot(result)
    return result


def publish_progress(result, state, label, next_action, **extra):
    snapshot = dict(result or {})
    snapshot.update(extra)
    snapshot["state"] = state
    snapshot["status_label"] = label
    snapshot["next_action"] = next_action
    snapshot["updated_at"] = now()
    snapshot["updated_at_iso"] = utc_stamp(snapshot["updated_at"])
    write_json(STATUS_FILE, snapshot)
    record_worker_activity_snapshot(snapshot)
    return snapshot


def record_worker_activity_snapshot(snapshot):
    if inkdrop_state is None:
        return None
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    try:
        return inkdrop_state.record_worker_activity(
            INKDROP_STATE_DB,
            "manual_source_autoresolve",
            {
                "lane": "manual_source",
                "source": "slskd",
                "state": snapshot.get("state") or "running",
                "status_label": snapshot.get("status_label") or "Manual Source resolver",
                "next_action": snapshot.get("next_action") or "",
                "pid": os.getpid(),
                "lock_path": str(LOCK_DIR / "inkdrop-manual-source-autoresolve.lock"),
                "started_at": snapshot.get("generated_at"),
                "heartbeat_at": snapshot.get("updated_at") or now(),
                "ttl_seconds": 20 * 60,
                "waiting_count": snapshot.get("waiting_count"),
                "ready_detected_count": snapshot.get("ready_detected_count"),
                "retry_pending_count": snapshot.get("retry_pending_count"),
                "retry_cooldown_count": snapshot.get("retry_cooldown_count"),
                "eligible_count": snapshot.get("eligible_count"),
                "processed_count": snapshot.get("processed_count"),
                "skipped_count": snapshot.get("skipped_count"),
            },
        )
    except Exception as exc:
        log("worker_activity_snapshot_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def post_import_detected(api_url, review_id, path, dry_run):
    request_payload = {
        "review_id": review_id,
        "path": str(path),
        "dryRun": bool(dry_run),
    }
    explicit_http_boundary = bool(
        str(os.environ.get("INKDROP_MANUAL_SOURCE_IMPORT_API_URL") or "").strip()
        or str(os.environ.get("INKDROP_WEB_BASE_URL") or "").strip()
        or inkdrop_runtime_config.worker_api_key()
        or str(api_url or "").strip().rstrip("/") != DEFAULT_MANUAL_SOURCE_IMPORT_API_URL.rstrip("/")
    )
    if not explicit_http_boundary:
        return inkdrop_internal_jobs.run_manual_source_import(request_payload)
    payload = json.dumps(request_payload).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            **inkdrop_runtime_config.worker_auth_headers(required=True),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1800 if not dry_run else 300) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


def importable_preview(response):
    result = (response or {}).get("result") or {}
    status = result.get("manual_source_resolution_status") or {}
    return status.get("state") in {"preview_importable", "preview_already_present"}


def pending_or_consumed_preview(response):
    result = (response or {}).get("result") or {}
    status = result.get("manual_source_resolution_status") or {}
    if not isinstance(status, dict):
        return None
    state = str(status.get("state") or "").strip()
    note = str(status.get("note") or "").lower()
    try:
        pending_scan_count = int(status.get("verification_pending_scan_count") or 0)
    except (TypeError, ValueError):
        pending_scan_count = 0
    if state in {"verification_pending", "import_busy", "verified_clearable", "already_present_clearable"}:
        return status
    if pending_scan_count > 0:
        return status
    if "waiting for kavita scan" in note or "already present on disk" in note:
        return status
    return None


def live_import_busy(live_result):
    status = (live_result or {}).get("manual_source_resolution_status") or {}
    import_result = (live_result or {}).get("import_result") or {}
    return status.get("state") == "import_busy" or bool(import_result.get("manual_source_import_busy"))


def import_result_count(import_result):
    import_result = import_result if isinstance(import_result, dict) else {}
    for key in ("count", "imported_count"):
        try:
            value = int(import_result.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    imported = import_result.get("imported")
    if isinstance(imported, list):
        return len(imported)
    return 0


def already_present_preview(response):
    result = (response or {}).get("result") or {}
    status = result.get("manual_source_resolution_status") or {}
    return status.get("state") == "preview_already_present"


def already_present_destinations(import_result):
    rows = []
    for row in (import_result or {}).get("skipped") or []:
        if already_present_skip_row(row):
            rows.append(already_present_skip_path(row))
    return rows


def already_present_skip_row(row):
    if not isinstance(row, dict):
        return False
    if str(row.get("action_needed") or "none").lower() != "none":
        return False
    if (
        row.get("event") == "skip_canonical_already_present"
        or row.get("skip_reason") == "canonical_file_already_visible_or_present"
    ):
        return bool(row.get("dest"))
    if row.get("event") == "skip_manga_unit_guard" and row.get("skip_reason") == "already_verified_duplicate":
        return bool(row.get("existing_path"))
    return False


def already_present_skip_path(row):
    if not isinstance(row, dict):
        return None
    return row.get("dest") or row.get("existing_path")


def issue_number_keys(value):
    text = str(value or "").strip()
    out = set()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        raw = match.group(0)
        out.add(raw.lstrip("0") or "0")
        try:
            number = int(float(raw))
            out.add(str(number))
            out.add(f"{number:03d}")
            out.add(f"{number:04d}")
        except ValueError:
            pass
    return {value for value in out if value}


def issue_number_keys_in_text(value):
    out = set()
    for raw in re.findall(r"(?<!\d)\d{1,4}(?:\.\d+)?(?!\d)", str(value or "")):
        out |= issue_number_keys(raw)
    return out


def already_present_identity_ok(record, detected, preview):
    wanted = issue_number_keys((record or {}).get("issue"))
    if not wanted:
        return True, "row has no issue number to compare"
    preview_result = (preview or {}).get("result") or {}
    destinations = already_present_destinations(preview_result.get("import_result") or {})
    if not destinations:
        return False, "already-present preview did not report a destination"
    destination_issue_matches = [
        path for path in destinations
        if wanted & issue_number_keys_in_text(path)
    ]
    if not destination_issue_matches:
        return False, "already-present destination does not match requested issue"
    detected_values = [
        (detected or {}).get("filename"),
        (detected or {}).get("path"),
    ]
    detected_issue_keys = set()
    for value in detected_values:
        detected_issue_keys |= issue_number_keys_in_text(value)
    if detected_issue_keys and not (wanted & detected_issue_keys):
        return False, "detected staged file does not match requested issue"
    return True, "already-present destination matches requested issue"


def mark_already_present_resolved(review_id, record, detected, preview):
    identity_ok, identity_reason = already_present_identity_ok(record, detected, preview)
    if not identity_ok:
        raise RuntimeError(identity_reason)
    preview_result = (preview or {}).get("result") or {}
    import_result = preview_result.get("import_result") or {}
    destinations = already_present_destinations(import_result)
    actions = read_json(ACTIONS_FILE, {}) or {}
    if not isinstance(actions, dict):
        actions = {}
    approved = actions.setdefault("approved", [])
    if not isinstance(approved, list):
        approved = []
        actions["approved"] = approved
    if str(review_id) not in approved:
        approved.append(str(review_id))

    waiting = actions.setdefault("manual_source_waiting", {})
    previous_waiting = None
    if isinstance(waiting, dict):
        previous_waiting = waiting.pop(str(review_id), None)

    resolved = actions.setdefault("manual_source_resolved", [])
    if not isinstance(resolved, list):
        resolved = []
        actions["manual_source_resolved"] = resolved
    resolved_record = {
        "review_id": str(review_id),
        "series": (record or {}).get("series") or (record or {}).get("query"),
        "issue": (record or {}).get("issue"),
        "source_path": (detected or {}).get("path"),
        "source_filename": (detected or {}).get("filename"),
        "destinations": destinations[:10],
        "imported_count": 0,
        "already_present_count": len(destinations),
        "verification": import_result.get("verification") or {},
        "reason": "already_present",
        "ts": now(),
        "ts_iso": utc_stamp(),
    }
    for field in CONTEXT_FIELDS:
        value = None
        for source in (record, previous_waiting, detected):
            if isinstance(source, dict) and source.get(field) not in (None, ""):
                value = source.get(field)
                break
        if value not in (None, ""):
            resolved_record[field] = value
    resolved.append(resolved_record)
    actions["manual_source_resolved"] = resolved[-100:]
    removed_bad = retire_manual_source_bad_candidates(
        actions,
        review_id,
        "manual_source_resolved",
        resolved_record,
    )
    if removed_bad:
        resolved_record["retired_bad_candidate_count"] = len(removed_bad)

    history = actions.setdefault("manual_source_waiting_cleared", [])
    if not isinstance(history, list):
        history = []
    history.append({
        "review_id": str(review_id),
        "previous": previous_waiting,
        "reason": "already_present_resolved",
        "detail": "preview proved the canonical file was already present",
        "ts": now(),
        "ts_iso": utc_stamp(),
    })
    actions["manual_source_waiting_cleared"] = history[-100:]
    save_actions(actions)
    log(
        "manual_source_already_present_resolved",
        review_id=review_id,
        destinations=destinations[:10],
        source_path=(detected or {}).get("path"),
    )
    auto_grab_audit(
        "already_present_resolved",
        review_id=review_id,
        series=resolved_record.get("series"),
        issue=resolved_record.get("issue"),
        source_path=resolved_record.get("source_path"),
        destinations=destinations[:10],
    )
    return resolved_record


def kavita_path_for_host_path(path):
    if not path:
        return None
    candidate = Path(str(path))
    for host_root, kavita_root in (
        (COMIC_ROOT, KAVITA_COMIC_ROOT),
        (MANGA_ROOT, KAVITA_MANGA_ROOT),
    ):
        try:
            rel = candidate.relative_to(host_root)
        except ValueError:
            continue
        return f"{kavita_root}/{rel.as_posix()}"
    return None


def kavita_file_visible_for_host_path(path):
    try:
        if not Path(str(path)).exists():
            return False
    except OSError:
        return False
    kavita_path = kavita_path_for_host_path(path)
    if not kavita_path or not KAVITA_DB.exists():
        return False
    con = sqlite_connect(KAVITA_DB)
    try:
        row = con.execute("select 1 from MangaFile where FilePath = ? limit 1", (kavita_path,)).fetchone()
        return bool(row)
    finally:
        con.close()


def import_status_pending_for_source(path):
    path = str(path or "").strip()
    if not path:
        return None
    status = read_json(IMPORT_STATUS_FILE, {}) or {}
    if not isinstance(status, dict):
        return None
    imported = [row for row in (status.get("imported") or []) if isinstance(row, dict)]
    skipped = [row for row in (status.get("skipped") or []) if isinstance(row, dict)]
    matched = None
    for row in imported:
        if str(row.get("source") or "") == path:
            matched = row
            break
    matched_skipped = None
    matched_actionable_skipped = None
    for row in skipped:
        if str(row.get("source") or "") != path:
            continue
        if not already_present_skip_row(row):
            reason = actionable_import_skip_reason(row)
            if reason and matched_actionable_skipped is None:
                matched_actionable_skipped = row
            continue
        matched_skipped = row
        break
    source_match = bool(
        str(status.get("pack_path") or "") == path
        or path in {str(value) for value in (status.get("sources") or [])}
    )
    if not matched and not matched_skipped and not source_match:
        return None
    verification = status.get("verification") if isinstance(status.get("verification"), dict) else {}
    matched_imported = matched if matched else imported[-20:]
    if matched_skipped and not matched:
        dest = str(already_present_skip_path(matched_skipped) or "")
        visible = kavita_file_visible_for_host_path(dest)
        return {
            "note": (
                "Canonical file is already visible in a library frontend; live resolver can clear this row."
                if visible
                else "Canonical file is already present on disk and waiting for library scan."
            ),
            "imported": [],
            "skipped": [matched_skipped],
            "verification": verification,
            "failures": [] if visible else [matched_skipped],
            "verified": bool(visible),
        }
    if matched_actionable_skipped and not matched:
        reason = actionable_import_skip_reason(matched_actionable_skipped)
        return {
            "note": reason or matched_actionable_skipped.get("skip_reason") or "importer rejected source; retry another source",
            "imported": [],
            "skipped": [matched_actionable_skipped],
            "verification": verification,
            "failures": [matched_actionable_skipped],
            "verified": False,
            "failure_reason": reason,
            "failure_status": matched_actionable_skipped.get("skip_reason") or "import_rejected",
        }
    if matched:
        destinations = {str(matched.get("dest") or "")}
    else:
        destinations = {str(row.get("dest") or "") for row in imported if row.get("dest")}
    checked = [
        row for row in (verification.get("checked") or [])
        if isinstance(row, dict) and str(row.get("dest") or "") in destinations
    ]
    if source_match and imported and not checked:
        return {
            "note": "Copied pack files are waiting for import verification.",
            "imported": matched_imported,
            "verification": verification,
            "failures": [],
            "verified": False,
        }
    pending_checks = [
        row for row in checked
        if row.get("verification_status") in {"waiting_for_library_scan", "waiting_for_kavita_scan", "library_scan_timeout", "kavita_scan_timeout"}
    ]
    if pending_checks:
        now_visible = [row for row in pending_checks if kavita_file_visible_for_host_path(row.get("dest"))]
        if len(now_visible) == len(pending_checks):
            return {
                "note": "Copied file is now visible in a library frontend; live resolver can clear this row.",
                "imported": matched_imported,
                "verification": verification,
                "failures": [],
                "verified": True,
            }
        count = len(pending_checks)
        return {
            "note": f"Copied file is waiting for library scan on {count} issue{'s' if count != 1 else ''}.",
            "imported": matched_imported,
            "verification": verification,
            "failures": pending_checks[:5],
            "verified": False,
        }
    verified_checks = [
        row for row in checked
        if row.get("verification_status") == "kavita_verified"
    ]
    if checked and len(verified_checks) == len(checked):
        return {
            "note": "Copied file already has library visibility evidence; live resolver can clear this row.",
            "imported": matched_imported,
            "verification": verification,
            "failures": [],
            "verified": True,
        }
    failures = [
        row for row in (verification.get("failures") or [])
        if isinstance(row, dict) and str(row.get("dest") or "") in destinations
    ]
    if not failures:
        return None
    failure_count = len(failures)
    kavita_visible = sum(1 for row in failures if row.get("kavita_visible"))
    kapowarr_unlinked = sum(1 for row in failures if row.get("truth_model") == "kapowarr" and not row.get("kapowarr_linked"))
    waiting_scan = sum(1 for row in failures if row.get("verification_status") in {"waiting_for_library_scan", "waiting_for_kavita_scan", "library_scan_timeout", "kavita_scan_timeout"})
    scanner_lag = sum(
        1
        for row in failures
        if row.get("truth_model") == "kapowarr"
        and row.get("host_exists")
        and row.get("comicinfo_status") == "present"
        and not row.get("kavita_visible")
    )
    if kavita_visible == failure_count and kapowarr_unlinked == failure_count:
        return {
            "note": "Copied file already has library visibility evidence; Kapowarr linking can reconcile separately.",
            "imported": matched,
            "verification": verification,
            "failures": [],
            "verified": True,
        }
    elif waiting_scan == failure_count or scanner_lag == failure_count:
        note = f"Copied file is waiting for library scan on {failure_count} issue{'s' if failure_count != 1 else ''}."
    else:
        note = f"Copied file is still blocked by {failure_count} verification failure{'s' if failure_count != 1 else ''}."
    return {
        "note": note,
        "imported": matched,
        "verification": verification,
        "failures": failures[:5],
    }


def pending_imported_count(pending):
    imported = (pending or {}).get("imported")
    if isinstance(imported, list):
        return len(imported)
    if isinstance(imported, dict):
        return 1 if imported else 0
    return 0


def pending_import_destinations(pending):
    pending = pending if isinstance(pending, dict) else {}
    destinations = []
    for row in pending.get("imported") or []:
        if isinstance(row, dict) and row.get("dest"):
            destinations.append(str(row.get("dest")))
    for row in pending.get("skipped") or []:
        if already_present_skip_row(row):
            path = already_present_skip_path(row)
            if path:
                destinations.append(str(path))
    out = []
    seen = set()
    for path in destinations:
        marker = path.replace("\\", "/").rstrip("/").lower()
        if marker and marker not in seen:
            seen.add(marker)
            out.append(path)
    return out


def pending_import_identity_ok(record, detected, pending):
    wanted = issue_number_keys((record or {}).get("issue"))
    if not wanted:
        return True, "row has no issue number to compare"
    destinations = pending_import_destinations(pending)
    if not destinations:
        return False, "verified import status did not report a destination"
    destination_issue_matches = [path for path in destinations if wanted & issue_number_keys_in_text(path)]
    if not destination_issue_matches:
        return False, "verified destination does not match requested issue"
    detected_values = [
        (detected or {}).get("filename"),
        (detected or {}).get("path"),
    ]
    detected_issue_keys = set()
    for value in detected_values:
        detected_issue_keys |= issue_number_keys_in_text(value)
    if detected_issue_keys and not (wanted & detected_issue_keys):
        return False, "detected staged file does not match requested issue"
    return True, "verified import status destination matches requested issue"


def mark_import_status_resolved(review_id, record, detected, pending, path):
    identity_ok, identity_reason = pending_import_identity_ok(record, detected, pending)
    if not identity_ok:
        raise RuntimeError(identity_reason)
    destinations = pending_import_destinations(pending)
    actions = read_json(ACTIONS_FILE, {}) or {}
    if not isinstance(actions, dict):
        actions = {}
    approved = actions.setdefault("approved", [])
    if not isinstance(approved, list):
        approved = []
        actions["approved"] = approved
    if str(review_id) not in approved:
        approved.append(str(review_id))

    waiting = actions.setdefault("manual_source_waiting", {})
    previous_waiting = None
    if isinstance(waiting, dict):
        previous_waiting = waiting.pop(str(review_id), None)

    resolved = actions.setdefault("manual_source_resolved", [])
    if not isinstance(resolved, list):
        resolved = []
        actions["manual_source_resolved"] = resolved
    resolved_record = {
        "review_id": str(review_id),
        "series": (record or {}).get("series") or (record or {}).get("query"),
        "issue": (record or {}).get("issue"),
        "source_path": str(path or ""),
        "source_filename": (detected or {}).get("filename"),
        "destinations": destinations[:10],
        "imported_count": pending_imported_count(pending),
        "already_present_count": max(0, len(destinations) - pending_imported_count(pending)),
        "verification": (pending or {}).get("verification") or {},
        "reason": "import_status_verified",
        "note": (pending or {}).get("note"),
        "ts": now(),
        "ts_iso": utc_stamp(),
    }
    for field in CONTEXT_FIELDS:
        value = None
        for source_row in (record, previous_waiting, detected):
            if isinstance(source_row, dict) and source_row.get(field) not in (None, ""):
                value = source_row.get(field)
                break
        if value not in (None, ""):
            resolved_record[field] = value
    resolved.append(resolved_record)
    actions["manual_source_resolved"] = resolved[-100:]
    removed_bad = retire_manual_source_bad_candidates(
        actions,
        review_id,
        "import_status_verified",
        resolved_record,
    )
    if removed_bad:
        resolved_record["retired_bad_candidate_count"] = len(removed_bad)

    history = actions.setdefault("manual_source_waiting_cleared", [])
    if not isinstance(history, list):
        history = []
    history.append({
        "review_id": str(review_id),
        "previous": previous_waiting,
        "reason": "import_status_verified",
        "detail": (pending or {}).get("note") or "import status proved the canonical file is visible",
        "ts": now(),
        "ts_iso": utc_stamp(),
    })
    actions["manual_source_waiting_cleared"] = history[-100:]
    save_actions(actions)
    log(
        "manual_source_import_status_resolved",
        review_id=review_id,
        destinations=destinations[:10],
        source_path=str(path or ""),
        note=(pending or {}).get("note"),
    )
    auto_grab_audit(
        "import_status_resolved",
        review_id=review_id,
        series=resolved_record.get("series"),
        issue=resolved_record.get("issue"),
        source_path=resolved_record.get("source_path"),
        destinations=destinations[:10],
    )
    return resolved_record


def apply_verified_import_status_resolution(args, result, row, path, pending, review_id, record, detected):
    if not isinstance(pending, dict) or not pending.get("verified"):
        return False
    if not inkdrop_completed_import.auto_inspect_completion_allowed(
        {"manual_source_waiting": record if isinstance(record, dict) else {}},
        pending,
    ):
        row["status"] = "verification_pending"
        row["verification_pending_note"] = "Exact artifact inspection has not passed yet."
        row["manual_source_resolved"] = False
        return True
    verification = pending.get("verification") if isinstance(pending.get("verification"), dict) else {}
    row["verification_pending_note"] = library_frontend_note(pending.get("note"))
    row["imported"] = pending.get("imported")
    row["skipped"] = pending.get("skipped") or []
    row["verification"] = {
        "failure_count": int(verification.get("failure_count") or 0),
        "checked_count": int(verification.get("checked_count") or 0),
        "pending_scan_count": int(verification.get("pending_scan_count") or 0),
        "failures": pending.get("failures") or [],
    }
    if args.live:
        resolved_record = mark_import_status_resolved(review_id, record, detected, pending, path)
        row["status"] = "resolved"
        row["manual_source_resolved"] = True
        row["manual_source_resolution"] = resolved_record
        row["live"] = {
            "state": "verified_clearable",
            "resolved": True,
            "note": library_frontend_note(pending.get("note")) or "Import status proved the canonical file is visible in a library frontend.",
            "imported_count": pending_imported_count(pending),
            "verification_checked_count": row["verification"]["checked_count"],
            "verification_failure_count": row["verification"]["failure_count"],
            "verification_pending_scan_count": row["verification"]["pending_scan_count"],
            "import_status_resolved": True,
        }
        result["resolved_count"] = int(result.get("resolved_count") or 0) + 1
        result["imported_count"] = int(result.get("imported_count") or 0) + pending_imported_count(pending)
    else:
        row["status"] = "already_verified"
        row["manual_source_resolved"] = False
        row["live"] = {
            "state": "verified_clearable",
            "resolved": False,
            "note": library_frontend_note(pending.get("note")) or "Import status proves the canonical file is visible; live resolver can clear this row.",
            "imported_count": pending_imported_count(pending),
            "verification_checked_count": row["verification"]["checked_count"],
            "verification_failure_count": row["verification"]["failure_count"],
            "verification_pending_scan_count": row["verification"]["pending_scan_count"],
            "import_status_resolved": False,
        }
    return True


def reconcile_live_import_transport_error(row, result, path, record=None):
    pending = import_status_pending_for_source(path)
    if not pending:
        return False
    row["transport_reconciled"] = True
    row["transport_reconcile_source"] = "import_status"
    if row.get("error"):
        row["transport_error"] = row["error"]
    row["verification_pending_note"] = library_frontend_note(pending.get("note"))
    row["imported"] = pending.get("imported")
    row["skipped"] = pending.get("skipped") or []
    verification = pending.get("verification") if isinstance(pending.get("verification"), dict) else {}
    row["verification"] = {
        "failure_count": int(verification.get("failure_count") or 0),
        "checked_count": int(verification.get("checked_count") or 0),
        "pending_scan_count": int(verification.get("pending_scan_count") or 0),
        "failures": pending.get("failures") or [],
    }
    if pending.get("failure_reason"):
        row["status"] = pending.get("failure_status") or "import_rejected"
        row["reason"] = pending.get("failure_reason")
        return True
    inspection_completion_allowed = inkdrop_completed_import.auto_inspect_completion_allowed(
        {"manual_source_waiting": record if isinstance(record, dict) else {}},
        pending,
    )
    if pending.get("verified") and inspection_completion_allowed:
        row["status"] = "resolved"
        row["manual_source_resolved"] = True
        row["manual_source_resolution"] = {
            "source_path": str(path or ""),
            "imported_count": pending_imported_count(pending),
            "verification": verification,
            "note": library_frontend_note(pending.get("note")),
            "transport_reconciled": True,
        }
        row["live"] = {
            "state": "verified_clearable",
            "resolved": True,
            "note": library_frontend_note(pending.get("note")) or "Import completed and verified after the HTTP response disconnected.",
            "imported_count": pending_imported_count(pending),
            "verification_checked_count": row["verification"]["checked_count"],
            "verification_failure_count": row["verification"]["failure_count"],
            "verification_pending_scan_count": row["verification"]["pending_scan_count"],
            "transport_reconciled": True,
        }
        result["resolved_count"] = int(result.get("resolved_count") or 0) + 1
    else:
        row["status"] = "verification_pending"
        row["manual_source_resolved"] = False
        row["live"] = {
            "state": "verification_pending",
            "resolved": False,
            "note": library_frontend_note(pending.get("note")) or "Import response disconnected after copy; waiting for library visibility.",
            "imported_count": pending_imported_count(pending),
            "verification_checked_count": row["verification"]["checked_count"],
            "verification_failure_count": row["verification"]["failure_count"],
            "verification_pending_scan_count": row["verification"]["pending_scan_count"],
            "transport_reconciled": True,
        }
    result["imported_count"] = int(result.get("imported_count") or 0) + pending_imported_count(pending)
    return True


def run(args):
    # Finish or safely retire existing work before spending this resolver pass
    # on replacement searches. Candidate failures are persisted to the normal
    # pending-retry queue and one bounded retry runs after import processing.
    args._reconcile_existing_work_before_retry = True
    actions = read_json(ACTIONS_FILE, {}) or {}
    queue_items = autopilot_queue_items()
    verified_action_cleared_count = clear_verified_action_records(actions, queue_items)
    if verified_action_cleared_count:
        actions = read_json(ACTIONS_FILE, {}) or {}
    verified_probe_pruned_count = prune_verified_probe_records(queue_items)
    waiting = waiting_records(actions)
    probe_status = read_json(PROBE_STATUS_FILE, {}) or {}
    ready = ready_detected_records(probe_status, actions) if args.include_ready else {}
    pending_retries = retry_pending_records(actions)
    db_import_retries = db_import_retry_records(args.min_age_seconds)
    legacy_waiting_count = len(waiting)
    for review_id, record in db_import_retries.items():
        waiting.setdefault(str(review_id), record)
    started = now()
    stall_policy = slskd_stall_policy(INKDROP_STATE_DB)
    result = {
        "ok": True,
        "live": bool(args.live),
        "include_ready": bool(args.include_ready),
        "generated_at": started,
        "generated_at_iso": utc_stamp(started),
        "waiting_count": len(waiting),
        "legacy_waiting_count": legacy_waiting_count,
        "db_import_retry_count": len(db_import_retries),
        "ready_detected_count": len(ready),
        "retry_pending_count": len(pending_retries),
        "effective_config": {"slskd_stall_gate": stall_policy},
        "eligible_count": 0,
        "previewed_count": 0,
        "imported_count": 0,
        "resolved_count": 0,
        "bad_candidate_count": 0,
        "cancelled_transfer_count": 0,
        "retry_probe_count": 0,
        "retry_deferred_count": 0,
        "retry_started_count": 0,
        "retry_cooldown_count": 0,
        "verified_action_cleared_count": verified_action_cleared_count,
        "verified_probe_pruned_count": verified_probe_pruned_count,
        "terminal_false_duplicate_reconciliation": {
            "review_count": 0,
            "evidence_count": 0,
            "retired_count": 0,
            "rows": [],
        },
        "skipped": [],
        "processed": [],
        "policy": (
            "SLSKD/manual rows marked waiting are eligible. "
            "When --include-ready is used, exact Ready to Import rows are also eligible "
            "only if their detected file has a clean positive title/issue match. "
            "Each detected file must be stable, present in the refreshed probe cache, "
            "preview_importable through /api/manual-source/import-detected. Live imports "
            "clear rows after Kavita-visible verification; copied files that are still waiting "
            "on scanner visibility remain pending instead of being treated as failed candidates. "
            "Failed SLSKD candidates are marked bad, cancelled in SLSKD when possible, "
            "cleared from waiting, and followed by one focused next-best SLSKD autopick attempt. "
            "If SLSKD probing is busy, the retry is kept in a small pending retry queue."
        ),
    }
    if args.live:
        try:
            reconciliation_probe = load_probe_module(args.probe_script)
            result["terminal_false_duplicate_reconciliation"] = (
                reconcile_terminal_false_duplicate_review_attempts(reconciliation_probe, INKDROP_STATE_DB)
            )
        except Exception as exc:
            result["terminal_false_duplicate_reconciliation"] = {
                "review_count": 0,
                "evidence_count": 0,
                "retired_count": 0,
                "rows": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
            log("terminal_false_duplicate_reconciliation_failed", error=f"{type(exc).__name__}: {exc}")
    if not waiting and not ready and not pending_retries:
        finish_result_summary(result)
        write_json(STATUS_FILE, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return result

    publish_progress(
        result,
        "checking",
        "Manual Source resolver checking",
        (
            f"Checking {len(waiting)} waiting, {len(ready)} ready staged, "
            f"and {len(pending_retries)} pending retry row"
            f"{'s' if len(waiting) + len(ready) + len(pending_retries) != 1 else ''}."
        ),
    )
    try:
        probe, probe_settings = load_configured_probe_module(args.probe_script)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        result.update(
            {
                "ok": False,
                "state": "configuration_error",
                "error": message,
                "configuration_error": "slskd_provider_settings",
            }
        )
        log("manual_source_probe_settings_failed", error=message)
        finish_result_summary(result)
        write_json(STATUS_FILE, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return result
    result["effective_config"]["slskd_provider_source"] = probe_settings.get("source") or "fallback"
    records = dict(waiting)
    for review_id, record in ready.items():
        records.setdefault(review_id, record)
    download_status = slskd_download_transfers() if waiting else {"ok": True, "transfers": []}
    for review_id, record in list(waiting.items()):
        transfer = waiting_transfer_status(record, download_status)
        if not (transfer and transfer.get("status") == "transfer_failed"):
            continue
        record_slskd_learning(record, None, False, "slskd transfer failed", review_id)
        skip = waiting_status_row(
            review_id,
            record,
            "slskd transfer failed",
            source="waiting",
            status="transfer_failed",
            transfer=transfer,
        )
        recovery = recover_failed_waiting_candidate(args, result, review_id, record, None, "slskd transfer failed", transfer=transfer)
        if recovery:
            skip["recovery"] = recovery
        result["skipped"].append(skip)
        records.pop(review_id, None)
    if not records:
        process_pending_slskd_retries(
            args,
            result,
            read_json(ACTIONS_FILE, {}) or {},
            limit=1,
        )
        sync_autopilot_and_native_queue_from_result(result, reason="manual_source_waiting_terminal")
        finish_result_summary(result)
        write_json(STATUS_FILE, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return result
    publish_progress(
        result,
        "checking",
        "Refreshing staged file matches",
        "Refreshing SLSKD/manual staged-file detection before preview/import.",
    )
    refreshed, _probe_status = refresh_probe_rows(probe, records)
    eligible = []
    for entry in refreshed:
        review_id = str(entry.get("review_id") or "")
        source = "waiting" if review_id in waiting else str(entry.get("autoresolve_source") or "ready_detected")
        detected = entry.get("detected_files") or []
        record = records.get(review_id) or {}
        transfer = waiting_transfer_status(record, download_status) if source == "waiting" else None
        direct_rejection = None
        if transfer and transfer.get("status") == "transfer_succeeded":
            direct_candidates = transfer_local_path_candidates(probe, record, transfer)
            direct_detected = completed_transfer_detected_files(
                probe,
                record,
                record,
                transfer,
                candidates=direct_candidates,
            )
            if direct_detected:
                detected = merge_detected_file_rows(direct_detected, detected)
            else:
                direct_rejection = completed_transfer_rejection_evidence(
                    probe,
                    record,
                    transfer,
                    detected,
                    candidates=direct_candidates,
                )
        if not detected:
            if transfer and transfer.get("status") == "transfer_failed":
                record_slskd_learning(record, None, False, "slskd transfer failed", review_id)
                skip = waiting_status_row(
                    review_id,
                    record,
                    "slskd transfer failed",
                    source=source,
                    status="transfer_failed",
                    transfer=transfer,
                )
                recovery = recover_failed_waiting_candidate(args, result, review_id, record, None, "slskd transfer failed", transfer=transfer)
                if recovery:
                    skip["recovery"] = recovery
                result["skipped"].append(skip)
            elif transfer and transfer.get("status") == "transfer_in_progress":
                stale_reason = stale_waiting_failure_reason(record, transfer, stall_policy)
                if stale_reason:
                    record_slskd_learning(record, None, False, stale_reason, review_id)
                    skip = waiting_status_row(
                        review_id,
                        record,
                        stale_reason,
                        source=source,
                        status="transfer_stalled",
                        transfer=transfer,
                    )
                    recovery = recover_failed_waiting_candidate(args, result, review_id, record, None, stale_reason, transfer=transfer)
                    if recovery:
                        skip["recovery"] = recovery
                    result["skipped"].append(skip)
                else:
                    result["skipped"].append(waiting_status_row(
                        review_id,
                        record,
                        "slskd transfer in progress",
                        source=source,
                        status="transfer_in_progress",
                        transfer=transfer,
                    ))
            elif transfer and transfer.get("status") == "transfer_settling":
                result["skipped"].append(waiting_status_row(
                    review_id,
                    record,
                    "slskd transfer settling",
                    source=source,
                    status="transfer_settling",
                    transfer=transfer,
                ))
            elif transfer and transfer.get("status") == "transfer_succeeded":
                rejection_reason = "completed transfer artifact does not match selected item" if direct_rejection else ""
                reason = rejection_reason or "slskd transfer completed but no staged file detected"
                status = "staged_file_mismatch" if direct_rejection else "transfer_succeeded_missing_stage"
                record_slskd_learning(record, direct_rejection, False, reason, review_id)
                skip = waiting_status_row(
                    review_id,
                    record,
                    reason,
                    source=source,
                    status=status,
                    transfer=transfer,
                )
                recovery = recover_failed_waiting_candidate(
                    args,
                    result,
                    review_id,
                    record,
                    direct_rejection,
                    reason,
                    transfer=transfer,
                )
                if recovery:
                    skip["recovery"] = recovery
                result["skipped"].append(skip)
            elif transfer and transfer.get("status") == "transfer_lookup_error":
                result["skipped"].append(waiting_status_row(
                    review_id,
                    record,
                    "slskd transfer lookup failed",
                    source=source,
                    status="transfer_lookup_error",
                    transfer=transfer,
                ))
            elif transfer:
                stale_reason = stale_waiting_failure_reason(record, transfer, stall_policy)
                if stale_reason:
                    record_slskd_learning(record, None, False, stale_reason, review_id)
                    skip = waiting_status_row(
                        review_id,
                        record,
                        stale_reason,
                        source=source,
                        status="transfer_stale_unknown",
                        transfer=transfer,
                    )
                    recovery = recover_failed_waiting_candidate(args, result, review_id, record, None, stale_reason, transfer=transfer)
                    if recovery:
                        skip["recovery"] = recovery
                    result["skipped"].append(skip)
                else:
                    result["skipped"].append(waiting_status_row(
                        review_id,
                        record,
                        "slskd transfer status is not actionable yet",
                        source=source,
                        status=transfer.get("status") or "transfer_unknown",
                        transfer=transfer,
                    ))
            else:
                stale_reason = stale_waiting_failure_reason(record, stall_policy=stall_policy)
                if stale_reason:
                    record_slskd_learning(record, None, False, stale_reason, review_id)
                    skip = waiting_status_row(
                        review_id,
                        record,
                        stale_reason,
                        source=source,
                        status="transfer_missing_stale",
                    )
                    recovery = recover_failed_waiting_candidate(args, result, review_id, record, None, stale_reason)
                    if recovery:
                        skip["recovery"] = recovery
                    result["skipped"].append(skip)
                else:
                    result["skipped"].append(waiting_status_row(
                        review_id,
                        record,
                        "no detected staged file",
                        source=source,
                        status="waiting_for_staged_file",
                    ))
            continue
        detected = sorted(
            [row for row in detected if isinstance(row, dict)],
            key=lambda row: (int(row.get("score") or 0), float(row.get("mtime") or 0)),
            reverse=True,
        )
        if source == "waiting" and str(record.get("filename") or "").strip():
            matched_detected = []
            filename_rejections = []
            for row in detected:
                match_ok, match_reason = waiting_filename_match(row, record)
                if match_ok:
                    matched_detected.append(row)
                else:
                    filename_rejections.append({"path": row.get("path"), "reason": match_reason})
            if not matched_detected:
                if isinstance(transfer, dict) and transfer:
                    transfer_status = str(transfer.get("status") or "").strip()
                    if transfer_status == "transfer_failed":
                        record_slskd_learning(record, None, False, "slskd transfer failed", review_id)
                        skip = waiting_status_row(
                            review_id,
                            record,
                            "slskd transfer failed",
                            source=source,
                            status="transfer_failed",
                            transfer=transfer,
                            rejections=filename_rejections[:5],
                        )
                        recovery = recover_failed_waiting_candidate(
                            args,
                            result,
                            review_id,
                            record,
                            None,
                            "slskd transfer failed",
                            transfer=transfer,
                        )
                        if recovery:
                            skip["recovery"] = recovery
                        result["skipped"].append(skip)
                        continue
                    if transfer_status == "transfer_settling":
                        result["skipped"].append(waiting_status_row(
                            review_id,
                            record,
                            "slskd transfer settling",
                            source=source,
                            status="transfer_settling",
                            transfer=transfer,
                            rejections=filename_rejections[:5],
                        ))
                        continue
                    if transfer_status == "transfer_lookup_error":
                        result["skipped"].append(waiting_status_row(
                            review_id,
                            record,
                            "slskd transfer lookup failed",
                            source=source,
                            status="transfer_lookup_error",
                            transfer=transfer,
                            rejections=filename_rejections[:5],
                        ))
                        continue
                    if transfer_status != "transfer_succeeded":
                        stale_reason = stale_waiting_failure_reason(record, transfer, stall_policy)
                        if stale_reason:
                            record_slskd_learning(record, None, False, stale_reason, review_id)
                            skip = waiting_status_row(
                                review_id,
                                record,
                                stale_reason,
                                source=source,
                                status="transfer_stalled" if transfer_status == "transfer_in_progress" else "transfer_stale_unknown",
                                transfer=transfer,
                                rejections=filename_rejections[:5],
                            )
                            recovery = recover_failed_waiting_candidate(
                                args,
                                result,
                                review_id,
                                record,
                                None,
                                stale_reason,
                                transfer=transfer,
                            )
                            if recovery:
                                skip["recovery"] = recovery
                            result["skipped"].append(skip)
                        else:
                            result["skipped"].append(waiting_status_row(
                                review_id,
                                record,
                                "slskd transfer in progress"
                                if transfer_status == "transfer_in_progress"
                                else "slskd transfer status is not actionable yet",
                                source=source,
                                status=transfer_status or "transfer_unknown",
                                transfer=transfer,
                                rejections=filename_rejections[:5],
                                ignored_detected_count=len(filename_rejections),
                            ))
                        continue
                record_slskd_learning(record, None, False, "staged file did not match waiting candidate", review_id)
                result["skipped"].append(waiting_status_row(
                    review_id,
                    record,
                    "no detected staged file matched the marked waiting candidate filename",
                    source=source,
                    status="staged_filename_mismatch",
                    candidate_filename=record.get("filename"),
                    rejections=filename_rejections[:5],
                ))
                continue
            detected = matched_detected
        chosen = detected[0] if detected else None
        stable, reason = stable_detected_file(chosen, args.min_age_seconds)
        if not stable:
            result["skipped"].append(waiting_status_row(
                review_id,
                record,
                reason,
                source=source,
                status="staged_file_settling",
                detected=chosen,
                path=(chosen or {}).get("path"),
            ))
            continue
        quality_ok, quality_reason = auto_import_quality(chosen, source, item=record, probe_module=probe)
        if not quality_ok:
            record_slskd_learning(record, chosen, False, quality_reason, review_id)
            skip = waiting_status_row(
                review_id,
                record,
                quality_reason,
                source=source,
                status="quality_rejected",
                detected=chosen,
                path=(chosen or {}).get("path"),
            )
            recovery = recover_failed_waiting_candidate(args, result, review_id, record, chosen, quality_reason)
            if recovery:
                skip["recovery"] = recovery
            result["skipped"].append(skip)
            continue
        eligible.append((review_id, chosen, source, quality_reason, record))

    result["eligible_count"] = len(eligible)
    publish_progress(
        result,
        "checking" if eligible else "watching",
        "Auto-resolver checking staged files" if eligible else "Watching Manual Source downloads",
        (
            f"{len(eligible)} staged file{' is' if len(eligible) == 1 else 's are'} eligible for preview/import."
            if eligible
            else "No staged file is eligible for import yet."
        ),
        eligible_count=len(eligible),
        skipped=result.get("skipped") or [],
    )
    eligible_batch = eligible[: args.max_imports]
    result["eligible_deferred_count"] = max(0, len(eligible) - len(eligible_batch))
    for review_id, detected, source, quality_reason, record in eligible_batch:
        path = detected.get("path")
        row = {
            "review_id": review_id,
            "series": record.get("series"),
            "issue": record.get("issue"),
            "path": path,
            "filename": detected.get("filename"),
            "score": detected.get("score"),
            "source": source,
            "quality": quality_reason,
            "dry_run": not args.live,
        }
        for field in CONTEXT_FIELDS:
            value = record.get(field)
            if value not in (None, ""):
                row[field] = value
        try:
            pending = import_status_pending_for_source(path)
            if apply_verified_import_status_resolution(args, result, row, path, pending, review_id, record, detected):
                if args.live and row.get("manual_source_resolved"):
                    cancel_superseded_slskd_transfer(result, review_id, record, row, detected)
                record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
                result["processed"].append(row)
                log("manual_source_autoresolve_row", **row)
                continue
            if pending and not pending.get("verified"):
                if pending.get("failure_reason"):
                    row["status"] = pending.get("failure_status") or "import_rejected"
                    row["reason"] = pending.get("failure_reason")
                else:
                    row["status"] = "verification_pending"
                row["verification_pending_note"] = pending.get("note")
                row["imported"] = pending.get("imported")
                row["skipped"] = pending.get("skipped") or []
                row["verification"] = {
                    "failure_count": int((pending.get("verification") or {}).get("failure_count") or 0),
                    "checked_count": int((pending.get("verification") or {}).get("checked_count") or 0),
                    "pending_scan_count": int((pending.get("verification") or {}).get("pending_scan_count") or 0),
                    "failures": pending.get("failures") or [],
                }
                record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
                failure_reason = recovery_failure_reason(row)
                if failure_reason:
                    recovery = recover_failed_waiting_candidate(args, result, review_id, record, detected, failure_reason)
                    if recovery:
                        row["recovery"] = recovery
                result["processed"].append(row)
                continue
            publish_progress(
                result,
                "checking",
                "Previewing staged file",
                f"Previewing {detected.get('filename') or path} before any live import.",
                current_review_id=review_id,
                current_path=path,
                current_filename=detected.get("filename"),
            )
            preview = post_import_detected(args.api_url, review_id, path, dry_run=True)
            result["previewed_count"] += 1
            preview_result = (preview.get("result") or {}) if isinstance(preview, dict) else {}
            preview_import_result = preview_result.get("import_result") if isinstance(preview_result.get("import_result"), dict) else {}
            row["preview"] = preview_result.get("manual_source_resolution_status") or {}
            preview_skip_reason = import_response_actionable_skip_reason(preview)
            if not importable_preview(preview):
                pending_preview = pending_or_consumed_preview(preview)
                if pending_preview:
                    state = str(pending_preview.get("state") or "")
                    row["status"] = "already_verified" if state in {"verified_clearable", "already_present_clearable"} else "verification_pending"
                    row["verification_pending_note"] = pending_preview.get("note")
                    row["verification"] = {
                        "failure_count": int(pending_preview.get("verification_failure_count") or 0),
                        "checked_count": int(pending_preview.get("verification_checked_count") or 0),
                        "pending_scan_count": int(pending_preview.get("verification_pending_scan_count") or 0),
                        "identity_mismatch_count": int(pending_preview.get("verification_identity_mismatch_count") or 0),
                        "identity_mismatches": pending_preview.get("verification_identity_mismatches") or [],
                    }
                    if row["status"] == "already_verified" and args.live:
                        identity_ok, identity_reason = already_present_identity_ok(record, detected, preview)
                        if not identity_ok:
                            row["status"] = "preview_not_importable"
                            row["reason"] = identity_reason
                            row["verification"]["identity_mismatch_count"] = max(
                                1,
                                int(row["verification"].get("identity_mismatch_count") or 0),
                            )
                            row["verification"]["identity_mismatches"] = [
                                {
                                    "reason": identity_reason,
                                    "path": path,
                                    "filename": detected.get("filename"),
                                    "issue": record.get("issue"),
                                },
                                *(
                                    row["verification"].get("identity_mismatches")
                                    if isinstance(row["verification"].get("identity_mismatches"), list)
                                    else []
                                ),
                            ][:5]
                            record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
                            recovery = recover_failed_waiting_candidate(args, result, review_id, record, detected, identity_reason)
                            if recovery:
                                row["recovery"] = recovery
                            result["processed"].append(row)
                            continue
                        publish_progress(
                            result,
                            "importing",
                            "Clearing already verified staged file",
                            f"Clearing {detected.get('filename') or path} through the verified import endpoint.",
                            current_review_id=review_id,
                            current_path=path,
                            current_filename=detected.get("filename"),
                            previewed_count=result.get("previewed_count"),
                        )
                        live = post_import_detected(args.api_url, review_id, path, dry_run=False)
                        live_result = live.get("result") or {}
                        row["live"] = live_result.get("manual_source_resolution_status") or {}
                        row["manual_source_resolution"] = live_result.get("manual_source_resolution") or {}
                        row["manual_source_resolved"] = bool(live_result.get("manual_source_resolved"))
                        if live_import_busy(live_result):
                            row["status"] = "import_busy"
                        elif row["manual_source_resolved"]:
                            row["status"] = "resolved"
                            result["resolved_count"] += 1
                        result["imported_count"] += import_result_count(live_result.get("import_result"))
                    if args.live and row.get("manual_source_resolved"):
                        cancel_superseded_slskd_transfer(result, review_id, record, row, detected)
                    record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
                    result["processed"].append(row)
                    continue
                pending = import_status_pending_for_source(path)
                if pending:
                    if apply_verified_import_status_resolution(args, result, row, path, pending, review_id, record, detected):
                        if args.live and row.get("manual_source_resolved"):
                            cancel_superseded_slskd_transfer(result, review_id, record, row, detected)
                        record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
                        result["processed"].append(row)
                        continue
                    if pending.get("failure_reason"):
                        row["status"] = pending.get("failure_status") or "import_rejected"
                        row["reason"] = pending.get("failure_reason")
                    else:
                        row["status"] = "already_verified" if pending.get("verified") else "verification_pending"
                    row["verification_pending_note"] = pending.get("note")
                    row["imported"] = pending.get("imported")
                    row["skipped"] = pending.get("skipped") or []
                    row["verification"] = {
                        "failure_count": int((pending.get("verification") or {}).get("failure_count") or 0),
                        "checked_count": int((pending.get("verification") or {}).get("checked_count") or 0),
                        "failures": pending.get("failures") or [],
                    }
                    if row.get("reason"):
                        record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
                        failure_reason = recovery_failure_reason(row)
                        if failure_reason:
                            recovery = recover_failed_waiting_candidate(args, result, review_id, record, detected, failure_reason)
                            if recovery:
                                row["recovery"] = recovery
                        result["processed"].append(row)
                        continue
                    if args.live:
                        publish_progress(
                            result,
                            "importing",
                            "Clearing already verified staged file",
                            f"Clearing {detected.get('filename') or path} through the verified import endpoint.",
                            current_review_id=review_id,
                            current_path=path,
                            current_filename=detected.get("filename"),
                            previewed_count=result.get("previewed_count"),
                        )
                        live = post_import_detected(args.api_url, review_id, path, dry_run=False)
                        live_result = live.get("result") or {}
                        row["live"] = live_result.get("manual_source_resolution_status") or {}
                        row["manual_source_resolution"] = live_result.get("manual_source_resolution") or {}
                        row["manual_source_resolved"] = bool(live_result.get("manual_source_resolved"))
                        if live_import_busy(live_result):
                            row["status"] = "import_busy"
                        else:
                            row["status"] = "resolved" if row["manual_source_resolved"] else "verification_pending"
                        if row["manual_source_resolved"]:
                            result["resolved_count"] += 1
                        result["imported_count"] += import_result_count(live_result.get("import_result"))
                    if args.live and row.get("manual_source_resolved"):
                        cancel_superseded_slskd_transfer(result, review_id, record, row, detected)
                    record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
                    failure_reason = recovery_failure_reason(row)
                    if failure_reason:
                        recovery = recover_failed_waiting_candidate(args, result, review_id, record, detected, failure_reason)
                        if recovery:
                            row["recovery"] = recovery
                    result["processed"].append(row)
                    continue
                row["status"] = "preview_not_importable"
                if preview_skip_reason:
                    row["reason"] = preview_skip_reason
                    row["skipped"] = preview_import_result.get("skipped") or []
                record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
                failure_reason = recovery_failure_reason(row)
                if failure_reason:
                    recovery = recover_failed_waiting_candidate(args, result, review_id, record, detected, failure_reason)
                    if recovery:
                        row["recovery"] = recovery
                result["processed"].append(row)
                continue
            if already_present_preview(preview):
                row["status"] = "preview_already_present"
                identity_ok, identity_reason = already_present_identity_ok(record, detected, preview)
                if not identity_ok:
                    row["status"] = "preview_not_importable"
                    row["reason"] = identity_reason
                    row["verification"] = {
                        "failure_count": 0,
                        "checked_count": 0,
                        "pending_scan_count": 0,
                        "identity_mismatch_count": 1,
                        "identity_mismatches": [
                            {
                                "reason": identity_reason,
                                "path": path,
                                "filename": detected.get("filename"),
                                "issue": record.get("issue"),
                            }
                        ],
                    }
                    record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
                    recovery = recover_failed_waiting_candidate(args, result, review_id, record, detected, identity_reason)
                    if recovery:
                        row["recovery"] = recovery
                    result["processed"].append(row)
                    log("manual_source_autoresolve_row", **row)
                    continue
                if args.live:
                    resolved_record = mark_already_present_resolved(review_id, record, detected, preview)
                    row["status"] = "resolved"
                    row["resolution"] = "already_present"
                    row["manual_source_resolved"] = True
                    row["manual_source_resolution"] = resolved_record
                    row["live"] = {
                        "state": "already_present_clearable",
                        "resolved": True,
                        "note": "Preview proved the canonical file is already visible in a library frontend; cleared without copying.",
                        "imported_count": 0,
                        "already_present_count": int(resolved_record.get("already_present_count") or 0),
                    }
                    result["resolved_count"] += 1
                else:
                    row["status"] = "preview_already_present"
                    row["verification_pending_note"] = "Canonical file is already visible in a library frontend; live resolver can clear this row without copying."
                if args.live and row.get("manual_source_resolved"):
                    cancel_superseded_slskd_transfer(result, review_id, record, row, detected)
                record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
                result["processed"].append(row)
                publish_progress(
                            result,
                            "checking",
                            "SLSKD import row processed",
                            f"Processed {detected.get('filename') or path}: {row.get('status') or 'done'}.",
                    processed=result.get("processed") or [],
                    previewed_count=result.get("previewed_count"),
                    imported_count=result.get("imported_count"),
                    resolved_count=result.get("resolved_count"),
                )
                log("manual_source_autoresolve_row", **row)
                continue
            if args.live:
                publish_progress(
                    result,
                    "importing",
                    "Importing staged file",
                    f"Importing {detected.get('filename') or path} through verification.",
                    current_review_id=review_id,
                    current_path=path,
                    current_filename=detected.get("filename"),
                    previewed_count=result.get("previewed_count"),
                )
                live = post_import_detected(args.api_url, review_id, path, dry_run=False)
                live_result = live.get("result") or {}
                row["live"] = live_result.get("manual_source_resolution_status") or {}
                row["manual_source_resolution"] = live_result.get("manual_source_resolution") or {}
                row["manual_source_resolved"] = bool(live_result.get("manual_source_resolved"))
                if live_import_busy(live_result):
                    row["status"] = "import_busy"
                elif row["live"].get("state") == "verification_pending":
                    row["status"] = "verification_pending"
                    row["verification_pending_note"] = row["live"].get("note")
                else:
                    row["status"] = "resolved" if row["manual_source_resolved"] else "imported_not_resolved"
                if row["manual_source_resolved"]:
                    result["resolved_count"] += 1
                result["imported_count"] += import_result_count(live_result.get("import_result"))
            else:
                row["status"] = "preview_importable"
            if args.live and row.get("manual_source_resolved"):
                cancel_superseded_slskd_transfer(result, review_id, record, row, detected)
            record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
            failure_reason = recovery_failure_reason(row)
            if failure_reason:
                recovery = recover_failed_waiting_candidate(args, result, review_id, record, detected, failure_reason)
                if recovery:
                    row["recovery"] = recovery
            result["processed"].append(row)
            publish_progress(
                result,
                "checking",
                "SLSKD import row processed",
                f"Processed {detected.get('filename') or path}: {row.get('status') or 'done'}.",
                processed=result.get("processed") or [],
                previewed_count=result.get("previewed_count"),
                imported_count=result.get("imported_count"),
                resolved_count=result.get("resolved_count"),
            )
            log("manual_source_autoresolve_row", **row)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            reconciled = bool(args.live and reconcile_live_import_transport_error(row, result, path, record))
            if not reconciled:
                failure = classify_candidate_failure(row["error"])
                if failure.get("candidate_bad"):
                    row["status"] = "error"
                else:
                    row["status"] = "transient_error"
                    row["transient_reason"] = failure.get("reason") or "transient_resolver_error"
                    row["transient_label"] = failure.get("label") or "Temporary resolver problem"
                    result["transient_failure_count"] = int(result.get("transient_failure_count") or 0) + 1
            else:
                log(
                    "manual_source_live_transport_reconciled",
                    review_id=review_id,
                    path=path,
                    status=row.get("status"),
                    note=row.get("verification_pending_note"),
                    error=row.get("error"),
                )
            record_slskd_learning_from_row(args.live, source, row, record, detected, review_id)
            failure_reason = recovery_failure_reason(row)
            if failure_reason:
                recovery = recover_failed_waiting_candidate(args, result, review_id, record, detected, failure_reason)
                if recovery:
                    row["recovery"] = recovery
            result["processed"].append(row)
            progress_state = "checking" if reconciled else "error"
            progress_label = "SLSKD import reconciled" if reconciled else "Manual Source resolver error"
            progress_next = (
                f"Recovered import result for {detected.get('filename') or path}: {row.get('status') or 'done'}."
                if reconciled
                else f"Error while processing {detected.get('filename') or path}: {row.get('error')}"
            )
            publish_progress(
                result,
                progress_state,
                progress_label,
                progress_next,
                processed=result.get("processed") or [],
            )
            log("manual_source_autoresolve_error" if not reconciled else "manual_source_autoresolve_row", **row)

    # Do not spend this pass on replacement searches while completed/staged
    # artifacts still wait behind the bounded import batch. The durable retry
    # remains pending for a later pass after reconciliation catches up.
    if not result["eligible_deferred_count"]:
        process_pending_slskd_retries(args, result, read_json(ACTIONS_FILE, {}) or {}, limit=1)
    result["retry_pending_count"] = len(retry_pending_records(read_json(ACTIONS_FILE, {}) or {}))
    sync_autopilot_and_native_queue_from_result(result, reason="manual_source_autoresolve")
    finish_result_summary(result)
    write_json(STATUS_FILE, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


def main():
    parser = argparse.ArgumentParser(description="Auto-resolve Manual Source rows that now have a safe staged file.")
    parser.add_argument("--live", action="store_true", help="Run the live import after a preview_importable dry-run.")
    parser.add_argument("--include-ready", action="store_true", help="Also process exact Ready to Import rows that pass strict match gates.")
    parser.add_argument("--max-imports", type=int, default=2)
    parser.add_argument("--min-age-seconds", type=int, default=120)
    parser.add_argument("--api-url", default=API_URL)
    parser.add_argument("--probe-script", type=Path, default=PROBE_SCRIPT)
    args = parser.parse_args()
    args.max_imports = max(1, min(int(args.max_imports or 2), 10))
    args.min_age_seconds = max(30, int(args.min_age_seconds or 120))
    run(args)


if __name__ == "__main__":
    main()
