#!/usr/bin/env python3
"""InkDrop config/state backup and restore helpers.

The public install path needs a small, boring way to move InkDrop state between
config roots without copying secrets into support exports. This module creates a
zip archive containing:

- a SQLite state backup,
- a redacted config/settings export,
- a secret-reference manifest without secret values,
- a restore manifest with schema and path-remap warnings.

It does not copy user media, staging downloads, reader databases, or live
external-service state.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import math
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

import inkdrop_runtime_config
import inkdrop_auth
import inkdrop_settings_registry
import inkdrop_state
import inkdrop_version


BACKUP_RESTORE_SCHEMA_VERSION = 1
PORTABLE_SETTINGS_SCHEMA = "inkdrop.portable_settings.v1"
PORTABLE_SETTINGS_PRODUCT = "InkDrop"
PORTABLE_SETTINGS_MAX_BYTES = 1024 * 1024
PORTABLE_SETTINGS_MAX_COUNT = 1000
PROTOTYPE_KEYS = {"__proto__", "prototype", "constructor"}
INVALID_STORED_VALUE = object()
STATE_DB_ARCHIVE_NAME = "state/inkdrop-state.sqlite3"
AUTH_DB_ARCHIVE_NAME = "state/inkdrop-auth.sqlite3"
CONFIG_EXPORT_ARCHIVE_NAME = "config/inkdrop-config-export.json"
SECRET_REFS_ARCHIVE_NAME = "config/inkdrop-secret-refs.json"
MANIFEST_ARCHIVE_NAME = "manifest.json"
SECRET_KEY_MARKERS = ("API_KEY", "PASSWORD", "TOKEN", "SECRET", "USERNAME")
PATH_ENV_KEYS = (
    "INKDROP_CONFIG_DIR",
    "INKDROP_STATE_DIR",
    "INKDROP_LOG_DIR",
    "INKDROP_CACHE_DIR",
    "INKDROP_BACKUP_DIR",
    "INKDROP_STAGING_DIR",
    "INKDROP_MANUAL_INBOX_DIR",
    "INKDROP_QUARANTINE_DIR",
    "INKDROP_COMIC_ROOT",
    "INKDROP_MANGA_ROOT",
)


def _secure_backup_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path, 0o700)
        if path.stat().st_mode & 0o077:
            raise PermissionError(f"backup directory permissions are not private: {path}")


def _verify_sensitive_archive_mode(path):
    if os.name == "posix":
        os.chmod(path, 0o600)
        if path.stat().st_mode & 0o077:
            raise PermissionError(f"backup archive permissions are not private: {path}")


def utc_stamp(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts if ts is not None else time.time())))


def compact_stamp(ts=None):
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(float(ts if ts is not None else time.time())))


def path_text(path):
    return str(Path(path)).replace("\\", "/")


def is_secret_key(key):
    normalized = str(key or "").upper()
    return any(marker in normalized for marker in SECRET_KEY_MARKERS)


def redacted_config_export(environ=None):
    env = environ if environ is not None else os.environ
    values = {}
    secret_refs = {}
    for key in sorted(key for key in env if key.startswith("INKDROP_")):
        raw = str(env.get(key) or "")
        if is_secret_key(key):
            values[key] = "<set>" if raw else "<unset>"
            if raw:
                secret_refs[key] = {"configured": True, "value": "<redacted>"}
        else:
            values[key] = raw
    return {
        "schema_version": BACKUP_RESTORE_SCHEMA_VERSION,
        "exported_at": utc_stamp(),
        "values": values,
        "secret_refs": secret_refs,
    }


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _settings_checksum(document):
    payload = {key: value for key, value in dict(document or {}).items() if key != "checksum"}
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _reject_duplicate_object(pairs):
    out = {}
    for key, value in pairs:
        key = str(key)
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        if key.lower() in PROTOTYPE_KEYS:
            raise ValueError(f"reserved JSON key: {key}")
        out[key] = value
    return out


def _reject_nonfinite_constant(value):
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _require_finite_json(value, *, label="value"):
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"{label} contains a non-finite number")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_finite_json(item, label=f"{label}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _require_finite_json(item, label=f"{label}.{key}")
    return value


def _load_stored_json(raw):
    value = json.loads(raw or "null", parse_constant=_reject_nonfinite_constant)
    return _require_finite_json(value, label="stored setting")


def parse_strict_json_object(raw, *, max_bytes, label="JSON document"):
    if isinstance(raw, bytes):
        data = raw
    else:
        data = str(raw or "").encode("utf-8")
    if not data or len(data) > int(max_bytes):
        raise ValueError(f"{label} has an invalid size")
    try:
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=_reject_nonfinite_constant,
        )
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} root must be an object")
    return _require_finite_json(document, label=label)


def parse_portable_settings_document(raw):
    return parse_strict_json_object(raw, max_bytes=PORTABLE_SETTINGS_MAX_BYTES, label="settings backup")


def _portable_exclusion_reason(key, value):
    upper = str(key or "").upper()
    if any(marker in upper for marker in SECRET_KEY_MARKERS):
        return "secret_or_credential"
    if any(marker in upper for marker in ("PATH", "ROOT", "DIRECTORY", "FOLDER", "URL", "HOST", "ORIGIN", "PROXY")):
        return "private_or_host_specific"
    if str(key or "").lower().startswith("auth."):
        return "security_or_identity"
    return ""


def _portable_rows(con):
    return con.execute("select key,value_json from app_settings order by key").fetchall()


def _portable_document_from_rows(rows, *, now=None, version=None):
    now = time.time() if now is None else float(now)
    included = {}
    excluded = []
    for row in rows[:PORTABLE_SETTINGS_MAX_COUNT + 1]:
        key = str(row["key"] or "").strip()
        if not key or len(key) > 160 or key.lower() in PROTOTYPE_KEYS:
            excluded.append({"key": key[:160], "reason": "invalid_or_reserved_key"})
            continue
        try:
            value = _load_stored_json(row["value_json"])
        except (TypeError, ValueError):
            excluded.append({"key": key, "reason": "malformed_or_non_finite_stored_value"})
            continue
        if not inkdrop_settings_registry.is_defined_public_setting(key):
            excluded.append({"key": key, "reason": "unknown_or_deprecated"})
            continue
        reason = _portable_exclusion_reason(key, value)
        if reason:
            excluded.append({"key": key, "reason": reason, "value": "<not-exported>"})
            continue
        try:
            included[key] = inkdrop_auth.validate_setting_value(key, inkdrop_settings_registry.validate_value(key, value))
        except ValueError:
            excluded.append({"key": key, "reason": "invalid_stored_value"})
    if len(rows) > PORTABLE_SETTINGS_MAX_COUNT:
        raise ValueError("too many settings to export")
    metadata = inkdrop_version.build_metadata() if version is None else {"version": str(version)}
    document = {
        "schema": PORTABLE_SETTINGS_SCHEMA,
        "schema_version": 1,
        "product": PORTABLE_SETTINGS_PRODUCT,
        "product_version": str(metadata.get("version") or "dev")[:64],
        "exported_at": utc_stamp(now),
        "mode": "merge",
        "settings": included,
        "excluded": excluded,
        "contains": {
            "app_settings": True,
            "provider_credentials": False,
            "private_paths": False,
            "media": False,
            "tasks_history_users_sessions": False,
        },
    }
    document["checksum"] = _settings_checksum(document)
    return document


def export_portable_settings(db_path, *, now=None, version=None):
    with inkdrop_state.connect_read(db_path) as con:
        rows = _portable_rows(con)
    return _portable_document_from_rows(rows, now=now, version=version)


def portable_settings_filename(document):
    stamp = "".join(character for character in str((document or {}).get("exported_at") or "") if character.isdigit())[:14]
    return f"inkdrop-settings-{stamp or 'backup'}.json"


def _strict_portable_value(key, value):
    _require_finite_json(value, label=key)
    schema = inkdrop_settings_registry.field_schema(key)
    kind = schema.get("kind")
    if kind == "boolean" and type(value) is not bool:
        raise ValueError(f"{key} must be a JSON boolean")
    if kind == "array":
        if type(value) is not list or any(type(item) is not str for item in value):
            raise ValueError(f"{key} must be a JSON list of strings")
    elif kind == "number":
        if schema.get("integer"):
            if type(value) is not int:
                raise ValueError(f"{key} must be a JSON integer")
        elif type(value) not in {int, float}:
            raise ValueError(f"{key} must be a JSON number")
    elif kind in {"choice", "string"} and type(value) is not str:
        raise ValueError(f"{key} must be a JSON string")
    return inkdrop_auth.validate_setting_value(key, inkdrop_settings_registry.validate_value(key, value))


def _portable_settings_plan(document, rows):
    schema_version = document.get("schema_version")
    if document.get("schema") != PORTABLE_SETTINGS_SCHEMA or type(schema_version) is not int or schema_version != 1:
        raise ValueError("unsupported settings backup schema")
    if document.get("product") != PORTABLE_SETTINGS_PRODUCT:
        raise ValueError("settings backup belongs to another product")
    if not str(document.get("product_version") or "").strip() or len(str(document.get("product_version"))) > 64:
        raise ValueError("settings backup product version is invalid")
    supplied_checksum = str(document.get("checksum") or "")
    if not supplied_checksum or not hmac.compare_digest(supplied_checksum, _settings_checksum(document)):
        raise ValueError("settings backup checksum mismatch")
    settings = document.get("settings")
    if not isinstance(settings, dict) or len(settings) > PORTABLE_SETTINGS_MAX_COUNT:
        raise ValueError("settings backup has an invalid settings collection")
    current = {}
    for row in rows:
        key = str(row["key"])
        try:
            current[key] = _load_stored_json(row["value_json"])
        except (TypeError, ValueError):
            current[key] = INVALID_STORED_VALUE
    changes, unchanged, unknown, invalid = [], [], [], []
    validated = {}
    restart_required = []
    for key, value in sorted(settings.items()):
        key = str(key or "").strip()
        if not key or len(key) > 160 or key.lower() in PROTOTYPE_KEYS:
            invalid.append({"key": key[:160], "reason": "invalid_or_reserved_key"})
            continue
        if key not in current or not inkdrop_settings_registry.is_defined_public_setting(key):
            unknown.append({"key": key, "reason": "unknown_or_deprecated"})
            continue
        if _portable_exclusion_reason(key, value):
            invalid.append({"key": key, "reason": "non_portable_setting"})
            continue
        if current[key] is INVALID_STORED_VALUE:
            invalid.append({"key": key, "reason": "current_stored_value_is_malformed_or_non_finite"})
            continue
        try:
            parsed = _strict_portable_value(key, value)
        except ValueError as exc:
            invalid.append({"key": key, "reason": str(exc)[:240]})
            continue
        validated[key] = parsed
        if current[key] == parsed:
            unchanged.append(key)
        else:
            changes.append({"key": key, "from": current[key], "to": parsed})
        if inkdrop_settings_registry.augment_setting({"key": key}).get("restart_required"):
            restart_required.append(key)
    return {
        "ok": not invalid,
        "merge_only": True,
        "changes": changes,
        "unchanged": unchanged,
        "unknown": unknown,
        "invalid": invalid,
        "validated": validated,
        "restart_required": restart_required,
        "restart_guidance": (
            "Restart InkDrop after apply for: " + ", ".join(restart_required)
            if restart_required else "No restart is required for the portable settings in this backup."
        ),
    }


def _snapshot_existing_file(source, backup_dir, *, now=None):
    """Copy `source` aside into `backup_dir` before it gets overwritten, using
    the same atomic tempfile-then-replace pattern as _write_settings_snapshot.
    Returns the snapshot path, or None if there was nothing to snapshot yet
    (e.g. a first-ever restore onto an empty state dir)."""
    source = Path(source)
    if not source.exists():
        return None
    backup_dir = Path(backup_dir)
    _secure_backup_directory(backup_dir)
    target = backup_dir / f"{source.stem}-before-restore-{compact_stamp(now)}{source.suffix}"
    fd, temporary = tempfile.mkstemp(prefix=f".{source.stem}-", suffix=".tmp", dir=backup_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            with source.open("rb") as src:
                shutil.copyfileobj(src, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _verify_sensitive_archive_mode(target)
        if os.name != "nt":
            directory_fd = os.open(str(backup_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def _write_settings_snapshot(rows, backup_dir, *, now=None):
    backup_dir = Path(backup_dir)
    _secure_backup_directory(backup_dir)
    document = _portable_document_from_rows(rows, now=now)
    target = backup_dir / f"inkdrop-settings-before-restore-{compact_stamp(now)}.json"
    fd, temporary = tempfile.mkstemp(prefix=".inkdrop-settings-", suffix=".tmp", dir=backup_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _verify_sensitive_archive_mode(target)
        if os.name != "nt":
            directory_fd = os.open(str(backup_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target


def _apply_portable_setting(con, key, value, now):
    con.execute(
        "update app_settings set value_json=?,source='user',updated_at=? where key=?",
        (_canonical_json(value), now, key),
    )


def restore_portable_settings(db_path, raw_document, *, apply=False, backup_dir=None, now=None):
    document = parse_portable_settings_document(raw_document)
    if not apply:
        with inkdrop_state.connect_read(db_path) as con:
            rows = _portable_rows(con)
        plan = _portable_settings_plan(document, rows)
        public_plan = {key: value for key, value in plan.items() if key != "validated"}
        return {
            "ok": bool(plan["ok"]),
            "applied": False,
            "dry_run": True,
            "source": {"product_version": document.get("product_version"), "exported_at": document.get("exported_at"), "checksum": document.get("checksum")},
            "plan": public_plan,
        }
    timestamp = time.time() if now is None else float(now)
    with inkdrop_state.connect(db_path) as con:
        con.execute("begin immediate")
        rows = _portable_rows(con)
        plan = _portable_settings_plan(document, rows)
        public_plan = {key: value for key, value in plan.items() if key != "validated"}
        result = {
            "ok": bool(plan["ok"]),
            "applied": False,
            "dry_run": False,
            "source": {"product_version": document.get("product_version"), "exported_at": document.get("exported_at"), "checksum": document.get("checksum")},
            "plan": public_plan,
        }
        if not plan["ok"]:
            con.rollback()
            return result
        snapshot = _write_settings_snapshot(
            rows,
            backup_dir or inkdrop_runtime_config.backup_dir(),
            now=now,
        )
        changed_keys = [row["key"] for row in plan["changes"]]
        for key in changed_keys:
            _apply_portable_setting(con, key, plan["validated"][key], timestamp)
        inkdrop_state.record_settings_history(
            con,
            "settings_restore",
            "settings_backup",
            str(document.get("checksum") or "")[-16:],
            "settings",
            f"Restored {len(changed_keys)} portable setting(s)",
            {"changed_keys": changed_keys, "changed_count": len(changed_keys), "unknown_count": len(plan["unknown"]), "source_schema": document.get("schema")},
            timestamp,
        )
        inkdrop_state.update_sync_meta(con, timestamp, "settings_restore")
        con.commit()
    inkdrop_state.clear_settings_caches()
    result.update({"ok": True, "applied": True, "snapshot": path_text(snapshot), "changed_count": len(changed_keys)})
    return result


def backup_sqlite_db(source_db: Path, target_db: Path):
    target_db.parent.mkdir(parents=True, exist_ok=True)
    if not source_db.exists():
        return {"ok": False, "reason": "state_db_missing", "source": path_text(source_db)}
    try:
        with contextlib.closing(sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)) as src, contextlib.closing(sqlite3.connect(target_db)) as dst:
            src.backup(dst)
            dst.commit()
        method = "sqlite_backup"
    except sqlite3.Error:
        shutil.copy2(source_db, target_db)
        method = "file_copy_fallback"
    auth_safety = inkdrop_auth.sanitize_auth_database_copy(target_db)
    if not auth_safety.get("ok"):
        target_db.unlink(missing_ok=True)
        raise ValueError("state backup credential-safety validation failed")
    return {
        "ok": True,
        "method": method,
        "source": path_text(source_db),
        "bytes": target_db.stat().st_size,
        "auth_credential_safety": auth_safety,
    }


def create_backup_archive(
    *,
    config_dir=None,
    state_db_path=None,
    backup_dir=None,
    environ=None,
    label="manual",
):
    env = environ if environ is not None else os.environ
    config_dir = Path(config_dir or inkdrop_runtime_config.config_dir(env))
    state_db_path = Path(state_db_path or inkdrop_runtime_config.state_db_path(env))
    backup_dir = Path(backup_dir or inkdrop_runtime_config.backup_dir(env))
    _secure_backup_directory(backup_dir)
    archive_path = backup_dir / f"inkdrop-backup-{compact_stamp()}-{label}.zip"
    fd, temp_archive_name = tempfile.mkstemp(prefix=".inkdrop-backup-", suffix=".tmp", dir=backup_dir)
    temp_archive = Path(temp_archive_name)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        os.close(fd)
        fd = -1
        with tempfile.TemporaryDirectory(prefix="inkdrop-backup-build-") as tmp:
            tmp_root = Path(tmp)
            temp_db = tmp_root / STATE_DB_ARCHIVE_NAME
            db_backup = backup_sqlite_db(state_db_path, temp_db)
            if not db_backup.get("ok"):
                raise ValueError(
                    f"state database backup failed: {db_backup.get('reason') or 'unknown_error'}"
                )
            # Logins, sessions, and API keys live in their own database next
            # to the state file; a backup that skipped it would restore a
            # library with last year's credentials.
            auth_db_path = inkdrop_auth.auth_store_path(state_db_path)
            temp_auth_db = tmp_root / AUTH_DB_ARCHIVE_NAME
            auth_db_backup = backup_sqlite_db(auth_db_path, temp_auth_db)
            config_export = redacted_config_export(env)
            secret_refs = {
            "schema_version": BACKUP_RESTORE_SCHEMA_VERSION,
            "exported_at": utc_stamp(),
            "secrets": config_export.get("secret_refs") or {},
            "note": "Secret values are not included. Recreate these values in the target environment.",
        }
            manifest = {
            "schema_version": BACKUP_RESTORE_SCHEMA_VERSION,
            "created_at": utc_stamp(),
            "label": str(label or "manual"),
            "contains": {
                "state_db": bool(db_backup.get("ok")),
                "auth_db": bool(auth_db_backup.get("ok")),
                "redacted_config_export": True,
                "secret_reference_manifest": True,
                "media_files": False,
                "reader_databases": False,
                "staging_downloads": False,
                "reusable_credentials": False,
            },
            "source": {
                "config_dir": path_text(config_dir),
                "state_db_path": path_text(state_db_path),
                "auth_db_path": path_text(auth_db_path),
                "backup_dir": path_text(backup_dir),
            },
            "state_db_backup": db_backup,
            "auth_db_backup": auth_db_backup,
            "credential_policy": "Authentication material in the state backup is cryptographically hashed; plaintext passwords, sessions, recovery tokens, and API keys are never exported.",
        }
            with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(MANIFEST_ARCHIVE_NAME, json.dumps(manifest, indent=2, sort_keys=True))
                zf.writestr(CONFIG_EXPORT_ARCHIVE_NAME, json.dumps(config_export, indent=2, sort_keys=True))
                zf.writestr(SECRET_REFS_ARCHIVE_NAME, json.dumps(secret_refs, indent=2, sort_keys=True))
                if db_backup.get("ok"):
                    zf.write(temp_db, STATE_DB_ARCHIVE_NAME)
                if auth_db_backup.get("ok"):
                    zf.write(temp_auth_db, AUTH_DB_ARCHIVE_NAME)
        _verify_sensitive_archive_mode(temp_archive)
        with temp_archive.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_archive, archive_path)
        _verify_sensitive_archive_mode(archive_path)
        if os.name == "posix":
            directory_fd = os.open(backup_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        temp_archive.unlink(missing_ok=True)
        raise
    finally:
        if fd >= 0:
            os.close(fd)
    return {
        "ok": True,
        "archive_path": path_text(archive_path),
        "manifest": manifest,
        "config_export": {
            "schema_version": config_export["schema_version"],
            "value_count": len(config_export.get("values") or {}),
            "secret_ref_count": len(config_export.get("secret_refs") or {}),
        },
    }


def _safe_zip_members(zf):
    members = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"unsafe archive member path: {info.filename}")
        members.append(info)
    return members


def _validate_sqlite_database(database_path, *, label):
    """Reject non-databases and internally inconsistent SQLite snapshots."""
    try:
        with contextlib.closing(
            sqlite3.connect(f"file:{Path(database_path)}?mode=ro", uri=True)
        ) as con:
            quick_check = [row[0] for row in con.execute("pragma quick_check").fetchall()]
            foreign_key_violations = con.execute("pragma foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"{label} is not a valid SQLite database: {exc}") from exc
    if quick_check != ["ok"]:
        raise ValueError(f"{label} failed SQLite quick_check: {quick_check!r}")
    if foreign_key_violations:
        raise ValueError(
            f"{label} has {len(foreign_key_violations)} foreign-key violation(s)"
        )
    return {"quick_check": "ok", "foreign_key_violations": 0}


def _stage_archive_member(zf, archive_name, target_dir):
    """Copy a member to a durable sibling temp file for atomic replacement."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(prefix=".inkdrop-restore-", dir=target_dir)
    staged_path = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as dst, zf.open(archive_name) as src:
            fd = -1
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        return staged_path
    except Exception:
        staged_path.unlink(missing_ok=True)
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def _clear_state_auth_generation(state_db_path):
    """Drop a restored state database's claim about which auth store it owns."""
    state_db_path = Path(state_db_path)
    if not state_db_path.exists():
        return
    try:
        with contextlib.closing(sqlite3.connect(state_db_path)) as con:
            con.execute("delete from app_settings where key='auth.store_generation'")
            con.commit()
    except sqlite3.Error:
        pass


def restore_backup_archive(
    archive_path,
    *,
    target_config_dir=None,
    target_state_dir=None,
    path_remaps=None,
    apply=False,
    backup_dir=None,
    preserve_current_auth=False,
):
    archive_path = Path(archive_path)
    target_config_dir = Path(target_config_dir or inkdrop_runtime_config.config_dir())
    target_state_dir = Path(target_state_dir or inkdrop_runtime_config.state_dir())
    backup_dir = Path(backup_dir or inkdrop_runtime_config.backup_dir())
    path_remaps = dict(path_remaps or {})
    with zipfile.ZipFile(archive_path, "r") as zf:
        _safe_zip_members(zf)
        archive_names = set(zf.namelist())
        if STATE_DB_ARCHIVE_NAME not in archive_names:
            raise ValueError("backup archive does not contain the required state database")
        for required_name in (MANIFEST_ARCHIVE_NAME, CONFIG_EXPORT_ARCHIVE_NAME, SECRET_REFS_ARCHIVE_NAME):
            if required_name not in archive_names:
                raise ValueError(f"backup archive is missing required member: {required_name}")
        manifest = json.loads(zf.read(MANIFEST_ARCHIVE_NAME).decode("utf-8"))
        config_export = json.loads(zf.read(CONFIG_EXPORT_ARCHIVE_NAME).decode("utf-8"))
        source_values = config_export.get("values") if isinstance(config_export.get("values"), dict) else {}
        path_warnings = []
        for key in PATH_ENV_KEYS:
            value = str(source_values.get(key) or "").strip()
            if not value or value in {"<set>", "<unset>"}:
                continue
            if key in path_remaps:
                continue
            if not Path(value).exists():
                path_warnings.append(
                    {
                        "key": key,
                        "source_path": value,
                        "reason": "path_missing_on_restore_host",
                        "next_action": "Provide a path remap or update this setting after restore.",
                    }
                )
        # A dry run must prove the databases are usable, not merely that the
        # ZIP member names exist.  Extraction is isolated from restore targets.
        with tempfile.TemporaryDirectory(prefix="inkdrop-restore-preview-") as preview_dir:
            preview_root = Path(preview_dir)
            preview_state = _stage_archive_member(zf, STATE_DB_ARCHIVE_NAME, preview_root)
            database_validation = {
                "state_db": _validate_sqlite_database(preview_state, label="backup state database")
            }
            if AUTH_DB_ARCHIVE_NAME in archive_names:
                preview_auth = _stage_archive_member(zf, AUTH_DB_ARCHIVE_NAME, preview_root)
                database_validation["auth_db"] = _validate_sqlite_database(
                    preview_auth, label="backup auth database"
                )
        result = {
            "ok": True,
            "dry_run": not bool(apply),
            "archive_path": path_text(archive_path),
            "target_config_dir": path_text(target_config_dir),
            "target_state_dir": path_text(target_state_dir),
            "manifest": manifest,
            "database_validation": database_validation,
            "path_warnings": path_warnings,
            "would_restore": {
                "state_db": STATE_DB_ARCHIVE_NAME in archive_names,
                "auth_db": AUTH_DB_ARCHIVE_NAME in archive_names,
                "config_export": CONFIG_EXPORT_ARCHIVE_NAME in archive_names,
                "secret_refs": SECRET_REFS_ARCHIVE_NAME in archive_names,
            },
        }
        if not apply:
            return result
        target_config_dir.mkdir(parents=True, exist_ok=True)
        target_state_dir.mkdir(parents=True, exist_ok=True)
        staged_files = {}
        try:
            staged_files[STATE_DB_ARCHIVE_NAME] = _stage_archive_member(
                zf, STATE_DB_ARCHIVE_NAME, target_state_dir
            )
            _validate_sqlite_database(
                staged_files[STATE_DB_ARCHIVE_NAME], label="backup state database"
            )
            if AUTH_DB_ARCHIVE_NAME in archive_names:
                staged_files[AUTH_DB_ARCHIVE_NAME] = _stage_archive_member(
                    zf, AUTH_DB_ARCHIVE_NAME, target_state_dir
                )
                _validate_sqlite_database(
                    staged_files[AUTH_DB_ARCHIVE_NAME], label="backup auth database"
                )
            for archive_name in (CONFIG_EXPORT_ARCHIVE_NAME, SECRET_REFS_ARCHIVE_NAME):
                staged_files[archive_name] = _stage_archive_member(
                    zf, archive_name, target_config_dir
                )
        except Exception:
            for staged_path in staged_files.values():
                staged_path.unlink(missing_ok=True)
            raise
        # Snapshot whatever's already on disk before any restore write
        # overwrites it -- restore_portable_settings has always done this for
        # its own narrower scope (_write_settings_snapshot); this path never
        # did, so an operator restoring the wrong archive (or restoring twice)
        # had no automatic way back to what was there a moment before.
        pre_restore_snapshots = []
        try:
            state_target = target_state_dir / inkdrop_runtime_config.STATE_DB_NAME
            snapshot = _snapshot_existing_file(state_target, backup_dir)
            if snapshot:
                pre_restore_snapshots.append(path_text(snapshot))
            os.replace(staged_files.pop(STATE_DB_ARCHIVE_NAME), state_target)
            result["restored_state_db"] = path_text(state_target)
            auth_target = target_state_dir / inkdrop_auth.AUTH_STORE_DB_NAME
            if preserve_current_auth:
                # Explicitly requested: keep whatever credentials exist right
                # now and let the next open adopt them. The restored state
                # database must not claim a store generation it never met.
                _clear_state_auth_generation(target_state_dir / inkdrop_runtime_config.STATE_DB_NAME)
                result["auth_store"] = "preserved_current_auth"
            elif AUTH_DB_ARCHIVE_NAME in archive_names:
                snapshot = _snapshot_existing_file(auth_target, backup_dir)
                if snapshot:
                    pre_restore_snapshots.append(path_text(snapshot))
                os.replace(staged_files.pop(AUTH_DB_ARCHIVE_NAME), auth_target)
                for suffix in ("-wal", "-shm"):
                    Path(str(auth_target) + suffix).unlink(missing_ok=True)
                result["restored_auth_db"] = path_text(auth_target)
                result["auth_store"] = "restored_from_archive"
            elif auth_target.exists():
                # The archive predates the auth split. Leaving today's store
                # would mix old application state with current logins, so the
                # first open must rebuild it from the restored state database.
                snapshot = _snapshot_existing_file(auth_target, backup_dir)
                if snapshot:
                    pre_restore_snapshots.append(path_text(snapshot))
                auth_target.unlink()
                for suffix in ("-wal", "-shm"):
                    Path(str(auth_target) + suffix).unlink(missing_ok=True)
                result["auth_store"] = "removed_for_archive_epoch"
            else:
                result["auth_store"] = "absent"
            inkdrop_auth.reset_auth_store_cache()
            for archive_name, target_name in (
                (CONFIG_EXPORT_ARCHIVE_NAME, "inkdrop-config-export.json"),
                (SECRET_REFS_ARCHIVE_NAME, "inkdrop-secret-refs.json"),
            ):
                existing_target = target_config_dir / target_name
                snapshot = _snapshot_existing_file(existing_target, backup_dir)
                if snapshot:
                    pre_restore_snapshots.append(path_text(snapshot))
                os.replace(staged_files.pop(archive_name), existing_target)
        finally:
            for staged_path in staged_files.values():
                staged_path.unlink(missing_ok=True)
        result["restored_config_files"] = [
            path_text(target_config_dir / "inkdrop-config-export.json"),
            path_text(target_config_dir / "inkdrop-secret-refs.json"),
        ]
        result["pre_restore_snapshots"] = pre_restore_snapshots
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create or restore an InkDrop config/state backup archive.")
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="Create a config/state backup archive.")
    backup.add_argument("--config-dir")
    backup.add_argument("--state-db")
    backup.add_argument("--backup-dir")
    backup.add_argument("--label", default="manual")
    restore = sub.add_parser("restore", help="Preview or apply a config/state backup restore.")
    restore.add_argument("archive")
    restore.add_argument("--target-config-dir")
    restore.add_argument("--target-state-dir")
    restore.add_argument("--path-remap", action="append", default=[], help="Map KEY=/new/path for restored path settings.")
    restore.add_argument("--apply", action="store_true", help="Write restored state/config files.")
    restore.add_argument("--backup-dir", help="Where to snapshot existing state/config files before an --apply restore overwrites them.")
    restore.add_argument(
        "--preserve-current-auth",
        action="store_true",
        help="Keep today's logins and API keys instead of the archive's. Default restores auth from the same point in time as the rest of the archive.",
    )
    args = parser.parse_args(argv)
    if args.command == "backup":
        result = create_backup_archive(
            config_dir=args.config_dir,
            state_db_path=args.state_db,
            backup_dir=args.backup_dir,
            label=args.label,
        )
    else:
        remaps = {}
        for item in args.path_remap or []:
            if "=" not in item:
                raise SystemExit(f"invalid --path-remap {item!r}; expected KEY=/path")
            key, value = item.split("=", 1)
            remaps[key.strip()] = value.strip()
        result = restore_backup_archive(
            args.archive,
            backup_dir=args.backup_dir,
            target_config_dir=args.target_config_dir,
            target_state_dir=args.target_state_dir,
            path_remaps=remaps,
            apply=args.apply,
            preserve_current_auth=bool(args.preserve_current_auth),
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
