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


def main():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "public"
        staged_export(target)
        assert_manifest_matches_tree(target)
        assert_canaries_run(target)
    print("PUBLIC_EXPORT_RUNNABLE_OK: staged export runs and its manifest matches the tree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
