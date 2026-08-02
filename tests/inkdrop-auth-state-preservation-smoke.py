#!/usr/bin/env python3
"""Prove the Build 56 auth-policy repair is read-only for existing state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
from pathlib import Path

import inkdrop_auth
import inkdrop_settings_registry
import inkdrop_state


def logical_digest(db):
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        tables = [row[0] for row in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
        payload = {}
        for table in tables:
            rows = [dict(row) for row in con.execute(f'select * from "{table}"')]
            payload[table] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-auth-preservation-", ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "state.sqlite3"
        inkdrop_state.ensure_schema(db)
        now = time.time()
        with inkdrop_state.connect(db) as con:
            for provider_id, url, settings in (
                ("qbittorrent", "http://qbittorrent:8080", {"path_mappings": [{"remote": "/downloads", "local": "/data"}]}),
                ("sabnzbd", "http://sabnzbd:8080", {"category": "comics"}),
                ("slskd", "http://slskd:5030", {"download_root": "/downloads/slskd"}),
                ("prowlarr", "http://prowlarr:9696", {"indexer_ids": [1, 2]}),
                ("comicvine", "https://comicvine.gamespot.com/api", {"secret_ref": "env:COMICVINE_API_KEY"}),
                ("kavita", "http://kavita:5000", {}),
                ("komga", "http://komga:25600", {}),
                ("kapowarr", "http://kapowarr:5656", {"compatibility_only": True}),
            ):
                inkdrop_state.upsert_provider_config(con, {"id": provider_id, "source": "user", "base_url": url, "settings": settings}, now)
            for key, value in (
                ("media_management.comics_root", "/library/comics"),
                ("media_management.manga_root", "/library/manga"),
                ("automation.source_order", ["prowlarr", "slskd", "comicscodes"]),
                ("auth.mode", "built_in"),
            ):
                inkdrop_state.upsert_app_setting(con, {"key": key, "scope": "general", "source": "user", "value": value}, now)
            con.commit()

        inkdrop_auth.bootstrap_admin(db, "existing-admin", "existing build 56 password")
        login = inkdrop_auth.login(db, "existing-admin", "existing build 56 password", remote_addr="127.0.0.1")
        inkdrop_auth.create_api_key(db, "QA acquisition", scopes=["read", "acquisition"])
        before = logical_digest(db)

        policy = inkdrop_auth.password_policy(db, environ={})
        status = inkdrop_auth.public_status(db, environ={"INKDROP_AUTH_MODE": "built_in"})
        inkdrop_settings_registry.validate_value("auth.password_min_length", policy["minimum_length"])

        after = logical_digest(db)
        if before != after:
            raise AssertionError("auth policy/status reads modified Build 56 state")
        if not inkdrop_auth.verify_password("existing build 56 password", sqlite3.connect(inkdrop_auth.auth_store_path(db)).execute("select password_hash from auth_users where username='existing-admin'").fetchone()[0]):
            raise AssertionError("existing administrator hash no longer verifies")
        if not inkdrop_auth.verify_session(db, login["session"]["token"]):
            raise AssertionError("existing session no longer verifies")
        if status["password_policy"]["minimum_length"] != 8:
            raise AssertionError("default policy was not exposed")

    print(json.dumps({"ok": True, "build56_state_preservation": "passed"}, indent=2))


if __name__ == "__main__":
    main()
