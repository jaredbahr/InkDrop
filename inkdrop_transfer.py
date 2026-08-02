#!/usr/bin/env python3
"""Normalize downloader-specific state into InkDrop transfer telemetry."""

from __future__ import annotations

import datetime as _dt
import re
import time


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


def _timestamp(value):
    number = _number(value)
    if number is not None:
        return number if number > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _duration_seconds(value):
    number = _number(value)
    if number is not None:
        return int(number) if 0 <= number < 31_536_000 else None
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "n/a", "-"}:
        return None
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})", text)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    return None


def _bytes_from_mib(value):
    number = _number(value)
    return int(number * 1024 * 1024) if number is not None and number >= 0 else None


def _rate_bytes(value):
    number = _number(value)
    if number is not None:
        return int(number) if number >= 0 else None
    text = str(value or "").strip().replace("/s", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b)", text, re.I)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    powers = {"b": 0, "kb": 1, "kib": 1, "mb": 2, "mib": 2, "gb": 3, "gib": 3, "tb": 4, "tib": 4}
    return int(amount * (1024 ** powers.get(unit, 0)))


def _client_name(task, raw):
    value = task.get("download_client") or task.get("client") or raw.get("client") or task.get("protocol") or task.get("source")
    text = str(value or "unknown").strip().lower()
    if text in {"qbit", "qb", "qbittorrent"}:
        return "qbittorrent"
    if text in {"sab", "sabnzbd"}:
        return "sabnzbd"
    if text in {"transmission", "transmissionbt"}:
        return "transmission"
    if text in {"deluge", "delugeweb"}:
        return "deluge"
    if text in {"nzbget", "nzb"}:
        return "nzbget"
    if text in {"utorrent", "utorrentweb", "utorrent_webui"}:
        return "utorrent"
    if text in {"rtorrent", "rtorrent_xmlrpc"}:
        return "rtorrent"
    return text


def _nested_raw(raw):
    candidates = [raw]
    for key in ("download_client_snapshot", "client_snapshot", "transfer", "raw"):
        value = raw.get(key)
        if isinstance(value, dict):
            candidates.append(value)
            nested = value.get("raw")
            if isinstance(nested, dict):
                candidates.append(nested)
    merged = {}
    for item in candidates:
        merged.update(item)
    return merged


def normalize_transfer_status(task, raw=None, now=None):
    """Return a stable, nullable telemetry contract for a download task."""
    task = task if isinstance(task, dict) else {}
    raw = _nested_raw(raw if isinstance(raw, dict) else {})
    now = time.time() if now is None else float(now)
    client = _client_name(task, raw)
    status = str(task.get("status") or raw.get("client_state") or raw.get("status") or "").strip().lower()
    native_state = str(raw.get("state") or raw.get("client_state") or task.get("state") or status).strip()

    total = _integer(raw.get("bytes_total") or raw.get("total_size") or raw.get("size_bytes") or raw.get("size") or task.get("size_bytes"))
    completed = _integer(raw.get("bytes_completed") or raw.get("downloaded_bytes") or raw.get("downloaded"))
    if client == "sabnzbd":
        total = total or _bytes_from_mib(raw.get("mb"))
        left = _bytes_from_mib(raw.get("mbleft"))
        if completed is None and total is not None and left is not None:
            completed = max(0, total - left)
    if client == "transmission":
        left = _integer(raw.get("leftUntilDone"))
        if completed is None and total is not None and left is not None:
            completed = max(0, total - left)
    if client == "deluge":
        total = total or _integer(raw.get("total_size"))
        completed = completed if completed is not None else _integer(raw.get("total_done"))
    if client == "nzbget":
        total = total or _bytes_from_mib(raw.get("FileSizeMB"))
        remaining = _bytes_from_mib(raw.get("RemainingSizeMB"))
        if completed is None and total is not None and remaining is not None:
            completed = max(0, total - remaining)
        downloaded = _bytes_from_mib(raw.get("DownloadedSizeMB"))
        if downloaded is not None:
            completed = downloaded

    percent = _number(raw.get("percent_complete"))
    native_progress = raw.get("progress") if raw.get("progress") not in (None, "") else task.get("progress")
    progress = _number(native_progress)
    if percent is None and progress is not None:
        percent = progress * 100.0 if 0 <= progress <= 1 else progress
    if percent is None and completed is not None and total:
        percent = completed * 100.0 / total
    if percent is not None:
        percent = round(max(0.0, min(100.0, percent)), 2)
    if completed is not None and total is not None:
        completed = min(completed, total)

    started_at = _timestamp(raw.get("started_at") or raw.get("added_on") or task.get("started_at"))
    updated_at = _timestamp(raw.get("last_updated_at") or raw.get("last_activity") or task.get("updated_at"))
    completed_at = _timestamp(raw.get("completed_at") or raw.get("completion_on") or task.get("completed_at"))
    terminal = any(token in status for token in ("complete", "verified", "failed", "cancel", "removed"))
    active = any(token in status for token in ("download", "transfer", "queued", "waiting", "started", "import")) and not terminal
    transfer_complete = bool((percent is not None and percent >= 100) or any(token in status for token in ("completed", "staged", "ready", "verified")))
    stalled = not transfer_complete and (
        "stall" in native_state.lower()
        or "stale" in status
        or (client == "transmission" and raw.get("isStalled") is True)
    )
    error = task.get("failure_reason") or raw.get("failure_reason") or raw.get("error") or raw.get("fail_message")

    if client == "nzbget" and status.startswith("success"):
        transfer_state, import_stage = "completed", None
    elif client == "nzbget" and status.startswith("deleted"):
        transfer_state, import_stage = "removed", None
    elif client == "nzbget" and status.startswith("failure"):
        transfer_state, import_stage = "failed", None
    elif client == "deluge" and status in {"error", "failed"}:
        transfer_state, import_stage = "failed", None
    elif "import" in status or str(task.get("state") or "").lower() == "importing":
        transfer_state, import_stage = "completed", status or "importing"
    elif any(token in status for token in ("failed", "error", "cancel")):
        transfer_state, import_stage = "failed", None
    elif client == "transmission" and status == "seeding":
        transfer_state, import_stage = "seeding", None
    elif client == "deluge" and status == "seeding":
        transfer_state, import_stage = "seeding", None
    elif transfer_complete:
        transfer_state, import_stage = "completed", None
    elif stalled:
        transfer_state, import_stage = "stalled", None
    elif client == "transmission" and status == "paused":
        transfer_state, import_stage = "paused", None
    elif client == "deluge" and status == "paused":
        transfer_state, import_stage = "paused", None
    elif client == "deluge" and status in {"queued", "checking"}:
        transfer_state, import_stage = "queued", None
    elif client == "deluge" and status in {"downloading", "allocating", "moving"}:
        transfer_state, import_stage = "downloading", None
    elif client == "nzbget" and status == "paused":
        transfer_state, import_stage = "paused", None
    elif client == "nzbget" and status in {"queued", "pp-queued"}:
        transfer_state, import_stage = "queued", None
    elif client == "nzbget" and status in {"downloading", "fetching", "post-processing", "moving", "repairing"}:
        transfer_state, import_stage = "downloading", None
    elif client in {"utorrent", "rtorrent"} and status == "seeding":
        transfer_state, import_stage = "seeding", None
    elif client in {"utorrent", "rtorrent"} and status == "paused":
        transfer_state, import_stage = "paused", None
    elif client in {"utorrent", "rtorrent"} and status == "active":
        transfer_state, import_stage = "downloading", None
    elif client in {"qbittorrent", "sabnzbd"} and status == "paused":
        transfer_state, import_stage = "paused", None
    elif "queue" in status or "wait" in status:
        transfer_state, import_stage = "queued", None
    elif active:
        transfer_state, import_stage = "downloading", None
    else:
        transfer_state, import_stage = "unknown", None

    rate = _rate_bytes(raw.get("download_rate_bytes_per_second") or raw.get("download_speed") or raw.get("dlspeed") or raw.get("speed"))
    eta = _duration_seconds(raw.get("eta_seconds") or raw.get("eta") or raw.get("timeleft"))
    if transfer_complete:
        eta = None
    elapsed = int(max(0, now - started_at)) if started_at else None
    next_step = {
        "queued": "Waiting for the download client to start",
        "downloading": "Downloader transfer is active",
        "stalled": "Waiting for downloader progress or automatic retry",
        "completed": "Transfer complete; import or verification is next",
        "paused": "Downloader transfer is paused",
        "removed": "Downloader item was removed",
        "seeding": "Torrent transfer is complete and seeding",
        "failed": "Automatic source retry is next when eligible",
        "unknown": "Waiting for the next downloader status refresh",
    }[transfer_state]
    if import_stage:
        next_step = "Import and verification are in progress"

    return {
        "client": client,
        "client_item_id": task.get("external_id") or raw.get("external_id") or raw.get("hash") or raw.get("nzo_id") or raw.get("transfer_id"),
        "transfer_state": transfer_state,
        "native_state": native_state or None,
        "progress_kind": "determinate" if percent is not None else "indeterminate",
        "percent_complete": percent,
        "bytes_completed": completed,
        "bytes_total": total,
        "download_rate_bytes_per_second": rate,
        "upload_rate_bytes_per_second": _rate_bytes(raw.get("upload_rate_bytes_per_second") or raw.get("upload_speed") or raw.get("upspeed")),
        "eta_seconds": eta,
        "elapsed_seconds": elapsed,
        "started_at": started_at,
        "last_updated_at": updated_at,
        "completed_at": completed_at,
        "stalled": bool(stalled),
        "stalled_reason": (native_state or status) if stalled else None,
        "client_error": str(error)[:500] if error else None,
        "import_stage": import_stage,
        "next_step": next_step,
        "source": task.get("source") or raw.get("source"),
        "provider": task.get("provider") or raw.get("provider"),
        "display_title": task.get("title") or raw.get("title") or raw.get("name"),
    }
