#!/usr/bin/env python3
"""Bounded rotation and retention for append-only InkDrop runtime logs."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path

import inkdrop_runtime_config


DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_RETENTION_FILES = 5
DEFAULT_RETENTION_DAYS = 14
DEFAULT_WRITER_GRACE_SECONDS = 2 * 60 * 60
DEFAULT_QUIET_SECONDS = 60
DEFAULT_SAMPLE_MILLISECONDS = 100
LEGACY_ROTATED_NAME = re.compile(r"^(?P<base>.+\.(?:log|jsonl))\.(?P<index>[1-9][0-9]*)$")
IMMUTABLE_ROTATED_NAME = re.compile(
    r"^(?P<base>.+\.(?:log|jsonl))\.r-(?P<created_ns>[0-9]+)-(?P<token>[0-9a-f]{32})$"
)
MAINTENANCE_LOCK_NAME = "inkdrop-log-retention.lock"
OPEN_INODES_UNSET = object()


@dataclasses.dataclass(frozen=True)
class LogRetentionPolicy:
    max_bytes: int = DEFAULT_MAX_BYTES
    retention_files: int = DEFAULT_RETENTION_FILES
    retention_days: int = DEFAULT_RETENTION_DAYS
    writer_grace_seconds: int = DEFAULT_WRITER_GRACE_SECONDS
    quiet_seconds: int = DEFAULT_QUIET_SECONDS
    sample_milliseconds: int = DEFAULT_SAMPLE_MILLISECONDS


def _bounded_env(env, name, default, minimum, maximum):
    try:
        value = int(str(env.get(name) or default).strip())
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def acquire_maintenance_lock(path=None):
    lock_path = Path(path or inkdrop_runtime_config.lock_path(MAINTENANCE_LOCK_NAME))
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError as exc:
        return None, f"lock_open_failed:{type(exc).__name__}"
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        handle.close()
        return None, "maintenance_lock_busy"
    except OSError as exc:
        handle.close()
        if getattr(exc, "winerror", None) in {33, 36}:
            return None, "maintenance_lock_busy"
        return None, f"lock_failed:{type(exc).__name__}"
    return handle, ""


def release_maintenance_lock(handle):
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def policy_from_env(environ=None):
    env = os.environ if environ is None else environ
    return LogRetentionPolicy(
        max_bytes=_bounded_env(env, "INKDROP_LOG_ROTATION_MAX_BYTES", DEFAULT_MAX_BYTES, 1024 * 1024, 1024 * 1024 * 1024),
        retention_files=_bounded_env(env, "INKDROP_LOG_RETENTION_FILES", DEFAULT_RETENTION_FILES, 1, 50),
        retention_days=_bounded_env(env, "INKDROP_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, 1, 3650),
        writer_grace_seconds=_bounded_env(
            env,
            "INKDROP_LOG_RETENTION_WRITER_GRACE_SECONDS",
            DEFAULT_WRITER_GRACE_SECONDS,
            3601,
            604800,
        ),
        quiet_seconds=_bounded_env(env, "INKDROP_LOG_ROTATION_QUIET_SECONDS", DEFAULT_QUIET_SECONDS, 0, 86400),
        sample_milliseconds=_bounded_env(env, "INKDROP_LOG_ROTATION_SAMPLE_MILLISECONDS", DEFAULT_SAMPLE_MILLISECONDS, 1, 5000),
    )


def inode_key(path):
    try:
        row = path.stat()
    except OSError:
        return None
    return int(row.st_dev), int(row.st_ino)


def open_inode_snapshot(proc_root=Path("/proc")):
    if not proc_root.is_dir():
        return None
    opened = set()
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return None
    for process in processes:
        if not process.name.isdigit():
            continue
        fd_dir = process / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                row = descriptor.stat()
            except OSError:
                continue
            opened.add((int(row.st_dev), int(row.st_ino)))
    return opened


def _regular_file(path):
    try:
        row = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(row.st_mode) and not path.is_symlink()


def _active_log(path):
    return path.name.endswith(".log") or path.name.endswith(".jsonl")


def _snapshot(path):
    try:
        row = path.stat()
    except OSError:
        return None
    return int(row.st_dev), int(row.st_ino), int(row.st_size), int(row.st_mtime_ns)


def _is_open(path, opened):
    key = inode_key(path)
    return key is not None and opened is not None and key in opened


def _snapshot_is_open(snapshot, opened):
    return snapshot is not None and opened is not None and (snapshot[0], snapshot[1]) in opened


def immutable_generation_path(path, *, now=None, token=None):
    created_ns = time.time_ns() if now is None else int(float(now) * 1_000_000_000)
    suffix = str(token or secrets.token_hex(16)).lower()
    return path.with_name(f"{path.name}.r-{created_ns}-{suffix}")


def rotate_log(path, policy, *, opened=OPEN_INODES_UNSET, open_inode_provider=None, sleep=time.sleep, now=None):
    now = time.time() if now is None else float(now)
    before = _snapshot(path)
    if before is None or before[2] <= int(policy.max_bytes):
        return "below_threshold"
    if opened is OPEN_INODES_UNSET:
        opened = open_inode_provider() if open_inode_provider is not None else None
    posix_open_writer = False
    if os.name == "posix":
        # POSIX rename preserves every existing descriptor on the old inode.
        # The unique destination means no container-local /proc view is needed
        # to protect a generation owned by a writer in another PID namespace.
        before_open = _snapshot_is_open(before, opened)
        posix_open_writer = before_open
    else:
        before_open = _snapshot_is_open(before, opened)
        if before_open:
            return "open_inode"
        if now - (before[3] / 1_000_000_000) < int(policy.quiet_seconds):
            return "recent_write"
        sample_seconds = max(0, int(policy.sample_milliseconds)) / 1000.0
        if sample_seconds:
            sleep(sample_seconds)
        after = _snapshot(path)
        if before != after:
            return "changed_during_sample"
        if open_inode_provider is not None:
            refreshed = open_inode_provider()
            if refreshed is not None:
                opened = refreshed
        if _snapshot_is_open(after, opened):
            return "open_inode"
    generation = immutable_generation_path(path, now=now)
    while generation.exists():
        generation = immutable_generation_path(path, now=now)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        os.replace(path, generation)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            os.close(descriptor)
    except OSError:
        if not path.exists() and generation.exists():
            try:
                os.replace(generation, path)
            except OSError:
                pass
        return "rotation_error"
    return "rotated_open_writer" if posix_open_writer else "rotated"


def _rotated_log(path):
    match = IMMUTABLE_ROTATED_NAME.match(path.name)
    if match:
        return {
            "base": match.group("base"),
            "created_ns": int(match.group("created_ns")),
            "legacy_index": None,
        }
    match = LEGACY_ROTATED_NAME.match(path.name)
    if match:
        return {
            "base": match.group("base"),
            "created_ns": None,
            "legacy_index": int(match.group("index")),
        }
    return None


def scheduled_generation_bound(policy, interval_seconds, scheduler_count=1):
    interval = max(1, int(interval_seconds))
    grace_rotations = (int(policy.writer_grace_seconds) + interval - 1) // interval
    return grace_rotations * max(1, int(scheduler_count)) + int(policy.retention_files)


def _generation_reference_seconds(path, parsed):
    if parsed["created_ns"] is not None:
        return parsed["created_ns"] / 1_000_000_000
    try:
        row = path.stat()
    except OSError:
        return None
    return max(float(row.st_mtime), float(row.st_ctime))


def maintain_logs(
    log_dir=None,
    policy=None,
    *,
    lock_path=None,
    open_inode_provider=open_inode_snapshot,
    sleep=time.sleep,
    now=None,
):
    root = Path(log_dir or inkdrop_runtime_config.log_dir())
    selected = policy or policy_from_env()
    now = time.time() if now is None else float(now)
    summary = {
        "ok": True,
        "log_dir": str(root),
        "deferred": False,
        "scanned": 0,
        "rotated": 0,
        "rotated_open_writer": 0,
        "deleted": 0,
        "protected_by_grace": 0,
        "retained_generations": 0,
        "legacy_generations": 0,
        "skipped_open": 0,
        "skipped_active": 0,
        "below_threshold": 0,
        "errors": [],
    }
    lock_handle, lock_error = acquire_maintenance_lock(lock_path)
    if lock_handle is None:
        if lock_error == "maintenance_lock_busy":
            summary["deferred"] = True
            summary["reason"] = lock_error
        else:
            summary["errors"].append({"path": str(lock_path or inkdrop_runtime_config.lock_path(MAINTENANCE_LOCK_NAME)), "error": lock_error})
            summary["ok"] = False
        return summary
    try:
        root.mkdir(parents=True, exist_ok=True)
        active_logs = sorted(path for path in root.rglob("*") if _active_log(path) and _regular_file(path))
        active_opened = open_inode_provider()
        for path in active_logs:
            summary["scanned"] += 1
            outcome = rotate_log(
                path,
                selected,
                opened=active_opened,
                open_inode_provider=open_inode_provider,
                sleep=sleep,
                now=now,
            )
            if outcome in {"rotated", "rotated_open_writer"}:
                summary["rotated"] += 1
                if outcome == "rotated_open_writer":
                    summary["rotated_open_writer"] += 1
            elif outcome == "below_threshold":
                summary["below_threshold"] += 1
            elif outcome in {"open_inode", "open_rotation"}:
                summary["skipped_open"] += 1
            elif outcome in {"recent_write", "changed_during_sample", "open_snapshot_unavailable"}:
                summary["skipped_active"] += 1
            else:
                summary["errors"].append({"path": str(path), "error": outcome})

        expiry = now - int(selected.retention_days) * 86400
        grouped = {}
        for path in root.rglob("*"):
            parsed = _rotated_log(path)
            if parsed and _regular_file(path):
                reference = _generation_reference_seconds(path, parsed)
                if reference is None:
                    continue
                grouped.setdefault((str(path.parent), parsed["base"]), []).append(
                    {"path": path, "parsed": parsed, "reference": reference}
                )
                if parsed["legacy_index"] is not None:
                    summary["legacy_generations"] += 1

        deletion_candidates = []
        grace_seconds = int(selected.writer_grace_seconds)
        for rows in grouped.values():
            immutable = sorted(
                (row for row in rows if row["parsed"]["legacy_index"] is None),
                key=lambda row: (row["reference"], row["path"].name),
                reverse=True,
            )
            eligible = []
            for row in immutable:
                if now - row["reference"] < grace_seconds:
                    summary["protected_by_grace"] += 1
                else:
                    eligible.append(row)
            for position, row in enumerate(eligible, start=1):
                if position <= int(selected.retention_files) and row["reference"] >= expiry:
                    summary["retained_generations"] += 1
                else:
                    deletion_candidates.append(row["path"])

            for row in (row for row in rows if row["parsed"]["legacy_index"] is not None):
                if now - row["reference"] < grace_seconds:
                    summary["protected_by_grace"] += 1
                    continue
                index = int(row["parsed"]["legacy_index"])
                if index <= int(selected.retention_files) and row["reference"] >= expiry:
                    summary["retained_generations"] += 1
                else:
                    deletion_candidates.append(row["path"])

        before_delete = {path: _snapshot(path) for path in deletion_candidates}
        # Grace establishes that all bounded InkDrop writers have exited; this
        # sample then rejects a late or out-of-contract writer without relying
        # on visibility into another container's descriptor table.
        if deletion_candidates and int(selected.sample_milliseconds) > 0:
            sleep(int(selected.sample_milliseconds) / 1000.0)
        for path in deletion_candidates:
            before = before_delete.get(path)
            if before is None:
                continue
            if _snapshot(path) != before:
                summary["skipped_active"] += 1
                continue
            try:
                path.unlink()
                summary["deleted"] += 1
            except OSError as exc:
                summary["errors"].append({"path": str(path), "error": type(exc).__name__})
    finally:
        release_maintenance_lock(lock_handle)
    summary["ok"] = not summary["errors"]
    return summary


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    payload = maintain_logs(log_dir=args.log_dir)
    print(json.dumps(payload, indent=2 if args.json else None, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
