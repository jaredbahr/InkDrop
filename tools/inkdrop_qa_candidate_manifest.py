#!/usr/bin/env python3
"""Create the immutable QA candidate manifest after the registry returns a digest."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path


SCHEMA = "inkdrop.qa_candidate.v1"
SCHEMA_VERSION = 1


def valid_timestamp(value):
    candidate = str(value or "").strip()
    datetime.fromisoformat(candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate)
    return candidate


def build_manifest(args):
    commit = str(args.commit_sha or "").strip().lower()
    digest = str(args.image_digest or "").strip().lower()
    image_tag = str(args.image_tag or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("commit SHA must contain exactly 40 hexadecimal characters")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("image digest must be an immutable sha256 digest")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+:[a-z0-9_.-]+", image_tag):
        raise ValueError("image tag must identify one GitHub Container Registry repository and tag")
    image_repository = image_tag.rsplit(":", 1)[0]
    if int(args.qa_build_number) <= 0:
        raise ValueError("QA build number must be a positive integer")
    return {
        "schema": SCHEMA,
        "manifest_schema_version": SCHEMA_VERSION,
        "branch": str(args.branch or "").strip(),
        "full_commit_sha": commit,
        "short_sha": commit[:12],
        "version": str(args.version or "").strip(),
        "release_channel": str(args.release_channel or "").strip().lower(),
        "build_date": valid_timestamp(args.build_date),
        "image_repository": image_repository,
        "image_tag": image_tag,
        "image_digest": digest,
        "workflow_run_id": str(args.workflow_run_id or "").strip(),
        "qa_build_number": int(args.qa_build_number),
        "state_schema_version": int(args.state_schema_version),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--branch", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-channel", required=True)
    parser.add_argument("--build-date", required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--qa-build-number", required=True, type=int)
    parser.add_argument("--state-schema-version", required=True, type=int)
    parser.add_argument("--output", default="qa-candidate.json")
    args = parser.parse_args(argv)
    payload = build_manifest(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
