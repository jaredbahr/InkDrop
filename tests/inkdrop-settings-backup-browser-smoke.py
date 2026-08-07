#!/usr/bin/env python3
"""Run the responsive settings backup/restore browser contract against InkDrop."""

import os
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

from core import inkdrop_state

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.exceptions = types.SimpleNamespace(RequestException=Exception, Timeout=Exception, ConnectionError=Exception, HTTPError=Exception)
    sys.modules["requests"] = requests_stub

from core import inkdrop_web


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-settings-browser-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db = root / "inkdrop.sqlite3"
        inkdrop_state.sync_settings(db, settings=[
            {"key": "ui.relative_dates", "scope": "ui", "label": "Relative dates", "value": True, "description": "test"},
            {"key": "automation.queue_watchdog_enabled", "scope": "automation", "label": "Watchdog", "value": True, "description": "test"},
        ])
        old_db = inkdrop_web.INKDROP_STATE_DB
        old_required = os.environ.get("INKDROP_AUTH_REQUIRED")
        old_mode = os.environ.get("INKDROP_AUTH_MODE")
        old_backup = os.environ.get("INKDROP_BACKUP_DIR")
        os.environ.update({"INKDROP_AUTH_REQUIRED": "0", "INKDROP_AUTH_MODE": "disabled", "INKDROP_BACKUP_DIR": str(root / "backups")})
        inkdrop_web.INKDROP_STATE_DB = db
        inkdrop_web.INKDROP_AUTH_STATUS_CACHE.update({"ts": 0.0, "status": None, "key": None})
        server = inkdrop_web.InkDropThreadingHTTPServer(("127.0.0.1", 0), inkdrop_web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = dict(os.environ)
            env["INKDROP_SETTINGS_BROWSER_URL"] = f"http://127.0.0.1:{server.server_address[1]}/"
            completed = subprocess.run(
                ["node", "web/tests/settings-backup-restore-browser-smoke.js"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                capture_output=True,
                timeout=90,
            )
            if completed.stdout:
                print(completed.stdout.strip())
            if completed.returncode:
                raise AssertionError(completed.stderr or f"browser exited {completed.returncode}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            inkdrop_web.INKDROP_STATE_DB = old_db
            for key, value in (("INKDROP_AUTH_REQUIRED", old_required), ("INKDROP_AUTH_MODE", old_mode), ("INKDROP_BACKUP_DIR", old_backup)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
