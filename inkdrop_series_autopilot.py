#!/usr/bin/env python3
"""Watched-series autopilot queue for InkDrop.

This worker is intentionally an orchestrator. It keeps a durable queue derived
from InkDrop-owned watched series, then nudges the existing acquisition workers
in source order instead of becoming a second downloader.
"""

import argparse
import collections
import contextlib
import hashlib
import ipaddress
import importlib.util
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from inkdrop_acquire import detect_pack_info

import inkdrop_db
import inkdrop_runtime_config
import inkdrop_internal_jobs

try:
    import inkdrop_state
except Exception:
    inkdrop_state = None

try:
    import inkdrop_source_worker_coordinator
except Exception:
    inkdrop_source_worker_coordinator = None

try:
    import inkdrop_source_worker_cli
except Exception:
    inkdrop_source_worker_cli = None


def script_path(name, remote_path=None, *, env_var=None, fallback=None):
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


def python_command():
    return os.environ.get("PYTHON_BIN") or sys.executable or "python3"


CONFIG_DIR = inkdrop_runtime_config.config_dir()
STATE_DIR = inkdrop_runtime_config.state_dir()
LOG_DIR = inkdrop_runtime_config.log_dir()
STAGING_DIR = inkdrop_runtime_config.staging_dir()
INKDROP_STATE_DB = STATE_DIR / (inkdrop_state.STATE_DB_NAME if inkdrop_state else "inkdrop-state.sqlite3")
COMIC_SERIES_FILE = STATE_DIR / "comic-series-watches.json"
QUEUE_FILE = STATE_DIR / "series-autopilot-queue.json"
STATUS_FILE = STATE_DIR / "series-autopilot-status.json"
LOG_FILE = LOG_DIR / "series-autopilot.log"
MANUAL_REVIEW_FILE = STATE_DIR / "manual-review.jsonl"
MANUAL_REVIEW_ACTIONS_FILE = STATE_DIR / "manual-review-actions.json"
SLSKD_SOURCE_PROBE_STATUS_FILE = STATE_DIR / "slskd-source-probe-status.json"
SLSKD_SOURCE_PROBE_CACHE_FILE = STATE_DIR / "slskd-source-probe-cache.json"
SLSKD_AUTO_GRAB_STATE_FILE = STATE_DIR / "slskd-auto-grab-state.json"
MANUAL_SOURCE_AUTORESOLVE_STATUS_FILE = STATE_DIR / "manual-source-autoresolve-status.json"
MANUAL_SOURCE_QUEUE_SYNC_FILE = STATE_DIR / "manual-source-queue-sync-pending.json"
IMPORT_STATUS_FILE = STATE_DIR / "import-status.json"
PACK_AUTO_IMPORT_STATUS_FILE = STATE_DIR / "pack-auto-import-status.json"
PACK_REVIEW_STATE_FILE = STATE_DIR / "pack-review-state.json"
PACK_BAD_ARCHIVE_HISTORY_FILE = STATE_DIR / "pack-bad-archive-history.json"
IMPORTED_DB = STATE_DIR / "imported-files.sqlite3"
KAVITA_DB = inkdrop_runtime_config.kavita_db_path()
COMIC_ROOT = Path(os.environ.get("INKDROP_COMIC_ROOT") or "/library/comics")
MANGA_ROOT = Path(os.environ.get("INKDROP_MANGA_ROOT") or "/library/manga")
KAVITA_COMIC_ROOT = os.environ.get("INKDROP_KAVITA_COMIC_ROOT") or "/data/comics"
KAVITA_MANGA_ROOT = os.environ.get("INKDROP_KAVITA_MANGA_ROOT") or "/data/manga"
SQLITE_BUSY_TIMEOUT_MS = 60000
INKDROP_STATE_QUEUE_SYNC_MIN_SECONDS = 15.0
INKDROP_STATE_FAST_SYNC_TIMEOUT_SECONDS = 2.0
INKDROP_STATE_FAST_SYNC_BUSY_TIMEOUT_MS = 2000
LAST_INKDROP_STATE_QUEUE_SYNC = 0.0
ACTIVE_CHILD_PROCS = set()
INKDROP_STATE_IMPORT_READY_STATUSES = (
    "completed_in_client",
    "staged_file_ready",
    "ready_import",
    "preview_importable",
    "ready_to_import",
)

MISSING_SCRIPT = script_path("inkdrop_missing_acquire.py", env_var="INKDROP_MISSING_ACQUIRE_SCRIPT")
RSS_SCRIPT = script_path("inkdrop_rss_discovery.py", env_var="INKDROP_RSS_DISCOVERY_SCRIPT")
COMICSCODES_SCRIPT = script_path("inkdrop_comicscodes_discovery.py", env_var="INKDROP_COMICSCODES_DISCOVERY_SCRIPT")
SLSKD_SOURCE_PROBE_SCRIPT = script_path("inkdrop_slskd_source_probe.py", env_var="INKDROP_SLSKD_SOURCE_PROBE_SCRIPT")
SLSKD_SOURCE_PROBE_MODULE_SCRIPT = script_path("inkdrop_slskd_source_probe.py", fallback=SLSKD_SOURCE_PROBE_SCRIPT)
MANGADEX_DIRECT_SCRIPT = script_path("inkdrop_mangadex_direct.py", env_var="INKDROP_MANGADEX_DIRECT_SCRIPT")
LOCK_DIR = inkdrop_runtime_config.lock_dir()
MISSING_ACQUIRE_LOCK = LOCK_DIR / "inkdrop-missing-acquire.lock"
SOURCE_WORKER_LOCK = LOCK_DIR / "inkdrop-source-worker.lock"
SLSKD_SOURCE_PROBE_LOCK = LOCK_DIR / "inkdrop-slskd-source-probe.lock"
MANGADEX_DIRECT_LOCK = LOCK_DIR / "inkdrop-mangadex-direct.lock"
SOURCE_WORKER_STAGING_ROOT = Path(os.environ.get("INKDROP_SOURCE_WORKER_STAGING_ROOT") or STAGING_DIR / "source-worker")
WEB_BASE_URL = inkdrop_runtime_config.worker_web_base_url()

QUEUE_SCHEMA_VERSION = 2
DEFAULT_SOURCE_ORDER = ["local", "prowlarr", "rss", "comicscodes", "slskd"]
SOURCE_ORDER = list(DEFAULT_SOURCE_ORDER)
VALID_SOURCE_ORDER = set(DEFAULT_SOURCE_ORDER) | {"mangadex"}
SOURCE_PROVIDER_IDS = {
    "prowlarr": "prowlarr",
    "rss": "rss",
    "comicscodes": "comicscodes",
    "slskd": "slskd",
    "mangadex": "mangadex",
}
PROVIDER_SOURCE_ENABLED = {source: True for source in SOURCE_PROVIDER_IDS}
PROVIDER_SOURCE_DISABLED_REASONS = {}
RETIRED_QUEUE_STATES = {"superseded_duplicate"}
TERMINAL_QUEUE_STATES = {"verified", *RETIRED_QUEUE_STATES}
SOURCE_WAIT_QUEUE_STATES = {"source_wait"}
ACTIVE_QUEUE_STATES = {"downloading", "importing"} | SOURCE_WAIT_QUEUE_STATES
RECOVERY_STEPS = ["failed_retry"]
PUBLIC_SOURCE_NAMES = {
    "failed_retry": "prowlarr",
    "prowlarr": "Prowlarr",
    "rss": "RSS",
    "comicscodes": "ComicsCodes",
    "slskd": "SLSKD",
    "mangadex": "MangaDex",
}
STALE_SLSKD_IMPORT_SIGNAL_SECONDS = 45 * 60
STALE_SLSKD_DETECTED_FILE_SECONDS = 24 * 3600
STALE_DOWNLOADER_SEND_SECONDS = 10 * 60
STALE_SEARCH_SOURCE_MARKER_SECONDS = 45 * 60
STALE_DOWNLOADER_SEND_EVENT = "stale downloader send cleared; no client or import evidence found"
STALE_DOWNLOADER_CONTINUE_EVENT = "downloader candidate vanished; continuing source ladder"
STALE_DOWNLOADER_OUTCOME_REASON = "stale_downloader_send_no_client"
PACK_IMPORT_ACTIVE_SECONDS = 2 * 60 * 60
DEFAULT_RETRY_SECONDS = 30 * 60
DEFAULT_PROWLARR_LIMIT = 20
DEFAULT_PROWLARR_MAX_QUERIES_PER_ISSUE = 6
DEFAULT_PROWLARR_TIMEOUT_SECONDS = 12.0
DEFAULT_PROWLARR_COMMAND_TIMEOUT_SECONDS = 60
DEFAULT_PROWLARR_SEARCH_BUDGET_SECONDS = 45
DEFAULT_PROWLARR_PROVIDER_TIMEOUT_WINDOW_SECONDS = 1800
DEFAULT_PROWLARR_PROVIDER_TIMEOUT_THRESHOLD = 3
DEFAULT_PROWLARR_PROVIDER_TIMEOUT_COOLDOWN_SECONDS = 1800
DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS = 1800
DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_THRESHOLD = 2
DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS = 1800
DEFAULT_FAILED_RETRY_COMMAND_TIMEOUT_SECONDS = 60
STALE_PROWLARR_SOURCE_MARKER_SECONDS = max(180, DEFAULT_PROWLARR_COMMAND_TIMEOUT_SECONDS + 60)
DEFAULT_STARTUP_SYNC_TIMEOUT_SECONDS = 20
DEFAULT_METADATA_ADAPTER_SYNC_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RUN_SECONDS = 8 * 60
DEFAULT_AUTOPILOT_IMPORT_BACKLOG_PRIORITY_MIN = 1
DEFAULT_AUTOPILOT_IMPORT_BACKLOG_HARD_LIMIT = 24
try:
    AUTOPILOT_IMPORT_BACKLOG_PRIORITY_MIN = max(
        1,
        int(
            os.environ.get(
                "INKDROP_AUTOPILOT_IMPORT_BACKLOG_PRIORITY_MIN",
                os.environ.get(
                    "INKDROP_QUEUE_RUNNER_IMPORT_PRIORITY_READY_IMPORTS",
                    str(DEFAULT_AUTOPILOT_IMPORT_BACKLOG_PRIORITY_MIN),
                ),
            )
            or str(DEFAULT_AUTOPILOT_IMPORT_BACKLOG_PRIORITY_MIN)
        ),
    )
except (TypeError, ValueError):
    AUTOPILOT_IMPORT_BACKLOG_PRIORITY_MIN = DEFAULT_AUTOPILOT_IMPORT_BACKLOG_PRIORITY_MIN
try:
    AUTOPILOT_IMPORT_BACKLOG_HARD_LIMIT = max(
        1,
        int(
            os.environ.get(
                "INKDROP_AUTOPILOT_IMPORT_BACKLOG_HARD_LIMIT",
                str(DEFAULT_AUTOPILOT_IMPORT_BACKLOG_HARD_LIMIT),
            )
            or str(DEFAULT_AUTOPILOT_IMPORT_BACKLOG_HARD_LIMIT)
        ),
    )
except (TypeError, ValueError):
    AUTOPILOT_IMPORT_BACKLOG_HARD_LIMIT = DEFAULT_AUTOPILOT_IMPORT_BACKLOG_HARD_LIMIT
# How long a backlog may hold search priority without ever getting smaller.
# Past this, searching resumes on the normal rotation: a backlog that is not
# draining is not going to drain because we waited longer, and the whole
# catalog should not stop looking for issues while one stuck row sits there.
DEFAULT_AUTOPILOT_IMPORT_BACKLOG_STALL_SECONDS = 2 * 60 * 60
try:
    AUTOPILOT_IMPORT_BACKLOG_STALL_SECONDS = max(
        0,
        int(
            os.environ.get(
                "INKDROP_AUTOPILOT_IMPORT_BACKLOG_STALL_SECONDS",
                str(DEFAULT_AUTOPILOT_IMPORT_BACKLOG_STALL_SECONDS),
            )
            or str(DEFAULT_AUTOPILOT_IMPORT_BACKLOG_STALL_SECONDS)
        ),
    )
except (TypeError, ValueError):
    AUTOPILOT_IMPORT_BACKLOG_STALL_SECONDS = DEFAULT_AUTOPILOT_IMPORT_BACKLOG_STALL_SECONDS
# How many cycles in a row may defer searching to drain imports. A draining
# backlog resets the stall window on every new low, so without this a healthy
# import queue can defer search indefinitely -- which it did.
DEFAULT_AUTOPILOT_IMPORT_BACKLOG_MAX_CONSECUTIVE_DEFERS = 4
try:
    AUTOPILOT_IMPORT_BACKLOG_MAX_CONSECUTIVE_DEFERS = max(
        0,
        int(
            os.environ.get(
                "INKDROP_AUTOPILOT_IMPORT_BACKLOG_MAX_CONSECUTIVE_DEFERS",
                str(DEFAULT_AUTOPILOT_IMPORT_BACKLOG_MAX_CONSECUTIVE_DEFERS),
            )
            or str(DEFAULT_AUTOPILOT_IMPORT_BACKLOG_MAX_CONSECUTIVE_DEFERS)
        ),
    )
except (TypeError, ValueError):
    AUTOPILOT_IMPORT_BACKLOG_MAX_CONSECUTIVE_DEFERS = (
        DEFAULT_AUTOPILOT_IMPORT_BACKLOG_MAX_CONSECUTIVE_DEFERS
    )
IMPORT_BACKLOG_GATE_STATE_FILENAME = "import-backlog-gate.json"
# Same rule as the import backlog above, for accepted downloads still waiting on
# a client job. Shorter window: a handoff is one API call to qBittorrent or SAB,
# and the web app relaunches that runner on its own schedule, so half an hour of
# no progress means something is wrong with that row, not that we waited badly.
# Measured 2026-07-27: one row that never handed off stopped every search in the
# catalog for 3.5 hours, across 163 consecutive aborted passes.
DEFAULT_AUTOPILOT_HANDOFF_GATE_STALL_SECONDS = 30 * 60
try:
    AUTOPILOT_HANDOFF_GATE_STALL_SECONDS = max(
        0,
        int(
            os.environ.get(
                "INKDROP_AUTOPILOT_HANDOFF_GATE_STALL_SECONDS",
                str(DEFAULT_AUTOPILOT_HANDOFF_GATE_STALL_SECONDS),
            )
            or str(DEFAULT_AUTOPILOT_HANDOFF_GATE_STALL_SECONDS)
        ),
    )
except (TypeError, ValueError):
    AUTOPILOT_HANDOFF_GATE_STALL_SECONDS = DEFAULT_AUTOPILOT_HANDOFF_GATE_STALL_SECONDS
HANDOFF_GATE_STATE_FILENAME = "download-handoff-gate.json"
# Enough to tell one stuck row from a real backlog without paying for a full
# scan; each candidate costs its own queue lookup.
HANDOFF_GATE_PENDING_COUNT_LIMIT = 25
MIN_BUDGET_RETRY_SECONDS = 5 * 60
RUNTIME_CHILD_CLEANUP_SECONDS = 25
SLSKD_HANDOFF_RESERVE_SECONDS = 90
RUNTIME_HARD_EXIT_GRACE_SECONDS = 90
RUNTIME_BUDGET_CHILD_PROVIDER_SAMPLE_LIMIT = 12
RUNTIME_BUDGET_CHILD_PROVIDER_JOB_LIMIT = 20
PROWLARR_COMMAND_TIMEOUT_HEADROOM_SECONDS = 10
DEFAULT_SLSKD_SOURCE_LOCK_WAIT_SECONDS = 5
INKDROP_STATE_FINAL_SYNC_TIMEOUT_SECONDS = 90
INKDROP_STATE_FINAL_SYNC_BUSY_TIMEOUT_MS = 90000
INKDROP_STATE_FINAL_SYNC_LOCK_ATTEMPTS = 8
INKDROP_STATE_FINAL_SYNC_INITIAL_DELAY = 2.0
DEFAULT_ANNOTATE_TIMEOUT_SECONDS = 60
DEFAULT_DISCOVERY_LIMIT = 24
DEFAULT_DISCOVERY_MAX_AUTO = 6
DEFAULT_DISCOVERY_MAX_PER_SERIES = 3
DEFAULT_RSS_COMMAND_TIMEOUT_SECONDS = 120
DEFAULT_RSS_SOURCE_WORKER_HTTP_TIMEOUT_SECONDS = 12
DEFAULT_RSS_PROVIDER_TIMEOUT_WINDOW_SECONDS = 1800
DEFAULT_RSS_PROVIDER_TIMEOUT_THRESHOLD = 3
DEFAULT_RSS_PROVIDER_TIMEOUT_COOLDOWN_SECONDS = 1800
DEFAULT_RSS_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS = 1800
DEFAULT_RSS_PROVIDER_FETCH_FAILURE_THRESHOLD = 2
DEFAULT_RSS_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS = 1800
DEFAULT_COMICSCODES_COMMAND_TIMEOUT_SECONDS = 120
DEFAULT_MANGADEX_COMMAND_TIMEOUT_SECONDS = 360
DEFAULT_MANGADEX_VERIFY_TIMEOUT_SECONDS = 90
STALE_RSS_SOURCE_MARKER_SECONDS = max(180, DEFAULT_RSS_COMMAND_TIMEOUT_SECONDS + 60)
STALE_COMICSCODES_SOURCE_MARKER_SECONDS = max(180, DEFAULT_COMICSCODES_COMMAND_TIMEOUT_SECONDS + 60)
STALE_MANGADEX_SOURCE_MARKER_SECONDS = max(180, DEFAULT_MANGADEX_COMMAND_TIMEOUT_SECONDS + 60)
SLSKD_USER_LOAD_RETRY_SECONDS = 3 * 60
SLSKD_TRANSIENT_RETRY_SECONDS = 5 * 60
SLSKD_CACHED_RETRY_LOOKAHEAD_SECONDS = 5 * 60
SLSKD_ZERO_RESULT_REPROBE_SECONDS = 60 * 60
DEFAULT_SLSKD_MAX_TOTAL = 20
DEFAULT_SLSKD_MAX_PER_SERIES = 12
DEFAULT_SLSKD_WAIT_SECONDS = 8
SOURCE_WORKER_PROWLARR_ALLOWED_HOSTS = tuple(
    host.strip()
    for host in str(os.environ.get("INKDROP_SOURCE_WORKER_PROWLARR_ALLOWED_HOSTS") or "").split(",")
    if host.strip()
)
SOURCE_WORKER_RSS_ALLOWED_HOST_FALLBACKS = ("getcomics.org", "www.getcomics.org")
SOURCE_WORKER_RSS_DIRECT_ALLOWED_HOST_FALLBACKS = (
    "getcomics.org",
    "www.getcomics.org",
    "pixeldrain.com",
    "www.pixeldrain.com",
)
SOURCE_WORKER_RSS_SHARED_FILE_HOST_DIRECT_HOSTS = {
    "pixeldrain": ("pixeldrain.com", "www.pixeldrain.com"),
}
SOURCE_WORKER_MANGADEX_ALLOWED_HOSTS = ("api.mangadex.org", "mangadex.org", "uploads.mangadex.org")
# The wildcard entry covers MangaDex@Home page servers, which use per-node
# hostnames handed out at runtime (cmdxd98sb0x3yprd.mangadex.network, ...) --
# no static exact list can name them. The transport layer's host caps
# understand '*.domain' families (inkdrop_source_worker_http._split_host_cap);
# candidates still carry their own concrete-host allowlists enumerated from
# the actual page URLs, so this widens nothing beyond the mangadex CDN.
SOURCE_WORKER_MANGADEX_DIRECT_ALLOWED_HOSTS = ("uploads.mangadex.org", "*.mangadex.network")
SOURCE_WORKER_PROWLARR_DEFAULT_JOB_LIMIT = 20
SOURCE_WORKER_RSS_DEFAULT_JOB_LIMIT = 5
SOURCE_WORKER_MANGADEX_DEFAULT_JOB_LIMIT = 5
DEFAULT_SLSKD_MAX_QUERIES = 5
DEFAULT_SLSKD_COOLDOWN_HOURS = 0.75
DEFAULT_SLSKD_AUTO_GRAB_MAX = 8
DEFAULT_SLSKD_PROBE_BUDGET_SECONDS = 300
DEFAULT_SLSKD_BROAD_MAX_TOTAL = 8
DEFAULT_SLSKD_BROAD_PROBE_BUDGET_SECONDS = 120
DEFAULT_SLSKD_BROAD_MIN_PROBE_BUDGET_SECONDS = 60
DEFAULT_EXHAUSTION_CYCLES = 6
ADAPTIVE_SLSKD_MIN_LADDER_ATTEMPTS = 2
AUTOMATION_RETRY_GENERATION = 2
NO_ACTIONABLE_SOURCE_RETRY_SECONDS = 60 * 60
STARTUP_RETRY_COOLDOWN_ANNOTATION_ROWS = 40
EXTENDED_SOURCE_RETRY_STEPS = (
    (30, 24 * 60 * 60),
    (24, 12 * 60 * 60),
    (18, 6 * 60 * 60),
    (12, 3 * 60 * 60),
)
EXHAUSTION_ANNOTATION_GRACE_SECONDS = 10 * 60
LIBRARY_IMPORT_SCAN_RETRY_SECONDS = 20 * 60
LIBRARY_IMPORT_SCAN_RETRY_LIMIT = 2
LIBRARY_IMPORT_SCAN_RETRY_SETTLE_SECONDS = 10 * 60
KAVITA_IMPORT_SCAN_RETRY_SECONDS = LIBRARY_IMPORT_SCAN_RETRY_SECONDS
KAVITA_IMPORT_SCAN_RETRY_LIMIT = LIBRARY_IMPORT_SCAN_RETRY_LIMIT
KAVITA_IMPORT_SCAN_RETRY_SETTLE_SECONDS = LIBRARY_IMPORT_SCAN_RETRY_SETTLE_SECONDS
DEFERRED_MANUAL_SOURCE_QUEUE_SYNC_TTL_SECONDS = 48 * 3600
DEFERRED_MANUAL_SOURCE_QUEUE_SYNC_MAX_ITEMS = 80
FAILED_RECONCILIATION_STATES = {"failed_download", "bad_archive", "false_positive", "stale_no_local_file", "wrong_series_or_subseries"}
FAILED_RETRY_CONTINUE_REASONS = {
    "alternate_already_attempted",
    "alternate_attempt_budget_exhausted",
    "alternate_attempts_exhausted",
    "failed_record_not_currently_missing",
    "pack_candidate_not_actionable",
}
SOURCE_HEALTH_BLOCKING_STATES = {"backoff", "disabled", "error", "failed", "unavailable", "watch"}
SOURCE_HEALTH_GATED_SOURCES = {"prowlarr", "rss", "comicscodes", "slskd"}


def public_source_name(source):
    if source in (None, ""):
        return None
    source = str(source)
    return PUBLIC_SOURCE_NAMES.get(source, source)


def source_attempt_event(source):
    source = str(source or "").strip()
    labels = {
        "local": "checking local library",
        "prowlarr": "searching indexers",
        "failed_retry": "retrying next indexer candidate",
        "rss": "checking RSS releases",
        "comicscodes": "checking ComicsCodes",
        "slskd": "searching SLSKD candidates",
    }
    return labels.get(source, f"searching {public_source_name(source) or source}")


def public_event_label(event):
    text = str(event or "").strip()
    lower = text.lower()
    if lower.startswith("trying "):
        return source_attempt_event(text.split(None, 1)[1])
    if lower == "manual source autoresolver verification_pending":
        return "copied; waiting for library scan"
    if lower == "manual source autoresolver imported_not_resolved":
        return "copied; waiting for library verification"
    if lower == "manual source autoresolver preview_importable":
        return "staged file is importable; import worker will pick it up"
    return text


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


def wrong_language_quarantine_active(item):
    if not isinstance(item, dict):
        return False
    markers = {
        str(item.get("last_import_status") or "").lower(),
        str(item.get("last_bad_slskd_reason") or "").lower(),
    }
    if "wrong_language_source" in markers:
        return True
    event = str(item.get("last_event") or "").lower()
    return "wrong language" in event or "raw/japanese" in event or "non-english" in event


NEEDS_SOURCE_REASONS = {
    "no_safe_source",
    "no_exact_result",
    "no_safe_alternate_found",
    "prowlarr_search_error",
    "manga_no_safe_result",
    "download_client_send_failed",
    "failed_download_duplicate_nzb",
}
AUTOMATION_REVIEW_REASONS = {
    "ambiguous_results",
    "pack_candidate_requires_review",
    "rss_pack_requires_review",
}
HUMAN_REVIEW_REASONS = {
    "unsafe_or_missing_target_folder",
}
SOFT_REVIEW_REASONS = NEEDS_SOURCE_REASONS | AUTOMATION_REVIEW_REASONS
SLSKD_NO_AUTOMATIC_RESULT_STATES = {"searched_no_candidates", "no_query", "failed_candidates_exhausted"}
SLSKD_PROVIDER_WAIT_RESULT_STATES = {"api_error", "provider_unavailable", "provider_wait"}
SLSKD_TRANSIENT_RESULT_STATES = {"error", "api_error", "timeout", "probe_error"}
SLSKD_AUTOPICK_SIGNAL_EVENTS = {
    "SLSKD candidates available for autopick",
    "SLSKD candidate found for autopick",
    "SLSKD candidate would be auto-picked",
}
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
AUTOPILOT_POLICY = (
    "InkDrop-owned SQLite wanted/queue rows are the acquisition source of truth; metadata adapters "
    "only fill temporary gaps. The queue tries managed-folder/library truth by sync, Prowlarr/SAB/qB, RSS, "
    "ComicsCodes, and then SLSKD best-candidate autopick. "
    "Failed-download alternate retry runs as an automatic recovery step inside the ladder. "
    "Rows that exhaust the source ladder are parked for scheduled retry. Manual Review is reserved "
    "for genuinely unsafe path/import decisions."
)
DEFERRED_MANUAL_SOURCE_SYNC_ACK_IDS = set()


def watch_auto_grab_enabled(watch):
    return bool((watch or {}).get("autoGrab", True))


def now_iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def numeric_timestamp(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def touch_queue_item(item, now=None):
    if not isinstance(item, dict):
        return False
    now = now or time.time()
    item["updated_at"] = now
    item["updated_at_iso"] = now_iso(now)
    return True


def deferred_manual_source_queue_sync_entries():
    now = time.time()
    valid = []
    seen_ids = set()

    def add_entry(entry):
        if not isinstance(entry, dict):
            return
        created_at = numeric_timestamp(entry.get("created_at") or entry.get("ts"))
        if created_at and now - created_at > DEFERRED_MANUAL_SOURCE_QUEUE_SYNC_TTL_SECONDS:
            return
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        if not isinstance(result.get("processed"), list) and not isinstance(result.get("skipped"), list):
            return
        sync_id = str(entry.get("id") or "").strip()
        if sync_id:
            if sync_id in seen_ids:
                return
            seen_ids.add(sync_id)
        valid.append(entry)

    if inkdrop_state is not None:
        try:
            for row in inkdrop_state.pending_deferred_queue_sync_rows(
                INKDROP_STATE_DB,
                source="manual_source_autoresolve",
                ttl_seconds=DEFERRED_MANUAL_SOURCE_QUEUE_SYNC_TTL_SECONDS,
                limit=DEFERRED_MANUAL_SOURCE_QUEUE_SYNC_MAX_ITEMS,
            ):
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else row.get("result")
                add_entry({
                    "id": row.get("id"),
                    "source": row.get("source"),
                    "reason": row.get("reason"),
                    "created_at": row.get("created_at"),
                    "created_at_iso": row.get("created_at_iso"),
                    "row_count": row.get("row_count"),
                    "result": payload if isinstance(payload, dict) else {},
                    "_deferred_queue_sync_store": "sqlite",
                })
        except Exception as exc:
            log("manual_source_queue_sync_db_read_failed", error=f"{type(exc).__name__}: {exc}")

    data = read_json(MANUAL_SOURCE_QUEUE_SYNC_FILE, {}) or {}
    entries = data.get("items") if isinstance(data, dict) else []
    if not isinstance(entries, list):
        entries = []
    for entry in entries:
        add_entry(entry)
    return valid


def manual_source_autoresolve_snapshots(current_status):
    snapshots = []
    for entry in deferred_manual_source_queue_sync_entries():
        result = dict(entry.get("result") or {})
        result["_deferred_queue_sync_id"] = entry.get("id")
        result.setdefault("state", "deferred")
        snapshots.append(result)
    if isinstance(current_status, dict) and current_status:
        snapshots.append(current_status)
    return snapshots


def replay_deferred_native_autoresolve_attempts(snapshot):
    """Replay DB-lock deferred native attempts before acknowledging queue projection."""
    rows = snapshot.get("native_attempt_replay") if isinstance(snapshot, dict) else []
    if not isinstance(rows, list) or not rows:
        return {"ok": True, "attempted": 0, "recorded": 0}
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing", "attempted": len(rows), "recorded": 0}
    recorded = 0
    for row in rows:
        if not isinstance(row, dict) or not row.get("queue_id") or not isinstance(row.get("attempt"), dict):
            continue
        try:
            result = inkdrop_state.record_queue_source_attempt(
                INKDROP_STATE_DB,
                row["queue_id"],
                row["attempt"],
                attempt_id=row.get("attempt_id"),
                started_at=row.get("started_at"),
                completed_at=row.get("completed_at"),
            )
        except Exception as exc:
            log(
                "manual_source_native_attempt_replay_failed",
                queue_id=row.get("queue_id"),
                status=(row.get("attempt") or {}).get("status"),
                error=f"{type(exc).__name__}: {exc}",
            )
            return {"ok": False, "reason": "record_failed", "attempted": len(rows), "recorded": recorded}
        if not isinstance(result, dict) or not result.get("ok"):
            return {
                "ok": False,
                "reason": (result or {}).get("reason") if isinstance(result, dict) else "record_failed",
                "attempted": len(rows),
                "recorded": recorded,
            }
        recorded += 1
    return {"ok": True, "attempted": len(rows), "recorded": recorded}


def mark_deferred_manual_source_sync_applied(sync_id):
    sync_id = str(sync_id or "").strip()
    if sync_id:
        if inkdrop_state is not None:
            try:
                inkdrop_state.mark_deferred_queue_sync_applied(INKDROP_STATE_DB, sync_id)
            except Exception as exc:
                log("manual_source_queue_sync_db_mark_failed", sync_id=sync_id, error=f"{type(exc).__name__}: {exc}")
        DEFERRED_MANUAL_SOURCE_SYNC_ACK_IDS.add(sync_id)


def ack_deferred_manual_source_queue_syncs():
    ack_ids = set(DEFERRED_MANUAL_SOURCE_SYNC_ACK_IDS)
    if inkdrop_state is not None:
        if ack_ids:
            try:
                result = inkdrop_state.ack_deferred_queue_syncs(INKDROP_STATE_DB, ack_ids)
                if result.get("acknowledged"):
                    log("manual_source_queue_sync_db_ack", acknowledged=result.get("acknowledged"))
            except Exception as exc:
                log("manual_source_queue_sync_db_ack_failed", error=f"{type(exc).__name__}: {exc}")
        try:
            stale_result = inkdrop_state.ack_applied_deferred_queue_syncs(
                INKDROP_STATE_DB,
                source="manual_source_autoresolve",
                older_than_seconds=60,
                limit=200,
            )
            if stale_result.get("acknowledged"):
                log("manual_source_queue_sync_db_ack_applied", acknowledged=stale_result.get("acknowledged"))
        except Exception as exc:
            log("manual_source_queue_sync_db_ack_applied_failed", error=f"{type(exc).__name__}: {exc}")
    if ack_ids:
        try:
            data = read_json(MANUAL_SOURCE_QUEUE_SYNC_FILE, {}) or {}
            entries = data.get("items") if isinstance(data, dict) else []
            if not isinstance(entries, list):
                DEFERRED_MANUAL_SOURCE_SYNC_ACK_IDS.clear()
                return
            kept = [entry for entry in entries if not isinstance(entry, dict) or str(entry.get("id") or "") not in ack_ids]
            if len(kept) != len(entries):
                if kept:
                    data["items"] = kept
                    data["updated_at"] = time.time()
                    data["updated_at_iso"] = now_iso(data["updated_at"])
                    write_json(MANUAL_SOURCE_QUEUE_SYNC_FILE, data)
                else:
                    try:
                        MANUAL_SOURCE_QUEUE_SYNC_FILE.unlink()
                    except FileNotFoundError:
                        pass
                log("manual_source_queue_sync_ack", acknowledged=len(entries) - len(kept))
        except Exception as exc:
            log("manual_source_queue_sync_ack_failed", error=f"{type(exc).__name__}: {exc}")
            return
    DEFERRED_MANUAL_SOURCE_SYNC_ACK_IDS.clear()


def slskd_attempted_at(item):
    if not isinstance(item, dict):
        return 0
    for key in ("autopilot_slskd_attempted_at", "last_slskd_at"):
        ts = numeric_timestamp(item.get(key))
        if ts > 0:
            return ts
    return 0


def normalize_slskd_attempt_marker(item, now):
    attempted_at = slskd_attempted_at(item) or now
    if numeric_timestamp(item.get("autopilot_slskd_attempted_at")):
        return False
    item["autopilot_slskd_attempted_at"] = attempted_at
    item["autopilot_slskd_attempted_at_iso"] = now_iso(attempted_at)
    item["historical_slskd_attempt_normalized_at"] = now
    item["historical_slskd_attempt_normalized_at_iso"] = now_iso(now)
    return True


def clear_soft_review_metadata(item):
    if str(item.get("last_review_reason") or "") in SOFT_REVIEW_REASONS:
        item.pop("last_review_reason", None)
        item.pop("last_review_source", None)
        item.pop("last_review_at", None)
        item.pop("last_review_at_iso", None)
    if str(item.get("needs_you_reason") or "") in SOFT_REVIEW_REASONS:
        item.pop("needs_you_reason", None)


def record_automation_source_outcome(item, reason, source, now, row=None):
    item["last_source_outcome_reason"] = reason
    item["last_source_outcome_source"] = source
    item["last_source_outcome_at"] = now
    item["last_source_outcome_at_iso"] = now_iso(now)
    if isinstance(row, dict):
        if row.get("query"):
            item["last_source_outcome_query"] = row.get("query")
        if row.get("candidate") and isinstance(row.get("candidate"), dict):
            item["last_source_outcome_candidate"] = (row.get("candidate") or {}).get("title")
    clear_soft_review_metadata(item)


def read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


_SLSKD_SOURCE_CACHE_SNAPSHOT = {"mtime": None, "data": None}


def slskd_source_probe_cache():
    try:
        mtime = SLSKD_SOURCE_PROBE_CACHE_FILE.stat().st_mtime
    except OSError:
        _SLSKD_SOURCE_CACHE_SNAPSHOT["mtime"] = None
        _SLSKD_SOURCE_CACHE_SNAPSHOT["data"] = {}
        return {}
    if _SLSKD_SOURCE_CACHE_SNAPSHOT.get("mtime") != mtime:
        _SLSKD_SOURCE_CACHE_SNAPSHOT["mtime"] = mtime
        _SLSKD_SOURCE_CACHE_SNAPSHOT["data"] = read_json(SLSKD_SOURCE_PROBE_CACHE_FILE, {}) or {}
    data = _SLSKD_SOURCE_CACHE_SNAPSHOT.get("data")
    return data if isinstance(data, dict) else {}


def connect_imported_db():
    con = sqlite3.connect(IMPORTED_DB, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    con.execute(f"pragma busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    con.row_factory = sqlite3.Row
    return con


def log(event, **payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "ts_iso": now_iso(), "event": event, **payload}
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def sync_inkdrop_queue_state(
    force=False,
    reason="queue_update",
    timeout_seconds=None,
    busy_timeout_ms=None,
    lock_attempts=None,
    lock_initial_delay=None,
    sync_mode="queue",
):
    global LAST_INKDROP_STATE_QUEUE_SYNC
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    now = time.time()
    if not force and LAST_INKDROP_STATE_QUEUE_SYNC and now - LAST_INKDROP_STATE_QUEUE_SYNC < INKDROP_STATE_QUEUE_SYNC_MIN_SECONDS:
        return {"ok": True, "skipped": "throttled"}
    try:
        kwargs = {}
        if timeout_seconds is not None:
            kwargs["timeout_seconds"] = timeout_seconds
        if busy_timeout_ms is not None:
            kwargs["busy_timeout_ms"] = busy_timeout_ms
        if lock_attempts is not None:
            kwargs["lock_attempts"] = lock_attempts
        if lock_initial_delay is not None:
            kwargs["lock_initial_delay"] = lock_initial_delay
        kwargs["mode"] = sync_mode
        summary = inkdrop_state.sync_queue_state(STATE_DIR, INKDROP_STATE_DB, **kwargs)
        LAST_INKDROP_STATE_QUEUE_SYNC = now
        return summary
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        locked = "database is locked" in str(exc).lower()
        result = {
            "ok": False,
            "reason": "database_locked" if locked else "inkdrop_state_queue_sync_failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "locked": locked,
        }
        log("inkdrop_state_queue_sync_failed", reason=reason, error=error, locked=locked)
        return result


def inkdrop_state_sync_failed_due_to_lock(result):
    if not isinstance(result, dict) or result.get("ok") is not False:
        return False
    if result.get("locked"):
        return True
    text = " ".join(
        str(result.get(key) or "")
        for key in ("reason", "error", "error_type")
    ).lower()
    return "database is locked" in text or "database_locked" in text


def mark_inkdrop_state_sync_pending(queue, result, reason):
    if not isinstance(queue, dict):
        return
    now = time.time()
    pending = {
        "reason": reason,
        "error": str((result or {}).get("error") or ""),
        "result_reason": str((result or {}).get("reason") or ""),
        "locked": inkdrop_state_sync_failed_due_to_lock(result),
        "at": now,
        "at_iso": now_iso(now),
    }
    previous = queue.get("inkdrop_state_sync_pending") if isinstance(queue.get("inkdrop_state_sync_pending"), dict) else {}
    queue["inkdrop_state_sync_pending"] = pending
    previous_at = numeric_timestamp(previous.get("at")) if previous else 0
    if not previous or previous.get("reason") != reason or now - previous_at > 300:
        queue.setdefault("history", []).append(
            {
                "ts": now,
                "ts_iso": now_iso(now),
                "event": "inkdrop_state_sync_deferred",
                "reason": reason,
                "result_reason": pending["result_reason"],
                "locked": pending["locked"],
            }
        )


def clear_inkdrop_state_sync_pending(queue):
    if isinstance(queue, dict) and queue.pop("inkdrop_state_sync_pending", None):
        now = time.time()
        queue.setdefault("history", []).append(
            {
                "ts": now,
                "ts_iso": now_iso(now),
                "event": "inkdrop_state_sync_deferred_cleared",
            }
        )
        return True
    return False


def runtime_deadline(args):
    try:
        seconds = int(getattr(args, "max_run_seconds", 0) or 0)
    except (TypeError, ValueError):
        seconds = 0
    return time.time() + seconds if seconds > 0 else None


def provider_protected_budget_seconds(args):
    try:
        max_run_seconds = max(0.0, float(getattr(args, "max_run_seconds", 0) or 0))
    except (TypeError, ValueError):
        max_run_seconds = 0.0
    return max_run_seconds / 2 if max_run_seconds else None


def startup_maintenance_timeout(args, requested_seconds, setup_started_monotonic, *, share=1.0):
    try:
        requested = max(1.0, float(requested_seconds or 1))
    except (TypeError, ValueError):
        requested = 1.0
    protected = provider_protected_budget_seconds(args)
    if protected is None:
        return max(1, int(requested))
    remaining = protected - max(0.0, time.monotonic() - setup_started_monotonic)
    if remaining < 1.0:
        return 0
    return max(1, int(min(requested, max(1.0, remaining * max(0.05, min(float(share), 1.0))))))


def startup_timing_summary(setup_started_monotonic, phase_seconds):
    elapsed = max(0.0, time.monotonic() - float(setup_started_monotonic))
    accounted = sum(max(0.0, float(value or 0)) for value in (phase_seconds or {}).values())
    return {
        "startup_phase_seconds": dict(phase_seconds or {}),
        "startup_elapsed_seconds": round(elapsed, 3),
        "startup_accounted_seconds": round(accounted, 3),
        "startup_unaccounted_seconds": round(max(0.0, elapsed - accounted), 3),
    }


def provider_start_timing(args, setup_started_monotonic, provider_started_monotonic=None):
    started = time.monotonic() if provider_started_monotonic is None else float(provider_started_monotonic)
    elapsed = max(0.0, started - float(setup_started_monotonic))
    protected = provider_protected_budget_seconds(args)
    return {
        "provider_work_started": True,
        "provider_work_start_elapsed_seconds": round(elapsed, 3),
        "provider_work_started_before_half_runtime": bool(protected is None or elapsed < protected),
    }


def provider_payload_verdict(source, payload, error=None):
    """Return (actual_call, healthy, reason) using each legacy adapter contract.

    ``reason`` names the branch that decided the verdict so a pass can report
    *why* a provider call was counted unhealthy instead of only how many were.
    """
    source = str(source or "").strip().lower()
    if error:
        return True, False, f"exception:{type(error).__name__}"
    if not isinstance(payload, dict) or not payload:
        return False, False, "no_payload"
    if payload.get("skipped_busy") or payload.get("skipped") is True:
        return False, False, "skipped"
    skips = payload.get("skips") if isinstance(payload.get("skips"), list) else []
    disabled = str(payload.get("status") or "").strip().upper() == "DISABLED" or any(
        isinstance(row, dict) and str(row.get("reason") or "").strip() == "provider_disabled"
        for row in skips
    )
    if disabled:
        return False, False, "provider_disabled"
    try:
        attempted_total = int(payload.get("attempted_total") or 0) if "attempted_total" in payload else None
    except (TypeError, ValueError):
        attempted_total = 0
    if source in {"prowlarr", "failed_retry"} and attempted_total is not None and attempted_total <= 0:
        return False, False, "nothing_attempted"
    if source == "slskd" and int(payload.get("checked_count") or 0) <= 0:
        return False, False, "nothing_checked"
    actual_attempt = bool(attempted_total and attempted_total > 0)
    if (
        payload.get("budget_skipped")
        or payload.get("budget_skipped_queue_ids")
        or payload.get("search_budget_exhausted")
    ) and not actual_attempt:
        return False, False, "budget_skipped"
    if payload.get("timed_out") or payload.get("command_timed_out"):
        return True, False, "timed_out"
    if payload.get("errors"):
        return True, False, "errors_reported"
    try:
        if int(payload.get("failed") or 0) > 0:
            return True, False, "failed_items"
    except (TypeError, ValueError):
        return True, False, "failed_unparseable"
    if source == "mangadex" and int(payload.get("rows_considered") or 0) <= 0:
        return False, False, "nothing_considered"
    # Legacy Prowlarr/RSS/ComicsCodes payloads predate the explicit ok field.
    if payload.get("ok") is False:
        detail = str(payload.get("reason") or payload.get("error") or "").strip()
        return True, False, (f"ok_false:{detail[:60]}" if detail else "ok_false")
    return True, True, "healthy"


def provider_payload_outcome(source, payload, error=None):
    """Return (actual_call, healthy) using each legacy adapter contract."""
    actual_call, healthy, _reason = provider_payload_verdict(source, payload, error=error)
    return actual_call, healthy


def provider_call_id(source):
    return f"{str(source or 'provider').strip().lower()}:{time.monotonic_ns()}"


def provider_start_allowed(observer, source, series, started_monotonic, call_id):
    if not observer:
        return True
    decision = observer(
        {
            "phase": "permission",
            "call_id": call_id,
            "source": source,
            "series": series,
            "started_monotonic": float(started_monotonic),
            "healthy": None,
        }
    )
    return decision is not False


def observe_provider_result(observer, source, series, started_monotonic, payload=None, error=None, call_id=None):
    """Publish only calls proven to have crossed an adapter boundary."""
    if not observer:
        return
    actual_call, healthy, reason = provider_payload_verdict(source, payload, error=error)
    if not actual_call:
        return
    call_id = call_id or provider_call_id(source)
    base = {
        "call_id": call_id,
        "source": source,
        "series": series,
        "started_monotonic": float(started_monotonic),
    }
    observer({**base, "phase": "start", "healthy": None, "failed": False, "error": ""})
    observer(
        {
            **base,
            "phase": "finish",
            "healthy": healthy,
            "failed": bool(error or not healthy),
            "reason": reason,
            "error": str(error or ""),
        }
    )


def run_observed_provider_call(source, series, observer, callback):
    started_monotonic = time.monotonic()
    call_id = provider_call_id(source)
    if not provider_start_allowed(observer, source, series, started_monotonic, call_id):
        return {
            "ok": False,
            "skipped": True,
            "provider_start_deadline_missed": True,
            "reason": "provider_start_deadline_missed",
        }
    try:
        payload = callback()
    except Exception as exc:
        observe_provider_result(observer, source, series, started_monotonic, error=exc, call_id=call_id)
        raise
    observe_provider_result(observer, source, series, started_monotonic, payload=payload, call_id=call_id)
    return payload


def record_provider_observation(sync_result, args, setup_started_monotonic, observation, call_states=None):
    if not isinstance(sync_result, dict) or not isinstance(observation, dict):
        return False
    call_states = call_states if isinstance(call_states, dict) else {}
    call_id = str(observation.get("call_id") or "").strip() or provider_call_id(observation.get("source"))
    phase = str(observation.get("phase") or "finish").strip().lower()
    started_monotonic = float(observation.get("started_monotonic") or time.monotonic())
    timing = provider_start_timing(args, setup_started_monotonic, started_monotonic)
    if phase == "permission":
        if not sync_result.get("provider_work_started") and not timing.get("provider_work_started_before_half_runtime"):
            # Setup ran past the point where providers were supposed to get their
            # share of the pass. Record it so the pass reports the starvation, but
            # let the search run anyway: refusing here spent the rest of the
            # budget on the same maintenance that caused the delay, so the pass
            # searched nothing at all. The run deadline still stops work that
            # cannot finish.
            sync_result["provider_start_deadline_missed"] = True
            sync_result["maintenance_starved_provider"] = True
            sync_result["provider_start_deadline_elapsed_seconds"] = timing.get("provider_work_start_elapsed_seconds")
        return True
    state = call_states.setdefault(call_id, {})
    source_key = str(observation.get("source") or "unknown").strip().lower() or "unknown"
    if phase == "start" and not state.get("started"):
        state["started"] = True
        state["healthy"] = None
        sync_result.pop("provider_work_healthy", None)
        sync_result["provider_call_count"] = int(sync_result.get("provider_call_count") or 0) + 1
        by_source = sync_result.setdefault("provider_calls_by_source", {})
        by_source[source_key] = int(by_source.get(source_key) or 0) + 1
    if not sync_result.get("provider_work_started"):
        sync_result.update(
            timing
        )
        sync_result["first_provider_source"] = observation.get("source")
    if not timing.get("provider_work_started_before_half_runtime"):
        sync_result["late_provider_start"] = True
    if phase != "finish" or state.get("finished"):
        return True
    state["finished"] = True
    state["healthy"] = observation.get("healthy")
    spent = sync_result.setdefault("provider_seconds_by_source", {})
    spent[source_key] = round(
        float(spent.get(source_key) or 0.0) + max(0.0, time.monotonic() - started_monotonic), 1
    )
    if observation.get("healthy") is True:
        sync_result["provider_work_healthy"] = True
        sync_result["provider_healthy_call_count"] = int(sync_result.get("provider_healthy_call_count") or 0) + 1
    else:
        sync_result["provider_unconfirmed_call_count"] = int(sync_result.get("provider_unconfirmed_call_count") or 0) + 1
    if observation.get("failed"):
        sync_result["provider_failed_call_count"] = int(sync_result.get("provider_failed_call_count") or 0) + 1
        failed_by_source = sync_result.setdefault("provider_failed_calls_by_source", {})
        failed_by_source[source_key] = int(failed_by_source.get(source_key) or 0) + 1
        reason_key = f"{source_key}:{str(observation.get('reason') or 'unknown').strip() or 'unknown'}"
        reasons = sync_result.setdefault("provider_failure_reasons", {})
        reasons[reason_key] = int(reasons.get(reason_key) or 0) + 1
    return True


def runtime_hard_exit_grace_seconds():
    try:
        value = int(os.environ.get("INKDROP_AUTOPILOT_RUNTIME_HARD_GRACE_SECONDS", RUNTIME_HARD_EXIT_GRACE_SECONDS) or 0)
    except (TypeError, ValueError):
        value = RUNTIME_HARD_EXIT_GRACE_SECONDS
    return max(15, min(value, 15 * 60))


def install_runtime_hard_exit(args):
    if not hasattr(signal, "SIGALRM"):
        return None
    try:
        seconds = int(getattr(args, "max_run_seconds", 0) or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0 or getattr(args, "status_only", False):
        return None
    grace = runtime_hard_exit_grace_seconds()
    timeout_seconds = max(1, seconds + grace)

    def _handler(signum, frame):
        try:
            log(
                "runtime_hard_timeout_exit",
                max_run_seconds=seconds,
                grace_seconds=grace,
                timeout_seconds=timeout_seconds,
            )
        finally:
            os._exit(75)

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout_seconds)
    watchdog_pid = install_runtime_hard_exit_watchdog(seconds, grace, timeout_seconds)
    return {
        "timeout_seconds": timeout_seconds,
        "previous_handler": previous_handler,
        "watchdog_pid": watchdog_pid,
    }


def install_runtime_hard_exit_watchdog(max_run_seconds, grace_seconds, timeout_seconds):
    if not hasattr(os, "fork") or not hasattr(signal, "SIGKILL"):
        return None
    parent_pid = os.getpid()
    try:
        pid = os.fork()
    except OSError as exc:
        log("runtime_hard_timeout_watchdog_unavailable", error=f"{type(exc).__name__}: {exc}")
        return None
    if pid:
        return pid
    try:
        time.sleep(max(1, int(timeout_seconds or 1)))
        try:
            log(
                "runtime_hard_timeout_watchdog_exit",
                max_run_seconds=max_run_seconds,
                grace_seconds=grace_seconds,
                timeout_seconds=timeout_seconds,
                parent_pid=parent_pid,
            )
        finally:
            os.kill(parent_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except SystemExit:
        pass
    except BaseException as exc:
        try:
            log("runtime_hard_timeout_watchdog_error", error=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
    finally:
        os._exit(0)


def clear_runtime_hard_exit(alarm_state=None):
    if not alarm_state or not hasattr(signal, "SIGALRM"):
        return
    watchdog_pid = alarm_state.get("watchdog_pid")
    if watchdog_pid and hasattr(os, "kill") and hasattr(signal, "SIGTERM"):
        try:
            os.kill(int(watchdog_pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            pass
        if hasattr(os, "waitpid"):
            try:
                os.waitpid(int(watchdog_pid), getattr(os, "WNOHANG", 0))
            except Exception:
                pass
    signal.alarm(0)
    previous_handler = alarm_state.get("previous_handler")
    if previous_handler is not None:
        signal.signal(signal.SIGALRM, previous_handler)


def runtime_deadline_expired(deadline):
    return bool(deadline and time.time() >= float(deadline))


def runtime_seconds_remaining(deadline):
    if not deadline:
        return None
    try:
        return max(0.0, float(deadline) - time.time())
    except (TypeError, ValueError):
        return None


def runtime_deadline_too_close(deadline, minimum_seconds):
    remaining = runtime_seconds_remaining(deadline)
    if remaining is None:
        return False
    try:
        minimum = max(0.0, float(minimum_seconds or 0))
    except (TypeError, ValueError):
        minimum = 0.0
    return remaining <= minimum


def runtime_budget_skip_reason(source, deadline, minimum_seconds):
    remaining = runtime_seconds_remaining(deadline)
    remaining_label = duration_label(remaining or 0) or "0s"
    minimum_label = duration_label(minimum_seconds or 0) or "0s"
    label = public_source_name(source) or str(source or "source")
    return f"runtime budget has {remaining_label} left; {label} needs about {minimum_label}"


def run_group_start_min_seconds():
    try:
        return max(0.0, float(os.environ.get("INKDROP_AUTOPILOT_GROUP_START_MIN_SECONDS", "90")))
    except (TypeError, ValueError):
        return 90.0


def env_flag_enabled(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return str(value or "").strip().lower() not in {"0", "false", "no", "off", "disabled"}


def source_worker_lock_busy(lock_path=SOURCE_WORKER_LOCK):
    flock = Path("/usr/bin/flock")
    if os.name == "nt" or not flock.exists():
        return False
    try:
        result = subprocess.run(
            [str(flock), "-n", str(lock_path), "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except Exception as exc:
        log("source_worker_lock_probe_failed", error=f"{type(exc).__name__}: {exc}")
        return False
    return result.returncode != 0


@contextlib.contextmanager
def held_source_worker_lock(lock_path=SOURCE_WORKER_LOCK, wait_seconds=0):
    """Hold the same lock as the scheduled source worker during inline writes."""
    if os.name == "nt":
        yield True
        return
    try:
        import fcntl
    except ImportError:
        yield True
        return
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    acquired = False
    try:
        wait_deadline = time.monotonic() + max(0.0, float(wait_seconds or 0))
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= wait_deadline:
                    break
                time.sleep(min(0.1, max(0.01, wait_deadline - time.monotonic())))
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def run_source_worker_cli_locked(argv, *, source, series, missing_candidates, provider_observer=None):
    with held_source_worker_lock() as acquired:
        if not acquired:
            return {
                "ok": True,
                "mode": "source_worker",
                "source": source,
                "series": series,
                "actions": [],
                "review": [],
                "reviews": [],
                "missing_candidates": int(missing_candidates or 0),
                "attempted_total": 0,
                "skipped_busy": True,
                "reason": "source_worker_lock_busy",
            }
        def source_worker_provider_observer(observation):
            if not provider_observer or not isinstance(observation, dict):
                return
            event = dict(observation)
            event["source"] = source
            event["series"] = series
            return provider_observer(event)

        try:
            return inkdrop_source_worker_cli.run_source_worker_cli(
                argv,
                provider_observer=source_worker_provider_observer,
            )
        except inkdrop_source_worker_cli.ProviderStartDeadlineMissed:
            return {
                "ok": False,
                "mode": "source_worker",
                "source": source,
                "series": series,
                "actions": [],
                "review": [],
                "reviews": [],
                "missing_candidates": int(missing_candidates or 0),
                "attempted_total": 0,
                "skipped": True,
                "provider_start_deadline_missed": True,
                "reason": "provider_start_deadline_missed",
            }


def source_worker_pressure_yield(args):
    if getattr(args, "dry_run", False) or getattr(args, "status_only", False) or getattr(args, "annotate_only", False):
        return {}
    if getattr(args, "no_yield_to_source_worker", False):
        return {}
    if not env_flag_enabled("INKDROP_AUTOPILOT_YIELD_TO_SOURCE_WORKER", True):
        return {}
    if not source_worker_lock_busy():
        return {}
    payload = {
        "enabled": True,
        "reason": "source_worker_lock_busy",
        "lock_path": str(SOURCE_WORKER_LOCK),
        "deferred_final_sync": True,
    }
    log("source_worker_pressure_yield", **payload)
    return payload


def handoff_gate_state_path():
    return STATE_DIR / HANDOFF_GATE_STATE_FILENAME


def load_handoff_gate_state():
    try:
        with open(handoff_gate_state_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_handoff_gate_state(payload):
    try:
        path = handoff_gate_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except Exception as exc:
        log("handoff_gate_state_write_failed", error=f"{type(exc).__name__}: {exc}")


def handoff_gate_stall_state(pending, active, now=None):
    """Track whether the pending handoffs holding priority are actually clearing.

    Progress is the count going down. While it keeps dropping the clock resets
    and the gate holds priority as before. If it never gets below the smallest
    value seen for the whole window, nothing is handing off, and stopping every
    search in the catalog is only costing search cycles.
    """

    now = float(now if now is not None else time.time())
    previous = load_handoff_gate_state()
    if not active:
        if previous:
            save_handoff_gate_state({})
        return {"stalled": False, "held_seconds": 0.0, "min_pending_seen": None}
    try:
        previous_min = int(previous.get("min_pending_seen"))
    except (TypeError, ValueError):
        previous_min = None
    try:
        since = float(previous.get("since"))
    except (TypeError, ValueError):
        since = None
    progressed = previous_min is None or pending < previous_min
    if progressed or since is None:
        since = now
        previous_min = pending
    held_seconds = max(0.0, now - since)
    stalled = bool(
        AUTOPILOT_HANDOFF_GATE_STALL_SECONDS
        and held_seconds >= AUTOPILOT_HANDOFF_GATE_STALL_SECONDS
    )
    save_handoff_gate_state({
        "since": since,
        "since_iso": now_iso(since),
        "min_pending_seen": previous_min,
        "last_pending": pending,
        "last_seen_at": now,
    })
    return {"stalled": stalled, "held_seconds": round(held_seconds, 1), "min_pending_seen": previous_min}


def accepted_download_handoff_priority_gate(db_path=None, *, now=None):
    """Keep new searches behind accepted work that still needs a client job.

    Finishing an accepted download before starting more searches is right, and
    it is right almost every time this fires. Twice in two days it was not: a row
    that was never going to hand off held the gate and every series in the
    catalog stopped searching -- 3.5 hours in one stretch, 163 aborted passes.

    Two changes, the same two the import backlog gate got for the same defect.
    The gate counts what is actually pending instead of stopping at the first
    row, so a stuck singleton is distinguishable from a real backlog and shows
    up truthfully in the log. And a queue of handoffs that never shrinks gives
    priority back after INKDROP_AUTOPILOT_HANDOFF_GATE_STALL_SECONDS; zero
    disables it. The handoff runner keeps running on its own schedule either way.
    """

    if inkdrop_source_worker_coordinator is None:
        return {"active": False, "reason": "source_worker_coordinator_unavailable"}
    path = Path(db_path or INKDROP_STATE_DB)
    if not path.exists():
        return {"active": False, "reason": "state_database_missing"}
    try:
        queue_ids = inkdrop_source_worker_coordinator.pending_download_client_handoff_queue_ids(
            path,
            limit=HANDOFF_GATE_PENDING_COUNT_LIMIT,
            now=now,
        )
    except Exception as exc:
        return {
            "active": False,
            "reason": "pending_download_client_handoff_check_failed",
            "error": type(exc).__name__,
        }
    pending = len(queue_ids or [])
    active = pending > 0
    stall = handoff_gate_stall_state(pending, active, now=now)
    if stall.get("stalled"):
        active = False
    return {
        "active": active,
        "reason": (
            "handoff_backlog_stalled_search_resumed"
            if stall.get("stalled")
            else "accepted_download_waiting_for_client_job"
            if pending
            else "none"
        ),
        "pending_count": pending,
        "pending_count_limit": HANDOFF_GATE_PENDING_COUNT_LIMIT,
        "stall_seconds": AUTOPILOT_HANDOFF_GATE_STALL_SECONDS,
        "stalled": bool(stall.get("stalled")),
        "held_seconds": stall.get("held_seconds"),
    }


def runtime_limited_child_timeout(timeout_seconds, deadline, cleanup_seconds=RUNTIME_CHILD_CLEANUP_SECONDS):
    try:
        timeout = float(timeout_seconds or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    remaining = runtime_seconds_remaining(deadline)
    if remaining is None:
        return max(1, int(timeout or 1))
    try:
        cleanup = max(0.0, float(cleanup_seconds or 0))
    except (TypeError, ValueError):
        cleanup = float(RUNTIME_CHILD_CLEANUP_SECONDS)
    capped = max(1.0, remaining - cleanup)
    return max(1, int(min(timeout or capped, capped)))


def slskd_probe_budget_for_runtime(requested_budget, limited_timeout, deadline):
    """Leave time to turn retained SLSKD results into durable handoffs.

    The probe reports candidates only after its network pass finishes, while
    live auto-grab must still reserve the candidate, enqueue the transfer, and
    persist the waiting record.  A runtime deadline that gives all available
    time to probing can therefore strand a safe result without weakening any
    candidate gate.
    """
    try:
        requested = max(30, int(requested_budget or 0))
    except (TypeError, ValueError):
        requested = 30
    if deadline is None:
        return requested
    try:
        timeout = max(0.0, float(limited_timeout or 0))
    except (TypeError, ValueError):
        timeout = 0.0
    available = timeout - RUNTIME_CHILD_CLEANUP_SECONDS - SLSKD_HANDOFF_RESERVE_SECONDS
    return max(30, min(requested, int(max(30.0, available))))


def budget_retry_seconds(args):
    try:
        retry_seconds = int(getattr(args, "retry_seconds", DEFAULT_RETRY_SECONDS) or DEFAULT_RETRY_SECONDS)
    except (TypeError, ValueError):
        retry_seconds = DEFAULT_RETRY_SECONDS
    return max(MIN_BUDGET_RETRY_SECONDS, min(retry_seconds, 10 * 60))


def inkdrop_retired_queue_ids():
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return set()
    try:
        with inkdrop_state.connect(INKDROP_STATE_DB) as con:
            return {
                str(row["id"])
                for row in con.execute(
                    "select id from queue_items where state in ('superseded_duplicate')"
                )
                if row["id"] not in (None, "")
            }
    except Exception as exc:
        log("inkdrop_retired_queue_ids_failed", error=f"{type(exc).__name__}: {exc}")
        return set()


def inkdrop_terminal_queue_rows():
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {}
    try:
        with inkdrop_state.connect(INKDROP_STATE_DB) as con:
            return {
                str(row["id"]): {
                    "state": str(row["state"] or ""),
                    "last_event": row["last_event"],
                    "updated_at": row["updated_at"],
                }
                for row in con.execute(
                    """
                    select id, state, last_event, updated_at
                    from queue_items
                    where state in ('superseded_duplicate', 'verified')
                    """
                )
                if row["id"] not in (None, "")
            }
    except Exception as exc:
        log("inkdrop_terminal_queue_rows_failed", error=f"{type(exc).__name__}: {exc}")
        return {}


def retire_queue_items_from_inkdrop_state(queue):
    items = queue.get("items") if isinstance(queue, dict) else {}
    if not isinstance(items, dict) or not items:
        return 0
    terminal_rows = inkdrop_terminal_queue_rows()
    if not terminal_rows:
        return 0
    now = time.time()
    changed = 0
    for key, item in items.items():
        if not isinstance(item, dict):
            continue
        identifiers = {str(key), str(item.get("key") or "")}
        terminal = None
        for identifier in identifiers:
            terminal = terminal_rows.get(identifier)
            if terminal:
                break
        if not terminal:
            continue
        target_state = terminal.get("state")
        if target_state not in TERMINAL_QUEUE_STATES:
            continue
        if item.get("state") == target_state:
            continue
        previous_state = item.get("state")
        item["state"] = target_state
        item["current_source"] = None
        item["terminal_state_synced_from_inkdrop_at"] = now
        item["terminal_state_synced_from_inkdrop_at_iso"] = now_iso(now)
        item["terminal_state_synced_previous_state"] = previous_state
        item["last_event"] = terminal.get("last_event") or (
            "Duplicate queue row retired by InkDrop state"
            if target_state == "superseded_duplicate"
            else "Verified by InkDrop state"
        )
        if target_state == "verified":
            item.setdefault("completed_at", terminal.get("updated_at") or now)
            item.setdefault("completed_at_iso", now_iso(float(item.get("completed_at") or now)))
        item["updated_at"] = now
        item["updated_at_iso"] = now_iso(now)
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        changed += 1
    if changed:
        queue.setdefault("history", []).append(
            {
                "ts": now,
                "ts_iso": now_iso(now),
                "event": "inkdrop_state_terminal_queue_rows",
                "count": changed,
            }
        )
    return changed


def inkdrop_provider_config(provider_id):
    if inkdrop_state is None:
        return None
    try:
        return inkdrop_state.provider_config(INKDROP_STATE_DB, provider_id)
    except Exception as exc:
        log("inkdrop_provider_config_failed", provider_id=provider_id, error=f"{type(exc).__name__}: {exc}")
        return None


def inkdrop_app_setting(key):
    if inkdrop_state is None:
        return None
    try:
        return inkdrop_state.app_setting(INKDROP_STATE_DB, key)
    except Exception as exc:
        log("inkdrop_app_setting_failed", key=key, error=f"{type(exc).__name__}: {exc}")
        return None


def latest_inkdrop_provider_health():
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {}
    try:
        with inkdrop_state.connect(INKDROP_STATE_DB) as con:
            return inkdrop_state.latest_provider_health_map(con)
    except Exception as exc:
        log("inkdrop_provider_health_failed", error=f"{type(exc).__name__}: {exc}")
        return {}


def source_health_skip_reason(source, health_map=None):
    source = str(source or "").strip().lower()
    if source not in SOURCE_HEALTH_GATED_SOURCES:
        return None
    health = (health_map or {}).get(source)
    if not isinstance(health, dict):
        return None
    state = str(health.get("state") or "").strip().lower()
    if state not in SOURCE_HEALTH_BLOCKING_STATES:
        return None
    label = str(health.get("label") or state or "health").strip()
    detail = str(health.get("detail") or health.get("message") or "").strip()
    source_label = source_attempt_event(source).replace("checking ", "").replace("searching ", "")
    reason = f"skipped; {source_label} health {label}"
    if detail:
        reason += f": {detail}"
    return reason


def provider_health_skip_status(health):
    state = str((health or {}).get("state") or "").strip().lower()
    if state in {"unavailable", "disabled", "error", "failed"}:
        return "provider_unavailable"
    return "provider_wait"


def record_provider_health_skip_attempt(item, source, reason, health_map=None, now=None):
    if not isinstance(item, dict):
        return False
    source = str(source or "").strip().lower()
    if not source:
        return False
    health = (health_map or {}).get(source)
    health = health if isinstance(health, dict) else {}
    now = now or time.time()
    query = item.get("query") or " ".join(
        str(value or "").strip()
        for value in (item.get("series"), item.get("issue"))
        if str(value or "").strip()
    )
    attempt = {
        "ts": now,
        "ts_iso": now_iso(now),
        "source": source,
        "provider": source,
        "status": provider_health_skip_status(health),
        "reason": reason,
        "kind": "provider_health_skip",
        "query": query,
        "title": query,
        "provider_health_state": health.get("state"),
        "provider_health_label": health.get("label"),
        "provider_health_detail": health.get("detail") or health.get("message"),
    }
    item["last_provider_health_blocked_source"] = source
    item["last_provider_health_blocked_reason"] = reason
    item["last_provider_health_blocked_at"] = now
    item["last_provider_health_blocked_at_iso"] = now_iso(now)
    return append_unique_queue_attempt(item, attempt)


def clear_health_blocked_current_sources(queue, health_map=None):
    items = queue.get("items") if isinstance(queue, dict) else {}
    if not isinstance(items, dict):
        return 0
    health_map = health_map if isinstance(health_map, dict) else latest_inkdrop_provider_health()
    now = time.time()
    changed = 0
    for item in items.values():
        if not isinstance(item, dict):
            continue
        source = str(item.get("current_source") or "").strip().lower()
        reason = source_health_skip_reason(source, health_map)
        if not reason:
            continue
        if item.get("state") in ACTIVE_QUEUE_STATES | TERMINAL_QUEUE_STATES:
            continue
        item["state"] = "queued"
        item["current_source"] = None
        item["provider_health_blocked_source"] = source
        item["provider_health_blocked_at"] = now
        item["provider_health_blocked_at_iso"] = now_iso(now)
        item["last_event"] = f"{reason}; continuing source ladder"
        item.pop("needs_you_reason", None)
        touch_queue_item(item, now)
        changed += 1
    if changed:
        queue.setdefault("history", []).append(
            {
                "ts": now,
                "ts_iso": now_iso(now),
                "event": "provider_health_blocked_sources_cleared",
                "count": changed,
            }
        )
        log("provider_health_blocked_sources_cleared", count=changed)
    return changed


def source_provider_id(source):
    return SOURCE_PROVIDER_IDS.get(str(source or "").strip().lower())


def source_disabled_reason(source):
    source = str(source or "").strip().lower()
    if source in {"", "local"}:
        return None
    return PROVIDER_SOURCE_DISABLED_REASONS.get(source)


def source_enabled(source):
    source = str(source or "").strip().lower()
    if source in {"", "local"}:
        return True
    return bool(PROVIDER_SOURCE_ENABLED.get(source, True)) and not source_disabled_reason(source)


def filter_enabled_source_order(order):
    filtered = []
    seen = set()
    for source in order or []:
        source = str(source or "").strip().lower()
        if source not in VALID_SOURCE_ORDER or source in seen:
            continue
        if not source_enabled(source):
            continue
        filtered.append(source)
        seen.add(source)
    if "local" not in seen:
        filtered.insert(0, "local")
    return filtered or ["local"]


def clear_disabled_current_sources(queue):
    items = queue.get("items") if isinstance(queue, dict) else {}
    if not isinstance(items, dict):
        return 0
    now = time.time()
    changed = 0
    for item in items.values():
        if not isinstance(item, dict):
            continue
        source = str(item.get("current_source") or "").strip().lower()
        reason = source_disabled_reason(source)
        if not reason:
            continue
        if item.get("state") in ACTIVE_QUEUE_STATES | TERMINAL_QUEUE_STATES:
            continue
        item["state"] = "queued"
        item["current_source"] = None
        item["provider_config_blocked_source"] = source
        item["provider_config_blocked_at"] = now
        item["provider_config_blocked_at_iso"] = now_iso(now)
        item["last_event"] = f"{reason}; continuing source ladder"
        item.pop("needs_you_reason", None)
        touch_queue_item(item, now)
        changed += 1
    if changed:
        queue.setdefault("history", []).append(
            {
                "ts": now,
                "ts_iso": now_iso(now),
                "event": "provider_config_blocked_sources_cleared",
                "count": changed,
            }
        )
        log("provider_config_blocked_sources_cleared", count=changed)
    return changed


def normalize_source_order(value):
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    order = []
    seen = set()
    for source in raw:
        source = str(source or "").strip().lower()
        if source not in VALID_SOURCE_ORDER or source in seen:
            continue
        order.append(source)
        seen.add(source)
    for source in DEFAULT_SOURCE_ORDER:
        if source not in seen:
            order.append(source)
    return order or list(DEFAULT_SOURCE_ORDER)


MANGA_SOURCE_POLICY_PUBLISHERS = {
    "comikey",
    "denpa",
    "futabasha",
    "kadokawa",
    "kodansha",
    "kurokawa",
    "seven seas",
    "shogakukan",
    "shueisha",
    "square enix",
    "tokyopop",
    "vertical",
    "viz",
    "yen press",
}


def source_policy_text_key(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def manga_source_policy_publisher_signal(value):
    publisher_key = source_policy_text_key(value)
    if not publisher_key:
        return False
    for candidate in MANGA_SOURCE_POLICY_PUBLISHERS:
        candidate_key = source_policy_text_key(candidate)
        if candidate_key and (publisher_key == candidate_key or candidate_key in publisher_key):
            return True
    return False


def manga_source_policy_eligible(item):
    if not isinstance(item, dict):
        return False
    for key in ("mangadex_id", "mangadexId", "manga_id", "mangadex_chapter_id", "mangadexChapterId"):
        if item.get(key) not in (None, "", [], {}):
            return True
    metadata_provider = source_policy_text_key(
        item.get("metadata_provider")
        or item.get("metadataProvider")
        or item.get("issue_metadata_provider")
        or item.get("issueMetadataProvider")
    )
    if metadata_provider == "mangadex":
        return True
    media_type = source_policy_text_key(item.get("media_type") or item.get("mediaType"))
    if media_type in {"manga", "manga metadata", "manga metadata source"}:
        return True
    publisher = (
        item.get("publisher")
        or item.get("watch_publisher")
        or item.get("watchPublisher")
        or item.get("series_publisher")
    )
    return manga_source_policy_publisher_signal(publisher)


def manga_source_policy_order(item, order):
    order = list(order or [])
    if not manga_source_policy_eligible(item):
        return order, ""
    if not source_enabled("mangadex"):
        return order, ""
    if "mangadex" in order:
        return order, ""
    out = []
    inserted = False
    for source in order:
        out.append(source)
        if source == "local":
            out.append("mangadex")
            inserted = True
    if not inserted:
        out.insert(0, "mangadex")
    return out, "manga_metadata_or_publisher"


def queue_item_base_source_order(item):
    if isinstance(item, dict) and isinstance(item.get("source_order"), list) and item.get("source_order"):
        return filter_enabled_source_order(normalize_source_order(item.get("source_order")))
    return filter_enabled_source_order(SOURCE_ORDER)


def source_order_attempt_key(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    aliases = {
        "soulseek": "slskd",
        "slskd": "slskd",
        "prowlarr": "prowlarr",
        "failed_retry": "prowlarr",
        "failed-download retry": "prowlarr",
        "rss": "rss",
        "comicscodes": "comicscodes",
        "comiccodes": "comicscodes",
        "mangadex": "mangadex",
    }
    if text in aliases:
        return aliases[text]
    for key, alias in aliases.items():
        if key in text:
            return alias
    return text if text in VALID_SOURCE_ORDER else ""


def queue_item_source_attempt_counts(item):
    counts = queue_item_recorded_source_attempt_counts(item)
    if not isinstance(item, dict):
        return counts
    for key in ("last_source_outcome_source", "last_attempt_source", "current_source"):
        source_key = source_order_attempt_key(item.get(key))
        if source_key:
            counts[source_key] += 1
    if slskd_attempted_at(item) and counts["slskd"] <= 0:
        counts["slskd"] = 1
    return counts


def queue_item_recorded_source_attempt_counts(item):
    counts = collections.Counter()
    if not isinstance(item, dict):
        return counts
    raw_counts = item.get("source_attempt_counts")
    if isinstance(raw_counts, dict):
        for source, count in raw_counts.items():
            source_key = source_order_attempt_key(source)
            if not source_key:
                continue
            try:
                counts[source_key] += int(count or 0)
            except (TypeError, ValueError):
                pass
    for attempt in item.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("kind") or "").strip().lower() == "source_runtime_budget_skipped":
            continue
        source_key = source_order_attempt_key(
            attempt.get("source")
            or attempt.get("provider_id")
            or attempt.get("provider")
            or attempt.get("download_client")
        )
        if source_key:
            counts[source_key] += 1
    return counts


def adaptive_slskd_source_order(item, order):
    order = list(order or [])
    if not isinstance(item, dict) or "slskd" not in order:
        return order, ""
    state = str(item.get("state") or "").strip().lower()
    if state in ACTIVE_QUEUE_STATES | TERMINAL_QUEUE_STATES | {"needs_you"}:
        return order, ""
    acquisition_order = [source for source in order if source != "local"]
    try:
        slskd_index = acquisition_order.index("slskd")
    except ValueError:
        return order, ""
    if slskd_index <= 0:
        return order, ""
    prior_sources = [
        source
        for source in acquisition_order[:slskd_index]
        if source in SOURCE_PROVIDER_IDS
    ]
    if not prior_sources:
        return order, ""
    counts = queue_item_source_attempt_counts(item)
    if counts["slskd"] <= 0:
        return order, ""
    if any(counts[source] <= 0 for source in prior_sources):
        return order, ""
    try:
        ladder_attempts = int(item.get("source_ladder_attempt_count") or 0)
    except (TypeError, ValueError):
        ladder_attempts = 0
    if ladder_attempts < ADAPTIVE_SLSKD_MIN_LADDER_ATTEMPTS and counts["slskd"] < ADAPTIVE_SLSKD_MIN_LADDER_ATTEMPTS:
        return order, ""
    promoted = []
    if "local" in order:
        promoted.append("local")
    promoted.append("slskd")
    for source in order:
        if source not in promoted:
            promoted.append(source)
    return promoted, "prior_sources_already_attempted"


def queue_item_source_order(item):
    order = queue_item_base_source_order(item)
    order, _manga_reason = manga_source_policy_order(item, order)
    adapted, _reason = adaptive_slskd_source_order(item, order)
    if "slskd" in adapted and slskd_source_result_reprobe_due(item):
        adapted = [source for source in adapted if source != "slskd"]
        adapted.insert(1 if adapted and adapted[0] == "local" else 0, "slskd")
    return adapted


def apply_queue_item_source_policy(item, now=None):
    if not isinstance(item, dict):
        return filter_enabled_source_order(SOURCE_ORDER)
    base_order = queue_item_base_source_order(item)
    manga_order, manga_reason = manga_source_policy_order(item, base_order)
    if manga_reason and manga_order != base_order:
        if now is None:
            now = time.time()
        item.setdefault("manga_source_order_original", list(base_order))
        item["manga_source_order"] = list(manga_order)
        item["manga_source_order_reason"] = manga_reason
        item.setdefault("manga_source_order_at", now)
        item.setdefault("manga_source_order_at_iso", now_iso(item["manga_source_order_at"]))
    adapted_order, reason = adaptive_slskd_source_order(item, manga_order)
    if reason and adapted_order != manga_order:
        if now is None:
            now = time.time()
        item.setdefault("adaptive_source_order_original", list(manga_order))
        item["adaptive_source_order"] = list(adapted_order)
        item["adaptive_source_order_reason"] = reason
        item.setdefault("adaptive_source_order_at", now)
        item.setdefault("adaptive_source_order_at_iso", now_iso(item["adaptive_source_order_at"]))
    return adapted_order


def queue_item_recovery_steps(item):
    if isinstance(item, dict) and isinstance(item.get("recovery_steps"), list) and item.get("recovery_steps"):
        steps = []
        seen = set()
        for step in item.get("recovery_steps") or []:
            step = str(step or "").strip().lower()
            if step and step not in seen:
                seen.add(step)
                steps.append(step)
        return steps or list(RECOVERY_STEPS)
    return list(RECOVERY_STEPS)


def source_order_for_rows(rows):
    order = []
    seen = set()
    for item in rows or []:
        for source in queue_item_source_order(item):
            if source and source not in seen:
                seen.add(source)
                order.append(source)
    for source in filter_enabled_source_order(SOURCE_ORDER):
        if source not in seen:
            seen.add(source)
            order.append(source)
    return order or filter_enabled_source_order(SOURCE_ORDER)


def apply_automation_app_settings(args):
    global SOURCE_ORDER
    setting = inkdrop_app_setting("automation.source_order") or {}
    source = setting.get("source") or ("runtime" if setting else "fallback")
    SOURCE_ORDER = normalize_source_order(setting.get("value"))
    args.source_order = list(SOURCE_ORDER)
    args.source_order_unfiltered = list(SOURCE_ORDER)
    args.source_order_settings_source = source
    log("automation_source_order_loaded", source=source, source_order=SOURCE_ORDER)


def int_provider_setting(settings, key, default, minimum=None, maximum=None):
    try:
        value = int((settings or {}).get(key))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def list_provider_setting(settings, *keys, default=None):
    settings = settings if isinstance(settings, dict) else {}
    raw = None
    for key in keys:
        if key in settings and settings.get(key) not in (None, "", [], {}):
            raw = settings.get(key)
            break
    if raw in (None, "", [], {}):
        raw = default
    if raw in (None, "", [], {}):
        return []
    values = raw
    if isinstance(values, str):
        values = re.split(r"[,;\s]+", values)
    elif isinstance(values, dict):
        values = values.get("hosts") or values.get("host") or values.get("domains") or values.get("domain") or []
    elif not isinstance(values, (list, tuple, set)):
        values = [values]
    out = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def float_provider_setting(settings, key, default, minimum=None, maximum=None):
    try:
        value = float((settings or {}).get(key))
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def bounded_int_value(value, default, minimum=None, maximum=None):
    try:
        number = int(value if value is not None else default)
    except (TypeError, ValueError):
        number = int(default)
    if minimum is not None:
        number = max(int(minimum), number)
    if maximum is not None:
        number = min(int(maximum), number)
    return number


def bool_provider_setting(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def load_prowlarr_autopilot_settings():
    config = inkdrop_provider_config("prowlarr") or {}
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    return {
        "enabled": bool(config.get("enabled", True)),
        "source": config.get("source") or ("runtime" if config else "fallback"),
        "limit": int_provider_setting(settings, "limit", DEFAULT_PROWLARR_LIMIT, 1, 100),
        "max_queries_per_issue": int_provider_setting(
            settings,
            "max_queries_per_issue",
            DEFAULT_PROWLARR_MAX_QUERIES_PER_ISSUE,
            1,
            20,
        ),
        "timeout_seconds": float_provider_setting(
            settings,
            "timeout_seconds",
            DEFAULT_PROWLARR_TIMEOUT_SECONDS,
            5.0,
            30.0,
        ),
        "command_timeout_seconds": int_provider_setting(
            settings,
            "command_timeout_seconds",
            DEFAULT_PROWLARR_COMMAND_TIMEOUT_SECONDS,
            15,
            300,
        ),
        "search_budget_seconds": int_provider_setting(
            settings,
            "search_budget_seconds",
            DEFAULT_PROWLARR_SEARCH_BUDGET_SECONDS,
            8,
            300,
        ),
        "failed_retry_command_timeout_seconds": int_provider_setting(
            settings,
            "failed_retry_command_timeout_seconds",
            DEFAULT_FAILED_RETRY_COMMAND_TIMEOUT_SECONDS,
            15,
            300,
        ),
        "provider_timeout_window_seconds": int_provider_setting(
            settings,
            "provider_timeout_window_seconds",
            DEFAULT_PROWLARR_PROVIDER_TIMEOUT_WINDOW_SECONDS,
            0,
            24 * 3600,
        ),
        "provider_timeout_threshold": int_provider_setting(
            settings,
            "provider_timeout_threshold",
            DEFAULT_PROWLARR_PROVIDER_TIMEOUT_THRESHOLD,
            0,
            100,
        ),
        "provider_timeout_cooldown_seconds": int_provider_setting(
            settings,
            "provider_timeout_cooldown_seconds",
            DEFAULT_PROWLARR_PROVIDER_TIMEOUT_COOLDOWN_SECONDS,
            0,
            24 * 3600,
        ),
        "provider_fetch_failure_window_seconds": int_provider_setting(
            settings,
            "provider_fetch_failure_window_seconds",
            DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS,
            0,
            24 * 3600,
        ),
        "provider_fetch_failure_threshold": int_provider_setting(
            settings,
            "provider_fetch_failure_threshold",
            DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_THRESHOLD,
            0,
            100,
        ),
        "provider_fetch_failure_cooldown_seconds": int_provider_setting(
            settings,
            "provider_fetch_failure_cooldown_seconds",
            DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS,
            0,
            24 * 3600,
        ),
    }


def apply_prowlarr_provider_defaults(args):
    settings = load_prowlarr_autopilot_settings()
    args.prowlarr_provider_settings_source = settings["source"]
    args.prowlarr_provider_enabled = bool(settings["enabled"])
    if not args.prowlarr_provider_enabled:
        args.skip_prowlarr = True
        args.skip_failed_retry = True
    if args.prowlarr_limit is None:
        args.prowlarr_limit = settings["limit"]
    if args.prowlarr_max_queries_per_issue is None:
        args.prowlarr_max_queries_per_issue = settings["max_queries_per_issue"]
    if args.prowlarr_timeout_seconds is None:
        args.prowlarr_timeout_seconds = settings["timeout_seconds"]
    if args.prowlarr_command_timeout_seconds is None:
        args.prowlarr_command_timeout_seconds = settings["command_timeout_seconds"]
    if args.prowlarr_search_budget_seconds is None:
        args.prowlarr_search_budget_seconds = settings["search_budget_seconds"]
    if args.failed_retry_command_timeout_seconds is None:
        args.failed_retry_command_timeout_seconds = settings["failed_retry_command_timeout_seconds"]
    if getattr(args, "prowlarr_provider_timeout_window_seconds", None) is None:
        args.prowlarr_provider_timeout_window_seconds = settings["provider_timeout_window_seconds"]
    if getattr(args, "prowlarr_provider_timeout_threshold", None) is None:
        args.prowlarr_provider_timeout_threshold = settings["provider_timeout_threshold"]
    if getattr(args, "prowlarr_provider_timeout_cooldown_seconds", None) is None:
        args.prowlarr_provider_timeout_cooldown_seconds = settings["provider_timeout_cooldown_seconds"]
    if getattr(args, "prowlarr_provider_fetch_failure_window_seconds", None) is None:
        args.prowlarr_provider_fetch_failure_window_seconds = settings["provider_fetch_failure_window_seconds"]
    if getattr(args, "prowlarr_provider_fetch_failure_threshold", None) is None:
        args.prowlarr_provider_fetch_failure_threshold = settings["provider_fetch_failure_threshold"]
    if getattr(args, "prowlarr_provider_fetch_failure_cooldown_seconds", None) is None:
        args.prowlarr_provider_fetch_failure_cooldown_seconds = settings["provider_fetch_failure_cooldown_seconds"]
    return settings


def load_slskd_autopilot_settings():
    config = inkdrop_provider_config("slskd") or {}
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    return {
        "enabled": bool(config.get("enabled", True)),
        "source": config.get("source") or ("runtime" if config else "fallback"),
        "max_total": int_provider_setting(settings, "max_total", DEFAULT_SLSKD_MAX_TOTAL, 1, 50),
        "max_per_series": int_provider_setting(settings, "max_per_series", DEFAULT_SLSKD_MAX_PER_SERIES, 1, 20),
        "wait_seconds": int_provider_setting(settings, "wait_seconds", DEFAULT_SLSKD_WAIT_SECONDS, 2, 30),
        "max_queries": int_provider_setting(settings, "max_queries", DEFAULT_SLSKD_MAX_QUERIES, 1, 5),
        "cooldown_hours": float_provider_setting(settings, "cooldown_hours", DEFAULT_SLSKD_COOLDOWN_HOURS, 0.0, 24.0 * 30.0),
        "auto_grab_max": int_provider_setting(settings, "auto_grab_max", DEFAULT_SLSKD_AUTO_GRAB_MAX, 0, 10),
        "probe_budget_seconds": int_provider_setting(
            settings,
            "probe_budget_seconds",
            DEFAULT_SLSKD_PROBE_BUDGET_SECONDS,
            30,
            15 * 60,
        ),
    }


def apply_slskd_provider_defaults(args):
    settings = load_slskd_autopilot_settings()
    args.slskd_provider_settings_source = settings["source"]
    args.slskd_provider_enabled = bool(settings["enabled"])
    if not args.slskd_provider_enabled:
        args.skip_slskd = True
    if args.slskd_max_total is None:
        args.slskd_max_total = settings["max_total"]
    if args.slskd_max_per_series is None:
        args.slskd_max_per_series = settings["max_per_series"]
    if args.slskd_wait_seconds is None:
        args.slskd_wait_seconds = settings["wait_seconds"]
    if args.slskd_max_queries is None:
        args.slskd_max_queries = settings["max_queries"]
    if args.slskd_cooldown_hours is None:
        args.slskd_cooldown_hours = settings["cooldown_hours"]
    if args.slskd_auto_grab_max is None:
        args.slskd_auto_grab_max = settings["auto_grab_max"]
    if args.slskd_probe_budget_seconds is None:
        args.slskd_probe_budget_seconds = settings["probe_budget_seconds"]
    return settings


def direct_discovery_default_command_timeout(provider_id):
    provider_id = str(provider_id or "").strip().lower()
    if provider_id == "comicscodes":
        return DEFAULT_COMICSCODES_COMMAND_TIMEOUT_SECONDS
    return DEFAULT_RSS_COMMAND_TIMEOUT_SECONDS


def load_direct_discovery_provider_settings(provider_id):
    config = inkdrop_provider_config(provider_id) or {}
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    feed_url = str(settings.get("feed_url") or config.get("base_url") or "").strip()
    out = {
        "enabled": bool(config.get("enabled", True)),
        "source": config.get("source") or ("runtime" if config else "fallback"),
        "feed_url": feed_url,
        "source_allowed_hosts": list_provider_setting(
            settings,
            "source_allowed_hosts",
            "allowed_hosts",
            "allowed_source_hosts",
        ),
        "direct_allowed_hosts": list_provider_setting(
            settings,
            "direct_allowed_hosts",
            "direct_allowed_host",
            "allowed_direct_hosts",
        ),
        "allowed_shared_file_hosts": list_provider_setting(
            settings,
            "allowed_shared_file_hosts",
            "shared_file_hosts",
        ),
        "limit": int_provider_setting(settings, "default_limit", DEFAULT_DISCOVERY_LIMIT, 1, 100),
        "max_auto": int_provider_setting(settings, "max_auto", DEFAULT_DISCOVERY_MAX_AUTO, 0, 50),
        "max_per_series": int_provider_setting(
            settings,
            "max_per_series",
            DEFAULT_DISCOVERY_MAX_PER_SERIES,
            0,
            20,
        ),
        "command_timeout_seconds": int_provider_setting(
            settings,
            "command_timeout_seconds",
            direct_discovery_default_command_timeout(provider_id),
            30,
            300,
        ),
    }
    if str(provider_id or "").strip().lower() == "rss":
        out.update(
            {
                "source_worker_http_timeout_seconds": int_provider_setting(
                    settings,
                    "source_worker_http_timeout_seconds",
                    DEFAULT_RSS_SOURCE_WORKER_HTTP_TIMEOUT_SECONDS,
                    5,
                    30,
                ),
                "provider_timeout_window_seconds": int_provider_setting(
                    settings,
                    "provider_timeout_window_seconds",
                    DEFAULT_RSS_PROVIDER_TIMEOUT_WINDOW_SECONDS,
                    0,
                    24 * 3600,
                ),
                "provider_timeout_threshold": int_provider_setting(
                    settings,
                    "provider_timeout_threshold",
                    DEFAULT_RSS_PROVIDER_TIMEOUT_THRESHOLD,
                    0,
                    100,
                ),
                "provider_timeout_cooldown_seconds": int_provider_setting(
                    settings,
                    "provider_timeout_cooldown_seconds",
                    DEFAULT_RSS_PROVIDER_TIMEOUT_COOLDOWN_SECONDS,
                    0,
                    24 * 3600,
                ),
                "provider_fetch_failure_window_seconds": int_provider_setting(
                    settings,
                    "provider_fetch_failure_window_seconds",
                    DEFAULT_RSS_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS,
                    0,
                    24 * 3600,
                ),
                "provider_fetch_failure_threshold": int_provider_setting(
                    settings,
                    "provider_fetch_failure_threshold",
                    DEFAULT_RSS_PROVIDER_FETCH_FAILURE_THRESHOLD,
                    0,
                    100,
                ),
                "provider_fetch_failure_cooldown_seconds": int_provider_setting(
                    settings,
                    "provider_fetch_failure_cooldown_seconds",
                    DEFAULT_RSS_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS,
                    0,
                    24 * 3600,
                ),
            }
        )
    return out


def host_from_url(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text if "://" in text else f"//{text}")
    except Exception:
        return ""
    host = str(parsed.hostname or "").strip().lower()
    return host


def normalized_host_list(values):
    out = []
    seen = set()
    for value in values or []:
        raw_host = str(value or "").strip().lower()
        try:
            host = str(ipaddress.ip_address(raw_host))
        except ValueError:
            host = host_from_url(value)
            if not host:
                host = raw_host.split("/", 1)[0].split(":", 1)[0].strip()
        if not host or host in seen:
            continue
        seen.add(host)
        out.append(host)
    return out


def configured_http_host(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        host = str(parsed.hostname or "").strip().lower()
        parsed.port
    except (TypeError, ValueError):
        return ""
    return host


def rss_shared_file_hosts_to_direct_hosts(values):
    hosts = []
    for value in values or []:
        key = str(value or "").strip().lower()
        if not key:
            continue
        mapped = SOURCE_WORKER_RSS_SHARED_FILE_HOST_DIRECT_HOSTS.get(key)
        if mapped:
            hosts.extend(mapped)
            continue
        host = host_from_url(key) or (key if "." in key else "")
        if host:
            hosts.append(host)
    return normalized_host_list(hosts)


def rss_source_worker_allowed_hosts_from_settings(settings):
    hosts = []
    hosts.extend(settings.get("source_allowed_hosts") or [])
    feed_host = host_from_url(settings.get("feed_url"))
    if feed_host:
        hosts.append(feed_host)
    hosts.extend(SOURCE_WORKER_RSS_ALLOWED_HOST_FALLBACKS)
    return normalized_host_list(hosts)


def rss_source_worker_direct_allowed_hosts_from_settings(settings):
    hosts = []
    hosts.extend(settings.get("direct_allowed_hosts") or [])
    hosts.extend(rss_shared_file_hosts_to_direct_hosts(settings.get("allowed_shared_file_hosts") or []))
    hosts.extend(SOURCE_WORKER_RSS_DIRECT_ALLOWED_HOST_FALLBACKS)
    return normalized_host_list(hosts)


def source_worker_prowlarr_allowed_hosts():
    hosts = []
    hosts.extend(SOURCE_WORKER_PROWLARR_ALLOWED_HOSTS)
    hosts.append(os.environ.get("INKDROP_PROWLARR_URL") or "")
    hosts.append(os.environ.get("INKDROP_PROWLARR_PUBLIC_BASE_URL") or "")
    hosts.extend(str(os.environ.get("INKDROP_PROWLARR_INTERNAL_BASE_URLS") or "").split(","))
    config = inkdrop_provider_config("prowlarr") or {}
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    effective_persisted_url = config.get("base_url") or settings.get("base_url")
    persisted_host = configured_http_host(effective_persisted_url)
    if persisted_host:
        hosts.append(persisted_host)
    return normalized_host_list(hosts)


def apply_direct_discovery_provider_defaults(args):
    rss_settings = load_direct_discovery_provider_settings("rss")
    comicscodes_settings = load_direct_discovery_provider_settings("comicscodes")
    args.rss_provider_settings_source = rss_settings["source"]
    args.comicscodes_provider_settings_source = comicscodes_settings["source"]
    args.rss_provider_enabled = bool(rss_settings["enabled"])
    args.comicscodes_provider_enabled = bool(comicscodes_settings["enabled"])
    if not args.rss_provider_enabled:
        args.skip_rss = True
    if not args.comicscodes_provider_enabled:
        args.skip_comicscodes = True
    args.rss_discovery_limit = args.discovery_limit if args.discovery_limit is not None else rss_settings["limit"]
    args.rss_discovery_max_auto = args.discovery_max_auto if args.discovery_max_auto is not None else rss_settings["max_auto"]
    args.rss_discovery_max_per_series = (
        args.discovery_max_per_series
        if args.discovery_max_per_series is not None
        else rss_settings["max_per_series"]
    )
    if getattr(args, "rss_command_timeout_seconds", None) is None:
        args.rss_command_timeout_seconds = rss_settings["command_timeout_seconds"]
    args.rss_feed_url = rss_settings.get("feed_url") or getattr(args, "rss_feed_url", "")
    if getattr(args, "rss_source_worker_allowed_hosts", None) in (None, "", []):
        args.rss_source_worker_allowed_hosts = rss_source_worker_allowed_hosts_from_settings(rss_settings)
    if getattr(args, "rss_source_worker_direct_allowed_hosts", None) in (None, "", []):
        args.rss_source_worker_direct_allowed_hosts = rss_source_worker_direct_allowed_hosts_from_settings(rss_settings)
    if getattr(args, "rss_source_worker_http_timeout_seconds", None) is None:
        args.rss_source_worker_http_timeout_seconds = rss_settings.get("source_worker_http_timeout_seconds")
    for key in (
        "provider_timeout_window_seconds",
        "provider_timeout_threshold",
        "provider_timeout_cooldown_seconds",
        "provider_fetch_failure_window_seconds",
        "provider_fetch_failure_threshold",
        "provider_fetch_failure_cooldown_seconds",
    ):
        attr = f"rss_{key}"
        if getattr(args, attr, None) is None:
            setattr(args, attr, rss_settings.get(key))
    args.comicscodes_discovery_limit = (
        args.discovery_limit if args.discovery_limit is not None else comicscodes_settings["limit"]
    )
    args.comicscodes_discovery_max_auto = (
        args.discovery_max_auto if args.discovery_max_auto is not None else comicscodes_settings["max_auto"]
    )
    args.comicscodes_discovery_max_per_series = (
        args.discovery_max_per_series
        if args.discovery_max_per_series is not None
        else comicscodes_settings["max_per_series"]
    )
    if getattr(args, "comicscodes_command_timeout_seconds", None) is None:
        args.comicscodes_command_timeout_seconds = comicscodes_settings["command_timeout_seconds"]
    log(
        "direct_discovery_provider_settings_loaded",
        rss={
            "enabled": args.rss_provider_enabled,
            "settings_source": args.rss_provider_settings_source,
            "limit": args.rss_discovery_limit,
            "max_auto": args.rss_discovery_max_auto,
            "max_per_series": args.rss_discovery_max_per_series,
            "command_timeout_seconds": args.rss_command_timeout_seconds,
            "source_worker_http_timeout_seconds": args.rss_source_worker_http_timeout_seconds,
            "source_allowed_hosts": list(getattr(args, "rss_source_worker_allowed_hosts", []) or []),
            "direct_allowed_hosts": list(getattr(args, "rss_source_worker_direct_allowed_hosts", []) or []),
            "provider_timeout_window_seconds": getattr(args, "rss_provider_timeout_window_seconds", None),
            "provider_timeout_threshold": getattr(args, "rss_provider_timeout_threshold", None),
            "provider_timeout_cooldown_seconds": getattr(args, "rss_provider_timeout_cooldown_seconds", None),
            "provider_fetch_failure_window_seconds": getattr(args, "rss_provider_fetch_failure_window_seconds", None),
            "provider_fetch_failure_threshold": getattr(args, "rss_provider_fetch_failure_threshold", None),
            "provider_fetch_failure_cooldown_seconds": getattr(args, "rss_provider_fetch_failure_cooldown_seconds", None),
        },
        comicscodes={
            "enabled": args.comicscodes_provider_enabled,
            "settings_source": args.comicscodes_provider_settings_source,
            "limit": args.comicscodes_discovery_limit,
            "max_auto": args.comicscodes_discovery_max_auto,
            "max_per_series": args.comicscodes_discovery_max_per_series,
            "command_timeout_seconds": args.comicscodes_command_timeout_seconds,
        },
        cli_override=bool(
            args.discovery_limit is not None
            or args.discovery_max_auto is not None
            or args.discovery_max_per_series is not None
            or getattr(args, "rss_command_timeout_seconds", None) != rss_settings["command_timeout_seconds"]
            or getattr(args, "comicscodes_command_timeout_seconds", None) != comicscodes_settings["command_timeout_seconds"]
        ),
    )
    return {"rss": rss_settings, "comicscodes": comicscodes_settings}


def load_mangadex_autopilot_settings():
    config = inkdrop_provider_config("mangadex") or {}
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    return {
        "enabled": bool(config.get("enabled", True)),
        "source": config.get("source") or ("runtime" if config else "fallback"),
        "data_saver": bool_provider_setting(settings.get("data_saver"), False),
        "command_timeout_seconds": int_provider_setting(
            settings,
            "command_timeout_seconds",
            DEFAULT_MANGADEX_COMMAND_TIMEOUT_SECONDS,
            30,
            1800,
        ),
        "verify_timeout_seconds": int_provider_setting(
            settings,
            "verify_timeout_seconds",
            DEFAULT_MANGADEX_VERIFY_TIMEOUT_SECONDS,
            0,
            300,
        ),
    }


def apply_mangadex_provider_defaults(args):
    settings = load_mangadex_autopilot_settings()
    args.mangadex_provider_settings_source = settings["source"]
    args.mangadex_provider_enabled = bool(settings["enabled"])
    if not args.mangadex_provider_enabled:
        args.skip_mangadex = True
    elif settings.get("data_saver") and not getattr(args, "mangadex_data_saver", False):
        args.mangadex_data_saver = True
    if getattr(args, "mangadex_command_timeout_seconds", None) is None:
        args.mangadex_command_timeout_seconds = settings["command_timeout_seconds"]
    if getattr(args, "mangadex_verify_timeout_seconds", None) is None:
        args.mangadex_verify_timeout_seconds = settings["verify_timeout_seconds"]
    return settings


def source_provider_disabled_reason(source, provider_id, args):
    source = str(source or "").strip().lower()
    provider_id = str(provider_id or source or "").strip().lower()
    config = inkdrop_provider_config(provider_id) or {}
    if config and not bool(config.get("enabled", True)):
        return f"skipped; {provider_id} provider is disabled in InkDrop Settings"
    if source == "prowlarr" and getattr(args, "skip_prowlarr", False):
        return "skipped; Prowlarr provider is disabled"
    if source == "rss" and getattr(args, "skip_rss", False):
        return "skipped; RSS provider is disabled"
    if source == "comicscodes" and getattr(args, "skip_comicscodes", False):
        return "skipped; ComicsCodes provider is disabled"
    if source == "slskd" and getattr(args, "skip_slskd", False):
        return "skipped; SLSKD provider is disabled"
    if source == "mangadex" and getattr(args, "skip_mangadex", False):
        return "skipped; MangaDex provider is disabled"
    return None


def apply_provider_source_policy(args):
    global SOURCE_ORDER, PROVIDER_SOURCE_ENABLED, PROVIDER_SOURCE_DISABLED_REASONS
    enabled = {}
    disabled = {}
    for source, provider_id in SOURCE_PROVIDER_IDS.items():
        reason = source_provider_disabled_reason(source, provider_id, args)
        enabled[source] = not bool(reason)
        if reason:
            disabled[source] = reason
    PROVIDER_SOURCE_ENABLED = enabled
    PROVIDER_SOURCE_DISABLED_REASONS = disabled
    raw_order = normalize_source_order(getattr(args, "source_order_unfiltered", None) or SOURCE_ORDER)
    SOURCE_ORDER = filter_enabled_source_order(raw_order)
    args.source_order_unfiltered = raw_order
    args.source_order = list(SOURCE_ORDER)
    args.provider_source_enabled = dict(enabled)
    args.provider_source_disabled_reasons = dict(disabled)
    log(
        "provider_source_policy_loaded",
        source_order=SOURCE_ORDER,
        source_order_unfiltered=raw_order,
        enabled=enabled,
        disabled=disabled,
    )
    return {"enabled": enabled, "disabled": disabled, "source_order": SOURCE_ORDER, "source_order_unfiltered": raw_order}


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def normalize_issue(value):
    text = str(value or "").strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return normalize(text)
    number = match.group(0)
    if "." in number:
        return number.rstrip("0").rstrip(".")
    return str(int(number)).zfill(4)


def issue_number_keys(value):
    text = str(value or "").strip()
    out = {normalize_issue(text)}
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
    out.discard("")
    return out


def issue_number_keys_in_text(value):
    out = set()
    for raw in re.findall(r"(?<!\d)\d{1,4}(?:\.\d+)?(?!\d)", str(value or "")):
        out |= issue_number_keys(raw)
    return out


def kavita_placeholder_issue_number(value):
    text = str(value or "").strip()
    return text in {"-100000", "-100000.0"}


def kavita_record_issue_numbers(record, fallback_text=""):
    row_numbers = set()
    saw_placeholder = False
    for field in ("number", "chapter_range"):
        value = record.get(field) if isinstance(record, dict) else None
        if kavita_placeholder_issue_number(value):
            saw_placeholder = True
            continue
        row_numbers |= issue_number_keys(value)
    fallback_numbers = issue_number_keys_in_text(fallback_text)
    if fallback_numbers and (saw_placeholder or not row_numbers):
        row_numbers |= fallback_numbers
    return row_numbers


def issue_number_value(value):
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def queue_key(series, issue, identity=None):
    raw = f"{normalize(series)}|{normalize_issue(issue)}"
    if identity:
        raw = f"{raw}|{normalize(identity)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def queue_identity(
    *,
    watch_id=None,
    kapowarr_id=None,
    comicvine_id=None,
    source=None,
    owner=None,
    ownership=None,
    metadata_provider=None,
):
    if comicvine_id not in (None, ""):
        return f"comicvine:{comicvine_id}"
    if kapowarr_id not in (None, ""):
        return f"kapowarr:{kapowarr_id}"
    if watch_id not in (None, ""):
        return f"watch:{watch_id}"
    return ""


def equivalent_queue_identities(*, watch_id=None, kapowarr_id=None, comicvine_id=None, source=None, owner=None, ownership=None, metadata_provider=None):
    values = [
        queue_identity(
            watch_id=watch_id,
            kapowarr_id=kapowarr_id,
            comicvine_id=comicvine_id,
            source=source,
            owner=owner,
            ownership=ownership,
            metadata_provider=metadata_provider,
        )
    ]
    if kapowarr_id not in (None, ""):
        values.append(f"kapowarr:{kapowarr_id}")
    if comicvine_id not in (None, ""):
        values.append(f"comicvine:{comicvine_id}")
    if watch_id not in (None, ""):
        values.append(f"watch:{watch_id}")
    out = []
    seen = set()
    for value in values:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def queue_key_for_watch(series, issue, watch):
    return queue_key(
        series,
        issue,
        queue_identity(
            watch_id=watch.get("id"),
            kapowarr_id=watch.get("kapowarrId"),
            comicvine_id=watch.get("comicvineId"),
            source=watch.get("source"),
            owner=watch.get("owner"),
            ownership=watch.get("ownership"),
            metadata_provider=watch.get("metadataProvider"),
        ),
    )


def legacy_queue_key(series, issue):
    return queue_key(series, issue)


def item_legacy_key(item):
    return item.get("legacy_key") or legacy_queue_key(item.get("series"), item.get("issue"))


def row_text(row):
    parts = []
    for field in (
        "query",
        "title",
        "filename",
        "path",
        "source",
        "dest",
        "matched_local_path",
        "matched_series",
        "candidate",
    ):
        value = row.get(field) if isinstance(row, dict) else None
        if value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts)


def text_has_year(text, year):
    if year in (None, ""):
        return False
    return bool(re.search(rf"(?<!\d){re.escape(str(year))}(?!\d)", str(text or "")))


def row_item_match_score(row, item):
    text = row_text(row)
    normalized_text = normalize(text)
    score = 0
    row_query = normalize(row.get("query") if isinstance(row, dict) else "")
    item_query = normalize(item.get("query") or "")
    if row_query and item_query and row_query == item_query:
        score += 40
    elif item_query and item_query in normalized_text:
        score += 20
    year = item.get("watch_year") or item.get("year")
    if text_has_year(text, year):
        score += 25
    publisher = normalize(item.get("publisher") or item.get("watch_publisher") or "")
    if publisher and publisher in normalized_text:
        score += 10
    return score


def build_row_queue_target_index(queue, *, deadline=None, budget_state=None):
    """Build one bounded lookup for evidence-to-queue matching.

    Annotation sources can contain thousands of historical rows. Re-scanning
    the complete queue for each row made startup quadratic before any series
    was processed. The index only narrows the existing fallback candidate
    set; ``row_queue_targets`` still applies every identity, activity,
    ambiguity, and score check below.
    """
    indexed = {"all": collections.defaultdict(list), "active": collections.defaultdict(list)}
    items = (queue or {}).get("items") or {}
    if not isinstance(items, dict):
        return indexed
    for key, item in items.items():
        if deadline is not None and time.time() >= deadline:
            if isinstance(budget_state, dict):
                budget_state["queue_target_index_deadline_reached"] = True
            break
        if not isinstance(item, dict):
            continue
        series_key = normalize(item.get("series") or "")
        if not series_key:
            continue
        for issue_key in issue_number_keys(item.get("issue")):
            lookup_key = (series_key, issue_key)
            indexed["all"][lookup_key].append((key, item))
            if item.get("present_in_watch", True):
                indexed["active"][lookup_key].append((key, item))
    return indexed


def row_queue_targets(queue, row, *, include_inactive=False, target_index=None):
    if not isinstance(row, dict):
        return []
    series = row.get("series") or row.get("matched_series") or row.get("title") or row.get("query")
    issue = row.get("issue") if row.get("issue") is not None else row.get("issue_number")
    if not series or issue is None:
        return []
    wanted_series = normalize(series)
    wanted_issues = issue_number_keys(issue)
    identity = queue_identity(
        watch_id=row.get("watch_id") or row.get("watchId"),
        kapowarr_id=row.get("kapowarr_id") or row.get("kapowarrId") or row.get("volume_id") or row.get("volumeId"),
        comicvine_id=row.get("comicvine_id") or row.get("comicvineId"),
        source=row.get("series_source") or row.get("source"),
        owner=row.get("owner"),
        ownership=row.get("ownership"),
        metadata_provider=row.get("metadata_provider"),
    )
    identities = equivalent_queue_identities(
        watch_id=row.get("watch_id") or row.get("watchId"),
        kapowarr_id=row.get("kapowarr_id") or row.get("kapowarrId") or row.get("volume_id") or row.get("volumeId"),
        comicvine_id=row.get("comicvine_id") or row.get("comicvineId"),
        source=row.get("series_source") or row.get("source"),
        owner=row.get("owner"),
        ownership=row.get("ownership"),
        metadata_provider=row.get("metadata_provider"),
    )
    direct_keys = [queue_key(series, issue, value) for value in identities]
    direct_keys.append(legacy_queue_key(series, issue))
    items = queue.get("items", {}) or {}
    seen = set()
    targets = []
    for key in direct_keys:
        item = items.get(key)
        if item and key not in seen:
            if include_inactive or item.get("present_in_watch", True):
                targets.append((key, item))
                seen.add(key)
    if targets and identity:
        return targets
    if targets and len(targets) == 1:
        return targets
    if target_index is not None:
        candidate_rows = []
        candidate_seen = set()
        index_name = "all" if include_inactive else "active"
        lookup = target_index.get(index_name, {}) if isinstance(target_index, dict) else {}
        for issue_key in wanted_issues:
            for key, item in lookup.get((wanted_series, issue_key), []):
                if key not in candidate_seen:
                    candidate_rows.append((key, item))
                    candidate_seen.add(key)
    else:
        candidate_rows = items.items()
    for key, item in candidate_rows:
        if key in seen or not isinstance(item, dict):
            continue
        if not include_inactive and not item.get("present_in_watch", True):
            continue
        if normalize(item.get("series") or "") != wanted_series:
            continue
        if not (issue_number_keys(item.get("issue")) & wanted_issues):
            continue
        if identity and item.get("queue_identity") not in identities:
            continue
        targets.append((key, item))
        seen.add(key)
    if len(targets) <= 1 or identity:
        return targets
    scored = [(row_item_match_score(row, item), key, item) for key, item in targets]
    best = max(score for score, _, _ in scored)
    if best <= 0:
        return []
    winners = [(key, item) for score, key, item in scored if score == best]
    return winners if len(winners) == 1 else []


def command_env(env=None):
    proc_env = None
    if env:
        proc_env = dict(os.environ)
        proc_env.update({str(key): str(value) for key, value in env.items()})
    return proc_env


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


def terminate_active_child_processes(sig=signal.SIGTERM):
    for proc in list(ACTIVE_CHILD_PROCS):
        try:
            if proc.poll() is None:
                signal_process_group(proc, sig)
        except Exception:
            continue


def handle_shutdown_signal(signum, _frame):
    terminate_active_child_processes(signal.SIGTERM)
    raise SystemExit(128 + int(signum))


for _shutdown_signal in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_shutdown_signal, handle_shutdown_signal)
    except Exception:
        pass


def run_process(cmd, timeout=300, env=None):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=command_env(env),
        start_new_session=(os.name == "posix"),
    )
    ACTIVE_CHILD_PROCS.add(proc)
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
        stdout = f"{stdout}{extra_stdout or ''}"
        stderr = f"{stderr}{extra_stderr or ''}"
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    finally:
        ACTIVE_CHILD_PROCS.discard(proc)
    return proc.returncode, stdout, stderr


def run_process_with_progress(cmd, timeout=300, env=None, progress=None, progress_interval=15):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=command_env(env),
        start_new_session=(os.name == "posix"),
    )
    ACTIVE_CHILD_PROCS.add(proc)
    started_at = time.time()
    next_progress_at = started_at
    try:
        while True:
            try:
                returncode = proc.wait(timeout=max(1, min(int(progress_interval or 15), 30)))
                stdout, stderr = proc.communicate(timeout=5)
                return returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                if time.time() >= next_progress_at:
                    if progress:
                        try:
                            progress(proc=proc, elapsed_seconds=max(0, time.time() - started_at))
                        except Exception as exc:
                            log("child_progress_callback_failed", error=f"{type(exc).__name__}: {exc}")
                    next_progress_at = time.time() + max(5, min(int(progress_interval or 15), 60))
                if timeout and time.time() - started_at >= timeout:
                    signal_process_group(proc, signal.SIGTERM)
                    try:
                        stdout, stderr = proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        signal_process_group(proc, signal.SIGKILL)
                        try:
                            stdout, stderr = proc.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            stdout, stderr = "", "\nprocess group did not exit after SIGKILL"
                    raise subprocess.TimeoutExpired(cmd, timeout, output=stdout or "", stderr=stderr or "")
    finally:
        ACTIVE_CHILD_PROCS.discard(proc)


def parse_command_json(returncode, stdout, stderr):
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if returncode != 0:
        raise RuntimeError(stderr or stdout or f"command failed: {returncode}")
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"raw_output": stdout}


def command_timeout_tail(value, limit=2000):
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[-limit:]


def prowlarr_command_timeout_payload(mode, series, args, exc):
    timeout_seconds = getattr(args, "prowlarr_command_timeout_seconds", None)
    if mode == "retry_failed":
        timeout_seconds = getattr(args, "failed_retry_command_timeout_seconds", timeout_seconds)
    try:
        timeout_seconds = int(timeout_seconds or 0)
    except (TypeError, ValueError):
        timeout_seconds = 0
    reason = "failed_retry_command_timeout" if mode == "retry_failed" else "prowlarr_command_timeout"
    return {
        "ok": True,
        "mode": mode,
        "dry_run": bool(getattr(args, "dry_run", False)),
        "series": series,
        "series_count": 1 if series else 0,
        "missing_candidates": 1,
        "attempted_total": 0,
        "actions": [],
        "review": [],
        "skipped": [{"reason": reason}],
        "reason": reason,
        "command_timed_out": True,
        "timed_out": True,
        "timeout_seconds": timeout_seconds,
        "search_budget_exhausted": True,
        "budget_skipped_count": 1,
        "budget_skipped_samples": [{"series": series}] if series else [],
        "stdout_tail": command_timeout_tail(getattr(exc, "output", "")),
        "stderr_tail": command_timeout_tail(getattr(exc, "stderr", "")),
        "prowlarr_provider": {
            "enabled": bool(getattr(args, "prowlarr_provider_enabled", not getattr(args, "skip_prowlarr", False))),
            "settings_source": getattr(args, "prowlarr_provider_settings_source", "fallback"),
            "limit": getattr(args, "prowlarr_limit", None),
            "max_queries_per_issue": getattr(args, "prowlarr_max_queries_per_issue", None),
            "timeout_seconds": getattr(args, "prowlarr_timeout_seconds", None),
            "command_timeout_seconds": getattr(args, "prowlarr_command_timeout_seconds", None),
            "search_budget_seconds": getattr(args, "prowlarr_search_budget_seconds", None),
            "failed_retry_command_timeout_seconds": getattr(args, "failed_retry_command_timeout_seconds", None),
            "provider_timeout_window_seconds": getattr(args, "prowlarr_provider_timeout_window_seconds", None),
            "provider_timeout_threshold": getattr(args, "prowlarr_provider_timeout_threshold", None),
            "provider_timeout_cooldown_seconds": getattr(args, "prowlarr_provider_timeout_cooldown_seconds", None),
            "provider_fetch_failure_window_seconds": getattr(args, "prowlarr_provider_fetch_failure_window_seconds", None),
            "provider_fetch_failure_threshold": getattr(args, "prowlarr_provider_fetch_failure_threshold", None),
            "provider_fetch_failure_cooldown_seconds": getattr(args, "prowlarr_provider_fetch_failure_cooldown_seconds", None),
        },
    }


def mangadex_configured_command_timeout_seconds(args):
    value = getattr(args, "mangadex_command_timeout_seconds", None)
    try:
        timeout = int(DEFAULT_MANGADEX_COMMAND_TIMEOUT_SECONDS if value is None else value)
    except (TypeError, ValueError):
        timeout = DEFAULT_MANGADEX_COMMAND_TIMEOUT_SECONDS
    return max(30, min(timeout, 1800))


def mangadex_configured_verify_timeout_seconds(args):
    value = getattr(args, "mangadex_verify_timeout_seconds", None)
    try:
        timeout = int(DEFAULT_MANGADEX_VERIFY_TIMEOUT_SECONDS if value is None else value)
    except (TypeError, ValueError):
        timeout = DEFAULT_MANGADEX_VERIFY_TIMEOUT_SECONDS
    return max(0, min(timeout, 300))


def mangadex_command_timeout(args, deadline=None):
    timeout = mangadex_configured_command_timeout_seconds(args)
    return runtime_limited_child_timeout(timeout, deadline)


def mangadex_command_timeout_payload(series, args, exc):
    timeout_seconds = mangadex_configured_command_timeout_seconds(args)
    return {
        "ok": True,
        "source": "mangadex",
        "dry_run": bool(getattr(args, "dry_run", False)),
        "series": series,
        "rows_considered": 1 if series else 0,
        "actions": [],
        "review": [],
        "skipped": [{"reason": "mangadex_command_timeout"}],
        "errors": [{"error": "mangadex_command_timeout", "series": series}] if series else [],
        "reason": "mangadex_command_timeout",
        "command_timed_out": True,
        "timed_out": True,
        "timeout_seconds": timeout_seconds,
        "stdout_tail": command_timeout_tail(getattr(exc, "output", "")),
        "stderr_tail": command_timeout_tail(getattr(exc, "stderr", "")),
        "mangadex_provider": {
            "enabled": bool(getattr(args, "mangadex_provider_enabled", not getattr(args, "skip_mangadex", False))),
            "settings_source": getattr(args, "mangadex_provider_settings_source", "fallback"),
            "data_saver": bool(getattr(args, "mangadex_data_saver", False)),
            "max_total": getattr(args, "mangadex_max_total", None),
            "max_per_series": getattr(args, "mangadex_max_per_series", None),
            "command_timeout_seconds": timeout_seconds,
            "verify_timeout_seconds": mangadex_configured_verify_timeout_seconds(args),
        },
    }


def run_command(cmd, timeout=300, env=None):
    returncode, stdout, stderr = run_process(cmd, timeout=timeout, env=env)
    return parse_command_json(returncode, stdout, stderr)


def run_locked_command(cmd, lock_path, *, timeout=300, wait_seconds=60, busy_source="worker", env=None):
    locked_cmd = [
        "/usr/bin/flock",
        "-E",
        "75",
        "-w",
        str(wait_seconds),
        str(lock_path),
        *cmd,
    ]
    returncode, stdout, stderr = run_process(locked_cmd, timeout=timeout, env=env)
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if returncode == 75:
        return {
            "ok": True,
            "skipped_busy": True,
            "reason": f"{busy_source} is already running; autopilot will retry on the next pass.",
        }
    return parse_command_json(returncode, stdout, stderr)


def run_locked_provider_command(
    cmd,
    lock_path,
    *,
    source,
    series,
    provider_observer=None,
    timeout=300,
    wait_seconds=60,
    busy_source="worker",
    env=None,
):
    with held_source_worker_lock(lock_path, wait_seconds=wait_seconds) as acquired:
        if not acquired:
            return {
                "ok": True,
                "skipped_busy": True,
                "reason": f"{busy_source} is already running; autopilot will retry on the next pass.",
            }
        return run_observed_provider_call(
            source,
            series,
            provider_observer,
            lambda: run_command(cmd, timeout=timeout, env=env),
        )


def post_json(path, payload=None, timeout=120):
    if not inkdrop_runtime_config.worker_http_callback_requested():
        return inkdrop_internal_jobs.run_autopilot_web_job(path, payload or {})
    response = requests.post(
        WEB_BASE_URL.rstrip("/") + path,
        json=payload or {},
        headers=inkdrop_runtime_config.worker_auth_headers(required=True),
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json() if response.text else {}


def backfill_slskd_download_started_at(queue):
    items = queue.get("items") if isinstance(queue, dict) else {}
    if not isinstance(items, dict):
        return 0
    state = read_json(SLSKD_AUTO_GRAB_STATE_FILE, {}) or {}
    attempts = state.get("last_attempts") if isinstance(state, dict) else {}
    if not isinstance(attempts, dict):
        return 0
    changed = 0
    backfilled_at = time.time()
    for item in items.values():
        if not isinstance(item, dict):
            continue
        if item.get("state") != "downloading" or item.get("download_started_at"):
            continue
        review_id = str(item.get("last_slskd_waiting_review_id") or item.get("review_id") or "").strip()
        if not review_id:
            continue
        attempt = attempts.get(review_id)
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("status") or "") not in {"started_waiting", "already_downloading"}:
            continue
        item_filename = str(item.get("last_slskd_candidate") or "").strip()
        attempt_filename = str(attempt.get("filename") or "").strip()
        if item_filename and attempt_filename and item_filename != attempt_filename:
            continue
        started_at = numeric_timestamp(attempt.get("ts")) or backfilled_at
        item["download_started_at"] = started_at
        item["download_started_at_iso"] = now_iso(started_at)
        item["last_download_started_at"] = started_at
        item["last_download_started_at_iso"] = now_iso(started_at)
        item["download_started_backfilled_at"] = backfilled_at
        item["download_started_backfilled_at_iso"] = now_iso(backfilled_at)
        item["updated_at"] = backfilled_at
        item["updated_at_iso"] = now_iso(backfilled_at)
        changed += 1
    if changed:
        queue.setdefault("history", []).append(
            {
                "ts": backfilled_at,
                "ts_iso": now_iso(backfilled_at),
                "event": "slskd_download_started_backfilled",
                "count": changed,
            }
        )
    return changed


def normalize_stale_slskd_autopick_errors(queue):
    items = queue.get("items") if isinstance(queue, dict) else {}
    if not isinstance(items, dict):
        return 0
    now = time.time()
    changed = 0
    for item in items.values():
        if not isinstance(item, dict):
            continue
        if item.get("state") not in {"queued", "searching"}:
            continue
        status = str(item.get("last_slskd_autopick_status") or "")
        if status not in {"error", "download_api_error", "download_preflight_api_error", "transient_error"}:
            continue
        if status == "transient_error" and item.get("retry_after"):
            continue
        if item.get("download_started_at"):
            continue
        if status != "transient_error" and item.get("retry_after"):
            continue
        item["state"] = "queued"
        item["current_source"] = None
        item["last_slskd_autopick_status"] = "transient_error"
        item["last_slskd_autopick_error"] = (
            item.get("last_slskd_autopick_error")
            or "SLSKD auto-grab failed before retry metadata was recorded"
        )
        item["last_slskd_transient_error_at"] = now
        item["last_slskd_transient_error_at_iso"] = now_iso(now)
        item["last_event"] = "SLSKD download API hiccup; retry scheduled"
        retry_after = now + SLSKD_TRANSIENT_RETRY_SECONDS
        item["retry_after"] = retry_after
        item["retry_after_iso"] = now_iso(retry_after)
        item["updated_at"] = now
        item["updated_at_iso"] = now_iso(now)
        item.pop("needs_you_reason", None)
        changed += 1
    if changed:
        queue.setdefault("history", []).append(
            {
                "ts": now,
                "ts_iso": now_iso(now),
                "event": "stale_slskd_autopick_errors_normalized",
                "count": changed,
            }
        )
    return changed


def source_started_at(item):
    started = numeric_timestamp(item.get("last_source_started_at"))
    if started > 0:
        return started
    latest = 0
    for attempt in item.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("kind") or "").strip().lower() != "source_started":
            continue
        latest = max(latest, numeric_timestamp(attempt.get("ts") or attempt.get("started_at")))
    return latest


def source_started_source(item):
    source = source_order_attempt_key(item.get("current_source") or item.get("last_source_started_source") or "")
    if source:
        return source
    best_ts = 0
    best_source = ""
    for attempt in item.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("kind") or "").strip().lower() != "source_started":
            continue
        ts = numeric_timestamp(attempt.get("ts") or attempt.get("started_at"))
        if ts >= best_ts:
            best_ts = ts
            best_source = source_order_attempt_key(attempt.get("source") or attempt.get("provider_id") or "")
    return best_source


def source_started_timeout_attempt_recorded(item, source, started_at):
    source = str(source_order_attempt_key(source) or source or "").strip().lower()
    started_at = numeric_timestamp(started_at)
    if not source or started_at <= 0:
        return False
    for attempt in item.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("kind") or "").strip().lower() != "source_started_timeout":
            continue
        attempt_source = str(
            source_order_attempt_key(attempt.get("source") or attempt.get("provider_id") or "")
            or attempt.get("source")
            or attempt.get("provider_id")
            or ""
        ).strip().lower()
        if attempt_source != source:
            continue
        if abs(numeric_timestamp(attempt.get("started_at")) - started_at) < 1:
            return True
    return False


def clear_source_started_marker(item, source=None):
    if not isinstance(item, dict):
        return False
    requested = source_order_attempt_key(source) if source else ""
    current = source_order_attempt_key(item.get("last_source_started_source") or item.get("current_source") or "")
    if requested and current and current != requested:
        return False
    changed = False
    for key in (
        "last_source_started_source",
        "last_source_started_at",
        "last_source_started_at_iso",
        "last_source_started_note",
    ):
        if key in item:
            item.pop(key, None)
            changed = True
    return changed


def source_started_stale_seconds(source, default_seconds=STALE_SEARCH_SOURCE_MARKER_SECONDS):
    try:
        default_seconds = max(60, int(default_seconds or STALE_SEARCH_SOURCE_MARKER_SECONDS))
    except (TypeError, ValueError):
        default_seconds = STALE_SEARCH_SOURCE_MARKER_SECONDS
    source = source_order_attempt_key(source)
    provider_thresholds = {
        "prowlarr": STALE_PROWLARR_SOURCE_MARKER_SECONDS,
        "rss": STALE_RSS_SOURCE_MARKER_SECONDS,
        "comicscodes": STALE_COMICSCODES_SOURCE_MARKER_SECONDS,
        "mangadex": STALE_MANGADEX_SOURCE_MARKER_SECONDS,
    }
    if source in provider_thresholds:
        return min(default_seconds, provider_thresholds[source])
    return default_seconds


def normalize_stale_source_started_attempts(queue, now=None, stale_seconds=STALE_SEARCH_SOURCE_MARKER_SECONDS):
    items = queue.get("items") if isinstance(queue, dict) else {}
    if not isinstance(items, dict):
        return 0
    now = now or time.time()
    try:
        stale_seconds = max(60, int(stale_seconds or STALE_SEARCH_SOURCE_MARKER_SECONDS))
    except (TypeError, ValueError):
        stale_seconds = STALE_SEARCH_SOURCE_MARKER_SECONDS
    changed = 0
    for item in items.values():
        if not isinstance(item, dict):
            continue
        if item.get("state") not in {"searching", "queued"}:
            continue
        started_at = source_started_at(item)
        if started_at <= 0:
            continue
        source = source_started_source(item)
        if not source:
            continue
        threshold_seconds = source_started_stale_seconds(source, stale_seconds)
        if started_at > now - threshold_seconds:
            continue
        if source_started_timeout_attempt_recorded(item, source, started_at):
            if item.get("current_source") == source:
                item["current_source"] = None
            item.pop("last_source_started_source", None)
            item.pop("last_source_started_at", None)
            item.pop("last_source_started_at_iso", None)
            item.pop("last_source_started_note", None)
            continue
        if item.get("download_started_at") or item.get("last_download_started_at"):
            continue
        if item.get("last_slskd_waiting_review_id") or item.get("last_slskd_transfer_id"):
            continue
        label = public_source_name(source) or source
        reason = f"{label} source started but did not report a result before the stale timeout"
        item["state"] = "queued"
        item["current_source"] = None
        item["last_source_error_source"] = source
        item["last_source_error"] = reason
        item["last_source_error_at"] = now
        item["last_source_error_at_iso"] = now_iso(now)
        item["source_error_retry_at"] = now
        item["source_error_retry_at_iso"] = now_iso(now)
        item["stale_source_started_source"] = source
        item["stale_source_started_at"] = started_at
        item["stale_source_started_at_iso"] = now_iso(started_at)
        item["stale_source_started_normalized_at"] = now
        item["stale_source_started_normalized_at_iso"] = now_iso(now)
        item["last_event"] = f"{label} source timed out; automatic retry scheduled"
        retry_delay = SLSKD_TRANSIENT_RETRY_SECONDS if source == "slskd" else DEFAULT_RETRY_SECONDS
        schedule_retry_after(item, now, retry_delay)
        item.pop("needs_you_reason", None)
        title = " ".join(
            str(value or "").strip()
            for value in (item.get("series"), item.get("issue"))
            if str(value or "").strip()
        )
        append_unique_queue_attempt(
            item,
            {
                "ts": now,
                "ts_iso": now_iso(now),
                "source": source,
                "provider": label,
                "provider_id": source,
                "status": "timeout",
                "lifecycle_phase": "retry_later",
                "display_phase": "retry_later",
                "outcome": "retry_later",
                "retry_eligible": True,
                "reason": reason,
                "failure_reason": reason,
                "kind": "source_started_timeout",
                "title": title,
                "query": item.get("query") or title,
                "started_at": started_at,
                "stale_seconds": int(max(0, now - started_at)),
                "threshold_seconds": threshold_seconds,
            },
        )
        touch_queue_item(item, now)
        changed += 1
    if changed:
        queue.setdefault("history", []).append(
            {
                "ts": now,
                "ts_iso": now_iso(now),
                "event": "stale_source_started_attempts_normalized",
                "count": changed,
            }
        )
    return changed


def load_queue():
    data = read_json(QUEUE_FILE, {}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema_version", QUEUE_SCHEMA_VERSION)
    data.setdefault("created_at", time.time())
    data.setdefault("items", {})
    data.setdefault("history", [])
    backfill_slskd_download_started_at(data)
    normalize_stale_slskd_autopick_errors(data)
    data["_stale_source_started_normalized_count"] = normalize_stale_source_started_attempts(data)
    data["_retired_from_inkdrop_state_count"] = retire_queue_items_from_inkdrop_state(data)
    data["_provider_health_blocked_sources_cleared_count"] = clear_health_blocked_current_sources(data)
    return data


def save_queue(
    queue,
    *,
    sync_state=True,
    ack_deferred=True,
    merge_disk=True,
    retire_terminal=True,
    sync_reason="save_queue",
    sync_kwargs=None,
):
    if merge_disk:
        disk = read_json(QUEUE_FILE, {}) or {}
        disk_items = disk.get("items") if isinstance(disk, dict) else {}
    else:
        disk_items = None
    if isinstance(disk_items, dict):
        items = queue.setdefault("items", {})
        merged = 0
        for key, item in disk_items.items():
            migrated_elsewhere = any(
                isinstance(existing, dict) and existing.get("migrated_from_legacy_key") == key
                for existing in items.values()
            )
            if migrated_elsewhere:
                continue
            if key not in items and isinstance(item, dict):
                items[key] = item
                merged += 1
        if merged:
            now = time.time()
            queue.setdefault("history", []).append(
                {
                    "ts": now,
                    "ts_iso": now_iso(now),
                    "event": "external_queue_merge",
                    "created": merged,
                }
            )
    if retire_terminal:
        retire_queue_items_from_inkdrop_state(queue)
    queue["schema_version"] = QUEUE_SCHEMA_VERSION
    queue["updated_at"] = time.time()
    queue["updated_at_iso"] = now_iso(queue["updated_at"])
    history = queue.setdefault("history", [])
    if len(history) > 300:
        queue["history"] = history[-300:]
    write_json(QUEUE_FILE, queue)
    if ack_deferred:
        ack_deferred_manual_source_queue_syncs()
    sync_result = None
    if sync_state:
        sync_result = sync_inkdrop_queue_state(reason=sync_reason, **(sync_kwargs or {}))
        if isinstance(sync_result, dict) and sync_result.get("ok") is False:
            mark_inkdrop_state_sync_pending(queue, sync_result, sync_reason)
            write_json(QUEUE_FILE, queue)
            if inkdrop_state_sync_failed_due_to_lock(sync_result):
                log("inkdrop_state_queue_sync_deferred", reason=sync_reason, result_reason=sync_result.get("reason"))
        elif clear_inkdrop_state_sync_pending(queue):
            write_json(QUEUE_FILE, queue)
    return sync_result


def save_queue_progress_snapshot(queue):
    save_queue(queue, sync_state=False, ack_deferred=False, merge_disk=False, retire_terminal=False)


def save_startup_queue_snapshot(queue):
    """Persist startup normalization without replaying unrelated deferred work."""
    return save_queue(
        queue,
        sync_state=False,
        ack_deferred=False,
        merge_disk=False,
        retire_terminal=False,
    )


def persist_startup_queue_normalization(queue, *, stale_source_started_count=0):
    """Save startup repairs without letting DB maintenance starve providers."""
    if stale_source_started_count:
        mark_inkdrop_state_sync_pending(
            queue,
            {
                "ok": False,
                "reason": "startup_provider_budget_protected",
                "locked": False,
            },
            "stale_source_started_normalized",
        )
    return save_startup_queue_snapshot(queue)


def sync_watched_state(
    sync=False,
    sync_metadata_adapter=False,
    sync_timeout_seconds=DEFAULT_STARTUP_SYNC_TIMEOUT_SECONDS,
    sync_metadata_adapter_timeout_seconds=DEFAULT_METADATA_ADAPTER_SYNC_TIMEOUT_SECONDS,
):
    sync_result = None
    if sync:
        sync_result = {}
        try:
            state = post_json(
                "/api/inkdrop-state/sync",
                {"mode": "queue"},
                timeout=sync_timeout_seconds,
            ).get("state") or {}
            sync_result["inkdrop_state"] = {
                "ok": bool(state.get("ok")),
                "series": state.get("series"),
                "wanted_items": state.get("wanted_items"),
                "queue_items": state.get("queue_items"),
                "active_queue_items": state.get("active_queue_items"),
                "synced_at_iso": state.get("synced_at_iso") or state.get("last_sync_at_iso"),
                "scope": state.get("last_sync_scope"),
                "timeout_seconds": sync_timeout_seconds,
            }
        except Exception as exc:
            sync_result["inkdrop_state_error"] = f"{type(exc).__name__}: {exc}"
            sync_result["inkdrop_state_timeout_seconds"] = sync_timeout_seconds
            log(
                "sync_failed",
                sync_kind="inkdrop_state",
                timeout_seconds=sync_timeout_seconds,
                error=f"{type(exc).__name__}: {exc}",
            )
    # sync_metadata_adapter is accepted but ignored -- Kapowarr is retired
    # software with no live endpoint to sync against.
    return sync_result


def load_watches(
    sync=False,
    sync_metadata_adapter=False,
    sync_timeout_seconds=DEFAULT_STARTUP_SYNC_TIMEOUT_SECONDS,
    sync_metadata_adapter_timeout_seconds=DEFAULT_METADATA_ADAPTER_SYNC_TIMEOUT_SECONDS,
):
    sync_result = sync_watched_state(
        sync=sync,
        sync_metadata_adapter=sync_metadata_adapter,
        sync_timeout_seconds=sync_timeout_seconds,
        sync_metadata_adapter_timeout_seconds=sync_metadata_adapter_timeout_seconds,
    )
    data = read_json(COMIC_SERIES_FILE, {"watches": []}) or {"watches": []}
    if not isinstance(data, dict):
        data = {"watches": []}
    data.setdefault("watches", [])
    return data, sync_result


def current_missing_from_watches(watches):
    current = {}
    queue_truth_from_watch = inkdrop_state.queue_row_source_of_truth if inkdrop_state is not None else None
    for watch in watches:
        if not watch.get("enabled", True):
            continue
        if watch.get("source") != "kapowarr" and not watch.get("comicvineId"):
            continue
        series = str(watch.get("name") or "").strip()
        if not series:
            continue
        for issue in watch.get("missingIssues") or []:
            status = str(issue.get("status") or "missing").lower()
            if status == "suppressed":
                continue
            issue_number = str(issue.get("issueNumber") or "").strip()
            if not issue_number:
                continue
            legacy_key = legacy_queue_key(series, issue_number)
            identity = queue_identity(
                watch_id=watch.get("id"),
                kapowarr_id=watch.get("kapowarrId"),
                comicvine_id=watch.get("comicvineId"),
                source=watch.get("source"),
                owner=watch.get("owner"),
                ownership=watch.get("ownership"),
                metadata_provider=watch.get("metadataProvider"),
            )
            source_truth = queue_truth_from_watch(watch) if callable(queue_truth_from_watch) else {}
            identities = equivalent_queue_identities(
                watch_id=watch.get("id"),
                kapowarr_id=watch.get("kapowarrId"),
                comicvine_id=watch.get("comicvineId"),
                source=watch.get("source"),
                owner=watch.get("owner"),
                ownership=watch.get("ownership"),
                metadata_provider=watch.get("metadataProvider"),
            )
            key = queue_key_for_watch(series, issue_number, watch)
            alternate_keys = [legacy_key]
            for alternate_identity in identities:
                alternate_key = queue_key(series, issue_number, alternate_identity)
                if alternate_key not in alternate_keys:
                    alternate_keys.append(alternate_key)
            current[key] = {
                "key": key,
                "legacy_key": legacy_key,
                "alternate_keys": alternate_keys,
                "queue_identity": identity,
                "series": series,
                "issue": issue_number,
                "issue_title": issue.get("title") or "",
                "issue_id": issue.get("id"),
                "kapowarr_issue_id": issue.get("kapowarrIssueId"),
                "watch_id": watch.get("id"),
                "kapowarr_id": watch.get("kapowarrId"),
                "comicvine_id": watch.get("comicvineId"),
                "series_source": watch.get("source"),
                "owner": watch.get("owner"),
                "ownership": watch.get("ownership"),
                "metadata_provider": watch.get("metadataProvider"),
                "watch_year": watch.get("year"),
                "watch_publisher": watch.get("publisher"),
                "query": issue.get("searchQuery") or f"{series} {issue_number}".strip(),
                "watch_auto_grab": watch_auto_grab_enabled(watch),
                "watch_status": status,
                "state_source": "metadata_adapter",
                "source_of_truth": source_truth.get("source_of_truth"),
                "source_of_truth_confidence": source_truth.get("source_of_truth_confidence"),
                "source_of_truth_evidence": source_truth.get("source_of_truth_evidence"),
            }
    return current


def json_dict(value):
    if isinstance(value, dict):
        return dict(value)
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def current_missing_from_inkdrop_state():
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {}
    current = {}
    con = inkdrop_db.open_connection(
        INKDROP_STATE_DB,
        readonly=True,
        timeout_seconds=30,
        busy_timeout_ms=30000,
        operation="autopilot_current_missing",
    )
    try:
        rows = con.execute(
            """
            with active_queue as (
                select
                    q.id as queue_id,
                    q.wanted_id,
                    q.state as queue_state,
                    q.query,
                    q.source_order_json,
                    q.recovery_steps_json,
                    q.raw_json as queue_raw_json,
                    q.created_at as queue_created_at,
                    q.updated_at as queue_updated_at,
                    w.status as wanted_status,
                    w.raw_json as wanted_raw_json,
                    s.title as series,
                    s.year as watch_year,
                    s.publisher as watch_publisher,
                    s.source as series_source,
                    s.metadata_provider,
                    s.metadata_id,
                    s.kapowarr_id,
                    s.auto_grab,
                    i.id as issue_row_id,
                    i.issue_number,
                    i.title as issue_title,
                    i.metadata_provider as issue_metadata_provider,
                    i.metadata_id as issue_metadata_id,
                    i.kapowarr_issue_id,
                    i.raw_json as issue_raw_json
                from queue_items q
                join series s on s.id = q.series_id
                left join issues i on i.id = q.issue_id
                left join wanted_items w on w.id = q.wanted_id
                where q.active = 1
                  and q.state not in ('verified', 'superseded_duplicate')
                  and coalesce(s.monitored, 1) = 1
                  and coalesce(w.status, 'wanted') not in ('satisfied', 'ignored', 'suppressed', 'superseded_duplicate')
            ),
            attempt_counts as (
                select
                    sa.queue_id,
                    sum(case when lower(coalesce(nullif(sa.provider_id, ''), nullif(sa.source, ''), nullif(sa.provider, ''), '')) = 'prowlarr' then 1 else 0 end) as attempts_prowlarr,
                    sum(case when lower(coalesce(nullif(sa.provider_id, ''), nullif(sa.source, ''), nullif(sa.provider, ''), '')) = 'rss' then 1 else 0 end) as attempts_rss,
                    sum(case when lower(coalesce(nullif(sa.provider_id, ''), nullif(sa.source, ''), nullif(sa.provider, ''), '')) = 'comicscodes' then 1 else 0 end) as attempts_comicscodes,
                    sum(case when lower(coalesce(nullif(sa.provider_id, ''), nullif(sa.source, ''), nullif(sa.provider, ''), '')) = 'slskd' then 1 else 0 end) as attempts_slskd,
                    sum(case when lower(coalesce(nullif(sa.provider_id, ''), nullif(sa.source, ''), nullif(sa.provider, ''), '')) = 'mangadex' then 1 else 0 end) as attempts_mangadex
                from source_attempts sa
                join active_queue aq on aq.queue_id = sa.queue_id
                group by sa.queue_id
            )
            select
                aq.queue_id,
                aq.wanted_id,
                aq.queue_state,
                aq.query,
                aq.source_order_json,
                aq.recovery_steps_json,
                aq.queue_raw_json,
                aq.queue_created_at,
                aq.queue_updated_at,
                aq.wanted_status,
                aq.wanted_raw_json,
                aq.series,
                aq.watch_year,
                aq.watch_publisher,
                aq.series_source,
                aq.metadata_provider,
                aq.metadata_id,
                aq.kapowarr_id,
                aq.auto_grab,
                aq.issue_row_id,
                aq.issue_number,
                aq.issue_title,
                aq.issue_metadata_provider,
                aq.issue_metadata_id,
                aq.kapowarr_issue_id,
                coalesce(sac.attempts_prowlarr, 0) as attempts_prowlarr,
                coalesce(sac.attempts_rss, 0) as attempts_rss,
                coalesce(sac.attempts_comicscodes, 0) as attempts_comicscodes,
                coalesce(sac.attempts_slskd, 0) as attempts_slskd,
                coalesce(sac.attempts_mangadex, 0) as attempts_mangadex,
                aq.issue_raw_json
            from active_queue aq
            left join attempt_counts sac on sac.queue_id = aq.queue_id
            order by aq.queue_updated_at desc
            """
        ).fetchall()
    finally:
        con.close()
    for row in rows:
        raw = json_dict(row["queue_raw_json"])
        issue_raw = json_dict(row["issue_raw_json"])
        wanted_raw = json_dict(row["wanted_raw_json"])
        source_truth = {
            "source_of_truth": raw.get("source_of_truth"),
            "source_of_truth_confidence": raw.get("source_of_truth_confidence"),
            "source_of_truth_evidence": raw.get("source_of_truth_evidence"),
        }
        series = str(row["series"] or raw.get("series") or "").strip()
        issue_number = str(row["issue_number"] or raw.get("issue") or raw.get("issue_number") or "").strip()
        if not series or not issue_number:
            continue
        watch_id = raw.get("watch_id")
        kapowarr_id = row["kapowarr_id"] if row["kapowarr_id"] not in (None, "") else raw.get("kapowarr_id")
        comicvine_id = row["metadata_id"] if row["metadata_provider"] == "comicvine" else raw.get("comicvine_id")
        metadata_provider = str(row["metadata_provider"] or raw.get("metadata_provider") or "").strip().lower()
        native_provider = bool(metadata_provider and metadata_provider not in {"kapowarr", "watch", "manual"})
        owner = raw.get("owner") or ("inkdrop" if native_provider else None)
        ownership = raw.get("ownership") or ("native" if native_provider else None)
        identity = raw.get("queue_identity") or queue_identity(
            watch_id=watch_id,
            kapowarr_id=kapowarr_id,
            comicvine_id=comicvine_id,
            source=row["series_source"],
            owner=owner,
            ownership=ownership,
            metadata_provider=metadata_provider,
        )
        if all(v in (None, "") for v in source_truth.values()) and inkdrop_state is not None:
            inferred_truth = inkdrop_state.queue_row_source_of_truth({
                "owner": owner,
                "ownership": ownership,
                "source": row["series_source"],
                "metadataProvider": metadata_provider,
                "kapowarrId": kapowarr_id,
                "kapowarr_id": kapowarr_id,
            })
            if isinstance(inferred_truth, dict):
                source_truth.update({k: v for k, v in inferred_truth.items() if v not in (None, "")})
        source_order = json_dict({"value": row["source_order_json"]}).get("value")
        if not isinstance(source_order, list):
            try:
                source_order = json.loads(row["source_order_json"] or "[]")
            except (TypeError, ValueError):
                source_order = []
        if not isinstance(source_order, list) or not source_order:
            source_order = raw.get("source_order") if isinstance(raw.get("source_order"), list) else []
        recovery_steps = []
        try:
            recovery_steps = json.loads(row["recovery_steps_json"] or "[]")
        except (TypeError, ValueError):
            recovery_steps = []
        if not isinstance(recovery_steps, list) or not recovery_steps:
            recovery_steps = raw.get("recovery_steps") if isinstance(raw.get("recovery_steps"), list) else []
        chapter_payload = {}
        for source_payload in (issue_raw, wanted_raw, raw):
            nested = source_payload.get("raw") if isinstance(source_payload.get("raw"), dict) else {}
            if nested:
                chapter_payload.update(nested)
            chapter_payload.update(source_payload)
        query = (
            row["query"]
            or chapter_payload.get("searchQuery")
            or raw.get("query")
            or f"{series} {issue_number}".strip()
        )
        key = str(row["queue_id"] or "").strip() or queue_key(series, issue_number, identity)
        legacy_key = str(raw.get("legacy_key") or "").strip() or legacy_queue_key(series, issue_number)
        alternate_keys = []
        for candidate in raw.get("alternate_keys") or []:
            candidate = str(candidate or "").strip()
            if candidate and candidate not in alternate_keys:
                alternate_keys.append(candidate)
        for candidate in (key, legacy_key):
            if candidate and candidate not in alternate_keys:
                alternate_keys.append(candidate)
        for alternate_identity in equivalent_queue_identities(
            watch_id=watch_id,
            kapowarr_id=kapowarr_id,
            comicvine_id=comicvine_id,
            source=row["series_source"],
            owner=owner,
            ownership=ownership,
            metadata_provider=row["metadata_provider"],
        ):
            alternate_key = queue_key(series, issue_number, alternate_identity)
            if alternate_key not in alternate_keys:
                alternate_keys.append(alternate_key)
        item_payload = {
            "key": key,
            "legacy_key": legacy_key,
            "alternate_keys": alternate_keys,
            "queue_identity": identity,
            "series": series,
            "issue": issue_number,
            "issue_title": row["issue_title"] or raw.get("issue_title") or "",
            "issue_id": raw.get("issue_id") or row["issue_row_id"],
            "kapowarr_issue_id": row["kapowarr_issue_id"] or raw.get("kapowarr_issue_id"),
            "watch_id": watch_id,
            "kapowarr_id": kapowarr_id,
            "comicvine_id": comicvine_id,
            "series_source": row["series_source"],
            "owner": owner,
            "ownership": ownership,
            "metadata_provider": metadata_provider,
            "metadata_id": row["metadata_id"] or raw.get("metadata_id"),
            "issue_metadata_provider": row["issue_metadata_provider"] or raw.get("issue_metadata_provider"),
            "issue_metadata_id": row["issue_metadata_id"] or raw.get("issue_metadata_id"),
            "mangadex_id": row["metadata_id"] if metadata_provider == "mangadex" else raw.get("mangadex_id") or raw.get("mangadexId"),
            "mangadex_chapter_id": row["issue_metadata_id"] if str(row["issue_metadata_provider"] or "").lower() == "mangadex" else raw.get("mangadex_chapter_id") or raw.get("chapterId") or raw.get("chapter_id"),
            "volume": chapter_payload.get("volume") or raw.get("volume"),
            "chapter": chapter_payload.get("chapter") or raw.get("chapter") or issue_number,
            "translatedLanguage": chapter_payload.get("translatedLanguage") or raw.get("translatedLanguage"),
            "unitType": chapter_payload.get("unitType") or raw.get("unitType"),
            "manga_unit_model": chapter_payload.get("manga_unit_model") or raw.get("manga_unit_model"),
            "pages": chapter_payload.get("pages") or raw.get("pages"),
            "watch_year": row["watch_year"],
            "watch_publisher": row["watch_publisher"],
            "query": query,
            "searchQuery": chapter_payload.get("searchQuery") or raw.get("searchQuery") or query,
            "queue_created_at": numeric_timestamp(row["queue_created_at"]) or numeric_timestamp(raw.get("created_at")),
            "queue_updated_at": numeric_timestamp(row["queue_updated_at"]) or numeric_timestamp(raw.get("updated_at")),
            "source_attempt_counts": {
                "prowlarr": int(row["attempts_prowlarr"] or 0),
                "rss": int(row["attempts_rss"] or 0),
                "comicscodes": int(row["attempts_comicscodes"] or 0),
                "slskd": int(row["attempts_slskd"] or 0),
                "mangadex": int(row["attempts_mangadex"] or 0),
            },
            "watch_auto_grab": bool(raw.get("watch_auto_grab", row["auto_grab"])),
            "watch_status": str(row["wanted_status"] or raw.get("watch_status") or "missing").lower(),
            "source_order": normalize_source_order(source_order) if source_order else list(SOURCE_ORDER),
            "recovery_steps": queue_item_recovery_steps({"recovery_steps": recovery_steps}) if recovery_steps else list(RECOVERY_STEPS),
            "state_source": "inkdrop_state",
            "source_of_truth": source_truth.get("source_of_truth"),
            "source_of_truth_confidence": source_truth.get("source_of_truth_confidence"),
            "source_of_truth_evidence": source_truth.get("source_of_truth_evidence"),
        }
        item_payload["source_order"] = apply_queue_item_source_policy(item_payload)
        current[key] = item_payload
    return current


def current_item_aliases(item):
    aliases = []
    if not isinstance(item, dict):
        return aliases
    for field in ("key", "legacy_key"):
        value = str(item.get(field) or "").strip()
        if value and value not in aliases:
            aliases.append(value)
    for value in item.get("alternate_keys") or []:
        value = str(value or "").strip()
        if value and value not in aliases:
            aliases.append(value)
    return aliases


def add_current_aliases(index, key, item):
    for alias in current_item_aliases(item):
        index.setdefault(alias, key)


def equivalent_current_key(index, item):
    for alias in current_item_aliases(item):
        if alias in index:
            return index[alias]
    return None


def merge_adapter_context(primary, adapter):
    primary = primary or {}
    merged = dict(primary)
    adapter = adapter or {}
    for field in (
        "legacy_key",
        "watch_id",
        "kapowarr_id",
        "kapowarr_issue_id",
        "comicvine_id",
        "series_source",
        "owner",
        "ownership",
        "metadata_provider",
        "watch_year",
        "watch_publisher",
        "watch_status",
        "source_of_truth",
        "source_of_truth_confidence",
        "source_of_truth_evidence",
    ):
        if merged.get(field) in (None, "") and adapter.get(field) not in (None, ""):
            merged[field] = adapter.get(field)
    alternate_keys = []
    for source in (merged, adapter):
        for alias in current_item_aliases(source):
            if alias and alias not in alternate_keys:
                alternate_keys.append(alias)
    if alternate_keys:
        merged["alternate_keys"] = alternate_keys
    adapter_key = str(adapter.get("key") or "").strip()
    if adapter_key and adapter_key != str(merged.get("key") or "").strip():
        merged["metadata_adapter_key"] = adapter_key
    merged["metadata_adapter_state_source"] = adapter.get("state_source") or "metadata_adapter"
    merged["state_source"] = primary.get("state_source") or "inkdrop_state"
    return merged


def current_missing_from_active_sources(watches):
    watch_current = current_missing_from_watches(watches)
    db_current = current_missing_from_inkdrop_state()
    current = {}
    alias_index = {}
    for key, item in db_current.items():
        current[key] = dict(item)
        add_current_aliases(alias_index, key, current[key])

    watch_fallback = 0
    watch_shadowed = 0
    db_shadowed = set()
    for key, item in watch_current.items():
        matching_key = equivalent_current_key(alias_index, item)
        if matching_key:
            current[matching_key] = merge_adapter_context(current.get(matching_key) or {}, item)
            add_current_aliases(alias_index, matching_key, current[matching_key])
            watch_shadowed += 1
            db_shadowed.add(matching_key)
            continue
        current[key] = item
        add_current_aliases(alias_index, key, item)
        watch_fallback += 1
    db_available = bool(inkdrop_state is not None and INKDROP_STATE_DB.exists())
    return current, {
        "source_of_truth": "inkdrop_state" if db_current or db_available else "metadata_adapter",
        "adapter_role": "fallback" if db_current or db_available else "primary_fallback",
        "watch_rows": len(watch_current),
        "inkdrop_state_rows": len(db_current),
        "inkdrop_state_primary_rows": len(db_current),
        "inkdrop_state_only_rows": max(0, len(db_current) - len(db_shadowed)),
        "watch_shadowed_rows": watch_shadowed,
        "adapter_fallback_rows": watch_fallback,
        "watch_fallback_rows": watch_fallback,
        "merged_rows": len(current),
    }


def read_manual_review_items(limit=800):
    rows = []
    if not MANUAL_REVIEW_FILE.exists():
        return rows
    try:
        with MANUAL_REVIEW_FILE.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return rows
    return rows[-limit:]


def read_waiting_records():
    actions = read_json(MANUAL_REVIEW_ACTIONS_FILE, {}) or {}
    waiting = actions.get("manual_source_waiting") if isinstance(actions, dict) else {}
    if not isinstance(waiting, dict):
        return {}
    return {
        str(key): value
        for key, value in waiting.items()
        if key and isinstance(value, dict)
    }


def row_ts(row):
    if not isinstance(row, dict):
        return 0.0
    best = 0.0
    for key in (
        "ts",
        "started_at",
        "requested_at",
        "last_action_at",
        "last_attempt_at",
        "checked_at",
        "staged_scan_at",
        "updated_at",
        "generated_at",
    ):
        try:
            value = float(row.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            best = max(best, value)
    return best


def slskd_row_checked_at(row):
    if not isinstance(row, dict):
        return 0.0
    best = 0.0
    for key in ("checked_at", "staged_scan_at", "ts", "updated_at", "generated_at"):
        try:
            value = float(row.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            best = max(best, value)
    return best


def stale_slskd_detected_probe_row(row, now):
    if not isinstance(row, dict):
        return False
    try:
        detected_count = int(row.get("detected_count") or 0)
    except (TypeError, ValueError):
        detected_count = 0
    if detected_count <= 0:
        return False
    detected_files = [file for file in (row.get("detected_files") or []) if isinstance(file, dict)]
    mtimes = []
    for file in detected_files:
        try:
            mtime = float(file.get("mtime") or 0)
        except (TypeError, ValueError):
            mtime = 0
        if mtime > 0:
            mtimes.append(mtime)
    if mtimes and max(mtimes) < now - STALE_SLSKD_DETECTED_FILE_SECONDS:
        return True
    checked_at = slskd_row_checked_at(row)
    if checked_at <= 0:
        return False
    return checked_at < now - STALE_SLSKD_IMPORT_SIGNAL_SECONDS


def read_waiting_review_ids():
    return set(read_waiting_records())


def manual_source_resolved_destination_paths(row):
    if not isinstance(row, dict):
        return []
    paths = []

    def add_path(value):
        if value:
            paths.append(str(value))

    for field in ("dest", "destination", "last_import_dest"):
        add_path(row.get(field))
    destinations = row.get("destinations") if isinstance(row.get("destinations"), list) else []
    for path in destinations:
        add_path(path)
    verification = row.get("verification") if isinstance(row.get("verification"), dict) else {}
    for checked in verification.get("checked") or []:
        if not isinstance(checked, dict):
            continue
        add_path(checked.get("dest"))
    out = []
    seen = set()
    for path in paths:
        key = normalize_kavita_path(path)
        if key and key not in seen:
            seen.add(key)
            out.append(path)
    return out


def manual_source_resolved_has_existing_destination(row):
    paths = manual_source_resolved_destination_paths(row)
    if not paths:
        return True
    for path in paths:
        host_path = host_path_for_kavita_path(path) or path
        try:
            if Path(str(host_path)).exists():
                return True
        except OSError:
            continue
    return False


def read_manual_source_resolved_records(queue=None, *, target_index=None, deadline=None):
    actions = read_json(MANUAL_REVIEW_ACTIONS_FILE, {}) or {}
    resolved = actions.get("manual_source_resolved") if isinstance(actions, dict) else []
    if not isinstance(resolved, list):
        return {}, {}
    retracted = actions.get("manual_source_retracted_resolved") if isinstance(actions, dict) else []
    # Older action documents and interrupted rehearsal fixtures may contain an
    # explicit JSON null here.  Treat it like an empty collection; a missing
    # retraction list must never stop the entire Automatic Search worker.
    if not isinstance(retracted, list):
        retracted = []
    retracted_ids = {
        str(row.get("review_id") or "")
        for row in retracted
        if isinstance(row, dict) and row.get("review_id")
    }
    by_key = {}
    by_review_id = {}
    for row in resolved:
        if deadline is not None and time.time() >= deadline:
            break
        if not isinstance(row, dict):
            continue
        review_id = str(row.get("review_id") or "")
        if review_id and review_id in retracted_ids:
            continue
        if not manual_source_resolved_has_existing_destination(row):
            continue
        series = row.get("series")
        issue = row.get("issue")
        if series and issue is not None:
            targets = row_queue_targets(
                queue,
                row,
                include_inactive=True,
                target_index=target_index,
            ) if queue else []
            if queue:
                verified_targets = [
                    (key, item)
                    for key, item in targets
                    if isinstance(item, dict) and resolved_record_verified_for_item(item, row)
                ]
                if not verified_targets:
                    continue
                target_keys = [key for key, _ in verified_targets]
            else:
                target_keys = [legacy_queue_key(series, issue)]
            for key in target_keys:
                existing = by_key.get(key)
                if not existing or float(row.get("ts") or 0) >= float(existing.get("ts") or 0):
                    by_key[key] = row
        if review_id:
            existing = by_review_id.get(review_id)
            if not existing or float(row.get("ts") or 0) >= float(existing.get("ts") or 0):
                by_review_id[review_id] = row
    return by_key, by_review_id


def read_manual_source_bad_candidate_records(queue=None, *, target_index=None, deadline=None):
    actions = read_json(MANUAL_REVIEW_ACTIONS_FILE, {}) or {}
    bad = actions.get("manual_source_bad_candidates") if isinstance(actions, dict) else {}
    if not isinstance(bad, dict):
        return {}, {}
    by_key = {}
    by_review_id = {}
    for review_id, rows in bad.items():
        if deadline is not None and time.time() >= deadline:
            break
        if not isinstance(rows, list):
            continue
        for row in rows:
            if deadline is not None and time.time() >= deadline:
                break
            if not isinstance(row, dict):
                continue
            enriched = dict(row)
            enriched.setdefault("review_id", str(review_id))
            targets = row_queue_targets(
                queue,
                enriched,
                include_inactive=True,
                target_index=target_index,
            ) if queue else []
            target_keys = [key for key, _ in targets]
            if not target_keys and enriched.get("series") and enriched.get("issue") is not None:
                target_keys = [legacy_queue_key(enriched.get("series"), enriched.get("issue"))]
            for key in target_keys:
                existing = by_key.get(key)
                if not existing or float(enriched.get("ts") or 0) >= float(existing.get("ts") or 0):
                    by_key[key] = enriched
            if review_id:
                existing = by_review_id.get(str(review_id))
                if not existing or float(enriched.get("ts") or 0) >= float(existing.get("ts") or 0):
                    by_review_id[str(review_id)] = enriched
    return by_key, by_review_id


def manual_source_record_queue_keys(queue, row, *, target_index=None):
    if not isinstance(row, dict):
        return []
    items = (queue or {}).get("items", {}) if isinstance(queue, dict) else {}
    keys = []
    direct = str(row.get("autopilot_queue_key") or row.get("queue_key") or "").strip()
    if direct and (not items or direct in items):
        keys.append(direct)
    if queue:
        keys.extend(
            key for key, _ in row_queue_targets(
                queue,
                row,
                include_inactive=True,
                target_index=target_index,
            )
        )
    if row.get("series") and row.get("issue") is not None:
        keys.append(legacy_queue_key(row.get("series"), row.get("issue")))
    out = []
    seen = set()
    for key in keys:
        if key and key not in seen:
            out.append(key)
            seen.add(key)
    return out


def read_manual_source_retry_pending_records(queue=None, *, target_index=None, deadline=None):
    actions = read_json(MANUAL_REVIEW_ACTIONS_FILE, {}) or {}
    pending = actions.get("manual_source_retry_pending") if isinstance(actions, dict) else {}
    if not isinstance(pending, dict):
        return {}, {}
    by_key = {}
    by_review_id = {}
    for review_id, record in pending.items():
        if deadline is not None and time.time() >= deadline:
            break
        if not isinstance(record, dict):
            continue
        enriched = dict(record)
        enriched.setdefault("review_id", str(review_id))
        bad_candidate = enriched.get("bad_candidate") if isinstance(enriched.get("bad_candidate"), dict) else {}
        if bad_candidate:
            enriched["bad_candidate"] = dict(bad_candidate)
            enriched["bad_candidate"].setdefault("review_id", str(review_id))
        target_keys = manual_source_record_queue_keys(queue, enriched, target_index=target_index)
        if bad_candidate:
            target_keys.extend(
                manual_source_record_queue_keys(
                    queue,
                    enriched["bad_candidate"],
                    target_index=target_index,
                )
            )
        if not target_keys and enriched.get("series") and enriched.get("issue") is not None:
            target_keys = [legacy_queue_key(enriched.get("series"), enriched.get("issue"))]
        for key in dict.fromkeys(key for key in target_keys if key):
            existing = by_key.get(key)
            if not existing or row_ts(enriched) >= row_ts(existing):
                by_key[key] = enriched
        if review_id:
            existing = by_review_id.get(str(review_id))
            if not existing or row_ts(enriched) >= row_ts(existing):
                by_review_id[str(review_id)] = enriched
    return by_key, by_review_id


def apply_failed_slskd_candidate(item, bad_candidate, now, *, retry_started=False):
    if not isinstance(item, dict) or not isinstance(bad_candidate, dict):
        return False
    if item.get("state") == "verified":
        return False
    try:
        failure_ts = float(bad_candidate.get("ts") or 0)
    except (TypeError, ValueError):
        failure_ts = 0
    try:
        previous_ts = float(item.get("last_failed_candidate_at") or 0)
    except (TypeError, ValueError):
        previous_ts = 0
    if failure_ts and previous_ts and failure_ts < previous_ts:
        return False

    item["last_failed_candidate_reason"] = bad_candidate.get("reason") or "candidate_failed"
    item["last_failed_candidate_kind"] = bad_candidate.get("failure_kind")
    item["last_failed_candidate_label"] = bad_candidate.get("failure_label")
    item["last_failed_candidate_detail"] = bad_candidate.get("detail")
    item["last_failed_candidate_filename"] = bad_candidate.get("filename")
    item["last_failed_candidate_user"] = bad_candidate.get("username")
    item["last_failed_candidate_score"] = bad_candidate.get("candidate_score")
    item["last_failed_candidate_at"] = failure_ts or now
    item["last_failed_candidate_at_iso"] = bad_candidate.get("ts_iso") or now_iso(failure_ts or now)
    item["last_failed_candidate_review_id"] = bad_candidate.get("review_id")
    item["last_failed_slskd_transfer_id"] = bad_candidate.get("slskd_transfer_id") or item.get("last_slskd_transfer_id")
    item["last_failed_slskd_transfer_state"] = bad_candidate.get("slskd_transfer_state") or item.get("last_slskd_transfer_state")
    item["last_failed_slskd_transfer_requested_at"] = (
        bad_candidate.get("slskd_transfer_requested_at")
        or item.get("last_slskd_transfer_requested_at")
    )
    try:
        candidate_count = int(item.get("last_slskd_candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    try:
        failed_count = int(item.get("last_slskd_failed_candidate_count") or 0)
    except (TypeError, ValueError):
        failed_count = 0
    candidates_exhausted = (
        str(item.get("last_slskd_status") or "") == "failed_candidates_exhausted"
        or (candidate_count > 0 and failed_count >= candidate_count)
    )
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
    if retry_started:
        item["last_slskd_autoresolve_status"] = "retry_started_after_failure"
        item["last_slskd_autoresolve_reason"] = "next SLSKD candidate started after previous failure"
        clear_failed_import_state_for_new_download(item, now)
    elif candidates_exhausted:
        item["last_slskd_autoresolve_status"] = "retry_exhausted"
        item["last_slskd_autoresolve_reason"] = "SLSKD candidates exhausted; continuing source ladder"
    else:
        item["last_slskd_autoresolve_status"] = "candidate_failed"
        item["last_slskd_autoresolve_reason"] = bad_candidate.get("detail") or bad_candidate.get("failure_label") or bad_candidate.get("reason")
    item["last_slskd_autoresolve_at"] = now
    item["last_slskd_autoresolve_at_iso"] = now_iso(now)
    item["slskd_active_cleared_at"] = now
    item["slskd_active_cleared_at_iso"] = now_iso(now)
    touch_queue_item(item, now)

    should_release = item.get("state") in {"downloading", "importing"} or item.get("current_source") == "slskd"
    if should_release:
        item["state"] = "downloading" if retry_started else "searching"
        item["current_source"] = "slskd" if retry_started else None
        if retry_started:
            item["last_event"] = "failed SLSKD candidate marked bad; next candidate started"
        elif candidates_exhausted:
            item["last_event"] = "SLSKD candidates exhausted; continuing source ladder"
        else:
            item["last_event"] = "failed SLSKD candidate marked bad; retrying next candidate"
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
    return True


def retry_pending_reason(record):
    if not isinstance(record, dict):
        return "waiting to retry next SLSKD candidate"
    retry = record.get("last_retry") if isinstance(record.get("last_retry"), dict) else {}
    return (
        retry.get("reason")
        or retry.get("error")
        or record.get("reason")
        or "waiting to retry next SLSKD candidate"
    )


def apply_pending_slskd_retry(item, record, now):
    if not isinstance(item, dict) or not isinstance(record, dict):
        return False
    if item.get("state") == "verified":
        return False
    bad_candidate = record.get("bad_candidate") if isinstance(record.get("bad_candidate"), dict) else None
    if bad_candidate:
        apply_failed_slskd_candidate(item, bad_candidate, now, retry_started=False)
    elif item.get("state") in {"downloading", "importing"} or item.get("current_source") == "slskd":
        clear_verified_slskd_activity(item)
    item["state"] = "searching"
    item["current_source"] = None
    item["last_slskd_autoresolve_status"] = "retry_pending"
    item["last_slskd_autoresolve_reason"] = retry_pending_reason(record)
    item["last_slskd_autoresolve_at"] = now
    item["last_slskd_autoresolve_at_iso"] = now_iso(now)
    touch_queue_item(item, now)
    retry = record.get("last_retry") if isinstance(record.get("last_retry"), dict) else {}
    if str(retry.get("status") or "") in {"busy", "locked"}:
        item["last_event"] = "SLSKD worker busy; retrying next candidate soon"
    else:
        item["last_event"] = "failed SLSKD candidate marked bad; waiting to retry next candidate"
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)
    return True


def first_present(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def transfer_row_value(row, key):
    transfer = row.get("transfer") if isinstance(row.get("transfer"), dict) else {}
    return first_present(transfer.get(key), row.get(f"transfer_{key}"))


def transfer_percent_label(value):
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return ""
    if percent <= 0:
        return ""
    if percent >= 99.5:
        return "100%"
    return f"{percent:.0f}%"


def clear_verified_slskd_activity(item):
    if not isinstance(item, dict):
        return False
    changed = False
    for key in SLSKD_ACTIVE_FIELDS:
        if key in item:
            item.pop(key, None)
            changed = True
    if item.get("current_source") == "slskd":
        item["current_source"] = None
        changed = True
    return changed


def clear_active_retry_state(item):
    if not isinstance(item, dict):
        return False
    if item.get("state") not in {"downloading", "importing", "verified"}:
        return False
    changed = False
    for key in ("retry_after", "retry_after_iso", "needs_you_reason"):
        if key in item:
            item.pop(key, None)
            changed = True
    return changed


def clear_failed_import_state_for_new_download(item, now=None):
    if not isinstance(item, dict):
        return False
    failed_statuses = set(FAILED_RECONCILIATION_STATES) | {"library_scan_timeout_failed", "kavita_scan_timeout_failed"}
    if str(item.get("last_import_status") or "") not in failed_statuses:
        return False
    now = now or time.time()
    item["stale_failed_import_status"] = item.get("last_import_status")
    item["stale_failed_import_cleared_at"] = now
    item["stale_failed_import_cleared_at_iso"] = now_iso(now)
    changed = False
    for key in (
        "last_import_status",
        "last_import_failed_status",
        "last_import_failed_dest",
        "last_import_failed_at",
        "last_import_failed_at_iso",
        "last_import_failed_retry_count",
        "last_bad_archive_source",
        "last_bad_archive_dest",
        "last_bad_archive_pack_review_id",
        "last_bad_archive_reason",
        "last_import_ignored_dest",
        "last_import_ignored_reason",
    ):
        if key in item:
            item.pop(key, None)
            changed = True
    return changed


def clear_failed_candidate_status(item):
    if not isinstance(item, dict):
        return False
    changed = False
    for key in (
        "last_failed_candidate_reason",
        "last_failed_candidate_kind",
        "last_failed_candidate_label",
        "last_failed_candidate_detail",
        "last_failed_candidate_filename",
        "last_failed_candidate_user",
        "last_failed_candidate_score",
        "last_failed_candidate_at",
        "last_failed_candidate_at_iso",
        "last_failed_candidate_review_id",
        "last_failed_slskd_transfer_id",
        "last_failed_slskd_transfer_state",
        "last_failed_slskd_transfer_requested_at",
    ):
        if key in item:
            item.pop(key, None)
            changed = True
    return changed


def clear_slskd_transfer_activity_fields(item):
    if not isinstance(item, dict):
        return False
    changed = False
    for key in (
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
        if key in item:
            item.pop(key, None)
            changed = True
    return changed


def mark_safe_slskd_alternate_available(item, bad_candidate, now):
    if not isinstance(item, dict) or not isinstance(bad_candidate, dict):
        return False
    item["last_failed_candidate_reason"] = bad_candidate.get("reason") or "candidate_failed"
    item["last_failed_candidate_kind"] = bad_candidate.get("failure_kind")
    item["last_failed_candidate_label"] = bad_candidate.get("failure_label")
    item["last_failed_candidate_detail"] = bad_candidate.get("detail")
    item["last_failed_candidate_filename"] = bad_candidate.get("filename")
    item["last_failed_candidate_user"] = bad_candidate.get("username")
    item["last_failed_candidate_score"] = bad_candidate.get("candidate_score")
    item["last_failed_candidate_review_id"] = bad_candidate.get("review_id")
    item["last_failed_candidate_at"] = row_ts(bad_candidate) or now
    item["last_failed_candidate_at_iso"] = bad_candidate.get("ts_iso") or now_iso(item["last_failed_candidate_at"])
    item["last_failed_slskd_transfer_id"] = bad_candidate.get("slskd_transfer_id") or item.get("last_slskd_transfer_id")
    item["last_failed_slskd_transfer_state"] = bad_candidate.get("slskd_transfer_state") or item.get("last_slskd_transfer_state")
    item["last_failed_slskd_transfer_requested_at"] = (
        bad_candidate.get("slskd_transfer_requested_at")
        or item.get("last_slskd_transfer_requested_at")
    )
    clear_slskd_transfer_activity_fields(item)
    item["last_slskd_autoresolve_status"] = "safe_alternate_available"
    item["last_slskd_autoresolve_reason"] = "failed SLSKD candidate marked bad; safe alternate available"
    item["last_slskd_autoresolve_at"] = now
    item["last_slskd_autoresolve_at_iso"] = now_iso(now)
    item["last_failed_candidate_safe_alternate_available_at"] = now
    item["last_failed_candidate_safe_alternate_available_at_iso"] = now_iso(now)
    touch_queue_item(item, now)
    if item.get("state") not in {"importing", "verified", "needs_you"}:
        item["state"] = "searching"
        item["current_source"] = None
        item["last_event"] = "failed SLSKD candidate marked bad; safe alternate available"
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
    return True


def apply_slskd_transfer_status(item, row, now):
    if not isinstance(item, dict) or not isinstance(row, dict):
        return False
    if item.get("state") == "verified":
        return False
    transfer = row.get("transfer") if isinstance(row.get("transfer"), dict) else {}
    status = str(row.get("status") or row.get("transfer_status") or transfer.get("status") or "")
    reason = str(row.get("reason") or "")
    if not status and not transfer:
        return False

    item["current_source"] = "slskd"
    item["last_slskd_autoresolve_status"] = status
    item["last_slskd_autoresolve_reason"] = reason
    item["last_slskd_autoresolve_at"] = now
    item["last_slskd_autoresolve_at_iso"] = now_iso(now)
    touch_queue_item(item, now)
    for dest, value in (
        ("last_slskd_transfer_id", first_present(row.get("slskd_transfer_id"), transfer.get("id"))),
        (
            "last_slskd_transfer_state",
            first_present(transfer.get("stateDescription"), transfer.get("state"), row.get("slskd_transfer_state")),
        ),
        (
            "last_slskd_transfer_requested_at",
            first_present(transfer.get("requestedAt"), row.get("slskd_transfer_requested_at")),
        ),
        ("last_slskd_transfer_started_at", transfer.get("startedAt")),
        ("last_slskd_transfer_ended_at", transfer.get("endedAt")),
        ("last_slskd_transfer_percent", transfer_row_value(row, "percentComplete")),
        ("last_slskd_transfer_bytes_transferred", transfer_row_value(row, "bytesTransferred")),
        ("last_slskd_transfer_bytes_remaining", transfer_row_value(row, "bytesRemaining")),
        ("last_slskd_transfer_average_speed", transfer_row_value(row, "averageSpeed")),
        ("last_slskd_transfer_attempts", transfer.get("attempts")),
        ("last_slskd_candidate", first_present(row.get("filename"), transfer.get("filename"))),
        ("last_slskd_user", first_present(row.get("username"), transfer.get("username"))),
    ):
        if value not in (None, ""):
            item[dest] = value

    percent = transfer_percent_label(item.get("last_slskd_transfer_percent"))
    state_text = str(item.get("last_slskd_transfer_state") or "").strip()
    if status == "transfer_in_progress":
        item["state"] = "downloading"
        if percent:
            item["last_event"] = f"SLSKD transfer {percent} complete"
        elif state_text:
            item["last_event"] = f"SLSKD transfer {state_text.lower()}; waiting for download"
        else:
            item["last_event"] = "SLSKD transfer in progress; waiting for download"
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        return True
    if status == "transfer_settling":
        item["state"] = "downloading"
        item["last_event"] = "SLSKD transfer completed; waiting for staged file to settle"
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        return True
    if status in {"waiting_for_staged_file", "transfer_lookup_error", "transfer_unknown"}:
        if item.get("state") not in {"importing", "verified"}:
            item["state"] = "downloading"
        item["last_event"] = (
            "SLSKD transfer status unavailable; watcher will retry"
            if status == "transfer_lookup_error"
            else "SLSKD transfer is being watched for staged file"
        )
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        return True
    if status in {
        "transfer_failed",
        "transfer_stalled",
        "transfer_succeeded_missing_stage",
        "transfer_stale_unknown",
        "transfer_missing_stale",
    }:
        item["state"] = "searching"
        item["current_source"] = None
        item["last_slskd_autoresolve_status"] = "retry_pending"
        item["last_slskd_autoresolve_reason"] = reason or "SLSKD transfer failed; retrying next candidate"
        item["last_slskd_autoresolve_at"] = now
        item["last_slskd_autoresolve_at_iso"] = now_iso(now)
        item["last_failed_candidate_filename"] = item.get("last_slskd_candidate")
        item["last_failed_candidate_user"] = item.get("last_slskd_user")
        item["last_failed_slskd_transfer_id"] = item.get("last_slskd_transfer_id")
        item["last_failed_slskd_transfer_state"] = item.get("last_slskd_transfer_state")
        item["last_event"] = reason or "SLSKD transfer failed; retrying next candidate"
        item.pop("needs_you_reason", None)
        return True
    return False


def stale_slskd_import_signal(item, now):
    if item.get("state") != "importing":
        return False
    slskd_signal = (
        item.get("current_source") == "slskd"
        or str(item.get("last_slskd_status") or "") == "staged_file_ready"
        or str(item.get("last_event") or "") in {
            "SLSKD/manual staged file detected",
            "staged file detected; waiting for verified import",
        }
    )
    if not slskd_signal:
        return False
    if (
        item.get("last_import_status") in {"library_scan_timeout", "kavita_scan_timeout"}
        and item.get("last_import_dest")
    ):
        dest = Path(str(item.get("last_import_dest")))
        if dest.exists() or kavita_file_visible_for_host_path(item.get("last_import_dest")):
            return False
        return True
    if str(item.get("last_slskd_status") or "") != "staged_file_ready":
        return False
    try:
        last_slskd_at = float(item.get("last_slskd_at") or 0)
    except (TypeError, ValueError):
        last_slskd_at = 0
    if last_slskd_at <= 0:
        return False
    return last_slskd_at < now - STALE_SLSKD_IMPORT_SIGNAL_SECONDS


def clear_stale_slskd_import_signal(item, now, srow=None):
    item["state"] = "queued"
    item["current_source"] = None
    item["stale_slskd_status"] = item.get("last_slskd_status")
    item["stale_slskd_detected_count"] = item.get("last_slskd_detected_count")
    item["stale_slskd_cleared_at"] = now
    item["stale_slskd_cleared_at_iso"] = now_iso(now)
    if isinstance(srow, dict):
        item["stale_slskd_review_id"] = srow.get("review_id")
        item["stale_slskd_checked_at"] = slskd_row_checked_at(srow) or item.get("last_slskd_at")
        if item.get("stale_slskd_checked_at"):
            item["stale_slskd_checked_at_iso"] = now_iso(float(item["stale_slskd_checked_at"]))
    item["last_slskd_status"] = "stale_staged_signal_cleared"
    item["last_slskd_detected_count"] = 0
    item["last_slskd_candidate_count"] = 0
    item["last_slskd_failed_candidate_count"] = 0
    item["last_slskd_auto_grab_safe_count"] = 0
    item["last_slskd_auto_grab_review_count"] = 0
    item["last_slskd_auto_grab_blocked_count"] = 0
    item["last_event"] = "stale SLSKD staged signal cleared; watch still reports missing"
    touch_queue_item(item, now)
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)


def slskd_no_automatic_candidate_event(status):
    status = str(status or "")
    if status == "failed_candidates_exhausted":
        return "SLSKD candidates exhausted; continuing source ladder"
    if status == "no_query":
        return "SLSKD had no useful query; continuing source ladder"
    return "SLSKD found no automatic candidate; continuing source ladder"


def stale_slskd_autopick_signal(item):
    if not isinstance(item, dict):
        return False
    if item.get("state") in {"downloading", "importing", "verified", "needs_you"}:
        return False
    if str(item.get("last_event") or "") not in SLSKD_AUTOPICK_SIGNAL_EVENTS:
        return False
    if has_cached_safe_slskd_candidate(item):
        return False
    try:
        candidate_count = int(item.get("last_slskd_candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    try:
        safe_count = int(item.get("last_slskd_auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        safe_count = 0
    status = str(item.get("last_slskd_status") or "")
    if candidate_count > 0 and safe_count > 0:
        return False
    return candidate_count <= 0 or safe_count <= 0 or status in SLSKD_NO_AUTOMATIC_RESULT_STATES


def clear_stale_slskd_autopick_signal(item, now):
    item["state"] = "searching"
    if item.get("current_source") == "slskd":
        item["current_source"] = None
    try:
        candidate_count = int(item.get("last_slskd_candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    try:
        safe_count = int(item.get("last_slskd_auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        safe_count = 0
    if candidate_count > 0 and safe_count <= 0:
        if slskd_only_blocked_candidates(item):
            item["last_event"] = "SLSKD candidates were rejected by safety checks; continuing source ladder"
        else:
            item["last_event"] = "SLSKD candidates were not safe enough to auto-pick; continuing source ladder"
    else:
        item["last_event"] = slskd_no_automatic_candidate_event(item.get("last_slskd_status"))
    item["stale_slskd_autopick_cleared_at"] = now
    item["stale_slskd_autopick_cleared_at_iso"] = now_iso(now)
    item.pop("needs_you_reason", None)


def keep_pending_import_state(item, now, *, verification_status=None, dest=None):
    status = str(verification_status or item.get("last_import_status") or "")
    if status not in {"waiting_for_library_scan", "waiting_for_kavita_scan", "library_scan_timeout", "kavita_scan_timeout"}:
        return False
    dest = dest or item.get("last_import_dest")
    if not dest:
        return False
    if kavita_file_visible_for_item(item, dest):
        item["state"] = "verified"
        item["completed_at"] = now
        item["completed_at_iso"] = now_iso(now)
        item["last_import_status"] = "library_visible"
        item["last_import_dest"] = dest
        item["last_event"] = "Library verified imported file"
        item["current_source"] = None
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        return True
    if not Path(str(dest)).exists():
        return False
    if status in {"library_scan_timeout", "kavita_scan_timeout"}:
        outcome = handle_library_scan_timeout(item, now, dest)
        if outcome == "released":
            return True
    item["state"] = "importing"
    item["last_import_status"] = status
    item["last_import_dest"] = dest
    if status in {"library_scan_timeout", "kavita_scan_timeout"}:
        if item.get("last_import_scan_retry_error"):
            item["last_event"] = "Library scan retry failed; automation will retry"
        elif item.get("last_import_scan_retry_queued_at"):
            item["last_event"] = "Library scan retry queued for imported file"
        else:
            item["last_event"] = "imported file is waiting for library scan retry"
    else:
        item["last_event"] = "imported file is waiting for library scan"
    if item.get("current_source") in {None, "", "verified"}:
        item["current_source"] = "slskd" if (item.get("last_slskd_status") or item.get("last_slskd_transfer_id")) else "import"
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)
    return True


def handle_library_scan_timeout(item, now, dest):
    try:
        retry_count = int(item.get("last_import_scan_retry_count") or 0)
    except (TypeError, ValueError):
        retry_count = 0
    last_retry_at = numeric_timestamp(item.get("last_import_scan_retry_at"))
    if (
        retry_count >= LIBRARY_IMPORT_SCAN_RETRY_LIMIT
        and last_retry_at > 0
        and last_retry_at <= now - LIBRARY_IMPORT_SCAN_RETRY_SETTLE_SECONDS
    ):
        release_scan_timeout_import(item, now, dest, retry_count)
        return "released"
    if last_retry_at > now - LIBRARY_IMPORT_SCAN_RETRY_SECONDS:
        return "waiting"
    try:
        import inkdrop_completed_import as completed_import

        folder = str(Path(str(dest)).parent)
        if hasattr(completed_import, "sync_library_frontend_folders"):
            scan = completed_import.sync_library_frontend_folders(
                [folder],
                force_library_scan_folders={folder},
                event_prefix="autopilot_",
            )
        else:
            scan = completed_import.trigger_kavita_scan_folder(folder, force_library_scan=True)
        item["last_import_scan_retry"] = scan
        item.pop("last_import_scan_retry_error", None)
    except Exception as exc:
        item["last_import_scan_retry_error"] = f"{type(exc).__name__}: {exc}"
        log(
            "library_import_scan_retry_failed",
            series=item.get("series"),
            issue=item.get("issue"),
            dest=dest,
            error=item["last_import_scan_retry_error"],
        )
    item["last_import_scan_retry_count"] = retry_count + 1
    item["last_import_scan_retry_at"] = now
    item["last_import_scan_retry_at_iso"] = now_iso(now)
    item["last_import_scan_retry_queued_at"] = now
    item["last_import_scan_retry_queued_at_iso"] = now_iso(now)
    item.setdefault("attempts", []).append(
        {
            "ts": now,
            "ts_iso": now_iso(now),
            "source": "library_frontend",
            "status": "scan_retry_queued" if not item.get("last_import_scan_retry_error") else "scan_retry_error",
            "dest": dest,
            "retry_count": retry_count + 1,
        }
    )
    return "queued"


handle_kavita_scan_timeout = handle_library_scan_timeout


def release_scan_timeout_import(item, now, dest, retry_count):
    item["state"] = "searching"
    item["current_source"] = None
    item["last_event"] = "Library adapters did not see imported file after scan retries; trying sources again"
    item["last_import_status"] = "library_scan_timeout_failed"
    item["last_import_failed_status"] = "library_scan_timeout"
    item["last_import_failed_dest"] = dest
    item["last_import_failed_at"] = now
    item["last_import_failed_at_iso"] = now_iso(now)
    item["last_import_failed_retry_count"] = retry_count
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)
    item.setdefault("attempts", []).append(
        {
            "ts": now,
            "ts_iso": now_iso(now),
            "source": "library_frontend",
            "status": "scan_timeout_released_to_sources",
            "dest": dest,
            "retry_count": retry_count,
        }
    )


def clear_mismatched_verified_state(item, now, path):
    item["state"] = "searching"
    item["current_source"] = None
    item["last_event"] = "verified path belonged to a different metadata-adapter folder; rechecking sources"
    item["verified_path_mismatch_at"] = now
    item["verified_path_mismatch_at_iso"] = now_iso(now)
    item["verified_path_mismatch_path"] = path
    item.pop("completed_at", None)
    item.pop("completed_at_iso", None)
    item.pop("last_import_status", None)
    item.pop("last_import_dest", None)
    item.pop("last_kavita_file_path", None)
    item.pop("last_local_truth_at", None)
    item.pop("last_local_truth_at_iso", None)
    item.pop("needs_you_reason", None)


def clear_mismatched_verified_import_state(item, now, dest=None, *, force=False):
    if not isinstance(item, dict):
        return False
    if not force and str(item.get("last_import_status") or "") not in {"library_visible", "kavita_verified"}:
        return False
    dest = dest or item.get("last_import_dest")
    if not dest:
        return False
    if kavita_file_visible_for_item(item, dest):
        return False
    records = kavita_file_records_for_host_path(dest)
    reason = "different_issue" if records else "verified_path_not_visible"
    item["state"] = "searching"
    item["current_source"] = None
    item["last_event"] = (
        "verified import belonged to a different issue; continuing source ladder"
        if records
        else "verified import is not visible for this issue; continuing source ladder"
    )
    item["last_import_ignored_dest"] = dest
    item["last_import_ignored_reason"] = reason
    item["last_import_ignored_at"] = now
    item["last_import_ignored_at_iso"] = now_iso(now)
    if records:
        item["last_import_ignored_records"] = records[:5]
    item.pop("completed_at", None)
    item.pop("completed_at_iso", None)
    item.pop("last_import_status", None)
    item.pop("last_import_dest", None)
    item.pop("last_kavita_file_path", None)
    item.pop("last_local_truth_at", None)
    item.pop("last_local_truth_at_iso", None)
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)
    item.setdefault("attempts", []).append(
        {
            "ts": now,
            "ts_iso": now_iso(now),
            "source": "library_frontend",
            "status": f"verified_import_ignored_{reason}",
            "dest": dest,
        }
    )
    return True


def clear_stale_verified_import_metadata(item, now):
    if not isinstance(item, dict):
        return False
    if item.get("state") == "verified":
        return False
    if str(item.get("last_import_status") or "") not in {"library_visible", "kavita_verified"}:
        return False
    reason = str(item.get("last_import_ignored_reason") or "")
    if reason == "different_kapowarr_folder" and not item_uses_kapowarr_as_truth(item):
        item["state"] = "verified"
        item["completed_at"] = now
        item["completed_at_iso"] = now_iso(now)
        item["last_event"] = "Library verified imported file; metadata-adapter folder mismatch ignored"
        item["current_source"] = None
        item["last_verified_folder_mismatch_allowed"] = "native_inkdrop_series"
        item.pop("last_import_ignored_reason", None)
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        return True
    if reason not in {"different_issue", "verified_path_not_visible", "different_kapowarr_folder"}:
        return False
    dest = item.get("last_import_ignored_dest") or item.get("last_import_dest") or item.get("imported_path")
    item["state"] = "searching"
    item["current_source"] = None
    item["last_event"] = (
        "verified import belonged to a different issue; continuing source ladder"
        if reason == "different_issue"
        else "verified import is not visible for this issue; continuing source ladder"
    )
    item["stale_verified_import_metadata_cleared_at"] = now
    item["stale_verified_import_metadata_cleared_at_iso"] = now_iso(now)
    if dest:
        item["stale_verified_import_metadata_dest"] = dest
    item.pop("last_import_status", None)
    item.pop("last_import_dest", None)
    item.pop("imported_path", None)
    item.pop("completed_at", None)
    item.pop("completed_at_iso", None)
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)
    return True


def keep_verified_import_state(item, now, folder_prefixes=None):
    status = str(item.get("last_import_status") or "")
    if status not in {"folder_verified", "library_visible", "kavita_verified"}:
        return False
    dest = item.get("last_import_dest")
    if not dest:
        return False
    if status == "folder_verified":
        if not Path(str(dest)).exists():
            return False
        item["state"] = "verified"
        item["completed_at"] = now
        item["completed_at_iso"] = now_iso(now)
        item["last_event"] = "Folder verified imported file"
        item["current_source"] = None
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        return True
    if not kavita_file_visible_for_item(item, dest):
        clear_mismatched_verified_import_state(item, now, dest)
        return False
    path = item.get("last_kavita_file_path") or dest
    if not item_path_matches_kapowarr_folder(item, path, folder_prefixes) and not kavita_file_verified_for_item(item, path):
        clear_mismatched_verified_state(item, now, path)
        return False
    if not item_path_matches_kapowarr_folder(item, path, folder_prefixes):
        item["last_verified_folder_mismatch_allowed"] = "kavita_series_issue_match"
    item["state"] = "verified"
    item["completed_at"] = now
    item["completed_at_iso"] = now_iso(now)
    item["last_event"] = "Library verified imported file"
    item["current_source"] = None
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)
    return True


def stale_downloader_send(item, now):
    if item.get("state") not in {"downloading", "source_wait"}:
        return False
    if not item.get("present_in_watch", True):
        return False
    watch_status = str(item.get("watch_status") or "").strip().lower()
    if watch_status in {"verified", "satisfied", "complete", "completed", "suppressed", "ignored"}:
        return False
    if item.get("current_source") not in {"prowlarr", "rss", "comicscodes", "failed_retry"}:
        return False
    if "sent a candidate" not in str(item.get("last_event") or ""):
        return False
    try:
        last_action_at = float(item.get("last_action_at") or 0)
    except (TypeError, ValueError):
        last_action_at = 0
    if last_action_at <= 0:
        attempts = [
            attempt for attempt in item.get("attempts", [])
            if isinstance(attempt, dict) and attempt.get("status") == "sent"
        ]
        if attempts:
            try:
                last_action_at = max(float(attempt.get("ts") or 0) for attempt in attempts)
            except (TypeError, ValueError):
                last_action_at = 0
    if last_action_at <= 0:
        return False
    return last_action_at < now - STALE_DOWNLOADER_SEND_SECONDS


def downloader_handoff_wait(item):
    if not isinstance(item, dict):
        return False
    if item.get("state") not in {"downloading", "source_wait"}:
        return False
    if not item.get("present_in_watch", True):
        return False
    watch_status = str(item.get("watch_status") or "").strip().lower()
    if watch_status in {"verified", "satisfied", "complete", "completed", "suppressed", "ignored"}:
        return False
    if item.get("current_source") not in {"prowlarr", "rss", "comicscodes", "failed_retry"}:
        return False
    if item.get("download_started_at") or item.get("last_download_started_at"):
        return False
    return "sent a candidate" in str(item.get("last_event") or "")


def stale_downloader_send_result(item):
    if not isinstance(item, dict):
        return False
    if item.get("state") in {"downloading", "importing", "verified", "needs_you"}:
        return False
    if item.get("current_source"):
        return False
    event = str(item.get("last_event") or "")
    event_lower = event.lower()
    event_lower = event.lower()
    return event in {STALE_DOWNLOADER_SEND_EVENT, STALE_DOWNLOADER_CONTINUE_EVENT}


def normalize_stale_downloader_send_result(item, now):
    if not stale_downloader_send_result(item):
        return False
    source = (
        item.get("stale_downloader_source")
        or item.get("stale_current_source")
        or item.get("last_source_outcome_source")
        or "downloader"
    )
    title = item.get("stale_downloader_title") or item.get("last_candidate_title") or item.get("last_source_outcome_candidate")
    row = {"candidate": {"title": title}} if title else None
    outcome_at = numeric_timestamp(item.get("stale_downloader_cleared_at")) or now
    record_automation_source_outcome(item, STALE_DOWNLOADER_OUTCOME_REASON, source, outcome_at, row)
    item["state"] = "searching"
    item["current_source"] = None
    item["last_event"] = STALE_DOWNLOADER_CONTINUE_EVENT
    item["stale_downloader_normalized_at"] = now
    item["stale_downloader_normalized_at_iso"] = now_iso(now)
    if str(source or "") == "failed_retry":
        item["last_failed_retry_reason"] = STALE_DOWNLOADER_OUTCOME_REASON
        item["last_failed_retry_at"] = outcome_at
        item["last_failed_retry_at_iso"] = now_iso(outcome_at)
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)
    return True


def active_pack_import_review_ids(now):
    active = {}
    pack_state = read_json(PACK_REVIEW_STATE_FILE, {}) or {}
    state_active = pack_state.get("active") if isinstance(pack_state, dict) else {}
    if isinstance(state_active, dict):
        review_id = str(state_active.get("review_id") or "").strip()
        status = str(state_active.get("status") or "")
        try:
            updated_at = float(state_active.get("updated_at") or state_active.get("approved_at") or 0)
        except (TypeError, ValueError):
            updated_at = 0
        if review_id and status in {"importing"} and now - updated_at <= PACK_IMPORT_ACTIVE_SECONDS:
            active[review_id] = {
                "status": status,
                "title": state_active.get("title"),
                "pack_path": state_active.get("pack_path"),
                "source": "pack_review_state",
            }

    auto_status = read_json(PACK_AUTO_IMPORT_STATUS_FILE, {}) or {}
    if isinstance(auto_status, dict):
        review_id = str(auto_status.get("review_id") or "").strip()
        status = str(auto_status.get("status") or "")
        try:
            ts = float(auto_status.get("ts") or auto_status.get("updated_at") or 0)
        except (TypeError, ValueError):
            ts = 0
        if review_id and status in {"starting", "running", "importing"} and now - ts <= PACK_IMPORT_ACTIVE_SECONDS:
            active[review_id] = {
                "status": status,
                "title": auto_status.get("title"),
                "pack_path": auto_status.get("selected_path"),
                "source": "pack_auto_import",
            }
    return active


def mark_active_pack_import(item, pack_status, now):
    item["state"] = "importing"
    item["current_source"] = "pack_import"
    item["last_pack_import_status"] = pack_status.get("status")
    item["last_pack_import_source"] = pack_status.get("source")
    item["last_pack_import_at"] = now
    item["last_pack_import_at_iso"] = now_iso(now)
    if pack_status.get("pack_path"):
        item["last_pack_import_path"] = pack_status.get("pack_path")
    if pack_status.get("title"):
        item["last_candidate_title"] = pack_status.get("title")
    item["last_event"] = "pack import running"
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)


def clear_inactive_pack_import(item, now):
    item["stale_pack_import_review_id"] = item.get("last_pack_review_id")
    item["stale_pack_import_status"] = item.get("last_pack_import_status")
    item["stale_pack_import_source"] = item.get("last_pack_import_source")
    item["stale_pack_import_cleared_at"] = now
    item["stale_pack_import_cleared_at_iso"] = now_iso(now)
    item["state"] = "searching"
    item["current_source"] = None
    item["last_event"] = "pack import is no longer active; continuing source ladder"
    item.pop("last_pack_review_id", None)
    item.pop("last_pack_import_status", None)
    item.pop("last_pack_import_source", None)
    item.pop("last_pack_import_path", None)
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)


def clear_inactive_source_marker(item, now):
    if not isinstance(item, dict):
        return False
    state = str(item.get("state") or "")
    source = str(item.get("current_source") or "")
    if not source or state not in {"queued", "searching"}:
        return False
    event = str(item.get("last_event") or "")
    event_lower = event.lower()
    should_clear = state == "queued"
    if state == "searching":
        try:
            last_attempt_at = float(item.get("last_attempt_at") or 0)
        except (TypeError, ValueError):
            last_attempt_at = 0
        active_trying = event.startswith("trying ") and last_attempt_at > now - STALE_SEARCH_SOURCE_MARKER_SECONDS
        has_active_slskd = bool(item.get("last_slskd_waiting_review_id") or item.get("last_slskd_transfer_id"))
        has_staged_slskd = str(item.get("last_slskd_status") or "") == "staged_file_ready"
        should_clear = (
            not active_trying
            and not has_active_slskd
            and not has_staged_slskd
            and (
                source in {"failed_retry", "slskd"}
                or (source == "download_client" and "retrying source ladder" in event_lower)
                or retry_after_ts(item) > 0
                or event in SLSKD_AUTOPICK_SIGNAL_EVENTS
                or "retry scheduled" in event
                or "retry_scheduled" in event_lower
                or "source fallback queued" in event
                or "low-confidence source candidate parked" in event
            )
        )
    if not should_clear:
        return False
    item["stale_current_source"] = source
    item["stale_current_source_cleared_at"] = now
    item["stale_current_source_cleared_at_iso"] = now_iso(now)
    item["current_source"] = None
    return True


def pack_title_covers_item(title, item):
    pack_info = detect_pack_info(title)
    if not pack_info.get("is_pack") or not pack_info.get("ranges"):
        return True
    return pack_range_covers_issue(pack_info, issue_number_value(item.get("issue")))


def stale_pack_assignment(item):
    title = item.get("last_candidate_title")
    if not title:
        return False
    return not pack_title_covers_item(title, item)


def clear_stale_pack_assignment(item, now, title=None, source="annotation"):
    title = title or item.get("last_candidate_title")
    item["stale_pack_title"] = title
    item["stale_pack_review_id"] = item.get("last_pack_review_id")
    item["stale_pack_reason"] = "pack_does_not_cover_trigger_issue"
    item["stale_pack_cleared_at"] = now
    item["stale_pack_cleared_at_iso"] = now_iso(now)
    item.pop("last_candidate_title", None)
    item.pop("last_pack_review_id", None)
    item.pop("last_pack_trigger_issue", None)
    if item.get("state") == "downloading":
        item["state"] = "searching"
        item["current_source"] = None
        item["last_event"] = "stale pack assignment cleared; pack does not cover this issue"
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
    item.setdefault("attempts", []).append(
        {
            "ts": now,
            "ts_iso": now_iso(now),
            "source": source,
            "status": "stale_pack_cleared",
            "title": title,
            "reason": "pack_does_not_cover_trigger_issue",
        }
    )


def automatic_sources_exhausted(item, exhaustion_cycles=DEFAULT_EXHAUSTION_CYCLES, *, now=None, grace_seconds=0):
    try:
        attempts = int(item.get("source_ladder_attempt_count") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if attempts < int(exhaustion_cycles or 0):
        return False
    if missing_required_source_result_sources(item):
        return False
    if not slskd_attempted_at(item):
        return False
    slskd_status = str(item.get("last_slskd_status") or "")
    try:
        candidate_count = int(item.get("last_slskd_candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    try:
        detected_count = int(item.get("last_slskd_detected_count") or 0)
    except (TypeError, ValueError):
        detected_count = 0
    try:
        safe_count = int(item.get("last_slskd_auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        safe_count = 0
    if detected_count > 0:
        return False
    no_automatic_result = slskd_status in SLSKD_NO_AUTOMATIC_RESULT_STATES
    unsafe_candidates_only = slskd_status == "available" and candidate_count > 0 and safe_count <= 0
    if no_automatic_result:
        return False
    if not unsafe_candidates_only:
        return False
    if item.get("state") in {"downloading", "importing", "verified", "needs_you"}:
        return False
    if grace_seconds:
        current_source = str(item.get("current_source") or "")
        last_attempt_at = 0
        for key in ("last_attempt_at", "source_ladder_attempted_at"):
            try:
                last_attempt_at = max(last_attempt_at, float(item.get(key) or 0))
            except (TypeError, ValueError):
                pass
        if current_source and last_attempt_at and (now or time.time()) - last_attempt_at < grace_seconds:
            return False
    return True


def no_actionable_source_result(item):
    if not slskd_attempted_at(item):
        return False
    slskd_status = str(item.get("last_slskd_status") or "")
    if slskd_status not in SLSKD_NO_AUTOMATIC_RESULT_STATES:
        return False
    try:
        safe_count = int(item.get("last_slskd_auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        safe_count = 0
    if safe_count > 0:
        return False
    try:
        detected_count = int(item.get("last_slskd_detected_count") or 0)
    except (TypeError, ValueError):
        detected_count = 0
    return detected_count <= 0


def missing_required_source_result_sources(item):
    if not isinstance(item, dict):
        return []
    missing = []
    for source in queue_item_source_order(item):
        source = str(source or "").strip().lower()
        if source in {"", "local"} or source not in SOURCE_PROVIDER_IDS:
            continue
        if not source_enabled(source):
            continue
        slskd_reprobe = source == "slskd" and slskd_source_result_reprobe_due(item)
        if source == "slskd" and slskd_attempted_at(item) and not slskd_reprobe:
            continue
        if queue_item_recorded_source_result_attempt_count(item, source) > 0 and not slskd_reprobe:
            continue
        missing.append(source)
    return missing


def update_pending_source_result_markers(item):
    missing = missing_required_source_result_sources(item)
    if missing:
        item["pending_source_result_sources"] = list(missing)
        item["pending_source_result_sources_text"] = ", ".join(public_source_name(source) or source for source in missing)
    else:
        item.pop("pending_source_result_sources", None)
        item.pop("pending_source_result_sources_text", None)
    return missing


def automatic_source_retry_event(item):
    event = str((item or {}).get("last_event") or "").strip().lower()
    if not event:
        return False
    return any(
        marker in event
        for marker in (
            "automatic sources had no actionable candidate",
            "automatic sources exhausted",
            "source ladder attempted",
            "continuing source ladder",
            "slskd candidates were not safe enough",
            "slskd candidates were rejected by safety checks",
            "unsafe/rejected slskd candidates",
            "low-confidence slskd candidates",
            "not confident enough to auto-pick",
            "not safe enough to auto-pick",
        )
    )


def unsafe_or_low_confidence_event(item):
    event = str((item or {}).get("last_event") or "").strip().lower()
    return any(
        marker in event
        for marker in (
            "not safe enough",
            "rejected by safety checks",
            "unsafe/rejected",
            "low-confidence",
            "not confident enough",
        )
    )


def slskd_safety_counts(item):
    item = item if isinstance(item, dict) else {}

    def count_value(key):
        try:
            return int(item.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "candidate": count_value("last_slskd_candidate_count"),
        "safe": count_value("last_slskd_auto_grab_safe_count"),
        "review": count_value("last_slskd_auto_grab_review_count"),
        "blocked": count_value("last_slskd_auto_grab_blocked_count"),
    }


def slskd_only_blocked_candidates(item):
    counts = slskd_safety_counts(item)
    return counts["candidate"] > 0 and counts["safe"] <= 0 and counts["review"] <= 0 and counts["blocked"] > 0


def slskd_no_safe_candidate_event(item, *, extended=False):
    if slskd_only_blocked_candidates(item):
        return (
            "automatic sources found only unsafe/rejected SLSKD candidates; extended retry scheduled"
            if extended
            else "automatic sources found only unsafe/rejected SLSKD candidates; retry scheduled"
        )
    return (
        "automatic sources found only low-confidence SLSKD candidates; extended retry scheduled"
        if extended
        else "automatic sources found only low-confidence SLSKD candidates; retry scheduled"
    )


def low_confidence_slskd_result(item):
    if not slskd_attempted_at(item):
        return False
    if str(item.get("last_slskd_status") or "") != "available":
        return False
    try:
        candidate_count = int(item.get("last_slskd_candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    try:
        safe_count = int(item.get("last_slskd_auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        safe_count = 0
    try:
        detected_count = int(item.get("last_slskd_detected_count") or 0)
    except (TypeError, ValueError):
        detected_count = 0
    return candidate_count > 0 and safe_count <= 0 and detected_count <= 0


def retry_after_ts(item):
    try:
        return float(item.get("retry_after") or 0)
    except (TypeError, ValueError):
        return 0


def retry_due_now(item, now=None):
    if now is None:
        now = time.time()
    retry_after = retry_after_ts(item)
    return bool(retry_after > 0 and retry_after <= now)


def retry_effectively_due(item, now=None):
    """Return whether a row has no active cooldown, so it is safe to retry now.

    A queue row with no retry_after timer at all is not "waiting for a
    scheduled retry" -- it has no cooldown in effect, the same as one whose
    timer already elapsed. missing_required_source_result_due() already
    treats an absent timer this way; scheduler_bucket_for_rows() must use the
    same rule so these rows land in the retry_due lane the scheduler actually
    drains, instead of falling through to the unscheduled "queued" bucket
    that only gets serviced when every other lane is empty.
    """
    if now is None:
        now = time.time()
    retry_after = retry_after_ts(item)
    return bool(retry_after <= 0 or retry_after <= now)


def preserve_due_retry_timer(item, now=None):
    if now is None:
        now = time.time()
    retry_after = retry_after_ts(item)
    return bool(retry_after > 0 and retry_after <= now)


def retry_later(item, now=None):
    if now is None:
        now = time.time()
    retry_after = retry_after_ts(item)
    return bool(retry_after > now)


def safe_cached_slskd_candidate_count(item):
    try:
        return int(item.get("last_slskd_auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        return 0


def filename_leaf(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("\\", "/").rsplit("/", 1)[-1]


def candidate_filename_values(row):
    if not isinstance(row, dict):
        return set()
    values = set()
    for key in (
        "filename",
        "path",
        "candidate_filename",
        "candidate_path",
        "filename_leaf",
        "detected_filename",
        "detected_path",
    ):
        value = str(row.get(key) or "").strip()
        if not value:
            continue
        values.add(normalize(value))
        values.add(normalize(filename_leaf(value)))
    values.discard("")
    return values


def failed_candidate_content_sensitive(item):
    text = " ".join(
        str(item.get(key) or "")
        for key in (
            "last_failed_candidate_reason",
            "last_failed_candidate_kind",
            "last_failed_candidate_label",
            "last_failed_candidate_detail",
            "last_slskd_autoresolve_reason",
        )
    ).lower()
    return any(
        marker in text
        for marker in (
            "archive",
            "bad_archive",
            "identity",
            "language",
            "non-english",
            "verification",
            "wrong series",
            "wrong_language",
        )
    )


def candidate_matches_last_failed_candidate(candidate, item):
    if not isinstance(candidate, dict) or not isinstance(item, dict):
        return False
    failed_values = candidate_filename_values(
        {
            "filename": item.get("last_failed_candidate_filename"),
            "filename_leaf": filename_leaf(item.get("last_failed_candidate_filename")),
            "detected_filename": item.get("last_failed_candidate_detected_filename"),
            "detected_path": item.get("last_failed_candidate_detected_path"),
        }
    )
    if not failed_values:
        return False
    candidate_values = candidate_filename_values(candidate)
    if not (failed_values & candidate_values):
        return False
    failed_user = normalize(item.get("last_failed_candidate_user") or "")
    candidate_user = normalize(candidate.get("username") or "")
    if failed_candidate_content_sensitive(item):
        return True
    return not failed_user or not candidate_user or failed_user == candidate_user


def safe_slskd_candidates_from_entry(entry, item=None):
    if not isinstance(entry, dict):
        return None
    candidates = entry.get("candidates")
    if not isinstance(candidates, list):
        return None
    safe = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        gate = candidate.get("auto_grab") if isinstance(candidate.get("auto_grab"), dict) else {}
        if gate.get("verdict") != "auto_grab_safe" or gate.get("blockers"):
            continue
        if candidate.get("manual_source_bad_candidate"):
            continue
        if item and candidate_matches_last_failed_candidate(candidate, item):
            continue
        safe.append(candidate)
    return safe


def effective_safe_slskd_candidate_count(entry, item=None):
    safe = safe_slskd_candidates_from_entry(entry, item=item)
    if safe is not None:
        return len(safe)
    try:
        return int((entry or {}).get("auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        return 0


def slskd_cache_entry_matches_item(entry, item):
    if not isinstance(entry, dict) or not isinstance(item, dict):
        return False
    if normalize(entry.get("series") or "") != normalize(item.get("series") or ""):
        return False
    if str(entry.get("issue") or "").strip() != str(item.get("issue") or "").strip():
        return False
    item_identity = str(item.get("queue_identity") or "").strip()
    entry_identity = str(entry.get("queue_identity") or "").strip()
    if item_identity and entry_identity and item_identity != entry_identity:
        return False
    for field in ("watch_id", "kapowarr_id", "comicvine_id"):
        item_value = str(item.get(field) or "").strip()
        entry_value = str(entry.get(field) or "").strip()
        if item_value and entry_value and item_value != entry_value:
            return False
    return True


def cached_safe_slskd_entry_for_item(item):
    if not isinstance(item, dict):
        return "", None
    cache = slskd_source_probe_cache()
    review_ids = [
        str(item.get("last_failed_candidate_review_id") or "").strip(),
        str(item.get("last_slskd_waiting_review_id") or "").strip(),
        str(item.get("review_id") or "").strip(),
    ]
    for review_id in dict.fromkeys(value for value in review_ids if value):
        entry = cache.get(review_id)
        if isinstance(entry, dict) and effective_safe_slskd_candidate_count(entry, item=item) > 0:
            return review_id, entry
    best_review_id = ""
    best_entry = None
    best_rank = (-1, 0.0)
    for review_id, entry in cache.items():
        if not slskd_cache_entry_matches_item(entry, item):
            continue
        safe_count = effective_safe_slskd_candidate_count(entry, item=item)
        if safe_count <= 0:
            continue
        rank = (safe_count, slskd_row_checked_at(entry))
        if rank > best_rank:
            best_review_id = str(entry.get("review_id") or review_id)
            best_entry = entry
            best_rank = rank
    return best_review_id, best_entry


def cached_safe_slskd_candidate_count(item):
    _review_id, entry = cached_safe_slskd_entry_for_item(item)
    if isinstance(entry, dict):
        return effective_safe_slskd_candidate_count(entry, item=item)
    return safe_cached_slskd_candidate_count(item)


def effective_safe_slskd_count_for_row(row, item=None):
    if not isinstance(row, dict):
        return 0
    safe = safe_slskd_candidates_from_entry(row, item=item)
    if safe is not None:
        return len(safe)
    cache = slskd_source_probe_cache()
    review_ids = [
        str(row.get("review_id") or "").strip(),
        str(row.get("last_failed_candidate_review_id") or "").strip(),
        str(row.get("last_slskd_waiting_review_id") or "").strip(),
    ]
    for review_id in dict.fromkeys(value for value in review_ids if value):
        entry = cache.get(review_id)
        if isinstance(entry, dict):
            return effective_safe_slskd_candidate_count(entry, item=item)
    try:
        raw_count = int(row.get("auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        raw_count = 0
    if raw_count > 0 and item and candidate_matches_last_failed_candidate(row, item):
        return max(0, raw_count - 1)
    return raw_count


def has_cached_safe_slskd_candidate(item):
    if not isinstance(item, dict):
        return False
    _review_id, entry = cached_safe_slskd_entry_for_item(item)
    if isinstance(entry, dict) and effective_safe_slskd_candidate_count(entry, item=item) > 0:
        return True
    if safe_cached_slskd_candidate_count(item) <= 0:
        return False
    try:
        candidate_count = int(item.get("last_slskd_candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    return candidate_count > 0


def has_due_cached_slskd_autopick(item, now=None, lookahead_seconds=0):
    if not isinstance(item, dict):
        return False
    if item.get("state") in {"verified", "downloading", "importing", "needs_you"}:
        return False
    if not has_cached_safe_slskd_candidate(item):
        return False
    if now is None:
        now = time.time()
    retry_after = retry_after_ts(item)
    try:
        lookahead = max(0, float(lookahead_seconds or 0))
    except (TypeError, ValueError):
        lookahead = 0
    return retry_after <= 0 or retry_after <= now + lookahead


def has_soon_cached_slskd_autopick(item, now=None):
    return has_due_cached_slskd_autopick(
        item,
        now=now,
        lookahead_seconds=SLSKD_CACHED_RETRY_LOOKAHEAD_SECONDS,
    )


def latest_slskd_result_signature_at(item):
    latest = max(
        numeric_timestamp((item or {}).get("last_slskd_at")),
        numeric_timestamp((item or {}).get("autopilot_slskd_attempted_at")),
    )
    for attempt in (item or {}).get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("kind") or "").strip().lower() in NON_RESULT_SOURCE_ATTEMPT_KINDS:
            continue
        source = source_order_attempt_key(
            attempt.get("source")
            or attempt.get("provider_id")
            or attempt.get("provider")
            or attempt.get("download_client")
        )
        if source != "slskd":
            continue
        latest = max(
            latest,
            numeric_timestamp(attempt.get("ts")),
            numeric_timestamp(attempt.get("started_at")),
        )
    return latest


def slskd_source_result_reprobe_due(item, now=None):
    """Rotate stale automatic zero-result signatures back through SLSKD."""
    if not isinstance(item, dict):
        return False
    if item.get("state") in ACTIVE_QUEUE_STATES | TERMINAL_QUEUE_STATES | {"needs_you"}:
        return False
    if has_cached_safe_slskd_candidate(item):
        return False
    try:
        candidate_count = int(item.get("last_slskd_candidate_count") or 0)
        detected_count = int(item.get("last_slskd_detected_count") or 0)
        safe_count = int(item.get("last_slskd_auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        return False
    # Candidate-bearing results stay governed by the existing safety, failed-
    # candidate, and review gates. This lane is only for a stale empty result.
    if candidate_count > 0 or detected_count > 0 or safe_count > 0:
        return False
    status = str(item.get("last_slskd_status") or "").strip().lower()
    if status and status not in SLSKD_NO_AUTOMATIC_RESULT_STATES | SLSKD_TRANSIENT_RESULT_STATES:
        return False
    recorded_count = int(queue_item_recorded_source_attempt_counts(item).get("slskd") or 0)
    if not slskd_attempted_at(item) and recorded_count <= 0:
        return False
    if now is None:
        now = time.time()
    if retry_after_ts(item) > now:
        return False
    signature_at = latest_slskd_result_signature_at(item)
    if signature_at <= 0:
        signature_at = queue_created_ts(item)
    cooldown = SLSKD_TRANSIENT_RETRY_SECONDS if status in SLSKD_TRANSIENT_RESULT_STATES else SLSKD_ZERO_RESULT_REPROBE_SECONDS
    return signature_at <= 0 or now - signature_at >= cooldown


def slskd_transient_retry_pending(item, now=None):
    if not isinstance(item, dict):
        return False
    if str(item.get("last_slskd_autopick_status") or "") != "transient_error":
        return False
    if now is None:
        now = time.time()
    retry_after = retry_after_ts(item)
    return retry_after > now


def slskd_transient_checked_result(item):
    if not isinstance(item, dict):
        return False
    if str(item.get("last_slskd_status") or "") not in SLSKD_TRANSIENT_RESULT_STATES:
        return False
    try:
        safe_count = int(item.get("last_slskd_auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        safe_count = 0
    try:
        detected_count = int(item.get("last_slskd_detected_count") or 0)
    except (TypeError, ValueError):
        detected_count = 0
    return safe_count <= 0 and detected_count <= 0


def slskd_user_load_limited(item):
    if not isinstance(item, dict):
        return False
    if item.get("last_slskd_autopick_status") not in {
        "user_load_wait", "waiting_for_slot", "candidate_reserved",
        "enqueue_response_ambiguous", "ambiguous_enqueue_response",
    }:
        return False
    return cached_safe_slskd_candidate_count(item) > 0


def no_actionable_source_retry_delay(item, exhaustion_cycles=DEFAULT_EXHAUSTION_CYCLES, *, base_retry_seconds=DEFAULT_RETRY_SECONDS):
    try:
        attempts = int(item.get("source_ladder_attempt_count") or 0)
    except (TypeError, ValueError):
        attempts = 0
    try:
        threshold = max(1, int(exhaustion_cycles or DEFAULT_EXHAUSTION_CYCLES))
    except (TypeError, ValueError):
        threshold = DEFAULT_EXHAUSTION_CYCLES
    try:
        base_retry_seconds = max(0, int(base_retry_seconds or DEFAULT_RETRY_SECONDS))
    except (TypeError, ValueError):
        base_retry_seconds = DEFAULT_RETRY_SECONDS
    if missing_required_source_result_sources(item):
        return base_retry_seconds
    if attempts < threshold:
        return base_retry_seconds
    delay = max(base_retry_seconds, NO_ACTIONABLE_SOURCE_RETRY_SECONDS)
    for attempt_threshold, retry_seconds in EXTENDED_SOURCE_RETRY_STEPS:
        if attempts >= attempt_threshold:
            delay = max(delay, retry_seconds)
            break
    return delay


def repeated_source_retry_should_cooldown(item, now, *, missing_source_results=None, exhaustion_cycles=DEFAULT_EXHAUSTION_CYCLES):
    if not isinstance(item, dict):
        return False
    if item.get("state") in {"downloading", "importing", "verified", "needs_you"}:
        return False
    if item.get("current_source"):
        return False
    if not retry_due_now(item, now=now):
        return False
    if missing_source_results is None:
        missing_source_results = missing_required_source_result_sources(item)
    if missing_source_results:
        return False
    if has_cached_safe_slskd_candidate(item):
        return False
    if slskd_user_load_limited(item):
        return False
    if slskd_transient_retry_pending(item, now) or slskd_transient_checked_result(item):
        return False
    try:
        attempts = int(item.get("source_ladder_attempt_count") or 0)
    except (TypeError, ValueError):
        attempts = 0
    try:
        threshold = max(1, int(exhaustion_cycles or DEFAULT_EXHAUSTION_CYCLES))
    except (TypeError, ValueError):
        threshold = DEFAULT_EXHAUSTION_CYCLES
    if attempts < threshold:
        return False
    return bool(
        no_actionable_source_result(item)
        or low_confidence_slskd_result(item)
        or automatic_source_retry_event(item)
        or unsafe_or_low_confidence_event(item)
    )


def provider_retry_source_from_event(item):
    if not isinstance(item, dict):
        return ""
    event = str(item.get("last_event") or "").strip().lower()
    event_markers = (
        ("slskd", ("slskd source errored", "slskd source timed out", "slskd download api hiccup")),
        ("rss", ("rss source errored", "rss source timed out", "rss timed out")),
        ("prowlarr", ("prowlarr source errored", "prowlarr source timed out", "prowlarr timed out")),
        ("comicscodes", ("comicscodes source errored", "comicscodes source timed out", "comicscodes timed out")),
        ("mangadex", ("mangadex source errored", "mangadex source timed out", "mangadex timed out")),
    )
    for source, markers in event_markers:
        if any(marker in event for marker in markers):
            return source
    last_source = source_order_attempt_key(item.get("last_source_error_source"))
    if last_source:
        return last_source
    for attempt in reversed(item.get("attempts") or []):
        if not isinstance(attempt, dict):
            continue
        source = source_order_attempt_key(
            attempt.get("source")
            or attempt.get("provider_id")
            or attempt.get("provider")
        )
        if not source:
            continue
        status = str(attempt.get("status") or "").strip().lower()
        kind = str(attempt.get("kind") or "").strip().lower()
        reason = str(attempt.get("reason") or attempt.get("failure_reason") or "").strip().lower()
        if kind in {"source_error", "source_started_timeout"}:
            return source
        if status in {"error", "timeout", "api_error", "probe_error", "retry_scheduled"} and any(
            marker in reason
            for marker in ("timed out", "timeout", "errored", "error", "api hiccup")
        ):
            return source
    return ""


def provider_retry_kind_from_event(item):
    text = " ".join(
        str((item or {}).get(key) or "")
        for key in ("last_event", "last_source_error", "last_source_busy_reason")
    ).lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "busy" in text:
        return "busy"
    if "api hiccup" in text:
        return "api_hiccup"
    return "error"


def provider_transient_retry_base_seconds(source):
    source = str(source or "").strip().lower()
    if source == "slskd":
        return SLSKD_TRANSIENT_RETRY_SECONDS
    if source == "queue":
        return MIN_BUDGET_RETRY_SECONDS
    return DEFAULT_RETRY_SECONDS


def provider_transient_retry_delay(item, source, *, base_seconds=None, missing_source_results=None):
    source = str(source or "").strip().lower()
    if base_seconds is None:
        base_seconds = provider_transient_retry_base_seconds(source)
    try:
        delay = max(60, int(base_seconds or provider_transient_retry_base_seconds(source)))
    except (TypeError, ValueError):
        delay = provider_transient_retry_base_seconds(source)
    if missing_source_results is None:
        missing_source_results = missing_required_source_result_sources(item)
    if missing_source_results:
        return delay
    try:
        attempts = int((item or {}).get("source_ladder_attempt_count") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if attempts >= 30:
        return max(delay, 6 * 60 * 60)
    if attempts >= 18:
        return max(delay, 3 * 60 * 60)
    if attempts >= 12:
        return max(delay, 60 * 60)
    if attempts >= DEFAULT_EXHAUSTION_CYCLES:
        return max(delay, DEFAULT_RETRY_SECONDS)
    return delay


def provider_retry_should_cooldown(item, now, *, source=None, missing_source_results=None):
    if not isinstance(item, dict):
        return False
    if item.get("state") in {"downloading", "importing", "verified", "needs_you"}:
        return False
    if item.get("current_source"):
        return False
    if not retry_due_now(item, now=now):
        return False
    source = str(source or provider_retry_source_from_event(item) or "").strip().lower()
    if source not in SOURCE_PROVIDER_IDS:
        return False
    if missing_source_results is None:
        missing_source_results = missing_required_source_result_sources(item)
    if missing_source_results:
        return False
    if has_cached_safe_slskd_candidate(item):
        return False
    if slskd_user_load_limited(item):
        return False
    return True


def mark_provider_retry_cooldown(item, source, now, *, set_timer=True, missing_source_results=None, base_seconds=None):
    source = str(source or provider_retry_source_from_event(item) or "source").strip().lower()
    label = public_source_name(source) or source
    retry_kind = provider_retry_kind_from_event(item)
    retry_delay = provider_transient_retry_delay(
        item,
        source,
        base_seconds=base_seconds,
        missing_source_results=missing_source_results,
    )
    item["state"] = "queued"
    item["current_source"] = None
    if retry_kind == "timeout":
        event = f"{label} source timed out; automatic retry scheduled"
    elif retry_kind == "busy":
        event = f"{label} source busy; automatic retry scheduled"
    elif retry_kind == "api_hiccup" and source == "slskd":
        event = "SLSKD download API hiccup; retry scheduled"
    else:
        event = f"{label} source errored; automatic retry scheduled"
    item["last_event"] = event
    item["provider_retry_deferred_source"] = source
    item["provider_retry_deferred_at"] = now
    item["provider_retry_deferred_at_iso"] = now_iso(now)
    item["provider_retry_delay_seconds"] = retry_delay
    item.pop("needs_you_reason", None)
    if set_timer:
        schedule_retry_after(item, now, retry_delay)
    append_unique_queue_attempt(
        item,
        {
            "ts": now,
            "ts_iso": now_iso(now),
            "source": source,
            "provider": label,
            "provider_id": source,
            "status": "retry_scheduled",
            "lifecycle_phase": "retry_later",
            "reason": event,
            "failure_reason": event,
            "kind": "provider_retry_cooldown",
            "title": " ".join(
                str(value or "").strip()
                for value in (item.get("series"), item.get("issue"))
                if str(value or "").strip()
            ),
            "query": item.get("query"),
            "retry_delay_seconds": retry_delay,
        },
    )
    return retry_delay


def mark_retry_planner_deferral(item, now, reason):
    item["retry_planner_deferred_at"] = now
    item["retry_planner_deferred_at_iso"] = now_iso(now)
    item["retry_planner_deferred_reason"] = str(reason or "repeated_source_retry")


def schedule_retry_after(item, now, delay_seconds):
    try:
        delay_seconds = max(0, int(delay_seconds or 0))
    except (TypeError, ValueError):
        delay_seconds = DEFAULT_RETRY_SECONDS
    retry_after = now + delay_seconds
    existing_retry_after = retry_after_ts(item)
    if existing_retry_after > now and existing_retry_after <= retry_after:
        return existing_retry_after
    item["retry_after"] = retry_after
    item["retry_after_iso"] = now_iso(retry_after)
    return retry_after


def source_error_retry_delay(source, args, item=None):
    source = str(source or "").strip().lower()
    if source == "slskd":
        return provider_transient_retry_delay(item, source, base_seconds=SLSKD_TRANSIENT_RETRY_SECONDS)
    try:
        base_seconds = max(60, int(getattr(args, "retry_seconds", DEFAULT_RETRY_SECONDS) or DEFAULT_RETRY_SECONDS))
    except (TypeError, ValueError):
        base_seconds = DEFAULT_RETRY_SECONDS
    return provider_transient_retry_delay(item, source, base_seconds=base_seconds)


def mark_source_error_retry(item, source, error, now, args):
    source = str(source or "source").strip().lower()
    label = public_source_name(source) or source
    retry_delay = source_error_retry_delay(source, args, item=item)
    item["state"] = "queued"
    item["current_source"] = None
    item["last_source_error_source"] = source
    item["last_source_error"] = str(error or "").strip()
    item["last_source_error_at"] = now
    item["last_source_error_at_iso"] = now_iso(now)
    item["source_error_retry_at"] = now
    item["source_error_retry_at_iso"] = now_iso(now)
    item["last_event"] = f"{label} source errored; automatic retry scheduled"
    item.pop("needs_you_reason", None)
    schedule_retry_after(item, now, retry_delay)
    attempt = {
        "ts": now,
        "ts_iso": now_iso(now),
        "source": source,
        "status": "error",
        "reason": str(error or f"{label} source errored").strip(),
        "kind": "source_error",
        "title": " ".join(
            str(value or "").strip()
            for value in (item.get("series"), item.get("issue"))
            if str(value or "").strip()
        ),
        "query": item.get("query"),
    }
    append_unique_queue_attempt(item, attempt)


def mark_source_busy_retry(item, source, reason, now, args):
    source = str(source or "source").strip().lower()
    label = public_source_name(source) or source
    retry_delay = SLSKD_USER_LOAD_RETRY_SECONDS if source == "slskd" else source_error_retry_delay(source, args, item=item)
    item["state"] = "queued"
    item["current_source"] = None
    item["last_source_busy_source"] = source
    item["last_source_busy_reason"] = str(reason or "").strip()
    item["last_source_busy_at"] = now
    item["last_source_busy_at_iso"] = now_iso(now)
    item["source_busy_retry_at"] = now
    item["source_busy_retry_at_iso"] = now_iso(now)
    item["last_event"] = f"{label} source busy; automatic retry scheduled"
    item.pop("needs_you_reason", None)
    schedule_retry_after(item, now, retry_delay)
    attempt = {
        "ts": now,
        "ts_iso": now_iso(now),
        "source": source,
        "status": "busy",
        "reason": str(reason or f"{label} source busy").strip(),
        "kind": "source_busy",
        "title": " ".join(
            str(value or "").strip()
            for value in (item.get("series"), item.get("issue"))
            if str(value or "").strip()
        ),
        "query": item.get("query"),
    }
    append_unique_queue_attempt(item, attempt)


def source_error_retry_fresh(item, now, window_seconds=300):
    if not isinstance(item, dict):
        return False
    if item.get("state") != "queued":
        return False
    ts = numeric_timestamp(item.get("source_error_retry_at"))
    return ts > 0 and now - ts <= window_seconds


def source_busy_retry_fresh(item, now, window_seconds=300):
    if not isinstance(item, dict):
        return False
    if item.get("state") != "queued":
        return False
    ts = numeric_timestamp(item.get("source_busy_retry_at"))
    return ts > 0 and now - ts <= window_seconds


def mark_no_actionable_source_retry(item, now, *, set_timer=True):
    normalize_slskd_attempt_marker(item, now)
    retry_delay = no_actionable_source_retry_delay(item)
    item["state"] = "queued"
    item["current_source"] = None
    item["last_event"] = (
        "automatic sources had no actionable candidate; extended retry scheduled"
        if retry_delay > NO_ACTIONABLE_SOURCE_RETRY_SECONDS
        else "automatic sources had no actionable candidate; retry scheduled"
    )
    item["no_actionable_source_at"] = now
    item["no_actionable_source_at_iso"] = now_iso(now)
    item.pop("needs_you_reason", None)
    if set_timer:
        schedule_retry_after(item, now, retry_delay)


def mark_extended_source_ladder_retry(item, now, *, set_timer=True):
    retry_delay = no_actionable_source_retry_delay(item)
    item["state"] = "queued"
    item["current_source"] = None
    if unsafe_or_low_confidence_event(item):
        item["last_event"] = slskd_no_safe_candidate_event(
            item,
            extended=retry_delay > NO_ACTIONABLE_SOURCE_RETRY_SECONDS,
        )
    elif no_actionable_source_result(item):
        item["last_event"] = (
            "automatic sources had no actionable candidate; extended retry scheduled"
            if retry_delay > NO_ACTIONABLE_SOURCE_RETRY_SECONDS
            else "automatic sources had no actionable candidate; retry scheduled"
        )
    else:
        item["last_event"] = (
            "automatic sources exhausted; extended retry scheduled"
            if retry_delay > NO_ACTIONABLE_SOURCE_RETRY_SECONDS
            else "automatic sources exhausted; retry scheduled"
        )
    item["automatic_source_retry_at"] = now
    item["automatic_source_retry_at_iso"] = now_iso(now)
    item.pop("needs_you_reason", None)
    if set_timer:
        schedule_retry_after(item, now, retry_delay)


def mark_low_confidence_slskd_retry(item, now, *, set_timer=True):
    normalize_slskd_attempt_marker(item, now)
    item["state"] = "queued"
    item["current_source"] = None
    item["last_event"] = "SLSKD candidates were not confident enough to auto-pick; retry scheduled"
    item["low_confidence_slskd_at"] = now
    item["low_confidence_slskd_at_iso"] = now_iso(now)
    item.pop("needs_you_reason", None)
    if set_timer:
        existing_retry_after = retry_after_ts(item)
        if existing_retry_after <= now:
            schedule_retry_after(item, now, DEFAULT_RETRY_SECONDS)


def normalize_waiting_retry_state(item, now):
    if not isinstance(item, dict):
        return False
    if item.get("state") in {"downloading", "importing", "verified", "needs_you"}:
        return False
    if item.get("current_source"):
        return False
    if slskd_source_result_reprobe_due(item, now=now):
        item["state"] = "queued"
        item["current_source"] = None
        item["last_event"] = "stale empty SLSKD result queued for bounded automatic reprobe"
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        item["slskd_result_reprobe_due_at"] = now
        item["slskd_result_reprobe_due_at_iso"] = now_iso(now)
        item["retry_waiting_normalized_at"] = now
        item["retry_waiting_normalized_at_iso"] = now_iso(now)
        return True
    if slskd_transient_retry_pending(item, now):
        item["state"] = "queued"
        item["current_source"] = None
        item["last_event"] = "SLSKD download API hiccup; retry scheduled"
        item.pop("needs_you_reason", None)
        item["retry_waiting_normalized_at"] = now
        item["retry_waiting_normalized_at_iso"] = now_iso(now)
        return True
    if slskd_transient_checked_result(item):
        missing_source_results = missing_required_source_result_sources(item)
        retry_delay = provider_transient_retry_delay(
            item,
            "slskd",
            base_seconds=SLSKD_TRANSIENT_RETRY_SECONDS,
            missing_source_results=missing_source_results,
        )
        item["state"] = "queued"
        item["current_source"] = None
        item["last_event"] = "SLSKD source errored; automatic retry scheduled"
        schedule_retry_after(item, now, retry_delay)
        if retry_delay > SLSKD_TRANSIENT_RETRY_SECONDS and not missing_source_results:
            item["provider_retry_deferred_source"] = "slskd"
            item["provider_retry_deferred_at"] = now
            item["provider_retry_deferred_at_iso"] = now_iso(now)
            item["provider_retry_delay_seconds"] = retry_delay
            mark_retry_planner_deferral(item, now, "provider_transient_retry")
        item.pop("needs_you_reason", None)
        item["retry_waiting_normalized_at"] = now
        item["retry_waiting_normalized_at_iso"] = now_iso(now)
        return True
    if slskd_user_load_limited(item):
        item["state"] = "queued"
        item["current_source"] = None
        item["last_event"] = "SLSKD candidate ready; waiting for transfer slot"
        retry_after = retry_after_ts(item)
        next_retry = now + SLSKD_USER_LOAD_RETRY_SECONDS
        if retry_after <= 0 or retry_after <= now or retry_after > next_retry:
            item["retry_after"] = next_retry
            item["retry_after_iso"] = now_iso(next_retry)
        item.pop("needs_you_reason", None)
        item["retry_waiting_normalized_at"] = now
        item["retry_waiting_normalized_at_iso"] = now_iso(now)
        return True
    if has_due_cached_slskd_autopick(item, now=now):
        item["state"] = "searching"
        item["current_source"] = None
        if item.get("last_event") not in SLSKD_AUTOPICK_SIGNAL_EVENTS and "safe alternate" not in str(item.get("last_event") or "").lower():
            item["last_event"] = "SLSKD candidates available for autopick"
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        item["retry_waiting_normalized_at"] = now
        item["retry_waiting_normalized_at_iso"] = now_iso(now)
        return True
    retry_after = retry_after_ts(item)
    no_actionable = no_actionable_source_result(item)
    low_confidence = low_confidence_slskd_result(item)
    automatic_retry_event = automatic_source_retry_event(item)
    missing_source_results = update_pending_source_result_markers(item)
    provider_retry_source = provider_retry_source_from_event(item)
    provider_retry_cooldown = provider_retry_should_cooldown(
        item,
        now,
        source=provider_retry_source,
        missing_source_results=missing_source_results,
    )
    try:
        source_ladder_attempts = int(item.get("source_ladder_attempt_count") or 0)
    except (TypeError, ValueError):
        source_ladder_attempts = 0
    extended_source_retry = bool(
        automatic_retry_event
        and source_ladder_attempts >= DEFAULT_EXHAUSTION_CYCLES
        and (retry_after <= now or retry_after <= 0)
        and not missing_source_results
    )
    repeated_retry_cooldown = repeated_source_retry_should_cooldown(
        item,
        now,
        missing_source_results=missing_source_results,
    )
    stranded_search = item.get("state") == "searching" and retry_after <= now
    if (
        not no_actionable
        and not low_confidence
        and not extended_source_retry
        and not provider_retry_cooldown
        and not stranded_search
        and retry_after <= now
    ):
        return False
    if no_actionable:
        mark_no_actionable_source_retry(
            item,
            now,
            set_timer=repeated_retry_cooldown or not preserve_due_retry_timer(item, now),
        )
    elif low_confidence:
        mark_low_confidence_slskd_retry(
            item,
            now,
            set_timer=repeated_retry_cooldown or not preserve_due_retry_timer(item, now),
        )
    elif extended_source_retry:
        mark_extended_source_ladder_retry(
            item,
            now,
            set_timer=repeated_retry_cooldown or not preserve_due_retry_timer(item, now),
        )
    elif provider_retry_cooldown:
        mark_provider_retry_cooldown(
            item,
            provider_retry_source,
            now,
            missing_source_results=missing_source_results,
        )
    elif stranded_search:
        item["state"] = "queued"
        item.setdefault("last_event", "queued for source ladder retry")
        item.pop("needs_you_reason", None)
    else:
        item["state"] = "queued"
        item["last_event"] = "source ladder attempted; retry scheduled"
        item.pop("needs_you_reason", None)
    if repeated_retry_cooldown:
        mark_retry_planner_deferral(item, now, "repeated_source_ladder_no_action")
    elif provider_retry_cooldown:
        mark_retry_planner_deferral(item, now, "provider_transient_retry")
    item["retry_waiting_normalized_at"] = now
    item["retry_waiting_normalized_at_iso"] = now_iso(now)
    return True


def mark_historical_slskd_no_action(item, status, now):
    normalize_slskd_attempt_marker(item, now)
    item["state"] = "searching"
    item["current_source"] = None
    item["last_event"] = (
        "historical SLSKD search had no candidate; queued for autopilot"
        if str(status or "") == "searched_no_candidates"
        else slskd_no_automatic_candidate_event(status)
    )
    item["historical_slskd_no_action_at"] = now
    item["historical_slskd_no_action_at_iso"] = now_iso(now)
    item.pop("needs_you_reason", None)


def mark_automation_exhausted(item, now, *, source="annotation"):
    try:
        candidate_count = int(item.get("last_slskd_candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    try:
        safe_count = int(item.get("last_slskd_auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        safe_count = 0
    item["state"] = "queued"
    item["current_source"] = None
    item.pop("needs_you_reason", None)
    try:
        exhausted_count = int(item.get("automation_exhausted_count") or 0)
    except (TypeError, ValueError):
        exhausted_count = 0
    item["automation_exhausted_count"] = exhausted_count + 1
    retry_delay = no_actionable_source_retry_delay(item)
    if candidate_count > 0 and safe_count <= 0:
        item["last_event"] = slskd_no_safe_candidate_event(
            item,
            extended=retry_delay > NO_ACTIONABLE_SOURCE_RETRY_SECONDS,
        )
    else:
        item["last_event"] = (
            "automatic sources exhausted; extended retry scheduled"
            if retry_delay > NO_ACTIONABLE_SOURCE_RETRY_SECONDS
            else "automatic sources exhausted; retry scheduled"
        )
    item["automation_exhausted_at"] = now
    item["automation_exhausted_at_iso"] = now_iso(now)
    item["automation_exhausted_source"] = source
    schedule_retry_after(item, now, retry_delay)
    item.setdefault("attempts", []).append({
        "ts": now,
        "ts_iso": now_iso(now),
        "source": "autopilot",
        "status": "automation_exhausted_retry_scheduled",
        "reason": "low_confidence_slskd_candidates" if candidate_count > 0 and safe_count <= 0 else "sources_exhausted",
    })


def release_automation_exhausted_for_retry(item, now):
    if item.get("state") != "needs_you":
        return False
    if item.get("needs_you_reason") != "automation_exhausted":
        return False
    try:
        generation = int(item.get("automation_retry_generation") or 0)
    except (TypeError, ValueError):
        generation = 0
    no_actionable = no_actionable_source_result(item)
    if generation >= AUTOMATION_RETRY_GENERATION and not no_actionable:
        return False
    if item.get("present_in_watch") is False:
        return False
    item["state"] = "queued"
    item["current_source"] = None
    item["automation_retry_generation"] = AUTOMATION_RETRY_GENERATION
    item["automation_retry_at"] = now
    item["automation_retry_at_iso"] = now_iso(now)
    item["previous_source_ladder_attempt_count"] = item.get("source_ladder_attempt_count")
    item["source_ladder_attempt_count"] = 0
    if no_actionable:
        retry_after = now + NO_ACTIONABLE_SOURCE_RETRY_SECONDS
        item["retry_after"] = retry_after
        item["retry_after_iso"] = now_iso(retry_after)
        item["last_event"] = "automatic sources had no actionable candidate; retry scheduled"
    else:
        item["last_event"] = "automation policy changed; retrying source ladder"
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
    item.setdefault("attempts", []).append({
        "ts": now,
        "ts_iso": now_iso(now),
        "source": "autopilot",
        "status": "automation_exhaustion_released_no_candidate" if no_actionable else "automation_exhaustion_released",
        "generation": AUTOMATION_RETRY_GENERATION,
    })
    item.pop("needs_you_reason", None)
    return True


def series_summary_identity(item):
    identity = item.get("queue_identity") or queue_identity(
        watch_id=item.get("watch_id"),
        kapowarr_id=item.get("kapowarr_id"),
        comicvine_id=item.get("comicvine_id"),
    )
    return identity or ""


def series_summary_display_name(item, duplicate_title=False):
    name = item.get("series") or "Unknown"
    if not duplicate_title:
        return name
    bits = []
    if item.get("watch_publisher"):
        bits.append(str(item.get("watch_publisher")))
    if item.get("watch_year"):
        bits.append(str(item.get("watch_year")))
    if not bits and item.get("kapowarr_id") not in (None, ""):
        bits.append(f"Kapowarr {item.get('kapowarr_id')}")
    return f"{name} ({', '.join(bits)})" if bits else name


def manual_review_index(queue=None, *, target_index=None, deadline=None):
    index = {}
    for row in read_manual_review_items():
        if deadline is not None and time.time() >= deadline:
            break
        series = row.get("series")
        issue = row.get("issue")
        if not series or issue is None:
            continue
        targets = row_queue_targets(
            queue,
            row,
            include_inactive=True,
            target_index=target_index,
        ) if queue else []
        target_keys = [key for key, _ in targets] or [legacy_queue_key(series, issue)]
        for key in target_keys:
            index.setdefault(key, []).append(row)
    return index


def slskd_cache_entry_rank(row):
    if not isinstance(row, dict):
        return (0, 0, 0, 0)
    try:
        safe_count = int(row.get("auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        safe_count = 0
    try:
        candidate_count = int(row.get("candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    try:
        detected_count = int(row.get("detected_count") or 0)
    except (TypeError, ValueError):
        detected_count = 0
    checked_at = slskd_row_checked_at(row)
    if stale_slskd_detected_probe_row(row, time.time()):
        detected_count = 0
    return (safe_count, candidate_count, detected_count, checked_at)


def load_slskd_probe_module():
    if not SLSKD_SOURCE_PROBE_MODULE_SCRIPT.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("inkdrop_slskd_probe", str(SLSKD_SOURCE_PROBE_MODULE_SCRIPT))
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        log("slskd_probe_module_load_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def refreshed_slskd_cache_entry(probe, entry, queue_item=None):
    if not isinstance(entry, dict):
        return None
    row = dict(entry)
    if queue_item:
        context = dict(queue_item)
        context.update(row)
    else:
        context = row
    if probe and hasattr(probe, "refresh_cached_candidate_verdicts"):
        try:
            row, _changed = probe.refresh_cached_candidate_verdicts(row, item=context)
        except Exception as exc:
            log(
                "slskd_cache_verdict_refresh_failed",
                review_id=row.get("review_id"),
                series=row.get("series"),
                issue=row.get("issue"),
                error=f"{type(exc).__name__}: {exc}",
            )
    return row


def slskd_index(
    queue=None,
    *,
    refresh_cached_verdicts=True,
    deadline=None,
    max_refresh_entries=None,
    target_index=None,
    budget_state=None,
):
    status = read_json(SLSKD_SOURCE_PROBE_STATUS_FILE, {}) or {}
    cache = read_json(SLSKD_SOURCE_PROBE_CACHE_FILE, {}) or {}
    probe = load_slskd_probe_module() if refresh_cached_verdicts and isinstance(cache, dict) and cache else None
    index = {}
    refreshed_count = 0

    def deadline_reached():
        reached = bool(deadline is not None and time.time() >= deadline)
        if reached and isinstance(budget_state, dict):
            budget_state["slskd_index_deadline_reached"] = True
        return reached

    def add_row(row, targets=None):
        if not isinstance(row, dict):
            return
        series = row.get("series")
        issue = row.get("issue")
        if not series or issue is None:
            return
        targets = targets if targets is not None else (
            row_queue_targets(
                queue,
                row,
                include_inactive=True,
                target_index=target_index,
            ) if queue else []
        )
        target_keys = [key for key, _ in targets] or [legacy_queue_key(series, issue)]
        for key in target_keys:
            existing = index.get(key)
            if not existing or slskd_cache_entry_rank(row) >= slskd_cache_entry_rank(existing):
                index[key] = row

    def should_refresh_cached_entry():
        nonlocal refreshed_count
        if not refresh_cached_verdicts:
            return False
        if deadline is not None and time.time() >= deadline:
            return False
        if max_refresh_entries is not None:
            try:
                limit = int(max_refresh_entries)
            except (TypeError, ValueError):
                limit = 0
            if limit > 0 and refreshed_count >= limit:
                return False
        refreshed_count += 1
        return True

    for review_id, item in (status.get("items") or {}).items():
        if deadline_reached():
            break
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["review_id"] = review_id
        add_row(row)
    if isinstance(cache, dict):
        for review_id, entry in cache.items():
            if deadline_reached():
                break
            if not isinstance(entry, dict):
                continue
            base_row = dict(entry)
            base_row["review_id"] = str(base_row.get("review_id") or review_id)
            targets = row_queue_targets(
                queue,
                base_row,
                include_inactive=True,
                target_index=target_index,
            ) if queue else []
            queue_item = targets[0][1] if targets else None
            if should_refresh_cached_entry():
                row = refreshed_slskd_cache_entry(probe, base_row, queue_item=queue_item)
            else:
                row = base_row
            if row:
                row["review_id"] = str(row.get("review_id") or review_id)
                add_row(row, targets=targets)
    return index


def enrich_slskd_checked_row(row):
    if not isinstance(row, dict):
        return row
    try:
        detected_count = int(row.get("detected_count") or 0)
    except (TypeError, ValueError):
        detected_count = 0
    if detected_count <= 0 or row.get("detected_files"):
        return row
    review_id = str(row.get("review_id") or "").strip()
    queue_key = str(row.get("autopilot_queue_key") or row.get("queue_key") or "").strip()
    if not review_id and not queue_key:
        return row

    candidates = []
    status = read_json(SLSKD_SOURCE_PROBE_STATUS_FILE, {}) or {}
    for key, value in (status.get("items") or {}).items():
        if isinstance(value, dict):
            candidate = dict(value)
            candidate.setdefault("review_id", str(key))
            candidates.append(candidate)
    cache = read_json(SLSKD_SOURCE_PROBE_CACHE_FILE, {}) or {}
    if isinstance(cache, dict):
        for key, value in cache.items():
            if isinstance(value, dict):
                candidate = dict(value)
                candidate.setdefault("review_id", str(key))
                candidates.append(candidate)

    for candidate in candidates:
        candidate_review_id = str(candidate.get("review_id") or "").strip()
        candidate_queue_key = str(candidate.get("autopilot_queue_key") or candidate.get("queue_key") or "").strip()
        if not (
            (review_id and candidate_review_id == review_id)
            or (queue_key and candidate_queue_key == queue_key)
        ):
            continue
        detected_files = candidate.get("detected_files")
        if not isinstance(detected_files, list) or not detected_files:
            continue
        enriched = dict(row)
        enriched["detected_files"] = detected_files
        for key in ("checked_at", "checked_at_iso", "staged_scan_at", "staged_scan_at_iso"):
            if enriched.get(key) in (None, "") and candidate.get(key) not in (None, ""):
                enriched[key] = candidate.get(key)
        return enriched
    return row


def reconciliation_rows():
    if not IMPORTED_DB.exists():
        return []
    con = connect_imported_db()
    try:
        exists = con.execute(
            "select name from sqlite_master where type='table' and name='download_reconciliation'"
        ).fetchone()
        if not exists:
            return []
        return [
            dict(row)
            for row in con.execute(
                """
                select lifecycle_state, reason, matched_series, title, query, matched_local_path,
                       client, client_id, updated_at
                from download_reconciliation
                where lifecycle_state in (
                    'queued', 'downloading', 'stalled_downloading',
                    'completed_in_client', 'ready_to_import', 'waiting_for_library_scan', 'waiting_for_kavita_scan',
                    'failed_download', 'bad_archive', 'false_positive', 'stale_no_local_file', 'wrong_series_or_subseries'
                )
                """
            )
        ]
    except sqlite3.Error as exc:
        log("reconciliation_read_failed", error=f"{type(exc).__name__}: {exc}")
        return []
    finally:
        con.close()


def reconciliation_series_matches(row, item):
    series = normalize(item.get("series") or "")
    if not series:
        return False
    matched_series = normalize(row.get("matched_series") or "")
    if matched_series and (matched_series == series or series in matched_series or matched_series in series):
        return True
    query = normalize(row.get("query") or "")
    if query and (query == series or query.startswith(series + " ")):
        return True
    title = normalize(row.get("title") or "")
    if title and (title == series or title.startswith(series + " ")):
        return True
    path = normalize(row.get("matched_local_path") or "")
    return bool(path and f" {series} " in f" {path} ")


def reconciliation_issue_text_values(row):
    values = [row.get("query"), row.get("title")]
    local_path = row.get("matched_local_path")
    if local_path:
        state = str(row.get("lifecycle_state") or "")
        if state in {"completed_in_client", "ready_to_import", "waiting_for_library_scan", "waiting_for_kavita_scan"}:
            values.append(Path(str(local_path)).name)
        else:
            values.append(local_path)
    return values


def reconciliation_issue_matches(row, item):
    wanted = issue_number_keys(item.get("issue"))
    if not wanted:
        return False
    for value in reconciliation_issue_text_values(row):
        if wanted & issue_number_keys_in_text(value):
            return True
    return False


def reconciliation_index(queue, *, deadline=None):
    rows = reconciliation_rows()
    if not rows:
        return {}
    items = [
        item for item in (queue.get("items") or {}).values()
        if isinstance(item, dict) and item.get("present_in_watch", True)
    ]
    index = {}
    rank = {
        "waiting_for_library_scan": 5,
        "waiting_for_kavita_scan": 5,
        "ready_to_import": 4,
        "completed_in_client": 3,
        "stalled_downloading": 2,
        "downloading": 2,
        "queued": 1,
        "failed_download": 1,
        "bad_archive": 1,
        "false_positive": 1,
        "stale_no_local_file": 1,
        "wrong_series_or_subseries": 1,
    }
    for row in rows:
        if deadline is not None and time.time() >= deadline:
            break
        for item in items:
            if deadline is not None and time.time() >= deadline:
                break
            if not reconciliation_series_matches(row, item):
                continue
            if not reconciliation_issue_matches(row, item):
                continue
            key = item.get("key") or queue_key(item.get("series"), item.get("issue"))
            existing = index.get(key)
            if not existing or rank.get(row.get("lifecycle_state"), 0) > rank.get(existing.get("lifecycle_state"), 0):
                index[key] = row
    return index


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
    kavita_path = kavita_path_for_host_path(path)
    if not kavita_path or not KAVITA_DB.exists():
        return False
    con = sqlite3.connect(KAVITA_DB)
    try:
        return bool(con.execute("select 1 from MangaFile where FilePath = ? limit 1", (kavita_path,)).fetchone())
    finally:
        con.close()


def kavita_file_records_for_host_path(path):
    try:
        if not Path(str(path)).exists():
            return []
    except OSError:
        return []
    kavita_path = kavita_path_for_host_path(path)
    if not kavita_path or not KAVITA_DB.exists():
        return []
    con = sqlite3.connect(KAVITA_DB)
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in con.execute(
                """
                select
                    s.Name as series,
                    c.Number as number,
                    c.Range as chapter_range,
                    mf.FilePath as file_path,
                    mf.FileName as file_name
                from MangaFile mf
                join Chapter c on mf.ChapterId = c.Id
                join Volume v on c.VolumeId = v.Id
                join Series s on v.SeriesId = s.Id
                where mf.FilePath = ?
                """,
                (kavita_path,),
            ).fetchall()
        ]
    finally:
        con.close()


COLLECTION_TARGET_PATTERN = re.compile(
    r"\b(?:omnibus|library\s+edition|complete\s+collection|collection|treasury)\b",
    re.I,
)
SINGLE_PART_SOURCE_PATTERN = re.compile(r"\b(?:part|pt|chapter|chap|ch|issue)\s*0*\d+\b", re.I)


def collection_guard_text(*values):
    return " ".join(str(value or "") for value in values if value not in (None, "")).strip()


def collection_guard_normalized_text(*values):
    text = collection_guard_text(*values)
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def collection_target_single_part_block_reason(item, *source_values):
    item = item if isinstance(item, dict) else {}
    target_text = collection_guard_text(
        item.get("series"),
        item.get("query"),
        item.get("issue_title"),
        item.get("title"),
    )
    target_norm = collection_guard_normalized_text(target_text)
    issue_title_norm = collection_guard_normalized_text(item.get("issue_title"))
    target_is_collection = bool(COLLECTION_TARGET_PATTERN.search(target_text)) or issue_title_norm in {
        "tpb",
        "trade paperback",
        "hc",
        "hardcover",
    }
    if not target_is_collection:
        return ""
    source_text = collection_guard_text(*source_values)
    if not source_text:
        return ""
    source_norm = collection_guard_normalized_text(source_text)
    if SINGLE_PART_SOURCE_PATTERN.search(source_norm):
        return "single_part_file_does_not_satisfy_collection_target"
    if COLLECTION_TARGET_PATTERN.search(source_text):
        return ""
    if "omnibus" in target_norm and "omnibus" not in source_norm and SINGLE_PART_SOURCE_PATTERN.search(source_norm):
        return "single_part_file_does_not_satisfy_omnibus_target"
    return ""


def kavita_file_visible_for_item(item, path):
    if collection_target_single_part_block_reason(item, path):
        return False
    records = kavita_file_records_for_host_path(path)
    if not records:
        return False
    item_numbers = issue_number_keys((item or {}).get("issue"))
    if not item_numbers:
        return True
    for record in records:
        row_numbers = kavita_record_issue_numbers(record, record.get("file_path") or record.get("file_name") or path)
        if item_numbers & row_numbers:
            return True
    return False


def kavita_file_verified_for_item(item, path):
    if collection_target_single_part_block_reason(item, path):
        return False
    records = kavita_file_records_for_host_path(path)
    if not records:
        return False
    item_numbers = issue_number_keys((item or {}).get("issue"))
    item_series = (item or {}).get("series")
    for record in records:
        record_series = record.get("series")
        if item_series and record_series and not series_names_match(item_series, record_series):
            continue
        row_numbers = kavita_record_issue_numbers(record, record.get("file_path") or record.get("file_name") or path)
        if item_numbers and not (item_numbers & row_numbers):
            continue
        return True
    return False


def resolved_record_verified_for_item(item, resolved_row):
    if not isinstance(resolved_row, dict):
        return False
    if resolved_row.get("series") and item.get("series") and not series_names_match(resolved_row.get("series"), item.get("series")):
        return False
    if resolved_row.get("issue") is not None:
        if not (issue_number_keys(resolved_row.get("issue")) & issue_number_keys(item.get("issue"))):
            return False
    destinations = resolved_row.get("destinations") if isinstance(resolved_row.get("destinations"), list) else []
    if any(kavita_file_verified_for_item(item, dest) for dest in destinations):
        return True
    verification = resolved_row.get("verification") if isinstance(resolved_row.get("verification"), dict) else {}
    for row in verification.get("checked") or []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("verification_status") or "").strip().lower()
        if status not in {"folder_verified", "library_visible", "kavita_verified"}:
            continue
        row_dest = row.get("dest")
        if row_dest:
            if status == "folder_verified":
                if not Path(str(row_dest)).exists():
                    continue
            elif not kavita_file_verified_for_item(item, row_dest):
                continue
        else:
            row_numbers = kavita_record_issue_numbers(row, row.get("file_path") or row.get("file_name") or "")
            if not row_numbers or not (issue_number_keys(item.get("issue")) & row_numbers):
                continue
        if row.get("series") and item.get("series") and not series_names_match(row.get("series"), item.get("series")):
            continue
        row_volume_id = row.get("volume_id")
        item_id = item_volume_id(item)
        try:
            if row_volume_id not in (None, "") and item_id is not None and int(row_volume_id) != int(item_id):
                continue
        except (TypeError, ValueError):
            continue
        return True
    return False


def host_path_for_kavita_path(path):
    if not path:
        return None
    text = str(path)
    for kavita_root, host_root in (
        (KAVITA_COMIC_ROOT, COMIC_ROOT),
        (KAVITA_MANGA_ROOT, MANGA_ROOT),
    ):
        prefix = f"{kavita_root}/"
        if text == kavita_root:
            return str(host_root)
        if text.startswith(prefix):
            return str(host_root / text[len(prefix):])
    return None


def series_name_variants(value):
    norm = normalize(value)
    variants = {norm} if norm else set()
    if norm.startswith("nickelodeon "):
        variants.add(norm.removeprefix("nickelodeon ").strip())
    return {variant for variant in variants if variant}


def series_names_match(left, right):
    return bool(series_name_variants(left) & series_name_variants(right))


def normalize_kavita_path(path):
    return str(path or "").replace("\\", "/").rstrip("/")


def path_under_prefix(path, prefix):
    path = normalize_kavita_path(path)
    prefix = normalize_kavita_path(prefix)
    return bool(path and prefix and (path == prefix or path.startswith(prefix + "/")))


def item_volume_id(item):
    value = item.get("kapowarr_id") or item.get("volume_id")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def item_uses_kapowarr_as_truth(item):
    if not isinstance(item, dict):
        return False
    source_of_truth = str(item.get("source_of_truth") or "").strip().lower()
    adapter_provider = str(
        item.get("adapter_provider")
        or item.get("metadata_adapter")
        or item.get("metadata_provider")
        or item.get("metadataProvider")
        or ""
    ).strip().lower()
    if source_of_truth in {"inkdrop", "inkdrop_state", "kavita"}:
        return False
    if source_of_truth == "metadata_adapter":
        return adapter_provider == "kapowarr"
    if source_of_truth == "kapowarr":
        return True

    owner = str(item.get("owner") or "").strip().lower()
    ownership = str(item.get("ownership") or "").strip().lower()
    series_source = str(item.get("series_source") or item.get("source") or "").strip().lower()
    metadata_provider = str(item.get("metadata_provider") or "").strip().lower()
    identity = str(item.get("queue_identity") or "").strip().lower()
    if owner == "inkdrop" or ownership == "native":
        return False
    if identity.startswith("comicvine:"):
        return False
    if series_source == "comicvine" or metadata_provider == "comicvine":
        return False
    return series_source == "kapowarr" or metadata_provider == "kapowarr"


def item_path_matches_kapowarr_folder(item, path, folder_prefixes=None):
    # Kapowarr is retired software; there is no live Kapowarr volume/folder
    # data to compare against, so this check always passes now. Kept as a
    # no-op function rather than deleted -- it has several call sites that
    # still gate on its result, and none of them need touching now that the
    # answer is unconditionally "yes, treat this as matched."
    return True


def kavita_visible_issue_index(queue):
    if not KAVITA_DB.exists():
        return {}
    items = [item for item in (queue.get("items") or {}).values() if isinstance(item, dict)]
    if not items:
        return {}
    rows = []
    con = sqlite3.connect(KAVITA_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            select
                s.Name as series,
                c.Number as number,
                c.Range as chapter_range,
                c.Title as title,
                mf.FilePath as file_path,
                mf.FileName as file_name,
                mf.Pages as pages,
                mf.Bytes as bytes
            from MangaFile mf
            join Chapter c on mf.ChapterId = c.Id
            join Volume v on c.VolumeId = v.Id
            join Series s on v.SeriesId = s.Id
            where mf.FilePath is not null
            """
        ).fetchall()
    finally:
        con.close()
    # Kapowarr is retired; no live volume/folder data to key candidate
    # matching against.
    folder_prefixes = {}
    index = {}
    for row in rows:
        row_series = row["series"] or ""
        row_file_path = row["file_path"] or ""
        row_host_path = host_path_for_kavita_path(row_file_path)
        row_record = {"number": row["number"], "chapter_range": row["chapter_range"]}
        row_numbers = kavita_record_issue_numbers(row_record, row_file_path or row["file_name"] or "")
        if not row_series or not row_numbers:
            continue
        for item in items:
            volume_id = item_volume_id(item) if item_uses_kapowarr_as_truth(item) else None
            item_prefixes = folder_prefixes.get(volume_id) if volume_id else None
            if item_prefixes:
                if not any(path_under_prefix(row_file_path, prefix) for prefix in item_prefixes):
                    continue
            elif not series_names_match(item.get("series"), row_series):
                continue
            if collection_target_single_part_block_reason(item, row_file_path, row["file_name"], row_series, row["title"]):
                continue
            if not (issue_number_keys(item.get("issue")) & row_numbers):
                continue
            key = item.get("key") or queue_key(item.get("series"), item.get("issue"))
            index[key] = {
                "verification_status": "library_visible",
                "dest": row_host_path or row_file_path,
                "library_file_path": row_host_path or row_file_path,
                "library_visibility_provider": "kavita",
                "kavita_file_path": row_file_path,
                "host_exists": None,
                "series": row_series,
                "issue": row["number"] or row["chapter_range"],
                "pages": row["pages"],
                "bytes": row["bytes"],
                "source": "kavita_visible_issue_index",
            }
    return index


def import_status_index(queue, *, deadline=None):
    status = read_json(IMPORT_STATUS_FILE, {}) or {}
    imported = status.get("imported") or []
    bad_archives = status.get("bad_archives") or status.get("skipped_bad_archives") or []
    bad_archive_history = read_json(PACK_BAD_ARCHIVE_HISTORY_FILE, {}) or {}
    history_bad_archives = (
        bad_archive_history.get("bad_archives")
        if isinstance(bad_archive_history, dict)
        else []
    ) or []
    if not isinstance(imported, list):
        imported = []
    if not isinstance(bad_archives, list):
        bad_archives = []
    if not isinstance(history_bad_archives, list):
        history_bad_archives = []
    if history_bad_archives:
        bad_archives = [*bad_archives, *history_bad_archives]
    if not imported and not bad_archives:
        return {}
    checked = ((status.get("verification") or {}).get("checked") or [])
    items = [
        item for item in (queue.get("items") or {}).values()
        if isinstance(item, dict)
    ]
    index = {}
    completed_statuses = {"folder_verified", "library_visible", "kavita_verified"}
    pending_statuses = {"waiting_for_library_scan", "waiting_for_kavita_scan", "library_scan_timeout", "kavita_scan_timeout"}
    rank = {
        "library_visible": 5,
        "folder_verified": 4,
        "kavita_verified": 4,
        "waiting_for_library_scan": 3,
        "waiting_for_kavita_scan": 3,
        "library_scan_timeout": 2,
        "kavita_scan_timeout": 2,
        "bad_archive": 1,
    }

    def index_row(row, verification_status, verification=None):
        if not isinstance(row, dict):
            return
        verification = verification if isinstance(verification, dict) else {}
        row_series = normalize(row.get("matched_series") or verification.get("series") or "")
        row_numbers = set()
        for field in ("normalized_number", "canonical_issue_number", "canonical_number", "issue_number"):
            row_numbers |= issue_number_keys_in_text(row.get(field))
        if not row_numbers:
            for field in ("dest", "source"):
                row_numbers |= issue_number_keys_in_text(row.get(field))
        if not row_series or not row_numbers:
            return
        row_volume_ids = {
            str(value)
            for value in (
                row.get("matched_kapowarr_id"),
                row.get("kapowarr_id"),
                row.get("volume_id"),
                verification.get("volume_id"),
            )
            if value not in (None, "")
        }
        row_issue_ids = {
            str(value)
            for value in (
                row.get("matched_kapowarr_issue_id"),
                row.get("kapowarr_issue_id"),
                row.get("issue_id"),
                verification.get("issue_id"),
            )
            if value not in (None, "")
        }
        for item in items:
            if deadline is not None and time.time() >= deadline:
                break
            item_volume_id = str(item.get("kapowarr_id") or item.get("volume_id") or "")
            if row_volume_ids and item_volume_id and item_volume_id not in row_volume_ids:
                continue
            item_issue_id = str(item.get("kapowarr_issue_id") or item.get("issue_id") or "")
            if row_issue_ids and item_issue_id and item_issue_id not in row_issue_ids:
                continue
            series = normalize(item.get("series") or "")
            if not series or not (series == row_series or series in row_series or row_series in series):
                continue
            if collection_target_single_part_block_reason(
                item,
                row.get("dest"),
                row.get("source"),
                row.get("title"),
                verification.get("dest"),
                verification.get("file_name"),
            ):
                continue
            if not (issue_number_keys(item.get("issue")) & row_numbers):
                continue
            if verification_status in pending_statuses and kavita_file_visible_for_item(item, row.get("dest") or verification.get("dest")):
                verification_status = "library_visible"
            merged = dict(row)
            merged["verification_status"] = verification_status
            merged["verification"] = verification
            key = item.get("key") or queue_key(item.get("series"), item.get("issue"))
            existing = index.get(key)
            if not existing or rank.get(verification_status, 0) > rank.get(existing.get("verification_status"), 0):
                index[key] = merged

    for pos, row in enumerate(imported):
        if deadline is not None and time.time() >= deadline:
            break
        verification = checked[pos] if pos < len(checked) and isinstance(checked[pos], dict) else {}
        base_verification_status = str(verification.get("verification_status") or "")
        if base_verification_status not in pending_statuses | completed_statuses:
            continue
        index_row(row, base_verification_status, verification)
    for row in bad_archives:
        if deadline is not None and time.time() >= deadline:
            break
        index_row(row, "bad_archive", {"verification_status": "bad_archive"})
    return index


def merge_current_queue(queue, current):
    now = time.time()
    items = queue.setdefault("items", {})
    created = 0
    verified = 0
    for key, entry in current.items():
        item = items.get(key)
        alternate_keys = entry.get("alternate_keys") if isinstance(entry.get("alternate_keys"), list) else []
        legacy_key = entry.get("legacy_key")
        if legacy_key and legacy_key not in alternate_keys:
            alternate_keys = [legacy_key, *alternate_keys]
        for alternate_key in alternate_keys:
            if item is not None or not alternate_key or alternate_key == key:
                continue
            legacy_item = items.get(alternate_key)
            if not isinstance(legacy_item, dict):
                continue
            legacy_identity = legacy_item.get("queue_identity") or queue_identity(
                watch_id=legacy_item.get("watch_id"),
                kapowarr_id=legacy_item.get("kapowarr_id"),
                comicvine_id=legacy_item.get("comicvine_id"),
                source=legacy_item.get("series_source"),
                owner=legacy_item.get("owner"),
                ownership=legacy_item.get("ownership"),
                metadata_provider=legacy_item.get("metadata_provider"),
            )
            entry_identity = entry.get("queue_identity")
            entry_equivalents = {
                value
                for value in equivalent_queue_identities(
                    watch_id=entry.get("watch_id"),
                    kapowarr_id=entry.get("kapowarr_id"),
                    comicvine_id=entry.get("comicvine_id"),
                    source=entry.get("series_source"),
                    owner=entry.get("owner"),
                    ownership=entry.get("ownership"),
                    metadata_provider=entry.get("metadata_provider"),
                )
                if value
            }
            if not legacy_identity or legacy_identity == entry_identity or legacy_identity in entry_equivalents:
                item = legacy_item
                item["migrated_from_legacy_key"] = alternate_key
                if legacy_identity and legacy_identity != entry_identity:
                    item["migrated_from_queue_identity"] = legacy_identity
                items[key] = item
                del items[alternate_key]
        if item is None:
            entry_source_order = apply_queue_item_source_policy(entry, now)
            entry_recovery_steps = queue_item_recovery_steps(entry)
            item = {
                "key": key,
                "state": "queued",
                "source_order": entry_source_order,
                "recovery_steps": entry_recovery_steps,
                "created_at": now,
                "created_at_iso": now_iso(now),
                "attempts": [],
            }
            items[key] = item
            created += 1
        elif item.get("state") == "verified":
            item["state"] = "queued"
            item.pop("completed_at", None)
            item.pop("completed_at_iso", None)
            item["resurrected_at"] = now
            item["resurrected_at_iso"] = now_iso(now)
        item.update(entry)
        item["source_order"] = apply_queue_item_source_policy(item, now)
        item["recovery_steps"] = queue_item_recovery_steps(item)
        item["present_in_watch"] = True
        item["updated_from_watch_at"] = now
        item["updated_from_watch_at_iso"] = now_iso(now)
        if item.get("state") in {"", None}:
            item["state"] = "queued"

    for key, item in items.items():
        if key in current:
            continue
        item["present_in_watch"] = False
        if wrong_language_quarantine_active(item):
            if item.get("state") == "verified":
                item["state"] = "queued"
                item.pop("completed_at", None)
                item.pop("completed_at_iso", None)
            item["current_source"] = None
            item["retry_after"] = 0
            item["retry_after_iso"] = "1970-01-01T00:00:00Z"
            item["last_event"] = "wrong-language source quarantined; waiting for library rescan"
            continue
        if item.get("state") != "verified":
            item["state"] = "verified"
            item["completed_at"] = now
            item["completed_at_iso"] = now_iso(now)
            item["last_event"] = "no longer missing in watched series"
            verified += 1
    if created or verified:
        queue.setdefault("history", []).append(
            {
                "ts": now,
                "ts_iso": now_iso(now),
                "event": "reconcile",
                "created": created,
                "verified": verified,
                "current_missing": len(current),
            }
        )
    return {"created": created, "verified": verified, "current_missing": len(current)}


def newest_review(rows):
    if not rows:
        return None
    return max(rows, key=lambda row: float(row.get("ts") or 0))


def queue_keys_for_rows(queue, selected_rows):
    items = (queue or {}).get("items") or {}
    if not isinstance(items, dict) or not selected_rows:
        return []
    selected_ids = {id(row) for row in selected_rows if isinstance(row, dict)}
    direct_keys = {
        str(row.get("key") or "").strip()
        for row in selected_rows
        if isinstance(row, dict) and row.get("key")
    }
    keys = []
    seen = set()
    for key, item in items.items():
        if key in seen:
            continue
        if id(item) in selected_ids or str(key) in direct_keys:
            keys.append(key)
            seen.add(key)
    return keys


def startup_annotation_row_keys(queue, args):
    items = (queue or {}).get("items") or {}
    if not isinstance(items, dict) or getattr(args, "status_only", False):
        return None
    keys = []
    seen = set()
    now = time.time()

    def add_key(key):
        if key and key not in seen:
            keys.append(key)
            seen.add(key)

    try:
        max_rows_per_group = max(1, int(getattr(args, "max_issues_per_series", 1) or 1))
    except (TypeError, ValueError):
        max_rows_per_group = 1
    try:
        max_groups = max(1, int(getattr(args, "max_series", 1) or 1))
    except (TypeError, ValueError):
        max_groups = 1
    for group_number, (_series, rows) in enumerate(due_series(queue, args)):
        if group_number >= max_groups:
            break
        for key in queue_keys_for_rows(queue, rows[:max_rows_per_group]):
            add_key(key)
    retry_cooldown_candidates = []
    for key, item in items.items():
        if key in seen or not isinstance(item, dict):
            continue
        missing_source_results = missing_required_source_result_sources(item)
        if repeated_source_retry_should_cooldown(
            item,
            now,
            missing_source_results=missing_source_results,
        ) or provider_retry_should_cooldown(
            item,
            now,
            missing_source_results=missing_source_results,
        ):
            retry_cooldown_candidates.append((retry_after_ts(item), queue_last_activity_ts(item), key))
    retry_cooldown_candidates.sort()
    for _retry_after, _activity, key in retry_cooldown_candidates[:STARTUP_RETRY_COOLDOWN_ANNOTATION_ROWS]:
        add_key(key)
    for key in deferred_manual_source_queue_keys(queue):
        add_key(key)
    return keys


def provider_targeted_annotation_deferred(queue, args, reason):
    """Leave expensive file checks to the exact rows at each provider boundary."""
    row_keys = startup_annotation_row_keys(queue, args) or []
    return {
        "ok": True,
        "reason": reason,
        "skipped": "provider_targeted_checks",
        "provider_targeted_checks": True,
        "processed": 0,
        "total": len(row_keys),
        "queue_total": len(((queue or {}).get("items") or {})),
    }


def deferred_manual_source_queue_keys(queue):
    keys = []
    seen = set()
    items = (queue or {}).get("items") or {}
    if not isinstance(items, dict):
        return keys

    def add_key(value):
        value = str(value or "").strip()
        if value and value in items and value not in seen:
            seen.add(value)
            keys.append(value)

    for entry in deferred_manual_source_queue_sync_entries():
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        rows = [
            row
            for row in [*(result.get("processed") or []), *(result.get("skipped") or [])]
            if isinstance(row, dict)
        ]
        for row in rows:
            for field in ("autopilot_queue_key", "queue_id", "queue_key", "key"):
                add_key(row.get(field))
            for target_key, _item in row_queue_targets(queue, row, include_inactive=True):
                add_key(target_key)
    return keys


def annotate_states(queue, max_seconds=None, reason=None, row_keys=None):
    started_at = time.time()
    started_monotonic = time.monotonic()
    deadline = None
    scoped_keys = set(row_keys or [])
    scoped = row_keys is not None
    queue_items = (queue.get("items") or {}) if isinstance(queue, dict) else {}
    if scoped:
        scoped_queue = dict(queue or {})
        scoped_queue["items"] = {
            key: item
            for key, item in queue_items.items()
            if key in scoped_keys
        }
        queue = scoped_queue
    if max_seconds is not None:
        try:
            budget = float(max_seconds)
        except (TypeError, ValueError):
            budget = 0.0
        if budget > 0:
            deadline = started_at + budget
    annotate_summary = {
        "ok": True,
        "reason": reason,
        "max_seconds": max_seconds,
        "processed": 0,
        "total": len(scoped_keys) if scoped else len(queue_items),
        "queue_total": len(queue_items),
    }
    if scoped:
        annotate_summary["scoped"] = True
    annotate_summary["phase_seconds"] = {}
    if scoped and not scoped_keys:
        annotate_summary["skipped"] = "empty_scope"
        return annotate_summary

    def timed_phase(name, callback):
        phase_started = time.monotonic()
        result = callback()
        annotate_summary["phase_seconds"][name] = round(time.monotonic() - phase_started, 3)
        return result

    def budget_exhausted(stage):
        if deadline is None or time.time() < deadline:
            return False
        annotate_summary.update({
            "ok": False,
            "stage": stage,
            "seconds": round(time.time() - started_at, 3),
        })
        log("annotate_states_budget_exhausted", **annotate_summary)
        return True

    budgeted = deadline is not None
    annotate_summary["slskd_cache_verdict_refresh"] = not budgeted

    target_index_budget_state = {}
    target_index = timed_phase(
        "queue_target_index",
        lambda: build_row_queue_target_index(
            queue,
            deadline=deadline,
            budget_state=target_index_budget_state,
        ),
    )
    annotate_summary.update(target_index_budget_state)
    if budget_exhausted("queue_target_index"):
        return annotate_summary
    reviews = timed_phase(
        "manual_review_index",
        lambda: manual_review_index(queue, target_index=target_index, deadline=deadline),
    )
    if budget_exhausted("manual_review_index"):
        return annotate_summary
    slskd_budget_state = {}
    slskd = timed_phase(
        "slskd_index",
        lambda: slskd_index(
            queue,
            refresh_cached_verdicts=not budgeted,
            deadline=deadline,
            target_index=target_index,
            budget_state=slskd_budget_state,
        ),
    )
    annotate_summary.update(slskd_budget_state)
    if budget_exhausted("slskd_index"):
        return annotate_summary
    reconcile = timed_phase("reconciliation_index", lambda: reconciliation_index(queue, deadline=deadline))
    if budget_exhausted("reconciliation_index"):
        return annotate_summary
    import_status = timed_phase("import_status_index", lambda: import_status_index(queue, deadline=deadline))
    if budget_exhausted("import_status_index"):
        return annotate_summary
    if budgeted:
        annotate_summary["kavita_visible_deferred"] = True
        kavita_visible = {}
    else:
        kavita_visible = timed_phase("kavita_visible_issue_index", lambda: kavita_visible_issue_index(queue))
        if budget_exhausted("kavita_visible_issue_index"):
            return annotate_summary
    # Kapowarr is retired; no live volume/folder data to key candidate
    # matching against.
    folder_prefixes = {}
    waiting_records = timed_phase("read_waiting_records", read_waiting_records)
    if budget_exhausted("read_waiting_records"):
        return annotate_summary
    waiting_ids = set()
    for review_id in waiting_records:
        if budget_exhausted("waiting_review_id_set"):
            return annotate_summary
        waiting_ids.add(review_id)
    resolved_by_key, resolved_by_review_id = timed_phase(
        "read_manual_source_resolved_records",
        lambda: read_manual_source_resolved_records(
            queue,
            target_index=target_index,
            deadline=deadline,
        ),
    )
    if budget_exhausted("read_manual_source_resolved_records"):
        return annotate_summary
    bad_candidate_by_key, bad_candidate_by_review_id = timed_phase(
        "read_manual_source_bad_candidate_records",
        lambda: read_manual_source_bad_candidate_records(
            queue,
            target_index=target_index,
            deadline=deadline,
        ),
    )
    if budget_exhausted("read_manual_source_bad_candidate_records"):
        return annotate_summary
    retry_pending_by_key, retry_pending_by_review_id = timed_phase(
        "read_manual_source_retry_pending_records",
        lambda: read_manual_source_retry_pending_records(
            queue,
            target_index=target_index,
            deadline=deadline,
        ),
    )
    if budget_exhausted("read_manual_source_retry_pending_records"):
        return annotate_summary
    autoresolve = read_json(MANUAL_SOURCE_AUTORESOLVE_STATUS_FILE, {}) or {}
    if budget_exhausted("read_manual_source_autoresolve_status"):
        return annotate_summary
    review_id_to_key = {}
    for key, rows in reviews.items():
        if budget_exhausted("review_id_map"):
            return annotate_summary
        for row in rows or []:
            if budget_exhausted("review_id_map"):
                return annotate_summary
            if isinstance(row, dict) and row.get("review_id"):
                review_id_to_key[str(row.get("review_id"))] = key
    for key, row in slskd.items():
        if budget_exhausted("slskd_review_id_map"):
            return annotate_summary
        if isinstance(row, dict) and row.get("review_id"):
            review_id_to_key[str(row.get("review_id"))] = key
    for review_id, record in waiting_records.items():
        if budget_exhausted("waiting_review_id_map"):
            return annotate_summary
        targets = row_queue_targets(
            queue,
            record,
            include_inactive=True,
            target_index=target_index,
        )
        review_id_to_key[str(review_id)] = targets[0][0] if targets else legacy_queue_key(record.get("series") or record.get("query"), record.get("issue"))
    for review_id, record in bad_candidate_by_review_id.items():
        if budget_exhausted("bad_candidate_review_id_map"):
            return annotate_summary
        if str(review_id) in review_id_to_key:
            continue
        targets = row_queue_targets(
            queue,
            record,
            include_inactive=True,
            target_index=target_index,
        )
        if targets:
            review_id_to_key[str(review_id)] = targets[0][0]
    for review_id, record in retry_pending_by_review_id.items():
        if budget_exhausted("retry_pending_review_id_map"):
            return annotate_summary
        if str(review_id) in review_id_to_key:
            continue
        targets = row_queue_targets(
            queue,
            record,
            include_inactive=True,
            target_index=target_index,
        )
        if targets:
            review_id_to_key[str(review_id)] = targets[0][0]
    waiting_by_key = {}
    for review_id, record in waiting_records.items():
        if budget_exhausted("waiting_by_key_map"):
            return annotate_summary
        key = review_id_to_key.get(str(review_id))
        if not key:
            continue
        existing = waiting_by_key.get(key)
        if not existing or row_ts(record) >= row_ts(existing):
            waiting_by_key[key] = record

    def waiting_supersedes_bad_candidate(key, bad_candidate):
        if not bad_candidate:
            return False
        current_waiting_record = waiting_by_key.get(key)
        return bool(current_waiting_record and row_ts(current_waiting_record) >= row_ts(bad_candidate))

    def pending_retry_for_key(key):
        retry_record = retry_pending_by_key.get(key)
        if retry_record:
            return retry_record
        for review_id, row in retry_pending_by_review_id.items():
            if deadline is not None and time.time() >= deadline:
                return None
            if review_id_to_key.get(review_id) == key:
                return row
        return None

    def clear_stale_candidate_failed_status(item, key):
        current_waiting_record = waiting_by_key.get(key)
        if (
            current_waiting_record
            and item.get("last_slskd_autoresolve_status") in {
                "candidate_failed",
                "retry_pending",
                "retry_exhausted",
                "retry_not_started",
            }
            and row_ts(current_waiting_record) >= float(item.get("last_failed_candidate_at") or 0)
        ):
            item.pop("last_slskd_autoresolve_status", None)
            item.pop("last_slskd_autoresolve_reason", None)

    def current_waiting_record_for_queue_row(key, review_id=None):
        review_id = str(review_id or "").strip()
        if review_id and isinstance(waiting_records.get(review_id), dict):
            return waiting_records.get(review_id)
        return waiting_by_key.get(key)

    def waiting_supersedes_row(key, row):
        record = current_waiting_record_for_queue_row(key, (row or {}).get("review_id"))
        return record if record and row_ts(record) >= row_ts(row) else None

    def apply_current_waiting_record(item, record, now):
        if not isinstance(item, dict) or not isinstance(record, dict):
            return False
        transfer = record.get("slskd_transfer") if isinstance(record.get("slskd_transfer"), dict) else {}
        started_at = row_ts(record) or now
        item["state"] = "downloading"
        item["current_source"] = "slskd"
        item["last_event"] = "SLSKD candidate started; waiting for download"
        item["last_slskd_waiting_review_id"] = record.get("review_id") or item.get("last_slskd_waiting_review_id")
        item["last_slskd_candidate"] = record.get("filename") or item.get("last_slskd_candidate")
        item["last_slskd_user"] = record.get("username") or item.get("last_slskd_user")
        item["last_slskd_score"] = record.get("score") or record.get("candidate_score") or item.get("last_slskd_score")
        item["last_slskd_autopick_status"] = "started_waiting"
        item["last_slskd_autoresolve_status"] = "waiting_for_transfer"
        item["last_slskd_autoresolve_at"] = now
        item["last_slskd_autoresolve_at_iso"] = now_iso(now)
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
        for source_key, target_key in (
            ("percentComplete", "last_slskd_transfer_percent"),
            ("bytesTransferred", "last_slskd_transfer_bytes_transferred"),
            ("bytesRemaining", "last_slskd_transfer_bytes_remaining"),
            ("averageSpeed", "last_slskd_transfer_average_speed"),
            ("attempts", "last_slskd_transfer_attempts"),
        ):
            if transfer.get(source_key) not in (None, ""):
                item[target_key] = transfer.get(source_key)
        if not item.get("download_started_at"):
            item["download_started_at"] = started_at
            item["download_started_at_iso"] = now_iso(started_at)
        item["last_download_started_at"] = started_at
        item["last_download_started_at_iso"] = now_iso(started_at)
        item["updated_at"] = now
        item["updated_at_iso"] = now_iso(now)
        item.pop("retry_after", None)
        item.pop("retry_after_iso", None)
        item.pop("needs_you_reason", None)
        clear_failed_candidate_status(item)
        return True

    now = time.time()
    active_pack_imports = timed_phase("active_pack_import_review_ids", lambda: active_pack_import_review_ids(now))
    if budget_exhausted("active_pack_import_review_ids"):
        return annotate_summary
    for key, item in queue_items.items():
        if scoped and key not in scoped_keys:
            continue
        if budget_exhausted("queue_row_loop"):
            return annotate_summary
        annotate_summary["processed"] += 1
        item_source_order = apply_queue_item_source_policy(item, now)
        if item.get("source_order") != item_source_order:
            item["source_order"] = item_source_order
        item_recovery_steps = queue_item_recovery_steps(item)
        if item.get("recovery_steps") != item_recovery_steps:
            item["recovery_steps"] = item_recovery_steps
        clear_soft_review_metadata(item)
        if clear_stale_verified_import_metadata(item, now):
            continue
        irow = import_status.get(key)
        istate = str((irow or {}).get("verification_status") or "")
        krow = kavita_visible.get(key)
        completed_import_statuses = {"folder_verified", "library_visible", "kavita_verified"}
        pending_import_statuses = {"waiting_for_library_scan", "waiting_for_kavita_scan", "library_scan_timeout", "kavita_scan_timeout"}
        if (
            irow
            and istate != "bad_archive"
            and not item_path_matches_kapowarr_folder(item, irow.get("dest"), folder_prefixes)
            and istate not in completed_import_statuses
        ):
            item["last_import_ignored_dest"] = irow.get("dest")
            item["last_import_ignored_reason"] = "different_kapowarr_folder"
            irow = None
            istate = ""
        elif irow and not item_path_matches_kapowarr_folder(item, irow.get("dest"), folder_prefixes):
            item["last_import_folder_mismatch_allowed"] = (
                "bad_archive_retry_evidence" if istate == "bad_archive" else "kavita_series_issue_match"
            )
        if irow and istate == "kavita_verified" and not kavita_file_visible_for_item(item, irow.get("dest")):
            clear_mismatched_verified_import_state(item, now, irow.get("dest"), force=True)
            irow = None
            istate = ""
        if krow:
            item["state"] = "verified"
            item["completed_at"] = now
            item["completed_at_iso"] = now_iso(now)
            item["last_import_status"] = "library_visible"
            item["last_import_dest"] = krow.get("dest")
            item["last_kavita_file_path"] = krow.get("kavita_file_path")
            item["last_local_truth_at"] = now
            item["last_local_truth_at_iso"] = now_iso(now)
            item["last_event"] = "Library already has issue"
            item["current_source"] = None
            item.pop("retry_after", None)
            item.pop("retry_after_iso", None)
            item.pop("needs_you_reason", None)
            continue
        pending_import_status = istate in pending_import_statuses
        bad_archive_for_active_pack = False
        if irow and istate in completed_import_statuses:
            item["state"] = "verified"
            item["completed_at"] = now
            item["completed_at_iso"] = now_iso(now)
            item["last_import_status"] = istate
            item["last_import_dest"] = irow.get("dest")
            item["last_event"] = (
                "Library verified imported file"
                if istate in {"library_visible", "kavita_verified"}
                else "Folder verified imported file"
            )
            item["current_source"] = None
            item.pop("retry_after", None)
            item.pop("retry_after_iso", None)
            item.pop("needs_you_reason", None)
            if not item.get("present_in_watch", True):
                continue
        if irow and istate == "bad_archive" and item.get("state") != "verified":
            bad_archive_pack_review_id = str(
                irow.get("review_id")
                or irow.get("pack_review_id")
                or item.get("last_pack_review_id")
                or ""
            ).strip()
            item["state"] = "searching"
            item["current_source"] = "failed_retry"
            item["last_import_status"] = "bad_archive"
            item["last_import_failed_at"] = now
            item["last_import_failed_at_iso"] = now_iso(now)
            item["last_bad_archive_source"] = irow.get("source")
            item["last_bad_archive_dest"] = irow.get("dest")
            if bad_archive_pack_review_id:
                item["last_bad_archive_pack_review_id"] = bad_archive_pack_review_id
            archive_check = irow.get("archive_check") if isinstance(irow.get("archive_check"), dict) else {}
            item["last_bad_archive_reason"] = archive_check.get("reason") or irow.get("reason") or "bad_archive"
            item["last_event"] = "pack import found a bad archive; retrying next candidate"
            bad_archive_for_active_pack = True
            item.pop("last_import_ignored_dest", None)
            item.pop("last_import_ignored_reason", None)
            item.pop("needs_you_reason", None)
            item.pop("retry_after", None)
            item.pop("retry_after_iso", None)
            irow = None
            istate = ""
        if (
            not irow
            and item.get("state") == "verified"
            and item.get("last_import_status") in pending_import_statuses
            and kavita_file_visible_for_item(item, item.get("last_import_dest"))
        ):
            item["last_import_status"] = "library_visible"
            item["last_event"] = "Library verified imported file"
            item["current_source"] = None
        if pending_import_status:
            if not keep_pending_import_state(item, now, verification_status=istate, dest=irow.get("dest")):
                item["state"] = "importing"
                item["last_import_status"] = istate
                item["last_import_dest"] = irow.get("dest")
                item["last_event"] = "imported file is waiting for library scan"
                if item.get("current_source") in {None, "", "verified"}:
                    item["current_source"] = "slskd" if (item.get("last_slskd_status") or item.get("last_slskd_transfer_id")) else "import"
            continue
        if not irow and keep_pending_import_state(item, now):
            continue
        if item.get("state") == "verified":
            item["current_source"] = None
            if keep_verified_import_state(item, now, folder_prefixes):
                continue
            if not item.get("present_in_watch", True):
                continue
            item["state"] = "queued"
            item.pop("completed_at", None)
            item.pop("completed_at_iso", None)
            item["last_event"] = "verified state rechecking because watch still reports missing"
        if not item.get("present_in_watch", True):
            continue
        pack_review_id = str(item.get("last_pack_review_id") or "")
        if (
            item.get("last_import_status") == "bad_archive"
            and item.get("last_bad_archive_pack_review_id")
            and str(item.get("last_bad_archive_pack_review_id") or "") == pack_review_id
        ):
            bad_archive_for_active_pack = True
        pack_status = active_pack_imports.get(pack_review_id)
        if pack_status and item.get("state") not in {"verified", "needs_you"} and not bad_archive_for_active_pack:
            mark_active_pack_import(item, pack_status, now)
            continue
        if (
            item.get("state") == "importing"
            and item.get("current_source") == "pack_import"
            and not pack_status
            and not irow
        ):
            clear_inactive_pack_import(item, now)
        release_automation_exhausted_for_retry(item, now)
        if stale_pack_assignment(item):
            clear_stale_pack_assignment(item, now)
        if item.get("state") == "needs_you" and not item.get("autopilot_slskd_attempted_at"):
            reason = str(item.get("needs_you_reason") or item.get("last_review_reason") or "")
            if reason not in HUMAN_REVIEW_REASONS:
                item["state"] = "queued"
                item.pop("needs_you_reason", None)
        if item.get("watch_status") == "grabbed" and item.get("state") not in {"importing", "verified"}:
            item["state"] = "downloading"
            item["last_event"] = "watch marked issue grabbed"
        rrow = reconcile.get(key)
        if rrow and item.get("state") != "verified" and not pack_title_covers_item(rrow.get("title"), item):
            item["last_reconcile_ignored_title"] = rrow.get("title")
            item["last_reconcile_ignored_reason"] = "pack_does_not_cover_trigger_issue"
            item["last_reconcile_ignored_at"] = now
            item["last_reconcile_ignored_at_iso"] = now_iso(now)
            if item.get("state") == "downloading":
                clear_stale_pack_assignment(item, now, title=rrow.get("title"), source="reconcile")
            rrow = None
        if rrow and item.get("state") != "verified":
            rstate = str(rrow.get("lifecycle_state") or "")
            item["last_reconcile_state"] = rstate
            item["last_reconcile_title"] = rrow.get("title")
            item["last_reconcile_client"] = rrow.get("client")
            item["last_reconcile_at"] = rrow.get("updated_at") or now
            item["last_reconcile_at_iso"] = now_iso(float(item["last_reconcile_at"]) if item.get("last_reconcile_at") else now)
            if rstate in {"queued", "downloading", "stalled_downloading"}:
                item["state"] = "downloading"
                item["last_event"] = f"{rrow.get('client') or 'download client'} reports {rstate}"
            elif rstate in {"completed_in_client", "ready_to_import", "waiting_for_library_scan", "waiting_for_kavita_scan"}:
                item["state"] = "importing"
                item["last_event"] = f"download is {rstate.replace('_', ' ')}"
            elif rstate in FAILED_RECONCILIATION_STATES and item.get("state") != "verified":
                item["state"] = "searching"
                item["current_source"] = "failed_retry"
                item["last_failed_download_state"] = rstate
                item["last_failed_download_reason"] = rrow.get("reason")
                item["last_failed_download_at"] = rrow.get("updated_at")
                item["last_event"] = f"previous download {rstate.replace('_', ' ')}; retrying next candidate"
                item.pop("needs_you_reason", None)
                item.pop("retry_after", None)
                item.pop("retry_after_iso", None)
        if irow and item.get("state") != "verified":
            item["last_import_status"] = istate
            item["last_import_dest"] = irow.get("dest")
            if istate in completed_import_statuses:
                item["state"] = "verified"
                item["completed_at"] = now
                item["completed_at_iso"] = now_iso(now)
                item["last_event"] = (
                    "Library verified imported file"
                    if istate in {"library_visible", "kavita_verified"}
                    else "Folder verified imported file"
                )
                continue
            elif istate in pending_import_statuses:
                item["state"] = "importing"
                item["last_event"] = "imported file is waiting for library scan"
        if keep_verified_import_state(item, now, folder_prefixes):
            continue
        if (
            item.get("state") == "searching"
            and item.get("current_source") == "failed_retry"
            and item.get("last_failed_retry_reason") in FAILED_RETRY_CONTINUE_REASONS
        ):
            item["current_source"] = None
            item["last_event"] = "failed retry exhausted; continuing source ladder"
        resolved_row = resolved_by_key.get(key)
        if not resolved_row:
            for review_id, resolved in resolved_by_review_id.items():
                if review_id_to_key.get(review_id) == key:
                    resolved_row = resolved
                    break
        if resolved_row and item.get("state") != "verified" and not pending_import_status:
            destinations = resolved_row.get("destinations") if isinstance(resolved_row.get("destinations"), list) else []
            if (
                destinations
                and not any(item_path_matches_kapowarr_folder(item, dest, folder_prefixes) for dest in destinations)
                and not resolved_record_verified_for_item(item, resolved_row)
            ):
                item["last_resolved_ignored_dest"] = destinations[0]
                item["last_resolved_ignored_reason"] = "different_kapowarr_folder"
                resolved_row = None
            elif destinations and not any(item_path_matches_kapowarr_folder(item, dest, folder_prefixes) for dest in destinations):
                item["last_resolved_folder_mismatch_allowed"] = "folder_or_library_verified"
        if resolved_row and item.get("state") != "verified" and not pending_import_status:
            item["state"] = "verified"
            item["completed_at"] = float(resolved_row.get("ts") or now)
            item["completed_at_iso"] = now_iso(item["completed_at"])
            item["last_event"] = "manual source import verified"
            item["last_import_status"] = "folder_verified"
            if destinations:
                item["last_import_dest"] = destinations[0]
            item["current_source"] = None
            item.pop("retry_after", None)
            item.pop("retry_after_iso", None)
            item.pop("needs_you_reason", None)
            continue
        srow = slskd.get(key)
        if srow:
            effective_safe_count = effective_safe_slskd_count_for_row(srow, item=item)
            item["last_slskd_status"] = srow.get("status")
            item["last_slskd_candidate_count"] = int(srow.get("candidate_count") or 0)
            item["last_slskd_detected_count"] = int(srow.get("detected_count") or 0)
            item["last_slskd_failed_candidate_count"] = int(srow.get("failed_candidate_count") or 0)
            item["last_slskd_auto_grab_safe_count"] = effective_safe_count
            item["last_slskd_auto_grab_review_count"] = int(srow.get("auto_grab_review_count") or 0)
            item["last_slskd_auto_grab_blocked_count"] = int(srow.get("auto_grab_blocked_count") or 0)
            item["last_slskd_at"] = srow.get("checked_at") or srow.get("staged_scan_at") or now
            review_id = str(srow.get("review_id") or "")
            if not rrow and not irow and stale_slskd_detected_probe_row(srow, now):
                clear_stale_slskd_import_signal(item, now, srow=srow)
            elif review_id in waiting_ids:
                waiting_record = waiting_records.get(review_id) or {}
                clear_stale_candidate_failed_status(item, key)
                item["current_source"] = "slskd"
                item["last_slskd_waiting_review_id"] = review_id
                item["last_slskd_candidate"] = waiting_record.get("filename") or item.get("last_slskd_candidate")
                item["last_slskd_user"] = waiting_record.get("username") or item.get("last_slskd_user")
                item["last_slskd_score"] = (
                    waiting_record.get("score")
                    or waiting_record.get("candidate_score")
                    or item.get("last_slskd_score")
                )
                transfer = waiting_record.get("slskd_transfer") if isinstance(waiting_record.get("slskd_transfer"), dict) else {}
                item["last_slskd_transfer_id"] = (
                    waiting_record.get("slskd_transfer_id")
                    or transfer.get("id")
                    or item.get("last_slskd_transfer_id")
                )
                item["last_slskd_transfer_state"] = (
                    waiting_record.get("slskd_transfer_state")
                    or transfer.get("state")
                    or transfer.get("stateDescription")
                    or item.get("last_slskd_transfer_state")
                )
                item["last_slskd_transfer_requested_at"] = (
                    waiting_record.get("slskd_transfer_requested_at")
                    or transfer.get("requestedAt")
                    or item.get("last_slskd_transfer_requested_at")
                )
                if item.get("state") not in {"importing", "verified"}:
                    item["state"] = "downloading"
                    item["last_event"] = "SLSKD candidate started; waiting for download"
                    clear_active_retry_state(item)
                    clear_failed_candidate_status(item)
                    clear_failed_import_state_for_new_download(item, now)
            elif int(srow.get("detected_count") or 0) > 0 and item.get("state") != "verified":
                item["current_source"] = "slskd"
                if item.get("state") != "importing":
                    item["state"] = "importing"
                    item["last_event"] = "staged file detected; waiting for verified import"
                    clear_active_retry_state(item)
            elif (
                int(srow.get("candidate_count") or 0) > 0
                and effective_safe_count > 0
                and item.get("state") not in {"downloading", "importing", "verified"}
            ):
                if slskd_transient_retry_pending(item, now):
                    item["state"] = "queued"
                    item["current_source"] = None
                    item["last_event"] = "SLSKD download API hiccup; retry scheduled"
                else:
                    item["state"] = "searching"
                    item["current_source"] = None
                    item["last_event"] = "SLSKD candidates available for autopick"
                    item.pop("retry_after", None)
                    item.pop("retry_after_iso", None)
                item.pop("needs_you_reason", None)
            elif (
                str(srow.get("status") or "") in SLSKD_NO_AUTOMATIC_RESULT_STATES
                and item.get("autopilot_slskd_attempted_at")
                and item.get("state") not in {"downloading", "importing", "verified"}
            ):
                mark_no_actionable_source_retry(item, now, set_timer=not preserve_due_retry_timer(item, now))
            elif (
                str(srow.get("status") or "") in SLSKD_NO_AUTOMATIC_RESULT_STATES
                and item.get("state") in {"queued", "searching"}
            ):
                mark_historical_slskd_no_action(item, srow.get("status"), now)
            elif int(srow.get("candidate_count") or 0) > 0 and item.get("state") not in {"downloading", "importing", "verified"}:
                item["state"] = "searching"
                item["current_source"] = None
                if effective_safe_count > 0:
                    item["last_event"] = "SLSKD candidates available for autopick"
                elif int(srow.get("auto_grab_blocked_count") or 0) > 0 and int(srow.get("auto_grab_review_count") or 0) <= 0:
                    item["last_event"] = "SLSKD candidates were rejected by safety checks; continuing source ladder"
                else:
                    item["last_event"] = "SLSKD candidates were not safe enough to auto-pick; continuing source ladder"
                item.pop("needs_you_reason", None)
            elif (
                str(srow.get("status") or "") in {"searched_no_candidates", "no_query", "failed_candidates_exhausted"}
                and int(srow.get("detected_count") or 0) <= 0
                and item.get("state") == "importing"
                and (item.get("current_source") == "slskd" or item.get("last_slskd_status") == "staged_file_ready")
            ):
                item["state"] = "searching"
                item["current_source"] = None
                item["slskd_staged_cleared_at"] = now
                item["slskd_staged_cleared_at_iso"] = now_iso(now)
                item["last_event"] = "SLSKD staged signal cleared; no matching staged file after refresh"
                item.pop("retry_after", None)
                item.pop("retry_after_iso", None)
                item.pop("needs_you_reason", None)
        elif not rrow and not irow and stale_slskd_import_signal(item, now):
            clear_stale_slskd_import_signal(item, now)
        if (
            not rrow
            and not irow
            and item.get("state") == "importing"
            and str(item.get("last_reconcile_state") or "") in {"completed_in_client", "ready_to_import"}
        ):
            item["state"] = "searching"
            item["current_source"] = None
            item["stale_reconcile_title"] = item.get("last_reconcile_title")
            item["stale_reconcile_state"] = item.get("last_reconcile_state")
            item["stale_reconcile_cleared_at"] = now
            item["stale_reconcile_cleared_at_iso"] = now_iso(now)
            item["last_reconcile_state"] = None
            item["last_reconcile_title"] = None
            item["last_event"] = "ready import no longer matches this issue; continuing source ladder"
            item.pop("retry_after", None)
            item.pop("retry_after_iso", None)
            item.pop("needs_you_reason", None)

        if stale_slskd_autopick_signal(item):
            clear_stale_slskd_autopick_signal(item, now)

        retry_pending = pending_retry_for_key(key)
        retry_pending_bad = (
            retry_pending.get("bad_candidate")
            if isinstance(retry_pending, dict) and isinstance(retry_pending.get("bad_candidate"), dict)
            else retry_pending
        )
        if waiting_supersedes_bad_candidate(key, retry_pending_bad):
            retry_pending = None
            clear_stale_candidate_failed_status(item, key)
        if retry_pending and not has_cached_safe_slskd_candidate(item):
            apply_pending_slskd_retry(item, retry_pending, now)

        bad_candidate = bad_candidate_by_key.get(key)
        if not bad_candidate:
            for review_id, row in bad_candidate_by_review_id.items():
                if review_id_to_key.get(review_id) == key:
                    bad_candidate = row
                    break
        if waiting_supersedes_bad_candidate(key, bad_candidate):
            bad_candidate = None
        if bad_candidate:
            item_retry_pending = str(item.get("last_slskd_autoresolve_status") or "") == "retry_pending"
            if retry_pending or item_retry_pending:
                item["last_failed_candidate_reason"] = bad_candidate.get("reason") or item.get("last_failed_candidate_reason")
                if item_retry_pending and has_cached_safe_slskd_candidate(item):
                    item["state"] = "searching"
                    item["current_source"] = None
                    item["last_event"] = "SLSKD worker busy; retrying next candidate soon"
                    item.pop("retry_after", None)
                    item.pop("retry_after_iso", None)
                    item.pop("needs_you_reason", None)
            elif has_cached_safe_slskd_candidate(item):
                mark_safe_slskd_alternate_available(item, bad_candidate, now)
                if (
                    item.get("state") == "importing"
                    and not rrow
                    and not irow
                    and not current_waiting_record_for_queue_row(key, item.get("last_slskd_waiting_review_id"))
                ):
                    item["state"] = "searching"
                    item["current_source"] = None
                    item["last_event"] = "failed SLSKD candidate marked bad; retrying safe alternate"
                    item["updated_at"] = now
                    item["updated_at_iso"] = now_iso(now)
                    item.pop("retry_after", None)
                    item.pop("retry_after_iso", None)
                    item.pop("needs_you_reason", None)
            else:
                apply_failed_slskd_candidate(item, bad_candidate, now)

        if not rrow and not irow and downloader_handoff_wait(item):
            item["state"] = "source_wait"
            item["last_event"] = f"{item.get('current_source') or 'source'} sent a candidate; waiting for downloader confirmation"
            item.setdefault("source_wait_started_at", item.get("last_action_at") or now)
            item.setdefault("source_wait_started_at_iso", item.get("last_action_at_iso") or now_iso(now))
            item["updated_at"] = now
            item["updated_at_iso"] = now_iso(now)
        if not rrow and not irow and stale_downloader_send(item, now):
            item["state"] = "searching"
            item["stale_downloader_source"] = item.get("current_source")
            item["stale_downloader_title"] = item.get("last_candidate_title")
            item["stale_downloader_cleared_at"] = now
            item["stale_downloader_cleared_at_iso"] = now_iso(now)
            item["current_source"] = None
            item["last_event"] = STALE_DOWNLOADER_SEND_EVENT
            item.pop("retry_after", None)
            item.pop("retry_after_iso", None)
            item.pop("needs_you_reason", None)
        normalize_stale_downloader_send_result(item, now)

        review = newest_review(reviews.get(key) or [])
        if review:
            reason = str(review.get("reason") or "")
            if reason in HUMAN_REVIEW_REASONS:
                item["last_review_reason"] = reason
                item["last_review_at"] = review.get("ts")
                item["state"] = "needs_you"
                item["needs_you_reason"] = reason
                item["last_event"] = "review row needs a human decision"
            elif reason in AUTOMATION_REVIEW_REASONS:
                record_automation_source_outcome(
                    item,
                    reason,
                    str(review.get("source") or item.get("last_source_outcome_source") or "source"),
                    row_ts(review) or now,
                    review,
                )
                if item.get("state") not in {"downloading", "importing", "verified"}:
                    item["state"] = "searching"
                    item["last_event"] = "low-confidence source candidate parked while autopilot continues"
            elif reason in NEEDS_SOURCE_REASONS and item.get("state") == "queued":
                record_automation_source_outcome(
                    item,
                    reason,
                    str(review.get("source") or item.get("last_source_outcome_source") or "source"),
                    row_ts(review) or now,
                    review,
                )
                item["state"] = "searching"
                item["last_event"] = "source fallback queued"
            elif reason in NEEDS_SOURCE_REASONS:
                record_automation_source_outcome(
                    item,
                    reason,
                    str(review.get("source") or item.get("last_source_outcome_source") or "source"),
                    row_ts(review) or now,
                    review,
                )

        clear_inactive_source_marker(item, now)
        normalize_waiting_retry_state(item, now)

        if automatic_sources_exhausted(
            item,
            DEFAULT_EXHAUSTION_CYCLES,
            now=now,
            grace_seconds=EXHAUSTION_ANNOTATION_GRACE_SECONDS,
        ):
            mark_automation_exhausted(item, now, source="annotate")

        if item.get("state") == "verified":
            clear_verified_slskd_activity(item)

    if scoped:
        annotate_summary["manual_source_autoresolve_scoped"] = True
        annotate_summary["manual_source_autoresolve_scope_skipped"] = 0
        annotate_summary["manual_source_autoresolve_deferred"] = True
    autoresolve_snapshots = [] if scoped else manual_source_autoresolve_snapshots(autoresolve)
    for autoresolve_snapshot in autoresolve_snapshots:
        if budget_exhausted("manual_source_autoresolve_loop"):
            return annotate_summary
        autoresolve_rows = [
            row
            for row in [*(autoresolve_snapshot.get("processed") or []), *(autoresolve_snapshot.get("skipped") or [])]
            if isinstance(row, dict)
        ]
        if not autoresolve_rows and autoresolve_snapshot.get("state") not in {"checking", "watching", "importing", "resolved", "deferred"}:
            continue
        snapshot_touched = not scoped
        snapshot_complete = not scoped
        native_replay = replay_deferred_native_autoresolve_attempts(autoresolve_snapshot)
        if not native_replay.get("ok"):
            snapshot_complete = False
            annotate_summary["manual_source_native_attempt_replay_deferred"] = True
        elif native_replay.get("recorded"):
            annotate_summary["manual_source_native_attempts_replayed"] = int(
                annotate_summary.get("manual_source_native_attempts_replayed") or 0
            ) + int(native_replay.get("recorded") or 0)
        for row in autoresolve_rows:
            review_id = str(row.get("review_id") or "")
            status = str(row.get("status") or autoresolve_snapshot.get("state") or "")
            targets = row_queue_targets(queue, row, include_inactive=True)
            if not targets:
                key = review_id_to_key.get(review_id)
                item = queue.get("items", {}).get(key)
                targets = [(key, item)] if key and item else []
            if scoped and not targets:
                snapshot_complete = False
                continue
            if scoped:
                targets = [(key, item) for key, item in targets if key in scoped_keys]
                if not targets:
                    snapshot_complete = False
                    continue
                snapshot_touched = True
            for key, item in targets:
                    if not item or item.get("state") == "verified":
                        continue
                    pending_import_status = str((import_status.get(key) or {}).get("verification_status") or "") in {
                        "waiting_for_library_scan",
                        "waiting_for_kavita_scan",
                        "library_scan_timeout",
                        "kavita_scan_timeout",
                    }
                    recovery = row.get("recovery") if isinstance(row.get("recovery"), dict) else {}
                    retry_probe = recovery.get("retry_probe") if isinstance(recovery.get("retry_probe"), dict) else {}
                    bad_candidate = recovery.get("bad_candidate") if isinstance(recovery.get("bad_candidate"), dict) else None
                    superseding_wait = waiting_supersedes_row(key, row)
                    if superseding_wait:
                        apply_current_waiting_record(item, superseding_wait, now)
                        continue
                    if waiting_supersedes_bad_candidate(key, bad_candidate):
                        bad_candidate = None
                        clear_stale_candidate_failed_status(item, key)
                    if not bad_candidate and review_id and review_id not in waiting_ids:
                        existing_bad = bad_candidate_by_review_id.get(review_id)
                        if existing_bad:
                            if str(item.get("last_slskd_autoresolve_status") or "") == "retry_pending":
                                item["state"] = "searching"
                                item["current_source"] = None
                                item["last_event"] = "SLSKD worker busy; retrying next candidate soon"
                            elif has_cached_safe_slskd_candidate(item):
                                mark_safe_slskd_alternate_available(item, existing_bad, now)
                            else:
                                apply_failed_slskd_candidate(item, existing_bad, now, retry_started=False)
                                continue
                    if (
                        review_id
                        and review_id not in waiting_ids
                        and status in {
                            "transfer_in_progress",
                            "transfer_settling",
                            "waiting_for_staged_file",
                            "transfer_unknown",
                            "transfer_lookup_error",
                            "staged_file_settling",
                        }
                    ):
                        continue
                    if bad_candidate:
                        if status == "retry_pending":
                            pending_record = {
                                "review_id": review_id,
                                "bad_candidate": bad_candidate,
                                "last_retry": retry_probe,
                                "reason": row.get("reason") or retry_pending_reason({"last_retry": retry_probe}),
                            }
                            apply_pending_slskd_retry(item, pending_record, now)
                        elif str(item.get("last_slskd_autoresolve_status") or "") == "retry_pending":
                            item["state"] = "searching"
                            item["current_source"] = None
                            item["last_event"] = "SLSKD worker busy; retrying next candidate soon"
                        elif has_cached_safe_slskd_candidate(item):
                            mark_safe_slskd_alternate_available(item, bad_candidate, now)
                        else:
                            apply_failed_slskd_candidate(item, bad_candidate, now, retry_started=bool(retry_probe.get("started")))
                        continue
                    if apply_slskd_transfer_status(item, row, now):
                        continue
                    already_verified_row = manual_source_row_already_verified(row)
                    if (
                        (status in {"resolved", "already_verified"} or row.get("manual_source_resolved") or already_verified_row)
                        and (not pending_import_status or already_verified_row)
                    ):
                        item["state"] = "verified"
                        item["last_event"] = "Library verified imported file" if already_verified_row else "manual source import verified"
                        item["current_source"] = None
                        item["completed_at"] = now
                        item["completed_at_iso"] = now_iso(now)
                        item["verified_at"] = now
                        item["verified_at_iso"] = now_iso(now)
                        item["last_import_status"] = "library_visible" if already_verified_row else "folder_verified"
                        destinations = row.get("destinations") if isinstance(row.get("destinations"), list) else []
                        if not destinations:
                            resolution = row.get("manual_source_resolution") if isinstance(row.get("manual_source_resolution"), dict) else {}
                            destinations = resolution.get("destinations") if isinstance(resolution.get("destinations"), list) else []
                        if not destinations:
                            live = row.get("live") if isinstance(row.get("live"), dict) else {}
                            destinations = live.get("destinations") if isinstance(live.get("destinations"), list) else []
                        if not destinations:
                            imported = row.get("imported") if isinstance(row.get("imported"), list) else []
                            destinations = [
                                entry.get("dest")
                                for entry in imported
                                if isinstance(entry, dict) and entry.get("dest")
                            ]
                        if destinations:
                            item["last_import_dest"] = destinations[0]
                            item["imported_path"] = destinations[0]
                        clear_active_retry_state(item)
                        clear_verified_slskd_activity(item)
                    elif status == "resolved" and pending_import_status:
                        item["state"] = "importing"
                        item["last_event"] = "manual source import copied; waiting for library scan"
                        clear_active_retry_state(item)
                    elif retry_probe.get("started"):
                        item["state"] = "downloading"
                        item["last_event"] = "failed SLSKD candidate marked bad; next candidate started"
                        clear_active_retry_state(item)
                    elif status in {"preview_importable", "verification_pending", "imported_not_resolved"}:
                        item["state"] = "importing"
                        live = row.get("live") if isinstance(row.get("live"), dict) else {}
                        note = (
                            row.get("verification_pending_note")
                            or live.get("note")
                            or (
                                "manual source preview is importable; waiting for live import"
                                if status == "preview_importable"
                                else ""
                            )
                        )
                        item["last_event"] = note or f"manual source autoresolver {status}"
                        clear_active_retry_state(item)
                    elif status == "error":
                        item["state"] = "searching"
                        item["last_event"] = "manual source autoresolver error; automation will retry"
                        item.pop("needs_you_reason", None)
                    else:
                        item["last_event"] = f"manual source autoresolver {status}"
        if snapshot_touched and snapshot_complete:
            mark_deferred_manual_source_sync_applied(autoresolve_snapshot.get("_deferred_queue_sync_id"))
        elif scoped:
            annotate_summary["manual_source_autoresolve_scope_skipped"] += 1

    final_items = (
        (queue_items.get(key) for key in scoped_keys)
        if scoped
        else (queue.get("items") or {}).values()
    )
    for item in final_items:
        if budget_exhausted("final_verified_cleanup_loop"):
            return annotate_summary
        if isinstance(item, dict):
            clear_active_retry_state(item)
            if item.get("state") == "verified":
                item["current_source"] = None
                if item.get("last_import_dest"):
                    item["imported_path"] = item.get("last_import_dest")
                item.pop("needs_you_reason", None)
                clear_verified_slskd_activity(item)
    annotate_summary["seconds"] = round(time.time() - started_at, 3)
    if deadline is not None or annotate_summary["seconds"] >= 10:
        log("annotate_states_finished", **annotate_summary)
    annotate_summary["seconds"] = round(time.monotonic() - started_monotonic, 3)
    return annotate_summary


def action_pack_metadata(action):
    outcome = action.get("outcome") if isinstance(action.get("outcome"), dict) else {}
    pack_info = action.get("pack_info") or outcome.get("pack_info") or {}
    pack_match = action.get("pack_match") or outcome.get("pack_match") or {}
    if not isinstance(pack_info, dict):
        pack_info = {}
    if not isinstance(pack_match, dict):
        pack_match = {}
    return (pack_info, pack_match)


def pack_sample_issues(pack_match):
    issues = set()
    for row in (pack_match or {}).get("useful_missing_sample") or []:
        if not isinstance(row, dict):
            continue
        issues |= issue_number_keys(row.get("issue"))
        issues |= issue_number_keys(row.get("calculated"))
    return issues


def pack_range_covers_issue(pack_info, issue_value):
    if issue_value is None:
        return False
    for row in (pack_info or {}).get("ranges") or []:
        if not isinstance(row, dict):
            continue
        start = issue_number_value(row.get("start"))
        end = issue_number_value(row.get("end"))
        if start is None or end is None:
            continue
        low, high = sorted((start, end))
        if low <= issue_value <= high:
            return True
    return False


def pack_action_has_coverage_metadata(action):
    pack_info, pack_match = action_pack_metadata(action)
    return bool(
        (pack_info.get("ranges") if isinstance(pack_info, dict) else None)
        or (pack_match.get("useful_missing_sample") if isinstance(pack_match, dict) else None)
        or (pack_match.get("coverage_source") if isinstance(pack_match, dict) else None)
    )


def pack_action_covers_item(action, item):
    if not action.get("pack_auto_approved"):
        return True
    pack_info, pack_match = action_pack_metadata(action)
    sample_issues = pack_sample_issues(pack_match)
    if sample_issues and sample_issues & issue_number_keys(item.get("issue")):
        return True
    if pack_range_covers_issue(pack_info, issue_number_value(item.get("issue"))):
        return True
    if (pack_match or {}).get("coverage_source") == "complete_keyword" and not (pack_info or {}).get("ranges"):
        return True
    if not pack_action_has_coverage_metadata(action):
        return legacy_queue_key(action.get("series"), action.get("issue")) == item_legacy_key(item)
    return False


def action_queue_targets(queue, action):
    items = queue.get("items", {}) or {}
    if action.get("pack_auto_approved"):
        series = normalize(action.get("series") or "")
        covered = [
            (key, item)
            for key, item in items.items()
            if isinstance(item, dict)
            and normalize(item.get("series") or "") == series
            and item.get("present_in_watch", True)
            and pack_action_covers_item(action, item)
        ]
        if covered:
            return covered
        if pack_action_has_coverage_metadata(action):
            return []
    return row_queue_targets(queue, action)


def row_candidate(row):
    candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else {}
    return candidate if isinstance(candidate, dict) else {}


def first_row_value(row, candidate, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
        value = candidate.get(key)
        if value not in (None, ""):
            return value
    return None


def download_client_for_attempt(row, candidate):
    outcome = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
    value = outcome.get("download_client") or outcome.get("client")
    if value not in (None, ""):
        return value
    protocol = str(first_row_value(row, candidate, "protocol") or "").lower()
    if protocol == "torrent":
        return "qBittorrent"
    if protocol == "usenet":
        return "SABnzbd"
    return None


def source_attempt_from_row(row, source, status, now, reason=None):
    candidate = row_candidate(row)
    outcome = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
    attempt = {
        "ts": now,
        "ts_iso": now_iso(now),
        "source": source,
        "status": status,
        "reason": reason or row.get("reason"),
        "title": first_row_value(row, candidate, "title", "release_title"),
        "query": row.get("query"),
        "provider": first_row_value(row, candidate, "indexer", "provider"),
        "indexer": first_row_value(row, candidate, "indexer", "provider"),
        "protocol": first_row_value(row, candidate, "protocol"),
        "download_client": download_client_for_attempt(row, candidate),
        "category": outcome.get("category") or row.get("category"),
        "save_path": outcome.get("save_path") or row.get("save_path"),
        "download_url_hash": first_row_value(row, candidate, "download_url_hash", "downloadUrlHash", "url_hash"),
        "score": row.get("score") or row.get("confidence_score") or candidate.get("score"),
        "seeders": first_row_value(row, candidate, "seeders"),
        "pack_auto_approved": bool(row.get("pack_auto_approved")),
        "pack_trigger_issue": row.get("issue") if row.get("pack_auto_approved") else None,
        "pack_review_id": row.get("pack_review_id"),
    }
    return {key: value for key, value in attempt.items() if value not in (None, "")}


def result_review_rows(result):
    if not isinstance(result, dict):
        return []
    rows = []
    for key in ("review", "reviews"):
        value = result.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    return rows


def apply_result_to_queue(queue, result, source):
    if not isinstance(result, dict):
        return {"actions": 0, "reviews": 0}
    touched = {"actions": 0, "reviews": 0}
    now = time.time()
    for action in result.get("actions") or []:
        targets = action_queue_targets(queue, action)
        if not targets:
            continue
        for _, item in targets:
            item["state"] = "source_wait"
            item["current_source"] = source
            item["last_event"] = f"{source} sent a candidate; waiting for downloader confirmation"
            clear_source_started_marker(item, source)
            item["last_action_at"] = now
            item["last_action_at_iso"] = now_iso(now)
            item["last_candidate_title"] = action.get("title")
            if action.get("pack_auto_approved"):
                item["last_pack_trigger_issue"] = action.get("issue")
                item["last_pack_review_id"] = action.get("pack_review_id")
            item.setdefault("attempts", []).append(source_attempt_from_row(action, source, "sent", now))
            touched["actions"] += 1
    for review in result_review_rows(result):
        targets = row_queue_targets(queue, review)
        if not targets:
            continue
        reason = str(review.get("reason") or "")
        for _, item in targets:
            if reason in HUMAN_REVIEW_REASONS:
                item["last_review_reason"] = reason
                item["last_review_source"] = source
                item["last_review_at"] = now
                item["last_review_at_iso"] = now_iso(now)
                item["state"] = "needs_you"
                item["needs_you_reason"] = reason
                item["last_event"] = f"{source} needs a human decision"
            elif reason in AUTOMATION_REVIEW_REASONS:
                record_automation_source_outcome(item, reason, source, now, review)
                if item.get("state") not in ACTIVE_QUEUE_STATES | {"verified"}:
                    item["state"] = "searching"
                    item["last_event"] = f"{source} found a low-confidence candidate; trying the next source"
            elif item.get("state") not in ACTIVE_QUEUE_STATES | {"needs_you"}:
                record_automation_source_outcome(item, reason, source, now, review)
                item["state"] = "searching"
                item["last_event"] = f"{source} did not find a safe candidate"
            clear_source_started_marker(item, source)
            item.setdefault("attempts", []).append(source_attempt_from_row(review, source, "review", now, reason=reason))
            touched["reviews"] += 1
    return touched


def apply_mangadex_result_to_queue(queue, result):
    if not isinstance(result, dict):
        return {"actions": 0, "reviews": 0}
    touched = {"actions": 0, "reviews": 0, "verified": 0, "importing": 0}
    now = time.time()
    for action in result.get("actions") or []:
        targets = action_queue_targets(queue, action)
        if not targets:
            continue
        status = str(action.get("status") or "").strip().lower()
        verified = bool(action.get("verified")) or status in {"kavita_verified", "verified"}
        for _, item in targets:
            clear_source_started_marker(item, "mangadex")
            if status == "dry_run":
                item["last_event"] = "MangaDex direct download previewed"
            elif verified:
                item["state"] = "verified"
                item["current_source"] = "mangadex"
                item["completed_at"] = now
                item["completed_at_iso"] = now_iso(now)
                item["last_event"] = action.get("reason") or "MangaDex chapter visible in library"
                touched["verified"] += 1
            else:
                item["state"] = "importing"
                item["current_source"] = "mangadex"
                item["last_event"] = action.get("reason") or "MangaDex chapter downloaded; waiting for library verification"
                touched["importing"] += 1
            item["last_action_at"] = now
            item["last_action_at_iso"] = now_iso(now)
            item["last_import_dest"] = action.get("dest") or item.get("last_import_dest")
            item["last_mangadex_status"] = status
            item["last_mangadex_chapter_id"] = action.get("mangadex_chapter_id") or item.get("mangadex_chapter_id")
            item.setdefault("attempts", []).append(
                {
                    "ts": now,
                    "ts_iso": now_iso(now),
                    "source": "mangadex",
                    "provider": "MangaDex",
                    "protocol": "https",
                    "download_client": "MangaDex",
                    "status": status or "downloaded",
                    "reason": action.get("reason"),
                    "title": action.get("title"),
                    "query": action.get("title") or item.get("query"),
                    "dest_path": action.get("dest"),
                    "local_path": action.get("dest"),
                    "mangadex_id": action.get("mangadex_id"),
                    "mangadex_chapter_id": action.get("mangadex_chapter_id"),
                    "page_count": action.get("page_count"),
                    "size_bytes": action.get("size_bytes"),
                }
            )
            touched["actions"] += 1
    for review in result.get("review") or []:
        targets = row_queue_targets(queue, review)
        for _, item in targets:
            clear_source_started_marker(item, "mangadex")
            item["state"] = "searching"
            item["current_source"] = None
            item["last_event"] = "MangaDex direct source failed; continuing source ladder"
            item["last_mangadex_status"] = "error"
            item.setdefault("attempts", []).append(
                {
                    "ts": now,
                    "ts_iso": now_iso(now),
                    "source": "mangadex",
                    "provider": "MangaDex",
                    "status": "error",
                    "reason": review.get("error") or review.get("reason"),
                    "query": item.get("query"),
                }
            )
            touched["reviews"] += 1
    return touched


def apply_slskd_auto_grab(queue, result):
    outcome = (result or {}).get("auto_grab") or {}
    touched = {
        "started": 0,
        "safe": int((result or {}).get("auto_grab_safe_count") or 0),
        "user_load_wait": 0,
        "transient_retry": 0,
    }
    now = time.time()

    def append_slskd_attempt(item, row, status, reason=None):
        transfer = row.get("transfer") if isinstance(row.get("transfer"), dict) else {}
        mark_waiting = row.get("mark_waiting") if isinstance(row.get("mark_waiting"), dict) else {}
        attempt = {
            "ts": now,
            "ts_iso": now_iso(now),
            "source": "slskd",
            "status": status,
            "reason": reason or row.get("reason"),
            "filename": row.get("filename"),
            "username": row.get("username"),
            "score": row.get("score"),
            "error": row.get("error"),
            "slskd_transfer_id": (
                row.get("slskd_transfer_id")
                or transfer.get("id")
                or mark_waiting.get("slskd_transfer_id")
            ),
            "slskd_transfer_state": (
                row.get("slskd_transfer_state")
                or transfer.get("state")
                or transfer.get("stateDescription")
                or mark_waiting.get("slskd_transfer_state")
            ),
        }
        item.setdefault("attempts", []).append({key: value for key, value in attempt.items() if value not in (None, "")})

    def mark_transient_retry(item, row):
        status = str(row.get("status") or "").strip().lower()
        item["state"] = "queued"
        item["current_source"] = None
        item["last_slskd_autopick_status"] = status or "transient_error"
        item["last_slskd_autopick_error"] = row.get("error")
        item["last_slskd_transient_error_at"] = now
        item["last_slskd_transient_error_at_iso"] = now_iso(now)
        item["last_slskd_candidate"] = row.get("filename") or item.get("last_slskd_candidate")
        item["last_slskd_user"] = row.get("username") or item.get("last_slskd_user")
        item["last_slskd_score"] = row.get("score") or item.get("last_slskd_score")
        item["last_slskd_auto_grab_safe_count"] = effective_safe_slskd_count_for_row(row, item=item)
        if status == "ambiguous_enqueue_response":
            item["last_event"] = "SLSKD enqueue response was ambiguous; rechecking transfer identity"
        else:
            item["last_event"] = "SLSKD download API hiccup; retry scheduled"
        try:
            retry_seconds = int(row.get("retry_after_seconds") or SLSKD_TRANSIENT_RETRY_SECONDS)
        except (TypeError, ValueError):
            retry_seconds = SLSKD_TRANSIENT_RETRY_SECONDS
        retry_seconds = max(60, min(retry_seconds, 30 * 60))
        retry_after = now + retry_seconds
        item["retry_after"] = retry_after
        item["retry_after_iso"] = now_iso(retry_after)
        item.pop("needs_you_reason", None)
        append_slskd_attempt(item, row, status or "transient_retry", row.get("reason") or "download API hiccup; retry scheduled")

    def slskd_autograb_error_should_retry(row, status):
        if status == "transient_error" or row.get("transient_error"):
            return True
        if status in {"error", "download_api_error", "download_preflight_api_error"} and row.get("error"):
            if row.get("transfer") or row.get("mark_waiting"):
                return False
            return True
        return False

    for row in outcome.get("user_load_skipped") or []:
        targets = row_queue_targets(queue, row)
        if not targets:
            continue
        slot_request = row.get("slot_request") if isinstance(row.get("slot_request"), dict) else {}
        slot_status = str(row.get("status") or slot_request.get("status") or "").strip().lower()
        for _, item in targets:
            if item.get("state") in {"downloading", "importing", "verified", "needs_you"}:
                continue
            clear_source_started_marker(item, "slskd")
            item["state"] = "queued"
            item["current_source"] = None
            item["last_slskd_autopick_status"] = slot_status or "slot_request_failed"
            item["last_slskd_user_load_reason"] = row.get("reason")
            item["last_slskd_user_load_at"] = now
            item["last_slskd_user_load_at_iso"] = now_iso(now)
            item["last_slskd_candidate"] = row.get("filename") or item.get("last_slskd_candidate")
            item["last_slskd_user"] = row.get("username") or item.get("last_slskd_user")
            item["last_slskd_score"] = row.get("score") or item.get("last_slskd_score")
            item["last_slskd_auto_grab_safe_count"] = effective_safe_slskd_count_for_row(row, item=item)
            for source_key, target_key in (
                ("slot_request_id", "last_slskd_slot_request_id"),
                ("download_task_id", "last_slskd_slot_download_task_id"),
                ("slot_request_created_at", "last_slskd_slot_request_created_at"),
                ("slot_request_deadline", "last_slskd_slot_request_deadline"),
            ):
                if slot_request.get(source_key) not in (None, ""):
                    item[target_key] = slot_request.get(source_key)
            if slot_status in {"slot_request_failed", "slot_request_expired"}:
                item["last_event"] = (
                    "SLSKD transfer slot wait expired; automatic retry scheduled"
                    if slot_status == "slot_request_expired"
                    else "SLSKD transfer slot wait could not be saved; automatic retry scheduled"
                )
            else:
                item["last_event"] = "SLSKD candidate ready; waiting for transfer slot"
            retry_after = row.get("slot_request_retry_at") or slot_request.get("slot_request_retry_at")
            try:
                retry_after = float(retry_after)
            except (TypeError, ValueError):
                retry_after = now + SLSKD_USER_LOAD_RETRY_SECONDS
            item["retry_after"] = retry_after
            item["retry_after_iso"] = now_iso(retry_after)
            item.pop("needs_you_reason", None)
            append_slskd_attempt(item, row, slot_status or "slot_request_failed", row.get("reason") or "waiting for transfer slot")
            touched["user_load_wait"] += 1
    for row in outcome.get("rows") or []:
        targets = row_queue_targets(queue, row)
        if not targets:
            continue
        status = str(row.get("status") or "")
        for _, item in targets:
            clear_source_started_marker(item, "slskd")
            if slskd_autograb_error_should_retry(row, status):
                if item.get("state") not in {"downloading", "importing", "verified", "needs_you"}:
                    mark_transient_retry(item, row)
                    touched["transient_retry"] += 1
                continue
            item["current_source"] = "slskd"
            item["last_slskd_autopick_status"] = status
            item["last_slskd_candidate"] = row.get("filename")
            item["last_slskd_user"] = row.get("username")
            item["last_slskd_score"] = row.get("score")
            append_slskd_attempt(item, row, status)
            if status in {"started_waiting", "already_downloading"}:
                item["state"] = "downloading"
                item["last_event"] = "SLSKD candidate started; waiting for download"
                if status == "started_waiting" or not item.get("download_started_at"):
                    item["download_started_at"] = now
                    item["download_started_at_iso"] = now_iso(now)
                item["last_download_started_at"] = now
                item["last_download_started_at_iso"] = now_iso(now)
                item.pop("retry_after", None)
                item.pop("retry_after_iso", None)
                item.pop("needs_you_reason", None)
                clear_failed_candidate_status(item)
                clear_failed_import_state_for_new_download(item, now)
                transfer = row.get("transfer") if isinstance(row.get("transfer"), dict) else {}
                mark_waiting = row.get("mark_waiting") if isinstance(row.get("mark_waiting"), dict) else {}
                item["last_slskd_transfer_id"] = (
                    row.get("slskd_transfer_id")
                    or transfer.get("id")
                    or mark_waiting.get("slskd_transfer_id")
                    or item.get("last_slskd_transfer_id")
                )
                item["last_slskd_transfer_state"] = (
                    row.get("slskd_transfer_state")
                    or transfer.get("state")
                    or transfer.get("stateDescription")
                    or mark_waiting.get("slskd_transfer_state")
                    or item.get("last_slskd_transfer_state")
                )
                item["last_slskd_transfer_requested_at"] = (
                    row.get("slskd_transfer_requested_at")
                    or transfer.get("requestedAt")
                    or mark_waiting.get("slskd_transfer_requested_at")
                    or item.get("last_slskd_transfer_requested_at")
                )
                item["updated_at"] = now
                item["updated_at_iso"] = now_iso(now)
                touched["started"] += 1
            elif status in {"transfer_failed", "stale_failed_transfer_cleared", "waiting_record_missing"}:
                if item.get("state") not in {"downloading", "importing", "verified", "needs_you"} or row.get("retry_next_candidate"):
                    item["state"] = "searching"
                    item["current_source"] = None
                    retry_status = "safe_alternate_available" if row.get("retry_next_candidate") else "retry_pending"
                    item["last_slskd_autoresolve_status"] = retry_status
                    item["last_slskd_autoresolve_reason"] = (
                        row.get("reason")
                        or "SLSKD candidate failed; retrying next safe candidate"
                    )
                    item["last_slskd_autoresolve_at"] = now
                    item["last_slskd_autoresolve_at_iso"] = now_iso(now)
                    item["last_event"] = (
                        "SLSKD candidate retired; trying next safe candidate"
                        if row.get("retry_next_candidate")
                        else "SLSKD candidate needs retry follow-up"
                    )
                    item.pop("retry_after", None)
                    item.pop("retry_after_iso", None)
                    item.pop("needs_you_reason", None)
            elif status == "dry_run_safe":
                item["state"] = "searching"
                item["last_event"] = "SLSKD candidate would be auto-picked"
    return touched


RETRY_STATE_ATTEMPT_STATUSES = {
    "no_candidate_retry",
    "retry_scheduled",
    "searched_no_candidates",
    "provider_wait",
    "timeout",
}

RETRY_STATE_ATTEMPT_KINDS = {
    "queue_activity",
    "source_ladder_provider_summary",
    "source_runtime_budget_skipped",
    "source_started_timeout",
}

NON_RESULT_SOURCE_ATTEMPT_KINDS = {
    "source_runtime_budget_skipped",
    "source_started",
}


def queue_attempt_retry_state_signature(attempt):
    if not isinstance(attempt, dict):
        return None
    source = str(attempt.get("source") or attempt.get("provider_id") or attempt.get("provider") or "").strip().lower()
    status = str(attempt.get("status") or "").strip().lower()
    kind = str(attempt.get("kind") or "").strip().lower()
    if status not in RETRY_STATE_ATTEMPT_STATUSES and kind not in RETRY_STATE_ATTEMPT_KINDS:
        return None
    reason = str(attempt.get("reason") or attempt.get("failure_reason") or "").strip()
    if not reason and status not in RETRY_STATE_ATTEMPT_STATUSES:
        return None
    title = str(attempt.get("title") or attempt.get("filename") or "").strip()
    query = str(attempt.get("query") or title).strip()
    return (
        source,
        str(attempt.get("provider_id") or attempt.get("provider") or "").strip().lower(),
        kind,
        status,
        normalize(query),
        normalize(title),
        normalize(reason),
    )


def append_unique_queue_attempt(item, attempt, keep=240, *, dedupe_retry_state=True):
    if not isinstance(item, dict) or not isinstance(attempt, dict):
        return False
    cleaned = {key: value for key, value in attempt.items() if value not in (None, "")}
    if not cleaned:
        return False
    attempts = item.setdefault("attempts", [])
    if not isinstance(attempts, list):
        attempts = []
        item["attempts"] = attempts
    retry_state_signature = queue_attempt_retry_state_signature(cleaned)
    signature = (
        cleaned.get("source"),
        cleaned.get("status"),
        cleaned.get("ts"),
        cleaned.get("review_id"),
        cleaned.get("query"),
        cleaned.get("title") or cleaned.get("filename"),
    )
    for existing in attempts[-60:]:
        if not isinstance(existing, dict):
            continue
        if (
            dedupe_retry_state
            and retry_state_signature
            and queue_attempt_retry_state_signature(existing) == retry_state_signature
        ):
            return False
        existing_signature = (
            existing.get("source"),
            existing.get("status"),
            existing.get("ts"),
            existing.get("review_id"),
            existing.get("query"),
            existing.get("title") or existing.get("filename"),
        )
        if existing_signature == signature:
            return False
    attempts.append(cleaned)
    if len(attempts) > keep:
        del attempts[:-keep]
    return True


def queue_item_recorded_source_result_attempt_count(item, source):
    if not isinstance(item, dict):
        return 0
    source_key = source_order_attempt_key(source)
    if not source_key:
        return 0
    count = 0
    for attempt in item.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("kind") or "").strip().lower() in NON_RESULT_SOURCE_ATTEMPT_KINDS:
            continue
        attempt_source = source_order_attempt_key(
            attempt.get("source")
            or attempt.get("provider_id")
            or attempt.get("provider")
            or attempt.get("download_client")
        )
        if attempt_source == source_key:
            count += 1
    return count


def record_source_started_attempts(rows, source, note=None, now=None):
    source = str(source or "source").strip().lower()
    label = public_source_name(source) or source
    now = now or time.time()
    added = 0
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(
            str(value or "").strip()
            for value in (item.get("series"), item.get("issue"))
            if str(value or "").strip()
        )
        item["last_source_started_source"] = source
        item["last_source_started_at"] = now
        item["last_source_started_at_iso"] = now_iso(now)
        item["last_source_started_note"] = str(note or "").strip() or f"{label} source started"
        attempt = {
            "ts": now,
            "ts_iso": now_iso(now),
            "source": source,
            "provider": label,
            "provider_id": source,
            "status": "searching",
            "lifecycle_phase": "searching",
            "display_phase": "searching",
            "outcome": "in_progress",
            "retry_eligible": True,
            "reason": item["last_source_started_note"],
            "kind": "source_started",
            "title": title,
            "query": item.get("query") or title,
        }
        if append_unique_queue_attempt(item, attempt):
            touch_queue_item(item, now)
            added += 1
    return added


def source_runtime_budget_item_queue_id(item):
    if not isinstance(item, dict):
        return ""
    raw = json_dict(item.get("raw_json") or item.get("raw"))
    for value in (
        item.get("queue_id"),
        item.get("id"),
        item.get("key"),
        item.get("autopilot_queue_key"),
        item.get("queue_key"),
        raw.get("queue_id"),
        raw.get("id"),
        raw.get("key"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _inc_counter(mapping, key):
    key = str(key or "unknown").strip() or "unknown"
    mapping[key] = int(mapping.get(key) or 0) + 1


def source_runtime_budget_child_provider_context(
    source,
    rows,
    *,
    max_rows=RUNTIME_BUDGET_CHILD_PROVIDER_SAMPLE_LIMIT,
    job_limit=RUNTIME_BUDGET_CHILD_PROVIDER_JOB_LIMIT,
):
    source = source_order_attempt_key(source) or str(source or "").strip().lower()
    if source != "prowlarr":
        return {}
    if inkdrop_source_worker_coordinator is None:
        return {}
    try:
        if not INKDROP_STATE_DB.exists():
            return {}
    except Exception:
        return {}

    queue_ids = []
    seen_queue_ids = set()
    for item in rows or []:
        queue_id = source_runtime_budget_item_queue_id(item)
        if not queue_id or queue_id in seen_queue_ids:
            continue
        seen_queue_ids.add(queue_id)
        queue_ids.append(queue_id)
        if len(queue_ids) >= max(1, int(max_rows or 1)):
            break
    if not queue_ids:
        return {}

    providers = {}
    status_counts = {}
    schedule_counts = {}
    adapter_counts = {}
    job_count = 0
    error_count = 0
    missing_queue_count = 0
    for queue_id in queue_ids:
        try:
            planned = inkdrop_source_worker_coordinator.source_jobs_for_queue(
                INKDROP_STATE_DB,
                queue_id,
                include_operator=False,
                include_blocked=False,
                provider_ids=None,
                job_limit=job_limit,
            )
        except Exception:
            error_count += 1
            continue
        if not isinstance(planned, dict) or not planned.get("ok"):
            missing_queue_count += 1
            continue
        for job in planned.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            provider_id = str(job.get("provider_id") or "").strip().lower()
            if not provider_id.startswith("prowlarr_"):
                continue
            job_count += 1
            status = str(job.get("job_status") or "unknown").strip() or "unknown"
            schedule = str(job.get("schedule_state") or "unknown").strip() or "unknown"
            adapter = str(job.get("adapter_family") or "unknown").strip() or "unknown"
            _inc_counter(status_counts, status)
            _inc_counter(schedule_counts, schedule)
            _inc_counter(adapter_counts, adapter)
            entry = providers.setdefault(
                provider_id,
                {
                    "provider_id": provider_id,
                    "display_name": job.get("display_name") or provider_id,
                    "job_status": status,
                    "schedule_state": schedule,
                    "adapter_family": adapter,
                    "emits_download_task": bool(job.get("emits_download_task")),
                    "sampled_queue_ids": [],
                    "sampled_queue_count": 0,
                },
            )
            entry["sampled_queue_count"] = int(entry.get("sampled_queue_count") or 0) + 1
            if queue_id not in entry["sampled_queue_ids"] and len(entry["sampled_queue_ids"]) < 5:
                entry["sampled_queue_ids"].append(queue_id)
            if job.get("reason") and not entry.get("reason"):
                entry["reason"] = job.get("reason")
            if job.get("health_provider_ids") and not entry.get("health_provider_ids"):
                entry["health_provider_ids"] = list(job.get("health_provider_ids") or [])

    if not providers:
        return {}
    provider_rows = sorted(
        providers.values(),
        key=lambda row: (str(row.get("display_name") or "").lower(), str(row.get("provider_id") or "")),
    )
    return {
        "source": source,
        "attribution_scope": "parent_source_runtime_budget_skip",
        "sampled_queue_ids": queue_ids,
        "sampled_queue_count": len(queue_ids),
        "provider_ids": [row["provider_id"] for row in provider_rows],
        "provider_count": len(provider_rows),
        "job_count": job_count,
        "job_status_counts": dict(sorted(status_counts.items())),
        "schedule_state_counts": dict(sorted(schedule_counts.items())),
        "adapter_counts": dict(sorted(adapter_counts.items())),
        "providers": provider_rows,
        "planning_error_count": error_count,
        "planning_missing_queue_count": missing_queue_count,
    }


def record_source_runtime_budget_skip_attempts(source, rows, reason=None, now=None, child_provider_context=None):
    source = str(source or "source").strip().lower()
    label = public_source_name(source) or source
    now = now or time.time()
    reason = (
        str(reason or "").strip()
        or f"{label} did not start before the worker runtime budget; automatic retry scheduled"
    )
    child_provider_context = child_provider_context if isinstance(child_provider_context, dict) else {}
    added = 0
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        if item.get("state") in ACTIVE_QUEUE_STATES | {"needs_you"} | TERMINAL_QUEUE_STATES:
            continue
        title = " ".join(
            str(value or "").strip()
            for value in (item.get("series"), item.get("issue"))
            if str(value or "").strip()
        )
        attempt = {
            "ts": now,
            "ts_iso": now_iso(now),
            "source": source,
            "provider": label,
            "provider_id": source,
            "status": "retry_scheduled",
            "lifecycle_phase": "retry_later",
            "display_phase": "retry_later",
            "outcome": "no_candidate",
            "retry_eligible": True,
            "reason": reason,
            "failure_reason": reason,
            "kind": "source_runtime_budget_skipped",
            "title": title,
            "query": item.get("query") or title,
        }
        if child_provider_context:
            attempt["attribution_scope"] = child_provider_context.get("attribution_scope") or "parent_source_runtime_budget_skip"
            attempt["child_provider_ids"] = list(child_provider_context.get("provider_ids") or [])
            attempt["child_provider_count"] = child_provider_context.get("provider_count")
            attempt["child_provider_job_status_counts"] = child_provider_context.get("job_status_counts")
            attempt["child_provider_schedule_state_counts"] = child_provider_context.get("schedule_state_counts")
            attempt["child_provider_context"] = child_provider_context
        if append_unique_queue_attempt(item, attempt):
            item["last_source_runtime_budget_skipped_source"] = source
            item["last_source_runtime_budget_skipped_at"] = now
            item["last_source_runtime_budget_skipped_at_iso"] = now_iso(now)
            item["last_source_runtime_budget_skipped_reason"] = reason
            if child_provider_context:
                item["last_source_runtime_budget_child_provider_ids"] = list(child_provider_context.get("provider_ids") or [])
                item["last_source_runtime_budget_child_provider_count"] = child_provider_context.get("provider_count")
                item["last_source_runtime_budget_attribution_scope"] = child_provider_context.get("attribution_scope") or "parent_source_runtime_budget_skip"
            item["last_event"] = reason
            item.pop("needs_you_reason", None)
            touch_queue_item(item, now)
            added += 1
    return added


def slskd_provider_wait_status_value(status):
    status = str(status or "").strip().lower()
    return "provider_unavailable" if status in {"api_error", "provider_unavailable"} else "provider_wait"


def slskd_provider_wait_result_reason(row):
    row = row if isinstance(row, dict) else {}
    status = str(row.get("status") or "").strip().lower()
    detail = str(row.get("provider_error") or row.get("error") or row.get("reason") or "").strip()
    if status in {"api_error", "provider_unavailable"}:
        reason = "SLSKD provider unavailable; automatic retry scheduled"
    else:
        reason = "SLSKD provider wait; automatic retry scheduled"
    if detail and detail.lower() not in reason.lower():
        reason = f"{reason}: {detail}"
    return reason


def source_no_row_result_attempt_status(source, payload, row_count=0):
    source = str(source or "").strip().lower()
    if source not in {"prowlarr", "rss", "comicscodes", "slskd", "mangadex"} or not isinstance(payload, dict):
        return None
    if source == "prowlarr":
        if payload.get("skipped_busy"):
            return {
                "status": "provider_wait",
                "lifecycle_phase": "provider_wait",
                "reason": payload.get("reason") or "Prowlarr/SAB/qB worker is busy; retry scheduled",
            }
        if payload.get("command_timed_out") or payload.get("timed_out"):
            return {
                "status": "timeout",
                "lifecycle_phase": "retry_later",
                "reason": "Prowlarr command timed out before returning row results; automatic retry scheduled",
            }
        if payload.get("mode") == "source_worker":
            if payload.get("ok") is False:
                return {
                    "status": "error",
                    "lifecycle_phase": "retry_later",
                    "reason": payload.get("reason") or "Prowlarr source worker errored before returning a row result",
                }
            if int(payload.get("operator_required_count") or 0) > 0:
                return {
                    "status": "operator_required",
                    "lifecycle_phase": "manual_review",
                    "reason": "Prowlarr requires explicit operator input",
                }
            if int(payload.get("malformed_provider_result_count") or 0) > 0:
                return {
                    "status": "error",
                    "lifecycle_phase": "retry_later",
                    "reason": "Prowlarr returned malformed or missing provider status evidence",
                }
            if int(payload.get("skipped_provider_count") or 0) > 0 and not int(payload.get("missing_candidates") or 0):
                return {
                    "status": "skipped",
                    "lifecycle_phase": "observed",
                    "reason": "Prowlarr provider execution was explicitly skipped",
                }
            if int(payload.get("provider_wait_count") or 0) > 0:
                return {
                    "status": "provider_wait",
                    "lifecycle_phase": "provider_wait",
                    "reason": "Prowlarr source worker is waiting on provider health",
                }
        actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        reviews = result_review_rows(payload)
        if actions or reviews:
            return None
        missing = int(payload.get("missing_candidates") or 0)
        attempted = int(payload.get("attempted_total") or 0)
        budget_skipped = int(payload.get("budget_skipped_count") or 0)
        if payload.get("search_budget_exhausted") and not actions and not reviews:
            return {
                "status": "timeout",
                "lifecycle_phase": "retry_later",
                "reason": "Prowlarr search budget exhausted before finding a safe candidate; automatic retry scheduled",
            }
        if missing > 0 or attempted > 0 or budget_skipped > 0:
            return {
                "status": "searched_no_candidates",
                "lifecycle_phase": "searched_no_candidates",
                "reason": "Prowlarr checked; no safe automatic candidate for this row",
            }
        return None
    if source == "slskd":
        payload_status = str(payload.get("status") or "").strip().lower()
        if payload_status in SLSKD_PROVIDER_WAIT_RESULT_STATES or payload.get("provider_unavailable"):
            return {
                "status": slskd_provider_wait_status_value(payload_status),
                "lifecycle_phase": "provider_wait",
                "reason": slskd_provider_wait_result_reason(payload),
            }
        if payload.get("skipped_busy"):
            return {
                "status": "provider_wait",
                "lifecycle_phase": "provider_wait",
                "reason": payload.get("reason") or "SLSKD probe is already running; retry scheduled",
            }
        if payload.get("probe_budget_exhausted"):
            return {
                "status": "timeout",
                "lifecycle_phase": "retry_later",
                "reason": "SLSKD probe budget ended before this row returned a result; automatic retry scheduled",
            }
        if payload.get("ok") is False:
            return {
                "status": "error",
                "lifecycle_phase": "retry_later",
                "reason": payload.get("reason") or "SLSKD source errored before returning a row result",
            }
        selected = int(payload.get("selected_count") or 0)
        checked = int(payload.get("checked_count") or 0)
        auto = payload.get("auto_grab") if isinstance(payload.get("auto_grab"), dict) else {}
        auto_rows = [row for row in auto.get("rows") or [] if isinstance(row, dict)]
        if selected > 0 or checked > 0 or auto_rows:
            return {
                "status": "searched_no_candidates",
                "lifecycle_phase": "searched_no_candidates",
                "reason": "SLSKD pass did not return a concrete result for this row; automatic retry scheduled",
            }
        if int(row_count or 0) > 0:
            return {
                "status": "searched_no_candidates",
                "lifecycle_phase": "searched_no_candidates",
                "reason": "SLSKD did not select this row in the bounded source pass; automatic retry scheduled",
            }
        return None
    if source == "mangadex":
        if not any(
            key in payload
            for key in (
                "ok",
                "source",
                "rows_considered",
                "actions",
                "review",
                "skipped",
                "errors",
                "command_timed_out",
                "timed_out",
            )
        ):
            return None
        if payload.get("skipped_busy"):
            return {
                "status": "provider_wait",
                "lifecycle_phase": "provider_wait",
                "reason": payload.get("reason") or "MangaDex direct downloader is busy; retry scheduled",
            }
        if payload.get("command_timed_out") or payload.get("timed_out"):
            return {
                "status": "timeout",
                "lifecycle_phase": "retry_later",
                "reason": "MangaDex command timed out before returning row results; automatic retry scheduled",
            }
        actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        reviews = result_review_rows(payload)
        if actions or reviews:
            return None
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        if errors or payload.get("ok") is False:
            return {
                "status": "error",
                "lifecycle_phase": "retry_later",
                "reason": payload.get("reason") or "MangaDex source errored before returning a row result",
            }
        if int(payload.get("rows_considered") or 0) > 0 or int(row_count or 0) > 0:
            return {
                "status": "searched_no_candidates",
                "lifecycle_phase": "searched_no_candidates",
                "reason": "MangaDex checked; no matching chapter result for this row",
            }
        return None
    if payload.get("command_timed_out") or payload.get("timed_out"):
        return {
            "status": "timeout",
            "lifecycle_phase": "retry_later",
            "reason": f"{public_source_name(source) or source} command timed out before returning row results; automatic retry scheduled",
        }
    if payload.get("mode") == "source_worker":
        actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        reviews = result_review_rows(payload)
        if actions or reviews:
            return None
        if payload.get("ok") is False:
            return {
                "status": "error",
                "lifecycle_phase": "retry_later",
                "reason": payload.get("reason") or f"{public_source_name(source) or source} source worker errored before returning a row result",
            }
        if int(payload.get("operator_required_count") or 0) > 0:
            return {
                "status": "operator_required",
                "lifecycle_phase": "manual_review",
                "reason": f"{public_source_name(source) or source} requires explicit operator input",
            }
        if int(payload.get("malformed_provider_result_count") or 0) > 0:
            return {
                "status": "error",
                "lifecycle_phase": "retry_later",
                "reason": f"{public_source_name(source) or source} returned malformed or missing provider status evidence",
            }
        if int(payload.get("skipped_provider_count") or 0) > 0 and not int(payload.get("missing_candidates") or 0):
            return {
                "status": "skipped",
                "lifecycle_phase": "observed",
                "reason": f"{public_source_name(source) or source} provider execution was explicitly skipped",
            }
        if int(payload.get("provider_wait_count") or 0) > 0:
            return {
                "status": "provider_wait",
                "lifecycle_phase": "provider_wait",
                "reason": f"{public_source_name(source) or source} source worker is waiting on provider health",
            }
        missing = int(payload.get("missing_candidates") or 0)
        attempted = int(payload.get("attempted_total") or 0)
        blocked = int(payload.get("blocked_candidate_count") or 0)
        plans = int(payload.get("source_worker_schedule_plan_count") or 0)
        if missing > 0 or attempted > 0 or blocked > 0 or plans > 0 or int(row_count or 0) > 0:
            return {
                "status": "searched_no_candidates",
                "lifecycle_phase": "searched_no_candidates",
                "reason": f"{public_source_name(source) or source} source worker checked; no safe automatic candidate for this row",
            }
        return None
    status = str(payload.get("status") or "").strip().upper()
    if status in {"", "DISABLED"}:
        return None
    skips = payload.get("skips") if isinstance(payload.get("skips"), list) else []
    skip_reasons = {
        str(row.get("reason") or "").strip().lower()
        for row in skips
        if isinstance(row, dict)
    }
    blocked = int(payload.get("blocked_sources") or 0)
    backoff = int(payload.get("backoff_sources") or 0)
    if (
        payload.get("feed_status") == "backoff"
        or "feed_backoff_active" in skip_reasons
        or blocked
        or backoff
    ):
        return {
            "status": "provider_wait",
            "lifecycle_phase": "provider_wait",
            "reason": f"{public_source_name(source) or source} provider/backoff prevented a concrete row result",
        }
    if int(payload.get("failed") or 0) and not int(payload.get("candidates_found") or 0):
        return {
            "status": "error",
            "lifecycle_phase": "retry_later",
            "reason": f"{public_source_name(source) or source} source errored before finding a candidate",
        }
    missing_targets = int(payload.get("missing_targets") or 0)
    if missing_targets <= 0 and int(row_count or 0) > 0:
        actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        reviews = result_review_rows(payload)
        if not actions and not reviews:
            missing_targets = int(row_count or 0)
    if missing_targets <= 0:
        return None
    return {
        "status": "searched_no_candidates",
        "lifecycle_phase": "searched_no_candidates",
        "reason": f"{public_source_name(source) or source} checked; no matching candidate for this row",
    }


def source_worker_row_result_payload(payload, item):
    """Project an aggregate source-worker response onto one explicitly requested row."""

    if not isinstance(payload, dict) or payload.get("mode") != "source_worker":
        return payload
    queue_id = source_runtime_budget_item_queue_id(item)
    if not queue_id:
        return payload
    queue_id = str(queue_id)

    def targeted(rows):
        return [
            row for row in rows or []
            if isinstance(row, dict) and str(row.get("queue_id") or "") == queue_id
        ]

    actions = targeted(payload.get("actions"))
    reviews = targeted(result_review_rows(payload))
    source_worker = payload.get("source_worker") if isinstance(payload.get("source_worker"), dict) else {}
    runs = [
        run for run in source_worker_result_runs(source_worker)
        if str(run.get("queue_id") or "") == queue_id
    ]
    schedule = source_worker.get("schedule") if isinstance(source_worker.get("schedule"), dict) else {}
    plans = [
        plan for plan in schedule.get("plans") or []
        if isinstance(plan, dict) and str(plan.get("queue_id") or "") == queue_id
    ]
    selected = {
        str(value or "") for value in source_worker.get("selected_queue_ids") or payload.get("selected_queue_ids") or []
        if str(value or "")
    }
    if not actions and not reviews and not runs and not plans and queue_id not in selected:
        return None

    projected = {
        "mode": "source_worker",
        "ok": bool(payload.get("ok", True)),
        "actions": actions,
        "review": reviews,
        "reviews": reviews,
        "missing_candidates": 0,
        "attempted_total": 0,
        "provider_wait_count": 0,
        "blocked_candidate_count": 0,
        "source_worker_schedule_plan_count": len(plans),
        "provider_results": [],
        "operator_required_count": 0,
        "skipped_provider_count": 0,
        "malformed_provider_result_count": 0,
    }
    for run in runs:
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        if result.get("ok") is False:
            projected["ok"] = False
            projected["reason"] = result.get("reason") or payload.get("reason")
        summary = result.get("job_result_summary") if isinstance(result.get("job_result_summary"), dict) else {}
        projected["attempted_total"] += int(summary.get("total") or summary.get("attempts") or 0)
        projected["blocked_candidate_count"] += int(summary.get("blocked_candidate_count") or 0)
        statuses = summary.get("by_status") if isinstance(summary.get("by_status"), dict) else {}
        projected["provider_wait_count"] += int(statuses.get("provider_wait") or 0)
        projected["missing_candidates"] += int(statuses.get("searched_no_candidates") or 0)
        for job_result in result.get("job_results") or []:
            if not isinstance(job_result, dict):
                continue
            result_status = str(job_result.get("result_status") or "").strip().lower()
            runtime_results = [row for row in job_result.get("runtime_results") or [] if isinstance(row, dict)]
            attempts = [row for row in job_result.get("attempts") or [] if isinstance(row, dict)]
            fetch_evidence = {}
            for attempt in attempts:
                raw = attempt.get("raw") if isinstance(attempt.get("raw"), dict) else {}
                if isinstance(raw.get("fetch"), dict):
                    fetch_evidence = raw.get("fetch")
                    break
            result_count = sum(int(row.get("candidate_count") or 0) for row in runtime_results)
            if result_count <= 0 and fetch_evidence.get("payload_result_count") not in (None, ""):
                result_count = int(fetch_evidence.get("payload_result_count") or 0)
            projected["provider_results"].append(
                {
                    "provider_id": job_result.get("provider_id"),
                    "status": result_status or "unknown",
                    "result_count": result_count,
                    "candidate_count": sum(int(row.get("candidate_count") or 0) for row in runtime_results),
                    "safe_candidate_count": sum(int(row.get("safe_candidate_count") or 0) for row in runtime_results),
                    "variant_result_counts": list(fetch_evidence.get("variant_result_counts") or []),
                    "partial_error_count": len(fetch_evidence.get("partial_errors") or []),
                }
            )
            if result_status in {"provider_wait", "provider_unavailable"}:
                projected["provider_wait_count"] += 1
            elif result_status == "searched_no_candidates":
                projected["missing_candidates"] += 1
            elif result_status == "blocked":
                projected["blocked_candidate_count"] += 1
            elif result_status in {"operator_required", "manual_operator_required"}:
                projected["operator_required_count"] += 1
            elif result_status in {"skipped", "cooldown"}:
                projected["skipped_provider_count"] += 1
            elif result_status in {"", "unknown"}:
                projected["malformed_provider_result_count"] += 1
    for plan in plans:
        plan_status = str(plan.get("status") or "").strip().lower()
        if plan_status in {"provider_wait", "provider_unavailable", "waiting_for_retry"}:
            projected["provider_wait_count"] += 1
        elif plan_status in {"no_ready_jobs", "waiting", "cooldown"}:
            projected["missing_candidates"] += 1
        elif plan_status in {"blocked", "blocked_no_jobs", "no_jobs"}:
            projected["blocked_candidate_count"] += 1
    if runs and not any(
        int(projected.get(key) or 0)
        for key in ("attempted_total", "provider_wait_count", "missing_candidates", "blocked_candidate_count")
    ) and not actions and not reviews:
        projected["missing_candidates"] = 1
    return projected


def provider_terminal_result_evidence(source, payload):
    """Return privacy-safe terminal/result evidence without changing matching."""

    payload = payload if isinstance(payload, dict) else {}
    source = str(source or "").strip().lower()
    provider_results = [row for row in payload.get("provider_results") or [] if isinstance(row, dict)]
    completed_statuses = {"sent", "review", "searched_no_candidates", "blocked", "observed"}
    timeout_statuses = {"timeout", "timed_out", "provider_timeout"}
    wait_statuses = {"provider_unavailable", "provider_wait", "waiting_for_retry"}
    failed_statuses = timeout_statuses | wait_statuses | {"error", "failed"}
    completed = [row for row in provider_results if str(row.get("status") or "").lower() in completed_statuses]
    failed = [row for row in provider_results if str(row.get("status") or "").lower() in failed_statuses]
    result_count = sum(max(0, int(row.get("result_count") or 0)) for row in provider_results)
    candidate_count = sum(max(0, int(row.get("candidate_count") or 0)) for row in provider_results)
    partial_error_count = sum(max(0, int(row.get("partial_error_count") or 0)) for row in provider_results)
    if not provider_results and source == "rss":
        result_count = max(0, int(payload.get("feed_items") or 0))
        candidate_count = max(0, int(payload.get("candidates_found") or 0))
    timed_out = bool(payload.get("command_timed_out") or payload.get("timed_out")) or any(
        str(row.get("status") or "").lower() in timeout_statuses for row in provider_results
    )
    skipped_count = max(0, int(payload.get("skipped") or 0))
    error_count = len(payload.get("errors") or []) if isinstance(payload.get("errors"), list) else max(0, int(payload.get("failed") or 0))
    provider_statuses = {str(row.get("status") or "").strip().lower() for row in provider_results}
    provider_wait = bool(
        payload.get("feed_status") == "backoff"
        or int(payload.get("blocked_sources") or 0) > 0
        or int(payload.get("backoff_sources") or 0) > 0
        or int(payload.get("provider_wait_count") or 0) > 0
        or provider_statuses.intersection(wait_statuses)
    )
    operator_required = bool(
        int(payload.get("operator_required_count") or 0) > 0
        or provider_statuses.intersection({"operator_required", "manual_operator_required"})
    )
    malformed = bool(
        int(payload.get("malformed_provider_result_count") or 0) > 0
        or provider_statuses.intersection({"", "unknown"})
    )
    if (completed or result_count > 0 or candidate_count > 0) and (failed or partial_error_count or error_count):
        terminal_state = "partial"
    elif timed_out:
        terminal_state = "timeout"
    elif provider_wait:
        terminal_state = "provider_wait"
    elif operator_required:
        terminal_state = "operator_required"
    elif malformed:
        terminal_state = "malformed"
    elif payload.get("ok") is False or error_count or provider_statuses.intersection({"error", "failed"}):
        terminal_state = "error"
    elif (
        provider_results
        and all(str(row.get("status") or "").lower() in {"skipped", "cooldown"} for row in provider_results)
    ) or (skipped_count and result_count <= 0 and candidate_count <= 0):
        terminal_state = "skipped"
    elif result_count > 0 or candidate_count > 0:
        terminal_state = "results" if candidate_count > 0 else "zero_matching_results"
    elif provider_results or payload.get("status") or payload.get("feed_status"):
        terminal_state = "zero_results"
    else:
        terminal_state = "malformed"
    return {
        "terminal_state": terminal_state,
        "result_count": result_count,
        "candidate_count": candidate_count,
        "completed_provider_count": len(completed),
        "failed_provider_count": len(failed),
        "partial_error_count": partial_error_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "provider_results": provider_results,
    }


def record_source_no_row_result_attempts(source, payload, rows, attempt_counts_before, now=None):
    row_count = sum(1 for item in rows or [] if isinstance(item, dict))
    now = now or time.time()
    source = str(source or "").strip().lower()
    label = public_source_name(source) or source
    added = 0
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        if item.get("state") in ACTIVE_QUEUE_STATES | {"needs_you"} | TERMINAL_QUEUE_STATES:
            continue
        before_count = int((attempt_counts_before or {}).get(id(item)) or 0)
        current_count = int(queue_item_recorded_source_result_attempt_count(item, source))
        if current_count > before_count:
            continue
        row_payload = source_worker_row_result_payload(payload, item)
        if row_payload is None:
            continue
        status = source_no_row_result_attempt_status(source, row_payload, row_count=1)
        if not status:
            continue
        title = " ".join(
            str(value or "").strip()
            for value in (item.get("series"), item.get("issue"))
            if str(value or "").strip()
        )
        attempt = {
            "ts": now,
            "ts_iso": now_iso(now),
            "source": source,
            "provider": label,
            "provider_id": source,
            "status": status["status"],
            "lifecycle_phase": status["lifecycle_phase"],
            "reason": status["reason"],
            "failure_reason": status["reason"],
            "kind": "source_ladder_provider_summary",
            "title": title,
            "query": item.get("query") or title,
            "missing_targets": row_payload.get("missing_targets") if row_payload.get("missing_targets") not in (None, "") else 1,
            "candidates_found": row_payload.get("candidates_found"),
            "auto_grabbed": row_payload.get("auto_grabbed"),
            "sent_to_review": row_payload.get("sent_to_review"),
            "skipped": row_payload.get("skipped"),
            "failed": row_payload.get("failed"),
            "feed_status": row_payload.get("feed_status"),
            "source_status": row_payload.get("status"),
            "rows_considered": row_payload.get("rows_considered"),
            "timeout_seconds": row_payload.get("timeout_seconds"),
            "command_timed_out": bool(row_payload.get("command_timed_out") or row_payload.get("timed_out")),
            "search_budget_exhausted": bool(row_payload.get("search_budget_exhausted")) if "search_budget_exhausted" in row_payload else None,
            "search_budget_seconds": row_payload.get("search_budget_seconds"),
            "budget_skipped_count": row_payload.get("budget_skipped_count"),
            "missing_candidates": row_payload.get("missing_candidates"),
            "attempted_total": row_payload.get("attempted_total"),
            "error_count": len(row_payload.get("errors") or []) if isinstance(row_payload.get("errors"), list) else None,
            "provider_terminal_evidence": provider_terminal_result_evidence(source, row_payload),
        }
        if append_unique_queue_attempt(item, attempt, dedupe_retry_state=False):
            record_automation_source_outcome(item, status["status"], source, now, {"query": attempt["query"]})
            if item.get("current_source") == source:
                item["last_event"] = status["reason"]
                if status.get("lifecycle_phase") in {"searched_no_candidates", "retry_later", "provider_wait"}:
                    item["current_source"] = None
            clear_source_started_marker(item, source)
            touch_queue_item(item, now)
            added += 1
    return added


def slskd_checked_attempt_from_row(row, item, now):
    row = row if isinstance(row, dict) else {}

    def count_value(key):
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    status = str(row.get("status") or "checked").strip().lower() or "checked"
    candidate_count = count_value("candidate_count")
    detected_count = count_value("detected_count")
    detected_files = [file for file in (row.get("detected_files") or []) if isinstance(file, dict)]
    detected_file = detected_files[0] if detected_files else {}
    failed_count = count_value("failed_candidate_count")
    safe_count = effective_safe_slskd_count_for_row(row, item=item)
    review_count = count_value("auto_grab_review_count")
    blocked_count = count_value("auto_grab_blocked_count")
    lifecycle_phase = ""
    display_phase = ""
    outcome = ""
    retry_eligible = False
    if status in SLSKD_PROVIDER_WAIT_RESULT_STATES:
        status = slskd_provider_wait_status_value(status)
        reason = slskd_provider_wait_result_reason(row)
        lifecycle_phase = "provider_wait"
        display_phase = "provider_wait"
        outcome = "problem"
        retry_eligible = True
    elif status in SLSKD_TRANSIENT_RESULT_STATES:
        reason = "SLSKD source errored; automatic retry scheduled"
        lifecycle_phase = "retry_later"
        display_phase = "retry_later"
        outcome = "problem"
        retry_eligible = True
    elif status in SLSKD_NO_AUTOMATIC_RESULT_STATES:
        reason = "SLSKD checked; no candidates found" if candidate_count <= 0 else slskd_no_automatic_candidate_event(status)
        lifecycle_phase = "searched_no_candidates"
        display_phase = "no_candidate"
        outcome = "no_candidate"
        retry_eligible = True
    elif detected_count > 0 and detected_file.get("path"):
        reason = "SLSKD/manual staged file detected"
    elif detected_count > 0:
        status = "checked_no_staged_path"
        reason = "SLSKD reported a staged file but no local path was available; recheck scheduled"
    elif candidate_count > 0 and safe_count > 0:
        reason = "SLSKD checked; auto-grab candidate available"
    elif candidate_count > 0 and blocked_count > 0 and review_count <= 0:
        reason = "SLSKD checked; candidates were rejected by safety checks"
    elif candidate_count > 0:
        reason = "SLSKD checked; candidates were not safe enough to auto-pick"
    else:
        reason = "SLSKD checked"
    ts = slskd_row_checked_at(row) or now
    query = (
        row.get("query")
        or row.get("search_query")
        or " ".join(str(value or "").strip() for value in ((item or {}).get("series"), (item or {}).get("issue")) if str(value or "").strip())
    )
    attempt = {
        "ts": ts,
        "ts_iso": now_iso(ts),
        "source": "slskd",
        "provider": "SLSKD",
        "protocol": "soulseek",
        "download_client": "SLSKD",
        "status": status,
        "lifecycle_phase": lifecycle_phase,
        "display_phase": display_phase,
        "outcome": outcome,
        "retry_eligible": retry_eligible,
        "reason": reason,
        "failure_reason": reason if retry_eligible else "",
        "title": query,
        "query": query,
        "review_id": row.get("review_id"),
        "provider_error": row.get("provider_error") or row.get("error"),
        "provider_state": row.get("provider_state"),
        "provider_connected": row.get("provider_connected"),
        "provider_logged_in": row.get("provider_logged_in"),
        "provider_transitioning": row.get("provider_transitioning"),
        "candidate_count": candidate_count,
        "detected_count": detected_count,
        "failed_candidate_count": failed_count,
        "auto_grab_safe_count": safe_count,
        "auto_grab_review_count": review_count,
        "auto_grab_blocked_count": blocked_count,
        "query_offset": row.get("query_offset"),
        "query_total": row.get("query_total"),
    }
    if detected_file.get("path"):
        attempt["local_path"] = detected_file.get("path")
        attempt["staged_path"] = detected_file.get("path")
        attempt["detected_path"] = detected_file.get("path")
        attempt["filename"] = detected_file.get("filename") or detected_file.get("path")
        attempt["detected_filename"] = detected_file.get("filename")
        attempt["size"] = detected_file.get("size")
    return {key: value for key, value in attempt.items() if value not in (None, "")}


def apply_slskd_checked(queue, result):
    touched = {"checked": 0, "needs_you": 0, "attempts": 0}
    now = time.time()
    for row in (result or {}).get("checked") or []:
        row = enrich_slskd_checked_row(row)
        targets = row_queue_targets(queue, row)
        if not targets:
            continue
        for _, item in targets:
            item["autopilot_slskd_attempted_at"] = now
            item["autopilot_slskd_attempted_at_iso"] = now_iso(now)
            clear_source_started_marker(item, "slskd")
            item["last_slskd_at"] = now
            item["last_slskd_at_iso"] = now_iso(now)
            item["last_slskd_status"] = row.get("status")
            item["last_slskd_candidate_count"] = int(row.get("candidate_count") or 0)
            item["last_slskd_detected_count"] = int(row.get("detected_count") or 0)
            item["last_slskd_failed_candidate_count"] = int(row.get("failed_candidate_count") or 0)
            item["last_slskd_auto_grab_safe_count"] = effective_safe_slskd_count_for_row(row, item=item)
            item["last_slskd_auto_grab_review_count"] = int(row.get("auto_grab_review_count") or 0)
            item["last_slskd_auto_grab_blocked_count"] = int(row.get("auto_grab_blocked_count") or 0)
            touched["checked"] += 1
            if append_unique_queue_attempt(item, slskd_checked_attempt_from_row(row, item, now)):
                touched["attempts"] += 1
            if stale_slskd_detected_probe_row(row, now):
                clear_stale_slskd_import_signal(item, now, srow=row)
            elif str(row.get("status") or "").strip().lower() in SLSKD_PROVIDER_WAIT_RESULT_STATES and item.get("state") not in {"downloading", "importing", "verified", "needs_you"}:
                reason = slskd_provider_wait_result_reason(row)
                item["state"] = "queued"
                item["current_source"] = None
                item["last_event"] = reason
                item["last_slskd_provider_error"] = row.get("provider_error") or row.get("error")
                item["last_slskd_provider_wait_status"] = slskd_provider_wait_status_value(row.get("status"))
                item["last_slskd_provider_wait_at"] = now
                item["last_slskd_provider_wait_at_iso"] = now_iso(now)
                item.pop("needs_you_reason", None)
                schedule_retry_after(item, now, SLSKD_TRANSIENT_RETRY_SECONDS)
            elif str(row.get("status") or "").strip().lower() in SLSKD_TRANSIENT_RESULT_STATES and item.get("state") not in {"downloading", "importing", "verified"}:
                item["state"] = "queued"
                item["current_source"] = None
                item["last_event"] = "SLSKD source errored; automatic retry scheduled"
                item.pop("needs_you_reason", None)
                schedule_retry_after(item, now, SLSKD_TRANSIENT_RETRY_SECONDS)
            elif int(row.get("detected_count") or 0) > 0:
                item["current_source"] = "slskd"
                item["state"] = "importing"
                item["last_event"] = "SLSKD/manual staged file detected"
            elif str(row.get("status") or "") in SLSKD_NO_AUTOMATIC_RESULT_STATES and item.get("state") not in {"downloading", "importing", "verified"}:
                item["state"] = "searching"
                item["current_source"] = None
                item.pop("needs_you_reason", None)
                item["last_event"] = slskd_no_automatic_candidate_event(row.get("status"))
            elif int(row.get("candidate_count") or 0) > 0:
                item["state"] = "searching"
                item["current_source"] = None
                if int(row.get("auto_grab_safe_count") or 0) > 0:
                    item["last_event"] = "SLSKD candidate found for autopick"
                elif int(row.get("auto_grab_blocked_count") or 0) > 0 and int(row.get("auto_grab_review_count") or 0) <= 0:
                    item["last_event"] = "SLSKD candidates were rejected by safety checks; continuing source ladder"
                else:
                    item["last_event"] = "SLSKD candidates were not safe enough to auto-pick; continuing source ladder"
                item.pop("needs_you_reason", None)
    return touched


def apply_failed_retry_to_queue(queue, result):
    touched = apply_result_to_queue(queue, result, "failed_retry")
    now = time.time()
    for skipped in (result or {}).get("skipped") or []:
        reason = str(skipped.get("reason") or "")
        targets = row_queue_targets(queue, skipped)
        if not targets:
            continue
        for _, item in targets:
            item["last_failed_retry_reason"] = reason
            item["last_failed_retry_at"] = now
            item["last_failed_retry_at_iso"] = now_iso(now)
            if reason in {"already_active_or_ready"}:
                item["last_event"] = "failed retry skipped because a replacement is already active"
            elif reason in FAILED_RETRY_CONTINUE_REASONS and item.get("state") not in {"downloading", "importing", "verified"}:
                item["state"] = "searching"
                item["current_source"] = None
                if reason in {"alternate_attempt_budget_exhausted", "alternate_attempts_exhausted"}:
                    item["last_event"] = "failed retry budget exhausted; continuing source ladder"
                elif reason == "failed_record_not_currently_missing":
                    item["last_event"] = "failed retry record no longer matches a missing row"
                elif reason == "pack_candidate_not_actionable":
                    item["last_event"] = "failed retry skipped a non-actionable pack; continuing source ladder"
                else:
                    item["last_event"] = "failed retry already tried one alternate; continuing source ladder"
    return touched


def summarize_source_result(source, payload):
    if not isinstance(payload, dict):
        return {}
    if source in {"prowlarr", "failed_retry"}:
        reasons = collections.Counter(str(row.get("reason") or "review") for row in payload.get("review") or [])
        return {
            "dry_run": bool(payload.get("dry_run")),
            "mode": payload.get("mode"),
            "skipped_busy": bool(payload.get("skipped_busy")),
            "reason": payload.get("reason"),
            "failure_records_seen": int(payload.get("failure_records_seen") or 0),
            "missing_candidates": int(payload.get("missing_candidates") or 0),
            "attempted_total": int(payload.get("attempted_total") or 0),
            "command_timed_out": bool(payload.get("command_timed_out") or payload.get("timed_out")),
            "timeout_seconds": payload.get("timeout_seconds"),
            "action_count": len(payload.get("actions") or []),
            "review_count": len(payload.get("review") or []),
            "review_reasons": dict(reasons),
            "skipped_count": len(payload.get("skipped") or []),
            "search_budget_seconds": float(payload.get("search_budget_seconds") or 0),
            "search_budget_exhausted": bool(payload.get("search_budget_exhausted")),
            "budget_skipped_count": int(payload.get("budget_skipped_count") or 0),
            "suppressed_completed": int(payload.get("suppressed_completed") or payload.get("suppressed_manga_completed") or 0),
        }
    if source in {"rss", "comicscodes"}:
        return {
            "status": payload.get("status"),
            "mode": payload.get("mode"),
            "candidates_found": int(payload.get("candidates_found") or 0),
            "auto_grabbed": int(payload.get("auto_grabbed") or 0),
            "sent_to_review": int(payload.get("sent_to_review") or 0),
            "failed": int(payload.get("failed") or 0),
            "skipped": int(payload.get("skipped") or 0),
            "missing_targets": int(payload.get("missing_targets") or 0),
            "blocked_sources": int(payload.get("blocked_sources") or 0),
            "backoff_sources": int(payload.get("backoff_sources") or 0),
        }
    if source == "slskd":
        auto = payload.get("auto_grab") or {}
        return {
            "ok": bool(payload.get("ok", True)),
            "checked_count": int(payload.get("checked_count") or 0),
            "candidate_count": int(payload.get("candidate_count") or 0),
            "auto_grab_safe_count": int(payload.get("auto_grab_safe_count") or 0),
            "auto_grab_review_count": int(payload.get("auto_grab_review_count") or 0),
            "auto_grab_blocked_count": int(payload.get("auto_grab_blocked_count") or 0),
            "auto_grab_failed_count": int(payload.get("auto_grab_failed_count") or 0),
            "auto_grab_started_count": int(auto.get("started_count") or 0),
            "auto_grab_selected_count": int(auto.get("selected_count") or 0),
            "auto_grab_transient_error_count": int(auto.get("transient_error_count") or 0),
        }
    return {}


def source_summary_public_bit(source, summary):
    source_label = public_source_name(source) or str(source or "").strip() or "source"
    summary = summary if isinstance(summary, dict) else {}
    if summary.get("skipped"):
        reason = str(summary.get("reason") or "skipped").strip()
        if reason:
            return f"{source_label} skipped: {reason}"
        return f"{source_label} skipped"
    if source == "local":
        verified = int(summary.get("verified_count") or 0)
        active = int(summary.get("active_or_verified_count") or 0)
        eligible = int(summary.get("eligible_count") or 0)
        if verified or active:
            return f"{source_label} {verified or active} already covered"
        return f"{source_label} {eligible} eligible"
    if source in {"prowlarr", "failed_retry"}:
        actions = int(summary.get("action_count") or 0)
        reviews = int(summary.get("review_count") or 0)
        missing = int(summary.get("missing_candidates") or 0)
        if actions:
            return f"{source_label} {actions} sent"
        if reviews:
            return f"{source_label} {reviews} review"
        if summary.get("search_budget_exhausted"):
            return f"{source_label} timed out"
        return f"{source_label} {missing} candidates"
    if source in {"rss", "comicscodes"}:
        grabbed = int(summary.get("auto_grabbed") or 0)
        candidates = int(summary.get("candidates_found") or 0)
        reviews = int(summary.get("sent_to_review") or 0)
        if grabbed:
            return f"{source_label} {grabbed} grabbed"
        if reviews:
            return f"{source_label} {reviews} review"
        return f"{source_label} {candidates} candidates"
    if source == "slskd":
        started = int(summary.get("auto_grab_started_count") or 0)
        safe = int(summary.get("auto_grab_safe_count") or 0)
        candidates = int(summary.get("candidate_count") or 0)
        checked = int(summary.get("checked_count") or 0)
        if started:
            return f"{source_label} {started} started"
        if safe:
            return f"{source_label} {safe} safe"
        return f"{source_label} {candidates} candidates / {checked} checked"
    return source_label


def autopilot_series_run_activity(result):
    result = result if isinstance(result, dict) else {}
    sources = result.get("sources") if isinstance(result.get("sources"), dict) else {}
    errors = [row for row in (result.get("errors") or []) if isinstance(row, dict)]
    state_counts = result.get("source_state_counts") if isinstance(result.get("source_state_counts"), dict) else {}
    moved_states = {"downloading", "importing", "verified", "source_wait"}
    productive = False
    for counts in state_counts.values():
        if isinstance(counts, dict) and any(int(counts.get(state) or 0) > 0 for state in moved_states):
            productive = True
            break
    for source, summary in sources.items():
        if not isinstance(summary, dict):
            continue
        if source in {"prowlarr", "failed_retry"} and int(summary.get("action_count") or 0) > 0:
            productive = True
        if source in {"rss", "comicscodes"} and int(summary.get("auto_grabbed") or 0) > 0:
            productive = True
        if source == "slskd" and int(summary.get("auto_grab_started_count") or 0) > 0:
            productive = True
    if errors:
        return "problem", "problem", "source errors recorded"
    if productive:
        return "productive", "searching", "automation moved at least one row"
    return "no_candidate", "retry_later", "no safe automatic candidate found"


def record_autopilot_series_history(result, rows=None):
    if inkdrop_state is None or not isinstance(result, dict):
        return {"ok": False, "reason": "inkdrop_state_unavailable"}
    series = str(result.get("series") or "").strip()
    if not series:
        return {"ok": False, "reason": "missing_series"}
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    identity = str(result.get("queue_identity") or "").strip()
    if not identity and rows:
        identity = series_summary_identity(rows[0])
    series_id = identity if ":" in identity else None
    sources = result.get("sources") if isinstance(result.get("sources"), dict) else {}
    source_order = []
    if rows:
        source_order = source_order_for_rows(rows)
    if not source_order:
        source_order = [source for source in SOURCE_ORDER if source in sources]
    for source in sources:
        if source not in source_order:
            source_order.append(source)
    source_bits = [
        source_summary_public_bit(source, sources.get(source))
        for source in source_order
        if source in sources
    ]
    outcome, display_phase, reason = autopilot_series_run_activity(result)
    missing_rows = int(result.get("missing_rows") or 0)
    message_bits = [
        f"{series}: source ladder checked {missing_rows} row{'s' if missing_rows != 1 else ''}",
        "; ".join(bit for bit in source_bits if bit),
    ]
    if reason:
        message_bits.append(reason)
    message = " | ".join(bit for bit in message_bits if bit)
    raw = {
        "status": "searched_no_candidates" if outcome == "no_candidate" else ("searching" if outcome == "productive" else "problem"),
        "outcome": outcome,
        "display_phase": display_phase,
        "series": series,
        "queue_identity": identity,
        "missing_rows": missing_rows,
        "source_order": source_order,
        "sources": sources,
        "source_state_counts": result.get("source_state_counts") or {},
        "slskd_checked": result.get("slskd_checked") or {},
        "slskd_auto_grab": result.get("slskd_auto_grab") or {},
        "errors": result.get("errors") or [],
        "history_kind": "autopilot_series_run",
    }
    try:
        return inkdrop_state.record_history_event(
            INKDROP_STATE_DB,
            event_type="autopilot_series_run",
            entity_type="series",
            entity_id=identity or f"title:{normalize(series)}",
            series_id=series_id,
            source="autopilot",
            message=message,
            raw=raw,
        )
    except Exception as exc:
        log("autopilot_series_history_failed", series=series, error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def queue_worker_env(extra=None):
    env = {"INKDROP_QUEUE_MODE": "1"}
    if extra:
        env.update(extra)
    return env


def duration_label(seconds):
    try:
        seconds = int(float(seconds or 0))
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return ""
    if seconds < 90:
        return f"{seconds}s"
    minutes = max(1, round(seconds / 60))
    return f"{minutes}m"


def prowlarr_command_timeout_headroom_seconds(source_lock_wait_seconds=0):
    try:
        lock_wait = max(0, int(source_lock_wait_seconds or 0))
    except (TypeError, ValueError):
        lock_wait = 0
    return lock_wait + RUNTIME_CHILD_CLEANUP_SECONDS + PROWLARR_COMMAND_TIMEOUT_HEADROOM_SECONDS


def prowlarr_min_command_timeout_seconds(search_budget_seconds, source_lock_wait_seconds=0):
    try:
        search_budget = max(0, int(search_budget_seconds or 0))
    except (TypeError, ValueError):
        search_budget = 0
    headroom = prowlarr_command_timeout_headroom_seconds(source_lock_wait_seconds)
    return max(15, min(300, search_budget + headroom))


def ensure_prowlarr_timeout_headroom(args):
    if args is None:
        return args
    min_timeout = prowlarr_min_command_timeout_seconds(
        getattr(args, "prowlarr_search_budget_seconds", 0),
        getattr(args, "source_lock_wait_seconds", 0),
    )
    for field in ("prowlarr_command_timeout_seconds", "failed_retry_command_timeout_seconds"):
        try:
            current = int(getattr(args, field) or 0)
        except (TypeError, ValueError):
            current = 0
        setattr(args, field, max(15, min(300, max(current, min_timeout))))
    return args


def normalized_source_key(source):
    source = str(source or "").strip().lower()
    aliases = {
        "prowlarr/sab/qb": "prowlarr",
        "prowlarr/sab/qbit": "prowlarr",
        "prowlarr / sab / qb": "prowlarr",
        "soulseek": "slskd",
        "soulseek/slskd": "slskd",
    }
    return aliases.get(source, source)


def source_budget_seconds(source, args):
    source = normalized_source_key(source)
    field = {
        "prowlarr": "prowlarr_search_budget_seconds",
        "failed_retry": "failed_retry_command_timeout_seconds",
        "slskd": "slskd_probe_budget_seconds",
        "mangadex": "mangadex_command_timeout_seconds",
    }.get(source)
    if not field:
        return None
    try:
        return int(getattr(args, field) or 0)
    except (TypeError, ValueError):
        return None


def source_progress_note(source, note, args):
    note = str(note or "").strip()
    source = normalized_source_key(source)
    budget = source_budget_seconds(source, args)
    budget_text = duration_label(budget)
    if source == "slskd" and budget_text and "budget" not in note.lower() and "up to" not in note.lower():
        return f"{note}; SLSKD probe budget {budget_text}"
    if source == "prowlarr" and budget_text and "budget" not in note.lower():
        return f"{note}; Prowlarr search budget {budget_text}"
    if source == "mangadex" and budget_text and "budget" not in note.lower():
        return f"{note}; MangaDex command budget {budget_text}"
    return note


def active_task_payload(args, progress):
    source = str((progress or {}).get("current_source") or "").strip()
    series = str((progress or {}).get("current_series") or "").strip()
    note = str((progress or {}).get("progress_note") or "").strip()
    source_key = normalized_source_key(source)
    public_source = public_source_name(source_key) or source
    if not (source or series):
        return None
    budget = source_budget_seconds(source_key, args)
    detail_bits = []
    if series:
        detail_bits.append(series)
    if public_source:
        detail_bits.append(public_source)
    if note:
        detail_bits.append(note)
    if budget:
        detail_bits.append(f"budget {duration_label(budget)}")
    task = {
        "series": series,
        "source": source_key,
        "source_label": public_source,
        "note": note,
        "detail": " · ".join(bit for bit in detail_bits if bit),
        "budget_seconds": budget,
        "updated_at": time.time(),
        "updated_at_iso": now_iso(),
    }
    if source_key == "slskd":
        task.update({
            "max_total": getattr(args, "slskd_max_total", None),
            "max_per_series": getattr(args, "slskd_max_per_series", None),
            "max_queries": getattr(args, "slskd_max_queries", None),
            "wait_seconds": getattr(args, "slskd_wait_seconds", None),
            "auto_grab_max": getattr(args, "slskd_auto_grab_max", None),
        })
    elif source_key == "prowlarr":
        task.update({
            "max_queries_per_issue": getattr(args, "prowlarr_max_queries_per_issue", None),
            "request_timeout_seconds": getattr(args, "prowlarr_timeout_seconds", None),
            "command_timeout_seconds": getattr(args, "prowlarr_command_timeout_seconds", None),
        })
    return task


def prowlarr_worker_env(args):
    try:
        timeout_seconds = float(args.prowlarr_timeout_seconds or 12)
    except (TypeError, ValueError):
        timeout_seconds = 12.0
    timeout_seconds = max(5.0, min(timeout_seconds, 30.0))
    return queue_worker_env({"INKDROP_PROWLARR_SEARCH_TIMEOUT_SECONDS": f"{timeout_seconds:g}"})


def _positive_int_value(value, default=0):
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        parsed = 0
    return parsed if parsed > 0 else int(default or 0)


def source_worker_prowlarr_queue_ids(rows):
    queue_ids = []
    seen = set()
    for item in rows or []:
        queue_id = source_runtime_budget_item_queue_id(item)
        if not queue_id or queue_id in seen:
            continue
        seen.add(queue_id)
        queue_ids.append(queue_id)
    return queue_ids


def source_worker_prowlarr_provider_ids(rows):
    context = source_runtime_budget_child_provider_context(
        "prowlarr",
        rows,
        max_rows=RUNTIME_BUDGET_CHILD_PROVIDER_SAMPLE_LIMIT,
        job_limit=RUNTIME_BUDGET_CHILD_PROVIDER_JOB_LIMIT,
    )
    provider_ids = []
    seen = set()
    for provider_id in context.get("provider_ids") or []:
        provider_id = str(provider_id or "").strip().lower()
        if not provider_id.startswith("prowlarr_") or provider_id in seen:
            continue
        seen.add(provider_id)
        provider_ids.append(provider_id)
    return provider_ids, context


def source_worker_prowlarr_limit(args, row_count):
    limits = [row_count]
    for attr in ("missing_max_per_series", "missing_max_total"):
        value = _positive_int_value(getattr(args, attr, 0), 0)
        if value > 0:
            limits.append(value)
    return max(1, min(value for value in limits if value > 0))


def source_worker_prowlarr_argv(queue_ids, provider_ids, args):
    queue_ids = [str(value).strip() for value in queue_ids or [] if str(value or "").strip()]
    provider_ids = [str(value).strip() for value in provider_ids or [] if str(value or "").strip()]
    limit = source_worker_prowlarr_limit(args, len(queue_ids) or 1)
    argv = [str(INKDROP_STATE_DB)]
    for queue_id in queue_ids:
        argv.extend(["--queue-id", queue_id])
    for provider_id in provider_ids:
        argv.extend(["--provider-id", provider_id])
    if queue_ids and getattr(args, "force", False):
        argv.append("--include-waiting")
    argv.extend(
        [
            "--queue-limit",
            str(max(1, len(queue_ids))),
            "--eligible-limit",
            str(limit),
            "--job-limit",
            str(_positive_int_value(getattr(args, "source_worker_job_limit", 0), SOURCE_WORKER_PROWLARR_DEFAULT_JOB_LIMIT)),
            "--run-limit",
            "1",
            "--execute",
            "--allow-network",
            "--handoff-download-clients",
            "--timeout-seconds",
            str(max(5, min(_positive_int_value(getattr(args, "prowlarr_timeout_seconds", 0), DEFAULT_PROWLARR_TIMEOUT_SECONDS), 30))),
            "--provider-timeout-window-seconds",
            str(
                bounded_int_value(
                    getattr(args, "prowlarr_provider_timeout_window_seconds", None),
                    DEFAULT_PROWLARR_PROVIDER_TIMEOUT_WINDOW_SECONDS,
                    0,
                    24 * 3600,
                )
            ),
            "--provider-timeout-threshold",
            str(
                bounded_int_value(
                    getattr(args, "prowlarr_provider_timeout_threshold", None),
                    DEFAULT_PROWLARR_PROVIDER_TIMEOUT_THRESHOLD,
                    0,
                    100,
                )
            ),
            "--provider-timeout-cooldown-seconds",
            str(
                bounded_int_value(
                    getattr(args, "prowlarr_provider_timeout_cooldown_seconds", None),
                    DEFAULT_PROWLARR_PROVIDER_TIMEOUT_COOLDOWN_SECONDS,
                    0,
                    24 * 3600,
                )
            ),
            "--provider-fetch-failure-window-seconds",
            str(
                bounded_int_value(
                    getattr(args, "prowlarr_provider_fetch_failure_window_seconds", None),
                    DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS,
                    0,
                    24 * 3600,
                )
            ),
            "--provider-fetch-failure-threshold",
            str(
                bounded_int_value(
                    getattr(args, "prowlarr_provider_fetch_failure_threshold", None),
                    DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_THRESHOLD,
                    0,
                    100,
                )
            ),
            "--provider-fetch-failure-cooldown-seconds",
            str(
                bounded_int_value(
                    getattr(args, "prowlarr_provider_fetch_failure_cooldown_seconds", None),
                    DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS,
                    0,
                    24 * 3600,
                )
            ),
            "--source-memory-db",
            str(INKDROP_STATE_DB),
            "--full-output",
        ]
    )
    for host in source_worker_prowlarr_allowed_hosts():
        argv.extend(["--allowed-host", host])
    if not getattr(args, "dry_run", False):
        argv.append("--write")
    return argv


def source_worker_rss_queue_ids(rows):
    return source_worker_prowlarr_queue_ids(rows)


def source_worker_rss_limit(args, row_count):
    limits = [row_count]
    for attr in ("rss_discovery_max_per_series", "rss_discovery_max_auto"):
        value = _positive_int_value(getattr(args, attr, 0), 0)
        if value > 0:
            limits.append(value)
    return max(1, min(value for value in limits if value > 0))


def source_worker_rss_allowed_hosts(args):
    hosts = normalized_host_list(getattr(args, "rss_source_worker_allowed_hosts", None) or [])
    if not hosts:
        feed_host = host_from_url(getattr(args, "rss_feed_url", ""))
        hosts = normalized_host_list([feed_host, *SOURCE_WORKER_RSS_ALLOWED_HOST_FALLBACKS])
    return hosts


def source_worker_rss_direct_allowed_hosts(args):
    hosts = normalized_host_list(getattr(args, "rss_source_worker_direct_allowed_hosts", None) or [])
    if not hosts:
        hosts = normalized_host_list(SOURCE_WORKER_RSS_DIRECT_ALLOWED_HOST_FALLBACKS)
    return hosts


def source_worker_rss_argv(queue_ids, args):
    queue_ids = [str(value).strip() for value in queue_ids or [] if str(value or "").strip()]
    limit = source_worker_rss_limit(args, len(queue_ids) or 1)
    timeout_seconds = bounded_int_value(
        getattr(args, "rss_source_worker_http_timeout_seconds", None),
        DEFAULT_RSS_SOURCE_WORKER_HTTP_TIMEOUT_SECONDS,
        5,
        30,
    )
    argv = [str(INKDROP_STATE_DB)]
    for queue_id in queue_ids:
        argv.extend(["--queue-id", queue_id])
    if queue_ids and getattr(args, "force", False):
        argv.append("--include-waiting")
    argv.extend(
        [
            "--provider-id",
            "rss",
            "--queue-limit",
            str(max(1, len(queue_ids))),
            "--eligible-limit",
            str(limit),
            "--job-limit",
            str(_positive_int_value(getattr(args, "source_worker_job_limit", 0), SOURCE_WORKER_RSS_DEFAULT_JOB_LIMIT)),
            "--run-limit",
            "1",
            "--execute",
            "--allow-network",
            "--timeout-seconds",
            str(timeout_seconds),
            "--provider-timeout-window-seconds",
            str(
                bounded_int_value(
                    getattr(args, "rss_provider_timeout_window_seconds", None),
                    DEFAULT_RSS_PROVIDER_TIMEOUT_WINDOW_SECONDS,
                    0,
                    24 * 3600,
                )
            ),
            "--provider-timeout-threshold",
            str(
                bounded_int_value(
                    getattr(args, "rss_provider_timeout_threshold", None),
                    DEFAULT_RSS_PROVIDER_TIMEOUT_THRESHOLD,
                    0,
                    100,
                )
            ),
            "--provider-timeout-cooldown-seconds",
            str(
                bounded_int_value(
                    getattr(args, "rss_provider_timeout_cooldown_seconds", None),
                    DEFAULT_RSS_PROVIDER_TIMEOUT_COOLDOWN_SECONDS,
                    0,
                    24 * 3600,
                )
            ),
            "--provider-fetch-failure-window-seconds",
            str(
                bounded_int_value(
                    getattr(args, "rss_provider_fetch_failure_window_seconds", None),
                    DEFAULT_RSS_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS,
                    0,
                    24 * 3600,
                )
            ),
            "--provider-fetch-failure-threshold",
            str(
                bounded_int_value(
                    getattr(args, "rss_provider_fetch_failure_threshold", None),
                    DEFAULT_RSS_PROVIDER_FETCH_FAILURE_THRESHOLD,
                    0,
                    100,
                )
            ),
            "--provider-fetch-failure-cooldown-seconds",
            str(
                bounded_int_value(
                    getattr(args, "rss_provider_fetch_failure_cooldown_seconds", None),
                    DEFAULT_RSS_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS,
                    0,
                    24 * 3600,
                )
            ),
            "--source-memory-db",
            str(INKDROP_STATE_DB),
            "--full-output",
        ]
    )
    for host in source_worker_rss_allowed_hosts(args):
        argv.extend(["--allowed-host", host])
    if not getattr(args, "dry_run", False):
        argv.append("--write")
        direct_hosts = source_worker_rss_direct_allowed_hosts(args)
        if direct_hosts:
            argv.extend(
                [
                    "--stage-direct",
                    "--allow-direct-network",
                    "--staging-root",
                    str(SOURCE_WORKER_STAGING_ROOT),
                ]
            )
            for host in direct_hosts:
                argv.extend(["--direct-allowed-host", host])
    return argv


def source_worker_result_runs(payload):
    if not isinstance(payload, dict):
        return []
    batch = payload.get("batch") if isinstance(payload.get("batch"), dict) else payload
    return [row for row in (batch.get("runs") or []) if isinstance(row, dict)]


def source_worker_run_wanted_item(run):
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    wanted = result.get("wanted_item") if isinstance(result.get("wanted_item"), dict) else {}
    return wanted


def source_worker_attempt_action_row(run, job_result, attempt):
    attempt = dict(attempt or {})
    wanted = source_worker_run_wanted_item(run)
    provider_id = str(
        attempt.get("provider_id")
        or (job_result or {}).get("provider_id")
        or next(iter(run.get("provider_ids") or []), "")
    ).strip()
    if provider_id:
        attempt.setdefault("provider_id", provider_id)
        attempt.setdefault("source", provider_id)
    attempt.setdefault("queue_id", run.get("queue_id"))
    attempt.setdefault("series", wanted.get("series") or wanted.get("series_title") or wanted.get("title"))
    attempt.setdefault("issue", wanted.get("issue_number") or wanted.get("issue"))
    attempt.setdefault("issue_number", wanted.get("issue_number") or wanted.get("issue"))
    attempt.setdefault("query", wanted.get("query"))
    attempt.setdefault("metadata_provider", wanted.get("metadata_provider"))
    attempt.setdefault("metadata_id", wanted.get("metadata_id"))
    if wanted.get("kapowarr_id"):
        attempt.setdefault("kapowarr_id", wanted.get("kapowarr_id"))
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def prowlarr_source_worker_payload_to_result(payload, series, source="prowlarr"):
    source = str(source or "prowlarr").strip().lower() or "prowlarr"
    actions = []
    reviews = []
    errors = []
    attempted_total = 0
    missing_candidates = 0
    safe_candidates = 0
    provider_wait = 0
    blocked = 0
    selected_queue_ids = []
    budget_skipped = []
    schedule_plan_count = 0
    schedule_blocked_count = 0
    schedule_provider_wait_count = 0
    schedule_no_ready_count = 0
    batch = payload.get("batch") if isinstance(payload, dict) and isinstance(payload.get("batch"), dict) else payload
    if isinstance(batch, dict):
        selected_queue_ids = list(batch.get("selected_queue_ids") or [])
        budget_skipped = list(batch.get("budget_skipped_queue_ids") or [])
        schedule = batch.get("schedule") if isinstance(batch.get("schedule"), dict) else {}
        for plan in schedule.get("plans") or []:
            if not isinstance(plan, dict):
                continue
            schedule_plan_count += 1
            plan_status = str(plan.get("status") or "").strip().lower()
            if plan_status in {"blocked", "blocked_no_jobs", "no_jobs"}:
                schedule_blocked_count += 1
            elif plan_status in {"provider_wait", "provider_unavailable"}:
                schedule_provider_wait_count += 1
            elif plan_status in {"no_ready_jobs", "waiting", "cooldown"}:
                schedule_no_ready_count += 1
    for run in source_worker_result_runs(payload):
        if run.get("queue_id") and run.get("queue_id") not in selected_queue_ids:
            selected_queue_ids.append(run.get("queue_id"))
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        summary = result.get("job_result_summary") if isinstance(result.get("job_result_summary"), dict) else {}
        attempted_total += int(summary.get("total") or summary.get("attempts") or 0)
        safe_candidates += int(summary.get("safe_candidate_count") or 0)
        provider_wait += int((summary.get("by_status") or {}).get("provider_wait") or 0) if isinstance(summary.get("by_status"), dict) else 0
        blocked += int(summary.get("blocked_candidate_count") or 0)
        for job_result in result.get("job_results") or []:
            if not isinstance(job_result, dict):
                continue
            result_status = str(job_result.get("result_status") or "").strip().lower()
            if result_status in {"searched_no_candidates", "unknown"}:
                missing_candidates += 1
            for attempt in job_result.get("attempts") or []:
                if not isinstance(attempt, dict):
                    continue
                status = str(attempt.get("status") or "").strip().lower()
                row = source_worker_attempt_action_row(run, job_result, attempt)
                if status == "sent":
                    actions.append(row)
                elif status in {"review", "blocked"}:
                    reviews.append(row)
                elif status in {"provider_wait", "provider_unavailable"}:
                    provider_wait += 1
                elif status in {"searched_no_candidates", "no_candidate_retry", "retry_scheduled"}:
                    missing_candidates += 1
    if budget_skipped:
        missing_candidates += len(budget_skipped)
    if schedule_blocked_count:
        blocked += schedule_blocked_count
    if schedule_provider_wait_count:
        provider_wait += schedule_provider_wait_count
    if schedule_no_ready_count:
        missing_candidates += schedule_no_ready_count
    converted = {
        "ok": bool((payload or {}).get("ok", True)),
        "mode": "source_worker",
        "source": source,
        "series": series,
        "actions": actions,
        "review": reviews,
        "reviews": reviews,
        "missing_candidates": missing_candidates,
        "attempted_total": attempted_total,
        "safe_candidates": safe_candidates,
        "provider_wait_count": provider_wait,
        "blocked_candidate_count": blocked,
        "budget_skipped_count": len(budget_skipped),
        "source_worker_schedule_plan_count": schedule_plan_count,
        "source_worker_schedule_blocked_count": schedule_blocked_count,
        "source_worker_schedule_provider_wait_count": schedule_provider_wait_count,
        "source_worker_schedule_no_ready_count": schedule_no_ready_count,
        "selected_queue_ids": selected_queue_ids,
        "source_worker": batch if isinstance(batch, dict) else {},
    }
    if isinstance(payload, dict):
        if payload.get("skipped_busy"):
            converted["skipped_busy"] = True
        if payload.get("reason"):
            converted["reason"] = payload.get("reason")
    return converted


def source_worker_queue_targets(queue, row):
    queue_id = str((row or {}).get("queue_id") or "").strip()
    if queue_id:
        item = (queue.get("items") or {}).get(queue_id) if isinstance(queue, dict) else None
        if isinstance(item, dict) and item.get("present_in_watch", True):
            return [(queue_id, item)]
    return row_queue_targets(queue, row)


def source_worker_handoff_records(result):
    records = []
    source_worker = result.get("source_worker") if isinstance(result, dict) else {}
    for run in source_worker_result_runs(source_worker):
        run_result = run.get("result") if isinstance(run.get("result"), dict) else {}
        handoff = run_result.get("download_client_handoff") if isinstance(run_result.get("download_client_handoff"), dict) else {}
        for record in handoff.get("attempt_records") or []:
            if not isinstance(record, dict) or not record.get("ok"):
                continue
            item = dict(record)
            item.setdefault("queue_id", run.get("queue_id"))
            wanted = source_worker_run_wanted_item(run)
            item.setdefault("series", wanted.get("series") or wanted.get("series_title") or wanted.get("title"))
            item.setdefault("issue", wanted.get("issue_number") or wanted.get("issue"))
            records.append(item)
    return records


def source_worker_direct_stage_records(result):
    records = []
    source_worker = result.get("source_worker") if isinstance(result, dict) else {}
    for run in source_worker_result_runs(source_worker):
        run_result = run.get("result") if isinstance(run.get("result"), dict) else {}
        direct_stage = run_result.get("direct_stage") if isinstance(run_result.get("direct_stage"), dict) else {}
        for record in direct_stage.get("attempt_records") or []:
            if not isinstance(record, dict) or not record.get("ok"):
                continue
            item = dict(record)
            item.setdefault("queue_id", run.get("queue_id"))
            wanted = source_worker_run_wanted_item(run)
            item.setdefault("series", wanted.get("series") or wanted.get("series_title") or wanted.get("title"))
            item.setdefault("issue", wanted.get("issue_number") or wanted.get("issue"))
            records.append(item)
    return records


def apply_source_worker_prowlarr_result_to_queue(queue, result):
    touched = {"actions": 0, "reviews": 0, "handoffs": 0}
    handoff_queue_ids = set()
    now = time.time()
    for record in source_worker_handoff_records(result):
        targets = source_worker_queue_targets(queue, record)
        if not targets:
            continue
        for _, item in targets:
            queue_id = str(record.get("queue_id") or item.get("key") or "").strip()
            if queue_id:
                handoff_queue_ids.add(queue_id)
            item["state"] = record.get("state") or "downloading"
            item["current_source"] = record.get("current_source") or "download_client"
            item["last_event"] = record.get("last_event") or "download client accepted source-worker candidate"
            item["last_action_at"] = now
            item["last_action_at_iso"] = now_iso(now)
            clear_source_started_marker(item, "prowlarr")
            item.setdefault("attempts", []).append(
                {
                    "ts": now,
                    "ts_iso": now_iso(now),
                    "source": item["current_source"],
                    "provider_id": item["current_source"],
                    "status": "download_started" if item["state"] == "downloading" else item["state"],
                    "reason": item["last_event"],
                    "kind": "source_worker_download_client_handoff",
                }
            )
            touched["handoffs"] += 1
    filtered = dict(result or {})
    if handoff_queue_ids:
        filtered["actions"] = [
            action
            for action in (result.get("actions") or [])
            if str(action.get("queue_id") or "").strip() not in handoff_queue_ids
        ]
    applied = apply_result_to_queue(queue, filtered, "prowlarr")
    touched["actions"] += int(applied.get("actions") or 0)
    touched["reviews"] += int(applied.get("reviews") or 0)
    return touched


def apply_source_worker_mangadex_result_to_queue(queue, result):
    touched = {"actions": 0, "reviews": 0, "staged": 0}
    staged_queue_ids = set()
    now = time.time()
    for record in source_worker_direct_stage_records(result):
        targets = source_worker_queue_targets(queue, record)
        if not targets:
            continue
        for _, item in targets:
            queue_id = str(record.get("queue_id") or item.get("key") or "").strip()
            if queue_id:
                staged_queue_ids.add(queue_id)
            item["state"] = record.get("state") or "importing"
            item["current_source"] = record.get("current_source") or "mangadex"
            item["last_event"] = record.get("last_event") or "MangaDex page pack staged; waiting for import"
            item["last_action_at"] = now
            item["last_action_at_iso"] = now_iso(now)
            item["last_mangadex_status"] = "staged_file_ready"
            clear_source_started_marker(item, "mangadex")
            item.setdefault("attempts", []).append(
                {
                    "ts": now,
                    "ts_iso": now_iso(now),
                    "source": "mangadex",
                    "provider": "MangaDex",
                    "provider_id": "mangadex",
                    "status": "staged_file_ready",
                    "reason": item["last_event"],
                    "kind": "source_worker_direct_stage",
                }
            )
            touched["staged"] += 1
    filtered = dict(result or {})
    if staged_queue_ids:
        filtered["actions"] = [
            action
            for action in (result.get("actions") or [])
            if str(action.get("queue_id") or "").strip() not in staged_queue_ids
        ]
    applied = apply_result_to_queue(queue, filtered, "mangadex")
    touched["actions"] += int(applied.get("actions") or 0)
    touched["reviews"] += int(applied.get("reviews") or 0)
    return touched


def _direct_download_action(action):
    client = str((action or {}).get("download_client") or "").strip().lower()
    return client in {"inkdrop_direct", "inkdrop_page_pack"}


def apply_source_worker_rss_result_to_queue(queue, result):
    touched = {"actions": 0, "reviews": 0, "staged": 0}
    staged_queue_ids = set()
    now = time.time()
    for record in source_worker_direct_stage_records(result):
        targets = source_worker_queue_targets(queue, record)
        if not targets:
            continue
        for _, item in targets:
            queue_id = str(record.get("queue_id") or item.get("key") or "").strip()
            if queue_id:
                staged_queue_ids.add(queue_id)
            item["state"] = record.get("state") or "importing"
            item["current_source"] = record.get("current_source") or "rss"
            item["last_event"] = record.get("last_event") or "RSS direct artifact staged; waiting for import"
            item["last_action_at"] = now
            item["last_action_at_iso"] = now_iso(now)
            item["last_rss_status"] = "staged_file_ready"
            clear_source_started_marker(item, "rss")
            item.setdefault("attempts", []).append(
                {
                    "ts": now,
                    "ts_iso": now_iso(now),
                    "source": "rss",
                    "provider": "RSS",
                    "provider_id": "rss",
                    "status": "staged_file_ready",
                    "reason": item["last_event"],
                    "kind": "source_worker_direct_stage",
                }
            )
            touched["staged"] += 1
    filtered = dict(result or {})
    stage_requested = bool(filtered.get("source_worker_direct_stage_requested"))
    if staged_queue_ids or stage_requested:
        stage_queue_ids = set(staged_queue_ids)
        if stage_requested:
            stage_queue_ids.update(str(value or "").strip() for value in filtered.get("source_worker_argv_queue_ids") or [])
        filtered["actions"] = [
            action
            for action in (result.get("actions") or [])
            if str(action.get("queue_id") or "").strip() not in stage_queue_ids or not _direct_download_action(action)
        ]
    applied = apply_result_to_queue(queue, filtered, "rss")
    touched["actions"] += int(applied.get("actions") or 0)
    touched["reviews"] += int(applied.get("reviews") or 0)
    return touched


def run_source_worker_prowlarr(series, rows, args, provider_observer=None):
    if inkdrop_source_worker_cli is None:
        return run_missing(series, args, provider_observer=provider_observer)
    try:
        if not INKDROP_STATE_DB.exists():
            return run_missing(series, args, provider_observer=provider_observer)
    except Exception:
        return run_missing(series, args, provider_observer=provider_observer)
    eligible = source_eligible_rows(rows, args, source="prowlarr")
    queue_ids = source_worker_prowlarr_queue_ids(eligible)
    provider_ids, child_context = source_worker_prowlarr_provider_ids(eligible)
    if not queue_ids or not provider_ids:
        payload = run_missing(series, args, provider_observer=provider_observer)
        if isinstance(payload, dict):
            payload.setdefault("source_worker_bridge_skipped", True)
            payload.setdefault("source_worker_child_provider_context", child_context)
        return payload
    argv = source_worker_prowlarr_argv(queue_ids, provider_ids, args)
    try:
        payload = run_source_worker_cli_locked(
            argv,
            source="prowlarr",
            series=series,
            missing_candidates=len(queue_ids),
            provider_observer=provider_observer,
        )
    except Exception as exc:
        return {
            "ok": False,
            "mode": "source_worker",
            "source": "prowlarr",
            "series": series,
            "actions": [],
            "review": [],
            "reviews": [],
            "missing_candidates": len(queue_ids),
            "attempted_total": 0,
            "failed": len(queue_ids),
            "errors": [{"error": f"{type(exc).__name__}: {exc}", "series": series}],
            "reason": "source_worker_prowlarr_failed",
            "source_worker_child_provider_context": child_context,
        }
    result = prowlarr_source_worker_payload_to_result(payload, series)
    result["source_worker_child_provider_context"] = child_context
    result["source_worker_argv_provider_ids"] = provider_ids
    result["source_worker_argv_queue_ids"] = queue_ids
    return result


def run_source_worker_rss(series, rows, args, deadline=None, provider_observer=None):
    if inkdrop_source_worker_cli is None:
        return run_rss(series, args, deadline=deadline, provider_observer=provider_observer)
    try:
        if not INKDROP_STATE_DB.exists():
            return run_rss(series, args, deadline=deadline, provider_observer=provider_observer)
    except Exception:
        return run_rss(series, args, deadline=deadline, provider_observer=provider_observer)
    eligible = source_eligible_rows(rows, args, source="rss")
    queue_ids = source_worker_rss_queue_ids(eligible)
    if not queue_ids:
        return run_rss(series, args, deadline=deadline, provider_observer=provider_observer)
    argv = source_worker_rss_argv(queue_ids, args)
    try:
        payload = run_source_worker_cli_locked(
            argv,
            source="rss",
            series=series,
            missing_candidates=len(queue_ids),
            provider_observer=provider_observer,
        )
    except Exception as exc:
        return {
            "ok": False,
            "mode": "source_worker",
            "source": "rss",
            "series": series,
            "actions": [],
            "review": [],
            "reviews": [],
            "missing_candidates": len(queue_ids),
            "attempted_total": 0,
            "failed": len(queue_ids),
            "errors": [{"error": f"{type(exc).__name__}: {exc}", "series": series}],
            "reason": "source_worker_rss_failed",
        }
    result = prowlarr_source_worker_payload_to_result(payload, series, source="rss")
    result["source_worker_argv_provider_ids"] = ["rss"]
    result["source_worker_argv_queue_ids"] = queue_ids
    result["source_worker_direct_stage_requested"] = "--stage-direct" in argv
    return result


def source_worker_mangadex_queue_ids(rows):
    return source_worker_prowlarr_queue_ids(rows)


def source_worker_mangadex_limit(args, row_count):
    limits = [row_count]
    for attr in ("mangadex_max_per_series", "mangadex_max_total"):
        value = _positive_int_value(getattr(args, attr, 0), 0)
        if value > 0:
            limits.append(value)
    return max(1, min(value for value in limits if value > 0))


def source_worker_mangadex_argv(queue_ids, args):
    queue_ids = [str(value).strip() for value in queue_ids or [] if str(value or "").strip()]
    limit = source_worker_mangadex_limit(args, len(queue_ids) or 1)
    argv = [str(INKDROP_STATE_DB)]
    for queue_id in queue_ids:
        argv.extend(["--queue-id", queue_id])
    argv.extend(
        [
            "--provider-id",
            "mangadex",
            "--queue-limit",
            str(max(1, len(queue_ids))),
            "--eligible-limit",
            str(limit),
            "--job-limit",
            str(_positive_int_value(getattr(args, "source_worker_job_limit", 0), SOURCE_WORKER_MANGADEX_DEFAULT_JOB_LIMIT)),
            "--run-limit",
            "1",
            "--execute",
            "--allow-network",
            "--timeout-seconds",
            "20",
            "--source-memory-db",
            str(INKDROP_STATE_DB),
            "--full-output",
        ]
    )
    for host in SOURCE_WORKER_MANGADEX_ALLOWED_HOSTS:
        argv.extend(["--allowed-host", host])
    if not getattr(args, "dry_run", False):
        argv.extend(
            [
                "--write",
                "--stage-direct",
                "--allow-direct-network",
                "--staging-root",
                str(SOURCE_WORKER_STAGING_ROOT),
            ]
        )
        for host in SOURCE_WORKER_MANGADEX_DIRECT_ALLOWED_HOSTS:
            argv.extend(["--direct-allowed-host", host])
    return argv


def run_source_worker_mangadex(series, rows, args, deadline=None, provider_observer=None):
    if inkdrop_source_worker_cli is None:
        return run_mangadex(series, args, deadline=deadline, provider_observer=provider_observer)
    try:
        if not INKDROP_STATE_DB.exists():
            return run_mangadex(series, args, deadline=deadline, provider_observer=provider_observer)
    except Exception:
        return run_mangadex(series, args, deadline=deadline, provider_observer=provider_observer)
    eligible = source_eligible_rows(rows, args, source="mangadex")
    queue_ids = source_worker_mangadex_queue_ids(eligible)
    if not queue_ids:
        return run_mangadex(series, args, deadline=deadline, provider_observer=provider_observer)
    argv = source_worker_mangadex_argv(queue_ids, args)
    try:
        payload = run_source_worker_cli_locked(
            argv,
            source="mangadex",
            series=series,
            missing_candidates=len(queue_ids),
            provider_observer=provider_observer,
        )
    except Exception as exc:
        return {
            "ok": False,
            "mode": "source_worker",
            "source": "mangadex",
            "series": series,
            "actions": [],
            "review": [],
            "reviews": [],
            "missing_candidates": len(queue_ids),
            "attempted_total": 0,
            "failed": len(queue_ids),
            "errors": [{"error": f"{type(exc).__name__}: {exc}", "series": series}],
            "reason": "source_worker_mangadex_failed",
        }
    result = prowlarr_source_worker_payload_to_result(payload, series, source="mangadex")
    result["source_worker_argv_provider_ids"] = ["mangadex"]
    result["source_worker_argv_queue_ids"] = queue_ids
    return result


def run_missing(series, args, provider_observer=None):
    cmd = [
        python_command(),
        str(MISSING_SCRIPT),
        "--series",
        series,
        "--max-per-series",
        str(args.missing_max_per_series),
        "--max-total",
        str(args.missing_max_total),
        "--limit",
        str(args.prowlarr_limit),
        "--max-queries-per-issue",
        str(args.prowlarr_max_queries_per_issue),
        "--prowlarr-timeout-seconds",
        str(args.prowlarr_timeout_seconds),
        "--no-result-cooldown-hours",
        str(args.no_result_cooldown_hours),
        "--search-budget-seconds",
        str(args.prowlarr_search_budget_seconds),
    ]
    if args.dry_run:
        cmd.append("--dry-run")
    try:
        return run_locked_provider_command(
            cmd,
            MISSING_ACQUIRE_LOCK,
            source="prowlarr",
            series=series,
            provider_observer=provider_observer,
            timeout=args.prowlarr_command_timeout_seconds,
            wait_seconds=args.source_lock_wait_seconds,
            busy_source="Prowlarr/SAB/qB acquire",
            env=prowlarr_worker_env(args),
        )
    except subprocess.TimeoutExpired as exc:
        return prowlarr_command_timeout_payload("missing_acquire", series, args, exc)


def run_failed_retry(series, args, provider_observer=None):
    if not MISSING_SCRIPT.exists() or args.skip_failed_retry:
        return {}
    try:
        retry_timeout = int(args.failed_retry_command_timeout_seconds or DEFAULT_FAILED_RETRY_COMMAND_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        retry_timeout = DEFAULT_FAILED_RETRY_COMMAND_TIMEOUT_SECONDS
    retry_search_budget = max(5, min(int(args.prowlarr_search_budget_seconds or retry_timeout), max(5, retry_timeout - 8)))
    cmd = [
        python_command(),
        str(MISSING_SCRIPT),
        "--retry-failed",
        "--series",
        series,
        "--max-total",
        str(args.failed_retry_max_total),
        "--retry-failed-limit",
        str(args.failed_retry_limit),
        "--retry-failed-max-attempts",
        str(args.failed_retry_max_attempts),
        "--limit",
        str(args.prowlarr_limit),
        "--max-queries-per-issue",
        str(args.prowlarr_max_queries_per_issue),
        "--prowlarr-timeout-seconds",
        str(args.prowlarr_timeout_seconds),
        "--no-result-cooldown-hours",
        str(args.no_result_cooldown_hours),
        "--search-budget-seconds",
        str(retry_search_budget),
    ]
    if args.dry_run:
        cmd.append("--dry-run")
    try:
        return run_locked_provider_command(
            cmd,
            MISSING_ACQUIRE_LOCK,
            source="prowlarr",
            series=series,
            provider_observer=provider_observer,
            timeout=retry_timeout,
            wait_seconds=args.source_lock_wait_seconds,
            busy_source="failed-download retry",
            env=prowlarr_worker_env(args),
        )
    except subprocess.TimeoutExpired as exc:
        return prowlarr_command_timeout_payload("retry_failed", series, args, exc)


def direct_discovery_configured_command_timeout_seconds(source, args):
    source = source_order_attempt_key(source)
    if source == "comicscodes":
        default = DEFAULT_COMICSCODES_COMMAND_TIMEOUT_SECONDS
        field = "comicscodes_command_timeout_seconds"
    else:
        default = DEFAULT_RSS_COMMAND_TIMEOUT_SECONDS
        field = "rss_command_timeout_seconds"
    try:
        timeout = int(getattr(args, field, default) or default)
    except (TypeError, ValueError):
        timeout = default
    return max(30, min(timeout, 300))


def direct_discovery_command_timeout(source, args, deadline=None):
    timeout = direct_discovery_configured_command_timeout_seconds(source, args)
    return runtime_limited_child_timeout(timeout, deadline)


def direct_discovery_command_timeout_payload(source, series, args, exc):
    source = source_order_attempt_key(source) or "rss"
    label = public_source_name(source) or source
    timeout_seconds = direct_discovery_configured_command_timeout_seconds(source, args)
    reason = f"{source}_command_timeout"
    return {
        "ok": True,
        "source": source,
        "status": "WATCH",
        "feed_status": "command_timeout",
        "dry_run": bool(getattr(args, "dry_run", False)),
        "series": series,
        "missing_targets": 1 if series else 0,
        "candidates_found": 0,
        "auto_grabbed": 0,
        "sent_to_review": 0,
        "failed": 1,
        "skipped": 0,
        "blocked_sources": 0,
        "backoff_sources": 0,
        "actions": [],
        "review": [],
        "reviews": [],
        "errors": [{"error": reason, "series": series}] if series else [{"error": reason}],
        "reason": reason,
        "command_timed_out": True,
        "timed_out": True,
        "timeout_seconds": timeout_seconds,
        "stdout_tail": command_timeout_tail(getattr(exc, "output", "")),
        "stderr_tail": command_timeout_tail(getattr(exc, "stderr", "")),
        "provider": {
            "id": source,
            "label": label,
            "command_timeout_seconds": timeout_seconds,
        },
    }


def run_rss(series, args, deadline=None, provider_observer=None):
    if not RSS_SCRIPT.exists() or args.skip_rss:
        return {}
    cmd = [
        python_command(),
        str(RSS_SCRIPT),
        "--series",
        series,
        "--limit",
        str(args.rss_discovery_limit),
        "--max-auto",
        str(args.rss_discovery_max_auto),
        "--max-per-series",
        str(args.rss_discovery_max_per_series),
    ]
    if args.dry_run:
        cmd.append("--dry-run")
    try:
        return run_observed_provider_call(
            "rss",
            series,
            provider_observer,
            lambda: run_command(cmd, timeout=direct_discovery_command_timeout("rss", args, deadline), env=queue_worker_env()),
        )
    except subprocess.TimeoutExpired as exc:
        return direct_discovery_command_timeout_payload("rss", series, args, exc)


def run_comicscodes(series, args, deadline=None, provider_observer=None):
    if not COMICSCODES_SCRIPT.exists() or args.skip_comicscodes:
        return {}
    cmd = [
        python_command(),
        str(COMICSCODES_SCRIPT),
        "--series",
        series,
        "--limit",
        str(args.comicscodes_discovery_limit),
        "--max-auto",
        str(args.comicscodes_discovery_max_auto),
        "--max-per-series",
        str(args.comicscodes_discovery_max_per_series),
    ]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.include_comicscodes_lists:
        cmd.append("--include-lists")
    try:
        return run_observed_provider_call(
            "comicscodes",
            series,
            provider_observer,
            lambda: run_command(cmd, timeout=direct_discovery_command_timeout("comicscodes", args, deadline), env=queue_worker_env()),
        )
    except subprocess.TimeoutExpired as exc:
        return direct_discovery_command_timeout_payload("comicscodes", series, args, exc)


def run_mangadex(series, args, deadline=None, provider_observer=None):
    if not MANGADEX_DIRECT_SCRIPT.exists() or args.skip_mangadex:
        return {}
    cmd = [
        python_command(),
        str(MANGADEX_DIRECT_SCRIPT),
        "--series",
        series,
        "--max-total",
        str(args.mangadex_max_total),
        "--max-per-series",
        str(args.mangadex_max_per_series),
        "--verify-timeout-seconds",
        str(args.mangadex_verify_timeout_seconds),
    ]
    if args.mangadex_data_saver:
        cmd.append("--data-saver")
    if args.dry_run:
        cmd.extend(["--dry-run", "--preflight"])
    if args.force:
        cmd.append("--force")
    try:
        return run_locked_provider_command(
            cmd,
            MANGADEX_DIRECT_LOCK,
            source="mangadex",
            series=series,
            provider_observer=provider_observer,
            timeout=mangadex_command_timeout(args, deadline),
            wait_seconds=args.source_lock_wait_seconds,
            busy_source="MangaDex direct downloader",
            env=queue_worker_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return mangadex_command_timeout_payload(series, args, exc)


def slskd_child_progress_note(status, *, fallback_series=None, elapsed_seconds=0):
    status = status if isinstance(status, dict) else {}
    stage = str(status.get("current_stage") or status.get("status") or "running").strip()
    series = str(status.get("current_series") or fallback_series or "").strip()
    issue = str(status.get("current_issue") or "").strip()
    query = str(status.get("current_query") or "").strip()
    if len(query) > 96:
        query = query[:93].rstrip() + "..."
    checked = status.get("checked_count")
    selected = status.get("selected_count")
    query_index = status.get("current_query_index")
    query_total = status.get("current_query_total")
    elapsed = status.get("probe_elapsed_seconds")
    if elapsed in (None, ""):
        elapsed = elapsed_seconds
    budget = status.get("probe_budget_seconds")
    parts = [f"SLSKD probe {stage}"]
    if series:
        parts.append(series)
    if issue:
        parts.append(f"issue {issue}")
    if query:
        if query_index and query_total:
            parts.append(f"query {query_index}/{query_total}: {query}")
        else:
            parts.append(f"query: {query}")
    if selected not in (None, ""):
        try:
            checked_count = int(checked or 0)
            selected_count = int(selected or 0)
            parts.append(f"{checked_count}/{selected_count} checked")
        except (TypeError, ValueError):
            pass
    elapsed_label = duration_label(elapsed)
    budget_label = duration_label(budget)
    if elapsed_label and budget_label:
        parts.append(f"{elapsed_label} elapsed of {budget_label} budget")
    elif elapsed_label:
        parts.append(f"{elapsed_label} elapsed")
    return "; ".join(parts)


def run_slskd(
    series,
    args,
    *,
    force=None,
    max_total=None,
    max_per_series=None,
    max_queries=None,
    probe_budget_seconds=None,
    cooldown_hours=None,
    auto_grab_max=None,
    review_id=None,
    progress=None,
    deadline=None,
    provider_observer=None,
):
    if not SLSKD_SOURCE_PROBE_SCRIPT.exists() or args.skip_slskd:
        return {}
    requested_probe_budget = probe_budget_seconds if probe_budget_seconds is not None else args.slskd_probe_budget_seconds
    timeout = slskd_source_timeout_seconds(
        args,
        max_total=max_total,
        max_queries=max_queries,
        probe_budget_seconds=requested_probe_budget,
    )
    limited_timeout = runtime_limited_child_timeout(timeout, deadline)
    try:
        requested_probe_budget = int(requested_probe_budget or args.slskd_probe_budget_seconds)
    except (TypeError, ValueError):
        requested_probe_budget = int(DEFAULT_SLSKD_PROBE_BUDGET_SECONDS)
    effective_probe_budget = requested_probe_budget
    if deadline is not None:
        effective_probe_budget = slskd_probe_budget_for_runtime(
            requested_probe_budget,
            limited_timeout,
            deadline,
        )
    try:
        lock_wait_seconds = max(
            0,
            min(
                int(getattr(args, "source_lock_wait_seconds", DEFAULT_SLSKD_SOURCE_LOCK_WAIT_SECONDS) or 0),
                10,
            ),
        )
    except (TypeError, ValueError):
        lock_wait_seconds = DEFAULT_SLSKD_SOURCE_LOCK_WAIT_SECONDS
    probe_cmd = [
        python_command(),
        str(SLSKD_SOURCE_PROBE_SCRIPT),
        "--series",
        series,
        "--max-total",
        str(max_total if max_total is not None else args.slskd_max_total),
        "--max-per-series",
        str(max_per_series if max_per_series is not None else args.slskd_max_per_series),
        "--wait-seconds",
        str(args.slskd_wait_seconds),
        "--max-queries",
        str(max_queries if max_queries is not None else args.slskd_max_queries),
        "--probe-budget-seconds",
        str(effective_probe_budget),
        "--cooldown-hours",
        str(cooldown_hours if cooldown_hours is not None else args.slskd_cooldown_hours),
    ]
    if force is None:
        force = args.force_slskd
    if force:
        probe_cmd.append("--force")
    review_id = str(review_id or "").strip()
    if review_id:
        probe_cmd.extend(["--review-id", review_id])
    auto_grab_max = auto_grab_max if auto_grab_max is not None else args.slskd_auto_grab_max
    if args.dry_run:
        probe_cmd.extend(["--auto-grab-dry-run", "--auto-grab-max", str(auto_grab_max)])
    else:
        probe_cmd.extend(["--auto-grab-live", "--auto-grab-max", str(auto_grab_max)])
    cmd = probe_cmd
    timeout = limited_timeout

    def publish_slskd_child_progress(proc=None, elapsed_seconds=0):
        if not progress:
            return
        status = read_json(SLSKD_SOURCE_PROBE_STATUS_FILE, {}) or {}
        note = slskd_child_progress_note(status, fallback_series=series, elapsed_seconds=elapsed_seconds)
        progress(note)

    with held_source_worker_lock(SLSKD_SOURCE_PROBE_LOCK, wait_seconds=lock_wait_seconds) as acquired:
        if not acquired:
            return {
                "ok": True,
                "source": "slskd",
                "skipped_busy": True,
                "reason": "SLSKD probe is already running; this pass will retry on the next autopilot cycle.",
            }
        started_monotonic = time.monotonic()
        call_id = provider_call_id("slskd")
        if not provider_start_allowed(provider_observer, "slskd", series, started_monotonic, call_id):
            return {
                "ok": False,
                "source": "slskd",
                "skipped": True,
                "provider_start_deadline_missed": True,
                "reason": "provider_start_deadline_missed",
            }
        try:
            returncode, stdout, stderr = run_process_with_progress(
                cmd,
                timeout=timeout,
                env=queue_worker_env(),
                progress=publish_slskd_child_progress if progress else None,
                progress_interval=10,
            )
        except subprocess.TimeoutExpired as exc:
            status = read_json(SLSKD_SOURCE_PROBE_STATUS_FILE, {}) or {}
            ts = time.time()
            timeout_payload = {
                **(status if isinstance(status, dict) else {}),
                "ok": False,
                "state": "finished",
                "status": "timeout",
                "source": "slskd",
                "reason": "SLSKD probe timed out; automatic retry scheduled.",
                "timeout_seconds": timeout,
                "generated_at": ts,
                "generated_at_iso": now_iso(ts),
                "finished_at": ts,
                "finished_at_iso": now_iso(ts),
                "stdout_tail": command_timeout_tail(getattr(exc, "output", "")),
                "stderr_tail": command_timeout_tail(getattr(exc, "stderr", "")),
            }
            try:
                write_json(SLSKD_SOURCE_PROBE_STATUS_FILE, timeout_payload)
            except Exception as write_exc:
                log("slskd_timeout_status_write_failed", error=f"{type(write_exc).__name__}: {write_exc}")
            observe_provider_result(
                provider_observer, "slskd", series, started_monotonic, payload=timeout_payload, call_id=call_id
            )
            return timeout_payload
        except Exception as exc:
            observe_provider_result(provider_observer, "slskd", series, started_monotonic, error=exc, call_id=call_id)
            raise
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    try:
        payload = parse_command_json(returncode, stdout, stderr)
    except Exception as exc:
        observe_provider_result(provider_observer, "slskd", series, started_monotonic, error=exc, call_id=call_id)
        raise
    observe_provider_result(provider_observer, "slskd", series, started_monotonic, payload=payload, call_id=call_id)
    return payload


def slskd_source_timeout_seconds(args, *, max_total=None, max_queries=None, probe_budget_seconds=None, **_unused):
    try:
        effective_budget = int(
            probe_budget_seconds
            if probe_budget_seconds is not None
            else getattr(args, "slskd_probe_budget_seconds", DEFAULT_SLSKD_PROBE_BUDGET_SECONDS)
        )
    except (TypeError, ValueError):
        effective_budget = DEFAULT_SLSKD_PROBE_BUDGET_SECONDS
    try:
        effective_total = int(max_total if max_total is not None else getattr(args, "slskd_max_total", DEFAULT_SLSKD_MAX_TOTAL))
    except (TypeError, ValueError):
        effective_total = DEFAULT_SLSKD_MAX_TOTAL
    try:
        effective_queries = int(max_queries if max_queries is not None else getattr(args, "slskd_max_queries", 1))
    except (TypeError, ValueError):
        effective_queries = 1
    try:
        wait_seconds = int(getattr(args, "slskd_wait_seconds", 8) or 8)
    except (TypeError, ValueError):
        wait_seconds = 8
    raw_query_ceiling = effective_total * effective_queries * (wait_seconds + 4) + 20
    # The child enforces probe_budget_seconds as a hard wall. Do not reserve a
    # theoretical all-query runtime that can exceed the parent worker window.
    query_ceiling = min(raw_query_ceiling, max(45, effective_budget + 30))
    timeout = max(45, effective_budget + 20, query_ceiling)
    return min(timeout + 10, 960)


def slskd_broad_probe_kwargs(args, eligible_count=0, row_count=0):
    try:
        eligible = int(eligible_count or 0)
    except (TypeError, ValueError):
        eligible = 0
    try:
        rows = int(row_count or 0)
    except (TypeError, ValueError):
        rows = 0
    try:
        configured_total = int(getattr(args, "slskd_max_total", DEFAULT_SLSKD_MAX_TOTAL) or DEFAULT_SLSKD_MAX_TOTAL)
    except (TypeError, ValueError):
        configured_total = DEFAULT_SLSKD_MAX_TOTAL
    try:
        configured_per_series = int(
            getattr(args, "slskd_max_per_series", DEFAULT_SLSKD_MAX_PER_SERIES) or DEFAULT_SLSKD_MAX_PER_SERIES
        )
    except (TypeError, ValueError):
        configured_per_series = DEFAULT_SLSKD_MAX_PER_SERIES
    try:
        configured_auto_grab = int(
            getattr(args, "slskd_auto_grab_max", DEFAULT_SLSKD_AUTO_GRAB_MAX) or DEFAULT_SLSKD_AUTO_GRAB_MAX
        )
    except (TypeError, ValueError):
        configured_auto_grab = DEFAULT_SLSKD_AUTO_GRAB_MAX
    try:
        configured_budget = int(
            getattr(args, "slskd_probe_budget_seconds", DEFAULT_SLSKD_PROBE_BUDGET_SECONDS)
            or DEFAULT_SLSKD_PROBE_BUDGET_SECONDS
        )
    except (TypeError, ValueError):
        configured_budget = DEFAULT_SLSKD_PROBE_BUDGET_SECONDS
    max_queries = max(1, min(int(getattr(args, "slskd_max_queries", 1) or 1), 5))
    wait_seconds = max(2, min(int(getattr(args, "slskd_wait_seconds", 8) or 8), 30))
    wanted_batch = max(1, eligible or rows or 1)
    batch_size = max(
        1,
        min(
            wanted_batch,
            max(1, configured_total),
            max(1, configured_per_series),
            DEFAULT_SLSKD_BROAD_MAX_TOTAL,
        ),
    )
    query_budget = batch_size * max_queries * (wait_seconds + 2)
    budget_floor = min(DEFAULT_SLSKD_BROAD_MIN_PROBE_BUDGET_SECONDS, max(30, configured_budget))
    budget = max(
        budget_floor,
        min(
            max(30, configured_budget),
            DEFAULT_SLSKD_BROAD_PROBE_BUDGET_SECONDS,
            max(30, query_budget),
        ),
    )
    return {
        "max_total": batch_size,
        "max_per_series": batch_size,
        "auto_grab_max": max(0, min(configured_auto_grab, batch_size)),
        "probe_budget_seconds": budget,
    }


def source_runtime_min_seconds(source, args, *, slskd_kwargs=None):
    source = source_order_attempt_key(source)
    slskd_kwargs = slskd_kwargs if isinstance(slskd_kwargs, dict) else {}
    if source == "failed_retry":
        timeout = getattr(args, "failed_retry_command_timeout_seconds", DEFAULT_FAILED_RETRY_COMMAND_TIMEOUT_SECONDS)
    elif source == "prowlarr":
        timeout = getattr(args, "prowlarr_command_timeout_seconds", DEFAULT_PROWLARR_COMMAND_TIMEOUT_SECONDS)
    elif source == "rss":
        timeout = direct_discovery_configured_command_timeout_seconds("rss", args)
    elif source == "comicscodes":
        timeout = direct_discovery_configured_command_timeout_seconds("comicscodes", args)
    elif source == "mangadex":
        timeout = mangadex_configured_command_timeout_seconds(args)
    elif source == "slskd":
        timeout = slskd_source_timeout_seconds(args, **slskd_kwargs)
    else:
        timeout = 60
    try:
        timeout = float(timeout or 0)
    except (TypeError, ValueError):
        timeout = 60.0
    # Leave a little room for JSON save, status write, queue sync, and lock release.
    return max(15.0, timeout + 20.0)


def slskd_hot_retry_candidate(item, now):
    if not isinstance(item, dict):
        return False
    if item.get("state") in {"verified", "downloading", "importing", "needs_you"}:
        return False
    if not item.get("present_in_watch", True):
        return False
    if not has_due_cached_slskd_autopick(item, now=now):
        return False
    status = str(item.get("last_slskd_autoresolve_status") or "")
    event = str(item.get("last_event") or "").lower()
    if (
        status == "safe_alternate_available"
        or "safe alternate available" in event
        or (
            item.get("last_failed_candidate_review_id")
            and item.get("last_failed_candidate_reason")
        )
    ):
        return cached_safe_slskd_candidate_count(item) > 0
    return bool(cached_safe_slskd_entry_for_item(item)[0])


def slskd_hot_retry_sort_key(item):
    # Cached candidates have already paid the discovery and safety-gate cost.
    # Serve the oldest retained result first so a freshly retried row rotates
    # behind candidates that have never reached the durable handoff path.
    return slskd_recovery_fairness_key(item, "cached_slskd")


def slskd_recovery_fairness_key(item, lane="", now=None):
    if lane == "slskd_reprobe":
        activity_at = latest_slskd_result_signature_at(item)
    else:
        activity_at = max(
            numeric_timestamp(item.get("last_failed_candidate_at")),
            numeric_timestamp(item.get("last_slskd_at")),
        )
    activity_at = activity_at or queue_created_ts(item)
    return (
        activity_at if activity_at > 0 else 0,
        0 if lane == "cached_slskd" else 1,
        queue_attempt_count(item),
        normalize(item.get("series") or ""),
        normalize(str(item.get("issue") or "")),
    )


def slskd_recovery_group_service_at(rows):
    """When SLSKD last did anything for this series, as a shared bidding clock.

    The two recovery lanes -- cached_slskd and slskd_reprobe -- bid for the same
    two slots in a pass, so they have to bid the same quantity. Each lane orders
    itself correctly on its own terms: cached by the oldest retained candidate,
    reprobes by the oldest unserved retry promise. The auction then scored those
    heads with slskd_recovery_fairness_key, which is the cached lane's key. The
    reprobe lane was nominating a group for one reason and bidding a number that
    measured something else, so it bid whatever its promise-ordered head happened
    to score.

    Measured on the live catalog 2026-07-27: the reprobe lane held 152 groups and
    1,228 rows -- half of all due work -- with its oldest group 45 days without an
    SLSKD result. It nominated a 7-day-old group, bid 7 days, and lost both slots
    to a 12-group lane on every pass.

    The one thing both lanes mean by "still waiting" is how long since SLSKD last
    ran for this series, so that is the bid. Same idea and the same fields as
    broad_group_service_key, plus the result signature the reprobe lane already
    uses as its own service clock. A group SLSKD has never touched returns 0 and
    sorts first.
    """

    latest = 0.0
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        latest = max(latest, latest_slskd_result_signature_at(item))
        for field in ("last_attempt_at", "autopilot_slskd_attempted_at", "last_slskd_at"):
            latest = max(latest, numeric_timestamp(item.get(field)))
    return latest


def slskd_recovery_group_bid(rows, lane, now=None):
    """Rank one lane's nominated group against the other lane's for a shared slot.

    Oldest service first. A tie means neither lane has been served more recently
    than the other, and the cached lane wins it: that group already has a safety
    gated candidate in hand, so it is the cheaper of two equally stale options.
    """

    return (
        slskd_recovery_group_service_at(rows),
        0 if str(lane or "") == "cached_slskd" else 1,
    )


def slskd_recovery_slot_lanes(lane_sizes, capacity):
    """Decide which lane gets each shared recovery slot, biggest backlog first.

    cached_slskd and slskd_reprobe split a fixed two slots out of a six-series
    pass, and which lane got them was decided by comparing one head against the
    other. That reads as fair and is not, because the two lanes are nowhere near
    the same size. On the live catalog 2026-07-27 the reprobe lane held 152
    groups and 1,228 rows -- half of all due work -- against 12 groups and 39
    rows in cached, and lost both slots on every pass, so half the backlog was
    contending for a share of two slots it never won.

    Head-to-head only works between lanes of comparable size. So the lane with
    more waiting groups takes first refusal on one slot, and the rest are still
    decided head to head on how long each lane's nominated group has waited.
    Neither lane can be shut out: with the usual capacity of two, the bigger
    backlog gets one and the older work gets the other.

    A single slot is never reserved. A one-slot pass is the case where cached
    recovery is meant to keep its established priority -- it already holds a
    safety gated candidate -- so that slot stays a straight bid on who has
    waited longest.

    Returns the lane name to serve first for each slot, or None where the slot
    should be decided by the head-to-head bid.
    """

    sizes = {
        str(lane): int(size or 0)
        for lane, size in (lane_sizes or {}).items()
        if int(size or 0) > 0
    }
    try:
        capacity = max(0, int(capacity or 0))
    except (TypeError, ValueError):
        capacity = 0
    if capacity < 2 or len(sizes) < 2:
        return [None] * capacity
    largest = max(sizes, key=lambda lane: (sizes[lane], lane))
    others = max(size for lane, size in sizes.items() if lane != largest)
    if sizes[largest] <= others:
        return [None] * capacity
    return [largest, *([None] * (capacity - 1))]


def slskd_reprobe_admission_fairness_key(item, now=None):
    if now is None:
        now = time.time()
    result_at = latest_slskd_result_signature_at(item)
    activity_at = result_at or queue_created_ts(item)
    promised_at = retry_after_ts(item)
    # A due retry timestamp is a promise to revisit the previous result. Give
    # an unserved promise precedence within the reprobe backlog, but only until
    # a fresh result signature proves that an attempt ran. Cross-lane cached
    # recovery priority remains governed by slskd_recovery_fairness_key.
    unserved_promise = promised_at > 0 and promised_at <= now and result_at < promised_at
    return (
        0 if unserved_promise else 1,
        promised_at if unserved_promise else (activity_at if activity_at > 0 else 0),
        queue_attempt_count(item),
        normalize(item.get("series") or ""),
        normalize(str(item.get("issue") or "")),
    )


def slskd_reprobe_group_admission_fairness_key(rows, now=None, service_at=None):
    """Rotate overdue SLSKD reprobes by series while preserving row promises.

    A series can contain many overdue rows with the same retry promise.  Using
    only the oldest row key lets that series keep winning after one sibling was
    actually reprobed.  The latest result signature is therefore the group
    service clock; one fresh bounded result moves the group behind untouched
    groups, while the oldest unserved promise still selects the exact row to
    process when the group returns.
    """
    if now is None:
        now = time.time()
    rows = [item for item in (rows or []) if isinstance(item, dict)]
    if not rows:
        return (2, 0, 0, 999999, "", "")
    promised = []
    for item in rows:
        result_at = latest_slskd_result_signature_at(item)
        promised_at = retry_after_ts(item)
        if promised_at > 0 and promised_at <= now and result_at < promised_at:
            promised.append((promised_at, item))
    if promised:
        oldest_promised_at = min(entry[0] for entry in promised)
        if service_at is None:
            service_at = max(latest_slskd_result_signature_at(item) for item in rows)
        representative = min(
            (entry[1] for entry in promised),
            key=lambda item: slskd_reprobe_admission_fairness_key(item, now=now),
        )
        return (
            0,
            service_at if service_at > 0 else 0,
            oldest_promised_at,
            queue_attempt_count(representative),
            normalize(representative.get("series") or ""),
            normalize(str(representative.get("issue") or "")),
        )
    representative = min(
        rows,
        key=lambda item: slskd_reprobe_admission_fairness_key(item, now=now),
    )
    activity_at = (
        max(latest_slskd_result_signature_at(item) for item in rows)
        if service_at is None
        else service_at
    )
    return (
        1,
        activity_at if activity_at > 0 else 0,
        retry_after_ts(representative),
        queue_attempt_count(representative),
        normalize(representative.get("series") or ""),
        normalize(str(representative.get("issue") or "")),
    )


def oldest_due_slskd_reprobe(queue, args, now=None):
    if now is None:
        now = time.time()
    allowed_series = set(getattr(args, "series", []) or [])
    rows = []
    for item in (queue.get("items") or {}).values():
        if not isinstance(item, dict):
            continue
        if allowed_series and item.get("series") not in allowed_series:
            continue
        if has_soon_cached_slskd_autopick(item, now=now):
            continue
        if slskd_source_result_reprobe_due(item, now=now):
            rows.append(item)
    return min(
        rows,
        key=lambda item: slskd_reprobe_admission_fairness_key(item, now=now),
        default=None,
    )


def first_pass_due_row_count(queue, args, now=None):
    if now is None:
        now = time.time()
    allowed_series = set(getattr(args, "series", []) or [])
    count = 0
    for item in (queue.get("items") or {}).values():
        if not isinstance(item, dict):
            continue
        if allowed_series and item.get("series") not in allowed_series:
            continue
        if item.get("state") in TERMINAL_QUEUE_STATES | ACTIVE_QUEUE_STATES:
            continue
        if item.get("state") == "needs_you" and not getattr(args, "retry_needs_you", False):
            continue
        if not item.get("present_in_watch", True):
            continue
        retry_after = retry_after_ts(item)
        if retry_after > now and not getattr(args, "force", False):
            continue
        if queue_provider_evidence_count(item) <= 0:
            count += 1
    return count


def broad_due_group_count(queue, args, now=None):
    """Count due groups that still need the normal source ladder.

    Cached SLSKD retries have their own fast lane.  They must not hide the
    ordinary retry/first-pass backlog from the hot-retry budget calculation.
    """
    if now is None:
        now = time.time()
    allowed_series = set(getattr(args, "series", []) or [])
    groups = set()
    for item in (queue.get("items") or {}).values():
        if not isinstance(item, dict):
            continue
        if allowed_series and item.get("series") not in allowed_series:
            continue
        if item.get("state") in TERMINAL_QUEUE_STATES | ACTIVE_QUEUE_STATES:
            continue
        if item.get("state") == "needs_you" and not getattr(args, "retry_needs_you", False):
            continue
        if not item.get("present_in_watch", True):
            continue
        retry_after = retry_after_ts(item)
        if retry_after > now and not getattr(args, "force", False):
            continue
        if slskd_hot_retry_candidate(item, now):
            continue
        groups.add(due_group_key(item))
    return len(groups)


def broad_due_runtime_reservation_seconds(queue, args, now=None):
    if broad_due_group_count(queue, args, now=now) <= 0:
        return 0.0
    try:
        configured = float(os.environ.get("INKDROP_AUTOPILOT_BROAD_RESERVE_SECONDS", "180"))
    except (TypeError, ValueError):
        configured = 180.0
    # A group must have enough time to start and complete at least one bounded
    # provider attempt, not merely enough time to be selected and immediately
    # returned to retry_after.
    return max(run_group_start_min_seconds(), configured)


def slskd_hot_retry_limit(queue, args, now=None):
    if now is None:
        now = time.time()
    configured = max(0, int(getattr(args, "slskd_hot_retry_max", 0) or 0))
    if configured <= 0:
        return 0
    max_series = max(1, int(getattr(args, "max_series", 1) or 1))
    configured = min(configured, max_series)
    broad_due = broad_due_group_count(queue, args, now=now)
    if broad_due <= 0:
        return configured
    # Keep cached recovery moving, but reserve at least one slot and most of the
    # bounded pass for the normal first-pass/retry scheduler.  A six-series pass
    # therefore runs at most one hot retry while broad work is due.
    hot_capacity = max(0, max_series - 1)
    if hot_capacity <= 0:
        return 0
    # Cached handoffs have already returned a safety-gated candidate. Give one
    # of them the bounded recovery slot before spending that slot on another
    # empty-result reprobe. Broad first-pass/retry work still keeps the
    # remaining slots and the explicit runtime reservation below.
    allowed_series = set(getattr(args, "series", []) or [])
    hot_rows = [
        item
        for item in (queue.get("items") or {}).values()
        if isinstance(item, dict)
        and (not allowed_series or item.get("series") in allowed_series)
        and slskd_hot_retry_candidate(item, now)
    ]
    if not hot_rows:
        return 0
    # Cached candidates have already paid the discovery and safety-gate cost.
    # Let a six-series pass advance two of them while retaining a two-thirds
    # majority of slots (and the explicit runtime reservation) for broad work.
    reserved_limit = max(1, max_series // 3)
    return min(configured, reserved_limit, hot_capacity)


def slskd_hot_retry_rows(queue, args):
    if getattr(args, "skip_slskd", False):
        return []
    now = time.time()
    limit = slskd_hot_retry_limit(queue, args, now=now)
    if limit <= 0:
        return []
    allowed_series = set(args.series or [])
    rows = []
    seen_review_ids = set()
    for item in (queue.get("items") or {}).values():
        if item.get("state") in TERMINAL_QUEUE_STATES:
            continue
        if allowed_series and item.get("series") not in allowed_series:
            continue
        if not slskd_hot_retry_candidate(item, now):
            continue
        review_id, _entry = cached_safe_slskd_entry_for_item(item)
        if review_id and review_id in seen_review_ids:
            continue
        if review_id:
            seen_review_ids.add(review_id)
        rows.append(item)
    rows.sort(key=slskd_hot_retry_sort_key)
    return rows[:limit]


def process_slskd_hot_retries(queue, args, progress=None, deadline=None, provider_observer=None):
    rows = slskd_hot_retry_rows(queue, args)
    if not rows:
        return []
    processed = []
    broad_reservation = broad_due_runtime_reservation_seconds(queue, args)
    max_queries = max(1, min(int(args.slskd_max_queries or 1), 5))
    probe_budget = max(30, min(int(args.slskd_probe_budget_seconds or 180), 180))
    for item in rows:
        min_seconds = source_runtime_min_seconds(
            "slskd",
            args,
            slskd_kwargs={
                "max_total": 1,
                "max_queries": max_queries,
                "probe_budget_seconds": probe_budget,
            },
        )
        if runtime_deadline_too_close(deadline, min_seconds + broad_reservation):
            if progress:
                progress(
                    series=item.get("series"),
                    source="slskd",
                    note=(
                        runtime_budget_skip_reason("slskd", deadline, min_seconds)
                        + (
                            f"; reserving about {duration_label(broad_reservation)} for due series"
                            if broad_reservation
                            else ""
                        )
                    ),
                )
            break
        series = str(item.get("series") or "").strip()
        if not series:
            continue
        review_id, _entry = cached_safe_slskd_entry_for_item(item)
        failed_retry = bool(item.get("last_failed_candidate_review_id") and item.get("last_failed_candidate_reason"))
        result = {
            "series": series,
            "queue_identity": series_summary_identity(item),
            "missing_rows": 1,
            "hot_retry": True,
            "cached_slskd_start": not failed_retry,
            "issue": item.get("issue"),
            "review_id": review_id,
            "sources": {},
            "errors": [],
        }
        if progress:
            note = (
                f"retrying next SLSKD candidate for issue {item.get('issue')}"
                if failed_retry
                else f"starting cached SLSKD candidate for issue {item.get('issue')}"
            )
            progress(series=series, source="slskd", note=note)
        targeted_evidence = ensure_targeted_provider_evidence(queue, [item], args, deadline=deadline)
        result["targeted_annotation"] = targeted_evidence
        hot_retry_eligible = slskd_hot_retry_candidate(item, now=time.time())
        if not targeted_evidence.get("ready") or not hot_retry_eligible:
            result["evidence_deferred"] = True
            result["state_after"] = item.get("state")
            result["last_event_after"] = item.get("last_event")
            processed.append(result)
            if progress:
                progress(
                    series=series,
                    source="queue",
                    note="required file and completion checks did not finish; retrying before source search",
                )
            continue
        payload = run_slskd(
            series,
            args,
            force=True,
            max_total=1,
            max_per_series=1,
            max_queries=max_queries,
            probe_budget_seconds=probe_budget,
            cooldown_hours=24,
            auto_grab_max=1,
            review_id=review_id,
            deadline=deadline,
            progress=(
                (lambda note, _series=series: progress(series=_series, source="slskd", note=note))
                if progress
                else None
            ),
            provider_observer=provider_observer,
        )
        result["sources"]["slskd"] = summarize_source_result("slskd", payload)
        if isinstance(payload, dict):
            result["slskd_checked"] = apply_slskd_checked(queue, payload)
            result["slskd_auto_grab"] = apply_slskd_auto_grab(queue, payload)
            if payload.get("skipped_busy"):
                now = time.time()
                busy_reason = (
                    payload.get("reason")
                    or "SLSKD probe is already running; retrying next candidate soon."
                )
                mark_source_busy_retry(item, "slskd", busy_reason, now, args)
                item["last_slskd_autoresolve_status"] = "retry_pending"
                item["last_slskd_autoresolve_reason"] = busy_reason
                item["last_slskd_autoresolve_at"] = now
                item["last_slskd_autoresolve_at_iso"] = now_iso(now)
                item.pop("needs_you_reason", None)
                touch_queue_item(item, now)
                result["busy"] = True
        row_key = str(item.get("key") or "").strip()
        annotate_kwargs = {
            "max_seconds": getattr(args, "annotate_timeout_seconds", DEFAULT_ANNOTATE_TIMEOUT_SECONDS),
            "reason": "slskd_hot_retry",
        }
        if row_key:
            annotate_kwargs["row_keys"] = [row_key]
        annotate_states(queue, **annotate_kwargs)
        result["state_after"] = item.get("state")
        result["last_event_after"] = item.get("last_event")
        processed.append(result)
        save_queue_progress_snapshot(queue)
        if isinstance(payload, dict) and payload.get("skipped_busy"):
            break
    return processed


def broad_queue_state_after_hot_retries(hot_processed):
    hot_processed = hot_processed if isinstance(hot_processed, list) else []
    slskd_busy = any(row.get("hot_retry") and row.get("busy") for row in hot_processed if isinstance(row, dict))
    return {
        "slskd_busy": slskd_busy,
        "skip_sources": {"slskd"} if slskd_busy else set(),
        "pause_broad_queue": False,
    }


def process_deferred_hot_retries(
    queue,
    args,
    *,
    provider_work_started,
    broad_work_available,
    progress=None,
    deadline=None,
    provider_observer=None,
):
    """Run cached SLSKD retries only after broad provider work has had priority."""
    if broad_work_available and not provider_work_started:
        return {
            "processed": [],
            "deferred": True,
            "reason": "waiting_for_broad_provider_start",
        }
    return {
        "processed": process_slskd_hot_retries(
            queue,
            args,
            progress=progress,
            deadline=deadline,
            provider_observer=provider_observer,
        ),
        "deferred": False,
        "reason": "broad_provider_started" if provider_work_started else "no_broad_work_available",
    }


def queue_attempt_count(item):
    try:
        return int(item.get("source_ladder_attempt_count") or 0)
    except (TypeError, ValueError):
        return 0


def queue_provider_evidence_count(item):
    counts = queue_item_recorded_source_attempt_counts(item)
    return sum(int(counts.get(source) or 0) for source in VALID_SOURCE_ORDER if source != "local")


def queue_last_activity_ts(item):
    best = 0
    for key in (
        "updated_at",
        "last_slskd_autoresolve_at",
        "last_download_started_at",
        "last_import_failed_at",
        "source_ladder_attempted_at",
        "last_attempt_at",
        "last_slskd_at",
        "last_source_outcome_at",
        "last_failed_retry_at",
        "stale_downloader_normalized_at",
        "stale_downloader_cleared_at",
        "retry_waiting_normalized_at",
        "created_at",
    ):
        best = max(best, numeric_timestamp(item.get(key)))
    return best


def queue_retry_activity_ts(item):
    """Return stable work activity for fairness among unscheduled retries.

    Generic queue refreshes update ``updated_at``, resolver observation time,
    and retry-normalization annotations without performing another provider or
    download attempt.  Those observations must not make old retryable handoffs
    look like fresh work forever.
    """

    best = 0
    for key in (
        "last_download_started_at",
        "last_import_failed_at",
        "source_ladder_attempted_at",
        "last_attempt_at",
        "last_slskd_at",
        "last_source_outcome_at",
        "last_failed_retry_at",
        "last_source_error_at",
        "last_source_busy_at",
        "queue_created_at",
        "created_at",
    ):
        best = max(best, numeric_timestamp(item.get(key)))
    return best


def queue_created_ts(item):
    return (
        numeric_timestamp(item.get("queue_created_at"))
        or numeric_timestamp(item.get("created_at"))
    )


def queue_first_pass_priority(item):
    return 0 if queue_provider_evidence_count(item) <= 0 else 1


def has_missing_required_source_result(item):
    return bool(queue_first_pass_priority(item) > 0 and missing_required_source_result_sources(item))


def missing_required_source_result_priority(item):
    return 0 if has_missing_required_source_result(item) else 1


def missing_required_source_result_due(item, now=None):
    if not has_missing_required_source_result(item):
        return False
    now = time.time() if now is None else float(now)
    retry_after = retry_after_ts(item)
    return retry_after <= 0 or retry_after <= now


def queue_first_pass_sort_ts(item):
    if queue_first_pass_priority(item) == 0:
        return queue_created_ts(item)
    return queue_retry_activity_ts(item)


def queue_runtime_budget_retry_priority(item):
    event = normalize(str(item.get("last_event") or ""))
    if "runtime budget" in event or "budget reached" in event:
        return 0
    return 1


def due_row_sort_key(item):
    issue_value = issue_number_value(item.get("issue"))
    issue_sort = issue_value if issue_value is not None else 999999
    retry_after = retry_after_ts(item)
    cached_slskd_priority = 0 if has_soon_cached_slskd_autopick(item) else 1
    stale_downloader_priority = 0 if stale_downloader_send_result(item) else 1
    missing_result_priority = missing_required_source_result_priority(item)
    runtime_budget_priority = queue_runtime_budget_retry_priority(item)
    return (
        cached_slskd_priority,
        stale_downloader_priority,
        runtime_budget_priority,
        queue_first_pass_priority(item),
        missing_result_priority,
        retry_after if retry_after > 0 else 0,
        queue_attempt_count(item),
        queue_first_pass_sort_ts(item),
        issue_sort,
        normalize(str(item.get("issue") or "")),
    )


def broad_group_service_key(rows):
    """When this series was last actually searched, as a rotation clock.

    The broad lanes -- first_pass, retry_due, missing_provider_result, queued --
    sorted only by due_group_sort_key, which for a first-pass group is the
    creation time of its oldest row. That value never changes, so whichever
    series sat at the head of the lane sat there permanently: it was picked
    every cycle, searching it did not move it, and everything behind it was
    unreachable.

    Measured on the live catalog 2026-07-27: 152 of 218 series with open wanted
    units had gone 48 hours without a single provider search, while the top ten
    series took 78% of all search attempts. Series with complete, trivially
    available sources sitting on SLSKD -- The Sacrificers, The Moon is
    Following Us -- were at positions 35 and 36 of a 66-group lane and had
    never been reached.

    slskd_reprobe already solved this for itself: it uses the latest result
    signature as a group service clock so a freshly served group falls behind
    untouched ones. This is the same idea for the broad lanes, using the
    attempt time mark_series_searching() already records. A group that has
    never been searched sorts first (0), and one just searched goes to the
    back, so the lane rotates instead of replaying its head forever.

    due_group_sort_key remains the tiebreak, so among never-searched groups the
    oldest work still goes first.
    """

    latest = 0.0
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        for field in ("last_attempt_at", "autopilot_slskd_attempted_at", "last_slskd_at"):
            latest = max(latest, numeric_timestamp(item.get(field)))
    return latest


def due_group_sort_key(series, rows, series_activity=None):
    group_key = series
    identity = ""
    lane = ""
    if isinstance(series, tuple):
        identity = str(series[1] if len(series) > 1 else "")
        lane = str(series[2] if len(series) > 2 else "")
        series = series[0]
    if not rows:
        return (999999, 999999999999, 999999, normalize(series), normalize(identity), normalize(lane))
    recent_series_activity = numeric_timestamp((series_activity or {}).get(group_key))
    if recent_series_activity <= 0:
        recent_series_activity = max(queue_last_activity_ts(row) for row in rows)
    cached_slskd_priority = 0 if any(has_soon_cached_slskd_autopick(row) for row in rows) else 1
    stale_downloader_priority = 0 if any(stale_downloader_send_result(row) for row in rows) else 1
    runtime_budget_priority = min(queue_runtime_budget_retry_priority(row) for row in rows)
    first_pass_priority = min(queue_first_pass_priority(row) for row in rows)
    missing_result_priority = min(missing_required_source_result_priority(row) for row in rows)
    oldest_due_retry = min(
        (retry_after_ts(row) for row in rows if retry_due_now(row)),
        default=0,
    )
    if first_pass_priority == 0:
        fairness_sort = min(queue_created_ts(row) for row in rows)
    elif oldest_due_retry > 0:
        # A recent annotation/import on one row must not reset the age of older
        # due work in the same series.  Sort retry groups by the oldest promised
        # retry time so large or frequently touched series eventually run.
        fairness_sort = oldest_due_retry
    else:
        # Unscheduled retry rows have no explicit promise to sort by. Use only
        # stable work activity: generic observations can refresh updated_at and
        # annotation timestamps many times without making acquisition progress.
        fairness_sort = min(queue_retry_activity_ts(row) for row in rows)
    # Rotate by the oldest promised retry (or stable work activity when unscheduled)
    # before row attempt count so large runs do not monopolize every pass. For
    # first-pass groups, oldest untouched rows go first so runtime-budget skips
    # do not sit behind every fresh add forever.
    return (
        cached_slskd_priority,
        stale_downloader_priority,
        runtime_budget_priority,
        first_pass_priority,
        missing_result_priority,
        fairness_sort,
        min(queue_attempt_count(row) for row in rows),
        normalize(series),
        normalize(identity),
        normalize(lane),
    )


def missing_provider_result_lane_source(item):
    if not has_missing_required_source_result(item):
        return ""
    missing = missing_required_source_result_sources(item)
    return str(missing[0] if missing else "").strip().lower()


def due_group_key(item):
    series = item.get("series") or ""
    identity = series_summary_identity(item) or f"title:{normalize(series)}"
    missing_source = missing_provider_result_lane_source(item)
    if missing_source:
        return (series, identity, f"missing_provider:{missing_source}")
    return (series, identity)


SCHEDULER_BUCKET_LABELS = {
    "cached_slskd": "cached SLSKD",
    "slskd_reprobe": "SLSKD reprobe",
    "stale_downloader": "stale downloader",
    "missing_provider_result": "missing provider result",
    "retry_due": "retry due",
    "first_pass": "first pass",
    "active": "active",
    "queued": "queued",
}

MISSING_RECOVERY_COHORTS = (
    "handoff_transfer_recovery",
    "ordinary_new",
    "never_no_call",
    "result_candidate_loss",
    "import_reader_recovery",
)
MISSING_RECOVERY_NEVER_TRIED_AGE_SECONDS = 24 * 60 * 60


def missing_recovery_enabled(args):
    if not bool(getattr(args, "missing_recovery_enabled", False)):
        return False
    if "INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED" in os.environ or "INKDROP_MISSING_RECOVERY_ENABLED" in os.environ:
        return missing_recovery_runtime_enabled()
    return not missing_recovery_control().get("paused", False)


def automatic_search_runtime_enabled():
    value = str(os.environ.get("INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED", "0") or "0").strip().lower()
    return value in {"1", "true", "on", "yes", "enabled"}


def missing_recovery_control():
    path = Path(os.environ.get("INKDROP_STATE_DIR") or "/state") / "missing-recovery-control.json"
    try:
        if path.is_symlink():
            return {"paused": True, "reason": "control_not_regular_file", "path": str(path)}
        if not path.exists():
            return {"paused": False, "reason": "control_missing", "path": str(path)}
        if not path.is_file():
            return {"paused": True, "reason": "control_not_regular_file", "path": str(path)}
        if path.stat().st_size > 16 * 1024:
            return {"paused": True, "reason": "control_too_large", "path": str(path)}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("control payload must be an object")
        return {
            "paused": payload.get("paused") is True,
            "reason": "operator_paused" if payload.get("paused") is True else "operator_running",
            "path": str(path),
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"paused": True, "reason": "control_invalid", "path": str(path), "error": type(exc).__name__}


def missing_recovery_runtime_enabled():
    value = str(os.environ.get("INKDROP_MISSING_RECOVERY_ENABLED", "0") or "0").strip().lower()
    return (
        automatic_search_runtime_enabled()
        and value in {"1", "true", "on", "yes", "enabled"}
        and not missing_recovery_control().get("paused", False)
    )


def missing_recovery_max_per_cohort(args, max_groups):
    try:
        configured = int(getattr(args, "missing_recovery_max_per_cohort", 2) or 2)
    except (TypeError, ValueError):
        configured = 2
    return max(1, min(configured, max(1, int(max_groups or 1))))


def import_reader_recovery_due(item):
    item = item if isinstance(item, dict) else {}
    text = " ".join(
        str(item.get(key) or "").strip().lower()
        for key in (
            "last_import_status",
            "last_import_failed_status",
            "last_event",
            "last_reconcile_state",
            "display_phase",
            "outcome",
        )
    )
    return any(
        marker in text
        for marker in (
            "library_scan_timeout",
            "reader scan timeout",
            "missing_file",
            "missing file",
            "stale_folder_proof",
            "import failed",
            "import_failed",
            "stale import",
        )
    )


def group_service_clock(bucket, rows):
    """When this group was last actually served, whichever lane it came from.

    due_series orders each bucket by a rotation clock so a group that was just
    searched falls behind untouched ones. select_missing_recovery_groups then
    rebuilds its own cohort lists and re-sorted them by due_group_sort_key alone,
    which threw that ordering away -- and missing recovery is on in production,
    so that is the path every real pass takes.

    Measured 2026-07-27 against the live queue: the first_pass bucket ordered by
    rotation clock leads with never-searched series (Oblivion Song, Umbrella
    Academy: Hotel Oblivion). After the cohort re-sort it led with One Piece,
    searched 2.9 days earlier, and Beneath the Trees Where Nobody Sees, searched
    7 hours earlier. Forty consecutive simulated passes reached 18 distinct
    series and picked the same three every single time.
    """

    if str(bucket or "") in {"cached_slskd", "slskd_reprobe"}:
        return slskd_recovery_group_service_at(rows)
    return broad_group_service_key(rows)


def missing_recovery_cohort_for_rows(rows, *, bucket=None, now=None):
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    now = time.time() if now is None else float(now)
    bucket = str(bucket or scheduler_bucket_for_rows(rows, now=now))
    if any(import_reader_recovery_due(row) for row in rows):
        return "import_reader_recovery"
    if bucket in {"cached_slskd", "stale_downloader"}:
        return "handoff_transfer_recovery"
    if bucket in {"slskd_reprobe", "missing_provider_result", "retry_due", "active"}:
        return "result_candidate_loss"
    if bucket == "first_pass":
        oldest = min((queue_created_ts(row) for row in rows if queue_created_ts(row) > 0), default=now)
        if now - oldest >= MISSING_RECOVERY_NEVER_TRIED_AGE_SECONDS:
            return "never_no_call"
    return "ordinary_new"


def select_missing_recovery_groups(buckets, *, max_groups, max_per_cohort, now=None):
    """Select a bounded mixed cohort without changing the enclosing pass cap."""

    now = time.time() if now is None else float(now)
    cohort_rows = collections.defaultdict(list)
    for bucket, rows in (buckets or {}).items():
        for group in rows or []:
            cohort = missing_recovery_cohort_for_rows(group[1], bucket=bucket, now=now)
            cohort_rows[cohort].append((bucket, group))
    for rows in cohort_rows.values():
        # Bucket precedence first, exactly as before, then the rotation clock the
        # bucket was already ordered by, then the original tiebreak. Without the
        # middle term this re-sort silently discarded that ordering.
        rows.sort(
            key=lambda entry: (
                scheduler_bucket_rank(entry[0]),
                group_service_clock(entry[0], entry[1][1]),
                entry[1][2],
            )
        )

    selected = []
    counts = collections.Counter()
    max_groups = max(1, int(max_groups or 1))
    max_per_cohort = max(1, min(int(max_per_cohort or 1), max_groups))

    legacy_recovery_buckets = {"cached_slskd", "slskd_reprobe"}
    broad_exists = any(
        bucket not in legacy_recovery_buckets
        for rows in cohort_rows.values()
        for bucket, _group in rows
    )

    # A one-series pass preserves the established broad-work guarantee: cached
    # transfer recovery cannot consume the only ordinary/provider opportunity.
    if max_groups == 1 and broad_exists:
        for cohort in MISSING_RECOVERY_COHORTS:
            if cohort == "handoff_transfer_recovery" or not cohort_rows.get(cohort):
                continue
            _bucket, group = cohort_rows[cohort].pop(0)
            return [group]

    # Preserve the established shared SLSKD recovery fraction. Cached starts
    # and zero-result reprobes rotate through one bounded pool instead of being
    # counted as separate new cohorts and displacing broad acquisition work.
    if broad_exists:
        recovery_capacity = max(1, max_groups // 3)
        recovery_lanes = {
            bucket: [
                (missing_recovery_cohort_for_rows(group[1], bucket=bucket, now=now), group)
                for group in (buckets or {}).get(bucket, [])
            ]
            for bucket in legacy_recovery_buckets
        }
        slot_lanes = slskd_recovery_slot_lanes(
            {lane: len(rows) for lane, rows in recovery_lanes.items()},
            recovery_capacity,
        )
        for slot_index in range(recovery_capacity):
            reserved = slot_lanes[slot_index] if slot_index < len(slot_lanes) else None
            if reserved and recovery_lanes.get(reserved):
                bucket = reserved
            else:
                candidates = []
                for bucket_name, rows in recovery_lanes.items():
                    if not rows:
                        continue
                    _cohort, group = rows[0]
                    candidates.append(
                        (slskd_recovery_group_bid(group[1], bucket_name, now=now), bucket_name)
                    )
                if not candidates:
                    break
                _fairness, bucket = min(candidates, key=lambda row: row[0])
            cohort, group = recovery_lanes[bucket].pop(0)
            selected.append(group)
            counts[cohort] += 1
        for cohort, rows in list(cohort_rows.items()):
            cohort_rows[cohort] = [
                (bucket, group)
                for bucket, group in rows
                if bucket not in legacy_recovery_buckets
            ]

    # One opportunity per available cohort first. This is the starvation guard.
    for cohort in MISSING_RECOVERY_COHORTS:
        if len(selected) >= max_groups:
            break
        if cohort_rows.get(cohort):
            bucket, group = cohort_rows[cohort].pop(0)
            selected.append(group)
            counts[cohort] += 1

    # Then round-robin. Prefer the configured ceiling first; once every waiting
    # cohort reaches it, refill evenly so bounded pass capacity is not idled.
    while len(selected) < max_groups:
        available = [cohort for cohort in MISSING_RECOVERY_COHORTS if cohort_rows.get(cohort)]
        if not available:
            break
        bounded = [cohort for cohort in available if counts[cohort] < max_per_cohort]
        choices = bounded or sorted(available, key=lambda cohort: counts[cohort])
        if not choices:
            break
        moved = False
        for cohort in choices:
            if len(selected) >= max_groups:
                break
            _bucket, group = cohort_rows[cohort].pop(0)
            selected.append(group)
            counts[cohort] += 1
            moved = True
        if not moved:
            break
    return selected


def scheduler_bucket_for_rows(rows, now=None):
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return "queued"
    if now is None:
        now = time.time()
    if any(has_soon_cached_slskd_autopick(row, now=now) for row in rows):
        return "cached_slskd"
    if any(slskd_source_result_reprobe_due(row, now=now) for row in rows):
        return "slskd_reprobe"
    if any(stale_downloader_send_result(row) for row in rows):
        return "stale_downloader"
    if any(queue_first_pass_priority(row) == 0 for row in rows):
        return "first_pass"
    if any(has_missing_required_source_result(row) for row in rows):
        return "missing_provider_result"
    if any(retry_effectively_due(row, now=now) for row in rows):
        return "retry_due"
    if any(str(row.get("state") or "") == "searching" for row in rows):
        return "active"
    return "queued"


def scheduler_bucket_rank(bucket):
    ranks = {
        "cached_slskd": 0,
        "slskd_reprobe": 1,
        "stale_downloader": 2,
        "first_pass": 3,
        "missing_provider_result": 4,
        "retry_due": 5,
        "active": 6,
        "queued": 7,
    }
    return ranks.get(str(bucket or ""), 99)


def due_series(queue, args):
    now = time.time()
    groups = collections.defaultdict(list)
    items = list((queue.get("items") or {}).values())
    allowed_series = set(args.series or [])
    series_activity = collections.defaultdict(float)
    group_slskd_service_at = collections.defaultdict(float)
    for item in items:
        series = item.get("series") or ""
        if allowed_series and series not in allowed_series:
            continue
        if not item.get("present_in_watch", True):
            continue
        group_key = due_group_key(item)
        group_slskd_service_at[group_key] = max(
            group_slskd_service_at[group_key],
            latest_slskd_result_signature_at(item),
        )
        if item.get("state") in TERMINAL_QUEUE_STATES:
            continue
        series_activity[group_key] = max(series_activity[group_key], queue_last_activity_ts(item))
    for item in items:
        if allowed_series and item.get("series") not in allowed_series:
            continue
        if item.get("state") in TERMINAL_QUEUE_STATES | ACTIVE_QUEUE_STATES:
            continue
        if item.get("state") == "needs_you" and not args.retry_needs_you:
            continue
        if not item.get("present_in_watch", True):
            continue
        retry_after = float(item.get("retry_after") or 0)
        if (
            retry_after > now
            and not args.force
            and not has_soon_cached_slskd_autopick(item, now=now)
        ):
            continue
        groups[due_group_key(item)].append(item)
    for rows in groups.values():
        rows.sort(key=due_row_sort_key)
    max_groups = max(1, int(args.max_series or 1))
    buckets = collections.defaultdict(list)
    for group_key, rows in groups.items():
        bucket = scheduler_bucket_for_rows(rows, now=now)
        if bucket == "slskd_reprobe":
            rows.sort(
                key=lambda item: (
                    slskd_reprobe_admission_fairness_key(item, now=now),
                    due_row_sort_key(item),
                )
            )
        buckets[bucket].append((group_key, rows, due_group_sort_key(group_key, rows, series_activity)))
    recovery_lanes = ("cached_slskd", "slskd_reprobe")
    for bucket, bucket_rows in buckets.items():
        if bucket in recovery_lanes:
            bucket_rows.sort(
                key=lambda row: (
                    # An overdue retry promise still leads this lane: we said we
                    # would come back at time T and T has passed. What changed is
                    # how the lane competes for a shared slot, not how it orders
                    # itself -- see slskd_recovery_slot_lanes.
                    slskd_reprobe_group_admission_fairness_key(
                        row[1],
                        now=now,
                        service_at=group_slskd_service_at[row[0]],
                    )
                    if bucket == "slskd_reprobe"
                    # Lead with the same service clock the shared slot is bid on,
                    # so the group this lane nominates is the group it is bidding.
                    else (
                        slskd_recovery_group_service_at(row[1]),
                        min(slskd_recovery_fairness_key(item, bucket, now=now) for item in row[1]),
                    )
                )
            )
        else:
            bucket_rows.sort(key=lambda row: (broad_group_service_key(row[1]), row[2]))

    if missing_recovery_enabled(args):
        selected = select_missing_recovery_groups(
            buckets,
            max_groups=max_groups,
            max_per_cohort=missing_recovery_max_per_cohort(args, max_groups),
            now=now,
        )
        return [(group_key[0], rows) for group_key, rows, _sort_key in selected if group_key[0]]

    selected = []

    def take(bucket):
        if len(selected) >= max_groups or not buckets.get(bucket):
            return False
        selected.append(buckets[bucket].pop(0))
        return True

    broad_lanes = (
        "first_pass",
        "retry_due",
        "stale_downloader",
        "missing_provider_result",
        "first_pass",
        "missing_provider_result",
        "retry_due",
        "active",
        "first_pass",
        "missing_provider_result",
        "retry_due",
    )

    broad_available = any(buckets.get(bucket) for bucket in broad_lanes)
    recovery_available = any(buckets.get(bucket) for bucket in recovery_lanes)
    if broad_available and recovery_available:
        recovery_capacity = 0 if max_groups <= 1 else max(1, max_groups // 3)
    elif recovery_available:
        recovery_capacity = max_groups
    else:
        recovery_capacity = 0

    recovery_slot_lanes = slskd_recovery_slot_lanes(
        {lane: len(buckets.get(lane) or []) for lane in recovery_lanes},
        recovery_capacity,
    )
    while len(selected) < recovery_capacity and any(buckets.get(bucket) for bucket in recovery_lanes):
        slot_index = len(selected)
        reserved = recovery_slot_lanes[slot_index] if slot_index < len(recovery_slot_lanes) else None
        if reserved and buckets.get(reserved):
            take(reserved)
            continue
        candidates = []
        for lane in recovery_lanes:
            if not buckets.get(lane):
                continue
            rows = buckets[lane][0][1]
            candidates.append((slskd_recovery_group_bid(rows, lane, now=now), lane))
        if not candidates:
            break
        _fairness, lane = min(candidates, key=lambda row: row[0])
        take(lane)

    while len(selected) < max_groups and any(buckets.get(bucket) for bucket in broad_lanes):
        moved = False
        for bucket in broad_lanes:
            if take(bucket):
                moved = True
                if len(selected) >= max_groups:
                    break
        if not moved:
            break

    if len(selected) < max_groups:
        leftovers = []
        for bucket, bucket_rows in buckets.items():
            leftovers.extend((scheduler_bucket_rank(bucket), *row) for row in bucket_rows)
        leftovers.sort(key=lambda row: (row[0], row[3]))
        selected.extend((group_key, rows, sort_key) for _rank, group_key, rows, sort_key in leftovers[: max_groups - len(selected)])

    return [(group_key[0], rows) for group_key, rows, _sort_key in selected if group_key[0]]


def mark_series_searching(rows, source):
    now = time.time()
    for item in rows:
        if item.get("state") not in ACTIVE_QUEUE_STATES | TERMINAL_QUEUE_STATES:
            item["state"] = "searching"
            item["current_source"] = source
            item["last_attempt_at"] = now
            item["last_attempt_at_iso"] = now_iso(now)
            item["last_event"] = source_attempt_event(source)
            touch_queue_item(item, now)


def mark_source_ladder_attempt(rows):
    now = time.time()
    for item in rows:
        if item.get("state") in ACTIVE_QUEUE_STATES | {"needs_you"} | TERMINAL_QUEUE_STATES:
            continue
        try:
            count = int(item.get("source_ladder_attempt_count") or 0)
        except (TypeError, ValueError):
            count = 0
        item["source_ladder_attempt_count"] = count + 1
        item["source_ladder_attempted_at"] = now
        item["source_ladder_attempted_at_iso"] = now_iso(now)
        item.setdefault("source_ladder_first_attempted_at", now)
        item.setdefault("source_ladder_first_attempted_at_iso", now_iso(now))
        touch_queue_item(item, now)


def source_ladder_exhausted(item, args):
    return automatic_sources_exhausted(item, int(args.exhaustion_cycles or 0))


def source_missing_provider_reservation_sources(rows, source):
    source = str(source or "").strip().lower()
    reserved = []
    seen = set()
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        if item.get("state") in ACTIVE_QUEUE_STATES | {"needs_you"} | TERMINAL_QUEUE_STATES:
            continue
        missing = missing_required_source_result_sources(item)
        if not missing or source in missing:
            continue
        for missing_source in missing:
            if missing_source in seen:
                continue
            seen.add(missing_source)
            reserved.append(missing_source)
    return reserved


def source_missing_provider_reservation_reason(rows, source):
    reserved = source_missing_provider_reservation_sources(rows, source)
    if not reserved:
        return ""
    labels = ", ".join(public_source_name(value) or value for value in reserved)
    return f"skipped; reserving runtime for missing provider checks: {labels}"


def source_eligible_rows(rows, args, source=None):
    eligible = []
    for item in rows:
        state = item.get("state")
        if state in ACTIVE_QUEUE_STATES | TERMINAL_QUEUE_STATES:
            continue
        if state == "needs_you" and not args.retry_needs_you:
            continue
        if source not in {None, "local"} and source not in queue_item_source_order(item):
            continue
        if source not in {None, "local"}:
            missing_sources = missing_required_source_result_sources(item)
            if missing_sources and source not in missing_sources:
                continue
        if source not in {None, "local", "slskd"} and has_soon_cached_slskd_autopick(item):
            continue
        eligible.append(item)
    return eligible


def source_state_counts(rows):
    return dict(collections.Counter(item.get("state") or "queued" for item in rows))


def refresh_series_row_policy(rows, now=None):
    now = time.time() if now is None else float(now)
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        item_source_order = apply_queue_item_source_policy(item, now)
        if item.get("source_order") != item_source_order:
            item["source_order"] = item_source_order
        item_recovery_steps = queue_item_recovery_steps(item)
        if item.get("recovery_steps") != item_recovery_steps:
            item["recovery_steps"] = item_recovery_steps
        clear_soft_review_metadata(item)


def local_source_summary(rows, eligible):
    counts = source_state_counts(rows)
    return {
        "state_counts": counts,
        "eligible_count": len(eligible),
        "active_or_verified_count": max(0, len(rows) - len(eligible)),
        "verified_count": int(counts.get("verified") or 0),
        "downloading_count": int(counts.get("downloading") or 0),
        "importing_count": int(counts.get("importing") or 0),
    }


def ensure_targeted_provider_evidence(queue, rows, args, deadline=None):
    queue_rows = [item for item in rows if isinstance(item, dict)]
    row_keys = list(dict.fromkeys(str(item.get("key") or "").strip() for item in queue_rows))
    if not row_keys or any(not key for key in row_keys) or len(row_keys) != len(queue_rows):
        return {
            "ok": False,
            "ready": False,
            "reason": "targeted_annotation_missing_queue_key",
            "processed": 0,
            "total": len(row_keys),
        }
    try:
        configured = max(1, int(getattr(args, "annotate_timeout_seconds", DEFAULT_ANNOTATE_TIMEOUT_SECONDS) or 1))
    except (TypeError, ValueError):
        configured = DEFAULT_ANNOTATE_TIMEOUT_SECONDS
    remaining = runtime_seconds_remaining(deadline)
    if remaining is not None:
        allowance = int(max(0.0, remaining - RUNTIME_CHILD_CLEANUP_SECONDS))
        if allowance < 1:
            return {
                "ok": False,
                "ready": False,
                "reason": "targeted_annotation_runtime_budget_reached",
                "processed": 0,
                "total": len(row_keys),
            }
        configured = min(configured, allowance)
    evidence = annotate_states(
        queue,
        max_seconds=configured,
        reason="provider_target",
        row_keys=row_keys,
    )
    evidence = dict(evidence or {}) if isinstance(evidence, dict) else {}
    evidence["ready"] = bool(
        evidence.get("ok") is True
        and int(evidence.get("processed") or 0) == len(row_keys)
        and int(evidence.get("total") or 0) == len(row_keys)
    )
    if not evidence["ready"]:
        evidence.setdefault("reason", "targeted_annotation_incomplete")
    return evidence


def invoke_provider_after_targeted_evidence(queue, eligible, args, source, runner, deadline=None):
    """Run with no I/O between exact-row evidence and the provider boundary."""
    identity_fields = (
        "key",
        "id",
        "queue_id",
        "wanted_id",
        "series_id",
        "issue_id",
        "series",
        "display_series",
        "issue_number",
        "issue",
        "number",
        "volume_number",
        "chapter_number",
        "comicvine_id",
        "queue_identity",
        "watch_id",
        "watchId",
        "kapowarr_id",
        "kapowarrId",
        "volume_id",
        "volumeId",
        "metadata_provider",
        "series_source",
        "owner",
        "ownership",
    )
    identity_before = {
        id(item): tuple(item.get(field) for field in identity_fields)
        for item in eligible
    }
    evidence = ensure_targeted_provider_evidence(queue, eligible, args, deadline=deadline)
    if not evidence.get("ready"):
        return False, None, evidence
    if any(
        identity_before.get(id(item)) != tuple(item.get(field) for field in identity_fields)
        for item in eligible
    ):
        evidence["blocked_reason"] = "target_identity_changed"
        return False, None, evidence
    source_key = source_order_attempt_key(source)
    actionable = [
        item
        for item in eligible
        if item.get("state") == "searching"
        and item.get("current_source")
        and source_order_attempt_key(item.get("current_source")) == source_key
    ]
    remaining = source_eligible_rows(eligible, args, source=source)
    remaining_ids = {id(item) for item in remaining}
    if len(remaining) != len(eligible) or any(id(item) not in remaining_ids for item in eligible):
        evidence["blocked_reason"] = "target_policy_changed"
        return False, None, evidence
    if len(actionable) != len(eligible):
        # Targeted annotation can normalize an unchanged, unresolved row back
        # to queued. Re-arm only that exact transition. Every other disposition
        # (including another source, blocked, active, or unsafe) stops the batch.
        normalized = [
            item
            for item in eligible
            if item.get("state") in {"queued", "searching"} and not item.get("current_source")
        ]
        safe_ids = {id(item) for item in actionable}
        safe_ids.update(id(item) for item in normalized)
        if len(safe_ids) != len(eligible) or any(id(item) not in safe_ids for item in eligible):
            evidence["blocked_reason"] = "target_state_changed"
            return False, None, evidence
        mark_series_searching(normalized, source)
        evidence["rows_rearmed_after_evidence"] = len(normalized)
    return True, runner(), evidence


def process_series(queue, series, rows, args, progress=None, deadline=None, provider_observer=None):
    result = {
        "series": series,
        "queue_identity": series_summary_identity(rows[0]) if rows else None,
        "missing_rows": len(rows),
        "sources": {},
        "errors": [],
    }
    provider_health = latest_inkdrop_provider_health()

    def publish(source, note):
        if progress:
            progress(series=series, source=source, note=note)

    def refresh_after_source(source):
        # Exact rows are annotated immediately before each provider call.
        # Re-annotating unrelated backlog rows here can starve later series.
        refresh_series_row_policy(rows)
        eligible = source_eligible_rows(rows, args, source=source)
        result.setdefault("source_state_counts", {})[source] = source_state_counts(rows)
        return eligible

    def skip_source(source, reason):
        result["sources"][source] = {
            "skipped": True,
            "reason": reason,
            "state_counts": source_state_counts(rows),
        }
        publish(source, reason)

    def mark_budget_retry(source=None, detail_reason=None, eligible_rows=None):
        now_budget = time.time()
        retry_after_seconds = budget_retry_seconds(args)
        result["budget_exhausted"] = True
        if source:
            result["budget_exhausted_source"] = source
        result["budget_retry_seconds"] = retry_after_seconds
        reason = "autopilot runtime budget reached; retry scheduled for the next pass"
        for item in rows:
            if item.get("state") in ACTIVE_QUEUE_STATES | {"needs_you"} | TERMINAL_QUEUE_STATES:
                continue
            item["state"] = "queued"
            item["current_source"] = None
            item.pop("needs_you_reason", None)
            schedule_retry_after(item, now_budget, retry_after_seconds)
            item["last_event"] = reason
            touch_queue_item(item, now_budget)
        if source:
            if eligible_rows is not None:
                child_provider_context = source_runtime_budget_child_provider_context(source, eligible_rows)
                if child_provider_context:
                    result.setdefault("budget_skipped_child_provider_context", {})[source] = child_provider_context
                    result.setdefault("budget_skipped_child_provider_ids", {})[source] = list(
                        child_provider_context.get("provider_ids") or []
                    )
                skipped_attempts = record_source_runtime_budget_skip_attempts(
                    source,
                    eligible_rows,
                    detail_reason or reason,
                    now=now_budget,
                    child_provider_context=child_provider_context,
                )
                if skipped_attempts:
                    result.setdefault("budget_skipped_source_attempts", {})[source] = skipped_attempts
            skip_source(source, detail_reason or reason)
        else:
            publish("queue", reason)
        return reason

    def run_source(source, start_note, finish_note, runner, applier=None, timeout_note=None, min_runtime_seconds=None):
        if runtime_deadline_expired(deadline):
            mark_budget_retry(source)
            return {"ok": False, "skipped": True, "reason": "runtime_budget_exhausted", "source": source}
        min_seconds = (
            source_runtime_min_seconds(source, args)
            if min_runtime_seconds in (None, "")
            else min_runtime_seconds
        )
        eligible = source_eligible_rows(rows, args, source=source)
        if not eligible:
            cached_waiting = sum(
                1
                for item in rows
                if item.get("state") not in ACTIVE_QUEUE_STATES | {"verified", "needs_you"}
                and has_soon_cached_slskd_autopick(item)
            )
            reservation_reason = source_missing_provider_reservation_reason(rows, source)
            reason = (
                reservation_reason
                if reservation_reason
                else (
                "skipped; rows already have safe SLSKD candidates waiting for transfer slots"
                if source != "slskd" and cached_waiting
                else "skipped; all rows are already downloading, waiting for downloader confirmation, importing, verified, or waiting for you"
                )
            )
            skip_source(source, reason)
            return None
        if runtime_deadline_too_close(deadline, min_seconds):
            reason = runtime_budget_skip_reason(source, deadline, min_seconds)
            mark_budget_retry(source, detail_reason=reason, eligible_rows=eligible)
            log("source_skipped_for_runtime_budget", series=series, source=source, reason=reason)
            return {
                "ok": False,
                "skipped": True,
                "reason": "runtime_budget_too_close",
                "detail": reason,
                "source": source,
            }
        try:
            row_snapshots = {
                id(item): {
                    key: item.get(key)
                    for key in ("state", "current_source", "last_event", "retry_after", "retry_after_iso")
                }
                for item in eligible
            }
            attempt_counts_before = {
                id(item): int(queue_item_recorded_source_result_attempt_count(item, source))
                for item in eligible
                if isinstance(item, dict)
            }
            mark_series_searching(eligible, source)
            started_attempts = record_source_started_attempts(eligible, source, start_note)
            if started_attempts:
                save_queue_progress_snapshot(queue)
            log(
                "source_started",
                series=series,
                source=source,
                eligible_count=len(eligible),
                started_attempts=started_attempts,
            )
            publish(source, start_note)
            invoked, payload, targeted_evidence = invoke_provider_after_targeted_evidence(
                queue,
                eligible,
                args,
                source,
                runner,
                deadline=deadline,
            )
            result.setdefault("targeted_annotations", {})[source] = targeted_evidence
            if not invoked:
                if not targeted_evidence.get("ready"):
                    for item in eligible:
                        snapshot = row_snapshots.get(id(item)) or {}
                        for key, value in snapshot.items():
                            if value is None:
                                item.pop(key, None)
                            else:
                                item[key] = value
                        clear_source_started_marker(item, source)
                if not targeted_evidence.get("ready"):
                    result["evidence_deferred"] = True
                else:
                    result["evidence_changed_state"] = True
                reason = (
                    "required file and completion checks did not finish; retrying before source search"
                    if not targeted_evidence.get("ready")
                    else "file or completion evidence changed before source search"
                )
                result["sources"][source] = {
                    "skipped": True,
                    "reason": reason,
                    "state_counts": source_state_counts(rows),
                }
                return {"ok": False, "skipped": True, "reason": "targeted_annotation_blocked", "source": source}
            applied = None
            if applier:
                applied = applier(payload)
            summary = summarize_source_result(source, payload)
            if applied is not None:
                summary["applied"] = applied
            no_row_attempts = record_source_no_row_result_attempts(
                source,
                payload,
                eligible,
                attempt_counts_before,
            )
            if no_row_attempts:
                summary["row_result_attempts"] = no_row_attempts
            result["sources"][source] = summary
            eligible = refresh_after_source(source)
            if eligible:
                publish(source, finish_note)
            else:
                publish(source, f"{finish_note}; no remaining rows need this ladder pass")
            return payload
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            result["errors"].append({"source": source, "error": error})
            log("source_error", series=series, source=source, error=error)
            now_error = time.time()
            for item in eligible:
                if item.get("state") not in ACTIVE_QUEUE_STATES | {"needs_you"} | TERMINAL_QUEUE_STATES:
                    mark_source_error_retry(item, source, error, now_error, args)
            if timeout_note:
                publish(source, timeout_note)
            else:
                publish(source, f"{public_source_name(source) or source} source errored; automatic retry scheduled")
            refresh_after_source(source)
            return None

    mark_source_ladder_attempt(rows)
    publish("queue", "starting source ladder")
    publish("local", "checking managed-folder/library truth before searching")
    eligible = refresh_after_source("local")
    result["sources"]["local"] = local_source_summary(rows, eligible)
    if not eligible:
        publish("local", "managed-folder/library truth already accounts for every row")
    else:
        publish("local", "managed-folder/library check finished; continuing source ladder")

    def apply_slskd_payload(payload):
        applied = {"checked": {"checked": 0, "needs_you": 0, "attempts": 0}, "auto_grab": {"started": 0, "safe": 0, "user_load_wait": 0, "transient_retry": 0}}
        if not isinstance(payload, dict):
            return applied
        if payload.get("skipped_busy"):
            now_busy = time.time()
            busy_reason = payload.get("reason") or "SLSKD probe is already running; retrying shortly."
            for item in source_eligible_rows(rows, args, source="slskd"):
                if item.get("state") not in ACTIVE_QUEUE_STATES | {"verified", "needs_you"}:
                    mark_source_busy_retry(item, "slskd", busy_reason, now_busy, args)
        applied["checked"] = apply_slskd_checked(queue, payload)
        applied["auto_grab"] = apply_slskd_auto_grab(queue, payload)
        result["slskd_checked"] = applied["checked"]
        result["slskd_auto_grab"] = applied["auto_grab"]
        return applied

    def slskd_batch_kwargs():
        eligible_count = len(source_eligible_rows(rows, args, source="slskd"))
        return slskd_broad_probe_kwargs(args, eligible_count=eligible_count, row_count=len(rows))

    if getattr(args, "skip_failed_retry", False):
        skip_source("failed_retry", "skipped; failed-download retry is disabled")
    elif runtime_deadline_expired(deadline):
        mark_budget_retry("failed_retry")
    else:
        run_source(
            "failed_retry",
            "checking failed downloads for alternate candidates",
            "failed-download retry finished",
            lambda: run_failed_retry(series, args, provider_observer=provider_observer),
            lambda payload: apply_failed_retry_to_queue(queue, payload),
            timeout_note="failed-download retry timed out; continuing source ladder",
        )
    source_runners = {
        "mangadex": (
            "downloading from MangaDex",
            "MangaDex direct download finished",
            lambda: run_source_worker_mangadex(
                series, rows, args, deadline=deadline, provider_observer=provider_observer
            ),
            lambda payload: apply_source_worker_mangadex_result_to_queue(queue, payload)
            if isinstance(payload, dict) and payload.get("mode") == "source_worker"
            else apply_mangadex_result_to_queue(queue, payload),
        ),
        "prowlarr": (
            "searching Prowlarr/SAB/qB",
            "Prowlarr/SAB/qB search finished",
            lambda: run_source_worker_prowlarr(series, rows, args, provider_observer=provider_observer),
            lambda payload: apply_source_worker_prowlarr_result_to_queue(queue, payload)
            if isinstance(payload, dict) and payload.get("mode") == "source_worker"
            else apply_result_to_queue(queue, payload, "prowlarr"),
        ),
        "rss": (
            "searching RSS sources",
            "RSS search finished",
            lambda: run_source_worker_rss(
                series, rows, args, deadline=deadline, provider_observer=provider_observer
            ),
            lambda payload: apply_source_worker_rss_result_to_queue(queue, payload)
            if isinstance(payload, dict) and payload.get("mode") == "source_worker"
            else apply_result_to_queue(queue, payload, "rss"),
        ),
        "comicscodes": (
            "searching ComicsCodes",
            "ComicsCodes search finished",
            lambda: run_comicscodes(series, args, deadline=deadline, provider_observer=provider_observer),
            lambda payload: apply_result_to_queue(queue, payload, "comicscodes"),
        ),
        "slskd": (
            "searching SLSKD and autopicking safe fallback candidates",
            "SLSKD fallback search finished",
            lambda: run_slskd(
                series,
                args,
                **slskd_batch_kwargs(),
                deadline=deadline,
                progress=lambda note: publish("slskd", note),
                provider_observer=provider_observer,
            ),
            apply_slskd_payload,
        ),
    }
    source_order = source_order_for_rows(rows)

    def first_runtime_budget_eligible_source():
        for candidate in source_order:
            if candidate == "local":
                continue
            if candidate == "slskd" and getattr(args, "skip_slskd_broad_due_to_busy", False):
                continue
            if candidate == "prowlarr" and getattr(args, "skip_prowlarr", False):
                continue
            if candidate not in source_runners:
                continue
            if source_eligible_rows(rows, args, source=candidate):
                return candidate
        return ""

    for source in source_order_for_rows(rows):
        if source == "local":
            continue
        if runtime_deadline_expired(deadline):
            budget_source = first_runtime_budget_eligible_source() or source
            eligible_rows = source_eligible_rows(rows, args, source=budget_source)
            min_runtime_seconds = source_runtime_min_seconds(
                budget_source,
                args,
                slskd_kwargs=slskd_batch_kwargs() if budget_source == "slskd" else None,
            )
            reason = runtime_budget_skip_reason(budget_source, deadline, min_runtime_seconds)
            mark_budget_retry(budget_source, detail_reason=reason, eligible_rows=eligible_rows)
            break
        if source == "slskd" and getattr(args, "skip_slskd_broad_due_to_busy", False):
            skip_source("slskd", "skipped; SLSKD hot retry is busy, continuing other sources")
            continue
        if source == "prowlarr" and getattr(args, "skip_prowlarr", False):
            skip_source("prowlarr", "skipped; Prowlarr provider is disabled")
            continue
        health_skip_reason = source_health_skip_reason(source, provider_health)
        if health_skip_reason:
            now_health_skip = time.time()
            for item in source_eligible_rows(rows, args, source=source):
                record_provider_health_skip_attempt(
                    item,
                    source,
                    health_skip_reason,
                    provider_health,
                    now=now_health_skip,
                )
                if item.get("current_source") == source:
                    item["current_source"] = None
                    item["last_event"] = f"{health_skip_reason}; continuing source ladder"
                    touch_queue_item(item, now_health_skip)
            skip_source(source, health_skip_reason)
            continue
        source_runner = source_runners.get(source)
        if not source_runner:
            continue
        start_note, finish_note, runner, applier = source_runner
        timeout_note = "Prowlarr timed out; continuing source ladder" if source == "prowlarr" else None
        min_runtime_seconds = source_runtime_min_seconds(
            source,
            args,
            slskd_kwargs=slskd_batch_kwargs() if source == "slskd" else None,
        )
        payload = run_source(
            source,
            start_note,
            finish_note,
            runner,
            applier,
            timeout_note=timeout_note,
            min_runtime_seconds=min_runtime_seconds,
        )
        if source == "slskd" and isinstance(payload, dict):
            refresh_after_source("slskd")
    now = time.time()
    for item in rows:
        if item.get("state") in ACTIVE_QUEUE_STATES | {"needs_you"} | TERMINAL_QUEUE_STATES:
            continue
        if result.get("budget_exhausted"):
            item["state"] = "queued"
            item["current_source"] = None
            item.pop("needs_you_reason", None)
            schedule_retry_after(item, now, int(result.get("budget_retry_seconds") or budget_retry_seconds(args)))
            item["last_event"] = "autopilot runtime budget reached; retry scheduled for the next pass"
            continue
        if source_error_retry_fresh(item, now) or source_busy_retry_fresh(item, now):
            continue
        missing_source_results = update_pending_source_result_markers(item)
        if missing_source_results and (no_actionable_source_result(item) or automatic_source_retry_event(item)):
            item["state"] = "queued"
            item["current_source"] = None
            item.pop("needs_you_reason", None)
            schedule_retry_after(item, now, args.retry_seconds)
            missing_labels = item.get("pending_source_result_sources_text") or ", ".join(missing_source_results)
            item["last_event"] = f"automatic sources still need checks from {missing_labels}; retry scheduled"
            continue
        user_load_wait = slskd_user_load_limited(item)
        if user_load_wait:
            retry_delay = SLSKD_USER_LOAD_RETRY_SECONDS
            retry_after = now + retry_delay
            item["state"] = "queued"
            item["current_source"] = None
            item["retry_after"] = retry_after
            item["retry_after_iso"] = now_iso(retry_after)
            item["last_event"] = "SLSKD candidate ready; waiting for transfer slot"
            item.pop("needs_you_reason", None)
            continue
        if source_ladder_exhausted(item, args):
            mark_automation_exhausted(item, now, source="source_ladder")
            continue
        retry_delay = args.retry_seconds
        try:
            attempt_count = int(item.get("source_ladder_attempt_count") or 0)
        except (TypeError, ValueError):
            attempt_count = 0
        if automatic_source_retry_event(item) and attempt_count >= int(args.exhaustion_cycles or 0):
            mark_extended_source_ladder_retry(item, now)
            continue
        if no_actionable_source_result(item) and attempt_count >= int(args.exhaustion_cycles or 0):
            retry_delay = max(
                retry_delay,
                no_actionable_source_retry_delay(
                    item,
                    int(args.exhaustion_cycles or DEFAULT_EXHAUSTION_CYCLES),
                    base_retry_seconds=args.retry_seconds,
                ),
            )
        item["state"] = "queued"
        item["current_source"] = None
        schedule_retry_after(item, now, retry_delay)
        item["last_event"] = (
            "automatic sources had no actionable candidate; extended retry scheduled"
            if retry_delay > NO_ACTIONABLE_SOURCE_RETRY_SECONDS
            else "automatic sources had no actionable candidate; retry scheduled"
            if retry_delay > args.retry_seconds
            else "source ladder attempted; retry scheduled"
        )
    result["history_event"] = record_autopilot_series_history(result, rows)
    return result


def item_ready_to_import(item):
    return (
        item.get("state") == "importing"
        and str(item.get("last_reconcile_state") or "") in {"completed_in_client", "ready_to_import"}
    )


def state_ready_import_count():
    if not INKDROP_STATE_DB.exists():
        return 0
    try:
        status_placeholders = ",".join("?" for _ in INKDROP_STATE_IMPORT_READY_STATUSES)
        db_uri = f"file:{INKDROP_STATE_DB}?mode=ro"
        # Closed explicitly: `with` on a connection ends the transaction but
        # leaves the file handle open until the object is collected.
        con = sqlite3.connect(db_uri, uri=True, timeout=1.0)
        try:
            con.execute("pragma query_only = 1")
            con.execute("pragma busy_timeout = 500")
            row = con.execute(
                f"""
                select count(distinct q.id)
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
                """,
                tuple(INKDROP_STATE_IMPORT_READY_STATUSES),
            ).fetchone()
        finally:
            con.close()
        return int(row[0] or 0) if row else 0
    except Exception as exc:
        log("state_ready_import_count_failed", error=f"{type(exc).__name__}: {exc}")
        return 0


def undiscoverable_ready_import_count():
    """Count ready imports the import worker cannot actually see.

    The gate counts download_tasks in the state DB; the import worker finds its
    work in download_reconciliation, which lives in imported-files.sqlite3. A
    row present in the first and absent from the second is counted as backlog
    and can never be drained, because nothing will ever pick it up.

    Returns None when the answer cannot be established -- a missing table, an
    unreadable file -- so callers fail open and keep the old behaviour rather
    than dropping a backlog that may be real.
    """

    if not INKDROP_STATE_DB.exists() or not IMPORTED_DB.exists():
        return None
    try:
        status_placeholders = ",".join("?" for _ in INKDROP_STATE_IMPORT_READY_STATUSES)
        # Closed explicitly rather than via `with`: the context manager ends the
        # transaction but leaves the handle -- and the attached second database
        # file -- open.
        con = sqlite3.connect(f"file:{INKDROP_STATE_DB}?mode=ro", uri=True, timeout=1.0)
        try:
            con.execute("pragma busy_timeout = 500")
            con.execute("attach database ? as impdb", (f"file:{IMPORTED_DB}?mode=ro",))
            try:
                row = con.execute(
                    f"""
                    select count(distinct q.id)
                    from queue_items q
                    join download_tasks dt on dt.queue_id=q.id
                    where q.active=1
                      and lower(coalesce(q.state, ''))='importing'
                      and lower(coalesce(dt.state, ''))='import_ready'
                      and lower(coalesce(dt.status, '')) in ({status_placeholders})
                      and nullif(trim(coalesce(dt.local_path, '')), '') is not null
                      and not exists (
                        select 1 from impdb.download_reconciliation r
                         where r.inkdrop_download_task_id = dt.id
                      )
                    """,
                    tuple(INKDROP_STATE_IMPORT_READY_STATUSES),
                ).fetchone()
            finally:
                con.execute("detach database impdb")
        finally:
            con.close()
        return int(row[0] or 0) if row else 0
    except Exception as exc:
        log("undiscoverable_ready_import_count_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def import_backlog_gate_state_path():
    return STATE_DIR / IMPORT_BACKLOG_GATE_STATE_FILENAME


def load_import_backlog_gate_state():
    try:
        with open(import_backlog_gate_state_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_import_backlog_gate_state(payload):
    try:
        path = import_backlog_gate_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except Exception as exc:
        log("import_backlog_gate_state_write_failed", error=f"{type(exc).__name__}: {exc}")


def import_backlog_stall_state(ready, active, now=None):
    """Decide whether a backlog may keep deferring the search cycle.

    Two independent releases, because the first one alone was not enough.

    The stall window: progress means the count reaching a new low, so a backlog
    that never gets below its own smallest observed value stops holding
    priority once the window elapses.

    That is correct but not sufficient on a live catalog. Every partial drain
    is a genuine new low, resets the clock, and buys another two hours during
    which search is deferred on essentially every cycle. Measured on
    production: 92 of roughly 200 consecutive cycle records deferred with
    reason ready_import_backlog_priority, while series that had never been
    searched once waited. The backlog was making progress the whole time -- the
    gate was behaving exactly as designed and still starving search.

    So the number of deferrals in a row is bounded as well. After that many
    consecutive deferred cycles the next one searches regardless of how well
    the backlog is draining, and the counter starts again. Imports keep the
    large majority of cycles; searching just stops being able to wait forever.
    """

    now = float(now if now is not None else time.time())
    previous = load_import_backlog_gate_state()
    if not active:
        if previous:
            save_import_backlog_gate_state({})
        return {
            "stalled": False,
            "held_seconds": 0.0,
            "min_ready_seen": None,
            "consecutive_defers": 0,
            "release_reason": "",
        }
    try:
        previous_min = int(previous.get("min_ready_seen"))
    except (TypeError, ValueError):
        previous_min = None
    try:
        since = float(previous.get("since"))
    except (TypeError, ValueError):
        since = None
    try:
        defers = int(previous.get("consecutive_defers") or 0)
    except (TypeError, ValueError):
        defers = 0
    progressed = previous_min is None or ready < previous_min
    if progressed or since is None:
        since = now
        previous_min = ready
    held_seconds = max(0.0, now - since)
    window_stalled = bool(
        AUTOPILOT_IMPORT_BACKLOG_STALL_SECONDS
        and held_seconds >= AUTOPILOT_IMPORT_BACKLOG_STALL_SECONDS
    )
    defers += 1
    defer_capped = bool(
        AUTOPILOT_IMPORT_BACKLOG_MAX_CONSECUTIVE_DEFERS
        and defers > AUTOPILOT_IMPORT_BACKLOG_MAX_CONSECUTIVE_DEFERS
    )
    stalled = window_stalled or defer_capped
    if stalled:
        # This cycle searches, so the run of deferrals restarts from here.
        defers = 0
    save_import_backlog_gate_state({
        "since": since,
        "since_iso": now_iso(since),
        "min_ready_seen": previous_min,
        "last_ready": ready,
        "last_seen_at": now,
        "consecutive_defers": defers,
    })
    return {
        "stalled": stalled,
        "held_seconds": round(held_seconds, 1),
        "min_ready_seen": previous_min,
        "consecutive_defers": defers,
        "release_reason": "stall_window" if window_stalled else ("defer_cap" if defer_capped else ""),
    }


def import_backlog_priority_gate(queue=None, now=None):
    queue = queue if isinstance(queue, dict) else {}
    queue_ready = sum(
        1
        for item in (queue.get("items") or {}).values()
        if isinstance(item, dict) and item_ready_to_import(item)
    )
    state_ready = state_ready_import_count()
    undiscoverable = undiscoverable_ready_import_count()
    drainable_state_ready = state_ready
    if undiscoverable is not None:
        drainable_state_ready = max(0, int(state_ready or 0) - int(undiscoverable))
    ready = max(int(queue_ready or 0), int(drainable_state_ready or 0))
    priority_active = ready >= AUTOPILOT_IMPORT_BACKLOG_PRIORITY_MIN
    hard_blocked = ready >= AUTOPILOT_IMPORT_BACKLOG_HARD_LIMIT
    stall = import_backlog_stall_state(ready, priority_active or hard_blocked, now=now)
    if stall.get("stalled"):
        priority_active = False
        hard_blocked = False
    return {
        # Finish durable acquisition work before starting another provider cycle.
        "active": priority_active or hard_blocked,
        "priority_active": priority_active,
        "hard_blocked": hard_blocked,
        "ready_import_count": ready,
        "state_ready_import_count": state_ready,
        "drainable_ready_import_count": drainable_state_ready,
        "undiscoverable_ready_import_count": undiscoverable,
        "queue_ready_import_count": queue_ready,
        "threshold": AUTOPILOT_IMPORT_BACKLOG_PRIORITY_MIN,
        "hard_limit": AUTOPILOT_IMPORT_BACKLOG_HARD_LIMIT,
        "stall_seconds": AUTOPILOT_IMPORT_BACKLOG_STALL_SECONDS,
        "stalled": bool(stall.get("stalled")),
        "held_seconds": stall.get("held_seconds"),
        "consecutive_defers": stall.get("consecutive_defers"),
        "max_consecutive_defers": AUTOPILOT_IMPORT_BACKLOG_MAX_CONSECUTIVE_DEFERS,
        "release_reason": stall.get("release_reason"),
        "reason": "import_backlog_stalled_search_resumed" if stall.get("stalled") else "ready_import_backlog_priority",
    }


def write_import_backlog_priority_deferred_status(args, import_backlog_gate, previous_status=None, gate_stage="pre_queue_load"):
    previous_status = previous_status if isinstance(previous_status, dict) else {}
    sync_result = {
        "ok": True,
        "deferred": True,
        "reason": "ready_import_backlog_priority",
        "import_backlog_gate": import_backlog_gate,
        "gate_stage": gate_stage,
    }
    reconcile = {
        "skipped": True,
        "reason": "ready_import_backlog_priority",
        "import_backlog_gate": import_backlog_gate,
        "gate_stage": gate_stage,
    }
    note = f"deferred; {import_backlog_gate.get('ready_import_count')} ready imports waiting for import worker"
    status = write_status(
        {},
        status_payload(
            args,
            sync_result,
            reconcile,
            [],
            fatal_error=None,
            in_progress=False,
            current_series=None,
            current_source="import",
            progress_note=note,
            last_processed_series=previous_status.get("last_processed_series"),
            import_backlog_priority=True,
            import_backlog_gate=import_backlog_gate,
            deferred_reason="ready_import_backlog_priority",
        ),
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return status


def queue_scheduler_summary(items, now=None):
    now = time.time() if now is None else float(now)
    out = {
        "retry_due": 0,
        "retry_later": 0,
        "retry_unscheduled": 0,
        "first_pass": 0,
        "cached_slskd": 0,
        "stale_downloader": 0,
        "missing_provider_result": 0,
        "next_retry_after": None,
        "next_retry_after_iso": "",
    }
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("state") != "queued":
            continue
        if has_soon_cached_slskd_autopick(item, now=now):
            out["cached_slskd"] += 1
        if stale_downloader_send_result(item):
            out["stale_downloader"] += 1
        if queue_first_pass_priority(item) == 0:
            out["first_pass"] += 1
        if missing_required_source_result_due(item, now=now):
            out["missing_provider_result"] += 1
        retry_after = retry_after_ts(item)
        if retry_after > 0 and retry_after <= now:
            out["retry_due"] += 1
        elif retry_after > now:
            out["retry_later"] += 1
            current_next = numeric_timestamp(out.get("next_retry_after"))
            if current_next <= 0 or retry_after < current_next:
                out["next_retry_after"] = retry_after
                out["next_retry_after_iso"] = now_iso(retry_after)
        else:
            out["retry_unscheduled"] += 1
    return out


def queue_summary(queue):
    all_items = [
        item
        for item in (queue.get("items") or {}).values()
        if isinstance(item, dict)
    ]
    retired_count = sum(1 for item in all_items if item.get("state") in RETIRED_QUEUE_STATES)
    inactive_count = sum(1 for item in all_items if not item.get("present_in_watch", True))
    items = [
        item
        for item in all_items
        if item.get("state") not in RETIRED_QUEUE_STATES
        and item.get("present_in_watch", True)
    ]
    counts = collections.Counter(item.get("state") or "queued" for item in items)
    scheduler = queue_scheduler_summary(items)
    ready_importing = sum(1 for item in items if item_ready_to_import(item))
    active = (
        counts["queued"]
        + counts["searching"]
        + counts["source_wait"]
        + counts["downloading"]
        + counts["importing"]
        + counts["needs_you"]
    )
    series = {}
    by_series = collections.defaultdict(list)
    title_identities = collections.defaultdict(set)
    for item in items:
        if item.get("state") in TERMINAL_QUEUE_STATES:
            continue
        name = item.get("series") or "Unknown"
        identity = series_summary_identity(item) or f"title:{normalize(name)}"
        title_identities[normalize(name)].add(identity)
        group_key = f"{normalize(name)}|{identity}"
        by_series[group_key].append(item)
    duplicate_titles = {
        title
        for title, identities in title_identities.items()
        if title and len(identities) > 1
    }

    def row_activity_ts(row):
        best = 0
        for key in (
            "updated_at",
            "last_slskd_autoresolve_at",
            "last_action_at",
            "last_review_at",
            "last_attempt_at",
            "source_ladder_attempted_at",
            "completed_at",
        ):
            try:
                best = max(best, float(row.get(key) or 0))
            except (TypeError, ValueError):
                pass
        return best

    def representative_row(rows, state):
        wanted = str(state or "").lower().replace("needs you", "needs_you")
        if wanted == "ready to import":
            wanted = "importing"
        candidates = [row for row in rows if str(row.get("state") or "queued") == wanted]
        if not candidates:
            candidates = list(rows)
        source_rank = {"slskd": 3, "prowlarr": 2, "rss": 2, "comicscodes": 2}
        return max(
            candidates,
            key=lambda row: (
                source_rank.get(str(public_source_name(row.get("current_source")) or ""), 0),
                row_activity_ts(row),
            ),
        ) if candidates else {}

    def series_detail(row, state):
        event = public_event_label(row.get("last_event"))
        if not event:
            return ""
        issue = row.get("issue")
        prefix = f"#{issue}: " if issue not in (None, "") else ""
        detail = f"{prefix}{event}"
        if len(detail) > 150:
            detail = detail[:147].rstrip() + "..."
        return detail

    for group_key, rows in by_series.items():
        first = rows[0] if rows else {}
        name = first.get("series") or "Unknown"
        identity = series_summary_identity(first)
        duplicate_title = normalize(name) in duplicate_titles
        display_name = series_summary_display_name(first, duplicate_title)
        row_counts = collections.Counter(row.get("state") or "queued" for row in rows)
        ready_count = sum(1 for row in rows if item_ready_to_import(row))
        active_importing = max(0, row_counts["importing"] - ready_count)
        if row_counts["needs_you"]:
            state = "Needs You"
        elif row_counts["downloading"]:
            state = "Downloading"
        elif row_counts["source_wait"]:
            state = "Source Wait"
        elif active_importing:
            state = "Importing"
        elif ready_count:
            state = "Importing"
        elif row_counts["searching"] or row_counts["queued"]:
            state = "Searching"
        else:
            state = "Verified"
        active_row = representative_row(rows, state)
        series[group_key] = {
            "series": name,
            "display_series": display_name,
            "series_key": group_key,
            "queue_identity": identity,
            "watch_id": first.get("watch_id"),
            "kapowarr_id": first.get("kapowarr_id"),
            "comicvine_id": first.get("comicvine_id"),
            "watch_year": first.get("watch_year"),
            "watch_publisher": first.get("watch_publisher"),
            "state": state,
            "detail": series_detail(active_row, state),
            "current_issue": active_row.get("issue"),
            "current_source": public_source_name(active_row.get("current_source")),
            "last_event": active_row.get("last_event"),
            "total": len(rows),
            "queued": row_counts["queued"],
            "searching": row_counts["searching"],
            "source_wait": row_counts["source_wait"],
            "downloading": row_counts["downloading"],
            "importing": row_counts["importing"],
            "ready_importing": ready_count,
            "needs_you": row_counts["needs_you"],
        }
    return {
        "total": len(items),
        "retired": retired_count,
        "inactive": inactive_count,
        "active": active,
        "queued": counts["queued"],
        "searching": counts["searching"],
        "source_wait": counts["source_wait"],
        "downloading": counts["downloading"],
        "importing": counts["importing"],
        "ready_importing": ready_importing,
        "verified": counts["verified"],
        "needs_you": counts["needs_you"],
        "scheduler": scheduler,
        "retry_due": scheduler["retry_due"],
        "retry_later": scheduler["retry_later"],
        "retry_unscheduled": scheduler["retry_unscheduled"],
        "first_pass": scheduler["first_pass"],
        "cached_slskd": scheduler["cached_slskd"],
        "stale_downloader": scheduler["stale_downloader"],
        "series": sorted(series.values(), key=lambda row: normalize(row.get("display_series") or row["series"]))[:100],
    }


def build_status(queue, payload):
    summary = queue_summary(queue)
    worker_running = bool(payload.get("in_progress"))
    queue_active = bool(
        summary["searching"]
        or summary["source_wait"]
        or summary["downloading"]
        or summary["importing"]
        or summary["queued"]
    )
    if worker_running:
        state = "running"
    elif payload.get("fatal_error"):
        state = "error"
    elif summary["needs_you"]:
        state = "needs_you"
    elif queue_active:
        state = "watching"
    else:
        state = "idle"
    status = {
        "ok": not payload.get("fatal_error"),
        "generated_at": time.time(),
        "generated_at_iso": now_iso(),
        "state": state,
        "queue_active": queue_active,
        "summary": summary,
        **payload,
    }
    return status


def queue_progress_counts(queue):
    """Cheap worker-heartbeat counts; never run readiness or retry checks."""
    counts = collections.Counter()
    total = 0
    for item in ((queue or {}).get("items") or {}).values():
        if not isinstance(item, dict) or not item.get("present_in_watch", True):
            continue
        state = str(item.get("state") or "queued")
        if state in RETIRED_QUEUE_STATES:
            continue
        total += 1
        counts[state] += 1
    active = sum(counts[state] for state in ("queued", "searching", "source_wait", "downloading", "importing", "needs_you"))
    return {"total": total, "active": active, **dict(counts)}


def automatic_search_health(payload):
    payload = payload if isinstance(payload, dict) else {}
    sync_result = payload.get("sync_result") if isinstance(payload.get("sync_result"), dict) else {}
    annotate = sync_result.get("annotate") if isinstance(sync_result.get("annotate"), dict) else {}
    reason_text = " ".join(
        str(value or "").lower()
        for value in (
            sync_result.get("reason"), sync_result.get("error"), annotate.get("reason"), annotate.get("error")
        )
    )
    maintenance_timed_out = "timeout" in reason_text or "timed_out" in reason_text or "timed out" in reason_text
    maintenance_degraded = bool(
        maintenance_timed_out
        or sync_result.get("ok") is False
        or annotate.get("ok") is False
        or sync_result.get("startup_sync_deferred")
        or sync_result.get("metadata_sync_deferred")
        or annotate.get("startup_budget_limited")
    )
    provider_started = bool(payload.get("provider_work_started") or sync_result.get("provider_work_started"))
    provider_healthy = bool(payload.get("provider_work_healthy") or sync_result.get("provider_work_healthy"))
    if payload.get("operator_paused"):
        return {"state": "operator_paused", "label": "Automatic Search is paused by the operator."}
    if payload.get("acquisition_worker_available") is False or payload.get("fatal_error"):
        return {"state": "worker_unavailable", "label": "The acquisition worker is unavailable."}
    if sync_result.get("source_worker_pressure_yield") and not provider_started:
        return {"state": "worker_unavailable", "label": "The acquisition worker is unavailable for this pass."}
    if payload.get("source_configuration_missing"):
        return {"state": "waiting_for_configuration", "label": "Automatic Search needs a configured source."}
    if sync_result.get("provider_start_deadline_missed") and not provider_started:
        return {
            "state": "provider_start_deadline_missed",
            "label": "Maintenance used the provider search window; no source search started.",
        }
    if sync_result.get("late_provider_start") or (
        provider_started and sync_result.get("provider_work_started_before_half_runtime") is False
    ):
        return {
            "state": "late_provider_start",
            "label": "A source search started too late in this Automatic Search pass.",
        }
    if maintenance_timed_out:
        return {
            "state": "provider_healthy_maintenance_timed_out" if provider_healthy else "maintenance_timed_out",
            "label": "Provider searches are working, but maintenance timed out." if provider_healthy else "Maintenance timed out before provider health was confirmed.",
        }
    if maintenance_degraded:
        return {
            "state": "provider_healthy_maintenance_degraded" if provider_healthy else "maintenance_degraded",
            "label": "Provider searches are working, but maintenance needs attention." if provider_healthy else "Maintenance needs attention; provider health is not confirmed.",
        }
    if provider_healthy:
        return {"state": "healthy", "label": "Automatic Search is running normally."}
    if provider_started:
        return {
            "state": "provider_health_unconfirmed",
            "label": "Automatic Search called a source, but source health is not confirmed yet.",
        }
    return {
        "state": "waiting_for_provider",
        "label": "Automatic Search has not started a source search yet.",
    }


def build_progress_status(queue, payload, previous_status=None):
    """Build a cheap heartbeat without recomputing the public queue summary."""
    previous = previous_status if isinstance(previous_status, dict) else {}
    counts = queue_progress_counts(queue)
    queue_active = bool(counts.get("active"))
    status = dict(previous)
    status.update(
        {
            "ok": not payload.get("fatal_error"),
            "generated_at": time.time(),
            "generated_at_iso": now_iso(),
            "state": "running" if payload.get("in_progress") else ("error" if payload.get("fatal_error") else "watching" if queue_active else "idle"),
            "queue_active": queue_active,
            "progress_counts": counts,
            "summary": previous.get("summary") if isinstance(previous.get("summary"), dict) else {},
            "summary_refreshed": False,
            **payload,
        }
    )
    status["automatic_search_health"] = automatic_search_health(status)
    return status


def record_worker_activity_status(status):
    if inkdrop_state is None:
        return None
    status = status if isinstance(status, dict) else {}
    try:
        state = "running" if status.get("queue_active") else (status.get("state") or "idle")
        current_source = str(status.get("current_source") or "queue").strip().lower() or "queue"
        label = "Series autopilot running" if status.get("queue_active") else "Series autopilot idle"
        if status.get("fatal_error"):
            state = "error"
            label = "Series autopilot error"
        return inkdrop_state.record_worker_activity(
            INKDROP_STATE_DB,
            "series_autopilot",
            {
                "lane": "autopilot",
                "source": current_source,
                "state": state,
                "status_label": label,
                "next_action": status.get("progress_note") or status.get("last_processed_series") or "",
                "pid": os.getpid(),
                "lock_path": str(LOCK_DIR / "inkdrop-series-autopilot.lock"),
                "started_at": status.get("startup_heartbeat_at") or status.get("generated_at"),
                "heartbeat_at": status.get("generated_at") or time.time(),
                "ttl_seconds": 30 * 60,
                "current_series": status.get("current_series"),
                "current_source": status.get("current_source"),
                "processed_count": len(status.get("processed") or []) if isinstance(status.get("processed"), list) else None,
                "worker_heartbeat_age_seconds": status.get("worker_heartbeat_age_seconds"),
            },
        )
    except Exception as exc:
        log("worker_activity_status_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def write_status(queue, payload):
    status = build_status(queue, payload)
    status["summary_refreshed"] = True
    status["automatic_search_health"] = automatic_search_health(status)
    write_json(STATUS_FILE, status)
    record_worker_activity_status(status)
    return status


def write_progress_status(queue, payload):
    previous_status = read_json(STATUS_FILE, {}) or {}
    status = build_progress_status(queue, payload, previous_status=previous_status)
    write_json(STATUS_FILE, status)
    record_worker_activity_status(status)
    return status


def startup_current_series(args):
    series = [
        str(value or "").strip()
        for value in (getattr(args, "series", []) or [])
        if str(value or "").strip()
    ]
    if len(series) == 1:
        return series[0]
    if len(series) > 1:
        return f"{len(series)} selected series"
    return None


def write_startup_progress(queue, args, note=None, source="queue"):
    series = startup_current_series(args)
    note = str(note or "").strip() or (f"starting {series}" if series else "starting scheduled worker")
    source = str(source or "queue").strip() or "queue"
    try:
        return write_progress_status(
            queue,
            status_payload(
                args,
                {},
                {},
                [],
                fatal_error=None,
                in_progress=True,
                current_series=series,
                current_source=source,
                progress_note=note,
                last_processed_series=None,
                startup_heartbeat=True,
                startup_heartbeat_at=time.time(),
                startup_heartbeat_at_iso=now_iso(),
            ),
        )
    except Exception as exc:
        log("startup_heartbeat_failed", error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": str(exc)}


def write_startup_heartbeat(queue, args):
    return write_startup_progress(queue, args)


def process_running_for_script(script_path, *, ignore_markers=()):
    try:
        proc = subprocess.run(
            ["pgrep", "-af", str(script_path.name)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    current_pid = str(os.getpid())
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if parts and parts[0] == current_pid:
            continue
        command = parts[1] if len(parts) > 1 else line
        if str(script_path) not in command and str(script_path.name) not in command:
            continue
        if any(marker and marker in command for marker in ignore_markers):
            continue
        return True
    return False


def live_worker_in_progress():
    return bool(series_worker_in_progress() or source_probe_in_progress())


def series_worker_in_progress():
    return process_running_for_script(Path(__file__), ignore_markers=("--status-only",))


def source_probe_in_progress():
    return process_running_for_script(SLSKD_SOURCE_PROBE_SCRIPT)


def status_payload(args, sync_result, reconcile, processed, fatal_error=None, **progress):
    raw_source = progress.get("current_source")
    if "progress_note" in progress:
        progress["progress_note"] = source_progress_note(raw_source, progress.get("progress_note"), args)
    task = active_task_payload(args, {
        "current_source": raw_source,
        "current_series": progress.get("current_series"),
        "progress_note": progress.get("progress_note"),
    })
    if task:
        progress["active_task"] = task
        progress["active_task_detail"] = task.get("detail") or ""
    if "current_source" in progress:
        progress["current_source"] = public_source_name(raw_source)
    enabled_sources = dict(getattr(args, "provider_source_enabled", PROVIDER_SOURCE_ENABLED))
    configured_sources = [
        source for source in list(getattr(args, "source_order", SOURCE_ORDER))
        if enabled_sources.get(source, True)
    ]
    progress.setdefault("source_configuration_missing", not bool(configured_sources))
    progress.setdefault("operator_paused", bool(missing_recovery_control().get("paused", False)))
    progress.setdefault("acquisition_worker_available", not bool(fatal_error))
    return {
        "dry_run": bool(args.dry_run),
        "annotate_only": bool(getattr(args, "annotate_only", False)),
        "status_only": bool(getattr(args, "status_only", False)),
        "sync_result": sync_result,
        "reconcile": reconcile,
        "processed": processed,
        "processed_count": len(processed),
        "fatal_error": fatal_error,
        "policy": AUTOPILOT_POLICY,
        "source_order": list(getattr(args, "source_order", SOURCE_ORDER)),
        "source_order_unfiltered": list(getattr(args, "source_order_unfiltered", getattr(args, "source_order", SOURCE_ORDER))),
        "source_order_settings_source": getattr(args, "source_order_settings_source", "fallback"),
        "provider_source_enabled": dict(getattr(args, "provider_source_enabled", PROVIDER_SOURCE_ENABLED)),
        "provider_source_disabled_reasons": dict(getattr(args, "provider_source_disabled_reasons", PROVIDER_SOURCE_DISABLED_REASONS)),
        "prowlarr_provider": {
            "enabled": bool(getattr(args, "prowlarr_provider_enabled", not getattr(args, "skip_prowlarr", False))),
            "settings_source": getattr(args, "prowlarr_provider_settings_source", "fallback"),
            "limit": getattr(args, "prowlarr_limit", None),
            "max_queries_per_issue": getattr(args, "prowlarr_max_queries_per_issue", None),
            "timeout_seconds": getattr(args, "prowlarr_timeout_seconds", None),
            "command_timeout_seconds": getattr(args, "prowlarr_command_timeout_seconds", None),
            "search_budget_seconds": getattr(args, "prowlarr_search_budget_seconds", None),
            "failed_retry_command_timeout_seconds": getattr(args, "failed_retry_command_timeout_seconds", None),
            "provider_timeout_window_seconds": getattr(args, "prowlarr_provider_timeout_window_seconds", None),
            "provider_timeout_threshold": getattr(args, "prowlarr_provider_timeout_threshold", None),
            "provider_timeout_cooldown_seconds": getattr(args, "prowlarr_provider_timeout_cooldown_seconds", None),
            "provider_fetch_failure_window_seconds": getattr(args, "prowlarr_provider_fetch_failure_window_seconds", None),
            "provider_fetch_failure_threshold": getattr(args, "prowlarr_provider_fetch_failure_threshold", None),
            "provider_fetch_failure_cooldown_seconds": getattr(args, "prowlarr_provider_fetch_failure_cooldown_seconds", None),
        },
        "slskd_provider": {
            "enabled": bool(getattr(args, "slskd_provider_enabled", not getattr(args, "skip_slskd", False))),
            "settings_source": getattr(args, "slskd_provider_settings_source", "fallback"),
            "max_total": getattr(args, "slskd_max_total", None),
            "max_per_series": getattr(args, "slskd_max_per_series", None),
            "wait_seconds": getattr(args, "slskd_wait_seconds", None),
            "max_queries": getattr(args, "slskd_max_queries", None),
            "auto_grab_max": getattr(args, "slskd_auto_grab_max", None),
            "probe_budget_seconds": getattr(args, "slskd_probe_budget_seconds", None),
            "cooldown_hours": getattr(args, "slskd_cooldown_hours", None),
        },
        "rss_provider": {
            "enabled": bool(getattr(args, "rss_provider_enabled", not getattr(args, "skip_rss", False))),
            "settings_source": getattr(args, "rss_provider_settings_source", "fallback"),
            "limit": getattr(args, "rss_discovery_limit", None),
            "max_auto": getattr(args, "rss_discovery_max_auto", None),
            "max_per_series": getattr(args, "rss_discovery_max_per_series", None),
            "command_timeout_seconds": getattr(args, "rss_command_timeout_seconds", None),
            "source_worker_http_timeout_seconds": getattr(args, "rss_source_worker_http_timeout_seconds", None),
            "source_allowed_hosts": list(getattr(args, "rss_source_worker_allowed_hosts", []) or []),
            "direct_allowed_hosts": list(getattr(args, "rss_source_worker_direct_allowed_hosts", []) or []),
            "provider_timeout_window_seconds": getattr(args, "rss_provider_timeout_window_seconds", None),
            "provider_timeout_threshold": getattr(args, "rss_provider_timeout_threshold", None),
            "provider_timeout_cooldown_seconds": getattr(args, "rss_provider_timeout_cooldown_seconds", None),
            "provider_fetch_failure_window_seconds": getattr(args, "rss_provider_fetch_failure_window_seconds", None),
            "provider_fetch_failure_threshold": getattr(args, "rss_provider_fetch_failure_threshold", None),
            "provider_fetch_failure_cooldown_seconds": getattr(args, "rss_provider_fetch_failure_cooldown_seconds", None),
        },
        "comicscodes_provider": {
            "enabled": bool(getattr(args, "comicscodes_provider_enabled", not getattr(args, "skip_comicscodes", False))),
            "settings_source": getattr(args, "comicscodes_provider_settings_source", "fallback"),
            "limit": getattr(args, "comicscodes_discovery_limit", None),
            "max_auto": getattr(args, "comicscodes_discovery_max_auto", None),
            "max_per_series": getattr(args, "comicscodes_discovery_max_per_series", None),
            "command_timeout_seconds": getattr(args, "comicscodes_command_timeout_seconds", None),
        },
        "mangadex_provider": {
            "enabled": bool(getattr(args, "mangadex_provider_enabled", not getattr(args, "skip_mangadex", False))),
            "settings_source": getattr(args, "mangadex_provider_settings_source", "fallback"),
            "data_saver": bool(getattr(args, "mangadex_data_saver", False)),
            "max_total": getattr(args, "mangadex_max_total", None),
            "max_per_series": getattr(args, "mangadex_max_per_series", None),
            "command_timeout_seconds": getattr(args, "mangadex_command_timeout_seconds", None),
            "verify_timeout_seconds": getattr(args, "mangadex_verify_timeout_seconds", None),
        },
        **progress,
    }


def status_only_busy_response(args):
    previous_status = read_json(STATUS_FILE, {}) or {}
    if not isinstance(previous_status, dict):
        previous_status = {}
    now = time.time()
    previous_summary = previous_status.get("summary") if isinstance(previous_status.get("summary"), dict) else {}
    previous_generated_at = numeric_timestamp(previous_status.get("generated_at"))
    heartbeat_age_seconds = max(0, now - previous_generated_at) if previous_generated_at else None
    heartbeat_stale = bool(heartbeat_age_seconds is not None and heartbeat_age_seconds >= 180)
    progress_note = previous_status.get("progress_note") or "worker running; status refresh deferred"
    if heartbeat_stale:
        progress_note = f"worker heartbeat stale; last progress {int(heartbeat_age_seconds)}s ago"
    progress_note = source_progress_note(previous_status.get("current_source"), progress_note, args)
    active_task = previous_status.get("active_task") if isinstance(previous_status.get("active_task"), dict) else None
    if not active_task:
        active_task = active_task_payload(args, {
            "current_source": previous_status.get("current_source"),
            "current_series": previous_status.get("current_series"),
            "progress_note": progress_note,
        })
    active_task_detail = str(
        previous_status.get("active_task_detail")
        or ((active_task or {}).get("detail") if isinstance(active_task, dict) else "")
        or ""
    )
    queue_active = bool(
        previous_summary.get("queued")
        or previous_summary.get("searching")
        or previous_summary.get("downloading")
        or previous_summary.get("importing")
        or previous_status.get("queue_active")
    )
    sync_result = previous_status.get("sync_result") if isinstance(previous_status.get("sync_result"), dict) else {}
    sync_result = dict(sync_result)
    try:
        watch_data = read_json(COMIC_SERIES_FILE, {"watches": []}) or {"watches": []}
        _, current_source_summary = current_missing_from_active_sources(watch_data.get("watches", []))
        sync_result["current_sources"] = current_source_summary
    except Exception as exc:
        sync_result["current_sources_error"] = f"{type(exc).__name__}: {exc}"
    status = dict(previous_status)
    status.update(
        {
            "ok": not previous_status.get("fatal_error"),
            "generated_at": now,
            "generated_at_iso": now_iso(now),
            "state": "running",
            "queue_active": queue_active,
            "summary": previous_summary,
            "sync_result": sync_result,
            "policy": AUTOPILOT_POLICY,
            "dry_run": bool(getattr(args, "dry_run", False)),
            "annotate_only": bool(getattr(args, "annotate_only", False)),
            "status_only": True,
            "in_progress": True,
            "current_series": previous_status.get("current_series"),
            "current_source": public_source_name(previous_status.get("current_source")),
            "progress_note": progress_note,
            "active_task": active_task or {},
            "active_task_detail": active_task_detail,
            "last_processed_series": previous_status.get("last_processed_series"),
            "status_refreshed_at": now,
            "status_refreshed_at_iso": now_iso(now),
            "status_refresh_persisted": False,
            "status_refresh_deferred": True,
            "status_refresh_deferred_reason": "autopilot_worker_running",
            "worker_heartbeat_age_seconds": heartbeat_age_seconds,
            "worker_heartbeat_stale": heartbeat_stale,
            "source_order": list(getattr(args, "source_order", SOURCE_ORDER)),
            "source_order_unfiltered": list(getattr(args, "source_order_unfiltered", getattr(args, "source_order", SOURCE_ORDER))),
            "source_order_settings_source": getattr(args, "source_order_settings_source", "fallback"),
            "provider_source_enabled": dict(getattr(args, "provider_source_enabled", PROVIDER_SOURCE_ENABLED)),
            "provider_source_disabled_reasons": dict(getattr(args, "provider_source_disabled_reasons", PROVIDER_SOURCE_DISABLED_REASONS)),
            "prowlarr_provider": {
                "enabled": bool(getattr(args, "prowlarr_provider_enabled", not getattr(args, "skip_prowlarr", False))),
                "settings_source": getattr(args, "prowlarr_provider_settings_source", "fallback"),
                "limit": getattr(args, "prowlarr_limit", None),
                "max_queries_per_issue": getattr(args, "prowlarr_max_queries_per_issue", None),
                "timeout_seconds": getattr(args, "prowlarr_timeout_seconds", None),
                "command_timeout_seconds": getattr(args, "prowlarr_command_timeout_seconds", None),
                "search_budget_seconds": getattr(args, "prowlarr_search_budget_seconds", None),
                "failed_retry_command_timeout_seconds": getattr(args, "failed_retry_command_timeout_seconds", None),
                "provider_timeout_window_seconds": getattr(args, "prowlarr_provider_timeout_window_seconds", None),
                "provider_timeout_threshold": getattr(args, "prowlarr_provider_timeout_threshold", None),
                "provider_timeout_cooldown_seconds": getattr(args, "prowlarr_provider_timeout_cooldown_seconds", None),
                "provider_fetch_failure_window_seconds": getattr(args, "prowlarr_provider_fetch_failure_window_seconds", None),
                "provider_fetch_failure_threshold": getattr(args, "prowlarr_provider_fetch_failure_threshold", None),
                "provider_fetch_failure_cooldown_seconds": getattr(args, "prowlarr_provider_fetch_failure_cooldown_seconds", None),
            },
            "slskd_provider": {
                "enabled": bool(getattr(args, "slskd_provider_enabled", not getattr(args, "skip_slskd", False))),
                "settings_source": getattr(args, "slskd_provider_settings_source", "fallback"),
                "max_total": getattr(args, "slskd_max_total", None),
                "max_per_series": getattr(args, "slskd_max_per_series", None),
                "wait_seconds": getattr(args, "slskd_wait_seconds", None),
                "max_queries": getattr(args, "slskd_max_queries", None),
                "auto_grab_max": getattr(args, "slskd_auto_grab_max", None),
                "probe_budget_seconds": getattr(args, "slskd_probe_budget_seconds", None),
                "cooldown_hours": getattr(args, "slskd_cooldown_hours", None),
            },
            "rss_provider": {
                "enabled": bool(getattr(args, "rss_provider_enabled", not getattr(args, "skip_rss", False))),
                "settings_source": getattr(args, "rss_provider_settings_source", "fallback"),
                "limit": getattr(args, "rss_discovery_limit", None),
                "max_auto": getattr(args, "rss_discovery_max_auto", None),
                "max_per_series": getattr(args, "rss_discovery_max_per_series", None),
                "command_timeout_seconds": getattr(args, "rss_command_timeout_seconds", None),
                "source_worker_http_timeout_seconds": getattr(args, "rss_source_worker_http_timeout_seconds", None),
                "source_allowed_hosts": list(getattr(args, "rss_source_worker_allowed_hosts", []) or []),
                "direct_allowed_hosts": list(getattr(args, "rss_source_worker_direct_allowed_hosts", []) or []),
                "provider_timeout_window_seconds": getattr(args, "rss_provider_timeout_window_seconds", None),
                "provider_timeout_threshold": getattr(args, "rss_provider_timeout_threshold", None),
                "provider_timeout_cooldown_seconds": getattr(args, "rss_provider_timeout_cooldown_seconds", None),
                "provider_fetch_failure_window_seconds": getattr(args, "rss_provider_fetch_failure_window_seconds", None),
                "provider_fetch_failure_threshold": getattr(args, "rss_provider_fetch_failure_threshold", None),
                "provider_fetch_failure_cooldown_seconds": getattr(args, "rss_provider_fetch_failure_cooldown_seconds", None),
            },
            "comicscodes_provider": {
                "enabled": bool(getattr(args, "comicscodes_provider_enabled", not getattr(args, "skip_comicscodes", False))),
                "settings_source": getattr(args, "comicscodes_provider_settings_source", "fallback"),
                "limit": getattr(args, "comicscodes_discovery_limit", None),
                "max_auto": getattr(args, "comicscodes_discovery_max_auto", None),
                "max_per_series": getattr(args, "comicscodes_discovery_max_per_series", None),
                "command_timeout_seconds": getattr(args, "comicscodes_command_timeout_seconds", None),
            },
            "mangadex_provider": {
                "enabled": bool(getattr(args, "mangadex_provider_enabled", not getattr(args, "skip_mangadex", False))),
                "settings_source": getattr(args, "mangadex_provider_settings_source", "fallback"),
                "data_saver": bool(getattr(args, "mangadex_data_saver", False)),
                "max_total": getattr(args, "mangadex_max_total", None),
                "max_per_series": getattr(args, "mangadex_max_per_series", None),
                "command_timeout_seconds": getattr(args, "mangadex_command_timeout_seconds", None),
                "verify_timeout_seconds": getattr(args, "mangadex_verify_timeout_seconds", None),
            },
        }
    )
    print(json.dumps(status, indent=2, sort_keys=True))
    return status


def run(args):
    setup_started_monotonic = time.monotonic()
    startup_phase_seconds = {}

    def startup_phase(name, callback):
        phase_started = time.monotonic()
        result = callback()
        elapsed = time.monotonic() - phase_started
        startup_phase_seconds[name] = round(float(startup_phase_seconds.get(name) or 0) + elapsed, 3)
        return result

    run_deadline = runtime_deadline(args)
    if getattr(args, "status_only", False) and series_worker_in_progress():
        return status_only_busy_response(args)
    hard_exit_alarm = install_runtime_hard_exit(args)
    previous_status = read_json(STATUS_FILE, {}) or {}
    if not isinstance(previous_status, dict):
        previous_status = {}
    early_import_backlog_gate = import_backlog_priority_gate()
    if (
        early_import_backlog_gate.get("active")
        and not getattr(args, "dry_run", False)
        and not getattr(args, "status_only", False)
        and not getattr(args, "annotate_only", False)
    ):
        status = write_import_backlog_priority_deferred_status(
            args,
            early_import_backlog_gate,
            previous_status,
            gate_stage="pre_queue_load",
        )
        clear_runtime_hard_exit(hard_exit_alarm)
        return status

    startup_phase_seconds["pre_queue_setup"] = round(time.monotonic() - setup_started_monotonic, 3)
    queue = startup_phase("load_queue", load_queue)
    retired_on_load = int(queue.pop("_retired_from_inkdrop_state_count", 0) or 0)
    health_cleared_on_load = int(queue.pop("_provider_health_blocked_sources_cleared_count", 0) or 0)
    stale_source_started_on_load = int(queue.pop("_stale_source_started_normalized_count", 0) or 0)
    disabled_cleared_on_load = clear_disabled_current_sources(queue)
    if (
        retired_on_load
        or health_cleared_on_load
        or stale_source_started_on_load
        or disabled_cleared_on_load
    ) and not getattr(args, "dry_run", False) and not getattr(args, "status_only", False):
        # The queue JSON is the Automatic Search work source. Persist repaired
        # rows now, but do not run a second synchronous queue reconciliation
        # before provider selection. Under DB contention that maintenance pass
        # can consume the provider budget even though the exact rows are
        # already safe to retry. The normal bounded startup/final sync projects
        # this saved state.
        persist_startup_queue_normalization(
            queue,
            stale_source_started_count=stale_source_started_on_load,
        )
    if getattr(args, "status_only", False):
        previous_processed = previous_status.get("processed") if isinstance(previous_status.get("processed"), list) else []
        worker_running = series_worker_in_progress()
        source_probe_running = source_probe_in_progress()
        previous_fatal_error = previous_status.get("fatal_error") if worker_running else None
        sync_result = {"status_only_fast_path": True}
        reconcile = {"status_only_fast_path": True}
        payload = status_payload(
            args,
            sync_result,
            reconcile,
            previous_processed,
            fatal_error=previous_fatal_error,
            in_progress=worker_running,
            current_series=previous_status.get("current_series") if worker_running else None,
            current_source=previous_status.get("current_source") if worker_running else None,
            progress_note=(
                previous_status.get("progress_note")
                if worker_running and previous_status.get("progress_note")
                else "status refreshed" if worker_running else "waiting for next scheduled worker"
            ),
            last_processed_series=previous_status.get("last_processed_series"),
            status_refreshed_at=time.time(),
            status_refreshed_at_iso=now_iso(),
            status_refresh_fast_path=True,
            auxiliary_worker_running=source_probe_running,
            slskd_source_probe_running=source_probe_running,
        )
        if worker_running:
            status = build_status(queue, payload)
            status["status_refresh_persisted"] = False
        else:
            status = write_status(queue, payload)
            status["status_refresh_persisted"] = True
        print(json.dumps(status, indent=2, sort_keys=True))
        return status
    import_backlog_gate = import_backlog_priority_gate(queue)
    if (
        import_backlog_gate.get("active")
        and not getattr(args, "dry_run", False)
        and not getattr(args, "annotate_only", False)
    ):
        sync_result = {
            "ok": True,
            "deferred": True,
            "reason": "ready_import_backlog_priority",
            "import_backlog_gate": import_backlog_gate,
        }
        reconcile = {
            "skipped": True,
            "reason": "ready_import_backlog_priority",
            "import_backlog_gate": import_backlog_gate,
        }
        note = f"deferred; {import_backlog_gate.get('ready_import_count')} ready imports waiting for import worker"
        status = write_status(
            queue,
            status_payload(
                args,
                sync_result,
                reconcile,
                [],
                fatal_error=None,
                in_progress=False,
                current_series=None,
                current_source="import",
                progress_note=note,
                last_processed_series=previous_status.get("last_processed_series") if isinstance(previous_status, dict) else None,
                import_backlog_priority=True,
                import_backlog_gate=import_backlog_gate,
                deferred_reason="ready_import_backlog_priority",
            ),
        )
        print(json.dumps(status, indent=2, sort_keys=True))
        clear_runtime_hard_exit(hard_exit_alarm)
        return status
    if not getattr(args, "dry_run", False) and not getattr(args, "status_only", False):
        startup_phase("status_publication", lambda: write_startup_heartbeat(queue, args))
        startup_phase(
            "status_publication",
            lambda: write_startup_progress(queue, args, "syncing InkDrop queue state", source="sync"),
        )
    sync_requested = bool(args.sync)
    metadata_sync_requested = bool(getattr(args, "sync_metadata_adapter", False))
    sync_result = {}
    if sync_requested:
        startup_sync_timeout = startup_maintenance_timeout(
            args,
            getattr(args, "sync_timeout_seconds", None) or DEFAULT_STARTUP_SYNC_TIMEOUT_SECONDS,
            setup_started_monotonic,
            share=0.6,
        )
        if startup_sync_timeout > 0:
            stage_result = startup_phase(
                "inkdrop_state_sync",
                lambda: sync_watched_state(sync=True, sync_timeout_seconds=startup_sync_timeout),
            )
            if isinstance(stage_result, dict):
                sync_result.update(stage_result)
        else:
            sync_result["startup_sync_deferred"] = "provider_budget_boundary_reached"
    if metadata_sync_requested:
        metadata_sync_timeout = startup_maintenance_timeout(
            args,
            getattr(args, "sync_metadata_adapter_timeout_seconds", None) or DEFAULT_METADATA_ADAPTER_SYNC_TIMEOUT_SECONDS,
            setup_started_monotonic,
            share=0.3,
        )
        if metadata_sync_timeout > 0:
            stage_result = startup_phase(
                "metadata_adapter_sync",
                lambda: sync_watched_state(
                    sync_metadata_adapter=True,
                    sync_metadata_adapter_timeout_seconds=metadata_sync_timeout,
                ),
            )
            if isinstance(stage_result, dict):
                sync_result.update(stage_result)
        else:
            sync_result["metadata_sync_deferred"] = "provider_budget_boundary_reached"
    watches, _ = startup_phase("load_watches", lambda: load_watches(sync=False))
    if not getattr(args, "dry_run", False) and not getattr(args, "status_only", False):
        startup_phase(
            "status_publication",
            lambda: write_startup_progress(queue, args, "building active queue from InkDrop state", source="queue"),
        )
    current, current_source_summary = startup_phase(
        "load_active_missing",
        lambda: current_missing_from_active_sources(watches.get("watches", [])),
    )
    sync_result["current_sources"] = current_source_summary
    if not getattr(args, "dry_run", False) and not getattr(args, "status_only", False):
        startup_phase(
            "status_publication",
            lambda: write_startup_progress(queue, args, "merging active queue rows", source="queue"),
        )
    reconcile = startup_phase("merge_active_queue", lambda: merge_current_queue(queue, current))
    terminal_after_merge = retire_queue_items_from_inkdrop_state(queue)
    if terminal_after_merge and not getattr(args, "dry_run", False):
        save_startup_queue_snapshot(queue)
    source_worker_yield = source_worker_pressure_yield(args)
    if not getattr(args, "dry_run", False) and not getattr(args, "status_only", False) and not source_worker_yield:
        startup_phase(
            "status_publication",
            lambda: write_startup_progress(queue, args, "annotating queue evidence", source="queue"),
        )
    if source_worker_yield:
        annotate_result = {
            "ok": False,
            "reason": "source_worker_pressure_yield",
            "skipped": "source_worker_lock_busy",
            "processed": 0,
            "total": len((queue or {}).get("items") or {}),
        }
    else:
        annotate_result = startup_phase(
            "annotate_queue_evidence",
            lambda: provider_targeted_annotation_deferred(queue, args, "startup"),
        )
    if isinstance(sync_result, dict):
        sync_result["annotate"] = annotate_result
        sync_result.update(startup_timing_summary(setup_started_monotonic, startup_phase_seconds))
        protected_budget = provider_protected_budget_seconds(args)
        sync_result["provider_protected_budget_seconds"] = round(protected_budget, 3) if protected_budget else None
        if source_worker_yield:
            sync_result["source_worker_pressure_yield"] = source_worker_yield
    processed = []
    processed_series_queue_syncs_deferred = 0
    fatal_error = None
    last_processed_series = None
    provider_call_states = {}

    def provider_observer(observation):
        return record_provider_observation(
            sync_result,
            args,
            setup_started_monotonic,
            observation,
            call_states=provider_call_states,
        )

    def refresh_queue_from_watches():
        nonlocal watches, current, current_source_summary, reconcile, sync_result
        watches, _ = load_watches(sync=False)
        current, current_source_summary = current_missing_from_active_sources(watches.get("watches", []))
        sync_result["current_sources"] = current_source_summary
        reconcile = merge_current_queue(queue, current)
        retire_queue_items_from_inkdrop_state(queue)
        annotate_refresh_result = provider_targeted_annotation_deferred(queue, args, "watch_refresh")
        if isinstance(sync_result, dict):
            sync_result["annotate"] = annotate_refresh_result
        return reconcile

    def publish_progress(series=None, source=None, note=None, fatal=None):
        save_queue_progress_snapshot(queue)
        return write_progress_status(
            queue,
            status_payload(
                args,
                sync_result,
                reconcile,
                processed,
                fatal_error=fatal,
                in_progress=True,
                current_series=series,
                current_source=source,
                progress_note=note,
                last_processed_series=last_processed_series,
            ),
        )

    publish_progress(note="queue synced")
    if source_worker_yield:
        publish_progress(source="source_worker", note="dedicated source-worker pass is waiting; yielding autopilot source work")
    if not getattr(args, "annotate_only", False) and not source_worker_yield:
        try:
            hot_retries_finished = False
            processed_groups = set()
            previous_broad_skip = getattr(args, "skip_slskd_broad_due_to_busy", False)
            args.skip_slskd_broad_due_to_busy = False
            broad_groups_processed = 0
            try:
                while len(processed) < int(args.max_series or 0):
                    if runtime_deadline_expired(run_deadline):
                        publish_progress(note="autopilot runtime budget reached; finishing this pass")
                        break
                    group_start_min_seconds = run_group_start_min_seconds()
                    if runtime_deadline_too_close(run_deadline, group_start_min_seconds):
                        remaining = runtime_seconds_remaining(run_deadline) or 0
                        publish_progress(
                            note=(
                                "autopilot runtime budget is too close to start another series; "
                                f"{duration_label(remaining) or '0s'} left"
                            )
                        )
                        break
                    if broad_groups_processed:
                        refresh_queue_from_watches()
                        publish_progress(note="queue refreshed")
                    next_group = None
                    for series, rows in due_series(queue, args):
                        if not rows:
                            continue
                        group_key = due_group_key(rows[0])
                        if group_key in processed_groups:
                            continue
                        next_group = (series, rows, group_key)
                        break
                    if not next_group:
                        if not hot_retries_finished:
                            hot_result = process_deferred_hot_retries(
                                queue,
                                args,
                                provider_work_started=bool(sync_result.get("provider_work_started")),
                                broad_work_available=False,
                                progress=publish_progress,
                                deadline=run_deadline,
                                provider_observer=provider_observer,
                            )
                            hot_processed = hot_result.get("processed") or []
                            hot_retries_finished = not hot_result.get("deferred")
                            if hot_processed:
                                processed.extend(hot_processed)
                                processed_groups.update(
                                    (
                                        row.get("series"),
                                        row.get("queue_identity")
                                        or f"title:{normalize(row.get('series') or '')}",
                                    )
                                    for row in hot_processed
                                    if row.get("hot_retry")
                                )
                                publish_progress(note="cached SLSKD retries finished after broad queue")
                        break
                    series, rows, group_key = next_group
                    publish_progress(series=series, source="queue", note=f"starting {series}")
                    processed.append(
                        process_series(
                            queue,
                            series,
                            rows[: args.max_issues_per_series],
                            args,
                            progress=publish_progress,
                            deadline=run_deadline,
                            provider_observer=provider_observer,
                        )
                    )
                    processed_groups.add(group_key)
                    broad_groups_processed += 1
                    save_queue(queue, sync_state=False, sync_reason="series_finished_json_save")
                    processed_series_queue_syncs_deferred += 1
                    last_processed_series = series
                    publish_progress(note=f"finished {series}")
                    if not hot_retries_finished and sync_result.get("provider_work_started"):
                        hot_result = process_deferred_hot_retries(
                            queue,
                            args,
                            provider_work_started=True,
                            broad_work_available=True,
                            progress=publish_progress,
                            deadline=run_deadline,
                            provider_observer=provider_observer,
                        )
                        hot_processed = hot_result.get("processed") or []
                        hot_retries_finished = not hot_result.get("deferred")
                        if hot_processed:
                            processed.extend(hot_processed)
                            processed_groups.update(
                                (
                                    row.get("series"),
                                    row.get("queue_identity")
                                    or f"title:{normalize(row.get('series') or '')}",
                                )
                                for row in hot_processed
                                if row.get("hot_retry")
                            )
                            hot_retry_state = broad_queue_state_after_hot_retries(hot_processed)
                            if "slskd" in hot_retry_state.get("skip_sources", set()):
                                args.skip_slskd_broad_due_to_busy = True
                            publish_progress(note="cached SLSKD retries finished after provider work")
                    if runtime_deadline_expired(run_deadline):
                        publish_progress(note="autopilot runtime budget reached after series; finishing this pass")
                        break
            finally:
                args.skip_slskd_broad_due_to_busy = previous_broad_skip
        except Exception as exc:
            fatal_error = f"{type(exc).__name__}: {exc}"
            log("fatal_error", error=fatal_error)
            publish_progress(fatal=fatal_error, note="autopilot stopped with an error")
        if runtime_deadline_expired(run_deadline):
            annotate_finish_result = {
                "ok": False,
                "reason": "run_finish_skipped_runtime_budget",
                "skipped": "runtime_budget_reached",
                "max_seconds": 0,
                "processed": 0,
                "total": len((queue or {}).get("items") or {}),
            }
            log("annotate_states_skipped", **annotate_finish_result)
        else:
            annotate_finish_result = annotate_states(
                queue,
                max_seconds=getattr(args, "annotate_timeout_seconds", DEFAULT_ANNOTATE_TIMEOUT_SECONDS),
                reason="run_finish",
                row_keys=startup_annotation_row_keys(queue, args),
            )
        if isinstance(sync_result, dict):
            sync_result["annotate"] = annotate_finish_result
    final_save_sync = save_queue(queue, sync_state=False, sync_reason="run_finish_json_save") or {
        "ok": True,
        "skipped": "sync_state_false",
        "reason": "run_finish_json_save",
    }
    runtime_budget_reached = runtime_deadline_expired(run_deadline)
    if runtime_budget_reached:
        final_sync = {
            "ok": False,
            "reason": "runtime_budget_reached_deferred",
            "error": "autopilot runtime budget reached before final InkDrop state sync; scheduled maintenance will reconcile it",
            "locked": False,
            "deferred": True,
            "budget_reached": True,
        }
    elif source_worker_yield:
        final_sync = {
            "ok": False,
            "reason": "source_worker_pressure_deferred",
            "error": "dedicated source-worker pass is waiting; final InkDrop state sync deferred so autopilot releases its lock",
            "locked": False,
            "deferred": True,
        }
    else:
        final_sync = sync_inkdrop_queue_state(
            force=True,
            reason="run_finished",
            timeout_seconds=INKDROP_STATE_FINAL_SYNC_TIMEOUT_SECONDS,
            busy_timeout_ms=INKDROP_STATE_FINAL_SYNC_BUSY_TIMEOUT_MS,
            lock_attempts=INKDROP_STATE_FINAL_SYNC_LOCK_ATTEMPTS,
            lock_initial_delay=INKDROP_STATE_FINAL_SYNC_INITIAL_DELAY,
        )
    if isinstance(sync_result, dict):
        sync_result["final_queue_sync"] = {
            "save_queue": final_save_sync,
            "run_finished": final_sync,
        }
        sync_result["processed_series_queue_syncs_deferred"] = processed_series_queue_syncs_deferred
        if run_deadline:
            sync_result["max_run_seconds"] = int(getattr(args, "max_run_seconds", 0) or 0)
            sync_result["runtime_budget_reached"] = runtime_budget_reached
        if source_worker_yield:
            sync_result["source_worker_pressure_yield"] = source_worker_yield
    if isinstance(final_sync, dict) and final_sync.get("ok") is False:
        mark_inkdrop_state_sync_pending(queue, final_sync, "run_finished")
        write_json(QUEUE_FILE, queue)
    elif clear_inkdrop_state_sync_pending(queue):
        write_json(QUEUE_FILE, queue)
    status = write_status(
        queue,
        status_payload(
            args,
            sync_result,
            reconcile,
            processed,
            fatal_error=fatal_error,
            in_progress=False,
            current_series=None,
            current_source=None,
            progress_note="yielded to source worker" if source_worker_yield and not fatal_error else "finished" if not fatal_error else "stopped with an error",
            last_processed_series=last_processed_series,
        ),
    )
    if runtime_budget_reached:
        print(json.dumps({
            "ok": status.get("ok"),
            "state": status.get("state"),
            "generated_at_iso": status.get("generated_at_iso"),
            "processed_count": status.get("processed_count"),
            "progress_note": status.get("progress_note"),
            "sync_result": status.get("sync_result"),
        }, sort_keys=True))
    else:
        print(json.dumps(status, indent=2, sort_keys=True))
    clear_runtime_hard_exit(hard_exit_alarm)
    return status


def run_provider_then_companion_maintenance(args, provider_runner=None, maintenance_runner=None):
    provider_result = (provider_runner or run)(args)
    if args.dry_run or args.status_only or args.annotate_only or args.skip_mangadex:
        return provider_result
    try:
        if maintenance_runner is None:
            import inkdrop_web

            maintenance_runner = inkdrop_web.run_post_provider_manga_companion_maintenance
        maintenance_runner(reconcile_limit=1, refresh_limit=1)
    except Exception as exc:
        print(json.dumps({"warning": "post-provider manga companion maintenance failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
    return provider_result


def main():
    parser = argparse.ArgumentParser(description="InkDrop watched-series autopilot queue")
    parser.add_argument("--series", action="append", default=[], help="limit run to exact series title")
    parser.add_argument("--sync", action="store_true", help="sync InkDrop Core state before reading the watched-series queue")
    parser.add_argument("--sync-metadata-adapter", action="store_true", help="also sync the temporary Kapowarr metadata adapter")
    parser.add_argument("--sync-timeout-seconds", type=int, default=None, help="bounded startup InkDrop Core sync timeout")
    parser.add_argument("--sync-metadata-adapter-timeout-seconds", type=int, default=None, help="bounded startup metadata-adapter sync timeout")
    parser.add_argument("--annotate-timeout-seconds", type=int, default=None, help="bounded queue annotation budget before source processing")
    parser.add_argument("--max-run-seconds", type=int, default=0, help="stop starting new source work after this many seconds and finish the pass cleanly")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--annotate-only", action="store_true", help="refresh queue state from watch/download/import evidence without starting source searches")
    parser.add_argument("--status-only", action="store_true", help="refresh status summary from queue/import/SLSKD evidence without writing the queue or starting sources")
    parser.add_argument("--no-yield-to-source-worker", action="store_true", help="do not shorten this autopilot pass when a dedicated source-worker pass is already waiting")
    parser.add_argument("--force", action="store_true", help="ignore queue retry timers")
    parser.add_argument("--retry-needs-you", action="store_true", help="retry rows already marked Needs You")
    parser.add_argument("--max-series", type=int, default=6)
    parser.add_argument(
        "--missing-recovery-enabled",
        action=argparse.BooleanOptionalAction,
        default=missing_recovery_runtime_enabled(),
        help="opt in to bounded pass capacity across missing-library recovery cohorts",
    )
    parser.add_argument(
        "--missing-recovery-max-per-cohort",
        type=int,
        default=2,
        help="preferred per-cohort ceiling before balanced refill of remaining pass capacity",
    )
    parser.add_argument("--max-issues-per-series", type=int, default=12)
    parser.add_argument("--missing-max-per-series", type=int, default=6)
    parser.add_argument("--missing-max-total", type=int, default=20)
    parser.add_argument("--skip-prowlarr", action="store_true")
    parser.add_argument("--prowlarr-limit", type=int, default=None)
    parser.add_argument("--prowlarr-max-queries-per-issue", type=int, default=None)
    parser.add_argument("--prowlarr-timeout-seconds", type=float, default=None)
    parser.add_argument("--prowlarr-command-timeout-seconds", type=int, default=None)
    parser.add_argument("--prowlarr-search-budget-seconds", type=int, default=None)
    parser.add_argument("--prowlarr-provider-timeout-window-seconds", type=int, default=None)
    parser.add_argument("--prowlarr-provider-timeout-threshold", type=int, default=None)
    parser.add_argument("--prowlarr-provider-timeout-cooldown-seconds", type=int, default=None)
    parser.add_argument("--prowlarr-provider-fetch-failure-window-seconds", type=int, default=None)
    parser.add_argument("--prowlarr-provider-fetch-failure-threshold", type=int, default=None)
    parser.add_argument("--prowlarr-provider-fetch-failure-cooldown-seconds", type=int, default=None)
    parser.add_argument("--source-lock-wait-seconds", type=int, default=DEFAULT_SLSKD_SOURCE_LOCK_WAIT_SECONDS, help="seconds to wait for a busy source worker before retrying on the next queue pass")
    parser.add_argument("--no-result-cooldown-hours", type=float, default=1)
    parser.add_argument("--skip-failed-retry", action="store_true")
    parser.add_argument("--failed-retry-max-total", type=int, default=3)
    parser.add_argument("--failed-retry-limit", type=int, default=50)
    parser.add_argument("--failed-retry-max-attempts", type=int, default=3, help="alternate attempts per failed series/issue before letting the normal ladder continue")
    parser.add_argument("--failed-retry-command-timeout-seconds", type=int, default=None)
    parser.add_argument("--discovery-limit", type=int, default=None, help="override RSS/ComicsCodes provider default_limit")
    parser.add_argument("--discovery-max-auto", type=int, default=None, help="override RSS/ComicsCodes provider max_auto")
    parser.add_argument("--discovery-max-per-series", type=int, default=None, help="override RSS/ComicsCodes provider max_per_series")
    parser.add_argument("--skip-rss", action="store_true")
    parser.add_argument("--rss-command-timeout-seconds", type=int, default=None)
    parser.add_argument("--rss-source-worker-http-timeout-seconds", type=int, default=None)
    parser.add_argument("--rss-provider-timeout-window-seconds", type=int, default=None)
    parser.add_argument("--rss-provider-timeout-threshold", type=int, default=None)
    parser.add_argument("--rss-provider-timeout-cooldown-seconds", type=int, default=None)
    parser.add_argument("--rss-provider-fetch-failure-window-seconds", type=int, default=None)
    parser.add_argument("--rss-provider-fetch-failure-threshold", type=int, default=None)
    parser.add_argument("--rss-provider-fetch-failure-cooldown-seconds", type=int, default=None)
    parser.add_argument("--skip-comicscodes", action="store_true")
    parser.add_argument("--comicscodes-command-timeout-seconds", type=int, default=None)
    parser.add_argument("--include-comicscodes-lists", action="store_true")
    parser.add_argument("--skip-mangadex", action="store_true")
    parser.add_argument("--mangadex-max-total", type=int, default=6)
    parser.add_argument("--mangadex-max-per-series", type=int, default=3)
    parser.add_argument("--mangadex-command-timeout-seconds", type=int, default=None)
    parser.add_argument("--mangadex-verify-timeout-seconds", type=int, default=None)
    parser.add_argument("--mangadex-data-saver", action="store_true")
    parser.add_argument("--skip-slskd", action="store_true")
    parser.add_argument("--force-slskd", action="store_true")
    parser.add_argument("--slskd-max-total", type=int, default=None)
    parser.add_argument("--slskd-max-per-series", type=int, default=None)
    parser.add_argument("--slskd-wait-seconds", type=int, default=None)
    parser.add_argument("--slskd-max-queries", type=int, default=None)
    parser.add_argument("--slskd-cooldown-hours", type=float, default=None)
    parser.add_argument("--slskd-auto-grab-max", type=int, default=None)
    parser.add_argument("--slskd-probe-budget-seconds", type=int, default=None)
    parser.add_argument("--slskd-hot-retry-max", type=int, default=6, help="focused cached SLSKD starts/retries to run before the broad source ladder")
    parser.add_argument("--retry-seconds", type=int, default=30 * 60)
    parser.add_argument("--exhaustion-cycles", type=int, default=6, help="park rows for retry after this many full source-ladder attempts plus SLSKD")
    args = parser.parse_args()
    accepted_handoff_gate = accepted_download_handoff_priority_gate()
    if (
        accepted_handoff_gate.get("active")
        and not getattr(args, "dry_run", False)
        and not getattr(args, "status_only", False)
        and not getattr(args, "annotate_only", False)
    ):
        log(
            "accepted_download_handoff_priority",
            reason=accepted_handoff_gate.get("reason"),
            pending_count=accepted_handoff_gate.get("pending_count"),
            held_seconds=accepted_handoff_gate.get("held_seconds"),
            stall_seconds=accepted_handoff_gate.get("stall_seconds"),
            next_action="source_worker_download_client_handoff",
        )
        return
    preflight_import_backlog_gate = import_backlog_priority_gate()
    if (
        preflight_import_backlog_gate.get("active")
        and not getattr(args, "dry_run", False)
        and not getattr(args, "status_only", False)
        and not getattr(args, "annotate_only", False)
    ):
        previous_status = read_json(STATUS_FILE, {}) or {}
        write_import_backlog_priority_deferred_status(
            args,
            preflight_import_backlog_gate,
            previous_status if isinstance(previous_status, dict) else {},
            gate_stage="pre_provider_settings",
        )
        return
    apply_automation_app_settings(args)
    apply_prowlarr_provider_defaults(args)
    apply_slskd_provider_defaults(args)
    apply_direct_discovery_provider_defaults(args)
    apply_mangadex_provider_defaults(args)
    apply_provider_source_policy(args)

    args.max_series = max(1, min(int(args.max_series or 1), 10))
    args.max_issues_per_series = max(1, min(int(args.max_issues_per_series or 1), 50))
    args.missing_max_per_series = max(1, min(int(args.missing_max_per_series or 1), 25))
    args.missing_max_total = max(1, min(int(args.missing_max_total or 1), 50))
    args.prowlarr_limit = max(1, min(int(args.prowlarr_limit or DEFAULT_PROWLARR_LIMIT), 100))
    args.prowlarr_max_queries_per_issue = max(
        1,
        min(int(args.prowlarr_max_queries_per_issue or DEFAULT_PROWLARR_MAX_QUERIES_PER_ISSUE), 20),
    )
    args.prowlarr_timeout_seconds = max(
        5.0,
        min(float(args.prowlarr_timeout_seconds or DEFAULT_PROWLARR_TIMEOUT_SECONDS), 30.0),
    )
    args.prowlarr_command_timeout_seconds = max(
        15,
        min(int(args.prowlarr_command_timeout_seconds or DEFAULT_PROWLARR_COMMAND_TIMEOUT_SECONDS), 300),
    )
    args.prowlarr_search_budget_seconds = max(
        8,
        min(
            int(args.prowlarr_search_budget_seconds or DEFAULT_PROWLARR_SEARCH_BUDGET_SECONDS),
            max(8, args.prowlarr_command_timeout_seconds - 8),
        ),
    )
    args.prowlarr_provider_timeout_window_seconds = bounded_int_value(
        args.prowlarr_provider_timeout_window_seconds,
        DEFAULT_PROWLARR_PROVIDER_TIMEOUT_WINDOW_SECONDS,
        0,
        24 * 3600,
    )
    args.prowlarr_provider_timeout_threshold = bounded_int_value(
        args.prowlarr_provider_timeout_threshold,
        DEFAULT_PROWLARR_PROVIDER_TIMEOUT_THRESHOLD,
        0,
        100,
    )
    args.prowlarr_provider_timeout_cooldown_seconds = bounded_int_value(
        args.prowlarr_provider_timeout_cooldown_seconds,
        DEFAULT_PROWLARR_PROVIDER_TIMEOUT_COOLDOWN_SECONDS,
        0,
        24 * 3600,
    )
    args.prowlarr_provider_fetch_failure_window_seconds = bounded_int_value(
        args.prowlarr_provider_fetch_failure_window_seconds,
        DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS,
        0,
        24 * 3600,
    )
    args.prowlarr_provider_fetch_failure_threshold = bounded_int_value(
        args.prowlarr_provider_fetch_failure_threshold,
        DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_THRESHOLD,
        0,
        100,
    )
    args.prowlarr_provider_fetch_failure_cooldown_seconds = bounded_int_value(
        args.prowlarr_provider_fetch_failure_cooldown_seconds,
        DEFAULT_PROWLARR_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS,
        0,
        24 * 3600,
    )
    args.source_lock_wait_seconds = max(0, min(int(args.source_lock_wait_seconds or DEFAULT_SLSKD_SOURCE_LOCK_WAIT_SECONDS), 60))
    args.sync_timeout_seconds = max(
        5,
        min(int(args.sync_timeout_seconds or DEFAULT_STARTUP_SYNC_TIMEOUT_SECONDS), 240),
    )
    args.max_run_seconds = max(0, min(int(args.max_run_seconds or 0), 60 * 60))
    args.sync_metadata_adapter_timeout_seconds = max(
        5,
        min(
            int(args.sync_metadata_adapter_timeout_seconds or DEFAULT_METADATA_ADAPTER_SYNC_TIMEOUT_SECONDS),
            240,
        ),
    )
    args.annotate_timeout_seconds = max(
        10,
        min(int(args.annotate_timeout_seconds or DEFAULT_ANNOTATE_TIMEOUT_SECONDS), 300),
    )
    args.failed_retry_max_total = max(0, min(int(args.failed_retry_max_total or 0), 10))
    args.failed_retry_limit = max(1, min(int(args.failed_retry_limit or 1), 200))
    args.failed_retry_max_attempts = max(1, min(int(args.failed_retry_max_attempts or 3), 10))
    args.failed_retry_command_timeout_seconds = max(
        15,
        min(
            int(args.failed_retry_command_timeout_seconds or DEFAULT_FAILED_RETRY_COMMAND_TIMEOUT_SECONDS),
            300,
        ),
    )
    ensure_prowlarr_timeout_headroom(args)
    args.rss_discovery_limit = bounded_int_value(args.rss_discovery_limit, DEFAULT_DISCOVERY_LIMIT, 1, 100)
    args.rss_discovery_max_auto = bounded_int_value(args.rss_discovery_max_auto, DEFAULT_DISCOVERY_MAX_AUTO, 0, 10)
    args.rss_discovery_max_per_series = bounded_int_value(
        args.rss_discovery_max_per_series,
        DEFAULT_DISCOVERY_MAX_PER_SERIES,
        0,
        10,
    )
    args.rss_command_timeout_seconds = max(
        30,
        min(int(args.rss_command_timeout_seconds or DEFAULT_RSS_COMMAND_TIMEOUT_SECONDS), 300),
    )
    args.rss_source_worker_http_timeout_seconds = bounded_int_value(
        getattr(args, "rss_source_worker_http_timeout_seconds", None),
        DEFAULT_RSS_SOURCE_WORKER_HTTP_TIMEOUT_SECONDS,
        5,
        30,
    )
    args.rss_provider_timeout_window_seconds = bounded_int_value(
        getattr(args, "rss_provider_timeout_window_seconds", None),
        DEFAULT_RSS_PROVIDER_TIMEOUT_WINDOW_SECONDS,
        0,
        24 * 3600,
    )
    args.rss_provider_timeout_threshold = bounded_int_value(
        getattr(args, "rss_provider_timeout_threshold", None),
        DEFAULT_RSS_PROVIDER_TIMEOUT_THRESHOLD,
        0,
        100,
    )
    args.rss_provider_timeout_cooldown_seconds = bounded_int_value(
        getattr(args, "rss_provider_timeout_cooldown_seconds", None),
        DEFAULT_RSS_PROVIDER_TIMEOUT_COOLDOWN_SECONDS,
        0,
        24 * 3600,
    )
    args.rss_provider_fetch_failure_window_seconds = bounded_int_value(
        getattr(args, "rss_provider_fetch_failure_window_seconds", None),
        DEFAULT_RSS_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS,
        0,
        24 * 3600,
    )
    args.rss_provider_fetch_failure_threshold = bounded_int_value(
        getattr(args, "rss_provider_fetch_failure_threshold", None),
        DEFAULT_RSS_PROVIDER_FETCH_FAILURE_THRESHOLD,
        0,
        100,
    )
    args.rss_provider_fetch_failure_cooldown_seconds = bounded_int_value(
        getattr(args, "rss_provider_fetch_failure_cooldown_seconds", None),
        DEFAULT_RSS_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS,
        0,
        24 * 3600,
    )
    args.comicscodes_discovery_limit = bounded_int_value(
        args.comicscodes_discovery_limit,
        DEFAULT_DISCOVERY_LIMIT,
        1,
        100,
    )
    args.comicscodes_discovery_max_auto = bounded_int_value(
        args.comicscodes_discovery_max_auto,
        DEFAULT_DISCOVERY_MAX_AUTO,
        0,
        10,
    )
    args.comicscodes_discovery_max_per_series = bounded_int_value(
        args.comicscodes_discovery_max_per_series,
        DEFAULT_DISCOVERY_MAX_PER_SERIES,
        0,
        10,
    )
    args.comicscodes_command_timeout_seconds = max(
        30,
        min(int(args.comicscodes_command_timeout_seconds or DEFAULT_COMICSCODES_COMMAND_TIMEOUT_SECONDS), 300),
    )
    args.mangadex_max_total = max(1, min(int(args.mangadex_max_total or 1), 50))
    args.mangadex_max_per_series = max(1, min(int(args.mangadex_max_per_series or 1), 20))
    args.mangadex_command_timeout_seconds = mangadex_configured_command_timeout_seconds(args)
    args.mangadex_verify_timeout_seconds = mangadex_configured_verify_timeout_seconds(args)
    args.slskd_auto_grab_max = max(0, min(int(args.slskd_auto_grab_max or 0), 10))
    args.slskd_hot_retry_max = max(0, min(int(args.slskd_hot_retry_max or 0), 10))
    args.slskd_max_total = max(1, min(int(args.slskd_max_total or 1), 50))
    args.slskd_max_per_series = max(1, min(int(args.slskd_max_per_series or 1), 20))
    args.slskd_wait_seconds = max(2, min(int(args.slskd_wait_seconds or 8), 30))
    args.slskd_max_queries = max(1, min(int(args.slskd_max_queries or 1), 5))
    args.slskd_probe_budget_seconds = max(30, min(int(args.slskd_probe_budget_seconds or 300), 15 * 60))
    args.retry_seconds = max(300, min(int(args.retry_seconds or 7200), 24 * 3600))
    args.exhaustion_cycles = max(1, min(int(args.exhaustion_cycles or 6), 30))
    run_provider_then_companion_maintenance(args)


if __name__ == "__main__":
    main()
