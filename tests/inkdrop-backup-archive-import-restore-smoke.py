#!/usr/bin/env python3
"""Real HTTP-level regression for the full-backup import/restore flow this
branch adds: /api/inkdrop-settings/backup/archives/upload,
.../restore/preview, and .../restore/apply.

Before this branch, InkDrop could create, list, download, and delete a full
backup archive from the UI, but there was no way to actually restore one --
and no way to bring in an archive from outside InkDrop's own backups
directory (a copy from another host, an older install). This proves
uploading an arbitrary archive validates it before keeping it (and rejects a
bad one without ever adding it to the list).

restore/apply used to be disabled outright (SIXH-20260812-RESTORE-P0-01),
because applying a restore in-process replaces the live state DB, auth DB and
config/secret files with no worker quiescence and no rollback. That hazard is
real, but a permanent 503 answered it by making a WORKING recovery path
unreachable: a restore of the real archive is measured at ~515 s, so for an
operator with only the UI the recovery time was not 515 seconds, it was never.

Apply is now GATED rather than disabled, and this proves both directions,
which is the whole point -- a gate that can only refuse is the 503 wearing a
different status code, and a gate that cannot be made to fire is not a gate:
  (a) refuses with 409 while the worker scheduler is heartbeating, naming it
  (b) refuses with 409 while a job lock is held, naming it
  (c) still 404s an unknown archive name
  (d) SUCCEEDS once nothing else is writing, and the restored database still
      answers a real query
A refused apply must never modify the live state database.
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core import inkdrop_auth
from core import inkdrop_state
from core import inkdrop_web

TESTING_MODE = {
    "INKDROP_AUTH_MODE": "disabled",
    "INKDROP_AUTH_ALLOW_DISABLED": "1",
    "INKDROP_TRUSTED_LAN_TESTING": "1",
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def request(port, path, *, method="GET", body=None, content_type=None):
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
        headers = {"Content-Type": content_type or "application/octet-stream"}
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        data = None
        headers = {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, response, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc, exc.read()


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-backup-import-restore-", ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "inkdrop-state.sqlite3"
        with inkdrop_state.connect(db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                "insert into app_settings(key, value_json, source, updated_at) values ('media_management.series_folder_format', '\"{Series Title}\"', 'user', 0)"
            )
            con.commit()

        original_db = inkdrop_web.INKDROP_STATE_DB
        original_env = {k: os.environ.get(k) for k in TESTING_MODE}
        os.environ.update(TESTING_MODE)
        os.environ["INKDROP_CONFIG_DIR"] = tmp
        os.environ["INKDROP_STATE_DIR"] = tmp
        os.environ["INKDROP_BACKUP_DIR"] = str(Path(tmp) / "backups")
        # Pin the lock directory too. It normally derives from
        # INKDROP_STATE_DIR, but an ambient INKDROP_LOCK_DIR overrides that --
        # and CI sets one. Without this the quiescence probe scans the runner's
        # lock directory instead of this test's, finds nothing, and reports the
        # install quiescent while the test is holding a lock somewhere else.
        # That is how this test passed locally and failed in CI.
        os.environ["INKDROP_LOCK_DIR"] = str(Path(tmp) / "locks")
        inkdrop_auth.clear_config_cache()
        inkdrop_web.clear_inkdrop_auth_status_cache()
        inkdrop_web.INKDROP_STATE_DB = db

        server = inkdrop_web.InkDropThreadingHTTPServer(("127.0.0.1", 0), inkdrop_web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            # --- Baseline: create a real archive through the existing
            # create endpoint so preview/apply have something real to work
            # against.
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/create", method="POST", body={})
            require(status == 200, f"create must succeed: {status} {body}")
            payload = json.loads(body)
            archive_name = payload["archives"][0]["name"]

            # --- Unknown archive name must 404 for preview, matching the
            # existing download/delete guard.
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/restore/preview", method="POST", body={"name": "does-not-exist.zip"})
            require(status == 404, f"unknown archive preview must 404: {status} {body}")

            # --- Preview is read-only: must not touch the live state DB.
            db_mtime_before_preview = db.stat().st_mtime_ns
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/restore/preview", method="POST", body={"name": archive_name})
            require(status == 200, f"preview of a real archive must succeed: {status} {body}")
            preview_payload = json.loads(body)
            result = preview_payload["result"]
            require(result["dry_run"] is True, result)
            require(result["would_restore"]["state_db"] is True, result)
            require(result["database_validation"]["state_db"]["quick_check"] == "ok", result)
            require(db.stat().st_mtime_ns == db_mtime_before_preview, "preview must not modify the live state database")

            # --- Apply is no longer disabled outright. It is GATED on
            # quiescence (SIXH-20260812-RESTORE-P0-01 turned from a locked
            # door into a precondition), so both directions are asserted here:
            # it must refuse while something else is writing, and it must
            # actually work once nothing is. A gate that only ever refuses is
            # indistinguishable from the 503 it replaced; a gate that cannot be
            # made to fire is not a gate at all.
            worker_status = Path(tmp) / "worker-scheduler-status.json"
            lock_dir = Path(tmp) / "locks"
            lock_dir.mkdir(parents=True, exist_ok=True)

            # (a) REFUSES: a worker scheduler that heartbeated just now.
            db_content_before_apply = db.read_bytes()
            worker_status.write_text(
                json.dumps({"heartbeat_at": time.time(), "active_jobs": []}), encoding="utf-8"
            )
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/restore/apply", method="POST", body={"name": archive_name})
            require(status == 409, f"apply must refuse while the worker is live: {status} {body}")
            refusal = json.loads(body)
            require(refusal["error"] == "restore_blocked_by_live_writers", body)
            require(
                any(blocker["kind"] == "worker_scheduler_live" for blocker in refusal["blockers"]),
                f"the refusal must NAME the live worker, not just refuse: {body}",
            )
            require(db.read_bytes() == db_content_before_apply, "a refused apply must never modify the live state database")

            # (b) REFUSES: no worker at all, but a job lock held by someone.
            # Proves the lock probe fires on its own, not only via heartbeat.
            worker_status.unlink()
            held_lock = lock_dir / "inkdrop-comics-import.lock"
            held_handle = open(held_lock, "a+b")
            held_handle.write(b"0")
            held_handle.flush()
            held_handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(held_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(held_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/restore/apply", method="POST", body={"name": archive_name})
                require(status == 409, f"apply must refuse while a job lock is held: {status} {body}")
                refusal = json.loads(body)
                require(
                    any(blocker["kind"] == "job_lock_held" for blocker in refusal["blockers"]),
                    f"the refusal must NAME the held lock: {body}",
                )
                # The probe must have looked in the directory this test is
                # actually using. A probe scanning somewhere else reports
                # "quiescent" having examined nothing, which reads exactly like
                # a genuine pass -- that is the CI failure this line exists for.
                require(
                    "inkdrop-comics-import.lock" in " ".join(b.get("detail", "") for b in refusal["blockers"]),
                    f"the probe must have scanned THIS test's lock directory: {body}",
                )
                require(db.read_bytes() == db_content_before_apply, "a refused apply must never modify the live state database")
            finally:
                held_handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(held_handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(held_handle.fileno(), fcntl.LOCK_UN)
                held_handle.close()

            # (c) Unknown archive is still a 404, and is answered BEFORE the
            # quiescence gate -- a wrong name is a wrong name whether or not
            # the worker happens to be running.
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/restore/apply", method="POST", body={"name": "does-not-exist.zip"})
            require(status == 404, f"apply for an unknown archive must 404: {status} {body}")

            # (d) PROCEEDS: worker stopped, no lock held. This is the half that
            # makes the capability reachable, and the half a permanently
            # disabled endpoint could never have.
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/restore/apply", method="POST", body={"name": archive_name})
            require(status == 200, f"apply must succeed once nothing else is writing: {status} {body}")
            applied = json.loads(body)["result"]
            require(applied["dry_run"] is False, applied)
            require(applied.get("restored_state_db"), f"a successful apply must report the restored state db: {applied}")
            require(applied.get("pre_restore_snapshots"), f"a successful apply must snapshot what it overwrote: {applied}")
            # Usable, not merely replaced: the restored database still answers.
            with inkdrop_state.connect_read(db) as con:
                restored_setting = con.execute(
                    "select value_json from app_settings where key = 'media_management.series_folder_format'"
                ).fetchone()
            require(restored_setting is not None, "the restored state database must still answer a real query")

            # --- Uploading a real archive (bytes copied from the one this
            # test already created) must validate and add it to the list
            # under InkDrop's own naming convention, without needing it to
            # have started out in the backups directory.
            backups_dir = Path(tmp) / "backups"
            source_archive_bytes = (backups_dir / archive_name).read_bytes()
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/upload", method="POST", body=source_archive_bytes, content_type="application/zip")
            require(status == 200, f"uploading a valid archive must succeed: {status} {body[:300]}")
            upload_payload = json.loads(body)
            uploaded_name = upload_payload["archive"]["name"]
            require(uploaded_name.startswith("inkdrop-backup-") and uploaded_name.endswith("-imported.zip"), uploaded_name)
            require(upload_payload["preview"]["would_restore"]["state_db"] is True, upload_payload)
            require(any(item["name"] == uploaded_name for item in upload_payload["archives"]), upload_payload["archives"])

            # --- The uploaded archive is a first-class archive: it can be
            # previewed exactly like a manually created one (apply is
            # disabled for all archives regardless of origin, tested above).
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/restore/preview", method="POST", body={"name": uploaded_name})
            require(status == 200, f"preview of an uploaded archive must succeed: {status} {body}")

            # --- Uploading garbage must be rejected and must never be kept
            # on disk or listed -- an invalid upload is not silently added.
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives")
            names_before_bad_upload = {item["name"] for item in json.loads(body)["archives"]}
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/upload", method="POST", body=b"this is not a zip file", content_type="application/zip")
            require(status == 400, f"uploading garbage must be rejected: {status} {body}")
            require(json.loads(body)["error"] == "invalid_backup_archive", body)
            on_disk_after_bad_upload = {p.name for p in backups_dir.iterdir()}
            require(
                not any(name.endswith("-imported.zip") and name not in {uploaded_name} for name in on_disk_after_bad_upload),
                f"a rejected upload must not leave a file behind: {on_disk_after_bad_upload}",
            )
            require(not any(name.endswith(".zip.tmp") for name in on_disk_after_bad_upload), f"temp upload file must be cleaned up: {on_disk_after_bad_upload}")
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives")
            names_after_bad_upload = {item["name"] for item in json.loads(body)["archives"]}
            require(names_after_bad_upload == names_before_bad_upload, "a rejected upload must not appear in the archive list")

            # --- Uploading a well-formed zip that is not an InkDrop backup
            # (no manifest / state DB member) must also be rejected.
            fake_zip_path = Path(tmp) / "not-a-backup.zip"
            with zipfile.ZipFile(fake_zip_path, "w") as zf:
                zf.writestr("hello.txt", "not a backup archive")
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/upload", method="POST", body=fake_zip_path.read_bytes(), content_type="application/zip")
            require(status == 400, f"a well-formed but foreign zip must still be rejected: {status} {body}")
            require(json.loads(body)["error"] == "invalid_backup_archive", body)

            # --- An empty upload must be rejected up front.
            status, _resp, body = request(port, "/api/inkdrop-settings/backup/archives/upload", method="POST", body=b"", content_type="application/zip")
            require(status == 400, f"an empty upload must be rejected: {status} {body}")
            require(json.loads(body)["error"] == "empty_upload", body)
        finally:
            server.shutdown()
            inkdrop_web.INKDROP_STATE_DB = original_db
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            os.environ.pop("INKDROP_CONFIG_DIR", None)
            os.environ.pop("INKDROP_STATE_DIR", None)
            os.environ.pop("INKDROP_BACKUP_DIR", None)
            inkdrop_auth.clear_config_cache()
            inkdrop_web.clear_inkdrop_auth_status_cache()

    print("inkdrop-backup-archive-import-restore-smoke: PASS")


if __name__ == "__main__":
    main()
