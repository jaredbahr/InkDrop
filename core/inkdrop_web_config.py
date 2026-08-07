#!/usr/bin/env python3
"""Module-level configuration constants and small stateless helpers shared by inkdrop_web.

Extracted verbatim from inkdrop_web.py so the file's runtime configuration
surface (paths, tunables, feature flags) has one home instead of living
inline with routing and business-logic glue. inkdrop_web.py re-exports
everything here via `from inkdrop_web_config import *`, so every existing
`inkdrop_web.NAME` reference (bare-name inside the module, or dotted from
other modules) keeps working unchanged.
"""

import faulthandler
import hashlib
import os
import signal
import sys
import threading
import time
from pathlib import Path

from core import inkdrop_client_status
from core import inkdrop_manga_unit_policy
from core import inkdrop_runtime_config
from core import inkdrop_state


def env_value(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value in (None, ""):
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


HOST = inkdrop_runtime_config.web_host()
PORT = inkdrop_runtime_config.web_port(strict=False)
WEB_RUNTIME_STARTED_AT = time.time()
MANUAL_SEARCH_THREADS = {}
MANUAL_SEARCH_THREADS_LOCK = threading.Lock()
ARCHIVE_CONVERSION_TASKS = {}
ARCHIVE_CONVERSION_TASKS_LOCK = threading.Lock()
SUPPORT_BUNDLE_BUILD_SLOT = threading.BoundedSemaphore(1)


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


def python_command() -> str:
    return os.environ.get("PYTHON_BIN") or sys.executable or "python3"


ACQUIRE_SCRIPT = script_path("inkdrop_missing_acquire.py", env_var="INKDROP_ACQUIRE_SCRIPT")
ACQUIRE_COMMAND_SCRIPT = script_path(
    "inkdrop_acquire_adapter.py",
    env_var="INKDROP_ACQUIRE_COMMAND_SCRIPT",
    fallback=ACQUIRE_SCRIPT,
)
IMPORT_SCRIPT = script_path("inkdrop_completed_import.py", env_var="INKDROP_IMPORT_SCRIPT")
RECONCILE_SCRIPT = script_path("inkdrop_reconcile_imports.py", env_var="INKDROP_RECONCILE_SCRIPT")
PACK_IMPORT_SCRIPT = script_path("inkdrop_pack_import.py", env_var="INKDROP_PACK_IMPORT_SCRIPT")
MANGA_CHAPTER_ARTIFACT_REPAIR_SCRIPT = script_path(
    "inkdrop_manga_chapter_artifact_repair.py",
    env_var="INKDROP_MANGA_CHAPTER_ARTIFACT_REPAIR_SCRIPT",
)
MIXED_MANGA_UNIT_REPAIR_SCRIPT = script_path(
    "inkdrop_mixed_manga_unit_repair.py",
    env_var="INKDROP_MIXED_MANGA_UNIT_REPAIR_SCRIPT",
)
READER_FRONTEND_ORPHAN_CLEANUP_SCRIPT = script_path(
    "inkdrop_reader_frontend_orphan_cleanup.py",
    env_var="INKDROP_READER_FRONTEND_ORPHAN_CLEANUP_SCRIPT",
)
MISSING_ACQUIRE_SCRIPT = script_path("inkdrop_missing_acquire.py", env_var="INKDROP_MISSING_ACQUIRE_SCRIPT")
MISSING_ACQUIRE_MODULE_SCRIPT = script_path("inkdrop_missing_acquire.py", fallback=MISSING_ACQUIRE_SCRIPT)
SAB_RESCUE_SCRIPT = script_path("sab_rescue_server.py", env_var="INKDROP_SAB_RESCUE_SCRIPT")
RSS_DISCOVERY_SCRIPT = script_path(
    "inkdrop_rss_discovery.py",
    env_var="INKDROP_RSS_DISCOVERY_SCRIPT",
)
COMICSCODES_DISCOVERY_SCRIPT = script_path(
    "inkdrop_comicscodes_discovery.py",
    env_var="INKDROP_COMICSCODES_DISCOVERY_SCRIPT",
)
SLSKD_SOURCE_PROBE_SCRIPT = script_path("inkdrop_slskd_source_probe.py", env_var="INKDROP_SLSKD_SOURCE_PROBE_SCRIPT")
SERIES_AUTOPILOT_SCRIPT = script_path("inkdrop_series_autopilot.py", env_var="INKDROP_SERIES_AUTOPILOT_SCRIPT")
STATE_DIR = inkdrop_runtime_config.state_dir()
LOCK_DIR = inkdrop_runtime_config.lock_dir()
LOCK_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = inkdrop_runtime_config.log_dir()
CACHE_DIR = inkdrop_runtime_config.cache_dir()
BACKUP_DIR = inkdrop_runtime_config.backup_dir()
STAGING_DIR = inkdrop_runtime_config.staging_dir()
MANUAL_INBOX_DIR = inkdrop_runtime_config.manual_inbox_dir()
QUARANTINE_DIR = inkdrop_runtime_config.quarantine_dir()
ACQUIRE_LOG, LEGACY_ACQUIRE_LOG = inkdrop_runtime_config.compatible_log_paths(
    "inkdrop-acquire.log", "kavita-acquire.log"
)
IMPORT_LOG, LEGACY_IMPORT_LOG = inkdrop_runtime_config.compatible_log_paths(
    "inkdrop-import.log", "kavita-import.log"
)
IMPORT_STATUS_FILE = STATE_DIR / "import-status.json"
IMPORT_LOCK = LOCK_DIR / "inkdrop-comics-import.lock"
IMPORT_LOCK_BUSY_CODE = 75
RECONCILE_STATUS_FILE = STATE_DIR / "import-reconcile-status.json"
IMPORTED_DB = STATE_DIR / "imported-files.sqlite3"
IMPORTED_DB_BUSY_TIMEOUT_MS = 8000
WATCHES_FILE = STATE_DIR / "watches.json"
WATCH_LOG, LEGACY_WATCH_LOG = inkdrop_runtime_config.compatible_log_paths(
    "inkdrop-watch.log", "kavita-watch.log"
)
MANUAL_REVIEW_FILE = STATE_DIR / "manual-review.jsonl"
FRESH_SWEEP_LOG = LOG_DIR / "fresh-release-sweep.log"
HOT_SWEEP_LOG = LOG_DIR / "hot-release-sweep.log"
MISSING_ACQUIRE_LOG = LOG_DIR / "missing-acquire-cron.log"
RSS_DISCOVERY_LOG = LOG_DIR / "rss-discovery.log"
RSS_DISCOVERY_STATUS_FILE = STATE_DIR / "rss-discovery-status.json"
RSS_DISCOVERY_STATUS_STALE_MINUTES = 90
COMICSCODES_DISCOVERY_LOG = LOG_DIR / "comicscodes-discovery.log"
COMICSCODES_DISCOVERY_STATUS_FILE = STATE_DIR / "comicscodes-discovery-status.json"
# A gated source's health snapshot (rss/comicscodes) is read from its own
# discovery status file, and a blocking state (backoff/watch/etc.) stops the
# autopilot ladder from ever invoking that discovery script again -- which is
# also the only thing that refreshes the status file. If the last refresh
# predates this, the block has outlived its own longest legitimate backoff
# (12h) and is a stale artifact rather than a live condition, so it's
# reported as unknown instead of a blocking state to let the ladder retry
# and find out. Confirmed live 2026-08-02: comics.codes answered normally
# (HTTP 200, real feed) while its recorded health was still "backoff" from a
# check 33 days earlier.
SOURCE_HEALTH_GATE_RETRY_AFTER_MINUTES = 16 * 60
SLSKD_SOURCE_PROBE_STATUS_FILE = STATE_DIR / "slskd-source-probe-status.json"
SLSKD_SOURCE_PROBE_CACHE_FILE = STATE_DIR / "slskd-source-probe-cache.json"
SLSKD_SOURCE_PROBE_LOG = LOG_DIR / "slskd-source-probe.log"
SLSKD_SOURCE_PROBE_LOCK = LOCK_DIR / "inkdrop-slskd-source-probe.lock"
SLSKD_CONFIG = Path(env_value("INKDROP_SLSKD_CONFIG", "/config/slskd/slskd.yml"))
SLSKD_API_BASE_URL = env_value("INKDROP_SLSKD_API_BASE_URL", "")
SLSKD_WEB_URL = env_value("INKDROP_SLSKD_WEB_URL", SLSKD_API_BASE_URL.rsplit("/api/", 1)[0])
SERIES_AUTOPILOT_STATUS_FILE = STATE_DIR / "series-autopilot-status.json"
SERIES_AUTOPILOT_QUEUE_FILE = STATE_DIR / "series-autopilot-queue.json"
MISSING_RECOVERY_CONTROL_FILE = STATE_DIR / "missing-recovery-control.json"
USER_SEARCH_PRIORITY_FILE = STATE_DIR / "user-search-priority.json"
USER_SEARCH_PRIORITY_MAX_AGE_SECONDS = 10 * 60
USER_SEARCH_PRIORITY_MAX_ENTRIES = 50
SERIES_AUTOPILOT_LOG = LOG_DIR / "series-autopilot.log"
SERIES_AUTOPILOT_QUEUE_SCHEMA_VERSION = 2
SERIES_AUTOPILOT_DEFAULT_SOURCE_ORDER = ["local", "prowlarr", "rss", "slskd"]
SERIES_AUTOPILOT_SOURCE_ORDER = list(SERIES_AUTOPILOT_DEFAULT_SOURCE_ORDER)
SERIES_AUTOPILOT_VALID_SOURCES = set(SERIES_AUTOPILOT_DEFAULT_SOURCE_ORDER) | {"mangadex"}
SERIES_AUTOPILOT_RECOVERY_STEPS = ["failed_retry"]


def slskd_config_candidates():
    candidates = []
    explicit = os.environ.get("INKDROP_SLSKD_CONFIG")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(SLSKD_CONFIG)
    try:
        config_dir = inkdrop_runtime_config.config_dir()
        candidates.append(config_dir / "slskd" / "slskd.yml")
        candidates.append(config_dir.parent / "slskd" / "slskd.yml")
    except Exception:
        pass
    candidates.append(STATE_DIR / "slskd" / "slskd.yml")
    candidates.append(STATE_DIR.parent / "slskd" / "slskd.yml")
    out = []
    seen = set()
    for candidate in candidates:
        try:
            key = str(candidate.expanduser())
        except Exception:
            key = str(candidate)
        if key and key not in seen:
            seen.add(key)
            out.append(Path(key))
    return out


def read_slskd_config_text():
    for candidate in slskd_config_candidates():
        try:
            return candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
    return ""
SERIES_QUEUE_RUNNER_STATUS_FILE = STATE_DIR / "series-queue-runner-status.json"
MANAGED_LIBRARY_AUDIT_LAST_FILE = STATE_DIR / "managed-library-audit-last.json"
SERIES_QUEUE_RUNNER_LOG = LOG_DIR / "series-queue-runner.log"
SERIES_QUEUE_RUNNER_IMPORT_LOG = LOG_DIR / "series-queue-runner-import.log"
INKDROP_STATE_SYNC_LOG = LOG_DIR / "inkdrop-state-sync.log"
INKDROP_STATE_SYNC_LOCK = LOCK_DIR / "inkdrop-state-sync.lock"
INKDROP_STATE_SYNC_STALE_SECONDS = int(env_value("INKDROP_STATE_SYNC_STALE_SECONDS", "900"))
INKDROP_MANUAL_STATE_SYNC_ENABLED = env_bool("INKDROP_MANUAL_STATE_SYNC_ENABLED", False)
INKDROP_STATE_DB = STATE_DIR / inkdrop_state.STATE_DB_NAME
MANUAL_SOURCE_AUTORESOLVE_SCRIPT = script_path(
    "inkdrop_manual_source_autoresolve.py",
    env_var="INKDROP_MANUAL_SOURCE_AUTORESOLVE_SCRIPT",
)
MANUAL_SOURCE_AUTORESOLVE_STATUS_FILE = STATE_DIR / "manual-source-autoresolve-status.json"
MANUAL_SOURCE_QUEUE_SYNC_FILE = STATE_DIR / "manual-source-queue-sync-pending.json"
MISSING_ACQUIRE_CACHE_FILE = STATE_DIR / "missing-acquire-cache.json"
MANUAL_REVIEW_ACTIONS_FILE = STATE_DIR / "manual-review-actions.json"
PACK_REVIEW_STATE_FILE = STATE_DIR / "pack-review-state.json"
PENDING_PACKS_LOG = STATE_DIR / "pending-pack-imports.jsonl"
PACK_IMPORT_LOG = LOG_DIR / "pack-import.log"
PACK_AUTO_IMPORT_STATUS_FILE = STATE_DIR / "pack-auto-import-status.json"
PACK_BAD_ARCHIVE_HISTORY_FILE = STATE_DIR / "pack-bad-archive-history.json"
RSS_ALIASES_FILE = STATE_DIR / "rss-aliases.json"
RSS_BAD_MATCHES_FILE = STATE_DIR / "rss-bad-matches.json"
UNMATCHED_ACTION_LOG = STATE_DIR / "unmatched-download-actions.jsonl"
GUARDED_DISCOVERY_SOURCES = {"rss_discovery", "comicscodes_discovery"}
PACK_REVIEW_REASONS = {"pack_candidate_requires_review", "rss_pack_requires_review"}
PACK_IMPORT_REVIEW_REASONS = {"pack_import_verification_failed", "pack_import_bad_archive"}
PACK_ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
MANUAL_SOURCE_REVIEW_REASONS = {
    "no_safe_source",
    "no_exact_result",
    "no_safe_alternate_found",
    "prowlarr_search_error",
    "manga_no_safe_result",
    "download_client_send_failed",
    "failed_download_duplicate_nzb",
}
AUTOPILOT_SOFT_REVIEW_REASONS = PACK_REVIEW_REASONS | PACK_IMPORT_REVIEW_REASONS | MANUAL_SOURCE_REVIEW_REASONS | {"ambiguous_results"}
AUTOPILOT_HARD_REVIEW_REASONS = {
    "unsafe_or_missing_target_folder",
    "import_verification_failed",
}
AUTOPILOT_HIDE_REVIEW_STATES = {"queued", "searching", "downloading", "importing", "verified"}
PACK_AUTO_IMPORT_COOLDOWN_SECONDS = 10 * 60
PACK_AUTO_IMPORT_IN_PROGRESS_SECONDS = 2 * 60 * 60
PACK_AUTO_IMPORT_POLL_SECONDS = 120
PACK_FINISHED_ACTIVITY_SECONDS = 6 * 60 * 60
PACK_BAD_ARCHIVE_AUTO_BLOCK_MIN = 3
PACK_BAD_ARCHIVE_HISTORY_CACHE = {"mtime": None, "paths": {}, "titles": {}}
SERIES_QUEUE_RUNNER_START_DELAY_SECONDS = 20
SERIES_QUEUE_RUNNER_POLL_SECONDS = 60
SERIES_QUEUE_RUNNER_AUTOPILOT_MIN_SECONDS = 45
SERIES_QUEUE_RUNNER_RESOLVER_MIN_SECONDS = 10 * 60
SERIES_QUEUE_RUNNER_DOWNLOAD_CLIENT_MIN_SECONDS = 120
SERIES_QUEUE_RUNNER_IMPORT_MIN_SECONDS = 180
SERIES_QUEUE_RUNNER_DEFERRED_SYNC_MIN_SECONDS = 300
SERIES_QUEUE_RUNNER_AUTOPILOT_ENABLED = str(os.environ.get("INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED", "0") or "0").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
    "disabled",
}
try:
    SERIES_QUEUE_RUNNER_IMPORT_PRIORITY_READY_IMPORTS = max(
        1,
        int(os.environ.get("INKDROP_QUEUE_RUNNER_IMPORT_PRIORITY_READY_IMPORTS", "1") or "1"),
    )
except (TypeError, ValueError):
    SERIES_QUEUE_RUNNER_IMPORT_PRIORITY_READY_IMPORTS = 1
INKDROP_STATE_IMPORT_READY_STATUSES = (
    "completed_in_client",
    "staged_file_ready",
    "ready_import",
    "preview_importable",
    "ready_to_import",
)
SERIES_AUTOPILOT_WORKER_MAX_SERIES = 10
SERIES_AUTOPILOT_LOCK_NOTICE_SECONDS = 3 * 60
SERIES_AUTOPILOT_LOCK_PRESSURE_SECONDS = 8 * 60
SERIES_AUTOPILOT_LOCK_KERNEL_WAIT_SECONDS = 10 * 60
STATUS_CACHE_TTL_SECONDS = 15
STATUS_CACHE = {"ts": 0.0, "data": None}
STATUS_CACHE_LOCK = threading.Lock()
STATUS_COMPUTE_LOCK = threading.Lock()
DOWNLOAD_CLIENT_STATUS_CACHE = inkdrop_client_status.ClientStatusCache(
    ttl_seconds=max(5, int(env_value("INKDROP_CLIENT_STATUS_CACHE_SECONDS", "15") or 15)),
    stale_seconds=max(30, int(env_value("INKDROP_CLIENT_STATUS_STALE_SECONDS", "120") or 120)),
)
try:
    STATE_ENDPOINT_CONCURRENCY = max(1, min(int(os.environ.get("INKDROP_STATE_ENDPOINT_CONCURRENCY", "2") or "2"), 8))
except (TypeError, ValueError):
    STATE_ENDPOINT_CONCURRENCY = 2
try:
    WEB_SOCKET_TIMEOUT_SECONDS = max(
        5.0,
        min(float(os.environ.get("INKDROP_WEB_SOCKET_TIMEOUT_SECONDS", "30") or "30"), 300.0),
    )
except (TypeError, ValueError):
    WEB_SOCKET_TIMEOUT_SECONDS = 30.0
STATE_ENDPOINT_SEMAPHORE = threading.BoundedSemaphore(STATE_ENDPOINT_CONCURRENCY)
# Log any request that takes longer than this. Low enough to catch what a
# person would call slow, high enough that a healthy install stays quiet.
# Both neighbouring constants wrap their parse; this one did not. A typo in
# INKDROP_SLOW_REQUEST_LOG_SECONDS raised ValueError at import time and took
# the entire web process down before it could serve anything -- an operator
# mistuning a diagnostic threshold should not be able to stop the app.
try:
    SLOW_REQUEST_LOG_SECONDS = max(
        0.2,
        min(float(os.environ.get("INKDROP_SLOW_REQUEST_LOG_SECONDS") or "1.5"), 600.0),
    )
except (TypeError, ValueError):
    SLOW_REQUEST_LOG_SECONDS = 1.5
ACTIVE_WEB_REQUESTS = {}
ACTIVE_WEB_REQUESTS_LOCK = threading.Lock()
DEBUG_ACTIVE_REQUESTS_ENABLED = str(os.environ.get("INKDROP_DEBUG_ACTIVE_REQUESTS") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def install_web_stack_dump_signal():
    try:
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
        return True
    except Exception:
        return False


WEB_STACK_DUMP_SIGNAL_INSTALLED = install_web_stack_dump_signal()
COMICVINE_WATCHES_CACHE = {"ts": 0.0, "payload": None}
COMICVINE_WATCHES_CACHE_LOCK = threading.Lock()
COMICVINE_WATCHES_CACHE_TTL_SECONDS = 10
SYSTEM_DISK_WARN_USED_PERCENT = 90.0
SYSTEM_DISK_CRITICAL_USED_PERCENT = 95.0
SYSTEM_DISK_WARN_FREE_GIB = 20.0
SYSTEM_DISK_CRITICAL_FREE_GIB = 5.0
SYSTEM_LOG_WARN_MIB = 256.0
SYSTEM_LOG_CRITICAL_MIB = 1024.0
SYSTEM_HEALTH_LOG_LIMIT = 8
SERIES_AUTOPILOT_LOCK_PATH = LOCK_DIR / "inkdrop-series-autopilot.lock"
SERIES_AUTOPILOT_STATUS_REPAIR_STALE_SECONDS = 3 * 60
SERIES_AUTOPILOT_STATUS_REPAIR_COOLDOWN_SECONDS = 60
SERIES_AUTOPILOT_STATUS_REPAIR_ANNOTATE_SECONDS = 5
SERIES_AUTOPILOT_STATUS_REPAIR_TIMEOUT_SECONDS = 15
SERIES_AUTOPILOT_STATUS_REPAIR_CACHE = {"last_attempt": 0.0}
COMPLETION_STALE_AUDIT_CACHE = {"ts": 0.0, "data": None}
COMPLETION_STALE_AUDIT_CACHE_TTL_SECONDS = 300
COMPLETION_STALE_AUDIT_CACHE_LOCK = threading.Lock()
KAVITA_COMPLETED_IMPORT_MODULE = None
MANGA_CHAPTER_ARTIFACT_REPAIR_MODULE = None
MIXED_MANGA_UNIT_REPAIR_MODULE = None
READER_FRONTEND_ORPHAN_CLEANUP_MODULE = None
MANUAL_REVIEW_NATIVE_SYNC_TTL_SECONDS = 30
# How long an operator-facing review exception may stay invisible.
MANUAL_REVIEW_RECONCILE_INTERVAL_SECONDS = 60
MANUAL_REVIEW_NATIVE_SYNC_CACHE = {"signature": None, "ts": 0.0, "result": None}
MANUAL_REVIEW_NATIVE_SYNC_LOCK = threading.Lock()
SERIES_AUTOPILOT_WORKER_MAX_ISSUES = 12


def web_runtime_status_fields():
    return {
        "runtime_pid": os.getpid(),
        "runtime_started_at": WEB_RUNTIME_STARTED_AT,
        "runtime_started_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(WEB_RUNTIME_STARTED_AT)),
        "runtime_uptime_seconds": round(max(0.0, time.time() - WEB_RUNTIME_STARTED_AT), 3),
    }


def attach_web_runtime_status(payload):
    out = dict(payload or {})
    out.update(web_runtime_status_fields())
    return out


try:
    SERIES_AUTOPILOT_BACKGROUND_MAX_SERIES = max(
        1,
        min(int(os.environ.get("INKDROP_AUTOPILOT_BACKGROUND_MAX_SERIES", "12") or "12"), 20),
    )
except (TypeError, ValueError):
    SERIES_AUTOPILOT_BACKGROUND_MAX_SERIES = 12
try:
    SERIES_AUTOPILOT_BACKGROUND_MAX_ISSUES = max(
        1,
        min(int(os.environ.get("INKDROP_AUTOPILOT_BACKGROUND_MAX_ISSUES", "16") or "16"), 50),
    )
except (TypeError, ValueError):
    SERIES_AUTOPILOT_BACKGROUND_MAX_ISSUES = 16
SERIES_AUTOPILOT_BACKGROUND_TIMEOUT = os.environ.get("INKDROP_AUTOPILOT_BACKGROUND_TIMEOUT", "30m") or "30m"
SERIES_QUEUE_RUNNER_TIMEOUT_KILL_AFTER = "30s"
try:
    SERIES_AUTOPILOT_MAX_RUN_SECONDS = max(
        120,
        min(int(os.environ.get("INKDROP_AUTOPILOT_MAX_RUN_SECONDS", str(15 * 60)) or str(15 * 60)), 30 * 60),
    )
except (TypeError, ValueError):
    SERIES_AUTOPILOT_MAX_RUN_SECONDS = 15 * 60
SERIES_AUTOPILOT_WEB_TRIGGER_LOCK_WAIT_SECONDS = 0
try:
    SERIES_AUTOPILOT_MISSING_MAX_PER_SERIES = max(
        1,
        min(int(os.environ.get("INKDROP_AUTOPILOT_MISSING_MAX_PER_SERIES", "10") or "10"), 25),
    )
except (TypeError, ValueError):
    SERIES_AUTOPILOT_MISSING_MAX_PER_SERIES = 10
try:
    SERIES_AUTOPILOT_MISSING_MAX_TOTAL = max(
        5,
        min(int(os.environ.get("INKDROP_AUTOPILOT_MISSING_MAX_TOTAL", "80") or "80"), 120),
    )
except (TypeError, ValueError):
    SERIES_AUTOPILOT_MISSING_MAX_TOTAL = 80
SERIES_AUTOPILOT_PROWLARR_LIMIT = 20
SERIES_AUTOPILOT_PROWLARR_MAX_QUERIES_PER_ISSUE = 6
SERIES_AUTOPILOT_PROWLARR_SEARCH_TIMEOUT = 12
SERIES_AUTOPILOT_PROWLARR_COMMAND_TIMEOUT = 60
SERIES_AUTOPILOT_PROWLARR_SEARCH_BUDGET_SECONDS = 45
SERIES_AUTOPILOT_PROWLARR_PROVIDER_TIMEOUT_WINDOW_SECONDS = 1800
SERIES_AUTOPILOT_PROWLARR_PROVIDER_TIMEOUT_THRESHOLD = 3
SERIES_AUTOPILOT_PROWLARR_PROVIDER_TIMEOUT_COOLDOWN_SECONDS = 1800
SERIES_AUTOPILOT_PROWLARR_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS = 1800
SERIES_AUTOPILOT_PROWLARR_PROVIDER_FETCH_FAILURE_THRESHOLD = 2
SERIES_AUTOPILOT_PROWLARR_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS = 1800
SERIES_AUTOPILOT_PROWLARR_NO_RESULT_COOLDOWN_HOURS = 0.33
SERIES_AUTOPILOT_RSS_SOURCE_WORKER_HTTP_TIMEOUT_SECONDS = 12
SERIES_AUTOPILOT_RSS_PROVIDER_TIMEOUT_WINDOW_SECONDS = 1800
SERIES_AUTOPILOT_RSS_PROVIDER_TIMEOUT_THRESHOLD = 3
SERIES_AUTOPILOT_RSS_PROVIDER_TIMEOUT_COOLDOWN_SECONDS = 1800
SERIES_AUTOPILOT_RSS_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS = 1800
SERIES_AUTOPILOT_RSS_PROVIDER_FETCH_FAILURE_THRESHOLD = 2
SERIES_AUTOPILOT_RSS_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS = 1800
SERIES_AUTOPILOT_FAILED_RETRY_COMMAND_TIMEOUT = 60
SERIES_AUTOPILOT_SOURCE_LOCK_WAIT_SECONDS = 5
SERIES_AUTOPILOT_DISCOVERY_LIMIT = 24
SERIES_AUTOPILOT_DISCOVERY_MAX_AUTO = 6
SERIES_AUTOPILOT_DISCOVERY_MAX_PER_SERIES = 3
SERIES_AUTOPILOT_SLSKD_MAX_TOTAL = 20
SERIES_AUTOPILOT_SLSKD_MAX_PER_SERIES = 12
SERIES_AUTOPILOT_SLSKD_WAIT_SECONDS = 8
SERIES_AUTOPILOT_SLSKD_MAX_QUERIES = 5
SERIES_AUTOPILOT_SLSKD_AUTO_GRAB_MAX = 8
SERIES_AUTOPILOT_SLSKD_PROBE_BUDGET_SECONDS = 300
SERIES_AUTOPILOT_SLSKD_COOLDOWN_HOURS = 0.0
SERIES_AUTOPILOT_RETRY_SECONDS = 1800
# Shared with inkdrop_completed_import's copies -- see inkdrop_manga_unit_policy.
MANGA_UNIT_MODELS = inkdrop_manga_unit_policy.MANGA_UNIT_MODELS
MANGA_UNIT_POLICY_TO_MODEL = inkdrop_manga_unit_policy.MANGA_UNIT_POLICY_TO_MODEL
MANGA_UNIT_MODEL_TO_POLICY = inkdrop_manga_unit_policy.MANGA_UNIT_MODEL_TO_POLICY
MANGA_UNIT_POLICY_LABELS = inkdrop_manga_unit_policy.MANGA_UNIT_POLICY_LABELS
normalize_manga_unit_model = inkdrop_manga_unit_policy.normalize_manga_unit_model
manga_unit_policy_for_model = inkdrop_manga_unit_policy.manga_unit_policy_for_model


def manga_unit_policy_label(policy_or_model):
    policy = (
        str(policy_or_model or "").strip().lower().replace("-", "_").replace(" ", "_")
        if str(policy_or_model or "").strip().lower().replace("-", "_").replace(" ", "_") in MANGA_UNIT_POLICY_TO_MODEL
        else manga_unit_policy_for_model(policy_or_model)
    )
    return MANGA_UNIT_POLICY_LABELS.get(policy, policy.replace("_", " ").title())
MANGA_PUBLISHER_HINTS = {
    "shueisha",
    "hakusensha",
    "kodansha",
    "viz",
    "yen press",
    "seven seas",
    "dark horse manga",
    "tokyopop",
    "square enix",
}
MANGA_TITLE_HINTS = {"berserk", "chainsaw man", "one piece", "onepiece", "fire punch", "firepunch", "vagabond"}
MANUAL_INBOX_EXTS = {".cbz", ".cbr", ".pdf", ".epub"}
COMIC_SERIES_FILE = STATE_DIR / "comic-series-watches.json"
KAVITA_API = env_value("INKDROP_KAVITA_URL", "")
KAVITA_DB = inkdrop_runtime_config.kavita_db_path()
KOMGA_API = env_value("INKDROP_KOMGA_URL", "")
INKDROP_LOGO_MARK_FILE = Path(__file__).resolve().parents[1] / "inkdrop-logo-mark.png"
INKDROP_UI_CSS_FILE = Path(__file__).resolve().parents[1] / "web" / "static" / "css" / "inkdrop.css"
INKDROP_AUTH_BACKDROP_FILE = Path(__file__).resolve().parents[1] / "web" / "static" / "img" / "inkdrop-auth-backdrop.webp"
INKDROP_UI_JS_DIR = Path(__file__).resolve().parents[1] / "web" / "static" / "js"
INKDROP_UI_JS_ASSETS = frozenset(
    {
        "inkdrop-operational-preferences.js",
        "inkdrop-operational-table-controls.js",
        "inkdrop-operational-query-controls.js",
        "inkdrop-operational-row-controls.js",
        "inkdrop-transfer-telemetry.js",
        "inkdrop-version-about.js",
        "inkdrop-activity-ui.js",
        "inkdrop-operational-bootstrap.js",
        "inkdrop-api.js",
        "inkdrop-manual-search.js",
        "inkdrop-auth-ui.js",
        "inkdrop-download-clients-ui.js",
        "inkdrop-missing-recovery.js",
    }
)
try:
    INKDROP_UI_CSS_VERSION = hashlib.sha256(INKDROP_UI_CSS_FILE.read_bytes()).hexdigest()[:12]
except OSError:
    INKDROP_UI_CSS_VERSION = "missing"
try:
    INKDROP_UI_JS_VERSION = hashlib.sha256(
        b"".join((INKDROP_UI_JS_DIR / name).read_bytes() for name in sorted(INKDROP_UI_JS_ASSETS))
    ).hexdigest()[:12]
except OSError:
    INKDROP_UI_JS_VERSION = "missing"
# Built by web/frontend (Vite) into web/static/dist; not committed to Git, so
# a checkout that hasn't run `npm run build` (or the Docker image, which
# always has) simply reports "missing" the same way a stripped-down export
# with no CSS/JS assets does above.
INKDROP_UI_REACT_DIR = Path(__file__).resolve().parents[1] / "web" / "static" / "dist"
INKDROP_UI_REACT_ASSETS = frozenset({"inkdrop-react.js"})
try:
    INKDROP_UI_REACT_VERSION = hashlib.sha256(
        b"".join((INKDROP_UI_REACT_DIR / name).read_bytes() for name in sorted(INKDROP_UI_REACT_ASSETS))
    ).hexdigest()[:12]
except OSError:
    INKDROP_UI_REACT_VERSION = "missing"
COVER_CACHE_DIR = CACHE_DIR / "cover-cache"
COVER_PROXY_ALLOWED_HOSTS = {
    "comicvine.gamespot.com",
    "www.comicvine.gamespot.com",
    "uploads.mangadex.org",
}
COVER_PROXY_MAX_BYTES = 8 * 1024 * 1024
COVER_PROXY_TIMEOUT_SECONDS = 20
COVER_PROXY_CACHE_SECONDS = 60 * 60
COVER_PROXY_STALE_REVALIDATE_SECONDS = 24 * 60 * 60
COVER_PROXY_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
COMIC_ROOT = Path(env_value("INKDROP_COMIC_ROOT", "/library/comics"))
MANGA_ROOT = Path(env_value("INKDROP_MANGA_ROOT", "/library/manga"))
KAVITA_COMIC_ROOT = env_value("INKDROP_KAVITA_COMIC_ROOT", "/data/comics")
KAVITA_MANGA_ROOT = env_value("INKDROP_KAVITA_MANGA_ROOT", "/data/manga")
MANUAL_COMICS_INBOX = Path(env_value("INKDROP_MANUAL_COMICS_INBOX", str(MANUAL_INBOX_DIR / "comics")))
MANUAL_EBOOKS_INBOX = Path(env_value("INKDROP_MANUAL_EBOOKS_INBOX", str(MANUAL_INBOX_DIR / "ebooks")))
SLSKD_DOWNLOAD_ROOT = Path(env_value("INKDROP_SLSKD_DOWNLOAD_ROOT", str(STAGING_DIR / "slskd")))
SLSKD_INCOMPLETE_ROOT = SLSKD_DOWNLOAD_ROOT / "incomplete"
UNMATCHED_LOCAL_ROOTS = (
    Path(env_value("INKDROP_UNMATCHED_DOWNLOAD_ROOT", str(STAGING_DIR / "downloads" / "comics"))),
    Path(env_value("INKDROP_DIRECT_DOWNLOAD_ROOT", str(STAGING_DIR / "direct" / "comics"))),
    MANUAL_COMICS_INBOX,
    Path(env_value("INKDROP_SUWAYOMI_STAGING_ROOT", str(STAGING_DIR / "suwayomi"))),
)
UNMATCHED_QUARANTINE_ALLOWED_ROOTS = (
    Path(env_value("INKDROP_UNMATCHED_DOWNLOAD_ROOT", str(STAGING_DIR / "downloads" / "comics"))),
    MANUAL_COMICS_INBOX,
    Path(env_value("INKDROP_SUWAYOMI_STAGING_ROOT", str(STAGING_DIR / "suwayomi"))),
)
UNMATCHED_QUARANTINE_ROOT = Path(env_value("INKDROP_UNMATCHED_QUARANTINE_ROOT", str(QUARANTINE_DIR / "unmatched")))
COMICVINE_API = "https://comicvine.gamespot.com/api"
MANGADEX_API = "https://api.mangadex.org"
MANGADEX_SITE_URL = "https://mangadex.org/title"
MANGADEX_COVER_URL = "https://uploads.mangadex.org/covers"
SAB_COMIC_CATEGORIES = {"comics", "manga", "mylar", "kapowarr"}
COMICVINE_USER_AGENT = env_value("INKDROP_COMICVINE_USER_AGENT", "InkDrop/0.1 (+metadata lookup)")
MANGADEX_USER_AGENT = "InkDrop/0.1 (+metadata lookup)"
# MangaDex's own API default is safe + suggestive + erotica; it excludes only
# pornographic. InkDrop defaulted to safe + suggestive, which is stricter than
# upstream and silently so: the rating filter is applied in the request, so an
# excluded title is not down-ranked or flagged, it simply does not exist as far as
# the user can tell. Berserk is rated erotica on MangaDex, so searching for it
# returned nothing at all -- reported by a tester on 2026-08-01, and it is 8,959
# titles wide, not one book.
#
# Mature content is still handled, just at the right layer: mangadex_search_result
# scores erotica and pornographic at -35, so it ranks below an equally-matching
# safe title instead of vanishing. That penalty was dead code for erotica while
# the request filter removed those rows first. Pornographic stays excluded by
# default, and the whole list remains user-configurable.
MANGADEX_DEFAULT_CONTENT_RATINGS = ("safe", "suggestive", "erotica")
# Smaller than the narrowest relevance tier gap in mangadex_result_score (12).
MANGADEX_MATURE_RATING_PENALTY = 8

COMICVINE_ENGLISH_PUBLISHER_HINTS = {
    "abrams",
    "archie",
    "boom studios",
    "dark horse",
    "dark horse comics",
    "dark horse manga",
    "dc",
    "dc comics",
    "denpa",
    "drawn and quarterly",
    "dynamite entertainment",
    "fantagraphics",
    "first second",
    "idw publishing",
    "image",
    "image comics",
    "kodansha comics",
    "marvel",
    "marvel comics",
    "oni press",
    "seven seas",
    "seven seas entertainment",
    "square enix manga",
    "titan comics",
    "vertical",
    "viz",
    "viz media",
    "yen press",
}
COMICVINE_NON_ENGLISH_PUBLISHER_HINTS = {
    "carlsen",
    "delcourt",
    "egmont",
    "glenat",
    "hakusensha",
    "ivrea",
    "kana",
    "kaze",
    "norma editorial",
    "ecc ediciones",
    "panini comics",
    "panini verlag",
    "pika edition",
    "planeta",
    "planeta deagostini",
    "shogakukan",
    "shueisha",
    "star comics",
    "tong li publishing",
}
MAX_AUTO_GRABS_PER_SCAN = 3
MAX_AUTO_GRABS_PER_WATCH = 1


def watch_auto_grab_enabled(watch):
    return bool((watch or {}).get("autoGrab", True))


__all__ = [
    "ACQUIRE_COMMAND_SCRIPT",
    "ACQUIRE_LOG",
    "ACQUIRE_SCRIPT",
    "ACTIVE_WEB_REQUESTS",
    "ACTIVE_WEB_REQUESTS_LOCK",
    "ARCHIVE_CONVERSION_TASKS",
    "ARCHIVE_CONVERSION_TASKS_LOCK",
    "AUTOPILOT_HARD_REVIEW_REASONS",
    "AUTOPILOT_HIDE_REVIEW_STATES",
    "AUTOPILOT_SOFT_REVIEW_REASONS",
    "BACKUP_DIR",
    "CACHE_DIR",
    "COMICSCODES_DISCOVERY_LOG",
    "COMICSCODES_DISCOVERY_SCRIPT",
    "COMICSCODES_DISCOVERY_STATUS_FILE",
    "COMICVINE_API",
    "COMICVINE_ENGLISH_PUBLISHER_HINTS",
    "COMICVINE_NON_ENGLISH_PUBLISHER_HINTS",
    "COMICVINE_USER_AGENT",
    "COMICVINE_WATCHES_CACHE",
    "COMICVINE_WATCHES_CACHE_LOCK",
    "COMICVINE_WATCHES_CACHE_TTL_SECONDS",
    "COMIC_ROOT",
    "COMIC_SERIES_FILE",
    "COMPLETION_STALE_AUDIT_CACHE",
    "COMPLETION_STALE_AUDIT_CACHE_LOCK",
    "COMPLETION_STALE_AUDIT_CACHE_TTL_SECONDS",
    "COVER_CACHE_DIR",
    "COVER_PROXY_ALLOWED_HOSTS",
    "COVER_PROXY_CACHE_SECONDS",
    "COVER_PROXY_CONTENT_TYPES",
    "COVER_PROXY_MAX_BYTES",
    "COVER_PROXY_STALE_REVALIDATE_SECONDS",
    "COVER_PROXY_TIMEOUT_SECONDS",
    "DEBUG_ACTIVE_REQUESTS_ENABLED",
    "DOWNLOAD_CLIENT_STATUS_CACHE",
    "FRESH_SWEEP_LOG",
    "GUARDED_DISCOVERY_SOURCES",
    "HOST",
    "HOT_SWEEP_LOG",
    "IMPORTED_DB",
    "IMPORTED_DB_BUSY_TIMEOUT_MS",
    "IMPORT_LOCK",
    "IMPORT_LOCK_BUSY_CODE",
    "IMPORT_LOG",
    "IMPORT_SCRIPT",
    "IMPORT_STATUS_FILE",
    "INKDROP_AUTH_BACKDROP_FILE",
    "INKDROP_LOGO_MARK_FILE",
    "INKDROP_MANUAL_STATE_SYNC_ENABLED",
    "INKDROP_STATE_DB",
    "INKDROP_STATE_IMPORT_READY_STATUSES",
    "INKDROP_STATE_SYNC_LOCK",
    "INKDROP_STATE_SYNC_LOG",
    "INKDROP_STATE_SYNC_STALE_SECONDS",
    "INKDROP_UI_CSS_FILE",
    "INKDROP_UI_CSS_VERSION",
    "INKDROP_UI_JS_ASSETS",
    "INKDROP_UI_JS_DIR",
    "INKDROP_UI_JS_VERSION",
    "INKDROP_UI_REACT_ASSETS",
    "INKDROP_UI_REACT_DIR",
    "INKDROP_UI_REACT_VERSION",
    "KAVITA_API",
    "KAVITA_COMIC_ROOT",
    "KAVITA_COMPLETED_IMPORT_MODULE",
    "KAVITA_DB",
    "KAVITA_MANGA_ROOT",
    "KOMGA_API",
    "LEGACY_ACQUIRE_LOG",
    "LEGACY_IMPORT_LOG",
    "LEGACY_WATCH_LOG",
    "LOCK_DIR",
    "LOG_DIR",
    "MANAGED_LIBRARY_AUDIT_LAST_FILE",
    "MANGADEX_API",
    "MANGADEX_COVER_URL",
    "MANGADEX_DEFAULT_CONTENT_RATINGS",
    "MANGADEX_MATURE_RATING_PENALTY",
    "MANGADEX_SITE_URL",
    "MANGADEX_USER_AGENT",
    "MANGA_CHAPTER_ARTIFACT_REPAIR_MODULE",
    "MANGA_CHAPTER_ARTIFACT_REPAIR_SCRIPT",
    "MANGA_PUBLISHER_HINTS",
    "MANGA_ROOT",
    "MANGA_TITLE_HINTS",
    "MANGA_UNIT_MODELS",
    "MANGA_UNIT_MODEL_TO_POLICY",
    "MANGA_UNIT_POLICY_LABELS",
    "MANGA_UNIT_POLICY_TO_MODEL",
    "MANUAL_COMICS_INBOX",
    "MANUAL_EBOOKS_INBOX",
    "MANUAL_INBOX_DIR",
    "MANUAL_INBOX_EXTS",
    "MANUAL_REVIEW_ACTIONS_FILE",
    "MANUAL_REVIEW_FILE",
    "MANUAL_REVIEW_NATIVE_SYNC_CACHE",
    "MANUAL_REVIEW_NATIVE_SYNC_LOCK",
    "MANUAL_REVIEW_NATIVE_SYNC_TTL_SECONDS",
    "MANUAL_REVIEW_RECONCILE_INTERVAL_SECONDS",
    "MANUAL_SEARCH_THREADS",
    "MANUAL_SEARCH_THREADS_LOCK",
    "MANUAL_SOURCE_AUTORESOLVE_SCRIPT",
    "MANUAL_SOURCE_AUTORESOLVE_STATUS_FILE",
    "MANUAL_SOURCE_QUEUE_SYNC_FILE",
    "MANUAL_SOURCE_REVIEW_REASONS",
    "MAX_AUTO_GRABS_PER_SCAN",
    "MAX_AUTO_GRABS_PER_WATCH",
    "MISSING_ACQUIRE_CACHE_FILE",
    "MISSING_ACQUIRE_LOG",
    "MISSING_ACQUIRE_MODULE_SCRIPT",
    "MISSING_ACQUIRE_SCRIPT",
    "MISSING_RECOVERY_CONTROL_FILE",
    "MIXED_MANGA_UNIT_REPAIR_MODULE",
    "MIXED_MANGA_UNIT_REPAIR_SCRIPT",
    "PACK_ARCHIVE_EXTENSIONS",
    "PACK_AUTO_IMPORT_COOLDOWN_SECONDS",
    "PACK_AUTO_IMPORT_IN_PROGRESS_SECONDS",
    "PACK_AUTO_IMPORT_POLL_SECONDS",
    "PACK_AUTO_IMPORT_STATUS_FILE",
    "PACK_BAD_ARCHIVE_AUTO_BLOCK_MIN",
    "PACK_BAD_ARCHIVE_HISTORY_CACHE",
    "PACK_BAD_ARCHIVE_HISTORY_FILE",
    "PACK_FINISHED_ACTIVITY_SECONDS",
    "PACK_IMPORT_LOG",
    "PACK_IMPORT_REVIEW_REASONS",
    "PACK_IMPORT_SCRIPT",
    "PACK_REVIEW_REASONS",
    "PACK_REVIEW_STATE_FILE",
    "PENDING_PACKS_LOG",
    "PORT",
    "QUARANTINE_DIR",
    "READER_FRONTEND_ORPHAN_CLEANUP_MODULE",
    "READER_FRONTEND_ORPHAN_CLEANUP_SCRIPT",
    "RECONCILE_SCRIPT",
    "RECONCILE_STATUS_FILE",
    "RSS_ALIASES_FILE",
    "RSS_BAD_MATCHES_FILE",
    "RSS_DISCOVERY_LOG",
    "RSS_DISCOVERY_SCRIPT",
    "RSS_DISCOVERY_STATUS_FILE",
    "RSS_DISCOVERY_STATUS_STALE_MINUTES",
    "SAB_COMIC_CATEGORIES",
    "SAB_RESCUE_SCRIPT",
    "SERIES_AUTOPILOT_BACKGROUND_MAX_ISSUES",
    "SERIES_AUTOPILOT_BACKGROUND_MAX_SERIES",
    "SERIES_AUTOPILOT_BACKGROUND_TIMEOUT",
    "SERIES_AUTOPILOT_DEFAULT_SOURCE_ORDER",
    "SERIES_AUTOPILOT_DISCOVERY_LIMIT",
    "SERIES_AUTOPILOT_DISCOVERY_MAX_AUTO",
    "SERIES_AUTOPILOT_DISCOVERY_MAX_PER_SERIES",
    "SERIES_AUTOPILOT_FAILED_RETRY_COMMAND_TIMEOUT",
    "SERIES_AUTOPILOT_LOCK_KERNEL_WAIT_SECONDS",
    "SERIES_AUTOPILOT_LOCK_NOTICE_SECONDS",
    "SERIES_AUTOPILOT_LOCK_PATH",
    "SERIES_AUTOPILOT_LOCK_PRESSURE_SECONDS",
    "SERIES_AUTOPILOT_LOG",
    "SERIES_AUTOPILOT_MAX_RUN_SECONDS",
    "SERIES_AUTOPILOT_MISSING_MAX_PER_SERIES",
    "SERIES_AUTOPILOT_MISSING_MAX_TOTAL",
    "SERIES_AUTOPILOT_PROWLARR_COMMAND_TIMEOUT",
    "SERIES_AUTOPILOT_PROWLARR_LIMIT",
    "SERIES_AUTOPILOT_PROWLARR_MAX_QUERIES_PER_ISSUE",
    "SERIES_AUTOPILOT_PROWLARR_NO_RESULT_COOLDOWN_HOURS",
    "SERIES_AUTOPILOT_PROWLARR_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS",
    "SERIES_AUTOPILOT_PROWLARR_PROVIDER_FETCH_FAILURE_THRESHOLD",
    "SERIES_AUTOPILOT_PROWLARR_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS",
    "SERIES_AUTOPILOT_PROWLARR_PROVIDER_TIMEOUT_COOLDOWN_SECONDS",
    "SERIES_AUTOPILOT_PROWLARR_PROVIDER_TIMEOUT_THRESHOLD",
    "SERIES_AUTOPILOT_PROWLARR_PROVIDER_TIMEOUT_WINDOW_SECONDS",
    "SERIES_AUTOPILOT_PROWLARR_SEARCH_BUDGET_SECONDS",
    "SERIES_AUTOPILOT_PROWLARR_SEARCH_TIMEOUT",
    "SERIES_AUTOPILOT_QUEUE_FILE",
    "SERIES_AUTOPILOT_QUEUE_SCHEMA_VERSION",
    "SERIES_AUTOPILOT_RECOVERY_STEPS",
    "SERIES_AUTOPILOT_RETRY_SECONDS",
    "SERIES_AUTOPILOT_RSS_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS",
    "SERIES_AUTOPILOT_RSS_PROVIDER_FETCH_FAILURE_THRESHOLD",
    "SERIES_AUTOPILOT_RSS_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS",
    "SERIES_AUTOPILOT_RSS_PROVIDER_TIMEOUT_COOLDOWN_SECONDS",
    "SERIES_AUTOPILOT_RSS_PROVIDER_TIMEOUT_THRESHOLD",
    "SERIES_AUTOPILOT_RSS_PROVIDER_TIMEOUT_WINDOW_SECONDS",
    "SERIES_AUTOPILOT_RSS_SOURCE_WORKER_HTTP_TIMEOUT_SECONDS",
    "SERIES_AUTOPILOT_SCRIPT",
    "SERIES_AUTOPILOT_SLSKD_AUTO_GRAB_MAX",
    "SERIES_AUTOPILOT_SLSKD_COOLDOWN_HOURS",
    "SERIES_AUTOPILOT_SLSKD_MAX_PER_SERIES",
    "SERIES_AUTOPILOT_SLSKD_MAX_QUERIES",
    "SERIES_AUTOPILOT_SLSKD_MAX_TOTAL",
    "SERIES_AUTOPILOT_SLSKD_PROBE_BUDGET_SECONDS",
    "SERIES_AUTOPILOT_SLSKD_WAIT_SECONDS",
    "SERIES_AUTOPILOT_SOURCE_LOCK_WAIT_SECONDS",
    "SERIES_AUTOPILOT_SOURCE_ORDER",
    "SERIES_AUTOPILOT_STATUS_FILE",
    "SERIES_AUTOPILOT_STATUS_REPAIR_ANNOTATE_SECONDS",
    "SERIES_AUTOPILOT_STATUS_REPAIR_CACHE",
    "SERIES_AUTOPILOT_STATUS_REPAIR_COOLDOWN_SECONDS",
    "SERIES_AUTOPILOT_STATUS_REPAIR_STALE_SECONDS",
    "SERIES_AUTOPILOT_STATUS_REPAIR_TIMEOUT_SECONDS",
    "SERIES_AUTOPILOT_VALID_SOURCES",
    "SERIES_AUTOPILOT_WEB_TRIGGER_LOCK_WAIT_SECONDS",
    "SERIES_AUTOPILOT_WORKER_MAX_ISSUES",
    "SERIES_AUTOPILOT_WORKER_MAX_SERIES",
    "SERIES_QUEUE_RUNNER_AUTOPILOT_ENABLED",
    "SERIES_QUEUE_RUNNER_AUTOPILOT_MIN_SECONDS",
    "SERIES_QUEUE_RUNNER_DEFERRED_SYNC_MIN_SECONDS",
    "SERIES_QUEUE_RUNNER_DOWNLOAD_CLIENT_MIN_SECONDS",
    "SERIES_QUEUE_RUNNER_IMPORT_LOG",
    "SERIES_QUEUE_RUNNER_IMPORT_MIN_SECONDS",
    "SERIES_QUEUE_RUNNER_IMPORT_PRIORITY_READY_IMPORTS",
    "SERIES_QUEUE_RUNNER_LOG",
    "SERIES_QUEUE_RUNNER_POLL_SECONDS",
    "SERIES_QUEUE_RUNNER_RESOLVER_MIN_SECONDS",
    "SERIES_QUEUE_RUNNER_START_DELAY_SECONDS",
    "SERIES_QUEUE_RUNNER_STATUS_FILE",
    "SERIES_QUEUE_RUNNER_TIMEOUT_KILL_AFTER",
    "SLOW_REQUEST_LOG_SECONDS",
    "SLSKD_API_BASE_URL",
    "SLSKD_CONFIG",
    "SLSKD_DOWNLOAD_ROOT",
    "SLSKD_INCOMPLETE_ROOT",
    "SLSKD_SOURCE_PROBE_CACHE_FILE",
    "SLSKD_SOURCE_PROBE_LOCK",
    "SLSKD_SOURCE_PROBE_LOG",
    "SLSKD_SOURCE_PROBE_SCRIPT",
    "SLSKD_SOURCE_PROBE_STATUS_FILE",
    "SLSKD_WEB_URL",
    "SOURCE_HEALTH_GATE_RETRY_AFTER_MINUTES",
    "STAGING_DIR",
    "STATE_DIR",
    "STATE_ENDPOINT_CONCURRENCY",
    "STATE_ENDPOINT_SEMAPHORE",
    "STATUS_CACHE",
    "STATUS_CACHE_LOCK",
    "STATUS_CACHE_TTL_SECONDS",
    "STATUS_COMPUTE_LOCK",
    "SUPPORT_BUNDLE_BUILD_SLOT",
    "SYSTEM_DISK_CRITICAL_FREE_GIB",
    "SYSTEM_DISK_CRITICAL_USED_PERCENT",
    "SYSTEM_DISK_WARN_FREE_GIB",
    "SYSTEM_DISK_WARN_USED_PERCENT",
    "SYSTEM_HEALTH_LOG_LIMIT",
    "SYSTEM_LOG_CRITICAL_MIB",
    "SYSTEM_LOG_WARN_MIB",
    "UNMATCHED_ACTION_LOG",
    "UNMATCHED_LOCAL_ROOTS",
    "UNMATCHED_QUARANTINE_ALLOWED_ROOTS",
    "UNMATCHED_QUARANTINE_ROOT",
    "USER_SEARCH_PRIORITY_FILE",
    "USER_SEARCH_PRIORITY_MAX_AGE_SECONDS",
    "USER_SEARCH_PRIORITY_MAX_ENTRIES",
    "WATCHES_FILE",
    "WATCH_LOG",
    "WEB_RUNTIME_STARTED_AT",
    "WEB_SOCKET_TIMEOUT_SECONDS",
    "WEB_STACK_DUMP_SIGNAL_INSTALLED",
    "attach_web_runtime_status",
    "env_bool",
    "env_value",
    "install_web_stack_dump_signal",
    "manga_unit_policy_for_model",
    "manga_unit_policy_label",
    "normalize_manga_unit_model",
    "python_command",
    "read_slskd_config_text",
    "script_path",
    "slskd_config_candidates",
    "watch_auto_grab_enabled",
    "web_runtime_status_fields",
]
