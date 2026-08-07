#!/usr/bin/env python3
"""Smoke checks for backend operator contracts."""

from __future__ import annotations

import os
from pathlib import Path

from core import inkdrop_operator_contracts as contracts
from core import inkdrop_version


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def test_manual_review_contracts():
    automatic = contracts.manual_review_contract(
        {
            "queue_id": "q-auto",
            "state": "queued",
            "last_event": "No safe candidate found yet; InkDrop will retry automatically.",
            "next_retry_at": 123.0,
        }
    )
    assert_equal(automatic["eligible"], False, "no-candidate retry must not require Manual Review")
    assert_equal(automatic["available_actions"], [], "automatic retry rows must not expose review actions")
    assert_true(automatic["excluded_reason"] in {"automatic_retry", "no_candidate"}, "automatic row should explain exclusion")

    provider_wait = contracts.manual_review_contract(
        {
            "queue_id": "q-provider",
            "state": "provider_wait",
            "reason": "Waiting on provider health.",
        }
    )
    assert_equal(provider_wait["eligible"], False, "provider wait must not require Manual Review")
    assert_equal(provider_wait["safe_default"], "automation_retry", "provider wait should remain automatic")

    ambiguous = contracts.manual_review_contract(
        {
            "review_id": "r-1",
            "state": "needs_you",
            "reason_code": "ambiguous_trusted_candidate",
            "evidence_summary": "Two candidates match the same issue.",
        }
    )
    assert_equal(ambiguous["eligible"], True, "ambiguous trusted candidates should be reviewable")
    assert_true("approve_candidate" in ambiguous["available_actions"], "reviewable rows need meaningful actions")

    destination = contracts.manual_review_contract(
        {
            "review_id": "r-2",
            "state": "needs_you",
            "reason_code": "destination_conflict",
            "evidence_summary": "Two managed roots could receive this file.",
        }
    )
    assert_equal(destination["eligible"], True, "destination conflicts should be reviewable")
    assert_true("resolve_destination" in destination["available_actions"], "destination conflicts need a destination action")

    invalid_archive = contracts.manual_review_contract(
        {
            "queue_id": "q-archive",
            "state": "retry_later",
            "reason_code": "invalid_archive_retry",
            "retry_eligible": True,
        }
    )
    assert_equal(invalid_archive["eligible"], False, "invalid archive retry should stay automatic when recoverable")


def test_settings_contracts():
    secret = contracts.setting_contract({"key": "comicvine.api_key", "value": "secret-value", "editable": True})
    assert_equal(secret["secret"], True, "API keys should be marked secret")
    assert_equal(secret["write_only"], True, "API keys should be write-only")
    assert_equal(secret["value"], None, "API keys should not echo values")

    low_level = contracts.setting_contract({"key": "automation.probe_budget_seconds", "value": 300})
    assert_equal(low_level["advanced"], True, "low-level budget settings should be advanced")


def test_version_contract():
    env = {
        "INKDROP_VERSION": "v0.1.0-alpha.1",
        "INKDROP_COMMIT_SHA": "abcdef1234567890",
        "INKDROP_BUILD_DATE": "2026-07-10T12:00:00Z",
        "INKDROP_RELEASE_CHANNEL": "qa",
        "INKDROP_IMAGE_REVISION": "abcdef1234567890",
        "INKDROP_IMAGE_DIGEST": "sha256:test",
    }
    metadata = inkdrop_version.build_metadata(env)
    assert_equal(metadata["product_name"], "InkDrop", "product name should be InkDrop")
    assert_equal(metadata["channel"], "qa", "release channel should be exposed")
    assert_equal(metadata["short_sha"], "abcdef1", "short SHA should be exposed")
    assert_equal(metadata["development"], False, "QA tagged builds should not report development")
    assert_equal(metadata["image_digest"], "sha256:test", "image digest should be exposed")
    assert_equal(metadata["oci"]["org.opencontainers.image.revision"], "abcdef1234567890", "OCI revision should match build")


def test_download_client_registry():
    registry = contracts.download_client_registry()
    implemented = set(registry["implemented"])
    planned = set(registry["planned"])
    assert_true(
        {"qbittorrent", "sabnzbd", "slskd", "nzbget", "deluge", "transmission", "utorrent", "rtorrent"} == implemented,
        "all eight accepted adapters should be implemented",
    )
    assert_equal(planned, set(), "accepted adapter registry should not advertise planned-only clients")
    for row in registry["clients"]:
        if row["client_id"] in planned:
            assert_equal(row["implemented"], False, f"{row['client_id']} should not claim implementation")
            assert_equal(row["add_grab"], False, f"{row['client_id']} should not claim grab support")
        if row["client_id"] in {"nzbget", "deluge", "transmission", "utorrent", "rtorrent"}:
            assert_equal(row["certification_tier"], "beta", f"{row['client_id']} should remain Beta")
            assert_equal(row["add_grab"], True, f"{row['client_id']} should expose tested handoff support")


def test_storage_and_maintenance_contracts():
    metric = contracts.storage_metric("workspace", Path.cwd(), "Workspace")
    assert_equal(metric["available"], True, "current workspace storage should be measurable")
    assert_true(isinstance(metric["free_bytes"], int), "free space should be numeric")
    assert_true(0 <= metric["used_percent"] <= 100, "available storage percentage should be valid")

    unavailable = contracts.storage_metric("missing", "Z:/definitely/not/a/real/inkdrop/path", "Missing")
    assert_equal(unavailable["available"], False, "missing storage should be explicit")
    assert_equal(unavailable["used_percent"], None, "unavailable storage must not send decorative percentages")

    catalog = contracts.maintenance_catalog()
    categories = {row["category"] for row in catalog["actions"]}
    assert_true("safe_read_only" in categories, "maintenance catalog needs read-only actions")
    assert_true("destructive_high_impact" in categories, "maintenance catalog needs destructive classification")
    for row in catalog["actions"]:
        assert_true("last_run" in row, f"{row['id']} should expose last_run")
        assert_true("current_state" in row, f"{row['id']} should expose current_state")
        assert_true("auth_requirement" in row, f"{row['id']} should expose auth_requirement")


def test_activity_contract():
    event = contracts.bounded_activity_event(
        {
            "queue_id": "q1",
            "wanted_id": "w1",
            "state": "staged_file_ready",
            "provider": "slskd",
            "current_source": "slskd",
            "query": "Vinland Saga Omnibus 01",
            "candidate_counts": {"accepted": 1},
            "rejection_reason_counts": {"language": 2},
            "download_client": "slskd",
            "status": "verification_pending",
            "next_source": "prowlarr",
            "next_retry_at": 123.0,
            "updated_at": 456.0,
        }
    )
    assert_equal(event["stage"], "completed_in_client", "activity stage should normalize to the contract vocabulary")
    assert_true(event["stage"] in contracts.ACTIVITY_STAGES, "activity stage must be a known contract stage")
    assert_equal(event["query_summary"], "Vinland Saga Omnibus 01", "activity should expose query summary")
    assert_equal(event["candidate_counts"]["accepted"], 1, "activity should expose candidate counts")


def main():
    os.environ.setdefault("INKDROP_TESTING", "1")
    test_manual_review_contracts()
    test_settings_contracts()
    test_version_contract()
    test_download_client_registry()
    test_storage_and_maintenance_contracts()
    test_activity_contract()
    print("inkdrop-operator-contracts-smoke: ok")


if __name__ == "__main__":
    main()
