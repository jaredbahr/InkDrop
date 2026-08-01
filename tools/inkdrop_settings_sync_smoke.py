#!/usr/bin/env python3
"""Smoke-test InkDrop provider/app settings sync on a temporary DB."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import inkdrop_state


def load_json(value, fallback):
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def row_dict(row):
    return dict(row) if row is not None else {}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="inkdrop-settings-sync-smoke-") as tmp:
        db_path = Path(tmp) / inkdrop_state.STATE_DB_NAME
        inkdrop_state.ensure_schema(db_path)
        with inkdrop_state.connect(db_path) as con:
            now = 1.0
            inkdrop_state.upsert_provider_config(
                con,
                {
                    "id": "comicvine",
                    "provider_type": "metadata",
                    "display_name": "ComicVine User",
                    "enabled": True,
                    "base_url": "https://user.example.invalid/comicvine",
                    "secret_ref": "user:comicvine-token",
                    "settings_group": "metadata",
                    "ownership": "user",
                    "automation_role": "User metadata provider",
                    "description": "User controlled ComicVine config.",
                    "next_action": "Keep user settings.",
                    "capabilities": ["metadata"],
                    "settings": {
                        "api_key": "literal-user-secret",
                        "api_key_env": "INKDROP_COMICVINE_API_KEY",
                        "policy": {"language": "en", "pack_mode": "strict"},
                        "secret_fields": ["api_key"],
                        "editable_fields": ["language", "pack_mode"],
                    },
                    "source": "user",
                },
                now,
            )
            inkdrop_state.upsert_app_setting(
                con,
                {
                    "key": "media_management.rename_imported_files",
                    "scope": "media_management",
                    "label": "User rename policy",
                    "value": False,
                    "description": "User controlled value.",
                    "source": "user",
                },
                now,
            )
            con.commit()

        result = inkdrop_state.sync_settings(
            db_path,
            providers=[
                {
                    "id": "comicvine",
                    "provider_type": "metadata",
                    "display_name": "ComicVine Runtime",
                    "enabled": False,
                    "base_url": "",
                    "secret_ref": "env:INKDROP_COMICVINE_API_KEY",
                    "settings_group": "metadata",
                    "ownership": "native",
                    "automation_role": "Primary metadata source",
                    "description": "Runtime metadata template.",
                    "next_action": "Configure the API key before enabling.",
                    "capabilities": ["metadata", "series_search", "issue_lookup"],
                    "applied_by": ["Add Series", "InkDrop native metadata sync"],
                    "settings": {
                        "api_key": "",
                        "api_key_env": "INKDROP_COMICVINE_API_KEY",
                        "policy": {"pack_mode": "loose", "decimal_chapters": "safe"},
                        "secret_fields": ["api_key"],
                        "editable_fields": ["language", "pack_mode", "decimal_chapters"],
                    },
                    "source": "runtime",
                },
                {
                    "id": "slskd",
                    "provider_type": "download_source",
                    "display_name": "SLSKD",
                    "enabled": False,
                    "base_url": "",
                    "secret_ref": "",
                    "settings_group": "download_sources",
                    "ownership": "native",
                    "automation_role": "Soulseek downloader",
                    "capabilities": ["search", "grab", "poll", "staged_import"],
                    "settings": {"download_root_env": "INKDROP_SLSKD_DOWNLOAD_ROOT"},
                    "source": "runtime",
                },
            ],
            settings=[
                {
                    "key": "media_management.rename_imported_files",
                    "scope": "media_management",
                    "label": "Rename imported files",
                    "value": True,
                    "description": "Runtime default should not overwrite user value.",
                    "source": "runtime",
                },
                {
                    "key": "sources.protocol_order",
                    "scope": "sources",
                    "label": "Protocol order",
                    "value": ["usenet", "torrent", "direct"],
                    "description": "Default source protocol order.",
                    "source": "runtime",
                },
            ],
        )
        require(result.get("ok"), "sync_settings should succeed")

        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            comicvine = row_dict(con.execute("select * from provider_configs where id='comicvine'").fetchone())
            slskd = row_dict(con.execute("select * from provider_configs where id='slskd'").fetchone())
            rename = row_dict(con.execute("select * from app_settings where key='media_management.rename_imported_files'").fetchone())
            protocol_order = row_dict(con.execute("select * from app_settings where key='sources.protocol_order'").fetchone())
        finally:
            con.close()

    require(comicvine.get("display_name") == "ComicVine Runtime", "runtime metadata should refresh display name")
    require(comicvine.get("base_url") == "https://user.example.invalid/comicvine", "user-owned provider base_url should be preserved")
    require(comicvine.get("secret_ref") == "user:comicvine-token", "user-owned provider secret_ref should be preserved")
    require(comicvine.get("source") == "user", "user-owned provider source should be preserved")
    require(comicvine.get("ownership") == "native", "runtime ownership metadata should refresh")
    require(comicvine.get("automation_role") == "Primary metadata source", "runtime automation role should refresh")
    require("series_search" in load_json(comicvine.get("capabilities_json"), []), "runtime capabilities should refresh")
    comicvine_settings = load_json(comicvine.get("settings_json"), {})
    require(comicvine_settings.get("api_key") == "literal-user-secret", "blank runtime secret should not erase user secret")
    require(comicvine_settings.get("api_key_env") == "INKDROP_COMICVINE_API_KEY", "env secret pointer should remain explicit")
    require(comicvine_settings.get("policy", {}).get("pack_mode") == "strict", "user policy keys should not be overwritten by runtime defaults")
    require(comicvine_settings.get("policy", {}).get("language") == "en", "user policy keys not supplied by runtime should be preserved")
    require(comicvine_settings.get("policy", {}).get("decimal_chapters") == "safe", "new runtime policy keys should be merged")

    require(slskd.get("base_url") in ("", None), "new runtime provider should keep blank endpoint default")
    require(slskd.get("ownership") == "native", "new runtime provider should record native ownership")
    require("staged_import" in load_json(slskd.get("capabilities_json"), []), "new runtime provider should store capabilities")

    require(rename.get("source") == "user", "user app setting source should be preserved")
    require(load_json(rename.get("value_json"), None) is False, "user app setting value should not be overwritten by runtime default")
    require(rename.get("label") == "Rename imported files", "runtime label should refresh user app setting")
    require(protocol_order.get("source") == "runtime", "new runtime app setting should be inserted")
    require(load_json(protocol_order.get("value_json"), []) == ["usenet", "torrent", "direct"], "runtime app setting value should be stored")

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
