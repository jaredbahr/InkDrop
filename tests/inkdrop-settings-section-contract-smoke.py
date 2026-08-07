#!/usr/bin/env python3
"""Smoke test that public settings sections expose renderable backend payloads."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

sys.modules.setdefault("requests", types.ModuleType("requests"))

from core import inkdrop_state
from core import inkdrop_web as web


SETTINGS_SECTIONS = {
    "setup": {"min_items": 1, "setting_prefixes": ("setup.",)},
    "media_management": {"min_items": 1, "provider_ids": {"media_management"}},
    "language": {"min_items": 1, "provider_ids": {"quality_language_rules"}},
    "indexers": {"min_items": 1, "provider_ids": {"prowlarr"}},
    "download_clients": {"min_items": 1, "provider_ids": {"sabnzbd", "qbittorrent", "slskd", "comicscodes"}},
    "import_lists": {"min_items": 1, "setting_prefixes": ("import_lists.",)},
    "connect": {"min_items": 1, "provider_ids": {"kavita", "komga"}},
    "metadata_files": {"min_items": 1, "setting_prefixes": ("metadata_files.",)},
    "metadata": {"min_items": 1, "provider_ids": {"comicvine", "mangadex"}},
    "general": {"min_items": 1},
    "ui": {"min_items": 1, "setting_prefixes": ("ui.",)},
    "root_folders": {"min_items": 1, "provider_ids": {"library_paths", "manual_inboxes"}},
    "automation": {"min_items": 1, "setting_prefixes": ("automation.",)},
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def payload_items(payload):
    providers = payload.get("providers") if isinstance(payload.get("providers"), list) else []
    settings = payload.get("settings") if isinstance(payload.get("settings"), list) else []
    return providers, settings


def main():
    require(
        "...(setting.field_schema || {})" in web.HTML
        and "min: setting.minimum" in web.HTML
        and "max: setting.maximum" in web.HTML,
        "app setting controls must receive their backend field schema and numeric bounds",
    )
    require("INKDROP_HOST_PORT" in web.HTML, "General settings should explain the supported host-port setting")
    require("--force-recreate inkdrop inkdrop-worker" in web.HTML, "General settings should explain that listener changes require service recreation")
    require("This runtime-managed value cannot rebind a running process" in web.HTML, "General settings must not imply a live settings save can rebind the listener")
    require("/api/inkdrop-settings/backup/export" in web.HTML, "General settings should expose portable export")
    require("/api/inkdrop-settings/backup/preview" in web.HTML, "General settings should preview before restore")
    require("/api/inkdrop-settings/backup/restore" in web.HTML, "General settings should expose confirmed merge restore")
    require("window.InkDropDownloadClients?.mount?.(providerTarget)" in web.HTML, "Download Clients settings should mount the instance manager")
    require("inkdrop-download-clients-ui.js" in web.HTML, "Download Clients settings asset should be loaded")
    require("Credentials, private paths, media, history, users, sessions, and active work are never included" in web.HTML, "Settings backup scope should be explicit")
    require("Send Anonymous Usage Data" not in web.HTML, "Settings must not expose an anonymous usage control")
    require("privacy.analytics" not in web.HTML, "Settings must not expose the dead analytics placeholder key")
    require("logging and analytics" not in web.HTML, "General settings wording must not advertise analytics")
    with tempfile.TemporaryDirectory(prefix="inkdrop-settings-section-contract-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / inkdrop_state.STATE_DB_NAME
        inkdrop_state.ensure_schema(db_path)
        old_db = web.INKDROP_STATE_DB
        web.INKDROP_STATE_DB = db_path
        try:
            for area, contract in SETTINGS_SECTIONS.items():
                payload = web.inkdrop_settings_public(sync=False, area=area)
                encoded = json.dumps(payload, sort_keys=True)
                require(payload.get("ok") is True, f"{area} settings payload should be ok")
                require(payload.get("settings_area_filtered") is True, f"{area} settings payload should be area-filtered")
                listener = (payload.get("deployment") or {}).get("listener") or {}
                require(1 <= int(listener.get("host_port") or 0) <= 65535, f"{area} should report an effective host port")
                require(1 <= int(listener.get("container_port") or 0) <= 65535, f"{area} should report an effective container port")
                require(listener.get("restart_required") is True and listener.get("recreate_required") is True, f"{area} should report listener restart/recreate semantics")
                providers, settings = payload_items(payload)
                require(
                    all(str(row.get("key") or "") != "privacy.analytics" for row in settings),
                    f"{area} must not publish a privacy.analytics setting",
                )
                require(len(providers) + len(settings) >= contract["min_items"], f"{area} should expose renderable settings data")
                require("literal-user-secret" not in encoded, f"{area} payload should not expose test secret marker")
                provider_ids = {str(row.get("id") or "").strip().lower() for row in providers}
                expected_provider_ids = contract.get("provider_ids") or set()
                require(expected_provider_ids.issubset(provider_ids), f"{area} missing expected provider ids: {sorted(expected_provider_ids - provider_ids)}")
                prefixes = contract.get("setting_prefixes") or ()
                if prefixes:
                    keys = [str(row.get("key") or "") for row in settings]
                    require(any(key.startswith(prefixes) for key in keys), f"{area} missing setting with prefixes {prefixes}")
            updated = web.update_inkdrop_app_setting(
                {"key": "automation.queue_watchdog_slskd_stale_minutes", "value": 60}
            )
            updated_rows = {row["key"]: row for row in updated.get("settings") or []}
            require(
                updated_rows["automation.queue_watchdog_slskd_stale_minutes"]["value"] == 60,
                "saving a runtime-only app setting should claim and persist it",
            )

            # RSS and ComicsCodes are download sources, reachable by normal
            # click-through under Download Clients now -- not stuck behind
            # the "automation" nav entry, which stays hidden.
            dc_payload = web.inkdrop_settings_public(sync=False, area="download_clients")
            dc_providers, _ = payload_items(dc_payload)
            dc_ids = {str(row.get("id") or "").strip().lower() for row in dc_providers}
            require(
                "comicscodes" in dc_ids and ("rss" in dc_ids or "rss_direct" in dc_ids),
                f"RSS and ComicsCodes should be reachable from Download Clients, got {sorted(dc_ids)}",
            )

            # The Settings-UI toggle sends whatever id the payload showed it
            # (canonicalize_settings_providers() may have rewritten "rss" to
            # "rss_direct" for the cross-provider status rollup) -- saving
            # must resolve back to the row actually stored in provider_configs
            # rather than failing "provider config not found" or silently
            # creating an orphaned duplicate row under the display id.
            save_result = web.update_inkdrop_provider_settings({"id": "rss_direct", "enabled": False})
            require(save_result.get("ok") is not False, f"saving RSS via its canonicalized id should succeed: {save_result}")
            with sqlite3.connect(db_path) as con:
                con.row_factory = sqlite3.Row
                rss_rows = con.execute("select id, enabled, source from provider_configs where id like '%rss%'").fetchall()
            require(len(rss_rows) == 1, f"saving RSS via its canonicalized id must update the one real row, not create a duplicate: {[dict(r) for r in rss_rows]}")
            require(rss_rows[0]["id"] == "rss", f"the real stored row must stay keyed by its native id, got {rss_rows[0]['id']!r}")
            require(rss_rows[0]["enabled"] == 0, "the save must actually persist the new enabled value")

            # Regression: saving ANY one native provider used to silently
            # drop every OTHER not-yet-individually-saved native provider
            # from every subsequent settings response (merge_runtime_settings_
            # snapshot only restored missing source_catalog templates, not
            # missing native "runtime" providers) -- reproduced live: saving
            # qBittorrent alone made SLSKD/SABnzbd disappear, even after a
            # full page reload. Confirm they all still show up after two
            # separate saves in this same area.
            qbit_save = web.update_inkdrop_provider_settings({"id": "qbittorrent", "enabled": True})
            require(qbit_save.get("ok") is not False, f"saving qBittorrent should succeed: {qbit_save}")
            dc_payload_after = web.inkdrop_settings_public(sync=False, area="download_clients")
            dc_ids_after = {str(row.get("id") or "").strip().lower() for row in payload_items(dc_payload_after)[0]}
            require(
                {"comicscodes", "slskd", "sabnzbd", "qbittorrent"}.issubset(dc_ids_after)
                and ("rss" in dc_ids_after or "rss_direct" in dc_ids_after),
                f"saving one provider must not hide its untouched siblings, got {sorted(dc_ids_after)}",
            )
        finally:
            web.INKDROP_STATE_DB = old_db

    print(json.dumps({"ok": True, "settings_section_contract_smoke": "passed"}, indent=2))


if __name__ == "__main__":
    main()
