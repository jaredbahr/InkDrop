#!/usr/bin/env python3
"""Run every tracked repo-root smoke test, honoring a documented skip list.

The qa workflow's path filter fires on any inkdrop*.py change, but until now
the workflow executed exactly one repo-root smoke -- a green check meant one
test passed, not that the suite did. This runner executes all of them.

Every skip below must carry a reason and a pointer. A skipped test that
passes is reported so it can be un-skipped.
"""

import os
import subprocess
import sys
import time

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
    "inkdrop-public-docker-runtime-smoke.py": (
        "builds and boots the docker image; ~4 minutes when docker is present "
        "and the qa_image job already exercises the real container build"
    ),
}

PER_TEST_TIMEOUT = int(os.environ.get("INKDROP_SMOKE_SUITE_PER_TEST_TIMEOUT", "420"))


def tracked_smokes():
    out = subprocess.run(
        ["git", "ls-files", "inkdrop-*smoke*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(name for name in out.stdout.splitlines() if name.strip())


def main():
    names = tracked_smokes()
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
            )
            outcome = "ok" if proc.returncode == 0 else f"rc={proc.returncode}"
            tail = (proc.stdout + proc.stderr)[-1500:]
        except subprocess.TimeoutExpired as exc:
            proc = None
            outcome = f"timeout>{PER_TEST_TIMEOUT}s"
            tail = ((exc.stdout or "") + (exc.stderr or ""))[-1500:] if isinstance(exc.stdout, str) else ""
        elapsed = time.time() - t0
        if name in SKIP:
            if proc is not None and proc.returncode == 0:
                skipped_passing.append(name)
                print(f"{label}: {outcome} in {elapsed:.1f}s (on skip list but PASSING -- un-skip it)")
            else:
                print(f"{label}: {outcome} in {elapsed:.1f}s (skipped: {SKIP[name]})")
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
