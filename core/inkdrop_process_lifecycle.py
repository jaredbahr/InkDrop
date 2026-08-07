#!/usr/bin/env python3
"""Bounded child-process tracking and reaping for long-lived InkDrop services."""

from __future__ import annotations

import os
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


def _running_as_pid_one() -> bool:
    return os.getpid() == 1


def _forget_reaped(proc: subprocess.Popen) -> None:
    if proc.returncode is None:
        return
    with _LOCK:
        _CHILDREN.pop(proc.pid, None)
        _DETACHED.discard(proc.pid)


def popen_tracked(args, **kwargs) -> subprocess.Popen:
    """Spawn and register a child before any concurrent orphan sweep can reap it."""
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
            proc.kill()
            stdout, stderr = communicate_tracked(proc)
            exc.stdout = stdout
            exc.stderr = stderr
            raise
        except BaseException:
            proc.kill()
            communicate_tracked(proc)
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
