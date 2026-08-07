#!/usr/bin/env python3
"""Portable settings backup/restore contract regression."""

import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from core import inkdrop_backup_restore as backup
from core import inkdrop_state


def require(value, message):
    if not value:
        raise AssertionError(message)


def value(db, key):
    return inkdrop_state.app_setting(db, key)["value"]


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-settings-portable-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db = root / "inkdrop.sqlite3"
        snapshots = root / "backups"
        inkdrop_state.sync_settings(db, settings=[
            {"key": "automation.queue_watchdog_enabled", "scope": "automation", "label": "Watchdog", "value": True, "description": "test"},
            {"key": "automation.queue_watchdog_slskd_stale_minutes", "scope": "automation", "label": "Stall", "value": 45, "description": "test"},
            {"key": "ui.relative_dates", "scope": "ui", "label": "Dates", "value": True, "description": "test"},
            {"key": "general.log_level", "scope": "general", "label": "Log", "value": "info", "description": "test"},
            {"key": "automation.source_order", "scope": "automation", "label": "Order", "value": ["slskd", "prowlarr"], "description": "test"},
            {"key": "legacy.removed", "scope": "general", "label": "Legacy", "value": "stale", "description": "test"},
            {"key": "auth.allowed_origins", "scope": "auth", "label": "Origins", "value": ["https://private.example"], "description": "private"},
            {"key": "path.comic_root", "scope": "paths", "label": "Root", "value": "/private/comics", "description": "private"},
            {"key": "media_management.apply_planned_path", "scope": "media_management", "label": "Planned path", "value": True, "description": "policy"},
            {"key": "media_management.root_folder_strategy", "scope": "media_management", "label": "Root strategy", "value": "media_type", "description": "policy"},
            {"key": "media_management.series_folder_format", "scope": "media_management", "label": "Folder format", "value": "{Series Title} ({Year})", "description": "policy"},
            {"key": "media_management.manga_companion_folder_convergence", "scope": "media_management", "label": "Companion convergence", "value": True, "description": "policy"},
            {"key": "quality.allow_raw_image_page_folders", "scope": "quality", "label": "Raw pages", "value": False, "description": "policy"},
            {"key": "setup.local_folder_only_mode", "scope": "setup", "label": "Local only", "value": False, "description": "policy"},
        ])
        first = backup.export_portable_settings(db, now=1_700_000_000, version="0.1.0-alpha.7")
        second = backup.export_portable_settings(db, now=1_700_000_000, version="0.1.0-alpha.7")
        require(first == second, "fixed-time export must be deterministic")
        serialized = json.dumps(first, sort_keys=True)
        require("private.example" not in serialized and "/private/comics" not in serialized and '"stale"' not in serialized, "private/deprecated values leaked")
        require(first["contains"]["tasks_history_users_sessions"] is False, "scope contract missing")
        require(first["checksum"].startswith("sha256:"), "checksum missing")
        require(not backup.inkdrop_settings_registry.is_defined_public_setting("legacy.removed"), "deprecated fixture unexpectedly remains in the public registry")
        require("legacy.removed" not in first["settings"], "stale DB setting was exported")
        require(any(row["key"] == "legacy.removed" and row["reason"] == "unknown_or_deprecated" for row in first["excluded"]), "stale export exclusion missing")
        for policy_key, policy_value in (
            ("media_management.apply_planned_path", True),
            ("media_management.root_folder_strategy", "media_type"),
            ("media_management.series_folder_format", "{Series Title} ({Year})"),
            ("media_management.manga_companion_folder_convergence", True),
            ("quality.allow_raw_image_page_folders", False),
            ("setup.local_folder_only_mode", False),
        ):
            require(first["settings"].get(policy_key) == policy_value, f"portable policy dropped from export: {policy_key}")
        require("path.comic_root" not in first["settings"] and any(row["key"] == "path.comic_root" for row in first["excluded"]), "real path settings must stay excluded")
        require("auth.allowed_origins" not in first["settings"], "auth settings must stay excluded")
        for legacy_token in ("NaN", "Infinity", "-Infinity", "1e9999"):
            with sqlite3.connect(db) as con:
                con.execute("update app_settings set value_json=? where key='legacy.removed'", (legacy_token,))
                con.commit()
            nonfinite_legacy_export = backup.export_portable_settings(db, now=1_700_000_000, version="0.1.0-alpha.7")
            json.dumps(nonfinite_legacy_export, allow_nan=False)
            require(any(row["key"] == "legacy.removed" and "non_finite" in row["reason"] for row in nonfinite_legacy_export["excluded"]), f"legacy non-finite stored value was not safely excluded: {legacy_token}")

        restored = json.loads(json.dumps(first))
        restored["settings"]["automation.queue_watchdog_enabled"] = False
        restored["settings"]["automation.queue_watchdog_slskd_stale_minutes"] = 60
        restored["settings"]["media_management.apply_planned_path"] = False
        restored["checksum"] = backup._settings_checksum(restored)
        raw = json.dumps(restored)
        preview = backup.restore_portable_settings(db, raw, apply=False)
        require(preview["ok"] and preview["dry_run"] and len(preview["plan"]["changes"]) == 3, preview)
        require(value(db, "automation.queue_watchdog_enabled") is True, "preview mutated settings")
        applied = backup.restore_portable_settings(db, raw, apply=True, backup_dir=snapshots, now=1_700_000_001)
        require(applied["ok"] and applied["applied"] and applied["changed_count"] == 3, applied)
        require(value(db, "automation.queue_watchdog_enabled") is False and value(db, "automation.queue_watchdog_slskd_stale_minutes") == 60, "roundtrip failed")
        require(value(db, "media_management.apply_planned_path") is False, "formerly-dropped policy key did not restore")
        require(Path(applied["snapshot"]).exists(), "pre-restore snapshot missing")

        unknown = json.loads(raw)
        unknown["settings"]["future.setting"] = "ignored"
        unknown["settings"]["legacy.removed"] = "ignored"
        unknown["checksum"] = backup._settings_checksum(unknown)
        unknown_plan = backup.restore_portable_settings(db, json.dumps(unknown), apply=False)
        require(unknown_plan["ok"] and {row["key"] for row in unknown_plan["plan"]["unknown"]} == {"future.setting", "legacy.removed"}, "forward-compatible/deprecated unknown not reported")

        invalid = json.loads(raw)
        invalid["settings"]["automation.queue_watchdog_slskd_stale_minutes"] = 1
        invalid["checksum"] = backup._settings_checksum(invalid)
        invalid_plan = backup.restore_portable_settings(db, json.dumps(invalid), apply=False)
        require(not invalid_plan["ok"] and invalid_plan["plan"]["invalid"], "range error accepted")
        strict_values = (
            ("automation.queue_watchdog_slskd_stale_minutes", True),
            ("automation.queue_watchdog_slskd_stale_minutes", "60"),
            ("automation.queue_watchdog_slskd_stale_minutes", 60.0),
            ("automation.queue_watchdog_enabled", 1),
            ("automation.queue_watchdog_enabled", "false"),
            ("general.log_level", 1),
            ("automation.source_order", ["slskd", 1]),
        )
        require(backup.inkdrop_settings_registry.validate_value("automation.queue_watchdog_slskd_stale_minutes", "60") == 60, "portable strictness accidentally changed existing API coercion")
        for key, hostile_value in strict_values:
            hostile = json.loads(raw)
            hostile["settings"][key] = hostile_value
            hostile["checksum"] = backup._settings_checksum(hostile)
            strict_plan = backup.restore_portable_settings(db, json.dumps(hostile), apply=False)
            require(not strict_plan["ok"] and strict_plan["plan"]["invalid"], f"coercive portable value accepted: {key}={hostile_value!r}")
        for mutate in (
            lambda d: d.update(product="OtherArr"),
            lambda d: d.update(schema_version=2),
            lambda d: d.update(schema_version=True),
            lambda d: d.update(schema_version=1.0),
            lambda d: d.update(checksum="sha256:" + "0" * 64),
        ):
            hostile = json.loads(raw)
            mutate(hostile)
            if hostile.get("checksum", "").endswith("0" * 64) is False:
                hostile["checksum"] = backup._settings_checksum(hostile)
            try:
                backup.restore_portable_settings(db, json.dumps(hostile), apply=False)
            except ValueError:
                pass
            else:
                raise AssertionError("cross-product/schema/checksum misuse accepted")
        for hostile_raw in (
            '{"schema":"x","schema":"y"}',
            '{"settings":{"ui.relative_dates":true,"ui.relative_dates":false}}',
            '{"__proto__":{"polluted":true}}',
            '{"schema_version":NaN}',
            '{"settings":{"automation.queue_watchdog_slskd_stale_minutes":Infinity}}',
            '{"settings":{"automation.queue_watchdog_slskd_stale_minutes":-Infinity}}',
            '{"settings":{"automation.queue_watchdog_slskd_stale_minutes":1e9999}}',
            '{"settings":{"automation.source_order":["slskd",NaN]}}',
            "x" * (backup.PORTABLE_SETTINGS_MAX_BYTES + 1),
        ):
            try:
                backup.parse_portable_settings_document(hostile_raw)
            except ValueError:
                pass
            else:
                raise AssertionError("malformed/adversarial JSON accepted")
        for nonfinite in (float("nan"), float("inf"), float("-inf")):
            programmatic = json.loads(raw)
            programmatic["settings"]["automation.queue_watchdog_slskd_stale_minutes"] = nonfinite
            programmatic["checksum"] = first["checksum"]
            try:
                backup._settings_checksum(programmatic)
            except ValueError:
                pass
            else:
                raise AssertionError("canonical checksum accepted a non-finite programmatic document")
        numeric_list = json.loads(raw)
        numeric_list["settings"]["automation.source_order"] = ["slskd", float("nan")]
        try:
            backup._settings_checksum(numeric_list)
        except ValueError:
            pass
        else:
            raise AssertionError("canonical checksum accepted nested list NaN")

        before_nonfinite_values = {
            "automation.queue_watchdog_enabled": value(db, "automation.queue_watchdog_enabled"),
            "automation.queue_watchdog_slskd_stale_minutes": value(db, "automation.queue_watchdog_slskd_stale_minutes"),
        }
        with inkdrop_state.connect_read(db) as con:
            before_nonfinite_audits = con.execute("select count(*) from history_events where event_type='settings_restore'").fetchone()[0]
        before_nonfinite_snapshots = set(snapshots.glob("*.json"))
        for token in ("NaN", "Infinity", "-Infinity", "1e9999"):
            hostile_apply = raw.replace('"automation.queue_watchdog_slskd_stale_minutes": 60', f'"automation.queue_watchdog_slskd_stale_minutes": {token}')
            try:
                backup.restore_portable_settings(db, hostile_apply, apply=True, backup_dir=snapshots, now=1_700_000_002)
            except ValueError:
                pass
            else:
                raise AssertionError(f"non-finite apply token accepted: {token}")
        require({key: value(db, key) for key in before_nonfinite_values} == before_nonfinite_values, "non-finite apply mutated settings")
        with inkdrop_state.connect_read(db) as con:
            require(con.execute("select count(*) from history_events where event_type='settings_restore'").fetchone()[0] == before_nonfinite_audits, "non-finite apply wrote an audit")
        require(set(snapshots.glob("*.json")) == before_nonfinite_snapshots, "non-finite apply created a snapshot")

        rollback = backup.export_portable_settings(db, now=1_700_000_002, version="0.1.0-alpha.7")
        rollback["settings"]["automation.queue_watchdog_enabled"] = True
        rollback["settings"]["ui.relative_dates"] = False
        rollback["checksum"] = backup._settings_checksum(rollback)
        original_apply = backup._apply_portable_setting
        calls = {"count": 0}
        def fail_second(con, key, setting_value, now):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("fixture failure")
            return original_apply(con, key, setting_value, now)
        with mock.patch.object(backup, "_apply_portable_setting", side_effect=fail_second):
            try:
                backup.restore_portable_settings(db, json.dumps(rollback), apply=True, backup_dir=snapshots, now=1_700_000_003)
            except RuntimeError:
                pass
            else:
                raise AssertionError("injected transactional failure did not escape")
        require(value(db, "automation.queue_watchdog_enabled") is False and value(db, "ui.relative_dates") is True, "failed restore partially committed")
        with mock.patch.object(backup, "_write_settings_snapshot", side_effect=OSError("snapshot failed")):
            try:
                backup.restore_portable_settings(db, json.dumps(rollback), apply=True, backup_dir=snapshots, now=1_700_000_004)
            except OSError:
                pass
            else:
                raise AssertionError("snapshot failure did not abort restore")
        require(value(db, "automation.queue_watchdog_enabled") is False and value(db, "ui.relative_dates") is True, "snapshot failure mutated settings")
        with mock.patch.object(inkdrop_state, "record_settings_history", side_effect=RuntimeError("audit failed")):
            try:
                backup.restore_portable_settings(db, json.dumps(rollback), apply=True, backup_dir=snapshots, now=1_700_000_005)
            except RuntimeError:
                pass
            else:
                raise AssertionError("audit failure did not abort restore")
        require(value(db, "automation.queue_watchdog_enabled") is False and value(db, "ui.relative_dates") is True, "audit failure partially committed settings")
        with inkdrop_state.connect_read(db) as con:
            require(con.execute("select count(*) from history_events where event_type='settings_restore'").fetchone()[0] == 1, "truthful restore audit missing or duplicated")

        # A writer that commits before BEGIN IMMEDIATE must be part of both the
        # apply plan and snapshot. A writer arriving after the fence must block,
        # then may commit normally after restore releases the lock.
        with sqlite3.connect(db) as con:
            con.execute("update app_settings set value_json='false' where key='ui.relative_dates'")
            con.commit()
        race_document = backup.export_portable_settings(db, now=1_700_000_006, version="0.1.0-alpha.7")
        race_document["settings"]["ui.relative_dates"] = True
        race_document["checksum"] = backup._settings_checksum(race_document)
        lock_held = threading.Event()
        writer_started = threading.Event()
        writer_done = threading.Event()
        original_snapshot = backup._write_settings_snapshot

        def fenced_snapshot(rows, backup_dir, *, now=None):
            row_values = {row["key"]: json.loads(row["value_json"]) for row in rows}
            require(row_values["ui.relative_dates"] is False, "pre-lock update missing from fenced rows")
            lock_held.set()
            require(writer_started.wait(2), "concurrent writer did not start")
            time.sleep(0.15)
            require(not writer_done.is_set(), "writer bypassed BEGIN IMMEDIATE fence")
            return original_snapshot(rows, backup_dir, now=now)

        def concurrent_writer():
            require(lock_held.wait(2), "restore never acquired write fence")
            writer_started.set()
            with sqlite3.connect(db, timeout=5) as con:
                con.execute("update app_settings set value_json='false', source='user' where key='ui.relative_dates'")
                con.commit()
            writer_done.set()

        writer = threading.Thread(target=concurrent_writer)
        writer.start()
        with mock.patch.object(backup, "_write_settings_snapshot", side_effect=fenced_snapshot), mock.patch.object(
            backup.inkdrop_state, "connect_read", side_effect=AssertionError("apply opened a second DB read")
        ):
            raced = backup.restore_portable_settings(db, json.dumps(race_document), apply=True, backup_dir=snapshots, now=1_700_000_007)
        writer.join(5)
        require(writer_done.is_set(), "blocked writer did not resume after commit")
        require(raced["plan"]["changes"][0]["from"] is False, "apply plan did not use fenced current state")
        snapshot_document = json.loads(Path(raced["snapshot"]).read_text(encoding="utf-8"))
        require(snapshot_document["settings"]["ui.relative_dates"] is False, "snapshot differs from exact fenced pre-apply state")
        require(value(db, "ui.relative_dates") is False, "post-fence writer was silently overwritten")
        print(json.dumps({"ok": True, "portable_settings": "passed"}))


if __name__ == "__main__":
    main()
