#!/usr/bin/env python3
"""Validate and idempotently publish the checked-in InkDrop GitHub prerelease."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import inkdrop_version
from tools import inkdrop_closed_alpha_packet


DEFAULT_CONTRACT = ROOT / "docs" / "inkdrop" / "releases" / "current.json"
SCHEMA = "inkdrop.github_release.v1"
VALIDATION_SCHEMA = "inkdrop.qa_image_validation.v1"
VERIFIED_START = "<!-- inkdrop-verified-qa:start -->"
VERIFIED_END = "<!-- inkdrop-verified-qa:end -->"
TAG_READ_ATTEMPTS = 6
TAG_READ_DELAY_SECONDS = 1.0
DEFAULT_IMAGE_REPOSITORY = "ghcr.io/{owner}/inkdrop"


def resolve_image_repository(repository, image_repository=None):
    owner = str(repository or "").split("/", 1)[0].lower()
    value = str(image_repository or DEFAULT_IMAGE_REPOSITORY.format(owner=owner)).strip().lower()
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+", value):
        raise ValueError("image repository must be a GitHub Container Registry repository")
    if not value.startswith(f"ghcr.io/{owner}/"):
        raise ValueError("image repository must belong to the repository owner")
    if value.rsplit("/", 1)[-1] not in {"inkdrop", "inkdrop-qa"}:
        raise ValueError("release image repository must be inkdrop or inkdrop-qa")
    return value


def load_contract(path=DEFAULT_CONTRACT, root=ROOT):
    path = Path(path).resolve()
    root = Path(root).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError("unsupported release contract schema")
    version = str(payload.get("version") or "").strip()
    tag = str(payload.get("tag") or "").strip()
    name = str(payload.get("name") or "").strip()
    # Versions are the bare number now (0.1.02); the older suffixed forms
    # (0.1.01-beta, 0.1.0-alpha.98) still validate so historical contracts and
    # any re-publish of an old tag keep working. The patch keeps its leading
    # zero, so this deliberately is not a strict semver check.
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("release version must be a dotted version, optionally with a prerelease suffix")
    if tag != f"v{version}":
        raise ValueError("release tag must be v + version")
    if not name or payload.get("prerelease") is not True:
        raise ValueError("release name and prerelease=true are required")
    previous_state_schema_version = payload.get("previous_state_schema_version")
    if not isinstance(previous_state_schema_version, int) or previous_state_schema_version < 1:
        raise ValueError("previous_state_schema_version must be a positive integer")
    notes_path = (root / str(payload.get("notes_path") or "")).resolve()
    try:
        notes_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("release notes must stay inside the repository") from exc
    notes = notes_path.read_text(encoding="utf-8").strip()
    if len(notes) < 80 or len(notes.encode("utf-8")) > 65536:
        raise ValueError("release notes are missing or outside the 80-byte to 64-KiB bound")
    if version not in notes:
        raise ValueError("release notes must name the exact version")
    if VERIFIED_START in notes or VERIFIED_END in notes:
        raise ValueError("release notes may not contain reserved verified-build markers")
    return {
        "version": version,
        "tag": tag,
        "name": name,
        "notes_path": str(notes_path.relative_to(root)).replace("\\", "/"),
        "notes": notes,
        "prerelease": True,
        "previous_state_schema_version": previous_state_schema_version,
    }


class GitHubApi:
    sleep = staticmethod(time.sleep)

    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
            raise ValueError("invalid GitHub repository")
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        self.base = f"https://api.github.com/repos/{repository}"
        self.token = token

    def request(self, method, path, payload=None, allow_missing=False):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "inkdrop-release-sync",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            if allow_missing and exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"GitHub API {method} {path} failed ({exc.code}): {detail}") from exc

    def request_bytes(self, method, path, payload=None, accept="application/octet-stream", content_type=None):
        data = payload
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "inkdrop-release-sync",
        }
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"GitHub API {method} {path} failed ({exc.code}): {detail}") from exc

    def upload_asset(self, upload_url, name, content):
        base = str(upload_url or "").split("{", 1)[0]
        expected = self.base.replace("https://api.github.com", "https://uploads.github.com") + "/releases/"
        if not base.startswith(expected):
            raise RuntimeError("release upload URL is outside the expected repository")
        url = f"{base}?name={urllib.parse.quote(name, safe='')}"
        path = url.removeprefix("https://uploads.github.com/repos/")
        upload_base = "https://uploads.github.com/repos/" + path.split("/releases/", 1)[0]
        original_base, self.base = self.base, upload_base
        try:
            raw = self.request_bytes(
                "POST",
                "/releases/" + path.split("/releases/", 1)[1].split("?", 1)[0] + "?" + url.split("?", 1)[1],
                content,
                "application/vnd.github+json",
                "application/json",
            )
            return json.loads(raw.decode("utf-8"))
        finally:
            self.base = original_base

    def download_asset(self, asset_id):
        return self.request_bytes("GET", f"/releases/assets/{int(asset_id)}")

    def delete_asset(self, asset_id):
        self.request_bytes("DELETE", f"/releases/assets/{int(asset_id)}", accept="application/vnd.github+json")


def resolved_tag_commit(api, tag):
    encoded = urllib.parse.quote(tag, safe="")
    ref = api.request("GET", f"/git/ref/tags/{encoded}", allow_missing=True)
    if ref is None:
        return None
    obj = ref.get("object") or {}
    if obj.get("type") == "commit":
        return str(obj.get("sha") or "").lower()
    if obj.get("type") == "tag":
        tag_object = api.request("GET", f"/git/tags/{obj.get('sha')}")
        target = tag_object.get("object") or {}
        if target.get("type") == "commit":
            return str(target.get("sha") or "").lower()
    raise RuntimeError("release tag does not resolve directly to a commit")


def load_verified_evidence(candidate_path, validation_path, contract, repository, commit, workflow_run_id, update_manifest_path=None, image_repository=None):
    candidate_path = Path(candidate_path)
    validation_path = Path(validation_path)
    candidate_bytes = candidate_path.read_bytes()
    validation_bytes = validation_path.read_bytes()
    candidate = json.loads(candidate_bytes.decode("utf-8"))
    validation = json.loads(validation_bytes.decode("utf-8"))
    digest = str(candidate.get("image_digest") or "").lower()
    candidate_repository = resolve_image_repository(repository, candidate.get("image_repository"))
    requested_repository = resolve_image_repository(repository, image_repository) if image_repository else candidate_repository
    if requested_repository != candidate_repository:
        raise RuntimeError("QA candidate image repository does not match the requested release repository")
    image_repository = candidate_repository
    image_ref = f"{candidate_repository}@{digest}"
    expected_candidate = {
        "full_commit_sha": commit,
        "version": contract["version"],
        "release_channel": "qa",
        "image_repository": candidate_repository,
        "workflow_run_id": str(workflow_run_id),
    }
    candidate_mismatches = {
        key: {"expected": value, "actual": candidate.get(key)}
        for key, value in expected_candidate.items()
        if str(candidate.get(key)) != str(value)
    }
    if candidate_mismatches or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError(f"QA candidate evidence mismatch: {candidate_mismatches or {'image_digest': digest}}")
    expected_validation = {
        "schema": VALIDATION_SCHEMA,
        "passed": True,
        "commit": commit,
        "version": contract["version"],
        "release_tag": contract["tag"],
        "image_digest": digest,
        "image_ref": image_ref,
        "workflow_run_id": str(workflow_run_id),
        "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "previous_state_schema_version": contract["previous_state_schema_version"],
        "target_state_schema_version": int(candidate.get("state_schema_version") or 0),
    }
    validation_mismatches = {
        key: {"expected": value, "actual": validation.get(key)}
        for key, value in expected_validation.items()
        if validation.get(key) != value
    }
    if validation_mismatches:
        raise RuntimeError(f"QA image validation evidence mismatch: {validation_mismatches}")
    block = "\n".join([
        VERIFIED_START,
        "### Verified QA build",
        f"- Commit: `{commit}`",
        f"- Immutable image: `{image_ref}`",
        f"- Workflow run: [GitHub Actions run {workflow_run_id}](https://github.com/{repository}/actions/runs/{workflow_run_id})",
        "- Deployment guidance: pin the immutable image digest above; never deploy the moving `qa` tag as release evidence.",
        VERIFIED_END,
    ])
    if len(block.encode("utf-8")) > 2048:
        raise RuntimeError("verified QA build block exceeded its 2-KiB bound")
    assets = {
        "qa-candidate.json": candidate_bytes,
        "qa-image-validation.json": validation_bytes,
        "inkdrop-closed-alpha-compose.zip": inkdrop_closed_alpha_packet.build_packet_bytes(candidate_path),
    }
    if update_manifest_path:
        update_bytes = Path(update_manifest_path).read_bytes()
        if not update_bytes or len(update_bytes) > inkdrop_version.UPDATE_MANIFEST_MAX_BYTES:
            raise RuntimeError("update manifest asset is empty or exceeds 64 KiB")
        try:
            update = inkdrop_version.validate_update_manifest(json.loads(update_bytes.decode("utf-8")))
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"update manifest asset is invalid: {exc}") from exc
        expected_update = {
            "version": contract["version"],
            "commit_sha": commit,
            "image_digest": digest,
            "image_repository": image_repository,
            "release_url": f"https://github.com/{repository}/releases/tag/{contract['tag']}",
            "channel": "qa",
            "validated": True,
        }
        mismatches = {key: {"expected": value, "actual": update.get(key)} for key, value in expected_update.items() if update.get(key) != value}
        if mismatches:
            raise RuntimeError(f"update manifest evidence mismatch: {mismatches}")
        assets["inkdrop-update-manifest.json"] = update_bytes
    return {
        "body": contract["notes"] + "\n\n" + block,
        "assets": assets,
        "image_digest": digest,
    }


def build_update_manifest(candidate_path, validation_path, contract, repository, commit, workflow_run_id, image_repository=None):
    evidence = load_verified_evidence(candidate_path, validation_path, contract, repository, commit, workflow_run_id, image_repository=image_repository)
    candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    validation = json.loads(Path(validation_path).read_text(encoding="utf-8"))
    notes_summary = " ".join(line.strip("# ") for line in contract["notes"].splitlines() if line.strip())[:2000]
    payload = {
        "schema_version": inkdrop_version.UPDATE_MANIFEST_SCHEMA_VERSION,
        "product": "InkDrop",
        "published_at": inkdrop_version.normalize_update_timestamp(validation.get("validated_at")),
        "channel": "qa",
        "version": contract["version"],
        "prerelease": True,
        "validated": True,
        "release_url": f"https://github.com/{repository}/releases/tag/{contract['tag']}",
        "commit_sha": commit,
        "image_repository": resolve_image_repository(repository, image_repository),
        "image_digest": evidence["image_digest"],
        "minimum_supported_version": "0.1.0-alpha.1",
        "minimum_schema": 1,
        "maximum_schema": int(validation["target_state_schema_version"]),
        "database_migration": validation["previous_state_schema_version"] != validation["target_state_schema_version"],
        "restart_required": True,
        "release_notes_summary": notes_summary,
        "installation_notes": "Pin this exact image digest in Dockhand, Docker Compose, or Portainer and recreate inkdrop and inkdrop-worker together.",
        "rollback_notes": "Keep the previous immutable digest and a pre-update backup; recreate both services with that digest if verification fails.",
    }
    payload["manifest_identity"] = inkdrop_version.update_manifest_identity(payload)
    return inkdrop_version.validate_update_manifest(payload)


def sync_release_assets(api, release, assets):
    if not release.get("id") or not release.get("upload_url"):
        raise RuntimeError("GitHub release response omitted asset endpoints")
    existing = list(release.get("assets") or [])
    for name, content in assets.items():
        matches = [asset for asset in existing if asset.get("name") == name]
        keep = None
        for asset in matches:
            if keep is None and api.download_asset(asset["id"]) == content:
                keep = asset
            else:
                api.delete_asset(asset["id"])
        if keep is None:
            keep = api.upload_asset(release["upload_url"], name, content)
        downloaded = api.download_asset(keep["id"])
        if downloaded != content:
            raise RuntimeError(f"release asset verification failed for {name}")


def publish_release(contract, api, commit, evidence=None):
    commit = str(commit or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("release commit must be an exact 40-character SHA")
    tag_commit = resolved_tag_commit(api, contract["tag"])
    if tag_commit is None:
        created_ref = api.request("POST", "/git/refs", {"ref": f"refs/tags/{contract['tag']}", "sha": commit})
        created_object = created_ref.get("object") or {}
        created_sha = str(created_object.get("sha") or "").lower()
        if created_object.get("type") != "commit" or created_sha != commit:
            raise RuntimeError(
                f"GitHub returned an unexpected object after creating {contract['tag']}: "
                f"type={created_object.get('type')!r} sha={created_sha!r}; refusing to continue"
            )
        for attempt in range(TAG_READ_ATTEMPTS):
            tag_commit = resolved_tag_commit(api, contract["tag"])
            if tag_commit is not None:
                break
            if attempt + 1 < TAG_READ_ATTEMPTS:
                getattr(api, "sleep", time.sleep)(TAG_READ_DELAY_SECONDS)
        if tag_commit is None:
            raise RuntimeError(
                f"created release tag {contract['tag']} at {commit}, but GitHub did not expose the ref "
                f"after {TAG_READ_ATTEMPTS} bounded verification attempts; refusing to publish"
            )
    if tag_commit != commit:
        raise RuntimeError(f"release tag {contract['tag']} points to {tag_commit}, expected {commit}; refusing to rewrite history")
    encoded = urllib.parse.quote(contract["tag"], safe="")
    release = api.request("GET", f"/releases/tags/{encoded}", allow_missing=True)
    payload = {
        "tag_name": contract["tag"],
        "name": contract["name"],
        "body": evidence["body"] if evidence else contract["notes"],
        "draft": False,
        "prerelease": True,
        "make_latest": "false",
    }
    if release is None:
        payload["target_commitish"] = commit
        release = api.request("POST", "/releases", payload)
    else:
        release = api.request("PATCH", f"/releases/{int(release['id'])}", payload)
    if evidence:
        sync_release_assets(api, release, evidence["assets"])
    return release


def publish_release_or_skip(contract, api, commit, evidence=None):
    """Publish, or skip cleanly if this exact tag was already published for a different commit.

    A version bump is required to give a candidate its own release tag. When
    two builds ship in a row without one, the second has nothing new to
    publish -- that's not a failure, just nothing to do, and shouldn't turn
    the whole CI run red for an otherwise-clean commit.
    """
    try:
        return publish_release(contract, api, commit, evidence), None
    except RuntimeError as exc:
        if "refusing to rewrite history" not in str(exc):
            raise
        return None, {
            "reason": "release_tag_already_published_for_a_different_commit",
            "detail": str(exc),
        }


def append_github_output(path, contract):
    with Path(path).open("a", encoding="utf-8", newline="\n") as handle:
        for key in ("version", "tag", "name", "notes_path", "previous_state_schema_version"):
            handle.write(f"{key}={contract[key]}\n")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--github-output")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--candidate")
    parser.add_argument("--validation")
    parser.add_argument("--update-manifest")
    parser.add_argument("--generate-update-manifest-output")
    parser.add_argument("--image-repository", default=None)
    parser.add_argument("--workflow-run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    args = parser.parse_args(argv)
    contract = load_contract(args.contract)
    if args.github_output:
        append_github_output(args.github_output, contract)
    result = None
    evidence = None
    if bool(args.candidate) != bool(args.validation):
        raise ValueError("candidate and validation evidence must be supplied together")
    if args.candidate:
        evidence = load_verified_evidence(
            args.candidate,
            args.validation,
            contract,
            args.repository,
            str(args.commit or "").lower(),
            args.workflow_run_id,
            args.update_manifest,
            args.image_repository,
        )
        if args.generate_update_manifest_output:
            update_payload = build_update_manifest(
                args.candidate, args.validation, contract, args.repository,
                str(args.commit or "").lower(), args.workflow_run_id,
                args.image_repository,
            )
            output = Path(args.generate_update_manifest_output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(update_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elif args.generate_update_manifest_output:
        raise ValueError("verified candidate and validation evidence are required to generate update metadata")
    if args.publish:
        if evidence is None:
            raise ValueError("verified candidate and validation evidence are required for publication")
        result, skip = publish_release_or_skip(
            contract, GitHubApi(args.repository, os.environ.get("GITHUB_TOKEN", "")), args.commit, evidence
        )
        if skip:
            print(json.dumps(
                {"ok": True, "version": contract["version"], "tag": contract["tag"], "published": False, "skipped": True, **skip},
                sort_keys=True,
            ))
            return 0
    print(json.dumps({"ok": True, "version": contract["version"], "tag": contract["tag"], "published": bool(result)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
