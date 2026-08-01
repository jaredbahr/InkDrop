#!/usr/bin/env python3
"""Smoke-test the clean local-folder-only InkDrop install path.

This proves a public install can start with local/manual folders only: no
Kavita, Komga, Kapowarr, Prowlarr, Suwayomi, SLSKD, qBittorrent, or SABnzbd
configured. Optional automation is limited, but the install should be usable
and explain what remains unconfigured.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import inkdrop_preflight
import inkdrop_runtime_config
from tools import inkdrop_install_support_summary


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def clean_local_folder_env(root: Path):
    return {
        inkdrop_runtime_config.ENV_CONFIG_DIR: str(root / "config"),
        inkdrop_runtime_config.ENV_STATE_DIR: str(root / "state"),
        inkdrop_runtime_config.ENV_LOG_DIR: str(root / "state" / "logs"),
        inkdrop_runtime_config.ENV_CACHE_DIR: str(root / "state" / "cache"),
        inkdrop_runtime_config.ENV_BACKUP_DIR: str(root / "state" / "backups"),
        inkdrop_runtime_config.ENV_STAGING_DIR: str(root / "staging"),
        inkdrop_runtime_config.ENV_MANUAL_INBOX_DIR: str(root / "manual-inbox"),
        inkdrop_runtime_config.ENV_QUARANTINE_DIR: str(root / "state" / "quarantine"),
        "INKDROP_COMIC_ROOT": str(root / "library" / "comics"),
        "INKDROP_MANGA_ROOT": str(root / "library" / "manga"),
        "INKDROP_MANUAL_COMICS_INBOX": str(root / "manual-inbox" / "comics"),
        "INKDROP_MANUAL_EBOOKS_INBOX": str(root / "manual-inbox" / "ebooks"),
    }


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-local-folder-only-") as tmp:
        root = Path(tmp)
        env = clean_local_folder_env(root)

        missing = inkdrop_preflight.run_preflight(env, create=False)
        require(missing["ok"] is False, "missing required roots should fail before create")
        require(missing["roots"]["config_dir"]["required"] is True, "config dir is required")
        require(missing["roots"]["state_dir"]["required"] is True, "state dir is required")
        require(missing["roots"]["manual_inbox_dir"]["required"] is False, "manual inbox is optional but supported")

        created = inkdrop_preflight.run_preflight(env, create=True)
        require(created["ok"] is True, "local-folder-only preflight should pass after creating runtime roots")
        require(created["created_missing_dirs"] is True, "preflight should report directory creation")
        require(created["state_db_path"].endswith("/state/inkdrop-state.sqlite3"), "state DB should live under local state root")
        require(created["path_mappings"]["INKDROP_SAB_PATH_MAPPINGS"]["configured"] is False, "SAB path mappings are not needed")
        require(created["path_mappings"]["INKDROP_UNC_PATH_MAPPINGS"]["configured"] is False, "UNC mappings are not needed")

        configured = created["configured_adapters"]
        for adapter in (
            "comicvine",
            "kapowarr",
            "kavita",
            "komga",
            "prowlarr",
            "qbittorrent",
            "sabnzbd",
            "slskd",
            "suwayomi",
        ):
            require(configured[adapter]["configured"] is False, f"{adapter} should be unconfigured")
            require(configured[adapter]["configured_by"] == "", f"{adapter} should not report a config source")

        warnings = created["warning_summary"]["optional_adapters_unconfigured"]
        require(warnings == ["comicvine", "prowlarr", "qbittorrent", "sabnzbd", "slskd", "suwayomi"], "optional source/download warnings should be stable")
        require("kapowarr" not in warnings, "Kapowarr migration adapter should not warn in clean local mode")
        require("kavita" not in warnings, "Kavita visibility adapter should not warn in clean local mode")
        require("komga" not in warnings, "Komga visibility adapter should not warn in clean local mode")

        defaults = inkdrop_install_support_summary._install_defaults()
        require(defaults["paths"]["comic_root"] == "/library/comics", "install defaults should prefill comic root")
        require(defaults["paths"]["manga_root"] == "/library/manga", "install defaults should prefill manga root")
        require(defaults["paths"]["manual_comics_inbox"] == "/manual-inbox/comics", "install defaults should prefill manual comics inbox")
        require(defaults["optional_adapter_defaults"]["slskd_api_base_url"] == "", "SLSKD endpoint default must stay blank")
        require(defaults["optional_adapter_defaults"]["qbittorrent_url"] == "", "qBittorrent endpoint default must stay blank")
        require(defaults["optional_adapter_defaults"]["sabnzbd_url"] == "", "SAB endpoint default must stay blank")
        require("must not enable adapters" in defaults["secret_policy"], "install defaults should keep suggestions inactive")

    print("INKDROP_LOCAL_FOLDER_ONLY_FLOW_OK: clean local/manual install works without external adapters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
