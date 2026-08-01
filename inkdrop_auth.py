#!/usr/bin/env python3
"""InkDrop single-admin authentication and authorization primitives.

This module owns security-sensitive state and deliberately has no dependency on
the web application. Tokens are returned only at creation time; SQLite stores
cryptographic digests, masked fingerprints, and audit-safe metadata.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import inkdrop_db


AUTH_SCHEMA_VERSION = 13
AUTH_MODES = {"built_in", "external", "built_in_or_external", "disabled"}
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
PASSWORD_DEFAULT_MIN_LENGTH = 8
PASSWORD_POLICY_MIN_LENGTH = 1
PASSWORD_POLICY_MAX_LENGTH = 128
PASSWORD_MAX_LENGTH = 1024
# Compatibility name for callers that import the old constant. It now reflects
# the public default; account creation resolves saved/environment policy first.
PASSWORD_MIN_LENGTH = PASSWORD_DEFAULT_MIN_LENGTH
CSRF_COOKIE_NAME = "inkdrop_csrf"
CSRF_HEADER_NAME = "X-InkDrop-CSRF"
SESSION_COOKIE_NAME = "inkdrop_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_MAX_TTL_SECONDS = 30 * 24 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
LOGIN_MAX_BACKOFF_SECONDS = 15 * 60
RECOVERY_TTL_SECONDS = 15 * 60
API_KEY_PREFIX = "ik"
SESSION_PREFIX = "is"
RECOVERY_PREFIX = "ir"
API_KEY_SCOPES = {"read", "write", "settings", "acquisition", "maintenance", "admin"}
API_KEY_MIN_TTL_SECONDS = 60
API_KEY_MAX_TTL_SECONDS = 366 * 24 * 60 * 60
_AUTH_SCHEMA_READY = set()
_AUTH_SCHEMA_LOCK = threading.Lock()
_CONFIG_CACHE = {"key": None, "at": 0.0, "value": None}
_CONFIG_CACHE_LOCK = threading.Lock()
CONFIG_CACHE_TTL_SECONDS = 5
# Auth lives in its own database file so a login never waits on the state
# database's write lock. A long import transaction used to make the login
# POST time out at the busy timeout while plain reads stayed instant.
AUTH_STORE_DB_NAME = "inkdrop-auth.sqlite3"
AUTH_STORE_MIGRATION_NAME = "auth_store_split"
# Copy order matters: sessions and recovery tokens reference auth_users.
AUTH_TABLE_NAMES = (
    "auth_users",
    "auth_sessions",
    "api_keys",
    "auth_login_attempts",
    "auth_recovery_tokens",
    "auth_audit_events",
    "auth_recovery_requests",
)
_AUTH_STORE_READY = set()
_AUTH_STORE_LOCK = threading.Lock()


class AuthError(ValueError):
    def __init__(self, code, message=None, *, status=400, retry_after=None):
        super().__init__(message or code)
        self.code = str(code)
        self.status = int(status)
        self.retry_after = int(retry_after) if retry_after is not None else None

    def payload(self):
        payload = {"ok": False, "error": self.code, "detail": str(self)}
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        return payload


def _json(value, fallback=None):
    try:
        return json.loads(value) if value not in (None, "") else fallback
    except (TypeError, ValueError):
        return fallback


def _json_text(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _digest(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _token(prefix):
    return f"{prefix}_{secrets.token_urlsafe(36)}"


def fingerprint(value):
    digest = _digest(value)
    return f"{digest[:8]}:{digest[-8:]}"


def utc_stamp(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts if ts is not None else time.time())))


def _password_text(password):
    if password is None:
        return ""
    return password if isinstance(password, str) else str(password)


def _password_minimum(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise AuthError(
            "invalid_password_policy",
            f"password minimum length must be an integer from {PASSWORD_POLICY_MIN_LENGTH} through {PASSWORD_POLICY_MAX_LENGTH}",
        ) from None
    if parsed < PASSWORD_POLICY_MIN_LENGTH or parsed > PASSWORD_POLICY_MAX_LENGTH:
        raise AuthError(
            "invalid_password_policy",
            f"password minimum length must be from {PASSWORD_POLICY_MIN_LENGTH} through {PASSWORD_POLICY_MAX_LENGTH}",
        )
    return parsed


def password_policy(db_path=None, environ=None):
    env = os.environ if environ is None else environ
    minimum = None
    source = "default"
    if db_path is not None:
        with _settings_connect(db_path, operation="auth_password_policy") as con:
            saved, saved_source = _setting_record(con, "auth.password_min_length", None)
        if saved_source == "user":
            minimum, source = saved, "saved"
    if minimum in (None, "") and str(env.get("INKDROP_PASSWORD_MIN_LENGTH") or "").strip():
        minimum, source = env.get("INKDROP_PASSWORD_MIN_LENGTH"), "environment"
    if minimum in (None, ""):
        minimum = PASSWORD_DEFAULT_MIN_LENGTH
    minimum = _password_minimum(minimum)
    return {
        "minimum_length": minimum,
        "maximum_length": PASSWORD_MAX_LENGTH,
        "composition_required": False,
        "spaces_allowed": True,
        "unicode_allowed": True,
        "source": source,
        "configuration_valid": True,
        "configuration_error": None,
    }


def validate_password(password, policy=None):
    password = _password_text(password)
    policy = dict(policy or password_policy())
    minimum = _password_minimum(policy.get("minimum_length", PASSWORD_DEFAULT_MIN_LENGTH))
    maximum = int(policy.get("maximum_length") or PASSWORD_MAX_LENGTH)
    if not password:
        raise AuthError("password_empty", "password must not be empty")
    if len(password) < minimum:
        raise AuthError("password_too_short", f"password must be at least {minimum} characters")
    if len(password) > maximum:
        raise AuthError("password_too_long", f"password must be no more than {maximum} characters")
    return password


def password_hash(password, *, salt=None, iterations=PASSWORD_ITERATIONS, policy=None):
    password = validate_password(password, policy)
    iterations = max(260_000, int(iterations or PASSWORD_ITERATIONS))
    salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
    return f"{PASSWORD_ALGORITHM}${iterations}${salt_bytes.hex()}${value.hex()}"


def verify_password(password, stored_hash):
    parts = str(stored_hash or "").split("$")
    if len(parts) != 4 or parts[0] != PASSWORD_ALGORITHM:
        return False
    try:
        iterations = int(parts[1])
        salt_bytes = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
        actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt_bytes, iterations)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def password_needs_rehash(stored_hash):
    parts = str(stored_hash or "").split("$")
    try:
        return len(parts) != 4 or parts[0] != PASSWORD_ALGORITHM or int(parts[1]) < PASSWORD_ITERATIONS
    except (TypeError, ValueError):
        return True


def _ensure_column(con, table, name, declaration):
    columns = {row[1] for row in con.execute(f"pragma table_info({table})")}
    if name not in columns:
        con.execute(f"alter table {table} add column {name} {declaration}")


def ensure_auth_schema(con, *, _test_fail_after=None):
    """Apply the idempotent authentication migration."""
    cache_key = ""
    try:
        for row in con.execute("pragma database_list"):
            if str(row[1] or "") == "main" and str(row[2] or ""):
                cache_key = str(Path(row[2]).resolve())
                break
    except (OSError, sqlite3.Error):
        cache_key = ""
    if _test_fail_after is None and cache_key and cache_key in _AUTH_SCHEMA_READY:
        return
    if _test_fail_after is None:
        _AUTH_SCHEMA_LOCK.acquire()
        if cache_key and cache_key in _AUTH_SCHEMA_READY:
            _AUTH_SCHEMA_LOCK.release()
            return
    schema_sql = """
        create table if not exists schema_migrations (
            version integer primary key,
            name text not null,
            applied_at real not null
        );
        create table if not exists auth_users (
            id text primary key,
            username text not null unique,
            password_hash text not null,
            role text not null default 'admin',
            enabled integer not null default 1,
            created_at real,
            updated_at real,
            last_login_at real,
            password_changed_at real,
            raw_json text
        );
        create table if not exists auth_sessions (
            id text primary key,
            user_id text not null references auth_users(id) on delete cascade,
            token_hash text not null unique,
            csrf_hash text,
            created_at real,
            last_seen_at real,
            expires_at real,
            revoked_at real,
            revoked_reason text,
            user_agent text,
            remote_addr text,
            raw_json text
        );
        create table if not exists api_keys (
            id text primary key,
            name text not null,
            description text,
            key_hash text not null unique,
            fingerprint text not null,
            prefix text,
            scopes_json text not null default '["read"]',
            role text not null default 'admin',
            enabled integer not null default 1,
            created_at real,
            updated_at real,
            last_used_at real,
            expires_at real,
            revoked_at real,
            raw_json text
        );
        create table if not exists auth_login_attempts (
            bucket_key text primary key,
            username_hint text,
            remote_addr text,
            failure_count integer not null default 0,
            window_started_at real,
            last_failed_at real,
            locked_until real,
            updated_at real
        );
        create table if not exists auth_recovery_tokens (
            id text primary key,
            user_id text not null references auth_users(id) on delete cascade,
            token_hash text not null unique,
            created_at real not null,
            expires_at real not null,
            used_at real,
            created_by text,
            raw_json text
        );
        create table if not exists auth_audit_events (
            id text primary key,
            event_type text not null,
            outcome text not null,
            actor_type text,
            actor_id text,
            username_hint text,
            remote_addr text,
            user_agent text,
            created_at real not null,
            detail_json text
        );
        create table if not exists auth_recovery_requests (
            id text primary key,
            username text not null,
            created_at real not null,
            remote_addr text,
            resolved_at real,
            resolved_by text
        );
        create table if not exists auth_store_meta (
            key text primary key,
            value text
        );
        create table if not exists auth_bootstrap_credentials (
            token_hash text primary key,
            created_at real not null,
            expires_at real not null,
            consumed_at real
        );
        create index if not exists idx_auth_users_username on auth_users(username);
        create index if not exists idx_auth_sessions_token_hash on auth_sessions(token_hash);
        create index if not exists idx_auth_sessions_user_active on auth_sessions(user_id, revoked_at, expires_at);
        create index if not exists idx_auth_sessions_cleanup on auth_sessions(expires_at, revoked_at);
        create index if not exists idx_api_keys_fingerprint on api_keys(fingerprint);
        create index if not exists idx_api_keys_enabled on api_keys(enabled, revoked_at);
        create index if not exists idx_auth_login_attempts_locked on auth_login_attempts(locked_until, updated_at);
        create index if not exists idx_auth_recovery_token_hash on auth_recovery_tokens(token_hash);
        create index if not exists idx_auth_recovery_cleanup on auth_recovery_tokens(expires_at, used_at);
        create index if not exists idx_auth_audit_created on auth_audit_events(created_at desc);
        create index if not exists idx_auth_audit_type_created on auth_audit_events(event_type, created_at desc);
        create index if not exists idx_auth_recovery_requests_pending on auth_recovery_requests(resolved_at, created_at desc);
        """
    statements = [statement.strip() for statement in schema_sql.split(";") if statement.strip()]
    savepoint = "inkdrop_auth_v12"
    con.execute(f"savepoint {savepoint}")
    try:
        for index, statement in enumerate(statements, start=1):
            con.execute(statement)
            if _test_fail_after is not None and index >= int(_test_fail_after):
                raise RuntimeError("injected auth migration failure")
        # Upgrade databases created by the original auth prototype.
        for table, columns in {
            "auth_users": {
                "password_changed_at": "real",
            },
            "auth_sessions": {
                "csrf_hash": "text",
                "last_seen_at": "real",
                "revoked_reason": "text",
            },
            "api_keys": {
                "description": "text",
                "scopes_json": "text not null default '[\"read\"]'",
                "expires_at": "real",
            },
        }.items():
            for name, declaration in columns.items():
                _ensure_column(con, table, name, declaration)
        con.execute("create index if not exists idx_api_keys_expires_at on api_keys(expires_at)")
        con.execute(
            "insert or ignore into schema_migrations(version,name,applied_at) values(?,?,?)",
            (12, "auth_security", time.time()),
        )
        con.execute(
            "insert or ignore into schema_migrations(version,name,applied_at) values(?,?,?)",
            (AUTH_SCHEMA_VERSION, "auth_security_api_key_expiry", time.time()),
        )
        con.execute(f"release savepoint {savepoint}")
        if _test_fail_after is None and cache_key:
            _AUTH_SCHEMA_READY.add(cache_key)
    except Exception:
        con.execute(f"rollback to savepoint {savepoint}")
        con.execute(f"release savepoint {savepoint}")
        raise
    finally:
        if _test_fail_after is None:
            _AUTH_SCHEMA_LOCK.release()


def auth_store_path(db_path):
    """Return the auth database that backs the given state database path.

    Auth tables live in a sibling file so session writes never queue behind
    the state database's write lock. INKDROP_AUTH_DB overrides the location.
    """
    override = str(os.environ.get("INKDROP_AUTH_DB") or "").strip()
    if override:
        return Path(override)
    state = Path(db_path)
    # In-memory and bare-name paths (tests) keep auth alongside the state DB.
    if not state.name or state.name == ":memory:":
        return state
    return state.with_name(AUTH_STORE_DB_NAME)


def _connect_exact(db_path, *, readonly=False, operation="auth"):
    return inkdrop_db.connection(
        Path(db_path),
        readonly=readonly,
        timeout_seconds=30,
        busy_timeout_ms=30_000,
        operation=operation,
    )


def _connect(db_path, *, readonly=False, operation="auth"):
    store = auth_store_path(db_path)
    _ensure_auth_store(store, Path(db_path))
    return _connect_exact(store, readonly=readonly, operation=operation)


def _settings_connect(db_path, *, operation="auth_setting"):
    """Open the state database itself: app_settings stays there, auth reads it."""
    return _connect_exact(db_path, operation=operation)


def reset_auth_store_cache():
    """Forget per-process store state; required after a restore replaces files."""
    with _AUTH_STORE_LOCK:
        _AUTH_STORE_READY.clear()
    _AUTH_SCHEMA_READY.clear()
    clear_config_cache()


def _store_generation(con):
    try:
        row = con.execute("select value from auth_store_meta where key='generation'").fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row[0] or "") if row else ""


def _state_auth_generation(state_path):
    """The generation recorded in the state database, '' when none/unreadable."""
    state_path = Path(state_path)
    if str(state_path) in ("", ":memory:") or not state_path.exists():
        return ""
    try:
        with _connect_exact(state_path, operation="auth_setting") as con:
            return str(_setting(con, "auth.store_generation", "") or "")
    except (sqlite3.Error, OSError):
        return ""


def _write_state_auth_generation(state_path, generation):
    """Record the store generation in the state database; False on failure."""
    state_path = Path(state_path)
    if str(state_path) in ("", ":memory:") or not state_path.exists():
        return True
    try:
        with _connect_exact(state_path, operation="auth_setting") as con:
            has_table = con.execute(
                "select 1 from sqlite_master where type='table' and name='app_settings'"
            ).fetchone()
            if not has_table:
                return True
            con.execute(
                "insert into app_settings(key, value_json, source, updated_at) values(?,?,?,?)"
                " on conflict(key) do update set value_json=excluded.value_json,"
                " source=excluded.source, updated_at=excluded.updated_at",
                ("auth.store_generation", json.dumps(str(generation)), "system", time.time()),
            )
        return True
    except (sqlite3.Error, OSError):
        return False


def _auth_store_recovery_requested():
    return str(os.environ.get("INKDROP_AUTH_STORE_RECOVERY") or "").strip().lower() in {
        "1", "true", "yes", "accept",
    }


def _ensure_auth_store(store_path, state_path):
    """Create the auth store, inherit legacy rows once, and hold the line.

    The state database records the store's generation. Once recorded, a
    missing or foreign-generation store FAILS CLOSED instead of silently
    re-inheriting the frozen legacy rows -- a password change, a revoked
    session, or a revoked API key must never come back to life because the
    store file was lost or swapped. Recovery is explicit
    (INKDROP_AUTH_STORE_RECOVERY=accept) and writes an audit event.
    """
    try:
        key = str(Path(store_path).resolve())
    except (OSError, ValueError):
        key = str(store_path)
    if key in _AUTH_STORE_READY:
        return
    with _AUTH_STORE_LOCK:
        if key in _AUTH_STORE_READY:
            return
        same_file = False
        try:
            same_file = Path(store_path).resolve() == Path(state_path).resolve()
        except OSError:
            same_file = False
        state_generation = "" if same_file else _state_auth_generation(state_path)
        recovery = _auth_store_recovery_requested()
        if state_generation and not recovery:
            if not Path(store_path).exists():
                raise AuthError(
                    "auth_store_missing",
                    "The authentication database this installation recorded is missing. "
                    "Restore it from backup, or set INKDROP_AUTH_STORE_RECOVERY=accept to "
                    "rebuild it from the pre-split copy (older credentials come back).",
                    status=503,
                )
            with _connect_exact(store_path, operation="auth_store_migration") as con:
                ensure_auth_schema(con)
                con.commit()
                store_generation = _store_generation(con)
            if store_generation != state_generation:
                raise AuthError(
                    "auth_store_generation_mismatch",
                    "The authentication database does not belong to this installation's "
                    "recorded generation. Restore the matching pair from one backup, or set "
                    "INKDROP_AUTH_STORE_RECOVERY=accept to adopt this store explicitly.",
                    status=503,
                )
            _AUTH_STORE_READY.add(key)
            return
        with _connect_exact(store_path, operation="auth_store_migration") as con:
            ensure_auth_schema(con)
            # ATTACH refuses to run inside a transaction; close the one the
            # schema migration may have left open.
            con.commit()
            done = con.execute(
                "select 1 from schema_migrations where name=?",
                (AUTH_STORE_MIGRATION_NAME,),
            ).fetchone()
            if not done:
                _inherit_legacy_auth_rows(con, store_path, state_path)
            if recovery and state_generation:
                _audit(
                    con,
                    "auth_store_recovery",
                    "accepted",
                    actor_type="operator",
                    detail={
                        "recorded_generation": state_generation,
                        "reason": "INKDROP_AUTH_STORE_RECOVERY explicitly set",
                    },
                )
                con.commit()
            generation = _store_generation(con)
            if not generation:
                generation = secrets.token_hex(16)
                con.execute(
                    "insert or replace into auth_store_meta(key, value) values('generation', ?)",
                    (generation,),
                )
                con.commit()
        # The recorded generation IS the security commit. Proceeding without
        # it would authorize this operation against a store the installation
        # has no durable memory of -- lose that store later and the frozen
        # legacy rows resurrect, the exact class this exists to stop. A busy
        # state database therefore refuses the operation; the next attempt
        # retries the write.
        if not same_file and not _write_state_auth_generation(state_path, generation):
            raise AuthError(
                "auth_store_generation_unrecorded",
                "The installation could not durably record its authentication "
                "database. Nothing was authorized; retry when the state "
                "database is reachable.",
                status=503,
            )
        _AUTH_STORE_READY.add(key)


def _inherit_legacy_auth_rows(con, store_path, state_path):
    state_path = Path(state_path)
    copy_wanted = True
    if str(state_path) in ("", ":memory:") or not state_path.exists():
        copy_wanted = False
    else:
        try:
            if state_path.resolve() == Path(store_path).resolve():
                copy_wanted = False
        except OSError:
            copy_wanted = False
    if copy_wanted:
        # A store that already has users was created fresh by first-run
        # setup; the legacy rows are not its history.
        users_here = int(con.execute("select count(*) from auth_users").fetchone()[0] or 0)
        copy_wanted = users_here == 0
    attached = False
    try:
        if copy_wanted:
            con.execute("attach database ? as legacy", (str(state_path),))
            attached = True
            legacy_tables = {
                str(row[0]) for row in con.execute("select name from legacy.sqlite_master where type='table'")
            }
            for table in AUTH_TABLE_NAMES:
                if table not in legacy_tables:
                    continue
                legacy_columns = {str(row[1]) for row in con.execute(f"pragma legacy.table_info({table})")}
                shared = [
                    str(row[1])
                    for row in con.execute(f"pragma table_info({table})")
                    if str(row[1]) in legacy_columns
                ]
                if not shared:
                    continue
                columns = ",".join(shared)
                con.execute(
                    f"insert or ignore into {table}({columns}) select {columns} from legacy.{table}"
                )
        # Same transaction as the copy: a crash re-runs the whole inherit
        # (insert-or-ignore makes that harmless), never half-stamps.
        con.execute(
            "insert or ignore into schema_migrations(version,name,applied_at) values(?,?,?)",
            (14, AUTH_STORE_MIGRATION_NAME, time.time()),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        if attached:
            con.execute("detach database legacy")


def _audit(con, event_type, outcome, *, actor_type=None, actor_id=None, username=None, remote_addr=None, user_agent=None, detail=None):
    now = time.time()
    safe_detail = dict(detail or {})
    for key in tuple(safe_detail):
        if any(marker in key.lower() for marker in ("password", "token", "secret", "key")):
            safe_detail[key] = "<redacted>"
    con.execute(
        """
        insert into auth_audit_events(
            id,event_type,outcome,actor_type,actor_id,username_hint,remote_addr,user_agent,created_at,detail_json
        ) values(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            uuid.uuid4().hex,
            str(event_type),
            str(outcome),
            str(actor_type or ""),
            str(actor_id or ""),
            str(username or "")[:128],
            str(remote_addr or "")[:128],
            str(user_agent or "")[:256],
            now,
            _json_text(safe_detail),
        ),
    )


def _setting(con, key, fallback=None):
    try:
        row = con.execute("select value_json from app_settings where key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return fallback
    return _json(row[0], fallback) if row else fallback


def _setting_record(con, key, fallback=None):
    try:
        row = con.execute("select value_json,source from app_settings where key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return fallback, ""
    if not row:
        return fallback, ""
    return _json(row[0], fallback), str(row[1] or "")


def _env_bool(env, key, fallback=False):
    value = env.get(key)
    if value in (None, ""):
        return bool(fallback)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _csv(value):
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def parse_trusted_proxy_networks(value):
    """Strictly parse trusted proxy IPs/CIDRs without exposing them in status."""
    valid_networks = []
    invalid_entries = []
    validation_errors = []
    for entry in _csv(value):
        try:
            if "/" in entry:
                network = ipaddress.ip_network(entry, strict=True)
            else:
                address = ipaddress.ip_address(entry)
                network = ipaddress.ip_network(
                    f"{address}/{32 if address.version == 4 else 128}", strict=True
                )
        except ValueError:
            invalid_entries.append(entry)
            validation_errors.append("invalid_ip_or_cidr")
            continue
        canonical = str(network)
        if canonical not in valid_networks:
            valid_networks.append(canonical)
    return {
        "valid_networks": valid_networks,
        "invalid_entries": invalid_entries,
        "validation_errors": sorted(set(validation_errors)),
        "configuration_valid": not invalid_entries,
    }


def validate_setting_value(key, value):
    if str(key or "").strip() != "auth.external.trusted_proxies":
        return value
    parsed = parse_trusted_proxy_networks(value)
    if not parsed["configuration_valid"]:
        raise AuthError(
            "invalid_trusted_proxy_configuration",
            "trusted proxies must contain only valid IP addresses or strict CIDRs",
        )
    return parsed["valid_networks"]


def external_auth_would_be_ready(external_enabled, identity_header, trusted_proxies_value):
    """Shared by resolve_config() and the auth.mode save guard so "ready" can
    never mean two different things depending on which of them asks."""
    trusted_proxy_config = parse_trusted_proxy_networks(trusted_proxies_value)
    return bool(
        external_enabled
        and str(identity_header or "").strip()
        and trusted_proxy_config["valid_networks"]
        and trusted_proxy_config["configuration_valid"]
    )


def clear_config_cache():
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE.update({"key": None, "at": 0.0, "value": None})


def resolve_config(db_path, environ=None):
    env = dict(os.environ if environ is None else environ)
    db_path = Path(db_path)
    # Key the cache on the auth store's file times, not the state database's:
    # state mtimes change on every background write, which made this cache
    # miss almost always. Setting changes are covered by clear_config_cache()
    # in-process and by the short TTL across processes.
    store_path = auth_store_path(db_path)
    try:
        db_mtime = store_path.stat().st_mtime_ns
    except OSError:
        db_mtime = 0
    try:
        wal_mtime = Path(str(store_path) + "-wal").stat().st_mtime_ns
    except OSError:
        wal_mtime = 0
    env_keys = (
        "INKDROP_AUTH_MODE", "INKDROP_AUTH_REQUIRED", "INKDROP_AUTH_ALLOW_DISABLED",
        "INKDROP_TRUSTED_LAN_TESTING", "INKDROP_EXTERNAL_AUTH_ENABLED",
        "INKDROP_EXTERNAL_AUTH_HEADER", "INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES",
        "INKDROP_EXTERNAL_AUTH_GROUP_HEADER", "INKDROP_EXTERNAL_AUTH_ADMIN_GROUP",
        "INKDROP_AUTH_ALLOWED_ORIGINS", "INKDROP_AUTH_SESSION_TTL_SECONDS",
        "INKDROP_AUTH_COOKIE_SECURE", "INKDROP_API_KEYS_WHEN_DISABLED",
        "INKDROP_PASSWORD_MIN_LENGTH",
    )
    cache_key = (str(db_path.resolve()), str(store_path), db_mtime, wal_mtime, tuple((key, str(env.get(key) or "")) for key in env_keys))
    now = time.time()
    with _CONFIG_CACHE_LOCK:
        cached = _CONFIG_CACHE.get("value")
        if cached is not None and _CONFIG_CACHE.get("key") == cache_key and now - float(_CONFIG_CACHE.get("at") or 0) < CONFIG_CACHE_TTL_SECONDS:
            return json.loads(json.dumps(cached))
    env_mode = str(env.get("INKDROP_AUTH_MODE") or "").strip().lower()
    with _connect(db_path, operation="auth_config") as con:
        ensure_auth_schema(con)
        user_count = int(con.execute("select count(*) from auth_users where enabled=1").fetchone()[0] or 0)
        active_sessions = int(con.execute("select count(*) from auth_sessions where revoked_at is null and expires_at>?", (time.time(),)).fetchone()[0] or 0)
        active_keys = int(con.execute("select count(*) from api_keys where enabled=1 and revoked_at is null and (expires_at is null or expires_at>?)", (time.time(),)).fetchone()[0] or 0)
        revoked_keys = int(con.execute("select count(*) from api_keys where enabled=0 or revoked_at is not null").fetchone()[0] or 0)
    # auth.* settings live in app_settings, which stays in the state database.
    with _settings_connect(db_path, operation="auth_config_settings") as con:
        stored_mode_value, stored_mode_source = _setting_record(con, "auth.mode", "")
        stored_mode = str(stored_mode_value or "").strip().lower()
        db_external_enabled, external_enabled_source = _setting_record(con, "auth.external.enabled", False)
        db_identity_header, identity_header_source = _setting_record(con, "auth.external.identity_header", "")
        db_group_header, group_header_source = _setting_record(con, "auth.external.group_header", "")
        db_admin_group, admin_group_source = _setting_record(con, "auth.external.admin_group", "")
        db_trusted, trusted_source = _setting_record(con, "auth.external.trusted_proxies", [])
        db_session_ttl, session_ttl_source = _setting_record(con, "auth.session_ttl_seconds", SESSION_TTL_SECONDS)
        db_allowed_origins, allowed_origins_source = _setting_record(con, "auth.allowed_origins", [])
    requested_mode = stored_mode if stored_mode_source == "user" else (env_mode or stored_mode)
    legacy_required = _env_bool(env, "INKDROP_AUTH_REQUIRED", False)
    legacy_external = (
        external_enabled_source != "user"
        and _env_bool(env, "INKDROP_EXTERNAL_AUTH_ENABLED", bool(db_external_enabled))
    )
    inferred_mode = (
        "built_in_or_external" if legacy_required and legacy_external
        else "external" if legacy_external
        else "built_in"
    )
    mode = requested_mode or stored_mode or inferred_mode
    if mode not in AUTH_MODES:
        mode = "built_in"
    disabled_allowed = _env_bool(env, "INKDROP_AUTH_ALLOW_DISABLED", False) and _env_bool(
        env, "INKDROP_TRUSTED_LAN_TESTING", False
    )
    mode_rejected = mode == "disabled" and not disabled_allowed
    if mode_rejected:
        mode = "built_in"
    external_policy = bool(db_external_enabled) if external_enabled_source == "user" else _env_bool(env, "INKDROP_EXTERNAL_AUTH_ENABLED", db_external_enabled)
    external_enabled = mode in {"external", "built_in_or_external"} and (external_policy or mode == "external")
    trusted_proxies_input = db_trusted if trusted_source == "user" else (env.get("INKDROP_EXTERNAL_AUTH_TRUSTED_PROXIES") or db_trusted)
    trusted_proxy_config = parse_trusted_proxy_networks(trusted_proxies_input)
    trusted_proxies = trusted_proxy_config["valid_networks"]
    identity_header = str(db_identity_header if identity_header_source == "user" else (env.get("INKDROP_EXTERNAL_AUTH_HEADER") or db_identity_header or "X-Forwarded-User")).strip()
    group_header = str(db_group_header if group_header_source == "user" else (env.get("INKDROP_EXTERNAL_AUTH_GROUP_HEADER") or db_group_header or "")).strip()
    admin_group = str(db_admin_group if admin_group_source == "user" else (env.get("INKDROP_EXTERNAL_AUTH_ADMIN_GROUP") or db_admin_group or "")).strip()
    allowed_origins = _csv(db_allowed_origins if allowed_origins_source == "user" else (env.get("INKDROP_AUTH_ALLOWED_ORIGINS") or db_allowed_origins))
    session_ttl_value = db_session_ttl if session_ttl_source == "user" else (env.get("INKDROP_AUTH_SESSION_TTL_SECONDS") or db_session_ttl or SESSION_TTL_SECONDS)
    built_in_enabled = mode in {"built_in", "built_in_or_external"}
    bootstrap_required = built_in_enabled and user_count == 0
    external_ready = external_auth_would_be_ready(external_enabled, identity_header, trusted_proxies_input)
    enforcement_active = (
        (built_in_enabled and user_count > 0)
        or (mode in {"external", "built_in_or_external"} and external_ready)
    )
    setup_required = mode != "disabled" and not enforcement_active
    result = {
        "mode": mode,
        "requested_mode": requested_mode or None,
        "mode_rejected": mode_rejected,
        "disabled_allowed": disabled_allowed,
        "required": enforcement_active,
        "enforcement_active": enforcement_active,
        "setup_required": setup_required,
        "built_in_enabled": built_in_enabled,
        "external_enabled": external_enabled,
        "api_keys_enabled": mode != "disabled" or _env_bool(env, "INKDROP_API_KEYS_WHEN_DISABLED", True),
        "bootstrap_required": bootstrap_required,
        "user_count": user_count,
        "active_session_count": active_sessions,
        "active_api_key_count": active_keys,
        "revoked_api_key_count": revoked_keys,
        "external": {
            "enabled": external_enabled,
            "identity_header": identity_header,
            "group_header": group_header or None,
            "admin_group": admin_group or None,
            "trusted_proxies": trusted_proxies,
            "configuration_valid": trusted_proxy_config["configuration_valid"],
            "configuration_errors": trusted_proxy_config["validation_errors"],
            "invalid_entry_count": len(trusted_proxy_config["invalid_entries"]),
            "ready": external_ready,
        },
        "allowed_origins": allowed_origins,
        "session_ttl_seconds": max(300, min(SESSION_MAX_TTL_SECONDS, int(session_ttl_value))),
        "cookie_secure": str(env.get("INKDROP_AUTH_COOKIE_SECURE") or "auto").strip().lower(),
    }
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE.update({"key": cache_key, "at": now, "value": result})
    return json.loads(json.dumps(result))


def _setting_from_db(db_path, key, fallback=None):
    with _settings_connect(db_path) as con:
        return _setting(con, key, fallback)


def public_status(db_path, environ=None):
    config = resolve_config(db_path, environ)
    external = config["external"]
    try:
        resolved_password_policy = password_policy(db_path, environ=environ)
    except AuthError as exc:
        # A malformed bootstrap value must be visible and must block password
        # creation/change, but it must not make login/status unavailable for an
        # existing installation.
        resolved_password_policy = password_policy(environ={})
        resolved_password_policy.update({
            "source": "safe_default",
            "configuration_valid": False,
            "configuration_error": exc.code,
        })
    status = {
        "ok": True,
        "mode": config["mode"],
        "required": config["required"],
        "enforcement_active": config["enforcement_active"],
        "setup_required": config["setup_required"],
        "mode_rejected": config["mode_rejected"],
        "built_in_auth": {
            "available": True,
            "enabled": config["built_in_enabled"],
            "configured": config["user_count"] > 0,
            "user_count": config["user_count"],
            "active_session_count": config["active_session_count"],
            "bootstrap_required": config["bootstrap_required"],
            "login_supported": True,
            "password_change_supported": True,
            "recovery": "local_cli_one_time_token",
        },
        "external_auth": {
            "compatible": True,
            "enabled": external["enabled"],
            "ready": external["ready"],
            "identity_header": external["identity_header"],
            "group_header": external["group_header"],
            "admin_group_required": bool(external["admin_group"]),
            "trusted_proxy_count": len(external["trusted_proxies"]),
            "external_configuration_valid": external["configuration_valid"],
            "external_configuration_errors": external["configuration_errors"],
            "trust_boundary": "Headers are accepted only from configured trusted proxy networks.",
            "next_action": (
                "External authentication is ready behind the configured trusted proxy."
                if external["ready"]
                else "Configure at least one trusted proxy CIDR before external identity headers can authenticate."
            ),
        },
        "api_keys": {
            "available": True,
            "enabled": config["api_keys_enabled"],
            "configured": config["active_api_key_count"] > 0,
            "active_count": config["active_api_key_count"],
            "revoked_count": config["revoked_api_key_count"],
            "scopes": sorted(API_KEY_SCOPES),
            "raw_key_visible_once": True,
        },
        "password_policy": resolved_password_policy,
        "browser_mutation_contract": {
            "csrf_cookie_name": CSRF_COOKIE_NAME,
            "csrf_header_name": CSRF_HEADER_NAME,
            "csrf_required_for_cookie_mutations": True,
            "protected_methods": ["POST", "PUT", "PATCH", "DELETE"],
            "origin_validation_required": True,
            "api_keys_require_csrf": False,
        },
        "csrf_cookie_name": CSRF_COOKIE_NAME,
        "csrf_header_name": CSRF_HEADER_NAME,
        "csrf_required_for_cookie_mutations": True,
        "session_authenticated": False,
        "session_expires_at": None,
        "public_endpoints": ["/status.json", "/api/system/version", "/api/auth/status"],
        "secret_policy": "Passwords, sessions, recovery tokens, and API keys are stored only as cryptographic hashes.",
    }
    return status


def bootstrap_admin(db_path, username, password, *, remote_addr=None, user_agent=None):
    """Create the first administrator. The first caller to finish wins.

    begin immediate is what makes that safe: it takes the write lock before
    the existence check, so two simultaneous requests serialize and the
    second one sees the administrator the first one wrote and is refused.
    """
    username = str(username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,128}", username):
        raise AuthError("invalid_username", "username must be 3-128 safe characters")
    hashed = password_hash(password, policy=password_policy(db_path))
    now = time.time()
    with _connect(db_path, operation="auth_bootstrap") as con:
        ensure_auth_schema(con)
        con.execute("begin immediate")
        if con.execute("select 1 from auth_users limit 1").fetchone():
            _audit(con, "bootstrap", "denied", username=username, remote_addr=remote_addr, user_agent=user_agent)
            con.commit()
            raise AuthError("bootstrap_closed", "the first administrator already exists", status=409)
        user_id = uuid.uuid4().hex
        con.execute(
            "insert into auth_users(id,username,password_hash,role,enabled,created_at,updated_at,password_changed_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
            (user_id, username, hashed, "admin", 1, now, now, now, _json_text({"source": "bootstrap"})),
        )
        _audit(con, "bootstrap", "success", actor_type="user", actor_id=user_id, username=username, remote_addr=remote_addr, user_agent=user_agent)
    return {"ok": True, "user": {"id": user_id, "username": username, "role": "admin", "enabled": True}}


def _login_bucket(username, remote_addr):
    # Single-admin alpha: back off the remote address rather than a supplied
    # username so attackers cannot bypass the limiter by rotating usernames.
    return _digest(f"remote|{str(remote_addr or 'unknown')}")


def _rate_limit_row(con, username, remote_addr, now):
    key = _login_bucket(username, remote_addr)
    row = con.execute("select * from auth_login_attempts where bucket_key=?", (key,)).fetchone()
    if row and float(row["locked_until"] or 0) > now:
        retry = max(1, int(float(row["locked_until"]) - now))
        raise AuthError("login_rate_limited", "too many failed login attempts", status=429, retry_after=retry)
    return key, row


def _record_login_failure(con, key, row, username, remote_addr, now):
    window_started = float(row["window_started_at"] or 0) if row else 0
    failures = int(row["failure_count"] or 0) if row else 0
    if not window_started or now - window_started > LOGIN_WINDOW_SECONDS:
        window_started, failures = now, 0
    failures += 1
    locked_until = None
    if failures >= LOGIN_MAX_FAILURES:
        exponent = min(6, failures - LOGIN_MAX_FAILURES)
        locked_until = now + min(LOGIN_MAX_BACKOFF_SECONDS, 30 * (2**exponent))
    con.execute(
        """
        insert into auth_login_attempts(bucket_key,username_hint,remote_addr,failure_count,window_started_at,last_failed_at,locked_until,updated_at)
        values(?,?,?,?,?,?,?,?)
        on conflict(bucket_key) do update set failure_count=excluded.failure_count,window_started_at=excluded.window_started_at,
        last_failed_at=excluded.last_failed_at,locked_until=excluded.locked_until,updated_at=excluded.updated_at
        """,
        (key, str(username or "")[:128], str(remote_addr or "")[:128], failures, window_started, now, locked_until, now),
    )
    return locked_until


def login(db_path, username, password, *, remote_addr=None, user_agent=None, ttl_seconds=None):
    username = str(username or "").strip()
    now = time.time()
    with _connect(db_path, operation="auth_login") as con:
        ensure_auth_schema(con)
        key, attempt = _rate_limit_row(con, username, remote_addr, now)
        row = con.execute(
            "select id,username,password_hash,role,enabled from auth_users where lower(username)=lower(?)",
            (username,),
        ).fetchone()
        if not row or not bool(row["enabled"]) or not verify_password(password, row["password_hash"]):
            locked_until = _record_login_failure(con, key, attempt, username, remote_addr, now)
            _audit(con, "login", "failure", username=username, remote_addr=remote_addr, user_agent=user_agent)
            retry = max(1, int(locked_until - now)) if locked_until else None
            con.commit()
            raise AuthError("invalid_credentials", "invalid username or password", status=429 if retry else 401, retry_after=retry)
        con.execute("delete from auth_login_attempts where bucket_key=?", (key,))
        if password_needs_rehash(row["password_hash"]):
            con.execute("update auth_users set password_hash=?,updated_at=? where id=?", (password_hash(password), now, row["id"]))
        token = _token(SESSION_PREFIX)
        csrf_token = _token("ic")
        session_id = uuid.uuid4().hex
        ttl = max(300, min(SESSION_MAX_TTL_SECONDS, int(ttl_seconds or SESSION_TTL_SECONDS)))
        expires_at = now + ttl
        con.execute(
            """
            insert into auth_sessions(id,user_id,token_hash,csrf_hash,created_at,last_seen_at,expires_at,user_agent,remote_addr,raw_json)
            values(?,?,?,?,?,?,?,?,?,?)
            """,
            (session_id, row["id"], _digest(token), _digest(csrf_token), now, now, expires_at, str(user_agent or "")[:256], str(remote_addr or "")[:128], _json_text({"source": "login"})),
        )
        con.execute("update auth_users set last_login_at=?,updated_at=? where id=?", (now, now, row["id"]))
        _audit(con, "login", "success", actor_type="user", actor_id=row["id"], username=row["username"], remote_addr=remote_addr, user_agent=user_agent)
    return {
        "ok": True,
        "session": {"id": session_id, "token": token, "csrf_token": csrf_token, "expires_at": expires_at, "expires_at_iso": utc_stamp(expires_at)},
        "user": {"id": row["id"], "username": row["username"], "role": row["role"], "enabled": True},
    }


def verify_session(db_path, token, *, csrf_token=None, touch=False):
    if not token:
        return None
    now = time.time()
    with _connect(db_path, operation="auth_session_verify") as con:
        ensure_auth_schema(con)
        row = con.execute(
            """
            select s.id session_id,s.csrf_hash,s.last_seen_at,s.expires_at,u.id user_id,u.username,u.role,u.enabled
            from auth_sessions s join auth_users u on u.id=s.user_id
            where s.token_hash=? and s.revoked_at is null and s.expires_at>?
            """,
            (_digest(token), now),
        ).fetchone()
        if not row or not bool(row["enabled"]):
            return None
        csrf_valid = bool(csrf_token and row["csrf_hash"] and hmac.compare_digest(_digest(csrf_token), row["csrf_hash"]))
        if touch and (not row["last_seen_at"] or now - float(row["last_seen_at"]) >= 60):
            con.execute("update auth_sessions set last_seen_at=? where id=?", (now, row["session_id"]))
        return {
            "session": {"id": row["session_id"], "expires_at": row["expires_at"], "csrf_valid": csrf_valid},
            "user": {"id": row["user_id"], "username": row["username"], "role": row["role"], "enabled": True},
            "scopes": ["admin"],
        }


def revoke_session(db_path, token, *, reason="logout", actor=None):
    if not token:
        return {"ok": True, "revoked": 0}
    now = time.time()
    with _connect(db_path, operation="auth_session_revoke") as con:
        ensure_auth_schema(con)
        row = con.execute("select id,user_id from auth_sessions where token_hash=? and revoked_at is null", (_digest(token),)).fetchone()
        count = 0
        if row:
            count = con.execute("update auth_sessions set revoked_at=?,revoked_reason=? where id=?", (now, str(reason)[:128], row["id"])).rowcount
            _audit(con, "session_revoke", "success", actor_type=(actor or {}).get("method"), actor_id=(actor or {}).get("id"), detail={"session_id": row["id"], "reason": reason})
    return {"ok": True, "revoked": int(count or 0)}


def revoke_user_sessions(db_path, user_id, *, except_session_id=None, reason="admin_revoke", actor=None):
    now = time.time()
    with _connect(db_path, operation="auth_sessions_revoke_all") as con:
        ensure_auth_schema(con)
        if except_session_id:
            cur = con.execute("update auth_sessions set revoked_at=?,revoked_reason=? where user_id=? and revoked_at is null and id<>?", (now, reason, user_id, except_session_id))
        else:
            cur = con.execute("update auth_sessions set revoked_at=?,revoked_reason=? where user_id=? and revoked_at is null", (now, reason, user_id))
        _audit(con, "sessions_revoke", "success", actor_type=(actor or {}).get("method"), actor_id=(actor or {}).get("id"), detail={"user_id": user_id, "count": int(cur.rowcount or 0), "reason": reason})
    return {"ok": True, "revoked": int(cur.rowcount or 0)}


def change_password(
    db_path,
    user_id,
    current_password,
    new_password,
    *,
    current_session_id=None,
    revoke_other_sessions=True,
    remote_addr=None,
    user_agent=None,
):
    new_hash = password_hash(new_password, policy=password_policy(db_path))
    now = time.time()
    with _connect(db_path, operation="auth_password_change") as con:
        ensure_auth_schema(con)
        row = con.execute("select id,username,password_hash from auth_users where id=? and enabled=1", (user_id,)).fetchone()
        if not row or not verify_password(current_password, row["password_hash"]):
            _audit(con, "password_change", "failure", actor_type="user", actor_id=user_id, remote_addr=remote_addr, user_agent=user_agent)
            con.commit()
            raise AuthError("invalid_current_password", "current password is incorrect", status=403)
        con.execute("update auth_users set password_hash=?,password_changed_at=?,updated_at=? where id=?", (new_hash, now, now, user_id))
        if revoke_other_sessions:
            if current_session_id:
                con.execute("update auth_sessions set revoked_at=?,revoked_reason='password_changed' where user_id=? and revoked_at is null and id<>?", (now, user_id, current_session_id))
            else:
                con.execute("update auth_sessions set revoked_at=?,revoked_reason='password_changed' where user_id=? and revoked_at is null", (now, user_id))
        _audit(con, "password_change", "success", actor_type="user", actor_id=user_id, username=row["username"], remote_addr=remote_addr, user_agent=user_agent)
    return {"ok": True, "password_changed": True, "other_sessions_revoked": bool(revoke_other_sessions)}


def normalize_scopes(scopes):
    values = {str(value or "").strip().lower() for value in (scopes or ["read"])}
    unknown = values - API_KEY_SCOPES
    if unknown:
        raise AuthError("invalid_api_key_scope", f"unknown API key scope: {', '.join(sorted(unknown))}")
    return sorted(values or {"read"})


def _api_key_expiration(*, expires_in_seconds=None, expires_at=None, now=None):
    now = float(now if now is not None else time.time())
    if expires_in_seconds not in (None, "") and expires_at not in (None, ""):
        raise AuthError("conflicting_api_key_expiration", "choose expires_in_seconds or expires_at, not both")
    if expires_in_seconds not in (None, ""):
        try:
            ttl = int(expires_in_seconds)
        except (TypeError, ValueError):
            raise AuthError("invalid_api_key_expiration", "expires_in_seconds must be an integer")
        if ttl < API_KEY_MIN_TTL_SECONDS or ttl > API_KEY_MAX_TTL_SECONDS:
            raise AuthError("invalid_api_key_expiration", "API key expiration is outside the supported range")
        return now + ttl
    if expires_at not in (None, ""):
        try:
            timestamp = float(expires_at)
        except (TypeError, ValueError):
            raise AuthError("invalid_api_key_expiration", "expires_at must be a Unix timestamp")
        if timestamp <= now or timestamp - now > API_KEY_MAX_TTL_SECONDS:
            raise AuthError("invalid_api_key_expiration", "API key expiration must be future and within one year")
        return timestamp
    return None


def create_api_key(db_path, name, *, description="", scopes=None, actor=None, expires_in_seconds=None, expires_at=None):
    name = str(name or "").strip()
    if not name or len(name) > 128:
        raise AuthError("invalid_api_key_name", "API key name is required and must be under 128 characters")
    scopes = normalize_scopes(scopes)
    token = _token(API_KEY_PREFIX)
    now = time.time()
    expires_at = _api_key_expiration(
        expires_in_seconds=expires_in_seconds,
        expires_at=expires_at,
        now=now,
    )
    key_id = uuid.uuid4().hex
    prefix = token[:12]
    key_fingerprint = fingerprint(token)
    with _connect(db_path, operation="auth_api_key_create") as con:
        ensure_auth_schema(con)
        con.execute(
            """
            insert into api_keys(id,name,description,key_hash,fingerprint,prefix,scopes_json,role,enabled,created_at,updated_at,expires_at,raw_json)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (key_id, name, str(description or "")[:512], _digest(token), key_fingerprint, prefix, _json_text(scopes), "admin" if "admin" in scopes else "api", 1, now, now, expires_at, _json_text({"raw_key_visible_once": True})),
        )
        _audit(con, "api_key_create", "success", actor_type=(actor or {}).get("method"), actor_id=(actor or {}).get("id"), detail={"api_key_id": key_id, "scopes": scopes})
    return {"ok": True, "api_key": {"id": key_id, "name": name, "description": str(description or "")[:512], "key": token, "prefix": prefix, "fingerprint": key_fingerprint, "scopes": scopes, "created_at": now, "expires_at": expires_at, "expired": False, "raw_key_visible_once": True}}


def list_api_keys(db_path):
    with _connect(db_path, operation="auth_api_key_list") as con:
        ensure_auth_schema(con)
        rows = con.execute("select id,name,description,fingerprint,prefix,scopes_json,enabled,created_at,updated_at,last_used_at,expires_at,revoked_at from api_keys order by created_at desc").fetchall()
    now = time.time()
    return [{
        "id": row["id"], "name": row["name"], "description": row["description"] or "", "fingerprint": row["fingerprint"],
        "preview": f"{row['prefix']}..." if row["prefix"] else "", "scopes": _json(row["scopes_json"], ["read"]),
        "enabled": bool(row["enabled"]) and row["revoked_at"] is None and (row["expires_at"] is None or float(row["expires_at"]) > now), "created_at": row["created_at"],
        "updated_at": row["updated_at"], "last_used_at": row["last_used_at"], "expires_at": row["expires_at"],
        "expired": row["expires_at"] is not None and float(row["expires_at"]) <= now, "revoked_at": row["revoked_at"],
    } for row in rows]


def verify_api_key(db_path, token, *, mark_used=False):
    if not token:
        return None
    now = time.time()
    with _connect(db_path, operation="auth_api_key_verify") as con:
        ensure_auth_schema(con)
        row = con.execute("select id,name,description,fingerprint,prefix,scopes_json,last_used_at,expires_at from api_keys where key_hash=? and enabled=1 and revoked_at is null and (expires_at is null or expires_at>?)", (_digest(token), now)).fetchone()
        if not row:
            return None
        if mark_used and (not row["last_used_at"] or now - float(row["last_used_at"]) >= 60):
            con.execute("update api_keys set last_used_at=?,updated_at=? where id=?", (now, now, row["id"]))
        return {"id": row["id"], "name": row["name"], "description": row["description"] or "", "fingerprint": row["fingerprint"], "prefix": row["prefix"], "scopes": _json(row["scopes_json"], ["read"]), "expires_at": row["expires_at"], "enabled": True}


def revoke_api_key(db_path, key_id, *, actor=None):
    key_id = str(key_id or "").strip()
    if not key_id:
        raise AuthError("api_key_id_required")
    now = time.time()
    with _connect(db_path, operation="auth_api_key_revoke") as con:
        ensure_auth_schema(con)
        cur = con.execute("update api_keys set enabled=0,revoked_at=?,updated_at=? where id=? and revoked_at is null", (now, now, key_id))
        _audit(con, "api_key_revoke", "success" if cur.rowcount else "not_found", actor_type=(actor or {}).get("method"), actor_id=(actor or {}).get("id"), detail={"api_key_id": key_id})
    return {"ok": True, "revoked": int(cur.rowcount or 0), "api_keys": list_api_keys(db_path)}


def remote_is_trusted(remote_addr, trusted_proxies):
    try:
        address = ipaddress.ip_address(str(remote_addr or ""))
    except ValueError:
        return False
    for value in trusted_proxies or []:
        try:
            if address in ipaddress.ip_network(str(value), strict=False):
                return True
        except ValueError:
            continue
    return False


def external_principal(headers, remote_addr, config):
    external = (config or {}).get("external") or {}
    if not external.get("enabled") or not remote_is_trusted(remote_addr, external.get("trusted_proxies")):
        return None
    username = str((headers or {}).get(external.get("identity_header") or "") or "").strip()
    if not username:
        return None
    admin_group = str(external.get("admin_group") or "").strip()
    groups = _csv((headers or {}).get(external.get("group_header") or "")) if external.get("group_header") else []
    if admin_group and admin_group not in groups:
        return None
    return {"method": "external", "id": username, "user": {"username": username, "role": "admin"}, "scopes": ["admin"], "trusted_proxy": str(remote_addr)}


def audit_external_auth(db_path, principal, *, user_agent=None, minimum_interval_seconds=3600):
    """Record external authentication without writing one event per request."""
    if not principal or principal.get("method") != "external":
        return False
    username = str(((principal.get("user") or {}).get("username") or ""))
    remote_addr = str(principal.get("trusted_proxy") or "")
    now = time.time()
    with _connect(db_path, operation="auth_external_audit") as con:
        ensure_auth_schema(con)
        recent = con.execute(
            "select 1 from auth_audit_events where event_type='external_auth' and actor_id=? and remote_addr=? and created_at>? limit 1",
            (username, remote_addr, now - max(60, int(minimum_interval_seconds or 3600))),
        ).fetchone()
        if recent:
            return False
        _audit(con, "external_auth", "success", actor_type="external", actor_id=username, username=username, remote_addr=remote_addr, user_agent=user_agent)
    return True


def principal_has_scope(principal, scope):
    if not principal:
        return False
    scopes = set(principal.get("scopes") or [])
    role = str(((principal.get("user") or {}).get("role") or principal.get("role") or "")).lower()
    if "admin" in scopes or role == "admin" or scope in scopes:
        return True
    if scope == "read" and scopes:
        return True
    if scope == "write" and scopes.intersection({"settings", "acquisition", "maintenance"}):
        return True
    return False


def authorize(principal, *, scope="read", admin_required=False):
    if not principal:
        raise AuthError("authentication_required", status=401)
    if admin_required and not principal_has_scope(principal, "admin"):
        raise AuthError("admin_required", status=403)
    if not principal_has_scope(principal, scope):
        raise AuthError("insufficient_scope", f"API key requires '{scope}' scope", status=403)
    return principal


def cleanup_auth_state(
    db_path,
    *,
    actor=None,
    batch_limit=100,
    now=None,
    session_retention_seconds=24 * 60 * 60,
    recovery_retention_seconds=24 * 60 * 60,
    login_quiet_seconds=24 * 60 * 60,
):
    """Delete stale authentication state in short, bounded transactions."""
    started = time.monotonic()
    now = float(now if now is not None else time.time())
    limit = max(1, min(1000, int(batch_limit or 100)))
    categories = {
        "sessions": (
            "auth_sessions", "id",
            "((expires_at is not null and expires_at<=?) or (revoked_at is not null and revoked_at<=?))",
            (now - max(0, int(session_retention_seconds)), now - max(0, int(session_retention_seconds))),
        ),
        "recovery_tokens": (
            "auth_recovery_tokens", "id",
            "((expires_at<=?) or (used_at is not null and used_at<=?))",
            (now - max(0, int(recovery_retention_seconds)), now - max(0, int(recovery_retention_seconds))),
        ),
        "login_attempts": (
            "auth_login_attempts", "bucket_key",
            "updated_at<=? and (locked_until is null or locked_until<=?)",
            (now - max(60, int(login_quiet_seconds)), now),
        ),
    }
    result = {"ok": True, "batch_limit": limit, "examined": 0, "deleted": {}, "remaining_eligible": {}, "error": None}
    try:
        with _connect(db_path, operation="auth_cleanup") as con:
            ensure_auth_schema(con)
            con.execute("begin immediate")
            for name, (table, identity, predicate, params) in categories.items():
                ids = [row[0] for row in con.execute(
                    f"select {identity} from {table} where {predicate} order by {identity} limit ?",
                    (*params, limit),
                ).fetchall()]
                result["examined"] += len(ids)
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    deleted = con.execute(f"delete from {table} where {identity} in ({placeholders})", ids).rowcount
                else:
                    deleted = 0
                result["deleted"][name] = int(deleted or 0)
                result["remaining_eligible"][name] = int(con.execute(
                    f"select count(*) from {table} where {predicate}", params
                ).fetchone()[0] or 0)
            _audit(
                con,
                "auth_cleanup",
                "success",
                actor_type=(actor or {}).get("method") or "maintenance",
                actor_id=(actor or {}).get("id"),
                detail={"action": "auth_state_cleanup", "authorization_method": (actor or {}).get("method") or "local_maintenance", "deleted": result["deleted"]},
            )
            con.commit()
    except sqlite3.OperationalError as exc:
        result.update({"ok": False, "deferred": True, "error": "database_busy" if "lock" in str(exc).lower() else "database_error"})
    result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def create_recovery_token(db_path, *, username=None, created_by="local_cli", ttl_seconds=RECOVERY_TTL_SECONDS):
    now = time.time()
    with _connect(db_path, operation="auth_recovery_create") as con:
        ensure_auth_schema(con)
        if username:
            row = con.execute("select id,username from auth_users where lower(username)=lower(?) and enabled=1", (username,)).fetchone()
        else:
            row = con.execute("select id,username from auth_users where enabled=1 order by created_at limit 1").fetchone()
        if not row:
            raise AuthError("admin_not_found", status=404)
        token = _token(RECOVERY_PREFIX)
        token_id = uuid.uuid4().hex
        expires_at = now + max(60, min(60 * 60, int(ttl_seconds or RECOVERY_TTL_SECONDS)))
        con.execute("insert into auth_recovery_tokens(id,user_id,token_hash,created_at,expires_at,created_by,raw_json) values(?,?,?,?,?,?,?)", (token_id, row["id"], _digest(token), now, expires_at, str(created_by)[:128], _json_text({"one_time": True})))
        _audit(con, "recovery_token_create", "success", actor_type="local_cli", actor_id=str(created_by), username=row["username"], detail={"recovery_id": token_id, "expires_at": expires_at})
    return {"ok": True, "recovery": {"id": token_id, "token": token, "expires_at": expires_at, "expires_at_iso": utc_stamp(expires_at), "one_time": True}}


def reset_password_with_recovery(db_path, token, new_password, *, remote_addr=None, user_agent=None):
    new_hash = password_hash(new_password, policy=password_policy(db_path))
    now = time.time()
    with _connect(db_path, operation="auth_recovery_reset") as con:
        ensure_auth_schema(con)
        row = con.execute("select r.id,r.user_id,u.username from auth_recovery_tokens r join auth_users u on u.id=r.user_id where r.token_hash=? and r.used_at is null and r.expires_at>?", (_digest(token), now)).fetchone()
        if not row:
            _audit(con, "recovery_reset", "failure", remote_addr=remote_addr, user_agent=user_agent)
            con.commit()
            raise AuthError("invalid_recovery_token", status=403)
        con.execute("update auth_users set password_hash=?,password_changed_at=?,updated_at=? where id=?", (new_hash, now, now, row["user_id"]))
        con.execute("update auth_recovery_tokens set used_at=? where id=?", (now, row["id"]))
        con.execute("update auth_sessions set revoked_at=?,revoked_reason='password_recovery' where user_id=? and revoked_at is null", (now, row["user_id"]))
        _audit(con, "recovery_reset", "success", actor_type="recovery", actor_id=row["user_id"], username=row["username"], remote_addr=remote_addr, user_agent=user_agent)
    return {"ok": True, "password_reset": True, "sessions_revoked": True}


def recent_audit_events(db_path, limit=100):
    limit = max(1, min(500, int(limit or 100)))
    with _connect(db_path, operation="auth_audit_list") as con:
        ensure_auth_schema(con)
        rows = con.execute("select id,event_type,outcome,actor_type,actor_id,username_hint,remote_addr,created_at,detail_json from auth_audit_events order by created_at desc limit ?", (limit,)).fetchall()
    return [{**dict(row), "detail": _json(row["detail_json"], {})} for row in rows]


def sanitize_auth_database_copy(db_path):
    """Return a credential-safety report for a SQLite backup/export.

    Audits exactly the file it is given -- backup copies live at temporary
    paths, so this must never redirect to a sibling auth store.
    """
    with _connect_exact(db_path, readonly=True, operation="auth_backup_audit") as con:
        tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        report = {"ok": True, "plaintext_passwords": 0, "plaintext_api_keys": 0, "plaintext_sessions": 0, "plaintext_recovery_tokens": 0}
        if "auth_users" in tables:
            for row in con.execute("select password_hash from auth_users"):
                if not str(row[0] or "").startswith(f"{PASSWORD_ALGORITHM}$"):
                    report["plaintext_passwords"] += 1
        if "api_keys" in tables:
            for row in con.execute("select key_hash from api_keys"):
                if not re.fullmatch(r"[0-9a-f]{64}", str(row[0] or "")):
                    report["plaintext_api_keys"] += 1
        if "auth_sessions" in tables:
            for row in con.execute("select token_hash from auth_sessions"):
                if not re.fullmatch(r"[0-9a-f]{64}", str(row[0] or "")):
                    report["plaintext_sessions"] += 1
        if "auth_recovery_tokens" in tables:
            for row in con.execute("select token_hash from auth_recovery_tokens"):
                if not re.fullmatch(r"[0-9a-f]{64}", str(row[0] or "")):
                    report["plaintext_recovery_tokens"] += 1
    report["ok"] = not any(value for key, value in report.items() if key != "ok")
    return report
