#!/usr/bin/env python3
"""Execute the public export instead of only reading it.

Every public-repo defect this guard exists for was invisible to a reader. The
export moved tests into tests/ and every one of the 109 stopped running -- bare
imports could not see the root modules, and the tests that resolve files
relative to their own location started looking inside tests/. Nothing failed,
because nothing ran the exported tree. Twenty-two internal release notes shipped
the same way.

So this stages a real export and runs code out of it. The canaries are chosen
for the two things relocation breaks: importing a root module, and finding a
file by path. Running the whole suite here would take minutes; these fail for
the same reason the other hundred would.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import inkdrop_public_repo_export as exporter

# One canary that imports root modules, one that reads a sibling file by path,
# one that walks docs, and one that reads the release contract.
CANARIES = (
    "inkdrop-backup-restore-smoke.py",
    "inkdrop-queue-claim-smoke.py",
    "inkdrop-release-notes-version-smoke.py",
    "inkdrop-db-boundary-smoke.py",
    # Tracker #141's clause 1 is "the benchmark scorer, corpus fixture and doc
    # run from a staged public export by someone outside the project". That was
    # answered by hand at closure and again on 2026-08-30, and NOTHING ran it
    # here in between -- the gate is in the export allowlist, so a change that
    # dropped it, or that left the corpus behind, would have shipped an export
    # whose release gate cannot run. It exercises the matcher over the whole
    # corpus and costs under a second.
    "inkdrop-candidate-matching-benchmark-smoke.py",
)


def fail(message):
    raise AssertionError(message)


def staged_export(target):
    result = exporter.run_export(target=target, apply=True, force=True)
    if not result.get("ok"):
        fail(f"export refused to stage: missing={result.get('missing')} forbidden={result.get('forbidden')}")
    return result


def assert_manifest_matches_tree(target):
    """The manifest has to describe the tree exactly, in both directions.

    A published manifest already drifted once: files were removed from the repo
    and the manifest kept listing them, so it claimed 251 files against a tree
    of 230.
    """
    manifest = json.loads((target / "PUBLIC_REPO_MANIFEST.json").read_text(encoding="utf-8"))
    listed = {entry["path"] for entry in manifest["files"]}
    on_disk = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file() and path.name != "PUBLIC_REPO_MANIFEST.json"
    }
    missing = sorted(listed - on_disk)
    unlisted = sorted(on_disk - listed)
    if missing:
        fail(f"manifest lists files the export does not contain: {missing[:5]}")
    if unlisted:
        fail(f"export contains files the manifest does not list: {unlisted[:5]}")
    if manifest["file_count"] != len(on_disk):
        fail(f"manifest file_count {manifest['file_count']} != {len(on_disk)} files on disk")


def assert_canaries_run(target):
    env = dict(os.environ)
    root = str(target)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([root, existing]) if existing else root
    for name in CANARIES:
        candidates = [target / name, target / "tests" / name]
        script = next((path for path in candidates if path.is_file()), None)
        if script is None:
            fail(f"canary is not in the export at all: {name}")
        finished = subprocess.run(
            [sys.executable, "-B", str(script)],
            cwd=target,
            env=env,
            text=True,
            capture_output=True,
            timeout=300,
        )
        if finished.returncode != 0:
            tail = (finished.stderr or finished.stdout or "").strip().splitlines()
            fail(f"exported {name} does not run: {tail[-1] if tail else 'no output'}")


def assert_install_guide_matches_published_compose(target):
    """The install guide must not tell a reader to run what the shipped file cannot do.

    The working tree's own docker-first-install.md is written against the
    development Compose file -- two services, a build: block, and ${...}
    substitution that makes a .env file meaningful. The published Compose file
    is one image-only service with none of that, and the guide shipped verbatim
    anyway. So the public copy told readers to obtain an install packet that has
    never been attached to any release, to run `docker compose up --build`
    (which silently pulls the published image instead of building anything), and
    to copy .env.example to .env (which silently reaches nothing).

    Every one of those fails without an error message, which is why they
    survived. The first two checks below are tied to the shipped Compose file
    rather than to a banned word, so they stay correct if it ever gains a
    build: block or an env_file.
    """
    guide_path = target / "docs" / "inkdrop" / "docker-first-install.md"
    if not guide_path.is_file():
        fail("the export does not carry docs/inkdrop/docker-first-install.md")
    guide = guide_path.read_text(encoding="utf-8")
    compose = (target / "docker-compose.yml").read_text(encoding="utf-8")
    lowered = guide.lower()

    # Things a public reader cannot obtain or act on at all.
    for term, why in (
        ("closed-alpha", "the closed alpha is not a thing a public reader can join"),
        ("closed alpha", "the closed alpha is not a thing a public reader can join"),
        ("inkdrop-closed-alpha-compose", "no release has ever carried an attached install packet"),
        ("read:packages", "the published image pulls anonymously; no token is needed"),
        ("docker login ghcr.io", "the published image pulls anonymously; no login is needed"),
        ("inkdrop-worker", "the published Compose file has no worker service"),
    ):
        if term in lowered:
            fail(f"published install guide references {term!r}: {why}")

    # A --build instruction is only honest if the shipped Compose file can build.
    # Scoped to fenced code blocks on purpose: prose that WARNS the reader that
    # --build will not build their checkout is exactly what this guide should
    # say, and an earlier version of this check refused that sentence too. A
    # guard that over-refuses the correct copy is as wrong as one that misses
    # the defect.
    fenced = "\n".join(re.findall(r"^```[a-z]*\n(.*?)^```", guide, flags=re.S | re.M))
    if "build:" not in compose and "--build" in fenced:
        fail(
            "published install guide tells the reader to run --build, but the "
            "published Compose file has no build: block, so the command silently "
            "pulls the published image instead of building their checkout"
        )

    # A .env instruction is only honest if the shipped Compose file reads one.
    compose_reads_env = "env_file" in compose or "${" in compose
    if not compose_reads_env:
        for phrase in ("copy `.env.example`", "copy .env.example", "to `.env`"):
            if phrase in lowered:
                fail(
                    "published install guide tells the reader to create a .env "
                    "file, but the published Compose file has neither env_file "
                    "nor variable substitution, so nothing in it reaches the "
                    "container"
                )


def main():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "public"
        staged_export(target)
        assert_manifest_matches_tree(target)
        assert_install_guide_matches_published_compose(target)
        assert_canaries_run(target)
    print("PUBLIC_EXPORT_RUNNABLE_OK: staged export runs, manifest matches the tree, "
          "and the install guide matches the Compose file it ships beside")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
