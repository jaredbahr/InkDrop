#!/usr/bin/env python3
"""Direct source cleanup and certification smoke."""

import io
import json
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import inkdrop_direct_downloader as downloader
from core import inkdrop_source_catalog as catalog
from core import inkdrop_source_providers as source_providers
from core import inkdrop_source_worker_adapters as source_adapters
from core import inkdrop_source_worker_http as source_http


def fail(message):
    print(f"DIRECT_SOURCE_CERTIFICATION_FAIL: {message}")
    raise SystemExit(1)


def ok(message):
    print(f"DIRECT_SOURCE_CERTIFICATION_OK: {message}")


def assert_equal(actual, expected, message):
    if actual != expected:
        fail(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value, message):
    if not value:
        fail(message)


def assert_false(value, message):
    if value:
        fail(message)


def by_id(rows):
    return {row.get("id"): row for row in rows or []}


def zip_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("issue.txt", "InkDrop direct source certification fixture\n")
    return stream.getvalue()


def http_response(body, *, content_type="application/zip", status_code=200, final_url="https://example.test/book.cbz", declared_size=None):
    return {
        "status_code": status_code,
        "final_url": final_url,
        "redirect_count": 0,
        "headers": {
            "content-type": content_type,
            "content-length": str(len(body) if declared_size is None else declared_size),
            "set-cookie": "session=must-not-persist",
            "location": "https://secret.example/private-file",
        },
        "body": body,
    }


def assert_direct_redirect_caps():
    state = {"hits": [], "destination_headers": [], "destination_body_sent": 0}
    body = zip_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"].append(self.path)
            if self.path == "/escaped":
                state["destination_headers"].append(dict(self.headers.items()))
                state["destination_body_sent"] += len(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            target = self.path.split("/redirect/", 1)[-1]
            location = (
                f"http://localhost:{self.server.server_port}/escaped"
                if target == "localhost"
                else f"http://{target}/escaped"
            )
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="inkdrop-direct-hostile-redirect-") as tmp:
            for escaped_host in ("localhost", "comics-all.example", "florenfile.example", "arbitrary.example"):
                before = len(state["hits"])
                client = source_http.make_source_http_get(
                    allowed_hosts=["127.0.0.1", escaped_host],
                    allow_request_hosts=True,
                )
                result = downloader.download_direct_file(
                    url=f"http://127.0.0.1:{server.server_port}/redirect/{escaped_host}",
                    provider_id="rss_getcomics",
                    download_task_id=f"hostile-{escaped_host}",
                    staging_root=Path(tmp) / "staging",
                    final_filename=f"{escaped_host}.cbz",
                    allowed_extensions=[".cbz"],
                    allowed_content_types=["application/zip"],
                    allowed_hosts=["127.0.0.1"],
                    max_redirects=2,
                    headers={"Authorization": "Bearer must-not-leak"},
                    http_get=client,
                )
                assert_equal(result.get("reason"), "http_request_failed", f"{escaped_host} direct redirect is blocked")
                assert_equal(result.get("error_reason"), "disallowed_host", f"{escaped_host} fails at the host cap")
                assert_equal(len(state["hits"]), before + 1, f"{escaped_host} receives no second-hop request")
            assert_false(state["destination_headers"], "direct redirect destination receives no headers")
            assert_equal(state["destination_body_sent"], 0, "direct redirect destination body is never requested or read")
            assert_false(any((Path(tmp) / "staging").glob("*.cbz")), "hostile redirects stage no files")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def assert_direct_download_gates():
    with tempfile.TemporaryDirectory(prefix="inkdrop-direct-source-cert-") as tmp:
        staging = Path(tmp) / "staging"
        body = zip_bytes()
        result = downloader.download_direct_file(
            url="https://example.test/book.cbz",
            provider_id="generic_safe_http_direct_download",
            download_task_id="fixture-ok",
            staging_root=staging,
            final_filename="book.cbz",
            allowed_extensions=[".cbz"],
            allowed_content_types=["application/zip"],
            max_bytes=len(body) + 10,
            http_get=lambda request: http_response(body),
        )
        assert_true(result.get("ok"), "valid CBZ direct download stages successfully")
        assert_equal(result.get("state"), "import_ready", "valid CBZ is marked import-ready")
        assert_equal(result.get("archive_validation"), "passed", "valid CBZ records archive validation")
        metadata_path = Path(result["metadata_path"])
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert_equal(metadata.get("archive_validation"), "passed", "sidecar records archive validation")
        assert_true(metadata.get("url_hash"), "sidecar records redacted URL evidence")
        assert_false(metadata.get("source_metadata", {}).get("download_url"), "sidecar does not require raw URL evidence")
        assert_false("set-cookie" in metadata.get("response_headers", {}), "sidecar drops cookie headers")
        assert_false("location" in metadata.get("response_headers", {}), "sidecar drops redirect locations")

        wrong_host = downloader.download_direct_file(
            url="https://florenfile.example/book.cbz",
            provider_id="rss_getcomics",
            download_task_id="fixture-wrong-host",
            staging_root=staging,
            final_filename="wrong-host.cbz",
            allowed_extensions=[".cbz"],
            allowed_hosts=["pixeldrain.com"],
            http_get=lambda request: http_response(body),
        )
        assert_equal(wrong_host.get("reason"), "download_host_not_allowed", "unapproved transport host is blocked")

        escaped_redirect = downloader.download_direct_file(
            url="https://pixeldrain.com/api/file/fixture?download",
            provider_id="rss_getcomics",
            download_task_id="fixture-final-host",
            staging_root=staging,
            final_filename="final-host.cbz",
            allowed_extensions=[".cbz"],
            allowed_hosts=["pixeldrain.com"],
            http_get=lambda request: http_response(body, final_url="https://comics-all.example/book.cbz"),
        )
        assert_equal(escaped_redirect.get("reason"), "final_host_not_allowed", "redirect host escape is blocked")

        size_mismatch = downloader.download_direct_file(
            url="https://pixeldrain.com/api/file/fixture?download",
            provider_id="rss_getcomics",
            download_task_id="fixture-size-mismatch",
            staging_root=staging,
            final_filename="size-mismatch.cbz",
            allowed_extensions=[".cbz"],
            allowed_hosts=["pixeldrain.com"],
            http_get=lambda request: http_response(body, final_url=request["url"], declared_size=len(body) + 1),
        )
        assert_equal(size_mismatch.get("reason"), "content_length_mismatch", "declared and actual size mismatch is blocked")

        invalid = downloader.download_direct_file(
            url="https://example.test/bad.cbz",
            provider_id="generic_safe_http_direct_download",
            download_task_id="fixture-bad-archive",
            staging_root=staging,
            final_filename="bad.cbz",
            allowed_extensions=[".cbz"],
            allowed_content_types=["application/zip"],
            max_bytes=1024,
            http_get=lambda request: http_response(b"not a zip archive"),
        )
        assert_false(invalid.get("ok"), "invalid archive is blocked")
        assert_equal(invalid.get("reason"), "archive_validation_failed", "invalid archive reason is explicit")
        assert_false((staging / "bad.cbz").exists(), "invalid archive is not promoted to final path")

        unsafe_url = downloader.download_direct_file(
            url="file:///tmp/book.cbz",
            provider_id="generic_safe_http_direct_download",
            download_task_id="fixture-unsafe-url",
            staging_root=staging,
            final_filename="unsafe.cbz",
            allowed_extensions=[".cbz"],
            http_get=lambda request: http_response(body),
        )
        assert_equal(unsafe_url.get("reason"), "download_url_not_safe_http", "non-HTTP URL is blocked before fetch")

        unsafe_path = downloader.download_direct_file(
            url="https://example.test/book.cbz",
            provider_id="generic_safe_http_direct_download",
            download_task_id="fixture-unsafe-path",
            staging_root=staging,
            local_path="../escape.cbz",
            allowed_extensions=[".cbz"],
            http_get=lambda request: http_response(body),
        )
        assert_equal(unsafe_path.get("reason"), "target_outside_staging_root", "path escape is blocked")

        html = downloader.download_direct_file(
            url="https://example.test/book.cbz",
            provider_id="generic_safe_http_direct_download",
            download_task_id="fixture-html",
            staging_root=staging,
            final_filename="html.cbz",
            allowed_extensions=[".cbz"],
            allowed_content_types=["application/zip"],
            http_get=lambda request: http_response(b"<!doctype html><title>login</title>", content_type="application/zip"),
        )
        assert_equal(html.get("reason"), "html_or_login_response", "HTML/login response is blocked")


def assert_pixeldrain_resolver_toggle_gates_getcomics():
    """Pixeldrain has its own enable/disable toggle under Download Clients.
    GetComics' entire transport is Pixeldrain, so disabling that toggle must
    make GetComics unable to produce any candidate -- not just fail to
    resolve one, since an empty transport_allowed_hosts otherwise means "no
    restriction" everywhere else in this function."""
    payload = {
        "source_url": "https://getcomics.org/example-toggle-001/",
        "detail_pages": [{
            "source_url": "https://getcomics.org/example-toggle-001/",
            "title": "Example Toggle 001",
            "text": '<a href="https://pixeldrain.com/u/toggle001">Download</a>',
        }],
        "probe_headers": {
            source_providers.url_hash("https://pixeldrain.com/api/file/toggle001?download"): {
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="Example Toggle 001.cbz"',
                "Content-Length": "204800",
            },
        },
        "probe_status": {source_providers.url_hash("https://pixeldrain.com/api/file/toggle001?download"): 200},
    }
    wanted_item = {"series_title": "Example Toggle", "issue_number": "1"}

    enabled_row = {
        "provider_id": "rss_getcomics",
        "provider_type": "direct_download",
        "source_kind": "rss_detail_probe_feed",
        "registry_state": "assist",
        "auto_search_allowed": True,
        "pixeldrain_resolver_enabled": True,
    }
    enabled_candidates = source_providers.direct_file_probe_candidates_from_payload(payload, enabled_row, wanted_item)
    assert_equal(len(enabled_candidates), 1, "GetComics resolves a Pixeldrain candidate while the resolver is enabled")

    disabled_row = dict(enabled_row)
    disabled_row["pixeldrain_resolver_enabled"] = False
    disabled_candidates = source_providers.direct_file_probe_candidates_from_payload(payload, disabled_row, wanted_item)
    assert_equal(len(disabled_candidates), 0, "disabling the Pixeldrain resolver leaves GetComics with no candidates, not an unrestricted transport")

    # No explicit registry_row value at all (e.g. a fresh install that has
    # never synced a "pixeldrain" provider_configs row) must behave like
    # today's always-on resolution, not silently go dark.
    default_row = {k: v for k, v in enabled_row.items() if k != "pixeldrain_resolver_enabled"}
    default_candidates = source_providers.direct_file_probe_candidates_from_payload(payload, default_row, wanted_item)
    assert_equal(len(default_candidates), 1, "an absent resolver-enabled flag defaults to enabled, matching pre-toggle behavior")


def assert_getcomics_dls_shortener_resolution():
    """GetComics wraps every mirror link -- including Pixeldrain -- behind a
    same-site /dls/ redirect shortener. The raw href never carries an
    approved transport host, so it must be resolved before the transport
    allowlist means anything. Modeled on a real GetComics post captured
    2026-08-08 (getcomics.org/other-comics/white-sky-5-2026/)."""
    detail_url = "https://getcomics.org/example-shortener-002/"
    dls_url = "https://getcomics.org/dls/token002"
    pixeldrain_share_url = "https://pixeldrain.com/u/short002"
    pixeldrain_api_url = "https://pixeldrain.com/api/file/short002?download"
    detail_html = (
        f'<div class="aio-button-center"><div class="aio-pulse">'
        f'<a rel="nofollow" href="{dls_url}" class="aio-orange" title="PIXELDRAIN">'
        f'<i class="glyphicons glyphicons-ok"></i>PIXELDRAIN</a></div></div>'
    )
    feed_xml = (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><item>'
        f"<title>Example Shortener #2</title><link>{detail_url}</link><guid>{detail_url}</guid>"
        "</item></channel></rss>"
    )
    row = {
        "provider_id": "rss_getcomics",
        "provider_type": "direct_download",
        "source_kind": "rss_detail_probe_feed",
        "registry_state": "assist",
        "auto_search_allowed": True,
        "policy": {
            "allowed_extensions": [".cbz", ".cbr", ".zip"],
            "shared_file_hosts": ["pixeldrain"],
            "transport_allowed_hosts": ["pixeldrain.com", "www.pixeldrain.com"],
            "feed_detail_allowed_hosts": ["getcomics.org", "www.getcomics.org"],
            "max_detail_pages": 5,
            "max_probe_links": 5,
            "probe_method": "HEAD",
            "max_redirects": 2,
        },
    }
    plan = {"adapter_family": "rss_detail_probe_feed", "adapter_id": "generic_rss_detail_probe_feed"}
    wanted_item = {"title": "Example Shortener #2", "series_title": "Example Shortener"}

    def http_get(request):
        url = request.get("url", "")
        if url.rstrip("/") == "https://getcomics.org/feed":
            return {"status_code": 200, "headers": {"content-type": "application/rss+xml"}, "text": feed_xml, "final_url": url}
        if url == detail_url:
            return {"status_code": 200, "headers": {"content-type": "text/html"}, "text": detail_html, "final_url": url}
        if url == dls_url:
            # Real observed GetComics behavior: a clean single-hop 302 to the
            # Pixeldrain share page, no captcha or JS interstitial.
            return {"status_code": 200, "headers": {"content-type": "text/html"}, "text": "", "final_url": pixeldrain_share_url}
        if url == pixeldrain_api_url:
            return {
                "status_code": 200,
                "headers": {
                    "content-type": "application/octet-stream",
                    "content-disposition": 'attachment; filename="Example Shortener 002.cbr"',
                    # A real comic-sized declaration -- the fixture zip body itself
                    # is never transmitted for a HEAD probe, only its headers are.
                    "content-length": str(41 * 1024 * 1024),
                },
                "text": "",
                "final_url": url,
            }
        raise AssertionError(f"unexpected probe URL: {url}")

    result = source_adapters.fetch_payloads(row, plan, wanted_item, http_get=http_get, limit=5)
    assert_true(result.get("ok"), "GetComics fetch with a dls-shortened Pixeldrain link succeeds")
    payload = (result.get("payloads") or [{}])[0]
    assert_equal(
        payload.get("resolved_redirect_urls", {}).get(source_providers.url_hash(dls_url)),
        pixeldrain_api_url,
        "the dls redirect resolves to the real Pixeldrain file endpoint, not the share page",
    )
    candidates = source_providers.direct_file_probe_candidates_from_payload(payload, row, wanted_item, limit=5)
    pixeldrain_candidates = [c for c in candidates if c.get("download_url") == pixeldrain_api_url]
    assert_equal(len(pixeldrain_candidates), 1, "the resolved candidate carries the real Pixeldrain file endpoint")
    headers = payload.get("probe_headers", {}).get(source_providers.url_hash(pixeldrain_api_url), {})
    verdict = source_providers.direct_artifact_verdict(pixeldrain_candidates[0], row, headers=headers)
    assert_equal(
        verdict.get("block_reasons"),
        ["auto_download_not_allowed"],
        "a resolved GetComics->Pixeldrain candidate clears every safety gate except the manual-review gate",
    )

    # A dls link that resolves OFF the approved transport (e.g. a non-Pixeldrain
    # mirror behind the same shortener) must still be rejected -- redirect
    # resolution must not become a way to widen the certified transport.
    hostile_dls_url = "https://getcomics.org/dls/hostile001"
    hostile_html = f'<a rel="nofollow" href="{hostile_dls_url}" class="aio-blue" title="MEGA"><i></i>MEGA</a>'
    hostile_feed_xml = feed_xml.replace(detail_url, "https://getcomics.org/example-hostile-001/")
    hostile_detail_url = "https://getcomics.org/example-hostile-001/"

    def hostile_http_get(request):
        url = request.get("url", "")
        if url.rstrip("/") == "https://getcomics.org/feed":
            return {"status_code": 200, "headers": {"content-type": "application/rss+xml"}, "text": hostile_feed_xml, "final_url": url}
        if url == hostile_detail_url:
            return {"status_code": 200, "headers": {"content-type": "text/html"}, "text": hostile_html, "final_url": url}
        if url == hostile_dls_url:
            return {"status_code": 200, "headers": {"content-type": "text/html"}, "text": "", "final_url": "https://mega.example/file.cbz"}
        raise AssertionError(f"unexpected hostile probe URL: {url}")

    hostile_result = source_adapters.fetch_payloads(row, plan, {"title": "Example Hostile"}, http_get=hostile_http_get, limit=5)
    hostile_payload = (hostile_result.get("payloads") or [{}])[0]
    assert_false(
        hostile_payload.get("resolved_redirect_urls"),
        "a dls link resolving off Pixeldrain is never recorded as a resolved transport URL",
    )
    hostile_candidates = source_providers.direct_file_probe_candidates_from_payload(
        hostile_payload, row, {"title": "Example Hostile"}, limit=5
    )
    assert_false(
        any("mega.example" in (c.get("download_url") or "") for c in hostile_candidates),
        "a dls link resolving to an unapproved host never becomes a candidate",
    )


def assert_rar_content_type_accepts_vnd_rar():
    """Confirmed live 2026-08-08 against a real Pixeldrain-hosted .cbr file
    (via getcomics.org/other-comics/white-sky-5-2026/): Pixeldrain serves it
    with Content-Type: application/vnd.rar, not application/vnd.comicbook-rar
    or application/octet-stream. application/vnd.rar is the IANA-registered
    RAR media type; a real .cbr candidate must not be rejected for carrying
    it."""
    candidate = {
        "download_url": "https://pixeldrain.com/api/file/liveverify?download",
        "extension": ".cbr",
        "provider_id": "rss_getcomics",
    }
    headers = {
        "Content-Type": "application/vnd.rar",
        "Content-Disposition": 'attachment; filename="White Sky 005 (2026).cbr"',
        "Content-Length": "41673397",
    }
    row = {"provider_id": "rss_getcomics", "registry_state": "assist", "auto_search_allowed": True}
    verdict = source_providers.direct_artifact_verdict(candidate, row, headers=headers)
    assert_false(
        "content_type_mismatch" in (verdict.get("block_reasons") or []),
        f"application/vnd.rar must be an accepted .cbr content type: {verdict.get('block_reasons')}",
    )


def main():
    assert_direct_redirect_caps()
    assert_pixeldrain_resolver_toggle_gates_getcomics()
    assert_getcomics_dls_shortener_resolution()
    assert_rar_content_type_accepts_vnd_rar()
    providers = by_id(catalog.provider_candidates())
    seed_providers = {row["id"]: row for row in catalog.settings_seed_payload()["providers"]}
    assert_equal(set(catalog.PRODUCT_DIRECT_SOURCE_IDS), {
        "generic_rss_direct_feed",
        "rss_getcomics",
        "suwayomi_managed_folder",
        "generic_safe_http_direct_download",
        "local_manual_inbox",
    }, "direct product source set is explicit and small")
    assert_true(set(catalog.PRODUCT_DIRECT_SOURCE_IDS).issubset(seed_providers), "all concrete direct sources are product settings")
    assert_false(set(catalog.VAGUE_DIRECT_SOURCE_BUCKET_IDS) & set(seed_providers), "vague direct buckets are not product settings")

    expected_status = {
        "generic_rss_direct_feed": "Beta",
        "rss_getcomics": "Experimental",
        "suwayomi_managed_folder": "Beta",
        "generic_safe_http_direct_download": "Beta",
        "local_manual_inbox": "Beta",
    }
    common_gate_groups = {
        "configuration",
        "discovery",
        "candidate_identity",
        "evidence_history",
    }
    import_gate_groups = {
        "deduplication",
        "archive_validation",
        "retry",
        "import_handoff",
        "end_to_end_fixture",
    }
    for provider_id, status in expected_status.items():
        settings = seed_providers[provider_id]["settings"]
        policy = settings.get("policy") or {}
        certification = settings.get("source_certification") or {}
        gates = set(certification.get("required_gates") or [])
        assert_equal(settings.get("certification_status"), status, f"{provider_id} has truthful certification status")
        assert_true(common_gate_groups.issubset(gates), f"{provider_id} records common certification gates")
        if provider_id != "rss_getcomics":
            assert_true(import_gate_groups.issubset(gates), f"{provider_id} records import/download certification gates")
        assert_false(policy.get("requires_account") is True, f"{provider_id} does not require account automation")
        if provider_id != "rss_getcomics":
            assert_false(policy.get("requires_browser") is True, f"{provider_id} does not require browser automation")
        forbidden_policy = " ".join(str(policy.get(key) or "") for key in ("direct_url_policy", "rights_gate"))
        for forbidden in ("bypass", "circumvent", "paywall"):
            assert_false(forbidden in forbidden_policy.lower(), f"{provider_id} policy does not implement {forbidden}")

    getcomics_cert = seed_providers["rss_getcomics"]["settings"]["source_certification"]
    assert_equal(getcomics_cert.get("blocked_gates"), ["live_provider_acceptance"], "GetComics stays experimental pending live acceptance")
    getcomics = providers["rss_getcomics"]
    assert_equal(getcomics.get("source_kind"), "rss_detail_probe_feed", "GetComics uses detail plus header probe discovery")
    assert_equal(getcomics.get("integration_class"), "DirectFileProbeProvider", "GetComics uses probe provider integration")
    assert_equal(getcomics.get("policy", {}).get("shared_file_hosts"), ["pixeldrain"], "Pixeldrain is the only GetComics transport")
    assert_false(getcomics.get("auto_download_allowed"), "GetComics remains disabled for automatic downloads")
    assert_true(getcomics.get("policy", {}).get("requires_manual_confirm"), "GetComics keeps manual confirmation gate")
    hostile_row = {
        "provider_id": "rss_getcomics",
        "base_url": "https://comics-all.example/feed",
        "policy": {
            "feed_detail_allowed_hosts": ["comics-all.example"],
            "transport_allowed_hosts": ["florenfile.example"],
        },
    }
    assert_equal(
        source_adapters._rss_discovery_allowed_hosts(hostile_row),
        ["getcomics.org", "www.getcomics.org"],
        "GetComics discovery hosts cannot be widened by configuration",
    )
    assert_equal(
        source_adapters._direct_transport_allowed_hosts(hostile_row),
        ["pixeldrain.com", "www.pixeldrain.com"],
        "GetComics transport hosts cannot be widened by configuration",
    )

    hostile_probe_policy = dict(getcomics.get("policy", {}))
    hostile_probe_policy["shared_file_hosts"] = ["pixeldrain", "comics-all.example", "florenfile.example"]
    hostile_probe_policy["transport_allowed_hosts"] = ["pixeldrain.com", "comics-all.example", "florenfile.example"]
    probe_row = {
        "provider_id": "rss_getcomics",
        "provider_type": "direct_download",
        "source_kind": "rss_detail_probe_feed",
        "registry_state": "ready",
        "auto_search_allowed": True,
        "auto_download_allowed": True,
        "policy": hostile_probe_policy,
    }
    probe_payload = {
        "source_url": "https://getcomics.org/example-book-001/",
        "detail_pages": [{
            "source_url": "https://getcomics.org/example-book-001/",
            "title": "Example Book 001",
            "text": """<a href="https://pixeldrain.com/u/cert001">Download</a>
            <a href="https://comics-all.example/file.cbz">Comics-All</a>
            <a href="https://florenfile.example/file.cbz">FlorenFile</a>""",
        }],
        "probe_headers": {
            source_providers.url_hash("https://pixeldrain.com/api/file/cert001?download"): {
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="Example Book 001.cbz"',
                "Content-Length": "204800",
            },
        },
        "probe_status": {source_providers.url_hash("https://pixeldrain.com/api/file/cert001?download"): 503},
    }
    candidates = source_providers.direct_file_probe_candidates_from_payload(
        probe_payload,
        probe_row,
        {"series_title": "Example Book", "issue_number": "1"},
    )
    assert_equal(len(candidates), 1, "only approved Pixeldrain transport becomes a candidate")
    failed_probe = source_providers.direct_artifact_verdict(candidates[0], probe_row)
    assert_equal(failed_probe.get("review_reason"), "probe_status_not_success", "non-2xx Pixeldrain probe is blocked")
    candidate_json = json.dumps(candidates)
    assert_false("comics-all" in candidate_json.lower(), "Comics-All is not an approved transport")
    assert_false("florenfile" in candidate_json.lower(), "FlorenFile is not an approved transport")

    assert_equal(
        providers["generic_safe_http_direct_download"]["policy"].get("direct_url_policy"),
        "explicit_http_file_url_header_verified_only",
        "safe HTTP source is concrete direct URL/header verified",
    )
    assert_equal(
        providers["local_manual_inbox"]["policy"].get("direct_url_policy"),
        "local_filesystem_only_no_remote_fetch",
        "manual inbox is local-only intake",
    )

    assert_direct_download_gates()
    ok("concrete direct sources are certified separately and vague buckets stay out of product settings")


if __name__ == "__main__":
    main()
