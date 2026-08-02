#!/usr/bin/env python3
import argparse
import calendar
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import unicodedata
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

import requests

try:
    import inkdrop_state
except Exception:
    inkdrop_state = None

import inkdrop_runtime_config
import inkdrop_internal_jobs
import inkdrop_download_client_routing
import inkdrop_sources

try:
    import inkdrop_language
except Exception:
    inkdrop_language = None

try:
    import inkdrop_source_providers
except Exception:
    inkdrop_source_providers = None

try:
    import inkdrop_candidate_matching
except Exception:
    inkdrop_candidate_matching = None


CONFIG_DIR = inkdrop_runtime_config.config_dir()
STATE_DIR = inkdrop_runtime_config.state_dir()
LOCK_DIR = inkdrop_runtime_config.lock_dir()
LOG_DIR = inkdrop_runtime_config.log_dir()
STAGING_DIR = inkdrop_runtime_config.staging_dir()
MANUAL_INBOX_DIR = inkdrop_runtime_config.manual_inbox_dir()
INKDROP_STATE_DB = STATE_DIR / (inkdrop_state.STATE_DB_NAME if inkdrop_state else "inkdrop-state.sqlite3")
REVIEW_FILE = STATE_DIR / "manual-review.jsonl"
MANUAL_REVIEW_ACTIONS_FILE = STATE_DIR / "manual-review-actions.json"
STATUS_FILE = STATE_DIR / "slskd-source-probe-status.json"
CACHE_FILE = STATE_DIR / "slskd-source-probe-cache.json"
COMIC_SERIES_WATCHES_FILE = STATE_DIR / "comic-series-watches.json"
SERIES_AUTOPILOT_QUEUE_FILE = STATE_DIR / "series-autopilot-queue.json"
LOG_FILE = LOG_DIR / "slskd-source-probe.log"
SLSKD_LEARNING_FILE = STATE_DIR / "slskd-auto-grab-learning.json"
SLSKD_CONFIG = Path(os.environ.get("INKDROP_SLSKD_CONFIG") or CONFIG_DIR / "slskd" / "slskd.yml")
RSS_ALIASES_FILE = STATE_DIR / "rss-aliases.json"
MANGADEX_ALT_TITLE_CACHE_FILE = STATE_DIR / "mangadex-alt-title-cache.json"
MANGADEX_API = "https://api.mangadex.org"
MANGADEX_USER_AGENT = "InkDrop/0.1 (+metadata lookup)"
MANGADEX_ALT_TITLE_CACHE_TTL_SECONDS = 30 * 24 * 3600
EDITION_ALT_CACHE_FILE = STATE_DIR / "edition-conflict-alt-cache.json"
COMICVINE_API = "https://comicvine.gamespot.com/api"
COMICVINE_USER_AGENT = "InkDrop/0.1 (+metadata lookup)"
EDITION_ALT_CACHE_TTL_SECONDS = 90 * 24 * 3600
DEFAULT_SLSKD_BASE_URL = os.environ.get("INKDROP_SLSKD_API_BASE_URL") or ""
SLSKD_BASE_URL = DEFAULT_SLSKD_BASE_URL
MARK_WAITING_API_URL = os.environ.get("INKDROP_MARK_WAITING_API_URL") or (
    f"{inkdrop_runtime_config.worker_web_base_url()}"
    "/api/manual-source/mark-waiting"
)
DEFAULT_SLSKD_DOWNLOAD_ROOT = Path(os.environ.get("INKDROP_SLSKD_DOWNLOAD_ROOT") or STAGING_DIR / "slskd")
SLSKD_DOWNLOAD_ROOT = DEFAULT_SLSKD_DOWNLOAD_ROOT
SLSKD_INCOMPLETE_ROOT = SLSKD_DOWNLOAD_ROOT / "incomplete"
MANUAL_COMICS_INBOX = Path(os.environ.get("INKDROP_MANUAL_COMICS_INBOX") or MANUAL_INBOX_DIR / "comics")


def slskd_config_candidates():
    candidates = []
    explicit = os.environ.get("INKDROP_SLSKD_CONFIG")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(SLSKD_CONFIG)
    candidates.append(CONFIG_DIR / "slskd" / "slskd.yml")
    candidates.append(CONFIG_DIR.parent / "slskd" / "slskd.yml")
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
SLSKD_AUTO_GRAB_STATE_FILE = STATE_DIR / "slskd-auto-grab-state.json"
SERIES_AUTOPILOT_LOCK = LOCK_DIR / "inkdrop-series-autopilot.lock"
SLSKD_AUTO_GRAB_AUDIT_LOG = STATE_DIR / "slskd-auto-grab-audit.jsonl"
SLSKD_PROVIDER_SETTINGS = {"source": "fallback"}
QUALITY_LANGUAGE_RULES = {
    "source": "fallback",
    "preferred_language": "english",
    "pdf_allowed": True,
    "packs_allowed": True,
    "allowed_extensions": {".cbz", ".cbr", ".pdf", ".zip", ".rar", ".7z"},
    "blocked_release_terms": [
        "spanish",
        "espanol",
        "spa",
        "castellano",
        "latino",
        "latam",
        "es-la",
        "es-es",
        "pt-br",
        "portuguese",
        "brasileiro",
        "francais",
        "french",
        "italian",
        "deutsch",
        "german",
    ],
}


def env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return int(default)


SLSKD_QUEUE_ATTEMPT_RECORD_ATTEMPTS = max(1, env_int("INKDROP_SLSKD_QUEUE_ATTEMPT_RECORD_ATTEMPTS", 6))
SLSKD_QUEUE_ATTEMPT_RECORD_INITIAL_DELAY = 0.75
PROBE_SCHEMA_VERSION = 61
QUERY_PLAN_VERSION = 8
QUERY_ROTATION_EVIDENCE_VERSION = 1
CANDIDATE_RECHECK_SECONDS = 20 * 60
CANDIDATE_HEADLINE_SECONDS = 45 * 60
ACTIVE_CACHE_SECONDS = 7 * 86400
TRANSIENT_BAD_CANDIDATE_RETRY_SECONDS = env_int("INKDROP_SLSKD_TRANSIENT_BAD_CANDIDATE_RETRY_SECONDS", 30 * 60)
TRANSIENT_BAD_CANDIDATE_REASONS = {
    "resolver_error",
    "slskd_transfer_failed",
    "slskd_transfer_missing_staged_file",
    "slskd_transfer_stalled",
}
TRANSIENT_AUTO_GRAB_RETRY_SECONDS = env_int("INKDROP_SLSKD_TRANSIENT_AUTO_GRAB_RETRY_SECONDS", 5 * 60)
SLSKD_SLOT_REQUEST_RETRY_SECONDS = env_int("INKDROP_SLSKD_SLOT_REQUEST_RETRY_SECONDS", 3 * 60)
SLSKD_SLOT_REQUEST_TTL_SECONDS = env_int("INKDROP_SLSKD_SLOT_REQUEST_TTL_SECONDS", 15 * 60)
TRANSIENT_AUTO_GRAB_ERROR_PATTERNS = (
    "curl: (22)",
    "error: 429",
    "error: 500",
    "error: 502",
    "error: 503",
    "error: 504",
    "too many requests",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
)


class SLSKDTransferLookupError(RuntimeError):
    pass


class SLSKDProviderUnavailable(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status if isinstance(status, dict) else {}


AUTO_GRAB_HIGH_SCORE = 70
AUTO_GRAB_MEDIUM_SCORE = 55
AUTO_GRAB_MIN_SCORE = AUTO_GRAB_MEDIUM_SCORE
AUTO_GRAB_DIRECT_MATCH_MIN_SCORE = 50
AUTO_GRAB_RETRY_MIN_SCORE = 45
AUTO_GRAB_CLEAR_WIN_DELTA = 10
AUTO_GRAB_CLOSE_SCORE_DELTA = 5
AUTO_GRAB_MIN_BYTES = 5 * 1024 * 1024
AUTO_INSPECT_HARD_MIN_BYTES = 64 * 1024
DEFAULT_SLSKD_PREFERRED_EXACT_MIN_BYTES = max(
    256 * 1024,
    min(env_int("INKDROP_SLSKD_PREFERRED_EXACT_MIN_BYTES", 3 * 1024 * 1024), 64 * 1024 * 1024),
)
SLSKD_PREFERRED_EXACT_MIN_BYTES = DEFAULT_SLSKD_PREFERRED_EXACT_MIN_BYTES
AUTO_INSPECT_USER_MESSAGE = "Exact issue match, but the file is smaller than expected. Sent to inspection."
AUTO_GRAB_MAX_BYTES = 2 * 1024 * 1024 * 1024
AUTO_GRAB_PACK_MAX_BYTES = 5 * 1024 * 1024 * 1024
AUTO_GRAB_MAX_ATTEMPTS_PER_REVIEW = 12
AUTO_GRAB_MAX_RECOVERY_ATTEMPTS_PER_REVIEW = max(
    AUTO_GRAB_MAX_ATTEMPTS_PER_REVIEW,
    env_int("INKDROP_SLSKD_MAX_RECOVERY_ATTEMPTS_PER_REVIEW", 24),
)
AUTO_GRAB_MAX_ATTEMPTS_PER_CANDIDATE = 1
AUTO_GRAB_CANDIDATE_LIMIT = 25
AUTO_GRAB_MAX_ACTIVE_PER_USER = max(1, min(env_int("INKDROP_SLSKD_MAX_ACTIVE_PER_USER", 4), 8))
SERIES_RUN_MAX_ISSUES = max(1, min(env_int("INKDROP_SLSKD_SERIES_RUN_MAX_ISSUES", 8), 25))
SERIES_RUN_MAX_BYTES = max(
    50 * 1024 * 1024,
    min(env_int("INKDROP_SLSKD_SERIES_RUN_MAX_BYTES", 1024 * 1024 * 1024), 10 * 1024 * 1024 * 1024),
)
SERIES_RUN_MAX_OBSERVED_FILES = max(16, min(env_int("INKDROP_SLSKD_SERIES_RUN_MAX_OBSERVED_FILES", 160), 500))
# A directory that alone already proves it holds the bulk of a series' open
# run gets its own, larger, dedicated ceiling instead of trickling through the
# paced SERIES_RUN_MAX_ISSUES/SERIES_RUN_MAX_BYTES budget above -- see
# apply_series_pack_complete_opportunities.
SERIES_PACK_COMPLETE_MIN_COVERAGE = max(2, min(env_int("INKDROP_SLSKD_SERIES_PACK_COMPLETE_MIN_COVERAGE", 3), 200))
SERIES_PACK_COMPLETE_MIN_RATIO_PCT = max(10, min(env_int("INKDROP_SLSKD_SERIES_PACK_COMPLETE_MIN_RATIO_PCT", 75), 100))
SERIES_PACK_COMPLETE_MAX_DIRECTORIES = max(1, min(env_int("INKDROP_SLSKD_SERIES_PACK_COMPLETE_MAX_DIRECTORIES", 3), 25))
DEFAULT_PROBE_BUDGET_SECONDS = 5 * 60
STAGED_SCAN_MAX_SECONDS = 8
STAGED_FILE_SCAN_CACHE = {}
SERIES_RUN_EPHEMERAL_CANDIDATES = {}
ISSUE_METADATA_CACHE = None


def item_issue_token(item):
    match = re.search(r"\d+(?:\.\d+)?", str((item or {}).get("issue") or ""))
    if not match:
        return ""
    token = match.group(0)
    return token.rstrip("0").rstrip(".") if "." in token else str(int(token))


def strip_item_issue_suffix(value, issue):
    text = display_clean(value)
    token = str(issue or "").strip()
    if not text or not token:
        return text
    try:
        number = int(float(token))
    except (TypeError, ValueError):
        return text
    issue_forms = [
        str(number),
        f"{number:02d}",
        f"{number:03d}",
        f"#{number}",
        f"#{number:02d}",
        f"#{number:03d}",
        f"v{number}",
        f"v{number:02d}",
        f"v{number:03d}",
        f"vol {number}",
        f"vol {number:02d}",
        f"vol {number:03d}",
        f"volume {number}",
        f"volume {number:02d}",
        f"volume {number:03d}",
        f"chapter {number}",
        f"chapter {number:03d}",
        f"ch {number}",
        f"ch {number:03d}",
    ]
    pattern = "|".join(re.escape(form) for form in sorted(set(issue_forms), key=len, reverse=True))
    if not pattern:
        return text
    stripped = re.sub(
        rf"\s+(?:{pattern})(?:\s+(?:19|20)\d{{2}})?\s*$",
        "",
        text,
        flags=re.I,
    )
    return display_clean(stripped) or text


def item_series_title(item):
    item = item if isinstance(item, dict) else {}
    explicit = item.get("series") or item.get("series_title") or item.get("seriesTitle")
    if explicit:
        return display_clean(explicit)
    issue = item_issue_token(item)
    for key in ("title", "query", "name"):
        value = str(item.get(key) or "").strip()
        if not value:
            continue
        cleaned = strip_item_issue_suffix(value, issue)
        if cleaned:
            return cleaned
    return ""
SLSKD_LEARNING_CACHE = None
IDENTITY_CONTEXT_CACHE = None
SLSKD_API_KEY_CACHE = None
SLSKD_SERVER_STATUS_CACHE = {"ts": 0, "status": None}

SOURCE_REASONS = {
    "no_safe_source",
    "no_exact_result",
    "no_safe_alternate_found",
    "prowlarr_search_error",
    "manga_no_safe_result",
}


def truthy_env(name):
    value = str(os.environ.get(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def provider_config(provider_id):
    if inkdrop_state is None:
        return None
    try:
        return inkdrop_state.provider_config(INKDROP_STATE_DB, provider_id)
    except Exception:
        return None


def inkdrop_app_setting(key):
    if inkdrop_state is None:
        return None
    try:
        return inkdrop_state.app_setting(INKDROP_STATE_DB, key) or {}
    except Exception:
        return None


def inkdrop_app_setting_value(key, default=None):
    setting = inkdrop_app_setting(key) or {}
    return setting.get("value", default)


def inkdrop_user_app_setting_value(key, default=None):
    setting = inkdrop_app_setting(key) or {}
    if setting.get("source") != "user":
        return default
    return setting.get("value", default)


def inkdrop_quality_app_settings():
    return {
        "preferred_language": inkdrop_user_app_setting_value("quality.preferred_language", None),
        "allow_non_english": inkdrop_user_app_setting_value("quality.allow_non_english", None),
        "allow_pdfs": inkdrop_user_app_setting_value("quality.allow_pdfs", None),
        "allow_packs": inkdrop_user_app_setting_value("quality.allow_packs", None),
        "preferred_extensions": inkdrop_user_app_setting_value("quality.preferred_extensions", []) or [],
        "blocked_release_terms": inkdrop_user_app_setting_value("quality.blocked_release_terms", []) or [],
    }


def normalized_slskd_base_url(value):
    base = str(value or DEFAULT_SLSKD_BASE_URL).strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    if not base.endswith("/api/v0"):
        base = base.rstrip("/") + "/api/v0"
    return base


def int_setting(settings, key, default, minimum=None, maximum=None):
    try:
        value = int(settings.get(key, default))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def float_setting(settings, key, default, minimum=None, maximum=None):
    try:
        value = float(settings.get(key, default))
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def bool_setting(settings, key, default):
    if not isinstance(settings, dict):
        return bool(default)
    value = settings.get(key)
    return boolish_value(value, default)


def boolish_value(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_extension(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text if text.startswith(".") else f".{text}"


def normalized_extensions(values):
    out = set()
    for value in values or []:
        ext = normalize_extension(value)
        if ext:
            out.add(ext)
    return out


def load_quality_language_rules():
    config = provider_config("quality_language_rules") or {}
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    rules = {
        "source": config.get("source") or "fallback",
        "preferred_language": str(settings.get("preferred_language") or "english").strip().lower() or "english",
        "pdf_allowed": bool_setting(settings, "pdf_allowed", True),
        "packs_allowed": bool_setting(settings, "packs_allowed", True),
        "allowed_extensions": normalized_extensions(settings.get("allowed_manual_extensions") or [".cbz", ".cbr", ".pdf"]),
        "blocked_release_terms": list(QUALITY_LANGUAGE_RULES.get("blocked_release_terms") or []),
    }
    if bool_setting(settings, "allow_non_english", False):
        rules["preferred_language"] = "any"
    rules["allowed_extensions"].update({".cbz", ".cbr"})
    if rules["pdf_allowed"]:
        rules["allowed_extensions"].add(".pdf")
    else:
        rules["allowed_extensions"].discard(".pdf")
    if rules["packs_allowed"]:
        rules["allowed_extensions"].update(normalized_extensions(settings.get("pack_archives") or [".zip", ".rar", ".7z"]))
    blocked = settings.get("blocked_release_terms") or []
    if blocked:
        rules["blocked_release_terms"] = [str(term).strip().lower() for term in blocked if str(term or "").strip()]
    app_settings = inkdrop_quality_app_settings()
    preferred = app_settings.get("preferred_language")
    if preferred not in (None, ""):
        rules["preferred_language"] = str(preferred).strip().lower() or rules["preferred_language"]
        rules["source"] = "inkdrop_app_settings"
    if app_settings.get("allow_non_english") is not None:
        if boolish_value(app_settings.get("allow_non_english"), False):
            rules["preferred_language"] = "any"
        elif str(rules.get("preferred_language") or "").strip().lower() == "any":
            rules["preferred_language"] = str(preferred or "english").strip().lower() or "english"
        rules["source"] = "inkdrop_app_settings"
    if app_settings.get("allow_pdfs") is not None:
        rules["pdf_allowed"] = boolish_value(app_settings.get("allow_pdfs"), rules["pdf_allowed"])
        rules["source"] = "inkdrop_app_settings"
    if app_settings.get("allow_packs") is not None:
        rules["packs_allowed"] = boolish_value(app_settings.get("allow_packs"), rules["packs_allowed"])
        rules["source"] = "inkdrop_app_settings"
    preferred_extensions = normalized_extensions(app_settings.get("preferred_extensions") or [])
    if preferred_extensions:
        rules["allowed_extensions"] = preferred_extensions | {".cbz", ".cbr"}
        rules["source"] = "inkdrop_app_settings"
    if rules["pdf_allowed"]:
        rules["allowed_extensions"].add(".pdf")
    else:
        rules["allowed_extensions"].discard(".pdf")
    if rules["packs_allowed"]:
        rules["allowed_extensions"].update({".zip", ".rar", ".7z"})
    else:
        rules["allowed_extensions"].difference_update({".zip", ".rar", ".7z"})
    blocked_terms = app_settings.get("blocked_release_terms") or []
    if blocked_terms:
        rules["blocked_release_terms"] = [str(term).strip().lower() for term in blocked_terms if str(term or "").strip()]
    return rules


def load_slskd_provider_settings():
    routed = inkdrop_download_client_routing.slskd_source_instance(INKDROP_STATE_DB, "comics")
    if routed:
        instance = routed.get("instance") or {}
        runtime = instance.get("settings") if isinstance(instance.get("settings"), dict) else {}
        paths = instance.get("download_paths") if isinstance(instance.get("download_paths"), dict) else {}
        download_root = str(paths.get("comics") or instance.get("download_path") or DEFAULT_SLSKD_DOWNLOAD_ROOT)
        return {
            "source": "download_client_instance",
            "download_client_instance_id": routed["download_client_instance_id"],
            "base_url": normalized_slskd_base_url(instance.get("base_url")),
            "download_root": download_root,
            "incomplete_root": str(runtime.get("incomplete_root") or Path(download_root) / "incomplete"),
            "max_total": int_setting(runtime, "max_total", 12, 0, 50),
            "max_per_series": int_setting(runtime, "max_per_series", 3, 1, 20),
            "wait_seconds": int_setting(runtime, "wait_seconds", 8, 2, 60),
            "max_queries": int_setting(runtime, "max_queries", 2, 0, 5),
            "probe_budget_seconds": int_setting(runtime, "probe_budget_seconds", DEFAULT_PROBE_BUDGET_SECONDS, 30, 15 * 60),
            "cooldown_hours": float_setting(runtime, "cooldown_hours", 24, 0, 24 * 30),
            "auto_grab_max": int_setting(runtime, "auto_grab_max", 6, 0, 10),
            "preferred_exact_min_bytes": int_setting(
                runtime,
                "preferred_exact_min_bytes",
                DEFAULT_SLSKD_PREFERRED_EXACT_MIN_BYTES,
                256 * 1024,
                64 * 1024 * 1024,
            ),
            "max_active_per_user": int_setting(runtime, "max_active_per_user", AUTO_GRAB_MAX_ACTIVE_PER_USER, 1, 8),
            "series_run_max_issues": int_setting(runtime, "series_run_max_issues", SERIES_RUN_MAX_ISSUES, 1, 25),
            "series_run_max_bytes": int_setting(runtime, "series_run_max_bytes", SERIES_RUN_MAX_BYTES, 50 * 1024 * 1024, 10 * 1024 * 1024 * 1024),
            "series_run_max_observed_files": int_setting(runtime, "series_run_max_observed_files", SERIES_RUN_MAX_OBSERVED_FILES, 16, 500),
        }
    config = provider_config("slskd") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("SLSKD provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    base_url = normalized_slskd_base_url(config.get("base_url") or settings.get("base_url") or DEFAULT_SLSKD_BASE_URL)
    if not base_url:
        raise RuntimeError("SLSKD API base URL is not configured; set INKDROP_SLSKD_API_BASE_URL or the SLSKD provider base_url setting.")
    return {
        "source": config.get("source") or "fallback",
        "base_url": base_url,
        "download_root": str(settings.get("download_root") or DEFAULT_SLSKD_DOWNLOAD_ROOT),
        "incomplete_root": str(settings.get("incomplete_root") or Path(settings.get("download_root") or DEFAULT_SLSKD_DOWNLOAD_ROOT) / "incomplete"),
        "max_total": int_setting(settings, "max_total", 12, 0, 50),
        "max_per_series": int_setting(settings, "max_per_series", 3, 1, 20),
        "wait_seconds": int_setting(settings, "wait_seconds", 8, 2, 30),
        "max_queries": int_setting(settings, "max_queries", 2, 0, 5),
        "probe_budget_seconds": int_setting(settings, "probe_budget_seconds", DEFAULT_PROBE_BUDGET_SECONDS, 30, 15 * 60),
        "cooldown_hours": float_setting(settings, "cooldown_hours", 24, 0, 24 * 30),
        "auto_grab_max": int_setting(settings, "auto_grab_max", 6, 0, 10),
        "preferred_exact_min_bytes": int_setting(
            settings,
            "preferred_exact_min_bytes",
            DEFAULT_SLSKD_PREFERRED_EXACT_MIN_BYTES,
            256 * 1024,
            64 * 1024 * 1024,
        ),
        "max_active_per_user": int_setting(settings, "max_active_per_user", AUTO_GRAB_MAX_ACTIVE_PER_USER, 1, 8),
        "series_run_max_issues": int_setting(settings, "series_run_max_issues", SERIES_RUN_MAX_ISSUES, 1, 25),
        "series_run_max_bytes": int_setting(settings, "series_run_max_bytes", SERIES_RUN_MAX_BYTES, 50 * 1024 * 1024, 10 * 1024 * 1024 * 1024),
        "series_run_max_observed_files": int_setting(settings, "series_run_max_observed_files", SERIES_RUN_MAX_OBSERVED_FILES, 16, 500),
    }


def apply_slskd_provider_settings():
    global SLSKD_BASE_URL, SLSKD_DOWNLOAD_ROOT, SLSKD_INCOMPLETE_ROOT, AUTO_GRAB_MAX_ACTIVE_PER_USER
    global SLSKD_PREFERRED_EXACT_MIN_BYTES
    global SERIES_RUN_MAX_ISSUES, SERIES_RUN_MAX_BYTES, SERIES_RUN_MAX_OBSERVED_FILES, SLSKD_PROVIDER_SETTINGS
    settings = load_slskd_provider_settings()
    SLSKD_BASE_URL = settings["base_url"]
    SLSKD_DOWNLOAD_ROOT = Path(settings["download_root"])
    SLSKD_INCOMPLETE_ROOT = Path(settings["incomplete_root"])
    AUTO_GRAB_MAX_ACTIVE_PER_USER = int(settings["max_active_per_user"])
    SLSKD_PREFERRED_EXACT_MIN_BYTES = int(settings["preferred_exact_min_bytes"])
    SERIES_RUN_MAX_ISSUES = int(settings["series_run_max_issues"])
    SERIES_RUN_MAX_BYTES = int(settings["series_run_max_bytes"])
    SERIES_RUN_MAX_OBSERVED_FILES = int(settings["series_run_max_observed_files"])
    SLSKD_PROVIDER_SETTINGS = settings
    return settings


def refresh_slskd_preferred_exact_size_setting():
    """Refresh the bounded size preference without requiring provider secrets."""

    global SLSKD_PREFERRED_EXACT_MIN_BYTES
    runtime = {}
    try:
        routed = inkdrop_download_client_routing.slskd_source_instance(INKDROP_STATE_DB, "comics")
        if routed:
            instance = routed.get("instance") or {}
            runtime = instance.get("settings") if isinstance(instance.get("settings"), dict) else {}
        else:
            config = provider_config("slskd") or {}
            runtime = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    except Exception:
        runtime = {}
    SLSKD_PREFERRED_EXACT_MIN_BYTES = int_setting(
        runtime,
        "preferred_exact_min_bytes",
        DEFAULT_SLSKD_PREFERRED_EXACT_MIN_BYTES,
        256 * 1024,
        64 * 1024 * 1024,
    )
    return SLSKD_PREFERRED_EXACT_MIN_BYTES


COMIC_EXTENSIONS = {".cbz", ".cbr", ".pdf", ".zip", ".rar", ".7z"}
AUTO_GRAB_EXTENSIONS = {".cbz", ".cbr", ".pdf"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
AUTO_GRAB_EXACT_ARCHIVE_EXTENSIONS = {".zip"}


def apply_quality_language_rules():
    global QUALITY_LANGUAGE_RULES, COMIC_EXTENSIONS, AUTO_GRAB_EXTENSIONS, ARCHIVE_EXTENSIONS, AUTO_GRAB_EXACT_ARCHIVE_EXTENSIONS
    rules = load_quality_language_rules()
    pack_exts = {".zip", ".rar", ".7z"} if rules.get("packs_allowed", True) else set()
    single_exts = {".cbz", ".cbr"}
    if rules.get("pdf_allowed", True):
        single_exts.add(".pdf")
    allowed_exts = set(rules.get("allowed_extensions") or set()) | single_exts | pack_exts
    if not rules.get("pdf_allowed", True):
        allowed_exts.discard(".pdf")
    COMIC_EXTENSIONS = allowed_exts
    AUTO_GRAB_EXTENSIONS = single_exts
    ARCHIVE_EXTENSIONS = pack_exts
    AUTO_GRAB_EXACT_ARCHIVE_EXTENSIONS = {".zip"} if rules.get("packs_allowed", True) else set()
    rules["allowed_extensions"] = allowed_exts
    QUALITY_LANGUAGE_RULES = rules
    return rules


SUPPLEMENTAL_AUTOPICK_PHRASES = {
    "adventurer s bible",
    "art book",
    "artbook",
    "bonus material",
    "character book",
    "companion",
    "cover gallery",
    "covers only",
    "encyclopedia",
    "extras",
    "fan book",
    "fanbook",
    "guide book",
    "guidebook",
    "official guide",
    "poster book",
    "preview",
    "sampler",
    "sample",
    "sketch book",
    "sketchbook",
    "wallpaper",
    "world guide",
}
COMIC_CONTEXT_WORDS = {
    "book",
    "books",
    "cbz",
    "cbr",
    "comic",
    "comics",
    "graphic",
    "manga",
    "manhwa",
    "manhua",
    "scan",
    "scans",
}
GENERIC_COMIC_CONTEXT_WORDS = {
    "book",
    "books",
}
NON_COMIC_CONTEXT_WORDS = {
    "adapter",
    "app",
    "apps",
    "atari",
    "audio",
    "crack",
    "cracks",
    "dos",
    "exe",
    "flac",
    "game",
    "games",
    "gamebook",
    "gamebooks",
    "genesis",
    "installer",
    "linux",
    "lossless",
    "macos",
    "mp3",
    "music",
    "nintendo",
    "osx",
    "ost",
    "episode",
    "episodes",
    "plugin",
    "plugins",
    "portable",
    "program",
    "rom",
    "roms",
    "rpg",
    "sega",
    "setup",
    "software",
    "soundtrack",
    "sub",
    "subs",
    "subtitle",
    "subtitles",
    "tabletop",
    "timed",
    "ttrpg",
    "vst",
    "video",
    "webrip",
    "win32",
    "win64",
    "windows",
    "x64",
    "x86",
}
NON_ENGLISH_LANGUAGE_MARKERS = {
    "chinese",
    "deutsch",
    "dutch",
    "es-la",
    "es-es",
    "espanol",
    "español",
    "francais",
    "français",
    "french",
    "german",
    "italian",
    "japanese",
    "jpn",
    "korean",
    "nederlands",
    "portugues",
    "português",
    "pt-br",
    "raw",
    "raws",
    "spa",
    "spanish",
}
NON_ENGLISH_COLLECTION_MARKERS = {
    "bd",
    "bds",
    "fumetti",
    "historieta",
    "historietas",
    "occidentali",
    "quadrinho",
    "quadrinhos",
}
NON_ENGLISH_ARTICLES = {
    "das",
    "de",
    "del",
    "den",
    "der",
    "des",
    "die",
    "du",
    "een",
    "el",
    "het",
    "la",
    "las",
    "le",
    "les",
    "los",
    "un",
    "une",
}
ENGLISH_RELEASE_MARKERS = {
    "dcp",
    "digital",
    "empire",
    "english",
    "getcomics",
    "lucaz",
    "minutemen",
    "nem",
    "ultron",
    "us",
    "usa",
    "zone",
}
WESTERN_COMIC_PUBLISHER_PHRASES = {
    "2000 ad",
    "ablaze",
    "aftershock",
    "archie",
    "awa",
    "black mask",
    "boom",
    "dark horse",
    "dc",
    "dc comics",
    "dynamite",
    "idw",
    "image",
    "image comics",
    "mad cave",
    "marvel",
    "oni",
    "rebellion",
    "scout",
    "titan",
    "top shelf",
    "valiant",
    "vault",
    "vertigo",
}
MANGA_PUBLISHER_PHRASES = {
    "denpa",
    "j novel",
    "kodansha",
    "one peace",
    "seven seas",
    "shogakukan",
    "shonen jump",
    "shueisha",
    "square enix",
    "tokyopop",
    "vertical",
    "viz",
    "yen",
    "yen press",
}
EXPLICIT_ENGLISH_TRANSLATION_MARKERS = {
    "eng",
    "english",
    "scanlation",
    "scanlations",
    "translated",
    "translation",
}
STOP_WORDS = {
    "and",
    "are",
    "book",
    "comic",
    "comics",
    "complete",
    "digital",
    "edition",
    "issue",
    "library",
    "manga",
    "omnibus",
    "part",
    "tpb",
    "the",
    "vol",
    "volume",
}
EDITION_PHRASES = [
    "library edition",
    "deluxe edition",
    "complete edition",
    "complete collection",
    "hardcover",
    "paperback",
    "omnibus",
    "tpb",
]
NUMBER_WORDS = {
    0: "zero",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
    20: "twenty",
}
ORDINAL_NUMBER_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
}
NUMBER_WORD_VALUES = {word: number for number, word in NUMBER_WORDS.items()}
NUMBER_WORD_VALUES.update(ORDINAL_NUMBER_WORDS)
NUMBER_TOKEN_PATTERN = r"(?:\d{1,4}|" + "|".join(sorted((re.escape(word) for word in NUMBER_WORD_VALUES), key=len, reverse=True)) + r")"


def now():
    return time.time()


def utc_stamp(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or now()))


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def normalize_key(value):
    return normalize(value)


def display_clean(value):
    return re.sub(r"\s+", " ", str(value or "").replace(":", " ").replace(" - ", " ")).strip()


def without_edition_phrases(value):
    out = str(value or "")
    for phrase in EDITION_PHRASES:
        out = re.sub(rf"\b{re.escape(phrase)}\b", " ", out, flags=re.I)
    out = re.sub(r"\b(v|vol|volume)\s*\d+(?:\.\d+)?\b", " ", out, flags=re.I)
    return display_clean(out)


def without_parenthetical_identity(value):
    return display_clean(re.sub(r"\s*[\[(][^)\]]*[\])]", " ", str(value or "")))


def without_branding_prefix(value):
    return display_clean(re.sub(r"^\s*(?:nickelodeon)\s+", " ", str(value or ""), flags=re.I))


def unique_values(values, limit=None):
    out = []
    seen = set()
    for value in values:
        value = display_clean(value)
        key = normalize(value)
        if not value or not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
        if limit and len(out) >= limit:
            break
    return out


def stylized_x_title_variants(title):
    text = display_clean(title)
    if not text:
        return []
    variants = [text]
    if re.search(r"(?i)\b[a-z0-9]+\s+x\s+[a-z0-9]+\b", text):
        variants.append(re.sub(r"(?i)\s+x\s+", " x ", text))
        variants.append(re.sub(r"(?i)\s+x\s+", " X ", text))
        variants.append(re.sub(r"(?i)\s+x\s+", "×", text))
    if "×" in text:
        variants.append(re.sub(r"\s*×\s*", "×", text))
        variants.append(re.sub(r"\s*×\s*", " x ", text))
        variants.append(re.sub(r"\s*×\s*", " X ", text))
    return unique_values(variants)


def number_word_for_token(value):
    try:
        number = int(str(value or "").strip())
    except (TypeError, ValueError):
        return ""
    return NUMBER_WORDS.get(number, "")


ROMAN_NUMERAL_STEPS = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)


def roman_numeral_for_number(number):
    """Omnibus/collected-volume titles occasionally use Roman numerals
    ("Volume IV") instead of Arabic digits; only 1-49 are realistic for a
    collected-edition volume count, but the conversion works for any
    positive int under 4000."""
    try:
        number = int(number)
    except (TypeError, ValueError):
        return ""
    if not 0 < number < 4000:
        return ""
    parts = []
    remaining = number
    for value, symbol in ROMAN_NUMERAL_STEPS:
        count, remaining = divmod(remaining, value)
        parts.append(symbol * count)
    return "".join(parts)


def numeric_word_title_variants(value):
    text = str(value or "")
    variants = []
    if not re.search(r"\d", text):
        return variants

    def replace_number(match):
        word = number_word_for_token(match.group(0))
        return word.capitalize() if word else match.group(0)

    worded = re.sub(r"\b\d{1,2}\b", replace_number, text)
    worded = display_clean(worded)
    if worded and normalize(worded) != normalize(text):
        variants.append(worded)
        if not normalize(worded).startswith("the "):
            variants.append(f"The {worded}")
    return variants


def review_id_for(item):
    raw = "|".join(
        str(value or "").lower()
        for value in (
            item.get("reason"),
            item.get("series"),
            item.get("issue"),
            item.get("autopilot_queue_key"),
            item.get("queue_identity"),
            item.get("comicvine_id"),
            item.get("query"),
            (item.get("candidate") or {}).get("title"),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def issue_number_keys(value):
    text = str(value or "").strip()
    keys = {normalize(text)}
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        raw = match.group(0)
        keys.add(normalize(raw))
        try:
            number = int(float(raw))
            keys.add(str(number))
            keys.add(f"{number:03d}")
        except ValueError:
            pass
    keys.discard("")
    return keys


def identity_values_for_item(item):
    if not isinstance(item, dict):
        return []
    values = []
    queue_identity = str(item.get("queue_identity") or "").strip()
    if queue_identity:
        values.append(queue_identity)
    for field in ("comicvine_id", "comicvineId"):
        value = item.get(field)
        if value not in (None, ""):
            values.append(f"comicvine:{value}")
    for field in ("watch_id", "id", "watchId"):
        value = item.get(field)
        if value not in (None, ""):
            values.append(f"watch:{value}")
    return unique_values(values, limit=10)


def issue_metadata_index():
    global ISSUE_METADATA_CACHE
    if ISSUE_METADATA_CACHE is not None:
        return ISSUE_METADATA_CACHE
    data = read_json(COMIC_SERIES_WATCHES_FILE, {}) or {}
    watches = data.get("watches") if isinstance(data, dict) else []
    index = {}
    for watch in watches or []:
        if not isinstance(watch, dict):
            continue
        series_names = [
            watch.get("name"),
            watch.get("title"),
            watch.get("series"),
            watch.get("query"),
        ]
        series_keys = {
            normalize(name)
            for name in series_names
            if name
        } | {
            normalize(without_edition_phrases(name))
            for name in series_names
            if name
        }
        series_keys.discard("")
        identity_values = identity_values_for_item(watch)
        queue_identity = identity_values[0] if identity_values else ""
        issues = []
        known = watch.get("knownIssues")
        if isinstance(known, dict):
            issues.extend(row for row in known.values() if isinstance(row, dict))
        for key in ("missingIssues", "issues", "newIssues"):
            rows = watch.get(key)
            if isinstance(rows, list):
                issues.extend(row for row in rows if isinstance(row, dict))
        for issue in issues:
            number = issue.get("issueNumber") or issue.get("issue") or issue.get("number")
            number_keys = issue_number_keys(number)
            if not number_keys:
                continue
            record = {
                "title": display_clean(issue.get("title") or ""),
                "date": issue.get("date") or "",
                "search_query": issue.get("searchQuery") or "",
                "series": watch.get("name") or watch.get("title") or "",
                "year": watch.get("year") or "",
                "publisher": watch.get("publisher") or "",
                "watch_id": watch.get("id") or "",
                "comicvine_id": watch.get("comicvineId") or "",
                "queue_identity": queue_identity,
            }
            if not any(record.values()):
                continue
            for series_key in series_keys:
                for number_key in number_keys:
                    for identity in identity_values:
                        index[(series_key, number_key, identity)] = record
                    existing = index.get((series_key, number_key), "__missing__")
                    if existing == "__missing__":
                        index[(series_key, number_key)] = record
                    elif isinstance(existing, dict) and existing.get("queue_identity") != record.get("queue_identity"):
                        index[(series_key, number_key)] = None
    ISSUE_METADATA_CACHE = index
    return ISSUE_METADATA_CACHE


def issue_metadata_for_item(item):
    direct_title = clean_issue_title(
        (item or {}).get("issue_title")
        or (item or {}).get("title")
        or (item or {}).get("issueTitle")
        or ""
    )
    direct_record = {
        "title": direct_title,
        "date": (item or {}).get("issue_date") or (item or {}).get("date") or "",
        "search_query": (item or {}).get("search_query") or "",
        "series": item_series_title(item),
        "year": (item or {}).get("year") or (item or {}).get("watch_year") or "",
        "publisher": (item or {}).get("publisher") or (item or {}).get("watch_publisher") or "",
        "watch_id": (item or {}).get("watch_id") or "",
        "comicvine_id": (item or {}).get("comicvine_id") or "",
        "queue_identity": (item or {}).get("queue_identity") or "",
    }
    series_values = [
        item_series_title(item),
        (item or {}).get("query"),
    ]
    number_keys = issue_number_keys((item or {}).get("issue"))
    if not number_keys:
        return {}
    index = issue_metadata_index()
    identities = identity_values_for_item(item)
    for series in series_values:
        keys = {
            normalize(series),
            normalize(without_edition_phrases(series)),
        }
        keys.discard("")
        for series_key in keys:
            for number_key in number_keys:
                for identity in identities:
                    record = index.get((series_key, number_key, identity))
                    if record:
                        merged = dict(record)
                        for key, value in direct_record.items():
                            if value not in (None, ""):
                                merged[key] = value
                        return merged
                record = index.get((series_key, number_key))
                if record is None:
                    continue
                if record:
                    merged = dict(record)
                    for key, value in direct_record.items():
                        if value not in (None, ""):
                            merged[key] = value
                    return merged
    return direct_record if any(direct_record.values()) else {}


def write_json(path, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


RUNNING_STATUS_CLEAR_FIELDS = (
    "available_issue_count",
    "available_review_count",
    "auto_grab",
    "auto_grab_blocked_count",
    "auto_grab_failed_count",
    "auto_grab_policy",
    "auto_grab_review_count",
    "auto_grab_safe_count",
    "candidate_count",
    "candidate_issue_count",
    "checked",
    "items",
    "queue_review_rows",
    "skipped_cooldown",
    "staged_issue_count",
    "staged_review_count",
)


def publish_probe_progress(**payload):
    current = read_json(STATUS_FILE, {}) or {}
    if not isinstance(current, dict):
        current = {}
    for key in RUNNING_STATUS_CLEAR_FIELDS:
        if key not in payload:
            current.pop(key, None)
    current.update({
        "ok": True,
        "state": "running",
        "status": "running",
        "generated_at": now(),
        "generated_at_iso": utc_stamp(),
        "schema_version": PROBE_SCHEMA_VERSION,
        **payload,
    })
    write_json(STATUS_FILE, current)


def log(event, **payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": now(), "event": event, **payload}, sort_keys=True) + "\n")


def sync_inkdrop_queue_state(reason="slskd_queue_update"):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    try:
        return inkdrop_state.sync_queue_state(STATE_DIR, INKDROP_STATE_DB, mode="queue")
    except Exception as exc:
        log("inkdrop_state_queue_sync_failed", reason=reason, error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": str(exc)}


def export_autopilot_queue_from_inkdrop_state(reason="slskd_queue_export", focus_queue_id=""):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    try:
        export = inkdrop_state.export_series_autopilot_queue_json(
            STATE_DIR,
            INKDROP_STATE_DB,
            reason=reason,
            focus_queue_id=focus_queue_id,
            schema_version=2,
        )
    except Exception as exc:
        log("inkdrop_state_queue_export_failed", reason=reason, error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": str(exc)}
    if not export.get("ok"):
        log("inkdrop_state_queue_export_failed", reason=reason, export=export)
        return export
    return export


def slskd_queue_attempt_database_locked(exc):
    if inkdrop_state is not None and hasattr(inkdrop_state, "is_database_locked_error"):
        try:
            return bool(inkdrop_state.is_database_locked_error(exc))
        except Exception:
            pass
    return isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc).lower()


def record_queue_source_attempt_with_lock_retry(queue_id, attempt, attempt_id=None):
    attempts = max(1, int(SLSKD_QUEUE_ATTEMPT_RECORD_ATTEMPTS or 1))
    delay = max(0.1, float(SLSKD_QUEUE_ATTEMPT_RECORD_INITIAL_DELAY or 0.75))
    for attempt_number in range(1, attempts + 1):
        try:
            return inkdrop_state.record_queue_source_attempt(
                INKDROP_STATE_DB,
                queue_id,
                attempt,
                attempt_id=attempt_id,
            )
        except Exception as exc:
            if not slskd_queue_attempt_database_locked(exc) or attempt_number >= attempts:
                raise
            log(
                "slskd_queue_attempt_record_retry",
                queue_id=queue_id,
                status=attempt.get("status"),
                series=attempt.get("series"),
                issue=attempt.get("issue"),
                attempt_number=attempt_number,
                attempts=attempts,
                error=f"{type(exc).__name__}: {exc}",
            )
            time.sleep(delay)
            delay = min(delay * 2, 10.0)


def record_slskd_queue_attempt(entry, candidate, status, reason="", transfer=None, extra=None, attempt_id=None):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    entry = entry if isinstance(entry, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    transfer = transfer if isinstance(transfer, dict) else {}
    queue_id = str(
        entry.get("autopilot_queue_key")
        or entry.get("queue_key")
        or entry.get("key")
        or ""
    ).strip()
    if not queue_id:
        return {"ok": False, "reason": "queue_id_missing"}
    filename = slskd_candidate_filename(candidate, entry)
    public_filename = filename_leaf(filename)
    provider_name = slskd_candidate_provider(candidate)
    source_hash = slskd_candidate_download_url_hash(candidate, entry)
    candidate_identity = slskd_candidate_identity(entry, candidate, source_hash)
    transfer_state = transfer.get("state") or transfer.get("stateDescription")
    attempt = {
        "source": "slskd",
        "provider_id": "slskd",
        "provider": provider_name,
        "protocol": "soulseek",
        "download_client": "SLSKD",
        "download_client_instance_id": SLSKD_PROVIDER_SETTINGS.get("download_client_instance_id"),
        "status": status,
        "reason": reason or transfer_state or status,
        "query": auto_grab_waiting_query(entry),
        "title": public_filename,
        "filename": public_filename,
        "source_path": public_filename,
        "username": provider_name,
        "score": candidate.get("score"),
        "candidate_score": candidate.get("score"),
        "candidate_size": candidate.get("size"),
        "series": entry.get("series") or entry.get("query"),
        "issue": entry.get("issue"),
        "client_id": transfer.get("id"),
        "transfer_id": transfer.get("id"),
        "slskd_transfer_id": transfer.get("id"),
        "slskd_transfer_state": transfer_state,
        "transfer_state": transfer_state,
        "slskd_transfer_requested_at": transfer.get("requestedAt"),
        "transfer_requested_at": transfer.get("requestedAt"),
        "kind": "slskd_auto_grab",
        "retry_scope": "slskd_candidate",
        "ts": now(),
    }
    if source_hash:
        attempt["download_url_hash"] = source_hash
    if candidate_identity:
        attempt["candidate_identity"] = candidate_identity
    for field in ("bytesTransferred", "bytesRemaining", "percentComplete", "averageSpeed", "attempts", "size"):
        if transfer.get(field) is not None:
            attempt[f"slskd_{field}"] = transfer.get(field)
    if extra:
        attempt.update({key: value for key, value in dict(extra).items() if value not in (None, "")})
    if not attempt_id and inkdrop_state.slskd_terminal_recovery_attempt(attempt):
        recovery_identity = "|".join(
            str(value or "").strip().lower()
            for value in (
                queue_id,
                entry.get("review_id"),
                candidate_identity,
                transfer.get("id"),
                status,
            )
        )
        attempt_id = f"slskd-terminal-{hashlib.sha256(recovery_identity.encode('utf-8')).hexdigest()[:24]}"
    if status in (
        inkdrop_state.SLSKD_RESERVATION_ACTIVE_STATUSES
        | inkdrop_state.SLSKD_RESERVATION_TERMINAL_STATUSES
    ):
        transition = inkdrop_state.transition_matching_slskd_candidate_task(
            INKDROP_STATE_DB,
            queue_id,
            candidate_identity,
            status,
            transfer_id=transfer.get("id"),
            reason=reason or transfer_state or status,
            extra=extra,
        )
        if transition.get("ok"):
            transition["attempt_id"] = attempt_id
            return transition
    try:
        result = record_queue_source_attempt_with_lock_retry(queue_id, attempt, attempt_id=attempt_id)
    except Exception as exc:
        log(
            "slskd_queue_attempt_record_failed",
            queue_id=queue_id,
            status=status,
            series=attempt.get("series"),
            issue=attempt.get("issue"),
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "error": str(exc), "queue_id": queue_id}
    return result


def reserve_slskd_candidate(entry, candidate, reason="", *, acquire_claim=False):
    """Bind an accepted candidate to its task before waiting or enqueue."""

    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    entry = entry if isinstance(entry, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    queue_id = str(
        entry.get("autopilot_queue_key")
        or entry.get("queue_key")
        or entry.get("key")
        or ""
    ).strip()
    queue_identity = str(entry.get("queue_identity") or "").strip()
    if entry.get("autopilot_queue") and (not queue_id or not queue_identity):
        return {"ok": False, "reason": "durable_queue_identity_incomplete", "queue_id": queue_id}
    filename = slskd_candidate_filename(candidate, entry)
    public_filename = filename_leaf(filename)
    source_hash = slskd_candidate_download_url_hash(candidate, entry)
    candidate_identity = slskd_candidate_identity(entry, candidate, source_hash)
    candidate_instance = auto_grab_candidate_key(str(entry.get("review_id") or ""), candidate)
    locator_digest = slskd_private_locator_digest(candidate, entry)
    unit_type, unit_number = canonical_retarget_unit(entry)
    claim_owner_id = (
        hashlib.sha256(
            "|".join(("slskd_candidate_enqueue", queue_id, candidate_instance, str(uuid.uuid4()))).encode("utf-8")
        ).hexdigest()[:24]
        if acquire_claim else None
    )
    attempt = {
        "source": "slskd",
        "provider_id": "slskd",
        "provider": "SLSKD",
        "protocol": "soulseek",
        "download_client": "SLSKD",
        "download_client_instance_id": SLSKD_PROVIDER_SETTINGS.get("download_client_instance_id"),
        "status": "waiting_for_slot",
        "lifecycle_phase": "provider_wait",
        "reason": reason or "waiting for an SLSKD transfer slot",
        "query": auto_grab_waiting_query(entry),
        "title": public_filename,
        "filename": public_filename,
        "source_path": public_filename,
        "score": candidate.get("score"),
        "candidate_score": candidate.get("score"),
        "candidate_size": candidate.get("size"),
        "candidate_identity": candidate_identity,
        "candidate_instance_identity": candidate_instance,
        "candidate_locator_digest": locator_digest,
        "candidate_safe": bool((candidate.get("auto_grab") or {}).get("verdict") == "auto_grab_safe"),
        "accepted_candidate_binding": True,
        "queue_identity": queue_identity,
        "unit_type": unit_type,
        f"{unit_type}_number" if unit_type in {"issue", "chapter", "volume"} else "issue_number": unit_number,
        "issue_number": unit_number if unit_type == "issue" else entry.get("issue_number"),
        "series": entry.get("series") or entry.get("query"),
        "issue": entry.get("issue"),
        "kind": "slskd_auto_grab",
        "retry_scope": "slskd_candidate",
        "retry_eligible": True,
        "ts": now(),
    }
    if source_hash:
        attempt["download_url_hash"] = source_hash
    try:
        return inkdrop_state.reserve_slskd_candidate(
            INKDROP_STATE_DB,
            queue_id,
            attempt,
            requested_at=attempt["ts"],
            retry_seconds=SLSKD_SLOT_REQUEST_RETRY_SECONDS,
            ttl_seconds=SLSKD_SLOT_REQUEST_TTL_SECONDS,
            claim_owner_id=claim_owner_id,
        )
    except Exception as exc:
        log(
            "slskd_slot_request_record_failed",
            queue_id=queue_id,
            series=attempt.get("series"),
            issue=attempt.get("issue"),
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "reason": "slot_request_persist_failed", "queue_id": queue_id}


def record_slskd_slot_request(entry, candidate, reason=""):
    return reserve_slskd_candidate(entry, candidate, reason, acquire_claim=False)


SLSKD_AUTOMATIC_HANDOFF_DECISIONS = frozenset({
    "authorize_enqueue",
    "reuse_existing",
    "retryable_rollback",
    "blocked_active_owner",
    "blocked_completion",
    "invalid_binding",
})


def decide_automatic_slskd_handoff(entry, candidate, reason="", *, acquire_claim=True):
    """Return the sole explicit authorization decision for automatic SLSKD handoff."""

    entry = entry if isinstance(entry, dict) else {}
    if not entry.get("autopilot_queue"):
        return {
            "decision": "invalid_binding",
            "reason": "automatic_slskd_handoff_requires_autopilot_queue",
            "ok": False,
        }
    queue_id = str(
        entry.get("autopilot_queue_key") or entry.get("queue_key") or entry.get("key") or ""
    ).strip()
    queue_identity = str(entry.get("queue_identity") or "").strip()
    if not queue_id or not queue_identity:
        return {
            "decision": "invalid_binding",
            "reason": "durable_queue_identity_incomplete",
            "queue_id": queue_id,
            "ok": False,
        }
    reservation = reserve_slskd_candidate(
        entry,
        candidate,
        reason,
        acquire_claim=acquire_claim,
    )
    reservation_reason = str(reservation.get("reason") or "").strip().lower()
    if reservation.get("ok") and reservation.get("created") and reservation_reason == "candidate_reserved":
        decision = "authorize_enqueue"
    elif reservation.get("ok") and reservation.get("idempotent") and reservation_reason == "candidate_reservation_active":
        decision = "reuse_existing"
    elif reservation_reason in {"queue_has_active_candidate_task", "sibling_exact_unit_active"}:
        decision = "blocked_active_owner"
    elif reservation_reason == "candidate_completion_fence":
        decision = "blocked_completion"
    elif reservation_reason == "queue_not_retryable" and str(reservation.get("state") or "").strip().lower() in {
        "verified", "satisfied", "superseded_duplicate", "removed", "ignored", "inactive",
    }:
        decision = "blocked_completion"
    elif reservation.get("expired") or reservation_reason in {
        "slot_request_expired",
        "candidate_reservation_claim_unavailable",
        "slot_request_persist_failed",
    }:
        decision = "retryable_rollback"
    else:
        decision = "invalid_binding"
    result = dict(reservation)
    result["decision"] = decision
    result["authorized"] = decision == "authorize_enqueue"
    return result


def slskd_provider_wait_attempt_id(entry):
    queue_key = str(
        (entry or {}).get("autopilot_queue_key")
        or (entry or {}).get("queue_key")
        or (entry or {}).get("key")
        or ""
    ).strip()
    review_id = str((entry or {}).get("review_id") or "").strip()
    provider_state = str((entry or {}).get("provider_state") or "").strip().lower()
    try:
        checked_at = float((entry or {}).get("checked_at") or now())
    except (TypeError, ValueError):
        checked_at = now()
    bucket = int(checked_at // max(60, TRANSIENT_AUTO_GRAB_RETRY_SECONDS))
    raw = "|".join([queue_key, review_id, provider_state, str(bucket)])
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"slskd-provider-wait-{digest}"


def slskd_provider_wait_status(entry):
    if (entry or {}).get("provider_connected") is False:
        return "provider_unavailable"
    if (entry or {}).get("provider_logged_in") is False or (entry or {}).get("provider_transitioning") is True:
        return "provider_wait"
    return "provider_unavailable"


def slskd_provider_wait_reason(entry):
    state = str((entry or {}).get("provider_state") or "").strip()
    error = str((entry or {}).get("provider_error") or "").strip()
    connected = (entry or {}).get("provider_connected")
    logged_in = (entry or {}).get("provider_logged_in")
    transitioning = (entry or {}).get("provider_transitioning")
    if connected is False:
        return f"SLSKD provider unavailable: Soulseek disconnected{f' ({state})' if state else ''}; retrying automatically"
    if logged_in is False:
        return f"SLSKD provider wait: Soulseek logged out{f' ({state})' if state else ''}; retrying automatically"
    if transitioning is True:
        return f"SLSKD provider wait: Soulseek is logging in{f' ({state})' if state else ''}; retrying automatically"
    if state:
        return f"SLSKD provider unavailable: Soulseek state {state}; retrying automatically"
    if error:
        return f"SLSKD provider unavailable: {error}; retrying automatically"
    return "SLSKD provider unavailable; retrying automatically"


def record_slskd_provider_wait_attempt(entry):
    entry = entry if isinstance(entry, dict) else {}
    if str(entry.get("status") or "").strip().lower() not in {"api_error", "provider_unavailable", "provider_wait"}:
        return {"ok": False, "reason": "not_provider_wait"}
    if not entry.get("autopilot_queue"):
        return {"ok": False, "reason": "not_autopilot_queue"}
    status = slskd_provider_wait_status(entry)
    reason = slskd_provider_wait_reason(entry)
    result = record_slskd_queue_attempt(
        entry,
        {
            "filename": "SLSKD provider health",
            "username": "SLSKD",
        },
        status,
        reason,
        extra={
            "kind": "slskd_queue_check",
            "review_id": entry.get("review_id"),
            "provider_unavailable": True,
            "provider_state": entry.get("provider_state"),
            "provider_connected": entry.get("provider_connected"),
            "provider_logged_in": entry.get("provider_logged_in"),
            "provider_transitioning": entry.get("provider_transitioning"),
            "provider_error": entry.get("provider_error"),
            "retry_after_seconds": TRANSIENT_AUTO_GRAB_RETRY_SECONDS,
            "query_count": len(entry.get("queries") or []),
            "last_slskd_status": status,
        },
        attempt_id=slskd_provider_wait_attempt_id(entry),
    )
    queue_key = str(entry.get("autopilot_queue_key") or entry.get("queue_key") or entry.get("key") or "").strip()
    if isinstance(result, dict) and result.get("ok") and queue_key:
        result["export"] = export_autopilot_queue_from_inkdrop_state("slskd_provider_wait", queue_key)
    return result


def auto_grab_audit(event, **payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe_payload = inkdrop_state.slskd_private_evidence_payload(payload) if inkdrop_state is not None else payload
    with SLSKD_AUTO_GRAB_AUDIT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": now(), "event": event, **safe_payload}, sort_keys=True) + "\n")


def load_auto_grab_state():
    data = read_json(SLSKD_AUTO_GRAB_STATE_FILE, {}) or {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("review_attempts"), dict):
        data["review_attempts"] = {}
    if not isinstance(data.get("candidate_attempts"), dict):
        data["candidate_attempts"] = {}
    if not isinstance(data.get("last_attempts"), dict):
        data["last_attempts"] = {}
    if not isinstance(data.get("candidate_last_attempts"), dict):
        data["candidate_last_attempts"] = {}
    data["schema_version"] = PROBE_SCHEMA_VERSION
    return data


def save_auto_grab_state(state):
    if not isinstance(state, dict):
        state = {}
    state["updated_at"] = now()
    state["schema_version"] = PROBE_SCHEMA_VERSION
    write_json(SLSKD_AUTO_GRAB_STATE_FILE, state)


def acquire_auto_grab_state_lock(blocking=True):
    try:
        SERIES_AUTOPILOT_LOCK.parent.mkdir(parents=True, exist_ok=True)
        handle = SERIES_AUTOPILOT_LOCK.open("a+b")
    except OSError:
        return None
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return handle
    except (OSError, BlockingIOError, PermissionError):
        handle.close()
        return None


def release_auto_grab_state_lock(handle):
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
    finally:
        handle.close()


def commit_auto_grab_state_changes(base_state, run_state):
    """Merge this probe run's local attempt deltas into freshly locked state."""
    handle = acquire_auto_grab_state_lock(blocking=True)
    if handle is None:
        raise RuntimeError("series autopilot lock unavailable for auto-grab state commit")
    try:
        current = load_auto_grab_state()
        base_state = base_state if isinstance(base_state, dict) else {}
        run_state = run_state if isinstance(run_state, dict) else {}
        for field in ("review_attempts", "candidate_attempts"):
            base_counts = base_state.get(field) if isinstance(base_state.get(field), dict) else {}
            run_counts = run_state.get(field) if isinstance(run_state.get(field), dict) else {}
            current_counts = current.setdefault(field, {})
            for key in set(base_counts) | set(run_counts):
                try:
                    delta = int(run_counts.get(key) or 0) - int(base_counts.get(key) or 0)
                    existing = int(current_counts.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if delta:
                    current_counts[key] = max(0, existing + delta)
        for field in ("last_attempts", "candidate_last_attempts"):
            base_records = base_state.get(field) if isinstance(base_state.get(field), dict) else {}
            run_records = run_state.get(field) if isinstance(run_state.get(field), dict) else {}
            current_records = current.setdefault(field, {})
            for key, record in run_records.items():
                if record != base_records.get(key):
                    current_record = current_records.get(key)
                    try:
                        record_ts = float((record or {}).get("ts") or 0)
                        current_ts = float((current_record or {}).get("ts") or 0)
                    except (AttributeError, TypeError, ValueError):
                        record_ts = current_ts = 0
                    if not isinstance(current_record, dict) or record_ts >= current_ts:
                        current_records[key] = record
        save_auto_grab_state(current)
        return current
    finally:
        release_auto_grab_state_lock(handle)


def retire_auto_grab_review_attempts(review_id, evidence_ids, reason=""):
    """Retire resolver-proven events while its shared autopilot lock is held."""
    review_key = str(review_id or "").strip()
    stable_evidence_ids = list(dict.fromkeys(
        str(value or "").strip() for value in (evidence_ids or []) if str(value or "").strip()
    ))
    if not review_key or not stable_evidence_ids:
        return {"ok": False, "retired_count": 0, "reason": "review_or_evidence_missing"}

    state = load_auto_grab_state()
    retired_by_review = state.setdefault("retired_terminal_review_attempt_evidence", {})
    retired = retired_by_review.get(review_key)
    if not isinstance(retired, list):
        retired = []
    retired_set = {str(value) for value in retired if str(value or "").strip()}
    newly_retired = [value for value in stable_evidence_ids if value not in retired_set]
    if not newly_retired:
        return {
            "ok": True,
            "review_id": review_key,
            "retired_count": 0,
            "review_attempts": int((state.get("review_attempts") or {}).get(review_key) or 0),
        }

    attempts = state.setdefault("review_attempts", {})
    try:
        previous_attempts = max(0, int(attempts.get(review_key) or 0))
    except (TypeError, ValueError):
        previous_attempts = 0
    retired_count = min(previous_attempts, len(newly_retired))
    attempts[review_key] = previous_attempts - retired_count
    retired_by_review[review_key] = [*retired, *newly_retired]
    terminal_attempt = {
        "ts": now(),
        "status": "transfer_failed",
        "retryable": True,
        "terminal_false_duplicate_retired": True,
        "error": str(reason or "Terminal false-duplicate handoff retired by authoritative resolver evidence"),
        "retired_evidence_ids": newly_retired,
        "retired_count": retired_count,
    }
    state.setdefault("last_attempts", {})[review_key] = terminal_attempt
    save_auto_grab_state(state)
    return {
        "ok": True,
        "review_id": review_key,
        "retired_count": retired_count,
        "new_evidence_count": len(newly_retired),
        "review_attempts": attempts[review_key],
    }


def auto_grab_candidate_key(review_id, candidate):
    handoff_token = str((candidate or {}).get("series_directory_handoff_token") or "").strip()
    if handoff_token:
        return hashlib.sha256(f"series_handoff|{review_id}|{handoff_token}".encode("utf-8")).hexdigest()[:24]
    if inkdrop_candidate_matching:
        identity_candidate = dict(candidate or {})
        identity_candidate.setdefault("provider_id", "slskd")
        identity_candidate.setdefault("title", slskd_candidate_filename(candidate))
        instance = inkdrop_candidate_matching.stable_candidate_identities(identity_candidate).get(
            "candidate_instance_identity"
        )
        if instance:
            return hashlib.sha256(f"{review_id}|{instance}".encode("utf-8")).hexdigest()[:20]
    raw = "|".join(
        str(value or "").strip().lower()
        for value in (
            review_id,
            (candidate or {}).get("username"),
            (candidate or {}).get("filename") or (candidate or {}).get("path"),
            (candidate or {}).get("size"),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def slskd_candidate_filename(candidate, entry=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    entry = entry if isinstance(entry, dict) else {}
    for key in ("filename", "path", "remote_filename", "detected_path", "detected_filename", "filename_leaf"):
        value = str(candidate.get(key) or "").strip()
        if value:
            return value
    return str(entry.get("filename") or "").strip()


def slskd_candidate_provider(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    return str(
        candidate.get("username")
        or candidate.get("provider")
        or candidate.get("user")
        or "slskd"
    ).strip() or "slskd"


def slskd_candidate_size(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    value = candidate.get("size") or candidate.get("size_bytes") or candidate.get("bytes")
    if value in (None, ""):
        return ""
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value).strip()


def slskd_candidate_download_url_hash(candidate, entry=None):
    handoff_token = str((candidate or {}).get("series_directory_handoff_token") or "").strip()
    if handoff_token:
        return hashlib.sha256(f"slskd-handoff|{handoff_token}".encode("utf-8")).hexdigest()
    provider = slskd_candidate_provider(candidate).lower()
    filename = re.sub(r"/+", "/", slskd_candidate_filename(candidate, entry).replace("\\", "/")).strip("/").casefold()
    # Automatic task identity and failed-candidate memory must survive SLSKD
    # switching between a directory-qualified result and a basename transfer.
    filename = filename.rsplit("/", 1)[-1]
    size = slskd_candidate_size(candidate)
    if not any((provider, filename, size)):
        return ""
    raw = "|".join(("slskd", provider, filename, size))
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def slskd_private_locator_digest(candidate, entry=None):
    """Bind an operator handoff to the exact peer, remote path, and size."""
    locator = dict(candidate or {})
    locator.setdefault("filename", slskd_candidate_filename(candidate, entry))
    locator.setdefault("username", slskd_candidate_provider(candidate))
    locator.setdefault("size", slskd_candidate_size(candidate))
    if inkdrop_state is not None:
        return inkdrop_state.slskd_private_locator_digest(locator)
    return ""


def slskd_candidate_identity(entry, candidate, download_url_hash=None):
    entry = entry if isinstance(entry, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    source_hash = str(download_url_hash or slskd_candidate_download_url_hash(candidate, entry) or "").strip()
    handoff_token = str(candidate.get("series_directory_handoff_token") or "").strip()
    if handoff_token:
        raw = "|".join((
            "slskd_series_handoff",
            normalize(entry.get("series") or candidate.get("series") or ""),
            str(entry.get("issue") or candidate.get("issue") or "").strip().lower(),
            handoff_token,
        ))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    provider = slskd_candidate_provider(candidate).lower()
    issue = str(entry.get("issue") or candidate.get("issue") or candidate.get("issue_number") or "").strip().lower()
    series = normalize(entry.get("series") or candidate.get("series") or "")
    if not any((source_hash, provider, issue, series)):
        return ""
    if inkdrop_candidate_matching:
        identity_candidate = dict(candidate)
        identity_candidate.update(
            {
                "provider_id": "slskd",
                "title": slskd_candidate_filename(candidate, entry),
                "series": series,
                "source_issue_number": issue,
                "download_url_hash": source_hash,
            }
        )
        return inkdrop_candidate_matching.stable_candidate_identities(identity_candidate)["candidate_family_identity"]
    raw = "|".join(("slskd_candidate", provider, series, issue, source_hash))
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def auto_grab_candidate_numeric(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def auto_grab_high_confidence_candidate(candidate):
    gate = (candidate or {}).get("auto_grab") if isinstance((candidate or {}).get("auto_grab"), dict) else {}
    if gate.get("verdict") != "auto_grab_safe":
        return False
    score = max(
        auto_grab_candidate_numeric((candidate or {}).get("score")),
        auto_grab_candidate_numeric(gate.get("score")),
        auto_grab_candidate_numeric(gate.get("match_score")),
    )
    rank = auto_grab_candidate_numeric(gate.get("autopick_rank"), 999999)
    return bool(
        score >= AUTO_GRAB_HIGH_SCORE
        or gate.get("direct_match_confidence")
        or rank == 1
    )


def auto_grab_review_attempt_records(state, review_id):
    review_id = str(review_id or "")
    records = []
    for record in (state.get("candidate_last_attempts") or {}).values():
        if not isinstance(record, dict):
            continue
        if str(record.get("review_id") or "") != review_id:
            continue
        records.append(record)
    return records


def auto_grab_review_has_recent_waiting_attempt(state, review_id):
    # A review-level waiting marker is only a duplicate guard while its latest
    # handoff is still active.  Once the latest attempt has been cleared or
    # failed, older started_waiting records must not suppress a new exact
    # candidate forever.
    latest = (state.get("last_attempts") or {}).get(str(review_id or ""))
    latest_ts = 0
    terminal_latest = False
    if isinstance(latest, dict):
        try:
            latest_ts = float(latest.get("ts") or 0)
        except (TypeError, ValueError):
            latest_ts = 0
        latest_status = str(latest.get("status") or "").strip().lower()
        if latest_status in {
            "stale_failed_transfer_cleared",
            "transfer_failed",
            "download_api_error",
            "download_preflight_api_error",
            "waiting_record_missing",
            "error",
        }:
            terminal_latest = True
    for record in auto_grab_review_attempt_records(state, review_id):
        status = str(record.get("status") or "").lower()
        if status not in {"started_waiting", "already_downloading"}:
            continue
        try:
            ts = float(record.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        if terminal_latest and latest_ts and ts <= latest_ts:
            continue
        if ts <= 0 or now() - ts < 24 * 60 * 60:
            return True
    return False


def record_auto_grab_terminal_attempt(review_id, record, status, reason="", *, blocking=True):
    """Record a resolver-observed terminal handoff without creating a job."""
    review_key = str(review_id or "")
    if not review_key:
        return False
    handle = acquire_auto_grab_state_lock(blocking=blocking)
    if handle is None:
        return False
    try:
        state = load_auto_grab_state()
        record = record if isinstance(record, dict) else {}
        candidate_key = str(record.get("candidate_key") or "")
        attempt = {
            "candidate_key": candidate_key,
            "ts": now(),
            "status": str(status or "error"),
            "filename": record.get("filename") or record.get("candidate_filename"),
            "username": record.get("username"),
            "score": record.get("candidate_score") or record.get("score"),
            "error": reason or record.get("error"),
        }
        state["last_attempts"][review_key] = attempt
        if candidate_key:
            candidate_attempt = dict(attempt)
            candidate_attempt["review_id"] = review_key
            state["candidate_last_attempts"][candidate_key] = candidate_attempt
        save_auto_grab_state(state)
        return True
    finally:
        release_auto_grab_state_lock(handle)


def auto_grab_row_attempt_cap_recovery_allowed(state, review_id, candidate, candidate_key, review_attempts, candidate_attempts):
    review_id = str(review_id or "")
    if int(review_attempts or 0) >= AUTO_GRAB_MAX_RECOVERY_ATTEMPTS_PER_REVIEW:
        return False, (
            f"row recovery attempt limit reached "
            f"({review_attempts}/{AUTO_GRAB_MAX_RECOVERY_ATTEMPTS_PER_REVIEW})"
        )
    if int(candidate_attempts or 0) >= AUTO_GRAB_MAX_ATTEMPTS_PER_CANDIDATE:
        return False, ""
    if not auto_grab_high_confidence_candidate(candidate):
        return False, ""
    if review_has_waiting_or_terminal_action(review_id):
        return False, ""
    if bad_candidate_match(review_id, candidate):
        return False, ""
    if auto_grab_review_has_recent_waiting_attempt(state, review_id):
        return False, ""
    return True, (
        f"row attempt limit reached ({review_attempts}/{AUTO_GRAB_MAX_ATTEMPTS_PER_REVIEW}); "
        "trying fresh high-confidence SLSKD candidate"
    )


def auto_grab_attempt_allowed(state, review_id, candidate):
    review_key = str(review_id)
    candidate_key = auto_grab_candidate_key(review_id, candidate)
    try:
        review_attempts = int((state.get("review_attempts") or {}).get(review_key) or 0)
    except (TypeError, ValueError):
        review_attempts = 0
    try:
        candidate_attempts = int((state.get("candidate_attempts") or {}).get(candidate_key) or 0)
    except (TypeError, ValueError):
        candidate_attempts = 0
    if candidate_attempts >= AUTO_GRAB_MAX_ATTEMPTS_PER_CANDIDATE:
        if cleared_watched_attempt_can_retry(state, review_key, candidate, candidate_key, candidate_attempts):
            if review_attempts >= AUTO_GRAB_MAX_ATTEMPTS_PER_REVIEW:
                allowed, reason = auto_grab_row_attempt_cap_recovery_allowed(
                    state, review_key, candidate, candidate_key, review_attempts, 0
                )
                if not allowed:
                    return False, reason or f"row attempt limit reached ({review_attempts}/{AUTO_GRAB_MAX_ATTEMPTS_PER_REVIEW})", candidate_key
            return True, "previous watched attempt cooled down without a terminal result; retrying candidate", candidate_key
        retry_reason = cooled_down_candidate_attempt_retry_reason(state, review_key, candidate, candidate_key)
        if retry_reason:
            if review_attempts >= AUTO_GRAB_MAX_ATTEMPTS_PER_REVIEW:
                allowed, reason = auto_grab_row_attempt_cap_recovery_allowed(
                    state, review_key, candidate, candidate_key, review_attempts, 0
                )
                if not allowed:
                    return False, reason or f"row attempt limit reached ({review_attempts}/{AUTO_GRAB_MAX_ATTEMPTS_PER_REVIEW})", candidate_key
            return True, retry_reason, candidate_key
        return False, f"candidate attempt limit reached ({candidate_attempts}/{AUTO_GRAB_MAX_ATTEMPTS_PER_CANDIDATE})", candidate_key
    if review_attempts >= AUTO_GRAB_MAX_ATTEMPTS_PER_REVIEW:
        allowed, reason = auto_grab_row_attempt_cap_recovery_allowed(
            state, review_key, candidate, candidate_key, review_attempts, candidate_attempts
        )
        if allowed:
            return True, reason, candidate_key
        return False, reason or f"row attempt limit reached ({review_attempts}/{AUTO_GRAB_MAX_ATTEMPTS_PER_REVIEW})", candidate_key
    return True, "", candidate_key


def record_auto_grab_attempt(state, review_id, candidate, row):
    review_key = str(review_id)
    candidate_key = auto_grab_candidate_key(review_id, candidate)
    state.setdefault("review_attempts", {})
    state.setdefault("candidate_attempts", {})
    state.setdefault("last_attempts", {})
    state.setdefault("candidate_last_attempts", {})
    state["review_attempts"][review_key] = int(state["review_attempts"].get(review_key) or 0) + 1
    state["candidate_attempts"][candidate_key] = int(state["candidate_attempts"].get(candidate_key) or 0) + 1
    attempt_record = {
        "candidate_key": candidate_key,
        "ts": now(),
        "status": row.get("status"),
        "filename": row.get("filename"),
        "username": row.get("username"),
        "score": row.get("score"),
        "error": row.get("error"),
    }
    state["last_attempts"][review_key] = attempt_record
    candidate_attempt_record = dict(attempt_record)
    candidate_attempt_record["review_id"] = review_key
    state["candidate_last_attempts"][candidate_key] = candidate_attempt_record
    return candidate_key


def auto_grab_error_is_transient(error):
    text = str(error or "").strip().lower()
    if not text:
        return False
    return any(pattern in text for pattern in TRANSIENT_AUTO_GRAB_ERROR_PATTERNS)


def mark_auto_grab_transient_error(row, error):
    row["status"] = "transient_error"
    row["transient_error"] = True
    row["error"] = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    row["retry_after_seconds"] = TRANSIENT_AUTO_GRAB_RETRY_SECONDS
    row["reason"] = "SLSKD download API error; retrying candidate shortly"
    return row


def auto_grab_attempt_retry_seconds(status, error=None):
    status = str(status or "").lower()
    if status in {"started_waiting", "already_downloading"}:
        return TRANSIENT_BAD_CANDIDATE_RETRY_SECONDS
    if status in {"enqueue_response_ambiguous", "ambiguous_enqueue_response"}:
        return TRANSIENT_AUTO_GRAB_RETRY_SECONDS
    if status in {"error", "download_api_error", "download_preflight_api_error", "transient_error"}:
        if auto_grab_error_is_transient(error):
            return TRANSIENT_AUTO_GRAB_RETRY_SECONDS
    return 0


def load_actions():
    data = read_json(MANUAL_REVIEW_ACTIONS_FILE, {}) or {}
    for key in ("ignored", "approved", "bad", "pack_finished"):
        data.setdefault(key, [])
    if not isinstance(data.get("manual_source_resolved"), list):
        data["manual_source_resolved"] = []
    if not isinstance(data.get("manual_source_bad_candidates"), dict):
        data["manual_source_bad_candidates"] = {}
    return data


def save_actions(actions):
    write_json(MANUAL_REVIEW_ACTIONS_FILE, actions if isinstance(actions, dict) else {})


def review_has_waiting_or_terminal_action(review_id):
    review_id = str(review_id or "")
    if not review_id:
        return False
    actions = load_actions()
    for key in ("ignored", "approved", "bad", "pack_finished", "pack_approved"):
        values = actions.get(key) or []
        if isinstance(values, list) and review_id in {str(value) for value in values}:
            return True
    waiting = actions.get("manual_source_waiting")
    if isinstance(waiting, dict) and review_id in waiting:
        return True
    for row in actions.get("manual_source_resolved") or []:
        if isinstance(row, dict) and str(row.get("review_id") or "") == review_id:
            return True
        if not isinstance(row, dict) and str(row or "") == review_id:
            return True
    return False


def cleared_watched_attempt_can_retry(state, review_id, candidate, candidate_key, candidate_attempts):
    if int(candidate_attempts or 0) < AUTO_GRAB_MAX_ATTEMPTS_PER_CANDIDATE:
        return False
    review_id = str(review_id or "")
    if review_has_waiting_or_terminal_action(review_id):
        return False
    if bad_candidate_match(review_id, candidate):
        return False
    last_attempt = (state.get("last_attempts") or {}).get(review_id)
    if not isinstance(last_attempt, dict):
        return False
    if str(last_attempt.get("candidate_key") or "") != str(candidate_key or ""):
        return False
    try:
        last_ts = float(last_attempt.get("ts") or 0)
    except (TypeError, ValueError):
        last_ts = 0
    status = str(last_attempt.get("status") or "").lower()
    retry_seconds = auto_grab_attempt_retry_seconds(status, last_attempt.get("error"))
    if retry_seconds <= 0:
        return False
    if last_ts <= 0 or now() - last_ts < retry_seconds:
        return False
    return True


def cooled_down_candidate_attempt_retry_reason(state, review_id, candidate, candidate_key):
    review_id = str(review_id or "")
    if not review_id or review_has_waiting_or_terminal_action(review_id):
        return ""
    if bad_candidate_match(review_id, candidate):
        return ""

    last_attempt = (state.get("candidate_last_attempts") or {}).get(str(candidate_key or ""))
    if not isinstance(last_attempt, dict):
        row_last_attempt = (state.get("last_attempts") or {}).get(review_id)
        if isinstance(row_last_attempt, dict) and str(row_last_attempt.get("candidate_key") or "") == str(candidate_key or ""):
            last_attempt = row_last_attempt
    if isinstance(last_attempt, dict):
        try:
            last_ts = float(last_attempt.get("ts") or 0)
        except (TypeError, ValueError):
            last_ts = 0
        status = str(last_attempt.get("status") or "").lower()
        retry_seconds = auto_grab_attempt_retry_seconds(status, last_attempt.get("error"))
        if retry_seconds > 0:
            if last_ts > 0 and now() - last_ts < retry_seconds:
                return ""
            if status in {"started_waiting", "already_downloading"}:
                return "previous candidate attempt cooled down without a terminal result; retrying candidate"
            return "previous SLSKD download API hiccup cooled down; retrying candidate"

    if transient_bad_candidate_retry_match(review_id, candidate):
        return "previous transient SLSKD failure cooled down; retrying candidate"
    return ""


def series_handoff_token(review_id, candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    payload = "|".join((
        str(review_id or ""),
        normalize(candidate.get("username") or ""),
        str(candidate.get("filename") or candidate.get("path") or "").replace("\\", "/").lower(),
        str(int(candidate.get("size") or 0)),
    ))
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:32]


def series_handoff_identity_binding(token, candidate):
    """Bind public identity proof to one private route token and leaf."""

    candidate = candidate if isinstance(candidate, dict) else {}
    payload = "|".join((
        "slskd-series-identity-v1",
        str(token or ""),
        normalize(candidate.get("series_directory_identity_filename") or ""),
        normalize(filename_leaf(candidate.get("filename") or candidate.get("path") or "")),
        str(auto_grab_candidate_numeric(candidate.get("size"))),
    ))
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def redact_series_handoff_candidate(review_id, candidate):
    """Store raw Soulseek routing only in process memory until enqueue."""

    raw = dict(candidate or {})
    token = series_handoff_token(review_id, raw)
    raw["_series_directory_handoff_review_id"] = str(review_id or "")
    SERIES_RUN_EPHEMERAL_CANDIDATES[token] = raw
    redacted = dict(raw)
    redacted["filename"] = filename_leaf(raw.get("filename") or raw.get("path"))
    redacted.pop("path", None)
    redacted.pop("remote_filename", None)
    redacted.pop("username", None)
    redacted.pop("provider", None)
    redacted.pop("user", None)
    redacted.pop("_series_directory_handoff_review_id", None)
    redacted["series_directory_handoff_token"] = token
    redacted["series_directory_identity_binding"] = series_handoff_identity_binding(token, redacted)
    redacted["source_instance_hash"] = hashlib.sha256(f"slskd-handoff|{token}".encode("utf-8")).hexdigest()[:24]
    return redacted


def hydrate_series_handoff_candidate(candidate, review_id=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    token = str(candidate.get("series_directory_handoff_token") or "").strip()
    if not token:
        return candidate, True
    raw = SERIES_RUN_EPHEMERAL_CANDIDATES.get(token)
    if not isinstance(raw, dict):
        return candidate, False
    identity_filename = str(candidate.get("series_directory_identity_filename") or "").strip()
    identity_binding = str(candidate.get("series_directory_identity_binding") or "").strip()
    expected_identity = str(raw.get("series_directory_identity_filename") or "").strip()
    expected_review_id = str(raw.get("_series_directory_handoff_review_id") or "").strip()
    presented_review_id = str(review_id or "").strip()
    expected_leaf = normalize(filename_leaf(raw.get("filename") or raw.get("path") or ""))
    presented_leaf = normalize(filename_leaf(candidate.get("filename") or candidate.get("path") or ""))
    if (
        not identity_filename
        or not expected_identity
        or not expected_review_id
        or not presented_review_id
        or not identity_binding
        or not hmac.compare_digest(identity_binding, series_handoff_identity_binding(token, candidate))
        or not hmac.compare_digest(identity_filename.encode("utf-8"), expected_identity.encode("utf-8"))
        or not hmac.compare_digest(presented_review_id.encode("utf-8"), expected_review_id.encode("utf-8"))
        or not hmac.compare_digest(presented_leaf.encode("utf-8"), expected_leaf.encode("utf-8"))
        or auto_grab_candidate_numeric(candidate.get("size")) != auto_grab_candidate_numeric(raw.get("size"))
    ):
        return candidate, False
    hydrated = dict(candidate)
    hydrated.update({
        "filename": raw.get("filename") or raw.get("path"),
        "username": raw.get("username") or raw.get("provider") or raw.get("user"),
        "size": raw.get("size") or candidate.get("size"),
    })
    return hydrated, True


def privacy_safe_handoff_transfer(transfer):
    transfer = transfer if isinstance(transfer, dict) else {}
    return {
        key: transfer.get(key)
        for key in (
            "id", "state", "stateDescription", "requestedAt", "endedAt",
            "bytesTransferred", "bytesRemaining", "percentComplete",
            "averageSpeed", "attempts", "size",
        )
        if transfer.get(key) not in (None, "")
    }


def privacy_safe_handoff_enqueue(enqueue):
    enqueue = enqueue if isinstance(enqueue, dict) else {}
    return {
        "ok": bool(enqueue.get("ok", True)),
        "dry_run": bool(enqueue.get("dry_run")),
        "transfer_count": len(auto_grab_enqueue_transfer_rows(enqueue)),
    }


def privacy_safe_handoff_operation(result):
    result = result if isinstance(result, dict) else {}
    return {
        key: result.get(key)
        for key in ("ok", "deleted", "dry_run", "status")
        if result.get(key) not in (None, "")
    }


def hidden_review_ids():
    actions = load_actions()
    resolved = set()
    for row in actions.get("manual_source_resolved") or []:
        if isinstance(row, dict) and row.get("review_id"):
            resolved.add(str(row.get("review_id")))
        elif row:
            resolved.add(str(row))
    return set(actions.get("ignored", [])) | set(actions.get("approved", [])) | set(actions.get("bad", [])) | resolved


def hard_hidden_review_ids():
    actions = load_actions()
    return set(actions.get("ignored", [])) | set(actions.get("bad", []))


def load_source_review_items(limit=200, series=None):
    if not REVIEW_FILE.exists():
        return []
    hidden = hidden_review_ids()
    try:
        # Keep this aligned with InkDrop's Action Queue window so status badges
        # never advertise SLSKD candidates for rows the UI cannot open.
        lines = REVIEW_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-500:]
    except OSError:
        return []
    out = []
    seen = set()
    series_norm = normalize(series) if series else ""
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("autopilot_queue") or str(item.get("source") or "") == "series_autopilot_queue":
            continue
        if str(item.get("reason") or "") not in SOURCE_REASONS:
            continue
        if series_norm and series_norm not in normalize(item.get("series") or item.get("query")):
            continue
        rid = review_id_for(item)
        if rid in hidden or rid in seen:
            continue
        seen.add(rid)
        item = dict(item)
        item["review_id"] = rid
        out.append(item)
        if len(out) >= limit:
            break
    return out


def queue_source_issue(row):
    value = (row or {}).get("issue") or (row or {}).get("issue_number") or (row or {}).get("number")
    return str(value or "").strip()


def queue_source_explicit_unit_context(item):
    """Resolve one durable queue unit; downstream handoff may only consume it."""
    item = item if isinstance(item, dict) else {}
    if not inkdrop_candidate_matching:
        return {}
    target = inkdrop_candidate_matching.target_context(item)
    unit_type = str(target.get("unit_type") or "").strip().lower()
    aliases = {
        "vol": "volume", "book_volume": "volume", "manga_volume": "volume",
        "manga_chapter": "chapter", "comic_issue": "issue",
    }
    unit_type = aliases.get(unit_type, unit_type)
    if unit_type not in {"issue", "chapter", "volume"}:
        return {}
    number = str(target.get(f"{unit_type}_number") or "").strip()
    if not number:
        return {}
    issue_alias = str(item.get("issue_number") or item.get("issue") or "").strip()
    chapter_alias = str(item.get("chapter_number") or item.get("chapter") or "").strip()
    media_type = str(item.get("media_type") or "").strip().lower()
    if unit_type == "volume" and not str(item.get("unit_type") or "").strip():
        # Legacy manga rows may be promoted from their exact durable ``Vol. N``
        # title only when both compatibility aliases bind to that same number.
        if not (
            media_type == "manga"
            and issue_alias
            and chapter_alias
            and issue_number_keys(issue_alias) == issue_number_keys(chapter_alias)
            and issue_number_keys(number) == issue_number_keys(issue_alias)
        ):
            return {}
    elif unit_type == "chapter" and issue_alias and (
        not chapter_alias or issue_number_keys(issue_alias) != issue_number_keys(chapter_alias)
    ):
        return {}
    elif unit_type == "issue" and chapter_alias and (
        not issue_alias or issue_number_keys(issue_alias) != issue_number_keys(chapter_alias)
    ):
        return {}
    return {"unit_type": unit_type, f"{unit_type}_number": number}


def queue_source_review_item(row):
    if not isinstance(row, dict):
        return None
    series = str(row.get("series") or row.get("query") or "").strip()
    issue = queue_source_issue(row)
    if not series or not issue:
        return None
    item = {
        "reason": "no_safe_source",
        "series": series,
        "query": series,
        "issue": issue,
        "issue_title": row.get("issue_title") or row.get("title") or "",
        "publisher": row.get("publisher") or row.get("watch_publisher") or "",
        "watch_publisher": row.get("watch_publisher") or row.get("publisher") or "",
        "folder": row.get("folder") or "",
        "search_query": row.get("query") or "",
        "year": row.get("watch_year") or row.get("year") or "",
        "watch_year": row.get("watch_year") or row.get("year") or "",
        "media_type": row.get("media_type") or "",
        "comicvine_id": row.get("comicvine_id") or "",
        "watch_id": row.get("watch_id") or "",
        "series_id": row.get("series_id") or "",
        "queue_identity": row.get("queue_identity") or "",
        "unit_type": row.get("unit_type") or row.get("unitType") or "",
        "issue_number": row.get("issue_number") or row.get("issue") or issue,
        "chapter_number": row.get("chapter_number") or row.get("chapter") or "",
        "volume_number": row.get("volume_number") or row.get("volume") or "",
        "edition_id": row.get("edition_id") or "",
        "edition_marker": row.get("edition_marker") or "",
        "username": row.get("username") or row.get("slskd_username") or row.get("last_slskd_user") or "",
        "source": "series_autopilot_queue",
        "autopilot_queue": True,
        "autopilot_queue_key": row.get("key") or "",
        "legacy_key": row.get("legacy_key") or "",
        "autopilot_state": row.get("state") or "queued",
        "ts": row.get("last_attempt_at") or row.get("source_ladder_attempted_at") or now(),
    }
    item.update(queue_source_explicit_unit_context(item))
    series_id = str(row.get("series_id") or "").strip()
    if not series_id:
        provider = str(row.get("metadata_provider") or row.get("series_source") or "").strip().lower()
        metadata_id = str(row.get("metadata_id") or row.get("comicvine_id") or "").strip()
        if provider == "comicvine" and metadata_id.isdigit() and int(metadata_id) > 0:
            series_id = f"comicvine:{metadata_id}"
    if series_id and inkdrop_state:
        try:
            import inkdrop_source_worker_coordinator as source_coordinator

            singleton_context = source_coordinator._singleton_issue_context(INKDROP_STATE_DB, series_id)
            authoritative_title = str(singleton_context.get("singleton_series_title") or "").strip()
            row_issue_provider = str(row.get("issue_metadata_provider") or "").strip().lower()
            row_issue_metadata_id = str(row.get("issue_metadata_id") or "").strip()
            row_issue_number = inkdrop_state.normalize_issue_number(queue_source_issue(row))
            authoritative_issue_number = inkdrop_state.normalize_issue_number(
                singleton_context.get("singleton_issue_number")
            )
            identity_bound = bool(
                singleton_context.get("singleton_series_id") == series_id
                and authoritative_title
                and normalize(series) == normalize(authoritative_title)
                and row_issue_provider
                == str(singleton_context.get("singleton_issue_metadata_provider") or "").strip().lower()
                and row_issue_metadata_id
                == str(singleton_context.get("singleton_issue_metadata_id") or "").strip()
                and row_issue_number
                and row_issue_number == authoritative_issue_number
            )
            if identity_bound:
                item.update(singleton_context)
        except (ImportError, AttributeError, OSError, sqlite3.Error):
            # Proof is authority from the durable DB. If it cannot be read,
            # leave the ordinary strict unit gate in place.
            pass
    if item.get("collected_singleton_proof"):
        try:
            import inkdrop_manual_search

            item["collected_singleton_title_aliases"] = inkdrop_manual_search.collected_title_aliases(
                item.get("singleton_series_title")
            )
        except (ImportError, AttributeError):
            pass
    item["review_id"] = review_id_for(item)
    return item


def load_queue_context_review_items():
    queue = read_json(SERIES_AUTOPILOT_QUEUE_FILE, {}) or {}
    rows = (queue.get("items") or {}).values() if isinstance(queue, dict) else []
    out = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("present_in_watch") is False:
            continue
        item = queue_source_review_item(row)
        if not item:
            continue
        rid = str(item.get("review_id") or review_id_for(item))
        if rid in seen:
            continue
        seen.add(rid)
        out.append(item)
    return out


def queue_row_has_cached_safe_slskd_candidate(row):
    if not isinstance(row, dict):
        return False
    try:
        safe_count = int(row.get("last_slskd_auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        safe_count = 0
    if safe_count > 0:
        return True
    last_event = str(row.get("last_event") or "").lower()
    if "slskd candidates available for autopick" in last_event:
        return True
    return False


def queue_probe_priority(row):
    state = str((row or {}).get("state") or "queued")
    source = str((row or {}).get("current_source") or "")
    if queue_row_needs_staged_recheck(row):
        bucket = 0
    elif source == "slskd":
        bucket = 0
    elif queue_row_has_cached_safe_slskd_candidate(row):
        bucket = 1
    elif state == "searching":
        bucket = 2
    else:
        bucket = 3
    try:
        retry_after = float((row or {}).get("retry_after") or 0)
    except (TypeError, ValueError):
        retry_after = 0
    # A never-attempted row has no retry_after (0) and must not outrank a row
    # whose cooldown already elapsed -- both are equally eligible right now.
    # Comparing raw epoch timestamps here previously ranked every untried row
    # ahead of any row with a real retry_after, permanently starving retried
    # candidates once the untried backlog exceeded the per-pass selection cap.
    retry_wait = max(0.0, retry_after - time.time())
    # With retry_after cleared/expired backlog-wide, every bucket-3 row ties
    # at retry_wait=0 and the series-name tiebreak below decides every pass --
    # series sorting late alphabetically never clear the per-pass selection
    # window and starve forever, the same failure mode this function was
    # already fixed for once, just moved one tiebreak level down. Break the
    # tie by staleness (oldest touched first) so the backlog rotates and
    # every row eventually surfaces regardless of series name.
    try:
        last_touched = float((row or {}).get("ts") or 0)
    except (TypeError, ValueError):
        last_touched = 0
    return (
        bucket,
        retry_wait,
        last_touched if last_touched > 0 else float("inf"),
        normalize((row or {}).get("series") or ""),
        token_number(queue_source_issue(row)) or 999999,
        queue_source_issue(row),
    )


def queue_row_needs_staged_recheck(row):
    if not isinstance(row, dict):
        return False
    if str(row.get("state") or "") != "importing":
        return False
    if str(row.get("last_slskd_status") or "") == "staged_file_ready":
        return True
    last_event = str(row.get("last_event") or "").lower()
    return "staged file detected" in last_event


def load_queue_source_review_items(limit=200, series=None):
    queue = read_json(SERIES_AUTOPILOT_QUEUE_FILE, {}) or {}
    raw_items = queue.get("items") if isinstance(queue, dict) else {}
    if isinstance(raw_items, dict):
        rows = list(raw_items.values())
    elif isinstance(raw_items, list):
        rows = raw_items
    else:
        rows = []
    hidden = hidden_review_ids()
    hard_hidden = hard_hidden_review_ids()
    series_norm = normalize(series) if series else ""
    out = []
    seen = set()
    for row in sorted(rows, key=queue_probe_priority):
        if not isinstance(row, dict):
            continue
        if row.get("present_in_watch") is False:
            continue
        state = str(row.get("state") or "queued")
        if state in {"downloading", "verified", "needs_you"}:
            continue
        if state == "importing" and not queue_row_needs_staged_recheck(row):
            continue
        if series_norm and series_norm not in normalize(row.get("series") or row.get("query")):
            continue
        item = queue_source_review_item(row)
        if not item:
            continue
        rid = str(item.get("review_id") or review_id_for(item))
        if rid in hard_hidden or rid in seen:
            continue
        if (
            rid in hidden
            and str(row.get("state") or "queued") in {"downloading", "importing", "verified", "needs_you"}
            and not queue_row_needs_staged_recheck(row)
        ):
            continue
        seen.add(rid)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def combine_source_review_items(*groups):
    out = []
    seen_review_ids = set()
    issue_index = {}
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("review_id") or review_id_for(item))
            issue_key = (
                normalize(item.get("series") or item.get("query") or ""),
                normalize(item.get("issue") or ""),
                str(
                    item.get("autopilot_queue_key")
                    or item.get("queue_identity")
                    or item.get("comicvine_id")
                    or ""
                ),
            )
            if rid in seen_review_ids:
                continue
            existing = issue_index.get(issue_key)
            if existing:
                for key in (
                    "issue_title",
                    "publisher",
                    "folder",
                    "search_query",
                    "year",
                    "watch_year",
                    "comicvine_id",
                    "watch_id",
                    "queue_identity",
                    "autopilot_queue_key",
                    "legacy_key",
                ):
                    if not existing.get(key) and item.get(key):
                        existing[key] = item.get(key)
                if item.get("autopilot_queue"):
                    existing["autopilot_queue_metadata"] = True
                    existing["autopilot_state"] = item.get("autopilot_state") or existing.get("autopilot_state")
                continue
            item = dict(item)
            item["review_id"] = rid
            seen_review_ids.add(rid)
            issue_index[issue_key] = item
            out.append(item)
    return out


def recent_review_ids(max_lines=500):
    if not REVIEW_FILE.exists():
        return set()
    try:
        lines = REVIEW_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
    except OSError:
        return set()
    ids = set()
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        ids.add(str(item.get("review_id") or review_id_for(item)))
    ids.discard("")
    return ids


def ensure_queue_review_rows(items):
    queue_items = [item for item in items or [] if isinstance(item, dict) and item.get("autopilot_queue")]
    if not queue_items:
        return {"ensured": 0, "already_present": 0, "skipped": 0, "persisted": False}
    if not truthy_env("INKDROP_SLSKD_PERSIST_QUEUE_REVIEW_ROWS"):
        return {
            "ensured": 0,
            "already_present": 0,
            "skipped": len(queue_items),
            "persisted": False,
            "reason": "queue-backed rows stay in the durable autopilot queue/status instead of manual-review.jsonl",
        }
    existing = recent_review_ids()
    rows = []
    already_present = 0
    for item in queue_items:
        row = dict(item)
        rid = str(row.get("review_id") or review_id_for(row))
        if rid in existing:
            already_present += 1
            continue
        row["review_id"] = rid
        row["ts"] = now()
        row["ts_iso"] = utc_stamp(row["ts"])
        row["automation_note"] = "created from watched-series queue for SLSKD autopick"
        rows.append(row)
        existing.add(rid)
    if rows:
        REVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
        with REVIEW_FILE.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    return {"ensured": len(rows), "already_present": already_present, "skipped": 0, "persisted": True}


# A colon always separates a title from its subtitle ("Batman: The Court of
# Owls"), and so does a dash standing alone with space on both sides ("Fables
# - Legends in Exile", "The Last Airbender -- Suki, Alone" -- ComicVine writes
# the em dash as two hyphens often enough to matter).
SPACED_SEPARATOR_RE = re.compile(r"\s*:\s*|\s+[-–—]+\s+")

# A tight, unspaced dash is the ambiguous one, and both readings are real in
# the library. Usually it joins one compound word -- "Spider-Man", "X-Men",
# "One-Punch Man", "Shangri-La Frontier", "ODY-C" -- where dropping the
# leading part leaves a fragment ("Man") that matches half the shelf.
#
# But ComicVine also names story arcs this way, with no space:
# "Avatar: The Last Airbender-North and South", "Nickelodeon The Legend of
# Korra-Turf Wars". There the tail IS the subtitle, and it is how the files
# are actually named, so losing it costs real matches.
#
# What separates them is where the dash falls. A compound word sits inside the
# opening words of a title; an arc separator comes after the full series name
# has already been spelled out. Three or more words ahead of the dash is the
# line, and it holds for every monitored series carrying a tight dash.
TIGHT_DASH_RE = re.compile(r"(?<=[^\s])[-–—](?=[^\s])")
TIGHT_DASH_MIN_LEADING_WORDS = 3


def tight_dash_is_subtitle_separator(value, match_start):
    leading = str(value or "")[:match_start]
    return len(re.findall(r"[A-Za-z0-9]+", leading)) >= TIGHT_DASH_MIN_LEADING_WORDS


def split_title_and_subtitle(value):
    """Split a series title into its parts on real separators only."""

    text = str(value or "")
    pieces = []
    cursor = 0
    for match in TIGHT_DASH_RE.finditer(text):
        if tight_dash_is_subtitle_separator(text, match.start()):
            pieces.append(text[cursor:match.start()])
            cursor = match.end()
    pieces.append(text[cursor:])
    parts = []
    for piece in pieces:
        parts.extend(SPACED_SEPARATOR_RE.split(piece))
    return [part for part in parts if part and part.strip()]


def has_subtitle_separator(value):
    return len(split_title_and_subtitle(value)) >= 2


def title_variants(series):
    raw = str(series or "").strip()
    if not raw:
        return []
    cleaned = without_edition_phrases(raw)
    no_identity = without_parenthetical_identity(cleaned)
    no_punctuation = display_clean(re.sub(r"[:\-–—]+", " ", no_identity or cleaned))
    raw_clean = display_clean(raw)
    brandless = without_branding_prefix(no_punctuation)
    variants = [brandless, no_punctuation, no_identity, cleaned, raw_clean]
    split_sources = [raw]
    if normalize(without_branding_prefix(raw)) != normalize(raw):
        split_sources.insert(0, without_branding_prefix(raw))
    for split_source in split_sources:
        if has_subtitle_separator(split_source):
            parts = [
                without_branding_prefix(without_parenthetical_identity(without_edition_phrases(part)))
                for part in split_title_and_subtitle(split_source)
            ]
            parts = [part for part in parts if part]
            if len(parts) >= 2:
                variants.append(" ".join(parts))
                variants.append(f"{parts[0]} {parts[-1]}")
                variants.append(parts[-1])
    if has_subtitle_separator(raw):
        parts = [without_edition_phrases(part) for part in split_title_and_subtitle(raw)]
        parts = [part for part in parts if part]
        if len(parts) >= 2:
            variants.append(" ".join(parts))
            variants.append(f"{parts[0]} {parts[-1]}")
            variants.append(parts[-1])
    variants.extend(numeric_word_title_variants(brandless))
    variants.extend(numeric_word_title_variants(no_punctuation))
    variants.append(re.sub(r"\bnickelodeon\b", " ", no_punctuation, flags=re.I))
    expanded = []
    for variant in variants:
        expanded.extend(stylized_x_title_variants(variant))
    return unique_values(expanded, limit=16)


def creator_possessive_title_variants(title):
    text = display_clean(str(title or ""))
    if not text:
        return []
    match = re.match(
        r"^(?:[A-Z][\w.\-]+(?:\s+[A-Z][\w.\-]+){0,3})['’]s\s+(.+)$",
        text,
    )
    if not match:
        return []
    remainder = display_clean(match.group(1))
    if not remainder:
        return []
    words = re.findall(r"[a-z0-9]+", remainder.lower())
    if len(words) >= 2 or re.match(r"^\d", remainder):
        return [remainder]
    # A single-word remainder is still a plausible standalone title --
    # "Naoki Urasawa's Monster" is exactly this shape, and real Soulseek
    # shares are filed as "Monster", never under the creator credit -- but a
    # short one is too likely to be an unrelated common word rather than a
    # real title, so only trust it starting at a modest length.
    if words and len(words[0]) >= 4:
        return [remainder]
    return []


def slskd_query_priority_titles(series, variants):
    """Put the de-prefixed alias ahead of a licensed "Creator's Title" credit.

    Only the first title in the returned list gets the full issue-number
    suffix treatment in source_queries (v01/c01/ch01/Vol. N/...); every other
    variant gets at most one suffix. "Naoki Urasawa's 20th Century Boys" and
    "Naoki Urasawa's Monster" are the licensed English series titles, so they
    are what series/issue metadata stores and what source_title_variants
    puts first -- but Soulseek share names are almost never filed under the
    creator-credit form, so the whole per-issue suffix budget was being
    spent on the title variant real shares don't use, while the de-prefixed
    alias (already generated by creator_possessive_title_variants) only ever
    got a single bare-number query. Promote it to first when present; this
    is a no-op for the large majority of titles that have no creator-credit
    prefix at all.
    """

    variants = list(variants or [])
    if not variants:
        return variants
    stripped = creator_possessive_title_variants(series)
    if not stripped:
        return variants
    target_key = normalize(stripped[0])
    if not target_key:
        return variants
    match_index = next(
        (index for index, value in enumerate(variants) if normalize(value) == target_key),
        None,
    )
    if match_index is None or match_index == 0:
        return variants
    reordered = list(variants)
    reordered.insert(0, reordered.pop(match_index))
    return reordered


def clean_alias_title(value):
    text = str(value or "")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\b\d{4}(?:\s*[-–—]\s*\d{2,4})?\b", " ", text)
    text = re.sub(r"\b\d{1,4}\s*[-–—]\s*\d{1,4}\+?\b", " ", text)
    text = re.sub(
        r"\b(?:digital|empire|zone|ctc|covers?|complete|lucaz|minutemen|spaztastic|scanlation|webrip)\b",
        " ",
        text,
        flags=re.I,
    )
    return display_clean(text)


def clean_issue_title(value):
    text = str(value or "")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\b\d{4}(?:\s*[-–—]\s*\d{2,4})?\b", " ", text)
    text = re.sub(r"[,;]+", " ", text)
    return display_clean(text)


def without_leading_article(value):
    return display_clean(re.sub(r"^\s*(?:a|an|the)\s+", " ", str(value or ""), flags=re.I))


def without_part_suffix(value):
    return display_clean(
        re.sub(
            rf"\s*(?:[,:\-–—]\s*)?(?:part|pt)\.?\s+{NUMBER_TOKEN_PATTERN}(?:\s+of\s+{NUMBER_TOKEN_PATTERN})?\s*$",
            " ",
            str(value or ""),
            flags=re.I,
        )
    )


def issue_title_variants(item):
    metadata = issue_metadata_for_item(item)
    title = clean_issue_title(metadata.get("title") or "")
    if not title:
        return []
    variants = [title]
    if ":" in title or "-" in title or "–" in title or "—" in title:
        parts = [display_clean(part) for part in re.split(r"[:\-–—]+", title) if display_clean(part)]
        if len(parts) >= 2:
            variants.append(" ".join(parts))
            variants.append(parts[-1])
    without_book_prefix = re.sub(
        r"^\s*book\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*",
        " ",
        title,
        flags=re.I,
    )
    without_book_prefix = display_clean(without_book_prefix)
    if without_book_prefix and normalize(without_book_prefix) != normalize(title):
        variants.append(without_book_prefix)
    without_part = without_part_suffix(title)
    if without_part and normalize(without_part) != normalize(title):
        variants.append(without_part)
    for value in list(variants):
        article_free = without_leading_article(value)
        if article_free and normalize(article_free) != normalize(value):
            variants.append(article_free)
    return unique_values(variants, limit=6)


def alias_key_matches_series(key, series):
    series_keys = {
        normalize(series),
        normalize(without_edition_phrases(series)),
    }
    key_keys = {
        normalize(key),
        normalize(without_edition_phrases(key)),
    }
    series_keys.discard("")
    key_keys.discard("")
    if series_keys & key_keys:
        return True
    for series_key in series_keys:
        series_words = set(series_key.split())
        for key_key in key_keys:
            key_words = set(key_key.split())
            if len(key_words) >= 2 and key_words <= series_words:
                return True
            if len(series_words) >= 2 and series_words <= key_words:
                return True
    return False


def action_aliases_for_series(series):
    data = read_json(MANUAL_REVIEW_ACTIONS_FILE, {}) or {}
    aliases = data.get("aliases") if isinstance(data, dict) else {}
    if not isinstance(aliases, dict):
        return []
    out = []
    for row in aliases.values():
        if not isinstance(row, dict):
            continue
        if not alias_key_matches_series(row.get("series") or "", series):
            continue
        if row.get("alias"):
            out.append(str(row.get("alias") or ""))
    return unique_values(out, limit=12)


def aliases_for_series(series):
    data = read_json(RSS_ALIASES_FILE, {}) or {}
    aliases = []
    if isinstance(data, dict):
        for key, values in data.items():
            if not alias_key_matches_series(key, series):
                continue
            if isinstance(values, list):
                aliases.extend(str(value or "") for value in values)
            elif values:
                aliases.append(str(values))
    aliases.extend(action_aliases_for_series(series))
    return unique_values(aliases, limit=18)


def _mangadex_alt_title_values(attributes):
    values = []
    title = (attributes or {}).get("title")
    if isinstance(title, dict):
        values.extend(str(v) for v in title.values() if v)
    for row in (attributes or {}).get("altTitles") or []:
        if isinstance(row, dict):
            values.extend(str(v) for v in row.values() if v)
    return values


def mangadex_alt_titles_for_series(series):
    """Manga's own Japanese/romaji/original title is not a different series.

    MangaDex stores every alternate name a manga is known by (native script,
    romaji, and other-language titles). A candidate filename carrying one of
    those is the same release under its other name, not evidence of an
    unrelated subseries -- so this is consulted before flagging trailing
    filename words as suspicious.
    """
    key = normalize(series)
    if not key:
        return []
    cache = read_json(MANGADEX_ALT_TITLE_CACHE_FILE, {}) or {}
    if not isinstance(cache, dict):
        cache = {}
    entry = cache.get(key)
    try:
        cached_at = float((entry or {}).get("cached_at")) if isinstance(entry, dict) else 0.0
    except (TypeError, ValueError):
        cached_at = 0.0
    if isinstance(entry, dict) and now() - cached_at < MANGADEX_ALT_TITLE_CACHE_TTL_SECONDS:
        titles = entry.get("titles")
        return list(titles) if isinstance(titles, list) else []
    titles = []
    try:
        response = requests.get(
            f"{MANGADEX_API}/manga",
            params={"title": series, "limit": 1, "order[relevance]": "desc"},
            headers={"User-Agent": MANGADEX_USER_AGENT},
            timeout=6,
        )
        response.raise_for_status()
        results = response.json().get("data") or []
        if results:
            attributes = results[0].get("attributes") or {}
            candidates = _mangadex_alt_title_values(attributes)
            series_words = set(important_words(series))
            # MangaDex's own "primary" title is sometimes the romaji/native
            # name itself (e.g. this manga's primary title is "Jigokuraku",
            # not "Hell's Paradise") -- so confirm identity against every
            # known name for the result, not just whichever one MangaDex
            # happens to have marked primary.
            candidate_words = set()
            for candidate in candidates:
                candidate_words |= set(important_words(candidate))
            if series_words and candidate_words and (series_words & candidate_words):
                titles = unique_values(candidates, limit=12)
    except Exception:
        titles = []
    cache[key] = {"titles": titles, "cached_at": now()}
    try:
        write_json(MANGADEX_ALT_TITLE_CACHE_FILE, cache)
    except Exception:
        pass
    return titles


EDITION_DESCRIPTOR_RE = re.compile(
    r"(?i)\b(?:"
    r"absolute|omnibus|deluxe(?:\s+edition)?|library\s+edition|essential\s+edition|"
    r"complete\s+(?:collection|edition)|collected\s+edition|trade\s+paperback|tpb|"
    r"hardcover|hc|compendium|anniversary\s+edition|noir|coloring\s+book|"
    r"definitive\s+edition|director'?s\s+cut"
    r")\b"
)


def strip_edition_descriptors(title):
    text = EDITION_DESCRIPTOR_RE.sub(" ", str(title or ""))
    text = re.sub(r"[:\-–—]\s*(?=[:\-–—]|$)", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" :-–—")
    return text


def _edition_title_key(value):
    text = re.sub(r"[:\-–—,'’]+", " ", str(value or ""))
    # A franchise's own volumes aren't consistent about carrying the network/
    # publisher branding prefix (confirmed live: ComicVine has both "Avatar:
    # The Last Airbender-Smoke and Shadow Library Edition" and the real plain
    # "Nickelodeon Avatar: The Last Airbender - Smoke and Shadow" as the exact
    # same underlying book) -- strip it so that isn't mistaken for a real
    # title difference.
    text = re.sub(r"(?i)\bnickelodeon\b", " ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def comicvine_edition_target_has_standalone_alternative(comicvine_id, series_title):
    """Does ComicVine separately catalog a plain, non-special edition of this work?

    Three-state: True (a real plain edition exists elsewhere -- the tracked
    edition is a meaningful choice, keep the strict marker/year check), False
    (confirmed no standalone plain edition exists -- the tracked edition was
    never a real choice among options, safe to relax), or None (couldn't
    determine -- no API key, ComicVine unreachable, or no usable series title
    -- treat as unknown and keep the strict check, the safe default).

    A wanted series tracking a specific collected edition (Absolute, Omnibus,
    Library Edition, ...) only needs that *exact* printing when ComicVine's
    own catalog treats the plain version as a real, separate, queryable
    volume. When it doesn't, the tracked edition was never the "normal" book
    most readers mean by that title, and holding out for its exact printing
    has nothing real to hold out for (the case an operator explicitly
    confirmed for Absolute Batman: The Court of Owls -- ComicVine's catalog
    for that title has no plain trade at all, only differently-branded
    reprint lines). Cached with a long TTL: this is a durable catalog fact,
    not something that changes day to day, and never blocks the caller on a
    slow or failed lookup.
    """
    comicvine_id = str(comicvine_id or "").strip()
    series_title = str(series_title or "").strip()
    if not comicvine_id or not series_title:
        return None
    cache = read_json(EDITION_ALT_CACHE_FILE, {}) or {}
    if not isinstance(cache, dict):
        cache = {}
    entry = cache.get(comicvine_id)
    try:
        cached_at = float((entry or {}).get("cached_at")) if isinstance(entry, dict) else 0.0
    except (TypeError, ValueError):
        cached_at = 0.0
    if (
        isinstance(entry, dict)
        and "has_alternative" in entry
        and now() - cached_at < EDITION_ALT_CACHE_TTL_SECONDS
    ):
        return entry.get("has_alternative")

    config = provider_config("comicvine") or {}
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    api_key = str(settings.get("api_key") or "").strip()
    base_url = str(config.get("base_url") or COMICVINE_API).rstrip("/")
    if not api_key:
        return None

    plain_title = strip_edition_descriptors(series_title)
    if not plain_title:
        return None
    plain_key = _edition_title_key(plain_title)
    queries = [series_title]
    if plain_title.casefold() != series_title.casefold():
        queries.append(plain_title)

    any_query_succeeded = False
    for query in queries:
        try:
            response = requests.get(
                f"{base_url}/search/",
                params={
                    "api_key": api_key,
                    "format": "json",
                    "resources": "volume",
                    "query": query,
                    "field_list": "id,name",
                    "limit": 10,
                },
                headers={"User-Agent": COMICVINE_USER_AGENT},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            continue
        any_query_succeeded = True
        for result in data.get("results") or []:
            result_id = str(result.get("id") or "")
            if not result_id or result_id == comicvine_id:
                continue
            if _edition_title_key(result.get("name")) == plain_key:
                cache[comicvine_id] = {"has_alternative": True, "cached_at": now()}
                try:
                    write_json(EDITION_ALT_CACHE_FILE, cache)
                except Exception:
                    pass
                return True
    if not any_query_succeeded:
        return None
    cache[comicvine_id] = {"has_alternative": False, "cached_at": now()}
    try:
        write_json(EDITION_ALT_CACHE_FILE, cache)
    except Exception:
        pass
    return False


def alias_mentions_issue(alias, issue):
    number = token_number(str(issue or "").strip())
    if number is None:
        return False
    text = str(alias or "")
    for start, end in re.findall(r"(?<!\d)(\d{1,4})\s*[-–—]\s*(\d{1,4})(?!\d)", text):
        try:
            if int(start) <= number <= int(end):
                return True
        except ValueError:
            pass
    tokens = {
        token_number(token)
        for token in re.findall(rf"(?<![a-z0-9]){NUMBER_TOKEN_PATTERN}(?![a-z0-9])", text, flags=re.I)
    }
    tokens.discard(None)
    return number in tokens


def title_has_numbering(value):
    return bool(
        re.search(
            rf"\b(?:v|vol|volume|book|part|pt|issue)\s*{NUMBER_TOKEN_PATTERN}(?:\.\d+)?\b",
            str(value or ""),
            flags=re.I,
        )
    )


def source_title_variants(item):
    series = item_series_title(item)
    raw_series = str((item or {}).get("series") or (item or {}).get("series_title") or series).strip()
    values = []
    values.extend(inkdrop_sources.collected_title_aliases(raw_series))
    values.extend(inkdrop_sources.contributor_title_aliases(raw_series))
    values.extend(inkdrop_sources.contributor_title_aliases(series))
    alias_values = aliases_for_series(series)
    for alias in alias_values:
        cleaned = clean_alias_title(alias)
        if cleaned:
            values.extend(title_variants(cleaned))
            for variant in title_variants(cleaned):
                values.extend(creator_possessive_title_variants(variant))
        values.append(alias)
        values.extend(creator_possessive_title_variants(alias))
    values.extend(title_variants(raw_series))
    values.extend(title_variants(series))
    for variant in title_variants(raw_series):
        values.extend(creator_possessive_title_variants(variant))
    return unique_values(values, limit=24)


def prioritized_title_variants(variants):
    variants = list(variants or [])
    if not variants:
        return []
    first = variants[0]
    if re.match(r"^\s*\d+", str(first or "")):
        numeric_worded = [
            value for value in variants[1:]
            if not re.match(r"^\s*\d+", str(value or ""))
        ]
        if numeric_worded:
            return unique_values([numeric_worded[0], first, *numeric_worded[1:], *variants], limit=len(variants))
    return variants


def issue_query_suffixes(issue):
    text = str(issue or "").strip()
    if not text:
        return []
    out = [text]
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        raw = match.group(0)
        try:
            number = int(float(raw))
            out.extend([
                str(number),
                f"{number:03d}",
                f"#{number:02d}",
                f"#{number:03d}",
                f"c{number:02d}",
                f"c{number:03d}",
                f"ch{number:02d}",
                f"ch{number:03d}",
                f"v{number:02d}",
                f"v{number}",
                f"Part {number}",
                f"Pt {number}",
                f"Chapter {number}",
                f"Chapter {number:03d}",
                f"Ch {number}",
                f"Ch {number:03d}",
                f"Book {number}",
                f"Book {number:02d}",
                f"Volume {number}",
                f"Volume {number:02d}",
                f"Vol {number}",
                f"Vol {number:02d}",
                f"Issue {number}",
                f"Issue {number:03d}",
                f"{number:02d}",
                f"#{number}",
            ])
        except ValueError:
            pass
    return unique_values(out)


def early_issue_query_suffixes(issue):
    text = str(issue or "").strip()
    out = [text] if text else []
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        raw = match.group(0)
        try:
            number = int(float(raw))
            out.extend([
                f"v{number:02d}",
                f"v{number}",
                f"v{number:03d}",
                f"{number:03d}",
                f"{number:02d}",
                f"#{number:02d}",
                f"#{number:03d}",
                f"#{number}",
                f"c{number:03d}",
                f"c{number:02d}",
                f"ch{number:03d}",
                f"ch{number:02d}",
            ])
        except ValueError:
            pass
    return unique_values(out, limit=12)


def volume_like_issue_title(issue_titles, issue):
    match = re.search(r"\d+(?:\.\d+)?", str(issue or ""))
    if not match:
        return False
    try:
        number = int(float(match.group(0)))
    except ValueError:
        return False
    number_tokens = {str(number), f"{number:02d}", f"{number:03d}"}
    number_word = number_word_for_token(number)
    if number_word:
        number_tokens.add(normalize(number_word))
    for title in issue_titles or []:
        norm = normalize(title)
        words = norm.split()
        if not words:
            continue
        if (
            words[0] in {"v", "vol", "volume", "book", "band", "tome", "tomo"}
            and any(token in words for token in number_tokens)
        ):
            return True
        # Format-only metadata is common for collected-edition rows whose
        # ordinal remains in the issue field (for example HC/TPB or a library
        # edition). Treat it as volume discovery context, but do not infer a
        # volume from ordinary prose merely containing words such as "book".
        if norm in {"hc", "hardcover", "tpb", "paperback", "trade paperback", "omnibus", "library edition"}:
            return True
    return False


def volume_query_suffixes(issue_titles, issue):
    """Return identity-preserving volume spellings before generic variants."""

    if not volume_like_issue_title(issue_titles, issue):
        return []
    out = []
    for title in issue_titles or []:
        if volume_like_issue_title([title], issue):
            out.append(title)
    out.extend(compact_volume_query_suffixes(issue))
    return unique_values(out, limit=10)


def compact_volume_query_suffixes(issue):
    match = re.search(r"\d+(?:\.\d+)?", str(issue or ""))
    if not match:
        return []
    try:
        number = int(float(match.group(0)))
    except ValueError:
        return []
    out = [
        f"Volume {number}",
        f"Vol {number:02d}",
        f"v{number:02d}",
        f"Vol {number}",
        f"v{number}",
        f"Volume {number:02d}",
        f"v{number:03d}",
    ]
    roman = roman_numeral_for_number(number)
    if roman:
        out.extend([f"Volume {roman}", f"Vol {roman}"])
    return unique_values(out, limit=10)


def graphic_novel_query_suffixes(issue):
    match = re.search(r"\d+(?:\.\d+)?", str(issue or ""))
    if not match:
        return []
    try:
        number = int(float(match.group(0)))
    except ValueError:
        return []
    out = []
    word = number_word_for_token(number)
    out.append(f"Book {number}")
    if word:
        out.append(f"Book {word.capitalize()}")
    out.extend([f"Volume {number}", f"Vol {number}"])
    if word:
        out.extend([f"Volume {word.capitalize()}", f"Vol {word.capitalize()}"])
    out.extend([f"Book {number:02d}", f"Volume {number:02d}", f"Vol {number:02d}"])
    roman = roman_numeral_for_number(number)
    if roman:
        out.extend([f"Book {roman}", f"Volume {roman}", f"Vol {roman}"])
    return unique_values(out, limit=13)


def slskd_media_query_qualifier(item):
    """Return the broad provider vocabulary for this managed series."""

    item = item if isinstance(item, dict) else {}
    media_type = normalize(item.get("media_type") or item.get("mediaType") or "")
    provider = normalize(item.get("metadata_provider") or item.get("provider") or item.get("source") or "")
    publisher = item_publisher_text(item)
    if (
        media_type in {"manga", "manhwa", "manhua"}
        or provider == "mangadex"
        or any_normalized_phrase_in_text(publisher, MANGA_PUBLISHER_PHRASES)
    ):
        return "manga"
    return "comics"


def broad_series_query_variants(title, qualifier="comics"):
    clean = display_clean(title)
    if not clean or title_has_numbering(clean):
        return []
    words = important_words(clean)
    short_title = len(" ".join(words)) <= 3
    out = []
    # Automatic discovery must see the provider's broad series/folder cohort
    # before spending its small query budget on one exact issue spelling.
    out.append(clean)
    qualifier = normalize(qualifier) or "comics"
    out.append(f"{clean} {qualifier}")
    if short_title:
        out.extend([f"{clean} comics", f"{clean} manga", f"{clean} comic", f"{clean} cbz", f"{clean} cbr"])
    else:
        if len(words) <= 2:
            out.extend([f"{clean} comics", f"{clean} manga", f"{clean} cbz", f"{clean} cbr"])
    out.extend([
        f"{clean} complete",
        f"{clean} collection",
        f"{clean} volumes",
        f"{clean} volume",
    ])
    return unique_values(out, limit=10)


def trusted_collected_singleton_query_anchor(item):
    """Return the narrow high-recall alias backed by durable singleton proof."""

    item = item if isinstance(item, dict) else {}
    if not item.get("collected_singleton_proof"):
        return ""
    aliases = unique_values(item.get("collected_singleton_title_aliases") or [], limit=12)
    eligible = [
        display_clean(alias)
        for alias in aliases
        if len(important_words(alias)) >= 2
        and not title_has_numbering(alias)
    ]
    if not eligible:
        return ""
    # Manual Search already uses the shortest structural alias as its bounded
    # discovery fallback.  Use the same anchor for unattended probing only
    # when the durable singleton identity proof is present; strict candidate
    # compatibility still decides whether any returned file may be grabbed.
    return min(eligible, key=lambda value: (len(important_words(value)), len(normalize(value)), normalize(value)))


def source_queries(item):
    series = item_series_title(item)
    issue = str(item.get("issue") or "").strip()
    queries = []
    metadata = issue_metadata_for_item(item)
    metadata_query = metadata.get("search_query")
    variants = slskd_query_priority_titles(series, prioritized_title_variants(source_title_variants(item)))
    suffixes = issue_query_suffixes(issue)
    early_suffixes = early_issue_query_suffixes(issue)
    issue_titles = issue_title_variants(item)
    compact_volume_suffixes = volume_query_suffixes(issue_titles, issue)
    graphic_suffixes = graphic_novel_query_suffixes(issue) if issue_titles else []
    for alias in aliases_for_series(series):
        if alias_mentions_issue(alias, issue):
            queries.append(alias)
    preferred_titles = variants[:6]
    first_suffix = suffixes[0] if suffixes else ""
    canonical_title = preferred_titles[0] if preferred_titles else ""
    trusted_singleton_anchor = trusted_collected_singleton_query_anchor(item)
    media_query_qualifier = slskd_media_query_qualifier(item)
    if trusted_singleton_anchor:
        queries.append(trusted_singleton_anchor)
    # Broad series/alias discovery leads automatic SLSKD searches. Candidate
    # parsing still enforces the requested unit, while one response can expose
    # a complete sibling directory for safe multi-issue coverage.
    for title in preferred_titles[:1]:
        if alias_mentions_issue(title, issue) or title_has_numbering(title):
            continue
        queries.extend(broad_series_query_variants(title, media_query_qualifier)[:2])
    if canonical_title and compact_volume_suffixes and not alias_mentions_issue(canonical_title, issue) and not title_has_numbering(canonical_title):
        for suffix in compact_volume_suffixes[:3]:
            queries.append(f"{canonical_title} {suffix}")
    if canonical_title and first_suffix and not alias_mentions_issue(canonical_title, issue) and not title_has_numbering(canonical_title):
        queries.append(f"{canonical_title} {first_suffix}")
    # The alternate title variant gets its bare and media-qualified forms too,
    # not only the later "complete"/"collection" ones below. A metadata title
    # can be a bad Soulseek query while the shelf name is a good one: uploaders
    # file "Naoki Urasawa's 20th Century Boys" under Manga/URASAWA Naoki/20th
    # Century Boys, so the canonical title matches almost no filenames. Live
    # counts for that series -- the canonical query returns 1 file from 1 peer
    # on every run, while "20th Century Boys manga" returns 1,058 files from 19
    # peers and was never being generated at all.
    #
    # These sit after the first numbered query on purpose. Finding the wanted
    # issue still leads; this only stops a bad canonical title from being the
    # only broad query a series ever gets.
    for title in preferred_titles[1:2]:
        if alias_mentions_issue(title, issue) or title_has_numbering(title):
            continue
        queries.extend(broad_series_query_variants(title, media_query_qualifier)[:2])
    for title in preferred_titles[:2]:
        if alias_mentions_issue(title, issue) or title_has_numbering(title):
            continue
        queries.extend(broad_series_query_variants(title, media_query_qualifier)[2:6])
    if canonical_title and not alias_mentions_issue(canonical_title, issue) and not title_has_numbering(canonical_title):
        for suffix in compact_volume_suffixes[:1]:
            queries.append(f"{canonical_title} {suffix}")
    if canonical_title and not alias_mentions_issue(canonical_title, issue) and not title_has_numbering(canonical_title):
        for suffix in compact_volume_suffixes[1:4]:
            queries.append(f"{canonical_title} {suffix}")
    if metadata_query:
        queries.append(metadata_query)
    if canonical_title and not alias_mentions_issue(canonical_title, issue):
        for issue_title in issue_titles[:2]:
            queries.append(f"{canonical_title} {issue_title}")
            if first_suffix and not title_has_numbering(canonical_title):
                queries.append(f"{canonical_title} {first_suffix} {issue_title}")
    for title in preferred_titles[1:4]:
        if first_suffix and not alias_mentions_issue(title, issue) and not title_has_numbering(title):
            queries.append(f"{title} {first_suffix}")
    if canonical_title and not alias_mentions_issue(canonical_title, issue) and not title_has_numbering(canonical_title):
        for suffix in early_suffixes[1:]:
            queries.append(f"{canonical_title} {suffix}")
            for issue_title in issue_titles[:1]:
                queries.append(f"{canonical_title} {suffix} {issue_title}")
    for title in preferred_titles:
        if alias_mentions_issue(title, issue):
            queries.append(title)
            continue
        if first_suffix and not title_has_numbering(title):
            queries.append(f"{title} {first_suffix}")
    for title in preferred_titles[:3]:
        if alias_mentions_issue(title, issue) or title_has_numbering(title):
            continue
        for suffix in graphic_suffixes[:6]:
            queries.append(f"{title} {suffix}")
    for title in preferred_titles:
        if alias_mentions_issue(title, issue):
            continue
        for issue_title in issue_titles[:2]:
            queries.append(f"{title} {issue_title}")
    for title in preferred_titles:
        if alias_mentions_issue(title, issue):
            continue
        if first_suffix and not title_has_numbering(title):
            for issue_title in issue_titles[:2]:
                queries.append(f"{title} {first_suffix} {issue_title}")
    for title in preferred_titles:
        if alias_mentions_issue(title, issue) or title_has_numbering(title):
            continue
        for issue_title in issue_titles[:2]:
            for suffix in graphic_suffixes[:6]:
                queries.append(f"{title} {suffix} {issue_title}")
    for issue_title in issue_titles:
        queries.append(issue_title)
    issue_year = str(metadata.get("date") or "")[:4] if metadata.get("date") else ""
    if not issue_year and metadata.get("year"):
        issue_year = str(metadata.get("year") or "")
    if issue_year:
        for title in preferred_titles[:2]:
            if suffixes and not title_has_numbering(title):
                queries.append(f"{title} {suffixes[0]} {issue_year}")
            for issue_title in issue_titles[:1]:
                queries.append(f"{title} {issue_title} {issue_year}")
    suffixable_titles = [
        title
        for title in preferred_titles
        if not alias_mentions_issue(title, issue) and not title_has_numbering(title)
    ]
    suffix_budget = 12 if len(suffixable_titles) <= 1 else 8
    for suffix in suffixes[1:1 + suffix_budget]:
        for title in suffixable_titles[:3]:
            queries.append(f"{title} {suffix}")
    for query in item.get("tried_queries") or []:
        queries.append(str(query or "").strip())
    queries.extend(variants)
    if series:
        queries.append(series)
    return unique_values(queries, limit=36)


def manual_search_query_variants(item, explicit_queries=None):
    """Prioritize high-recall SLSKD variants for one explicit operator search."""

    item = dict(item or {})
    raw_title = str(item.get("series") or item.get("series_title") or item_series_title(item)).strip()
    clean_title = display_clean(raw_title)
    import inkdrop_manual_search

    structured_aliases = inkdrop_manual_search.collected_title_aliases(raw_title)
    prefixless = display_clean(structured_aliases[0] if structured_aliases else clean_title)
    if len(important_words(prefixless)) < 2:
        prefixless = clean_title
    split_titles = title_variants(raw_title)
    short_title = next((without_leading_article(value) for value in reversed(split_titles) if len(important_words(value)) >= 2), "")
    number = issue_number(item.get("issue"))
    year = str(item.get("year") or "").strip()
    anchors = [clean_title]
    if number is not None:
        anchors.append(f"{clean_title} {number}")
    anchors.append(prefixless)
    if number is not None:
        anchors.append(f"{prefixless} v{number:02d}")
    if prefixless != clean_title:
        anchors.append(f"{prefixless} omnibus")
    anchors.append(short_title)
    if number is not None and year:
        anchors.append(f"{prefixless} {number:03d} {year}")
    elif number is not None:
        anchors.append(f"{short_title or prefixless} {number:03d}")
    # The Manual Search core has already built a bounded, metadata-aware plan.
    # Execute it first.  Previously SLSKD put six locally-derived anchors ahead
    # of that plan, so a 25 second provider deadline could expire before the
    # identity-preserving alias was ever attempted.
    return unique_values([*(explicit_queries or []), *anchors, *source_queries(item)], limit=36)


def query_signature(queries):
    raw = f"v{QUERY_PLAN_VERSION}|" + "|".join(normalize(query) for query in queries or [])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def query_anchor_count(query_count, max_queries):
    if query_count <= 0:
        return 0
    try:
        limit = max(0, min(int(max_queries or 0), query_count))
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return 0
    return min(limit, query_count, 1)


def rotated_query_batch(queries, max_queries=2, offset=0, include_anchor=True):
    queries = list(queries or [])
    if not queries:
        return []
    try:
        offset = int(offset or 0)
    except (TypeError, ValueError):
        offset = 0
    offset = offset % len(queries)
    limit = max(0, min(int(max_queries or 0), len(queries)))
    if limit <= 0:
        return []
    anchor_count = query_anchor_count(len(queries), limit) if include_anchor or len(queries) <= 1 else 0
    anchors = queries[:anchor_count]
    if not anchor_count and len(queries) > 1:
        limit = min(limit, len(queries) - 1)
    if len(anchors) >= limit:
        return anchors
    # Query zero is the high-confidence anchor.  A same-plan no-candidate
    # retry has already paid for that anchor, so preserve it as an identity
    # boundary while rotating only the remaining variants.
    remaining = queries[anchor_count if anchor_count else (1 if len(queries) > 1 else 0):]
    if not remaining:
        return anchors
    offset = offset % len(remaining)
    rotated = [remaining[(offset + index) % len(remaining)] for index in range(limit - len(anchors))]
    return anchors + rotated


def next_query_offset(queries, start_offset, attempted_count, max_queries=None, include_anchor=True):
    queries = list(queries or [])
    if not queries:
        return 0
    try:
        start_offset = int(start_offset or 0)
    except (TypeError, ValueError):
        start_offset = 0
    try:
        attempted_count = int(attempted_count or 0)
    except (TypeError, ValueError):
        attempted_count = 0
    try:
        query_limit = int(max_queries if max_queries is not None else attempted_count)
    except (TypeError, ValueError):
        query_limit = attempted_count
    anchor_count = query_anchor_count(len(queries), query_limit) if include_anchor or len(queries) <= 1 else 0
    rotating_attempts = max(0, min(max(0, attempted_count), max(0, query_limit)) - anchor_count)
    if rotating_attempts <= 0:
        return start_offset % len(queries)
    remaining_count = max(1, len(queries) - (anchor_count if anchor_count else (1 if len(queries) > 1 else 0)))
    return (start_offset + rotating_attempts) % remaining_count


def query_attempt_completed_clean_zero(attempt):
    if not isinstance(attempt, dict):
        return False
    if (
        attempt.get("skipped")
        or attempt.get("error")
        or attempt.get("transient_error")
        or attempt.get("partial")
        or attempt.get("partial_reason")
        or attempt.get("cancelled")
    ):
        return False
    status = str(attempt.get("status") or "").strip().lower()
    if status not in {"", "completed", "zero_results"}:
        return False
    response_count = attempt.get("response_count")
    candidate_count = attempt.get("candidate_count")
    if type(response_count) is not int or type(candidate_count) is not int:
        return False
    return response_count >= 0 and candidate_count == 0


def query_rotation_evidence(queries, attempts):
    attempts = attempts if isinstance(attempts, list) else []
    # A probe that runs out of provider budget records the queries it never
    # sent. Those markers report nothing about the queries that did run, so
    # they must not contradict them. Counting them as failures pinned every
    # budget-truncated probe to query zero forever: a one-call budget can only
    # afford the anchor, the anchor's own clean zero then failed to qualify,
    # and the next pass re-ran that same anchor instead of a fresh variant.
    executed = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict) and not attempt.get("skipped")
    ]
    qualified = bool(executed) and all(query_attempt_completed_clean_zero(attempt) for attempt in executed)
    return {
        "version": QUERY_ROTATION_EVIDENCE_VERSION,
        "query_signature": query_signature(queries),
        "all_attempts_completed_clean_zero": qualified,
        "completed_attempt_count": len(executed) if qualified else 0,
    }


def retry_rotates_without_anchor(queries, cache_entry, refresh_reason="", force=False):
    if force or refresh_reason or not queries or not isinstance(cache_entry, dict):
        return False
    if str(cache_entry.get("status") or "") != "searched_no_candidates":
        return False
    signature = query_signature(queries)
    if cache_entry.get("query_signature") != signature:
        return False
    evidence = cache_entry.get("query_rotation_evidence")
    if not isinstance(evidence, dict):
        return False
    version = evidence.get("version")
    completed_attempt_count = evidence.get("completed_attempt_count")
    if type(version) is not int or type(completed_attempt_count) is not int:
        return False
    attempts = cache_entry.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return False
    # The stored marker still has to describe this exact attempt list; the
    # recomputed comparison below is the check that enforces it. Comparing the
    # count against len(attempts) instead would reject any probe that recorded
    # a budget skip, which is the case this rotation exists to serve.
    recomputed = query_rotation_evidence(queries, attempts)
    return bool(
        version == QUERY_ROTATION_EVIDENCE_VERSION
        and evidence.get("query_signature") == signature
        and evidence.get("all_attempts_completed_clean_zero") is True
        and completed_attempt_count > 0
        and recomputed.get("all_attempts_completed_clean_zero") is True
        and recomputed.get("completed_attempt_count") == completed_attempt_count
        and recomputed.get("query_signature") == signature
    )


def derive_query_offset(queries, cache_entry, refresh_reason="", force=False):
    queries = list(queries or [])
    if force or not queries or not isinstance(cache_entry, dict):
        return 0
    # Only a changed query plan invalidates the stored rotation position --
    # the offset was computed against a query list that no longer exists.
    # Every other refresh reason (candidate_recheck, retry_probe_error,
    # schema/verdict upgrades) previously hit a blanket "return 0" too, which
    # meant a periodically-rechecked item (has cached candidates but nothing
    # passed matching) restarted at the anchor query every single recheck
    # and never rotated through the rest of the query list.
    if refresh_reason == "query_plan_changed":
        return 0
    if str(cache_entry.get("status") or "") not in {"searched_no_candidates", "no_query"}:
        return 0
    if cache_entry.get("query_signature") == query_signature(queries):
        try:
            return int(cache_entry.get("next_query_offset") or 0) % len(queries)
        except (TypeError, ValueError):
            return 0
    previous = [
        str(attempt.get("query") or "")
        for attempt in (cache_entry.get("queries") or [])
        if isinstance(attempt, dict) and attempt.get("query")
    ]
    positions = [queries.index(query) for query in previous if query in queries]
    if positions:
        return (max(positions) + 1) % len(queries)
    return 0


def token_number(value):
    raw = str(value or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        try:
            return int(float(raw))
        except ValueError:
            return None
    word = normalize(raw)
    if re.fullmatch(r"[a-z]+", word):
        return NUMBER_WORD_VALUES.get(word)
    return None


def slskd_api_key():
    global SLSKD_API_KEY_CACHE
    instance_id = str(SLSKD_PROVIDER_SETTINGS.get("download_client_instance_id") or "").strip()
    if instance_id:
        selected = inkdrop_download_client_routing.slskd_source_instance(INKDROP_STATE_DB, "comics", materialize=True)
        if not selected or selected.get("instance_id") != instance_id:
            raise RuntimeError("selected SLSKD instance is no longer enabled and ready")
        api_key = str((selected.get("settings") or {}).get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("selected SLSKD instance API key is unavailable")
        return api_key
    config = provider_config("slskd") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("SLSKD provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    provider_key = str(settings.get("api_key") or "").strip()
    if provider_key:
        SLSKD_API_KEY_CACHE = provider_key
        return provider_key
    if SLSKD_API_KEY_CACHE:
        return SLSKD_API_KEY_CACHE
    text = read_slskd_config_text()
    if not text:
        raise RuntimeError("SLSKD API key is not set in InkDrop settings and slskd config could not be read")
    match = re.search(r"^\s*key:\s*([^\s#]+)", text, flags=re.M)
    if not match:
        raise RuntimeError("SLSKD API key is not set in InkDrop settings or slskd config")
    SLSKD_API_KEY_CACHE = match.group(1)
    return SLSKD_API_KEY_CACHE


def slskd_headers():
    return {"X-API-Key": slskd_api_key(), "Content-Type": "application/json"}


def slskd_curl_request(method, path, payload=None, timeout=15):
    timeout = max(1, int(timeout or 1))
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-curl-") as tmpdir:
        tmpdir = Path(tmpdir)
        stdout_path = tmpdir / "stdout.json"
        stderr_path = tmpdir / "stderr.txt"
        payload_path = tmpdir / "payload.json"
        cmd = [
            "/usr/bin/curl",
            "-fsS",
            "--max-time",
            str(timeout),
            "-X",
            method.upper(),
            "-H",
            f"X-API-Key: {slskd_api_key()}",
            "-H",
            "Content-Type: application/json",
            SLSKD_BASE_URL + path,
        ]
        if payload is not None:
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            cmd.extend(["--data-binary", f"@{payload_path}"])
        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
            proc = subprocess.Popen(cmd, stdout=stdout_handle, stderr=stderr_handle, text=True)
            deadline = now() + timeout + 5
            while proc.poll() is None and now() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                raise TimeoutError(f"SLSKD curl request exceeded {timeout}s: {method.upper()} {path}")
            returncode = int(proc.returncode or 0)
        stdout = stdout_path.read_text(encoding="utf-8", errors="ignore").strip() if stdout_path.exists() else ""
        stderr = stderr_path.read_text(encoding="utf-8", errors="ignore").strip() if stderr_path.exists() else ""
    if returncode != 0:
        detail = stderr or stdout or f"SLSKD curl request failed: {returncode}"
        raise RuntimeError(f"{method.upper()} {path}: {detail}")
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"SLSKD returned non-JSON response: {stdout[:500]}") from exc


def raise_for_status_with_body(response):
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = str(response.text or "").strip()
        if body:
            raise requests.HTTPError(f"{exc}; body={body[:500]}", response=response) from exc
        raise


def slskd_get(path, timeout=15):
    return slskd_curl_request("GET", path, timeout=timeout)


def slskd_post(path, payload, timeout=30):
    return slskd_curl_request("POST", path, payload=payload, timeout=timeout)


def slskd_conflict_error(exc):
    text = str(exc or "").lower()
    return "returned error: 409" in text or "http 409" in text or "status 409" in text


def slskd_unavailable_error(exc):
    text = str(exc or "").lower()
    search_endpoint_failure = "/searches" in text and (
        "returned error: 500" in text
        or "http 500" in text
        or "status 500" in text
        or "internal server error" in text
    )
    return (
        "must be connected and logged in" in text
        or "currently: disconnecting" in text
        or "currently: disconnected" in text
        or "currently: connecting" in text
        or "sqlite error 19" in text
        or "unique constraint failed: searches.id" in text
        or search_endpoint_failure
    )


def slskd_server_status(max_age_seconds=5):
    cached_at = float(SLSKD_SERVER_STATUS_CACHE.get("ts") or 0)
    cached = SLSKD_SERVER_STATUS_CACHE.get("status")
    if isinstance(cached, dict) and cached_at > now() - max(0, int(max_age_seconds or 0)):
        return cached
    status = slskd_get("/server", timeout=5) or {}
    if not isinstance(status, dict):
        status = {}
    SLSKD_SERVER_STATUS_CACHE["ts"] = now()
    SLSKD_SERVER_STATUS_CACHE["status"] = status
    return status


def require_slskd_ready_for_search():
    try:
        status = slskd_server_status()
    except Exception as exc:
        raise SLSKDProviderUnavailable(
            f"SLSKD server status check failed: {type(exc).__name__}: {exc}"
        ) from exc
    connected = bool(status.get("isConnected"))
    logged_in = bool(status.get("isLoggedIn"))
    transitioning = bool(status.get("isTransitioning"))
    state = str(status.get("state") or "").strip()
    if connected and logged_in and not transitioning:
        return status
    detail = state or "not connected"
    raise SLSKDProviderUnavailable(
        f"SLSKD is not ready for search: {detail}; connected={connected}; logged_in={logged_in}",
        status=status,
    )


def seconds_remaining(deadline):
    if not deadline:
        return None
    return max(0.0, float(deadline) - now())


def merge_slskd_search_responses(existing, observed):
    """Accumulate streaming peer responses without duplicating snapshots."""

    peers = {}
    order = []
    for response in [*(existing or []), *(observed or [])]:
        if not isinstance(response, dict):
            continue
        peer = str(response.get("username") or response.get("user") or response.get("id") or "").strip()
        key = peer.casefold() or hashlib.sha256(
            json.dumps(response, sort_keys=True, default=str).encode("utf-8", errors="replace")
        ).hexdigest()[:20]
        if key not in peers:
            peers[key] = dict(response)
            peers[key]["files"] = []
            order.append(key)
        target = peers[key]
        target.update({name: value for name, value in response.items() if name != "files"})
        seen_files = {
            (str(row.get("filename") or row.get("path") or "").casefold(), int(row.get("size") or 0))
            for row in target.get("files") or [] if isinstance(row, dict)
        }
        for file_row in response.get("files") or []:
            if not isinstance(file_row, dict):
                continue
            file_key = (
                str(file_row.get("filename") or file_row.get("path") or "").casefold(),
                int(file_row.get("size") or 0),
            )
            if file_key in seen_files:
                continue
            seen_files.add(file_key)
            target["files"].append(dict(file_row))
    return [peers[key] for key in order]


DEFAULT_SLSKD_SEARCH_MIN_INTERVAL_SECONDS = 5.0
DEFAULT_SLSKD_SEARCH_MAX_PER_HOUR = 60
DEFAULT_SLSKD_REPEAT_QUERY_COOLDOWN_SECONDS = 24 * 3600
# One peer answering is a search that got cut short, not an answer about what
# the network holds.
MIN_REUSABLE_SEARCH_RESPONSES = 2


def slskd_search_min_interval_seconds():
    try:
        value = float(
            os.environ.get("INKDROP_SLSKD_SEARCH_MIN_INTERVAL_SECONDS")
            or DEFAULT_SLSKD_SEARCH_MIN_INTERVAL_SECONDS
        )
    except (TypeError, ValueError):
        value = DEFAULT_SLSKD_SEARCH_MIN_INTERVAL_SECONDS
    return max(0.0, min(value, 300.0))


def slskd_search_max_per_hour():
    try:
        value = int(
            os.environ.get("INKDROP_SLSKD_SEARCH_MAX_PER_HOUR")
            or DEFAULT_SLSKD_SEARCH_MAX_PER_HOUR
        )
    except (TypeError, ValueError):
        value = DEFAULT_SLSKD_SEARCH_MAX_PER_HOUR
    return max(1, min(value, 1000))


def _parse_slskd_started_at(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    head = text.split(".", 1)[0]
    if not head.endswith("Z"):
        head = head + "Z"
    try:
        return float(calendar.timegm(time.strptime(head, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def recent_slskd_search_start_times(max_age_seconds=3600):
    """Recent search-initiation times, read from SLSKD's own /searches history.

    Every InkDrop process that starts searches (manual search threads, the
    scheduler poller, series autopilot) talks to the same SLSKD daemon, so
    its history is shared state across processes without a separate lock
    file or state store to keep in sync.
    """
    try:
        rows = slskd_get("/searches", timeout=5)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    cutoff = now() - max(0, int(max_age_seconds or 0))
    starts = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        started_at = _parse_slskd_started_at(row.get("startedAt"))
        if started_at is not None and started_at >= cutoff:
            starts.append(started_at)
    return starts


def slskd_repeat_query_cooldown_seconds():
    try:
        value = float(
            os.environ.get("INKDROP_SLSKD_REPEAT_QUERY_COOLDOWN_SECONDS")
            or DEFAULT_SLSKD_REPEAT_QUERY_COOLDOWN_SECONDS
        )
    except (TypeError, ValueError):
        value = DEFAULT_SLSKD_REPEAT_QUERY_COOLDOWN_SECONDS
    return max(0.0, min(value, 7 * 24 * 3600))


def reusable_slskd_search(query, max_age_seconds):
    """The most recent finished search for this exact text, if it still stands.

    Soulseek has no index to re-crawl: a search asks whoever is online right
    now what they are sharing. Asking the same question again an hour later
    gets the same peers and the same files back. Live check on this deployment:
    "Vagabond manga" ran 16 times in 19 hours and returned ~1,700 files every
    time, and one wanted series can hold ~100 rows that all build the same
    broad series query, so the per-row cooldown never sees a repeat.

    Reuse what SLSKD already stored instead of starting another real search on
    the operator's account. Only reuse a search that actually found something --
    if the last identical query came back empty, let a new one run, so this can
    never pin a row to an old miss.

    "Finished" is not the same as "representative", and the difference is not
    cosmetic. A Soulseek search ends either because it ran out of room for
    replies or because it ran out of time, and the timed-out ones on this
    deployment range from 0 to 185 peers. The live search for "Injustice Gods
    Among Us comic" timed out having heard back from exactly one peer, while
    the run of single issues it was looking for is widely shared. Reusing that
    for a day would pin the series to one peer's shelf.

    So reuse only the strongest snapshot this query has produced inside the
    window, and never a single-peer answer. A thinner, newer search does not
    displace a richer one, and a query that has only ever answered thinly is
    left to run again.
    """
    if max_age_seconds <= 0:
        return None
    wanted = normalize(query)
    if not wanted:
        return None
    try:
        rows = slskd_get("/searches", timeout=5)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    cutoff = now() - max_age_seconds
    matching = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("isComplete"):
            continue
        if normalize(row.get("searchText")) != wanted:
            continue
        if int(row.get("fileCount") or 0) <= 0:
            continue
        started_at = _parse_slskd_started_at(row.get("startedAt"))
        if started_at is None or started_at < cutoff:
            continue
        matching.append((started_at, int(row.get("responseCount") or 0), row))
    if not matching:
        return None
    best_responses = max(responses for _, responses, _ in matching)
    if best_responses < MIN_REUSABLE_SEARCH_RESPONSES:
        return None
    # Newest among the richest, so reuse tracks the best answer we have seen
    # rather than the most recent one.
    richest = [row for row in matching if row[1] >= best_responses]
    richest.sort(key=lambda row: row[0], reverse=True)
    return richest[0][2]


def enforce_slskd_search_pacing(deadline=None):
    """Keep new Soulseek-network search initiations at a safe, human-plausible pace.

    Nothing else in this codebase paces POST /searches calls today: each one
    starts a real query against the Soulseek network under the operator's
    account, and there is no provider in front of SLSKD to absorb a burst the
    way Prowlarr absorbs bursts against trackers. This is a self-imposed
    floor, not a reaction to SLSKD rejecting anything, so it fails open if
    the history lookup itself is unavailable rather than blocking a probe on
    a diagnostic call.
    """
    starts = recent_slskd_search_start_times()
    if not starts:
        return
    starts.sort()
    max_per_hour = slskd_search_max_per_hour()
    if len(starts) >= max_per_hour:
        raise SLSKDProviderUnavailable(
            f"SLSKD search pace limit reached: {len(starts)} searches already started in the "
            f"last hour (budget {max_per_hour}/hour)"
        )
    wait_for = slskd_search_min_interval_seconds() - (now() - starts[-1])
    if wait_for > 0:
        remaining = seconds_remaining(deadline)
        if remaining is not None and wait_for >= remaining:
            raise SLSKDProviderUnavailable(
                f"SLSKD search pace limit requires a {wait_for:.1f}s gap since the last search "
                f"but only {remaining:.1f}s remain in this probe's budget"
            )
        time.sleep(wait_for)


def slskd_search(query, wait_seconds=8, deadline=None, reuse_recent_seconds=0):
    remaining = seconds_remaining(deadline)
    if remaining is not None and remaining < 1:
        raise TimeoutError("SLSKD probe budget exhausted before query")
    require_slskd_ready_for_search()
    # Automatic passes opt into reuse; a person who just pressed Search has not,
    # and always gets a live query.
    recent = reusable_slskd_search(query, reuse_recent_seconds) if reuse_recent_seconds else None
    if recent:
        try:
            reused = slskd_get(
                f"/searches/{recent.get('id')}/responses",
                timeout=min(15, max(1, int(seconds_remaining(deadline) or 15))),
            )
        except Exception:
            reused = None
        if isinstance(reused, list) and reused:
            log(
                "slskd_search_reused_recent",
                query=query,
                started_at=recent.get("startedAt"),
                age_seconds=round(now() - (_parse_slskd_started_at(recent.get("startedAt")) or now()), 1),
                file_count=recent.get("fileCount"),
                response_count=len(reused),
            )
            return reused
    enforce_slskd_search_pacing(deadline)
    search_id = str(uuid.uuid4())
    conflict_errors = []
    for attempt in range(4):
        try:
            slskd_post(
                "/searches",
                {"id": search_id, "searchText": query},
                timeout=min(15, max(1, int(seconds_remaining(deadline) or 15))),
            )
            break
        except RuntimeError as exc:
            if slskd_unavailable_error(exc):
                raise SLSKDProviderUnavailable(f"SLSKD is not ready for search: {exc}") from exc
            if not slskd_conflict_error(exc):
                raise
            conflict_errors.append(str(exc))
            remaining = seconds_remaining(deadline)
            if attempt >= 3 or (remaining is not None and remaining < 2):
                raise RuntimeError(
                    "SLSKD search conflict after retries; Soulseek may still be reconnecting"
                ) from exc
            time.sleep(min(2 + attempt * 2, max(0, int((remaining or 3) - 1))))
    query_window = max(2.0, min(float(wait_seconds), 55.0))
    query_deadline = now() + query_window
    if deadline is not None:
        query_deadline = min(query_deadline, max(now(), float(deadline) - 1.0))
    responses = []
    poll_count = 0
    last_growth_at = now()
    last_file_count = 0
    # Poll because Soulseek peers answer over time. A single GET after a fixed
    # sleep repeatedly observed an empty snapshot even though the same search
    # populated moments later in SLSKD. Stop after a short quiet period once
    # useful responses exist, otherwise use the bounded query window.
    while now() < query_deadline:
        sleep_for = min(1.0, max(0.0, query_deadline - now()))
        if sleep_for:
            time.sleep(sleep_for)
        remaining = seconds_remaining(deadline)
        if remaining is not None and remaining < 1:
            break
        observed = slskd_get(
            f"/searches/{search_id}/responses",
            timeout=min(5, max(1, int(remaining or 5))),
        ) or []
        poll_count += 1
        responses = merge_slskd_search_responses(responses, observed)
        file_count = sum(len(row.get("files") or []) for row in responses if isinstance(row, dict))
        if file_count > last_file_count:
            last_file_count = file_count
            last_growth_at = now()
        elif file_count and now() - last_growth_at >= 2.0:
            break
    if responses:
        return responses
    remaining = seconds_remaining(deadline)
    if remaining is not None and remaining < 1 and poll_count <= 0:
        raise TimeoutError("SLSKD probe budget exhausted before collecting responses")
    return []


def extension_for(filename):
    return Path(str(filename or "").split("?")[0]).suffix.lower()


def path_segments(value):
    return [segment for segment in re.split(r"[\\/]+", str(value or "")) if segment]


def filename_leaf(value):
    segments = path_segments(value)
    return segments[-1] if segments else str(value or "")


def filename_match_values(value):
    text = str(value or "").replace("\\", "/").strip().lower()
    values = {text} if text else set()
    leaf = filename_leaf(text).lower()
    if leaf:
        values.add(leaf)
    return {value for value in values if value}


def manual_source_bad_candidate_rows(review_id):
    actions = load_actions()
    bad = actions.get("manual_source_bad_candidates")
    if not isinstance(bad, dict):
        return []
    rows = bad.get(str(review_id))
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def matching_bad_candidate_rows(review_id, candidate):
    candidate = candidate or {}
    candidate_values = set()
    for key in ("filename", "path", "filename_leaf", "remote_filename", "detected_path", "detected_filename"):
        candidate_values.update(filename_match_values(candidate.get(key)))
    if not candidate_values:
        return []
    candidate_user = normalize(candidate.get("username") or "")
    matches = []
    for row in manual_source_bad_candidate_rows(review_id):
        bad_values = set()
        for key in ("filename", "candidate_filename", "filename_leaf", "detected_path", "detected_filename"):
            bad_values.update(filename_match_values(row.get(key)))
        if not (candidate_values & bad_values):
            continue
        bad_user = normalize(row.get("username") or "")
        if candidate_user and bad_user and candidate_user != bad_user:
            continue
        matches.append(row)
    return matches


def candidate_source_memory_path(candidate):
    candidate = candidate or {}
    for key in ("filename", "path", "remote_filename", "detected_path", "detected_filename", "filename_leaf"):
        value = str(candidate.get(key) or "").strip()
        if value:
            return value
    return ""


def candidate_source_memory_title(candidate, source_path):
    if inkdrop_state is not None:
        try:
            title = inkdrop_state.bad_source_candidate_path_title(source_path)
        except Exception:
            title = ""
        if title:
            return title
    leaf = filename_leaf(source_path)
    if leaf:
        return re.sub(r"\.[^.\\/]+$", "", leaf).strip()
    return ""


def durable_bad_source_candidate_match(candidate):
    if inkdrop_state is None:
        return None
    candidate = candidate or {}
    provider = str(candidate.get("username") or candidate.get("provider") or candidate.get("user") or "").strip()
    if not provider:
        return None
    source_path = candidate_source_memory_path(candidate)
    title = candidate_source_memory_title(candidate, source_path)
    if not (source_path or title):
        return None
    try:
        row = inkdrop_state.find_bad_source_candidate(
            INKDROP_STATE_DB,
            title=title,
            source="slskd",
            provider=provider,
            protocol="soulseek",
            source_path=source_path,
        )
    except Exception:
        return None
    if not row:
        return None
    out = dict(row)
    reason = str(out.get("reason") or "bad_source_memory").strip() or "bad_source_memory"
    label = reason.replace("_", " ")
    try:
        last_seen = float(out.get("last_seen_at") or 0)
    except (TypeError, ValueError):
        last_seen = 0
    out["reason"] = reason
    out["failure_label"] = out.get("failure_label") or f"source memory: {label}"
    out["detail"] = out.get("detail") or out.get("source_path") or out.get("title") or source_path or title
    out["failure_kind"] = out.get("failure_kind") or "source_memory"
    out["ts_iso"] = out.get("ts_iso") or (utc_stamp(last_seen) if last_seen > 0 else "")
    out["detected_filename"] = out.get("detected_filename") or source_path or out.get("source_path") or title
    out["source_memory"] = True
    out["source_memory_id"] = out.get("id")
    return out


def bad_candidate_match(review_id, candidate):
    for row in matching_bad_candidate_rows(review_id, candidate):
        if transient_bad_candidate_retry_ready(row):
            continue
        return row
    durable_match = durable_bad_source_candidate_match(candidate)
    if durable_match:
        return durable_match
    return None


def transient_bad_candidate_retry_match(review_id, candidate):
    for row in matching_bad_candidate_rows(review_id, candidate):
        if transient_bad_candidate_retry_ready(row):
            return row
    return None


def transient_bad_candidate_retry_ready(row):
    if not isinstance(row, dict):
        return False
    if str(row.get("reason") or "") not in TRANSIENT_BAD_CANDIDATE_REASONS:
        return False
    try:
        ts = float(row.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0 and row.get("ts_iso"):
        try:
            ts = float(calendar.timegm(time.strptime(str(row.get("ts_iso")), "%Y-%m-%dT%H:%M:%SZ")))
        except (TypeError, ValueError, OverflowError):
            ts = 0
    if ts <= 0:
        return False
    return (now() - ts) >= TRANSIENT_BAD_CANDIDATE_RETRY_SECONDS


def cached_bad_candidate_match(candidate, key):
    """Return active cached failure evidence, clearing expired transient rows."""

    row = candidate.get(key)
    if not isinstance(row, dict):
        return None
    if transient_bad_candidate_retry_ready(row):
        candidate.pop(key, None)
        return None
    return dict(row)


def entry_has_unfailed_detected_file(review_id, entry):
    for detected in (entry or {}).get("detected_files") or []:
        if not isinstance(detected, dict):
            continue
        candidate = {
            "filename": detected.get("filename"),
            "path": detected.get("path"),
            "detected_filename": detected.get("filename"),
            "detected_path": detected.get("path"),
        }
        if not bad_candidate_match(str(review_id or ""), candidate):
            return True
    return False


def filename_stem(value):
    leaf = filename_leaf(value)
    return re.sub(r"\.[a-z0-9]{1,6}$", "", leaf, flags=re.I)


def slskd_learning_data():
    global SLSKD_LEARNING_CACHE
    if SLSKD_LEARNING_CACHE is None:
        data = read_json(SLSKD_LEARNING_FILE, {}) or {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("users", {})
        data.setdefault("path_styles", {})
        data.setdefault("extensions", {})
        SLSKD_LEARNING_CACHE = data
    return SLSKD_LEARNING_CACHE


def stable_payload_signature(payload):
    try:
        encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    except TypeError:
        encoded = repr(payload)
    return hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()[:20]


def auto_grab_context_signature():
    actions = load_actions()
    learning = slskd_learning_data()
    return stable_payload_signature({
        "schema": PROBE_SCHEMA_VERSION,
        "preferred_exact_min_bytes": SLSKD_PREFERRED_EXACT_MIN_BYTES,
        "language_policy": {
            "preferred_language": QUALITY_LANGUAGE_RULES.get("preferred_language"),
            "pdf_allowed": bool(QUALITY_LANGUAGE_RULES.get("pdf_allowed", True)),
            "packs_allowed": bool(QUALITY_LANGUAGE_RULES.get("packs_allowed", True)),
            "allowed_extensions": sorted(QUALITY_LANGUAGE_RULES.get("allowed_extensions") or []),
            "blocked_release_terms": sorted(QUALITY_LANGUAGE_RULES.get("blocked_release_terms") or []),
            "explicit_english_translation_markers": sorted(EXPLICIT_ENGLISH_TRANSLATION_MARKERS),
            "english_release_markers": sorted(ENGLISH_RELEASE_MARKERS),
            "manga_publisher_phrases": sorted(MANGA_PUBLISHER_PHRASES),
            "non_english_collection_markers": sorted(NON_ENGLISH_COLLECTION_MARKERS),
            "non_english_language_markers": sorted(NON_ENGLISH_LANGUAGE_MARKERS),
            "source_language_blocker": 2,
            "same_series_path_language_blocker": 1,
            "ambiguous_hq_source_folder_blocker": 1,
            "western_comic_publisher_phrases": sorted(WESTERN_COMIC_PUBLISHER_PHRASES),
            "western_comic_source_confidence_blocker": 1,
        },
        "bad_candidates": actions.get("manual_source_bad_candidates") or {},
        "learning": {
            "users": (learning or {}).get("users") or {},
            "path_styles": (learning or {}).get("path_styles") or {},
            "extensions": (learning or {}).get("extensions") or {},
        },
    })


def slskd_learning_path_style(filename):
    parts = path_segments(filename)
    if not parts:
        return ""
    ext = extension_for(parts[-1])
    parent = normalize(parts[-2]) if len(parts) >= 2 else "root"
    context = ""
    for part in reversed(parts[:-1]):
        key = normalize(part)
        words = set(key.split())
        if words & (COMIC_CONTEXT_WORDS | {"graphic", "novel", "novels"}):
            context = key
            break
    return "|".join(part for part in (context, parent, ext) if part)


def slskd_learning_path_style_variants(filename):
    parts = path_segments(filename)
    variants = []
    full_style = slskd_learning_path_style(filename)
    if full_style:
        variants.append(full_style)
    if parts:
        ext = extension_for(parts[-1])
        parent = normalize(parts[-2]) if len(parts) >= 2 else "root"
        parent_style = "|".join(part for part in (parent, ext) if part)
        if parent_style and parent_style not in variants:
            variants.append(parent_style)
    return variants


def slskd_learning_entry_score(entry, success_weight, failure_weight, cap):
    if not isinstance(entry, dict):
        return 0
    try:
        successes = int(entry.get("successes") or 0)
        failures = int(entry.get("failures") or 0)
    except (TypeError, ValueError):
        return 0
    value = (successes * success_weight) - (failures * failure_weight)
    return max(-cap, min(cap, value))


def slskd_learning_adjustment(candidate):
    data = slskd_learning_data()
    filename = str((candidate or {}).get("filename") or "")
    username = normalize((candidate or {}).get("username") or "")
    path_style = slskd_learning_path_style(filename)
    ext = extension_for(filename)
    score = 0
    notes = []

    user_score = slskd_learning_entry_score((data.get("users") or {}).get(username), 2, 5, 8) if username else 0
    if user_score:
        score += user_score
        notes.append(f"SLSKD user history {user_score:+d}")
    style_score = slskd_learning_entry_score((data.get("path_styles") or {}).get(path_style), 1, 3, 5) if path_style else 0
    if style_score:
        score += style_score
        notes.append(f"SLSKD path history {style_score:+d}")
    ext_score = slskd_learning_entry_score((data.get("extensions") or {}).get(ext), 1, 2, 3) if ext else 0
    if ext_score:
        score += ext_score
        notes.append(f"SLSKD extension history {ext_score:+d}")
    return score, notes


def slskd_learning_same_series_language_history(entry, series_key):
    latest_language_failure = 0.0
    latest_series_success = 0.0
    language_failures = 0
    for example in (entry or {}).get("examples") or []:
        if not isinstance(example, dict):
            continue
        if normalize(example.get("series") or "") != series_key:
            continue
        try:
            ts = float(example.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if example.get("success"):
            latest_series_success = max(latest_series_success, ts)
            continue
        reason = normalize(example.get("reason") or "")
        if "wrong language" in reason or "wrong_language" in reason or "language source" in reason:
            language_failures += 1
            latest_language_failure = max(latest_language_failure, ts)
    return latest_language_failure, latest_series_success, language_failures


def slskd_learning_series_key(item):
    series_label = str((item or {}).get("series") or (item or {}).get("query") or "").strip()
    series_key = normalize(series_label)
    return series_label, series_key


def slskd_learning_same_series_language_blocker(candidate, item):
    data = slskd_learning_data()
    username = normalize((candidate or {}).get("username") or "")
    if not username:
        return ""
    entry = (data.get("users") or {}).get(username)
    if not isinstance(entry, dict):
        return ""
    series_label, series_key = slskd_learning_series_key(item)
    if not series_key:
        return ""

    latest_language_failure, latest_series_success, _ = slskd_learning_same_series_language_history(entry, series_key)

    if latest_language_failure and latest_series_success < latest_language_failure:
        user_label = (candidate or {}).get("username") or username
        return f"SLSKD user {user_label} previously supplied wrong-language {series_label}"
    return ""


def slskd_learning_same_series_path_language_blocker(candidate, item):
    data = slskd_learning_data()
    filename = str((candidate or {}).get("filename") or "")
    path_styles = slskd_learning_path_style_variants(filename)
    if not path_styles:
        return ""
    series_label, series_key = slskd_learning_series_key(item)
    if not series_key:
        return ""

    for path_style in path_styles:
        entry = (data.get("path_styles") or {}).get(path_style)
        if not isinstance(entry, dict):
            continue
        latest_language_failure, latest_series_success, language_failures = slskd_learning_same_series_language_history(
            entry, series_key
        )
        if language_failures >= 1 and latest_language_failure and latest_series_success < latest_language_failure:
            return f"SLSKD source pattern {path_style} previously supplied wrong-language {series_label}"
    return ""


def slskd_learning_language_blockers(candidate, item):
    blockers = []
    for blocker in (
        slskd_learning_same_series_language_blocker(candidate, item),
        slskd_learning_same_series_path_language_blocker(candidate, item),
    ):
        if blocker and blocker not in blockers:
            blockers.append(blocker)
    return blockers


def identity_issue_number_value(value):
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return int(float(match.group(0)))
    except (TypeError, ValueError):
        return None


def identity_issue_numbers_from_row(row):
    if not isinstance(row, dict):
        return []
    numbers = set()

    def add_value(value):
        number = identity_issue_number_value(value)
        if number is not None:
            numbers.add(number)

    for key in ("issue", "issue_number", "issueNumber", "number", "current_issue"):
        add_value(row.get(key))

    known = row.get("knownIssues")
    if isinstance(known, dict):
        for issue in known.values():
            if isinstance(issue, dict):
                add_value(issue.get("issueNumber") or issue.get("issue") or issue.get("number"))

    for key in ("missingIssues", "issues", "newIssues"):
        rows = row.get(key)
        if isinstance(rows, list):
            for issue in rows:
                if isinstance(issue, dict):
                    add_value(issue.get("issueNumber") or issue.get("issue") or issue.get("number"))

    return sorted(numbers)


def identity_context_rows():
    global IDENTITY_CONTEXT_CACHE
    if IDENTITY_CONTEXT_CACHE is not None:
        return IDENTITY_CONTEXT_CACHE
    rows = []

    def add_row(source, row):
        if not isinstance(row, dict):
            return
        series = str(row.get("series") or row.get("query") or row.get("name") or row.get("title") or "").strip()
        series_key = normalize(series)
        if not series_key:
            return
        identities = identity_values_for_item(row)
        if not identities:
            return
        rows.append({
            "source": source,
            "series": series,
            "series_key": series_key,
            "identity": identities[0],
            "identities": identities,
            "issue_numbers": identity_issue_numbers_from_row(row),
            "publisher": row.get("publisher") or row.get("watch_publisher") or "",
            "year": row.get("watch_year") or row.get("year") or "",
            "watch_id": row.get("watch_id") or row.get("id") or "",
            "comicvine_id": row.get("comicvine_id") or row.get("comicvineId") or "",
            "mangadex_id": row.get("mangadex_id") or row.get("mangadexId") or "",
            "queue_identity": row.get("queue_identity") or identities[0],
        })

    watches = (read_json(COMIC_SERIES_WATCHES_FILE, {}) or {}).get("watches") or []
    for watch in watches:
        if isinstance(watch, dict) and watch.get("enabled", True):
            add_row("watch", watch)

    queue = read_json(SERIES_AUTOPILOT_QUEUE_FILE, {}) or {}
    raw_items = queue.get("items") if isinstance(queue, dict) else {}
    queue_rows = raw_items.values() if isinstance(raw_items, dict) else raw_items if isinstance(raw_items, list) else []
    for row in queue_rows:
        if not isinstance(row, dict):
            continue
        if row.get("present_in_watch") is False:
            continue
        if str(row.get("state") or "queued") == "verified":
            continue
        add_row("queue", row)

    deduped = {}
    for row in rows:
        key = (row.get("series_key"), row.get("identity"))
        existing = deduped.get(key)
        if not existing:
            deduped[key] = row
            continue
        existing_numbers = set(existing.get("issue_numbers") or [])
        existing_numbers.update(row.get("issue_numbers") or [])
        existing["issue_numbers"] = sorted(existing_numbers)
        for field in ("publisher", "year", "watch_id", "comicvine_id", "queue_identity"):
            if not existing.get(field) and row.get(field):
                existing[field] = row.get(field)
    IDENTITY_CONTEXT_CACHE = list(deduped.values())
    return IDENTITY_CONTEXT_CACHE


def duplicate_identity_rows_for_item(item):
    series_key = normalize((item or {}).get("series") or (item or {}).get("query") or "")
    if not series_key:
        return []
    rows = [row for row in identity_context_rows() if row.get("series_key") == series_key]
    identities = {row.get("identity") for row in rows if row.get("identity")}
    if len(identities) < 2:
        return []
    item_identities = set(identity_values_for_item(item))
    if not item_identities:
        candidates = rows
    else:
        candidates = [row for row in rows if not (set(row.get("identities") or []) & item_identities)]
    return [row for row in candidates if not manga_companion_row(row, item)]


def manga_companion_row(row, item):
    """A ComicVine/MangaDex companion pair (manga_companion_links) is the
    same real series tracked under two provider identities by design -- not
    a genuine duplicate sibling. Their per-record publisher/year can
    legitimately disagree (e.g. a ComicVine record enriched via a Kapowarr
    metadata adapter vs. a MangaDex chapter record), which otherwise reads
    as one record's evidence "pointing at" the other and blocks real
    candidates for the series against itself.
    """
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return False
    item_comicvine = str((item or {}).get("comicvine_id") or "").strip()
    item_mangadex = str((item or {}).get("mangadex_id") or "").strip()
    row_comicvine = str(row.get("comicvine_id") or "").strip()
    row_mangadex = str(row.get("mangadex_id") or "").strip()
    pairs = []
    if row_comicvine and item_mangadex:
        pairs.append((f"comicvine:{row_comicvine}", f"mangadex:{item_mangadex}"))
    if item_comicvine and row_mangadex:
        pairs.append((f"comicvine:{item_comicvine}", f"mangadex:{row_mangadex}"))
    for comicvine_series_id, mangadex_series_id in pairs:
        try:
            if inkdrop_state.manga_companion_pair_is_linked(
                INKDROP_STATE_DB,
                comicvine_series_id=comicvine_series_id,
                mangadex_series_id=mangadex_series_id,
            ):
                return True
        except Exception:
            continue
    return False


def identity_word_tokens(value):
    generic = {
        "comic",
        "comics",
        "digital",
        "edition",
        "ediciones",
        "editions",
        "manga",
        "publisher",
        "publishing",
        "press",
    }
    words = []
    for word in normalize(value).split():
        if word in STOP_WORDS or word in generic:
            continue
        if len(word) >= 3 and word not in words:
            words.append(word)
    return words


def normalized_phrase_in_text(text, phrase):
    text_norm = normalize(text)
    phrase_norm = normalize(phrase)
    if not text_norm or not phrase_norm:
        return False
    return bool(re.search(rf"(?:^|\s){re.escape(phrase_norm)}(?:\s|$)", text_norm))


def any_normalized_phrase_in_text(text, phrases):
    return any(normalized_phrase_in_text(text, phrase) for phrase in phrases or [])


def item_publisher_text(item):
    values = []
    for key in ("publisher", "watch_publisher"):
        value = str((item or {}).get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    metadata = issue_metadata_for_item(item)
    if isinstance(metadata, dict):
        value = str(metadata.get("publisher") or "").strip()
        if value and value not in values:
            values.append(value)
    return " ".join(values)


def item_looks_like_western_comic(item):
    publisher = item_publisher_text(item)
    if not publisher:
        return False
    if any_normalized_phrase_in_text(publisher, MANGA_PUBLISHER_PHRASES):
        return False
    return any_normalized_phrase_in_text(publisher, WESTERN_COMIC_PUBLISHER_PHRASES)


def target_publisher_language_words(item):
    generic = {
        "comic",
        "comics",
        "digital",
        "edition",
        "editions",
        "manga",
        "publisher",
        "publishing",
        "press",
    }
    words = []
    for word in normalize(item_publisher_text(item)).split():
        if word in STOP_WORDS or word in generic:
            continue
        if len(word) >= 2 and word not in words:
            words.append(word)
    return words


def ambiguous_hq_source_folder(filename):
    segments = [normalize(segment) for segment in path_segments(filename)]
    return "hq" in segments[:-1]


def explicit_english_source_marker_present(filename):
    words = context_words(filename)
    return explicit_english_translation_present(words) or bool(words & ENGLISH_RELEASE_MARKERS)


def slskd_learning_same_series_success_reason(candidate, item):
    data = slskd_learning_data()
    series_label, series_key = slskd_learning_series_key(item)
    if not series_key:
        return ""

    username = normalize((candidate or {}).get("username") or "")
    if username:
        entry = (data.get("users") or {}).get(username)
        latest_language_failure, latest_series_success, _ = slskd_learning_same_series_language_history(
            entry, series_key
        )
        if latest_series_success and latest_series_success >= latest_language_failure:
            user_label = (candidate or {}).get("username") or username
            return f"SLSKD user {user_label} has verified same-series history for {series_label}"

    for path_style in slskd_learning_path_style_variants((candidate or {}).get("filename") or ""):
        entry = (data.get("path_styles") or {}).get(path_style)
        latest_language_failure, latest_series_success, _ = slskd_learning_same_series_language_history(
            entry, series_key
        )
        if latest_series_success and latest_series_success >= latest_language_failure:
            return f"SLSKD source pattern {path_style} has verified same-series history for {series_label}"
    return ""


def english_source_confidence_reason(filename, candidate, item):
    words = context_words(filename)
    explicit = sorted(words & EXPLICIT_ENGLISH_TRANSLATION_MARKERS)
    if explicit:
        return "explicit English source marker: " + ", ".join(explicit[:3])

    release = sorted(words & ENGLISH_RELEASE_MARKERS)
    if release:
        return "English/source provenance: " + ", ".join(release[:3])

    publisher_words = target_publisher_language_words(item)
    publisher_hits = [word for word in publisher_words if word in words]
    year_hits = []
    for year in identity_years_for_item(item):
        if str(year) in words:
            year_hits.append(str(year))
    if publisher_hits and year_hits:
        return "publisher/year source context: " + ", ".join([*publisher_hits[:2], *year_hits[:1]])

    # A returned directory cohort is stronger evidence than a lone ambiguous
    # filename when the directory itself is exactly the watched series and the
    # individual file has already passed title/issue/language matching.  Do not
    # apply this to fuzzy folders, packs, or ordinary one-file candidates.
    if (candidate or {}).get("series_directory_handoff"):
        try:
            cohort_size = int((candidate or {}).get("series_directory_file_count") or 0)
        except (TypeError, ValueError):
            cohort_size = 0
        if cohort_size >= 3 and (candidate or {}).get("series_directory_exact_series") is True:
            return f"exact series directory cohort ({cohort_size} files)"

    learned = slskd_learning_same_series_success_reason(candidate, item)
    if learned:
        return learned

    return ""


def western_comic_language_confidence_blocker(filename, candidate, item):
    if not item_looks_like_western_comic(item):
        return ""
    if source_language_blocker(filename):
        return ""
    if ambiguous_hq_source_folder(filename) and not explicit_english_source_marker_present(filename):
        return (
            "western comic SLSKD candidate uses an ambiguous HQ source folder "
            "without English release/source confidence"
        )
    # A plain, standard-format filename ("Series 009 (2022).cbr") is the
    # normal shape of a legitimate English-language western comic release --
    # scene releases essentially never embed the publisher's name. Requiring
    # positive proof (an explicit marker, a publisher name literally in the
    # filename, a 3+ file exact-series directory cohort, or prior learned
    # history) before allowing a candidate through inverted the burden of
    # proof: it penalized clean, tidy filenames with no release-group tag
    # while source_language_blocker() above already catches real negative
    # evidence (explicit non-English script/markers). Confirmed live against
    # 474r4x14's library: "What's The Furthest Place from Here 009
    # (2022).cbr" was blocked by this while "Chew 012 (2010).cbr" from the
    # same user only passed because of the learned-history fallback -- not
    # because it was actually less "English" than the other file.
    return ""


def years_from_value(value):
    years = set()
    for match in re.finditer(r"\b((?:19|20)\d{2})\b", str(value or "")):
        years.add(match.group(1))
    return years


def year_ranges_from_value(value):
    ranges = []
    text = str(value or "")
    for match in re.finditer(r"\b((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)?\d{2})\b", text):
        try:
            start = int(match.group(1))
            raw_end = match.group(2)
            end = int(raw_end)
            if end < 100:
                end += (start // 100) * 100
            if 1900 <= start <= 2099 and 1900 <= end <= 2099:
                low, high = sorted((start, end))
                ranges.append((low, high))
        except (TypeError, ValueError):
            continue
    return ranges


def identity_years_for_item(item):
    years = set()
    for key in ("year", "watch_year", "issue_year"):
        years.update(years_from_value((item or {}).get(key)))
    metadata = issue_metadata_for_item(item)
    if isinstance(metadata, dict):
        for key in ("year", "date", "search_query"):
            years.update(years_from_value(metadata.get(key)))
    return years


def explicit_year_range_conflict(filename, item):
    target_years = set()
    for year in identity_years_for_item(item):
        try:
            target_years.add(int(year))
        except (TypeError, ValueError):
            continue
    if not target_years:
        return ""
    ranges = year_ranges_from_value(filename)
    if not ranges:
        return ""
    if any(low <= year <= high for low, high in ranges for year in target_years):
        return ""
    shown_ranges = ", ".join(f"{low}-{high}" for low, high in ranges[:3])
    shown_targets = ", ".join(str(year) for year in sorted(target_years)[:3])
    return f"candidate year range {shown_ranges} does not overlap target year {shown_targets}"


def collected_singleton_edition_conflict(filename, item):
    """A single-issue collected target (Absolute/Deluxe/Omnibus/etc, proven by
    collected_singleton_proof) can have more than one real printing -- e.g. a
    2012 trade collection and a 2015 Absolute Edition hardcover of the same
    underlying story. The generic franchise-word + volume-evidence match in
    item_match_details() cannot tell those apart on its own. This only fires
    when the item has specific, already-detected format markers (so it never
    applies to an ordinary series) AND the candidate supplies its own
    conflicting publication year with none of those markers echoed -- a
    candidate with no year at all, or one that does carry a matching marker,
    is left alone.

    Before any of that: if ComicVine's own catalog has no standalone plain
    edition of this work at all (comicvine_edition_target_has_standalone_
    alternative), the tracked edition was never a real choice among options --
    per an explicit operator decision (originally scoped to Absolute Batman:
    The Court of Owls, generalized here to any series in the same shape), any
    complete, correctly-identified release is accepted instead of requiring
    the exact tracked printing. Defaults to the strict check whenever this
    can't be confirmed (no comicvine_id, no API key, ComicVine unreachable) --
    unproven is not the same as proven absent.
    """
    comicvine_id = str((item or {}).get("comicvine_id") or "").strip()
    singleton_title = str((item or {}).get("singleton_series_title") or (item or {}).get("series") or "").strip()
    if (
        comicvine_id
        and singleton_title
        and comicvine_edition_target_has_standalone_alternative(comicvine_id, singleton_title) is False
    ):
        return ""
    markers = set((item or {}).get("collected_singleton_markers") or [])
    if not markers:
        return ""
    target_years = set()
    for year in identity_years_for_item(item):
        try:
            target_years.add(int(year))
        except (TypeError, ValueError):
            continue
    if not target_years:
        return ""
    candidate_years = set()
    for year in years_from_value(filename):
        try:
            candidate_years.add(int(year))
        except (TypeError, ValueError):
            continue
    if not candidate_years or candidate_years & target_years:
        return ""
    try:
        import inkdrop_source_worker_coordinator as source_coordinator

        patterns = dict(source_coordinator.COLLECTED_SINGLETON_PATTERNS)
    except (ImportError, AttributeError):
        return ""
    if any(patterns[marker].search(filename) for marker in markers if marker in patterns):
        return ""
    shown_markers = ", ".join(sorted(markers)[:3])
    shown_target = ", ".join(str(year) for year in sorted(target_years)[:3])
    shown_candidate = ", ".join(str(year) for year in sorted(candidate_years)[:3])
    return (
        f"candidate year {shown_candidate} conflicts with target year {shown_target} "
        f"and does not carry the target's {shown_markers} edition marker"
    )


def sibling_item_for_identity(item, sibling):
    sibling_item = {
        "series": (item or {}).get("series") or (item or {}).get("query"),
        "query": (item or {}).get("series") or (item or {}).get("query"),
        "issue": (item or {}).get("issue"),
        "publisher": sibling.get("publisher") or "",
        "watch_publisher": sibling.get("publisher") or "",
        "year": sibling.get("year") or "",
        "watch_year": sibling.get("year") or "",
        "watch_id": sibling.get("watch_id") or "",
        "comicvine_id": sibling.get("comicvine_id") or "",
        "queue_identity": sibling.get("queue_identity") or sibling.get("identity") or "",
    }
    return sibling_item


def duplicate_identity_mismatch_count(series):
    series_key = normalize(series)
    if not series_key:
        return 0
    actions = load_actions()
    bad = actions.get("manual_source_bad_candidates") if isinstance(actions, dict) else {}
    if not isinstance(bad, dict):
        return 0
    count = 0
    for rows in bad.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if normalize(row.get("series") or "") != series_key:
                continue
            if str(row.get("reason") or "") == "identity_mismatch":
                count += 1
    return count


def duplicate_identity_gate(filename, item, candidate=None):
    siblings = duplicate_identity_rows_for_item(item)
    if not siblings:
        return "", [], []
    words = context_words(filename)
    candidate_years = years_from_value(filename)
    metadata = issue_metadata_for_item(item)
    metadata = metadata if isinstance(metadata, dict) else {}
    target_words = []
    for value in (
        (item or {}).get("publisher"),
        (item or {}).get("watch_publisher"),
        metadata.get("publisher"),
    ):
        target_words.extend(identity_word_tokens(value))
    target_words = list(dict.fromkeys(target_words))
    target_years = identity_years_for_item(item)
    target_hits = []
    for word in target_words:
        if word in words:
            target_hits.append(word)
    for year in sorted(target_years):
        if year in candidate_years:
            target_hits.append(year)

    conflict_hits = []
    for sibling in siblings:
        sibling_words = identity_word_tokens(sibling.get("publisher") or "")
        sibling_years = identity_years_for_item(sibling_item_for_identity(item, sibling))
        sibling_hits = [word for word in sibling_words if word in words]
        sibling_hits.extend(year for year in sorted(sibling_years) if year in candidate_years)
        if sibling_hits:
            conflict_hits.append(f"{sibling.get('identity')}: {', '.join(list(dict.fromkeys(sibling_hits))[:4])}")

    if conflict_hits and not target_hits:
        return (
            "duplicate title identity evidence points at another watched volume: " + "; ".join(conflict_hits[:2]),
            [],
            [],
        )

    reasons = []
    review_reasons = []
    if target_hits:
        reasons.append("duplicate title identity evidence: " + ", ".join(list(dict.fromkeys(target_hits))[:4]))
    elif duplicate_identity_mismatch_count((item or {}).get("series") or (item or {}).get("query")):
        review_reasons.append("duplicate title has prior identity mismatch; verifier will guard final import")
    else:
        review_reasons.append("duplicate title lacks publisher/year identity evidence; verifier will guard final import")
    return "", reasons, review_reasons


def important_words(series):
    words = []
    tokens = normalize(without_edition_phrases(series)).split()
    for index, word in enumerate(tokens):
        if word in STOP_WORDS:
            continue
        preserve_stylized_x = (
            word == "x"
            and 0 < index < len(tokens) - 1
            and len(tokens[index - 1]) > 2
            and len(tokens[index + 1]) > 2
        )
        if word.isdigit() or len(word) > 2 or preserve_stylized_x:
            words.append(word)
    return words[:6]


def title_connector_words(series):
    """Return the short words a title carries that important_words drops.

    "Yona of the Dawn" reduces to yona/dawn, but a release is still named
    "Yona of the Dawn 002.cbz". The dropped "of" then sits between two words
    the ordered phrase check expects to find side by side, and an exact
    filename fails to match its own series. Handing those words back lets the
    phrase step over them -- only the ones the target itself owns, so this
    never bridges a gap in some other series' name.
    """

    kept = set(important_words(series))
    connectors = set()
    for word in normalize(without_edition_phrases(series)).split():
        if word in kept or word.isdigit():
            continue
        if len(word) <= 2:
            connectors.add(word)
    return connectors


def context_words(filename):
    return set(normalize(filename).split())


def has_comic_context(filename):
    words = context_words(filename)
    comic_words = words & COMIC_CONTEXT_WORDS
    if not comic_words:
        return False
    # Generic library folders like "books" should not rescue obvious non-comic
    # paths such as TTRPG/gamebook collections. Specific comic/manga words still do.
    if comic_words <= GENERIC_COMIC_CONTEXT_WORDS and words & NON_COMIC_CONTEXT_WORDS:
        return False
    return True


def has_non_comic_context(filename):
    return bool(context_words(filename) & NON_COMIC_CONTEXT_WORDS)


def title_word_forms(word):
    forms = {str(word or "")}
    if str(word or "").isdigit():
        word_value = number_word_for_token(word)
        if word_value:
            forms.add(word_value)
    return {form for form in forms if form}


def title_word_present(word, words):
    word_set = set(words or [])
    return bool(title_word_forms(word) & word_set)


def title_matched_words(words, available_words):
    available = set(available_words or [])
    return [word for word in words if title_word_present(word, available)]


def title_phrase_present(segment_words, words, *, allow_connectors=False, target_connectors=()):
    segment_words = [str(word or "") for word in segment_words or [] if str(word or "")]
    words = [str(word or "") for word in words or [] if str(word or "")]
    if not segment_words or not words:
        return False
    target_connectors = set(target_connectors or ())
    for start in range(len(segment_words)):
        index = start
        matched = True
        for word in words:
            connector_words = (
                STOP_WORDS
                | target_connectors
                | ({"a", "an", "in", "of"} if allow_connectors else set())
            )
            # normalize() turns "'" into a space, so a possessive title ("Hell's")
            # leaves a stray one-letter "s" token between the real words -- skip it
            # like a connector unless it's actually the word we're trying to match.
            while index < len(segment_words) and (
                segment_words[index] in connector_words
                or (
                    len(segment_words[index]) == 1
                    and not title_word_present(word, {segment_words[index]})
                )
            ):
                index += 1
            if index >= len(segment_words) or not title_word_present(word, {segment_words[index]}):
                matched = False
                break
            index += 1
        if matched:
            return True
    return False


def single_word_repeated_title_conflict(segment_words, word):
    segment_words = [str(value or "") for value in segment_words or [] if str(value or "")]
    if not segment_words or not word:
        return False
    start = 1 if segment_words[0] == "the" and len(segment_words) > 1 else 0
    if start >= len(segment_words) or not title_word_present(word, {segment_words[start]}):
        return False
    repeat_count = 0
    for token in segment_words[start:]:
        if token in {"v", "vol", "volume", "book", "issue", "part", "pt", "chapter", "ch"}:
            break
        number = token_number(token)
        if number is not None:
            break
        if title_word_present(word, {token}):
            repeat_count += 1
    return repeat_count > 1


def title_match(filename, words, *, allow_connectors=False, target_connectors=()):
    if not words:
        return {"matched": False, "matched_words": [], "penalty": "no title words available"}
    segments = [normalize(segment) for segment in path_segments(filename)]
    segment_word_lists = [segment.split() for segment in segments if segment]
    candidate_segments = [normalize(filename_stem(filename)).split(), *segment_word_lists]
    full_words = context_words(filename)
    stem_words = normalize(filename_stem(filename)).split()
    stem_word_set = set(stem_words)
    matched_words = title_matched_words(words, full_words)

    if len(words) == 1:
        word = words[0]
        if not title_word_present(word, stem_word_set):
            path_starts_like_title = False
            for segment_words in segment_word_lists:
                if not segment_words:
                    continue
                if title_word_present(word, {segment_words[0]}) or (
                    segment_words[0] == "the"
                    and len(segment_words) > 1
                    and title_word_present(word, {segment_words[1]})
                ):
                    if single_word_repeated_title_conflict(segment_words, word):
                        return {
                            "matched": False,
                            "matched_words": matched_words,
                            "penalty": "single-word title repeats like a different series title",
                        }
                    path_starts_like_title = True
                    break
            if path_starts_like_title:
                return {"matched": True, "matched_words": [word], "penalty": "", "matched_in_path": True}
            return {
                "matched": False,
                "matched_words": matched_words,
                "penalty": "single-word title missing from filename",
            }
        # One-word series names are noisy on Soulseek; require the file/title segment
        # to actually start with that word instead of burying it as a subtitle.
        starts_like_title = False
        for segment_words in [normalize(filename_stem(filename)).split(), *[segment.split() for segment in segments]]:
            if not segment_words:
                continue
            if title_word_present(word, {segment_words[0]}) or (
                segment_words[0] == "the"
                and len(segment_words) > 1
                and title_word_present(word, {segment_words[1]})
            ):
                if single_word_repeated_title_conflict(segment_words, word):
                    return {
                        "matched": False,
                        "matched_words": matched_words,
                        "penalty": "single-word title repeats like a different series title",
                    }
                starts_like_title = True
                break
        if not starts_like_title:
            return {
                "matched": False,
                "matched_words": matched_words,
                "penalty": "single-word title appears only as subtitle/path text",
            }
        return {"matched": True, "matched_words": [word], "penalty": ""}

    if len(words) == 2:
        has_exact_segment = any(
            title_phrase_present(
                segment_words,
                words,
                allow_connectors=allow_connectors,
                target_connectors=target_connectors,
            )
            for segment_words in candidate_segments
        )
        if not has_exact_segment:
            return {
                "matched": False,
                "matched_words": matched_words,
                "penalty": "series title words are not an ordered phrase",
            }
        return {"matched": True, "matched_words": [word for word in words if word in full_words], "penalty": ""}

    has_title_phrase = any(
        title_phrase_present(
            segment_words,
            words,
            allow_connectors=allow_connectors,
            target_connectors=target_connectors,
        )
        for segment_words in candidate_segments
    )
    required = min(3, max(2, len(words) - 1))
    if not has_title_phrase and len(matched_words) < required:
        return {
            "matched": False,
            "matched_words": matched_words,
            "penalty": f"matched only {len(matched_words)}/{required} required title words",
        }
    if not has_title_phrase:
        return {
            "matched": False,
            "matched_words": matched_words,
            "penalty": "series title words are scattered across filename/path text",
        }
    specific_words = words[3:]
    if specific_words and not any(title_word_present(word, full_words) for word in specific_words):
        return {
            "matched": False,
            "matched_words": matched_words,
            "penalty": "missing subtitle/title word: " + ", ".join(specific_words[:3]),
        }
    return {"matched": True, "matched_words": matched_words, "penalty": ""}


def series_identity_match(filename, item):
    variants = source_title_variants(item)
    if not variants:
        variants = [(item or {}).get("series") or (item or {}).get("query")]
    best = None
    for variant in variants:
        words = important_words(variant)
        if not words:
            continue
        # Manual Search and unattended acquisition must not disagree before
        # they reach the shared compatibility gate. Collected releases often
        # insert unit markers or grammatical connectors into an otherwise
        # exact title ("Batman v01 - The Court of Owls" and
        # "Batman - A Death in the Family"). Permit that ordered connector
        # tolerance automatically only when durable collected-singleton proof
        # is present. The shared compatibility engine still vetoes wrong
        # work/unit/edition, packs, previews, and unsafe ranges.
        allow_connectors = bool(
            (item or {}).get("manual_search_discovery")
            or (item or {}).get("collected_singleton_proof")
        )
        details = dict(
            title_match(
                filename,
                words,
                allow_connectors=allow_connectors,
                target_connectors=title_connector_words(variant),
            )
        )
        details["title_variant"] = display_clean(variant)
        details["title_words"] = words
        if details.get("matched"):
            punctuation_conflict = significant_terminal_punctuation_series_conflict(
                filename,
                item,
                title_variant=variant,
            )
            if punctuation_conflict:
                details["matched"] = False
                details["penalty"] = punctuation_conflict
                return details
            return details
        if best is None or len(details.get("matched_words") or []) > len(best.get("matched_words") or []):
            best = details
    if best:
        return best
    return {
        "matched": False,
        "matched_words": [],
        "penalty": "no series title words available",
        "title_variant": "",
        "title_words": [],
    }


def significant_terminal_punctuation(value):
    match = re.search(r"([!?]{2,})\s*$", str(value or "").strip())
    return match.group(1) if match else ""


def significant_terminal_punctuation_series_conflict(filename, item, *, title_variant=None):
    """Reject a punctuation-distinct title attached to an otherwise exact name.

    Repeated terminal ``!``/``?`` is treated as title identity when it appears
    directly after the matched series phrase. A target that owns the same
    punctuation remains valid, and punctuation omission/single decorative
    punctuation remains normalizable.
    """

    canonical = str(item_series_title(item or {}) or "").strip()
    target_signature = significant_terminal_punctuation(canonical)
    variant = str(title_variant or canonical).strip()
    variant_base = re.sub(r"[!?]+\s*$", "", variant).strip()
    words = normalize(variant_base).split()
    if not words:
        return ""
    phrase = r"(?<![a-z0-9])" + r"[\s._\-–—:]+".join(re.escape(word) for word in words)
    pattern = re.compile(
        phrase + r"(?P<punct>[!?]{2,})(?=$|[\s._\-–—:\(\[\{\\/])",
        flags=re.I,
    )
    for segment in path_segments(filename):
        text = filename_stem(segment) if segment == filename_leaf(filename) else segment
        for match in pattern.finditer(str(text or "")):
            candidate_signature = match.group("punct")
            if target_signature and candidate_signature == target_signature:
                continue
            return "candidate title has significant terminal punctuation for a different series identity"
    return ""


def issue_tokens(issue):
    text = str(issue or "").strip()
    tokens = set()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        raw = match.group(0)
        tokens.add(raw)
        try:
            number = int(float(raw))
            tokens.add(str(number))
            tokens.add(f"{number:03d}")
        except ValueError:
            pass
    return tokens


def issue_number(issue):
    tokens = issue_tokens(issue)
    numbers = {token_number(token) for token in tokens}
    numbers.discard(None)
    if not numbers:
        return None
    return sorted(numbers)[0]


def issue_match_text(filename):
    text = filename_stem(filename)
    text = re.sub(r"\(\s*\d+\s*\)\s*$", " ", text)
    text = re.sub(r"\b(?:win(?:dows)?|linux|macos|osx)[-_ ]?(?:x86|x64|32|64)?\b", " ", text, flags=re.I)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\{[^}]*\}", " ", text)
    text = re.sub(r"\([^)]*(?:19|20)\d{2}[^)]*\)", " ", text)
    text = re.sub(r"\b(?:19|20)\d{2}(?:\s*[-_]\s*\d{1,2})?\b", " ", text)
    return display_clean(text)


def labeled_issue_numbers(text):
    numbers = []
    pattern = re.compile(
        rf"(?:#|\b(?:issue|iss|no|num|number|part|pt|chapter|ch)\.?\s*)0*({NUMBER_TOKEN_PATTERN})(?:\.\d+)?\b",
        flags=re.I,
    )
    for match in pattern.finditer(str(text or "")):
        number = token_number(match.group(1))
        if number is not None:
            numbers.append(number)
    return numbers


def issue_range_match(filename, item):
    wanted = issue_number((item or {}).get("issue"))
    if wanted is None:
        return {"matched": False, "reason": "", "penalty": ""}
    ranges = []
    for match in re.finditer(r"\b0*(\d{1,4})\s*[-–—]\s*0*(\d{1,4})\b", issue_match_text(filename)):
        try:
            left = int(match.group(1))
            right = int(match.group(2))
        except (TypeError, ValueError):
            continue
        if 1900 <= left <= 2099 and 1 <= right <= 12:
            continue
        if left == right:
            continue
        low, high = sorted((left, right))
        ranges.append((low, high))
        if low <= wanted <= high:
            return {"matched": True, "reason": f"issue range {low}-{high} contains {wanted}", "penalty": ""}
    if ranges:
        shown = ", ".join(f"{low}-{high}" for low, high in ranges[:4])
        return {"matched": False, "reason": "", "penalty": f"issue range {shown} does not contain {wanted}"}
    return {"matched": False, "reason": "", "penalty": ""}


def book_volume_numbers(text):
    numbers = []
    pattern = re.compile(
        rf"\b(?:v|vol|volume|book|band|tome|tomo)\.?\s*0*({NUMBER_TOKEN_PATTERN})(?:\.\d+)?\b",
        flags=re.I,
    )
    for match in pattern.finditer(str(text or "")):
        number = token_number(match.group(1))
        if number is not None:
            numbers.append(number)
    return numbers


def bare_issue_numbers(filename, item):
    text = issue_match_text(filename)
    text = re.sub(
        rf"\b(?:v|vol|volume|book|band|tome|tomo)\.?\s*0*{NUMBER_TOKEN_PATTERN}(?:\.\d+)?\b",
        " ",
        text,
        flags=re.I,
    )
    words = normalize(text).split()
    title_numbers = {
        token_number(word)
        for word in important_words((item or {}).get("series") or (item or {}).get("query"))
        if token_number(word) is not None
    }
    numbers = []
    for index, word in enumerate(words):
        number = token_number(word)
        if number is None:
            continue
        if 1900 <= number <= 2099:
            continue
        previous = words[index - 1] if index else ""
        if previous in {"v", "vol", "volume", "book", "band", "tome", "tomo"}:
            continue
        if number in title_numbers and index <= 1:
            continue
        numbers.append(number)
    return numbers


def book_volume_number_match(filename, item):
    wanted = issue_number((item or {}).get("issue"))
    if wanted is None:
        return {"matched": False, "reason": "", "penalty": ""}
    numbers = book_volume_numbers(filename_stem(filename))
    if not numbers:
        return {"matched": False, "reason": "", "penalty": ""}
    if wanted in numbers:
        return {"matched": True, "reason": f"book/volume token {wanted}", "penalty": ""}
    return {
        "matched": False,
        "reason": "",
        "penalty": "book/volume token " + ", ".join(str(value) for value in numbers[:4]) + f" does not match {wanted}",
    }


def shared_volume_artifact_match(filename, item):
    if not inkdrop_source_providers:
        return None
    item = item if isinstance(item, dict) else {}
    candidate = {
        "series_title": item.get("series_title") or item.get("series") or item.get("query") or item.get("title"),
        "series": item.get("series") or item.get("series_title") or item.get("query") or item.get("title"),
        "issue_number": item.get("issue_number") or item.get("issue") or item.get("number"),
        "normalized_number": item.get("normalized_number") or item.get("issue") or item.get("number"),
        "volume_number": item.get("volume_number") or item.get("volume") or item.get("book_volume") or item.get("manga_volume"),
        "volume": item.get("volume") or item.get("volume_number") or item.get("book_volume") or item.get("manga_volume"),
        "issue_title": item.get("issue_title") or item.get("issueTitle") or item.get("title"),
        "metadata_provider": item.get("metadata_provider") or item.get("source") or item.get("provider"),
        "series_source": item.get("series_source") or item.get("source") or item.get("metadata_provider"),
        "series_id": item.get("series_id") or item.get("seriesIdentity") or item.get("queue_identity"),
        "unit_type": item.get("unit_type") or item.get("unitType"),
        "unit_model": item.get("unit_model") or item.get("unitModel"),
        "media_type": item.get("media_type") or item.get("mediaType"),
        "publisher": item.get("publisher") or item.get("watch_publisher") or item.get("series_publisher"),
        "watch_publisher": item.get("watch_publisher") or item.get("publisher") or item.get("series_publisher"),
    }
    try:
        return inkdrop_source_providers.indexer_manifest_entry_matches_volume_candidate(candidate, filename)
    except Exception:
        return None


def issue_number_match(filename, item):
    wanted = issue_number((item or {}).get("issue"))
    if wanted is None:
        return {"matched": False, "reason": "", "penalty": "no numeric issue token"}

    stem = filename_stem(filename)
    ranged = issue_range_match(filename, item)
    if ranged.get("matched"):
        return ranged

    explicit = labeled_issue_numbers(stem)
    if explicit:
        if wanted in explicit:
            return {"matched": True, "reason": f"issue/part token {wanted}", "penalty": ""}
        return {
            "matched": False,
            "reason": "",
            "penalty": "explicit issue token " + ", ".join(str(value) for value in explicit[:4]) + f" does not match {wanted}",
        }

    bare = bare_issue_numbers(filename, item)
    if wanted in bare:
        return {"matched": True, "reason": f"issue/part token {wanted}", "penalty": ""}
    if bare:
        return {
            "matched": False,
            "reason": "",
            "penalty": "filename issue token " + ", ".join(str(value) for value in bare[:4]) + f" does not match {wanted}",
        }
    return {"matched": False, "reason": "", "penalty": "missing issue/part token"}


def issue_title_words(item):
    words = []
    for title in issue_title_variants(item):
        for word in normalize(title).split():
            if word in STOP_WORDS:
                continue
            if word.isdigit() or len(word) <= 2:
                continue
            if word not in words:
                words.append(word)
    return words[:8]


def issue_title_match(filename, item):
    words = issue_title_words(item)
    if not words:
        return {"matched": False, "matched_words": [], "penalty": "no issue title metadata"}
    full_words = context_words(filename)
    matched_words = [word for word in words if word in full_words]
    required = len(words) if len(words) <= 2 else min(3, max(2, len(words) - 1))
    if len(matched_words) < required:
        return {
            "matched": False,
            "matched_words": matched_words,
            "penalty": f"matched only {len(matched_words)}/{required} required issue-title words",
        }
    return {"matched": True, "matched_words": matched_words, "penalty": ""}


def issue_tail_words(filename, item):
    tokens = issue_tokens(item.get("issue"))
    if not tokens:
        return []
    numeric_tokens = {token_number(token) for token in tokens}
    numeric_tokens.discard(None)
    stem_words = normalize(filename_stem(filename)).split()
    for index, word in enumerate(stem_words):
        if word in tokens or token_number(word) in numeric_tokens:
            return stem_words[index + 1 :]
    return []


def localized_title_penalty(filename, item):
    words = context_words(filename)
    markers = sorted(words & NON_ENGLISH_LANGUAGE_MARKERS)
    if markers and not explicit_english_translation_present(words):
        return "non-English language marker: " + ", ".join(markers[:3])
    title_words = important_words(item.get("series") or item.get("query"))
    if len(title_words) > 2:
        return ""
    tail = [word for word in issue_tail_words(filename, item) if word not in {"conv", "digital"}]
    if len(tail) >= 2 and tail[0] in NON_ENGLISH_ARTICLES and not explicit_english_translation_present(tail):
        return "likely translated issue title: " + " ".join(tail[:4])
    return ""


def explicit_english_translation_present(words):
    return bool(set(words or []) & EXPLICIT_ENGLISH_TRANSLATION_MARKERS)


def non_english_script_marker(text):
    labels = []
    for char in str(text or ""):
        code = ord(char)
        if (
            0x3040 <= code <= 0x30FF
            or 0x31F0 <= code <= 0x31FF
            or 0xFF66 <= code <= 0xFF9F
        ):
            labels.append("japanese script")
        elif 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            labels.append("cjk ideograph")
        elif 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
            labels.append("korean script")
        elif 0x0400 <= code <= 0x04FF:
            labels.append("cyrillic script")
    labels = list(dict.fromkeys(labels))
    return ", ".join(labels[:3])


def source_language_blocker(filename):
    if inkdrop_language is not None:
        language = inkdrop_language.classify_release_language(
            title=filename,
            path=filename,
            preferred_languages=("en",),
            unknown_policy="allow_if_exact",
        )
        if language.get("blocked"):
            return language.get("detail") or "wrong language source"
    words = context_words(filename)
    explicit_english = explicit_english_translation_present(words)
    script_marker = non_english_script_marker(filename)
    if script_marker and not explicit_english:
        return "non-English script in source path: " + script_marker
    markers = sorted((words & NON_ENGLISH_LANGUAGE_MARKERS) | (words & NON_ENGLISH_COLLECTION_MARKERS))
    if not markers:
        return ""
    if explicit_english:
        return ""
    return "non-English or ambiguous language source marker: " + ", ".join(markers[:3])


def english_release_provenance_bonus(filename):
    words = context_words(filename)
    markers = words & ENGLISH_RELEASE_MARKERS
    if not markers:
        return 0, []
    bonus = 8
    if markers & {"digital", "empire", "dcp", "getcomics", "minutemen", "zone"}:
        bonus += 4
    if markers & {"us", "usa", "english"}:
        bonus += 3
    return min(14, bonus), ["English/source provenance +" + str(min(14, bonus))]


def concrete_leaf_title_prefix_words(filename):
    marker_words = {
        "v",
        "vol",
        "vols",
        "volume",
        "volumes",
        "book",
        "books",
        "issue",
        "issues",
        "part",
        "parts",
        "pt",
        "pts",
        "chapter",
        "chapters",
        "ch",
        "no",
        "num",
        "number",
    }
    ignored = STOP_WORDS | ENGLISH_RELEASE_MARKERS | {
        "digital",
        "edition",
        "english",
        "hybrid",
        "scan",
        "scans",
        "unknown",
        "unk",
    }
    prefix = []
    for word in normalize(filename_stem(filename_leaf(filename))).split():
        attached_number = re.match(
            r"^(?:v|vols?|volumes?|books?|issues?|parts?|pts?|chapters?|ch)0*(\d{1,4})$",
            word,
        )
        if attached_number:
            break
        if word in marker_words:
            break
        if token_number(word) is not None:
            break
        if word in ignored:
            continue
        if len(word) <= 2:
            continue
        prefix.append(word)
        if len(prefix) >= 6:
            break
    return prefix


def leaf_title_conflict(filename, item, title_details):
    leaf = filename_leaf(filename)
    if not leaf or leaf == str(filename or ""):
        return ""
    leaf_title = series_identity_match(leaf, item)
    if leaf_title.get("matched"):
        return ""
    number_details = issue_number_match(leaf, item)
    volume_details = book_volume_number_match(leaf, item)
    if not (number_details.get("matched") or volume_details.get("matched")):
        return ""
    title_words = set(title_details.get("title_words") or [])
    if not title_words:
        title_words = set(important_words((item or {}).get("series") or (item or {}).get("query")))
    if not title_words:
        return ""
    prefix = concrete_leaf_title_prefix_words(leaf)
    if len(prefix) < 1:
        return ""
    title_word_list = list(title_details.get("title_words") or [])
    if title_word_list and ordered_title_start(prefix, title_word_list) == 0:
        return ""
    if set(prefix) & title_words:
        return "filename title appears to be a related different series/subseries: " + " ".join(prefix[:5])
    return "filename title appears to be a different series: " + " ".join(prefix[:5])


def ordered_title_start(segment_words, title_words):
    segment_words = [str(word or "") for word in segment_words or [] if str(word or "")]
    title_words = [str(word or "") for word in title_words or [] if str(word or "")]
    if not segment_words or not title_words:
        return None
    for start in range(len(segment_words)):
        index = start
        if segment_words[index] == "the" and (not title_words or title_words[0] != "the"):
            index += 1
        for word in title_words:
            while index < len(segment_words) and segment_words[index] in STOP_WORDS:
                index += 1
            if index >= len(segment_words) or not title_word_present(word, {segment_words[index]}):
                break
            index += 1
        else:
            return start
    return None


def title_prefix_subseries_conflict(filename, item, title_details):
    title_words = list(title_details.get("title_words") or [])
    if len(title_words) < 2:
        return ""
    leaf_words = normalize(filename_stem(filename_leaf(filename))).split()
    start = ordered_title_start(leaf_words, title_words)
    if start is None or start <= 0:
        return ""
    prefix_words = leaf_words[:start]
    ignored = STOP_WORDS | ENGLISH_RELEASE_MARKERS | COMIC_CONTEXT_WORDS | {
        "digital",
        "edition",
        "english",
        "hybrid",
        "scan",
        "scans",
        "unknown",
        "unk",
    }
    prefix = []
    for word in prefix_words:
        if word in ignored:
            continue
        if token_number(word) is not None:
            continue
        if len(word) <= 2:
            continue
        prefix.append(word)
    if not prefix:
        return ""
    bridge_words = {"after", "before", "featuring", "presents", "versus", "vs"}
    if set(prefix) & bridge_words:
        return "candidate appears to be a different titled series/subseries: " + " ".join(prefix[:5])
    return ""


def item_match_details(filename, item):
    ext = extension_for(filename)
    reasons = []
    penalties = []
    score_reasons = []
    if not ext or ext not in COMIC_EXTENSIONS:
        label = ext or "no extension"
        return {"matched": False, "score": -100, "reasons": reasons, "penalties": [f"unsupported extension {label}"]}
    if has_non_comic_context(filename) and not has_comic_context(filename):
        return {
            "matched": False,
            "score": -90,
            "reasons": reasons,
            "penalties": ["non-comic path context"],
        }
    title = series_identity_match(filename, item)
    matched_words = list(title.get("matched_words") or [])
    if not title.get("matched"):
        return {
            "matched": False,
            "score": -50,
            "reasons": reasons,
            "penalties": [title.get("penalty") or "title mismatch"],
        }
    raw_series_source = str((item or {}).get("series") or (item or {}).get("query") or "")
    edition_match = re.match(r"(?i)^\s*absolute\s+([^:]+):\s*.+$", raw_series_source)
    filename_words = set(normalize(filename).split())
    franchise_words = important_words(edition_match.group(1)) if edition_match else []
    semantic_conflict = bool(
        re.search(r"(?i)\b(?:soundtracks?|ost|music|audio|flac|mp3)\b", filename)
        or re.search(r"(?i)\bdetective\s+comics\b", filename)
        or re.search(r"(?i)\b(?:monthly|new\s+series|ongoing\s+series)\b", filename)
    )
    explicit_collection = bool(
        re.search(
            r"(?i)\b(?:omnibus|saga|collection|collected|compendium|deluxe|v\s*0*\d+|vol(?:ume)?\.?\s*0*\d+)\b",
            filename,
        )
    )
    range_pack = bool(
        re.search(r"(?<!\d)0*\d{1,4}\s*[-–—]\s*0*\d{1,4}(?!\d)", filename)
        and re.search(r"(?i)\b(?:pack|bundle)\b", filename)
    )
    if edition_match and semantic_conflict:
        return {
            "matched": False,
            "score": -70,
            "reasons": reasons,
            "penalties": ["semantic conflict with collected comic target"],
        }
    if (
        edition_match
        and "absolute" not in filename_words
        and (
            not franchise_words
            or not all(word in filename_words for word in franchise_words)
            or not (explicit_collection or range_pack)
        )
    ):
        return {
            "matched": False,
            "score": -55,
            "reasons": reasons,
            "penalties": ["prefixless Absolute-edition match lacks collection/volume evidence"],
        }
    prefix_conflict = title_prefix_subseries_conflict(filename, item, title)
    if prefix_conflict:
        return {
            "matched": False,
            "score": -55,
            "reasons": reasons,
            "penalties": [prefix_conflict],
        }
    leaf_conflict = leaf_title_conflict(filename, item, title)
    if leaf_conflict:
        return {
            "matched": False,
            "score": -55,
            "reasons": reasons,
            "penalties": [leaf_conflict],
        }
    edition_conflict = collected_singleton_edition_conflict(filename, item)
    if edition_conflict:
        return {
            "matched": False,
            "score": -55,
            "reasons": reasons,
            "penalties": [edition_conflict],
        }
    if matched_words:
        reasons.append("title words: " + ", ".join(matched_words[:5]))
    issue_matched = False
    issue_title_bonus_words = []
    if issue_tokens(item.get("issue")):
        number_details = issue_number_match(filename, item)
        shared_volume_details = shared_volume_artifact_match(filename, item)
        book_volume_details = book_volume_number_match(filename, item)
        if number_details.get("matched"):
            issue_matched = True
            reasons.append(number_details.get("reason") or "issue/part token")
            title_details = issue_title_match(filename, item)
            if title_details.get("matched"):
                issue_title_bonus_words = list(title_details.get("matched_words") or [])
                reasons.append("issue title: " + ", ".join(issue_title_bonus_words[:5]))
        elif shared_volume_details:
            issue_matched = True
            reasons.append(
                "volume artifact token "
                + str(shared_volume_details.get("volume_number") or shared_volume_details.get("issue_number") or "")
            )
        elif book_volume_details.get("matched"):
            issue_matched = True
            reasons.append(book_volume_details.get("reason") or "book/volume token")
        elif book_volume_details.get("penalty"):
            return {
                "matched": False,
                "score": -40,
                "reasons": reasons,
                "penalties": [book_volume_details.get("penalty")],
            }
        if not issue_matched:
            title_details = issue_title_match(filename, item)
            if title_details.get("matched"):
                issue_matched = True
                if book_volume_details.get("matched"):
                    reasons.append(book_volume_details.get("reason") or "book/volume token")
                reasons.append("issue title: " + ", ".join(list(title_details.get("matched_words") or [])[:5]))
            elif item.get("manual_search_discovery") and item.get("pack_allowed") and manual_discovery_pack_evidence(filename, item)[0]:
                issue_matched = True
                reasons.append("pack/collection discovery evidence")
            else:
                return {
                    "matched": False,
                    "score": -40,
                    "reasons": reasons,
                    "penalties": [number_details.get("penalty") or title_details.get("penalty") or "missing issue/part token"],
                }
    elif issue_title_match(filename, item).get("matched"):
        reasons.append("issue title: " + ", ".join(list(issue_title_match(filename, item).get("matched_words") or [])[:5]))
    language_penalty = localized_title_penalty(filename, item)
    if language_penalty:
        return {"matched": False, "score": -45, "reasons": reasons, "penalties": [language_penalty]}
    score = 10 * len(matched_words)
    if matched_words:
        score_reasons.append(f"title word match +{10 * len(matched_words)}")
    if issue_matched:
        score += 35
        score_reasons.append("issue/part evidence +35")
    if issue_title_bonus_words:
        bonus = min(8, max(6, 2 * len(issue_title_bonus_words)))
        score += bonus
        score_reasons.append(f"issue title evidence +{bonus}")
    if ext:
        reasons.append(f"comic extension {ext}")
        score += 6
        score_reasons.append("comic extension +6")
    return {"matched": True, "score": score, "reasons": reasons, "penalties": penalties, "score_reasons": score_reasons}


TRUSTED_SINGLETON_POSITIVE_EVIDENCE = {
    "singleton_exact_title",
    "singleton_exact_bare_volume_number",
    "collected_singleton_exact_title",
    "collected_singleton_alias_exact_title",
    "collected_singleton_alias_volume",
}


def authoritative_candidate_identity_text(candidate, filename):
    """Return artifact identity, never an unrelated remote ancestor folder."""

    candidate = candidate if isinstance(candidate, dict) else {}
    manifest_match = (
        candidate.get("pack_contents_match")
        if isinstance(candidate.get("pack_contents_match"), dict)
        else {}
    )
    if manifest_match.get("coverage_source") in {
        "pack_contents_filename",
        "pack_contents_volume_filename",
    }:
        manifest_entry = (
            manifest_match.get("entry")
            or candidate.get("pack_contents_matching_entry")
        )
        if manifest_entry:
            return filename_leaf(manifest_entry)
    return filename_leaf(filename)


def candidate_identity_compatibility(candidate, filename, item):
    identity_text = authoritative_candidate_identity_text(candidate, filename)
    candidate = candidate if isinstance(candidate, dict) else {}
    identity_candidate = {
        "title": identity_text,
        "filename": identity_text,
        "remote_filename": identity_text,
        "path": identity_text,
        "provider_id": candidate.get("provider_id") or "slskd",
    }
    manifest_match = candidate.get("pack_contents_match")
    if isinstance(manifest_match, dict):
        identity_candidate["pack_contents_match"] = dict(manifest_match)
    compatibility = inkdrop_candidate_matching.candidate_compatibility(identity_candidate, item)

    # Alternate result fields may veto a leaf, but must never donate positive
    # title or unit evidence to it. Keep these provider/durable safety signals
    # outside the identity projection and merge only their rejection codes.
    veto_codes = []
    match_confidence = str(candidate.get("match_confidence") or "").strip().lower().replace("-", "_")
    if match_confidence == "mismatch":
        veto_codes.append("candidate_title_mismatch")
    elif match_confidence.startswith("related_series") or match_confidence in {"subseries", "related_title"}:
        veto_codes.append("related_series_identity")
    if candidate.get("known_bad_candidate") or str(candidate.get("source_memory_status") or "").strip().lower() == "known_bad":
        veto_codes.append("known_bad_candidate")
    if candidate.get("preview_or_sample"):
        veto_codes.append("preview_or_sample")
    for value in (
        candidate.get("original_result_title"),
        candidate.get("title"),
        candidate.get("filename"),
        candidate.get("remote_filename"),
        candidate.get("path"),
    ):
        if inkdrop_candidate_matching.parse_release_title(value).get("preview_or_sample"):
            veto_codes.append("preview_or_sample")
            break
    alternate_compatibility = inkdrop_candidate_matching.candidate_compatibility(candidate, item)
    if "creator_identity_conflict" in (alternate_compatibility.get("rejection_codes") or []):
        veto_codes.append("creator_identity_conflict")

    veto_codes = list(dict.fromkeys(veto_codes))
    rejection_codes = list(dict.fromkeys([
        *list(compatibility.get("rejection_codes") or []),
        *veto_codes,
    ]))
    if rejection_codes != list(compatibility.get("rejection_codes") or []):
        compatibility["rejection_codes"] = rejection_codes
        compatibility["review_codes"] = [
            code for code in (compatibility.get("review_codes") or []) if code not in rejection_codes
        ]
        compatibility["status"] = "blocked"
        compatibility["rejection_explanations"] = [
            *list(compatibility.get("rejection_explanations") or []),
            *[
                {"code": code, "explanation": "Alternate provider or durable evidence vetoes this artifact identity."}
                for code in veto_codes
                if code not in {
                    row.get("code")
                    for row in (compatibility.get("rejection_explanations") or [])
                    if isinstance(row, dict)
                }
            ],
        ]
        compatibility["explanation"] = compatibility["rejection_explanations"][0]["explanation"]
    return compatibility, identity_text


def shared_candidate_match_details(filename, item, candidate=None):
    """Normalize a candidate through the same authoritative identity contract."""

    details = item_match_details(filename, item)
    if not inkdrop_candidate_matching:
        return details
    if not (
        (item or {}).get("singleton_issue_proof")
        or (item or {}).get("collected_singleton_proof")
    ):
        return details
    if extension_for(filename) not in COMIC_EXTENSIONS:
        return details
    if has_non_comic_context(filename) and not has_comic_context(filename):
        return details
    compatibility, identity_text = candidate_identity_compatibility(
        candidate,
        filename,
        item,
    )
    positive = list(compatibility.get("positive_evidence") or [])
    trusted = bool(TRUSTED_SINGLETON_POSITIVE_EVIDENCE & set(positive))
    if not trusted or compatibility.get("rejection_codes"):
        identity_reasons = [
            *list(compatibility.get("rejection_codes") or []),
            *list(compatibility.get("review_codes") or []),
        ]
        return {
            "matched": False,
            "score": min(-40, int(details.get("score") or 0)),
            "reasons": [],
            "penalties": [
                identity_reasons[0]
                if identity_reasons
                else "candidate file identity does not prove the collected singleton"
            ],
            "target_compatibility": compatibility,
            "candidate_identity_text": identity_text,
        }
    if details.get("matched"):
        details["target_compatibility"] = compatibility
        details["candidate_identity_text"] = identity_text
        return details
    words = important_words((item or {}).get("series") or (item or {}).get("query") or "")
    matched_words = title_matched_words(words, context_words(identity_text))
    score = 35 + 6 + 10 * max(3, min(len(matched_words), 4))
    return {
        "matched": True,
        "score": score,
        "reasons": [
            "trusted singleton title and unit identity matched",
            "issue/part evidence from shared compatibility",
            f"comic extension {extension_for(filename)}",
        ],
        "penalties": [],
        "score_reasons": [
            f"trusted singleton identity +{score - 6}",
            "comic extension +6",
        ],
        "target_compatibility": compatibility,
        "candidate_identity_text": identity_text,
    }


def file_matches_item(filename, item):
    return bool(shared_candidate_match_details(filename, item).get("matched"))


def score_candidate(candidate, item):
    score, _ = score_candidate_details(candidate, item)
    return score


def score_candidate_details(candidate, item):
    filename = candidate.get("filename") or ""
    details = shared_candidate_match_details(filename, item, candidate=candidate)
    score = int(details.get("score") or 0)
    notes = list(details.get("score_reasons") or [])
    language_blocker = source_language_blocker(filename)
    if language_blocker:
        score -= 40
        notes.append(language_blocker + " -40")
    western_language_blocker = western_comic_language_confidence_blocker(filename, candidate, item)
    if western_language_blocker:
        score -= 35
        notes.append(western_language_blocker + " -35")
    learned_language_blockers = slskd_learning_language_blockers(candidate, item)
    if learned_language_blockers:
        score -= 100 * len(learned_language_blockers)
        for learned_language_blocker in learned_language_blockers:
            notes.append(learned_language_blocker + " -100")
    provenance_score, provenance_notes = english_release_provenance_bonus(filename)
    if provenance_score:
        score += provenance_score
        notes.extend(provenance_notes)
    elif not western_language_blocker:
        provenance_reason = english_source_confidence_reason(filename, candidate, item)
        if provenance_reason:
            notes.append(provenance_reason)
    if candidate.get("has_free_upload_slot"):
        score += 12
        notes.append("free upload slot +12")
    try:
        upload_bonus = min(15, int(candidate.get("upload_speed") or 0) // 200_000)
        if upload_bonus:
            score += upload_bonus
            notes.append(f"upload speed +{upload_bonus}")
    except (TypeError, ValueError):
        pass
    try:
        queue_penalty = min(12, int(candidate.get("queue_length") or 0))
        if queue_penalty:
            score -= queue_penalty
            notes.append(f"queue length -{queue_penalty}")
    except (TypeError, ValueError):
        pass
    if candidate.get("locked"):
        score -= 20
        notes.append("locked file -20")
    learning_score, learning_notes = slskd_learning_adjustment(candidate)
    if learning_score:
        score += learning_score
        notes.extend(learning_notes)
    return score, notes


def filename_without_exact_issue_titles(filename, item=None):
    text = unicodedata.normalize("NFKC", str(filename or ""))
    scan_text = re.sub(r"\.(?:cbz|cbr|zip|rar|7z|pdf|epub)$", "", text, flags=re.I)
    series_tokens = normalize(item_series_title(item or {})).split()
    wanted_token = item_issue_token(item or {})
    if not series_tokens or not wanted_token:
        return text
    try:
        Decimal(wanted_token)
    except (InvalidOperation, TypeError, ValueError):
        return text
    series_pattern = r"(?<!\w)" + r"(?:[\W_]+)".join(re.escape(token) for token in series_tokens) + r"(?!\w)"
    series_match = re.search(series_pattern, scan_text, flags=re.I)
    if not series_match:
        return text
    policy_chars = list(text)
    series_key = normalize(item_series_title(item or {}))
    if "." in wanted_token:
        wanted_whole, wanted_fraction = wanted_token.split(".", 1)
        wanted_number_pattern = rf"0*{int(wanted_whole)}\.{re.escape(wanted_fraction)}"
    else:
        wanted_number_pattern = rf"0*{int(wanted_token)}"
    wanted_pattern = re.compile(
        rf"(?<!\w)(?:v(?:ol(?:ume)?)?\.?\s*)?{wanted_number_pattern}",
        flags=re.I,
    )
    for issue_title in issue_title_variants(item or {}):
        title_tokens = normalize(issue_title).split()
        title_key = " ".join(title_tokens)
        if not title_tokens or all(token.isdigit() for token in title_tokens) or title_key == series_key:
            continue
        title_pattern = r"(?<!\w)" + r"(?:[\W_]+)".join(re.escape(token) for token in title_tokens) + r"(?!\w)"
        for title_match in re.finditer(title_pattern, scan_text, flags=re.I):
            if title_match.start() < series_match.end():
                continue
            protected_by_wanted_unit = False
            for unit_match in wanted_pattern.finditer(scan_text, series_match.end(), title_match.start()):
                separator = scan_text[unit_match.end():title_match.start()]
                if separator and all(
                    character.isspace() or unicodedata.category(character)[:1] in {"P", "S"}
                    for character in separator
                ):
                    protected_by_wanted_unit = True
            if not protected_by_wanted_unit:
                continue
            policy_chars[title_match.start():title_match.end()] = " " * (title_match.end() - title_match.start())
    return "".join(policy_chars)


def filename_without_exact_series_span(filename, item=None):
    text = unicodedata.normalize("NFKC", str(filename or ""))
    series_tokens = normalize(item_series_title(item or {})).split()
    if not series_tokens:
        return text
    series_pattern = r"(?<!\w)" + r"(?:[\W_]+)".join(re.escape(token) for token in series_tokens) + r"(?!\w)"
    series_matches = list(re.finditer(series_pattern, text, flags=re.I))
    if not series_matches:
        return text
    protected = list(text)
    for series_match in series_matches:
        protected[series_match.start():series_match.end()] = " " * (series_match.end() - series_match.start())
    return "".join(protected)


def filename_without_publication_date_tokens(filename):
    text = str(filename or "")
    if inkdrop_candidate_matching and hasattr(inkdrop_candidate_matching, "publication_date_evidence"):
        return inkdrop_candidate_matching.publication_date_evidence(text).get("masked_text", text)
    return text


def filename_has_pack_or_range(filename, item=None, validated_series_directory=False):
    raw_text = str(filename or "")
    if validated_series_directory:
        basename = raw_text.replace("\\", "/").rsplit("/", 1)[-1]
        raw_text = f"{item_series_title(item or {})} {basename}".strip()
    text = unicodedata.normalize("NFKC", raw_text)
    policy_text = filename_without_exact_issue_titles(text, item=item)
    policy_text = filename_without_publication_date_tokens(policy_text)
    global_policy_text = filename_without_exact_series_span(policy_text, item=item)
    norm = normalize(global_policy_text)
    pack_markers = {
        "bundle",
        "bundled",
        "collection",
        "collected",
        "complete",
        "compendium",
        "omnibus",
        "pack",
        "tpb",
    }
    words = set(norm.split())
    if words & pack_markers:
        return True, "pack/collection marker"
    unit_value = r"0*\d{1,4}(?:\.\d+)?"
    unit_number = rf"({unit_value})"
    range_separator = r"[-_‐‑‒–—]"
    for match in re.finditer(rf"\b{unit_number}\s*{range_separator}\s*{unit_number}\b", global_policy_text):
        try:
            left = Decimal(match.group(1))
            right = Decimal(match.group(2))
        except (InvalidOperation, TypeError, ValueError):
            continue
        # Common comic filenames include publication month fragments such as
        # "(2025-07)"; those are not multi-issue ranges. Treat the same
        # bounded year/month shape consistently across punctuation variants.
        if left == left.to_integral_value() and right == right.to_integral_value() and 1900 <= left <= 2099 and 1 <= right <= 12:
            continue
        if left != right:
            return True, "numeric range marker"
    explicit_pattern = re.compile(
        rf"\b(?:ch|chapters?|v|vol|volumes?|issues?|books?)\.?\s*({unit_value})"
        rf"\s*(?:to|through|thru|and|&)\s*({unit_value})\b",
        flags=re.I,
    )
    for match in explicit_pattern.finditer(global_policy_text):
        try:
            if Decimal(match.group(1)) != Decimal(match.group(2)):
                return True, "explicit range marker"
        except (InvalidOperation, TypeError, ValueError):
            continue
    # SLSKD users commonly publish individual-looking archives with a bare
    # multi-unit list in the basename. Require two numeric unit tokens around
    # an unambiguous list separator so title numbers, years, and decimal units
    # remain valid individual candidates.
    list_separator = r"(?:,|;|&|\+|⁄|∕|\band\b|\bto\b|\bthrough\b|\bthru\b)"
    list_pattern = re.compile(
        rf"\b({unit_value})\s*{list_separator}\s*(?:#|no\.?\s*)?({unit_value})\b",
        flags=re.I,
    )
    for match in list_pattern.finditer(global_policy_text):
        try:
            if Decimal(match.group(1)) != Decimal(match.group(2)):
                return True, "multiple unit marker"
        except (InvalidOperation, TypeError, ValueError):
            continue

    # After the exact wanted series title, any two numeric unit tokens separated
    # only by Unicode punctuation, symbols, or whitespace are ambiguous
    # multi-unit coverage. NFKC above folds compatibility variants (for example
    # fullwidth and small solidus forms), while the category rule also covers
    # non-compatibility separator glyphs without maintaining a denylist.
    # Item-aware title consumption prevents numeric series identities (New 52)
    # from becoming false pack markers.
    series_tokens = normalize(item_series_title(item or {})).split()
    if series_tokens:
        item_text = re.sub(r"\.(?:cbz|cbr|zip|rar|7z|pdf|epub)$", "", policy_text, flags=re.I)
        series_pattern = r"(?<!\w)" + r"(?:[\W_]+)".join(re.escape(token) for token in series_tokens) + r"(?!\w)"
        unit_tokens = re.compile(
            r"(?<!\w)(?:v(?:ol(?:ume)?)?\.?\s*)?(0*\d{1,4}(?:\.\d+)?)"
            r"(?=$|[^\w.]|\.(?=\D))",
            flags=re.I,
        )

        def masked_unit_tail(value):
            masked = list(value)

            def mask(start, end):
                masked[start:end] = " " * (end - start)

            # Exact issue-title phrases can legitimately contain numbers
            # ("2 Guns", "Chapter 2"). Remove those known title spans before
            # considering any remaining numbers as covered units.
            for issue_title in issue_title_variants(item or {}):
                title_tokens = normalize(issue_title).split()
                if not title_tokens:
                    continue
                title_pattern = r"(?<!\w)" + r"(?:[\W_]+)".join(re.escape(token) for token in title_tokens) + r"(?!\w)"
                for title_match in re.finditer(title_pattern, value, flags=re.I):
                    mask(title_match.start(), title_match.end())

            # Printing metadata has one narrow, structural form. Unknown
            # alphabetic parentheticals remain visible: phrases such as
            # "(and 002)" or "(002 included)" may describe another unit.
            printing_pattern = r"\(\s*\d+(?:st|nd|rd|th)\s+(?:printing|print)\s*\)"
            for printing in re.finditer(printing_pattern, value, flags=re.I):
                mask(printing.start(), printing.end())

            # Remove publication date metadata before comparing the remaining
            # unit stream. Masking preserves punctuation/spacing boundaries,
            # allowing "001 (2012) 002" to be recognized as 001 + 002.
            date_patterns = (
                r"(?<!\d)(?:19|20)\d{2}\s*(?:[-_.]|\s+)\s*(?:0?[1-9]|1[0-2])(?!\d)",
                r"(?<![\w.])(?:19|20)\d{2}(?![\w.])",
            )
            for date_pattern in date_patterns:
                current = "".join(masked)
                for metadata in re.finditer(date_pattern, current):
                    mask(metadata.start(), metadata.end())
            return "".join(masked)

        structural_series_matches = list(re.finditer(series_pattern, item_text, flags=re.I))
        try:
            wanted_unit_decimal = Decimal(item_issue_token(item or {}))
        except (InvalidOperation, TypeError, ValueError):
            wanted_unit_decimal = None
        for anchor_index, series_match in enumerate(structural_series_matches):
            anchored_chars = list(item_text)
            for other_index, other_match in enumerate(structural_series_matches):
                if other_index == anchor_index:
                    continue
                anchored_chars[other_match.start():other_match.end()] = " " * (other_match.end() - other_match.start())
            anchored_text = "".join(anchored_chars)
            tail = masked_unit_tail(anchored_text[series_match.end():])
            for dotted_match in re.finditer(r"(?<!\w)0*\d{1,4}\.0\d{1,3}(?=$|[^\d])", tail):
                try:
                    dotted_unit = Decimal(dotted_match.group(0))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if wanted_unit_decimal is not None and dotted_unit == wanted_unit_decimal:
                    continue
                return True, "zero-padded dotted unit marker"
            matches = list(unit_tokens.finditer(tail))
            for left_match, right_match in zip(matches, matches[1:]):
                separator = tail[left_match.end():right_match.start()]
                comparable_separator = re.sub(
                    r"\b(?:and|plus|with|including|included|includes?)\b",
                    " ",
                    separator,
                    flags=re.I,
                )
                # Words before a number inside an open parenthetical are
                # descriptive context for that visible token, not a boundary
                # that can hide it. Outside parentheses, alphabetic title text
                # continues to prevent unrelated title numbers from pairing.
                if separator.rfind("(") > separator.rfind(")"):
                    comparable_separator = "".join(
                        " " if character.isalpha() else character
                        for character in comparable_separator
                    )
                if not comparable_separator or not all(
                    character.isspace() or unicodedata.category(character)[:1] in {"P", "S"}
                    for character in comparable_separator
                ):
                    continue
                try:
                    left = Decimal(left_match.group(1))
                    right = Decimal(right_match.group(1))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if left == right:
                    continue
                return True, "punctuation-separated unit marker"
    return False, ""


def malformed_unit_syntax_reason(filename, item=None, validated_series_directory=False):
    raw_text = str(filename or "")
    series_tokens = normalize(item_series_title(item or {})).split()
    if not series_tokens:
        return ""
    series_pattern = r"(?<!\w)" + r"(?:[\W_]+)".join(re.escape(token) for token in series_tokens) + r"(?!\w)"
    if validated_series_directory:
        basename = raw_text.replace("\\", "/").rsplit("/", 1)[-1]
        if not re.search(series_pattern, unicodedata.normalize("NFKC", basename), flags=re.I):
            raw_text = f"{item_series_title(item or {})} {basename}".strip()
        else:
            raw_text = basename
    text = filename_without_exact_issue_titles(raw_text, item=item)
    text = filename_without_publication_date_tokens(text)
    text = re.sub(r"\.(?:cbz|cbr|zip|rar|7z|pdf|epub)$", "", text, flags=re.I)
    series_matches = list(re.finditer(series_pattern, text, flags=re.I))
    for anchor_index, anchor in enumerate(series_matches):
        anchored = list(text)
        for other_index, other in enumerate(series_matches):
            if other_index != anchor_index:
                anchored[other.start():other.end()] = "X" * (other.end() - other.start())
        tail = "".join(anchored[anchor.end():])
        tail = re.sub(
            r"(?<!\d)(?:19|20)\d{2}\s*(?:[-_.]|\s+)\s*(?:0?[1-9]|1[0-2])(?!\d)",
            " ",
            tail,
        )
        tail = re.sub(r"(?<![\w.])(?:19|20)\d{2}(?![\w.])", " ", tail)
        for index, character in enumerate(tail[:-1]):
            if not tail[index + 1].isdigit():
                continue
            normalized_sign = unicodedata.normalize("NFKC", character)
            unicode_name = unicodedata.name(character, "")
            sign_like = bool(
                normalized_sign in {"+", "-", "±", "−"}
                or unicodedata.category(character) == "Pd"
                or "MINUS" in unicode_name
                or "PLUS" in unicode_name
            )
            if not sign_like or index == 0:
                continue
            previous = tail[index - 1]
            if previous.isspace() or unicodedata.category(previous)[:1] in {"P", "S"}:
                return "malformed unit syntax: attached numeric sign"
        for index, character in enumerate(tail[:-1]):
            unicode_name = unicodedata.name(character, "")
            decimal_like = bool(
                character == "."
                or "DECIMAL SEPARATOR" in unicode_name
                or "DECIMAL POINT" in unicode_name
            )
            if not decimal_like or not tail[index + 1].isdigit() or index == 0:
                continue
            previous = tail[index - 1]
            if previous.isspace() or unicodedata.category(previous)[:1] in {"P", "S"}:
                return "malformed unit syntax: leading decimal point"
        if re.search(r"(?<![\w.])\d+(?:\.\d+){2,}(?![\w.])", tail):
            return "malformed unit syntax: chained decimal components"
        if re.search(r"(?<!\w)\d+(?:\.\d+)?[eE][+-]?\d+(?!\w)", tail):
            return "malformed unit syntax: numeric exponent"
        if re.search(r"(?<![\w.])\d{5,}(?![\w.])", tail):
            return "malformed unit syntax: oversized unit token"
    return ""


def manual_discovery_pack_evidence(filename, item):
    is_pack, reason = filename_has_pack_or_range(filename, item=item)
    if is_pack:
        return True, reason
    if (item or {}).get("manual_search_discovery") and (item or {}).get("pack_allowed") and "saga" in set(normalize(filename).split()):
        return True, "saga collection marker"
    return False, ""


def leaf_has_exact_item_match(filename, item):
    leaf = filename_leaf(filename)
    if not leaf:
        return False
    details = item_match_details(leaf, item)
    if not details.get("matched"):
        return False
    reasons = " | ".join(str(value).lower() for value in (details.get("reasons") or []))
    return bool(
        "issue/part token" in reasons
        or "issue range" in reasons
        or "book/volume token" in reasons
        or "issue title:" in reasons
    )


def supplemental_autopick_reason(filename, item=None):
    leaf = filename_leaf(filename)
    leaf_norm = normalize(leaf)
    leaf_padded = f" {leaf_norm} "
    for phrase in sorted(SUPPLEMENTAL_AUTOPICK_PHRASES):
        if f" {phrase} " in leaf_padded:
            return f"supplemental/junk marker: {phrase}"
    if re.search(r"\bcover\s+gallery\b", leaf_norm):
        return "supplemental/junk marker: cover gallery"
    if re.search(r"\bcovers?\s+only\b", leaf_norm):
        return "supplemental/junk marker: covers only"

    norm = normalize(filename)
    padded = f" {norm} "
    for phrase in sorted(SUPPLEMENTAL_AUTOPICK_PHRASES):
        if f" {phrase} " in padded:
            if item is not None and leaf_has_exact_item_match(filename, item):
                return ""
            return f"supplemental/junk marker: {phrase}"
    if re.search(r"\bcover\s+gallery\b", norm):
        if item is not None and leaf_has_exact_item_match(filename, item):
            return ""
        return "supplemental/junk marker: cover gallery"
    if re.search(r"\bcovers?\s+only\b", norm):
        if item is not None and leaf_has_exact_item_match(filename, item):
            return ""
        return "supplemental/junk marker: covers only"
    return ""


def strict_series_match(filename, item):
    details = series_identity_match(filename, item)
    words = list(details.get("title_words") or [])
    if not words:
        return False, "no series title words available"
    if details.get("matched"):
        variant = details.get("title_variant") or ((item or {}).get("series") or (item or {}).get("query") or "")
        if len(words) == 1:
            return True, f"single-word series identity matched: {variant}"
        return True, f"series identity phrase matched: {variant}"
    matched = list(details.get("matched_words") or [])
    if matched:
        return False, details.get("penalty") or f"matched only {len(matched)}/{len(words)} series title words"
    return False, details.get("penalty") or "series identity phrase was not present"


def leading_title_tail_words(segment_words, title_words):
    segment_words = [str(word or "") for word in segment_words or [] if str(word or "")]
    title_words = [str(word or "") for word in title_words or [] if str(word or "")]
    if not segment_words or not title_words:
        return None
    for start in range(len(segment_words)):
        index = start
        if segment_words[index] == "the" and (not title_words or title_words[0] != "the"):
            index += 1
        for word in title_words:
            while index < len(segment_words) and segment_words[index] in STOP_WORDS:
                index += 1
            if index >= len(segment_words) or not title_word_present(word, {segment_words[index]}):
                break
            index += 1
        else:
            return segment_words[index:]
    return None


def unexpected_series_subtitle_blocker(filename, item):
    details = series_identity_match(filename, item)
    if not details.get("matched"):
        return ""
    title_words = list(details.get("title_words") or [])
    if not title_words:
        return ""
    series_title = str((item or {}).get("series") or (item or {}).get("query") or "")
    expected_words = set(context_words(series_title))
    # A registered alias -- including a manga's own Japanese/romaji original
    # title -- names the same series, not a different one. Fold those words
    # in so a genuine alternate title never reads as suspicious subseries
    # evidence.
    alias_titles = list(aliases_for_series(series_title))
    if str((item or {}).get("media_type") or "").strip().lower() == "manga":
        alias_titles.extend(mangadex_alt_titles_for_series(series_title))
    for alias_title in alias_titles:
        expected_words |= set(context_words(alias_title))
    issue_title = set(issue_title_words(item))
    wanted = issue_number((item or {}).get("issue"))
    target_year_numbers = set()
    for year in identity_years_for_item(item):
        try:
            target_year_numbers.add(int(year))
        except (TypeError, ValueError):
            continue
    pack_unit_words = {
        "v",
        "vol",
        "vols",
        "volume",
        "volumes",
        "book",
        "books",
        "issue",
        "issues",
        "part",
        "parts",
        "pt",
        "pts",
        "chapter",
        "chapters",
        "ch",
    }
    ignored = STOP_WORDS | ENGLISH_RELEASE_MARKERS | {
        "digital",
        "edition",
        "english",
        "hybrid",
        "scan",
        "scans",
        "unknown",
        "unk",
    }
    leaf_exact_match = leaf_has_exact_item_match(filename, item)
    for raw_segment in path_segments(filename):
        segment = filename_stem(raw_segment) if raw_segment == filename_leaf(filename) else raw_segment
        if raw_segment != filename_leaf(filename) and leaf_exact_match and filename_has_pack_or_range(raw_segment)[0]:
            continue
        words = normalize(segment).split()
        tail = leading_title_tail_words(words, title_words)
        if not tail:
            continue
        if wanted is not None and len(tail) >= 2:
            left = token_number(tail[0])
            right = token_number(tail[1])
            if left is not None and right is not None and left != right:
                low, high = sorted((left, right))
                if low <= wanted <= high:
                    continue
        segment_years = set()
        for year in years_from_value(segment):
            try:
                segment_years.add(int(year))
            except (TypeError, ValueError):
                continue
        suspicious = []
        stopped_at_wanted_issue = False
        for word in tail:
            if word in pack_unit_words:
                break
            attached_number = re.match(r"^(?:v|vols?|volumes?|books?|issues?|parts?|pts?|chapters?|ch)0*(\d{1,4})$", word)
            if attached_number:
                try:
                    attached_value = int(attached_number.group(1))
                except ValueError:
                    attached_value = None
                if (
                    attached_value == wanted
                    or attached_value in target_year_numbers
                    or (attached_value is not None and 1900 <= attached_value <= 2099)
                ):
                    break
                if (
                    re.match(r"^(?:v|vols?|volumes?|books?)0*\d{1,4}$", word)
                    and segment_years & target_year_numbers
                ):
                    # A bare volume/book marker whose own segment carries the
                    # wanted item's real publication year names which
                    # numbered run this is, not a different subseries -- a
                    # genuinely different real-world volume carries a
                    # different year here instead.
                    continue
                suspicious.append(word)
                continue
            number = token_number(word)
            if number is not None:
                if number == wanted or 1900 <= number <= 2099:
                    if number == wanted:
                        stopped_at_wanted_issue = True
                    break
                suspicious.append(word)
                continue
            if word in ignored or word in expected_words or word in issue_title:
                continue
            suspicious.append(word)
        if (
            len(suspicious) >= 2
            or any(token_number(word) is not None for word in suspicious)
            or (suspicious and stopped_at_wanted_issue)
        ):
            return "candidate appears to be a different titled series/subseries: " + " ".join(suspicious[:5])
    leaf_words = normalize(filename_stem(filename)).split()
    if leaf_words and not title_phrase_present(leaf_words, title_words):
        prefix = []
        for word in leaf_words:
            number = token_number(word)
            attached_number = re.match(r"^(?:v|vols?|volumes?|books?|issues?|parts?|pts?|chapters?|ch)0*(\d{1,4})$", word)
            if attached_number:
                try:
                    number = int(attached_number.group(1))
                except ValueError:
                    number = None
            if word in pack_unit_words:
                break
            if number is not None and (number == wanted or 1900 <= number <= 2099):
                break
            if word in ignored:
                continue
            prefix.append(word)
        prefix = [word for word in prefix if word not in issue_title and word not in expected_words]
        overlap = set(leaf_words) & set(title_words)
        if overlap and prefix:
            return "candidate appears to be a related different series/subseries: " + " ".join(prefix[:5])
    return ""


def availability_gate(candidate):
    if candidate.get("has_free_upload_slot"):
        return True, "free upload slot"
    try:
        queue_length = int(candidate.get("queue_length") or 0)
    except (TypeError, ValueError):
        queue_length = 999999
    try:
        upload_speed = int(candidate.get("upload_speed") or 0)
    except (TypeError, ValueError):
        upload_speed = 0
    if queue_length == 0 and upload_speed > 0:
        return True, "available with active upload speed"
    return False, "no free slot or known available transfer"


def auto_grab_size_ceiling(filename):
    is_pack, _ = filename_has_pack_or_range(filename)
    return AUTO_GRAB_PACK_MAX_BYTES if is_pack else AUTO_GRAB_MAX_BYTES


def direct_title_issue_evidence(reasons):
    text = " | ".join(str(reason or "").lower() for reason in reasons or [])
    has_title = (
        "title words:" in text
        or "series identity" in text
        or "single-word series identity" in text
    )
    has_issue = (
        "issue range" in text
        or "book/volume token" in text
        or "issue/part token" in text
        or "issue/part evidence" in text
        or "issue title:" in text
    )
    return bool(has_title and has_issue)


EXACT_UNIT_POSITIVE_EVIDENCE = {
    "exact_issue_number",
    "exact_chapter_number",
    "exact_volume_number",
    "singleton_exact_bare_volume_number",
}


def exact_candidate_folder_context(candidate, item):
    """Require a directory whose final meaningful component is the exact series."""

    path = str((candidate or {}).get("filename") or (candidate or {}).get("path") or "").replace("\\", "/")
    if "/" not in path:
        return False
    directory = path.rsplit("/", 1)[0].strip("/")
    if not directory:
        return False
    return bool(series_directory_matches_item(series_directory_cohort_root(directory), item))


def auto_inspect_non_overridable_blockers(candidate):
    flags = {
        "already_downloading": "already_downloading",
        "already_imported": "already_imported",
        "already_present": "already_present",
        "duplicate": "duplicate",
        "duplicate_active": "duplicate_active",
        "known_malicious": "known_malicious",
        "malicious": "known_malicious",
        "unsafe_locator": "unsafe_locator",
    }
    return [code for key, code in flags.items() if bool((candidate or {}).get(key))]


def effective_direct_match_score(row):
    if not isinstance(row, dict):
        return 0
    gate = row.get("auto_grab") if isinstance(row.get("auto_grab"), dict) else {}
    try:
        score = int(gate.get("score") or row.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    reasons = []
    reasons.extend(row.get("match_reasons") or [])
    reasons.extend(gate.get("reasons") or [])
    if direct_title_issue_evidence(reasons):
        try:
            score = max(score, int(gate.get("match_score") or 0))
        except (TypeError, ValueError):
            pass
    return score


TERMINAL_IMAGE_IMPRINT_CANDIDATE_RE = re.compile(
    r"\s*\(\s*image\s*,\s*(?:19|20)\d{2}[-_.](?:0?[1-9]|1[0-2])\s*\)(?=\.(?:cbz|cbr|pdf)$)",
    re.I,
)


def compatibility_title_without_terminal_image_imprint(filename, item):
    """Remove a publisher date only after exact title/issue proof.

    The shared unit parser correctly treats numeric ranges as coverage.  An
    Image release stamp such as ``(Image, 2018-04)`` is not coverage, but it is
    ignored only when terminal and attached to an otherwise exact wanted issue.
    """

    text = str(filename or "")
    if not TERMINAL_IMAGE_IMPRINT_CANDIDATE_RE.search(text):
        return text, False
    sanitized = TERMINAL_IMAGE_IMPRINT_CANDIDATE_RE.sub("", text)
    if filename_has_pack_or_range(sanitized, item=item)[0]:
        return text, False
    if not series_identity_match(sanitized, item).get("matched"):
        return text, False
    if not issue_number_match(sanitized, item).get("matched"):
        return text, False
    if unexpected_series_subtitle_blocker(sanitized, item):
        return text, False
    return sanitized, True


def auto_grab_candidate_verdict(candidate, item):
    filename = candidate.get("filename") or candidate.get("path") or ""
    policy_filename = filename_without_exact_issue_titles(filename, item=item)
    directory_identity_filename = ""
    if candidate.get("series_directory_handoff"):
        directory_identity_filename = str(candidate.get("series_directory_identity_filename") or "").strip()
    identity_policy_filename = filename_without_exact_issue_titles(
        directory_identity_filename or policy_filename,
        item=item,
    )
    blockers = []
    review_reasons = []
    reasons = []
    proof_bound_identity = bool(
        (item or {}).get("singleton_issue_proof")
        or (item or {}).get("collected_singleton_proof")
    )
    identity_filename = (
        authoritative_candidate_identity_text(candidate, policy_filename)
        if proof_bound_identity
        else identity_policy_filename
    )
    details = item_match_details(identity_filename, item)
    punctuation_identity_blocker = significant_terminal_punctuation_series_conflict(filename, item)
    if punctuation_identity_blocker:
        blockers.append(punctuation_identity_blocker)
    unit_compatibility = None
    if inkdrop_candidate_matching:
        compatibility_title, ignored_image_imprint_date = compatibility_title_without_terminal_image_imprint(identity_filename, item)
        if proof_bound_identity:
            unit_compatibility, _identity_text = candidate_identity_compatibility(
                candidate,
                compatibility_title,
                item,
            )
        else:
            unit_candidate = dict(candidate or {})
            unit_candidate.setdefault("provider_id", "slskd")
            unit_candidate["title"] = compatibility_title
            unit_compatibility = inkdrop_candidate_matching.candidate_compatibility(unit_candidate, item)
        blockers.extend(unit_compatibility.get("rejection_codes") or [])
        review_reasons.extend(unit_compatibility.get("review_codes") or [])
        reasons.extend(unit_compatibility.get("positive_evidence") or [])
        if ignored_image_imprint_date:
            reasons.append("terminal Image publisher/date metadata ignored for unit coverage parsing")
    trusted_singleton_match = bool(
        TRUSTED_SINGLETON_POSITIVE_EVIDENCE
        & set((unit_compatibility or {}).get("positive_evidence") or [])
    )
    try:
        match_score = int(details.get("score") or 0)
    except (TypeError, ValueError):
        match_score = 0
    if not details.get("matched") and not trusted_singleton_match:
        blockers.extend(details.get("penalties") or ["candidate no longer matches row"])
    if details.get("penalties") and not trusted_singleton_match:
        blockers.extend(details.get("penalties") or [])
    else:
        reasons.extend(details.get("reasons") or [])

    if has_non_comic_context(filename) and not has_comic_context(filename):
        blockers.append("non-comic path context")

    language_blocker = source_language_blocker(filename)
    if language_blocker:
        blockers.append(language_blocker)
    western_language_blocker = western_comic_language_confidence_blocker(filename, candidate, item)
    if western_language_blocker:
        blockers.append(western_language_blocker)
    else:
        provenance_reason = english_source_confidence_reason(filename, candidate, item)
        if provenance_reason:
            reasons.append(provenance_reason)
    blockers.extend(slskd_learning_language_blockers(candidate, item))

    malformed_reason = malformed_unit_syntax_reason(filename, item=item)
    if malformed_reason:
        blockers.append(malformed_reason)

    ext = extension_for(filename)
    is_pack, pack_reason = filename_has_pack_or_range(filename, item=item)
    supplemental_reason = supplemental_autopick_reason(filename, item)
    if supplemental_reason:
        blockers.append(supplemental_reason)
    if is_pack:
        reasons.append(f"pack/range allowed by confidence policy: {pack_reason}")

    strict_match, strict_reason = strict_series_match(filename, item)
    if trusted_singleton_match:
        strict_match = True
        strict_reason = "trusted singleton title and unit identity matched"
    if strict_match:
        reasons.append(strict_reason)
    else:
        review_reasons.append(f"soft series check: {strict_reason}")

    subtitle_blocker = unexpected_series_subtitle_blocker(filename, item)
    if subtitle_blocker and not trusted_singleton_match:
        blockers.append(subtitle_blocker)
    elif subtitle_blocker:
        reasons.append("shared singleton compatibility superseded soft subtitle parsing")

    year_range_blocker = explicit_year_range_conflict(filename, item)
    if year_range_blocker:
        blockers.append(year_range_blocker)

    identity_blocker, identity_reasons, identity_review_reasons = duplicate_identity_gate(filename, item, candidate=candidate)
    if identity_blocker:
        blockers.append(identity_blocker)
    reasons.extend(identity_reasons)
    review_reasons.extend(identity_review_reasons)
    blockers.extend(auto_inspect_non_overridable_blockers(candidate))

    direct_match = bool(strict_match and direct_title_issue_evidence([*(details.get("reasons") or []), *reasons]))
    direct_match_score_ok = bool(direct_match and match_score >= AUTO_GRAB_DIRECT_MATCH_MIN_SCORE)
    archive_pack_eligible = bool(ext in ARCHIVE_EXTENSIONS and is_pack and strict_match)
    archive_exact_issue_eligible = bool(
        ext in AUTO_GRAB_EXACT_ARCHIVE_EXTENSIONS
        and not is_pack
        and strict_match
        and direct_match
        and match_score >= AUTO_GRAB_DIRECT_MATCH_MIN_SCORE
    )
    if ext not in COMIC_EXTENSIONS:
        blockers.append(f"extension {ext or 'unknown'} is not a comic import format")
    elif ext in AUTO_GRAB_EXTENSIONS:
        reasons.append(f"auto-pickable comic extension {ext}")
    elif ext in ARCHIVE_EXTENSIONS and archive_pack_eligible:
        reasons.append(f"auto-pickable archive pack extension {ext}")
    elif ext in ARCHIVE_EXTENSIONS and archive_exact_issue_eligible:
        reasons.append(f"auto-pickable exact issue archive extension {ext}")
    elif ext in ARCHIVE_EXTENSIONS:
        review_reasons.append(f"archive extension {ext} needs a strict series pack/range match before auto-pick")
    else:
        review_reasons.append(f"extension {ext or 'unknown'} is importable only after manual handling")

    score = int(candidate.get("score") or 0)
    score_ok = bool(score >= AUTO_GRAB_MIN_SCORE or direct_match_score_ok)
    if not score_ok:
        review_reasons.append(f"score {score} below auto-pick threshold {AUTO_GRAB_MIN_SCORE}")
    else:
        if score >= AUTO_GRAB_MIN_SCORE:
            reasons.append(f"score {score} meets auto-pick threshold")
        else:
            reasons.append(
                f"exact title/issue match score {match_score} meets direct auto-pick threshold "
                f"{AUTO_GRAB_DIRECT_MATCH_MIN_SCORE}; downloader score is {score}"
            )

    try:
        size = int(candidate.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    size_ceiling = auto_grab_size_ceiling(filename)
    size_floor = SLSKD_PREFERRED_EXACT_MIN_BYTES if direct_match and not is_pack else AUTO_GRAB_MIN_BYTES
    if size < size_floor:
        review_reasons.append("file is smaller than the preferred exact-match size")
    elif size > size_ceiling:
        review_reasons.append("file is larger than safe auto-grab size ceiling")
    else:
        if is_pack:
            reasons.append("file size is in safe pack range")
        elif size_floor < AUTO_GRAB_MIN_BYTES:
            reasons.append("file size is in safe exact-match range")
        else:
            reasons.append("file size is in safe single-item range")

    if candidate.get("locked"):
        blockers.append("locked file")

    available, available_reason = availability_gate(candidate)
    if available:
        reasons.append(available_reason)
    else:
        reasons.append(f"{available_reason}; SLSKD can queue the transfer")

    blockers = list(dict.fromkeys(str(value) for value in blockers if value))
    review_reasons = list(dict.fromkeys(str(value) for value in review_reasons if value))
    reasons = list(dict.fromkeys(str(value) for value in reasons if value))
    leaf = filename_leaf(filename)
    leaf_series_match, _leaf_series_reason = strict_series_match(leaf, item)
    leaf_issue_match = issue_number_match(leaf, item)
    leaf_compatibility, _leaf_identity = candidate_identity_compatibility(candidate, leaf, item)
    leaf_positive = set(leaf_compatibility.get("positive_evidence") or [])
    exact_leaf_identity = bool(
        leaf_series_match
        and leaf_issue_match.get("matched")
        and not leaf_compatibility.get("rejection_codes")
        and not leaf_compatibility.get("review_codes")
        and bool(leaf_positive & (EXACT_UNIT_POSITIVE_EVIDENCE | TRUSTED_SINGLETON_POSITIVE_EVIDENCE))
    )
    exact_folder_context = exact_candidate_folder_context(candidate, item)
    size_only_review = bool(
        review_reasons == ["file is smaller than the preferred exact-match size"]
    )
    auto_inspect_eligible = bool(
        not blockers
        and not is_pack
        and direct_match
        and strict_match
        and exact_leaf_identity
        and exact_folder_context
        and (ext in AUTO_GRAB_EXTENSIONS or archive_exact_issue_eligible)
        and score_ok
        and AUTO_INSPECT_HARD_MIN_BYTES <= size < size_floor
        and size <= size_ceiling
        and size_only_review
    )
    if auto_inspect_eligible:
        reasons.append("exact file and folder identity passed inspection handoff checks")
    autopick_eligible = (
        not blockers
        and not (
            proof_bound_identity
            and (unit_compatibility or {}).get("review_codes")
        )
        and (ext in AUTO_GRAB_EXTENSIONS or archive_pack_eligible or archive_exact_issue_eligible)
        and score_ok
        and size_floor <= size <= size_ceiling
    )
    if blockers:
        verdict = "blocked"
    elif review_reasons:
        verdict = "needs_review"
    else:
        verdict = "auto_grab_safe"
    return {
        "verdict": verdict,
        "score": score,
        "reasons": reasons,
        "review_reasons": review_reasons,
        "blockers": blockers,
        "autopick_eligible": autopick_eligible,
        "auto_inspect_eligible": auto_inspect_eligible,
        "inspection_message": AUTO_INSPECT_USER_MESSAGE if auto_inspect_eligible else "",
        "preferred_size_bytes": SLSKD_PREFERRED_EXACT_MIN_BYTES,
        "inspection_hard_min_bytes": AUTO_INSPECT_HARD_MIN_BYTES,
        "exact_leaf_identity": exact_leaf_identity,
        "exact_folder_context": exact_folder_context,
        "extension": ext,
        "match_score": match_score,
        "direct_match_confidence": direct_match,
        "is_pack_candidate": is_pack,
        "is_archive_pack_candidate": archive_pack_eligible,
        "is_archive_exact_issue_candidate": archive_exact_issue_eligible,
        "supplemental_reason": supplemental_reason,
        "size_bytes": size,
        "size_floor_bytes": size_floor,
        "size_ceiling_bytes": size_ceiling,
        "policy_version": PROBE_SCHEMA_VERSION,
        "target_compatibility": unit_compatibility or {},
        "rejection_codes": list((unit_compatibility or {}).get("rejection_codes") or []),
    }


def medium_confidence_exact_series_autopick(row, item):
    if not isinstance(row, dict):
        return False
    gate = row.get("auto_grab") if isinstance(row.get("auto_grab"), dict) else {}
    if not gate.get("autopick_eligible") or gate.get("blockers"):
        return False
    try:
        score = int(gate.get("score") or row.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    if score < AUTO_GRAB_MEDIUM_SCORE:
        return False
    title_words = important_words((item or {}).get("series") or (item or {}).get("query"))
    non_numeric_title_words = [word for word in title_words if token_number(word) is None]
    if len(non_numeric_title_words) < 2:
        return False
    reasons = []
    reasons.extend(str(value).lower() for value in (row.get("match_reasons") or []))
    reasons.extend(str(value).lower() for value in (gate.get("reasons") or []))
    text = " | ".join(reasons)
    has_series_phrase = "series identity phrase matched" in text
    has_issue = (
        "issue/part token" in text
        or "issue range" in text
        or "book/volume token" in text
        or "issue/part evidence" in text
    )
    return bool(has_series_phrase and has_issue)


def close_direct_match_best_candidate(row):
    if not isinstance(row, dict):
        return False
    gate = row.get("auto_grab") if isinstance(row.get("auto_grab"), dict) else {}
    if not gate.get("autopick_eligible") or gate.get("blockers"):
        return False
    if not gate.get("direct_match_confidence"):
        return False
    score = auto_grab_sort_int(row.get("score") or gate.get("score"), 0)
    if score < AUTO_GRAB_MEDIUM_SCORE:
        return False
    review_reasons = [
        str(reason)
        for reason in list(gate.get("review_reasons") or [])
        if reason
        and reason != "lower-ranked autopick candidate"
        and not str(reason).startswith("score ")
        and not str(reason).startswith("best candidate is not clearly ahead")
        and not str(reason).startswith("best candidate score ")
    ]
    return not review_reasons


def auto_grab_sort_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def auto_grab_candidate_display_rank(row):
    row = row if isinstance(row, dict) else {}
    gate = row.get("auto_grab") if isinstance(row.get("auto_grab"), dict) else {}
    verdict = str(gate.get("verdict") or "")
    if verdict == "auto_grab_safe":
        bucket = 0
    elif gate.get("autopick_eligible"):
        bucket = 1
    elif verdict == "needs_review":
        bucket = 2
    elif verdict == "blocked":
        bucket = 4
    else:
        bucket = 3
    rank = auto_grab_sort_int(gate.get("autopick_rank"), 999999)
    score = auto_grab_sort_int(row.get("score"), 0)
    upload_speed = auto_grab_sort_int(row.get("upload_speed"), 0)
    free_slot = 1 if row.get("has_free_upload_slot") else 0
    filename = normalize(row.get("filename") or row.get("path") or "")
    return (bucket, rank, -score, -free_slot, -upload_speed, filename)


def sort_auto_grab_candidates_for_display(rows):
    return sorted([row for row in rows or [] if isinstance(row, dict)], key=auto_grab_candidate_display_rank)


def annotate_auto_grab_verdicts(candidates, item):
    rows = []
    for candidate in candidates or []:
        annotated = dict(candidate)
        annotated["auto_grab"] = auto_grab_candidate_verdict(annotated, item)
        rows.append(annotated)

    if not rows:
        return rows

    def score_for(row):
        try:
            return int(row.get("score") or 0)
        except (TypeError, ValueError):
            return 0

    leaf_counts = {}
    for row in rows:
        leaf = normalize(filename_leaf(row.get("filename") or row.get("path")))
        if leaf:
            leaf_counts[leaf] = leaf_counts.get(leaf, 0) + 1

    eligible = [
        row for row in rows
        if ((row.get("auto_grab") or {}).get("autopick_eligible"))
        and (row.get("auto_grab") or {}).get("verdict") != "blocked"
    ]
    eligible.sort(key=score_for, reverse=True)
    for rank, row in enumerate(eligible, start=1):
        gate = row.get("auto_grab") or {}
        gate["autopick_rank"] = rank
        row["auto_grab"] = gate

    winner = eligible[0] if eligible else None
    winner_score = score_for(winner) if winner else 0
    next_score = score_for(eligible[1]) if len(eligible) > 1 else None
    winner_gap = None if next_score is None else winner_score - next_score
    winner_exact_medium = medium_confidence_exact_series_autopick(winner, item)
    winner_direct_close = close_direct_match_best_candidate(winner)
    winner_is_clear = bool(
        winner
        and (
            winner_score >= AUTO_GRAB_HIGH_SCORE
            or winner_gap is None
            or winner_gap >= AUTO_GRAB_CLEAR_WIN_DELTA
            or winner_exact_medium
            or winner_direct_close
        )
    )

    for row in rows:
        gate = row.get("auto_grab") or {}
        if gate.get("verdict") == "auto_grab_safe":
            gate["verdict"] = "needs_review"
        if gate.get("autopick_eligible") and row is not winner:
            reasons = list(gate.get("review_reasons") or [])
            reasons.append("lower-ranked autopick candidate")
            gate["review_reasons"] = list(dict.fromkeys(reasons))
        row["auto_grab"] = gate

    if winner:
        gate = winner.get("auto_grab") or {}
        if winner_is_clear:
            gate["verdict"] = "auto_grab_safe"
            gate = clear_threshold_review_reasons(gate)
            reasons = list(gate.get("reasons") or [])
            try:
                match_score = int(gate.get("match_score") or 0)
            except (TypeError, ValueError):
                match_score = 0
            if winner_score >= AUTO_GRAB_HIGH_SCORE:
                reasons.append(f"best candidate selected at high confidence ({winner_score})")
            elif gate.get("direct_match_confidence") and match_score >= AUTO_GRAB_DIRECT_MATCH_MIN_SCORE and winner_score < AUTO_GRAB_MEDIUM_SCORE:
                if winner_gap is None:
                    reasons.append(
                        f"only exact title/issue candidate selected by direct-match confidence "
                        f"({match_score}; downloader score {winner_score})"
                    )
                else:
                    reasons.append(
                        f"best exact title/issue candidate selected by direct-match confidence "
                        f"({match_score}; downloader score {winner_score} vs {next_score})"
                    )
            elif winner_gap is None:
                reasons.append(f"only eligible candidate selected at medium confidence ({winner_score})")
            elif winner_exact_medium:
                reasons.append(f"best exact series/issue candidate selected at medium confidence ({winner_score})")
            elif winner_direct_close:
                reasons.append(f"best exact title/issue candidate selected at medium confidence ({winner_score} vs {next_score})")
            else:
                reasons.append(f"best candidate selected by clear score gap ({winner_score} vs {next_score})")
            leaf = normalize(filename_leaf(winner.get("filename") or winner.get("path")))
            if leaf_counts.get(leaf, 0) > 1:
                reasons.append("duplicate-looking filename exists; choosing top scored source")
            gate["reasons"] = list(dict.fromkeys(reasons))
        else:
            gate["verdict"] = "needs_review"
            reasons = list(gate.get("review_reasons") or [])
            if next_score is None:
                reasons.append(f"best candidate score {winner_score} is below high confidence")
            else:
                reasons.append(f"best candidate is not clearly ahead ({winner_score} vs {next_score})")
            gate["review_reasons"] = list(dict.fromkeys(reasons))
        gate["autopick_gap"] = winner_gap
        winner["auto_grab"] = gate
    return sort_auto_grab_candidates_for_display(rows)


def retry_candidate_has_direct_match(row):
    gate = (row or {}).get("auto_grab") if isinstance((row or {}).get("auto_grab"), dict) else {}
    reasons = []
    reasons.extend(str(value) for value in ((row or {}).get("match_reasons") or []))
    reasons.extend(str(value) for value in (gate.get("reasons") or []))
    return direct_title_issue_evidence(reasons)


def retry_candidate_after_failure_eligible(row):
    if not isinstance(row, dict) or row.get("manual_source_bad_candidate"):
        return False
    gate = row.get("auto_grab") if isinstance(row.get("auto_grab"), dict) else {}
    if gate.get("verdict") == "blocked" or gate.get("blockers"):
        return False
    score = effective_direct_match_score(row)
    if score < AUTO_GRAB_RETRY_MIN_SCORE:
        return False
    filename = row.get("filename") or row.get("path") or ""
    ext = gate.get("extension") or extension_for(filename)
    if (
        ext not in AUTO_GRAB_EXTENSIONS
        and not gate.get("is_archive_pack_candidate")
        and not gate.get("is_archive_exact_issue_candidate")
    ):
        return False
    try:
        size = int(gate.get("size_bytes") or row.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    try:
        floor = int(gate.get("size_floor_bytes") or AUTO_GRAB_MIN_BYTES)
    except (TypeError, ValueError):
        floor = AUTO_GRAB_MIN_BYTES
    try:
        ceiling = int(gate.get("size_ceiling_bytes") or auto_grab_size_ceiling(filename))
    except (TypeError, ValueError):
        ceiling = auto_grab_size_ceiling(filename)
    if size < floor or size > ceiling:
        return False
    return retry_candidate_has_direct_match(row)


def near_threshold_direct_match_eligible(row):
    if not isinstance(row, dict) or row.get("manual_source_bad_candidate"):
        return False
    gate = row.get("auto_grab") if isinstance(row.get("auto_grab"), dict) else {}
    if gate.get("verdict") == "blocked" or gate.get("blockers"):
        return False
    score = effective_direct_match_score(row)
    if score < AUTO_GRAB_DIRECT_MATCH_MIN_SCORE:
        return False
    filename = row.get("filename") or row.get("path") or ""
    ext = gate.get("extension") or extension_for(filename)
    if (
        ext not in AUTO_GRAB_EXTENSIONS
        and not gate.get("is_archive_pack_candidate")
        and not gate.get("is_archive_exact_issue_candidate")
    ):
        return False
    try:
        size = int(gate.get("size_bytes") or row.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    try:
        floor = int(gate.get("size_floor_bytes") or AUTO_GRAB_MIN_BYTES)
    except (TypeError, ValueError):
        floor = AUTO_GRAB_MIN_BYTES
    try:
        ceiling = int(gate.get("size_ceiling_bytes") or auto_grab_size_ceiling(filename))
    except (TypeError, ValueError):
        ceiling = auto_grab_size_ceiling(filename)
    if size < floor or size > ceiling:
        return False
    return retry_candidate_has_direct_match(row)


def clear_threshold_review_reasons(gate):
    gate["review_reasons"] = [
        reason for reason in list(gate.get("review_reasons") or [])
        if not str(reason).startswith("score ")
        and reason != "lower-ranked autopick candidate"
        and not str(reason).startswith("best candidate is not clearly ahead")
        and not str(reason).startswith("best candidate score ")
    ]
    return gate


def annotate_bad_candidate_verdicts(candidates, review_id):
    if not review_id and inkdrop_state is None:
        return candidates
    rows = []
    had_bad_candidate = False
    for candidate in candidates or []:
        annotated = dict(candidate)
        bad_match = bad_candidate_match(review_id, annotated)
        if not bad_match:
            bad_match = cached_bad_candidate_match(annotated, "manual_source_bad_candidate")
        if not bad_match:
            bad_match = cached_bad_candidate_match(annotated, "source_memory_bad_candidate")
        if bad_match:
            failure_reason = bad_match.get("reason") or "failed verification/import"
            failure_label = bad_match.get("failure_label") or bad_match.get("label") or failure_reason
            failure_detail = bad_match.get("detail") or ""
            had_bad_candidate = True
            gate = dict(annotated.get("auto_grab") or {})
            gate["reasons"] = [
                reason for reason in list(gate.get("reasons") or [])
                if not str(reason).startswith("best candidate selected")
                and not str(reason).startswith("best remaining candidate selected")
                and not str(reason).startswith("only eligible candidate selected")
                and not str(reason).startswith("only remaining eligible candidate selected")
            ]
            blockers = list(gate.get("blockers") or [])
            blockers.append(f"previous candidate failure: {failure_label}")
            gate["blockers"] = list(dict.fromkeys(str(value) for value in blockers if value))
            gate["review_reasons"] = list(gate.get("review_reasons") or [])
            gate["verdict"] = "blocked"
            gate["autopick_eligible"] = False
            gate["previous_failure"] = {
                "reason": failure_reason,
                "label": failure_label,
                "detail": failure_detail,
                "kind": bad_match.get("failure_kind") or bad_match.get("kind"),
                "ts": bad_match.get("ts"),
                "ts_iso": bad_match.get("ts_iso"),
                "detected_filename": bad_match.get("detected_filename"),
            }
            annotated["auto_grab"] = gate
            annotated["manual_source_bad_candidate"] = gate["previous_failure"]
            if bad_match.get("source_memory"):
                annotated["source_memory_bad_candidate"] = gate["previous_failure"]
        rows.append(annotated)
    if not any((row.get("auto_grab") or {}).get("verdict") == "auto_grab_safe" for row in rows):
        def score_for(row):
            try:
                return int(row.get("score") or 0)
            except (TypeError, ValueError):
                return 0

        eligible = []
        for row in rows:
            gate = row.get("auto_grab") or {}
            if row.get("manual_source_bad_candidate") or gate.get("verdict") == "blocked":
                continue
            if (
                gate.get("autopick_eligible")
                or (had_bad_candidate and retry_candidate_after_failure_eligible(row))
                or near_threshold_direct_match_eligible(row)
            ):
                eligible.append(row)
        eligible.sort(key=score_for, reverse=True)
        winner = eligible[0] if eligible else None
        winner_score = score_for(winner) if winner else 0
        next_score = score_for(eligible[1]) if len(eligible) > 1 else None
        winner_gap = None if next_score is None else winner_score - next_score
        retry_relaxed = bool(had_bad_candidate and retry_candidate_after_failure_eligible(winner))
        near_threshold_direct = bool(not had_bad_candidate and near_threshold_direct_match_eligible(winner))
        if winner and (
            winner_score >= AUTO_GRAB_HIGH_SCORE
            or winner_gap is None
            or winner_gap >= AUTO_GRAB_CLEAR_WIN_DELTA
            or (had_bad_candidate and winner_score >= AUTO_GRAB_MEDIUM_SCORE)
            or retry_relaxed
            or near_threshold_direct
        ):
            gate = dict(winner.get("auto_grab") or {})
            gate["verdict"] = "auto_grab_safe"
            if retry_relaxed:
                gate["autopick_eligible"] = True
                gate["retry_after_failed_candidate"] = True
            if near_threshold_direct:
                gate["autopick_eligible"] = True
                gate["near_threshold_direct_match"] = True
            reasons = list(gate.get("reasons") or [])
            if winner_score >= AUTO_GRAB_HIGH_SCORE:
                reasons.append(f"best remaining candidate selected at high confidence ({winner_score})")
            elif near_threshold_direct:
                if winner_gap is None:
                    reasons.append(f"only exact title/issue candidate selected at near-threshold confidence ({winner_score})")
                else:
                    reasons.append(f"best exact title/issue candidate selected near threshold ({winner_score} vs {next_score})")
            elif retry_relaxed:
                reasons.append(f"previous candidate failed; retrying exact-match remaining candidate at retry confidence ({winner_score})")
                reasons.append("duplicate-title retry allowed; Kavita verifier guards final import")
            elif winner_gap is None:
                reasons.append(f"only remaining eligible candidate selected at medium confidence ({winner_score})")
            elif had_bad_candidate:
                reasons.append(f"previous candidate failed; retrying best remaining candidate at medium confidence ({winner_score})")
            else:
                reasons.append(f"best remaining candidate selected by clear score gap ({winner_score} vs {next_score})")
            if retry_relaxed:
                gate["review_reasons"] = []
            elif near_threshold_direct:
                gate = clear_threshold_review_reasons(gate)
            else:
                gate["review_reasons"] = [
                    reason for reason in list(gate.get("review_reasons") or [])
                    if reason != "lower-ranked autopick candidate"
                ]
            gate["reasons"] = list(dict.fromkeys(reasons))
            gate["autopick_gap"] = winner_gap
            winner["auto_grab"] = gate
    return sort_auto_grab_candidates_for_display(rows)


def auto_grab_counts(candidates):
    counts = {"auto_grab_safe": 0, "needs_review": 0, "blocked": 0}
    for candidate in candidates or []:
        verdict = str(((candidate or {}).get("auto_grab") or {}).get("verdict") or "")
        if verdict in counts:
            counts[verdict] += 1
    return counts


def entry_auto_grab_counts(entry):
    if not isinstance(entry, dict):
        return {"auto_grab_safe": 0, "needs_review": 0, "blocked": 0}
    candidates = entry.get("candidates") or []
    counts = auto_grab_counts(candidates)
    if not any(counts.values()):
        counts["auto_grab_safe"] = int(entry.get("auto_grab_safe_count") or 0)
        counts["needs_review"] = int(entry.get("auto_grab_review_count") or 0)
        counts["blocked"] = int(entry.get("auto_grab_blocked_count") or 0)
    return counts


def waiting_review_ids():
    actions = load_actions()
    waiting = actions.get("manual_source_waiting")
    if not isinstance(waiting, dict):
        return set()
    return {str(review_id) for review_id in waiting if review_id}


def slskd_download_rows():
    try:
        payload = slskd_get("/transfers/downloads", timeout=15) or []
    except Exception as exc:
        log("slskd_auto_grab_transfer_lookup_error", error=f"{type(exc).__name__}: {exc}")
        raise SLSKDTransferLookupError(f"SLSKD transfer lookup timed out or failed: {exc}") from exc
    rows = []
    for user in payload if isinstance(payload, list) else []:
        username = str(user.get("username") or "")
        for directory in user.get("directories") or []:
            directory_name = str((directory or {}).get("directory") or "")
            for row in (directory or {}).get("files") or []:
                if not isinstance(row, dict):
                    continue
                item = dict(row)
                item.setdefault("username", username)
                item.setdefault("directory", directory_name)
                rows.append(item)
    return rows


def slskd_transfer_locator_digests(row):
    """Return exact peer/path/size digests that a live transfer can prove."""
    row = row if isinstance(row, dict) else {}
    username = str(row.get("username") or "").strip()
    filename = str(row.get("filename") or row.get("remoteFilename") or "").strip()
    directory = str(row.get("directory") or "").strip()
    size = row.get("size") or row.get("size_bytes")
    if not username or not filename or size in (None, ""):
        return set()
    candidates = [filename]
    if directory and not any(separator in filename for separator in ("\\", "/")):
        candidates.append(directory.rstrip("\\/") + "\\" + filename)
    return {
        digest
        for candidate_filename in candidates
        for digest in (
            slskd_private_locator_digest({
                "username": username,
                "filename": candidate_filename,
                "size": size,
            }),
        )
        if digest
    }


def reconcile_slskd_sibling_queue_projections(*, observed_at=None):
    """Release queue ownership after the sibling that replaced a failed task ends."""
    if inkdrop_state is None or not INKDROP_STATE_DB:
        return {"checked": 0, "reconciled": 0, "unchanged": 0}
    observed_at = float(observed_at or now())
    with inkdrop_state.connect_read(INKDROP_STATE_DB) as con:
        queue_rows = [dict(row) for row in con.execute(
            "select id,raw_json from queue_items where raw_json is not null"
        ).fetchall()]
    pending = []
    for queue in queue_rows:
        try:
            queue_raw = json.loads(queue.get("raw_json") or "{}")
        except (TypeError, ValueError):
            queue_raw = {}
        task_id = str((queue_raw or {}).get("retired_slskd_download_task_id") or "").strip()
        sibling_id = str((queue_raw or {}).get("authoritative_sibling_download_task_id") or "").strip()
        if task_id and sibling_id:
            pending.append((str(queue.get("id") or ""), task_id))
    reconciled = 0
    unchanged = 0
    for queue_id, task_id in pending:
        with inkdrop_state.connect_read(INKDROP_STATE_DB) as con:
            task_row = con.execute("select * from download_tasks where id=?", (task_id,)).fetchone()
        task = dict(task_row) if task_row else {}
        raw = inkdrop_state.download_task_raw_payload(task)
        reservation_id = str(raw.get("reservation_id") or task.get("source_attempt_id") or "").strip()
        status = str(task.get("status") or "").strip().lower()
        if (
            not task
            or inkdrop_state.download_task_is_activeish(task)
            or status not in inkdrop_state.SLSKD_RESERVATION_TERMINAL_STATUSES
            or not reservation_id
        ):
            unchanged += 1
            continue
        transition = inkdrop_state.transition_slskd_candidate_task(
            INKDROP_STATE_DB,
            queue_id,
            reservation_id,
            status,
            transfer_id=task.get("external_id"),
            reason=str(task.get("failure_reason") or raw.get("failure_reason") or "SLSKD transfer ended"),
            observed_at=observed_at,
        )
        reconciled += int(bool(transition.get("ok")))
        unchanged += int(not transition.get("ok"))
    return {"checked": len(pending), "reconciled": reconciled, "unchanged": unchanged}


def persisted_slskd_transfer_locator_digests(external_id):
    """Return exact locator digests from durable, previously recorded transfers."""
    external_id = str(external_id or "").strip()
    if not external_id:
        return set()
    actions = read_json(MANUAL_REVIEW_ACTIONS_FILE, {}) or {}
    digests = set()

    def visit(value):
        if isinstance(value, dict):
            if str(value.get("id") or "").strip() == external_id:
                digest = slskd_private_locator_digest(value)
                if digest:
                    digests.add(digest)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(actions)
    return digests


def reconcile_slskd_transfer_identity_tasks(*, observed_at=None, transfer_rows=None):
    """Bind unresolved handoffs to live transfers or retire proven misses."""
    if inkdrop_state is None or not INKDROP_STATE_DB:
        return {"ok": False, "reason": "inkdrop_state_unavailable", "recovered": 0, "retired": 0}
    observed_at = float(observed_at or now())
    sibling_projection = reconcile_slskd_sibling_queue_projections(observed_at=observed_at)
    try:
        live_rows = slskd_download_rows() if transfer_rows is None else list(transfer_rows)
    except Exception as exc:
        return {
            "ok": False,
            "reason": "slskd_transfer_inventory_unavailable",
            "error": type(exc).__name__,
            "recovered": 0,
            "retired": 0,
            "sibling_projection": sibling_projection,
        }
    digest_rows = {}
    id_rows = {}
    inventory_complete = True
    filename_inventory_complete = True
    live_filenames = set()
    for row in live_rows:
        transfer_id = str((row or {}).get("id") or "").strip()
        transfer_filename = str((row or {}).get("filename") or (row or {}).get("remoteFilename") or "").strip()
        if not transfer_id or not transfer_filename:
            filename_inventory_complete = False
        elif transfer_filename:
            live_filenames.add(filename_leaf(transfer_filename).casefold())
        if transfer_id:
            id_rows.setdefault(transfer_id, []).append(row)
        digests = slskd_transfer_locator_digests(row)
        if not digests or not transfer_id:
            inventory_complete = False
            continue
        for digest in digests:
            digest_rows.setdefault(digest, []).append(row)
    with inkdrop_state.connect_read(INKDROP_STATE_DB) as con:
        tasks = [
            dict(row)
            for row in con.execute(
                """
                select * from download_tasks
                where external_id is null
                  and lower(coalesce(status,'')) in
                      ('started_waiting','already_downloading','download_started',
                       'downloading','transfer_in_progress','enqueue_response_ambiguous',
                       'ambiguous_enqueue_response')
                  and (
                        lower(coalesce(download_client,''))='slskd'
                     or lower(coalesce(source,'')) in ('slskd','soulseek')
                     or lower(coalesce(protocol,'')) in ('slskd','soulseek')
                  )
                order by coalesce(updated_at,started_at,0),id
                """
            ).fetchall()
        ]
        completed_owner_tasks = [
            dict(row)
            for row in con.execute(
                """
                select dt.* from download_tasks dt
                join queue_items q on q.id=dt.queue_id
                where trim(coalesce(dt.external_id,''))<>''
                  and q.active=1
                  and lower(coalesce(q.state,'')) not in
                      ('verified','satisfied','superseded_duplicate','removed','ignored','inactive','needs_you','blocked')
                  and (
                        lower(coalesce(dt.download_client,''))='slskd'
                     or lower(coalesce(dt.source,'')) in ('slskd','soulseek')
                     or lower(coalesce(dt.protocol,'')) in ('slskd','soulseek')
                  )
                order by coalesce(dt.updated_at,dt.started_at,0),dt.id
                """
            ).fetchall()
        ]
    completed_recovered = 0
    completed_ambiguous = 0
    completed_unchanged = 0
    tasks_by_external_id = {}
    for task in completed_owner_tasks:
        tasks_by_external_id.setdefault(str(task.get("external_id") or "").strip(), []).append(task)
    for external_id, owner_tasks in tasks_by_external_id.items():
        matching_rows = id_rows.get(external_id) or []
        if len(owner_tasks) != 1 or len(matching_rows) != 1:
            if matching_rows and any(slskd_transfer_completed(row) for row in matching_rows):
                completed_ambiguous += 1
            else:
                completed_unchanged += 1
            continue
        transfer = matching_rows[0]
        if not slskd_transfer_completed(transfer):
            completed_unchanged += 1
            continue
        persisted_locator_digests = persisted_slskd_transfer_locator_digests(external_id)
        transition = inkdrop_state.recover_completed_slskd_candidate_task(
            INKDROP_STATE_DB,
            owner_tasks[0].get("id"),
            external_id,
            transfer,
            observed_at=observed_at,
            legacy_locator_digest=(
                next(iter(persisted_locator_digests))
                if len(persisted_locator_digests) == 1 else None
            ),
        )
        completed_recovered += int(bool(transition.get("ok") and not transition.get("idempotent")))
        completed_unchanged += int(not transition.get("ok") or bool(transition.get("idempotent")))
    recovered = 0
    retired = 0
    ambiguous = 0
    unchanged = 0
    for task in tasks:
        raw = inkdrop_state.download_task_raw_payload(task)
        reservation_id = str(raw.get("reservation_id") or task.get("source_attempt_id") or "").strip()
        locator_digest = str(raw.get("candidate_locator_digest") or "").strip()
        queue_id = str(task.get("queue_id") or "").strip()
        if not queue_id:
            unchanged += 1
            continue
        if not reservation_id or not locator_digest:
            task_filename = filename_leaf(
                raw.get("filename") or task.get("title") or ""
            ).casefold()
            task_timestamp = float(task.get("updated_at") or task.get("started_at") or 0)
            if (
                not filename_inventory_complete
                or not task_filename
                or task_filename in live_filenames
                or task_timestamp <= 0
                or observed_at - task_timestamp < SLSKD_SLOT_REQUEST_TTL_SECONDS
            ):
                unchanged += 1
                continue
            transition = inkdrop_state.retire_unbound_slskd_candidate_task(
                INKDROP_STATE_DB,
                task.get("id"),
                reason="SLSKD reported no transfer for the legacy handoff; automatic retry scheduled",
                observed_at=observed_at,
            )
            retired += int(bool(transition.get("ok")))
            unchanged += int(not transition.get("ok"))
            continue
        matches = digest_rows.get(locator_digest) or []
        match_ids = sorted({str(row.get("id") or "").strip() for row in matches if str(row.get("id") or "").strip()})
        if len(match_ids) == 1:
            transition = inkdrop_state.transition_slskd_candidate_task(
                INKDROP_STATE_DB,
                queue_id,
                reservation_id,
                "started_waiting",
                transfer_id=match_ids[0],
                reason="SLSKD transfer identity recovered from the exact candidate binding",
                observed_at=observed_at,
                extra={"transfer_identity_recovered": True},
            )
            recovered += int(bool(transition.get("ok")))
            unchanged += int(not transition.get("ok"))
            continue
        if len(match_ids) > 1:
            transition = inkdrop_state.transition_slskd_candidate_task(
                INKDROP_STATE_DB,
                queue_id,
                reservation_id,
                "enqueue_response_ambiguous",
                reason="More than one live SLSKD transfer matched the exact candidate binding",
                observed_at=observed_at,
                extra={"live_transfer_match_count": len(match_ids)},
            )
            ambiguous += int(bool(transition.get("ok")))
            unchanged += int(not transition.get("ok"))
            continue
        try:
            deadline = float(raw.get("reservation_deadline") or raw.get("slot_request_deadline") or 0)
        except (TypeError, ValueError):
            deadline = 0
        if not inventory_complete or deadline <= 0 or deadline > observed_at:
            unchanged += 1
            continue
        transition = inkdrop_state.transition_slskd_candidate_task(
            INKDROP_STATE_DB,
            queue_id,
            reservation_id,
            "reservation_failed",
            reason="SLSKD reported no transfer for the exact candidate; automatic retry scheduled",
            observed_at=observed_at,
            extra={"authoritative_transfer_missing": True},
        )
        retired += int(bool(transition.get("ok")))
        unchanged += int(not transition.get("ok"))
    return {
        "ok": True,
        "inventory_rows": len(live_rows),
        "inventory_complete": inventory_complete,
        "filename_inventory_complete": filename_inventory_complete,
        "unresolved_tasks": len(tasks),
        "recovered": recovered,
        "retired": retired,
        "ambiguous": ambiguous,
        "unchanged": unchanged,
        "completed_owner_tasks": len(completed_owner_tasks),
        "completed_recovered": completed_recovered,
        "completed_ambiguous": completed_ambiguous,
        "completed_unchanged": completed_unchanged,
        "sibling_projection": sibling_projection,
    }


def slskd_existing_download(candidate, *, strict_path=False):
    expected_user = str(candidate.get("username") or "").lower()
    expected_file = str(candidate.get("filename") or "").replace("\\", "/").lower()
    expected_leaf = filename_leaf(expected_file).lower()
    if not expected_user or not expected_file:
        return {}
    for row in slskd_download_rows():
        username = str(row.get("username") or "").lower()
        filename = str(row.get("filename") or row.get("remoteFilename") or "").replace("\\", "/").lower()
        if username != expected_user:
            continue
        if strict_path:
            try:
                expected_size = int(float(candidate.get("size") or candidate.get("size_bytes") or 0))
                actual_size = int(float(row.get("size") or row.get("size_bytes") or 0))
            except (TypeError, ValueError):
                continue
            if expected_size > 0 and actual_size == expected_size and filename == expected_file:
                return row
        elif filename in {expected_file, expected_leaf} or filename_leaf(filename).lower() == expected_leaf:
            return row
    return {}


def slskd_download_already_exists(candidate):
    return bool(slskd_existing_download(candidate))


def auto_grab_waiting_query(entry):
    search_query = str(entry.get("search_query") or "").strip()
    if search_query:
        return search_query
    series = str(entry.get("series") or "").strip()
    issue = str(entry.get("issue") or "").strip()
    return f"{series} {issue}".strip()


ITEM_CONTEXT_FIELDS = (
    "search_query",
    "year",
    "watch_year",
    "watch_publisher",
    "publisher",
    "comicvine_id",
    "watch_id",
    "series_id",
    "queue_identity",
    "media_type",
    "unit_type",
    "issue_number",
    "chapter_number",
    "volume_number",
    "edition_id",
    "edition_marker",
    "autopilot_queue_key",
    "legacy_key",
    "source",
    "autopilot_queue",
)


def copy_item_context(entry, item):
    entry = dict(entry or {})
    if isinstance(item, dict) and item.get("autopilot_queue"):
        # The current durable queue row owns unit identity.  Never allow a
        # previously cached candidate decision to supply unit fields that the
        # current row could not safely resolve.
        for key in (
            "unit_type", "unitType", "source_unit", "issue_number",
            "chapter_number", "volume_number", "collected_number",
            "pack_member_number",
        ):
            entry.pop(key, None)
    for key in ITEM_CONTEXT_FIELDS:
        value = (item or {}).get(key)
        if value not in (None, ""):
            entry[key] = value
    return entry


def slskd_transfer_state_text(transfer):
    if not isinstance(transfer, dict):
        return ""
    return f"{transfer.get('state') or ''} {transfer.get('stateDescription') or ''}".lower()


def slskd_transfer_failed(transfer):
    state = slskd_transfer_state_text(transfer)
    return any(
        token in state
        for token in (
            "error", "failed", "cancelled", "canceled", "aborted", "rejected", "denied",
            "timedout", "timed out", "timeout", "stalled",
        )
    )


def slskd_transfer_completed(transfer):
    state = slskd_transfer_state_text(transfer)
    if slskd_transfer_failed(transfer):
        return False
    try:
        percent = float((transfer or {}).get("percentComplete") or -1)
        remaining = float((transfer or {}).get("bytesRemaining") if (transfer or {}).get("bytesRemaining") is not None else -1)
    except (TypeError, ValueError):
        percent, remaining = -1, -1
    return bool(
        "succeeded" in state
        or "completed" in state
        or (percent >= 100 and remaining == 0)
    )


def slskd_transfer_failure_reason(transfer):
    state = str((transfer or {}).get("state") or (transfer or {}).get("stateDescription") or "").strip()
    if state:
        return f"SLSKD transfer failed: {state}"
    return "SLSKD transfer failed"


def mark_probe_candidate_bad(review_id, entry, candidate, reason, transfer=None):
    actions = load_actions()
    bad = actions.setdefault("manual_source_bad_candidates", {})
    if not isinstance(bad, dict):
        bad = {}
        actions["manual_source_bad_candidates"] = bad
    rows = bad.setdefault(str(review_id), [])
    if not isinstance(rows, list):
        rows = []
    filename = (candidate or {}).get("filename") or (candidate or {}).get("path") or ""
    bad_entry = {
        "review_id": str(review_id),
        "series": (entry or {}).get("series"),
        "issue": (entry or {}).get("issue"),
        "username": (candidate or {}).get("username"),
        "filename": filename,
        "filename_leaf": filename_leaf(filename),
        "candidate_score": (candidate or {}).get("score"),
        "candidate_size": (candidate or {}).get("size"),
        "reason": "slskd_transfer_failed",
        "failure_kind": "transfer",
        "failure_label": "SLSKD transfer failed",
        "detail": str(reason or "SLSKD transfer failed"),
        "ts": now(),
        "ts_iso": utc_stamp(),
        "candidate_key": normalize_key("|".join([
            str((candidate or {}).get("username") or "").lower(),
            str(filename).replace("\\", "/").lower(),
            filename_leaf(filename).lower(),
        ])),
    }
    for key in ITEM_CONTEXT_FIELDS:
        value = (entry or {}).get(key)
        if value not in (None, ""):
            bad_entry[key] = value
    if isinstance(transfer, dict) and transfer:
        bad_entry["slskd_transfer_id"] = transfer.get("id")
        bad_entry["slskd_transfer_state"] = transfer.get("state") or transfer.get("stateDescription")
        bad_entry["slskd_transfer_requested_at"] = transfer.get("requestedAt")
    updated_existing = False
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("candidate_key") or "") != bad_entry["candidate_key"]:
            continue
        merged = dict(row)
        merged.update({key: value for key, value in bad_entry.items() if value not in (None, "")})
        rows[index] = merged
        updated_existing = True
        break
    if not updated_existing:
        rows.insert(0, bad_entry)
    bad[str(review_id)] = rows[:20]
    save_actions(actions)
    auto_grab_audit(
        "candidate_failed",
        review_id=str(review_id),
        reason=bad_entry.get("reason"),
        detail=bad_entry.get("detail"),
        failure_kind=bad_entry.get("failure_kind"),
        failure_label=bad_entry.get("failure_label"),
        series=bad_entry.get("series"),
        issue=bad_entry.get("issue"),
        filename=bad_entry.get("filename"),
        username=bad_entry.get("username"),
        candidate_score=bad_entry.get("candidate_score"),
        candidate_key=bad_entry.get("candidate_key"),
    )
    return bad_entry


def mark_waiting_record_missing_retry(review_id, entry, candidate, row, dry_run, raw_transfer=None):
    row["attempt_consumed"] = True
    row["waiting_record_recovery"] = "retry_next_candidate"
    row["retry_next_candidate"] = True
    transfer = raw_transfer if isinstance(raw_transfer, dict) and raw_transfer else (
        row.get("transfer") if isinstance(row.get("transfer"), dict) else {}
    )
    if transfer:
        try:
            delete_result = slskd_delete_download_transfer(transfer, dry_run=dry_run)
            row["delete_orphan_transfer"] = (
                privacy_safe_handoff_operation(delete_result)
                if (candidate or {}).get("series_directory_handoff_token")
                else delete_result
            )
            row["orphan_transfer_cleared"] = True
        except Exception as exc:
            row["orphan_transfer_clear_error"] = f"{type(exc).__name__}: {exc}"
            row["retry_next_candidate"] = False
            row["transient_error"] = True
            row["retry_after_seconds"] = TRANSIENT_AUTO_GRAB_RETRY_SECONDS
            row["reason"] = (
                "SLSKD waiting record was missing, but orphan transfer cleanup failed; "
                "retrying status check before trying another candidate"
            )
            return row
    if not dry_run:
        row["bad_candidate"] = mark_probe_candidate_bad(
            review_id,
            entry,
            candidate,
            row.get("reason") or "SLSKD waiting record missing",
            transfer=transfer,
        )
    return row


def mark_manual_source_waiting_api(entry, candidate, dry_run, transfer=None):
    payload = {
        "review_id": entry.get("review_id"),
        "query": auto_grab_waiting_query(entry),
        "filename": candidate.get("filename"),
        "username": candidate.get("username"),
        "candidateScore": candidate.get("score"),
        "candidateSize": candidate.get("size"),
        "autoGrab": candidate.get("auto_grab") if isinstance(candidate.get("auto_grab"), dict) else {},
        "dryRun": bool(dry_run),
    }
    for key in ITEM_CONTEXT_FIELDS:
        value = (entry or {}).get(key)
        if value not in (None, ""):
            payload[key] = value
    if isinstance(transfer, dict) and transfer:
        payload["transfer"] = transfer
    if not inkdrop_runtime_config.worker_http_callback_requested(
        endpoint_keys=("INKDROP_MARK_WAITING_API_URL",)
    ):
        return inkdrop_internal_jobs.run_manual_source_mark_waiting(payload)
    response = requests.post(
        MARK_WAITING_API_URL,
        json=payload,
        headers=inkdrop_runtime_config.worker_auth_headers(required=True),
        timeout=20,
    )
    if not response.ok:
        detail = (response.text or "").strip()
        if len(detail) > 500:
            detail = detail[:500].rstrip() + "..."
        raise RuntimeError(f"mark-waiting failed HTTP {response.status_code}: {detail or response.reason}")
    return response.json() if response.text else {}


def mark_waiting_record_from_response(response):
    if not isinstance(response, dict):
        return {}
    record = (response.get("result") or {}).get("record") if isinstance(response.get("result"), dict) else None
    if not isinstance(record, dict):
        record = response.get("record")
    return record if isinstance(record, dict) else {}


def privacy_safe_waiting_record(record):
    record = record if isinstance(record, dict) else {}
    marker = record.get("auto_inspect") if isinstance(record.get("auto_inspect"), dict) else {}
    return {
        key: value
        for key, value in {
            "review_id": record.get("review_id"),
            "persisted": bool(record.get("review_id")),
            "auto_inspect": marker,
            "candidate_locator_digest": record.get("candidate_locator_digest"),
        }.items()
        if value not in (None, "", {}, [])
    }


def transfer_waiting_record(transfer, username=""):
    if not isinstance(transfer, dict) or not transfer:
        return {}
    record = {
        "id": transfer.get("id"),
        "username": transfer.get("username") or username,
        "filename": transfer.get("filename") or transfer.get("remoteFilename"),
        "state": transfer.get("state"),
        "stateDescription": transfer.get("stateDescription"),
        "requestedAt": transfer.get("requestedAt"),
        "endedAt": transfer.get("endedAt"),
    }
    for field in ("bytesTransferred", "bytesRemaining", "percentComplete", "averageSpeed", "attempts", "size"):
        value = transfer.get(field)
        if value is not None:
            record[field] = value
    return {key: value for key, value in record.items() if value not in (None, "")}


def claim_autopilot_handoff(entry, candidate_key):
    """Atomically reserve an exact nonterminal queue row immediately before enqueue."""
    entry = entry if isinstance(entry, dict) else {}
    if not entry.get("autopilot_queue"):
        return {"allowed": True, "claimed": False, "reason": "not_autopilot_queue"}
    queue_id = str(entry.get("autopilot_queue_key") or entry.get("queue_key") or "").strip()
    series_key = normalize(entry.get("series") or entry.get("query"))
    issue_key = exact_single_issue_key(entry.get("issue"))
    identity = str(entry.get("queue_identity") or "").strip()
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"allowed": False, "claimed": False, "reason": "durable_queue_unavailable"}
    if not queue_id or not series_key or not issue_key or not identity:
        return {"allowed": False, "claimed": False, "reason": "durable_queue_identity_incomplete"}
    owner_id = hashlib.sha256(
        "|".join(("slskd_auto_grab_handoff", queue_id, str(entry.get("review_id") or ""), str(candidate_key or ""), str(uuid.uuid4()))).encode(
            "utf-8", errors="replace"
        )
    ).hexdigest()[:24]
    try:
        claim = inkdrop_state.claim_queue_item(
            INKDROP_STATE_DB,
            queue_id,
            owner_id,
            operation="slskd_auto_grab_handoff",
            lease_seconds=120,
            raw={"review_id_hash": hashlib.sha256(str(entry.get("review_id") or "").encode()).hexdigest()[:16]},
        )
    except Exception as exc:
        return {
            "allowed": False,
            "claimed": False,
            "reason": "durable_queue_claim_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not claim.get("acquired"):
        return {
            "allowed": False,
            "claimed": False,
            "reason": str(claim.get("reason") or "durable_queue_not_claimable"),
        }
    try:
        validation = validate_autopilot_handoff_claim(entry, {"queue_id": queue_id, "owner_id": owner_id, "claimed": True})
        if not validation.get("allowed"):
            inkdrop_state.release_queue_claim(INKDROP_STATE_DB, queue_id, owner_id)
            return {"allowed": False, "claimed": False, "reason": "durable_queue_identity_or_state_changed"}
        return {
            "allowed": True,
            "claimed": True,
            "reason": "durable_queue_claimed",
            "queue_id": queue_id,
            "owner_id": owner_id,
        }
    except Exception as exc:
        try:
            inkdrop_state.release_queue_claim(INKDROP_STATE_DB, queue_id, owner_id)
        except Exception:
            pass
        return {
            "allowed": False,
            "claimed": False,
            "reason": "durable_queue_identity_check_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


def exact_single_issue_key(value):
    """Return one canonical numeric unit, rejecting ranges and compound labels."""
    matches = re.findall(r"\d+(?:\.\d+)?", str(value or ""))
    if len(matches) != 1:
        return ""
    try:
        number = Decimal(matches[0])
    except InvalidOperation:
        return ""
    if not number.is_finite() or number < 0:
        return ""
    return format(number.normalize(), "f")


def validate_autopilot_handoff_claim(entry, claim):
    """Revalidate the exact queue row and exclusive owner while its claim is live."""
    entry = entry if isinstance(entry, dict) else {}
    claim = claim if isinstance(claim, dict) else {}
    queue_id = str(claim.get("queue_id") or "").strip()
    owner_id = str(claim.get("owner_id") or "").strip()
    series_key = normalize(entry.get("series") or entry.get("query"))
    issue_key = exact_single_issue_key(entry.get("issue"))
    identity = str(entry.get("queue_identity") or "").strip()
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"allowed": False, "reason": "durable_queue_unavailable"}
    if not queue_id or not owner_id or not series_key or not issue_key or not identity:
        return {"allowed": False, "reason": "durable_queue_identity_incomplete"}
    try:
        with inkdrop_state.connect_read(INKDROP_STATE_DB) as con:
            row = con.execute(
                "select active, state, series_id, raw_json from queue_items where id=? limit 1",
                (queue_id,),
            ).fetchone()
            owner = con.execute(
                "select owner_id, expires_at from queue_claims where queue_id=? limit 1",
                (queue_id,),
            ).fetchone()
        raw = json.loads(row["raw_json"] or "{}") if row else {}
        if not isinstance(raw, dict):
            return {"allowed": False, "reason": "durable_queue_evidence_malformed"}
        durable_series_key = normalize(raw.get("series") or raw.get("series_title"))
        durable_issue_key = exact_single_issue_key(raw.get("issue") or raw.get("issue_number") or raw.get("chapter"))
        durable_identity = str(raw.get("queue_identity") or "").strip()
        state = str(row["state"] or "").strip().lower() if row else ""
        allowed = bool(
            row
            and owner
            and str(owner["owner_id"] or "") == owner_id
            and float(owner["expires_at"] or 0) > time.time()
            and int(row["active"] or 0)
            and state not in {"verified", "satisfied", "superseded_duplicate", "removed", "ignored", "inactive"}
            and durable_series_key == series_key
            and durable_issue_key == issue_key
            and durable_identity == identity
        )
        return {"allowed": allowed, "reason": "durable_queue_claim_valid" if allowed else "durable_queue_identity_or_state_changed"}
    except Exception as exc:
        return {"allowed": False, "reason": "durable_queue_identity_check_failed", "error": f"{type(exc).__name__}: {exc}"}


def release_autopilot_handoff_claim(claim):
    claim = claim if isinstance(claim, dict) else {}
    if not claim.get("claimed") or inkdrop_state is None:
        return False
    try:
        return bool(inkdrop_state.release_queue_claim(INKDROP_STATE_DB, claim.get("queue_id"), claim.get("owner_id")))
    except Exception as exc:
        log("slskd_auto_grab_claim_release_failed", queue_id=claim.get("queue_id"), error=f"{type(exc).__name__}: {exc}")
        return False


def update_autopilot_queue_from_waiting_record(record):
    if not isinstance(record, dict) or not record.get("autopilot_queue"):
        return {"updated": False, "reason": "not_autopilot_queue"}
    queue = read_json(SERIES_AUTOPILOT_QUEUE_FILE, {}) or {}
    items = queue.get("items") if isinstance(queue, dict) else {}
    if not isinstance(items, dict):
        return {"updated": False, "reason": "queue_missing"}
    queue_key = str(record.get("autopilot_queue_key") or "").strip()
    item = items.get(queue_key) if queue_key else None
    if not isinstance(item, dict):
        record_review_id = str(record.get("review_id") or "").strip()
        record_series = normalize(record.get("series"))
        record_issue_keys = issue_number_keys(record.get("issue"))
        record_identity = str(record.get("queue_identity") or "").strip()
        for key, candidate in items.items():
            if not isinstance(candidate, dict):
                continue
            if record_review_id and str(candidate.get("review_id") or "") == record_review_id:
                queue_key = key
                item = candidate
                break
            same_identity = not record_identity or str(candidate.get("queue_identity") or "") == record_identity
            same_series = normalize(candidate.get("series")) == record_series
            same_issue = bool(issue_number_keys(candidate.get("issue")) & record_issue_keys)
            if same_identity and same_series and same_issue:
                queue_key = key
                item = candidate
                break
    if not isinstance(item, dict):
        return {"updated": False, "reason": "queue_row_not_found", "queue_key": queue_key}
    if str(item.get("state") or "") == "verified":
        return {"updated": False, "reason": "already_verified", "queue_key": queue_key}

    transfer = record.get("slskd_transfer") if isinstance(record.get("slskd_transfer"), dict) else {}
    now_ts = now()
    item["state"] = "downloading"
    item["current_source"] = "slskd"
    item["last_event"] = "SLSKD started best candidate"
    item["download_started_at"] = now_ts
    item["download_started_at_iso"] = utc_stamp(now_ts)
    item["last_download_started_at"] = now_ts
    item["last_download_started_at_iso"] = utc_stamp(now_ts)
    item["last_slskd_waiting_review_id"] = record.get("review_id")
    item["last_slskd_autopick_status"] = "started_waiting"
    item["last_slskd_autoresolve_status"] = "waiting_for_transfer"
    item["last_slskd_autoresolve_at"] = now_ts
    item["last_slskd_autoresolve_at_iso"] = utc_stamp(now_ts)
    item["last_slskd_candidate"] = record.get("filename") or item.get("last_slskd_candidate")
    item["last_slskd_user"] = record.get("username") or item.get("last_slskd_user")
    item["last_slskd_score"] = record.get("candidate_score") or item.get("last_slskd_score")
    item["last_slskd_transfer_id"] = record.get("slskd_transfer_id") or transfer.get("id") or item.get("last_slskd_transfer_id")
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
        value = transfer.get(source_key)
        if value is not None:
            item[target_key] = value
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)
    item["updated_at"] = now_ts
    item["updated_at_iso"] = utc_stamp(now_ts)
    db_patch = {}
    if inkdrop_state is not None:
        try:
            db_patch = inkdrop_state.patch_queue_item_state(
                INKDROP_STATE_DB,
                queue_key,
                state="downloading",
                current_source="slskd",
                last_event="SLSKD started best candidate",
                raw_updates=item,
                raw_clear_keys=("retry_after", "retry_after_iso", "needs_you_reason"),
                source="slskd",
                event_type="slskd_waiting_record",
                history_message="SLSKD started best candidate",
                updated_at=now_ts,
            )
        except Exception as exc:
            db_patch = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            log("inkdrop_state_queue_patch_failed", reason="slskd_waiting_record", queue_key=queue_key, error=db_patch["error"])
    if db_patch.get("ok"):
        sync_result = {"ok": True, "db_authoritative": True, "patch": db_patch}
    else:
        write_json(SERIES_AUTOPILOT_QUEUE_FILE, queue)
        sync_result = sync_inkdrop_queue_state(reason="slskd_waiting_record")
    attempt_result = record_slskd_queue_attempt(
        record,
        {
            "filename": record.get("filename"),
            "username": record.get("username"),
            "score": record.get("candidate_score"),
            "size": record.get("candidate_size"),
        },
        "started_waiting",
        "SLSKD started best candidate",
        transfer=transfer,
        extra={"review_id": record.get("review_id"), "waiting_record": True},
    )
    export_result = export_autopilot_queue_from_inkdrop_state("slskd_waiting_record", queue_key) if db_patch.get("ok") else {}
    return {
        "updated": True,
        "queue_key": queue_key,
        "state": item.get("state"),
        "current_source": item.get("current_source"),
        "sync": sync_result,
        "attempt": attempt_result,
        "export": export_result,
    }


def staged_attempt_id(entry, detected):
    queue_key = str((entry or {}).get("autopilot_queue_key") or "").strip()
    review_id = str((entry or {}).get("review_id") or "").strip()
    path = str((detected or {}).get("path") or (detected or {}).get("filename") or "").strip()
    mtime = str((detected or {}).get("mtime") or "").strip()
    digest = hashlib.sha256("|".join([queue_key, review_id, path, mtime]).encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"slskd-staged-{digest}"


def update_autopilot_queue_from_staged_entry(entry, detected=None):
    entry = entry if isinstance(entry, dict) else {}
    detected = detected if isinstance(detected, dict) else {}
    if not entry.get("autopilot_queue"):
        return {"updated": False, "reason": "not_autopilot_queue"}
    queue = read_json(SERIES_AUTOPILOT_QUEUE_FILE, {}) or {}
    items = queue.get("items") if isinstance(queue, dict) else {}
    if not isinstance(items, dict):
        return {"updated": False, "reason": "queue_missing"}
    queue_key = str(entry.get("autopilot_queue_key") or "").strip()
    item = items.get(queue_key) if queue_key else None
    if not isinstance(item, dict):
        entry_review_id = str(entry.get("review_id") or "").strip()
        entry_series = normalize(entry.get("series") or entry.get("query"))
        entry_issue_keys = issue_number_keys(entry.get("issue"))
        entry_identity = str(entry.get("queue_identity") or "").strip()
        for key, candidate in items.items():
            if not isinstance(candidate, dict):
                continue
            if entry_review_id and str(candidate.get("review_id") or "") == entry_review_id:
                queue_key = key
                item = candidate
                break
            same_identity = not entry_identity or str(candidate.get("queue_identity") or "") == entry_identity
            same_series = normalize(candidate.get("series")) == entry_series
            same_issue = bool(issue_number_keys(candidate.get("issue")) & entry_issue_keys)
            if same_identity and same_series and same_issue:
                queue_key = key
                item = candidate
                break
    if not isinstance(item, dict):
        return {"updated": False, "reason": "queue_row_not_found", "queue_key": queue_key}
    if str(item.get("state") or "") == "verified":
        return {"updated": False, "reason": "already_verified", "queue_key": queue_key}
    detected_path = str(detected.get("path") or "").strip()
    if not detected_path:
        return {"updated": False, "reason": "staged_file_path_missing", "queue_key": queue_key}

    now_ts = now()
    detected_files = [row for row in entry.get("detected_files") or [] if isinstance(row, dict)]
    item["state"] = "importing"
    item["current_source"] = "slskd"
    item["last_event"] = "SLSKD staged file detected; waiting for verified import"
    item["last_slskd_status"] = "staged_file_ready"
    item["last_slskd_detected_count"] = int(entry.get("detected_count") or len(detected_files) or 1)
    item["last_slskd_candidate_count"] = int(entry.get("candidate_count") or 0)
    item["last_slskd_failed_candidate_count"] = int(entry.get("failed_candidate_count") or 0)
    item["last_slskd_auto_grab_safe_count"] = int(entry.get("auto_grab_safe_count") or 0)
    item["last_slskd_auto_grab_review_count"] = int(entry.get("auto_grab_review_count") or 0)
    item["last_slskd_auto_grab_blocked_count"] = int(entry.get("auto_grab_blocked_count") or 0)
    item["last_slskd_at"] = entry.get("staged_scan_at") or entry.get("checked_at") or now_ts
    item["last_slskd_at_iso"] = entry.get("staged_scan_at_iso") or entry.get("checked_at_iso") or utc_stamp(item["last_slskd_at"])
    item["last_slskd_waiting_review_id"] = entry.get("review_id") or item.get("last_slskd_waiting_review_id")
    item["last_slskd_autopick_status"] = "staged_file_ready"
    item["last_slskd_autoresolve_status"] = "staged_file_ready"
    item["last_slskd_autoresolve_reason"] = "SLSKD staged file detected; waiting for import"
    item["last_slskd_autoresolve_at"] = now_ts
    item["last_slskd_autoresolve_at_iso"] = utc_stamp(now_ts)
    item["last_slskd_candidate"] = detected.get("filename") or detected_path or item.get("last_slskd_candidate")
    item["last_slskd_detected_filename"] = detected.get("filename") or item.get("last_slskd_detected_filename")
    item["last_slskd_detected_path"] = detected_path or item.get("last_slskd_detected_path")
    item["last_slskd_detected_size"] = detected.get("size") or item.get("last_slskd_detected_size")
    if detected_files:
        item["last_slskd_detected_files"] = detected_files[:5]
    item.pop("retry_after", None)
    item.pop("retry_after_iso", None)
    item.pop("needs_you_reason", None)
    item["updated_at"] = now_ts
    item["updated_at_iso"] = utc_stamp(now_ts)
    db_patch = {}
    if inkdrop_state is not None:
        try:
            db_patch = inkdrop_state.patch_queue_item_state(
                INKDROP_STATE_DB,
                queue_key,
                state="importing",
                current_source="slskd",
                last_event="SLSKD staged file detected; waiting for verified import",
                raw_updates=item,
                raw_clear_keys=("retry_after", "retry_after_iso", "needs_you_reason"),
                source="slskd",
                event_type="slskd_staged_file_ready",
                history_message="SLSKD staged file detected; waiting for verified import",
                updated_at=now_ts,
            )
        except Exception as exc:
            db_patch = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            log("inkdrop_state_queue_patch_failed", reason="slskd_staged_file_ready", queue_key=queue_key, error=db_patch["error"])
    if db_patch.get("ok"):
        sync_result = {"ok": True, "db_authoritative": True, "patch": db_patch}
    else:
        write_json(SERIES_AUTOPILOT_QUEUE_FILE, queue)
        sync_result = sync_inkdrop_queue_state(reason="slskd_staged_file_ready")
    attempt_result = record_slskd_queue_attempt(
        entry,
        {
            "filename": detected.get("filename") or detected_path,
            "path": detected_path,
            "score": detected.get("score"),
            "size": detected.get("size"),
        },
        "staged_file_ready",
        "SLSKD staged file detected; waiting for verified import",
        extra={
            "review_id": entry.get("review_id"),
            "detected_file": True,
            # This is the local completed-download path, not the private peer
            # locator. The importer needs it to claim the exact staged artifact.
            "local_path": detected_path,
            "detected_path": detected.get("path"),
            "detected_filename": detected.get("filename"),
            "detected_count": item.get("last_slskd_detected_count"),
            "last_slskd_status": "staged_file_ready",
            "last_slskd_detected_count": item.get("last_slskd_detected_count"),
        },
        attempt_id=staged_attempt_id(entry, detected),
    )
    export_result = export_autopilot_queue_from_inkdrop_state("slskd_staged_file_ready", queue_key) if db_patch.get("ok") else {}
    log(
        "slskd_staged_queue_update",
        review_id=entry.get("review_id"),
        series=entry.get("series"),
        issue=entry.get("issue"),
        queue_key=queue_key,
        path=detected.get("path"),
        sync=sync_result,
        attempt=attempt_result,
        export=export_result,
    )
    return {
        "updated": True,
        "queue_key": queue_key,
        "state": item.get("state"),
        "current_source": item.get("current_source"),
        "sync": sync_result,
        "attempt": attempt_result,
        "export": export_result,
    }


def fallback_manual_source_waiting_record(entry, candidate, transfer, validation=None, api_error=""):
    validation = validation if isinstance(validation, dict) else {}
    record = (validation.get("result") or {}).get("record") or validation.get("record") or {}
    record = dict(record) if isinstance(record, dict) else {}
    review_id = str((entry or {}).get("review_id") or record.get("review_id") or "").strip()
    if not review_id:
        raise RuntimeError("cannot fallback mark-waiting without review_id")
    record["review_id"] = review_id
    record["series"] = record.get("series") or (entry or {}).get("series") or (entry or {}).get("query")
    record["issue"] = record.get("issue") or (entry or {}).get("issue")
    record["query"] = record.get("query") or auto_grab_waiting_query(entry)
    record["filename"] = record.get("filename") or (candidate or {}).get("filename")
    record["filename_leaf"] = record.get("filename_leaf") or filename_leaf(record.get("filename"))
    record["username"] = record.get("username") or (candidate or {}).get("username")
    for key in ITEM_CONTEXT_FIELDS:
        value = record.get(key)
        if value in (None, ""):
            value = (entry or {}).get(key)
        if value not in (None, ""):
            record[key] = value
    record["candidate_source"] = record.get("candidate_source") or "slskd_probe"
    record["candidate_score"] = record.get("candidate_score") or (candidate or {}).get("score")
    record["candidate_size"] = record.get("candidate_size") or (candidate or {}).get("size")
    auto_grab = (candidate or {}).get("auto_grab") if isinstance((candidate or {}).get("auto_grab"), dict) else {}
    if auto_grab and not isinstance(record.get("candidate_auto_grab"), dict):
        record["candidate_auto_grab"] = {
            key: auto_grab.get(key)
            for key in (
                "verdict",
                "extension",
                "score",
                "match_score",
                "direct_match_confidence",
                "is_pack_candidate",
                "is_archive_pack_candidate",
                "is_archive_exact_issue_candidate",
                "autopick_eligible",
                "auto_inspect_eligible",
                "inspection_message",
                "preferred_size_bytes",
                "inspection_hard_min_bytes",
                "policy_version",
            )
            if auto_grab.get(key) not in (None, "")
        }
    if auto_grab.get("auto_inspect_eligible"):
        identity_hash = slskd_private_locator_digest(candidate, entry)
        if not identity_hash:
            raise RuntimeError("inspection handoff requires an exact candidate identity")
        record["auto_inspect"] = {
            "contract_version": 1,
            "outcome": "auto_inspect",
            "candidate_identity_hash": identity_hash,
            "exact_artifact_proof_required": True,
            "neutral_missing_evidence": ["size_below_preferred"],
            "preferred_size_bytes": int(auto_grab.get("preferred_size_bytes") or SLSKD_PREFERRED_EXACT_MIN_BYTES),
        }
        record["candidate_locator_digest"] = identity_hash
    transfer_record = transfer_waiting_record(transfer, username=record.get("username"))
    if transfer_record:
        record["slskd_transfer"] = transfer_record
        record["slskd_transfer_id"] = transfer_record.get("id")
        record["slskd_transfer_state"] = transfer_record.get("state") or transfer_record.get("stateDescription")
        record["slskd_transfer_requested_at"] = transfer_record.get("requestedAt")
    record["ts"] = now()
    record["ts_iso"] = utc_stamp(record["ts"])
    record["mark_waiting_fallback"] = True
    if api_error:
        record["mark_waiting_api_error"] = (
            "SLSKD waiting-record request failed" if record.get("auto_inspect") else str(api_error)[-500:]
        )
    return record


def mark_manual_source_waiting_local(entry, candidate, dry_run=False, transfer=None, validation=None, api_error="", fallback=False):
    record = fallback_manual_source_waiting_record(
        entry,
        candidate,
        transfer if isinstance(transfer, dict) else {},
        validation=validation,
        api_error=api_error,
    )
    if fallback:
        record["mark_waiting_fallback"] = True
    else:
        record.pop("mark_waiting_fallback", None)
        record["mark_waiting_local"] = True
    auto_inspect = bool(record.get("auto_inspect"))
    response_record = privacy_safe_waiting_record(record) if auto_inspect else record
    safe_api_error = "SLSKD waiting-record request failed" if auto_inspect and api_error else str(api_error)[-500:]
    if dry_run:
        return {
            "ok": True,
            "result": {
                "dry_run": True,
                "record": response_record,
                "local": True,
                "fallback": bool(fallback),
            },
        }

    actions = load_actions()
    waiting = actions.setdefault("manual_source_waiting", {})
    if not isinstance(waiting, dict):
        waiting = {}
        actions["manual_source_waiting"] = waiting
    waiting[str(record["review_id"])] = record
    save_actions(actions)
    queue_update = update_autopilot_queue_from_waiting_record(record)
    log(
        "manual_source_waiting_local",
        review_id=record.get("review_id"),
        series=None if auto_inspect else record.get("series"),
        issue=record.get("issue"),
        filename=None if auto_inspect else record.get("filename"),
        username=None if auto_inspect else record.get("username"),
        transfer_id=record.get("slskd_transfer_id"),
        queue_update=None if auto_inspect else queue_update,
        fallback=bool(fallback),
        api_error=safe_api_error,
    )
    return {
        "ok": True,
        "result": {
            "dry_run": False,
            "record": response_record,
            "queue_update": queue_update,
            "local": True,
            "fallback": bool(fallback),
            "api_error": safe_api_error,
        },
    }


def mark_manual_source_waiting_fallback(entry, candidate, transfer, validation=None, api_error=""):
    return mark_manual_source_waiting_local(
        entry,
        candidate,
        dry_run=False,
        transfer=transfer,
        validation=validation,
        api_error=api_error,
        fallback=True,
    )


def mark_manual_source_waiting_api_with_retry(entry, candidate, transfer=None, validation=None, attempts=4, delay_seconds=2.0):
    errors = []
    for attempt in range(1, max(1, int(attempts or 1)) + 1):
        try:
            response = mark_manual_source_waiting_api(entry, candidate, dry_run=False, transfer=transfer)
            record = mark_waiting_record_from_response(response)
            if record.get("review_id"):
                return response
            errors.append("mark-waiting returned no persisted waiting record")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        if attempt >= max(1, int(attempts or 1)):
            break
        time.sleep(max(0.1, float(delay_seconds or 0)))
    if isinstance(transfer, dict) and transfer:
        return mark_manual_source_waiting_fallback(
            entry,
            candidate,
            transfer,
            validation=validation,
            api_error="mark-waiting failed after transfer enqueue: " + errors[-1],
        )
    raise RuntimeError("mark-waiting failed after transfer enqueue: " + errors[-1])


def auto_grab_transfer_from_enqueue(enqueue, candidate, *, strict_path=False):
    result = {
        "transfer": {},
        "match_status": "no_rows",
        "ambiguous": False,
        "reason": "SLSKD enqueue response did not include transfer rows",
        "row_count": 0,
        "candidate_rows": [],
    }
    if not isinstance(enqueue, dict):
        result["match_status"] = "invalid_response"
        result["reason"] = "SLSKD enqueue response was not a JSON object"
        return result
    rows = auto_grab_enqueue_transfer_rows(enqueue)
    result["row_count"] = len(rows)
    result["candidate_rows"] = compact_enqueue_transfer_rows(rows)
    if enqueue.get("dry_run"):
        result["match_status"] = "dry_run"
        result["reason"] = "dry run does not create an SLSKD transfer"
        return result
    if not rows:
        return result
    matches = [row for row in rows if slskd_transfer_matches_candidate(row, candidate, strict_path=strict_path)]
    if len(matches) == 1:
        result["transfer"] = matches[0]
        result["match_status"] = "matched"
        result["reason"] = "SLSKD enqueue transfer matched candidate username and filename"
        return result
    result["ambiguous"] = True
    result["match_status"] = "ambiguous"
    if matches:
        result["reason"] = "SLSKD enqueue returned multiple transfer rows matching the candidate"
    else:
        result["reason"] = (
            "SLSKD enqueue returned transfer rows, but none matched the candidate username "
            "and filename/leaf"
        )
    return result


def auto_grab_enqueue_transfer_rows(enqueue):
    if not isinstance(enqueue, dict):
        return []
    rows = []
    for key in ("enqueued", "files", "transfers"):
        value = enqueue.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    return rows


def slskd_transfer_matches_candidate(row, candidate, *, strict_path=False):
    if not isinstance(row, dict) or not isinstance(candidate, dict):
        return False
    expected_user = str((candidate or {}).get("username") or "").strip().lower()
    expected_file = str((candidate or {}).get("filename") or "").replace("\\", "/").lower()
    expected_leaf = filename_leaf(expected_file).lower()
    if not expected_user or not expected_leaf:
        return False
    username = str(row.get("username") or "").strip().lower()
    filename = str(row.get("filename") or row.get("remoteFilename") or "").replace("\\", "/").lower()
    if not username or username != expected_user:
        return False
    if strict_path:
        try:
            expected_size = int(float(candidate.get("size") or candidate.get("size_bytes") or 0))
            actual_size = int(float(row.get("size") or row.get("size_bytes") or 0))
        except (TypeError, ValueError):
            return False
        return bool(expected_size > 0 and actual_size == expected_size and filename == expected_file)
    return bool(filename and (filename in {expected_file, expected_leaf} or filename_leaf(filename).lower() == expected_leaf))


def compact_enqueue_transfer_rows(rows, limit=5):
    compacted = []
    for row in rows[: max(0, int(limit or 0))]:
        if not isinstance(row, dict):
            continue
        compacted.append({
            key: row.get(key)
            for key in (
                "id",
                "username",
                "filename",
                "remoteFilename",
                "state",
                "stateDescription",
                "requestedAt",
                "size",
                "bytesTransferred",
                "percentComplete",
            )
            if row.get(key) not in (None, "")
        })
    return compacted


def slskd_enqueue_candidate(candidate, dry_run):
    username = str(candidate.get("username") or "").strip()
    filename = str(candidate.get("filename") or "").strip()
    try:
        size = int(candidate.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if not username:
        raise RuntimeError("candidate has no SLSKD username")
    if not filename:
        raise RuntimeError("candidate has no filename")
    files = [{"filename": filename, "size": size}]
    if dry_run:
        return {"dry_run": True, "endpoint": f"/transfers/downloads/{username}", "files": files}
    return slskd_post(f"/transfers/downloads/{quote(username, safe='')}", files, timeout=30)


def slskd_delete_download_transfer(transfer, dry_run):
    transfer = transfer or {}
    username = str(transfer.get("username") or "").strip()
    transfer_id = str(transfer.get("id") or "").strip()
    if not username or not transfer_id:
        raise RuntimeError("cannot delete SLSKD transfer without username and id")
    endpoint = f"/transfers/downloads/{quote(username, safe='')}/{quote(transfer_id, safe='')}"
    if dry_run:
        return {"dry_run": True, "endpoint": endpoint}
    response = slskd_curl_request("DELETE", endpoint, timeout=15)
    return response if response is not None else {"deleted": True, "endpoint": endpoint}


def ranked_auto_grab_candidates(entry):
    def candidate_score(candidate):
        try:
            return int(candidate.get("score") or 0)
        except (TypeError, ValueError):
            return 0

    def candidate_rank(candidate):
        try:
            return int((candidate.get("auto_grab") or {}).get("autopick_rank") or 999999)
        except (TypeError, ValueError):
            return 999999

    candidates = [
        candidate for candidate in (entry or {}).get("candidates") or []
        if isinstance(candidate, dict)
        and (candidate.get("auto_grab") or {}).get("verdict") == "auto_grab_safe"
        and not (candidate.get("auto_grab") or {}).get("blockers")
    ]
    candidates.sort(key=lambda candidate: (
        candidate_rank(candidate),
        -candidate_score(candidate),
    ))
    return candidates


def ranked_auto_inspect_candidates(entry):
    candidates = [
        candidate for candidate in (entry or {}).get("candidates") or []
        if isinstance(candidate, dict)
        and (candidate.get("auto_grab") or {}).get("auto_inspect_eligible") is True
        and not (candidate.get("auto_grab") or {}).get("blockers")
    ]
    candidates.sort(key=lambda candidate: -int(candidate.get("score") or 0))
    return candidates


def ranked_retry_fallback_candidates(entry, reason):
    def candidate_score(candidate):
        try:
            return int(candidate.get("score") or 0)
        except (TypeError, ValueError):
            return 0

    def candidate_rank(candidate):
        try:
            return int((candidate.get("auto_grab") or {}).get("autopick_rank") or 999999)
        except (TypeError, ValueError):
            return 999999

    candidates = []
    for candidate in (entry or {}).get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if (candidate.get("auto_grab") or {}).get("verdict") == "auto_grab_safe":
            continue
        if not retry_candidate_after_failure_eligible(candidate):
            continue
        promoted = dict(candidate)
        gate = dict(promoted.get("auto_grab") or {})
        gate["verdict"] = "auto_grab_safe"
        gate["autopick_eligible"] = True
        gate["retry_after_attempt_limit"] = True
        reasons = list(gate.get("reasons") or [])
        reasons.append(reason)
        reasons.append("retry fallback requires direct title and issue evidence")
        gate["reasons"] = list(dict.fromkeys(str(value) for value in reasons if value))
        gate["review_reasons"] = [
            review_reason
            for review_reason in list(gate.get("review_reasons") or [])
            if review_reason != "lower-ranked autopick candidate"
            and not str(review_reason).startswith("score ")
            and not str(review_reason).startswith("best candidate")
        ]
        promoted["auto_grab"] = gate
        candidates.append(promoted)
    candidates.sort(key=lambda candidate: (
        candidate_rank(candidate),
        -candidate_score(candidate),
    ))
    return candidates


def auto_grab_attempt_candidates(entry, first_candidate):
    """Ordered candidates to try for one row during this auto-grab pass."""
    seen_candidate_keys = set()
    candidates = []
    retry_reason = "previous SLSKD candidate failed during auto-grab; retrying next best exact-match candidate"
    for candidate in [
        first_candidate,
        *ranked_auto_grab_candidates(entry),
        *ranked_auto_inspect_candidates(entry),
        *ranked_retry_fallback_candidates(entry, retry_reason),
    ]:
        candidate_key = auto_grab_candidate_key(entry.get("review_id"), candidate)
        if candidate_key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(candidate_key)
        candidates.append(candidate)
    return candidates


def auto_grab_review_rows(result, state=None):
    rows = []
    skipped_attempt_limits = []
    skipped_bad_candidates = []
    skipped_bad_keys = set()
    state = state or {}
    waiting = waiting_review_ids()
    items = result.get("items") if isinstance(result, dict) else {}

    def record_bad_candidate(review_id, entry, candidate, bad_match):
        candidate_key = auto_grab_candidate_key(str(review_id), candidate)
        if candidate_key in skipped_bad_keys:
            return
        skipped_bad_keys.add(candidate_key)
        failure_reason = bad_match.get("reason") or "previous candidate failure"
        skipped_bad_candidates.append({
            "review_id": str(review_id),
            "series": entry.get("series"),
            "issue": entry.get("issue"),
            "filename": candidate.get("filename"),
            "username": candidate.get("username"),
            "score": candidate.get("score"),
            "reason": failure_reason,
            "failure_label": bad_match.get("failure_label") or failure_reason,
            "detail": bad_match.get("detail") or "",
            "failure_kind": bad_match.get("failure_kind"),
            "failed_at_iso": bad_match.get("ts_iso"),
        })

    for review_id, entry in (items or {}).items():
        entry = dict(entry or {})
        entry["candidates"] = annotate_bad_candidate_verdicts(entry.get("candidates") or [], str(review_id))
        if str(review_id) in waiting:
            continue
        if entry_has_unfailed_detected_file(review_id, entry):
            continue
        for candidate in entry.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            bad_match = bad_candidate_match(str(review_id), candidate)
            if bad_match:
                record_bad_candidate(review_id, entry, candidate, bad_match)
        safe_candidates = [
            *ranked_auto_grab_candidates(entry),
            *ranked_auto_inspect_candidates(entry),
        ]
        entry_selected = False
        retry_fallback_reason = ""
        tried_candidate_keys = set()
        for candidate in safe_candidates:
            bad_match = bad_candidate_match(str(review_id), candidate)
            if bad_match:
                record_bad_candidate(review_id, entry, candidate, bad_match)
                retry_fallback_reason = (
                    "top SLSKD candidate failed previously; retrying next best exact-match candidate"
                )
                continue
            allowed, reason, candidate_key = auto_grab_attempt_allowed(state, str(review_id), candidate)
            tried_candidate_keys.add(candidate_key)
            if allowed:
                rows.append((str(review_id), entry, candidate))
                entry_selected = True
                break
            skipped_attempt_limits.append({
                "review_id": str(review_id),
                "series": entry.get("series"),
                "issue": entry.get("issue"),
                "filename": candidate.get("filename"),
                "username": candidate.get("username"),
                "score": candidate.get("score"),
                "candidate_key": candidate_key,
                "reason": reason,
            })
            if str(reason or "").startswith("candidate attempt limit"):
                retry_fallback_reason = "top SLSKD candidate already attempted; retrying next best exact-match candidate"
            elif str(reason or "").startswith("row attempt limit"):
                retry_fallback_reason = ""
                break
        if not entry_selected and retry_fallback_reason:
            for candidate in ranked_retry_fallback_candidates(entry, retry_fallback_reason):
                bad_match = bad_candidate_match(str(review_id), candidate)
                if bad_match:
                    record_bad_candidate(review_id, entry, candidate, bad_match)
                    continue
                allowed, reason, candidate_key = auto_grab_attempt_allowed(state, str(review_id), candidate)
                if candidate_key in tried_candidate_keys:
                    continue
                tried_candidate_keys.add(candidate_key)
                if allowed:
                    rows.append((str(review_id), entry, candidate))
                    entry_selected = True
                    break
                skipped_attempt_limits.append({
                    "review_id": str(review_id),
                    "series": entry.get("series"),
                    "issue": entry.get("issue"),
                    "filename": candidate.get("filename"),
                    "username": candidate.get("username"),
                    "score": candidate.get("score"),
                    "candidate_key": candidate_key,
                    "reason": reason,
                })
                if str(reason or "").startswith("row attempt limit"):
                    break
    rows.sort(key=lambda row: (
        normalize(row[1].get("series") or ""),
        token_number(row[1].get("issue")) or 999999,
        str(row[1].get("issue") or ""),
    ))
    return rows, skipped_attempt_limits, skipped_bad_candidates


def active_auto_grab_user_load():
    counts = {}
    for row in slskd_download_rows():
        if not isinstance(row, dict):
            continue
        username = normalize(row.get("username") or "")
        if not username:
            continue
        state = slskd_transfer_state_text(row)
        if slskd_transfer_failed(row):
            continue
        if "succeeded" in state or ("completed" in state and "fail" not in state and "error" not in state):
            continue
        if any(token in state for token in ("requested", "queued", "progress", "initial", "remotely", "locally")):
            counts[username] = counts.get(username, 0) + 1
    return counts


def select_auto_grab_rows(rows, max_grabs):
    if max_grabs <= 0:
        return [], []
    user_load = active_auto_grab_user_load()
    selected = []
    skipped_user_load = []
    selected_by_user = {}
    for review_id, entry, candidate in rows or []:
        hydrated_candidate, hydration_available = hydrate_series_handoff_candidate(candidate, review_id=review_id)
        if (candidate or {}).get("series_directory_handoff_token") and not hydration_available:
            skipped_user_load.append(copy_item_context({
                "review_id": str(review_id),
                "series": (entry or {}).get("series"),
                "issue": (entry or {}).get("issue"),
                "filename": filename_leaf((candidate or {}).get("filename")),
                "score": (candidate or {}).get("score"),
                "reason": "fresh in-memory SLSKD handoff routing is unavailable; re-probing before enqueue",
            }, entry or {}))
            continue
        username = normalize((hydrated_candidate or {}).get("username") or "")
        active_count = user_load.get(username, 0) if username else 0
        selected_count = selected_by_user.get(username, 0) if username else 0
        if username and active_count + selected_count >= AUTO_GRAB_MAX_ACTIVE_PER_USER:
            slot_wait = copy_item_context({
                "review_id": str(review_id),
                "series": (entry or {}).get("series"),
                "issue": (entry or {}).get("issue"),
                "filename": filename_leaf((candidate or {}).get("filename")),
                "score": (candidate or {}).get("score"),
                "active_user_transfer_count": active_count,
                "selected_user_transfer_count": selected_count,
                "limit": AUTO_GRAB_MAX_ACTIVE_PER_USER,
                "reason": f"SLSKD user already has {active_count + selected_count} active/queued transfer(s)",
            }, entry or {})
            slot_wait["_slot_entry"] = dict(entry or {})
            slot_candidate = dict(hydrated_candidate or candidate or {})
            slot_candidate.setdefault("auto_grab", (candidate or {}).get("auto_grab") or {})
            slot_wait["_slot_candidate"] = slot_candidate
            skipped_user_load.append(slot_wait)
            continue
        selected.append((review_id, entry, candidate))
        if username:
            selected_by_user[username] = selected_count + 1
        if len(selected) >= max_grabs:
            break
    return selected, skipped_user_load


def _run_auto_grab_with_ephemeral_candidates(args, result):
    live = bool(args.auto_grab_live)
    dry_run = not live
    max_grabs = max(0, min(int(args.auto_grab_max or 0), 10))
    transfer_identity_reconciliation = (
        reconcile_slskd_transfer_identity_tasks() if live else
        {"ok": True, "reason": "dry_run", "recovered": 0, "retired": 0}
    )
    state = load_auto_grab_state()
    base_state = json.loads(json.dumps(state))
    rows, skipped_attempt_limits, skipped_bad_candidates = auto_grab_review_rows(result, state=state)
    selected, skipped_user_load = select_auto_grab_rows(rows, max_grabs)
    for skipped in skipped_user_load:
        slot_entry = skipped.pop("_slot_entry", None)
        slot_candidate = skipped.pop("_slot_candidate", None)
        if not isinstance(slot_entry, dict) or not isinstance(slot_candidate, dict):
            continue
        if live:
            slot_result = decide_automatic_slskd_handoff(
                slot_entry,
                slot_candidate,
                skipped.get("reason") or "",
                acquire_claim=False,
            )
        else:
            slot_result = {
                "ok": True,
                "created": False,
                "reason": "slot_request_dry_run",
                "status": "waiting_for_slot",
            }
        safe_slot_result = {
            key: slot_result.get(key)
            for key in (
                "ok", "created", "expired", "idempotent", "decision", "reason", "status",
                "download_task_id", "slot_request_id", "slot_request_created_at",
                "slot_request_retry_at", "slot_request_deadline",
            )
            if slot_result.get(key) not in (None, "")
        }
        skipped["slot_request"] = safe_slot_result
        skipped["status"] = str(slot_result.get("status") or (
            "waiting_for_slot" if slot_result.get("ok") else "slot_request_failed"
        ))
        if slot_result.get("slot_request_retry_at") is not None:
            skipped["slot_request_retry_at"] = slot_result.get("slot_request_retry_at")
        if not slot_result.get("ok"):
            skipped["reason"] = "SLSKD transfer slot wait could not be saved; automatic retry scheduled"
    outcome = {
        "enabled": bool(args.auto_grab_live or args.auto_grab_dry_run),
        "transfer_identity_reconciliation": transfer_identity_reconciliation,
        "live": live,
        "dry_run": dry_run,
        "candidate_count": len(rows),
        "attempt_limit_skipped_count": len(skipped_attempt_limits),
        "attempt_limit_skipped": skipped_attempt_limits[:100],
        "bad_candidate_skipped_count": len(skipped_bad_candidates),
        "bad_candidate_skipped": skipped_bad_candidates[:100],
        "user_load_skipped_count": len(skipped_user_load),
        "user_load_skipped": skipped_user_load[:100],
        "unsafe_candidate_skipped_count": 0,
        "unsafe_candidate_skipped": [],
        "selected_count": len(selected),
        "started_count": 0,
        "failed_attempt_consumed_count": 0,
        "transient_error_count": 0,
        "transient_errors": [],
        "rows": [],
    }
    state_dirty = False
    for skipped in skipped_attempt_limits[:100]:
        auto_grab_audit("attempt_limit_skipped", live=live, dry_run=dry_run, **skipped)
    for skipped in skipped_bad_candidates[:100]:
        auto_grab_audit("bad_candidate_skipped", live=live, dry_run=dry_run, **skipped)
    for skipped in skipped_user_load[:100]:
        auto_grab_audit("user_load_skipped", live=live, dry_run=dry_run, **skipped)
    for review_id, entry, first_candidate in selected:
        entry = dict(entry or {})
        entry.setdefault("review_id", review_id)
        candidates = auto_grab_attempt_candidates(entry, first_candidate)
        for candidate in candidates:
            transfer_candidate, handoff_routing_available = hydrate_series_handoff_candidate(
                candidate,
                review_id=review_id,
            )
            privacy_handoff = bool(candidate.get("series_directory_handoff_token"))
            allowed, attempt_reason, candidate_key = auto_grab_attempt_allowed(state, review_id, candidate)
            tried_enqueue = False
            gate = candidate.get("auto_grab") or {}
            inspection_handoff = bool(gate.get("auto_inspect_eligible"))
            if inspection_handoff:
                hydrated_gate_candidate = dict(transfer_candidate or {})
                hydrated_gate_candidate["auto_grab"] = gate
                gate = auto_grab_candidate_verdict(hydrated_gate_candidate, entry)
                inspection_handoff = bool(gate.get("auto_inspect_eligible"))
                candidate = dict(candidate)
                candidate["auto_grab"] = gate
            redact_handoff = bool(privacy_handoff or inspection_handoff)
            waiting_candidate = dict(transfer_candidate or candidate)
            waiting_candidate["auto_grab"] = gate
            row = {
                "review_id": review_id,
                "series": entry.get("series"),
                "issue": entry.get("issue"),
                "filename": filename_leaf(candidate.get("filename")),
                "score": candidate.get("score"),
                "verdict": gate.get("verdict"),
                "candidate_key": candidate_key,
                "autopick_reasons": gate.get("reasons") or [],
                "autopick_review_reasons": gate.get("review_reasons") or [],
                "inspection_handoff": inspection_handoff,
                "inspection_message": gate.get("inspection_message") if inspection_handoff else "",
                "preferred_size_bytes": gate.get("preferred_size_bytes"),
            }
            row = copy_item_context(row, entry)
            if privacy_handoff and not handoff_routing_available:
                row["status"] = "fresh_handoff_required"
                row["reason"] = "fresh in-memory SLSKD handoff routing is unavailable; re-probing before enqueue"
                outcome["rows"].append(row)
                auto_grab_audit("fresh_handoff_required", live=live, dry_run=dry_run, **row)
                continue
            bad_match = bad_candidate_match(str(review_id), candidate)
            if bad_match:
                row["status"] = "skipped_bad_candidate"
                row["reason"] = bad_match.get("reason") or "previous candidate failure"
                row["failure_label"] = bad_match.get("failure_label") or row["reason"]
                outcome["bad_candidate_skipped_count"] += 1
                outcome["bad_candidate_skipped"].append(row)
                outcome["rows"].append(row)
                auto_grab_audit("bad_candidate_skipped_selected", live=live, dry_run=dry_run, **row)
                continue
            if gate.get("verdict") != "auto_grab_safe" and not inspection_handoff:
                row["status"] = "skipped_unsafe_verdict"
                row["reason"] = "final auto-grab verdict was not safe"
                outcome["unsafe_candidate_skipped_count"] += 1
                outcome["unsafe_candidate_skipped"].append(row)
                outcome["rows"].append(row)
                auto_grab_audit("unsafe_candidate_skipped_selected", live=live, dry_run=dry_run, **row)
                continue
            if not allowed:
                row["status"] = "skipped_attempt_limit"
                row["reason"] = attempt_reason
                outcome["rows"].append(row)
                auto_grab_audit("attempt_limit_skipped_selected", live=live, dry_run=dry_run, **row)
                continue
            reservation = {}
            dry_run_binding = bool(
                dry_run
                and entry.get("autopilot_queue")
                and str(entry.get("autopilot_queue_key") or entry.get("queue_key") or entry.get("key") or "").strip()
                and str(entry.get("queue_identity") or "").strip()
            )
            handoff_claim = {
                "claimed": False,
                "reason": "dry_run_no_claim" if dry_run_binding else "automatic_handoff_not_authorized",
            }
            existing_transfer = {}
            automatic_decision = "authorize_enqueue" if dry_run_binding else "invalid_binding"
            reuse_existing = False
            try:
                validation = mark_manual_source_waiting_local(entry, waiting_candidate, dry_run=True)
                validation_record = (validation.get("result") or {}).get("record") or validation.get("record") or {}
                row["waiting_validation"] = privacy_safe_waiting_record(validation_record)
                if live:
                    reservation = decide_automatic_slskd_handoff(
                        entry,
                        waiting_candidate,
                        "accepted SLSKD candidate reserved before provider handoff",
                        acquire_claim=True,
                    )
                    automatic_decision = str(reservation.get("decision") or "invalid_binding")
                    if automatic_decision in SLSKD_AUTOMATIC_HANDOFF_DECISIONS:
                        handoff_claim = {
                            "claimed": bool(reservation.get("claim_owner_id")),
                            "reason": reservation.get("reason"),
                            "queue_id": reservation.get("queue_id"),
                            "owner_id": reservation.get("claim_owner_id"),
                        }
                        row["candidate_reservation"] = {
                            key: reservation.get(key)
                            for key in ("decision", "ok", "created", "idempotent", "reason", "reservation_id", "download_task_id")
                            if reservation.get(key) not in (None, "")
                        }
                        if automatic_decision == "reuse_existing":
                            reuse_existing = True
                            existing_status = str(reservation.get("status") or "waiting_for_slot").strip().lower()
                            row["status"] = (
                                "already_downloading"
                                if str(reservation.get("state") or "").strip().lower() == "downloading"
                                else existing_status
                            )
                            row["reason"] = "authoritative same-candidate task already owns this handoff"
                            row["slskd_transfer_id"] = reservation.get("external_id")
                        elif automatic_decision != "authorize_enqueue":
                            row["status"] = str(reservation.get("status") or "skipped_durable_queue_gate")
                            row["reason"] = reservation.get("reason") or "durable candidate handoff was not authorized"
                            row["retry_eligible"] = automatic_decision in {"retryable_rollback", "invalid_binding"}
                            row["manual_review_required"] = automatic_decision == "invalid_binding"
                            if row["retry_eligible"]:
                                row["transient_error"] = True
                                row["retry_after_seconds"] = TRANSIENT_AUTO_GRAB_RETRY_SECONDS
                                outcome["transient_error_count"] += 1
                                outcome["transient_errors"].append(row)
                            outcome["rows"].append(row)
                            auto_grab_audit("durable_queue_gate_skipped", live=live, dry_run=dry_run, **row)
                            continue
                    else:
                        row["candidate_reservation"] = {"decision": "invalid_binding", "ok": False}
                        row["status"] = "skipped_durable_queue_gate"
                        row["reason"] = "automatic SLSKD handoff returned an invalid decision"
                        row["retry_eligible"] = False
                        row["manual_review_required"] = True
                        outcome["rows"].append(row)
                        auto_grab_audit("durable_queue_gate_skipped", live=live, dry_run=dry_run, **row)
                        continue
                if reuse_existing:
                    existing_transfer = {}
                elif automatic_decision == "authorize_enqueue":
                    existing_transfer = slskd_existing_download(
                        transfer_candidate, strict_path=inspection_handoff,
                    )
                if existing_transfer:
                    safe_existing_transfer = privacy_safe_handoff_transfer(existing_transfer) if redact_handoff else existing_transfer
                    row["transfer"] = safe_existing_transfer
                    row["slskd_transfer_id"] = existing_transfer.get("id")
                    row["slskd_transfer_state"] = existing_transfer.get("state") or existing_transfer.get("stateDescription")
                    row["slskd_transfer_requested_at"] = existing_transfer.get("requestedAt")
                    if slskd_transfer_failed(existing_transfer):
                        row["stale_failed_transfer"] = safe_existing_transfer
                        row["stale_failed_transfer_state"] = row["slskd_transfer_state"]
                        delete_stale_transfer = slskd_delete_download_transfer(existing_transfer, dry_run=dry_run)
                        row["delete_stale_transfer"] = privacy_safe_handoff_operation(delete_stale_transfer) if redact_handoff else delete_stale_transfer
                        row["status"] = "stale_failed_transfer_cleared" if live else "dry_run_requeue_after_failed_transfer"
                        row["reason"] = "stale failed SLSKD transfer cleared before retry"
                        if live:
                            row["retry_next_candidate"] = True
                            row["attempt_consumed"] = True
                            outcome["failed_attempt_consumed_count"] += 1
                            row["bad_candidate"] = mark_probe_candidate_bad(
                                review_id,
                                entry,
                                candidate,
                                row["reason"],
                                transfer=safe_existing_transfer,
                            )
                        else:
                            existing_transfer = {}
                    else:
                        row["status"] = "already_downloading"
                    if existing_transfer and live and row.get("status") == "already_downloading":
                        mark_waiting = mark_manual_source_waiting_local(
                            entry,
                            waiting_candidate,
                            transfer=existing_transfer,
                            validation=validation,
                        )
                        persisted_waiting = mark_waiting_record_from_response(mark_waiting)
                        row["mark_waiting"] = privacy_safe_waiting_record(persisted_waiting)
                        if not row["mark_waiting"].get("review_id"):
                            row["status"] = "waiting_record_missing"
                            row["reason"] = "existing SLSKD transfer found but no waiting record was persisted"
                            mark_waiting_record_missing_retry(
                                review_id, entry, candidate, row, dry_run=dry_run, raw_transfer=existing_transfer
                            )
                            outcome["failed_attempt_consumed_count"] += 1
                if not existing_transfer and not reuse_existing:
                    row["durable_queue_gate"] = {
                        "decision": automatic_decision,
                        "authorized": automatic_decision == "authorize_enqueue",
                        "claimed": bool(handoff_claim.get("claimed")),
                        "reason": handoff_claim.get("reason"),
                    }
                    if automatic_decision != "authorize_enqueue":
                        row["status"] = "skipped_durable_queue_gate"
                        row["reason"] = handoff_claim.get("reason") or "durable queue no longer permits handoff"
                        outcome["rows"].append(row)
                        auto_grab_audit("durable_queue_gate_skipped", live=live, dry_run=dry_run, **row)
                        continue
                    tried_enqueue = True
                    try:
                        enqueue = slskd_enqueue_candidate(transfer_candidate, dry_run=dry_run)
                        enqueue_match = auto_grab_transfer_from_enqueue(
                            enqueue,
                            transfer_candidate,
                            strict_path=inspection_handoff,
                        )
                        post_enqueue_gate = (
                            inkdrop_state.validate_slskd_candidate_claim(
                                INKDROP_STATE_DB,
                                reservation.get("queue_id"),
                                reservation.get("reservation_id"),
                                reservation.get("claim_owner_id"),
                            )
                            if handoff_claim.get("claimed")
                            else {"valid": dry_run, "reason": handoff_claim.get("reason") or "no_durable_claim_required"}
                        )
                        row["durable_queue_post_enqueue_gate"] = {
                            "valid": bool(post_enqueue_gate.get("valid")),
                            "reason": post_enqueue_gate.get("reason"),
                        }
                        if not post_enqueue_gate.get("valid"):
                            raced_transfer = enqueue_match.get("transfer") or {}
                            if raced_transfer:
                                cancelled = slskd_delete_download_transfer(raced_transfer, dry_run=False)
                                row["raced_transfer_cancelled"] = privacy_safe_handoff_operation(cancelled)
                            if reservation.get("reservation_id"):
                                row["candidate_transition"] = inkdrop_state.transition_slskd_candidate_task(
                                    INKDROP_STATE_DB,
                                    reservation.get("queue_id"),
                                    reservation.get("reservation_id"),
                                    "reservation_failed",
                                    reason="durable queue changed during SLSKD handoff",
                                    claim_owner_id=reservation.get("claim_owner_id"),
                                )
                            row["status"] = "skipped_durable_queue_gate"
                            row["reason"] = "durable queue changed while the SLSKD handoff was being submitted"
                            outcome["rows"].append(row)
                            audit_row = dict(row)
                            audit_row["waiting_validation"] = {
                                "persisted": bool((row.get("waiting_validation") or {}).get("review_id"))
                            }
                            auto_grab_audit("durable_queue_gate_race_cancelled", live=live, dry_run=dry_run, **audit_row)
                            continue
                    except Exception:
                        raise
                    row["enqueue"] = privacy_safe_handoff_enqueue(enqueue) if redact_handoff else enqueue
                    row["enqueue_transfer_match"] = {
                        key: value
                        for key, value in enqueue_match.items()
                        if key not in ({"transfer", "candidate_rows"} if redact_handoff else {"transfer"})
                        and value not in (None, "", [], {})
                    }
                    transfer = enqueue_match.get("transfer") or {}
                    if transfer:
                        row["transfer"] = privacy_safe_handoff_transfer(transfer) if redact_handoff else transfer
                        row["slskd_transfer_id"] = transfer.get("id")
                        row["slskd_transfer_state"] = transfer.get("state") or transfer.get("stateDescription")
                        row["slskd_transfer_requested_at"] = transfer.get("requestedAt")
                    if not transfer:
                        row["status"] = "enqueue_response_ambiguous"
                        row["reason"] = enqueue_match.get("reason") or (
                            "SLSKD enqueue returned no authoritative transfer identity"
                        )
                        row["transient_error"] = True
                        row["retry_after_seconds"] = TRANSIENT_AUTO_GRAB_RETRY_SECONDS
                        row["enqueue_response_row_count"] = len(enqueue_match.get("candidate_rows") or [])
                        if not redact_handoff:
                            row["enqueue_response_rows"] = enqueue_match.get("candidate_rows") or []
                        outcome["transient_error_count"] += 1
                        outcome["transient_errors"].append(row)
                        log("slskd_auto_grab_ambiguous_enqueue_response", **row)
                    elif transfer and slskd_transfer_failed(transfer):
                        row["status"] = "transfer_failed"
                        row["reason"] = slskd_transfer_failure_reason(transfer)
                        delete_terminal_transfer = slskd_delete_download_transfer(transfer, dry_run=dry_run)
                        row["delete_terminal_transfer"] = privacy_safe_handoff_operation(delete_terminal_transfer) if redact_handoff else delete_terminal_transfer
                        row["retry_next_candidate"] = True
                        row["attempt_consumed"] = True
                        outcome["failed_attempt_consumed_count"] += 1
                        if live:
                            row["bad_candidate"] = mark_probe_candidate_bad(
                                review_id,
                                entry,
                                candidate,
                                row["reason"],
                                transfer=privacy_safe_handoff_transfer(transfer) if redact_handoff else transfer,
                            )
                    elif live:
                        mark_waiting = mark_manual_source_waiting_local(
                            entry,
                            waiting_candidate,
                            transfer=transfer,
                            validation=validation,
                            api_error="" if transfer else "SLSKD enqueue response did not include a transfer row",
                        )
                        persisted_waiting = mark_waiting_record_from_response(mark_waiting)
                        row["mark_waiting"] = privacy_safe_waiting_record(persisted_waiting)
                        if row["mark_waiting"].get("review_id"):
                            row["status"] = "started_waiting"
                            if inspection_handoff:
                                row["reason"] = AUTO_INSPECT_USER_MESSAGE
                            outcome["started_count"] += 1
                        else:
                            row["status"] = "waiting_record_missing"
                            row["reason"] = "SLSKD transfer was queued but no waiting record was persisted"
                            mark_waiting_record_missing_retry(
                                review_id, entry, candidate, row, dry_run=dry_run, raw_transfer=transfer
                            )
                            outcome["failed_attempt_consumed_count"] += 1
                    else:
                        row["status"] = "dry_run_safe"
            except Exception as exc:
                safe_error = "SLSKD handoff API request failed" if redact_handoff else f"{type(exc).__name__}: {exc}"
                if live and auto_grab_error_is_transient(exc):
                    if redact_handoff:
                        row["status"] = "transient_error"
                        row["transient_error"] = True
                        row["retry_after_seconds"] = TRANSIENT_AUTO_GRAB_RETRY_SECONDS
                        row["error"] = safe_error
                        row["reason"] = "SLSKD download API error; retrying candidate shortly"
                    else:
                        mark_auto_grab_transient_error(row, exc)
                    no_persisted_transfer = not row.get("transfer") and not row.get("mark_waiting")
                    can_try_next_candidate = no_persisted_transfer and (
                        tried_enqueue
                        or bool(row.get("waiting_validation"))
                    )
                    if can_try_next_candidate:
                        row["status"] = "download_api_error" if tried_enqueue else "download_preflight_api_error"
                        row.pop("transient_error", None)
                        row.pop("retry_after_seconds", None)
                        row["reason"] = "SLSKD API failed before creating a transfer; trying next best candidate"
                        row["retry_next_candidate"] = True
                        row["attempt_consumed"] = True
                        row["candidate_enqueue_error"] = bool(tried_enqueue)
                        row["candidate_preflight_error"] = not bool(tried_enqueue)
                        outcome["failed_attempt_consumed_count"] += 1
                        log("slskd_auto_grab_download_api_error_try_next", **row)
                    else:
                        outcome["transient_error_count"] += 1
                        outcome["transient_errors"].append(row)
                        log("slskd_auto_grab_transient_error", **row)
                else:
                    row["status"] = "error"
                    row["error"] = safe_error
                    log("slskd_auto_grab_error", **row)
                if live and tried_enqueue and not row.get("transient_error"):
                    row["attempt_consumed"] = True
                    outcome["failed_attempt_consumed_count"] += 1
            if live and reservation.get("reservation_id") and automatic_decision in {"authorize_enqueue", "reuse_existing"}:
                transition_status = str(row.get("status") or "").strip().lower()
                supported = (
                    inkdrop_state.SLSKD_RESERVATION_ACTIVE_STATUSES
                    | inkdrop_state.SLSKD_RESERVATION_TERMINAL_STATUSES
                )
                if transition_status not in supported:
                    transition_status = "reservation_failed"
                row["candidate_transition"] = inkdrop_state.transition_slskd_candidate_task(
                    INKDROP_STATE_DB,
                    reservation.get("queue_id"),
                    reservation.get("reservation_id"),
                    transition_status,
                    transfer_id=row.get("slskd_transfer_id"),
                    reason=row.get("reason") or row.get("error") or transition_status,
                    claim_owner_id=reservation.get("claim_owner_id"),
                    extra={
                        "review_id": review_id,
                        "candidate_instance_identity": candidate_key,
                        "candidate_locator_digest": slskd_private_locator_digest(waiting_candidate, entry),
                    },
                )
                row["durable_queue_claim_released"] = bool(row["candidate_transition"].get("ok"))
            elif live and handoff_claim.get("claimed"):
                row["durable_queue_claim_released"] = release_autopilot_handoff_claim(handoff_claim)
            if privacy_handoff:
                SERIES_RUN_EPHEMERAL_CANDIDATES.pop(
                    str(candidate.get("series_directory_handoff_token") or ""),
                    None,
                )
            if live and (
                (
                    not row.get("transient_error")
                    or row.get("status") in {"enqueue_response_ambiguous", "ambiguous_enqueue_response"}
                )
                and (
                    row.get("enqueue")
                    or row.get("mark_waiting")
                    or row.get("status") == "already_downloading"
                    or row.get("attempt_consumed")
                )
            ):
                record_auto_grab_attempt(state, review_id, candidate, row)
                state_dirty = True
            if live and not row.get("candidate_transition") and row.get("status") in {
                "started_waiting",
                "already_downloading",
                "transfer_failed",
                "stale_failed_transfer_cleared",
                "enqueue_response_ambiguous",
                "ambiguous_enqueue_response",
                "download_api_error",
                "download_preflight_api_error",
                "waiting_record_missing",
                "error",
            }:
                row["inkdrop_queue_attempt"] = record_slskd_queue_attempt(
                    entry,
                    candidate,
                    row.get("status"),
                    row.get("reason") or row.get("error") or row.get("slskd_transfer_state") or "SLSKD auto-grab attempt",
                    transfer=row.get("transfer"),
                    extra={
                        "review_id": review_id,
                        "candidate_key": candidate_key,
                        "ambiguous_enqueue_response": row.get("status") in {"enqueue_response_ambiguous", "ambiguous_enqueue_response"},
                        "enqueue_match_status": (row.get("enqueue_transfer_match") or {}).get("match_status"),
                        "enqueue_row_count": (row.get("enqueue_transfer_match") or {}).get("row_count"),
                        "enqueue_response_rows": row.get("enqueue_response_rows"),
                        "retry_next_candidate": row.get("retry_next_candidate"),
                        "attempt_consumed": row.get("attempt_consumed"),
                    },
                )
            auto_grab_audit("attempt", live=live, dry_run=dry_run, **row)
            outcome["rows"].append(row)
            if row.get("status") in {"started_waiting", "already_downloading", "dry_run_safe"}:
                break
            if row.get("retry_next_candidate"):
                retry_event = (
                    "retry_next_candidate_after_download_api_error"
                    if row.get("candidate_enqueue_error") or row.get("candidate_preflight_error")
                    else "retry_next_candidate_after_transfer_failure"
                )
                auto_grab_audit(retry_event, live=live, dry_run=dry_run, **row)
                continue
            if row.get("transient_error"):
                auto_grab_audit("retry_same_candidate_after_transient_error", live=live, dry_run=dry_run, **row)
                break
            if live and row.get("attempt_consumed") and not row.get("enqueue") and not row.get("transfer"):
                auto_grab_audit("retry_next_candidate_after_enqueue_error", live=live, dry_run=dry_run, **row)
                continue
            break
    if state_dirty:
        commit_auto_grab_state_changes(base_state, state)
    log("slskd_auto_grab", live=live, dry_run=dry_run, candidate_count=len(rows), selected_count=len(selected), started_count=outcome["started_count"])
    return outcome


def run_auto_grab(args, result):
    """Guarantee raw series-handoff routing is erased on every exit path."""

    try:
        return _run_auto_grab_with_ephemeral_candidates(args, result)
    finally:
        SERIES_RUN_EPHEMERAL_CANDIDATES.clear()


def attach_match_explanation(candidate, item, match_filename=None):
    details = item_match_details(match_filename or candidate.get("filename") or candidate.get("path") or "", item)
    candidate["match_reasons"] = list(details.get("reasons") or [])
    candidate["match_penalties"] = list(details.get("penalties") or [])
    score, notes = score_candidate_details(candidate, item)
    candidate["score"] = score
    candidate["score_reasons"] = notes
    return candidate


def path_is_within(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def stable_file(path, min_age_seconds=60):
    try:
        return path.is_file() and now() - path.stat().st_mtime >= min_age_seconds
    except OSError:
        return False


def staged_roots():
    return [
        ("slskd_downloads", SLSKD_DOWNLOAD_ROOT),
        ("manual_inbox", MANUAL_COMICS_INBOX),
    ]


def staged_scan_cache_key():
    return tuple(
        (source, str(root), str(SLSKD_INCOMPLETE_ROOT) if source == "slskd_downloads" else "")
        for source, root in staged_roots()
    )


def staged_scan_priority_context():
    actions = load_actions()
    waiting = actions.get("manual_source_waiting") if isinstance(actions, dict) else {}
    if not isinstance(waiting, dict):
        waiting = {}
    words = set()
    segments = set()
    leaves = set()
    ignored_words = STOP_WORDS | {
        "back",
        "books",
        "download",
        "downloads",
        "issue",
        "public",
        "seedbox",
        "share",
        "shared",
        "temp",
    }
    for record in waiting.values():
        if not isinstance(record, dict):
            continue
        values = [
            record.get("series"),
            record.get("query"),
            record.get("search_query"),
            record.get("filename_leaf"),
            filename_leaf(record.get("filename")),
        ]
        for value in values:
            for word in normalize(value).split():
                if len(word) > 2 and word not in ignored_words:
                    words.add(word)
        leaf = filename_leaf(record.get("filename") or record.get("filename_leaf"))
        if leaf:
            leaves.add(normalize(leaf))
            leaves.add(normalize(Path(leaf).stem))
        for segment in path_segments(record.get("filename"))[:-1]:
            key = normalize(segment)
            if key and key not in ignored_words:
                segments.add(key)
            for word in key.split():
                if len(word) > 2 and word not in ignored_words:
                    words.add(word)
    return {"words": words, "segments": segments, "leaves": leaves}


def path_mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0


def staged_path_words(path):
    try:
        text = " ".join(Path(path).parts)
    except TypeError:
        text = str(path or "")
    return set(normalize(text).split())


def staged_directory_sort_key(path, source, root, context):
    words = staged_path_words(path)
    name_key = normalize(Path(path).name)
    hint_hit = bool(
        name_key in context.get("segments", set())
        or words & context.get("words", set())
    )
    comicish = bool(words & (COMIC_CONTEXT_WORDS | ENGLISH_RELEASE_MARKERS))
    non_comic = bool(words & NON_COMIC_CONTEXT_WORDS)
    return (
        0 if hint_hit else 1,
        0 if comicish else 1,
        1 if non_comic else 0,
        -path_mtime(path),
        str(Path(path).name).lower(),
    )


def staged_filename_sort_key(directory, filename, context):
    path = Path(directory) / filename
    leaf_key = normalize(filename)
    stem_key = normalize(Path(filename).stem)
    words = staged_path_words(path)
    waiting_leaf = bool(leaf_key in context.get("leaves", set()) or stem_key in context.get("leaves", set()))
    hint_hit = bool(waiting_leaf or words & context.get("words", set()))
    non_comic = bool(words & NON_COMIC_CONTEXT_WORDS)
    return (
        0 if waiting_leaf else 1,
        0 if hint_hit else 1,
        0 if extension_for(filename) in COMIC_EXTENSIONS else 1,
        1 if non_comic else 0,
        -path_mtime(path),
        str(filename).lower(),
    )


def relative_display_path(path, root):
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (OSError, ValueError):
        return str(path)


def has_internal_path_segment(path, root):
    try:
        relative = Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return any(str(part).startswith("_") for part in relative.parts)


def staged_context_filename(path, root):
    try:
        relative = Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return Path(path).name
    if len(relative.parts) <= 1:
        return Path(path).name
    return display_clean(" ".join(str(part) for part in relative.parts))


def concrete_filename_issue_mismatch(filename, item):
    details = issue_number_match(filename, item)
    if details.get("matched"):
        return ""
    penalty = str(details.get("penalty") or "")
    if penalty and penalty not in {"no numeric issue token", "missing issue/part token"}:
        return penalty
    if shared_volume_artifact_match(filename, item):
        return ""
    volume_details = book_volume_number_match(filename, item)
    if volume_details.get("matched"):
        return ""
    volume_penalty = str(volume_details.get("penalty") or "")
    if volume_penalty and volume_penalty not in {"no book/volume token"}:
        return volume_penalty
    return ""


def weak_staged_filename_guard(filename, item):
    filename = str(filename or "").strip()
    if not filename:
        return ""
    stem = Path(filename).stem
    if not stem:
        return ""

    import re as _re

    text = stem.replace("_", " ")
    lower = text.lower()
    range_lower = re.sub(
        r"\b(?:19|20)\d{2}\s*(?:-|\u2013|\u2014)\s*(?:0?[1-9]|1[0-2])\b",
        " ",
        lower,
        flags=re.I,
    )
    item = item if isinstance(item, dict) else {}
    target_title = str(item.get("series") or item.get("title") or "").lower()
    target_volume = any(
        item.get(key) not in (None, "")
        for key in ("volume", "volume_number", "book_volume", "manga_volume")
    )
    target_volume = target_volume or bool(_re.search(r"\b(?:v|vol(?:ume)?)\.?\s*\d+\b", target_title))
    collection_target = any(
        marker in target_title
        for marker in (
            "omnibus",
            "library edition",
            "deluxe edition",
            "complete collection",
            "compendium",
            "trade paperback",
            " tpb",
        )
    )

    copy_suffix = _re.search(r"\s\(([2-9][0-9]*)\)$", stem)
    if copy_suffix:
        try:
            copy_suffix_value = int(copy_suffix.group(1))
        except ValueError:
            copy_suffix_value = 0
        if not (1900 <= copy_suffix_value <= 2099):
            return "duplicate_copy_suffix"

    range_patterns = (
        r"\b(?:v|vol(?:ume)?s?)\.?\s*0*\d+(?:\.\d+)?\s*(?:-|\u2013|\u2014|to)\s*(?:v|vol(?:ume)?s?)?\.?\s*0*\d+(?:\.\d+)?\b",
        r"\b(?:ch|chap(?:ter)?s?|issues?)\.?\s*0*\d+(?:\.\d+)?\s*(?:-|\u2013|\u2014|to)\s*(?:ch|chap(?:ter)?s?|issues?)?\.?\s*0*\d+(?:\.\d+)?\b",
        r"\b0*\d+(?:\.\d+)?\s*(?:-|\u2013|\u2014|to)\s*0*\d+(?:\.\d+)?\b",
        r"\b(?:complete|collection|pack|set)\b",
    )
    if any(_re.search(pattern, range_lower) for pattern in range_patterns):
        return "pack_candidate_requires_pack_handling"

    strong_unit = _re.search(
        r"(?:#\s*0*\d+|(?:issue|chapter|chap|ch|vol(?:ume)?|v)\.?\s*0*\d+|0*\d+\s*(?:of|/)\s*\d+)",
        lower,
    )
    if _re.search(r"^\s*\d{1,4}[\s_.-]+[A-Za-z]", stem) and not strong_unit:
        return "weak_filename_unit_evidence"

    if collection_target and _re.search(r"\b(?:part|pt|chapter|chap|ch|issue)\.?\s*0*\d+\b", lower):
        return "single_part_file_does_not_satisfy_collection_target"

    if target_volume and _re.search(r"\b(?:chapter|chap|ch)\.?\s*0*\d+\b", lower):
        return "unit_model_mismatch"

    return ""


def staged_match_details(path, root, item):
    leaf = Path(path).name
    weak_guard = weak_staged_filename_guard(leaf, item)
    if weak_guard:
        return {
            "matched": False,
            "match_basis": "filename",
            "match_text": leaf,
            "penalties": [weak_guard],
            "weak_filename_guard": True,
        }
    leaf_details = item_match_details(leaf, item)
    if leaf_details.get("matched"):
        leaf_details["match_basis"] = "filename"
        leaf_details["match_text"] = leaf
        return leaf_details
    leaf_penalties = " | ".join(str(value).lower() for value in (leaf_details.get("penalties") or []))
    if "different titled series/subseries" in leaf_penalties or "filename title appears to be a different series" in leaf_penalties:
        leaf_details["match_basis"] = "filename"
        leaf_details["match_text"] = leaf
        return leaf_details
    leaf_issue_mismatch = concrete_filename_issue_mismatch(leaf, item)
    if leaf_issue_mismatch:
        penalties = list(leaf_details.get("penalties") or [])
        penalties.append(f"filename issue evidence overrides folder context: {leaf_issue_mismatch}")
        leaf_details["penalties"] = list(dict.fromkeys(str(value) for value in penalties if value))
        leaf_details["match_basis"] = "filename"
        leaf_details["match_text"] = leaf
        return leaf_details
    context = staged_context_filename(path, root)
    if context != leaf:
        context_details = item_match_details(context, item)
        if context_details.get("matched"):
            context_details["match_basis"] = "folder_context"
            context_details["match_text"] = context
            return context_details
    relative = relative_display_path(path, root)
    if relative == leaf:
        leaf_details["match_basis"] = "filename"
        leaf_details["match_text"] = leaf
        return leaf_details
    relative_details = item_match_details(relative, item)
    if relative_details.get("matched"):
        relative_details["match_basis"] = "relative_path"
        relative_details["match_text"] = relative
        return relative_details
    # Keep the more specific leaf rejection because the file name itself is the
    # safest operator-facing explanation.
    leaf_details["match_basis"] = "filename"
    leaf_details["match_text"] = leaf
    return leaf_details


def scan_staged_file_candidates():
    key = staged_scan_cache_key()
    if key in STAGED_FILE_SCAN_CACHE:
        return STAGED_FILE_SCAN_CACHE[key]
    candidates = []
    priority_context = staged_scan_priority_context()
    started = now()
    deadline = started + STAGED_SCAN_MAX_SECONDS
    truncated = False
    for source, root in staged_roots():
        if not root.exists():
            continue
        scanned = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                directory = Path(dirpath)
                if source == "slskd_downloads" and directory == root:
                    dirnames[:] = [name for name in dirnames if name != SLSKD_INCOMPLETE_ROOT.name]
                elif source == "manual_inbox":
                    dirnames[:] = [name for name in dirnames if not str(name).startswith("_")]
                dirnames[:] = sorted(
                    dirnames,
                    key=lambda name: staged_directory_sort_key(directory / name, source, root, priority_context),
                )
                filenames = sorted(
                    filenames,
                    key=lambda name: staged_filename_sort_key(directory, name, priority_context),
                )

                scanned += len(dirnames)
                if scanned > 5000 or now() > deadline:
                    truncated = True
                    break

                for filename in filenames:
                    scanned += 1
                    if scanned > 5000 or now() > deadline:
                        truncated = True
                        break
                    if extension_for(filename) not in COMIC_EXTENSIONS:
                        continue
                    path = directory / filename
                    if source == "manual_inbox" and has_internal_path_segment(path, root):
                        continue
                    if not stable_file(path):
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    candidates.append({
                        "source": source,
                        "root": root,
                        "path": path,
                        "filename": path.name,
                        "size": int(stat.st_size),
                        "mtime": stat.st_mtime,
                        "mtime_iso": utc_stamp(stat.st_mtime),
                        "extension": extension_for(path.name),
                    })
                if truncated:
                    break
        except OSError:
            continue
        if now() > deadline:
            break
    if truncated:
        log("staged_scan_truncated", elapsed_seconds=round(now() - started, 1), candidate_count=len(candidates))
    STAGED_FILE_SCAN_CACHE[key] = candidates
    return candidates


def detected_staged_files(item, max_files=8, review_id=None):
    out = []
    review_id = str(review_id or (item or {}).get("review_id") or "")
    for candidate in scan_staged_file_candidates():
        path = candidate.get("path")
        root = candidate.get("root")
        details = staged_match_details(path, root, item)
        if not details.get("matched"):
            continue
        candidate_for_bad_check = {
            **candidate,
            "path": str(path),
            "filename": candidate.get("filename"),
            # A staged filesystem scan has no peer identity of its own.  Bind
            # bad-candidate history to the authoritative handoff owner carried
            # by the queue/task item so an unrelated peer's filename-only
            # failure cannot shadow this exact transfer.
            "username": (
                (item or {}).get("username")
                or (item or {}).get("slskd_username")
                or (item or {}).get("last_slskd_user")
            ),
            "provider": (item or {}).get("provider"),
        }
        if review_id:
            bad_matches = [
                row for row in matching_bad_candidate_rows(review_id, candidate_for_bad_check)
                if not transient_bad_candidate_retry_ready(row)
            ]
            durable_bad_match = durable_bad_source_candidate_match(candidate_for_bad_check)
            if durable_bad_match:
                bad_matches.append(durable_bad_match)
            if bad_matches:
                # Match-quality memory records describe an earlier matcher
                # decision, not a dangerous artifact. Re-evaluate those rows
                # with the full current identity gate. Every other matching
                # failure row remains authoritative, including a durable row
                # that an older low-confidence row would otherwise shadow.
                identity_gate, _identity_text = candidate_identity_compatibility(
                    candidate_for_bad_check,
                    candidate.get("filename"),
                    item,
                )
                only_stale_match_memory = all(
                    isinstance(row, dict)
                    and str(row.get("reason") or "").strip().lower() == "staged_file_low_confidence"
                    and str(row.get("failure_kind") or "").strip().lower() == "match"
                    for row in bad_matches
                )
                current_identity_safe = (
                    details.get("matched") is True
                    and not details.get("penalties")
                    and identity_gate.get("status") == "compatible"
                    and not identity_gate.get("rejection_codes")
                    and not identity_gate.get("review_codes")
                )
                if not (only_stale_match_memory and current_identity_safe):
                    continue
        row = {
            "source": candidate.get("source"),
            "path": str(path),
            "filename": candidate.get("filename"),
            "size": int(candidate.get("size") or 0),
            "mtime": candidate.get("mtime"),
            "mtime_iso": candidate.get("mtime_iso"),
            "extension": candidate.get("extension"),
            "score": int(details.get("score") or 0),
            "match_reasons": list(details.get("reasons") or []),
            "match_penalties": list(details.get("penalties") or []),
            "match_basis": details.get("match_basis") or "filename",
            "match_text": details.get("match_text") or candidate.get("filename"),
        }
        out.append(row)
    out.sort(key=lambda row: (row.get("score", 0), row.get("mtime", 0)), reverse=True)
    return out[:max_files]


def attach_staged_detection(entry, item):
    review_id = str((item or {}).get("review_id") or (entry or {}).get("review_id") or "")
    detected = detected_staged_files(item, review_id=review_id)
    previous_status = str(entry.get("status") or "")
    entry["staged_scan_at"] = now()
    entry["staged_scan_at_iso"] = utc_stamp()
    entry["detected_count"] = len(detected)
    entry["detected_files"] = detected
    if detected:
        entry["status"] = "staged_file_ready"
    elif previous_status == "staged_file_ready":
        if entry.get("candidates"):
            entry["status"] = "available"
        else:
            entry["status"] = "searched_no_candidates"
    return entry


def response_get(response, key, default=None):
    for candidate in (key, key[:1].upper() + key[1:], key.upper()):
        if isinstance(response, dict) and candidate in response:
            return response[candidate]
    return default


def slskd_response_availability_key(response):
    return (
        bool(response_get(response, "hasFreeUploadSlot", response_get(response, "HasFreeUploadSlot", False))),
        -int(response_get(response, "queueLength", response_get(response, "QueueLength", 0)) or 0),
        int(response_get(response, "uploadSpeed", response_get(response, "UploadSpeed", 0)) or 0),
    )


def file_get(row, key, default=None):
    for candidate in (key, key[:1].upper() + key[1:], key.upper()):
        if isinstance(row, dict) and candidate in row:
            return row[candidate]
    return default


def rejection_label(details):
    penalties = list(details.get("penalties") or [])
    if penalties:
        return str(penalties[0])
    return "rejected by matcher"


def rejection_sample(filename, details):
    return {
        "filename": str(filename or ""),
        "reason": rejection_label(details),
        "score": int(details.get("score") or 0),
        "match_reasons": list(details.get("reasons") or []),
        "match_penalties": list(details.get("penalties") or []),
    }


def rejection_sample_priority(filename, details):
    label = rejection_label(details)
    if extension_for(filename) in COMIC_EXTENSIONS:
        return 0
    if details.get("reasons"):
        return 1
    if not label.startswith("unsupported extension"):
        return 2
    return 3


def summarize_rejections(rejections, checked_file_count, response_count):
    counts = {}
    for filename, details in rejections:
        label = rejection_label(details)
        counts[label] = counts.get(label, 0) + 1
    reason_counts = [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda row: (-row[1], row[0]))[:8]
    ]
    ranked_samples = sorted(
        enumerate(rejections),
        key=lambda row: (
            rejection_sample_priority(row[1][0], row[1][1]),
            int(row[1][1].get("score") or 0),
            row[0],
        ),
    )
    samples = [rejection_sample(filename, details) for _, (filename, details) in ranked_samples[:5]]
    return {
        "response_count": int(response_count or 0),
        "checked_file_count": int(checked_file_count or 0),
        "rejected_file_count": len(rejections),
        "rejection_reasons": reason_counts,
        "rejection_samples": samples,
    }


def candidates_from_responses(responses, item, *, deadline=None, max_files=None, candidate_limit=None, annotate_auto_grab=True):
    out = []
    rejections = []
    checked_file_count = 0
    processing_timed_out = False
    processing_file_cap_reached = False
    bounded = deadline is not None or max_files is not None or candidate_limit is not None
    file_cap = max(1, int(max_files)) if max_files is not None else None
    result_cap = max(1, int(candidate_limit)) if candidate_limit is not None else None
    # Do not let a large locked/busy response consume a bounded file budget.
    ordered_responses = sorted(responses or [], key=slskd_response_availability_key, reverse=True)
    for response in ordered_responses:
        username = response_get(response, "username")
        upload_speed = response_get(response, "uploadSpeed", response_get(response, "UploadSpeed", 0))
        queue_length = response_get(response, "queueLength", response_get(response, "QueueLength", 0))
        free_slot = bool(response_get(response, "hasFreeUploadSlot", response_get(response, "HasFreeUploadSlot", False)))
        files = response_get(response, "files", response_get(response, "Files", [])) or []
        locked_files = response_get(response, "lockedFiles", response_get(response, "LockedFiles", [])) or []
        for rows, force_locked in ((files, False), (locked_files, True)):
            for raw_row in rows:
                if deadline is not None and seconds_remaining(deadline) <= 0:
                    processing_timed_out = True
                    break
                if not isinstance(raw_row, dict):
                    continue
                row = {**raw_row, "IsLocked": True} if force_locked else raw_row
                filename = file_get(row, "filename")
                if not filename:
                    continue
                ext = extension_for(filename)
                if ext not in COMIC_EXTENSIONS:
                    # A file whose extension is not in COMIC_EXTENSIONS is
                    # rejected by item_match_details() on that basis alone,
                    # regardless of series/item -- so it can never become a
                    # candidate. Charging it against the enumeration budget
                    # let a junk-heavy response (e.g. an audio-dominated
                    # folder ordered ahead of the real comic files) exhaust
                    # file_cap before the actual wanted file was ever
                    # reached. Reject it for free instead of spending budget
                    # on a result that was never in question.
                    if len(rejections) < 2000:
                        rejections.append((
                            filename,
                            {
                                "matched": False,
                                "score": -100,
                                "reasons": [],
                                "penalties": [f"unsupported extension {ext or 'no extension'}"],
                            },
                        ))
                    continue
                if file_cap is not None and checked_file_count >= file_cap:
                    processing_file_cap_reached = True
                    break
                checked_file_count += 1
                candidate = {
                    "filename": str(filename),
                    "size": int(file_get(row, "size", 0) or 0),
                    "extension": ext,
                    "username": str(username or ""),
                    "upload_speed": int(upload_speed or 0),
                    "queue_length": int(queue_length or 0),
                    "has_free_upload_slot": free_slot,
                    "locked": bool(file_get(row, "isLocked", False)),
                }
                details = shared_candidate_match_details(
                    filename,
                    item,
                    candidate=candidate,
                )
                if deadline is not None and seconds_remaining(deadline) <= 0:
                    processing_timed_out = True
                    break
                if not details.get("matched"):
                    if len(rejections) < 2000:
                        rejections.append((filename, details))
                    continue
                out.append(attach_match_explanation(candidate, item))
                if result_cap is not None and len(out) > result_cap:
                    out.sort(key=lambda value: (value.get("score", 0), value.get("has_free_upload_slot", False), value.get("upload_speed", 0)), reverse=True)
                    del out[result_cap:]
            if processing_timed_out or processing_file_cap_reached:
                break
        if processing_timed_out or processing_file_cap_reached:
            break
    out.sort(key=lambda row: (row.get("score", 0), row.get("has_free_upload_slot", False), row.get("upload_speed", 0)), reverse=True)
    deduped = []
    seen = set()
    for candidate in out:
        key = (normalize(candidate.get("filename")), candidate.get("username"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
        if len(deduped) >= AUTO_GRAB_CANDIDATE_LIMIT:
            break
    summary = summarize_rejections(rejections, checked_file_count, len(responses or []))
    if bounded:
        summary.update({
            "processing_complete": not processing_timed_out and not processing_file_cap_reached,
            "processing_timed_out": processing_timed_out,
            "processing_file_cap_reached": processing_file_cap_reached,
        })
    candidates = annotate_auto_grab_verdicts(deduped, item) if annotate_auto_grab else deduped
    return candidates, summary


def series_directory_cohort_root(directory):
    """Collapse only generic organizational leaves to their series folder."""

    parts = [part.strip() for part in str(directory or "").replace("\\", "/").split("/") if part.strip()]
    for _ in range(2):
        if len(parts) < 2:
            break
        leaf = normalize(parts[-1])
        organizational = bool(
            re.fullmatch(r"(?:vol(?:ume)?|book)\s*0*\d+(?:\.\d+)?", leaf)
            or re.fullmatch(r"v0*\d+(?:\.\d+)?", leaf)
            or re.fullmatch(r"(?:18|19|20)\d{2}(?:\s+(?:18|19|20)\d{2})?", leaf)
            or leaf in {
                "complete",
                "complete series",
                "digital",
                "issues",
                "single issues",
                "volumes",
            }
        )
        if not organizational:
            break
        parts = parts[:-1]
    return "/".join(parts)


def series_directory_matches_item(directory, item):
    """Match one bounded cohort root to an exact series folder identity."""

    leaf = str(directory or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    series = display_clean(item_series_title(item or {}))
    if not leaf or not series:
        return False
    series_key = normalize(series)
    if normalize(leaf) == series_key:
        return True

    # Keep exact-series folders that add only organizational context.  SLSKD
    # libraries commonly nest usable individual files under folders such as
    # ``Akira Vol. 01`` or ``All Star Superman (2006-2008) (12 Issues)``.
    # This check only retains the folder for inspection; every member still
    # passes ``series_run_candidate_for_item`` and the normal wrong-work,
    # wrong-unit, publisher, year, archive, known-bad, and duplicate gates.
    organizational = str(leaf or "").strip()
    removed_organizational = False
    deferred_group = False
    group_pattern = re.compile(r"\s*[\[(]\s*([^)\]]+?)\s*[\])]\s*$")
    organizational_group = re.compile(
        r"(?i)^(?:"
        r"digital|complete(?:\s+(?:series|collection))?|issues?|single\s+issues|volumes?|"
        r"(?:vol(?:ume)?\.?|book|tome)\s*0*\d+(?:\.\d+)?|"
        r"0*\d+\s*(?:-|–|—|to|through)\s*0*\d+\s*(?:issues?|chapters?|volumes?|books?|trades?)?|"
        r"0*\d+\s+(?:issues?|chapters?|volumes?|books?|trades?)|"
        r"(?:18|19|20)\d{2}(?:\s*(?:-|–|—|to|through)\s*(?:18|19|20)\d{2})?"
        r")$"
    )
    # Permit one trailing bracketed release-group label only when a recognized
    # organizational group immediately precedes it.  The release group never
    # becomes identity evidence; it merely stops a safe folder from vanishing
    # before its individual members are evaluated.
    for _ in range(6):
        match = group_pattern.search(organizational)
        if not match:
            break
        content = str(match.group(1) or "").strip()
        recognized = bool(organizational_group.fullmatch(content))
        if not recognized and (deferred_group or removed_organizational):
            break
        if not recognized:
            deferred_group = True
        else:
            removed_organizational = True
        organizational = organizational[:match.start()].rstrip(" ._-")
    suffix_pattern = re.compile(
        r"(?i)\s+(?:"
        r"digital|complete(?:\s+(?:series|collection))?|issues?|single\s+issues|volumes?|"
        r"(?:vol(?:ume)?\.?|book|tome)\s*0*\d+(?:\.\d+)?|"
        r"0*\d+\s*(?:-|–|—|to|through)\s*0*\d+\s*(?:issues?|chapters?|volumes?|books?|trades?)?|"
        r"0*\d+\s+(?:issues?|chapters?|volumes?|books?|trades?)|"
        r"(?:18|19|20)\d{2}(?:\s*(?:-|–|—|to|through)\s*(?:18|19|20)\d{2})?"
        r")\s*$"
    )
    for _ in range(4):
        match = suffix_pattern.search(organizational)
        if not match:
            break
        removed_organizational = True
        organizational = organizational[:match.start()].rstrip(" ._-")
    if removed_organizational and normalize(organizational) == series_key:
        return True

    qualifiers = []
    year = str((item or {}).get("year") or "").strip()
    if re.fullmatch(r"(?:18|19|20)\d{2}", year):
        qualifiers.append(year)
    publisher = display_clean((item or {}).get("publisher") or (item or {}).get("watch_publisher") or "")
    if publisher:
        qualifiers.append(publisher)
    qualifiers.extend(["complete", "complete series", "digital", "issues", "volumes"])

    allowed = set()
    for qualifier in qualifiers:
        allowed.add(normalize(f"{series} {qualifier}"))
        allowed.add(normalize(f"{qualifier} {series}"))
    for first in qualifiers:
        for second in qualifiers:
            if normalize(first) == normalize(second):
                continue
            allowed.add(normalize(f"{series} {first} {second}"))
    return normalize(leaf) in allowed


def slskd_series_directory_observations(responses, max_files=None, items=None):
    """Return bounded, unlocked, individually safe directory evidence."""

    if max_files is None:
        file_cap = max(1, min(int(SERIES_RUN_MAX_OBSERVED_FILES), 500))
    else:
        file_cap = max(0, min(int(max_files), 500))
    if file_cap <= 0:
        return [], {"observed_file_count": 0, "observed_directory_count": 0,
                    "observation_truncated": bool(responses), "observed_file_cap": 0}
    grouped = {}
    scanned = 0
    locked_skipped = 0
    scan_cap = min(max(file_cap * 20, 512), 5000)
    truncated = False
    active_items = [item for item in (items or []) if item_is_active_wanted_for_series_run(item)[0]]
    for response in responses or []:
        username = str(response_get(response, "username") or "")
        upload_speed = int(response_get(response, "uploadSpeed", response_get(response, "UploadSpeed", 0)) or 0)
        queue_length = int(response_get(response, "queueLength", response_get(response, "QueueLength", 0)) or 0)
        free_slot = bool(response_get(response, "hasFreeUploadSlot", response_get(response, "HasFreeUploadSlot", False)))
        locked_skipped += len(response_get(response, "lockedFiles", response_get(response, "LockedFiles", [])) or [])
        for raw_row in response_get(response, "files", response_get(response, "Files", [])) or []:
            if scanned >= scan_cap:
                truncated = True
                break
            if not isinstance(raw_row, dict):
                continue
            scanned += 1
            filename = str(file_get(raw_row, "filename") or "").strip()
            if not filename or extension_for(filename) not in COMIC_EXTENSIONS or file_get(raw_row, "isLocked", False):
                continue
            normalized_path = filename.replace("\\", "/")
            directory = normalized_path.rsplit("/", 1)[0] if "/" in normalized_path else ""
            if not directory:
                continue
            directory = series_directory_cohort_root(directory)
            key = (normalize(username), normalize(directory))
            observation = grouped.setdefault(key, {
                "username": username, "directory": directory, "upload_speed": upload_speed,
                "queue_length": queue_length, "has_free_upload_slot": free_slot, "files": [],
            })
            observation["files"].append({
                "filename": filename, "size": int(file_get(raw_row, "size", 0) or 0),
                "extension": extension_for(filename), "username": username,
                "upload_speed": upload_speed, "queue_length": queue_length,
                "has_free_upload_slot": free_slot, "locked": False,
            })
        if truncated:
            break
    observations = []
    for observation in grouped.values():
        deduped = []
        seen = set()
        for row in observation["files"]:
            key = (normalize(row.get("filename")), int(row.get("size") or 0))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        if len(deduped) < 2:
            continue
        observation["file_count"] = len(deduped)
        parent_items = [
            item for item in active_items
            if series_directory_matches_item(observation.get("directory"), item)
        ]
        if active_items and not parent_items:
            continue
        coverage = set()
        for row in deduped:
            for item in parent_items:
                rid = str(item.get("review_id") or review_id_for(item))
                if rid in coverage:
                    continue
                candidate, _reason = series_run_candidate_for_item(row, item, observation)
                if candidate:
                    coverage.add(rid)
        # The directory cohort validates the parent independently of the
        # current wanted intersection. Keep its exact archive leaves in memory;
        # apply_series_directory_opportunities performs the per-row unit and
        # common-safety checks. Requiring two currently wanted leaves here made
        # a valid directory disappear when only one issue remained missing.
        observation["files"] = deduped
        observation["file_count"] = len(deduped)
        observation["active_wanted_coverage"] = len(coverage)
        observations.append(observation)
    observations.sort(key=lambda row: (int(row.get("active_wanted_coverage") or 0),
        bool(row.get("has_free_upload_slot")), -int(row.get("queue_length") or 0),
        int(row.get("upload_speed") or 0), int(row.get("file_count") or 0)), reverse=True)
    selected = []
    selected_files = 0
    for observation in observations:
        remaining = file_cap - selected_files
        if remaining <= 0:
            truncated = True
            break
        chosen = dict(observation)
        chosen["files"] = list(observation.get("files") or [])[:remaining]
        chosen["file_count"] = len(chosen["files"])
        if chosen["file_count"] >= 2:
            selected.append(chosen)
            selected_files += chosen["file_count"]
        if chosen["file_count"] < int(observation.get("file_count") or 0):
            truncated = True
            break
    usable_budget_count = min(sum(int(row.get("file_count") or 0) for row in observations), file_cap)
    return selected, {"observed_file_count": usable_budget_count, "observed_directory_count": len(selected),
        "observation_truncated": truncated or len(selected) < len(observations), "observed_file_cap": file_cap,
        "scanned_file_count": scanned, "scan_file_cap": scan_cap, "locked_file_count_skipped": locked_skipped}


def item_is_active_wanted_for_series_run(item):
    state = str((item or {}).get("autopilot_state") or "").strip().lower()
    if state in {"downloading", "importing", "verified", "completed", "needs_you"}:
        return False, "issue is already active, imported, or complete"
    if not item_issue_token(item):
        return False, "wanted row has no numeric issue identity"
    return True, "active wanted issue"


def compact_series_issue_leaf(leaf, item):
    """Separate an exact title from a directly attached issue number."""

    series = display_clean(item_series_title(item or {}))
    if not series or re.search(r"\d\s*$", series):
        return str(leaf or "")
    series_pattern = re.escape(series).replace(r"\ ", r"[\W_]+")
    match = re.match(
        rf"(?i)^\s*(?P<title>{series_pattern})(?P<number>0*\d{{1,4}})(?=$|[\s.\[(\-_])",
        str(leaf or ""),
    )
    if not match:
        return str(leaf or "")
    return (
        str(leaf or "")[:match.start("number")]
        + " "
        + match.group("number")
        + str(leaf or "")[match.end("number"):]
    )


def publisher_identity_key(value):
    generic = {"comic", "comics", "imprint", "press", "publisher", "publishing"}
    return " ".join(word for word in normalize(value).split() if word not in generic)


def explicit_leaf_publisher_conflict(leaf, item):
    """Reject only recognized publisher names in explicit metadata groups."""

    metadata = issue_metadata_for_item(item)
    metadata = metadata if isinstance(metadata, dict) else {}
    target_values = [
        (item or {}).get("publisher"),
        (item or {}).get("watch_publisher"),
        (item or {}).get("publisher_name"),
        (item or {}).get("imprint"),
        metadata.get("publisher"),
        metadata.get("imprint"),
    ]
    target_keys = {publisher_identity_key(value) for value in target_values if publisher_identity_key(value)}
    if not target_keys:
        return ""

    known_phrases = sorted(
        {*WESTERN_COMIC_PUBLISHER_PHRASES, *MANGA_PUBLISHER_PHRASES},
        key=lambda value: len(normalize(value)),
        reverse=True,
    )
    explicit_keys = set()
    for match in re.finditer(r"\(([^)]*)\)|\[([^]]*)\]", str(leaf or "")):
        group = normalize(match.group(1) if match.group(1) is not None else match.group(2))
        if not group:
            continue
        labeled = re.match(r"^(?:publisher|imprint)\s+(.+)$", group)
        publisher_text = labeled.group(1).strip() if labeled else group
        for phrase in known_phrases:
            phrase_key = normalize(phrase)
            unmistakable = bool(
                (labeled and re.search(rf"(?:^|\s){re.escape(phrase_key)}(?:\s|$)", publisher_text))
                or (
                    len(phrase_key.split()) >= 2
                    and publisher_identity_key(publisher_text) == publisher_identity_key(phrase_key)
                )
            )
            if unmistakable:
                identity_key = publisher_identity_key(phrase)
                if identity_key:
                    explicit_keys.add(identity_key)
                break

    conflicts = sorted(key for key in explicit_keys if key not in target_keys)
    if not conflicts:
        return ""
    return (
        "leaf publisher metadata " + ", ".join(display_clean(value) for value in conflicts[:2])
        + " conflicts with wanted publisher "
        + ", ".join(display_clean(value) for value in sorted(target_keys)[:2])
    )


def series_run_leaf_identity_filename(file_row, item):
    """Build bounded identity text only after the leaf proves its exact unit."""

    filename = str((file_row or {}).get("filename") or "")
    leaf = filename_leaf(filename)
    identity_leaf = compact_series_issue_leaf(leaf, item)
    if not identity_leaf:
        return "", "file has no archive leaf identity"
    publisher_conflict = explicit_leaf_publisher_conflict(identity_leaf, item)
    if publisher_conflict:
        return "", publisher_conflict

    if inkdrop_candidate_matching:
        compatibility_leaf = filename_without_exact_issue_titles(identity_leaf, item=item)
        compatibility_leaf, _ignored_imprint = compatibility_title_without_terminal_image_imprint(
            compatibility_leaf,
            item,
        )
        compatibility = inkdrop_candidate_matching.candidate_compatibility(
            {"title": compatibility_leaf, "provider_id": "slskd"},
            item,
        )
        rejection_codes = list(compatibility.get("rejection_codes") or [])
        review_codes = list(compatibility.get("review_codes") or [])
        if rejection_codes or review_codes:
            codes = rejection_codes or review_codes
            return "", (
                "leaf unit identity is not compatible with the wanted row: "
                + ", ".join(codes[:3])
            )
        if not compatibility.get("positive_evidence"):
            return "", "leaf does not prove the exact wanted unit"
    else:
        leaf_number = issue_number_match(identity_leaf, item)
        leaf_volume = book_volume_number_match(identity_leaf, item)
        if not (leaf_number.get("matched") or leaf_volume.get("matched")):
            return "", "leaf does not prove the exact wanted unit"

    target_years = set()
    for key in ("issue_year", "publication_year", "release_year"):
        target_years.update(years_from_value((item or {}).get(key)))
    issue_metadata = issue_metadata_for_item(item)
    if isinstance(issue_metadata, dict):
        # ``year`` on this surface is the watched series year. Individual
        # issues commonly publish later, so only issue-level date fields can
        # veto an explicit leaf year.
        for key in ("date", "publication_date", "release_date"):
            target_years.update(years_from_value(issue_metadata.get(key)))
    leaf_years = years_from_value(identity_leaf)
    if target_years and leaf_years and target_years.isdisjoint(leaf_years):
        return "", (
            "leaf year " + ", ".join(sorted(leaf_years))
            + " does not match wanted year " + ", ".join(sorted(target_years))
        )

    if series_identity_match(identity_leaf, item).get("matched"):
        return identity_leaf, "leaf contains exact series and unit identity"
    prefix = concrete_leaf_title_prefix_words(identity_leaf)
    if prefix:
        return "", "leaf title appears to identify a different series: " + " ".join(prefix[:5])
    series = display_clean(item_series_title(item or {}))
    return f"{series} {identity_leaf}".strip(), "validated parent supplied missing series title"


def series_run_candidate_for_item(file_row, item, observation):
    filename = str((file_row or {}).get("filename") or "")
    if not series_directory_matches_item((observation or {}).get("directory"), item):
        return None, "directory parent does not exactly match the wanted series metadata"
    if extension_for(filename) not in AUTO_GRAB_EXTENSIONS:
        return None, "unsupported or pack archive extension"
    malformed_reason = malformed_unit_syntax_reason(
        filename,
        item=item,
        validated_series_directory=True,
    )
    if malformed_reason:
        return None, malformed_reason
    # A generic parent folder such as "Complete" or "Volume 01" describes
    # organization, not the individual archive. Pack/range policy belongs to
    # the candidate basename after the directory cohort has been validated.
    candidate_leaf = filename.replace("\\", "/").rsplit("/", 1)[-1]
    is_pack, pack_reason = filename_has_pack_or_range(
        candidate_leaf,
        item=item,
        validated_series_directory=True,
    )
    if is_pack:
        return None, f"pack/range is not an individual handoff: {pack_reason}"
    identity_filename, identity_reason = series_run_leaf_identity_filename(file_row, item)
    if not identity_filename:
        return None, identity_reason
    policy_filename = filename_without_exact_issue_titles(identity_filename, item=item)
    unit_details = item_match_details(policy_filename, item)
    if not unit_details.get("matched"):
        return None, (unit_details.get("penalties") or ["file does not match wanted issue/volume"])[0]
    candidate = dict(file_row or {})
    candidate.update({
        "series_directory_handoff": True,
        "series_directory_exact_series": True,
        "series_directory_file_count": int((observation or {}).get("file_count") or 0),
        "series_directory_identity_filename": identity_filename,
    })
    review_id = str((item or {}).get("review_id") or review_id_for(item or {}))
    if review_id and bad_candidate_match(review_id, candidate):
        return None, "candidate was already rejected or failed for this wanted issue"
    candidate = attach_match_explanation(candidate, item, match_filename=policy_filename)
    gate = auto_grab_candidate_verdict(candidate, item)
    candidate["auto_grab"] = gate
    if gate.get("verdict") == "blocked" or not (
        gate.get("autopick_eligible") or gate.get("auto_inspect_eligible")
    ):
        reason = (gate.get("blockers") or gate.get("review_reasons") or ["candidate did not meet automatic safety policy"])[0]
        return None, str(reason)
    return candidate, "exact series/issue file passed existing safety policy"


def series_directory_candidate_rank(candidate):
    return (
        int(candidate.get("series_directory_active_wanted_coverage") or 0),
        bool(candidate.get("has_free_upload_slot")), -int(candidate.get("queue_length") or 0),
        int(candidate.get("score") or 0), int(candidate.get("upload_speed") or 0),
    )


def apply_series_directory_opportunities(entries, items, cache, *, deadline=None, observations=None, selection_budget=None):
    """Intersect observed directories with wanted rows and augment cache safely."""

    if observations is None:
        observations = []
        for entry in entries or []:
            observations.extend((entry or {}).get("series_directory_observations") or [])
    else:
        observations = list(observations or [])
    ledger = selection_budget if isinstance(selection_budget, dict) else {}
    ledger_rows = ledger.setdefault("selected_by_review", {})
    selected_by_review = dict(ledger_rows) if isinstance(ledger_rows, dict) else {}
    selected_bytes = sum(int(value[1].get("size") or 0) for value in selected_by_review.values())
    summary = {
        "observed_directory_count": len(observations),
        "observed_file_count": sum(int(row.get("file_count") or 0) for row in observations),
        "evaluated_issue_count": 0,
        "selected_issue_count": len(selected_by_review),
        "selected_bytes": selected_bytes,
        "selected_review_ids": sorted(selected_by_review),
        "skipped_reason_counts": {},
        "deadline_exhausted": False,
        "issue_cap": SERIES_RUN_MAX_ISSUES,
        "byte_cap": SERIES_RUN_MAX_BYTES,
        "mode": "individual_file_handoff_only",
    }
    if not observations:
        return summary

    seen_observations = set()
    deduped_observations = []
    for observation in observations:
        key = (normalize(observation.get("username")), normalize(observation.get("directory")))
        if key in seen_observations:
            continue
        seen_observations.add(key)
        deduped_observations.append(observation)

    active_items = []
    for item in items or []:
        active, reason = item_is_active_wanted_for_series_run(item)
        if active:
            active_items.append(item)
        else:
            summary["skipped_reason_counts"][reason] = summary["skipped_reason_counts"].get(reason, 0) + 1
    active_items.sort(key=lambda row: (
        normalize(row.get("series") or row.get("query")),
        token_number(row.get("issue")) or 999999,
        str(row.get("issue") or ""),
    ))

    skipped = summary["skipped_reason_counts"]
    ranked_observations = []
    for observation in deduped_observations:
        safe_by_review = {}
        for file_row in observation.get("files") or []:
            if deadline is not None and seconds_remaining(deadline) <= 0:
                summary["deadline_exhausted"] = True
                skipped["series-run evaluation deadline exhausted"] = skipped.get("series-run evaluation deadline exhausted", 0) + 1
                break
            for item in active_items:
                rid = str(item.get("review_id") or review_id_for(item))
                if not series_directory_matches_item(observation.get("directory"), item):
                    continue
                summary["evaluated_issue_count"] += 1
                candidate, reason = series_run_candidate_for_item(file_row, item, observation)
                if not candidate:
                    skipped[reason] = skipped.get(reason, 0) + 1
                    continue
                previous = safe_by_review.get(rid)
                if previous:
                    previous_candidate = previous[1]
                    previous_rank = (
                        int(previous_candidate.get("score") or 0),
                        bool(previous_candidate.get("has_free_upload_slot")),
                        int(previous_candidate.get("upload_speed") or 0),
                    )
                    candidate_rank = (
                        int(candidate.get("score") or 0),
                        bool(candidate.get("has_free_upload_slot")),
                        int(candidate.get("upload_speed") or 0),
                    )
                    if candidate_rank <= previous_rank:
                        skipped["lower-ranked safe peer candidate"] = skipped.get("lower-ranked safe peer candidate", 0) + 1
                        continue
                safe_by_review[rid] = (item, candidate, observation)
            if summary["deadline_exhausted"]:
                break
        if safe_by_review:
            coverage = len(safe_by_review)
            for _rid, (_item, candidate, _observation) in safe_by_review.items():
                candidate["series_directory_active_wanted_coverage"] = coverage
            ranked_observations.append((observation, safe_by_review))
        if summary["deadline_exhausted"]:
            break

    # Prefer broad, available cohorts; each file already passed normal gates.
    ranked_observations.sort(key=lambda pair: (
        len(pair[1]),
        bool(pair[0].get("has_free_upload_slot")),
        -int(pair[0].get("queue_length") or 0),
        max((int(value[1].get("score") or 0) for value in pair[1].values()), default=0),
        int(pair[0].get("upload_speed") or 0),
        int(pair[0].get("file_count") or 0),
    ), reverse=True)
    for observation, safe_by_review in ranked_observations:
        for rid, (item, candidate, _observation) in safe_by_review.items():
            size = int(candidate.get("size") or 0)
            previous = selected_by_review.get(rid)
            candidate_rank = series_directory_candidate_rank(candidate)
            if previous:
                previous_candidate = previous[1]
                previous_rank = series_directory_candidate_rank(previous_candidate)
                if candidate_rank <= previous_rank:
                    skipped["lower-coverage or less-available safe directory"] = skipped.get("lower-coverage or less-available safe directory", 0) + 1
                    continue
                replacement_bytes = summary["selected_bytes"] - int(previous_candidate.get("size") or 0) + size
                if replacement_bytes > SERIES_RUN_MAX_BYTES:
                    skipped["series-run byte cap reached"] = skipped.get("series-run byte cap reached", 0) + 1
                    continue
                selected_by_review[rid] = (item, candidate, observation)
                summary["selected_bytes"] = replacement_bytes
                continue
            if len(selected_by_review) >= SERIES_RUN_MAX_ISSUES:
                skipped["series-run issue cap reached"] = skipped.get("series-run issue cap reached", 0) + 1
                continue
            if summary["selected_bytes"] + size > SERIES_RUN_MAX_BYTES:
                skipped["series-run byte cap reached"] = skipped.get("series-run byte cap reached", 0) + 1
                continue
            selected_by_review[rid] = (item, candidate, observation)
            summary["selected_bytes"] += size

    for rid, (item, candidate, observation) in selected_by_review.items():
        candidate = redact_series_handoff_candidate(rid, candidate)
        existing = cache.get(rid) if isinstance(cache.get(rid), dict) else {}
        merged = merge_query_candidates([existing.get("candidates") or [], [candidate]], item)
        merged = annotate_bad_candidate_verdicts(merged, rid)
        counts = auto_grab_counts(merged)
        entry = copy_item_context(dict(existing), item)
        entry.update({
            "schema_version": PROBE_SCHEMA_VERSION,
            "auto_grab_context_signature": auto_grab_context_signature(),
            "review_id": rid,
            "series": item.get("series"),
            "issue": item.get("issue"),
            "checked_at": now(),
            "checked_at_iso": utc_stamp(),
            "status": "available",
            "candidate_count": len(merged),
            "failed_candidate_count": sum(1 for row in merged if row.get("manual_source_bad_candidate")),
            "auto_grab_safe_count": counts["auto_grab_safe"],
            "auto_grab_review_count": counts["needs_review"],
            "auto_grab_blocked_count": counts["blocked"],
            "candidates": merged,
            "series_directory_opportunity": {
                "status": "selected",
                "mode": "individual_file_handoff_only",
                "directory_file_count": int(observation.get("file_count") or 0),
                "selected_filename": filename_leaf(candidate.get("filename")),
                "selected_size": int(candidate.get("size") or 0),
                "active_wanted_coverage": int(candidate.get("series_directory_active_wanted_coverage") or 0),
                "reason": "active wanted issue intersected with exact safe numbered sibling file",
            },
        })
        cache[rid] = entry
        summary["selected_review_ids"].append(rid)
    summary["selected_issue_count"] = len(selected_by_review)
    summary["selected_review_ids"] = sorted(selected_by_review)
    ledger["selected_by_review"] = selected_by_review
    ledger["selected_bytes"] = summary["selected_bytes"]
    summary["skipped_reasons"] = [
        {"reason": reason, "count": count}
        for reason, count in sorted(skipped.items(), key=lambda row: (-row[1], row[0]))[:20]
    ]
    return summary


def series_directory_completeness(observation, active_items):
    """How much of a series' currently open wanted range this directory alone covers.

    Reuses ``series_run_candidate_for_item`` verbatim per file/item, so this is
    never a looser identity check -- only a completeness measurement taken
    after that gate has already vouched for each file. A directory clears the
    bar only when it covers both a meaningful absolute count and share of the
    series' open run, so a folder with a couple of stray matches never reads
    as "the pack."
    """

    directory = (observation or {}).get("directory")
    matched_items = [item for item in active_items if series_directory_matches_item(directory, item)]
    if not matched_items:
        return {"eligible": False, "covered": {}, "reason": "directory does not match any active wanted series"}
    series_keys = {normalize(item_series_title(item)) for item in matched_items}
    series_active_total = sum(
        1 for item in active_items if normalize(item_series_title(item)) in series_keys
    )
    covered = {}
    for file_row in observation.get("files") or []:
        for item in matched_items:
            rid = str(item.get("review_id") or review_id_for(item))
            if rid in covered:
                continue
            candidate, _reason = series_run_candidate_for_item(file_row, item, observation)
            if candidate:
                covered[rid] = (item, candidate)
    coverage_count = len(covered)
    ratio_pct = (100.0 * coverage_count / series_active_total) if series_active_total else 0.0
    eligible = (
        coverage_count >= SERIES_PACK_COMPLETE_MIN_COVERAGE
        and ratio_pct >= SERIES_PACK_COMPLETE_MIN_RATIO_PCT
    )
    reason = "" if eligible else (
        f"coverage {coverage_count}/{series_active_total} ({ratio_pct:.0f}%) below the "
        f"{SERIES_PACK_COMPLETE_MIN_COVERAGE}-issue/{SERIES_PACK_COMPLETE_MIN_RATIO_PCT:.0f}% pack floor"
    )
    return {
        "eligible": eligible,
        "covered": covered,
        "series_active_total": series_active_total,
        "coverage_count": coverage_count,
        "ratio_pct": round(ratio_pct, 1),
        "reason": reason,
    }


def apply_series_pack_complete_opportunities(items, cache, *, observations=None, entries=None, deadline=None):
    """Grab a directory that alone already covers the bulk of a series' open
    wanted range in one pass, instead of leaving every file in it to trickle
    through the paced per-cycle budget in ``apply_series_directory_opportunities``.

    Deliberately a separate pass with its own ceiling, not a change to that
    function's ``SERIES_RUN_MAX_ISSUES``/``SERIES_RUN_MAX_BYTES`` budget: that
    pacing exists to protect the operator's Soulseek account from a burst of
    simultaneous requests, and stays exactly as strict for the common
    partial-coverage case this was built for. This pass only fires once a
    directory has already proven, through the same per-file safety gate
    individual handoff uses, that it holds the bulk of a series' currently
    open run -- a real complete or near-complete pack, not a handful of stray
    files -- and only that proven case gets the larger, dedicated ceiling
    (``AUTO_GRAB_PACK_MAX_BYTES``, the same one already used for single pack
    archives elsewhere in this module).
    """

    if observations is None:
        observations = []
        for entry in entries or []:
            observations.extend((entry or {}).get("series_directory_observations") or [])
    else:
        observations = list(observations or [])
    summary = {
        "mode": "series_pack_complete_handoff",
        "observed_directory_count": len(observations),
        "eligible_directory_count": 0,
        "selected_issue_count": 0,
        "selected_bytes": 0,
        "selected_review_ids": [],
        "skipped_reason_counts": {},
        "deadline_exhausted": False,
        "directory_cap": SERIES_PACK_COMPLETE_MAX_DIRECTORIES,
        "byte_cap": AUTO_GRAB_PACK_MAX_BYTES,
        "min_coverage": SERIES_PACK_COMPLETE_MIN_COVERAGE,
        "min_ratio_pct": SERIES_PACK_COMPLETE_MIN_RATIO_PCT,
    }
    if not observations:
        return summary
    active_items = [item for item in items or [] if item_is_active_wanted_for_series_run(item)[0]]
    if not active_items:
        return summary
    skipped = summary["skipped_reason_counts"]

    seen_observations = set()
    deduped_observations = []
    for observation in observations:
        key = (normalize(observation.get("username")), normalize(observation.get("directory")))
        if key in seen_observations:
            continue
        seen_observations.add(key)
        deduped_observations.append(observation)

    eligible = []
    for observation in deduped_observations:
        if deadline is not None and seconds_remaining(deadline) <= 0:
            summary["deadline_exhausted"] = True
            skipped["series-pack evaluation deadline exhausted"] = skipped.get("series-pack evaluation deadline exhausted", 0) + 1
            break
        completeness = series_directory_completeness(observation, active_items)
        if not completeness["eligible"]:
            reason = completeness.get("reason") or "directory below the pack-complete floor"
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        eligible.append((observation, completeness))

    eligible.sort(key=lambda pair: (pair[1]["coverage_count"], pair[1]["ratio_pct"]), reverse=True)

    selected_by_review = {}
    selected_bytes = 0
    directories_used = 0
    for observation, completeness in eligible:
        if directories_used >= SERIES_PACK_COMPLETE_MAX_DIRECTORIES:
            skipped["series-pack directory cap reached"] = skipped.get("series-pack directory cap reached", 0) + 1
            continue
        directory_selected = 0
        for rid, (item, candidate) in completeness["covered"].items():
            if rid in selected_by_review:
                continue
            size = int(candidate.get("size") or 0)
            if selected_bytes + size > AUTO_GRAB_PACK_MAX_BYTES:
                skipped["series-pack byte cap reached"] = skipped.get("series-pack byte cap reached", 0) + 1
                continue
            candidate = dict(candidate)
            candidate["series_directory_active_wanted_coverage"] = completeness["coverage_count"]
            selected_by_review[rid] = (item, candidate, observation)
            selected_bytes += size
            directory_selected += 1
        if directory_selected:
            directories_used += 1

    for rid, (item, candidate, observation) in selected_by_review.items():
        candidate = redact_series_handoff_candidate(rid, candidate)
        existing = cache.get(rid) if isinstance(cache.get(rid), dict) else {}
        merged = merge_query_candidates([existing.get("candidates") or [], [candidate]], item)
        merged = annotate_bad_candidate_verdicts(merged, rid)
        counts = auto_grab_counts(merged)
        entry = copy_item_context(dict(existing), item)
        entry.update({
            "schema_version": PROBE_SCHEMA_VERSION,
            "auto_grab_context_signature": auto_grab_context_signature(),
            "review_id": rid,
            "series": item.get("series"),
            "issue": item.get("issue"),
            "checked_at": now(),
            "checked_at_iso": utc_stamp(),
            "status": "available",
            "candidate_count": len(merged),
            "failed_candidate_count": sum(1 for row in merged if row.get("manual_source_bad_candidate")),
            "auto_grab_safe_count": counts["auto_grab_safe"],
            "auto_grab_review_count": counts["needs_review"],
            "auto_grab_blocked_count": counts["blocked"],
            "candidates": merged,
            "series_directory_opportunity": {
                "status": "selected",
                "mode": "series_pack_complete_handoff",
                "directory_file_count": int(observation.get("file_count") or 0),
                "selected_filename": filename_leaf(candidate.get("filename")),
                "selected_size": int(candidate.get("size") or 0),
                "active_wanted_coverage": int(candidate.get("series_directory_active_wanted_coverage") or 0),
                "reason": "directory alone covers the bulk of the series' currently open wanted range",
            },
        })
        cache[rid] = entry

    summary["eligible_directory_count"] = len(eligible)
    summary["selected_issue_count"] = len(selected_by_review)
    summary["selected_bytes"] = selected_bytes
    summary["selected_review_ids"] = sorted(selected_by_review)
    summary["skipped_reasons"] = [
        {"reason": reason, "count": count}
        for reason, count in sorted(skipped.items(), key=lambda row: (-row[1], row[0]))[:20]
    ]
    return summary


def merge_query_candidates(candidate_groups, item):
    merged = []
    seen = set()
    for candidate in sorted(
        [dict(row) for group in (candidate_groups or []) for row in (group or []) if isinstance(row, dict)],
        key=lambda row: (int(row.get("score") or 0), bool(row.get("has_free_upload_slot")), int(row.get("upload_speed") or 0)),
        reverse=True,
    ):
        key = (normalize(candidate.get("filename")), candidate.get("username"))
        if key in seen:
            continue
        seen.add(key)
        candidate.pop("auto_grab", None)
        merged.append(candidate)
        if len(merged) >= AUTO_GRAB_CANDIDATE_LIMIT:
            break
    return annotate_auto_grab_verdicts(merged, item)


def manual_search_discovery(item, explicit_queries=None, *, wait_seconds=8, max_queries=6, deadline=None, candidate_limit=None):
    """Run bounded SLSKD discovery without enqueue, cache, or action writes.

    This is deliberately narrower than ``probe_item`` and retains no query text
    in its execution evidence.  Candidate safety is still decided by the same
    title/unit/format gate used by automatic SLSKD acquisition; otherwise an
    exact candidate shown in Manual Search could be needlessly downgraded to an
    operator-only result even though the automatic worker can safely use it.
    """

    item = dict(item or {})
    item["manual_search_discovery"] = True
    query_cap = max(1, min(int(max_queries or 1), 6))
    result_cap = max(1, min(int(candidate_limit or AUTO_GRAB_CANDIDATE_LIMIT), AUTO_GRAB_CANDIDATE_LIMIT))
    supplied = [str(row or "").strip() for row in (explicit_queries or []) if str(row or "").strip()]
    planned_queries = manual_search_query_variants(item, supplied)[:query_cap]
    attempts = []
    candidate_groups = []
    response_count = 0
    rejected_file_count = 0
    processed_file_count = 0
    remaining_file_budget = max(25, min(2000, result_cap * 50))
    completed_query_count = 0
    failure_status = ""
    failure_reason = ""

    for query in planned_queries:
        remaining = seconds_remaining(deadline)
        if remaining is not None and remaining <= 0:
            failure_status = "provider_timeout"
            failure_reason = "manual_search_deadline_exhausted"
            attempts.append({"query_ordinal": len(attempts) + 1, "query_fingerprint": hashlib.sha256(normalize(query).encode()).hexdigest()[:12], "status": "provider_timeout", "elapsed_seconds": 0.0, "response_count": 0, "candidate_count": 0})
            break
        started = now()
        try:
            responses = slskd_search(query, wait_seconds=wait_seconds, deadline=deadline)
            candidates, rejection_summary = candidates_from_responses(
                responses,
                item,
                deadline=deadline,
                max_files=remaining_file_budget,
                candidate_limit=result_cap,
                annotate_auto_grab=False,
            )
            response_count += len(responses or [])
            processed = int(rejection_summary.get("checked_file_count") or 0)
            processed_file_count += processed
            remaining_file_budget = max(0, remaining_file_budget - processed)
            rejected = int(rejection_summary.get("rejected_file_count") or 0)
            rejected_file_count += rejected
            candidate_groups.append(candidates)
            attempt_status = "completed"
            if rejection_summary.get("processing_timed_out"):
                failure_status = "provider_timeout"
                failure_reason = "slskd_normalization_timeout"
                attempt_status = "partial_timeout"
            elif rejection_summary.get("processing_file_cap_reached"):
                failure_status = "provider_failure"
                failure_reason = "slskd_normalization_file_cap"
                attempt_status = "partial_file_cap"
            else:
                completed_query_count += 1
            attempts.append({
                "query_ordinal": len(attempts) + 1,
                "query_fingerprint": hashlib.sha256(normalize(query).encode()).hexdigest()[:12],
                "status": attempt_status,
                "elapsed_seconds": round(now() - started, 3),
                "response_count": len(responses or []),
                "candidate_count": len(candidates),
                "rejected_file_count": rejected,
                "processed_file_count": processed,
            })
            if not failure_status and remaining_file_budget <= 0 and completed_query_count < len(planned_queries):
                failure_status = "provider_failure"
                failure_reason = "slskd_normalization_file_cap"
                attempts[-1]["status"] = "partial_file_cap"
            if failure_status:
                break
            query_verdicts = annotate_auto_grab_verdicts(
                [dict(candidate) for candidate in candidates if isinstance(candidate, dict)],
                item,
            )
            if any(
                str((candidate.get("auto_grab") or {}).get("verdict") or "blocked").lower()
                != "blocked"
                and not (candidate.get("auto_grab") or {}).get("blockers")
                for candidate in query_verdicts
            ):
                # Stop only after a query returns evidence for the requested
                # target. Wrong-work and right-work/wrong-unit rows are retained
                # for diagnostics, but cannot prevent the next bounded query
                # from finding healthy compatible sibling evidence.
                break
        except SLSKDProviderUnavailable:
            failure_status = "provider_unavailable"
            failure_reason = "slskd_provider_unavailable"
            attempts.append({"query_ordinal": len(attempts) + 1, "query_fingerprint": hashlib.sha256(normalize(query).encode()).hexdigest()[:12], "status": failure_status, "elapsed_seconds": round(now() - started, 3), "response_count": 0, "candidate_count": 0})
            break
        except TimeoutError:
            failure_status = "provider_timeout"
            failure_reason = "slskd_provider_timeout"
            attempts.append({"query_ordinal": len(attempts) + 1, "query_fingerprint": hashlib.sha256(normalize(query).encode()).hexdigest()[:12], "status": failure_status, "elapsed_seconds": round(now() - started, 3), "response_count": 0, "candidate_count": 0})
            break
        except Exception as exc:
            if slskd_unavailable_error(exc):
                failure_status = "provider_unavailable"
                failure_reason = "slskd_provider_unavailable"
            else:
                failure_status = "provider_failure"
                failure_reason = "slskd_discovery_failed"
            attempts.append({"query_ordinal": len(attempts) + 1, "query_fingerprint": hashlib.sha256(normalize(query).encode()).hexdigest()[:12], "status": failure_status, "elapsed_seconds": round(now() - started, 3), "response_count": 0, "candidate_count": 0})
            break

    merged = []
    seen = set()
    for candidate in sorted(
        [dict(row) for group in candidate_groups for row in (group or []) if isinstance(row, dict)],
        key=lambda row: (int(row.get("score") or 0), bool(row.get("has_free_upload_slot")), int(row.get("upload_speed") or 0)),
        reverse=True,
    ):
        key = (normalize(candidate.get("filename")), str(candidate.get("username") or ""))
        if key in seen:
            continue
        seen.add(key)
        for unsafe_key in ("auto_grab", "auto_grab_verdict"):
            candidate.pop(unsafe_key, None)
        merged.append(candidate)
        if len(merged) >= result_cap:
            break

    merged = annotate_bad_candidate_verdicts(merged, item.get("review_id"))
    # Reuse the production auto-grab gate instead of maintaining a weaker
    # Manual Search-only interpretation.  The gate keeps wrong-title,
    # wrong-unit, language, format, size, and learned bad-candidate protections
    # intact while allowing its single clear winner to remain auto-grabbable.
    merged = annotate_auto_grab_verdicts(merged, item)
    projected = []
    for candidate in merged:
        gate = candidate.get("auto_grab") if isinstance(candidate.get("auto_grab"), dict) else {}
        verdict = str(gate.get("verdict") or "needs_review").strip().lower()
        automatic = verdict == "auto_grab_safe" and bool(gate.get("autopick_eligible"))
        blocked = verdict == "blocked" or bool(gate.get("blockers"))
        manual_pack, manual_pack_reason = manual_discovery_pack_evidence(candidate.get("filename"), item)
        candidate.update({
            "title": filename_leaf(candidate.get("filename")),
            "provider_id": "slskd",
            "protocol": "soulseek",
            "candidate_identity": slskd_candidate_identity(item, candidate),
            "accepted": not blocked,
            "candidate_safe": automatic,
            "acquisition_capability": "automatic" if automatic else ("unavailable" if blocked else "assisted"),
            "assisted_only": not automatic and not blocked,
            "requires_manual_review": not automatic and not blocked,
            "auto_grab_verdict": verdict,
            "target_compatibility": gate.get("target_compatibility") or {},
            "rejection_codes": list(gate.get("rejection_codes") or []),
            "block_reasons": list(gate.get("blockers") or []),
            "review_reasons": list(gate.get("review_reasons") or []),
            "inspection_message": str(gate.get("inspection_message") or ""),
            "preferred_size_bytes": int(gate.get("preferred_size_bytes") or SLSKD_PREFERRED_EXACT_MIN_BYTES),
            "review_basis": (
                ["peer_source", "automatic_handoff", "shared_auto_grab_gate"]
                if automatic
                else ["peer_source", "operator_assisted_handoff", "shared_auto_grab_gate"]
            ),
        })
        if manual_pack:
            candidate.update({"pack": True, "is_pack": True, "manual_pack_reason": manual_pack_reason})
        projected.append(candidate)
    merged = projected

    if merged:
        status = "results_partial" if failure_status else "results"
        completed = True
        error = ""
    elif failure_status:
        status = failure_status
        completed = False
        error = failure_reason
    elif planned_queries and completed_query_count == len(planned_queries):
        status = "zero_results"
        completed = True
        error = ""
    elif not planned_queries:
        status = "provider_failure"
        completed = False
        error = "no_search_query"
    else:
        status = "provider_failure"
        completed = False
        error = "slskd_discovery_incomplete"

    return {
        "status": status,
        "completed": completed,
        "error": error,
        "candidates": merged,
        "evidence": {
            "contract_version": 1,
            "planned_query_count": len(planned_queries),
            "completed_query_count": completed_query_count,
            "response_count": response_count,
            "candidate_count": len(merged),
            "rejected_file_count": rejected_file_count,
            "processed_file_count": processed_file_count,
            "processing_file_budget": max(25, min(2000, result_cap * 50)),
            "partial_reason": failure_reason,
            "partial_error_count": int(bool(failure_status)),
            "attempts": attempts,
        },
    }


def probe_item(
    item,
    wait_seconds=8,
    max_queries=2,
    query_offset=0,
    include_anchor=True,
    deadline=None,
    progress=None,
    directory_observation_sink=None,
    directory_observation_budget=None,
    directory_items=None,
):
    queries = source_queries(item)
    planned_queries = rotated_query_batch(
        queries,
        max_queries=max_queries,
        offset=query_offset,
        include_anchor=include_anchor,
    )
    attempts = []
    candidate_groups = []
    directory_observations = []
    directory_observation_summary = {
        "observed_file_count": 0,
        "observed_directory_count": 0,
        "observation_truncated": False,
        "observed_file_cap": SERIES_RUN_MAX_OBSERVED_FILES,
    }
    shared_observation_budget = directory_observation_budget if isinstance(directory_observation_budget, dict) else None
    response_count = 0
    for query in planned_queries:
        remaining = seconds_remaining(deadline)
        if remaining is not None and remaining < 8:
            attempts.append({
                "query": query,
                "skipped": "probe_budget_exhausted",
                "remaining_seconds": round(remaining, 1),
            })
            break
        started = now()
        if progress:
            progress(item=item, query=query, attempt_index=len(attempts) + 1, attempt_total=len(planned_queries))
        try:
            # Soulseek responses for less-common titles routinely arrive well
            # after even a 25s snapshot -- confirmed live: a real query for
            # "League of Extraordinary Gentlemen" returned nothing through 40s,
            # then 134 peers/1998 files all at once at 45s. Polling exits early
            # once results settle (see the quiet-period break below), so a
            # longer floor costs nothing for titles that answer fast; it only
            # matters for the slow ones this was silently giving up on.
            responses = slskd_search(
                query,
                wait_seconds=max(50, int(wait_seconds or 0)),
                deadline=deadline,
                reuse_recent_seconds=slskd_repeat_query_cooldown_seconds(),
            )
            response_count += len(responses)
            observation_used = (
                int(shared_observation_budget.get("used") or 0)
                if shared_observation_budget is not None
                else int(directory_observation_summary["observed_file_count"] or 0)
            )
            observation_remaining = max(0, SERIES_RUN_MAX_OBSERVED_FILES - observation_used)
            query_observations, query_observation_summary = slskd_series_directory_observations(
                responses,
                max_files=observation_remaining,
                items=directory_items,
            )
            directory_observations.extend(query_observations)
            directory_observation_summary["observed_file_count"] += int(query_observation_summary.get("observed_file_count") or 0)
            directory_observation_summary["observed_directory_count"] += int(query_observation_summary.get("observed_directory_count") or 0)
            directory_observation_summary["observation_truncated"] = bool(
                directory_observation_summary["observation_truncated"]
                or query_observation_summary.get("observation_truncated")
            )
            if shared_observation_budget is not None:
                shared_observation_budget["used"] = observation_used + int(query_observation_summary.get("observed_file_count") or 0)
                shared_observation_budget["truncated"] = bool(
                    shared_observation_budget.get("truncated")
                    or query_observation_summary.get("observation_truncated")
                )
            candidates, rejection_summary = candidates_from_responses(responses, item)
            # A provider query may initially look safe and then be blocked by
            # durable failed-candidate memory. Apply that annotation before
            # deciding whether the exact requested unit is settled.
            candidates = annotate_bad_candidate_verdicts(candidates, item.get("review_id"))
            candidate_groups.append(candidates)
            query_auto_counts = auto_grab_counts(candidates)
            cumulative_candidates = merge_query_candidates(candidate_groups, item)
            cumulative_candidates = annotate_bad_candidate_verdicts(cumulative_candidates, item.get("review_id"))
            cumulative_auto_counts = auto_grab_counts(cumulative_candidates)
            persisted_rejection_summary = dict(rejection_summary)
            # Full remote paths in rejection samples can reveal peer library
            # inventory. Counts and normalized reasons are sufficient for the
            # durable cache/status diagnostic contract.
            persisted_rejection_summary.pop("rejection_samples", None)
            attempt = {
                "query": query,
                "elapsed_seconds": round(now() - started, 1),
                "response_count": len(responses),
                "candidate_count": len(candidates),
                "auto_grab_safe_count": query_auto_counts["auto_grab_safe"],
                "cumulative_auto_grab_safe_count": cumulative_auto_counts["auto_grab_safe"],
                "series_directory_observation_count": len(query_observations),
                **persisted_rejection_summary,
            }
            attempts.append(attempt)
            if cumulative_auto_counts["auto_grab_safe"]:
                attempt["search_stop_reason"] = "safe_exact_candidate_found"
                break
        except SLSKDProviderUnavailable as exc:
            status_payload = exc.status if isinstance(exc.status, dict) else {}
            connected = status_payload.get("isConnected") if "isConnected" in status_payload else None
            logged_in = status_payload.get("isLoggedIn") if "isLoggedIn" in status_payload else None
            transitioning = status_payload.get("isTransitioning") if "isTransitioning" in status_payload else None
            provider_attempt = {
                "provider_state": status_payload.get("state"),
                "provider_connected": bool(connected) if connected is not None else None,
                "provider_logged_in": bool(logged_in) if logged_in is not None else None,
                "provider_transitioning": bool(transitioning) if transitioning is not None else None,
                "provider_error": f"{type(exc).__name__}: {exc}",
            }
            attempts.append({
                "query": query,
                "elapsed_seconds": round(now() - started, 1),
                "status": slskd_provider_wait_status(provider_attempt),
                "error": f"{type(exc).__name__}: {exc}",
                **provider_attempt,
                "transient_error": True,
            })
            log(
                "probe_query_error",
                review_id=item.get("review_id"),
                query=query,
                error=f"{type(exc).__name__}: {exc}",
                provider_state=status_payload.get("state"),
            )
            break
        except Exception as exc:
            attempt = {"query": query, "elapsed_seconds": round(now() - started, 1), "error": f"{type(exc).__name__}: {exc}"}
            if slskd_unavailable_error(exc):
                attempt["status"] = "provider_unavailable"
                attempt["provider_error"] = f"{type(exc).__name__}: {exc}"
                attempt["transient_error"] = True
            attempts.append(attempt)
            log("probe_query_error", review_id=item.get("review_id"), query=query, error=f"{type(exc).__name__}: {exc}")
    best_candidates = merge_query_candidates(candidate_groups, item)
    status = "available" if best_candidates else ("searched_no_candidates" if attempts else "no_query")
    provider_wait_statuses = {"api_error", "provider_unavailable", "provider_wait"}
    if attempts and all(attempt.get("status") in provider_wait_statuses for attempt in attempts):
        status = next((attempt.get("status") for attempt in attempts if attempt.get("status") in {"provider_unavailable", "provider_wait"}), "provider_unavailable")
    elif attempts and all(attempt.get("skipped") == "probe_budget_exhausted" for attempt in attempts):
        status = "probe_budget_exhausted"
    elif attempts and all(attempt.get("error") for attempt in attempts):
        status = "error"
    best_candidates = annotate_bad_candidate_verdicts(best_candidates, item.get("review_id"))
    auto_counts = auto_grab_counts(best_candidates)
    failed_candidate_count = sum(1 for row in best_candidates if (row or {}).get("manual_source_bad_candidate"))
    if best_candidates and failed_candidate_count == len(best_candidates):
        status = "failed_candidates_exhausted"
    entry = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "auto_grab_context_signature": auto_grab_context_signature(),
        "review_id": item.get("review_id"),
        "series": item.get("series"),
        "issue": item.get("issue"),
        "reason": item.get("reason"),
        "checked_at": now(),
        "checked_at_iso": utc_stamp(),
        "status": status,
        "queries": attempts,
        "attempts": attempts,
        "query_total": len(queries),
        "query_offset": int(query_offset or 0) % len(queries) if queries else 0,
        "query_anchor_included": bool(include_anchor),
        "next_query_offset": next_query_offset(
            queries,
            query_offset,
            sum(1 for attempt in attempts if not attempt.get("skipped")),
            max_queries=max_queries,
            include_anchor=include_anchor,
        ),
        "query_signature": query_signature(queries),
        "query_rotation_evidence": query_rotation_evidence(queries, attempts),
        "response_count": response_count,
        "candidate_count": len(best_candidates),
        "failed_candidate_count": failed_candidate_count,
        "auto_grab_safe_count": auto_counts["auto_grab_safe"],
        "auto_grab_review_count": auto_counts["needs_review"],
        "auto_grab_blocked_count": auto_counts["blocked"],
        "candidates": best_candidates,
        "series_directory_observation_summary": directory_observation_summary,
    }
    api_error_attempt = next((attempt for attempt in attempts if attempt.get("status") in provider_wait_statuses), None)
    if api_error_attempt:
        entry["reason"] = "slskd_provider_unavailable"
        entry["provider_state"] = api_error_attempt.get("provider_state")
        entry["provider_connected"] = api_error_attempt.get("provider_connected")
        entry["provider_logged_in"] = api_error_attempt.get("provider_logged_in")
        entry["provider_transitioning"] = api_error_attempt.get("provider_transitioning")
        entry["provider_error"] = api_error_attempt.get("provider_error") or api_error_attempt.get("error")
        entry["transient_error"] = True
    entry = copy_item_context(entry, item)
    if directory_observation_sink is not None:
        directory_observation_sink.extend(directory_observations)
    return attach_staged_detection(entry, item)


def should_skip_cache(cache_entry, cooldown_hours, force=False):
    if force or not cache_entry:
        return False
    for candidate in (cache_entry or {}).get("candidates") or []:
        if not isinstance(candidate, dict) or not candidate.get("series_directory_handoff"):
            continue
        token = str(candidate.get("series_directory_handoff_token") or "").strip()
        if not token or token not in SERIES_RUN_EPHEMERAL_CANDIDATES:
            return False
    checked = float(cache_entry.get("checked_at") or 0)
    return checked > now() - max(0, float(cooldown_hours)) * 3600


def entry_has_rejection_summary(cache_entry):
    for attempt in (cache_entry or {}).get("queries") or []:
        if not isinstance(attempt, dict):
            continue
        if attempt.get("rejection_reasons") or attempt.get("rejection_samples"):
            return True
    return False


def entry_has_match_explanations(cache_entry):
    candidates = list((cache_entry or {}).get("candidates") or [])
    detected = list((cache_entry or {}).get("detected_files") or [])
    rows = candidates + detected
    if not rows:
        return True
    return all(row.get("match_reasons") or row.get("match_penalties") for row in rows if isinstance(row, dict))


def entry_has_current_auto_grab_verdicts(cache_entry):
    candidates = [row for row in (cache_entry or {}).get("candidates") or [] if isinstance(row, dict)]
    if not candidates:
        return True
    for candidate in candidates:
        gate = candidate.get("auto_grab")
        if not isinstance(gate, dict) or not gate.get("verdict"):
            return False
        try:
            policy_version = int(gate.get("policy_version") or 0)
        except (TypeError, ValueError):
            policy_version = 0
        if policy_version < PROBE_SCHEMA_VERSION:
            return False
    return True


def refresh_cached_detected_files(cache_entry):
    if not isinstance(cache_entry, dict):
        return cache_entry, False
    detected_files = [row for row in cache_entry.get("detected_files") or [] if isinstance(row, dict)]
    if not detected_files:
        return cache_entry, False
    review_id = str(cache_entry.get("review_id") or "")
    if not review_id:
        return cache_entry, False
    kept = []
    changed = False
    for detected in detected_files:
        candidate = {
            "filename": detected.get("filename"),
            "path": detected.get("path"),
            "detected_filename": detected.get("filename"),
            "detected_path": detected.get("path"),
        }
        bad_match = bad_candidate_match(review_id, candidate)
        if bad_match:
            changed = True
            continue
        kept.append(detected)
    if not changed:
        return cache_entry, False
    refreshed = dict(cache_entry)
    refreshed["detected_files"] = kept
    refreshed["detected_count"] = len(kept)
    refreshed["detected_file_bad_candidate_filtered_at"] = now()
    refreshed["detected_file_bad_candidate_filtered_at_iso"] = utc_stamp()
    if not kept and str(refreshed.get("status") or "") == "staged_file_ready":
        refreshed["status"] = "available" if int(refreshed.get("candidate_count") or 0) > 0 else "searched_no_candidates"
    return refreshed, True


def refresh_cached_candidate_verdicts(
    cache_entry,
    item=None,
    context_signature=None,
    force=False,
    bad_review_ids=None,
):
    if not isinstance(cache_entry, dict):
        return cache_entry, False
    candidates = [row for row in cache_entry.get("candidates") or [] if isinstance(row, dict)]
    refreshed, detected_changed = refresh_cached_detected_files(cache_entry)
    if not candidates:
        return refreshed, detected_changed
    context_signature = context_signature or auto_grab_context_signature()
    if (
        not force
        and entry_has_current_auto_grab_verdicts(cache_entry)
        and cache_entry.get("auto_grab_context_signature") == context_signature
    ):
        return refreshed, detected_changed
    basis = dict(cache_entry)
    if isinstance(item, dict):
        basis.update({key: value for key, value in item.items() if value not in (None, "")})
    refreshed = copy_item_context(dict(refreshed), basis)
    refreshed_candidates = []
    for candidate in candidates:
        candidate = dict(candidate)
        refreshed_candidates.append(attach_match_explanation(candidate, basis))
    refreshed_candidates = annotate_auto_grab_verdicts(refreshed_candidates, basis)
    review_ids = list(bad_review_ids or [basis.get("review_id")])
    review_ids = list(dict.fromkeys(str(value or "").strip() for value in review_ids if str(value or "").strip()))
    for review_id in review_ids:
        refreshed_candidates = annotate_bad_candidate_verdicts(refreshed_candidates, review_id)
    auto_counts = auto_grab_counts(refreshed_candidates)
    failed_candidate_count = sum(1 for row in refreshed_candidates if (row or {}).get("manual_source_bad_candidate"))
    refreshed["schema_version"] = PROBE_SCHEMA_VERSION
    refreshed["auto_grab_context_signature"] = context_signature
    refreshed["candidates"] = refreshed_candidates
    refreshed["candidate_count"] = len(refreshed_candidates)
    refreshed["failed_candidate_count"] = failed_candidate_count
    refreshed["auto_grab_safe_count"] = auto_counts["auto_grab_safe"]
    refreshed["auto_grab_review_count"] = auto_counts["needs_review"]
    refreshed["auto_grab_blocked_count"] = auto_counts["blocked"]
    refreshed["auto_grab_verdict_refreshed_at"] = now()
    refreshed["auto_grab_verdict_refreshed_at_iso"] = utc_stamp()
    if refreshed_candidates and str(refreshed.get("status") or "") in {"searched_no_candidates", "no_query"}:
        refreshed["status"] = "available"
    return refreshed, True


def verdict_refresh_budget_seconds(probe_budget_seconds):
    """Seconds this pass may spend reconciling cached verdicts before searching.

    Reconciliation is bookkeeping; searching is the work. Give it a slice of the
    pass and no more, so a large or fully-invalidated cache can never spend the
    whole provider window before the first query goes out.
    """
    try:
        budget = float(probe_budget_seconds or 0)
    except (TypeError, ValueError):
        budget = 0.0
    return max(5.0, min(15.0, budget * 0.25))


def refresh_cache_candidate_verdicts(cache, items, deadline=None):
    if not isinstance(cache, dict):
        return {}, 0, 0
    item_by_review_id = {
        str(item.get("review_id") or ""): item
        for item in items or []
        if isinstance(item, dict) and item.get("review_id")
    }
    context_signature = auto_grab_context_signature()
    refreshed_count = 0
    skipped_count = 0
    # Only reconcile rows in the active probe window.  Production caches can
    # contain years of inactive searches; walking and re-scoring every cached
    # candidate here can consume the entire bounded provider window before the
    # first network query is attempted.
    #
    # The row window alone is not a bound.  The context signature folds in the
    # auto-grab learning data, which every completed transfer rewrites, so a
    # normal production pass finds *every* cached verdict stale and re-scores
    # the whole window.  Measured on the live cache that is ~118s of work
    # against a 90s subprocess timeout: the probe was killed mid-reconcile,
    # every pass, having issued no query and written no cache.  Walk the rows
    # the probe is most likely to select first and stop at the deadline.
    #
    # Within a priority bucket, take the least-recently reconciled row first.
    # Probe priority alone is stable, so a fixed order would hand the budget to
    # the same head every pass and never reconcile the tail -- the rows holding
    # candidates sort last and are exactly the expensive ones. Oldest-first
    # rotates the whole window across passes instead of starving it.
    def refresh_order(entry):
        review_id, item = entry
        cached = cache.get(review_id)
        try:
            reconciled_at = float((cached or {}).get("auto_grab_verdict_refreshed_at") or 0)
        except (TypeError, ValueError):
            reconciled_at = 0.0
        return (item_probe_priority(item, cache), reconciled_at)

    ordered = sorted(item_by_review_id.items(), key=refresh_order)
    for review_id, item in ordered:
        entry = cache.get(review_id)
        if not isinstance(entry, dict):
            continue
        if deadline is not None and now() >= deadline:
            skipped_count += 1
            continue
        refreshed, changed = refresh_cached_candidate_verdicts(entry, item=item, context_signature=context_signature)
        if changed:
            cache[review_id] = refreshed
            refreshed_count += 1
    return cache, refreshed_count, skipped_count


def cache_refresh_reason(cache_entry, queries=None):
    if not cache_entry:
        return "not_checked"
    status = str(cache_entry.get("status") or "")
    if queries and status in {"searched_no_candidates", "no_query"}:
        current_signature = query_signature(queries)
        if cache_entry.get("query_signature") != current_signature:
            return "query_plan_changed"
    if int(cache_entry.get("candidate_count") or 0) > 0 and not entry_has_current_auto_grab_verdicts(cache_entry):
        return "upgrade_autograb_verdicts"
    if int(cache_entry.get("schema_version") or 0) < PROBE_SCHEMA_VERSION:
        if (
            int(cache_entry.get("candidate_count") or 0) <= 0
            and int(cache_entry.get("detected_count") or 0) <= 0
            and status in {"searched_no_candidates", "no_query"}
        ):
            return ""
        if int(cache_entry.get("response_count") or 0) > 0 and not entry_has_rejection_summary(cache_entry):
            return "upgrade_rejection_summary"
        if int(cache_entry.get("candidate_count") or 0) > 0 and not entry_has_match_explanations(cache_entry):
            return "upgrade_match_explanation"
        return "schema_upgrade"
    if status in {"error", "api_error", "provider_unavailable", "provider_wait", "timeout", "probe_error"}:
        return "retry_probe_error"
    if int(cache_entry.get("candidate_count") or 0) > 0 and int(cache_entry.get("detected_count") or 0) <= 0:
        try:
            checked_at = float(cache_entry.get("checked_at") or 0)
        except (TypeError, ValueError):
            checked_at = 0
        if checked_at <= now() - CANDIDATE_RECHECK_SECONDS:
            return "candidate_recheck"
    return ""


def cache_entry_is_current(cache_entry):
    if not isinstance(cache_entry, dict):
        return False
    try:
        return int(cache_entry.get("schema_version") or 0) >= PROBE_SCHEMA_VERSION
    except (TypeError, ValueError):
        return False


def cache_entry_is_active(cache_entry):
    if not cache_entry_is_current(cache_entry):
        return False
    if int(cache_entry.get("candidate_count") or 0) > 0 and not entry_has_current_auto_grab_verdicts(cache_entry):
        return False
    try:
        checked_at = float((cache_entry or {}).get("checked_at") or 0)
    except (TypeError, ValueError):
        checked_at = 0
    if checked_at <= now() - ACTIVE_CACHE_SECONDS:
        return False
    if int(cache_entry.get("candidate_count") or 0) > 0 and int(cache_entry.get("detected_count") or 0) <= 0:
        return checked_at > now() - CANDIDATE_HEADLINE_SECONDS
    return True


def retain_cached_candidates_after_empty_retry(previous_entry, fresh_entry, item):
    if not isinstance(previous_entry, dict) or not isinstance(fresh_entry, dict):
        return None
    refreshed_previous, _ = refresh_cached_candidate_verdicts(previous_entry, item=item)
    try:
        previous_candidates = int(refreshed_previous.get("candidate_count") or 0)
    except (TypeError, ValueError):
        previous_candidates = 0
    try:
        fresh_candidates = int(fresh_entry.get("candidate_count") or 0)
    except (TypeError, ValueError):
        fresh_candidates = 0
    try:
        fresh_detected = int(fresh_entry.get("detected_count") or 0)
    except (TypeError, ValueError):
        fresh_detected = 0
    if previous_candidates <= 0 or fresh_candidates > 0 or fresh_detected > 0:
        return None
    try:
        previous_checked_at = float(previous_entry.get("checked_at") or 0)
    except (TypeError, ValueError):
        previous_checked_at = 0
    if previous_checked_at <= now() - ACTIVE_CACHE_SECONDS:
        return None
    retained = copy_item_context(dict(refreshed_previous), item)
    retained["status"] = "available"
    retained["checked_at"] = fresh_entry.get("checked_at") or now()
    retained["checked_at_iso"] = fresh_entry.get("checked_at_iso") or utc_stamp(retained["checked_at"])
    retained["detected_count"] = fresh_detected
    retained["detected_files"] = fresh_entry.get("detected_files") if isinstance(fresh_entry.get("detected_files"), list) else []
    if fresh_entry.get("staged_scan_at"):
        retained["staged_scan_at"] = fresh_entry.get("staged_scan_at")
        retained["staged_scan_at_iso"] = fresh_entry.get("staged_scan_at_iso") or utc_stamp(fresh_entry.get("staged_scan_at"))
    retained["probe_reason"] = "retained_cached_candidates_after_empty_retry"
    retained["fresh_empty_retry_probe"] = {
        "status": fresh_entry.get("status"),
        "candidate_count": fresh_entry.get("candidate_count"),
        "detected_count": fresh_entry.get("detected_count"),
        "checked_at": fresh_entry.get("checked_at"),
        "checked_at_iso": fresh_entry.get("checked_at_iso"),
        "query_signature": fresh_entry.get("query_signature"),
    }
    retained["retained_cached_candidates_at"] = now()
    retained["retained_cached_candidates_at_iso"] = utc_stamp()
    return retained


def sibling_cached_candidates_after_empty_retry(cache, review_id, fresh_entry, item):
    if not isinstance(cache, dict) or not isinstance(fresh_entry, dict):
        return None
    try:
        fresh_candidates = int(fresh_entry.get("candidate_count") or 0)
    except (TypeError, ValueError):
        fresh_candidates = 0
    try:
        fresh_detected = int(fresh_entry.get("detected_count") or 0)
    except (TypeError, ValueError):
        fresh_detected = 0
    if fresh_candidates > 0 or fresh_detected > 0:
        return None
    item_issue_keys = set(issue_identity_keys(item))
    item_loose = {
        (normalize((item or {}).get("series") or (item or {}).get("query") or ""), issue)
        for issue in issue_number_keys((item or {}).get("issue"))
    }
    best = None
    for key, entry in cache.items():
        if str(key) == str(review_id) or not isinstance(entry, dict):
            continue
        try:
            candidate_count = int(entry.get("candidate_count") or 0)
        except (TypeError, ValueError):
            candidate_count = 0
        if candidate_count <= 0:
            continue
        try:
            checked_at = float(entry.get("checked_at") or 0)
        except (TypeError, ValueError):
            checked_at = 0
        if checked_at <= now() - ACTIVE_CACHE_SECONDS:
            continue
        entry_issue_keys = set(issue_identity_keys(entry))
        entry_loose = {
            (normalize(entry.get("series") or entry.get("query") or ""), issue)
            for issue in issue_number_keys(entry.get("issue"))
        }
        if not (item_issue_keys & entry_issue_keys or item_loose & entry_loose):
            continue
        refreshed, _ = refresh_cached_candidate_verdicts(entry, item=item)
        if int(refreshed.get("candidate_count") or 0) <= 0:
            continue
        if best is None or active_cache_rank(refreshed) > active_cache_rank(best):
            best = refreshed
    if not best:
        return None
    retained = copy_item_context(dict(best), item)
    retained["review_id"] = str(review_id)
    retained["status"] = "available"
    retained["checked_at"] = fresh_entry.get("checked_at") or now()
    retained["checked_at_iso"] = fresh_entry.get("checked_at_iso") or utc_stamp(retained["checked_at"])
    retained["detected_count"] = fresh_detected
    retained["detected_files"] = fresh_entry.get("detected_files") if isinstance(fresh_entry.get("detected_files"), list) else []
    if fresh_entry.get("staged_scan_at"):
        retained["staged_scan_at"] = fresh_entry.get("staged_scan_at")
        retained["staged_scan_at_iso"] = fresh_entry.get("staged_scan_at_iso") or utc_stamp(fresh_entry.get("staged_scan_at"))
    retained["probe_reason"] = "retained_sibling_cached_candidates_after_empty_retry"
    retained["fresh_empty_retry_probe"] = {
        "status": fresh_entry.get("status"),
        "candidate_count": fresh_entry.get("candidate_count"),
        "detected_count": fresh_entry.get("detected_count"),
        "checked_at": fresh_entry.get("checked_at"),
        "checked_at_iso": fresh_entry.get("checked_at_iso"),
        "query_signature": fresh_entry.get("query_signature"),
    }
    retained["retained_cached_candidates_at"] = now()
    retained["retained_cached_candidates_at_iso"] = utc_stamp()
    return retained


def probe_issue_key(entry):
    if not isinstance(entry, dict):
        return ""
    series = normalize(entry.get("series") or "")
    issue = normalize(entry.get("issue") or "")
    identity = (
        entry.get("autopilot_queue_key")
        or entry.get("queue_identity")
        or entry.get("comicvine_id")
        or ""
    )
    return f"{series}|{issue or entry.get('review_id') or ''}|{identity}"


def issue_identity_keys(entry):
    if not isinstance(entry, dict):
        return []
    series = normalize(entry.get("series") or entry.get("query") or "")
    if not series:
        return []
    issue_keys = issue_number_keys(entry.get("issue"))
    identities = identity_values_for_item(entry)
    keys = []
    for issue in issue_keys:
        if not issue:
            continue
        for identity in identities:
            keys.append((series, issue, str(identity)))
    return list(dict.fromkeys(keys))


def canonical_retarget_unit(entry):
    entry = entry if isinstance(entry, dict) else {}
    if entry.get("autopilot_queue"):
        unit_type = str(entry.get("unit_type") or "").strip().lower()
        aliases = {
            "vol": "volume", "book_volume": "volume", "manga_volume": "volume",
            "manga_chapter": "chapter", "comic_issue": "issue",
        }
        unit_type = aliases.get(unit_type, unit_type)
        if unit_type in {"issue", "chapter", "volume"}:
            unit_number = str(entry.get(f"{unit_type}_number") or "").strip()
            return (unit_type, unit_number) if unit_number else ("", "")
        return "", ""
    if inkdrop_candidate_matching:
        target = inkdrop_candidate_matching.target_context(entry)
        unit_type = str(target.get("unit_type") or "").strip().lower()
        if unit_type in {"volume", "vol", "book_volume", "manga_volume"}:
            return "volume", str(target.get("volume_number") or "")
        if unit_type in {"chapter", "manga_chapter"}:
            return "chapter", str(target.get("chapter_number") or "")
        if unit_type in {"issue", "comic_issue"}:
            return "issue", str(target.get("issue_number") or "")
    issue = str(entry.get("issue_number") or entry.get("issue") or "").strip()
    return ("issue", issue) if issue else ("", "")


def retarget_edition_evidence(entry):
    entry = entry if isinstance(entry, dict) else {}
    values = {
        normalize(entry.get(key))
        for key in ("edition_id", "edition_marker", "edition")
        if normalize(entry.get(key))
    }
    if inkdrop_candidate_matching:
        for value in (entry.get("series"), entry.get("query")):
            parsed = inkdrop_candidate_matching.parse_release_title(value)
            values.update(
                str(marker or "").strip().lower()
                for marker in (parsed.get("edition_markers") or [parsed.get("edition_marker")])
                if str(marker or "").strip()
            )
    return values


def cached_review_target_matches_item(cached, item):
    if not isinstance(cached, dict) or not isinstance(item, dict):
        return False
    if normalize(cached.get("series") or cached.get("query")) != normalize(item.get("series") or item.get("query")):
        return False
    cached_unit = canonical_retarget_unit(cached)
    item_unit = canonical_retarget_unit(item)
    if not cached_unit[0] or not cached_unit[1] or cached_unit != item_unit:
        return False
    matched_durable_id = False
    for field in ("queue_identity", "series_id", "watch_id", "comicvine_id"):
        cached_value = str(cached.get(field) or "").strip()
        item_value = str(item.get(field) or "").strip()
        if cached_value and item_value:
            if cached_value != item_value:
                return False
            matched_durable_id = True
    if not matched_durable_id:
        return False
    cached_editions = retarget_edition_evidence(cached)
    item_editions = retarget_edition_evidence(item)
    if cached_editions and item_editions and cached_editions != item_editions:
        return False
    for fields in (("publisher", "watch_publisher"), ("year", "watch_year")):
        cached_value = next((normalize(cached.get(field)) for field in fields if normalize(cached.get(field))), "")
        item_value = next((normalize(item.get(field)) for field in fields if normalize(item.get(field))), "")
        if cached_value and item_value and cached_value != item_value:
            return False
    return True


def current_queue_item_for_cached_review_id(review_id, items, cache):
    """Resolve a rotated review id only with unique durable issue identity proof."""

    cached = (cache or {}).get(str(review_id or "")) if isinstance(cache, dict) else None
    if not isinstance(cached, dict):
        return None
    matches = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if not (item.get("autopilot_queue") or str(item.get("source") or "") == "series_autopilot_queue"):
            continue
        if str(item.get("autopilot_state") or "queued") in {"downloading", "importing", "verified", "needs_you"}:
            continue
        if item.get("present_in_watch") is False:
            continue
        if cached_review_target_matches_item(cached, item):
            matches.append(item)
    if len(matches) != 1:
        return None
    refreshed, _changed = refresh_cached_candidate_verdicts(
        cached,
        item=matches[0],
        force=True,
        bad_review_ids=[cached.get("review_id") or review_id, matches[0].get("review_id")],
    )
    cache[str(review_id)] = refreshed
    return matches[0] if ranked_auto_grab_candidates(refreshed) else None


def active_cache_rank(entry):
    if not isinstance(entry, dict):
        return (0, 0, 0, 0, 0)
    queue_backed = 1 if (entry.get("autopilot_queue") or str(entry.get("source") or "") == "series_autopilot_queue") else 0
    try:
        safe_count = int(entry.get("auto_grab_safe_count") or 0)
    except (TypeError, ValueError):
        safe_count = 0
    try:
        candidate_count = int(entry.get("candidate_count") or 0)
    except (TypeError, ValueError):
        candidate_count = 0
    try:
        detected_count = int(entry.get("detected_count") or 0)
    except (TypeError, ValueError):
        detected_count = 0
    try:
        checked_at = float(entry.get("checked_at") or 0)
    except (TypeError, ValueError):
        checked_at = 0
    return (queue_backed, safe_count, candidate_count, detected_count, checked_at)


def auto_grab_scope_from_active_cache(active_cache, selected_review_ids, eligible_review_ids=None):
    if not isinstance(active_cache, dict):
        return {}, [], 0
    selected = {str(value) for value in selected_review_ids or [] if str(value)}
    eligible = {str(value) for value in eligible_review_ids or [] if str(value)}
    scoped = {}
    cached_candidate_count = 0
    for review_id, entry in active_cache.items():
        rid = str(review_id or "")
        if not rid or not isinstance(entry, dict):
            continue
        try:
            candidate_count = int(entry.get("candidate_count") or 0)
        except (TypeError, ValueError):
            candidate_count = 0
        include_cached = candidate_count > 0 and (not eligible or rid in eligible)
        if rid in selected or include_cached:
            scoped[rid] = entry
            if rid not in selected and include_cached:
                cached_candidate_count += 1
    return scoped, sorted(scoped), cached_candidate_count


def backfill_queue_context_for_active_cache(active_cache, all_items):
    if not isinstance(active_cache, dict):
        return {}
    queue_items = [
        item for item in all_items or []
        if isinstance(item, dict) and item.get("autopilot_queue")
    ]
    existing_review_ids = {str(item.get("review_id") or "") for item in queue_items if isinstance(item, dict)}
    for item in load_queue_context_review_items():
        rid = str(item.get("review_id") or "")
        if rid and rid in existing_review_ids:
            continue
        queue_items.append(item)
        if rid:
            existing_review_ids.add(rid)
    if not queue_items:
        return active_cache
    exact = {}
    loose = {}
    for item in queue_items:
        for key in issue_identity_keys(item):
            exact.setdefault(key, item)
        series = normalize(item.get("series") or item.get("query") or "")
        for issue in issue_number_keys(item.get("issue")):
            loose.setdefault((series, issue), []).append(item)

    def queue_match(entry):
        for key in issue_identity_keys(entry):
            if key in exact:
                return exact[key]
        series = normalize(entry.get("series") or entry.get("query") or "")
        for issue in issue_number_keys(entry.get("issue")):
            candidates = loose.get((series, issue)) or []
            unique = {
                str(candidate.get("review_id") or ""): candidate
                for candidate in candidates
                if candidate.get("review_id")
            }
            if len(unique) == 1:
                return next(iter(unique.values()))
        return None

    merged_by_issue = {}
    passthrough = {}
    for review_id, entry in active_cache.items():
        entry = dict(entry or {})
        match = queue_match(entry)
        if match and not entry.get("autopilot_queue"):
            entry = copy_item_context(entry, match)
            entry["review_id"] = match.get("review_id") or entry.get("review_id")
            entry["source"] = "series_autopilot_queue"
            entry["autopilot_queue"] = True
            entry["autopilot_queue_context_backfilled"] = True
        key = probe_issue_key(entry)
        if key:
            existing = merged_by_issue.get(key)
            if existing is None or active_cache_rank(entry) > active_cache_rank(existing):
                merged_by_issue[key] = entry
        else:
            passthrough[str(entry.get("review_id") or review_id)] = entry

    out = {}
    for entry in merged_by_issue.values():
        out[str(entry.get("review_id") or probe_issue_key(entry))] = entry
    out.update(passthrough)
    return out


def item_probe_priority(item, cache):
    review_id = item.get("review_id")
    cache_entry = cache.get(review_id) if isinstance(cache, dict) else None
    refresh_reason = cache_refresh_reason(cache_entry, source_queries(item))
    if not cache_entry:
        bucket = 0
    elif refresh_reason:
        bucket = 1
    elif int(cache_entry.get("detected_count") or 0) > 0:
        bucket = 4
    elif int(cache_entry.get("candidate_count") or 0) > 0:
        bucket = 4
    else:
        bucket = 2
    checked_at = float((cache_entry or {}).get("checked_at") or 0)
    return (
        bucket,
        checked_at if bucket == 2 else 0,
        normalize(item.get("series") or item.get("query")),
        token_number(item.get("issue")) or 999999,
        str(item.get("issue") or ""),
    )


def select_probe_items(items, cache, max_total, max_per_series):
    if int(max_total or 0) <= 0:
        return []
    selected = []
    per_series = {}
    for item in sorted(items, key=lambda row: item_probe_priority(row, cache)):
        series_key = normalize(item.get("series") or item.get("query"))
        if per_series.get(series_key, 0) >= max_per_series:
            continue
        selected.append(item)
        per_series[series_key] = per_series.get(series_key, 0) + 1
        if len(selected) >= max_total:
            break
    return selected


def run(args):
    apply_quality_language_rules()
    SERIES_RUN_EPHEMERAL_CANDIDATES.clear()
    started_at = now()
    probe_budget_seconds = max(30, int(getattr(args, "probe_budget_seconds", DEFAULT_PROBE_BUDGET_SECONDS) or DEFAULT_PROBE_BUDGET_SECONDS))
    cache = read_json(CACHE_FILE, {}) or {}
    review_id_filter = str(getattr(args, "review_id", "") or "").strip()
    publish_probe_progress(
        dry_run=bool(args.dry_run),
        review_id_filter=review_id_filter,
        current_stage="loading rows",
        probe_budget_seconds=probe_budget_seconds,
        probe_elapsed_seconds=0,
    )
    if args.series:
        all_items = combine_source_review_items(
            load_source_review_items(limit=max(args.max_total * 4, 300)),
            load_queue_source_review_items(limit=max(args.max_total * 4, 300)),
        )
        items = combine_source_review_items(
            load_source_review_items(limit=max(args.max_total * 4, 80), series=args.series),
            load_queue_source_review_items(limit=max(args.max_total * 4, 80), series=args.series),
        )
    else:
        items = combine_source_review_items(
            load_source_review_items(limit=max(args.max_total * 4, 300)),
            load_queue_source_review_items(limit=max(args.max_total * 4, 300)),
        )
        all_items = items
    if review_id_filter:
        scoped = [item for item in items if str(item.get("review_id") or "") == review_id_filter]
        if not scoped:
            scoped = [item for item in all_items if str(item.get("review_id") or "") == review_id_filter]
        if not scoped:
            alias_item = current_queue_item_for_cached_review_id(review_id_filter, all_items, cache)
            if alias_item:
                scoped = [alias_item]
                log(
                    "review_filter_retargeted_to_current_queue_item",
                    cached_review_id=review_id_filter,
                    current_review_id=alias_item.get("review_id"),
                    series=alias_item.get("series"),
                    issue=alias_item.get("issue"),
                )
        items = scoped
    cache, refreshed_cached_verdict_count, skipped_cached_verdict_count = refresh_cache_candidate_verdicts(
        cache,
        all_items,
        deadline=started_at + verdict_refresh_budget_seconds(probe_budget_seconds),
    )
    if skipped_cached_verdict_count:
        log(
            "verdict_refresh_budget_reached",
            refreshed=refreshed_cached_verdict_count,
            deferred=skipped_cached_verdict_count,
            budget_seconds=round(verdict_refresh_budget_seconds(probe_budget_seconds), 1),
        )
    active_scope_items = items if (args.series or review_id_filter) else all_items
    active_review_ids = {str(item.get("review_id") or "") for item in active_scope_items}
    for item in all_items:
        review_id = str(item.get("review_id") or "")
        if review_id and isinstance(cache.get(review_id), dict):
            cache[review_id] = copy_item_context(cache[review_id], item)
    pre_probe_cache = {
        str(key): dict(value)
        for key, value in cache.items()
        if isinstance(value, dict)
    }
    selected = select_probe_items(items, cache, args.max_total, args.max_per_series)
    selected_review_ids = {
        str(item.get("review_id") or "")
        for item in selected
        if str(item.get("review_id") or "")
    }
    queue_review_rows = {"ensured": 0, "already_present": 0, "skipped": 0, "persisted": False}
    queue_backed_selected_count = sum(1 for item in selected if item.get("autopilot_queue"))
    publish_probe_progress(
        dry_run=bool(args.dry_run),
        review_id_filter=review_id_filter,
        current_stage="selected rows",
        selected_count=len(selected),
        queue_backed_selected_count=queue_backed_selected_count,
        probe_budget_seconds=probe_budget_seconds,
        probe_elapsed_seconds=round(now() - started_at, 1),
    )
    if not args.dry_run:
        queue_review_rows = ensure_queue_review_rows(selected)

    if args.dry_run:
        result = {
            "ok": True,
            "dry_run": True,
            "schema_version": PROBE_SCHEMA_VERSION,
            "state": "finished",
            "status": "finished",
            "started_at": started_at,
            "started_at_iso": utc_stamp(started_at),
            "generated_at": now(),
            "generated_at_iso": utc_stamp(),
            "probe_budget_seconds": probe_budget_seconds,
            "probe_elapsed_seconds": round(now() - started_at, 1),
            "review_id_filter": review_id_filter,
            "selected_count": len(selected),
            "selected_review_ids": sorted(selected_review_ids),
            "queue_backed_selected_count": queue_backed_selected_count,
            "refreshed_cached_verdict_count": refreshed_cached_verdict_count,
            "selected": [
                {
                    "review_id": item.get("review_id"),
                    "series": item.get("series"),
                    "issue": item.get("issue"),
                    "priority": cache_refresh_reason(
                        cache.get(item.get("review_id")),
                        source_queries(item),
                    ) or "scheduled",
                    "queries": source_queries(item),
                    "query_offset": derive_query_offset(
                        source_queries(item),
                        cache.get(item.get("review_id")),
                        cache_refresh_reason(
                            cache.get(item.get("review_id")),
                            source_queries(item),
                        ),
                        force=args.force,
                    ),
                    "next_queries": rotated_query_batch(
                        source_queries(item),
                        max_queries=args.max_queries,
                        offset=derive_query_offset(
                            source_queries(item),
                            cache.get(item.get("review_id")),
                            cache_refresh_reason(
                                cache.get(item.get("review_id")),
                                source_queries(item),
                            ),
                            force=args.force,
                        ),
                        include_anchor=not retry_rotates_without_anchor(
                            source_queries(item),
                            cache.get(item.get("review_id")),
                            cache_refresh_reason(
                                cache.get(item.get("review_id")),
                                source_queries(item),
                            ),
                            force=args.force,
                        ),
                    ),
                }
                for item in selected
            ],
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return result

    # The configured probe budget is provider/network time. Loading a large
    # durable cache and reconciling active rows must not spend that budget and
    # silently turn a scheduled search into checked_count=0.
    network_started_at = now()
    deadline = network_started_at + probe_budget_seconds

    checked = []
    skipped = []
    run_directory_observations = []
    run_directory_observation_budget = {"used": 0, "truncated": False}
    run_directory_selection_budget = {"selected_by_review": {}, "selected_bytes": 0}
    budget_exhausted = False
    budget_exhausted_count = 0
    def progress_item(item=None, query=None, attempt_index=None, attempt_total=None, **extra):
        item = item or {}
        publish_probe_progress(
            dry_run=False,
            review_id_filter=review_id_filter,
            current_stage="probing",
            current_review_id=item.get("review_id"),
            current_series=item.get("series"),
            current_issue=item.get("issue"),
            current_query=query,
            current_query_index=attempt_index,
            current_query_total=attempt_total,
            selected_count=len(selected),
            checked_count=len(checked),
            skipped_cooldown_count=len(skipped),
            probe_budget_seconds=probe_budget_seconds,
            probe_elapsed_seconds=round(now() - started_at, 1),
            **extra,
        )

    for index, item in enumerate(selected, start=1):
        remaining = seconds_remaining(deadline)
        if remaining is not None and remaining < 8:
            budget_exhausted = True
            budget_exhausted_count += 1
            skipped.append(str(item.get("review_id") or ""))
            log(
                "probe_budget_exhausted",
                review_id=item.get("review_id"),
                series=item.get("series"),
                issue=item.get("issue"),
                selected_index=index,
                selected_count=len(selected),
            )
            continue
        review_id = item.get("review_id")
        progress_item(item=item, selected_index=index)
        cache_entry = cache.get(review_id)
        queries = source_queries(item)
        refresh_reason = cache_refresh_reason(cache_entry, queries)
        # Only a changed query plan makes the cached cooldown timestamp
        # meaningless. Every other refresh reason previously bypassed
        # cooldown_hours too (bool(refresh_reason) is true for any non-empty
        # string), so a periodic candidate_recheck or a schema/verdict
        # upgrade marker re-searched on every pass regardless of the
        # configured cooldown -- see Wild Strawberry (finding 8).
        if should_skip_cache(cache_entry, args.cooldown_hours, force=args.force or refresh_reason == "query_plan_changed"):
            cache[review_id] = attach_staged_detection(copy_item_context(cache_entry or {"review_id": review_id}, item), item)
            skipped.append(review_id)
            continue
        query_offset = derive_query_offset(queries, cache_entry, refresh_reason, force=args.force)
        include_anchor = not retry_rotates_without_anchor(
            queries,
            cache_entry,
            refresh_reason,
            force=args.force,
        )
        entry = probe_item(
            item,
            wait_seconds=args.wait_seconds,
            max_queries=args.max_queries,
            query_offset=query_offset,
            include_anchor=include_anchor,
            deadline=deadline,
            progress=progress_item,
            directory_observation_sink=run_directory_observations,
            directory_observation_budget=run_directory_observation_budget,
            directory_items=all_items,
        )
        entry["probe_reason"] = refresh_reason or "scheduled"
        if review_id_filter and (args.auto_grab_live or args.auto_grab_dry_run):
            retained_from = "same_review"
            retained = retain_cached_candidates_after_empty_retry(
                pre_probe_cache.get(str(review_id)),
                entry,
                item,
            )
            if not retained:
                retained_from = "sibling_review"
                retained = sibling_cached_candidates_after_empty_retry(
                    pre_probe_cache,
                    review_id,
                    entry,
                    item,
                )
            if retained:
                log(
                    "retry_retained_cached_candidates",
                    review_id=review_id,
                    series=item.get("series"),
                    issue=item.get("issue"),
                    retained_from=retained_from,
                    fresh_status=entry.get("status"),
                    retained_candidate_count=retained.get("candidate_count"),
                )
                entry = retained
        cache[review_id] = entry
        checked.append(entry)
        log("probe_item", review_id=review_id, series=item.get("series"), issue=item.get("issue"), status=entry.get("status"), candidate_count=entry.get("candidate_count"), probe_reason=entry.get("probe_reason"))
        if int((entry.get("series_directory_observation_summary") or {}).get("observed_directory_count") or 0):
            # Reuse a safe cohort before probing another issue in this series.
            immediate_handoff = apply_series_directory_opportunities(
                checked,
                all_items,
                cache,
                deadline=now() + 8,
                observations=run_directory_observations,
                selection_budget=run_directory_selection_budget,
            )
            selected_review_ids.update(immediate_handoff.get("selected_review_ids") or [])

    series_directory_handoff = apply_series_directory_opportunities(
        checked,
        all_items,
        cache,
        # Directory intersection is local, already bounded by issue/file/byte
        # caps, and should not inherit an exhausted network probe deadline.
        deadline=now() + 8,
        observations=run_directory_observations,
        selection_budget=run_directory_selection_budget,
    )
    series_directory_handoff["observation_budget_used"] = int(run_directory_observation_budget.get("used") or 0)
    series_directory_handoff["observation_truncated"] = bool(run_directory_observation_budget.get("truncated"))
    selected_review_ids.update(series_directory_handoff.get("selected_review_ids") or [])
    if series_directory_handoff.get("observed_directory_count"):
        log(
            "slskd_series_directory_handoff",
            observed_directory_count=series_directory_handoff.get("observed_directory_count"),
            observed_file_count=series_directory_handoff.get("observed_file_count"),
            evaluated_issue_count=series_directory_handoff.get("evaluated_issue_count"),
            selected_issue_count=series_directory_handoff.get("selected_issue_count"),
            selected_bytes=series_directory_handoff.get("selected_bytes"),
            deadline_exhausted=series_directory_handoff.get("deadline_exhausted"),
        )

    # A directory that alone already covers the bulk of a series' open run
    # (a real complete-series pack, not a handful of stray matches) gets its
    # own dedicated grab instead of trickling through the paced budget above.
    series_pack_complete_handoff = apply_series_pack_complete_opportunities(
        all_items,
        cache,
        observations=run_directory_observations,
        deadline=now() + 8,
    )
    selected_review_ids.update(series_pack_complete_handoff.get("selected_review_ids") or [])
    if series_pack_complete_handoff.get("eligible_directory_count"):
        log(
            "slskd_series_pack_complete_handoff",
            eligible_directory_count=series_pack_complete_handoff.get("eligible_directory_count"),
            selected_issue_count=series_pack_complete_handoff.get("selected_issue_count"),
            selected_bytes=series_pack_complete_handoff.get("selected_bytes"),
            deadline_exhausted=series_pack_complete_handoff.get("deadline_exhausted"),
        )
    write_json(CACHE_FILE, cache)
    active_cache = {
        key: value
        for key, value in cache.items()
        if key in active_review_ids
        and cache_entry_is_active(value)
    }
    active_cache = backfill_queue_context_for_active_cache(active_cache, all_items)
    queue_context_backfilled_count = 0
    for review_id, entry in active_cache.items():
        if isinstance(entry, dict) and entry.get("autopilot_queue_context_backfilled"):
            cache[str(review_id)] = entry
            queue_context_backfilled_count += 1
    if queue_context_backfilled_count:
        write_json(CACHE_FILE, cache)
    available_items = [
        value for value in active_cache.values()
        if int(value.get("candidate_count") or 0) > 0 or int(value.get("detected_count") or 0) > 0
    ]
    if not args.dry_run and active_cache:
        active_queue_rows = ensure_queue_review_rows(active_cache.values())
        for key in ("ensured", "already_present", "skipped"):
            queue_review_rows[key] = int(queue_review_rows.get(key) or 0) + int(active_queue_rows.get(key) or 0)
        queue_review_rows["persisted"] = bool(queue_review_rows.get("persisted") or active_queue_rows.get("persisted"))
        if active_queue_rows.get("reason"):
            queue_review_rows["reason"] = active_queue_rows.get("reason")
    staged_items = [value for value in active_cache.values() if int(value.get("detected_count") or 0) > 0]
    staged_queue_updates = []
    if not args.dry_run:
        for entry in staged_items:
            detected_files = [row for row in (entry or {}).get("detected_files") or [] if isinstance(row, dict)]
            update = update_autopilot_queue_from_staged_entry(entry, detected_files[0] if detected_files else {})
            staged_queue_updates.append(update)
    provider_wait_items = [
        value for value in active_cache.values()
        if str((value or {}).get("status") or "").strip().lower() in {"api_error", "provider_unavailable", "provider_wait"}
    ]
    provider_wait_queue_updates = []
    if not args.dry_run:
        for entry in provider_wait_items:
            update = record_slskd_provider_wait_attempt(entry)
            provider_wait_queue_updates.append(update)
    available_issue_count = len({probe_issue_key(value) for value in available_items if probe_issue_key(value)})
    candidate_issue_count = len({
        probe_issue_key(value)
        for value in available_items
        if int(value.get("candidate_count") or 0) > 0 and probe_issue_key(value)
    })
    staged_issue_count = len({probe_issue_key(value) for value in staged_items if probe_issue_key(value)})
    auto_counts = {"auto_grab_safe": 0, "needs_review": 0, "blocked": 0}
    auto_failed_count = 0
    for value in available_items:
        row_counts = entry_auto_grab_counts(value)
        for key in auto_counts:
            auto_counts[key] += int(row_counts.get(key) or 0)
        auto_failed_count += int(value.get("failed_candidate_count") or 0)
    result = {
        "ok": True,
        "dry_run": False,
        "schema_version": PROBE_SCHEMA_VERSION,
        "state": "finished",
        "status": "finished",
        "started_at": started_at,
        "started_at_iso": utc_stamp(started_at),
        "generated_at": now(),
        "generated_at_iso": utc_stamp(),
        "probe_budget_seconds": probe_budget_seconds,
        "probe_setup_seconds": round(network_started_at - started_at, 1),
        "probe_elapsed_seconds": round(now() - started_at, 1),
        "probe_budget_exhausted": budget_exhausted,
        "probe_budget_exhausted_count": budget_exhausted_count,
        "review_id_filter": review_id_filter,
        "selected_count": len(selected),
        "selected_review_ids": sorted(selected_review_ids),
        "queue_backed_selected_count": queue_backed_selected_count,
        "refreshed_cached_verdict_count": refreshed_cached_verdict_count,
        "series_directory_handoff": series_directory_handoff,
        "queue_context_backfilled_count": queue_context_backfilled_count,
        "queue_review_rows": queue_review_rows,
        "checked_count": len(checked),
        "skipped_cooldown_count": len(skipped),
        "available_review_count": len(available_items),
        "available_issue_count": available_issue_count,
        "candidate_count": sum(int(value.get("candidate_count") or 0) for value in available_items),
        "candidate_issue_count": candidate_issue_count,
        "auto_grab_safe_count": auto_counts["auto_grab_safe"],
        "auto_grab_review_count": auto_counts["needs_review"],
        "auto_grab_blocked_count": auto_counts["blocked"],
        "auto_grab_failed_count": auto_failed_count,
        "auto_grab_policy": {
            "mode": "best_candidate_autopick",
            "provider_settings_source": SLSKD_PROVIDER_SETTINGS.get("source"),
            "quality_settings_source": QUALITY_LANGUAGE_RULES.get("source"),
            "preferred_language": QUALITY_LANGUAGE_RULES.get("preferred_language"),
            "pdf_allowed": bool(QUALITY_LANGUAGE_RULES.get("pdf_allowed", True)),
            "packs_allowed": bool(QUALITY_LANGUAGE_RULES.get("packs_allowed", True)),
            "provider_configured": bool(SLSKD_BASE_URL),
            "min_score": AUTO_GRAB_MIN_SCORE,
            "direct_match_min_score": AUTO_GRAB_DIRECT_MATCH_MIN_SCORE,
            "retry_min_score_after_failed_candidate": AUTO_GRAB_RETRY_MIN_SCORE,
            "high_confidence_score": AUTO_GRAB_HIGH_SCORE,
            "medium_confidence_score": AUTO_GRAB_MEDIUM_SCORE,
            "clear_win_delta": AUTO_GRAB_CLEAR_WIN_DELTA,
            "close_score_delta": AUTO_GRAB_CLOSE_SCORE_DELTA,
            "min_bytes": AUTO_GRAB_MIN_BYTES,
            "preferred_exact_min_bytes": SLSKD_PREFERRED_EXACT_MIN_BYTES,
            "inspection_hard_min_bytes": AUTO_INSPECT_HARD_MIN_BYTES,
            "max_bytes": AUTO_GRAB_MAX_BYTES,
            "pack_max_bytes": AUTO_GRAB_PACK_MAX_BYTES,
            "max_attempts_per_review": AUTO_GRAB_MAX_ATTEMPTS_PER_REVIEW,
            "max_recovery_attempts_per_review": AUTO_GRAB_MAX_RECOVERY_ATTEMPTS_PER_REVIEW,
            "max_attempts_per_candidate": AUTO_GRAB_MAX_ATTEMPTS_PER_CANDIDATE,
            "transient_bad_candidate_retry_seconds": TRANSIENT_BAD_CANDIDATE_RETRY_SECONDS,
            "transient_auto_grab_retry_seconds": TRANSIENT_AUTO_GRAB_RETRY_SECONDS,
            "transient_bad_candidate_reasons": sorted(TRANSIENT_BAD_CANDIDATE_REASONS),
            "max_active_per_user": AUTO_GRAB_MAX_ACTIVE_PER_USER,
            "series_run_max_issues": SERIES_RUN_MAX_ISSUES,
            "series_run_max_bytes": SERIES_RUN_MAX_BYTES,
            "series_run_max_observed_files": SERIES_RUN_MAX_OBSERVED_FILES,
            "candidate_limit": AUTO_GRAB_CANDIDATE_LIMIT,
            "extensions": sorted(AUTO_GRAB_EXTENSIONS),
            "archive_pack_extensions": sorted(ARCHIVE_EXTENSIONS),
            "exact_issue_archive_extensions": sorted(AUTO_GRAB_EXACT_ARCHIVE_EXTENSIONS),
            "supplemental_blockers": sorted(SUPPLEMENTAL_AUTOPICK_PHRASES),
        },
        "staged_review_count": len(staged_items),
        "staged_issue_count": staged_issue_count,
        "detected_file_count": sum(int(value.get("detected_count") or 0) for value in staged_items),
        "staged_queue_update_count": sum(1 for row in staged_queue_updates if isinstance(row, dict) and row.get("updated")),
        "staged_queue_updates": staged_queue_updates[:50],
        "provider_wait_review_count": len(provider_wait_items),
        "provider_wait_queue_update_count": sum(1 for row in provider_wait_queue_updates if isinstance(row, dict) and row.get("ok")),
        "provider_wait_queue_updates": provider_wait_queue_updates[:50],
        "items": active_cache,
        "checked": checked,
        "skipped_cooldown": skipped[:100],
        "candidate_recheck_seconds": CANDIDATE_RECHECK_SECONDS,
        "candidate_headline_seconds": CANDIDATE_HEADLINE_SECONDS,
        "policy": "SLSKD auto-grab scores candidates for best-candidate autopick. PDFs and pack-like files can be selected when confidence is good; downloads stay in SLSKD/manual staging and imports still require the existing verified Manual Source autoresolver.",
    }
    if args.auto_grab_live or args.auto_grab_dry_run:
        auto_grab_result = dict(result)
        eligible_auto_grab_review_ids = {
            str(item.get("review_id") or "")
            for item in items
            if str(item.get("review_id") or "")
        }
        scoped_items, scope_review_ids, cached_candidate_count = auto_grab_scope_from_active_cache(
            active_cache,
            selected_review_ids,
            eligible_auto_grab_review_ids,
        )
        auto_grab_result["items"] = scoped_items
        auto_grab_result["auto_grab_scope_review_ids"] = scope_review_ids
        auto_grab_result["auto_grab_cached_candidate_scope_count"] = cached_candidate_count
        result["auto_grab"] = run_auto_grab(args, auto_grab_result)
        if args.auto_grab_live:
            result["policy"] = "SLSKD auto-grab live mode picks the best eligible candidate per row, starts it in SLSKD, marks the row waiting, and imports only through the existing verified Manual Source autoresolver."
    SERIES_RUN_EPHEMERAL_CANDIDATES.clear()
    write_json(STATUS_FILE, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


def main():
    parser = argparse.ArgumentParser(description="Probe slskd/Soulseek availability for InkDrop Manual Source rows without downloading.")
    parser.add_argument("--max-total", type=int, default=None)
    parser.add_argument("--max-per-series", type=int, default=None)
    parser.add_argument("--wait-seconds", type=int, default=None)
    parser.add_argument("--max-queries", type=int, default=None)
    parser.add_argument("--probe-budget-seconds", type=int, default=None, help="Maximum wall time for one SLSKD probe pass before deferring remaining rows.")
    parser.add_argument("--cooldown-hours", type=float, default=None)
    parser.add_argument("--series")
    parser.add_argument("--review-id", help="Restrict probing/autopick to one Manual Review row.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auto-grab-dry-run", action="store_true", help="Audit auto-grab-safe SLSKD candidates without starting downloads.")
    parser.add_argument("--auto-grab-live", action="store_true", help="Start exactly gated SLSKD downloads and mark rows waiting.")
    parser.add_argument("--auto-grab-max", type=int, default=None, help="Maximum auto-grab-safe rows to process when auto-grab is enabled.")
    args = parser.parse_args()
    provider_settings = apply_slskd_provider_settings()
    apply_quality_language_rules()
    args.max_total = max(0, min(int(provider_settings["max_total"] if args.max_total is None else args.max_total), 50))
    args.max_per_series = max(1, min(int(provider_settings["max_per_series"] if args.max_per_series is None else args.max_per_series), 20))
    args.wait_seconds = max(2, min(int(provider_settings["wait_seconds"] if args.wait_seconds is None else args.wait_seconds), 60))
    args.max_queries = max(0, min(int(provider_settings["max_queries"] if args.max_queries is None else args.max_queries), 5))
    args.probe_budget_seconds = max(30, min(int(provider_settings["probe_budget_seconds"] if args.probe_budget_seconds is None else args.probe_budget_seconds), 15 * 60))
    args.cooldown_hours = max(0.0, min(float(provider_settings["cooldown_hours"] if args.cooldown_hours is None else args.cooldown_hours), 24.0 * 30.0))
    args.auto_grab_max = max(0, min(int(provider_settings["auto_grab_max"] if args.auto_grab_max is None else args.auto_grab_max), 10))
    if args.auto_grab_live:
        args.auto_grab_dry_run = False
    run(args)


if __name__ == "__main__":
    main()
