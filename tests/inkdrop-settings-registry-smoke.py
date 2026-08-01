#!/usr/bin/env python3
import json
import tempfile
from pathlib import Path

import inkdrop_settings_registry as registry
import inkdrop_state


def require(value, message):
    if not value:
        raise AssertionError(message)


def main():
    require(registry.validate_value("automation.queue_watchdog_enabled", True) is True, "boolean validates")
    require(registry.validate_value("automation.queue_watchdog_slskd_stale_minutes", "45") == 45, "number coerces")
    stall_schema = registry.field_schema("automation.queue_watchdog_slskd_stale_minutes")
    require(
        stall_schema.get("units") == "minutes"
        and stall_schema.get("min") == 5
        and stall_schema.get("max") == 1440
        and stall_schema.get("default") == 45,
        f"SLSKD stall schema is incomplete: {stall_schema}",
    )
    try:
        registry.validate_value("automation.queue_watchdog_slskd_stale_minutes", 1)
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe stale window should be rejected")
    try:
        registry.validate_value("automation.queue_watchdog_slskd_stale_minutes", 1441)
    except ValueError:
        pass
    else:
        raise AssertionError("unbounded stale window should be rejected")
    require(registry.classify_environment_name("INKDROP_SABNZBD_API_KEY") == "secret", "secret classified")
    require(registry.classify_environment_name("INKDROP_STATE_DIR") == "container_bootstrap", "bootstrap classified")
    contract = registry.environment_contract({"INKDROP_STATE_DIR": "/state", "INKDROP_SABNZBD_API_KEY": "secret-value"})
    require(contract["values_exposed"] is False, "environment values stay private")
    require(all("value" not in row for row in contract["variables"]), "environment contract contains no values")

    # Windows can briefly retain SQLite/WAL handles after interpreter-level
    # schema caches are released. Assertions are complete before cleanup.
    with tempfile.TemporaryDirectory(prefix="inkdrop-settings-registry-", ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "inkdrop-state.sqlite3"
        inkdrop_state.sync_settings(
            db,
            settings=[
                {
                    "key": "automation.queue_watchdog_enabled",
                    "scope": "automation",
                    "label": "Queue Watchdog",
                    "value": True,
                    "description": "test",
                    "source": "runtime",
                },
                {
                    "key": "automation.queue_watchdog_slskd_stale_minutes",
                    "scope": "automation",
                    "label": "SLSKD Active Stall Threshold",
                    "value": 45,
                    "description": "test",
                    "source": "runtime",
                },
            ],
        )
        snapshot_rows = {row["key"]: row for row in inkdrop_state.settings_snapshot(db)["settings"]}
        stall_row = snapshot_rows["automation.queue_watchdog_slskd_stale_minutes"]
        require(
            stall_row.get("units") == "minutes"
            and stall_row.get("minimum") == 5
            and stall_row.get("maximum") == 1440
            and stall_row.get("default") == 45,
            f"effective settings row omitted SLSKD stall contract: {stall_row}",
        )
        inkdrop_state.update_app_setting(db, "automation.queue_watchdog_slskd_stale_minutes", 45)
        require(
            inkdrop_state.app_setting(db, "automation.queue_watchdog_slskd_stale_minutes")["source"] == "user",
            "saving an unchanged displayed runtime value did not persist explicit user intent",
        )
        inkdrop_state.update_app_setting(db, "automation.queue_watchdog_enabled", False)
        inkdrop_state.update_app_setting(db, "automation.queue_watchdog_slskd_stale_minutes", 60)
        require(inkdrop_state.app_setting(db, "automation.queue_watchdog_enabled")["value"] is False, "validated value stored")
        with inkdrop_state.connect(db) as con:
            policy = inkdrop_state.queue_watchdog_policy(con)
        require(policy["enabled"] is False, "watchdog reads SQLite setting")
        require(policy["slskd_stale_seconds"] == 60 * 60, "watchdog did not consume saved SLSKD threshold")
        try:
            inkdrop_state.update_app_setting(db, "automation.queue_watchdog_enabled", "false")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid boolean should not be stored")
    print(json.dumps({"ok": True, "settings_registry_smoke": "passed"}, indent=2))


if __name__ == "__main__":
    main()
