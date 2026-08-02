#!/usr/bin/env python3

import json
import tempfile
from argparse import Namespace
from pathlib import Path

import inkdrop_version
from tools import inkdrop_qa_candidate_manifest as manifest_tool


COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64


def manifest_args(output):
    return Namespace(
        branch="qa",
        commit_sha=COMMIT,
        version="0.1.0-alpha.0",
        release_channel="qa",
        build_date="2026-07-11T13:36:09Z",
        image_tag="ghcr.io/example/inkdrop-qa:qa-aaaaaaaaaaaa",
        image_digest=DIGEST,
        workflow_run_id="12345",
        qa_build_number=43,
        state_schema_version=12,
        output=str(output),
    )


with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "release" / "current.json"
    payload = manifest_tool.build_manifest(manifest_args(path))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    env = {
        "INKDROP_VERSION": "0.1.0-alpha.0",
        "INKDROP_COMMIT_SHA": COMMIT,
        "INKDROP_BUILD_DATE": "2026-07-11T13:36:09Z",
        "INKDROP_RELEASE_CHANNEL": "qa",
        "INKDROP_IMAGE_DIGEST": DIGEST,
        "INKDROP_IMAGE_REPOSITORY": "ghcr.io/example/inkdrop-qa",
        "INKDROP_QA_BUILD_NUMBER": "43",
        "INKDROP_STATE_SCHEMA_VERSION": "12",
        "INKDROP_CANDIDATE_MANIFEST_PATH": str(path),
    }
    metadata = inkdrop_version.build_metadata(env)
    assert metadata["candidate_manifest_status"] == "matched", metadata
    assert metadata["image_digest"] == DIGEST, metadata
    assert metadata["candidate_branch"] == "qa", metadata
    assert metadata["candidate_workflow_run_id"] == "12345", metadata
    assert metadata["state_schema_version"] == 12, metadata
    assert metadata["qa_build_number"] == 43, metadata
    assert metadata["candidate_manifest"]["qa_build_number"] == 43, metadata
    assert metadata["candidate_manifest"]["image_repository"] == "ghcr.io/example/inkdrop-qa", metadata
    assert metadata["display_version"] == "0.1.0-alpha.0", metadata
    assert metadata["oci"]["io.inkdrop.qa.build-number"] == "43", metadata

    mismatches = {
        "INKDROP_VERSION": "0.1.0-alpha.99",
        "INKDROP_BUILD_DATE": "2026-07-12T13:36:09Z",
        "INKDROP_RELEASE_CHANNEL": "beta",
        "INKDROP_IMAGE_DIGEST": "sha256:" + "c" * 64,
        "INKDROP_IMAGE_REPOSITORY": "ghcr.io/example/other",
        "INKDROP_QA_BUILD_NUMBER": "44",
        "INKDROP_STATE_SCHEMA_VERSION": "13",
    }
    expected_fields = {
        "INKDROP_VERSION": "version",
        "INKDROP_BUILD_DATE": "build_date",
        "INKDROP_RELEASE_CHANNEL": "release_channel",
        "INKDROP_IMAGE_DIGEST": "image_digest",
        "INKDROP_IMAGE_REPOSITORY": "image_repository",
        "INKDROP_QA_BUILD_NUMBER": "qa_build_number",
        "INKDROP_STATE_SCHEMA_VERSION": "state_schema_version",
    }
    for env_key, bad_value in mismatches.items():
        bad_env = dict(env, **{env_key: bad_value})
        mismatch = inkdrop_version.build_metadata(bad_env)
        assert mismatch["candidate_manifest_status"] == "mismatch", (env_key, mismatch)
        assert expected_fields[env_key] in mismatch["candidate_manifest_mismatches"], (env_key, mismatch)
        if env_key == "INKDROP_IMAGE_DIGEST":
            assert mismatch["image_digest"] == bad_value, mismatch

    invalid_args = manifest_args(path)
    invalid_args.image_tag = "docker.io/example/inkdrop-qa:qa-aaaaaaaaaaaa"
    try:
        manifest_tool.build_manifest(invalid_args)
    except ValueError:
        pass
    else:
        raise AssertionError("non-GHCR candidate image tag was accepted")

    path.write_text(json.dumps(dict(payload, full_commit_sha="d" * 40)), encoding="utf-8")
    metadata = inkdrop_version.build_metadata(env)
    assert metadata["candidate_manifest_status"] == "stale", metadata
    assert metadata["image_digest"] == env["INKDROP_IMAGE_DIGEST"], metadata
    assert "candidate_manifest" not in metadata, metadata

    path.write_text("not-json", encoding="utf-8")
    assert inkdrop_version.build_metadata(env)["candidate_manifest_status"] == "invalid"
    path.unlink()
    assert inkdrop_version.build_metadata(env)["candidate_manifest_status"] == "missing"

workflow = Path(".github/workflows/inkdrop-public-release.yml").read_text(encoding="utf-8")
for required in (
    "inkdrop_qa_candidate_manifest.py",
    "qa-candidate.json",
    "inkdrop-qa-candidate-",
    "steps.build.outputs.digest",
    "github.run_id",
    "github.run_number",
    "INKDROP_QA_BUILD_NUMBER",
):
    assert required in workflow, required

print("inkdrop QA candidate manifest smoke: PASS")
