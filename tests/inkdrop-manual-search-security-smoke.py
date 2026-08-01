#!/usr/bin/env python3
"""Security, serialization, authorization, and retention smoke for Manual Search."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import inkdrop_auth
import inkdrop_manual_search
import inkdrop_manual_search_core as core
import inkdrop_state
import inkdrop_web


SECRET = "manual-search-secret-sentinel"
HEALTH_URL = "http://10.23.45.67/private/diagnostic"
PRIVATE_LOCATOR = "https://downloads.example.invalid/item/123"
CREDENTIAL_LOCATOR = f"https://private-user:{SECRET}@downloads.example.invalid/item/456"
TOKEN_LOCATOR = f"https://downloads.example.invalid/item/789?token={SECRET}&download=1"


def require(value, message):
    if not value:
        raise AssertionError(message)


def seed(db):
    inkdrop_state.ensure_schema(db)
    with inkdrop_state.connect(db) as con, con:
        con.execute(
            "insert into series(id,title,media_type,year,publisher,metadata_provider,metadata_id,source,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?)",
            ("series-sec", "Amulet", "comic", 2008, "Graphix", "comicvine", "amulet", "native", 1, 1, "{}"),
        )


def http_json(url, api_key, payload=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload or {"override_hard_limit": True}).encode("utf-8"),
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json", "X-InkDrop-API-Key": api_key},
    )
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    payload = json.loads(response.read().decode("utf-8"))
    return response.status, payload


def http_get_json(url, api_key):
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "X-InkDrop-API-Key": api_key},
    )
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    payload = json.loads(response.read().decode("utf-8"))
    return response.status, payload


def handler_override_contract(db):
    inkdrop_state.bootstrap_auth_user(
        db,
        "http-admin",
        "correct horse battery staple",
        credential=inkdrop_auth.current_bootstrap_credential(db),
    )
    acquisition_key = inkdrop_state.create_api_key(db, "Manual Search acquisition", scopes=["acquisition"])["api_key"]["key"]
    admin_key = inkdrop_state.create_api_key(db, "Manual Search admin", scopes=["admin"])["api_key"]["key"]
    original_db = inkdrop_web.INKDROP_STATE_DB
    original_grab = core.safe_grab_candidate
    original_env = {key: os.environ.get(key) for key in ("INKDROP_AUTH_MODE", "INKDROP_AUTH_REQUIRED")}
    calls = []
    candidate_ids = []

    def fake_grab(*args, **kwargs):
        calls.append(kwargs)
        candidate_ids.append(args[1] if len(args) > 1 else "")
        return {"ok": True, "grab_result": {"state": "queued_for_handoff"}}

    os.environ.update({"INKDROP_AUTH_MODE": "built_in", "INKDROP_AUTH_REQUIRED": "1"})
    inkdrop_web.INKDROP_STATE_DB = db
    core.safe_grab_candidate = fake_grab
    inkdrop_auth.clear_config_cache()
    inkdrop_web.clear_inkdrop_auth_status_cache()
    server = inkdrop_web.InkDropThreadingHTTPServer(("127.0.0.1", 0), inkdrop_web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/api/manual-search/candidates/http-fixture/grab"
    try:
        denied_code, denied = http_json(endpoint, acquisition_key)
        require(denied_code == 403 and denied.get("error") == "admin_required_for_hard_limit_override", "actual Handler must reject acquisition-only hard-limit override with HTTP 403")
        require(not calls, "denied Handler request must not reach Core grab")
        admin_code, admin = http_json(endpoint, admin_key)
        require(admin_code == 200 and admin.get("ok"), "actual Handler must allow an admin hard-limit override")
        require(calls and calls[-1].get("override_hard_limit") and calls[-1].get("hard_limit_override_authorized"), "Handler must pass explicit admin authorization to Core")
        force_payload = {"force_rejected": True, "confirm_rejected_risk": True}
        denied_force_code, denied_force = http_json(endpoint, acquisition_key, force_payload)
        require(denied_force_code == 403 and denied_force.get("error") == "admin_required_for_rejected_candidate_override", "actual Handler must reject acquisition-only force grab")
        admin_force_code, admin_force = http_json(endpoint, admin_key, force_payload)
        require(admin_force_code == 200 and admin_force.get("ok"), "actual Handler must allow an explicitly confirmed admin force grab")
        require(calls[-1].get("force_rejected") and calls[-1].get("force_rejected_authorized") and calls[-1].get("confirm_rejected_risk"), "Handler must pass force-grab authorization and confirmation to Core")

        created = core.create_search_run(
            db,
            series_id="series-sec",
            provider_selection=["prowlarr"],
            requested_by="encoded-route-smoke",
        )
        encoded_run = urllib.parse.quote(created["run_id"], safe="")
        base = f"http://127.0.0.1:{server.server_address[1]}/api/manual-search/runs/{encoded_run}"
        for suffix in ("", "/results", "/diagnostics"):
            status, payload = http_get_json(base + suffix, admin_key)
            require(status == 200 and payload.get("ok") and payload.get("run_id", created["run_id"]) == created["run_id"], f"encoded run route failed: {suffix}")
        cancel_status, cancel_payload = http_json(base + "/cancel", acquisition_key)
        require(cancel_status == 200 and cancel_payload.get("run_id") == created["run_id"], "encoded run ID must cancel the exact run")

        encoded_candidate = urllib.parse.quote("manual-candidate:fixture", safe="")
        encoded_endpoint = f"http://127.0.0.1:{server.server_address[1]}/api/manual-search/candidates/{encoded_candidate}/grab"
        encoded_status, encoded_payload = http_json(encoded_endpoint, admin_key)
        require(encoded_status == 200 and encoded_payload.get("ok"), "encoded candidate grab route must resolve")
        require(candidate_ids[-1] == "manual-candidate:fixture", "candidate route must decode exactly one path component")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        core.safe_grab_candidate = original_grab
        inkdrop_web.INKDROP_STATE_DB = original_db
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        inkdrop_auth.clear_config_cache()
        inkdrop_web.clear_inkdrop_auth_status_cache()


def provider_runner(*_args):
    return {
        "completed": True,
        "health": {
            "status": "degraded",
            "healthy": False,
            "current_error": f"failed {HEALTH_URL} C:\\Users\\private\\provider.log api_key={SECRET}",
            "api_key": SECRET,
            "base_url": HEALTH_URL,
            "private_path": "/home/private/provider.json",
            "nested": {"Authorization": SECRET},
        },
        "candidates": [{
            "title": "Amulet v09.cbz",
            "provider_id": "prowlarr",
            "protocol": "usenet",
            "candidate_safe": True,
            "accepted": True,
            "candidate_family_identity": "qa83-family-amulet-v09",
            "candidate_instance_identity": "qa83-instance-amulet-v09",
            "content_identity": "qa83-content-amulet-v09",
            "provider_candidate_identity": "qa83-provider-amulet-v09",
            "request_id": "qa83-request-1",
            "query_group": "strict-volume",
            "target_compatibility": {"status": "compatible", "positive_evidence": ["volume_9"]},
            "source_unit_evidence": {"parsed_volume_number": "9"},
            "parsed_unit_type": "volume",
            "parsed_volume_number": "9",
            "artifact_safe": True,
            "auto_grab_verdict": "safe",
            "quality_status": "accepted",
            "rejection_codes": [f"provider detail {PRIVATE_LOCATOR}?apikey={SECRET}"],
            "raw": {"Authorization": SECRET, "api_key": SECRET, "source_url": PRIVATE_LOCATOR},
            "_inkdrop_manual_attempt": {
                "provider_id": "prowlarr",
                "protocol": "usenet",
                "status": "sent",
                "download_url": PRIVATE_LOCATOR,
                "credential_url": CREDENTIAL_LOCATOR,
                "tokenized_url": TOKEN_LOCATOR,
                "headers": {"Authorization": SECRET},
                "api_key": SECRET,
                "raw": {"candidate": {"download_url": PRIVATE_LOCATOR, "cookie": SECRET}},
            },
        }],
    }


def run():
    with tempfile.TemporaryDirectory(prefix="inkdrop-manual-security-") as temp:
        db = Path(temp) / "state.sqlite3"
        seed(db)
        created = core.create_search_run(db, series_id="series-sec", provider_selection=["prowlarr"], requested_by="operator-private", now=100)
        require(created["ok"], "search run must be created")
        processed = core.process_search_run(db, created["run_id"], provider_runner, worker_id="worker-private", now=101)
        require(processed["ok"], "search run must complete")
        results = core.search_results(db, created["run_id"])
        diagnostics = core.search_diagnostics(db, created["run_id"])
        status = core.get_search_run(db, created["run_id"])
        serialized = json.dumps({"results": results, "diagnostics": diagnostics, "status": status}, sort_keys=True)
        for forbidden in (SECRET, HEALTH_URL, "10.23.45.67", "C:\\Users\\private", "operator-private", "worker-private", "claim_token", "claimed_by", "requested_by"):
            require(forbidden not in serialized, f"public API serialization leaked {forbidden!r}")
        require(diagnostics["queries"] and diagnostics["queries"][0]["query_text"], "bounded planned query evidence must remain public")
        require(set(diagnostics["provider_attempts"][0]["health"]) <= {"status", "healthy", "current_error"}, "health diagnostics must use strict allowlist")
        candidate = results["results"][0]
        blocked_projection = inkdrop_manual_search.normalize_candidate(
            {"accepted": True, "artifact_safe": True, "auto_grab_verdict": "auto_grab_safe", "known_bad_candidate": True},
            {"canonical_work_title": "Amulet", "unit_type": "volume", "unit_number": "9"},
            search_run_id="known-bad-fixture",
            provider_id="prowlarr",
        )
        require(not blocked_projection["accepted"] and "known_bad_candidate" in blocked_projection["rejection_codes"], "provider accepted=true must not override known-bad evidence")
        require(candidate["candidate_family_identity"] == "qa83-family-amulet-v09", "QA83 family identity must survive projection")
        require(candidate["target_compatibility"]["status"] == "compatible" and candidate["query_evidence"]["request_id"] == "qa83-request-1", "QA83 relevance/provenance must survive projection")

        with inkdrop_state.connect_read(db) as con:
            evidence = con.execute("select evidence_json from manual_search_candidates").fetchone()["evidence_json"]
            capsule = con.execute("select capsule_json,expires_at from manual_search_handoff_capsules").fetchone()
            health = con.execute("select health_json from manual_search_provider_attempts").fetchone()["health_json"]
        require("raw_candidate" not in evidence and SECRET not in evidence and PRIVATE_LOCATOR not in evidence, "evidence must never retain raw locators or credentials")
        require(SECRET not in capsule["capsule_json"] and PRIVATE_LOCATOR in capsule["capsule_json"], "private TTL capsule may retain only a non-credential acquisition locator")
        require(CREDENTIAL_LOCATOR not in capsule["capsule_json"] and TOKEN_LOCATOR not in capsule["capsule_json"] and "<redacted-private-locator>" in capsule["capsule_json"], "credential-bearing URL userinfo/query secrets must be rejected before persistence")
        require(capsule["expires_at"] == 101 + core.DEFAULT_RUN_RETENTION_SECONDS, "private handoff state must have explicit seven-day TTL")
        require(SECRET not in health and HEALTH_URL not in health and "10.23.45.67" not in health, "persisted health must redact secrets, URLs, and private IPs")

        acquisition_key = {"method": "api_key", "scopes": ["acquisition"], "role": "api"}
        require(inkdrop_auth.principal_has_scope(acquisition_key, "acquisition"), "fixture must be an acquisition-capable API key")
        require(not inkdrop_auth.principal_has_scope(acquisition_key, "admin"), "acquisition-only API key must not be an admin")
        candidate_id = candidate["candidate_id"]
        with inkdrop_state.connect(db) as con, con:
            public = json.loads(con.execute("select public_json from manual_search_candidates where id=?", (candidate_id,)).fetchone()["public_json"])
            public["decision"]["negative_evidence"] = ["pack_hard_limit_exceeded"]
            con.execute("update manual_search_candidates set accepted=0,public_json=? where id=?", (json.dumps(public), candidate_id))
        denied = core.safe_grab_candidate(db, candidate_id, lambda *_: {"ok": True}, override_hard_limit=True, hard_limit_override_authorized=False)
        require(denied["reason"] == "admin_required_for_hard_limit_override", "acquisition-only key must not bypass a hard pack limit")
        handler_override_contract(db)

        queued = core.create_search_run(db, series_id="series-sec", requested_by="retention", now=200)
        first = core.cleanup_search_runs(db, now=200 + core.DEFAULT_RUN_RETENTION_SECONDS + 1)
        require(first["expired"] >= 1 and first["deleted"] >= 1, "cleanup must expire live runs and delete terminal runs in a bounded pass")
        second = core.cleanup_search_runs(db, now=200 + core.DEFAULT_RUN_RETENTION_SECONDS + 2)
        require(second["deleted"] >= 1, "next bounded pass must delete previously expired runs")
        with inkdrop_state.connect_read(db) as con:
            require(not con.execute("select 1 from manual_search_runs where id=?", (queued["run_id"],)).fetchone(), "expired run must be deleted")
            require(not con.execute("select 1 from manual_search_candidates where run_id=?", (created["run_id"],)).fetchone(), "terminal deletion must cascade to candidates")

        print(json.dumps({"ok": True, "health_redacted": True, "private_capsule_ttl": True, "admin_override_enforced": True, "handler_http_override_enforced": True, "retention_cascade": True}, indent=2))


if __name__ == "__main__":
    run()
