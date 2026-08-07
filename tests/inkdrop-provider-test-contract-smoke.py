#!/usr/bin/env python3
"""Smoke test for InkDrop provider connection-test safety contracts."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

sys.modules.setdefault("requests", types.ModuleType("requests"))

from core import inkdrop_state
from core import inkdrop_web as web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fake_caps_get(request_payload, *, secret_values=None, allowed_hosts=None, timeout_seconds=8.0, max_bytes=512 * 1024):
    safe_request = web.inkdrop_source_worker_http.redacted_request(request_payload)
    require(safe_request["params"].get("apikey") == "<redacted>", "provider test request should redact API key")
    require("super-secret-indexer-key" not in json.dumps(safe_request), "redacted request must not expose raw secret")
    require(allowed_hosts == ["indexer.example"], "provider test should allow only the configured host")
    require(secret_values.get("generic_torznab_indexer_api_key") == "super-secret-indexer-key", "secret value should resolve by secret ref")
    return {
        "status_code": 200,
        "text": "<caps><limits max=\"100\" default=\"50\" /></caps>",
        "headers": {"Content-Type": "application/xml"},
        "elapsed_ms": 12,
        "json": None,
    }


def fake_missing_secret_get(request_payload, *, secret_values=None, allowed_hosts=None, timeout_seconds=8.0, max_bytes=512 * 1024):
    raise web.inkdrop_source_worker_http.SourceHttpError(
        "unresolved_secret_ref",
        "secret ref is not resolvable: generic_torznab_indexer_api_key",
        request=request_payload,
    )


def fake_suwayomi_get(request_payload, *, allowed_hosts=None, timeout_seconds=4.0, max_bytes=256 * 1024, **kwargs):
    require(request_payload["method"] == "GET", "Suwayomi connection test must be read-only GET")
    require(allowed_hosts == ["suwayomi.example"], "Suwayomi test must pin the configured host")
    require(timeout_seconds <= 4.0, "Suwayomi test timeout must stay bounded")
    url = request_payload["url"]
    if url.endswith("/settings/about"):
        require(max_bytes == 256 * 1024, "about inventory should keep the 256 KiB ceiling")
        payload = {"name": "Suwayomi", "version": "1.2.3"}
    elif url.endswith("/source/list"):
        require(max_bytes == 256 * 1024, "source inventory should keep the 256 KiB ceiling")
        payload = [{"id": "source-1", "name": "MangaDex"}]
    elif url.endswith("/extension/list"):
        require(max_bytes == 768 * 1024, "extension inventory should use the justified 768 KiB ceiling")
        payload = [{"pkgName": "eu.kanade.tachiyomi.extension.en.mangadex", "name": "MangaDex", "installed": True, "hasUpdate": True, "obsolete": False}]
    else:
        raise AssertionError(f"unexpected Suwayomi endpoint: {url}")
    return {"status_code": 200, "json": payload, "headers": {"Content-Type": "application/json"}, "elapsed_ms": 2}


def main():
    provider = {
        "id": "generic_torznab_indexer",
        "provider_type": "indexer",
        "display_name": "Generic Torznab",
        "enabled": True,
        "base_url": "https://indexer.example/api",
        "secret_ref": "generic_torznab_indexer_api_key",
        "settings": {
            "source_kind": "torznab_indexer",
            "base_url": "https://indexer.example/api",
            "api_key": "super-secret-indexer-key",
            "secret_fields": ["api_key"],
        },
    }

    health = web.indexer_provider_health(provider, http_get=fake_caps_get)
    encoded = json.dumps(health, sort_keys=True)
    require(health["ok"] is True, "healthy Torznab caps test should pass")
    require(health["state"] == "healthy", "successful caps test should be healthy")
    require(health["request"]["params"].get("apikey") == "<redacted>", "health request should redact API key")
    require("super-secret-indexer-key" not in encoded, "provider health result must not expose raw API key")

    missing_secret_provider = dict(provider)
    missing_secret_provider["settings"] = {
        "source_kind": "torznab_indexer",
        "base_url": "https://indexer.example/api",
        "secret_fields": ["api_key"],
    }
    missing = web.indexer_provider_health(missing_secret_provider, http_get=fake_missing_secret_get)
    require(missing["ok"] is False, "missing indexer API key should not pass")
    require(missing["state"] == "configuration_required", "missing indexer API key should be configuration-required")
    require(missing.get("reason") == "unresolved_secret_ref", "missing indexer API key should report unresolved secret ref")
    require("super-secret-indexer-key" not in json.dumps(missing, sort_keys=True), "missing-secret result must not expose raw API key")

    suwayomi = web.suwayomi_provider_health(
        {"id": "suwayomi", "enabled": True, "base_url": "https://suwayomi.example", "settings": {}},
        http_get=fake_suwayomi_get,
    )
    require(suwayomi["ok"] is True, "usable Suwayomi source and installed extension inventories should pass")
    require(suwayomi["state"] == "update" and suwayomi["update_count"] == 1, "extension updates should be classified")
    require(suwayomi["read_only"] is True, "Suwayomi connection test should declare read-only behavior")
    require([item["purpose"] for item in suwayomi["requests"]] == [
        "test_suwayomi_about_inventory", "test_suwayomi_sources_inventory", "test_suwayomi_extensions_inventory"
    ], "Suwayomi test should run only sequential inventory checks")
    require(not any(token in json.dumps(suwayomi).lower() for token in ("search_query", "page/list", "download", "import")), "connection test must not search, page-fetch, download, or import")

    large_inventory_calls = []
    def large_inventory_get(request_payload, *, max_bytes=None, **kwargs):
        large_inventory_calls.append((request_payload["request_id"], max_bytes))
        if request_payload["url"].endswith("/settings/about"):
            payload = {"name": "Suwayomi"}
        elif request_payload["url"].endswith("/source/list"):
            payload = [{"id": "source-1"}]
        else:
            require(max_bytes >= 503739, "current extension inventory must fit beneath the extension ceiling")
            payload = [{"name": "MangaDex", "installed": True, "obsolete": False, "fixture_bytes": 503739}]
        return {"status_code": 200, "json": payload, "headers": {"Content-Type": "application/json"}}

    large_inventory = web.suwayomi_provider_health(
        {"id": "suwayomi", "enabled": True, "base_url": "https://suwayomi.example", "settings": {}},
        http_get=large_inventory_get,
    )
    require(large_inventory["ok"] is True, ">256 KiB extension inventory within 768 KiB should succeed")
    require(large_inventory_calls == [
        ("suwayomi_about_inventory", 256 * 1024),
        ("suwayomi_sources_inventory", 256 * 1024),
        ("suwayomi_extensions_inventory", 768 * 1024),
    ], "each inventory endpoint must execute once with its own ceiling")

    over_ceiling_calls = []
    def over_ceiling_get(request_payload, *, max_bytes=None, **kwargs):
        over_ceiling_calls.append(request_payload["request_id"])
        if request_payload["url"].endswith("/extension/list"):
            raise web.inkdrop_source_worker_http.SourceHttpError("response_too_large", f"source response exceeded {max_bytes} bytes")
        payload = {"name": "Suwayomi"} if request_payload["url"].endswith("/settings/about") else [{"id": "source-1"}]
        return {"status_code": 200, "json": payload, "headers": {"Content-Type": "application/json"}}

    over_ceiling = web.suwayomi_provider_health(
        {"id": "suwayomi", "enabled": True, "base_url": "https://suwayomi.example", "settings": {}},
        http_get=over_ceiling_get,
    )
    require(over_ceiling["ok"] is False and over_ceiling["state"] == "unavailable", "over-ceiling inventory must fail safely")
    require(over_ceiling.get("reason") == "response_too_large", "over-ceiling failure should retain the safe reason")
    require(over_ceiling_calls == ["suwayomi_about_inventory", "suwayomi_sources_inventory", "suwayomi_extensions_inventory"], "over-ceiling failure must not retry or storm")

    malformed = web.suwayomi_provider_health(
        {"id": "suwayomi", "enabled": True, "base_url": "https://suwayomi.example", "settings": {}},
        http_get=lambda request_payload, **kwargs: {"status_code": 200, "text": "<html>challenge</html>", "headers": {"Content-Type": "text/html"}},
    )
    require(malformed["ok"] is False and malformed["state"] == "malformed", "HTML inventory responses must fail as malformed")

    def status_get(status):
        return lambda request_payload, **kwargs: {
            "status_code": status, "json": {"error": "redacted"}, "headers": {"Content-Type": "application/json"}
        }

    auth = web.suwayomi_provider_health(
        {"id": "suwayomi", "enabled": True, "base_url": "https://suwayomi.example", "settings": {}},
        http_get=status_get(401),
    )
    require(auth["ok"] is False and auth["state"] == "auth", "401 inventory responses must classify as auth")
    challenged = web.suwayomi_provider_health(
        {"id": "suwayomi", "enabled": True, "base_url": "https://suwayomi.example", "settings": {}},
        http_get=status_get(429),
    )
    require(challenged["ok"] is False and challenged["state"] == "challenge", "429 inventory responses must classify as challenge")

    def timeout_get(request_payload, **kwargs):
        raise TimeoutError("private-token-value timed out")

    timed_out = web.suwayomi_provider_health(
        {"id": "suwayomi", "enabled": True, "base_url": "https://suwayomi.example", "settings": {}},
        http_get=timeout_get,
    )
    require(timed_out["ok"] is False and timed_out["state"] == "timeout", "timeouts must classify distinctly")
    require("private-token-value" not in json.dumps(timed_out), "exception detail must be privacy safe")

    userinfo_calls = []
    userinfo = web.suwayomi_provider_health(
        {"id": "suwayomi", "enabled": True, "base_url": "http://alice:secret-value@suwayomi.example", "settings": {}},
        http_get=lambda request_payload, **kwargs: userinfo_calls.append(request_payload),
    )
    userinfo_encoded = json.dumps(userinfo, sort_keys=True)
    require(userinfo["ok"] is False and userinfo["state"] == "configuration_required", "URL userinfo must be rejected before probing")
    require(userinfo_calls == [], "URL userinfo rejection must happen before constructing or sending requests")
    require("alice" not in userinfo_encoded and "secret-value" not in userinfo_encoded, "userinfo must never reach results or provider-test history")

    for unsafe_url, private_value in (
        ("http://suwayomi.example?password=query-secret-value", "query-secret-value"),
        ("http://suwayomi.example?harmless_name=arbitrary-private-value", "arbitrary-private-value"),
        ("http://suwayomi.example#fragment-private-value", "fragment-private-value"),
    ):
        unsafe_calls = []
        unsafe = web.suwayomi_provider_health(
            {"id": "suwayomi", "enabled": True, "base_url": unsafe_url, "settings": {}},
            http_get=lambda request_payload, **kwargs: unsafe_calls.append(request_payload),
        )
        unsafe_encoded = json.dumps(unsafe, sort_keys=True)
        require(unsafe["ok"] is False and unsafe["state"] == "configuration_required", "Suwayomi URL queries/fragments must be rejected")
        require(unsafe_calls == [], "query/fragment rejection must happen before constructing or sending requests")
        require(private_value not in unsafe_encoded and unsafe_url not in unsafe_encoded, "query/fragment values must never reach result or history-safe payload")

    def obsolete_get(request_payload, **kwargs):
        if request_payload["url"].endswith("/settings/about"):
            payload = {"name": "Suwayomi"}
        elif request_payload["url"].endswith("/source/list"):
            payload = [{"id": "source-1"}]
        else:
            payload = [{"name": "Old source", "installed": True, "obsolete": True}]
        return {"status_code": 200, "json": payload, "headers": {"Content-Type": "application/json"}}

    obsolete = web.suwayomi_provider_health(
        {"id": "suwayomi", "enabled": True, "base_url": "https://suwayomi.example", "settings": {}},
        http_get=obsolete_get,
    )
    require(obsolete["ok"] is False and obsolete["state"] == "obsolete", "obsolete-only inventory must not be usable")

    original_provider_lookup = web.provider_for_direct_test
    original_suwayomi_health = web.suwayomi_provider_health
    try:
        calls = []
        web.provider_for_direct_test = lambda provider_id: {
            "id": "suwayomi", "display_name": "Suwayomi", "enabled": True,
            "health": {"state": "healthy", "detail": "cached state must not win"}, "activity": {},
        }
        web.suwayomi_provider_health = lambda provider: calls.append(provider) or {
            "ok": False, "state": "timeout", "label": "Suwayomi timed out", "detail": "bounded inventory timed out"
        }
        canonical = web.test_inkdrop_provider({"id": "suwayomi"})
        require(len(calls) == 1, "canonical provider test must invoke the live Suwayomi inventory probe")
        require(canonical["ok"] is False and canonical["health"]["state"] == "timeout", "cached provider health must not override live Suwayomi test truth")
    finally:
        web.provider_for_direct_test = original_provider_lookup
        web.suwayomi_provider_health = original_suwayomi_health

    with tempfile.TemporaryDirectory(prefix="inkdrop-provider-test-contract-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        inkdrop_state.ensure_schema(db_path)
        recorded = inkdrop_state.record_provider_test(
            db_path,
            {
                "ok": True,
                "provider_id": "generic_torznab_indexer",
                "display_name": "Generic Torznab",
                "health": health,
                "message": health["detail"],
            },
        )
        require(recorded["ok"] is True, "provider test history should record")
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = [dict(row) for row in con.execute("select * from history_events order by created_at desc limit 5")]
        encoded_rows = json.dumps(rows, sort_keys=True)
        require("provider_test" in encoded_rows, "provider test history event should be queryable")
        require("super-secret-indexer-key" not in encoded_rows, "provider test history must not expose raw API key")

    print(json.dumps({"ok": True, "provider_test_contract_smoke": "passed"}, indent=2))


if __name__ == "__main__":
    main()
