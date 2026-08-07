#!/usr/bin/env python3
"""Schema-13 through current-schema preservation regression for Manual Search."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from core import inkdrop_state


ADDITIVE_TABLES = (
    "manual_search_handoff_capsules",
    "manual_search_grab_results",
    "manual_search_candidate_decisions",
    "manual_search_candidates",
    "manual_search_provider_attempts",
    "manual_search_queries",
    "manual_search_runs",
    "series_source_profile_overrides",
    "source_profiles",
    "identity_conflicts",
    "identity_external_ids",
    "identity_artifacts",
    "identity_units",
    "identity_editions",
    "identity_works",
)

PRESERVED_TABLES = (
    "app_settings",
    "provider_configs",
    "auth_users",
    "auth_sessions",
    "api_keys",
    "series",
    "issues",
    "wanted_items",
    "queue_items",
    "source_attempts",
    "download_tasks",
    "import_results",
    "history_events",
)


def require(value, message):
    if not value:
        raise AssertionError(message)


def table_digest(con, table):
    rows = [dict(row) for row in con.execute(f"select * from {table} order by 1")]
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), len(rows)


def seed_exact_qa83_schema13_state(db):
    inkdrop_state.ensure_schema(db)
    now = 1_720_000_000.0
    with inkdrop_state.connect(db) as con, con:
        con.execute("insert or replace into app_settings(key,scope,label,value_json,description,source,updated_at) values(?,?,?,?,?,?,?)", ("download_clients.qbittorrent.url", "download_clients", "qBittorrent URL", json.dumps("http://qbittorrent:8080"), "fixture", "user", now))
        con.execute("insert or replace into provider_configs(id,provider_type,display_name,enabled,base_url,secret_ref,settings_group,ownership,automation_role,capabilities_json,settings_json,source,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("prowlarr", "prowlarr", "Prowlarr", 1, "http://prowlarr:9696", "secret://prowlarr", "indexers", "user", "search", '["search"]', '{"indexer_ids":[12]}', "user", now, now))
        con.execute("insert or replace into auth_users(id,username,password_hash,role,enabled,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)", ("admin-fixture", "operator", "argon2id$fixture-hash-not-a-secret", "admin", 1, now, now, "{}"))
        con.execute("insert or replace into auth_sessions(id,user_id,token_hash,csrf_hash,created_at,last_seen_at,expires_at,raw_json) values(?,?,?,?,?,?,?,?)", ("session-fixture", "admin-fixture", "session-hash", "csrf-hash", now, now, now + 3600, "{}"))
        con.execute("insert or replace into api_keys(id,name,key_hash,fingerprint,prefix,scopes_json,role,enabled,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?)", ("key-fixture", "automation", "api-key-hash", "fingerprint", "ink_qa", '["read","acquisition"]', "admin", 1, now, now))
        con.execute("insert or replace into series(id,title,media_type,year,publisher,metadata_provider,metadata_id,source,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?)", ("series-fixture", "The Sandman", "comic", 1989, "DC Comics", "comicvine", "4207", "native", now, now, "{}"))
        con.execute("insert or replace into issues(id,series_id,issue_number,normalized_number,title,release_date,metadata_provider,metadata_id,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?)", ("issue-fixture", "series-fixture", "1", "1", "Sleep of the Just", "1989-01-01", "comicvine", "4208", now, now, "{}"))
        con.execute("insert or replace into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)", ("wanted-fixture", "series-fixture", "issue-fixture", "wanted", now, now, "{}"))
        con.execute("insert or replace into queue_items(id,wanted_id,series_id,issue_id,state,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)", ("queue-fixture", "wanted-fixture", "series-fixture", "issue-fixture", "queued", 1, now, now, "{}"))
        con.execute("insert or replace into source_attempts(id,queue_id,wanted_id,series_id,issue_id,source,provider_id,provider,protocol,status,title,started_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("attempt-fixture", "queue-fixture", "wanted-fixture", "series-fixture", "issue-fixture", "prowlarr", "prowlarr", "DOGnzb", "usenet", "sent", "The Sandman 001", now, "{}"))
        con.execute("insert or replace into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source_attempt_id,source,provider_id,provider,protocol,download_client,title,status,state,started_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("task-fixture", "queue-fixture", "wanted-fixture", "series-fixture", "issue-fixture", "attempt-fixture", "prowlarr", "prowlarr", "DOGnzb", "usenet", "sabnzbd", "The Sandman 001", "queued", "queued", now, now, "{}"))
        con.execute("insert or replace into import_results(id,queue_id,source_attempt_id,series_id,issue_id,source_path,dest_path,status,verified,created_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?)", ("import-fixture", "queue-fixture", "attempt-fixture", "series-fixture", "issue-fixture", "/staging/sandman.cbz", "/library/The Sandman/001.cbz", "pending", 0, now, "{}"))
        con.execute("insert or replace into history_events(id,entity_type,entity_id,series_id,issue_id,event_type,source,message,outcome,created_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?)", ("history-fixture", "queue", "queue-fixture", "series-fixture", "issue-fixture", "queue_created", "fixture", "Queued", "queued", now, "{}"))

        for table in ADDITIVE_TABLES:
            con.execute(f"drop table if exists {table}")
        con.execute("delete from schema_migrations where version>=14")
        con.execute("update schema_meta set value='13' where key='schema_version'")
    inkdrop_state.INIT_SCHEMA_READY_KEYS.discard(str(Path(db).resolve()))


def run():
    with tempfile.TemporaryDirectory(prefix="inkdrop-schema14-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed_exact_qa83_schema13_state(db)
        with inkdrop_state.connect_read(db) as con:
            before = {table: table_digest(con, table) for table in PRESERVED_TABLES}
            require(con.execute("select value from schema_meta where key='schema_version'").fetchone()["value"] == "13", "fixture must be exact schema 13 before migration")
            require(not con.execute("select 1 from schema_migrations where version>=14").fetchone(), "QA83 schema-13 fixture must not retain a v14 migration ledger artifact")
            require(not any(con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone() for table in ADDITIVE_TABLES), "QA83 schema-13 fixture must not retain a v14 additive table")
        backup = Path(temp) / "qa83-schema13-backup.sqlite3"
        shutil.copy2(db, backup)
        inkdrop_state.ensure_schema(db)
        with inkdrop_state.connect_read(db) as con:
            after = {table: table_digest(con, table) for table in PRESERVED_TABLES}
            version = con.execute("select value from schema_meta where key='schema_version'").fetchone()["value"]
            additive = {table for table in ADDITIVE_TABLES if con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()}
            integrity = con.execute("pragma integrity_check").fetchone()[0]
            foreign_keys = con.execute("pragma foreign_key_check").fetchall()
            ledger = con.execute("select version,name from schema_migrations where version=14").fetchone()
            auth = con.execute("select username,role,enabled from auth_users where id='admin-fixture'").fetchone()
            session = con.execute("select user_id,token_hash,csrf_hash from auth_sessions where id='session-fixture'").fetchone()
            api_key = con.execute("select scopes_json,enabled from api_keys where id='key-fixture'").fetchone()
            setting = con.execute("select value_json,source from app_settings where key='download_clients.qbittorrent.url'").fetchone()
        require(version == str(inkdrop_state.SCHEMA_VERSION), f"schema version must advance to {inkdrop_state.SCHEMA_VERSION}")
        require(before == after, f"schema {inkdrop_state.SCHEMA_VERSION} changed Build 61 state: {before} != {after}")
        require(additive == set(ADDITIVE_TABLES), "all additive identity/search tables must exist")
        require(integrity == "ok" and not foreign_keys, "upgrade must retain SQLite integrity")
        require(ledger and ledger["name"] == "manual_search_identity_and_private_handoff", "schema 14 migration ledger entry must identify Manual Search")
        require(dict(auth) == {"username": "operator", "role": "admin", "enabled": 1}, "admin identity/role must survive migration")
        require(dict(session) == {"user_id": "admin-fixture", "token_hash": "session-hash", "csrf_hash": "csrf-hash"}, "session and CSRF hashes must survive migration")
        require(json.loads(api_key["scopes_json"]) == ["read", "acquisition"] and api_key["enabled"] == 1, "API-key scopes must survive migration")
        require(json.loads(setting["value_json"]) == "http://qbittorrent:8080" and setting["source"] == "user", "user settings must survive migration")

        rollback = Path(temp) / "qa83-schema13-restored.sqlite3"
        shutil.copy2(backup, rollback)
        with inkdrop_state.connect_read(rollback) as con:
            rollback_version = con.execute("select value from schema_meta where key='schema_version'").fetchone()["value"]
            rollback_rows = {table: table_digest(con, table) for table in PRESERVED_TABLES}
            rollback_integrity = con.execute("pragma integrity_check").fetchone()[0]
            rollback_fks = con.execute("pragma foreign_key_check").fetchall()
        require(rollback_version == "13" and rollback_rows == before, "backup restore must reproduce exact pre-migration schema-13 state")
        require(rollback_integrity == "ok" and not rollback_fks, "restored schema-13 backup must retain integrity")
        print(json.dumps({"ok": True, "schema": version, "preserved_tables": len(before), "additive_tables": len(additive), "ledger": dict(ledger), "rollback_schema": rollback_version}, indent=2))


if __name__ == "__main__":
    run()
