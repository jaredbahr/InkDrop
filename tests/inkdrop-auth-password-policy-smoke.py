#!/usr/bin/env python3
"""Regression coverage for InkDrop's configurable password policy."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from pathlib import Path

import inkdrop_auth
import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def expect_code(code, callback):
    try:
        callback()
    except inkdrop_auth.AuthError as exc:
        require(exc.code == code, f"expected {code}, received {exc.code}")
        return
    raise AssertionError(f"expected {code}")


def save_minimum(db, value):
    with sqlite3.connect(db) as con:
        con.execute(
            """
            insert into app_settings(key,scope,label,value_json,description,source,updated_at)
            values('auth.password_min_length','general','Minimum Password Length',?,'','user',?)
            on conflict(key) do update set value_json=excluded.value_json,source='user',updated_at=excluded.updated_at
            """,
            (json.dumps(value), time.time()),
        )
    inkdrop_auth.clear_config_cache()


def main():
    # inkdrop_db keeps a process-local pooled handle; Windows may retain it
    # through interpreter shutdown even after each transaction is closed.
    with tempfile.TemporaryDirectory(prefix="inkdrop-password-policy-", ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "state.sqlite3"
        inkdrop_state.ensure_schema(db)

        default = inkdrop_auth.password_policy(db, environ={})
        require(default["minimum_length"] == 8, "public default must be 8")
        require(default["maximum_length"] >= 256, "maximum must accept at least 256 characters")
        require(not default["composition_required"], "composition rules must remain disabled")
        require(default["spaces_allowed"] and default["unicode_allowed"], "spaces and Unicode must be allowed")
        expect_code("password_too_short", lambda: inkdrop_auth.password_hash("1234567", policy=default))
        require(inkdrop_auth.verify_password("12345678", inkdrop_auth.password_hash("12345678", policy=default)), "8 characters must work")
        require(inkdrop_auth.verify_password("        ", inkdrop_auth.password_hash("        ", policy=default)), "spaces must not be trimmed")
        require(inkdrop_auth.verify_password("密碼密碼密碼密碼", inkdrop_auth.password_hash("密碼密碼密碼密碼", policy=default)), "Unicode must work")
        long_password = "長" * 256
        require(inkdrop_auth.verify_password(long_password, inkdrop_auth.password_hash(long_password, policy=default)), "long password must not be truncated")

        old_hash = inkdrop_auth.password_hash("an existing twelve character password", policy=default)
        save_minimum(db, 128)
        require(inkdrop_auth.verify_password("an existing twelve character password", old_hash), "raising policy must not invalidate existing hashes")

        save_minimum(db, 1)
        saved = inkdrop_auth.password_policy(db, environ={"INKDROP_PASSWORD_MIN_LENGTH": "25"})
        require(saved["minimum_length"] == 1 and saved["source"] == "saved", "saved policy must win over environment")
        require(inkdrop_auth.verify_password("x", inkdrop_auth.password_hash("x", policy=saved)), "one character works only when configured")
        expect_code("password_empty", lambda: inkdrop_auth.password_hash("", policy=saved))

        with sqlite3.connect(db) as con:
            con.execute("delete from app_settings where key='auth.password_min_length'")
        inkdrop_auth.clear_config_cache()
        require(inkdrop_auth.password_policy(db, environ={"INKDROP_PASSWORD_MIN_LENGTH": "9"})["minimum_length"] == 9, "environment fallback must work")
        expect_code("invalid_password_policy", lambda: inkdrop_auth.password_policy(db, environ={"INKDROP_PASSWORD_MIN_LENGTH": "0"}))
        expect_code("invalid_password_policy", lambda: inkdrop_auth.password_policy(db, environ={"INKDROP_PASSWORD_MIN_LENGTH": "not-a-number"}))
        invalid_status = inkdrop_auth.public_status(db, environ={"INKDROP_PASSWORD_MIN_LENGTH": "not-a-number"})
        require(not invalid_status["password_policy"]["configuration_valid"], "invalid policy must be visible without locking out status/login")
        require(invalid_status["password_policy"]["minimum_length"] == 8, "invalid policy must fail to the safe public default")

        dump = json.dumps(inkdrop_auth.public_status(db, environ={}), ensure_ascii=False)
        for secret in ("12345678", long_password, "an existing twelve character password"):
            require(secret not in dump, "password must never appear in public status")

    print(json.dumps({"ok": True, "password_policy": "passed"}, indent=2))


if __name__ == "__main__":
    main()
