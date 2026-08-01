#!/usr/bin/env python3
"""Canonical, redacted effective integration configuration for InkDrop."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import contextlib
from pathlib import Path
from urllib.parse import urlsplit


INTEGRATION_ALIASES = {
    "comic_vine": "comicvine",
    "comic vine": "comicvine",
    "comic-vine": "comicvine",
    "qbit": "qbittorrent",
    "q_bit_torrent": "qbittorrent",
    "sab": "sabnzbd",
    "soulseek": "slskd",
    "rss": "rss_direct",
    "rss_getcomics": "rss_direct",
    "generic_rss_direct_feed": "rss_direct",
    "local": "local_manual_inbox",
    "manual": "local_manual_inbox",
    "local_manual": "local_manual_inbox",
}


def _spec(display_name, category, certification_tier, capabilities, *, env_url=(), env_secrets=(), defaults_configured=False, implemented=True, configure_target="", requires_url=False, requires_secret=False):
    return {
        "display_name": display_name,
        "category": category,
        "certification_tier": certification_tier,
        "capabilities": list(capabilities),
        "env_url": tuple(env_url),
        "env_secrets": tuple(env_secrets),
        "defaults_configured": bool(defaults_configured),
        "implemented": bool(implemented),
        "configure_target": configure_target,
        "requires_url": bool(requires_url),
        "requires_secret": bool(requires_secret),
    }


INTEGRATIONS = {
    "comicvine": _spec("ComicVine", "metadata", "implemented", ("metadata_search", "series_catalog", "test"), env_url=("INKDROP_COMICVINE_URL",), env_secrets=("INKDROP_COMICVINE_API_KEY",), configure_target="metadata_source", requires_secret=True),
    "mangadex": _spec("MangaDex", "metadata_source", "implemented", ("metadata_search", "chapter_catalog", "direct_download", "test"), env_url=("INKDROP_MANGADEX_API_BASE_URL",), defaults_configured=True, configure_target="metadata_source"),
    "prowlarr": _spec("Prowlarr", "indexer", "implemented", ("search", "candidate_discovery", "test"), env_url=("INKDROP_PROWLARR_URL",), env_secrets=("INKDROP_PROWLARR_API_KEY",), configure_target="indexers", requires_url=True, requires_secret=True),
    "qbittorrent": _spec("qBittorrent", "download_client", "implemented", ("test", "grab", "poll", "progress", "import"), env_url=("INKDROP_QBITTORRENT_URL", "INKDROP_QBIT_URL"), env_secrets=("INKDROP_QBITTORRENT_PASSWORD",), configure_target="download_clients"),
    "sabnzbd": _spec("SABnzbd", "download_client", "implemented", ("test", "grab", "poll", "progress", "import"), env_url=("INKDROP_SABNZBD_URL",), env_secrets=("INKDROP_SABNZBD_API_KEY",), configure_target="download_clients", requires_url=True, requires_secret=True),
    "slskd": _spec("SLSKD", "download_client", "implemented", ("test", "search", "grab", "poll", "import"), env_url=("INKDROP_SLSKD_API_BASE_URL",), env_secrets=("INKDROP_SLSKD_API_KEY",), configure_target="download_clients", requires_url=True, requires_secret=True),
    "suwayomi": _spec("Suwayomi", "managed_folder_source", "beta", ("test", "managed_folder", "import"), env_url=("INKDROP_SUWAYOMI_API_BASE_URL",), env_secrets=("INKDROP_SUWAYOMI_API_KEY",), configure_target="download_sources"),
    "kavita": _spec("Kavita", "library_frontend", "implemented", ("test", "scan", "visibility"), env_url=("INKDROP_KAVITA_URL",), env_secrets=("INKDROP_KAVITA_API_KEY",), configure_target="connect", requires_url=True, requires_secret=True),
    "komga": _spec("Komga", "library_frontend", "beta", ("test", "scan", "visibility"), env_url=("INKDROP_KOMGA_URL",), env_secrets=("INKDROP_KOMGA_PASSWORD", "INKDROP_KOMGA_API_KEY"), configure_target="connect", requires_url=True, requires_secret=True),
    "transmission": _spec("Transmission", "download_client", "beta", ("test", "grab", "poll", "progress", "import"), env_url=("INKDROP_TRANSMISSION_URL",), env_secrets=("INKDROP_TRANSMISSION_PASSWORD",), configure_target="download_clients"),
    "deluge": _spec("Deluge", "download_client", "beta", ("test", "grab", "poll", "progress", "import"), env_url=("INKDROP_DELUGE_URL",), env_secrets=("INKDROP_DELUGE_PASSWORD",), configure_target="download_clients"),
    "nzbget": _spec("NZBGet", "download_client", "beta", ("test", "grab", "poll", "progress", "import"), env_url=("INKDROP_NZBGET_URL",), env_secrets=("INKDROP_NZBGET_PASSWORD", "INKDROP_NZBGET_API_KEY"), configure_target="download_clients"),
    "rss_direct": _spec("Direct / RSS Sources", "download_source", "beta", ("feed", "candidate_discovery", "direct_download", "import"), env_url=("INKDROP_RSS_FEED_URL",), defaults_configured=True, configure_target="download_sources"),
    "local_manual_inbox": _spec("Local / Manual Inbox", "local_source", "implemented", ("local_folder", "manual_intake", "import"), env_url=("INKDROP_MANUAL_INBOX_DIR",), defaults_configured=True, configure_target="paths"),
}


CONTENT_FIT = {
    "comicvine": ["western_comics", "graphic_novels"],
    "mangadex": ["manga", "manhwa", "manhua"],
    "prowlarr": ["comics", "manga", "ebooks"],
    "rss_direct": ["comics", "manga"],
    "local_manual_inbox": ["comics", "manga", "ebooks"],
}


SECRET_SETTING_KEYS = {
    "api_key", "apikey", "password", "token", "access_token", "auth_token", "cookie", "cookies",
}
URL_SETTING_KEYS = {"base_url", "url", "host", "api_base_url", "endpoint", "feed_url"}


def canonical_integration_id(value):
    raw = str(value or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return INTEGRATION_ALIASES.get(raw) or INTEGRATION_ALIASES.get(compact) or compact


def _nonblank(value):
    return bool(str(value or "").strip())


def _safe_host(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        return "configured"
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "configured"
    host = parsed.hostname or ""
    if not host:
        return "configured"
    return f"{host}:{parsed.port}" if parsed.port else host


def _json_object(value):
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stored_provider_rows(db_path):
    path = Path(db_path) if db_path else None
    if not path or not path.exists():
        return []
    uri = f"file:{path}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as con:
        con.row_factory = sqlite3.Row
        con.execute("pragma query_only=1")
        provider_table = con.execute(
            "select 1 from sqlite_master where type='table' and name='provider_configs' limit 1"
        ).fetchone()
        if not provider_table:
            return []
        return [dict(row) for row in con.execute(
            """
            select id, provider_type, display_name, enabled, base_url, secret_ref,
                   settings_json, source, updated_at
            from provider_configs
            """
        )]


def _stored_provider_map(rows):
    result = {}
    for row in rows or ():
        provider_id = canonical_integration_id(row.get("id") or row.get("display_name"))
        if provider_id not in INTEGRATIONS:
            continue
        current = result.get(provider_id)
        if current is None or float(row.get("updated_at") or 0) >= float(current.get("updated_at") or 0):
            result[provider_id] = dict(row)
    return result


def _health_map(health_rows):
    result = {}
    if isinstance(health_rows, dict):
        iterable = health_rows.items()
    else:
        iterable = ((row.get("integration_id") or row.get("provider_id") or row.get("id"), row) for row in (health_rows or ()))
    for key, row in iterable:
        canonical = canonical_integration_id(key)
        if canonical in INTEGRATIONS and isinstance(row, dict):
            result[canonical] = row
    return result


def _first_env(env, keys):
    for key in keys:
        if _nonblank(env.get(key)):
            return key, env.get(key)
    return "", ""


def _sqlite_evidence(row):
    if not row:
        return False, False, False, ""
    settings = _json_object(row.get("settings_json") if "settings_json" in row else row.get("settings"))
    secret_present = any(_nonblank(settings.get(key)) and str(settings.get(key)).strip() not in {"***", "********"} for key in SECRET_SETTING_KEYS)
    secret_ref = str(row.get("secret_ref") or "").strip()
    if secret_ref and not any(token in secret_ref.lower() for token in ("provider setting:", "fallback ", "config:")):
        secret_present = True
    base_url = str(row.get("base_url") or "").strip()
    if not base_url:
        for key in URL_SETTING_KEYS:
            if _nonblank(settings.get(key)):
                base_url = str(settings.get(key)).strip()
                break
    meaningful_setting = any(
        _nonblank(value)
        for key, value in settings.items()
        if key not in {"editable_fields", "secret_fields", "policy", "adapter"}
    )
    configured = bool(base_url or secret_present or meaningful_setting)
    return configured, secret_present, bool(base_url), base_url


def resolve_effective_integrations(db_path=None, environ=None, health_rows=None, stored_rows=None):
    """Return one canonical, secret-free status row per integration."""
    env = os.environ if environ is None else environ
    stored = _stored_provider_map(stored_rows if stored_rows is not None else _stored_provider_rows(db_path))
    health = _health_map(health_rows)
    rows = []
    for integration_id, spec in INTEGRATIONS.items():
        saved = stored.get(integration_id) or {}
        sqlite_configured, sqlite_secret, sqlite_url, sqlite_url_value = _sqlite_evidence(saved)
        env_url_key, env_url_value = _first_env(env, spec["env_url"])
        env_secret_key, _env_secret_value = _first_env(env, spec["env_secrets"])
        env_configured = bool(env_url_key or env_secret_key)
        default_configured = bool(spec["defaults_configured"])
        known_healthy = str((health.get(integration_id) or {}).get("state") or (health.get(integration_id) or {}).get("health_status") or "").strip().lower() in {"healthy", "connected", "available", "ok"}
        url_present = bool(sqlite_url or env_url_key)
        secret_present = bool(sqlite_secret or env_secret_key)
        evidence_present = bool(sqlite_configured or env_configured or default_configured or known_healthy)
        configured = bool(
            default_configured
            or (
                evidence_present
                and (url_present or not spec["requires_url"])
                and (secret_present or not spec["requires_secret"])
            )
            or known_healthy
        )
        if saved:
            configuration_source = "sqlite"
        elif env_secret_key:
            configuration_source = "secret_reference"
        elif env_url_key:
            configuration_source = "environment"
        elif known_healthy:
            configuration_source = "legacy_compatibility"
        elif default_configured:
            configuration_source = "default"
        else:
            configuration_source = "none"
        enabled = bool(saved.get("enabled", True)) if saved else True
        health_row = health.get(integration_id) or {}
        health_status = str(health_row.get("health_status") or health_row.get("state") or "").strip().lower()
        healthy = health_row.get("healthy")
        if healthy is None and health_status:
            healthy = health_status in {"healthy", "connected", "available", "ok"}
        if not configured:
            health_status = "configuration_needed"
            healthy = None
        elif not enabled:
            health_status = "disabled"
            healthy = None
        elif not health_status:
            health_status = "unknown"
        base_url_value = sqlite_url_value or env_url_value
        rows.append({
            "integration_id": integration_id,
            "display_name": spec["display_name"],
            "category": spec["category"],
            "implemented": spec["implemented"],
            "certification_tier": spec["certification_tier"],
            "configured": configured,
            "enabled": enabled,
            "configuration_source": configuration_source,
            "secret_present": secret_present,
            "base_url_present": bool(url_present or (default_configured and integration_id in {"mangadex", "rss_direct", "local_manual_inbox"})),
            "base_url_host": _safe_host(base_url_value),
            "healthy": healthy,
            "health_status": health_status,
            "last_test_at": health_row.get("last_test_at"),
            "last_success_at": health_row.get("last_success_at"),
            "last_failure_at": health_row.get("last_failure_at"),
            "configuration_needed": bool(spec["implemented"] and not configured),
            "restart_required": bool(health_row.get("restart_required", False)),
            "disabled_reason": None if enabled else str(health_row.get("disabled_reason") or "disabled_by_user"),
            "capabilities": list(spec["capabilities"]),
            "content_fit": list(CONTENT_FIT.get(integration_id, ())),
            "test_capability": "test" in spec["capabilities"],
            "metadata_capabilities": [
                capability
                for capability in spec["capabilities"]
                if capability in {"metadata_search", "series_catalog", "chapter_catalog"}
            ],
            "configure_target": spec["configure_target"],
        })
    return {
        "schema": "inkdrop.effective_integrations.v1",
        "configuration_precedence": ["sqlite", "environment", "secret_reference", "legacy_compatibility", "default", "none"],
        "integrations": rows,
    }


def canonicalize_provider_rows(providers, effective_by_id=None):
    """Deduplicate known aliases while preferring the canonical provider row."""
    effective_by_id = effective_by_id if isinstance(effective_by_id, dict) else {}
    deduped = {}
    order = []
    for provider in providers or []:
        if not isinstance(provider, dict):
            continue
        original_id = str(provider.get("id") or provider.get("provider_id") or "").strip()
        canonical_id = canonical_integration_id(original_id)
        dedupe_key = canonical_id if canonical_id in INTEGRATIONS else original_id.lower()
        item = dict(provider)
        item["_effective_original_id"] = original_id
        if canonical_id in effective_by_id:
            item["id"] = canonical_id
            item["provider_id"] = canonical_id
            item["effective_configuration"] = effective_by_id[canonical_id]
            for field in (
                "configured", "enabled", "configuration_source", "secret_present",
                "base_url_present", "healthy", "health_status", "configuration_needed",
                "restart_required", "disabled_reason", "certification_tier", "configure_target",
                "content_fit", "test_capability", "metadata_capabilities",
            ):
                item[field] = effective_by_id[canonical_id].get(field)
            settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
            policy = settings.get("policy") if isinstance(settings.get("policy"), dict) else {}
            item["priority"] = item.get("priority") or policy.get("priority") or settings.get("priority") or 100
            item["health"] = {
                "healthy": effective_by_id[canonical_id].get("healthy"),
                "status": effective_by_id[canonical_id].get("health_status"),
            }
        existing = deduped.get(dedupe_key)
        if existing is None:
            deduped[dedupe_key] = item
            order.append(dedupe_key)
            continue
        existing_is_canonical = str(existing.get("_effective_original_id") or "").strip().lower() == dedupe_key
        item_is_canonical = original_id.lower() == dedupe_key
        if item_is_canonical and not existing_is_canonical:
            deduped[dedupe_key] = item
    result = []
    for key in order:
        item = deduped[key]
        item.pop("_effective_original_id", None)
        result.append(item)
    return result
