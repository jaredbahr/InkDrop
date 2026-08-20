#!/usr/bin/env python3
"""Bounded child-process tracking and reaping for long-lived InkDrop services."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path


_LOCK = threading.RLock()
_CHILDREN: dict[int, subprocess.Popen] = {}
_DETACHED: set[int] = set()
_REAPER_THREAD = None
_REAPER_WAKE = threading.Event()
_REAPER_INTERVAL_SECONDS = 0.1
# How long a killed child gets to die politely before the tree is SIGKILLed,
# and how long we then wait to collect its output. Both are bounded on purpose:
# an unbounded drain here is what turns a timed-out child into a stuck caller
# (see _drain_after_kill).
_KILL_GRACE_SECONDS = 5.0
_KILL_DRAIN_SECONDS = 10.0


# SIGKILL does not exist on Windows, and this module is otherwise careful to be
# portable -- every os.killpg/os.getpgid use is hasattr-guarded. The constant was
# the gap: `signal.SIGKILL` is evaluated as a call ARGUMENT, so it raised
# AttributeError before any of that guarding could run, and the whole
# cross-platform contract failed on the first hard kill.
#
# SIGTERM is the honest fallback rather than a compromise. On Windows there is
# no signal distinction to make: os.kill() and Popen.kill() both end at
# TerminateProcess, which is already unconditional, so the "polite then forceful"
# escalation this module implements collapses to one behaviour the platform
# provides. On POSIX the escalation is unchanged.
_HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _running_as_pid_one() -> bool:
    return os.getpid() == 1


def _isolates_process_group(kwargs) -> bool:
    """Whether we may give this child its own process group.

    POSIX only, and never over the top of a caller that already made its own
    arrangements -- inkdrop_web.run_command, for instance, sets preexec_fn=setsid
    itself and manages the group by hand.
    """
    if os.name != "posix" or not hasattr(os, "killpg") or not hasattr(os, "getpgid"):
        return False
    if kwargs.get("preexec_fn") is not None:
        return False
    if "start_new_session" in kwargs or kwargs.get("process_group") is not None:
        return False
    return str(os.environ.get("INKDROP_PROCESS_GROUP_ISOLATION", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _proc_stat(pid):
    """(ppid, session) for a live pid, or None.

    comm sits in parentheses and may itself contain spaces and parentheses, so
    the numeric fields are read from after the LAST ')'.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except (OSError, ValueError):
        return None
    fields = raw.rpartition(")")[2].split()
    if len(fields) < 4:
        return None
    try:
        return int(fields[1]), int(fields[3])
    except ValueError:
        return None


def _descendant_pids(root_pid: int) -> set[int]:
    """Every live process under root_pid, by session AND by parent chain.

    Two lookups because either one alone leaks a real case:

      * Session: because a descendant may move itself into a NEW PROCESS GROUP
        and so escape killpg. GNU timeout does exactly this, and
        manual-source-autoresolve is launched as flock -> timeout -> python.
      * Parent chain: because a descendant may call setsid() and so leave the
        session, and because a process re-parented to PID 1 keeps neither.

    Returns an empty set where /proc is unavailable; callers fall back to the
    process-group kill.
    """
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return set()
    parents: dict[int, int] = {}
    sessions: dict[int, int] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return set()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat = _proc_stat(pid)
        if stat is None:
            continue
        parents[pid], sessions[pid] = stat
    found = {pid for pid, sid in sessions.items() if sid == root_pid}
    pending = [root_pid, *found]
    seen = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for pid, ppid in parents.items():
            if ppid == current and pid not in found:
                found.add(pid)
                pending.append(pid)
    found.add(root_pid)
    # Never signal this process, its ancestors, or init.
    return {pid for pid in found if pid > 1 and pid != os.getpid()}


def _signal_descendants(proc, sig) -> None:
    for pid in _descendant_pids(proc.pid):
        try:
            os.kill(pid, sig)
        except OSError:
            continue


def _signal_process_tree(proc, sig) -> bool:
    """Signal the child's whole process group; report whether the group got it.

    Only ever signals a group this child actually LEADS. If process-group
    isolation was suppressed the child shares our own group, and killpg would
    take this process down with it.
    """
    if not hasattr(os, "killpg") or not hasattr(os, "getpgid"):
        return False
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return False
    if pgid != proc.pid:
        return False
    try:
        os.killpg(pgid, sig)
    except OSError:
        return False
    return True


def _kill_process_tree(proc) -> None:
    """Kill the child AND every descendant it spawned.

    A bare proc.kill() reaches only the direct child. Six scheduler jobs wrap
    their real work in flock/bash, so killing the wrapper left the grandchild
    running and still holding the lock fd it inherited. Every later pass then
    found that lock busy and exited 75, which the scheduler records as
    "deferred" -- an outcome that resets consecutive_failures to zero, so a
    permanently hung job kept all three worker healthcheck gates green
    indefinitely.
    """
    grouped = _signal_process_tree(proc, signal.SIGTERM)
    _signal_descendants(proc, signal.SIGTERM)
    if not grouped:
        # No group to lean on -- isolation suppressed, or a platform without it.
        # The direct child still has to die the way it always did, and the
        # descendant sweep is the only other reach we have into the tree.
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if grouped:
        _signal_process_tree(proc, _HARD_KILL_SIGNAL)
    # Second sweep, not a repeat of the first: a SIGTERM handler can spawn or
    # re-parent work, and anything that ignored the TERM is still here.
    _signal_descendants(proc, _HARD_KILL_SIGNAL)


def _close_streams(proc) -> None:
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _drain_after_kill(proc):
    """Collect what the killed child left, without blocking forever.

    communicate() waits for EOF on the pipes, and any descendant that outlived
    the kill still holds its inherited copies of them -- so an unbounded drain
    here hands the caller a permanent hang instead of a timeout. Bounded, and
    the streams are closed if it expires.
    """
    try:
        return communicate_tracked(proc, timeout=_KILL_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        _close_streams(proc)
        return None, None


def _forget_reaped(proc: subprocess.Popen) -> None:
    if proc.returncode is None:
        return
    with _LOCK:
        _CHILDREN.pop(proc.pid, None)
        _DETACHED.discard(proc.pid)


def popen_tracked(args, **kwargs) -> subprocess.Popen:
    """Spawn and register a child before any concurrent orphan sweep can reap it."""
    # Its own session, so a timeout can kill the whole tree rather than just the
    # wrapper we happen to have a handle on (_kill_process_tree).
    if _isolates_process_group(kwargs):
        kwargs["start_new_session"] = True
    with _LOCK:
        proc = subprocess.Popen(args, **kwargs)
        _CHILDREN[proc.pid] = proc
    # Both long-lived services are PID 1 in their containers. Start the orphan
    # sweep for foreground-only workloads too; do this after registration and
    # outside the lock so the new thread cannot race or deadlock this spawn.
    if _running_as_pid_one():
        _ensure_reaper_thread()
        _REAPER_WAKE.set()
    return proc


def communicate_tracked(proc: subprocess.Popen, input=None, timeout=None):
    try:
        return proc.communicate(input=input, timeout=timeout)
    finally:
        _forget_reaped(proc)


def wait_tracked(proc: subprocess.Popen, timeout=None):
    try:
        return proc.wait(timeout=timeout)
    finally:
        _forget_reaped(proc)


def run_tracked(args, *, timeout=None, check=False, **kwargs) -> subprocess.CompletedProcess:
    """Equivalent to subprocess.run while keeping the child visible to the reaper."""
    input_value = kwargs.pop("input", None)
    capture_output = kwargs.pop("capture_output", False)
    if input_value is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    proc = popen_tracked(args, **kwargs)
    try:
        try:
            stdout, stderr = communicate_tracked(proc, input=input_value, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_tree(proc)
            stdout, stderr = _drain_after_kill(proc)
            exc.stdout = stdout
            exc.stderr = stderr
            raise
        except BaseException:
            _kill_process_tree(proc)
            _drain_after_kill(proc)
            raise
    finally:
        _forget_reaped(proc)
    completed = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    if check:
        completed.check_returncode()
    return completed


def launch_detached(args, **kwargs) -> subprocess.Popen:
    """Launch background work and arrange non-blocking eventual wait/reaping."""
    proc = popen_tracked(args, **kwargs)
    with _LOCK:
        _DETACHED.add(proc.pid)
    _ensure_reaper_thread()
    _REAPER_WAKE.set()
    return proc


def _child_pids_from_proc() -> set[int]:
    task_root = Path(f"/proc/{os.getpid()}/task")
    if os.name != "posix" or not task_root.is_dir():
        return set()
    child_pids = set()
    try:
        task_dirs = list(task_root.iterdir())
    except OSError:
        return set()
    for task_dir in task_dirs:
        try:
            values = (task_dir / "children").read_text(encoding="ascii").split()
        except OSError:
            continue
        for value in values:
            try:
                child_pids.add(int(value))
            except ValueError:
                continue
    return child_pids


def reap_untracked_children(*, force=False) -> int:
    """Reap completed adopted children, only when this service is PID 1."""
    if not hasattr(os, "waitpid") or not hasattr(os, "WNOHANG"):
        return 0
    if not force and not _running_as_pid_one():
        return 0
    reaped = 0
    with _LOCK:
        candidates = _child_pids_from_proc() - set(_CHILDREN)
        for pid in candidates:
            try:
                waited_pid, _status = os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                continue
            if waited_pid:
                reaped += 1
    return reaped


def reap_detached_children() -> int:
    reaped = 0
    with _LOCK:
        processes = [_CHILDREN[pid] for pid in tuple(_DETACHED) if pid in _CHILDREN]
    for proc in processes:
        if proc.poll() is not None:
            _forget_reaped(proc)
            reaped += 1
    return reaped


def _reaper_loop() -> None:
    while True:
        reap_detached_children()
        reap_untracked_children()
        _REAPER_WAKE.wait(_REAPER_INTERVAL_SECONDS)
        _REAPER_WAKE.clear()


def _ensure_reaper_thread() -> None:
    global _REAPER_THREAD
    with _LOCK:
        if _REAPER_THREAD is not None and _REAPER_THREAD.is_alive():
            return
        _REAPER_THREAD = threading.Thread(
            target=_reaper_loop,
            name="inkdrop-child-reaper",
            daemon=True,
        )
        _REAPER_THREAD.start()


def process_registry_status() -> dict:
    with _LOCK:
        return {
            "tracked": len(_CHILDREN),
            "detached": len(_DETACHED),
            "tracked_pids": sorted(_CHILDREN),
            "detached_pids": sorted(_DETACHED),
        }


def drain_detached_children(timeout=5.0) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout or 0))
    while True:
        reap_detached_children()
        if process_registry_status()["detached"] == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(_REAPER_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
