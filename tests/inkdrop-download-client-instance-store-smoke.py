#!/usr/bin/env python3
"""Focused contract smoke for durable download-client instances."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
from pathlib import Path

from core import inkdrop_download_client_config as config
from core import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def expect_error(fn, text):
    try:
        fn()
    except ValueError as exc:
        require(text.lower() in str(exc).lower(), f"expected {text!r}, got {exc!r}")
        return
    raise AssertionError(f"expected ValueError containing {text!r}")


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-download-client-store-") as tmp:
        root = Path(tmp)
        db = root / "state.sqlite3"
        secret_root = root / "secrets"
        with inkdrop_state.connect(db) as con:
            inkdrop_state.init_schema(con)
            inkdrop_state.init_schema(con)
            tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
            require("download_client_instances" in tables, "instance table is additive and idempotent")
            require("download_client_provider_mappings" in tables, "provider mapping table exists")
            require("download_client_instance_migrations" in tables, "migration scaffold exists")
            columns = {row[1] for row in con.execute("pragma table_info(download_tasks)")}
            require("download_client_instance_id" in columns, "download tasks gain an additive instance reference")
            require(con.execute("select value from schema_meta where key='schema_version'").fetchone()[0] == str(inkdrop_state.SCHEMA_VERSION), "current schema version is recorded")

        first = inkdrop_state.create_download_client_instance(
            db,
            {
                "id": "home-qbit",
                "name": "Home qBit",
                "client_type": "qbittorrent",
                "enabled": True,
                "priority": 20,
                "base_url": "http://qbittorrent:8080",
                "username": "inkdrop",
                "category": "comics",
                "categories": {"comics": "comics", "manga": "manga"},
                "download_paths": {"comics": "/downloads/comics", "manga": "/downloads/manga"},
                "path_mappings": [{"remote_path": "/downloads", "local_path": "/staging/downloads"}],
                "settings": {"verify_tls": True, "cleanup_policy": "external"},
                "secrets": {"password": "super-secret-qbit-password"},
                "provider_mappings": [{"provider_id": "prowlarr", "protocol": "torrent", "media_type": "comics"}],
            },
            secret_root=secret_root,
        )
        require(first["id"] == "home-qbit", "instance id is immutable and stable")
        require(first["revision"] == 1, "new instance starts at revision 1")
        require(first["secret_fields"] == {"password": {"configured": True}}, "response exposes only secret presence")
        require("super-secret" not in json.dumps(first), "response does not echo secret")

        raw_db = db.read_bytes()
        require(b"super-secret-qbit-password" not in raw_db, "reversible secret is not stored in SQLite")
        with contextlib.closing(sqlite3.connect(db)) as con:
            history = "\n".join(str(row[0] or "") for row in con.execute("select raw_json from history_events"))
        require("super-secret-qbit-password" not in history, "history is redacted")
        require("password:secret" in history, "history records only secret field metadata")

        expect_error(
            lambda: inkdrop_state.create_download_client_instance(
                db,
                {"name": "  HOME QBIT  ", "client_type": "qbittorrent"},
                secret_root=secret_root,
            ),
            "unique",
        )
        expect_error(
            lambda: inkdrop_state.create_download_client_instance(
                db,
                {
                    "name": "Duplicate endpoint",
                    "client_type": "qbittorrent",
                    "base_url": "http://qbittorrent:8080/",
                    "username": "INKDROP",
                },
                secret_root=secret_root,
            ),
            "endpoint",
        )
        expect_error(
            lambda: inkdrop_state.create_download_client_instance(
                db,
                {"name": "Unsafe URL", "client_type": "transmission", "base_url": "http://user:pass@host:9091"},
                secret_root=secret_root,
            ),
            "embedded credentials",
        )
        expect_error(
            lambda: inkdrop_state.create_download_client_instance(
                db,
                {"name": "Unsafe Query", "client_type": "transmission", "base_url": "http://host:9091/rpc?token=secret"},
                secret_root=secret_root,
            ),
            "query",
        )
        expect_error(
            lambda: inkdrop_state.create_download_client_instance(
                db,
                {"name": "Secret in settings", "client_type": "transmission", "settings": {"api_token": "bad"}},
                secret_root=secret_root,
            ),
            "secrets object",
        )
        expect_error(
            lambda: inkdrop_state.create_download_client_instance(
                db,
                {
                    "name": "Too many mappings",
                    "client_type": "transmission",
                    "path_mappings": [{"remote_path": f"/r/{index}", "local_path": f"/l/{index}"} for index in range(33)],
                },
                secret_root=secret_root,
            ),
            "cannot exceed",
        )

        draft = inkdrop_state.create_download_client_instance(
            db,
            {"name": "Future client", "client_type": "future-client", "enabled": False},
            secret_root=secret_root,
        )
        require(not draft["enabled"], "unknown client types may be saved incomplete while disabled")
        expect_error(
            lambda: inkdrop_state.update_download_client_instance(
                db,
                draft["id"],
                {"enabled": True},
                expected_revision=draft["revision"],
                secret_root=secret_root,
            ),
            "registry-ready",
        )

        expect_error(
            lambda: inkdrop_state.update_download_client_instance(
                db,
                first["id"],
                {"priority": 30},
                expected_revision=999,
                secret_root=secret_root,
            ),
            "revision conflict",
        )
        updated = inkdrop_state.update_download_client_instance(
            db,
            first["id"],
            {"priority": 10, "secrets": {"password": ""}},
            expected_revision=first["revision"],
            secret_root=secret_root,
        )
        require(updated["revision"] == 2 and updated["priority"] == 10, "optimistic update increments revision")
        require(updated["secret_fields"]["password"]["configured"], "blank secret preserves configured value")
        expect_error(
            lambda: inkdrop_state.update_download_client_instance(
                db,
                first["id"],
                {"id": "changed-id"},
                expected_revision=updated["revision"],
                secret_root=secret_root,
            ),
            "immutable",
        )

        expect_error(
            lambda: inkdrop_state.delete_download_client_instance(db, first["id"], expected_revision=updated["revision"]),
            "provider mappings",
        )
        unmapped = inkdrop_state.update_download_client_instance(
            db,
            first["id"],
            {"provider_mappings": []},
            expected_revision=updated["revision"],
            secret_root=secret_root,
        )
        with inkdrop_state.connect(db) as con:
            con.execute(
                "insert into download_tasks(id,state,download_client_instance_id) values(?,?,?)",
                ("active-download", "downloading", first["id"]),
            )
            con.commit()
        expect_error(
            lambda: inkdrop_state.delete_download_client_instance(db, first["id"], expected_revision=unmapped["revision"]),
            "active download tasks",
        )
        with inkdrop_state.connect(db) as con:
            con.execute("update download_tasks set state='completed' where id='active-download'")
            con.commit()
        deleted = inkdrop_state.delete_download_client_instance(
            db,
            first["id"],
            expected_revision=unmapped["revision"],
            secret_root=secret_root,
        )
        require(deleted["deleted_at"] and not deleted["enabled"], "delete is a reversible soft delete")
        require(deleted["secret_fields"] == {}, "soft delete clears secret references")
        require(not list(secret_root.glob("*.secret")), "soft delete removes committed secret files")
        require(inkdrop_state.download_client_instance(db, first["id"]) is None, "deleted instance is hidden by default")
        require(inkdrop_state.download_client_instance(db, first["id"], include_deleted=True)["deleted_at"], "deleted instance remains auditable")

        inkdrop_state.sync_settings(
            db,
            providers=[{
                "id": "sabnzbd",
                "provider_type": "download_client",
                "display_name": "Legacy SAB",
                "enabled": True,
                "base_url": "http://sab:8080",
                "settings": {"api_key": "legacy-secret", "secret_fields": ["api_key"], "editable_fields": ["api_key"]},
                "source": "user",
            }],
            settings=[],
        )
        before = db.stat().st_mtime_ns
        legacy = inkdrop_state.download_client_legacy_migration_metadata(db)
        after = db.stat().st_mtime_ns
        require(legacy["writes_performed"] is False and before == after, "legacy synthesis is read-only")
        require(legacy["legacy_instances"][0]["secret_fields"]["api_key"]["configured"], "legacy metadata reports secret presence")
        require("legacy-secret" not in json.dumps(legacy), "legacy metadata never echoes credentials")

        print(json.dumps({
            "ok": True,
            "schema": config.CONTRACT_SCHEMA,
            "schema_version": inkdrop_state.SCHEMA_VERSION,
            "created_instance": first["id"],
            "soft_deleted": deleted["id"],
            "legacy_candidates": len(legacy["legacy_instances"]),
        }, sort_keys=True))


if __name__ == "__main__":
    main()
