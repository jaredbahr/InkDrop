#!/usr/bin/env python3
"""Smoke test that public settings sections expose renderable backend payloads."""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

sys.modules.setdefault("requests", types.ModuleType("requests"))

import inkdrop_state
import inkdrop_web as web


SETTINGS_SECTIONS = {
    "setup": {"min_items": 1, "setting_prefixes": ("setup.",)},
    "media_management": {"min_items": 1, "provider_ids": {"media_management"}},
    "language": {"min_items": 1, "provider_ids": {"quality_language_rules"}},
    "indexers": {"min_items": 1, "provider_ids": {"prowlarr"}},
    "download_clients": {"min_items": 1, "provider_ids": {"sabnzbd", "qbittorrent", "slskd"}},
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
        finally:
            web.INKDROP_STATE_DB = old_db

    print(json.dumps({"ok": True, "settings_section_contract_smoke": "passed"}, indent=2))


if __name__ == "__main__":
    main()
