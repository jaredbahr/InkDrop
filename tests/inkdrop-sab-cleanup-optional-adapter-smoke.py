#!/usr/bin/env python3
"""Ensure a blank optional SABnzbd adapter is a clean scheduler no-op."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="inkdrop-sab-cleanup-optional-") as temp_dir:
        root = Path(temp_dir)
        env = os.environ.copy()
        for key in (
            "SAB_RESCUE_API_KEY",
            "INKDROP_SABNZBD_API_KEY",
            "SABNZBD_API_KEY",
        ):
            env.pop(key, None)
        env.update(
            {
                "INKDROP_CONFIG_DIR": str(root / "config"),
                "INKDROP_STATE_DIR": str(root / "state"),
                "INKDROP_LOG_DIR": str(root / "state" / "logs"),
                "INKDROP_CACHE_DIR": str(root / "state" / "cache"),
                "INKDROP_BACKUP_DIR": str(root / "state" / "backups"),
            }
        )
        proc = subprocess.run(
            [sys.executable, "-B", str(ROOT / "core" / "inkdrop_sab_failed_cleanup.py"), "--json"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
        payload = json.loads(proc.stdout)
        assert payload.get("ok") is True, payload
        assert payload.get("skipped") is True, payload
        assert payload.get("skip_reason") == "adapter_not_configured", payload
        assert payload.get("failed_count") == 0, payload
        assert "Traceback" not in proc.stderr, proc.stderr

    print("INKDROP_SAB_CLEANUP_OPTIONAL_ADAPTER_OK")


if __name__ == "__main__":
    main()
