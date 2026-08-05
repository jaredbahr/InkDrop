#!/usr/bin/env python3
"""Positive and adversarial update-awareness contract coverage."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import inkdrop_version as updates
from tools import inkdrop_github_release as release_tool


ROOT = Path(__file__).resolve().parents[1]
NOW = 1784422800
COMMIT = "a" * 40
CURRENT_DIGEST = "sha256:" + "b" * 64
LATEST_DIGEST = "sha256:" + "c" * 64


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def manifest(**changes):
    payload = {
        "schema_version": updates.UPDATE_MANIFEST_SCHEMA_VERSION,
        "product": "InkDrop",
        "version": "0.1.0-alpha.53",
        "channel": "qa",
        "prerelease": True,
        "validated": True,
        "published_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat().replace("+00:00", "Z"),
        "commit_sha": "d" * 40,
        "image_repository": updates.UPDATE_IMAGE_REPOSITORIES["qa"],
        "image_digest": LATEST_DIGEST,
        "minimum_supported_version": "0.1.0-alpha.1",
        "minimum_schema": 1,
        "maximum_schema": 12,
        "database_migration": False,
        "restart_required": True,
        "release_notes_summary": "A verified InkDrop prerelease with bounded update metadata.",
        "release_url": "https://github.com/jaredbahr/inkdrop-dev/releases/tag/v0.1.0-alpha.53",
        "installation_notes": "Pin the exact digest and recreate web and worker together with an external manager.",
        "rollback_notes": "Keep the prior digest and backup, then recreate both services if verification fails.",
    }
    payload.update(changes)
    payload["manifest_identity"] = updates.update_manifest_identity(payload)
    return payload


def expect_invalid(payload, phrase, now=NOW):
    try:
        updates.validate_update_manifest(payload, now=now)
    except ValueError as exc:
        require(phrase.lower() in str(exc).lower(), (phrase, exc))
    else:
        raise AssertionError(f"invalid update manifest passed: {phrase}")


valid = updates.validate_update_manifest(manifest(), now=NOW)
require(set(updates.UPDATE_MANIFEST_REQUIRED_KEYS).issubset(valid), valid)
expect_invalid(manifest(schema_version=2), "schema")
expect_invalid(dict(manifest(), unexpected=True), "fields")
expect_invalid(manifest(validated=False), "not validated")
expect_invalid(manifest(image_repository="ghcr.io/attacker/inkdrop"), "channel repository")
expect_invalid(manifest(image_digest="sha256:short"), "immutable")
bad_identity = manifest(); bad_identity["manifest_identity"] = "sha256:" + "0" * 64
expect_invalid(bad_identity, "identity mismatch")
expect_invalid(manifest(published_at=datetime.fromtimestamp(NOW - 40 * 86400, timezone.utc).isoformat()), "stale")
expect_invalid(manifest(published_at=datetime.fromtimestamp(NOW + 3600, timezone.utc).isoformat()), "future")
expect_invalid(manifest(minimum_schema=13, maximum_schema=12), "bounds")
expect_invalid(manifest(database_migration=True, restart_required=False), "restart")
secret_note = manifest(installation_notes="Use the external manager with access_token=private-update-token after taking a backup.")
expect_invalid(secret_note, "secret-like")
try:
    updates.validate_update_manifest(secret_note, now=NOW)
except ValueError as exc:
    require("private-update-token" not in str(exc), "manifest validation errors must redact rejected values")
else:
    raise AssertionError("secret-like update metadata was accepted")
for secret_text, private_value in (
    ("Use Authorization: Bearer authorization-private-value only in a private shell.", "authorization-private-value"),
    ("Never paste Bearer standalone-private-token into release metadata.", "standalone-private-token"),
    ("The external manager credential=credential-private-value must remain private.", "credential-private-value"),
    ("Never publish github_pat_abcdefghijklmnopqrstuvwxyz1234567890 in release notes.", "github_pat_abcdefghijklmnopqrstuvwxyz1234567890"),
):
    secret_manifest = manifest(rollback_notes=secret_text)
    try:
        updates.validate_update_manifest(secret_manifest, now=NOW)
    except ValueError as exc:
        require("secret-like" in str(exc) and private_value not in str(exc), (secret_text, exc))
    else:
        raise AssertionError(f"secret-like update metadata was accepted: {private_value}")
bounded_cache_fixture = {}
for index in range(12):
    updates._bounded_cache_put(bounded_cache_fixture, f"context-{index}", {"index": index})
require(len(bounded_cache_fixture) == 8 and "context-0" not in bounded_cache_fixture and "context-11" in bounded_cache_fixture, bounded_cache_fixture)

with tempfile.TemporaryDirectory(prefix="inkdrop-update-awareness-") as temp:
    folder = Path(temp)
    candidate_path = folder / "current.json"
    latest_path = folder / "latest-update.json"
    candidate = {
        "schema": "inkdrop.qa_candidate.v1", "manifest_schema_version": 1, "branch": "qa",
        "full_commit_sha": COMMIT, "short_sha": COMMIT[:12], "version": "0.1.0-alpha.52",
        "release_channel": "qa", "build_date": "2026-07-19T00:00:00Z",
        "image_repository": updates.UPDATE_IMAGE_REPOSITORIES["qa"],
        "image_tag": "ghcr.io/jaredbahr/inkdrop-qa:qa-aaaaaaaaaaaa", "image_digest": CURRENT_DIGEST,
        "workflow_run_id": "122", "qa_build_number": 52, "state_schema_version": 12,
    }
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    base_env = {
        "INKDROP_VERSION": "0.1.0-alpha.52", "INKDROP_COMMIT_SHA": COMMIT,
        "INKDROP_BUILD_DATE": "2026-07-19T00:00:00Z", "INKDROP_RELEASE_CHANNEL": "qa",
        "INKDROP_QA_BUILD_NUMBER": "52", "INKDROP_STATE_SCHEMA_VERSION": "12",
        "INKDROP_IMAGE_REPOSITORY": updates.UPDATE_IMAGE_REPOSITORIES["qa"],
        "INKDROP_IMAGE_DIGEST": CURRENT_DIGEST,
        "INKDROP_CANDIDATE_MANIFEST_PATH": str(candidate_path),
        "INKDROP_WORKER_IMAGE_DIGEST": CURRENT_DIGEST,
        "INKDROP_UPDATE_MANIFEST_PATH": str(latest_path), "INKDROP_UPDATE_REMOTE_ENABLED": "0",
        "INKDROP_UPDATE_MANIFEST_URL": "https://github.com/jaredbahr/inkdrop-dev/releases/download/v0.1.0-alpha.53/inkdrop-update-manifest.json",
        "INKDROP_UPDATE_CACHE_SECONDS": "60", "INKDROP_UPDATE_CHANNEL": "qa",
    }

    def state_for(payload, **env_changes):
        effective_env = dict(base_env, **env_changes)
        current_candidate = dict(candidate)
        current_channel = str(effective_env["INKDROP_RELEASE_CHANNEL"]).lower()
        current_repository = updates.UPDATE_IMAGE_REPOSITORIES.get(current_channel, updates.UPDATE_IMAGE_REPOSITORIES["qa"])
        effective_env["INKDROP_IMAGE_REPOSITORY"] = current_repository
        current_candidate["release_channel"] = current_channel
        current_candidate["version"] = effective_env["INKDROP_VERSION"]
        current_candidate["build_date"] = effective_env["INKDROP_BUILD_DATE"]
        current_candidate["image_repository"] = current_repository
        current_candidate["image_tag"] = f"{current_repository}:qa-{COMMIT[:12]}"
        current_candidate["image_digest"] = effective_env["INKDROP_IMAGE_DIGEST"]
        current_candidate["qa_build_number"] = int(effective_env["INKDROP_QA_BUILD_NUMBER"])
        current_candidate["state_schema_version"] = int(effective_env["INKDROP_STATE_SCHEMA_VERSION"])
        candidate_path.write_text(json.dumps(current_candidate), encoding="utf-8")
        if payload is None:
            latest_path.unlink(missing_ok=True)
        else:
            latest_path.write_text(json.dumps(payload), encoding="utf-8")
        updates.clear_update_cache()
        return updates.update_status(effective_env, now=NOW)

    seen = set()
    prerelease = state_for(manifest())
    seen.add(prerelease["state"])
    require(prerelease["schema"] == updates.UPDATE_STATUS_SCHEMA and prerelease["status_schema_version"] == 1, prerelease)
    require(isinstance(prerelease["current"], dict) and isinstance(prerelease["deployment"], dict) and isinstance(prerelease["cache"], dict), prerelease)
    require(prerelease["state"] == "newer_prerelease_available" and prerelease["external_updater"]["install_available"] is False, prerelease)
    require(prerelease["external_updater"]["image_ref"] == f"{updates.UPDATE_IMAGE_REPOSITORIES['qa']}@{LATEST_DIGEST}", prerelease)

    stable_update = manifest(version="0.1.1", channel="stable", prerelease=False, image_repository=updates.UPDATE_IMAGE_REPOSITORIES["stable"], release_url="https://github.com/jaredbahr/inkdrop-dev/releases/tag/v0.1.1")
    result = state_for(stable_update, INKDROP_RELEASE_CHANNEL="stable", INKDROP_UPDATE_CHANNEL="stable", INKDROP_VERSION="0.1.0")
    seen.add(result["state"]); require(result["state"] == "update_available", result)

    current = manifest(version="0.1.0-alpha.52", release_url="https://github.com/jaredbahr/inkdrop-dev/releases/tag/v0.1.0-alpha.52", image_digest=CURRENT_DIGEST)
    result = state_for(current, INKDROP_WORKER_IMAGE_DIGEST=CURRENT_DIGEST)
    seen.add(result["state"]); require(result["state"] == "up_to_date" and result["deployment"]["consistent"] is True, result)

    result = state_for(manifest(channel="alpha", image_repository=updates.UPDATE_IMAGE_REPOSITORIES["alpha"]), INKDROP_UPDATE_CHANNEL="qa")
    seen.add(result["state"]); require(result["state"] == "channel_mismatch", result)
    stable_prerelease = manifest(channel="stable", image_repository=updates.UPDATE_IMAGE_REPOSITORIES["stable"])
    result = state_for(stable_prerelease, INKDROP_RELEASE_CHANNEL="stable", INKDROP_UPDATE_CHANNEL="stable", INKDROP_VERSION="0.1.0")
    require(result["state"] == "channel_mismatch", result)

    result = state_for(manifest(minimum_supported_version="0.2.0"))
    seen.add(result["state"]); require(result["state"] == "incompatible_update", result)
    require(state_for(manifest(minimum_schema=13, maximum_schema=20))["state"] == "incompatible_update", "minimum schema was ignored")
    require(state_for(manifest(maximum_schema=11))["state"] == "incompatible_update", "maximum schema was ignored")

    result = state_for(None)
    seen.add(result["state"]); require(result["state"] == "update_check_unavailable", result)
    malformed = manifest(); malformed["validated"] = False; malformed["manifest_identity"] = updates.update_manifest_identity(malformed)
    result = state_for(malformed)
    seen.add(result["state"]); require(result["state"] == "invalid_manifest", result)

    result = state_for(manifest(), INKDROP_WORKER_IMAGE_DIGEST="sha256:" + "e" * 64)
    seen.add(result["state"]); require(result["state"] == "unsupported_install", result)
    require(result["deployment"]["consistent"] is False and result["external_updater"]["image_ref"] == "" and result["external_updater"]["commands"] == {}, result)
    invalid_worker = state_for(manifest(), INKDROP_WORKER_IMAGE_DIGEST="not-an-immutable-digest")
    require(invalid_worker["state"] == "unsupported_install" and invalid_worker["deployment"]["worker_identity_valid"] is False, invalid_worker)
    require(invalid_worker["deployment"]["worker_image_digest"] == "", "invalid worker identity must not be reflected")
    result = state_for(manifest(), INKDROP_VERSION="dev", INKDROP_RELEASE_CHANNEL="dev")
    seen.add(result["state"]); require(result["state"] == "development_build", result)

    # A remote validation failure is distinct from an unavailable check.
    state_for(None)
    updates.clear_update_cache()
    invalid_remote_env = dict(base_env, INKDROP_UPDATE_REMOTE_ENABLED="1")
    result = updates.update_status(invalid_remote_env, now=NOW, fetcher=lambda *_: b'{"validated":false}')
    seen.add(result["state"]); require(result["state"] == "invalid_manifest", result)
    updates.clear_update_cache()
    bad_url = updates.update_status(dict(base_env, INKDROP_UPDATE_REMOTE_ENABLED="1", INKDROP_UPDATE_MANIFEST_URL="https://example.invalid/update.json"), now=NOW)
    require(bad_url["state"] == "invalid_manifest", bad_url)

    # Successful remote reads are single-flight cached; timeout uses last-known-good without retry storms.
    calls = {"count": 0}
    remote_bytes = json.dumps(manifest()).encode()
    def remote_ok(*_args):
        calls["count"] += 1
        return remote_bytes
    state_for(None)
    updates.clear_update_cache()
    latest_path.unlink(missing_ok=True)
    remote_env = dict(base_env, INKDROP_UPDATE_REMOTE_ENABLED="1")
    cold_local = state_for(None)
    require(cold_local["state"] == "update_check_unavailable" and calls["count"] == 0, cold_local)
    first = updates.update_status(remote_env, now=NOW, fetcher=remote_ok)
    warm_local = updates.update_status(base_env, now=NOW + 15)
    second = updates.update_status(remote_env, now=NOW + 30, fetcher=remote_ok)
    require(warm_local["state"] == first["state"] and warm_local["cache"]["last_known_good"] is True, warm_local)
    require(calls["count"] == 1 and second["cache"]["age_seconds"] == 30, (calls, second))

    # Local first paint must not wait on the global cache lock while a remote refresh is in flight.
    updates.clear_update_cache()
    latest_path.unlink(missing_ok=True)
    slow_started = threading.Event()
    slow_release = threading.Event()
    slow_calls = {"count": 0}
    def held_remote(*_args):
        slow_calls["count"] += 1
        slow_started.set()
        require(slow_release.wait(timeout=2), "test did not release held remote fetch")
        return remote_bytes
    with ThreadPoolExecutor(max_workers=1) as pool:
        remote_future = pool.submit(updates.update_status, remote_env, NOW, held_remote)
        require(slow_started.wait(timeout=1), "remote fetch did not start")
        local_started = time.monotonic()
        concurrent_local = updates.update_status(base_env, now=NOW, allow_remote=False)
        local_elapsed = time.monotonic() - local_started
        require(local_elapsed < .2, (local_elapsed, concurrent_local))
        require(concurrent_local["state"] == "update_check_unavailable", concurrent_local)
        slow_release.set()
        held_result = remote_future.result(timeout=2)
    require(held_result["state"] == "newer_prerelease_available" and slow_calls["count"] == 1, (held_result, slow_calls))

    def remote_timeout(*_args):
        calls["count"] += 1
        raise TimeoutError("bounded timeout")
    fallback = updates.update_status(remote_env, now=NOW + 61, fetcher=remote_timeout)
    require(fallback["state"] == first["state"] and fallback["cache"]["last_known_good"] is True and calls["count"] == 2, fallback)

    # An injected fetcher is outer-deadline bounded by wall clock.
    updates.clear_update_cache()
    deadline_env = dict(remote_env, INKDROP_UPDATE_TOTAL_TIMEOUT_SECONDS="1", INKDROP_UPDATE_CONNECT_TIMEOUT_SECONDS="10")
    started = time.monotonic()
    bounded = updates.update_status(deadline_env, now=NOW, fetcher=lambda *_: time.sleep(2))
    elapsed = time.monotonic() - started
    require(elapsed < 1.5 and bounded["cache"]["state"] == "timeout", (elapsed, bounded))

    # Last-known-good is revalidated on fallback and cannot outlive manifest max age.
    updates.clear_update_cache()
    latest_path.unlink(missing_ok=True)
    expiring_env = dict(remote_env, INKDROP_UPDATE_MANIFEST_MAX_AGE_SECONDS="3600")
    require(updates.update_status(expiring_env, now=NOW, fetcher=remote_ok)["latest"], "remote LKG was not seeded")
    expired = updates.update_status(expiring_env, now=NOW + 3601, fetcher=remote_timeout)
    require(expired["state"] == "update_check_unavailable" and not expired["cache"]["last_known_good"], expired)

    updates.clear_update_cache()
    calls["count"] = 0
    def slow_remote(*_args):
        calls["count"] += 1
        time.sleep(.05)
        return remote_bytes
    with ThreadPoolExecutor(max_workers=2) as pool:
        simultaneous = list(pool.map(lambda _: updates.update_status(remote_env, now=NOW + 100, fetcher=slow_remote), range(2)))
    require(calls["count"] == 1 and {item["state"] for item in simultaneous} == {"newer_prerelease_available"}, (calls, simultaneous))

    # Distinct in-flight contexts are bounded; saturation degrades without another fetch.
    updates.clear_update_cache()
    with updates._UPDATE_CACHE_LOCK:
        updates._UPDATE_CACHE["inflight"] = {f"held-{index}": threading.Event() for index in range(updates.UPDATE_CACHE_MAX_CONTEXTS)}
    saturated_calls = {"count": 0}
    def saturated_fetch(*_args):
        saturated_calls["count"] += 1
        return remote_bytes
    saturated = updates.update_status(remote_env, now=NOW, fetcher=saturated_fetch)
    require(saturated["state"] == "update_check_unavailable" and saturated["cache"]["state"] == "unavailable", saturated)
    require(saturated_calls["count"] == 0, saturated_calls)
    updates.clear_update_cache()

    require(seen == updates.UPDATE_STATES, {"missing": updates.UPDATE_STATES - seen, "extra": seen - updates.UPDATE_STATES})

    # Release generation remains rooted in exact candidate + validation evidence.
    contract = release_tool.load_contract()
    candidate.update(version=contract["version"], workflow_run_id="177", image_digest=LATEST_DIGEST, state_schema_version=contract["previous_state_schema_version"])
    candidate_path.write_text(json.dumps(candidate, sort_keys=True) + "\n", encoding="utf-8")
    validation_path = folder / "qa-image-validation.json"
    validation = {
        "schema": release_tool.VALIDATION_SCHEMA, "passed": True, "commit": COMMIT,
        "version": contract["version"], "release_tag": contract["tag"], "image_digest": LATEST_DIGEST,
        "image_ref": f"{updates.UPDATE_IMAGE_REPOSITORIES['qa']}@{LATEST_DIGEST}", "workflow_run_id": "177",
        "candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        "validated_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat().replace("+00:00", ".123456Z"),
        "previous_state_schema_version": contract["previous_state_schema_version"],
        "target_state_schema_version": candidate["state_schema_version"],
        "release_image_tags": contract["image_tags"],
        "platforms": ["linux/amd64", "linux/arm64"],
        "platform_runtime": [
            {"platform": "linux/amd64", "machine": "x86_64", "passed": True},
            {"platform": "linux/arm64", "machine": "aarch64", "passed": True},
        ],
        "checks": [
            "candidate_identity", "image_platforms", "platform_runtime", "oci_labels",
            "strict_preflight", "container_health", "public_http",
        ],
    }
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    generated = release_tool.build_update_manifest(candidate_path, validation_path, contract, "jaredbahr/inkdrop-dev", COMMIT, "177", updates.UPDATE_IMAGE_REPOSITORIES["qa"])
    expected_published = datetime.fromtimestamp(NOW, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    require(generated["published_at"] == expected_published, generated["published_at"])
    require(generated["database_migration"] is False, generated)
    candidate["state_schema_version"] += 1
    candidate_path.write_text(json.dumps(candidate, sort_keys=True) + "\n", encoding="utf-8")
    validation.update(target_state_schema_version=candidate["state_schema_version"], candidate_sha256=hashlib.sha256(candidate_path.read_bytes()).hexdigest())
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    changed = release_tool.build_update_manifest(candidate_path, validation_path, contract, "jaredbahr/inkdrop-dev", COMMIT, "177", updates.UPDATE_IMAGE_REPOSITORIES["qa"])
    require(changed["database_migration"] is True, changed)
    candidate["state_schema_version"] = contract["previous_state_schema_version"]
    candidate_path.write_text(json.dumps(candidate, sort_keys=True) + "\n", encoding="utf-8")
    validation.update(target_state_schema_version=candidate["state_schema_version"], candidate_sha256=hashlib.sha256(candidate_path.read_bytes()).hexdigest())
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    update_path = folder / "inkdrop-update-manifest.json"
    update_path.write_text(json.dumps(generated, sort_keys=True) + "\n", encoding="utf-8")
    evidence = release_tool.load_verified_evidence(candidate_path, validation_path, contract, "jaredbahr/inkdrop-dev", COMMIT, "177", update_path, updates.UPDATE_IMAGE_REPOSITORIES["qa"])
    require("inkdrop-update-manifest.json" in evidence["assets"], evidence)
    tampered = dict(generated, image_digest="sha256:" + "9" * 64)
    tampered["manifest_identity"] = updates.update_manifest_identity(tampered)
    update_path.write_text(json.dumps(tampered), encoding="utf-8")
    try:
        release_tool.load_verified_evidence(candidate_path, validation_path, contract, "jaredbahr/inkdrop-dev", COMMIT, "177", update_path, updates.UPDATE_IMAGE_REPOSITORIES["qa"])
    except RuntimeError as exc:
        require("evidence mismatch" in str(exc), exc)
    else:
        raise AssertionError("tampered update manifest was attached")

web = (ROOT / "inkdrop_web.py").read_text(encoding="utf-8")
workflow = (ROOT / ".github/workflows/inkdrop-public-release.yml").read_text(encoding="utf-8")
require('const updatePromise = Promise.resolve({});' in web, "normal System pages should not request update status")
require('fetchJson("/api/system/update-status?refresh=0")' not in web, "removed Updates surface must not fetch local update status")
require('fetchJson("/api/system/update-status?refresh=1")' not in web, "removed Updates surface must not refresh remote update status")
require("/api/system/install-update" not in web.lower(), "in-process updater endpoint detected")
require("does not access the docker socket" in web.lower(), "container-manager guidance should explain that InkDrop does not access the Docker socket")
require(workflow.index("needs: [qa_image, validate_qa_image]") < workflow.index("--generate-update-manifest-output"), "manifest generation precedes image validation")
require("--update-manifest release-evidence/inkdrop-update-manifest.json" in workflow, "release attachment missing")

print("inkdrop update awareness smoke: PASS")
