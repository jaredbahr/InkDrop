#!/usr/bin/env python3
"""InkDrop config/state backup and restore helpers.

The public install path needs a small, boring way to move InkDrop state between
config roots without copying secrets into support exports. This module creates a
zip archive containing:

- a SQLite state backup,
- a redacted config/settings export,
- a secret-reference manifest without secret values,
- a restore manifest with schema and path-availability warnings.

It does not copy user media, staging downloads, reader databases, or live
external-service state.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import base64
import calendar
import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import time
import zipfile
from pathlib import Path
from stat import S_ISLNK

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

from core import inkdrop_runtime_config
from core import inkdrop_auth
from core import inkdrop_settings_registry
from core import inkdrop_state
from core import inkdrop_version


BACKUP_RESTORE_SCHEMA_VERSION = 1
PORTABLE_SETTINGS_SCHEMA = "inkdrop.portable_settings.v1"
PORTABLE_SETTINGS_PRODUCT = "InkDrop"
PORTABLE_SETTINGS_MAX_BYTES = 1024 * 1024
PORTABLE_SETTINGS_MAX_COUNT = 1000
PROTOTYPE_KEYS = {"__proto__", "prototype", "constructor"}
INVALID_STORED_VALUE = object()
STATE_DB_ARCHIVE_NAME = "state/inkdrop-state.sqlite3"
AUTH_DB_ARCHIVE_NAME = "state/inkdrop-auth.sqlite3"
# The completion/import ledger, which lives in the state directory next to the
# other two databases but was never carried by a backup -- and, worse, was not
# named in the manifest's `contains` block either, so nothing told an operator
# it was missing. Most of what it holds a filesystem rescan can rebuild (at a
# cost: a full SLSKD backlog scan is ~42 minutes, see inkdrop_container_scheduler),
# but `artifact_bad_content_memory` and `source_failures` are *learned* -- no
# scan reconstructs them, so a restored instance re-learns them by re-downloading
# the same bad releases it had already ruled out. Measured 6.3 MiB against a
# ~1.7 GB archive on the production instance, so carrying it costs nothing.
COMPLETION_DB_ARCHIVE_NAME = "state/imported-files.sqlite3"
CONFIG_EXPORT_ARCHIVE_NAME = "config/inkdrop-config-export.json"
SECRET_REFS_ARCHIVE_NAME = "config/inkdrop-secret-refs.json"
MANIFEST_ARCHIVE_NAME = "manifest.json"
# How many scheduled archives to keep, and how old one may get. Operator
# policy, so both live in app_settings and are editable from Settings; the
# environment variables stay honoured underneath for installs that already set
# them in .env. See resolve_backup_retention() for the precedence.
BACKUP_RETENTION_COUNT_SETTING_KEY = "backup.retention_count"
BACKUP_RETENTION_DAYS_SETTING_KEY = "backup.retention_days"
BACKUP_RETENTION_COUNT_ENV_KEY = "INKDROP_BACKUP_RETENTION_COUNT"
BACKUP_RETENTION_DAYS_ENV_KEY = "INKDROP_BACKUP_RETENTION_DAYS"
DEFAULT_BACKUP_RETENTION_COUNT = 6
DEFAULT_BACKUP_RETENTION_DAYS = 28
SECRET_KEY_MARKERS = ("API_KEY", "PASSWORD", "TOKEN", "SECRET", "USERNAME")
CREDENTIAL_PASSPHRASE_MIN_LENGTH = 8
CREDENTIAL_KDF_ITERATIONS = 480_000
CREDENTIAL_TABLES = ("provider_configs", "notification_connectors")
# Naming-format templates hold tokens like "{Series Title} ({Year})", never
# host paths; comic_issue_format already exports, this keeps its sibling from
# being excluded for having FOLDER in its name.
PORTABLE_NAMING_TEMPLATE_KEYS = frozenset({"media_management.series_folder_format"})
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


def sanitized_provider_settings(settings):
    """Split provider settings into exportable values and dropped credential fields.

    Shared by the portability JSON export and the full backup archive's raw
    state-DB redaction pass (see redact_provider_secrets below) so both code
    paths that ship a copy of provider_configs.settings_json agree on exactly
    which fields are credentials.
    """
    kept = {}
    removed = []
    # Fields the provider itself declared secret are dropped first: names like
    # discord_webhook_url carry credentials without matching any credential
    # name pattern.
    declared = settings.get("secret_fields") if isinstance(settings, dict) else None
    declared = {str(name) for name in declared} if isinstance(declared, list) else set()
    for key in sorted(settings or {}, key=str):
        name = str(key)
        if name == "secret_ref":
            kept[name] = str(settings[key] or "")
            continue
        if name in declared:
            removed.append(name)
            continue
        if is_secret_key(name):
            removed.append(name)
            continue
        value = settings[key]
        if isinstance(value, (dict, list)):
            # Nested provider settings are rare and may bury credentials one
            # level down; drop them rather than guessing which leaves are safe.
            removed.append(name)
            continue
        kept[name] = value
    return kept, removed


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


# --------------------------------------------------------------------------
# Optional encrypted credentials, opt-in at export time. The portable
# settings document above never carries these -- sanitized_provider_settings()
# strips them the same way redact_provider_secrets() does for the full backup
# archive. This is the one path that CAN include them, because a user
# rebuilding a fresh install from a settings export otherwise has to manually
# re-enter every download-client password and notification webhook by hand.
# They're encrypted with a passphrase set at export time (never derived from
# or stored alongside anything else InkDrop already knows), so the exported
# file is only as sensitive as that passphrase -- not plaintext-sensitive on
# its own, unlike everything else this module ever writes to disk.
# --------------------------------------------------------------------------

def _validate_credential_passphrase(passphrase):
    text = str(passphrase or "")
    if len(text) < CREDENTIAL_PASSPHRASE_MIN_LENGTH:
        raise ValueError(f"credentials passphrase must be at least {CREDENTIAL_PASSPHRASE_MIN_LENGTH} characters")
    return text


def _credential_fernet(passphrase, salt, iterations=CREDENTIAL_KDF_ITERATIONS):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=int(iterations))
    return Fernet(base64.urlsafe_b64encode(kdf.derive(str(passphrase or "").encode("utf-8"))))


def _gather_credential_secrets(con):
    """{"provider_configs": {id: {field: value}}, "notification_connectors":
    {id: {field: value}}} for every credential-shaped field
    sanitized_provider_settings() (the same filter redact_provider_secrets()
    applies to the full backup archive) would otherwise strip. Tables/rows/
    fields with nothing secret are omitted entirely, not included empty.

    notification_connectors is created lazily by inkdrop_notification_store
    on first use, not by inkdrop_state.init_schema() -- an install that has
    never opened notification settings genuinely has no such table yet.
    Checking sqlite_master first (the same guard redact_provider_secrets()
    already uses for the full backup archive) keeps that a normal "nothing to
    gather" case instead of an unhandled OperationalError that would take the
    whole export down with it.
    """
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    credentials = {}
    provider_rows = con.execute("select id, settings_json from provider_configs").fetchall() if "provider_configs" in tables else []
    provider_credentials = {}
    for provider_id, settings_json in provider_rows:
        try:
            settings = json.loads(settings_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(settings, dict):
            continue
        _kept, removed = sanitized_provider_settings(settings)
        fields = {name: settings[name] for name in removed if name in settings}
        if fields:
            provider_credentials[str(provider_id)] = fields
    if provider_credentials:
        credentials["provider_configs"] = provider_credentials

    if "notification_connectors" not in tables:
        return credentials
    try:
        from core import inkdrop_notifications as notifications
    except Exception:
        notifications = None
    connector_credentials = {}
    connector_rows = con.execute("select id, type, settings_json from notification_connectors").fetchall()
    for connector_id, connector_type, settings_json in connector_rows:
        try:
            settings = json.loads(settings_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(settings, dict):
            continue
        cls = notifications.PROVIDER_CLASSES.get(connector_type) if notifications else None
        declared = [field["key"] for field in cls.config_fields if field.get("secret")] if cls else []
        settings_with_declared = dict(settings)
        settings_with_declared["secret_fields"] = sorted(set(settings.get("secret_fields") or []) | set(declared))
        _kept, removed = sanitized_provider_settings(settings_with_declared)
        fields = {name: settings[name] for name in removed if name in settings}
        if fields:
            connector_credentials[str(connector_id)] = fields
    if connector_credentials:
        credentials["notification_connectors"] = connector_credentials
    return credentials


def _credential_count(credentials):
    return sum(len(fields) for table in (credentials or {}).values() for fields in table.values())


def encrypt_credentials_payload(credentials, passphrase):
    passphrase = _validate_credential_passphrase(passphrase)
    salt = os.urandom(16)
    ciphertext = _credential_fernet(passphrase, salt).encrypt(_canonical_json(credentials).encode("utf-8"))
    return {
        "kdf": "pbkdf2-sha256",
        "iterations": CREDENTIAL_KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "ciphertext": ciphertext.decode("ascii"),
        "count": _credential_count(credentials),
    }


def decrypt_credentials_payload(blob, passphrase):
    """Raises ValueError with a message safe to show the user directly --
    never includes the passphrase, the ciphertext, or decrypted values."""
    if not isinstance(blob, dict):
        raise ValueError("credentials block is malformed")
    try:
        salt = base64.b64decode(str(blob.get("salt") or ""), validate=True)
        ciphertext = str(blob.get("ciphertext") or "").encode("ascii")
        iterations = int(blob.get("iterations") or 0)
    except Exception as exc:
        raise ValueError("credentials block is malformed") from exc
    if not salt or not ciphertext or iterations <= 0:
        raise ValueError("credentials block is malformed")
    fernet = _credential_fernet(passphrase, salt, iterations=iterations)
    try:
        plaintext = fernet.decrypt(ciphertext)
    except InvalidToken as exc:
        raise ValueError("incorrect passphrase, or the credentials in this file are corrupted") from exc
    try:
        credentials = json.loads(plaintext.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("decrypted credentials are not valid JSON") from exc
    if not isinstance(credentials, dict) or any(table not in CREDENTIAL_TABLES for table in credentials):
        raise ValueError("decrypted credentials have an unexpected shape")
    return credentials


def _credential_restore_plan(con, credentials):
    """Never returns a decrypted value, only which provider/connector rows
    and field names would change -- the plan is shown in a preview response
    before Apply, and a plaintext credential has no business round-tripping
    back out over the network once it's been encrypted."""
    changes = []
    unknown = []
    existing_tables = _existing_credential_tables(con)
    for table in CREDENTIAL_TABLES:
        rows = credentials.get(table)
        if not isinstance(rows, dict):
            continue
        if table not in existing_tables:
            for row_id in rows:
                unknown.append({"table": table, "id": str(row_id), "reason": "table_not_present_on_this_install"})
            continue
        for row_id, fields in rows.items():
            if not isinstance(fields, dict):
                continue
            existing = con.execute(f"select settings_json from {table} where id=?", (str(row_id),)).fetchone()
            if not existing:
                unknown.append({"table": table, "id": str(row_id), "reason": "row_not_present_on_this_install"})
                continue
            try:
                existing_settings = json.loads(existing[0] or "{}")
            except (TypeError, ValueError):
                existing_settings = {}
            for field in sorted(fields):
                if not isinstance(existing_settings, dict) or field not in existing_settings:
                    unknown.append({"table": table, "id": str(row_id), "field": field, "reason": "field_not_present_on_this_install"})
                    continue
                changes.append({"table": table, "id": str(row_id), "field": field})
    return {"changes": changes, "unknown": unknown}


def _existing_credential_tables(con):
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    return {table for table in CREDENTIAL_TABLES if table in tables}


def _apply_credential_restore(con, credentials, now):
    applied = 0
    existing_tables = _existing_credential_tables(con)
    for table in CREDENTIAL_TABLES:
        if table not in existing_tables:
            continue
        rows = credentials.get(table)
        if not isinstance(rows, dict):
            continue
        for row_id, fields in rows.items():
            if not isinstance(fields, dict):
                continue
            existing = con.execute(f"select settings_json from {table} where id=?", (str(row_id),)).fetchone()
            if not existing:
                continue
            try:
                existing_settings = json.loads(existing[0] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(existing_settings, dict):
                continue
            changed = False
            for field, value in fields.items():
                if field not in existing_settings:
                    continue
                existing_settings[field] = value
                changed = True
                applied += 1
            if changed:
                con.execute(
                    f"update {table} set settings_json=?, updated_at=? where id=?",
                    (_canonical_json(existing_settings), now, str(row_id)),
                )
    return applied


def _portable_exclusion_reason(key, value):
    key_text = str(key or "")
    if key_text.lower().startswith("auth."):
        return "security_or_identity"
    # Booleans, numbers, and closed choice sets cannot carry a host path or a
    # credential, whatever their name contains. Without this, policy settings
    # like media_management.apply_planned_path and setup.local_folder_only_mode
    # were dropped from every export for matching PATH/FOLDER by name.
    kind = inkdrop_settings_registry.field_schema(key_text).get("kind")
    if kind in {"boolean", "number", "choice"}:
        return ""
    if key_text in PORTABLE_NAMING_TEMPLATE_KEYS:
        return ""
    upper = key_text.upper()
    if any(marker in upper for marker in SECRET_KEY_MARKERS):
        return "secret_or_credential"
    if any(marker in upper for marker in ("PATH", "ROOT", "DIRECTORY", "FOLDER", "URL", "HOST", "ORIGIN", "PROXY")):
        return "private_or_host_specific"
    return ""


def _portable_rows(con):
    return con.execute("select key,value_json from app_settings order by key").fetchall()


def _portable_document_from_rows(rows, *, now=None, version=None, credentials_encrypted=None):
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
            "provider_credentials": bool(credentials_encrypted),
            "private_paths": False,
            "media": False,
            "tasks_history_users_sessions": False,
        },
    }
    if credentials_encrypted:
        document["credentials_encrypted"] = credentials_encrypted
    document["checksum"] = _settings_checksum(document)
    return document


def export_portable_settings(db_path, *, now=None, version=None, include_credentials=False, passphrase=None):
    with inkdrop_state.connect_read(db_path) as con:
        rows = _portable_rows(con)
        credentials_encrypted = None
        if include_credentials:
            credentials = _gather_credential_secrets(con)
            credentials_encrypted = encrypt_credentials_payload(credentials, passphrase)
    return _portable_document_from_rows(rows, now=now, version=version, credentials_encrypted=credentials_encrypted)


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


def _credentials_plan_section(con, document, passphrase):
    """Never touches the DB. Returns a plan section safe to send back to the
    client as-is: counts and which rows/fields would change, never a
    decrypted value. `ok` gates whether Apply may proceed when credentials
    are present -- present-but-unverified must block Apply rather than
    silently apply settings while skipping credentials with no signal that
    anything was left out."""
    blob = document.get("credentials_encrypted")
    if not blob:
        return {"present": False, "count": 0, "passphrase_provided": False, "verified": False, "ok": True}
    count = int((blob or {}).get("count") or 0) if isinstance(blob, dict) else 0
    if not passphrase:
        return {"present": True, "count": count, "passphrase_provided": False, "verified": False, "ok": False}
    try:
        credentials = decrypt_credentials_payload(blob, passphrase)
    except ValueError as exc:
        return {"present": True, "count": count, "passphrase_provided": True, "verified": False, "ok": False, "error": str(exc)}
    row_plan = _credential_restore_plan(con, credentials)
    return {
        "present": True,
        "count": count,
        "passphrase_provided": True,
        "verified": True,
        "ok": True,
        "changes": row_plan["changes"],
        "unknown": row_plan["unknown"],
        "_credentials": credentials,
    }


def restore_portable_settings(db_path, raw_document, *, apply=False, backup_dir=None, now=None, passphrase=None):
    document = parse_portable_settings_document(raw_document)
    if not apply:
        with inkdrop_state.connect_read(db_path) as con:
            rows = _portable_rows(con)
            credentials_plan = _credentials_plan_section(con, document, passphrase)
        plan = _portable_settings_plan(document, rows)
        public_plan = {key: value for key, value in plan.items() if key != "validated"}
        public_plan["credentials"] = {key: value for key, value in credentials_plan.items() if key != "_credentials"}
        return {
            "ok": bool(plan["ok"]) and bool(credentials_plan["ok"]),
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
        credentials_plan = _credentials_plan_section(con, document, passphrase)
        public_plan = {key: value for key, value in plan.items() if key != "validated"}
        public_plan["credentials"] = {key: value for key, value in credentials_plan.items() if key != "_credentials"}
        result = {
            "ok": bool(plan["ok"]) and bool(credentials_plan["ok"]),
            "applied": False,
            "dry_run": False,
            "source": {"product_version": document.get("product_version"), "exported_at": document.get("exported_at"), "checksum": document.get("checksum")},
            "plan": public_plan,
        }
        if not plan["ok"] or not credentials_plan["ok"]:
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
        credentials_applied = 0
        if credentials_plan.get("_credentials"):
            credentials_applied = _apply_credential_restore(con, credentials_plan["_credentials"], timestamp)
        inkdrop_state.record_settings_history(
            con,
            "settings_restore",
            "settings_backup",
            str(document.get("checksum") or "")[-16:],
            "settings",
            f"Restored {len(changed_keys)} portable setting(s)" + (f", {credentials_applied} credential(s)" if credentials_applied else ""),
            {
                "changed_keys": changed_keys,
                "changed_count": len(changed_keys),
                "unknown_count": len(plan["unknown"]),
                "source_schema": document.get("schema"),
                "credentials_applied": credentials_applied,
            },
            timestamp,
        )
        inkdrop_state.update_sync_meta(con, timestamp, "settings_restore")
        con.commit()
    inkdrop_state.clear_settings_caches()
    result.update({
        "ok": True,
        "applied": True,
        "snapshot": path_text(snapshot),
        "changed_count": len(changed_keys),
        "credentials_applied": credentials_applied,
    })
    return result


def _redact_notification_connectors(con, report):
    """Sibling pass for notification_connectors.settings_json -- Phase 1's
    per-instance connector table holds the exact same shape of raw,
    vault-less credentials (a Discord webhook URL, a Pushover token/key)
    that provider_configs holds, but under type-generic key names
    (webhook_url, api_token, user_key) that is_secret_key()'s naming
    heuristic doesn't catch. Rather than duplicate a second secret-field
    registry here, ask inkdrop_notifications' PROVIDER_CLASSES -- the one
    place a connector type's config_fields (and their secret=True flags)
    are actually declared -- which fields to drop for each row's type.
    """
    try:
        from core import inkdrop_notifications as notifications
    except Exception:
        return
    rows = con.execute("select id, type, settings_json from notification_connectors").fetchall()
    for connector_id, connector_type, settings_json in rows:
        report["rows_scanned"] += 1
        try:
            settings = json.loads(settings_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(settings, dict):
            continue
        cls = notifications.PROVIDER_CLASSES.get(connector_type)
        declared = [field["key"] for field in cls.config_fields if field.get("secret")] if cls else []
        settings = dict(settings)
        settings["secret_fields"] = sorted(set(settings.get("secret_fields") or []) | set(declared))
        kept, removed = sanitized_provider_settings(settings)
        if not removed:
            continue
        con.execute(
            "update notification_connectors set settings_json=? where id=?",
            (_canonical_json(kept), connector_id),
        )
        report["rows_redacted"] += 1
        report["fields_redacted"] += len(removed)


def redact_provider_secrets(target_db: Path):
    """Null out credential-shaped provider_configs.settings_json fields (and
    notification_connectors.settings_json -- see _redact_notification_connectors)
    in a private backup copy, using the same field-name rules
    sanitized_provider_settings() already applies to the portability JSON
    export. Neither table has vault indirection like download_client_instances
    does -- callers store credentials (e.g. a Komga password, a Discord
    webhook URL) directly in settings_json -- so the raw SQLite copy embedded
    in the full backup archive must go through the same filter the JSON
    export already applies, or it ships them in plaintext.

    Runs against `target_db` in place. The copy may have inherited WAL mode
    from the live source, in which case a plain UPDATE would land in a
    `-wal` sibling file that create_backup_archive() never zips -- forcing
    `journal_mode=delete` first ensures the redaction is actually present in
    the single file that gets shipped.
    """
    report = {"ok": True, "rows_scanned": 0, "rows_redacted": 0, "fields_redacted": 0}
    with contextlib.closing(sqlite3.connect(target_db)) as con:
        con.execute("pragma journal_mode=delete")
        tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        if "provider_configs" in tables:
            rows = con.execute("select id, settings_json from provider_configs").fetchall()
            for provider_id, settings_json in rows:
                report["rows_scanned"] += 1
                try:
                    settings = json.loads(settings_json or "{}")
                except (TypeError, ValueError):
                    continue
                if not isinstance(settings, dict):
                    continue
                kept, removed = sanitized_provider_settings(settings)
                if not removed:
                    continue
                con.execute(
                    "update provider_configs set settings_json=? where id=?",
                    (_canonical_json(kept), provider_id),
                )
                report["rows_redacted"] += 1
                report["fields_redacted"] += len(removed)
        if "notification_connectors" in tables:
            _redact_notification_connectors(con, report)
        con.commit()
    return report


def backup_sqlite_db(source_db: Path, target_db: Path):
    target_db.parent.mkdir(parents=True, exist_ok=True)
    if not source_db.exists():
        return {"ok": False, "reason": "state_db_missing", "source": path_text(source_db)}
    # sqlite3.Connection.backup() is the correct, WAL-safe hot-backup API --
    # it copies whatever is in the WAL along with the main file, unlike a raw
    # file copy, which would silently drop any transactions still sitting in
    # a WAL-mode source's -wal file at copy time (the live state DB runs in
    # WAL mode). A previous shutil.copy2() fallback ran on any sqlite3.Error,
    # which could ship a stale/torn snapshot with no indication anything was
    # wrong. A backup that might not be a coherent snapshot is worse than a
    # loud failure, so transient errors (e.g. SQLITE_BUSY lock contention)
    # get a few retries, and the backup fails closed rather than silently
    # degrading if the safe API still can't succeed.
    attempts = 3
    last_error = None
    for attempt in range(1, attempts + 1):
        target_db.unlink(missing_ok=True)
        try:
            with contextlib.closing(sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)) as src, contextlib.closing(sqlite3.connect(target_db)) as dst:
                src.backup(dst)
                dst.commit()
            last_error = None
            break
        except sqlite3.Error as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.5)
    if last_error is not None:
        target_db.unlink(missing_ok=True)
        return {
            "ok": False,
            "reason": "sqlite_backup_failed",
            "source": path_text(source_db),
            "error": f"{type(last_error).__name__}: {last_error}",
            "attempts": attempts,
        }
    # Born hardened, not hardened afterwards. This copy has just been through
    # auth sanitisation and provider-secret redaction, but redaction is not
    # removal -- the file still carries enough to be worth 0600, and leaving
    # it at the process umask produced a 31.7 GB backup sitting at 0644.
    # create_backup_archive() already does this for the archive path
    # (os.fchmod(fd, 0o600)); the raw-copy path did not.
    try:
        os.chmod(target_db, 0o600)
    except OSError:
        # Never fail a good backup over a permission call; the copy itself is
        # the artifact worth keeping.
        pass
    _validate_sqlite_database(target_db, label="state backup copy")
    auth_safety = inkdrop_auth.sanitize_auth_database_copy(target_db)
    if not auth_safety.get("ok"):
        target_db.unlink(missing_ok=True)
        raise ValueError("state backup credential-safety validation failed")
    provider_secret_redaction = redact_provider_secrets(target_db)
    return {
        "ok": True,
        "method": "sqlite_backup",
        "source": path_text(source_db),
        "bytes": target_db.stat().st_size,
        "auth_credential_safety": auth_safety,
        "provider_secret_redaction": provider_secret_redaction,
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
        # Staging is a full, uncompressed copy of every database being backed
        # up, and it lands in the system temp directory -- not in backup_dir,
        # which is the filesystem an operator would think to check. On the
        # production instance that is an 11.2 GiB write into the container's
        # /tmp. restore_backup_archive() has always preflighted its own
        # staging with _require_free_space(); this path never did, so a
        # too-small temp filesystem surfaced as an ENOSPC partway through
        # rather than as a refusal before any work started.
        #
        # Only the staging copy is preflighted here, because it is the one
        # number that is known exactly up front (it is a byte-for-byte copy of
        # files already on disk). The finished archive's size is not knowable
        # before compression, and over-estimating it is precisely what made
        # restore demand hundreds of gigabytes to preview a legitimate backup
        # (see the member_real_sizes comment in restore_backup_archive) -- so
        # this deliberately does not guess at it.
        staging_root = Path(tempfile.gettempdir())
        staging_budget = _existing_file_size(state_db_path)
        staging_budget += _existing_file_size(inkdrop_auth.auth_store_path(state_db_path))
        completion_db_path = Path(state_db_path).parent / inkdrop_runtime_config.IMPORTED_FILES_DB_NAME
        staging_budget += _existing_file_size(completion_db_path)
        _require_free_space(staging_root, staging_budget, label="backup staging copy")
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
            # Optional: installs that have never run an import do not have one
            # yet, and an archive without it stays restorable (the restore
            # path treats it the same way it treats a pre-auth-split archive).
            temp_completion_db = tmp_root / COMPLETION_DB_ARCHIVE_NAME
            completion_db_backup = backup_sqlite_db(completion_db_path, temp_completion_db)
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
                "completion_db": bool(completion_db_backup.get("ok")),
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
                "completion_db_path": path_text(completion_db_path),
                "backup_dir": path_text(backup_dir),
            },
            "state_db_backup": db_backup,
            "auth_db_backup": auth_db_backup,
            "completion_db_backup": completion_db_backup,
            "credential_policy": "Authentication material in the state backup is cryptographically hashed; plaintext passwords, sessions, recovery tokens, and API keys are never exported. Provider credentials stored directly in provider settings (see state_db_backup.provider_secret_redaction) are redacted from the embedded state database copy the same way they are from the portability export.",
        }
            with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(MANIFEST_ARCHIVE_NAME, json.dumps(manifest, indent=2, sort_keys=True))
                zf.writestr(CONFIG_EXPORT_ARCHIVE_NAME, json.dumps(config_export, indent=2, sort_keys=True))
                zf.writestr(SECRET_REFS_ARCHIVE_NAME, json.dumps(secret_refs, indent=2, sort_keys=True))
                if db_backup.get("ok"):
                    zf.write(temp_db, STATE_DB_ARCHIVE_NAME)
                if auth_db_backup.get("ok"):
                    zf.write(temp_auth_db, AUTH_DB_ARCHIVE_NAME)
                if completion_db_backup.get("ok"):
                    zf.write(temp_completion_db, COMPLETION_DB_ARCHIVE_NAME)
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


# inkdrop-backup-{YYYYmmdd-HHMMSS}-{label}.zip, matching compact_stamp()'s own
# format -- create_backup_archive() names every archive this way, so listing/
# pruning read the timestamp back out of the filename itself rather than
# keeping a separate index that could drift from what's actually on disk.
# A backup that dies between mkstemp() and os.replace() leaves two artifacts:
# the partial archive (.inkdrop-backup-*.tmp, in backup_dir) and the staging
# directory holding a full uncompressed copy of every database
# (inkdrop-backup-build-*, in the system temp dir). create_backup_archive()
# cleans both up on any exception, but SIGKILL is not an exception -- a
# container recreate, an OOM kill, or a redeploy mid-backup skips the handler
# entirely, so no amount of exception handling closes this.
#
# prune_backup_archives() does not help: it deliberately only ever considers
# files matching BACKUP_ARCHIVE_NAME_RE, which is what makes it safe to point
# at a directory that also holds hand-made pre-migration snapshots. So nothing
# swept these, and on an 11 GiB database each abandoned staging directory is
# an 11 GiB leak that persists for as long as its filesystem does.
_STALE_BACKUP_WORKSPACE_MIN_AGE_SECONDS = int(
    os.environ.get("INKDROP_BACKUP_STALE_WORKSPACE_MIN_AGE_SECONDS") or 6 * 3600
)


def sweep_stale_backup_workspaces(*, backup_dir=None, temp_dir=None, now=None, min_age_seconds=None):
    """Remove abandoned backup temp files and staging directories.

    Age-gated rather than unconditional: a backup of a multi-gigabyte database
    legitimately runs for minutes, and this is called at container start while
    the *other* container may be mid-backup against the same shared backup
    directory. Deleting a live run's workspace would turn a slow backup into a
    corrupt one, so anything younger than the threshold is left alone.
    """
    backup_dir = Path(backup_dir or inkdrop_runtime_config.backup_dir())
    temp_dir = Path(temp_dir if temp_dir is not None else tempfile.gettempdir())
    now = float(now if now is not None else time.time())
    min_age = int(min_age_seconds if min_age_seconds is not None else _STALE_BACKUP_WORKSPACE_MIN_AGE_SECONDS)
    report = {"removed_temp_archives": [], "removed_staging_dirs": [], "skipped_recent": 0, "errors": []}

    def _too_young(path):
        try:
            return (now - path.stat().st_mtime) < min_age
        except OSError:
            return True

    candidates = []
    if backup_dir.is_dir():
        # Both the create path (.inkdrop-backup-*.tmp) and the upload path
        # (.inkdrop-backup-upload-*.zip.tmp, inkdrop_web) stage here.
        candidates.extend(sorted(backup_dir.glob(".inkdrop-backup-*.tmp")))
        candidates.extend(sorted(backup_dir.glob(".inkdrop-backup-upload-*.zip.tmp")))
    for path in candidates:
        if not path.is_file():
            continue
        if _too_young(path):
            report["skipped_recent"] += 1
            continue
        try:
            path.unlink()
            report["removed_temp_archives"].append(path_text(path))
        except OSError as exc:
            report["errors"].append({"path": path_text(path), "error": str(exc)})

    if temp_dir.is_dir():
        for path in sorted(temp_dir.glob("inkdrop-backup-build-*")):
            if not path.is_dir():
                continue
            if _too_young(path):
                report["skipped_recent"] += 1
                continue
            try:
                shutil.rmtree(path)
                report["removed_staging_dirs"].append(path_text(path))
            except OSError as exc:
                report["errors"].append({"path": path_text(path), "error": str(exc)})
    return report


BACKUP_ARCHIVE_NAME_RE = re.compile(r"^inkdrop-backup-(\d{8}-\d{6})-(.+)\.zip$")


def _parsed_backup_archive(path):
    match = BACKUP_ARCHIVE_NAME_RE.match(path.name)
    if not match:
        return None
    try:
        created_at = calendar.timegm(time.strptime(match.group(1), "%Y%m%d-%H%M%S"))
    except ValueError:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "name": path.name,
        "label": match.group(2),
        "created_at": created_at,
        "created_at_iso": utc_stamp(created_at),
        "bytes": stat.st_size,
    }


def list_backup_archives(backup_dir=None):
    """Every recognized backup archive on disk, newest first.

    Reads the directory rather than any stored index, so a file dropped in or
    removed outside InkDrop (an operator's own cron, a manual scp) shows up
    correctly on the next call -- there is nothing else to keep in sync.
    """
    backup_dir = Path(backup_dir or inkdrop_runtime_config.backup_dir())
    if not backup_dir.is_dir():
        return []
    archives = []
    for entry in backup_dir.iterdir():
        if not entry.is_file():
            continue
        parsed = _parsed_backup_archive(entry)
        if parsed:
            archives.append(parsed)
    archives.sort(key=lambda item: item["created_at"], reverse=True)
    return archives


def _stored_retention_setting(state_db_path, key):
    """The saved value for a retention setting, but only once a person set it.

    ``source`` is what separates "the operator chose this" from "the runtime
    seeded the default row on first sync". Only the former may override the
    environment: an install that already carries INKDROP_BACKUP_RETENTION_COUNT
    in its .env must not silently drop to the seeded default just because the
    settings row exists. Same shape as slskd_active_stall_policy(), which makes
    the same distinction for the same reason.

    Returns None whenever there is nothing trustworthy to use -- no database, no
    row, a row the runtime seeded, or a stored value that no longer validates
    (bounds can tighten between releases, and a stale out-of-range value must
    fall through to the default rather than reach prune_backup_archives()).
    """
    try:
        setting = inkdrop_state.app_setting(state_db_path, key)
    except Exception:
        return None
    if not setting:
        return None
    if str(setting.get("source") or "runtime").strip().lower() != "user":
        return None
    try:
        return int(inkdrop_settings_registry.validate_value(key, setting.get("value")))
    except (TypeError, ValueError):
        return None


def _environment_retention(environ, key, minimum, maximum):
    raw = str((environ if environ is not None else os.environ).get(key) or "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, parsed))


def resolve_backup_retention(*, state_db_path=None, environ=None, retention_days=None, retention_count=None):
    """Work out the retention limits one scheduled pass should apply.

    Precedence, highest first: an explicit argument (a human running the CLI by
    hand, or a test), then the saved Settings value, then the environment
    variable, then the shipped default. Resolution happens per run rather than
    when the scheduler builds its job list, so changing the setting in Settings
    takes effect on the next scheduled backup without restarting the container.

    ``source`` in the result is what the UI reports back, so an operator whose
    .env pins the value can see why the box they are looking at is not the one
    in charge.
    """
    if state_db_path is None:
        try:
            state_db_path = inkdrop_runtime_config.state_db_path(environ if environ is not None else os.environ)
        except Exception:
            state_db_path = None
    resolved = {}
    for name, explicit, setting_key, env_key, fallback in (
        (
            "retention_count",
            retention_count,
            BACKUP_RETENTION_COUNT_SETTING_KEY,
            BACKUP_RETENTION_COUNT_ENV_KEY,
            DEFAULT_BACKUP_RETENTION_COUNT,
        ),
        (
            "retention_days",
            retention_days,
            BACKUP_RETENTION_DAYS_SETTING_KEY,
            BACKUP_RETENTION_DAYS_ENV_KEY,
            DEFAULT_BACKUP_RETENTION_DAYS,
        ),
    ):
        schema = inkdrop_settings_registry.field_schema(setting_key)
        if explicit is not None:
            resolved[name] = int(explicit)
            resolved[f"{name}_source"] = "explicit"
            continue
        stored = _stored_retention_setting(state_db_path, setting_key) if state_db_path else None
        if stored is not None:
            resolved[name] = stored
            resolved[f"{name}_source"] = "settings"
            continue
        from_env = _environment_retention(environ, env_key, int(schema["min"]), int(schema["max"]))
        if from_env is not None:
            resolved[name] = from_env
            resolved[f"{name}_source"] = "environment"
            continue
        resolved[name] = int(fallback)
        resolved[f"{name}_source"] = "default"
    return resolved


def backup_retention_contract(*, state_db_path=None, environ=None):
    """Effective retention plus the schema the Settings panel renders from."""
    resolved = resolve_backup_retention(state_db_path=state_db_path, environ=environ)
    contract = {"ok": True}
    for name, setting_key, fallback in (
        ("retention_count", BACKUP_RETENTION_COUNT_SETTING_KEY, DEFAULT_BACKUP_RETENTION_COUNT),
        ("retention_days", BACKUP_RETENTION_DAYS_SETTING_KEY, DEFAULT_BACKUP_RETENTION_DAYS),
    ):
        schema = inkdrop_settings_registry.field_schema(setting_key)
        contract[name] = {
            "key": setting_key,
            "value": resolved[name],
            "source": resolved[f"{name}_source"],
            "default": int(fallback),
            "minimum": int(schema["min"]),
            "maximum": int(schema["max"]),
            "units": schema.get("units"),
        }
    return contract


def prune_backup_archives(backup_dir=None, *, retention_days=28, retention_count=0, label=None, now=None):
    """Delete archives past retention, sonarr-style.

    Two independent limits, either of which can retire an archive:

    * ``retention_days`` -- age. The original limit.
    * ``retention_count`` -- how many to keep, newest first. 0 disables it.
      Age alone does not bound disk usage: each archive of the production
      database is ~2.25GB, so shortening the backup interval to daily under a
      28-day window would quietly hold ~63GB. The count cap is what makes the
      footprint predictable regardless of how the interval is configured.

    Always keeps at least the single newest archive matching `label` (or the
    newest archive overall when label is None), even if it is itself past
    retention -- a misconfigured retention window (0, or a clock that jumped)
    must never delete every backup in the directory and leave none.

    Only ever considers files matching InkDrop's own archive naming
    convention (see BACKUP_ARCHIVE_NAME_RE), which is what makes this safe to
    point at the shared backups directory: that directory also holds one-off
    pre-migration snapshots written by repair tooling, and those are deliberate
    rollback points that blind age-based deletion must not touch.
    """
    backup_dir = Path(backup_dir or inkdrop_runtime_config.backup_dir())
    now = float(now if now is not None else time.time())
    cutoff = now - max(0, int(retention_days)) * 86400
    keep_newest = max(0, int(retention_count or 0))
    archives = list_backup_archives(backup_dir)
    if label is not None:
        scoped = [item for item in archives if item["label"] == label]
    else:
        scoped = archives
    deleted = []
    kept = []
    for index, item in enumerate(scoped):
        too_old = item["created_at"] < cutoff
        beyond_count = keep_newest > 0 and index >= keep_newest
        if index == 0 or not (too_old or beyond_count):
            kept.append(item["name"])
            continue
        try:
            (backup_dir / item["name"]).unlink()
            deleted.append(item["name"])
        except OSError as exc:
            kept.append(item["name"])
            item["prune_error"] = f"{type(exc).__name__}: {exc}"
    return {
        "ok": True,
        "deleted": deleted,
        "kept": kept,
        "retention_days": int(retention_days),
        "retention_count": keep_newest,
    }


def run_scheduled_backup(*, config_dir=None, state_db_path=None, backup_dir=None, environ=None, interval_days=7, retention_days=None, retention_count=None, now=None):
    """Create a new "scheduled" backup only once interval_days have passed
    since the last one, then prune -- the interval check only ever looks at
    prior "scheduled" archives, so a manual backup an operator triggers from
    the UI never delays or resets the automatic cadence.

    Leaving either retention limit as None resolves it from Settings, then the
    environment, then the shipped default (resolve_backup_retention). That
    lookup happens here, on every pass, which is what lets an operator change
    retention in Settings and have the next scheduled backup honour it without
    restarting anything.
    """
    now = float(now if now is not None else time.time())
    retention = resolve_backup_retention(
        state_db_path=state_db_path,
        environ=environ,
        retention_days=retention_days,
        retention_count=retention_count,
    )
    retention_days = retention["retention_days"]
    retention_count = retention["retention_count"]
    backup_dir = Path(backup_dir or inkdrop_runtime_config.backup_dir(environ if environ is not None else os.environ))
    existing = [item for item in list_backup_archives(backup_dir) if item["label"] == "scheduled"]
    due = not existing or (now - existing[0]["created_at"]) >= max(1, int(interval_days)) * 86400
    created = None
    if due:
        created = create_backup_archive(
            config_dir=config_dir,
            state_db_path=state_db_path,
            backup_dir=backup_dir,
            environ=environ,
            label="scheduled",
        )
    prune_result = prune_backup_archives(
        backup_dir,
        retention_days=retention_days,
        retention_count=retention_count,
        label="scheduled",
        now=now,
    )
    return {"ok": True, "due": due, "created": created, "prune": prune_result, "retention": retention}


# A backup archive InkDrop itself writes only ever has 3-5 members (manifest,
# config export, secret refs, state DB, optional auth DB); this stays well
# above that so a genuine future addition doesn't need a version bump here,
# while still rejecting an archive whose central directory claims thousands
# of entries.
_ARCHIVE_MAX_MEMBERS = 16
_ARCHIVE_SUPPORTED_COMPRESS_TYPES = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP_EOCD_STRUCT = "<4sHHHHIIH"
_ZIP_EOCD_SIZE = struct.calcsize(_ZIP_EOCD_STRUCT)
_ZIP_EOCD_MAX_COMMENT_BYTES = 65535
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_LOCATOR_STRUCT = "<4sIQI"
_ZIP64_LOCATOR_SIZE = struct.calcsize(_ZIP64_LOCATOR_STRUCT)
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_EOCD_STRUCT = "<4sQHHIIQQQQ"
_ZIP64_EOCD_FIXED_SIZE = struct.calcsize(_ZIP64_EOCD_STRUCT)
# A central directory file header is 46 fixed bytes before its variable-length
# name/extra/comment fields -- a declared entry count that couldn't possibly
# fit in the declared central directory size is a forged or corrupt archive.
_ZIP_CENTRAL_DIRECTORY_HEADER_MIN_BYTES = 46


def _find_end_of_central_directory(handle, file_size):
    """Locate and parse the End Of Central Directory record by hand, without
    letting zipfile parse the (possibly forged) central directory it points
    to first.

    A crafted comment can embed a decoy EOCD signature ahead of the real one.
    The only reliable way to tell a genuine EOCD from a decoy is that a
    genuine record's declared comment length reaches exactly to the end of
    the file, so every signature match is checked against that before being
    accepted, searching backward from the end the way the format requires.
    """
    tail_size = min(file_size, _ZIP_EOCD_SIZE + _ZIP_EOCD_MAX_COMMENT_BYTES)
    handle.seek(file_size - tail_size)
    tail = handle.read(tail_size)
    base_offset = file_size - tail_size
    search_end = len(tail)
    while True:
        idx = tail.rfind(_ZIP_EOCD_SIGNATURE, 0, search_end)
        if idx == -1:
            raise ValueError("archive has no valid end-of-central-directory record")
        if idx + _ZIP_EOCD_SIZE <= len(tail):
            fields = struct.unpack(_ZIP_EOCD_STRUCT, tail[idx : idx + _ZIP_EOCD_SIZE])
            comment_len = fields[7]
            if base_offset + idx + _ZIP_EOCD_SIZE + comment_len == file_size:
                return base_offset + idx, fields
        search_end = idx


def _admit_zip_central_directory(archive_path):
    """Validate the archive's End Of Central Directory (and ZIP64 EOCD, if
    present) on cheap, bounded reads before zipfile.ZipFile() is ever asked
    to parse the central directory itself -- so a forged EOCD, a ZIP/ZIP64
    entry-count mismatch, or an absurd declared member count is rejected
    before any work proportional to that count happens.

    Returns the admitted (trustworthy) member count.
    """
    archive_path = Path(archive_path)
    file_size = archive_path.stat().st_size
    if file_size < _ZIP_EOCD_SIZE:
        raise ValueError("archive is too small to be a valid ZIP file")
    with archive_path.open("rb") as handle:
        eocd_offset, fields = _find_end_of_central_directory(handle, file_size)
        _sig, disk_no, disk_with_cd, entries_on_disk, total_entries, cd_size, cd_offset, _comment_len = fields
        cd_bound = eocd_offset
        declares_zip64_sentinel = total_entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF
        # A writer may emit a ZIP64 locator/EOCD even when the classic EOCD's
        # own fields are small enough not to need it (zipfile does, when
        # ZIP64_LIMIT is lowered) -- so presence of a valid locator, not just
        # the classic EOCD's sentinel values, is what makes the ZIP64 record
        # authoritative. Whichever record is authoritative is the one whose
        # entry counts must agree; deferring to sentinel values alone would
        # let a real ZIP64 record's (mismatched) counts go unchecked.
        locator_offset = eocd_offset - _ZIP64_LOCATOR_SIZE
        has_zip64_locator = False
        locator = b""
        if locator_offset >= 0:
            handle.seek(locator_offset)
            locator = handle.read(_ZIP64_LOCATOR_SIZE)
            has_zip64_locator = len(locator) == _ZIP64_LOCATOR_SIZE and locator[:4] == _ZIP64_LOCATOR_SIGNATURE
        if declares_zip64_sentinel and not has_zip64_locator:
            raise ValueError("archive declares ZIP64 but has no ZIP64 end-of-central-directory locator")
        if has_zip64_locator:
            _lsig, _ldisk, zip64_eocd_offset, _ldisks = struct.unpack(_ZIP64_LOCATOR_STRUCT, locator)
            if zip64_eocd_offset < 0 or zip64_eocd_offset + _ZIP64_EOCD_FIXED_SIZE > locator_offset:
                raise ValueError("archive's ZIP64 end-of-central-directory record is out of bounds")
            handle.seek(zip64_eocd_offset)
            zip64_record = handle.read(_ZIP64_EOCD_FIXED_SIZE)
            if len(zip64_record) != _ZIP64_EOCD_FIXED_SIZE or zip64_record[:4] != _ZIP64_EOCD_SIGNATURE:
                raise ValueError("archive's ZIP64 end-of-central-directory record is missing or has an invalid signature")
            zip64_fields = struct.unpack(_ZIP64_EOCD_STRUCT, zip64_record)
            disk_no, disk_with_cd, entries_on_disk, total_entries, cd_size, cd_offset = zip64_fields[4:10]
            cd_bound = zip64_eocd_offset
        if disk_no != 0 or disk_with_cd != 0:
            raise ValueError("multi-disk archives are not supported")
        if entries_on_disk != total_entries:
            raise ValueError(
                "archive's central directory entry counts do not agree "
                "(possible forged or spanned archive)"
            )
        if total_entries > _ARCHIVE_MAX_MEMBERS:
            raise ValueError(f"archive declares {total_entries} members, over the {_ARCHIVE_MAX_MEMBERS}-member limit")
        if cd_size < 0 or cd_offset < 0 or cd_offset + cd_size > cd_bound:
            raise ValueError("archive's central directory extends past its own end marker (possible forged EOCD)")
        if total_entries > 0 and cd_size < total_entries * _ZIP_CENTRAL_DIRECTORY_HEADER_MIN_BYTES:
            raise ValueError("archive's central directory is too small for its declared member count")
    return int(total_entries)


def _safe_zip_members(zf, *, expected_count=None):
    infolist = zf.infolist()
    if expected_count is not None and len(infolist) != expected_count:
        raise ValueError(
            f"archive's parsed member count ({len(infolist)}) does not match its declared "
            f"end-of-central-directory count ({expected_count}) -- possible forged EOCD"
        )
    if len(infolist) > _ARCHIVE_MAX_MEMBERS:
        raise ValueError(f"archive has {len(infolist)} members, over the {_ARCHIVE_MAX_MEMBERS}-member limit")
    seen_names = set()
    members = []
    for info in infolist:
        name = info.filename.replace("\\", "/")
        if not name or "\x00" in name:
            raise ValueError(f"unsafe archive member path: {info.filename!r}")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"unsafe archive member path: {info.filename}")
        if name in seen_names:
            # zipfile.ZipFile resolves getinfo()/open() by name against a
            # dict keyed on filename, last-definition-wins -- a duplicate
            # name lets a small decoy entry sit next to the real one that a
            # human skimming an archive listing would see, while the entry
            # our code actually reads is whichever one zipfile kept.
            raise ValueError(f"duplicate archive member: {name}")
        seen_names.add(name)
        if info.is_dir():
            raise ValueError(f"archive member is a directory, not a file: {name}")
        if info.flag_bits & 0x1:
            raise ValueError(f"archive member {name!r} is encrypted")
        if info.compress_type not in _ARCHIVE_SUPPORTED_COMPRESS_TYPES:
            raise ValueError(f"archive member {name!r} uses an unsupported compression method")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and S_ISLNK(unix_mode):
            raise ValueError(f"archive member {name!r} is a symlink, not a regular file")
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


# A crafted archive can claim any uncompressed size it likes in its central
# directory -- that field is just metadata, not something zipfile enforces
# during extraction -- so it isn't a trustworthy bound. What *is* trustworthy
# is how many compressed bytes the member actually occupies in the archive
# (that's real data read off disk), so decompressed output is capped as a
# multiple of that instead. Real InkDrop state-DB backups land well under
# this even accounting for a heavily-freelisted database (see the DB size
# audit: ~45GB DB, ~27% freelist -- mostly-zeroed freelist pages compress
# hard, but the ratio for the whole file still stays a small multiple, not
# hundreds of times over). A DEFLATE bomb crafted to lie about its size
# trips the floor/ratio check long before it can exhaust disk.
_MEMBER_DECOMPRESSION_RATIO_LIMIT = 300
_MEMBER_DECOMPRESSION_FLOOR_BYTES = 4 * 1024 ** 3

# manifest.json, the config export, and the secret-ref manifest are small,
# human-authored JSON documents, not database copies -- a much smaller floor
# and an absolute ceiling both apply, so a tiny compressed member can't claim
# gigabytes of legitimate-looking "ratio budget" the way a real database
# member's floor allows.
_JSON_MEMBER_RATIO_LIMIT = 300
_JSON_MEMBER_RATIO_FLOOR_BYTES = 64 * 1024
_JSON_MEMBER_HARD_MAX_BYTES = 8 * 1024 * 1024

# Across every member this code will actually stage or read, the total
# admitted expansion cannot exceed this regardless of how many members pass
# their individual per-member budgets -- otherwise several members each just
# under their own cap could still sum to an unbounded write.
_ARCHIVE_MAX_AGGREGATE_EXPANDED_BYTES = int(
    os.environ.get("INKDROP_BACKUP_MAX_AGGREGATE_EXPANDED_BYTES") or 64 * 1024 ** 3
)

# Needs real headroom above how long a legitimate large database actually
# takes to stage and pass its SQLite integrity check (pragma quick_check +
# foreign_key_check aren't free on a heavily-freelisted multi-gigabyte file):
# measured at ~757s end-to-end for this project's own ~45.7GB production
# state DB. 600s -- the previous default -- cut that off mid-validation on
# every restore of a real backup this size; a crafted archive designed to
# pass every byte-based check while still taking forever to stream still
# gets cut off eventually instead of tying up the worker indefinitely.
_VALIDATION_DEADLINE_SECONDS = int(os.environ.get("INKDROP_BACKUP_VALIDATION_DEADLINE_SECONDS") or 3600)


def _validation_deadline():
    return time.monotonic() + _VALIDATION_DEADLINE_SECONDS


def _check_validation_deadline(deadline, *, stage):
    if time.monotonic() > deadline:
        raise ValueError(f"backup archive validation exceeded its time limit during {stage}")


def _decompression_budget(info, *, floor_bytes, ratio_limit, hard_max_bytes=None):
    """The most decompressed bytes a member is allowed to produce.

    Bounded by a multiple of the member's actual compressed size (the one
    thing a malicious archive can't lie about, since it's real bytes that had
    to be transferred), floored so tiny legitimate members aren't penalized,
    and optionally hard-capped regardless of compressed size for members that
    should never be large no matter what.
    """
    budget = max(int(floor_bytes), info.compress_size * int(ratio_limit))
    if hard_max_bytes is not None:
        budget = min(budget, int(hard_max_bytes))
    return budget


class _AggregateExpansionTracker:
    """Bounds the real decompressed bytes produced across every member read
    or staged within one validation/staging pass.

    Summing each member's worst-case per-member budget (compressed_size x
    300) before any bytes are actually read overstates real archives by two
    orders of magnitude -- a legitimate multi-gigabyte database compresses at
    a small single-digit-to-tens multiple, not 300x, so that upfront sum
    rejected real backups that were nowhere near the aggregate limit. Tracking
    actual streamed bytes here instead still catches the thing the aggregate
    cap exists for (several members each just under their own cap summing to
    an unbounded write), and it does so using real data, not a hypothetical.
    """

    def __init__(self, limit_bytes):
        self.limit_bytes = int(limit_bytes)
        self.consumed = 0

    def consume(self, num_bytes):
        self.consumed += num_bytes
        if self.consumed > self.limit_bytes:
            raise ValueError(
                f"backup archive's members decompressed past {self.consumed} bytes so far, "
                f"over the {self.limit_bytes}-byte aggregate limit"
            )


def _require_free_space(directory, needed_bytes, *, label):
    directory = Path(directory)
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return
    if free < needed_bytes * 1.05:
        raise ValueError(
            f"not enough free space to safely validate/restore {label}: "
            f"need ~{needed_bytes} bytes, {free} available in {directory}"
        )


def _stage_archive_member(zf, archive_name, target_dir, *, floor_bytes=None, ratio_limit=None, hard_max_bytes=None, aggregate_tracker=None):
    """Copy a member to a durable sibling temp file for atomic replacement.

    Bounds decompressed output regardless of what the member's declared
    uncompressed size claims, so a corrupt or malicious archive can't turn a
    small upload into an unbounded write during preview or apply.

    floor_bytes/ratio_limit default to the module-level DB-oriented globals,
    resolved at call time rather than bound as default-argument values, so
    tests (and any future caller) that patch those globals see the patched
    value even when they don't pass floor_bytes/ratio_limit explicitly.

    aggregate_tracker, when given, is charged for real bytes written here so
    several members that each stay under their own cap still can't sum to an
    unbounded write across one validation/staging pass.
    """
    info = zf.getinfo(archive_name)
    resolved_floor = _MEMBER_DECOMPRESSION_FLOOR_BYTES if floor_bytes is None else floor_bytes
    resolved_ratio = _MEMBER_DECOMPRESSION_RATIO_LIMIT if ratio_limit is None else ratio_limit
    max_bytes = _decompression_budget(info, floor_bytes=resolved_floor, ratio_limit=resolved_ratio, hard_max_bytes=hard_max_bytes)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(prefix=".inkdrop-restore-", dir=target_dir)
    staged_path = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as dst, zf.open(archive_name) as src:
            fd = -1
            written = 0
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(
                        f"archive member {archive_name!r} decompressed past "
                        f"{max_bytes} bytes ({info.compress_size} bytes compressed) "
                        "-- refusing to extract further"
                    )
                if aggregate_tracker is not None:
                    aggregate_tracker.consume(len(chunk))
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        return staged_path
    except Exception:
        staged_path.unlink(missing_ok=True)
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def _read_bounded_zip_member(zf, archive_name, *, floor_bytes, ratio_limit, hard_max_bytes, label=None, aggregate_tracker=None):
    """Read a member fully into memory, but only up to its decompression
    budget -- for the small JSON documents this backs, that budget is itself
    small (see _JSON_MEMBER_HARD_MAX_BYTES), so this never buffers more than
    a few megabytes regardless of what the archive claims.

    aggregate_tracker, when given, is charged for real bytes read here --
    see _AggregateExpansionTracker."""
    label = label or archive_name
    info = zf.getinfo(archive_name)
    max_bytes = _decompression_budget(info, floor_bytes=floor_bytes, ratio_limit=ratio_limit, hard_max_bytes=hard_max_bytes)
    chunks = []
    total = 0
    with zf.open(archive_name) as src:
        while True:
            chunk = src.read(min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(
                    f"{label} decompressed past {max_bytes} bytes "
                    f"({info.compress_size} bytes compressed) -- refusing to read further"
                )
            if aggregate_tracker is not None:
                aggregate_tracker.consume(len(chunk))
            chunks.append(chunk)
    return b"".join(chunks)


def _existing_file_size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


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
    apply=False,
    backup_dir=None,
    preserve_current_auth=False,
):
    archive_path = Path(archive_path)
    target_config_dir = Path(target_config_dir or inkdrop_runtime_config.config_dir())
    target_state_dir = Path(target_state_dir or inkdrop_runtime_config.state_dir())
    backup_dir = Path(backup_dir or inkdrop_runtime_config.backup_dir())
    deadline = _validation_deadline()
    # The central directory is admitted -- entry count, EOCD/ZIP64 agreement,
    # and directory bounds all checked -- on bounded reads before zipfile is
    # ever asked to parse it, so a forged EOCD or an absurd declared member
    # count is rejected before any work proportional to that count happens.
    expected_member_count = _admit_zip_central_directory(archive_path)
    with zipfile.ZipFile(archive_path, "r") as zf:
        _safe_zip_members(zf, expected_count=expected_member_count)
        _check_validation_deadline(deadline, stage="central directory admission")
        archive_names = set(zf.namelist())
        if STATE_DB_ARCHIVE_NAME not in archive_names:
            raise ValueError("backup archive does not contain the required state database")
        for required_name in (MANIFEST_ARCHIVE_NAME, CONFIG_EXPORT_ARCHIVE_NAME, SECRET_REFS_ARCHIVE_NAME):
            if required_name not in archive_names:
                raise ValueError(f"backup archive is missing required member: {required_name}")

        # Free-space preflight checks below need an estimate of how big each
        # member will actually land on disk. The central directory's declared
        # uncompressed size isn't a trustworthy *security* bound (zipfile
        # doesn't enforce it, so a malicious archive could lie) -- that's why
        # actual decompression is separately bounded by the real per-member
        # compressed-size*ratio cap during streaming, and by the aggregate
        # tracker across a pass, regardless of what's declared here. But as a
        # disk-space *estimate* for a real archive, the declared size is
        # accurate, whereas the worst-case ratio budget this used to use
        # (compressed_size * 300) demanded hundreds of gigabytes of free space
        # to preview a legitimate multi-gigabyte backup that only needed as
        # much free space as its real size.
        member_real_sizes = {
            name: zf.getinfo(name).file_size
            for name in (STATE_DB_ARCHIVE_NAME, AUTH_DB_ARCHIVE_NAME, COMPLETION_DB_ARCHIVE_NAME, CONFIG_EXPORT_ARCHIVE_NAME, SECRET_REFS_ARCHIVE_NAME)
            if name in archive_names
        }

        # Shared across the manifest/config reads and the preview staging
        # below -- that's the one pass this function always performs (dry
        # run or apply alike) -- so members that each pass their own cap
        # still can't sum to unbounded real output in that pass.
        validation_tracker = _AggregateExpansionTracker(_ARCHIVE_MAX_AGGREGATE_EXPANDED_BYTES)

        manifest_raw = _read_bounded_zip_member(
            zf,
            MANIFEST_ARCHIVE_NAME,
            floor_bytes=_JSON_MEMBER_RATIO_FLOOR_BYTES,
            ratio_limit=_JSON_MEMBER_RATIO_LIMIT,
            hard_max_bytes=_JSON_MEMBER_HARD_MAX_BYTES,
            label="backup manifest",
            aggregate_tracker=validation_tracker,
        )
        manifest = parse_strict_json_object(manifest_raw, max_bytes=_JSON_MEMBER_HARD_MAX_BYTES, label="backup manifest")
        # Refuse an archive written by a newer InkDrop than this one. The
        # portable-settings path has always checked its own document version
        # (see _portable_settings_plan), but this path read the manifest's
        # schema_version and never compared it -- so an archive from a future
        # format was accepted and restored on the assumption that every member
        # meant what today's code thinks it means.
        #
        # Only the forward direction is refused. An *older* archive is fine and
        # is deliberately still accepted: init_schema() migrates a restored
        # database forward on first open, which is verified by the
        # old-application-schema case in the workspace-and-version-gate smoke.
        # Deliberately narrow: only a version this code can read *and* compare,
        # and which is genuinely higher, is refused. A missing or unparseable
        # schema_version is not evidence of a future format -- a future format
        # would carry a higher number, not a broken one -- so it falls through
        # to the structural and SQLite integrity checks that actually defend
        # this path. Rejecting it here instead pre-empted the decompression-bomb
        # refusals with a misleading message, which is how
        # inkdrop-backup-restore-decompression-bomb-smoke and
        # inkdrop-backup-archive-resource-exhaustion-smoke caught the first cut.
        manifest_schema_version = manifest.get("schema_version")
        if type(manifest_schema_version) is int and manifest_schema_version > BACKUP_RESTORE_SCHEMA_VERSION:
            raise ValueError(
                f"backup archive was written by a newer InkDrop (backup format "
                f"v{manifest_schema_version}; this build understands up to "
                f"v{BACKUP_RESTORE_SCHEMA_VERSION}). Upgrade before restoring it."
            )
        config_export_raw = _read_bounded_zip_member(
            zf,
            CONFIG_EXPORT_ARCHIVE_NAME,
            floor_bytes=_JSON_MEMBER_RATIO_FLOOR_BYTES,
            ratio_limit=_JSON_MEMBER_RATIO_LIMIT,
            hard_max_bytes=_JSON_MEMBER_HARD_MAX_BYTES,
            label="backup config export",
            aggregate_tracker=validation_tracker,
        )
        config_export = parse_strict_json_object(config_export_raw, max_bytes=_JSON_MEMBER_HARD_MAX_BYTES, label="backup config export")
        _check_validation_deadline(deadline, stage="manifest and config export read")
        source_values = config_export.get("values") if isinstance(config_export.get("values"), dict) else {}
        path_warnings = []
        for key in PATH_ENV_KEYS:
            value = str(source_values.get(key) or "").strip()
            if not value or value in {"<set>", "<unset>"}:
                continue
            if not Path(value).exists():
                path_warnings.append(
                    {
                        "key": key,
                        "source_path": value,
                        "reason": "path_missing_on_restore_host",
                        "next_action": "Update this setting after restore.",
                    }
                )
        # A dry run must prove the databases are usable, not merely that the
        # ZIP member names exist.  Extraction is isolated from restore targets.
        preview_budget = (
            member_real_sizes.get(STATE_DB_ARCHIVE_NAME, 0)
            + member_real_sizes.get(AUTH_DB_ARCHIVE_NAME, 0)
            + member_real_sizes.get(COMPLETION_DB_ARCHIVE_NAME, 0)
        )
        with tempfile.TemporaryDirectory(prefix="inkdrop-restore-preview-") as preview_dir:
            preview_root = Path(preview_dir)
            _require_free_space(preview_root, preview_budget, label="backup preview staging")
            preview_state = _stage_archive_member(zf, STATE_DB_ARCHIVE_NAME, preview_root, aggregate_tracker=validation_tracker)
            database_validation = {
                "state_db": _validate_sqlite_database(preview_state, label="backup state database")
            }
            _check_validation_deadline(deadline, stage="state database preview staging")
            if AUTH_DB_ARCHIVE_NAME in archive_names:
                preview_auth = _stage_archive_member(zf, AUTH_DB_ARCHIVE_NAME, preview_root, aggregate_tracker=validation_tracker)
                database_validation["auth_db"] = _validate_sqlite_database(
                    preview_auth, label="backup auth database"
                )
                _check_validation_deadline(deadline, stage="auth database preview staging")
            if COMPLETION_DB_ARCHIVE_NAME in archive_names:
                preview_completion = _stage_archive_member(
                    zf, COMPLETION_DB_ARCHIVE_NAME, preview_root, aggregate_tracker=validation_tracker
                )
                database_validation["completion_db"] = _validate_sqlite_database(
                    preview_completion, label="backup completion database"
                )
                _check_validation_deadline(deadline, stage="completion database preview staging")
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
                "completion_db": COMPLETION_DB_ARCHIVE_NAME in archive_names,
                "config_export": CONFIG_EXPORT_ARCHIVE_NAME in archive_names,
                "secret_refs": SECRET_REFS_ARCHIVE_NAME in archive_names,
            },
        }
        if not apply:
            return result
        target_config_dir.mkdir(parents=True, exist_ok=True)
        target_state_dir.mkdir(parents=True, exist_ok=True)
        # An apply also has to make room for _snapshot_existing_file() copying
        # whatever is already at each target aside into backup_dir before it
        # gets overwritten -- free space needs to cover both the incoming
        # (validated, budget-bounded) members and today's outgoing files.
        existing_state_bytes = _existing_file_size(target_state_dir / inkdrop_runtime_config.STATE_DB_NAME)
        existing_auth_bytes = _existing_file_size(target_state_dir / inkdrop_auth.AUTH_STORE_DB_NAME)
        existing_config_bytes = sum(
            _existing_file_size(target_config_dir / name)
            for name in ("inkdrop-config-export.json", "inkdrop-secret-refs.json")
        )
        _require_free_space(
            target_state_dir,
            member_real_sizes.get(STATE_DB_ARCHIVE_NAME, 0)
            + member_real_sizes.get(AUTH_DB_ARCHIVE_NAME, 0)
            + member_real_sizes.get(COMPLETION_DB_ARCHIVE_NAME, 0),
            label="restored state/auth databases",
        )
        _require_free_space(
            target_config_dir,
            member_real_sizes.get(CONFIG_EXPORT_ARCHIVE_NAME, 0) + member_real_sizes.get(SECRET_REFS_ARCHIVE_NAME, 0),
            label="restored config files",
        )
        _require_free_space(
            backup_dir,
            existing_state_bytes + existing_auth_bytes + existing_config_bytes,
            label="pre-restore snapshots",
        )
        staged_files = {}
        # A fresh tracker, not validation_tracker above -- this is a separate
        # staging pass (into the real restore targets, after the preview pass
        # already proved the same members decompress cleanly), so it must not
        # be charged twice for the same bytes.
        apply_tracker = _AggregateExpansionTracker(_ARCHIVE_MAX_AGGREGATE_EXPANDED_BYTES)
        try:
            staged_files[STATE_DB_ARCHIVE_NAME] = _stage_archive_member(
                zf, STATE_DB_ARCHIVE_NAME, target_state_dir, aggregate_tracker=apply_tracker
            )
            _validate_sqlite_database(
                staged_files[STATE_DB_ARCHIVE_NAME], label="backup state database"
            )
            _check_validation_deadline(deadline, stage="state database staging")
            if AUTH_DB_ARCHIVE_NAME in archive_names:
                staged_files[AUTH_DB_ARCHIVE_NAME] = _stage_archive_member(
                    zf, AUTH_DB_ARCHIVE_NAME, target_state_dir, aggregate_tracker=apply_tracker
                )
                _validate_sqlite_database(
                    staged_files[AUTH_DB_ARCHIVE_NAME], label="backup auth database"
                )
                _check_validation_deadline(deadline, stage="auth database staging")
            if COMPLETION_DB_ARCHIVE_NAME in archive_names:
                staged_files[COMPLETION_DB_ARCHIVE_NAME] = _stage_archive_member(
                    zf, COMPLETION_DB_ARCHIVE_NAME, target_state_dir, aggregate_tracker=apply_tracker
                )
                _validate_sqlite_database(
                    staged_files[COMPLETION_DB_ARCHIVE_NAME], label="backup completion database"
                )
                _check_validation_deadline(deadline, stage="completion database staging")
            for archive_name in (CONFIG_EXPORT_ARCHIVE_NAME, SECRET_REFS_ARCHIVE_NAME):
                staged_files[archive_name] = _stage_archive_member(
                    zf, archive_name, target_config_dir,
                    floor_bytes=_JSON_MEMBER_RATIO_FLOOR_BYTES,
                    ratio_limit=_JSON_MEMBER_RATIO_LIMIT,
                    hard_max_bytes=_JSON_MEMBER_HARD_MAX_BYTES,
                    aggregate_tracker=apply_tracker,
                )
            _check_validation_deadline(deadline, stage="config file staging")
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
            # The live state DB runs in WAL mode (see backup_sqlite_db above),
            # so a -wal/-shm pair from whatever was at this path before almost
            # always still exists here. Left in place, the next open replays
            # those stale frames onto the just-restored file -- silently
            # reinstating the pre-restore data (including, in the worst case,
            # whatever was corrupt enough to need a restore in the first
            # place) while this function still reports the restore as ok.
            # The auth_target branch below has always done this; this branch
            # never did.
            for suffix in ("-wal", "-shm"):
                Path(str(state_target) + suffix).unlink(missing_ok=True)
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
            if COMPLETION_DB_ARCHIVE_NAME in staged_files:
                completion_target = target_state_dir / inkdrop_runtime_config.IMPORTED_FILES_DB_NAME
                snapshot = _snapshot_existing_file(completion_target, backup_dir)
                if snapshot:
                    pre_restore_snapshots.append(path_text(snapshot))
                os.replace(staged_files.pop(COMPLETION_DB_ARCHIVE_NAME), completion_target)
                # Same stale-WAL hazard as the state database above: whatever
                # -wal/-shm pair belonged to the file that used to be at this
                # path would otherwise be replayed onto the restored one.
                for suffix in ("-wal", "-shm"):
                    Path(str(completion_target) + suffix).unlink(missing_ok=True)
                result["restored_completion_db"] = path_text(completion_target)
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


BACKUP_MERGE_INCLUDED_TABLES = ("series", "issues")
BACKUP_MERGE_EXCLUDED_TABLES = (
    "wanted_items", "history_events", "queue_items", "source_attempts",
    "download_tasks", "import_results", "media_files", "provider_configs",
    "app_settings", "auth_users", "auth_sessions", "api_keys",
)
BACKUP_MERGE_SCOPE_NOTE = (
    "This preview covers series and issues only. Wanted items, history, queue/download "
    "state, media files, credentials, and settings are never read or imported by this "
    "feature."
)
BACKUP_MERGE_IDENTITY_RULE_NOTE = (
    "A backup row is only ever counted as already present when it has an exact, "
    "non-empty (metadata provider, metadata id) match to an existing row. Everything "
    "else -- no external id on either side, a mismatched id, or an id that matches more "
    "than one existing row -- is counted as new or flagged for review, never guessed at. "
    "No existing row's fields are ever changed by this preview or by any future import."
)
# How many example rows each new/matched/needs_review bucket carries in the response --
# real libraries can have tens of thousands of issues, so the classification counts every
# row but only a bounded sample is ever serialized.
_MERGE_PREVIEW_SAMPLE_LIMIT = int(os.environ.get("INKDROP_BACKUP_MERGE_PREVIEW_SAMPLE_LIMIT") or 50)


def _merge_identity_key(provider, external_id):
    """The only identity signal this preview trusts: a real, non-empty external
    metadata id from a real provider. Title/year/path similarity is deliberately not
    used -- this repo already shipped a dedicated tool (apply_series_duplicate_merge)
    to clean up 107 series that only collided on title, and today's
    repair_media_file_identity() near-miss (SIXH-20260812-IDENTITY-P1-01) showed that
    even an id-occupancy heuristic that looks individually sound can silently merge
    unrelated rows. Matching on anything less than an exact external id is not
    unambiguous enough to resolve automatically.
    """
    provider = str(provider or "").strip()
    external_id = str(external_id or "").strip()
    if not provider or not external_id:
        return None
    return (provider, external_id)


class _MergeBucket:
    """Accumulates a bounded sample of classified rows plus an exact total
    count, without ever holding more than sample_limit row dicts at once --
    the classification loops below run over every row in a backup that can
    have hundreds of thousands of them, and only sample_limit (default 50)
    are ever serialized in the response."""

    def __init__(self, sample_limit):
        self._sample_limit = sample_limit
        self._sample = []
        self.count = 0

    def add(self, row):
        self.count += 1
        if len(self._sample) < self._sample_limit:
            self._sample.append(row)

    def summary(self):
        return {
            "count": self.count,
            "sample": self._sample,
            "sample_truncated": self.count > len(self._sample),
        }


# Bounds how many live rows a single identity lookup pulls back. Only ever
# used to tell "no match" / "exactly one match" / "more than one match"
# apart and, for the last case, to show a few candidates -- an exact count
# of matches beyond "more than one" is never displayed, so there is no
# reason to fetch more than this.
_MERGE_PREVIEW_IDENTITY_LOOKUP_LIMIT = 5
# How many backup series rows are batched into a single round of live-side
# lookups. Bounds the chunk-local index built below to this many distinct
# keys at a time, regardless of how large the live or backup series tables
# are -- see _classify_backup_series.
_MERGE_PREVIEW_SERIES_CHUNK_SIZE = 500
# Same bound for backup issue rows -- see _classify_backup_issues. Kept
# comfortably under SQLite's default 999-variable statement limit, since a
# chunk's keys become `metadata_id in (...)` placeholders.
_MERGE_PREVIEW_ISSUE_CHUNK_SIZE = 500


def _live_series_identity_chunk_lookup(con, keys):
    """Batched identity lookup for a bounded chunk of (provider, external_id)
    pairs against the live series table, backed by idx_series_metadata_identity.
    Grouped by provider and issued as one indexed `metadata_id in (...)` query per
    provider (in practice almost always a single query, since a library only ever
    uses a handful of metadata providers) -- this is what replaces the earlier
    approach of pre-building one dict for the *entire* live series table, which is
    what kept this preview's memory scaling with library size even after the
    streamed-cursor fix in PR #593 (measured 15.92MB at 90K issues vs 3.60MB at
    9K). Returns {(provider, external_id): [live series id, ...]}, discarded by
    the caller once the chunk it was built for has been classified.
    """
    by_provider = {}
    for provider, external_id in keys:
        by_provider.setdefault(provider, []).append(external_id)
    index = {}
    for provider, external_ids in by_provider.items():
        placeholders = ",".join("?" for _ in external_ids)
        for row in con.execute(
            f"select metadata_id, id from main.series where metadata_provider=? and metadata_id in ({placeholders})",
            (provider, *external_ids),
        ):
            bucket = index.setdefault((provider, row["metadata_id"]), [])
            if len(bucket) < _MERGE_PREVIEW_IDENTITY_LOOKUP_LIMIT:
                bucket.append(row["id"])
    return index


def _classify_backup_series(con, *, deadline, sample_limit):
    backup_tables = {row[0] for row in con.execute("select name from backup.sqlite_master where type='table'")}
    if "series" not in backup_tables:
        raise ValueError("backup state database does not contain a series table")
    new_bucket = _MergeBucket(sample_limit)
    matched_bucket = _MergeBucket(sample_limit)
    needs_review_bucket = _MergeBucket(sample_limit)
    matched_target_by_backup_id = {}
    needs_review_backup_ids = set()
    total_in_backup = 0

    def classify_chunk(chunk):
        index = _live_series_identity_chunk_lookup(con, {entry["key"] for entry in chunk if entry["key"] is not None})
        for entry in chunk:
            sample = entry["sample"]
            targets = index.get(entry["key"]) if entry["key"] is not None else None
            if not targets:
                new_bucket.add(sample)
            elif len(targets) == 1:
                matched_target_by_backup_id[entry["backup_id"]] = targets[0]
                matched_bucket.add({**sample, "matched_target_id": targets[0]})
            else:
                needs_review_backup_ids.add(entry["backup_id"])
                needs_review_bucket.add({
                    **sample,
                    "reason": "identity_matches_multiple_existing_rows",
                    "candidate_target_ids": targets[:5],
                })

    # Streamed via the cursor rather than .fetchall(): the backup series
    # table can run to hundreds of thousands of rows, and a fetchall() here
    # would materialize the entire table as Python Row objects before a
    # single one gets classified. Rows are matched against the live side in
    # bounded chunks rather than via one pre-built in-memory index of the
    # whole live table, so memory here stays flat regardless of how large
    # either side's series table is.
    chunk = []
    for index, row in enumerate(con.execute(
        "select id, title, year, media_type, metadata_provider, metadata_id from backup.series order by title"
    )):
        total_in_backup += 1
        if index % 500 == 0:
            _check_validation_deadline(deadline, stage="series classification")
        sample = {
            "backup_id": row["id"],
            "title": row["title"],
            "year": row["year"],
            "media_type": row["media_type"],
            "metadata_provider": row["metadata_provider"],
            "metadata_id": row["metadata_id"],
        }
        key = _merge_identity_key(row["metadata_provider"], row["metadata_id"])
        chunk.append({"backup_id": row["id"], "sample": sample, "key": key})
        if len(chunk) >= _MERGE_PREVIEW_SERIES_CHUNK_SIZE:
            classify_chunk(chunk)
            chunk = []
    if chunk:
        classify_chunk(chunk)
    return {
        "total_in_backup": total_in_backup,
        "new": new_bucket.summary(),
        "matched": matched_bucket.summary(),
        "needs_review": needs_review_bucket.summary(),
    }, matched_target_by_backup_id, needs_review_backup_ids


def _live_issue_identity_chunk_lookup(con, keys):
    """Batched identity lookup for a bounded chunk of
    (live series id, provider, external_id) triples against the live issues
    table, backed by idx_issues_metadata_identity. Grouped by (series, provider)
    and issued as one indexed `metadata_id in (...)` query per group.

    This replaces an earlier per-target-series index that materialized *every*
    live issue of one series into a Python dict. That was flat enough for a
    catalog whose issues are spread evenly across many series, but not for a
    concentration case: a single series holding the bulk of a library measured
    3.61MB peak at 10K issues and 34.66MB at 100K -- ~9.6x for a 10x catalog,
    i.e. still linear in issue count, just relocated from the whole table to
    one series. Chunking on the *backup* side instead bounds the live-side
    working set to one chunk's keys no matter how the issues are distributed.

    Returns {(series_id, provider, external_id): [live issue id, ...]},
    discarded by the caller once the chunk it was built for is classified.
    """
    by_scope = {}
    for series_id, provider, external_id in keys:
        by_scope.setdefault((series_id, provider), []).append(external_id)
    index = {}
    for (series_id, provider), external_ids in by_scope.items():
        placeholders = ",".join("?" for _ in external_ids)
        for row in con.execute(
            f"select id, metadata_id from main.issues "
            f"where series_id=? and metadata_provider=? and metadata_id in ({placeholders})",
            (series_id, provider, *external_ids),
        ):
            bucket = index.setdefault((series_id, provider, row["metadata_id"]), [])
            if len(bucket) < _MERGE_PREVIEW_IDENTITY_LOOKUP_LIMIT:
                bucket.append(row["id"])
    return index


def _classify_backup_issues(con, *, matched_target_by_backup_id, needs_review_series_ids, deadline, sample_limit):
    backup_tables = {row[0] for row in con.execute("select name from backup.sqlite_master where type='table'")}
    if "issues" not in backup_tables:
        raise ValueError("backup state database does not contain an issues table")
    new_bucket = _MergeBucket(sample_limit)
    matched_bucket = _MergeBucket(sample_limit)
    needs_review_bucket = _MergeBucket(sample_limit)
    total_in_backup = 0

    def classify_chunk(chunk):
        # Every row in the chunk -- including the ones whose verdict was
        # already decided from the parent series alone -- is dispatched here,
        # in stream order, so each bucket's bounded sample stays in exactly
        # the order the rows were read.
        index = _live_issue_identity_chunk_lookup(
            con, {entry["key"] for entry in chunk if entry["key"] is not None}
        )
        for entry in chunk:
            sample = entry["sample"]
            disposition = entry["disposition"]
            if disposition == "parent_series_needs_review":
                needs_review_bucket.add({**sample, "reason": "parent_series_needs_review"})
                continue
            if disposition == "parent_series_new":
                # The parent series itself is new (or, defensively, unrecognized) -- there
                # is nothing on the live side to compare this issue against yet, so it can
                # only ever be new too.
                new_bucket.add({**sample, "reason": "parent_series_new"})
                continue
            targets = index.get(entry["key"]) if entry["key"] is not None else None
            if not targets:
                new_bucket.add(sample)
            elif len(targets) == 1:
                matched_bucket.add({
                    **sample,
                    "matched_target_series_id": entry["target_series_id"],
                    "matched_target_issue_id": targets[0],
                })
            else:
                needs_review_bucket.add({
                    **sample,
                    "reason": "identity_matches_multiple_existing_rows",
                    "candidate_target_ids": targets[:5],
                })

    # Streamed via the cursor rather than .fetchall(): a backup's issues
    # table is the largest one this preview reads (a 200K-issue library was
    # measured at ~136MB just for the materialized row list, scaling toward
    # ~1GB at a million rows) and every row is classified once, in order, so
    # there is never a reason to hold the whole result set in memory at once.
    # Live-side matching runs in fixed-size chunks against the live issues
    # table (see _live_issue_identity_chunk_lookup) rather than through an
    # index of one target series' issues: that older shape stayed flat only
    # while issues were spread across many series, and grew ~9.6x for a 10x
    # catalog once one series held them all. `order by series_id` is retained
    # so a chunk's keys cluster into few (series, provider) groups and each
    # lookup stays on idx_issues_metadata_identity, but correctness no longer
    # depends on that grouping.
    chunk = []
    for index, row in enumerate(con.execute(
        "select id, series_id, issue_number, title, release_date, metadata_provider, metadata_id "
        "from backup.issues order by series_id, issue_number"
    )):
        total_in_backup += 1
        if index % 500 == 0:
            _check_validation_deadline(deadline, stage="issue classification")
        sample = {
            "backup_id": row["id"],
            "backup_series_id": row["series_id"],
            "issue_number": row["issue_number"],
            "title": row["title"],
            "release_date": row["release_date"],
            "metadata_provider": row["metadata_provider"],
            "metadata_id": row["metadata_id"],
        }
        if row["series_id"] in needs_review_series_ids:
            chunk.append({"sample": sample, "disposition": "parent_series_needs_review", "key": None,
                          "target_series_id": None})
        else:
            target_series_id = matched_target_by_backup_id.get(row["series_id"])
            if target_series_id is None:
                chunk.append({"sample": sample, "disposition": "parent_series_new", "key": None,
                              "target_series_id": None})
            else:
                identity = _merge_identity_key(row["metadata_provider"], row["metadata_id"])
                key = None if identity is None else (target_series_id, identity[0], identity[1])
                chunk.append({"sample": sample, "disposition": "lookup", "key": key,
                              "target_series_id": target_series_id})
        if len(chunk) >= _MERGE_PREVIEW_ISSUE_CHUNK_SIZE:
            classify_chunk(chunk)
            chunk = []
    if chunk:
        classify_chunk(chunk)
    return {
        "total_in_backup": total_in_backup,
        "new": new_bucket.summary(),
        "matched": matched_bucket.summary(),
        "needs_review": needs_review_bucket.summary(),
    }


def preview_backup_merge(archive_path, *, state_db_path=None, backup_dir=None, sample_limit=None):
    """Read-only dry run for importing another instance's catalog (series + issues
    only -- see BACKUP_MERGE_SCOPE_NOTE) into the live library. Classifies every row
    in the uploaded backup as new / matched / needs_review against the live state
    database and returns counts plus a bounded sample of each -- it never opens the
    live database for anything but reading, and the apply/write path this preview is
    for does not exist yet.
    """
    archive_path = Path(archive_path)
    state_db_path = Path(state_db_path or inkdrop_runtime_config.state_db_path())
    backup_dir = Path(backup_dir or inkdrop_runtime_config.backup_dir())
    sample_limit = _MERGE_PREVIEW_SAMPLE_LIMIT if sample_limit is None else max(1, int(sample_limit))
    deadline = _validation_deadline()
    expected_member_count = _admit_zip_central_directory(archive_path)
    with zipfile.ZipFile(archive_path, "r") as zf:
        _safe_zip_members(zf, expected_count=expected_member_count)
        _check_validation_deadline(deadline, stage="central directory admission")
        archive_names = set(zf.namelist())
        if STATE_DB_ARCHIVE_NAME not in archive_names:
            raise ValueError("backup archive does not contain the required state database")
        if MANIFEST_ARCHIVE_NAME not in archive_names:
            raise ValueError("backup archive is missing required member: manifest.json")
        member_real_size = zf.getinfo(STATE_DB_ARCHIVE_NAME).file_size
        tracker = _AggregateExpansionTracker(_ARCHIVE_MAX_AGGREGATE_EXPANDED_BYTES)
        manifest_raw = _read_bounded_zip_member(
            zf, MANIFEST_ARCHIVE_NAME,
            floor_bytes=_JSON_MEMBER_RATIO_FLOOR_BYTES, ratio_limit=_JSON_MEMBER_RATIO_LIMIT,
            hard_max_bytes=_JSON_MEMBER_HARD_MAX_BYTES, label="backup manifest", aggregate_tracker=tracker,
        )
        manifest = parse_strict_json_object(manifest_raw, max_bytes=_JSON_MEMBER_HARD_MAX_BYTES, label="backup manifest")
        _check_validation_deadline(deadline, stage="manifest read")
        with tempfile.TemporaryDirectory(prefix="inkdrop-merge-preview-") as tmp:
            tmp_root = Path(tmp)
            _require_free_space(tmp_root, member_real_size, label="merge preview staging")
            staged_state = _stage_archive_member(zf, STATE_DB_ARCHIVE_NAME, tmp_root, aggregate_tracker=tracker)
            _check_validation_deadline(deadline, stage="state database staging")
            database_validation = {"state_db": _validate_sqlite_database(staged_state, label="backup state database")}
            _check_validation_deadline(deadline, stage="state database validation")
            staged_uri = f"file:{staged_state.resolve().as_posix()}?mode=ro"
            # Both sides are read-only: the live connection is opened via
            # inkdrop_state.connect_read (pragma query_only=1), and the uploaded
            # archive's state DB is attached by its own read-only URI -- nothing in
            # this function can write to either database.
            with inkdrop_state.connect_read(state_db_path) as con:
                con.execute("attach database ? as backup", (staged_uri,))
                try:
                    series_result, matched_target_by_backup_id, needs_review_series_ids = _classify_backup_series(
                        con, deadline=deadline, sample_limit=sample_limit
                    )
                    issues_result = _classify_backup_issues(
                        con,
                        matched_target_by_backup_id=matched_target_by_backup_id,
                        needs_review_series_ids=needs_review_series_ids,
                        deadline=deadline,
                        sample_limit=sample_limit,
                    )
                finally:
                    con.execute("detach database backup")
    return {
        "ok": True,
        "archive_path": path_text(archive_path),
        "manifest": manifest,
        "database_validation": database_validation,
        "series": series_result,
        "issues": issues_result,
        "scope": {
            "included": list(BACKUP_MERGE_INCLUDED_TABLES),
            "excluded": list(BACKUP_MERGE_EXCLUDED_TABLES),
            "note": BACKUP_MERGE_SCOPE_NOTE,
        },
        "identity_rule": BACKUP_MERGE_IDENTITY_RULE_NOTE,
        "write_capability": "preview_only",
    }


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
    restore.add_argument("--apply", action="store_true", help="Write restored state/config files.")
    restore.add_argument("--backup-dir", help="Where to snapshot existing state/config files before an --apply restore overwrites them.")
    restore.add_argument(
        "--preserve-current-auth",
        action="store_true",
        help="Keep today's logins and API keys instead of the archive's. Default restores auth from the same point in time as the rest of the archive.",
    )
    scheduled = sub.add_parser("scheduled-backup", help="Create a backup if the interval has elapsed, then prune by retention.")
    scheduled.add_argument("--config-dir")
    scheduled.add_argument("--state-db")
    scheduled.add_argument("--backup-dir")
    scheduled.add_argument("--interval-days", type=int, default=7)
    # Both retention limits default to unset so the run resolves them from
    # Settings first. Passing one here is the override an operator running the
    # command by hand gets; the scheduler deliberately passes neither.
    scheduled.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Delete scheduled archives older than this. Omit to use the configured Backups setting.",
    )
    scheduled.add_argument(
        "--retention-count",
        type=int,
        default=None,
        help="Also keep at most this many scheduled archives, newest first. Omit to use the configured Backups setting.",
    )
    args = parser.parse_args(argv)
    if args.command == "backup":
        result = create_backup_archive(
            config_dir=args.config_dir,
            state_db_path=args.state_db,
            backup_dir=args.backup_dir,
            label=args.label,
        )
    elif args.command == "scheduled-backup":
        result = run_scheduled_backup(
            config_dir=args.config_dir,
            state_db_path=args.state_db,
            backup_dir=args.backup_dir,
            interval_days=args.interval_days,
            retention_days=args.retention_days,
            retention_count=args.retention_count,
        )
    else:
        result = restore_backup_archive(
            args.archive,
            backup_dir=args.backup_dir,
            target_config_dir=args.target_config_dir,
            target_state_dir=args.target_state_dir,
            apply=args.apply,
            preserve_current_auth=bool(args.preserve_current_auth),
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
