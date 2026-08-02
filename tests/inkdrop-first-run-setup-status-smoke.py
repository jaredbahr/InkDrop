#!/usr/bin/env python3
"""Smoke-test InkDrop first-run setup status for Docker-first installs."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import inkdrop_state
from tools import inkdrop_install_support_summary


OPTIONAL_ADAPTER_KEYS = (
    "INKDROP_COMICVINE_API_KEY",
    "INKDROP_PROWLARR_URL",
    "INKDROP_PROWLARR_API_KEY",
    "INKDROP_SABNZBD_URL",
    "INKDROP_SABNZBD_API_KEY",
    "INKDROP_QBITTORRENT_URL",
    "INKDROP_QBITTORRENT_USERNAME",
    "INKDROP_QBITTORRENT_PASSWORD",
    "INKDROP_SLSKD_API_BASE_URL",
    "INKDROP_SUWAYOMI_API_BASE_URL",
    "INKDROP_KAVITA_URL",
    "INKDROP_KOMGA_URL",
)


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def with_env(updates):
    class _Env:
        def __enter__(self):
            self.old = {key: os.environ.get(key) for key in updates}
            self.removed = {key: os.environ.get(key) for key in OPTIONAL_ADAPTER_KEYS}
            for key in OPTIONAL_ADAPTER_KEYS:
                os.environ.pop(key, None)
            for key, value in updates.items():
                os.environ[key] = str(value)
            return self

        def __exit__(self, exc_type, exc, tb):
            for key in updates:
                if self.old[key] is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = self.old[key]
            for key, value in self.removed.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            return False

    return _Env()


def main():
    with tempfile.TemporaryDirectory(
        prefix="inkdrop-first-run-",
        ignore_cleanup_errors=os.name == "nt",
    ) as tmp:
        root = Path(tmp)
        config = root / "config"
        state = root / "state"
        backup = state / "backups"
        staging = root / "staging"
        manual = root / "manual-inbox"
        comics = root / "library" / "comics"
        manga = root / "library" / "manga"
        for path in (config, state, backup, staging, manual, comics, manga):
            path.mkdir(parents=True, exist_ok=True)
        env = {
            "INKDROP_CONFIG_DIR": str(config),
            "INKDROP_STATE_DIR": str(state),
            "INKDROP_BACKUP_DIR": str(backup),
            "INKDROP_STAGING_DIR": str(staging),
            "INKDROP_MANUAL_INBOX_DIR": str(manual),
            "INKDROP_COMIC_ROOT": str(comics),
            "INKDROP_MANGA_ROOT": str(manga),
        }
        missing_root = root / "missing-library"
        missing_env = dict(env)
        missing_env["INKDROP_COMIC_ROOT"] = str(missing_root / "comics")
        missing_env["INKDROP_MANGA_ROOT"] = str(missing_root / "manga")
        # Keep the incomplete and ready profiles isolated. The status probe may
        # initialize durable state, and a second profile must not inherit paths
        # created or persisted by the first one.
        missing = inkdrop_state.first_run_setup_status(state / "missing-roots.sqlite3", environ=missing_env)
        assert_true(missing["complete"] is False, "missing library roots must not report setup complete")
        assert_true(missing["status"] == "setup_incomplete", "missing roots must expose setup_incomplete")
        assert_true(missing["readiness"]["local_manual"]["ready"] is False, "missing roots must block local/manual readiness")
        assert_true(missing["readiness"]["library_roots"]["ready"] is False, "missing roots need their own truthful capability")
        assert_true(missing["readiness"]["import"]["ready"] is False, "missing roots must block import readiness")
        assert_true(missing["readiness"]["acquisition"]["ready"] is False, "blank providers must not report acquisition readiness")
        assert_true(missing["readiness"]["download_client"]["ready"] is False, "blank clients must not report handoff readiness")
        assert_true(missing["readiness"]["reader"]["state"] == "optional_not_configured", "optional reader must be truthful without blocking local mode")
        assert_true(missing["readiness"]["automatic_search"]["ready"] is False, "Automatic Search must wait for configuration")
        assert_true(not next(group for group in missing["groups"] if group["id"] == "source_providers")["ready"], "source group must not be hard-coded ready")

        status = inkdrop_state.first_run_setup_status(state / "inkdrop-state.sqlite3", environ=env)
        group_ids = {group["id"] for group in status["groups"]}
        expected_groups = {group["id"] for group in inkdrop_state.FIRST_RUN_SETUP_GROUPS}
        assert_true(status["implemented"] is True, "first-run status should report implemented")
        assert_true(status["status"] == "local_manual_ready", "providerless minimal mode must identify only local/manual readiness")
        assert_true(status["status_marker"] == status["status"], "status_marker should match setup status")
        assert_true(status["setup_complete"] is False, "providerless local mode must not claim broad setup completion")
        assert_true(status["setup_incomplete"] is False, "setup_incomplete boolean should be false for a complete local-folder profile")
        assert_true(status["setup_required"] is False, "setup_required should not be true once runtime setup is implemented")
        assert_true(status["presentation"]["kind"] == "setup_checklist", "first-run setup should advertise checklist presentation")
        assert_true(status["presentation"]["read_only_status"] is True, "first-run setup should be explicit read-only status data")
        assert_true(status["presentation"]["editable_settings_live_elsewhere"] is True, "setup should point UI toward editable settings sections")
        assert_true(status["complete"] is False, "valid minimal mode must remain distinct from full workflow completion")
        assert_true(status["readiness"]["local_manual"]["ready"] is True, "local/manual capability should be ready")
        assert_true("administrator" in status["readiness"], "administrator readiness must be reported independently")
        assert_true(status["readiness"]["library_roots"]["ready"] is True, "writable roots should satisfy library-root readiness")
        assert_true(status["readiness"]["metadata"]["state"] == "local_mode", "local metadata mode should be explicit")
        assert_true(status["readiness"]["acquisition"]["ready"] is False, "minimal mode must not imply acquisition readiness")
        assert_true(status["readiness"]["automatic_search"]["ready"] is False, "minimal mode must not imply Automatic Search readiness")
        assert_true(status["readiness"]["reader_visibility"]["state"] == "not_configured", "reader visibility must remain separate from imported state")
        assert_true(status["operating_modes"]["local_manual"]["ready"] is True, "local/manual mode should be explicitly valid")
        assert_true(status["operating_modes"]["automatic_acquisition"]["ready"] is False, "minimal mode must not imply automatic acquisition readiness")
        assert_true(group_ids == expected_groups, "first-run groups do not match runtime contract")
        assert_true(status["local_folder_only"]["supported"] is True, "local-folder-only mode should be supported")
        assert_true(status["local_folder_only"]["requires_external_adapters"] is False, "local-folder-only must not require adapters")
        assert_true(status["download_clients"]["optional"] is True, "download clients should be optional")
        assert_true(status["source_providers"]["optional"] is True, "source providers should be optional")
        assert_true(status["library_adapters"]["optional"] is True, "library adapters should be optional")
        assert_true(not status["adapter_status"]["qbittorrent"]["configured"], "qBittorrent should not be auto-configured")
        assert_true(status["adapter_status"]["qbittorrent"]["optional"] is True, "qBittorrent should be marked optional")
        assert_true(status["adapter_status"]["qbittorrent"]["required_for_startup"] is False, "qBittorrent must not be required for startup")
        assert_true(status["adapter_status"]["qbittorrent"]["status"] == "optional_unconfigured", "blank qBittorrent should have explicit optional status")
        assert_true(not status["adapter_status"]["sabnzbd"]["configured"], "SABnzbd should not be auto-configured")
        assert_true(status["adapter_status"]["sabnzbd"]["status_label"] == "Optional - not configured", "blank SAB should have operator-friendly optional label")
        assert_true("network_exposure_warning" in status["security_summary"], "security summary missing exposure warning")

        disabled_db = state / "disabled-integrations.sqlite3"
        with inkdrop_state.connect(disabled_db) as con:
            inkdrop_state.init_schema(con)
            stamp = time.time()
            for adapter_id, capabilities in (
                ("prowlarr", ["candidate_production"]),
                ("qbittorrent", ["download", "import"]),
            ):
                con.execute(
                    """insert into provider_configs(
                           id, provider_type, display_name, enabled, base_url, secret_ref,
                           capabilities_json, settings_json, source, created_at, updated_at
                       ) values(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        adapter_id, adapter_id, adapter_id, 0, f"http://{adapter_id}.invalid",
                        f"env:INKDROP_{adapter_id.upper()}_SECRET", json.dumps(capabilities),
                        "{}", "rehearsal", stamp, stamp,
                    ),
                )
            con.commit()
        disabled = inkdrop_state.first_run_setup_status(disabled_db, environ=env)
        assert_true(disabled["adapter_status"]["prowlarr"]["status"] == "disabled", "disabled Prowlarr status was relabeled")
        assert_true(disabled["adapter_status"]["qbittorrent"]["status"] == "disabled", "disabled qBittorrent status was relabeled")
        assert_true(disabled["readiness"]["acquisition"]["ready"] is False, "disabled provider must not satisfy acquisition readiness")
        assert_true(disabled["readiness"]["download_client"]["ready"] is False, "disabled client must not satisfy handoff readiness")
        assert_true(disabled["readiness"]["automatic_search"]["ready"] is False, "disabled integrations must not satisfy Automatic Search readiness")

        instance_db = state / "download-client-instance.sqlite3"
        with inkdrop_state.connect(instance_db) as con:
            inkdrop_state.init_schema(con)
            stamp = time.time()
            con.execute(
                """insert into download_client_instances(
                       id, name, name_key, client_type, enabled, priority, base_url,
                       created_at, updated_at
                   ) values(?,?,?,?,?,?,?,?,?)""",
                ("transmission-1", "Transmission", "transmission", "transmission", 1, 100, "http://transmission.invalid", stamp, stamp),
            )
            con.commit()
        instance_status = inkdrop_state.first_run_setup_status(instance_db, environ=env)
        assert_true(
            "transmission" in instance_status["download_clients"]["configured"],
            "a real enabled download_client_instances row must count toward download-client readiness even with no legacy env vars set",
        )
        assert_true(instance_status["readiness"]["download_client"]["ready"] is True, "an enabled download_client_instances row alone should satisfy handoff readiness")

        disabled_instance_db = state / "download-client-instance-disabled.sqlite3"
        with inkdrop_state.connect(disabled_instance_db) as con:
            inkdrop_state.init_schema(con)
            stamp = time.time()
            con.execute(
                """insert into download_client_instances(
                       id, name, name_key, client_type, enabled, priority, base_url,
                       created_at, updated_at
                   ) values(?,?,?,?,?,?,?,?,?)""",
                ("transmission-1", "Transmission", "transmission", "transmission", 0, 100, "http://transmission.invalid", stamp, stamp),
            )
            con.commit()
        disabled_instance_status = inkdrop_state.first_run_setup_status(disabled_instance_db, environ=env)
        assert_true(
            "transmission" not in disabled_instance_status["download_clients"]["configured"],
            "a disabled download_client_instances row must not count toward readiness",
        )
        assert_true(disabled_instance_status["readiness"]["download_client"]["ready"] is False, "a disabled download client instance alone must not satisfy handoff readiness")

        snapshot = inkdrop_state.settings_snapshot(state / "missing-state.sqlite3")
        assert_true(snapshot["reason"] == "state_db_missing", "missing DB snapshot should remain explicit")
        assert_true(snapshot["first_run_setup"]["implemented"] is True, "missing DB snapshot should include first-run setup")

        with with_env(env):
            summary = inkdrop_install_support_summary.build_summary()
        assert_true("first_run_setup" in summary, "install support summary missing first-run setup")
        assert_true(summary["first_run_setup"]["local_folder_only"]["supported"] is True, "install summary missing local-folder support")
        assert_true(summary["first_run_setup"]["presentation"]["kind"] == "setup_checklist", "install summary missing setup checklist presentation")
        assert_true(summary["install_defaults"]["optional_adapter_defaults"]["qbittorrent_url"] == "", "qBit default should be blank")
        assert_true(summary["install_defaults"]["optional_adapter_defaults"]["sabnzbd_url"] == "", "SAB default should be blank")
    print("INKDROP_FIRST_RUN_SETUP_STATUS_OK")


if __name__ == "__main__":
    main()
