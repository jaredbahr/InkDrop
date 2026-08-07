#!/usr/bin/env python3
import sys
import tempfile
import types
import gc
from pathlib import Path

sys.modules.setdefault("requests", types.ModuleType("requests"))

from core import inkdrop_state
from core import inkdrop_source_catalog as catalog
from core import inkdrop_source_registry as registry
from core import inkdrop_source_worker_adapters as adapters
from core import inkdrop_web as web


def fail(message):
    print(f"RUNTIME_SOURCE_SETTINGS_FAIL: {message}")
    raise SystemExit(1)


def ok(message):
    print(f"RUNTIME_SOURCE_SETTINGS_OK: {message}")


def assert_equal(actual, expected, message):
    if actual != expected:
        fail(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value, message):
    if not value:
        fail(message)


def assert_false(value, message):
    if value:
        fail(message)


def assert_manga_alias_defaults(alias_map, provider_label):
    assert_equal(alias_map.get("Oyasumi Punpun"), ["Goodnight Punpun"], f"{provider_label} ships Oyasumi/Goodnight alias")
    assert_equal(alias_map.get("Delicious in Dungeon"), ["Dungeon Meshi"], f"{provider_label} ships Delicious/Dungeon Meshi alias")


def by_id(rows, key=None):
    if key:
        return {str(row.get(key) or ""): row for row in rows or []}
    return {str(row.get("id") or row.get("key") or ""): row for row in rows or []}


def assert_runtime_schema_merge_preserves_user_source_settings(runtime_payload):
    runtime_provider = by_id(runtime_payload.get("providers") or [])["suwayomi"]
    user_disabled_ids = ["4972933717624256217"]
    user_disabled_names = ["Comick (Unoriginal) (EN)"]
    stale_provider = dict(runtime_provider)
    stale_provider["source"] = "user"
    stale_provider["settings"] = {
        "source_template": True,
        "source_mode": "auto",
        "implementation_status": "implemented",
        "auto_download_allowed": True,
        "suwayomi_disabled_source_ids": user_disabled_ids,
        "suwayomi_disabled_source_names": user_disabled_names,
        "editable_fields": ["base_url", "suwayomi_source_ids", "suwayomi_source_names"],
        "policy": {},
    }
    snapshot = {
        "ok": True,
        "providers": [stale_provider],
        "settings": runtime_payload.get("settings") or [],
        "settings_sync_needed": False,
    }

    merged = web.merge_runtime_settings_snapshot(snapshot, runtime_payload)
    merged_suwayomi = by_id(merged.get("providers"))["suwayomi"]
    merged_settings = merged_suwayomi.get("settings") or {}
    merged_policy = merged_settings.get("policy") or {}
    registry_rows = by_id(
        registry.registry_from_settings_snapshot(
            {"ok": True, "providers": [merged_suwayomi], "settings": []},
            include_disabled=True,
        ),
        key="provider_id",
    )
    registry_suwayomi = registry_rows["suwayomi"]
    registry_policy = registry_suwayomi.get("policy") or {}
    source_payload = [
        {
            "id": "2499283573021220255",
            "displayName": "MangaDex (EN)",
            "name": "MangaDex",
            "lang": "en",
        },
        {
            "id": user_disabled_ids[0],
            "displayName": user_disabled_names[0],
            "name": "Comick (Unoriginal)",
            "lang": "en",
        },
    ]
    selected_source_ids = [
        str(row.get("id") or "")
        for row in adapters._suwayomi_selected_sources(registry_suwayomi, source_payload)
    ]

    assert_true(merged["settings_sync_needed"], "stale user-owned provider settings request explicit sync")
    assert_equal(merged["settings_sync_reason"], "runtime_settings_schema_drift", "runtime schema drift reason is surfaced")
    assert_equal(merged_suwayomi["source"], "user", "runtime display merge preserves user-owned provider row")
    assert_true("series_query_aliases" in merged_settings["editable_fields"], "runtime display merge surfaces Suwayomi query aliases")
    assert_true("suwayomi_disabled_source_ids" in merged_settings["editable_fields"], "runtime display merge surfaces disabled source ids")
    assert_true("suwayomi_disabled_source_names" in merged_settings["editable_fields"], "runtime display merge surfaces disabled source names")
    assert_true(
        "suwayomi_source_error_quarantine_threshold" in merged_settings["editable_fields"],
        "runtime display merge surfaces source-error quarantine knobs",
    )
    assert_true(
        "suwayomi_rest_chapter_fallback_enabled" in merged_settings["editable_fields"],
        "runtime display merge surfaces Suwayomi REST chapter fallback knob",
    )
    assert_true(
        "suwayomi_source_error_cooldown_probe_enabled" in merged_settings["editable_fields"],
        "runtime display merge surfaces source-error probe enable knob",
    )
    assert_true(
        "suwayomi_source_error_cooldown_probe_after_seconds" in merged_settings["editable_fields"],
        "runtime display merge surfaces source-error probe interval knob",
    )
    assert_true(
        "suwayomi_volume_gap_cooldown_probe_after_seconds" in merged_settings["editable_fields"],
        "runtime display merge surfaces volume-gap probe interval knob",
    )
    assert_equal(
        merged_policy.get("suwayomi_source_error_cooldown_probe_after_seconds"),
        1800,
        "runtime display merge applies source-error probe interval default",
    )
    assert_true(
        merged_policy.get("suwayomi_rest_chapter_fallback_enabled"),
        "runtime display merge applies Suwayomi REST chapter fallback default",
    )
    assert_equal(
        merged_policy.get("suwayomi_volume_gap_cooldown_probe_after_seconds"),
        300,
        "runtime display merge applies volume-gap probe interval default",
    )
    assert_equal(
        merged_settings["suwayomi_disabled_source_ids"],
        user_disabled_ids,
        "runtime display merge preserves user-disabled Suwayomi source ids",
    )
    assert_equal(
        merged_settings["suwayomi_disabled_source_names"],
        user_disabled_names,
        "runtime display merge preserves user-disabled Suwayomi source names",
    )
    assert_equal(
        registry_policy["suwayomi_disabled_source_ids"],
        user_disabled_ids,
        "registry policy promotes user-disabled Suwayomi source ids",
    )
    assert_equal(
        registry_policy["suwayomi_disabled_source_names"],
        user_disabled_names,
        "registry policy promotes user-disabled Suwayomi source names",
    )
    assert_true("2499283573021220255" in selected_source_ids, "Suwayomi registry still selects enabled source ids")
    assert_false(user_disabled_ids[0] in selected_source_ids, "Suwayomi registry does not select disabled source ids")
    assert_manga_alias_defaults(merged_policy["series_query_aliases"], "merged Suwayomi")


def assert_missing_runtime_templates_are_visible_without_global_sync(runtime_payload):
    providers = runtime_payload.get("providers") or []
    providers_by_id = by_id(providers)
    with tempfile.TemporaryDirectory(prefix="inkdrop-runtime-template-settings-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        inkdrop_state.sync_settings(db_path, providers=[providers_by_id["mangadex"]], settings=[])
        stored_before = inkdrop_state.settings_snapshot(db_path)
        assert_equal(len(stored_before.get("providers") or []), 1, "fixture starts with one persisted provider")

        old_db_path = web.INKDROP_STATE_DB
        try:
            web.INKDROP_STATE_DB = db_path
            public_settings = web.inkdrop_settings_public(sync=False)
        finally:
            web.INKDROP_STATE_DB = old_db_path

        stored_after_get = inkdrop_state.settings_snapshot(db_path)
        assert_equal(
            len(stored_after_get.get("providers") or []),
            1,
            "Settings GET does not persist missing runtime templates",
        )
        public_providers = by_id(public_settings.get("providers") or [])
        suwayomi_folder = public_providers.get("suwayomi_managed_folder") or {}
        assert_true(suwayomi_folder, "Settings GET surfaces missing Suwayomi managed-folder runtime template")
        assert_true(suwayomi_folder.get("settings_runtime_only"), "missing runtime template is marked runtime-only")
        assert_true(suwayomi_folder.get("runtime_template"), "missing runtime template is marked as a template")
        assert_false(suwayomi_folder.get("stored_provider_config"), "missing runtime template is not represented as stored")
        assert_equal(suwayomi_folder.get("source"), "source_catalog", "missing runtime template keeps source-catalog identity")
        assert_false(suwayomi_folder.get("enabled"), "missing Suwayomi managed-folder template stays disabled on GET")
        assert_equal(
            public_settings.get("settings_sync_reason"),
            "runtime_provider_template_missing",
            "missing runtime template advertises explicit sync reason",
        )
        assert_true(
            "suwayomi_managed_folder" in (public_settings.get("runtime_template_provider_ids") or []),
            "missing runtime template id is surfaced for diagnostics",
        )

        old_db_path = web.INKDROP_STATE_DB
        try:
            web.INKDROP_STATE_DB = db_path
            updated = web.claim_inkdrop_provider_settings(
                {
                    "id": "suwayomi_managed_folder",
                    "settings": {"suwayomi_download_root": "/tmp/suwayomi-downloads"},
                }
            )
        finally:
            web.INKDROP_STATE_DB = old_db_path

        updated_providers = by_id(updated.get("providers") or [])
        updated_folder = updated_providers.get("suwayomi_managed_folder") or {}
        updated_settings = updated_folder.get("settings") or {}
        updated_policy = updated_settings.get("policy") or {}
        assert_true(updated_folder, "explicit provider save seeds the selected runtime template")
        assert_equal(updated_folder.get("source"), "user", "explicit provider save claims only the selected template as user settings")
        assert_false(updated_folder.get("enabled"), "explicit provider save does not enable unless requested")
        assert_equal(
            updated_settings.get("suwayomi_download_root"),
            "/tmp/suwayomi-downloads",
            "explicit provider save persists managed-folder root",
        )
        assert_equal(
            updated_policy.get("suwayomi_download_root"),
            "/tmp/suwayomi-downloads",
            "explicit provider save promotes managed-folder root into policy",
        )
        old_db_path = web.INKDROP_STATE_DB
        try:
            web.INKDROP_STATE_DB = db_path
            public_after_claim = web.inkdrop_settings_public(sync=False)
        finally:
            web.INKDROP_STATE_DB = old_db_path

        public_after_providers = by_id(public_after_claim.get("providers") or [])
        public_after_folder = public_after_providers.get("suwayomi_managed_folder") or {}
        assert_equal(public_after_folder.get("source"), "user", "claimed runtime template appears as user-owned provider")
        assert_false(public_after_folder.get("settings_runtime_only"), "claimed runtime template is no longer runtime-only")
        assert_false(public_after_folder.get("runtime_template"), "claimed runtime template no longer uses runtime template marker")
        assert_true(
            "suwayomi_managed_folder" not in (public_after_claim.get("runtime_template_provider_ids") or []),
            "claimed runtime template is removed from runtime template diagnostics",
        )
        inkdrop_state.clear_settings_caches()
        gc.collect()


def main():
    payload = web.runtime_provider_settings()
    providers = payload.get("providers") or []
    settings = payload.get("settings") or []
    provider_ids = [str(provider.get("id") or "") for provider in providers]
    assert_equal(len(provider_ids), len(set(provider_ids)), "runtime provider ids are unique")

    providers_by_id = by_id(providers)
    settings_by_key = by_id(settings)
    assert_runtime_schema_merge_preserves_user_source_settings(payload)
    assert_missing_runtime_templates_are_visible_without_global_sync(payload)
    assert_true("standard_ebooks" in providers_by_id, "Standard Ebooks source template is present")
    assert_true("prowlarr_nyaa" in providers_by_id, "Prowlarr Nyaa source template is present")
    assert_true("prowlarr_tokyo_toshokan_manga" in providers_by_id, "Prowlarr Tokyo Toshokan manga source template is present")
    assert_true("prowlarr_torrentleech_comics" in providers_by_id, "Prowlarr TorrentLeech comics source template is present")
    assert_false("prowlarr_kat_comics" in providers_by_id, "legacy KAT preset is hidden from new runtime settings")
    assert_false("prowlarr_pirate_bay_comics" in providers_by_id, "legacy Pirate Bay preset is hidden from new runtime settings")
    assert_false("prowlarr_torrentdownload_comics" in providers_by_id, "legacy TorrentDownload preset is hidden from new runtime settings")
    assert_true("prowlarr_dognzb_comics" in providers_by_id, "Prowlarr DOGnzb comics source template is present")
    assert_true("generic_torznab_indexer" in providers_by_id, "Generic Torznab source template is present")
    assert_true("generic_newznab_indexer" in providers_by_id, "Generic Newznab source template is present")
    assert_true("generic_torrent_rss_feed" in providers_by_id, "Generic torrent RSS source template is present")
    assert_true("generic_torrent_detail_rss_feed" in providers_by_id, "Generic torrent detail RSS source template is present")
    assert_false("generic_torrent_html_search" in providers_by_id, "legacy generic HTML scraper is hidden from new runtime settings")
    assert_false("generic_torrent_detail_search" in providers_by_id, "legacy detail-page scraper is hidden from new runtime settings")
    assert_true("generic_rss_direct_feed" in providers_by_id, "Generic RSS direct feed template is present")
    assert_true("rss_getcomics" in providers_by_id, "RSS/GetComics template is present")
    assert_true("generic_safe_http_direct_download" in providers_by_id, "Generic safe HTTP direct download template is present")
    assert_true("local_manual_inbox" in providers_by_id, "Local/manual inbox template is present")
    assert_true("generic_opds_catalog" in providers_by_id, "Generic OPDS catalog template is present")
    assert_true("suwayomi" in providers_by_id, "Suwayomi source template is present")
    assert_true("suwayomi_managed_folder" in providers_by_id, "Suwayomi managed folder source template is present")
    for provider_id in catalog.VAGUE_DIRECT_SOURCE_BUCKET_IDS:
        assert_false(provider_id in providers_by_id, f"{provider_id} vague direct bucket is absent from runtime product settings")
    assert_equal(providers_by_id["mangadex"]["source"], "runtime", "MangaDex runtime provider stays authoritative")
    assert_equal(providers_by_id["comicscodes"]["source"], "runtime", "ComicsCodes runtime provider stays authoritative")
    mangadex_settings = providers_by_id["mangadex"].get("settings") or {}
    assert_true("fetch_at_home_pages" in mangadex_settings["editable_fields"], "MangaDex At-Home setting is editable at runtime")
    assert_true("mangadex_feed_page_size" in mangadex_settings["editable_fields"], "MangaDex feed page size is editable at runtime")
    assert_true("mangadex_feed_max_pages" in mangadex_settings["editable_fields"], "MangaDex feed page cap is editable at runtime")
    assert_true("allowed_languages" in mangadex_settings["editable_fields"], "MangaDex language policy is editable at runtime")
    assert_true("content_ratings" in mangadex_settings["editable_fields"], "MangaDex content rating policy is editable at runtime")
    assert_true("health_provider_ids" in mangadex_settings["editable_fields"], "MangaDex health mapping is editable at runtime")
    assert_true("series_query_aliases" in mangadex_settings["editable_fields"], "MangaDex series aliases are editable at runtime")
    assert_true("enable_volume_page_pack" in mangadex_settings["editable_fields"], "MangaDex volume page-pack toggle is editable at runtime")
    assert_equal(mangadex_settings["source_mode"], "auto", "MangaDex runtime mode is auto")
    assert_true(mangadex_settings["auto_download_allowed"], "MangaDex runtime At-Home page packs are auto-capable")
    assert_false(mangadex_settings["requires_manual_confirm"], "MangaDex runtime auto path does not require manual confirm")
    assert_equal(mangadex_settings["allowed_languages"], ["en"], "MangaDex runtime allowed language default")
    assert_equal(mangadex_settings["policy"]["health_provider_ids"], ["mangadex", "mangadex_api"], "MangaDex runtime health providers")
    assert_true(mangadex_settings["policy"]["fetch_at_home_pages"], "MangaDex At-Home fetching is enabled at runtime")
    assert_true(mangadex_settings["policy"]["enable_volume_page_pack"], "MangaDex volume page packs are enabled at runtime")
    assert_equal(mangadex_settings["policy"]["volume_page_pack_min_chapters"], 2, "MangaDex runtime volume pack minimum chapter count")
    assert_equal(mangadex_settings["policy"]["volume_page_pack_max_chapters"], 40, "MangaDex runtime volume pack chapter cap")
    assert_equal(mangadex_settings["policy"]["mangadex_feed_page_size"], 100, "MangaDex feed page size default at runtime")
    assert_manga_alias_defaults(mangadex_settings["policy"]["series_query_aliases"], "MangaDex")
    assert_equal(mangadex_settings["policy"]["mangadex_feed_max_pages"], 3, "MangaDex feed page cap default at runtime")

    suwayomi_settings = providers_by_id["suwayomi"].get("settings") or {}
    assert_equal(providers_by_id["suwayomi"]["source"], "source_catalog", "Suwayomi comes from source catalog")
    assert_equal(providers_by_id["suwayomi"]["settings_group"], "download_sources", "Suwayomi runtime settings group")
    assert_equal(suwayomi_settings["source_mode"], "auto", "Suwayomi runtime mode is auto")
    assert_true(suwayomi_settings["auto_download_allowed"], "Suwayomi runtime page packs are auto-capable")
    assert_false(suwayomi_settings["requires_manual_confirm"], "Suwayomi runtime auto path does not require manual confirm")
    assert_equal(suwayomi_settings["base_url"], "http://127.0.0.1:4568", "Suwayomi runtime base URL")
    assert_equal(suwayomi_settings["source_allowed_hosts"], ["127.0.0.1", "localhost"], "Suwayomi runtime allowed hosts")
    assert_true("suwayomi_source_ids" in suwayomi_settings["editable_fields"], "Suwayomi source ids are editable at runtime")
    assert_true("suwayomi_source_names" in suwayomi_settings["editable_fields"], "Suwayomi source names are editable at runtime")
    assert_true("series_query_aliases" in suwayomi_settings["editable_fields"], "Suwayomi series aliases are editable at runtime")
    assert_true("suwayomi_page_image_extension" in suwayomi_settings["editable_fields"], "Suwayomi page extension is editable at runtime")
    assert_true("enable_volume_page_pack" in suwayomi_settings["editable_fields"], "Suwayomi volume page-pack toggle is editable at runtime")
    assert_manga_alias_defaults(suwayomi_settings["policy"]["series_query_aliases"], "Suwayomi")
    assert_equal(suwayomi_settings["policy"]["health_provider_ids"], ["suwayomi"], "Suwayomi runtime health provider")
    assert_true(suwayomi_settings["policy"]["enable_volume_page_pack"], "Suwayomi volume page packs are enabled at runtime")
    assert_equal(suwayomi_settings["policy"]["volume_page_pack_min_chapters"], 2, "Suwayomi runtime volume pack minimum chapter count")
    assert_equal(suwayomi_settings["policy"]["volume_page_pack_max_chapters"], 40, "Suwayomi runtime volume pack chapter cap")

    suwayomi_folder = providers_by_id["suwayomi_managed_folder"]
    suwayomi_folder_settings = suwayomi_folder.get("settings") or {}
    assert_equal(suwayomi_folder["source"], "source_catalog", "Suwayomi managed folder comes from source catalog")
    assert_equal(suwayomi_folder["settings_group"], "download_sources", "Suwayomi managed folder settings group")
    assert_false(suwayomi_folder["enabled"], "Suwayomi managed folder starts disabled")
    assert_equal(suwayomi_folder_settings["source_mode"], "assist", "Suwayomi managed folder starts in assist/report mode")
    assert_false(suwayomi_folder_settings["auto_download_allowed"], "Suwayomi managed folder is not auto-download until scanner/import-ready gates exist")
    assert_false(suwayomi_folder_settings["requires_manual_confirm"], "Suwayomi managed folder strict gates should not require default manual review")
    for field in (
        "suwayomi_folder_mode",
        "suwayomi_download_root",
        "suwayomi_import_staging_root",
        "suwayomi_folder_copy_policy",
        "suwayomi_folder_cleanup_policy",
        "suwayomi_folder_import_ready_enabled",
    ):
        assert_true(field in suwayomi_folder_settings["editable_fields"], f"Suwayomi managed folder exposes {field}")
    assert_equal(
        suwayomi_folder_settings["policy"]["suwayomi_folder_mode"],
        "report_only",
        "Suwayomi managed folder defaults to report-only",
    )
    assert_equal(
        suwayomi_folder_settings["policy"]["suwayomi_folder_copy_policy"],
        "copy_to_staging",
        "Suwayomi managed folder copies into staging before import",
    )
    assert_false(
        suwayomi_folder_settings["policy"]["suwayomi_folder_import_ready_enabled"],
        "Suwayomi managed folder import-ready promotion starts disabled",
    )

    catalog_providers = [provider for provider in providers if provider.get("source") == "source_catalog"]
    assert_true(len(catalog_providers) >= 20, "catalog templates are appended to runtime settings")
    assert_equal(
        settings_by_key["sources.catalog.auto_download_candidates"]["value"],
        [
            "standard_ebooks",
            "gutendex",
            "mangadex",
            "suwayomi",
            "prowlarr_nyaa",
            "prowlarr_tokyo_toshokan_manga",
            "prowlarr_torrentleech_comics",
            "prowlarr_dognzb_comics",
        ],
        "runtime settings include source catalog auto candidates",
    )

    standard = providers_by_id["standard_ebooks"]
    standard_settings = standard.get("settings") or {}
    assert_equal(standard["settings_group"], "download_sources", "Standard Ebooks settings group")
    assert_false(standard["enabled"], "Standard Ebooks template starts disabled")
    assert_true(standard_settings["user_enableable"], "Standard Ebooks can be enabled by a user")
    assert_true("source_mode" in standard_settings["editable_fields"], "source mode is editable")
    assert_true("allowed_languages" in standard_settings["editable_fields"], "Standard Ebooks language policy is editable")
    assert_equal(standard_settings["policy"]["health_provider_ids"], ["standard_ebooks"], "Standard Ebooks health provider")

    nyaa = providers_by_id["prowlarr_nyaa"]
    nyaa_settings = nyaa.get("settings") or {}
    assert_equal(nyaa["settings_group"], "indexers", "Prowlarr Nyaa settings group")
    assert_false(nyaa["enabled"], "Prowlarr Nyaa starts disabled")
    assert_equal(nyaa_settings["source_mode"], "auto", "Prowlarr Nyaa starts auto-capable")
    assert_true(nyaa_settings["auto_download_allowed"], "Prowlarr Nyaa can auto-download when enabled")
    assert_false(nyaa_settings["requires_manual_confirm"], "Prowlarr Nyaa strict gates replace manual confirm")
    assert_equal(nyaa_settings["rights_gate"], "user_owned_collection_required", "Prowlarr Nyaa ownership gate")
    assert_true("indexer_ids" in nyaa_settings["editable_fields"], "Prowlarr Nyaa target indexers are editable")
    assert_true("categoryless_fallback_indexer_ids" in nyaa_settings["editable_fields"], "Prowlarr Nyaa categoryless fallback indexers are editable")
    assert_equal(nyaa_settings["policy"]["indexer_ids"], [], "Prowlarr Nyaa ships with no indexer IDs preselected; the user maps their own Prowlarr indexer")
    assert_equal(nyaa_settings["policy"]["categoryless_fallback_indexer_ids"], [], "Prowlarr Nyaa categoryless fallback ships empty until the user maps an indexer")
    assert_equal(nyaa_settings["policy"]["allowed_languages"], ["en"], "Prowlarr Nyaa language policy")
    assert_equal(nyaa_settings["policy"]["scope_policy"], "manga_metadata_or_manga_publisher", "Prowlarr Nyaa manga scope policy")
    assert_true("health_provider_ids" in nyaa_settings["editable_fields"], "Prowlarr Nyaa health providers are editable")
    assert_true("download_client_by_protocol" in nyaa_settings["editable_fields"], "Prowlarr Nyaa download client mapping is editable")
    assert_equal(nyaa_settings["policy"]["download_client_by_protocol"]["torrent"], "qbittorrent", "Prowlarr Nyaa qBit handoff default")

    tokyo = providers_by_id["prowlarr_tokyo_toshokan_manga"]
    tokyo_settings = tokyo.get("settings") or {}
    assert_equal(tokyo["settings_group"], "indexers", "Prowlarr Tokyo Toshokan manga settings group")
    assert_false(tokyo["enabled"], "Prowlarr Tokyo Toshokan manga starts disabled")
    assert_equal(tokyo_settings["source_mode"], "auto", "Prowlarr Tokyo Toshokan manga starts auto-capable")
    assert_true(tokyo_settings["auto_download_allowed"], "Prowlarr Tokyo Toshokan manga can auto-download when enabled")
    assert_false(tokyo_settings["requires_manual_confirm"], "Prowlarr Tokyo Toshokan manga strict gates replace manual confirm")
    assert_equal(tokyo_settings["rights_gate"], "user_owned_collection_required", "Prowlarr Tokyo Toshokan manga ownership gate")
    assert_true("indexer_ids" in tokyo_settings["editable_fields"], "Prowlarr Tokyo Toshokan manga indexer IDs are editable")
    assert_equal(tokyo_settings["policy"]["indexer_ids"], [], "Prowlarr Tokyo Toshokan manga ships with no indexer IDs preselected; the user maps their own Prowlarr indexer")
    assert_equal(tokyo_settings["policy"]["allowed_languages"], ["en"], "Prowlarr Tokyo Toshokan manga language policy")
    assert_equal(tokyo_settings["policy"]["scope_policy"], "manga_metadata_or_manga_publisher", "Prowlarr Tokyo Toshokan manga manga scope policy")

    tl_comics = providers_by_id["prowlarr_torrentleech_comics"]
    tl_settings = tl_comics.get("settings") or {}
    assert_equal(tl_comics["settings_group"], "indexers", "Prowlarr TorrentLeech comics settings group")
    assert_false(tl_comics["enabled"], "Prowlarr TorrentLeech comics starts disabled")
    assert_equal(tl_settings["source_mode"], "auto", "Prowlarr TorrentLeech comics starts auto-capable")
    assert_true(tl_settings["auto_download_allowed"], "Prowlarr TorrentLeech comics can auto-download when enabled")
    assert_false(tl_settings["requires_manual_confirm"], "Prowlarr TorrentLeech comics strict gates replace manual confirm")
    assert_true("indexer_ids" in tl_settings["editable_fields"], "Prowlarr TorrentLeech comics indexer IDs are editable")
    assert_true("weekly_pack_query_limit" in tl_settings["editable_fields"], "Prowlarr TorrentLeech comics weekly pack cap is editable")
    assert_equal(tl_settings["policy"]["indexer_ids"], [], "Prowlarr TorrentLeech comics ships with no indexer IDs preselected; the user maps their own Prowlarr indexer")
    assert_equal(tl_settings["policy"]["scope_policy"], "western_comic_pack", "Prowlarr TorrentLeech comics scope policy")
    assert_equal(tl_settings["policy"]["weekly_pack_query_limit"], 8, "Prowlarr TorrentLeech comics can ask dated weekly-pack queries")
    assert_equal(tl_settings["policy"]["pack_detail_max_fetches"], 20, "Prowlarr TorrentLeech comics detail cap supports weekly pack NFO scanning")

    dog = providers_by_id["prowlarr_dognzb_comics"]
    dog_settings = dog.get("settings") or {}
    assert_equal(dog["settings_group"], "indexers", "Prowlarr DOGnzb comics settings group")
    assert_false(dog["enabled"], "Prowlarr DOGnzb comics starts disabled")
    assert_equal(dog_settings["source_mode"], "auto", "Prowlarr DOGnzb comics starts auto-capable")
    assert_true(dog_settings["auto_download_allowed"], "Prowlarr DOGnzb comics can auto-download when enabled")
    assert_false(dog_settings["requires_manual_confirm"], "Prowlarr DOGnzb comics strict gates replace manual confirm")
    assert_equal(dog_settings["rights_gate"], "user_owned_collection_required", "Prowlarr DOGnzb comics ownership gate")
    assert_true("indexer_ids" in dog_settings["editable_fields"], "Prowlarr DOGnzb comics indexer IDs are editable")
    assert_true("download_client_by_protocol" in dog_settings["editable_fields"], "Prowlarr DOGnzb comics client mapping is editable")
    assert_true("weekly_pack_query_limit" in dog_settings["editable_fields"], "Prowlarr DOGnzb comics weekly pack cap is editable")
    assert_equal(dog_settings["policy"]["indexer_ids"], [], "Prowlarr DOGnzb comics ships with no indexer IDs preselected; the user maps their own Prowlarr indexer")
    assert_equal(dog_settings["policy"]["scope_policy"], "western_comic_pack", "Prowlarr DOGnzb comics scope policy")
    assert_equal(dog_settings["policy"]["download_client_by_protocol"], {"usenet": "sabnzbd"}, "Prowlarr DOGnzb comics uses SABnzbd for Usenet")
    assert_equal(dog_settings["policy"]["weekly_pack_query_limit"], 8, "Prowlarr DOGnzb comics can ask dated weekly-pack queries")
    assert_equal(dog_settings["policy"]["pack_detail_max_fetches"], 20, "Prowlarr DOGnzb comics detail cap supports weekly pack NFO scanning")
    assert_equal(dog_settings["policy"]["pack_detail_allowed_hosts"], ["dognzb.cr"], "Prowlarr DOGnzb comics sidecar host is provider-scoped")

    torznab = providers_by_id["generic_torznab_indexer"]
    torznab_settings = torznab.get("settings") or {}
    assert_equal(torznab["settings_group"], "indexers", "Generic Torznab settings group")
    assert_false(torznab["enabled"], "Generic Torznab starts disabled")
    assert_equal(torznab_settings["source_mode"], "assist", "Generic Torznab starts as assist")
    assert_false(torznab_settings["auto_download_allowed"], "Generic Torznab is not auto-download by default")
    assert_true("api_key" in (torznab_settings.get("secret_fields") or []), "Generic Torznab API key is secret")
    assert_true("secret_ref" in torznab_settings["editable_fields"], "Generic Torznab external secret ref is editable")
    assert_true("download_client_by_protocol" in torznab_settings["editable_fields"], "Generic Torznab download client mapping is editable")
    assert_equal(torznab_settings["policy"]["download_client_by_protocol"]["torrent"], "qbittorrent", "Generic Torznab qBit handoff default")
    assert_equal(torznab_settings["download_client_by_protocol"]["torrent"], "qbittorrent", "Generic Torznab qBit handoff default is surfaced")
    assert_equal(torznab_settings["weekly_pack_query_limit"], 4, "Generic Torznab weekly pack query cap")
    assert_true("weekly_pack_query_limit" in torznab_settings["editable_fields"], "Generic Torznab weekly pack query cap is editable")
    assert_true(torznab_settings["pack_detail_fetch"], "Generic Torznab pack detail fetch starts enabled")
    assert_true("pack_detail_allowed_hosts" in torznab_settings["editable_fields"], "Generic Torznab pack detail hosts are editable")

    newznab = providers_by_id["generic_newznab_indexer"]
    newznab_settings = newznab.get("settings") or {}
    assert_equal(newznab["settings_group"], "indexers", "Generic Newznab settings group")
    assert_false(newznab["enabled"], "Generic Newznab starts disabled")
    assert_equal(newznab_settings["source_mode"], "assist", "Generic Newznab starts as assist")
    assert_false(newznab_settings["auto_download_allowed"], "Generic Newznab is not auto-download by default")
    assert_true("api_key" in (newznab_settings.get("secret_fields") or []), "Generic Newznab API key is secret")
    assert_true("secret_ref" in newznab_settings["editable_fields"], "Generic Newznab external secret ref is editable")
    assert_true("categories" in newznab_settings["editable_fields"], "Generic Newznab categories are editable")
    assert_true("download_client_by_protocol" in newznab_settings["editable_fields"], "Generic Newznab download client mapping is editable")
    assert_equal(newznab_settings["policy"]["download_client_by_protocol"]["usenet"], "sabnzbd", "Generic Newznab SAB handoff default")
    assert_equal(newznab_settings["download_client_by_protocol"]["usenet"], "sabnzbd", "Generic Newznab SAB handoff default is surfaced")
    assert_equal(newznab_settings["weekly_pack_query_limit"], 4, "Generic Newznab weekly pack query cap")
    assert_true("weekly_pack_query_limit" in newznab_settings["editable_fields"], "Generic Newznab weekly pack query cap is editable")
    assert_true(newznab_settings["pack_detail_fetch"], "Generic Newznab pack detail fetch starts enabled")
    assert_true("pack_detail_allowed_hosts" in newznab_settings["editable_fields"], "Generic Newznab pack detail hosts are editable")

    torrent_rss = providers_by_id["generic_torrent_rss_feed"]
    torrent_rss_settings = torrent_rss.get("settings") or {}
    assert_equal(torrent_rss["settings_group"], "indexers", "Generic torrent RSS settings group")
    assert_false(torrent_rss["enabled"], "Generic torrent RSS starts disabled")
    assert_equal(torrent_rss_settings["source_mode"], "assist", "Generic torrent RSS starts as assist")
    assert_false(torrent_rss_settings["auto_download_allowed"], "Generic torrent RSS is not auto-download by default")
    assert_true("base_url" in torrent_rss_settings["editable_fields"], "Generic torrent RSS feed URL is editable")

    torrent_detail_rss = providers_by_id["generic_torrent_detail_rss_feed"]
    torrent_detail_rss_settings = torrent_detail_rss.get("settings") or {}
    assert_equal(torrent_detail_rss["settings_group"], "indexers", "Generic torrent detail RSS settings group")
    assert_false(torrent_detail_rss["enabled"], "Generic torrent detail RSS starts disabled")
    assert_equal(torrent_detail_rss_settings["source_mode"], "assist", "Generic torrent detail RSS starts as assist")
    assert_false(torrent_detail_rss_settings["auto_download_allowed"], "Generic torrent detail RSS is not auto-download by default")
    assert_true("base_url" in torrent_detail_rss_settings["editable_fields"], "Generic torrent detail RSS feed URL is editable")
    assert_true("minimum_seeders" in torrent_detail_rss_settings["editable_fields"], "Generic torrent detail RSS seed gate is editable")

    direct_feed = providers_by_id["generic_rss_direct_feed"]
    direct_feed_settings = direct_feed.get("settings") or {}
    assert_equal(direct_feed["settings_group"], "download_sources", "Generic RSS direct feed settings group")
    assert_false(direct_feed["enabled"], "Generic RSS direct feed starts disabled")
    assert_equal(direct_feed_settings["source_mode"], "assist", "Generic RSS direct feed starts as assist")
    assert_false(direct_feed_settings["auto_download_allowed"], "Generic RSS direct feed is not auto-download by default")
    assert_true("base_url" in direct_feed_settings["editable_fields"], "Generic RSS direct feed URL is editable")

    getcomics = providers_by_id["rss_getcomics"]
    getcomics_settings = getcomics.get("settings") or {}
    assert_equal(getcomics["integration_role"], "experimental_provider", "GetComics runtime role stays experimental")
    assert_equal(getcomics_settings["certification_status"], "Experimental", "GetComics runtime certification stays experimental")
    assert_equal(
        getcomics_settings["source_certification"].get("blocked_gates"),
        ["live_provider_acceptance"],
        "GetComics runtime blocks live provider certification",
    )
    assert_equal(getcomics_settings.get("source_kind"), "rss_detail_probe_feed", "GetComics runtime uses bounded detail/probe source kind")
    assert_equal(getcomics_settings.get("policy", {}).get("shared_file_hosts"), ["pixeldrain"], "GetComics runtime allows only Pixeldrain transport")
    assert_false(getcomics_settings.get("auto_download_allowed"), "GetComics runtime stays non-automatic")
    assert_true(getcomics_settings.get("requires_manual_confirm"), "GetComics runtime keeps manual confirmation")

    safe_http = providers_by_id["generic_safe_http_direct_download"]
    safe_http_settings = safe_http.get("settings") or {}
    assert_equal(safe_http["settings_group"], "download_sources", "Generic safe HTTP settings group")
    assert_false(safe_http["enabled"], "Generic safe HTTP starts disabled")
    assert_equal(safe_http_settings["source_mode"], "assist", "Generic safe HTTP starts as assist")
    assert_equal(safe_http_settings["certification_status"], "Beta", "Generic safe HTTP is separately certified")
    for field in ("allowed_content_types", "max_bytes", "max_redirects", "max_probe_links", "probe_method"):
        assert_true(field in safe_http_settings["editable_fields"], f"Generic safe HTTP exposes {field}")

    manual_inbox = providers_by_id["local_manual_inbox"]
    manual_inbox_settings = manual_inbox.get("settings") or {}
    assert_equal(manual_inbox["settings_group"], "download_sources", "Local/manual inbox settings group")
    assert_false(manual_inbox["enabled"], "Local/manual inbox starts disabled")
    assert_equal(manual_inbox_settings["source_mode"], "assist", "Local/manual inbox starts as assist")
    assert_equal(manual_inbox_settings["certification_status"], "Beta", "Local/manual inbox is separately certified")
    assert_equal(
        manual_inbox_settings["policy"]["direct_url_policy"],
        "local_filesystem_only_no_remote_fetch",
        "Local/manual inbox is local-only",
    )
    for field in (
        "manual_inbox_root",
        "manual_inbox_staging_root",
        "manual_inbox_copy_policy",
        "manual_inbox_cleanup_policy",
        "manual_inbox_min_age_seconds",
        "manual_inbox_max_scan_files",
    ):
        assert_true(field in manual_inbox_settings["editable_fields"], f"Local/manual inbox exposes {field}")

    opds = providers_by_id["generic_opds_catalog"]
    opds_settings = opds.get("settings") or {}
    assert_equal(opds["settings_group"], "download_sources", "Generic OPDS settings group")
    assert_false(opds["enabled"], "Generic OPDS starts disabled")
    assert_equal(opds_settings["source_mode"], "assist", "Generic OPDS starts as assist")
    assert_false(opds_settings["auto_download_allowed"], "Generic OPDS is not auto-download by default")
    assert_true("base_url" in opds_settings["editable_fields"], "Generic OPDS catalog URL is editable")
    assert_true("allowed_languages" in opds_settings["editable_fields"], "Generic OPDS language policy is editable")
    assert_true("import_handoff_expectation" in opds_settings["editable_fields"], "Generic OPDS import handoff is editable")
    assert_equal(opds_settings["policy"]["health_provider_ids"], ["opds_catalog"], "Generic OPDS health provider")

    comic_dl = providers_by_id["comic_dl"]
    comic_dl_settings = comic_dl.get("settings") or {}
    assert_equal(comic_dl["settings_group"], "download_sources", "Comic-DL uses download source settings group")
    assert_false(comic_dl["enabled"], "Comic-DL starts disabled")
    assert_equal(comic_dl_settings["source_mode"], "manual_review", "Comic-DL starts as manual review")
    assert_false(comic_dl_settings["auto_download_allowed"], "Comic-DL is not auto-download by default")
    assert_true("auto_stage_tool_output" in comic_dl_settings["editable_fields"], "Comic-DL auto-stage policy is editable")
    assert_true("staged_output_root" in comic_dl_settings["editable_fields"], "Comic-DL staged output root is editable")
    assert_true("command_executable" in comic_dl_settings["editable_fields"], "Comic-DL command executable is editable")
    assert_true("command_args" in comic_dl_settings["editable_fields"], "Comic-DL command args are editable")
    assert_true("secret_env" in comic_dl_settings["editable_fields"], "Comic-DL secret env refs are editable")

    with tempfile.TemporaryDirectory(prefix="inkdrop-runtime-source-settings-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        result = inkdrop_state.sync_settings(db_path, providers=providers, settings=settings)
        assert_true(result.get("ok"), "runtime settings can sync into provider_configs")
        snapshot = inkdrop_state.settings_snapshot(db_path)
        snapshot_providers = by_id(snapshot.get("providers") or [])
        assert_true("standard_ebooks" in snapshot_providers, "Standard Ebooks persists to provider_configs")
        assert_false(snapshot_providers["standard_ebooks"]["enabled"], "persisted Standard Ebooks remains disabled")
        assert_equal(snapshot_providers["mangadex"]["source"], "runtime", "persisted MangaDex remains runtime")
        legacy_html = catalog.provider_config_template(catalog.provider_entry("generic_torrent_html_search"))
        legacy_html["source"] = "user"
        legacy_html["enabled"] = True
        legacy_html["settings"]["search_url_templates"] = ["https://legacy.example/search?q={query}"]
        inkdrop_state.sync_settings(db_path, providers=[legacy_html], settings=[])
        inkdrop_state.sync_settings(db_path, providers=providers, settings=settings)
        persisted_legacy = inkdrop_state.provider_config(db_path, "generic_torrent_html_search")
        assert_true(persisted_legacy["enabled"], "persisted legacy HTML source survives catalog reconciliation")
        assert_equal(persisted_legacy["source"], "user", "persisted legacy HTML source keeps user ownership")
        assert_equal(
            persisted_legacy["settings"]["search_url_templates"],
            ["https://legacy.example/search?q={query}"],
            "persisted legacy HTML source keeps user configuration",
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_torznab_indexer",
            {
                "enabled": True,
                "settings": {
                    "base_url": "http://jackett.local/api/v2.0/indexers/example/results/torznab",
                    "categories": ["7030"],
                    "minimum_seeders": 3,
                    "weekly_pack_query_limit": 2,
                    "pack_detail_allowed_hosts": ["jackett.local"],
                },
            },
        )
        torznab_config = inkdrop_state.provider_config(db_path, "generic_torznab_indexer")
        assert_true(torznab_config["enabled"], "runtime Generic Torznab can be enabled")
        assert_equal(torznab_config["settings"]["base_url"], "http://jackett.local/api/v2.0/indexers/example/results/torznab", "runtime Generic Torznab settings URL persists")
        registry_rows = by_id(registry.registry_from_settings_snapshot(inkdrop_state.settings_snapshot(db_path), include_disabled=True), key="provider_id")
        assert_equal(registry_rows["generic_torznab_indexer"]["base_url"], "http://jackett.local/api/v2.0/indexers/example/results/torznab", "runtime Generic Torznab registry uses settings URL")
        assert_equal(registry_rows["generic_torznab_indexer"]["policy"]["minimum_seeders"], 3, "runtime Generic Torznab registry uses settings seed gate")
        inkdrop_state.update_provider_config(
            db_path,
            "generic_newznab_indexer",
            {
                "enabled": True,
                "settings": {
                    "base_url": "https://newznab.example/api",
                    "categories": ["7030"],
                    "weekly_pack_query_limit": 2,
                    "pack_detail_allowed_hosts": ["newznab.example"],
                },
            },
        )
        newznab_config = inkdrop_state.provider_config(db_path, "generic_newznab_indexer")
        assert_true(newznab_config["enabled"], "runtime Generic Newznab can be enabled")
        assert_equal(newznab_config["settings"]["base_url"], "https://newznab.example/api", "runtime Generic Newznab settings URL persists")
        registry_rows = by_id(registry.registry_from_settings_snapshot(inkdrop_state.settings_snapshot(db_path), include_disabled=True), key="provider_id")
        assert_equal(registry_rows["generic_newznab_indexer"]["base_url"], "https://newznab.example/api", "runtime Generic Newznab registry uses settings URL")
        assert_equal(registry_rows["generic_newznab_indexer"]["policy"]["weekly_pack_query_limit"], 2, "runtime Generic Newznab registry uses settings weekly cap")
        old_db_path = web.INKDROP_STATE_DB
        try:
            web.INKDROP_STATE_DB = db_path
            added_snapshot = web.add_inkdrop_provider_from_template(
                {
                    "template_id": "generic_newznab_indexer",
                    "display_name": "Smoke NZB Indexer",
                    "base_url": "https://smoke-newznab.example/api",
                    "settings": {"categories": ["7030"]},
                }
            )
        finally:
            web.INKDROP_STATE_DB = old_db_path
        added_providers = by_id(added_snapshot.get("providers") or [])
        assert_true("newznab_smoke_nzb_indexer" in added_providers, "runtime web helper adds Newznab template instance")
        assert_false(added_providers["newznab_smoke_nzb_indexer"]["enabled"], "runtime web helper clone starts disabled")
        assert_equal(
            added_providers["newznab_smoke_nzb_indexer"]["settings"]["base_url"],
            "https://smoke-newznab.example/api",
            "runtime web helper clone stores settings URL",
        )
        inkdrop_state.update_provider_config(
            db_path,
            "prowlarr_nyaa",
            {
                "enabled": True,
                "settings": {
                    "indexer_ids": ["6", "46"],
                    "categoryless_fallback_indexer_ids": ["46"],
                    "minimum_seeders": 2,
                    "source_mode": "auto",
                    "auto_download_allowed": True,
                    "requires_manual_confirm": False,
                },
            },
        )
        nyaa_config = inkdrop_state.provider_config(db_path, "prowlarr_nyaa")
        assert_true(nyaa_config["enabled"], "runtime Prowlarr Nyaa can be enabled")
        assert_equal(nyaa_config["settings"]["indexer_ids"], ["6", "46"], "runtime Prowlarr Nyaa indexer IDs persist")
        assert_equal(nyaa_config["settings"]["categoryless_fallback_indexer_ids"], ["46"], "runtime Prowlarr Nyaa categoryless fallback IDs persist")
        registry_rows = by_id(registry.registry_from_settings_snapshot(inkdrop_state.settings_snapshot(db_path), include_disabled=True), key="provider_id")
        assert_equal(registry_rows["prowlarr_nyaa"]["policy"]["indexer_ids"], ["6", "46"], "runtime Prowlarr Nyaa registry uses settings indexer IDs")
        assert_equal(registry_rows["prowlarr_nyaa"]["policy"]["categoryless_fallback_indexer_ids"], ["46"], "runtime Prowlarr Nyaa registry uses categoryless fallback IDs")
        assert_equal(registry_rows["prowlarr_nyaa"]["policy"]["minimum_seeders"], 2, "runtime Prowlarr Nyaa registry uses settings seed gate")
        assert_equal(registry_rows["prowlarr_nyaa"]["registry_state"], "ready", "enabled Prowlarr Nyaa registry state")
        assert_true(registry_rows["prowlarr_nyaa"]["auto_download_allowed"], "enabled Prowlarr Nyaa can auto-handoff after strict verdicts")
        assert_false(registry_rows["prowlarr_nyaa"]["requires_manual_review"], "enabled Prowlarr Nyaa does not require manual review")
        assert_equal(
            registry_rows["prowlarr_nyaa"]["base_url"],
            providers_by_id["prowlarr"].get("base_url"),
            "runtime Prowlarr Nyaa inherits parent Prowlarr URL",
        )
        assert_equal(
            registry_rows["prowlarr_nyaa"]["secret_ref"],
            providers_by_id["prowlarr"].get("secret_ref"),
            "runtime Prowlarr Nyaa inherits parent Prowlarr secret",
        )
        inkdrop_state.update_provider_config(
            db_path,
            "prowlarr_tokyo_toshokan_manga",
            {
                "enabled": True,
                "settings": {
                    "indexer_ids": ["45"],
                    "minimum_seeders": 2,
                    "source_mode": "auto",
                    "auto_download_allowed": True,
                    "requires_manual_confirm": False,
                },
            },
        )
        tokyo_config = inkdrop_state.provider_config(db_path, "prowlarr_tokyo_toshokan_manga")
        assert_true(tokyo_config["enabled"], "runtime Prowlarr Tokyo Toshokan manga can be enabled")
        assert_equal(tokyo_config["settings"]["indexer_ids"], ["45"], "runtime Prowlarr Tokyo Toshokan manga indexer IDs persist")
        registry_rows = by_id(registry.registry_from_settings_snapshot(inkdrop_state.settings_snapshot(db_path), include_disabled=True), key="provider_id")
        assert_equal(registry_rows["prowlarr_tokyo_toshokan_manga"]["registry_state"], "ready", "enabled Prowlarr Tokyo Toshokan manga registry state")
        assert_true(registry_rows["prowlarr_tokyo_toshokan_manga"]["auto_download_allowed"], "enabled Prowlarr Tokyo Toshokan manga can auto-handoff after strict verdicts")
        assert_false(registry_rows["prowlarr_tokyo_toshokan_manga"]["requires_manual_review"], "enabled Prowlarr Tokyo Toshokan manga does not require manual review")
        assert_equal(registry_rows["prowlarr_tokyo_toshokan_manga"]["policy"]["indexer_ids"], ["45"], "Tokyo Toshokan manga registry targets configured indexer")
        inkdrop_state.update_provider_config(
            db_path,
            "prowlarr_torrentleech_comics",
            {"enabled": True},
        )
        tl_config = inkdrop_state.provider_config(db_path, "prowlarr_torrentleech_comics")
        assert_true(tl_config["enabled"], "runtime Prowlarr TorrentLeech comics can be enabled")
        registry_rows = by_id(registry.registry_from_settings_snapshot(inkdrop_state.settings_snapshot(db_path), include_disabled=True), key="provider_id")
        assert_equal(registry_rows["prowlarr_torrentleech_comics"]["registry_state"], "ready", "enabled TorrentLeech comics registry state")
        assert_true(registry_rows["prowlarr_torrentleech_comics"]["auto_download_allowed"], "enabled TorrentLeech comics can auto-handoff after strict verdicts")
        assert_false(registry_rows["prowlarr_torrentleech_comics"]["requires_manual_review"], "enabled TorrentLeech comics does not require manual review")
        assert_equal(registry_rows["prowlarr_torrentleech_comics"]["policy"]["indexer_ids"], [], "TorrentLeech comics registry has no indexer ID until the user maps one")
        inkdrop_state.update_provider_config(
            db_path,
            "prowlarr_dognzb_comics",
            {"enabled": True},
        )
        dog_config = inkdrop_state.provider_config(db_path, "prowlarr_dognzb_comics")
        assert_true(dog_config["enabled"], "runtime Prowlarr DOGnzb comics can be enabled")
        registry_rows = by_id(registry.registry_from_settings_snapshot(inkdrop_state.settings_snapshot(db_path), include_disabled=True), key="provider_id")
        assert_equal(registry_rows["prowlarr_dognzb_comics"]["registry_state"], "ready", "enabled DOGnzb comics registry state")
        assert_true(registry_rows["prowlarr_dognzb_comics"]["auto_download_allowed"], "enabled DOGnzb comics can auto-handoff after strict verdicts")
        assert_false(registry_rows["prowlarr_dognzb_comics"]["requires_manual_review"], "enabled DOGnzb comics does not require manual review")
        assert_equal(registry_rows["prowlarr_dognzb_comics"]["policy"]["indexer_ids"], [], "DOGnzb comics registry has no indexer ID until the user maps one")
        assert_equal(registry_rows["prowlarr_dognzb_comics"]["policy"]["download_client_by_protocol"], {"usenet": "sabnzbd"}, "DOGnzb comics registry uses SABnzbd for Usenet")
        assert_equal(registry_rows["prowlarr_dognzb_comics"]["policy"]["pack_detail_allowed_hosts"], ["dognzb.cr"], "DOGnzb comics registry keeps sidecar host provider-scoped")
        inkdrop_state.update_provider_config(
            db_path,
            "generic_torrent_rss_feed",
            {"enabled": True, "base_url": "https://torrent.example/feed.xml"},
        )
        torrent_rss_config = inkdrop_state.provider_config(db_path, "generic_torrent_rss_feed")
        assert_true(torrent_rss_config["enabled"], "runtime Generic torrent RSS can be enabled")
        inkdrop_state.update_provider_config(
            db_path,
            "generic_torrent_detail_rss_feed",
            {"enabled": True, "base_url": "https://torrent-detail-rss.example/feed.xml"},
        )
        torrent_detail_rss_config = inkdrop_state.provider_config(db_path, "generic_torrent_detail_rss_feed")
        assert_true(torrent_detail_rss_config["enabled"], "runtime Generic torrent detail RSS can be enabled")
        inkdrop_state.update_provider_config(
            db_path,
            "generic_rss_direct_feed",
            {"enabled": True, "base_url": "https://feeds.example/direct.xml"},
        )
        direct_feed_config = inkdrop_state.provider_config(db_path, "generic_rss_direct_feed")
        assert_true(direct_feed_config["enabled"], "runtime Generic RSS direct feed can be enabled")
        inkdrop_state.update_provider_config(
            db_path,
            "generic_safe_http_direct_download",
            {
                "enabled": True,
                "settings": {
                    "max_bytes": 1048576,
                    "max_redirects": 3,
                    "allowed_content_types": ["application/zip"],
                },
            },
        )
        safe_http_config = inkdrop_state.provider_config(db_path, "generic_safe_http_direct_download")
        assert_true(safe_http_config["enabled"], "runtime Generic safe HTTP direct download can be enabled")
        assert_equal(safe_http_config["settings"]["max_bytes"], 1048576, "runtime Generic safe HTTP max byte setting persists")
        inkdrop_state.update_provider_config(
            db_path,
            "local_manual_inbox",
            {
                "enabled": True,
                "settings": {
                    "manual_inbox_root": "/tmp/inkdrop-manual-inbox",
                    "manual_inbox_staging_root": "/tmp/inkdrop-manual-staging",
                },
            },
        )
        manual_inbox_config = inkdrop_state.provider_config(db_path, "local_manual_inbox")
        assert_true(manual_inbox_config["enabled"], "runtime Local/manual inbox can be enabled")
        assert_equal(
            manual_inbox_config["settings"]["manual_inbox_root"],
            "/tmp/inkdrop-manual-inbox",
            "runtime Local/manual inbox root persists",
        )
        inkdrop_state.update_provider_config(
            db_path,
            "generic_opds_catalog",
            {"enabled": True, "base_url": "https://opds.example/catalog.xml"},
        )
        opds_config = inkdrop_state.provider_config(db_path, "generic_opds_catalog")
        assert_true(opds_config["enabled"], "runtime Generic OPDS catalog can be enabled")

    ok("runtime settings include catalog source templates without enabling them")


if __name__ == "__main__":
    main()
