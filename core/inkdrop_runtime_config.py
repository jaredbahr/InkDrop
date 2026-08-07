#!/usr/bin/env python3
"""Small runtime/profile path accessors for InkDrop.

This keeps production-compatible defaults available while allowing clean
installs to provide their own roots through environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path


STATE_DB_NAME = "inkdrop-state.sqlite3"
IMPORTED_FILES_DB_NAME = "imported-files.sqlite3"
PENDING_PACK_IMPORTS_NAME = "pending-pack-imports.jsonl"
SAB_FAILED_CLEANUP_STATUS_NAME = "sab-failed-cleanup-status.json"
PACK_REVIEW_STATE_NAME = "pack-review-state.json"

ENV_CONFIG_DIR = "INKDROP_CONFIG_DIR"
ENV_STATE_DIR = "INKDROP_STATE_DIR"
ENV_LOCK_DIR = "INKDROP_LOCK_DIR"
ENV_LOG_DIR = "INKDROP_LOG_DIR"
ENV_CACHE_DIR = "INKDROP_CACHE_DIR"
ENV_BACKUP_DIR = "INKDROP_BACKUP_DIR"
ENV_STAGING_DIR = "INKDROP_STAGING_DIR"
ENV_MANUAL_INBOX_DIR = "INKDROP_MANUAL_INBOX_DIR"
ENV_QUARANTINE_DIR = "INKDROP_QUARANTINE_DIR"
ENV_HOST = "INKDROP_HOST"
ENV_PORT = "INKDROP_PORT"
ENV_HOST_PORT = "INKDROP_HOST_PORT"
ENV_WEB_BASE_URL = "INKDROP_WEB_BASE_URL"
ENV_CONTAINER_WEB_BASE_URL = "INKDROP_CONTAINER_WEB_BASE_URL"
ENV_WORKER_API_KEY = "INKDROP_WORKER_API_KEY"
ENV_KAVITA_DB = "INKDROP_KAVITA_DB"
LEGACY_ENV_STATE_DIR = "KAVITA_ACQUIRE_STATE_DIR"
LEGACY_ENV_KAVITA_DB = "INKDROP_KAVITA_DB_PATH"
ENV_WORKER_STATUS_FILE = "INKDROP_WORKER_STATUS_FILE"


def env_value(environ, key, fallback="", *, legacy_keys=()):
    """Resolve a canonical setting before any compatibility aliases."""
    env = environ if environ is not None else os.environ
    for candidate in (key, *tuple(legacy_keys or ())):
        raw = str(env.get(candidate) or "").strip()
        if raw:
            return raw
    return fallback


def _path_from_env(env, key, fallback):
    raw = str(env.get(key) or "").strip()
    return Path(raw).expanduser() if raw else Path(fallback)


def web_host(environ=None):
    env = environ if environ is not None else os.environ
    return str(env.get(ENV_HOST) or "").strip() or "0.0.0.0"


def web_port(environ=None, *, strict=True):
    env = environ if environ is not None else os.environ
    raw = str(env.get(ENV_PORT) or "").strip() or "8796"
    try:
        port = int(raw)
    except (TypeError, ValueError):
        if strict:
            raise ValueError(f"{ENV_PORT} must be an integer between 1 and 65535, got {raw!r}")
        return 8796
    if 1 <= port <= 65535:
        return port
    if strict:
        raise ValueError(f"{ENV_PORT} must be between 1 and 65535, got {port}")
    return 8796


def published_web_port(environ=None, *, strict=True):
    """Return the operator-facing Docker host port.

    ``INKDROP_HOST_PORT`` is intentionally separate from the in-container bind
    port. Falling back to ``INKDROP_PORT`` preserves existing installs that
    used that older setting for both sides of the Compose mapping.
    """
    env = environ if environ is not None else os.environ
    raw = str(env.get(ENV_HOST_PORT) or "").strip()
    if not raw:
        return web_port(env, strict=strict)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        if strict:
            raise ValueError(f"{ENV_HOST_PORT} must be an integer between 1 and 65535, got {raw!r}")
        return web_port(env, strict=False)
    if 1 <= port <= 65535:
        return port
    if strict:
        raise ValueError(f"{ENV_HOST_PORT} must be between 1 and 65535, got {port}")
    return web_port(env, strict=False)


def web_listener_config(environ=None):
    env = environ if environ is not None else os.environ
    bind_port = web_port(env, strict=False)
    host_port_configured = bool(str(env.get(ENV_HOST_PORT) or "").strip())
    return {
        "bind_host": web_host(env),
        "container_port": bind_port,
        "host_port": published_web_port(env, strict=False),
        "host_port_source": ENV_HOST_PORT if host_port_configured else ENV_PORT,
        "configuration_source": "environment",
        "restart_required": True,
        "recreate_required": True,
    }


def worker_web_base_url(environ=None):
    """Resolve the callback base used by worker processes.

    Compose supplies the service-DNS container URL. Local/standalone commands
    do not, so their safe fallback remains loopback on the internal bind port.
    """
    env = environ if environ is not None else os.environ
    for key in (ENV_CONTAINER_WEB_BASE_URL, ENV_WEB_BASE_URL):
        configured = str(env.get(key) or "").strip().rstrip("/")
        if configured:
            return configured
    return f"http://127.0.0.1:{web_port(env, strict=False)}"


def worker_web_base_url_source(environ=None):
    env = environ if environ is not None else os.environ
    if str(env.get(ENV_CONTAINER_WEB_BASE_URL) or "").strip():
        return ENV_CONTAINER_WEB_BASE_URL
    if str(env.get(ENV_WEB_BASE_URL) or "").strip():
        return ENV_WEB_BASE_URL
    return "local_port"


def worker_api_key(environ=None):
    env = environ if environ is not None else os.environ
    return str(env.get(ENV_WORKER_API_KEY) or "").strip()


def worker_auth_headers(environ=None, *, required=True):
    token = worker_api_key(environ)
    if not token and required:
        raise RuntimeError(
            "authenticated worker HTTP callback requires INKDROP_WORKER_API_KEY; "
            "create an InkDrop API key with read and acquisition scopes"
        )
    return {"X-InkDrop-API-Key": token} if token else {}


def worker_http_callback_requested(environ=None, *, endpoint_keys=()):
    """Whether an operator explicitly selected the authenticated HTTP path."""
    env = environ if environ is not None else os.environ
    keys = (ENV_WORKER_API_KEY, ENV_WEB_BASE_URL, *tuple(endpoint_keys or ()))
    return any(str(env.get(key) or "").strip() for key in keys)


def config_dir(environ=None):
    env = environ if environ is not None else os.environ
    raw = str(env.get(ENV_CONFIG_DIR) or "").strip()
    if raw:
        return Path(raw).expanduser()
    state_raw = env_value(env, ENV_STATE_DIR, legacy_keys=(LEGACY_ENV_STATE_DIR,))
    if state_raw:
        return Path(state_raw).expanduser()
    return Path("/config")


def state_dir(environ=None):
    env = environ if environ is not None else os.environ
    return Path(
        env_value(env, ENV_STATE_DIR, "/state", legacy_keys=(LEGACY_ENV_STATE_DIR,))
    ).expanduser()


def normalize_runtime_environment(environ=None):
    """Materialize one state domain for container entrypoints and children.

    Canonical values win, the legacy state root is accepted only as a fallback,
    and explicit related-path overrides are never replaced.
    """
    env = environ if environ is not None else os.environ
    resolved_state = state_dir(env)
    defaults = {
        ENV_STATE_DIR: resolved_state,
        ENV_LOCK_DIR: resolved_state / "locks",
        ENV_LOG_DIR: resolved_state / "logs",
        ENV_CACHE_DIR: resolved_state / "cache",
        ENV_BACKUP_DIR: resolved_state / "backups",
        ENV_QUARANTINE_DIR: resolved_state / "quarantine",
        ENV_WORKER_STATUS_FILE: resolved_state / "worker-scheduler-status.json",
        "INKDROP_CANDIDATE_MANIFEST_PATH": resolved_state / "release" / "current.json",
        "INKDROP_UNMATCHED_QUARANTINE_ROOT": resolved_state / "quarantine" / "unmatched",
        "INKDROP_MANAGED_DUPLICATE_QUARANTINE_ROOT": resolved_state / "quarantine" / "managed-duplicate-files",
        "INKDROP_PACK_DUPLICATE_QUARANTINE_ROOT": resolved_state / "quarantine" / "pack-duplicates",
        "INKDROP_PACK_REVIEW_QUARANTINE_ROOT": resolved_state / "quarantine" / "pack-review",
    }
    for key, value in defaults.items():
        if not str(env.get(key) or "").strip():
            env[key] = str(value)
    return env


def lock_dir(environ=None):
    env = environ if environ is not None else os.environ
    return _path_from_env(env, ENV_LOCK_DIR, state_dir(env) / "locks")


def lock_path(name, environ=None):
    leaf = Path(str(name or "").strip()).name
    if not leaf:
        raise ValueError("lock name is required")
    return lock_dir(environ) / leaf


def log_dir(environ=None):
    env = environ if environ is not None else os.environ
    return _path_from_env(env, ENV_LOG_DIR, state_dir(env) / "logs")


def cache_dir(environ=None):
    env = environ if environ is not None else os.environ
    return _path_from_env(env, ENV_CACHE_DIR, state_dir(env) / "cache")


def backup_dir(environ=None):
    env = environ if environ is not None else os.environ
    return _path_from_env(env, ENV_BACKUP_DIR, state_dir(env) / "backups")


def staging_dir(environ=None):
    env = environ if environ is not None else os.environ
    return _path_from_env(env, ENV_STAGING_DIR, "/staging")


def manual_inbox_dir(environ=None):
    env = environ if environ is not None else os.environ
    return _path_from_env(env, ENV_MANUAL_INBOX_DIR, "/manual-inbox")


def quarantine_dir(environ=None):
    env = environ if environ is not None else os.environ
    return _path_from_env(env, ENV_QUARANTINE_DIR, state_dir(env) / "quarantine")


def optional_adapter_db_path(environ=None, *, env_key, adapter_dir, filename, legacy_env_keys=()):
    env = environ if environ is not None else os.environ
    configured = env_value(env, env_key, legacy_keys=legacy_env_keys)
    if configured:
        return Path(configured).expanduser()
    return config_dir(env) / adapter_dir / filename


def kavita_db_path(environ=None):
    return optional_adapter_db_path(
        environ,
        env_key=ENV_KAVITA_DB,
        adapter_dir="kavita",
        filename="kavita.db",
        legacy_env_keys=(LEGACY_ENV_KAVITA_DB,),
    )


def compatible_log_paths(canonical_name, *legacy_names, environ=None):
    """Return canonical-first log candidates without copying or dual-writing."""
    root = log_dir(environ)
    names = (canonical_name, *legacy_names)
    return tuple(root / Path(str(name)).name for name in names if str(name or "").strip())


def state_db_path(environ=None):
    return state_dir(environ) / STATE_DB_NAME


def imported_files_db_path(environ=None):
    return state_dir(environ) / IMPORTED_FILES_DB_NAME


def pending_pack_imports_path(environ=None):
    return state_dir(environ) / PENDING_PACK_IMPORTS_NAME


def sab_failed_cleanup_status_path(environ=None):
    return state_dir(environ) / SAB_FAILED_CLEANUP_STATUS_NAME


def pack_review_state_path(environ=None):
    return state_dir(environ) / PACK_REVIEW_STATE_NAME


def runtime_roots(environ=None):
    env = environ if environ is not None else os.environ
    return {
        "config_dir": config_dir(env),
        "state_dir": state_dir(env),
        "log_dir": log_dir(env),
        "cache_dir": cache_dir(env),
        "backup_dir": backup_dir(env),
        "staging_dir": staging_dir(env),
        "manual_inbox_dir": manual_inbox_dir(env),
        "quarantine_dir": quarantine_dir(env),
    }


def ensure_runtime_roots(environ=None):
    roots = runtime_roots(environ)
    optional_roots = {"staging_dir", "manual_inbox_dir", "quarantine_dir"}
    for key, path in roots.items():
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError:
            if key in optional_roots:
                continue
            raise
    return roots
