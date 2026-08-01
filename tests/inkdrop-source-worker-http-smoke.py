#!/usr/bin/env python3
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import inkdrop_source_worker_adapters as adapters
import inkdrop_source_worker_http as source_http


def fail(message):
    print(f"SOURCE_WORKER_HTTP_FAIL: {message}")
    raise SystemExit(1)


def ok(message):
    print(f"SOURCE_WORKER_HTTP_OK: {message}")


def assert_equal(actual, expected, message):
    if actual != expected:
        fail(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value, message):
    if not value:
        fail(message)


def assert_false(value, message):
    if value:
        fail(message)


class FakeResponse:
    def __init__(self, body, *, status=200, headers=None, final_url="", redirect_count=0, peer_ip="93.184.216.34"):
        self._body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self._offset = 0
        self.status = status
        self.headers = dict(headers or {})
        self.closed = False
        self.url = final_url
        self.redirect_count = redirect_count
        self.peer_ip = peer_ip

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True


def capture_opener(captured, body, headers=None):
    def _opener(prepared, timeout=None):
        captured["url"] = prepared.full_url
        captured["headers"] = dict(prepared.header_items())
        captured["timeout"] = timeout
        return FakeResponse(body, headers=headers or {"Content-Type": "application/json"})

    return _opener


def capture_body_opener(captured, body, headers=None):
    def _opener(prepared, timeout=None):
        captured["url"] = prepared.full_url
        captured["headers"] = dict(prepared.header_items())
        captured["timeout"] = timeout
        captured["method"] = prepared.get_method()
        captured["body"] = prepared.data
        return FakeResponse(body, headers=headers or {"Content-Type": "application/json"})

    return _opener


def expect_http_error(reason, func, message):
    try:
        func()
    except source_http.SourceHttpError as exc:
        assert_equal(exc.reason, reason, message)
        assert_false("live-secret" in json.dumps(exc.safe_request), "safe request redacts secrets")
    else:
        fail(f"{message}: expected SourceHttpError")


def hostile_redirect_cap_smoke():
    state = {"hits": [], "headers": [], "destination_headers": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"].append(self.path)
            state["headers"].append(dict(self.headers.items()))
            if self.path == "/escaped":
                state["destination_headers"].append(dict(self.headers.items()))
            target = self.path.split("/redirect/", 1)[-1]
            if target == "localhost":
                location = f"http://localhost:{self.server.server_port}/escaped"
            else:
                location = f"http://{target}/escaped"
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for escaped_host in ("localhost", "comics-all.example", "florenfile.example", "arbitrary.example"):
            before = len(state["hits"])
            request = {
                "method": "GET",
                "url": f"http://127.0.0.1:{server.server_port}/redirect/{escaped_host}",
                "headers": {"Authorization": "Bearer must-not-leak"},
                "allowed_hosts": ["127.0.0.1"],
            }
            expect_http_error(
                "disallowed_host",
                lambda request=request, escaped_host=escaped_host: source_http.source_http_get(
                    request,
                    allowed_hosts=["127.0.0.1", escaped_host],
                    allow_request_hosts=True,
                ),
                f"request route cap blocks configured-only redirect host {escaped_host}",
            )
            assert_equal(len(state["hits"]), before + 1, f"{escaped_host} redirect is blocked before a second request")
        assert_false(any(path == "/escaped" for path in state["hits"]), "configured-only localhost receives no redirected request")
        assert_false(
            any(headers.get("Authorization") == "Bearer must-not-leak" for headers in state["destination_headers"]),
            "redirect destination receives no authorization header",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    hostile_redirect_cap_smoke()
    request = {
        "request_id": "prowlarr_search",
        "method": "GET",
        "url": "http://prowlarr.local/api/v1/search",
        "params": {"query": "Hunter x Hunter", "limit": 5},
        "headers": {"Accept": "application/json", "X-Api-Key": "<secret_ref:prowlarr_api_key>"},
        "purpose": "search_prowlarr",
    }
    captured = {}
    result = source_http.source_http_get(
        request,
        secret_values={"prowlarr_api_key": "live-secret"},
        allowed_hosts=["prowlarr.local"],
        opener=capture_opener(
            captured,
            json.dumps([{"title": "Hunter x Hunter 001.cbz", "seeders": 12}]),
        ),
        timeout_seconds=7,
    )
    assert_equal(result["status_code"], 200, "status code")
    assert_equal(result["source_http_contract_version"], 2, "typed transport contract version")
    assert_equal(result["peer_ip"], "93.184.216.34", "actual connected peer evidence is returned")
    assert_equal(result["json"][0]["title"], "Hunter x Hunter 001.cbz", "JSON payload parsed")
    assert_equal(captured["headers"]["X-api-key"], "live-secret", "secret ref resolved for actual request")
    assert_true("query=Hunter+x+Hunter" in captured["url"], "query params encoded")
    assert_equal(captured["timeout"], 7.0, "timeout passed through")
    redacted = source_http.redacted_request(request)
    assert_equal(redacted["headers"]["X-Api-Key"], "<redacted>", "redacted request hides API key header")
    assert_false("live-secret" in json.dumps(redacted), "redacted request contains no raw secret")
    resolved = source_http.resolve_request_secrets(request, secret_values={"prowlarr_api_key": "live-secret"})
    assert_equal(resolved["headers"]["X-Api-Key"], "live-secret", "resolve helper returns real header only when requested")

    graphql_request = {
        "request_id": "suwayomi_fetch_chapter_pages",
        "method": "POST",
        "url": "http://127.0.0.1:4568/api/graphql",
        "json": {"query": "mutation { fetchChapterPages(input: { chapterId: 1 }) { pages } }"},
        "headers": {"Accept": "application/json"},
        "purpose": "fetch_suwayomi_chapter_pages",
    }
    graphql_captured = {}
    graphql_result = source_http.source_http_get(
        graphql_request,
        allowed_hosts=["127.0.0.1"],
        opener=capture_body_opener(
            graphql_captured,
            json.dumps({"data": {"fetchChapterPages": {"pages": []}}}),
        ),
    )
    assert_equal(graphql_result["json"]["data"]["fetchChapterPages"]["pages"], [], "JSON POST response parsed")
    assert_equal(graphql_captured["method"], "POST", "JSON POST method used")
    assert_true(b"fetchChapterPages" in graphql_captured["body"], "JSON POST body sent")
    assert_equal(graphql_captured["headers"]["Content-type"], "application/json", "JSON POST content type set")
    assert_equal(source_http.redacted_request(graphql_request)["body"], "<present>", "JSON POST body redacted from safe request")

    torznab_request = {
        "request_id": "torznab_search",
        "method": "GET",
        "url": "http://jackett.local/api/v2.0/indexers/example/results/torznab",
        "params": {"t": "search", "q": "Hunter x Hunter", "cat": "7030"},
        "secret_params": {"apikey": "<secret_ref:torznab_api_key>"},
        "headers": {"Accept": "application/rss+xml"},
        "purpose": "search_torznab",
    }
    torznab_captured = {}
    torznab_result = source_http.source_http_get(
        torznab_request,
        secret_values={"torznab_api_key": "live-secret"},
        allowed_hosts=["jackett.local"],
        opener=capture_opener(
            torznab_captured,
            "<rss><channel></channel></rss>",
            headers={"Content-Type": "application/rss+xml"},
        ),
    )
    assert_equal(torznab_result["text"], "<rss><channel></channel></rss>", "Torznab XML payload parsed as text")
    assert_true("apikey=live-secret" in torznab_captured["url"], "secret query param resolved only for actual request")
    torznab_redacted = source_http.redacted_request(torznab_request)
    assert_equal(torznab_redacted["params"]["apikey"], "<redacted>", "Torznab secret param redacted")
    assert_false("live-secret" in json.dumps(torznab_redacted), "Torznab redacted request contains no raw secret")
    torznab_resolved = source_http.resolve_request_secrets(
        torznab_request,
        secret_values={"torznab_api_key": "live-secret"},
    )
    assert_equal(torznab_resolved["params"]["apikey"], "live-secret", "resolve helper returns Torznab API key only when requested")

    expect_http_error(
        "unresolved_secret_ref",
        lambda: source_http.source_http_get(request, opener=capture_opener({}, "[]")),
        "unresolved secret rejected",
    )
    expect_http_error(
        "secret_ref_in_query",
        lambda: source_http.source_http_get(
            {**request, "params": {"apikey": "<secret_ref:prowlarr_api_key>"}},
            secret_values={"prowlarr_api_key": "live-secret"},
            opener=capture_opener({}, "[]"),
        ),
        "secret ref in query rejected",
    )
    expect_http_error(
        "secret_param_conflict",
        lambda: source_http.source_http_get(
            {**torznab_request, "params": {"apikey": "visible"}},
            secret_values={"torznab_api_key": "live-secret"},
            opener=capture_opener({}, "[]"),
        ),
        "secret param conflict rejected",
    )
    expect_http_error(
        "disallowed_scheme",
        lambda: source_http.source_http_get(
            {**request, "url": "file:///tmp/source.json"},
            secret_values={"prowlarr_api_key": "live-secret"},
            opener=capture_opener({}, "[]"),
        ),
        "disallowed scheme rejected",
    )
    expect_http_error(
        "url_userinfo_not_allowed",
        lambda: source_http.source_http_get(
            {**request, "url": "https://user:password@prowlarr.example/api/v1/search"},
            secret_values={"prowlarr_api_key": "live-secret"},
            allowed_hosts=["prowlarr.example"],
            opener=capture_opener({}, "[]"),
        ),
        "URL userinfo rejected before fetch",
    )
    expect_http_error(
        "disallowed_host",
        lambda: source_http.source_http_get(
            request,
            secret_values={"prowlarr_api_key": "live-secret"},
            allowed_hosts=["other.local"],
            opener=capture_opener({}, "[]"),
        ),
        "disallowed host rejected",
    )
    expect_http_error(
        "disallowed_host",
        lambda: source_http.source_http_get(
            {"method": "GET", "url": "https://pixeldrain.com/api/file/fixture?download"},
            allowed_hosts=["pixeldrain.com"],
            opener=lambda prepared, timeout=None: FakeResponse(
                b"payload",
                headers={"Content-Type": "application/octet-stream"},
                final_url="https://florenfile.example/payload",
                redirect_count=1,
            ),
        ),
        "final redirect host escape rejected",
    )
    expect_http_error(
        "redirect_limit_exceeded",
        lambda: source_http.source_http_get(
            {"method": "GET", "url": "https://pixeldrain.com/api/file/fixture?download"},
            allowed_hosts=["pixeldrain.com"],
            max_redirects=1,
            opener=lambda prepared, timeout=None: FakeResponse(
                b"payload",
                headers={"Content-Type": "application/octet-stream"},
                final_url="https://pixeldrain.com/api/file/fixture?download",
                redirect_count=2,
            ),
        ),
        "redirect count is bounded",
    )
    redirect_handler = source_http._ConstrainedRedirectHandler(
        request={"url": "https://getcomics.org/feed/"},
        allowed_schemes=("https",),
        allowed_hosts=("getcomics.org",),
        max_redirects=2,
    )
    expect_http_error(
        "disallowed_host",
        lambda: redirect_handler.redirect_request(None, None, 302, "Found", {}, "https://comics-all.example/post"),
        "each redirect hop is host-confined before follow",
    )
    request_allowed_host = {
        "request_id": "indexer_pack_detail_0",
        "method": "GET",
        "url": "https://sidecar.example/nfo/weekly-pack",
        "allowed_hosts": ["sidecar.example"],
        "purpose": "fetch_indexer_pack_detail_download_metadata_sidecar",
    }
    expect_http_error(
        "disallowed_host",
        lambda: source_http.source_http_get(
            request_allowed_host,
            allowed_hosts=["prowlarr.local"],
            opener=capture_opener({}, "Example Comics 001.cbz", headers={"Content-Type": "text/plain"}),
        ),
        "request-level hosts are ignored by default",
    )
    request_host_result = source_http.source_http_get(
        request_allowed_host,
        allowed_hosts=["prowlarr.local", "sidecar.example"],
        allow_request_hosts=True,
        opener=capture_opener({}, "Example Comics 001.cbz", headers={"Content-Type": "text/plain"}),
    )
    assert_equal(request_host_result["text"], "Example Comics 001.cbz", "opt-in request-level host allowed")
    expect_http_error(
        "response_too_large",
        lambda: source_http.source_http_get(
            {**request, "headers": {"X-Api-Key": "<secret_ref:prowlarr_api_key>"}},
            secret_values={"prowlarr_api_key": "live-secret"},
            opener=capture_opener({}, b"x" * 2048, headers={"Content-Type": "text/plain"}),
            max_bytes=1024,
        ),
        "oversize response rejected",
    )
    truncated_result = source_http.source_http_get(
        {
            **request,
            "headers": {"X-Api-Key": "<secret_ref:prowlarr_api_key>"},
            "allow_truncated": True,
            "max_bytes": 1024,
        },
        secret_values={"prowlarr_api_key": "live-secret"},
        opener=capture_opener({}, b"x" * 2048, headers={"Content-Type": "text/plain"}),
        max_bytes=4096,
    )
    assert_equal(len(truncated_result["text"]), 1024, "explicit truncation caps response body")
    assert_equal(truncated_result["headers"]["X-InkDrop-Truncated"], "1", "explicit truncation marks response")

    text_result = source_http.source_http_get(
        {
            "method": "GET",
            "url": "https://standardebooks.org/feeds/opds",
            "headers": {"Accept": "application/atom+xml"},
        },
        opener=capture_opener({}, "<feed></feed>", headers={"Content-Type": "application/atom+xml; charset=utf-8"}),
    )
    assert_equal(text_result["text"], "<feed></feed>", "OPDS/XML payload stays text")

    guarded = source_http.make_source_http_get(
        secret_values={},
        opener=capture_opener({}, "[]"),
    )
    prowlarr_row = {
        "provider_id": "prowlarr_nyaa",
        "base_url": "http://prowlarr.local",
        "secret_ref": "prowlarr_api_key",
        "policy": {},
    }
    prowlarr_plan = {"adapter_family": "prowlarr_indexer"}
    fetch = adapters.fetch_payloads(
        prowlarr_row,
        prowlarr_plan,
        {"series_title": "Hunter x Hunter"},
        http_get=guarded,
    )
    assert_false(fetch["ok"], "adapter fetch handles HTTP client failure")
    assert_equal(fetch["reason"], "http_request_failed", "adapter fetch failure reason")
    assert_true("unresolved_secret_ref" in fetch["error"], "adapter fetch preserves safe error reason")
    assert_false("live-secret" in json.dumps(fetch), "adapter fetch error does not leak secret")

    ok("source worker HTTP client resolves settings secret refs and guards source requests")


if __name__ == "__main__":
    main()
