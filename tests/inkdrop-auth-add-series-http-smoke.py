#!/usr/bin/env python3
"""Authenticated Add Series and retry HTTP contract regression."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import inkdrop_auth
import inkdrop_state

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    class _RequestsException(Exception):
        pass
    requests_stub.exceptions = types.SimpleNamespace(RequestException=_RequestsException, Timeout=_RequestsException, ConnectionError=_RequestsException, HTTPError=_RequestsException)
    sys.modules["requests"] = requests_stub

import inkdrop_web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def http_json(opener, method, url, payload=None, headers=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if body is not None else {}),
        **(headers or {}),
    })
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    raw = response.read()
    return response.status, json.loads(raw.decode("utf-8")) if raw else {}


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-add-series-http-", ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "state.sqlite3"
        inkdrop_state.ensure_schema(db)
        original_db = inkdrop_web.INKDROP_STATE_DB
        original_add = inkdrop_web.add_comic_series
        original_retry = inkdrop_web.run_inkdrop_series_search
        original_env = {key: os.environ.get(key) for key in ("INKDROP_AUTH_MODE", "INKDROP_AUTH_REQUIRED")}
        additions = set()
        retries = set()

        def fake_add(payload):
            identity = f"comicvine:{int(payload.get('comicvineId') or 0)}"
            additions.add(identity)
            return {"ok": True, "series_id": identity, "series_count": len(additions), "wanted_count": 3}

        def fake_retry(payload):
            identity = str(payload.get("seriesId") or payload.get("series_id") or "")
            retries.add(identity)
            return {"ok": True, "series_id": identity, "request_count": len(retries)}

        os.environ.update({"INKDROP_AUTH_MODE": "built_in", "INKDROP_AUTH_REQUIRED": "1"})
        inkdrop_web.INKDROP_STATE_DB = db
        inkdrop_web.add_comic_series = fake_add
        inkdrop_web.run_inkdrop_series_search = fake_retry
        inkdrop_web.clear_inkdrop_auth_status_cache()
        server = inkdrop_web.InkDropThreadingHTTPServer(("127.0.0.1", 0), inkdrop_web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        anonymous = urllib.request.build_opener()
        try:
            code, _ = http_json(anonymous, "POST", base + "/api/auth/bootstrap", {"username": "admin", "password": "12345678"})
            require(code == 200, "bootstrap must accept the default eight-character policy")
            jar = CookieJar()
            browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            code, _ = http_json(browser, "POST", base + "/api/auth/login", {"username": "admin", "password": "12345678"})
            require(code == 200, "login must succeed")
            cookies = {cookie.name: cookie.value for cookie in jar}
            session_cookie = cookies[inkdrop_auth.SESSION_COOKIE_NAME]
            csrf = cookies[inkdrop_auth.CSRF_COOKIE_NAME]

            code, status = http_json(browser, "GET", base + "/api/auth/status")
            auth = status["auth"]
            require(code == 200 and auth["session_authenticated"], "status must safely report authenticated session state")
            require(auth["csrf_cookie_name"] == inkdrop_auth.CSRF_COOKIE_NAME, "status must publish CSRF cookie name")
            require(auth["csrf_header_name"] == inkdrop_auth.CSRF_HEADER_NAME, "status must publish CSRF header name")
            require(auth["csrf_required_for_cookie_mutations"], "status must publish cookie mutation requirement")
            require(auth["session_expires_at"], "status may publish session expiry but not credentials")
            require("session_token" not in auth and "csrf_token" not in auth, "status must not expose reusable credentials")

            payload = {"comicvineId": 4207, "name": "The Sandman", "autoGrab": True}
            code, result = http_json(browser, "POST", base + "/api/comicvine/add", payload)
            require(code == 403 and result["error"] == "csrf_header_required", "missing CSRF header must fail")

            raw = urllib.request.build_opener()
            code, result = http_json(raw, "POST", base + "/api/comicvine/add", payload, {
                "Cookie": f"{inkdrop_auth.SESSION_COOKIE_NAME}={session_cookie}",
                inkdrop_auth.CSRF_HEADER_NAME: csrf,
                "Origin": base,
            })
            require(code == 403 and result["error"] == "csrf_cookie_required", "missing CSRF cookie must fail")
            code, result = http_json(raw, "POST", base + "/api/comicvine/add", payload, {
                "Cookie": f"{inkdrop_auth.SESSION_COOKIE_NAME}={session_cookie}; {inkdrop_auth.CSRF_COOKIE_NAME}=wrong",
                inkdrop_auth.CSRF_HEADER_NAME: csrf,
                "Origin": base,
            })
            require(code == 403 and result["error"] == "csrf_token_mismatch", "wrong CSRF cookie must fail")

            code, result = http_json(browser, "POST", base + "/api/comicvine/add", payload, {
                inkdrop_auth.CSRF_HEADER_NAME: csrf,
            })
            require(code == 403 and result["error"] == "origin_header_required", "missing Origin must fail for cookie mutations")

            good_headers = {inkdrop_auth.CSRF_HEADER_NAME: csrf, "Origin": base}
            code, result = http_json(browser, "POST", base + "/api/comicvine/add", payload, good_headers)
            require(code == 200 and result["result"]["series_count"] == 1, "valid cookie mutation must succeed")
            code, result = http_json(browser, "POST", base + "/api/comicvine/add", payload, good_headers)
            require(code == 200 and result["result"]["series_count"] == 1, "double submission must remain idempotent")
            retry_payload = {"seriesId": "comicvine:4207"}
            for _ in range(2):
                code, result = http_json(browser, "POST", base + "/api/inkdrop-state/series/run", retry_payload, good_headers)
                require(code == 200 and result["result"]["request_count"] == 1, "repeated retry must be safe")

            read_key = inkdrop_auth.create_api_key(db, "read", scopes=["read"])["api_key"]["key"]
            acquisition_key = inkdrop_auth.create_api_key(db, "acquisition", scopes=["acquisition"])["api_key"]["key"]
            code, result = http_json(raw, "POST", base + "/api/comicvine/add", payload, {"X-InkDrop-API-Key": read_key})
            require(code == 403 and result["error"] == "insufficient_scope", "read-only key must fail")
            code, result = http_json(raw, "POST", base + "/api/comicvine/add", payload, {"X-InkDrop-API-Key": acquisition_key})
            require(code == 200 and result["result"]["series_count"] == 1, "acquisition key must work without browser CSRF")

            with sqlite3.connect(inkdrop_auth.auth_store_path(db)) as con:
                con.execute("update auth_sessions set expires_at=?", (time.time() - 5,))
            inkdrop_web.clear_inkdrop_auth_status_cache()
            code, result = http_json(browser, "POST", base + "/api/comicvine/add", payload, good_headers)
            require(code == 401 and result["error"] == "authentication_required", "expired session must fail")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            inkdrop_web.INKDROP_STATE_DB = original_db
            inkdrop_web.add_comic_series = original_add
            inkdrop_web.run_inkdrop_series_search = original_retry
            inkdrop_web.clear_inkdrop_auth_status_cache()
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    print(json.dumps({"ok": True, "add_series_http_contract": "passed"}, indent=2))


if __name__ == "__main__":
    main()
