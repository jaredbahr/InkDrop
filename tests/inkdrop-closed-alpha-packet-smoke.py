#!/usr/bin/env python3
"""Contract coverage for the package-only closed-alpha release asset."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from tools import inkdrop_closed_alpha_packet as packet


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-closed-alpha-packet-") as tmp:
        candidate_path = Path(tmp) / "qa-candidate.json"
        digest = "sha256:" + "b" * 64
        candidate = {
            "schema": "inkdrop.qa_candidate.v1",
            "manifest_schema_version": 1,
            "branch": "qa",
            "full_commit_sha": "a" * 40,
            "short_sha": "a" * 12,
            "version": "0.1.0-alpha.67",
            "release_channel": "qa",
            "build_date": "2026-07-20T10:23:29Z",
            "image_repository": "ghcr.io/jaredbahr/inkdrop-qa",
            "image_tag": "ghcr.io/jaredbahr/inkdrop-qa:qa-aaaaaaaaaaaa",
            "image_digest": digest,
            "workflow_run_id": "29734597335",
            "qa_build_number": 391,
            "state_schema_version": 17,
        }
        candidate_path.write_text(json.dumps(candidate, sort_keys=True) + "\n", encoding="utf-8")
        source_candidate = candidate_path.read_bytes()
        first = packet.build_packet_bytes(candidate_path)
        second = packet.build_packet_bytes(candidate_path)
        require(first == second, "packet bytes must be deterministic")
        require(len(first) <= packet.MAX_PACKET_BYTES, "packet exceeded size bound")
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            names = set(archive.namelist())
            root = packet.PACKET_NAME + "/"
            require(names == {
                root + ".env",
                root + "README.md",
                root + "compose.yaml",
                root + "state/release/qa-candidate.json",
            }, names)
            compose = archive.read(root + "compose.yaml").decode()
            env = archive.read(root + ".env").decode()
            guide = archive.read(root + "README.md").decode()
            embedded_bytes = archive.read(root + "state/release/qa-candidate.json")
            embedded = json.loads(embedded_bytes)
        image = f"ghcr.io/jaredbahr/inkdrop-qa@{digest}"
        require("build:" not in compose and compose.count("image: ${INKDROP_IMAGE") == 2, compose)
        require("INKDROP_WORKER_IMAGE_DIGEST" in compose and "INKDROP_CANDIDATE_MANIFEST_PATH" in compose, compose)
        require("INKDROP_IMAGE_REPOSITORY" in compose and "INKDROP_STATE_SCHEMA_VERSION" in compose, compose)
        require(f"INKDROP_IMAGE={image}" in env and "INKDROP_EXPECTED_COMMIT=" + "a" * 40 in env, env)
        require("read:packages" in guide and "--password-stdin" in guide, guide)
        require("owner-granted access to the InkDrop package" in guide and "SSO" in guide and "authorization if" in guide, guide)
        require("repository read access" not in guide, guide)
        require("docker compose config --images" in guide, guide)
        require('docker image inspect "$EXPECTED_IMAGE"' in guide and "candidate_manifest_status" in guide, guide)
        require('p["qa_build_number"]' in guide and 'p["state_schema_version"]' in guide, guide)
        require('p["release_channel"] == c["release_channel"]' in guide, guide)
        require('"INKDROP_EXPECTED_CHANNEL"' in guide and '"INKDROP_EXPECTED_SCHEMA"' in guide, guide)
        require('"$INKDROP_IMAGE_DIGEST"' not in guide and '"$INKDROP_EXPECTED_COMMIT"' not in guide, guide)
        require("docker compose port inkdrop 8796" in guide and "$INKDROP_BIND_ADDRESS" not in guide, guide)
        require("backup --label pre-update" in guide and 'assert p["ok"]' in guide, guide)
        require(guide.index("docker compose stop inkdrop-worker inkdrop") < guide.index('cp "$NEW_PACKET/compose.yaml"'), guide)
        require("docker compose pull inkdrop inkdrop-worker" in guide, guide)
        require("docker compose up -d --force-recreate --wait --wait-timeout 120 inkdrop inkdrop-worker" in guide, guide)
        require("merge_release_env" in guide and "operator settings and credentials remain untouched" in guide, guide)
        require('cp "$NEW_PACKET/.env" "$INSTALL_ROOT/.env"' not in guide, guide)
        require('cp "$PRIOR_PACKET/.env" "$INSTALL_ROOT/.env"' not in guide, guide)
        require("INKDROP_PROWLARR_URL" not in guide and "INKDROP_PROWLARR_API_KEY" not in guide, guide)
        require("pragma quick_check" in guide and "foreign_key_check" in guide, guide)
        require("Do not copy the database or media" in guide and "matching pre-update backup" in guide, guide)
        require("cp -a" not in guide and "down -v" in guide, guide)
        bash = shutil.which("bash")
        if bash:
            documented_shell = "\n".join(re.findall(r"```bash\r?\n(.*?)```", guide, flags=re.DOTALL)).replace("\r", "")
            # Git Bash receives text-mode Windows pipes as CRLF even when the
            # source contract is LF. Send bytes so Bash validates the actual
            # documented shell rather than Python's platform newline rewrite.
            syntax = subprocess.run([bash, "-n"], input=documented_shell.encode("utf-8"), capture_output=True)
            require(syntax.returncode == 0, syntax.stderr.decode("utf-8", errors="replace"))
        require(embedded == candidate, embedded)
        require(embedded_bytes != source_candidate, "packet embedded raw candidate bytes")
        bad = dict(candidate, image_digest="latest")
        candidate_path.write_text(json.dumps(bad), encoding="utf-8")
        try:
            packet.build_packet_bytes(candidate_path)
        except ValueError as exc:
            require("immutable" in str(exc), exc)
        else:
            raise AssertionError("mutable image identity entered a release packet")
        bad = dict(candidate, version="0.1.0-alpha.67\nINKDROP_IMAGE=attacker")
        candidate_path.write_text(json.dumps(bad), encoding="utf-8")
        try:
            packet.build_packet_bytes(candidate_path)
        except ValueError as exc:
            require("version" in str(exc), exc)
        else:
            raise AssertionError("environment-line injection entered a release packet")
        bad = dict(candidate, support_token="ghp_should_never_ship")
        candidate_path.write_text(json.dumps(bad), encoding="utf-8")
        try:
            packet.build_packet_bytes(candidate_path)
        except ValueError as exc:
            require("unknown" in str(exc) and "support_token" in str(exc), exc)
        else:
            raise AssertionError("unknown secret field entered the public packet")
    print("closed-alpha packet smoke passed")


if __name__ == "__main__":
    main()
