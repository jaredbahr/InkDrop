#!/usr/bin/env python3
"""Docker healthcheck for the InkDrop container.

The startup shim validates configuration before launching the web process. This
healthcheck repeats the strict preflight and also proves the in-container web
server is answering its public status endpoint.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import http.client
import json
import os
import sys
import time
from pathlib import Path

from core import inkdrop_preflight
from core import inkdrop_public_contracts
from core import inkdrop_runtime_config
from core import inkdrop_version


def _emit(payload):
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)


def _status_url(port):
    return f"http://127.0.0.1:{port}/status.json"


def _probe_status_once(port, *, timeout):
    started = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=float(timeout))
    try:
        conn.request("GET", "/status.json", headers={"Accept": "application/json"})
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "host": "127.0.0.1",
            "port": int(port),
            "timeout_seconds": float(timeout),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        conn.close()
    elapsed_seconds = round(time.monotonic() - started, 3)
    if response.status < 200 or response.status >= 300:
        return {
            "ok": False,
            "error": f"/status.json returned HTTP {response.status}",
            "http_status": response.status,
            "host": "127.0.0.1",
            "port": int(port),
            "timeout_seconds": float(timeout),
            "elapsed_seconds": elapsed_seconds,
            "body": body[:500],
        }
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"/status.json returned non-JSON body: {exc}",
            "error_type": type(exc).__name__,
            "host": "127.0.0.1",
            "port": int(port),
            "timeout_seconds": float(timeout),
            "elapsed_seconds": elapsed_seconds,
            "body": body[:500],
        }
    return {
        "ok": True,
        "status": payload.get("status"),
        "product": payload.get("product") or inkdrop_version.PRODUCT_NAME,
        "version": payload.get("version") or inkdrop_version.build_metadata(),
        "status_cache": payload.get("status_cache"),
        "status_partial": payload.get("status_partial"),
        "host": "127.0.0.1",
        "port": int(port),
        "timeout_seconds": float(timeout),
        "elapsed_seconds": elapsed_seconds,
    }


def _probe_status(port, *, timeout, wait_seconds=0.0):
    deadline = time.monotonic() + max(0.0, float(wait_seconds or 0.0))
    attempts = 0
    started = time.monotonic()
    last = None
    while True:
        attempts += 1
        last = _probe_status_once(port, timeout=timeout)
        if last.get("ok"):
            last["attempts"] = attempts
            last["wait_seconds"] = max(0.0, float(wait_seconds or 0.0))
            last["total_elapsed_seconds"] = round(time.monotonic() - started, 3)
            return last
        if time.monotonic() >= deadline:
            last["attempts"] = attempts
            last["wait_seconds"] = max(0.0, float(wait_seconds or 0.0))
            last["total_elapsed_seconds"] = round(time.monotonic() - started, 3)
            return last
        time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))


def run_healthcheck(*, preflight_only=False, timeout=5.0, wait_seconds=0.0):
    inkdrop_runtime_config.normalize_runtime_environment()
    try:
        preflight = inkdrop_preflight.run_preflight(create=True, strict_dependencies=True, strict_runtime_tools=True)
    except Exception as exc:
        return {
            "healthcheck_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
            "ok": False,
            "phase": "preflight",
            "errors": [f"preflight crashed during healthcheck: {exc}"],
        }
    if not preflight.get("ok"):
        return {
            "healthcheck_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
            "ok": False,
            "phase": "preflight",
            "preflight": preflight,
        }
    if preflight_only:
        return {
            "healthcheck_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
            "ok": True,
            "phase": "preflight",
            "preflight_ok": True,
        }

    try:
        port = inkdrop_runtime_config.web_port(strict=True)
        status = _probe_status(port, timeout=timeout, wait_seconds=wait_seconds)
    except Exception as exc:
        status = {
            "ok": False,
            "error": str(exc),
        }
        port = inkdrop_runtime_config.web_port(strict=False)
    if not status.get("ok"):
        return {
            "healthcheck_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
            "ok": False,
            "phase": "http",
            "preflight_ok": True,
            "status_url": _status_url(port),
            "status_probe": status,
        }
    return {
        "healthcheck_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
        "ok": True,
        "phase": "http",
        "preflight_ok": True,
        "status_url": _status_url(port),
        "status_probe": status,
    }


def _positive_int_env(name, default, minimum=1, maximum=86400):
    try:
        value = int(str(os.environ.get(name) or default).strip())
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def _safe_number(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return float(default)


def _worker_status_path():
    state_dir = inkdrop_runtime_config.state_dir()
    return Path(os.environ.get("INKDROP_WORKER_STATUS_FILE") or state_dir / "worker-scheduler-status.json")


def run_worker_healthcheck(*, status_path=None, now=None):
    inkdrop_runtime_config.normalize_runtime_environment()
    preflight = run_healthcheck(preflight_only=True)
    if not preflight.get("ok"):
        return dict(preflight, phase="worker_preflight")
    path = Path(status_path) if status_path else _worker_status_path()
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "healthcheck_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
            "ok": False,
            "phase": "worker",
            "worker_state": "unavailable",
            "error": "worker scheduler status file is missing",
            "status_path": str(path),
        }
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return {
            "healthcheck_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
            "ok": False,
            "phase": "worker",
            "worker_state": "unavailable",
            "error": f"worker scheduler status is unreadable: {exc}",
            "status_path": str(path),
        }
    now = time.time() if now is None else float(now)
    try:
        heartbeat_age = max(0.0, now - float(status.get("heartbeat_at") or 0))
    except (TypeError, ValueError):
        heartbeat_age = float("inf")
    max_age = _positive_int_env("INKDROP_WORKER_HEALTH_MAX_AGE_SECONDS", 90, 10, 3600)
    failure_threshold = _positive_int_env("INKDROP_WORKER_CRITICAL_FAILURE_THRESHOLD", 3, 1, 100)
    max_lateness = _positive_int_env("INKDROP_WORKER_HEALTH_MAX_LATENESS_SECONDS", 600, 30, 86400)
    stale = heartbeat_age > max_age
    jobs = status.get("jobs") if isinstance(status.get("jobs"), list) else []
    active_jobs = status.get("active_jobs") if isinstance(status.get("active_jobs"), list) else []
    failed_jobs = [row for row in jobs if isinstance(row, dict) and _safe_number(row.get("consecutive_failures")) > 0]
    critical_failures = [
        row
        for row in failed_jobs
        if row.get("critical") and _safe_number(row.get("consecutive_failures")) >= failure_threshold
    ]
    critically_late = [
        row
        for row in jobs
        if isinstance(row, dict)
        and row.get("critical")
        and _safe_number(row.get("late_by_seconds")) > max_lateness
    ]
    hung_jobs = []
    for row in active_jobs:
        if not isinstance(row, dict):
            continue
        try:
            age = max(0.0, now - float(row.get("started_at") or now))
            timeout_seconds = max(1.0, float(row.get("timeout_seconds") or 1))
        except (TypeError, ValueError):
            continue
        if row.get("critical") and age > timeout_seconds + max_age:
            hung_jobs.append(dict(row, active_seconds=round(age, 3)))
    stopping = str(status.get("state") or "").strip().lower() == "stopping"
    ok = not stale and not stopping and not critical_failures and not critically_late and not hung_jobs
    degraded = bool(failed_jobs or critically_late or hung_jobs)
    return {
        "healthcheck_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
        "ok": ok,
        "phase": "worker",
        "worker_state": "unavailable" if stale or stopping else "degraded" if degraded else "healthy",
        "degraded": degraded,
        "preflight_ok": True,
        "status_path": str(path),
        "heartbeat_age_seconds": round(heartbeat_age, 3),
        "heartbeat_max_age_seconds": max_age,
        "critical_failure_threshold": failure_threshold,
        "critical_failures": critical_failures,
        "critically_late_jobs": critically_late,
        "hung_jobs": hung_jobs,
        "failed_jobs": failed_jobs,
        "active_job_count": len(active_jobs),
        "scheduler": status,
    }


def main(argv=None):
    inkdrop_runtime_config.normalize_runtime_environment()
    parser = argparse.ArgumentParser(description="Run InkDrop's container healthcheck.")
    parser.add_argument("--preflight-only", action="store_true", help="Only run strict preflight, without probing /status.json.")
    parser.add_argument("--worker", action="store_true", help="Validate the container scheduler heartbeat and critical job state.")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds for /status.json.")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=0.0,
        help="Wait up to this many seconds for /status.json before failing the HTTP phase. Useful for manual startup troubleshooting.",
    )
    parser.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")
    args = parser.parse_args(argv)

    if args.worker:
        payload = run_worker_healthcheck()
    else:
        payload = run_healthcheck(
            preflight_only=bool(args.preflight_only),
            timeout=max(0.5, float(args.timeout or 5.0)),
            wait_seconds=max(0.0, float(args.wait_seconds or 0.0)),
        )
    payload["product"] = inkdrop_version.PRODUCT_NAME
    payload["version"] = inkdrop_version.build_metadata()
    _emit(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
