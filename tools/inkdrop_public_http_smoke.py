#!/usr/bin/env python3
"""Live HTTP smoke for the public InkDrop web/state surface.

This uses a temporary runtime profile and an ephemeral localhost port, so it can
prove the standalone web entrypoint without touching live InkDrop state, reader
databases, downloads, or media roots.
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import inkdrop_preflight
import inkdrop_runtime_config
import inkdrop_state


REQUIRED_STATUS_FIELDS = {
    "status",
    "detail",
    "source_health",
    "system_health",
    "inkdrop_state",
    "status_cache",
    "status_partial",
}

REQUIRED_STATE_FIELDS = {
    "ok",
    "dashboard_shell",
    "preview_mode",
    "sections",
    "section_links",
    "summary",
}

REQUIRED_UI_ASSETS = (
    "inkdrop-operational-preferences.js",
    "inkdrop-operational-table-controls.js",
    "inkdrop-operational-query-controls.js",
    "inkdrop-operational-row-controls.js",
    "inkdrop-transfer-telemetry.js",
    "inkdrop-version-about.js",
    "inkdrop-operational-bootstrap.js",
)


def missing_dependencies():
    missing = []
    for module_name, package_name in sorted(inkdrop_preflight.REQUIRED_PYTHON_MODULES.items()):
        if importlib.util.find_spec(module_name) is None:
            missing.append({"module": module_name, "package": package_name})
    return missing


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def runtime_env(runtime_root, port):
    env = dict(os.environ)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "INKDROP_HOST": "127.0.0.1",
            "INKDROP_PORT": str(port),
            "INKDROP_CONFIG_DIR": str(runtime_root / "config"),
            "INKDROP_STATE_DIR": str(runtime_root / "state"),
            "INKDROP_LOG_DIR": str(runtime_root / "logs"),
            "INKDROP_CACHE_DIR": str(runtime_root / "cache"),
            "INKDROP_BACKUP_DIR": str(runtime_root / "backups"),
            "INKDROP_STAGING_DIR": str(runtime_root / "staging"),
            "INKDROP_MANUAL_INBOX_DIR": str(runtime_root / "manual-inbox"),
            "INKDROP_QUARANTINE_DIR": str(runtime_root / "quarantine"),
        }
    )
    return env


def fetch_json(port, path, *, timeout=3.0, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        request_headers = {"Accept": "application/json"}
        request_headers.update(headers or {})
        conn.request("GET", path, headers=request_headers)
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
    finally:
        conn.close()
    if response.status < 200 or response.status >= 300:
        raise RuntimeError(f"{path} returned HTTP {response.status}: {body[:500]}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} returned non-JSON body: {body[:500]}") from exc


def post_json(port, path, payload, *, timeout=3.0, headers=None):
    body = json.dumps(payload).encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    request_headers.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", path, body=body, headers=request_headers)
        response = conn.getresponse()
        response_body = response.read().decode("utf-8", errors="replace")
        response_headers = response.getheaders()
    finally:
        conn.close()
    if response.status < 200 or response.status >= 300:
        raise RuntimeError(f"{path} returned HTTP {response.status}: {response_body[:500]}")
    try:
        return json.loads(response_body), response_headers
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} returned non-JSON body: {response_body[:500]}") from exc


def read_setup_code(config_dir, *, timeout=10.0):
    """Read the first-run setup code the way an operator does: from the file.

    The server writes it just after it starts listening, so this can win the
    race against startup on a slow runner.
    """
    path = Path(config_dir) / "bootstrap-token.txt"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            first_line = ""
        if first_line:
            return first_line
        time.sleep(0.1)
    raise RuntimeError(f"the server never wrote a setup code to {path}")


def login_cookie(port, config_dir):
    password = f"Smoke-{secrets.token_urlsafe(18)}"
    post_json(
        port,
        "/api/auth/bootstrap",
        {
            "username": "smoke-admin",
            "password": password,
            "credential": read_setup_code(config_dir),
        },
        timeout=5.0,
    )
    _payload, response_headers = post_json(
        port,
        "/api/auth/login",
        {"username": "smoke-admin", "password": password},
        timeout=5.0,
    )
    cookies = []
    for name, value in response_headers:
        if name.lower() == "set-cookie":
            cookies.append(value.split(";", 1)[0])
    if not any(value.startswith("inkdrop_session=") for value in cookies):
        raise RuntimeError("login response did not set the InkDrop session cookie")
    return "; ".join(cookies)


def fetch_response(port, path, *, timeout=3.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        conn.close()


def wait_for_status(port, *, deadline):
    last_error = ""
    while time.time() < deadline:
        try:
            return fetch_json(port, "/status.json", timeout=2.0)
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.35)
    raise RuntimeError(f"server did not answer /status.json before timeout: {last_error}")


def terminate(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=8)


def limited_text(value, limit=4000):
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[-limit:]


def compact_state_payload(payload):
    state = payload.get("state") if isinstance(payload.get("state"), dict) else payload
    return state if isinstance(state, dict) else {}


def run_smoke(*, startup_timeout=45, skip_if_missing_dependencies=False):
    missing = missing_dependencies()
    if missing:
        return {
            "ok": bool(skip_if_missing_dependencies),
            "skipped": bool(skip_if_missing_dependencies),
            "reason": "missing_python_dependencies",
            "missing_python_dependencies": missing,
        }

    with tempfile.TemporaryDirectory(prefix="inkdrop-public-http-smoke-") as tmp:
        runtime_root = Path(tmp)
        port = free_port()
        env = runtime_env(runtime_root, port)
        inkdrop_runtime_config.ensure_runtime_roots(env)
        inkdrop_state.ensure_schema(inkdrop_runtime_config.state_db_path(env))

        proc = subprocess.Popen(
            [sys.executable, "-B", "inkdrop_web.py"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        findings = []
        status_payload = {}
        state_payload = {}
        try:
            status_payload = wait_for_status(port, deadline=time.time() + startup_timeout)
            cookie = login_cookie(port, env["INKDROP_CONFIG_DIR"])
            state_payload = fetch_json(
                port,
                "/api/inkdrop-state/sections?summary=compact",
                timeout=5.0,
                headers={"Cookie": cookie},
            )
            state = compact_state_payload(state_payload)

            root_status, root_headers, root_body = fetch_response(port, "/", timeout=5.0)
            root_text = root_body.decode("utf-8", errors="replace")
            if root_status != 200 or "text/html" not in str(root_headers.get("Content-Type") or ""):
                findings.append({"kind": "invalid_root_response", "status": root_status})
            bootstrap_src = "/static/js/inkdrop-operational-bootstrap.js"
            if root_text.count(bootstrap_src) != 1:
                findings.append({"kind": "operational_bootstrap_count", "count": root_text.count(bootstrap_src)})
            for asset in REQUIRED_UI_ASSETS:
                asset_status, asset_headers, asset_body = fetch_response(port, f"/static/js/{asset}", timeout=5.0)
                content_type = str(asset_headers.get("Content-Type") or "")
                if asset_status != 200 or "application/javascript" not in content_type or not asset_body:
                    findings.append(
                        {
                            "kind": "invalid_ui_asset",
                            "asset": asset,
                            "status": asset_status,
                            "content_type": content_type,
                            "bytes": len(asset_body),
                        }
                    )

            for field in sorted(REQUIRED_STATUS_FIELDS):
                if field not in status_payload:
                    findings.append({"kind": "missing_status_field", "field": field})
            if not isinstance(status_payload.get("inkdrop_state"), dict):
                findings.append({"kind": "invalid_status_field", "field": "inkdrop_state"})

            if not state:
                findings.append({"kind": "invalid_state_payload", "field": "state"})
            for field in sorted(REQUIRED_STATE_FIELDS):
                if field not in state:
                    findings.append({"kind": "missing_state_field", "field": field})
            if state.get("ok") is not True:
                findings.append({"kind": "state_not_ok", "value": state.get("ok")})
        except Exception as exc:
            findings.append({"kind": "http_smoke_error", "error": str(exc)})
        finally:
            terminate(proc)
            stdout, stderr = proc.communicate(timeout=8)

        state = compact_state_payload(state_payload)
        return {
            "ok": not findings,
            "skipped": False,
            "port": port,
            "findings": findings,
            "status_summary": {
                "status": status_payload.get("status"),
                "status_cache": status_payload.get("status_cache"),
                "status_partial": status_payload.get("status_partial"),
            },
            "state_summary": {
                "ok": state.get("ok"),
                "section_count": state.get("section_count"),
            },
            "process_returncode": proc.returncode,
            "stdout_tail": limited_text(stdout),
            "stderr_tail": limited_text(stderr),
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a temp-root live HTTP smoke against InkDrop public endpoints.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--startup-timeout", type=int, default=45, help="Seconds to wait for /status.json.")
    parser.add_argument(
        "--skip-if-missing-dependencies",
        action="store_true",
        help="Return success with a skipped payload if Python requirements are not installed.",
    )
    args = parser.parse_args(argv)

    payload = run_smoke(
        startup_timeout=max(1, int(args.startup_timeout or 45)),
        skip_if_missing_dependencies=bool(args.skip_if_missing_dependencies),
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        status = "ok" if payload.get("ok") else "failed"
        if payload.get("skipped"):
            status = "skipped"
        print(f"{status}: inkdrop_public_http_smoke")
        if payload.get("reason"):
            print(payload["reason"])
        for finding in payload.get("findings") or []:
            print(f"- {finding}")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
