#!/usr/bin/env python3
"""Prowlarr base_url must always resolve under /api/v1.

Prowlarr's real API lives under /api/v1; a bare host:port base_url (no
suffix) 302-redirects unauthenticated requests to Prowlarr's Angular login
page, which serves HTML where JSON was expected. Every InkDrop call site
that builds a Prowlarr request URL from the stored base_url setting must
normalize it first -- confirmed against a live Prowlarr 2.5.2.5491 instance
where a bare base_url masked itself as a "bad_json" health result instead
of the real cause.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

try:
    import requests  # noqa: F401
except Exception:
    sys.modules["requests"] = types.SimpleNamespace()

from core import inkdrop_acquire as acquire
from core import inkdrop_state
from core import inkdrop_web as web

BASE_URL_FORMS = [
    "http://127.0.0.1:9696",
    "http://127.0.0.1:9696/",
    "http://127.0.0.1:9696/api/v1",
    "http://127.0.0.1:9696/api/v1/",
    "127.0.0.1:9696",
]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class FakeResponse:
    def __init__(self, *, status_code=200, json_payload=None, json_error=False, content=b"{}"):
        self.status_code = status_code
        self.content = content
        self._json_payload = json_payload
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_payload


def test_normalize_prowlarr_api_url():
    for base in BASE_URL_FORMS:
        normalized = acquire.normalize_prowlarr_api_url(base)
        require(normalized == "http://127.0.0.1:9696/api/v1", f"{base!r} did not normalize to /api/v1: {normalized!r}")
    require(acquire.normalize_prowlarr_api_url("") == "", "empty base_url must normalize to empty, not raise")
    require(acquire.normalize_prowlarr_api_url(None) == "", "missing base_url must normalize to empty, not raise")


def test_prowlarr_search_url():
    for base in BASE_URL_FORMS:
        url = acquire.prowlarr_search_url(base)
        require(url == "http://127.0.0.1:9696/api/v1/search", f"{base!r} produced wrong search URL: {url!r}")
    # A base_url a user already saved with a trailing /search (legacy shape)
    # must still resolve under /api/v1, not get /api/v1 appended after /search.
    legacy = acquire.prowlarr_search_url("http://127.0.0.1:9696/search")
    require(legacy == "http://127.0.0.1:9696/api/v1/search", f"legacy /search base_url mishandled: {legacy!r}")
    already_full = acquire.prowlarr_search_url("http://127.0.0.1:9696/api/v1/search")
    require(already_full == "http://127.0.0.1:9696/api/v1/search", f"already-normalized URL was mangled: {already_full!r}")
    try:
        acquire.prowlarr_search_url("")
    except RuntimeError as exc:
        require("not configured" in str(exc), f"unconfigured base_url raised the wrong message: {exc}")
    else:
        raise AssertionError("empty base_url must raise RuntimeError, not silently build a bad URL")


def test_prowlarr_search_resolves_and_handles_bad_json():
    settings = {
        "base_url": "http://127.0.0.1:9696",
        "search_url": "http://127.0.0.1:9696/api/v1/search",
        "categories": ["7030"],
        "timeout_seconds": 12.0,
        "source": "test",
        "apikey_query_param_fallback": False,
    }
    captured = {}

    class FakeHttp:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            return FakeResponse(json_payload=[{"protocol": "torrent", "seeders": 5, "size": 10}])

    with (
        mock.patch.object(acquire, "require_requests", return_value=FakeHttp),
        mock.patch.object(acquire, "load_prowlarr_key", return_value="fixture-key"),
        mock.patch.object(acquire, "load_prowlarr_settings", return_value=settings),
    ):
        results = acquire.prowlarr_search("Adventureman", "comics")
    require(captured["url"] == "http://127.0.0.1:9696/api/v1/search", f"prowlarr_search hit the wrong endpoint: {captured['url']!r}")
    require(len(results) == 1, "well-formed JSON response must still return results")

    class FakeHttpBadJson:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            return FakeResponse(json_error=True)

    with (
        mock.patch.object(acquire, "require_requests", return_value=FakeHttpBadJson),
        mock.patch.object(acquire, "load_prowlarr_key", return_value="fixture-key"),
        mock.patch.object(acquire, "load_prowlarr_settings", return_value=settings),
    ):
        try:
            acquire.prowlarr_search("Adventureman", "comics")
        except RuntimeError as exc:
            require("non-JSON" in str(exc), f"bad-JSON response raised the wrong message: {exc}")
        except ValueError:
            raise AssertionError("a bad-JSON Prowlarr response must degrade to RuntimeError, not crash with a raw ValueError")
        else:
            raise AssertionError("a bad-JSON Prowlarr response must raise, not silently return")


def fixture_provider(base_url):
    return {
        "id": "prowlarr",
        "provider_type": "indexer",
        "display_name": "Prowlarr",
        "enabled": True,
        "base_url": base_url,
        "secret_ref": "",
        "capabilities": ["search"],
        "settings": {"api_key": "fixture-key", "editable_fields": ["api_key"], "secret_fields": ["api_key"]},
        "source": "user",
    }


def test_prowlarr_api_health_normalizes_stored_base_url():
    for stored_base_url in ("http://127.0.0.1:9696", "http://127.0.0.1:9696/api/v1", "http://127.0.0.1:9696/api/v1/"):
        with tempfile.TemporaryDirectory(prefix="inkdrop-prowlarr-url-norm-") as temp:
            db = Path(temp) / inkdrop_state.STATE_DB_NAME
            inkdrop_state.sync_settings(db, providers=[fixture_provider(stored_base_url)])
            old_db = web.INKDROP_STATE_DB
            web.INKDROP_STATE_DB = db
            requested_urls = []
            requested_calls = []

            def fake_get(url, params=None, headers=None, timeout=None):
                requested_urls.append(url)
                requested_calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
                if url.endswith("/system/status"):
                    return FakeResponse(json_payload={"version": "2.5.2.5491", "appName": "Prowlarr"})
                if url.endswith("/indexer"):
                    return FakeResponse(json_payload=[{"id": 1, "name": "Fixture", "enable": True}])
                return FakeResponse(status_code=404)

            try:
                # load_acquire_module() re-execs inkdrop_acquire.py from disk each
                # call, so its own INKDROP_STATE_DB is recomputed from this env var
                # at that time -- it does not see web.INKDROP_STATE_DB above.
                with (
                    mock.patch.dict(os.environ, {"INKDROP_STATE_DIR": temp}, clear=False),
                    mock.patch.object(web.requests, "get", side_effect=fake_get),
                ):
                    health = web.prowlarr_api_health(timeout=1.0)
            finally:
                web.INKDROP_STATE_DB = old_db

            require(
                requested_urls == [
                    "http://127.0.0.1:9696/api/v1/system/status",
                    "http://127.0.0.1:9696/api/v1/indexer",
                    # The health probe also reads per-indexer availability now:
                    # "N indexers enabled" stays true while some of them are in
                    # failure backoff and answering nothing.
                    "http://127.0.0.1:9696/api/v1/indexerstatus",
                ],
                f"stored base_url {stored_base_url!r} produced wrong request URLs: {requested_urls}",
            )
            require(health["state"] == "healthy", f"stored base_url {stored_base_url!r} did not resolve to a healthy check: {health}")
            require(health["base_url"] == "http://127.0.0.1:9696/api/v1", f"health payload did not report the normalized base_url: {health.get('base_url')!r}")
            for call in requested_calls:
                require(call["params"].get("apikey") is None, f"{call['url']} must not send apikey as a query param by default: {call['params']!r}")
                require(call["headers"].get("X-Api-Key") == "fixture-key", f"{call['url']} missing X-Api-Key header: {call['headers']!r}")
                require(call["headers"].get("User-Agent") == "InkDropSourceWorker/1", f"{call['url']} missing InkDrop User-Agent: {call['headers']!r}")
                require(call["headers"].get("Accept") == "application/json", f"{call['url']} missing Accept: application/json: {call['headers']!r}")


def main():
    test_normalize_prowlarr_api_url()
    test_prowlarr_search_url()
    test_prowlarr_search_resolves_and_handles_bad_json()
    test_prowlarr_api_health_normalizes_stored_base_url()
    print("INKDROP_PROWLARR_API_URL_NORMALIZATION_OK")


if __name__ == "__main__":
    main()
