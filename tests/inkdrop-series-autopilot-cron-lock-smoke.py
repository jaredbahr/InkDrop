#!/usr/bin/env python3
"""Regression coverage for the scheduled autopilot shared-lock boundary."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import inkdrop_container_scheduler as scheduler


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts/inkdrop-series-autopilot-cron.sh"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def bash_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    converted = subprocess.run(
        ["bash", "-lc", f"wslpath -a {shlex.quote(str(path))}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return converted.stdout.strip()


def run_wrapper(*, hold_lock: bool, child_rc: int, automatic_search_enabled: bool = True, legacy_state_only: bool = False) -> dict:
    bash = shutil.which("bash")
    require(bash, "bash is required for the cron lock regression")
    harness = r'''
set -eu
wrapper="$1"
hold_lock="$2"
child_rc="$3"
automatic_search_enabled="$4"
legacy_state_only="$5"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tr -d '\r' < "$wrapper" > "$tmp/wrapper.sh"
wrapper="$tmp/wrapper.sh"
lock="$tmp/autopilot.lock"
log="$tmp/autopilot.log"
runs="$tmp/runs"
child="$tmp/child.sh"
printf '%s\n' '#!/usr/bin/env bash' 'printf "run\\n" >> "$INKDROP_TEST_RUNS"' 'exit "$INKDROP_TEST_CHILD_RC"' > "$child"
chmod +x "$child"

if [ "$hold_lock" = "1" ]; then
  exec 9>"$lock"
  /usr/bin/flock -n 9
fi

set +e
# Keep the wrapper regression independent from release-runner path variables.
# The legacy-only case must prove the legacy state root by itself, even when
# the parent process has already exported InkDrop-native directories.
unset INKDROP_STATE_DIR INKDROP_LOG_DIR INKDROP_LOCK_DIR
unset INKDROP_SERIES_AUTOPILOT_LOG INKDROP_SERIES_AUTOPILOT_LOCK
unset KAVITA_ACQUIRE_STATE_DIR
state_key=INKDROP_STATE_DIR
if [ "$legacy_state_only" = "1" ]; then
  state_key=KAVITA_ACQUIRE_STATE_DIR
  lock="$tmp/state/locks/inkdrop-series-autopilot.lock"
  log="$tmp/state/logs/series-autopilot.log"
fi
runtime_env=( \
  "$state_key=$tmp/state" \
  INKDROP_SERIES_AUTOPILOT_PYTHON=/bin/bash \
  INKDROP_SERIES_AUTOPILOT_SCRIPT="$child" \
  INKDROP_SERIES_AUTOPILOT_PROTECTED_MINUTES=off \
  INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED="$automatic_search_enabled" \
  INKDROP_TEST_RUNS="$runs" \
  INKDROP_TEST_CHILD_RC="$child_rc" \
)
if [ "$legacy_state_only" != "1" ]; then
  runtime_env+=("INKDROP_LOG_DIR=$tmp/logs" "INKDROP_SERIES_AUTOPILOT_LOG=$log" "INKDROP_SERIES_AUTOPILOT_LOCK=$lock")
fi
env "${runtime_env[@]}" /bin/bash "$wrapper"
rc=$?
set -e

if [ "$hold_lock" = "1" ]; then
  /usr/bin/flock -u 9
  exec 9>&-
fi
runs_count=0
if [ -f "$runs" ]; then
  runs_count="$(wc -l < "$runs" | tr -d '[:space:]')"
fi
printf 'RESULT_RC=%s\n' "$rc"
printf 'RESULT_RUNS=%s\n' "$runs_count"
if [ -f "$lock" ]; then printf 'RESULT_LOCK_EXISTS=1\n'; else printf 'RESULT_LOCK_EXISTS=0\n'; fi
cat "$log"
'''
    completed = subprocess.run(
        [
            bash,
            "-s",
            "--",
            bash_path(WRAPPER),
            "1" if hold_lock else "0",
            str(child_rc),
            "1" if automatic_search_enabled else "0",
            "1" if legacy_state_only else "0",
        ],
        input=harness.encode("utf-8"),
        capture_output=True,
        timeout=30,
        check=False,
    )
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    require(completed.returncode == 0, (completed.returncode, stdout, stderr))
    values = {}
    for line in stdout.splitlines():
        if line.startswith("RESULT_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = int(value)
    values["log"] = stdout
    return values


held = run_wrapper(hold_lock=True, child_rc=0)
require(held.get("RESULT_RC") == 75, held)
require(held.get("RESULT_RUNS") == 0, held)
require("lock busy" in held["log"] and "deferring pass with rc=75" in held["log"], held)

# Releasing the same shared lock permits the natural scheduler retry to run.
released = run_wrapper(hold_lock=False, child_rc=0)
require(released.get("RESULT_RC") == 0, released)
require(released.get("RESULT_RUNS") == 1, released)
require("lock busy" not in released["log"], released)

legacy_state = run_wrapper(hold_lock=False, child_rc=0, legacy_state_only=True)
require(
    legacy_state.get("RESULT_RC") == 0
    and legacy_state.get("RESULT_RUNS") == 1
    and legacy_state.get("RESULT_LOCK_EXISTS") == 1,
    legacy_state,
)
require(
    "recovery config: enabled=no-missing-recovery-enabled" in released["log"],
    "recovery must remain independently default-off when Automatic Search is enabled",
)

# The master Automatic Search switch is authoritative for scheduled work.
# Direct CLI/Run Now does not use this wrapper and remains a separate action.
disabled = run_wrapper(hold_lock=False, child_rc=0, automatic_search_enabled=False)
require(disabled.get("RESULT_RC") == 0, disabled)
require(disabled.get("RESULT_RUNS") == 0, disabled)
require(
    "disabled by INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED" in disabled["log"]
    and "missing recovery did not run" in disabled["log"],
    disabled,
)

# Child failures, including rc=1, are neither flattened to success nor
# confused with flock's rc=1 acquisition conflict.
for child_rc in (1, 42):
    failed = run_wrapper(hold_lock=False, child_rc=child_rc)
    require(failed.get("RESULT_RC") == child_rc, failed)
    require(failed.get("RESULT_RUNS") == 1, failed)
    require(f"failed with rc={child_rc}" in failed["log"] and "lock busy" not in failed["log"], failed)

job = scheduler.ScheduledJob("series-autopilot", interval_seconds=900)
old_retry = os.environ.get("INKDROP_SCHEDULER_DEFERRED_RETRY_SECONDS")
os.environ["INKDROP_SCHEDULER_DEFERRED_RETRY_SECONDS"] = "45"
try:
    deferred_failures, deferred_delay, deferred_outcome = scheduler.completion_schedule(job, 75, 0)
finally:
    if old_retry is None:
        os.environ.pop("INKDROP_SCHEDULER_DEFERRED_RETRY_SECONDS", None)
    else:
        os.environ["INKDROP_SCHEDULER_DEFERRED_RETRY_SECONDS"] = old_retry
require(deferred_failures == 0, (deferred_failures, deferred_delay, deferred_outcome))
require(deferred_delay == 45 and deferred_outcome == "deferred", (deferred_delay, deferred_outcome))
require(deferred_outcome != "success", deferred_outcome)

failure_count, failure_delay, failure_outcome = scheduler.completion_schedule(job, 42, 2)
require(failure_count == 3 and failure_outcome == "failed", (failure_count, failure_delay, failure_outcome))
require(failure_delay == scheduler.failure_backoff_seconds(job, 3), failure_delay)
require(scheduler.completion_schedule(job, 0, 3) == (0, 900, "success"), "success accounting changed")

print("inkdrop series autopilot cron lock smoke: PASS")
