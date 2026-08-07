#!/usr/bin/env python3
"""Prove every registered InkDrop mutation has an explicit auth contract."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from core import inkdrop_auth_contracts
from core import inkdrop_web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def literal_routes(function):
    return {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/api/")
        and not node.value.endswith("/")
    }


def main():
    tree = ast.parse(Path(inkdrop_web.__file__).read_text(encoding="utf-8"))
    handlers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in {"do_POST", "do_DELETE"}
    }
    for handler_name, method in (("do_POST", "POST"), ("do_DELETE", "DELETE")):
        actual = literal_routes(handlers[handler_name])
        covered = {route for inventory_method, route in inkdrop_auth_contracts.MUTATION_ROUTE_INVENTORY if inventory_method == method}
        missing = sorted(actual - covered)
        stale = sorted(covered - actual)
        require(not missing, f"registered {method} routes missing auth inventory: {missing}")
        require(not stale, f"auth inventory has stale {method} routes: {stale}")

    expected_settings = {
        "/api/inkdrop-settings/app", "/api/inkdrop-settings/app/update",
        "/api/inkdrop-settings/provider", "/api/inkdrop-settings/provider/update",
    }
    for method in ("PATCH", "PUT"):
        covered = {route for inventory_method, route in inkdrop_auth_contracts.MUTATION_ROUTE_INVENTORY if inventory_method == method}
        require(covered == expected_settings, f"{method} settings inventory drifted")

    inventory = inkdrop_auth_contracts.public_inventory()
    for row in inventory:
        if row["authentication_methods"] != ["public"]:
            require(row["required_api_key_scope"], f"protected route lacks scope: {row}")
            require(row["csrf_required_for_cookie_sessions"], f"cookie mutation lacks CSRF: {row}")
        policy = inkdrop_web.inkdrop_auth_action_policy(row["route"], row["method"])
        require(policy["scope"] == row["scope"], f"runtime scope differs from inventory: {row}")
        require(policy["admin_only"] == row["admin_only"], f"runtime admin contract differs: {row}")

    required_routes = {
        "/api/auth/bootstrap", "/api/auth/login", "/api/auth/logout", "/api/auth/password",
        "/api/auth/sessions/revoke", "/api/auth/api-keys", "/api/auth/recovery/reset",
        "/api/comicvine/add", "/api/mangadex/add", "/api/inkdrop-state/series/run",
        "/api/inkdrop-state/wanted/run", "/api/inkdrop-state/queue/run",
        "/api/manual-review/approve", "/api/manual-review/ignore", "/api/manual-review/add-alias",
        "/api/inkdrop-settings/app/update", "/api/inkdrop-settings/provider/test",
        "/api/slskd-source-probe/run", "/api/source-probe", "/api/inkdrop-state/series/remove",
    }
    inventoried_routes = {row["route"] for row in inventory}
    require(required_routes <= inventoried_routes, f"required operator routes missing: {sorted(required_routes - inventoried_routes)}")

    print(json.dumps({"ok": True, "route_inventory": "passed", "mutation_routes": len(inventory)}, indent=2))


if __name__ == "__main__":
    main()
