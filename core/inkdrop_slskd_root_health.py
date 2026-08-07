"""Container-local reachability checks for SLSKD staging roots."""

from __future__ import annotations

import copy
import os
import secrets
import stat
import threading
import time
from pathlib import Path


_CACHE = {}
_CACHE_LOCK = threading.Lock()
_PROBE_LOCK = threading.Lock()
_PROBE_NAME = ".inkdrop-write-probe"
_PROBE_CONTENT = b"inkdrop-root-health:v1\n"


def _owned_probe_file(path):
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            return False
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            return False
        if metadata.st_size != len(_PROBE_CONTENT):
            return False
        return path.read_bytes() == _PROBE_CONTENT
    except OSError:
        return False


def _probe_root_unlocked(key, label, configured_path):
    path_text = str(configured_path or "").strip()
    result = {
        "key": key,
        "label": label,
        "path": path_text,
        "exists": False,
        "is_directory": False,
        "readable": False,
        "writable": False,
        "checked_at": time.time(),
    }
    if not path_text:
        result.update(
            state="critical",
            status="unavailable",
            detail=f"{label} is not configured inside the InkDrop container.",
            next_action="Set the path in Settings > Paths, then verify the same mount exists in the web and worker containers.",
        )
        return result

    path = Path(path_text)
    result["exists"] = path.exists()
    if not result["exists"]:
        result.update(
            state="critical",
            status="unavailable",
            detail=f"{label} does not exist inside the InkDrop container: {path_text}",
            next_action="Create or mount this directory into both the web and worker containers, then test again.",
        )
        return result
    result["is_directory"] = path.is_dir()
    if not result["is_directory"]:
        result.update(
            state="critical",
            status="unavailable",
            detail=f"{label} is not a directory inside the InkDrop container: {path_text}",
            next_action="Point this setting at a mounted directory, then test again.",
        )
        return result

    try:
        with os.scandir(path) as entries:
            next(entries, None)
        result["readable"] = True
    except OSError as exc:
        result.update(
            state="critical",
            status="unavailable",
            detail=f"{label} cannot be read inside the InkDrop container: {path_text} ({exc})",
            next_action="Fix the container mount and directory permissions for the InkDrop web and worker user.",
        )
        return result

    # Older builds used one deterministic sentinel. Remove only that legacy
    # file; current probes use a per-process random name so the web and worker
    # containers cannot remove or collide with each other's live checks.
    legacy_probe_path = path / _PROBE_NAME
    if legacy_probe_path.exists() or legacy_probe_path.is_symlink():
        if not _owned_probe_file(legacy_probe_path):
            result.update(
                state="critical",
                status="probe collision",
                detail=f"{label} contains an unrecognized {_PROBE_NAME} file; InkDrop did not modify it: {path_text}",
                next_action=f"Inspect and remove {legacy_probe_path} if it is safe, then test again.",
                cleanup_failed=True,
            )
            return result
        try:
            legacy_probe_path.unlink()
        except FileNotFoundError:
            # Another updated InkDrop process may have removed the same
            # legacy sentinel while both containers were starting.
            pass
        except OSError as exc:
            result.update(
                state="critical",
                status="cleanup required",
                detail=f"{label} has a prior InkDrop probe file that cannot be removed: {legacy_probe_path} ({exc})",
                next_action=f"Fix permissions and remove {legacy_probe_path}, then test again.",
                cleanup_failed=True,
            )
            return result
    probe_path = path / f"{_PROBE_NAME}.{os.getpid()}.{secrets.token_hex(8)}"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(probe_path), flags, 0o600)
        try:
            written = os.write(descriptor, _PROBE_CONTENT)
            if written != len(_PROBE_CONTENT):
                raise OSError(f"short probe write ({written}/{len(_PROBE_CONTENT)} bytes)")
        finally:
            os.close(descriptor)
        try:
            probe_path.unlink()
        except OSError as exc:
            result.update(
                state="critical",
                status="cleanup required",
                detail=f"{label} accepted a write, but InkDrop could not remove its probe file: {probe_path} ({exc})",
                next_action=f"Fix permissions and remove {probe_path}, then test again.",
                cleanup_failed=True,
            )
            return result
        result["writable"] = True
    except OSError as exc:
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass
        result.update(
            state="critical",
            status="unavailable",
            detail=f"{label} cannot be written inside the InkDrop container: {path_text} ({exc})",
            next_action="Fix the container mount mode, ownership, or permissions for the InkDrop web and worker user.",
            cleanup_failed=probe_path.exists(),
        )
        return result

    result.update(
        state="healthy",
        status="reachable",
        detail=f"{label} is readable and writable inside the InkDrop container: {path_text}",
        next_action=None,
    )
    return result


def probe_root(key, label, configured_path):
    # Keep same-process probes serialized to avoid unnecessary filesystem
    # churn. Unique sentinels provide the cross-process/container guarantee.
    with _PROBE_LOCK:
        return _probe_root_unlocked(key, label, configured_path)


def clear_root_health_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


def _neutral_root(key, label, path, state, detail):
    return {
        "key": key,
        "label": label,
        "path": str(path or "").strip(),
        "exists": None,
        "is_directory": None,
        "readable": None,
        "writable": None,
        "state": state,
        "status": "not checked",
        "detail": detail,
        "next_action": None,
        "checked_at": time.time(),
    }


def slskd_root_reachability(download_root, incomplete_root, cache_seconds=15.0, *, enabled=True, configured=True):
    cache_key = (
        str(download_root or "").strip(),
        str(incomplete_root or "").strip(),
        bool(enabled),
        bool(configured),
    )
    now = time.monotonic()
    if cache_seconds > 0:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and now - cached[0] < cache_seconds:
                return copy.deepcopy(cached[1])
    labels_and_paths = [
        ("slskd_download_root", "SLSKD Download Root", download_root),
        ("slskd_incomplete_root", "SLSKD Incomplete Root", incomplete_root),
    ]
    if not enabled or not configured:
        state = "disabled" if not enabled else "unconfigured"
        detail = "SLSKD is disabled; download roots were not probed." if not enabled else "SLSKD is not configured; download roots were not probed."
        result = {
            "ok": True,
            "state": state,
            "status": "not checked",
            "label": state,
            "detail": detail,
            "problem_count": 0,
            "roots": [_neutral_root(key, label, path, state, detail) for key, label, path in labels_and_paths],
        }
        if cache_seconds > 0:
            with _CACHE_LOCK:
                _CACHE[cache_key] = (now, copy.deepcopy(result))
        return result
    roots = [
        probe_root("slskd_download_root", "SLSKD Download Root", download_root),
        probe_root("slskd_incomplete_root", "SLSKD Incomplete Root", incomplete_root),
    ]
    problems = [item for item in roots if item["state"] != "healthy"]
    if problems:
        labels = ", ".join(item["label"] for item in problems)
        result = {
            "ok": False,
            "state": "critical",
            "status": "unavailable",
            "label": "download paths unavailable",
            "detail": f"{labels} failed the container-side read/write check.",
            "problem_count": len(problems),
            "roots": roots,
        }
    else:
        result = {
            "ok": True,
            "state": "healthy",
            "status": "reachable",
            "label": "download paths reachable",
            "detail": "Both SLSKD roots are readable and writable inside the InkDrop container.",
            "problem_count": 0,
            "roots": roots,
        }
    if cache_seconds > 0:
        with _CACHE_LOCK:
            _CACHE[cache_key] = (now, copy.deepcopy(result))
    return result
