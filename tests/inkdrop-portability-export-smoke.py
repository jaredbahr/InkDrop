#!/usr/bin/env python3
"""Prove the portability export carries series and providers but never secrets."""

import json
import tempfile
import time
from pathlib import Path

import inkdrop_backup_restore
import inkdrop_portability_export
import inkdrop_state


SECRET_VALUE = "prowlarr-key-1f2e3d4c5b6a"
DECLARED_SECRET_VALUE = "https://discord.com/api/webhooks/123/leaky-webhook-token"
CLIENT_PASSWORD = "qbit-password-9z8y7x"
CLIENT_USERNAME = "qbit-operator"


def require(value, message):
    if not value:
        raise AssertionError(message)


def seed_database(db_path):
    inkdrop_state.ensure_schema(db_path)
    now = time.time()
    with inkdrop_state.connect(db_path) as con:
        con.execute(
            "insert into series(id, title, sort_title, media_type, year, publisher,"
            " metadata_provider, metadata_id, source, library_root, library_path,"
            " monitored, monitor_new, auto_grab, created_at, updated_at, raw_json)"
            " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "mangadex:aa11", "Frieren: Beyond Journey's End", "Frieren", "manga", 2020,
                "Shogakukan", "mangadex", "aa11", "mangadex",
                "/library/manga", "/library/manga/Frieren Beyond Journeys End",
                1, 1, 1, now, now,
                json.dumps({"manga_unit_policy": "volume_only"}),
            ),
        )
        con.execute(
            "insert into series(id, title, sort_title, media_type, year, publisher,"
            " metadata_provider, metadata_id, source, library_root, library_path,"
            " monitored, monitor_new, auto_grab, created_at, updated_at, raw_json)"
            " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "comicvine:4050-1234", "Saga", "Saga", "comic", 2012,
                "Image", "comicvine", "4050-1234", "comicvine",
                "/library/comics", "/library/comics/Saga (2012)",
                0, 0, 0, now, now, "{}",
            ),
        )
        con.execute(
            "insert into series(id, title, sort_title, media_type, year, publisher,"
            " metadata_provider, metadata_id, source, library_root, library_path,"
            " monitored, monitor_new, auto_grab, created_at, updated_at, raw_json)"
            " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "comicvine:4050-9999", "Replaced Relic", "Replaced Relic", "comic", 2010,
                "Image", "comicvine", "4050-9999", "replaced_metadata",
                "/library/comics", "/library/comics/Replaced Relic",
                0, 0, 0, now, now, "{}",
            ),
        )
        con.execute(
            "insert into series(id, title, sort_title, media_type, year, publisher,"
            " metadata_provider, metadata_id, source, library_root, library_path,"
            " monitored, monitor_new, auto_grab, created_at, updated_at, raw_json)"
            " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "comicvine:4050-8888", "Parked Phantom", "Parked Phantom", "comic", 2015,
                "Image", "comicvine", "4050-8888", "comicvine",
                "/library/comics", "/library/comics/Parked Phantom",
                0, 0, 0, now, now,
                json.dumps({"automation_parked_reason": "series_shadow_retired"}),
            ),
        )
        con.execute(
            "insert into provider_configs(id, provider_type, display_name, enabled,"
            " base_url, secret_ref, settings_group, automation_role, settings_json,"
            " created_at, updated_at) values(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "prowlarr", "indexer_manager", "Prowlarr", 1,
                "http://prowlarr:9696", "prowlarr_api_key", "acquisition", "search",
                json.dumps({
                    "api_key": SECRET_VALUE,
                    "secret_ref": "prowlarr_api_key",
                    "timeout_seconds": 45,
                    "nested": {"password": "should-not-survive"},
                }),
                now, now,
            ),
        )
        con.execute(
            "insert into provider_configs(id, provider_type, display_name, enabled,"
            " base_url, secret_ref, settings_group, automation_role, settings_json,"
            " created_at, updated_at) values(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "notifications", "notification_channel", "Notifications", 1,
                "", "", "notifications", "alerts",
                json.dumps({
                    "discord_webhook_url": DECLARED_SECRET_VALUE,
                    "discord_enabled": True,
                    "secret_fields": ["discord_webhook_url"],
                }),
                now, now,
            ),
        )
        con.commit()
    inkdrop_state.create_download_client_instance(
        db_path,
        {
            "name": "Main qBittorrent",
            "client_type": "qbittorrent",
            "enabled": True,
            "base_url": "http://qbit:8080",
            "username": CLIENT_USERNAME,
            "category": "inkdrop",
            "secrets": {"password": CLIENT_PASSWORD},
        },
        secret_root=Path(db_path).parent / "secrets",
    )
    inkdrop_state.sync_settings(
        db_path,
        settings=[
            {"key": "ui.relative_dates", "scope": "ui", "value": True, "source": "user"},
            {"key": "path.manga_root", "scope": "paths", "value": "/library/manga", "source": "user"},
        ],
    )


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-portability-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_database(db_path)

        document = inkdrop_portability_export.export_portability_document(
            db_path, now=1750000000.0, version="smoke", secret_root=Path(db_path).parent / "secrets"
        )

        require(document["schema"] == "inkdrop.portability_export.v1", "schema name is stable")
        require(document["checksum"] == inkdrop_portability_export.document_checksum(document), "checksum verifies")
        require(document["summary"]["series_total"] == 2, f"expected 2 series, got {document['summary']}")
        require(document["summary"]["series_monitored"] == 1, "exactly one monitored series")
        require(document["summary"]["series_by_media_type"] == {"manga": 1, "comic": 1}, "media type counts")
        require(document["summary"]["series_excluded_parked_or_replaced"] == 2, "parked/replaced series are counted, not listed")
        require("comicvine:4050-9999" not in {item["id"] for item in document["series"]}, "replaced_metadata tombstone leaked into the series list")
        require("comicvine:4050-8888" not in {item["id"] for item in document["series"]}, "automation-parked series leaked into the series list")

        by_id = {item["id"]: item for item in document["series"]}
        frieren = by_id["mangadex:aa11"]
        require(frieren["monitored"] is True and frieren["auto_grab"] is True, "monitored flags survive")
        require(frieren["manga_unit_policy"] == "volume_only", f"manga unit policy exported: {frieren}")
        require(frieren["library_folder"] == "Frieren Beyond Journeys End", "folder name only, no path")
        require("library_path" not in frieren, "full library path is not exported")
        saga = by_id["comicvine:4050-1234"]
        require(saga["monitored"] is False, "unmonitored series keeps its flag")
        require("manga_unit_policy" not in saga, "comics carry no manga policy")

        providers_by_id = {item["id"]: item for item in document["providers"]}
        provider = providers_by_id["prowlarr"]
        require(provider["base_url"] == "http://prowlarr:9696", "provider basics")
        require(provider["settings"].get("timeout_seconds") == 45, "plain settings survive")
        require(provider["settings"].get("secret_ref") == "prowlarr_api_key", "secret reference name survives")
        require("api_key" not in provider["settings"], "credential value is dropped")
        require("nested" not in provider["settings"], "nested settings are dropped, not guessed at")
        require(sorted(provider["credential_fields_removed"]) == ["api_key", "nested"], "dropped fields are named")

        notifications = providers_by_id["notifications"]
        require("discord_webhook_url" not in notifications["settings"], "declared secret field is dropped even without a credential-looking name")
        require("discord_webhook_url" in notifications["credential_fields_removed"], "declared secret drop is named")
        require(notifications["settings"].get("discord_enabled") is True, "plain notification setting survives")

        require(document["summary"]["download_clients_total"] == 1, document["summary"])
        client = document["download_clients"][0]
        require(client["client_type"] == "qbittorrent" and client["base_url"] == "http://qbit:8080", "client basics survive")
        require(client["secrets_configured"] == ["password", "username"], client["secrets_configured"])
        require("username" not in client, "client username value is not exported")

        settings_doc = document["settings"]
        require(settings_doc["schema"] == "inkdrop.portable_settings.v1", "reuses the portable settings document")
        require(settings_doc["settings"].get("ui.relative_dates") is True, "portable setting exported")
        require("path.manga_root" not in settings_doc["settings"], "host-specific setting stays excluded")
        preview = inkdrop_backup_restore.restore_portable_settings(db_path, json.dumps(settings_doc), apply=False)
        require(preview["ok"] and preview["dry_run"], "settings block is not accepted by the existing restore path")

        rendered = json.dumps(document, ensure_ascii=False)
        require(SECRET_VALUE not in rendered, "secret value never appears anywhere in the document")
        require("should-not-survive" not in rendered, "nested credential never appears in the document")
        require(DECLARED_SECRET_VALUE not in rendered, "declared-secret webhook URL never appears in the document")
        require(CLIENT_PASSWORD not in rendered, "download-client password never appears in the document")
        require(CLIENT_USERNAME not in rendered, "download-client username never appears in the document")

        again = inkdrop_portability_export.export_portability_document(
            db_path, now=1750000000.0, version="smoke", secret_root=Path(db_path).parent / "secrets"
        )
        require(json.dumps(again, sort_keys=True) == json.dumps(document, sort_keys=True), "export is deterministic")

        filename = inkdrop_portability_export.portability_export_filename(document)
        require(filename.startswith("inkdrop-portability-") and filename.endswith(".json"), f"filename shape: {filename}")

    print("portability export smoke: ok")


if __name__ == "__main__":
    main()
