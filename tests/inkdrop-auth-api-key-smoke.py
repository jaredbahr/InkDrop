#!/usr/bin/env python3
"""Smoke test for InkDrop built-in auth and API key storage."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import inkdrop_auth
import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-auth-smoke-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        inkdrop_state.ensure_schema(db_path)

        status = inkdrop_state.auth_status(db_path, environ={})
        require(status["built_in_auth"]["bootstrap_required"] is True, "fresh DB should require auth bootstrap")
        require(status["api_keys"]["configured"] is False, "fresh DB should have no API keys")

        user = inkdrop_state.bootstrap_auth_user(db_path, "admin", "correct horse battery staple")
        require(user["ok"], "bootstrap user should be created")
        require("password_hash" not in json.dumps(user), "bootstrap response must not expose password hash")

        login = inkdrop_state.login_auth_user(db_path, "admin", "correct horse battery staple")
        require(login["ok"], "login should succeed")
        session_token = login["session"]["token"]
        require(session_token.startswith("is_"), "session token should use InkDrop session prefix")
        require(inkdrop_state.verify_auth_session(db_path, session_token), "session should verify")
        require(inkdrop_state.revoke_auth_session(db_path, session_token)["revoked"] == 1, "session should revoke")
        require(not inkdrop_state.verify_auth_session(db_path, session_token), "revoked session should not verify")

        api_key = inkdrop_state.create_api_key(db_path, "Automation")["api_key"]
        raw_key = api_key["key"]
        require(raw_key.startswith("ik_"), "api key should use InkDrop API key prefix")
        require(api_key["fingerprint"], "API key create response should include fingerprint")
        require(inkdrop_state.verify_api_key(db_path, raw_key), "API key should verify")

        listed = inkdrop_state.list_api_keys(db_path)
        encoded_list = json.dumps(listed, sort_keys=True)
        require(raw_key not in encoded_list, "API key list must not expose raw key")
        require(api_key["fingerprint"] in encoded_list, "API key list should expose fingerprint")
        require("preview" in listed[0], "API key list should expose masked preview")

        status = inkdrop_state.auth_status(db_path, environ={})
        require(status["built_in_auth"]["configured"] is True, "auth status should show configured user")
        require(status["api_keys"]["active_count"] == 1, "auth status should count active API key")
        require(raw_key not in json.dumps(status, sort_keys=True), "auth status must not expose raw API key")

        revoked = inkdrop_state.revoke_api_key(db_path, api_key["id"])
        require(revoked["revoked"] == 1, "API key should revoke")
        require(not inkdrop_state.verify_api_key(db_path, raw_key), "revoked API key should not verify")

    print(json.dumps({"ok": True, "auth_smoke": "passed"}, indent=2))


if __name__ == "__main__":
    main()
