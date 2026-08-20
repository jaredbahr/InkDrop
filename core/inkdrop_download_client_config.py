#!/usr/bin/env python3
"""Durable, redacted download-client instance configuration.

This module is storage-only. It deliberately does not choose clients, call
download-client APIs, or alter the legacy singleton runtime behavior.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from core import inkdrop_secret_store


CONTRACT_SCHEMA = "inkdrop.download_client_instances.v1"
INSTANCE_SCHEMA_VERSION = 1
MAX_PATH_MAPPINGS = 32
MEDIA_KEYS = {"comics", "manga", "ebooks"}
SECRET_KEY_PATTERN = re.compile(r"(?:password|passphrase|api_?key|token|secret|cookie)", re.I)
INSTANCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,95}$")
TERMINAL_TASK_STATES = {"completed", "failed", "cancelled", "canceled", "removed", "verified", "imported"}


DEFAULT_TYPE_SCHEMAS = {
    # qBittorrent 5.2.0 added API keys, which authenticate without a Web UI
    # user at all. Requiring username+password here blocked those users from
    # saving a client before any network call happened, so the requirement is
    # "one of password or api_key" with username paired to password below.
    "qbittorrent": {"implemented": True, "protocols": ["torrent"], "required_fields": ["base_url"], "secret_fields": ["password", "api_key"], "required_secret_fields_any": ["password", "api_key"]},
    "sabnzbd": {"implemented": True, "protocols": ["usenet"], "required_fields": ["base_url"], "secret_fields": ["api_key"], "required_secret_fields": ["api_key"]},
    "slskd": {"implemented": True, "protocols": ["soulseek"], "required_fields": ["base_url"], "secret_fields": ["api_key"], "required_secret_fields": ["api_key"]},
    "transmission": {"implemented": True, "protocols": ["torrent"], "required_fields": ["base_url"], "secret_fields": ["password"], "required_secret_fields": []},
    "deluge": {"implemented": True, "protocols": ["torrent"], "required_fields": ["base_url"], "secret_fields": ["password"], "required_secret_fields": ["password"]},
    "nzbget": {"implemented": True, "protocols": ["usenet"], "required_fields": ["base_url"], "secret_fields": ["password", "api_key"], "required_secret_fields_any": ["password", "api_key"]},
    "utorrent": {"implemented": True, "protocols": ["torrent"], "required_fields": ["base_url", "username"], "secret_fields": ["password"], "required_secret_fields": ["password"]},
    "rtorrent": {"implemented": True, "protocols": ["torrent"], "required_fields": ["base_url"], "secret_fields": ["password"], "required_secret_fields": []},
}


LEGACY_MIGRATION_KEY_PREFIX = "legacy_provider_card_to_instance.v1:"

# What each legacy provider card kept, and where it lives on an instance. Only
# the three clients that ever had a forced card are listed; the other five
# client types were always add-only, so they have nothing to carry across.
LEGACY_CLIENT_FIELDS = {
    "qbittorrent": {
        "secrets": ("password", "api_key"),
        "username_field": "username",
        "categories": {"comics": "comics_category", "manga": "manga_category", "ebooks": "ebooks_category"},
        "download_paths": {"comics": "comics_save_path", "manga": "manga_save_path", "ebooks": "ebooks_save_path"},
        "carry": ("torrent_cleanup_policy", "verify_tls"),
    },
    "sabnzbd": {
        "secrets": ("api_key",),
        "categories": {"comics": "comics_category"},
        "carry": (
            "failure_categories", "remove_completed_downloads", "remove_failed_downloads",
            "completed_history_min_age_hours", "failed_history_min_age_hours", "sab_history_limit",
            "max_failed_history_delete", "max_completed_history_delete", "verify_tls",
        ),
    },
    "slskd": {
        "secrets": ("api_key",),
        "download_path_field": "download_root",
        "download_paths": {"comics": "download_root"},
        "carry": (
            "download_root", "incomplete_root", "max_total", "max_per_series", "wait_seconds",
            "max_queries", "auto_grab_max", "probe_budget_seconds", "cooldown_hours",
            "max_active_per_user", "preferred_exact_min_bytes", "delete_search_history",
            "search_history_keep", "search_history_max_delete", "search_history_min_age_minutes",
            "verify_tls",
        ),
    },
}


SCHEMA_SQL = """
create table if not exists download_client_instances (
    id text primary key,
    name text not null,
    name_key text not null,
    client_type text not null,
    enabled integer not null default 0,
    priority integer not null default 100,
    base_url text,
    username text,
    category text,
    download_path text,
    categories_json text not null default '{}',
    download_paths_json text not null default '{}',
    path_mappings_json text not null default '[]',
    settings_json text not null default '{}',
    secret_refs_json text not null default '{}',
    auth_method text not null default '',
    revision integer not null default 1,
    source text not null default 'user',
    created_at real not null,
    updated_at real not null,
    deleted_at real,
    check(priority between 1 and 1000),
    check(revision >= 1)
);
create unique index if not exists idx_download_client_instances_active_name
    on download_client_instances(name_key) where deleted_at is null;
create index if not exists idx_download_client_instances_type_enabled_priority
    on download_client_instances(client_type, enabled, priority, created_at, id);
create table if not exists download_client_provider_mappings (
    provider_id text not null,
    protocol text not null,
    media_type text not null default '',
    client_instance_id text not null,
    created_at real not null,
    updated_at real not null,
    primary key(provider_id, protocol, media_type),
    foreign key(client_instance_id) references download_client_instances(id) on delete restrict
);
create index if not exists idx_download_client_provider_mappings_instance
    on download_client_provider_mappings(client_instance_id, protocol, provider_id);
create table if not exists download_client_instance_migrations (
    migration_key text primary key,
    status text not null,
    detail_json text not null default '{}',
    created_at real not null,
    updated_at real not null
);
"""


def ensure_schema(con):
    con.executescript(SCHEMA_SQL)
    # "create table if not exists" above only covers a fresh database --
    # an install from before auth_method existed needs the column added to
    # its already-created table. Existing rows default to '' (unknown),
    # which the exclusivity enforcement below deliberately leaves alone
    # rather than guessing which of an already-dual-credential row's secrets
    # to delete; see PASS32-CONFIG-P2-01.
    columns = {row[1] for row in con.execute("pragma table_info(download_client_instances)")}
    if "auth_method" not in columns:
        con.execute("alter table download_client_instances add column auth_method text not null default ''")
    return True


def _connect(db_path):
    con = sqlite3.connect(Path(db_path), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("pragma foreign_keys=on")
    ensure_schema(con)
    return con


@contextlib.contextmanager
def _connection(db_path):
    con = _connect(db_path)
    try:
        yield con
    finally:
        con.close()


def _json(value, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _name_key(value):
    return " ".join(str(value or "").strip().casefold().split())


def _instance_id(value=None):
    candidate = str(value or "").strip().lower()
    if not candidate:
        candidate = f"dc_{uuid.uuid4().hex}"
    if not INSTANCE_ID_PATTERN.fullmatch(candidate):
        raise ValueError("download-client instance id must be 3-96 lowercase letters, numbers, dots, underscores, or dashes")
    return candidate


def _schema_for(client_type, schema_resolver=None):
    if schema_resolver is not None:
        resolved = schema_resolver(client_type)
        if resolved is not None:
            return dict(resolved)
    return dict(DEFAULT_TYPE_SCHEMAS.get(client_type) or {})


def _exclusive_secret_group(schema):
    """Secret fields that are alternatives, not a set (password vs api_key).

    required_secret_fields_any already means "at least one of these" -- when
    there's more than one field in that group, at most one of them may ever
    be configured at a time too, or auth silently picks whichever one a
    consumer's code happens to check first (PASS32-CONFIG-P2-01).
    """
    group = list((schema or {}).get("required_secret_fields_any") or [])
    return group if len(group) > 1 else []


def exclusive_secret_group_for_type(client_type, schema_resolver=None):
    """Public wrapper for callers outside this module (routing's own secret
    resolution path materialize_instance_settings() must apply the same
    auth_method filtering adapter_settings() does, not just the Test-button
    path -- see PASS32-CONFIG-P2-01)."""
    return _exclusive_secret_group(_schema_for(client_type, schema_resolver=schema_resolver))


def _validate_url(value):
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if len(text) > 2048:
        raise ValueError("base_url exceeds 2048 characters")
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise ValueError("base_url is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must use http or https and include a host")
    if parsed.username or parsed.password:
        raise ValueError("base_url cannot contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url cannot contain query parameters or fragments")
    return text


def _clean_scalar(value, *, maximum=256):
    text = str(value if value is not None else "").strip()
    if len(text) > maximum:
        raise ValueError(f"value exceeds {maximum} characters")
    return text


def _media_map(value, field_name):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    out = {}
    for key, item in value.items():
        media = str(key or "").strip().lower()
        if media not in MEDIA_KEYS:
            raise ValueError(f"{field_name} contains unsupported media type: {media}")
        out[media] = _clean_scalar(item, maximum=1024)
    return out


def _absolute_path(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "\x00" in text:
        raise ValueError("path cannot contain NUL bytes")
    parts = PureWindowsPath(text).parts if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\") else PurePosixPath(text).parts
    if not (text.startswith(("/", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", text)):
        raise ValueError("path mappings require absolute paths")
    if ".." in parts:
        raise ValueError("path mappings cannot contain parent traversal")
    return text.rstrip("/\\") or text


def normalize_path_mappings(value, *, path_validator=None):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("path_mappings must be a list")
    if len(value) > MAX_PATH_MAPPINGS:
        raise ValueError(f"path_mappings cannot exceed {MAX_PATH_MAPPINGS} rows")
    out = []
    seen = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("path mapping rows must be objects")
        remote = _absolute_path(row.get("remote_path") or row.get("remote"))
        local = _absolute_path(row.get("local_path") or row.get("local"))
        if not remote or not local:
            raise ValueError("path mapping remote_path and local_path are required")
        key = remote.replace("\\", "/").casefold()
        if key in seen:
            raise ValueError("duplicate remote path mapping")
        seen.add(key)
        if path_validator is not None:
            path_validator(remote, local)
        out.append({"remote_path": remote, "local_path": local})
    return out


def _nonsecret_settings(value):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("settings must be an object")

    def clean(item, path="settings"):
        if isinstance(item, dict):
            out = {}
            for key, nested in item.items():
                field = str(key or "").strip()
                if not field:
                    raise ValueError(f"{path} contains a blank key")
                if SECRET_KEY_PATTERN.search(field):
                    raise ValueError(f"{path}.{field} is secret-like and must use the secrets object")
                out[field] = clean(nested, f"{path}.{field}")
            return out
        if isinstance(item, list):
            if len(item) > 256:
                raise ValueError(f"{path} exceeds 256 items")
            return [clean(nested, path) for nested in item]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return _clean_scalar(item, maximum=4096)

    return clean(value)


def _normalize_provider_mappings(value, protocols):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("provider_mappings must be a list")
    if len(value) > 128:
        raise ValueError("provider_mappings cannot exceed 128 rows")
    out = []
    seen = set()
    allowed = {str(item).lower() for item in protocols or []}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("provider mapping rows must be objects")
        provider_id = _clean_scalar(row.get("provider_id"), maximum=128).lower()
        protocol = _clean_scalar(row.get("protocol"), maximum=32).lower()
        media_type = _clean_scalar(row.get("media_type"), maximum=32).lower()
        if not provider_id or not protocol:
            raise ValueError("provider_id and protocol are required")
        if allowed and protocol not in allowed:
            raise ValueError(f"client type does not support protocol: {protocol}")
        if media_type and media_type not in MEDIA_KEYS:
            raise ValueError(f"unsupported mapping media type: {media_type}")
        key = (provider_id, protocol, media_type)
        if key in seen:
            raise ValueError("duplicate provider mapping")
        seen.add(key)
        out.append({"provider_id": provider_id, "protocol": protocol, "media_type": media_type})
    return out


def _normalize_candidate(payload, current=None, *, schema_resolver=None, path_validator=None):
    payload = dict(payload or {})
    current = dict(current or {})
    if current and "id" in payload and _instance_id(payload.get("id")) != current.get("id"):
        raise ValueError("download-client instance id is immutable")
    client_type = _clean_scalar(payload.get("client_type", current.get("client_type")), maximum=64).lower()
    if not client_type:
        raise ValueError("client_type is required")
    schema = _schema_for(client_type, schema_resolver=schema_resolver)
    name = _clean_scalar(payload.get("name", current.get("name")), maximum=128)
    if not name:
        raise ValueError("name is required")
    # enabled arms a live download client, so it takes real JSON booleans
    # only: bool("false") is True in Python, and the audit's fixture proved a
    # Transmission client submitted as "enabled":"false" persisted enabled
    # (PASS13-API-P1-01; same contract the web-layer gates now enforce). The
    # raised ValueError surfaces as the endpoint's plain 400.
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise ValueError("enabled must be JSON true or false")
        enabled = payload["enabled"]
    else:
        enabled = bool(current.get("enabled", False))
    try:
        priority = int(payload.get("priority", current.get("priority", 100)))
    except (TypeError, ValueError):
        raise ValueError("priority must be an integer") from None
    if priority < 1 or priority > 1000:
        raise ValueError("priority must be between 1 and 1000")
    candidate = {
        "id": _instance_id(current.get("id") or payload.get("id")),
        "name": name,
        "name_key": _name_key(name),
        "client_type": client_type,
        "enabled": enabled,
        "priority": priority,
        "base_url": _validate_url(payload.get("base_url", current.get("base_url"))),
        "username": _clean_scalar(payload.get("username", current.get("username")), maximum=256),
        "category": _clean_scalar(payload.get("category", current.get("category")), maximum=128),
        "download_path": _clean_scalar(payload.get("download_path", current.get("download_path")), maximum=1024),
        "categories": _media_map(payload.get("categories", current.get("categories")), "categories"),
        "download_paths": _media_map(payload.get("download_paths", current.get("download_paths")), "download_paths"),
        "path_mappings": normalize_path_mappings(payload.get("path_mappings", current.get("path_mappings")), path_validator=path_validator),
        "settings": _nonsecret_settings(payload.get("settings", current.get("settings"))),
        "source": _clean_scalar(current.get("source") or payload.get("source") or "user", maximum=32),
        "auth_method": _clean_scalar(payload.get("auth_method", current.get("auth_method")), maximum=32).lower(),
        "schema": schema,
    }
    group = _exclusive_secret_group(schema)
    if candidate["auth_method"] and group and candidate["auth_method"] not in group:
        raise ValueError(f"auth_method must be one of {', '.join(group)}")
    if candidate["auth_method"] and not group:
        raise ValueError(f"{client_type} does not support choosing an auth_method")
    return candidate


def _validate_ready(candidate, configured_secrets):
    if not candidate["enabled"]:
        return
    schema = candidate.get("schema") or {}
    if not schema or schema.get("implemented") is not True:
        raise ValueError("client type is not registry-ready and cannot be enabled")
    for field in schema.get("required_fields") or []:
        if not candidate.get(field):
            raise ValueError(f"{field} is required when the client is enabled")
    for field in schema.get("required_secret_fields") or []:
        if not configured_secrets.get(field):
            raise ValueError(f"{field} is required when the client is enabled")
    any_fields = list(schema.get("required_secret_fields_any") or [])
    if any_fields and not any(configured_secrets.get(field) for field in any_fields):
        raise ValueError(f"one of {', '.join(any_fields)} is required when the client is enabled")
    if candidate["client_type"] == "qbittorrent":
        # An API key stands alone. A password does not: qBittorrent's login
        # endpoint takes both fields, so a password with no username can only
        # ever produce a confusing "Fails." from the far end.
        if configured_secrets.get("password") and not candidate.get("username"):
            raise ValueError("qBittorrent needs a username with the password, or an API key instead")
    if candidate["client_type"] == "transmission":
        has_username = bool(candidate.get("username"))
        has_password = bool(configured_secrets.get("password"))
        if has_username != has_password:
            raise ValueError("Transmission username and password must be configured together")


def _secret_plan(payload, existing_refs, schema):
    existing = dict(existing_refs or {})
    supplied_value = payload.get("secrets")
    if supplied_value not in (None, "") and not isinstance(supplied_value, dict):
        raise ValueError("secrets must be an object")
    supplied = supplied_value if isinstance(supplied_value, dict) else {}
    clear_value = payload.get("clear_secret_fields")
    if clear_value not in (None, "") and not isinstance(clear_value, list):
        raise ValueError("clear_secret_fields must be a list")
    clear = {str(item or "").strip() for item in (clear_value or []) if str(item or "").strip()}
    allowed = {str(item) for item in (schema.get("secret_fields") or [])}
    unknown = (set(supplied) | clear) - allowed
    if unknown:
        raise ValueError(f"unsupported secret fields: {', '.join(sorted(unknown))}")
    return existing, supplied, clear


def _enforce_exclusive_secret_group(candidate, new_refs, supplied, old_refs_to_gc):
    """Retire the other credential the moment one in the group is chosen.

    Supplying a new value for one field of an exclusive group (password vs
    api_key) picks that field as auth_method and, in the SAME transaction,
    clears any other group field's ref -- so a rotation always takes effect
    instead of leaving both stored and letting whichever consumer code
    happens to check first decide, silently (PASS32-CONFIG-P2-01). A row
    that already had auth_method set (from a prior save) keeps it when
    neither field is being touched now. A pre-existing row where neither
    ever happened (auth_method still '') is left exactly as it was --
    nothing here deletes a stored secret nobody asked to replace.
    """
    group = _exclusive_secret_group(candidate["schema"])
    if not group:
        return
    supplied_in_group = [field for field in group if str(supplied.get(field) or "").strip()]
    if len(supplied_in_group) > 1:
        raise ValueError(f"only one of {', '.join(group)} can be configured at a time")
    effective_method = supplied_in_group[0] if supplied_in_group else candidate["auth_method"]
    if not effective_method:
        return
    for field in group:
        if field != effective_method and new_refs.get(field):
            old_refs_to_gc.append(new_refs[field])
            new_refs.pop(field, None)
    candidate["auth_method"] = effective_method


def _row_private(row):
    if row is None:
        return None
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    item["categories"] = _json(item.pop("categories_json", "{}"), {})
    item["download_paths"] = _json(item.pop("download_paths_json", "{}"), {})
    item["path_mappings"] = _json(item.pop("path_mappings_json", "[]"), [])
    item["settings"] = _json(item.pop("settings_json", "{}"), {})
    item["secret_refs"] = _json(item.pop("secret_refs_json", "{}"), {})
    return item


def _mappings(con, instance_id):
    return [
        {"provider_id": row["provider_id"], "protocol": row["protocol"], "media_type": row["media_type"] or ""}
        for row in con.execute(
            "select provider_id,protocol,media_type from download_client_provider_mappings where client_instance_id=? order by provider_id,protocol,media_type",
            (instance_id,),
        )
    ]


def _secret_field_status(reference, secret_root=None):
    if not reference:
        return {"configured": False}
    try:
        inkdrop_secret_store.read_secret(reference, root=secret_root)
    except ValueError:
        # The reference row survives a state-DB restore, but the secret file
        # itself is intentionally excluded from backups -- without this check
        # a restored instance would report configured=true with no working
        # credential underneath it.
        return {"configured": False, "reason": "secret_unresolvable"}
    return {"configured": True}


def _public(item, mappings=None, secret_root=None):
    if not item:
        return None
    secret_refs = item.get("secret_refs") if isinstance(item.get("secret_refs"), dict) else {}
    return {
        "schema": CONTRACT_SCHEMA,
        "id": item.get("id"),
        "name": item.get("name"),
        "client_type": item.get("client_type"),
        "enabled": bool(item.get("enabled")),
        "priority": int(item.get("priority") or 100),
        "base_url": item.get("base_url") or "",
        "username": item.get("username") or "",
        "category": item.get("category") or "",
        "download_path": item.get("download_path") or "",
        "categories": dict(item.get("categories") or {}),
        "download_paths": dict(item.get("download_paths") or {}),
        "path_mappings": list(item.get("path_mappings") or []),
        "settings": dict(item.get("settings") or {}),
        "auth_method": item.get("auth_method") or "",
        "secret_fields": {
            key: _secret_field_status(reference, secret_root=secret_root)
            for key, reference in sorted(secret_refs.items())
        },
        "provider_mappings": list(mappings or []),
        "revision": int(item.get("revision") or 1),
        "source": item.get("source") or "user",
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "deleted_at": item.get("deleted_at"),
    }


def _history(con, event_type, instance_id, message, changed_fields, now, history_writer=None):
    raw = {"download_client_instance_id": instance_id, "changed_fields": sorted(set(changed_fields or [])), "redacted": True}
    if history_writer is not None:
        history_writer(con, event_type, "download_client_instance", instance_id, "download_client_config", message, raw, now)
        return
    table = con.execute("select 1 from sqlite_master where type='table' and name='history_events'").fetchone()
    if table:
        con.execute(
            "insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json) values(?,?,?,?,?,?,?,?)",
            (f"{event_type}-{uuid.uuid4().hex}", "download_client_instance", instance_id, event_type, "download_client_config", message, now, _dump(raw)),
        )


def _write_mappings(con, instance_id, mappings, now):
    con.execute("delete from download_client_provider_mappings where client_instance_id=?", (instance_id,))
    for row in mappings:
        con.execute(
            "insert into download_client_provider_mappings(provider_id,protocol,media_type,client_instance_id,created_at,updated_at) values(?,?,?,?,?,?)",
            (row["provider_id"], row["protocol"], row.get("media_type") or "", instance_id, now, now),
        )


def _assert_unique_endpoint(con, candidate, exclude_id=None):
    name_row = con.execute(
        "select id from download_client_instances where deleted_at is null and name_key=?",
        (candidate["name_key"],),
    ).fetchone()
    if name_row and (not exclude_id or name_row["id"] != exclude_id):
        raise ValueError("active download-client instance name must be unique")
    if not candidate.get("base_url"):
        return
    rows = con.execute(
        "select id,client_type,base_url,username from download_client_instances where deleted_at is null and client_type=?",
        (candidate["client_type"],),
    )
    for row in rows:
        if exclude_id and row["id"] == exclude_id:
            continue
        if str(row["base_url"] or "").rstrip("/").casefold() == candidate["base_url"].casefold() and str(row["username"] or "").casefold() == candidate["username"].casefold():
            raise ValueError("an active client instance already uses this type, endpoint, and username")


def create_instance(
    db_path,
    payload,
    *,
    secret_root=None,
    schema_resolver=None,
    path_validator=None,
    history_writer=None,
    before_commit=None,
):
    payload = dict(payload or {})
    candidate = _normalize_candidate(payload, schema_resolver=schema_resolver, path_validator=path_validator)
    existing_refs, supplied, clear = _secret_plan(payload, {}, candidate["schema"])
    if clear:
        raise ValueError("cannot clear an unset secret while creating a client")
    new_refs = dict(existing_refs)
    created_refs = []
    old_refs_to_gc = []
    try:
        for field, value in supplied.items():
            if str(value or "").strip() == "":
                continue
            result = inkdrop_secret_store.write_secret(value, root=secret_root)
            new_refs[field] = result["reference"]
            created_refs.append(result["reference"])
        _enforce_exclusive_secret_group(candidate, new_refs, supplied, old_refs_to_gc)
        _validate_ready(candidate, {field: bool(value) for field, value in new_refs.items()})
        protocols = candidate["schema"].get("protocols") or []
        mappings = _normalize_provider_mappings(payload.get("provider_mappings"), protocols)
        now = time.time()
        with _connection(db_path) as con:
            con.execute("begin immediate")
            _assert_unique_endpoint(con, candidate)
            con.execute(
                """insert into download_client_instances(
                    id,name,name_key,client_type,enabled,priority,base_url,username,category,download_path,
                    categories_json,download_paths_json,path_mappings_json,settings_json,secret_refs_json,
                    auth_method,revision,source,created_at,updated_at,deleted_at
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,null)""",
                (
                    candidate["id"], candidate["name"], candidate["name_key"], candidate["client_type"], int(candidate["enabled"]),
                    candidate["priority"], candidate["base_url"] or None, candidate["username"] or None, candidate["category"] or None,
                    candidate["download_path"] or None, _dump(candidate["categories"]), _dump(candidate["download_paths"]),
                    _dump(candidate["path_mappings"]), _dump(candidate["settings"]), _dump(new_refs), candidate["auth_method"],
                    1, candidate["source"], now, now,
                ),
            )
            _write_mappings(con, candidate["id"], mappings, now)
            changed = [key for key in payload if key != "secrets"]
            changed.extend(f"{key}:secret" for key in supplied if str(supplied.get(key) or "").strip())
            _history(con, "download_client_instance_added", candidate["id"], f"{candidate['name']} download client added", changed, now, history_writer)
            if before_commit is not None:
                before_commit(con)
            con.commit()
            item = _row_private(con.execute("select * from download_client_instances where id=?", (candidate["id"],)).fetchone())
            return _public(item, _mappings(con, candidate["id"]), secret_root=secret_root)
    except Exception as exc:
        for reference in created_refs:
            inkdrop_secret_store.delete_secret(reference, root=secret_root)
        if isinstance(exc, sqlite3.IntegrityError) and "download_client_instances.name_key" in str(exc):
            raise ValueError("active download-client instance name must be unique") from None
        raise


def get_instance(db_path, instance_id, *, include_deleted=False, secret_root=None):
    with _connection(db_path) as con:
        clause = "" if include_deleted else " and deleted_at is null"
        row = con.execute(f"select * from download_client_instances where id=?{clause}", (str(instance_id),)).fetchone()
        item = _row_private(row)
        return _public(item, _mappings(con, item["id"]) if item else [], secret_root=secret_root)


def list_instances(db_path, *, include_deleted=False, secret_root=None):
    with _connection(db_path) as con:
        clause = "" if include_deleted else "where deleted_at is null"
        rows = con.execute(f"select * from download_client_instances {clause} order by priority,name_key,id").fetchall()
        return {
            "schema": CONTRACT_SCHEMA,
            "instances": [_public(_row_private(row), _mappings(con, row["id"]), secret_root=secret_root) for row in rows],
        }


def update_instance(
    db_path,
    instance_id,
    payload,
    *,
    expected_revision,
    secret_root=None,
    schema_resolver=None,
    path_validator=None,
    history_writer=None,
    before_commit=None,
):
    payload = dict(payload or {})
    instance_id = str(instance_id or "").strip().lower()
    with _connection(db_path) as read_con:
        current = _row_private(read_con.execute("select * from download_client_instances where id=? and deleted_at is null", (instance_id,)).fetchone())
        current_mappings = _mappings(read_con, instance_id) if current else []
    if not current:
        raise ValueError("download-client instance not found")
    if int(expected_revision or 0) != int(current["revision"]):
        raise ValueError("download-client instance revision conflict")
    candidate = _normalize_candidate(payload, current, schema_resolver=schema_resolver, path_validator=path_validator)
    existing_refs, supplied, clear = _secret_plan(payload, current.get("secret_refs"), candidate["schema"])
    new_refs = dict(existing_refs)
    created_refs = []
    old_refs_to_gc = []
    try:
        for field, value in supplied.items():
            if str(value or "").strip() == "":
                continue
            result = inkdrop_secret_store.write_secret(value, root=secret_root)
            if new_refs.get(field):
                old_refs_to_gc.append(new_refs[field])
            new_refs[field] = result["reference"]
            created_refs.append(result["reference"])
        for field in clear:
            if new_refs.get(field):
                old_refs_to_gc.append(new_refs[field])
            new_refs.pop(field, None)
        _enforce_exclusive_secret_group(candidate, new_refs, supplied, old_refs_to_gc)
        _validate_ready(candidate, {field: bool(value) for field, value in new_refs.items()})
        protocols = candidate["schema"].get("protocols") or []
        mappings = _normalize_provider_mappings(payload.get("provider_mappings", current_mappings), protocols)
        now = time.time()
        with _connection(db_path) as con:
            con.execute("begin immediate")
            live_revision = con.execute("select revision from download_client_instances where id=? and deleted_at is null", (instance_id,)).fetchone()
            if not live_revision or int(live_revision["revision"]) != int(expected_revision):
                raise ValueError("download-client instance revision conflict")
            _assert_unique_endpoint(con, candidate, exclude_id=instance_id)
            next_revision = int(expected_revision) + 1
            con.execute(
                """update download_client_instances set
                    name=?,name_key=?,client_type=?,enabled=?,priority=?,base_url=?,username=?,category=?,download_path=?,
                    categories_json=?,download_paths_json=?,path_mappings_json=?,settings_json=?,secret_refs_json=?,
                    auth_method=?,revision=?,source='user',updated_at=? where id=? and revision=? and deleted_at is null""",
                (
                    candidate["name"], candidate["name_key"], candidate["client_type"], int(candidate["enabled"]), candidate["priority"],
                    candidate["base_url"] or None, candidate["username"] or None, candidate["category"] or None, candidate["download_path"] or None,
                    _dump(candidate["categories"]), _dump(candidate["download_paths"]), _dump(candidate["path_mappings"]),
                    _dump(candidate["settings"]), _dump(new_refs), candidate["auth_method"], next_revision, now, instance_id, int(expected_revision),
                ),
            )
            if con.total_changes < 1:
                raise ValueError("download-client instance revision conflict")
            _write_mappings(con, instance_id, mappings, now)
            changed = [key for key in payload if key not in {"secrets"}]
            changed.extend(f"{key}:secret" for key in supplied if str(supplied.get(key) or "").strip())
            changed.extend(f"{key}:secret_cleared" for key in clear)
            _history(con, "download_client_instance_updated", instance_id, f"{candidate['name']} download client updated", changed, now, history_writer)
            if before_commit is not None:
                before_commit(con)
            con.commit()
            item = _row_private(con.execute("select * from download_client_instances where id=?", (instance_id,)).fetchone())
            response = _public(item, _mappings(con, instance_id), secret_root=secret_root)
        for reference in set(old_refs_to_gc):
            inkdrop_secret_store.delete_secret(reference, root=secret_root)
        return response
    except Exception as exc:
        for reference in created_refs:
            inkdrop_secret_store.delete_secret(reference, root=secret_root)
        if isinstance(exc, sqlite3.IntegrityError) and "download_client_instances.name_key" in str(exc):
            raise ValueError("active download-client instance name must be unique") from None
        raise


def soft_delete_instance(db_path, instance_id, *, expected_revision, secret_root=None, history_writer=None):
    instance_id = str(instance_id or "").strip().lower()
    now = time.time()
    refs_to_gc = []
    with _connection(db_path) as con:
        con.execute("begin immediate")
        row = con.execute("select * from download_client_instances where id=? and deleted_at is null", (instance_id,)).fetchone()
        if not row:
            raise ValueError("download-client instance not found")
        if int(row["revision"]) != int(expected_revision or 0):
            raise ValueError("download-client instance revision conflict")
        mapping_count = con.execute("select count(*) from download_client_provider_mappings where client_instance_id=?", (instance_id,)).fetchone()[0]
        if mapping_count:
            raise ValueError("download-client instance is still referenced by provider mappings")
        columns = {item[1] for item in con.execute("pragma table_info(download_tasks)")}
        if "download_client_instance_id" in columns:
            placeholders = ",".join("?" for _ in TERMINAL_TASK_STATES)
            active = con.execute(
                f"select count(*) from download_tasks where download_client_instance_id=? and lower(coalesce(state,'')) not in ({placeholders})",
                (instance_id, *sorted(TERMINAL_TASK_STATES)),
            ).fetchone()[0]
            if active:
                raise ValueError("download-client instance still has active download tasks")
        refs_to_gc = list(_json(row["secret_refs_json"], {}).values())
        con.execute(
            "update download_client_instances set enabled=0,secret_refs_json='{}',deleted_at=?,updated_at=?,revision=revision+1 where id=? and revision=?",
            (now, now, instance_id, int(expected_revision)),
        )
        _history(con, "download_client_instance_deleted", instance_id, f"{row['name']} download client deleted", ["enabled", "deleted_at"], now, history_writer)
        con.commit()
        item = _row_private(con.execute("select * from download_client_instances where id=?", (instance_id,)).fetchone())
        response = _public(item, [])
    for reference in set(refs_to_gc):
        inkdrop_secret_store.delete_secret(reference, root=secret_root)
    return response


def active_secret_references(db_path, *, include_deleted=True):
    with _connection(db_path) as con:
        clause = "" if include_deleted else "where deleted_at is null"
        refs = set()
        for row in con.execute(f"select secret_refs_json from download_client_instances {clause}"):
            refs.update(value for value in _json(row["secret_refs_json"], {}).values() if value)
        return sorted(refs)


def adapter_settings(db_path, instance_id, *, secret_root=None):
    """Resolve one instance for an adapter call; callers must never serialize it."""
    with _connection(db_path) as con:
        item = _row_private(con.execute(
            "select * from download_client_instances where id=? and deleted_at is null",
            (str(instance_id or "").strip().lower(),),
        ).fetchone())
    if not item:
        raise ValueError("download-client instance not found")
    resolved = {
        "id": item["id"], "client_type": item["client_type"], "enabled": item["enabled"],
        "base_url": item.get("base_url") or "", "username": item.get("username") or "",
        "category": item.get("category") or "", "download_path": item.get("download_path") or "",
        "path_mappings": list(item.get("path_mappings") or []), **dict(item.get("settings") or {}),
    }
    secret_refs = dict(item.get("secret_refs") or {})
    auth_method = str(item.get("auth_method") or "").strip()
    if auth_method:
        # A row saved before this fix (or otherwise never explicitly given
        # an auth_method) still gets every stored secret resolved below,
        # unchanged -- but once a method is chosen, the OTHER credential
        # must never even reach the adapter, or password-vs-api_key
        # precedence is right back to being an implicit per-caller guess
        # (PASS32-CONFIG-P2-01).
        group = _exclusive_secret_group(_schema_for(item["client_type"]))
        if auth_method in group:
            secret_refs = {field: ref for field, ref in secret_refs.items() if field not in group or field == auth_method}
    for field, reference in secret_refs.items():
        resolved[field] = inkdrop_secret_store.read_secret(reference, root=secret_root)
    resolved["auth_method"] = auth_method
    return resolved


def cleanup_orphan_secrets(db_path, *, secret_root=None, max_delete=50, min_age_seconds=3600, now=None):
    return inkdrop_secret_store.cleanup_orphans(
        active_secret_references(db_path, include_deleted=True),
        root=secret_root,
        max_delete=max_delete,
        min_age_seconds=min_age_seconds,
        now=now,
    )


def _legacy_rows(db_path, client_types):
    """Read the legacy provider-card rows for the given client types."""
    path = Path(db_path)
    if not path.exists():
        return {}
    uri = f"file:{path}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=3.0)) as con:
        con.row_factory = sqlite3.Row
        table = con.execute("select 1 from sqlite_master where type='table' and name='provider_configs'").fetchone()
        if not table:
            return {}
        rows = {}
        for client_type in client_types:
            row = con.execute(
                "select id,display_name,enabled,base_url,settings_json from provider_configs where id=?",
                (client_type,),
            ).fetchone()
            if row is not None:
                rows[client_type] = dict(row)
        return rows


def _migration_states(con, client_types):
    states = {}
    for client_type in client_types:
        row = con.execute(
            "select status from download_client_instance_migrations where migration_key=?",
            (f"{LEGACY_MIGRATION_KEY_PREFIX}{client_type}",),
        ).fetchone()
        if row is not None:
            states[client_type] = str(row["status"] or "")
    return states


def _record_migration(db_path, client_type, status, detail):
    now = time.time()
    with _connection(db_path) as con:
        con.execute(
            """insert into download_client_instance_migrations(migration_key,status,detail_json,created_at,updated_at)
               values(?,?,?,?,?)
               on conflict(migration_key) do update set
                   status=excluded.status, detail_json=excluded.detail_json, updated_at=excluded.updated_at""",
            (f"{LEGACY_MIGRATION_KEY_PREFIX}{client_type}", status, _dump(detail or {}), now, now),
        )
        con.commit()


def _legacy_payload(client_type, row):
    """Build a create_instance() payload from one legacy provider-card row.

    Returns None when the card holds nothing worth carrying across -- a
    never-configured card is exactly the forced default this migration exists
    to stop showing, so materializing it would recreate the confusion.
    """
    plan = LEGACY_CLIENT_FIELDS.get(client_type) or {}
    settings = _json(row.get("settings_json"), {})
    base_url = str(row.get("base_url") or settings.get("base_url") or settings.get("host") or "").strip()
    secrets = {}
    for field in plan.get("secrets") or ():
        value = str(settings.get(field) or "").strip()
        if value:
            secrets[field] = value
    if not base_url and not secrets:
        return None
    payload = {
        "name": str(row.get("display_name") or client_type).strip() or client_type,
        "client_type": client_type,
        # A legacy card could be marked enabled with nothing behind it (SLSKD's
        # card was enabled purely because a script file shipped in the image).
        # Enablement is re-derived from what actually got carried across, and
        # _validate_ready() still has the final say below.
        "enabled": bool(row.get("enabled")) and bool(base_url) and bool(secrets),
        "base_url": base_url,
        "source": "legacy_provider_config",
    }
    username_field = plan.get("username_field")
    if username_field:
        payload["username"] = str(settings.get(username_field) or "").strip()
    if secrets:
        payload["secrets"] = secrets
    categories = {}
    for media, field in (plan.get("categories") or {}).items():
        value = str(settings.get(field) or "").strip()
        if value:
            categories[media] = value
    if categories:
        payload["categories"] = categories
    download_paths = {}
    for media, field in (plan.get("download_paths") or {}).items():
        value = str(settings.get(field) or "").strip()
        if value:
            download_paths[media] = value
    if download_paths:
        payload["download_paths"] = download_paths
    default_path = str(settings.get(plan.get("download_path_field") or "") or "").strip()
    if default_path:
        payload["download_path"] = default_path
    carried = {}
    for field in plan.get("carry") or ():
        if field in settings and settings[field] not in (None, ""):
            carried[field] = settings[field]
    if carried:
        payload["settings"] = carried
    mappings = settings.get("path_mappings") or settings.get("remote_path_mappings")
    if isinstance(mappings, list) and mappings:
        try:
            payload["path_mappings"] = normalize_path_mappings(mappings)
        except ValueError:
            # A malformed legacy mapping must not cost the user the whole
            # connection; the rest of the config still migrates.
            payload["path_mappings"] = []
    return payload


def materialize_legacy_instances(db_path, *, secret_root=None, history_writer=None, client_types=None):
    """Carry configured legacy provider-card clients into real instances, once.

    The Download Clients page used to seed a fixed card per client type whether
    or not the user ran that client, so "SLSKD is enabled" and "I have an SLSKD
    client" looked identical while meaning different things. The page now lists
    only instances the user added, which makes those legacy cards the one thing
    that must not be dropped: they are live config, still read by
    ``load_qbit_settings()`` and friends.

    So this copies each configured card into the instance model instead of
    deleting anything. The legacy row is left exactly as it was -- the adapters'
    fallback path keeps working untouched, and a failed migration can be retried.
    Secrets move out of ``settings_json`` plaintext into the secret store on the
    way across.
    """
    types = [str(value).lower() for value in (client_types or LEGACY_CLIENT_FIELDS)]
    result = {"schema": CONTRACT_SCHEMA, "migrated": [], "skipped": []}
    rows = _legacy_rows(db_path, types)
    if not rows:
        return result
    with _connection(db_path) as con:
        states = _migration_states(con, types)
        existing_types = {
            str(row["client_type"] or "").lower()
            for row in con.execute("select client_type from download_client_instances where deleted_at is null")
        }
    for client_type in types:
        row = rows.get(client_type)
        if row is None:
            continue
        if states.get(client_type) == "completed":
            # Already carried across. Never redo it -- the user may since have
            # deliberately deleted or renamed the instance.
            continue
        if client_type in existing_types:
            result["skipped"].append({"client_type": client_type, "reason": "instance_already_exists"})
            _record_migration(db_path, client_type, "completed", {"reason": "instance_already_exists"})
            continue
        payload = _legacy_payload(client_type, row)
        if payload is None:
            # Nothing configured on the card. Leave it pending rather than
            # completed: an existing install whose card is still visible can be
            # filled in later, and this should pick it up when it is.
            result["skipped"].append({"client_type": client_type, "reason": "not_configured"})
            _record_migration(db_path, client_type, "pending", {"reason": "not_configured"})
            continue
        created = None
        last_error = ""
        for attempt_enabled in ([True, False] if payload["enabled"] else [False]):
            attempt = dict(payload, enabled=attempt_enabled)
            try:
                created = create_instance(
                    db_path, attempt, secret_root=secret_root,
                    schema_resolver=None, history_writer=history_writer,
                )
                break
            except ValueError as exc:
                # A card can be enabled but incomplete by the instance model's
                # stricter rules (qBittorrent password with no username, say).
                # Saving it disabled keeps the config instead of losing it.
                last_error = str(exc)
        if created is None:
            result["skipped"].append({"client_type": client_type, "reason": "invalid", "detail": last_error})
            _record_migration(db_path, client_type, "failed", {"reason": "invalid", "detail": last_error})
            continue
        result["migrated"].append({
            "client_type": client_type,
            "instance_id": created["id"],
            "enabled": created["enabled"],
            "requested_enabled": payload["enabled"],
        })
        _record_migration(db_path, client_type, "completed", {
            "instance_id": created["id"], "enabled": created["enabled"],
        })
    return result


def legacy_instance_metadata(db_path, known_types=None):
    """Return redacted migration candidates without writing or copying secrets."""
    known = {str(value).lower() for value in (known_types or DEFAULT_TYPE_SCHEMAS)}
    path = Path(db_path)
    if not path.exists():
        return {"schema": CONTRACT_SCHEMA, "legacy_instances": [], "writes_performed": False}
    uri = f"file:{path}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=3.0)) as con:
        con.row_factory = sqlite3.Row
        table = con.execute("select 1 from sqlite_master where type='table' and name='provider_configs'").fetchone()
        if not table:
            return {"schema": CONTRACT_SCHEMA, "legacy_instances": [], "writes_performed": False}
        rows = []
        for row in con.execute("select id,display_name,enabled,base_url,settings_json,source,updated_at from provider_configs order by id"):
            client_type = str(row["id"] or "").strip().lower()
            if client_type not in known:
                continue
            settings = _json(row["settings_json"], {})
            secret_fields = settings.get("secret_fields") if isinstance(settings.get("secret_fields"), list) else []
            rows.append({
                "id": f"legacy-{client_type}",
                "name": row["display_name"] or client_type,
                "client_type": client_type,
                "enabled": bool(row["enabled"]),
                "base_url_configured": bool(str(row["base_url"] or "").strip()),
                "secret_fields": {str(field): {"configured": bool(settings.get(field))} for field in secret_fields},
                "configuration_source": row["source"] or "legacy_provider_config",
                "migration_state": "not_materialized",
                "updated_at": row["updated_at"],
            })
    return {"schema": CONTRACT_SCHEMA, "legacy_instances": rows, "writes_performed": False}
