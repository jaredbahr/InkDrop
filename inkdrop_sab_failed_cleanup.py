#!/usr/bin/env python3
"""Clean old InkDrop-owned SAB history rows.

This intentionally handles only comic/manga NZB history entries that InkDrop can
classify safely. Failed rows are removed after a cooldown. Completed rows are
removed only when their archive payload is already recorded in InkDrop's import
ledger, so SAB history does not pile up after successful imports.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import inkdrop_runtime_config


def script_path(name: str, remote_path: str | None = None, *, env_var: str | None = None) -> Path:
    local = Path(__file__).resolve().with_name(name)
    if local.exists():
        return local
    if env_var:
        configured = os.environ.get(env_var)
        if configured:
            return Path(configured)
    return Path(remote_path) if remote_path else local


STATE_DIR = inkdrop_runtime_config.state_dir()
STATUS_PATH = inkdrop_runtime_config.sab_failed_cleanup_status_path()
SAB_HELPER = script_path("sab_rescue_server.py", env_var="INKDROP_SAB_RESCUE_SCRIPT")
RECONCILE_SCRIPT = script_path("inkdrop_reconcile_imports.py", env_var="INKDROP_RECONCILE_SCRIPT")
DB_PATH = inkdrop_runtime_config.imported_files_db_path()
STATE_DB_PATH = inkdrop_runtime_config.state_db_path()

INKDROP_CATEGORIES = {"comics", "manga", "mylar", "kapowarr"}
# How far back a download_tasks row can still prove SAB-job ownership.
OWNERSHIP_MAX_AGE_SECONDS = float(os.environ.get("INKDROP_SAB_OWNERSHIP_MAX_AGE_SECONDS", 30 * 24 * 3600))
# Diagnostic only. These substrings used to authorize deletion, which meant any
# unrelated SAB download whose name merely contained a generic word such as
# "saga", "chew", or "prophet" was treated as InkDrop-owned and removed with
# del_files=1. Ownership is now proven against InkDrop's own download_tasks
# rows; this set survives solely so the status report can show how many rows the
# retired heuristic would have claimed without durable evidence.
LEGACY_TITLE_HINTS = {
    "chew",
    "prophet",
    "saga",
}
OWNERSHIP_ID_KEYS = (
    "external_id",
    "externalId",
    "client_external_id",
    "clientExternalId",
    "client_id",
    "clientId",
    "download_id",
    "downloadId",
    "nzo_id",
    "nzoId",
)
OWNERSHIP_ID_LIST_KEYS = ("nzo_ids", "nzoIds")
FAILED_CLASSES = {
    "duplicate_nzb",
    "url_fetch_failed",
    "incomplete",
}
ARCHIVE_SUFFIXES = {".cbz", ".cbr", ".zip", ".rar", ".7z", ".pdf", ".epub"}
DEFAULT_SAB_CLEANUP_SETTINGS = {
    # Fail closed: these defaults apply exactly when the stored settings could
    # NOT be read (missing DB, missing table, unreadable row). A cleanup that
    # deletes payload files must not default to deleting because its
    # configuration went missing -- the operator's stored choice (explicit
    # True in provider_configs) is what turns deletion on.
    "remove_completed_downloads": False,
    "remove_failed_downloads": False,
    "completed_history_min_age_hours": 0.25,
    "failed_history_min_age_hours": 2.0,
    "sab_history_limit": 500,
    "max_failed_history_delete": 50,
    "max_completed_history_delete": 50,
}


def iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_sab_helper():
    spec = importlib.util.spec_from_file_location("sab_rescue_server", SAB_HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SAB_HELPER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def boolish(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def bounded_number(value: Any, default: float, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not number or number < minimum:
        return minimum
    return number


def bounded_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, number)


def load_sab_cleanup_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SAB_CLEANUP_SETTINGS)
    settings["source"] = "defaults"
    if not STATE_DB_PATH.exists():
        return settings
    try:
        con = sqlite3.connect(STATE_DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            exists = con.execute(
                "select 1 from sqlite_master where type='table' and name='provider_configs'"
            ).fetchone()
            if not exists:
                return settings
            row = con.execute(
                "select enabled, settings_json from provider_configs where id='sabnzbd' limit 1"
            ).fetchone()
            if not row:
                return settings
            try:
                stored = json.loads(row["settings_json"] or "{}")
            except ValueError:
                stored = {}
            if not isinstance(stored, dict):
                stored = {}
            settings.update(
                {
                    "remove_completed_downloads": boolish(
                        stored.get("remove_completed_downloads"),
                        settings["remove_completed_downloads"],
                    ),
                    "remove_failed_downloads": boolish(
                        stored.get("remove_failed_downloads"),
                        settings["remove_failed_downloads"],
                    ),
                    "completed_history_min_age_hours": bounded_number(
                        stored.get("completed_history_min_age_hours"),
                        float(settings["completed_history_min_age_hours"]),
                    ),
                    "failed_history_min_age_hours": bounded_number(
                        stored.get("failed_history_min_age_hours"),
                        float(settings["failed_history_min_age_hours"]),
                    ),
                    "sab_history_limit": bounded_int(
                        stored.get("sab_history_limit"),
                        int(settings["sab_history_limit"]),
                        minimum=1,
                    ),
                    "max_failed_history_delete": bounded_int(
                        stored.get("max_failed_history_delete"),
                        int(settings["max_failed_history_delete"]),
                    ),
                    "max_completed_history_delete": bounded_int(
                        stored.get("max_completed_history_delete"),
                        int(settings["max_completed_history_delete"]),
                    ),
                    "provider_enabled": bool(row["enabled"]),
                    "source": "provider_config",
                }
            )
            return settings
        finally:
            con.close()
    except sqlite3.Error as exc:
        settings["source"] = "defaults_after_settings_error"
        settings["settings_error"] = f"{type(exc).__name__}: {exc}"
        return settings


def text_blob(slot: dict[str, Any]) -> str:
    fields = (
        "name",
        "filename",
        "nzb_name",
        "status",
        "fail_message",
        "fail_msg",
        "script_log",
        "action_line",
        "stage_log",
        "url",
        "download_url",
    )
    return " ".join(str(slot.get(key) or "") for key in fields)


def slot_name(slot: dict[str, Any]) -> str:
    return str(slot.get("name") or slot.get("filename") or slot.get("nzb_name") or "")


def slot_category(slot: dict[str, Any]) -> str:
    return str(slot.get("category") or slot.get("cat") or "").strip().lower()


def normalize_drivepool_path(value: str | None) -> str:
    if not value:
        return ""
    path = str(value).strip().replace("\\", "/")
    lowered = path.lower()
    prefixes = []
    for item in str(os.environ.get("INKDROP_SAB_PATH_MAPPINGS") or os.environ.get("INKDROP_UNC_PATH_MAPPINGS") or "").split(";"):
        if "=" not in item:
            continue
        prefix, replacement = item.split("=", 1)
        prefix = prefix.strip().replace("\\", "/").lower().rstrip("/") + "/"
        replacement = replacement.strip().replace("\\", "/").rstrip("/") + "/"
        if prefix and replacement:
            prefixes.append((prefix, replacement))
    for prefix, replacement in prefixes:
        if lowered.startswith(prefix):
            path = replacement + path[len(prefix):]
            break
    while "//" in path.replace("://", "__URLSEP__"):
        protected = path.replace("://", "__URLSEP__")
        protected = protected.replace("//", "/")
        path = protected.replace("__URLSEP__", "://")
    return path.rstrip("/")


def is_archive_path(path: Path) -> bool:
    lower = path.name.lower()
    return path.suffix.lower() in ARCHIVE_SUFFIXES or lower.endswith(".cbz.zip")


def load_imported_source_paths() -> set[str]:
    if not DB_PATH.exists():
        return set()
    conn = sqlite3.connect(DB_PATH)
    try:
        columns = {row[1] for row in conn.execute("pragma table_info(imported_files)").fetchall()}
        source_col = "source_path" if "source_path" in columns else "source"
        dest_col = "dest_path" if "dest_path" in columns else "dest"
        paths: set[str] = set()
        for source, dest in conn.execute(f"select {source_col}, {dest_col} from imported_files"):
            source_norm = normalize_drivepool_path(source)
            dest_norm = normalize_drivepool_path(dest)
            if source_norm and (not dest_norm or Path(dest_norm).exists()):
                paths.add(source_norm)
        return paths
    finally:
        conn.close()


def local_archive_payload(storage: str) -> list[str]:
    normalized = normalize_drivepool_path(storage)
    if not normalized:
        return []
    path = Path(normalized)
    if path.is_file() and is_archive_path(path):
        return [str(path)]
    if not path.is_dir():
        return []
    archives: list[str] = []
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        if is_archive_path(child):
            archives.append(str(child))
    return sorted(archives)


def normalize_external_id(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, bool):
        return ""
    return str(value).strip().lower()


def collect_ownership_ids(payload: Any, into: dict[str, str], task_id: str) -> None:
    """Record every external job identifier a download_tasks row carries."""
    if not isinstance(payload, dict):
        return
    for key in OWNERSHIP_ID_KEYS:
        normalized = normalize_external_id(payload.get(key))
        if normalized:
            into.setdefault(normalized, task_id)
    for key in OWNERSHIP_ID_LIST_KEYS:
        values = payload.get(key)
        if isinstance(values, (list, tuple)):
            for value in values:
                normalized = normalize_external_id(value)
                if normalized:
                    into.setdefault(normalized, task_id)


def load_owned_external_ids() -> tuple[dict[str, str], dict[str, Any]]:
    """Map every SAB job id InkDrop durably recorded to the task that owns it.

    Returns the map plus a diagnostic block, so an unreadable or absent state DB
    is visible in the status report instead of silently authorizing deletions.
    """
    diagnostics: dict[str, Any] = {"source": "download_tasks", "task_rows": 0}
    owned: dict[str, str] = {}
    if not STATE_DB_PATH.exists():
        diagnostics["source"] = "unavailable"
        diagnostics["reason"] = "state_db_missing"
        return owned, diagnostics
    try:
        con = sqlite3.connect(f"file:{STATE_DB_PATH}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            exists = con.execute(
                "select 1 from sqlite_master where type='table' and name='download_tasks'"
            ).fetchone()
            if not exists:
                diagnostics["source"] = "unavailable"
                diagnostics["reason"] = "download_tasks_table_missing"
                return owned, diagnostics
            rows = con.execute(
                # SAB tasks only, on purpose: this map AUTHORIZES del_files=1
                # deletion of a SAB job's payload. A qbittorrent hash or slskd
                # id that happens to collide with a foreign SAB job's nzo id
                # must never prove InkDrop owns that SAB job -- only a task
                # InkDrop actually handed to SABnzbd can.
                # Stale or superseded tasks stop proving ownership: SAB nzo
                # ids are per-instance and get reused, so a 180-day-old
                # superseded row from a long-gone SAB install must not
                # authorize deleting a CURRENT job that happens to share the
                # id (Pass 14 fixture). The window is generous -- this job
                # cleans hours-to-days-old failures, not archaeology.
                "select id, external_id, download_client, raw_json from download_tasks"
                " where lower(coalesce(download_client, '')) in ('sabnzbd', 'sab')"
                "   and lower(coalesce(state, '')) not in ('superseded', 'retired', 'superseded_duplicate')"
                "   and coalesce(updated_at, completed_at, started_at, 0) > ?",
                (time.time() - OWNERSHIP_MAX_AGE_SECONDS,),
            ).fetchall()
            for row in rows:
                diagnostics["task_rows"] += 1
                task_id = str(row["id"] or "")
                normalized = normalize_external_id(row["external_id"])
                if normalized:
                    owned.setdefault(normalized, task_id)
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                except ValueError:
                    continue
                collect_ownership_ids(raw, owned, task_id)
                for nested_key in ("download_client_snapshot", "handoff", "outcome"):
                    collect_ownership_ids(raw.get(nested_key), owned, task_id)
        finally:
            con.close()
    except sqlite3.Error as exc:
        diagnostics["source"] = "unavailable"
        diagnostics["reason"] = f"{type(exc).__name__}: {exc}"
        return {}, diagnostics
    diagnostics["owned_id_count"] = len(owned)
    return owned, diagnostics


def slot_external_ids(slot: dict[str, Any]) -> list[str]:
    values = [slot.get("nzo_id"), slot.get("nzoid"), slot.get("nzoId"), slot.get("id")]
    seen: list[str] = []
    for value in values:
        normalized = normalize_external_id(value)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def matches_legacy_title_hint(slot: dict[str, Any]) -> bool:
    """Diagnostic only - never authorizes deletion. See LEGACY_TITLE_HINTS."""
    blob = text_blob(slot).lower()
    return any(hint in blob for hint in LEGACY_TITLE_HINTS)


def ownership_evidence(slot: dict[str, Any], owned_external_ids: dict[str, str]) -> dict[str, Any]:
    """Decide whether InkDrop can prove it created this SAB job.

    Deletion here removes the payload from disk, so ownership must come from a
    durable InkDrop record: the external job id we stored when we handed the
    download off, or a category InkDrop itself assigns. Nothing is inferred from
    the release title.
    """
    for external_id in slot_external_ids(slot):
        task_id = owned_external_ids.get(external_id)
        if task_id is not None:
            return {
                "owned": True,
                "ownership_source": "download_task_external_id",
                "owning_task_id": task_id,
                "matched_external_id": external_id,
            }
    category = slot_category(slot)
    if category in INKDROP_CATEGORIES:
        # Diagnostic only, never authorization: these are generic category
        # names -- two of them are literally other tools' names (mylar,
        # kapowarr) -- so any unrelated SAB job sharing one would have its
        # payload deleted with del_files=1. Worse, when the state DB is
        # unreadable the external-id map above comes back empty, which used
        # to leave this name match as the SOLE authorizer: a DB hiccup failed
        # toward more deletion. Ownership now requires the durable
        # download_tasks external-id record, same retirement the title-hint
        # heuristic got.
        return {
            "owned": False,
            "ownership_source": None,
            "unproven_category_match": category,
        }
    return {"owned": False, "ownership_source": None}


def is_inkdrop_slot(slot: dict[str, Any], owned_external_ids: dict[str, str]) -> bool:
    return bool(ownership_evidence(slot, owned_external_ids).get("owned"))


def failure_class(slot: dict[str, Any]) -> str | None:
    blob = text_blob(slot).lower()
    status = str(slot.get("status") or "").lower()
    if "failed" not in status and "fail" not in blob and "aborted" not in blob:
        return None
    if "duplicate nzb" in blob:
        return "duplicate_nzb"
    if "dognzb.cr/fail" in blob or "url fetching failed" in blob or "maximum retries" in blob:
        return "url_fetch_failed"
    if (
        "sabnzbd.org/not-complete" in blob
        or "cannot be completed" in blob
        or "not complete" in blob
        or "missing articles" in blob
        or ("article" in blob and "missing" in blob)
    ):
        return "incomplete"
    return None


def completed_epoch(slot: dict[str, Any]) -> float | None:
    for key in ("completed", "completed_at", "finish_time", "loaded"):
        value = slot.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def age_hours(slot: dict[str, Any], now_epoch: float) -> float | None:
    completed = completed_epoch(slot)
    if not completed:
        return None
    return max(0.0, (now_epoch - completed) / 3600.0)


def run_reconcile() -> dict[str, Any]:
    if os.environ.get("INKDROP_SAB_CLEANUP_RECONCILE", "").lower() not in {"1", "true", "yes"}:
        return {"skipped": True, "reason": "sab_cleanup_reconcile_disabled_by_default"}
    if not RECONCILE_SCRIPT.exists():
        return {"skipped": True, "reason": "reconcile_script_missing"}
    proc = subprocess.run(
        [sys.executable, str(RECONCILE_SCRIPT), "--write-status", "--json"],
        text=True,
        capture_output=True,
        timeout=180,
    )
    return {
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def candidate_from_slot(
    slot: dict[str, Any],
    now_epoch: float,
    owned_external_ids: dict[str, str],
) -> dict[str, Any] | None:
    kind = failure_class(slot)
    if kind not in FAILED_CLASSES:
        return None
    evidence = ownership_evidence(slot, owned_external_ids)
    if not evidence.get("owned"):
        return None
    nzo_id = str(slot.get("nzo_id") or slot.get("nzoid") or slot.get("id") or "")
    if not nzo_id:
        return None
    candidate = {
        "nzo_id": nzo_id,
        "name": slot_name(slot),
        "category": slot_category(slot),
        "candidate_type": "failed",
        "failure_class": kind,
        "fail_message": str(slot.get("fail_message") or slot.get("fail_msg") or ""),
        "age_hours": age_hours(slot, now_epoch),
        "storage": str(slot.get("storage") or slot.get("path") or ""),
    }
    candidate.update({key: value for key, value in evidence.items() if key != "owned"})
    return candidate


def completed_candidate_from_slot(
    slot: dict[str, Any],
    now_epoch: float,
    imported_sources: set[str],
    owned_external_ids: dict[str, str],
) -> dict[str, Any] | None:
    status = str(slot.get("status") or "").strip().lower()
    if status != "completed":
        return None
    evidence = ownership_evidence(slot, owned_external_ids)
    if not evidence.get("owned"):
        return None
    nzo_id = str(slot.get("nzo_id") or slot.get("nzoid") or slot.get("id") or "")
    if not nzo_id:
        return None
    storage = str(slot.get("storage") or "")
    archives = local_archive_payload(storage)
    if not archives:
        return None
    unimported = [path for path in archives if normalize_drivepool_path(path) not in imported_sources]
    if unimported:
        return None
    candidate = {
        "nzo_id": nzo_id,
        "name": slot_name(slot),
        "category": slot_category(slot),
        "candidate_type": "completed_imported",
        "failure_class": "",
        "age_hours": age_hours(slot, now_epoch),
        "storage": storage,
        "archive_count": len(archives),
    }
    candidate.update({key: value for key, value in evidence.items() if key != "owned"})
    return candidate


def collect_candidates(
    sab,
    limit: int,
    min_age_hours: float,
    completed_min_age_hours: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    now_epoch = time.time()
    imported_sources = load_imported_source_paths()
    owned_external_ids, ownership_diagnostics = load_owned_external_ids()
    history = sab.sab_api("history", limit=str(limit)).get("history", {})
    slots = history.get("slots") if isinstance(history, dict) else []
    all_candidates: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    all_completed_candidates: list[dict[str, Any]] = []
    completed_eligible: list[dict[str, Any]] = []
    retired_heuristic_only: list[dict[str, Any]] = []
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        candidate = candidate_from_slot(slot, now_epoch, owned_external_ids)
        if candidate:
            all_candidates.append(candidate)
            item_age = candidate.get("age_hours")
            if item_age is not None and float(item_age) >= min_age_hours:
                eligible.append(candidate)
        completed = completed_candidate_from_slot(
            slot, now_epoch, imported_sources, owned_external_ids
        )
        if completed:
            all_completed_candidates.append(completed)
            item_age = completed.get("age_hours")
            if item_age is not None and float(item_age) >= completed_min_age_hours:
                completed_eligible.append(completed)
        if not is_inkdrop_slot(slot, owned_external_ids) and matches_legacy_title_hint(slot):
            retired_heuristic_only.append(
                {
                    "nzo_id": str(slot.get("nzo_id") or slot.get("nzoid") or slot.get("id") or ""),
                    "name": slot_name(slot),
                    "category": slot_category(slot),
                    "status": str(slot.get("status") or ""),
                }
            )
    eligible.sort(key=lambda item: (float(item.get("age_hours") or 0), item.get("name") or ""), reverse=True)
    completed_eligible.sort(key=lambda item: (float(item.get("age_hours") or 0), item.get("name") or ""), reverse=True)
    ownership_diagnostics = dict(ownership_diagnostics)
    ownership_diagnostics["retired_title_hint_only_count"] = len(retired_heuristic_only)
    ownership_diagnostics["retired_title_hint_only_samples"] = retired_heuristic_only[:15]
    return all_candidates, eligible, all_completed_candidates, completed_eligible, ownership_diagnostics


def cleanup_history(
    sab,
    candidates: list[dict[str, Any]],
    dry_run: bool,
    max_delete: int,
    action_label: str,
) -> list[dict[str, Any]]:
    results = []
    for candidate in candidates[: max(0, max_delete)]:
        result = {
            "nzo_id": candidate["nzo_id"],
            "name": candidate.get("name"),
            "candidate_type": candidate.get("candidate_type"),
            "failure_class": candidate.get("failure_class"),
            "age_hours": candidate.get("age_hours"),
            "dry_run": dry_run,
        }
        if dry_run:
            dry_action = action_label
            if dry_action.startswith("deleted_"):
                dry_action = "delete_" + dry_action[len("deleted_"):]
            result["action"] = f"would_{dry_action}"
            results.append(result)
            continue
        try:
            api_result = sab.sab_api(
                "history",
                name="delete",
                value=candidate["nzo_id"],
                del_files="1",
            )
            result["action"] = action_label
            result["api_result"] = api_result
        except Exception as exc:  # noqa: BLE001 - status report needs the exact failure
            result["action"] = "delete_failed"
            result["error"] = f"{type(exc).__name__}: {exc}"
        results.append(result)
    return results


def write_status(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(STATUS_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATUS_PATH)


def unconfigured_adapter_status(cleanup_settings: dict[str, Any], reason: str) -> dict[str, Any]:
    # "Not configured" and "half configured" look the same from here, and they
    # are very different to the operator: someone who turned Remove Completed on
    # is waiting for history to shrink, and a cheerful idle status hides why it
    # never does.
    wants_cleanup = boolish(cleanup_settings.get("remove_completed_downloads"), False) or boolish(
        cleanup_settings.get("remove_failed_downloads"), False
    )
    if wants_cleanup:
        detail = (
            "Remove Completed/Remove Failed are on, but SABnzbd has no API key saved, "
            "so no history can be read or removed. Add the key in Settings > SABnzbd."
        )
    else:
        detail = "SABnzbd cleanup is idle because the optional SABnzbd adapter is not configured."
    return {
        "updated_at": iso(),
        "ok": True,
        "skipped": True,
        "skip_reason": reason,
        "cleanup_requested": bool(wants_cleanup),
        "detail": detail,
        "cleanup_settings_source": cleanup_settings.get("source"),
        "candidate_count": 0,
        "eligible_count": 0,
        "completed_candidate_count": 0,
        "completed_eligible_count": 0,
        "deleted_count": 0,
        "failed_deleted_count": 0,
        "completed_deleted_count": 0,
        "failed_count": 0,
        "results": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean old InkDrop-owned failed SAB NZB history rows")
    parser.add_argument("--dry-run", action="store_true", help="report eligible rows without deleting")
    parser.add_argument("--min-age-hours", type=float, default=None)
    parser.add_argument("--completed-min-age-hours", type=float, default=None)
    parser.add_argument("--history-limit", type=int, default=None)
    parser.add_argument("--max-delete", type=int, default=None)
    parser.add_argument("--max-completed-delete", type=int, default=None)
    parser.add_argument("--no-clear-failed", action="store_true")
    parser.add_argument("--no-clear-completed", action="store_true")
    parser.add_argument("--skip-reconcile", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cleanup_settings = load_sab_cleanup_settings()
    min_age_hours = (
        max(0.0, args.min_age_hours)
        if args.min_age_hours is not None
        else bounded_number(cleanup_settings.get("failed_history_min_age_hours"), 2.0)
    )
    completed_min_age_hours = (
        max(0.0, args.completed_min_age_hours)
        if args.completed_min_age_hours is not None
        else bounded_number(cleanup_settings.get("completed_history_min_age_hours"), 0.25)
    )
    history_limit = (
        max(1, args.history_limit)
        if args.history_limit is not None
        else bounded_int(cleanup_settings.get("sab_history_limit"), 500, minimum=1)
    )
    max_delete = (
        max(0, args.max_delete)
        if args.max_delete is not None
        else bounded_int(cleanup_settings.get("max_failed_history_delete"), 50)
    )
    max_completed_delete = (
        max(0, args.max_completed_delete)
        if args.max_completed_delete is not None
        else bounded_int(cleanup_settings.get("max_completed_history_delete"), 50)
    )
    clear_failed = boolish(cleanup_settings.get("remove_failed_downloads"), False) and not args.no_clear_failed
    clear_completed = boolish(cleanup_settings.get("remove_completed_downloads"), False) and not args.no_clear_completed

    # The stored provider row's enabled flag governs the whole adapter, and a
    # cleanup that deletes payload files is part of the adapter: disabling the
    # SABnzbd provider must disable this job too, not just the handoff path.
    if "provider_enabled" in cleanup_settings and not boolish(cleanup_settings.get("provider_enabled"), False):
        status = unconfigured_adapter_status(cleanup_settings, "sab_provider_disabled")
        write_status(status)
        print(json.dumps(status, indent=2, sort_keys=True))
        return

    sab = load_sab_helper()
    try:
        sab.read_sab_key()
    except RuntimeError as exc:
        if "api key is not configured" not in str(exc).lower():
            raise
        status = unconfigured_adapter_status(cleanup_settings, "adapter_not_configured")
        write_status(status)
        print(json.dumps(status, indent=2, sort_keys=True))
        return

    # Removal fully off means ZERO SAB network traffic: authenticating and
    # paging history just to run a suppressed delete loop is still touching a
    # service the operator turned off (Pass 14). Placed after the local
    # key-file check so a genuinely unconfigured adapter still reports
    # adapter_not_configured, which is the more useful diagnosis.
    if not clear_failed and not clear_completed:
        status = unconfigured_adapter_status(cleanup_settings, "sab_cleanup_disabled")
        write_status(status)
        print(json.dumps(status, indent=2, sort_keys=True))
        return
    reconcile = {"skipped": True, "reason": "dry_run"} if args.dry_run else (
        {"skipped": True, "reason": "skip_reconcile"} if args.skip_reconcile else run_reconcile()
    )
    all_candidates, eligible, all_completed_candidates, completed_eligible, ownership = collect_candidates(
        sab,
        limit=history_limit,
        min_age_hours=min_age_hours,
        completed_min_age_hours=completed_min_age_hours,
    )
    failed_results = [] if not clear_failed else cleanup_history(
        sab,
        eligible,
        dry_run=args.dry_run,
        max_delete=max_delete,
        action_label="deleted_failed_sab_history_with_files",
    )
    completed_results = [] if not clear_completed else cleanup_history(
        sab,
        completed_eligible,
        dry_run=args.dry_run,
        max_delete=max_completed_delete,
        action_label="deleted_completed_imported_sab_history_with_files",
    )
    results = failed_results + completed_results
    status = {
        "updated_at": iso(),
        "dry_run": bool(args.dry_run),
        "min_age_hours": min_age_hours,
        "completed_min_age_hours": completed_min_age_hours,
        "history_limit": history_limit,
        "max_delete": max_delete,
        "max_completed_delete": max_completed_delete,
        "clear_failed": clear_failed,
        "clear_completed": clear_completed,
        "cleanup_settings_source": cleanup_settings.get("source"),
        "cleanup_settings": {
            key: cleanup_settings.get(key)
            for key in (
                "remove_failed_downloads",
                "remove_completed_downloads",
                "failed_history_min_age_hours",
                "completed_history_min_age_hours",
                "sab_history_limit",
                "max_failed_history_delete",
                "max_completed_history_delete",
                "provider_enabled",
                "settings_error",
            )
            if key in cleanup_settings
        },
        "reconcile": reconcile,
        "ownership": ownership,
        "candidate_count": len(all_candidates),
        "eligible_count": len(eligible),
        "completed_candidate_count": len(all_completed_candidates),
        "completed_eligible_count": len(completed_eligible),
        "deleted_count": sum(1 for item in results if str(item.get("action")).startswith("deleted_")),
        "failed_deleted_count": sum(1 for item in results if item.get("action") == "deleted_failed_sab_history_with_files"),
        "completed_deleted_count": sum(1 for item in results if item.get("action") == "deleted_completed_imported_sab_history_with_files"),
        "failed_count": sum(1 for item in results if item.get("action") == "delete_failed"),
        "eligible_samples": eligible[:15],
        "completed_eligible_samples": completed_eligible[:15],
        "results": results,
    }
    write_status(status)
    if args.json or True:
        print(json.dumps(status, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
