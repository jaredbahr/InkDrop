"""Read-only source registry decisions derived from InkDrop provider settings."""

from __future__ import annotations

import json
from pathlib import Path

from core import inkdrop_sources

try:
    from core import inkdrop_source_catalog
except Exception:
    inkdrop_source_catalog = None


ACQUISITION_PROVIDER_TYPES = {
    "direct_download",
    "download_source",
    "indexer",
    "metadata_download_source",
}

MANUAL_SOURCE_PROVIDER_TYPES = {
    "source",
}

DEFAULT_SOURCE_MODE_BY_TYPE = {
    "direct_download": "auto",
    "download_source": "auto",
    "indexer": "auto",
    "metadata_download_source": "auto",
    "metadata": "metadata_only",
}

SOURCE_ORDER_SETTING_KEY = "automation.source_order"

PROVIDER_HEALTH_SETTING_KEYS = (
    "health_provider_ids",
    "health_provider_id",
    "upstream_provider_ids",
    "upstream_provider_id",
)

PARENT_HEALTH_BY_SOURCE_KIND = {
    "prowlarr_indexer": "prowlarr",
    "prowlarr_or_torrent_indexer": "prowlarr",
    "rss_direct_feed": "rss",
    "rss_detail_direct_feed": "rss",
    "rss_detail_probe_feed": "rss",
    "rss_reader_page_pack_feed": "rss",
    "suwayomi_api_page_provider": "suwayomi",
    "suwayomi_managed_folder_source": "suwayomi",
    "torznab_indexer": "torznab_indexer",
    "newznab_indexer": "newznab_indexers",
    "torrent_html_search": "torrent_html_sources",
}

PARENT_CONFIG_BY_SOURCE_KIND = {
    "prowlarr_indexer": "prowlarr",
    "prowlarr_or_torrent_indexer": "prowlarr",
}

BUILTIN_PROVIDER_POLICY_DEFAULTS = {
    "prowlarr": {
        "categories": ["7030", "8000", "8010"],
        "minimum_seeders": 1,
        "download_category": "inkdrop",
        "download_client_by_protocol": {"torrent": "qbittorrent", "usenet": "sabnzbd"},
        "health_provider_ids": ["prowlarr", "qbittorrent", "sabnzbd"],
        "weekly_pack_query_limit": 2,
        "requires_manual_confirm": False,
    },
}

POLICY_SETTING_OVERRIDE_KEYS = (
    "allowed_extensions",
    "allowed_content_types",
    "allowed_languages",
    "allowed_image_extensions",
    "allow_at_home_page_pack",
    "allow_packs",
    "allowed_pack_detail_hosts",
    "allowed_shared_file_hosts",
    "auto_stage_tool_output",
    "categories",
    "categoryless_fallback_indexer_ids",
    "command_args",
    "command_argv",
    "command_env",
    "command_executable",
    "command_timeout_seconds",
    "content_ratings",
    "disable_pack_detail_fetch",
    "disable_weekly_pack_queries",
    "download_category",
    "download_client_by_protocol",
    "enable_at_home_page_pack",
    "extract_script_image_urls",
    "fetch_at_home_pages",
    "fresh_release_max_age_days",
    "fresh_release_only",
    "health_provider_ids",
    "historical_weekly_pack_date_query_limit",
    "import_handoff_expectation",
    "indexer_id",
    "indexer_ids",
    "indexer_allowed_hosts",
    "indexer_max_query_variants",
    "indexer_pack_detail_allowed_hosts",
    "indexer_weekly_pack_query_limit",
    "list_pagination_url_templates",
    "list_url_templates",
    "mangadex_page_quality",
    "manual_inbox_cleanup_policy",
    "manual_inbox_copy_policy",
    "manual_inbox_max_scan_files",
    "manual_inbox_min_age_seconds",
    "manual_inbox_root",
    "manual_inbox_staging_root",
    "max_at_home_chapters",
    "max_bytes",
    "max_detail_pages",
    "max_pack_detail_fetches",
    "max_probe_links",
    "max_redirects",
    "max_query_variants",
    "max_reader_pages",
    "max_search_pages",
    "max_series_pages",
    "suwayomi_allowed_hosts",
    "suwayomi_allow_nsfw_sources",
    "suwayomi_disabled_source_ids",
    "suwayomi_disabled_source_names",
    "suwayomi_max_chapters",
    "suwayomi_max_manga_matches",
    "suwayomi_max_query_variants",
    "suwayomi_max_source_count",
    "suwayomi_page_base_url",
    "suwayomi_page_image_extension",
    "suwayomi_rest_chapter_fallback_enabled",
    "suwayomi_search_page",
    "suwayomi_skip_obsolete_sources",
    "suwayomi_skip_source_after_search_error",
    "suwayomi_skip_sources_with_updates",
    "suwayomi_folder_cleanup_policy",
    "suwayomi_folder_copy_policy",
    "suwayomi_folder_import_ready_enabled",
    "suwayomi_folder_max_scan_files",
    "suwayomi_folder_metadata_providers",
    "suwayomi_folder_min_age_seconds",
    "suwayomi_folder_mode",
    "suwayomi_folder_path_patterns",
    "suwayomi_download_root",
    "suwayomi_import_staging_root",
    "suwayomi_source_error_cooldown_enabled",
    "suwayomi_source_error_cooldown_probe_after_seconds",
    "suwayomi_source_error_cooldown_probe_enabled",
    "suwayomi_source_error_cooldown_probe_max_sources",
    "suwayomi_source_error_cooldown_seconds",
    "suwayomi_source_error_cooldown_threshold",
    "suwayomi_volume_gap_cooldown_enabled",
    "suwayomi_volume_gap_cooldown_seconds",
    "suwayomi_volume_gap_cooldown_threshold",
    "suwayomi_volume_metadata_gap_cooldown_threshold",
    "suwayomi_volume_gap_cooldown_max_sources",
    "suwayomi_volume_gap_cooldown_probe_enabled",
    "suwayomi_volume_gap_cooldown_probe_after_seconds",
    "suwayomi_volume_gap_cooldown_probe_max_sources",
    "suwayomi_source_error_quarantine_enabled",
    "suwayomi_source_error_quarantine_max_sources",
    "suwayomi_source_error_quarantine_seconds",
    "suwayomi_source_error_quarantine_threshold",
    "suwayomi_source_ids",
    "suwayomi_source_names",
    "max_weekly_pack_queries",
    "minimum_seeders",
    "comic_series_fallback_queries_enabled",
    "disable_comic_series_fallback_queries",
    "pack_auto_allowed",
    "pack_detail_allowed_hosts",
    "pack_detail_fetch",
    "pack_detail_max_bytes",
    "pack_detail_max_fetches",
    "pack_detail_sidecar_max_bytes",
    "packs_allowed",
    "pagination_url_templates",
    "probe_method",
    "result_title_allow_patterns",
    "result_title_deny_patterns",
    "result_url_allow_patterns",
    "result_url_deny_patterns",
    "rss_backfill_allowed",
    "rss_fresh_release_max_age_days",
    "rss_fresh_release_only",
    "search_api_flavor",
    "search_api_flavors",
    "search_url_templates",
    "secret_env",
    "secret_env_var",
    "query_aliases",
    "series_query_aliases",
    "series_title_aliases",
    "shared_file_host_rules",
    "shared_file_hosts",
    "source_allowed_hosts",
    "source_site_label",
    "staged_output_root",
    "timeout_seconds",
    "torrent_download_client",
    "usenet_download_client",
    "verify_timeout_seconds",
    "title_query_aliases",
    "enable_volume_page_pack",
    "weekly_pack_query_limit",
    "weekly_pack_queries_enabled",
    "weekly_pack_historical_date_query_limit",
    "working_directory",
    "volume_page_pack_max_chapters",
    "volume_page_pack_min_chapters",
)

POLICY_SETTING_FORCE_OVERRIDE_KEYS = {
    "suwayomi_disabled_source_ids",
    "suwayomi_disabled_source_names",
    "query_aliases",
    "series_query_aliases",
    "series_title_aliases",
    "title_query_aliases",
}


def _settings_policy_override_allowed(value, *, key_missing=False, force=False, user_owned=False):
    if value in (None, ""):
        return False
    if key_missing or force:
        return True
    if not user_owned:
        return False
    if value in ([], {}):
        return False
    return True


def is_searchable_provider_type(provider_type, source_template=False):
    provider_type = str(provider_type or "").strip().lower()
    return provider_type in ACQUISITION_PROVIDER_TYPES or (
        source_template and provider_type in MANUAL_SOURCE_PROVIDER_TYPES
    )


def is_download_capable_provider_type(provider_type):
    return str(provider_type or "").strip().lower() in ACQUISITION_PROVIDER_TYPES


def _settings(provider):
    provider = provider if isinstance(provider, dict) else {}
    settings = provider.get("settings")
    return settings if isinstance(settings, dict) else {}


def _boolish(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _intish(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def _setting_value_map(snapshot):
    out = {}
    for setting in (snapshot or {}).get("settings") or []:
        if not isinstance(setting, dict):
            continue
        key = str(setting.get("key") or "").strip()
        if key:
            out[key] = setting.get("value")
    return out


def _listish(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str) and "," in value:
        return [part.strip() for part in value.split(",")]
    return [value]


def _clone_jsonish(value):
    try:
        return json.loads(json.dumps(value))
    except Exception:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, list):
            return list(value)
        return value


def _missing_policy_defaults(policy, defaults):
    policy = dict(policy or {})
    defaults = defaults if isinstance(defaults, dict) else {}
    for key, value in defaults.items():
        if key not in policy:
            policy[key] = _clone_jsonish(value)
    return policy


def _catalog_policy_defaults(provider_id):
    if inkdrop_source_catalog is None:
        return {}
    try:
        entry = inkdrop_source_catalog.provider_entry(provider_id) or {}
    except Exception:
        return {}
    policy = entry.get("policy") if isinstance(entry.get("policy"), dict) else {}
    return policy


def _merge_policy_defaults(provider_id, policy, *, source_template=False):
    policy = dict(policy or {})
    policy = _missing_policy_defaults(policy, _catalog_policy_defaults(provider_id))
    if source_template:
        return policy
    defaults = BUILTIN_PROVIDER_POLICY_DEFAULTS.get(provider_id) or {}
    return _missing_policy_defaults(policy, defaults)



def _health_problem(health):
    health = health if isinstance(health, dict) else {}
    state = str(health.get("state") or "").strip().lower()
    if state not in inkdrop_sources.PROVIDER_HEALTH_PROBLEM_STATES:
        return False
    scope = str(health.get("health_scope") or health.get("scope") or "").strip().lower()
    if scope in {"download_history", "download_history_only"}:
        return False
    return True


def health_provider_ids_for_provider(provider_or_row):
    provider_or_row = provider_or_row if isinstance(provider_or_row, dict) else {}
    settings = _settings(provider_or_row)
    policy = settings.get("policy") if isinstance(settings.get("policy"), dict) else provider_or_row.get("policy")
    policy = policy if isinstance(policy, dict) else {}
    provider_id = inkdrop_sources.provider_key(
        provider_or_row.get("provider_id") or provider_or_row.get("id")
    )
    source_kind = str(settings.get("source_kind") or provider_or_row.get("source_kind") or "").strip()
    candidates = []
    for key in PROVIDER_HEALTH_SETTING_KEYS:
        candidates.extend(_listish(settings.get(key)))
        candidates.extend(_listish(policy.get(key)))
    parent = PARENT_HEALTH_BY_SOURCE_KIND.get(source_kind)
    if parent:
        candidates.append(parent)
    if provider_id.startswith("prowlarr_"):
        candidates.append("prowlarr")
    if provider_id.startswith("rss_"):
        candidates.append("rss")
    candidates.append(provider_id)
    out = []
    seen = set()
    for value in candidates:
        key = inkdrop_sources.provider_key(value)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def apply_provider_health_to_row(row, provider_health_map=None):
    row = dict(row) if isinstance(row, dict) else {}
    provider_health_map = provider_health_map if isinstance(provider_health_map, dict) else {}
    health_ids = health_provider_ids_for_provider(row)
    found = None
    problem = None
    for provider_id in health_ids:
        health = provider_health_map.get(provider_id)
        if not isinstance(health, dict):
            continue
        shaped = dict(health)
        shaped["provider_id"] = shaped.get("provider_id") or provider_id
        if _health_problem(shaped):
            problem = shaped
            break
        if found is None:
            found = shaped
    health = problem or found
    row["health_provider_ids"] = health_ids
    if health:
        state = str(health.get("state") or "").strip().lower()
        label = health.get("label") or health.get("health_label") or state
        detail = health.get("detail") or health.get("message") or ""
        row["provider_health"] = health
        row["provider_health_state"] = state
        row["provider_health_label"] = label
        row["provider_health_detail"] = detail
        row["provider_health_problem"] = bool(problem)
        if problem:
            reasons = list(row.get("block_reasons") or [])
            if "provider_health_problem" not in reasons:
                reasons.append("provider_health_problem")
            row["block_reasons"] = reasons
            row["provider_health_reason"] = "provider_health_problem"
    else:
        row["provider_health_problem"] = False
    return row


def source_order_from_snapshot(snapshot):
    values = _setting_value_map(snapshot)
    raw = values.get(SOURCE_ORDER_SETTING_KEY)
    if isinstance(raw, str):
        return [inkdrop_sources.provider_key(part.strip()) for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [inkdrop_sources.provider_key(part) for part in raw if str(part or "").strip()]
    return []


def source_mode(provider):
    provider = provider if isinstance(provider, dict) else {}
    settings = _settings(provider)
    explicit = str(settings.get("source_mode") or "").strip().lower()
    if explicit:
        return explicit
    provider_type = str(provider.get("provider_type") or "").strip().lower()
    return DEFAULT_SOURCE_MODE_BY_TYPE.get(provider_type, "disabled")


def implementation_status(provider):
    provider = provider if isinstance(provider, dict) else {}
    settings = _settings(provider)
    explicit = str(settings.get("implementation_status") or "").strip().lower()
    if explicit:
        return explicit
    return "planned" if settings.get("source_template") is True else "implemented"


def provider_priority(provider, source_order=None):
    settings = _settings(provider)
    if settings.get("priority") not in (None, ""):
        return _intish(settings.get("priority"), 100)
    provider_id = inkdrop_sources.provider_key((provider or {}).get("id"))
    source_order = list(source_order or [])
    if provider_id in source_order:
        return 20 + source_order.index(provider_id)
    return 100


def _block_reasons(provider):
    provider = provider if isinstance(provider, dict) else {}
    settings = _settings(provider)
    mode = source_mode(provider)
    status = implementation_status(provider)
    provider_type = str(provider.get("provider_type") or "").strip().lower()
    enabled = bool(provider.get("enabled"))
    source_template = settings.get("source_template") is True
    user_enableable = settings.get("user_enableable", True) is not False
    reasons = []
    if not is_searchable_provider_type(provider_type, source_template):
        reasons.append("not_acquisition_provider")
    if not enabled:
        reasons.append("disabled")
    if mode == "disabled":
        reasons.append("source_mode_disabled")
    if source_template and not user_enableable:
        reasons.append("disabled_boundary")
    if status != "implemented":
        reasons.append("implementation_pending")
    return reasons


def registry_state(provider):
    reasons = _block_reasons(provider)
    mode = source_mode(provider)
    if "disabled_boundary" in reasons:
        return "disabled_boundary"
    if "disabled" in reasons or "source_mode_disabled" in reasons:
        return "disabled"
    if "implementation_pending" in reasons:
        return "planned"
    if "not_acquisition_provider" in reasons:
        return "non_acquisition"
    if mode == "manual_review":
        return "manual_review"
    if mode == "metadata_only":
        return "metadata_only"
    if mode == "assist":
        return "assist"
    return "ready"


def registry_entry(provider, *, source_order=None):
    provider = provider if isinstance(provider, dict) else {}
    settings = _settings(provider)
    provider_id = inkdrop_sources.provider_key(provider.get("id"))
    provider_type = str(provider.get("provider_type") or "").strip().lower() or "source"
    mode = source_mode(provider)
    status = implementation_status(provider)
    state = registry_state(provider)
    block_reasons = _block_reasons(provider)
    implemented = status == "implemented"
    enabled = bool(provider.get("enabled"))
    source_template = settings.get("source_template") is True
    searchable_provider = is_searchable_provider_type(provider_type, source_template)
    download_capable_provider = is_download_capable_provider_type(provider_type)
    policy = _merge_policy_defaults(
        provider_id,
        settings.get("policy") or {},
        source_template=source_template,
    )
    user_owned_settings = str(provider.get("source") or "").strip().lower() == "user"
    for policy_key in POLICY_SETTING_OVERRIDE_KEYS:
        value = settings.get(policy_key)
        if (
            policy_key in settings
            and _settings_policy_override_allowed(
                value,
                key_missing=policy_key not in policy,
                force=policy_key in POLICY_SETTING_FORCE_OVERRIDE_KEYS,
                user_owned=user_owned_settings,
            )
        ):
            policy[policy_key] = value
    manual_confirm = settings.get("requires_manual_confirm")
    if manual_confirm is None:
        manual_confirm = policy.get("requires_manual_confirm")
    requires_manual = (
        mode == "manual_review"
        or _boolish(manual_confirm, False)
        or state == "manual_review"
    )
    auto_config_allowed = settings.get("auto_download_allowed")
    if auto_config_allowed is None and not source_template:
        auto_config_allowed = download_capable_provider
    auto_config_allowed = _boolish(auto_config_allowed, False)
    mangadex_at_home_auto_opt_in = bool(
        provider_id == "mangadex"
        and mode == "auto"
        and _boolish(settings.get("fetch_at_home_pages", policy.get("fetch_at_home_pages")), False)
        and not requires_manual
    )
    if provider_id == "mangadex":
        auto_config_allowed = mangadex_at_home_auto_opt_in
    auto_search_allowed = bool(
        enabled
        and implemented
        and searchable_provider
        and mode in {"auto", "assist", "manual_review"}
        and state not in {"disabled", "disabled_boundary", "planned", "non_acquisition"}
    )
    auto_download_allowed = bool(
        auto_search_allowed
        and download_capable_provider
        and mode == "auto"
        and auto_config_allowed
        and not requires_manual
    )
    manual_review_allowed = bool(
        enabled
        and implemented
        and searchable_provider
        and mode in {"assist", "manual_review"}
        and state not in {"disabled", "disabled_boundary", "planned", "non_acquisition"}
    )
    base_url = provider.get("base_url")
    if base_url in (None, ""):
        base_url = settings.get("base_url")
    base_url = str(base_url or "").strip()
    provider_kind = str(settings.get("source_kind") or "").strip().lower()
    if (
        provider_id == "rss"
        or provider_id.startswith("rss_")
        or provider_kind in {"rss_direct_feed", "rss_detail_direct_feed", "rss_detail_probe_feed", "rss_reader_page_pack_feed"}
    ) and settings.get("feed_url") not in (None, ""):
        base_url = settings.get("feed_url")
    secret_ref = provider.get("secret_ref")
    if secret_ref in (None, ""):
        secret_ref = settings.get("secret_ref")
    secret_ref = str(secret_ref or "").strip()
    return {
        "provider_id": provider_id,
        "display_name": provider.get("display_name") or inkdrop_sources.provider_label(provider_id),
        "provider_type": provider_type,
        "settings_group": provider.get("settings_group"),
        "enabled": enabled,
        "base_url": base_url,
        "secret_ref": secret_ref,
        "source": provider.get("source"),
        "source_template": source_template,
        "source_kind": str(settings.get("source_kind") or ""),
        "source_mode": mode,
        "implementation_status": status,
        "registry_state": state,
        "block_reasons": block_reasons,
        "priority": provider_priority(provider, source_order),
        "media_types": list(settings.get("media_types") or []),
        "capabilities": list(provider.get("capabilities") or []),
        "risk_class": str(settings.get("risk_class") or ""),
        "integration_class": str(settings.get("integration_class") or ""),
        "auto_download_config_allowed": auto_config_allowed,
        "auto_search_allowed": auto_search_allowed,
        "auto_download_allowed": auto_download_allowed,
        "manual_review_allowed": manual_review_allowed,
        "requires_manual_review": requires_manual,
        "user_enableable": settings.get("user_enableable", True) is not False,
        "enablement_guard": settings.get("enablement_guard"),
        "rights_gate": settings.get("rights_gate"),
        "direct_url_policy": settings.get("direct_url_policy"),
        "allowed_extensions": list(settings.get("allowed_extensions") or []),
        "download_root": str(settings.get("download_root") or "").strip(),
        "incomplete_root": str(settings.get("incomplete_root") or "").strip(),
        "minimum_seeders": settings.get("minimum_seeders"),
        "search_url_templates": _listish(settings.get("search_url_templates")),
        "policy": policy,
        "health_provider_ids": health_provider_ids_for_provider(provider),
        "source_attempt_seed": {
            "provider_id": provider_id,
            "source": provider_id,
            "source_type": provider_type,
            "provider_mode": mode,
            "risk_class": str(settings.get("risk_class") or ""),
            "auto_download_allowed": auto_download_allowed,
            "requires_manual_review": requires_manual,
            "implementation_status": status,
            "registry_state": state,
        },
    }


def _registry_state_rank(state):
    return {
        "ready": 6,
        "assist": 5,
        "manual_review": 4,
        "metadata_only": 3,
        "planned": 2,
        "disabled": 1,
        "disabled_boundary": 0,
        "non_acquisition": 0,
    }.get(str(state or "").strip().lower(), 0)


def _provider_type_rank(provider_type):
    return {
        "indexer": 8,
        "direct_download": 7,
        "metadata_download_source": 6,
        "download_source": 4,
        "source": 2,
    }.get(str(provider_type or "").strip().lower(), 0)


def _provider_source_rank(source):
    source = str(source or "").strip().lower()
    if source == "user":
        return 8
    if source == "runtime":
        return 7
    if source == "source_catalog":
        return 6
    if source in {"health", "activity"}:
        return 3
    if source == "state_memory":
        return 1
    return 4


def _adapter_config_rank(row):
    row = row if isinstance(row, dict) else {}
    score = 0
    if str(row.get("source_kind") or "").strip():
        score += 8
    if row.get("base_url") not in (None, ""):
        score += 4
    if row.get("secret_ref") not in (None, ""):
        score += 2
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    if policy.get("indexer_ids"):
        score += 2
    return score


def _registry_dedupe_key(row):
    row = row if isinstance(row, dict) else {}
    try:
        priority = int(row.get("priority") or 100)
    except Exception:
        priority = 100
    return (
        _provider_source_rank(row.get("source")),
        _adapter_config_rank(row),
        _provider_type_rank(row.get("provider_type")),
        _registry_state_rank(row.get("registry_state")),
        1 if row.get("auto_download_allowed") else 0,
        1 if row.get("auto_search_allowed") else 0,
        -priority,
    )


def _dedupe_registry_rows(rows):
    by_id = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        provider_id = str(row.get("provider_id") or "").strip()
        if not provider_id:
            continue
        current = by_id.get(provider_id)
        if current is None or _registry_dedupe_key(row) > _registry_dedupe_key(current):
            by_id[provider_id] = row
    return list(by_id.values())


def registry_from_settings_snapshot(
    snapshot,
    *,
    include_non_acquisition=False,
    include_disabled=True,
    provider_health_map=None,
):
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    source_order = source_order_from_snapshot(snapshot)
    providers = [provider for provider in (snapshot.get("providers") or []) if isinstance(provider, dict)]
    provider_by_id = {
        inkdrop_sources.provider_key(provider.get("id")): provider
        for provider in providers
    }
    rows = []
    for provider in providers:
        row = apply_provider_health_to_row(
            registry_entry(provider, source_order=source_order),
            provider_health_map=provider_health_map,
        )
        parent_id = PARENT_CONFIG_BY_SOURCE_KIND.get(str(row.get("source_kind") or "").strip().lower())
        parent = provider_by_id.get(parent_id) if parent_id else None
        if parent:
            parent_settings = _settings(parent)
            if row.get("base_url") in (None, ""):
                row["base_url"] = str(parent.get("base_url") or parent_settings.get("base_url") or "").strip()
            if row.get("secret_ref") in (None, ""):
                row["secret_ref"] = str(parent.get("secret_ref") or parent_settings.get("secret_ref") or "").strip()
        if not include_non_acquisition and not is_searchable_provider_type(
            row["provider_type"],
            row["source_template"],
        ):
            continue
        if not include_disabled and row["registry_state"] in {"disabled", "disabled_boundary"}:
            continue
        rows.append(row)
    rows = _dedupe_registry_rows(rows)
    return sorted(rows, key=lambda row: (row["priority"], row["display_name"].lower(), row["provider_id"]))


def registry_from_db(db_path, *, include_non_acquisition=False, include_disabled=True):
    from core import inkdrop_state

    snapshot = inkdrop_state.settings_snapshot(Path(db_path))
    provider_health_map = {}
    with inkdrop_state.connect_read(db_path) as con:
        if inkdrop_state.table_exists(con, "history_events"):
            provider_health_map = inkdrop_state.latest_provider_health_map(con)
    return registry_from_settings_snapshot(
        snapshot,
        include_non_acquisition=include_non_acquisition,
        include_disabled=include_disabled,
        provider_health_map=provider_health_map,
    )


def enabled_registry_entries(snapshot, *, states=None):
    states = set(states or {"ready", "assist", "manual_review", "metadata_only"})
    return [
        row
        for row in registry_from_settings_snapshot(snapshot, include_disabled=False)
        if row["registry_state"] in states and row["enabled"]
    ]


def registry_summary(rows):
    rows = list(rows or [])
    counts = {}
    for row in rows:
        state = str((row or {}).get("registry_state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return {
        "total": len(rows),
        "by_state": dict(sorted(counts.items())),
        "auto_search": sum(1 for row in rows if row.get("auto_search_allowed")),
        "auto_download": sum(1 for row in rows if row.get("auto_download_allowed")),
        "manual_review": sum(1 for row in rows if row.get("manual_review_allowed")),
    }


def registry_json(rows):
    return json.dumps(list(rows or []), ensure_ascii=False, sort_keys=True, indent=2)
