#!/usr/bin/env python3
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import argparse
import inspect
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from core import inkdrop_runtime_config
from core import inkdrop_library_frontends
from core import inkdrop_artifact_acceptance

try:
    from core import inkdrop_state
except Exception:
    inkdrop_state = None

try:
    from core import inkdrop_nfo_parser
except Exception:
    inkdrop_nfo_parser = None


def script_path(name: str, remote_path: str = "", *, env_var: str = "") -> Path:
    local = Path(__file__).resolve().with_name(name)
    if local.exists():
        return local
    if env_var:
        configured = os.environ.get(env_var)
        if configured:
            return Path(configured)
    if remote_path:
        return Path(remote_path)
    return Path(remote_path)


STATE_DIR = inkdrop_runtime_config.state_dir()
CONFIG_DIR = inkdrop_runtime_config.config_dir()
LOG_DIR = inkdrop_runtime_config.log_dir()
STAGING_DIR = inkdrop_runtime_config.staging_dir()
QUARANTINE_DIR = inkdrop_runtime_config.quarantine_dir()
COMPLETION_DB = STATE_DIR / "imported-files.sqlite3"
INKDROP_STATE_DB = STATE_DIR / (inkdrop_state.STATE_DB_NAME if inkdrop_state else "inkdrop-state.sqlite3")
MANUAL_REVIEW_FILE = STATE_DIR / "manual-review.jsonl"
MANUAL_REVIEW_ACTIONS_FILE = STATE_DIR / "manual-review-actions.json"
SERIES_AUTOPILOT_QUEUE_FILE = STATE_DIR / "series-autopilot-queue.json"
PACK_REVIEW_STATE_FILE = STATE_DIR / "pack-review-state.json"
PACK_AUTO_IMPORT_STATUS_FILE = STATE_DIR / "pack-auto-import-status.json"
PACK_BAD_ARCHIVE_HISTORY_FILE = STATE_DIR / "pack-bad-archive-history.json"
PACK_LOG = LOG_DIR / "pack-import.log"
PENDING_PACKS_LOG = STATE_DIR / "pending-pack-imports.jsonl"
COMPLETED_IMPORT_PATH = script_path("inkdrop_completed_import.py", env_var="INKDROP_COMPLETED_IMPORT_SCRIPT")

COMIC_ROOT = Path(os.environ.get("INKDROP_COMIC_ROOT") or "/library/comics")
PACK_SOURCES = [
    Path(os.environ.get("INKDROP_PACK_DOWNLOAD_ROOT") or STAGING_DIR / "downloads" / "comics"),
    Path(os.environ.get("INKDROP_PACK_TEMP_DOWNLOAD_ROOT") or STAGING_DIR / "temp" / "downloads" / "comics"),
]
QUARANTINE_ROOT = Path(os.environ.get("INKDROP_PACK_REVIEW_QUARANTINE_ROOT") or QUARANTINE_DIR / "pack-review")
COMIC_EXTS = {".cbz", ".cbr", ".pdf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
PACK_ARCHIVE_EXTS = {".zip", ".rar", ".7z"}
MANGA_SERIES = {"berserk", "fire punch", "one piece"}
PACK_REVIEW_REASONS = {"pack_candidate_requires_review", "rss_pack_requires_review"}
MANUAL_SOURCE_REASONS = {
    "no_safe_source",
    "no_exact_result",
    "no_safe_alternate_found",
    "prowlarr_search_error",
    "manga_no_safe_result",
}
SQLITE_BUSY_TIMEOUT_MS = 300000


def truthy_env(name):
    value = str(os.environ.get(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def sqlite_locked(exc):
    return isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc).lower()


def commit_with_retry(conn, attempts=8, delay=5):
    for attempt in range(max(1, attempts)):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if not sqlite_locked(exc) or attempt >= attempts - 1:
                raise
            time.sleep(delay)


def load_importer():
    spec = importlib.util.spec_from_file_location("inkdrop_completed_import", COMPLETED_IMPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def log(event):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with PACK_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**event, "ts": time.time()}, sort_keys=True) + "\n")


def normalize(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


SUPPLEMENTAL_PACK_PHRASES = {
    "adventurer s bible",
    "art book",
    "artbook",
    "character book",
    "companion",
    "encyclopedia",
    "fan book",
    "fanbook",
    "guide book",
    "guidebook",
    "official guide",
    "world guide",
}


def is_supplemental_pack_result(raw_title):
    normalized = normalize(raw_title)
    padded = f" {normalized} "
    return any(f" {phrase} " in padded for phrase in SUPPLEMENTAL_PACK_PHRASES)


def review_id_for(item):
    if item.get("review_id"):
        return item["review_id"]
    raw = "|".join(
        str(value or "").lower()
        for value in (
            item.get("reason"),
            item.get("series"),
            item.get("issue"),
            item.get("query"),
            (item.get("candidate") or {}).get("title"),
        )
    )
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def pending_pack_records():
    if not PENDING_PACKS_LOG.exists():
        return []
    records = []
    with PENDING_PACKS_LOG.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def pending_pack_record_for(review_id):
    for record in reversed(pending_pack_records()):
        if record.get("review_id") == review_id:
            item = dict(record)
            item.setdefault("reason", "pack_candidate_requires_review")
            item.setdefault("query", item.get("title"))
            item.setdefault("candidate", {})
            if item.get("title") and not item["candidate"].get("title"):
                item["candidate"]["title"] = item["title"]
            return item
    return None


def first_scalar(value):
    if isinstance(value, (list, tuple)):
        for item in value:
            if item not in (None, ""):
                return item
        return None
    return value if value not in (None, "") else None


def pack_item_client_ids(item):
    item = item if isinstance(item, dict) else {}
    outcome = item.get("outcome") if isinstance(item.get("outcome"), dict) else {}
    ids = []
    for value in (
        item.get("client_id"),
        item.get("client_hash"),
        item.get("nzo_id"),
        first_scalar(item.get("nzo_ids")),
        outcome.get("client_id"),
        outcome.get("client_hash"),
        outcome.get("nzo_id"),
        first_scalar(outcome.get("nzo_ids")),
    ):
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)
    return ids


def manual_source_waiting_record_for(review_id):
    actions = read_json(MANUAL_REVIEW_ACTIONS_FILE, {}) or {}
    waiting = actions.get("manual_source_waiting") if isinstance(actions, dict) else {}
    if not isinstance(waiting, dict):
        return None
    record = waiting.get(str(review_id))
    return record if isinstance(record, dict) else None


def queue_row_for_waiting_record(record):
    if not isinstance(record, dict):
        return None
    queue = read_json(SERIES_AUTOPILOT_QUEUE_FILE, {}) or {}
    items = queue.get("items") if isinstance(queue, dict) else {}
    if not isinstance(items, dict):
        return None
    queue_key = str(record.get("autopilot_queue_key") or "").strip()
    if queue_key and isinstance(items.get(queue_key), dict):
        return dict(items[queue_key])
    wanted_series = normalize(record.get("series") or record.get("query"))
    wanted_issue = issue_key(record.get("issue"))
    wanted_identity = str(record.get("queue_identity") or "").strip()
    matches = []
    for row in items.values():
        if not isinstance(row, dict):
            continue
        if wanted_series and normalize(row.get("series") or row.get("query")) != wanted_series:
            continue
        if wanted_issue is not None and issue_key(row.get("issue")) != wanted_issue:
            continue
        if wanted_identity and str(row.get("queue_identity") or "").strip() != wanted_identity:
            continue
        matches.append(dict(row))
    return matches[0] if len(matches) == 1 else None


def synthetic_manual_source_archive_item(review_id, source_path=None):
    record = manual_source_waiting_record_for(review_id)
    if not isinstance(record, dict):
        return None
    row = queue_row_for_waiting_record(record) or {}
    source_path = Path(source_path) if source_path else None
    item = {}
    for source in (row, record):
        for field in (
            "series",
            "query",
            "issue",
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
        ):
            value = source.get(field) if isinstance(source, dict) else None
            if value not in (None, ""):
                item[field] = value
    title = (
        record.get("filename")
        or record.get("filename_leaf")
        or (source_path.name if source_path else "")
        or item.get("query")
        or item.get("series")
    )
    item.update({
        "review_id": str(review_id),
        "reason": record.get("reason") or "no_safe_source",
        "source": "manual_source_waiting",
        "title": title,
        "candidate": {"title": title},
        "manual_source_waiting": record,
        "manual_source_waiting_synthetic": True,
        "autopilot_queue": bool(record.get("autopilot_queue") or row),
    })
    if item.get("kapowarr_id") not in (None, "") and item.get("volume_id") in (None, ""):
        item["volume_id"] = item.get("kapowarr_id")
    if not item.get("query"):
        item["query"] = " ".join(str(value) for value in (item.get("series"), item.get("issue")) if value not in (None, ""))
    if not item.get("series"):
        return None
    return item


def load_manual_review_item(review_id, allow_manual_source_archive=False, source_path=None):
    if not MANUAL_REVIEW_FILE.exists():
        item = pending_pack_record_for(review_id)
        if item:
            return item
        if allow_manual_source_archive:
            item = synthetic_manual_source_archive_item(review_id, source_path=source_path)
            if item:
                return item
        raise ValueError("manual review file is missing")
    with MANUAL_REVIEW_FILE.open("r", encoding="utf-8") as handle:
        for line in reversed(handle.readlines()[-1000:]):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if review_id_for(item) == review_id:
                reason = str(item.get("reason") or "")
                is_pack_review = reason in PACK_REVIEW_REASONS or item.get("pack_info")
                is_manual_source_archive = allow_manual_source_archive and reason in MANUAL_SOURCE_REASONS
                if not is_pack_review and not is_manual_source_archive:
                    continue
                return item
    item = pending_pack_record_for(review_id)
    if item:
        return item
    if allow_manual_source_archive:
        item = synthetic_manual_source_archive_item(review_id, source_path=source_path)
        if item:
            return item
        raise ValueError("pack/manual-source archive review item not found")
    raise ValueError("pack review item not found")


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def write_pack_auto_import_status(payload):
    existing = read_json(PACK_AUTO_IMPORT_STATUS_FILE, {}) or {}
    if not isinstance(existing, dict):
        existing = {}
    record = {**existing, **dict(payload or {}), "ts": time.time()}
    write_json(PACK_AUTO_IMPORT_STATUS_FILE, record)
    return record


PACK_TERMINAL_STATUSES = {
    "verified",
    "finished",
    "finished_imported",
    "finished_already_satisfied",
    "finished_bad_archive_candidate",
    "finished_import_error",
    "finished_no_importable_source",
    "finished_no_matching_wanted_files",
    "finished_no_missing",
    "finished_supplemental_release_blocked",
    "imported",
    "already_verified",
    "pack_already_handled",
    "cleared",
    "ignored",
}


def pack_status_is_terminal(status):
    status = str(status or "").strip().lower()
    return bool(status and (status in PACK_TERMINAL_STATUSES or status.startswith("finished_")))


def pack_handled_key_for_item(item, candidate=None):
    import hashlib

    item = item if isinstance(item, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else (item.get("candidate") if isinstance(item.get("candidate"), dict) else {})
    pack_info = item.get("pack_info") if isinstance(item.get("pack_info"), dict) else {}
    pack_match = item.get("pack_match") if isinstance(item.get("pack_match"), dict) else {}
    series = normalize(item.get("series") or pack_match.get("series") or "")
    title = normalize(candidate.get("title") or item.get("title") or item.get("query") or "")
    range_text = normalize(
        pack_info.get("summary")
        or pack_info.get("range")
        or pack_match.get("summary")
        or pack_match.get("range")
        or pack_match.get("pack_range")
        or ""
    )
    protocol = normalize(candidate.get("protocol") or item.get("protocol") or "")
    indexer = normalize(candidate.get("indexer") or candidate.get("indexerId") or item.get("indexer") or "")
    if not series and not title:
        return None
    raw = "|".join([series, title, range_text, protocol, indexer])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def pack_family_key_for_item(item, candidate=None):
    import hashlib

    item = item if isinstance(item, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else (item.get("candidate") if isinstance(item.get("candidate"), dict) else {})
    pack_info = item.get("pack_info") if isinstance(item.get("pack_info"), dict) else {}
    pack_match = item.get("pack_match") if isinstance(item.get("pack_match"), dict) else {}
    series = normalize(item.get("series") or pack_match.get("series") or "")
    title = normalize(candidate.get("title") or item.get("title") or item.get("query") or "")
    range_text = normalize(
        pack_info.get("summary")
        or pack_info.get("range")
        or pack_match.get("summary")
        or pack_match.get("range")
        or pack_match.get("pack_range")
        or ""
    )
    if not series and not title:
        return None
    raw = "|".join([series, title, range_text])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def load_manual_review_actions():
    data = read_json(MANUAL_REVIEW_ACTIONS_FILE, {}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("ignored", [])
    data.setdefault("approved", [])
    data.setdefault("pack_approved", [])
    data.setdefault("pack_finished", [])
    data.setdefault("pack_handled_keys", {})
    if not isinstance(data.get("pack_finished_families"), dict):
        data["pack_finished_families"] = {}
    if not isinstance(data.get("pack_finished_paths"), dict):
        data["pack_finished_paths"] = {}
    return data


def save_manual_review_actions(data):
    write_json(MANUAL_REVIEW_ACTIONS_FILE, data)


def pack_finished_path_map(actions):
    paths = actions.get("pack_finished_paths", {})
    return paths if isinstance(paths, dict) else {}


def pack_finished_family_map(actions):
    families = actions.get("pack_finished_families", {})
    return families if isinstance(families, dict) else {}


def pack_handled_map(actions):
    handled = actions.get("pack_handled_keys", {})
    if isinstance(handled, dict):
        return handled
    if isinstance(handled, list):
        return {str(key): {"legacy": True} for key in handled}
    return {}


def pack_item_finished_by_actions(item, actions=None):
    item = item if isinstance(item, dict) else {}
    actions = actions if isinstance(actions, dict) else load_manual_review_actions()
    review_id = str(item.get("review_id") or review_id_for(item) or "").strip()
    if review_id and review_id in set(actions.get("pack_finished") or []):
        return True
    key = pack_handled_key_for_item(item)
    if key:
        handled = pack_handled_map(actions).get(key)
        if isinstance(handled, dict) and pack_status_is_terminal(handled.get("status")):
            return True
    family_key = pack_family_key_for_item(item)
    if family_key:
        family = pack_finished_family_map(actions).get(family_key)
        if isinstance(family, dict) and pack_status_is_terminal(family.get("status")):
            return True
    return False


def mark_pack_handled(review_id, item, status, reason=None, result=None):
    item = item if isinstance(item, dict) else {}
    result = result if isinstance(result, dict) else {}
    actions = load_manual_review_actions()
    changed = False
    review_id = str(review_id or item.get("review_id") or review_id_for(item) or "").strip()
    if review_id and review_id not in actions.setdefault("pack_finished", []):
        actions["pack_finished"].append(review_id)
        changed = True
    key = pack_handled_key_for_item(item)
    if key:
        handled = actions.setdefault("pack_handled_keys", {})
        if not isinstance(handled, dict):
            handled = pack_handled_map(actions)
            actions["pack_handled_keys"] = handled
            changed = True
        if key not in handled:
            handled[key] = {
                "review_id": review_id,
                "series": item.get("series"),
                "title": item.get("title") or item.get("query"),
                "status": status,
                "reason": reason,
                "updated_at": time.time(),
            }
            if result:
                handled[key]["result_status"] = result.get("status")
            changed = True
    family_key = pack_family_key_for_item(item)
    if family_key:
        families = actions.setdefault("pack_finished_families", {})
        if not isinstance(families, dict):
            families = pack_finished_family_map(actions)
            actions["pack_finished_families"] = families
            changed = True
        if family_key not in families:
            families[family_key] = {
                "review_id": review_id,
                "series": item.get("series"),
                "title": item.get("title") or item.get("query"),
                "status": status,
                "reason": reason,
                "updated_at": time.time(),
            }
            changed = True
    pack_path = result.get("pack_path") or result.get("selected_path")
    if pack_path:
        paths = actions.setdefault("pack_finished_paths", {})
        if not isinstance(paths, dict):
            paths = pack_finished_path_map(actions)
            actions["pack_finished_paths"] = paths
            changed = True
        pack_path = str(pack_path)
        if pack_path not in paths:
            paths[pack_path] = {
                "review_id": review_id,
                "series": item.get("series"),
                "title": item.get("title") or item.get("query"),
                "status": status,
                "reason": reason,
                "updated_at": time.time(),
            }
            changed = True
    if changed:
        save_manual_review_actions(actions)
    return key


def finish_pack_review_state(review_id, status, reason, result=None, item=None):
    state = read_json(PACK_REVIEW_STATE_FILE, {"active": None, "history": []}) or {}
    if not isinstance(state, dict):
        state = {"active": None, "history": []}
    state.setdefault("history", [])
    active = state.get("active") if isinstance(state.get("active"), dict) else None
    active_matches = bool(active and str(active.get("review_id") or "") == str(review_id or ""))
    previous = dict(active or item or {})
    finished = dict(previous)
    finished.update(
        {
            "review_id": review_id,
            "status": status,
            "finish_reason": reason,
            "finish_evidence": result or {},
            "reconciled_at": time.time(),
            "pack_path": (result or {}).get("pack_path") or (result or {}).get("selected_path") or previous.get("pack_path"),
            "title": previous.get("title") or (item or {}).get("title") or (item or {}).get("query") or (result or {}).get("title"),
            "series": previous.get("series") or (item or {}).get("series") or (result or {}).get("series"),
        }
    )
    if active_matches:
        state["active"] = None
        state["finished"] = finished
    state["history"].append(
        {
            "event": status if active_matches else f"{status}_non_active",
            "review_id": review_id,
            "reason": reason,
            "result": result,
            "ts": time.time(),
            "previous": previous,
            **({} if active_matches else {"active_review_id": (active or {}).get("review_id") if active else None}),
        }
    )
    write_json(PACK_REVIEW_STATE_FILE, state)
    return {"active_cleared": active_matches, "status": status}


def bad_archive_history_key(row):
    return "|".join(
        str(row.get(field) or "")
        for field in ("review_id", "matched_kapowarr_id", "matched_kapowarr_issue_id", "normalized_number", "source")
    )


def append_pack_bad_archive_history(rows, limit=5000):
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return {"added": 0, "total": 0}
    existing = read_json(PACK_BAD_ARCHIVE_HISTORY_FILE, {}) or {}
    if not isinstance(existing, dict):
        existing = {}
    history = existing.get("bad_archives") or []
    if not isinstance(history, list):
        history = []
    by_key = {}
    for row in history:
        if isinstance(row, dict):
            key = bad_archive_history_key(row)
            if key.strip("|"):
                by_key[key] = row
    added = 0
    now = time.time()
    for row in rows:
        record = dict(row)
        record.setdefault("history_source", "pack_import")
        record["history_updated_at"] = now
        key = bad_archive_history_key(record)
        if not key.strip("|"):
            continue
        if key not in by_key:
            added += 1
        by_key[key] = record
    merged = sorted(
        by_key.values(),
        key=lambda row: float(row.get("ts") or row.get("history_updated_at") or 0),
        reverse=True,
    )[:limit]
    write_json(
        PACK_BAD_ARCHIVE_HISTORY_FILE,
        {
            "updated_at": now,
            "bad_archives": merged,
            "bad_archive_count": len(merged),
        },
    )
    return {"added": added, "total": len(merged)}


def append_manual_review(reason, payload, db_path=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "reason": reason, **payload}
    if str(reason or "") == "pack_import_bad_archive" and not truthy_env("INKDROP_PACK_IMPORT_PERSIST_BAD_ARCHIVES"):
        return record
    with MANUAL_REVIEW_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    try:
        from core import inkdrop_notifications
        # Callers should pass db_path explicitly (every real call site now does),
        # but this fallback is what actually prevents a silent no-delivery repeat
        # of the bug this replaced -- notify_manual_review() no-ops on db_path=None
        # with no error, so an eventual new call site that forgets db_path would
        # otherwise fail exactly as silently as every existing one did.
        inkdrop_notifications.notify_manual_review(
            db_path if db_path is not None else INKDROP_STATE_DB,
            reason=reason,
            series=payload.get("series") or payload.get("matched_series"),
            source=payload.get("source"),
            detail=payload.get("detail"),
            note=payload.get("note"),
            series_id=payload.get("series_id") or payload.get("native_series_id"),
            issue_id=payload.get("issue_id"),
        )
    except Exception:
        pass
    return record


def issue_key(value):
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def normalize_manga_number(value):
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    raw = match.group(0)
    try:
        number = float(raw)
    except ValueError:
        return None
    if number < 0:
        return None
    if number.is_integer():
        return f"{int(number):03d}"
    whole, _, frac = raw.partition(".")
    frac = frac.rstrip("0") or "0"
    return f"{int(whole):03d}.{frac}"


def missing_issue_numbers(volume_id):
    # Kapowarr is retired; there is no live issues/issues_files table to read
    # a missing-issue list from. Pack-match coverage now comes entirely from
    # missing_issue_numbers_from_pack_match() via merged_missing_issue_numbers().
    return {}


def pack_match_sample_rows(item):
    pack_match = (item or {}).get("pack_match") or {}
    sample = pack_match.get("useful_missing_sample") if isinstance(pack_match, dict) else []
    if not isinstance(sample, list):
        return []
    return [row for row in sample if isinstance(row, dict)]


def pack_match_series_filters(item):
    names = []
    for value in ((item or {}).get("series"),):
        if value not in (None, "") and normalize(value) not in {normalize(name) for name in names}:
            names.append(str(value))
    for row in pack_match_sample_rows(item):
        value = row.get("series") or row.get("title")
        if value in (None, ""):
            continue
        if normalize(value) not in {normalize(name) for name in names}:
            names.append(str(value))
    return names


def pack_sample_target_keys(row):
    row = row if isinstance(row, dict) else {}
    keys = set()
    for key in ("series_id", "native_series_id", "inkdrop_series_id"):
        value = str(row.get(key) or "").strip()
        if value:
            keys.add(f"id:{value}")
    provider = str(row.get("metadata_provider") or "").strip().lower()
    metadata_id = str(row.get("metadata_id") or "").strip()
    if provider and metadata_id:
        keys.add(f"metadata:{provider}:{metadata_id}")
        if provider == "comicvine":
            keys.add(f"comicvine:{metadata_id}")
    kapowarr_id = str(row.get("kapowarr_id") or row.get("volume_id") or "").strip()
    if kapowarr_id:
        keys.add(f"kapowarr:{kapowarr_id}")
    title = normalize(row.get("series") or row.get("title"))
    if title:
        keys.add(f"title:{title}")
    return keys


def target_pack_keys(target):
    target = target if isinstance(target, dict) else {}
    keys = set()
    for key in ("native_series_id", "inkdrop_series_id"):
        value = str(target.get(key) or "").strip()
        if value:
            keys.add(f"id:{value}")
    provider = str(target.get("metadata_provider") or "").strip().lower()
    metadata_id = str(target.get("metadata_id") or "").strip()
    if provider and metadata_id:
        keys.add(f"metadata:{provider}:{metadata_id}")
        if provider == "comicvine":
            keys.add(f"comicvine:{metadata_id}")
    comicvine_id = str(target.get("comicvine_id") or "").strip()
    if comicvine_id:
        keys.add(f"comicvine:{comicvine_id}")
        keys.add(f"metadata:comicvine:{comicvine_id}")
    volume_id = target_kapowarr_volume_id(target)
    if volume_id is not None:
        keys.add(f"kapowarr:{volume_id}")
    title = normalize(target.get("title"))
    if title:
        keys.add(f"title:{title}")
    for alias in target.get("aliases") or []:
        alias_key = normalize(alias)
        if alias_key:
            keys.add(f"title:{alias_key}")
    return keys


def target_owner_key(target):
    """The single stable identity that owns a target's missing-issue map.

    Titles and aliases are never owners: two different series legitimately
    share a title, and an owner's map must never be overwritten by a
    neighbour's. A target with no stable identifier at all falls back to a
    title-only owner -- two such targets with the same title are genuinely
    indistinguishable and share one merged map.
    """
    target = target if isinstance(target, dict) else {}
    for key in ("native_series_id", "inkdrop_series_id"):
        value = str(target.get(key) or "").strip()
        if value:
            return f"id:{value}"
    provider = str(target.get("metadata_provider") or "").strip().lower()
    metadata_id = str(target.get("metadata_id") or "").strip()
    if provider and metadata_id:
        return f"metadata:{provider}:{metadata_id}"
    comicvine_id = str(target.get("comicvine_id") or "").strip()
    if comicvine_id:
        return f"metadata:comicvine:{comicvine_id}"
    volume_id = target_kapowarr_volume_id(target)
    if volume_id is not None:
        return f"kapowarr:{volume_id}"
    title = normalize(target.get("title"))
    if title:
        return f"title-only:{title}"
    return ""


def same_pack_target_identity(a, b):
    """True only for stable-identity equivalence. A shared title never makes
    two targets one series."""
    owner_a = target_owner_key(a)
    owner_b = target_owner_key(b)
    if not owner_a or not owner_b:
        return False
    if owner_a.startswith("title-only:") or owner_b.startswith("title-only:"):
        return False
    return owner_a == owner_b


def pack_missing_maps_from_pairs(pairs):
    """Build the canonical single-owner layout from (target, missing) pairs.

    Every owner key maps to exactly one missing-issue map and is never
    overwritten; alias keys (including titles) live in a separate multimap
    that resolves lookups but is never authoritative. The same stable owner
    appearing twice merges per issue key, first writer wins.
    """
    by_owner = {}
    aliases = {}
    total = 0
    for target, missing in pairs or []:
        owner = target_owner_key(target)
        if not owner:
            continue
        # Every target registers, wanted rows or not. A target with nothing
        # missing that stayed unregistered left its shared title pointing at
        # a NEIGHBOUR's stable identity, and lookups walked straight into
        # the other series' rows -- its own empty map is the correct answer.
        existing = by_owner.get(owner)
        if existing is None:
            by_owner[owner] = missing if isinstance(missing, dict) else {}
        elif missing and existing is not missing:
            for issue_key_value, row in missing.items():
                existing.setdefault(issue_key_value, row)
        total += len(missing or {})
        for key in target_pack_keys(target):
            aliases.setdefault(key, set()).add(owner)
    return by_owner, aliases, total


def pack_owner_for_target(missing, target):
    """Resolve which owner's map a target may read; '' when none or ambiguous.

    Stable keys resolve first; title keys are consulted only when the target
    carries no resolving stable identity, and a title shared by more than one
    owner identifies nobody -- ambiguity fails closed.
    """
    if not isinstance(missing, dict):
        return ""
    by_owner = missing.get("__by_owner")
    if not isinstance(by_owner, dict):
        return ""
    owner = target_owner_key(target)
    if owner and owner in by_owner:
        return owner
    aliases = missing.get("__owner_aliases")
    if not isinstance(aliases, dict):
        return ""
    stable_hits = set()
    title_hits = set()
    for key in target_pack_keys(target):
        hit = aliases.get(key)
        if not hit:
            continue
        (title_hits if key.startswith("title:") else stable_hits).update(hit)
    if len(stable_hits) == 1:
        return next(iter(stable_hits))
    if stable_hits:
        return ""
    # A target that carries a stable identity of its own must resolve
    # through stable keys or not at all -- a title is shared property and
    # falling through to it once handed a zero-missing target another
    # series' rows.
    if owner and not owner.startswith("title-only:"):
        return ""
    if len(title_hits) == 1:
        return next(iter(title_hits))
    return ""


def pack_row_owner_conflict(row, owner_key):
    """A row that names its own series must agree with the map it lives in."""
    if not isinstance(row, dict):
        return False
    row_series = str(row.get("series_id") or row.get("inkdrop_series_id") or "").strip()
    return bool(row_series) and str(owner_key or "").startswith("id:") and owner_key != f"id:{row_series}"


def pack_sample_matches_target(row, target, fallback_series=None):
    sample_keys = pack_sample_target_keys(row)
    target_keys = target_pack_keys(target)
    if sample_keys and target_keys:
        return bool(sample_keys.intersection(target_keys))
    sample_series = normalize((row or {}).get("series") or (row or {}).get("title"))
    target_title = normalize((target or {}).get("title"))
    if sample_series and target_title:
        return sample_series == target_title or sample_series in {normalize(alias) for alias in (target or {}).get("aliases") or []}
    if fallback_series:
        return normalize(fallback_series) == target_title
    return not sample_keys


def missing_issue_numbers_from_pack_match(item, target=None):
    sample = pack_match_sample_rows(item)
    if not sample:
        return {}
    out = {}
    for row in sample:
        if target is not None and not pack_sample_matches_target(row, target, fallback_series=(item or {}).get("series")):
            continue
        value = (
            row.get("calculated")
            or row.get("calculated_issue_number")
            or row.get("issue")
            or row.get("issue_number")
            or row.get("number")
        )
        key = issue_key(value)
        if key is None:
            continue
        out[key] = {
            "issue_id": row.get("issue_id") or row.get("native_issue_id") or row.get("matched_kapowarr_issue_id"),
            "series_id": row.get("series_id") or row.get("native_series_id") or row.get("inkdrop_series_id"),
            "wanted_id": row.get("wanted_id"),
            "queue_id": row.get("queue_id"),
            "series": row.get("series") or row.get("title"),
            "issue": row.get("issue") or row.get("issue_number") or value,
            "calculated": row.get("calculated") or row.get("calculated_issue_number") or value,
            "presence": row.get("presence"),
            "matching_entry": row.get("matching_entry") or row.get("file_entry"),
            "source": "pack_match",
        }
    return out


def numeric_issue_value(value):
    if value in (None, ""):
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def collected_edition_range_for_item(item):
    pack_match = (item or {}).get("pack_match") if isinstance(item, dict) else {}
    if not isinstance(pack_match, dict) or not pack_match.get("collected_edition"):
        return None
    raw_range = pack_match.get("range")
    if not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2:
        return None
    try:
        start = int(float(raw_range[0]))
        end = int(float(raw_range[1]))
    except (TypeError, ValueError):
        return None
    if start <= 0 or end < start:
        return None
    return start, end


def friendly_collection_title(raw_title, series):
    text = str(raw_title or "").strip()
    if not text:
        return "Collected Edition"
    text = re.sub(r"\.(?=[A-Za-z0-9])", " ", text)
    text = re.sub(r"[_]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    series_text = str(series or "").strip()
    if series_text and text.lower().startswith(series_text.lower()):
        text = text[len(series_text):].strip(" -:.")
    text = re.sub(r"\b(?:digital|webrip|scan)\b.*$", "", text, flags=re.I).strip(" -:.")
    return text or "Collected Edition"


def collected_edition_year_from_text(*values):
    for value in values:
        match = re.search(r"\b((?:19|20)\d{2})\b", str(value or ""))
        if match:
            return match.group(1)
    return ""


def collection_coverage_rows_for_target(target_missing, start, end):
    coverage = []
    seen = set()
    for key, row in (target_missing or {}).items():
        if not isinstance(row, dict):
            continue
        number = numeric_issue_value(row.get("calculated") or row.get("issue") or row.get("issue_number") or key)
        if number is None:
            continue
        if int(number) != number or not (start <= int(number) <= end):
            continue
        out = dict(row)
        out.setdefault("issue", row.get("issue") or str(int(number)))
        out.setdefault("calculated", row.get("calculated") or str(int(number)))
        out["collection_range"] = [start, end]
        out["collection_coverage_source"] = "collected_edition_range_hint"
        identity = (out.get("queue_id"), out.get("wanted_id"), out.get("issue_id"), out.get("issue"), out.get("calculated"))
        if identity in seen:
            continue
        seen.add(identity)
        coverage.append(out)
    coverage.sort(key=lambda row: numeric_issue_value(row.get("calculated") or row.get("issue")) or 0)
    return coverage


def collected_edition_match_for_file(item, path, target, target_missing, single_file_pack=False):
    if not single_file_pack or not target:
        return None
    covered = collected_edition_range_for_item(item)
    if not covered:
        return None
    start, end = covered
    coverage = collection_coverage_rows_for_target(target_missing, start, end)
    if not coverage:
        return None
    candidate_title = (item.get("candidate") or {}).get("title") or item.get("title") or Path(path).stem
    collection_title = friendly_collection_title(candidate_title, target.get("title"))
    collection = {
        "series": target.get("title"),
        "collection_title": collection_title,
        "year": collected_edition_year_from_text(candidate_title, Path(path).name, target.get("year")),
        "range": [start, end],
        "coverage_source": "collected_edition_range_hint",
        "source_title": candidate_title,
    }
    anchor = dict(coverage[0])
    return {
        "source": str(path),
        "issue_number": f"{start}-{end}",
        "matched_series": target.get("title"),
        "matched_kapowarr_id": target_kapowarr_volume_id(target),
        "source_unit": "collected_edition",
        "manga_unit_model": "collected_edition",
        "truth_model": "kavita_collection",
        "missing_issue": anchor,
        "collection": collection,
        "collection_range": [start, end],
        "collection_coverage": coverage,
        "collection_coverage_count": len(coverage),
    }


def merged_missing_issue_numbers(volume_id, item, target=None):
    missing = {}
    if volume_id not in (None, ""):
        missing.update(missing_issue_numbers(volume_id))
    pack_missing = missing_issue_numbers_from_pack_match(item, target=target)
    for key, row in pack_missing.items():
        missing.setdefault(key, row)
    return missing


def target_volume_id_for_pack(target, trigger_volume_id=None, item=None, all_targets=None):
    """Bind the pack's trigger volume to a target only when it can own it.

    Handing the trigger volume to any target that merely matched the pack's
    title let two same-titled targets absorb the same volume's missing rows
    into their own maps -- the ownership registry then faithfully guarded
    rows that never belonged to the target, and files recorded under the
    wrong series. The volume follows its stable owner: a target that claims
    it by Kapowarr id owns it outright, and the title fallback applies only
    when no other target claims the volume and no other target shares this
    target's title.
    """
    volume_id = target_kapowarr_volume_id(target)
    if volume_id is not None:
        return volume_id
    if trigger_volume_id in (None, ""):
        return None
    for other in all_targets or []:
        if other is target:
            continue
        if str(target_kapowarr_volume_id(other) or "") == str(trigger_volume_id):
            return None
    my_title = normalize(target.get("title") if isinstance(target, dict) else "")
    if my_title:
        for other in all_targets or []:
            if other is target:
                continue
            if isinstance(other, dict) and normalize(other.get("title")) == my_title:
                return None
    if pack_sample_matches_target({"series": (item or {}).get("series")}, target, fallback_series=(item or {}).get("series")):
        return trigger_volume_id
    return None


def target_missing_issue_maps(targets, volume_id, item):
    pairs = []
    for target in targets or []:
        missing = merged_missing_issue_numbers(
            target_volume_id_for_pack(target, volume_id, item, all_targets=targets), item, target=target
        )
        missing = enrich_missing_issue_map_with_inkdrop_queue(target, missing)
        # EVERY target reaches the registry, wanted rows or not. Filtering
        # the empty-handed ones here starved the registration fix downstream:
        # a legacy title-only target with nothing missing never registered,
        # its shared title resolved uniquely to a neighbour, and it read the
        # neighbour's rows -- the fifth shape of the wrong-series defect,
        # this time above the repaired function.
        pairs.append((target, missing or {}))
    by_owner, aliases, total = pack_missing_maps_from_pairs(pairs)
    return {
        "__by_owner": by_owner,
        "__owner_aliases": aliases,
        "__total_missing": total,
        "__legacy": merged_missing_issue_numbers(volume_id, item),
    }


def inkdrop_issue_number_keys(*values):
    keys = set()
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            keys.add(text)
        if inkdrop_state is not None and hasattr(inkdrop_state, "issue_number_keys"):
            try:
                keys.update(str(key) for key in inkdrop_state.issue_number_keys(text) if key not in (None, ""))
                continue
            except Exception:
                pass
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            continue
        raw = match.group(0)
        if "." in raw:
            keys.add(raw.rstrip("0").rstrip("."))
        else:
            try:
                number = int(raw)
            except ValueError:
                continue
            keys.add(str(number))
            keys.add(f"{number:03d}")
            keys.add(f"{number:04d}")
    return {key for key in keys if key}


def inkdrop_normalized_issue_number(value):
    if inkdrop_state is not None and hasattr(inkdrop_state, "normalize_issue_number"):
        try:
            return inkdrop_state.normalize_issue_number(value)
        except Exception:
            pass
    keys = inkdrop_issue_number_keys(value)
    return sorted(keys, key=len, reverse=True)[0] if keys else None


def inkdrop_queue_identity_for_pack_missing(target, row, key, active_only=True):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return None
    target = target if isinstance(target, dict) else {}
    row = row if isinstance(row, dict) else {}

    series_terms = []
    params = []
    for value in (
        row.get("series_id"),
        row.get("native_series_id"),
        row.get("inkdrop_series_id"),
        target.get("native_series_id"),
        target.get("inkdrop_series_id"),
    ):
        value = str(value or "").strip()
        if value:
            series_terms.append("s.id = ?")
            params.append(value)

    volume_id = target_kapowarr_volume_id(target)
    if volume_id is not None:
        series_terms.append("s.kapowarr_id = ?")
        params.append(int(volume_id))

    provider = str(target.get("metadata_provider") or row.get("metadata_provider") or "").strip().lower()
    metadata_id = str(target.get("metadata_id") or row.get("metadata_id") or "").strip()
    if provider and metadata_id:
        series_terms.append("(lower(coalesce(s.metadata_provider,'')) = ? and s.metadata_id = ?)")
        params.extend([provider, metadata_id])

    comicvine_id = str(target.get("comicvine_id") or row.get("comicvine_id") or "").strip()
    if comicvine_id:
        series_terms.append("(lower(coalesce(s.metadata_provider,'')) = 'comicvine' and s.metadata_id = ?)")
        params.append(comicvine_id)

    for title in (
        row.get("series"),
        row.get("title"),
        target.get("title"),
        *list(target.get("aliases") or []),
    ):
        title_key = normalize(title)
        if title_key:
            series_terms.append("s.sort_title = ?")
            params.append(title_key)

    if not series_terms:
        return None

    issue_values = [
        key,
        row.get("issue"),
        row.get("issue_number"),
        row.get("calculated"),
        row.get("calculated_issue_number"),
        row.get("number"),
    ]
    issue_keys = inkdrop_issue_number_keys(*issue_values)
    issue_terms = []
    for issue in sorted(issue_keys):
        issue_terms.append("(i.normalized_number = ? or i.issue_number = ?)")
        params.extend([inkdrop_normalized_issue_number(issue), issue])

    adapter_issue_id = row.get("kapowarr_issue_id") or row.get("matched_kapowarr_issue_id") or row.get("issue_id")
    if volume_id is not None and str(adapter_issue_id or "").isdigit():
        issue_terms.append("i.kapowarr_issue_id = ?")
        params.append(int(adapter_issue_id))

    if not issue_terms:
        return None

    terminal_states = (
        "verified",
        "satisfied",
        "superseded_duplicate",
        "ignored",
        "removed",
        "inactive",
    )
    if active_only:
        state_filter = f"q.active = 1 and q.state not in ({','.join('?' for _ in terminal_states)})"
        state_params = list(terminal_states)
    else:
        state_filter = "(q.state in ('verified','satisfied') or lower(coalesce(w.status,'')) = 'satisfied')"
        state_params = []
    try:
        conn = sqlite3.connect(f"file:{INKDROP_STATE_DB}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            found = conn.execute(
                f"""
                select q.id as queue_id, q.wanted_id, q.series_id, q.issue_id,
                       q.state as queue_state, q.active, w.status as wanted_status,
                       i.issue_number, i.normalized_number, i.kapowarr_issue_id
                from queue_items q
                join series s on s.id = q.series_id
                left join issues i on i.id = q.issue_id
                left join wanted_items w on w.id = q.wanted_id
                where {state_filter}
                  and ({' or '.join(series_terms)})
                  and ({' or '.join(issue_terms)})
                order by
                  case q.state
                    when 'verified' then 0
                    when 'satisfied' then 1
                    when 'importing' then 0
                    when 'downloading' then 1
                    when 'source_wait' then 2
                    when 'searching' then 3
                    when 'queued' then 4
                    else 5
                  end,
                  coalesce(q.updated_at, q.created_at, 0) desc
                limit 1
                """,
                (*state_params, *params),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        log({
            "event": "pack_queue_identity_lookup_failed",
            "target": target.get("title"),
            "issue": row.get("issue") or row.get("calculated") or key,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return None
    if not found:
        return None
    return {
        "queue_id": found["queue_id"],
        "wanted_id": found["wanted_id"],
        "series_id": found["series_id"],
        "issue_id": found["issue_id"],
        "queue_state": found["queue_state"],
        "active": bool(found["active"]),
        "wanted_status": found["wanted_status"],
        "issue": found["issue_number"],
        "calculated": found["normalized_number"],
        "kapowarr_issue_id": found["kapowarr_issue_id"],
        "identity_source": "inkdrop_queue_lookup",
    }


def enrich_missing_issue_map_with_inkdrop_queue(target, missing):
    if not isinstance(missing, dict) or not missing:
        return missing
    enriched = {}
    for key, row in missing.items():
        if not isinstance(row, dict):
            enriched[key] = row
            continue
        row_out = dict(row)
        if row_out.get("queue_id") and row_out.get("wanted_id") and row_out.get("series_id") and row_out.get("issue_id"):
            enriched[key] = row_out
            continue
        identity = inkdrop_queue_identity_for_pack_missing(target, row_out, key, active_only=True)
        if not identity:
            satisfied = inkdrop_queue_identity_for_pack_missing(target, row_out, key, active_only=False)
            if satisfied:
                continue
        if identity:
            previous_issue_id = row_out.get("issue_id")
            identity_issue_id = identity.get("issue_id")
            if previous_issue_id not in (None, "") and previous_issue_id != identity_issue_id:
                if str(previous_issue_id).isdigit():
                    row_out.setdefault("kapowarr_issue_id", previous_issue_id)
                else:
                    row_out.setdefault("source_issue_id", previous_issue_id)
            row_out.update(
                {
                    "queue_id": identity.get("queue_id") or row_out.get("queue_id"),
                    "wanted_id": identity.get("wanted_id") or row_out.get("wanted_id"),
                    "series_id": identity.get("series_id") or row_out.get("series_id"),
                    "issue_id": identity_issue_id or row_out.get("issue_id"),
                    "identity_source": identity.get("identity_source"),
                    "queue_state": identity.get("queue_state"),
                    "wanted_status": identity.get("wanted_status"),
                }
            )
            if identity.get("issue"):
                row_out.setdefault("issue", identity.get("issue"))
            if identity.get("calculated"):
                row_out.setdefault("calculated", identity.get("calculated"))
            if identity.get("kapowarr_issue_id"):
                row_out.setdefault("kapowarr_issue_id", identity.get("kapowarr_issue_id"))
        enriched[key] = row_out
    return enriched


def missing_issue_map_for_target(missing, target):
    """The target's own rows -- proving the map is its own is not enough.

    Owning the map only says nobody else's map was handed over. It says
    nothing about the rows inside it, and rows arrive carrying their own
    series: a wanted row is matched into a target's map by a shared title
    (pack_sample_matches_target), and the queue lookup that stamps a row's
    series_id ORs its title terms, so a same-titled neighbour's row can land
    in this target's map naming that neighbour.

    The path matcher already refuses to bind a file to a row whose series
    disagrees with the owner (manifest_target_for_path). The filename matcher
    consumed this map on key presence alone, so the same foreign row it
    rejects on one branch was accepted on the other -- the file filed under
    the matched target, the import record written against the other series.
    Drop conflicting rows here so both branches see the same rows.
    """
    if not isinstance(missing, dict):
        return {}
    by_owner = missing.get("__by_owner")
    if isinstance(by_owner, dict):
        owner = pack_owner_for_target(missing, target)
        values = by_owner.get(owner) if owner else None
        if not isinstance(values, dict):
            return {}
        return {
            key: row
            for key, row in values.items()
            if not pack_row_owner_conflict(row, owner)
        }
    return missing


def missing_issue_map_has_values(missing):
    if not isinstance(missing, dict):
        return False
    by_owner = missing.get("__by_owner")
    if isinstance(by_owner, dict):
        return any(bool(values) for values in by_owner.values() if isinstance(values, dict))
    return bool(missing)


def missing_issue_map_total_count(missing):
    if not isinstance(missing, dict):
        return 0
    by_owner = missing.get("__by_owner")
    if isinstance(by_owner, dict):
        return sum(len(values) for values in by_owner.values() if isinstance(values, dict))
    return len(missing)


def missing_target_filters(target_filters, targets):
    loaded = set()
    for target in targets or []:
        title = normalize(target.get("title"))
        if title:
            loaded.add(title)
        for alias in target.get("aliases") or []:
            alias_key = normalize(alias)
            if alias_key:
                loaded.add(alias_key)
    return [name for name in target_filters or [] if normalize(name) and normalize(name) not in loaded]


def probe_row_summary(row):
    row = row if isinstance(row, dict) else {}
    missing = row.get("missing_issue") if isinstance(row.get("missing_issue"), dict) else {}
    summary = {
        "source": row.get("source"),
        "series": row.get("matched_series") or missing.get("series"),
        "issue_number": row.get("issue_number") or missing.get("issue") or missing.get("calculated"),
        "queue_id": missing.get("queue_id"),
        "wanted_id": missing.get("wanted_id"),
        "issue_id": missing.get("issue_id"),
        "kapowarr_issue_id": missing.get("kapowarr_issue_id"),
        "series_id": missing.get("series_id"),
        "identity_source": missing.get("identity_source"),
        "matching_entry": missing.get("matching_entry"),
        "source_unit": row.get("source_unit"),
    }
    if row.get("source_unit") == "collected_edition":
        summary["collection_range"] = row.get("collection_range")
        summary["collection_title"] = (row.get("collection") or {}).get("collection_title")
        summary["collection_coverage_count"] = len(row.get("collection_coverage") or [])
        summary["covered_queue_ids"] = [
            coverage.get("queue_id")
            for coverage in (row.get("collection_coverage") or [])
            if isinstance(coverage, dict) and coverage.get("queue_id")
        ][:50]
    return summary


def qbit_incomplete_paths_for_importer(importer, kind="comics"):
    loader = getattr(importer, "load_qbit_incomplete_paths", None)
    if not callable(loader):
        return set()
    try:
        return {str(path) for path in (loader(kind) or set()) if str(path or "").strip()}
    except Exception as exc:
        log({
            "event": "pack_probe_incomplete_qbit_probe_failed",
            "kind": kind,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return set()


def split_qbit_incomplete_pack_matches(matched, incomplete_qbit_paths):
    incomplete = set(str(path) for path in (incomplete_qbit_paths or set()) if str(path or "").strip())
    if not incomplete:
        return list(matched or []), []
    ready = []
    waiting = []
    for path, target, row in matched or []:
        if str(path) in incomplete:
            waiting.append((path, target, row))
        else:
            ready.append((path, target, row))
    return ready, waiting


def qbit_incomplete_probe_summary(row):
    summary = probe_row_summary(row)
    summary["skip_reason"] = "source_file_incomplete_qbit_download"
    summary["action_needed"] = "automatic_wait"
    summary["detail"] = "qBittorrent still reports this source file as incomplete; import will retry automatically."
    return summary


# Phase 1 of docs/inkdrop/nfo-pack-monitoring-proposal.md: read the .nfo a pack
# ships with, record what it claims, and gate nothing on it. Sixteen of the
# nineteen real .nfo files in this library are just the folder name echoed
# back; the three that matter are ls -R listings of the multi-series weekly
# bundles, and those listings matched disk exactly. Until that holds across
# more than three packs, this is evidence to look at, not a decision input.
NFO_PROBE_MAX_FILES = 4
NFO_PROBE_UNRELATED_SAMPLE = 25


def pack_nfo_paths(pack_path):
    """The .nfo files shipped with a pack. Top level only, capped, never raises.

    Real packs put it beside the issues and name it after the folder. We do not
    go hunting deeper -- a .nfo buried three levels down is not describing the
    pack, and walking a 700-file tree to find out is not worth it.
    """
    if not pack_path:
        return []
    path = Path(pack_path)
    try:
        if path.is_dir():
            found = sorted(
                item for item in path.iterdir()
                if item.is_file() and item.suffix.lower() == ".nfo"
            )
        elif path.is_file():
            # An archive pack keeps its .nfo next to it, sharing the stem.
            sibling = path.with_suffix(".nfo")
            found = [sibling] if sibling.is_file() else []
        else:
            found = []
    except OSError:
        return []
    return found[:NFO_PROBE_MAX_FILES]


def nfo_evidence_for_pack(pack_path, files, target_titles, target_aliases=None):
    """Report what a pack's .nfo claims about its contents. Observation only.

    `files` is the file list we independently observed; agreement with it is
    the only thing that lifts the .nfo above "a stranger's claim". Every
    failure is swallowed into a reason code -- a malformed .nfo written by an
    anonymous third party must never be able to stall a pack import.
    """
    evidence = {
        "present": False,
        "kind": None,
        "confidence": "none",
        "reason_codes": [],
        "source_name": None,
    }
    if inkdrop_nfo_parser is None:
        evidence["reason_codes"] = ["nfo_parser_unavailable"]
        return evidence

    try:
        candidates = pack_nfo_paths(pack_path)
        if not candidates:
            evidence["reason_codes"] = ["nfo_absent"]
            return evidence

        # Prefer the .nfo that actually lists something. Most are echoes.
        best = None
        for candidate in candidates:
            try:
                text, meta = inkdrop_nfo_parser.read_nfo(candidate)
            except inkdrop_nfo_parser.NfoTooLarge:
                evidence["reason_codes"].append("nfo_too_large")
                continue
            release_name = Path(pack_path).name
            parsed = inkdrop_nfo_parser.parse_nfo(text, release_name=release_name)
            if best is None or (parsed["kind"] == "manifest" and best[1]["kind"] != "manifest"):
                best = (candidate, parsed, meta)
            if parsed["kind"] == "manifest":
                break
        if best is None:
            return evidence

        candidate, parsed, meta = best
        evidence["present"] = True
        evidence["kind"] = parsed["kind"]
        evidence["source_name"] = candidate.name
        evidence["encoding"] = meta.get("encoding")

        observed = [str(item) for item in (files or [])]
        comparison = inkdrop_nfo_parser.compare_manifest_to_listing(parsed, observed)
        scope = inkdrop_nfo_parser.evaluate_pack_scope(
            parsed,
            target_titles or [],
            comparison=comparison,
            target_aliases=target_aliases,
        )

        evidence.update(
            {
                "confidence": scope["confidence"],
                "reason_codes": evidence["reason_codes"] + list(scope["reason_codes"]),
                "matched_entry_count": scope.get("matched_entry_count", 0),
                "total_entry_count": scope.get("total_entry_count", 0),
                "wanted_share": scope.get("wanted_share", 0.0),
                "unrelated_series_count": scope.get("unrelated_series_count", 0),
                "unrelated_series": [
                    row["series"] for row in scope.get("unrelated_series", [])[:NFO_PROBE_UNRELATED_SAMPLE]
                ],
                "matched_series": [row["series"] for row in scope.get("matched_series", [])],
                "listing_agreement": comparison.get("agreement"),
                "listing_status": comparison.get("status"),
                "claimed_not_observed_count": len(comparison.get("claimed_not_observed") or []),
                "observed_not_claimed_count": len(comparison.get("observed_not_claimed") or []),
            }
        )
        return evidence
    except Exception as exc:
        evidence["reason_codes"] = list(evidence["reason_codes"]) + ["nfo_probe_error"]
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        return evidence


def completed_pack_probe_for_item(importer, item, explicit_path=None, max_preview=50, incomplete_qbit_paths=None):
    item = item if isinstance(item, dict) else {}
    review_id = item.get("review_id") or review_id_for(item)
    candidate_title = (item.get("candidate") or {}).get("title") or item.get("title") or item.get("query")
    target_filters = pack_match_series_filters(item)
    try:
        targets = importer.load_comic_targets(target_filters)
    except Exception as exc:
        targets = []
        target_error = f"{type(exc).__name__}: {exc}"
    else:
        target_error = None
    missing_targets = missing_target_filters(target_filters, targets)
    try:
        volume_id = item.get("volume_id") or resolve_volume_id(item.get("series"))
    except Exception:
        volume_id = item.get("volume_id")
    missing = target_missing_issue_maps(targets, volume_id, item) if targets else {}
    locate_details = {
        "path": Path(explicit_path) if explicit_path else None,
        "scanned": 0,
        "limited": False,
        "score": None,
        "elapsed_seconds": 0,
    }
    locate_details = resolve_pack_path(
        item,
        explicit_path=explicit_path,
        max_seconds=env_float("INKDROP_PACK_PROBE_SCAN_SECONDS", 2.0),
        max_entries=env_int("INKDROP_PACK_PROBE_SCAN_ENTRIES", 20000),
        return_details=True,
        shallow=True,
    )
    pack_path = locate_details.get("path")
    pack_exists = bool(pack_path and Path(pack_path).exists())
    files = []
    archive_preview = []
    image_folder_unit_count = 0
    if pack_exists:
        pack_path = Path(pack_path)
        files = comic_files_under(pack_path)
        if not files and pack_path.is_file() and pack_path.suffix.lower() in PACK_ARCHIVE_EXTS:
            archive_preview = [str(name) for name in inspect_archive_pack(pack_path)]
            files = [Path(name) for name in archive_preview]
        elif pack_path.is_dir():
            image_folder_unit_count = len(image_unit_dirs(pack_path))
    matched, not_imported = classify_files(importer, files, targets, missing, item=item) if files and targets else ([], [])
    matched, waiting_for_complete = split_qbit_incomplete_pack_matches(matched, incomplete_qbit_paths)
    target_aliases = []
    for target in targets or []:
        for alias in (target or {}).get("aliases") or []:
            target_aliases.append(alias)
    nfo_evidence = (
        nfo_evidence_for_pack(pack_path, files, target_filters, target_aliases=target_aliases)
        if pack_exists
        else {"present": False, "kind": None, "confidence": "none", "reason_codes": ["pack_not_present"]}
    )
    path_identity_confirmed = True
    if pack_exists:
        path_identity_confirmed = pack_probe_path_identity_confirmed(
            {
                "review_id": review_id,
                "series": item.get("series"),
                "title": candidate_title,
                "pack_path": str(pack_path),
                "pack_exists": True,
            },
            item,
        )
    if pack_exists and not path_identity_confirmed:
        matched = []
        not_imported = []
    if not pack_exists:
        status = "pack_scan_limited" if locate_details.get("limited") else "waiting_for_local_pack"
    elif target_error:
        status = "target_load_error"
    elif not targets:
        status = "missing_target_folders"
    elif not path_identity_confirmed:
        status = "pack_path_identity_unconfirmed"
    elif missing_targets and matched:
        status = "partial_ready_missing_targets"
    elif missing_targets:
        status = "missing_target_folders"
    elif matched:
        status = "ready_to_import"
    elif waiting_for_complete:
        status = "waiting_for_complete_source"
    elif files:
        status = "no_matching_wanted_files"
    else:
        status = "no_files_found"
    return {
        "review_id": review_id,
        "status": status,
        "series": item.get("series"),
        "title": candidate_title,
        "pack_path": str(pack_path) if pack_path else None,
        "pack_exists": pack_exists,
        "path_identity_confirmed": path_identity_confirmed,
        "pack_search": {
            "scanned": locate_details.get("scanned"),
            "limited": bool(locate_details.get("limited")),
            "score": locate_details.get("score"),
            "elapsed_seconds": locate_details.get("elapsed_seconds"),
            "source": locate_details.get("source"),
            "checked_hints": json_safe(locate_details.get("checked_hints") or []),
            "shallow": locate_details.get("shallow"),
        },
        "target_series_filters": target_filters,
        "target_count": len(targets),
        # Observation only -- nothing in the pipeline reads this yet. See
        # docs/inkdrop/nfo-pack-monitoring-proposal.md section 5.
        "nfo": json_safe(nfo_evidence),
        "missing_target_series": missing_targets,
        "target_error": target_error,
        "manifest_missing_count": missing_issue_map_total_count(missing),
        "file_count": len(files),
        "archive_preview": archive_preview[:max_preview],
        "image_folder_unit_count": image_folder_unit_count,
        "would_import_count": matched_wanted_row_count(matched),
        "would_import": [probe_row_summary(row) for _, _, row in matched[:max_preview]],
        "waiting_for_complete_source_count": matched_wanted_row_count(waiting_for_complete),
        "waiting_for_complete_source": [
            qbit_incomplete_probe_summary(row)
            for _, _, row in waiting_for_complete[:max_preview]
        ],
        "not_imported_count": len(not_imported),
        "not_imported": not_imported[:max_preview],
        "mutates_database": False,
        "mutates_filesystem": False,
    }


def pending_pack_probe_records(review_id=None, limit=20):
    wanted_review_id = str(review_id or "").strip()
    actions = load_manual_review_actions()
    records = []
    seen = set()
    for record in reversed(pending_pack_records()):
        if not isinstance(record, dict):
            continue
        rid = str(record.get("review_id") or review_id_for(record) or "").strip()
        if wanted_review_id and rid != wanted_review_id:
            continue
        if rid in seen:
            continue
        status = str(record.get("status") or "").strip().lower()
        if (pack_status_is_terminal(status) or pack_item_finished_by_actions(record, actions)) and not wanted_review_id:
            continue
        seen.add(rid)
        record = dict(record)
        record.setdefault("review_id", rid)
        records.append(record)
        if len(records) >= max(1, int(limit or 20)):
            break
    return records


def probe_completed_packs(review_id=None, explicit_path=None, limit=20, max_preview=50):
    importer = load_importer()
    incomplete_qbit_paths = qbit_incomplete_paths_for_importer(importer, "comics")
    records = pending_pack_probe_records(review_id=review_id, limit=limit)
    if review_id and not records:
        item = load_manual_review_item(
            review_id,
            allow_manual_source_archive=bool(explicit_path and Path(explicit_path).suffix.lower() in PACK_ARCHIVE_EXTS),
            source_path=Path(explicit_path) if explicit_path else None,
        )
        records = [item]
    items = [
        completed_pack_probe_for_item(
            importer,
            record,
            explicit_path=explicit_path if review_id and len(records) == 1 else None,
            max_preview=max_preview,
            incomplete_qbit_paths=incomplete_qbit_paths,
        )
        for record in records
    ]
    counts = {}
    for item in items:
        counts[item.get("status")] = counts.get(item.get("status"), 0) + 1
    return {
        "ok": True,
        "mode": "completed_pack_probe",
        "review_id": review_id,
        "records": len(records),
        "status_counts": counts,
        "ready_count": sum(1 for item in items if item.get("would_import_count")),
        "would_import_count": sum(int(item.get("would_import_count") or 0) for item in items),
        "missing_target_count": sum(len(item.get("missing_target_series") or []) for item in items),
        "mutates_database": False,
        "mutates_filesystem": False,
        "items": items,
    }


PACK_RECONCILE_STOPWORDS = {
    "all",
    "and",
    "chapter",
    "chapters",
    "ch",
    "comic",
    "comics",
    "complete",
    "digital",
    "edition",
    "fixed",
    "manga",
    "media",
    "ongoing",
    "pack",
    "the",
    "llc",
    "inc",
    "viz",
    "vol",
    "volume",
    "vols",
    "with",
}


def pack_reconcile_identity_tokens(value):
    tokens = []
    for token in normalize(re.sub(r"^\[[^\]]+\]\s*", "", str(value or ""))).split():
        if len(token) <= 2:
            continue
        if token in PACK_RECONCILE_STOPWORDS:
            continue
        if re.fullmatch(r"(?:v|vol)?\d+(?:\d+)?", token):
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", token):
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def pack_probe_path_identity_confirmed(probe_item, record=None):
    probe_item = probe_item if isinstance(probe_item, dict) else {}
    record = record if isinstance(record, dict) else {}
    path = str(probe_item.get("pack_path") or "").strip()
    if not path:
        return False
    path_key = normalize(Path(path).name)
    if not path_key:
        return False
    candidate = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
    title_values = [
        probe_item.get("title"),
        candidate.get("title"),
        record.get("title"),
        record.get("query"),
    ]
    path_obj = Path(path)
    for value in title_values:
        identity = dated_weekly_pack_identity(value)
        if identity and dated_weekly_pack_match_score(
            identity,
            path_obj,
            archive_count=2 if path_obj.is_dir() else 0,
        ):
            return True
    title_tokens = []
    for value in title_values:
        for token in pack_reconcile_identity_tokens(value):
            if token not in title_tokens:
                title_tokens.append(token)
    if not title_tokens:
        return False
    series_tokens = pack_reconcile_identity_tokens(probe_item.get("series") or record.get("series"))
    if title_tokens[0] in path_key and title_tokens[0] in series_tokens:
        return True
    required = title_tokens[: min(2, len(title_tokens))]
    if not all(token in path_key for token in required):
        return False
    return True


def completed_pack_reconcile_decision(probe_item, record=None):
    probe_item = probe_item if isinstance(probe_item, dict) else {}
    status = str(probe_item.get("status") or "").strip().lower()
    if not probe_item.get("pack_exists"):
        return None
    if int(probe_item.get("would_import_count") or 0) > 0:
        return None
    if not pack_probe_path_identity_confirmed(probe_item, record):
        return {
            "status": "skipped_path_identity_unconfirmed",
            "reason": "local pack path does not confidently match the pending pack title",
            "terminal": False,
        }
    if status == "no_matching_wanted_files":
        return {
            "status": "finished_no_missing",
            "reason": "completed pack has no files matching active wanted items",
            "terminal": True,
        }
    if status == "no_files_found":
        return {
            "status": "finished_no_importable_source",
            "reason": "completed pack folder/archive contains no importable comic files",
            "terminal": True,
        }
    return None


def reconcile_completed_pack_probe_item(record, probe_item, dry_run=False):
    record = record if isinstance(record, dict) else {}
    probe_item = probe_item if isinstance(probe_item, dict) else {}
    review_id = str(probe_item.get("review_id") or record.get("review_id") or review_id_for(record) or "").strip()
    if not review_id:
        return {"ok": False, "reason": "review_id_missing", "status": "skipped"}
    if pack_item_finished_by_actions({**record, "review_id": review_id}):
        return {"ok": True, "review_id": review_id, "status": "already_finished"}
    decision = completed_pack_reconcile_decision(probe_item, record)
    if not decision:
        return {
            "ok": True,
            "review_id": review_id,
            "status": "skipped",
            "reason": "probe_status_not_terminal",
            "probe_status": probe_item.get("status"),
            "would_import_count": int(probe_item.get("would_import_count") or 0),
        }
    if not decision.get("terminal", True):
        return {
            "ok": True,
            "review_id": review_id,
            "status": "skipped",
            "reason": decision.get("reason") or "probe_not_terminal",
            "probe_status": probe_item.get("status"),
            "decision_status": decision.get("status"),
            "pack_path": probe_item.get("pack_path"),
            "would_import_count": int(probe_item.get("would_import_count") or 0),
        }
    reason = decision["reason"]
    terminal_status = decision["status"]
    result = {
        "status": probe_item.get("status"),
        "result_status": terminal_status,
        "review_id": review_id,
        "series": probe_item.get("series") or record.get("series"),
        "title": probe_item.get("title") or record.get("title") or record.get("query"),
        "pack_path": probe_item.get("pack_path"),
        "selected_path": probe_item.get("pack_path"),
        "pack_review_finished": True,
        "pack_review_finish_reason": reason,
        "source": "completed_pack_reconciler",
        "would_import_count": int(probe_item.get("would_import_count") or 0),
        "not_imported_count": int(probe_item.get("not_imported_count") or 0),
        "manifest_missing_count": int(probe_item.get("manifest_missing_count") or 0),
        "file_count": int(probe_item.get("file_count") or 0),
    }
    if dry_run:
        return {
            "ok": True,
            "review_id": review_id,
            "status": "would_reconcile",
            "terminal_status": terminal_status,
            "reason": reason,
            "result": result,
        }
    mark_pack_handled(review_id, {**record, "review_id": review_id}, terminal_status, reason, result)
    state_result = finish_pack_review_state(review_id, terminal_status, reason, result=result, item=record)
    auto_status = write_pack_auto_import_status(
        {
            "status": terminal_status,
            "result_status": probe_item.get("status"),
            "review_id": review_id,
            "series": result.get("series"),
            "title": result.get("title"),
            "selected_path": result.get("pack_path"),
            "pack_path": result.get("pack_path"),
            "source": "completed_pack_reconciler",
            "pack_review_finished": True,
            "pack_review_finish_reason": reason,
            "would_import_count": result.get("would_import_count"),
            "not_imported_count": result.get("not_imported_count"),
            "manifest_missing_count": result.get("manifest_missing_count"),
        }
    )
    return {
        "ok": True,
        "review_id": review_id,
        "status": "reconciled",
        "terminal_status": terminal_status,
        "reason": reason,
        "pack_path": result.get("pack_path"),
        "state": state_result,
        "auto_status": {
            "status": auto_status.get("status"),
            "review_id": auto_status.get("review_id"),
            "pack_review_finished": auto_status.get("pack_review_finished"),
        },
    }


def reconcile_completed_packs(review_id=None, explicit_path=None, limit=20, max_preview=50, dry_run=False):
    importer = load_importer()
    incomplete_qbit_paths = qbit_incomplete_paths_for_importer(importer, "comics")
    records = pending_pack_probe_records(review_id=review_id, limit=limit)
    if review_id and not records:
        item = load_manual_review_item(
            review_id,
            allow_manual_source_archive=bool(explicit_path and Path(explicit_path).suffix.lower() in PACK_ARCHIVE_EXTS),
            source_path=Path(explicit_path) if explicit_path else None,
        )
        records = [item]
    items = []
    reconciliations = []
    for record in records:
        probe_item = completed_pack_probe_for_item(
            importer,
            record,
            explicit_path=explicit_path if review_id and len(records) == 1 else None,
            max_preview=max_preview,
            incomplete_qbit_paths=incomplete_qbit_paths,
        )
        items.append(probe_item)
        reconciliations.append(reconcile_completed_pack_probe_item(record, probe_item, dry_run=dry_run))
    status_counts = {}
    for item in items:
        status_counts[item.get("status")] = status_counts.get(item.get("status"), 0) + 1
    return {
        "ok": True,
        "mode": "completed_pack_reconcile",
        "dry_run": bool(dry_run),
        "review_id": review_id,
        "records": len(records),
        "status_counts": status_counts,
        "ready_count": sum(1 for item in items if item.get("would_import_count")),
        "would_import_count": sum(int(item.get("would_import_count") or 0) for item in items),
        "reconciled_count": sum(1 for row in reconciliations if row.get("status") == "reconciled"),
        "would_reconcile_count": sum(1 for row in reconciliations if row.get("status") == "would_reconcile"),
        "mutates_database": False,
        "mutates_filesystem": not dry_run,
        "items": items,
        "reconciliations": reconciliations,
    }


def resolve_volume_id(series):
    # Kapowarr is retired; there is no live volumes table to resolve a
    # series title against.
    return None


def candidate_search_terms(item):
    candidate = item.get("candidate") or {}
    values = [candidate.get("title"), item.get("query"), item.get("series")]
    terms = []
    for value in values:
        text = str(value or "")
        norm = normalize(re.sub(r"^\[[^\]]+\]\s*", "", text)) or normalize(text)
        if norm and (len(norm.split()) >= 2 or value == item.get("series")) and norm not in terms:
            terms.append(norm)
    return terms


def dated_weekly_pack_identity(value):
    norm = normalize(value)
    tokens = norm.split()
    if not tokens or "pack" not in tokens or not any(token in tokens for token in ("week", "weekly")):
        return None
    for idx in range(0, max(0, len(tokens) - 2)):
        year, month, day = tokens[idx : idx + 3]
        if not re.fullmatch(r"20\d{2}", year):
            continue
        if not re.fullmatch(r"\d{1,2}", month) or not re.fullmatch(r"\d{1,2}", day):
            continue
        try:
            month_i = int(month)
            day_i = int(day)
        except ValueError:
            continue
        if 1 <= month_i <= 12 and 1 <= day_i <= 31:
            return {
                "year": year,
                "month": f"{month_i:02d}",
                "day": f"{day_i:02d}",
                "month_forms": {str(month_i), f"{month_i:02d}"},
                "day_forms": {str(day_i), f"{day_i:02d}"},
            }
    return None


def dated_weekly_pack_match_score(identity, path, archive_count=0):
    if not identity or not path:
        return 0
    path_norm = normalize(path.name)
    parent_norm = normalize(path.parent.name)
    combined_norm = normalize(f"{path.name} {path.parent.name}")
    path_tokens = set(path_norm.split())
    combined_tokens = set(combined_norm.split())
    if identity["year"] not in combined_tokens:
        return 0
    if not (combined_tokens & identity["month_forms"]) or not (combined_tokens & identity["day_forms"]):
        return 0
    if not any(token in combined_tokens for token in ("week", "weekly")):
        return 0
    has_pack = "pack" in combined_tokens
    if not has_pack and archive_count <= 1:
        return 0

    score = 700
    if identity["year"] in path_tokens and path_tokens & identity["month_forms"] and path_tokens & identity["day_forms"]:
        score += 250
    if any(token in path_tokens for token in ("week", "weekly")):
        score += 150
    if "pack" in path_tokens:
        score += 250
    if "pack" in set(parent_norm.split()):
        score += 50
    return score + min(int(archive_count or 0), 20)


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def load_sab_settings_for_pack_probe():
    try:
        from core import inkdrop_acquire

        return inkdrop_acquire.load_sab_settings()
    except Exception as exc:
        log({"event": "pack_sab_settings_unavailable", "error": f"{type(exc).__name__}: {exc}"})
        return None


def configured_path_mappings(*env_names):
    mappings = []
    for env_name in env_names:
        raw = str(os.environ.get(env_name) or "").strip()
        if not raw:
            continue
        for part in re.split(r"[;\n]+", raw):
            if not part.strip():
                continue
            if "=>" in part:
                source, target = part.split("=>", 1)
            elif "=" in part:
                source, target = part.split("=", 1)
            else:
                continue
            source = source.strip().replace("\\", "/").rstrip("/")
            target = target.strip()
            if source and target:
                mappings.append((source.lower(), Path(target)))
    return mappings


def sab_storage_to_local_path(value):
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("\\", "/")
    normalized_key = normalized.rstrip("/").lower()
    for source, target in configured_path_mappings("INKDROP_SAB_PATH_MAPPINGS", "INKDROP_UNC_PATH_MAPPINGS"):
        if normalized_key == source:
            return target
        if normalized_key.startswith(source + "/"):
            return target.joinpath(*normalized[len(source):].lstrip("/").split("/"))
    path = Path(normalized)
    if path.exists():
        return path
    return path


def sab_history_slots(settings, nzo_ids=None, limit=200, timeout=8):
    settings = settings if isinstance(settings, dict) else {}
    host = str(settings.get("host") or "").rstrip("/")
    api_key = str(settings.get("api_key") or "").strip()
    if not host or not api_key:
        return []
    params = {
        "mode": "history",
        "output": "json",
        "apikey": api_key,
        "limit": str(max(1, min(int(limit or 200), 1000))),
    }
    ids = [str(value).strip() for value in (nzo_ids or []) if str(value or "").strip()]
    if ids:
        params["nzo_ids"] = ",".join(ids)
    url = f"{host}/api?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=max(1, min(float(timeout or 8), 30))) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        log({"event": "pack_sab_history_lookup_failed", "error": f"{type(exc).__name__}: {exc}"})
        return []
    history = payload.get("history") if isinstance(payload, dict) else {}
    slots = history.get("slots") if isinstance(history, dict) else None
    if slots is None and isinstance(payload, dict):
        slots = payload.get("slots")
    return slots if isinstance(slots, list) else []


def sab_slot_identity_values(slot):
    slot = slot if isinstance(slot, dict) else {}
    values = []
    for key in ("nzo_id", "nzoid", "id", "job_id", "filename", "name"):
        value = slot.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                text = str(item or "").strip()
                if text and text not in values:
                    values.append(text)
        else:
            text = str(value or "").strip()
            if text and text not in values:
                values.append(text)
    return values


def sab_slot_storage_values(slot):
    slot = slot if isinstance(slot, dict) else {}
    values = []
    for key in ("storage", "path", "download_path", "folder", "completed_dir", "complete_dir"):
        text = str(slot.get(key) or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def sab_completed_pack_path_hint(item):
    item = item if isinstance(item, dict) else {}
    client_ids = pack_item_client_ids(item)
    if not client_ids:
        return None
    settings = load_sab_settings_for_pack_probe()
    if not settings:
        return None
    slots = sab_history_slots(settings, client_ids)
    if not slots:
        return None
    wanted_ids = {value.lower() for value in client_ids}
    title = normalize((item.get("candidate") or {}).get("title") or item.get("title") or item.get("query"))
    best = None
    best_score = -1
    for slot in slots:
        slot = slot if isinstance(slot, dict) else {}
        identities = {value.lower() for value in sab_slot_identity_values(slot)}
        id_match = bool(wanted_ids.intersection(identities))
        slot_title = normalize(slot.get("name") or slot.get("filename"))
        title_match = bool(title and (title == slot_title or title in slot_title or slot_title in title))
        if not id_match and not title_match:
            continue
        for storage in sab_slot_storage_values(slot):
            path = sab_storage_to_local_path(storage)
            score = (1000 if id_match else 0) + (100 if title_match else 0) + (10 if path and path.exists() else 0)
            if path and score > best_score:
                best = {
                    "path": path,
                    "source": "sab_history",
                    "score": score,
                    "storage": storage,
                    "slot": {key: slot.get(key) for key in ("name", "filename", "nzo_id", "status", "storage")},
                }
                best_score = score
    return best


def json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return value


def pending_item_path_hints(item):
    item = item if isinstance(item, dict) else {}
    hints = []
    for key in ("pack_path", "selected_path", "source_path", "path", "local_path"):
        value = str(item.get(key) or "").strip()
        if value:
            hints.append({"path": Path(value), "source": f"pending_{key}", "score": 900})
    outcome = item.get("outcome") if isinstance(item.get("outcome"), dict) else {}
    for key in ("storage", "path", "local_path", "save_path"):
        path = sab_storage_to_local_path(outcome.get(key))
        if path:
            hints.append({"path": path, "source": f"outcome_{key}", "score": 800})
    sab_hint = sab_completed_pack_path_hint(item)
    if sab_hint:
        hints.append(sab_hint)
    hints.extend(inkdrop_download_task_path_hints(item))
    return hints


def pack_item_queue_ids(item):
    item = item if isinstance(item, dict) else {}
    ids = []

    def add(value):
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)

    for key in ("queue_id", "queueId"):
        add(item.get(key))
    pack_match = item.get("pack_match") if isinstance(item.get("pack_match"), dict) else {}
    for key in ("covered_queue_ids", "queue_ids"):
        values = pack_match.get(key)
        if isinstance(values, (list, tuple)):
            for value in values:
                add(value)
        else:
            add(values)
    for key in ("useful_missing_sample", "collection_coverage", "covered_rows"):
        values = pack_match.get(key)
        if not isinstance(values, list):
            continue
        for row in values:
            if isinstance(row, dict):
                add(row.get("queue_id"))
    return ids


def path_matches_pack_identity(path, item):
    path = Path(path) if path else None
    if not path or not path.exists():
        return False
    return pack_probe_path_identity_confirmed(
        {
            "review_id": (item or {}).get("review_id") or review_id_for(item or {}),
            "series": (item or {}).get("series"),
            "title": (
                (((item or {}).get("candidate") or {}).get("title") if isinstance((item or {}).get("candidate"), dict) else None)
                or (item or {}).get("title")
                or (item or {}).get("query")
            ),
            "pack_path": str(path),
            "pack_exists": True,
        },
        item,
    )


def pack_root_from_download_task_path(value, item):
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    candidates = []
    if path.exists() and path.is_dir():
        candidates.append(path)
    if path.exists() and path.is_file():
        candidates.extend(path.parents)
    elif path.suffix.lower() in COMIC_EXTS:
        candidates.extend(path.parents)
    best = None
    best_score = -1
    title = (
        (((item or {}).get("candidate") or {}).get("title") if isinstance((item or {}).get("candidate"), dict) else None)
        or (item or {}).get("title")
        or (item or {}).get("query")
    )
    weekly_identity = dated_weekly_pack_identity(title)
    for candidate in candidates:
        try:
            if any(candidate == root for root in PACK_SOURCES):
                continue
        except Exception:
            pass
        if not candidate.exists() or not path_matches_pack_identity(candidate, item):
            continue
        score = 1
        if weekly_identity:
            score = dated_weekly_pack_match_score(
                weekly_identity,
                candidate,
                archive_count=2 if candidate.is_dir() else 0,
            )
        if "pack" in set(normalize(candidate.name).split()):
            score += 25
        if score > best_score:
            best = candidate
            best_score = score
    if best:
        return best
    return None


def inkdrop_download_task_path_hints(item, limit=80):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return []
    queue_ids = pack_item_queue_ids(item)
    if not queue_ids:
        return []
    placeholders = ",".join("?" for _ in queue_ids)
    try:
        conn = sqlite3.connect(f"file:{INKDROP_STATE_DB}?mode=ro", uri=True, timeout=8)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"""
                select id, queue_id, source, provider, status, title,
                       save_path, local_path, updated_at
                from download_tasks
                where queue_id in ({placeholders})
                  and (
                    nullif(trim(coalesce(save_path, '')), '') is not null
                    or nullif(trim(coalesce(local_path, '')), '') is not null
                  )
                order by coalesce(updated_at, completed_at, started_at, 0) desc, id desc
                limit ?
                """,
                (*queue_ids, max(1, int(limit or 80))),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log({
            "event": "pack_probe_download_task_path_hints_failed",
            "review_id": item.get("review_id") or review_id_for(item),
            "error": f"{type(exc).__name__}: {exc}",
        })
        return []
    hints = []
    seen = set()
    for row in rows:
        for key, base_score in (("save_path", 970), ("local_path", 930)):
            root = pack_root_from_download_task_path(row[key], item)
            if not root:
                continue
            root_key = str(root)
            if root_key in seen:
                continue
            seen.add(root_key)
            status = str(row["status"] or "").lower()
            score = base_score
            if status in {"ready_to_import", "waiting_for_complete_source", "completed_in_client", "downloading", "queue_verified"}:
                score += 20
            if str(row["source"] or "").lower() in {"download_client", "local_pack", "prowlarr_torrentleech_comics", "prowlarr_dognzb_comics"}:
                score += 10
            hints.append({
                "path": root,
                "source": f"inkdrop_download_task_{key}",
                "score": score,
                "queue_id": row["queue_id"],
                "download_task_id": row["id"],
                "task_status": row["status"],
                "task_source": row["source"],
                "task_provider": row["provider"],
            })
    return hints


def resolve_pack_path(item, explicit_path=None, max_seconds=None, max_entries=None, return_details=False, shallow=False):
    if explicit_path:
        details = {
            "path": Path(explicit_path),
            "scanned": 0,
            "limited": False,
            "score": 10000,
            "minimum_score": 1,
            "elapsed_seconds": 0,
            "source": "explicit_path",
            "shallow": bool(shallow),
        }
        return details if return_details else details["path"]
    start = time.monotonic()
    checked = []
    best_hint = None
    for hint in pending_item_path_hints(item):
        path = hint.get("path")
        checked.append({key: str(value) if key == "path" else value for key, value in hint.items() if key != "slot"})
        if not path or not Path(path).exists():
            continue
        if not best_hint or int(hint.get("score") or 0) > int(best_hint.get("score") or 0):
            best_hint = hint
    if best_hint:
        details = {
            "path": Path(best_hint["path"]),
            "scanned": 0,
            "limited": False,
            "score": best_hint.get("score"),
            "minimum_score": 1,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "source": best_hint.get("source"),
            "checked_hints": checked[:10],
            "shallow": bool(shallow),
        }
        return details if return_details else details["path"]
    details = locate_pack_path(
        item,
        max_seconds=max_seconds,
        max_entries=max_entries,
        return_details=True,
        shallow=shallow,
    )
    details["source"] = "filesystem_scan"
    if checked:
        details["checked_hints"] = checked[:10]
    return details if return_details else details.get("path")


def pack_like_search_terms(terms):
    for term in terms or []:
        tokens = set(normalize(term).split())
        if tokens & {"weekly", "releases", "release", "complete", "collection"}:
            return True
    return False


def minimum_pack_path_score(terms, weekly_identities=None):
    if weekly_identities:
        return 100
    if pack_like_search_terms(terms):
        return 50
    return 1


def locate_pack_path(item, max_seconds=None, max_entries=None, return_details=False, shallow=False):
    terms = candidate_search_terms(item)
    if not terms:
        details = {"path": None, "scanned": 0, "limited": False, "reason": "no_search_terms"}
        return details if return_details else None
    weekly_identities = [identity for identity in (dated_weekly_pack_identity(term) for term in terms) if identity]
    best = None
    best_score = 0
    scanned = 0
    limited = False
    start = time.monotonic()
    deadline = start + float(max_seconds) if max_seconds not in (None, "") and float(max_seconds) > 0 else None
    max_entries = int(max_entries) if max_entries not in (None, "") else None
    for root in PACK_SOURCES:
        if not root.exists():
            continue
        try:
            iterator = root.iterdir() if shallow else root.rglob("*")
        except OSError:
            continue
        for path in iterator:
            scanned += 1
            if max_entries and scanned > max_entries:
                limited = True
                break
            if deadline and time.monotonic() >= deadline:
                limited = True
                break
            if any(part.startswith("_") for part in path.parts):
                continue
            archive_count = 0
            child_names = []
            if path.is_dir():
                try:
                    for child in path.iterdir():
                        if child.is_file() and child.suffix.lower() in COMIC_EXTS:
                            archive_count += 1
                            if len(child_names) < 40:
                                child_names.append(child.name)
                except OSError:
                    child_names = []
            name = normalize(" ".join([path.name, path.parent.name, " ".join(child_names)]))
            score = 0
            if weekly_identities:
                for identity in weekly_identities:
                    score = max(score, dated_weekly_pack_match_score(identity, path, archive_count=archive_count))
            else:
                for term in terms:
                    words = term.split()
                    required = words[: min(2, len(words))]
                    if required and all(word in name for word in required):
                        hits = sum(1 for word in words[:8] if word in name)
                        # Prefer the pack folder over any individual child archive.
                        folder_bonus = 50 if path.is_dir() and archive_count > 1 else 0
                        score = max(score, hits + folder_bonus + min(archive_count, 20))
            if score > best_score:
                best = path
                best_score = score
        if limited:
            break
    min_score = minimum_pack_path_score(terms, weekly_identities)
    if best_score < min_score:
        best = None
    if return_details:
        return {
            "path": best,
            "scanned": scanned,
            "limited": limited,
            "score": best_score,
            "minimum_score": min_score,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "terms": terms,
            "shallow": bool(shallow),
        }
    return best


def archive_member_names(path):
    proc = subprocess.run(["7z", "l", "-ba", str(path)], capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        return []
    names = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[-1]
        if Path(name).suffix.lower() in COMIC_EXTS:
            names.append(name)
    return names


EXTRACT_TIMEOUT_BASE_SECONDS = env_int("INKDROP_PACK_EXTRACT_TIMEOUT_BASE_SECONDS", 1200)
EXTRACT_TIMEOUT_BYTES_PER_SECOND = env_int("INKDROP_PACK_EXTRACT_TIMEOUT_BYTES_PER_SECOND", 5 * 1024 * 1024)
EXTRACT_TIMEOUT_MAX_SECONDS = env_int("INKDROP_PACK_EXTRACT_TIMEOUT_MAX_SECONDS", 3 * 3600)


def extract_archive_timeout_seconds(path):
    try:
        archive_bytes = Path(path).stat().st_size
    except OSError:
        archive_bytes = 0
    scaled = archive_bytes / EXTRACT_TIMEOUT_BYTES_PER_SECOND if EXTRACT_TIMEOUT_BYTES_PER_SECOND > 0 else 0
    return max(EXTRACT_TIMEOUT_BASE_SECONDS, min(scaled, EXTRACT_TIMEOUT_MAX_SECONDS))


def extract_archive(path, dest):
    dest.mkdir(parents=True, exist_ok=True)
    timeout = extract_archive_timeout_seconds(path)
    try:
        proc = subprocess.run(["7z", "x", "-y", f"-o{dest}", str(path)], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"7z extraction timed out after {int(timeout)}s for {path}")
    if proc.returncode != 0:
        raise RuntimeError(f"7z extraction failed for {path}: {proc.stderr or proc.stdout}")


def comic_files_under(path):
    if not path:
        return []
    path = Path(path)
    if path.is_dir():
        return sorted(item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in COMIC_EXTS)
    if path.is_file() and path.suffix.lower() in COMIC_EXTS:
        return [path]
    return []


def natural_sort_key(value):
    parts = re.split(r"(\d+)", str(value).lower())
    return [int(part) if part.isdigit() else part for part in parts]


def direct_image_files(path):
    try:
        return sorted(
            (item for item in Path(path).iterdir() if item.is_file() and item.suffix.lower() in IMAGE_EXTS),
            key=lambda item: natural_sort_key(item.name),
        )
    except OSError:
        return []


def image_unit_dirs(root):
    if not root or not Path(root).is_dir():
        return []
    units = []
    for folder in sorted((item for item in Path(root).rglob("*") if item.is_dir()), key=lambda item: natural_sort_key(str(item))):
        images = direct_image_files(folder)
        if len(images) >= 5:
            units.append((folder, images))
    return units


def image_unit_number(folder):
    numbers = re.findall(r"\d+", str(Path(folder).name))
    if numbers:
        return int(numbers[-1])
    numbers = re.findall(r"\d+", str(Path(folder).parent.name))
    if numbers:
        return int(numbers[-1])
    return None


def write_image_folder_cbz(folder, images, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for idx, image in enumerate(images, start=1):
            archive.write(image, f"{idx:04d}{image.suffix.lower()}")
    return dest


def build_cbz_from_image_units(extracted_root, series, work_root):
    generated = []
    details = []
    seen_numbers = set()
    for folder, images in image_unit_dirs(extracted_root):
        number = image_unit_number(folder)
        if number is None or number in seen_numbers:
            continue
        seen_numbers.add(number)
        name = f"{series or 'Pack'} v{number:03d}.cbz"
        dest = Path(work_root) / "image-folder-cbz" / name
        write_image_folder_cbz(folder, images, dest)
        generated.append(dest)
        details.append({
            "source_folder": str(folder),
            "generated_archive": str(dest),
            "number": number,
            "image_count": len(images),
        })
    return generated, details


def inspect_archive_pack(path):
    if Path(path).suffix.lower() not in PACK_ARCHIVE_EXTS:
        return []
    return [Path(name) for name in archive_member_names(path)]


def normalize_pack_member_path(value):
    text = str(value or "").replace("\\", "/").strip().lower()
    text = re.sub(r"/+", "/", text)
    return text.strip("/")


def manifest_row_matches_path(row, path, *, ambiguous_basenames=frozenset()):
    """Match a manifest entry to an extracted file by PATH identity.

    The old form accepted an unanchored suffix match or bare basename
    equality. In a multi-series pack, 'Alpha/Issue 001.cbz' therefore matched
    BOTH 'extract/Alpha/Issue 001.cbz' and 'extract/Beta/Issue 001.cbz', and
    the first target in iteration order claimed whichever file it saw first --
    Beta's real bytes landed in Alpha's library folder under Alpha's name
    (PASS2-ACQ-03; this is the mechanism that produced the misfiled
    Halloween files).

    A directory-qualified entry matches by anchored path identity first and
    only falls back to its basename when that basename is globally unique
    across the pack's extracted members and manifest entries. A BARE entry
    (no folder component -- providers store plain basenames in file_entry)
    has no path identity at all: for it, an "anchored suffix" match IS a
    basename match, so it goes straight to the uniqueness-gated fallback.
    An ambiguous basename never matches on name alone, whatever the entry's
    shape.
    """
    row = row if isinstance(row, dict) else {}
    entry = row.get("matching_entry") or row.get("file_entry")
    if not entry:
        return False
    entry_norm = normalize_pack_member_path(entry)
    if not entry_norm:
        return False
    path_norm = normalize_pack_member_path(Path(path).as_posix())
    if "/" in entry_norm and (path_norm == entry_norm or path_norm.endswith("/" + entry_norm)):
        return True
    entry_name = Path(entry_norm).name
    if not entry_name or entry_name in ambiguous_basenames:
        return False
    return Path(path_norm).name == entry_name


def pack_ambiguous_basenames(files, missing):
    """Basenames that appear more than once among extracted files or manifest
    entries -- for these, only full-path identity may match.

    Owner maps store the SAME row object under every issue-number-key
    variant ("1"/"001"/"0001"), so rows are deduplicated by object identity
    before counting -- otherwise one row's entry counts several times and a
    genuinely unique basename is flagged ambiguous, breaking the legitimate
    moved-folder fallback."""
    counts = {}
    for path in files or []:
        name = Path(normalize_pack_member_path(Path(path).as_posix())).name
        if name:
            counts[name] = counts.get(name, 0) + 1
    by_owner = missing.get("__by_owner") if isinstance(missing, dict) else None
    entry_counts = {}
    seen_rows = set()
    if isinstance(by_owner, dict):
        for values in by_owner.values():
            if not isinstance(values, dict):
                continue
            for row in values.values():
                if not isinstance(row, dict) or id(row) in seen_rows:
                    continue
                seen_rows.add(id(row))
                entry = row.get("matching_entry") or row.get("file_entry")
                name = Path(normalize_pack_member_path(entry)).name if entry else ""
                if name:
                    entry_counts[name] = entry_counts.get(name, 0) + 1
    return frozenset(
        {name for name, count in counts.items() if count > 1}
        | {name for name, count in entry_counts.items() if count > 1}
    )


def manifest_target_for_path(path, targets, missing, *, ambiguous_basenames=frozenset()):
    """Resolve a file to (target, issue key, manifest row) by owner identity.

    Each target reads exactly one owner's map -- the one its stable identity
    resolves to -- so a returned row always belongs to the returned target.
    Rows that name their own series are additionally required to agree with
    the owning map; a disagreement means an upstream bucketing fault and the
    row is not trusted for this target.
    """
    by_owner = missing.get("__by_owner") if isinstance(missing, dict) else None
    if not isinstance(by_owner, dict):
        return None, None, None
    seen_rows = set()
    for target in targets or []:
        owner = pack_owner_for_target(missing, target)
        if not owner:
            continue
        values = by_owner.get(owner)
        if not isinstance(values, dict):
            continue
        for issue_key_value, row in values.items():
            if not isinstance(row, dict) or id(row) in seen_rows:
                continue
            seen_rows.add(id(row))
            if pack_row_owner_conflict(row, owner):
                continue
            if manifest_row_matches_path(row, path, ambiguous_basenames=ambiguous_basenames):
                return target, issue_key_value, row
    return None, None, None


def pack_related_subseries_blocker(importer, target, path):
    if not target:
        return ""
    blocker = getattr(importer, "related_subseries_source_blocker", None)
    if not callable(blocker):
        return ""
    try:
        return blocker(
            (target or {}).get("title") or (target or {}).get("series"),
            path,
            issue_title=(target or {}).get("issue_title"),
            issue_number=(target or {}).get("issue_number") or (target or {}).get("normalized_number"),
            publisher=(target or {}).get("publisher"),
        ) or ""
    except Exception as exc:
        log({
            "event": "pack_related_subseries_blocker_failed",
            "source": str(path),
            "matched_series": (target or {}).get("title"),
            "error": f"{type(exc).__name__}: {exc}",
        })
        return ""


def classify_files(importer, files, targets, missing, item=None):
    matched = []
    already_or_unmatched = []
    single_file_pack = len(files or []) == 1
    # Computed once per pack: duplicate basenames may only match by full
    # relative path, never by name alone (PASS2-ACQ-03).
    ambiguous_basenames = pack_ambiguous_basenames(files, missing)
    for path in files:
        target = importer.match_comic_target(path, targets)
        number = importer.extract_issue_number(path)
        key = issue_key(number)
        manifest_target, manifest_key, manifest_missing = manifest_target_for_path(
            path, targets, missing, ambiguous_basenames=ambiguous_basenames
        )
        # The manifest's path-derived assignment outranks the filename
        # matcher whenever the two disagree on stable identity -- and a
        # shared title is NOT agreement; that equivalence once handed one
        # series' file to a same-titled neighbour.
        if manifest_target and (not target or not same_pack_target_identity(target, manifest_target)):
            target = manifest_target
        target_missing = missing_issue_map_for_target(missing, target) if target else {}
        if (
            manifest_missing
            and manifest_key is not None
            and (target is manifest_target or same_pack_target_identity(target, manifest_target))
        ):
            target_missing = dict(target_missing)
            target_missing.setdefault(manifest_key, manifest_missing)
            if key not in target_missing:
                key = manifest_key
        related_reason = pack_related_subseries_blocker(importer, target, path)
        if related_reason:
            already_or_unmatched.append(
                {
                    "source": str(path),
                    "issue_number": number,
                    "matched_series": target.get("title") if target else None,
                    "matched_kapowarr_id": target_kapowarr_volume_id(target),
                    "skip_reason": "wrong_series_or_subseries",
                    "detail": related_reason,
                    "action_needed": "retry_another_source",
                }
            )
            continue
        collection_row = collected_edition_match_for_file(
            item or {},
            path,
            target,
            target_missing,
            single_file_pack=single_file_pack,
        )
        if collection_row:
            matched.append((path, target, collection_row))
            continue
        source_unit = manga_pack_unit_for_file(path, target, number, target_missing)
        row = {
            "source": str(path),
            "issue_number": number,
            "matched_series": target.get("title") if target else None,
            "matched_kapowarr_id": target_kapowarr_volume_id(target),
            "source_unit": source_unit,
            "manga_unit_model": source_unit,
        }
        if target and key in target_missing:
            missing_issue = target_missing[key]
            expected_unit = missing_issue_expected_unit(target, missing_issue)
            row["target_unit_type"] = expected_unit
            if source_unit not in {"chapter", "volume", "collected_edition"}:
                row.update(
                    {
                        "missing_issue": missing_issue,
                        "skip_reason": "unit_type_unproven",
                        "detail": "ambiguous_artifact_unit_requires_authoritative_target_or_manifest_proof",
                        "action_needed": "manual_review",
                    }
                )
                already_or_unmatched.append(row)
            elif expected_unit and expected_unit != source_unit:
                row.update(
                    {
                        "missing_issue": missing_issue,
                        "skip_reason": "wrong_unit_type",
                        "detail": f"{source_unit}_artifact_cannot_satisfy_{expected_unit}_target",
                        "action_needed": "retry_another_source",
                    }
                )
                already_or_unmatched.append(row)
            else:
                row["missing_issue"] = missing_issue
                matched.append((path, target, row))
        elif target and source_unit == "chapter":
            row["missing_issue"] = synthetic_missing_row(number, source_unit)
            matched.append((path, target, row))
        else:
            already_or_unmatched.append(row)
    return matched, already_or_unmatched


def matched_wanted_row_count(matched):
    total = 0
    for _path, _target, row in matched or []:
        coverage = row.get("collection_coverage") if isinstance(row, dict) else None
        if isinstance(coverage, list) and coverage:
            total += len(coverage)
        else:
            total += 1
    return total


def is_manga_target(target):
    title = str((target or {}).get("title") or "").strip().lower()
    publisher = str((target or {}).get("publisher") or "").strip().lower()
    return title in MANGA_SERIES or any(marker in publisher for marker in ("manga", "viz", "shueisha", "hakusensha", "kodansha"))


def source_unit_hint(path):
    text = " ".join(Path(path).parts[-2:]).lower()
    if re.search(r"(?:^|[\s._-])(?:chapter|ch)[\s._-]*\d+", text):
        return "chapter"
    if re.search(r"(?:^|[\s._-])(?:v|vol|volume)[\s._-]*0*\d{1,5}(?:\.\d+)?", text):
        return "volume"
    return None


def manga_pack_unit_for_file(path, target, number, missing):
    member_semantics = inkdrop_artifact_acceptance.archive_member_semantics(path, fresh=True)
    if member_semantics.get("semantic_unit") == "chapter":
        return "chapter"
    if member_semantics.get("semantic_unit") in {"volume", "multi_chapter_archive"}:
        return "volume"
    if member_semantics.get("semantic_unit") == "conflicting":
        return "unknown"
    hint = source_unit_hint(path)
    if hint:
        return hint
    if not is_manga_target(target):
        return "volume"
    normalized = normalize_manga_number(number)
    if not normalized:
        return "volume"
    try:
        numeric = int(normalized)
    except (TypeError, ValueError):
        return "volume"
    # Mixed manga packs often contain normal volume files plus high-number
    # chapter files. If a high-number manga item is not in Kapowarr's issue map,
    # keep it as a chapter instead of dropping it as "already/unmatched".
    if numeric >= 100 and issue_key(number) not in missing:
        return "chapter"
    # A low, unprefixed manga number is ambiguous: it commonly means a chapter
    # in digital packs. Do not let target numbering turn that ambiguity into
    # self-fulfilling volume evidence.
    return "unknown"


def missing_issue_expected_unit(target, missing_issue):
    target = target if isinstance(target, dict) else {}
    missing_issue = missing_issue if isinstance(missing_issue, dict) else {}
    for value in (
        missing_issue.get("unit_type"),
        missing_issue.get("semantic_unit_type"),
        (missing_issue.get("matching_entry") or {}).get("unit_type")
        if isinstance(missing_issue.get("matching_entry"), dict)
        else None,
        target.get("unit_type"),
        target.get("semantic_unit_type"),
        target.get("manga_unit_model"),
    ):
        unit = str(value or "").strip().lower()
        if unit in {"chapter", "volume"}:
            return unit
    title = str(missing_issue.get("title") or missing_issue.get("issue_title") or "")
    if re.search(r"\b(?:chapter|ch)\s*\d+", title, re.I):
        return "chapter"
    if re.search(r"\b(?:volume|vol|book)\s*\d+", title, re.I):
        return "volume"
    # No real per-series or per-issue evidence of which shape this title
    # releases in. ComicVine/Kapowarr track plenty of manga issue-by-issue
    # with no volume grouping (e.g. digital-first titles with no tankobon
    # yet), so guessing "volume" here rejected real, correctly-classified
    # chapter artifacts as wrong_unit_type. Trust manga_pack_unit_for_file's
    # actual archive-content classification instead of assuming a shape.
    return None


def synthetic_missing_row(number, source_unit):
    normalized = normalize_manga_number(number)
    if normalized:
        label_number = str(int(normalized))
    else:
        label_number = str(number or "").strip()
    label = "Chapter" if source_unit == "chapter" else "Volume"
    return {
        "issue_id": None,
        "issue": number,
        "calculated": number,
        "title": f"{label} {label_number}".strip(),
        "date": "",
    }


def target_identity_for_pack(target):
    if not isinstance(target, dict):
        return ""
    for key in ("id", "kapowarr_id", "native_series_id", "inkdrop_series_id", "metadata_id"):
        value = target.get(key)
        if value not in (None, ""):
            return str(value)
    return normalize(target.get("title"))


def target_kapowarr_volume_id(target):
    if not isinstance(target, dict):
        return None
    for key in ("kapowarr_id", "id"):
        value = target.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def comicinfo_xml(series, number, title=None, year=None, unit_type="volume"):
    def node(name, value):
        if value is None or value == "":
            return ""
        value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"  <{name}>{value}</{name}>\n"
    unit_type = str(unit_type or "volume").strip().lower()
    display_number = int(float(number))
    if unit_type == "chapter":
        number_nodes = f"{node('Number', f'{display_number:03d}')}"
        format_name = "Manga Chapter"
        default_title = f"Chapter {display_number}"
    else:
        number_nodes = f"{node('Volume', display_number)}"
        format_name = "Manga"
        default_title = f"Volume {display_number:02d}"
    return (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        "<ComicInfo>\n"
        f"{node('Series', series)}"
        f"{node('Title', title or default_title)}"
        f"{number_nodes}"
        f"{node('Year', year)}"
        f"{node('Format', format_name)}"
        "  <LanguageISO>en</LanguageISO>\n"
        "</ComicInfo>\n"
    )


def ensure_comicinfo(dest, target, row):
    if Path(dest).suffix.lower() != ".cbz":
        return {"written": False, "reason": "not_cbz"}
    source_unit = str(row.get("source_unit") or row.get("unit_type") or "volume").strip().lower()
    if source_unit not in {"chapter", "volume", "pack"}:
        source_unit = "volume"
    with zipfile.ZipFile(dest, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        if any(name.lower() == "comicinfo.xml" for name in archive.namelist()):
            return {"written": False, "reason": "already_present"}
        archive.writestr(
            "ComicInfo.xml",
            comicinfo_xml(
                target.get("title"),
                row.get("issue_number"),
                title=(row.get("missing_issue") or {}).get("title"),
                year=(row.get("missing_issue") or {}).get("date", "")[:4],
                unit_type=source_unit,
            ),
        )
    return {"written": True, "reason": "generated_for_kavita_manga", "unit_type": source_unit}


def existing_manga_unit(importer, target, row, source_path, target_dir):
    if not is_manga_target(target):
        return None
    explicit_unit = str(row.get("source_unit") or row.get("unit_type") or "").strip().lower()
    source_unit = explicit_unit or "volume"
    if source_unit == "pack":
        source_unit = "volume"
    if source_unit not in {"chapter", "volume"}:
        source_unit = "volume"
    source_number = normalize_manga_number(row.get("issue_number"))

    unit_reader = getattr(importer, "manga_file_unit_and_number", None)
    if unit_reader:
        try:
            detected_unit, detected_number = unit_reader(source_path)
            if not explicit_unit and detected_unit in {"chapter", "volume"}:
                source_unit = detected_unit
            source_number = normalize_manga_number(detected_number) or source_number
        except Exception:
            pass
    if not source_number:
        return None

    extractor = getattr(importer, "extract_issue_number", lambda path: None)
    internal = getattr(importer, "is_internal_import_path", lambda path, root=None: False)
    for candidate in Path(target_dir).rglob("*"):
        if internal(candidate, target_dir):
            continue
        if not candidate.is_file() or candidate.suffix.lower() not in {".cbz", ".cbr", ".pdf"}:
            continue
        try:
            if candidate.resolve() == Path(source_path).resolve():
                continue
        except FileNotFoundError:
            continue
        candidate_unit = source_unit_hint(candidate) or source_unit
        candidate_number = normalize_manga_number(extractor(candidate))
        if candidate_unit == source_unit and candidate_number == source_number:
            existing = candidate
            break
    else:
        existing = None

    finder = getattr(importer, "find_existing_manga_unit_file", None)
    if finder:
        try:
            existing = existing or finder(target, source_number, source_unit, exclude=source_path)
        except Exception:
            existing = existing
    if not existing:
        for candidate in Path(target_dir).rglob("*"):
            if internal(candidate, target_dir):
                continue
            if not candidate.is_file() or candidate.suffix.lower() not in {".cbz", ".cbr", ".pdf"}:
                continue
            try:
                if candidate.resolve() == Path(source_path).resolve():
                    continue
            except FileNotFoundError:
                continue
            try:
                candidate_unit, candidate_number = unit_reader(candidate) if unit_reader else (source_unit, None)
            except Exception:
                candidate_unit, candidate_number = source_unit, None
            candidate_number = normalize_manga_number(
                candidate_number
                or getattr(importer, "extract_issue_number", lambda path: None)(candidate)
            )
            if candidate_unit == source_unit and candidate_number == source_number:
                existing = candidate
                break
    if not existing:
        return None
    return {
        "existing_path": str(existing),
        "unit_model": source_unit,
        "normalized_number": source_number,
        "reason": "already_verified_manga_file_present",
    }


COPY_SUFFIX_RE = re.compile(r"^(?P<stem>.+?) \((?P<counter>[2-9][0-9]{0,2})\)(?P<suffix>\.[^.]+)$")


def suffixless_existing_dest(dest):
    """Return the base file when a generated '(2)' style destination already exists."""
    dest = Path(dest)
    match = COPY_SUFFIX_RE.match(dest.name)
    if not match:
        return None
    base = dest.with_name(f"{match.group('stem')}{match.group('suffix')}")
    return base if base.exists() else None


def existing_canonical_file(dest):
    dest = Path(dest)
    if dest.exists():
        return dest
    suffixless = suffixless_existing_dest(dest)
    if suffixless:
        return suffixless
    match = COPY_SUFFIX_RE.match(dest.name)
    stem = match.group("stem") if match else dest.stem
    suffix = match.group("suffix") if match else dest.suffix
    pattern = re.compile(rf"^{re.escape(stem)} \(([2-9][0-9]*)\){re.escape(suffix)}$")
    try:
        siblings = sorted(
            candidate
            for candidate in dest.parent.iterdir()
            if candidate.is_file() and pattern.match(candidate.name)
        )
    except OSError:
        siblings = []
    return siblings[0] if siblings else None


def same_file_in_target(importer, target_dir, path, digest, dry_run):
    if dry_run:
        return None
    finder = getattr(importer, "find_same_file", None)
    if not callable(finder):
        return None
    try:
        existing = finder(Path(target_dir), Path(path), digest)
    except Exception:
        return None
    if not existing:
        return None
    try:
        if existing.resolve() == Path(path).resolve():
            return None
    except FileNotFoundError:
        pass
    return existing


def verify_imported_items_for_library(importer, imported, wait_for_library_scan):
    verifier = getattr(importer, "verify_imported_items", None)
    if not callable(verifier):
        return {}
    try:
        signature = inspect.signature(verifier)
    except (TypeError, ValueError):
        signature = None
    if signature and "poll_library_visibility" in signature.parameters:
        return verifier(imported, poll_library_visibility=bool(wait_for_library_scan))
    return verifier(imported, poll_kavita=bool(wait_for_library_scan))


def importer_bool_setting(importer, name, default):
    reader = getattr(importer, name, None)
    if not callable(reader):
        return bool(default)
    try:
        return bool(reader())
    except Exception:
        return bool(default)


def sync_pack_library_frontends(importer, folders):
    return inkdrop_library_frontends.sync_library_frontends(
        folders,
        frontend_sync_enabled=importer_bool_setting(importer, "frontend_sync_after_import_enabled", True),
        kavita_scan_enabled=importer_bool_setting(importer, "kavita_scan_after_import_enabled", True),
        trigger_kavita_scan=getattr(importer, "trigger_kavita_scan_folder", None),
        load_komga_settings=getattr(importer, "load_komga_settings", None),
        trigger_komga_scan=getattr(importer, "trigger_komga_scan_folder", None),
        log_event=log,
        event_prefix="pack_import_",
    )


def import_matched_files(importer, matched, dry_run, max_files, review_id, extracted_root=None, wait_for_kavita_scan=False, wait_for_library_scan=None):
    if wait_for_library_scan is None:
        wait_for_library_scan = bool(wait_for_kavita_scan)
    else:
        wait_for_library_scan = bool(wait_for_library_scan)
    conn = importer.connect()
    conn.execute(f"pragma busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    imported = []
    skipped_existing = []
    bad_archives = []
    handled_units = set()
    scan_folders = set()
    for path, target, row in matched:
        source_unit = str(row.get("source_unit") or row.get("unit_type") or "volume").strip().lower()
        if source_unit == "pack":
            source_unit = "volume"
        if source_unit not in {"chapter", "volume", "collected_edition"}:
            source_unit = "volume"
        if hasattr(importer, "comic_import_target_dir"):
            target_dir = Path(importer.comic_import_target_dir(target))
        else:
            target_dir = (
                Path(importer.kavita_manga_series_dir(target))
                if is_manga_target(target) and hasattr(importer, "kavita_manga_series_dir")
                else Path(target["folder"])
            )
        digest = f"dry-run:{path}" if dry_run else importer.sha256(path)
        if source_unit == "collected_edition" and hasattr(importer, "collection_dest"):
            collection = row.get("collection") or {}
            dest = importer.collection_dest(target_dir, path, collection)
            canonical = {
                "canonical_filename": Path(dest).name,
                "canonical_issue_number": row.get("issue_number"),
                "canonical_issue_title": collection.get("collection_title"),
                "canonical_year": collection.get("year"),
                "canonical_source": "collected_edition_range_hint",
                "collection": collection,
                "collection_range": row.get("collection_range") or collection.get("range"),
                "collection_coverage": row.get("collection_coverage") or [],
                "collection_coverage_count": len(row.get("collection_coverage") or []),
            }
        elif is_manga_target(target) and source_unit == "chapter" and hasattr(importer, "suwayomi_chapter_dest"):
            dest = importer.suwayomi_chapter_dest(target_dir, target, path, row.get("issue_number"))
            canonical = {
                "canonical_issue_number": row.get("issue_number"),
                "manga_unit_model": "chapter",
                "source_unit": "chapter",
            }
        else:
            try:
                dest, canonical = importer.canonical_comic_dest(
                    target_dir, path, target, source_unit=source_unit
                )
            except TypeError:
                # Compatibility for older importer modules used by standalone
                # pack-repair tooling.
                dest, canonical = importer.canonical_comic_dest(target_dir, path, target)
        missing_issue = row.get("missing_issue") or {}
        target_volume_id = target_kapowarr_volume_id(target)
        missing_issue_id = missing_issue.get("issue_id") or missing_issue.get("inkdrop_issue_id")
        kapowarr_issue_id = missing_issue.get("kapowarr_issue_id") or missing_issue.get("matched_kapowarr_issue_id")
        if not kapowarr_issue_id and target_volume_id is not None and str(missing_issue_id or "").isdigit():
            kapowarr_issue_id = missing_issue_id
        event = {
            "event": "pack_import_file",
            "review_id": review_id,
            "source": str(path),
            "dest": str(dest),
            "sha256": digest,
            "dry_run": dry_run,
            "matched_series": target["title"],
            "series": target["title"],
            "matched_kapowarr_id": target_volume_id,
            "matched_kapowarr_issue_id": kapowarr_issue_id,
            "matched_inkdrop_issue_id": missing_issue_id,
            "series_id": missing_issue.get("series_id") or target.get("native_series_id") or target.get("inkdrop_series_id"),
            "issue_id": missing_issue_id,
            "wanted_id": missing_issue.get("wanted_id"),
            "queue_id": missing_issue.get("queue_id"),
            "issue_number": row.get("issue_number"),
            "normalized_number": normalize_manga_number(row.get("issue_number")),
            "truth_model": "kavita_collection" if source_unit == "collected_edition" else ("kavita_manga" if is_manga_target(target) else "kapowarr"),
            "source_unit": source_unit,
            "manga_unit_model": source_unit,
            "pack_matching_entry": missing_issue.get("matching_entry"),
        }
        if source_unit == "collected_edition":
            event["collection"] = row.get("collection") or {}
            event["collection_range"] = row.get("collection_range") or (row.get("collection") or {}).get("range")
            event["collection_coverage"] = row.get("collection_coverage") or []
            event["collection_coverage_count"] = len(row.get("collection_coverage") or [])
        if canonical:
            event.update(canonical)
        event["source_unit"] = source_unit
        event["manga_unit_model"] = source_unit
        identity_fields = {}
        identity_reader = getattr(importer, "target_identity_fields", None)
        if callable(identity_reader):
            try:
                identity_fields = identity_reader(target) or {}
            except Exception:
                identity_fields = {}
        if identity_fields:
            event.update(identity_fields)
        event.setdefault("native_series_id", target.get("native_series_id") or target.get("inkdrop_series_id"))
        event.setdefault("metadata_provider", target.get("metadata_provider"))
        event.setdefault("metadata_id", target.get("metadata_id"))
        bad_memory = importer.find_artifact_bad_content_memory(
            conn, path, file_sha256=digest
        ) if hasattr(importer, "find_artifact_bad_content_memory") else None
        if bad_memory:
            event.update({
                "event": "pack_skip_known_bad_artifact_content",
                "skip_reason": "known_bad_artifact_content",
                "state": "suppressed_bad_artifact",
                "action_needed": "retry_another_source",
            })
            bad_archives.append(event)
            log(event)
            continue
        unit_key = (
            target_identity_for_pack(target),
            source_unit,
            normalize_manga_number(row.get("issue_number")),
        )
        if unit_key[0] and unit_key[2] and unit_key in handled_units:
            event["event"] = "pack_skip_duplicate_unit_in_run"
            event["state"] = "suppressed_completed"
            event["skip_reason"] = "unit_already_imported_or_suppressed_in_this_pack"
            skipped_existing.append(event)
            log(event)
            continue
        if path.suffix.lower() == ".pdf" and dest.suffix.lower() != ".cbz":
            dest = dest.with_suffix(".cbz")
            event["dest"] = str(dest)
        same_existing = same_file_in_target(importer, target_dir, path, digest, dry_run)
        if same_existing:
            event["event"] = "pack_skip_same_file_already_present"
            event["dest"] = str(same_existing)
            event["state"] = "suppressed_completed"
            event["skip_reason"] = "same_file_already_visible_or_present"
            skipped_existing.append(event)
            log(event)
            if unit_key[0] and unit_key[2]:
                handled_units.add(unit_key)
            continue
        existing_canonical = None
        canonical_finder = getattr(importer, "existing_canonical_dest", None)
        if canonical_finder and canonical:
            try:
                existing_canonical = canonical_finder(target_dir, canonical, path)
            except Exception:
                existing_canonical = None
        if not existing_canonical:
            existing_canonical = existing_canonical_file(dest)
        if existing_canonical:
            try:
                same_source = existing_canonical.resolve() == Path(path).resolve()
            except FileNotFoundError:
                same_source = False
            if not same_source:
                event["event"] = "pack_skip_canonical_already_present"
                event["dest"] = str(existing_canonical)
                event["state"] = "suppressed_completed"
                event["skip_reason"] = "canonical_file_already_visible_or_present"
                skipped_existing.append(event)
                log(event)
                if unit_key[0] and unit_key[2]:
                    handled_units.add(unit_key)
                continue
        existing = existing_manga_unit(importer, target, row, path, target_dir)
        if existing:
            event.update(existing)
            event["event"] = "pack_skip_existing_manga"
            event["state"] = "suppressed_completed"
            skipped_existing.append(event)
            log(event)
            if unit_key[0] and unit_key[2]:
                handled_units.add(unit_key)
            continue
        if len(imported) >= max_files:
            break
        archive_check = importer.validate_comic_archive(path)
        event["archive_check"] = archive_check
        if not archive_check.get("ok"):
            bad_event = {**event, "event": "pack_skip_bad_archive"}
            append_manual_review("pack_import_bad_archive", bad_event, db_path=INKDROP_STATE_DB)
            bad_archives.append(bad_event)
            log(bad_event)
            continue
        acceptance = inkdrop_artifact_acceptance.decide_acceptance(
            path,
            target=target,
            event=event,
            row=row,
            archive_check=archive_check,
            collection=row.get("collection") if source_unit == "collected_edition" else None,
            source_unit=source_unit,
        )
        event["artifact_acceptance"] = inkdrop_artifact_acceptance.sanitized_decision(acceptance)
        if not acceptance.get("completion_eligible"):
            blocked_event = {
                **event,
                "event": "pack_skip_artifact_acceptance_gate",
                "skip_reason": acceptance.get("decision") or "artifact_acceptance_rejected",
                "state": "manual_review_required" if acceptance.get("decision") == "manual_review_required" else "suppressed_bad_artifact",
                "action_needed": "manual_review" if acceptance.get("decision") == "manual_review_required" else "retry_another_source",
            }
            if not dry_run and hasattr(importer, "record_artifact_bad_content_memory"):
                file_sha = importer.sha256(path) if hasattr(importer, "sha256") else None
                bad_conn = importer.connect()
                try:
                    blocked_event["bad_content_identity"] = importer.record_artifact_bad_content_memory(
                        bad_conn, file_sha, path, acceptance
                    )
                finally:
                    bad_conn.close()
            append_manual_review("artifact_acceptance_gate", blocked_event, db_path=INKDROP_STATE_DB)
            bad_archives.append(blocked_event)
            log(blocked_event)
            continue
        if not dry_run:
            if source_unit == "collected_edition" and hasattr(importer, "copy_collection_archive"):
                event["normalized_archive"] = importer.copy_collection_archive(path, dest, row.get("collection") or {})
            elif path.suffix.lower() == ".cbr":
                dest = importer.unique_dest_name(dest.parent, dest.with_suffix(".cbz").name)
                event["dest"] = str(dest)
                existing_after_normalize = existing_canonical_file(dest)
                if existing_after_normalize:
                    event["event"] = "pack_skip_canonical_already_present"
                    event["dest"] = str(existing_after_normalize)
                    event["state"] = "suppressed_completed"
                    event["skip_reason"] = "canonical_file_already_visible_or_present"
                    skipped_existing.append(event)
                    log(event)
                    if unit_key[0] and unit_key[2]:
                        handled_units.add(unit_key)
                    continue
                event["normalized_archive"] = importer.repack_cbr_to_cbz(path, dest)
            elif path.suffix.lower() == ".pdf":
                dest = importer.unique_dest_name(dest.parent, dest.with_suffix(".cbz").name)
                event["dest"] = str(dest)
                existing_after_normalize = existing_canonical_file(dest)
                if existing_after_normalize:
                    event["event"] = "pack_skip_canonical_already_present"
                    event["dest"] = str(existing_after_normalize)
                    event["state"] = "suppressed_completed"
                    event["skip_reason"] = "canonical_file_already_visible_or_present"
                    skipped_existing.append(event)
                    log(event)
                    if unit_key[0] and unit_key[2]:
                        handled_units.add(unit_key)
                    continue
                issue_row = row.get("missing_issue") or {
                    "issue_number": row.get("issue_number"),
                    "calculated_issue_number": row.get("issue_number"),
                    "title": target.get("title"),
                    "year": target.get("year"),
                }
                event["normalized_archive"] = importer.convert_pdf_to_cbz(path, dest, target, issue_row)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            if event.get("truth_model") == "kavita_manga":
                event["comicinfo"] = ensure_comicinfo(dest, target, row)
            conn.execute(
                "insert or replace into imported_files values (?,?,?,?,?)",
                (digest, str(path), str(dest), path.stat().st_size, time.time()),
            )
            commit_with_retry(conn)
            if unit_key[0] and unit_key[2]:
                handled_units.add(unit_key)
            scan_folders.add(str(target_dir))
        log(event)
        imported.append(event)
    # Kapowarr is retired; scanning is Kavita/Komga-only now.
    scan_tasks = []
    kavita_tasks = []
    komga_tasks = []
    library_scan_tasks = {"kavita": kavita_tasks, "komga": komga_tasks}
    if not dry_run:
        frontend_sync = sync_pack_library_frontends(importer, scan_folders)
        kavita_tasks = frontend_sync.get("kavita") or []
        komga_tasks = frontend_sync.get("komga") or []
        library_scan_tasks = frontend_sync.get("library_scan_tasks") or {
            "kavita": kavita_tasks,
            "komga": komga_tasks,
        }
        if scan_tasks or kavita_tasks or komga_tasks:
            time.sleep(20)
    verification = (
        verify_imported_items_for_library(importer, imported, wait_for_library_scan)
        if imported and not dry_run
        else {}
    )
    if verification.get("failure_count"):
        append_manual_review(
            "pack_import_verification_failed",
            {"review_id": review_id, "verification": verification},
            db_path=INKDROP_STATE_DB,
        )
    try:
        conn.close()
    except Exception:
        pass
    return {
        "imported": imported,
        "skipped_existing": skipped_existing,
        "bad_archives": bad_archives,
        "bad_archive_count": len(bad_archives),
        "kapowarr_scan_tasks": scan_tasks,
        "kavita_scan_tasks": kavita_tasks,
        "komga_scan_tasks": komga_tasks,
        "library_scan_tasks": library_scan_tasks,
        "wait_for_library_scan": wait_for_library_scan,
        "wait_for_kavita_scan": wait_for_library_scan,
        "verification": verification,
    }


def update_pack_state(review_id, status, details):
    state = read_json(PACK_REVIEW_STATE_FILE, {"active": None, "history": []})
    active = state.get("active") or {}
    if active and active.get("review_id") not in {None, review_id}:
        state.setdefault("history", []).append({
            "review_id": review_id,
            "status": status,
            "ts": time.time(),
            "event": "pack_import_state_not_active",
            "active_review_id": active.get("review_id"),
            **details,
        })
        write_json(PACK_REVIEW_STATE_FILE, state)
        return
    active.update({"review_id": review_id, "status": status, "updated_at": time.time(), **details})
    state["active"] = active
    state.setdefault("history", []).append({"review_id": review_id, "status": status, "ts": time.time(), **details})
    write_json(PACK_REVIEW_STATE_FILE, state)


def record_native_pack_import_results(result, item, pack_path, candidate_title):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    try:
        return inkdrop_state.record_pack_import_results(
            INKDROP_STATE_DB,
            imported=result.get("imported") or [],
            skipped_existing=result.get("skipped_existing") or [],
            review_id=result.get("review_id"),
            pack_path=str(pack_path),
            series=item.get("series"),
            title=candidate_title,
            verification=result.get("verification") or {},
            source="pack_import",
        )
    except Exception as exc:
        error = {"ok": False, "reason": "inkdrop_state_pack_import_failed", "error": f"{type(exc).__name__}: {exc}"}
        log({"event": "inkdrop_state_pack_import_failed", **error, "review_id": result.get("review_id")})
        return error


PACK_NO_MATCH_IDENTITY_KEYS = {
    "source_attempt_id",
    "candidate_identity",
    "download_id",
    "downloadId",
    "external_id",
    "externalId",
    "download_url_hash",
    "downloadUrlHash",
    "url_hash",
    "torrent_hash",
    "torrentHash",
    "info_hash",
    "infoHash",
    "hash",
    "nzo_id",
    "nzoId",
    "client_external_id",
    "clientExternalId",
}
PACK_NO_MATCH_PATH_KEYS = {
    "pack_path",
    "selected_path",
    "source_path",
    "sourcePath",
    "local_path",
    "localPath",
    "save_path",
    "savePath",
    "download_path",
    "downloadPath",
    "pack_archive_path",
    "path",
    "filename",
}
PACK_NO_MATCH_TITLE_KEYS = {"title", "query", "summary", "candidate_title", "pack_title"}
PACK_NO_MATCH_REVIEW_KEYS = {"review_id", "pack_review_id", "pending_pack_review_id", "source_worker_pack_review_id"}


def _json_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except Exception:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _pack_text_values(*values):
    out = []

    def add(value):
        if value in (None, "", [], {}):
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        if isinstance(value, dict):
            return
        text = str(value).strip()
        if text:
            out.append(text)

    for value in values:
        add(value)
    return out


def _pack_collect_keys(*containers, keys):
    values = []
    for container in containers:
        data = container if isinstance(container, dict) else {}
        for key in keys:
            if key in data:
                values.extend(_pack_text_values(data.get(key)))
    return values


def _pack_identity_set(*values):
    return {str(value).strip().lower() for value in _pack_text_values(*values) if str(value).strip()}


def _pack_path_key(value):
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    return re.sub(r"/+", "/", text).rstrip("/").lower()


def _pack_path_matches(left, right):
    left_key = _pack_path_key(left)
    right_key = _pack_path_key(right)
    if not left_key or not right_key:
        return False
    return left_key == right_key or left_key.startswith(right_key + "/") or right_key.startswith(left_key + "/")


def _pack_no_match_task_matches_failure(task, result, item, pack_path, candidate_title, series):
    task = task if isinstance(task, dict) else {}
    result = result if isinstance(result, dict) else {}
    item = item if isinstance(item, dict) else {}
    task_raw = _json_dict(task.get("raw_json"))
    task_candidate = _json_dict(task_raw.get("candidate"))
    task_outcome = _json_dict(task_raw.get("outcome"))
    item_candidate = _json_dict(item.get("candidate"))
    item_outcome = _json_dict(item.get("outcome"))
    item_pack_info = _json_dict(item.get("pack_info"))

    expected_reviews = _pack_identity_set(_pack_collect_keys(item, result, keys=PACK_NO_MATCH_REVIEW_KEYS))
    task_reviews = _pack_identity_set(_pack_collect_keys(task_raw, keys=PACK_NO_MATCH_REVIEW_KEYS))
    if expected_reviews and task_reviews and expected_reviews.intersection(task_reviews):
        return True

    expected_identities = _pack_identity_set(
        _pack_collect_keys(item, result, item_candidate, item_outcome, item_pack_info, keys=PACK_NO_MATCH_IDENTITY_KEYS)
    )
    task_identities = _pack_identity_set(
        task.get("source_attempt_id"),
        task.get("external_id"),
        task.get("candidate_identity"),
        _pack_collect_keys(task_raw, task_candidate, task_outcome, keys=PACK_NO_MATCH_IDENTITY_KEYS),
    )
    if expected_identities and task_identities and expected_identities.intersection(task_identities):
        return True

    expected_paths = _pack_text_values(
        pack_path,
        _pack_collect_keys(item, result, item_outcome, item_pack_info, keys=PACK_NO_MATCH_PATH_KEYS),
    )
    task_paths = _pack_text_values(
        task.get("local_path"),
        _pack_collect_keys(task_raw, task_candidate, task_outcome, keys=PACK_NO_MATCH_PATH_KEYS),
    )
    if expected_paths and task_paths and any(_pack_path_matches(left, right) for left in expected_paths for right in task_paths):
        return True

    series_key = normalize(series)
    expected_titles = {
        normalize(value)
        for value in _pack_text_values(
            candidate_title,
            _pack_collect_keys(item, result, item_candidate, item_pack_info, keys=PACK_NO_MATCH_TITLE_KEYS),
        )
    }
    expected_titles = {value for value in expected_titles if value and value != series_key}
    task_titles = {
        normalize(value)
        for value in _pack_text_values(
            task.get("title"),
            _pack_collect_keys(task_raw, task_candidate, keys=PACK_NO_MATCH_TITLE_KEYS),
        )
    }
    task_titles = {value for value in task_titles if value}
    return bool(expected_titles and task_titles and expected_titles.intersection(task_titles))


def record_native_pack_no_match(result, item, pack_path, candidate_title, missing):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    if not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "state_db_missing", "db_path": str(INKDROP_STATE_DB)}
    series = str((item or {}).get("series") or "").strip()
    if not series:
        return {"ok": False, "reason": "series_missing"}
    issue_keys = set()
    by_owner = (missing or {}).get("__by_owner") if isinstance(missing, dict) else None
    if isinstance(by_owner, dict):
        for values in by_owner.values():
            if isinstance(values, dict):
                issue_keys.update(key for key in values.keys() if key is not None)
    elif isinstance(missing, dict):
        issue_keys = {key for key in (missing or {}).keys() if key is not None}
    if not issue_keys:
        return {"ok": False, "reason": "missing_issue_map_empty"}
    rows = []
    try:
        conn = sqlite3.connect(f"file:{INKDROP_STATE_DB}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            candidates = conn.execute(
                """
                select q.id, q.state, q.current_source, q.raw_json,
                       q.wanted_id, q.series_id, q.issue_id, i.issue_number, i.normalized_number
                from queue_items q
                join series s on s.id = q.series_id
                left join issues i on i.id = q.issue_id
                where q.active = 1
                  and lower(s.title) = lower(?)
                  and q.state in ('downloading', 'importing', 'source_wait', 'searching', 'queued')
                """,
                (series,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "reason": "state_read_failed", "error": f"{type(exc).__name__}: {exc}"}

    for row in candidates:
        key = issue_key(row["normalized_number"] or row["issue_number"])
        if key in issue_keys:
            rows.append(dict(row))
    if not rows:
        return {"ok": True, "recorded": 0, "reason": "no_active_matching_queue_rows"}

    now = time.time()
    message = "Pack candidate did not contain a file matching this missing issue; retrying alternate source"
    attempt = {
        "source": "pack_import",
        "provider_id": "local_pack",
        "provider": "local_pack",
        "source_type": "local",
        "protocol": "local",
        "download_client": "pack_import",
        "status": "retry_scheduled",
        "lifecycle_phase": "retry_later",
        "outcome": "no_candidate",
        "display_phase": "retry_later",
        "retry_eligible": True,
        "title": candidate_title or (item or {}).get("title") or series,
        "reason": message,
        "failure_reason": "pack_no_matching_missing_file",
        "review_id": (item or {}).get("review_id") or result.get("review_id"),
        "pack_path": str(pack_path or ""),
        "matched_missing_count": int(result.get("matched_missing_count") or 0),
        "not_imported_count": int(result.get("not_imported_count") or len(result.get("not_imported") or [])),
        "retry_state_key": f"pack_no_match|{(item or {}).get('review_id') or result.get('review_id') or ''}|{normalize(candidate_title)}",
        "ts": now,
    }
    retired_tasks = 0
    bad_source_records = 0
    preserved_active_handoffs = 0
    preserved_queue_rows = 0
    released_rows = []
    errors = []
    stale_statuses = {
        "sent",
        "download_started",
        "downloading",
        "started_waiting",
        "already_downloading",
        "waiting_for_transfer",
        "transfer_in_progress",
        "transfer_settling",
        "waiting_for_staged_file",
        "staged_file_settling",
        "completed_in_client",
        "ready_to_import",
        "import_busy",
        "verification_pending",
    }
    try:
        with inkdrop_state.connect(INKDROP_STATE_DB, timeout_seconds=90, busy_timeout_ms=90000) as con:
            inkdrop_state.init_schema(con)
            for row in rows:
                task_rows = con.execute(
                    f"""
                    select id, source_attempt_id, source, provider_id, provider, protocol, download_client,
                           external_id, candidate_identity, title, local_path, raw_json
                    from download_tasks
                    where queue_id=?
                      and status in ({','.join('?' for _ in stale_statuses)})
                    order by coalesce(updated_at, started_at, completed_at, 0) desc, id desc
                    """,
                    (row["id"], *sorted(stale_statuses)),
                ).fetchall()
                task_dicts = [dict(task) for task in task_rows]
                matching_tasks = [
                    task
                    for task in task_dicts
                    if _pack_no_match_task_matches_failure(task, result, item, pack_path, candidate_title, series)
                ]
                preserved_tasks = [task for task in task_dicts if task not in matching_tasks]
                if task_dicts and not matching_tasks:
                    preserved_active_handoffs += len(preserved_tasks)
                    preserved_queue_rows += 1
                    continue
                if matching_tasks:
                    ph = ",".join("?" for _ in matching_tasks)
                    cur = con.execute(
                        f"""
                        update download_tasks
                        set status='candidate_failed',
                            lifecycle_phase='candidate_failed',
                            outcome='failed',
                            display_phase='retry_later',
                            failure_reason='pack_no_matching_missing_file',
                            retry_eligible=1,
                            completed_at=coalesce(completed_at, ?),
                            updated_at=?,
                            state='terminal'
                        where id in ({ph})
                        """,
                        (now, now, *(task["id"] for task in matching_tasks)),
                    )
                    retired_tasks += int(cur.rowcount or 0)
                release_queue = not preserved_tasks
                if preserved_tasks:
                    preserved_active_handoffs += len(preserved_tasks)
                    preserved_queue_rows += 1
                scope_key = inkdrop_state.bad_source_scope_key(
                    series=series,
                    series_id=row.get("series_id"),
                    issue_id=row.get("issue_id"),
                    issue_number=row.get("issue_number"),
                    queue_id=row.get("id"),
                    wanted_id=row.get("wanted_id"),
                )
                source_memory_tasks = matching_tasks
                if not source_memory_tasks:
                    source_memory_tasks = [
                        {
                            "id": "",
                            "source": "pack_import",
                            "provider_id": "local_pack",
                            "provider": "local_pack",
                            "protocol": "local",
                            "download_client": "pack_import",
                            "external_id": "",
                            "candidate_identity": "",
                            "title": candidate_title or (item or {}).get("title") or series,
                            "local_path": str(pack_path or ""),
                            "raw_json": "{}",
                        }
                    ]
                for task in source_memory_tasks:
                    task_raw = inkdrop_state.json_loads(task.get("raw_json") or "{}", {})
                    task_raw = task_raw if isinstance(task_raw, dict) else {}
                    payload = {
                        "source": task.get("provider_id") or task.get("source") or "pack_import",
                        "provider": task.get("provider") or task.get("provider_id") or task.get("source") or "local_pack",
                        "protocol": task.get("protocol") or ("local" if task.get("provider_id") == "local_pack" else ""),
                        "scope_key": scope_key,
                        "series": series,
                        "title": task.get("title") or candidate_title or (item or {}).get("title") or series,
                        "download_url_hash": task.get("candidate_identity") or "",
                        "source_path": task.get("external_id") or task.get("local_path") or str(pack_path or ""),
                        "reason": "pack_no_matching_missing_file",
                        "raw": {
                            "kind": "pack_no_matching_missing_file",
                            "queue_id": row.get("id"),
                            "wanted_id": row.get("wanted_id"),
                            "series_id": row.get("series_id"),
                            "issue_id": row.get("issue_id"),
                            "issue_number": row.get("issue_number"),
                            "scope_key": scope_key,
                            "review_id": (item or {}).get("review_id") or result.get("review_id"),
                            "pack_path": str(pack_path or ""),
                            "candidate_title": candidate_title or (item or {}).get("title") or series,
                            "download_task": {key: task.get(key) for key in (
                                "id",
                                "source",
                                "provider_id",
                                "provider",
                                "protocol",
                                "download_client",
                                "external_id",
                                "candidate_identity",
                                "title",
                                "local_path",
                            )},
                            "download_task_raw": task_raw,
                        },
                        "seen_at": now,
                    }
                    if inkdrop_state.record_bad_source_candidate_row(con, payload, increment=True):
                        bad_source_records += 1
                if not release_queue:
                    continue
                raw = inkdrop_state.json_loads(row.get("raw_json") or "{}", {})
                raw = raw if isinstance(raw, dict) else {}
                raw["state"] = "queued"
                raw["current_source"] = None
                raw["last_event"] = message
                raw["updated_at"] = now
                raw["updated_at_iso"] = inkdrop_state.utc_stamp(now)
                raw["last_attempt_source"] = "pack_import"
                raw["last_attempt_status"] = "retry_scheduled"
                raw["last_attempt_at"] = now
                raw["last_attempt_at_iso"] = inkdrop_state.utc_stamp(now)
                raw["pack_no_match_retry"] = {
                    "reason": "pack_no_matching_missing_file",
                    "review_id": (item or {}).get("review_id") or result.get("review_id"),
                    "pack_path": str(pack_path or ""),
                    "title": candidate_title or (item or {}).get("title") or series,
                    "at": now,
                    "at_iso": inkdrop_state.utc_stamp(now),
                }
                con.execute(
                    """
                    update queue_items
                    set state='queued',
                        current_source=null,
                        last_event=?,
                        updated_at=?,
                        retry_after=null,
                        retry_after_iso=null,
                        outcome='no_candidate',
                        display_phase='retry_later',
                        raw_json=?
                    where id=?
                    """,
                    (message, now, inkdrop_state.json_dumps(raw), row["id"]),
                )
                if row.get("wanted_id"):
                    con.execute(
                        "update wanted_items set status='wanted', updated_at=? where id=?",
                        (now, row["wanted_id"]),
                    )
                try:
                    inkdrop_state.refresh_queue_provider_status_columns(con, row["id"])
                except Exception:
                    pass
                released_rows.append(row)
            con.commit()
    except Exception as exc:
        errors.append({"download_task_retire_error": f"{type(exc).__name__}: {exc}"})

    recorded = 0
    for row in released_rows:
        try:
            update = inkdrop_state.record_queue_source_attempt(
                INKDROP_STATE_DB,
                row["id"],
                dict(attempt),
                started_at=now,
                completed_at=now,
            )
            if update.get("ok"):
                recorded += 1
            else:
                errors.append({"queue_id": row["id"], "result": update})
        except Exception as exc:
            errors.append({"queue_id": row["id"], "error": f"{type(exc).__name__}: {exc}"})

    return {
        "ok": not errors,
        "recorded": recorded,
        "matched_queue_rows": len(rows),
        "released_queue_rows": len(released_rows),
        "preserved_queue_rows": preserved_queue_rows,
        "preserved_active_handoffs": preserved_active_handoffs,
        "retired_download_tasks": retired_tasks,
        "bad_source_candidates_recorded": bad_source_records,
        "errors": errors[:10],
    }


def run(args):
    importer = load_importer()
    explicit_path = Path(args.path) if args.path else None
    item = load_manual_review_item(
        args.review_id,
        allow_manual_source_archive=bool(explicit_path and explicit_path.suffix.lower() in PACK_ARCHIVE_EXTS),
        source_path=explicit_path,
    )
    candidate_title = (item.get("candidate") or {}).get("title") or item.get("title") or item.get("query")
    if is_supplemental_pack_result(candidate_title):
        result = {
            "status": "blocked_supplemental_release",
            "reason": "supplemental_release_requires_review",
            "review_id": args.review_id,
            "series": item.get("series"),
            "candidate": candidate_title,
            "dry_run": args.dry_run,
        }
        log({**result, "event": "pack_import_blocked_supplemental_release"})
        if not args.dry_run:
            append_manual_review("pack_import_supplemental_release_blocked", result, db_path=INKDROP_STATE_DB)
            update_pack_state(args.review_id, "blocked_supplemental_release", result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    volume_id = item.get("volume_id") or resolve_volume_id(item.get("series"))
    locate_details = resolve_pack_path(item, explicit_path=explicit_path, return_details=True)
    pack_path = locate_details.get("path")
    if not pack_path or not pack_path.exists():
        result = {
            "status": "waiting_for_local_pack",
            "review_id": args.review_id,
            "series": item.get("series"),
            "candidate": (item.get("candidate") or {}).get("title"),
            "searched_roots": [str(root) for root in PACK_SOURCES],
            "pack_search": json_safe(locate_details),
        }
        print(json.dumps(result, indent=2))
        return
    if not pack_probe_path_identity_confirmed(
        {
            "review_id": args.review_id,
            "series": item.get("series"),
            "title": candidate_title,
            "pack_path": str(pack_path),
            "pack_exists": True,
        },
        item,
    ):
        result = {
            "status": "pack_path_identity_unconfirmed",
            "review_id": args.review_id,
            "series": item.get("series"),
            "candidate": candidate_title,
            "pack_path": str(pack_path),
            "reason": "local pack path does not confidently match the pending pack title",
            "mutates_database": False,
            "mutates_filesystem": False,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if not args.dry_run:
        active_status = {
            "status": "importing",
            "review_id": args.review_id,
            "series": item.get("series"),
            "title": candidate_title,
            "selected_path": str(pack_path),
            "source": "pack_import_worker",
        }
        write_pack_auto_import_status(active_status)
        update_pack_state(args.review_id, "importing", {"pack_path": str(pack_path), "title": candidate_title, "series": item.get("series")})
    target_filters = pack_match_series_filters(item)
    targets = importer.load_comic_targets(target_filters)
    if not targets:
        raise ValueError("no safe InkDrop target folder found for pack series")
    missing = target_missing_issue_maps(targets, volume_id, item)
    manga_target = any(is_manga_target(target) for target in targets)
    if not missing_issue_map_has_values(missing) and not manga_target:
        print(json.dumps({"status": "nothing_missing", "review_id": args.review_id, "pack_path": str(pack_path)}, indent=2))
        return

    extracted_dir = None
    temp_ctx = None
    files = comic_files_under(pack_path)
    archive_preview = []
    image_folder_units = []
    if not files and pack_path.is_file() and pack_path.suffix.lower() in PACK_ARCHIVE_EXTS:
        archive_preview = [str(name) for name in inspect_archive_pack(pack_path)]
        if not args.dry_run:
            temp_ctx = tempfile.TemporaryDirectory(prefix="kavita-pack-import-")
            extracted_dir = Path(temp_ctx.name) / "extract"
            extract_archive(pack_path, extracted_dir)
            files = comic_files_under(extracted_dir)
            if not files:
                files, image_folder_units = build_cbz_from_image_units(extracted_dir, item.get("series"), Path(temp_ctx.name))
        else:
            files = [Path(name) for name in archive_preview]

    matched, not_imported = classify_files(importer, files, targets, missing, item=item)
    pack_match = item.get("pack_match") or {}
    status = "inspected" if args.dry_run else ("no_importable_files" if not files else "imported")
    result = {
        "status": status,
        "review_id": args.review_id,
        "series": item.get("series"),
        "target_series_filters": target_filters,
        "target_count": len(targets),
        "pack_path": str(pack_path),
        "dry_run": args.dry_run,
        "file_count": len(files),
        "image_folder_unit_count": len(image_folder_units),
        "image_folder_units": image_folder_units[:50],
        "useful_missing_count": int(pack_match.get("useful_missing_count") or 0),
        "matched_missing_count": matched_wanted_row_count(matched),
        "not_imported_count": len(not_imported),
        "archive_preview": archive_preview[:50],
        "matched": [row for _, _, row in matched[:50]],
        "not_imported": not_imported[:50],
    }
    if not args.dry_run:
        if files:
            import_result = import_matched_files(
                importer,
                matched,
                False,
                args.max_files,
                args.review_id,
                extracted_root=extracted_dir,
                wait_for_library_scan=bool(getattr(args, "wait_for_library_scan", getattr(args, "wait_for_kavita_scan", False))),
            )
            result.update(import_result)
            result["wait_for_library_scan"] = bool(getattr(args, "wait_for_library_scan", getattr(args, "wait_for_kavita_scan", False)))
            result["wait_for_kavita_scan"] = result["wait_for_library_scan"]
            native_result = record_native_pack_import_results(result, item, pack_path, candidate_title)
            result["inkdrop_state_imports"] = native_result
            if not matched:
                result["inkdrop_state_no_match"] = record_native_pack_no_match(result, item, pack_path, candidate_title, missing)
            history_result = append_pack_bad_archive_history(result.get("bad_archives") or [])
            if history_result.get("total"):
                result["bad_archive_history"] = history_result
        else:
            append_manual_review(
                "pack_import_no_importable_files",
                {
                    "review_id": args.review_id,
                    "series": item.get("series"),
                    "pack_path": str(pack_path),
                    "archive_preview": archive_preview[:50],
                },
                db_path=INKDROP_STATE_DB,
            )
        if temp_ctx:
            quarantine = QUARANTINE_ROOT / args.review_id
            quarantine.mkdir(parents=True, exist_ok=True)
            for row in not_imported[:100]:
                src = Path(row["source"])
                if extracted_dir and src.exists() and extracted_dir in src.parents:
                    dest = importer.unique_dest_name(quarantine, src.name)
                    shutil.copy2(src, dest)
        native_state = result.get("inkdrop_state_imports") or {}
        update_pack_state(
            args.review_id,
            status,
            {
                "pack_path": str(pack_path),
                "imported_count": len(result.get("imported") or []),
                "inkdrop_state_ok": native_state.get("ok"),
                "inkdrop_state_reason": native_state.get("reason"),
                "inkdrop_state_error": native_state.get("error"),
            },
        )
        write_pack_auto_import_status({
            "status": "complete",
            "review_id": args.review_id,
            "series": item.get("series"),
            "title": candidate_title,
            "selected_path": str(pack_path),
            "source": "pack_import_worker",
            "result_status": status,
            "imported_count": len(result.get("imported") or []),
            "bad_archive_count": len(result.get("bad_archives") or []),
            "skipped_existing_count": len(result.get("skipped_existing") or []),
            "inkdrop_state_ok": native_state.get("ok"),
            "inkdrop_state_reason": native_state.get("reason"),
            "inkdrop_state_error": native_state.get("error"),
            "inkdrop_state_needs_reverify": bool(native_state and not native_state.get("ok")),
            "inkdrop_state_recorded": native_state.get("recorded"),
            "inkdrop_state_deferred": native_state.get("deferred"),
            "inkdrop_state_unmatched": native_state.get("unmatched"),
        })
    if temp_ctx:
        temp_ctx.cleanup()
    print(json.dumps(result, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description="Inspect and safely import approved InkDrop comic/manga packs")
    parser.add_argument("--review-id")
    parser.add_argument("--path", help="Optional explicit local pack path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-files", type=int, default=25, help="Live import cap for files already matched to monitored missing items")
    parser.add_argument(
        "--wait-for-library-scan",
        "--wait-for-kavita-scan",
        dest="wait_for_library_scan",
        action="store_true",
        help="Block until configured library frontends verify imported pack files; automation leaves this off and verifies asynchronously",
    )
    parser.add_argument("--probe-completed-packs", action="store_true", help="Read-only report of pending completed packs and wanted rows they would import")
    parser.add_argument("--reconcile-completed-packs", action="store_true", help="Mark completed pending packs with zero actionable wanted files as finished so stale banners clear")
    parser.add_argument("--probe-limit", type=int, default=20, help="Maximum pending pack records to include in the read-only probe")
    parser.add_argument("--probe-preview", type=int, default=50, help="Maximum matched/unmatched rows shown per probed pack")
    parser.add_argument("--probe-scan-seconds", type=float, help="Per-pack filesystem search budget for read-only completed-pack probes")
    parser.add_argument("--probe-scan-entries", type=int, help="Per-pack filesystem entry cap for read-only completed-pack probes")
    args = parser.parse_args()
    args.wait_for_kavita_scan = bool(args.wait_for_library_scan)
    args.max_files = max(1, min(int(args.max_files or 25), 75))
    if args.probe_completed_packs or args.reconcile_completed_packs:
        if args.probe_scan_seconds is not None:
            os.environ["INKDROP_PACK_PROBE_SCAN_SECONDS"] = str(max(0.1, min(float(args.probe_scan_seconds), 30.0)))
        if args.probe_scan_entries is not None:
            os.environ["INKDROP_PACK_PROBE_SCAN_ENTRIES"] = str(max(100, min(int(args.probe_scan_entries), 250000)))
        if args.reconcile_completed_packs:
            result = reconcile_completed_packs(
                review_id=args.review_id,
                explicit_path=args.path,
                limit=max(1, min(int(args.probe_limit or 20), 100)),
                max_preview=max(1, min(int(args.probe_preview or 50), 200)),
                dry_run=bool(args.dry_run),
            )
        else:
            result = probe_completed_packs(
                review_id=args.review_id,
                explicit_path=args.path,
                limit=max(1, min(int(args.probe_limit or 20), 100)),
                max_preview=max(1, min(int(args.probe_preview or 50), 200)),
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if not args.review_id:
        parser.error("--review-id is required unless --probe-completed-packs or --reconcile-completed-packs is used")
    run(args)


if __name__ == "__main__":
    main()
