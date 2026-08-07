#!/usr/bin/env python3
"""HTTP contract smoke for InkDrop auth cookies, CSRF, routes, and scopes."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from unittest import mock

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


def http_json(opener, method, url, payload=None, headers=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})},
    )
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    raw = response.read()
    return response.status, response.headers, json.loads(raw.decode("utf-8")) if raw else {}


def http_raw_json(opener, method, url, body, headers=None):
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})},
    )
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    raw = response.read()
    return response.status, response.headers, json.loads(raw.decode("utf-8")) if raw else {}


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-auth-http-") as tmp:
        db = Path(tmp) / "state.sqlite3"
        inkdrop_state.ensure_schema(db)
        original_db = inkdrop_web.INKDROP_STATE_DB
        env_keys = {
            "INKDROP_AUTH_MODE": os.environ.get("INKDROP_AUTH_MODE"),
            "INKDROP_AUTH_REQUIRED": os.environ.get("INKDROP_AUTH_REQUIRED"),
            "INKDROP_BACKUP_DIR": os.environ.get("INKDROP_BACKUP_DIR"),
        }
        os.environ["INKDROP_AUTH_MODE"] = "built_in"
        os.environ["INKDROP_AUTH_REQUIRED"] = "1"
        os.environ["INKDROP_BACKUP_DIR"] = str(Path(tmp) / "backups")
        inkdrop_web.INKDROP_STATE_DB = db
        inkdrop_web.INKDROP_AUTH_STATUS_CACHE.update({"ts": 0.0, "status": None, "key": None})
        server = inkdrop_web.InkDropThreadingHTTPServer(("127.0.0.1", 0), inkdrop_web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            anonymous = urllib.request.build_opener()
            status_code, status_headers, status_payload = http_json(anonymous, "GET", base + "/api/auth/status")
            require(status_code == 200 and status_payload["auth"]["built_in_auth"]["bootstrap_required"], "auth status must be public")
            require(status_headers.get("X-Frame-Options") == "DENY", "security headers must protect JSON responses")
            require("frame-ancestors 'none'" in status_headers.get("Content-Security-Policy", ""), "CSP frame boundary is required")

            legacy_status_code, _, legacy_status_payload = http_json(anonymous, "GET", base + "/api/inkdrop-auth/status")
            require(legacy_status_code == 200 and legacy_status_payload == status_payload, "legacy status alias must use the canonical auth service")

            # An unclaimed install exposes nothing but its own setup. This
            # check previously required the opposite -- anonymous API access
            # before an administrator exists -- which is precisely how the
            # takeover window went unnoticed.
            pre_bootstrap_code, _, pre_bootstrap_payload = http_json(anonymous, "GET", base + "/api/inkdrop-settings")
            require(
                pre_bootstrap_code == 403 and pre_bootstrap_payload.get("error") == "setup_not_complete",
                f"an unclaimed install served an application API anonymously: {pre_bootstrap_code} {pre_bootstrap_payload}",
            )

            # First-run setup takes no setup code: whoever
            # reaches it first creates the administrator. This assertion used to
            # be its exact opposite -- that an uncredentialed attempt is refused
            # 403 -- and it passed while the browser flow was broken, because
            # the setup form never had a field to send a code from. The check
            # that still matters is the one below: the route closes for good
            # once an administrator exists.
            bootstrap_code, _, bootstrap_payload = http_json(anonymous, "POST", base + "/api/auth/bootstrap", {"username": "admin", "password": "a correct horse battery staple"})
            require(bootstrap_code == 200 and bootstrap_payload["ok"], f"first-run setup was refused: {bootstrap_code} {bootstrap_payload}")
            blocked_code, _, _ = http_json(anonymous, "GET", base + "/api/inkdrop-settings")
            require(blocked_code == 401, "application APIs must require auth immediately after bootstrap")
            closed_code, _, _ = http_json(anonymous, "POST", base + "/api/auth/bootstrap", {"username": "other", "password": "another correct horse password"})
            require(closed_code in {401, 409}, "bootstrap must close after first admin")

            compatibility_unauth_code, _, _ = http_json(anonymous, "POST", base + "/api/inkdrop-auth/api-keys/create", {"name": "Bypass", "scopes": ["read"]})
            require(compatibility_unauth_code == 401, "legacy API-key alias must not bypass authentication")

            jar = CookieJar()
            browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            login_code, login_headers, payload = http_json(browser, "POST", base + "/api/auth/login", {"username": "admin", "password": "a correct horse battery staple"})
            require(login_code == 200 and payload["session"]["cookie_authenticated"], "login must create cookie session")
            require("token" not in payload["session"] and "csrf_token" not in payload["session"], "login JSON must not expose reusable session credentials")
            set_cookie = ",".join(login_headers.get_all("Set-Cookie") or [])
            require("HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie, "session cookie must be HttpOnly and SameSite")
            cookies = {cookie.name: cookie.value for cookie in jar}
            require(cookies.get("inkdrop_session") and cookies.get("inkdrop_csrf"), "session and CSRF cookies must be issued")

            session_code, _, session_payload = http_json(browser, "GET", base + "/api/auth/session")
            require(session_code == 200 and session_payload["authenticated"], "session endpoint must validate current login")

            no_csrf_code, _, no_csrf_payload = http_json(browser, "POST", base + "/api/auth/api-keys", {"name": "No CSRF", "scopes": ["read"]})
            require(no_csrf_code == 403 and no_csrf_payload["error"] == "csrf_header_required", "cookie mutation without CSRF header must fail clearly")
            legacy_no_csrf_code, _, legacy_no_csrf_payload = http_json(browser, "POST", base + "/api/inkdrop-auth/api-keys/create", {"name": "Legacy no CSRF", "scopes": ["read"]})
            require(legacy_no_csrf_code == 403 and legacy_no_csrf_payload["error"] == "csrf_header_required", "legacy mutation alias must share CSRF enforcement")
            csrf = cookies["inkdrop_csrf"]
            backup_no_csrf_code, _, backup_no_csrf_payload = http_json(browser, "POST", base + "/api/inkdrop-settings/backup/export", {})
            require(backup_no_csrf_code == 403 and backup_no_csrf_payload["error"] == "csrf_header_required", "settings export must require CSRF for cookie admin")
            backup_code, backup_headers, backup_payload = http_json(browser, "POST", base + "/api/inkdrop-settings/backup/export", {}, {"X-InkDrop-CSRF": csrf, "Origin": base})
            require(backup_code == 200 and backup_payload["document"]["product"] == "InkDrop", "admin settings export failed")
            require("no-store" in (backup_headers.get("Cache-Control") or ""), "portable settings export must never be cached")
            expected_disposition = f'attachment; filename="{backup_payload["filename"]}"'
            require(backup_headers.get("Content-Disposition") == expected_disposition, "portable settings export attachment filename must match the sanitized stable payload name")
            require(backup_payload["filename"].startswith("inkdrop-settings-") and backup_payload["filename"].endswith(".json") and all(character.isalnum() or character in ".-" for character in backup_payload["filename"]), "portable settings filename is not safely sanitized")
            require(backup_headers.get("X-Content-Type-Options") == "nosniff", "portable settings export must retain nosniff")
            strict_headers = {"X-InkDrop-CSRF": csrf, "Origin": base}
            for hostile_body in (
                b'{"document_text":"{}","document_text":"{}"}',
                b'{"document_text":"{}","metadata":{"__proto__":{}}}',
            ):
                hostile_code, _, hostile_payload = http_raw_json(browser, "POST", base + "/api/inkdrop-settings/backup/preview", hostile_body, strict_headers)
                require(hostile_code == 400 and hostile_payload.get("error"), "strict outer settings envelope accepted duplicate/reserved keys")
            oversized_body = b'{"document_text":"' + (b"x" * (1024 * 1024 + 65536)) + b'"}'
            oversized_code, _, oversized_payload = http_raw_json(browser, "POST", base + "/api/inkdrop-settings/backup/preview", oversized_body, strict_headers)
            require(oversized_code == 400 and oversized_payload.get("error"), "oversized settings endpoint envelope was accepted")
            with inkdrop_state.connect_read(db) as con:
                nonfinite_audits_before = con.execute("select count(*) from history_events where event_type='settings_restore'").fetchone()[0]
                nonfinite_settings_before = [(row["key"], row["value_json"]) for row in con.execute("select key,value_json from app_settings order by key")]
            nonfinite_snapshots_before = set((Path(tmp) / "backups").glob("*.json"))
            for token in ("NaN", "Infinity", "-Infinity", "1e9999"):
                outer_body = f'{{"document_text":"{{}}","metadata":{token}}}'.encode("utf-8")
                outer_code, _, outer_payload = http_raw_json(browser, "POST", base + "/api/inkdrop-settings/backup/restore", outer_body, strict_headers)
                require(outer_code == 400 and outer_payload.get("error"), f"strict outer envelope accepted {token}")
                nested_body = json.dumps({"document_text": f'{{"schema_version":{token}}}'}).encode("utf-8")
                nested_code, _, nested_payload = http_raw_json(browser, "POST", base + "/api/inkdrop-settings/backup/restore", nested_body, strict_headers)
                require(nested_code == 400 and nested_payload.get("error"), f"nested portable document accepted {token}")
            with inkdrop_state.connect_read(db) as con:
                require(con.execute("select count(*) from history_events where event_type='settings_restore'").fetchone()[0] == nonfinite_audits_before, "non-finite HTTP input wrote a restore audit")
                require([(row["key"], row["value_json"]) for row in con.execute("select key,value_json from app_settings order by key")] == nonfinite_settings_before, "non-finite HTTP input mutated settings")
            require(set((Path(tmp) / "backups").glob("*.json")) == nonfinite_snapshots_before, "non-finite HTTP input created a snapshot")
            restore_text = json.dumps(backup_payload["document"])
            preview_code, _, preview_payload = http_json(browser, "POST", base + "/api/inkdrop-settings/backup/preview", {"document_text": restore_text}, {"X-InkDrop-CSRF": csrf, "Origin": base})
            require(preview_code == 200 and preview_payload["result"]["dry_run"], "admin restore preview failed")
            restore_code, _, restore_payload = http_json(browser, "POST", base + "/api/inkdrop-settings/backup/restore", {"document_text": restore_text}, {"X-InkDrop-CSRF": csrf, "Origin": base})
            require(restore_code == 200 and restore_payload["result"]["applied"], "admin merge restore failed")
            created_code, _, created_payload = http_json(browser, "POST", base + "/api/auth/api-keys", {"name": "HTTP reader", "description": "test", "scopes": ["read"]}, {"X-InkDrop-CSRF": csrf, "Origin": base})
            require(created_code == 200 and created_payload["api_key"]["key"].startswith("ik_"), "CSRF-protected API key creation must succeed")
            raw_key = created_payload["api_key"]["key"]

            worker_key = inkdrop_state.create_api_key(
                db,
                "Container worker",
                scopes=["read", "acquisition"],
            )["api_key"]["key"]
            worker_route = base + "/api/manual-source/import-detected"
            with mock.patch.object(
                inkdrop_web,
                "import_detected_manual_source",
                side_effect=lambda payload: {"accepted": True, "review_id": payload.get("review_id")},
            ):
                missing_worker_code, _, missing_worker_payload = http_json(
                    anonymous, "POST", worker_route, {"review_id": "worker:missing", "dryRun": True}
                )
                require(missing_worker_code == 401 and missing_worker_payload.get("error") == "authentication_required", "missing worker API key must receive 401")
                invalid_worker_code, _, invalid_worker_payload = http_json(
                    anonymous,
                    "POST",
                    worker_route,
                    {"review_id": "worker:invalid", "dryRun": True},
                    {"X-InkDrop-API-Key": "ik_invalid_worker_key"},
                )
                require(invalid_worker_code == 401 and invalid_worker_payload.get("error") == "authentication_required", "invalid worker API key must receive 401")
                valid_worker_code, _, valid_worker_payload = http_json(
                    anonymous,
                    "POST",
                    worker_route,
                    {"review_id": "worker:valid", "dryRun": True},
                    {"X-InkDrop-API-Key": worker_key},
                )
                require(valid_worker_code == 200 and (valid_worker_payload.get("result") or {}).get("accepted") is True, "read+acquisition worker API key must authorize the protected callback")

                from core import inkdrop_manual_source_autoresolve as worker_resolver
                with mock.patch.dict(
                    os.environ,
                    {
                        "INKDROP_MANUAL_SOURCE_IMPORT_API_URL": worker_route,
                        "INKDROP_WORKER_API_KEY": worker_key,
                    },
                    clear=False,
                ):
                    client_result = worker_resolver.post_import_detected(
                        worker_route,
                        "worker:client",
                        "/staging/worker-client.cbz",
                        dry_run=True,
                    )
                require((client_result.get("result") or {}).get("review_id") == "worker:client", "production worker client must authenticate through the supported API-key header")

            legacy_list_code, _, legacy_list_payload = http_json(browser, "GET", base + "/api/inkdrop-auth/api-keys")
            require(legacy_list_code == 200 and legacy_list_payload["api_keys"], "legacy API-key list alias must share authenticated service state")

            read_code, _, _ = http_json(anonymous, "GET", base + "/api/inkdrop-settings", headers={"X-InkDrop-API-Key": raw_key})
            require(read_code == 200, "read-scoped API key must access read endpoint")
            denied_code, _, denied_payload = http_json(anonymous, "POST", base + "/api/inkdrop-settings/app/update", {"key": "ui.test", "value": True}, {"X-InkDrop-API-Key": raw_key})
            require(denied_code == 403 and denied_payload["error"] == "insufficient_scope", "read key must not mutate settings")
            backup_denied_code, _, backup_denied_payload = http_json(anonymous, "POST", base + "/api/inkdrop-settings/backup/export", {}, {"X-InkDrop-API-Key": raw_key})
            require(backup_denied_code == 403 and backup_denied_payload["error"] in {"insufficient_scope", "admin_required"}, "read key must not export settings")

            bad_origin_code, _, bad_origin_payload = http_json(browser, "POST", base + "/api/auth/api-keys", {"name": "Bad origin", "scopes": ["read"]}, {"X-InkDrop-CSRF": csrf, "Origin": "https://attacker.invalid"})
            require(bad_origin_code == 403 and bad_origin_payload["error"] == "origin_validation_failed", "cross-origin cookie mutation must fail")

            logout_code, _, logout_payload = http_json(browser, "POST", base + "/api/auth/logout", {}, {"X-InkDrop-CSRF": csrf, "Origin": base})
            require(logout_code == 200 and logout_payload["revoked"] == 1, "logout must invalidate session")
            final_session_code, _, _ = http_json(browser, "GET", base + "/api/auth/session")
            require(final_session_code == 401, "logged-out session must no longer authenticate")

            legacy_jar = CookieJar()
            legacy_browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(legacy_jar))
            legacy_login_code, _, legacy_login_payload = http_json(legacy_browser, "POST", base + "/api/inkdrop-auth/login", {"username": "admin", "password": "a correct horse battery staple"})
            require(legacy_login_code == 200 and legacy_login_payload["session"]["cookie_authenticated"], "legacy login alias must use canonical session service")
            legacy_cookies = {cookie.name: cookie.value for cookie in legacy_jar}
            legacy_logout_code, _, legacy_logout_payload = http_json(legacy_browser, "POST", base + "/api/inkdrop-auth/logout", {}, {"X-InkDrop-CSRF": legacy_cookies["inkdrop_csrf"], "Origin": base})
            require(legacy_logout_code == 200 and legacy_logout_payload["revoked"] == 1, "legacy logout alias must revoke the canonical session")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            inkdrop_web.INKDROP_STATE_DB = original_db
            inkdrop_web.INKDROP_AUTH_STATUS_CACHE.update({"ts": 0.0, "status": None, "key": None})
            for key, value in env_keys.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    print(json.dumps({"ok": True, "auth_http_smoke": "passed"}, indent=2))


if __name__ == "__main__":
    main()
