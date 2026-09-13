#!/usr/bin/env python3
"""Whether it is safe to swap the state database out from under this install.

WHY THIS EXISTS
    `/api/inkdrop-settings/backup/archives/restore/apply` returned 503
    unconditionally (`SIXH-20260812-RESTORE-P0-01`) because applying a restore
    replaces the live state database with no coordination: a worker mid-cycle
    against the same file, and no rollback if a later step in the swap fails
    after an earlier one succeeded.

    That concern is real and is NOT removed here. What was wrong was the SHAPE
    of the answer. A permanent 503 does not make a restore safe -- it makes it
    unreachable, and a capability that is proven to work but cannot be invoked
    has a recovery time of "never" for anyone who only has the UI. A restore of
    the real 13.55 GB archive was measured at 515 s on 2026-08-28; the 503 was
    the difference between that number and infinity.

    So the concern becomes a PRECONDITION THAT CAN BE MET rather than a door
    nailed shut: a restore refuses while anything else is writing, and proceeds
    once the worker is actually stopped. The operator's move is
    `docker stop inkdrop-worker` -- a step they can take, unlike "wait for a
    maintenance-lease primitive to be built".

    Note the CLI path -- the one that actually worked, and the one a disaster
    recovery would have used -- was never gated AT ALL. It carries exactly the
    same hazard the 503 describes. The guard was on the door nobody could open.
    Both routes take this gate now.

WHAT THIS DOES NOT CLAIM
    This does not stop writers; nothing in this codebase can. There is no
    runtime pause lever in the container scheduler: jobs are built once at
    start, enablement is read from the environment at process start, and the
    only stop is SIGTERM. So this DETECTS a quiescent install and refuses
    otherwise -- it does not create quiescence.

    Residual, stated rather than hidden: the web process itself serves other
    requests, and a concurrent request that writes during the swap lands in the
    replaced inode and is lost. `os.replace` is atomic, so that is lost writes
    in a seconds-wide window, not a corrupt database, and `pre_restore_snapshots`
    still captures what was there. An operator restoring a backup is not
    simultaneously using the app; that is a real reduction, not a proof.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import json
import os
import time
from pathlib import Path

from core import inkdrop_runtime_config


RESTORE_LOCK_NAME = "inkdrop-restore.lock"
WORKER_STATUS_FILE_NAME = "worker-scheduler-status.json"
# The scheduler heartbeats every INKDROP_SCHEDULER_HEARTBEAT_SECONDS (default
# 10, bounded 2..60). Treat a heartbeat as live well past the slowest possible
# beat: a stale-looking heartbeat from a merely slow host must never read as
# "the worker is stopped", because that is the direction that loses data.
DEFAULT_HEARTBEAT_LIVE_SECONDS = 180


def _bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return int(default)
    return max(int(minimum), min(int(maximum), parsed))


def heartbeat_live_seconds(environ=None):
    env = environ if environ is not None else os.environ
    return _bounded_int(
        env.get("INKDROP_RESTORE_HEARTBEAT_LIVE_SECONDS"),
        DEFAULT_HEARTBEAT_LIVE_SECONDS,
        30,
        3600,
    )


def worker_status_path(environ=None):
    env = environ if environ is not None else os.environ
    explicit = env.get("INKDROP_WORKER_STATUS_FILE")
    if explicit:
        return Path(explicit)
    return Path(inkdrop_runtime_config.state_dir(env)) / WORKER_STATUS_FILE_NAME


def _probe_one_lock(lock_path):
    """True if some other open file description holds this lock.

    Takes the lock and immediately drops it. A file that cannot be opened at
    all is reported as held: refusing on an unreadable lock is the safe
    direction, because the alternative is proceeding while blind.
    """
    # Open READ-ONLY on POSIX. `flock()` does not require a writable
    # descriptor, and requiring one here would be an over-refusal bug waiting
    # to happen: a lock directory the web process cannot write to would make
    # every lock report unreadable, and unreadable is treated as held, so every
    # restore would be refused forever while the gate looked perfectly healthy.
    # Found by running this probe against production with the lock directory
    # mounted read-only -- all 15 locks came back "lock_unreadable" when
    # exactly one was actually held.
    #
    # A read-only open also means the probe cannot create a lock file it was
    # only meant to look at, or touch one it did not create.
    #
    # Windows keeps the read-write open: msvcrt.locking() genuinely does need
    # write access, and the deploy target is Linux.
    try:
        handle = open(lock_path, "a+b" if os.name == "nt" else "rb")
    except OSError:
        return True, "lock_unreadable"
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False, ""
    except (BlockingIOError, PermissionError):
        return True, "held"
    except OSError as exc:
        if getattr(exc, "winerror", None) in {33, 36}:
            return True, "held"
        return True, "probe_failed:" + type(exc).__name__
    finally:
        try:
            handle.close()
        except OSError:
            pass


def probe_restore_quiescence(*, lock_dir=None, status_path=None, now=None, environ=None):
    """Report every reason a restore must not run right now.

    The lock directory is SCANNED rather than compared against a list of known
    job locks. A hardcoded list goes stale silently the first time a job is
    added: the new job's lock would simply not be probed, and the gate would
    pass while that job wrote. Scanning cannot develop that hole.
    """
    env = environ if environ is not None else os.environ
    moment = float(now if now is not None else time.time())
    directory = Path(lock_dir or inkdrop_runtime_config.lock_dir(env))
    status_file = Path(status_path or worker_status_path(env))

    blockers = []
    checked = {"lock_dir": str(directory), "worker_status_file": str(status_file)}

    # 1. Is the worker scheduler alive?
    live_window = heartbeat_live_seconds(env)
    checked["heartbeat_live_seconds"] = live_window
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = None
    checked["worker_heartbeat_age_seconds"] = None
    if isinstance(payload, dict):
        try:
            heartbeat_at = float(payload.get("heartbeat_at") or 0.0)
        except (TypeError, ValueError):
            heartbeat_at = 0.0
        # A status file with no readable heartbeat cannot be dated at all, so
        # nothing in it may be treated as stale: an undatable file is the
        # blind case, and blind fails closed. The producer always writes
        # heartbeat_at and active_jobs in one payload, so this is a corrupt or
        # foreign file, not a worker state.
        status_file_is_current = True
        if heartbeat_at:
            age = moment - heartbeat_at
            checked["worker_heartbeat_age_seconds"] = round(age, 1)
            status_file_is_current = age <= live_window
            if status_file_is_current:
                blockers.append(
                    {
                        "kind": "worker_scheduler_live",
                        "detail": (
                            "the worker scheduler heartbeated "
                            + format(age, ".0f")
                            + "s ago (counts as live within "
                            + str(live_window)
                            + "s)"
                        ),
                        "next_action": "Stop the worker container, then retry: docker stop inkdrop-worker",
                    }
                )
        checked["worker_status_file_is_current"] = status_file_is_current
        active = payload.get("active_jobs")
        if isinstance(active, list) and active:
            names = sorted(str(row.get("name") or "?") for row in active if isinstance(row, dict))
            checked["worker_active_jobs"] = names
            # `active_jobs` is only as fresh as the heartbeat that shipped with
            # it. The list is not a live reading -- it is a line in a file the
            # worker leaves behind, and a worker SIGKILLed mid-job leaves it
            # populated forever. Believing it past the heartbeat window made
            # the operator move this gate exists to permit ("docker stop
            # inkdrop-worker, then restore") refuse indefinitely, with no way
            # out through the UI at all: the availability failure in row #974.
            #
            # The heartbeat only decides whether the FILE still describes
            # anything. What is actually running is decided below by probing
            # the job locks, which are evidence from live processes rather
            # than a record of them, and which are untouched by this.
            if status_file_is_current:
                blockers.append(
                    {
                        "kind": "worker_job_active",
                        "detail": "the worker is running: " + ", ".join(names),
                        "next_action": "Wait for these jobs to finish, or stop the worker container.",
                    }
                )
            else:
                # Say that the list was seen and set aside. An ignored reading
                # and a reading never taken look identical in an incident.
                checked["worker_active_jobs_disregarded"] = (
                    "the heartbeat that vouched for these jobs is older than "
                    + str(live_window)
                    + "s, so the status file no longer describes a running worker; "
                    "the job locks decide"
                )

    # 2. Is any job lock held, by anything?
    #
    # An ABSENT lock directory is not suspicious -- an install where no job has
    # ever taken a lock has none, and refusing there would block a restore on a
    # fresh machine forever. A lock directory that exists but cannot be LISTED
    # is different: that is the probe going blind, and a blind probe reporting
    # "nothing is running" is the failure this gate exists to prevent. CI found
    # this by pointing INKDROP_LOCK_DIR somewhere the test was not writing --
    # the probe reported quiescent having examined nothing at all, which is
    # indistinguishable from a genuine pass unless it says what it looked at.
    held = []
    lock_dir_exists = directory.is_dir()
    checked["lock_dir_exists"] = lock_dir_exists
    lock_files = []
    if lock_dir_exists:
        try:
            lock_files = sorted(p for p in directory.glob("*.lock") if p.is_file())
        except OSError as exc:
            blockers.append(
                {
                    "kind": "lock_dir_unreadable",
                    "detail": (
                        "the lock directory " + str(directory) + " exists but could not be read ("
                        + type(exc).__name__ + "), so whether a job is running is unknown"
                    ),
                    "next_action": "Fix permissions on the lock directory, or stop the worker container and retry.",
                }
            )
    checked["lock_files_probed"] = [p.name for p in lock_files]
    for lock_file in lock_files:
        if lock_file.name == RESTORE_LOCK_NAME:
            continue
        is_held, reason = _probe_one_lock(lock_file)
        if is_held:
            held.append(lock_file.name + " (" + reason + ")")
    if held:
        blockers.append(
            {
                "kind": "job_lock_held",
                "detail": "another process holds: " + ", ".join(sorted(held)),
                "next_action": "Wait for that job to finish, or stop the worker container.",
            }
        )
    checked["locks_held"] = sorted(held)

    return {
        "quiescent": not blockers,
        "blockers": blockers,
        "checked": checked,
        "probed_at": moment,
    }


def blocker_summary(probe):
    """One operator-facing sentence naming what is in the way."""
    blockers = (probe or {}).get("blockers") or []
    if not blockers:
        return "Nothing else is writing to the database."
    reasons = "; ".join(str(b.get("detail") or b.get("kind")) for b in blockers)
    return "Restore is blocked because " + reasons + "."


class RestoreNotQuiescent(RuntimeError):
    """Raised when a restore is attempted while something else is writing."""

    def __init__(self, probe):
        self.probe = probe
        super().__init__(blocker_summary(probe))


def require_restore_quiescence(**kwargs):
    """Probe, and refuse loudly if anything else is writing.

    Returns the probe so a caller can record what it checked -- a gate that
    passes should still be able to say WHAT it looked at, otherwise a passing
    gate and a gate that examined nothing are indistinguishable.
    """
    probe = probe_restore_quiescence(**kwargs)
    if not probe["quiescent"]:
        raise RestoreNotQuiescent(probe)
    return probe
