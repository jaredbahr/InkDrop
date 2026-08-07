#!/usr/bin/env python3
"""Deterministic GitHub prerelease workflow and publisher contract coverage."""

from __future__ import annotations

import json
import hashlib
import io
import tempfile
import zipfile
from pathlib import Path

from tools import inkdrop_github_release as release_tool


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class FakeApi:
    def __init__(self, tag_commit=None, release=None, ref_misses_after_create=0):
        self.tag_commit = tag_commit
        self.release = release
        self.calls = []
        self.asset_bytes = {}
        self.next_asset_id = 100
        self.ref_misses_after_create = ref_misses_after_create
        self.ref_created = False

    def sleep(self, _seconds):
        self.calls.append(("SLEEP", _seconds, None))

    def request(self, method, path, payload=None, allow_missing=False):
        self.calls.append((method, path, payload))
        if path.startswith("/git/ref/tags/"):
            if self.ref_created and self.ref_misses_after_create > 0:
                self.ref_misses_after_create -= 1
                return None
            if self.tag_commit is None:
                return None
            return {"object": {"type": "commit", "sha": self.tag_commit}}
        if method == "POST" and path == "/git/refs":
            self.tag_commit = payload["sha"]
            self.ref_created = True
            return {"ref": payload["ref"], "object": {"type": "commit", "sha": payload["sha"]}}
        if path.startswith("/releases/tags/"):
            return self.release
        if method == "POST" and path == "/releases":
            self.release = {"id": 42, "upload_url": "https://uploads.github.com/repos/jaredbahr/inkdrop-dev/releases/42/assets{?name,label}", "assets": [], **payload}
            return self.release
        if method == "PATCH" and path == "/releases/42":
            self.release.update(payload)
            return self.release
        raise AssertionError((method, path, payload, allow_missing))

    def upload_asset(self, upload_url, name, content):
        asset = {"id": self.next_asset_id, "name": name}
        self.next_asset_id += 1
        self.asset_bytes[asset["id"]] = content
        self.release.setdefault("assets", []).append(asset)
        self.calls.append(("UPLOAD", name, len(content)))
        return asset

    def download_asset(self, asset_id):
        self.calls.append(("DOWNLOAD", asset_id, None))
        return self.asset_bytes[asset_id]

    def delete_asset(self, asset_id):
        self.calls.append(("DELETE_ASSET", asset_id, None))
        self.asset_bytes.pop(asset_id, None)
        self.release["assets"] = [asset for asset in self.release.get("assets", []) if asset["id"] != asset_id]


def main():
    contract = release_tool.load_contract()
    require(contract["tag"] == f"v{contract['version']}", contract)
    require(contract["prerelease"] is True and len(contract["notes"]) >= 80, contract)
    version_match = release_tool.INKDROP_VERSION_PATTERN.fullmatch(contract["version"])
    require(version_match and not version_match.group("prerelease"), "current contract versions are bare numbers")
    require(
        contract["image_tags"]
        == [
            contract["version"],
            f"{version_match.group('major')}.{version_match.group('minor')}",
            version_match.group("major"),
            "latest",
        ],
        contract["image_tags"],
    )
    require(
        release_tool.release_image_tags("0.1.2", prerelease=False) == ["0.1.2", "0.1", "0", "latest"],
        "stable image tags should include exact, minor, major, and latest tags",
    )
    require(
        release_tool.release_image_tags("0.1.2", prerelease=True) == ["0.1.2", "0.1", "0", "latest"],
        "a bare-version closed-alpha prerelease publishes the same public tag ladder",
    )
    require(
        release_tool.release_image_tags("0.1.0-beta.4", prerelease=True)
        == ["0.1.0-beta.4", "0.1-beta", "0-beta", "beta", "latest"],
        "beta image tags should retain their prerelease channel",
    )
    require(
        release_tool.release_image_tags("0.1.0-alpha.4", prerelease=True)
        == ["0.1.0-alpha.4", "0.1-alpha", "0-alpha", "alpha"],
        "alpha image tags must not move latest",
    )
    for version, prerelease in (("0.1", False), ("0.1", True), ("0.1.2-beta", False)):
        try:
            release_tool.release_image_tags(version, prerelease=prerelease)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid image tag release contract passed: {version!r}, {prerelease!r}")

    commit = "a" * 40
    api = FakeApi()
    created = release_tool.publish_release(contract, api, commit)
    require(api.tag_commit == commit and created["prerelease"] is True, api.calls)
    require(created["make_latest"] == "false", created)
    first_post_count = len([call for call in api.calls if call[0] == "POST"])
    updated = release_tool.publish_release(contract, api, commit)
    require(updated["id"] == 42, updated)
    require(len([call for call in api.calls if call[0] == "POST"]) == first_post_count, "idempotent rerun created another tag or release")
    require(any(call[0] == "PATCH" and call[1] == "/releases/42" for call in api.calls), api.calls)

    eventual = FakeApi(ref_misses_after_create=2)
    eventual_release = release_tool.publish_release(contract, eventual, commit)
    require(eventual_release["id"] == 42, eventual.calls)
    require(len([call for call in eventual.calls if call[0] == "POST" and call[1] == "/git/refs"]) == 1, eventual.calls)
    require(len([call for call in eventual.calls if call[0] == "SLEEP"]) == 2, eventual.calls)
    require(eventual.tag_commit == commit, eventual.calls)

    bounded = FakeApi(ref_misses_after_create=release_tool.TAG_READ_ATTEMPTS)
    try:
        release_tool.publish_release(contract, bounded, commit)
    except RuntimeError as exc:
        require("bounded verification attempts" in str(exc), exc)
    else:
        raise AssertionError("release publication continued without read-after-write verification")
    require(len([call for call in bounded.calls if call[0] == "SLEEP"]) == release_tool.TAG_READ_ATTEMPTS - 1, bounded.calls)
    require(not any(call[0] == "POST" and call[1] == "/releases" for call in bounded.calls), bounded.calls)

    mismatch = FakeApi(tag_commit="b" * 40)
    try:
        release_tool.publish_release(contract, mismatch, commit)
    except RuntimeError as exc:
        require("refusing to rewrite history" in str(exc), exc)
    else:
        raise AssertionError("mismatched existing tag was rewritten")

    unchanged_version = FakeApi(tag_commit="b" * 40)
    skip_result, skip_info = release_tool.publish_release_or_skip(contract, unchanged_version, commit)
    require(skip_result is None, skip_result)
    require(
        skip_info and skip_info["reason"] == "release_tag_already_published_for_a_different_commit",
        skip_info,
    )
    require(not any(call[0] in ("POST", "PATCH") for call in unchanged_version.calls), unchanged_version.calls)

    other_error = FakeApi(ref_misses_after_create=release_tool.TAG_READ_ATTEMPTS)
    try:
        release_tool.publish_release_or_skip(contract, other_error, commit)
    except RuntimeError as exc:
        require("bounded verification attempts" in str(exc), exc)
    else:
        raise AssertionError("publish_release_or_skip swallowed an unrelated RuntimeError")

    with tempfile.TemporaryDirectory(prefix="inkdrop-release-contract-") as temp:
        temp_root = Path(temp)
        bad_contract = temp_root / "current.json"
        bad_contract.write_text(json.dumps({
            "schema": release_tool.SCHEMA,
            "version": "0.1.0-alpha.22",
            "tag": "v0.1.0-alpha.22",
            "name": "InkDrop test",
            "notes_path": "missing.md",
            "previous_state_schema_version": 17,
            "prerelease": True,
        }), encoding="utf-8")
        try:
            release_tool.load_contract(bad_contract, root=temp_root)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("missing release notes did not fail closed")

        output = temp_root / "github-output.txt"
        release_tool.append_github_output(output, contract)
        rendered = output.read_text(encoding="utf-8")
        require(f"version={contract['version']}" in rendered and f"tag={contract['tag']}" in rendered, rendered)
        require(
            f"image_tags_json={json.dumps(contract['image_tags'], separators=(',', ':'))}" in rendered,
            rendered,
        )

        candidate_path = temp_root / "qa-candidate.json"
        validation_path = temp_root / "qa-image-validation.json"
        digest = "sha256:" + "c" * 64
        candidate = {
            "schema": "inkdrop.qa_candidate.v1",
            "manifest_schema_version": 1,
            "branch": "qa",
            "full_commit_sha": commit,
            "short_sha": commit[:12],
            "version": contract["version"],
            "release_channel": "qa",
            "build_date": "2026-07-20T10:23:29Z",
            "qa_build_number": 177,
            "workflow_run_id": "177",
            "image_repository": "ghcr.io/jaredbahr/inkdrop",
            "image_tag": "ghcr.io/jaredbahr/inkdrop:qa-aaaaaaaaaaaa",
            "image_digest": digest,
            "state_schema_version": contract["previous_state_schema_version"],
        }
        candidate_path.write_text(json.dumps(candidate, sort_keys=True) + "\n", encoding="utf-8")
        validation = {
            "schema": release_tool.VALIDATION_SCHEMA,
            "passed": True,
            "commit": commit,
            "version": contract["version"],
            "release_tag": contract["tag"],
            "image_digest": digest,
            "image_ref": f"ghcr.io/jaredbahr/inkdrop@{digest}",
            "workflow_run_id": "177",
            "candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
            "previous_state_schema_version": contract["previous_state_schema_version"],
            "target_state_schema_version": candidate["state_schema_version"],
            "release_image_tags": contract["image_tags"],
            "platforms": ["linux/amd64", "linux/arm64"],
            "platform_runtime": [
                {"platform": "linux/amd64", "machine": "x86_64", "passed": True},
                {"platform": "linux/arm64", "machine": "aarch64", "passed": True},
            ],
            "checks": [
                "candidate_identity",
                "image_platforms",
                "platform_runtime",
                "oci_labels",
                "strict_preflight",
                "container_health",
                "public_http",
            ],
        }
        validation_path.write_text(json.dumps(validation, sort_keys=True) + "\n", encoding="utf-8")
        evidence = release_tool.load_verified_evidence(
            candidate_path, validation_path, contract, "jaredbahr/inkdrop-dev", commit, "177"
        )
        require(release_tool.VERIFIED_START in evidence["body"] and release_tool.VERIFIED_END in evidence["body"], evidence)
        require(commit in evidence["body"] and f"inkdrop@{digest}" in evidence["body"], evidence)
        require("pin the immutable image digest" in evidence["body"], evidence)
        require("inkdrop-closed-alpha-compose.zip" in evidence["assets"], evidence["assets"])
        with zipfile.ZipFile(io.BytesIO(evidence["assets"]["inkdrop-closed-alpha-compose.zip"])) as packet_zip:
            packet_env = packet_zip.read("inkdrop-closed-alpha/.env").decode("utf-8")
            require(f"INKDROP_IMAGE=ghcr.io/jaredbahr/inkdrop@{digest}" in packet_env, packet_env)
        evidence_api = FakeApi()
        release_tool.publish_release(contract, evidence_api, commit, evidence)
        require(set(asset["name"] for asset in evidence_api.release["assets"]) == set(evidence["assets"]), evidence_api.release)
        upload_count = len([call for call in evidence_api.calls if call[0] == "UPLOAD"])
        release_tool.publish_release(contract, evidence_api, commit, evidence)
        require(len([call for call in evidence_api.calls if call[0] == "UPLOAD"]) == upload_count, "identical assets were uploaded again")
        candidate_asset = next(asset for asset in evidence_api.release["assets"] if asset["name"] == "qa-candidate.json")
        evidence_api.asset_bytes[candidate_asset["id"]] = b"stale evidence"
        release_tool.publish_release(contract, evidence_api, commit, evidence)
        require(any(call[0] == "DELETE_ASSET" and call[1] == candidate_asset["id"] for call in evidence_api.calls), evidence_api.calls)
        current_candidate = next(asset for asset in evidence_api.release["assets"] if asset["name"] == "qa-candidate.json")
        require(evidence_api.asset_bytes[current_candidate["id"]] == evidence["assets"]["qa-candidate.json"], "replacement asset was not byte-verified")

        for field, bad_value in (("commit", "b" * 40), ("release_tag", "v0.1.0-alpha.999"), ("image_digest", "sha256:" + "d" * 64)):
            bad_validation = dict(validation)
            bad_validation[field] = bad_value
            validation_path.write_text(json.dumps(bad_validation), encoding="utf-8")
            try:
                release_tool.load_verified_evidence(candidate_path, validation_path, contract, "jaredbahr/inkdrop-dev", commit, "177")
            except RuntimeError as exc:
                require("evidence mismatch" in str(exc), exc)
            else:
                raise AssertionError(f"mismatched {field} evidence passed")

        incomplete_runtime = dict(validation)
        incomplete_runtime["platform_runtime"] = validation["platform_runtime"][:1]
        validation_path.write_text(json.dumps(incomplete_runtime), encoding="utf-8")
        try:
            release_tool.load_verified_evidence(candidate_path, validation_path, contract, "jaredbahr/inkdrop-dev", commit, "177")
        except RuntimeError as exc:
            require("incomplete platform runtime" in str(exc), exc)
        else:
            raise AssertionError("incomplete platform runtime evidence passed")
        validation_path.write_text(json.dumps(validation), encoding="utf-8")

    workflow = (ROOT / ".github/workflows/inkdrop-public-release.yml").read_text(encoding="utf-8")
    require("validate_qa_image:" in workflow, "immutable-image validation job is required")
    require("docker/setup-qemu-action@v3" in workflow, "multi-platform builds require QEMU on the hosted runner")
    require("platforms: linux/amd64,linux/arm64" in workflow, "QA images must publish amd64 and arm64 manifests")
    require("docker buildx imagetools inspect" in workflow, "published platform manifests must be verified by digest")
    require('["linux/amd64", "linux/arm64"]' in workflow, "platform validation must require exactly one descriptor for each supported architecture")
    require("allowed_media_types" in workflow and "invalid platform manifest digest" in workflow, "platform descriptors must have valid child digests and image media types")
    require("image_tags_json: ${{ steps.release.outputs.image_tags_json }}" in workflow, "release image tags must survive as a job output")
    require("EXPECTED_IMAGE_TAGS_JSON: ${{ needs.qa_image.outputs.image_tags_json }}" in workflow, "validation evidence must retain release image tags")
    require('for platform in linux/amd64 linux/arm64' in workflow, "both platform images must execute under validation")
    require('docker run --rm --platform "$platform"' in workflow, "cross-platform runtime probes must select each platform explicitly")
    require('"platforms": platforms' in workflow and '"platform_runtime": runtime_platforms' in workflow, "validation evidence must record platform and runtime results")
    require("qa-image-index.json" in workflow and "qa-platform-runtime.jsonl" in workflow, "raw platform evidence must be uploaded")
    require("needs: [qa_image, validate_qa_image]" in workflow, "release job must retain build outputs and wait for immutable-image validation")
    require('image="ghcr.io/${GITHUB_REPOSITORY_OWNER}/inkdrop-qa@${IMAGE_DIGEST}"' in workflow, "validation must pull the exact QA digest")
    require("QA candidate identity mismatch" in workflow and "QA image label mismatch" in workflow, "manifest and OCI identity checks are required")
    require("--strict-dependencies --strict-runtime-tools" in workflow, "strict image preflight is required")
    require("/status.json" in workflow and ".State.Health.Status" in workflow, "image HTTP and health checks are required")
    require("qa-image-validation.json" in workflow and "candidate_sha256" in workflow, "permanent validation evidence is required")
    require(workflow.index("Create verified QA image summary") > workflow.index("Prove container health and public HTTP"), "validation summary must be created only after exact-image checks")
    require("--candidate release-evidence/candidate/qa-candidate.json" in workflow, "release must reverify downloaded candidate evidence")
    require(
        "concurrency:" in workflow and "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow,
        "QA publication (push) runs must never be auto-cancelled; only pull_request runs, which never reach the publish jobs, may be",
    )
    require("github.ref == 'refs/heads/qa'" in workflow, "release publication must be QA-only")
    require("github.event_name != 'pull_request'" in workflow, "release publication must reject PRs")
    require("contents: write" in workflow, "release job needs scoped contents permission")
    require("tools/inkdrop_github_release.py" in workflow and "--publish" in workflow, "workflow must use guarded publisher")
    require("softprops/action-gh-release" not in workflow and "ncipollo/release-action" not in workflow, "third-party release actions are forbidden")
    require("INKDROP_VERSION=0.1.0-alpha." not in workflow, "workflow duplicated hardcoded QA version")
    print("github release contract smoke passed")


if __name__ == "__main__":
    main()
