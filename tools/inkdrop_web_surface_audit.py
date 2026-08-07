#!/usr/bin/env python3
"""Static audit for InkDrop's legacy web surface before route/module splits."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_FILE = ROOT / "core" / "inkdrop_web.py"
WEB_CONFIG_FILE = ROOT / "core" / "inkdrop_web_config.py"

REQUIRED_GET_PATHS = {
    "/",
    "/status.json",
    "/api/inkdrop-state",
    "/api/inkdrop-settings",
    "/api/manual-review",
    "/api/pack-review/state",
    "/api/import-reconcile",
    "/api/unmatched-downloads",
}

REQUIRED_POST_PATHS = {
    "/api/comicvine/add",
    "/api/mangadex/add",
    "/api/inkdrop-state/series/run",
    "/api/inkdrop-state/wanted/run",
    "/api/inkdrop-state/queue/run",
    "/api/inkdrop-settings/provider/add",
    "/api/inkdrop-settings/sync",
    "/api/inkdrop-settings/provider/claim",
    "/api/inkdrop-settings/provider/update",
    "/api/inkdrop-settings/provider/recommendation/apply",
    "/api/inkdrop-settings/provider/test",
    "/api/inkdrop-settings/app/update",
    "/api/missing/process",
    "/api/slskd-source-probe/run",
    "/api/manual-source/import-detected",
    "/api/manual-review/approve",
    "/api/pack-review/import",
    "/api/unmatched-downloads/import",
}

REQUIRED_STARTSWITH_PREFIXES = {
    "/api/inkdrop-state/",
}

REQUIRED_WORKER_REFERENCES = {
    "ACQUIRE_SCRIPT": "inkdrop_missing_acquire.py",
    "IMPORT_SCRIPT": "inkdrop_completed_import.py",
    "RECONCILE_SCRIPT": "inkdrop_reconcile_imports.py",
    "PACK_IMPORT_SCRIPT": "inkdrop_pack_import.py",
    "MISSING_ACQUIRE_MODULE_SCRIPT": "inkdrop_missing_acquire.py",
    "RSS_DISCOVERY_SCRIPT": "inkdrop_rss_discovery.py",
    "COMICSCODES_DISCOVERY_SCRIPT": "inkdrop_comicscodes_discovery.py",
    "SLSKD_SOURCE_PROBE_SCRIPT": "inkdrop_slskd_source_probe.py",
    "SERIES_AUTOPILOT_SCRIPT": "inkdrop_series_autopilot.py",
    "MANUAL_SOURCE_AUTORESOLVE_SCRIPT": "inkdrop_manual_source_autoresolve.py",
}

REQUIRED_STATUS_FUNCTIONS = {
    "quick_script_status",
    "warming_script_status",
    "refresh_status_cache_background",
    "cached_script_status",
}

REQUIRED_STATUS_FIELDS = {
    "status",
    "detail",
    "diagnostic_detail",
    "server_activities",
    "source_health",
    "system_health",
    "pack_review_state",
    "inkdrop_state",
    "automation",
    "series_autopilot_summary",
    "series_autopilot_queued_count",
    "series_autopilot_searching_count",
    "series_autopilot_downloading_count",
    "series_autopilot_importing_count",
    "status_cache",
    "status_cache_age_seconds",
    "status_partial",
    "status_partial_reason",
}


def _handler_text(source: str) -> str:
    marker = "class Handler(BaseHTTPRequestHandler):"
    start = source.find(marker)
    if start < 0:
        return ""
    end = source.find("\ndef main(", start)
    if end < 0:
        end = len(source)
    return source[start:end]


def _method_text(handler: str, method: str) -> str:
    marker = f"    def {method}(self):"
    start = handler.find(marker)
    if start < 0:
        return ""
    next_method = handler.find("\n    def ", start + len(marker))
    if next_method < 0:
        next_method = len(handler)
    return handler[start:next_method]


def _extract_exact_paths(text: str):
    paths = set()
    for match in re.finditer(r'path\s*==\s*"([^"]+)"', text):
        paths.add(match.group(1))
    return paths


def _extract_prefixes(text: str):
    prefixes = set()
    for match in re.finditer(r'path\.startswith\("([^"]+)"\)', text):
        prefixes.add(match.group(1))
    return prefixes


def _function_text(source: str, function_name: str) -> str:
    marker = f"\ndef {function_name}("
    start = source.find(marker)
    if start < 0:
        if source.startswith(f"def {function_name}("):
            start = 0
        else:
            return ""
    else:
        start += 1
    next_function = source.find("\ndef ", start + len(marker))
    next_class = source.find("\nclass ", start + len(marker))
    candidates = [value for value in (next_function, next_class) if value >= 0]
    end = min(candidates) if candidates else len(source)
    return source[start:end]


def _status_contract_findings(source: str):
    findings = []
    function_texts = {name: _function_text(source, name) for name in REQUIRED_STATUS_FUNCTIONS}
    for name, text in function_texts.items():
        if not text:
            findings.append({"kind": "missing_status_function", "function": name, "message": f"status helper {name} is missing"})
    combined = "\n".join(function_texts.values())
    for field in sorted(REQUIRED_STATUS_FIELDS):
        if f'"{field}"' not in combined:
            findings.append({"kind": "missing_status_field", "field": field, "message": f"/status.json helpers should expose {field}"})
    for needle in (
        "source_health_summary(",
        "system_health_summary()",
        "bounded_status_state_summary(",
        "STATUS_CACHE_TTL_SECONDS",
        "start_status_cache_refresh()",
        "status_cache_age_seconds",
    ):
        if needle not in combined:
            findings.append({"kind": "missing_status_contract", "needle": needle, "message": f"/status.json contract should include {needle}"})
    return findings


def audit():
    findings = []
    if not WEB_FILE.exists():
        return {"ok": False, "findings": [{"kind": "missing_file", "file": str(WEB_FILE), "message": "web implementation file is missing"}]}
    source = WEB_FILE.read_text(encoding="utf-8", errors="replace")
    # Module-level script_path(...) constants moved to inkdrop_web_config.py
    # (re-exported into inkdrop_web via `from inkdrop_web_config import *`);
    # the worker-reference check below spans both files, everything else
    # here (routes, Handler, status contract) is still inkdrop_web.py only.
    worker_reference_source = source
    if WEB_CONFIG_FILE.exists():
        worker_reference_source += WEB_CONFIG_FILE.read_text(encoding="utf-8", errors="replace")
    handler = _handler_text(source)
    get_text = _method_text(handler, "do_GET")
    post_text = _method_text(handler, "do_POST")
    get_paths = _extract_exact_paths(get_text)
    post_paths = _extract_exact_paths(post_text)
    prefixes = _extract_prefixes(get_text) | _extract_prefixes(post_text)

    for path in sorted(REQUIRED_GET_PATHS - get_paths):
        findings.append({"kind": "missing_get_route", "path": path, "message": f"GET route {path} is missing from Handler.do_GET"})
    for path in sorted(REQUIRED_POST_PATHS - post_paths):
        findings.append({"kind": "missing_post_route", "path": path, "message": f"POST route {path} is missing from Handler.do_POST"})
    for prefix in sorted(REQUIRED_STARTSWITH_PREFIXES - prefixes):
        findings.append({"kind": "missing_route_prefix", "path": prefix, "message": f"route prefix {prefix} is missing from Handler"})
    findings.extend(_status_contract_findings(source))

    if "ThreadingHTTPServer((HOST, PORT), Handler)" not in source:
        findings.append({"kind": "missing_server_binding", "message": "web main should bind Handler through ThreadingHTTPServer"})

    for constant, filename in REQUIRED_WORKER_REFERENCES.items():
        if constant not in worker_reference_source or filename not in worker_reference_source:
            findings.append({"kind": "missing_worker_reference", "constant": constant, "file": filename, "message": f"{constant} should reference {filename}"})

    return {
        "ok": not findings,
        "get_route_count": len(get_paths),
        "post_route_count": len(post_paths),
        "prefix_count": len(prefixes),
        "required_worker_reference_count": len(REQUIRED_WORKER_REFERENCES),
        "required_status_field_count": len(REQUIRED_STATUS_FIELDS),
        "required_status_function_count": len(REQUIRED_STATUS_FUNCTIONS),
        "findings": findings,
    }


def main() -> int:
    payload = audit()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
