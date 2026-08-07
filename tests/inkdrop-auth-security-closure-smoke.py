#!/usr/bin/env python3
"""Focused regressions for authentication security closure."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from core import inkdrop_auth
from core import inkdrop_state
from core import inkdrop_web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    invalid = inkdrop_auth.parse_trusted_proxy_networks("10.0.0.0/24,not-a-cidr")
    require(not invalid["configuration_valid"] and len(invalid["valid_networks"]) == 1, "mixed trust config must be invalid")
    require(inkdrop_auth.parse_trusted_proxy_networks("2001:db8::/32")["configuration_valid"], "IPv6 CIDR should parse")

    with tempfile.TemporaryDirectory(prefix="inkdrop-auth-closure-") as tmp:
        db_path = Path(tmp) / "state.sqlite3"
        inkdrop_state.ensure_schema(db_path)

        malformed = inkdrop_auth.public_status(db_path, {
            "INKDROP_AUTH_MODE": "external",
            "INKDROP_EXTERNAL_AUTH_ENABLED": "1",
            "INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES": "definitely-not-a-cidr",
        })
        require(not malformed["external_auth"]["ready"], "malformed external trust must not be ready")
        require(not malformed["external_auth"]["external_configuration_valid"], "status must expose safe invalid state")
        require(malformed["external_auth"]["trusted_proxy_count"] == 0, "invalid entries must not count")
        with inkdrop_state.connect(db_path) as con:
            con.execute(
                "insert into app_settings(key,scope,label,value_json,source,updated_at) values(?,?,?,?,?,?)",
                ("auth.external.trusted_proxies", "auth", "Trusted proxies", '["10.0.0.0/24"]', "user", time.time()),
            )
            con.commit()
        try:
            inkdrop_state.update_app_setting(db_path, "auth.external.trusted_proxies", ["bad-cidr"])
        except (ValueError, inkdrop_auth.AuthError):
            pass
        else:
            raise AssertionError("malformed trusted proxy setting must be rejected")
        with inkdrop_state.connect(db_path) as con:
            saved = con.execute("select value_json from app_settings where key='auth.external.trusted_proxies'").fetchone()[0]
        require(json.loads(saved) == ["10.0.0.0/24"], "invalid update must preserve last-known-good trust config")

        # Saving auth.mode=external is the one settings write that can lock an
        # otherwise-working install out of its entire API in one step (every
        # route 403s the instant external_ready is false), so it needs its own
        # guard rather than relying on the runtime readiness check alone.
        with inkdrop_state.connect(db_path) as con:
            con.executemany(
                "insert into app_settings(key,scope,label,value_json,source,updated_at) values(?,?,?,?,?,?)",
                [
                    ("auth.mode", "auth", "Configured Mode", '"built_in"', "runtime", time.time()),
                    ("auth.external.identity_header", "auth", "External Identity Header", '"X-Forwarded-User"', "runtime", time.time()),
                ],
            )
            con.commit()
        inkdrop_state.update_app_setting(db_path, "auth.external.trusted_proxies", [])

        try:
            inkdrop_state.update_app_setting(db_path, "auth.mode", "external")
        except inkdrop_auth.AuthError as exc:
            require(exc.code == "external_auth_not_ready", f"wrong refusal code for an unready external save: {exc.code}")
        else:
            raise AssertionError("auth.mode=external with no trusted proxy configured must be refused")
        with inkdrop_state.connect(db_path) as con:
            stored_mode = con.execute("select value_json from app_settings where key='auth.mode'").fetchone()[0]
        require(json.loads(stored_mode) == "built_in", "a refused external-mode save must not change auth.mode in the database")

        # mode=="external" forces external auth "enabled" once resolve_config()
        # reads it back (see inkdrop_auth.resolve_config), so a valid trusted
        # proxy alone -- with the standalone auth.external.enabled toggle left
        # at its default -- must be enough for the save to succeed.
        inkdrop_state.update_app_setting(db_path, "auth.external.trusted_proxies", ["10.0.0.0/24"])
        inkdrop_state.update_app_setting(db_path, "auth.mode", "external")
        with inkdrop_state.connect(db_path) as con:
            stored_mode = con.execute("select value_json from app_settings where key='auth.mode'").fetchone()[0]
        require(json.loads(stored_mode) == "external", "auth.mode=external with a valid trusted proxy configured must save normally")

        # built_in_or_external keeps the built-in login path usable even when
        # external isn't ready, so it can never lock the install out this way
        # and must not be subject to the same guard.
        inkdrop_state.update_app_setting(db_path, "auth.external.trusted_proxies", [])
        inkdrop_state.update_app_setting(db_path, "auth.mode", "built_in_or_external")
        with inkdrop_state.connect(db_path) as con:
            stored_mode = con.execute("select value_json from app_settings where key='auth.mode'").fetchone()[0]
        require(json.loads(stored_mode) == "built_in_or_external", "built_in_or_external must not be subject to the external-readiness guard")
        inkdrop_state.update_app_setting(db_path, "auth.mode", "built_in")

        inkdrop_state.bootstrap_auth_user(db_path, "admin", "correct horse battery staple")
        expired_recovery = inkdrop_auth.create_recovery_token(db_path)["recovery"]
        valid_recovery = inkdrop_auth.create_recovery_token(db_path)["recovery"]
        first = inkdrop_state.login_auth_user(db_path, "admin", "correct horse battery staple")["session"]
        second = inkdrop_state.login_auth_user(db_path, "admin", "correct horse battery staple")["session"]
        cookie = "; ".join((
            f"inkdrop_session={first['token']}",
            f"inkdrop_csrf={first['csrf_token']}",
        ))
        principal = inkdrop_web.inkdrop_auth_principal(
            {"Cookie": cookie, "X-InkDrop-CSRF": first["csrf_token"]}, db_path
        )
        require((principal.get("session") or {}).get("csrf_valid"), "matching cookie/header/session CSRF must pass")
        wrong_cookie = f"inkdrop_session={first['token']}; inkdrop_csrf={second['csrf_token']}"
        principal = inkdrop_web.inkdrop_auth_principal(
            {"Cookie": wrong_cookie, "X-InkDrop-CSRF": first["csrf_token"]}, db_path
        )
        require(not (principal.get("session") or {}).get("csrf_valid"), "wrong CSRF cookie must fail")
        principal = inkdrop_web.inkdrop_auth_principal(
            {"Cookie": f"inkdrop_session={first['token']}", "X-InkDrop-CSRF": first["csrf_token"]}, db_path
        )
        require(not (principal.get("session") or {}).get("csrf_valid"), "missing CSRF cookie must fail")

        no_expiry = inkdrop_state.create_api_key(db_path, "no expiry")["api_key"]
        expiring = inkdrop_state.create_api_key(db_path, "expiring", expires_in_seconds=60)["api_key"]
        require(no_expiry["expires_at"] is None, "no-expiry must be explicit and preserved")
        require(expiring["expires_at"] > time.time(), "future expiry must be persisted")
        with inkdrop_state.connect(inkdrop_auth.auth_store_path(db_path)) as con:
            con.execute("update api_keys set expires_at=? where id=?", (time.time() - 1, expiring["id"]))
            con.commit()
        require(not inkdrop_state.verify_api_key(db_path, expiring["key"]), "expired key must not authenticate")
        require(inkdrop_state.verify_api_key(db_path, no_expiry["key"]), "existing no-expiry key must authenticate")
        require(expiring["key"] not in json.dumps(inkdrop_state.list_api_keys(db_path)), "list must stay masked")

        maintenance = {"method": "api_key", "scopes": ["maintenance"], "id": "maintenance"}
        inkdrop_auth.authorize(maintenance, scope="maintenance", admin_required=False)
        try:
            inkdrop_auth.authorize(maintenance, scope="admin", admin_required=True)
        except inkdrop_auth.AuthError:
            pass
        else:
            raise AssertionError("maintenance scope must not imply admin")
        policy = inkdrop_web.inkdrop_auth_action_policy("/api/auth/maintenance/cleanup", "POST")
        require(policy["scope"] == "maintenance" and not policy["admin_only"], "routine cleanup policy must allow maintenance")

        now = time.time()
        with inkdrop_state.connect(inkdrop_auth.auth_store_path(db_path)) as con:
            con.execute("update auth_sessions set expires_at=? where id=?", (now - 90000, first["id"]))
            con.execute("update auth_recovery_tokens set expires_at=? where id=?", (now - 90000, expired_recovery["id"]))
            con.execute(
                "insert into auth_login_attempts(bucket_key,failure_count,updated_at,locked_until) values(?,?,?,?)",
                ("old", 0, now - 90000, None),
            )
            con.commit()
        cleanup = inkdrop_auth.cleanup_auth_state(db_path, batch_limit=1, now=now)
        require(cleanup["ok"] and cleanup["examined"] <= 3, "cleanup must be bounded per category")
        second_cleanup = inkdrop_auth.cleanup_auth_state(db_path, batch_limit=10, now=now)
        require(second_cleanup["ok"], "cleanup must be idempotent")
        require(inkdrop_state.verify_auth_session(db_path, second["token"]), "active session must survive cleanup")
        with inkdrop_state.connect(inkdrop_auth.auth_store_path(db_path)) as con:
            require(not con.execute("select 1 from auth_recovery_tokens where id=?", (expired_recovery["id"],)).fetchone(), "expired recovery token must be removed after retention")
            require(con.execute("select 1 from auth_recovery_tokens where id=?", (valid_recovery["id"],)).fetchone(), "valid recovery token must survive cleanup")

    print(json.dumps({"ok": True, "auth_security_closure": "passed"}, indent=2))


if __name__ == "__main__":
    main()
