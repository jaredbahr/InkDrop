#!/usr/bin/env python3
"""Table-driven Automatic Search configuration/runtime truth smoke."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from core import inkdrop_web as web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def setup_payload(ready):
    return {
        "readiness": {
            "automatic_search": {
                "ready": bool(ready),
                "next_action": "Configure a source and transfer handoff.",
            }
        }
    }


def write_scheduler(root, *, state="healthy", ok=True, now=1000.0, failure_count=0, jobs=None):
    (root / "worker-scheduler-status.json").write_text(
        json.dumps({
            "ok": ok,
            "state": state,
            "heartbeat_at": now,
            "failure_count": failure_count,
            "jobs": jobs or [],
        }),
        encoding="utf-8",
    )


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-automatic-search-state-") as tmp:
        root = Path(tmp)
        write_scheduler(root)
        common = {
            "monitored_series": 3,
            "in_progress": False,
            "active_work": 0,
            "now": 1000.0,
        }
        with mock.patch.object(web, "STATE_DIR", root):
            with mock.patch.object(web, "SERIES_QUEUE_RUNNER_AUTOPILOT_ENABLED", False):
                disabled = web.automatic_search_runtime_state(setup=setup_payload(True), **common)
            require(disabled["state"] == "disabled" and disabled["disabled"], disabled)
            require(not disabled["currently_idle"], "disabled must never be presented as idle")
            require(disabled["scheduler_active"] and disabled["worker_healthy"], "scheduler/worker facts must stay independent")
            require("INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED=1" in disabled["next_action"], disabled)
            require("in .env" in disabled["next_action"], disabled)
            require("docker compose up -d --force-recreate inkdrop inkdrop-worker" in disabled["next_action"], disabled)

            with mock.patch.object(web, "SERIES_QUEUE_RUNNER_AUTOPILOT_ENABLED", True):
                waiting = web.automatic_search_runtime_state(setup=setup_payload(False), **common)
                require(waiting["state"] == "waiting_for_configuration", waiting)
                require(waiting["enabled"] and waiting["waiting_for_configuration"], waiting)
                require(not waiting["currently_idle"], "waiting configuration must not be idle")

                idle = web.automatic_search_runtime_state(setup=setup_payload(True), **common)
                require(idle["state"] == "idle" and idle["currently_idle"], idle)
                require(idle["series_monitored"] == 3, idle)

                running = web.automatic_search_runtime_state(
                    setup=setup_payload(True),
                    **dict(common, in_progress=True, active_work=1),
                )
                require(running["state"] == "running" and not running["currently_idle"], running)

                write_scheduler(root, state="degraded", ok=True, failure_count=1)
                unhealthy = web.automatic_search_runtime_state(setup=setup_payload(True), **common)
                require(unhealthy["state"] == "maintenance_degraded", unhealthy)
                require(unhealthy["scheduler_active"] and not unhealthy["worker_healthy"], unhealthy)
                require(unhealthy["acquisition_worker_healthy"] and unhealthy["maintenance_degraded"], unhealthy)
                require(unhealthy["failure_code"] == "worker_degraded", unhealthy)
                require(unhealthy["core_library_usable"], unhealthy)

                write_scheduler(root, state="degraded", ok=True, failure_count=1, jobs=[{
                    "name": "full_state_reconciliation",
                    "critical": False,
                    "last_completed_at": 950.0,
                    "last_rc": 124,
                    "last_outcome": "timeout",
                    "consecutive_failures": 2,
                }])
                timed_out = web.automatic_search_runtime_state(setup=setup_payload(True), **common)
                require(timed_out["failure_code"] == "maintenance_timed_out", timed_out)
                require("ran out of time" in timed_out["failure_reason"].lower(), timed_out)
                require("try again" in timed_out["failure_reason"].lower(), timed_out)
                require(timed_out["last_worked_at"] is None, timed_out)
                require(timed_out["acquisition_worker_healthy"] and timed_out["maintenance_degraded"], timed_out)

                write_scheduler(root, state="paused", ok=True, failure_count=0)
                paused = web.automatic_search_runtime_state(setup=setup_payload(True), **common)
                require(paused["state"] == "operator_paused" and paused["operator_paused"], paused)
                require(not paused["scheduler_active"] and not paused["acquisition_worker_healthy"], paused)
                require(not paused["worker_healthy"] and not paused["maintenance_degraded"], paused)
                require(not paused["currently_idle"], paused)
                require(paused["failure_code"] == "operator_pause", paused)
                require("Restart the inkdrop-worker container" in paused["next_action"], paused)

                write_scheduler(root, state="degraded", ok=True, failure_count=1, jobs=[{
                    "name": "provider_search",
                    "critical": True,
                    "last_completed_at": 975.0,
                    "last_rc": 0,
                    "last_outcome": "success",
                    "consecutive_failures": 0,
                }, {
                    "name": "critical_import",
                    "critical": True,
                    "last_completed_at": 980.0,
                    "last_rc": 1,
                    "last_outcome": "failed",
                    "consecutive_failures": 1,
                }])
                critical = web.automatic_search_runtime_state(setup=setup_payload(True), **common)
                require(critical["failure_code"] == "critical_job_failed", critical)
                require(critical["last_worked_at"] == 975.0 and critical["last_worked_at_iso"], critical)

                write_scheduler(root, state="healthy", ok=True, now=800.0)
                inactive = web.automatic_search_runtime_state(setup=setup_payload(True), **common)
                require(inactive["state"] == "worker_unhealthy", inactive)
                require(not inactive["scheduler_active"] and not inactive["worker_healthy"], inactive)
                require(inactive["failure_code"] == "stale_heartbeat", inactive)

    print("INKDROP_AUTOMATIC_SEARCH_STATE_OK")


if __name__ == "__main__":
    main()
