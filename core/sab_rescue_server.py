#!/usr/bin/env python3
"""Small SABnzbd rescue/status helper for Homepage.

The helper reads the existing SAB API key from Homepage config, reports
post-processing items that have stayed in Waiting/Queued/Unpacking-style states,
and exposes a manual restart button. It does not delete SAB jobs or files.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import base64
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from core import inkdrop_runtime_config
except Exception:
    inkdrop_runtime_config = None


SAB_URL_ENV = os.environ.get("SAB_RESCUE_SAB_URL") or os.environ.get("INKDROP_SABNZBD_URL") or ""
SAB_URL_DEFAULT = "http://sabnzbd:8080"
# Kept for callers that only want the static value; sab_base_url() is what the
# API path uses, because the operator's configured address has to win.
SAB_URL = SAB_URL_ENV or SAB_URL_DEFAULT
HOMEPAGE_SERVICES = Path(os.environ.get("SAB_RESCUE_HOMEPAGE_SERVICES") or "/config/homepage/services.yaml")
STATE_PATH = Path(os.environ.get("SAB_RESCUE_STATE_PATH") or "/state/sab-rescue-state.json")
OUT_PATH = Path(os.environ.get("SAB_RESCUE_OUT_PATH") or "/state/sab-rescue-status.json")
PORT = int(os.environ.get("SAB_RESCUE_PORT") or "8788")
FILESERV_SSH_KEY = Path(os.environ.get("SAB_RESCUE_SSH_KEY") or "/config/sab-rescue/ssh_key")
FILESERV_SSH_TARGET = os.environ.get("SAB_RESCUE_SSH_TARGET") or ""
FILESERV_RESCUE_SCRIPT = os.environ.get("SAB_RESCUE_SCRIPT") or ""
FILESERV_QUARANTINE_ROOT = os.environ.get("SAB_RESCUE_QUARANTINE_ROOT") or "/staging/sab-rescue-quarantine"

STUCK_STATUSES = {
    "queued",
    "waiting",
    "extracting",
    "unpacking",
    "verifying",
    "repairing",
    "fetching",
    "moving",
}
CLEANUP_ELIGIBLE_STATUSES = {"extracting", "unpacking", "verifying", "repairing"}
SAFE_SAB_PATH_RE = re.compile(r"^[A-Z]:\\Temp\\(?:Incomplete|Downloads)\\[^\\].+", re.I)
WARN_AFTER_MINUTES = 30
CRITICAL_AFTER_MINUTES = 60
AUTO_RESTART_COOLDOWN_MINUTES = 60
POST_RESTART_VERIFY_SECONDS = 35
AUTO_QUARANTINE_COOLDOWN_MINUTES = 180


def iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_sab_key_from_env() -> str:
    for name in (
        "SAB_RESCUE_API_KEY",
        "INKDROP_SABNZBD_API_KEY",
        "INKDROP_SAB_API_KEY",
        "SABNZBD_API_KEY",
    ):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def inkdrop_state_db_path() -> Path:
    explicit = os.environ.get("INKDROP_STATE_DB")
    if explicit:
        return Path(explicit)
    if inkdrop_runtime_config:
        try:
            return inkdrop_runtime_config.state_db_path()
        except Exception:
            pass
    state_dir = Path(os.environ.get("INKDROP_STATE_DIR") or "/state")
    return state_dir / "inkdrop-state.sqlite3"


def read_sab_key_from_inkdrop_settings() -> str:
    db_path = inkdrop_state_db_path()
    if not db_path.exists():
        return ""
    try:
        con = sqlite3.connect(str(db_path), timeout=5)
        con.row_factory = sqlite3.Row
        try:
            exists = con.execute(
                "select 1 from sqlite_master where type='table' and name='provider_configs'"
            ).fetchone()
            if not exists:
                return ""
            row = con.execute(
                "select settings_json from provider_configs where id='sabnzbd' limit 1"
            ).fetchone()
            if not row:
                return ""
            try:
                settings = json.loads(row["settings_json"] or "{}")
            except ValueError:
                return ""
            if not isinstance(settings, dict):
                return ""
            return str(settings.get("api_key") or "").strip()
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def read_sab_base_url_from_inkdrop_settings() -> str:
    """Read SAB's address from the provider record.

    ``base_url`` is a column on ``provider_configs``, not a key inside
    ``settings_json`` -- the settings blob is only the editable provider
    settings, and SABnzbd does not list ``base_url`` among them.
    """

    db_path = inkdrop_state_db_path()
    if not db_path.exists():
        return ""
    try:
        con = sqlite3.connect(str(db_path), timeout=5)
        con.row_factory = sqlite3.Row
        try:
            exists = con.execute(
                "select 1 from sqlite_master where type='table' and name='provider_configs'"
            ).fetchone()
            if not exists:
                return ""
            row = con.execute(
                "select base_url from provider_configs where id='sabnzbd' limit 1"
            ).fetchone()
            if not row:
                return ""
            return str(row["base_url"] or "").strip().rstrip("/")
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def sab_base_url() -> str:
    """Resolve SAB's address the same way the API key is resolved.

    An explicit environment override wins, then whatever the operator saved in
    Settings, then the compose-style default. The default assumes SAB is a
    sibling container named ``sabnzbd``; when it is not, the configured Base URL
    is the only address that works, so it must not be ignored.
    """

    if SAB_URL_ENV:
        return SAB_URL_ENV.rstrip("/")
    configured = read_sab_base_url_from_inkdrop_settings()
    if configured:
        return configured
    return SAB_URL_DEFAULT


def read_sab_key_from_homepage() -> str:
    if not HOMEPAGE_SERVICES.exists():
        return ""
    text = HOMEPAGE_SERVICES.read_text(errors="ignore")
    sab_block = re.search(r"type:\s*sabnzbd\b(?P<body>.*?)(?:\n\s*-\s+\w|\Z)", text, re.S)
    if not sab_block:
        return ""
    key_match = re.search(r"\n\s*key:\s*([^\s#]+)", sab_block.group("body"))
    if not key_match:
        return ""
    return key_match.group(1).strip()


def read_sab_key() -> str:
    key = read_sab_key_from_env()
    if key:
        return key
    key = read_sab_key_from_inkdrop_settings()
    if key:
        return key
    key = read_sab_key_from_homepage()
    if key:
        return key
    raise RuntimeError(
        "SAB API key is not configured; set SAB_RESCUE_API_KEY/INKDROP_SABNZBD_API_KEY "
        "or save the SABnzbd provider API key in InkDrop settings"
    )


def sab_api(mode: str, **extra: str) -> dict[str, Any]:
    query = {"mode": mode, "output": "json", "apikey": read_sab_key()}
    query.update(extra)
    url = f"{sab_base_url()}/api?{urllib.parse.urlencode(query)}"
    with urllib.request.urlopen(url, timeout=25) as response:
        raw = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def normalize_status(value: Any) -> str:
    text = str(value or "").strip()
    if ":" in text:
        text = text.split(":", 1)[0]
    return text


def history_slots() -> list[dict[str, Any]]:
    history = sab_api("history").get("history", {})
    slots = history.get("slots") if isinstance(history, dict) else []
    return [slot for slot in slots or [] if isinstance(slot, dict)]


def stuck_key(item: dict[str, Any]) -> str:
    return f"{item.get('nzo_id') or ''}:{str(item.get('status') or '').lower()}"


def item_key(item: dict[str, Any]) -> str:
    return f"{item.get('nzo_id') or item.get('name') or ''}:{normalize_status(item.get('status')).lower()}"


def safe_filename(value: str, limit: int = 96) -> str:
    text = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" ._")
    text = re.sub(r"\s+", " ", text)
    return (text or "sab-job")[:limit]


def validate_sab_cleanup_path(path: Any) -> tuple[bool, str]:
    text = str(path or "").strip()
    if not text:
        return False, "missing path"
    normalized = text.replace("/", "\\")
    if ".." in normalized:
        return False, "path traversal marker"
    if not SAFE_SAB_PATH_RE.match(normalized):
        return False, "outside allowed SAB temp/download roots"
    lowered = normalized.lower()
    blocked_words = ("\\movies\\", "\\tv\\", "\\anime\\", "\\music\\", "\\comics\\", "\\ebooks\\", "\\media\\")
    if lowered.startswith(("h:\\movies\\", "h:\\tv\\", "h:\\anime\\", "h:\\music\\", "h:\\comics\\", "h:\\ebooks\\")):
        return False, "media/library root blocked"
    if any(word in lowered for word in blocked_words) and not lowered.startswith("h:\\temp\\"):
        return False, "media-like path blocked"
    parts = [part for part in normalized.split("\\") if part]
    if len(parts) < 4:
        return False, "path is too broad"
    return True, normalized


def fileserver_powershell(script: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    cmd = [
        "ssh",
        "-i",
        str(FILESERV_SSH_KEY),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=8",
        FILESERV_SSH_TARGET,
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def inspect_cleanup_candidate(item: dict[str, Any]) -> dict[str, Any]:
    status = normalize_status(item.get("status"))
    status_key = status.lower()
    ok_path, safe_path_or_reason = validate_sab_cleanup_path(item.get("path"))
    eligible = status_key in CLEANUP_ELIGIBLE_STATUSES and ok_path
    reason = "eligible"
    if status_key not in CLEANUP_ELIGIBLE_STATUSES:
        reason = "status not cleanup eligible"
    elif not ok_path:
        reason = safe_path_or_reason
    return {
        "name": str(item.get("name") or "")[:180],
        "nzo_id": str(item.get("nzo_id") or ""),
        "status": status,
        "category": item.get("category"),
        "age_minutes": item.get("age_minutes"),
        "size": item.get("size"),
        "path": safe_path_or_reason if ok_path else str(item.get("path") or ""),
        "safe_path": ok_path,
        "cleanup_eligible": eligible,
        "cleanup_reason": reason,
    }


def quarantine_sab_job(item: dict[str, Any], *, dry_run: bool, reason: str) -> dict[str, Any]:
    ok_path, safe_path_or_reason = validate_sab_cleanup_path(item.get("path"))
    status = normalize_status(item.get("status"))
    if status.lower() not in CLEANUP_ELIGIBLE_STATUSES:
        raise RuntimeError(f"status {status!r} is not eligible for quarantine")
    if not ok_path:
        raise RuntimeError(f"unsafe cleanup path: {safe_path_or_reason}")
    source = safe_path_or_reason
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target_name = safe_filename(str(item.get("name") or item.get("nzo_id") or "sab-job"))
    target = f"{FILESERV_QUARANTINE_ROOT}\\{stamp}-{target_name}"
    if dry_run:
        event = {
            "requested_at": iso(),
            "action": "quarantine_dry_run",
            "reason": reason,
            "name": item.get("name"),
            "nzo_id": item.get("nzo_id"),
            "status": status,
            "source_path": source,
            "quarantine_path": target,
            "result": "would move safe SAB temp path",
        }
        return log_event(event)
    ps = f"""
$ErrorActionPreference = 'Stop'
$source = @'
{source}
'@
$target = @'
{target}
'@
if (-not (Test-Path -LiteralPath $source)) {{
  throw "Source path not found: $source"
}}
$parent = Split-Path -Parent $target
New-Item -ItemType Directory -Force -Path $parent | Out-Null
Move-Item -LiteralPath $source -Destination $target -Force
$size = 0
if (Test-Path -LiteralPath $target) {{
  $size = (Get-ChildItem -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
}}
[pscustomobject]@{{ source=$source; target=$target; bytes=[int64]$size }} | ConvertTo-Json -Compress
"""
    proc = fileserver_powershell(ps, timeout=900)
    event = {
        "requested_at": iso(),
        "action": "quarantine_job",
        "reason": reason,
        "name": item.get("name"),
        "nzo_id": item.get("nzo_id"),
        "status": status,
        "source_path": source,
        "quarantine_path": target,
        "returncode": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-1000:],
    }
    return log_event(event)


def stop_sab_for_quarantine(reason: str) -> dict[str, Any]:
    ps = r"""
$ErrorActionPreference = 'SilentlyContinue'
$taskName = 'SABnzbd Interactive Rescue'
$actions = @()
try {
  Stop-ScheduledTask -TaskName $taskName | Out-Null
  $actions += "stopped existing scheduled task"
} catch {}
$killNames = @(
  'SABnzbd.exe',
  'SABnzbd-console.exe',
  'par2.exe',
  'par2j.exe',
  'unrar.exe',
  'rar.exe',
  '7z.exe',
  '7za.exe',
  '7zr.exe'
)
foreach ($name in $killNames) {
  $before = Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($name)) -ErrorAction SilentlyContinue
  if ($before) {
    & taskkill.exe /F /IM $name /T | Out-Null
    $actions += "killed $name"
  }
}
Start-Sleep -Seconds 7
[pscustomobject]@{ actions=$actions } | ConvertTo-Json -Compress
"""
    proc = fileserver_powershell(ps, timeout=180)
    return log_event(
        {
            "requested_at": iso(),
            "action": "stop_before_quarantine",
            "reason": reason,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-2000:],
            "stderr": proc.stderr[-1000:],
        }
    )


def cleanup_candidates_from_slots(slots: list[dict[str, Any]], seen: dict[str, Any], now: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for slot in slots:
        status = normalize_status(slot.get("status"))
        status_key = status.lower()
        if status_key not in CLEANUP_ELIGIBLE_STATUSES:
            continue
        nzo_id = str(slot.get("nzo_id") or slot.get("name") or "")
        if not nzo_id:
            continue
        key = f"{nzo_id}:{status_key}"
        first_seen = float(seen.get(key) or now)
        age_minutes = round((now - first_seen) / 60.0, 1)
        candidate = inspect_cleanup_candidate(
            {
                **slot,
                "status": status,
                "age_minutes": age_minutes,
            }
        )
        candidates.append(candidate)
    return candidates


def select_cleanup_candidate(candidates: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any] | None:
    rescued = set(state.get("last_rescue_stuck_keys") or [])
    eligible = [
        item
        for item in candidates
        if item.get("cleanup_eligible")
        and item_key(item) in rescued
        and float(item.get("age_minutes") or 0) >= CRITICAL_AFTER_MINUTES
    ]
    eligible.sort(key=lambda item: float(item.get("age_minutes") or 0), reverse=True)
    return eligible[0] if eligible else None


def select_manual_cleanup_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [item for item in candidates if item.get("cleanup_eligible")]
    eligible.sort(key=lambda item: float(item.get("age_minutes") or 0), reverse=True)
    return eligible[0] if eligible else None


def rescue_status(state: dict[str, Any], stuck: list[dict[str, Any]]) -> tuple[str, str]:
    last_at = str(state.get("last_hard_rescue_at") or "")
    if not last_at:
        return "OK", "No hard rescue recorded"
    triggered = set(state.get("last_rescue_stuck_keys") or [])
    current = {stuck_key(item) for item in stuck}
    unresolved = sorted(triggered & current)
    if unresolved:
        return "WARN", f"{len(unresolved)} item(s) still stuck after last rescue"
    return "OK", f"Last hard rescue clear at {last_at}"


def build_status(allow_auto_restart: bool = True) -> dict[str, Any]:
    now = time.time()
    state = load_json(STATE_PATH, {})
    seen = state.get("seen", {}) if isinstance(state, dict) else {}
    if not isinstance(seen, dict):
        seen = {}

    active: dict[str, Any] = {}
    stuck: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    api_error = ""

    try:
        slots = history_slots()
    except Exception as exc:
        slots = []
        api_error = f"{type(exc).__name__}: {exc}"

    for slot in slots:
        status = normalize_status(slot.get("status"))
        status_key = status.lower()
        if status_key not in STUCK_STATUSES:
            continue
        nzo_id = str(slot.get("nzo_id") or slot.get("name") or "")
        if not nzo_id:
            continue
        key = f"{nzo_id}:{status_key}"
        first_seen = float(seen.get(key) or now)
        active[key] = first_seen
        age_minutes = round((now - first_seen) / 60.0, 1)
        counts[status] = counts.get(status, 0) + 1
        if age_minutes >= WARN_AFTER_MINUTES:
            stuck.append(
                {
                    "name": str(slot.get("name") or "")[:140],
                    "nzo_id": nzo_id,
                    "status": status,
                    "category": slot.get("category"),
                    "age_minutes": age_minutes,
                    "size": slot.get("size"),
                }
            )

    oldest = max((item["age_minutes"] for item in stuck), default=0)
    if api_error:
        status = "WARN"
        detail = "SAB API unreachable"
    elif oldest >= CRITICAL_AFTER_MINUTES:
        status = "WARN"
        detail = f"{len(stuck)} stuck, oldest {oldest:.0f}m"
    elif stuck:
        status = "WATCH"
        detail = f"{len(stuck)} slow, oldest {oldest:.0f}m"
    else:
        status = "OK"
        detail = "No stuck post-processing"

    cleanup_candidates = cleanup_candidates_from_slots(slots, seen, now) if not api_error else []
    auto_rescue = None
    auto_quarantine = None
    last_hard_rescue_epoch = float(state.get("last_hard_rescue_epoch") or 0) if isinstance(state, dict) else 0
    last_hard_rescue_at = str(state.get("last_hard_rescue_at") or "") if isinstance(state, dict) else ""
    last_quarantine_epoch = float(state.get("last_quarantine_epoch") or 0) if isinstance(state, dict) else 0
    last_quarantine_at = str(state.get("last_quarantine_at") or "") if isinstance(state, dict) else ""
    last_quarantine_path = str(state.get("last_quarantine_path") or "") if isinstance(state, dict) else ""

    if allow_auto_restart and not api_error:
        quarantine_candidate = select_cleanup_candidate(cleanup_candidates, state if isinstance(state, dict) else {})
        quarantine_cooldown = AUTO_QUARANTINE_COOLDOWN_MINUTES * 60
        if quarantine_candidate and now - last_quarantine_epoch >= quarantine_cooldown:
            stop_event = stop_sab_for_quarantine("auto-stuck-after-hard-rescue")
            auto_quarantine = quarantine_sab_job(
                quarantine_candidate,
                dry_run=False,
                reason="auto-stuck-after-hard-rescue",
            )
            auto_quarantine["pre_quarantine_stop"] = stop_event
            if auto_quarantine.get("returncode") == 0:
                last_quarantine_epoch = now
                last_quarantine_at = iso()
                last_quarantine_path = str(auto_quarantine.get("quarantine_path") or "")
                state["last_quarantine_job"] = {
                    "name": quarantine_candidate.get("name"),
                    "nzo_id": quarantine_candidate.get("nzo_id"),
                    "status": quarantine_candidate.get("status"),
                    "path": quarantine_candidate.get("path"),
                }
            auto_rescue = hard_restart_sab("auto-post-quarantine-restart")
            last_hard_rescue_epoch = now
            last_hard_rescue_at = iso()
            state["last_rescue_stuck_keys"] = [stuck_key(item) for item in stuck]
            state["last_rescue_reason"] = "auto-post-quarantine-restart"

    if allow_auto_restart and not api_error and oldest >= CRITICAL_AFTER_MINUTES:
        cooldown = AUTO_RESTART_COOLDOWN_MINUTES * 60
        if auto_rescue is None and now - last_hard_rescue_epoch >= cooldown:
            trigger_keys = [stuck_key(item) for item in stuck]
            auto_rescue = hard_restart_sab("auto-stuck-threshold")
            last_hard_rescue_epoch = now
            last_hard_rescue_at = iso()
            state["last_rescue_stuck_keys"] = trigger_keys
            state["last_rescue_reason"] = "auto-stuck-threshold"

    rescue_state, rescue_detail = rescue_status(state if isinstance(state, dict) else {}, stuck)

    write_json(
        STATE_PATH,
        {
            "updated_at": iso(),
            "seen": active,
            "last_hard_rescue_epoch": last_hard_rescue_epoch,
            "last_hard_rescue_at": last_hard_rescue_at,
            "last_rescue_stuck_keys": state.get("last_rescue_stuck_keys", []) if isinstance(state, dict) else [],
            "last_rescue_reason": state.get("last_rescue_reason", "") if isinstance(state, dict) else "",
            "last_quarantine_epoch": last_quarantine_epoch,
            "last_quarantine_at": last_quarantine_at,
            "last_quarantine_path": last_quarantine_path,
            "last_quarantine_job": state.get("last_quarantine_job", {}) if isinstance(state, dict) else {},
        },
    )

    data = {
        "generated_at": iso(),
        "status": status,
        "detail": detail,
        "stuck_count": len(stuck),
        "oldest_stuck_minutes": oldest,
        "watching_count": sum(counts.values()),
        "history_status_counts": counts,
        "stuck_items": stuck[:10],
        "cleanup_candidates": cleanup_candidates[:10],
        "cleanup_eligible_count": sum(1 for item in cleanup_candidates if item.get("cleanup_eligible")),
        "api_error": api_error,
        "last_hard_rescue_at": last_hard_rescue_at,
        "last_quarantine_at": last_quarantine_at,
        "last_quarantine_path": last_quarantine_path,
        "rescue_status": rescue_state,
        "rescue_detail": rescue_detail,
        "auto_rescue": auto_rescue,
        "auto_quarantine": auto_quarantine,
        "action": "Open button to hard-restart SAB or quarantine one confirmed stuck post-processing job",
    }
    write_json(OUT_PATH, data)
    return data


def log_event(event: dict[str, Any]) -> dict[str, Any]:
    log_path = OUT_PATH.with_name("sab-rescue-actions.jsonl")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def hard_restart_sab(reason: str) -> dict[str, Any]:
    cmd = [
        "ssh",
        "-i",
        str(FILESERV_SSH_KEY),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=8",
        FILESERV_SSH_TARGET,
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        FILESERV_RESCUE_SCRIPT,
    ]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=660)
    verify = {"reachable": False, "detail": "not checked"}
    time.sleep(POST_RESTART_VERIFY_SECONDS)
    try:
        queue = sab_api("queue").get("queue", {})
        verify = {
            "reachable": True,
            "detail": f"queue reachable, {queue.get('noofslots', 'unknown')} slots",
        }
    except Exception as exc:
        verify = {"reachable": False, "detail": f"{type(exc).__name__}: {exc}"}
    event = {
        "requested_at": iso(),
        "action": "hard_restart",
        "reason": reason,
        "duration_seconds": round(time.time() - started, 1),
        "returncode": proc.returncode,
        "verify": verify,
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-1000:],
    }
    return log_event(event)


def restart_sab() -> dict[str, Any]:
    try:
        before = build_status(allow_auto_restart=False)
        event = hard_restart_sab("manual-button")
        state = load_json(STATE_PATH, {})
        if not isinstance(state, dict):
            state = {}
        state["last_hard_rescue_epoch"] = time.time()
        state["last_hard_rescue_at"] = iso()
        state["last_rescue_stuck_keys"] = [stuck_key(item) for item in before.get("stuck_items") or []]
        state["last_rescue_reason"] = "manual-button"
        write_json(STATE_PATH, state)
        return event
    except Exception as exc:
        fallback = sab_api("restart")
        return log_event(
            {
                "requested_at": iso(),
                "action": "soft_restart_fallback",
                "hard_restart_error": f"{type(exc).__name__}: {exc}",
                "result": fallback,
            }
        )


def cleanup_stuck_job(*, dry_run: bool) -> dict[str, Any]:
    status = build_status(allow_auto_restart=False)
    candidate = select_manual_cleanup_candidate(status.get("cleanup_candidates") or [])
    if not candidate:
        return log_event(
            {
                "requested_at": iso(),
                "action": "quarantine_dry_run" if dry_run else "quarantine_job",
                "result": "no eligible stuck post-processing candidate",
            }
        )
    if dry_run:
        return quarantine_sab_job(candidate, dry_run=True, reason="manual-button")
    stop_event = stop_sab_for_quarantine("manual-button")
    event = quarantine_sab_job(candidate, dry_run=False, reason="manual-button")
    event["pre_quarantine_stop"] = stop_event
    rescue = hard_restart_sab("manual-post-quarantine-restart")
    event["post_quarantine_restart"] = rescue
    if event.get("returncode") == 0:
        state = load_json(STATE_PATH, {})
        if not isinstance(state, dict):
            state = {}
        state["last_quarantine_epoch"] = time.time()
        state["last_quarantine_at"] = iso()
        state["last_quarantine_path"] = event.get("quarantine_path", "")
        state["last_quarantine_job"] = {
            "name": candidate.get("name"),
            "nzo_id": candidate.get("nzo_id"),
            "status": candidate.get("status"),
            "path": candidate.get("path"),
        }
        state["last_hard_rescue_epoch"] = time.time()
        state["last_hard_rescue_at"] = iso()
        state["last_rescue_reason"] = "manual-post-quarantine-restart"
        state["last_rescue_stuck_keys"] = [stuck_key(item) for item in status.get("stuck_items") or []]
        write_json(STATE_PATH, state)
    return event


def page(status: dict[str, Any], message: str = "") -> str:
    rows = []
    for item in status.get("stuck_items") or []:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('age_minutes') or ''))}m</td>"
            f"<td>{html.escape(str(item.get('category') or ''))}</td>"
            f"<td>{html.escape(str(item.get('name') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="4">No stuck post-processing items detected.</td></tr>')
    cleanup_rows = []
    for item in status.get("cleanup_candidates") or []:
        cleanup_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('status') or ''))}</td>"
            f"<td>{html.escape(str(item.get('age_minutes') or ''))}m</td>"
            f"<td>{html.escape(str(item.get('category') or ''))}</td>"
            f"<td>{html.escape(str(item.get('cleanup_reason') or ''))}</td>"
            f"<td>{html.escape(str(item.get('name') or ''))}</td>"
            f"<td><code>{html.escape(str(item.get('path') or ''))}</code></td>"
            "</tr>"
        )
    if not cleanup_rows:
        cleanup_rows.append('<tr><td colspan="6">No cleanup-eligible post-processing folders detected.</td></tr>')
    msg = f"<p class='message'>{html.escape(message)}</p>" if message else ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SAB Rescue</title>
  <style>
    body {{ margin:0; font-family: system-ui, Segoe UI, sans-serif; color:#e8edf5; background:#071018; }}
    main {{ max-width:920px; margin:0 auto; padding:32px 20px; }}
    h1 {{ margin:0 0 8px; font-size:28px; }}
    .status {{ color:#9db4c8; margin-bottom:22px; }}
    .panel {{ border:1px solid #253547; background:#0d1824; border-radius:8px; padding:18px; margin:18px 0; }}
    .message {{ color:#8ecae6; }}
    button {{ background:#d95454; color:white; border:0; border-radius:6px; padding:11px 16px; font-weight:700; cursor:pointer; margin-right:8px; }}
    .secondary {{ background:#28648a; }}
    button:hover {{ background:#ee6666; }}
    table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    th, td {{ text-align:left; padding:9px; border-bottom:1px solid #1d2b3a; vertical-align:top; }}
    th {{ color:#8ecae6; font-size:12px; text-transform:uppercase; }}
    code {{ color:#f4d35e; }}
  </style>
</head>
<body>
<main>
  <h1>SAB Rescue</h1>
  <div class="status"><strong>{html.escape(str(status.get('status')))}</strong> · {html.escape(str(status.get('detail')))} · updated {html.escape(str(status.get('generated_at')))}</div>
  <div class="status"><strong>Rescue:</strong> {html.escape(str(status.get('rescue_status') or 'OK'))} · {html.escape(str(status.get('rescue_detail') or 'No hard rescue recorded'))}</div>
  {msg}
  <section class="panel">
    <form method="post" action="/restart" onsubmit="return confirm('Hard-restart SABnzbd now? Active downloads may reconnect afterward.');">
      <button type="submit">Hard Restart SABnzbd</button>
    </form>
    <p>This kills SAB/unpack helper processes and relaunches SAB through the Administrator interactive task. It does not delete queue, history, downloads, or media.</p>
  </section>
  <section class="panel">
    <h2>Bad Post-Processing Cleanup</h2>
    <form method="post" action="/cleanup-dry-run">
      <button class="secondary" type="submit">Dry Run Quarantine</button>
    </form>
    <form method="post" action="/cleanup" onsubmit="return confirm('Move one verified stuck SAB temp folder to quarantine and restart SAB? This will not touch media libraries.');">
      <button type="submit">Quarantine Bad Job + Restart</button>
    </form>
    <p>Only SAB temp/download paths under <code>H:\\Temp\\Incomplete</code> or <code>H:\\Temp\\Downloads</code> are eligible. Queued orphan-style jobs are reported but not auto-moved.</p>
    <table>
      <thead><tr><th>Status</th><th>Age</th><th>Cat</th><th>Reason</th><th>Name</th><th>Path</th></tr></thead>
      <tbody>{''.join(cleanup_rows)}</tbody>
    </table>
  </section>
  <section class="panel">
    <h2>Watched Post-Processing</h2>
    <table>
      <thead><tr><th>Status</th><th>Age</th><th>Cat</th><th>Name</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </section>
  <p><a href="/status.json" style="color:#8ecae6">status.json</a></p>
</main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def send_text(self, code: int, body: str, content_type: str = "text/html") -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.startswith("/status.json"):
            self.send_text(200, json.dumps(build_status(), indent=2, sort_keys=True), "application/json")
            return
        self.send_text(200, page(build_status()))

    def do_POST(self) -> None:
        if self.path not in {"/restart", "/cleanup-dry-run", "/cleanup"}:
            self.send_text(404, "not found", "text/plain")
            return
        try:
            if self.path == "/restart":
                event = restart_sab()
                message = f"Restart requested at {event['requested_at']}"
            elif self.path == "/cleanup-dry-run":
                event = cleanup_stuck_job(dry_run=True)
                message = f"Dry run: {event.get('result') or event.get('quarantine_path') or 'checked'}"
            else:
                event = cleanup_stuck_job(dry_run=False)
                if event.get("returncode") == 0:
                    message = f"Quarantined one stuck job and restarted SAB at {event['requested_at']}"
                else:
                    message = f"Cleanup did not complete: {event.get('result') or event.get('stderr') or 'see action log'}"
        except Exception as exc:
            message = f"Action failed: {type(exc).__name__}"
        try:
            status = build_status()
        except Exception:
            status = load_json(OUT_PATH, {"status": "WARN", "detail": "SAB status temporarily unavailable"})
        self.send_text(200, page(status, message))

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="SAB rescue status/button helper")
    parser.add_argument("--once", action="store_true", help="write status JSON and exit")
    args = parser.parse_args()
    if args.once:
        print(json.dumps(build_status(), indent=2, sort_keys=True))
        return 0
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
