"""Standalone backup inspection and redacted support-bundle primitives."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit


STATE_DB_MEMBER = "state/inkdrop-state.sqlite3"
AUTH_DB_MEMBER = "state/inkdrop-auth.sqlite3"
CONFIG_EXPORT_MEMBER = "config/inkdrop-config-export.json"
SECRET_REFS_MEMBER = "config/inkdrop-secret-refs.json"
MANIFEST_MEMBER = "manifest.json"
REDACTED = "[redacted]"
SECRET_KEY_RE = re.compile(
    r"(?i)(api.?key|username|password|passwd|passphrase|token|secret|credential|authorization|cookie|session|csrf|private.?key|client.?secret)"
)
CREDENTIAL_RE = re.compile(
    r"(?im)((?:api.?key|username|password|passwd|passphrase|access.?token|refresh.?token|token|secret|credential|"
    r"private.?key|client.?secret|authorization|cookie|set-cookie|session|csrf|x-api-key)\s*[:=]\s*)[^\r\n]+"
)
AUTH_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"']+")
URL_USERINFO_RE = re.compile(r"(?i)\b((?:https?|ftp)://)[^/@\s]+@")
MAX_QUERY_SPECS = 32
MAX_QUERY_CELL_BYTES = 64 * 1024
MAX_QUERY_RESULT_BYTES = 2 * 1024 * 1024
MAX_STRUCTURED_MEMBER_BYTES = 2 * 1024 * 1024
MAX_STRUCTURED_TOTAL_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 64
MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200
MAX_SQL_BYTES = 16 * 1024
MAX_QUERY_PARAMETERS = 64
MAX_QUERY_PARAMETER_BYTES = 64 * 1024
EXPECTED_AUTH_TABLES = (
    "schema_migrations",
    "auth_users",
    "auth_sessions",
    "api_keys",
    "auth_login_attempts",
    "auth_recovery_tokens",
    "auth_audit_events",
    "auth_recovery_requests",
    "auth_store_meta",
    "auth_bootstrap_credentials",
)
MAX_SUPPORT_LOGS = 32
MAX_SUPPORT_LOG_BYTES = 2 * 1024 * 1024
MAX_SUPPORT_TOTAL_LOG_BYTES = 16 * 1024 * 1024
MAX_SUPPORT_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_SUPPORT_DEADLINE_SECONDS = 15.0


@contextlib.contextmanager
def _open_regular_binary(path):
    path = Path(path)
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise ValueError("archive must be a regular, non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("archive changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(descriptor)


def _safe_member(info):
    name = str(info.filename or "").replace("\\", "/")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or name.startswith("/"):
        raise ValueError(f"unsafe archive member: {name!r}")
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        raise ValueError(f"archive symlink is not allowed: {name}")
    if info.flag_bits & 0x1:
        raise ValueError(f"encrypted archive member is not supported: {name}")
    return name


def _read_member(zf, info, max_bytes):
    digest = hashlib.sha256()
    total = 0
    chunks = []
    with zf.open(info, "r") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"archive member exceeds limit: {info.filename}")
            digest.update(chunk)
            chunks.append(chunk)
    return b"".join(chunks), digest.hexdigest()


def _readonly_sqlite_bytes(payload):
    con = sqlite3.connect(":memory:")
    con.deserialize(payload)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=on")
    return con


def _validate_query(sql):
    text = str(sql or "").strip()
    if len(text.encode("utf-8")) > MAX_SQL_BYTES:
        raise ValueError("diagnostic query SQL exceeds byte limit")
    safe_pragma = re.fullmatch(r"(?is)pragma\s+(?:table_info|table_xinfo|index_list|index_info|foreign_key_list)\s*\([^;]+\)\s*;?", text)
    if not (re.match(r"(?is)^(select|with)\b", text) or safe_pragma):
        raise ValueError("diagnostic query must be read-only")
    if ";" in text.rstrip(";"):
        raise ValueError("diagnostic query must contain one statement")
    return text.rstrip(";")


def _query_authorizer(action, _arg1, _arg2, _database, _trigger):
    denied = {
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW, sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_ANALYZE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
    }
    return sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK


def _bounded_cell(value, max_bytes):
    if isinstance(value, bytes):
        if len(value) > max_bytes:
            raise ValueError("diagnostic query cell exceeds byte limit")
        return {"blob_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, str) and len(value.encode("utf-8")) > max_bytes:
        raise ValueError("diagnostic query cell exceeds byte limit")
    return value


def _bounded_structure_size(value, limit, *, depth=0, check_deadline=lambda: None):
    check_deadline()
    if depth > 12:
        raise ValueError("support data exceeds nesting limit")
    if isinstance(value, dict):
        if len(value) > 1000:
            raise ValueError("support mapping exceeds item limit")
        total = 2
        for key, child in value.items():
            total += len(str(key).encode("utf-8")) + _bounded_structure_size(child, limit - total, depth=depth + 1, check_deadline=check_deadline)
            if total > limit:
                raise ValueError("structured support input exceeds byte limit")
        return total
    if isinstance(value, (list, tuple)):
        if len(value) > 1000:
            raise ValueError("support list exceeds item limit")
        total = 2
        for child in value:
            total += _bounded_structure_size(child, limit - total, depth=depth + 1, check_deadline=check_deadline)
            if total > limit:
                raise ValueError("structured support input exceeds byte limit")
        return total
    size = len(str(value if value is not None else "null").encode("utf-8"))
    if size > limit:
        raise ValueError("structured support input exceeds byte limit")
    return size


def inspect_backup_archive(
    archive_path,
    *,
    expected_tables=("app_settings",),
    query_specs=(),
    max_query_rows=100,
    max_query_specs=MAX_QUERY_SPECS,
    max_query_cell_bytes=MAX_QUERY_CELL_BYTES,
    max_query_result_bytes=MAX_QUERY_RESULT_BYTES,
    query_deadline_seconds=5.0,
    expected_auth_tables=EXPECTED_AUTH_TABLES,
    expected_schema_versions=(1,),
):
    """Inspect a backup ZIP without restoring it or opening SQLite read/write."""
    archive_path = Path(archive_path)
    result = {"ok": False, "archive": str(archive_path), "errors": [], "warnings": []}
    try:
        query_specs = tuple(query_specs)
        if len(query_specs) > min(int(max_query_specs), MAX_QUERY_SPECS):
            raise ValueError("too many diagnostic queries")
        deadline = time.monotonic() + max(0.01, float(query_deadline_seconds))
        with _open_regular_binary(archive_path) as archive_handle, zipfile.ZipFile(archive_handle, "r") as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("backup archive contains too many members")
            names = [_safe_member(info) for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("backup archive contains duplicate members")
            declared_total = sum(int(info.file_size) for info in infos)
            if declared_total > MAX_ARCHIVE_TOTAL_BYTES or any(int(info.file_size) > MAX_ARCHIVE_MEMBER_BYTES for info in infos):
                raise ValueError("backup archive exceeds inspection limits")
            for info in infos:
                compressed = max(1, int(info.compress_size))
                if int(info.file_size) > 1024 * 1024 and int(info.file_size) / compressed > MAX_ARCHIVE_COMPRESSION_RATIO:
                    raise ValueError(f"backup archive member compression ratio exceeds limit: {info.filename}")
            corrupt = zf.testzip()
            if corrupt:
                raise ValueError(f"backup archive CRC failed: {corrupt}")
            required_members = (MANIFEST_MEMBER, STATE_DB_MEMBER, AUTH_DB_MEMBER, CONFIG_EXPORT_MEMBER, SECRET_REFS_MEMBER)
            for required in required_members:
                if required not in names:
                    raise ValueError(f"backup archive is missing required member: {required}")
            manifest_info = zf.getinfo(MANIFEST_MEMBER)
            if manifest_info.file_size > 1024 * 1024:
                raise ValueError("backup manifest exceeds inspection limit")
            manifest = json.loads(zf.read(manifest_info).decode("utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("backup manifest root must be an object")
            if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") not in set(expected_schema_versions):
                raise ValueError("backup manifest schema_version is unsupported")
            contains = manifest.get("contains") if isinstance(manifest.get("contains"), dict) else {}
            if contains.get("state_db") is not True:
                raise ValueError("backup manifest must declare contains.state_db=true")
            required_flags = {
                "auth_db": AUTH_DB_MEMBER,
                "redacted_config_export": CONFIG_EXPORT_MEMBER,
                "secret_reference_manifest": SECRET_REFS_MEMBER,
            }
            for flag, member in required_flags.items():
                if contains.get(flag) is not True:
                    raise ValueError(f"backup manifest must declare contains.{flag}=true for {member}")
            structured_exports = {}
            for member in (CONFIG_EXPORT_MEMBER, SECRET_REFS_MEMBER):
                info = zf.getinfo(member)
                if int(info.file_size) > MAX_STRUCTURED_MEMBER_BYTES:
                    raise ValueError(f"backup structured member exceeds limit: {member}")
                payload, _digest = _read_member(zf, info, MAX_STRUCTURED_MEMBER_BYTES)
                value = json.loads(payload.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError(f"backup structured member root must be an object: {member}")
                structured_exports[member] = value
            config_export = structured_exports[CONFIG_EXPORT_MEMBER]
            secret_references = structured_exports[SECRET_REFS_MEMBER]
            for field in ("schema_version", "exported_at", "values", "secret_refs"):
                if field not in config_export:
                    raise ValueError(f"backup config export is missing required field: {field}")
            if type(config_export.get("schema_version")) is not int:
                raise ValueError("backup config export schema_version must be an integer")
            if config_export.get("schema_version") != manifest.get("schema_version"):
                raise ValueError("backup config export schema_version does not match manifest")
            if not isinstance(config_export.get("exported_at"), str) or not config_export["exported_at"].strip():
                raise ValueError("backup config export exported_at must be a non-empty string")
            if not isinstance(config_export.get("values"), dict) or not isinstance(config_export.get("secret_refs"), dict):
                raise ValueError("backup config export values and secret_refs must be objects")
            config_values = config_export["values"]
            config_secret_refs = config_export["secret_refs"]
            if any(not isinstance(key, str) or not isinstance(value, str) for key, value in config_values.items()):
                raise ValueError("backup config export values must contain only string keys and values")
            for key, reference in config_secret_refs.items():
                if not isinstance(key, str) or not isinstance(reference, dict):
                    raise ValueError("backup config secret_refs entries must be canonical objects")
                if set(reference) != {"configured", "value"}:
                    raise ValueError("backup config secret_refs entries must contain configured and value only")
                if reference.get("configured") is not True or reference.get("value") != "<redacted>":
                    raise ValueError("backup config secret_refs entries have invalid configured/value fields")
                if config_values.get(key) != "<set>":
                    raise ValueError("backup config secret_refs contains an orphan reference")
            marked_secrets = {key for key, value in config_values.items() if value == "<set>"}
            if marked_secrets != set(config_secret_refs):
                raise ValueError("backup config secret markers and secret_refs do not match")
            for field in ("schema_version", "exported_at", "secrets", "note"):
                if field not in secret_references:
                    raise ValueError(f"backup secret reference manifest is missing required field: {field}")
            if type(secret_references.get("schema_version")) is not int:
                raise ValueError("backup secret reference manifest schema_version must be an integer")
            if secret_references.get("schema_version") != manifest.get("schema_version"):
                raise ValueError("backup secret reference manifest schema_version does not match manifest")
            if not isinstance(secret_references.get("exported_at"), str) or not secret_references["exported_at"].strip():
                raise ValueError("backup secret reference manifest exported_at must be a non-empty string")
            if not isinstance(secret_references.get("secrets"), dict):
                raise ValueError("backup secret reference manifest secrets must be an object")
            if not isinstance(secret_references.get("note"), str) or not secret_references["note"].strip():
                raise ValueError("backup secret reference manifest note must be a non-empty string")
            if secret_references["secrets"] != config_export["secret_refs"]:
                raise ValueError("backup config and secret reference manifests disagree")
            state_payload, sha256 = _read_member(zf, zf.getinfo(STATE_DB_MEMBER), MAX_ARCHIVE_MEMBER_BYTES)
            size_bytes = len(state_payload)
            with contextlib.closing(_readonly_sqlite_bytes(state_payload)) as con:
                    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
                    con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_QUERY_RESULT_BYTES)
                    quick = [str(row[0]) for row in con.execute("pragma quick_check").fetchall()]
                    if quick != ["ok"]:
                        raise ValueError(f"state database quick_check failed: {quick[:3]}")
                    foreign_keys = [dict(row) for row in con.execute("pragma foreign_key_check").fetchmany(101)]
                    if foreign_keys:
                        raise ValueError(f"state database foreign_key_check failed ({len(foreign_keys)} rows sampled)")
                    tables = {
                        str(row[0])
                        for row in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")
                    }
                    missing_tables = sorted(set(expected_tables) - tables)
                    if missing_tables:
                        raise ValueError(f"state database is missing expected tables: {', '.join(missing_tables)}")
                    table_counts = {}
                    for table in sorted(set(expected_tables)):
                        quoted = '"' + str(table).replace('"', '""') + '"'
                        table_counts[table] = int(con.execute(f"select count(*) from {quoted}").fetchone()[0])
                    queries = []
                    query_result_bytes = 0
                    con.set_authorizer(_query_authorizer)
                    for spec in query_specs:
                        if time.monotonic() > deadline:
                            raise ValueError("diagnostic query deadline exceeded")
                        spec = dict(spec or {})
                        name = str(spec.get("name") or "query")[:80]
                        sql = _validate_query(spec.get("sql"))
                        raw_params = spec.get("params")
                        if raw_params is None:
                            params = ()
                        elif isinstance(raw_params, (list, tuple)):
                            params = tuple(raw_params)
                        else:
                            raise ValueError("diagnostic query params must be a list or tuple")
                        if len(params) > MAX_QUERY_PARAMETERS:
                            raise ValueError("diagnostic query has too many parameters")
                        parameter_bytes = 0
                        for parameter in params:
                            if parameter is not None and not isinstance(parameter, (str, bytes, int, float)):
                                raise ValueError("diagnostic query parameter type is unsupported")
                            parameter_bytes += len(parameter if isinstance(parameter, bytes) else str(parameter).encode("utf-8"))
                        if parameter_bytes > MAX_QUERY_PARAMETER_BYTES:
                            raise ValueError("diagnostic query parameters exceed byte limit")
                        limit = max(1, min(int(spec.get("max_rows") or max_query_rows), int(max_query_rows)))
                        safe_columns = {str(item) for item in (spec.get("safe_columns") or ())}
                        cursor = con.execute(sql, params)
                        columns = [item[0] for item in (cursor.description or ())]
                        rows = cursor.fetchmany(limit + 1)
                        cursor.close()
                        bounded_rows = []
                        for row in rows[:limit]:
                            bounded_rows.append([
                                _bounded_cell(value, min(int(max_query_cell_bytes), MAX_QUERY_CELL_BYTES))
                                if columns[index] in safe_columns or value is None
                                else REDACTED
                                for index, value in enumerate(row)
                            ])
                        query_result_bytes += len(json.dumps(bounded_rows, ensure_ascii=False, default=str).encode("utf-8"))
                        if query_result_bytes > min(int(max_query_result_bytes), MAX_QUERY_RESULT_BYTES):
                            raise ValueError("diagnostic query results exceed byte limit")
                        queries.append({
                            "name": name,
                            "columns": columns,
                            "rows": bounded_rows,
                            "row_count": min(len(rows), limit),
                            "truncated": len(rows) > limit,
                            "limit": limit,
                        })
                    generation_row = con.execute(
                        "select value_json from app_settings where key='auth.store_generation'"
                    ).fetchone()
                    if generation_row is None:
                        raise ValueError("state database is missing auth.store_generation")
                    try:
                        state_generation = str(json.loads(generation_row[0]))
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ValueError("state auth.store_generation is invalid") from exc
            auth_payload, auth_sha256 = _read_member(zf, zf.getinfo(AUTH_DB_MEMBER), MAX_ARCHIVE_MEMBER_BYTES)
            auth_size = len(auth_payload)
            try:
                        with contextlib.closing(_readonly_sqlite_bytes(auth_payload)) as auth_con:
                            auth_con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
                            auth_quick = [str(row[0]) for row in auth_con.execute("pragma quick_check").fetchall()]
                            if auth_quick != ["ok"]:
                                raise ValueError(f"auth database quick_check failed: {auth_quick[:3]}")
                            auth_foreign_keys = auth_con.execute("pragma foreign_key_check").fetchmany(101)
                            if auth_foreign_keys:
                                raise ValueError(f"auth database foreign_key_check failed ({len(auth_foreign_keys)} rows sampled)")
                            auth_tables = {
                                str(row[0]) for row in auth_con.execute(
                                    "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                                )
                            }
                            missing_auth = sorted(set(expected_auth_tables) - auth_tables)
                            if missing_auth:
                                raise ValueError(f"auth database is missing expected tables: {', '.join(missing_auth)}")
                            auth_generation_row = auth_con.execute(
                                "select value from auth_store_meta where key='generation'"
                            ).fetchone()
                            if auth_generation_row is None or not str(auth_generation_row[0]):
                                raise ValueError("auth database is missing auth_store_meta generation")
                            auth_generation = str(auth_generation_row[0])
            except sqlite3.Error as exc:
                raise ValueError(f"auth database validation failed: {exc}") from exc
            if not state_generation or state_generation != auth_generation:
                raise ValueError("state/auth store generation mismatch")
            auth_database = {
                "member": AUTH_DB_MEMBER, "size_bytes": auth_size, "sha256": auth_sha256,
                "quick_check": "ok", "foreign_key_check": "ok", "tables": sorted(auth_tables),
                "store_generation": auth_generation,
            }
            result.update({
                "ok": True,
                "manifest": manifest,
                "members": names,
                "declared_uncompressed_bytes": declared_total,
                "state_database": {
                    "member": STATE_DB_MEMBER,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "quick_check": "ok",
                    "foreign_key_check": "ok",
                    "tables": sorted(tables),
                    "expected_table_counts": table_counts,
                },
                "queries": queries,
                "auth_database": auth_database,
                "config_export": config_export,
                "secret_references": secret_references,
            })
    except (OSError, ValueError, zipfile.BadZipFile, sqlite3.Error, json.JSONDecodeError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


def _collect_secrets(value, output, *, key="", depth=0, check_deadline=lambda: None):
    check_deadline()
    if depth > 12:
        raise ValueError("support data exceeds nesting limit")
    if isinstance(value, dict):
        if len(value) > 1000:
            raise ValueError("support mapping exceeds item limit")
        for child_key, child in value.items():
            if SECRET_KEY_RE.search(str(child_key)) and child not in (None, ""):
                output.add(str(child))
            _collect_secrets(child, output, key=str(child_key), depth=depth + 1, check_deadline=check_deadline)
    elif isinstance(value, (list, tuple)):
        if len(value) > 1000:
            raise ValueError("support list exceeds item limit")
        for child in value:
            _collect_secrets(child, output, key=key, depth=depth + 1, check_deadline=check_deadline)
    elif isinstance(value, str):
        for match in URL_RE.finditer(value):
            try:
                parsed = urlsplit(match.group(0).rstrip(".,;)]}"))
            except ValueError:
                continue
            if parsed.username and len(parsed.username.encode("utf-8")) >= 8:
                output.add(parsed.username)
            if parsed.password:
                output.add(parsed.password)
            for parameter, item in parse_qsl(parsed.query, keep_blank_values=True):
                if SECRET_KEY_RE.search(parameter) and item:
                    output.add(item)


def _secret_variants(secrets, check_deadline=lambda: None):
    variants = set()
    for value in secrets:
        check_deadline()
        text = str(value)
        encoded_length = len(text.encode("utf-8"))
        if encoded_length < 4:
            raise ValueError("secret values shorter than four bytes cannot be safely inventoried")
        payload = text.encode("utf-8")
        variants.update({
            text,
            quote(text, safe=""),
            quote_plus(text, safe=""),
            json.dumps(text, ensure_ascii=True)[1:-1],
            base64.b64encode(payload).decode("ascii"),
            base64.urlsafe_b64encode(payload).decode("ascii"),
        })
    return tuple(sorted((item for item in variants if item), key=len, reverse=True))


def _verification_needles(variants, check_deadline=lambda: None):
    needles = set()
    for variant in variants:
        check_deadline()
        if len(variant) <= 1024:
            needles.add(variant)
            continue
        needles.add(variant[:64])
        needles.add(variant[-64:])
        for offset in range(0, len(variant), 1024):
            chunk = variant[offset:offset + 1024]
            if len(chunk) >= 64:
                needles.add(chunk)
    return tuple(sorted(needles, key=len, reverse=True))


def _redact_text(value, variants, check_deadline=lambda: None):
    text = str(value or "")
    for variant in variants:
        check_deadline()
        text = text.replace(variant, REDACTED)
    text = URL_USERINFO_RE.sub(lambda match: match.group(1) + REDACTED + "@", text)
    text = AUTH_RE.sub(REDACTED, text)
    return CREDENTIAL_RE.sub(lambda match: match.group(1) + REDACTED, text)


def _sanitize(value, variants, *, key="", depth=0, check_deadline=lambda: None):
    check_deadline()
    if depth > 12:
        raise ValueError("support data exceeds nesting limit")
    if SECRET_KEY_RE.search(str(key)) and value not in (None, ""):
        return REDACTED
    if isinstance(value, dict):
        if len(value) > 1000:
            raise ValueError("support mapping exceeds item limit")
        return {str(item_key): _sanitize(item, variants, key=str(item_key), depth=depth + 1, check_deadline=check_deadline) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > 1000:
            raise ValueError("support list exceeds item limit")
        return [_sanitize(item, variants, key=key, depth=depth + 1, check_deadline=check_deadline) for item in value]
    if isinstance(value, str):
        return _redact_text(value, variants, check_deadline)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value), variants, check_deadline)


def _tail_file(path, cap, lookbehind, check_deadline=lambda: None):
    check_deadline()
    path = Path(path)
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
        raise ValueError(f"support log must be a regular, non-symlink file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        opened = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"support log changed while opening: {path}")
        read_size = min(int(opened.st_size), int(cap) + int(lookbehind))
        os.lseek(descriptor, max(0, int(opened.st_size) - read_size), os.SEEK_SET)
        data = b""
        while len(data) < read_size:
            check_deadline()
            chunk = os.read(descriptor, read_size - len(data))
            if not chunk:
                break
            data += chunk
        return data, int(opened.st_size) > int(cap)
    finally:
        os.close(descriptor)


def build_support_bundle(
    destination,
    *,
    diagnostics=None,
    query_results=None,
    log_paths=(),
    explicit_secrets=(),
    per_log_cap_bytes=2 * 1024 * 1024,
    total_log_cap_bytes=16 * 1024 * 1024,
    final_archive_cap_bytes=20 * 1024 * 1024,
    max_logs=32,
    deadline_seconds=15.0,
):
    """Build an atomic ZIP from caller-supplied diagnostics; performs no network calls."""
    requested_deadline = float(deadline_seconds)
    if requested_deadline != requested_deadline or abs(requested_deadline) == float("inf"):
        raise ValueError("support bundle deadline must be finite")
    deadline = time.monotonic() + min(max(0.0, requested_deadline), MAX_SUPPORT_DEADLINE_SECONDS)

    def check_deadline():
        if time.monotonic() >= deadline:
            raise ValueError("support bundle deadline exceeded")

    check_deadline()
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    query_results = query_results if isinstance(query_results, (dict, list)) else {}
    diagnostic_input_bytes = _bounded_structure_size(diagnostics, MAX_STRUCTURED_MEMBER_BYTES, check_deadline=check_deadline)
    query_input_bytes = _bounded_structure_size(query_results, MAX_STRUCTURED_MEMBER_BYTES, check_deadline=check_deadline)
    if diagnostic_input_bytes + query_input_bytes > MAX_STRUCTURED_TOTAL_BYTES:
        raise ValueError("structured support input exceeds total byte limit")
    secrets = {str(item) for item in explicit_secrets if item not in (None, "")}
    _collect_secrets(diagnostics, secrets, check_deadline=check_deadline)
    _collect_secrets(query_results, secrets, check_deadline=check_deadline)
    if len(secrets) > 256 or sum(len(item.encode("utf-8")) for item in secrets) > 2 * 1024 * 1024:
        raise ValueError("secret inventory exceeds support-bundle limit")
    variants = _secret_variants(secrets, check_deadline)
    verification_needles = _verification_needles(variants, check_deadline)
    lookbehind = max([64 * 1024] + [len(item.encode("utf-8")) for item in variants])
    clean_diagnostics = _sanitize(diagnostics, variants, check_deadline=check_deadline)
    clean_queries = _sanitize(query_results, variants, check_deadline=check_deadline)
    per_log_cap_bytes = min(int(per_log_cap_bytes), MAX_SUPPORT_LOG_BYTES)
    total_log_cap_bytes = min(int(total_log_cap_bytes), MAX_SUPPORT_TOTAL_LOG_BYTES)
    final_archive_cap_bytes = min(int(final_archive_cap_bytes), MAX_SUPPORT_ARCHIVE_BYTES)
    max_logs = min(int(max_logs), MAX_SUPPORT_LOGS)
    paths = [Path(path) for path in log_paths]
    if len(paths) > max_logs:
        raise ValueError("too many support log files")
    logs = []
    total_log_bytes = 0
    for index, path in enumerate(paths):
        check_deadline()
        remaining = total_log_cap_bytes - total_log_bytes
        if remaining <= 0:
            break
        cap = max(1, min(int(per_log_cap_bytes), remaining))
        raw, truncated = _tail_file(path, cap, lookbehind, check_deadline)
        clean_bytes = _redact_text(raw.decode("utf-8", errors="replace"), variants, check_deadline).encode("utf-8")
        clean_text = clean_bytes[-cap:].decode("utf-8", errors="replace")
        included_bytes = len(clean_text.encode("utf-8"))
        total_log_bytes += included_bytes
        logs.append({
            "member": f"logs/{index:02d}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', path.name)[:100] or 'log.txt'}",
            "source_name": path.name,
            "truncated": truncated,
            "bytes_included": included_bytes,
            "text": clean_text,
        })
    manifest = {
        "schema": "inkdrop.support_bundle.core.v1",
        "members": ["manifest.json", "diagnostics.json", "queries.json"] + [item["member"] for item in logs],
        "log_count": len(logs),
        "logs_truncated": any(item["truncated"] for item in logs) or len(logs) < len(paths),
        "log_bytes_included": total_log_bytes,
        "redaction": {"exact_secret_count": len(secrets), "variant_count": len(variants), "verified": False},
    }
    payloads = {
        "diagnostics.json": json.dumps(clean_diagnostics, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        "queries.json": json.dumps(clean_queries, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
    }
    if any(len(payload) > MAX_STRUCTURED_MEMBER_BYTES for payload in payloads.values()):
        raise ValueError("structured support member exceeds byte limit")
    if sum(len(payload) for payload in payloads.values()) > MAX_STRUCTURED_TOTAL_BYTES:
        raise ValueError("structured support data exceeds total byte limit")
    for item in logs:
        payloads[item["member"]] = item["text"].encode("utf-8")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".inkdrop-support-", suffix=".zip", dir=destination.parent)
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for name, payload in payloads.items():
                check_deadline()
                zf.writestr(name, payload)
            manifest["redaction"]["verified"] = True
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
        if temp_path.stat().st_size > final_archive_cap_bytes:
            raise ValueError("support bundle exceeds final archive limit")
        with zipfile.ZipFile(temp_path, "r") as zf:
            check_deadline()
            if sorted(zf.namelist()) != sorted(manifest["members"]):
                raise ValueError("support bundle members do not match manifest")
            for info in zf.infolist():
                check_deadline()
                chunks = []
                with zf.open(info, "r") as source:
                    while True:
                        check_deadline()
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                body = b"".join(chunks).decode("utf-8", errors="replace")
                leaked = None
                for needle in verification_needles:
                    check_deadline()
                    if needle in body:
                        leaked = needle
                        break
                if leaked:
                    raise ValueError(f"support bundle secret verification failed in {info.filename}")
        os.replace(temp_path, destination)
        return {"ok": True, "path": str(destination), "size_bytes": destination.stat().st_size, "manifest": manifest}
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
