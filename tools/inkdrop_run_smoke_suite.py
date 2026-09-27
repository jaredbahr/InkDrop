#!/usr/bin/env python3
"""Run every tracked repo-root smoke test, honoring a documented skip list.

The qa workflow's path filter fires on any inkdrop*.py change, but until now
the workflow executed exactly one repo-root smoke -- a green check meant one
test passed, not that the suite did. This runner executes all of them.

Every skip below must carry a reason and a pointer. A skipped test that
passes is reported so it can be un-skipped.
"""

import atexit
import argparse
import fnmatch
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


def _git_bash_path():
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    roots = [str(Path(git).parent.parent)] if git else []
    roots.extend(
        str(Path(value) / "Git") for value in
        (os.environ.get(name) for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")) if value
    )
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(str(Path(local) / "Programs" / "Git"))
    candidates = (
        candidate for root in roots if root
        for candidate in (Path(root) / "usr" / "bin" / "bash.exe", Path(root) / "bin" / "bash.exe")
    )
    return next((str(path) for path in candidates if path.is_file()), None)


def _node_bin_dir():
    if os.name != "nt":
        return None
    roots = [os.environ.get(name) for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")]
    candidates = [Path(root) / "nodejs" for root in roots if root]
    node = shutil.which("node")
    if node:
        candidates.append(Path(node).parent)
    return next((str(path) for path in candidates if (path / "node.exe").is_file()
                 and (path / "npm.cmd").is_file()), None)


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
    if os.name == "nt":
        node_dir = _node_bin_dir()
        if node_dir:
            resolved["PATH"] = os.pathsep.join([node_dir, resolved.get("PATH", "")])
    return resolved

# INKDROP_STATE_DIR and friends default to a real, persistent path

_GIT_BASH_TESTS = frozenset({
    "inkdrop-ci-guard-trusted-pin-smoke.py",
    "inkdrop-closed-alpha-packet-exec-smoke.py",
    "inkdrop-local-qa-deploy-build-smoke.py",
})


def subprocess_env_for(name):
    """Return the child environment for *name*.

    Git Bash is scoped to the three smokes that explicitly need it. Other
    children, especially the cron-lock smoke, retain the parent's bash so the
    requirement probe and the child resolve the same shell.
    """
    resolved = subprocess_env()
    if os.name == "nt" and os.path.basename(name) in _GIT_BASH_TESTS:
        bash = _git_bash_path()
        if bash:
            resolved["PATH"] = os.pathsep.join([
                str(Path(bash).parent), resolved.get("PATH", "")
            ])
    return resolved

# (inkdrop_runtime_config.state_dir()'s own fallback) when unset. CI always
# sets these explicitly via the workflow, but a local run of this script --
# while verifying a fix, exactly the kind of run that
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


def _write_denial_effective():
    """True when a read-only mode on our own file actually stops us writing it.

    SEPARATE FROM _read_denial_effective ON PURPOSE. The two answers are not
    the same answer, and the platform where they diverge is the one this suite
    runs on most often after Linux: on Windows os.chmod moves only the
    read-only attribute, so a 0444 file there is still READABLE but genuinely
    NOT WRITABLE. Declaring a write-denial fixture against `read_denial` would
    therefore mark it unrunnable on the one platform where its fault can still
    be caused, and that is a lost test, not a saved one. As root on Linux both
    checks are bypassed and both answer False, which is why one key looked
    sufficient from a Linux host.

    Asked by staging exactly what the fixtures stage -- the mode bits, then the
    write -- rather than inferred from os.name or geteuid, for the same reason
    the read probe is.
    """
    import stat
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix="inkdrop-write-denial-") as tmp:
            probe = os.path.join(tmp, "probe")
            with open(probe, "wb") as handle:
                handle.write(b"x")
            os.chmod(probe, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            try:
                with open(probe, "ab") as handle:
                    handle.write(b"y")
                return False
            except PermissionError:
                return True
            except OSError:
                return False
            finally:
                try:
                    os.chmod(probe, stat.S_IRUSR | stat.S_IWUSR)
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
        env = subprocess_env()
        bash = shutil.which("bash", path=env.get("PATH", ""))
        if not bash:
            return False
        probe = subprocess.run(
            [bash, "-lc", "wslpath -a /"],
            capture_output=True,
            timeout=30,
            env=env,
        )
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _fcntl_available():
    """True where the fcntl module exists. core/inkdrop_slskd_staging_sweep.py
    imports it at module level for flock, so on Windows every smoke importing
    that module fails at import, before any assertion it exists for."""
    import importlib.util
    return importlib.util.find_spec("fcntl") is not None


def _proc_process_table_readable():
    """True where /proc is a readable process table, not merely a path.

    inkdrop_web.process_table_rows() enumerates the numeric entries under /proc
    and reads each one's stat and cmdline. Where that answers nothing the status
    compute falls back to forking `pgrep` per script -- which is correct, and is
    exactly what the fork-free arm of the fork-storm smoke forbids. Windows has
    no /proc, so that arm cannot hold there, and the smoke's own control says so
    rather than pass: "/proc is unreadable on this host, so the control cannot
    run and the arm above proves nothing".

    Asked by reading this process's own entry the way the scanner does, rather
    than inferred from os.name: a path check alone would answer yes on a host
    where /proc exists but carries no process entries, which is the shape that
    would make the arm pass while proving nothing.
    """
    try:
        with open(f"/proc/{os.getpid()}/stat", "rb") as handle:
            own = handle.read()
        with open(f"/proc/{os.getpid()}/cmdline", "rb"):
            pass
        return bool(own.strip()) and any(name.isdigit() for name in os.listdir("/proc"))
    except OSError:
        return False

def _frontend_deps_installed():
    """True when web/frontend's node_modules carry the bundler a test needs.

    api.ts is TypeScript and Node cannot import it before 22. The smoke that
    drives the shipped client bundles it with rolldown, which vite already
    brings in -- so the question is not "is there a Node" but "has `npm ci`
    run in web/frontend". Probed by resolving the package rather than by
    checking for node_modules, because a partial install has the directory and
    not the dependency.
    """
    path = subprocess_env().get("PATH", "")
    return bool(shutil.which("node", path=path)) and (
        ROOT / "web" / "frontend" / "node_modules" / "rolldown"
    ).is_dir()


def _git_bash_available():
    return bool(_git_bash_path())


def _react_bundle_built():
    return any(path.is_file() for path in (ROOT / "web" / "static" / "dist").glob("*"))


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
    "write_denial": (
        _write_denial_effective,
        "needs an environment where a read-only mode actually denies a write -- root on "
        "Linux bypasses the check, so the EACCES under test cannot be caused here "
        "(Windows CAN cause it: os.chmod moves the read-only attribute there)",
    ),
    "frontend_deps": (
        _frontend_deps_installed,
        "needs web/frontend's node_modules, which carry the bundler that turns the shipped "
        "TypeScript client into something Node can import -- run `npm ci` in web/frontend",
    ),
    "proc_table": (
        _proc_process_table_readable,
        "needs /proc to be a readable process table -- without one the status compute "
        "correctly falls back to forking pgrep, so the fork-free arm cannot hold here",
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
    "git_bash": (
        _git_bash_available,
        "needs a native POSIX shell; Windows requires Git Bash rather than the WSL launcher",
    ),
    "react_bundle": (
        _react_bundle_built,
        "needs the generated web/static/dist React bundle; run the frontend build first",
    ),
}

# requirement key -> who to ask when it stops holding. Printed on every run
# beside the requirement's state, and again on each test it declines.
#
# WHY THIS SITS ON THE REQUIREMENT AND NOT ON EACH REQUIRES ENTRY. The question
# that prompted it was whether a REQUIRES entry should carry an owner the way a
# NON_QUALIFYING entry does. It should not, for two reasons.
#
# First, the two tables have different powers. A NON_QUALIFYING entry HIDES A
# RED: the test runs, fails, and is kept out of the qualifying count, so it
# needs a name and a deadline and the runner un-suppresses it at the deadline
# automatically. A REQUIRES entry hides nothing -- it declines to produce a
# result and prints that decision loudly, twice. So it needs no expiry either:
# "root bypasses chmod" and "Windows has no fcntl" do not expire, and a date on
# them would only manufacture churn.
#
# Second, an owner per entry would be the same fact written forty-odd times.
# Playwright going missing is ONE event, not thirty; the thing that can stop
# being true is the REQUIREMENT. One owner per requirement is the whole of it.
#
# Kept as a separate table rather than a third tuple slot because REQUIREMENTS
# values are unpacked as (predicate, explanation) by this runner, by
# conftest.py, and by the guard smokes that plant synthetic tables -- widening
# the tuple would invalidate every one of them for no gain here.
REQUIREMENT_OWNERS = {
    "origin_qa": "release",
    "playwright": "web",
    "read_denial": "harness",
    "write_denial": "harness",
    "change_time": "harness",
    "symlinks": "harness",
    "wslpath": "harness",
    "fcntl": "acquisition",
    "proc_table": "web",
    "frontend_deps": "web",
    "git_bash": "harness",
    "react_bundle": "web",
}


def unowned_requirements(requirements, owners):
    """(requirements with no owner, owners naming no requirement), each sorted.

    Not a refusal. An undeclared requirement KEY is refused before discovery
    because it silently turns a test into a permanent non-result; a missing
    owner costs nothing at run time and taking 939 tests off the board over a
    missing string would be the larger harm. The banner says UNOWNED on every
    run instead, and the guard smoke for this table is what keeps it at zero.

    Both tables are arguments for the same reason validate_requirement_keys'
    are: the guard that covers this swaps the module attributes, and a default
    would bind the tables the caller replaced.
    """
    missing = sorted(key for key in requirements if not str(owners.get(key) or "").strip())
    orphaned = sorted(key for key in owners if key not in requirements)
    return missing, orphaned


def requirement_owner(key, owners=None):
    """The owner of `key`, or UNOWNED. Never raises: this is banner text."""
    table = REQUIREMENT_OWNERS if owners is None else owners
    return str(table.get(key) or "").strip() or "UNOWNED"


# basename -> requirement key. A test listed here is never executed unless its
# requirement holds; when it does hold the test runs for real and any failure
# is a failure like any other. This is not a suppression list -- membership
# here cannot hide a result, it can only decline to produce one.
REQUIRES = {
    "inkdrop-archive-validation-cache-smoke.py": "change_time",
    "inkdrop-status-compute-without-process-forks-smoke.py": "proc_table",
    "inkdrop-archive-verdict-honesty-smoke.py": "symlinks",
    "inkdrop-series-autopilot-cron-lock-smoke.py": "wslpath",
    "inkdrop-slskd-staging-sweep-missing-stage-priority-smoke.py": "fcntl",
    "inkdrop-slskd-staging-sweep-raw-page-folder-smoke.py": "fcntl",
    "inkdrop-slskd-sweep-no-scan-wait-smoke.py": "fcntl",
    "inkdrop-slskd-sweep-trusted-issue-unit-smoke.py": "fcntl",
    "inkdrop-a-late-answer-is-not-the-current-one-smoke.py": "playwright",
    "inkdrop-a-malformed-success-is-not-success-smoke.py": "frontend_deps",
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
    # Stages a 0444 archive and asserts the repair does not claim it wrote one
    # it could not. Root bypasses that mode, so the archive IS writable, the
    # repair IS honest, and the test reported FAILED for an environment it
    # cannot construct the condition in. Gated on write_denial rather than
    # read_denial so it keeps running on Windows, where the write denial is
    # real and the read denial is not.
    "inkdrop-metadata-guard-repair-truth-smoke.py": "write_denial",
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
    "inkdrop-ci-guard-trusted-pin-smoke.py": "git_bash",
    "inkdrop-closed-alpha-packet-exec-smoke.py": "git_bash",
    "inkdrop-local-qa-deploy-build-smoke.py": "git_bash",
    "inkdrop-api-client-parity-smoke.py": "frontend_deps",
    "inkdrop-section-error-boundary-smoke.py": "frontend_deps",
    "inkdrop-selection-indeterminate-smoke.py": "frontend_deps",
    "inkdrop-react-bundle-contract-smoke.py": "react_bundle",
}


def validate_requirement_keys(requires, requirements):
    """The REQUIRES entries whose requirement key is not defined, sorted.

    Nothing else binds the two tables together, and an undefined key is the
    worst outcome available: the resolution below treats "no such requirement"
    as "the precondition is missing here", so a typo, a rename, or a
    requirement deleted without its entries marks the test unrunnable on every
    platform. It is then never executed, never counted, and never red, and no
    requirement banner line is printed for it either -- the one table that
    would show the mistake is built from `REQUIREMENTS`.

    Both tables are arguments rather than defaults on purpose: defaults bind at
    def time, and the guard that covers this swaps the module attributes, so a
    default would keep validating the tables the caller replaced.
    """
    return sorted(
        (basename, key)
        for basename, key in requires.items()
        if key not in requirements
    )


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
    # stale extractions and timeouts last.
    "inkdrop-activity-backend-contract-browser-smoke.py": {
        "reason": (
            "ReferenceError for maybeHydrateSeriesDetailEditionIndifferentAction: the test extracts a function out of core/inkdrop_web.py and evaluates it in a page, and that function now calls a helper the extraction does not carry. STALE TEST following code that moved. UN-SUPPRESSES WHEN: the extraction carries the helpers its subject calls"
        ),
        "owner": "web",
        "expires": "2026-09-25",
        "issue": "web extraction drift",
    },
    "inkdrop-sampled-history-facet-browser-smoke.py": {
        "reason": (
            "ReferenceError for appendSectionRowCountChip, same extraction drift as the activity backend contract test. STALE TEST. UN-SUPPRESSES WHEN: the extraction carries the helpers its subject calls"
        ),
        "owner": "web",
        "expires": "2026-09-25",
        "issue": "web extraction drift",
    },
    "inkdrop-series-poster-title-overflow-smoke.py": {
        "reason": (
            "page.waitForFunction exceeded 30s. TIMEOUT, cause unestablished; lowest triage priority because slow is better understood than unknown. UN-SUPPRESSES WHEN: the awaited condition is reached, or the wait is re-pointed at what the page actually renders"
        ),
        "owner": "web",
        "expires": "2026-09-25",
        "issue": "browser timeout under investigation",
    },
    "inkdrop-settings-backup-browser-smoke.py": {
        "reason": (
            "page.waitForEvent for a download exceeded 30s. TIMEOUT; note the viewer sandbox never fires a download for a page-initiated save, so the expectation itself may be wrong. UN-SUPPRESSES WHEN: the download fires, or the test asserts the payload without waiting on a browser download event"
        ),
        "owner": "web",
        "expires": "2026-09-25",
        "issue": "browser download expectation under investigation",
    },

    # inkdrop-slskd-failover-smoke.py was quarantined here for the 420s CI
    # timeout and is deliberately absent now: its cause was found and removed.
    # It was never the flock/SQLite contention section the quarantine named --
    # that section runs in 0.26s. AUTO_GRAB_STATE_LOCK resolves from
    # INKDROP_LOCK_DIR at import, the suite points that at one directory for the
    # whole run, and thirteen of the smoke's fourteen auto-grab call sites took
    # that shared file; a sibling holding it made this test wait 60s at a time.
    # The smoke now redirects every INKDROP_* path to a root of its own before
    # its core imports, and this runner's own rule applies: a quarantined test
    # that passes is removed from this table rather than left in it.
    # tests/inkdrop-slskd-failover-does-not-wait-on-an-ambient-lock-smoke.py
    # holds the fix in place.
    # Wiring these three up is what proved they had been dead for weeks. Each
    # needs a judgement this wiring pass deliberately did not make, so each runs
    # and prints its red rather than being hidden. Owner and expiry assigned by
    # the wiring pass, not by an external record -- NON_QUALIFYING requires both.
    "inkdrop-activity-queue-blocklist-contract-smoke.py": {
        "reason": (
            "measured 2026-09-08 against qa 2ad80c96 with every assert recorded: 9 of 18 "
            "fail, not the one the old note named. The five-column Blocklist literal "
            "(five columns confirmed intended), the Allow & Retry call form (a ternary "
            "endpoint and a body carrying revision since PR #349), the selection-count "
            "guard (now manual_review only), and six Queue grid contracts the current CSS "
            "contradicts (a source column; a 24px selection track the smoke forbids). "
            "UN-SUPPRESSES WHEN: web re-pins all nine against the shipped tree, or rules "
            "the changed layout contracts intended and re-pins the rest; the measured "
            "list remains available in the measured failure evidence"
        ),
        "owner": "web",
        "expires": "2026-10-06",
        "issue": "activity and blocklist contract drift",
    },
    "inkdrop-closed-alpha-user-journey-contract-smoke.py": {
        "reason": (
            "measured 2026-09-08 against qa 2ad80c96 with every assert recorded: 5 of 60 "
            "fail. The add-series readiness attribute is set through dataset now, the "
            "Series automation heading moved, 'Monitor future releases' and the "
            "'Administration' nav-group label no longer exist anywhere in the tree, and no "
            "Settings entry opens the setup area with the pinned call. UN-SUPPRESSES "
            "WHEN: web re-pins all 60 against current shipped copy without deleting any; "
            "the measured list remains available in the failure evidence"
        ),
        "owner": "web",
        "expires": "2026-10-06",
        "issue": "closed-alpha journey contract drift",
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


def _smoke_like_name(name):
    """The discovery pathspecs' own shape: inkdrop-*smoke*.py / inkdrop_*smoke*.py.

    Spelled without `re` because this module does not import it, and a new
    module-level import for one predicate is more surface than the predicate.
    """
    low = name.lower()
    return (low.startswith(("inkdrop-", "inkdrop_"))
            and "smoke" in low and low.endswith(".py"))
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", "dist", "build"}


def undiscovered_smokes(root=None):
    """Smoke-shaped files present on disk that git does not track.

    THE GUARD THAT LOOKS LIKE IT COVERS THIS IS BLIND, NOT LENIENT.
    `MIN_SMOKE_DISCOVERY_RATIO` compares tracked_smokes() against
    smoke_like_files(), and BOTH run `git ls-files`. An untracked file is
    missing from the numerator AND the denominator, so the ratio stays
    perfect however many there are. Measured on qa 3c933295: 864 tracked
    smokes, an 866-file denominator, threshold 692 -- and adding one untracked
    smoke to the tree moved none of the three.

    So this reads the FILESYSTEM and subtracts what git tracks. It looks in the
    two places the discovery pathspecs cover, `tests/` and the repository root,
    which is where a new test is actually written.

    Returns repository-relative POSIX paths, sorted. Never raises: a tree that
    is not a git repository yields nothing to subtract, and the caller's job is
    to report, not to fail.
    """
    root = Path(root) if root else ROOT
    on_disk = set()
    for base in (root / "tests", root):
        if not base.is_dir():
            continue
        walk = base.rglob("*") if base != root else base.glob("*")
        for path in walk:
            if not path.is_file() or not _smoke_like_name(path.name):
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            on_disk.add(path.relative_to(root).as_posix())
    probe = subprocess.run(
        ["git", "ls-files", "--cached", "--", "*smoke*.py"],
        capture_output=True, text=True, cwd=str(root),
    )
    if probe.returncode != 0:
        return sorted(on_disk)
    tracked = {line.strip() for line in probe.stdout.splitlines() if line.strip()}
    return sorted(on_disk - tracked)


def tracked_smokes(ref=None, repo_root=None):
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
    patterns = (
        "tests/inkdrop*smoke*.py",
        "inkdrop*smoke*.py",
        "tools/inkdrop*_smoke.py",
    )
    command = ["git", "ls-files", *patterns]
    if ref is not None:
        command = ["git", "ls-tree", "-r", "--name-only", ref]
    out = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    names = [name for name in out.stdout.splitlines() if name.strip()]
    if ref is not None:
        names = [
            name for name in names
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
        ]
    return sorted(names)


def main():
    ensure_isolated_state_env()

    # Before discovery, so this refusal can never be mistaken for a test
    # result. Passing both tables explicitly is what makes the check see the
    # tables this call is actually using.
    undeclared = validate_requirement_keys(REQUIRES, REQUIREMENTS)
    if undeclared:
        for basename, key in undeclared:
            print(
                f"REQUIRES[{basename}] names requirement {key!r}, which is not in "
                "REQUIREMENTS -- that test would be marked unrunnable on every "
                "platform, never executed and never counted as passing or failing"
            )
        print(
            f"refusing to run: {len(undeclared)} precondition entries name a "
            "requirement that does not exist. Fix the spelling in REQUIRES, or "
            "delete the entry."
        )
        return 1

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
    # A TEST THE SUITE COULD NOT SEE IS NAMED BEFORE ANYTHING RUNS.
    # This fires exactly when someone writes a NEW test, which is when the
    # suite's answer matters most, and the old behaviour failed toward "clean".
    # It is deliberately NOT a failure: an uncommitted test is the normal state
    # of a workstation mid-edit, and reddening the suite there would train
    # people to ignore it, which is the failure this exists to prevent. The
    # commit hook refuses the STATE; this stops a run REPORTING CLEAN while it
    # holds.
    undiscovered = undiscovered_smokes()
    if undiscovered:
        print("")
        print(f"present but NOT DISCOVERED ({len(undiscovered)}) -- untracked, so "
              f"`git ls-files` never offered them and they did not run:")
        for name in undiscovered:
            print(f"  - {name}")
        print("  `git add` them before treating this run as covering them.")
        print("")

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
        print("requirement " + key + ": " + state
              + " (owner=" + requirement_owner(key, REQUIREMENT_OWNERS) + ")" + suffix)

    # Reported, never refused -- see unowned_requirements' docstring. The
    # per-requirement line above already says UNOWNED for a requirement with no
    # owner, but it can only speak about keys that still exist: an owner naming
    # a requirement that has since been deleted appears on no line at all. That
    # orphan is the shape a precondition leaves behind when it stops being true,
    # so it is the one this banner has to say out loud.
    missing_owner, orphaned_owner = unowned_requirements(REQUIREMENTS, REQUIREMENT_OWNERS)
    for key in missing_owner:
        print(f"requirement {key}: UNOWNED -- no entry in REQUIREMENT_OWNERS")
    for key in orphaned_owner:
        print(f"requirement owner {key}: ORPHANED -- names no requirement that exists")

    today = time.strftime("%Y-%m-%d", time.gmtime())

    for index, name in enumerate(names, start=1):
        label = f"[{index}/{len(names)}] {name}"
        base = os.path.basename(name)

        # Preconditions first: a test that cannot run here is NOT executed, and
        # says so. It never touches the pass count and never touches red.
        # Indexed, not `.get(..., False)`: the gate at the top of main() has
        # already refused every undeclared key, so a missing one here is a bug
        # in this file and must say so rather than read as "unrunnable".
        requirement = REQUIRES.get(base)
        if requirement is not None and not satisfied[requirement]:
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
                env=subprocess_env_for(name),
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
        f"{len(unrunnable)} unrunnable, "
        + (f"{len(undiscovered)} undiscovered, " if undiscovered else "")
        + f"{time.time() - started:.0f}s total"
    )

    if unrunnable:
        print("")
        print(f"unrunnable here ({len(unrunnable)}) -- NOT executed, NOT counted as passing:")
        for name, requirement in unrunnable:
            # `requirement` is a REQUIREMENTS key for a precondition and a
            # free-text reason for an export decline; only the first has an
            # owner, and inventing one for the second would be a lie.
            owner = (f" owner={requirement_owner(requirement, REQUIREMENT_OWNERS)}"
                     if requirement in REQUIREMENTS else "")
            print(f"  - {name} (requires {requirement}){owner}")
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

def parse_args(argv=None):
    """Answer the command line before doing 38 minutes of work on it.

    This script had no argv handling at all, so every argument was ignored and
    the full sweep ran regardless. `--help` therefore RAN THE SUITE: measured
    2026-08-25, a session invoked it that way at 14:41:56 and was still executing
    real tests twelve minutes later.

    The second cost is the one that bites. The process list lied about what was
    happening -- two sessions read `--help` in a process tree as a harmless no-op
    while a real suite belonging to another session was in flight. On a host where
    several sessions contend for one runner, and where a concurrent run is a known
    source of false REDs, a command line that misrepresents the work is a
    coordination hazard.

    Unrecognised arguments are refused rather than ignored: silently discarding
    an argument the caller believed in is the same failure shape either way.
    argparse exits 2 and names the argument, which is what a caller needs.
    """
    parser = argparse.ArgumentParser(
        prog="inkdrop_run_smoke_suite.py",
        description=(
            "Run the InkDrop smoke suite: every tracked test the discovery "
            "pathspec finds, sequentially, in one process each. Takes roughly "
            "38 minutes and mutates shared temp state, so do not start one "
            "beside another -- check first."
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    parse_args()
    sys.exit(main())
