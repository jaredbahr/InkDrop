#!/usr/bin/env python3
"""Normalize downloader-specific state into InkDrop transfer telemetry."""

from __future__ import annotations

import datetime as _dt
import re
import time

from core.inkdrop_records import (
    TRANSFER_STATES,
    DownloadIdentity,
    TransferProgress,
    TransferStatus,
    coerce_client_id,
    coerce_transfer_state,
)


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
    # The alias table lives in inkdrop_records so a newly supported client is
    # taught to the codebase once; only the precedence order is local.
    value = task.get("download_client") or task.get("client") or raw.get("client") or task.get("protocol") or task.get("source")
    return coerce_client_id(value) or "unknown"


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


NZBGET_CLEAN_PAR = {"", "NONE", "SUCCESS"}
NZBGET_CLEAN_UNPACK = {"", "NONE", "SUCCESS"}
NZBGET_CLEAN_MOVE = {"", "NONE", "SUCCESS"}
NZBGET_CLEAN_SCRIPT = {"", "NONE", "SUCCESS"}
NZBGET_CLEAN_MARK = {"", "NONE"}
# SUCCESS/GOOD and SUCCESS/MARK are deliberately absent: NZBGet documents both
# as "marked as good by user", an operator override on an item it never
# verified itself. Everything here is a verification result.
NZBGET_CLEAN_HISTORY_STATUS = {
    "SUCCESS/ALL",
    "SUCCESS/UNPACK",
    "SUCCESS/PAR",
    "SUCCESS/HEALTH",
}
NZBGET_FULL_HEALTH = 1000


def _nzbget_snake(key):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def _nzbget_field(row, key):
    """Read an NZBGet field across the shapes it reaches us in.

    Raw JSON-RPC rows use PascalCase (``ParStatus``); InkDrop's own
    normalized snapshots lowercase the same values (``status``,
    ``par_status``). Miss either and a health signal reads as absent, which
    is exactly the silent-success failure this module exists to prevent.
    """
    row = row or {}
    for candidate in (key, key.lower(), _nzbget_snake(key)):
        if candidate in row:
            value = row.get(candidate)
            if value not in (None, ""):
                return str(value).strip().upper()
    return ""


def nzbget_health_verdict(row):
    """Judge an NZBGet row's post-processing health.

    Returns ``(verdict, reason)`` where verdict is one of ``clean``,
    ``damaged`` or ``unknown``. This is an allowlist: only outcomes NZBGet
    explicitly reports as good count as ``clean``. Anything damaged,
    force-marked, or simply unrecognized fails closed so a download that was
    never verified cannot be imported as if it were.
    """
    row = row if isinstance(row, dict) else {}

    par = _nzbget_field(row, "ParStatus")
    if par and par not in NZBGET_CLEAN_PAR:
        # REPAIR_POSSIBLE means damaged and *not* repaired; MANUAL means a
        # human still has to run the repair. Neither is a finished download.
        return "damaged", f"NZBGet par check reported {par}"

    unpack = _nzbget_field(row, "UnpackStatus")
    if unpack and unpack not in NZBGET_CLEAN_UNPACK:
        return "damaged", f"NZBGet unpack reported {unpack}"

    move = _nzbget_field(row, "MoveStatus")
    if move and move not in NZBGET_CLEAN_MOVE:
        return "damaged", f"NZBGet move reported {move}"

    script = _nzbget_field(row, "ScriptStatus")
    if script == "FAILURE":
        return "damaged", "NZBGet post-processing script failed"
    if script and script not in NZBGET_CLEAN_SCRIPT:
        return "unknown", f"NZBGet script status is {script}"

    # NZBGet also reports per-script results as an array; the scalar above is
    # only a summary and can be absent when the array is present.
    for entry in (row.get("ScriptStatuses") or row.get("script_statuses") or []):
        if not isinstance(entry, dict):
            continue
        entry_status = _nzbget_field(entry, "Status")
        if entry_status == "FAILURE":
            name = str(entry.get("Name") or entry.get("name") or "post-processing").strip()
            return "damaged", f"NZBGet script {name} failed"
        if entry_status and entry_status not in NZBGET_CLEAN_SCRIPT:
            return "unknown", f"NZBGet script status is {entry_status}"

    mark = _nzbget_field(row, "MarkStatus")
    if mark and mark not in NZBGET_CLEAN_MARK:
        # A force-marked item is an operator override on a download NZBGet
        # itself never verified. Surface it rather than importing silently.
        return "damaged", f"NZBGet item was force-marked {mark}"

    # Health is per-mille; 1000 is a complete, undamaged download. Anything
    # short of that means articles were lost, and only a successful par repair
    # puts them back. Without that evidence the download is still damaged -
    # comparing against CriticalHealth alone misses every partial download
    # that has no CriticalHealth reported.
    repaired = par == "SUCCESS"
    health = _integer(_nzbget_field(row, "Health"))
    critical = _integer(_nzbget_field(row, "CriticalHealth"))
    if health is not None and critical is not None and critical > 0 and health < critical:
        return "damaged", f"NZBGet health {health} is below critical {critical}"
    if health is not None and health < NZBGET_FULL_HEALTH and not repaired:
        return "damaged", f"NZBGet health is {health}/{NZBGET_FULL_HEALTH} with no successful repair"

    failed_articles = _integer(_nzbget_field(row, "FailedArticles"))
    if failed_articles and not repaired:
        return "damaged", f"NZBGet lost {failed_articles} article(s) with no successful repair"

    delete_status = _nzbget_field(row, "DeleteStatus")
    if delete_status and delete_status != "NONE":
        return "damaged", f"NZBGet deleted the item ({delete_status})"

    status = _nzbget_field(row, "Status")
    if not status:
        return "unknown", "NZBGet history row has no Status"
    if status.startswith("SUCCESS"):
        if status in NZBGET_CLEAN_HISTORY_STATUS:
            return "clean", ""
        # e.g. SUCCESS/MARK - success asserted by hand, not by verification.
        return "damaged", f"NZBGet reported {status}"
    if status.startswith("DELETED"):
        return "damaged", f"NZBGet reported {status}"
    if status.startswith("FAILURE"):
        return "damaged", f"NZBGet reported {status}"
    if status.startswith("WARNING"):
        return "damaged", f"NZBGet reported {status}"
    return "unknown", f"NZBGet reported an unrecognized status ({status})"


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

    # Progress arrives with its unit declared wherever InkDrop controls the
    # producer: `percent_complete` is 0-100 and `progress_fraction` is 0-1.
    # Both are read before the legacy `progress` key, whose unit is not
    # declared anywhere and which therefore still has to be guessed by
    # magnitude. That guess is why a SABnzbd download at 1% once read as
    # complete, so every InkDrop-side producer now emits an explicit key and
    # only third-party payloads reach the fallback.
    percent = _number(raw.get("percent_complete"))
    if percent is None:
        fraction = _number(raw.get("progress_fraction"))
        if fraction is None:
            fraction = _number(task.get("progress_fraction"))
        if fraction is not None:
            percent = fraction * 100.0
    if percent is None:
        native_progress = raw.get("progress") if raw.get("progress") not in (None, "") else task.get("progress")
        progress = _number(native_progress)
        if progress is not None:
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

    # A payload that already carries a canonical InkDrop state was produced by
    # a status builder that could see the client's own row - including the
    # NZBGet post-processing verdict, which this function can only re-derive
    # from whatever survived the trip. Trust that verdict instead of deciding
    # again from a lossier copy: re-deriving is how one row ended up with two
    # different answers depending on which module asked.
    upstream_state = ""
    if str(raw.get("transfer_state") or "").strip().lower() in TRANSFER_STATES:
        upstream_state = coerce_transfer_state(raw.get("transfer_state"))

    nzbget_terminal = client == "nzbget" and (
        str(raw.get("_inkdrop_section") or "").strip().lower() == "history"
        or status.startswith(("success", "deleted", "failure", "warning"))
    )
    if upstream_state:
        transfer_state, import_stage = upstream_state, None
        if transfer_state == "failed" and not error:
            error = raw.get("health_reason") or raw.get("client_error")
    elif nzbget_terminal:
        verdict, verdict_reason = nzbget_health_verdict(raw)
        if status.startswith("deleted"):
            transfer_state, import_stage = "removed", None
        elif verdict == "clean":
            transfer_state, import_stage = "completed", None
        else:
            # Terminal either way - a history row will not resolve on a later
            # poll, so an unknown outcome is a failure to review rather than
            # something still in flight.
            transfer_state, import_stage = "failed", None
        if verdict != "clean" and not error:
            error = verdict_reason
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
    elif status == "unknown":
        # An explicit "we do not know" must never be upgraded to a completion
        # by the percent heuristic below; 100% of bytes on disk says nothing
        # about whether those bytes are intact.
        transfer_state, import_stage = "unknown", None
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

    # Identity comes from one alias table rather than a fallback chain written
    # out here. The chain that used to live at this line knew `hash` and
    # `nzo_id` but not `torrent_hash`, so every Deluge, Transmission, uTorrent
    # and rTorrent transfer reported no client item id at all.
    identity = DownloadIdentity.from_mapping(task, client=client)
    if not identity.resolved:
        identity = DownloadIdentity.from_mapping(raw, client=client)

    verdict = str(raw.get("health_verdict") or "").strip().lower()
    return TransferStatus(
        identity=identity,
        state=transfer_state,
        native_state=native_state,
        progress=TransferProgress.from_percent(percent, bytes_completed=completed, bytes_total=total),
        download_rate_bytes_per_second=rate,
        upload_rate_bytes_per_second=_rate_bytes(
            raw.get("upload_rate_bytes_per_second") or raw.get("upload_speed") or raw.get("upspeed")
        ),
        eta_seconds=eta,
        elapsed_seconds=elapsed,
        started_at=started_at,
        last_updated_at=updated_at,
        completed_at=completed_at,
        stalled_reason=(native_state or status) if stalled else "",
        client_error=str(error) if error else "",
        import_stage=import_stage or "",
        health_verdict=verdict,
        health_reason=raw.get("health_reason") or "",
        completed_output_path=raw.get("completed_output_path") or "",
        source=task.get("source") or raw.get("source") or "",
        provider=task.get("provider") or raw.get("provider") or "",
        display_title=task.get("title") or raw.get("title") or raw.get("name") or "",
    ).to_dict()
