import copy
import json
from pathlib import Path

from core import inkdrop_sources


CATALOG_PATH = Path(__file__).resolve().parents[1] / "docs" / "inkdrop-source-candidate-catalog-20260702.json"

SOURCE_KIND_PROVIDER_TYPES = {
    "archive_item_api": "direct_download",
    "disabled_bucket": "source",
    "do_not_automate_bucket": "source",
    "direct_download_site": "direct_download",
    "direct_file_detail_search": "direct_download",
    "direct_file_html_search": "direct_download",
    "direct_file_probe_source": "direct_download",
    "external_tool_bridge": "download_source",
    "future_catalog_bucket": "source",
    "future_media_bucket": "source",
    "html_search_source": "source",
    "json_api_direct_catalog": "direct_download",
    "json_direct_source": "direct_download",
    "manual_inbox_source": "download_source",
    "local_dht_search": "indexer",
    "manual_bucket": "source",
    "manga_api_page_provider": "metadata_download_source",
    "metadata_api": "metadata",
    "metadata_catalog": "metadata",
    "newznab_indexer": "indexer",
    "opds_acquisition_catalog": "direct_download",
    "opds_direct_catalog": "direct_download",
    "out_of_scope_bucket": "source",
    "prowlarr_indexer": "indexer",
    "prowlarr_or_torrent_indexer": "indexer",
    "public_text_catalog": "metadata",
    "reader_page_pack_source": "direct_download",
    "rss_reader_page_pack_feed": "direct_download",
    "suwayomi_api_page_provider": "metadata_download_source",
    "suwayomi_managed_folder_source": "download_source",
    "rss_detail_probe_feed": "direct_download",
    "rss_detail_direct_feed": "direct_download",
    "rss_direct_feed": "direct_download",
    "rss_direct_source": "direct_download",
    "torznab_indexer": "indexer",
    "torrent_detail_rss_feed": "indexer",
    "torrent_html_search": "indexer",
    "torrent_detail_search": "indexer",
    "torrent_rss_feed": "indexer",
}

MODE_STATUS = {
    "auto": "available",
    "assist": "configured",
    "manual_review": "manual_review",
    "metadata_only": "configured",
    "disabled": "disabled",
}

MODE_ENABLED_DEFAULT = {
    "auto": True,
    "assist": True,
    "manual_review": True,
    "metadata_only": True,
    "disabled": False,
}

SETTINGS_GROUP_BY_PROVIDER_TYPE = {
    "direct_download": "download_sources",
    "download_source": "download_sources",
    "indexer": "indexers",
    "metadata": "metadata",
    "metadata_download_source": "download_sources",
    "source": "source_templates",
}

AUTOMATION_ROLE_BY_MODE = {
    "auto": "Auto-capable source template",
    "assist": "Assisted source template",
    "manual_review": "Manual-review source template",
    "metadata_only": "Metadata-only source template",
    "disabled": "Disabled source boundary",
}

BASE_TEMPLATE_EDITABLE_FIELDS = ["source_mode", "priority"]
IMPLEMENTATION_STATUS_VALUES = {"planned", "implemented"}
TEMPLATE_VISIBILITY_VALUES = {"standard", "advanced", "legacy"}

PRODUCT_DIRECT_SOURCE_IDS = {
    "generic_rss_direct_feed",
    "rss_getcomics",
    "suwayomi_managed_folder",
    "generic_safe_http_direct_download",
    "local_manual_inbox",
}

VAGUE_DIRECT_SOURCE_BUCKET_IDS = {
    "generic_rss_detail_direct_feed",
    "generic_rss_detail_probe_feed",
    "generic_rss_reader_page_pack_feed",
    "generic_direct_file_search",
    "generic_direct_file_detail_search",
    "generic_direct_file_probe_source",
    "generic_reader_page_pack_source",
    "generic_json_direct_source",
    "manual_reader_sites",
    "manual_ddl_blogs",
    "manual_search_engines",
    "public_free_book_sites",
    "shadow_libraries",
}

DIRECT_SOURCE_CERTIFICATIONS = {
    "generic_rss_direct_feed": {
        "certification": "Beta",
        "surface": "Generic RSS enclosure/direct-link source",
        "evidence": "Feed parsing emits direct candidates from enclosures or explicit item links; direct handoff uses shared artifact gates.",
        "required_gates": [
            "configuration",
            "feed_test",
            "discovery",
            "deduplication",
            "candidate_identity",
            "safe_url_validation",
            "bounded_download",
            "content_type_size_validation",
            "archive_validation",
            "retry",
            "import_handoff",
            "evidence_history",
            "end_to_end_fixture",
        ],
    },
    "rss_getcomics": {
        "certification": "Experimental",
        "surface": "RSS/GetComics",
        "evidence": "A network-free GetComics detail -> Pixeldrain probe -> guarded download -> import fixture exists; live provider acceptance and rights approval remain required.",
        "required_gates": [
            "configuration",
            "feed_test",
            "discovery",
            "bounded_detail_discovery",
            "deduplication",
            "candidate_identity",
            "safe_url_validation",
            "bounded_download",
            "content_type_size_validation",
            "archive_validation",
            "retry",
            "import_handoff",
            "evidence_history",
            "end_to_end_fixture",
        ],
        "blocked_gates": ["live_provider_acceptance"],
    },
    "suwayomi_managed_folder": {
        "certification": "Beta",
        "surface": "Suwayomi managed-folder intake",
        "evidence": "Managed folder scanner stages local files through guarded copy-to-staging/import-ready evidence.",
        "required_gates": [
            "configuration",
            "path_test",
            "discovery",
            "deduplication",
            "candidate_identity",
            "safe_path_validation",
            "bounded_scan",
            "archive_validation",
            "retry",
            "import_handoff",
            "evidence_history",
            "end_to_end_fixture",
        ],
    },
    "generic_safe_http_direct_download": {
        "certification": "Beta",
        "surface": "Generic safe HTTP direct download",
        "evidence": "Shared direct downloader validates URL/path, content type, size, redirects, archive-like extension, bounded bytes, sidecar evidence, and import-ready handoff.",
        "required_gates": [
            "configuration",
            "test_connection",
            "discovery",
            "deduplication",
            "candidate_identity",
            "safe_url_path_validation",
            "bounded_download",
            "content_type_size_validation",
            "archive_validation",
            "retry",
            "import_handoff",
            "evidence_history",
            "end_to_end_fixture",
        ],
    },
    "local_manual_inbox": {
        "certification": "Beta",
        "surface": "Local/manual inbox",
        "evidence": "Local/manual staging uses filesystem paths, guarded import-ready rows, retry/evidence history, and no remote download automation.",
        "required_gates": [
            "configuration",
            "path_test",
            "discovery",
            "deduplication",
            "candidate_identity",
            "safe_path_validation",
            "bounded_scan",
            "archive_validation",
            "retry",
            "import_handoff",
            "evidence_history",
            "end_to_end_fixture",
        ],
    },
}


def _catalog_path(path=None):
    return Path(path) if path else CATALOG_PATH


# load_catalog()/provider_map()/provider_entry() used to re-read, re-parse,
# and deep-copy the whole catalog on every call -- including once per
# registry entry inside the source-worker's per-queue-row planning loop.
# With a few hundred provider rows that meant tens of thousands of full
# 54-candidate deep copies for a single scheduling pass. Cache the parsed
# catalog and a normalized alias index keyed by (path, mtime, size) so a
# lookup only copies the one entry it returns; touching the catalog file on
# disk (tests, catalog edits) still invalidates it.
_CATALOG_CACHE = {}


def _catalog_cache_key(resolved_path):
    try:
        stat = resolved_path.stat()
        return (str(resolved_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (str(resolved_path), None, None)


def _cached_catalog(path=None):
    resolved_path = _catalog_path(path)
    cache_key = _catalog_cache_key(resolved_path)
    cached = _CATALOG_CACHE.get(str(resolved_path))
    if cached is not None and cached[0] == cache_key:
        return cached[1]
    with resolved_path.open("r", encoding="utf-8") as handle:
        catalog = json.load(handle)
    candidates = list(catalog.get("provider_candidates") or [])
    # Keyed by each entry's own raw "id" only -- matching provider_map()'s
    # original {id: entry} shape exactly. provider_entry() still normalizes
    # the *lookup* key on the caller's side; indexing catalog ids under their
    # own normalized/alias forms too would let a lookup match a different
    # entry than the un-cached linear scan ever could.
    alias_index = {
        str(provider.get("id") or "").strip(): provider
        for provider in candidates
        if str(provider.get("id") or "").strip()
    }
    entry = {"catalog": catalog, "candidates": candidates, "alias_index": alias_index}
    _CATALOG_CACHE[str(resolved_path)] = (cache_key, entry)
    return entry


def reset_catalog_cache():
    """Drop the cached catalog. Tests/dev tooling call this after editing the file on disk."""

    _CATALOG_CACHE.clear()


def load_catalog(path=None):
    """Return a copy of the structured source catalog."""
    return copy.deepcopy(_cached_catalog(path)["catalog"])


def catalog_version(path=None):
    return int(_cached_catalog(path)["catalog"].get("catalog_version") or 0)


def mode_definitions(path=None):
    return dict(_cached_catalog(path)["catalog"].get("mode_definitions") or {})


def implementation_order(path=None):
    return list(_cached_catalog(path)["catalog"].get("implementation_order") or [])


def provider_candidates(path=None):
    return copy.deepcopy(_cached_catalog(path)["candidates"])


def product_provider_candidates(path=None):
    return [
        entry
        for entry in provider_candidates(path)
        if inkdrop_sources.provider_key(entry.get("id")) not in VAGUE_DIRECT_SOURCE_BUCKET_IDS
        and template_visibility(entry) != "legacy"
    ]


def provider_map(path=None):
    return {
        provider.get("id"): copy.deepcopy(provider)
        for provider in _cached_catalog(path)["candidates"]
    }


def provider_entry(provider_id, path=None, default=None):
    alias_index = _cached_catalog(path)["alias_index"]
    direct_key = str(provider_id or "").strip()
    normalized_key = inkdrop_sources.normalize_key(provider_id)
    alias_key = inkdrop_sources.provider_key(provider_id)
    for key in (direct_key, normalized_key, alias_key):
        if key and key in alias_index:
            return copy.deepcopy(alias_index[key])
    return copy.deepcopy(default)


def provider_ids(path=None):
    return [provider.get("id") for provider in provider_candidates(path)]


def product_provider_ids(path=None):
    return [provider.get("id") for provider in product_provider_candidates(path)]


def provider_type(entry):
    entry = entry if isinstance(entry, dict) else {}
    source_kind = str(entry.get("source_kind") or "")
    return SOURCE_KIND_PROVIDER_TYPES.get(source_kind, "source")


def provider_mode(entry):
    entry = entry if isinstance(entry, dict) else {}
    return str(entry.get("default_mode") or "disabled")


def implementation_status(entry):
    entry = entry if isinstance(entry, dict) else {}
    status = str(entry.get("implementation_status") or "planned").strip().lower()
    return status if status in IMPLEMENTATION_STATUS_VALUES else "planned"


def template_visibility(entry):
    """Return how a built-in template should appear to new users.

    Legacy templates remain addressable through ``provider_entry`` so persisted
    configurations continue to load, but they are not offered as new defaults.
    """
    entry = entry if isinstance(entry, dict) else {}
    visibility = str(entry.get("template_visibility") or "standard").strip().lower()
    return visibility if visibility in TEMPLATE_VISIBILITY_VALUES else "standard"


def enabled_by_default(entry):
    return bool(MODE_ENABLED_DEFAULT.get(provider_mode(entry), False))


def is_auto_download_candidate(entry):
    entry = entry if isinstance(entry, dict) else {}
    return provider_mode(entry) == "auto" and entry.get("auto_download_allowed") is True


def requires_manual_review(entry):
    entry = entry if isinstance(entry, dict) else {}
    policy = entry.get("policy") if isinstance(entry.get("policy"), dict) else {}
    return provider_mode(entry) == "manual_review" or policy.get("requires_manual_confirm") is True


def is_disabled_boundary(entry):
    return provider_mode(entry) == "disabled" and not is_auto_download_candidate(entry)


def provider_summary(entry):
    entry = entry if isinstance(entry, dict) else {}
    provider_id = inkdrop_sources.provider_key(entry.get("id"))
    provider_type_value = provider_type(entry)
    return {
        "provider_id": provider_id,
        "provider_label": str(entry.get("display_name") or inkdrop_sources.provider_label(provider_id)),
        "provider_type": provider_type_value,
        "provider_type_label": inkdrop_sources.provider_type_label(provider_type_value),
        "provider_mode": provider_mode(entry),
        "risk_class": str(entry.get("risk_class") or ""),
        "media_types": list(entry.get("media_types") or []),
        "capabilities": list(entry.get("capabilities") or []),
        "integration_class": str(entry.get("integration_class") or ""),
        "auto_download_allowed": is_auto_download_candidate(entry),
        "enabled_by_default": enabled_by_default(entry),
        "template_visibility": template_visibility(entry),
        "default_visible": template_visibility(entry) == "standard",
        "requires_manual_review": requires_manual_review(entry),
        "first_ticket": str(entry.get("first_ticket") or ""),
    }


def settings_group_for_summary(summary):
    summary = summary if isinstance(summary, dict) else {}
    if summary.get("provider_mode") == "disabled":
        return "source_boundaries"
    return SETTINGS_GROUP_BY_PROVIDER_TYPE.get(str(summary.get("provider_type") or ""), "source_templates")


def editable_fields_for_entry(entry):
    entry = entry if isinstance(entry, dict) else {}
    policy = entry.get("policy") if isinstance(entry.get("policy"), dict) else {}
    fields = list(BASE_TEMPLATE_EDITABLE_FIELDS)
    search_backed_kinds = {
        "direct_file_detail_search",
        "direct_file_html_search",
        "direct_file_probe_source",
        "html_search_source",
        "json_direct_source",
        "reader_page_pack_source",
        "torrent_html_search",
        "torrent_detail_search",
    }
    result_filter_kinds = {
        "direct_file_detail_search",
        "direct_file_probe_source",
        "html_search_source",
        "reader_page_pack_source",
        "rss_reader_page_pack_feed",
        "torrent_detail_search",
    }
    if policy.get("allowed_extensions"):
        fields.append("allowed_extensions")
    if policy.get("minimum_seeders") is not None:
        fields.append("minimum_seeders")
    if policy.get("allowed_languages") is not None:
        fields.append("allowed_languages")
    if policy.get("health_provider_ids") is not None:
        fields.append("health_provider_ids")
    if policy.get("download_category") is not None:
        fields.append("download_category")
    if policy.get("download_client_by_protocol") is not None:
        fields.append("download_client_by_protocol")
    if policy.get("indexer_id") is not None:
        fields.append("indexer_id")
    if policy.get("indexer_ids") is not None:
        fields.append("indexer_ids")
    if policy.get("torrent_download_client") is not None:
        fields.append("torrent_download_client")
    if policy.get("usenet_download_client") is not None:
        fields.append("usenet_download_client")
    if policy.get("import_handoff_expectation") is not None:
        fields.append("import_handoff_expectation")
    if policy.get("probe_method"):
        fields.append("probe_method")
    if policy.get("shared_file_hosts") or policy.get("allowed_shared_file_hosts"):
        fields.append("shared_file_hosts")
    if policy.get("shared_file_host_rules") is not None:
        fields.append("shared_file_host_rules")
    if policy.get("max_detail_pages") is not None:
        fields.append("max_detail_pages")
    if policy.get("max_probe_links") is not None:
        fields.append("max_probe_links")
    if policy.get("max_redirects") is not None:
        fields.append("max_redirects")
    if policy.get("max_bytes") is not None:
        fields.append("max_bytes")
    if policy.get("allowed_content_types") is not None:
        fields.append("allowed_content_types")
    if policy.get("max_series_pages") is not None:
        fields.append("max_series_pages")
    if policy.get("max_reader_pages") is not None:
        fields.append("max_reader_pages")
    if policy.get("command_timeout_seconds") is not None:
        fields.append("command_timeout_seconds")
    if policy.get("verify_timeout_seconds") is not None:
        fields.append("verify_timeout_seconds")
    if policy.get("extract_script_image_urls") is not None:
        fields.append("extract_script_image_urls")
    for key in (
        "disable_weekly_pack_queries",
        "comic_series_fallback_queries_enabled",
        "disable_comic_series_fallback_queries",
        "categoryless_fallback_indexer_ids",
        "indexer_abort_after_request_error_count",
        "indexer_max_query_variants",
        "indexer_pack_detail_allowed_hosts",
        "indexer_weekly_pack_query_limit",
        "max_query_variants",
        "max_weekly_pack_queries",
        "pack_detail_allowed_hosts",
        "pack_detail_fetch",
        "pack_detail_max_bytes",
        "pack_detail_max_fetches",
        "pack_detail_sidecar_max_bytes",
        "query_aliases",
        "series_query_aliases",
        "series_title_aliases",
        "title_query_aliases",
        "weekly_pack_query_limit",
    ):
        if key in policy:
            fields.append(key)
    if entry.get("source_kind") == "manga_api_page_provider":
        fields.extend(
            [
                "fetch_at_home_pages",
                "enable_volume_page_pack",
                "mangadex_page_quality",
                "max_at_home_chapters",
                "volume_page_pack_min_chapters",
                "volume_page_pack_max_chapters",
                "mangadex_feed_page_size",
                "mangadex_feed_max_pages",
                "allowed_image_extensions",
                "allowed_languages",
                "content_ratings",
                "health_provider_ids",
            ]
        )
    if entry.get("source_kind") == "suwayomi_api_page_provider":
        fields.extend(
            [
                "base_url",
                "source_allowed_hosts",
                "suwayomi_source_ids",
                "suwayomi_source_names",
                "suwayomi_disabled_source_ids",
                "suwayomi_disabled_source_names",
                "suwayomi_allow_nsfw_sources",
                "suwayomi_rest_chapter_fallback_enabled",
                "suwayomi_skip_source_after_search_error",
                "suwayomi_source_error_cooldown_enabled",
                "suwayomi_source_error_cooldown_probe_enabled",
                "suwayomi_source_error_cooldown_probe_after_seconds",
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
                "suwayomi_source_error_quarantine_seconds",
                "suwayomi_source_error_quarantine_threshold",
                "suwayomi_source_error_quarantine_max_sources",
                "suwayomi_skip_obsolete_sources",
                "suwayomi_skip_sources_with_updates",
                "suwayomi_search_page",
                "suwayomi_max_query_variants",
                "suwayomi_max_source_count",
                "suwayomi_max_manga_matches",
                "suwayomi_max_chapters",
                "enable_volume_page_pack",
                "volume_page_pack_min_chapters",
                "volume_page_pack_max_chapters",
                "suwayomi_page_image_extension",
                "allowed_image_extensions",
                "allowed_languages",
                "health_provider_ids",
            ]
        )
    if entry.get("source_kind") == "suwayomi_managed_folder_source":
        fields.extend(
            [
                "suwayomi_folder_mode",
                "suwayomi_download_root",
                "suwayomi_import_staging_root",
                "suwayomi_folder_copy_policy",
                "suwayomi_folder_cleanup_policy",
                "suwayomi_folder_path_patterns",
                "suwayomi_folder_metadata_providers",
                "suwayomi_folder_min_age_seconds",
                "suwayomi_folder_max_scan_files",
                "suwayomi_folder_import_ready_enabled",
                "allowed_extensions",
                "allowed_languages",
                "health_provider_ids",
            ]
        )
    if entry.get("source_kind") == "manual_inbox_source":
        fields.extend(
            [
                "manual_inbox_root",
                "manual_inbox_staging_root",
                "manual_inbox_copy_policy",
                "manual_inbox_cleanup_policy",
                "manual_inbox_min_age_seconds",
                "manual_inbox_max_scan_files",
                "allowed_extensions",
                "allowed_languages",
                "health_provider_ids",
            ]
        )
    if policy.get("requires_account") is True or entry.get("source_kind") in {"json_direct_source", "metadata_api", "newznab_indexer", "opds_acquisition_catalog", "prowlarr_indexer", "rss_detail_direct_feed", "rss_detail_probe_feed", "rss_direct_feed", "rss_reader_page_pack_feed", "torznab_indexer", "torrent_detail_rss_feed", "torrent_detail_search", "torrent_rss_feed"}:
        fields.append("base_url")
    if entry.get("source_kind") in {"newznab_indexer", "prowlarr_indexer", "prowlarr_or_torrent_indexer", "torznab_indexer", "torrent_detail_rss_feed", "torrent_html_search", "torrent_detail_search", "torrent_rss_feed"}:
        fields.append("categories")
    if entry.get("source_kind") in {"prowlarr_indexer", "prowlarr_or_torrent_indexer", "torznab_indexer", "torrent_detail_rss_feed", "torrent_html_search", "torrent_detail_search", "torrent_rss_feed"}:
        fields.append("minimum_seeders")
    if entry.get("source_kind") in search_backed_kinds:
        fields.extend(["search_url_templates", "list_url_templates", "base_url"])
    if entry.get("source_kind") in search_backed_kinds:
        fields.extend(["pagination_url_templates", "list_pagination_url_templates", "max_search_pages"])
    if entry.get("source_kind") in result_filter_kinds or policy.get("result_url_allow_patterns") is not None or policy.get("result_url_deny_patterns") is not None:
        fields.extend(["result_url_allow_patterns", "result_url_deny_patterns", "result_title_allow_patterns", "result_title_deny_patterns"])
    if entry.get("source_kind") in {"direct_file_detail_search", "direct_file_probe_source", "json_direct_source", "torrent_detail_search"}:
        fields.append("search_api_flavors")
    if entry.get("source_kind") == "external_tool_bridge":
        fields.extend(
            [
                "auto_stage_tool_output",
                "staged_output_root",
                "command_executable",
                "command_args",
                "command_argv",
                "command_env",
                "secret_env",
                "secret_env_var",
                "working_directory",
                "timeout_seconds",
            ]
        )
    if policy.get("secret_fields"):
        fields.append("secret_ref")
    for field in policy.get("secret_fields") or []:
        fields.append(str(field))
    seen = set()
    out = []
    for field in fields:
        if field in seen:
            continue
        seen.add(field)
        out.append(field)
    return out


def source_template_settings(entry):
    entry = entry if isinstance(entry, dict) else {}
    summary = provider_summary(entry)
    policy = entry.get("policy") if isinstance(entry.get("policy"), dict) else {}
    mode = summary["provider_mode"]
    disabled_boundary = mode == "disabled"
    allowed_extensions = list(policy.get("allowed_extensions") or [])
    editable_fields = editable_fields_for_entry(entry)
    settings = {
        "catalog_version": catalog_version(),
        "source_template": True,
        "source_kind": str(entry.get("source_kind") or ""),
        "source_mode": mode,
        "target_mode_after_gates": str(entry.get("target_mode_after_gates") or ""),
        "risk_class": summary["risk_class"],
        "media_types": summary["media_types"],
        "integration_class": summary["integration_class"],
        "auto_download_allowed": summary["auto_download_allowed"],
        "user_addable": not disabled_boundary,
        "user_enableable": not disabled_boundary,
        "implementation_status": implementation_status(entry),
        "template_visibility": summary["template_visibility"],
        "default_visible": summary["default_visible"],
        "enablement_guard": "disabled_boundary" if disabled_boundary else "implementation_required",
        "rights_gate": str(policy.get("rights_gate") or ""),
        "direct_url_policy": str(policy.get("direct_url_policy") or ""),
        "allowed_extensions": allowed_extensions,
        "requires_account": policy.get("requires_account"),
        "requires_browser": policy.get("requires_browser"),
        "requires_manual_confirm": policy.get("requires_manual_confirm"),
        "minimum_seeders": policy.get("minimum_seeders"),
        "priority": 100,
        "policy": copy.deepcopy(policy),
        "examples": list(entry.get("examples") or []),
        "references": list(entry.get("references") or []),
        "editable_fields": editable_fields,
        "secret_fields": [str(field) for field in (policy.get("secret_fields") or [])],
    }
    certification = DIRECT_SOURCE_CERTIFICATIONS.get(summary["provider_id"])
    if certification:
        settings["source_certification"] = copy.deepcopy(certification)
        settings["certification_status"] = certification["certification"]
    for key in editable_fields:
        if key not in settings and key in policy:
            settings[key] = copy.deepcopy(policy[key])
    if policy.get("command_timeout_seconds") is not None:
        settings["command_timeout_seconds"] = policy.get("command_timeout_seconds")
    if policy.get("verify_timeout_seconds") is not None:
        settings["verify_timeout_seconds"] = policy.get("verify_timeout_seconds")
    if summary["auto_download_allowed"]:
        settings["priority"] = 10
    elif mode == "assist":
        settings["priority"] = 50
    elif mode == "manual_review":
        settings["priority"] = 80
    elif mode == "metadata_only":
        settings["priority"] = 90
    return settings


def provider_config_template(entry, *, enabled=False):
    entry = entry if isinstance(entry, dict) else {}
    summary = provider_summary(entry)
    settings = source_template_settings(entry)
    role = AUTOMATION_ROLE_BY_MODE.get(summary["provider_mode"], summary["provider_type_label"])
    if summary["provider_type"] == "indexer":
        role = f"{role} / Indexer"
    elif summary["provider_type"] == "direct_download":
        role = f"{role} / Direct download"
    elif summary["provider_type"] == "download_source":
        role = f"{role} / External or download source"
    description = str(entry.get("help_text") or f"{summary['provider_label']} source template from the 2026-07-02 source intake.")
    integration_role = "provider"
    if summary["provider_id"] == "suwayomi":
        description = "Suwayomi adapter that searches configured reader sources and packages matched pages for guarded import."
        integration_role = "adapter"
    elif summary["provider_id"] == "suwayomi_managed_folder":
        description = "Suwayomi adapter / managed-folder source for guarded discovery and staged import of completed files."
        integration_role = "adapter_managed_folder"
    elif summary["provider_id"] in {"getcomics", "rss_getcomics"}:
        description = "Experimental GetComics integration; end-to-end import and verification are not yet certified."
        integration_role = "experimental_provider"
    return {
        "id": summary["provider_id"],
        "provider_type": summary["provider_type"],
        "display_name": summary["provider_label"],
        "enabled": bool(enabled and settings.get("user_enableable")),
        "base_url": None,
        "secret_ref": None,
        "settings_group": settings_group_for_summary(summary),
        "ownership": "user",
        "automation_role": role,
        "description": description,
        "integration_role": integration_role,
        "next_action": summary["first_ticket"],
        "capabilities": summary["capabilities"],
        "applied_by": [
            "InkDrop source settings",
            "Source ladder",
            "Manual review" if summary["requires_manual_review"] else "Automatic candidate selection",
        ],
        "settings": settings,
        "source": "source_catalog",
    }


def provider_config_templates(path=None, *, enabled=False):
    return [provider_config_template(entry, enabled=enabled) for entry in product_provider_candidates(path)]


def source_catalog_app_settings(path=None):
    return [
        {
            "key": "sources.catalog.version",
            "scope": "sources",
            "label": "Source catalog version",
            "value": catalog_version(path),
            "description": "Machine-readable source catalog version used to seed Arr-style source settings.",
            "source": "source_catalog",
        },
        {
            "key": "sources.catalog.default_enabled",
            "scope": "sources",
            "label": "Default source template enabled state",
            "value": False,
            "description": "New source templates are visible in settings but disabled until the user enables a safe implemented provider.",
            "source": "source_catalog",
        },
        {
            "key": "sources.catalog.auto_download_candidates",
            "scope": "sources",
            "label": "Auto-download candidate providers",
            "value": auto_download_provider_ids(path),
            "description": "Providers eligible for auto mode after implementation and source-specific gates.",
            "source": "source_catalog",
        },
    ]


def settings_seed_payload(path=None):
    return {
        "providers": provider_config_templates(path),
        "settings": source_catalog_app_settings(path),
    }


def provider_summary_by_id(provider_id, path=None):
    entry = provider_entry(provider_id, path)
    if not entry:
        return None
    return provider_summary(entry)


def provider_summaries(path=None, *, mode=None, auto_download_allowed=None, media_type=None, capability=None):
    rows = []
    for entry in provider_candidates(path):
        if mode is not None and provider_mode(entry) != mode:
            continue
        if auto_download_allowed is not None and is_auto_download_candidate(entry) != bool(auto_download_allowed):
            continue
        if media_type is not None and media_type not in (entry.get("media_types") or []):
            continue
        if capability is not None and capability not in (entry.get("capabilities") or []):
            continue
        rows.append(provider_summary(entry))
    return rows


def auto_download_provider_ids(path=None):
    return [
        provider_summary(entry)["provider_id"]
        for entry in product_provider_candidates(path)
        if is_auto_download_candidate(entry)
    ]


def disabled_boundary_ids(path=None):
    return [row["provider_id"] for row in provider_summaries(path, mode="disabled")]


def implemented_provider_ids(path=None):
    return [
        inkdrop_sources.provider_key(entry.get("id"))
        for entry in provider_candidates(path)
        if implementation_status(entry) == "implemented"
    ]


def source_attempt_seed(provider_id, path=None):
    entry = provider_entry(provider_id, path)
    if not entry:
        return {}
    summary = provider_summary(entry)
    return {
        "provider_id": summary["provider_id"],
        "source": summary["provider_id"],
        "source_type": summary["provider_type"],
        "provider_mode": summary["provider_mode"],
        "risk_class": summary["risk_class"],
        "auto_download_allowed": summary["auto_download_allowed"],
        "requires_manual_review": summary["requires_manual_review"],
    }


def provider_status_from_catalog(provider_id, path=None, *, row_kind="provider"):
    entry = provider_entry(provider_id, path)
    if not entry:
        return None
    summary = provider_summary(entry)
    status = MODE_STATUS.get(summary["provider_mode"], "observed")
    state = "disabled" if summary["provider_mode"] == "disabled" else "configured"
    next_action = summary["first_ticket"] or f"{summary['provider_label']} is available to InkDrop"
    return inkdrop_sources.provider_status_contract(
        {
            "provider_id": summary["provider_id"],
            "provider_type": summary["provider_type"],
            "status": status,
            "state": state,
            "enabled": summary["enabled_by_default"],
            "detail": f"{summary['provider_label']} source mode is {summary['provider_mode']}",
            "next_action": next_action,
        },
        row_kind=row_kind,
    )
