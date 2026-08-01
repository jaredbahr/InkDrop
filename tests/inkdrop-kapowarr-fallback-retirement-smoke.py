#!/usr/bin/env python3
"""Prove retired Kapowarr fallback controls are hidden, inert, and rollback-safe."""

from __future__ import annotations

import inspect
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import inkdrop_backup_restore
import inkdrop_completed_import
import inkdrop_missing_acquire
import inkdrop_series_autopilot
import inkdrop_settings_registry
import inkdrop_state
import inkdrop_web


RETIRED = set(inkdrop_settings_registry.RETIRED_SETTING_KEYS)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    require(RETIRED == {
        "automation.kapowarr_missing_fallback",
        "automation.kapowarr_path_fallback",
        "automation.kapowarr_completed_import_fallback",
    }, "retired setting inventory drifted")
    require(not any(inkdrop_settings_registry.is_defined_public_setting(key) for key in RETIRED), "retired keys remain public")

    with tempfile.TemporaryDirectory(prefix="inkdrop-kapowarr-retired-", ignore_cleanup_errors=True) as tmp:
        root, db = Path(tmp), Path(tmp) / "state.sqlite3"
        inkdrop_state.sync_settings(db, settings=[{
            "key": key, "scope": "automation", "label": key, "value": True,
            "description": "legacy true fixture", "source": "user",
        } for key in sorted(RETIRED)])
        with inkdrop_state.connect_read(db) as con:
            stored = {row["key"]: json.loads(row["value_json"]) for row in con.execute(
                "select key,value_json from app_settings where key like 'automation.kapowarr_%_fallback'"
            )}
        require(stored == {key: True for key in RETIRED}, "legacy rows were not preserved for rollback/audit")

        snapshot = inkdrop_state.settings_snapshot(db)
        require(not (RETIRED & {row["key"] for row in snapshot.get("settings") or []}), "retired rows leaked through state settings snapshot")
        runtime = inkdrop_web.runtime_provider_settings()
        require(not (RETIRED & {row["key"] for row in runtime.get("settings") or []}), "retired runtime seeds remain public")
        merged = inkdrop_web.merge_runtime_settings_snapshot(snapshot, runtime)
        require(not (RETIRED & {row["key"] for row in merged.get("settings") or []}), "retired rows leaked through public settings merge")
        old_web_db = inkdrop_web.INKDROP_STATE_DB
        inkdrop_web.INKDROP_STATE_DB = db
        try:
            public_settings = inkdrop_web.inkdrop_settings_public(sync=False, area="other")
        finally:
            inkdrop_web.INKDROP_STATE_DB = old_web_db
        require(not (RETIRED & {row["key"] for row in public_settings.get("settings") or []}), "retired controls leaked through the public Settings API")

        for key in RETIRED:
            try:
                inkdrop_state.update_app_setting(db, key, False)
            except ValueError as exc:
                require("retired" in str(exc).lower(), f"retired mutation error was unclear: {exc}")
            else:
                raise AssertionError(f"retired setting remained mutable: {key}")

        exported = inkdrop_backup_restore.export_portable_settings(db, now=1_700_000_000, version="test")
        require(not (RETIRED & set(exported["settings"])), "retired settings were exported")
        excluded = {row["key"]: row["reason"] for row in exported.get("excluded") or []}
        require(all(excluded.get(key) == "unknown_or_deprecated" for key in RETIRED), "retired backup exclusions are not explicit")
        legacy_document = dict(exported)
        legacy_document["settings"] = {**legacy_document["settings"], **{key: False for key in RETIRED}}
        legacy_document["checksum"] = inkdrop_backup_restore._settings_checksum(legacy_document)
        preview = inkdrop_backup_restore.restore_portable_settings(db, json.dumps(legacy_document), apply=False)
        require({row["key"] for row in preview["plan"]["unknown"]} >= RETIRED, "legacy backup keys were not rejected as deprecated")
        applied = inkdrop_backup_restore.restore_portable_settings(db, json.dumps(legacy_document), apply=True)
        require({row["key"] for row in applied["plan"]["unknown"]} >= RETIRED, "apply-mode restore did not reject retired keys")
        with inkdrop_state.connect_read(db) as con:
            after_preview = {row["key"]: json.loads(row["value_json"]) for row in con.execute(
                "select key,value_json from app_settings where key like 'automation.kapowarr_%_fallback'"
            )}
        require(after_preview == stored, "retired restore preview mutated legacy rows")

        old_env = os.environ.get("INKDROP_KAPOWARR_MISSING_FALLBACK")
        os.environ["INKDROP_KAPOWARR_MISSING_FALLBACK"] = "true"
        old_missing_db = inkdrop_missing_acquire.INKDROP_STATE_DB
        old_series_db = inkdrop_series_autopilot.INKDROP_STATE_DB
        old_completed_db = inkdrop_completed_import.INKDROP_STATE_DB
        inkdrop_missing_acquire.INKDROP_STATE_DB = db
        inkdrop_series_autopilot.INKDROP_STATE_DB = db
        inkdrop_completed_import.INKDROP_STATE_DB = db
        try:
            require(not inkdrop_missing_acquire.kapowarr_missing_fallback_enabled(), "retired missing fallback honored DB/env true")
            require(not inkdrop_missing_acquire.kapowarr_missing_db_fallback_enabled(), "retired missing DB fallback enabled")
            require(not inkdrop_series_autopilot.kapowarr_path_fallback_enabled(), "retired path fallback enabled")
            require(not inkdrop_completed_import.completed_import_kapowarr_adapter_enabled(), "retired completed-import fallback enabled")
            with mock.patch.object(inkdrop_missing_acquire, "inkdrop_queue_missing_issues", return_value=[]), mock.patch.object(
                inkdrop_missing_acquire, "kapowarr_missing_issues", side_effect=AssertionError("Kapowarr fallback was called")
            ):
                require(inkdrop_missing_acquire.missing_issues(["Fixture"]) == [], "empty native queue did not stay native")
            require(inkdrop_series_autopilot.kapowarr_folder_prefixes_by_volume_id() == {}, "retired path fallback read Kapowarr folders")
        finally:
            inkdrop_missing_acquire.INKDROP_STATE_DB = old_missing_db
            inkdrop_series_autopilot.INKDROP_STATE_DB = old_series_db
            inkdrop_completed_import.INKDROP_STATE_DB = old_completed_db
            if old_env is None:
                os.environ.pop("INKDROP_KAPOWARR_MISSING_FALLBACK", None)
            else:
                os.environ["INKDROP_KAPOWARR_MISSING_FALLBACK"] = old_env

        view = inkdrop_state.kapowarr_shutdown_readiness_view(
            db, limit=100, summary_mode="compact", repo_root=Path(__file__).resolve().parent,
            container_probe=lambda: {"available": True, "containers": []},
        )
        fallback_rows = {row["id"]: row for row in view.get("rows") or [] if row.get("id") in {
            "kapowarr_missing_fallback", "kapowarr_path_fallback", "kapowarr_completed_import_fallback",
        }}
        require(len(fallback_rows) == 3, "shutdown readiness omitted retired diagnostics")
        require(all(row.get("retired") and row.get("ready") and not row.get("enabled") and not row.get("blocking") for row in fallback_rows.values()), "retired readiness rows are not permanently ready/off")
        require(not any((view.get("summary") or {}).get(key) for key in (
            "kapowarr_missing_fallback_enabled", "kapowarr_path_fallback_enabled", "kapowarr_completed_import_fallback_enabled",
        )), "shutdown summary projected a retired fallback enabled")

        web_source = Path(inkdrop_web.__file__).read_text(encoding="utf-8")
        section_source = inspect.getsource(inkdrop_web.runtime_provider_settings)
        require(not any(key in section_source for key in RETIRED), "runtime settings still render retired controls")
        require('if (key.includes("kapowarr")) return "Adapters";' not in web_source, "General still renders the retired Adapters subsection")
        for function in (inkdrop_web.script_status, inkdrop_web.light_script_status):
            source = inspect.getsource(function)
            for name in ("kapowarr_missing_fallback", "kapowarr_path_fallback", "kapowarr_completed_import_fallback"):
                require(f'"{name}": False' in source, f"{function.__name__} does not force {name} false")

        print(json.dumps({"ok": True, "retired_keys": sorted(RETIRED), "legacy_rows_preserved": True, "shutdown_ready": True}, sort_keys=True))


if __name__ == "__main__":
    main()
