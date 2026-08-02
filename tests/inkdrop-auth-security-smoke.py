#!/usr/bin/env python3
"""Regression coverage for InkDrop authentication security primitives."""

from __future__ import annotations

import json
import contextlib
import sqlite3
import sys
import tempfile
import time
import types
import zipfile
from pathlib import Path

import inkdrop_auth
import inkdrop_backup_restore
import inkdrop_state

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    class _RequestsException(Exception):
        pass
    requests_stub.exceptions = types.SimpleNamespace(
        RequestException=_RequestsException,
        Timeout=_RequestsException,
        ConnectionError=_RequestsException,
        HTTPError=_RequestsException,
    )
    sys.modules["requests"] = requests_stub

import inkdrop_web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def expect_auth_error(callback, code):
    try:
        callback()
    except inkdrop_auth.AuthError as exc:
        require(exc.code == code, f"expected {code}, received {exc.code}")
        return exc
    raise AssertionError(f"expected AuthError {code}")


def create_v11_fixture(path, *, include_admin=True):
    con = sqlite3.connect(path)
    con.executescript(
        """
        pragma foreign_keys=on;
        create table schema_meta(key text primary key,value text);
        insert into schema_meta values('schema_version','11');
        create table app_settings(key text primary key,scope text,label text,value_json text,description text,source text,updated_at real);
        create table auth_users(id text primary key,username text unique,password_hash text,role text,enabled integer,created_at real,updated_at real,last_login_at real,raw_json text);
        create table auth_sessions(id text primary key,user_id text,token_hash text unique,created_at real,expires_at real,revoked_at real,user_agent text,remote_addr text,raw_json text);
        create table api_keys(id text primary key,name text,key_hash text unique,fingerprint text,prefix text,role text,enabled integer,created_at real,updated_at real,last_used_at real,revoked_at real,raw_json text);
        """
    )
    if include_admin:
        con.execute("insert into auth_users values('legacy-admin','admin','pbkdf2_sha256$260000$00$00','admin',1,1,1,null,'{}')")
    con.commit()
    con.close()


def create_qa37_upgrade_fixture(path):
    create_v11_fixture(path, include_admin=False)
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table provider_configs (
            id text primary key, provider_type text not null, display_name text not null,
            enabled integer not null default 1, base_url text, secret_ref text,
            settings_group text, ownership text, automation_role text, description text,
            next_action text, capabilities_json text, applied_by_json text,
            settings_json text, source text, created_at real, updated_at real
        );
        """
    )
    settings = {
        "paths.remote_path_mappings": [{"remote": "/downloads", "local": "/staging/downloads"}],
        "paths.sab_path_mappings": [{"remote": "/complete", "local": "/staging/complete"}],
        "media_management.comic_root": "/library/comics",
        "automation.source_order": ["prowlarr", "slskd", "direct"],
    }
    for key, value in settings.items():
        con.execute(
            "insert into app_settings(key,scope,label,value_json,description,source,updated_at) values(?,?,?,?,?,?,?)",
            (key, "general", key, json.dumps(value, sort_keys=True), "QA Build 37 saved value", "user", 37.0),
        )
    providers = (
        ("qbittorrent", "download_client", "qBittorrent", "http://qbittorrent:8080", "env:INKDROP_QBITTORRENT_PASSWORD", {"remote_path_mappings": settings["paths.remote_path_mappings"]}),
        ("kavita", "library_frontend", "Kavita", "http://kavita:5000", "env:INKDROP_KAVITA_API_KEY", {"library_root": "/library/comics"}),
        ("kapowarr", "adapter", "Kapowarr", "http://kapowarr:5656", "env:INKDROP_KAPOWARR_API_KEY", {"allow_legacy_sync": False}),
        ("prowlarr", "indexer", "Prowlarr", "http://prowlarr:9696", "env:INKDROP_PROWLARR_API_KEY", {"categories": [7000, 7030]}),
    )
    for provider_id, provider_type, display_name, base_url, secret_ref, provider_settings in providers:
        con.execute(
            """
            insert into provider_configs(
                id,provider_type,display_name,enabled,base_url,secret_ref,settings_group,
                ownership,automation_role,description,next_action,capabilities_json,
                applied_by_json,settings_json,source,created_at,updated_at
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                provider_id, provider_type, display_name, 1, base_url, secret_ref,
                provider_type, "user", "configured", "QA Build 37 provider", "",
                "[]", "{}", json.dumps(provider_settings, sort_keys=True), "user", 37.0, 37.0,
            ),
        )
    con.commit()
    con.close()


def saved_configuration_snapshot(path):
    with contextlib.closing(sqlite3.connect(path)) as con:
        settings = con.execute(
            "select key,scope,label,value_json,description,source,updated_at from app_settings order by key"
        ).fetchall()
        providers = con.execute(
            """
            select id,provider_type,display_name,enabled,base_url,secret_ref,settings_group,
                   ownership,automation_role,description,next_action,capabilities_json,
                   applied_by_json,settings_json,source,created_at,updated_at
            from provider_configs order by id
            """
        ).fetchall()
    return {"settings": settings, "providers": providers}


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-auth-security-") as tmp:
        root = Path(tmp)
        db = root / "state.sqlite3"
        inkdrop_state.ensure_schema(db)

        # Public-alpha default and explicit trusted-LAN upgrade escape hatch.
        status = inkdrop_auth.public_status(db, environ={})
        require(status["mode"] == "built_in" and not status["required"], "built-in mode must not enforce before bootstrap")
        require(status["setup_required"] and not status["enforcement_active"], "fresh installs must expose setup-required without lockout")
        require(status["built_in_auth"]["bootstrap_required"], "fresh unauthenticated QA must expose bootstrap")
        disabled = inkdrop_auth.public_status(
            db,
            environ={"INKDROP_AUTH_MODE": "disabled", "INKDROP_AUTH_ALLOW_DISABLED": "1", "INKDROP_TRUSTED_LAN_TESTING": "1"},
        )
        require(not disabled["required"], "trusted-LAN disabled upgrade mode should remain available only when explicit")
        rejected = inkdrop_auth.public_status(db, environ={"INKDROP_AUTH_MODE": "disabled"})
        require(rejected["mode"] == "built_in" and rejected["mode_rejected"] and not rejected["required"], "unsafe disabled mode must fall back to pending built-in setup without lockout")

        legacy_external_env = {
            "INKDROP_EXTERNAL_AUTH_ENABLED": "1",
            "INKDROP_EXTERNAL_AUTH_HEADER": "X-Auth-User",
            "INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES": "10.20.0.0/16",
        }
        legacy_external = inkdrop_auth.public_status(db, environ=legacy_external_env)
        require(legacy_external["mode"] == "external" and legacy_external["required"], "legacy external-auth installs must retain external mode")
        require(legacy_external["external_auth"]["ready"], "legacy external-auth policy must remain ready after upgrade")
        legacy_combined = inkdrop_auth.public_status(db, environ={**legacy_external_env, "INKDROP_AUTH_REQUIRED": "1"})
        require(legacy_combined["mode"] == "built_in_or_external" and legacy_combined["required"], "legacy combined auth installs must retain both authorities")

        # Sibling auth stores mean one installation per directory; scenarios
        # that model separate installs each get their own.
        (root / "config-precedence").mkdir()
        config_db = root / "config-precedence" / "config-precedence.sqlite3"
        inkdrop_state.ensure_schema(config_db)
        inkdrop_state.sync_settings(
            config_db,
            settings=[
                {"key": "auth.mode", "scope": "general", "label": "Authentication Mode", "value": "built_in", "source": "runtime"},
                {"key": "auth.external.enabled", "scope": "general", "label": "External Authentication", "value": False, "source": "runtime"},
                {"key": "auth.external.trusted_proxies", "scope": "general", "label": "Trusted Proxies", "value": [], "source": "runtime"},
            ],
        )
        inkdrop_state.update_app_setting(config_db, "auth.mode", "built_in_or_external")
        inkdrop_state.update_app_setting(config_db, "auth.external.enabled", True)
        inkdrop_state.update_app_setting(config_db, "auth.external.trusted_proxies", ["10.55.0.0/16"])
        user_config = inkdrop_auth.resolve_config(
            config_db,
            environ={"INKDROP_AUTH_MODE": "built_in", "INKDROP_EXTERNAL_AUTH_ENABLED": "0"},
        )
        require(user_config["mode"] == "built_in_or_external" and user_config["external_enabled"], "user-owned SQLite auth policy must override bootstrap environment defaults")
        require(user_config["external"]["trusted_proxies"] == ["10.55.0.0/16"], "user-owned trusted proxy settings must persist")

        hashed = inkdrop_auth.password_hash("a correct horse battery staple")
        require("correct horse" not in hashed and inkdrop_auth.verify_password("a correct horse battery staple", hashed), "password hash must verify without plaintext")
        require(not inkdrop_auth.verify_password("wrong password value", hashed), "wrong password must fail")

        user = inkdrop_auth.bootstrap_admin(db, "admin", "a correct horse battery staple")
        user_id = user["user"]["id"]
        enforced = inkdrop_auth.public_status(db, environ={})
        require(enforced["required"] and enforced["enforcement_active"] and not enforced["setup_required"], "successful bootstrap must activate built-in enforcement")
        login = inkdrop_auth.login(db, "admin", "a correct horse battery staple", remote_addr="127.0.0.1")
        session_token = login["session"]["token"]
        csrf_token = login["session"]["csrf_token"]
        session = inkdrop_auth.verify_session(db, session_token, csrf_token=csrf_token)
        require(session and session["session"]["csrf_valid"], "session and matching CSRF token must validate")
        require(not inkdrop_auth.verify_session(db, session_token, csrf_token="wrong")["session"]["csrf_valid"], "wrong CSRF token must fail")
        with contextlib.closing(sqlite3.connect(inkdrop_auth.auth_store_path(db))) as con:
            stored_session = con.execute("select token_hash,csrf_hash from auth_sessions where id=?", (login["session"]["id"],)).fetchone()
        require(session_token not in stored_session and csrf_token not in stored_session, "session and CSRF plaintext must never be stored")
        require(stored_session == (inkdrop_auth._digest(session_token), inkdrop_auth._digest(csrf_token)), "session verifiers must be cryptographic digests")

        # Persistent login backoff.
        for _ in range(inkdrop_auth.LOGIN_MAX_FAILURES):
            expect_auth_error(lambda: inkdrop_auth.login(db, "admin", "definitely wrong", remote_addr="198.51.100.2"), "invalid_credentials")
        limited = expect_auth_error(lambda: inkdrop_auth.login(db, "admin", "a correct horse battery staple", remote_addr="198.51.100.2"), "login_rate_limited")
        require(limited.status == 429 and limited.retry_after, "rate limit must return durable retry guidance")

        # Expiry and explicit revocation.
        with contextlib.closing(sqlite3.connect(inkdrop_auth.auth_store_path(db))) as con:
            con.execute("update auth_sessions set expires_at=? where token_hash=?", (time.time() - 1, inkdrop_auth._digest(session_token)))
            con.commit()
        require(not inkdrop_auth.verify_session(db, session_token), "expired session must fail")
        login = inkdrop_auth.login(db, "admin", "a correct horse battery staple", remote_addr="127.0.0.1")
        session_token = login["session"]["token"]
        csrf_token = login["session"]["csrf_token"]
        require(inkdrop_auth.revoke_session(db, session_token)["revoked"] == 1, "logout must revoke session")
        require(not inkdrop_auth.verify_session(db, session_token), "revoked session must fail")

        # Scoped API keys: plaintext once, digest at rest, mask on list.
        created = inkdrop_auth.create_api_key(db, "Reader", description="Read-only integration", scopes=["read"])
        raw_key = created["api_key"]["key"]
        key_id = created["api_key"]["id"]
        verified_key = inkdrop_auth.verify_api_key(db, raw_key, mark_used=True)
        require(verified_key and verified_key["scopes"] == ["read"], "read API key must verify with its scope")
        inkdrop_auth.authorize({"method": "api_key", **verified_key}, scope="read")
        expect_auth_error(lambda: inkdrop_auth.authorize({"method": "api_key", **verified_key}, scope="settings"), "insufficient_scope")
        listed = json.dumps(inkdrop_auth.list_api_keys(db), sort_keys=True)
        require(raw_key not in listed and created["api_key"]["fingerprint"] in listed, "API key list must be masked")
        with contextlib.closing(sqlite3.connect(inkdrop_auth.auth_store_path(db))) as con:
            stored = con.execute("select key_hash,description,scopes_json,last_used_at from api_keys where id=?", (key_id,)).fetchone()
        require(stored[0] == inkdrop_auth._digest(raw_key) and raw_key != stored[0], "only API key digest may be stored")
        require(stored[1] == "Read-only integration" and json.loads(stored[2]) == ["read"] and stored[3], "API key metadata and last use must persist")

        # External headers are accepted only across the configured proxy boundary.
        external_config = inkdrop_auth.resolve_config(
            db,
            environ={
                "INKDROP_AUTH_MODE": "external",
                "INKDROP_EXTERNAL_AUTH_ENABLED": "1",
                "INKDROP_EXTERNAL_AUTH_HEADER": "X-Auth-User",
                "INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES": "10.20.0.0/16",
                "INKDROP_EXTERNAL_AUTH_GROUP_HEADER": "X-Auth-Groups",
                "INKDROP_EXTERNAL_AUTH_ADMIN_GROUP": "inkdrop-admins",
            },
        )
        headers = {"X-Auth-User": "jared", "X-Auth-Groups": "users,inkdrop-admins"}
        require(inkdrop_auth.external_principal(headers, "10.20.4.8", external_config), "trusted proxy/admin group should authenticate")
        require(not inkdrop_auth.external_principal(headers, "192.0.2.8", external_config), "untrusted forwarded headers must be ignored")
        require(not inkdrop_auth.external_principal({"X-Auth-User": "jared", "X-Auth-Groups": "users"}, "10.20.4.8", external_config), "missing admin group must fail")
        require(not inkdrop_auth.external_principal({"X-Forwarded-User": "jared", "X-Auth-Groups": "inkdrop-admins"}, "10.20.4.8", external_config), "wrong identity header must fail")
        require(not inkdrop_auth.external_principal({"X-Auth-Groups": "inkdrop-admins"}, "10.20.4.8", external_config), "missing identity must fail")

        # Password change revokes other sessions; recovery is one-time.
        first = inkdrop_auth.login(db, "admin", "a correct horse battery staple", remote_addr="127.0.0.1")
        second = inkdrop_auth.login(db, "admin", "a correct horse battery staple", remote_addr="127.0.0.2")
        inkdrop_auth.change_password(db, user_id, "a correct horse battery staple", "a newer correct horse password", current_session_id=first["session"]["id"])
        require(not inkdrop_auth.verify_session(db, second["session"]["token"]), "password change must revoke other sessions")
        recovery = inkdrop_auth.create_recovery_token(db)["recovery"]
        recovery_session = inkdrop_auth.login(db, "admin", "a newer correct horse password", remote_addr="127.0.0.3")
        inkdrop_auth.reset_password_with_recovery(db, recovery["token"], "a recovered correct horse password")
        require(not inkdrop_auth.verify_session(db, recovery_session["session"]["token"]), "password recovery must revoke active sessions")
        expect_auth_error(lambda: inkdrop_auth.reset_password_with_recovery(db, recovery["token"], "another recovered password"), "invalid_recovery_token")
        preserved_primary = inkdrop_auth.login(db, "admin", "a recovered correct horse password", remote_addr="127.0.0.4")
        preserved_secondary = inkdrop_auth.login(db, "admin", "a recovered correct horse password", remote_addr="127.0.0.5")
        optional_change = inkdrop_auth.change_password(
            db,
            user_id,
            "a recovered correct horse password",
            "an optional-session correct password",
            current_session_id=preserved_primary["session"]["id"],
            revoke_other_sessions=False,
        )
        require(not optional_change["other_sessions_revoked"], "password change must report the optional session-preservation choice")
        require(inkdrop_auth.verify_session(db, preserved_secondary["session"]["token"]), "password change may preserve other sessions when explicitly requested")
        expired_recovery = inkdrop_auth.create_recovery_token(db)["recovery"]
        with contextlib.closing(sqlite3.connect(inkdrop_auth.auth_store_path(db))) as con:
            con.execute("update auth_recovery_tokens set expires_at=? where id=?", (time.time() - 1, expired_recovery["id"]))
            con.commit()
        expect_auth_error(lambda: inkdrop_auth.reset_password_with_recovery(db, expired_recovery["token"], "expired recovery password"), "invalid_recovery_token")

        # Audit records and backup exports must contain no reusable credentials.
        audit_text = json.dumps(inkdrop_auth.recent_audit_events(db, 500), sort_keys=True)
        for secret in (raw_key, session_token, csrf_token, recovery["token"], "an optional-session correct password"):
            require(secret not in audit_text, "auth audit must redact reusable credentials")
        backup = inkdrop_backup_restore.create_backup_archive(
            config_dir=root / "config",
            state_db_path=db,
            backup_dir=root / "backups",
            environ={"INKDROP_AUTH_MODE": "built_in", "INKDROP_SAMPLE_API_KEY": raw_key},
            label="auth-test",
        )
        with zipfile.ZipFile(backup["archive_path"], "r") as zf:
            config_export = zf.read(inkdrop_backup_restore.CONFIG_EXPORT_ARCHIVE_NAME).decode("utf-8")
            extracted = root / "backup-state.sqlite3"
            extracted.write_bytes(zf.read(inkdrop_backup_restore.STATE_DB_ARCHIVE_NAME))
            manifest = json.loads(zf.read(inkdrop_backup_restore.MANIFEST_ARCHIVE_NAME))
        require(raw_key not in config_export, "config export must redact API keys")
        with contextlib.closing(sqlite3.connect(inkdrop_auth.auth_store_path(db))) as con:
            credential_hashes = [row[0] for row in con.execute("select password_hash from auth_users")]
            credential_hashes += [row[0] for row in con.execute("select token_hash from auth_sessions")]
            credential_hashes += [row[0] for row in con.execute("select key_hash from api_keys")]
            credential_hashes += [row[0] for row in con.execute("select token_hash from auth_recovery_tokens")]
        require(not any(value and value in config_export for value in credential_hashes), "redacted config export must exclude auth verifier hashes")
        require(manifest["contains"]["reusable_credentials"] is False, "backup manifest must deny reusable credentials")
        require(inkdrop_auth.sanitize_auth_database_copy(extracted)["ok"], "state backup must contain only credential hashes")

        # Version-11 upgrade preserves rows, the v12 auth baseline, and the
        # additive v13 API-key-expiration migration.
        legacy = root / "legacy.sqlite3"
        create_v11_fixture(legacy)
        inkdrop_state.ensure_schema(legacy)
        with contextlib.closing(sqlite3.connect(legacy)) as con:
            version = con.execute("select value from schema_meta where key='schema_version'").fetchone()[0]
            migration = con.execute("select name from schema_migrations where version=12").fetchone()[0]
            expiry_migration = con.execute("select name from schema_migrations where version=13").fetchone()[0]
            columns = {row[1] for row in con.execute("pragma table_info(api_keys)")}
            preserved = con.execute("select username from auth_users where id='legacy-admin'").fetchone()[0]
        # Authentication owns migrations 12 and 13, while later additive
        # feature migrations may advance the repository-wide schema version.
        require(int(version) >= 13 and migration == "auth_security" and expiry_migration == "auth_security_api_key_expiry", "v12 baseline and additive v13 migration must be recorded")
        require({"description", "scopes_json", "expires_at"} <= columns and preserved == "admin", "migration must preserve legacy auth rows")

        # QA Build 37-style upgrades preserve user-owned settings and remain
        # accessible until the first administrator is created successfully.
        # Own directory: the auth store is a sibling file, and this scenario
        # must model a separate installation, not share the first fixture's.
        (root / "qa37").mkdir()
        qa37 = root / "qa37" / "qa37.sqlite3"
        create_qa37_upgrade_fixture(qa37)
        before_upgrade = saved_configuration_snapshot(qa37)
        inkdrop_state.ensure_schema(qa37)
        after_upgrade = saved_configuration_snapshot(qa37)
        require(after_upgrade == before_upgrade, "additive auth migration must not rewrite saved settings, providers, URLs, or path mappings")
        qa37_status = inkdrop_auth.public_status(qa37, environ={})
        require(qa37_status["mode"] == "built_in" and qa37_status["setup_required"], "existing unauthenticated QA must enter explicit setup-required state")
        require(qa37_status["built_in_auth"]["bootstrap_required"] and not qa37_status["required"], "existing QA must not enforce login before bootstrap")
        inkdrop_auth.bootstrap_admin(qa37, "qa-admin", "a QA upgrade administrator password")
        require(inkdrop_auth.public_status(qa37, environ={})["required"], "existing QA must enforce built-in auth immediately after successful bootstrap")

        # Injected migration failure rolls all DDL back.
        rollback_db = root / "rollback.sqlite3"
        con = sqlite3.connect(rollback_db)
        try:
            try:
                inkdrop_auth.ensure_auth_schema(con, _test_fail_after=2)
            except RuntimeError:
                pass
            tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        finally:
            con.close()
        require("auth_users" not in tables and "schema_migrations" not in tables, "failed auth migration must roll back atomically")

        # Required route names and public policy remain explicit.
        web_source = Path(inkdrop_web.__file__).read_text(encoding="utf-8")
        for route in ("/api/auth/status", "/api/auth/bootstrap", "/api/auth/login", "/api/auth/logout", "/api/auth/session", "/api/auth/password", "/api/auth/api-keys"):
            require(route in web_source, f"required auth route missing: {route}")
        fresh_dir = root / "fresh"
        fresh_dir.mkdir()
        require(inkdrop_web.inkdrop_auth_is_public_path("/api/auth/bootstrap", "POST", inkdrop_auth.public_status(fresh_dir / "fresh.sqlite3", {})), "fresh bootstrap must remain public")
        require(not inkdrop_web.inkdrop_auth_is_public_path("/api/inkdrop-state", "GET", status), "application state must require authentication")
        require(
            inkdrop_web.inkdrop_request_is_https(
                {"X-Forwarded-Proto": "https"},
                "10.20.4.8",
                environ={"INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES": "10.20.0.0/16"},
                db_path=db,
            ),
            "trusted HTTPS proxy must produce Secure cookies",
        )
        require("Secure" in inkdrop_web.inkdrop_auth_session_cookie("test", secure=True), "HTTPS session cookie must be Secure")

    print(json.dumps({"ok": True, "auth_security_smoke": "passed", "schema_version": inkdrop_state.SCHEMA_VERSION}, indent=2))


if __name__ == "__main__":
    main()
