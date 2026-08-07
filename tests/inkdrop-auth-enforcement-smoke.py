#!/usr/bin/env python3
"""Smoke test for InkDrop's opt-in request auth gate helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

from core import inkdrop_auth
from core import inkdrop_state

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

from core import inkdrop_web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-auth-enforce-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        inkdrop_state.ensure_schema(db_path)

        status, required = inkdrop_web.inkdrop_auth_required_status(
            db_path,
            environ={
                "INKDROP_AUTH_MODE": "disabled",
                "INKDROP_AUTH_ALLOW_DISABLED": "1",
                "INKDROP_TRUSTED_LAN_TESTING": "1",
            },
        )
        require(status["required"] is False, "explicit trusted-LAN disabled mode should be optional")
        require(required is False, "required flag should mirror auth status")

        status, required = inkdrop_web.inkdrop_auth_required_status(
            db_path,
            environ={"INKDROP_AUTH_MODE": "disabled"},
        )
        require(status["mode_rejected"] is True, "disabled mode must be rejected without both safety acknowledgements")
        require(status["setup_required"] is True and status["required"] is False, "rejected disabled mode must require setup without locking out the upgrade")
        require(required is False, "rejected disabled mode should not enforce before bootstrap")

        status, required = inkdrop_web.inkdrop_auth_required_status(
            db_path,
            environ={"INKDROP_AUTH_REQUIRED": "1"},
        )
        require(status["setup_required"] is True and status["required"] is False, "legacy auth-required installs must expose bootstrap without pre-bootstrap lockout")
        require(required is False, "required flag should remain off until bootstrap succeeds")
        require(
            inkdrop_web.inkdrop_auth_is_public_path("/api/inkdrop-auth/bootstrap", "POST", status),
            "bootstrap must remain public before first user exists",
        )
        require(
            not inkdrop_web.inkdrop_auth_is_public_path("/api/inkdrop-state", "GET", status),
            "state endpoints must not be public when auth is required",
        )
        for diagnostic_path in (
            "/api/inkdrop-diagnostics/acquisition-funnel",
            "/api/inkdrop-diagnostics/pack-duplicates",
            "/api/inkdrop-diagnostics/managed-library-audit",
            "/api/system/update-status",
        ):
            policy = inkdrop_web.inkdrop_auth_action_policy(diagnostic_path, "GET")
            require(policy["scope"] == "admin" and policy["admin_only"], f"{diagnostic_path} must require an administrator")
        normal_status_policy = inkdrop_web.inkdrop_auth_action_policy("/api/system/version", "GET")
        require(
            normal_status_policy["scope"] == "read" and not normal_status_policy["admin_only"],
            "installed version must remain available to ordinary signed-in users",
        )

        user = inkdrop_state.bootstrap_auth_user(db_path, "admin", "correct horse battery staple")
        require(user["ok"], "bootstrap should create the first user")
        status, _ = inkdrop_web.inkdrop_auth_required_status(
            db_path,
            environ={"INKDROP_AUTH_REQUIRED": "1"},
        )
        require(status["required"] is True and status["enforcement_active"] is True, "successful bootstrap must activate enforcement")
        require(
            not inkdrop_web.inkdrop_auth_is_public_path("/api/inkdrop-auth/bootstrap", "POST", status),
            "bootstrap must close after first user exists",
        )

        login = inkdrop_state.login_auth_user(db_path, "admin", "correct horse battery staple")
        session_token = login["session"]["token"]
        require(
            inkdrop_web.inkdrop_auth_principal({"Authorization": f"Bearer {session_token}"}, db_path),
            "bearer session token should authenticate",
        )
        cookie_header = inkdrop_web.inkdrop_auth_session_cookie(session_token)
        require("HttpOnly" in cookie_header and "SameSite=Lax" in cookie_header, "session cookie should be browser-safe")
        require(
            inkdrop_web.inkdrop_auth_principal({"Cookie": cookie_header}, db_path),
            "session cookie should authenticate",
        )

        created_key = inkdrop_state.create_api_key(db_path, "Automation")["api_key"]
        raw_key = created_key["key"]
        require(
            inkdrop_web.inkdrop_auth_principal({"X-InkDrop-API-Key": raw_key}, db_path),
            "InkDrop API key header should authenticate",
        )
        require(
            inkdrop_web.inkdrop_auth_principal({"X-Api-Key": raw_key}, db_path),
            "generic API key header should authenticate",
        )

        external = inkdrop_web.inkdrop_auth_principal(
            {"X-Forwarded-User": "jared"},
            db_path,
            remote_addr="10.0.0.5",
            environ={
                "INKDROP_AUTH_MODE": "external",
                "INKDROP_EXTERNAL_AUTH_ENABLED": "1",
                "INKDROP_EXTERNAL_AUTH_HEADER": "X-Forwarded-User",
                "INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES": "10.0.0.0/24",
            },
        )
        require(external and external["method"] == "external", "trusted proxy external auth header should authenticate")
        require(
            not inkdrop_web.inkdrop_auth_principal(
                {"X-Forwarded-User": "jared"},
                db_path,
                remote_addr="192.0.2.10",
                environ={
                    "INKDROP_AUTH_MODE": "external",
                    "INKDROP_EXTERNAL_AUTH_ENABLED": "1",
                    "INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES": "10.0.0.0/24",
                },
            ),
            "external auth headers must be ignored outside configured trusted proxy networks",
        )

    print(json.dumps({"ok": True, "auth_enforcement_smoke": "passed"}, indent=2))


if __name__ == "__main__":
    main()
