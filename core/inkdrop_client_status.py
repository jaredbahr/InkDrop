#!/usr/bin/env python3
"""Cached, nullable download-client observability contracts."""

from __future__ import annotations

import threading
import time

from core.inkdrop_transfer import normalize_transfer_status


CLIENT_NAMES = {
    "qbittorrent": "qBittorrent",
    "sabnzbd": "SABnzbd",
    "slskd": "SLSKD",
    "transmission": "Transmission",
    "deluge": "Deluge",
    "nzbget": "NZBGet",
    "utorrent": "uTorrent",
    "rtorrent": "rTorrent",
}


def canonical_client_id(value):
    key = str(value or "").strip().lower().replace("-", "").replace("_", "")
    return {
        "qbit": "qbittorrent",
        "qb": "qbittorrent",
        "qbittorrent": "qbittorrent",
        "sab": "sabnzbd",
        "sabnzbd": "sabnzbd",
        "soulseek": "slskd",
        "slskd": "slskd",
        "transmission": "transmission",
        "deluge": "deluge",
        "nzbget": "nzbget",
        "utorrent": "utorrent",
        "utorrentweb": "utorrent",
        "rtorrent": "rtorrent",
        "rtorrentxmlrpc": "rtorrent",
    }.get(key, str(value or "").strip().lower())


def _integer(value):
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def client_status_contract(client_id, *, health=None, snapshots=None, effective=None, polled_at=None):
    client_id = canonical_client_id(client_id)
    health = health if isinstance(health, dict) else {}
    effective = effective if isinstance(effective, dict) else {}
    snapshots = [item for item in (snapshots or []) if isinstance(item, dict)]
    polled_at = float(polled_at or time.time())
    available = bool(health.get("api_reachable", health.get("available", health.get("ok", False))))
    health_state = str(health.get("state") or health.get("health_status") or "unavailable").strip().lower()
    enabled = bool(effective.get("enabled", health_state != "disabled"))
    configured = bool(effective.get("configured", enabled and health_state not in {"disabled", "configuration_needed", "unconfigured"}))
    normalized = []
    for snapshot in snapshots:
        task = {
            "download_client": client_id,
            "external_id": snapshot.get("external_id"),
            "title": snapshot.get("title") or snapshot.get("name"),
            "status": snapshot.get("status"),
            "state": snapshot.get("state") or snapshot.get("client_state"),
            "size_bytes": snapshot.get("size_bytes"),
            "progress": snapshot.get("progress"),
            "started_at": snapshot.get("started_at") or snapshot.get("added_on"),
            "updated_at": snapshot.get("updated_at") or polled_at,
            "completed_at": snapshot.get("completed_at") or snapshot.get("completion_on"),
        }
        item = normalize_transfer_status(task, snapshot, now=polled_at)
        item["download_client_instance_id"] = snapshot.get("download_client_instance_id")
        normalized.append(item)
    states = [item.get("transfer_state") for item in normalized]
    active_states = {"downloading", "stalled"}
    queued_states = {"queued", "paused"}
    complete_states = {"completed", "seeding"}
    rates = [item.get("download_rate_bytes_per_second") for item in normalized if item.get("download_rate_bytes_per_second") is not None]
    upload_rates = [item.get("upload_rate_bytes_per_second") for item in normalized if item.get("upload_rate_bytes_per_second") is not None]
    remaining = []
    for item in normalized:
        total = _integer(item.get("bytes_total"))
        completed = _integer(item.get("bytes_completed"))
        if total is not None and completed is not None:
            remaining.append(max(0, total - completed))
    etas = [item.get("eta_seconds") for item in normalized if item.get("eta_seconds") is not None and item.get("transfer_state") in active_states]
    return {
        "schema": "inkdrop.download_client_status.v1",
        "client_id": client_id,
        "display_name": CLIENT_NAMES.get(client_id, client_id or "Download client"),
        "configured": configured,
        "enabled": enabled,
        "connected": available,
        "version": health.get("version"),
        "paused": health.get("paused"),
        "healthy": health_state == "healthy",
        "health_status": health_state,
        "last_poll_at": polled_at,
        "last_success_at": polled_at if available else health.get("last_success_at"),
        "last_error": None if available else (health.get("error") or health.get("detail") or health.get("label")),
        "active_count": sum(state in active_states for state in states) if available else None,
        "queued_count": sum(state in queued_states for state in states) if available else None,
        "completed_count": sum(state in complete_states for state in states) if available else None,
        "failed_count": sum(state == "failed" for state in states) if available else None,
        "total_download_rate": sum(rates) if rates else None,
        "total_upload_rate": sum(upload_rates) if upload_rates else None,
        "remaining_bytes": sum(remaining) if remaining else (0 if available and normalized and all(state in complete_states for state in states) else None),
        "eta_seconds": max(etas) if etas else None,
        "category_summary": health.get("category_summary"),
        "path_mapping_status": health.get("path_mapping_status") or effective.get("path_mapping_status"),
        "soulseek_connected": health.get("soulseek_connected"),
        "soulseek_logged_in": health.get("soulseek_logged_in"),
        "soulseek_transitioning": health.get("soulseek_transitioning"),
        "staging_path_healthy": health.get("staging_path_healthy"),
        "transfer_count": len(normalized) if available else None,
        "transfers": normalized[:100],
        "stale": False,
        "stale_age_seconds": 0,
    }


class ClientStatusCache:
    """Small stale-while-refresh cache; no provider call occurs while holding DB state."""

    def __init__(self, ttl_seconds=15, stale_seconds=120):
        self.ttl_seconds = max(0.01, float(ttl_seconds))
        self.stale_seconds = max(self.ttl_seconds, float(stale_seconds))
        self._lock = threading.Lock()
        self._entries = {}
        self._refreshing = set()

    def _refresh(self, client_id, fetcher):
        try:
            payload = fetcher(client_id)
            if not isinstance(payload, dict):
                raise TypeError("client status fetcher returned a non-object")
            fetched_at = time.time()
            with self._lock:
                self._entries[client_id] = (fetched_at, payload)
        finally:
            with self._lock:
                self._refreshing.discard(client_id)

    def get(self, client_id, fetcher, *, force=False):
        client_id = canonical_client_id(client_id)
        now = time.time()
        with self._lock:
            entry = self._entries.get(client_id)
            age = now - entry[0] if entry else None
            if entry and not force and age <= self.ttl_seconds:
                return dict(entry[1])
            if entry and not force:
                payload = dict(entry[1])
                payload["stale"] = True
                payload["stale_age_seconds"] = round(max(0.0, age), 3)
                if client_id not in self._refreshing:
                    self._refreshing.add(client_id)
                    threading.Thread(target=self._refresh, args=(client_id, fetcher), daemon=True).start()
                return payload
            if client_id in self._refreshing and entry:
                payload = dict(entry[1])
                payload["stale"] = True
                payload["stale_age_seconds"] = round(max(0.0, age or 0), 3)
                return payload
            self._refreshing.add(client_id)
        self._refresh(client_id, fetcher)
        with self._lock:
            return dict(self._entries[client_id][1])
