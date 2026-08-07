#!/usr/bin/env python3
"""Common download-client contracts for InkDrop handoff adapters.

The helpers here are intentionally transport-focused. Queue ownership,
matching, and import state remain in the existing InkDrop workers.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit
import xmlrpc.client

try:
    import requests
except ImportError:  # pragma: no cover - exercised by runtime guards
    requests = None


SECRET_KEYS = {"password", "api_key", "token", "secret"}

TRANSMISSION_STATUS = {
    0: "paused",
    1: "queued",
    2: "active",
    3: "queued",
    4: "active",
    5: "queued",
    6: "seeding",
}

DELUGE_STATUS = {
    "queued": "queued",
    "checking": "active",
    "downloading": "active",
    "seeding": "seeding",
    "paused": "paused",
    "error": "failed",
    "warning": "stalled",
    "allocating": "active",
    "moving": "active",
}

NZBGET_QUEUE_STATUS = {
    "queued": "queued",
    "paused": "paused",
    "downloading": "active",
    "fetching": "active",
    "pp-queued": "queued",
    "post-processing": "active",
    "loading pars": "active",
    "verifying sources": "active",
    "repairing": "active",
    "moving": "active",
}


def _text(value, default=""):
    text = str(value if value is not None else "").strip()
    return text if text else default


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _number(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value):
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _redact_value(key, value):
    if any(token in str(key or "").lower() for token in SECRET_KEYS):
        return "<redacted>" if value not in (None, "") else ""
    if isinstance(value, dict):
        return redact(value)
    if isinstance(value, list):
        return [redact(item) if isinstance(item, dict) else item for item in value]
    return value


def redact(payload):
    return {key: _redact_value(key, value) for key, value in dict(payload or {}).items()}


def handoff_identity(title, download_url=None, unique_tag=None, prefix="inkdrop-task"):
    if unique_tag:
        raw = str(unique_tag)
    else:
        seed = f"{title or 'inkdrop'}|{download_url or ''}"
        raw = f"{prefix}-{hashlib.sha1(seed.encode('utf-8', 'ignore')).hexdigest()[:24]}"
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw).strip("-")
    return (safe or f"{prefix}-handoff")[:64]


def download_client_schemas():
    common = {
        "enabled": {"kind": "boolean", "default": False},
        "base_url": {"kind": "url", "required_when_enabled": True},
        "username": {"kind": "string"},
        "password": {"kind": "secret", "write_only": True, "masked": True},
        "category": {"kind": "string"},
        "label": {"kind": "string"},
        "download_path": {"kind": "path"},
        "path_mappings": {
            "kind": "array",
            "items": {"remote_path": "path", "local_path": "path"},
        },
        "verify_tls": {"kind": "boolean", "default": True},
    }
    return {
        "qbittorrent": {
            "client": "qbittorrent",
            "display_name": "qBittorrent",
            "protocol": "torrent",
            "settings": {
                **common,
                # qBittorrent 5.2.0 and newer can authenticate with an API key
                # instead of a Web UI account. Either credential works on its own.
                "api_key": {"kind": "secret", "write_only": True, "masked": True},
            },
            "controls": ["pause", "resume", "remove"],
        },
        "transmission": {
            "client": "transmission",
            "display_name": "Transmission",
            "protocol": "torrent",
            "settings": dict(common),
            "controls": ["pause", "resume", "remove"],
        },
        "deluge": {
            "client": "deluge",
            "display_name": "Deluge",
            "protocol": "torrent",
            "settings": dict(common),
            "controls": ["pause", "resume", "remove"],
        },
        "nzbget": {
            "client": "nzbget",
            "display_name": "NZBGet",
            "protocol": "usenet",
            "settings": {
                **common,
                "api_key": {"kind": "secret", "write_only": True, "masked": True},
                "category": {"kind": "string"},
            },
            "controls": ["pause", "resume", "remove"],
        },
        "utorrent": {
            "client": "utorrent",
            "display_name": "uTorrent",
            "protocol": "torrent",
            "settings": dict(common),
            "controls": ["pause", "resume", "remove"],
            "implementation_status": "implemented",
        },
        "rtorrent": {
            "client": "rtorrent",
            "display_name": "rTorrent",
            "protocol": "torrent",
            "settings": dict(common),
            "controls": ["pause", "resume", "remove"],
            "implementation_status": "implemented",
        },
    }


def normalize_path_mappings(value):
    if not isinstance(value, list):
        return []
    mappings = []
    for row in value:
        if not isinstance(row, dict):
            continue
        remote = _text(row.get("remote_path") or row.get("remote"))
        local = _text(row.get("local_path") or row.get("local"))
        if remote and local:
            mappings.append({"remote_path": remote.rstrip("/\\"), "local_path": local.rstrip("/\\")})
    return mappings


def validate_transmission_settings(settings):
    raw = dict(settings or {})
    host = _text(raw.get("base_url") or raw.get("host") or os.environ.get("INKDROP_TRANSMISSION_URL")).rstrip("/")
    if host and not host.startswith(("http://", "https://")):
        host = "http://" + host
    username = _text(raw.get("username") or raw.get("user") or os.environ.get("INKDROP_TRANSMISSION_USERNAME"))
    password = _text(raw.get("password") or raw.get("pass") or os.environ.get("INKDROP_TRANSMISSION_PASSWORD"))
    enabled = _bool(raw.get("enabled"), True)
    if enabled and not host:
        raise ValueError("Transmission URL is not configured")
    if enabled and (username and not password):
        raise ValueError("Transmission password is required when username is set")
    if enabled and (password and not username):
        raise ValueError("Transmission username is required when password is set")
    return {
        "enabled": enabled,
        "host": host,
        "username": username,
        "password": password,
        "category": _text(raw.get("category"), "comics"),
        "label": _text(raw.get("label"), "inkdrop"),
        "download_path": _text(raw.get("download_path") or raw.get("save_path"), "/downloads/comics"),
        "path_mappings": normalize_path_mappings(raw.get("path_mappings") or raw.get("remote_path_mappings")),
        "verify_tls": _bool(raw.get("verify_tls"), True),
        "source": _text(raw.get("source"), "settings"),
    }


def validate_deluge_settings(settings):
    raw = dict(settings or {})
    host = _text(raw.get("base_url") or raw.get("host") or os.environ.get("INKDROP_DELUGE_URL")).rstrip("/")
    if host and not host.startswith(("http://", "https://")):
        host = "http://" + host
    password = _text(raw.get("password") or raw.get("pass") or os.environ.get("INKDROP_DELUGE_PASSWORD"))
    enabled = _bool(raw.get("enabled"), True)
    if enabled and not host:
        raise ValueError("Deluge URL is not configured")
    if enabled and not password:
        raise ValueError("Deluge Web password is not configured")
    return {
        "enabled": enabled,
        "host": host,
        "username": _text(raw.get("username") or raw.get("user") or os.environ.get("INKDROP_DELUGE_USERNAME")),
        "password": password,
        "category": _text(raw.get("category"), "comics"),
        "label": _text(raw.get("label"), "inkdrop"),
        "download_path": _text(raw.get("download_path") or raw.get("save_path"), "/downloads/comics"),
        "path_mappings": normalize_path_mappings(raw.get("path_mappings") or raw.get("remote_path_mappings")),
        "verify_tls": _bool(raw.get("verify_tls"), True),
        "source": _text(raw.get("source"), "settings"),
    }


def validate_nzbget_settings(settings):
    raw = dict(settings or {})
    host = _text(raw.get("base_url") or raw.get("host") or os.environ.get("INKDROP_NZBGET_URL")).rstrip("/")
    if host and not host.startswith(("http://", "https://")):
        host = "http://" + host
    username = _text(raw.get("username") or raw.get("user") or os.environ.get("INKDROP_NZBGET_USERNAME"), "nzbget")
    password = _text(raw.get("password") or raw.get("pass") or raw.get("api_key") or os.environ.get("INKDROP_NZBGET_PASSWORD") or os.environ.get("INKDROP_NZBGET_API_KEY"))
    enabled = _bool(raw.get("enabled"), True)
    if enabled and not host:
        raise ValueError("NZBGet URL is not configured")
    if enabled and not password:
        raise ValueError("NZBGet password/API key is not configured")
    return {
        "enabled": enabled,
        "host": host,
        "username": username,
        "password": password,
        "api_key": password,
        "category": _text(raw.get("category"), "comics"),
        "label": _text(raw.get("label"), "inkdrop"),
        "download_path": _text(raw.get("download_path") or raw.get("save_path"), "/downloads/comics"),
        "path_mappings": normalize_path_mappings(raw.get("path_mappings") or raw.get("remote_path_mappings")),
        "verify_tls": _bool(raw.get("verify_tls"), True),
        "source": _text(raw.get("source"), "settings"),
    }


def _strict_http_endpoint(value, label):
    endpoint = _text(value).rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain credentials, query, or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc
    return endpoint


def _bounded_client_settings(settings, client, *, require_auth=True):
    raw = dict(settings or {})
    env_key = client.upper().replace("TORRENT", "TORRENT")
    enabled = _bool(raw.get("enabled"), True)
    endpoint = raw.get("base_url") or raw.get("host") or os.environ.get(f"INKDROP_{env_key}_URL")
    host = _strict_http_endpoint(endpoint, f"{client} URL") if endpoint else ""
    username = _text(raw.get("username") or raw.get("user") or os.environ.get(f"INKDROP_{env_key}_USERNAME"))
    password = _text(raw.get("password") or raw.get("pass") or os.environ.get(f"INKDROP_{env_key}_PASSWORD"))
    if enabled and not host:
        raise ValueError(f"{client} URL is not configured")
    if enabled and require_auth and (not username or not password):
        raise ValueError(f"{client} username and password are required")
    if bool(username) != bool(password):
        raise ValueError(f"{client} username and password must be configured together")
    timeout = _integer(raw.get("timeout_seconds")) or 20
    max_response = _integer(raw.get("max_response_bytes")) or 2 * 1024 * 1024
    max_torrent = _integer(raw.get("max_torrent_bytes")) or 5 * 1024 * 1024
    if not 1 <= timeout <= 60 or not 1024 <= max_response <= 5 * 1024 * 1024 or not 1024 <= max_torrent <= 10 * 1024 * 1024:
        raise ValueError(f"{client} timeout/response limits are outside safe bounds")
    return {
        "enabled": enabled, "host": host,
        "username": username, "password": password,
        "category": _text(raw.get("category"), "comics"), "label": _text(raw.get("label"), "inkdrop"),
        "download_path": _text(raw.get("download_path") or raw.get("save_path"), "/downloads/comics"),
        "path_mappings": normalize_path_mappings(raw.get("path_mappings") or raw.get("remote_path_mappings")),
        "verify_tls": _bool(raw.get("verify_tls"), True), "timeout_seconds": timeout,
        "max_response_bytes": max_response, "max_torrent_bytes": max_torrent,
        "source": _text(raw.get("source"), "settings"),
    }


def validate_utorrent_settings(settings):
    return _bounded_client_settings(settings, "utorrent", require_auth=True)


def validate_rtorrent_settings(settings):
    return _bounded_client_settings(settings, "rtorrent", require_auth=False)


def _public_client_settings(settings):
    """Expose operational policy without returning credentials or private paths."""
    source = dict(settings or {})
    return {key: source.get(key) for key in (
        "enabled", "verify_tls", "timeout_seconds", "max_response_bytes", "max_torrent_bytes", "source"
    )}


def _require_requests():
    if requests is None:
        raise RuntimeError("Python requests package is required for download-client operations")
    return requests


def _require_torrent_identity(download_url, http=None, *, verify=True, timeout=30):
    text = _text(download_url)
    if text.startswith("magnet:"):
        match = re.search(r"(?:xt=urn:btih:)([A-Za-z0-9]{32,40})", text, re.I)
        if match:
            value = match.group(1)
            return value.lower(), None
        raise ValueError("Magnet link does not contain a btih identity")
    session = http or _require_requests()
    response = session.get(text, timeout=timeout, verify=verify)
    response.raise_for_status()
    blob = response.content
    info_hash = torrent_info_hash(blob)
    return info_hash, blob


def torrent_info_hash(blob):
    data = bytes(blob or b"")
    if not data:
        raise ValueError("Torrent payload is empty")

    def parse(index):
        token = data[index:index + 1]
        if token == b"i":
            end = data.index(b"e", index)
            return end + 1
        if token == b"l":
            index += 1
            while data[index:index + 1] != b"e":
                index = parse(index)
            return index + 1
        if token == b"d":
            index += 1
            while data[index:index + 1] != b"e":
                index = parse(index)
                index = parse(index)
            return index + 1
        if token.isdigit():
            colon = data.index(b":", index)
            length = int(data[index:colon])
            return colon + 1 + length
        raise ValueError("Invalid torrent bencode")

    if data[:1] != b"d":
        raise ValueError("Torrent payload is not a bencoded dictionary")
    index = 1
    while index < len(data) and data[index:index + 1] != b"e":
        key_start = index
        key_end = parse(index)
        key = data[data.index(b":", key_start) + 1:key_end]
        value_start = key_end
        value_end = parse(value_start)
        if key == b"info":
            return hashlib.sha1(data[value_start:value_end]).hexdigest()
        index = value_end
    raise ValueError("Torrent payload does not contain an info dictionary")


class DelugeRpc:
    def __init__(self, settings, http=None):
        self.settings = validate_deluge_settings(settings)
        self.http = http or _require_requests().Session()
        self.request_id = 0

    @property
    def json_url(self):
        return self.settings["host"].rstrip("/") + "/json"

    def call(self, method, params=None, *, timeout=20):
        self.request_id += 1
        response = self.http.post(
            self.json_url,
            json={"method": method, "params": params or [], "id": self.request_id},
            timeout=timeout,
            verify=self.settings.get("verify_tls", True),
        )
        if response.status_code in {401, 403}:
            raise PermissionError("Deluge authentication failed")
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            message = data["error"].get("message") if isinstance(data.get("error"), dict) else data.get("error")
            if "password" in str(message).lower() or "auth" in str(message).lower():
                raise PermissionError("Deluge authentication failed")
            raise RuntimeError(f"Deluge {method} failed: {message}")
        return data.get("result")

    def login(self):
        result = self.call("auth.login", [self.settings.get("password")])
        if result is not True:
            raise PermissionError("Deluge authentication failed")
        return True


def deluge_test_connection(settings, http=None):
    try:
        client = DelugeRpc(settings, http=http)
        client.login()
        version = client.call("web.connected") or None
        if isinstance(version, bool):
            version = client.call("daemon.info") if version else None
        return {
            "ok": True,
            "client": "deluge",
            "version": version,
            "capabilities": {
                "pause": True,
                "resume": True,
                "remove": True,
                "torrent_info_hash_identity_required": True,
            },
            "settings": redact(validate_deluge_settings(settings)),
        }
    except ValueError as exc:
        return {"ok": False, "client": "deluge", "error_type": "configuration", "reason": str(exc)}
    except PermissionError as exc:
        return {"ok": False, "client": "deluge", "error_type": "authentication", "reason": str(exc)}
    except Exception as exc:
        return {"ok": False, "client": "deluge", "error_type": "connectivity", "reason": str(exc)}


def deluge_find_existing(client, info_hash):
    if not info_hash:
        return None
    fields = [
        "name",
        "state",
        "progress",
        "total_size",
        "total_done",
        "download_payload_rate",
        "upload_payload_rate",
        "eta",
        "time_added",
        "seeding_time",
        "save_path",
        "message",
        "is_finished",
        "is_seed",
    ]
    result = client.call("core.get_torrents_status", [{}, fields]) or {}
    torrent = result.get(info_hash) or result.get(str(info_hash).lower()) or result.get(str(info_hash).upper())
    if isinstance(torrent, dict):
        return {**torrent, "hash": str(info_hash).lower()}
    return None


def deluge_output_path(torrent, settings):
    torrent = torrent if isinstance(torrent, dict) else {}
    save_path = _text(torrent.get("save_path") or settings.get("download_path"))
    name = _text(torrent.get("name"))
    if not save_path:
        return ""
    remote = str(PurePosixPath(save_path.replace("\\", "/")) / name) if name else save_path
    return map_remote_path(remote, settings.get("path_mappings"))


def deluge_status(torrent, settings=None, now=None):
    torrent = torrent if isinstance(torrent, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    now = time.time() if now is None else float(now)
    raw_state = _text(torrent.get("state"), "unknown")
    state = DELUGE_STATUS.get(raw_state.lower(), "unknown")
    progress = _number(torrent.get("progress"))
    percent = round(max(0.0, min(100.0, progress)), 2) if progress is not None else None
    total = _integer(torrent.get("total_size"))
    completed = _integer(torrent.get("total_done"))
    if percent is not None and percent >= 100 and state not in {"seeding", "failed"}:
        state = "completed"
    if torrent.get("is_seed") is True:
        state = "seeding"
    added = _integer(torrent.get("time_added"))
    message = _text(torrent.get("message"))
    return {
        "client": "deluge",
        "client_item_id": torrent.get("hash"),
        "transfer_state": state,
        "native_state": raw_state,
        "progress_kind": "determinate" if percent is not None else "indeterminate",
        "percent_complete": percent,
        "bytes_completed": completed,
        "bytes_total": total,
        "download_rate_bytes_per_second": _integer(torrent.get("download_payload_rate")),
        "upload_rate_bytes_per_second": _integer(torrent.get("upload_payload_rate")),
        "eta_seconds": None if state in {"completed", "seeding"} else _integer(torrent.get("eta")),
        "elapsed_seconds": int(max(0, now - added)) if added else None,
        "started_at": added,
        "last_updated_at": now,
        "completed_at": None,
        "stalled": state == "stalled",
        "stalled_reason": message if state == "stalled" and message else None,
        "client_error": message if state == "failed" and message else None,
        "completed_output_path": deluge_output_path(torrent, settings) if state in {"completed", "seeding"} else None,
        "seeding": state == "seeding",
    }


def deluge_result(torrent, *, settings, category, handoff_tag, added, existing=False):
    torrent = torrent if isinstance(torrent, dict) else {}
    status = deluge_status(torrent, settings)
    external_id = str(torrent.get("hash") or "").strip().lower()
    out = {
        "ok": bool(external_id),
        "added": bool(added),
        "existing": bool(existing),
        "download_client": "Deluge",
        "protocol": "torrent",
        "category": category,
        "save_path": settings.get("download_path"),
        "handoff_tag": handoff_tag,
        "client_id": external_id,
        "client_external_id": external_id,
        "torrent_hash": external_id,
        "client_name": torrent.get("name"),
        "client_state": status.get("transfer_state"),
        "progress": status.get("percent_complete"),
        "size_bytes": status.get("bytes_total"),
        "local_path": status.get("completed_output_path"),
        "download_client_snapshot": status,
        "settings_source": settings.get("source"),
    }
    return {key: value for key, value in out.items() if value not in (None, "")}


def deluge_add(download_url, title, settings, *, dry_run=False, unique_tag=None, http=None, fetch_http=None):
    settings = validate_deluge_settings(settings)
    category = settings.get("category") or "comics"
    handoff_tag = handoff_identity(title, download_url, unique_tag=unique_tag)
    info_hash, torrent_blob = _require_torrent_identity(
        download_url,
        http=fetch_http,
        verify=settings.get("verify_tls", True),
    )
    if dry_run:
        return {
            "dry_run": True,
            "download_client": "Deluge",
            "protocol": "torrent",
            "category": category,
            "save_path": settings.get("download_path"),
            "handoff_tag": handoff_tag,
            "client_external_id": info_hash,
            "torrent_hash": info_hash,
            "settings_source": settings.get("source"),
        }
    client = DelugeRpc(settings, http=http)
    client.login()
    existing = deluge_find_existing(client, info_hash)
    if existing:
        return deluge_result(existing, settings=settings, category=category, handoff_tag=handoff_tag, added=False, existing=True)
    options = {
        "download_location": settings.get("download_path"),
        "add_paused": False,
    }
    if torrent_blob is not None:
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", title or info_hash).strip("-") or f"{info_hash}.torrent"
        if not filename.lower().endswith(".torrent"):
            filename += ".torrent"
        added_hash = client.call(
            "core.add_torrent_file",
            [filename, base64.b64encode(torrent_blob).decode("ascii"), options],
            timeout=30,
        )
    else:
        added_hash = client.call("core.add_torrent_magnet", [download_url, options], timeout=30)
    added_hash = str(added_hash or info_hash).lower()
    torrent = deluge_find_existing(client, added_hash) or {"hash": added_hash, "name": title, "state": "Queued", "save_path": settings.get("download_path")}
    return deluge_result(torrent, settings=settings, category=category, handoff_tag=handoff_tag, added=True)


def deluge_control(settings, torrent_id, action, *, http=None):
    action = str(action or "").strip().lower()
    if action not in {"pause", "resume", "remove"}:
        return {"ok": False, "client": "deluge", "action": action, "unsupported": True}
    client = DelugeRpc(settings, http=http)
    client.login()
    ids = [torrent_id]
    if action == "pause":
        client.call("core.pause_torrent", [ids])
    elif action == "resume":
        client.call("core.resume_torrent", [ids])
    else:
        client.call("core.remove_torrent", [torrent_id, False])
    return {"ok": True, "client": "deluge", "action": action, "delete_data": False if action == "remove" else None}


class NzbgetRpc:
    def __init__(self, settings, http=None):
        self.settings = validate_nzbget_settings(settings)
        self.http = http or _require_requests().Session()
        self.request_id = 0

    @property
    def json_url(self):
        return self.settings["host"].rstrip("/") + "/jsonrpc"

    def _auth(self):
        return (self.settings.get("username") or "nzbget", self.settings.get("password") or "")

    def call(self, method, params=None, *, timeout=20):
        self.request_id += 1
        response = self.http.post(
            self.json_url,
            json={"method": method, "params": params or [], "id": self.request_id},
            auth=self._auth(),
            timeout=timeout,
            verify=self.settings.get("verify_tls", True),
        )
        if response.status_code in {401, 403}:
            raise PermissionError("NZBGet authentication failed")
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            message = data["error"].get("message") if isinstance(data.get("error"), dict) else data.get("error")
            if "auth" in str(message).lower() or "password" in str(message).lower():
                raise PermissionError("NZBGet authentication failed")
            raise RuntimeError(f"NZBGet {method} failed: {message}")
        return data.get("result")


def nzbget_test_connection(settings, http=None):
    try:
        client = NzbgetRpc(settings, http=http)
        version = client.call("version")
        status = client.call("status") or {}
        return {
            "ok": True,
            "client": "nzbget",
            "version": version,
            "capabilities": {
                "append": True,
                "listgroups": True,
                "history": True,
                "editqueue": True,
                "download_paused": bool(status.get("DownloadPaused")) if isinstance(status, dict) else None,
            },
            "settings": redact(validate_nzbget_settings(settings)),
        }
    except ValueError as exc:
        return {"ok": False, "client": "nzbget", "error_type": "configuration", "reason": str(exc)}
    except PermissionError as exc:
        return {"ok": False, "client": "nzbget", "error_type": "authentication", "reason": str(exc)}
    except Exception as exc:
        return {"ok": False, "client": "nzbget", "error_type": "connectivity", "reason": str(exc)}


def nzbget_handoff_key(title, download_url=None, unique_tag=None):
    return handoff_identity(title, download_url, unique_tag=unique_tag, prefix="inkdrop-nzb")


def _nzbget_rows(client):
    queue = client.call("listgroups", [0]) or []
    history = client.call("history", [True]) or []
    queue_rows = [{**row, "_inkdrop_section": "queue"} for row in queue if isinstance(row, dict)]
    history_rows = [{**row, "_inkdrop_section": "history"} for row in history if isinstance(row, dict)]
    return queue_rows, history_rows


def nzbget_find_existing(client, handoff_key):
    queue_rows, history_rows = _nzbget_rows(client)
    for row in queue_rows:
        if _text(row.get("DupeKey")) == handoff_key:
            return row
    for row in history_rows:
        if _text(row.get("DupeKey")) != handoff_key:
            continue
        status = _text(row.get("Status")).lower()
        delete_status = _text(row.get("DeleteStatus")).lower()
        if status.startswith("failure") or delete_status in {"manual", "dupe", "bad", "health", "scan"}:
            continue
        return row
    return None


def _nzbget_bytes(lo, hi, mb=None):
    low = _integer(lo)
    high = _integer(hi)
    if low is not None or high is not None:
        return int(low or 0) + (int(high or 0) << 32)
    mib = _number(mb)
    return int(mib * 1024 * 1024) if mib is not None else None


def nzbget_output_path(row, settings):
    row = row if isinstance(row, dict) else {}
    path = _text(row.get("FinalDir") or row.get("DestDir") or settings.get("download_path"))
    return map_remote_path(path, settings.get("path_mappings"))


def nzbget_status(row, settings=None, now=None):
    row = row if isinstance(row, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    now = time.time() if now is None else float(now)
    section = _text(row.get("_inkdrop_section"), "queue")
    raw_status = _text(row.get("Status") or row.get("Kind") or row.get("NZBName") or "unknown")
    state_key = raw_status.lower()
    if section == "history":
        if raw_status.startswith("SUCCESS"):
            state = "completed"
        elif raw_status.startswith("DELETED"):
            state = "removed"
        elif raw_status.startswith("FAILURE"):
            state = "failed"
        elif raw_status.startswith("WARNING"):
            state = "stalled"
        else:
            state = "completed"
    else:
        state = NZBGET_QUEUE_STATUS.get(state_key, "unknown")
        if bool(row.get("Paused")):
            state = "paused"
    total = _nzbget_bytes(row.get("FileSizeLo"), row.get("FileSizeHi"), row.get("FileSizeMB"))
    remaining = _nzbget_bytes(row.get("RemainingSizeLo"), row.get("RemainingSizeHi"), row.get("RemainingSizeMB"))
    completed = None if total is None else max(0, total - int(remaining or 0))
    if section == "history":
        downloaded = _nzbget_bytes(row.get("DownloadedSizeLo"), row.get("DownloadedSizeHi"), row.get("DownloadedSizeMB"))
        if downloaded is not None:
            completed = downloaded
    percent = None
    if total:
        percent = round(max(0.0, min(100.0, (completed or 0) * 100.0 / total)), 2)
    if state == "completed":
        percent = 100.0 if percent is None else percent
    added = _integer(row.get("MinPostTime") or row.get("HistoryTime"))
    error = _text(row.get("Status")) if state in {"failed", "stalled"} else ""
    return {
        "client": "nzbget",
        "client_item_id": row.get("NZBID") or row.get("ID"),
        "transfer_state": state,
        "native_state": raw_status,
        "progress_kind": "determinate" if percent is not None else "indeterminate",
        "percent_complete": percent,
        "bytes_completed": completed,
        "bytes_total": total,
        "download_rate_bytes_per_second": _integer(row.get("DownloadRate") or row.get("DownloadSpeed")),
        "upload_rate_bytes_per_second": None,
        "eta_seconds": _integer(row.get("RemainingTimeSec") or row.get("ETA")),
        "elapsed_seconds": int(max(0, now - added)) if added else _integer(row.get("DownloadTimeSec")),
        "started_at": added,
        "last_updated_at": now,
        "completed_at": _integer(row.get("HistoryTime")) if section == "history" else None,
        "stalled": state == "stalled",
        "stalled_reason": error if state == "stalled" else None,
        "client_error": error if state == "failed" else None,
        "completed_output_path": nzbget_output_path(row, settings) if state == "completed" else None,
        "seeding": False,
    }


def nzbget_result(row, *, settings, category, handoff_key, added, existing=False):
    row = row if isinstance(row, dict) else {}
    status = nzbget_status(row, settings)
    external_id = row.get("NZBID") or row.get("ID")
    out = {
        "ok": external_id not in (None, "", 0),
        "added": bool(added),
        "existing": bool(existing),
        "download_client": "NZBGet",
        "protocol": "usenet",
        "category": category,
        "save_path": settings.get("download_path"),
        "handoff_key": handoff_key,
        "client_id": external_id,
        "client_external_id": external_id,
        "nzb_id": external_id,
        "nzo_id": external_id,
        "client_name": row.get("NZBName") or row.get("Name"),
        "client_state": status.get("transfer_state"),
        "progress": status.get("percent_complete"),
        "size_bytes": status.get("bytes_total"),
        "local_path": status.get("completed_output_path"),
        "download_client_snapshot": status,
        "settings_source": settings.get("source"),
    }
    return {key: value for key, value in out.items() if value not in (None, "")}


def nzbget_add(download_url, title, settings, *, dry_run=False, unique_tag=None, http=None):
    settings = validate_nzbget_settings(settings)
    category = settings.get("category") or "comics"
    handoff_key = nzbget_handoff_key(title, download_url, unique_tag=unique_tag)
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", title or handoff_key).strip("-") or f"{handoff_key}.nzb"
    if not filename.lower().endswith(".nzb"):
        filename += ".nzb"
    if dry_run:
        return {
            "dry_run": True,
            "download_client": "NZBGet",
            "protocol": "usenet",
            "category": category,
            "save_path": settings.get("download_path"),
            "handoff_key": handoff_key,
            "settings_source": settings.get("source"),
        }
    client = NzbgetRpc(settings, http=http)
    existing = nzbget_find_existing(client, handoff_key)
    if existing:
        return nzbget_result(existing, settings=settings, category=category, handoff_key=handoff_key, added=False, existing=True)
    nzb_id = client.call(
        "append",
        [
            filename,
            _text(download_url),
            category,
            0,
            False,
            False,
            handoff_key,
            0,
            "SCORE",
            False,
            [],
        ],
        timeout=30,
    )
    try:
        nzb_id_int = int(nzb_id)
    except (TypeError, ValueError):
        nzb_id_int = 0
    if nzb_id_int <= 0:
        existing = nzbget_find_existing(client, handoff_key)
        if existing:
            return nzbget_result(existing, settings=settings, category=category, handoff_key=handoff_key, added=False, existing=True)
        return {
            "ok": False,
            "added": False,
            "download_client": "NZBGet",
            "protocol": "usenet",
            "status": "failed_download",
            "reason": "nzbget_append_failed",
            "handoff_key": handoff_key,
            "settings_source": settings.get("source"),
        }
    row = nzbget_find_existing(client, handoff_key) or {"NZBID": nzb_id_int, "NZBName": title, "Category": category, "Status": "queued", "_inkdrop_section": "queue"}
    return nzbget_result(row, settings=settings, category=category, handoff_key=handoff_key, added=True)


def nzbget_control(settings, nzb_id, action, *, http=None):
    action = str(action or "").strip().lower()
    if action not in {"pause", "resume", "remove"}:
        return {"ok": False, "client": "nzbget", "action": action, "unsupported": True}
    client = NzbgetRpc(settings, http=http)
    command = {"pause": "GroupPause", "resume": "GroupResume", "remove": "GroupDelete"}[action]
    result = client.call("editqueue", [command, "", [int(nzb_id)]])
    return {"ok": bool(result), "client": "nzbget", "action": action, "delete_data": False if action == "remove" else None}


class TransmissionRpc:
    def __init__(self, settings, http=None):
        self.settings = validate_transmission_settings(settings)
        self.http = http or _require_requests().Session()
        self.session_id = None

    @property
    def rpc_url(self):
        return self.settings["host"].rstrip("/") + "/transmission/rpc"

    def _auth(self):
        if self.settings.get("username") or self.settings.get("password"):
            return (self.settings.get("username") or "", self.settings.get("password") or "")
        return None

    def call(self, method, arguments=None, *, timeout=20):
        payload = {"method": method, "arguments": arguments or {}}
        headers = {}
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        response = self.http.post(
            self.rpc_url,
            json=payload,
            headers=headers,
            auth=self._auth(),
            timeout=timeout,
            verify=self.settings.get("verify_tls", True),
        )
        if response.status_code == 409:
            self.session_id = response.headers.get("X-Transmission-Session-Id")
            headers["X-Transmission-Session-Id"] = self.session_id
            response = self.http.post(
                self.rpc_url,
                json=payload,
                headers=headers,
                auth=self._auth(),
                timeout=timeout,
                verify=self.settings.get("verify_tls", True),
            )
        if response.status_code in {401, 403}:
            raise PermissionError("Transmission authentication failed")
        response.raise_for_status()
        data = response.json()
        if str(data.get("result") or "").lower() != "success":
            raise RuntimeError(f"Transmission {method} failed: {data.get('result')}")
        return data.get("arguments") or {}


def transmission_test_connection(settings, http=None):
    try:
        client = TransmissionRpc(settings, http=http)
        args = client.call("session-get", {"fields": ["version", "rpc-version", "rpc-version-min"]})
        return {
            "ok": True,
            "client": "transmission",
            "version": args.get("version"),
            "capabilities": {
                "rpc_version": args.get("rpc-version"),
                "rpc_version_min": args.get("rpc-version-min"),
                "labels": bool((args.get("rpc-version") or 0) >= 16),
                "pause": True,
                "resume": True,
                "remove": True,
            },
            "settings": redact(validate_transmission_settings(settings)),
        }
    except ValueError as exc:
        return {"ok": False, "client": "transmission", "error_type": "configuration", "reason": str(exc)}
    except PermissionError as exc:
        return {"ok": False, "client": "transmission", "error_type": "authentication", "reason": str(exc)}
    except Exception as exc:
        return {"ok": False, "client": "transmission", "error_type": "connectivity", "reason": str(exc)}


def transmission_torrent_fields():
    return [
        "id",
        "hashString",
        "name",
        "labels",
        "downloadDir",
        "status",
        "percentDone",
        "totalSize",
        "leftUntilDone",
        "rateDownload",
        "rateUpload",
        "eta",
        "isStalled",
        "error",
        "errorString",
        "addedDate",
        "doneDate",
    ]


def _torrent_labels(torrent):
    labels = (torrent or {}).get("labels") or []
    if isinstance(labels, str):
        return {part.strip() for part in labels.split(",") if part.strip()}
    return {str(part).strip() for part in labels if str(part or "").strip()}


def _normalize_title(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def transmission_find_existing(client, handoff_tag, title=None):
    args = client.call("torrent-get", {"fields": transmission_torrent_fields()})
    torrents = [row for row in args.get("torrents") or [] if isinstance(row, dict)]
    title_key = _normalize_title(title)
    for torrent in torrents:
        if handoff_tag in _torrent_labels(torrent):
            return torrent
    if title_key:
        for torrent in torrents:
            if _normalize_title(torrent.get("name")) == title_key:
                return torrent
    return None


def map_remote_path(path, mappings):
    text = _text(path)
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    for row in normalize_path_mappings(mappings):
        remote = row["remote_path"].replace("\\", "/").rstrip("/")
        local = row["local_path"].replace("\\", "/").rstrip("/")
        if normalized == remote or normalized.startswith(remote + "/"):
            return local + normalized[len(remote):]
    return text


def transmission_output_path(torrent, settings):
    torrent = torrent if isinstance(torrent, dict) else {}
    download_dir = _text(torrent.get("downloadDir") or settings.get("download_path"))
    name = _text(torrent.get("name"))
    if not download_dir:
        return ""
    remote = str(PurePosixPath(download_dir.replace("\\", "/")) / name) if name else download_dir
    return map_remote_path(remote, settings.get("path_mappings"))


def transmission_status(torrent, settings=None, now=None):
    torrent = torrent if isinstance(torrent, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    now = time.time() if now is None else float(now)
    percent = _number(torrent.get("percentDone"))
    if percent is not None:
        percent = round(max(0.0, min(100.0, percent * 100.0 if percent <= 1 else percent)), 2)
    total = _integer(torrent.get("totalSize"))
    left = _integer(torrent.get("leftUntilDone"))
    completed = max(0, total - left) if total is not None and left is not None else None
    native_status = torrent.get("status")
    state = TRANSMISSION_STATUS.get(native_status, "unknown")
    if torrent.get("error"):
        state = "failed"
    elif torrent.get("isStalled") and state in {"active", "queued"}:
        state = "stalled"
    elif percent is not None and percent >= 100 and state != "seeding":
        state = "completed"
    added = _integer(torrent.get("addedDate"))
    done = _integer(torrent.get("doneDate"))
    return {
        "client": "transmission",
        "client_item_id": torrent.get("hashString") or torrent.get("id"),
        "transfer_state": state,
        "native_state": f"transmission:{native_status}" if native_status is not None else None,
        "progress_kind": "determinate" if percent is not None else "indeterminate",
        "percent_complete": percent,
        "bytes_completed": completed,
        "bytes_total": total,
        "download_rate_bytes_per_second": _integer(torrent.get("rateDownload")),
        "upload_rate_bytes_per_second": _integer(torrent.get("rateUpload")),
        "eta_seconds": None if state in {"completed", "seeding"} else _integer(torrent.get("eta")),
        "elapsed_seconds": int(max(0, now - added)) if added else None,
        "started_at": added,
        "last_updated_at": now,
        "completed_at": done or None,
        "stalled": state == "stalled",
        "stalled_reason": "transmission_stalled" if state == "stalled" else None,
        "client_error": torrent.get("errorString") or None,
        "completed_output_path": transmission_output_path(torrent, settings) if state in {"completed", "seeding"} else None,
        "seeding": state == "seeding",
    }


def transmission_result(torrent, *, settings, category, handoff_tag, added, existing=False):
    torrent = torrent if isinstance(torrent, dict) else {}
    status = transmission_status(torrent, settings)
    external_id = str(torrent.get("hashString") or torrent.get("id") or "").strip()
    out = {
        "ok": bool(external_id),
        "added": bool(added),
        "existing": bool(existing),
        "download_client": "Transmission",
        "protocol": "torrent",
        "category": category,
        "save_path": settings.get("download_path"),
        "handoff_tag": handoff_tag,
        "client_id": external_id,
        "client_external_id": external_id,
        "torrent_hash": torrent.get("hashString"),
        "client_name": torrent.get("name"),
        "client_state": status.get("transfer_state"),
        "progress": status.get("percent_complete"),
        "size_bytes": status.get("bytes_total"),
        "local_path": status.get("completed_output_path"),
        "download_client_snapshot": status,
        "settings_source": settings.get("source"),
    }
    return {key: value for key, value in out.items() if value not in (None, "")}


def transmission_add(download_url, title, settings, *, dry_run=False, unique_tag=None, http=None):
    settings = validate_transmission_settings(settings)
    download_url = _text(download_url)
    category = settings.get("category") or "comics"
    handoff_tag = handoff_identity(title, download_url, unique_tag=unique_tag)
    if dry_run:
        return {
            "dry_run": True,
            "download_client": "Transmission",
            "protocol": "torrent",
            "category": category,
            "save_path": settings.get("download_path"),
            "handoff_tag": handoff_tag,
            "settings_source": settings.get("source"),
        }
    client = TransmissionRpc(settings, http=http)
    existing = transmission_find_existing(client, handoff_tag, title=title)
    if existing:
        return transmission_result(existing, settings=settings, category=category, handoff_tag=handoff_tag, added=False, existing=True)
    args = {
        "filename": download_url,
        "download-dir": settings.get("download_path"),
        "paused": False,
        "labels": [settings.get("label") or "inkdrop", handoff_tag],
    }
    added = client.call("torrent-add", args, timeout=30)
    torrent = added.get("torrent-added") or added.get("torrent-duplicate") or {}
    if not torrent:
        torrent = transmission_find_existing(client, handoff_tag, title=title) or {}
    return transmission_result(
        torrent,
        settings=settings,
        category=category,
        handoff_tag=handoff_tag,
        added=bool(added.get("torrent-added")),
        existing=bool(added.get("torrent-duplicate")),
    )


def transmission_control(settings, torrent_id, action, *, http=None):
    action = str(action or "").strip().lower()
    if action not in {"pause", "resume", "remove"}:
        return {"ok": False, "client": "transmission", "action": action, "unsupported": True}
    client = TransmissionRpc(settings, http=http)
    method = {"pause": "torrent-stop", "resume": "torrent-start", "remove": "torrent-remove"}[action]
    args = {"ids": [torrent_id]}
    if action == "remove":
        args["delete-local-data"] = False
    client.call(method, args)
    return {"ok": True, "client": "transmission", "action": action, "delete_data": False if action == "remove" else None}


def _bounded_response(response, limit):
    status = int(getattr(response, "status_code", 200) or 200)
    if 300 <= status < 400:
        raise RuntimeError("redirects are not permitted for download-client endpoints")
    response.raise_for_status()
    content = bytes(getattr(response, "content", b"") or b"")
    if len(content) > int(limit):
        raise RuntimeError("download-client response exceeded the configured bound")
    return content


def _fetch_metainfo(download_url, settings, http=None):
    text = _text(download_url)
    if text.startswith("magnet:"):
        return _require_torrent_identity(text, http=http, verify=settings["verify_tls"], timeout=settings["timeout_seconds"])
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("torrent locator must be a credential-free HTTP(S) URL or magnet")
    transport = http or _require_requests()
    response = transport.get(text, timeout=settings["timeout_seconds"], verify=settings["verify_tls"], allow_redirects=False)
    blob = _bounded_response(response, settings["max_torrent_bytes"])
    return torrent_info_hash(blob), blob


class UTorrentWebUi:
    def __init__(self, settings, http=None):
        self.settings = validate_utorrent_settings(settings)
        self.http = http or _require_requests().Session()
        self.token = ""

    def _auth(self):
        return (self.settings["username"], self.settings["password"])

    def authenticate(self):
        response = self.http.get(self.settings["host"] + "/gui/token.html", auth=self._auth(),
            timeout=self.settings["timeout_seconds"], verify=self.settings["verify_tls"], allow_redirects=False)
        if int(getattr(response, "status_code", 200) or 200) in {401, 403}:
            raise PermissionError("uTorrent WebUI authentication failed")
        body = _bounded_response(response, 64 * 1024).decode("utf-8", "replace")
        match = re.search(r"<div[^>]+id=[\"']token[\"'][^>]*>([^<]+)</div>", body, re.I)
        if not match or not match.group(1).strip():
            raise PermissionError("uTorrent WebUI token authentication failed")
        self.token = match.group(1).strip()
        return self.token

    def call(self, params=None, files=None):
        if not self.token:
            self.authenticate()
        query = {"token": self.token, **dict(params or {})}
        request = self.http.post if files else self.http.get
        kwargs = {"params": query, "auth": self._auth(), "timeout": self.settings["timeout_seconds"],
            "verify": self.settings["verify_tls"], "allow_redirects": False}
        if files:
            kwargs["files"] = files
        response = request(self.settings["host"] + "/gui/", **kwargs)
        body = _bounded_response(response, self.settings["max_response_bytes"])
        return json.loads(body.decode("utf-8")) if body else {}

    def torrents(self):
        return list((self.call({"list": 1}) or {}).get("torrents") or [])[:500]


def _utorrent_row(row):
    if isinstance(row, dict):
        return dict(row)
    values = list(row or [])
    def at(index, default=None):
        return values[index] if index < len(values) else default
    return {"hash": at(0, ""), "status": at(1, 0), "name": at(2, ""), "size": at(3, 0),
        "progress": at(4, 0), "downloaded": at(5, 0), "uploaded": at(6, 0), "upload_speed": at(8, 0),
        "download_speed": at(9, 0), "eta": at(10, 0), "label": at(11, ""), "remaining": at(18, 0),
        "save_path": at(26, "")}


def utorrent_status(row, settings=None):
    settings = validate_utorrent_settings(settings or {"base_url": "http://invalid", "username": "x", "password": "x"})
    row = _utorrent_row(row)
    progress = max(0.0, min(100.0, float(row.get("progress") or 0) / 10.0))
    flags = int(row.get("status") or 0)
    if flags & 16:
        state = "failed"
    elif progress >= 100 and flags & 1:
        state = "seeding"
    elif progress >= 100:
        state = "completed"
    elif flags & 32:
        state = "paused"
    elif flags & 64:
        state = "queued"
    elif flags & 1:
        state = "active"
    else:
        state = "paused"
    remote = _text(row.get("save_path"))
    local = map_remote_path(remote, settings.get("path_mappings")) if remote else None
    return {"client": "utorrent", "external_id": _text(row.get("hash")).lower(), "name": _text(row.get("name")),
        "status": state, "state": state, "transfer_state": state, "percent_complete": progress,
        "size_bytes": _integer(row.get("size")), "downloaded_bytes": _integer(row.get("downloaded")),
        "remaining_bytes": _integer(row.get("remaining")), "download_speed": _integer(row.get("download_speed")),
        "upload_speed": _integer(row.get("upload_speed")), "eta_seconds": _integer(row.get("eta")),
        "output_path": local, "completed_output_path": local if state in {"completed", "seeding"} else None}


def utorrent_find_existing(client, info_hash):
    target = _text(info_hash).lower()
    return next((_utorrent_row(row) for row in client.torrents() if _text(_utorrent_row(row).get("hash")).lower() == target), None)


def utorrent_test_connection(settings, http=None):
    try:
        client = UTorrentWebUi(settings, http=http)
        client.authenticate()
        client.torrents()
        return {"ok": True, "client": "utorrent", "capabilities": {"token_auth": True, "metainfo_upload": True},
            "settings": _public_client_settings(client.settings)}
    except ValueError as exc:
        return {"ok": False, "client": "utorrent", "error_type": "configuration", "reason": str(exc)}
    except PermissionError as exc:
        return {"ok": False, "client": "utorrent", "error_type": "authentication", "reason": str(exc)}
    except Exception as exc:
        return {"ok": False, "client": "utorrent", "error_type": "connectivity", "reason": type(exc).__name__}


def utorrent_add(download_url, title, settings, *, dry_run=False, unique_tag=None, http=None, fetch_http=None):
    settings = validate_utorrent_settings(settings)
    info_hash, blob = _fetch_metainfo(download_url, settings, http=fetch_http)
    handoff_tag = handoff_identity(title, download_url, unique_tag)
    if dry_run:
        return {"ok": True, "dry_run": True, "download_client": "uTorrent", "torrent_hash": info_hash,
            "client_external_id": info_hash, "handoff_tag": handoff_tag, "added": False}
    client = UTorrentWebUi(settings, http=http)
    existing = utorrent_find_existing(client, info_hash)
    if not existing:
        if blob is None:
            client.call({"action": "add-url", "s": download_url, "download_dir": settings["download_path"]})
        else:
            client.call({"action": "add-file", "download_dir": settings["download_path"]},
                files={"torrent_file": (handoff_tag + ".torrent", blob, "application/x-bittorrent")})
        existing = utorrent_find_existing(client, info_hash) or {"hash": info_hash, "name": title, "status": 1, "save_path": settings["download_path"]}
        added = True
    else:
        added = False
    snapshot = utorrent_status(existing, settings)
    return {"ok": True, "download_client": "uTorrent", "protocol": "torrent", "client_external_id": info_hash,
        "torrent_hash": info_hash, "handoff_tag": handoff_tag, "added": added, "existing": not added,
        "category": settings["category"], "save_path": settings["download_path"], "settings_source": settings["source"],
        "download_client_snapshot": snapshot, "local_path": snapshot.get("output_path")}


def utorrent_control(settings, torrent_id, action, *, http=None):
    if action not in {"pause", "resume", "remove"}:
        return {"ok": False, "client": "utorrent", "action": action, "unsupported": True}
    client = UTorrentWebUi(settings, http=http)
    native = {"pause": "pause", "resume": "start", "remove": "remove"}[action]
    client.call({"action": native, "hash": _text(torrent_id)})
    return {"ok": True, "client": "utorrent", "action": action, "delete_data": False if action == "remove" else None}


class RTorrentXmlRpc:
    def __init__(self, settings, http=None):
        self.settings = validate_rtorrent_settings(settings)
        self.http = http or _require_requests().Session()

    def call(self, method, *params):
        body = xmlrpc.client.dumps(params, methodname=method, allow_none=True).encode("utf-8")
        if len(body) > self.settings["max_response_bytes"]:
            raise RuntimeError("rTorrent XML-RPC request exceeded the configured bound")
        auth = (self.settings["username"], self.settings["password"]) if self.settings["username"] else None
        response = self.http.post(self.settings["host"], data=body, headers={"Content-Type": "text/xml"}, auth=auth,
            timeout=self.settings["timeout_seconds"], verify=self.settings["verify_tls"], allow_redirects=False)
        payload = _bounded_response(response, self.settings["max_response_bytes"])
        values, _method = xmlrpc.client.loads(payload)
        return values[0] if len(values) == 1 else values

    def torrents(self):
        fields = ("d.hash=", "d.name=", "d.state=", "d.complete=", "d.size_bytes=", "d.completed_bytes=",
            "d.down.rate=", "d.up.rate=", "d.left_bytes=", "d.directory=", "d.custom1=")
        try:
            rows = self.call("d.multicall2", "", "main", *fields)
        except xmlrpc.client.Fault:
            rows = self.call("d.multicall", "main", *fields)
        return list(rows or [])[:500]


def _rtorrent_row(row):
    values = list(row or [])
    values += [None] * (11 - len(values))
    return dict(zip(("hash", "name", "state", "complete", "size", "completed", "down_rate", "up_rate", "left", "directory", "custom1"), values[:11]))


def rtorrent_status(row, settings=None):
    settings = validate_rtorrent_settings(settings or {"base_url": "http://invalid"})
    row = _rtorrent_row(row) if not isinstance(row, dict) else dict(row)
    size, completed = int(row.get("size") or 0), int(row.get("completed") or 0)
    progress = 100.0 if row.get("complete") else (completed * 100.0 / size if size else 0.0)
    state = "seeding" if row.get("complete") and row.get("state") else "completed" if row.get("complete") else "active" if row.get("state") else "paused"
    remote = _text(row.get("directory")); local = map_remote_path(remote, settings.get("path_mappings")) if remote else None
    return {"client": "rtorrent", "external_id": _text(row.get("hash")).lower(), "name": _text(row.get("name")),
        "status": state, "state": state, "transfer_state": state, "percent_complete": progress, "size_bytes": size,
        "downloaded_bytes": completed, "remaining_bytes": int(row.get("left") or max(0, size - completed)),
        "download_speed": int(row.get("down_rate") or 0), "upload_speed": int(row.get("up_rate") or 0),
        "output_path": local, "completed_output_path": local if state in {"completed", "seeding"} else None}


def rtorrent_find_existing(client, info_hash):
    target = _text(info_hash).lower()
    return next((_rtorrent_row(row) for row in client.torrents() if _text(_rtorrent_row(row).get("hash")).lower() == target), None)


def rtorrent_test_connection(settings, http=None):
    try:
        client = RTorrentXmlRpc(settings, http=http)
        version = _text(client.call("system.client_version"))
        client.torrents()
        return {"ok": True, "client": "rtorrent", "version": version,
            "capabilities": {"xmlrpc_http": True, "raw_scgi": False, "bounded_multicall": True},
            "settings": _public_client_settings(client.settings)}
    except ValueError as exc:
        return {"ok": False, "client": "rtorrent", "error_type": "configuration", "reason": str(exc)}
    except xmlrpc.client.Fault:
        return {"ok": False, "client": "rtorrent", "error_type": "protocol", "reason": "XML-RPC fault"}
    except Exception as exc:
        return {"ok": False, "client": "rtorrent", "error_type": "connectivity", "reason": type(exc).__name__}


def rtorrent_add(download_url, title, settings, *, dry_run=False, unique_tag=None, http=None, fetch_http=None):
    settings = validate_rtorrent_settings(settings)
    info_hash, blob = _fetch_metainfo(download_url, settings, http=fetch_http)
    handoff_tag = handoff_identity(title, download_url, unique_tag)
    if dry_run:
        return {"ok": True, "dry_run": True, "download_client": "rTorrent", "torrent_hash": info_hash,
            "client_external_id": info_hash, "handoff_tag": handoff_tag, "added": False}
    client = RTorrentXmlRpc(settings, http=http)
    existing = rtorrent_find_existing(client, info_hash)
    if not existing:
        commands = (f"d.directory.set={settings['download_path']}", f"d.custom1.set={handoff_tag}")
        if blob is None:
            client.call("load.start", "", download_url, *commands)
        else:
            client.call("load.raw_start", "", xmlrpc.client.Binary(blob), *commands)
        existing = rtorrent_find_existing(client, info_hash) or {"hash": info_hash, "name": title, "state": 1,
            "directory": settings["download_path"], "custom1": handoff_tag}
        added = True
    else:
        added = False
    snapshot = rtorrent_status(existing, settings)
    return {"ok": True, "download_client": "rTorrent", "protocol": "torrent", "client_external_id": info_hash,
        "torrent_hash": info_hash, "handoff_tag": handoff_tag, "added": added, "existing": not added,
        "category": settings["category"], "save_path": settings["download_path"], "settings_source": settings["source"],
        "download_client_snapshot": snapshot, "local_path": snapshot.get("output_path")}


def rtorrent_control(settings, torrent_id, action, *, http=None):
    if action not in {"pause", "resume", "remove"}:
        return {"ok": False, "client": "rtorrent", "action": action, "unsupported": True}
    client = RTorrentXmlRpc(settings, http=http)
    client.call({"pause": "d.stop", "resume": "d.start", "remove": "d.erase"}[action], _text(torrent_id))
    return {"ok": True, "client": "rtorrent", "action": action, "delete_data": False if action == "remove" else None}
