#!/usr/bin/env python3
import json
import tempfile
from pathlib import Path

from core import inkdrop_settings_registry as registry
from core import inkdrop_state


def require(value, message):
    if not value:
        raise AssertionError(message)


def main():
    require(registry.validate_value("automation.queue_watchdog_enabled", True) is True, "boolean validates")
    require(registry.validate_value("automation.queue_watchdog_slskd_stale_minutes", "45") == 45, "number coerces")
    require(registry.validate_value("media_management.minimum_free_space_gb", "0") == 0, "finite zero remains valid")
    for unsafe in (float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", "-Infinity"):
        try:
            registry.validate_value("media_management.minimum_free_space_gb", unsafe)
        except ValueError as exc:
            require("finite number" in str(exc), f"non-finite value returned an unclear error: {unsafe!r}")
        else:
            raise AssertionError(f"non-finite number should be rejected: {unsafe!r}")
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
    # An integer field is integer-valued, not integer-rounded. The numeric
    # validator parsed a float, bounds-checked it, then applied int() -- so
    # 1.9 days of backup interval was accepted and silently stored as 1,
    # changing what the operator asked for instead of telling them the field
    # takes whole numbers. Every integer spec in the registry is checked, so
    # a new one cannot be added back into the truncating branch.
    integer_keys = [key for key, spec in registry.NUMBER_SPECS.items() if spec.get("integer")]
    require(integer_keys, "control: the registry must actually declare some integer fields")
    for key in integer_keys:
        spec = registry.NUMBER_SPECS[key]
        fractional = spec["min"] + 0.5
        require(fractional < spec["max"], f"fixture for {key} must stay inside its own range")
        try:
            registry.validate_value(key, fractional)
        except ValueError as exc:
            require("whole number" in str(exc), f"{key} rejected {fractional} with an unclear error: {exc}")
        else:
            raise AssertionError(f"{key} is an integer field and must reject {fractional}")
        # The control: the whole number at the same bound still passes, so
        # the rejection above is about the fraction and not about the range.
        require(
            registry.validate_value(key, spec["min"]) == spec["min"],
            f"{key} must still accept the whole number at its own minimum",
        )

    # Input forms that already worked keep working: a numeric string, and a
    # float that happens to be whole, both carry no fractional component.
    require(registry.validate_value("backup.interval_days", "2.0") == 2, "a whole-valued numeric string stays accepted")
    require(registry.validate_value("backup.interval_days", 2.0) == 2, "a whole-valued float stays accepted")
    require(isinstance(registry.validate_value("backup.interval_days", "2.0"), int), "an integer field returns an int")

    # Fractional-hour fields are declared integer: False and must keep
    # accepting fractions -- this is the arm that proves the check reads the
    # spec rather than rejecting every fraction everywhere.
    require(
        registry.validate_value("automation.queue_watchdog_slskd_never_started_hours", 1.5) == 1.5,
        "a non-integer field must still accept a fractional value",
    )

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
                {
                    "key": "media_management.minimum_free_space_gb",
                    "scope": "media_management",
                    "label": "Minimum Free Space GB",
                    "value": 10,
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
        # The same rejection has to hold through the save path the settings
        # route actually calls, not just through the validator in isolation,
        # and a rejected save must leave the stored value untouched.
        try:
            inkdrop_state.update_app_setting(db, "automation.queue_watchdog_slskd_stale_minutes", 60.5)
        except ValueError as exc:
            require("whole number" in str(exc), f"the save path rejected a fraction with an unclear error: {exc}")
        else:
            raise AssertionError("saving a fractional value to an integer setting should be rejected")
        require(
            inkdrop_state.app_setting(db, "automation.queue_watchdog_slskd_stale_minutes")["value"] == 60,
            "a rejected fractional save changed the stored integer setting",
        )
        inkdrop_state.update_app_setting(db, "automation.queue_watchdog_slskd_stale_minutes", "90.0")
        require(
            inkdrop_state.app_setting(db, "automation.queue_watchdog_slskd_stale_minutes")["value"] == 90,
            "a whole-valued numeric string must still save through the settings route",
        )

        try:
            inkdrop_state.update_app_setting(db, "media_management.minimum_free_space_gb", "NaN")
        except ValueError:
            pass
        else:
            raise AssertionError("non-finite free-space guard should not be stored")
        require(
            inkdrop_state.app_setting(db, "media_management.minimum_free_space_gb")["value"] == 10,
            "rejected non-finite value changed the stored free-space guard",
        )
        inkdrop_state.update_app_setting(db, "media_management.minimum_free_space_gb", "0")
        require(
            inkdrop_state.app_setting(db, "media_management.minimum_free_space_gb")["value"] == 0,
            "explicit finite zero no longer follows the existing free-space contract",
        )
        try:
            inkdrop_state.update_app_setting(db, "automation.queue_watchdog_enabled", "false")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid boolean should not be stored")
    print(json.dumps({"ok": True, "settings_registry_smoke": "passed"}, indent=2))


if __name__ == "__main__":
    main()
