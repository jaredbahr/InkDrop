#!/usr/bin/env python3
"""Run every tracked repo-root smoke test, honoring a documented skip list.

The qa workflow's path filter fires on any inkdrop*.py change, but until now
the workflow executed exactly one repo-root smoke -- a green check meant one
test passed, not that the suite did. This runner executes all of them.

Every skip below must carry a reason and a pointer. A skipped test that
passes is reported so it can be un-skipped.
"""

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# This script lives at tools/, so its repo root is one level up -- fixed
# regardless of where the smoke scripts tracked_smokes() discovers actually
# live (root or tests/).
ROOT = Path(__file__).resolve().parents[1]


def subprocess_env():
    """Run child scripts with the repo root importable.

    A relocated script's own directory is what Python puts on sys.path, so a
    test under tests/ cannot import the root modules it exercises without
    this (same problem and fix as tools/inkdrop_public_release_check.py's
    check_env()).
    """
    resolved = dict(os.environ)
    existing = resolved.get("PYTHONPATH", "")
    root = str(ROOT)
    if root not in existing.split(os.pathsep):
        resolved["PYTHONPATH"] = os.pathsep.join([root, existing]) if existing else root
    return resolved

# INKDROP_STATE_DIR and friends default to a real, persistent path
# (inkdrop_runtime_config.state_dir()'s own fallback) when unset. CI always
# sets these explicitly via the workflow, but a local run of this script --
# by a developer or an agent verifying a fix, exactly the kind of run that
# polluted the real state dir on a dev machine for over a week before this
# fix -- would otherwise run all ~265 smoke tests against real application
# state. Default to an isolated temp root unless the caller already set
# INKDROP_STATE_DIR themselves (CI's explicit exports are left untouched).
_STATE_ENV_VARS = (
    "INKDROP_CONFIG_DIR",
    "INKDROP_STATE_DIR",
    "INKDROP_LOCK_DIR",
    "INKDROP_LOG_DIR",
    "INKDROP_CACHE_DIR",
    "INKDROP_BACKUP_DIR",
    "INKDROP_STAGING_DIR",
    "INKDROP_MANUAL_INBOX_DIR",
    "INKDROP_QUARANTINE_DIR",
)


def ensure_isolated_state_env():
    if "INKDROP_STATE_DIR" in os.environ:
        return
    root = tempfile.mkdtemp(prefix="inkdrop-smoke-suite-")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    layout = {
        "INKDROP_CONFIG_DIR": "config",
        "INKDROP_STATE_DIR": "state",
        "INKDROP_LOCK_DIR": "state/locks",
        "INKDROP_LOG_DIR": "state/logs",
        "INKDROP_CACHE_DIR": "state/cache",
        "INKDROP_BACKUP_DIR": "state/backups",
        "INKDROP_STAGING_DIR": "staging",
        "INKDROP_MANUAL_INBOX_DIR": "manual-inbox",
        "INKDROP_QUARANTINE_DIR": "state/quarantine",
    }
    for var, rel in layout.items():
        path = os.path.join(root, rel)
        os.makedirs(path, exist_ok=True)
        os.environ[var] = path


# name -> reason. Keep this list SHORT and every entry justified.
SKIP = {
    "inkdrop-settings-backup-browser-smoke.py": (
        "drives a real browser: needs playwright plus a live authenticated "
        "instance; run locally against a throwaway instance instead"
    ),
    "inkdrop-settings-setup-prowlarr-browser-smoke.py": (
        "drives a real browser: needs playwright plus a live authenticated "
        "instance; run locally against a throwaway instance instead"
    ),
    "inkdrop-manual-review-load-browser-smoke.py": (
        "drives a real browser: needs playwright plus a live authenticated "
        "instance; run locally against a throwaway instance instead"
    ),
    "inkdrop-blocklist-react-island-browser-smoke.py": (
        "drives a real browser: needs playwright, a built web/frontend React "
        "bundle, and a live authenticated instance; run locally against a "
        "throwaway instance instead"
    ),
    "inkdrop-wanted-react-island-browser-smoke.py": (
        "drives a real browser: needs playwright, a built web/frontend React "
        "bundle, and a live authenticated instance; run locally against a "
        "throwaway instance instead"
    ),
    "inkdrop-queue-react-island-browser-smoke.py": (
        "drives a real browser: needs playwright, a built web/frontend React "
        "bundle, and a live authenticated instance; run locally against a "
        "throwaway instance instead"
    ),
    "inkdrop-history-react-island-browser-smoke.py": (
        "drives a real browser: needs playwright, a built web/frontend React "
        "bundle, and a live authenticated instance; run locally against a "
        "throwaway instance instead"
    ),
    "inkdrop-manual-review-react-island-browser-smoke.py": (
        "drives a real browser: needs playwright, a built web/frontend React "
        "bundle, and a live authenticated instance; run locally against a "
        "throwaway instance instead"
    ),
    "inkdrop-series-react-island-browser-smoke.py": (
        "drives a real browser: needs playwright, a built web/frontend React "
        "bundle, and a live authenticated instance; run locally against a "
        "throwaway instance instead"
    ),
    "inkdrop-public-docker-runtime-smoke.py": (
        "builds and boots the docker image; ~4 minutes when docker is present "
        "and the qa_image job already exercises the real container build"
    ),
}

PER_TEST_TIMEOUT = int(os.environ.get("INKDROP_SMOKE_SUITE_PER_TEST_TIMEOUT", "420"))

# A pathspec typo or a future file-layout change (scripts moving to another
# directory the pathspec below doesn't cover) makes tracked_smokes() return an
# empty or near-empty list -- main() would otherwise happily run zero tests,
# print "0 failed", and exit 0. This is intentionally well below the real
# count (373 at the time this guard was added) so ordinary test churn never
# trips it, but any layout mistake that silently drops most of the suite will.
MIN_EXPECTED_SMOKE_COUNT = 300


def tracked_smokes():
    out = subprocess.run(
        ["git", "ls-files", "tests/inkdrop-*smoke*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(name for name in out.stdout.splitlines() if name.strip())


def main():
    ensure_isolated_state_env()
    names = tracked_smokes()
    if len(names) < MIN_EXPECTED_SMOKE_COUNT:
        print(
            f"discovered only {len(names)} smoke tests via 'git ls-files tests/inkdrop-*smoke*.py' "
            f"(expected at least {MIN_EXPECTED_SMOKE_COUNT}) -- the discovery pathspec is almost "
            "certainly broken (wrong directory, typo, or a layout change this glob doesn't cover). "
            "Refusing to report a false-green result."
        )
        return 1
    failures = []
    skipped_passing = []
    started = time.time()
    for index, name in enumerate(names, start=1):
        label = f"[{index}/{len(names)}] {name}"
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, "-B", name],
                capture_output=True,
                text=True,
                timeout=PER_TEST_TIMEOUT,
                env=subprocess_env(),
            )
            outcome = "ok" if proc.returncode == 0 else f"rc={proc.returncode}"
            tail = (proc.stdout + proc.stderr)[-1500:]
        except subprocess.TimeoutExpired as exc:
            proc = None
            outcome = f"timeout>{PER_TEST_TIMEOUT}s"
            tail = ((exc.stdout or "") + (exc.stderr or ""))[-1500:] if isinstance(exc.stdout, str) else ""
        elapsed = time.time() - t0
        # SKIP is keyed by basename, not the tracked_smokes() path (which
        # carries whatever directory prefix the scripts currently live
        # under) -- same reasoning as inkdrop_public_release_check.py's
        # EXPORT_SKIPPED_CHECKS, so a future relocation can't silently
        # empty this list out by making every key stop matching.
        base = os.path.basename(name)
        if base in SKIP:
            if proc is not None and proc.returncode == 0:
                skipped_passing.append(name)
                print(f"{label}: {outcome} in {elapsed:.1f}s (on skip list but PASSING -- un-skip it)")
            else:
                print(f"{label}: {outcome} in {elapsed:.1f}s (skipped: {SKIP[base]})")
            continue
        if proc is None or proc.returncode != 0:
            failures.append((name, outcome, tail))
            print(f"{label}: FAILED ({outcome}) in {elapsed:.1f}s")
        else:
            print(f"{label}: ok in {elapsed:.1f}s")

    print(f"\nsuite: {len(names)} tests, {len(failures)} failed, {len(SKIP)} skipped, {time.time() - started:.0f}s total")
    if skipped_passing:
        print("skip-list entries that passed (remove them):")
        for name in skipped_passing:
            print(f"  - {name}")
    if failures:
        print("\nfailures:")
        for name, outcome, tail in failures:
            print(f"\n=== {name} ({outcome})")
            print(tail)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
