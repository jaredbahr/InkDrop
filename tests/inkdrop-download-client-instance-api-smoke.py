#!/usr/bin/env python3
"""HTTP/auth contract smoke for instance-scoped download-client APIs."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from unittest import mock

import inkdrop_auth
import inkdrop_auth_contracts
import inkdrop_download_client_api
import inkdrop_state
import inkdrop_web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def http_json(opener, method, url, payload=None, headers=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method, headers={
        "Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {}),
    })
    try:
        response = opener.open(request, timeout=8)
    except urllib.error.HTTPError as exc:
        response = exc
    raw = response.read()
    return response.status, response.headers, json.loads(raw.decode("utf-8")) if raw else {}


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-client-api-") as tmp:
        root, db = Path(tmp), Path(tmp) / "state.sqlite3"
        inkdrop_state.ensure_schema(db)
        old_db = inkdrop_web.INKDROP_STATE_DB
        old_env = {key: os.environ.get(key) for key in ("INKDROP_AUTH_MODE", "INKDROP_AUTH_REQUIRED", "INKDROP_DOWNLOAD_CLIENT_SECRET_DIR")}
        os.environ.update({"INKDROP_AUTH_MODE": "built_in", "INKDROP_AUTH_REQUIRED": "1", "INKDROP_DOWNLOAD_CLIENT_SECRET_DIR": str(root / "secrets")})
        inkdrop_web.INKDROP_STATE_DB = db
        inkdrop_web.clear_inkdrop_auth_status_cache()
        calls = []

        def fake_test(settings, _http):
            calls.append("draft" if str(settings.get("name") or "").startswith("Draft ") else settings.get("id"))
            return {"ok": True, "client": settings["client_type"], "detail": "reachable"}

        server = inkdrop_web.InkDropThreadingHTTPServer(("127.0.0.1", 0), inkdrop_web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            anonymous = urllib.request.build_opener()
            code, _, payload = http_json(anonymous, "POST", base + "/api/auth/bootstrap", {
                "username": "admin",
                "password": "a correct horse battery staple",
                "credential": inkdrop_auth.current_bootstrap_credential(db),
            })
            require(code == 200 and payload["ok"], "admin bootstrap failed")
            code, _, _ = http_json(anonymous, "GET", base + "/api/download-clients")
            require(code == 401, "instance registry must require authentication")

            jar = CookieJar()
            browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            code, _, payload = http_json(browser, "POST", base + "/api/auth/login", {"username": "admin", "password": "a correct horse battery staple"})
            require(code == 200, "admin login failed")
            cookies = {cookie.name: cookie.value for cookie in jar}
            csrf = cookies["inkdrop_csrf"]
            good_headers = {"X-InkDrop-CSRF": csrf, "Origin": base}

            no_csrf, _, payload = http_json(browser, "POST", base + "/api/download-clients", {"name": "No CSRF", "client_type": "qbittorrent"})
            require(no_csrf == 403 and payload["error"] == "csrf_header_required", "cookie mutation bypassed CSRF")
            bad_origin, _, payload = http_json(browser, "POST", base + "/api/download-clients", {"name": "Bad Origin", "client_type": "qbittorrent"}, {"X-InkDrop-CSRF": csrf, "Origin": "https://evil.invalid"})
            require(bad_origin == 403 and payload["error"] == "origin_validation_failed", "cookie mutation bypassed origin validation")
            oversized, _, _ = http_json(browser, "POST", base + "/api/download-clients/test", {"padding": "x" * (inkdrop_download_client_api.MAX_BODY_BYTES + 1)}, good_headers)
            require(oversized == 400, "oversized client body was accepted")

            class RedirectResponse:
                status_code = 302
            class RedirectTransport:
                def request(self, _method, _url, **_kwargs):
                    return RedirectResponse()
            safe = inkdrop_download_client_api.SafeSession()
            safe._session = RedirectTransport()
            try:
                safe.get("http://client.invalid/redirect", timeout=999)
            except RuntimeError as exc:
                require("redirect" in str(exc), "redirect rejection was not explicit")
            else:
                raise AssertionError("download-client test transport followed a redirect")

            first_body = {"id": "qbit-one", "name": "qBit One", "client_type": "qbittorrent", "enabled": True, "base_url": "http://qbit-one:8080", "username": "inkdrop", "settings": {"verify_tls": False}, "secrets": {"password": "never-echo-this"}}
            code, headers, created = http_json(browser, "POST", base + "/api/download-clients", first_body, good_headers)
            require(code == 201 and created["instance"]["revision"] == 1, "instance create failed")
            require("no-store" in headers.get("Cache-Control", "") and headers.get("ETag") == 'W/"1"', "create cache/revision headers missing")
            require("never-echo-this" not in json.dumps(created) and "dcs1_" not in json.dumps(created), "create leaked secret or reference")
            second_body = {**first_body, "id": "qbit-two", "name": "qBit Two", "base_url": "http://qbit-two:8080", "secrets": {"password": "second-secret"}, "provider_mappings": [{"provider_id": "prowlarr-two", "protocol": "torrent"}]}
            require(http_json(browser, "POST", base + "/api/download-clients", second_body, good_headers)[0] == 201, "multiple same-type instances must be allowed")

            code, _, listing = http_json(browser, "GET", base + "/api/download-clients")
            require(code == 200 and len(listing["instances"]) == 2, "instance list failed")
            require(set(listing["registry"]["addable"]) == {"qbittorrent", "sabnzbd", "slskd", "transmission", "deluge", "nzbget", "utorrent", "rtorrent"}, "registry must advertise exactly 8 implemented adapters")
            require("never-echo-this" not in json.dumps(listing) and "dcs1_" not in json.dumps(listing), "list leaked secret material")

            conflict, _, _ = http_json(browser, "PATCH", base + "/api/download-clients/qbit-one", {"priority": 2}, {**good_headers, "If-Match": 'W/"9"'})
            require(conflict == 409, "stale revision must conflict")
            code, headers, updated = http_json(browser, "PATCH", base + "/api/download-clients/qbit-one", {"priority": 2, "secrets": {"password": ""}}, {**good_headers, "If-Match": 'W/"1"'})
            require(code == 200 and updated["instance"]["revision"] == 2 and updated["instance"]["secret_fields"]["password"]["configured"], "blank secret must preserve configured value")
            require(updated["instance"]["settings"].get("verify_tls") is False, "omitted settings must survive unrelated PATCH")

            before_draft = len(inkdrop_state.download_client_instances(db)["instances"])
            with mock.patch.dict(inkdrop_download_client_api.TESTERS, {"qbittorrent": fake_test}):
                code, _, draft = http_json(browser, "POST", base + "/api/download-clients/test", {"client_type": "qbittorrent", "base_url": "http://draft:8080", "username": "inkdrop", "secrets": {"password": "draft-secret"}}, good_headers)
                require(code == 200 and draft["persisted"] is False and draft["result"]["ok"], "unsaved draft test failed")
                require(len(inkdrop_state.download_client_instances(db)["instances"]) == before_draft, "draft test persisted data")
                code, _, tested = http_json(browser, "POST", base + "/api/download-clients/qbit-one/test", {}, good_headers)
                require(code == 200 and tested["status"]["instance_id"] == "qbit-one", "saved instance test failed")
                code, _, status = http_json(browser, "GET", base + "/api/download-clients/qbit-one/status")
                require(code == 200 and status["status"]["instance_id"] == "qbit-one" and status["cached"], "instance-keyed cached status failed")
                disabled_code, _, _ = http_json(browser, "POST", base + "/api/download-clients", {"id": "qbit-disabled", "name": "qBit Disabled", "client_type": "qbittorrent", "enabled": False}, good_headers)
                require(disabled_code == 201, "disabled incomplete instance should remain storable")
                code, _, accepted = http_json(browser, "POST", base + "/api/download-clients/test-all", {}, good_headers)
                require(code == 202 and accepted["run_id"].startswith("dctr_"), "test-all must return 202 run id")
                run = None
                for _ in range(80):
                    code, _, run_payload = http_json(browser, "GET", base + "/api/download-clients/test-runs/" + accepted["run_id"])
                    run = run_payload.get("run")
                    if run and run.get("state") == "completed":
                        break
                    time.sleep(0.025)
                require(code == 200 and run and run["state"] == "completed" and len(run["results"]) == 3, "test-all run did not complete once per instance")
                require(any(row.get("instance_id") == "qbit-disabled" and row.get("skipped") for row in run["results"]), "test-all did not skip disabled instance")
            require(calls.count("qbit-one") == 2 and calls.count("qbit-two") == 1 and calls.count("draft") == 1, "test calls were duplicated or omitted")

            code, _, _ = http_json(browser, "DELETE", base + "/api/download-clients/qbit-two", None, {**good_headers, "If-Match": 'W/"1"'})
            require(code == 409, "delete must conflict while provider mappings reference an instance")
            code, _, second_updated = http_json(browser, "PATCH", base + "/api/download-clients/qbit-two", {"provider_mappings": []}, {**good_headers, "If-Match": 'W/"1"'})
            require(code == 200 and second_updated["instance"]["revision"] == 2, "provider mapping removal failed")
            with inkdrop_state.connect(db) as con:
                con.execute("insert into download_tasks(id,state,download_client_instance_id) values(?,?,?)", ("active-qbit-two", "downloading", "qbit-two"))
                con.commit()
            code, _, _ = http_json(browser, "DELETE", base + "/api/download-clients/qbit-two", None, {**good_headers, "If-Match": 'W/"2"'})
            require(code == 409, "delete must conflict while active tasks reference an instance")
            with inkdrop_state.connect(db) as con:
                con.execute("update download_tasks set state='completed' where id='active-qbit-two'")
                con.commit()

            # Legacy endpoints remain routed through their established contracts.
            code, _, legacy_registry = http_json(browser, "GET", base + "/api/download-clients/registry")
            require(code == 200 and "download_clients" in legacy_registry, "legacy registry endpoint broke")
            with mock.patch.object(inkdrop_web, "download_client_status_payload", return_value={"ok": True, "download_client": {"client_id": "qbittorrent"}}):
                code, _, legacy_status = http_json(browser, "GET", base + "/api/download-clients/qbittorrent/status")
                require(code == 200 and legacy_status["download_client"]["client_id"] == "qbittorrent", "legacy type status endpoint broke")

            # Clearing requires disable; deletion remains revision-guarded and soft.
            code, _, cleared = http_json(browser, "PATCH", base + "/api/download-clients/qbit-one", {"enabled": False, "clear_secret_fields": ["password"]}, {**good_headers, "If-Match": 'W/"2"'})
            require(code == 200 and cleared["instance"]["secret_fields"] == {}, "explicit secret clear failed")
            code, _, deleted = http_json(browser, "DELETE", base + "/api/download-clients/qbit-one", None, {**good_headers, "If-Match": 'W/"3"'})
            require(code == 200 and deleted["instance"]["deleted_at"], "soft delete failed")
            require(http_json(browser, "GET", base + "/api/download-clients/qbit-one")[0] == 404, "soft-deleted instance remained visible")

            for method, route in (("POST", "/api/download-clients"), ("POST", "/api/download-clients/test-all"), ("PATCH", "/api/download-clients/example"), ("DELETE", "/api/download-clients/example")):
                policy = inkdrop_auth_contracts.mutation_route_policy(route, method)
                require(policy and policy["admin_only"] and policy["csrf_required_for_cookie_sessions"], f"missing explicit admin auth policy for {method} {route}")
            print(json.dumps({"ok": True, "instances": 3, "implemented_types": 8, "test_calls": calls}, sort_keys=True))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            inkdrop_web.INKDROP_STATE_DB = old_db
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            inkdrop_web.clear_inkdrop_auth_status_cache()


if __name__ == "__main__":
    main()
