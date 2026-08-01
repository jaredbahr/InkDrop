#!/usr/bin/env python3
"""Regression smoke for the import-ready worker drain contract."""

import ast
import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import zipfile
from pathlib import Path
from PIL import Image

import inkdrop_state


def valid_large_png():
    output = io.BytesIO()
    Image.frombytes("RGB", (512, 512), os.urandom(512 * 512 * 3)).save(output, format="PNG")
    return output.getvalue()


ROOT = Path(__file__).resolve().parent
WORKER = ROOT / "inkdrop-import-ready-worker.sh"
RECONCILE = ROOT / "inkdrop_reconcile_imports.py"
COMPLETED_IMPORT = ROOT / "inkdrop_completed_import.py"
STATE = ROOT / "inkdrop_state.py"


def fail(message):
    raise AssertionError(message)


def smoke_import_result_state_uses_library_neutral_statuses():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    visible = {
        "imported": [{"source": "/tmp/source.cbz", "dest": "/tmp/dest.cbz"}],
        "verification": {
            "checked": [{"verification_status": "library_visible"}],
            "library_visible_count": 1,
        },
    }
    if inkdrop_reconcile_imports.import_result_state(visible) != ("verified", "library_visible", True):
        fail("import_result_state should emit library_visible for generic library proof")
    folder_visible = {
        "imported": [{"source": "/tmp/source.cbz", "dest": "/tmp/dest.cbz"}],
        "verification": {
            "checked": [{"verification_status": "folder_verified"}],
            "folder_verified_count": 1,
        },
    }
    if inkdrop_reconcile_imports.import_result_state(folder_visible) != ("verified", "folder_verified", True):
        fail("import_result_state should preserve folder_verified proof")
    waiting = {
        "imported": [{"source": "/tmp/source.cbz", "dest": "/tmp/dest.cbz"}],
        "verification": {
            "checked": [{"verification_status": "waiting_for_library_scan"}],
            "waiting_for_library_scan_count": 1,
            "pending_scan_count": 1,
        },
    }
    if inkdrop_reconcile_imports.import_result_state(waiting) != (
        "waiting_for_library_scan",
        "importer_copied_waiting_for_library_scan",
        False,
    ):
        fail("import_result_state should emit waiting_for_library_scan for scan-pending imports")


def smoke_direct_import_short_timeout_writer():
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db_path = root / "inkdrop-state.sqlite3"
        comic_root = root / "Comics"
        dest_path = comic_root / "Smoke Series" / "Smoke Series #001.cbz"
        verified_dest_path = comic_root / "Smoke Series" / "Smoke Series #002.cbz"
        matching_dest_path = comic_root / "Smoke Series" / "Smoke Series #004.cbz"
        dest_path.parent.mkdir(parents=True)
        for archive_path in (dest_path, verified_dest_path, matching_dest_path):
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("001.jpg", b"validated smoke page")
        now = time.time()
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into app_settings(key, scope, label, value_json, description, source, updated_at)
                values(?,?,?,?,?,?,?)
                """,
                (
                    "media_management.comic_root",
                    "media_management",
                    "Comic Root",
                    json.dumps(str(comic_root)),
                    "Smoke managed comic root",
                    "smoke",
                    now,
                ),
            )
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-smoke", "Smoke Series", "comic", "comicvine", "1", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-smoke", "series-smoke", "1", "1", "Issue 1", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-smoke-verified", "series-smoke", "2", "2", "Issue 2", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-smoke-already", "series-smoke", "3", "3", "Issue 3", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-smoke-matching-dest", "series-smoke", "4", "4", "Issue 4", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-smoke", "series-smoke", "issue-smoke", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("queue-smoke", "wanted-smoke", "series-smoke", "issue-smoke", "ready_to_import", "qbit", "Smoke Series 1", "ready", 1, now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-smoke-verified", "series-smoke", "issue-smoke-verified", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, outcome, display_phase, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-smoke-verified",
                    "wanted-smoke-verified",
                    "series-smoke",
                    "issue-smoke-verified",
                    "importing",
                    "download_client",
                    "Smoke Series 1 verified",
                    "waiting for library verification",
                    1,
                    now,
                    now,
                    "productive",
                    "observed",
                    "{}",
                ),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-smoke-matching-dest", "series-smoke", "issue-smoke-matching-dest", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, outcome, display_phase, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-smoke-matching-dest",
                    "wanted-smoke-matching-dest",
                    "series-smoke",
                    "issue-smoke-matching-dest",
                    "importing",
                    "download_client",
                    "Smoke Series 4 already imported destination",
                    "waiting for import replay",
                    1,
                    now,
                    now,
                    "productive",
                    "verifying",
                    "{}",
                ),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-smoke-already", "series-smoke", "issue-smoke-already", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, outcome, display_phase, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-smoke-already",
                    "wanted-smoke-already",
                    "series-smoke",
                    "issue-smoke-already",
                    "importing",
                    "download_client",
                    "Smoke Series 3 already",
                    "waiting for import replay",
                    1,
                    now,
                    now,
                    "productive",
                    "verifying",
                    "{}",
                ),
            )
            con.execute(
                """
                insert into source_attempts(
                    id,queue_id,wanted_id,series_id,issue_id,source,provider,protocol,download_client,
                    candidate_identity,lifecycle_phase,outcome,display_phase,retry_eligible,status,title,
                    started_at,completed_at,raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "attempt-smoke", "queue-smoke", "wanted-smoke", "series-smoke", "issue-smoke",
                    "rss_getcomics", "Pixeldrain", "http", "inkdrop_direct",
                    "candidate-getcomics-pixeldrain-smoke", "completed", "success", "completed", 0,
                    "completed", "Smoke Series 001.cbz", now, now, "{}",
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source_attempt_id, source, provider, protocol,
                    download_client, external_id, candidate_identity, title, status, state, lifecycle_phase,
                    retry_eligible, local_path, started_at, updated_at, completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-smoke",
                    "queue-smoke",
                    "wanted-smoke",
                    "series-smoke",
                    "issue-smoke",
                    "attempt-smoke",
                    "rss_getcomics",
                    "Pixeldrain",
                    "http",
                    "inkdrop_direct",
                    "getcomics-pixeldrain-smoke",
                    "candidate-getcomics-pixeldrain-smoke",
                    "Smoke Series 001.cbz",
                    "completed_in_client",
                    "import_ready",
                    "import_ready",
                    0,
                    str(dest_path),
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()
        direct_claim = inkdrop_state.claim_import_authority(
            db_path,
            "queue-smoke",
            "task-smoke",
            source_attempt_id="attempt-smoke",
            external_id="getcomics-pixeldrain-smoke",
            candidate_identity="candidate-getcomics-pixeldrain-smoke",
            download_client="inkdrop_direct",
            local_path=str(dest_path),
        )
        if not direct_claim.get("ok"):
            fail(f"direct import authority claim failed: {direct_claim}")
        result = inkdrop_state.record_direct_import_result(
            db_path,
            "queue-smoke",
            source_path=str(dest_path),
            dest_path=str(dest_path),
            source="rss_getcomics",
            status="waiting_for_kavita_scan",
            verified=False,
            imported_count=1,
            raw={
                "smoke": True,
                "download_task_id": "task-smoke",
                "raw": {
                    "media_management_destination_decision": {
                        "enabled": True,
                        "applied": True,
                        "reason": "planned_path_selected",
                    }
                },
            },
            import_authority=direct_claim.get("authority"),
            read_timeout_seconds=0.5,
            read_busy_timeout_ms=500,
            lock_timeout_seconds=0.5,
            lock_busy_timeout_ms=500,
        )
        if not result.get("ok"):
            fail(f"short-timeout direct import state write failed: {result}")
        if result.get("download_tasks_updated") != 1:
            fail(f"direct import did not update the matching download task: {result}")
        verified_result = inkdrop_state.record_direct_import_result(
            db_path,
            "queue-smoke-verified",
            source_path=str(verified_dest_path),
            dest_path=str(verified_dest_path),
            source="download_client",
            status="kavita_verified",
            verified=True,
            imported_count=1,
            raw={"smoke": True},
            read_timeout_seconds=0.5,
            read_busy_timeout_ms=500,
            lock_timeout_seconds=0.5,
            lock_busy_timeout_ms=500,
        )
        if not verified_result.get("ok"):
            fail(f"verified direct import state write failed: {verified_result}")
        already_result = inkdrop_state.record_direct_import_result(
            db_path,
            "queue-smoke-already",
            source_path=str(root / "Downloads" / "Smoke Series #003.cbz"),
            dest_path=str(root / "Downloads" / "Smoke Series #003.cbz"),
            source="download_client",
            status="imported",
            verified=False,
            imported_count=1,
            raw={
                "smoke": True,
                "raw": {
                    "reconciliation_state": "suppressed_completed",
                    "reason": "already_verified_series_number",
                    "previous_status": "library_visible",
                    "previous_verified": True,
                },
            },
            read_timeout_seconds=0.5,
            read_busy_timeout_ms=500,
            lock_timeout_seconds=0.5,
            lock_busy_timeout_ms=500,
        )
        if not already_result.get("ok"):
            fail(f"already-satisfied direct import state write failed: {already_result}")
        if already_result.get("status") == "missing_file":
            fail(f"already-satisfied direct import was reopened as missing: {already_result}")
        matching_dest_result = inkdrop_state.record_direct_import_result(
            db_path,
            "queue-smoke-matching-dest",
            source_path=str(root / "Downloads" / "Unrelated v99.cbz"),
            dest_path=str(matching_dest_path),
            source="download_client",
            status="imported",
            verified=False,
            imported_count=1,
            raw={
                "smoke": True,
                "raw": {
                    "reconciliation_state": "suppressed_completed",
                    "reason": "already_imported_matching_destination",
                    "imported_file_source_path": str(root / "Downloads" / "Unrelated v99.cbz"),
                    "imported_file_dest_path": str(matching_dest_path),
                },
            },
            read_timeout_seconds=0.5,
            read_busy_timeout_ms=500,
            lock_timeout_seconds=0.5,
            lock_busy_timeout_ms=500,
        )
        if not matching_dest_result.get("ok"):
            fail(f"matching-destination direct import state write failed: {matching_dest_result}")
        if matching_dest_result.get("status") == "wrong_unit_quarantined":
            fail(f"already-imported matching destination was downgraded by unit gate: {matching_dest_result}")
        with sqlite3.connect(db_path) as con:
            row = con.execute("select state, active, current_source, outcome, display_phase from queue_items where id='queue-smoke'").fetchone()
            if row != ("verified", 0, "rss_getcomics", "productive", "verified"):
                fail(f"optional folder-complete queue row was not satisfied: {row}")
            wanted_row = con.execute("select status from wanted_items where id='wanted-smoke'").fetchone()
            if wanted_row != ("satisfied",):
                fail(f"optional folder-complete wanted row was not satisfied: {wanted_row}")
            verified_row = con.execute(
                "select state, active, current_source, outcome, display_phase from queue_items where id='queue-smoke-verified'"
            ).fetchone()
            if verified_row != ("verified", 0, "download_client", "productive", "verified"):
                fail(f"verified queue row kept stale operator phase: {verified_row}")
            already_row = con.execute(
                "select state, active, current_source, last_event, outcome, display_phase from queue_items where id='queue-smoke-already'"
            ).fetchone()
            if already_row[:3] != ("searching", 1, "download_client"):
                fail(f"unmanaged already-satisfied import did not fail closed: {already_row}")
            if "could not confirm the file landed" not in str(already_row[3] or "").lower():
                fail(f"unmanaged already-satisfied import lacks safety explanation: {already_row}")
            already_import = con.execute(
                "select status, verified, library_visibility_status from import_results where queue_id='queue-smoke-already'"
            ).fetchone()
            if already_import[:2] != ("verification_pending", 0):
                fail(f"unmanaged already-satisfied import recorded completion: {already_import}")
            matching_dest_import = con.execute(
                "select status, verified from import_results where queue_id='queue-smoke-matching-dest'"
            ).fetchone()
            if matching_dest_import != ("verification_pending", 0):
                fail(f"mismatched source relabeled an already-imported destination: {matching_dest_import}")
            matching_dest_queue = con.execute(
                "select state, active, current_source from queue_items where id='queue-smoke-matching-dest'"
            ).fetchone()
            if matching_dest_queue != ("searching", 1, "download_client"):
                fail(f"mismatched source satisfied an already-imported destination: {matching_dest_queue}")
            task = con.execute("select state, status, lifecycle_phase from download_tasks where id='task-smoke'").fetchone()
            if task != ("verified", "queue_verified", "verified"):
                fail(f"download task was not retired after optional folder completion: {task}")
            count = con.execute("select count(*) from import_results where queue_id='queue-smoke'").fetchone()[0]
            columns = {row[1] for row in con.execute("pragma table_info(import_results)").fetchall()}
            expected_columns = {
                "completion_truth",
                "folder_imported",
                "library_visibility_required",
                "library_visibility_status",
                "library_visibility_provider",
            }
            missing_columns = sorted(expected_columns - columns)
            if missing_columns:
                fail("import_results is missing folder completion columns: " + ", ".join(missing_columns))
            import_row = con.execute(
                """
                select completion_truth, folder_imported, library_visibility_required,
                       library_visibility_status, library_visibility_provider, raw_json
                from import_results
                where queue_id='queue-smoke'
                """
            ).fetchone()
            task_raw_json = con.execute("select raw_json from download_tasks where id='task-smoke'").fetchone()[0]
        if count != 1:
            fail(f"expected one import_result row, found {count}")
        if import_row[:5] != ("folder", 1, 0, "optional", "library"):
            fail(f"direct import did not record folder completion evidence: {import_row[:5]}")
        import_payload = json.loads(import_row[5] or "{}")
        for key, expected in {
            "completion_truth": "folder",
            "folder_imported": True,
            "library_visibility_required": False,
            "library_visibility_status": "optional",
            "library_visibility_provider": "library",
        }.items():
            if import_payload.get(key) != expected:
                fail(f"direct import payload {key}={import_payload.get(key)!r}, expected {expected!r}")
        decision = import_payload.get("media_management_destination_decision") or {}
        if decision.get("applied") is not True or decision.get("reason") != "planned_path_selected":
            fail(f"direct import writer did not lift nested media-management decision: {import_payload}")
        task_raw = json.loads(task_raw_json or "{}")
        if task_raw.get("direct_import_completion_truth") != "folder" or task_raw.get("direct_import_folder_imported") is not True:
            fail(f"download task did not inherit folder completion evidence: {task_raw}")
        task_decision = ((task_raw.get("direct_import_media_management") or {}).get("media_management_destination_decision") or {})
        if task_decision.get("applied") is not True:
            fail(f"download task did not inherit media-management decision evidence: {task_raw}")


def smoke_reconcile_lock_waits_then_reports_busy():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import fcntl
    except ModuleNotFoundError:
        return
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-lock-smoke-", ignore_cleanup_errors=True) as tmp:
        lock_path = Path(tmp) / "reconcile.lock"
        old_lock = inkdrop_reconcile_imports.RECONCILE_LOCK_PATH
        holder = lock_path.open("w", encoding="utf-8")
        try:
            inkdrop_reconcile_imports.RECONCILE_LOCK_PATH = lock_path
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            try:
                inkdrop_reconcile_imports.acquire_reconcile_lock(wait_seconds=0.15)
            except BlockingIOError:
                elapsed = time.monotonic() - started
                if elapsed < 0.1:
                    fail(f"reconcile lock wait returned too quickly: {elapsed}")
            else:
                fail("reconcile lock wait unexpectedly acquired an owned lock")
            fcntl.flock(holder, fcntl.LOCK_UN)
            lock_handle, lock_module = inkdrop_reconcile_imports.acquire_reconcile_lock(wait_seconds=0.15)
            try:
                lock_module.flock(lock_handle, lock_module.LOCK_UN)
            finally:
                lock_handle.close()
        finally:
            inkdrop_reconcile_imports.RECONCILE_LOCK_PATH = old_lock
            holder.close()


def smoke_reconciliation_replay_to_inkdrop():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-replay-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        managed_root = root / "library"
        source_path = str(root / "downloads" / "Replay Series #002.cbz")
        dest_path = str(managed_root / "Replay Series #002.cbz")
        Path(source_path).parent.mkdir(parents=True)
        Path(dest_path).parent.mkdir(parents=True)
        for archive_path in (Path(source_path), Path(dest_path)):
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("001.jpg", b"validated replay page")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-replay", "Replay Series", "comic", "comicvine", "2", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-replay", "series-replay", "2", "2", "Issue 2", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-replay", "series-replay", "issue-replay", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("queue-replay", "wanted-replay", "series-replay", "issue-replay", "importing", "download_client", "Replay Series 2", "ready_to_import", 1, now, now, "{}"),
            )
            con.execute(
                """
                insert into app_settings(key, scope, label, value_json, description, source, updated_at)
                values(?,?,?,?,?,?,?)
                """,
                (
                    "media_management.comic_root",
                    "media_management",
                    "Comic Root",
                    json.dumps(str(managed_root)),
                    "Smoke managed comics root",
                    "smoke",
                    now,
                ),
            )
            con.commit()
        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.execute(
                    "create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)"
                )
                con.execute(
                    "insert into imported_files values(?,?,?,?,?)",
                    ("sha-replay", source_path, dest_path, 4, now),
                )
                con.execute(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, lifecycle_state,
                        reason, matched_local_path, matched_series, trusted_series_id,
                        trusted_issue, inkdrop_queue_id, imported_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "replay-key",
                        "Replay Series",
                        "Replay Series 2",
                        "torrent",
                        "qbit",
                        "client-id",
                        "waiting_for_kavita_scan",
                        "importer_copied_waiting_for_kavita_scan",
                        source_path,
                        "Replay Series",
                        "comicvine:2",
                        "2",
                        "queue-replay",
                        now,
                        now,
                    ),
                )
                con.commit()
            result = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports(limit=10)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
        if result.get("updated") != 0 or result.get("skipped", {}).get("missing_download_task_id") != 1:
            fail(f"taskless reconciliation replay did not fail closed: {result}")
        with sqlite3.connect(state_db) as con:
            queue = con.execute("select state, active, current_source, last_event, display_phase from queue_items where id='queue-replay'").fetchone()
            if queue != ("importing", 1, "download_client", "ready_to_import", None):
                fail(f"taskless reconciliation replay mutated queue state: {queue}")
            import_row = con.execute("select raw_json from import_results where queue_id='queue-replay' and dest_path=?", (dest_path,)).fetchone()
            wanted = con.execute("select status from wanted_items where id='wanted-replay'").fetchone()[0]
        if import_row is not None or wanted != "in_progress":
            fail(f"taskless reconciliation replay created completion truth: import={import_row} wanted={wanted}")


def smoke_reconciliation_replay_skips_missing_import_destination():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-empty-dest-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        source_path = str(root / "source.cbz")
        Path(source_path).write_bytes(b"smoke")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-empty-dest", "Empty Dest Series", "comic", "comicvine", "3", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-empty-dest", "series-empty-dest", "3", "3", "Issue 3", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-empty-dest", "series-empty-dest", "issue-empty-dest", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-empty-dest",
                    "wanted-empty-dest",
                    "series-empty-dest",
                    "issue-empty-dest",
                    "importing",
                    "download_client",
                    "Empty Dest Series 3",
                    "ready_to_import",
                    1,
                    now,
                    now,
                    "{}",
                ),
            )
        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.execute(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, lifecycle_state,
                        reason, matched_local_path, matched_series, trusted_series_id,
                        trusted_issue, inkdrop_queue_id, imported_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "empty-dest-key",
                        "Empty Dest Series",
                        "Empty Dest Series 3",
                        "torrent",
                        "qbit",
                        "client-id-empty",
                        "waiting_for_kavita_scan",
                        "inkdrop_queue_no_longer_import_ready",
                        source_path,
                        "Empty Dest Series",
                        "comicvine:3",
                        "3",
                        "queue-empty-dest",
                        now,
                        now,
                    ),
                )
                con.commit()
            result = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports(limit=10)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
        if result.get("updated") != 0 or result.get("skipped", {}).get("missing_imported_destination") != 1:
            fail(f"empty destination replay was not skipped cleanly: {result}")
        with sqlite3.connect(state_db) as con:
            count = con.execute("select count(*) from import_results where queue_id='queue-empty-dest'").fetchone()[0]
            queue = con.execute("select state, current_source, last_event from queue_items where id='queue-empty-dest'").fetchone()
        if count:
            fail(f"empty destination replay created a ghost import_result: {count}")
        if queue != ("importing", "download_client", "ready_to_import"):
            fail(f"empty destination replay mutated queue state: {queue}")


def smoke_reconciliation_replay_settles_suppressed_existing_path():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-suppressed-existing-replay-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        source_path = root / "Downloads" / "suwayomi" / "Gantz E - Chapter 64.cbz"
        stale_existing_path = root / "Manga" / "Gantz- E" / "Volume 08" / "Gantz E - Chapter 64.cbz"
        existing_path = root / "Manga" / "Gantz - E (2020)" / "Gantz E - Chapter 64.cbz"
        source_path.parent.mkdir(parents=True)
        existing_path.parent.mkdir(parents=True)
        for archive_path in (source_path, existing_path):
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("001.jpg", b"validated existing page")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into app_settings(key, scope, label, value_json, description, source, updated_at)
                values(?,?,?,?,?,?,?)
                """,
                (
                    "media_management.manga_root",
                    "media_management",
                    "Manga Root",
                    json.dumps(str(root / "Manga")),
                    "Smoke managed manga root",
                    "smoke",
                    now,
                ),
            )
            con.execute(
                """
                insert into series(
                    id, title, media_type, year, metadata_provider, metadata_id, source,
                    library_root, library_path, library_adapter_path,
                    created_at, updated_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "series-existing-replay", "Gantz: E", "manga", 2020,
                    "mangadex", "gantz-e", "inkdrop_series",
                    str(root / "Manga"), str(root / "Manga" / "Gantz- E"),
                    "/manga/Gantz- E/Volume 08", now, now, "{}",
                ),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-existing-replay", "series-existing-replay", "64", "64", "The Relentless Abyss", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-existing-replay", "series-existing-replay", "issue-existing-replay", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(
                    id, wanted_id, series_id, issue_id, state, current_source, query,
                    last_event, active, created_at, updated_at, display_phase, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-existing-replay",
                    "wanted-existing-replay",
                    "series-existing-replay",
                    "issue-existing-replay",
                    "importing",
                    "suwayomi",
                    "Gantz: E Chapter 64",
                    "page pack staged file ready",
                    1,
                    now,
                    now,
                    "staged_or_importing",
                    "{}",
                ),
            )
            con.execute(
                """
                insert into source_attempts(
                    id,queue_id,wanted_id,series_id,issue_id,source,provider_id,provider,protocol,download_client,
                    candidate_identity,lifecycle_phase,outcome,display_phase,retry_eligible,status,title,
                    started_at,completed_at,raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "attempt-existing-replay", "queue-existing-replay", "wanted-existing-replay",
                    "series-existing-replay", "issue-existing-replay", "suwayomi", "suwayomi", "suwayomi",
                    "local", "inkdrop_page_pack", "candidate-existing-replay", "completed", "success",
                    "completed", 0, "completed", "Gantz E - Chapter 64.cbz", now, now, "{}",
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source_attempt_id, source, provider_id, provider,
                    protocol, download_client, external_id, candidate_identity, title, status, state, lifecycle_phase,
                    retry_eligible, local_path, started_at, updated_at, completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-existing-replay",
                    "queue-existing-replay",
                    "wanted-existing-replay",
                    "series-existing-replay",
                    "issue-existing-replay",
                    "attempt-existing-replay",
                    "suwayomi",
                    "suwayomi",
                    "suwayomi",
                    "local",
                    "inkdrop_page_pack",
                    "suwayomi-existing-64",
                    "candidate-existing-replay",
                    "Gantz E - Chapter 64.cbz",
                    "staged_file_ready",
                    "import_ready",
                    "staged_or_importing",
                    0,
                    str(source_path),
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.execute(
                    """
                    create table if not exists imported_files (
                        sha256 text primary key, source text, dest text, size integer, imported_at real
                    )
                    """
                )
                con.execute(
                    "insert into imported_files values(?,?,?,?,?)",
                    ("stale-existing-replay", str(source_path), str(stale_existing_path), 4, now),
                )
                con.commit()
            inkdrop_reconcile_imports.upsert_reconciliation_records(
                [
                    {
                        "pending_key": "existing-replay-key",
                        "title": "Gantz E - Chapter 64",
                        "query": "Gantz: E Chapter 64",
                        "protocol": "local",
                        "client": "inkdrop_page_pack",
                        "client_id": "suwayomi-existing-64",
                        "download_url_hash": "suwayomi-existing-64",
                        "state": "suppressed_completed",
                        "reason": "canonical_file_already_present",
                        "local_path": str(source_path),
                        "matched_local_path": str(stale_existing_path),
                        "matched_series": "Gantz: E",
                        "trusted_series_id": "mangadex:gantz-e",
                        "trusted_issue": "64",
                        "inkdrop_queue_id": "queue-existing-replay",
                        "inkdrop_download_task_id": "task-existing-replay",
                        "truth_model": "kavita_manga",
                    }
                ],
                updated_at=now,
            )
            with sqlite3.connect(state_db) as con:
                before_without_authority = (
                    con.execute(
                        "select state,active,raw_json from queue_items where id='queue-existing-replay'"
                    ).fetchone(),
                    con.execute(
                        "select state,status,raw_json from download_tasks where id='task-existing-replay'"
                    ).fetchone(),
                    con.execute(
                        "select count(*) from import_results where queue_id='queue-existing-replay'"
                    ).fetchone()[0],
                )
            without_authority = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports(limit=10)
            with sqlite3.connect(state_db) as con:
                after_without_authority = (
                    con.execute(
                        "select state,active,raw_json from queue_items where id='queue-existing-replay'"
                    ).fetchone(),
                    con.execute(
                        "select state,status,raw_json from download_tasks where id='task-existing-replay'"
                    ).fetchone(),
                    con.execute(
                        "select count(*) from import_results where queue_id='queue-existing-replay'"
                    ).fetchone()[0],
                )
            if without_authority.get("updated") != 0 or after_without_authority != before_without_authority:
                fail(
                    "reconciled replay minted authority or changed lifecycle state: "
                    f"result={without_authority} before={before_without_authority} after={after_without_authority}"
                )
            claim_result = inkdrop_reconcile_imports.claim_suppressed_completed_import_authorities(limit=10)
            if claim_result.get("claimed") != 1:
                fail(f"suppressed replay did not begin with a real exact-task import lease: {claim_result}")
            result = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports(limit=10)
            repeated_claim = inkdrop_reconcile_imports.claim_suppressed_completed_import_authorities(limit=10)
            if repeated_claim.get("claimed") != 0:
                fail(f"verified suppressed destination was claimable twice: {repeated_claim}")
            with sqlite3.connect(reconcile_db) as con:
                stored_path = con.execute(
                    "select matched_local_path from download_reconciliation where pending_key='existing-replay-key'"
                ).fetchone()[0]
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
        if stored_path != str(stale_existing_path):
            fail(f"suppressed replay rewrote historical reconciliation evidence: {stored_path}")
        if result.get("updated") != 1:
            fail(f"suppressed existing path was not replayed into InkDrop: {result}")
        with sqlite3.connect(state_db) as con:
            queue = con.execute(
                "select state, active, current_source, display_phase, last_event from queue_items where id='queue-existing-replay'"
            ).fetchone()
            wanted = con.execute("select status from wanted_items where id='wanted-existing-replay'").fetchone()[0]
            task = con.execute(
                "select status, state from download_tasks where id='task-existing-replay'"
            ).fetchone()
            import_row = con.execute(
                """
                select status, verified, completion_truth, folder_imported, dest_path
                from import_results
                where queue_id='queue-existing-replay'
                """
            ).fetchone()
        if queue[:4] != ("verified", 0, "inkdrop_page_pack", "verified"):
            fail(f"suppressed existing replay did not verify queue: {queue}")
        if wanted != "satisfied":
            fail(f"suppressed existing replay did not satisfy wanted row: {wanted}")
        if task != ("queue_verified", "verified"):
            fail(f"suppressed existing replay did not retire staged task: {task}")
        if import_row != ("queue_verified", 1, "folder", 1, str(existing_path)):
            fail(f"suppressed existing replay import_result mismatch: {import_row}")
        with sqlite3.connect(state_db) as con:
            con.execute(
                """
                insert into import_results(
                    id,queue_id,series_id,issue_id,source_path,dest_path,status,verified,created_at,raw_json
                ) values(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "negative-existing-replay", "queue-existing-replay", "series-existing-replay",
                    "issue-existing-replay", str(existing_path), str(existing_path),
                    "preview_not_importable", 0, now,
                    json.dumps({"preview": True}),
                ),
            )
            con.commit()
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            blocked_identity = inkdrop_reconcile_imports.inkdrop_queue_identity_row("queue-existing-replay")
            blocked_existing = inkdrop_reconcile_imports.suppressed_completed_current_managed_path_row(
                blocked_identity, updated_at=now
            )
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
        if blocked_existing:
            fail(f"known unsafe managed artifact was reused as completion truth: {blocked_existing}")
        ambiguous_path = root / "Manga" / "Gantz_E [2020]"
        ambiguous_path.mkdir(parents=True)
        discovered = inkdrop_state.discovered_series_root_folder_candidates(
            {
                "title": "Gantz: E",
                "year": 2020,
                "library_root": str(root / "Manga"),
            }
        )
        if discovered:
            fail(f"ambiguous normalized series folders were treated as authoritative: {discovered}")
        preview_path = existing_path.with_name("Gantz E - Chapter 64 Sample.cbz")
        with zipfile.ZipFile(preview_path, "w") as archive:
            archive.writestr("001.jpg", b"sample page")
        preview_guard = inkdrop_state.managed_folder_artifact_semantic_guard(
            preview_path,
            {"title": "Gantz: E", "media_type": "manga", "metadata_provider": "mangadex"},
            {"issue_number": "64", "normalized_number": "64", "title": "The Relentless Abyss"},
        )
        if preview_guard.get("compatible") or preview_guard.get("artifact_type") != "sample":
            fail(f"managed-folder sample bypassed the non-overridable safety gate: {preview_guard}")
        staging_existing = root / "Downloads" / "comics" / "Chew 060.cbz"
        staging_existing.parent.mkdir(parents=True, exist_ok=True)
        staging_existing.write_bytes(b"already satisfied in completed download")
        staging_row = inkdrop_reconcile_imports.suppressed_completed_existing_path_row(
            "suppressed_completed",
            "already_imported_matching_destination",
            str(staging_existing),
            updated_at=now,
        )
        if staging_row.get("dest") != str(staging_existing):
            fail(f"suppressed completed staging path was not replayable: {staging_row}")


def smoke_import_ready_sync_preserves_suppressed_existing_path():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-sync-existing-path-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        source_path = root / "Downloads" / "inkdrop-source-worker" / "suwayomi" / "Gantz E - Chapter 64.cbz"
        existing_path = root / "Manga" / "Gantz - E (2020)" / "Gantz - E v008 c064.cbz"
        source_path.parent.mkdir(parents=True)
        existing_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"staged page pack")
        existing_path.write_bytes(b"existing library file")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into app_settings(key, scope, label, value_json, description, source, updated_at)
                values(?,?,?,?,?,?,?)
                """,
                (
                    "media_management.manga_root",
                    "media_management",
                    "Manga Root",
                    json.dumps(str(root / "Manga")),
                    "Smoke managed manga root",
                    "smoke",
                    now,
                ),
            )
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-sync-existing", "Gantz: E", "manga", "mangadex", "gantz-e", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-sync-existing", "series-sync-existing", "64", "0064", "Chapter 64", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-sync-existing", "series-sync-existing", "issue-sync-existing", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(
                    id, wanted_id, series_id, issue_id, state, current_source, query,
                    last_event, active, created_at, updated_at, display_phase, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-sync-existing",
                    "wanted-sync-existing",
                    "series-sync-existing",
                    "issue-sync-existing",
                    "importing",
                    "suwayomi",
                    "Gantz: E Chapter 64",
                    "page pack staged file ready",
                    1,
                    now,
                    now,
                    "staged_or_importing",
                    "{}",
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source, provider_id, provider,
                    protocol, download_client, external_id, title, status, state, lifecycle_phase,
                    retry_eligible, local_path, started_at, updated_at, completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-sync-existing",
                    "queue-sync-existing",
                    "wanted-sync-existing",
                    "series-sync-existing",
                    "issue-sync-existing",
                    "suwayomi",
                    "suwayomi",
                    "suwayomi",
                    "http",
                    "inkdrop_page_pack",
                    "suwayomi-sync-existing-64",
                    "Gantz E - Chapter 64.cbz",
                    "staged_file_ready",
                    "import_ready",
                    "staged_or_importing",
                    0,
                    str(source_path),
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()

        def fake_classify(path, row, targets, imported_state, bad_archive_memory):
            if str(path) != str(source_path):
                fail(f"sync classified unexpected path: {path}")
            return {
                "state": "suppressed_completed",
                "reason": "canonical_file_already_present",
                "local_path": str(source_path),
                "matched_local_path": str(existing_path),
                "matched_series": "Gantz: E",
                "truth_model": "kavita_manga",
            }

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        old_targets = inkdrop_reconcile_imports.imp.load_comic_targets
        old_imported_state = inkdrop_reconcile_imports.load_imported_state
        old_bad_archive_memory = inkdrop_reconcile_imports.load_bad_archive_validation_memory
        old_archive_paths = inkdrop_reconcile_imports.archive_paths_for_completed_client_path
        old_classify = inkdrop_reconcile_imports.classify_inkdrop_client_file
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            inkdrop_reconcile_imports.imp.load_comic_targets = lambda _arg: []
            inkdrop_reconcile_imports.load_imported_state = lambda: {
                "source_paths": set(),
                "dest_paths": set(),
                "hash_sizes": set(),
                "hashes": set(),
            }
            inkdrop_reconcile_imports.load_bad_archive_validation_memory = lambda: {}
            inkdrop_reconcile_imports.archive_paths_for_completed_client_path = lambda _path: [source_path]
            inkdrop_reconcile_imports.classify_inkdrop_client_file = fake_classify
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            result = inkdrop_reconcile_imports.sync_inkdrop_import_ready_records(max_records=10, budget_seconds=10)
            with sqlite3.connect(reconcile_db) as con:
                stored = con.execute(
                    """
                    select lifecycle_state, reason, matched_local_path
                    from download_reconciliation
                    where pending_key='inkdrop:queue-sync-existing'
                    """
                ).fetchone()
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
            inkdrop_reconcile_imports.imp.load_comic_targets = old_targets
            inkdrop_reconcile_imports.load_imported_state = old_imported_state
            inkdrop_reconcile_imports.load_bad_archive_validation_memory = old_bad_archive_memory
            inkdrop_reconcile_imports.archive_paths_for_completed_client_path = old_archive_paths
            inkdrop_reconcile_imports.classify_inkdrop_client_file = old_classify
        if result.get("upserted") != 1 or result.get("checked") != 1:
            fail(f"suppressed existing sync did not bridge one row: {result}")
        expected = ("suppressed_completed", "canonical_file_already_present", str(existing_path))
        if stored != expected:
            fail(f"suppressed existing sync stored wrong reconciliation path: {stored}")


def smoke_hash_suppression_preserves_managed_destination():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-hash-destination-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        reconcile_db = root / "imported-files.sqlite3"
        source = root / "Downloads" / "slskd" / "Love and Rockets 006.cbz"
        managed = root / "Comics" / "Love and Rockets (1982)" / "Love and Rockets #006 (1982).cbz"
        source.parent.mkdir(parents=True)
        managed.parent.mkdir(parents=True)
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("001.jpg", b"same exact issue")
        managed.write_bytes(source.read_bytes())
        digest = inkdrop_reconcile_imports.imp.sha256(source)
        with sqlite3.connect(reconcile_db) as con:
            con.execute(
                "create table imported_files(sha256 text primary key, source text, dest text, size integer, imported_at real)"
            )
            con.execute(
                "insert into imported_files values(?,?,?,?,?)",
                (digest, str(source).lower(), str(managed), managed.stat().st_size, time.time()),
            )
            con.commit()
        old_db = inkdrop_reconcile_imports.DB_PATH
        old_match = inkdrop_reconcile_imports.imp.match_comic_target
        old_manga = inkdrop_reconcile_imports.imp.is_manga_target
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.imp.match_comic_target = lambda _path, targets: dict(targets[0])
            inkdrop_reconcile_imports.imp.is_manga_target = lambda _target: False
            imported_state = inkdrop_reconcile_imports.load_imported_state()
            result = inkdrop_reconcile_imports.classify_local_file(
                source,
                [{"id": "comicvine:7000", "title": "Love and Rockets"}],
                imported_state,
                validate_archive=False,
                trusted_issue="6",
            )
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.imp.match_comic_target = old_match
            inkdrop_reconcile_imports.imp.is_manga_target = old_manga
        if result.get("state") != "suppressed_completed" or result.get("reason") != "already_imported_matching_hash":
            fail(f"matching hash was not suppressed as an existing import: {result}")
        if result.get("matched_local_path") != str(managed):
            fail(f"matching hash lost its managed destination: {result}")


def smoke_replay_identity_accepts_explicit_manga_volume_file():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    old_unit = inkdrop_reconcile_imports.imp.manga_file_unit_and_number
    try:
        inkdrop_reconcile_imports.imp.manga_file_unit_and_number = lambda _path: ("chapter", "66")
        imported = {"dest": "/library/manga/Gantz- E/Gantz E v08 (2020).cbz"}
        row = {
            "matched_series": "Gantz: E",
            "trusted_issue": "64",
            "query": "Gantz: E Chapter 64 Volume 8",
            "truth_model": "kavita_manga",
        }
        ok, reason = inkdrop_reconcile_imports.imported_file_identity_match(row, imported)
        if not ok or reason != "series_volume_path_match":
            fail(f"explicit manga volume identity did not match existing volume file: {(ok, reason)}")
        no_volume = dict(row)
        no_volume["query"] = "Gantz: E Chapter 64"
        ok, reason = inkdrop_reconcile_imports.imported_file_identity_match(no_volume, imported)
        if ok:
            fail(f"manga volume identity matched without an explicit queued volume hint: {(ok, reason)}")
        wrong_volume = dict(row)
        wrong_volume["query"] = "Gantz: E Chapter 64 Volume 7"
        ok, reason = inkdrop_reconcile_imports.imported_file_identity_match(wrong_volume, imported)
        if ok:
            fail(f"manga volume identity matched the wrong volume hint: {(ok, reason)}")
        chapter_file = {"dest": "/library/manga/Gantz- E/Gantz E Vol.8 Ch.66.cbz"}
        ok, reason = inkdrop_reconcile_imports.imported_file_identity_match(row, chapter_file)
        if ok:
            fail(f"manga volume identity accepted a chapter-token filename: {(ok, reason)}")
    finally:
        inkdrop_reconcile_imports.imp.manga_file_unit_and_number = old_unit


def smoke_reconciliation_replay_uses_queue_identity_for_imported_file_proof():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-replay-queue-identity-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        wrong_source = root / "Downloads" / "2025.06.04 Weekly Pack" / "Image Week" / "Universal Monsters - The Mummy 003.cbz"
        wrong_dest = root / "Comics" / "Universal Monsters- The Mummy" / "Universal Monsters The Mummy #003.cbz"
        wrong_source.parent.mkdir(parents=True)
        wrong_dest.parent.mkdir(parents=True)
        wrong_source.write_bytes(b"wrong source")
        wrong_dest.write_bytes(b"wrong dest")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-identity-replay", "Absolute Superman", "comic", "comicvine", "160860", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-identity-replay", "series-identity-replay", "8", "008", "Issue 8", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-identity-replay", "series-identity-replay", "issue-identity-replay", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-identity-replay",
                    "wanted-identity-replay",
                    "series-identity-replay",
                    "issue-identity-replay",
                    "importing",
                    "download_client",
                    "Absolute Superman 8 2025",
                    "ready_to_import",
                    1,
                    now,
                    now,
                    "{}",
                ),
            )
        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.execute(
                    "create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)"
                )
                con.execute(
                    "insert into imported_files values(?,?,?,?,?)",
                    ("sha-identity-replay", str(wrong_source), str(wrong_dest), wrong_dest.stat().st_size, now),
                )
                con.execute(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, lifecycle_state,
                        reason, matched_local_path, matched_series, trusted_series_id,
                        trusted_issue, inkdrop_queue_id, imported_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "identity-replay-key",
                        "2025.06.04 Weekly Pack",
                        "Universal Monsters: The Mummy 3 2025",
                        "direct",
                        "inkdrop_local_pack",
                        "client-id-identity",
                        "verified",
                        "kavita_verified",
                        str(wrong_source),
                        "Universal Monsters: The Mummy",
                        "comicvine:universal",
                        "3",
                        "queue-identity-replay",
                        now,
                        now,
                    ),
                )
                con.commit()
            result = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports(limit=10)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
        if result.get("updated") != 0 or result.get("skipped", {}).get("imported_file_identity_mismatch") != 1:
            fail(f"replay trusted stale reconciliation identity instead of queue identity: {result}")
        with sqlite3.connect(state_db) as con:
            count = con.execute("select count(*) from import_results where queue_id='queue-identity-replay'").fetchone()[0]
            queue = con.execute("select state, active, last_event from queue_items where id='queue-identity-replay'").fetchone()
        if count:
            fail(f"queue-identity replay created mismatched import_result rows: {count}")
        if queue != ("importing", 1, "ready_to_import"):
            fail(f"queue-identity replay mutated queue state: {queue}")


def smoke_verified_manga_import_results_backfill_completion_tables():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-manga-import-backfill-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        source = root / "Downloads" / "suwayomi" / "Gantz E - Chapter 72 - Ch.72.cbz"
        dest = root / "Manga" / "Gantz- E" / "Gantz E - Chapter 72.cbz"
        source.parent.mkdir(parents=True)
        dest.parent.mkdir(parents=True)
        for archive_path in (source, dest):
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("001.jpg", b"validated manga page")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into app_settings(key, scope, label, value_json, description, source, updated_at)
                values(?,?,?,?,?,?,?)
                """,
                (
                    "media_management.manga_root",
                    "media_management",
                    "Manga Root",
                    json.dumps(str(root / "Manga")),
                    "Smoke managed manga root",
                    "smoke",
                    now,
                ),
            )
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-gantz-backfill", "Gantz: E", "manga", "comicvine", "99972", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-gantz-72", "series-gantz-backfill", "72", "72", "A Strange Coincidence", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-gantz-72", "series-gantz-backfill", "issue-gantz-72", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-gantz-72",
                    "wanted-gantz-72",
                    "series-gantz-backfill",
                    "issue-gantz-72",
                    "importing",
                    "download_client",
                    "Gantz: E Chapter 72",
                    "download client import verified",
                    1,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()
        result = inkdrop_state.record_direct_import_result(
            state_db,
            "queue-gantz-72",
            source_path=str(source),
            dest_path=str(dest),
            source="download_client",
            status="kavita_verified",
            verified=True,
            imported_count=1,
            raw={"smoke": True, "trusted_series_id": "comicvine:99972", "trusted_issue": "72"},
        )
        if not result.get("ok"):
            fail(f"verified manga import_result fixture failed: {result}")
        with sqlite3.connect(state_db) as con:
            con.execute(
                """
                insert into import_results(
                    id, queue_id, series_id, issue_id, source_path, dest_path,
                    status, outcome, display_phase, completion_truth, folder_imported,
                    library_visibility_required, library_visibility_status, library_visibility_provider,
                    verified, imported_count, skipped_count, created_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "stale-source-path-verified-row",
                    "queue-gantz-72",
                    "series-gantz-backfill",
                    "issue-gantz-72",
                    str(source),
                    str(source),
                    "verified",
                    "success",
                    "verified",
                    "folder",
                    1,
                    1,
                    None,
                    "kavita",
                    1,
                    1,
                    0,
                    now + 10,
                    json.dumps({"raw": {"trusted_issue": "72"}}),
                ),
            )
            con.commit()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_imp_db = inkdrop_reconcile_imports.imp.DB_PATH
        old_imp_state_dir = inkdrop_reconcile_imports.imp.STATE_DIR
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.imp.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.imp.STATE_DIR = root
            reconcile_db.touch()
            inkdrop_reconcile_imports.imp.set_manga_unit_model("Gantz: E", "chapter", source="smoke")
            backfill = inkdrop_reconcile_imports.backfill_verified_manga_import_results(limit=10)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.imp.DB_PATH = old_imp_db
            inkdrop_reconcile_imports.imp.STATE_DIR = old_imp_state_dir
        if backfill.get("backfilled") != 1 or backfill.get("manga_unit_completion_rows") != 1:
            fail(f"verified manga import_result did not backfill completion: {backfill}")
        expected_number = inkdrop_reconcile_imports.imp.normalize_manga_number("72") or "72"
        with sqlite3.connect(reconcile_db) as con:
            unit = con.execute(
                """
                select series_title, normalized_number, manga_unit_model, truth_model, verification_status, target_file_path
                  from manga_unit_completion
                 where normalized_series='gantz e' and normalized_number=?
                """,
                (expected_number,),
            ).fetchone()
            coverage = con.execute(
                """
                select series_title, normalized_number, unit_type, truth_model, verification_status, target_file_path
                  from manga_coverage
                 where normalized_series='gantz e' and normalized_number=?
                """,
                (expected_number,),
            ).fetchone()
        expected_unit = ("Gantz: E", expected_number, "chapter", "kavita_manga", "library_visible", str(dest))
        expected_coverage = ("Gantz: E", expected_number, "chapter", "kavita_manga", "library_visible", str(dest))
        if unit != expected_unit:
            fail(f"manga unit completion row was wrong: {unit}")
        if coverage != expected_coverage:
            fail(f"manga coverage row was wrong: {coverage}")


def smoke_stale_completion_retraction_records_history():
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-stale-completion-history-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
        fixture = {
            "ok": True,
            "checked": 12,
            "retracted": 3,
            "updated_at": 1783345400.123,
            "tables": {
                "manga_unit_completion": {
                    "checked": 8,
                    "retracted": 2,
                    "reasons": {
                        "stale_target_missing": 1,
                        "stale_target_number_mismatch": 1,
                    },
                },
                "manga_coverage": {
                    "checked": 4,
                    "retracted": 1,
                    "reasons": {"stale_target_missing": 1},
                },
            },
        }
        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_backfill = inkdrop_reconcile_imports.backfill_verified_manga_import_results
        old_retract = inkdrop_reconcile_imports.imp.retract_stale_completion_rows
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            inkdrop_reconcile_imports.backfill_verified_manga_import_results = (
                lambda *args, **kwargs: {"ok": True, "checked": 0, "backfilled": 0}
            )
            inkdrop_reconcile_imports.imp.retract_stale_completion_rows = lambda *args, **kwargs: fixture
            result = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports(limit=10)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.backfill_verified_manga_import_results = old_backfill
            inkdrop_reconcile_imports.imp.retract_stale_completion_rows = old_retract
        history_result = result.get("stale_completion_history") or {}
        if not history_result.get("ok") or history_result.get("skipped"):
            fail(f"stale completion retraction did not record history: {result}")
        with sqlite3.connect(state_db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                select event_type, entity_type, entity_id, source, message,
                       outcome, display_phase, raw_json
                  from history_events
                 where event_type='stale_completion_retracted'
                """
            ).fetchall()
        if len(rows) != 1:
            fail(f"stale completion history row count was wrong: {len(rows)}")
        row = dict(rows[0])
        if row["entity_type"] != "import_ledger" or row["entity_id"] != "completion_ledger":
            fail(f"stale completion history identity was wrong: {row}")
        if row["source"] != "import_ready":
            fail(f"stale completion history source was wrong: {row}")
        message = row["message"]
        for token in (
            "Retracted 3 stale completion proof rows",
            "manga_unit_completion:2",
            "manga_coverage:1",
            "stale_target_missing:2",
            "stale_target_number_mismatch:1",
        ):
            if token not in message:
                fail(f"stale completion history message missing {token!r}: {message}")
        if row["outcome"] != "problem" or row["display_phase"] != "problem":
            fail(f"stale completion history did not surface as a problem activity: {row}")
        raw = json.loads(row["raw_json"])
        if raw.get("retracted") != 3 or raw.get("checked") != 12:
            fail(f"stale completion history raw counts were wrong: {raw}")
        if raw.get("tables", {}).get("manga_unit_completion", {}).get("retracted") != 2:
            fail(f"stale completion history raw table summary was wrong: {raw}")


def smoke_import_ready_rejection_requeues():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-reject-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-reject", "Reject Series", "comic", "comicvine", "3", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-reject", "series-reject", "3", "3", "Issue 3", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-reject", "series-reject", "issue-reject", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-reject",
                    "wanted-reject",
                    "series-reject",
                    "issue-reject",
                    "importing",
                    "sabnzbd",
                    "Reject Series 3",
                    "SABnzbd completed in client; import worker will scan it",
                    1,
                    now,
                    now,
                    "{}",
                ),
            )
            for task_id, state, status in (
                ("task-reject-ready", "import_ready", "completed_in_client"),
                ("task-reject-sent", "queued", "sent"),
            ):
                con.execute(
                    """
                    insert into download_tasks(
                        id, queue_id, wanted_id, series_id, issue_id, source, provider, protocol,
                        download_client, external_id, title, status, state, category, local_path,
                        started_at, updated_at, completed_at, raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        task_id,
                        "queue-reject",
                        "wanted-reject",
                        "series-reject",
                        "issue-reject",
                        "prowlarr",
                        "DOGnzb",
                        "usenet",
                        "SABnzbd",
                        "nzo-reject",
                        "Reject.Series.v01",
                        status,
                        state,
                        "comics",
                        str(root / "Reject.Series.v01" / "Reject Series v01.cbr"),
                        now,
                        now,
                        now if state == "import_ready" else None,
                        "{}",
                    ),
                )
            con.commit()
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            result = inkdrop_reconcile_imports.record_inkdrop_import_ready_rejection(
                {
                    "queue_id": "queue-reject",
                    "download_task_id": "task-reject-ready",
                    "download_client": "SABnzbd",
                    "protocol": "usenet",
                    "task_title": "Reject.Series.v01",
                    "local_path": str(root / "Reject.Series.v01" / "Reject Series v01.cbr"),
                },
                {
                    "state": "manual_review",
                    "reason": "unmatched_local_file",
                    "local_path": str(root / "Reject.Series.v01" / "Reject Series v01.cbr"),
                },
            )
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
        if not result.get("ok") or result.get("task_updates") != 2:
            fail(f"import-ready rejection did not update the queue and duplicate tasks: {result}")
        with sqlite3.connect(state_db) as con:
            queue = con.execute("select state, current_source, last_event from queue_items where id='queue-reject'").fetchone()
            if queue[0] != "queued" or queue[1] is not None or "not importable" not in queue[2]:
                fail(f"rejected import-ready row was not requeued automatically: {queue}")
            statuses = con.execute(
                "select status, state, retry_eligible from download_tasks where queue_id='queue-reject' order by id"
            ).fetchall()
            if statuses != [("failed_download", "failed", 1), ("failed_download", "failed", 1)]:
                fail(f"rejected import-ready tasks were not retired: {statuses}")
            attempts = con.execute(
                "select status, source, download_client from source_attempts where queue_id='queue-reject'"
            ).fetchall()
            if ("retry_scheduled", "download_client", "sab") not in attempts:
                fail(f"rejected import-ready source attempt was not recorded: {attempts}")


def smoke_failed_import_attempt_requeues_import_ready_download():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-failed-import-reject-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        source_path = root / "Bad Pack" / "Reject Series 003.cbr"
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"bad archive")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-failed-import", "Reject Series", "comic", "comicvine", "4", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-failed-import", "series-failed-import", "3", "3", "Issue 3", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-failed-import", "series-failed-import", "issue-failed-import", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-failed-import",
                    "wanted-failed-import",
                    "series-failed-import",
                    "issue-failed-import",
                    "importing",
                    "sabnzbd",
                    "Reject Series 3",
                    "SABnzbd completed in client; import worker will scan it",
                    1,
                    now,
                    now,
                    "{}",
                ),
            )
            con.execute(
                """
                insert into source_attempts(
                    id,queue_id,wanted_id,series_id,issue_id,source,provider,protocol,download_client,
                    candidate_identity,lifecycle_phase,outcome,display_phase,retry_eligible,status,title,
                    started_at,completed_at,raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "attempt-failed-import", "queue-failed-import", "wanted-failed-import",
                    "series-failed-import", "issue-failed-import", "prowlarr", "DOGnzb", "usenet", "sabnzbd",
                    "candidate-failed-import", "completed", "success", "completed", 0, "completed",
                    "Reject.Series.003", now, now, "{}",
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source_attempt_id, source, provider, protocol,
                    download_client, external_id, candidate_identity, title, status, state, category, local_path,
                    started_at, updated_at, completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-failed-import",
                    "queue-failed-import",
                    "wanted-failed-import",
                    "series-failed-import",
                    "issue-failed-import",
                    "attempt-failed-import",
                    "prowlarr",
                    "DOGnzb",
                    "usenet",
                    "SABnzbd",
                    "nzo-failed-import",
                    "candidate-failed-import",
                    "Reject.Series.003",
                    "completed_in_client",
                    "import_ready",
                    "comics",
                    str(source_path),
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            callback_record = {
                "inkdrop_queue_id": "queue-failed-import",
                "inkdrop_download_task_id": "task-failed-import",
                "inkdrop_source_attempt_id": "attempt-failed-import",
                "inkdrop_external_id": "nzo-failed-import",
                "inkdrop_candidate_identity": "candidate-failed-import",
                "inkdrop_download_client": "sabnzbd",
                "inkdrop_task_local_path": str(source_path),
                "client": "sab",
                "protocol": "usenet",
                "title": "Reject.Series.003",
                "query": "Reject Series 3",
                "matched_series": "Reject Series",
                "source_file": str(source_path),
            }
            claim = inkdrop_reconcile_imports.claim_inkdrop_import_attempt(callback_record)
            if not claim.get("ok"):
                fail(f"failed-import callback authority claim failed: {claim}")
            callback_record["import_authority"] = claim["authority"]
            result = inkdrop_reconcile_imports.record_inkdrop_import_attempt(
                callback_record,
                {
                    "imported": [],
                    "skipped": [
                        {
                            "event": "skip_bad_comic_archive",
                            "skip_reason": "bad_zip_member",
                            "source": str(source_path),
                            "archive_check": {"ok": False, "reason": "bad_zip_member"},
                        }
                    ],
                },
                returncode=0,
            )
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
        release = result.get("release") if isinstance(result, dict) else {}
        if result.get("skipped") != "failed_import" or not release.get("ok"):
            fail(f"failed import attempt did not reject the import-ready candidate: {result}")
        with sqlite3.connect(state_db) as con:
            queue = con.execute("select state, current_source, last_event from queue_items where id='queue-failed-import'").fetchone()
            if queue[0] != "queued" or queue[1] is not None or "automatic search will try another result" not in queue[2].lower():
                fail(f"failed import attempt did not requeue the item: {queue}")
            task = con.execute(
                "select status, state, lifecycle_phase, failure_reason, retry_eligible, raw_json from download_tasks where id='task-failed-import'"
            ).fetchone()
            expected = ("bad_archive", "failed", "failed_candidate", "importer_skipped_bad_zip_member", 0)
            if task[:5] != expected:
                fail(f"failed import attempt did not retire the bad candidate: {task}")
            task_raw = json.loads(task[5] or "{}")
            if not task_raw.get("artifact_retry_blocked") or task_raw.get("artifact_retry_blocked_reason") != "importer_skipped_bad_zip_member":
                fail(f"terminal artifact retry marker was not preserved: {task_raw}")
            attempts = con.execute(
                "select status, source, download_client from source_attempts where queue_id='queue-failed-import'"
            ).fetchall()
            if ("bad_archive", "prowlarr", "sabnzbd") not in attempts:
                fail(f"failed import rejection source attempt was not recorded: {attempts}")
        for terminal_skip in (
            {"skip_reason": "known_bad_artifact_content"},
            {"event": "skip_source_target_identity_mismatch", "action_needed": "manual_identity_review"},
            {"skip_reason": "wrong_series_or_subseries", "action_needed": "retry_another_source"},
            {"event": "skip_bad_comic_archive", "skip_reason": "too_little_image_payload", "action_needed": "regrab_or_manual_review"},
            {"event": "skip_unsafe_collection_part_match", "skip_reason": "unsafe_collection_identity", "action_needed": "manual_review"},
        ):
            if not inkdrop_reconcile_imports.terminal_import_artifact_rejection({"skipped": [terminal_skip]}):
                fail(f"terminal exact-artifact rejection was misclassified: {terminal_skip}")
        if inkdrop_reconcile_imports.terminal_import_artifact_rejection(
            {"skipped": [{"skip_reason": "source_file_incomplete_qbit_download", "action_needed": "automatic_wait"}]}
        ):
            fail("incomplete active transfer was incorrectly classified as a terminal artifact")
        first_recovery = inkdrop_reconcile_imports.recover_retryable_failed_staged_import_ready_records(max_records=10)
        second_recovery = inkdrop_reconcile_imports.recover_retryable_failed_staged_import_ready_records(max_records=10)
        if first_recovery.get("promoted") or second_recovery.get("promoted"):
            fail(f"terminal artifact was promoted back to import-ready: {first_recovery} / {second_recovery}")
        with sqlite3.connect(state_db) as con:
            repeated = con.execute(
                "select status, state, retry_eligible from download_tasks where id='task-failed-import'"
            ).fetchone()
        if repeated != ("bad_archive", "failed", 0):
            fail(f"terminal artifact retirement was not idempotent: {repeated}")


def smoke_import_ready_records_existing_planned_destination():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-existing-planned-dest-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        source_path = root / "downloads" / "Managed Series 001.cbz"
        planned_path = root / "Comics" / "Managed Series (2026)" / "Managed Series #001 (2026).cbz"
        source_path.parent.mkdir(parents=True)
        planned_path.parent.mkdir(parents=True)
        for archive_path in (source_path, planned_path):
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("001.jpg", b"validated planned destination page")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into app_settings(key, scope, label, value_json, description, source, updated_at)
                values(?,?,?,?,?,?,?)
                """,
                (
                    "media_management.comic_root",
                    "media_management",
                    "Comic Root",
                    json.dumps(str(root / "Comics")),
                    "Smoke managed comic root",
                    "smoke",
                    now,
                ),
            )
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-existing-planned", "Managed Series", "comic", "comicvine", "1001", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-existing-planned", "series-existing-planned", "1", "1", "Issue 1", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-existing-planned", "series-existing-planned", "issue-existing-planned", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-existing-planned",
                    "wanted-existing-planned",
                    "series-existing-planned",
                    "issue-existing-planned",
                    "importing",
                    "sabnzbd",
                    "Managed Series 1",
                    "SABnzbd completed in client; import worker will scan it",
                    1,
                    now,
                    now,
                    "{}",
                ),
            )
            con.execute(
                """
                insert into source_attempts(
                    id,queue_id,wanted_id,series_id,issue_id,source,provider,protocol,download_client,
                    candidate_identity,lifecycle_phase,outcome,display_phase,retry_eligible,status,title,
                    started_at,completed_at,raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "attempt-existing-planned", "queue-existing-planned", "wanted-existing-planned",
                    "series-existing-planned", "issue-existing-planned", "prowlarr", "DOGnzb", "usenet", "sabnzbd",
                    "candidate-existing-planned", "completed", "success", "completed", 0, "completed",
                    "Managed.Series.001", now, now, "{}",
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source_attempt_id, source, provider, protocol,
                    download_client, external_id, candidate_identity, title, status, state, category, local_path,
                    started_at, updated_at, completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-existing-planned",
                    "queue-existing-planned",
                    "wanted-existing-planned",
                    "series-existing-planned",
                    "issue-existing-planned",
                    "attempt-existing-planned",
                    "prowlarr",
                    "DOGnzb",
                    "usenet",
                    "SABnzbd",
                    "nzo-existing-planned",
                    "candidate-existing-planned",
                    "Managed.Series.001",
                    "completed_in_client",
                    "import_ready",
                    "comics",
                    str(source_path),
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            callback_record = {
                "inkdrop_queue_id": "queue-existing-planned",
                "inkdrop_download_task_id": "task-existing-planned",
                "inkdrop_source_attempt_id": "attempt-existing-planned",
                "inkdrop_external_id": "nzo-existing-planned",
                "inkdrop_candidate_identity": "candidate-existing-planned",
                "inkdrop_download_client": "sabnzbd",
                "inkdrop_task_local_path": str(source_path),
                "client": "sab",
                "protocol": "usenet",
                "title": "Managed.Series.001",
                "query": "Managed Series 1",
                "matched_series": "Managed Series",
                "source_file": str(source_path),
            }
            claim = inkdrop_reconcile_imports.claim_inkdrop_import_attempt(callback_record)
            if not claim.get("ok"):
                fail(f"existing-planned callback authority claim failed: {claim}")
            callback_record["import_authority"] = claim["authority"]
            result = inkdrop_reconcile_imports.record_inkdrop_import_attempt(
                callback_record,
                {
                    "imported": [],
                    "skipped": [
                        {
                            "event": "skip_media_management_existing_destination",
                            "skip_reason": "media_management_destination_exists",
                            "action_needed": "none",
                            "source": str(source_path),
                            "dest": str(planned_path),
                            "media_management_destination_decision": {
                                "enabled": True,
                                "override": True,
                                "legacy_dest_path": str(source_path),
                                "selected_dest_path": str(planned_path),
                                "planned_path": str(planned_path),
                                "applied": False,
                                "skip_existing_destination": True,
                                "reason": "planned_path_exists",
                            },
                            "media_management_preview": {
                                "planned_path": str(planned_path),
                                "selected_import_dest_path": str(planned_path),
                                "legacy_import_dest_path": str(source_path),
                                "current_import_dest_path": str(planned_path),
                                "current_import_dest_matches_preview": True,
                                "planned_path_apply_status": "blocked_existing_destination",
                                "planned_path_applied": False,
                                "apply_planned_path_override": True,
                            },
                        }
                    ],
                },
                returncode=0,
            )
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
        if not result.get("ok"):
            fail(f"existing planned destination was not recorded as retained import: {result}")
        with sqlite3.connect(state_db) as con:
            row = con.execute(
                "select source_path, dest_path, status, verified, skipped_count, raw_json from import_results where queue_id='queue-existing-planned'"
            ).fetchone()
            if not row:
                fail("existing planned destination did not create an import_result")
            if row[1] != str(planned_path):
                fail(f"import_result dest_path did not preserve selected planned path: {row}")
            if row[0] != str(source_path):
                fail(f"import_result source_path did not preserve source file: {row}")
            if row[2] != "imported" or row[3] != 0 or row[4] != 1:
                fail(f"existing planned destination got unexpected import lifecycle: {row[:5]}")
            raw = json.loads(row[5] or "{}")
            decision = raw.get("media_management_destination_decision") or {}
            if decision.get("selected_dest_path") != str(planned_path) or decision.get("reason") != "planned_path_exists":
                fail(f"planned destination decision was not persisted: {raw}")
            queue = con.execute("select state, current_source from queue_items where id='queue-existing-planned'").fetchone()
            if queue[0] not in {"importing", "queued", "verified", "satisfied"}:
                fail(f"queue state was unexpectedly corrupted after retained import: {queue}")


def smoke_import_ready_timeout_recovers_imported_file():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-timeout-recovery-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        source = root / "Completed Pack" / "Smoke Series 004 (2026).cbz"
        dest = root / "Comics" / "Smoke Series" / "Smoke Series #004 (2026).cbz"
        source.parent.mkdir(parents=True)
        dest.parent.mkdir(parents=True)
        for archive_path in (source, dest):
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("001.jpg", b"validated timeout recovery page")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into app_settings(key, scope, label, value_json, description, source, updated_at)
                values(?,?,?,?,?,?,?)
                """,
                (
                    "media_management.comic_root",
                    "media_management",
                    "Comic Root",
                    json.dumps(str(root / "Comics")),
                    "Smoke managed comic root",
                    "smoke",
                    now,
                ),
            )
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-timeout", "Smoke Series", "comic", "comicvine", "4", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-timeout", "series-timeout", "4", "4", "Issue 4", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-timeout", "series-timeout", "issue-timeout", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-timeout",
                    "wanted-timeout",
                    "series-timeout",
                    "issue-timeout",
                    "queued",
                    None,
                    "Smoke Series 4",
                    "Import-ready importer timed out; automatic retry scheduled",
                    1,
                    now,
                    now,
                    "{}",
                ),
            )
            con.execute(
                """
                insert into source_attempts(
                    id,queue_id,wanted_id,series_id,issue_id,source,provider,protocol,download_client,
                    candidate_identity,lifecycle_phase,outcome,display_phase,retry_eligible,status,title,
                    started_at,completed_at,raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "attempt-timeout", "queue-timeout", "wanted-timeout", "series-timeout", "issue-timeout",
                    "download_client", "qbit", "torrent", "qbittorrent", "candidate-timeout", "failed_candidate",
                    "failed", "retry_later", 1, "failed_import", "Completed Pack", now, now, "{}",
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source, provider, protocol,
                    download_client, source_attempt_id, external_id, candidate_identity, title, status, state, lifecycle_phase,
                    failure_reason, retry_eligible, local_path, started_at, updated_at,
                    completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-timeout",
                    "queue-timeout",
                    "wanted-timeout",
                    "series-timeout",
                    "issue-timeout",
                    "download_client",
                    "qbit",
                    "torrent",
                    "qBittorrent",
                    "attempt-timeout",
                    "hash-timeout",
                    "candidate-timeout",
                    "Completed Pack",
                    "staged_file_ready",
                    "import_ready",
                    "import_ready",
                    "import_ready_import_timeout",
                    1,
                    str(source),
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        old_imp = inkdrop_reconcile_imports.imp
        scan_calls = []

        class FakeImporter:
            @staticmethod
            def verify_imported_items(items, poll_kavita=False):
                return {
                    "checked": [
                        {
                            "verification_status": "waiting_for_kavita_scan",
                            "host_exists": True,
                            "kavita_visible": False,
                        }
                    ],
                    "waiting_for_kavita_scan_count": 1,
                    "pending_scan_count": 1,
                }

            @staticmethod
            def trigger_kavita_scan_folder(folder, force_library_scan=False):
                scan_calls.append((str(folder), bool(force_library_scan)))
                return {"folder": str(folder), "status_code": 200}

        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            inkdrop_reconcile_imports.imp = FakeImporter()
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.execute("create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
                con.execute(
                    "insert into imported_files values(?,?,?,?,?)",
                    ("sha-timeout", str(source), str(dest), dest.stat().st_size, now),
                )
                con.execute(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, lifecycle_state,
                        reason, matched_local_path, matched_series, trusted_series_id,
                        trusted_issue, inkdrop_queue_id, inkdrop_download_task_id,
                        completed_seen_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "inkdrop:queue-timeout",
                        "Completed Pack",
                        "Smoke Series 4",
                        "torrent",
                        "qbit",
                        "hash-timeout",
                        "failed_import",
                        "import_ready_import_timeout",
                        str(source),
                        "Smoke Series",
                        "comicvine:4",
                        "4",
                        "queue-timeout",
                        "task-timeout",
                        now,
                        now,
                    ),
                )
                con.commit()
            recovered = inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files(limit=10)
            retry_claim = inkdrop_state.claim_import_authority(
                state_db,
                "queue-timeout",
                "task-timeout",
                source_attempt_id="attempt-timeout",
                external_id="hash-timeout",
                candidate_identity="candidate-timeout",
                download_client="qbittorrent",
                local_path=str(source),
                claimed_at=now + 1,
            )
            if not retry_claim.get("ok"):
                fail(f"timeout retry did not acquire a normal import lease: {retry_claim}")
            replay = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports(limit=10)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
            inkdrop_reconcile_imports.imp = old_imp

        if recovered.get("recovered") != 1 or recovered.get("waiting_for_scan") != 1:
            fail(f"timeout recovery did not recover copied import: {recovered}")
        if not scan_calls:
            fail("timeout recovery did not queue a Kavita scan for a copied-but-not-visible file")
        if replay.get("updated") != 1:
            fail(f"timeout recovery did not replay into InkDrop: {replay}")
        with sqlite3.connect(reconcile_db) as con:
            rec = con.execute(
                "select lifecycle_state, reason from download_reconciliation where pending_key='inkdrop:queue-timeout'"
            ).fetchone()
        if rec != ("waiting_for_library_scan", "imported_after_timeout_waiting_for_library_scan"):
            fail(f"reconciliation row was not moved to waiting scan: {rec}")
        with sqlite3.connect(state_db) as con:
            queue = con.execute("select state, active, current_source, last_event, display_phase from queue_items where id='queue-timeout'").fetchone()
            if queue[:3] != ("verified", 0, "qbit") or "library visibility will follow" not in str(queue[3] or "").lower() or queue[4] != "verified":
                fail(f"queue was not satisfied by optional folder completion: {queue}")
            task = con.execute(
                "select status, state, lifecycle_phase, failure_reason, retry_eligible from download_tasks where id='task-timeout'"
            ).fetchone()
            if task[:2] != ("queue_verified", "verified"):
                fail(f"timeout task was not retired after optional folder completion: {task}")
            imported = con.execute(
                "select status, verified, dest_path from import_results where queue_id='queue-timeout'"
            ).fetchone()
            if imported != ("waiting_for_library_scan", 0, str(dest)):
                fail(f"import_result was not recorded as waiting for scan: {imported}")


def smoke_queue_backed_ready_import_skips_duplicate_prevalidation():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-prevalidate-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        reconcile_db = root / "imported-files.sqlite3"
        source = root / "Queue Backed 001.cbr"
        source.write_bytes(b"not actually opened by preselection")
        now = time.time()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_sync_ready = inkdrop_reconcile_imports.sync_inkdrop_import_ready_records
        old_sync_results = inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results
        old_recover = inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files
        old_replay = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports
        old_ready_rows = inkdrop_reconcile_imports.inkdrop_import_ready_rows
        old_imp = inkdrop_reconcile_imports.imp

        class NoPrevalidateImporter:
            @staticmethod
            def validate_comic_archive(path):
                raise AssertionError(f"queue-backed ready import was prevalidated: {path}")

        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.sync_inkdrop_import_ready_records = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.inkdrop_import_ready_rows = lambda limit=300: [{"queue_id": "queue-prevalidate"}]
            inkdrop_reconcile_imports.imp = NoPrevalidateImporter()
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.execute("create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
                con.execute(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, lifecycle_state,
                        reason, matched_local_path, matched_series, trusted_series_id,
                        trusted_issue, inkdrop_queue_id, inkdrop_download_task_id,
                        completed_seen_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "inkdrop:queue-prevalidate",
                        "Queue Backed",
                        "Queue Backed 1",
                        "torrent",
                        "qbit",
                        "hash-prevalidate",
                        "ready_to_import",
                        "download_client_ready",
                        str(source),
                        "Queue Backed",
                        "comicvine:55",
                        "1",
                        "queue-prevalidate",
                        "task-prevalidate",
                        now,
                        now,
                    ),
                )
                con.commit()
            records = inkdrop_reconcile_imports.ready_import_records(1)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.sync_inkdrop_import_ready_records = old_sync_ready
            inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = old_sync_results
            inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files = old_recover
            inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = old_replay
            inkdrop_reconcile_imports.inkdrop_import_ready_rows = old_ready_rows
            inkdrop_reconcile_imports.imp = old_imp

        if len(records) != 1 or records[0].get("source_file") != str(source):
            fail(f"queue-backed ready import was not selected without prevalidation: {records}")


def smoke_failed_filename_guard_recovery_is_queue_authoritative():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-filename-recover-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        reconcile_db = root / "imported-files.sqlite3"
        source = root / "The Department of Truth 015.cbr"
        stale_source = root / "Wrong Task 015.cbr"
        source.write_bytes(b"ready")
        stale_source.write_bytes(b"stale")
        now = time.time()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_active_rows = inkdrop_reconcile_imports.active_import_ready_rows
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.active_import_ready_rows = lambda limit=300: [
                {
                    "inkdrop_queue_id": "queue-trusted",
                    "inkdrop_download_task_id": "task-trusted",
                    "matched_local_path": str(source),
                    "trusted_issue": "15",
                }
            ]
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.executemany(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, lifecycle_state,
                        reason, matched_local_path, matched_series, trusted_series_id,
                        trusted_issue, inkdrop_queue_id, inkdrop_download_task_id,
                        completed_seen_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            "inkdrop:queue-trusted",
                            "The Department of Truth",
                            "The Department of Truth 15",
                            "usenet",
                            "sab",
                            "nzo-trusted",
                            "failed_import",
                            "importer_skipped_filename_confidence_too_low",
                            str(source),
                            "The Department of Truth",
                            "comicvine:130740",
                            "15",
                            "queue-trusted",
                            "task-trusted",
                            now,
                            now,
                        ),
                        (
                            "inkdrop:queue-stale",
                            "The Department of Truth",
                            "The Department of Truth 15",
                            "usenet",
                            "sab",
                            "nzo-stale",
                            "failed_import",
                            "importer_skipped_filename_confidence_too_low",
                            str(stale_source),
                            "The Department of Truth",
                            "comicvine:130740",
                            "15",
                            "queue-trusted",
                            "task-stale",
                            now,
                            now,
                        ),
                    ],
                )
                con.commit()
            result = inkdrop_reconcile_imports.recover_failed_filename_guard_import_ready_records(max_records=10)
            with sqlite3.connect(reconcile_db) as con:
                recovered = con.execute(
                    "select lifecycle_state, reason from download_reconciliation where pending_key='inkdrop:queue-trusted'"
                ).fetchone()
                stale = con.execute(
                    "select lifecycle_state, reason from download_reconciliation where pending_key='inkdrop:queue-stale'"
                ).fetchone()
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.active_import_ready_rows = old_active_rows

        if result.get("recovered") != 1:
            fail(f"trusted filename guard recovery did not recover exactly one row: {result}")
        if recovered != ("ready_to_import", "trusted_issue_filename_guard_retry"):
            fail(f"trusted filename guard row was not restored to ready import: {recovered}")
        if stale != ("failed_import", "importer_skipped_filename_confidence_too_low"):
            fail(f"stale filename guard row should not recover without exact task/path identity: {stale}")


def smoke_import_ready_runs_state_sync_before_record_selection():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    old_recover = inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files
    old_sync = inkdrop_reconcile_imports.sync_inkdrop_import_ready_records
    old_ready = inkdrop_reconcile_imports.ready_import_records
    calls = []
    try:
        inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files = lambda *args, **kwargs: {"checked": 0}

        def sync_before_ready(*args, **kwargs):
            calls.append(("sync", kwargs.get("max_records")))
            return {"ok": True, "upserted": 1, "ready": 1}

        def ready_after_sync(*args, **kwargs):
            calls.append(("ready", args[0] if args else None))
            return []

        inkdrop_reconcile_imports.sync_inkdrop_import_ready_records = sync_before_ready
        inkdrop_reconcile_imports.ready_import_records = ready_after_sync
        result = inkdrop_reconcile_imports.import_ready(3)
    finally:
        inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files = old_recover
        inkdrop_reconcile_imports.sync_inkdrop_import_ready_records = old_sync
        inkdrop_reconcile_imports.ready_import_records = old_ready

    if [call[0] for call in calls] != ["sync", "ready"]:
        fail(f"import_ready did not sync InkDrop active ready rows before selecting records: {calls}")
    if result.get("state_import_ready_sync", {}).get("upserted") != 1:
        fail(f"import_ready result did not report state import-ready sync: {result}")
    if result.get("reason") != "no_reconciled_ready_source_files":
        fail(f"empty post-sync import-ready batch should still be reported cleanly: {result}")


def smoke_ready_import_defers_qbit_incomplete_source_files():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-incomplete-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        reconcile_db = root / "imported-files.sqlite3"
        source = root / "Incomplete Pack 001.cbz"
        source.write_bytes(b"incomplete qbit file placeholder")
        now = time.time()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_sync_ready = inkdrop_reconcile_imports.sync_inkdrop_import_ready_records
        old_sync_results = inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results
        old_recover = inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files
        old_replay = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports
        old_ready_rows = inkdrop_reconcile_imports.inkdrop_import_ready_rows
        old_imp = inkdrop_reconcile_imports.imp

        class IncompleteImporter:
            @staticmethod
            def load_qbit_incomplete_paths(kind):
                return {str(source)} if kind == "comics" else set()

            @staticmethod
            def validate_comic_archive(path):
                return {"ok": False, "reason": "zip_file_invalid"}

        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.sync_inkdrop_import_ready_records = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.inkdrop_import_ready_rows = lambda limit=300: [{"queue_id": "queue-incomplete"}]
            inkdrop_reconcile_imports.imp = IncompleteImporter()
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.execute("create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
                con.execute(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, lifecycle_state,
                        reason, matched_local_path, matched_series, trusted_series_id,
                        trusted_issue, inkdrop_queue_id, inkdrop_download_task_id,
                        completed_seen_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "inkdrop:queue-incomplete",
                        "Incomplete Pack",
                        "Incomplete Pack 1",
                        "torrent",
                        "inkdrop_local_pack",
                        "local-pack",
                        "ready_to_import",
                        "safe_exact_local_match",
                        str(source),
                        "Incomplete Pack",
                        "comicvine:56",
                        "1",
                        "queue-incomplete",
                        "task-incomplete",
                        now,
                        now,
                    ),
                )
                con.commit()
            records = inkdrop_reconcile_imports.ready_import_records(1)
            with sqlite3.connect(reconcile_db) as con:
                row = con.execute(
                    "select lifecycle_state, reason from download_reconciliation where pending_key='inkdrop:queue-incomplete'"
                ).fetchone()
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.sync_inkdrop_import_ready_records = old_sync_ready
            inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = old_sync_results
            inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files = old_recover
            inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = old_replay
            inkdrop_reconcile_imports.inkdrop_import_ready_rows = old_ready_rows
            inkdrop_reconcile_imports.imp = old_imp

        if records:
            fail(f"incomplete qbit source was selected for import: {records}")
        if row != ("downloading", "source_file_incomplete_qbit_download"):
            fail(f"incomplete qbit source was not deferred in reconciliation: {row}")


def smoke_ready_import_accepts_valid_child_from_incomplete_pack():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-valid-child-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        reconcile_db = root / "imported-files.sqlite3"
        source = root / "2025.09.10 Weekly Pack" / "2025.09.10 DC Week" / "Absolute Batman 012.cbz"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"validated child archive")
        now = time.time()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_sync_ready = inkdrop_reconcile_imports.sync_inkdrop_import_ready_records
        old_sync_results = inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results
        old_recover = inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files
        old_replay = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports
        old_ready_rows = inkdrop_reconcile_imports.inkdrop_import_ready_rows
        old_imp = inkdrop_reconcile_imports.imp

        class ValidChildImporter:
            @staticmethod
            def load_qbit_incomplete_paths(kind):
                return {str(source)} if kind == "comics" else set()

            @staticmethod
            def validate_comic_archive(path):
                if str(path) != str(source):
                    raise AssertionError(f"unexpected archive validation path: {path}")
                return {"ok": True, "reason": "valid_child_archive"}

        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.sync_inkdrop_import_ready_records = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.inkdrop_import_ready_rows = lambda limit=300: [{"queue_id": "queue-valid-child"}]
            inkdrop_reconcile_imports.imp = ValidChildImporter()
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.execute("create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
                con.execute(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, lifecycle_state,
                        reason, matched_local_path, matched_series, trusted_series_id,
                        trusted_issue, inkdrop_queue_id, inkdrop_download_task_id,
                        completed_seen_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "inkdrop:queue-valid-child",
                        "2025.09.10 Weekly Pack",
                        "Absolute Batman 12",
                        "torrent",
                        "inkdrop_local_pack",
                        "local-pack",
                        "ready_to_import",
                        "safe_exact_local_match",
                        str(source),
                        "Absolute Batman",
                        "comicvine:160294",
                        "12",
                        "queue-valid-child",
                        "task-valid-child",
                        now,
                        now,
                    ),
                )
                con.commit()
            records = inkdrop_reconcile_imports.ready_import_records(1)
            with sqlite3.connect(reconcile_db) as con:
                row = con.execute(
                    "select lifecycle_state, reason from download_reconciliation where pending_key='inkdrop:queue-valid-child'"
                ).fetchone()
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.sync_inkdrop_import_ready_records = old_sync_ready
            inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = old_sync_results
            inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files = old_recover
            inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = old_replay
            inkdrop_reconcile_imports.inkdrop_import_ready_rows = old_ready_rows
            inkdrop_reconcile_imports.imp = old_imp

        if len(records) != 1 or records[0].get("source_file") != str(source):
            fail(f"validated child file from incomplete broad pack was not selected: {records}")
        if row != ("ready_to_import", "safe_exact_local_match"):
            fail(f"validated child import row was unexpectedly demoted: {row}")


def smoke_import_ready_deferral_updates_inkdrop_task_state():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-deferral-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-deferral", "Deferral Series", "comic", "comicvine", "56", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-deferral", "series-deferral", "1", "001", "Issue 1", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-deferral", "series-deferral", "issue-deferral", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, display_phase, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("queue-deferral", "wanted-deferral", "series-deferral", "issue-deferral", "importing", "download_client", "Deferral Series 1", "local pack ready", 1, now, now, "staged_or_importing", "{}"),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source, provider, protocol,
                    download_client, external_id, title, status, state, lifecycle_phase,
                    retry_eligible, local_path, started_at, updated_at, completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-deferral",
                    "queue-deferral",
                    "wanted-deferral",
                    "series-deferral",
                    "issue-deferral",
                    "local_pack",
                    "Local completed pack",
                    "torrent",
                    "inkdrop_local_pack",
                    "local-pack",
                    "Deferral Series 001.cbz",
                    "staged_file_ready",
                    "import_ready",
                    "import_ready",
                    1,
                    "/tmp/Deferral Series 001.cbz",
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()
        old_state_db = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            result = inkdrop_reconcile_imports.record_inkdrop_import_ready_deferral(
                "queue-deferral",
                "task-deferral",
                "source_file_incomplete_qbit_download",
                source_path="/tmp/Deferral Series 001.cbz",
                client="inkdrop_local_pack",
            )
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state_db
            inkdrop_reconcile_imports.inkdrop_state = old_module
        if not result.get("ok"):
            fail(f"import-ready deferral did not update InkDrop state: {result}")
        with sqlite3.connect(state_db) as con:
            queue = con.execute("select state, display_phase, current_source, last_event from queue_items where id='queue-deferral'").fetchone()
            task = con.execute("select status, state, lifecycle_phase, failure_reason from download_tasks where id='task-deferral'").fetchone()
        if queue[0] != "downloading" or queue[1] != "downloading" or "incomplete" not in queue[3].lower():
            fail(f"queue did not move to automatic downloading wait: {queue}")
        if task != ("waiting_for_complete_source", "downloading", "downloading", "source_file_incomplete_qbit_download"):
            fail(f"task did not move out of import-ready after deferral: {task}")


def smoke_import_ready_promotion_restores_completed_qbit_source_files():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-promotion-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        source = root / "Completed Pack 001.cbz"
        source.write_bytes(b"completed pack placeholder")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-promotion", "Promotion Series", "comic", "comicvine", "57", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-promotion", "series-promotion", "1", "001", "Issue 1", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-promotion", "series-promotion", "issue-promotion", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, display_phase, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("queue-promotion", "wanted-promotion", "series-promotion", "issue-promotion", "downloading", "download_client", "Promotion Series 1", "waiting on incomplete source", 1, now, now, "downloading", "{}"),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source, provider, protocol,
                    download_client, external_id, title, status, state, lifecycle_phase,
                    failure_reason, retry_eligible, local_path, started_at, updated_at, completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-promotion",
                    "queue-promotion",
                    "wanted-promotion",
                    "series-promotion",
                    "issue-promotion",
                    "local_pack",
                    "Local completed pack",
                    "torrent",
                    "inkdrop_local_pack",
                    "local-pack",
                    "Completed Pack 001.cbz",
                    "waiting_for_complete_source",
                    "downloading",
                    "downloading",
                    "source_file_incomplete_qbit_download",
                    1,
                    str(source),
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()

        class CompleteImporter:
            @staticmethod
            def load_qbit_incomplete_paths(kind):
                return set()

        old_state_db = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_reconcile_db = inkdrop_reconcile_imports.DB_PATH
        old_module = inkdrop_reconcile_imports.inkdrop_state
        old_imp = inkdrop_reconcile_imports.imp
        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            inkdrop_reconcile_imports.imp = CompleteImporter()
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.execute(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, lifecycle_state,
                        reason, matched_local_path, matched_series, trusted_series_id,
                        trusted_issue, inkdrop_queue_id, inkdrop_download_task_id,
                        completed_seen_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "inkdrop:queue-promotion",
                        "Promotion Series",
                        "Promotion Series 1",
                        "torrent",
                        "inkdrop_local_pack",
                        "local-pack",
                        "downloading",
                        "source_file_incomplete_qbit_download",
                        str(source),
                        "Promotion Series",
                        "comicvine:57",
                        "1",
                        "queue-promotion",
                        "task-promotion",
                        now,
                        now,
                    ),
                )
                con.commit()
            result = inkdrop_reconcile_imports.promote_complete_deferred_import_ready_records(max_records=10)
            with sqlite3.connect(reconcile_db) as con:
                reconcile = con.execute(
                    "select lifecycle_state, reason from download_reconciliation where pending_key='inkdrop:queue-promotion'"
                ).fetchone()
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state_db
            inkdrop_reconcile_imports.DB_PATH = old_reconcile_db
            inkdrop_reconcile_imports.inkdrop_state = old_module
            inkdrop_reconcile_imports.imp = old_imp

        with sqlite3.connect(state_db) as con:
            queue = con.execute("select state, display_phase, last_event from queue_items where id='queue-promotion'").fetchone()
            task = con.execute("select status, state, lifecycle_phase, failure_reason from download_tasks where id='task-promotion'").fetchone()
        if result.get("promoted") != 1 or result.get("still_incomplete") != 0:
            fail(f"completed qbit source was not promoted: {result}")
        if reconcile != ("ready_to_import", "source_file_completed_after_incomplete_wait"):
            fail(f"reconciliation row was not restored to ready_to_import: {reconcile}")
        if queue[0] != "importing" or queue[1] != "staged_or_importing" or "complete" not in queue[2].lower():
            fail(f"queue was not restored to staged importing: {queue}")
        if task != ("staged_file_ready", "import_ready", "import_ready", None):
            fail(f"task was not restored to import-ready: {task}")


def smoke_retryable_failed_staged_source_recovery_promotes_only_importable_files():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-failed-staged-recovery-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        ready_source = root / "Gantz E - Chapter 72 - Ch.72 - A Strange Coincidence.cbz"
        range_source = root / "Gantz E - Chapter 72-73.cbz"
        ready_source.write_bytes(b"ready page pack")
        range_source.write_bytes(b"real range page pack")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-gantz", "Gantz: E", "manga", "mangadex", "c37d93a8", "inkdrop_series", now, now, "{}"),
            )
            for suffix, issue_number, issue_title, source_path in (
                ("ready", "72", "A Strange Coincidence", ready_source),
                ("range", "73", "Range Still Blocked", range_source),
            ):
                con.execute(
                    """
                    insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?)
                    """,
                    (f"issue-{suffix}", "series-gantz", issue_number, issue_number, issue_title, now, now, "{}"),
                )
                con.execute(
                    """
                    insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?)
                    """,
                    (f"wanted-{suffix}", "series-gantz", f"issue-{suffix}", "missing", "in_progress", now, now, "{}"),
                )
                con.execute(
                    """
                    insert into queue_items(
                        id, wanted_id, series_id, issue_id, state, current_source, query, last_event,
                        active, created_at, updated_at, display_phase, raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"queue-{suffix}",
                        f"wanted-{suffix}",
                        "series-gantz",
                        f"issue-{suffix}",
                        "queued",
                        None,
                        f"Gantz: E Chapter {issue_number}",
                        "inkdrop_page_pack completed file was not importable (pack_candidate_requires_pack_handling); automatic retry scheduled",
                        1,
                        now,
                        now,
                        "retry_later",
                        "{}",
                    ),
                )
                con.execute(
                    """
                    insert into download_tasks(
                        id, queue_id, wanted_id, series_id, issue_id, source, provider, provider_id,
                        protocol, download_client, external_id, title, status, state, lifecycle_phase,
                        failure_reason, retry_eligible, local_path, started_at, updated_at,
                        completed_at, raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"task-{suffix}",
                        f"queue-{suffix}",
                        f"wanted-{suffix}",
                        "series-gantz",
                        f"issue-{suffix}",
                        "suwayomi",
                        "suwayomi",
                        "suwayomi",
                        "http",
                        "inkdrop_page_pack",
                        f"candidate-{suffix}",
                        source_path.name,
                        "failed_download",
                        "failed",
                        "failed_candidate",
                        "pack_candidate_requires_pack_handling",
                        1,
                        str(source_path),
                        now,
                        now,
                        now,
                        "{}",
                    ),
                )
            con.commit()

        old_state_db = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_reconcile_db = inkdrop_reconcile_imports.DB_PATH
        old_module = inkdrop_reconcile_imports.inkdrop_state
        old_classify = inkdrop_reconcile_imports.classify_inkdrop_client_file
        old_load_targets = inkdrop_reconcile_imports.imp.load_comic_targets
        old_load_imported = inkdrop_reconcile_imports.load_imported_state
        old_bad_memory = inkdrop_reconcile_imports.load_bad_archive_validation_memory

        def classify(path, row, targets, imported_state, bad_archive_memory):
            if "Ch.72" in Path(path).name:
                return {
                    "state": "ready_to_import",
                    "reason": "filename_confidence_ok",
                    "local_path": str(path),
                    "matched_series": row.get("series_title"),
                    "truth_model": "kavita_manga",
                }
            return {
                "state": "manual_review",
                "reason": "pack_candidate_requires_pack_handling",
                "local_path": str(path),
            }

        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            inkdrop_reconcile_imports.classify_inkdrop_client_file = classify
            inkdrop_reconcile_imports.imp.load_comic_targets = lambda _arg: []
            inkdrop_reconcile_imports.load_imported_state = lambda: {}
            inkdrop_reconcile_imports.load_bad_archive_validation_memory = lambda: {}
            result = inkdrop_reconcile_imports.recover_retryable_failed_staged_import_ready_records(max_records=10)
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state_db
            inkdrop_reconcile_imports.DB_PATH = old_reconcile_db
            inkdrop_reconcile_imports.inkdrop_state = old_module
            inkdrop_reconcile_imports.classify_inkdrop_client_file = old_classify
            inkdrop_reconcile_imports.imp.load_comic_targets = old_load_targets
            inkdrop_reconcile_imports.load_imported_state = old_load_imported
            inkdrop_reconcile_imports.load_bad_archive_validation_memory = old_bad_memory

        with sqlite3.connect(state_db) as con:
            ready_queue = con.execute("select state, display_phase, current_source, last_event from queue_items where id='queue-ready'").fetchone()
            ready_task = con.execute("select status, state, lifecycle_phase, failure_reason, retry_eligible, raw_json from download_tasks where id='task-ready'").fetchone()
            range_queue = con.execute("select state, display_phase from queue_items where id='queue-range'").fetchone()
            range_task = con.execute("select status, state, lifecycle_phase, failure_reason, retry_eligible from download_tasks where id='task-range'").fetchone()
        if result.get("checked") != 2 or result.get("promoted") != 1:
            fail(f"failed staged-source recovery summary was wrong: {result}")
        if result.get("skipped", {}).get("pack_candidate_requires_pack_handling") != 1:
            fail(f"non-importable staged file was not left blocked: {result}")
        if ready_queue[:3] != ("importing", "staged_or_importing", "download_client") or "importable again" not in ready_queue[3]:
            fail(f"ready staged file queue was not promoted: {ready_queue}")
        if ready_task[:5] != ("staged_file_ready", "import_ready", "import_ready", None, 0):
            fail(f"ready staged file task was not restored: {ready_task[:5]}")
        ready_raw = json.loads(ready_task[5] or "{}")
        if ready_raw.get("import_ready_promoted_reason") != "retryable_failed_staged_source_revalidated":
            fail(f"promotion raw reason missing: {ready_raw}")
        if range_queue != ("queued", "retry_later"):
            fail(f"blocked staged file queue should remain queued: {range_queue}")
        if range_task != ("failed_download", "failed", "failed_candidate", "pack_candidate_requires_pack_handling", 1):
            fail(f"blocked staged file task should remain failed: {range_task}")


def smoke_queue_only_ready_import_skips_unowned_rows():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-queue-only-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        reconcile_db = root / "imported-files.sqlite3"
        queue_source = root / "Queue Owned 001.cbz"
        legacy_source = root / "Legacy Ready 001.cbz"
        queue_source.write_bytes(b"queue")
        legacy_source.write_bytes(b"legacy")
        now = time.time()
        old_db = inkdrop_reconcile_imports.DB_PATH
        old_queue_only = inkdrop_reconcile_imports.IMPORT_READY_QUEUE_ONLY
        old_sync_ready = inkdrop_reconcile_imports.sync_inkdrop_import_ready_records
        old_sync_results = inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results
        old_recover = inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files
        old_replay = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports
        old_ready_rows = inkdrop_reconcile_imports.inkdrop_import_ready_rows
        old_imp = inkdrop_reconcile_imports.imp

        class NoLegacyPrevalidateImporter:
            @staticmethod
            def validate_comic_archive(path):
                raise AssertionError(f"queue-only import validated an unowned legacy row: {path}")

        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.IMPORT_READY_QUEUE_ONLY = True
            inkdrop_reconcile_imports.sync_inkdrop_import_ready_records = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = lambda *args, **kwargs: {"ok": True}
            inkdrop_reconcile_imports.inkdrop_import_ready_rows = lambda limit=300: [{"queue_id": "queue-owned"}]
            inkdrop_reconcile_imports.imp = NoLegacyPrevalidateImporter()
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            with sqlite3.connect(reconcile_db) as con:
                con.execute("create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
                rows = [
                    (
                        "legacy:ready",
                        "Legacy Ready",
                        "Legacy Ready 1",
                        "torrent",
                        "qbit",
                        "hash-legacy",
                        "ready_to_import",
                        "legacy_ready",
                        str(legacy_source),
                        "Legacy Ready",
                        "comicvine:legacy",
                        "1",
                        None,
                        None,
                        now + 10,
                        now + 10,
                    ),
                    (
                        "inkdrop:queue-owned",
                        "Queue Owned",
                        "Queue Owned 1",
                        "torrent",
                        "qbit",
                        "hash-queue",
                        "ready_to_import",
                        "download_client_ready",
                        str(queue_source),
                        "Queue Owned",
                        "comicvine:queue",
                        "1",
                        "queue-owned",
                        "task-queue",
                        now,
                        now,
                    ),
                ]
                con.executemany(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, lifecycle_state,
                        reason, matched_local_path, matched_series, trusted_series_id,
                        trusted_issue, inkdrop_queue_id, inkdrop_download_task_id,
                        completed_seen_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )
                con.commit()
            records = inkdrop_reconcile_imports.ready_import_records(3)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.IMPORT_READY_QUEUE_ONLY = old_queue_only
            inkdrop_reconcile_imports.sync_inkdrop_import_ready_records = old_sync_ready
            inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = old_sync_results
            inkdrop_reconcile_imports.recover_import_ready_timeouts_from_imported_files = old_recover
            inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = old_replay
            inkdrop_reconcile_imports.inkdrop_import_ready_rows = old_ready_rows
            inkdrop_reconcile_imports.imp = old_imp

        if len(records) != 1 or records[0].get("source_file") != str(queue_source):
            fail(f"queue-only import selected unexpected ready rows: {records}")


def smoke_completed_pack_download_client_rows_are_import_ready():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-pack-row-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        source = root / "Absolute Wonder Woman 002 (2025) (Digital).cbz"
        source.write_bytes(b"queue-backed pack file")
        now = time.time()
        old_state_db = inkdrop_reconcile_imports.INKDROP_STATE_DB
        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            with inkdrop_state.connect(state_db) as con:
                inkdrop_state.init_schema(con)
                con.execute(
                    """
                    insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?,?)
                    """,
                    ("series-pack", "Absolute Wonder Woman", "comic", "comicvine", "123", "inkdrop_series", now, now, "{}"),
                )
                con.execute(
                    """
                    insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?)
                    """,
                    ("issue-pack", "series-pack", "2", "2", "Issue 2", now, now, "{}"),
                )
                con.execute(
                    """
                    insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?)
                    """,
                    ("wanted-pack", "series-pack", "issue-pack", "missing", "in_progress", now, now, "{}"),
                )
                con.execute(
                    """
                    insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "queue-pack",
                        "wanted-pack",
                        "series-pack",
                        "issue-pack",
                        "importing",
                        "download_client",
                        "Absolute Wonder Woman 2 2025",
                        "completed pack contains this wanted issue",
                        1,
                        now,
                        now,
                        "{}",
                    ),
                )
                con.execute(
                    """
                    insert into download_tasks(
                        id, queue_id, wanted_id, series_id, issue_id, source, provider, protocol,
                        download_client, title, status, state, local_path, started_at, updated_at,
                        completed_at, raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "task-pack",
                        "queue-pack",
                        "wanted-pack",
                        "series-pack",
                        "issue-pack",
                        "download_client",
                        "DC.Comics.2024",
                        "torrent",
                        "qBittorrent",
                        "DC Comics 2024",
                        "completed_in_client",
                        "import_ready",
                        str(source),
                        now,
                        now,
                        now,
                        "{}",
                    ),
                )
                con.commit()
            rows = inkdrop_reconcile_imports.inkdrop_import_ready_rows(10)
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state_db

        if len(rows) != 1:
            fail(f"completed pack download-client row was not import-ready: {rows}")
        row = rows[0]
        if row.get("queue_id") != "queue-pack" or row.get("local_path") != str(source):
            fail(f"completed pack import-ready row did not preserve identity/path: {row}")


def smoke_completed_slskd_staged_source_rows_are_import_ready():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-completed-import-ready-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        source = root / "Gantz E - Chapter 77 - Ch.77.cbz"
        source.write_bytes(b"source page pack")
        active_source = root / "Gantz E - Chapter 78 - Ch.78.cbz"
        active_source.write_bytes(b"active transfer")
        downloading_source = root / "Gantz E - Chapter 79 - Ch.79.cbz"
        downloading_source.write_bytes(b"downloading transfer")
        state_db = root / "inkdrop-state.sqlite3"
        now = time.time()
        old_state_db = inkdrop_reconcile_imports.INKDROP_STATE_DB
        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            with inkdrop_state.connect(state_db) as con:
                inkdrop_state.init_schema(con)
                con.execute(
                    """
                    insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?,?)
                    """,
                    ("series-source-ready", "Gantz: E", "manga", "mangadex", "gantz-e", "inkdrop_series", now, now, "{}"),
                )
                con.execute(
                    """
                    insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?)
                    """,
                    ("issue-source-ready", "series-source-ready", "77", "077", "Resurrection from Death", now, now, "{}"),
                )
                con.execute(
                    """
                    insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?)
                    """,
                    ("wanted-source-ready", "series-source-ready", "issue-source-ready", "missing", "in_progress", now, now, "{}"),
                )
                con.execute(
                    """
                    insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "queue-source-ready",
                        "wanted-source-ready",
                        "series-source-ready",
                        "issue-source-ready",
                        "importing",
                        "slskd",
                        "Gantz: E Chapter 77",
                        "Download complete; waiting for import",
                        1,
                        now,
                        now,
                        "{}",
                    ),
                )
                con.execute(
                    """
                    insert into download_tasks(
                        id, queue_id, wanted_id, series_id, issue_id, source, provider, protocol,
                        download_client, title, status, state, local_path, started_at, updated_at,
                        completed_at, raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "task-source-ready",
                        "queue-source-ready",
                        "wanted-source-ready",
                        "series-source-ready",
                        "issue-source-ready",
                        "slskd",
                        "SLSKD",
                        "slskd",
                        "slskd",
                        "Gantz: E - Chapter 77 - Ch.77",
                        "staged_file_ready",
                        "import_ready",
                        str(source),
                        now,
                        now,
                        now,
                        "{}",
                    ),
                )
                for number in ("78", "79"):
                    con.execute(
                        """
                        insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                        values(?,?,?,?,?,?,?,?)
                        """,
                        (
                            f"issue-source-{number}", "series-source-ready", number, number,
                            f"Chapter {number}", now, now, "{}",
                        ),
                    )
                    con.execute(
                        """
                        insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                        values(?,?,?,?,?,?,?,?)
                        """,
                        (
                            f"wanted-source-{number}", "series-source-ready", f"issue-source-{number}",
                            "missing", "in_progress", now, now, "{}",
                        ),
                    )
                    con.execute(
                        """
                        insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                        values(?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            f"queue-source-{number}", f"wanted-source-{number}", "series-source-ready",
                            f"issue-source-{number}", "importing", "slskd", f"Gantz: E Chapter {number}",
                            "SLSKD transfer is still active", 1, now, now, "{}",
                        ),
                    )
                for number, state, path in (
                    ("78", "active", active_source),
                    ("79", "downloading", downloading_source),
                ):
                    con.execute(
                        """
                        insert into download_tasks(
                            id, queue_id, wanted_id, series_id, issue_id, source, provider, protocol,
                            download_client, title, status, state, local_path, started_at, updated_at, raw_json
                        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            f"task-source-{number}", f"queue-source-{number}", f"wanted-source-{number}",
                            "series-source-ready", f"issue-source-{number}", "slskd", "SLSKD", "slskd",
                            "slskd", f"Gantz: E - Chapter {number}", "ready_to_import", state,
                            str(path), now, now, "{}",
                        ),
                    )
                con.commit()
            rows = inkdrop_reconcile_imports.inkdrop_import_ready_rows(10)
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state_db
        if len(rows) != 1:
            fail(f"completed SLSKD staged source row was not import-ready: {rows}")
        row = rows[0]
        if row.get("queue_id") != "queue-source-ready" or row.get("local_path") != str(source):
            fail(f"completed SLSKD staged source import-ready row did not preserve identity/path: {row}")
        if row.get("download_client") != "slskd" or row.get("external_id") is not None:
            fail(f"SLSKD import-ready row fabricated or lost transfer identity: {row}")
        if row.get("download_task_id") != "task-source-ready":
            fail(f"active/downloading SLSKD task leaked into import-ready selection: {row}")
        if inkdrop_reconcile_imports.import_ready_client_priority("slskd") != 0:
            fail("ready SLSKD artifacts must drain with other staged sources")


def smoke_local_completed_pack_replay_creates_import_ready_row():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    with tempfile.TemporaryDirectory(prefix="inkdrop-local-pack-replay-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        downloads_root = root / "Downloads" / "comics"
        pack_root = downloads_root / "2026.06.24 Weekly Pack"
        archive = pack_root / "2026.06.24 DC Week" / "Absolute Superman 020 (2026) (Digital).cbz"
        imported_archive = pack_root / "2026.06.24 DC Week" / "Absolute Wonder Woman 021 (2026) (Digital).cbz"
        imported_dest = root / "Comics" / "Absolute Wonder Woman" / "Absolute Wonder Woman #021 (2026).cbz"
        archive.parent.mkdir(parents=True)
        imported_dest.parent.mkdir(parents=True)
        archive.write_bytes(b"absolute superman")
        imported_archive.write_bytes(b"absolute wonder woman")
        imported_dest.write_bytes(b"absolute wonder woman imported")
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        now = time.time()
        with sqlite3.connect(reconcile_db) as con:
            con.execute("create table imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
            con.execute(
                "insert into imported_files values(?,?,?,?,?)",
                ("sha-local-pack-imported", str(imported_archive), str(imported_dest), imported_dest.stat().st_size, now),
            )
            con.commit()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            for series_id, title, issue_id, wanted_id, queue_id, issue_number, normalized, query in (
                (
                    "series-local-pack",
                    "Absolute Superman",
                    "issue-local-pack",
                    "wanted-local-pack",
                    "queue-local-pack",
                    "20",
                    "020",
                    "Absolute Superman 20 2026",
                ),
                (
                    "series-local-pack-imported",
                    "Absolute Wonder Woman",
                    "issue-local-pack-imported",
                    "wanted-local-pack-imported",
                    "queue-local-pack-imported",
                    "21",
                    "021",
                    "Absolute Wonder Woman 21 2026",
                ),
            ):
                con.execute(
                    """
                    insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?,?)
                    """,
                    (series_id, title, "comic", "comicvine", series_id, "inkdrop_series", now, now, "{}"),
                )
                con.execute(
                    """
                    insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?)
                    """,
                    (issue_id, series_id, issue_number, normalized, f"Issue {issue_number}", now, now, "{}"),
                )
                con.execute(
                    """
                    insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?)
                    """,
                    (wanted_id, series_id, issue_id, "missing", "wanted", now, now, "{}"),
                )
                con.execute(
                    """
                    insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                    values(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (queue_id, wanted_id, series_id, issue_id, "queued", None, query, "source ladder found no candidate", 1, now, now, "{}"),
                )
            con.commit()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state_db = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_roots = inkdrop_reconcile_imports.COMIC_LOCAL_ROOTS
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.COMIC_LOCAL_ROOTS = [downloads_root]
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            result = inkdrop_reconcile_imports.fanout_local_completed_packs_to_inkdrop(
                max_roots=5,
                max_rows=20,
                max_created=10,
            )
            rows = inkdrop_reconcile_imports.inkdrop_import_ready_rows(10)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state_db
            inkdrop_reconcile_imports.COMIC_LOCAL_ROOTS = old_roots
            inkdrop_reconcile_imports.inkdrop_state = old_module

        if result.get("created") != 1 or result.get("updated") != 1 or result.get("matched") != 2:
            fail(f"local completed pack replay did not create/import expected children: {result}")
        with sqlite3.connect(state_db) as con:
            queue = con.execute(
                "select state, current_source, last_event from queue_items where id='queue-local-pack'"
            ).fetchone()
            if queue[0] != "importing" or queue[1] != "download_client" or "local completed pack" not in queue[2]:
                fail(f"local pack replay did not move queue to importing: {queue}")
            task = con.execute(
                "select source, provider_id, download_client, status, state, local_path from download_tasks where queue_id='queue-local-pack'"
            ).fetchone()
            expected_task = ("local_pack", "local_pack", "inkdrop_local_pack", "staged_file_ready", "import_ready", str(archive))
            if task != expected_task:
                fail(f"local pack replay wrote unexpected download task: {task}")
            imported_queue = con.execute(
                "select state, current_source, last_event from queue_items where id='queue-local-pack-imported'"
            ).fetchone()
            if (
                imported_queue[0] != "verified"
                or imported_queue[1] != "local_pack"
                or "managed folder" not in imported_queue[2]
            ):
                fail(f"already-imported local pack proof did not settle from folder truth: {imported_queue}")
            imported_result = con.execute(
                "select source_path, dest_path, status, verified from import_results where queue_id='queue-local-pack-imported'"
            ).fetchone()
            if imported_result != (str(imported_archive), str(imported_dest), "folder_verified", 1):
                fail(f"already-imported local pack proof did not record import_result: {imported_result}")
        if len(rows) != 1 or rows[0].get("queue_id") != "queue-local-pack" or rows[0].get("local_path") != str(archive):
            fail(f"local completed pack task is not visible to import-ready rows: {rows}")


def smoke_local_completed_pack_replay_defers_qbit_incomplete_archive():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    with tempfile.TemporaryDirectory(prefix="inkdrop-local-pack-incomplete-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        downloads_root = root / "Downloads" / "comics"
        pack_root = downloads_root / "2026.06.24 Weekly Pack"
        archive = pack_root / "2026.06.24 DC Week" / "Absolute Batman 020 (2026) (Digital).cbz"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"incomplete absolute batman")
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        now = time.time()
        with sqlite3.connect(reconcile_db) as con:
            con.execute("create table imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
            con.commit()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-local-pack-incomplete", "Absolute Batman", "comic", "comicvine", "series-local-pack-incomplete", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-local-pack-incomplete", "series-local-pack-incomplete", "20", "020", "Issue 20", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-local-pack-incomplete", "series-local-pack-incomplete", "issue-local-pack-incomplete", "missing", "wanted", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("queue-local-pack-incomplete", "wanted-local-pack-incomplete", "series-local-pack-incomplete", "issue-local-pack-incomplete", "queued", None, "Absolute Batman 20 2026", "source ladder found no candidate", 1, now, now, "{}"),
            )
            con.commit()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state_db = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_roots = inkdrop_reconcile_imports.COMIC_LOCAL_ROOTS
        old_module = inkdrop_reconcile_imports.inkdrop_state
        old_imp = inkdrop_reconcile_imports.imp

        class IncompleteImporter:
            def load_qbit_incomplete_paths(self, kind):
                return {str(archive)} if kind == "comics" else set()

            def __getattr__(self, name):
                return getattr(old_imp, name)

        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.COMIC_LOCAL_ROOTS = [downloads_root]
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            inkdrop_reconcile_imports.imp = IncompleteImporter()
            result = inkdrop_reconcile_imports.fanout_local_completed_packs_to_inkdrop(
                max_roots=5,
                max_rows=20,
                max_created=10,
            )
            rows = inkdrop_reconcile_imports.inkdrop_import_ready_rows(10)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state_db
            inkdrop_reconcile_imports.COMIC_LOCAL_ROOTS = old_roots
            inkdrop_reconcile_imports.inkdrop_state = old_module
            inkdrop_reconcile_imports.imp = old_imp

        if result.get("created") != 0 or result.get("deferred") != 1 or result.get("matched") != 1:
            fail(f"local pack incomplete qbit replay did not defer instead of stage: {result}")
        if rows:
            fail(f"incomplete local pack archive leaked into import-ready rows: {rows}")
        with sqlite3.connect(state_db) as con:
            queue = con.execute(
                "select state, display_phase, current_source, last_event from queue_items where id='queue-local-pack-incomplete'"
            ).fetchone()
            task = con.execute(
                """
                select source, provider_id, download_client, status, state, lifecycle_phase, failure_reason, local_path
                from download_tasks
                where queue_id='queue-local-pack-incomplete'
                """
            ).fetchone()
            source_attempt = con.execute(
                "select status, lifecycle_phase, failure_reason from source_attempts where queue_id='queue-local-pack-incomplete'"
            ).fetchone()
        if queue[0] != "downloading" or queue[1] != "downloading" or "incomplete" not in queue[3].lower():
            fail(f"incomplete local pack replay did not move queue to automatic wait: {queue}")
        expected_task = (
            "local_pack",
            "local_pack",
            "inkdrop_local_pack",
            "waiting_for_complete_source",
            "downloading",
            "downloading",
            "source_file_incomplete_qbit_download",
            str(archive),
        )
        if task != expected_task:
            fail(f"incomplete local pack replay wrote unexpected task: {task}")
        if source_attempt != ("waiting_for_complete_source", "downloading", "source_file_incomplete_qbit_download"):
            fail(f"incomplete local pack replay wrote unexpected source attempt: {source_attempt}")


def smoke_import_ready_classifier_accepts_cached_string_paths():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    with tempfile.TemporaryDirectory(prefix="inkdrop-import-ready-string-path-", ignore_cleanup_errors=True) as tmp:
        source = Path(tmp) / "Queue Backed 001.cbz"
        source.write_bytes(b"queue")
        old_classify = inkdrop_reconcile_imports.classify_local_file

        def fake_classify_local_file(path, *args, **kwargs):
            if not isinstance(path, Path):
                fail(f"cached import-ready path was not normalized to Path: {type(path)!r}")
            return {"state": "ready_to_import", "reason": "download_client_ready"}

        try:
            inkdrop_reconcile_imports.classify_local_file = fake_classify_local_file
            detail = inkdrop_reconcile_imports.classify_inkdrop_client_file(
                str(source),
                {"series_id": "", "issue_number": "1", "normalized_number": "001"},
                [],
                {"source_paths": set(), "dest_paths": set(), "hash_sizes": set(), "hashes": set()},
                {},
            )
        finally:
            inkdrop_reconcile_imports.classify_local_file = old_classify

        if detail.get("state") != "ready_to_import" or detail.get("local_path") != str(source):
            fail(f"cached string path classifier returned unexpected detail: {detail}")


def smoke_import_ready_timeout_continues():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    old_ready = inkdrop_reconcile_imports.ready_import_records
    old_run = inkdrop_reconcile_imports.subprocess.run
    old_mark_timeout = inkdrop_reconcile_imports.mark_reconciled_import_timeout
    old_record_timeout = inkdrop_reconcile_imports.record_inkdrop_import_timeout
    old_mark_attempt = inkdrop_reconcile_imports.mark_reconciled_import_attempt
    old_record_attempt = inkdrop_reconcile_imports.record_inkdrop_import_attempt
    old_sync_results = inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results
    old_sync_replay = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports
    old_recover_active = inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files
    old_claim = inkdrop_reconcile_imports.claim_inkdrop_import_attempt
    calls = []
    timeouts = []
    try:
        inkdrop_reconcile_imports.ready_import_records = lambda max_files: [
            {"pending_key": "slow", "source_file": "/tmp/slow.cbz", "inkdrop_queue_id": "queue-slow", "client": "sab"},
            {"pending_key": "next", "source_file": "/tmp/next.cbz", "inkdrop_queue_id": "queue-next", "client": "sab"},
        ]

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            timeouts.append(kwargs.get("timeout"))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, 1, output="slow out", stderr="slow err")
            return types.SimpleNamespace(
                returncode=0,
                stdout='{"imported":[{"source":"/tmp/next.cbz","dest":"/tmp/next.cbz"}],"verification":{"checked":[]}}',
                stderr="",
            )

        inkdrop_reconcile_imports.subprocess.run = fake_run
        inkdrop_reconcile_imports.mark_reconciled_import_timeout = lambda record, exc, timeout_seconds=None: {
            "state": "failed_import",
            "reason": "import_ready_import_timeout",
            "timeout_seconds": timeout_seconds,
        }
        inkdrop_reconcile_imports.record_inkdrop_import_timeout = lambda record, exc, timeout_seconds=None: {
            "ok": True,
            "reason": "retry_scheduled",
            "timeout_seconds": timeout_seconds,
        }
        inkdrop_reconcile_imports.mark_reconciled_import_attempt = lambda record, parsed, returncode=0: {"state": "imported"}
        inkdrop_reconcile_imports.record_inkdrop_import_attempt = lambda record, parsed, returncode=0: {"ok": True}
        inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = lambda: {"ok": True, "updated": 0}
        inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = lambda: {"ok": True, "updated": 0}
        inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files = lambda *args, **kwargs: {"ok": True, "checked": 0}
        inkdrop_reconcile_imports.claim_inkdrop_import_attempt = lambda record: {
            "ok": True,
            "authority": {"queue_id": record.get("inkdrop_queue_id"), "token": "timeout-smoke"},
        }
        result = inkdrop_reconcile_imports.import_ready(2)
    finally:
        inkdrop_reconcile_imports.ready_import_records = old_ready
        inkdrop_reconcile_imports.subprocess.run = old_run
        inkdrop_reconcile_imports.mark_reconciled_import_timeout = old_mark_timeout
        inkdrop_reconcile_imports.record_inkdrop_import_timeout = old_record_timeout
        inkdrop_reconcile_imports.mark_reconciled_import_attempt = old_mark_attempt
        inkdrop_reconcile_imports.record_inkdrop_import_attempt = old_record_attempt
        inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = old_sync_results
        inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = old_sync_replay
        inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files = old_recover_active
        inkdrop_reconcile_imports.claim_inkdrop_import_attempt = old_claim
    results = result.get("results") or []
    if len(results) != 2:
        fail(f"timeout import-ready batch did not continue to the next file: {result}")
    if not results[0].get("timeout") or results[0].get("returncode") != 124:
        fail(f"first result did not record timeout: {results[0]}")
    if results[1].get("returncode") != 0:
        fail(f"second import-ready file was not processed after timeout: {results[1]}")
    if len(timeouts) != 2 or not all(isinstance(value, int) and value >= 30 for value in timeouts):
        fail(f"import-ready subprocess calls did not receive bounded timeouts: {timeouts}")
    if "batch_timeout_seconds" not in result or "elapsed_seconds" not in result:
        fail(f"import-ready result does not report batch budget fields: {result}")
    if result.get("batch_budget_exhausted"):
        fail(f"import-ready batch unexpectedly exhausted budget during quick timeout smoke: {result}")


def smoke_import_ready_uses_planned_path_by_default_with_opt_out():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    old_ready = inkdrop_reconcile_imports.ready_import_records
    old_run = inkdrop_reconcile_imports.subprocess.run
    old_mark_attempt = inkdrop_reconcile_imports.mark_reconciled_import_attempt
    old_record_attempt = inkdrop_reconcile_imports.record_inkdrop_import_attempt
    old_sync_results = inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results
    old_sync_replay = inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports
    old_recover_active = inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files
    old_claim = inkdrop_reconcile_imports.claim_inkdrop_import_attempt
    old_apply = inkdrop_reconcile_imports.INKDROP_IMPORT_READY_APPLY_PLANNED_PATH
    calls = []
    try:
        inkdrop_reconcile_imports.ready_import_records = lambda max_files: [
            {
                "pending_key": "planned",
                "source_file": "/downloads/comics/2026.06.24 Weekly Pack/Absolute Wonder Woman 021.cbz",
                "inkdrop_queue_id": "queue-planned",
                "trusted_series_id": "comicvine:160511",
                "trusted_issue": "21",
                "client": "sabnzbd",
            }
        ]

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return types.SimpleNamespace(
                returncode=0,
                stdout='{"imported":[{"source":"/downloads/comics/2026.06.24 Weekly Pack/Absolute Wonder Woman 021.cbz","dest":"/library/Absolute Wonder Woman #021.cbz"}],"verification":{"checked":[]}}',
                stderr="",
            )

        inkdrop_reconcile_imports.subprocess.run = fake_run
        inkdrop_reconcile_imports.mark_reconciled_import_attempt = lambda record, parsed, returncode=0: {"state": "imported"}
        inkdrop_reconcile_imports.record_inkdrop_import_attempt = lambda record, parsed, returncode=0: {"ok": True}
        inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = lambda: {"ok": True, "updated": 0}
        inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = lambda: {"ok": True, "updated": 0}
        inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files = lambda *args, **kwargs: {"ok": True, "checked": 0}
        inkdrop_reconcile_imports.claim_inkdrop_import_attempt = lambda record: {
            "ok": True,
            "authority": {"queue_id": record.get("inkdrop_queue_id"), "token": "planned-path-smoke"},
        }
        inkdrop_reconcile_imports.INKDROP_IMPORT_READY_APPLY_PLANNED_PATH = True
        inkdrop_reconcile_imports.import_ready(1)
        inkdrop_reconcile_imports.INKDROP_IMPORT_READY_APPLY_PLANNED_PATH = False
        inkdrop_reconcile_imports.import_ready(1)
    finally:
        inkdrop_reconcile_imports.ready_import_records = old_ready
        inkdrop_reconcile_imports.subprocess.run = old_run
        inkdrop_reconcile_imports.mark_reconciled_import_attempt = old_mark_attempt
        inkdrop_reconcile_imports.record_inkdrop_import_attempt = old_record_attempt
        inkdrop_reconcile_imports.sync_reconciliation_from_inkdrop_import_results = old_sync_results
        inkdrop_reconcile_imports.sync_inkdrop_from_reconciled_imports = old_sync_replay
        inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files = old_recover_active
        inkdrop_reconcile_imports.claim_inkdrop_import_attempt = old_claim
        inkdrop_reconcile_imports.INKDROP_IMPORT_READY_APPLY_PLANNED_PATH = old_apply
    if len(calls) != 2:
        fail(f"expected two import-ready child calls, found {len(calls)}: {calls}")
    if "--apply-planned-path" not in calls[0]:
        fail(f"import-ready planned path flag should default on: {calls[0]}")
    if "--apply-planned-path" in calls[1]:
        fail(f"import-ready planned path opt-out was ignored: {calls[1]}")
    if "--trusted-series-id" not in calls[0] or "--trusted-issue" not in calls[0]:
        fail(f"planned-path import-ready command lost trusted target guards: {calls[0]}")


def smoke_import_ready_child_defers_broad_import_status_sync():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
        import inkdrop_completed_import
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    env = inkdrop_reconcile_imports.import_ready_child_env()
    if env.get("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE") != "defer":
        fail(f"import-ready child env does not defer broad import-status sync: {env}")
    reconcile_source = Path(inkdrop_reconcile_imports.__file__).read_text(encoding="utf-8")
    importer_source = Path(inkdrop_completed_import.__file__).read_text(encoding="utf-8")
    if "--no-wait-for-library-scan" not in reconcile_source:
        fail("import-ready child command should use the library-neutral scan flag")
    if '"library_scan_tasks": library_scan_tasks' not in importer_source:
        fail("completed importer should expose adapter-neutral library_scan_tasks")
    if "InkDrop managed reading libraries" not in importer_source:
        fail("completed importer CLI description should be library-neutral")
    if not inkdrop_completed_import.no_wait_for_library_scan_flag_present(["--no-wait-for-library-scan"]):
        fail("completed importer did not recognize --no-wait-for-library-scan")
    if not inkdrop_completed_import.no_wait_for_library_scan_flag_present(["--no-wait-for-kavita-scan"]):
        fail("completed importer did not preserve --no-wait-for-kavita-scan compatibility")

    with tempfile.TemporaryDirectory(prefix="inkdrop-import-status-defer-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_state_dir = inkdrop_completed_import.STATE_DIR
        old_status = inkdrop_completed_import.IMPORT_STATUS_PATH
        old_log = inkdrop_completed_import.LOG_PATH
        old_sync = inkdrop_completed_import.sync_inkdrop_import_results
        old_atomic_write = inkdrop_completed_import.write_json_atomic
        old_env = os.environ.get("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE")
        old_argv = inkdrop_completed_import.sys.argv[:]
        try:
            inkdrop_completed_import.STATE_DIR = root
            inkdrop_completed_import.IMPORT_STATUS_PATH = root / "import-status.json"
            inkdrop_completed_import.LOG_PATH = root / "kavita-import.log"
            inkdrop_completed_import.sync_inkdrop_import_results = lambda: fail(
                "deferred import-ready status write should not run broad InkDrop import sync"
            )
            os.environ["INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE"] = "defer"
            result = inkdrop_completed_import.write_import_status({"kind": "comics", "imported_count": 1})
            os.environ.pop("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE", None)
            inkdrop_completed_import.sys.argv = [
                "inkdrop_completed_import.py",
                "--kind",
                "comics",
                "--source-file",
                str(root / "Absolute Batman 001.cbz"),
                "--trusted-series-id",
                "comicvine:1",
                "--no-wait-for-kavita-scan",
            ]
            legacy_shape_result = inkdrop_completed_import.write_import_status({"kind": "comics", "imported_count": 1})
            inkdrop_completed_import.sys.argv = [
                "inkdrop_completed_import.py",
                "--kind",
                "comics",
                "--source-file",
                str(root / "Absolute Batman 002.cbz"),
                "--trusted-series-id",
                "comicvine:1",
                "--no-wait-for-library-scan",
            ]
            shape_result = inkdrop_completed_import.write_import_status({"kind": "comics", "imported_count": 1})
            def locked_legacy_status(path, payload):
                if Path(path) == inkdrop_completed_import.IMPORT_STATUS_PATH:
                    raise PermissionError("shared compatibility status is locked")
                return old_atomic_write(path, payload)
            inkdrop_completed_import.write_json_atomic = locked_legacy_status
            locked_result = inkdrop_completed_import.write_import_status({"kind": "comics", "imported_count": 1})
            event_count = len(list((root / "import-status-events").glob("*.json")))
        finally:
            if old_env is None:
                os.environ.pop("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE", None)
            else:
                os.environ["INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE"] = old_env
            inkdrop_completed_import.sys.argv = old_argv
            inkdrop_completed_import.STATE_DIR = old_state_dir
            inkdrop_completed_import.IMPORT_STATUS_PATH = old_status
            inkdrop_completed_import.LOG_PATH = old_log
            inkdrop_completed_import.sync_inkdrop_import_results = old_sync
            inkdrop_completed_import.write_json_atomic = old_atomic_write
    if not result.get("deferred") or result.get("reason") != "import_status_sync_deferred":
        fail(f"write_import_status did not report deferred sync: {result}")
    if not legacy_shape_result.get("deferred") or legacy_shape_result.get("reason") != "import_status_sync_deferred":
        fail(f"legacy queue-backed source-file child did not defer broad status sync without env: {legacy_shape_result}")
    if not shape_result.get("deferred") or shape_result.get("reason") != "import_status_sync_deferred":
        fail(f"library-neutral queue-backed source-file child did not defer broad status sync without env: {shape_result}")
    if not locked_result.get("deferred") or locked_result.get("legacy_status_written") is not False:
        fail(f"locked compatibility status incorrectly failed a durable import event: {locked_result}")
    if event_count != 4:
        fail(f"deferred import statuses were not preserved independently: {event_count}")


def smoke_deferred_import_statuses_are_lossless():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    import inkdrop_completed_import

    with tempfile.TemporaryDirectory(prefix="inkdrop-lossless-import-status-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db_path = root / "inkdrop-state.sqlite3"
        comic_root = root / "Comics"
        destinations = [comic_root / "Snotgirl (2016)" / f"Snotgirl #{number:03d} (2016).cbz" for number in (1, 3)]
        sources = [root / "slskd" / f"Snotgirl {number:03d} (2016).cbz" for number in (1, 3)]
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(destination, "w") as archive:
                archive.writestr("001.jpg", b"validated page")
        now = time.time()
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                "insert into app_settings(key,scope,label,value_json,description,source,updated_at) values(?,?,?,?,?,?,?)",
                ("media_management.comic_root", "media_management", "Comic Root", json.dumps(str(comic_root)), "Smoke root", "smoke", now),
            )
            con.execute(
                "insert into series(id,title,sort_title,media_type,metadata_provider,metadata_id,source,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
                ("comicvine:92387", "Snotgirl", "snotgirl", "comic", "comicvine", "92387", "inkdrop_series", now, now, "{}"),
            )
            for number in (1, 3):
                issue_id = f"comicvine:92387:issue:{number}"
                wanted_id = f"wanted:{number}"
                queue_id = f"queue:{number}"
                con.execute(
                    "insert into issues(id,series_id,issue_number,normalized_number,title,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
                    (issue_id, "comicvine:92387", str(number), str(number), f"Issue {number}", now, now, "{}"),
                )
                con.execute(
                    "insert into wanted_items(id,series_id,issue_id,reason,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
                    (wanted_id, "comicvine:92387", issue_id, "missing", "in_progress", now, now, "{}"),
                )
                con.execute(
                    "insert into queue_items(id,wanted_id,series_id,issue_id,state,current_source,query,last_event,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (queue_id, wanted_id, "comicvine:92387", issue_id, "importing", "slskd", f"Snotgirl {number:03d}", "waiting for import proof", 1, now, now, "{}"),
                )
            con.execute(
                "insert into import_results(id,queue_id,series_id,issue_id,source_path,dest_path,status,verified,created_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
                ("legacy-import-id", "queue:1", "comicvine:92387", "comicvine:92387:issue:1", str(sources[0]), str(destinations[0]), "pending", 0, now, json.dumps({"sha256": "sha-1"})),
            )
            con.execute(
                "insert into import_results(id,queue_id,series_id,issue_id,source_path,dest_path,status,verified,created_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
                ("legacy-missing-sha", "queue:3", "comicvine:92387", "comicvine:92387:issue:3", str(sources[1]), str(destinations[1]), "pending", 0, now, "{}"),
            )
            con.commit()

        old_state_dir = inkdrop_completed_import.STATE_DIR
        old_status_path = inkdrop_completed_import.IMPORT_STATUS_PATH
        old_log_path = inkdrop_completed_import.LOG_PATH
        old_env = os.environ.get("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE")
        statuses = []
        try:
            inkdrop_completed_import.STATE_DIR = root
            inkdrop_completed_import.IMPORT_STATUS_PATH = root / "import-status.json"
            inkdrop_completed_import.LOG_PATH = root / "inkdrop-import.log"
            os.environ["INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE"] = "defer"
            event_dir = root / "import-status-events"
            event_dir.mkdir(parents=True, exist_ok=True)
            for index in range(1001):
                (event_dir / f"0000-poison-{index:04d}.json").write_text("not json", encoding="utf-8")
            with (event_dir / "0000-oversized.json").open("wb") as handle:
                handle.seek(64 * 1024 * 1024)
                handle.write(b"x")
            for number, source, destination in zip((1, 3), sources, destinations):
                status = {
                    "kind": "comics",
                    "imported_count": 1,
                    "skipped_count": 0,
                    "imported": [{
                        "source": str(source),
                        "dest": str(destination),
                        "sha256": f"sha-{number}",
                        "matched_series": "Snotgirl",
                        "comicvine_id": "92387",
                        "canonical_issue_number": f"{number:03d}",
                    }],
                    "verification": {
                        "failure_count": 0,
                        "checked": [{"dest": str(destination), "verification_status": "folder_verified", "host_exists": True}],
                        "checked_count": 1,
                        "folder_verified_count": 1,
                    },
                }
                statuses.append(status)
                result = inkdrop_completed_import.write_import_status(status)
                if not result.get("deferred"):
                    fail(f"fixture status was not deferred: {result}")
        finally:
            if old_env is None:
                os.environ.pop("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE", None)
            else:
                os.environ["INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE"] = old_env
            inkdrop_completed_import.STATE_DIR = old_state_dir
            inkdrop_completed_import.IMPORT_STATUS_PATH = old_status_path
            inkdrop_completed_import.LOG_PATH = old_log_path

        summary = inkdrop_state.sync_import_results(root, db_path)
        retry_payload = {**statuses[0], "updated_at": time.time() + 10}
        retry_event = root / "import-status-events" / "retry-same-import.json"
        retry_event.write_text(json.dumps(retry_payload), encoding="utf-8")
        (root / "import-status.json").write_text(json.dumps(retry_payload), encoding="utf-8")
        retry_summary = inkdrop_state.sync_import_results(root, db_path)
        idle_summary = inkdrop_state.sync_import_results(root, db_path)
        full_summary = inkdrop_state.sync_state(root, db_path)
        with inkdrop_state.connect_read(db_path) as con:
            import_count = con.execute("select count(*) from import_results where verified=1").fetchone()[0]
            total_import_count = con.execute("select count(*) from import_results").fetchone()[0]
            matching_sha_status = con.execute("select status from import_results where id='legacy-import-id'").fetchone()[0]
            missing_sha_status = con.execute("select status from import_results where id='legacy-missing-sha'").fetchone()[0]
            media_count = con.execute("select count(*) from media_files where active=1").fetchone()[0]
            wanted = [row[0] for row in con.execute("select status from wanted_items order by id").fetchall()]
        remaining_events = list((root / "import-status-events").glob("*.json"))
        if import_count != 2 or media_count != 2 or wanted != ["satisfied", "satisfied"]:
            fail(f"deferred import statuses were lost: imports={import_count} media={media_count} wanted={wanted} summary={summary.get('synced')}")
        if remaining_events or summary.get("synced", {}).get("import_status_events") != 2:
            fail(f"committed import status events were not retired exactly once: {remaining_events} {summary.get('synced')}")
        if summary.get("synced", {}).get("rejected_import_status_events") != 1002:
            fail(f"poisoned events were not isolated without starving valid imports: {summary.get('synced')}")
        if retry_summary.get("synced", {}).get("import_status_events") != 1 or import_count != 2:
            fail(f"logical import retry was not idempotent: imports={import_count} summary={retry_summary.get('synced')}")
        if total_import_count != 4 or matching_sha_status != "pending" or missing_sha_status != "pending":
            fail(f"legacy row was relabeled unsafely: total={total_import_count} matching={matching_sha_status} missing={missing_sha_status}")
        if idle_summary.get("synced", {}).get("imports") != 0:
            fail(f"unchanged latest import status was processed again: {idle_summary.get('synced')}")
        if full_summary.get("synced", {}).get("imports") != 0:
            fail(f"full state sync replayed stale compatibility import status: {full_summary.get('synced')}")
        bounded_dir = root / "import-status-events"
        bounded_payload = {"padding": "x" * (700 * 1024)}
        for index in (1, 2):
            (bounded_dir / f"bounded-{index}.json").write_text(json.dumps(bounded_payload), encoding="utf-8")
        bounded_events, bounded_rejected = inkdrop_state.pending_import_status_events(
            root, limit=100, max_batch_bytes=1024 * 1024
        )
        if len(bounded_events) != 1 or bounded_rejected:
            fail(f"import event aggregate byte bound failed: events={len(bounded_events)} rejected={bounded_rejected}")
        for path in bounded_dir.glob("*.json"):
            path.unlink()
        for index in reversed(range(101)):
            (bounded_dir / f"hard-cap-{index:03d}.json").write_text(json.dumps({"index": index}), encoding="utf-8")
        hard_capped_events, _ = inkdrop_state.pending_import_status_events(
            root, limit=5000, max_batch_bytes=256 * 1024 * 1024
        )
        if len(hard_capped_events) != 100:
            fail(f"caller raised the hard import event cap: {len(hard_capped_events)}")
        selected_names = [path.name for path, _ in hard_capped_events]
        if selected_names[0] != "hard-cap-000.json" or selected_names[-1] != "hard-cap-099.json":
            fail(f"import event cutoff was applied before chronological filename order: {selected_names[:1]} {selected_names[-1:]}")


def smoke_active_import_ready_recovers_from_imported_file_proof():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    with tempfile.TemporaryDirectory(prefix="inkdrop-active-import-proof-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        source = root / "Downloads" / "Absolute Superman 001.cbz"
        dest = root / "Comics" / "Absolute Superman" / "Absolute Superman #001.cbz"
        source.parent.mkdir(parents=True)
        dest.parent.mkdir(parents=True)
        for archive_path in (source, dest):
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("001.jpg", b"active imported file proof")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-active-proof", "Absolute Superman", "comic", "comicvine", "160860", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-active-proof", "series-active-proof", "1", "1", "Issue 1", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-active-proof", "series-active-proof", "issue-active-proof", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-active-proof",
                    "wanted-active-proof",
                    "series-active-proof",
                    "issue-active-proof",
                    "importing",
                    "download_client",
                    "Absolute Superman 1",
                    "completed pack contains this wanted issue",
                    1,
                    now,
                    now,
                    "{}",
                ),
            )
            con.execute(
                """
                insert into source_attempts(
                    id,queue_id,wanted_id,series_id,issue_id,source,provider,protocol,download_client,
                    candidate_identity,lifecycle_phase,outcome,display_phase,retry_eligible,status,title,
                    started_at,completed_at,raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "attempt-active-proof", "queue-active-proof", "wanted-active-proof", "series-active-proof",
                    "issue-active-proof", "prowlarr", "TorrentLeech", "torrent", "qbittorrent",
                    "candidate-active-proof", "completed", "success", "completed", 0, "completed",
                    "Weekly Comics Pack", now, now, "{}",
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source_attempt_id, source, provider, protocol,
                    download_client, external_id, candidate_identity, title, status, state, lifecycle_phase,
                    retry_eligible, local_path, started_at, updated_at, completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-active-proof",
                    "queue-active-proof",
                    "wanted-active-proof",
                    "series-active-proof",
                    "issue-active-proof",
                    "attempt-active-proof",
                    "download_client",
                    "TorrentLeech",
                    "torrent",
                    "qBittorrent",
                    "hash-active-proof",
                    "candidate-active-proof",
                    "Weekly Comics Pack",
                    "completed_in_client",
                    "import_ready",
                    "import_ready",
                    0,
                    str(source),
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()
        with sqlite3.connect(reconcile_db) as con:
            con.execute(
                "create table imported_files(sha256 text primary key, source text, dest text, size integer, imported_at real)"
            )
            con.execute(
                "insert into imported_files values(?,?,?,?,?)",
                ("hash", str(source), str(dest), dest.stat().st_size, now),
            )
            con.commit()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        old_recovery = inkdrop_reconcile_imports.timeout_recovery_verification
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            inkdrop_reconcile_imports.timeout_recovery_verification = lambda _row, _imported_row: {
                "lifecycle_state": "waiting_for_kavita_scan",
                "reason": "imported_file_proof",
            }
            def authority_snapshot():
                with sqlite3.connect(state_db) as snapshot_con:
                    queue = snapshot_con.execute(
                        "select state, active, raw_json from queue_items where id='queue-active-proof'"
                    ).fetchone()
                    task = snapshot_con.execute(
                        "select state, status, raw_json from download_tasks where id='task-active-proof'"
                    ).fetchone()
                    result_count = snapshot_con.execute(
                        "select count(*) from import_results where queue_id='queue-active-proof'"
                    ).fetchone()[0]
                return queue, task, result_count

            before = authority_snapshot()
            without_authority = inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files(limit=10)
            after_without_authority = authority_snapshot()
            if without_authority.get("recovered") != 0:
                fail(f"replay minted import authority instead of skipping: {without_authority}")
            if after_without_authority != before:
                fail(
                    "replay without import authority changed lifecycle state: "
                    f"before={before} after={after_without_authority}"
                )
            claim = inkdrop_state.claim_import_authority(
                state_db,
                "queue-active-proof",
                "task-active-proof",
                source_attempt_id="attempt-active-proof",
                external_id="hash-active-proof",
                candidate_identity="candidate-active-proof",
                download_client="qBittorrent",
                local_path=str(source),
                claimed_at=now + 1,
            )
            if not claim.get("ok"):
                fail(f"normal import execution did not acquire authority: {claim}")
            result = inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files(limit=10)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
            inkdrop_reconcile_imports.timeout_recovery_verification = old_recovery
        if result.get("recovered") != 1 or result.get("task_updates") != 1:
            fail(f"active import-ready proof recovery did not update InkDrop state: {result}")
        with sqlite3.connect(state_db) as con:
            task = con.execute("select state, status from download_tasks where id='task-active-proof'").fetchone()
            if task != ("importing", "verification_pending"):
                fail(f"download task did not leave import_ready after imported-file proof: {task}")
            count = con.execute("select count(*) from import_results where queue_id='queue-active-proof'").fetchone()[0]
            if count != 1:
                fail(f"active imported-file proof did not create one import_result row: {count}")


def smoke_active_import_ready_rejects_mismatched_imported_file_proof():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    with tempfile.TemporaryDirectory(prefix="inkdrop-active-import-mismatch-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        wrong_source = root / "Downloads" / "2025.06.04 Weekly Pack" / "Image Week" / "Universal Monsters - The Mummy 003.cbz"
        wrong_dest = root / "Comics" / "Universal Monsters- The Mummy" / "Universal Monsters The Mummy #003.cbz"
        wrong_source.parent.mkdir(parents=True)
        wrong_dest.parent.mkdir(parents=True)
        wrong_source.write_bytes(b"wrong source")
        wrong_dest.write_bytes(b"wrong dest")
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-active-mismatch", "Absolute Superman", "comic", "comicvine", "160860", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-active-mismatch", "series-active-mismatch", "8", "008", "Issue 8", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-active-mismatch", "series-active-mismatch", "issue-active-mismatch", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-active-mismatch",
                    "wanted-active-mismatch",
                    "series-active-mismatch",
                    "issue-active-mismatch",
                    "importing",
                    "download_client",
                    "Absolute Superman 8 2025",
                    "completed pack contains this wanted issue",
                    1,
                    now,
                    now,
                    "{}",
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source, provider, protocol,
                    download_client, external_id, title, status, state, lifecycle_phase,
                    retry_eligible, local_path, started_at, updated_at, completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-active-mismatch",
                    "queue-active-mismatch",
                    "wanted-active-mismatch",
                    "series-active-mismatch",
                    "issue-active-mismatch",
                    "download_client",
                    "inkdrop_local_pack",
                    "direct",
                    "inkdrop_local_pack",
                    "weekly-pack",
                    "2025.06.04 Weekly Pack",
                    "completed_in_client",
                    "import_ready",
                    "import_ready",
                    0,
                    str(wrong_source),
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
            con.commit()
        with sqlite3.connect(reconcile_db) as con:
            con.execute(
                "create table imported_files(sha256 text primary key, source text, dest text, size integer, imported_at real)"
            )
            con.execute(
                "insert into imported_files values(?,?,?,?,?)",
                ("wrong-hash", str(wrong_source), str(wrong_dest), wrong_dest.stat().st_size, now),
            )
            con.commit()

        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            result = inkdrop_reconcile_imports.recover_active_import_ready_from_imported_files(limit=10)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
        if result.get("recovered") != 0 or result.get("skipped", {}).get("imported_file_identity_mismatch") != 1:
            fail(f"mismatched imported-file proof was not rejected: {result}")
        with sqlite3.connect(state_db) as con:
            count = con.execute("select count(*) from import_results where queue_id='queue-active-mismatch'").fetchone()[0]
            if count:
                fail(f"mismatched imported-file proof created import results: {count}")
            queue = con.execute("select state, active from queue_items where id='queue-active-mismatch'").fetchone()
            if queue != ("importing", 1):
                fail(f"mismatched imported-file proof changed queue state: {queue}")


def smoke_imported_path_must_match_trusted_target():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    with tempfile.TemporaryDirectory(prefix="inkdrop-imported-path-target-smoke-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        berserk = root / "Berserk v20 (2007) (Digital) (danke-Empire).cbz"
        absolute = root / "Absolute Superman 020 (2026) (Digital) (Lil-Empire).cbz"
        berserk.write_bytes(b"berserk")
        absolute.write_bytes(b"absolute")
        target = {"id": 160860, "title": "Absolute Superman"}

        class FakeImporter:
            @staticmethod
            def match_comic_target(path, targets):
                return dict(target) if "Absolute Superman" in Path(path).name else None

            @staticmethod
            def is_manga_target(_target):
                return False

        old_imp = inkdrop_reconcile_imports.imp
        try:
            inkdrop_reconcile_imports.imp = FakeImporter()
            imported_state = {
                "source_paths": {str(berserk), str(absolute)},
                "dest_paths": set(),
                "hash_sizes": set(),
                "hashes": set(),
            }
            wrong = inkdrop_reconcile_imports.classify_local_file(
                berserk,
                [target],
                imported_state,
                validate_archive=False,
                trusted_issue="20",
            )
            if wrong.get("state") == "suppressed_completed":
                fail(f"trusted target accepted an imported path before title matching: {wrong}")
            right = inkdrop_reconcile_imports.classify_local_file(
                absolute,
                [target],
                imported_state,
                validate_archive=False,
                trusted_issue="20",
            )
            if right.get("state") != "suppressed_completed" or right.get("matched_series") != "Absolute Superman":
                fail(f"trusted target did not suppress a matching imported path: {right}")
        finally:
            inkdrop_reconcile_imports.imp = old_imp


def smoke_queue_owned_target_classifies_without_adapter_target():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    with tempfile.TemporaryDirectory(prefix="inkdrop-queue-owned-target-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        library = root / "Comics" / "Native Standalone"
        library.mkdir(parents=True)
        source = root / "Downloads" / "Native Standalone 001 (2026) (Digital).cbz"
        wrong = root / "Downloads" / "Other Standalone 001 (2026) (Digital).cbz"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"native standalone")
        wrong.write_bytes(b"wrong standalone")
        row = {
            "queue_id": "queue-native-standalone",
            "series_id": "comicvine:999001",
            "series_title": "Native Standalone",
            "series_media_type": "comic",
            "series_year": 2026,
            "series_metadata_provider": "comicvine",
            "series_metadata_id": "999001",
            "series_library_path": str(library),
            "issue_id": "comicvine:999001:issue:1",
            "issue_number": "1",
            "normalized_number": "001",
            "issue_title": "Launch",
        }
        imported_state = {"source_paths": set(), "dest_paths": set(), "hash_sizes": set(), "hashes": set()}
        ok = inkdrop_reconcile_imports.classify_inkdrop_client_file(source, row, [], imported_state, {})
        if ok.get("state") != "ready_to_import" or ok.get("matched_series") != "Native Standalone":
            fail(f"queue-owned target fallback did not accept matching native file: {ok}")
        target = inkdrop_reconcile_imports.inkdrop_queue_row_target(row)
        if not target or target.get("target_source") != "inkdrop_queue_row" or target.get("native_series_id") != "comicvine:999001":
            fail(f"queue-owned target fallback lost native identity: {target}")
        bad = inkdrop_reconcile_imports.classify_inkdrop_client_file(wrong, row, [], imported_state, {})
        if bad.get("state") == "ready_to_import":
            fail(f"queue-owned target fallback accepted wrong series file: {bad}")


def smoke_queue_owned_one_word_manga_chapter_classifies_exact_unit():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
        import inkdrop_completed_import
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    # inkdrop_reconcile_imports does not `import inkdrop_completed_import` -- it
    # loads a second, independent module object via importlib.util.spec_from_file_location
    # (see inkdrop_reconcile_imports.load_importer()), never registered in
    # sys.modules. classify_inkdrop_client_file() below runs entirely against
    # that second copy (inkdrop_reconcile_imports.imp), so overriding
    # inkdrop_completed_import.STATE_DIR/DB_PATH here is a no-op: the guard's
    # manga_unit_model_for_target() -> connect() still reads/writes whatever
    # DB_PATH imp itself has, which defaults to the shared CI state dir
    # ($RUNNER_TEMP/inkdrop/state, every smoke script in the same job) --
    # matching the isolation inkdrop_reconcile_imports.imp already needs
    # elsewhere in this file (see reconcile_db/state_db overrides above).
    old_imp_state_dir = inkdrop_reconcile_imports.imp.STATE_DIR
    old_imp_db_path = inkdrop_reconcile_imports.imp.DB_PATH
    try:
        # Keep "chapter"/"vol" out of the temp dir name. suwayomi_chapter_number()
        # and manga_source_has_explicit_volume_hint() both scan the last FOUR path
        # components, so the staging dir's grandparent is inside the window they
        # read. A prefix ending in "-chapter-" put the literal token right next to
        # mkdtemp's 8-char random suffix, and whenever that suffix happened to start
        # with a digit (10 of its 37 legal characters, so ~27% of runs) the first
        # match of r"(?:^|[\s._-])chapter[\s._-]*(\d+)" landed on the temp dir --
        # "...-chapter-8uo2azhi" parsed as chapter 8 -- instead of on "Chapter 321"
        # in the filename. The wrong number then failed the
        # format_issue_number(number) == format_issue_number(trusted_issue) check in
        # native_manga_explicit_chapter_import_is_safe(), the auto-learn never fired,
        # and the guard rejected with manga_chapter_requires_chapter_unit_model.
        with tempfile.TemporaryDirectory(prefix="inkdrop-one-word-manga-unit-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            inkdrop_reconcile_imports.imp.STATE_DIR = root / "state"
            inkdrop_reconcile_imports.imp.DB_PATH = inkdrop_reconcile_imports.imp.STATE_DIR / "imported-files.sqlite3"
            library = root / "Manga" / "Vagabond (1999)"
            library.mkdir(parents=True)
            source = root / "staging" / "Vagabond - Chapter 321.cbz"
            wrong_unit = root / "staging" / "Vagabond - Chapter 320.cbz"
            wrong_series = root / "staging" / "Monster - Chapter 321.cbz"
            source.parent.mkdir(parents=True)
            for path in (source, wrong_unit, wrong_series):
                path.write_bytes(b"bounded staged chapter")
            row = {
                "queue_id": "queue-vagabond-321",
                "series_id": "mangadex:vagabond",
                "series_title": "Vagabond",
                "series_media_type": "manga",
                "series_year": 1999,
                "series_metadata_provider": "mangadex",
                "series_metadata_id": "vagabond",
                "series_library_path": str(library),
                "issue_id": "mangadex:vagabond:issue:321",
                "issue_number": "321",
                "normalized_number": "0321",
                "issue_title": "Chapter 321",
            }
            targets = [
                {
                    "id": None,
                    "inkdrop_series_id": "mangadex:vagabond",
                    "native_series_id": "mangadex:vagabond",
                    "title": "Vagabond",
                    "year": 1999,
                    "publisher": "MangaDex",
                    "media_type": "manga",
                    "folder": str(library),
                    "metadata_provider": "mangadex",
                    "metadata_id": "vagabond",
                    "target_source": "inkdrop_series",
                    "aliases": ["vagabond"],
                }
            ]
            imported_state = {"source_paths": set(), "dest_paths": set(), "hash_sizes": set(), "hashes": set()}
            ok = inkdrop_reconcile_imports.classify_inkdrop_client_file(source, row, targets, imported_state, {})
            if ok.get("state") != "ready_to_import" or ok.get("matched_series") != "Vagabond":
                fail(f"trusted one-word manga chapter did not reach import: {ok}")
            bad_unit = inkdrop_reconcile_imports.classify_inkdrop_client_file(wrong_unit, row, targets, imported_state, {})
            if bad_unit.get("state") == "ready_to_import":
                fail(f"trusted one-word manga chapter accepted the wrong unit: {bad_unit}")
            bad_series = inkdrop_reconcile_imports.classify_inkdrop_client_file(wrong_series, row, targets, imported_state, {})
            if bad_series.get("state") == "ready_to_import":
                fail(f"trusted one-word manga chapter accepted the wrong series: {bad_series}")
    finally:
        inkdrop_reconcile_imports.imp.STATE_DIR = old_imp_state_dir
        inkdrop_reconcile_imports.imp.DB_PATH = old_imp_db_path


def smoke_import_target_accepts_safe_leading_article_alias():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_completed_import
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise

    immortal_targets = inkdrop_completed_import.annotate_target_alias_conflicts(
        [
            {
                "id": "series-immortal-hulk",
                "title": "The Immortal Hulk",
                "native_series_id": "series-immortal-hulk",
                "metadata_provider": "comicvine",
                "metadata_id": "110953",
                "target_source": "inkdrop_series",
                "folder": "/library/The Immortal Hulk",
                "aliases": [
                    inkdrop_completed_import.normalize(alias)
                    for alias in (
                        ["The Immortal Hulk"]
                        + inkdrop_completed_import.leading_article_aliases("The Immortal Hulk")
                    )
                    if inkdrop_completed_import.normalize(alias)
                ],
            }
        ]
    )
    source_path = Path(
        "/downloads/comics/Immortal.Hulk.012.2019.Digital.Zone-Empire/"
        "Immortal Hulk 012 (2019) (Digital) (Zone-Empire).cbr"
    )
    match = inkdrop_completed_import.match_comic_target(source_path, immortal_targets)
    if not match or match.get("title") != "The Immortal Hulk":
        fail(f"leading-article import alias did not match safe source path: {match}")

    ambiguous_targets = inkdrop_completed_import.annotate_target_alias_conflicts(
        [
            {
                "id": "series-fix",
                "title": "Fix",
                "native_series_id": "series-fix",
                "metadata_provider": "comicvine",
                "metadata_id": "1",
                "target_source": "inkdrop_series",
                "folder": "/library/Fix",
                "aliases": ["fix"],
            },
            {
                "id": "series-the-fix",
                "title": "The Fix",
                "native_series_id": "series-the-fix",
                "metadata_provider": "comicvine",
                "metadata_id": "2",
                "target_source": "inkdrop_series",
                "folder": "/library/The Fix",
                "aliases": [
                    inkdrop_completed_import.normalize(alias)
                    for alias in (
                        ["The Fix"]
                        + inkdrop_completed_import.leading_article_aliases("The Fix")
                    )
                    if inkdrop_completed_import.normalize(alias)
                ],
            },
        ]
    )
    if inkdrop_completed_import.match_comic_target(Path("/downloads/comics/Fix 001.cbz"), ambiguous_targets):
        fail("ambiguous article-dropped alias should not match a conflicting short title")
    the_fix = inkdrop_completed_import.match_comic_target(Path("/downloads/comics/The Fix 001.cbz"), ambiguous_targets)
    if not the_fix or the_fix.get("title") != "The Fix":
        fail(f"full article title should still match when dropped alias is ambiguous: {the_fix}")


def smoke_import_ready_pack_priority_helpers():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    weekly_path = "/downloads/comics/2025-11-26 Weekly Comics Pack/Absolute Wonder Woman 014 (2026).cbz"
    spawn_path = "/downloads/comics/Spawn.(001-268+)(1992-)/Spawn 233(2013).cbr"
    single_path = "/downloads/comics/Absolute Superman 020 (2026).cbz"
    weekly_priority = inkdrop_reconcile_imports.import_ready_batch_priority(weekly_path)
    single_priority = inkdrop_reconcile_imports.import_ready_batch_priority(single_path)
    broad_priority = inkdrop_reconcile_imports.import_ready_batch_priority(spawn_path)
    if not (weekly_priority < single_priority < broad_priority):
        fail(f"unexpected import-ready pack priority order: weekly={weekly_priority}, single={single_priority}, broad={broad_priority}")
    broad_key = inkdrop_reconcile_imports.import_ready_broad_pack_key(spawn_path, "Spawn.(001-268+)(1992-)")
    if not broad_key or "spawn" not in broad_key:
        fail(f"broad pack key did not identify Spawn collection: {broad_key!r}")
    weekly_key = inkdrop_reconcile_imports.import_ready_broad_pack_key(weekly_path, "2025-11-26 Weekly Comics Pack")
    if weekly_key:
        fail(f"weekly packs should be prioritized, not broad-pack capped: {weekly_key!r}")
    staged_priority = inkdrop_reconcile_imports.import_ready_client_priority("inkdrop_local_pack")
    qbit_priority = inkdrop_reconcile_imports.import_ready_client_priority("qBittorrent")
    sab_priority = inkdrop_reconcile_imports.import_ready_client_priority("sabnzbd")
    if not (staged_priority < qbit_priority <= sab_priority):
        fail(f"staged import-ready clients should drain before download-client rows: staged={staged_priority}, qbit={qbit_priority}, sab={sab_priority}")


def smoke_import_ready_skip_result_classification():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    failed_state = inkdrop_reconcile_imports.import_result_state(
        {
            "imported": [],
            "skipped": [
                {
                    "event": "skip_weak_filename_import_guard",
                    "skip_reason": "filename_confidence_too_low",
                    "action_needed": "manual_review",
                }
            ],
        },
        returncode=0,
    )
    if failed_state[0] != "failed_import" or "filename_confidence_too_low" not in failed_state[1]:
        fail(f"manual-review importer skip was not classified as failed import: {failed_state}")
    benign_state = inkdrop_reconcile_imports.import_result_state(
        {
            "imported": [],
            "skipped": [
                {
                    "event": "skip_existing_destination",
                    "skip_reason": "already_imported_or_verified",
                }
            ],
        },
        returncode=0,
    )
    if benign_state != ("imported", "importer_skipped_existing_or_retained", False):
        fail(f"benign retained importer skip was not classified as imported: {benign_state}")
    collision_state = inkdrop_reconcile_imports.import_result_state(
        {
            "imported": [],
            "skipped": [
                {
                    "event": "skip_media_management_existing_destination",
                    "skip_reason": "media_management_destination_exists",
                    "action_needed": "none",
                    "dest": "/library/Managed Series/Managed Series #001.cbz",
                    "media_management_destination_decision": {
                        "selected_dest_path": "/library/Managed Series/Managed Series #001.cbz",
                        "planned_path": "/library/Managed Series/Managed Series #001.cbz",
                        "reason": "planned_path_exists",
                    },
                }
            ],
        },
        returncode=0,
    )
    if collision_state != ("imported", "importer_skipped_existing_or_retained", False):
        fail(f"planned-path existing destination should be retained with selected destination evidence: {collision_state}")
    unsafe_collision_state = inkdrop_reconcile_imports.import_result_state(
        {
            "imported": [],
            "skipped": [
                {
                    "event": "skip_media_management_existing_destination",
                    "skip_reason": "media_management_destination_exists",
                    "action_needed": "none",
                }
            ],
        },
        returncode=0,
    )
    if unsafe_collision_state[0] != "failed_import" or "media_management_destination_exists" not in unsafe_collision_state[1]:
        fail(f"planned-path existing destination without destination evidence should fail safe: {unsafe_collision_state}")


def smoke_completed_import_reports_incomplete_qbit_source_file():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_completed_import
    except FileNotFoundError:
        return
    with tempfile.TemporaryDirectory(prefix="inkdrop-completed-incomplete-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        source = root / "Incomplete Source 001.cbz"
        source.write_bytes(b"incomplete qbit file placeholder")
        db_path = root / "imported-files.sqlite3"

        old_apply = inkdrop_completed_import.apply_path_provider_settings
        old_connect = inkdrop_completed_import.connect
        old_scan = inkdrop_completed_import.scan_sources
        old_targets = inkdrop_completed_import.load_comic_targets
        old_incomplete = inkdrop_completed_import.load_qbit_incomplete_paths
        old_stable = inkdrop_completed_import.is_stable
        old_log = inkdrop_completed_import.log
        try:
            inkdrop_completed_import.apply_path_provider_settings = lambda: {
                "comic_root": root / "Comics",
                "manga_root": root / "Manga",
                "kavita_comic_root": "/data/comics",
                "kavita_manga_root": "/data/manga",
                "manual_comics_inbox": root / "manual-comics",
                "manual_ebooks_inbox": root / "manual-ebooks",
                "library_source": "smoke",
                "manual_inbox_source": "smoke",
            }

            def connect_smoke():
                con = sqlite3.connect(db_path)
                con.execute("create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
                return con

            inkdrop_completed_import.connect = connect_smoke
            inkdrop_completed_import.scan_sources = lambda kind, manual_inbox=False, suwayomi_staging=False, slskd_staging=False: ([root], root / "dest")
            inkdrop_completed_import.load_comic_targets = lambda series_filter=None: []
            inkdrop_completed_import.load_qbit_incomplete_paths = lambda kind: {str(source)} if kind == "comics" else set()
            inkdrop_completed_import.is_stable = lambda path, min_age_seconds: True
            inkdrop_completed_import.log = lambda event: None
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                inkdrop_completed_import.import_files(
                    "comics",
                    dry_run=True,
                    min_age_seconds=0,
                    ignore_cutoff=True,
                    matched_only=True,
                    all_series=True,
                    max_files=1,
                    source_files=[str(source)],
                    trusted_series_id="comicvine:1",
                    trusted_issue="1",
                    wait_for_kavita_scan=False,
                )
            payload = json.loads(buffer.getvalue())
        finally:
            inkdrop_completed_import.apply_path_provider_settings = old_apply
            inkdrop_completed_import.connect = old_connect
            inkdrop_completed_import.scan_sources = old_scan
            inkdrop_completed_import.load_comic_targets = old_targets
            inkdrop_completed_import.load_qbit_incomplete_paths = old_incomplete
            inkdrop_completed_import.is_stable = old_stable
            inkdrop_completed_import.log = old_log

        skipped = payload.get("skipped") or []
        if len(skipped) != 1 or skipped[0].get("skip_reason") != "source_file_incomplete_qbit_download":
            fail(f"completed importer did not expose incomplete qbit source skip: {payload}")
        if skipped[0].get("action_needed") != "automatic_wait":
            fail(f"incomplete qbit source skip should be automatic wait: {skipped[0]}")


def smoke_completed_import_reports_media_management_preview():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_completed_import
    except FileNotFoundError:
        return
    with tempfile.TemporaryDirectory(prefix="inkdrop-completed-media-preview-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        source = root / "Preview Series 001 (2026).cbz"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            payload = valid_large_png()
            for idx in range(1, 4):
                archive.writestr(f"{idx:03d}.png", payload)
        imported_db = root / "imported-files.sqlite3"
        state_db = root / "inkdrop-state.sqlite3"
        inkdrop_state.sync_settings(
            state_db,
            settings=[
                {
                    "key": "media_management.comic_root",
                    "scope": "media_management",
                    "label": "Comic Root",
                    "value": "/library/Comics",
                    "description": "smoke",
                    "source": "runtime",
                },
                {
                    "key": "media_management.series_folder_format",
                    "scope": "media_management",
                    "label": "Series Folder Format",
                    "value": "{Series Title} ({Year})",
                    "description": "smoke",
                    "source": "runtime",
                },
                {
                    "key": "media_management.comic_issue_format",
                    "scope": "media_management",
                    "label": "Comic Issue Format",
                    "value": "{Series Title} #{Issue:000} ({Year})",
                    "description": "smoke",
                    "source": "runtime",
                },
            ],
        )

        old_apply = inkdrop_completed_import.apply_path_provider_settings
        old_connect = inkdrop_completed_import.connect
        old_scan = inkdrop_completed_import.scan_sources
        old_targets = inkdrop_completed_import.load_comic_targets
        old_incomplete = inkdrop_completed_import.load_qbit_incomplete_paths
        old_stable = inkdrop_completed_import.is_stable
        old_log = inkdrop_completed_import.log
        old_state_db = inkdrop_completed_import.INKDROP_STATE_DB
        old_comic_root = inkdrop_completed_import.COMIC_ROOT
        old_manga_root = inkdrop_completed_import.MANGA_ROOT
        try:
            comic_root = root / "Comics"
            manga_root = root / "Manga"
            inkdrop_completed_import.INKDROP_STATE_DB = state_db
            inkdrop_completed_import.COMIC_ROOT = comic_root
            inkdrop_completed_import.MANGA_ROOT = manga_root
            target = {
                "id": None,
                "kapowarr_id": None,
                "inkdrop_series_id": "comicvine:preview",
                "native_series_id": "comicvine:preview",
                "title": "Preview Series",
                "year": "2026",
                "publisher": "Smoke",
                "media_type": "comic",
                "folder": str(comic_root / "Preview Series"),
                "metadata_provider": "comicvine",
                "metadata_id": "preview",
                "comicvine_id": "preview",
                "target_source": "inkdrop_series",
                "aliases": ["preview series"],
            }
            inkdrop_completed_import.apply_path_provider_settings = lambda: {
                "comic_root": comic_root,
                "manga_root": manga_root,
                "kavita_comic_root": "/data/comics",
                "kavita_manga_root": "/data/manga",
                "manual_comics_inbox": root / "manual-comics",
                "manual_ebooks_inbox": root / "manual-ebooks",
                "library_source": "smoke",
                "manual_inbox_source": "smoke",
            }

            def connect_smoke():
                con = sqlite3.connect(imported_db)
                con.execute("create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
                return con

            inkdrop_completed_import.connect = connect_smoke
            inkdrop_completed_import.scan_sources = lambda kind, manual_inbox=False, suwayomi_staging=False, slskd_staging=False: ([source], comic_root / "_Incoming")
            inkdrop_completed_import.load_comic_targets = lambda series_filter=None: [target]
            inkdrop_completed_import.load_qbit_incomplete_paths = lambda kind: set()
            inkdrop_completed_import.is_stable = lambda path, min_age_seconds: True
            inkdrop_completed_import.log = lambda event: None
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                inkdrop_completed_import.import_files(
                    "comics",
                    dry_run=True,
                    min_age_seconds=0,
                    ignore_cutoff=True,
                    matched_only=True,
                    all_series=True,
                    max_files=1,
                    source_files=[str(source)],
                    trusted_series_id="comicvine:preview",
                    trusted_issue="1",
                    wait_for_kavita_scan=False,
                )
            payload = json.loads(buffer.getvalue())
        finally:
            inkdrop_completed_import.apply_path_provider_settings = old_apply
            inkdrop_completed_import.connect = old_connect
            inkdrop_completed_import.scan_sources = old_scan
            inkdrop_completed_import.load_comic_targets = old_targets
            inkdrop_completed_import.load_qbit_incomplete_paths = old_incomplete
            inkdrop_completed_import.is_stable = old_stable
            inkdrop_completed_import.log = old_log
            inkdrop_completed_import.INKDROP_STATE_DB = old_state_db
            inkdrop_completed_import.COMIC_ROOT = old_comic_root
            inkdrop_completed_import.MANGA_ROOT = old_manga_root

        imported = payload.get("imported") or []
        if len(imported) != 1:
            fail(f"completed importer dry-run did not produce one import event: {payload}")
        preview = imported[0].get("media_management_preview") or {}
        if preview.get("planned_path") != "/library/Comics/Preview Series (2026)/Preview Series #001 (2026).cbz":
            fail(f"media-management preview planned path mismatch: {preview}")
        if preview.get("preview_only") is not True or preview.get("mutates_filesystem") is not False:
            fail(f"media-management preview was not read-only: {preview}")
        if not preview.get("current_import_dest_path") or preview.get("current_import_dest_matches_preview") is not False:
            fail(f"media-management preview did not compare current importer destination: {preview}")
        if preview.get("apply_planned_path_enabled") is not True:
            fail(f"media-management preview should default planned-path apply on: {preview}")
        if str(preview.get("planned_path_apply_status") or "") == "disabled":
            fail(f"media-management preview should not disable planned-path apply by default: {preview}")
        if str(preview.get("planned_path_apply_status") or "") not in {"blocked_not_absolute", "blocked_root_missing", "blocked_outside_configured_roots", "selected"}:
            fail(f"media-management preview did not expose a safe planned-path apply status: {preview}")
        if (root / "Comics" / "Preview Series" / "Preview Series #001 (2026).cbz").exists():
            fail("dry-run media-management preview copied the source file")


def smoke_completed_import_applies_media_management_path_when_enabled():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_completed_import
    except FileNotFoundError:
        return
    with tempfile.TemporaryDirectory(prefix="inkdrop-completed-media-apply-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        source_root = root / "source"
        source_root.mkdir(parents=True)
        source = source_root / "Preview Series 001 (2026).cbz"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            payload = valid_large_png()
            for idx in range(1, 4):
                archive.writestr(f"{idx:03d}.png", payload)
        imported_db = root / "imported-files.sqlite3"
        state_db = root / "inkdrop-state.sqlite3"
        managed_root = root / "Managed Comics"
        legacy_root = root / "Legacy Comics"
        managed_root.mkdir(parents=True, exist_ok=True)
        inkdrop_state.sync_settings(
            state_db,
            settings=[
                {
                    "key": "media_management.comic_root",
                    "scope": "media_management",
                    "label": "Comic Root",
                    "value": str(managed_root),
                    "description": "smoke",
                    "source": "runtime",
                },
                {
                    "key": "media_management.series_folder_format",
                    "scope": "media_management",
                    "label": "Series Folder Format",
                    "value": "{Series Title} ({Year})",
                    "description": "smoke",
                    "source": "runtime",
                },
                {
                    "key": "media_management.comic_issue_format",
                    "scope": "media_management",
                    "label": "Comic Issue Format",
                    "value": "{Series Title} #{Issue:000} ({Year})",
                    "description": "smoke",
                    "source": "runtime",
                },
                {
                    "key": "media_management.apply_planned_path",
                    "scope": "media_management",
                    "label": "Apply Planned Import Path",
                    "value": False,
                    "description": "smoke",
                    "source": "runtime",
                },
                {
                    "key": "media_management.minimum_free_space_gb",
                    "scope": "media_management",
                    "label": "Minimum Free Space GB",
                    "value": 0,
                    "description": "smoke",
                    "source": "runtime",
                },
            ],
        )

        old_apply = inkdrop_completed_import.apply_path_provider_settings
        old_connect = inkdrop_completed_import.connect
        old_scan = inkdrop_completed_import.scan_sources
        old_targets = inkdrop_completed_import.load_comic_targets
        old_incomplete = inkdrop_completed_import.load_qbit_incomplete_paths
        old_stable = inkdrop_completed_import.is_stable
        old_log = inkdrop_completed_import.log
        old_state_db = inkdrop_completed_import.INKDROP_STATE_DB
        old_comic_root = inkdrop_completed_import.COMIC_ROOT
        old_manga_root = inkdrop_completed_import.MANGA_ROOT
        try:
            manga_root = root / "Manga"
            inkdrop_completed_import.INKDROP_STATE_DB = state_db
            inkdrop_completed_import.COMIC_ROOT = managed_root
            inkdrop_completed_import.MANGA_ROOT = manga_root
            target = {
                "id": None,
                "kapowarr_id": None,
                "inkdrop_series_id": "comicvine:preview-apply",
                "native_series_id": "comicvine:preview-apply",
                "title": "Preview Series",
                "year": "2026",
                "publisher": "Smoke",
                "media_type": "comic",
                "folder": str(legacy_root / "Preview Series"),
                "metadata_provider": "comicvine",
                "metadata_id": "preview-apply",
                "comicvine_id": "preview-apply",
                "target_source": "inkdrop_series",
                "aliases": ["preview series"],
            }
            inkdrop_completed_import.apply_path_provider_settings = lambda: {
                "comic_root": managed_root,
                "manga_root": manga_root,
                "kavita_comic_root": "/data/comics",
                "kavita_manga_root": "/data/manga",
                "manual_comics_inbox": root / "manual-comics",
                "manual_ebooks_inbox": root / "manual-ebooks",
                "library_source": "smoke",
                "manual_inbox_source": "smoke",
            }

            def connect_smoke():
                con = sqlite3.connect(imported_db)
                con.execute("create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
                return con

            inkdrop_completed_import.connect = connect_smoke
            inkdrop_completed_import.scan_sources = lambda kind, manual_inbox=False, suwayomi_staging=False, slskd_staging=False: ([source], managed_root / "_Incoming")
            inkdrop_completed_import.load_comic_targets = lambda series_filter=None: [target]
            inkdrop_completed_import.load_qbit_incomplete_paths = lambda kind: set()
            inkdrop_completed_import.is_stable = lambda path, min_age_seconds: True
            inkdrop_completed_import.log = lambda event: None
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                inkdrop_completed_import.import_files(
                    "comics",
                    dry_run=True,
                    min_age_seconds=0,
                    ignore_cutoff=True,
                    matched_only=True,
                    all_series=True,
                    max_files=1,
                    source_files=[str(source)],
                    trusted_series_id="comicvine:preview-apply",
                    trusted_issue="1",
                    wait_for_kavita_scan=False,
                    apply_planned_path=True,
                )
            payload = json.loads(buffer.getvalue())
        finally:
            inkdrop_completed_import.apply_path_provider_settings = old_apply
            inkdrop_completed_import.connect = old_connect
            inkdrop_completed_import.scan_sources = old_scan
            inkdrop_completed_import.load_comic_targets = old_targets
            inkdrop_completed_import.load_qbit_incomplete_paths = old_incomplete
            inkdrop_completed_import.is_stable = old_stable
            inkdrop_completed_import.log = old_log
            inkdrop_completed_import.INKDROP_STATE_DB = old_state_db
            inkdrop_completed_import.COMIC_ROOT = old_comic_root
            inkdrop_completed_import.MANGA_ROOT = old_manga_root

        imported = payload.get("imported") or []
        if len(imported) != 1:
            fail(f"planned-path apply dry-run did not produce one import event: {payload}")
        event = imported[0]
        expected_path = managed_root / "Preview Series (2026)" / "Preview Series #001 (2026).cbz"
        if event.get("dest") != str(expected_path):
            fail(f"planned-path apply did not select managed destination: {event}")
        decision = event.get("media_management_destination_decision") or {}
        if decision.get("applied") is not True or decision.get("reason") != "planned_path_selected" or decision.get("override") is not True:
            fail(f"planned-path apply decision was not selected: {decision}")
        preview = event.get("media_management_preview") or {}
        if (
            preview.get("planned_path_applied") is not True
            or preview.get("planned_path_apply_status") != "selected"
            or preview.get("apply_planned_path_override") is not True
        ):
            fail(f"planned-path apply preview was not selected: {preview}")
        if preview.get("legacy_import_dest_path") == preview.get("selected_import_dest_path"):
            fail(f"planned-path apply did not preserve legacy-vs-selected evidence: {preview}")
        if expected_path.exists():
            fail("dry-run planned-path apply copied the source file")


def smoke_exact_manga_volume_plans_and_imports_volume_only_destination():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    import inkdrop_completed_import

    with tempfile.TemporaryDirectory(prefix="inkdrop-manga-volume-canonical-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        source_dir = root / "completed"
        manga_root = root / "Manga"
        series_dir = manga_root / "Dorohedoro"
        source_dir.mkdir()
        series_dir.mkdir(parents=True)
        source = source_dir / "Dorohedoro v06 (2012) (Digital).cbz"
        page = valid_large_png()
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
            for idx in range(1, 13):
                archive.writestr(f"{idx:03d}.png", page)
        conflicting = series_dir / "Dorohedoro v06 c006.cbz"
        conflicting.write_bytes(b"preserve wrong-unit artifact")
        conflicting_bytes = conflicting.read_bytes()
        imported_db = root / "imported-files.sqlite3"
        state_db = root / "inkdrop-state.sqlite3"
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                "insert into series(id,title,media_type,library_path,monitored,created_at,updated_at,raw_json) values(?,?,?,?,1,1,1,'{}')",
                ("series:doro", "Dorohedoro", "manga", str(series_dir)),
            )
            con.execute(
                "insert into issues(id,series_id,issue_number,normalized_number,title,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,1,1,1,'{}')",
                ("issue:doro:6", "series:doro", "6", "6", "Vol. 6"),
            )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,'in_progress',1,1,'{}')",
                ("wanted:doro:6", "series:doro", "issue:doro:6"),
            )
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,issue_id,state,query,active,created_at,updated_at,raw_json) values(?,?,?,?,'importing','Dorohedoro Vol. 6',1,1,1,?)",
                (
                    "queue:doro:6",
                    "wanted:doro:6",
                    "series:doro",
                    "issue:doro:6",
                    json.dumps({"media_type": "manga", "issue_title": "Vol. 6", "issue_number": "6"}),
                ),
            )
            inkdrop_state.record_source_attempt(
                con,
                "queue:doro:6",
                "wanted:doro:6",
                "series:doro",
                "issue:doro:6",
                {"source": "slskd", "provider": "peer", "status": "staged_file_ready", "title": source.name},
                attempt_id="attempt:doro:6",
                started_at=1,
                completed_at=1,
            )
            con.execute(
                """
                insert into download_tasks(
                    id,queue_id,wanted_id,series_id,issue_id,source_attempt_id,source,provider,
                    download_client,external_id,title,status,state,local_path,started_at,updated_at,raw_json
                ) values(?,?,?,?,?,?,'slskd','peer','SLSKD',?,?,'staged_file_ready','import_ready',?,1,1,'{}')
                """,
                (
                    "task:doro:6", "queue:doro:6", "wanted:doro:6", "series:doro", "issue:doro:6",
                    "attempt:doro:6", "transfer:doro:6", source.name, str(source),
                ),
            )
            con.commit()
        inkdrop_state.sync_settings(
            state_db,
            settings=[
                {
                    "key": "media_management.manga_root",
                    "scope": "media_management",
                    "label": "Manga Root",
                    "value": str(manga_root),
                    "description": "smoke",
                    "source": "runtime",
                },
                {
                    "key": "media_management.apply_planned_path",
                    "scope": "media_management",
                    "label": "Apply Planned Import Path",
                    "value": True,
                    "description": "smoke",
                    "source": "runtime",
                },
                {
                    "key": "media_management.minimum_free_space_gb",
                    "scope": "media_management",
                    "label": "Minimum Free Space GB",
                    "value": 0,
                    "description": "smoke",
                    "source": "runtime",
                },
            ],
        )
        chapter_preview = inkdrop_state.media_management_destination_preview(
            state_db,
            {
                "series": "Dorohedoro",
                "title": "Dorohedoro",
                "media_type": "manga",
                "native_series_id": "series:doro",
                "issue_number": "6",
                "normalized_number": "6",
                "source_unit": "chapter",
                "unit_type": "chapter",
                "chapter_number": "6",
                "manga_unit_policy": "mixed_allowed",
            },
            source_path=str(source_dir / "Dorohedoro Chapter 6.cbz"),
            settings={
                "manga_root": str(manga_root),
                "minimum_free_space_gb": 0,
                "apply_planned_path": True,
            },
        )
        chapter_name = Path(chapter_preview.get("planned_path") or "").name
        if chapter_name != "Dorohedoro c006.cbz" or "v06" in chapter_name.lower():
            fail(f"mixed-policy chapter target did not retain chapter-only canonical identity: {chapter_preview}")
        target = {
            "id": None,
            "native_series_id": "series:doro",
            "inkdrop_series_id": "series:doro",
            "title": "Dorohedoro",
            "aliases": ["dorohedoro"],
            "media_type": "manga",
            "query": "Dorohedoro Vol. 6",
            "issue_title": "Vol. 6",
            "issue_number": "6",
            "normalized_number": "6",
            "folder": str(series_dir),
            "target_source": "inkdrop_series",
        }
        conflicting_source_reason = inkdrop_completed_import.unsafe_comic_target_match_reason(
            source_dir / "Dorohedoro v06 c006.cbz",
            {**target, "query": "Dorohedoro Chapter 6", "issue_title": "Chapter 6", "manga_unit_policy": "mixed_allowed"},
        )
        if conflicting_source_reason != "manga_source_conflicting_unit_identity":
            fail(f"mixed volume/chapter source identity was not blocked: {conflicting_source_reason}")
        patched = {
            name: getattr(inkdrop_completed_import, name)
            for name in (
                "STATE_DIR", "DB_PATH", "INKDROP_STATE_DB", "COMIC_ROOT", "MANGA_ROOT",
                "apply_path_provider_settings", "connect", "scan_sources", "load_comic_targets",
                "load_qbit_incomplete_paths", "is_stable", "log", "sync_library_frontend_folders",
            )
        }
        connections = []
        try:
            inkdrop_completed_import.STATE_DIR = root / "import-state"
            inkdrop_completed_import.DB_PATH = imported_db
            inkdrop_completed_import.INKDROP_STATE_DB = state_db
            inkdrop_completed_import.COMIC_ROOT = root / "Comics"
            inkdrop_completed_import.MANGA_ROOT = manga_root
            inkdrop_completed_import.apply_path_provider_settings = lambda: {
                "comic_root": root / "Comics",
                "manga_root": manga_root,
                "kavita_comic_root": "/data/comics",
                "kavita_manga_root": "/data/manga",
                "manual_comics_inbox": root / "manual-comics",
                "manual_ebooks_inbox": root / "manual-ebooks",
                "library_source": "smoke",
                "manual_inbox_source": "smoke",
            }

            def connect_smoke():
                con = sqlite3.connect(imported_db)
                con.execute("create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
                inkdrop_completed_import.ensure_artifact_bad_content_memory_schema(con)
                inkdrop_completed_import.ensure_manga_completion_schema(con)
                inkdrop_completed_import.ensure_collection_completion_schema(con)
                inkdrop_completed_import.ensure_manga_unit_schema(con)
                connections.append(con)
                return con

            inkdrop_completed_import.connect = connect_smoke
            inkdrop_completed_import.scan_sources = lambda kind, manual_inbox=False, suwayomi_staging=False, slskd_staging=False: ([source], source_dir)
            inkdrop_completed_import.load_comic_targets = lambda series_filter=None: [dict(target)]
            inkdrop_completed_import.load_qbit_incomplete_paths = lambda kind: set()
            inkdrop_completed_import.is_stable = lambda path, min_age_seconds: True
            inkdrop_completed_import.log = lambda event: None
            inkdrop_completed_import.sync_library_frontend_folders = lambda *args, **kwargs: {
                "kavita": [], "komga": [], "library_scan_tasks": {"kavita": [], "komga": []}
            }

            def run_import(dry_run):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    inkdrop_completed_import.import_files(
                        "comics",
                        dry_run=dry_run,
                        min_age_seconds=0,
                        ignore_cutoff=True,
                        matched_only=True,
                        all_series=True,
                        max_files=1,
                        source_files=[str(source)],
                        trusted_issue="6",
                        trusted_series_id="series:doro",
                        trusted_issue_id="issue:doro:6",
                        wait_for_library_scan=False,
                        apply_planned_path=True,
                    )
                return json.loads(output.getvalue())

            preview = run_import(True)
            preview_event = (preview.get("imported") or [None])[0]
            if not preview_event:
                fail(f"exact manga volume dry-run did not reach import planning: {preview}")
            planned = Path((preview_event.get("media_management_preview") or {}).get("planned_path") or "")
            if planned.name != "Dorohedoro v06.cbz" or "c006" in planned.name.lower():
                fail(f"exact volume dry-run planned a non-volume destination: {preview_event}")
            if preview_event.get("source_unit") != "volume" or preview_event.get("source_volume_number") != "6":
                fail(f"exact volume identity was not bound before planning: {preview_event}")

            live = run_import(False)
            live_event = (live.get("imported") or [None])[0]
            if not live_event:
                fail(f"exact manga volume live import failed: {live}")
            canonical = Path(live_event.get("dest") or "")
            if canonical.name != "Dorohedoro v06.cbz" or not canonical.is_file():
                fail(f"live import did not create the volume-only canonical file: {live_event}")
            if conflicting.read_bytes() != conflicting_bytes:
                fail("wrong-unit v06 c006 artifact was modified or overwritten")

            replay = run_import(False)
            volume_files = list(manga_root.rglob("Dorohedoro v06*.cbz"))
            if len(volume_files) != 2:
                fail(f"replay created a duplicate canonical copy: {volume_files}")
            replay_rows = (replay.get("imported") or []) + (replay.get("skipped") or [])
            if not replay_rows or Path(replay_rows[0].get("dest") or "") != canonical:
                fail(f"replay did not settle on the existing exact canonical destination: {replay}")

            rejected = inkdrop_state.record_direct_import_result(
                state_db,
                "queue:doro:6",
                source_path=str(source),
                dest_path=str(conflicting),
                source="slskd",
                status="already_present_clearable",
                imported_count=0,
                skipped_count=1,
                raw={"reason": "canonical_file_already_present", "trusted_issue": "6"},
            )
            if rejected.get("status") != "wrong_unit_quarantined" or rejected.get("verified"):
                fail(f"wrong-unit destination satisfied exact volume completion: {rejected}")
            if conflicting.read_bytes() != conflicting_bytes:
                fail("strict completion quarantine modified the wrong-unit artifact")

            proof_event = dict(live_event)
            proof_event.update(
                {
                    "queue_id": "queue:doro:6",
                    "wanted_id": "wanted:doro:6",
                    "series_id": "series:doro",
                    "issue_id": "issue:doro:6",
                    "source_attempt_id": "attempt:doro:6",
                    "download_task_id": "task:doro:6",
                    "external_id": "transfer:doro:6",
                    "slskd_transfer_id": "transfer:doro:6",
                    "require_exact_queue_id": True,
                }
            )
            proof = inkdrop_state.record_pack_import_results(
                state_db,
                imported=[proof_event],
                review_id="review:doro:6",
                pack_path=str(source),
                series="Dorohedoro",
                title="Dorohedoro",
                verification={
                    "checked": [
                        {"dest": str(canonical), "verification_status": "folder_verified", "verified": True}
                    ]
                },
                source="manual_source_import",
            )
            if proof.get("recorded") != 1:
                fail(f"successful volume import did not record exact SLSKD provenance: {proof}")
            with inkdrop_state.connect_read(state_db) as con:
                proof_row = con.execute(
                    "select source_attempt_id,raw_json from import_results where queue_id='queue:doro:6' and verified=1 order by created_at desc limit 1"
                ).fetchone()
            proof_raw = json.loads(proof_row["raw_json"] or "{}") if proof_row else {}
            resolved = proof_raw.get("resolved_row") if isinstance(proof_raw.get("resolved_row"), dict) else {}
            if not proof_row or proof_row["source_attempt_id"] != "attempt:doro:6":
                fail(f"successful import lost exact source-attempt identity: {dict(proof_row) if proof_row else None}")
            if resolved.get("download_task_id") != "task:doro:6" or resolved.get("external_id") != "transfer:doro:6":
                fail(f"successful import lost task/transfer identity: {resolved}")
        finally:
            for con in connections:
                try:
                    con.close()
                except Exception:
                    pass
            for name, value in patched.items():
                setattr(inkdrop_completed_import, name, value)


def smoke_completed_import_trusted_issue_lifts_filename_guard():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_completed_import
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    target = {"title": "The Department of Truth", "year": 2020, "issue_number": None}
    source = Path("/tmp/The Department of Truth 015 (2022) (Digital) (Zone-Empire).cbr")
    old_read = inkdrop_completed_import.read_comicinfo
    try:
        inkdrop_completed_import.read_comicinfo = lambda path: None
        untrusted = inkdrop_completed_import.weak_filename_import_guard(source, target, "comics")
        trusted = inkdrop_completed_import.weak_filename_import_guard(
            source,
            target,
            "comics",
            trusted_issue="15",
        )
    finally:
        inkdrop_completed_import.read_comicinfo = old_read
    if untrusted.get("ok"):
        fail(f"untrusted queue-backed filename unexpectedly passed weak guard: {untrusted}")
    if not trusted.get("ok"):
        fail(f"trusted issue did not satisfy weak filename guard: {trusted}")
    if not any(str(item).startswith("trusted_issue:") for item in (trusted.get("evidence") or [])):
        fail(f"trusted issue evidence missing from filename guard: {trusted}")


def smoke_completed_import_trusted_tpb_lifts_missing_number_guard():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_completed_import
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    target = {
        "title": "Goodbye, Eri",
        "year": 2023,
        "media_type": "manga",
        "issue_title": "TPB",
        "aliases": ["goodbye eri"],
    }
    non_tpb_target = {**target, "issue_title": "Chapter One"}
    source = Path("/tmp/Goodbye, Eri (2023) (Digital) (1r0n).cbz")
    old_read = inkdrop_completed_import.read_comicinfo
    try:
        inkdrop_completed_import.read_comicinfo = lambda path: {}
        mismatch = inkdrop_completed_import.trusted_issue_mismatch_reason(
            source,
            "1",
            target=target,
        )
        trusted = inkdrop_completed_import.weak_filename_import_guard(
            source,
            target,
            "comics",
            trusted_issue="1",
        )
        unproved = inkdrop_completed_import.weak_filename_import_guard(
            source,
            non_tpb_target,
            "comics",
            trusted_issue="1",
        )
    finally:
        inkdrop_completed_import.read_comicinfo = old_read
    if mismatch is not None:
        fail(f"trusted TPB title/year proof should lift missing source number: {mismatch}")
    if not trusted.get("ok"):
        fail(f"trusted TPB source without filename number should pass: {trusted}")
    if "trusted_single_issue_artifact_title" not in (trusted.get("evidence") or []):
        fail(f"trusted TPB proof evidence missing: {trusted}")
    if unproved.get("ok") or unproved.get("reason") != "trusted_issue_missing_source_number":
        fail(f"non-TPB missing-number source should remain blocked: {unproved}")


def smoke_completed_import_trusted_issue_title_state_lookup():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_completed_import
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-trusted-title-lookup-", ignore_cleanup_errors=True) as tmp:
        state_db = Path(tmp) / "inkdrop-state.sqlite3"
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-goodbye", "Goodbye, Eri", "manga", "comicvine", "151809", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-goodbye-tpb", "series-goodbye", "1", "0001", "TPB", now, now, "{}"),
            )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                ("wanted-goodbye-tpb", "series-goodbye", "issue-goodbye-tpb", "wanted", now, now, "{}"),
            )
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-fma", "Fullmetal Alchemist", "manga", "comicvine", "100", "inkdrop_series", now, now, "{}"),
            )
            for issue_id, issue_title, updated_at in (
                ("000-stale", "Brotherhood", now - 86400),
                ("zzz-current", "After The Rain", now),
            ):
                con.execute(
                    """
                    insert into issues(id,series_id,issue_number,normalized_number,title,monitored,created_at,updated_at,raw_json)
                    values(?,?,?,?,?,?,?,?,?)
                    """,
                    (issue_id, "series-fma", "7", "0007", issue_title, 1, updated_at, updated_at, "{}"),
                )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                ("wanted-fma-current", "series-fma", "zzz-current", "wanted", now, now, "{}"),
            )
            con.execute(
                """
                insert into series(id,title,media_type,metadata_provider,metadata_id,source,created_at,updated_at,raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-ambiguous", "Ambiguous", "comic", "comicvine", "200", "inkdrop_series", now, now, "{}"),
            )
            for issue_id, issue_title in (
                ("ambiguous-a", "Alpha"),
                ("ambiguous-b", "Alpha"),
                ("ambiguous-c", "Beta"),
            ):
                con.execute(
                    "insert into issues(id,series_id,issue_number,normalized_number,title,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
                    (issue_id, "series-ambiguous", "7", "0007", issue_title, 1, now, now, "{}"),
                )
                con.execute(
                    "insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                    (f"wanted-{issue_id}", "series-ambiguous", issue_id, "wanted", now, now, "{}"),
                )
            con.execute(
                """
                insert into series(id,title,media_type,metadata_provider,metadata_id,source,created_at,updated_at,raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-equivalent", "Dorohedoro", "manga", "comicvine", "32093", "inkdrop_series", now, now, "{}"),
            )
            for issue_id in ("equivalent-legacy", "equivalent-native"):
                con.execute(
                    "insert into issues(id,series_id,issue_number,normalized_number,title,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
                    (issue_id, "series-equivalent", "11", "0011", "Vol. 11", 1, now, now, "{}"),
                )
                con.execute(
                    "insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                    (f"wanted-{issue_id}", "series-equivalent", issue_id, "wanted", now, now, "{}"),
                )
        old_state_db = inkdrop_completed_import.INKDROP_STATE_DB
        try:
            inkdrop_completed_import.INKDROP_STATE_DB = state_db
            provider_title = inkdrop_completed_import.trusted_issue_title_from_inkdrop_state(
                "comicvine:151809",
                "1",
            )
            native_title = inkdrop_completed_import.trusted_issue_title_from_inkdrop_state(
                "series-goodbye",
                "0001",
            )
            missing_title = inkdrop_completed_import.trusted_issue_title_from_inkdrop_state(
                "comicvine:151809",
                "2",
            )
            current_fma_title = inkdrop_completed_import.trusted_issue_title_from_inkdrop_state(
                "comicvine:100",
                "7",
            )
            exact_current_fma_title = inkdrop_completed_import.trusted_issue_title_from_inkdrop_state(
                "comicvine:100",
                "7",
                "zzz-current",
            )
            exact_stale_fma_title = inkdrop_completed_import.trusted_issue_title_from_inkdrop_state(
                "comicvine:100",
                "7",
                "000-stale",
            )
            ambiguous_title = inkdrop_completed_import.trusted_issue_title_from_inkdrop_state(
                "comicvine:200",
                "7",
            )
            equivalent_title = inkdrop_completed_import.trusted_issue_title_from_inkdrop_state(
                "comicvine:32093",
                "11",
            )
            validated_current_title = inkdrop_completed_import.trusted_issue_title_evidence(
                "comicvine:100",
                "7",
                "zzz-current",
                "After The Rain",
            )
            rejected_stale_title = inkdrop_completed_import.trusted_issue_title_evidence(
                "comicvine:100",
                "7",
                "000-stale",
                "Brotherhood",
            )
            rejected_mismatched_title = inkdrop_completed_import.trusted_issue_title_evidence(
                "comicvine:100",
                "7",
                "zzz-current",
                "Brotherhood",
            )
        finally:
            inkdrop_completed_import.INKDROP_STATE_DB = old_state_db
    if provider_title != "TPB":
        fail(f"trusted issue title lookup did not resolve provider identity: {provider_title!r}")
    if native_title != "TPB":
        fail(f"trusted issue title lookup did not resolve native identity: {native_title!r}")
    if missing_title:
        fail(f"trusted issue title lookup should not invent missing issue titles: {missing_title!r}")
    if current_fma_title != "After The Rain" or exact_current_fma_title != "After The Rain":
        fail(f"active canonical issue title was not selected: {current_fma_title!r}, {exact_current_fma_title!r}")
    if exact_stale_fma_title:
        fail(f"inactive stale duplicate issue title was trusted: {exact_stale_fma_title!r}")
    if ambiguous_title:
        fail(f"two active same-number issue rows must fail closed: {ambiguous_title!r}")
    if equivalent_title != "Vol. 11":
        fail(f"equivalent active duplicate issue titles should retain canonical evidence: {equivalent_title!r}")
    if validated_current_title != "After The Rain":
        fail(f"exact current issue title evidence was rejected: {validated_current_title!r}")
    if rejected_stale_title or rejected_mismatched_title:
        fail(f"stale or mismatched supplied issue title crossed trust boundary: {rejected_stale_title!r}, {rejected_mismatched_title!r}")
    unsafe_duplicate = Path("Fullmetal Alchemist #007 - Brotherhood.cbz")
    duplicate_reason = inkdrop_completed_import.related_subseries_source_blocker(
        "Fullmetal Alchemist",
        unsafe_duplicate,
        issue_title=current_fma_title,
        issue_number="7",
    )
    if not duplicate_reason:
        fail("stale duplicate issue title admitted the Brotherhood production adversary")


def smoke_completed_import_trusted_tpb_lookup_survives_same_target_match():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_completed_import
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-trusted-title-target-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp) / "Goodbye, Eri (2023) (Digital) (1r0n)"
        library_root = Path(tmp) / "library" / "manga" / "Goodbye, Eri"
        root.mkdir(parents=True, exist_ok=True)
        library_root.mkdir(parents=True, exist_ok=True)
        source = root / "Goodbye, Eri (2023) (Digital) (1r0n).cbz"
        source.write_bytes(b"not a real archive")
        base_target = {
            "id": "series-goodbye",
            "title": "Goodbye, Eri",
            "year": 2023,
            "media_type": "manga",
            "aliases": ["goodbye eri"],
            "native_series_id": "comicvine:151809",
            "metadata_provider": "comicvine",
            "metadata_id": "151809",
            "folder": str(library_root),
        }
        old_load_targets = inkdrop_completed_import.load_comic_targets
        old_match_target = inkdrop_completed_import.match_comic_target
        old_lookup = inkdrop_completed_import.trusted_issue_title_from_inkdrop_state
        old_read = inkdrop_completed_import.read_comicinfo
        old_validate = inkdrop_completed_import.validate_comic_archive
        old_qbit = inkdrop_completed_import.load_qbit_incomplete_paths
        old_log = inkdrop_completed_import.log
        old_append = inkdrop_completed_import.append_manual_review
        old_state_dir = inkdrop_completed_import.STATE_DIR
        old_db_path = inkdrop_completed_import.DB_PATH
        events = []
        try:
            inkdrop_completed_import.STATE_DIR = Path(tmp) / "state"
            inkdrop_completed_import.DB_PATH = inkdrop_completed_import.STATE_DIR / "imported-files.sqlite3"
            inkdrop_completed_import.load_comic_targets = lambda series_filter=None: [dict(base_target)]
            inkdrop_completed_import.match_comic_target = lambda path, targets: dict(base_target)
            inkdrop_completed_import.trusted_issue_title_from_inkdrop_state = lambda series_id, issue, issue_id=None: "TPB"
            inkdrop_completed_import.read_comicinfo = lambda path: {}
            inkdrop_completed_import.validate_comic_archive = lambda path: {"ok": False, "reason": "smoke_stop"}
            inkdrop_completed_import.load_qbit_incomplete_paths = lambda kind: set()
            inkdrop_completed_import.log = lambda event: events.append(dict(event or {}))
            inkdrop_completed_import.append_manual_review = lambda *args, **kwargs: None
            with contextlib.redirect_stdout(io.StringIO()):
                inkdrop_completed_import.import_files(
                    "comics",
                    dry_run=True,
                    min_age_seconds=0,
                    ignore_cutoff=True,
                    matched_only=True,
                    all_series=True,
                    max_files=1,
                    source_files=[source],
                    trusted_issue="1",
                    trusted_series_id="comicvine:151809",
                    wait_for_library_scan=False,
                )
        finally:
            inkdrop_completed_import.load_comic_targets = old_load_targets
            inkdrop_completed_import.match_comic_target = old_match_target
            inkdrop_completed_import.trusted_issue_title_from_inkdrop_state = old_lookup
            inkdrop_completed_import.read_comicinfo = old_read
            inkdrop_completed_import.validate_comic_archive = old_validate
            inkdrop_completed_import.load_qbit_incomplete_paths = old_qbit
            inkdrop_completed_import.log = old_log
            inkdrop_completed_import.append_manual_review = old_append
            inkdrop_completed_import.STATE_DIR = old_state_dir
            inkdrop_completed_import.DB_PATH = old_db_path
    weak_events = [event for event in events if event.get("event") == "skip_weak_filename_import_guard"]
    if weak_events:
        fail(f"trusted TPB lookup was dropped before weak filename guard: {weak_events[-1]}")
    bad_archive_events = [event for event in events if event.get("event") == "skip_bad_comic_archive"]
    if not bad_archive_events:
        fail(f"trusted TPB same-target import did not pass filename guard before archive check: {events}")


def smoke_direct_import_unit_gate_uses_trusted_tpb_source_evidence():
    with tempfile.TemporaryDirectory(prefix="inkdrop-tpb-direct-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        source_dir = root / "Goodbye, Eri (2023) (Digital) (1r0n)"
        source_dir.mkdir(parents=True, exist_ok=True)
        source = source_dir / "Goodbye, Eri (2023) (Digital) (1r0n).cbz"
        source.write_bytes(b"tpb source placeholder")
        dest = "/library/manga/Goodbye, Eri (2023)/Goodbye, Eri c001.cbz"
        state_db = root / "inkdrop-state.sqlite3"
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, year, metadata_provider, metadata_id, source, library_path, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "series-goodbye",
                    "Goodbye, Eri",
                    "manga",
                    2023,
                    "comicvine",
                    "151809",
                    "inkdrop_series",
                    "/library/manga/Goodbye, Eri",
                    now,
                    now,
                    "{}",
                ),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-goodbye-tpb", "series-goodbye", "1", "0001", "TPB", now, now, "{}"),
            )
            queue = {
                "id": "queue-goodbye",
                "series_id": "series-goodbye",
                "issue_id": "issue-goodbye-tpb",
            }
            raw = {
                "trusted_issue": "1",
                "imported_file_source_path": str(source),
                "imported_file_dest_path": dest,
            }
            gate = inkdrop_state.direct_import_destination_unit_gate(con, queue, dest, raw)
            if gate:
                fail(f"trusted TPB source evidence should prevent wrong-unit quarantine: {gate}")
            con.execute("update issues set title='Chapter One' where id='issue-goodbye-tpb'")
            non_tpb_gate = inkdrop_state.direct_import_destination_unit_gate(con, queue, dest, raw)
            if not non_tpb_gate or non_tpb_gate.get("reason") != "trusted_issue_missing_source_number":
                fail(f"non-TPB missing-number source should remain gated: {non_tpb_gate}")


def smoke_ready_import_records_threads_issue_title():
    try:
        import inkdrop_reconcile_imports
    except FileNotFoundError as exc:
        if "inkdrop_reconcile_imports.py" in str(exc):
            return
        raise
    with tempfile.TemporaryDirectory(prefix="inkdrop-ready-issue-title-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        source = root / "Goodbye, Eri (2023) (Digital) (1r0n).cbz"
        source.write_bytes(b"tpb archive placeholder")
        state_db = root / "inkdrop-state.sqlite3"
        reconcile_db = root / "imported-files.sqlite3"
        now = time.time()
        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into series(id, title, media_type, metadata_provider, metadata_id, source, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                ("series-goodbye", "Goodbye, Eri", "manga", "comicvine", "151809", "inkdrop_series", now, now, "{}"),
            )
            con.execute(
                """
                insert into issues(id, series_id, issue_number, normalized_number, title, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("issue-goodbye-tpb", "series-goodbye", "1", "1", "TPB", now, now, "{}"),
            )
            con.execute(
                """
                insert into wanted_items(id, series_id, issue_id, reason, status, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                ("wanted-goodbye", "series-goodbye", "issue-goodbye-tpb", "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                """
                insert into queue_items(id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active, created_at, updated_at, raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "queue-goodbye",
                    "wanted-goodbye",
                    "series-goodbye",
                    "issue-goodbye-tpb",
                    "importing",
                    "download_client",
                    "Goodbye, Eri 1 2023",
                    "completed in client",
                    1,
                    now,
                    now,
                    "{}",
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id, queue_id, wanted_id, series_id, issue_id, source, provider, protocol, download_client,
                    external_id, title, status, state, local_path, retry_eligible, started_at, updated_at, completed_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "task-goodbye",
                    "queue-goodbye",
                    "wanted-goodbye",
                    "series-goodbye",
                    "issue-goodbye-tpb",
                    "download_client",
                    "qbit",
                    "torrent",
                    "qBittorrent",
                    "hash-goodbye",
                    "Goodbye, Eri (2023) (Digital) (1r0n)",
                    "completed_in_client",
                    "import_ready",
                    str(source),
                    0,
                    now,
                    now,
                    now,
                    "{}",
                ),
            )
        old_db = inkdrop_reconcile_imports.DB_PATH
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        patched = {}
        for name in (
            "recover_retryable_failed_staged_import_ready_records",
            "sync_inkdrop_import_ready_records",
            "sync_reconciliation_from_inkdrop_import_results",
            "recover_import_ready_timeouts_from_imported_files",
            "sync_inkdrop_from_reconciled_imports",
            "promote_complete_deferred_import_ready_records",
            "recover_failed_filename_guard_import_ready_records",
        ):
            patched[name] = getattr(inkdrop_reconcile_imports, name)
            setattr(inkdrop_reconcile_imports, name, lambda *args, **kwargs: {})
        old_qbit = inkdrop_reconcile_imports.imp.load_qbit_incomplete_paths
        try:
            inkdrop_reconcile_imports.DB_PATH = reconcile_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.imp.load_qbit_incomplete_paths = lambda kind: set()
            reconcile_db.touch()
            inkdrop_reconcile_imports.ensure_reconciliation_table()
            conn = inkdrop_reconcile_imports.connect_db()
            try:
                conn.execute("create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)")
                conn.execute(
                    """
                    insert into download_reconciliation(
                        pending_key, title, query, protocol, client, client_id, trusted_series_id, trusted_issue,
                        inkdrop_queue_id, inkdrop_download_task_id, lifecycle_state, reason, matched_local_path,
                        matched_series, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "pending-goodbye",
                        "Goodbye, Eri (2023) (Digital) (1r0n)",
                        "Goodbye, Eri 1 2023",
                        "torrent",
                        "qbit",
                        "hash-goodbye",
                        "comicvine:151809",
                        "1",
                        "queue-goodbye",
                        "task-goodbye",
                        "ready_to_import",
                        "completed_in_client",
                        str(source),
                        "Goodbye, Eri",
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            records = inkdrop_reconcile_imports.ready_import_records(1)
        finally:
            inkdrop_reconcile_imports.DB_PATH = old_db
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.imp.load_qbit_incomplete_paths = old_qbit
            for name, value in patched.items():
                setattr(inkdrop_reconcile_imports, name, value)
    if not records:
        fail("ready import records did not include queue-owned TPB source")
    if records[0].get("trusted_issue_title") != "TPB":
        fail(f"ready import record did not thread issue title: {records[0]}")
    if records[0].get("trusted_issue_id") != "issue-goodbye-tpb":
        fail(f"ready import record did not thread exact issue identity: {records[0]}")


def smoke_completed_import_allows_duplicate_chapter_token_filename():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    try:
        import inkdrop_completed_import
    except FileNotFoundError as exc:
        if "inkdrop_completed_import.py" in str(exc):
            return
        raise
    old_read = inkdrop_completed_import.read_comicinfo
    old_db_path = inkdrop_completed_import.DB_PATH
    old_state_dir = inkdrop_completed_import.STATE_DIR
    try:
        with tempfile.TemporaryDirectory(prefix="inkdrop-gantz-chapter-token-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            inkdrop_completed_import.STATE_DIR = root / "state"
            inkdrop_completed_import.DB_PATH = inkdrop_completed_import.STATE_DIR / "imported-files.sqlite3"
            target = {
                "title": "Gantz: E",
                "series": "Gantz: E",
                "folder": str(root / "Manga" / "Gantz E"),
                "year": 2020,
                "media_type": "manga",
                "metadata_provider": "mangadex",
                "metadata_id": "mangadex:gantz-e",
                "source": "mangadex",
                "target_source": "inkdrop_series",
                "aliases": ["gantz e"],
            }
            duplicate_token = Path("/tmp/Gantz E - Chapter 72 - Ch.72 - A Strange Coincidence.cbz")
            true_range = Path("/tmp/Gantz E - Chapter 72-73.cbz")
            inkdrop_completed_import.read_comicinfo = lambda path: None
            duplicate_result = inkdrop_completed_import.weak_filename_import_guard(
                duplicate_token,
                target,
                "comics",
                trusted_issue="72",
            )
            range_result = inkdrop_completed_import.weak_filename_import_guard(
                true_range,
                target,
                "comics",
                trusted_issue="72",
            )
            trusted_chapter_unit = inkdrop_completed_import.manga_import_guard(
                duplicate_token,
                target,
                auto_learn=False,
                trusted_issue="72",
            )
            untrusted_chapter_unit = inkdrop_completed_import.manga_import_guard(
                duplicate_token,
                target,
                auto_learn=False,
            )
    finally:
        inkdrop_completed_import.read_comicinfo = old_read
        inkdrop_completed_import.DB_PATH = old_db_path
        inkdrop_completed_import.STATE_DIR = old_state_dir
    if not duplicate_result.get("ok"):
        fail(f"duplicate chapter token filename should pass trusted source-file guard: {duplicate_result}")
    if range_result.get("ok") or range_result.get("reason") != "pack_candidate_requires_pack_handling":
        fail(f"real chapter range should still require pack handling: {range_result}")
    if not trusted_chapter_unit.get("allowed") or trusted_chapter_unit.get("series_unit_model") != "chapter":
        fail(f"trusted explicit chapter should auto-learn chapter unit model: {trusted_chapter_unit}")
    auto_set = trusted_chapter_unit.get("auto_set_unit_model") or {}
    if auto_set.get("manga_unit_model") != "chapter" or auto_set.get("source") != "auto_explicit_chapter_import":
        fail(f"trusted explicit chapter auto-learn evidence missing: {trusted_chapter_unit}")
    if untrusted_chapter_unit.get("allowed") or untrusted_chapter_unit.get("reason") != "manga_chapter_requires_chapter_unit_model":
        fail(f"untrusted explicit chapter should still require manual unit-model decision: {untrusted_chapter_unit}")


def smoke_exact_volume_import_does_not_use_ambiguous_existing_file_as_duplicate():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    import inkdrop_completed_import

    old_values = {
        "STATE_DIR": inkdrop_completed_import.STATE_DIR,
        "DB_PATH": inkdrop_completed_import.DB_PATH,
        "COMIC_ROOT": inkdrop_completed_import.COMIC_ROOT,
        "MANGA_ROOT": inkdrop_completed_import.MANGA_ROOT,
        "load_comic_targets": inkdrop_completed_import.load_comic_targets,
        "match_comic_target": inkdrop_completed_import.match_comic_target,
        "read_comicinfo": inkdrop_completed_import.read_comicinfo,
        "validate_comic_archive": inkdrop_completed_import.validate_comic_archive,
        "load_qbit_incomplete_paths": inkdrop_completed_import.load_qbit_incomplete_paths,
        "log": inkdrop_completed_import.log,
        "append_manual_review": inkdrop_completed_import.append_manual_review,
    }
    events = []
    volume_events = []
    try:
        with tempfile.TemporaryDirectory(prefix="inkdrop-exact-volume-existing-order-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            manga_root = root / "Manga"
            series_dir = manga_root / "Dorohedoro"
            staging_dir = root / "staging"
            series_dir.mkdir(parents=True)
            staging_dir.mkdir()
            ambiguous_existing = series_dir / "Dorohedoro v06 c006.cbz"
            canonical_existing = series_dir / "Dorohedoro Volume 6.cbz"
            source = staging_dir / "Dorohedoro v06.cbz"
            for archive_path in (ambiguous_existing, source):
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr("001.jpg", b"page")
            target = {
                "id": None,
                "title": "Dorohedoro",
                "series": "Dorohedoro",
                "folder": str(series_dir),
                "media_type": "manga",
                "target_source": "inkdrop_series",
                "issue_number": "6",
                "normalized_number": "6",
                "issue_title": "Vol. 6",
                "query": "Dorohedoro Vol. 6",
                "manga_unit_policy": "volume_and_chapter",
                "aliases": ["dorohedoro"],
            }
            inkdrop_completed_import.STATE_DIR = root / "state"
            inkdrop_completed_import.DB_PATH = inkdrop_completed_import.STATE_DIR / "imported-files.sqlite3"
            inkdrop_completed_import.COMIC_ROOT = root / "Comics"
            inkdrop_completed_import.MANGA_ROOT = manga_root
            inkdrop_completed_import.load_comic_targets = lambda series_filter=None: [dict(target)]
            inkdrop_completed_import.match_comic_target = lambda path, targets: dict(target)
            inkdrop_completed_import.read_comicinfo = lambda path: {}
            inkdrop_completed_import.validate_comic_archive = lambda path, **kwargs: (
                {"ok": True, "page_count": 1}
                if Path(path) in {ambiguous_existing, canonical_existing}
                else {"ok": False, "reason": "smoke_stop_after_destination_planning"}
            )
            inkdrop_completed_import.load_qbit_incomplete_paths = lambda kind: set()
            inkdrop_completed_import.log = lambda event: events.append(dict(event or {}))
            inkdrop_completed_import.append_manual_review = lambda *args, **kwargs: None
            with contextlib.redirect_stdout(io.StringIO()):
                inkdrop_completed_import.import_files(
                    "comics",
                    dry_run=True,
                    min_age_seconds=0,
                    ignore_cutoff=True,
                    matched_only=True,
                    all_series=True,
                    max_files=1,
                    source_files=[source],
                    trusted_issue="6",
                    wait_for_library_scan=False,
                )
            volume_events = list(events)
            with zipfile.ZipFile(canonical_existing, "w") as archive:
                archive.writestr("001.jpg", b"canonical volume page")
            exact_identity = inkdrop_completed_import.exact_manga_volume_import_identity(source, target)
            canonical_guard = inkdrop_completed_import.manga_import_guard(
                source,
                target,
                auto_learn=False,
                trusted_issue="6",
                exact_volume_identity=exact_identity,
            )
            if canonical_guard.get("existing_path") != str(canonical_existing):
                fail(f"canonical exact volume stopped protecting against duplicates: {canonical_guard}")
            canonical_existing.unlink()
            target_without_policy = dict(target)
            target_without_policy.pop("manga_unit_policy")
            inkdrop_completed_import.set_manga_unit_model(
                "Dorohedoro",
                "chapter",
                source="smoke_persisted_chapter_policy",
            )
            exact_identity_without_payload_policy = inkdrop_completed_import.exact_manga_volume_import_identity(
                source,
                target_without_policy,
            )
            chapter_only_guard = inkdrop_completed_import.manga_import_guard(
                source,
                target_without_policy,
                auto_learn=False,
                trusted_issue="6",
                exact_volume_identity=exact_identity_without_payload_policy,
            )
            if (
                chapter_only_guard.get("allowed")
                or chapter_only_guard.get("completed")
                or chapter_only_guard.get("reason") != "manga_volume_requires_volume_or_mixed_unit_model"
            ):
                fail(f"exact volume bypassed persisted chapter-only policy: {chapter_only_guard}")
            events.clear()
            inkdrop_completed_import.match_comic_target = lambda path, targets: dict(target_without_policy)
            with contextlib.redirect_stdout(io.StringIO()):
                inkdrop_completed_import.import_files(
                    "comics",
                    dry_run=True,
                    min_age_seconds=0,
                    ignore_cutoff=True,
                    matched_only=True,
                    all_series=True,
                    max_files=1,
                    source_files=[source],
                    trusted_issue="6",
                    wait_for_library_scan=False,
                )
            chapter_policy_events = [
                event
                for event in events
                if event.get("event") == "skip_manga_unit_guard"
                and event.get("skip_reason") == "manga_volume_requires_volume_or_mixed_unit_model"
            ]
            if not chapter_policy_events:
                fail(f"importer ordering bypassed persisted chapter-only policy: {events}")
    finally:
        for name, value in old_values.items():
            setattr(inkdrop_completed_import, name, value)
    duplicate_events = [
        event
        for event in volume_events
        if event.get("event") == "skip_manga_unit_guard"
        and event.get("skip_reason") == "already_verified_duplicate"
    ]
    if duplicate_events:
        fail(f"ambiguous vNN cNN artifact suppressed an exact volume import: {duplicate_events[-1]}")
    planned = [
        event for event in volume_events if event.get("skip_reason") == "smoke_stop_after_destination_planning"
    ]
    if not planned or Path(planned[-1].get("dest") or "").name != "Dorohedoro v06.cbz":
        fail(f"exact volume did not reach canonical destination planning: {volume_events}")


def smoke_slskd_manga_completion_requires_durable_exact_path_and_settles_wanted():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    import inkdrop_completed_import

    old_db_path = inkdrop_completed_import.DB_PATH
    old_state_dir = inkdrop_completed_import.STATE_DIR
    old_comic_root = inkdrop_completed_import.COMIC_ROOT
    old_manga_root = inkdrop_completed_import.MANGA_ROOT
    try:
        with tempfile.TemporaryDirectory(prefix="inkdropslskdmangacompletion", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp) / "fixture" / "identity"
            state_dir = root / "state"
            manga_root = root / "Manga"
            series_dir = manga_root / "Snotgirl"
            series_dir.mkdir(parents=True)
            dest = series_dir / "Snotgirl Volume 5.cbz"

            inkdrop_completed_import.STATE_DIR = state_dir
            inkdrop_completed_import.DB_PATH = state_dir / "imported-files.sqlite3"
            inkdrop_completed_import.COMIC_ROOT = root / "Comics"
            inkdrop_completed_import.MANGA_ROOT = manga_root
            state_dir.mkdir()
            conn = inkdrop_completed_import.connect()
            try:
                inkdrop_completed_import.ensure_manga_unit_schema(conn)
                conn.execute(
                    """
                    insert into manga_coverage(
                        series_title,normalized_series,unit_type,normalized_number,source_quality,
                        truth_model,target_file_path,kavita_visibility_status,verification_status,
                        completed_at,updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "Snotgirl", "snotgirl", "volume", "5", "exact", "kavita_manga",
                        str(series_dir), "library_visible", "library_visible", time.time(), time.time(),
                    ),
                )
                conn.execute(
                    "insert into manga_series_unit_model(normalized_series,series_title,manga_unit_model,source,updated_at) values(?,?,?,?,?)",
                    ("snotgirl", "Snotgirl", "mixed_volume_preferred", "watch", time.time()),
                )
                conn.commit()
            finally:
                conn.close()

            target = {
                "title": "Snotgirl",
                "series": "Snotgirl",
                "folder": str(series_dir),
                "media_type": "manga",
                "aliases": ["snotgirl"],
            }
            staged = root / "staging" / "Snotgirl Volume 5.cbz"
            stale_guard = inkdrop_completed_import.manga_import_guard(
                staged, target, auto_learn=False, trusted_issue="5"
            )
            if not stale_guard.get("allowed") or stale_guard.get("completed"):
                fail(f"pathless/directory coverage suppressed a fresh exact artifact: {stale_guard}")

            dest.write_bytes(b"not a zip archive")
            conn = inkdrop_completed_import.connect()
            try:
                conn.execute(
                    "update manga_coverage set target_file_path=? where normalized_series='snotgirl' and normalized_number='5'",
                    (str(dest),),
                )
                conn.commit()
            finally:
                conn.close()
            corrupt_guard = inkdrop_completed_import.manga_import_guard(
                staged, target, auto_learn=False, trusted_issue="5"
            )
            if not corrupt_guard.get("allowed") or corrupt_guard.get("completed"):
                fail(f"corrupt managed CBZ suppressed a fresh exact artifact: {corrupt_guard}")

            dest.unlink()
            with zipfile.ZipFile(dest, "w") as archive:
                archive.writestr("001.jpg", b"validated manga volume page")
            conn = inkdrop_completed_import.connect()
            try:
                conn.execute(
                    "update manga_coverage set target_file_path=? where normalized_series='snotgirl' and normalized_number='5'",
                    (str(dest),),
                )
                conn.commit()
            finally:
                conn.close()
            durable_guard = inkdrop_completed_import.manga_import_guard(
                staged, target, auto_learn=False, trusted_issue="5"
            )
            if durable_guard.get("allowed") or durable_guard.get("existing_path") != str(dest):
                fail(f"durable exact manga volume was not returned as duplicate proof: {durable_guard}")

            db_path = state_dir / "inkdrop-state.sqlite3"
            queue_item = {
                "key": "queue-snotgirl-5",
                "series": "Snotgirl",
                "issue": "5",
                "issue_title": "Vol. 5",
                "query": "Snotgirl Vol. 5",
                "state": "importing",
                "media_type": "manga",
                "manga_unit_model": "mixed_volume_preferred",
                "autopilot_queue_key": "queue-snotgirl-5",
                "queue_identity": "comicvine:12345",
                "updated_at": time.time() - 60,
            }
            (state_dir / "series-autopilot-queue.json").write_text(
                json.dumps({"items": {"queue-snotgirl-5": queue_item}}), encoding="utf-8"
            )
            resolved_actions = {
                "manual_source_resolved": [{
                        "review_id": "review-snotgirl-5",
                        "series": "Snotgirl",
                        "issue": "5",
                        "autopilot_queue_key": "queue-snotgirl-5",
                        "queue_identity": "comicvine:12345",
                        "source_path": str(staged),
                        "source_filename": staged.name,
                        "destinations": [str(dest)],
                        "already_present_count": 1,
                        "manga_unit_model": "mixed_volume_preferred",
                        "slskd_transfer_id": "slskd-job-snotgirl-5",
                        "verification": {"checked": [{"dest": str(dest), "verification_status": "library_visible"}]},
                        "ts": time.time(),
                    }]
            }
            (state_dir / "manual-review-actions.json").write_text(
                json.dumps({"manual_source_resolved": []}), encoding="utf-8"
            )
            with inkdrop_state.connect(db_path) as con:
                inkdrop_state.init_schema(con)
                for key, value in (
                    ("media_management.manga_root", str(manga_root)),
                    ("media_management.comic_root", str(root / "Comics")),
                ):
                    con.execute(
                        "insert into app_settings(key,scope,label,value_json,source,updated_at) values(?,?,?,?,?,?)",
                        (key, "system", key, json.dumps(value), "test", time.time()),
                    )
                inkdrop_state.sync_queue(con, state_dir, time.time())
                seeded = con.execute(
                    "select series_id,issue_id,wanted_id from queue_items where id='queue-snotgirl-5'"
                ).fetchone()
                missing_dest = series_dir / "Snotgirl Volume 5 missing.cbz"
                con.execute(
                    """
                    insert into import_results(
                        id,queue_id,series_id,issue_id,source_path,dest_path,status,verified,
                        completion_truth,folder_imported,created_at,raw_json
                    ) values(?,?,?,?,?,?,'queue_verified',1,'folder',1,?,?)
                    """,
                    (
                        "stale-import-snotgirl-5", "queue-snotgirl-5", seeded["series_id"], seeded["issue_id"],
                        str(missing_dest), str(missing_dest), time.time() - 120,
                        json.dumps({"kind": "stale_missing_destination"}),
                    ),
                )
                (state_dir / "manual-review-actions.json").write_text(
                    json.dumps(resolved_actions), encoding="utf-8"
                )
                inkdrop_state.sync_queue(con, state_dir, time.time())
                inkdrop_state.sync_queue(con, state_dir, time.time())
                queue = con.execute(
                    "select state,active from queue_items where id='queue-snotgirl-5'"
                ).fetchone()
                wanted = con.execute(
                    "select status from wanted_items where issue_id=(select issue_id from queue_items where id='queue-snotgirl-5')"
                ).fetchone()
                imported = con.execute(
                    "select verified,raw_json from import_results where queue_id='queue-snotgirl-5' and dest_path=?",
                    (str(dest),),
                ).fetchone()
                stale = con.execute(
                    "select status,verified from import_results where id='stale-import-snotgirl-5'"
                ).fetchone()
                verified_count = con.execute(
                    "select count(*) from import_results where queue_id='queue-snotgirl-5' and verified=1"
                ).fetchone()[0]
            if tuple(queue or ()) != ("verified", 0) or not wanted or wanted[0] != "satisfied":
                fail(f"authoritative exact manga completion did not settle queue/wanted: {queue}, {wanted}")
            if not imported or imported[0] != 1:
                fail(f"authoritative exact manga completion did not create verified import proof: {imported}")
            if tuple(stale or ()) != ("stale_verified_proof_superseded", 0):
                fail(f"stale missing-destination proof was not atomically superseded: {stale}")
            if verified_count != 1:
                fail(f"replayed exact resolution created duplicate verified proofs: {verified_count}")
            if json.loads(imported[1]).get("resolved_row", {}).get("slskd_transfer_id") != "slskd-job-snotgirl-5":
                fail("verified import proof lost the authoritative SLSKD external job id")
    finally:
        inkdrop_completed_import.DB_PATH = old_db_path
        inkdrop_completed_import.STATE_DIR = old_state_dir
        inkdrop_completed_import.COMIC_ROOT = old_comic_root
        inkdrop_completed_import.MANGA_ROOT = old_manga_root


def smoke_import_authority_fences_callbacks_and_releases_exact_task():
    if "requests" not in sys.modules:
        sys.modules["requests"] = types.SimpleNamespace()
    import inkdrop_reconcile_imports

    with tempfile.TemporaryDirectory(prefix="inkdrop-import-authority-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_db = root / "inkdrop-state.sqlite3"
        library = root / "Comics"
        staging = root / "Staging"
        library.mkdir()
        staging.mkdir()
        source = staging / "Authority Series #001.cbz"
        dest = library / "Authority Series #001.cbz"
        for archive_path in (source, dest):
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("001.png", valid_large_png())
        now = time.time()

        def add_queue(con, suffix, *, sibling=False):
            issue_id = f"issue-{suffix}"
            wanted_id = f"wanted-{suffix}"
            queue_id = f"queue-{suffix}"
            attempt_id = f"attempt-{suffix}"
            task_id = f"task-{suffix}"
            con.execute(
                "insert into issues(id,series_id,issue_number,normalized_number,title,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
                (issue_id, "series-authority", "1", "1", "Authority", now, now, "{}"),
            )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,reason,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
                (wanted_id, "series-authority", issue_id, "missing", "in_progress", now, now, "{}"),
            )
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,issue_id,state,current_source,query,last_event,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                (queue_id, wanted_id, "series-authority", issue_id, "importing", "slskd", "Authority Series 1", "ready", 1, now, now, "{}"),
            )
            con.execute(
                """
                insert into source_attempts(
                    id,queue_id,wanted_id,series_id,issue_id,source,provider,protocol,download_client,
                    candidate_identity,lifecycle_phase,outcome,display_phase,retry_eligible,status,title,
                    started_at,completed_at,raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt_id, queue_id, wanted_id, "series-authority", issue_id, "slskd", "SLSKD", "slskd", "slskd",
                    f"candidate-{suffix}", "completed", "success", "completed", 0, "completed", str(source), now, now, "{}",
                ),
            )
            con.execute(
                """
                insert into download_tasks(
                    id,queue_id,wanted_id,series_id,issue_id,source_attempt_id,source,provider,protocol,
                    download_client,external_id,candidate_identity,title,status,state,lifecycle_phase,
                    retry_eligible,local_path,started_at,updated_at,completed_at,raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id, queue_id, wanted_id, "series-authority", issue_id, attempt_id, "slskd", "SLSKD", "slskd",
                    "slskd", f"transfer-{suffix}", f"candidate-{suffix}", "Authority Series #001", "staged_file_ready",
                    "import_ready", "import_ready", 0, str(source), now, now, now, "{}",
                ),
            )
            if sibling:
                con.execute(
                    """
                    insert into download_tasks(
                        id,queue_id,wanted_id,series_id,issue_id,source,provider,protocol,download_client,
                        external_id,candidate_identity,title,status,state,lifecycle_phase,retry_eligible,
                        local_path,started_at,updated_at,raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"task-{suffix}-sibling", queue_id, wanted_id, "series-authority", issue_id,
                        "prowlarr", "Prowlarr", "torrent", "qbittorrent", f"torrent-{suffix}",
                        f"candidate-{suffix}-sibling", "Authority Series #001", "download_started", "downloading",
                        "downloading", 0, str(staging / f"{suffix}-sibling.cbz"), now + 10, now + 10, "{}",
                    ),
                )
            return queue_id, wanted_id, attempt_id, task_id

        with inkdrop_state.connect(state_db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                "insert into app_settings(key,scope,label,value_json,description,source,updated_at) values(?,?,?,?,?,?,?)",
                ("media_management.comic_root", "media_management", "Comic Root", json.dumps(str(library)), "root", "smoke", now),
            )
            con.execute(
                "insert into series(id,title,media_type,library_path,metadata_provider,metadata_id,source,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
                ("series-authority", "Authority Series", "comic", str(library), "comicvine", "9001", "smoke", now, now, "{}"),
            )
            exact = add_queue(con, "exact")
            sibling_case = add_queue(con, "sibling", sibling=True)
            retry_case = add_queue(con, "retry")
            rollback_case = add_queue(con, "rollback")
            verification_pending_case = add_queue(con, "verification-pending")
            blank_source_case = add_queue(con, "blank-source")
            blank_external_case = add_queue(con, "blank-external")
            blank_candidate_case = add_queue(con, "blank-candidate")
            con.execute("update download_tasks set source_attempt_id=null where id=?", (blank_source_case[3],))
            con.execute("update download_tasks set external_id='' where id=?", (blank_external_case[3],))
            con.execute("update download_tasks set candidate_identity='' where id=?", (blank_candidate_case[3],))
            con.commit()

        def claim(case):
            queue_id, _wanted_id, attempt_id, task_id = case
            result = inkdrop_state.claim_import_authority(
                state_db,
                queue_id,
                task_id,
                source_attempt_id=attempt_id,
                external_id=f"transfer-{task_id.removeprefix('task-')}",
                candidate_identity=f"candidate-{task_id.removeprefix('task-')}",
                download_client="slskd",
                local_path=str(source),
                claimed_at=now + 20,
            )
            if not result.get("ok"):
                fail(f"exact import authority claim failed: {result}")
            return result["authority"]

        taskless = inkdrop_state.record_direct_import_result(
            state_db,
            retry_case[0],
            source_path=str(source),
            dest_path=str(dest),
            source="slskd",
            status="folder_verified",
            verified=True,
            imported_count=1,
            raw={"trusted_issue": "1"},
            created_at=now + 19,
        )
        if taskless.get("ok") or taskless.get("reason") != "import_authority_missing":
            fail(f"task-backed callback could masquerade as a taskless import: {taskless}")

        for blank_case, missing_field, expected_reason in (
            (blank_source_case, "source_attempt_id", "import_claim_source_attempt_missing"),
            (blank_external_case, "external_id", "import_claim_external_id_missing"),
            (blank_candidate_case, "candidate_identity", "import_claim_candidate_identity_missing"),
        ):
            queue_id, _wanted_id, attempt_id, task_id = blank_case
            claim_values = {
                "source_attempt_id": attempt_id,
                "external_id": f"transfer-{task_id.removeprefix('task-')}",
                "candidate_identity": f"candidate-{task_id.removeprefix('task-')}",
            }
            claim_values[missing_field] = ""
            incomplete_claim = inkdrop_state.claim_import_authority(
                state_db,
                queue_id,
                task_id,
                download_client="slskd",
                local_path=str(source),
                claimed_at=now + 20,
                **claim_values,
            )
            if incomplete_claim.get("ok") or incomplete_claim.get("reason") != expected_reason:
                fail(f"blank authority identity was accepted ({missing_field}): {incomplete_claim}")

        callback_without_authority = {
            "inkdrop_queue_id": exact[0],
            "inkdrop_download_task_id": exact[3],
            "inkdrop_source_attempt_id": exact[2],
            "inkdrop_external_id": "transfer-exact",
            "inkdrop_candidate_identity": "candidate-exact",
            "inkdrop_download_client": "slskd",
            "inkdrop_task_local_path": str(source),
            "client": "slskd",
            "source_file": str(source),
        }
        with sqlite3.connect(state_db) as con:
            preclaim_before = (
                con.execute("select state,current_source,raw_json from queue_items where id=?", (exact[0],)).fetchone(),
                con.execute("select state,status,raw_json from download_tasks where id=?", (exact[3],)).fetchone(),
                con.execute("select status from wanted_items where id=?", (exact[1],)).fetchone(),
            )
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            missing_authority_result = inkdrop_reconcile_imports.record_inkdrop_import_attempt(
                callback_without_authority,
                {"imported": [{"source": str(source), "dest": str(dest)}]},
                returncode=0,
            )
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
        if missing_authority_result.get("ok") or missing_authority_result.get("reason") != "import_authority_missing":
            fail(f"callback without preclaimed authority was accepted: {missing_authority_result}")
        with sqlite3.connect(state_db) as con:
            preclaim_after = (
                con.execute("select state,current_source,raw_json from queue_items where id=?", (exact[0],)).fetchone(),
                con.execute("select state,status,raw_json from download_tasks where id=?", (exact[3],)).fetchone(),
                con.execute("select status from wanted_items where id=?", (exact[1],)).fetchone(),
            )
        if preclaim_after != preclaim_before:
            fail(f"callback without authority mutated lifecycle state: before={preclaim_before} after={preclaim_after}")

        authority = claim(exact)
        with sqlite3.connect(state_db) as con:
            before = (
                con.execute("select state,current_source,raw_json from queue_items where id=?", (exact[0],)).fetchone(),
                con.execute("select state,status,external_id,source_attempt_id,raw_json from download_tasks where id=?", (exact[3],)).fetchone(),
                con.execute("select status from wanted_items where id=?", (exact[1],)).fetchone(),
                con.execute("select count(*) from import_results where queue_id=?", (exact[0],)).fetchone()[0],
            )
        callback_record = {
            "inkdrop_queue_id": exact[0],
            "inkdrop_download_task_id": exact[3],
            "inkdrop_source_attempt_id": exact[2],
            "inkdrop_external_id": "transfer-exact",
            "inkdrop_candidate_identity": "candidate-exact",
            "inkdrop_download_client": "slskd",
            "inkdrop_task_local_path": str(source),
            "client": "slskd",
            "source_file": str(source),
            "import_authority": authority,
        }
        old_state = inkdrop_reconcile_imports.INKDROP_STATE_DB
        old_module = inkdrop_reconcile_imports.inkdrop_state
        try:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = state_db
            inkdrop_reconcile_imports.inkdrop_state = inkdrop_state
            callback_conflict = inkdrop_reconcile_imports.record_inkdrop_import_attempt(
                {**callback_record, "inkdrop_download_task_id": retry_case[3]},
                {"imported": [{"source": str(source), "dest": str(dest)}]},
                returncode=0,
            )
            required_callback_fields = {
                "inkdrop_queue_id": "queue_id_missing",
                "inkdrop_download_task_id": "import_callback_download_task_id_missing",
                "inkdrop_source_attempt_id": "import_callback_source_attempt_id_missing",
                "inkdrop_external_id": "import_callback_external_id_missing",
                "inkdrop_candidate_identity": "import_callback_candidate_identity_missing",
                "inkdrop_download_client": "import_callback_download_client_missing",
                "inkdrop_task_local_path": "import_callback_local_path_missing",
                "client": "import_callback_download_client_missing",
                "source_file": "import_callback_source_path_missing",
            }
            for missing_field, expected_reason in required_callback_fields.items():
                incomplete = dict(callback_record)
                incomplete.pop(missing_field)
                incomplete_result = inkdrop_reconcile_imports.record_inkdrop_import_attempt(
                    incomplete,
                    {"imported": [{"source": str(source), "dest": str(dest)}]},
                    returncode=0,
                )
                if incomplete_result.get("ok") or incomplete_result.get("reason") != expected_reason:
                    fail(f"incomplete callback envelope was accepted ({missing_field}): {incomplete_result}")
        finally:
            inkdrop_reconcile_imports.INKDROP_STATE_DB = old_state
            inkdrop_reconcile_imports.inkdrop_state = old_module
        if callback_conflict.get("ok") or callback_conflict.get("reason") != "import_callback_download_task_id_mismatch":
            fail(f"callback identity was replaced by issued authority: {callback_conflict}")
        conflicts = (
            ({**authority, "download_task_id": retry_case[3]}, str(source), "import_authority_task_missing"),
            ({**authority, "external_id": "substituted-transfer"}, str(source), "import_authority_external_id_mismatch"),
            ({**authority, "source_attempt_id": "attempt-other"}, str(source), "import_authority_source_attempt_id_mismatch"),
            ({**authority, "candidate_identity": "candidate-other"}, str(source), "import_authority_candidate_identity_mismatch"),
            (authority, str(staging / "other.cbz"), "import_authority_callback_path_mismatch"),
            (authority, str(source).swapcase(), "import_authority_callback_path_mismatch"),
        )
        for conflicting_authority, callback_path, expected_reason in conflicts:
            result = inkdrop_state.record_direct_import_result(
                state_db,
                exact[0],
                source_path=callback_path,
                dest_path=str(dest),
                source="slskd",
                status="folder_verified",
                verified=True,
                imported_count=1,
                raw={"import_ready_bridge": True, "trusted_issue": "1"},
                import_authority=conflicting_authority,
                created_at=now + 30,
            )
            if result.get("ok") or result.get("reason") != expected_reason:
                fail(f"conflicting import callback was not fenced: {result}")
        with sqlite3.connect(state_db) as con:
            after_conflicts = (
                con.execute("select state,current_source,raw_json from queue_items where id=?", (exact[0],)).fetchone(),
                con.execute("select state,status,external_id,source_attempt_id,raw_json from download_tasks where id=?", (exact[3],)).fetchone(),
                con.execute("select status from wanted_items where id=?", (exact[1],)).fetchone(),
                con.execute("select count(*) from import_results where queue_id=?", (exact[0],)).fetchone()[0],
            )
        if after_conflicts != before:
            fail(f"rejected callback changed authoritative state: before={before} after={after_conflicts}")

        verified = inkdrop_state.record_direct_import_result(
            state_db,
            exact[0],
            source_path=str(source),
            dest_path=str(dest),
            source="slskd",
            status="folder_verified",
            verified=True,
            imported_count=1,
            raw={"import_ready_bridge": True, "trusted_issue": "1"},
            import_authority=authority,
            created_at=now + 40,
        )
        if not verified.get("ok") or verified.get("state") != "verified":
            fail(f"valid exact import authority did not verify: {verified}")
        repeated = inkdrop_state.record_direct_import_result(
            state_db,
            exact[0],
            source_path=str(source),
            dest_path=str(dest),
            source="slskd",
            status="folder_verified",
            verified=True,
            imported_count=1,
            raw={"import_ready_bridge": True, "trusted_issue": "1"},
            import_authority=authority,
            created_at=now + 41,
        )
        if repeated.get("ok") or repeated.get("reason") not in {"import_authority_queue_stale", "queue_already_verified"}:
            fail(f"spent import authority was replayable: {repeated}")
        with sqlite3.connect(state_db) as con:
            wanted = con.execute("select status from wanted_items where id=?", (exact[1],)).fetchone()
            import_count = con.execute("select count(*) from import_results where queue_id=?", (exact[0],)).fetchone()[0]
        if wanted != ("satisfied",) or import_count != 1:
            fail(f"valid import did not clear exactly one Wanted row: wanted={wanted} imports={import_count}")

        sibling_authority = claim(sibling_case)
        sibling_release = inkdrop_state.release_import_authority(
            state_db, sibling_authority, reason="simulated_import_failure", released_at=now + 50
        )
        if not sibling_release.get("ok") or not sibling_release.get("active_sibling_preserved"):
            fail(f"failed exact task did not preserve active sibling: {sibling_release}")
        retry_authority = claim(retry_case)
        retry_release = inkdrop_state.release_import_authority(
            state_db, retry_authority, reason="simulated_import_failure", released_at=now + 50
        )
        repeated_release = inkdrop_state.release_import_authority(
            state_db, retry_authority, reason="simulated_import_failure", released_at=now + 51
        )
        with sqlite3.connect(state_db) as con:
            sibling_queue = con.execute("select state,current_source from queue_items where id=?", (sibling_case[0],)).fetchone()
            sibling_wanted = con.execute("select status from wanted_items where id=?", (sibling_case[1],)).fetchone()
            sibling_task = con.execute("select state,status,external_id from download_tasks where id=?", (f"task-sibling-sibling",)).fetchone()
            retry_queue = con.execute("select state,current_source from queue_items where id=?", (retry_case[0],)).fetchone()
            retry_wanted = con.execute("select status from wanted_items where id=?", (retry_case[1],)).fetchone()
            task_count = con.execute("select count(*) from download_tasks").fetchone()[0]
        if sibling_queue != ("downloading", "qbittorrent") or sibling_wanted != ("downloading",):
            fail(f"active sibling ownership was not preserved: queue={sibling_queue} wanted={sibling_wanted}")
        if sibling_task != ("downloading", "download_started", "torrent-sibling"):
            fail(f"active sibling was modified by exact-task retirement: {sibling_task}")
        if not retry_release.get("ok") or retry_queue != ("queued", None) or retry_wanted != ("wanted",):
            fail(f"terminal exact task did not return Wanted to normal retry: {retry_release} {retry_queue} {retry_wanted}")
        if repeated_release.get("ok") or task_count != 9:
            fail(f"release was not idempotent or created a duplicate task: {repeated_release} count={task_count}")

        verification_authority = claim(verification_pending_case)
        verification_result = inkdrop_state.record_direct_import_result(
            state_db,
            verification_pending_case[0],
            source_path=str(source),
            dest_path="",
            source="slskd",
            status="verification_pending",
            verified=False,
            imported_count=0,
            skipped_count=0,
            raw={"import_ready_bridge": True, "trusted_issue": "1"},
            import_authority=verification_authority,
            created_at=now + 55,
        )
        if not verification_result.get("ok"):
            fail(f"verification-pending result did not persist: {verification_result}")
        with sqlite3.connect(state_db) as con:
            verification_queue = con.execute(
                "select state,raw_json from queue_items where id=?", (verification_pending_case[0],)
            ).fetchone()
            verification_task = con.execute(
                "select state,status,raw_json from download_tasks where id=?", (verification_pending_case[3],)
            ).fetchone()
        if verification_queue[0] != "importing" or verification_task[:2] != ("importing", "verification_pending"):
            fail(f"verification-pending lifecycle state was not retained: {verification_queue} {verification_task}")
        if "import_authority" in json.loads(verification_queue[1] or "{}") or "import_authority" in json.loads(verification_task[2] or "{}"):
            fail("completed import execution retained an active authority lease while waiting for verification")
        drained_pending = inkdrop_state.recover_active_import_authorities(state_db)
        if not drained_pending.get("ok") or drained_pending.get("found") != 0:
            fail(f"verification-pending result left rollback-blocking authority: {drained_pending}")

        rollback_authority = claim(rollback_case)
        rollback_recovery = inkdrop_state.recover_active_import_authorities(state_db)
        repeated_recovery = inkdrop_state.recover_active_import_authorities(state_db)
        with sqlite3.connect(state_db) as con:
            rollback_queue = con.execute(
                "select state,raw_json from queue_items where id=?", (rollback_case[0],)
            ).fetchone()
            rollback_task = con.execute(
                "select state,status,raw_json from download_tasks where id=?", (rollback_case[3],)
            ).fetchone()
        if rollback_recovery != {
            "ok": True, "found": 1, "recovered": 1, "failed": 0, "failures": [], "remaining": []
        }:
            fail(f"rollback precondition did not recover exact authority: {rollback_recovery}")
        if repeated_recovery != {
            "ok": True, "found": 0, "recovered": 0, "failed": 0, "failures": [], "remaining": []
        }:
            fail(f"rollback authority recovery was not idempotent: {repeated_recovery}")
        if rollback_queue[0] != "importing" or "import_authority" in json.loads(rollback_queue[1]):
            fail(f"rollback recovery left stale queue authority: {rollback_queue}")
        if rollback_task[:2] != ("import_ready", "staged_file_ready") or "import_authority" in json.loads(rollback_task[2]):
            fail(f"rollback recovery did not return exact task to staged retry: {rollback_task}")
        if not rollback_authority.get("token"):
            fail("rollback test did not begin with a durable authority token")

        with sqlite3.connect(state_db) as con:
            task_raw = json.loads(con.execute(
                "select raw_json from download_tasks where id=?", (rollback_case[3],)
            ).fetchone()[0])
            task_raw["import_authority"] = rollback_authority
            con.execute(
                "update download_tasks set raw_json=? where id=?",
                (json.dumps(task_raw), rollback_case[3]),
            )
            con.commit()
        blocked_recovery = inkdrop_state.recover_active_import_authorities(state_db)
        if blocked_recovery.get("ok") or blocked_recovery.get("failed") != 1 or len(blocked_recovery.get("remaining") or []) != 1:
            fail(f"rollback recovery did not fail closed with unresolved authority: {blocked_recovery}")
        if rollback_authority["token"] in json.dumps(blocked_recovery):
            fail("rollback recovery exposed the raw authority token instead of a fingerprint")


def smoke_completed_client_import_gets_turn_after_manual_source_cycle():
    import inkdrop_completed_import

    with tempfile.TemporaryDirectory(prefix="inkdrop-import-fairness-") as tmp:
        root = Path(tmp)
        actions_path = root / "manual-review-actions.json"
        status_path = root / "manual-source-autoresolve-status.json"
        old_paths = (
            inkdrop_completed_import.MANUAL_REVIEW_ACTIONS_FILE,
            inkdrop_completed_import.MANUAL_SOURCE_AUTORESOLVE_STATUS_FILE,
        )
        try:
            inkdrop_completed_import.MANUAL_REVIEW_ACTIONS_FILE = actions_path
            inkdrop_completed_import.MANUAL_SOURCE_AUTORESOLVE_STATUS_FILE = status_path
            actions_path.write_text(
                json.dumps({"manual_source_waiting": {"waiting-review": {"ts": time.time()}}}),
                encoding="utf-8",
            )
            status_path.write_text(
                json.dumps(
                    {
                        "state": "watching",
                        "eligible_count": 21,
                        "ready_detected_count": 22,
                        "updated_at": time.time() - 601,
                    }
                ),
                encoding="utf-8",
            )
            if inkdrop_completed_import.manual_source_priority_waiting() is not None:
                fail("completed-client import remained blocked by stale SLSKD detections")

            status_path.write_text(
                json.dumps(
                    {
                        "state": "importing",
                        "eligible_count": 2,
                        "ready_detected_count": 2,
                        "updated_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )
            active = inkdrop_completed_import.manual_source_priority_waiting() or {}
            if active.get("reason") != "manual_source_autoresolve_ready":
                fail(f"active manual-source import lost its bounded turn: {active}")

            status_path.write_text(
                json.dumps(
                    {
                        "state": "importing",
                        "eligible_count": 2,
                        "ready_detected_count": 2,
                        "updated_at": time.time() - 901,
                    }
                ),
                encoding="utf-8",
            )
            if inkdrop_completed_import.manual_source_priority_waiting() is not None:
                fail("stale manual-source activity retained global import priority")
        finally:
            (
                inkdrop_completed_import.MANUAL_REVIEW_ACTIONS_FILE,
                inkdrop_completed_import.MANUAL_SOURCE_AUTORESOLVE_STATUS_FILE,
            ) = old_paths


def main():
    reconcile_text = RECONCILE.read_text(encoding="utf-8")
    completed_text = COMPLETED_IMPORT.read_text(encoding="utf-8")
    state_text = STATE.read_text(encoding="utf-8")
    worker_text = WORKER.read_text(encoding="utf-8")
    tree = ast.parse(reconcile_text, filename=str(RECONCILE))
    ceiling = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "IMPORT_READY_MAX_FILES":
                    ceiling = ast.literal_eval(node.value)
    if not isinstance(ceiling, int):
        fail("IMPORT_READY_MAX_FILES constant is missing")
    if ceiling <= 5:
        fail(f"import-ready batch ceiling stayed too low: {ceiling}")
    if "def bounded_import_ready_max" not in reconcile_text:
        fail("bounded_import_ready_max helper is missing")
    if f"min(requested, IMPORT_READY_MAX_FILES)" not in reconcile_text:
        fail("bounded import-ready helper does not clamp to the configured ceiling")
    if not re.search(r"import_ready\(bounded_import_ready_max\(args\.max_files\)\)", reconcile_text):
        fail("import-ready CLI path does not use the bounded batch helper")
    if "INKDROP_IMPORT_READY_IMPORT_TIMEOUT_SECONDS" not in reconcile_text:
        fail("import-ready per-file timeout is not operator-configurable")
    if "INKDROP_IMPORT_READY_BATCH_TIMEOUT_SECONDS" not in reconcile_text:
        fail("import-ready batch timeout is not operator-configurable")
    if "INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS = env_float" not in reconcile_text:
        fail("reconciled import replay budget is not operator-configurable")
    if "INKDROP_IMPORT_READY_QUEUE_ONLY" not in reconcile_text or "IMPORT_READY_QUEUE_ONLY" not in reconcile_text:
        fail("import-ready queue-only automation flag is missing")
    if "archive_matches_issue(path, issue_number, filename_only=True)" not in reconcile_text:
        fail("queue-owned import-ready sync must use filename-only issue matching before import validation")
    for item in [
        "INKDROP_PACK_FANOUT_MAX_ROWS",
        "INKDROP_PACK_FANOUT_MAX_CREATED",
        "INKDROP_PACK_FANOUT_LOCK_RETRY_ATTEMPTS",
        "pack_fanout_snapshot_priority",
        "completed.sort(key=lambda pair: pack_fanout_snapshot_priority(pair[0]))",
        "inkdrop_state.connect_read",
        "configure_wal=False",
        "q.state in ('queued','source_wait','searching','downloading','importing')",
        "INKDROP_IMPORT_READY_MAX_PER_BROAD_PACK_PER_BATCH",
        "inkdrop_state_schema_present",
        "import_ready_batch_priority",
        "import_ready_broad_pack_key",
        "broad_pack_counts[broad_pack_key]",
        "import_ready_client_priority",
        "BENIGN_IMPORT_SKIP_TOKENS",
        "record_inkdrop_import_ready_rejection",
        "record_inkdrop_import_ready_deferral",
        "promote_complete_deferred_import_ready_records",
        "source_file_incomplete_qbit_download",
        "load_qbit_incomplete_paths(\"comics\")",
    ]:
        if item not in reconcile_text:
            fail(f"completed pack fanout is missing weekly-pack drain safeguard: {item}")
    if "source_file_incomplete_qbit_download" not in completed_text or "skip_incomplete_qbit_file" not in completed_text:
        fail("completed importer does not report exact-source qbit incomplete skips")
    for item in [
        "INKDROP_IMPORT_READY_APPLY_PLANNED_PATH",
        "--apply-planned-path",
        "--trusted-issue-title",
        "trusted_issue_title",
        "--trusted-issue-id",
        "trusted_issue_id",
    ]:
        if item not in reconcile_text:
            fail(f"import-ready worker does not expose managed-path import policy: {item}")
    for item in [
        "export INKDROP_IMPORT_READY_APPLY_PLANNED_PATH",
        "apply_planned_path=$INKDROP_IMPORT_READY_APPLY_PLANNED_PATH",
        "export INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS",
        "reconciled_import_sync_budget_seconds=$INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS",
    ]:
        if item not in worker_text:
            fail(f"import-ready shell worker does not surface managed-path policy: {item}")
    for item in [
        "def media_management_import_preview",
        "--apply-planned-path",
        "apply_planned_path=True if args.apply_planned_path else None",
        "args.trusted_issue_title",
        "args.trusted_issue_id",
        '"media_management_destination_decision"',
        '"apply_planned_path_override"',
        '"media_management_preview"',
        '"current_import_dest_matches_preview"',
    ]:
        if item not in completed_text:
            fail(f"completed importer does not expose media-management preview dry-run contract: {item}")
    if "batch_budget_exhausted" not in reconcile_text or "skipped_remaining_count" not in reconcile_text:
        fail("import-ready batch budget result fields are missing")
    if "except subprocess.TimeoutExpired" not in reconcile_text:
        fail("import-ready per-file timeout is not handled durably")
    if "mark_reconciled_import_timeout" not in reconcile_text or "record_inkdrop_import_timeout" not in reconcile_text:
        fail("import-ready timeout is not recorded back to reconciliation and InkDrop state")
    if "recover_import_ready_timeouts_from_imported_files" not in reconcile_text:
        fail("import-ready timeout recovery from imported-files proof is missing")
    if "timeout_recovered" not in reconcile_text:
        fail("import-ready timeout recovery does not annotate recovered download tasks")
    if "recover_active_import_ready_from_imported_files" not in reconcile_text:
        fail("active import-ready rows are not recovered from imported-files proof")
    if "record_inkdrop_import_ready_rejection" not in reconcile_text:
        fail("import-ready rejected client files are not recorded back to InkDrop state")
    if "def with_sqlite_lock_retry" not in reconcile_text:
        fail("SQLite lock retry helper is missing")
    if "with_sqlite_lock_retry(" not in reconcile_text or "record_direct_import_result" not in reconcile_text:
        fail("import-ready InkDrop state write-back is not covered by SQLite lock retry")
    if "def sync_inkdrop_from_reconciled_imports" not in reconcile_text:
        fail("reconciliation-to-InkDrop replay helper is missing")
    if "inkdrop_replay_sync" not in reconcile_text:
        fail("import-ready result does not report reconciliation-to-InkDrop replay")
    if "record_stale_completion_retraction_history(stale_completion_retraction)" not in reconcile_text:
        fail("stale completion cleanup is not recorded into InkDrop history")
    if '"stale_completion_history": stale_completion_history' not in reconcile_text:
        fail("reconciliation-to-InkDrop replay result does not expose stale completion history status")
    if "collection_target_single_part_block_reason" not in reconcile_text:
        fail("local completed pack fanout does not apply the edition single-part guard")
    if "def import_ready_child_env" not in reconcile_text or "INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE" not in reconcile_text:
        fail("import-ready child import does not defer broad completed-import status sync")
    if "env=import_ready_child_env()" not in reconcile_text:
        fail("import-ready subprocess is missing the deferred-sync child environment")
    if "sys.executable" not in reconcile_text or not re.search(r"cmd\s*=\s*\[\s*sys\.executable,\s*str\(IMPORTER_PATH\)", reconcile_text):
        fail("import-ready subprocess must launch the importer through the active Python interpreter")
    if "def import_status_sync_deferred" not in completed_text:
        fail("completed import status writer does not expose deferred sync guard")
    if "def import_status_queue_backed_source_file_child" not in completed_text:
        fail("completed import status writer does not defer queue-backed source-file child sync by shape")
    if "inkdrop_import_status_sync_deferred" not in completed_text:
        fail("completed import status writer does not log deferred sync")
    if "def pending_import_result_pack_candidates" not in completed_text:
        fail("pack reverify does not consume pending local-pack import_results")
    if "import_result_rows" not in completed_text:
        fail("pack reverify summary does not expose import_result candidate rows")
    for item in [
        "INKDROP_STATE_READ_TIMEOUT_SECONDS",
        "INKDROP_STATE_WRITE_TIMEOUT_SECONDS",
        "read_timeout_seconds=INKDROP_STATE_READ_TIMEOUT_SECONDS",
        "lock_timeout_seconds=INKDROP_STATE_WRITE_TIMEOUT_SECONDS",
    ]:
        if item not in reconcile_text:
            fail(f"import-ready write-back is missing short-timeout guard: {item}")
    for item in [
        "read_timeout_seconds=None",
        "lock_timeout_seconds=None",
        "def queue_item(db_path, queue_id, *, read_only=False",
        "connect_read(",
    ]:
        if item not in state_text:
            fail(f"InkDrop direct import state writer is missing short-timeout support: {item}")

    text = WORKER.read_text(encoding="utf-8")
    required = [
        "INKDROP_IMPORT_READY_MAX_FILES",
        "INKDROP_IMPORT_READY_LOCK_WAIT_SECONDS",
        "INKDROP_IMPORT_READY_RECONCILE_LOCK_WAIT_SECONDS",
        "INKDROP_IMPORT_READY_RECONCILE_TIMEOUT_SECONDS",
        "INKDROP_IMPORT_READY_RECONCILE_CLIENTS",
        "INKDROP_IMPORT_READY_IMPORT_TIMEOUT_SECONDS",
        "INKDROP_IMPORT_READY_BATCH_TIMEOUT_SECONDS",
        "INKDROP_IMPORT_READY_QUEUE_ONLY",
        "import_timeout_seconds=",
        "batch_timeout_seconds=",
        "queue_only=",
        "reconcile_lock_wait_seconds=",
        "run_quick_download_client_reconcile",
        "--download-clients",
        "--skip-download-clients",
        "--lock-wait-seconds \"$RECONCILE_LOCK_WAIT_SECONDS\"",
        "state_lock_mode=import_lock_only",
        "uses shared lock",
        "waits briefly for its own DB lock",
        "$LOCK_DIR/inkdrop-comics-import.lock",
        "flock -w",
        "/usr/bin/timeout",
        '--max-files "$MAX_FILES"',
    ]
    missing = [item for item in required if item not in text]
    if missing:
        fail("worker script is missing expected lock/batch safeguards: " + ", ".join(missing))
    quick_call_pos = text.rfind("\nrun_quick_download_client_reconcile\n")
    import_call_pos = text.find("--skip-download-clients --import-ready")
    if quick_call_pos < 0 or import_call_pos < 0 or not quick_call_pos < import_call_pos:
        fail("quick download-client reconcile must run before import-ready processing")
    if import_call_pos < 0:
        fail("long import-ready command must skip download-client reconcile after the quick preflight")
    for blocked in [
        "acquire_state_lock",
        "INKDROP_STATE_WRITER_LOCK_WAIT_SECONDS",
        "skipping import-ready pass",
        "/tmp/inkdrop-series-autopilot.lock",
        "/tmp/inkdrop-series-status-refresh.lock",
    ]:
        if blocked in text:
            fail(f"worker still waits on broad state writer locks before importing: {blocked}")
    if "--download-clients --download-client sabnzbd --import-ready" in text:
        fail("worker still combines download-client reconcile with long import-ready processing")

    smoke_import_result_state_uses_library_neutral_statuses()
    smoke_direct_import_short_timeout_writer()
    smoke_import_authority_fences_callbacks_and_releases_exact_task()
    smoke_reconcile_lock_waits_then_reports_busy()
    smoke_reconciliation_replay_to_inkdrop()
    smoke_reconciliation_replay_skips_missing_import_destination()
    smoke_reconciliation_replay_settles_suppressed_existing_path()
    smoke_import_ready_sync_preserves_suppressed_existing_path()
    smoke_hash_suppression_preserves_managed_destination()
    smoke_replay_identity_accepts_explicit_manga_volume_file()
    smoke_reconciliation_replay_uses_queue_identity_for_imported_file_proof()
    smoke_verified_manga_import_results_backfill_completion_tables()
    smoke_stale_completion_retraction_records_history()
    smoke_import_ready_rejection_requeues()
    smoke_failed_import_attempt_requeues_import_ready_download()
    smoke_import_ready_records_existing_planned_destination()
    smoke_import_ready_timeout_recovers_imported_file()
    smoke_queue_backed_ready_import_skips_duplicate_prevalidation()
    smoke_failed_filename_guard_recovery_is_queue_authoritative()
    smoke_import_ready_runs_state_sync_before_record_selection()
    smoke_ready_import_defers_qbit_incomplete_source_files()
    smoke_ready_import_accepts_valid_child_from_incomplete_pack()
    smoke_import_ready_deferral_updates_inkdrop_task_state()
    smoke_import_ready_promotion_restores_completed_qbit_source_files()
    smoke_queue_only_ready_import_skips_unowned_rows()
    smoke_completed_pack_download_client_rows_are_import_ready()
    smoke_completed_slskd_staged_source_rows_are_import_ready()
    smoke_local_completed_pack_replay_creates_import_ready_row()
    smoke_local_completed_pack_replay_defers_qbit_incomplete_archive()
    smoke_import_ready_classifier_accepts_cached_string_paths()
    smoke_import_ready_timeout_continues()
    smoke_import_ready_uses_planned_path_by_default_with_opt_out()
    smoke_import_ready_child_defers_broad_import_status_sync()
    smoke_deferred_import_statuses_are_lossless()
    smoke_active_import_ready_recovers_from_imported_file_proof()
    smoke_active_import_ready_rejects_mismatched_imported_file_proof()
    smoke_imported_path_must_match_trusted_target()
    smoke_queue_owned_target_classifies_without_adapter_target()
    smoke_queue_owned_one_word_manga_chapter_classifies_exact_unit()
    smoke_import_target_accepts_safe_leading_article_alias()
    smoke_import_ready_pack_priority_helpers()
    smoke_import_ready_skip_result_classification()
    smoke_completed_import_reports_incomplete_qbit_source_file()
    smoke_completed_import_reports_media_management_preview()
    smoke_completed_import_applies_media_management_path_when_enabled()
    smoke_exact_manga_volume_plans_and_imports_volume_only_destination()
    smoke_completed_import_trusted_issue_lifts_filename_guard()
    smoke_completed_import_trusted_tpb_lifts_missing_number_guard()
    smoke_completed_import_trusted_issue_title_state_lookup()
    smoke_completed_import_trusted_tpb_lookup_survives_same_target_match()
    smoke_direct_import_unit_gate_uses_trusted_tpb_source_evidence()
    smoke_ready_import_records_threads_issue_title()
    smoke_completed_import_allows_duplicate_chapter_token_filename()
    smoke_exact_volume_import_does_not_use_ambiguous_existing_file_as_duplicate()
    smoke_slskd_manga_completion_requires_durable_exact_path_and_settles_wanted()
    smoke_retryable_failed_staged_source_recovery_promotes_only_importable_files()
    smoke_completed_client_import_gets_turn_after_manual_source_cycle()

    print("IMPORT_READY_WORKER_OK: import-ready worker uses import lock without broad state-lock waits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
