"""Environment-driven service wrapper for settings-backed source batches.

The source worker CLI is the behavioral boundary. This module only translates
service/cron environment knobs into that CLI so automation can be deployed
without hard-coding active providers.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import inkdrop_runtime_config
import inkdrop_source_worker_cli as cli


CONTRACT_VERSION = 1
DEFAULT_STATE_DB = str(inkdrop_runtime_config.state_db_path())
PACK_REVIEW_STATE_NAME = inkdrop_runtime_config.PACK_REVIEW_STATE_NAME
RECONCILIATION_DB_NAME = inkdrop_runtime_config.IMPORTED_FILES_DB_NAME
DEFAULT_IMPORT_BACKLOG_PRIORITY_MIN = 3
PACK_IMPORT_ACTIVE_SECONDS = 2 * 60 * 60
INKDROP_STATE_IMPORT_READY_STATUSES = (
    "completed_in_client",
    "staged_file_ready",
    "ready_import",
    "preview_importable",
    "ready_to_import",
)
INKDROP_IMPORT_BACKLOG_ACTIONABLE_RECONCILIATION_STATES = {
    "ready_to_import",
    "importing",
}


TRUTHY = {"1", "true", "yes", "y", "on", "enabled"}
FALSY = {"0", "false", "no", "n", "off", "disabled"}


def _truthy(value):
    return str(value or "").strip().lower() in TRUTHY


def _falsy(value):
    return str(value or "").strip().lower() in FALSY


def _int(value, default=0, minimum=None):
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    if minimum is not None:
        parsed = max(int(minimum), parsed)
    return parsed


def _float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _split_csv(value):
    out = []
    for part in str(value or "").replace("\n", ",").split(","):
        text = part.strip()
        if text:
            out.append(text)
    return out


def _append_repeated(argv, flag, values):
    for value in values or []:
        argv.extend([flag, str(value)])


def _append_value(argv, flag, value):
    if value not in (None, ""):
        argv.extend([flag, str(value)])


def _env(environ, key, default=None):
    value = (environ or {}).get(key)
    return default if value in (None, "") else value


def _cli_value(argv, flag):
    try:
        index = list(argv or []).index(flag)
    except ValueError:
        return None
    if index + 1 >= len(argv or []):
        return None
    return list(argv or [])[index + 1]


def _state_dir_for_db(db_path):
    try:
        if db_path not in (None, ""):
            return Path(str(db_path)).expanduser().resolve().parent
        return inkdrop_runtime_config.state_dir()
    except Exception:
        return inkdrop_runtime_config.state_dir()


def _read_json(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
        parsed = json.loads(text or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _table_exists(con, table):
    row = con.execute(
        "select 1 from sqlite_master where type='table' and name=? limit 1",
        (str(table or ""),),
    ).fetchone()
    return bool(row)


def _reconciliation_db_path_for_state(db_path, environ=None):
    environ = environ if isinstance(environ, dict) else os.environ
    explicit = _env(environ, "INKDROP_SOURCE_WORKER_RECONCILIATION_DB", "")
    if explicit:
        return Path(explicit)
    if not db_path:
        return inkdrop_runtime_config.imported_files_db_path(environ)
    return _state_dir_for_db(db_path) / RECONCILIATION_DB_NAME


def _query_state_ready_import_rows(db_path):
    db_path = str(db_path or "").strip()
    if not db_path or not Path(db_path).exists():
        return []
    status_placeholders = ",".join("?" for _ in INKDROP_STATE_IMPORT_READY_STATUSES)
    db_uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(db_uri, uri=True, timeout=1.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("pragma query_only = 1")
        con.execute("pragma busy_timeout = 500")
        if not (_table_exists(con, "queue_items") and _table_exists(con, "download_tasks")):
            return []
        rows = con.execute(
            f"""
            select q.id as queue_id,
                   dt.id as download_task_id
            from queue_items q
            join download_tasks dt on dt.queue_id=q.id
            where q.active=1
              and lower(coalesce(q.state, ''))='importing'
              and (
                lower(coalesce(q.current_source, '')) in ('download_client','qbittorrent','sabnzbd')
                or lower(coalesce(dt.download_client, '')) in (
                    'qbittorrent','sabnzbd','inkdrop_direct','inkdrop_page_pack',
                    'inkdrop_external_tool','inkdrop_local_pack'
                )
              )
              and lower(coalesce(dt.state, ''))='import_ready'
              and lower(coalesce(dt.status, '')) in ({status_placeholders})
              and nullif(trim(coalesce(dt.local_path, '')), '') is not null
              and (
                lower(coalesce(dt.local_path, '')) glob '*.cbz'
                or lower(coalesce(dt.local_path, '')) glob '*.cbr'
                or lower(coalesce(dt.local_path, '')) glob '*.pdf'
              )
            order by coalesce(dt.updated_at, dt.completed_at, dt.started_at, q.updated_at, 0) desc,
                     dt.id desc
            """,
            tuple(INKDROP_STATE_IMPORT_READY_STATUSES),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def _latest_reconciliation_states(reconciliation_db_path, queue_ids, download_task_ids):
    path = Path(str(reconciliation_db_path or ""))
    if not path.exists():
        return {}, {}, "reconciliation_db_missing"
    queue_ids = [str(value) for value in queue_ids or [] if str(value or "").strip()]
    download_task_ids = [str(value) for value in download_task_ids or [] if str(value or "").strip()]
    if not queue_ids and not download_task_ids:
        return {}, {}, ""
    clauses = []
    params = []
    if queue_ids:
        clauses.append(f"inkdrop_queue_id in ({','.join('?' for _ in queue_ids)})")
        params.extend(queue_ids)
    if download_task_ids:
        clauses.append(f"inkdrop_download_task_id in ({','.join('?' for _ in download_task_ids)})")
        params.extend(download_task_ids)
    db_uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(db_uri, uri=True, timeout=1.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("pragma query_only = 1")
        con.execute("pragma busy_timeout = 500")
        if not _table_exists(con, "download_reconciliation"):
            return {}, {}, "download_reconciliation_missing"
        rows = con.execute(
            f"""
            select inkdrop_queue_id,
                   inkdrop_download_task_id,
                   lower(coalesce(lifecycle_state, '')) as lifecycle_state,
                   coalesce(reason, '') as reason,
                   coalesce(updated_at, 0) as updated_at
            from download_reconciliation
            where {' or '.join(clauses)}
            order by coalesce(updated_at, 0) desc
            """,
            tuple(params),
        ).fetchall()
    finally:
        con.close()
    by_queue = {}
    by_task = {}
    for row in rows:
        item = dict(row)
        task_id = str(item.get("inkdrop_download_task_id") or "").strip()
        queue_id = str(item.get("inkdrop_queue_id") or "").strip()
        if task_id and task_id not in by_task:
            by_task[task_id] = item
        if queue_id and queue_id not in by_queue:
            by_queue[queue_id] = item
    return by_queue, by_task, ""


def state_ready_import_summary(db_path, *, environ=None):
    rows = _query_state_ready_import_rows(db_path)
    by_queue = {}
    for row in rows:
        queue_id = str(row.get("queue_id") or "").strip()
        if queue_id and queue_id not in by_queue:
            by_queue[queue_id] = row
    state_count = len(by_queue)
    if state_count <= 0:
        return {
            "ready_import_count": 0,
            "state_ready_import_count": 0,
            "reconciliation_ready_import_count": 0,
            "reconciliation_importing_count": 0,
            "reconciliation_unmatched_ready_import_count": 0,
            "reconciliation_excluded_ready_import_count": 0,
            "reconciliation_excluded_states": {},
            "reconciliation_error": "",
        }
    reconciliation_db_path = _reconciliation_db_path_for_state(db_path, environ=environ)
    by_recon_queue, by_recon_task, recon_error = _latest_reconciliation_states(
        reconciliation_db_path,
        [row.get("queue_id") for row in by_queue.values()],
        [row.get("download_task_id") for row in by_queue.values()],
    )
    if recon_error:
        return {
            "ready_import_count": state_count,
            "state_ready_import_count": state_count,
            "reconciliation_ready_import_count": 0,
            "reconciliation_importing_count": 0,
            "reconciliation_unmatched_ready_import_count": state_count,
            "reconciliation_excluded_ready_import_count": 0,
            "reconciliation_excluded_states": {},
            "reconciliation_error": recon_error,
            "reconciliation_db_path": str(reconciliation_db_path),
        }
    actionable = 0
    ready_to_import = 0
    importing = 0
    unmatched = 0
    excluded = 0
    excluded_states = {}
    for queue_id, row in by_queue.items():
        task_id = str(row.get("download_task_id") or "").strip()
        recon = by_recon_task.get(task_id) if task_id else None
        if not recon:
            recon = by_recon_queue.get(queue_id)
        if not recon:
            actionable += 1
            unmatched += 1
            continue
        state = str(recon.get("lifecycle_state") or "").strip().lower()
        if state in INKDROP_IMPORT_BACKLOG_ACTIONABLE_RECONCILIATION_STATES:
            actionable += 1
            if state == "ready_to_import":
                ready_to_import += 1
            elif state == "importing":
                importing += 1
            continue
        excluded += 1
        excluded_states[state or "unknown"] = int(excluded_states.get(state or "unknown") or 0) + 1
    return {
        "ready_import_count": int(actionable),
        "state_ready_import_count": int(state_count),
        "reconciliation_ready_import_count": int(ready_to_import),
        "reconciliation_importing_count": int(importing),
        "reconciliation_unmatched_ready_import_count": int(unmatched),
        "reconciliation_excluded_ready_import_count": int(excluded),
        "reconciliation_excluded_states": excluded_states,
        "reconciliation_error": "",
        "reconciliation_db_path": str(reconciliation_db_path),
    }


def state_ready_import_count(db_path):
    return int(state_ready_import_summary(db_path).get("ready_import_count") or 0)


def active_pack_import_state(db_path=None, *, state_dir=None, now=None, active_seconds=PACK_IMPORT_ACTIVE_SECONDS):
    now = time.time() if now is None else float(now)
    state_dir = Path(state_dir) if state_dir else _state_dir_for_db(db_path)
    payload = _read_json(state_dir / PACK_REVIEW_STATE_NAME)
    active = payload.get("active") if isinstance(payload.get("active"), dict) else {}
    record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
    lifecycle = str(
        payload.get("lifecycle_state")
        or active.get("lifecycle_state")
        or active.get("status")
        or record.get("status")
        or ""
    ).strip().lower()
    status = str(active.get("status") or record.get("status") or "").strip().lower()
    updated_at = _float(
        payload.get("last_import_update_at")
        or active.get("updated_at")
        or active.get("started_at")
        or record.get("updated_at")
        or record.get("started_at"),
        0.0,
    )
    active_states = {"starting", "running", "sent", "importing", "busy", "ready_import"}
    fresh = updated_at <= 0 or now - updated_at <= max(1, float(active_seconds or PACK_IMPORT_ACTIVE_SECONDS))
    is_active = bool(fresh and (lifecycle in active_states or status in active_states or payload.get("blocks_new")))
    return {
        "active": is_active,
        "lifecycle_state": lifecycle,
        "status": status,
        "updated_at": updated_at,
        "age_seconds": max(0.0, now - updated_at) if updated_at else None,
        "review_id": active.get("review_id") or record.get("review_id"),
        "pack_path": active.get("pack_path") or payload.get("pack_path"),
        "series": active.get("series") or record.get("series"),
        "reason": payload.get("reason"),
    }


def import_backlog_gate(db_path, *, environ=None, now=None):
    environ = environ if isinstance(environ, dict) else os.environ
    threshold = _int(
        _env(environ, "INKDROP_SOURCE_WORKER_IMPORT_BACKLOG_PRIORITY_MIN", DEFAULT_IMPORT_BACKLOG_PRIORITY_MIN),
        DEFAULT_IMPORT_BACKLOG_PRIORITY_MIN,
        minimum=1,
    )
    ready = 0
    ready_error = ""
    ready_summary = {}
    try:
        ready_summary = state_ready_import_summary(db_path, environ=environ)
        ready = int(ready_summary.get("ready_import_count") or 0)
    except Exception as exc:
        ready_error = f"{type(exc).__name__}: {exc}"
        ready_summary = {
            "ready_import_count": 0,
            "state_ready_import_count": 0,
            "reconciliation_ready_import_count": 0,
            "reconciliation_importing_count": 0,
            "reconciliation_unmatched_ready_import_count": 0,
            "reconciliation_excluded_ready_import_count": 0,
            "reconciliation_excluded_states": {},
            "reconciliation_error": ready_error,
        }
    pack = active_pack_import_state(db_path, now=now)
    active = bool(int(ready or 0) >= threshold or pack.get("active"))
    sources = []
    if int(ready or 0) >= threshold:
        sources.append("ready_import_backlog")
    if pack.get("active"):
        sources.append("active_pack_import")
    return {
        "active": active,
        "reason": "ready_import_backlog_priority",
        "ready_import_count": int(ready or 0),
        "state_ready_import_count": int(ready_summary.get("state_ready_import_count") or 0),
        "threshold": threshold,
        "ready_import_error": ready_error,
        "ready_import_summary": ready_summary,
        "reconciliation_ready_import_count": int(ready_summary.get("reconciliation_ready_import_count") or 0),
        "reconciliation_importing_count": int(ready_summary.get("reconciliation_importing_count") or 0),
        "reconciliation_unmatched_ready_import_count": int(
            ready_summary.get("reconciliation_unmatched_ready_import_count") or 0
        ),
        "reconciliation_excluded_ready_import_count": int(
            ready_summary.get("reconciliation_excluded_ready_import_count") or 0
        ),
        "reconciliation_excluded_states": ready_summary.get("reconciliation_excluded_states") or {},
        "reconciliation_error": ready_summary.get("reconciliation_error") or "",
        "reconciliation_db_path": ready_summary.get("reconciliation_db_path") or "",
        "pack_import_active": bool(pack.get("active")),
        "pack_import": pack,
        "gate_sources": sources,
    }


def source_worker_should_honor_import_backlog(mode, environ=None):
    environ = environ if isinstance(environ, dict) else os.environ
    if _falsy(_env(environ, "INKDROP_SOURCE_WORKER_IMPORT_BACKLOG_GATE", "1")):
        return False
    mode = mode if isinstance(mode, dict) else {}
    return bool(
        mode.get("execute_jobs")
        or mode.get("write_enabled")
        or mode.get("stage_direct")
        or mode.get("handoff_download_clients")
    )


def build_source_worker_cli_argv_from_env(environ=None, *, db_path=None, full_output=None):
    environ = environ if isinstance(environ, dict) else os.environ
    argv = [str(db_path or _env(environ, "INKDROP_SOURCE_WORKER_DB", inkdrop_runtime_config.state_db_path(environ)))]

    _append_repeated(argv, "--queue-id", _split_csv(_env(environ, "INKDROP_SOURCE_WORKER_QUEUE_IDS", "")))
    _append_repeated(argv, "--provider-id", _split_csv(_env(environ, "INKDROP_SOURCE_WORKER_PROVIDER_IDS", "")))
    _append_repeated(argv, "--state", _split_csv(_env(environ, "INKDROP_SOURCE_WORKER_STATES", "")))

    for env_key, flag in (
        ("INKDROP_SOURCE_WORKER_QUEUE_LIMIT", "--queue-limit"),
        ("INKDROP_SOURCE_WORKER_JOB_LIMIT", "--job-limit"),
        ("INKDROP_SOURCE_WORKER_ATTEMPT_COOLDOWN_SECONDS", "--attempt-cooldown-seconds"),
        ("INKDROP_SOURCE_WORKER_PROVIDER_TIMEOUT_WINDOW_SECONDS", "--provider-timeout-window-seconds"),
        ("INKDROP_SOURCE_WORKER_PROVIDER_TIMEOUT_THRESHOLD", "--provider-timeout-threshold"),
        ("INKDROP_SOURCE_WORKER_PROVIDER_TIMEOUT_COOLDOWN_SECONDS", "--provider-timeout-cooldown-seconds"),
        ("INKDROP_SOURCE_WORKER_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS", "--provider-fetch-failure-window-seconds"),
        ("INKDROP_SOURCE_WORKER_PROVIDER_FETCH_FAILURE_THRESHOLD", "--provider-fetch-failure-threshold"),
        ("INKDROP_SOURCE_WORKER_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS", "--provider-fetch-failure-cooldown-seconds"),
        ("INKDROP_SOURCE_WORKER_COMIC_PACK_PROWLARR_CHILD_LANE_LIMIT", "--comic-pack-child-lane-limit"),
        ("INKDROP_SOURCE_WORKER_RUN_LIMIT", "--run-limit"),
        ("INKDROP_SOURCE_WORKER_ELIGIBLE_LIMIT", "--eligible-limit"),
        ("INKDROP_SOURCE_WORKER_MAX_RUN_SECONDS", "--max-run-seconds"),
        ("INKDROP_SOURCE_WORKER_TIMEOUT_SECONDS", "--timeout-seconds"),
        ("INKDROP_SOURCE_WORKER_MAX_BYTES", "--max-bytes"),
        ("INKDROP_SOURCE_WORKER_DIRECT_TIMEOUT_SECONDS", "--direct-timeout-seconds"),
        ("INKDROP_SOURCE_WORKER_DIRECT_MAX_BYTES", "--direct-max-bytes"),
        ("INKDROP_SOURCE_MEMORY_COOLDOWN_SECONDS", "--source-memory-cooldown-seconds"),
        ("INKDROP_SOURCE_WORKER_RECORD_LOCK_RETRY_ATTEMPTS", "--record-lock-retry-attempts"),
        ("INKDROP_SOURCE_WORKER_RECORD_LOCK_RETRY_INITIAL_DELAY_SECONDS", "--record-lock-retry-initial-delay"),
        ("INKDROP_SOURCE_WORKER_NOW", "--now"),
    ):
        _append_value(argv, flag, _env(environ, env_key))

    if _truthy(_env(environ, "INKDROP_SOURCE_WORKER_INCLUDE_WAITING")):
        argv.append("--include-waiting")
    if _truthy(_env(environ, "INKDROP_SOURCE_WORKER_INCLUDE_BLOCKED")):
        argv.append("--include-blocked")
    if _truthy(_env(environ, "INKDROP_SOURCE_WORKER_EXCLUDE_OPERATOR")):
        argv.append("--exclude-operator")
    if _truthy(_env(environ, "INKDROP_SOURCE_WORKER_EXECUTE")):
        argv.append("--execute")
    if _truthy(_env(environ, "INKDROP_SOURCE_WORKER_WRITE")):
        argv.append("--write")
    if _truthy(_env(environ, "INKDROP_SOURCE_WORKER_STAGE_DIRECT")):
        argv.append("--stage-direct")
    if _truthy(_env(environ, "INKDROP_SOURCE_WORKER_STAGE_MANAGED_FOLDERS")):
        argv.append("--stage-managed-folders")
    if _truthy(_env(environ, "INKDROP_SOURCE_WORKER_HANDOFF_DOWNLOAD_CLIENTS")):
        argv.append("--handoff-download-clients")
    if _truthy(_env(environ, "INKDROP_SOURCE_WORKER_ALLOW_NETWORK")):
        argv.append("--allow-network")
    if _truthy(_env(environ, "INKDROP_SOURCE_WORKER_ALLOW_DIRECT_NETWORK")):
        argv.append("--allow-direct-network")
    if full_output is True or (full_output is None and _truthy(_env(environ, "INKDROP_SOURCE_WORKER_FULL_OUTPUT"))):
        argv.append("--full-output")

    _append_value(argv, "--staging-root", _env(environ, "INKDROP_SOURCE_WORKER_STAGING_ROOT"))
    _append_value(argv, "--candidate-headers-json", _env(environ, "INKDROP_SOURCE_WORKER_CANDIDATE_HEADERS_JSON"))
    _append_value(argv, "--operator-payloads-json", _env(environ, "INKDROP_SOURCE_WORKER_OPERATOR_PAYLOADS_JSON"))
    _append_value(argv, "--source-memory-db", _env(environ, "INKDROP_SOURCE_MEMORY_DB"))
    _append_repeated(argv, "--allowed-host", _split_csv(_env(environ, "INKDROP_SOURCE_WORKER_ALLOWED_HOSTS", "")))
    _append_repeated(argv, "--direct-allowed-host", _split_csv(_env(environ, "INKDROP_SOURCE_WORKER_DIRECT_ALLOWED_HOSTS", "")))
    return argv


def service_mode_from_argv(cli_argv):
    source_memory_value = _cli_value(cli_argv, "--source-memory-db")
    return {
        "db_path": str(cli_argv[0]) if cli_argv else "",
        "execute_jobs": "--execute" in cli_argv,
        "write_enabled": "--write" in cli_argv,
        "stage_direct": "--stage-direct" in cli_argv,
        "stage_managed_folders": "--stage-managed-folders" in cli_argv,
        "handoff_download_clients": "--handoff-download-clients" in cli_argv,
        "source_network_enabled": "--allow-network" in cli_argv,
        "direct_network_enabled": "--allow-direct-network" in cli_argv,
        "provider_filter_count": cli_argv.count("--provider-id"),
        "queue_filter_count": cli_argv.count("--queue-id"),
        "max_run_seconds": _cli_value(cli_argv, "--max-run-seconds") or "",
        "provider_timeout_window_seconds": _cli_value(cli_argv, "--provider-timeout-window-seconds") or "",
        "provider_timeout_threshold": _cli_value(cli_argv, "--provider-timeout-threshold") or "",
        "provider_timeout_cooldown_seconds": _cli_value(cli_argv, "--provider-timeout-cooldown-seconds") or "",
        "provider_fetch_failure_window_seconds": _cli_value(cli_argv, "--provider-fetch-failure-window-seconds") or "",
        "provider_fetch_failure_threshold": _cli_value(cli_argv, "--provider-fetch-failure-threshold") or "",
        "provider_fetch_failure_cooldown_seconds": _cli_value(cli_argv, "--provider-fetch-failure-cooldown-seconds") or "",
        "comic_pack_child_lane_limit": _cli_value(cli_argv, "--comic-pack-child-lane-limit") or "",
        "record_lock_retry_attempts": _cli_value(cli_argv, "--record-lock-retry-attempts") or "",
        "record_lock_retry_initial_delay": _cli_value(cli_argv, "--record-lock-retry-initial-delay") or "",
        "source_memory_enabled": bool(source_memory_value or (str(cli_argv[0]) if cli_argv else "")),
        "source_memory_defaulted": not bool(source_memory_value),
    }


def run_source_worker_service(argv=None, *, environ=None, source_http_get=None, direct_http_get=None):
    parser = argparse.ArgumentParser(description="Run one guarded InkDrop source-worker service pass.")
    parser.add_argument("--db-path", help="Override INKDROP_SOURCE_WORKER_DB for this pass.")
    parser.add_argument("--full-output", action="store_true", help="Forward redacted full output from the source worker CLI.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON in command mode.")
    args = parser.parse_args(argv)

    environ = environ if isinstance(environ, dict) else os.environ
    cli_argv = build_source_worker_cli_argv_from_env(
        environ,
        db_path=args.db_path,
        full_output=True if args.full_output else None,
    )
    mode = service_mode_from_argv(cli_argv)
    gate_now_value = _cli_value(cli_argv, "--now")
    gate_now = _float(gate_now_value, time.time()) if gate_now_value not in (None, "") else None
    gate = import_backlog_gate(cli_argv[0], environ=environ, now=gate_now)
    if gate.get("active") and source_worker_should_honor_import_backlog(mode, environ):
        return {
            "source_worker_service_contract_version": CONTRACT_VERSION,
            "ok": True,
            "deferred": True,
            "reason": "ready_import_backlog_priority",
            "mode": mode,
            "cli_argv": cli_argv,
            "import_backlog_gate": gate,
            "result": {
                "ok": True,
                "deferred": True,
                "reason": "ready_import_backlog_priority",
                "summary": {
                    "eligible": 0,
                    "selected": 0,
                    "executed": 0,
                    "ready_import_count": gate.get("ready_import_count"),
                    "pack_import_active": gate.get("pack_import_active"),
                },
                "safety": {
                    "mutates_database": False,
                    "mutates_download_client": False,
                    "mutates_filesystem": False,
                },
            },
        }
    result = cli.run_source_worker_cli(
        cli_argv,
        source_http_get=source_http_get,
        direct_http_get=direct_http_get,
        environ=environ,
    )
    return {
        "source_worker_service_contract_version": CONTRACT_VERSION,
        "ok": bool(result.get("ok")),
        "mode": mode,
        "cli_argv": cli_argv,
        "import_backlog_gate": gate,
        "result": result,
    }


def main(argv=None):
    try:
        payload = run_source_worker_service(argv)
    except ValueError as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    indent = 2 if "--pretty" in (argv if argv is not None else sys.argv[1:]) else None
    print(json.dumps(payload, sort_keys=True, indent=indent, ensure_ascii=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
