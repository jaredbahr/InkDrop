#!/usr/bin/env python3
import argparse
import collections
import configparser
import errno
import hashlib
import importlib.util
import json
import math
import os
import posixpath
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests

import inkdrop_runtime_config
import inkdrop_download_client_routing


def script_path(name: str, remote_path=None, *, env_var=None, fallback=None) -> Path:
    local = Path(__file__).resolve().with_name(name)
    if local.exists():
        return local
    if env_var:
        configured = os.environ.get(env_var)
        if configured:
            return Path(configured)
    if remote_path:
        return Path(remote_path)
    if fallback is not None:
        return Path(fallback)
    return local


CONFIG_DIR = inkdrop_runtime_config.config_dir()
STATE_DIR = inkdrop_runtime_config.state_dir()
CACHE_DIR = inkdrop_runtime_config.cache_dir()
STAGING_DIR = inkdrop_runtime_config.staging_dir()
MANUAL_INBOX_DIR = inkdrop_runtime_config.manual_inbox_dir()
PENDING_IMPORTS_LOG = STATE_DIR / "pending-imports.jsonl"
RECONCILE_STATUS_PATH = STATE_DIR / "import-reconcile-status.json"
DUPLICATE_STATUS_CACHE_PATH = STATE_DIR / "manga-duplicate-status-cache.json"
QBIT_FILE_LIST_CACHE_PATH = CACHE_DIR / "qbit-file-list-cache.json"
ARCHIVE_VALIDATION_FAILURES_PATH = STATE_DIR / "archive-validation-failures.json"
RECONCILE_LOCK_PATH = inkdrop_runtime_config.lock_path("inkdrop-reconcile-imports.lock")
IMPORTER_MODULE_PATH = script_path("inkdrop_completed_import.py", env_var="INKDROP_IMPORTER_MODULE_SCRIPT")
IMPORTER_PATH = script_path("inkdrop_completed_import.py", env_var="INKDROP_COMPLETED_IMPORT_SCRIPT", fallback=IMPORTER_MODULE_PATH)
MISSING_ACQUIRE_PATH = script_path("inkdrop_missing_acquire.py", fallback=Path(__file__).resolve().with_name("inkdrop_missing_acquire.py"), env_var="INKDROP_MISSING_ACQUIRE_SCRIPT")
ACQUIRE_MODULE_PATH = script_path("inkdrop_acquire.py", env_var="INKDROP_ACQUIRE_SCRIPT")
QBIT_CONFIG = Path(os.environ.get("INKDROP_QBITTORRENT_CONFIG") or CONFIG_DIR / "qbit_manage" / "config.yml")
MYLAR_CONFIG = Path(os.environ.get("INKDROP_MYLAR_CONFIG") or CONFIG_DIR / "mylar" / "config.ini")
DB_PATH = STATE_DIR / "imported-files.sqlite3"
INKDROP_STATE_DB = STATE_DIR / "inkdrop-state.sqlite3"
INKDROP_STATE_MODULE_PATH = Path(__file__).with_name("inkdrop_state.py")

COMIC_CLIENT_CATEGORIES = {"comics", "manga", "mylar", "kapowarr"}
QBIT_BROAD_TAGS = {"inkdrop", "kavita-acquire"}
COMIC_LOCAL_ROOTS = [
    Path(os.environ.get("INKDROP_UNMATCHED_DOWNLOAD_ROOT") or STAGING_DIR / "downloads" / "comics"),
    Path(os.environ.get("INKDROP_DIRECT_DOWNLOAD_ROOT") or STAGING_DIR / "direct" / "comics"),
    Path(os.environ.get("INKDROP_MANUAL_COMICS_INBOX") or MANUAL_INBOX_DIR / "comics"),
    Path(os.environ.get("INKDROP_SUWAYOMI_STAGING_ROOT") or STAGING_DIR / "suwayomi"),
]
QBIT_DOWNLOAD_PATH_MAP = (
    ("/downloads/comics", Path(os.environ.get("INKDROP_DIRECT_DOWNLOAD_ROOT") or STAGING_DIR / "direct" / "comics")),
    ("/downloads", Path(os.environ.get("INKDROP_QBITTORRENT_DOWNLOAD_ROOT") or STAGING_DIR / "downloads")),
)
CONFIGURED_REMOTE_PATH_MAPPINGS_CACHE = None
CONFIGURED_REMOTE_PATH_MAPPINGS_CACHE_VERSION = None
QBIT_FILE_LIST_CACHE = None
QBIT_FILE_LIST_CACHE_DIRTY = False
ARCHIVE_SUFFIXES = {".cbz", ".cbr", ".pdf"}
INKDROP_IMPORT_READY_DOWNLOAD_CLIENTS = (
    "qbittorrent",
    "sabnzbd",
    "slskd",
    "inkdrop_direct",
    "inkdrop_page_pack",
    "inkdrop_external_tool",
    "inkdrop_local_pack",
)
INKDROP_STAGED_SOURCE_CLIENTS = (
    "slskd",
    "inkdrop_direct",
    "inkdrop_page_pack",
    "inkdrop_external_tool",
    "inkdrop_local_pack",
)
INKDROP_DIRECT_IMPORT_READY_STATUSES = (
    "staged_file_ready",
    "ready_import",
    "preview_importable",
    "ready_to_import",
)
STALE_AFTER_SECONDS = 24 * 60 * 60
QBIT_NO_PROGRESS_RETRY_SECONDS = 2 * 60 * 60
FILE_SCAN_TIMEOUT_SECONDS = 10
DUPLICATE_STATUS_CACHE_SECONDS = 6 * 60 * 60
IMPORT_READY_MAX_FILES = 12
IMPORT_READY_STAGE_OUTCOME_CONTRACT_VERSION = 1
IMPORT_READY_STAGE_OUTCOME_KEY = "import_ready_reconciliation_stages"
IMPORT_LIFECYCLE_OUTCOME_KEY = "latest_import_lifecycle"
IMPORT_LIFECYCLE_STAGE_CONTRACT_VERSION = 1
IMPORT_LIFECYCLE_STAGE_NAMES = (
    "client_reconciliation",
    "completed_download_detection",
    "artifact_discovery_validation",
    "import_preparation",
    "destination_selection",
    "file_placement",
    "metadata_write",
    "reader_scan_request",
    "completion_projection",
)
IMPORT_LIFECYCLE_STAGE_STATUSES = {"success", "no_work", "blocked", "retryable", "terminal"}
LAST_IMPORT_READY_STAGE_OUTCOMES = None
PENDING_IMPORT_READY_PRELIMINARY_STAGES = None
PENDING_IMPORT_READY_STARTED_AT = None


def env_int(name, default, minimum=None, maximum=None):
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def env_float(name, default, minimum=None, maximum=None):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


IMPORT_READY_IMPORT_TIMEOUT_SECONDS = env_int("INKDROP_IMPORT_READY_IMPORT_TIMEOUT_SECONDS", 240, minimum=30, maximum=1800)
IMPORT_READY_BATCH_TIMEOUT_SECONDS = env_int("INKDROP_IMPORT_READY_BATCH_TIMEOUT_SECONDS", 600, minimum=60, maximum=3600)
IMPORT_READY_QUEUE_ONLY = str(os.environ.get("INKDROP_IMPORT_READY_QUEUE_ONLY") or "").strip().lower() in {"1", "true", "yes", "on"}
INKDROP_IMPORT_READY_APPLY_PLANNED_PATH = env_bool("INKDROP_IMPORT_READY_APPLY_PLANNED_PATH", True)
PACK_MANIFEST_CACHE_SECONDS = env_int("INKDROP_PACK_MANIFEST_CACHE_SECONDS", 6 * 60 * 60, minimum=0, maximum=7 * 24 * 60 * 60)
QBIT_FILE_LIST_CACHE_SECONDS = env_int("INKDROP_QBIT_FILE_LIST_CACHE_SECONDS", 30 * 60, minimum=0, maximum=24 * 60 * 60)
SQLITE_LOCK_RETRY_ATTEMPTS = 4
SQLITE_LOCK_RETRY_INITIAL_DELAY_SECONDS = 0.25
INKDROP_STATE_READ_TIMEOUT_SECONDS = 2.0
INKDROP_STATE_READ_BUSY_TIMEOUT_MS = 2000
INKDROP_STATE_WRITE_TIMEOUT_SECONDS = 4.0
INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS = 4000
DOWNLOAD_CLIENT_RECONCILE_WRITE_TIMEOUT_SECONDS = 20.0
DOWNLOAD_CLIENT_RECONCILE_WRITE_BUSY_TIMEOUT_MS = 20000
DOWNLOAD_CLIENT_RECONCILE_ATTEMPTS = 6
INKDROP_RECONCILED_IMPORT_SYNC_LIMIT = 8
INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS = env_float(
    "INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS",
    20.0,
    minimum=1,
    maximum=300,
)
INKDROP_MANGA_COMPLETION_BACKFILL_LIMIT = env_int(
    "INKDROP_MANGA_COMPLETION_BACKFILL_LIMIT",
    50,
    minimum=0,
    maximum=500,
)
INKDROP_PACK_FANOUT_MAX_ROWS = env_int("INKDROP_PACK_FANOUT_MAX_ROWS", 1400, minimum=100, maximum=5000)
INKDROP_PACK_FANOUT_MAX_CREATED = env_int("INKDROP_PACK_FANOUT_MAX_CREATED", 120, minimum=10, maximum=500)
INKDROP_PACK_FANOUT_LOCK_RETRY_ATTEMPTS = env_int("INKDROP_PACK_FANOUT_LOCK_RETRY_ATTEMPTS", 16, minimum=1, maximum=60)
INKDROP_LOCAL_PACK_REPLAY_MAX_ROOTS = env_int("INKDROP_LOCAL_PACK_REPLAY_MAX_ROOTS", 60, minimum=0, maximum=300)
INKDROP_LOCAL_PACK_REPLAY_ARCHIVE_LIMIT = env_int("INKDROP_LOCAL_PACK_REPLAY_ARCHIVE_LIMIT", 1200, minimum=50, maximum=5000)
INKDROP_IMPORT_READY_MAX_PER_BROAD_PACK_PER_BATCH = env_int(
    "INKDROP_IMPORT_READY_MAX_PER_BROAD_PACK_PER_BATCH",
    2,
    minimum=1,
    maximum=20,
)
INKDROP_IMPORT_READY_REJECTION_UPDATE_LIMIT = env_int(
    "INKDROP_IMPORT_READY_REJECTION_UPDATE_LIMIT",
    6,
    minimum=0,
    maximum=50,
)
INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS = 0.75
INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS = 750
INKDROP_REPLAY_STATE_READ_RETRY_ATTEMPTS = 4
INKDROP_REPLAY_STATE_READ_RETRY_INITIAL_DELAY_SECONDS = 0.1
INKDROP_REPLAY_STATE_WRITE_TIMEOUT_SECONDS = 1.5
INKDROP_REPLAY_STATE_WRITE_BUSY_TIMEOUT_MS = 1500
INKDROP_IMPORT_READY_RECOVERY_WRITE_ATTEMPTS = env_int(
    "INKDROP_IMPORT_READY_RECOVERY_WRITE_ATTEMPTS",
    8,
    minimum=1,
    maximum=30,
)
INKDROP_IMPORT_READY_RECOVERY_INITIAL_DELAY_SECONDS = env_float(
    "INKDROP_IMPORT_READY_RECOVERY_INITIAL_DELAY_SECONDS",
    0.5,
    minimum=0,
    maximum=5,
)
# Content hashes are useful for duplicate suppression, but hashing large comic
# packs during every status reconciliation can make the UI look frozen.
MAX_HASH_SUPPRESSION_BYTES = 64 * 1024 * 1024
REVALIDATE_LOCAL_STATUSES = {
    "bad_archive",
    "failed_import",
    "waiting_for_library_scan",
    "waiting_for_kavita_scan",
    "library_scan_timeout",
    "completed_name_mismatch",
    "wrong_series_or_subseries",
}
INKDROP_IMPORT_READY_REJECTION_STATES = {
    "manual_review",
    "failed_download",
    "failed_import",
    "bad_archive",
    "false_positive",
    "stale_no_local_file",
    "wrong_series_or_subseries",
}
TERMINAL_IMPORT_ARTIFACT_SKIP_TOKENS = {
    "known_bad_artifact_content",
    "bad_archive",
    "bad_zip_member",
    "cbr_extract_failed",
    "skip_bad_comic_archive",
    "preview",
    "sample",
    "wrong_series",
    "wrong_unit",
    "unsafe_archive",
    "unsafe_path",
    "false_positive",
    "wrong_language_source",
    "source_target_identity_mismatch",
    "trusted_issue_mismatch",
    "too_few_image_pages",
    "too_little_image_payload",
    "zero_or_empty_header",
    "bad_zip_archive",
    "identity_mismatch",
}
TERMINAL_IMPORT_ARTIFACT_ACTION_PREFIXES = (
    "retry_another_source",
    "regrab_or_manual_review",
    "manual_identity_review",
    "manual_review",
)
SUPPRESSED_COMPLETED_EXISTING_PATH_REASONS = {
    "already_imported_matching_destination",
    "already_imported_matching_hash",
    "already_imported_or_verified",
    "already_verified_duplicate",
    "already_verified_manga_file_present",
    "already_verified_series_number",
    "canonical_file_already_present",
    "matching_filename_already_present",
    "source_already_imported",
}
DOWNLOAD_STAGING_ROOTS = tuple(COMIC_LOCAL_ROOTS) + (
    Path(os.environ.get("INKDROP_QBITTORRENT_DOWNLOAD_ROOT") or STAGING_DIR / "downloads"),
    Path(os.environ.get("INKDROP_DOWNLOAD_STAGING_ROOT") or STAGING_DIR),
    Path("/downloads"),
)
LOCAL_PATH_KEYS = (
    "source",
    "source_path",
    "local_path",
    "matched_local_path",
    "dest",
    "path",
)
LIFECYCLE_STATES = {
    "sent",
    "queued",
    "downloading",
    "stalled_downloading",
    "completed_in_client",
    "ready_to_import",
    "importing",
    "imported",
    "waiting_for_library_scan",
    "waiting_for_kavita_scan",
    "verified",
    "failed_download",
    "failed_import",
    "bad_archive",
    "false_positive",
    "stale_no_local_file",
    "wrong_series_or_subseries",
    "completed_name_mismatch",
    "manual_review",
    "suppressed_completed",
}

LIBRARY_SCAN_WAIT_STATE = "waiting_for_library_scan"
LEGACY_KAVITA_SCAN_WAIT_STATE = "waiting_for_kavita_scan"
LIBRARY_SCAN_WAIT_STATES = {LIBRARY_SCAN_WAIT_STATE, LEGACY_KAVITA_SCAN_WAIT_STATE}
LIBRARY_SCAN_TIMEOUT_STATES = {"library_scan_timeout", "kavita_scan_timeout"}
LIBRARY_VISIBLE_STATUSES = {"library_visible", "kavita_verified", "verified"}
FOLDER_VERIFIED_STATUSES = {"folder_verified"}
IMPORT_VERIFIED_STATUSES = LIBRARY_VISIBLE_STATUSES | FOLDER_VERIFIED_STATUSES


def is_library_scan_wait_state(value):
    return str(value or "").strip().lower() in LIBRARY_SCAN_WAIT_STATES


def is_import_visible_status(value):
    return str(value or "").strip().lower() in IMPORT_VERIFIED_STATUSES


def canonical_library_scan_reason(reason=None):
    text = str(reason or "").strip().lower()
    if text in {"importer_copied_waiting_for_kavita_scan", "importer_copied_waiting_for_library_scan"}:
        return "importer_copied_waiting_for_library_scan"
    if text in {
        "imported_after_timeout_waiting_for_kavita_scan",
        "imported_after_timeout_waiting_for_library_scan",
    }:
        return "imported_after_timeout_waiting_for_library_scan"
    if text == LEGACY_KAVITA_SCAN_WAIT_STATE:
        return LIBRARY_SCAN_WAIT_STATE
    if text == "kavita_verified":
        return "library_visible"
    return str(reason or LIBRARY_SCAN_WAIT_STATE).strip() or LIBRARY_SCAN_WAIT_STATE


def import_result_status_for_lifecycle(lifecycle_state, reason=None):
    state = str(lifecycle_state or "").strip().lower()
    reason_key = str(reason or "").strip().lower()
    if state == "verified":
        if reason_key in FOLDER_VERIFIED_STATUSES:
            return "folder_verified"
        return "library_visible"
    if state in LIBRARY_SCAN_WAIT_STATES:
        return LIBRARY_SCAN_WAIT_STATE
    return state


def load_importer():
    spec = importlib.util.spec_from_file_location("inkdrop_completed_import", IMPORTER_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


imp = load_importer()


def load_inkdrop_state_module():
    if not INKDROP_STATE_MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("inkdrop_state", INKDROP_STATE_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inkdrop_state = load_inkdrop_state_module()


def provider_config(provider_id):
    if inkdrop_state is None:
        return None
    try:
        return inkdrop_state.provider_config(INKDROP_STATE_DB, provider_id)
    except Exception:
        fallback_host = ""
        fallback_api_key = ""


def load_missing_acquire():
    if not MISSING_ACQUIRE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("inkdrop_missing_acquire", MISSING_ACQUIRE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_acquire_module():
    if not ACQUIRE_MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("inkdrop_acquire", ACQUIRE_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def now():
    return time.time()


def bounded_import_ready_max(value):
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = 1
    return max(1, min(requested, IMPORT_READY_MAX_FILES))


def sqlite_lock_error(exc):
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def with_sqlite_lock_retry(fn, attempts=SQLITE_LOCK_RETRY_ATTEMPTS, initial_delay=SQLITE_LOCK_RETRY_INITIAL_DELAY_SECONDS):
    delay = float(initial_delay or 0)
    last_exc = None
    for attempt in range(max(1, int(attempts or 1))):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not sqlite_lock_error(exc):
                raise
            last_exc = exc
            if attempt >= max(1, int(attempts or 1)) - 1:
                break
            if delay > 0:
                time.sleep(delay)
                delay = min(delay * 2, 3.0)
    raise last_exc


def with_sqlite_read_lock_retry(fn):
    """Bounded retry for query-only InkDrop state reads."""
    return with_sqlite_lock_retry(
        fn,
        attempts=INKDROP_REPLAY_STATE_READ_RETRY_ATTEMPTS,
        initial_delay=INKDROP_REPLAY_STATE_READ_RETRY_INITIAL_DELAY_SECONDS,
    )


def failed_download_record_hash(record):
    record = record or {}
    identity = "\x1f".join(
        str(record.get(key) or "").strip()
        for key in ("inkdrop_queue_id", "inkdrop_download_task_id", "client_id", "pending_key", "title", "query")
    )
    return hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()[:16]


def finite_timestamp(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def failed_download_sample_floor(started_at, lock_samples):
    values = [started_at]
    values.extend(sample.get("observed_at") for sample in lock_samples or [])
    finite_values = [float(value) for value in values if finite_timestamp(value)]
    return max(finite_values) if finite_values else None


def failed_download_lock_sample(record, stage, floor=None):
    observed_raw = now()
    sample = {
        "stage": str(stage),
        "record_hash": failed_download_record_hash(record),
    }
    if not finite_timestamp(observed_raw):
        sample.update({"observed_at": None, "timestamp_invalid": True})
        return sample
    observed_at = float(observed_raw)
    if finite_timestamp(floor) and observed_at < float(floor):
        sample.update(
            {
                "observed_at": float(floor),
                "observed_at_raw": observed_at,
                "clock_skew_detected": True,
            }
        )
        return sample
    sample["observed_at"] = observed_at
    return sample


def complete_failed_download_sync_timestamps(started_at, lock_samples):
    completed_raw = now()
    floors = [value for value in [started_at] if finite_timestamp(value)]
    floors.extend(
        sample.get("observed_at")
        for sample in lock_samples or []
        if finite_timestamp(sample.get("observed_at"))
    )
    floor = max(floors) if floors else None
    metadata = {}
    if any(sample.get("clock_skew_detected") for sample in lock_samples or []):
        metadata["sample_clock_skew_detected"] = True
    if any(sample.get("timestamp_invalid") for sample in lock_samples or []):
        metadata["sample_timestamp_invalid"] = True
    if not finite_timestamp(completed_raw):
        metadata["completed_timestamp_invalid"] = True
        return floor, metadata
    completed_at = float(completed_raw)
    if finite_timestamp(floor) and completed_at < float(floor):
        metadata.update({"clock_skew_detected": True, "completed_at_raw": completed_at})
        completed_at = float(floor)
    return completed_at, metadata


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("pragma busy_timeout = 30000")
    return conn


def norm(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def norm_contains_phrase(text_key, phrase_key):
    text_words = str(text_key or "").split()
    phrase_words = str(phrase_key or "").split()
    if not text_words or not phrase_words:
        return False
    length = len(phrase_words)
    if length > len(text_words):
        return False
    for idx in range(0, len(text_words) - length + 1):
        if text_words[idx : idx + length] == phrase_words:
            return True
    return False


def normalize_issue_number(value):
    if hasattr(imp, "normalize_manga_number"):
        return imp.normalize_manga_number(value)
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
        return ("%s" % number).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return re.sub(r"^0+", "", text) or text


def extract_issue_number_from_path(path):
    if hasattr(imp, "extract_issue_number"):
        return imp.extract_issue_number(path)
    stem = Path(str(path or "")).stem
    matches = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", stem)
    return matches[-1] if matches else ""


def filename_issue_number_candidates(path):
    stem = Path(str(path or "")).stem
    if not stem:
        return set()
    # Comic pack filenames often include total-count markers like "02 (of 06)".
    # Those totals are not issue identities and must not satisfy wanted issue 6.
    cleaned = re.sub(r"\(\s*of\s+\d{1,5}(?:\.\d+)?\s*\)", " ", stem, flags=re.I)
    cleaned = re.sub(r"\bof\s+\d{1,5}(?:\.\d+)?\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\(\s*(?:v|ver|version)\s*\d+\s*\)", " ", cleaned, flags=re.I)
    candidates = set()
    for token in re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", cleaned):
        normalized = normalize_issue_number(token)
        if not normalized:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", normalized):
            continue
        candidates.add(normalized)
    return candidates


def number_or_zero(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def import_ready_priority_text(*values):
    return " ".join(str(value or "") for value in values if value is not None).lower()


def import_ready_is_weekly_pack(*values):
    text = import_ready_priority_text(*values)
    return bool(
        "weekly comics pack" in text
        or "weekly pack" in text
        or "dc week" in text
        or "image week" in text
        or "marvel week" in text
        or re.search(r"\b20\d{2}[-. ]\d{2}[-. ]\d{2}\b.*\bweek", text)
    )


def import_ready_is_broad_pack(*values):
    text = import_ready_priority_text(*values)
    if not text or import_ready_is_weekly_pack(text):
        return False
    return bool(
        "complete" in text
        or "collection" in text
        or "omnibus" in text
        or re.search(r"\bpack\b", text)
        or re.search(r"\bv\d{1,3}\s*[-+]\s*v?\d{1,3}\b", text)
        or re.search(r"\b\d{1,4}\s*[-+]\s*\d{1,4}\+?\b", text)
    )


def import_ready_batch_priority(*values):
    if import_ready_is_weekly_pack(*values):
        return 0
    if import_ready_is_broad_pack(*values):
        return 2
    return 1


def import_ready_broad_pack_key(path, *values):
    if import_ready_is_weekly_pack(path, *values):
        return ""
    pieces = []
    try:
        source = Path(str(path or ""))
        pieces.extend([source.parent.name, source.name])
    except (TypeError, ValueError):
        pass
    pieces.extend(str(value or "") for value in values if value is not None)
    if not import_ready_is_broad_pack(*pieces):
        return ""
    return norm(" ".join(piece for piece in pieces if piece))[:160]


def pending_key(record):
    return norm(record.get("title") or record.get("query"))


def lifecycle_sample(records, limit=10):
    out = []
    for record in records[:limit]:
        out.append(
            {
                "title": record.get("title"),
                "query": record.get("query"),
                "state": record.get("state"),
                "reason": record.get("reason"),
                "protocol": record.get("protocol"),
                "client": record.get("client"),
                "local_path": record.get("local_path"),
                "matched_series": record.get("matched_series"),
            }
        )
    return out


def unmatched_group_name(record):
    raw = record.get("title") or record.get("query") or record.get("local_path") or ""
    name = Path(str(raw)).name
    lower_name = name.lower()
    for suffix in (".cbz.zip", ".cbz", ".cbr", ".pdf", ".zip", ".rar"):
        if lower_name.endswith(suffix):
            name = name[: -len(suffix)]
            lower_name = name.lower()
            break
    name = re.sub(r"\[[^\]]+\]", " ", name)
    name = re.sub(r"\((?:19|20)\d{2}[^)]*\)", " ", name)
    name = re.sub(r"\((?:digital|empire|zone-empire|danke-empire|minutemen|son-of-ultron)[^)]*\)", " ", name, flags=re.I)
    name = re.sub(r"\b(?:digital|empire|zone[- ]empire|danke[- ]empire|minutemen|son[- ]of[- ]ultron|dr\s*&\s*quinch[- ]empire|nothing[- ]empire)\b.*$", " ", name, flags=re.I)
    name = re.sub(r"[_\.]+", " ", name)
    name = re.sub(r"\s*[-–]\s*", " - ", name)
    name = re.sub(r"\b(?:v|vol(?:ume)?)\.?\s*\d{1,4}(?:\s*[-–]\s*\d{1,4})?\b.*$", " ", name, flags=re.I)
    name = re.sub(r"\b\d{1,4}(?:\s+of\s+\d{1,4})?\b.*$", " ", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" -._")
    if not name:
        return "Unknown local files"
    return name[:80]


def build_unmatched_download_groups(local_unlinked, limit=12):
    grouped = {}
    for record in local_unlinked:
        if record.get("state") != "manual_review" or record.get("reason") != "unmatched_local_file":
            continue
        group_name = unmatched_group_name(record)
        group = grouped.setdefault(
            group_name,
            {
                "name": group_name,
                "count": 0,
                "reason_counts": collections.Counter(),
                "sample_titles": [],
                "sample_paths": [],
            },
        )
        group["count"] += 1
        group["reason_counts"][record.get("reason") or "unknown"] += 1
        title = record.get("title") or record.get("query")
        if title and title not in group["sample_titles"] and len(group["sample_titles"]) < 4:
            group["sample_titles"].append(title)
        path = record.get("local_path")
        if path and path not in group["sample_paths"] and len(group["sample_paths"]) < 2:
            group["sample_paths"].append(path)
    groups = []
    for group in grouped.values():
        groups.append(
            {
                "name": group["name"],
                "count": group["count"],
                "reason_counts": dict(sorted(group["reason_counts"].items())),
                "sample_titles": group["sample_titles"],
                "sample_paths": group["sample_paths"],
            }
        )
    groups.sort(key=lambda item: (-item["count"], item["name"].lower()))
    return groups[:limit]


def ensure_reconciliation_table():
    if not DB_PATH.exists():
        return
    conn = connect_db()
    try:
        conn.execute(
            """
            create table if not exists download_reconciliation (
              pending_key text primary key,
              title text,
              query text,
              protocol text,
              client text,
              client_id text,
              client_hash text,
              nzo_id text,
              download_url_hash text,
              trusted_series_id text,
              trusted_issue text,
              inkdrop_queue_id text,
              inkdrop_download_task_id text,
              lifecycle_state text not null,
              reason text,
              matched_local_path text,
              matched_local_size integer,
              matched_local_mtime real,
              matched_series text,
              matched_kapowarr_volume_id integer,
              unit_model text,
              truth_model text,
              first_sent_at real,
              last_seen_in_client_at real,
              completed_seen_at real,
              imported_at real,
              verified_at real,
              updated_at real not null
            )
            """
        )
        columns = {row[1] for row in conn.execute("pragma table_info(download_reconciliation)").fetchall()}
        if "matched_local_size" not in columns:
            conn.execute("alter table download_reconciliation add column matched_local_size integer")
        if "matched_local_mtime" not in columns:
            conn.execute("alter table download_reconciliation add column matched_local_mtime real")
        if "download_url_hash" not in columns:
            conn.execute("alter table download_reconciliation add column download_url_hash text")
        if "trusted_series_id" not in columns:
            conn.execute("alter table download_reconciliation add column trusted_series_id text")
        if "trusted_issue" not in columns:
            conn.execute("alter table download_reconciliation add column trusted_issue text")
        if "inkdrop_queue_id" not in columns:
            conn.execute("alter table download_reconciliation add column inkdrop_queue_id text")
        if "inkdrop_download_task_id" not in columns:
            conn.execute("alter table download_reconciliation add column inkdrop_download_task_id text")
        conn.execute("create index if not exists idx_download_reconciliation_state on download_reconciliation (lifecycle_state)")
        conn.execute("create index if not exists idx_download_reconciliation_client on download_reconciliation (client, client_id)")
        conn.execute("create index if not exists idx_download_reconciliation_url_hash on download_reconciliation (download_url_hash)")
        conn.execute("create index if not exists idx_download_reconciliation_inkdrop_queue on download_reconciliation (inkdrop_queue_id)")
        conn.execute("create index if not exists idx_download_reconciliation_series on download_reconciliation (matched_series)")
        conn.execute("create index if not exists idx_download_reconciliation_local_path on download_reconciliation (matched_local_path)")
        conn.execute("create index if not exists idx_download_reconciliation_updated on download_reconciliation (updated_at)")
        conn.commit()
    finally:
        conn.close()


def ensure_completed_pack_manifest_table(conn=None):
    if not DB_PATH.exists() and conn is None:
        return False
    own_conn = conn is None
    con = conn or connect_db()
    try:
        con.execute(
            """
            create table if not exists completed_pack_manifests (
              root_path text primary key,
              root_kind text not null,
              root_size integer,
              root_mtime real,
              scanned_at real not null,
              archive_count integer not null default 0,
              cached_limit integer not null default 0,
              truncated integer not null default 0,
              archives_json text not null
            )
            """
        )
        con.execute("create index if not exists idx_completed_pack_manifests_scanned on completed_pack_manifests (scanned_at)")
        if own_conn:
            con.commit()
        return True
    finally:
        if own_conn:
            con.close()


def completed_pack_root_identity(root):
    try:
        stat = root.stat()
    except OSError:
        return None
    return {
        "root_kind": "file" if root.is_file() else "dir" if root.is_dir() else "other",
        "root_size": int(getattr(stat, "st_size", 0) or 0),
        "root_mtime": round(float(getattr(stat, "st_mtime", 0.0) or 0.0), 3),
    }


def cached_completed_pack_archives(root, limit):
    if PACK_MANIFEST_CACHE_SECONDS <= 0 or not DB_PATH.exists():
        return None
    identity = completed_pack_root_identity(root)
    if not identity or identity.get("root_kind") != "dir":
        return None
    conn = connect_db()
    try:
        ensure_completed_pack_manifest_table(conn)
        row = conn.execute(
            """
            select root_kind, root_size, root_mtime, scanned_at, cached_limit, truncated, archives_json
            from completed_pack_manifests
            where root_path=?
            """,
            (str(root),),
        ).fetchone()
        if not row:
            return None
        cached_kind, cached_size, cached_mtime, scanned_at, cached_limit, truncated, archives_json = row
        if str(cached_kind or "") != identity["root_kind"]:
            return None
        if int(cached_size or 0) != int(identity["root_size"] or 0):
            return None
        try:
            if abs(float(cached_mtime or 0) - float(identity["root_mtime"] or 0)) > 0.001:
                return None
        except (TypeError, ValueError):
            return None
        if now() - float(scanned_at or 0) > PACK_MANIFEST_CACHE_SECONDS:
            return None
        if int(truncated or 0) and int(cached_limit or 0) < int(limit or 0):
            return None
        try:
            paths = json.loads(archives_json or "[]")
        except (TypeError, ValueError):
            return None
        if not isinstance(paths, list):
            return None
        return [str(path) for path in paths[: int(limit or len(paths) or 0)] if str(path or "").strip()]
    finally:
        conn.close()


def store_completed_pack_archives(root, paths, limit, truncated=False):
    if PACK_MANIFEST_CACHE_SECONDS <= 0 or not DB_PATH.exists():
        return False
    identity = completed_pack_root_identity(root)
    if not identity or identity.get("root_kind") != "dir":
        return False
    clean_paths = [str(path) for path in (paths or []) if str(path or "").strip()]
    conn = connect_db()
    try:
        ensure_completed_pack_manifest_table(conn)
        conn.execute(
            """
            insert into completed_pack_manifests(
                root_path, root_kind, root_size, root_mtime, scanned_at,
                archive_count, cached_limit, truncated, archives_json
            ) values(?,?,?,?,?,?,?,?,?)
            on conflict(root_path) do update set
                root_kind=excluded.root_kind,
                root_size=excluded.root_size,
                root_mtime=excluded.root_mtime,
                scanned_at=excluded.scanned_at,
                archive_count=excluded.archive_count,
                cached_limit=excluded.cached_limit,
                truncated=excluded.truncated,
                archives_json=excluded.archives_json
            """,
            (
                str(root),
                identity["root_kind"],
                identity["root_size"],
                identity["root_mtime"],
                now(),
                len(clean_paths),
                int(limit or len(clean_paths) or 0),
                1 if truncated else 0,
                json.dumps(clean_paths),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def reconciliation_key(record):
    key = record.get("pending_key")
    if key:
        return key
    local_path = record.get("local_path")
    if local_path:
        return "local:" + norm(local_path)
    return norm(record.get("title") or record.get("query") or json.dumps(record, sort_keys=True))


def local_file_identity(path):
    if not path:
        return None, None, None
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return str(path), None, None
    return str(path), int(stat.st_size), round(float(stat.st_mtime), 3)


def path_under_root(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def download_staging_path(path):
    path = Path(str(path or ""))
    return any(path_under_root(path, root) for root in DOWNLOAD_STAGING_ROOTS)


def suppressed_completed_existing_path_state(state, reason):
    state = str(state or "").strip().lower()
    reason = str(reason or "").strip().lower()
    return state == "suppressed_completed" and reason in SUPPRESSED_COMPLETED_EXISTING_PATH_REASONS


def reconciliation_record_local_path(record):
    record = record if isinstance(record, dict) else {}
    if suppressed_completed_existing_path_state(record.get("state"), record.get("reason")):
        matched_path = str(record.get("matched_local_path") or "").strip()
        if matched_path:
            return matched_path
    return record.get("local_path") or record.get("matched_local_path")


def bad_archive_memory_key(record, path):
    path_text, size, mtime = local_file_identity(path)
    if not path_text or size is None or mtime is None:
        return None
    return (reconciliation_key(record), path_text, size, mtime)


def load_bad_archive_validation_memory():
    memory = load_archive_validation_failure_memory()
    if not DB_PATH.exists():
        return memory
    ensure_reconciliation_table()
    conn = connect_db()
    try:
        rows = conn.execute(
            """
            select pending_key, matched_local_path, matched_local_size, matched_local_mtime, reason
            from download_reconciliation
            where lifecycle_state='bad_archive'
              and matched_local_path is not null
              and matched_local_size is not null
              and matched_local_mtime is not null
            """
        ).fetchall()
    finally:
        conn.close()
    for key, path, size, mtime, reason in rows:
        try:
            memory[(key, path, int(size), round(float(mtime), 3))] = reason or "archive_validation_failed"
        except (TypeError, ValueError):
            continue
    return memory


def load_archive_validation_failure_records():
    try:
        data = json.loads(ARCHIVE_VALIDATION_FAILURES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = data.get("failures") if isinstance(data, dict) else data
    return rows if isinstance(rows, list) else []


def load_archive_validation_failure_memory():
    memory = {}
    for row in load_archive_validation_failure_records():
        if not isinstance(row, dict):
            continue
        try:
            key = (
                str(row.get("pending_key") or ""),
                str(row.get("path") or ""),
                int(row.get("size")),
                round(float(row.get("mtime")), 3),
            )
        except (TypeError, ValueError):
            continue
        if key[0] and key[1]:
            memory[key] = row.get("reason") or "archive_validation_failed"
    return memory


def remember_archive_validation_failure(pending_key, path, reason):
    path_text, size, mtime = local_file_identity(path)
    if not pending_key or not path_text or size is None or mtime is None:
        return
    rows = [
        row for row in load_archive_validation_failure_records()
        if isinstance(row, dict)
    ]
    row = {
        "pending_key": str(pending_key),
        "path": path_text,
        "size": int(size),
        "mtime": round(float(mtime), 3),
        "reason": reason or "archive_validation_failed",
        "updated_at": now(),
    }
    kept = []
    for old in rows:
        try:
            same_old = (
                old.get("pending_key") == row["pending_key"]
                and old.get("path") == row["path"]
                and int(old.get("size")) == row["size"]
                and round(float(old.get("mtime")), 3) == row["mtime"]
            )
        except (TypeError, ValueError):
            same_old = False
        if not same_old:
            kept.append(old)
    rows = kept
    rows.append(row)
    rows = rows[-500:]
    try:
        ARCHIVE_VALIDATION_FAILURES_PATH.parent.mkdir(parents=True, exist_ok=True)
        ARCHIVE_VALIDATION_FAILURES_PATH.write_text(
            json.dumps({"updated_at": now(), "failures": rows}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return


def apply_bad_archive_memory(record, detail, path, memory):
    if detail.get("state") != "ready_to_import":
        return detail
    key = bad_archive_memory_key(record, path)
    if not key or key not in memory:
        return detail
    updated = dict(detail)
    updated["state"] = "bad_archive"
    updated["reason"] = memory.get(key) or "archive_validation_failed"
    return updated


def state_timestamp_fields(record, updated_at):
    state = record.get("state")
    values = {
        "last_seen_in_client_at": None,
        "completed_seen_at": None,
        "imported_at": None,
        "verified_at": None,
    }
    if record.get("client") and state in {"queued", "downloading", "stalled_downloading", "completed_in_client", "failed_download", "bad_archive"}:
        values["last_seen_in_client_at"] = updated_at
    if state in {"completed_in_client", "ready_to_import"}:
        values["completed_seen_at"] = updated_at
    if state in {"imported", *LIBRARY_SCAN_WAIT_STATES}:
        values["imported_at"] = updated_at
    if state == "verified":
        values["verified_at"] = updated_at
    return values


def bounded_archive_files(roots, timeout_seconds=FILE_SCAN_TIMEOUT_SECONDS):
    roots = [Path(root) for root in roots if Path(root).exists()]
    if not roots:
        return []
    find_bin = Path("/usr/bin/find")
    if find_bin.exists():
        files = []
        per_root_timeout = max(3, int(timeout_seconds or FILE_SCAN_TIMEOUT_SECONDS))
        for root in roots:
            cmd = [
                str(find_bin),
                str(root),
                "-type",
                "f",
                "(",
                "-iname",
                "*.cbz",
                "-o",
                "-iname",
                "*.cbr",
                "-o",
                "-iname",
                "*.pdf",
                "-o",
                "-iname",
                "*.cbz.zip",
                ")",
            ]
            output = ""
            try:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=per_root_timeout,
                )
                if proc.returncode not in {0, 1}:
                    continue
                output = proc.stdout or ""
            except subprocess.TimeoutExpired as exc:
                output = exc.output or ""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
            except Exception:
                continue
            for line in output.splitlines():
                path = Path(line.strip())
                if not path:
                    continue
                if imp.is_internal_import_path(path, root):
                    continue
                files.append(path)
        return files
    started = now()
    files = []
    for root in roots:
        stack = [root]
        while stack:
            if now() - started > timeout_seconds:
                return files
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        path = Path(entry.path)
                        if imp.is_internal_import_path(path, root):
                            continue
                        lower = path.name.lower()
                        if path.suffix.lower() in ARCHIVE_SUFFIXES or lower.endswith(".cbz.zip"):
                            files.append(path)
            except OSError:
                continue
    return files


def persist_reconciliation(records, local_unlinked):
    if not DB_PATH.exists():
        return
    ensure_reconciliation_table()
    updated_at = now()
    upsert_reconciliation_records(list(records) + list(local_unlinked), updated_at=updated_at)
    conn = connect_db()
    try:
        conn.execute("delete from download_reconciliation where updated_at != ?", (updated_at,))
        conn.commit()
    finally:
        conn.close()


def upsert_reconciliation_records(records, updated_at=None):
    if not DB_PATH.exists():
        return 0
    ensure_reconciliation_table()
    updated_at = float(updated_at or now())
    rows = []
    for record in list(records or []):
        state = record.get("state") or "manual_review"
        if state not in LIFECYCLE_STATES:
            state = "manual_review"
        stamps = state_timestamp_fields(record, updated_at)
        local_path, local_size, local_mtime = local_file_identity(reconciliation_record_local_path(record))
        rows.append(
            {
                "pending_key": reconciliation_key(record),
                "title": record.get("title"),
                "query": record.get("query"),
                "protocol": record.get("protocol"),
                "client": record.get("client"),
                "client_id": record.get("client_id"),
                "client_hash": record.get("client_hash") or (record.get("client_id") if record.get("client") == "qbit" else None),
                "nzo_id": record.get("nzo_id") or (record.get("client_id") if record.get("client") == "sab" else None),
                "download_url_hash": record.get("download_url_hash") or record.get("downloadUrlHash") or record.get("url_hash"),
                "trusted_series_id": record.get("trusted_series_id"),
                "trusted_issue": record.get("trusted_issue"),
                "inkdrop_queue_id": record.get("inkdrop_queue_id"),
                "inkdrop_download_task_id": record.get("inkdrop_download_task_id"),
                "lifecycle_state": state,
                "reason": record.get("reason"),
                "matched_local_path": local_path,
                "matched_local_size": local_size,
                "matched_local_mtime": local_mtime,
                "matched_series": record.get("matched_series"),
                "matched_kapowarr_volume_id": record.get("matched_kapowarr_volume_id"),
                "unit_model": record.get("unit_model"),
                "truth_model": record.get("truth_model"),
                "first_sent_at": record.get("first_sent_at"),
                "last_seen_in_client_at": stamps["last_seen_in_client_at"],
                "completed_seen_at": stamps["completed_seen_at"],
                "imported_at": stamps["imported_at"],
                "verified_at": stamps["verified_at"],
                "updated_at": updated_at,
            }
        )
    if not rows:
        return 0
    conn = connect_db()
    try:
        conn.executemany(
            """
            insert into download_reconciliation (
              pending_key, title, query, protocol, client, client_id, client_hash, nzo_id, download_url_hash,
              trusted_series_id, trusted_issue, inkdrop_queue_id, inkdrop_download_task_id,
              lifecycle_state, reason, matched_local_path, matched_local_size, matched_local_mtime, matched_series,
              matched_kapowarr_volume_id, unit_model, truth_model, first_sent_at,
              last_seen_in_client_at, completed_seen_at, imported_at, verified_at, updated_at
            ) values (
              :pending_key, :title, :query, :protocol, :client, :client_id, :client_hash, :nzo_id, :download_url_hash,
              :trusted_series_id, :trusted_issue, :inkdrop_queue_id, :inkdrop_download_task_id,
              :lifecycle_state, :reason, :matched_local_path, :matched_local_size, :matched_local_mtime, :matched_series,
              :matched_kapowarr_volume_id, :unit_model, :truth_model, :first_sent_at,
              :last_seen_in_client_at, :completed_seen_at, :imported_at, :verified_at, :updated_at
            )
            on conflict(pending_key) do update set
              title=excluded.title,
              query=excluded.query,
              protocol=excluded.protocol,
              client=excluded.client,
              client_id=excluded.client_id,
              client_hash=excluded.client_hash,
              nzo_id=excluded.nzo_id,
              download_url_hash=coalesce(excluded.download_url_hash, download_reconciliation.download_url_hash),
              trusted_series_id=coalesce(excluded.trusted_series_id, download_reconciliation.trusted_series_id),
              trusted_issue=coalesce(excluded.trusted_issue, download_reconciliation.trusted_issue),
              inkdrop_queue_id=coalesce(excluded.inkdrop_queue_id, download_reconciliation.inkdrop_queue_id),
              inkdrop_download_task_id=coalesce(excluded.inkdrop_download_task_id, download_reconciliation.inkdrop_download_task_id),
              lifecycle_state=excluded.lifecycle_state,
              reason=excluded.reason,
              matched_local_path=excluded.matched_local_path,
              matched_local_size=excluded.matched_local_size,
              matched_local_mtime=excluded.matched_local_mtime,
              matched_series=excluded.matched_series,
              matched_kapowarr_volume_id=excluded.matched_kapowarr_volume_id,
              unit_model=excluded.unit_model,
              truth_model=excluded.truth_model,
              first_sent_at=coalesce(download_reconciliation.first_sent_at, excluded.first_sent_at),
              last_seen_in_client_at=coalesce(excluded.last_seen_in_client_at, download_reconciliation.last_seen_in_client_at),
              completed_seen_at=coalesce(excluded.completed_seen_at, download_reconciliation.completed_seen_at),
              imported_at=coalesce(excluded.imported_at, download_reconciliation.imported_at),
              verified_at=coalesce(excluded.verified_at, download_reconciliation.verified_at),
              updated_at=excluded.updated_at
            """,
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def archive_files():
    return bounded_archive_files(COMIC_LOCAL_ROOTS)


def load_pending_latest():
    latest = {}
    first_seen = {}
    preserved_identity_fields = (
        "downloadUrlHash",
        "download_url_hash",
        "url_hash",
        "downloadUrlHost",
        "download_url_host",
        "downloadUrl",
        "download_url",
        "indexer",
        "indexerId",
        "client_id",
        "client_hash",
        "nzo_id",
        "nzo_ids",
    )
    if not PENDING_IMPORTS_LOG.exists():
        return []
    with PENDING_IMPORTS_LOG.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") != "comics":
                continue
            key = pending_key(record)
            if not key:
                continue
            first_seen.setdefault(key, float(record.get("created_at") or record.get("ts") or now()))
            record["pending_key"] = key
            record["first_sent_at"] = first_seen[key]
            previous = latest.get(key) or {}
            for field in preserved_identity_fields:
                if record.get(field) in (None, "", []):
                    value = previous.get(field)
                    if value not in (None, "", []):
                        record[field] = value
            record["aliases"] = [item for item in (norm(record.get("title")), norm(record.get("query"))) if item]
            latest[key] = record
    return list(latest.values())


def qbit_settings():
    try:
        acquire = load_acquire_module()
        if acquire is not None and hasattr(acquire, "load_qbit_settings"):
            cfg = acquire.load_qbit_settings()
            host = str(cfg.get("host") or "").strip().rstrip("/")
            user = str(cfg.get("user") or "").strip()
            password = str(cfg.get("pass") or "").strip()
            if host and user and password:
                return {"host": host, "user": user, "pass": password}
    except Exception:
        pass
    try:
        import yaml

        cfg = yaml.safe_load(QBIT_CONFIG.read_text())["qbt"]
        host = str(os.environ.get("INKDROP_QBITTORRENT_URL") or cfg.get("host") or "").rstrip("/")
        if not host:
            return None
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        return {
            "host": host,
            "user": str(os.environ.get("INKDROP_QBITTORRENT_USERNAME") or cfg["user"]),
            "pass": str(os.environ.get("INKDROP_QBITTORRENT_PASSWORD") or cfg["pass"]),
        }
    except Exception:
        host = str(os.environ.get("INKDROP_QBITTORRENT_URL") or "").strip().rstrip("/")
        user = str(os.environ.get("INKDROP_QBITTORRENT_USERNAME") or "").strip()
        password = str(os.environ.get("INKDROP_QBITTORRENT_PASSWORD") or "").strip()
        if not host or not user or not password:
            return None
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        return {"host": host, "user": user, "pass": password}


def longest_prefix_host_root(mappings, normalized):
    # Longest prefix wins, not first-configured -- otherwise a broad mapping
    # like /downloads can shadow a more specific one like /downloads/manga
    # depending on storage order.
    normalized_lower = normalized.lower()
    best_match = None
    for prefix, host_root in mappings:
        prefix_lower = prefix.lower()
        if normalized_lower != prefix_lower and not normalized_lower.startswith(prefix_lower + "/"):
            continue
        if best_match is None or len(prefix) > len(best_match[0]):
            best_match = (prefix, host_root)
    if best_match is None:
        return None
    prefix, host_root = best_match
    if normalized_lower == prefix.lower():
        return host_root
    return host_root / normalized[len(prefix) + 1 :]


def qbit_host_path(path_value):
    raw = str(path_value or "").replace("\\", "/").rstrip("/")
    if not raw:
        return None
    candidates = []
    normalized = normalize_remote_path_prefix(raw)
    configured_match = longest_prefix_host_root(configured_remote_path_mappings(), normalized)
    if configured_match is not None:
        candidates.append(configured_match)
    for qbit_prefix, host_prefix in QBIT_DOWNLOAD_PATH_MAP:
        if raw == qbit_prefix:
            candidates.append(host_prefix)
        elif raw.startswith(qbit_prefix + "/"):
            rel = raw[len(qbit_prefix) + 1 :]
            candidates.append(host_prefix / rel)
    if raw.startswith("/"):
        candidates.append(Path(raw))
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return candidates[0] if candidates else None


def load_qbit_file_list_cache():
    global QBIT_FILE_LIST_CACHE
    if QBIT_FILE_LIST_CACHE is not None:
        return QBIT_FILE_LIST_CACHE
    QBIT_FILE_LIST_CACHE = {}
    if QBIT_FILE_LIST_CACHE_SECONDS <= 0:
        return QBIT_FILE_LIST_CACHE
    try:
        payload = json.loads(QBIT_FILE_LIST_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return QBIT_FILE_LIST_CACHE
    if isinstance(payload, dict):
        entries = payload.get("entries") if isinstance(payload.get("entries"), dict) else payload
        QBIT_FILE_LIST_CACHE = entries if isinstance(entries, dict) else {}
    return QBIT_FILE_LIST_CACHE


def write_qbit_file_list_cache():
    global QBIT_FILE_LIST_CACHE_DIRTY
    if QBIT_FILE_LIST_CACHE_SECONDS <= 0 or not QBIT_FILE_LIST_CACHE_DIRTY:
        return False
    try:
        QBIT_FILE_LIST_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        QBIT_FILE_LIST_CACHE_PATH.write_text(
            json.dumps(
                {
                    "updated_at": now(),
                    "ttl_seconds": QBIT_FILE_LIST_CACHE_SECONDS,
                    "entries": load_qbit_file_list_cache(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        QBIT_FILE_LIST_CACHE_DIRTY = False
        return True
    except OSError:
        return False


def qbit_file_list_cache_key(torrent_hash, save_path, require_complete):
    return "|".join(
        (
            str(torrent_hash or "").strip().lower(),
            normalize_remote_path_prefix(save_path).lower(),
            "complete" if require_complete else "incomplete",
        )
    )


def cached_qbit_archive_paths(torrent_hash, save_path, require_complete):
    if QBIT_FILE_LIST_CACHE_SECONDS <= 0 or not torrent_hash or not require_complete:
        return None
    entry = load_qbit_file_list_cache().get(qbit_file_list_cache_key(torrent_hash, save_path, require_complete))
    if not isinstance(entry, dict):
        return None
    try:
        scanned_at = float(entry.get("scanned_at") or 0)
    except (TypeError, ValueError):
        return None
    if now() - scanned_at > QBIT_FILE_LIST_CACHE_SECONDS:
        return None
    paths = entry.get("archive_paths")
    if not isinstance(paths, list):
        return None
    existing_paths = []
    for path_value in paths:
        path_text = str(path_value or "").strip()
        if not path_text:
            continue
        path = Path(path_text)
        try:
            if path.exists() and path.is_file() and is_archive_path(path):
                existing_paths.append(str(path).replace("\\", "/"))
        except OSError:
            continue
    return existing_paths


def store_qbit_archive_paths(torrent_hash, save_path, require_complete, paths):
    global QBIT_FILE_LIST_CACHE_DIRTY
    if QBIT_FILE_LIST_CACHE_SECONDS <= 0 or not torrent_hash or not require_complete:
        return False
    cache = load_qbit_file_list_cache()
    cache[qbit_file_list_cache_key(torrent_hash, save_path, require_complete)] = {
        "scanned_at": now(),
        "archive_paths": [str(path) for path in (paths or []) if str(path or "").strip()],
    }
    QBIT_FILE_LIST_CACHE_DIRTY = True
    return True


def normalize_remote_path_prefix(value):
    return str(value or "").strip().replace("\\", "/").rstrip("/")


def parse_remote_path_mapping_string(value):
    mappings = []
    for item in str(value or "").split(";"):
        separator = "=>" if "=>" in item else "=" if "=" in item else ""
        if not separator:
            continue
        remote_prefix, host_root = item.split(separator, 1)
        remote_prefix = normalize_remote_path_prefix(remote_prefix)
        host_root = str(host_root or "").strip()
        if remote_prefix and host_root:
            mappings.append((remote_prefix, Path(host_root)))
    return mappings


def remote_path_mappings_from_value(value):
    if isinstance(value, str):
        return parse_remote_path_mapping_string(value)
    if not isinstance(value, (list, tuple)):
        return []
    mappings = []
    for item in value:
        if isinstance(item, str):
            mappings.extend(parse_remote_path_mapping_string(item))
            continue
        if not isinstance(item, dict):
            continue
        remote_prefix = normalize_remote_path_prefix(
            item.get("remote_path")
            or item.get("remotePath")
            or item.get("remote")
            or item.get("source")
            or item.get("client_path")
        )
        host_root = str(
            item.get("host_path")
            or item.get("hostPath")
            or item.get("local_path")
            or item.get("localPath")
            or item.get("local")
            or item.get("dest")
            or ""
        ).strip()
        if remote_prefix and host_root:
            mappings.append((remote_prefix, Path(host_root)))
    return mappings


def remote_path_mapping_config_version():
    # Cheap fingerprint of the rows configured_remote_path_mappings() reads,
    # so a Settings save (in another process, since reconcile always runs as
    # its own subprocess) is detected without anyone remembering to call an
    # invalidation hook.
    if not INKDROP_STATE_DB.exists():
        return None
    try:
        con = sqlite3.connect(str(INKDROP_STATE_DB), timeout=5)
    except Exception:
        return None
    try:
        row = con.execute(
            "select max(updated_at) from ("
            "select updated_at from app_settings where key in (?, ?)"
            " union all "
            "select updated_at from provider_configs where id in (?, ?)"
            ")",
            ("download_clients.remote_path_mappings", "path.remote_path_mappings", "sabnzbd", "qbittorrent"),
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        con.close()


def configured_remote_path_mappings():
    global CONFIGURED_REMOTE_PATH_MAPPINGS_CACHE, CONFIGURED_REMOTE_PATH_MAPPINGS_CACHE_VERSION
    version = remote_path_mapping_config_version()
    if CONFIGURED_REMOTE_PATH_MAPPINGS_CACHE is not None and version == CONFIGURED_REMOTE_PATH_MAPPINGS_CACHE_VERSION:
        return list(CONFIGURED_REMOTE_PATH_MAPPINGS_CACHE)
    mappings = []
    for env_name in ("INKDROP_REMOTE_PATH_MAPPINGS", "INKDROP_SAB_PATH_MAPPINGS", "INKDROP_UNC_PATH_MAPPINGS"):
        mappings.extend(parse_remote_path_mapping_string(os.environ.get(env_name)))
    if inkdrop_state is not None and INKDROP_STATE_DB.exists():
        for key in ("download_clients.remote_path_mappings", "path.remote_path_mappings"):
            try:
                setting = inkdrop_state.app_setting(INKDROP_STATE_DB, key) or {}
            except Exception:
                setting = {}
            mappings.extend(remote_path_mappings_from_value(setting.get("value")))
    for provider_id in ("sabnzbd", "qbittorrent"):
        config = provider_config(provider_id) or {}
        settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
        for key in ("remote_path_mappings", "path_mappings"):
            mappings.extend(remote_path_mappings_from_value(settings.get(key)))
    out = []
    seen = set()
    for remote_prefix, host_root in mappings:
        key = (remote_prefix.lower(), str(host_root))
        if remote_prefix and str(host_root) and key not in seen:
            out.append((remote_prefix, host_root))
            seen.add(key)
    CONFIGURED_REMOTE_PATH_MAPPINGS_CACHE = tuple(out)
    CONFIGURED_REMOTE_PATH_MAPPINGS_CACHE_VERSION = version
    return out


def download_client_host_path(path_value, download_client_instance_id=None, db_path=None):
    raw = str(path_value or "").strip()
    if not raw:
        return None
    normalized = normalize_remote_path_prefix(raw)
    if download_client_instance_id:
        mappings = remote_path_mappings_from_value(inkdrop_download_client_routing.instance_path_mappings(
            db_path or INKDROP_STATE_DB, download_client_instance_id
        ))
    else:
        qbit_path = qbit_host_path(raw)
        if qbit_path:
            return qbit_path
        mappings = configured_remote_path_mappings()
    match = longest_prefix_host_root(mappings, normalized)
    return match if match is not None else Path(raw)


MEDIA_MANAGEMENT_IMPORT_EVIDENCE_KEYS = (
    "media_management_destination_decision",
    "media_management_preview",
    "selected_import_dest_path",
    "legacy_import_dest_path",
    "planned_path",
    "planned_path_apply_status",
    "planned_path_applied",
    "apply_planned_path_override",
    "current_import_dest_matches_preview",
)


def normalized_path_text(path_value):
    return str(path_value or "").strip().replace("\\", "/").rstrip("/")


def path_text_under_root(path_value, root_value):
    path_text = normalized_path_text(path_value)
    root_text = normalized_path_text(root_value)
    if not path_text or not root_text:
        return False
    return path_text == root_text or path_text.startswith(root_text + "/")


def media_management_replay_roots():
    settings = {}
    if inkdrop_state is not None and INKDROP_STATE_DB.exists():
        try:
            settings = inkdrop_state.media_management_settings_context(INKDROP_STATE_DB)
        except Exception:
            settings = {}
    defaults = getattr(inkdrop_state, "DEFAULT_LIBRARY_ROOTS", {}) if inkdrop_state is not None else {}
    candidates = [
        ("comic", settings.get("comic_root") or defaults.get("comic")),
        ("manga", settings.get("manga_root") or defaults.get("manga")),
    ]
    roots = []
    seen = set()
    for media_type, root in candidates:
        root_text = normalized_path_text(root)
        if not root_text or root_text in seen:
            continue
        seen.add(root_text)
        roots.append({"media_type": media_type, "root": root_text})
    return roots, settings


def media_management_event_evidence(event):
    event = event if isinstance(event, dict) else {}
    if inkdrop_state is not None and hasattr(inkdrop_state, "import_result_media_management_evidence"):
        try:
            return inkdrop_state.import_result_media_management_evidence(event)
        except Exception:
            pass
    evidence = {}
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    for source in (event, raw):
        for key in MEDIA_MANAGEMENT_IMPORT_EVIDENCE_KEYS:
            if key in evidence or key not in source:
                continue
            value = source.get(key)
            if value is None or value == "":
                continue
            evidence[key] = value
    return evidence


def replay_managed_destination_evidence(source_path, dest_path, reason=None):
    source_text = normalized_path_text(source_path)
    dest_text = normalized_path_text(dest_path)
    if not dest_text:
        return {}
    roots, settings = media_management_replay_roots()
    matched = next((item for item in roots if path_text_under_root(dest_text, item.get("root"))), None)
    if not matched:
        return {}
    same_path = bool(source_text and source_text.lower() == dest_text.lower())
    applied = not same_path
    root = matched.get("root") or ""
    reason_key = "replayed_managed_destination" if applied else "already_managed_folder"
    decision = {
        "enabled": True,
        "override": False,
        "legacy_dest_path": source_text,
        "selected_dest_path": dest_text,
        "planned_path": dest_text,
        "applied": applied,
        "skip_existing_destination": False,
        "reason": reason_key,
        "source": "reconciliation_replay",
        "replay_reason": str(reason or "").strip(),
    }
    preview = {
        "preview_only": False,
        "replay_only": True,
        "mutates_filesystem": False,
        "media_type": matched.get("media_type") or "comic",
        "root": root,
        "source_path": source_text,
        "existing_dest_path": dest_text,
        "planned_path": dest_text,
        "selected_import_dest_path": dest_text,
        "legacy_import_dest_path": source_text,
        "current_import_dest_path": dest_text,
        "current_import_dest_matches_preview": True,
        "apply_planned_path": True,
        "apply_planned_path_enabled": True,
        "apply_planned_path_override": False,
        "planned_path_applied": applied,
        "planned_path_apply_status": "replayed_selected" if applied else "already_target",
        "folder_completion_policy": str(settings.get("folder_completion_policy") or "folder_first"),
        "library_visibility_required": env_bool(
            "INKDROP_LIBRARY_VISIBILITY_REQUIRED",
            bool(settings.get("library_visibility_required", False)),
        ),
        "library_visibility_checks_enabled": bool(settings.get("library_visibility_checks_enabled", False)),
        "next_action": "Managed folder already contains the imported file",
    }
    return {
        "media_management_destination_decision": decision,
        "media_management_preview": preview,
    }


def qbit_archive_paths(session, host, torrent_hash, save_path, require_complete=True):
    paths = []
    if not torrent_hash:
        return paths
    cached = cached_qbit_archive_paths(torrent_hash, save_path, require_complete)
    if cached is not None:
        return cached
    qbit_base = str(save_path or "/downloads/comics").replace("\\", "/").rstrip("/")
    try:
        files = session.get(host + "/api/v2/torrents/files", params={"hash": torrent_hash}, timeout=20).json()
    except Exception:
        return paths
    for item in files:
        try:
            file_progress = float(item.get("progress") or 0)
            if require_complete and file_progress < 0.999:
                continue
            if not require_complete and file_progress >= 0.999:
                continue
        except (TypeError, ValueError):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if Path(name).name.startswith("."):
            continue
        candidate = qbit_host_path(qbit_base + "/" + name.lstrip("/"))
        if not candidate or not is_archive_path(candidate) or is_internal_segment_path(candidate):
            continue
        try:
            if not (candidate.exists() and candidate.is_file()):
                continue
        except OSError:
            continue
        paths.append(str(candidate).replace("\\", "/"))
    store_qbit_archive_paths(torrent_hash, save_path, require_complete, paths)
    return paths


def qbit_items():
    cfg = qbit_settings()
    if not cfg:
        return []
    try:
        session = requests.Session()
        login = session.post(
            cfg["host"] + "/api/v2/auth/login",
            data={"username": cfg["user"], "password": cfg["pass"]},
            timeout=15,
        )
        login.raise_for_status()
        torrents = session.get(cfg["host"] + "/api/v2/torrents/info", timeout=20).json()
        out = []
        for torrent in torrents:
            category = str(torrent.get("category") or "")
            raw_tags = str(torrent.get("tags") or "")
            tags = {part.strip().lower() for part in raw_tags.split(",") if part.strip()}
            if category not in COMIC_CLIENT_CATEGORIES and not (tags & QBIT_BROAD_TAGS):
                continue
            progress = float(torrent.get("progress") or 0)
            state = str(torrent.get("state") or "")
            client_state = "completed_in_client" if progress >= 0.999 else "downloading"
            state_lower = state.lower()
            if progress < 0.999 and "stall" in state_lower:
                client_state = "stalled_downloading"
            if progress < 0.999 and any(token in state_lower for token in ("stopped", "paused")):
                client_state = "stopped_downloading"
            torrent_hash = torrent.get("hash")
            archive_paths = qbit_archive_paths(session, cfg["host"], torrent_hash, torrent.get("save_path"))
            incomplete_archive_paths = (
                qbit_archive_paths(session, cfg["host"], torrent_hash, torrent.get("save_path"), require_complete=False)
                if progress < 0.999
                else []
            )
            all_archive_paths = list(dict.fromkeys([*archive_paths, *incomplete_archive_paths]))
            out.append(
                {
                    "client": "qbit",
                    "name": torrent.get("name"),
                    "hash": torrent_hash,
                    "category": category,
                    "tags": raw_tags,
                    "progress": progress,
                    "state": state,
                    "bytes_total": torrent.get("total_size") or torrent.get("size"),
                    "bytes_completed": torrent.get("downloaded"),
                    "download_rate_bytes_per_second": torrent.get("dlspeed"),
                    "upload_rate_bytes_per_second": torrent.get("upspeed"),
                    "eta_seconds": torrent.get("eta"),
                    "started_at": torrent.get("added_on"),
                    "last_updated_at": torrent.get("last_activity"),
                    "completed_at": torrent.get("completion_on"),
                    "save_path": torrent.get("save_path"),
                    "archive_paths": all_archive_paths,
                    "completed_archive_paths": archive_paths,
                    "incomplete_archive_paths": incomplete_archive_paths,
                    "partial_archive_paths_ready": bool(progress < 0.999 and all_archive_paths),
                    "client_state": client_state,
                    "client_state_reason": f"qbit_{state}" if client_state == "stopped_downloading" else None,
                    "normalized": norm(torrent.get("name")),
                }
            )
        write_qbit_file_list_cache()
        return out
    except Exception as exc:
        return [{"client": "qbit", "error": str(exc), "client_state": "client_unavailable"}]


def sab_settings():
    try:
        acquire = load_acquire_module()
        if acquire is not None and hasattr(acquire, "load_sab_settings"):
            cfg = acquire.load_sab_settings()
            host = str(cfg.get("host") or "").strip().rstrip("/")
            api_key = str(cfg.get("api_key") or cfg.get("apikey") or "").strip()
            if host and api_key:
                return {
                    "host": host,
                    "apikey": api_key,
                    "source": cfg.get("source") or "inkdrop_acquire",
                }
    except Exception:
        pass
    config = provider_config("sabnzbd") or {}
    if config and not config.get("enabled", True):
        return None
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    fallback_host = ""
    fallback_api_key = ""
    try:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(MYLAR_CONFIG)
        fallback_host = cp.get("SABnzbd", "sab_host", fallback="").rstrip("/")
        fallback_api_key = cp.get("SABnzbd", "sab_apikey", fallback="").strip()
    except Exception:
        return None
    host = str(config.get("base_url") or settings.get("host") or fallback_host or "").strip().rstrip("/")
    if host and not host.startswith(("http://", "https://")):
        host = "http://" + host
    api_key = str(settings.get("api_key") or fallback_api_key or "").strip()
    if not host or not api_key:
        return None
    return {
        "host": host,
        "apikey": api_key,
        "source": config.get("source") or ("inkdrop" if config else "mylar"),
    }


def sab_api(settings, mode, **params):
    params = {"mode": mode, "output": "json", "apikey": settings["apikey"], **params}
    response = requests.get(settings["host"] + "/api", params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def sab_slot_text(slot):
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


def classify_sab_history_slot(slot):
    raw_text = sab_slot_text(slot)
    text = raw_text.lower()
    status = str(slot.get("status") or "").lower()
    if "duplicate nzb" in text:
        return "failed_download", "failed_download_duplicate_nzb", raw_text
    if "dognzb.cr/fail" in text or "url fetching failed" in text or "maximum retries" in text:
        return "failed_download", "sab_url_fetch_failed", raw_text
    if (
        "sabnzbd.org/not-complete" in text
        or "cannot be completed" in text
        or "not complete" in text
        or "missing articles" in text
        or ("article" in text and "missing" in text)
    ):
        return "failed_download", "sab_not_complete", raw_text
    if any(term in text for term in ("error importing", "unpack", "repair", "crc", "post-process", "post processing", "rar error")):
        return "bad_archive", "sab_bad_archive", raw_text
    if "orphan" in text:
        return "manual_review", "sab_orphaned_job", raw_text
    if "fail" in status or "retry" in text or "aborted" in text or "failed" in text:
        return "failed_download", "sab_failed_download", raw_text
    return None, None, raw_text


def sab_items():
    settings = sab_settings()
    if not settings:
        return []
    items = []
    try:
        queue = sab_api(settings, "queue").get("queue", {}).get("slots", [])
        for slot in queue:
            category = str(slot.get("cat") or slot.get("category") or "")
            name = slot.get("filename") or slot.get("name") or slot.get("nzb_name")
            if category not in COMIC_CLIENT_CATEGORIES and "comic" not in norm(name):
                continue
            status = str(slot.get("status") or "").lower()
            state = "queued" if "queued" in status else "downloading"
            items.append(
                {
                    "client": "sab",
                    "name": name,
                    "nzo_id": slot.get("nzo_id"),
                    "category": category,
                    "status": slot.get("status"),
                    "progress": slot.get("percentage"),
                    "mb": slot.get("mb"),
                    "mbleft": slot.get("mbleft"),
                    "speed": slot.get("speed"),
                    "timeleft": slot.get("timeleft"),
                    "client_state": state,
                    "normalized": norm(name),
                }
            )
        history = sab_api(settings, "history", limit=200).get("history", {}).get("slots", [])
        for slot in history:
            category = str(slot.get("cat") or slot.get("category") or "")
            name = slot.get("name") or slot.get("filename") or slot.get("nzb_name")
            text = norm(sab_slot_text(slot))
            if category not in COMIC_CLIENT_CATEGORIES and not any(word in text for word in ("comic", "manga", "berserk", "one piece")):
                continue
            status = str(slot.get("status") or "").lower()
            if "complete" in status and "fail" not in status and "error" not in status:
                state = "completed_in_client"
                reason = "sab_completed_history"
                failure_state = None
                failure_reason = None
                failure_detail = None
            else:
                failure_state, failure_reason, failure_detail = classify_sab_history_slot(slot)
                if failure_state:
                    state = failure_state
                    reason = failure_reason
                else:
                    state = "completed_in_client"
                    reason = "sab_completed_history"
            storage = str(slot.get("storage") or slot.get("path") or "").strip()
            archive_paths = archive_paths_for_completed_client_path(storage) if state == "completed_in_client" else []
            local_path = storage
            if archive_paths:
                local_path = archive_paths[0] if len(archive_paths) == 1 else str(normalize_download_path(storage))
            items.append(
                {
                    "client": "sab",
                    "name": name,
                    "nzo_id": slot.get("nzo_id"),
                    "category": category,
                    "status": slot.get("status"),
                    "progress": slot.get("percentage"),
                    "mb": slot.get("mb"),
                    "mbleft": slot.get("mbleft"),
                    "completed_at": slot.get("completed"),
                    "storage": storage,
                    "local_path": local_path,
                    "archive_paths": archive_paths,
                    "fail_message": slot.get("fail_message"),
                    "client_state_reason": reason,
                    "failure_detail": failure_detail[:500] if failure_detail else None,
                    "client_state": state,
                    "normalized": norm(name),
                }
            )
    except Exception as exc:
        items.append({"client": "sab", "error": str(exc), "client_state": "client_unavailable"})
    return items


def download_client_archive_local_path(archive_paths):
    paths = [str(path or "").strip().replace("\\", "/").rstrip("/") for path in (archive_paths or []) if str(path or "").strip()]
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]
    parents = [posixpath.dirname(path) for path in paths if path]
    if not parents:
        return ""
    try:
        common_parent = posixpath.commonpath(parents)
    except ValueError:
        common_parent = parents[0]
    return common_parent.rstrip("/") or common_parent


def download_client_reconcile_snapshot(item):
    item = item if isinstance(item, dict) else {}
    client = str(item.get("client") or "").strip().lower()
    if client in {"qbit", "qbittorrent"}:
        client_key = "qbittorrent"
    elif client in {"sab", "sabnzbd"}:
        client_key = "sabnzbd"
    else:
        return None
    client_state = str(item.get("client_state") or item.get("state") or "").strip().lower()
    if client_state == "completed_in_client":
        status = "completed_in_client"
    elif client_state in {"failed_download", "bad_archive", "stale_no_local_file"}:
        status = "failed_download"
    else:
        status = "downloading"
    archive_paths = [str(path) for path in (item.get("archive_paths") or []) if str(path or "").strip()]
    archive_local_path = download_client_archive_local_path(archive_paths)
    local_path = (
        archive_local_path
        or str(item.get("local_path") or "").strip()
        or str(item.get("storage") or "").strip()
        or str(item.get("save_path") or "").strip()
    )
    external_id = item.get("hash") or item.get("nzo_id") or item.get("id")
    snapshot = {
        "client": client_key,
        "download_client_instance_id": item.get("download_client_instance_id"),
        "source": client_key,
        "status": status,
        "client_state": item.get("state") or item.get("status") or client_state,
        "raw_state": client_state,
        "title": item.get("name") or item.get("title"),
        "name": item.get("name") or item.get("title"),
        "external_id": external_id,
        "hash": item.get("hash"),
        "nzo_id": item.get("nzo_id"),
        "category": item.get("category"),
        "save_path": item.get("save_path"),
        "storage": item.get("storage"),
        "local_path": local_path,
        "archive_paths": archive_paths,
        "partial_archive_paths_ready": bool(item.get("partial_archive_paths_ready")),
        "progress": 1.0 if status == "completed_in_client" else item.get("progress"),
        "failure_reason": item.get("client_state_reason") or item.get("fail_message") or item.get("error"),
        "raw": item,
    }
    try:
        from inkdrop_transfer import normalize_transfer_status

        snapshot["transfer"] = normalize_transfer_status(snapshot, item)
    except Exception:
        snapshot["transfer"] = None
    return snapshot


def pack_fanout_active_queue_rows(con, limit=600):
    rows = con.execute(
        """
        select q.id as queue_id, q.wanted_id, q.series_id, q.issue_id,
               q.query, q.state, q.current_source, q.raw_json,
               s.title as series_title, s.year as series_year,
               i.issue_number, i.normalized_number, i.title as issue_title
        from queue_items q
        join series s on s.id=q.series_id
        left join issues i on i.id=q.issue_id
        where q.active=1
          and q.state in ('queued','source_wait','searching','downloading','importing')
          and not exists (
              select 1
              from download_tasks d
              where d.queue_id=q.id
                and d.state='verified'
                and lower(coalesce(d.status,'')) in ('queue_verified','verified','resolved')
          )
          and not exists (
              select 1
              from import_results ir
              where ir.queue_id=q.id
                and coalesce(ir.verified,0)=1
          )
        order by coalesce(q.updated_at, q.created_at, 0) asc, q.id asc
        limit ?
        """,
        (max(1, int(limit or 600)),),
    ).fetchall()
    return [dict(row) for row in rows]


def pack_fanout_existing_active_task(con, queue_id):
    row = con.execute(
        """
        select id, queue_id, wanted_id, series_id, issue_id, source, provider, protocol,
               download_client, external_id, candidate_identity, title, status, state,
               local_path, updated_at, raw_json
        from download_tasks
        where queue_id=?
          and state in ('downloading','import_ready','importing')
        order by coalesce(updated_at, completed_at, started_at, 0) desc
        limit 1
        """,
        (queue_id,),
    ).fetchone()
    return dict(row) if row else None


def pack_fanout_candidate_previously_failed(con, queue_id, archive_path):
    path_text = str(archive_path or "").strip()
    if not queue_id or not path_text:
        return False
    row = con.execute(
        """
        select 1
        from download_tasks
        where queue_id=?
          and local_path=?
          and state in ('failed','retired')
        limit 1
        """,
        (queue_id, path_text),
    ).fetchone()
    return row is not None


def pack_fanout_can_promote_existing_task(task, snapshot):
    task = task if isinstance(task, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    task_client = str(task.get("download_client") or task.get("protocol") or task.get("source") or "").strip().lower()
    snap_client = str(snapshot.get("client") or snapshot.get("source") or "").strip().lower()
    if snapshot.get("local_pack_replay"):
        if task_client not in {"qbit", "qbittorrent", "sab", "sabnzbd", "download_client"}:
            return False
        task_state = str(task.get("state") or "").strip().lower()
        task_status = str(task.get("status") or "").strip().lower()
        if task_state == "downloading":
            return True
        if task_state not in {"import_ready", "importing"} or task_status != "completed_in_client":
            return False
        task_path = str(task.get("local_path") or "").strip()
        if not task_path:
            return True
        try:
            return not is_archive_path(Path(task_path))
        except Exception:
            return True
    if task_client not in {"qbit", "qbittorrent"} or snap_client not in {"qbit", "qbittorrent"}:
        return False
    snap_id = str(snapshot.get("external_id") or snapshot.get("hash") or "").strip()
    task_id = str(task.get("external_id") or task.get("candidate_identity") or "").strip()
    if not bool(snap_id and (not task_id or snap_id == task_id)):
        return False
    task_state = str(task.get("state") or "").strip().lower()
    task_status = str(task.get("status") or "").strip().lower()
    if task_state == "downloading":
        return True
    if task_state not in {"import_ready", "importing"} or task_status != "completed_in_client":
        return False
    task_path = str(task.get("local_path") or "").strip()
    if not task_path:
        return True
    try:
        return not is_archive_path(task_path)
    except Exception:
        return True


def retire_pack_fanout_promoted_task(con, task, archive_path, ts):
    if not task:
        return 0
    raw = {}
    try:
        raw = json.loads(task.get("raw_json") or "{}")
    except (TypeError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    raw.update(
        {
            "previous_status": task.get("status"),
            "previous_state": task.get("state"),
            "superseded_by": "completed_pack_file_fanout",
            "superseded_archive_path": str(archive_path),
            "superseded_at": ts,
            "superseded_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        }
    )
    cur = con.execute(
        """
        update download_tasks
           set status='superseded_by_file_ready',
               state='failed',
               lifecycle_phase='superseded',
               retry_eligible=0,
               updated_at=?,
               completed_at=coalesce(completed_at, ?),
               raw_json=?
         where id=?
        """,
        (ts, ts, json.dumps(raw, sort_keys=True), task.get("id")),
    )
    return max(0, int(cur.rowcount or 0))


def pack_fanout_attempt(snapshot, archive_path, row, detail, ts):
    client = str(snapshot.get("client") or snapshot.get("source") or "download_client").strip().lower()
    local_pack_replay = bool(snapshot.get("local_pack_replay"))
    incomplete_qbit_source = str(detail.get("state") or "").strip() == "source_file_incomplete_qbit_download"
    protocol = (
        "direct"
        if local_pack_replay
        else "torrent" if client in {"qbit", "qbittorrent"}
        else "usenet" if client in {"sab", "sabnzbd"}
        else str(snapshot.get("protocol") or "").strip()
    )
    archive_text = str(archive_path)
    partial_file_ready = str(snapshot.get("status") or "").strip().lower() != "completed_in_client"
    status = (
        "waiting_for_complete_source"
        if incomplete_qbit_source
        else "staged_file_ready" if local_pack_replay else "completed_in_client"
    )
    reason = (
        "qBittorrent still reports the pack file incomplete; import will retry automatically"
        if incomplete_qbit_source
        else
        "local completed pack contains this wanted issue; import worker will scan it"
        if local_pack_replay
        else
        "qBittorrent reports this pack file complete; import worker will scan it"
        if partial_file_ready
        else "completed pack contains this wanted issue"
    )
    payload = {
        "source": "local_pack" if local_pack_replay else "download_client",
        "provider_id": "local_pack" if local_pack_replay else "download_client",
        "provider": "Local completed pack" if local_pack_replay else snapshot.get("name") or snapshot.get("title") or client or "download_client",
        "protocol": protocol,
        "download_client": "inkdrop_local_pack" if local_pack_replay else "qbittorrent" if client in {"qbit", "qbittorrent"} else "sabnzbd" if client in {"sab", "sabnzbd"} else client,
        "external_id": snapshot.get("external_id") or snapshot.get("hash") or snapshot.get("nzo_id"),
        "status": status,
        "lifecycle_phase": "downloading" if incomplete_qbit_source else "staged_or_importing",
        "outcome": "productive",
        "display_phase": "source_wait" if incomplete_qbit_source else "import_ready",
        "category": snapshot.get("category"),
        "save_path": snapshot.get("local_path") or snapshot.get("save_path") or snapshot.get("storage"),
        "local_path": archive_text,
        "matched_local_path": archive_text,
        "title": snapshot.get("name") or snapshot.get("title") or Path(archive_text).name,
        "candidate_identity": inkdrop_state.stable_id(
            "pack_fanout_candidate",
            row.get("queue_id"),
            snapshot.get("external_id") or snapshot.get("hash") or snapshot.get("nzo_id") or snapshot.get("name"),
            archive_text,
        ),
        "ts": ts,
        "pack_fanout": True,
        "pack_fanout_source": "download_client_reconcile",
        "pack_source_title": snapshot.get("name") or snapshot.get("title"),
        "pack_source_path": snapshot.get("local_path") or snapshot.get("save_path") or snapshot.get("storage"),
        "pack_client_external_id": snapshot.get("external_id") or snapshot.get("hash") or snapshot.get("nzo_id"),
        "pack_archive_path": archive_text,
        "pack_source_status": snapshot.get("status"),
        "local_pack_replay": local_pack_replay,
        "partial_pack_file_ready": bool(partial_file_ready),
        "matched_series": detail.get("matched_series") or row.get("series_title"),
        "trusted_series_id": row.get("series_id"),
        "trusted_issue": row.get("issue_number") or row.get("normalized_number"),
        "reason": reason,
    }
    if incomplete_qbit_source:
        payload.update(
            {
                "failure_reason": "source_file_incomplete_qbit_download",
                "retry_eligible": True,
                "started_at": ts,
                "progress": snapshot.get("progress"),
            }
        )
    else:
        payload["completed_at"] = ts
    return payload


def pack_fanout_lightweight_detail(path, row, imported_state):
    source_text = str(path)
    if source_text in imported_state.get("source_paths", set()) or source_text in imported_state.get("dest_paths", set()):
        # The ledger knowing this ARCHIVE was imported is not evidence it was
        # imported FOR THIS ROW's identity. This branch used to skip that
        # question entirely and downstream wrote verified=1 for whichever
        # queue row matched the filename -- how Low #6 got marked satisfied
        # by a Batman: The Long Halloween file, and how one physical file
        # verified two editions (PASS4-ACQ-01; the recovery branch already
        # carries this exact gate, this one just never did).
        dest_text = imported_state.get("source_to_dest", {}).get(source_text) or source_text
        identity_ok, identity_reason = imported_file_identity_match(
            row, {"source": source_text, "dest": dest_text}
        )
        if not identity_ok:
            return {
                "state": "ready_to_import",
                "reason": f"prior_import_identity_mismatch:{identity_reason}",
                "matched_series": row.get("series_title"),
                "truth_model": "inkdrop_queue",
            }
        return {
            "state": "already_imported",
            "reason": "source_already_imported",
            "matched_series": row.get("series_title"),
            "source_path": source_text,
            "dest_path": dest_text,
        }
    return {
        "state": "ready_to_import",
        "reason": "completed_pack_filename_match",
        "matched_series": row.get("series_title"),
        "truth_model": "inkdrop_queue",
    }


def pack_fanout_incomplete_qbit_detail(path, row):
    return {
        "state": "source_file_incomplete_qbit_download",
        "reason": "source_file_incomplete_qbit_download",
        "matched_series": row.get("series_title"),
        "source_path": str(path),
        "truth_model": "inkdrop_queue",
    }


def pack_fanout_path_keys(path):
    text = str(path or "").strip()
    if not text:
        return set()
    keys = {text, text.replace("\\", "/")}
    try:
        keys.add(str(Path(text)))
        keys.add(str(Path(text)).replace("\\", "/"))
    except (TypeError, ValueError):
        pass
    return {key for key in keys if key}


def clear_stale_pack_no_match_markers(raw):
    raw = dict(raw or {}) if isinstance(raw, dict) else {}
    for key in (
        "pack_no_match_retry",
        "stale_pack_reason",
        "stale_pack_title",
        "stale_pack_path",
        "stale_pack_source",
    ):
        raw.pop(key, None)
    return raw


def pack_fanout_snapshot_priority(snapshot):
    text = norm(" ".join(
        str(snapshot.get(key) or "")
        for key in ("name", "title", "local_path", "save_path", "storage")
    ))
    if re.search(r"\bweekly (comics?|releases?|pack)\b", text):
        return (0, text)
    if re.search(r"\b(dc|marvel|image|dark horse|idw|boom) (comics? )?week(ly)?\b", text):
        return (0, text)
    if " pack " in f" {text} " or " complete " in f" {text} " or " batch " in f" {text} ":
        return (1, text)
    return (2, text)


def local_completed_pack_root_priority(root):
    name = norm(Path(root).name)
    if import_ready_is_weekly_pack(str(root), Path(root).name):
        return (0, name)
    if re.search(r"\b(?:dc|marvel|image)\s+comics?\s+20\d{2}\b", name):
        return (1, name)
    if re.search(r"\b(?:dc|marvel|image)\s+(?:comics?\s+)?(?:weekly|week|releases?)\b", name):
        return (1, name)
    return (9, name)


def is_local_completed_pack_root(root):
    try:
        path = Path(root)
    except (TypeError, ValueError):
        return False
    if not path.exists() or not path.is_dir():
        return False
    priority, _name = local_completed_pack_root_priority(path)
    return priority < 9


def local_completed_pack_roots(max_roots=None):
    max_roots = INKDROP_LOCAL_PACK_REPLAY_MAX_ROOTS if max_roots is None else max_roots
    if int(max_roots or 0) <= 0:
        return []
    roots = []
    seen = set()
    for base in COMIC_LOCAL_ROOTS:
        try:
            base = Path(base)
            if not base.exists() or not base.is_dir():
                continue
            candidates = [base]
            with os.scandir(base) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            candidates.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
        for candidate in candidates:
            key = str(candidate)
            if key in seen or not is_local_completed_pack_root(candidate):
                continue
            seen.add(key)
            roots.append(candidate)
    roots.sort(key=local_completed_pack_root_priority)
    return roots[: max(1, int(max_roots or 1))]


def local_completed_pack_snapshots(max_roots=None):
    snapshots = []
    for root in local_completed_pack_roots(max_roots):
        archive_paths = archive_paths_for_completed_client_path(
            root,
            limit=INKDROP_LOCAL_PACK_REPLAY_ARCHIVE_LIMIT,
        )
        if not archive_paths:
            continue
        snapshots.append(
            {
                "client": "inkdrop_local_pack",
                "source": "local_pack",
                "protocol": "direct",
                "status": "completed_in_client",
                "client_state": "completed_in_client",
                "name": root.name,
                "title": root.name,
                "category": "comics",
                "local_path": str(root),
                "save_path": str(root),
                "storage": str(root),
                "archive_paths": archive_paths,
                "external_id": inkdrop_state.stable_id("local_pack_root", str(root)) if inkdrop_state else str(root),
                "local_pack_replay": True,
            }
        )
    return snapshots


def fanout_local_completed_packs_to_inkdrop(max_roots=None, max_rows=None, max_created=None):
    snapshots = local_completed_pack_snapshots(max_roots)
    if not snapshots:
        return {"ok": True, "checked": 0, "created": 0, "matched": 0, "snapshot_count": 0}
    result = fanout_completed_pack_snapshots_to_inkdrop(
        snapshots,
        max_rows=max_rows,
        max_created=max_created,
    )
    result = result if isinstance(result, dict) else {"result": result}
    result.setdefault("ok", True)
    result["snapshot_count"] = len(snapshots)
    result["roots_sample"] = [snapshot.get("local_path") for snapshot in snapshots[:8]]
    return result


def record_pack_fanout_already_imported(con, snapshot, archive_path, row, detail, ts):
    queue_id = str(row.get("queue_id") or "").strip()
    if not queue_id:
        return False
    source_path = str(detail.get("source_path") or archive_path or "").strip()
    dest_path = str(detail.get("dest_path") or source_path or "").strip()
    # One file, one identity: if another verified import already claims this
    # exact dest for a DIFFERENT issue, crediting a second issue from the
    # same bytes is exactly the two-editions-from-one-file corruption
    # (PASS4-ACQ-01: Batman The Long Halloween #1 and #10 both verified
    # against the same physical archive). Refuse and leave the row for the
    # real import path, which runs the full acceptance gates.
    issue_id = str(row.get("issue_id") or "").strip()
    if dest_path and issue_id:
        other_identity = con.execute(
            """
            select 1 from import_results
            where coalesce(verified, 0) = 1
              and dest_path = ?
              and coalesce(issue_id, '') not in ('', ?)
            limit 1
            """,
            (dest_path, issue_id),
        ).fetchone()
        if other_identity:
            return False
    source = "local_pack" if snapshot.get("local_pack_replay") else "download_client"
    library_visibility_required = inkdrop_state.boolish(
        inkdrop_state._app_setting_value_from_connection(
            con,
            "media_management.library_visibility_required",
            False,
        ),
        False,
    )
    status = "waiting_for_library_scan" if library_visibility_required else "folder_verified"
    verified = not library_visibility_required
    imported_count = 1
    skipped_count = 0
    outcome = inkdrop_state.import_result_outcome(status, verified, imported_count, skipped_count)
    display_phase = inkdrop_state.import_result_display_phase(status, verified, imported_count, skipped_count, outcome)
    completion_evidence = inkdrop_state.import_result_completion_evidence(
        status,
        verified,
        imported_count,
        dest_path,
        "library",
        library_visibility_required,
    )
    import_id = inkdrop_state.stable_id("direct_import_result", queue_id, source, dest_path or source_path)
    payload = {
        "kind": "pack_fanout_already_imported",
        "source": source,
        "queue_id": queue_id,
        "wanted_id": row.get("wanted_id"),
        "series_id": row.get("series_id"),
        "issue_id": row.get("issue_id"),
        "source_path": source_path,
        "dest_path": dest_path,
        "status": status,
        "verified": verified,
        "outcome": outcome,
        "display_phase": display_phase,
        **completion_evidence,
        "pack_source_title": snapshot.get("name") or snapshot.get("title"),
        "pack_source_path": snapshot.get("local_path") or snapshot.get("save_path") or snapshot.get("storage"),
        "pack_archive_path": str(archive_path),
        "recorded_at": ts,
        "recorded_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
    }
    con.execute(
        """
        insert into import_results(
            id, queue_id, source_attempt_id, series_id, issue_id, source_path, dest_path,
            status, outcome, display_phase, completion_truth, folder_imported,
            library_visibility_required, library_visibility_status, library_visibility_provider,
            verified, imported_count, skipped_count, created_at, raw_json
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(id) do update set
            source_path=excluded.source_path,
            dest_path=excluded.dest_path,
            status=excluded.status,
            outcome=excluded.outcome,
            display_phase=excluded.display_phase,
            completion_truth=excluded.completion_truth,
            folder_imported=excluded.folder_imported,
            library_visibility_required=excluded.library_visibility_required,
            library_visibility_status=excluded.library_visibility_status,
            library_visibility_provider=excluded.library_visibility_provider,
            verified=excluded.verified,
            imported_count=excluded.imported_count,
            skipped_count=excluded.skipped_count,
            created_at=excluded.created_at,
            raw_json=excluded.raw_json
        """,
        (
            import_id,
            queue_id,
            None,
            row.get("series_id"),
            row.get("issue_id"),
            source_path,
            dest_path,
            status,
            outcome,
            display_phase,
            completion_evidence["completion_truth"],
            1 if completion_evidence["folder_imported"] else 0,
            1 if completion_evidence["library_visibility_required"] else 0,
            completion_evidence["library_visibility_status"],
            completion_evidence["library_visibility_provider"],
            1 if verified else 0,
            imported_count,
            skipped_count,
            ts,
            json.dumps(payload, sort_keys=True),
        ),
    )
    message = (
        f"{inkdrop_state.source_display_label(source)} import copied to managed folder; frontend sync optional"
        if verified
        else f"{inkdrop_state.source_display_label(source)} import copied; waiting for required library visibility"
    )
    qraw = {}
    try:
        qraw = json.loads(row.get("raw_json") or "{}")
    except (TypeError, ValueError):
        qraw = {}
    qraw = qraw if isinstance(qraw, dict) else {}
    qraw = clear_stale_pack_no_match_markers(qraw)
    qraw.update(
        {
            "pack_fanout": True,
            "pack_fanout_already_imported": True,
            "pack_fanout_at": ts,
            "pack_fanout_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
            "pack_fanout_source_title": payload.get("pack_source_title"),
            "pack_fanout_archive_path": str(archive_path),
            "last_import_source": source,
            "last_import_source_path": source_path,
            "last_import_dest": dest_path,
            "last_import_status": status,
            "last_import_verified": verified,
            **completion_evidence,
            "last_event": message,
        }
    )
    queue_state = "verified" if verified else "importing"
    queue_active = 0 if verified else 1
    con.execute(
        """
        update queue_items
           set state=?,
               current_source=?,
               last_event=?,
               active=?,
               outcome=?,
               display_phase=?,
               updated_at=?,
               raw_json=?
         where id=? and active=1
        """,
        (
            queue_state,
            source,
            message,
            queue_active,
            "productive" if verified else "pending",
            "verified" if verified else "verifying",
            ts,
            json.dumps(qraw, sort_keys=True),
            queue_id,
        ),
    )
    if row.get("wanted_id"):
        wanted_status = "satisfied" if verified else "in_progress"
        con.execute("update wanted_items set status=?, updated_at=? where id=?", (wanted_status, ts, row.get("wanted_id")))
    try:
        inkdrop_state.refresh_queue_provider_status_columns(con, queue_id)
    except Exception:
        pass
    con.execute(
        """
        insert or ignore into history_events(
            id, entity_type, entity_id, series_id, issue_id, event_type,
            source, message, created_at, raw_json
        ) values(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            inkdrop_state.stable_id("pack_fanout_already_imported", import_id, status),
            "import_result",
            import_id,
            row.get("series_id"),
            row.get("issue_id"),
            "direct_import_result",
            source,
            message,
            ts,
            json.dumps(payload, sort_keys=True),
        ),
    )
    inkdrop_state.update_sync_meta(con, ts, "pack_fanout_already_imported")
    return True


def inkdrop_state_schema_present(con):
    required = {"queue_items", "wanted_items", "series", "issues", "source_attempts", "download_tasks", "import_results"}
    rows = con.execute(
        """
        select name
        from sqlite_master
        where type='table'
          and name in ('queue_items','wanted_items','series','issues','source_attempts','download_tasks','import_results')
        """
    ).fetchall()
    names = {str(row[0] if not isinstance(row, sqlite3.Row) else row["name"]) for row in rows}
    return required.issubset(names)


def apply_pack_fanout_incomplete_qbit_wait(snapshot, archive_path, row, detail, ts):
    queue_id = str(row.get("queue_id") or "").strip()
    if not queue_id:
        return {"ok": False, "reason": "missing_queue_id"}
    archive_keys = pack_fanout_path_keys(archive_path)
    message = "qBittorrent still reports the pack file incomplete; import will retry automatically"

    def _write():
        with inkdrop_state.connect(
            INKDROP_STATE_DB,
            timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS,
            busy_timeout_ms=INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS,
            configure_wal=False,
        ) as con:
            if not inkdrop_state_schema_present(con):
                inkdrop_state.init_schema(con)
            current = con.execute(
                """
                select id as queue_id, wanted_id, series_id, issue_id,
                       query, state, current_source, raw_json
                from queue_items
                where id=? and active=1
                limit 1
                """,
                (queue_id,),
            ).fetchone()
            if not current:
                return {"ok": False, "reason": "queue_inactive", "queue_id": queue_id}
            write_row = dict(row)
            write_row.update(dict(current))
            existing_task = pack_fanout_existing_active_task(con, queue_id)
            existing_task_blocks = False
            task_updated = 0
            if existing_task:
                task_state = str(existing_task.get("state") or "").strip().lower()
                task_status = str(existing_task.get("status") or "").strip().lower()
                task_client = str(
                    existing_task.get("download_client")
                    or existing_task.get("protocol")
                    or existing_task.get("source")
                    or ""
                ).strip().lower()
                task_path_keys = pack_fanout_path_keys(existing_task.get("local_path"))
                same_task_path = bool(archive_keys and task_path_keys and archive_keys.intersection(task_path_keys))
                preserves_real_transfer = task_state == "downloading" and task_client in {
                    "qbit",
                    "qbittorrent",
                    "download_client",
                    "sab",
                    "sabnzbd",
                }
                preserves_incomplete_wait = task_state == "downloading" and task_status == "waiting_for_complete_source"
                if same_task_path and (task_state in {"import_ready", "importing"} or task_status in {"staged_file_ready", "ready_import", "preview_importable", "completed_in_client"}):
                    raw = {}
                    try:
                        raw = json.loads(existing_task.get("raw_json") or "{}")
                    except (TypeError, ValueError):
                        raw = {}
                    raw = raw if isinstance(raw, dict) else {}
                    raw.update(
                        {
                            "pack_fanout_incomplete_qbit_wait": True,
                            "pack_fanout_incomplete_qbit_wait_at": ts,
                            "pack_fanout_incomplete_qbit_wait_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                            "pack_archive_path": str(archive_path),
                            "failure_reason": "source_file_incomplete_qbit_download",
                        }
                    )
                    outcome = inkdrop_state.download_task_outcome("downloading", "waiting_for_complete_source", "downloading")
                    display_phase = inkdrop_state.download_task_display_phase("downloading", "waiting_for_complete_source", "downloading", outcome)
                    cur = con.execute(
                        """
                        update download_tasks
                           set status='waiting_for_complete_source',
                               state='downloading',
                               lifecycle_phase='downloading',
                               failure_reason='source_file_incomplete_qbit_download',
                               retry_eligible=1,
                               updated_at=?,
                               completed_at=null,
                               outcome=?,
                               display_phase=?,
                               raw_json=?
                         where id=?
                        """,
                        (ts, outcome, display_phase, json.dumps(raw, sort_keys=True), existing_task.get("id")),
                    )
                    task_updated = int(cur.rowcount or 0)
                elif not (preserves_real_transfer or preserves_incomplete_wait):
                    existing_task_blocks = True

            if existing_task_blocks:
                return {"ok": False, "reason": "active_task_exists", "queue_id": queue_id}

            attempt = pack_fanout_attempt(snapshot, archive_path, write_row, detail, ts)
            attempt_id = inkdrop_state.stable_id("source_attempt_pack_fanout", queue_id, attempt.get("candidate_identity"))
            inkdrop_state.record_source_attempt(
                con,
                queue_id,
                write_row.get("wanted_id"),
                write_row.get("series_id"),
                write_row.get("issue_id"),
                attempt,
                attempt_id=attempt_id,
                started_at=ts,
                completed_at=None,
            )
            attempt_recorded = 1

            qraw = {}
            try:
                qraw = json.loads(write_row.get("raw_json") or "{}")
            except (TypeError, ValueError):
                qraw = {}
            qraw = qraw if isinstance(qraw, dict) else {}
            qraw = clear_stale_pack_no_match_markers(qraw)
            qraw.update(
                {
                    "pack_fanout": True,
                    "pack_fanout_incomplete_qbit_wait": True,
                    "pack_fanout_at": ts,
                    "pack_fanout_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                    "pack_fanout_source_title": snapshot.get("name") or snapshot.get("title"),
                    "pack_fanout_archive_path": str(archive_path),
                    "last_event": message,
                }
            )
            queue_cur = con.execute(
                """
                update queue_items
                   set state='downloading',
                       current_source='download_client',
                       display_phase='downloading',
                       last_event=?,
                       updated_at=?,
                       raw_json=?
                 where id=? and active=1
                """,
                (message, ts, json.dumps(qraw, sort_keys=True), queue_id),
            )
            if write_row.get("wanted_id"):
                con.execute("update wanted_items set status='in_progress', updated_at=? where id=?", (ts, write_row.get("wanted_id")))
            return {
                "ok": True,
                "updated": int(queue_cur.rowcount or 0),
                "created": 0,
                "deferred": 1,
                "task_updated": task_updated,
                "attempt_recorded": attempt_recorded,
                "queue_id": queue_id,
            }

    try:
        return with_sqlite_lock_retry(
            _write,
            attempts=INKDROP_PACK_FANOUT_LOCK_RETRY_ATTEMPTS,
            initial_delay=1.0,
        )
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        return {"ok": False, "reason": "state_db_locked", "error": str(exc), "queue_id": queue_id}


def apply_pack_fanout_match(snapshot, archive_path, row, detail, promotable_existing_task, ts):
    queue_id = str(row.get("queue_id") or "").strip()
    if not queue_id:
        return {"ok": False, "reason": "missing_queue_id"}

    def _write():
        with inkdrop_state.connect(
            INKDROP_STATE_DB,
            timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS,
            busy_timeout_ms=INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS,
            configure_wal=False,
        ) as con:
            if not inkdrop_state_schema_present(con):
                inkdrop_state.init_schema(con)
            current = con.execute(
                """
                select id as queue_id, wanted_id, series_id, issue_id,
                       query, state, current_source, raw_json
                from queue_items
                where id=? and active=1
                limit 1
                """,
                (queue_id,),
            ).fetchone()
            if not current:
                return {"ok": False, "reason": "queue_inactive", "queue_id": queue_id}
            write_row = dict(row)
            write_row.update(dict(current))
            existing_task = pack_fanout_existing_active_task(con, queue_id)
            write_promotable_task = None
            if existing_task:
                if pack_fanout_can_promote_existing_task(existing_task, snapshot):
                    write_promotable_task = existing_task
                else:
                    return {"ok": False, "reason": "active_task_exists", "queue_id": queue_id}

            if detail.get("state") == "already_imported":
                if record_pack_fanout_already_imported(con, snapshot, archive_path, write_row, detail, ts):
                    return {"ok": True, "updated": 1, "created": 0, "queue_id": queue_id}
                return {"ok": False, "reason": "already_imported_update_failed", "queue_id": queue_id}

            attempt = pack_fanout_attempt(snapshot, archive_path, write_row, detail, ts)
            retired = 0
            if write_promotable_task:
                retired = retire_pack_fanout_promoted_task(con, write_promotable_task, archive_path, ts)
            attempt_id = inkdrop_state.stable_id("source_attempt_pack_fanout", queue_id, attempt.get("candidate_identity"))
            inkdrop_state.record_source_attempt(
                con,
                queue_id,
                write_row.get("wanted_id"),
                write_row.get("series_id"),
                write_row.get("issue_id"),
                attempt,
                attempt_id=attempt_id,
                started_at=ts,
                completed_at=ts,
            )
            qraw = {}
            try:
                qraw = json.loads(write_row.get("raw_json") or "{}")
            except (TypeError, ValueError):
                qraw = {}
            qraw = qraw if isinstance(qraw, dict) else {}
            qraw = clear_stale_pack_no_match_markers(qraw)
            qraw.update(
                {
                    "pack_fanout": True,
                    "pack_fanout_at": ts,
                    "pack_fanout_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                    "pack_fanout_source_title": attempt.get("pack_source_title"),
                    "pack_fanout_archive_path": str(archive_path),
                    "partial_pack_file_ready": bool(attempt.get("partial_pack_file_ready")),
                    "last_event": attempt.get("reason"),
                }
            )
            con.execute(
                """
                update queue_items
                   set state='importing',
                       current_source='download_client',
                       last_event=?,
                       updated_at=?,
                       raw_json=?
                 where id=? and active=1
                """,
                (attempt.get("reason"), ts, json.dumps(qraw, sort_keys=True), queue_id),
            )
            if write_row.get("wanted_id"):
                con.execute("update wanted_items set status='in_progress', updated_at=? where id=?", (ts, write_row.get("wanted_id")))
            return {"ok": True, "created": 1, "updated": 0, "retired": retired, "queue_id": queue_id}

    try:
        return with_sqlite_lock_retry(
            _write,
            attempts=INKDROP_PACK_FANOUT_LOCK_RETRY_ATTEMPTS,
            initial_delay=1.0,
        )
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        return {"ok": False, "reason": "state_db_locked", "error": str(exc), "queue_id": queue_id}


def fanout_completed_pack_snapshots_to_inkdrop(snapshots, max_rows=None, max_created=None):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "inkdrop_state_unavailable", "created": 0}
    max_rows = INKDROP_PACK_FANOUT_MAX_ROWS if max_rows is None else max_rows
    max_created = INKDROP_PACK_FANOUT_MAX_CREATED if max_created is None else max_created
    incomplete_qbit_path_keys = None

    def qbit_incomplete_path_keys():
        nonlocal incomplete_qbit_path_keys
        if incomplete_qbit_path_keys is None:
            keys = set()
            loader = getattr(imp, "load_qbit_incomplete_paths", None)
            if callable(loader):
                try:
                    paths = loader("comics")
                except Exception:
                    paths = []
                for path in paths or []:
                    keys.update(pack_fanout_path_keys(path))
            incomplete_qbit_path_keys = keys
        return incomplete_qbit_path_keys

    def qbit_path_incomplete(path):
        keys = qbit_incomplete_path_keys()
        return bool(keys and pack_fanout_path_keys(path).intersection(keys))

    completed = []
    for snapshot in snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        if download_client_reconcile_snapshot(snapshot) is None and not snapshot.get("client"):
            continue
        status = str(snapshot.get("status") or "").strip().lower()
        partial_file_ready = (
            str(snapshot.get("client") or snapshot.get("source") or "").strip().lower() in {"qbit", "qbittorrent"}
            and bool(snapshot.get("partial_archive_paths_ready"))
            and status == "downloading"
        )
        if status != "completed_in_client" and not partial_file_ready:
            continue
        archive_paths = [Path(path) for path in (snapshot.get("archive_paths") or []) if str(path or "").strip()]
        if not archive_paths:
            continue
        completed.append((snapshot, archive_paths))
    completed.sort(key=lambda pair: pack_fanout_snapshot_priority(pair[0]))
    if not completed:
        return {"ok": True, "checked": 0, "created": 0, "matched": 0}

    imported_state = load_imported_state()
    ts = now()
    created = 0
    updated = 0
    matched = 0
    checked = 0
    planned = 0
    deferred = 0
    skipped = collections.Counter()
    samples = []

    def _fanout():
        nonlocal created, updated, matched, checked, planned, deferred
        with inkdrop_state.connect_read(
            INKDROP_STATE_DB,
            timeout_seconds=INKDROP_STATE_READ_TIMEOUT_SECONDS,
            busy_timeout_ms=INKDROP_STATE_READ_BUSY_TIMEOUT_MS,
        ) as con:
            rows = pack_fanout_active_queue_rows(con, max_rows)
            for snapshot, archive_paths in completed:
                archive_entries = [(path, norm(path.stem)) for path in archive_paths]
                for row in rows:
                    if created + updated >= int(max_created or 40):
                        skipped["created_limit"] += 1
                        break
                    existing_task = pack_fanout_existing_active_task(con, row.get("queue_id"))
                    promotable_existing_task = None
                    existing_task_blocks_fanout = False
                    if existing_task:
                        if pack_fanout_can_promote_existing_task(existing_task, snapshot):
                            promotable_existing_task = existing_task
                        else:
                            existing_task_blocks_fanout = True
                    issue_number = row.get("issue_number") or row.get("normalized_number")
                    series_key = norm(row.get("series_title"))
                    if not issue_number or not series_key:
                        skipped["missing_series_or_issue"] += 1
                        continue
                    candidates = [
                        path
                        for path, path_key in archive_entries
                        if norm_contains_phrase(path_key, series_key) and archive_matches_issue(path, issue_number, filename_only=True)
                    ]
                    if not candidates:
                        skipped["issue_not_in_pack"] += 1
                        continue
                    candidates.sort(key=lambda candidate: (1 if qbit_path_incomplete(candidate) else 0, str(candidate)))
                    for path in candidates[:12]:
                        checked += 1
                        try:
                            if not (path.exists() and path.is_file() and is_archive_path(path)):
                                skipped["archive_missing"] += 1
                                continue
                        except OSError:
                            skipped["archive_missing"] += 1
                            continue
                        if qbit_path_incomplete(path):
                            detail = pack_fanout_incomplete_qbit_detail(path, row)
                            planned += 1
                            write_result = apply_pack_fanout_incomplete_qbit_wait(snapshot, path, row, detail, ts)
                            if write_result.get("ok"):
                                matched += 1
                                updated += int(write_result.get("updated") or 0)
                                deferred += int(write_result.get("deferred") or 0)
                                if len(samples) < 8:
                                    samples.append({
                                        "queue_id": row.get("queue_id"),
                                        "series": row.get("series_title"),
                                        "issue": issue_number,
                                        "path": str(path),
                                        "state": "source_file_incomplete_qbit_download",
                                    })
                            else:
                                skipped["source_file_incomplete_qbit_download_" + str(write_result.get("reason") or "write_failed")] += 1
                            break
                        if archive_is_cover_only(path):
                            skipped["cover_only_archive"] += 1
                            continue
                        if archive_is_single_file_range_or_collection(path):
                            skipped["single_file_range_or_collection"] += 1
                            continue
                        if pack_fanout_candidate_previously_failed(con, row.get("queue_id"), path):
                            skipped["candidate_previously_failed"] += 1
                            continue
                        if existing_task_blocks_fanout:
                            skipped["active_task_exists"] += 1
                            break
                        collection_block_reason = ""
                        try:
                            collection_block_reason = inkdrop_state.collection_target_single_part_block_reason(
                                row,
                                {
                                    "matched_local_path": str(path),
                                    "title": path.name,
                                    "matched_series": row.get("series_title"),
                                },
                            )
                        except Exception:
                            collection_block_reason = ""
                        if collection_block_reason:
                            skipped[collection_block_reason] += 1
                            continue
                        detail = pack_fanout_lightweight_detail(path, row, imported_state)
                        if detail.get("state") == "already_imported":
                            planned += 1
                            write_result = apply_pack_fanout_match(snapshot, path, row, detail, promotable_existing_task, ts)
                            if write_result.get("ok"):
                                matched += 1
                                updated += int(write_result.get("updated") or 0)
                                if len(samples) < 8:
                                    samples.append({
                                        "queue_id": row.get("queue_id"),
                                        "series": row.get("series_title"),
                                        "issue": issue_number,
                                        "path": str(path),
                                        "state": "already_imported",
                                    })
                            else:
                                skipped["already_imported_" + str(write_result.get("reason") or "update_failed")] += 1
                            break
                        if detail.get("state") != "ready_to_import":
                            skipped[str(detail.get("reason") or detail.get("state") or "not_ready")] += 1
                            continue
                        planned += 1
                        write_result = apply_pack_fanout_match(snapshot, path, row, detail, promotable_existing_task, ts)
                        if not write_result.get("ok"):
                            skipped[str(write_result.get("reason") or "write_failed")] += 1
                            if write_result.get("reason") == "state_db_locked":
                                break
                            continue
                        matched += 1
                        created += int(write_result.get("created") or 0)
                        updated += int(write_result.get("updated") or 0)
                        if int(write_result.get("retired") or 0):
                            skipped["promoted_existing_task"] += int(write_result.get("retired") or 0)
                        if len(samples) < 8:
                            samples.append({"queue_id": row.get("queue_id"), "series": row.get("series_title"), "issue": issue_number, "path": str(path)})
                        break

    try:
        _fanout()
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        return {"ok": False, "reason": "state_db_locked", "error": str(exc), "created": created, "updated": updated, "matched": matched, "checked": checked, "planned": planned}

    return {
        "ok": True,
        "checked": checked,
        "planned": planned,
        "matched": matched,
        "created": created,
        "updated": updated,
        "deferred": deferred,
        "skipped": dict(sorted(skipped.items())),
        "samples": samples,
    }


def collect_download_client_reconcile_observations(requested_clients=None, observation_started_at=None):
    requested_clients = set(requested_clients or [])
    observation_started_at = now() if observation_started_at is None else float(observation_started_at)
    observations = []
    collectors = (
        ("qbittorrent", qbit_settings, qbit_items),
        ("sabnzbd", sab_settings, sab_items),
    )
    for client, settings_loader, collector in collectors:
        if requested_clients and client not in requested_clients:
            continue
        configured = bool(settings_loader())
        items = collector() if configured else []
        errors = [
            item
            for item in items
            if isinstance(item, dict) and item.get("client_state") == "client_unavailable"
        ]
        observations.append(
            {
                "client": client,
                "configured": configured,
                "authoritative": bool(configured and not errors),
                "observation_started_at": observation_started_at,
                "items": items,
                "errors": [
                    {key: item.get(key) for key in ("client", "error")}
                    for item in errors
                ],
            }
        )
    return {
        "observation_started_at": observation_started_at,
        "observations": observations,
    }


def download_client_reconcile_inputs(client_observations, requested_clients=None):
    requested_clients = set(requested_clients or [])
    available_clients = set()
    snapshots = []
    client_errors_out = []
    observation_started_at = None
    if isinstance(client_observations, dict) and isinstance(client_observations.get("observations"), list):
        observation_started_at = client_observations.get("observation_started_at")
        observations = client_observations.get("observations") or []
    else:
        # Compatibility for callers/tests that still provide flat legacy rows.
        observations = []
        grouped = {}
        for item in client_observations or []:
            if not isinstance(item, dict):
                continue
            client = str(item.get("client") or "").strip().lower()
            client_key = "qbittorrent" if client in {"qbit", "qbittorrent"} else "sabnzbd" if client in {"sab", "sabnzbd"} else ""
            if client_key:
                grouped.setdefault(client_key, []).append(item)
        for client_key, items in grouped.items():
            errors = [item for item in items if item.get("client_state") == "client_unavailable"]
            observations.append(
                {
                    "client": client_key,
                    "authoritative": not errors,
                    "items": items,
                    "errors": errors,
                }
            )
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        client_key = str(observation.get("client") or "").strip().lower()
        if client_key in {"qbit", "qbittorrent"}:
            client_key = "qbittorrent"
        elif client_key in {"sab", "sabnzbd"}:
            client_key = "sabnzbd"
        else:
            continue
        if requested_clients and client_key not in requested_clients:
            continue
        errors = list(observation.get("errors") or [])
        if errors or not observation.get("authoritative"):
            client_errors_out.extend(
                {key: item.get(key) for key in ("client", "error")}
                for item in errors
                if isinstance(item, dict)
            )
            continue
        available_clients.add(client_key)
        for item in observation.get("items") or []:
            if not isinstance(item, dict) or item.get("client_state") == "client_unavailable":
                continue
            snapshot = download_client_reconcile_snapshot(item)
            if snapshot:
                snapshots.append(snapshot)
    return sorted(available_clients), snapshots, client_errors_out, observation_started_at


def reconcile_inkdrop_download_clients(dry_run=False, client_filter=None):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_unavailable", "changed": 0}
    if not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "inkdrop_state_db_missing", "changed": 0}
    requested_clients = {
        "qbittorrent" if str(client or "").strip().lower() in {"qbit", "qbittorrent"} else "sabnzbd"
        for client in (client_filter or [])
        if str(client or "").strip().lower() in {"qbit", "qbittorrent", "sab", "sabnzbd"}
    }
    observation_started_at = now()
    client_observations = collect_download_client_reconcile_observations(
        requested_clients=requested_clients,
        observation_started_at=observation_started_at,
    )
    available_clients, snapshots, client_errors_out, observation_started_at = download_client_reconcile_inputs(
        client_observations,
        requested_clients=requested_clients,
    )
    result = {
        "ok": True,
        "dry_run": bool(dry_run),
        "available_clients": available_clients,
        "client_filter": sorted(requested_clients),
        "snapshot_count": len(snapshots),
        "client_errors": client_errors_out,
        "observation_started_at": observation_started_at,
        "changed": 0,
    }
    if dry_run:
        result["sample_snapshots"] = [
            {key: snap.get(key) for key in ("client", "status", "title", "external_id", "nzo_id", "local_path")}
            for snap in snapshots[:12]
        ]
        return result
    if not available_clients:
        local_pack_fanout = fanout_local_completed_packs_to_inkdrop()
        result["local_pack_fanout"] = local_pack_fanout
        if isinstance(local_pack_fanout, dict):
            result["changed"] = int(result.get("changed") or 0) + int(local_pack_fanout.get("created") or 0) + int(local_pack_fanout.get("updated") or 0)
        result.update(
            {
                "ok": bool(int(result.get("changed") or 0)),
                "reason": "download_clients_unavailable_local_pack_replay_ran"
                if int(result.get("changed") or 0)
                else "download_clients_unavailable",
            }
        )
        return result
    try:
        summary = inkdrop_state.reconcile_source_wait_download_clients(
            INKDROP_STATE_DB,
            snapshots,
            available_clients=available_clients,
            observation_started_at=observation_started_at,
            attempts=DOWNLOAD_CLIENT_RECONCILE_ATTEMPTS,
            timeout_seconds=DOWNLOAD_CLIENT_RECONCILE_WRITE_TIMEOUT_SECONDS,
            busy_timeout_ms=DOWNLOAD_CLIENT_RECONCILE_WRITE_BUSY_TIMEOUT_MS,
        )
    except Exception as exc:  # noqa: BLE001 - worker status should show the lock/API failure
        message = f"{type(exc).__name__}: {exc}"
        result.update(
            {
                "ok": False,
                "reason": "state_db_locked" if "database is locked" in str(exc).lower() else "download_client_reconcile_failed",
                "error": message,
                "changed": 0,
            }
        )
        return result
    pack_fanout = fanout_completed_pack_snapshots_to_inkdrop(snapshots)
    local_pack_fanout = fanout_local_completed_packs_to_inkdrop()
    result.update(summary if isinstance(summary, dict) else {"summary": summary})
    result["pack_fanout"] = pack_fanout
    result["local_pack_fanout"] = local_pack_fanout
    if isinstance(pack_fanout, dict):
        result["changed"] = int(result.get("changed") or 0) + int(pack_fanout.get("created") or 0) + int(pack_fanout.get("updated") or 0)
    if isinstance(local_pack_fanout, dict):
        result["changed"] = int(result.get("changed") or 0) + int(local_pack_fanout.get("created") or 0) + int(local_pack_fanout.get("updated") or 0)
    return result


def _sha256_of_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _series_folder_key(name):
    """Loose key for matching a series folder across a rename.

    "Chew" and "Chew (2009)" are the same series; so are "Hunter X Hunter" and
    "Hunter X Hunter (2005)". Dropping a trailing year and normalising leaves
    the two forms equal.
    """

    text = re.sub(r"\s*\((?:18|19|20)\d{2}\)\s*$", "", str(name or "").strip())
    return re.sub(r"[^a-z0-9]+", "", text.lower())


class _LibraryLookup:
    """Find a file by name, searching only the series folder it belongs to.

    Walking the whole library for every repair run is far too slow on a pooled
    mount -- it did not finish in fourteen minutes here. A moved file almost
    always stays inside its own series, and a reorganisation renames the series
    folder rather than scattering its contents, so the search is scoped to the
    folders whose name matches the one recorded in the stale path.
    """

    def __init__(self, roots):
        self._roots = [Path(r) for r in roots if r]
        self._series_dirs = None
        self._scanned = {}

    def _series_index(self):
        if self._series_dirs is None:
            self._series_dirs = {}
            for root in self._roots:
                try:
                    entries = list(os.scandir(root))
                except OSError:
                    continue
                for entry in entries:
                    if entry.is_dir():
                        self._series_dirs.setdefault(_series_folder_key(entry.name), []).append(entry.path)
        return self._series_dirs

    def _files_in(self, directory):
        if directory not in self._scanned:
            found = {}
            for dirpath, _dirnames, filenames in os.walk(directory):
                for name in filenames:
                    if Path(name).suffix.lower() in ARCHIVE_SUFFIXES:
                        found.setdefault(name, []).append(os.path.join(dirpath, name))
            self._scanned[directory] = found
        return self._scanned[directory]

    def candidates(self, stale_dest):
        """Paths that share a basename with the stale destination."""

        basename = os.path.basename(stale_dest)
        # The series folder is the first path segment under a library root.
        series_hint = ""
        for root in self._roots:
            try:
                rel = Path(stale_dest).relative_to(root)
            except ValueError:
                continue
            series_hint = rel.parts[0] if rel.parts else ""
            break
        hits = []
        for directory in self._series_index().get(_series_folder_key(series_hint), []):
            hits.extend(self._files_in(directory).get(basename, []))
        return hits


def repair_imported_file_destinations(limit=None, dry_run=True, roots=None, now=None):
    """Point imported_files.dest back at where the file actually lives now.

    dest is written once, at import, and nothing has ever updated it. A library
    reorganisation therefore strands the row: the unit is still imported, but
    every reader that checks the recorded path concludes it is not, and a
    download task can sit at import_ready forever behind that.

    Repair is by sha256, which is the table's own primary key, so a row is only
    rewritten when the bytes at the new path are confirmed to be the same
    bytes. A row whose file genuinely is not in the library any more is
    reported as missing and left exactly as it is -- that is a different
    problem and must not be papered over with a path that does not exist.
    """

    summary = {
        "checked": 0,
        "resolved": 0,
        "repaired": 0,
        "missing": 0,
        "ambiguous": 0,
        "dry_run": bool(dry_run),
        "repaired_samples": [],
        "missing_samples": [],
        "ambiguous_samples": [],
    }
    if not DB_PATH.exists():
        summary["skipped"] = "imported_files_db_missing"
        return summary
    if roots is None:
        roots = [
            getattr(imp, "COMIC_ROOT", None),
            getattr(imp, "MANGA_ROOT", None),
        ]
    roots = [r for r in roots if r]

    conn = connect_db()
    try:
        rows = conn.execute(
            "select sha256, dest, size from imported_files"
            " where dest is not null and trim(dest) <> ''"
        ).fetchall()
    except Exception:
        conn.close()
        raise

    stale = []
    for row in rows:
        dest = str(row[1] or "")
        if not dest:
            continue
        summary["checked"] += 1
        if os.path.isfile(dest):
            summary["resolved"] += 1
            continue
        stale.append((str(row[0] or ""), dest, row[2]))
    if not stale:
        conn.close()
        return summary
    if limit:
        stale = stale[: int(limit)]

    lookup = _LibraryLookup(roots)
    repairs = []
    for sha, dest, size in stale:
        candidates = lookup.candidates(dest)
        # Size is a cheap first filter; sha256 is what actually decides.
        sized = [c for c in candidates if size is None or _safe_size(c) == size]
        confirmed = []
        for candidate in sized or candidates:
            if not sha:
                break
            try:
                if _sha256_of_file(candidate) == sha:
                    confirmed.append(candidate)
                    break
            except OSError:
                continue
        if not confirmed:
            summary["missing"] += 1
            if len(summary["missing_samples"]) < 8:
                summary["missing_samples"].append(dest)
            continue
        if len(confirmed) > 1:
            summary["ambiguous"] += 1
            if len(summary["ambiguous_samples"]) < 8:
                summary["ambiguous_samples"].append(dest)
            continue
        repairs.append((confirmed[0], sha, dest))

    try:
        if repairs and not dry_run:
            conn.executemany(
                "update imported_files set dest=? where sha256=?",
                [(new, sha) for new, sha, _old in repairs],
            )
            conn.commit()
    finally:
        conn.close()
    summary["repaired"] = len(repairs)
    summary["repaired_samples"] = [
        {"sha256": sha[:12], "from": old, "to": new} for new, sha, old in repairs[:8]
    ]
    return summary


def _safe_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def load_imported_state():
    state = {
        "source_paths": set(),
        "dest_paths": set(),
        "hashes": set(),
        "hash_sizes": set(),
        "hash_to_imported": {},
        "manga_units": set(),
        "manga": set(),
        "manga_coverage": set(),
        "collections": set(),
        "source_to_dest": {},
        "dest_to_source": {},
    }
    if not DB_PATH.exists():
        return state
    conn = connect_db()
    try:
        imported_columns = {
            row[1]
            for row in conn.execute("pragma table_info(imported_files)").fetchall()
        }
        source_col = "source_path" if "source_path" in imported_columns else "source"
        dest_col = "dest_path" if "dest_path" in imported_columns else "dest"
        for source, dest, sha, size in conn.execute(f"select {source_col}, {dest_col}, sha256, size from imported_files"):
            if source:
                source_text = str(source)
                state["source_paths"].add(source_text)
            if dest:
                dest_text = str(dest)
                state["dest_paths"].add(dest_text)
            if source and dest:
                state["source_to_dest"][str(source)] = str(dest)
                state["dest_to_source"][str(dest)] = str(source)
            if sha:
                sha_text = str(sha)
                state["hashes"].add(sha_text)
                state["hash_to_imported"][sha_text] = {
                    "source": str(source or ""),
                    "dest": str(dest or ""),
                    "size": int(size or 0),
                }
            if size:
                state["hash_sizes"].add(int(size))
        for table, key in (("manga_completion", "manga"), ("manga_unit_completion", "manga_units"), ("manga_coverage", "manga_coverage"), ("collection_completion", "collections")):
            try:
                rows = conn.execute(f"select normalized_series, normalized_number, verification_status from {table}").fetchall()
            except sqlite3.Error:
                continue
            for series, number, status in rows:
                if str(status or "").strip().lower() in IMPORT_VERIFIED_STATUSES:
                    state[key].add((series, number))
    finally:
        conn.close()
    return state


def find_client_match(record, client_items):
    record_nzo_ids = set()
    for key in ("nzo_id", "nzoid", "client_id"):
        value = record.get(key)
        if value:
            record_nzo_ids.add(str(value))
    for value in record.get("nzo_ids") or []:
        if value:
            record_nzo_ids.add(str(value))
    if record_nzo_ids:
        for item in client_items:
            if item.get("client") == "sab" and str(item.get("nzo_id") or "") in record_nzo_ids:
                return item
    record_client_id = str(record.get("client_id") or "")
    if record_client_id:
        for item in client_items:
            if item.get("client") == "qbit" and str(item.get("hash") or "") == record_client_id:
                return item
    candidates = [norm(record.get("title")), norm(record.get("query"))]
    for item in client_items:
        item_norm = item.get("normalized") or norm(item.get("name"))
        if not item_norm:
            continue
        for candidate in candidates:
            if candidate and (candidate in item_norm or item_norm in candidate):
                return item
    return None


def client_errors(client_items):
    return [item for item in client_items if item.get("client_state") == "client_unavailable"]


def stopped_client_retryable(record, client):
    if client.get("client_state") not in {"stopped_downloading", "stalled_downloading"}:
        return False
    try:
        progress = float(client.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0.0
    if progress > 0.001:
        return False
    try:
        first_sent_at = float(record.get("first_sent_at") or 0)
    except (TypeError, ValueError):
        first_sent_at = 0
    if first_sent_at <= 0:
        return False
    return now() - first_sent_at >= QBIT_NO_PROGRESS_RETRY_SECONDS


def find_local_matches(record, files, pending_records):
    return [path for path in files if imp.matches_pending_import(path, [record])]


def is_archive_path(path):
    lower = path.name.lower()
    return path.suffix.lower() in ARCHIVE_SUFFIXES or lower.endswith(".cbz.zip")


def is_internal_segment_path(path):
    return any(part.startswith("_") for part in path.parts)


def record_local_archive_paths(record):
    paths = []
    seen = set()
    for key in LOCAL_PATH_KEYS:
        value = record.get(key)
        if not value:
            continue
        path = Path(value)
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            if not (path.exists() and path.is_file() and is_archive_path(path)):
                continue
        except OSError:
            continue
        if is_internal_segment_path(path):
            continue
        paths.append(path)
    return paths


def archive_paths_for_completed_client_path(path_value, limit=250, download_client_instance_id=None):
    root = download_client_host_path(path_value, download_client_instance_id=download_client_instance_id)
    if not root:
        return []
    try:
        if root.exists() and root.is_file() and is_archive_path(root) and not is_internal_segment_path(root):
            return [root]
    except OSError:
        return []
    try:
        if not root.exists() or not root.is_dir():
            return []
    except OSError:
        return []
    cached = cached_completed_pack_archives(root, limit)
    if cached is not None:
        return cached
    paths = []
    archive_files = bounded_archive_files([root], timeout_seconds=5)
    max_paths = int(limit or 250)
    for path in archive_files:
        if len(paths) >= max_paths:
            break
        if is_archive_path(path) and not is_internal_segment_path(path):
            paths.append(str(path))
    store_completed_pack_archives(root, paths, max_paths, truncated=len(archive_files) > len(paths))
    return paths


def completed_client_authorized_roots(download_client_instance_id=None):
    roots = list(DOWNLOAD_STAGING_ROOTS)
    roots.extend(host_root for _remote, host_root in configured_remote_path_mappings())
    if download_client_instance_id:
        try:
            roots.extend(
                host_root
                for _remote, host_root in remote_path_mappings_from_value(
                    inkdrop_download_client_routing.instance_path_mappings(
                        INKDROP_STATE_DB,
                        download_client_instance_id,
                    )
                )
            )
        except Exception:
            pass
    roots.extend(host_root for _remote, host_root in QBIT_DOWNLOAD_PATH_MAP)
    out = []
    seen = set()
    for root in roots:
        root_text = str(root or "").strip()
        if not root_text:
            continue
        root = Path(root_text)
        try:
            key = str(root.resolve())
        except (OSError, RuntimeError):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def completed_client_family_name_matches(path, row):
    candidate = norm(Path(path).name)
    title = norm((row or {}).get("task_title") or (row or {}).get("title"))
    return bool(candidate and title and candidate == title)


def completed_client_explicit_unit_candidates(path):
    stem = Path(str(path or "")).stem
    if not stem:
        return set()
    cleaned = re.sub(r"\(\s*(?:v|ver|version)\s*\d+\s*\)", " ", stem, flags=re.IGNORECASE)
    if re.search(
        r"\b(?:v|vol(?:ume)?)\s*0*\d+\s*[-–—]\s*(?:v|vol(?:ume)?)?\s*0*\d+\b",
        cleaned,
        re.IGNORECASE,
    ):
        return set()
    patterns = (
        r"\b(?:v|vol(?:ume)?)\s*0*(\d+(?:\.\d+)?)\b",
        r"(?:#|\bissue\s+|\bno\.?\s+)0*(\d+(?:\.\d+)?)\b",
    )
    candidates = set()
    for pattern in patterns:
        for match in re.finditer(pattern, cleaned, flags=re.IGNORECASE):
            normalized = normalize_issue_number(match.group(1))
            if normalized:
                candidates.add(normalized)
    return candidates


def bounded_completed_family_archives(roots, *, deadline, max_files):
    files = []
    stack = [Path(root) for root in reversed(list(roots or []))]
    while stack and len(files) < max_files and time.monotonic() < deadline:
        current = stack.pop()
        try:
            if current.is_file():
                if is_archive_path(current) and not is_internal_segment_path(current):
                    files.append(current)
                continue
            elif current.is_dir():
                with os.scandir(current) as scanned:
                    for entry in scanned:
                        if len(files) >= max_files or time.monotonic() >= deadline:
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                path = Path(entry.path)
                                if is_archive_path(path) and not is_internal_segment_path(path):
                                    files.append(path)
                        except OSError:
                            continue
        except OSError:
            continue
    return files


def completed_client_nested_archive_paths(row, limit=250, authorized_roots=None):
    """Find a completed pack member beside a missing client-reported path.

    The lookup never escapes configured client/staging roots and only descends
    into a sibling whose name matches the exact completed candidate family.
    """
    row = row if isinstance(row, dict) else {}
    instance_id = row.get("download_client_instance_id")
    roots = list(authorized_roots or completed_client_authorized_roots(instance_id))
    if not roots or not str(row.get("task_title") or "").strip():
        return []
    deadline = time.monotonic() + 5.0
    scopes = []
    seen_scopes = set()
    for value in (row.get("local_path"), row.get("save_path")):
        if time.monotonic() >= deadline:
            break
        if not str(value or "").strip():
            continue
        candidate = download_client_host_path(value, download_client_instance_id=instance_id)
        if not candidate or not any(path_under_root(candidate, root) for root in roots):
            continue
        candidate = Path(candidate)
        try:
            if candidate.exists() and candidate.is_file():
                candidate = candidate.parent
        except OSError:
            continue
        while candidate.parent != candidate:
            try:
                if candidate.exists():
                    break
            except OSError:
                candidate = None
                break
            parent = candidate.parent
            if not any(path_under_root(parent, root) for root in roots):
                break
            candidate = parent
        if candidate is None:
            continue
        try:
            if not candidate.exists() or not candidate.is_dir():
                continue
        except OSError:
            continue
        key = str(candidate.resolve())
        if key not in seen_scopes:
            seen_scopes.add(key)
            scopes.append(candidate)

    family_roots = []
    seen_family = set()
    for scope in scopes[:4]:
        if time.monotonic() >= deadline:
            break
        candidates = [scope] if completed_client_family_name_matches(scope, row) else []
        try:
            for index, candidate in enumerate(scope.iterdir()):
                if index >= 1000 or time.monotonic() >= deadline:
                    break
                candidates.append(candidate)
        except OSError:
            continue
        for candidate in candidates:
            if not completed_client_family_name_matches(candidate, row):
                continue
            if not any(path_under_root(candidate, root) for root in roots):
                continue
            try:
                key = str(candidate.resolve())
            except (OSError, RuntimeError):
                continue
            if key in seen_family:
                continue
            seen_family.add(key)
            family_roots.append(candidate)

    paths = []
    seen_paths = set()
    max_paths = max(1, int(limit or 250))
    candidates = bounded_completed_family_archives(
        family_roots[:8],
        deadline=deadline,
        max_files=max_paths,
    )
    for path in candidates:
        path = Path(path)
        family_root = next((root for root in family_roots if path_under_root(path, root)), None)
        if family_root is None or not any(path_under_root(path, root) for root in roots):
            continue
        try:
            if not path.exists() or not path.is_file():
                continue
            key = str(path.resolve())
        except (OSError, RuntimeError):
            continue
        if key in seen_paths:
            continue
        seen_paths.add(key)
        paths.append(str(path))
        if len(paths) >= max_paths:
            return paths
    return paths


def completed_client_nested_archive_resolution(row, limit=250, authorized_roots=None):
    paths = completed_client_nested_archive_paths(row, limit=limit, authorized_roots=authorized_roots)
    issue_number = (row or {}).get("issue_number") or (row or {}).get("normalized_number")
    wanted = normalize_issue_number(issue_number)
    matching = [path for path in paths if wanted and wanted in completed_client_explicit_unit_candidates(path)]
    if matching:
        return {"state": "found", "paths": paths, "matching_paths": matching}
    if paths:
        return {
            "state": "false_positive",
            "reason": "completed_client_nested_archive_wrong_unit",
            "paths": paths,
            "matching_paths": [],
        }
    return {
        "state": "stale_no_local_file",
        "reason": "completed_client_path_missing_archive",
        "paths": [],
        "matching_paths": [],
    }


def archive_matches_issue(path, issue_number, *, filename_only=False):
    wanted = normalize_issue_number(issue_number)
    if not wanted:
        return True
    candidates = set()
    if not filename_only:
        try:
            _unit, unit_number = imp.manga_file_unit_and_number(path)
            if unit_number:
                candidates.add(normalize_issue_number(unit_number))
        except Exception:
            pass
    try:
        if filename_only:
            candidates.update(filename_issue_number_candidates(path))
        else:
            extracted = extract_issue_number_from_path(path)
            if extracted:
                candidates.add(normalize_issue_number(extracted))
            candidates.update(filename_issue_number_candidates(path))
    except Exception:
        pass
    return wanted in candidates


def archive_is_cover_only(path):
    text = norm(Path(str(path or "")).stem)
    return bool(
        re.search(r"\bcover\s+only\b", text)
        or re.search(r"\bvariant\s+cover\s+only\b", text)
        or re.search(r"\bcover\s+only\s+digital\b", text)
    )


def archive_is_single_file_range_or_collection(path):
    stem = Path(str(path or "")).stem
    text = norm(stem)
    if re.search(r"(?<!\d)\d{1,5}(?:\.\d+)?\s*[-–]\s*\d{1,5}(?:\.\d+)?(?!\d)", stem):
        return True
    return bool(re.search(r"\b(?:omnibus|compendium|collection|complete|collected|library edition)\b", text))


def imported_file_identity_match(row, imported_row):
    row = row if isinstance(row, dict) else {}
    imported_row = imported_row if isinstance(imported_row, dict) else {}
    series_key = norm(row.get("matched_series") or row.get("series_title"))
    issue_number = row.get("trusted_issue") or row.get("issue_number") or row.get("normalized_number")
    if not series_key:
        return False, "missing_series_identity"
    if not issue_number:
        return False, "missing_issue_identity"
    path_values = [
        imported_row.get("dest"),
        imported_row.get("source"),
        row.get("matched_local_path"),
    ]
    checked = 0
    for value in path_values:
        text = str(value or "").strip()
        if not text:
            continue
        checked += 1
        try:
            path = Path(text)
            path_text = " ".join(
                str(bit)
                for bit in (
                    path.name,
                    path.parent.name,
                    path.parent.parent.name if path.parent != path.parent.parent else "",
                    text,
                )
                if bit
            )
        except (TypeError, ValueError):
            path = text
            path_text = text
        if norm_contains_phrase(norm(path_text), series_key) and archive_matches_issue(path, issue_number, filename_only=True):
            if _edition_year_conflicts(row, path_text):
                return False, "edition_year_mismatch"
            return True, "series_issue_path_match"
        if norm_contains_phrase(norm(path_text), series_key) and manga_volume_identity_match(row, path):
            if _edition_year_conflicts(row, path_text):
                return False, "edition_year_mismatch"
            return True, "series_volume_path_match"
    return False, "imported_file_identity_mismatch" if checked else "missing_imported_file_path"


def _edition_year_conflicts(row, path_text):
    """True when the row's series year and the file's year token disagree.

    Title + issue number alone cannot tell two editions apart: 'Same Title
    (2000)' and 'Same Title (2020)' both pass the phrase and issue checks, so
    whichever queue row processed first claimed the other edition's file
    (Pass 18's re-audit of PASS4-ACQ-01). The year is the edition
    discriminator this codebase already stamps into both series metadata and
    canonical filenames. Absent either side, nothing changes -- this only
    refuses a POSITIVE year contradiction, with slack for cover-date drift.
    """
    row_year = None
    for key in ("issue_year", "series_year", "year"):
        value = row.get(key) if isinstance(row, dict) else None
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if 1900 <= value <= 2100:
            row_year = value
            break
    if not row_year:
        return False
    file_years = [int(m) for m in re.findall(r"\((19\d{2}|20\d{2})\)", str(path_text or ""))]
    if not file_years:
        return False
    return all(abs(year - row_year) > 2 for year in file_years)


def explicit_volume_number_from_text(*values):
    text = " ".join(str(value or "") for value in values if value not in (None, ""))
    if not text:
        return None
    match = re.search(r"\b(?:volume|vol|v)[\s._-]*0*(\d{1,5}(?:\.\d+)?)\b", text, re.I)
    if not match:
        return None
    return normalize_issue_number(match.group(1))


def manga_volume_identity_match(row, path):
    row = row if isinstance(row, dict) else {}
    truth_model = str(row.get("truth_model") or "").strip().lower()
    unit_model = str(row.get("unit_model") or "").strip().lower()
    if truth_model and truth_model != "kavita_manga":
        return False
    volume_number = explicit_volume_number_from_text(
        row.get("query"),
        row.get("title"),
        row.get("pending_key"),
    )
    if not volume_number:
        return False
    try:
        unit, number = imp.manga_file_unit_and_number(path)
    except Exception:
        unit = ""
        number = explicit_volume_number_from_text(Path(str(path or "")).name)
    unit = str(unit or "").strip().lower()
    if unit_model and unit_model not in {"volume", "pack", "mixed_volume_preferred", "mixed_chapter_preferred"}:
        return False
    path_name = Path(str(path or "")).name
    filename_volume = explicit_volume_number_from_text(path_name)
    filename_has_chapter = bool(re.search(r"\b(?:chapter|ch)[\s._-]*0*\d{1,5}(?:\.\d+)?\b", path_name, re.I))
    if filename_volume == volume_number and not filename_has_chapter:
        return True
    if unit not in {"volume", "pack"}:
        return False
    return normalize_issue_number(number) == volume_number


def inkdrop_queue_identity_row(queue_id):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {}
    queue_id = str(queue_id or "").strip()
    if not queue_id:
        return {}
    try:
        with inkdrop_state.connect_read(
            INKDROP_STATE_DB,
            timeout_seconds=INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS,
            busy_timeout_ms=INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS,
        ) as con:
            row = con.execute(
                """
                select q.id as queue_id,
                       s.id as series_id,
                       s.title as matched_series,
                       s.media_type as series_media_type,
                       s.year as series_year,
                       s.publisher as series_publisher,
                       s.metadata_provider as series_metadata_provider,
                       s.metadata_id as series_metadata_id,
                       s.source as series_source,
                       s.library_root as series_library_root,
                       s.library_path as series_library_path,
                       s.library_adapter_path as series_library_adapter_path,
                       coalesce(nullif(i.issue_number, ''), nullif(i.normalized_number, '')) as trusted_issue,
                       i.title as issue_title,
                       i.id as trusted_issue_id,
                       i.issue_number,
                       i.normalized_number,
                       i.release_date as issue_release_date,
                       i.metadata_provider as issue_metadata_provider,
                       i.metadata_id as issue_metadata_id
                from queue_items q
                join series s on s.id=q.series_id
                left join issues i on i.id=q.issue_id
                where q.id=?
                limit 1
                """,
                (queue_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    return dict(row)


def trusted_target_for_inkdrop_row(row, targets):
    series_id = str((row or {}).get("series_id") or "").strip()
    if not series_id:
        series_id = str((row or {}).get("trusted_series_id") or "").strip()
    if not series_id:
        return None
    target = imp.trusted_comic_target(targets, native_series_id=series_id)
    return target or inkdrop_queue_row_target(row, series_id)


def inkdrop_queue_row_target(row, series_id=None):
    row = row if isinstance(row, dict) else {}
    series_id = str(series_id or row.get("series_id") or row.get("trusted_series_id") or "").strip()
    title = str(row.get("series_title") or row.get("matched_series") or "").strip()
    if not series_id or not title:
        return None
    metadata_provider = str(row.get("series_metadata_provider") or "").strip().lower()
    metadata_id = str(row.get("series_metadata_id") or "").strip()
    if (not metadata_provider or not metadata_id) and ":" in series_id:
        provider, ident = series_id.split(":", 1)
        metadata_provider = metadata_provider or provider.strip().lower()
        metadata_id = metadata_id or ident.strip()
    aliases = []
    alias_values = [title]
    try:
        alias_values.extend(imp.leading_article_aliases(title))
    except Exception:
        pass
    for value in alias_values:
        try:
            alias = imp.normalize(value)
        except Exception:
            alias = norm(value)
        if alias and alias not in aliases:
            aliases.append(alias)
    if not aliases:
        return None
    folder = str(
        row.get("series_library_path")
        or row.get("library_path")
        or row.get("series_library_adapter_path")
        or row.get("library_adapter_path")
        or ""
    ).strip()
    if not folder:
        if inkdrop_state is not None:
            key = inkdrop_state.stable_id("inkdrop_queue_row_target", series_id, title)
        else:
            key = norm(f"{series_id} {title}")[:80] or "unknown"
        folder = str(Path("/__inkdrop_unmanaged_target__") / key)
    media_type = str(row.get("series_media_type") or row.get("media_type") or "").strip().lower()
    if not media_type:
        media_type = "manga" if metadata_provider == "mangadex" or imp.is_manga_target({"title": title}) else "comic"
    return {
        "id": None,
        "kapowarr_id": None,
        "inkdrop_series_id": series_id,
        "native_series_id": series_id,
        "title": title,
        "year": row.get("series_year") or row.get("year"),
        "publisher": row.get("series_publisher") or row.get("publisher"),
        "media_type": media_type,
        "folder": folder,
        "library_path": row.get("series_library_path") or row.get("library_path"),
        "library_adapter_path": row.get("series_library_adapter_path") or row.get("library_adapter_path"),
        "metadata_provider": metadata_provider or None,
        "metadata_id": metadata_id or None,
        "native_issue_id": row.get("issue_id") or row.get("trusted_issue_id"),
        "issue_number": row.get("issue_number") or row.get("trusted_issue"),
        "normalized_number": row.get("normalized_number") or row.get("trusted_issue"),
        "issue_title": row.get("issue_title"),
        "special_version": None,
        "target_source": "inkdrop_queue_row",
        "aliases": aliases,
    }


def deambiguous_target(target):
    if not target:
        return None
    copy = dict(target)
    copy.pop("ambiguous_aliases", None)
    copy.pop("ambiguous_alias_targets", None)
    return copy


def _bounded_nested_dicts(value, depth=0):
    if depth > 6 or not isinstance(value, dict):
        return
    yield value
    for key in ("raw", "raw_json", "download_task", "download_task_seed", "candidate", "result"):
        child = value.get(key)
        if isinstance(child, str) and key == "raw_json":
            try:
                child = json.loads(child or "{}")
            except ValueError:
                child = {}
        if isinstance(child, dict):
            yield from _bounded_nested_dicts(child, depth + 1)


def mangadex_volume_page_pack_context(path, row, *, validate_archive=True):
    """Return guarded unit evidence for one queue-owned MangaDex volume pack."""
    row = row if isinstance(row, dict) else {}
    if str(row.get("download_client") or "").strip().lower() != "inkdrop_page_pack":
        return {}
    path = Path(path)
    sidecar_path = path.with_name(path.name + ".source.json")
    try:
        if not sidecar_path.is_file() or sidecar_path.stat().st_size > 1024 * 1024:
            return {}
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(sidecar, dict):
        return {}
    task_id = str(row.get("download_task_id") or row.get("id") or "").strip()
    if not task_id or str(sidecar.get("download_task_id") or "").strip() != task_id:
        return {}
    raw = row.get("task_raw_json") or row.get("raw_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    containers = list(_bounded_nested_dicts(raw if isinstance(raw, dict) else {}))
    containers.append(sidecar)
    volume_pack = any(
        bool(item.get("volume_pack") or item.get("volume_page_pack") or item.get("mangadex_volume_page_pack"))
        for item in containers
    )
    provider_text = " ".join(
        str(value or "").strip().lower()
        for value in (row.get("provider_id"), row.get("source"), sidecar.get("provider_id"), sidecar.get("source"))
    )
    if not volume_pack or "mangadex" not in provider_text:
        return {}
    volume = next((item.get("volume") for item in containers if item.get("volume") not in (None, "")), None)
    volume_number = imp.format_issue_number(volume) if volume not in (None, "") else ""
    source_number = imp.format_issue_number(imp.extract_issue_number(path))
    trusted_issue = row.get("issue_number") or row.get("normalized_number") or row.get("trusted_issue")
    trusted_number = imp.format_issue_number(trusted_issue)
    chapter_numbers = set()
    for item in containers:
        chapters = item.get("chapters") if isinstance(item.get("chapters"), list) else []
        for chapter in chapters:
            chapter_value = chapter.get("chapter") if isinstance(chapter, dict) else chapter
            chapter_number = imp.format_issue_number(chapter_value)
            if chapter_number:
                chapter_numbers.add(chapter_number)
    try:
        declared_count = max(int(item.get("chapter_count") or item.get("volume_pack_chapter_count") or 0) for item in containers)
    except (TypeError, ValueError):
        declared_count = 0
    if not volume_number or source_number != volume_number:
        return {}
    if trusted_number and trusted_number not in chapter_numbers:
        return {}
    if max(declared_count, len(chapter_numbers)) < 2:
        return {}
    if validate_archive:
        try:
            archive = imp.validate_comic_archive(path)
        except Exception:
            return {}
        if not archive.get("ok"):
            return {}
    return {
        "source_unit": "volume",
        "unit_model": "volume",
        "trusted_issue": str(volume),
        "volume": str(volume),
        "covered_trusted_issue": str(trusted_issue or ""),
        "chapter_numbers": sorted(chapter_numbers),
        "chapter_count": max(declared_count, len(chapter_numbers)),
        "download_task_id": task_id,
        "sidecar_path": str(sidecar_path),
        "reason": "mangadex_volume_page_pack_covered_issue",
    }


def classify_inkdrop_client_file(path, row, targets, imported_state, bad_archive_memory):
    path = Path(path)
    trusted_target = trusted_target_for_inkdrop_row(row, targets)
    trusted_issue = row.get("issue_number") or row.get("normalized_number")
    volume_pack_context = mangadex_volume_page_pack_context(path, row)
    classification_issue = volume_pack_context.get("trusted_issue") or trusted_issue
    if trusted_target:
        trusted_target = dict(trusted_target)
        if row.get("issue_title"):
            trusted_target["issue_title"] = row.get("issue_title")
        if classification_issue:
            trusted_target["issue_number"] = classification_issue
            trusted_target["normalized_number"] = classification_issue
        detail = classify_local_file(
            path,
            [deambiguous_target(trusted_target)],
            imported_state,
            validate_archive=False,
            current_missing_keys=None,
            trusted_issue=classification_issue,
            skip_related_subseries=True,
        )
    else:
        detail = classify_local_file(path, targets, imported_state, validate_archive=False, current_missing_keys=None)
    detail = apply_bad_archive_memory(row, detail, path, bad_archive_memory)
    detail["local_path"] = str(path)
    if volume_pack_context and detail.get("state") in {"ready_to_import", "suppressed_completed"}:
        detail.update(
            {
                "trusted_issue": volume_pack_context["trusted_issue"],
                "unit_model": "volume",
                "source_unit": "volume",
                "volume_page_pack_context": volume_pack_context,
                "volume_page_pack_reason": volume_pack_context["reason"],
            }
        )
        if detail.get("state") == "ready_to_import":
            detail["reason"] = volume_pack_context["reason"]
    if trusted_target:
        detail.setdefault("matched_series", trusted_target.get("title"))
        detail.setdefault("matched_kapowarr_volume_id", trusted_target.get("id"))
        detail.setdefault("truth_model", "kavita_manga" if imp.is_manga_target(trusted_target) else "kapowarr_comic")
    return detail


def import_ready_rejection_task_status(state, reason):
    state = str(state or "").strip().lower()
    reason = str(reason or "").strip().lower()
    if state in {"bad_archive", "false_positive", "stale_no_local_file", "wrong_series_or_subseries"}:
        return state
    if reason in {"bad_archive", "false_positive", "stale_no_local_file", "wrong_series_or_subseries"}:
        return reason
    if any(token in reason for token in ("bad_archive", "bad_zip_member", "cbr_extract_failed", "skip_bad_comic_archive")):
        return "bad_archive"
    if "wrong_series_or_subseries" in reason:
        return "wrong_series_or_subseries"
    if "single_part_file_does_not_satisfy_collection_target" in reason:
        return "false_positive"
    return "failed_download"


def import_ready_rejection_message(row, chosen):
    client = inkdrop_reconciliation_client((row or {}).get("download_client"))
    label = "SABnzbd" if client == "sab" else "qBittorrent" if client == "qbit" else client or "Download client"
    reason = str((chosen or {}).get("reason") or (chosen or {}).get("state") or "not_importable").strip()
    return f"{label} completed file was not importable ({reason}); automatic retry scheduled"


def record_inkdrop_import_ready_rejection(row, chosen):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "inkdrop_state_unavailable"}
    row = row if isinstance(row, dict) else {}
    chosen = chosen if isinstance(chosen, dict) else {}
    state = str(chosen.get("state") or "").strip().lower()
    if state not in INKDROP_IMPORT_READY_REJECTION_STATES:
        return {"ok": False, "reason": "state_not_rejection", "state": state}
    queue_id = str(row.get("queue_id") or "").strip()
    task_id = str(row.get("download_task_id") or "").strip()
    if not queue_id:
        return {"ok": False, "reason": "queue_id_missing"}
    ts = now()
    task_status = import_ready_rejection_task_status(state, chosen.get("reason"))
    failure_reason = str(chosen.get("reason") or state or "import_ready_rejected").strip()
    local_path = str(chosen.get("local_path") or row.get("local_path") or "").strip()
    task_title = str(row.get("task_title") or row.get("query") or row.get("series_title") or "").strip()
    message = import_ready_rejection_message(row, chosen)

    def mark_task_failed():
        with inkdrop_state.connect(
            INKDROP_STATE_DB,
            timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS,
            busy_timeout_ms=INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS,
        ) as con:
            inkdrop_state.init_schema(con)
            rows = con.execute(
                """
                select id, source_attempt_id, raw_json
                from download_tasks
                where queue_id=?
                  and state in ('queued','downloading','import_ready','importing')
                  and (
                        id=?
                        or (
                          ? <> ''
                          and lower(coalesce(title,'')) = lower(?)
                        )
                      )
                """,
                (queue_id, task_id, task_title, task_title),
            ).fetchall()
            updated = 0
            for task in rows:
                raw = {}
                try:
                    raw = json.loads(task["raw_json"] or "{}")
                except (TypeError, ValueError):
                    raw = {}
                if not isinstance(raw, dict):
                    raw = {}
                raw.update(
                    {
                        "import_ready_rejected": True,
                        "import_ready_rejected_at": ts,
                        "import_ready_rejected_reason": failure_reason,
                        "import_ready_rejected_state": state,
                        "import_ready_rejected_path": local_path,
                    }
                )
                outcome = inkdrop_state.download_task_outcome("failed", task_status, "failed_candidate")
                display_phase = inkdrop_state.download_task_display_phase("failed", task_status, "failed_candidate", outcome)
                con.execute(
                    """
                    update download_tasks
                       set status=?,
                           state='failed',
                           lifecycle_phase='failed_candidate',
                           failure_reason=?,
                           retry_eligible=1,
                           updated_at=?,
                           completed_at=coalesce(completed_at, ?),
                           outcome=?,
                           display_phase=?,
                           raw_json=?
                     where id=?
                    """,
                    (
                        task_status,
                        failure_reason,
                        ts,
                        ts,
                        outcome,
                        display_phase,
                        json.dumps(raw, sort_keys=True),
                        task["id"],
                    ),
                )
                if task["source_attempt_id"]:
                    attempt_raw_row = con.execute(
                        "select raw_json from source_attempts where id=?",
                        (task["source_attempt_id"],),
                    ).fetchone()
                    attempt_raw = {}
                    try:
                        attempt_raw = json.loads((attempt_raw_row or {})["raw_json"] or "{}") if attempt_raw_row else {}
                    except (TypeError, ValueError):
                        attempt_raw = {}
                    attempt_raw = attempt_raw if isinstance(attempt_raw, dict) else {}
                    attempt_raw.update(
                        {
                            "import_ready_rejected": True,
                            "import_ready_rejected_at": ts,
                            "import_ready_rejected_reason": failure_reason,
                            "import_ready_rejected_path": local_path,
                        }
                    )
                    con.execute(
                        """
                        update source_attempts
                           set status=?, lifecycle_phase='failed_candidate', outcome='failed',
                               display_phase='retry_later', failure_reason=?, retry_eligible=1,
                               completed_at=coalesce(completed_at, ?), raw_json=?
                         where id=?
                        """,
                        (task_status, failure_reason, ts, json.dumps(attempt_raw, sort_keys=True), task["source_attempt_id"]),
                    )
                updated += 1
            con.commit()
            return updated

    try:
        task_updates = with_sqlite_lock_retry(mark_task_failed)
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        return {"ok": False, "reason": "state_db_locked", "error": str(exc)}

    client = inkdrop_reconciliation_client(row.get("download_client"))
    attempt = {
        "source": "download_client",
        "provider": client or "download_client",
        "protocol": row.get("protocol"),
        "download_client": client or "download_client",
        "status": "retry_scheduled",
        "reason": message,
        "failure_reason": failure_reason,
        "retry_eligible": True,
        "title": task_title,
        "source_path": local_path,
        "matched_local_path": local_path,
        "lifecycle_state": state,
        "kind": "import_ready_rejection",
        "ts": ts,
    }
    try:
        attempt_result = with_sqlite_lock_retry(
            lambda: inkdrop_state.record_queue_source_attempt(
                INKDROP_STATE_DB,
                queue_id,
                attempt,
                attempt_id=f"import-ready-reject-{queue_id}-{task_id or 'task'}-{int(ts)}",
                started_at=ts,
                completed_at=ts,
            )
        )
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        return {"ok": False, "reason": "state_db_locked", "error": str(exc), "task_updates": task_updates}
    return {
        "ok": bool(isinstance(attempt_result, dict) and attempt_result.get("ok")),
        "queue_id": queue_id,
        "download_task_id": task_id,
        "state": state,
        "task_status": task_status,
        "task_updates": task_updates,
        "attempt": attempt_result,
    }


def record_inkdrop_import_ready_deferral(queue_id, task_id, reason, source_path=None, client=None):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "inkdrop_state_unavailable"}
    queue_id = str(queue_id or "").strip()
    task_id = str(task_id or "").strip()
    reason = str(reason or "import_ready_deferred").strip()
    if not queue_id or not task_id:
        return {"ok": False, "reason": "missing_queue_or_task_id"}
    ts = now()
    message = "Source file is not ready yet; import will retry automatically"
    if reason == "source_file_incomplete_qbit_download":
        message = "qBittorrent still reports the pack file incomplete; import will retry automatically"

    def mark_task_deferred():
        with inkdrop_state.connect(
            INKDROP_STATE_DB,
            timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS,
            busy_timeout_ms=INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS,
        ) as con:
            inkdrop_state.init_schema(con)
            row = con.execute("select raw_json from download_tasks where id=?", (task_id,)).fetchone()
            raw = {}
            if row:
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                except (TypeError, ValueError):
                    raw = {}
            if not isinstance(raw, dict):
                raw = {}
            raw.update(
                {
                    "import_ready_deferred": True,
                    "import_ready_deferred_at": ts,
                    "import_ready_deferred_reason": reason,
                    "import_ready_deferred_path": str(source_path or ""),
                    "import_ready_deferred_client": str(client or ""),
                }
            )
            outcome = inkdrop_state.download_task_outcome("downloading", "waiting_for_complete_source", "downloading")
            display_phase = inkdrop_state.download_task_display_phase("downloading", "waiting_for_complete_source", "downloading", outcome)
            task_cur = con.execute(
                """
                update download_tasks
                   set status='waiting_for_complete_source',
                       state='downloading',
                       lifecycle_phase='downloading',
                       failure_reason=?,
                       retry_eligible=1,
                       updated_at=?,
                       outcome=?,
                       display_phase=?,
                       raw_json=?
                 where id=?
                """,
                (reason, ts, outcome, display_phase, json.dumps(raw, sort_keys=True), task_id),
            )
            queue_cur = con.execute(
                """
                update queue_items
                   set state='downloading',
                       display_phase='downloading',
                       current_source=coalesce(nullif(current_source, ''), 'download_client'),
                       last_event=?,
                       updated_at=?
                 where id=?
                   and active=1
                """,
                (message, ts, queue_id),
            )
            con.commit()
            return {
                "ok": True,
                "queue_id": queue_id,
                "download_task_id": task_id,
                "reason": reason,
                "queue_updated": int(queue_cur.rowcount or 0),
                "task_updated": int(task_cur.rowcount or 0),
            }

    try:
        return with_sqlite_lock_retry(mark_task_deferred)
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        return {"ok": False, "reason": "state_db_locked", "error": str(exc)}


def record_inkdrop_import_ready_promotion(
    queue_id,
    task_id,
    source_path=None,
    client=None,
    *,
    promotion_reason=None,
    message=None,
):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "inkdrop_state_unavailable"}
    queue_id = str(queue_id or "").strip()
    task_id = str(task_id or "").strip()
    if not queue_id or not task_id:
        return {"ok": False, "reason": "missing_queue_or_task_id"}
    ts = now()
    source_text = str(source_path or "").strip()
    client_text = str(client or "").strip()
    reason = str(promotion_reason or "source_file_completed_after_incomplete_wait").strip()
    message = str(message or "Pack file is complete; import worker will scan it automatically").strip()

    def mark_task_ready():
        with inkdrop_state.connect(
            INKDROP_STATE_DB,
            timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS,
            busy_timeout_ms=INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS,
        ) as con:
            inkdrop_state.init_schema(con)
            row = con.execute(
                "select queue_id, state, status, retry_eligible, raw_json from download_tasks where id=?",
                (task_id,),
            ).fetchone()
            if not row or str(row["queue_id"] or "").strip() != queue_id:
                return {"ok": False, "reason": "download_task_not_found"}
            raw = {}
            if row:
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                except (TypeError, ValueError):
                    raw = {}
            if not isinstance(raw, dict):
                raw = {}
            if raw.get("artifact_retry_blocked"):
                return {"ok": False, "reason": "terminal_artifact_retry_blocked"}
            expected_state = str(row["state"] or "").strip().lower()
            expected_status = str(row["status"] or "").strip().lower()
            retry_eligible = int(row["retry_eligible"] or 0)
            recoverable = (
                (expected_state == "failed" and retry_eligible == 1)
                or (expected_state == "downloading" and expected_status == "waiting_for_complete_source")
            )
            if not recoverable:
                return {"ok": False, "reason": "task_not_recoverable", "state": expected_state, "status": expected_status}
            raw.update(
                {
                    "import_ready_promoted": True,
                    "import_ready_promoted_at": ts,
                    "import_ready_promoted_reason": reason,
                    "import_ready_promoted_path": source_text,
                    "import_ready_promoted_client": client_text,
                }
            )
            outcome = inkdrop_state.download_task_outcome("import_ready", "staged_file_ready", "import_ready")
            display_phase = inkdrop_state.download_task_display_phase("import_ready", "staged_file_ready", "import_ready", outcome)
            task_cur = con.execute(
                """
                update download_tasks
                   set status='staged_file_ready',
                       state='import_ready',
                       lifecycle_phase='import_ready',
                       failure_reason=null,
                       retry_eligible=0,
                       updated_at=?,
                       completed_at=coalesce(completed_at, ?),
                       outcome=?,
                       display_phase=?,
                       raw_json=?
                 where id=? and queue_id=? and lower(coalesce(state,''))=?
                   and lower(coalesce(status,''))=? and coalesce(retry_eligible,0)=?
                """,
                (
                    ts, ts, outcome, display_phase, json.dumps(raw, sort_keys=True),
                    task_id, queue_id, expected_state, expected_status, retry_eligible,
                ),
            )
            if int(task_cur.rowcount or 0) != 1:
                con.rollback()
                return {"ok": False, "reason": "task_recovery_compare_failed"}
            queue_cur = con.execute(
                """
                update queue_items
                   set state='importing',
                       display_phase='staged_or_importing',
                       current_source=coalesce(nullif(current_source, ''), 'download_client'),
                       last_event=?,
                       updated_at=?
                 where id=?
                   and active=1
                """,
                (message, ts, queue_id),
            )
            con.commit()
            return {
                "ok": True,
                "queue_id": queue_id,
                "download_task_id": task_id,
                "queue_updated": int(queue_cur.rowcount or 0),
                "task_updated": int(task_cur.rowcount or 0),
            }

    try:
        return with_sqlite_lock_retry(mark_task_ready)
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        return {"ok": False, "reason": "state_db_locked", "error": str(exc)}


def retryable_failed_staged_import_ready_rows(limit=300):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return []
    max_rows = max(1, min(int(limit or 300), 1000))
    staged_clients = tuple(sorted(set(INKDROP_STAGED_SOURCE_CLIENTS)))
    placeholders = ",".join("?" for _ in staged_clients)
    with inkdrop_state.connect_read(
        INKDROP_STATE_DB,
        timeout_seconds=INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS,
        busy_timeout_ms=INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS,
    ) as con:
        rows = con.execute(
            f"""
            select q.id as queue_id, q.wanted_id, q.series_id, q.issue_id,
                   q.query, q.updated_at as queue_updated_at,
                   s.title as series_title,
                   s.media_type as series_media_type,
                   s.year as series_year,
                   s.publisher as series_publisher,
                   s.metadata_provider as series_metadata_provider,
                   s.metadata_id as series_metadata_id,
                   s.library_path as series_library_path,
                   s.library_adapter_path as series_library_adapter_path,
                   i.issue_number, i.normalized_number, i.title as issue_title,
                   dt.id as download_task_id, dt.source_attempt_id,
                   dt.source, dt.provider_id, dt.provider, dt.protocol, dt.download_client, dt.download_client_instance_id,
                   dt.external_id, dt.candidate_identity, dt.title as task_title,
                   dt.category, dt.save_path, dt.local_path, dt.status as task_status,
                   dt.state as task_state, dt.lifecycle_phase, dt.failure_reason,
                   dt.retry_eligible, dt.started_at, dt.updated_at as task_updated_at,
                   dt.completed_at, dt.raw_json as task_raw_json
            from queue_items q
            join series s on s.id=q.series_id
            left join issues i on i.id=q.issue_id
            join download_tasks dt on dt.queue_id=q.id
            where q.active=1
              and lower(coalesce(dt.download_client, '')) in ({placeholders})
              and lower(coalesce(dt.state, ''))='failed'
              and coalesce(dt.retry_eligible, 0)=1
              and nullif(trim(coalesce(dt.local_path, '')), '') is not null
            order by coalesce(dt.updated_at, dt.completed_at, q.updated_at, 0) desc, dt.id desc
            limit ?
            """,
            (*staged_clients, max_rows),
        ).fetchall()
    return [dict(row) for row in rows]


def recover_retryable_failed_staged_import_ready_records(max_records=300):
    summary = {"checked": 0, "promoted": 0, "skipped": {}}
    try:
        rows = retryable_failed_staged_import_ready_rows(max_records)
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        summary["skipped"]["state_db_locked"] = 1
        return summary
    if not rows:
        return summary
    targets = imp.load_comic_targets(None)
    imported_state = load_imported_state()
    bad_archive_memory = load_bad_archive_validation_memory()
    for row in rows:
        summary["checked"] += 1
        source_path = str(row.get("local_path") or "").strip()
        queue_id = str(row.get("queue_id") or "").strip()
        task_id = str(row.get("download_task_id") or "").strip()
        if not (source_path and queue_id and task_id):
            summary["skipped"]["missing_identity"] = summary["skipped"].get("missing_identity", 0) + 1
            continue
        source = Path(source_path)
        try:
            source_ready = source.exists() and source.is_file() and is_archive_path(source)
        except OSError:
            source_ready = False
        if not source_ready:
            summary["skipped"]["missing_source_file"] = summary["skipped"].get("missing_source_file", 0) + 1
            continue
        detail = classify_inkdrop_client_file(source, row, targets, imported_state, bad_archive_memory)
        if detail.get("state") != "ready_to_import":
            reason = str(detail.get("reason") or detail.get("state") or "not_ready").strip() or "not_ready"
            summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1
            continue
        result = record_inkdrop_import_ready_promotion(
            queue_id,
            task_id,
            source_path=source_path,
            client=row.get("download_client"),
            promotion_reason="retryable_failed_staged_source_revalidated",
            message="Staged file is importable again; import worker will retry it automatically",
        )
        if result.get("ok") and result.get("task_updated"):
            summary["promoted"] += 1
        else:
            reason = str(result.get("reason") or "promotion_failed").strip() or "promotion_failed"
            summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1
    return summary


def promote_complete_deferred_import_ready_records(max_records=300):
    summary = {"checked": 0, "promoted": 0, "still_incomplete": 0, "missing_source": 0, "state_errors": 0, "reconcile_updates": 0}
    if inkdrop_state is None or not INKDROP_STATE_DB.exists() or not DB_PATH.exists():
        return summary
    try:
        incomplete_paths = set(imp.load_qbit_incomplete_paths("comics"))
    except Exception:
        incomplete_paths = set()
    try:
        max_rows = max(1, int(max_records or 300))
    except (TypeError, ValueError):
        max_rows = 300
    with inkdrop_state.connect(
        INKDROP_STATE_DB,
        timeout_seconds=INKDROP_STATE_READ_TIMEOUT_SECONDS,
        busy_timeout_ms=INKDROP_STATE_READ_BUSY_TIMEOUT_MS,
    ) as con:
        inkdrop_state.init_schema(con)
        rows = con.execute(
            """
            select q.id as queue_id,
                   dt.id as task_id,
                   dt.local_path,
                   dt.download_client
            from download_tasks dt
            join queue_items q on q.id=dt.queue_id
            where q.active=1
              and lower(coalesce(dt.source, ''))='local_pack'
              and lower(coalesce(dt.download_client, ''))='inkdrop_local_pack'
              and lower(coalesce(dt.status, ''))='waiting_for_complete_source'
              and lower(coalesce(dt.state, ''))='downloading'
              and lower(coalesce(dt.failure_reason, ''))='source_file_incomplete_qbit_download'
              and nullif(trim(coalesce(dt.local_path, '')), '') is not null
            order by coalesce(dt.updated_at, dt.started_at, 0) asc, dt.id asc
            limit ?
            """,
            (max_rows,),
        ).fetchall()
    for row in rows:
        summary["checked"] += 1
        source_text = str(row["local_path"] or "").strip()
        if source_text in incomplete_paths:
            summary["still_incomplete"] += 1
            continue
        source = Path(source_text)
        try:
            if not (source.exists() and source.is_file()):
                summary["missing_source"] += 1
                continue
        except OSError:
            summary["missing_source"] += 1
            continue
        ts = now()
        try:
            conn = connect_db()
            try:
                cur = conn.execute(
                    """
                    update download_reconciliation
                       set lifecycle_state='ready_to_import',
                           reason='source_file_completed_after_incomplete_wait',
                           updated_at=?
                     where inkdrop_queue_id=?
                       and matched_local_path=?
                       and lifecycle_state='downloading'
                       and reason='source_file_incomplete_qbit_download'
                    """,
                    (ts, row["queue_id"], source_text),
                )
                if not int(cur.rowcount or 0):
                    cur = conn.execute(
                        """
                        update download_reconciliation
                           set lifecycle_state='ready_to_import',
                               reason='source_file_completed_after_incomplete_wait',
                               updated_at=?
                         where inkdrop_download_task_id=?
                           and matched_local_path=?
                           and lifecycle_state='downloading'
                           and reason='source_file_incomplete_qbit_download'
                        """,
                        (ts, row["task_id"], source_text),
                    )
                conn.commit()
                summary["reconcile_updates"] += int(cur.rowcount or 0)
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            if not sqlite_lock_error(exc):
                raise
            summary["state_errors"] += 1
            continue
        result = record_inkdrop_import_ready_promotion(
            row["queue_id"],
            row["task_id"],
            source_path=source_text,
            client=row["download_client"],
        )
        if result.get("ok") and result.get("task_updated"):
            summary["promoted"] += 1
        else:
            summary["state_errors"] += 1
    return summary


def recover_failed_filename_guard_import_ready_records(max_records=300):
    summary = {"checked": 0, "recovered": 0, "skipped": {}}
    if not DB_PATH.exists():
        return summary
    try:
        rows = active_import_ready_rows(max_records)
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        summary["skipped"]["state_db_locked"] = 1
        return summary
    if not rows:
        return summary
    ts = now()
    conn = connect_db()
    try:
        for row in rows:
            summary["checked"] += 1
            queue_id = str(row.get("inkdrop_queue_id") or "").strip()
            task_id = str(row.get("inkdrop_download_task_id") or "").strip()
            source_path = str(row.get("matched_local_path") or "").strip()
            trusted_issue = str(row.get("trusted_issue") or "").strip()
            if not (queue_id and task_id and source_path and trusted_issue):
                summary["skipped"]["missing_identity"] = summary["skipped"].get("missing_identity", 0) + 1
                continue
            cur = conn.execute(
                """
                update download_reconciliation
                   set lifecycle_state='ready_to_import',
                       reason='trusted_issue_filename_guard_retry',
                       updated_at=?
                 where lifecycle_state='failed_import'
                   and reason='importer_skipped_filename_confidence_too_low'
                   and inkdrop_queue_id=?
                   and inkdrop_download_task_id=?
                   and matched_local_path=?
                   and nullif(trim(coalesce(trusted_issue, '')), '') is not null
                """,
                (ts, queue_id, task_id, source_path),
            )
            summary["recovered"] += int(cur.rowcount or 0)
        conn.commit()
    finally:
        conn.close()
    return summary


def inkdrop_reconciliation_client(download_client):
    client = str(download_client or "").strip().lower()
    if client in {"qbit", "qbittorrent"}:
        return "qbit"
    if client in {"sab", "sabnzbd"}:
        return "sab"
    if client in set(INKDROP_STAGED_SOURCE_CLIENTS):
        return client
    return client or "download_client"


def import_ready_client_priority(download_client):
    client = inkdrop_reconciliation_client(download_client)
    if client in set(INKDROP_STAGED_SOURCE_CLIENTS):
        return 0
    if client in {"qbit", "sab"}:
        return 1
    return 2


def inkdrop_reconciliation_client_ids(row):
    row = row if isinstance(row, dict) else {}
    client = inkdrop_reconciliation_client(row.get("download_client"))
    client_id = row.get("external_id") or row.get("candidate_identity") or row.get("download_task_id")
    return {
        "client": client,
        "client_id": client_id,
        "client_hash": row.get("external_id") if client == "qbit" else None,
        "nzo_id": row.get("external_id") if client == "sab" else None,
        "download_url_hash": row.get("external_id") if client in set(INKDROP_STAGED_SOURCE_CLIENTS) else None,
    }


def inkdrop_import_ready_rows(limit=300):
    if not INKDROP_STATE_DB.exists():
        return []
    if inkdrop_state is None:
        return []
    with inkdrop_state.connect_read(
        INKDROP_STATE_DB,
        timeout_seconds=max(INKDROP_STATE_READ_TIMEOUT_SECONDS, 10.0),
        busy_timeout_ms=max(INKDROP_STATE_READ_BUSY_TIMEOUT_MS, 10000),
    ) as conn:
        rows = conn.execute(
            """
            select q.id as queue_id, q.wanted_id, q.series_id, q.issue_id,
                   q.query, q.updated_at as queue_updated_at,
                   s.title as series_title,
                   i.issue_number, i.normalized_number, i.title as issue_title,
                   dt.id as download_task_id, dt.source_attempt_id,
                   dt.source, dt.provider_id, dt.provider, dt.protocol, dt.download_client, dt.download_client_instance_id,
                   dt.external_id, dt.candidate_identity, dt.title as task_title,
                   dt.category, dt.save_path, dt.local_path, dt.status as task_status,
                   dt.state as task_state, dt.started_at, dt.updated_at as task_updated_at,
                   dt.completed_at, dt.raw_json as task_raw_json
            from queue_items q
            join series s on s.id=q.series_id
            left join issues i on i.id=q.issue_id
            join download_tasks dt on dt.id = (
                select d.id
                from download_tasks d
                where d.queue_id=q.id
                  and lower(coalesce(d.download_client, '')) in ('qbittorrent','sabnzbd','slskd','inkdrop_direct','inkdrop_page_pack','inkdrop_external_tool','inkdrop_local_pack')
                  and nullif(trim(coalesce(d.local_path, '')), '') is not null
                  and (
                        (
                          lower(coalesce(d.download_client, '')) in ('qbittorrent','sabnzbd')
                          and d.state='import_ready'
                          and lower(coalesce(d.status, ''))='completed_in_client'
                        )
                        or (
                          lower(coalesce(d.download_client, '')) in ('slskd','inkdrop_direct','inkdrop_page_pack','inkdrop_external_tool','inkdrop_local_pack')
                          and d.state='import_ready'
                          and lower(coalesce(d.status, '')) in ('staged_file_ready','ready_import','preview_importable','ready_to_import','completed_in_client')
                        )
                        or (
                          lower(coalesce(d.download_client, ''))<>'slskd'
                          and lower(coalesce(d.status, ''))='ready_to_import'
                        )
                        or (
                          lower(coalesce(d.download_client, '')) in ('qbittorrent','sabnzbd')
                          and (
                            lower(coalesce(q.last_event, '')) like '%completed in client%'
                            or lower(coalesce(q.last_event, '')) like '%ready_to_import%'
                          )
                        )
                      )
                  and lower(coalesce(d.state, '')) not in ('failed')
                  and lower(coalesce(d.status, '')) not in ('error','failed','download_api_error','transfer_stale_unknown','stale_orphan')
                order by case
                           when lower(coalesce(d.download_client, '')) in ('slskd','inkdrop_direct','inkdrop_page_pack','inkdrop_external_tool','inkdrop_local_pack') then 0
                           when lower(coalesce(d.download_client, '')) in ('qbittorrent','sabnzbd') then 1
                           else 2
                         end,
                         coalesce(d.updated_at, d.completed_at, d.started_at, 0) desc,
                         d.id desc
                limit 1
            )
            where q.active=1
              and (
                    q.state='importing'
                    or (
                      lower(coalesce(dt.download_client, ''))<>'slskd'
                      and lower(coalesce(dt.status, ''))='ready_to_import'
                    )
                    or (
                      lower(coalesce(dt.download_client, '')) in ('slskd','inkdrop_direct','inkdrop_page_pack','inkdrop_external_tool','inkdrop_local_pack')
                      and lower(coalesce(dt.state, ''))='import_ready'
                      and lower(coalesce(dt.status, '')) in ('staged_file_ready','ready_import','preview_importable','ready_to_import','completed_in_client')
                    )
                    or (
                      lower(coalesce(dt.download_client, '')) in ('qbittorrent','sabnzbd')
                      and lower(coalesce(dt.state, ''))='import_ready'
                      and lower(coalesce(dt.status, ''))='completed_in_client'
                    )
                  )
              and (
                    lower(coalesce(q.current_source, '')) in ('qbittorrent','sabnzbd')
                    or (
                      lower(coalesce(q.current_source, ''))='download_client'
                      and lower(coalesce(q.last_event, '')) like '%ready_to_import%'
                    )
                    or (
                      lower(coalesce(dt.download_client, '')) in ('slskd','inkdrop_direct','inkdrop_page_pack','inkdrop_external_tool','inkdrop_local_pack')
                      and lower(coalesce(dt.state, ''))='import_ready'
                      and lower(coalesce(dt.status, '')) in ('staged_file_ready','ready_import','preview_importable','ready_to_import')
                    )
                    or (
                      lower(coalesce(q.current_source, ''))='download_client'
                      and lower(coalesce(dt.state, ''))='import_ready'
                      and lower(coalesce(dt.status, ''))='completed_in_client'
                      and (
                        lower(coalesce(dt.local_path, '')) glob '*.cbz'
                        or lower(coalesce(dt.local_path, '')) glob '*.cbr'
                        or lower(coalesce(dt.local_path, '')) glob '*.pdf'
                      )
                    )
                    or (
                      lower(coalesce(dt.download_client, '')) in ('qbittorrent','sabnzbd')
                      and lower(coalesce(dt.state, ''))='import_ready'
                      and lower(coalesce(dt.status, ''))='completed_in_client'
                    )
                    or (
                      lower(coalesce(dt.download_client, ''))<>'slskd'
                      and lower(coalesce(dt.status, ''))='ready_to_import'
                    )
                  )
            order by coalesce(dt.completed_at, dt.updated_at, q.updated_at, 0) asc, q.id asc
            limit ?
            """,
            (max(1, int(limit or 300)),),
        ).fetchall()
        items = [dict(row) for row in rows]
        items.sort(
            key=lambda row: (
                import_ready_batch_priority(
                    row.get("local_path"),
                    row.get("task_title"),
                    row.get("provider"),
                    row.get("series_title"),
                    row.get("query"),
                ),
                import_ready_client_priority(row.get("download_client")),
                number_or_zero(row.get("completed_at") or row.get("task_updated_at") or row.get("queue_updated_at")),
                str(row.get("queue_id") or ""),
            )
        )
        return items


def sync_inkdrop_import_ready_records(max_records=300, budget_seconds=None):
    rows = inkdrop_import_ready_rows(max_records)
    if not rows:
        return {"checked": 0, "ready": 0, "upserted": 0}
    started = time.monotonic()
    try:
        budget = float(
            INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS
            if budget_seconds is None
            else budget_seconds
        )
    except (TypeError, ValueError):
        budget = float(INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS)
    budget = max(1.0, budget)
    targets = imp.load_comic_targets(None)
    imported_state = load_imported_state()
    bad_archive_memory = load_bad_archive_validation_memory()
    records = []
    rejected = []
    checked = 0
    processed = 0
    budget_exhausted = False
    archive_cache = {}
    for row in rows:
        if processed and time.monotonic() - started >= budget:
            budget_exhausted = True
            break
        processed += 1

        def cached_archives(path_value):
            path_key = str(path_value or "").strip()
            if not path_key:
                return []
            key = (path_key, str(row.get("download_client_instance_id") or "").strip())
            if key not in archive_cache:
                instance_id = row.get("download_client_instance_id")
                archive_cache[key] = (
                    archive_paths_for_completed_client_path(path_value, download_client_instance_id=instance_id)
                    if instance_id
                    else archive_paths_for_completed_client_path(path_value)
                )
            return list(archive_cache.get(key) or [])

        archives = cached_archives(row.get("local_path"))
        if not archives and row.get("save_path"):
            archives.extend(cached_archives(row.get("save_path")))
        nested_resolution = None
        if not archives and inkdrop_reconciliation_client(row.get("download_client")) == "qbit":
            nested_resolution = completed_client_nested_archive_resolution(row)
            archives.extend(nested_resolution.get("paths") or [])
        seen = set()
        unique_archives = []
        for archive in archives:
            key = str(archive)
            if key in seen:
                continue
            seen.add(key)
            unique_archives.append(archive)
        issue_number = row.get("issue_number") or row.get("normalized_number")
        matching_archives = (
            list(nested_resolution.get("matching_paths") or [])
            if nested_resolution
            else [path for path in unique_archives if archive_matches_issue(path, issue_number, filename_only=True)]
        )
        if not matching_archives and len(unique_archives) == 1 and not nested_resolution:
            matching_archives = unique_archives
        classified = []
        for path in matching_archives[:8]:
            checked += 1
            classified.append(classify_inkdrop_client_file(path, row, targets, imported_state, bad_archive_memory))
        if classified:
            ready = [item for item in classified if item["state"] == "ready_to_import"]
            chosen = ready[0] if ready else classified[0]
        elif nested_resolution and nested_resolution.get("state") == "false_positive":
            chosen = {
                "state": "false_positive",
                "reason": nested_resolution.get("reason") or "completed_client_nested_archive_wrong_unit",
                "local_path": str(unique_archives[0]),
            }
        else:
            chosen = {
                "state": "stale_no_local_file",
                "reason": "completed_client_path_missing_archive",
                "local_path": None,
            }
        client_ids = inkdrop_reconciliation_client_ids(row)
        records.append(
            {
                "pending_key": "inkdrop:" + str(row.get("queue_id")),
                "title": row.get("task_title") or row.get("query") or row.get("series_title"),
                "query": row.get("query") or " ".join(str(bit) for bit in (row.get("series_title"), row.get("issue_number")) if bit),
                "protocol": row.get("protocol"),
                "client": client_ids["client"],
                "client_id": client_ids["client_id"],
                "client_hash": client_ids["client_hash"],
                "nzo_id": client_ids["nzo_id"],
                "download_url_hash": client_ids["download_url_hash"],
                "state": chosen.get("state"),
                "reason": chosen.get("reason") or "inkdrop_completed_client_task",
                "local_path": chosen.get("local_path"),
                "matched_local_path": chosen.get("matched_local_path"),
                "matched_series": chosen.get("matched_series") or row.get("series_title"),
                "matched_kapowarr_volume_id": chosen.get("matched_kapowarr_volume_id"),
                "unit_model": chosen.get("unit_model"),
                "truth_model": chosen.get("truth_model"),
                "first_sent_at": row.get("started_at") or row.get("queue_updated_at"),
                "trusted_series_id": row.get("series_id"),
                "trusted_issue": chosen.get("trusted_issue") or row.get("issue_number") or row.get("normalized_number"),
                "inkdrop_queue_id": row.get("queue_id"),
                "inkdrop_download_task_id": row.get("download_task_id"),
            }
        )
        if chosen.get("state") in INKDROP_IMPORT_READY_REJECTION_STATES:
            rejected.append((row, chosen))
    upserted = upsert_reconciliation_records(records)
    rejection_limit = int(INKDROP_IMPORT_READY_REJECTION_UPDATE_LIMIT)
    rejected_for_update = rejected[:rejection_limit] if rejection_limit >= 0 else list(rejected)
    rejection_results = [record_inkdrop_import_ready_rejection(row, chosen) for row, chosen in rejected_for_update]
    return {
        "checked": processed,
        "candidate_rows": len(rows),
        "archive_checks": checked,
        "ready": sum(1 for record in records if record.get("state") == "ready_to_import"),
        "rejected": len(rejected),
        "rejection_update_limit": rejection_limit,
        "rejection_updates_skipped": max(0, len(rejected) - len(rejected_for_update)),
        "rejection_updates": sum(1 for result in rejection_results if isinstance(result, dict) and result.get("ok")),
        "rejection_errors": [result for result in rejection_results if isinstance(result, dict) and not result.get("ok")][:5],
        "upserted": upserted,
        "budget_seconds": budget,
        "budget_exhausted": budget_exhausted,
    }


def completed_sets_for_target(target, imported_state):
    keys = set()
    if imp.is_manga_target(target):
        keys.update(imported_state.get("manga", set()))
        keys.update(imported_state.get("manga_units", set()))
        keys.update(imported_state.get("manga_coverage", set()))
    keys.update(imported_state.get("collections", set()))
    return keys


def matching_imported_destination(target, number, imported_state):
    if not target or not number:
        return None
    for dest in imported_state.get("dest_paths", set()):
        dest_path = Path(dest)
        if dest_path.suffix.lower() not in {".cbz", ".cbr", ".pdf"}:
            continue
        dest_number = imp.normalize_manga_number(imp.extract_issue_number(dest_path))
        if dest_number != number:
            continue
        try:
            matched = imp.match_comic_target(dest_path, [target])
        except Exception:
            continue
        if matched and matched.get("id") == target.get("id"):
            return dest
    return None


def build_current_missing_keys():
    try:
        missing = load_missing_acquire()
        if not missing:
            return None
        rows = missing.missing_issues(tuple(missing.monitored_series_names()))
        rows, _suppressed = missing.suppress_completed_reading(rows)
    except Exception:
        return None
    keys = set()
    for row in rows:
        series = imp.normalize_series(row.get("title"))
        number = imp.normalize_manga_number(row.get("calculated_issue_number") or row.get("issue_number"))
        if series and number:
            keys.add((series, number))
    return keys


def pending_record_issue_number(record):
    for value in (
        (record or {}).get("title"),
        (record or {}).get("query"),
        (record or {}).get("pending_key"),
    ):
        if value in (None, ""):
            continue
        number = imp.normalize_manga_number(imp.extract_issue_number(value))
        if number:
            return number
    return None


def pending_record_currently_missing(record, current_missing_keys):
    if current_missing_keys is None:
        return True
    number = pending_record_issue_number(record)
    if not number:
        return False
    text = norm(" ".join(str((record or {}).get(key) or "") for key in ("title", "query", "pending_key")))
    if not text:
        return False
    for series_key, missing_number in current_missing_keys:
        if missing_number != number:
            continue
        if series_key and series_key in text:
            return True
    return False


def classify_local_file(
    path,
    targets,
    imported_state,
    validate_archive=True,
    current_missing_keys=None,
    trusted_issue=None,
    skip_related_subseries=False,
):
    if path.suffix.lower() not in {".cbz", ".cbr", ".pdf"}:
        return {"state": "manual_review", "reason": "wrong_media_type_or_pdf_review"}
    try:
        path_size = path.stat().st_size
    except OSError:
        path_size = None
    target = imp.match_comic_target(path, targets)
    if not target:
        return {"state": "manual_review", "reason": "unmatched_local_file"}
    path_text = str(path)
    if path_text in imported_state["source_paths"] or path_text in imported_state["dest_paths"]:
        imported_dest = imported_state.get("source_to_dest", {}).get(path_text)
        return {
            "state": "suppressed_completed",
            "reason": "already_imported_or_verified",
            "matched_local_path": imported_dest or path_text,
            "matched_series": target.get("title"),
            "matched_kapowarr_volume_id": target.get("id"),
            "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kapowarr_comic",
        }
    if (
        path_size
        and path_size <= MAX_HASH_SUPPRESSION_BYTES
        and path_size in imported_state.get("hash_sizes", set())
    ):
        try:
            digest = imp.sha256(path)
            if digest in imported_state.get("hashes", set()):
                imported_match = imported_state.get("hash_to_imported", {}).get(digest) or {}
                return {
                    "state": "suppressed_completed",
                    "reason": "already_imported_matching_hash",
                    "matched_local_path": imported_match.get("dest") or None,
                    "matched_series": target.get("title"),
                    "matched_kapowarr_volume_id": target.get("id"),
                    "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kapowarr_comic",
                }
        except Exception:
            pass
    unsafe_match_reason = imp.unsafe_comic_target_match_reason(path, target)
    if unsafe_match_reason:
        return {
            "state": "manual_review",
            "reason": unsafe_match_reason,
            "matched_series": target.get("title"),
            "matched_kapowarr_volume_id": target.get("id"),
        }
    supplemental_reason = imp.supplemental_source_blocker(path)
    if supplemental_reason:
        return {
            "state": "false_positive",
            "reason": supplemental_reason,
            "matched_series": target.get("title"),
            "matched_kapowarr_volume_id": target.get("id"),
        }
    related_subseries_reason = "" if skip_related_subseries else imp.related_subseries_source_blocker(
        target.get("title"),
        path,
        issue_title=target.get("issue_title"),
        issue_number=trusted_issue or target.get("issue_number") or target.get("normalized_number"),
        publisher=target.get("publisher"),
    )
    if related_subseries_reason:
        return {
            "state": "wrong_series_or_subseries",
            "reason": related_subseries_reason,
            "matched_series": target.get("title"),
            "matched_kapowarr_volume_id": target.get("id"),
        }
    early_filename_gate = imp.classify_import_filename_safety(path, target=target, kind="comics", trusted_issue=trusted_issue)
    if not early_filename_gate.get("ok") and early_filename_gate.get("reason") != "duplicate_copy_suffix":
        return {
            "state": "manual_review",
            "reason": early_filename_gate.get("reason") or "weak_filename_import_guard",
            "detail": early_filename_gate.get("detail"),
            "filename_score": early_filename_gate.get("score"),
            "filename_evidence": early_filename_gate.get("evidence") or [],
            "matched_series": target.get("title"),
            "matched_kapowarr_volume_id": target.get("id"),
            "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kapowarr_comic",
        }

    existing_path_trusted_issue = (
        trusted_issue
        if trusted_issue not in (None, "")
        else target.get("issue_number")
        or target.get("normalized_number")
        or target.get("number")
    )

    def existing_completed_path_unit_mismatch(existing_path, reason):
        if existing_path_trusted_issue in (None, ""):
            return None
        existing_gate = imp.classify_import_filename_safety(
            existing_path,
            target=target,
            kind="comics",
            trusted_issue=existing_path_trusted_issue,
        )
        if existing_gate.get("ok") or existing_gate.get("reason") == "duplicate_copy_suffix":
            return None
        return {
            "state": "wrong_series_or_subseries",
            "reason": f"{reason}_unit_mismatch",
            "detail": existing_gate.get("detail"),
            "filename_score": existing_gate.get("score"),
            "filename_evidence": existing_gate.get("evidence") or [],
            "matched_series": target.get("title"),
            "matched_kapowarr_volume_id": target.get("id"),
            "matched_local_path": str(existing_path),
            "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kapowarr_comic",
        }

    target_dir = (
        Path(imp.kavita_manga_series_dir(target))
        if imp.is_manga_target(target)
        else Path(target.get("folder") or "")
    )
    if target_dir:
        existing_names = [target_dir / path.name]
        if path.suffix.lower() in {".cbr", ".pdf"}:
            existing_names.append(target_dir / f"{path.stem}.cbz")
        if path.name.lower().endswith(".cbz.zip"):
            existing_names.append(target_dir / f"{path.name[:-4]}")
        for existing_name in existing_names:
            try:
                if existing_name.exists() and existing_name.resolve() != path.resolve():
                    mismatch = existing_completed_path_unit_mismatch(
                        existing_name,
                        "matching_filename_already_present",
                    )
                    if mismatch:
                        return mismatch
                    return {
                        "state": "suppressed_completed",
                        "reason": "matching_filename_already_present",
                        "matched_series": target.get("title"),
                        "matched_kapowarr_volume_id": target.get("id"),
                        "matched_local_path": str(existing_name),
                        "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kapowarr_comic",
                    }
            except OSError:
                pass
        try:
            _predicted_dest, canonical = imp.canonical_comic_dest(target_dir, path, target)
            canonical_existing = imp.existing_canonical_dest(target_dir, canonical, path)
        except Exception:
            canonical_existing = None
        if canonical_existing and canonical_existing.resolve() != path.resolve():
            mismatch = existing_completed_path_unit_mismatch(
                canonical_existing,
                "canonical_file_already_present",
            )
            if mismatch:
                return mismatch
            return {
                "state": "suppressed_completed",
                "reason": "canonical_file_already_present",
                "matched_series": target.get("title"),
                "matched_kapowarr_volume_id": target.get("id"),
                "matched_local_path": str(canonical_existing),
                "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kapowarr_comic",
            }
    filename_gate = imp.classify_import_filename_safety(path, target=target, kind="comics", trusted_issue=trusted_issue)
    if not filename_gate.get("ok"):
        return {
            "state": "manual_review",
            "reason": filename_gate.get("reason") or "weak_filename_import_guard",
            "detail": filename_gate.get("detail"),
            "filename_score": filename_gate.get("score"),
            "filename_evidence": filename_gate.get("evidence") or [],
            "matched_series": target.get("title"),
            "matched_kapowarr_volume_id": target.get("id"),
            "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kapowarr_comic",
        }
    series_key = imp.normalize_series(target.get("title"))
    number_key = imp.normalize_manga_number(imp.extract_issue_number(path))
    if imp.is_manga_target(target):
        unit_key, unit_number = imp.manga_file_unit_and_number(path)
        existing_manga = imp.find_existing_manga_unit_file(target, unit_number, unit_key, exclude=path)
        if existing_manga:
            return {
                "state": "suppressed_completed",
                "reason": "already_verified_manga_file_present",
                "matched_series": target.get("title"),
                "matched_kapowarr_volume_id": target.get("id"),
                "matched_local_path": str(existing_manga),
                "unit_model": unit_key,
                "truth_model": "kavita_manga",
            }
        number_key = unit_number or number_key
    if current_missing_keys is not None and series_key and number_key and (series_key, number_key) not in current_missing_keys:
        return {
            "state": "suppressed_completed",
            "reason": "not_currently_missing",
            "matched_series": target.get("title"),
            "matched_kapowarr_volume_id": target.get("id"),
            "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kapowarr_comic",
        }
    if series_key and number_key and (series_key, number_key) in completed_sets_for_target(target, imported_state):
        return {
            "state": "suppressed_completed",
            "reason": "already_verified_series_number",
            "matched_series": target.get("title"),
            "matched_kapowarr_volume_id": target.get("id"),
            "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kavita_collection",
        }
    matched_dest = matching_imported_destination(target, number_key, imported_state)
    if matched_dest:
        return {
            "state": "suppressed_completed",
            "reason": "already_imported_matching_destination",
            "matched_series": target.get("title"),
            "matched_kapowarr_volume_id": target.get("id"),
            "matched_local_path": matched_dest,
            "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kapowarr_comic",
        }
    if imp.is_manga_target(target):
        unit = imp.manga_import_guard(
            path,
            target,
            suwayomi_staging=imp.is_suwayomi_import_source(path),
            trusted_issue=trusted_issue,
        )
        if unit.get("completed"):
            return {
                "state": "suppressed_completed",
                "reason": unit.get("reason") or "already_verified_duplicate",
                "matched_series": target.get("title"),
                "matched_kapowarr_volume_id": target.get("id"),
                "matched_local_path": unit.get("existing_path"),
                "unit_model": unit.get("source_unit"),
                "truth_model": "kavita_manga",
            }
        if not unit.get("allowed", True):
            return {
                "state": "manual_review",
                "reason": unit.get("reason") or "unsupported_manga_unit_model",
                "matched_series": target.get("title"),
                "matched_kapowarr_volume_id": target.get("id"),
                "unit_model": unit.get("source_unit"),
                "truth_model": "kavita_manga",
            }
    if validate_archive:
        try:
            archive = imp.validate_comic_archive(path)
        except Exception as exc:
            return {"state": "bad_archive", "reason": str(exc), "matched_series": target.get("title"), "matched_kapowarr_volume_id": target.get("id")}
        if not archive.get("ok"):
            return {"state": "bad_archive", "reason": archive.get("reason"), "matched_series": target.get("title"), "matched_kapowarr_volume_id": target.get("id")}
    return {
        "state": "ready_to_import",
        "reason": "safe_exact_local_match",
        "matched_series": target.get("title"),
        "matched_kapowarr_volume_id": target.get("id"),
        "truth_model": "kavita_manga" if imp.is_manga_target(target) else "kapowarr_comic",
    }


def manga_duplicate_status():
    cached = read_duplicate_status_cache()
    if cached:
        return cached
    groups = collections.defaultdict(list)
    root = getattr(imp, "MANGA_ROOT", Path(os.environ.get("INKDROP_MANGA_ROOT") or "/library/manga"))
    if not root.exists():
        return {"duplicate_manga_files": 0, "duplicate_manga_review_count": 0, "duplicate_manga_samples": []}
    for path in bounded_archive_files([root]):
        if imp.is_internal_import_path(path, root) or path.suffix.lower() not in ARCHIVE_SUFFIXES:
            continue
        info = imp.read_comicinfo(path)
        try:
            relative_parts = path.relative_to(root).parts
            folder_series = relative_parts[0] if relative_parts else path.parent.name
        except ValueError:
            folder_series = path.parent.name
        detected_unit, detected_number = imp.manga_file_unit_and_number(path)
        series = imp.comicinfo_text(info, "Series") or folder_series
        number = imp.normalize_manga_number(
            imp.comicinfo_text(info, "Number")
            or imp.comicinfo_text(info, "Volume")
            or detected_number
            or imp.extract_issue_number(path)
        )
        if not series or not number:
            continue
        fmt = imp.comicinfo_text(info, "Format").lower()
        title = imp.comicinfo_text(info, "Title").lower()
        path_text = " ".join(path.parts[-2:]).lower()
        unit = detected_unit or ("chapter" if "chapter" in fmt or "chapter" in title or re.search(r"(?:^|[\s._-])chapter[\s._-]*\d+", path_text) else "volume")
        groups[(imp.normalize_series(series), unit, number)].append(str(path))
    dupes = {key: paths for key, paths in groups.items() if len(paths) > 1}
    samples = []
    for (series, unit, number), paths in list(dupes.items())[:10]:
        samples.append({"series": series, "unit": unit, "number": number, "paths": paths})
    status = {
        "duplicate_manga_files": sum(len(paths) - 1 for paths in dupes.values()),
        "duplicate_manga_review_count": len(dupes),
        "duplicate_manga_samples": samples,
    }
    write_duplicate_status_cache(status)
    return status


def read_duplicate_status_cache():
    now_ts = now()
    for path in (DUPLICATE_STATUS_CACHE_PATH, RECONCILE_STATUS_PATH):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            age = now_ts - float(data.get("updated_at") or path.stat().st_mtime)
        except (TypeError, ValueError, OSError):
            age = DUPLICATE_STATUS_CACHE_SECONDS + 1
        if age > DUPLICATE_STATUS_CACHE_SECONDS:
            continue
        if data.get("duplicate_manga_files") is None:
            continue
        return {
            "duplicate_manga_files": int(data.get("duplicate_manga_files") or 0),
            "duplicate_manga_review_count": int(data.get("duplicate_manga_review_count") or 0),
            "duplicate_manga_samples": data.get("duplicate_manga_samples") or [],
        }
    return None


def write_duplicate_status_cache(status):
    try:
        payload = dict(status or {})
        payload["updated_at"] = now()
        DUPLICATE_STATUS_CACHE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def reconcile(deep_scan=False):
    pending = load_pending_latest()
    files = archive_files()
    targets = imp.load_comic_targets(None)
    imported_state = load_imported_state()
    current_missing = build_current_missing_keys()
    bad_archive_memory = load_bad_archive_validation_memory()
    client_items = qbit_items() + sab_items()
    unavailable_clients = client_errors(client_items)
    records = []
    ready_paths = set()
    for record in pending:
        out = {
            "pending_key": record.get("pending_key"),
            "title": record.get("title"),
            "query": record.get("query"),
            "protocol": record.get("protocol"),
            "first_sent_at": record.get("first_sent_at"),
            "status": record.get("status"),
            "inkdrop_queue_id": record.get("inkdrop_queue_id"),
            "inkdrop_download_task_id": record.get("inkdrop_download_task_id"),
        }
        status = record.get("status") or "unknown"
        if status != "sent":
            if status in REVALIDATE_LOCAL_STATUSES:
                local_paths = record_local_archive_paths(record)
                classified = []
                for path in local_paths:
                    detail = classify_local_file(path, targets, imported_state, validate_archive=bool(deep_scan), current_missing_keys=current_missing)
                    detail = apply_bad_archive_memory(record, detail, path, bad_archive_memory)
                    detail["local_path"] = str(path)
                    classified.append(detail)
                ready = [item for item in classified if item["state"] == "ready_to_import"]
                if ready:
                    chosen = ready[0]
                    ready_paths.add(chosen["local_path"])
                    out.update(chosen)
                    out["reason"] = chosen.get("reason") or f"revalidated_{status}"
                elif classified:
                    chosen = classified[0]
                    out.update(chosen)
                    out["reason"] = chosen.get("reason") or f"revalidated_{status}"
                else:
                    out.update({"state": "stale_no_local_file", "reason": f"{status}_source_missing"})
                records.append(out)
                continue
            out.update({"state": status, "reason": "latest_pending_status"})
            records.append(out)
            continue
        client = find_client_match(record, client_items)
        if client and client.get("client_state") != "client_unavailable":
            if client.get("client_state") == "completed_in_client" and client.get("archive_paths"):
                classified = []
                for path_value in client.get("archive_paths") or []:
                    path = Path(path_value)
                    if not (path.exists() and path.is_file() and is_archive_path(path)):
                        continue
                    detail = classify_local_file(
                        path,
                        targets,
                        imported_state,
                        validate_archive=bool(deep_scan),
                        current_missing_keys=current_missing,
                    )
                    detail = apply_bad_archive_memory(record, detail, path, bad_archive_memory)
                    detail["local_path"] = str(path)
                    classified.append(detail)
                ready = [item for item in classified if item["state"] == "ready_to_import"]
                if ready:
                    chosen = ready[0]
                    ready_paths.add(chosen["local_path"])
                    out.update(chosen)
                    out.update(
                        {
                            "client": client.get("client"),
                            "client_id": client.get("hash") or client.get("nzo_id"),
                            "reason": chosen.get("reason") or "matched_qbit_completed_archive",
                        }
                    )
                    records.append(out)
                    continue
                if classified:
                    chosen = classified[0]
                    out.update(chosen)
                    out.update(
                        {
                            "client": client.get("client"),
                            "client_id": client.get("hash") or client.get("nzo_id"),
                            "reason": chosen.get("reason") or "matched_qbit_completed_archive",
                        }
                    )
                    records.append(out)
                    continue
            if stopped_client_retryable(record, client):
                state_label = str(client.get("state") or client.get("client_state") or "stopped")
                out.update(
                    {
                        "state": "failed_download",
                        "reason": "qbit_no_progress_stopped",
                        "client": client.get("client"),
                        "client_id": client.get("hash") or client.get("nzo_id"),
                        "failure_detail": (
                            f"qBittorrent reports {state_label} at "
                            f"{float(client.get('progress') or 0) * 100:.1f}% past the no-progress retry window"
                        ),
                        "fail_message": "qBittorrent stopped with no progress; retrying the next candidate",
                    }
                )
                records.append(out)
                continue
            out.update(
                {
                    "state": client.get("client_state"),
                    "reason": client.get("client_state_reason") or f"matched_{client.get('client')}_client",
                    "client": client.get("client"),
                    "client_id": client.get("hash") or client.get("nzo_id"),
                    "failure_detail": client.get("failure_detail"),
                }
            )
            if client.get("client_state") in {"failed_download", "bad_archive"}:
                out["fail_message"] = client.get("fail_message")
            records.append(out)
            continue
        matches = find_local_matches(record, files, pending)
        classified = []
        for path in matches:
            detail = classify_local_file(path, targets, imported_state, validate_archive=bool(deep_scan), current_missing_keys=current_missing)
            detail = apply_bad_archive_memory(record, detail, path, bad_archive_memory)
            detail["local_path"] = str(path)
            classified.append(detail)
        ready = [item for item in classified if item["state"] == "ready_to_import"]
        if ready:
            chosen = ready[0]
            ready_paths.add(chosen["local_path"])
            out.update(chosen)
        elif classified:
            chosen = classified[0]
            out.update(chosen)
            out["state"] = chosen["state"]
        else:
            age = now() - float(record.get("first_sent_at") or now())
            out.update(
                {
                    "state": "stale_no_local_file" if age > STALE_AFTER_SECONDS else "sent",
                    "reason": "no_client_or_local_file_match",
                }
            )
        records.append(out)
    pending_matched_paths = {record.get("local_path") for record in records if record.get("local_path")}
    local_unlinked = []
    if deep_scan:
        for path in files:
            if str(path) in pending_matched_paths:
                continue
            detail = classify_local_file(path, targets, imported_state, validate_archive=True, current_missing_keys=current_missing)
            detail = apply_bad_archive_memory({"local_path": str(path)}, detail, path, bad_archive_memory)
            detail.update({"local_path": str(path), "title": path.name, "query": None, "protocol": "local"})
            if detail["state"] == "ready_to_import":
                detail["reason"] = "safe_exact_local_match_unlinked_validated"
            local_unlinked.append(detail)
    counts = collections.Counter(record["state"] for record in records)
    local_counts = collections.Counter(record["state"] for record in local_unlinked)
    stale_records = [record for record in records if record["state"] == "stale_no_local_file"]
    active_stale_records = [
        record for record in stale_records
        if pending_record_currently_missing(record, current_missing)
    ]
    retained_stale_records = [
        record for record in stale_records
        if record not in active_stale_records
    ]
    duplicate_status = manga_duplicate_status()
    persist_reconciliation(records, local_unlinked)
    failed_download_sync = sync_inkdrop_failed_download_records(records)
    queue_ready_imports = sum(
        1
        for record in records
        if record.get("state") == "ready_to_import"
        and str(record.get("inkdrop_queue_id") or "").strip()
    )
    legacy_ready_imports = max(0, counts.get("ready_to_import", 0) - queue_ready_imports) + local_counts.get("ready_to_import", 0)
    status = {
        "updated_at": now(),
        "scan_mode": "deep" if deep_scan else "fast",
        "deep_scan": bool(deep_scan),
        "pending_total": len(pending),
        "local_file_total": len(files),
        "counts": dict(sorted(counts.items())),
        "local_unlinked_counts": dict(sorted(local_counts.items())),
        "active_downloads": counts.get("queued", 0) + counts.get("downloading", 0) + counts.get("stalled_downloading", 0),
        "ready_imports": counts.get("ready_to_import", 0) + local_counts.get("ready_to_import", 0),
        "queue_ready_imports": queue_ready_imports,
        "legacy_ready_imports": legacy_ready_imports,
        "unowned_ready_imports": legacy_ready_imports,
        "pending_ready_imports": counts.get("ready_to_import", 0),
        "local_ready_imports": local_counts.get("ready_to_import", 0),
        "waiting_for_scan": counts.get("waiting_for_library_scan", 0) + counts.get("waiting_for_kavita_scan", 0),
        "verified_recently": counts.get("verified", 0) + counts.get("imported", 0),
        "failed_downloads": counts.get("failed_download", 0),
        "failed_download_sync": failed_download_sync,
        "failed_imports": counts.get("failed_import", 0) + counts.get("bad_archive", 0) + counts.get("wrong_series_or_subseries", 0),
        "stale_pending": len(active_stale_records),
        "stale_pending_total": counts.get("stale_no_local_file", 0),
        "stale_pending_retained": len(retained_stale_records),
        "manual_review": counts.get("manual_review", 0) + local_counts.get("manual_review", 0),
        "suppressed_completed": counts.get("suppressed_completed", 0) + local_counts.get("suppressed_completed", 0),
        "duplicate_manga_files": duplicate_status["duplicate_manga_files"],
        "duplicate_manga_review_count": duplicate_status["duplicate_manga_review_count"],
        "duplicate_manga_samples": duplicate_status["duplicate_manga_samples"],
        "failed_download_reasons": dict(
            sorted(collections.Counter(record.get("reason") or "unknown" for record in records if record["state"] == "failed_download").items())
        ),
        "bad_archive_reasons": dict(
            sorted(collections.Counter(record.get("reason") or "unknown" for record in records if record["state"] == "bad_archive").items())
        ),
        "client_unavailable": len(unavailable_clients),
        "client_unavailable_errors": unavailable_clients,
        "samples": {
            state: lifecycle_sample(
                active_stale_records if state == "stale_no_local_file" else [record for record in records if record["state"] == state],
                8,
            )
            for state in sorted(set(counts))
        },
        "retained_stale_pending_samples": lifecycle_sample(retained_stale_records, 8),
        "local_unlinked_samples": {
            state: lifecycle_sample([record for record in local_unlinked if record["state"] == state], 8)
            for state in sorted(set(local_counts))
        },
        "unmatched_download_groups": build_unmatched_download_groups(local_unlinked),
    }
    return status


def sync_inkdrop_failed_download_records(records, limit=100):
    started_raw = now()
    started_at = float(started_raw) if finite_timestamp(started_raw) else None
    timestamp_metadata = {"started_timestamp_invalid": True} if started_at is None else {}
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        completed_at, completed_metadata = complete_failed_download_sync_timestamps(started_at, [])
        return {
            "ok": False,
            "reason": "inkdrop_state_unavailable",
            "updated": 0,
            "bad_source_candidates": 0,
            "started_at": started_at,
            "completed_at": completed_at,
            "carried": False,
            **timestamp_metadata,
            **completed_metadata,
        }
    max_rows = max(1, min(int(limit or 100), 500))
    updated = 0
    bad_source_candidates = 0
    skipped = collections.Counter()
    errors = []
    lock_samples = []
    for record in records or []:
        if updated >= max_rows:
            skipped["limit_reached"] += 1
            break
        if str(record.get("state") or "").strip().lower() != "failed_download":
            continue
        queue_id = str(record.get("inkdrop_queue_id") or "").strip()
        try:
            queue = find_inkdrop_failed_download_queue(record)
        except sqlite3.OperationalError as exc:
            if not sqlite_lock_error(exc):
                raise
            skipped["state_db_locked"] += 1
            if len(lock_samples) < 10:
                lock_samples.append(
                    failed_download_lock_sample(
                        record,
                        "queue_lookup",
                        floor=failed_download_sample_floor(started_at, lock_samples),
                    )
                )
            continue
        if not queue:
            skipped["missing_queue_match" if not queue_id else "queue_item_not_found"] += 1
            continue
        queue_id = str(queue.get("id") or queue_id).strip()
        ts = now()
        client = inkdrop_state.normalized_download_client(record.get("client") or record.get("download_client") or "download_client")
        provider = client or "download_client"
        reason = str(record.get("reason") or record.get("failure_detail") or "failed_download").strip()
        title = str(record.get("title") or record.get("query") or "").strip()
        candidate_identity = str(record.get("client_id") or record.get("pending_key") or title or "").strip()
        source_memory_source = "prowlarr" if str(record.get("protocol") or "").strip().lower() in {"usenet", "nzb", "torrent"} else "download_client"
        source_memory_provider = str(
            record.get("indexer")
            or record.get("provider")
            or record.get("source_provider")
            or record.get("matched_indexer")
            or ""
        ).strip()
        attempt = {
            "source": "download_client",
            "provider": provider,
            "download_client": provider,
            "protocol": record.get("protocol"),
            "status": "failed_download",
            "reason": reason,
            "failure_reason": reason,
            "retry_eligible": True,
            "title": title,
            "query": record.get("query"),
            "candidate_identity": candidate_identity,
            "download_url_hash": candidate_identity,
            "source_type": "download_client",
            "lifecycle_phase": "failed_candidate",
            "raw": dict(record),
        }
        bad_payload = {
            "source": source_memory_source,
            "provider": source_memory_provider or None,
            "protocol": record.get("protocol"),
            "scope_key": inkdrop_state.bad_source_scope_key(
                series_id=queue.get("series_id"),
                issue_id=queue.get("issue_id"),
                queue_id=queue_id,
                wanted_id=queue.get("wanted_id"),
            ),
            "title": title,
            "download_url_hash": candidate_identity,
            "source_path": record.get("local_path"),
            "reason": "failed_download",
            "raw": {
                "kind": "download_client_failed_reconcile",
                "record": dict(record),
                "queue_id": queue_id,
                "client": provider,
                "source_memory_source": source_memory_source,
                "source_memory_provider": source_memory_provider,
                "reason": reason,
            },
            "seen_at": ts,
        }
        try:
            bad_result = inkdrop_state.record_bad_source_candidate(INKDROP_STATE_DB, **bad_payload)
            if bad_result.get("ok"):
                bad_source_candidates += 1
                attempt["bad_source_candidate_id"] = bad_result.get("candidate_id")
        except sqlite3.OperationalError as exc:
            if not sqlite_lock_error(exc):
                raise
            skipped["state_db_locked"] += 1
            if len(lock_samples) < 10:
                lock_samples.append(
                    failed_download_lock_sample(
                        record,
                        "bad_candidate_write",
                        floor=failed_download_sample_floor(started_at, lock_samples),
                    )
                )
            continue
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        try:
            result = inkdrop_state.record_queue_source_attempt(
                INKDROP_STATE_DB,
                queue_id,
                attempt,
                attempt_id=inkdrop_state.stable_id(
                    "download_client_failed_reconcile",
                    queue_id,
                    provider,
                    candidate_identity,
                    reason,
                ),
                started_at=ts,
                completed_at=ts,
            )
            if result.get("ok"):
                updated += 1
            else:
                skipped[str(result.get("reason") or "record_attempt_failed")] += 1
        except sqlite3.OperationalError as exc:
            if not sqlite_lock_error(exc):
                raise
            skipped["state_db_locked"] += 1
            if len(lock_samples) < 10:
                lock_samples.append(
                    failed_download_lock_sample(
                        record,
                        "attempt_write",
                        floor=failed_download_sample_floor(started_at, lock_samples),
                    )
                )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    completed_at, completed_metadata = complete_failed_download_sync_timestamps(started_at, lock_samples)
    return {
        "ok": not errors,
        "updated": updated,
        "bad_source_candidates": bad_source_candidates,
        "skipped": dict(sorted(skipped.items())),
        "errors": errors[:10],
        "lock_samples": lock_samples,
        "started_at": started_at,
        "completed_at": completed_at,
        "carried": False,
        **timestamp_metadata,
        **completed_metadata,
    }


def find_inkdrop_failed_download_queue(record):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return None
    queue_id = str((record or {}).get("inkdrop_queue_id") or "").strip()
    if queue_id:
        return with_sqlite_read_lock_retry(
            lambda: inkdrop_state.queue_item(
                INKDROP_STATE_DB,
                queue_id,
                read_only=True,
                timeout_seconds=INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS,
                busy_timeout_ms=INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS,
            )
        )
    needles = {
        norm((record or {}).get("query")),
        norm((record or {}).get("title")),
    }
    needles = {value for value in needles if value}
    record_issue = normalize_issue_number(pending_record_issue_number(record))
    record_text = norm(" ".join(str((record or {}).get(key) or "") for key in ("title", "query", "pending_key")))
    if not needles and not (record_issue and record_text):
        return None
    def query_rows():
        with inkdrop_state.connect_read(
            INKDROP_STATE_DB,
            timeout_seconds=INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS,
            busy_timeout_ms=INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS,
        ) as con:
            return con.execute(
                """
                select q.id, q.wanted_id, q.series_id, q.issue_id, q.state, q.current_source,
                       q.query, q.last_event, q.raw_json,
                       s.title as series_title,
                       coalesce(nullif(i.issue_number, ''), nullif(i.normalized_number, '')) as issue_number
                from queue_items q
                left join series s on s.id=q.series_id
                left join issues i on i.id=q.issue_id
                where q.active=1
                  and lower(coalesce(q.state, '')) <> 'verified'
                order by coalesce(q.updated_at, q.created_at, 0) desc
                limit 2500
                """
            ).fetchall()

    rows = with_sqlite_read_lock_retry(query_rows)
    matches = []
    for row in rows:
        row_query = norm(row["query"])
        if row_query and row_query in needles:
            matches.append(dict(row))
            continue
        row_issue = normalize_issue_number(row["issue_number"])
        series_key = norm(row["series_title"])
        if record_issue and row_issue == record_issue and series_key and norm_contains_phrase(record_text, series_key):
            matches.append(dict(row))
    return matches[0] if len(matches) == 1 else None


def write_status(status):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(status or {})
    preserved_keys = (IMPORT_READY_STAGE_OUTCOME_KEY, IMPORT_LIFECYCLE_OUTCOME_KEY)
    if any(key not in payload for key in preserved_keys) and RECONCILE_STATUS_PATH.exists():
        try:
            previous = json.loads(RECONCILE_STATUS_PATH.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            previous = {}
        if isinstance(previous, dict):
            for key in preserved_keys:
                if key not in payload and isinstance(previous.get(key), dict):
                    payload[key] = previous[key]
    temporary = RECONCILE_STATUS_PATH.with_name(
        f".{RECONCILE_STATUS_PATH.name}.{os.getpid()}.tmp"
    )
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, RECONCILE_STATUS_PATH)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def carry_failed_download_sync_evidence(sync, refreshed_at):
    if not isinstance(sync, dict):
        return sync
    carried = dict(sync)
    completed_present = "completed_at" in carried and carried.get("completed_at") is not None
    evidence_at = carried.get("completed_at") if completed_present else carried.get("started_at")
    for derived_key in (
        "evidence_age_seconds",
        "evidence_future_by_seconds",
        "evidence_timestamp_missing",
        "evidence_timestamp_invalid",
        "evidence_chronology_invalid",
        "evidence_clock_skew_detected",
        "lock_sample_timestamp_invalid",
        "lock_sample_chronology_invalid",
    ):
        carried.pop(derived_key, None)
    carried.update(
        {
            "carried": True,
            "fresh": False,
            "carried_at": float(refreshed_at) if finite_timestamp(refreshed_at) else None,
        }
    )

    def omit_ephemeral_lock_evidence(reason):
        carried.pop("lock_samples", None)
        skipped = dict(carried.get("skipped") or {})
        if "state_db_locked" in skipped:
            skipped.pop("state_db_locked", None)
            carried["ephemeral_lock_evidence_omitted"] = True
        carried["skipped"] = skipped
        carried[reason] = True

    if any(
        carried.get(marker)
        for marker in ("started_timestamp_invalid", "completed_timestamp_invalid", "sample_timestamp_invalid")
    ):
        omit_ephemeral_lock_evidence("evidence_timestamp_invalid")
        return carried
    if evidence_at is None:
        omit_ephemeral_lock_evidence("evidence_timestamp_missing")
        return carried
    if not finite_timestamp(evidence_at) or not finite_timestamp(refreshed_at):
        omit_ephemeral_lock_evidence("evidence_timestamp_invalid")
        return carried
    started_at = carried.get("started_at")
    if started_at is not None and (
        not finite_timestamp(started_at)
        or (completed_present and float(evidence_at) < float(started_at))
    ):
        omit_ephemeral_lock_evidence("evidence_chronology_invalid")
        return carried
    if float(evidence_at) > float(refreshed_at):
        carried["evidence_future_by_seconds"] = float(evidence_at) - float(refreshed_at)
        omit_ephemeral_lock_evidence("evidence_clock_skew_detected")
        return carried
    for sample in carried.get("lock_samples") or []:
        sample_at = sample.get("observed_at") if isinstance(sample, dict) else None
        if not finite_timestamp(sample_at):
            omit_ephemeral_lock_evidence("lock_sample_timestamp_invalid")
            return carried
        if (
            (finite_timestamp(started_at) and float(sample_at) < float(started_at))
            or float(sample_at) > float(evidence_at)
            or float(sample_at) > float(refreshed_at)
        ):
            omit_ephemeral_lock_evidence("lock_sample_chronology_invalid")
            return carried
    carried["evidence_age_seconds"] = float(refreshed_at) - float(evidence_at)
    return carried


def refresh_status_from_reconciliation_db():
    if not DB_PATH.exists():
        return {}
    try:
        status = json.loads(RECONCILE_STATUS_PATH.read_text(encoding="utf-8")) if RECONCILE_STATUS_PATH.exists() else {}
    except Exception:
        status = {}
    conn = connect_db()
    try:
        table = conn.execute(
            "select name from sqlite_master where type='table' and name='download_reconciliation'"
        ).fetchone()
        if not table:
            return status
        state_counts = collections.Counter()
        reason_counts = collections.Counter()
        queue_ready_imports = 0
        legacy_ready_imports = 0
        for state, reason, count in conn.execute(
            """
            select lifecycle_state, reason, count(*)
            from download_reconciliation
            group by lifecycle_state, reason
            """
        ):
            state_counts[state] += int(count or 0)
            if state == "bad_archive":
                reason_counts[reason or "unknown"] += int(count or 0)
        ready_split = conn.execute(
            """
            select
              sum(case when inkdrop_queue_id is not null and length(trim(inkdrop_queue_id)) > 0 then 1 else 0 end) as queue_ready,
              sum(case when inkdrop_queue_id is null or length(trim(coalesce(inkdrop_queue_id, ''))) = 0 then 1 else 0 end) as legacy_ready
            from download_reconciliation
            where lifecycle_state='ready_to_import'
              and matched_local_path is not null
              and length(trim(coalesce(matched_local_path, ''))) > 0
            """
        ).fetchone()
        if ready_split:
            queue_ready_imports = int(ready_split[0] or 0)
            legacy_ready_imports = int(ready_split[1] or 0)
    finally:
        conn.close()
    status = dict(status or {})
    refreshed_at = now()
    if "failed_download_sync" in status:
        status["failed_download_sync"] = carry_failed_download_sync_evidence(
            status.get("failed_download_sync"),
            refreshed_at,
        )
    status.update(
        {
            "updated_at": refreshed_at,
            "scan_mode": "db_refresh",
            "deep_scan": False,
            "counts": dict(sorted(state_counts.items())),
            "active_downloads": state_counts.get("queued", 0)
            + state_counts.get("downloading", 0)
            + state_counts.get("stalled_downloading", 0),
            "ready_imports": state_counts.get("ready_to_import", 0),
            "queue_ready_imports": queue_ready_imports,
            "legacy_ready_imports": legacy_ready_imports,
            "unowned_ready_imports": legacy_ready_imports,
            "pending_ready_imports": state_counts.get("ready_to_import", 0),
            "local_ready_imports": 0,
            "waiting_for_scan": state_counts.get("waiting_for_library_scan", 0) + state_counts.get("waiting_for_kavita_scan", 0),
            "verified_recently": state_counts.get("verified", 0) + state_counts.get("imported", 0),
            "failed_downloads": state_counts.get("failed_download", 0),
            "failed_imports": state_counts.get("failed_import", 0) + state_counts.get("bad_archive", 0) + state_counts.get("wrong_series_or_subseries", 0),
            "stale_pending": state_counts.get("stale_no_local_file", 0),
            "manual_review": state_counts.get("manual_review", 0),
            "suppressed_completed": state_counts.get("suppressed_completed", 0),
            "bad_archive_reasons": dict(sorted(reason_counts.items())),
        }
    )
    write_status(status)
    return status


def acquire_reconcile_lock(wait_seconds=0):
    import fcntl

    handle = RECONCILE_LOCK_PATH.open("w", encoding="utf-8")
    try:
        wait = max(0.0, float(wait_seconds or 0))
    except (TypeError, ValueError):
        wait = 0.0
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if wait <= 0 or time.monotonic() >= deadline:
                    raise
                time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if wait <= 0 or time.monotonic() >= deadline:
                    raise BlockingIOError(exc.errno, str(exc))
                time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
    except Exception:
        handle.close()
        raise
    return handle, fcntl


def _import_ready_stage_result_summary(result):
    summary = {}
    if isinstance(result, dict):
        if "ok" in result:
            summary["result_ok"] = bool(result.get("ok"))
        allowed_counts = (
            "checked", "updated", "promoted", "recovered", "reconciled",
            "imported", "retracted", "state_errors",
        )
        for key in allowed_counts:
            value = result.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                summary[key] = max(0, min(value, 1_000_000_000))
    return summary


def _import_ready_stage_error_category(exc):
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, sqlite3.Error):
        return "database"
    if isinstance(exc, OSError):
        return "io"
    return "unexpected"


def persist_import_ready_stage_outcomes(outcome):
    try:
        current = json.loads(RECONCILE_STATUS_PATH.read_text(encoding="utf-8")) if RECONCILE_STATUS_PATH.exists() else {}
    except (OSError, TypeError, ValueError):
        current = {}
    current = current if isinstance(current, dict) else {}
    current[IMPORT_READY_STAGE_OUTCOME_KEY] = dict(outcome or {})
    write_status(current)


def _observe_import_ready_stage(stage_name, stage_call):
    stage_started = time.monotonic()
    try:
        result = stage_call()
        result_summary = _import_ready_stage_result_summary(result)
        result_ok = result_summary.get("result_ok", True)
        stage = {
            "stage": stage_name,
            "status": "succeeded" if result_ok else "failed",
            "ok": bool(result_ok),
            **result_summary,
        }
        if not result_ok:
            stage["error_code"] = "stage_reported_failure"
    except Exception as exc:  # noqa: BLE001 - stages remain isolated and failure is durably surfaced
        result = {
            "ok": False,
            "error_code": "stage_exception",
            "error_category": _import_ready_stage_error_category(exc),
        }
        stage = {
            "stage": stage_name,
            "status": "failed",
            **result,
        }
    stage["duration_ms"] = max(0, min(int((time.monotonic() - stage_started) * 1000), 3_600_000))
    return result, stage


def run_import_ready_reconciliation_stages(sync_record_limit, *, preliminary_stages=None, started_at=None):
    global LAST_IMPORT_READY_STAGE_OUTCOMES
    max_records = max(1, min(int(sync_record_limit or 1), 100_000))
    stage_calls = (
        ("recover_retryable_failed_staged", lambda: recover_retryable_failed_staged_import_ready_records(max_records=max_records)),
        ("sync_state_import_ready", lambda: sync_inkdrop_import_ready_records(max_records=max_records)),
        ("sync_import_results", sync_reconciliation_from_inkdrop_import_results),
        ("recover_import_timeouts", recover_import_ready_timeouts_from_imported_files),
        ("claim_suppressed_completed", lambda: claim_suppressed_completed_import_authorities(limit=max_records)),
        ("sync_reconciled_to_state", sync_inkdrop_from_reconciled_imports),
        ("promote_complete_deferred", lambda: promote_complete_deferred_import_ready_records(max_records=max_records)),
        ("recover_filename_guard", lambda: recover_failed_filename_guard_import_ready_records(max_records=max_records)),
    )
    started_at = now() if started_at is None else started_at
    stages = [dict(stage) for stage in (preliminary_stages or []) if isinstance(stage, dict)][:10]
    for stage_name, stage_call in stage_calls:
        _result, stage = _observe_import_ready_stage(stage_name, stage_call)
        stages.append(stage)
    failed = sum(1 for stage in stages if not stage.get("ok"))
    outcome = {
        "contract_version": IMPORT_READY_STAGE_OUTCOME_CONTRACT_VERSION,
        "started_at": started_at,
        "completed_at": now(),
        "ok": failed == 0,
        "partial": failed > 0,
        "stage_count": len(stages),
        "failed_stage_count": failed,
        "stages": stages,
    }
    LAST_IMPORT_READY_STAGE_OUTCOMES = outcome
    persist_import_ready_stage_outcomes(outcome)
    return outcome


def finalize_import_ready_stage_outcomes(preliminary_stages, started_at):
    global LAST_IMPORT_READY_STAGE_OUTCOMES
    current = dict(LAST_IMPORT_READY_STAGE_OUTCOMES or {})
    stages = [dict(stage) for stage in (preliminary_stages or []) if isinstance(stage, dict)][:10]
    preliminary_names = {stage.get("stage") for stage in stages}
    stages.extend(
        dict(stage)
        for stage in (current.get("stages") or [])
        if isinstance(stage, dict) and stage.get("stage") not in preliminary_names
    )
    stages = stages[:20]
    failed = sum(1 for stage in stages if not stage.get("ok"))
    outcome = {
        "contract_version": IMPORT_READY_STAGE_OUTCOME_CONTRACT_VERSION,
        "started_at": started_at,
        "completed_at": now(),
        "ok": failed == 0,
        "partial": failed > 0,
        "stage_count": len(stages),
        "failed_stage_count": failed,
        "stages": stages,
    }
    LAST_IMPORT_READY_STAGE_OUTCOMES = outcome
    persist_import_ready_stage_outcomes(outcome)
    return outcome


def ready_import_records(max_files):
    global PENDING_IMPORT_READY_PRELIMINARY_STAGES, PENDING_IMPORT_READY_STARTED_AT
    if not DB_PATH.exists():
        return []
    ensure_reconciliation_table()
    sync_record_limit = max(int(max_files) * INKDROP_RECONCILED_IMPORT_SYNC_LIMIT, int(max_files))
    preliminary_stages = PENDING_IMPORT_READY_PRELIMINARY_STAGES
    preliminary_started_at = PENDING_IMPORT_READY_STARTED_AT
    PENDING_IMPORT_READY_PRELIMINARY_STAGES = None
    PENDING_IMPORT_READY_STARTED_AT = None
    run_import_ready_reconciliation_stages(
        sync_record_limit,
        preliminary_stages=preliminary_stages,
        started_at=preliminary_started_at,
    )
    eligible_inkdrop_rows = inkdrop_import_ready_rows(sync_record_limit)
    eligible_inkdrop_by_task = {
        (str(row.get("queue_id")), str(row.get("download_task_id"))): row
        for row in eligible_inkdrop_rows
        if row.get("queue_id") and row.get("download_task_id")
    }
    eligible_inkdrop_queue_ids = {
        str(row.get("queue_id")) for row in eligible_inkdrop_rows if row.get("queue_id")
    }
    imported_state = load_imported_state()
    incomplete_qbit_paths = None

    def qbit_incomplete_paths():
        nonlocal incomplete_qbit_paths
        if incomplete_qbit_paths is None:
            try:
                incomplete_qbit_paths = set(imp.load_qbit_incomplete_paths("comics"))
            except Exception:
                incomplete_qbit_paths = set()
        return incomplete_qbit_paths

    conn = connect_db()
    try:
        scan_limit = max(int(max_files) * 50, int(max_files))
        queue_only = bool(IMPORT_READY_QUEUE_ONLY)
        rows = conn.execute(
            """
            select pending_key, matched_local_path, trusted_series_id, trusted_issue,
                   inkdrop_queue_id, inkdrop_download_task_id, matched_series, title, query, client,
                   updated_at, completed_seen_at
            from download_reconciliation
            where lifecycle_state = 'ready_to_import'
              and matched_local_path is not null
            order by case when inkdrop_queue_id is not null and length(trim(inkdrop_queue_id)) > 0 then 0 else 1 end,
                     coalesce(updated_at, completed_seen_at) desc,
                     pending_key asc
            limit ?
            """,
            (scan_limit,),
        ).fetchall()
        rows = sorted(
            rows,
            key=lambda row: (
                0 if str(row[4] or "") in eligible_inkdrop_queue_ids else 1,
                import_ready_batch_priority(row[1], row[0], row[6], row[7], row[8]),
                import_ready_client_priority(row[9]),
                -(float(row[10] or row[11] or 0)),
                str(row[0] or ""),
            ),
        )
        records = []
        seen = set()
        broad_pack_counts = collections.Counter()
        for row in rows:
            pending_key = row[0]
            path = row[1]
            if len(records) >= int(max_files):
                break
            if not path or path in seen:
                continue
            source = Path(path)
            if not (source.exists() and source.is_file()):
                continue
            if str(source) in qbit_incomplete_paths():
                validated_incomplete_child = False
                archive = None
                if (
                    inkdrop_reconciliation_client(row[9]) in set(INKDROP_STAGED_SOURCE_CLIENTS)
                    and str(row[4] or "").strip() in eligible_inkdrop_queue_ids
                    and source.suffix.lower() in ARCHIVE_SUFFIXES
                ):
                    try:
                        archive = imp.validate_comic_archive(source)
                    except Exception as exc:
                        archive = {"ok": False, "reason": str(exc)}
                    validated_incomplete_child = bool(archive.get("ok"))
                    if validated_incomplete_child:
                        # Broad pack torrents can report incomplete while an
                        # individual child archive is already fully readable.
                        # Let validated exact child files drain through the
                        # normal import path.
                        pass
                if not validated_incomplete_child:
                    if archive and archive.get("reason"):
                        remember_archive_validation_failure(pending_key, source, archive.get("reason"))
                    try:
                        conn.execute(
                            """
                            update download_reconciliation
                               set lifecycle_state='downloading',
                                   reason='source_file_incomplete_qbit_download',
                                   updated_at=?
                             where pending_key=?
                               and matched_local_path=?
                            """,
                            (now(), pending_key, path),
                        )
                        conn.commit()
                        try:
                            record_inkdrop_import_ready_deferral(
                                row[4],
                                row[5],
                                "source_file_incomplete_qbit_download",
                                source_path=path,
                                client=row[9],
                            )
                        except Exception:
                            pass
                    except sqlite3.OperationalError as exc:
                        if "locked" not in str(exc).lower():
                            raise
                    continue
            source_text = str(source)
            imported_already = source_text in imported_state.get("source_paths", set()) or source_text in imported_state.get("dest_paths", set())
            queue_id = str(row[4] or "").strip()
            if queue_only and not queue_id:
                continue
            if imported_already or (queue_id and queue_id not in eligible_inkdrop_queue_ids):
                try:
                    conn.execute(
                        """
                        update download_reconciliation
                           set lifecycle_state=?,
                               reason=?,
                               updated_at=?
                         where pending_key=?
                           and matched_local_path=?
                        """,
                        (
                            "imported" if imported_already else LIBRARY_SCAN_WAIT_STATE,
                            "source_already_imported" if imported_already else "inkdrop_queue_no_longer_import_ready",
                            now(),
                            pending_key,
                            path,
                        ),
                    )
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                continue
            broad_pack_key = import_ready_broad_pack_key(path, pending_key, row[6], row[7], row[8], row[9])
            if (
                broad_pack_key
                and broad_pack_counts[broad_pack_key] >= INKDROP_IMPORT_READY_MAX_PER_BROAD_PACK_PER_BATCH
            ):
                continue
            record_payload = {
                "pending_key": pending_key,
                "source_file": str(source),
                "trusted_series_id": row[2],
                "trusted_issue": row[3],
                "inkdrop_queue_id": row[4],
                "inkdrop_download_task_id": row[5],
                "matched_series": row[6],
                "title": row[7],
                "query": row[8],
                "client": row[9],
            }
            authority_row = eligible_inkdrop_by_task.get((queue_id, str(row[5] or ""))) or {}
            if authority_row:
                record_payload.update(
                    {
                        "inkdrop_source_attempt_id": authority_row.get("source_attempt_id"),
                        "inkdrop_external_id": authority_row.get("external_id"),
                        "inkdrop_candidate_identity": authority_row.get("candidate_identity"),
                        "inkdrop_download_client": authority_row.get("download_client"),
                        "inkdrop_task_local_path": authority_row.get("local_path"),
                    }
                )
            identity = inkdrop_queue_identity_row(queue_id) if queue_id else {}
            if identity.get("issue_title"):
                record_payload["trusted_issue_title"] = identity.get("issue_title")
            if identity.get("trusted_issue_id"):
                record_payload["trusted_issue_id"] = identity.get("trusted_issue_id")
            if identity.get("trusted_issue") and not record_payload.get("trusted_issue"):
                record_payload["trusted_issue"] = identity.get("trusted_issue")
            if queue_id and queue_id in eligible_inkdrop_queue_ids:
                records.append(record_payload)
                seen.add(path)
                if broad_pack_key:
                    broad_pack_counts[broad_pack_key] += 1
                continue
            try:
                archive = imp.validate_comic_archive(source)
            except Exception as exc:
                archive = {"ok": False, "reason": str(exc)}
            if not archive.get("ok"):
                reason = archive.get("reason") or "bad_archive"
                remember_archive_validation_failure(pending_key, source, reason)
                _, local_size, local_mtime = local_file_identity(source)
                try:
                    conn.execute(
                        """
                        update download_reconciliation
                           set lifecycle_state='bad_archive',
                               reason=?,
                               matched_local_size=?,
                               matched_local_mtime=?,
                               updated_at=?
                         where pending_key=?
                           and matched_local_path=?
                        """,
                        (reason, local_size, local_mtime, now(), pending_key, path),
                    )
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                continue
            records.append(record_payload)
            seen.add(path)
            if broad_pack_key:
                broad_pack_counts[broad_pack_key] += 1
        return records
    finally:
        conn.close()


def ready_import_paths(max_files):
    return [record["source_file"] for record in ready_import_records(max_files)]


def sync_reconciliation_from_inkdrop_import_results(limit=1000):
    if not (DB_PATH.exists() and INKDROP_STATE_DB.exists()):
        return {"ok": False, "reason": "db_missing", "updated": 0}
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_unavailable", "updated": 0}
    with inkdrop_state.connect_read(
        INKDROP_STATE_DB,
        timeout_seconds=max(INKDROP_STATE_READ_TIMEOUT_SECONDS, 10.0),
        busy_timeout_ms=max(INKDROP_STATE_READ_BUSY_TIMEOUT_MS, 10000),
    ) as state_conn:
        rows = state_conn.execute(
            """
            select source_path, dest_path, status, verified, created_at
            from import_results
            where source_path is not null
              and length(trim(source_path)) > 0
            order by coalesce(verified, 0) desc, coalesce(created_at, 0) desc
            limit ?
            """,
            (int(limit),),
        ).fetchall()
    by_source = {}
    for row in rows:
        source_path = str(row["source_path"] or "").strip()
        if not source_path or source_path in by_source:
            continue
        by_source[source_path] = row
    if not by_source:
        return {"ok": True, "updated": 0}
    conn = connect_db()
    updated = 0
    ts = now()
    try:
        for path, row in by_source.items():
            verified = bool(row["verified"])
            state = "verified" if verified else LIBRARY_SCAN_WAIT_STATE
            reason = str(row["status"] or ("library_visible" if verified else LIBRARY_SCAN_WAIT_STATE))
            reason = canonical_library_scan_reason(reason)
            cur = conn.execute(
                """
                update download_reconciliation
                   set lifecycle_state=?,
                       reason=?,
                       imported_at=coalesce(imported_at, ?),
                       verified_at=case when ? then coalesce(verified_at, ?) else verified_at end,
                       updated_at=?
                 where matched_local_path=?
                   and lifecycle_state in ('ready_to_import','waiting_for_library_scan','waiting_for_kavita_scan','imported')
                """,
                (
                    state,
                    reason,
                    row["created_at"] or ts,
                    1 if verified else 0,
                    row["created_at"] or ts,
                    ts,
                    path,
                ),
            )
            updated += int(cur.rowcount or 0)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "updated": updated}


def imported_file_rows_by_source(paths):
    wanted = {str(path or "").strip() for path in paths or [] if str(path or "").strip()}
    if not wanted or not DB_PATH.exists():
        return {}
    conn = connect_db()
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                f"""
                select source, dest, imported_at
                from imported_files
                where source in ({','.join('?' for _ in wanted)})
                   or dest in ({','.join('?' for _ in wanted)})
                """,
                tuple(wanted) + tuple(wanted),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            rows = []
    finally:
        conn.close()
    matches = {}
    for row in rows:
        source = str(row["source"] or "").strip()
        dest = str(row["dest"] or "").strip()
        payload = {"source": source, "dest": dest, "imported_at": row["imported_at"]}
        if source:
            matches[source] = payload
        if dest:
            matches[dest] = payload
    return matches


def suppressed_completed_existing_path_row(lifecycle_state, reason, matched_local_path, updated_at=None):
    if not suppressed_completed_existing_path_state(lifecycle_state, reason):
        return {}
    path_text = str(matched_local_path or "").strip()
    if not path_text:
        return {}
    path = Path(path_text)
    try:
        if not (path.exists() and path.is_file() and is_archive_path(path)):
            return {}
    except OSError:
        return {}
    return {
        "source": path_text,
        "dest": path_text,
        "imported_at": updated_at,
        "existing_path_in_download_staging": download_staging_path(path),
    }


def suppressed_completed_current_managed_path_row(
    identity_row, updated_at=None, managed_folder_cache=None,
):
    identity_row = dict(identity_row or {})
    if inkdrop_state is None or not hasattr(inkdrop_state, "managed_folder_issue_file_presence"):
        return {}
    series_row = {
        "id": identity_row.get("series_id"),
        "title": identity_row.get("matched_series") or identity_row.get("series_title"),
        "media_type": identity_row.get("series_media_type") or identity_row.get("media_type"),
        "year": identity_row.get("series_year") or identity_row.get("year"),
        "publisher": identity_row.get("series_publisher") or identity_row.get("publisher"),
        "metadata_provider": identity_row.get("series_metadata_provider"),
        "metadata_id": identity_row.get("series_metadata_id"),
        "source": identity_row.get("series_source"),
        "library_root": identity_row.get("series_library_root") or identity_row.get("library_root"),
        "library_path": identity_row.get("series_library_path") or identity_row.get("library_path"),
        "library_adapter_path": identity_row.get("series_library_adapter_path") or identity_row.get("library_adapter_path"),
    }
    issue_row = {
        "id": identity_row.get("trusted_issue_id") or identity_row.get("issue_id"),
        "issue_number": identity_row.get("issue_number") or identity_row.get("trusted_issue"),
        "normalized_number": identity_row.get("normalized_number") or identity_row.get("trusted_issue"),
        "title": identity_row.get("issue_title"),
        "release_date": identity_row.get("issue_release_date") or identity_row.get("release_date"),
        "metadata_provider": identity_row.get("issue_metadata_provider"),
        "metadata_id": identity_row.get("issue_metadata_id"),
    }
    try:
        presence = inkdrop_state.managed_folder_issue_file_presence(
            series_row, issue_row, managed_folder_cache=managed_folder_cache
        )
    except Exception:
        return {}
    path_text = str(presence.get("path") or "").strip() if presence.get("present") else ""
    if not path_text:
        return {}
    path = Path(path_text)
    try:
        if not (path.exists() and path.is_file() and is_archive_path(path)):
            return {}
    except OSError:
        return {}
    if hasattr(inkdrop_state, "folder_presence_has_negative_import_proof"):
        try:
            with inkdrop_state.connect_read(
                INKDROP_STATE_DB,
                timeout_seconds=INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS,
                busy_timeout_ms=INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS,
            ) as state_conn:
                if inkdrop_state.folder_presence_has_negative_import_proof(
                    state_conn,
                    identity_row.get("queue_id"),
                    identity_row.get("trusted_issue_id") or identity_row.get("issue_id"),
                    path_text,
                ):
                    return {}
        except sqlite3.OperationalError:
            return {}
    return {
        "source": path_text,
        "dest": path_text,
        "imported_at": updated_at,
        "existing_path_in_download_staging": download_staging_path(path),
        "managed_folder_source": str(presence.get("source") or "managed_folder"),
        "managed_folder_confidence": str(presence.get("confidence") or ""),
        "managed_folder_match_reason": str(presence.get("match_reason") or ""),
    }


def suppressed_completed_authoritative_existing_path_row(
    lifecycle_state, reason, matched_local_path, identity_row, updated_at=None,
    managed_folder_cache=None,
):
    existing = suppressed_completed_existing_path_row(
        lifecycle_state, reason, matched_local_path, updated_at
    )
    if existing or not suppressed_completed_existing_path_state(lifecycle_state, reason):
        return existing
    return suppressed_completed_current_managed_path_row(
        identity_row, updated_at, managed_folder_cache=managed_folder_cache
    )


def claim_suppressed_completed_import_authorities(limit=INKDROP_RECONCILED_IMPORT_SYNC_LIMIT):
    if inkdrop_state is None or not (DB_PATH.exists() and INKDROP_STATE_DB.exists()):
        return {"ok": False, "reason": "db_missing", "checked": 0, "claimed": 0}
    max_rows = max(1, min(int(limit or INKDROP_RECONCILED_IMPORT_SYNC_LIMIT), INKDROP_RECONCILED_IMPORT_SYNC_LIMIT))
    ensure_reconciliation_table()
    conn = connect_db()
    try:
        rows = conn.execute(
            """
            select pending_key, lifecycle_state, reason, matched_local_path, matched_series,
                   trusted_series_id, trusted_issue, inkdrop_queue_id, inkdrop_download_task_id,
                   client, updated_at, title, query, unit_model, truth_model
              from download_reconciliation
             where inkdrop_queue_id is not null
               and length(trim(inkdrop_queue_id)) > 0
               and inkdrop_download_task_id is not null
               and length(trim(inkdrop_download_task_id)) > 0
               and matched_local_path is not null
               and length(trim(matched_local_path)) > 0
               and lifecycle_state='suppressed_completed'
             order by coalesce(updated_at,0) desc, pending_key asc
             limit ?
            """,
            (max_rows,),
        ).fetchall()
    finally:
        conn.close()
    checked = 0
    claimed = 0
    skipped = collections.Counter()
    errors = []
    managed_folder_cache = {}
    for row in rows:
        checked += 1
        (
            pending_key, lifecycle_state, reason, matched_local_path, matched_series,
            trusted_series_id, trusted_issue, queue_id, task_id, client, updated_at,
            title, query, unit_model, truth_model,
        ) = row
        identity_row = dict(inkdrop_queue_identity_row(queue_id) or {})
        identity_row.update({
            "matched_series": identity_row.get("matched_series") or matched_series,
            "trusted_issue": identity_row.get("trusted_issue") or trusted_issue,
            "matched_local_path": matched_local_path,
            "title": title,
            "query": query,
            "unit_model": unit_model,
            "truth_model": truth_model,
            "pending_key": pending_key,
        })
        existing = suppressed_completed_authoritative_existing_path_row(
            lifecycle_state, reason, matched_local_path, identity_row, updated_at,
            managed_folder_cache=managed_folder_cache,
        )
        if not existing:
            skipped["existing_destination_unavailable"] += 1
            continue
        if existing.get("existing_path_in_download_staging"):
            skipped["existing_path_in_download_staging"] += 1
            continue
        identity_ok, identity_reason = imported_file_identity_match(identity_row, existing)
        if not identity_ok:
            skipped[identity_reason] += 1
            continue
        try:
            with inkdrop_state.connect_read(
                INKDROP_STATE_DB,
                timeout_seconds=INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS,
                busy_timeout_ms=INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS,
            ) as state_conn:
                task = state_conn.execute(
                    """
                    select source_attempt_id, external_id, candidate_identity,
                           download_client, local_path
                      from download_tasks
                     where id=? and queue_id=?
                     limit 1
                    """,
                    (task_id, queue_id),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if not sqlite_lock_error(exc):
                raise
            skipped["state_db_locked"] += 1
            continue
        if not task:
            skipped["download_task_not_found"] += 1
            continue
        try:
            result = with_sqlite_lock_retry(
                lambda: inkdrop_state.claim_import_authority(
                    INKDROP_STATE_DB,
                    queue_id,
                    task_id,
                    source_attempt_id=task["source_attempt_id"],
                    external_id=task["external_id"],
                    candidate_identity=task["candidate_identity"],
                    download_client=task["download_client"] or client,
                    local_path=task["local_path"],
                    lock_timeout_seconds=INKDROP_REPLAY_STATE_WRITE_TIMEOUT_SECONDS,
                    lock_busy_timeout_ms=INKDROP_REPLAY_STATE_WRITE_BUSY_TIMEOUT_MS,
                ),
                attempts=INKDROP_IMPORT_READY_RECOVERY_WRITE_ATTEMPTS,
                initial_delay=INKDROP_IMPORT_READY_RECOVERY_INITIAL_DELAY_SECONDS,
            )
        except sqlite3.OperationalError as exc:
            if not sqlite_lock_error(exc):
                raise
            skipped["state_db_locked"] += 1
            continue
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        if result.get("ok"):
            claimed += 1
        else:
            skipped[str(result.get("reason") or "claim_failed")] += 1
    return {
        "ok": not errors,
        "checked": checked,
        "claimed": claimed,
        "skipped": dict(sorted(skipped.items())),
        "errors": errors[:5],
    }


def decode_json_object(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def first_payload_value(payloads, *keys):
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
    return None


def normalized_backfill_unit(value):
    unit = str(value or "").strip().lower()
    if unit in {"mixed_volume_preferred", "mixed_chapter_preferred"}:
        return "volume"
    if unit in {"chapter", "volume", "pack"}:
        return unit
    return ""


def verified_manga_backfill_unit(row, payloads):
    explicit = normalized_backfill_unit(
        first_payload_value(
            payloads,
            "source_unit",
            "manga_unit_model",
            "series_unit_model",
            "unit_model",
            "unit_type",
        )
    )
    if explicit:
        return explicit
    series_title = str(row["series_title"] or "").strip()
    if not series_title or not hasattr(imp, "manga_unit_model_for_target"):
        return ""
    try:
        return normalized_backfill_unit(imp.manga_unit_model_for_target({"title": series_title}))
    except Exception:
        return ""


def prefixed_metadata_identity(provider, metadata_id, fallback=None):
    provider = str(provider or "").strip().lower()
    metadata_id = str(metadata_id or "").strip()
    if provider in {"comicvine", "mangadex"} and metadata_id:
        return f"{provider}:{metadata_id}"
    return str(fallback or "").strip() or None


def backfill_verified_manga_import_results(limit=INKDROP_MANGA_COMPLETION_BACKFILL_LIMIT):
    if (
        inkdrop_state is None
        or not INKDROP_STATE_DB.exists()
        or int(limit or 0) <= 0
        or not hasattr(imp, "record_manga_unit_completion")
        or not hasattr(imp, "record_manga_coverage")
    ):
        return {"checked": 0, "backfilled": 0, "skipped": {"disabled": 1}}
    max_rows = max(1, min(int(limit), INKDROP_MANGA_COMPLETION_BACKFILL_LIMIT or int(limit)))
    skipped = collections.Counter()
    errors = []
    rows = []
    try:
        with inkdrop_state.connect_read(
            INKDROP_STATE_DB,
            timeout_seconds=max(INKDROP_STATE_READ_TIMEOUT_SECONDS, 10.0),
            busy_timeout_ms=max(INKDROP_STATE_READ_BUSY_TIMEOUT_MS, 10000),
        ) as con:
            rows = con.execute(
                """
                select ir.id as import_result_id, ir.queue_id, ir.series_id as import_series_id,
                       ir.issue_id as import_issue_id, ir.source_path, ir.dest_path, ir.status,
                       ir.verified, ir.library_visibility_status, ir.library_visibility_provider,
                       ir.raw_json as import_raw_json, ir.created_at,
                       q.series_id as queue_series_id, q.issue_id as queue_issue_id,
                       q.query as queue_query, q.raw_json as queue_raw_json,
                       s.id as series_id, s.title as series_title, s.media_type,
                       s.metadata_provider, s.metadata_id, s.source as series_source,
                       i.id as issue_id, i.issue_number, i.normalized_number,
                       i.title as issue_title, i.raw_json as issue_raw_json
                  from import_results ir
                  left join queue_items q on q.id = ir.queue_id
                  left join series s on s.id = coalesce(ir.series_id, q.series_id)
                  left join issues i on i.id = coalesce(ir.issue_id, q.issue_id)
                 where lower(coalesce(s.media_type, '')) = 'manga'
                   and coalesce(trim(ir.dest_path), '') != ''
                   and (
                        lower(coalesce(ir.status, '')) = 'kavita_verified'
                        or lower(coalesce(ir.library_visibility_status, '')) = 'library_visible'
                   )
                 order by coalesce(ir.created_at, 0) desc, ir.id desc
                 limit ?
                """,
                (max_rows,),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if sqlite_lock_error(exc):
            return {"checked": 0, "backfilled": 0, "skipped": {"state_db_locked": 1}}
        if "no such table" in str(exc).lower() or "no such column" in str(exc).lower():
            return {"checked": 0, "backfilled": 0, "skipped": {"schema_unavailable": 1}, "error": str(exc)}
        raise
    backfilled = 0
    unit_rows = 0
    coverage_rows = 0
    for row in rows:
        dest_path = str(row["dest_path"] or "").strip()
        if not dest_path:
            skipped["missing_dest"] += 1
            continue
        try:
            if not Path(dest_path).exists():
                skipped["dest_missing"] += 1
                continue
        except (OSError, ValueError):
            skipped["dest_unreadable"] += 1
            continue
        import_payload = decode_json_object(row["import_raw_json"])
        import_raw = decode_json_object(import_payload.get("raw"))
        queue_raw = decode_json_object(row["queue_raw_json"])
        issue_raw = decode_json_object(row["issue_raw_json"])
        payloads = (import_raw, import_payload, queue_raw, issue_raw)
        unit_model = verified_manga_backfill_unit(row, payloads)
        if not unit_model:
            skipped["unknown_unit_model"] += 1
            continue
        series_title = str(
            row["series_title"]
            or first_payload_value(payloads, "matched_series", "series", "title")
            or row["queue_query"]
            or ""
        ).strip()
        if not series_title:
            skipped["missing_series"] += 1
            continue
        normalized_number = (
            str(row["normalized_number"] or "").strip()
            or str(first_payload_value(payloads, "normalized_number", "canonical_number") or "").strip()
        )
        issue_number = (
            str(row["issue_number"] or "").strip()
            or str(first_payload_value(payloads, "trusted_issue", "issue_number", "canonical_issue_number") or "").strip()
            or normalized_number
        )
        metadata_provider = str(
            first_payload_value(payloads, "metadata_provider", "metadataProvider")
            or row["metadata_provider"]
            or ""
        ).strip().lower()
        metadata_id = str(
            first_payload_value(payloads, "metadata_id", "metadataId")
            or row["metadata_id"]
            or ""
        ).strip()
        native_series_id = (
            str(first_payload_value(payloads, "native_series_id", "trusted_series_id") or "").strip()
            or prefixed_metadata_identity(metadata_provider, metadata_id, row["series_id"])
        )
        item = {
            "source": str(row["source_path"] or "").strip(),
            "dest": dest_path,
            "matched_series": series_title,
            "issue_number": issue_number,
            "canonical_issue_number": issue_number,
            "normalized_number": normalized_number,
            "source_unit": unit_model,
            "manga_unit_model": unit_model,
            "native_series_id": native_series_id,
            "native_issue_id": str(row["issue_id"] or "").strip() or None,
            "metadata_provider": metadata_provider or None,
            "metadata_id": metadata_id or None,
        }
        result = {
            "truth_model": "kavita_manga",
            "verification_status": "library_visible",
            "series": series_title,
            "dest": dest_path,
            "source_unit": unit_model,
            "manga_unit_model": unit_model,
            "library_visible": True,
            "kavita_visible": str(row["library_visibility_provider"] or "").strip().lower() == "kavita",
            "comicinfo_status": str(first_payload_value(payloads, "comicinfo_status") or "not_checked"),
            "native_series_id": native_series_id,
            "metadata_provider": metadata_provider or None,
            "metadata_id": metadata_id or None,
        }
        try:
            wrote_unit = bool(imp.record_manga_unit_completion(item, result))
            wrote_coverage = bool(imp.record_manga_coverage(item, result))
        except Exception as exc:  # noqa: BLE001 - backfill must not block imports
            errors.append(f"{type(exc).__name__}: {exc}")
            skipped["write_failed"] += 1
            continue
        if wrote_unit:
            unit_rows += 1
        if wrote_coverage:
            coverage_rows += 1
        if wrote_unit or wrote_coverage:
            backfilled += 1
    return {
        "ok": not errors,
        "checked": len(rows),
        "backfilled": backfilled,
        "manga_unit_completion_rows": unit_rows,
        "manga_coverage_rows": coverage_rows,
        "skipped": dict(sorted(skipped.items())),
        "errors": errors[:5],
    }


def stale_completion_retraction_message(result):
    retracted = int((result or {}).get("retracted") or 0)
    tables = (result or {}).get("tables") if isinstance((result or {}).get("tables"), dict) else {}
    reason_counts = collections.Counter()
    table_bits = []
    for table, summary in tables.items():
        if not isinstance(summary, dict):
            continue
        count = int(summary.get("retracted") or 0)
        if count > 0:
            table_bits.append(f"{table}:{count}")
        reasons = summary.get("reasons") if isinstance(summary.get("reasons"), dict) else {}
        for reason, value in reasons.items():
            reason_counts[str(reason)] += int(value or 0)
    bits = [f"Retracted {retracted} stale completion proof row{'s' if retracted != 1 else ''}"]
    if table_bits:
        bits.append(", ".join(table_bits))
    if reason_counts:
        bits.append(", ".join(f"{key}:{value}" for key, value in sorted(reason_counts.items())))
    return " · ".join(bits)


def record_stale_completion_retraction_history(result):
    if inkdrop_state is None:
        return {"ok": False, "skipped": True, "reason": "inkdrop_state_unavailable"}
    if not isinstance(result, dict):
        return {"ok": False, "skipped": True, "reason": "invalid_result"}
    retracted = int(result.get("retracted") or 0)
    if retracted <= 0:
        return {"ok": True, "skipped": True, "reason": "no_rows_retracted", "retracted": 0}
    created_at = float(result.get("updated_at") or now())
    raw = {
        "kind": "stale_completion_retraction",
        "status": "stale_completion_retracted",
        "outcome": "problem",
        "display_phase": "problem",
        "retracted": retracted,
        "checked": int(result.get("checked") or 0),
        "tables": result.get("tables") if isinstance(result.get("tables"), dict) else {},
        "source_db": str(DB_PATH),
    }
    return inkdrop_state.record_history_event(
        INKDROP_STATE_DB,
        event_type="stale_completion_retracted",
        entity_type="import_ledger",
        entity_id="completion_ledger",
        source="import_ready",
        message=stale_completion_retraction_message(result),
        raw=raw,
        created_at=created_at,
        timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS,
        busy_timeout_ms=INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS,
    )


def timeout_recovery_verification(row, imported_row):
    dest = str((imported_row or {}).get("dest") or "").strip()
    if not dest or not Path(dest).exists():
        return {"lifecycle_state": LIBRARY_SCAN_WAIT_STATE, "reason": "imported_after_timeout_waiting_for_library_scan"}
    trusted_series = str((row or {}).get("trusted_series_id") or "").strip()
    imported_item = {
        "source": str((imported_row or {}).get("source") or (row or {}).get("matched_local_path") or ""),
        "dest": dest,
        "matched_series": (row or {}).get("matched_series") or (row or {}).get("title") or (row or {}).get("query"),
        "trusted_series_id": trusted_series,
        "trusted_issue": (row or {}).get("trusted_issue"),
        "metadata_provider": "comicvine" if trusted_series.startswith("comicvine:") else None,
        "metadata_id": trusted_series.split(":", 1)[1] if trusted_series.startswith("comicvine:") else None,
        "truth_model": "inkdrop_native",
    }
    try:
        verification = imp.verify_imported_items([imported_item], poll_kavita=False)
    except Exception as exc:  # noqa: BLE001 - timeout recovery should stay best-effort
        verification = {"error": f"{type(exc).__name__}: {exc}", "checked": []}
    checked = verification.get("checked") if isinstance(verification, dict) else []
    first = checked[0] if checked and isinstance(checked[0], dict) else {}
    verification_status = str(first.get("verification_status") or "").strip().lower()
    if verification_status in IMPORT_VERIFIED_STATUSES:
        return {"lifecycle_state": "verified", "reason": import_result_status_for_lifecycle("verified", verification_status), "verification": verification}
    try:
        sync_frontends = getattr(imp, "sync_library_frontend_folders", None)
        if callable(sync_frontends):
            sync_frontends([str(Path(dest).parent)], force_library_scan_folders=[str(Path(dest).parent)], event_prefix="timeout_recovery_")
        else:
            imp.trigger_kavita_scan_folder(Path(dest).parent, force_library_scan=True)
    except Exception:
        pass
    return {
        "lifecycle_state": LIBRARY_SCAN_WAIT_STATE,
        "reason": "imported_after_timeout_waiting_for_library_scan",
        "verification": verification,
    }


def update_timeout_recovered_download_task(row, lifecycle_state, reason, ts):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return False
    task_id = str((row or {}).get("inkdrop_download_task_id") or "").strip()
    if not task_id:
        return False
    try:
        with inkdrop_state.connect(
            INKDROP_STATE_DB,
            timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS,
            busy_timeout_ms=INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS,
        ) as con:
            inkdrop_state.init_schema(con)
            existing = con.execute("select raw_json from download_tasks where id=?", (task_id,)).fetchone()
            raw = {}
            if existing:
                try:
                    raw = json.loads(existing[0] or "{}")
                except ValueError:
                    raw = {}
            raw = raw if isinstance(raw, dict) else {}
            raw.update(
                {
                    "timeout_recovered": True,
                    "timeout_recovered_at": ts,
                    "timeout_recovered_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                    "timeout_recovery_reason": reason,
                    "previous_failure_reason": raw.get("failure_reason") or "import_ready_import_timeout",
                }
            )
            cur = con.execute(
                """
                update download_tasks
                   set status='staged_file_ready',
                       state='import_ready',
                       lifecycle_phase='import_ready',
                       failure_reason=null,
                       retry_eligible=0,
                       updated_at=?,
                       completed_at=coalesce(completed_at, ?),
                       raw_json=?
                 where id=?
                   and lower(coalesce(state,''))='failed'
                   and lower(coalesce(failure_reason,''))='import_ready_import_timeout'
                """,
                (ts, ts, json.dumps(raw, sort_keys=True), task_id),
            )
            con.commit()
            return bool(cur.rowcount)
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        return False


def recover_import_ready_timeouts_from_imported_files(limit=100):
    if not DB_PATH.exists():
        return {"checked": 0, "recovered": 0}
    ensure_reconciliation_table()
    conn = connect_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select pending_key, title, query, client, lifecycle_state, reason,
                   matched_local_path, matched_series, trusted_series_id, trusted_issue,
                   inkdrop_queue_id, inkdrop_download_task_id, imported_at, verified_at, updated_at
            from download_reconciliation
            where lifecycle_state='failed_import'
              and reason='import_ready_import_timeout'
              and matched_local_path is not null
              and length(trim(matched_local_path)) > 0
            order by coalesce(updated_at, 0) desc
            limit ?
            """,
            (max(1, min(int(limit or 100), 1000)),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"checked": 0, "recovered": 0}
    row_dicts = [dict(row) for row in rows]
    imported_by_path = imported_file_rows_by_source([row.get("matched_local_path") for row in row_dicts])
    recovered = 0
    task_updates = 0
    waiting_for_scan = 0
    verified = 0
    ts = now()
    conn = connect_db()
    try:
        for row in row_dicts:
            imported_row = imported_by_path.get(str(row.get("matched_local_path") or "").strip())
            if not imported_row:
                continue
            identity_row = inkdrop_queue_identity_row(row.get("inkdrop_queue_id")) or row
            identity_row = dict(identity_row)
            identity_row.setdefault("matched_local_path", row.get("matched_local_path"))
            identity_ok, _identity_reason = imported_file_identity_match(identity_row, imported_row)
            if not identity_ok:
                continue
            recovery = timeout_recovery_verification(row, imported_row)
            lifecycle_state = recovery.get("lifecycle_state") or LIBRARY_SCAN_WAIT_STATE
            reason = canonical_library_scan_reason(recovery.get("reason") or lifecycle_state)
            imported_at = imported_row.get("imported_at") or row.get("imported_at") or ts
            verified_at = ts if lifecycle_state == "verified" else row.get("verified_at")
            conn.execute(
                """
                update download_reconciliation
                   set lifecycle_state=?,
                       reason=?,
                       imported_at=coalesce(imported_at, ?),
                       verified_at=case when ?='verified' then coalesce(verified_at, ?) else verified_at end,
                       updated_at=?
                 where pending_key=?
                   and matched_local_path=?
                """,
                (
                    lifecycle_state,
                    reason,
                    imported_at,
                    lifecycle_state,
                    verified_at or ts,
                    ts,
                    row.get("pending_key"),
                    row.get("matched_local_path"),
                ),
            )
            recovered += 1
            if lifecycle_state == "verified":
                verified += 1
            else:
                waiting_for_scan += 1
            if update_timeout_recovered_download_task(row, lifecycle_state, reason, ts):
                task_updates += 1
        conn.commit()
    finally:
        conn.close()
    return {
        "checked": len(row_dicts),
        "recovered": recovered,
        "verified": verified,
        "waiting_for_scan": waiting_for_scan,
        "task_updates": task_updates,
    }


def active_import_ready_rows(limit=300):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return []
    max_rows = max(1, min(int(limit or 300), 1000))
    with inkdrop_state.connect_read(
        INKDROP_STATE_DB,
        timeout_seconds=INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS,
        busy_timeout_ms=INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS,
    ) as con:
        rows = con.execute(
            """
            select q.id as inkdrop_queue_id,
                   q.state as queue_state,
                   q.current_source,
                   s.id as series_id,
                   s.title as matched_series,
                   s.metadata_provider as series_metadata_provider,
                   s.metadata_id as series_metadata_id,
                   i.issue_number as trusted_issue,
                   dt.id as inkdrop_download_task_id,
                   dt.local_path as matched_local_path,
                   dt.download_client as client,
                   dt.provider,
                   dt.protocol,
                   dt.title,
                   dt.updated_at
            from queue_items q
            join series s on s.id=q.series_id
            left join issues i on i.id=q.issue_id
            join download_tasks dt on dt.queue_id=q.id
            where q.active=1
              and lower(coalesce(q.state, ''))='importing'
              and (
                lower(coalesce(q.current_source, '')) in ('download_client','qbittorrent','sabnzbd')
                or lower(coalesce(dt.download_client, '')) in ('qbittorrent','sabnzbd')
              )
              and (
                (
                  lower(coalesce(dt.state, ''))='import_ready'
                  and lower(coalesce(dt.status, ''))='completed_in_client'
                )
                or (
                  lower(coalesce(dt.state, ''))='importing'
                  and lower(coalesce(dt.status, ''))='import_in_progress'
                  and dt.raw_json like '%"import_authority"%'
                )
              )
              and nullif(trim(coalesce(dt.local_path, '')), '') is not null
            order by coalesce(dt.updated_at, q.updated_at, 0) desc
            limit ?
            """,
            (max_rows,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        provider = str(item.get("series_metadata_provider") or "").strip().lower()
        metadata_id = str(item.get("series_metadata_id") or "").strip()
        if provider and metadata_id:
            item["trusted_series_id"] = f"{provider}:{metadata_id}"
        else:
            item["trusted_series_id"] = str(item.get("series_id") or "").strip()
        item["pending_key"] = item.get("inkdrop_queue_id")
        out.append(item)
    return out


def mark_reconciliation_imported_from_active_row(row, lifecycle_state, reason, imported_row, ts):
    if not DB_PATH.exists():
        return 0
    source_path = str((row or {}).get("matched_local_path") or "").strip()
    if not source_path:
        return 0
    imported_at = (imported_row or {}).get("imported_at") or ts
    verified_at = ts if str(lifecycle_state or "").strip().lower() == "verified" else None
    conn = connect_db()
    try:
        try:
            cur = conn.execute(
                """
                update download_reconciliation
                   set lifecycle_state=?,
                       reason=?,
                       imported_at=coalesce(imported_at, ?),
                       verified_at=case when ?='verified' then coalesce(verified_at, ?) else verified_at end,
                       updated_at=?
                 where matched_local_path=?
                   and lifecycle_state in ('ready_to_import','importing','waiting_for_library_scan','waiting_for_kavita_scan','imported','failed_import')
                """,
                (
                    lifecycle_state,
                    reason,
                    imported_at,
                    lifecycle_state,
                    verified_at or ts,
                    ts,
                    source_path,
                ),
            )
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def recover_active_import_ready_from_imported_files(limit=300):
    if inkdrop_state is None or not (DB_PATH.exists() and INKDROP_STATE_DB.exists()):
        return {"checked": 0, "recovered": 0}
    try:
        rows = active_import_ready_rows(limit)
    except sqlite3.OperationalError as exc:
        if not sqlite_lock_error(exc):
            raise
        return {"checked": 0, "recovered": 0, "skipped": {"state_db_locked": 1}}
    if not rows:
        return {"checked": 0, "recovered": 0}
    imported_by_path = imported_file_rows_by_source(row.get("matched_local_path") for row in rows)
    recovered = 0
    verified = 0
    waiting_for_scan = 0
    task_updates = 0
    reconciliation_updates = 0
    skipped = collections.Counter()
    errors = []
    ts = now()
    for row in rows:
        source_path = str(row.get("matched_local_path") or "").strip()
        imported_row = imported_by_path.get(source_path)
        if not imported_row:
            skipped["not_imported_yet"] += 1
            continue
        identity_ok, identity_reason = imported_file_identity_match(row, imported_row)
        if not identity_ok:
            skipped[identity_reason] += 1
            continue
        recovery = timeout_recovery_verification(row, imported_row)
        lifecycle_state = str(recovery.get("lifecycle_state") or LIBRARY_SCAN_WAIT_STATE).strip().lower()
        reason = canonical_library_scan_reason(recovery.get("reason") or lifecycle_state)
        is_verified = lifecycle_state == "verified"
        status = import_result_status_for_lifecycle(lifecycle_state, reason)
        client = str(row.get("client") or row.get("protocol") or row.get("provider") or "download_client").strip().lower() or "download_client"
        raw = {
            "kind": "direct_import",
            "source": "download_client",
            "provider": client,
            "download_client": client,
            "reason": reason,
            "active_import_ready_imported_file_recovery": True,
            "trusted_series_id": row.get("trusted_series_id"),
            "trusted_issue": row.get("trusted_issue"),
            "matched_series": row.get("matched_series"),
            "download_task_id": row.get("inkdrop_download_task_id"),
            "imported_file_source_path": imported_row.get("source") or source_path,
            "imported_file_dest_path": imported_row.get("dest"),
        }
        import_authority = active_reconciled_import_authority(
            row.get("inkdrop_queue_id"),
            row.get("inkdrop_download_task_id"),
        )
        if not import_authority:
            skipped["import_authority_not_active"] += 1
            continue
        raw["import_ready_reconciliation_replay"] = True
        raw["source_attempt_id"] = import_authority.get("source_attempt_id")
        raw["external_id"] = import_authority.get("external_id")
        raw.update(
            replay_managed_destination_evidence(
                imported_row.get("source") or source_path,
                imported_row.get("dest"),
                reason=reason,
            )
        )
        try:
            result = with_sqlite_lock_retry(
                lambda: inkdrop_state.record_direct_import_result(
                    INKDROP_STATE_DB,
                    row.get("inkdrop_queue_id"),
                    source_path=import_authority.get("local_path") or source_path,
                    dest_path=imported_row.get("dest") or "",
                    source="download_client",
                    status=status,
                    verified=is_verified,
                    imported_count=1,
                    skipped_count=0,
                    raw=raw,
                    import_authority=import_authority,
                    created_at=imported_row.get("imported_at") or ts,
                    read_timeout_seconds=INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS,
                    read_busy_timeout_ms=INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS,
                    lock_timeout_seconds=INKDROP_REPLAY_STATE_WRITE_TIMEOUT_SECONDS,
                    lock_busy_timeout_ms=INKDROP_REPLAY_STATE_WRITE_BUSY_TIMEOUT_MS,
                ),
                attempts=INKDROP_IMPORT_READY_RECOVERY_WRITE_ATTEMPTS,
                initial_delay=INKDROP_IMPORT_READY_RECOVERY_INITIAL_DELAY_SECONDS,
            )
        except sqlite3.OperationalError as exc:
            if not sqlite_lock_error(exc):
                raise
            skipped["state_db_locked"] += 1
            continue
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        if not result.get("ok"):
            skipped[str(result.get("reason") or "not_updated")] += 1
            continue
        recovered += 1
        if is_verified:
            verified += 1
        else:
            waiting_for_scan += 1
        task_updates += int(result.get("download_tasks_updated") or 0)
        reconciliation_updates += mark_reconciliation_imported_from_active_row(
            row,
            lifecycle_state,
            reason,
            imported_row,
            ts,
        )
    return {
        "ok": not errors,
        "checked": len(rows),
        "recovered": recovered,
        "verified": verified,
        "waiting_for_scan": waiting_for_scan,
        "task_updates": task_updates,
        "reconciliation_updates": reconciliation_updates,
        "skipped": dict(sorted(skipped.items())),
        "errors": errors[:5],
    }


def sync_inkdrop_from_reconciled_imports(limit=INKDROP_RECONCILED_IMPORT_SYNC_LIMIT):
    if inkdrop_state is None or not (DB_PATH.exists() and INKDROP_STATE_DB.exists()):
        return {"ok": False, "reason": "db_missing", "updated": 0}
    started = time.monotonic()
    max_rows = max(1, min(int(limit or INKDROP_RECONCILED_IMPORT_SYNC_LIMIT), INKDROP_RECONCILED_IMPORT_SYNC_LIMIT))
    conn = connect_db()
    try:
        rows = conn.execute(
            """
            select pending_key, lifecycle_state, reason, matched_local_path, matched_series,
                   trusted_series_id, trusted_issue, inkdrop_queue_id, inkdrop_download_task_id, client, imported_at,
                   verified_at, updated_at, title, query, unit_model, truth_model
            from download_reconciliation
            where inkdrop_queue_id is not null
              and length(trim(inkdrop_queue_id)) > 0
              and matched_local_path is not null
              and length(trim(matched_local_path)) > 0
              and lifecycle_state in ('waiting_for_library_scan','waiting_for_kavita_scan','imported','verified','suppressed_completed')
            order by coalesce(updated_at, imported_at, verified_at, 0) desc, pending_key asc
            limit ?
            """,
            (max_rows,),
        ).fetchall()
    finally:
        conn.close()
    imported_rows = imported_file_rows_by_source(row[3] for row in rows)
    updated = 0
    skipped = collections.Counter()
    errors = []
    managed_folder_cache = {}
    for index, row in enumerate(rows):
        if time.monotonic() - started >= INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS:
            skipped["budget_exhausted"] += max(1, len(rows) - index)
            break
        (
            pending_key,
            lifecycle_state,
            reason,
            matched_local_path,
            matched_series,
            trusted_series_id,
            trusted_issue,
            inkdrop_queue_id,
            inkdrop_download_task_id,
            client,
            imported_at,
            verified_at,
            updated_at,
            title,
            query,
            unit_model,
            truth_model,
        ) = row
        queue_id = str(inkdrop_queue_id or "").strip()
        if not queue_id:
            skipped["missing_queue_id"] += 1
            continue
        try:
            queue = inkdrop_state.queue_item(
                INKDROP_STATE_DB,
                queue_id,
                read_only=True,
                timeout_seconds=INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS,
                busy_timeout_ms=INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS,
            )
        except sqlite3.OperationalError as exc:
            if not sqlite_lock_error(exc):
                raise
            skipped["state_db_locked"] += 1
            continue
        if not queue:
            skipped["queue_item_not_found"] += 1
            continue
        queue_state = str(queue.get("state") or "").strip().lower()
        state = str(lifecycle_state or "").strip().lower()
        reason_text = str(reason or state or "reconciled_import").strip()
        verified = state in {"verified", "suppressed_completed"} or reason_text in {
            "library_visible",
            "folder_verified",
            "kavita_verified",
            "already_imported_or_verified",
            "source_already_imported",
        }
        if queue_state == "verified" and not verified:
            skipped["queue_already_verified"] += 1
            continue
        status = import_result_status_for_lifecycle("verified" if verified else LIBRARY_SCAN_WAIT_STATE, reason_text)
        identity_row = inkdrop_queue_identity_row(queue_id) or {
            "matched_series": matched_series,
            "trusted_issue": trusted_issue,
            "download_task_id": inkdrop_download_task_id,
        }
        identity_row = dict(identity_row)
        identity_row.setdefault("matched_local_path", matched_local_path)
        identity_row.setdefault("title", title)
        identity_row.setdefault("query", query)
        identity_row.setdefault("unit_model", unit_model)
        identity_row.setdefault("truth_model", truth_model)
        identity_row.setdefault("pending_key", pending_key)
        historical_imported_row = imported_rows.get(str(matched_local_path or "").strip()) or {}
        if state == "suppressed_completed":
            imported_row = suppressed_completed_authoritative_existing_path_row(
                state,
                reason_text,
                historical_imported_row.get("dest") or matched_local_path,
                identity_row,
                historical_imported_row.get("imported_at") or updated_at,
                managed_folder_cache=managed_folder_cache,
            )
        else:
            imported_row = historical_imported_row
        imported_source_path = str(imported_row.get("source") or matched_local_path or "").strip()
        imported_dest_path = str(imported_row.get("dest") or "").strip()
        if not imported_dest_path:
            skipped["missing_imported_destination"] += 1
            continue
        if (
            str(client or "").strip().lower() == "inkdrop_page_pack"
            and str(unit_model or "").strip().lower() == "volume"
            and trusted_issue not in (None, "")
        ):
            identity_row["trusted_issue"] = trusted_issue
            identity_row["unit_model"] = "volume"
        identity_ok, identity_reason = imported_file_identity_match(identity_row, imported_row)
        if not identity_ok:
            skipped[identity_reason] += 1
            continue
        task_id = str(inkdrop_download_task_id or "").strip()
        if not task_id:
            skipped["missing_download_task_id"] += 1
            continue
        import_authority = active_reconciled_import_authority(queue_id, task_id)
        if not import_authority:
            skipped["import_authority_not_active"] += 1
            continue
        raw = {
            "kind": "direct_import",
            "source": "download_client",
            "provider": str(client or "download_client").strip().lower() or "download_client",
            "download_client": str(client or "download_client").strip().lower() or "download_client",
            "reason": reason_text,
            "import_ready_reconciliation_replay": True,
            "pending_key": pending_key,
            "matched_series": matched_series,
            "trusted_series_id": trusted_series_id,
            "trusted_issue": trusted_issue,
            "reconciliation_state": state,
            "reconciliation_updated_at": updated_at,
            "imported_file_source_path": imported_source_path,
            "imported_file_dest_path": imported_dest_path,
            "download_task_id": task_id,
            "source_attempt_id": import_authority.get("source_attempt_id"),
            "external_id": import_authority.get("external_id"),
        }
        raw.update(
            replay_managed_destination_evidence(
                imported_source_path,
                imported_dest_path,
                reason=reason_text,
            )
        )
        try:
            result = with_sqlite_lock_retry(
                lambda: inkdrop_state.record_direct_import_result(
                    INKDROP_STATE_DB,
                    queue_id,
                    source_path=import_authority.get("local_path") or imported_source_path,
                    dest_path=imported_dest_path,
                    source=str(client or "download_client").strip().lower() or "download_client",
                    status=status,
                    verified=verified,
                    imported_count=1,
                    skipped_count=0,
                    raw=raw,
                    import_authority=import_authority,
                    created_at=verified_at or imported_at or updated_at or now(),
                    read_timeout_seconds=INKDROP_REPLAY_STATE_READ_TIMEOUT_SECONDS,
                    read_busy_timeout_ms=INKDROP_REPLAY_STATE_READ_BUSY_TIMEOUT_MS,
                    lock_timeout_seconds=INKDROP_REPLAY_STATE_WRITE_TIMEOUT_SECONDS,
                    lock_busy_timeout_ms=INKDROP_REPLAY_STATE_WRITE_BUSY_TIMEOUT_MS,
                ),
                attempts=INKDROP_IMPORT_READY_RECOVERY_WRITE_ATTEMPTS,
                initial_delay=INKDROP_IMPORT_READY_RECOVERY_INITIAL_DELAY_SECONDS,
            )
        except sqlite3.OperationalError as exc:
            if not sqlite_lock_error(exc):
                raise
            skipped["state_db_locked"] += 1
            continue
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        if result.get("ok"):
            updated += 1
        else:
            skipped[str(result.get("reason") or "not_updated")] += 1
    try:
        manga_completion_backfill = backfill_verified_manga_import_results(
            limit=INKDROP_MANGA_COMPLETION_BACKFILL_LIMIT
        )
    except Exception as exc:  # noqa: BLE001 - replay should not fail because a completion upsert failed
        manga_completion_backfill = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        if hasattr(imp, "retract_stale_completion_rows"):
            stale_completion_retraction = imp.retract_stale_completion_rows(
                limit=INKDROP_MANGA_COMPLETION_BACKFILL_LIMIT
            )
        else:
            stale_completion_retraction = {"checked": 0, "retracted": 0, "skipped": {"helper_missing": 1}}
    except Exception as exc:  # noqa: BLE001 - stale cleanup must not block import replay
        stale_completion_retraction = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        stale_completion_history = record_stale_completion_retraction_history(stale_completion_retraction)
    except Exception as exc:  # noqa: BLE001 - history visibility should not block import replay
        stale_completion_history = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": not errors,
        "updated": updated,
        "checked": len(rows),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "manga_completion_backfill": manga_completion_backfill,
        "stale_completion_retraction": stale_completion_retraction,
        "stale_completion_history": stale_completion_history,
        "skipped": dict(sorted(skipped.items())),
        "errors": errors[:5],
    }


def import_lifecycle_stage(stage, status, reason, **evidence):
    """Return bounded, non-sensitive evidence for one independent lifecycle stage."""
    stage = str(stage or "").strip()
    status = str(status or "blocked").strip().lower()
    if stage not in IMPORT_LIFECYCLE_STAGE_NAMES:
        raise ValueError("unknown import lifecycle stage")
    if status not in IMPORT_LIFECYCLE_STAGE_STATUSES:
        raise ValueError("unknown import lifecycle stage status")
    bounded = {
        "stage": stage,
        "status": status,
        "ok": status in {"success", "no_work"},
        "reason": re.sub(r"[^a-z0-9_]+", "_", str(reason or status).strip().lower()).strip("_")[:96] or status,
    }
    allowed_evidence = {
        "client", "returncode", "imported_count", "skipped_count", "verification_count",
        "destination_selected", "artifact_selected", "result_ok", "error_category",
    }
    for key in allowed_evidence:
        value = evidence.get(key)
        if isinstance(value, bool):
            bounded[key] = value
        elif isinstance(value, int):
            bounded[key] = max(-1_000_000, min(value, 1_000_000))
        elif key in {"client", "error_category"} and value:
            bounded[key] = re.sub(r"[^a-z0-9_]+", "_", str(value).strip().lower()).strip("_")[:48]
    return bounded


def empty_import_lifecycle(reason="no_reconciled_ready_source_files"):
    return {
        "contract_version": IMPORT_LIFECYCLE_STAGE_CONTRACT_VERSION,
        "stages": [import_lifecycle_stage(name, "no_work", reason) for name in IMPORT_LIFECYCLE_STAGE_NAMES],
    }


def initial_import_lifecycle(record):
    record = record if isinstance(record, dict) else {}
    client = inkdrop_reconciliation_client(record.get("client"))
    stages = {
        "client_reconciliation": import_lifecycle_stage(
            "client_reconciliation",
            "success" if client else "no_work",
            "client_record_reconciled" if client else "local_artifact_without_client",
            client=client,
        ),
        "completed_download_detection": import_lifecycle_stage(
            "completed_download_detection", "success", "completed_record_selected"
        ),
        "artifact_discovery_validation": import_lifecycle_stage(
            "artifact_discovery_validation", "success", "import_ready_artifact_selected", artifact_selected=True
        ),
    }
    return stages


def observed_lifecycle_write(stage_name, stage_call, success_reason):
    """Run an independent persistence stage without hiding its outcome."""
    try:
        result = stage_call()
    except Exception as exc:  # noqa: BLE001 - sibling stages must continue and retain their evidence
        return (
            {
                "ok": False,
                "error": "stage_exception",
                "error_code": "stage_exception",
                "error_category": _import_ready_stage_error_category(exc),
            },
            import_lifecycle_stage(
                stage_name, "retryable", "stage_exception",
                result_ok=False, error_category=_import_ready_stage_error_category(exc),
            ),
        )
    result_ok = not isinstance(result, dict) or result.get("ok", True) is not False
    if result_ok:
        return result, import_lifecycle_stage(stage_name, "success", success_reason, result_ok=True)
    reason = str((result or {}).get("reason") or "stage_reported_failure").strip().lower()
    if any(token in reason for token in ("locked", "timeout", "temporary", "retry", "unavailable")):
        status = "retryable"
    elif any(token in reason for token in ("missing", "not_ready", "not_configured")):
        status = "blocked"
    else:
        status = "terminal"
    return result, import_lifecycle_stage(stage_name, status, reason, result_ok=False)


def importer_lifecycle_stages(parsed, returncode=0):
    parsed = parsed if isinstance(parsed, dict) else {}
    imported = parsed.get("imported") if isinstance(parsed.get("imported"), list) else []
    skipped = parsed.get("skipped") if isinstance(parsed.get("skipped"), list) else []
    item = first_imported_item(parsed)
    decision = item.get("media_management_destination_decision") if isinstance(item.get("media_management_destination_decision"), dict) else {}
    destination = item.get("dest") or item.get("selected_import_dest_path") or decision.get("selected_dest_path") or decision.get("planned_path")
    retained = bool(skipped and skipped_items_are_imported_or_retained(skipped))
    preparation_status = "success" if not returncode else "terminal"
    stages = {
        "import_preparation": import_lifecycle_stage(
            "import_preparation", preparation_status,
            "importer_completed" if not returncode else "importer_returncode",
            returncode=int(returncode or 0),
        ),
        "destination_selection": import_lifecycle_stage(
            "destination_selection",
            "success" if destination else ("terminal" if returncode or skipped else "blocked"),
            "destination_selected" if destination else "destination_not_reported",
            destination_selected=bool(destination),
        ),
        "file_placement": import_lifecycle_stage(
            "file_placement",
            "success" if imported or retained else ("terminal" if returncode or skipped else "blocked"),
            "artifact_placed" if imported else "existing_artifact_retained" if retained else "artifact_not_placed",
            imported_count=len(imported), skipped_count=len(skipped),
        ),
    }
    verification = parsed.get("verification") if isinstance(parsed.get("verification"), dict) else {}
    checked = verification.get("checked") if isinstance(verification.get("checked"), list) else []
    statuses = {
        str(row.get("verification_status") or "").strip().lower()
        for row in checked if isinstance(row, dict)
    }
    scan_requested = bool(
        statuses & (LIBRARY_SCAN_WAIT_STATES | {"verification_pending"})
        or int(verification.get("pending_scan_count") or 0) > 0
        or int(verification.get("waiting_for_library_scan_count") or 0) > 0
    )
    visible = bool(
        statuses & IMPORT_VERIFIED_STATUSES
        or int(verification.get("library_visible_count") or 0) > 0
        or int(verification.get("kavita_visible_count") or 0) > 0
    )
    if scan_requested or visible:
        scan_status, scan_reason = "success", "reader_scan_requested" if scan_requested else "reader_already_visible"
    elif imported or retained:
        scan_status, scan_reason = "no_work", "reader_scan_not_reported"
    else:
        scan_status, scan_reason = ("terminal", "import_failed_before_reader_scan") if returncode or skipped else ("blocked", "file_placement_incomplete")
    stages["reader_scan_request"] = import_lifecycle_stage(
        "reader_scan_request", scan_status, scan_reason, verification_count=len(checked)
    )
    return stages


def ordered_import_lifecycle(stages):
    stages = stages if isinstance(stages, dict) else {}
    ordered = []
    for name in IMPORT_LIFECYCLE_STAGE_NAMES:
        ordered.append(stages.get(name) or import_lifecycle_stage(name, "blocked", "prior_stage_evidence_unavailable"))
    return {"contract_version": IMPORT_LIFECYCLE_STAGE_CONTRACT_VERSION, "stages": ordered}


def combined_lifecycle_stage(stage_name, observations, success_reason):
    observations = [row for row in observations or [] if isinstance(row, dict)]
    for status in ("terminal", "retryable", "blocked"):
        failed = next((row for row in observations if row.get("status") == status), None)
        if failed:
            return import_lifecycle_stage(
                stage_name, status, failed.get("reason") or "sibling_operation_failed",
                result_ok=False, error_category=failed.get("error_category"),
            )
    return import_lifecycle_stage(stage_name, "success", success_reason, result_ok=True)


def persist_import_lifecycle_outcome(outcome):
    try:
        current = json.loads(RECONCILE_STATUS_PATH.read_text(encoding="utf-8")) if RECONCILE_STATUS_PATH.exists() else {}
    except (OSError, TypeError, ValueError):
        current = {}
    current = current if isinstance(current, dict) else {}
    current[IMPORT_LIFECYCLE_OUTCOME_KEY] = dict(outcome or {})
    write_status(current)


def summarize_import_lifecycles(results, *, reason="import_ready_batch"):
    lifecycles = [
        result.get("import_lifecycle")
        for result in results or []
        if isinstance(result, dict) and isinstance(result.get("import_lifecycle"), dict)
    ][:20]
    counts = collections.Counter(
        stage.get("status")
        for lifecycle in lifecycles
        for stage in lifecycle.get("stages") or []
        if isinstance(stage, dict) and stage.get("status") in IMPORT_LIFECYCLE_STAGE_STATUSES
    )
    return {
        "contract_version": IMPORT_LIFECYCLE_STAGE_CONTRACT_VERSION,
        "completed_at": now(),
        "reason": reason,
        "record_count": len(lifecycles),
        "stage_status_counts": dict(sorted(counts.items())),
        "records": lifecycles,
    }


def parse_importer_json(stdout):
    text = str(stdout or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except ValueError:
                return {}
    return {}


def import_ready_child_env():
    env = os.environ.copy()
    env.setdefault("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE", "defer")
    return env


BENIGN_IMPORT_SKIP_TOKENS = (
    "already_imported",
    "already_verified",
    "already present",
    "already_present",
    "existing",
    "retained",
)


def media_management_existing_destination_retained(item):
    item = item if isinstance(item, dict) else {}
    event = str(item.get("event") or "").strip()
    skip_reason = str(item.get("skip_reason") or "").strip()
    if event != "skip_media_management_existing_destination" and skip_reason != "media_management_destination_exists":
        return False
    decision = item.get("media_management_destination_decision") if isinstance(item.get("media_management_destination_decision"), dict) else {}
    preview = item.get("media_management_preview") if isinstance(item.get("media_management_preview"), dict) else {}
    selected = normalized_path_text(
        item.get("dest")
        or item.get("selected_import_dest_path")
        or decision.get("selected_dest_path")
        or preview.get("selected_import_dest_path")
        or preview.get("current_import_dest_path")
    )
    planned = normalized_path_text(
        item.get("planned_path")
        or decision.get("planned_path")
        or preview.get("planned_path")
    )
    if not selected:
        return False
    if planned and selected.lower() != planned.lower():
        return False
    return True


def import_skip_reason(item):
    item = item if isinstance(item, dict) else {}
    for key in ("skip_reason", "reason", "event", "detail", "action_needed"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return "importer_skipped_without_reason"


def skipped_items_are_imported_or_retained(skipped):
    for item in skipped or []:
        item = item if isinstance(item, dict) else {}
        if str(item.get("event") or "").strip() == "skip_media_management_existing_destination" or str(item.get("skip_reason") or "").strip() == "media_management_destination_exists":
            if media_management_existing_destination_retained(item):
                continue
            return False
        action_needed = str(item.get("action_needed") or "").strip().lower()
        reason_text = " ".join(
            str(item.get(key) or "").strip().lower()
            for key in ("skip_reason", "reason", "event", "detail")
            if str(item.get(key) or "").strip()
        )
        if action_needed.startswith("manual_review"):
            return False
        if not any(token in reason_text for token in BENIGN_IMPORT_SKIP_TOKENS):
            return False
    return bool(skipped)


def failed_import_reason_from_skips(skipped):
    for item in skipped or []:
        reason = import_skip_reason(item)
        if reason:
            return f"importer_skipped_{reason}"
    return "importer_skipped_without_import"


def terminal_import_artifact_rejection(parsed):
    parsed = parsed if isinstance(parsed, dict) else {}
    skipped = parsed.get("skipped") if isinstance(parsed.get("skipped"), list) else []
    for item in skipped:
        item = item if isinstance(item, dict) else {}
        action_needed = str(item.get("action_needed") or "").strip().lower()
        if action_needed.startswith(TERMINAL_IMPORT_ARTIFACT_ACTION_PREFIXES):
            return True
        evidence = " ".join(
            str(item.get(key) or "").strip().lower()
            for key in ("event", "skip_reason", "reason", "detail", "action_needed")
            if str(item.get(key) or "").strip()
        )
        if any(token in evidence for token in TERMINAL_IMPORT_ARTIFACT_SKIP_TOKENS):
            return True
    return False


def import_result_state(parsed, returncode=0):
    parsed = parsed if isinstance(parsed, dict) else {}
    imported = parsed.get("imported") if isinstance(parsed.get("imported"), list) else []
    skipped = parsed.get("skipped") if isinstance(parsed.get("skipped"), list) else []
    verification = parsed.get("verification") if isinstance(parsed.get("verification"), dict) else {}
    checked = verification.get("checked") if isinstance(verification.get("checked"), list) else []
    statuses = {
        str(item.get("verification_status") or "").strip().lower()
        for item in checked
        if isinstance(item, dict)
    }
    verified = (
        bool(statuses & IMPORT_VERIFIED_STATUSES)
        or int(verification.get("kavita_visible_count") or 0) > 0
        or int(verification.get("library_visible_count") or 0) > 0
        or int(verification.get("manga_verified_count") or 0) > 0
        or int(verification.get("collection_verified_count") or 0) > 0
    )
    pending = (
        bool(statuses & (LIBRARY_SCAN_WAIT_STATES | LIBRARY_SCAN_TIMEOUT_STATES | {"verification_pending"}))
        or int(verification.get("pending_scan_count") or 0) > 0
        or int(verification.get("waiting_for_library_scan_count") or 0) > 0
        or int(verification.get("waiting_for_kavita_scan_count") or 0) > 0
    )
    if verified:
        if "folder_verified" in statuses:
            return "verified", "folder_verified", True
        return "verified", "library_visible", True
    if imported and pending:
        return LIBRARY_SCAN_WAIT_STATE, "importer_copied_waiting_for_library_scan", False
    if imported:
        return "imported", "importer_copied", False
    if skipped:
        if not skipped_items_are_imported_or_retained(skipped):
            return "failed_import", failed_import_reason_from_skips(skipped), False
        return "imported", "importer_skipped_existing_or_retained", False
    if returncode:
        return "failed_import", f"importer_returncode_{returncode}", False
    return "failed_import", "importer_no_imported_files", False


def first_imported_item(parsed):
    parsed = parsed if isinstance(parsed, dict) else {}
    imported = parsed.get("imported") if isinstance(parsed.get("imported"), list) else []
    for item in imported:
        if isinstance(item, dict):
            return item
    skipped = parsed.get("skipped") if isinstance(parsed.get("skipped"), list) else []
    for item in skipped:
        if isinstance(item, dict):
            return item
    return {}


def mark_reconciled_import_attempt(record, parsed, returncode=0):
    state, reason, verified = import_result_state(parsed, returncode=returncode)
    source_path = str(record.get("source_file") or "")
    _, local_size, local_mtime = local_file_identity(source_path)
    ts = now()
    conn = connect_db()
    try:
        conn.execute(
            """
            update download_reconciliation
               set lifecycle_state=?,
                   reason=?,
                   matched_local_size=coalesce(?, matched_local_size),
                   matched_local_mtime=coalesce(?, matched_local_mtime),
                   imported_at=case
                       when ? in ('imported','waiting_for_library_scan','waiting_for_kavita_scan','verified') then coalesce(imported_at, ?)
                       else imported_at
                   end,
                   verified_at=case when ? = 'verified' then coalesce(verified_at, ?) else verified_at end,
                   updated_at=?
             where pending_key=?
               and matched_local_path=?
            """,
            (
                state,
                reason,
                local_size,
                local_mtime,
                state,
                ts,
                state,
                ts,
                ts,
                record.get("pending_key"),
                source_path,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"state": state, "reason": reason, "verified": verified, "updated_at": ts}


def claim_inkdrop_import_attempt(record):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "inkdrop_state_unavailable"}
    record = record if isinstance(record, dict) else {}
    queue_id = str(record.get("inkdrop_queue_id") or "").strip()
    task_id = str(record.get("inkdrop_download_task_id") or "").strip()
    if not queue_id or not task_id:
        return {"ok": False, "reason": "missing_queue_or_task_id"}
    return with_sqlite_lock_retry(
        lambda: inkdrop_state.claim_import_authority(
            INKDROP_STATE_DB,
            queue_id,
            task_id,
            source_attempt_id=record.get("inkdrop_source_attempt_id"),
            external_id=record.get("inkdrop_external_id"),
            candidate_identity=record.get("inkdrop_candidate_identity"),
            download_client=record.get("inkdrop_download_client") or record.get("client"),
            local_path=record.get("inkdrop_task_local_path") or record.get("source_file"),
            lock_timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS,
            lock_busy_timeout_ms=INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS,
        )
    )


def release_inkdrop_import_attempt(record, reason, *, retry_staged=False, artifact_retry_blocked=False):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "inkdrop_state_unavailable"}
    authority = (record or {}).get("import_authority")
    if not isinstance(authority, dict):
        return {"ok": False, "reason": "import_authority_missing"}
    return with_sqlite_lock_retry(
        lambda: inkdrop_state.release_import_authority(
            INKDROP_STATE_DB,
            authority,
            reason=reason,
            retry_staged=retry_staged,
            artifact_retry_blocked=artifact_retry_blocked,
            lock_timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS,
            lock_busy_timeout_ms=INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS,
        )
    )


def active_reconciled_import_authority(queue_id, task_id):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {}
    queue_id = str(queue_id or "").strip()
    task_id = str(task_id or "").strip()
    if not queue_id or not task_id:
        return {}
    active = inkdrop_state.active_import_authority(INKDROP_STATE_DB, queue_id, task_id)
    return dict(active or {})


def import_callback_authority_conflict(record, authority):
    record = record if isinstance(record, dict) else {}
    authority = authority if isinstance(authority, dict) else {}

    def canonical_client(value):
        value = str(value or "").strip().lower()
        if value in {"qbit", "qbittorrent"}:
            return "qbittorrent"
        if value in {"sab", "sabnzbd"}:
            return "sabnzbd"
        return value

    comparisons = (
        ("queue_id", "inkdrop_queue_id", "queue_id", False),
        ("download_task_id", "inkdrop_download_task_id", "download_task_id", False),
        ("source_attempt_id", "inkdrop_source_attempt_id", "source_attempt_id", False),
        ("external_id", "inkdrop_external_id", "external_id", False),
        ("candidate_identity", "inkdrop_candidate_identity", "candidate_identity", False),
        ("download_client", "inkdrop_download_client", "download_client", True),
    )
    for label, record_key, authority_key, casefold in comparisons:
        if record_key not in record:
            return f"import_callback_{label}_missing"
        callback_value = str(record.get(record_key) or "").strip()
        authority_value = str(authority.get(authority_key) or "").strip()
        if casefold:
            callback_value = canonical_client(callback_value)
            authority_value = canonical_client(authority_value)
        if callback_value != authority_value:
            return f"import_callback_{label}_mismatch"
    if "inkdrop_task_local_path" not in record:
        return "import_callback_local_path_missing"
    if "source_file" not in record:
        return "import_callback_source_path_missing"
    callback_path = str(record.get("inkdrop_task_local_path") or "").strip().replace("\\", "/")
    authority_path = str(authority.get("local_path") or "").strip().replace("\\", "/")
    if callback_path != authority_path:
        return "import_callback_local_path_mismatch"
    source_path = str(record.get("source_file") or "").strip().replace("\\", "/")
    if source_path != authority_path:
        return "import_callback_source_path_mismatch"
    if "client" not in record:
        return "import_callback_download_client_missing"
    callback_client = canonical_client(record.get("client"))
    authority_client = canonical_client(authority.get("download_client"))
    if callback_client != authority_client:
        return "import_callback_download_client_mismatch"
    return ""


def record_inkdrop_import_attempt(record, parsed, returncode=0):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "inkdrop_state_unavailable"}
    queue_id = str(record.get("inkdrop_queue_id") or "").strip()
    if not queue_id:
        return {"ok": False, "reason": "queue_id_missing"}
    state, reason, verified = import_result_state(parsed, returncode=returncode)
    task_id = str(record.get("inkdrop_download_task_id") or "").strip()
    authority = record.get("import_authority")
    if not isinstance(authority, dict):
        return {
            "ok": False,
            "reason": "import_authority_missing",
            "queue_id": queue_id,
            "download_task_id": task_id,
        }
    conflict = import_callback_authority_conflict(record, authority)
    if conflict:
        return {
            "ok": False,
            "reason": conflict,
            "queue_id": queue_id,
            "download_task_id": task_id,
        }
    if state == "failed_import":
        release = release_inkdrop_import_attempt(
            record,
            reason,
            retry_staged=False,
            artifact_retry_blocked=terminal_import_artifact_rejection(parsed),
        )
        return {"ok": False, "reason": reason, "skipped": "failed_import", "release": release}
    item = first_imported_item(parsed)
    source_path = str(item.get("source") or record.get("source_file") or "")
    dest_path = str(item.get("dest") or "")
    imported = parsed.get("imported") if isinstance(parsed, dict) and isinstance(parsed.get("imported"), list) else []
    skipped = parsed.get("skipped") if isinstance(parsed, dict) and isinstance(parsed.get("skipped"), list) else []
    client = str(record.get("client") or "download_client").strip().lower() or "download_client"
    status = import_result_status_for_lifecycle(state, reason)
    raw = {
        "kind": "direct_import",
        "source": "download_client",
        "provider": client,
        "download_client": client,
        "reason": reason,
        "import_ready_bridge": True,
        "trusted_series_id": record.get("trusted_series_id"),
        "trusted_issue": record.get("trusted_issue"),
        "pending_key": record.get("pending_key"),
        "returncode": int(returncode or 0),
        "download_task_id": record.get("inkdrop_download_task_id"),
        "source_attempt_id": record.get("inkdrop_source_attempt_id"),
        "external_id": record.get("inkdrop_external_id"),
    }
    raw.update(media_management_event_evidence(item))
    if "media_management_destination_decision" not in raw:
        raw.update(replay_managed_destination_evidence(source_path, dest_path, reason=reason))
    return with_sqlite_lock_retry(
        lambda: inkdrop_state.record_direct_import_result(
            INKDROP_STATE_DB,
            queue_id,
            source_path=source_path,
            dest_path=dest_path,
            source=client,
            status=status,
            verified=verified,
            imported_count=len(imported),
            skipped_count=len(skipped),
            raw=raw,
            import_authority=record.get("import_authority"),
            read_timeout_seconds=INKDROP_STATE_READ_TIMEOUT_SECONDS,
            read_busy_timeout_ms=INKDROP_STATE_READ_BUSY_TIMEOUT_MS,
            lock_timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS,
            lock_busy_timeout_ms=INKDROP_STATE_WRITE_BUSY_TIMEOUT_MS,
        )
    )


def mark_reconciled_import_timeout(record, exc, timeout_seconds=None):
    ts = now()
    reason = "import_ready_import_timeout"
    timeout_value = int(timeout_seconds or IMPORT_READY_IMPORT_TIMEOUT_SECONDS)
    source_path = str((record or {}).get("source_file") or "")
    _, local_size, local_mtime = local_file_identity(source_path)
    conn = connect_db()
    try:
        conn.execute(
            """
            update download_reconciliation
               set lifecycle_state='failed_import',
                   reason=?,
                   matched_local_size=coalesce(?, matched_local_size),
                   matched_local_mtime=coalesce(?, matched_local_mtime),
                   updated_at=?
             where pending_key=?
               and matched_local_path=?
            """,
            (
                reason,
                local_size,
                local_mtime,
                ts,
                (record or {}).get("pending_key"),
                source_path,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "state": "failed_import",
        "reason": reason,
        "timeout_seconds": timeout_value,
        "updated_at": ts,
        "error": str(exc),
    }


def record_inkdrop_import_timeout(record, exc, timeout_seconds=None):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "inkdrop_state_unavailable"}
    queue_id = str((record or {}).get("inkdrop_queue_id") or "").strip()
    if not queue_id:
        return {"ok": False, "reason": "queue_id_missing"}
    timeout_value = int(timeout_seconds or IMPORT_READY_IMPORT_TIMEOUT_SECONDS)
    try:
        release = release_inkdrop_import_attempt(
            record,
            "import_ready_import_timeout",
            retry_staged=True,
        )
    except sqlite3.OperationalError as db_exc:
        if not sqlite_lock_error(db_exc):
            raise
        return {"ok": False, "reason": "state_db_locked", "error": str(db_exc)}
    return {
        "ok": bool(release.get("ok")),
        "reason": "import_ready_import_timeout",
        "timeout_seconds": timeout_value,
        "source_path": str((record or {}).get("source_file") or ""),
        "error": str(exc),
        "release": release,
    }


def import_ready(max_files):
    global LAST_IMPORT_READY_STAGE_OUTCOMES
    global PENDING_IMPORT_READY_PRELIMINARY_STAGES, PENDING_IMPORT_READY_STARTED_AT
    batch_started = time.monotonic()
    stage_started_at = now()
    active_import_recovery, active_recovery_stage = _observe_import_ready_stage(
        "recover_active_imports",
        recover_active_import_ready_from_imported_files,
    )
    state_import_ready_sync, initial_state_sync_stage = _observe_import_ready_stage(
        "initial_state_import_ready_sync",
        lambda: sync_inkdrop_import_ready_records(
            max_records=max(1, int(max_files or IMPORT_READY_MAX_FILES) * 4)
        ),
    )
    LAST_IMPORT_READY_STAGE_OUTCOMES = None
    preliminary_stages = [active_recovery_stage, initial_state_sync_stage]
    finalize_import_ready_stage_outcomes(preliminary_stages, stage_started_at)
    PENDING_IMPORT_READY_PRELIMINARY_STAGES = preliminary_stages
    PENDING_IMPORT_READY_STARTED_AT = stage_started_at
    try:
        records = ready_import_records(max_files)
    except Exception as exc:  # noqa: BLE001 - discovery failure must be explicit and bounded
        category = _import_ready_stage_error_category(exc)
        stages = {
            "client_reconciliation": import_lifecycle_stage(
                "client_reconciliation",
                "success" if initial_state_sync_stage.get("ok") else "retryable",
                "state_import_ready_sync_complete" if initial_state_sync_stage.get("ok") else "state_import_ready_sync_failed",
                error_category=None if initial_state_sync_stage.get("ok") else initial_state_sync_stage.get("error_category"),
            ),
            "completed_download_detection": import_lifecycle_stage(
                "completed_download_detection", "retryable", "ready_record_discovery_exception", error_category=category
            ),
        }
        lifecycle = ordered_import_lifecycle(stages)
        lifecycle_summary = summarize_import_lifecycles(
            [{"import_lifecycle": lifecycle}], reason="import_ready_record_discovery_failed"
        )
        persist_import_lifecycle_outcome(lifecycle_summary)
        return {
            "ok": False,
            "skipped": True,
            "reason": "import_ready_record_discovery_failed",
            "error_code": "stage_exception",
            "error_category": category,
            "partial_reconciliation": True,
            "reconciliation_stages": finalize_import_ready_stage_outcomes(preliminary_stages, stage_started_at),
            "active_import_recovery": active_import_recovery,
            "state_import_ready_sync": state_import_ready_sync,
            "import_lifecycle": lifecycle,
            "import_lifecycle_summary": lifecycle_summary,
        }
    finally:
        PENDING_IMPORT_READY_PRELIMINARY_STAGES = None
        PENDING_IMPORT_READY_STARTED_AT = None
    reconciliation_stages = finalize_import_ready_stage_outcomes(
        preliminary_stages,
        stage_started_at,
    )
    if not records:
        lifecycle = empty_import_lifecycle()
        lifecycle_summary = summarize_import_lifecycles(
            [{"import_lifecycle": lifecycle}], reason="no_reconciled_ready_source_files"
        )
        persist_import_lifecycle_outcome(lifecycle_summary)
        return {
            "ok": bool(reconciliation_stages.get("ok", True)),
            "skipped": True,
            "reason": "no_reconciled_ready_source_files",
            "partial_reconciliation": bool(reconciliation_stages.get("partial")),
            "reconciliation_stages": reconciliation_stages,
            "active_import_recovery": active_import_recovery,
            "state_import_ready_sync": state_import_ready_sync,
            "import_lifecycle": lifecycle,
            "import_lifecycle_summary": lifecycle_summary,
        }
    results = []
    returncode = 0
    batch_budget_exhausted = False
    skipped_records = []
    for index, record in enumerate(records):
        elapsed = time.monotonic() - batch_started
        remaining = IMPORT_READY_BATCH_TIMEOUT_SECONDS - elapsed
        if remaining < 30:
            batch_budget_exhausted = True
            skipped_records = records[index:]
            if not returncode:
                returncode = 124
            break
        timeout_seconds = max(30, min(IMPORT_READY_IMPORT_TIMEOUT_SECONDS, int(remaining)))
        claim = claim_inkdrop_import_attempt(record)
        if not claim.get("ok"):
            results.append(
                {
                    "returncode": 0,
                    "skipped": True,
                    "reason": claim.get("reason") or "import_authority_claim_failed",
                    "source_file": record.get("source_file"),
                    "inkdrop_queue_id": record.get("inkdrop_queue_id"),
                    "inkdrop_download_task_id": record.get("inkdrop_download_task_id"),
                    "import_authority_claim": claim,
                    "import_lifecycle": ordered_import_lifecycle(
                        {
                            "completed_download_detection": import_lifecycle_stage(
                                "completed_download_detection", "success", "completed_transfer_found"
                            ),
                            "import_preparation": import_lifecycle_stage(
                                "import_preparation", "blocked", claim.get("reason") or "import_authority_claim_failed"
                            ),
                        }
                    ),
                    "stdout": "",
                    "stderr": "",
                }
            )
            continue
        record["import_authority"] = claim.get("authority")
        cmd = [
            sys.executable,
            str(IMPORTER_PATH),
            "--kind",
            "comics",
            "--all-series",
            "--matched-only",
            "--ignore-cutoff",
            "--min-age-seconds",
            "0",
            "--max-files",
            "1",
            "--source-file",
            record["source_file"],
            "--no-wait-for-library-scan",
        ]
        if record.get("trusted_series_id"):
            cmd.extend(["--trusted-series-id", str(record["trusted_series_id"])])
        if record.get("trusted_issue"):
            cmd.extend(["--trusted-issue", str(record["trusted_issue"])])
        if record.get("trusted_issue_title"):
            cmd.extend(["--trusted-issue-title", str(record["trusted_issue_title"])])
        if record.get("trusted_issue_id"):
            cmd.extend(["--trusted-issue-id", str(record["trusted_issue_id"])])
        if INKDROP_IMPORT_READY_APPLY_PLANNED_PATH:
            cmd.append("--apply-planned-path")
        lifecycle_stages = initial_import_lifecycle(record)
        lifecycle_stages["import_preparation"] = import_lifecycle_stage(
            "import_preparation", "success", "import_command_prepared"
        )
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                env=import_ready_child_env(),
            )
        except subprocess.TimeoutExpired as exc:
            reconcile_update, reconcile_projection_stage = observed_lifecycle_write(
                "completion_projection",
                lambda: mark_reconciled_import_timeout(record, exc, timeout_seconds=timeout_seconds),
                "timeout_projected_to_reconciliation",
            )
            inkdrop_update, metadata_stage = observed_lifecycle_write(
                "metadata_write",
                lambda: record_inkdrop_import_timeout(record, exc, timeout_seconds=timeout_seconds),
                "timeout_metadata_recorded",
            )
            lifecycle_stages.update(
                {
                    "destination_selection": import_lifecycle_stage("destination_selection", "blocked", "importer_timeout"),
                    "file_placement": import_lifecycle_stage("file_placement", "retryable", "importer_timeout"),
                    "metadata_write": metadata_stage,
                    "reader_scan_request": import_lifecycle_stage("reader_scan_request", "blocked", "file_placement_incomplete"),
                    "completion_projection": reconcile_projection_stage,
                }
            )
            if not returncode:
                returncode = 124
            results.append(
                {
                    "returncode": 124,
                    "timeout": True,
                    "timeout_seconds": timeout_seconds,
                    "source_file": record["source_file"],
                    "trusted_series_id": record.get("trusted_series_id"),
                    "trusted_issue": record.get("trusted_issue"),
                    "inkdrop_queue_id": record.get("inkdrop_queue_id"),
                    "reconcile_update": reconcile_update,
                    "inkdrop_update": inkdrop_update,
                    "import_lifecycle": ordered_import_lifecycle(lifecycle_stages),
                    "stdout": str(exc.output or "")[-4000:],
                    "stderr": str(exc.stderr or "")[-2000:],
                }
            )
            continue
        parsed = parse_importer_json(proc.stdout)
        lifecycle_stages.update(importer_lifecycle_stages(parsed, returncode=proc.returncode))
        reconcile_update, reconcile_projection_stage = observed_lifecycle_write(
            "completion_projection",
            lambda: mark_reconciled_import_attempt(record, parsed, returncode=proc.returncode),
            "reconciliation_projection_recorded",
        )
        inkdrop_update, direct_metadata_stage = observed_lifecycle_write(
            "metadata_write",
            lambda: record_inkdrop_import_attempt(record, parsed, returncode=proc.returncode),
            "import_metadata_recorded",
        )
        import_result_sync, sync_metadata_stage = observed_lifecycle_write(
            "metadata_write",
            sync_reconciliation_from_inkdrop_import_results,
            "import_metadata_reconciled",
        )
        inkdrop_replay_sync, replay_projection_stage = observed_lifecycle_write(
            "completion_projection",
            sync_inkdrop_from_reconciled_imports,
            "completion_projected_to_state",
        )
        lifecycle_stages["metadata_write"] = combined_lifecycle_stage(
            "metadata_write", (direct_metadata_stage, sync_metadata_stage), "import_metadata_recorded_and_reconciled"
        )
        lifecycle_stages["completion_projection"] = combined_lifecycle_stage(
            "completion_projection", (reconcile_projection_stage, replay_projection_stage), "completion_projection_recorded"
        )
        if proc.returncode and not returncode:
            returncode = proc.returncode
        results.append(
            {
                "returncode": proc.returncode,
                "source_file": record["source_file"],
                "trusted_series_id": record.get("trusted_series_id"),
                "trusted_issue": record.get("trusted_issue"),
                "inkdrop_queue_id": record.get("inkdrop_queue_id"),
                "reconcile_update": reconcile_update,
                "inkdrop_update": inkdrop_update,
                "import_result_sync": import_result_sync,
                "inkdrop_replay_sync": inkdrop_replay_sync,
                "import_lifecycle": ordered_import_lifecycle(lifecycle_stages),
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-2000:],
            }
        )
    lifecycle_summary = summarize_import_lifecycles(results)
    persist_import_lifecycle_outcome(lifecycle_summary)
    return {
        "ok": bool(reconciliation_stages.get("ok", True)) and returncode == 0,
        "returncode": returncode,
        "source_files": [record["source_file"] for record in records],
        "processed_source_files": [result.get("source_file") for result in results if result.get("source_file")],
        "skipped_source_files": [record["source_file"] for record in skipped_records],
        "processed_count": len(results),
        "skipped_remaining_count": len(skipped_records),
        "batch_budget_exhausted": batch_budget_exhausted,
        "batch_timeout_seconds": IMPORT_READY_BATCH_TIMEOUT_SECONDS,
        "elapsed_seconds": round(time.monotonic() - batch_started, 3),
        "active_import_recovery": active_import_recovery,
        "state_import_ready_sync": state_import_ready_sync,
        "partial_reconciliation": bool(reconciliation_stages.get("partial")),
        "reconciliation_stages": reconciliation_stages,
        "import_lifecycle_summary": lifecycle_summary,
        "results": results,
        "stdout": "\n".join(result.get("stdout") or "" for result in results)[-8000:],
        "stderr": "\n".join(result.get("stderr") or "" for result in results)[-4000:],
    }


def import_ready_dry_run(max_files):
    max_files = bounded_import_ready_max(max_files)
    try:
        rows = inkdrop_import_ready_rows(max(1, int(max_files or IMPORT_READY_MAX_FILES) * 4))
    except Exception as exc:
        return {
            "ok": False,
            "dry_run": True,
            "skipped": True,
            "reason": "state_import_ready_read_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    candidates = []
    for row in rows[:max_files]:
        candidates.append(
            {
                "queue_id": row.get("queue_id"),
                "download_task_id": row.get("download_task_id"),
                "series_title": row.get("series_title"),
                "issue_number": row.get("issue_number"),
                "source": row.get("source"),
                "provider": row.get("provider"),
                "download_client": row.get("download_client"),
                "task_status": row.get("task_status"),
                "task_state": row.get("task_state"),
                "local_path": row.get("local_path"),
            }
        )
    return {
        "ok": True,
        "dry_run": True,
        "reason": "import_ready_queue_inspection",
        "checked": len(rows),
        "candidate_count": len(candidates),
        "max_files": max_files,
        "would_import": candidates,
    }


def main():
    parser = argparse.ArgumentParser(description="Reconcile InkDrop pending downloads, clients, and completed imports")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write-status", action="store_true")
    parser.add_argument("--import-ready", action="store_true")
    parser.add_argument(
        "--drain-import-authorities-before-rollback",
        action="store_true",
        help="After stopping the worker, return exact in-flight imports to staged retry; exits nonzero if any authority remains.",
    )
    parser.add_argument("--download-clients", action="store_true", help="Reconcile qB/SAB source-wait rows before import.")
    parser.add_argument("--download-client", action="append", choices=("qbit", "qbittorrent", "sab", "sabnzbd"), help="Limit --download-clients to one download client; repeat to include more than one.")
    parser.add_argument("--skip-download-clients", action="store_true", help="Do not auto-reconcile qB/SAB source-wait rows in import-ready mode.")
    parser.add_argument("--max-files", type=int, default=5)
    parser.add_argument("--deep-scan", action="store_true", help="Run slower local-unlinked archive validation during reconcile.")
    parser.add_argument("--lock-wait-seconds", type=float, default=0, help="Wait this long for the reconcile lock before returning reconcile_lock_busy.")
    args = parser.parse_args()
    try:
        lock_handle, lock_module = acquire_reconcile_lock(args.lock_wait_seconds)
    except BlockingIOError:
        result = {
            "ok": False,
            "skipped": True,
            "reason": "reconcile_lock_busy",
            "lock": str(RECONCILE_LOCK_PATH),
            "lock_wait_seconds": float(args.lock_wait_seconds or 0),
            "dry_run": bool(args.dry_run),
            "import_ready": bool(args.import_ready),
            "write_status": bool(args.write_status),
            "download_clients": bool(args.download_clients),
        }
        if args.json or True:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 75
    try:
        if args.drain_import_authorities_before_rollback:
            if inkdrop_state is None or not INKDROP_STATE_DB.exists():
                result = {"ok": False, "reason": "inkdrop_state_unavailable", "state_db": str(INKDROP_STATE_DB)}
            else:
                result = inkdrop_state.recover_active_import_authorities(
                    INKDROP_STATE_DB,
                    reason="rollback_precondition",
                )
                result["operation"] = "drain_import_authorities_before_rollback"
                result["state_db"] = str(INKDROP_STATE_DB)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result.get("ok") else 1
        download_client_reconcile = None
        if args.download_clients or (args.import_ready and not args.skip_download_clients and not args.dry_run):
            download_client_reconcile = reconcile_inkdrop_download_clients(
                dry_run=bool(args.dry_run),
                client_filter=args.download_client,
            )
        if args.download_clients and not args.import_ready and not args.write_status:
            result = {
                "dry_run": bool(args.dry_run),
                "download_client_reconcile": download_client_reconcile,
            }
            if args.json or True:
                print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if not isinstance(download_client_reconcile, dict) or download_client_reconcile.get("ok", True) else 1

        if args.import_ready and args.dry_run and not args.write_status:
            result = {
                "dry_run": True,
                "import_ready": True,
                "reconciliation": {
                    "skipped": True,
                    "reason": "direct_import_ready_dry_run",
                },
                "import": import_ready_dry_run(args.max_files),
            }
            if download_client_reconcile is not None:
                result["download_client_reconcile"] = download_client_reconcile
            if args.json or True:
                print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["import"].get("ok", True) else 1

        if args.import_ready and not args.write_status and not args.dry_run:
            result = {
                "dry_run": False,
                "import_ready": True,
                "reconciliation": {
                    "skipped": True,
                    "reason": "direct_import_ready_mode",
                },
            }
            if download_client_reconcile is not None:
                result["download_client_reconcile"] = download_client_reconcile
            result["import"] = import_ready(bounded_import_ready_max(args.max_files))
            result["status_refresh"] = refresh_status_from_reconciliation_db()
            if args.json or True:
                print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["import"].get("ok", True) else 1

        status = reconcile(deep_scan=bool(args.deep_scan))
        result = {"reconciliation": status, "dry_run": bool(args.dry_run)}
        if download_client_reconcile is not None:
            result["download_client_reconcile"] = download_client_reconcile
        if args.write_status or args.dry_run or not args.import_ready:
            write_status(status)
        if args.import_ready:
            result["import"] = import_ready(bounded_import_ready_max(args.max_files))
            if not result["import"].get("skipped") or result["import"].get("reason") != "no_reconciled_ready_source_files":
                status = reconcile(deep_scan=bool(args.deep_scan))
                write_status(status)
                result["post_import_reconciliation"] = status
        if args.json or True:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if not args.import_ready or result.get("import", {}).get("ok", True) else 1
    finally:
        try:
            lock_module.flock(lock_handle, lock_module.LOCK_UN)
        finally:
            lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
