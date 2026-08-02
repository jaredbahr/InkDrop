#!/usr/bin/env python3
"""Catalog-wide Arr-style source settings contract smoke."""

import tempfile
from pathlib import Path

import inkdrop_source_catalog as catalog
import inkdrop_source_registry as registry
import inkdrop_state


def fail(message):
    print(f"SOURCE_SETTINGS_CONTRACT_FAIL: {message}")
    raise SystemExit(1)


def ok(message):
    print(f"SOURCE_SETTINGS_CONTRACT_OK: {message}")


def assert_equal(actual, expected, message):
    if actual != expected:
        fail(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value, message):
    if not value:
        fail(message)


def assert_false(value, message):
    if value:
        fail(message)


def by_id(rows, key="id"):
    return {row.get(key): row for row in rows or []}


def registry_by_id(db_path):
    return by_id(
        registry.registry_from_settings_snapshot(
            inkdrop_state.settings_snapshot(db_path),
            include_non_acquisition=True,
        ),
        key="provider_id",
    )


def assert_enable_fails(db_path, provider_id):
    try:
        inkdrop_state.update_provider_config(db_path, provider_id, {"enabled": True})
    except ValueError as exc:
        assert_true("not user-enableable" in str(exc), f"{provider_id} boundary error is explicit")
        return
    fail(f"{provider_id} boundary should not be user-enableable")


OWNERSHIP_GATED_AUTO_PROVIDERS = {
    "prowlarr_nyaa": {
        "rights_gate": "user_owned_collection_required",
        "direct_url_policy": "download_client_handoff_after_strict_candidate_verdict",
        "indexer_ids": [],
        "categoryless_fallback_indexer_ids": [],
        "requires_account": False,
    },
    "prowlarr_tokyo_toshokan_manga": {
        "rights_gate": "user_owned_collection_required",
        "direct_url_policy": "download_client_handoff_after_strict_candidate_verdict",
        "indexer_ids": [],
        "requires_account": False,
    },
    "prowlarr_torrentleech_comics": {
        "rights_gate": "user_owned_collection_required",
        "direct_url_policy": "download_client_handoff_after_strict_candidate_verdict",
        "indexer_ids": [],
        "requires_account": True,
    },
    "prowlarr_dognzb_comics": {
        "rights_gate": "user_owned_collection_required",
        "direct_url_policy": "download_client_handoff_after_strict_candidate_verdict",
        "indexer_ids": [],
        "requires_account": True,
        "pack_detail_allowed_hosts": ["dognzb.cr"],
    },
}

SITE_POLICY_PAGE_PACK_AUTO_PROVIDERS = {
    "mangadex": {
        "rights_gate": "site_terms_and_language_policy",
        "direct_url_policy": "allow_api_page_urls_after_chapter_mapping",
        "requires_account": False,
    },
    "suwayomi": {
        "rights_gate": "site_terms_and_language_policy",
        "direct_url_policy": "allow_local_suwayomi_page_urls_after_chapter_mapping",
        "requires_account": False,
    },
}


def main():
    entries = catalog.product_provider_candidates()
    seed = catalog.settings_seed_payload()
    all_provider_ids = catalog.provider_ids()
    provider_ids = catalog.product_provider_ids()
    disabled_boundary_ids = set(catalog.disabled_boundary_ids())
    auto_candidate_ids = set(catalog.auto_download_provider_ids())
    implemented_provider_ids = set(catalog.implemented_provider_ids())
    legacy_indexer_ids = {
        "prowlarr_kat_comics",
        "prowlarr_pirate_bay_comics",
        "prowlarr_torrentdownload_comics",
        "prowlarr_ebookbay",
        "prowlarr_academic_torrents",
        "prowlarr_bitmagnet",
        "generic_torrent_html_search",
        "generic_torrent_detail_search",
    }
    seed_providers = by_id(seed["providers"])

    assert_equal(set(seed_providers), set(provider_ids), "every product catalog provider has a settings template")
    assert_true(legacy_indexer_ids.issubset(set(all_provider_ids)), "legacy indexer ids remain loadable from the audit catalog")
    assert_false(legacy_indexer_ids & set(provider_ids), "legacy indexers are not offered as new source templates")
    for provider_id in legacy_indexer_ids:
        entry = catalog.provider_entry(provider_id)
        assert_equal(catalog.template_visibility(entry), "legacy", f"{provider_id} is explicitly legacy-hidden")
        compatibility_template = catalog.provider_config_template(entry)
        assert_equal(compatibility_template["id"], provider_id, f"{provider_id} compatibility template still materializes")
        assert_false(compatibility_template["enabled"], f"{provider_id} compatibility template stays disabled")
    indexer_entries = [entry for entry in catalog.product_provider_candidates() if catalog.provider_type(entry) == "indexer"]
    default_indexer_ids = {entry["id"] for entry in indexer_entries if catalog.template_visibility(entry) == "standard"}
    advanced_indexer_ids = {entry["id"] for entry in indexer_entries if catalog.template_visibility(entry) == "advanced"}
    assert_equal(
        default_indexer_ids,
        {"prowlarr_nyaa", "prowlarr_tokyo_toshokan_manga", "prowlarr_torrentleech_comics", "prowlarr_dognzb_comics"},
        "default indexer catalog contains only strong configured Prowlarr paths",
    )
    assert_equal(
        advanced_indexer_ids,
        {"prowlarr_internet_archive", "generic_torznab_indexer", "generic_newznab_indexer", "generic_torrent_rss_feed", "generic_torrent_detail_rss_feed"},
        "advanced indexer catalog retains supported configurable paths",
    )
    for provider_id in default_indexer_ids | advanced_indexer_ids:
        template = seed_providers[provider_id]
        assert_false(template["enabled"], f"{provider_id} is never enabled merely by catalog visibility")
        assert_true(bool(template.get("description")), f"{provider_id} provides operator help")
    assert_true(
        set(catalog.PRODUCT_DIRECT_SOURCE_IDS).issubset(set(seed_providers)),
        "certified concrete direct source templates are present",
    )
    assert_true(
        set(catalog.VAGUE_DIRECT_SOURCE_BUCKET_IDS).issubset(set(all_provider_ids)),
        "vague direct bucket records remain in the audit catalog",
    )
    assert_false(
        set(catalog.VAGUE_DIRECT_SOURCE_BUCKET_IDS) & set(seed_providers),
        "vague direct bucket records are removed from normal product settings",
    )
    assert_equal(disabled_boundary_ids, {
        "adult_nsfw_sources",
        "audiobook_sources",
        "course_and_video_sites",
        "private_trackers",
    }, "disabled source boundary set")
    for provider_id in ("rss_getcomics", "comicscodes"):
        settings = (seed_providers[provider_id].get("settings") or {})
        assert_equal(settings.get("command_timeout_seconds"), 120, f"{provider_id} command timeout default")
        assert_true(
            "command_timeout_seconds" in (settings.get("editable_fields") or []),
            f"{provider_id} command timeout is editable",
        )
    mangadex_settings = seed_providers["mangadex"].get("settings") or {}
    assert_equal(mangadex_settings.get("command_timeout_seconds"), 360, "MangaDex command timeout default")
    assert_equal(mangadex_settings.get("verify_timeout_seconds"), 90, "MangaDex verify timeout default")
    assert_true(
        "command_timeout_seconds" in (mangadex_settings.get("editable_fields") or []),
        "MangaDex command timeout is editable",
    )
    assert_true(
        "verify_timeout_seconds" in (mangadex_settings.get("editable_fields") or []),
        "MangaDex verify timeout is editable",
    )
    suwayomi_folder_settings = seed_providers["suwayomi_managed_folder"].get("settings") or {}
    suwayomi_settings = seed_providers["suwayomi"].get("settings") or {}
    suwayomi_policy = suwayomi_settings.get("policy") or {}
    assert_equal(
        suwayomi_policy.get("suwayomi_source_error_cooldown_probe_enabled"),
        True,
        "Suwayomi source-error cooldown probe defaults enabled",
    )
    assert_equal(
        suwayomi_policy.get("suwayomi_source_error_cooldown_probe_after_seconds"),
        1800,
        "Suwayomi source-error cooldown probe waits 30 minutes",
    )
    assert_equal(
        suwayomi_policy.get("suwayomi_source_error_cooldown_probe_max_sources"),
        2,
        "Suwayomi source-error cooldown probe can try two cooled sources",
    )
    for field in (
        "suwayomi_source_error_cooldown_probe_enabled",
        "suwayomi_source_error_cooldown_probe_after_seconds",
        "suwayomi_source_error_cooldown_probe_max_sources",
    ):
        assert_true(field in (suwayomi_settings.get("editable_fields") or []), f"Suwayomi exposes {field}")
    assert_equal(suwayomi_folder_settings.get("source_mode"), "assist", "Suwayomi managed folder starts as assist/report source")
    assert_equal(suwayomi_folder_settings.get("implementation_status"), "implemented", "Suwayomi managed folder scanner/writer is implemented")
    assert_false(suwayomi_folder_settings.get("auto_download_allowed"), "Suwayomi managed folder remains opt-in and not auto-download by default")
    assert_equal(
        (suwayomi_folder_settings.get("policy") or {}).get("suwayomi_folder_mode"),
        "report_only",
        "Suwayomi managed folder defaults to report-only",
    )
    assert_equal(
        (suwayomi_folder_settings.get("policy") or {}).get("suwayomi_folder_copy_policy"),
        "copy_to_staging",
        "Suwayomi managed folder copies into staging before import",
    )
    for field in (
        "suwayomi_download_root",
        "suwayomi_import_staging_root",
        "suwayomi_folder_path_patterns",
        "suwayomi_folder_metadata_providers",
        "suwayomi_folder_import_ready_enabled",
    ):
        assert_true(field in (suwayomi_folder_settings.get("editable_fields") or []), f"Suwayomi managed folder exposes {field}")

    expected_certifications = {
        "generic_rss_direct_feed": "Beta",
        "rss_getcomics": "Experimental",
        "suwayomi_managed_folder": "Beta",
        "generic_safe_http_direct_download": "Beta",
        "local_manual_inbox": "Beta",
    }
    for provider_id, status in expected_certifications.items():
        settings = seed_providers[provider_id].get("settings") or {}
        certification = settings.get("source_certification") or {}
        assert_equal(settings.get("certification_status"), status, f"{provider_id} certification status")
        assert_equal(certification.get("certification"), status, f"{provider_id} certification payload status")
        assert_true(certification.get("required_gates"), f"{provider_id} certification records required gates")
    assert_equal(
        (seed_providers["rss_getcomics"].get("settings") or {}).get("source_certification", {}).get("blocked_gates"),
        ["live_provider_acceptance"],
        "GetComics remains experimental pending live provider acceptance",
    )
    getcomics_settings = seed_providers["rss_getcomics"].get("settings") or {}
    getcomics_policy = getcomics_settings.get("policy") or {}
    assert_equal(getcomics_settings.get("source_kind"), "rss_detail_probe_feed", "GetComics uses bounded detail/probe source kind")
    assert_equal(getcomics_policy.get("shared_file_hosts"), ["pixeldrain"], "GetComics exposes only Pixeldrain transport")
    assert_equal(getcomics_policy.get("feed_detail_allowed_hosts"), ["getcomics.org", "www.getcomics.org"], "GetComics discovery hosts are exact")
    assert_equal(getcomics_policy.get("transport_allowed_hosts"), ["pixeldrain.com", "www.pixeldrain.com"], "GetComics transport hosts are exact")
    assert_true(getcomics_settings.get("requires_manual_confirm"), "GetComics keeps manual confirmation default")
    assert_false(getcomics_settings.get("auto_download_allowed"), "GetComics remains non-automatic by default")
    safe_http_settings = seed_providers["generic_safe_http_direct_download"].get("settings") or {}
    assert_equal(safe_http_settings.get("source_mode"), "assist", "Generic safe HTTP direct download starts in assist mode")
    for field in (
        "allowed_extensions",
        "allowed_content_types",
        "max_bytes",
        "max_redirects",
        "max_probe_links",
        "probe_method",
    ):
        assert_true(field in (safe_http_settings.get("editable_fields") or []), f"Generic safe HTTP exposes {field}")
    manual_inbox_settings = seed_providers["local_manual_inbox"].get("settings") or {}
    assert_equal(
        (manual_inbox_settings.get("policy") or {}).get("direct_url_policy"),
        "local_filesystem_only_no_remote_fetch",
        "Local/manual inbox has no remote fetch policy",
    )
    for field in (
        "manual_inbox_root",
        "manual_inbox_staging_root",
        "manual_inbox_copy_policy",
        "manual_inbox_cleanup_policy",
        "manual_inbox_min_age_seconds",
        "manual_inbox_max_scan_files",
    ):
        assert_true(field in (manual_inbox_settings.get("editable_fields") or []), f"Local/manual inbox exposes {field}")

    for entry in entries:
        provider_id = entry["id"]
        template = seed_providers[provider_id]
        settings = template.get("settings") or {}
        assert_equal(template.get("source"), "source_catalog", f"{provider_id} template source")
        assert_equal(template.get("ownership"), "user", f"{provider_id} template ownership")
        assert_false(template.get("enabled"), f"{provider_id} template starts disabled")
        assert_true(settings.get("source_template"), f"{provider_id} is marked source_template")
        expected_implementation = "implemented" if provider_id in implemented_provider_ids else "planned"
        assert_equal(settings.get("implementation_status"), expected_implementation, f"{provider_id} implementation status")
        assert_true("source_mode" in (settings.get("editable_fields") or []), f"{provider_id} has source mode setting")
        assert_equal(bool(settings.get("auto_download_allowed")), provider_id in auto_candidate_ids, f"{provider_id} auto flag matches catalog")

        policy = settings.get("policy") or {}
        if provider_id in auto_candidate_ids:
            assert_equal(settings.get("source_mode"), "auto", f"{provider_id} auto provider mode")
            rights_gate = str(settings.get("rights_gate") or policy.get("rights_gate") or "")
            if provider_id in OWNERSHIP_GATED_AUTO_PROVIDERS:
                expected = OWNERSHIP_GATED_AUTO_PROVIDERS[provider_id]
                assert_equal(rights_gate, expected["rights_gate"], f"{provider_id} ownership-gated auto provider rights gate")
                assert_equal(
                    settings.get("direct_url_policy") or policy.get("direct_url_policy"),
                    expected["direct_url_policy"],
                    f"{provider_id} ownership-gated auto provider handoff policy",
                )
                assert_equal(policy.get("indexer_ids"), expected["indexer_ids"], f"{provider_id} ownership-gated auto provider indexer target")
                if "categoryless_fallback_indexer_ids" in expected:
                    assert_equal(
                        policy.get("categoryless_fallback_indexer_ids"),
                        expected["categoryless_fallback_indexer_ids"],
                        f"{provider_id} ownership-gated auto provider categoryless fallback target",
                    )
                assert_equal(
                    policy.get("requires_account") is True,
                    expected["requires_account"],
                    f"{provider_id} ownership-gated auto provider account requirement",
                )
                if "pack_detail_allowed_hosts" in expected:
                    assert_equal(
                        policy.get("pack_detail_allowed_hosts"),
                        expected["pack_detail_allowed_hosts"],
                        f"{provider_id} ownership-gated auto provider sidecar host allowlist",
                    )
            elif provider_id in SITE_POLICY_PAGE_PACK_AUTO_PROVIDERS:
                expected = SITE_POLICY_PAGE_PACK_AUTO_PROVIDERS[provider_id]
                assert_equal(rights_gate, expected["rights_gate"], f"{provider_id} page-pack auto provider rights gate")
                assert_equal(
                    settings.get("direct_url_policy") or policy.get("direct_url_policy"),
                    expected["direct_url_policy"],
                    f"{provider_id} page-pack auto provider handoff policy",
                )
                assert_equal(
                    policy.get("requires_account") is True,
                    expected["requires_account"],
                    f"{provider_id} page-pack auto provider account requirement",
                )
            else:
                assert_true("public_domain" in rights_gate or "open_license" in rights_gate, f"{provider_id} auto provider has rights gate")
                assert_false(policy.get("requires_account") is True, f"{provider_id} auto provider requires no account")
            assert_true(settings.get("allowed_extensions"), f"{provider_id} auto provider has extension gate")
            assert_false(policy.get("requires_browser") is True, f"{provider_id} auto provider requires no browser")
            assert_false(policy.get("requires_manual_confirm") is True, f"{provider_id} auto provider requires no manual confirm")
        else:
            assert_false(settings.get("auto_download_allowed"), f"{provider_id} is not auto-download capable")

        if provider_id in disabled_boundary_ids:
            assert_equal(template.get("settings_group"), "source_boundaries", f"{provider_id} boundary settings group")
            assert_false(settings.get("user_addable"), f"{provider_id} boundary is not user-addable")
            assert_false(settings.get("user_enableable"), f"{provider_id} boundary is not user-enableable")
            assert_equal(settings.get("enablement_guard"), "disabled_boundary", f"{provider_id} boundary guard")
        else:
            assert_true(settings.get("user_addable"), f"{provider_id} is user-addable")
            assert_true(settings.get("user_enableable"), f"{provider_id} is user-enableable")
            assert_equal(settings.get("enablement_guard"), "implementation_required", f"{provider_id} implementation guard")

    with tempfile.TemporaryDirectory(prefix="inkdrop-source-settings-contract-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        result = inkdrop_state.sync_settings(db_path, providers=seed["providers"], settings=seed["settings"])
        assert_true(result.get("ok"), "settings sync ok")
        snapshot = inkdrop_state.settings_snapshot(db_path)
        providers = by_id(snapshot.get("providers"))
        assert_equal(set(providers), set(provider_ids), "settings snapshot has all source templates")
        torznab_settings = providers["generic_torznab_indexer"].get("settings") or {}
        newznab_settings = providers["generic_newznab_indexer"].get("settings") or {}
        assert_true("secret_ref" in (torznab_settings.get("editable_fields") or []), "Generic Torznab external secret ref is editable")
        assert_equal(torznab_settings.get("weekly_pack_query_limit"), 4, "Generic Torznab weekly pack cap default")
        assert_true(torznab_settings.get("pack_detail_fetch"), "Generic Torznab pack detail fetch default")
        assert_true("secret_ref" in (newznab_settings.get("editable_fields") or []), "Generic Newznab external secret ref is editable")
        assert_equal(newznab_settings.get("weekly_pack_query_limit"), 4, "Generic Newznab weekly pack cap default")
        assert_true(newznab_settings.get("pack_detail_fetch"), "Generic Newznab pack detail fetch default")
        clone_snapshot = inkdrop_state.add_provider_config_from_template(
            db_path,
            "generic_torznab_indexer",
            display_name="TorrentLeech Comics",
            base_url="https://jackett.example/api/v2.0/indexers/torrentleech/results/torznab",
            settings={
                "categories": ["7030"],
                "minimum_seeders": 2,
                "weekly_pack_query_limit": 2,
                "pack_detail_allowed_hosts": ["jackett.example"],
                "api_key": "clone-secret",
            },
        )
        clone_providers = by_id(clone_snapshot.get("providers"))
        clone = clone_providers["torznab_torrentleech_comics"]
        clone_settings = clone.get("settings") or {}
        assert_false(clone.get("enabled"), "cloned Torznab source starts disabled")
        assert_equal(clone.get("source"), "user", "cloned Torznab source is user-owned")
        assert_true(clone_settings.get("source_template_instance"), "cloned Torznab source is marked as a template instance")
        assert_equal(clone_settings.get("template_provider_id"), "generic_torznab_indexer", "cloned Torznab template id recorded")
        assert_false(clone_settings.get("user_addable"), "cloned Torznab source is not itself an addable template")
        assert_equal(clone_settings.get("secret_ref"), "torznab_torrentleech_comics_api_key", "cloned Torznab default secret alias")
        assert_equal(clone_settings.get("base_url"), "https://jackett.example/api/v2.0/indexers/torrentleech/results/torznab", "cloned Torznab settings URL")
        assert_equal(clone_settings.get("categories"), ["7030"], "cloned Torznab categories surface setting")
        assert_equal(clone_settings.get("policy", {}).get("categories"), ["7030"], "cloned Torznab categories reach policy")
        assert_equal(clone_settings.get("policy", {}).get("minimum_seeders"), 2, "cloned Torznab seed gate reaches policy")
        redacted_clone = by_id(inkdrop_state.settings_snapshot(db_path).get("providers"))["torznab_torrentleech_comics"]
        assert_equal((redacted_clone.get("settings") or {}).get("api_key"), "", "cloned Torznab API key is redacted from snapshots")
        assert_true(((redacted_clone.get("settings") or {}).get("has_secret_values") or {}).get("api_key"), "cloned Torznab snapshot marks saved API key")
        clone_registry = registry_by_id(db_path)
        assert_equal(clone_registry["torznab_torrentleech_comics"]["base_url"], "https://jackett.example/api/v2.0/indexers/torrentleech/results/torznab", "cloned Torznab registry uses settings URL")
        assert_equal(clone_registry["torznab_torrentleech_comics"]["source_kind"], "torznab_indexer", "cloned Torznab registry keeps adapter source kind")
        assert_false(clone_registry["torznab_torrentleech_comics"]["auto_search_allowed"], "cloned Torznab cannot search while disabled")
        inkdrop_state.update_provider_config(db_path, "torznab_torrentleech_comics", {"enabled": True})
        enabled_clone_registry = registry_by_id(db_path)
        assert_true(enabled_clone_registry["torznab_torrentleech_comics"]["auto_search_allowed"], "enabled cloned Torznab can search")
        assert_true(enabled_clone_registry["torznab_torrentleech_comics"]["manual_review_allowed"], "enabled cloned Torznab stays assist/review by default")
        assert_false(enabled_clone_registry["torznab_torrentleech_comics"]["auto_download_allowed"], "enabled cloned Torznab does not auto-download by default")

        initial_registry = registry_by_id(db_path)
        for provider_id in provider_ids:
            provider = providers[provider_id]
            row = initial_registry[provider_id]
            assert_false(provider.get("enabled"), f"{provider_id} persists disabled by default")
            assert_false(row.get("auto_search_allowed"), f"{provider_id} cannot search while disabled")
            assert_false(row.get("auto_download_allowed"), f"{provider_id} cannot download while disabled")

        for provider_id in disabled_boundary_ids:
            assert_enable_fails(db_path, provider_id)

        for provider_id in provider_ids:
            if provider_id in disabled_boundary_ids:
                continue
            inkdrop_state.update_provider_config(db_path, provider_id, {"enabled": True})

        enabled_planned_registry = registry_by_id(db_path)
        assert_true(
            enabled_planned_registry["mangadex"]["auto_download_allowed"],
            "MangaDex auto-downloads when the catalog At-Home page-pack gate is enabled",
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_torznab_indexer",
            {
                "settings": {
                    "base_url": "https://torznab.example/api",
                    "categories": ["7030"],
                    "minimum_seeders": 4,
                    "weekly_pack_query_limit": 2,
                    "pack_detail_allowed_hosts": ["torznab.example"],
                }
            },
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_newznab_indexer",
            {
                "settings": {
                    "base_url": "https://newznab.example/api",
                    "categories": ["7030"],
                    "weekly_pack_query_limit": 2,
                    "pack_detail_allowed_hosts": ["newznab.example"],
                }
            },
        )
        edited_registry = registry_by_id(db_path)
        assert_equal(edited_registry["generic_torznab_indexer"]["base_url"], "https://torznab.example/api", "Generic Torznab settings URL reaches registry")
        assert_equal(edited_registry["generic_torznab_indexer"]["policy"]["categories"], ["7030"], "Generic Torznab settings categories reach policy")
        assert_equal(edited_registry["generic_torznab_indexer"]["policy"]["minimum_seeders"], 4, "Generic Torznab settings seed gate reaches policy")
        assert_equal(edited_registry["generic_torznab_indexer"]["policy"]["weekly_pack_query_limit"], 2, "Generic Torznab weekly cap reaches policy")
        assert_equal(edited_registry["generic_newznab_indexer"]["base_url"], "https://newznab.example/api", "Generic Newznab settings URL reaches registry")
        assert_equal(edited_registry["generic_newznab_indexer"]["policy"]["categories"], ["7030"], "Generic Newznab settings categories reach policy")
        assert_equal(edited_registry["generic_newznab_indexer"]["policy"]["weekly_pack_query_limit"], 2, "Generic Newznab weekly cap reaches policy")
        inkdrop_state.update_provider_config(
            db_path,
            "mangadex",
            {
                "enabled": True,
                "settings": {
                    "source_mode": "auto",
                    "fetch_at_home_pages": True,
                    "requires_manual_confirm": False,
                    "policy": {
                        "fetch_at_home_pages": True,
                        "requires_manual_confirm": False,
                    },
                },
            },
        )
        mangadex_auto_registry = registry_by_id(db_path)["mangadex"]
        assert_true(mangadex_auto_registry["auto_search_allowed"], "MangaDex explicit auto opt-in can search")
        assert_true(mangadex_auto_registry["auto_download_config_allowed"], "MangaDex explicit At-Home opt-in is auto-download-configured")
        assert_true(mangadex_auto_registry["auto_download_allowed"], "MangaDex explicit At-Home opt-in can auto-download page packs")
        assert_false(mangadex_auto_registry["manual_review_allowed"], "MangaDex explicit auto opt-in leaves manual review lane")
        assert_false(mangadex_auto_registry["requires_manual_review"], "MangaDex explicit auto opt-in clears manual confirmation")
        for provider_id in provider_ids:
            row = enabled_planned_registry[provider_id]
            if provider_id in disabled_boundary_ids:
                assert_equal(row.get("registry_state"), "disabled_boundary", f"{provider_id} boundary registry")
            elif provider_id in implemented_provider_ids:
                if row.get("source_mode") == "auto":
                    assert_equal(row.get("registry_state"), "ready", f"{provider_id} implemented auto source is ready after user enable")
                    assert_true(row.get("auto_search_allowed"), f"{provider_id} implemented source can search after user enable")
                    expected_auto_download = provider_id in auto_candidate_ids
                    assert_equal(row.get("auto_download_allowed"), expected_auto_download, f"{provider_id} implemented source download gate")
                elif row.get("source_mode") in {"assist", "manual_review"}:
                    assert_true(row.get("auto_search_allowed"), f"{provider_id} implemented assist/manual source can search")
                    assert_true(row.get("manual_review_allowed"), f"{provider_id} implemented assist/manual source can manual-review")
                    assert_false(row.get("auto_download_allowed"), f"{provider_id} implemented assist/manual source cannot auto-download")
            else:
                assert_equal(row.get("registry_state"), "planned", f"{provider_id} enabled template remains planned")
                assert_true("implementation_pending" in (row.get("block_reasons") or []), f"{provider_id} blocked on implementation")
            if provider_id not in implemented_provider_ids:
                assert_false(row.get("auto_search_allowed"), f"{provider_id} cannot search before implementation")
                assert_false(row.get("auto_download_allowed"), f"{provider_id} cannot download before implementation")

        for provider_id in provider_ids:
            if provider_id in disabled_boundary_ids:
                continue
            inkdrop_state.update_provider_config(db_path, provider_id, {"settings": {"implementation_status": "implemented"}})

        implemented_registry = registry_by_id(db_path)
        auto_enabled = sorted(
            provider_id
            for provider_id, row in implemented_registry.items()
            if row.get("auto_download_allowed")
        )
        assert_equal(
            auto_enabled,
            sorted(auto_candidate_ids),
            "only catalog auto candidates can auto-download after implementation",
        )
        for provider_id, row in implemented_registry.items():
            if provider_id in disabled_boundary_ids:
                assert_equal(row.get("registry_state"), "disabled_boundary", f"{provider_id} remains boundary after implementation pass")
                assert_false(row.get("auto_search_allowed"), f"{provider_id} boundary cannot search")
                assert_false(row.get("manual_review_allowed"), f"{provider_id} boundary cannot manual-review")
            elif row.get("source_mode") == "metadata_only":
                assert_equal(row.get("registry_state"), "metadata_only" if row.get("provider_type") != "metadata" else "non_acquisition", f"{provider_id} metadata mode does not become acquisition")
                assert_false(row.get("auto_download_allowed"), f"{provider_id} metadata-only cannot auto-download")
            elif row.get("source_mode") in {"assist", "manual_review"}:
                assert_true(row.get("auto_search_allowed"), f"{provider_id} implemented assist/manual source can search")
                assert_true(row.get("manual_review_allowed"), f"{provider_id} implemented assist/manual source can manual-review")
                assert_false(row.get("auto_download_allowed"), f"{provider_id} implemented assist/manual source cannot auto-download")

    ok("all catalog sources remain Arr-style settings templates with guarded automation")


if __name__ == "__main__":
    main()
