#!/usr/bin/env python3
"""Instance-aware download-client selection and just-in-time dispatch."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import inkdrop_download_client_config
import inkdrop_secret_store


CLIENT_PROTOCOLS = {
    "qbittorrent": {"torrent"}, "sabnzbd": {"usenet"}, "slskd": {"soulseek"},
    "transmission": {"torrent"}, "deluge": {"torrent"}, "nzbget": {"usenet"},
    "utorrent": {"torrent"}, "rtorrent": {"torrent"},
}
CLIENT_ALIASES = {
    "qbit": "qbittorrent", "qb": "qbittorrent", "torrent": "qbittorrent",
    "sab": "sabnzbd", "usenet": "sabnzbd", "soulseek": "slskd",
    "transmissionbt": "transmission", "delugeweb": "deluge", "nzb": "nzbget",
    "utorrentweb": "utorrent", "utorrent_webui": "utorrent", "rtorrent_xmlrpc": "rtorrent",
}
FAILED_TEST_STATES = {"failed", "error", "unavailable", "unhealthy", "authentication_failed", "test_failed"}
MAX_FAILOVER_ATTEMPTS = 3
URL_HANDOFF_PROTOCOLS = {"torrent", "usenet"}


def _text(value):
    return str(value or "").strip()


def canonical_client_type(value):
    key = _text(value).lower().replace("-", "_").replace(" ", "_")
    return CLIENT_ALIASES.get(key, key)


def canonical_protocol(value):
    key = _text(value).lower()
    return {"nzb": "usenet", "sabnzbd": "usenet", "nzbget": "usenet", "slskd": "soulseek"}.get(key, key)


def task_media_type(task):
    task = task if isinstance(task, dict) else {}
    raw = task.get("raw_json") if isinstance(task.get("raw_json"), dict) else {}
    wanted = raw.get("wanted_item") if isinstance(raw.get("wanted_item"), dict) else {}
    value = _text(task.get("media_type") or raw.get("media_type") or wanted.get("media_type") or task.get("category")).lower()
    if "manga" in value:
        return "manga"
    if "ebook" in value or value == "book":
        return "ebooks"
    return "comics"


def _json(value, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _private_rows(db_path):
    path = Path(db_path)
    if not path.exists():
        return [], []
    uri = f"file:{path}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=3.0)) as con:
        con.row_factory = sqlite3.Row
        table = con.execute("select 1 from sqlite_master where type='table' and name='download_client_instances'").fetchone()
        if not table:
            return [], []
        rows = [dict(row) for row in con.execute(
            "select * from download_client_instances where deleted_at is null order by priority,created_at,id"
        )]
        mappings = [dict(row) for row in con.execute(
            "select provider_id,protocol,media_type,client_instance_id from download_client_provider_mappings"
        )]
    return rows, mappings


def _ready(row):
    if not row or not bool(row.get("enabled")):
        return False
    client_type = canonical_client_type(row.get("client_type"))
    schema = inkdrop_download_client_config.DEFAULT_TYPE_SCHEMAS.get(client_type) or {}
    if schema.get("implemented") is not True:
        return False
    settings = _json(row.get("settings_json"), {})
    if settings.get("runtime_ready") is False or settings.get("ready") is False:
        return False
    test_state = _text(settings.get("test_status") or settings.get("connection_test_status") or settings.get("health_status")).lower()
    if test_state in FAILED_TEST_STATES:
        return False
    for field in schema.get("required_fields") or []:
        if not _text(row.get(field)):
            return False
    refs = _json(row.get("secret_refs_json"), {})
    for field in schema.get("required_secret_fields") or []:
        if not refs.get(field):
            return False
    any_fields = schema.get("required_secret_fields_any") or []
    return not any_fields or any(refs.get(field) for field in any_fields)


def _compatible(row, protocol):
    return not protocol or protocol in CLIENT_PROTOCOLS.get(canonical_client_type(row.get("client_type")), set())


def _public_candidate(row, reason):
    return {"download_client_instance_id": row["id"], "client_type": canonical_client_type(row.get("client_type")),
        "name": row.get("name"), "priority": int(row.get("priority") or 100), "created_at": row.get("created_at"),
        "routing_reason": reason}


def select_instances(db_path, task, *, exclude_instance_ids=None, limit=MAX_FAILOVER_ATTEMPTS):
    """Return deterministic redacted candidates without decrypting a secret."""
    task = task if isinstance(task, dict) else {}
    rows, mappings = _private_rows(db_path)
    excluded = {_text(value).lower() for value in (exclude_instance_ids or []) if _text(value)}
    protocol = canonical_protocol(task.get("protocol"))
    legacy_type = canonical_client_type(task.get("download_client"))
    if not protocol and legacy_type:
        protocol = next(iter(CLIENT_PROTOCOLS.get(legacy_type) or []), "")
    provider_id = _text(task.get("provider_id") or task.get("source") or task.get("provider")).lower()
    media_type = task_media_type(task)
    ready = [row for row in rows if row["id"].lower() not in excluded and _ready(row) and _compatible(row, protocol)]
    if not ready:
        return []
    by_id = {row["id"]: row for row in ready}
    exact_ids, generic_ids = [], []
    for mapping in mappings:
        if _text(mapping.get("provider_id")).lower() != provider_id or canonical_protocol(mapping.get("protocol")) != protocol:
            continue
        mapped_media = _text(mapping.get("media_type")).lower()
        if mapped_media == media_type:
            exact_ids.append(mapping.get("client_instance_id"))
        elif not mapped_media:
            generic_ids.append(mapping.get("client_instance_id"))
    def ordered(values):
        return sorted({row["id"]: row for row in values}.values(), key=lambda row: (
            int(row.get("priority") or 100), float(row.get("created_at") or 0), row["id"]
        ))

    tiers = [
        ([by_id[value] for value in exact_ids if value in by_id], "provider_protocol_media_mapping"),
        ([by_id[value] for value in generic_ids if value in by_id], "provider_protocol_mapping"),
        ([row for row in ready if canonical_client_type(row.get("client_type")) == legacy_type], "legacy_client_type"),
        (ready, "compatible_priority"),
    ]
    selected, seen = [], set()
    maximum = max(1, min(int(limit or 1), MAX_FAILOVER_ATTEMPTS))
    for values, reason in tiers:
        for row in ordered(values):
            if row["id"] in seen:
                continue
            selected.append(_public_candidate(row, reason))
            seen.add(row["id"])
            if len(selected) >= maximum:
                return selected
    return selected


def select_url_handoff_instances(db_path, task, *, exclude_instance_ids=None, limit=MAX_FAILOVER_ATTEMPTS):
    """Fence Soulseek source work out of generic URL handoff dispatch."""
    task = task if isinstance(task, dict) else {}
    protocol = canonical_protocol(task.get("protocol"))
    if protocol not in URL_HANDOFF_PROTOCOLS or canonical_client_type(task.get("download_client")) == "slskd":
        return []
    return [row for row in select_instances(
        db_path, task, exclude_instance_ids=exclude_instance_ids, limit=limit
    ) if row.get("client_type") != "slskd"]


def slskd_source_instance(db_path, media_type="comics", *, secret_root=None, materialize=False):
    """Resolve SLSKD only for its username/file-list source handoff path."""
    candidates = select_instances(db_path, {"provider_id": "slskd", "protocol": "soulseek",
        "download_client": "slskd", "media_type": media_type}, limit=1)
    if not candidates or candidates[0].get("client_type") != "slskd":
        return None
    instance_id = candidates[0]["download_client_instance_id"]
    if materialize:
        return materialize_instance_settings(db_path, instance_id, media_type, secret_root=secret_root)
    public = inkdrop_download_client_config.get_instance(db_path, instance_id)
    return {**candidates[0], "instance": public}


def materialize_instance_settings(db_path, instance_id, media_type="comics", *, secret_root=None):
    """Decrypt only the selected instance; callers must not persist settings."""
    rows, _mappings = _private_rows(db_path)
    row = next((item for item in rows if item.get("id") == instance_id), None)
    if not _ready(row):
        raise ValueError("download-client instance is not enabled and ready")
    client_type = canonical_client_type(row.get("client_type"))
    settings = _json(row.get("settings_json"), {})
    categories = _json(row.get("categories_json"), {})
    paths = _json(row.get("download_paths_json"), {})
    refs = _json(row.get("secret_refs_json"), {})
    secrets = {field: inkdrop_secret_store.read_secret(reference, root=secret_root) for field, reference in refs.items()}
    category = _text(categories.get(media_type) or row.get("category"))
    download_path = _text(paths.get(media_type) or row.get("download_path"))
    runtime = {**settings, **secrets, "enabled": True, "base_url": row.get("base_url") or "",
        "host": row.get("base_url") or "", "username": row.get("username") or "", "user": row.get("username") or "",
        "category": category, "download_path": download_path, "save_path": download_path,
        "path_mappings": _json(row.get("path_mappings_json"), []), "source": "download_client_instance",
        "download_client_instance_id": row["id"]}
    if "password" in secrets:
        runtime["pass"] = secrets["password"]
    if client_type == "qbittorrent":
        runtime.update({
            "comics_category": _text(categories.get("comics") or category or "comics"),
            "manga_category": _text(categories.get("manga") or category or "manga"),
            "ebooks_category": _text(categories.get("ebooks") or category or "ebooks"),
            "comics_save_path": _text(paths.get("comics") or download_path or "/downloads/comics"),
            "manga_save_path": _text(paths.get("manga") or download_path or "/downloads/manga"),
            "ebooks_save_path": _text(paths.get("ebooks") or download_path or "/downloads/ebooks"),
        })
    if client_type == "sabnzbd":
        runtime["comics_category"] = category or "comics"
    return {"instance_id": row["id"], "client_type": client_type, "settings": runtime}


def dispatch_instance(
    instance,
    download_url,
    title,
    media_type,
    *,
    unique_tag=None,
    dry_run=False,
    expected_url_hash=None,
    require_prowlarr_fetch=False,
    expected_torrent_identity=None,
):
    import inkdrop_acquire
    import inkdrop_download_clients
    client_type, settings = instance["client_type"], instance["settings"]
    locator = inkdrop_acquire.prowlarr_download_url_for_client(download_url)
    if client_type == "slskd":
        return {"ok": False, "added": False, "status": "unsupported_mode",
            "reason": "slskd_requires_source_specific_handoff", "download_client": "slskd",
            "download_client_instance_id": instance["instance_id"]}
    if client_type == "qbittorrent":
        result = inkdrop_acquire.qbit_add(
            download_url,
            title,
            media_type,
            dry_run=dry_run,
            unique_tag=unique_tag,
            settings_override=settings,
            expected_url_hash=expected_url_hash,
            require_prowlarr_fetch=require_prowlarr_fetch,
            expected_torrent_identity=expected_torrent_identity,
        )
    elif client_type == "sabnzbd":
        # SAB handoff may need the original Prowlarr locator so InkDrop can
        # authenticate, fetch the NZB, and upload it instead of asking the
        # separate SAB host/container to resolve an InkDrop-local URL.
        result = inkdrop_acquire.sab_add(download_url, title, dry_run=dry_run, unique_tag=unique_tag, settings_override=settings)
    elif client_type in {"transmission", "deluge", "nzbget", "utorrent", "rtorrent"}:
        result = getattr(inkdrop_download_clients, f"{client_type}_add")(
            locator, title, settings, dry_run=dry_run, unique_tag=unique_tag)
    else:
        raise ValueError(f"{client_type} does not support coordinator URL handoff")
    payload = dict(result or {})
    payload["download_client_instance_id"] = instance["instance_id"]
    payload["download_client"] = client_type
    payload.pop("settings", None)
    return payload


def instance_path_mappings(db_path, instance_id):
    rows, _mappings = _private_rows(db_path)
    row = next((item for item in rows if item.get("id") == instance_id), None)
    return list(_json((row or {}).get("path_mappings_json"), []))
