#!/usr/bin/env python3
"""Container startup shim for the Docker-first InkDrop install path."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import json
import os
import signal
import subprocess
import sys
import time

from core import inkdrop_preflight
from core import inkdrop_public_contracts
from core import inkdrop_runtime_config


def embedded_scheduler_enabled(environ=None) -> bool:
    """Whether this container should run the scheduler beside the web server.

    The documented install is one container, and a container that serves the
    UI but never searches, downloads, or maintains anything is not InkDrop --
    so the default is on. Deployments that run a dedicated scheduler
    container set INKDROP_CONTAINER_SCHEDULER_ENABLED=0 on the web service;
    the same switch the scheduler itself honors, so there is exactly one
    knob for where scheduling lives.
    """
    env = os.environ if environ is None else environ
    value = str(env.get("INKDROP_CONTAINER_SCHEDULER_ENABLED") or "").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def run_supervised() -> int:
    """Run web and scheduler together; if either stops, stop the container.

    Die-together is deliberate: the restart policy revives the pair, and a
    container that is half-alive (UI up, no background work -- or the
    reverse) is worse than one that visibly restarts.
    """
    children = []

    def forward(signum, _frame):
        for child in children:
            try:
                child.send_signal(signum)
            except OSError:
                pass

    exit_code = 0
    try:
        web = subprocess.Popen([sys.executable, "-B", "core/inkdrop_web.py"])
        children.append(web)
        scheduler = subprocess.Popen([sys.executable, "-B", "core/inkdrop_container_scheduler.py"])
        children.append(scheduler)

        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)

        while True:
            finished = next((child for child in children if child.poll() is not None), None)
            if finished is not None:
                exit_code = int(finished.returncode or 0)
                break
            time.sleep(1)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        deadline = time.time() + 15
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                child.kill()
    return exit_code


def main() -> int:
    inkdrop_runtime_config.normalize_runtime_environment()
    try:
        payload = inkdrop_preflight.run_preflight(create=True, strict_dependencies=True, strict_runtime_tools=True)
    except Exception as exc:
        payload = {
            "preflight_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
            "ok": False,
            "startup_phase": "preflight",
            "errors": [f"preflight crashed before web startup: {exc}"],
            "warnings": [],
        }
    if not payload.get("ok"):
        payload.setdefault("startup_phase", "preflight")
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr, flush=True)
        return 1

    # Best-effort only: never let maintenance delay or prevent web startup.
    try:
        subprocess.run(
            [sys.executable, "-B", "core/inkdrop_auth_cli.py", "cleanup", "--batch-limit", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass

    if embedded_scheduler_enabled():
        try:
            return run_supervised()
        except OSError as exc:
            payload = {
                "preflight_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
                "ok": False,
                "startup_phase": "supervised_start",
                "errors": [f"failed to start web and scheduler: {exc}"],
                "warnings": [],
            }
            print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr, flush=True)
            return 127

    try:
        os.execvp(sys.executable, [sys.executable, "-B", "core/inkdrop_web.py"])
    except OSError as exc:
        payload = {
            "preflight_schema_version": inkdrop_public_contracts.PREFLIGHT_SCHEMA_VERSION,
            "ok": False,
            "startup_phase": "web_exec",
            "errors": [f"failed to exec web entrypoint: {exc}"],
            "warnings": [],
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr, flush=True)
        return 127
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
