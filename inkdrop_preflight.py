#!/usr/bin/env python3
"""InkDrop startup/preflight checks for Docker-first public installs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

import inkdrop_runtime_config
import inkdrop_public_contracts
import inkdrop_auth


OPTIONAL_ADAPTER_ENV = {
    "comicvine": ("INKDROP_COMICVINE_API_KEY",),
    "kavita": ("INKDROP_KAVITA_URL", "INKDROP_KAVITA_DB"),
    "komga": ("INKDROP_KOMGA_URL",),
    "slskd": ("INKDROP_SLSKD_API_BASE_URL",),
    "suwayomi": ("INKDROP_SUWAYOMI_API_BASE_URL",),
    "prowlarr": ("INKDROP_PROWLARR_URL", "INKDROP_PROWLARR_API_KEY"),
    "sabnzbd": ("INKDROP_SABNZBD_URL", "INKDROP_SABNZBD_API_KEY"),
    "qbittorrent": ("INKDROP_QBITTORRENT_URL",),
}

OPTIONAL_ADAPTER_REQUIRED_ENV = {
    "comicvine": ("INKDROP_COMICVINE_API_KEY",),
    "kavita": ("INKDROP_KAVITA_URL",),
    "komga": ("INKDROP_KOMGA_URL",),
    "slskd": ("INKDROP_SLSKD_API_BASE_URL",),
    "suwayomi": ("INKDROP_SUWAYOMI_API_BASE_URL",),
    "prowlarr": ("INKDROP_PROWLARR_URL", "INKDROP_PROWLARR_API_KEY"),
    "sabnzbd": ("INKDROP_SABNZBD_URL", "INKDROP_SABNZBD_API_KEY"),
    "qbittorrent": ("INKDROP_QBITTORRENT_URL",),
}

OPTIONAL_ADAPTER_EXISTING_PATH_ENV = {
    "kavita": ("INKDROP_KAVITA_DB",),
}

REQUIRED_PYTHON_MODULES = {
    "requests": "requests",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "lxml": "lxml",
    "PIL": "Pillow",
    "py7zr": "py7zr",
    "rarfile": "rarfile",
}

OPTIONAL_RUNTIME_ROOTS = {"staging_dir", "manual_inbox_dir", "quarantine_dir"}

CONFIG_ENV_KEYS = (
    "INKDROP_HOST",
    "INKDROP_PORT",
    "INKDROP_HOST_PORT",
    "INKDROP_VERSION",
    "INKDROP_COMMIT_SHA",
    "INKDROP_BUILD_DATE",
    "INKDROP_RELEASE_CHANNEL",
    "INKDROP_QA_BUILD_NUMBER",
    "INKDROP_CANDIDATE_MANIFEST_PATH",
    "INKDROP_WEB_BASE_URL",
    "INKDROP_CONFIG_DIR",
    "INKDROP_STATE_DIR",
    "INKDROP_LOCK_DIR",
    "INKDROP_LOG_DIR",
    "INKDROP_CACHE_DIR",
    "INKDROP_BACKUP_DIR",
    "INKDROP_STAGING_DIR",
    "INKDROP_MANUAL_INBOX_DIR",
    "INKDROP_QUARANTINE_DIR",
    "INKDROP_COMIC_ROOT",
    "INKDROP_MANGA_ROOT",
    "INKDROP_MANUAL_COMICS_INBOX",
    "INKDROP_MANUAL_EBOOKS_INBOX",
    "INKDROP_SLSKD_DOWNLOAD_ROOT",
    "INKDROP_UNMATCHED_DOWNLOAD_ROOT",
    "INKDROP_DIRECT_DOWNLOAD_ROOT",
    "INKDROP_PACK_DOWNLOAD_ROOT",
    "INKDROP_PACK_TEMP_DOWNLOAD_ROOT",
    "INKDROP_EBOOK_DOWNLOAD_ROOT",
    "INKDROP_SUWAYOMI_STAGING_ROOT",
    "INKDROP_SUWAYOMI_API_BASE_URL",
    "INKDROP_SOURCE_WORKER_STAGING_ROOT",
    "INKDROP_UNMATCHED_QUARANTINE_ROOT",
    "INKDROP_MANAGED_DUPLICATE_QUARANTINE_ROOT",
    "INKDROP_PACK_DUPLICATE_QUARANTINE_ROOT",
    "INKDROP_PACK_REVIEW_QUARANTINE_ROOT",
    "INKDROP_COMIC_INCOMING_ROOT",
    "INKDROP_EBOOK_INCOMING_ROOT",
    "INKDROP_PROTOCOL_ORDER",
    "INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED",
    "INKDROP_MISSING_RECOVERY_ENABLED",
    "INKDROP_MISSING_RECOVERY_MAX_PER_COHORT",
    "INKDROP_MISSING_RECOVERY_MAX_HANDOFFS_PER_HOUR",
    "INKDROP_MISSING_RECOVERY_MAX_BYTES_PER_HOUR",
    "INKDROP_MISSING_RECOVERY_MAX_BYTES_PER_DAY",
    "INKDROP_MISSING_RECOVERY_MIN_STAGING_FREE_BYTES",
    "INKDROP_MISSING_RECOVERY_PAUSE_AFTER_FAILURES",
    "INKDROP_MISSING_RECOVERY_QUIET_HOURS",
    "INKDROP_QUEUE_RUNNER_IMPORT_PRIORITY_READY_IMPORTS",
    "INKDROP_SERIES_AUTOPILOT_MAX_RUN_SECONDS",
    "INKDROP_SERIES_AUTOPILOT_OUTER_TIMEOUT_SECONDS",
    "INKDROP_SERIES_AUTOPILOT_TIMEOUT_KILL_AFTER_SECONDS",
    "INKDROP_SERIES_AUTOPILOT_PROTECTED_MINUTES",
    "INKDROP_SERIES_AUTOPILOT_PROTECTED_RESERVE_SECONDS",
    "INKDROP_AUTOPILOT_RUNTIME_HARD_GRACE_SECONDS",
    "INKDROP_IMPORT_READY_QUEUE_ONLY",
    "INKDROP_IMPORT_READY_IMPORT_TIMEOUT_SECONDS",
    "INKDROP_IMPORT_READY_BATCH_TIMEOUT_SECONDS",
    "INKDROP_PACK_MANIFEST_CACHE_SECONDS",
    "INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS",
    "INKDROP_MANGA_COMPLETION_BACKFILL_LIMIT",
    "INKDROP_PACK_PROBE_SCAN_SECONDS",
    "INKDROP_PACK_PROBE_SCAN_ENTRIES",
    "INKDROP_STATE_ENDPOINT_CONCURRENCY",
    "INKDROP_WEB_SOCKET_TIMEOUT_SECONDS",
    "INKDROP_DEBUG_ACTIVE_REQUESTS",
    "INKDROP_CONTAINER_SCHEDULER_ENABLED",
    "INKDROP_CONTAINER_WEB_BASE_URL",
    "INKDROP_WORKER_API_KEY",
    "INKDROP_WORKER_STATUS_FILE",
    "INKDROP_SCHEDULER_MAX_CONCURRENCY",
    "INKDROP_SCHEDULER_HEARTBEAT_SECONDS",
    "INKDROP_SCHEDULER_FAILURE_BACKOFF_MAX_SECONDS",
    "INKDROP_WORKER_HEALTH_MAX_AGE_SECONDS",
    "INKDROP_WORKER_HEALTH_MAX_LATENESS_SECONDS",
    "INKDROP_WORKER_CRITICAL_FAILURE_THRESHOLD",
    "INKDROP_SCHEDULER_STATUS_REFRESH_INTERVAL_SECONDS",
    "INKDROP_SCHEDULER_QUEUE_MAINTENANCE_INTERVAL_SECONDS",
    "INKDROP_SCHEDULER_QUEUE_MAINTENANCE_TIMEOUT_SECONDS",
    "INKDROP_SCHEDULER_COMPLETED_IMPORT_COMICS_INTERVAL_SECONDS",
    "INKDROP_SCHEDULER_IMPORT_READY_INTERVAL_SECONDS",
    "INKDROP_SCHEDULER_SERIES_AUTOPILOT_INTERVAL_SECONDS",
    "INKDROP_SCHEDULER_SOURCE_WORKER_INTERVAL_SECONDS",
    "INKDROP_SCHEDULER_SUWAYOMI_WORKER_INTERVAL_SECONDS",
    "INKDROP_SCHEDULER_SLSKD_SEARCH_CLEANUP_INTERVAL_SECONDS",
    "INKDROP_SCHEDULER_FULL_RECONCILIATION_INTERVAL_SECONDS",
    "INKDROP_AUTH_MODE",
    "INKDROP_AUTH_ALLOW_DISABLED",
    "INKDROP_TRUSTED_LAN_TESTING",
    "INKDROP_AUTH_COOKIE_SECURE",
    "INKDROP_PASSWORD_MIN_LENGTH",
    "INKDROP_AUTH_SESSION_TTL_SECONDS",
    "INKDROP_AUTH_ALLOWED_ORIGINS",
    "INKDROP_AUTH_REQUIRED",
    "INKDROP_EXTERNAL_AUTH_ENABLED",
    "INKDROP_EXTERNAL_AUTH_HEADER",
    "INKDROP_EXTERNAL_AUTH_GROUP_HEADER",
    "INKDROP_EXTERNAL_AUTH_ADMIN_GROUP",
    "INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES",
    "INKDROP_KAVITA_URL",
    "INKDROP_KAVITA_DB",
    "INKDROP_KAVITA_COMIC_ROOT",
    "INKDROP_KAVITA_MANGA_ROOT",
    "INKDROP_KOMGA_URL",
    "INKDROP_SLSKD_API_BASE_URL",
    "INKDROP_SLSKD_API_KEY",
    "INKDROP_SLSKD_WEB_URL",
    "INKDROP_SLSKD_CONFIG",
    "INKDROP_SLSKD_SEARCH_HISTORY_CLEANUP_ENABLED",
    "INKDROP_SLSKD_SEARCH_HISTORY_KEEP",
    "INKDROP_SLSKD_SEARCH_HISTORY_MAX_DELETE",
    "INKDROP_SLSKD_SEARCH_HISTORY_MIN_AGE_MINUTES",
    "INKDROP_SLSKD_SEARCH_MIN_INTERVAL_SECONDS",
    "INKDROP_SLSKD_SEARCH_MAX_PER_HOUR",
    "INKDROP_PROWLARR_URL",
    "INKDROP_PROWLARR_CONFIG",
    "INKDROP_PROWLARR_DB",
    "INKDROP_PROWLARR_PUBLIC_BASE_URL",
    "INKDROP_PROWLARR_INTERNAL_BASE_URLS",
    "INKDROP_SOURCE_WORKER_PROWLARR_ALLOWED_HOSTS",
    "INKDROP_TRUSTED_PROWLARR_HOSTS",
    "INKDROP_MYLAR_CONFIG",
    "INKDROP_SABNZBD_URL",
    "INKDROP_QBITTORRENT_URL",
    "INKDROP_QBITTORRENT_USERNAME",
    "INKDROP_QBITTORRENT_PASSWORD",
    "INKDROP_QBITTORRENT_CONFIG",
    "INKDROP_QBITTORRENT_DOWNLOAD_ROOT",
    "INKDROP_DOWNLOAD_STAGING_ROOT",
    "INKDROP_UNC_PATH_MAPPINGS",
    "INKDROP_SAB_PATH_MAPPINGS",
    "INKDROP_SAB_RESCUE_SCRIPT",
    "INKDROP_UNRAR_PATH",
    "INKDROP_MANUAL_SOURCE_IMPORT_API_URL",
    "INKDROP_MARK_WAITING_API_URL",
)

# Deprecated runtime inputs remain visible in advanced diagnostics without
# becoming new public configuration knobs in .env.example.
COMPATIBILITY_ENV_KEYS = (
    "KAVITA_ACQUIRE_STATE_DIR",
)

SECRET_ENV_KEY_MARKERS = ("API_KEY", "PASSWORD", "TOKEN", "SECRET", "USERNAME")
SECRET_QUERY_KEY_MARKERS = ("APIKEY", "API_KEY", "KEY", "TOKEN", "PASSWORD", "PASS", "SECRET")
PATH_MAPPING_ENV_KEYS = ("INKDROP_UNC_PATH_MAPPINGS", "INKDROP_SAB_PATH_MAPPINGS")
PATH_MAPPING_TARGET_ROOT_ENV_MARKERS = ("_DIR", "_ROOT", "_INBOX")
DEFAULT_CONTAINER_TARGET_ROOTS = ("/config", "/state", "/staging", "/manual-inbox", "/library")
URL_ENV_KEYS = (
    "INKDROP_KAVITA_URL",
    "INKDROP_KOMGA_URL",
    "INKDROP_SLSKD_API_BASE_URL",
    "INKDROP_SLSKD_WEB_URL",
    "INKDROP_SUWAYOMI_API_BASE_URL",
    "INKDROP_PROWLARR_URL",
    "INKDROP_PROWLARR_PUBLIC_BASE_URL",
    "INKDROP_SABNZBD_URL",
    "INKDROP_QBITTORRENT_URL",
    "INKDROP_CONTAINER_WEB_BASE_URL",
    "INKDROP_MANUAL_SOURCE_IMPORT_API_URL",
    "INKDROP_MARK_WAITING_API_URL",
)
COMMA_SEPARATED_URL_ENV_KEYS = ("INKDROP_PROWLARR_INTERNAL_BASE_URLS",)
ADAPTER_URL_ENV_KEYS = tuple(
    key
    for key in URL_ENV_KEYS
    if key not in {
        "INKDROP_WEB_BASE_URL",
        "INKDROP_CONTAINER_WEB_BASE_URL",
        "INKDROP_MANUAL_SOURCE_IMPORT_API_URL",
        "INKDROP_MARK_WAITING_API_URL",
    }
)
BOOLEAN_ENV_KEYS = (
    "INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED",
    "INKDROP_MISSING_RECOVERY_ENABLED",
    "INKDROP_IMPORT_READY_QUEUE_ONLY",
    "INKDROP_DEBUG_ACTIVE_REQUESTS",
    "INKDROP_CONTAINER_SCHEDULER_ENABLED",
    "INKDROP_AUTH_ALLOW_DISABLED",
    "INKDROP_TRUSTED_LAN_TESTING",
    "INKDROP_AUTH_REQUIRED",
    "INKDROP_EXTERNAL_AUTH_ENABLED",
)
INTEGER_ENV_RULES = {
    "INKDROP_MISSING_RECOVERY_MAX_PER_COHORT": (0, 100),
    "INKDROP_MISSING_RECOVERY_MAX_HANDOFFS_PER_HOUR": (0, 10000),
    "INKDROP_MISSING_RECOVERY_MAX_BYTES_PER_HOUR": (0, 1125899906842624),
    "INKDROP_MISSING_RECOVERY_MAX_BYTES_PER_DAY": (0, 1125899906842624),
    "INKDROP_MISSING_RECOVERY_MIN_STAGING_FREE_BYTES": (0, 1125899906842624),
    "INKDROP_MISSING_RECOVERY_PAUSE_AFTER_FAILURES": (0, 10000),
    "INKDROP_SCHEDULER_MAX_CONCURRENCY": (1, 8),
    "INKDROP_SCHEDULER_HEARTBEAT_SECONDS": (2, 60),
    "INKDROP_SCHEDULER_FAILURE_BACKOFF_MAX_SECONDS": (60, 86400),
    "INKDROP_WORKER_HEALTH_MAX_AGE_SECONDS": (10, 3600),
    "INKDROP_WORKER_HEALTH_MAX_LATENESS_SECONDS": (30, 86400),
    "INKDROP_WORKER_CRITICAL_FAILURE_THRESHOLD": (1, 100),
    "INKDROP_SCHEDULER_QUEUE_MAINTENANCE_INTERVAL_SECONDS": (1, 604800),
    "INKDROP_SCHEDULER_QUEUE_MAINTENANCE_TIMEOUT_SECONDS": (60, 1800),
    "INKDROP_SCHEDULER_FULL_RECONCILIATION_INTERVAL_SECONDS": (1, 2592000),
    "INKDROP_QUEUE_RUNNER_IMPORT_PRIORITY_READY_IMPORTS": (0, 100000),
    "INKDROP_SERIES_AUTOPILOT_MAX_RUN_SECONDS": (1, 86400),
    "INKDROP_SERIES_AUTOPILOT_OUTER_TIMEOUT_SECONDS": (1, 86400),
    "INKDROP_SERIES_AUTOPILOT_TIMEOUT_KILL_AFTER_SECONDS": (0, 3600),
    "INKDROP_SERIES_AUTOPILOT_PROTECTED_RESERVE_SECONDS": (0, 3600),
    "INKDROP_AUTOPILOT_RUNTIME_HARD_GRACE_SECONDS": (0, 86400),
    "INKDROP_IMPORT_READY_IMPORT_TIMEOUT_SECONDS": (1, 86400),
    "INKDROP_IMPORT_READY_BATCH_TIMEOUT_SECONDS": (1, 86400),
    "INKDROP_PACK_MANIFEST_CACHE_SECONDS": (0, 604800),
    "INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS": (1, 3600),
    "INKDROP_MANGA_COMPLETION_BACKFILL_LIMIT": (0, 100000),
    "INKDROP_PACK_PROBE_SCAN_SECONDS": (0, 3600),
    "INKDROP_PACK_PROBE_SCAN_ENTRIES": (0, 10000000),
    "INKDROP_STATE_ENDPOINT_CONCURRENCY": (1, 8),
    "INKDROP_WEB_SOCKET_TIMEOUT_SECONDS": (5, 300),
    "INKDROP_SCHEDULER_STATUS_REFRESH_INTERVAL_SECONDS": (30, 86400),
    "INKDROP_SCHEDULER_COMPLETED_IMPORT_COMICS_INTERVAL_SECONDS": (60, 86400),
    "INKDROP_SCHEDULER_IMPORT_READY_INTERVAL_SECONDS": (60, 86400),
    "INKDROP_SCHEDULER_SERIES_AUTOPILOT_INTERVAL_SECONDS": (60, 86400),
    "INKDROP_SCHEDULER_SOURCE_WORKER_INTERVAL_SECONDS": (60, 86400),
    "INKDROP_SCHEDULER_SUWAYOMI_WORKER_INTERVAL_SECONDS": (60, 86400),
    "INKDROP_SCHEDULER_SLSKD_SEARCH_CLEANUP_INTERVAL_SECONDS": (300, 86400),
    "INKDROP_SLSKD_SEARCH_HISTORY_KEEP": (0, 100000),
    "INKDROP_SLSKD_SEARCH_HISTORY_MAX_DELETE": (0, 100000),
    "INKDROP_SLSKD_SEARCH_HISTORY_MIN_AGE_MINUTES": (0, 10080),
    # Zero disables pacing entirely; the upper bounds are an hour between
    # searches and one a second, either of which is already absurd.
    "INKDROP_SLSKD_SEARCH_MIN_INTERVAL_SECONDS": (0, 3600),
    "INKDROP_SLSKD_SEARCH_MAX_PER_HOUR": (0, 3600),
    "INKDROP_AUTH_SESSION_TTL_SECONDS": (300, 2592000),
    "INKDROP_PASSWORD_MIN_LENGTH": (1, 128),
}
PROTOCOL_ALIASES = {
    "nzb": "usenet",
    "nzbs": "usenet",
    "sab": "usenet",
    "sabnzbd": "usenet",
    "usenet": "usenet",
    "torrent": "torrent",
    "torrents": "torrent",
    "qbit": "torrent",
    "qbittorrent": "torrent",
    "qb": "torrent",
    "direct": "direct",
    "directdownload": "direct",
    "direct_download": "direct",
    "http": "direct",
    "https": "direct",
}


def _path_text(path):
    return str(path).replace("\\", "/")


def _is_blank(value):
    return str(value or "").strip() == ""


def _is_secret_key(key):
    normalized = str(key or "").upper()
    return any(marker in normalized for marker in SECRET_ENV_KEY_MARKERS)


def _redacted_env_value(key, raw):
    if _is_secret_key(key):
        return "<set>" if raw else "<unset>"
    if key in PATH_MAPPING_ENV_KEYS:
        return _redact_path_mapping_list(raw) if raw else raw
    if key in URL_ENV_KEYS or key in COMMA_SEPARATED_URL_ENV_KEYS:
        return _redact_url_list(raw) if raw else raw
    return raw


def _redact_url(value):
    parsed = urlparse(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc or ("@" not in parsed.netloc):
        return _redact_url_query(parsed).geturl() if parsed.scheme and parsed.netloc else str(value or "").strip()
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    redacted_netloc = f"<redacted>@{host}{port}" if host else "<redacted>"
    return _redact_url_query(parsed._replace(netloc=redacted_netloc)).geturl()


def _is_secret_query_key(key):
    normalized = str(key or "").upper().replace("-", "_")
    return any(marker in normalized for marker in SECRET_QUERY_KEY_MARKERS)


def _redact_url_query(parsed):
    if not parsed.query:
        return parsed
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [
        (key, "<redacted>" if _is_secret_query_key(key) and value else value)
        for key, value in pairs
    ]
    return parsed._replace(query=urlencode(redacted, doseq=True))


def _redact_url_list(value):
    return ",".join(_redact_url(item.strip()) for item in str(value or "").split(",") if item.strip())


def _redact_path_mapping_list(value):
    count = len([item for item in str(value or "").split(",") if item.strip()])
    return f"<{count} mapping(s) configured>" if count else ""


def _check_writable_dir(path, *, create=False, required=True):
    item = {"path": _path_text(path), "exists": False, "writable": False, "required": bool(required)}
    try:
        path = Path(path)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        item["exists"] = path.exists()
        item["is_dir"] = path.is_dir()
        if not path.exists() or not path.is_dir():
            item["error"] = "directory missing"
            return item
        with tempfile.NamedTemporaryFile(prefix=".inkdrop-preflight-", dir=path, delete=True) as handle:
            handle.write(b"ok")
            handle.flush()
        item["writable"] = True
    except Exception as exc:
        item["error"] = str(exc)
    return item


def _tool_check(binary, *, env_key=None, env=None, alternatives=()):
    env = env if env is not None else os.environ
    configured = str(env.get(env_key) or "").strip() if env_key else ""
    candidates = [configured, binary, *alternatives]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_absolute() or any(sep in candidate for sep in ("/", "\\")):
            if path.exists():
                return {"available": True, "path": _path_text(path), "source": env_key if candidate == configured else "path"}
        resolved = shutil.which(candidate)
        if resolved:
            return {"available": True, "path": _path_text(resolved), "source": env_key if candidate == configured else "PATH"}
    return {"available": False, "path": "", "source": env_key or "PATH"}


def _python_dependency_checks():
    checks = {}
    for module_name, package_name in REQUIRED_PYTHON_MODULES.items():
        available = importlib.util.find_spec(module_name) is not None
        checks[module_name] = {"available": available, "package": package_name}
    return checks


def _adapter_configured(adapter, values):
    return _adapter_status(adapter, values)["configured"]


def _adapter_status(adapter, values):
    required_keys = OPTIONAL_ADAPTER_REQUIRED_ENV.get(adapter, OPTIONAL_ADAPTER_ENV.get(adapter, ()))
    existing_path_keys = OPTIONAL_ADAPTER_EXISTING_PATH_ENV.get(adapter, ())
    missing_required_keys = [key for key in required_keys if _is_blank(values.get(key))]
    existing_paths = []
    missing_paths = []
    for key in existing_path_keys:
        raw = str(values.get(key) or "").strip()
        if raw and Path(raw).expanduser().exists():
            existing_paths.append(key)
        elif raw:
            missing_paths.append(key)
    if required_keys and all(not _is_blank(values.get(key)) for key in required_keys):
        configured = True
        configured_by = "required_env"
        reason = "required settings present"
    elif existing_paths:
        configured = True
        configured_by = "existing_path"
        reason = "adapter path exists"
    else:
        configured = False
        configured_by = ""
        reason = "missing required settings"
        if existing_path_keys and missing_paths:
            reason = "missing required settings and configured adapter path does not exist"
    return {
        "configured": configured,
        "configured_by": configured_by,
        "keys": sorted(values),
        "required_keys": sorted(required_keys),
        "missing_required_keys": sorted(missing_required_keys),
        "existing_path_keys": sorted(existing_path_keys),
        "existing_path_exists_keys": sorted(existing_paths),
        "existing_path_missing_keys": sorted(missing_paths),
        "reason": reason,
    }


def _effective_config(env, roots):
    values = {}
    for key in (*CONFIG_ENV_KEYS, *COMPATIBILITY_ENV_KEYS):
        raw = str(env.get(key) or "").strip()
        values[key] = _redacted_env_value(key, raw)
    for key in sorted({key for keys in OPTIONAL_ADAPTER_ENV.values() for key in keys}):
        raw = str(env.get(key) or "").strip()
        values.setdefault(key, _redacted_env_value(key, raw))
    return {
        "runtime_roots": {name: _path_text(path) for name, path in roots.items()},
        "state_db_path": _path_text(inkdrop_runtime_config.state_db_path(env)),
        "env": values,
    }


def _web_config(env):
    configured_base_url = str(env.get("INKDROP_WEB_BASE_URL") or "").strip().rstrip("/")
    container_base_url = str(env.get("INKDROP_CONTAINER_WEB_BASE_URL") or "").strip().rstrip("/")
    host = inkdrop_runtime_config.web_host(env)
    port = inkdrop_runtime_config.web_port(env, strict=False)
    published_port = inkdrop_runtime_config.published_web_port(env, strict=False)
    local_base_url = f"http://127.0.0.1:{port}"
    return {
        "bind_host": host,
        "bind_port": port,
        "host_port": published_port,
        "host_port_source": "INKDROP_HOST_PORT" if str(env.get("INKDROP_HOST_PORT") or "").strip() else "INKDROP_PORT",
        "local_base_url": local_base_url,
        "configured_base_url": configured_base_url,
        "container_base_url": container_base_url,
        "callback_base_url": inkdrop_runtime_config.worker_web_base_url(env),
        "callback_base_source": inkdrop_runtime_config.worker_web_base_url_source(env),
    }


def _web_config_errors(env):
    errors = []
    raw_host = str(env.get("INKDROP_HOST") or "").strip()
    if raw_host and ("://" in raw_host or "/" in raw_host or "\\" in raw_host):
        errors.append("INKDROP_HOST must be a bind host/address such as 0.0.0.0, not a URL or path.")

    configured_base_url = str(env.get("INKDROP_WEB_BASE_URL") or "").strip()
    if configured_base_url:
        parsed = urlparse(configured_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("INKDROP_WEB_BASE_URL must be a full http(s) URL such as http://inkdrop.example:8796.")
    explicit_worker_http = any(
        str(env.get(key) or "").strip()
        for key in (
            "INKDROP_WEB_BASE_URL",
            "INKDROP_MANUAL_SOURCE_IMPORT_API_URL",
            "INKDROP_MARK_WAITING_API_URL",
        )
    )
    if explicit_worker_http and not inkdrop_runtime_config.worker_api_key(env):
        errors.append(
            "Explicit worker HTTP callbacks require INKDROP_WORKER_API_KEY with read and acquisition scopes."
        )
    return errors


def _is_full_http_url(value):
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _url_config_errors(env):
    errors = []
    for key in URL_ENV_KEYS:
        raw = str(env.get(key) or "").strip()
        if raw and not _is_full_http_url(raw):
            errors.append(f"{key} must be a full http(s) URL such as http://service:port.")

    for key in COMMA_SEPARATED_URL_ENV_KEYS:
        raw = str(env.get(key) or "").strip()
        if not raw:
            continue
        for index, item in enumerate(raw.split(","), 1):
            text = item.strip()
            if text and not _is_full_http_url(text):
                errors.append(f"{key} entry {index} must be a full http(s) URL such as http://service:port.")
    return errors


def _is_loopback_hostname(hostname):
    host = str(hostname or "").strip().lower().rstrip(".")
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def _adapter_url_warnings(env):
    warnings = []
    for key in ADAPTER_URL_ENV_KEYS:
        raw = str(env.get(key) or "").strip()
        if not raw or not _is_full_http_url(raw):
            continue
        parsed = urlparse(raw)
        if _is_loopback_hostname(parsed.hostname):
            warnings.append(
                f"{key} points at {parsed.hostname}; inside Docker this usually means the InkDrop container, not the external service. Use a Compose service name, LAN host, reverse proxy name, or host.docker.internal when appropriate."
            )
    for key in COMMA_SEPARATED_URL_ENV_KEYS:
        raw = str(env.get(key) or "").strip()
        if not raw:
            continue
        for index, item in enumerate(raw.split(","), 1):
            text = item.strip()
            if not text or not _is_full_http_url(text):
                continue
            parsed = urlparse(text)
            if _is_loopback_hostname(parsed.hostname):
                warnings.append(
                    f"{key} entry {index} points at {parsed.hostname}; inside Docker this usually means the InkDrop container, not the external service. Use a Compose service name, LAN host, reverse proxy name, or host.docker.internal when appropriate."
                )
    return warnings


def _operator_knob_errors(env):
    errors = []
    bool_values = {"0", "1", "true", "false", "yes", "no", "on", "off"}
    for key in BOOLEAN_ENV_KEYS:
        raw = str(env.get(key) or "").strip().lower()
        if raw and raw not in bool_values:
            errors.append(f"{key} must be a boolean value: 0/1, true/false, yes/no, or on/off.")

    for key, (minimum, maximum) in INTEGER_ENV_RULES.items():
        raw = str(env.get(key) or "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            errors.append(f"{key} must be an integer between {minimum} and {maximum}, got {raw!r}.")
            continue
        if not minimum <= value <= maximum:
            errors.append(f"{key} must be between {minimum} and {maximum}, got {value}.")

    quiet_hours = str(env.get("INKDROP_MISSING_RECOVERY_QUIET_HOURS") or "").strip()
    if quiet_hours:
        try:
            start_text, end_text = (part.strip() for part in quiet_hours.split("-", 1))
            start_hour, start_minute = (int(part) for part in start_text.split(":", 1))
            end_hour, end_minute = (int(part) for part in end_text.split(":", 1))
            start = start_hour * 60 + start_minute
            end = end_hour * 60 + end_minute
            valid_quiet_hours = 0 <= start < 1440 and 0 <= end < 1440 and start != end
        except (TypeError, ValueError):
            valid_quiet_hours = False
        if not valid_quiet_hours:
            errors.append(
                "INKDROP_MISSING_RECOVERY_QUIET_HOURS must be a non-empty local-time range "
                "such as 22:00-06:00, or blank to disable quiet hours."
            )

    raw_protocol_order = str(env.get("INKDROP_PROTOCOL_ORDER") or "").strip()
    if raw_protocol_order:
        unknown = []
        valid = []
        for item in raw_protocol_order.split(","):
            text = item.strip().lower()
            if not text:
                continue
            normalized = PROTOCOL_ALIASES.get(text)
            if normalized is None:
                unknown.append(text)
            else:
                valid.append(normalized)
        if unknown:
            errors.append("INKDROP_PROTOCOL_ORDER contains unsupported value(s): " + ", ".join(sorted(set(unknown))) + ". Supported values are usenet, torrent, and direct.")
        if not valid:
            errors.append("INKDROP_PROTOCOL_ORDER must include at least one supported value: usenet, torrent, or direct.")
    return errors


def _path_mapping_checks(env):
    allowed_target_roots = _path_mapping_target_roots(env)
    checks = {}
    for key in PATH_MAPPING_ENV_KEYS:
        raw = str(env.get(key) or "").strip()
        entries = []
        errors = []
        if raw:
            for index, item in enumerate(raw.split(","), 1):
                text = item.strip()
                if not text:
                    continue
                if "=" not in text:
                    errors.append(f"{key} entry {index} must use source=target syntax")
                    entries.append({"index": index, "raw": "<invalid-mapping>", "valid": False})
                    continue
                source, target = (part.strip() for part in text.split("=", 1))
                valid = bool(source and target)
                if not valid:
                    errors.append(f"{key} entry {index} must include both source and target")
                target_error = _path_mapping_target_error(target, allowed_target_roots)
                if target_error:
                    valid = False
                    errors.append(f"{key} entry {index} {target_error}")
                safe_target = _normalize_container_path(target) if valid else "<invalid-target>"
                entries.append({"index": index, "source": "<source-path>", "target": safe_target, "valid": valid})
        checks[key] = {"configured": bool(entries), "entries": entries, "errors": errors}
    return checks


def _normalize_container_path(value):
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def _path_mapping_target_roots(env):
    roots = {_normalize_container_path(item) for item in DEFAULT_CONTAINER_TARGET_ROOTS}
    for key in CONFIG_ENV_KEYS:
        if not key.startswith("INKDROP_") or not key.endswith(PATH_MAPPING_TARGET_ROOT_ENV_MARKERS):
            continue
        raw = str(env.get(key) or "").strip()
        if raw:
            roots.add(_normalize_container_path(raw))
    return tuple(sorted(root for root in roots if root.startswith("/")))


def _path_is_under_root(path, root):
    path = _normalize_container_path(path)
    root = _normalize_container_path(root)
    return path == root or path.startswith(root + "/")


def _path_mapping_target_error(target, allowed_target_roots):
    if not target:
        return ""
    text = str(target).strip()
    normalized = _normalize_container_path(text)
    if len(text) >= 2 and text[1] == ":":
        return "target must be a container path, not a Windows drive path"
    if text.startswith("\\\\") or text.startswith("//"):
        return "target must be a container path, not a UNC/network path"
    if "://" in text or urlparse(text).scheme:
        return "target must be a container filesystem path, not a URL"
    if not normalized.startswith("/"):
        return "target must be an absolute container path"
    parts = [part for part in normalized.split("/") if part]
    if ".." in parts:
        return "target must not contain parent-directory traversal"
    if not any(_path_is_under_root(normalized, root) for root in allowed_target_roots):
        allowed = ", ".join(DEFAULT_CONTAINER_TARGET_ROOTS)
        return f"target must be under an InkDrop container root such as {allowed}"
    return ""


def _warning_summary(configured_adapters, archive_tools, missing_python_dependencies):
    optional_adapters = [
        name
        for name, item in sorted(configured_adapters.items())
        if not item.get("configured")
        and name in {"comicvine", "prowlarr", "slskd", "suwayomi", "sabnzbd", "qbittorrent"}
    ]
    runtime_tools = [
        name
        for name, item in sorted(archive_tools.items())
        if not item.get("available")
    ]
    return {
        "optional_adapters_unconfigured": optional_adapters,
        "runtime_tools_missing": runtime_tools,
        "python_dependencies_missing": list(missing_python_dependencies),
    }


def run_preflight(environ=None, *, create=False, strict_dependencies=False, strict_runtime_tools=False):
    env = environ if environ is not None else os.environ
    strict_dependencies = bool(strict_dependencies or str(env.get("INKDROP_PREFLIGHT_STRICT_DEPENDENCIES") or "").strip().lower() in {"1", "true", "yes", "on"})
    strict_runtime_tools = bool(strict_runtime_tools or str(env.get("INKDROP_PREFLIGHT_STRICT_RUNTIME_TOOLS") or "").strip().lower() in {"1", "true", "yes", "on"})
    roots = inkdrop_runtime_config.runtime_roots(env)
    checks = {
        name: _check_writable_dir(path, create=create, required=name not in OPTIONAL_RUNTIME_ROOTS)
        for name, path in roots.items()
    }

    errors = []
    warnings = []
    try:
        inkdrop_runtime_config.web_port(env, strict=True)
    except ValueError as exc:
        errors.append(str(exc))
    try:
        inkdrop_runtime_config.published_web_port(env, strict=True)
    except ValueError as exc:
        errors.append(str(exc))
    errors.extend(_web_config_errors(env))
    errors.extend(_url_config_errors(env))
    errors.extend(_operator_knob_errors(env))
    external_requested = str(env.get("INKDROP_EXTERNAL_AUTH_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on", "enabled"} or str(env.get("INKDROP_AUTH_MODE") or "").strip().lower() in {"external", "built_in_or_external"}
    external_trust = inkdrop_auth.parse_trusted_proxy_networks(env.get("INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES") or "")
    if external_requested and not external_trust["configuration_valid"]:
        errors.append("External authentication trusted proxies contain an invalid IP address or CIDR.")
    if external_requested and not external_trust["valid_networks"]:
        errors.append("External authentication requires at least one valid trusted proxy IP address or CIDR.")
    warnings.extend(_adapter_url_warnings(env))
    for name, check in checks.items():
        issue = None
        if not check.get("exists"):
            issue = f"{name} does not exist: {check.get('path')}"
        elif not check.get("is_dir"):
            issue = f"{name} is not a directory: {check.get('path')}"
        elif not check.get("writable"):
            issue = f"{name} is not writable: {check.get('path')}"
        if issue and check.get("required"):
            errors.append(issue)
        elif issue:
            warnings.append(issue + "; related staging/manual/quarantine features may be limited.")

    configured_adapters = {}
    for adapter, keys in OPTIONAL_ADAPTER_ENV.items():
        values = {key: str(env.get(key) or "").strip() for key in keys}
        configured_adapters[adapter] = _adapter_status(adapter, values)
        if adapter == "comicvine" and not configured_adapters[adapter]["configured"]:
            warnings.append("ComicVine API key is not configured; ComicVine metadata lookup will be unavailable.")
        elif adapter in {"prowlarr", "slskd", "suwayomi", "sabnzbd", "qbittorrent"} and not configured_adapters[adapter]["configured"]:
            warnings.append(f"{adapter} is not configured; related automation will stay disabled or limited.")

    archive_tools = {
        "seven_zip": _tool_check("7z", env=env),
        "unrar": _tool_check("unrar", env_key="INKDROP_UNRAR_PATH", env=env, alternatives=("unrar-free",)),
    }
    if not archive_tools["seven_zip"]["available"]:
        message = "7z is not available; CBR/RAR inspection and pack extraction will be limited."
        if strict_runtime_tools:
            errors.append(message)
        else:
            warnings.append(message)
    if not archive_tools["unrar"]["available"]:
        message = "unrar is not available; 7z remains the primary CBR/RAR tool, but fallback extraction is unavailable."
        if strict_runtime_tools:
            errors.append(message)
        else:
            warnings.append(message)

    python_dependencies = _python_dependency_checks()
    missing_python_dependencies = [
        f"{module} ({item['package']})"
        for module, item in sorted(python_dependencies.items())
        if not item["available"]
    ]
    if missing_python_dependencies:
        message = "Missing Python dependencies: " + ", ".join(missing_python_dependencies)
        if strict_dependencies:
            errors.append(message)
        else:
            warnings.append(message + ". Run inside the Docker image or install requirements.txt before starting InkDrop directly.")

    path_mappings = _path_mapping_checks(env)
    for check in path_mappings.values():
        errors.extend(check["errors"])

    payload = {
        "preflight_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
        "ok": not errors,
        "created_missing_dirs": bool(create),
        "checked_at": time.time(),
        "roots": checks,
        "state_db_path": _path_text(inkdrop_runtime_config.state_db_path(env)),
        "web": _web_config(env),
        "path_mappings": path_mappings,
        "effective_config": _effective_config(env, roots),
        "configured_adapters": configured_adapters,
        "archive_tools": archive_tools,
        "python_dependencies": python_dependencies,
        "warning_summary": _warning_summary(configured_adapters, archive_tools, missing_python_dependencies),
        "warnings": warnings,
        "errors": errors,
    }
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run InkDrop Docker/startup preflight checks.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--quiet", action="store_true", help="Only print failures.")
    parser.add_argument("--create", action="store_true", help="Create missing runtime directories before checking.")
    parser.add_argument("--strict-dependencies", action="store_true", help="Fail when required Python packages are missing.")
    parser.add_argument("--strict-runtime-tools", action="store_true", help="Fail when required archive/runtime tools are missing.")
    args = parser.parse_args(argv)

    payload = run_preflight(create=args.create, strict_dependencies=args.strict_dependencies, strict_runtime_tools=args.strict_runtime_tools)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif payload["ok"]:
        if not args.quiet:
            print("InkDrop preflight ok")
            for warning in payload["warnings"]:
                print(f"warning: {warning}")
    else:
        for error in payload["errors"]:
            print(f"error: {error}")
        if not args.quiet:
            for warning in payload["warnings"]:
                print(f"warning: {warning}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
