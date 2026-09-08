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
import secrets
import string
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
    # A suite exercises the body it was handed, on purpose: a branch under
    # test is a diverged body by definition. tools/tracker_guard.py refuses
    # at import to run as anything but what origin/qa ships, and would
    # otherwise fail every test importing it from a branch. The guard's own
    # smoke clears this variable and plants fixtures, so it is still tested.
    resolved.setdefault("INKDROP_ALLOW_DIVERGED_TOOL", "1")
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


def unit_marker_safe_temp_root():
    """A suite root whose path cannot be read as a unit marker.

    `mkdtemp`'s random `[a-z0-9_]` suffix can spell one: it produced
    `inkdrop-smoke-suite-_c16v3dv` on 2026-08-19, whose `_c16` reads as chapter
    16. Every smoke building a fixture under this root inherits that component,
    and the ones that score whole path strings then refuse to run -- correctly,
    since their fixture is contaminated.

    A unit marker needs a digit after its token (`c16`, `v3`, `pt2`), so a
    suffix drawn from letters only cannot produce one no matter how the letters
    fall. Deterministic, not a re-roll that could in principle spin.
    """
    alphabet = string.ascii_lowercase
    for _ in range(64):
        suffix = "".join(secrets.choice(alphabet) for _ in range(10))
        candidate = os.path.join(tempfile.gettempdir(), f"inkdrop-smoke-suite-{suffix}")
        try:
            os.mkdir(candidate)
            atexit.register(shutil.rmtree, candidate, ignore_errors=True)
        except FileExistsError:
            continue
        return candidate
    # Falling back to mkdtemp is worse than failing: it reintroduces exactly
    # the digit-bearing suffix this exists to avoid, silently.
    raise RuntimeError("could not create a unit-marker-safe smoke suite root")


def ensure_isolated_state_env():
    if "INKDROP_STATE_DIR" in os.environ:
        return
    root = unit_marker_safe_temp_root()
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


# A test has exactly three possible outcomes here, and conflating any two of
# them is how "green" stopped meaning anything:
#
#   passed      -- executed, exit 0
#   FAILED      -- executed, nonzero or timeout. Always qualifying. Always red.
#   unrunnable  -- a precondition is missing, so it is NOT executed at all
#
# The previous design had one SKIP list and consulted it *after* running the
# test, discarding the result. Run 31960962361 executed 551 scripts: 18 browser
# wrappers returned rc=1, one timed out at its 420s ceiling, one passed while
# still suppressed -- and the job printed "551 tests, 0 failed, 20 skipped" and
# exited zero. Green meant "all non-suppressed tests passed", which is not what
# anyone reads it as.
#
# So preconditions are evaluated BEFORE execution and produce `unrunnable`,
# which is reported loudly and never folded into the pass count. Anything that
# actually runs and fails is red, and there is no list that can absolve it.


def _playwright_available():
    """True when a browser-driving test could actually run in this environment.

    Probes what the tests ACTUALLY use. All 30 tests gated on this requirement
    shell out to `node web/tests/<name>.js`, and every one of those files does
    `require("playwright")` -- the NODE package. This used to probe
    `import playwright.sync_api`, the PYTHON package, which nothing in this
    repository imports.

    That mismatch fails in both directions, and the second is why it survived:

      * Python playwright present, Node's absent -> all 30 are marked runnable
        and die on MODULE_NOT_FOUND, a red that looks like a product bug.
      * Node playwright present, Python's absent -> all 30 are skipped although
        they would have passed. That is the state installing the correct
        dependency creates, so the fix would have looked like it did nothing.

    Resolution runs from the repository root because that is where the tests
    resolve `playwright` from; `node -e` resolves relative to the working
    directory, so probing from anywhere else answers a different question.
    """
    try:
        probe = subprocess.run(
            ["node", "-e", "require.resolve('playwright')"],
            capture_output=True,
            timeout=60,
            env=subprocess_env(),
            cwd=str(ROOT),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _read_denial_effective():
    """True when chmod 0 on our own file actually stops us reading it.

    A test that needs an EACCES has to be able to cause one. It cannot here in
    two common environments, and in BOTH of them the failure is silent and
    looks like a real defect: on Windows os.chmod only moves the read-only
    bit, and as root on Linux the permission check is bypassed outright. In
    each case the file stays readable, the fault under test never happens, and
    the assertion fails while reporting the wrong reason. So the environment is
    asked directly rather than inferred from os.name or geteuid.
    """
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix="inkdrop-read-denial-") as tmp:
            probe = os.path.join(tmp, "probe")
            with open(probe, "wb") as handle:
                handle.write(b"x")
            os.chmod(probe, 0o000)
            try:
                with open(probe, "rb"):
                    return False
            except PermissionError:
                return True
            except OSError:
                return False
            finally:
                try:
                    os.chmod(probe, 0o600)
                except OSError:
                    pass
    except OSError:
        return False


def _export_skip_reason(basename):
    """Why this test has no subject in the tree it is running from, or None.

    The policy itself lives in tools/inkdrop_public_release_check.py and is
    read from there rather than restated here. Two consumers with two copies is
    what produced the split this closes: the release-check tool consulted
    EXPORT_SKIPPED_CHECKS and correctly declined eleven checks whose subject the
    public export does not carry, while this runner knew nothing about it and
    ran the same eleven as ordinary tests. They failed on missing files in every
    public run -- 13 red out of 135 -- so the public suite could never be green
    and its verdict stopped being read at all.

    The predicate is the shared one: a check is declined only when the specific
    dependency it names is genuinely absent. In this repo those files exist, so
    nothing is declined here and every one of them runs for real.
    """
    try:
        import importlib.util

        path = Path(__file__).resolve().with_name("inkdrop_public_release_check.py")
        spec = importlib.util.spec_from_file_location("_inkdrop_release_check", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.export_skip_reason(basename)
    except Exception:
        # A policy this runner cannot read must not silently become "skip
        # nothing" *or* "skip everything". Returning None runs the test, which
        # is the direction that can only produce a loud failure, never a
        # false pass.
        return None


def _origin_qa_available():
    """True when the checkout carries origin/qa AND sixty commits of its history.

    actions/checkout on a pull_request fetches the merge ref alone, so a test
    that diffs against origin/qa has nothing to diff against there and exits
    non-zero before reaching its subject. A shallow checkout of qa itself is
    the same gap one step later: the ref resolves, its parent does not, and
    on 2026-09-08 the verifier smoke failed the nightly in 0.0 s on
    `git rev-parse <tip>^`. The smoke walks back up to sixty commits looking
    for a diff that touches the shipped set, so that is what is asked for.
    """
    for ref in ("origin/qa", "origin/qa~60"):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
            capture_output=True, cwd=str(ROOT),
        )
        if probe.returncode != 0:
            return False
    return True


# requirement key -> (predicate, human explanation). Evaluated once per run.
def _change_time_moves():
    """True when st_ctime advances on an in-place rewrite, which is what the
    archive-validation cache keys on. Windows reports CREATION time in that
    field, so the staleness guard under test cannot be caused there. Asked
    directly, the way _read_denial_effective asks, rather than inferred."""
    import tempfile
    import time
    try:
        with tempfile.TemporaryDirectory(prefix="inkdrop-ctime-probe-") as tmp:
            probe = os.path.join(tmp, "probe")
            with open(probe, "wb") as handle:
                handle.write(b"x")
            before = os.stat(probe).st_ctime_ns
            time.sleep(0.05)
            with open(probe, "wb") as handle:
                handle.write(b"yy")
            return os.stat(probe).st_ctime_ns > before
    except OSError:
        return False


def _symlinks_allowed():
    """True when this process may create a symbolic link. On Windows that is a
    privilege (or Developer Mode) and an ordinary session raises WinError 1314,
    so a test that stages a dangling link to make a file unreadable cannot
    stage it there."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix="inkdrop-symlink-probe-") as tmp:
            target = os.path.join(tmp, "target")
            link = os.path.join(tmp, "link")
            with open(target, "wb") as handle:
                handle.write(b"x")
            os.symlink(target, link)
            return os.path.islink(link)
    except (OSError, NotImplementedError):
        return False


def _wslpath_available():
    """True when the cron lock wrapper can be run through bash here. On POSIX
    there is nothing to convert; on Windows the smoke runs the wrapper through
    `bash -lc` and converts its paths with wslpath, which only WSL provides.
    Git Bash answers 127."""
    if os.name != "nt":
        return True
    try:
        probe = subprocess.run(["bash", "-lc", "wslpath -a /"], capture_output=True, timeout=30)
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _fcntl_available():
    """True where the fcntl module exists. core/inkdrop_slskd_staging_sweep.py
    imports it at module level for flock, so on Windows every smoke importing
    that module fails at import, before any assertion it exists for."""
    import importlib.util
    return importlib.util.find_spec("fcntl") is not None

REQUIREMENTS = {
    "origin_qa": (
        _origin_qa_available,
        "needs a git checkout that carries origin/qa and sixty commits of its history; "
        "a pull-request checkout has neither and a shallow checkout has only the ref",
    ),
    "playwright": (
        _playwright_available,
        "needs playwright plus a browser binary, which this environment does not provide",
    ),
    "read_denial": (
        _read_denial_effective,
        "needs an environment where chmod 0 actually denies a read -- Windows cannot, "
        "and root on Linux bypasses the check, so the EACCES under test cannot be caused here",
    ),
    "change_time": (
        _change_time_moves,
        "needs a filesystem whose st_ctime moves on an in-place rewrite -- Windows reports creation time there",
    ),
    "symlinks": (
        _symlinks_allowed,
        "needs permission to create a symbolic link -- an ordinary Windows session has not got it (WinError 1314)",
    ),
    "wslpath": (
        _wslpath_available,
        "needs WSL bash with wslpath to run the cron lock wrapper on Windows -- Git Bash has no wslpath",
    ),
    "fcntl": (
        _fcntl_available,
        "needs the fcntl module, which only POSIX Python provides; the staging sweep imports it for flock",
    ),
}

# basename -> requirement key. A test listed here is never executed unless its
# requirement holds; when it does hold the test runs for real and any failure
# is a failure like any other. This is not a suppression list -- membership
# here cannot hide a result, it can only decline to produce one.
REQUIRES = {
    "inkdrop-archive-validation-cache-smoke.py": "change_time",
    "inkdrop-archive-verdict-honesty-smoke.py": "symlinks",
    "inkdrop-series-autopilot-cron-lock-smoke.py": "wslpath",
    "inkdrop-slskd-staging-sweep-missing-stage-priority-smoke.py": "fcntl",
    "inkdrop-slskd-staging-sweep-raw-page-folder-smoke.py": "fcntl",
    "inkdrop-slskd-sweep-no-scan-wait-smoke.py": "fcntl",
    "inkdrop-slskd-sweep-trusted-issue-unit-smoke.py": "fcntl",
    "inkdrop-blocklist-react-island-browser-smoke.py": "playwright",
    "inkdrop-history-react-island-browser-smoke.py": "playwright",
    "inkdrop-manual-review-decision-actions-browser-smoke.py": "playwright",
    "inkdrop-manual-review-load-browser-smoke.py": "playwright",
    "inkdrop-manual-review-react-island-browser-smoke.py": "playwright",
    "inkdrop-manual-search-diagnostics-staleness-browser-smoke.py": "playwright",
    "inkdrop-mobile-contrast-browser-smoke.py": "playwright",
    "inkdrop-mobile-setup-redirect-browser-smoke.py": "playwright",
    "inkdrop-notifications-history-modal-focus-browser-smoke.py": "playwright",
    "inkdrop-pull-list-operator-week-browser-smoke.py": "playwright",
    "inkdrop-queue-react-island-browser-smoke.py": "playwright",
    "inkdrop-row-action-error-ordering-browser-smoke.py": "playwright",
    "inkdrop-series-detail-react-island-browser-smoke.py": "playwright",
    "inkdrop-series-react-island-browser-smoke.py": "playwright",
    "inkdrop-settings-backup-browser-smoke.py": "playwright",
    "inkdrop-settings-opds-browser-smoke.py": "playwright",
    "inkdrop-settings-setup-prowlarr-browser-smoke.py": "playwright",
    "inkdrop-wanted-react-island-browser-smoke.py": "playwright",
    # Drivers added when the JavaScript smokes were given runners -- see
    # tests/inkdrop-js-smoke-runner-coverage-smoke.py, which fails when a
    # web/tests/*.js has nothing that runs it. Each shells out to a playwright
    # smoke, so it runs for real wherever playwright is present.
    "inkdrop-archive-read-undetermined-not-a-content-verdict-smoke.py": "read_denial",
    # Derives its probes from a diff against origin/qa; see _origin_qa_available().
    "inkdrop-qa-autodeploy-verifier-smoke.py": "origin_qa",
    "inkdrop-activity-backend-contract-browser-smoke.py": "playwright",
    "inkdrop-arr-table-menu-browser-smoke.py": "playwright",
    "inkdrop-hidden-attribute-leak-browser-smoke.py": "playwright",
    "inkdrop-manual-review-truth-browser-smoke.py": "playwright",
    "inkdrop-missing-recovery-browser-smoke.py": "playwright",
    "inkdrop-mobile-sheet-focus-browser-smoke.py": "playwright",
    "inkdrop-sampled-history-facet-browser-smoke.py": "playwright",
    "inkdrop-settings-form-responsive-browser-smoke.py": "playwright",
    "inkdrop-system-area-load-browser-smoke.py": "playwright",
    "inkdrop-system-copy-value-browser-smoke.py": "playwright",
    "inkdrop-system-mobile-browser-smoke.py": "playwright",
    "inkdrop-series-poster-title-overflow-smoke.py": "playwright",
}

# Explicitly non-qualifying: the test still RUNS and its result is still
# printed, but it does not gate a release. Every entry carries an owner and an
# expiry, both printed on every run, and an expired entry becomes qualifying
# again automatically -- otherwise this is the old skip list wearing a new
# name, which is the exact failure this change exists to end.
#
# inkdrop-public-docker-runtime-smoke.py is deliberately absent: it passed in
# 16.9s while still suppressed, so it is simply a normal test again.
NON_QUALIFYING = {
    # --- Browser smokes, quarantined 2026-08-28 when they first ran at all.
    # Until that day nothing installed playwright, so all 30 reported
    # UNRUNNABLE and no browser assertion in this project had ever been
    # checked by automation. Enabling them turned 17 green immediately.
    # These 13 fail, and they are quarantined WITH AN EXPIRY rather than
    # held back, because the alternative was leaving 17 working render
    # assertions switched off to keep the board clean -- and a permanently
    # red Full-smoke authority is a signal nobody can read, which is worse
    # than no signal because it looks like coverage. Each still RUNS and
    # prints its red. Expiries are staggered by what the failure means:
    # content assertions and unclassified crashes first, harness gaps next,
    # stale extractions and timeouts last. See tracker row #871.
    "inkdrop-settings-form-responsive-browser-smoke.py": {
        "reason": (
            "ERR_CONNECTION_REFUSED against the same unstarted fixture server on port 8877. HARNESS GAP. UN-SUPPRESSES WHEN: the suite serves web/tests/fixtures, or the test serves its own fixture"
        ),
        "owner": "web",
        "expires": "2026-09-18",
        "issue": "tracker row #871",
    },
    "inkdrop-system-mobile-browser-smoke.py": {
        "reason": (
            "ERR_CONNECTION_REFUSED against the same unstarted fixture server on port 8877. HARNESS GAP. UN-SUPPRESSES WHEN: the suite serves web/tests/fixtures, or the test serves its own fixture"
        ),
        "owner": "web",
        "expires": "2026-09-18",
        "issue": "tracker row #871",
    },
    "inkdrop-mobile-sheet-focus-browser-smoke.py": {
        "reason": (
            "ERR_CONNECTION_REFUSED against the application on port 8796, which this test expects to be running and the suite does not start. HARNESS GAP. UN-SUPPRESSES WHEN: the test starts the app it drives, as the passing browser smokes do"
        ),
        "owner": "web",
        "expires": "2026-09-18",
        "issue": "tracker row #871",
    },
    "inkdrop-activity-backend-contract-browser-smoke.py": {
        "reason": (
            "ReferenceError for maybeHydrateSeriesDetailEditionIndifferentAction: the test extracts a function out of core/inkdrop_web.py and evaluates it in a page, and that function now calls a helper the extraction does not carry. STALE TEST following code that moved. UN-SUPPRESSES WHEN: the extraction carries the helpers its subject calls"
        ),
        "owner": "web",
        "expires": "2026-09-25",
        "issue": "tracker row #871",
    },
    "inkdrop-sampled-history-facet-browser-smoke.py": {
        "reason": (
            "ReferenceError for appendSectionRowCountChip, same extraction drift as the activity backend contract test. STALE TEST. UN-SUPPRESSES WHEN: the extraction carries the helpers its subject calls"
        ),
        "owner": "web",
        "expires": "2026-09-25",
        "issue": "tracker row #871",
    },
    "inkdrop-series-poster-title-overflow-smoke.py": {
        "reason": (
            "page.waitForFunction exceeded 30s. TIMEOUT, cause unestablished; lowest triage priority because slow is better understood than unknown. UN-SUPPRESSES WHEN: the awaited condition is reached, or the wait is re-pointed at what the page actually renders"
        ),
        "owner": "web",
        "expires": "2026-09-25",
        "issue": "tracker row #871",
    },
    "inkdrop-settings-backup-browser-smoke.py": {
        "reason": (
            "page.waitForEvent for a download exceeded 30s. TIMEOUT; note the viewer sandbox never fires a download for a page-initiated save, so the expectation itself may be wrong. UN-SUPPRESSES WHEN: the download fires, or the test asserts the payload without waiting on a browser download event"
        ),
        "owner": "web",
        "expires": "2026-09-25",
        "issue": "tracker row #871",
    },

    "inkdrop-slskd-failover-smoke.py": {
        "reason": (
            "times out at exactly the 420s per-test ceiling on GitHub Actions "
            "ubuntu-latest runners (7/7 attempts on 2026-08-08), always in the "
            "same flock/SQLite contention section; runs in 9.8s locally"
        ),
        "owner": "acquisition",
        "expires": "2026-09-15",
        "issue": "https://github.com/jaredbahr/InkDrop/issues/413",
    },
    # Wiring these three up is what proved they had been dead for weeks. Each
    # needs a judgement this wiring pass deliberately did not make, so each runs
    # and prints its red rather than being hidden. Owner and expiry assigned by
    # the wiring pass, not by the tracker row -- NON_QUALIFYING requires both.
    "inkdrop-activity-queue-blocklist-contract-smoke.py": {
        "reason": (
            "pins the Blocklist column list as [\"Series / Issue\", \"Blocked "
            "reason\", \"Source title / provider\", \"Actions\"]; the shipped view "
            "is \"Series / Issue\", \"Blocked reason\", \"Source\", \"Release "
            "candidate\", \"Actions\". UN-SUPPRESSES WHEN: the test asserts the "
            "five-column shipped literal -- which is exactly what open PR #722 "
            "does, so landing #722 requalifies this entry"
        ),
        "owner": "web",
        "expires": "2026-09-15",
        "issue": "tracker row #158",
    },
    "inkdrop-closed-alpha-user-journey-contract-smoke.py": {
        "reason": (
            "38 copy assertions pinned to wording that has since been rewritten "
            "(it wants /Monitor future releases/; core/inkdrop_web.py ships "
            "'Monitoring future releases'). UN-SUPPRESSES WHEN: all 38 "
            "assertions are re-pinned against current shipped copy -- a re-pin "
            "pass, not a one-line edit"
        ),
        "owner": "web",
        "expires": "2026-09-15",
        "issue": "tracker row #159",
    },
}

PER_TEST_TIMEOUT = int(os.environ.get("INKDROP_SMOKE_SUITE_PER_TEST_TIMEOUT", "420"))


MIN_SMOKE_DISCOVERY_RATIO = 0.80
# Absolute backstop for a tree small enough that the ratio says little. Set
# below any real tree's count, including the public export's.
MIN_EXPECTED_SMOKE_COUNT = 25


def smoke_like_files():
    """Every tracked file whose name reads as a smoke test, wherever it sits.

    Deliberately broader than tracked_smokes(): this is the denominator the
    discovery pathspec is judged against, so it must not share the pathspec's
    assumptions about naming or directory.
    """
    out = subprocess.run(
        ["git", "ls-files", "*smoke*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(name for name in out.stdout.splitlines() if name.strip())


def tracked_smokes():
    # Repo-root smoke scripts (inkdrop-*-smoke.py and inkdrop_*_smoke.py) were
    # never included here -- this glob only ever covered tests/. 8 tracked
    # root-level smoke scripts existed and were allowlisted in .gitignore but
    # never executed by any workflow or tool, some silently broken since
    # before any of them ran. "inkdrop*smoke*.py" as
    # a bare pathspec matches only the repo root, not subdirectories -- it
    # does not re-match anything tests/inkdrop-*smoke*.py already covers.
    #
    # The tests/ pathspec matches inkdrop*smoke*.py, not inkdrop-*smoke*.py:
    # the public export relocates repo-root scripts into tests/, and
    # inkdrop_download_client_ownership_smoke.py (underscore) landed there and
    # stopped matching a hyphen-only glob -- it ran in development and silently
    # vanished from the public tree.
    # tools/ was never covered either, and two smokes there were executed by
    # nothing at all: inkdrop_blocklist_allow_retry_smoke.py is referenced
    # nowhere in the repo, and inkdrop_prowlarr_indexer_health_smoke.py appears
    # only inside a COMMENT in tests/inkdrop-source-worker-jobs-smoke.py, which
    # mentions it without running it. The other two tools smokes
    # (inkdrop_public_http_smoke, inkdrop_settings_sync_smoke) are invoked by the
    # release workflows, so they were already covered there and are simply
    # covered here too.
    #
    # The pattern ends in `_smoke.py` ON PURPOSE. A looser `tools/inkdrop*smoke*.py`
    # also matches THIS FILE -- inkdrop_run_smoke_suite.py -- and the suite would
    # discover and execute itself. Checked before widening rather than after.
    out = subprocess.run(
        ["git", "ls-files", "tests/inkdrop*smoke*.py", "inkdrop*smoke*.py",
         "tools/inkdrop*_smoke.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(name for name in out.stdout.splitlines() if name.strip())


def main():
    ensure_isolated_state_env()
    names = tracked_smokes()
    present = smoke_like_files()
    expected = max(MIN_EXPECTED_SMOKE_COUNT, int(len(present) * MIN_SMOKE_DISCOVERY_RATIO))
    if len(names) < expected:
        missed = [name for name in present if name not in set(names)]
        print(
            f"discovered {len(names)} smoke tests, but the tree holds {len(present)} files that "
            f"look like smoke tests (expected at least {expected}, i.e. "
            f"{int(MIN_SMOKE_DISCOVERY_RATIO * 100)}% of them) -- the discovery pathspec is almost "
            "certainly broken (wrong directory, typo, or a layout change this glob doesn't cover). "
            "Refusing to report a false-green result."
        )
        if missed:
            print("  present but not discovered:")
            for name in missed[:20]:
                print(f"    {name}")
            if len(missed) > 20:
                print(f"    ... and {len(missed) - 20} more")
        return 1
    failures = []             # executed, failed, qualifying -> red
    non_qualifying_fails = [] # executed, failed, quarantined lane
    unrunnable = []           # precondition missing -> never executed
    expired = []              # quarantine entries past their expiry
    started = time.time()

    satisfied = {}
    for key, (predicate, explanation) in REQUIREMENTS.items():
        satisfied[key] = bool(predicate())
        state = "available" if satisfied[key] else "MISSING"
        suffix = "" if satisfied[key] else " -- " + explanation
        print("requirement " + key + ": " + state + suffix)

    today = time.strftime("%Y-%m-%d", time.gmtime())

    for index, name in enumerate(names, start=1):
        label = f"[{index}/{len(names)}] {name}"
        base = os.path.basename(name)

        # Preconditions first: a test that cannot run here is NOT executed, and
        # says so. It never touches the pass count and never touches red.
        requirement = REQUIRES.get(base)
        if requirement is not None and not satisfied.get(requirement, False):
            unrunnable.append((name, requirement))
            print(f"{label}: UNRUNNABLE (requires {requirement}) -- not executed")
            continue

        # Same treatment for a test whose subject this tree does not contain.
        # Reported as unrunnable, never as passing, on exactly the same terms:
        # the export policy names the file each one needs, and only an actually
        # missing file declines it.
        export_reason = _export_skip_reason(base)
        if export_reason is not None:
            unrunnable.append((name, export_reason))
            print(f"{label}: UNRUNNABLE ({export_reason}) -- not executed")
            continue

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
        failed = proc is None or proc.returncode != 0

        quarantine = NON_QUALIFYING.get(base)
        if quarantine is not None:
            lapsed = str(quarantine.get("expires") or "") < today
            if lapsed:
                expired.append((name, quarantine))
            if failed and not lapsed:
                non_qualifying_fails.append((name, outcome, tail, quarantine))
                print(
                    f"{label}: FAILED ({outcome}) in {elapsed:.1f}s -- NON-QUALIFYING, "
                    f"owner={quarantine.get('owner')} expires={quarantine.get('expires')}"
                )
                continue
            if not failed:
                print(
                    f"{label}: ok in {elapsed:.1f}s "
                    "(quarantined but PASSING -- remove it from NON_QUALIFYING)"
                )
                continue
            # An expired quarantine falls through and counts as a real failure.

        if failed:
            failures.append((name, outcome, tail))
            print(f"{label}: FAILED ({outcome}) in {elapsed:.1f}s")
        else:
            print(f"{label}: ok in {elapsed:.1f}s")

    executed = len(names) - len(unrunnable)
    passed = executed - len(failures) - len(non_qualifying_fails)
    print("")
    print(
        f"suite: {len(names)} discovered, {executed} executed, {passed} passed, "
        f"{len(failures)} FAILED, {len(non_qualifying_fails)} failed-non-qualifying, "
        f"{len(unrunnable)} unrunnable, {time.time() - started:.0f}s total"
    )

    if unrunnable:
        print("")
        print(f"unrunnable here ({len(unrunnable)}) -- NOT executed, NOT counted as passing:")
        for name, requirement in unrunnable:
            print(f"  - {name} (requires {requirement})")
        print("  These are an environment gap, not evidence of correctness.")

    if non_qualifying_fails:
        print("")
        print(f"non-qualifying failures ({len(non_qualifying_fails)}):")
        for name, outcome, _tail, meta in non_qualifying_fails:
            print(
                f"  - {name} ({outcome}) owner={meta.get('owner')} "
                f"expires={meta.get('expires')} {meta.get('issue') or ''}"
            )

    if expired:
        print("")
        print("quarantine entries PAST their expiry -- now qualifying:")
        for name, meta in expired:
            print(f"  - {name} expired {meta.get('expires')} owner={meta.get('owner')}")

    if failures:
        print("")
        print("failures:")
        for name, outcome, tail in failures:
            print("")
            print(f"=== {name} ({outcome})")
            print(tail)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
