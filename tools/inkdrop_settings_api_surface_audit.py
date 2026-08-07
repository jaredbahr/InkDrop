#!/usr/bin/env python3
"""Static audit for InkDrop settings API route/helper wiring."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_FILE = ROOT / "core" / "inkdrop_web.py"
STATE_FILE = ROOT / "core" / "inkdrop_state.py"

REQUIRED_GET_ROUTES = {
    "/api/inkdrop-settings": "inkdrop_settings_public(sync=False",
}

REQUIRED_POST_ROUTES = {
    "/api/inkdrop-settings/sync": "inkdrop_settings_public(sync=True",
    "/api/inkdrop-settings/provider/add": "add_inkdrop_provider_from_template(data)",
    "/api/inkdrop-settings/provider/claim": "claim_inkdrop_provider_settings(data)",
    "/api/inkdrop-settings/provider/update": "update_inkdrop_provider_settings(data)",
    "/api/inkdrop-settings/provider/recommendation/apply": "apply_inkdrop_provider_recommendation(data)",
    "/api/inkdrop-settings/provider/test": "test_inkdrop_provider(data)",
    "/api/inkdrop-settings/app/update": "update_inkdrop_app_setting(data)",
}

REQUIRED_PATCH_ROUTES = {
    "/api/inkdrop-settings/app": "update_inkdrop_app_setting(data)",
    "/api/inkdrop-settings/provider": "update_inkdrop_provider_settings(data)",
}

REQUIRED_PUT_ROUTES = {
    "/api/inkdrop-settings/app": "update_inkdrop_app_setting(data)",
    "/api/inkdrop-settings/provider": "update_inkdrop_provider_settings(data)",
}

REQUIRED_DELETE_ROUTES = {
    "/api/inkdrop-auth/api-keys/revoke": "inkdrop_state.revoke_api_key(INKDROP_STATE_DB, key_id",
    "/api/inkdrop-auth/api-keys/": "inkdrop_state.revoke_api_key(INKDROP_STATE_DB, key_id",
    "/api/auth/api-keys/revoke": "inkdrop_state.revoke_api_key(INKDROP_STATE_DB, key_id",
    "/api/auth/api-keys/": "inkdrop_state.revoke_api_key(INKDROP_STATE_DB, key_id",
}

REQUIRED_WEB_HELPER_SNIPPETS = {
    "inkdrop_settings_public": (
        "def inkdrop_settings_public(sync=False",
        "inkdrop_state.settings_snapshot(INKDROP_STATE_DB)",
        "inkdrop_state.sync_settings(",
        "merge_runtime_settings_snapshot(snapshot, runtime)",
        "augment_settings_provider_health(snapshot, live_health=live_settings_health)",
    ),
    "claim_inkdrop_provider_settings": (
        "def claim_inkdrop_provider_settings(payload):",
        "provider_settings_patch_from_payload(payload)",
        "inkdrop_state.provider_config(INKDROP_STATE_DB, provider_id)",
        "runtime_provider_template_for_claim(provider_id)",
        "inkdrop_state.update_provider_config(INKDROP_STATE_DB, provider_id, patch)",
    ),
    "update_inkdrop_provider_settings": (
        "def update_inkdrop_provider_settings(payload):",
        "inkdrop_state.update_provider_config(INKDROP_STATE_DB, provider_id, patch)",
        "runtime_provider_by_id(provider_id)",
        "inkdrop_state.sync_settings(INKDROP_STATE_DB, providers=[runtime_provider], settings=[])",
    ),
    "add_inkdrop_provider_from_template": (
        "def add_inkdrop_provider_from_template(payload):",
        "runtime_provider_settings()",
        "inkdrop_state.sync_settings(",
        "inkdrop_state.add_provider_config_from_template(",
    ),
    "apply_inkdrop_provider_recommendation": (
        "def apply_inkdrop_provider_recommendation(payload):",
        "inkdrop_state.apply_source_provider_recommendation(",
        "dry_run=dry_run",
    ),
    "test_inkdrop_provider": (
        "def test_inkdrop_provider(payload):",
        "provider_for_direct_test(provider_id)",
        "active_health_checks",
        '"provider_id": provider_id',
    ),
    "update_inkdrop_app_setting": (
        "def update_inkdrop_app_setting(payload):",
        "normalize_series_autopilot_source_order(value)",
        "normalize_protocol_order(value)",
        "inkdrop_state.update_app_setting(INKDROP_STATE_DB, key, value)",
    ),
}

REQUIRED_STATE_SNIPPETS = {
    "provider_config": (
        "def provider_config(db_path, provider_id):",
        "select id, provider_type, display_name, enabled, base_url, secret_ref",
    ),
    "update_provider_config": (
        "def update_provider_config(db_path, provider_id, patch):",
        'allowed = {"enabled", "base_url", "secret_ref", "settings"}',
        'updates = {"source": "user", "updated_at": now}',
        "_provider_settings_patch(current, incoming)",
    ),
    "add_provider_config_from_template": (
        "def add_provider_config_from_template(",
        "source_template_instance",
        '"ownership": "user"',
    ),
    "update_app_setting": (
        "def update_app_setting(db_path, key, value):",
        "source='user'",
    ),
    "record_provider_test": (
        "def record_provider_test(db_path, result):",
        '"provider_test"',
        "history_events",
    ),
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


def _function_text(source: str, name: str) -> str:
    match = re.search(rf"^def {re.escape(name)}\(.*?(?=^def |\Z)", source, flags=re.MULTILINE | re.DOTALL)
    return match.group(0) if match else ""


def _handler_method_text(handler: str, name: str) -> str:
    marker = f"    def {name}(self"
    start = handler.find(marker)
    if start < 0:
        return ""
    next_method = handler.find("\n    def ", start + len(marker))
    if next_method < 0:
        next_method = len(handler)
    return handler[start:next_method]


def _path_branch_text(method_text: str, path: str) -> str:
    pattern = re.compile(
        rf"(?:if|elif)\s+path\s*==\s*\"{re.escape(path)}\"\s*:(?P<body>.*?)(?=\n\s+elif\s+path\s*==|\n\s+elif\s+path\s+in\s+\{{|\n\s+else:|\Z)",
        flags=re.DOTALL,
    )
    match = pattern.search(method_text)
    return match.group(0) if match else ""


def _path_in_text(method_text: str, path: str) -> bool:
    return f'path == "{path}"' in method_text


def audit():
    findings = []
    if not WEB_FILE.exists():
        return {"ok": False, "findings": [{"kind": "missing_file", "file": str(WEB_FILE), "message": "web file is missing"}]}
    if not STATE_FILE.exists():
        return {"ok": False, "findings": [{"kind": "missing_file", "file": str(STATE_FILE), "message": "state file is missing"}]}

    web_source = WEB_FILE.read_text(encoding="utf-8", errors="replace")
    state_source = STATE_FILE.read_text(encoding="utf-8", errors="replace")
    handler = _handler_text(web_source)
    get_text = _method_text(handler, "do_GET")
    post_text = _method_text(handler, "do_POST")
    patch_text = _method_text(handler, "do_PATCH")
    put_text = _method_text(handler, "do_PUT")
    delete_text = _method_text(handler, "do_DELETE")
    settings_write_text = _handler_method_text(handler, "_handle_settings_write")

    for path, required_snippet in sorted(REQUIRED_GET_ROUTES.items()):
        branch = _path_branch_text(get_text, path)
        if not branch:
            findings.append({"kind": "missing_get_route", "path": path, "message": f"GET route {path} is missing"})
            continue
        if required_snippet not in branch:
            findings.append({"kind": "get_route_contract_mismatch", "path": path, "message": f"GET {path} should call {required_snippet}"})
        if "sync=True" in branch:
            findings.append({"kind": "get_settings_mutates_state", "path": path, "message": "GET settings route must not force settings sync"})
        if "area=area" not in branch:
            findings.append({"kind": "get_settings_area_missing", "path": path, "message": "GET settings route should preserve area-scoped loading"})

    for path, required_snippet in sorted(REQUIRED_POST_ROUTES.items()):
        if not _path_in_text(post_text, path):
            findings.append({"kind": "missing_post_route", "path": path, "message": f"POST route {path} is missing"})
            continue
        branch = _path_branch_text(post_text, path)
        if required_snippet not in branch:
            findings.append({"kind": "post_route_contract_mismatch", "path": path, "message": f"POST {path} should call {required_snippet}"})
        if path == "/api/inkdrop-settings/sync" and "area=area" not in branch:
            findings.append({"kind": "post_settings_area_missing", "path": path, "message": "POST settings sync route should preserve area-scoped loading"})
        if path == "/api/inkdrop-settings/provider/test" and "inkdrop_state.record_provider_test(INKDROP_STATE_DB, result)" not in branch:
            findings.append({"kind": "provider_test_history_missing", "path": path, "message": "provider test route should record provider_test history"})
        if path != "/api/inkdrop-settings/provider/test" and "record_provider_test" in branch:
            findings.append({"kind": "unexpected_provider_test_history", "path": path, "message": "only provider test route should write provider_test history"})

    for method, method_text, routes in (
        ("PATCH", patch_text + settings_write_text, REQUIRED_PATCH_ROUTES),
        ("PUT", put_text + settings_write_text, REQUIRED_PUT_ROUTES),
        ("DELETE", delete_text, REQUIRED_DELETE_ROUTES),
    ):
        if not method_text:
            findings.append({"kind": f"missing_{method.lower()}_handler", "message": f"do_{method} handler is missing"})
            continue
        for path, required_snippet in sorted(routes.items()):
            if path not in method_text:
                findings.append({"kind": f"missing_{method.lower()}_route", "path": path, "message": f"{method} route {path} is missing"})
                continue
            if required_snippet not in method_text:
                findings.append({"kind": f"{method.lower()}_route_contract_mismatch", "path": path, "message": f"{method} {path} should call {required_snippet}"})

    for name, snippets in sorted(REQUIRED_WEB_HELPER_SNIPPETS.items()):
        body = _function_text(web_source, name)
        if not body:
            findings.append({"kind": "missing_web_helper", "helper": name, "message": f"web helper {name} is missing"})
            continue
        for snippet in snippets:
            if snippet not in body:
                findings.append({"kind": "web_helper_contract_mismatch", "helper": name, "snippet": snippet, "message": f"{name} should include {snippet}"})
        if name in {"claim_inkdrop_provider_settings", "update_inkdrop_provider_settings", "add_inkdrop_provider_from_template", "update_inkdrop_app_setting"} and "con.execute" in body:
            findings.append({"kind": "web_helper_raw_sql", "helper": name, "message": f"{name} should delegate settings writes to inkdrop_state helpers"})

    for name, snippets in sorted(REQUIRED_STATE_SNIPPETS.items()):
        body = _function_text(state_source, name)
        if not body:
            findings.append({"kind": "missing_state_helper", "helper": name, "message": f"state helper {name} is missing"})
            continue
        for snippet in snippets:
            if snippet not in body:
                findings.append({"kind": "state_helper_contract_mismatch", "helper": name, "snippet": snippet, "message": f"{name} should include {snippet}"})
    update_body = _function_text(state_source, "update_provider_config")
    if update_body and any(disallowed in update_body for disallowed in ('"provider_type"', '"display_name"', '"ownership"', '"automation_role"', '"capabilities_json"')):
        findings.append({"kind": "provider_update_too_broad", "helper": "update_provider_config", "message": "provider update helper should keep editable fields narrow"})

    return {
        "ok": not findings,
        "get_settings_route_count": sum(1 for path in REQUIRED_GET_ROUTES if _path_in_text(get_text, path)),
        "post_settings_route_count": sum(1 for path in REQUIRED_POST_ROUTES if _path_in_text(post_text, path)),
        "patch_settings_route_count": sum(1 for path in REQUIRED_PATCH_ROUTES if path in patch_text + settings_write_text),
        "put_settings_route_count": sum(1 for path in REQUIRED_PUT_ROUTES if path in put_text + settings_write_text),
        "delete_settings_route_count": sum(1 for path in REQUIRED_DELETE_ROUTES if path in delete_text),
        "required_web_helper_count": len(REQUIRED_WEB_HELPER_SNIPPETS),
        "required_state_helper_count": len(REQUIRED_STATE_SNIPPETS),
        "findings": findings,
    }


def main() -> int:
    payload = audit()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
