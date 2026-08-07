#!/usr/bin/env python3
"""Run container-local maintenance jobs without crossing the HTTP auth boundary."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import json


JOB_NAMES = ("comicvine-scan", "manual-review-noop-resolve", "notification-dispatch")


def run_notification_dispatch() -> dict:
    """Process due quiet-hours/rate-limit/retry queued deliveries, then run
    the read-only diff scanners that turn download-task/health/update state
    into grabbed/download_failed/health_issue/health_restored/
    application_update events."""

    from core import inkdrop_notification_events
    from core import inkdrop_notification_store
    from core import inkdrop_notifications
    from core import inkdrop_runtime_config

    db_path = inkdrop_runtime_config.state_db_path()
    retries = inkdrop_notifications.process_due_retries(db_path)
    scan = inkdrop_notification_events.run_scan(db_path)
    pruned = inkdrop_notification_store.prune_history(db_path)
    return {"retries": retries, "scan": scan, "pruned": pruned}


def run_manual_source_import(payload: dict) -> dict:
    """Run the trusted container-local import entrypoint without HTTP auth."""

    from core import inkdrop_web

    return {"ok": True, "result": inkdrop_web.import_detected_manual_source(dict(payload or {}))}


def run_manual_source_mark_waiting(payload: dict) -> dict:
    """Persist trusted worker waiting state without crossing HTTP auth."""

    from core import inkdrop_web

    return {"ok": True, "result": inkdrop_web.mark_manual_source_waiting(dict(payload or {}))}


def run_autopilot_web_job(path: str, payload: dict | None = None) -> dict:
    """Dispatch the two bounded autopilot callbacks inside the worker."""

    from core import inkdrop_web

    data = dict(payload or {})
    if path == "/api/inkdrop-state/sync":
        state = inkdrop_web.inkdrop_state_sections_public()
        sync_job = inkdrop_web.run_inkdrop_state_sync_background(
            "worker_internal_sync",
            mode=data.get("mode") or data.get("sync_mode") or "full",
        )
        if isinstance(state, dict):
            state["sync_scheduled"] = bool(sync_job.get("started"))
            state["sync_job"] = sync_job
        return {"ok": bool(sync_job.get("ok", True)), "state": state, "sync_job": sync_job}
    raise ValueError(f"unsupported internal autopilot route: {path}")


def run_pack_review_state() -> dict:
    """Read pack-review state through the same sanitizing web contract."""

    from core import inkdrop_web

    return {"ok": True, "pack_state": inkdrop_web.pack_review_public_state()}


def run_job(name: str) -> tuple[int, dict]:
    if name == "notification-dispatch":
        result = run_notification_dispatch()
        return 0, result

    from core import inkdrop_web

    if name == "comicvine-scan":
        result = inkdrop_web.scan_comic_series({})
    elif name == "manual-review-noop-resolve":
        result = inkdrop_web.resolve_noop_manual_reviews()
    else:
        raise ValueError(f"unsupported internal job: {name}")

    payload = result if isinstance(result, dict) else {"result": result}
    provider_status = str(payload.get("status") or "").strip().lower()
    return (78 if provider_status in {"configuration_needed", "disabled"} else 0), payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=JOB_NAMES)
    args = parser.parse_args()
    returncode, result = run_job(args.job)
    print(json.dumps({"ok": returncode == 0, "job": args.job, "result": result}, indent=2, default=str))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
