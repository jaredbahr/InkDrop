#!/usr/bin/env python3
"""Opaque, file-backed secret references for InkDrop download clients.

Secret values intentionally live outside SQLite and portable settings backups.
Files are versioned and immutable: callers write a new secret first, commit the
new reference in SQLite, and only then garbage-collect the previous reference.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import time
from pathlib import Path


REFERENCE_PATTERN = re.compile(r"^dcs1_[0-9a-f]{48}$")
SECRET_SUFFIX = ".secret"
DEFAULT_ORPHAN_MIN_AGE_SECONDS = 3600


def secret_root(environ=None, root=None):
    if root is not None:
        return Path(root)
    env = os.environ if environ is None else environ
    configured = str(env.get("INKDROP_DOWNLOAD_CLIENT_SECRET_DIR") or "").strip()
    if configured:
        return Path(configured)
    config_dir = Path(str(env.get("INKDROP_CONFIG_DIR") or "/config"))
    return config_dir / "secrets" / "download-clients"


def _chmod_best_effort(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        return False
    if os.name == "nt":
        # Windows ACLs are not represented by POSIX mode bits. chmod still
        # removes the writable bit where supported, but it is not an ACL proof.
        return False
    try:
        return stat.S_IMODE(Path(path).stat().st_mode) == mode
    except OSError:
        return False


def ensure_secret_root(root=None, environ=None):
    target = secret_root(environ=environ, root=root)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    enforced = _chmod_best_effort(target, 0o700)
    return {
        "path": str(target),
        "permissions_requested": "0700",
        "permissions_enforced": enforced,
        "platform_caveat": "Windows ACLs require deployment-level verification." if os.name == "nt" else None,
    }


def validate_reference(reference):
    value = str(reference or "").strip().lower()
    if not REFERENCE_PATTERN.fullmatch(value):
        raise ValueError("invalid download-client secret reference")
    return value


def reference_path(reference, root=None, environ=None):
    value = validate_reference(reference)
    return secret_root(environ=environ, root=root) / f"{value}{SECRET_SUFFIX}"


def write_secret(value, *, root=None, environ=None):
    if isinstance(value, bytes):
        payload = value
    else:
        payload = str(value if value is not None else "").encode("utf-8")
    if not payload:
        raise ValueError("secret value cannot be blank")
    if b"\x00" in payload:
        raise ValueError("secret value cannot contain NUL bytes")
    if len(payload) > 65536:
        raise ValueError("secret value exceeds 65536 bytes")

    root_status = ensure_secret_root(root=root, environ=environ)
    target_root = Path(root_status["path"])
    reference = f"dcs1_{secrets.token_hex(24)}"
    target = reference_path(reference, root=target_root)
    temporary = target_root / f".{reference}.{secrets.token_hex(8)}.tmp"
    fd = None
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_best_effort(temporary, 0o600)
        os.replace(temporary, target)
        permissions_enforced = _chmod_best_effort(target, 0o600)
        if os.name != "nt":
            try:
                directory_fd = os.open(str(target_root), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        return {
            "reference": reference,
            "configured": True,
            "permissions_requested": "0600",
            "permissions_enforced": permissions_enforced,
            "platform_caveat": root_status.get("platform_caveat"),
        }
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_secret(reference, *, root=None, environ=None):
    target = reference_path(reference, root=root, environ=environ)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ValueError("download-client secret reference is unavailable") from exc
    if not data:
        raise ValueError("download-client secret reference is empty")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("download-client secret is not valid UTF-8") from exc


def delete_secret(reference, *, root=None, environ=None):
    target = reference_path(reference, root=root, environ=environ)
    try:
        target.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def list_references(*, root=None, environ=None):
    target_root = secret_root(environ=environ, root=root)
    if not target_root.exists():
        return []
    references = []
    for path in target_root.glob(f"dcs1_*{SECRET_SUFFIX}"):
        reference = path.name[: -len(SECRET_SUFFIX)]
        if REFERENCE_PATTERN.fullmatch(reference):
            references.append(reference)
    return sorted(references)


def cleanup_orphans(
    active_references,
    *,
    root=None,
    environ=None,
    max_delete=50,
    min_age_seconds=DEFAULT_ORPHAN_MIN_AGE_SECONDS,
    now=None,
):
    active = {validate_reference(value) for value in (active_references or []) if value}
    limit = max(0, min(int(max_delete or 0), 500))
    minimum_age = max(0.0, float(min_age_seconds or 0))
    current = time.time() if now is None else float(now)
    target_root = secret_root(environ=environ, root=root)
    deleted = []
    skipped = []
    if not target_root.exists() or not limit:
        return {"ok": True, "deleted": deleted, "skipped": skipped, "limit": limit}
    candidates = []
    for reference in list_references(root=target_root):
        path = reference_path(reference, root=target_root)
        try:
            age = max(0.0, current - path.stat().st_mtime)
        except OSError:
            continue
        candidates.append((path.stat().st_mtime, reference, age))
    for _mtime, reference, age in sorted(candidates):
        if reference in active:
            skipped.append({"reference": reference, "reason": "active"})
            continue
        if age < minimum_age:
            skipped.append({"reference": reference, "reason": "too_new"})
            continue
        if len(deleted) >= limit:
            break
        if delete_secret(reference, root=target_root):
            deleted.append(reference)
    return {
        "ok": True,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "skipped": skipped,
        "limit": limit,
        "min_age_seconds": minimum_age,
    }
