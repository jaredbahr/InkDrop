#!/usr/bin/env python3
"""Run authenticated Manual Review loading regressions in a real browser."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import inkdrop_auth
import inkdrop_state


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-manual-review-browser-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state = root / "state"
        for path in (
            state,
            root / "config",
            root / "comics",
            root / "manga",
            root / "manual",
            root / "staging",
            root / "backups",
        ):
            path.mkdir(parents=True, exist_ok=True)
        env_updates = {
            "INKDROP_AUTH_MODE": "built_in",
            "INKDROP_AUTH_REQUIRED": "1",
            "INKDROP_CONFIG_DIR": str(root / "config"),
            "INKDROP_STATE_DIR": str(state),
            "INKDROP_COMIC_ROOT": str(root / "comics"),
            "INKDROP_MANGA_ROOT": str(root / "manga"),
            "INKDROP_MANUAL_INBOX_DIR": str(root / "manual"),
            "INKDROP_STAGING_DIR": str(root / "staging"),
            "INKDROP_BACKUP_DIR": str(root / "backups"),
            "INKDROP_WORKER_STATUS_FILE": str(state / "worker-scheduler-status.json"),
        }
        old_env = {key: os.environ.get(key) for key in env_updates}
        os.environ.update(env_updates)
        (state / "worker-scheduler-status.json").write_text(
            json.dumps({"ok": True, "state": "healthy", "failure_count": 0, "heartbeat_at": time.time()}),
            encoding="utf-8",
        )
        import inkdrop_web

        db = state / inkdrop_state.STATE_DB_NAME
        inkdrop_state.sync_settings(db, providers=[])
        inkdrop_auth.bootstrap_admin(db, "fixture-admin", "Fixture-browser-password-71!")
        old_db = inkdrop_web.INKDROP_STATE_DB
        inkdrop_web.INKDROP_STATE_DB = db
        inkdrop_web.INKDROP_AUTH_STATUS_CACHE.update({"ts": 0.0, "status": None, "key": None})
        server = inkdrop_web.InkDropThreadingHTTPServer(("127.0.0.1", 0), inkdrop_web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = dict(os.environ)
            env.update(
                {
                    "INKDROP_MANUAL_REVIEW_BROWSER_URL": f"http://127.0.0.1:{server.server_address[1]}/",
                    "INKDROP_MANUAL_REVIEW_BROWSER_USERNAME": "fixture-admin",
                    "INKDROP_MANUAL_REVIEW_BROWSER_PASSWORD": "Fixture-browser-password-71!",
                }
            )
            completed = subprocess.run(
                ["node", "web/tests/manual-review-load-browser-smoke.js"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
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
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
