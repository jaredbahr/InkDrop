#!/usr/bin/env python3
"""Focused regression checks for bounded queue synchronization."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import inkdrop_state
import inkdrop_container_scheduler


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def queue_item(now):
    return {
        "key": "queue:performance:1",
        "series": "Performance Smoke",
        "issue": "1",
        "query": "Performance Smoke 1",
        "state": "queued",
        "last_event": "queued for performance smoke",
        "source_order": ["prowlarr", "slskd"],
        "created_at": now,
        "updated_at": now,
        "attempts": [
            {
                "source": "prowlarr",
                "provider": "Smoke Indexer",
                "status": "searched_no_candidates",
                "ts": now + offset,
            }
            for offset in range(12)
        ],
    }


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-queue-performance-", ignore_cleanup_errors=True) as tmp:
        state_dir = Path(tmp)
        db_path = state_dir / "inkdrop-state.sqlite3"
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            inkdrop_state.init_schema(con)
            con.commit()

        now = time.time()
        payload = {"schema_version": 2, "items": {"queue:performance:1": queue_item(now)}}
        (state_dir / "series-autopilot-queue.json").write_text(json.dumps(payload), encoding="utf-8")

        original_refresh = inkdrop_state.refresh_queue_provider_status_columns
        refreshes = []
        try:
            inkdrop_state.refresh_queue_provider_status_columns = lambda con, queue_id: refreshes.append(queue_id) or 0
            with inkdrop_state.connect(db_path) as con:
                count = inkdrop_state.sync_queue(
                    con,
                    state_dir,
                    now,
                    verify_filesystem=False,
                    perform_backfills=False,
                )
                con.commit()
        finally:
            inkdrop_state.refresh_queue_provider_status_columns = original_refresh
        check(count == 1, f"expected one queue row, got {count}")
        check(not refreshes, f"bulk source attempts refreshed per row: {refreshes}")

        # Queue ingestion may preserve an already-verified row when its ledger
        # proof exists, but it must not run archive checks or promote a queued
        # row. Full/integrity maintenance owns those filesystem decisions.
        proof_calls = []
        original_verified_import = inkdrop_state.verified_import_for_queue
        try:
            inkdrop_state.verified_import_for_queue = (
                lambda *args, **kwargs: proof_calls.append(kwargs.get("require_existing_destination"))
                or {"id": "fixture-proof", "created_at": now}
            )
            with inkdrop_state.connect(db_path) as con:
                inkdrop_state.sync_queue(
                    con,
                    state_dir,
                    now + 1,
                    verify_filesystem=False,
                    perform_backfills=False,
                )
                row = con.execute(
                    "select state from queue_items where id='queue:performance:1'"
                ).fetchone()
                con.commit()
        finally:
            inkdrop_state.verified_import_for_queue = original_verified_import
        check(not proof_calls, f"queue-only sync inspected a proof for an active queued row: {proof_calls}")
        check(row and row["state"] == "queued", f"queue-only sync promoted a ledger row: {dict(row) if row else None}")

        verified_item = queue_item(now)
        verified_item["state"] = "verified"
        (state_dir / "series-autopilot-queue.json").write_text(
            json.dumps({"schema_version": 2, "items": {"queue:performance:1": verified_item}}),
            encoding="utf-8",
        )
        proof_calls = []
        try:
            inkdrop_state.verified_import_for_queue = (
                lambda *args, **kwargs: proof_calls.append(kwargs.get("require_existing_destination"))
                or {"id": "fixture-proof", "created_at": now}
            )
            with inkdrop_state.connect(db_path) as con:
                inkdrop_state.sync_queue(
                    con,
                    state_dir,
                    now + 2,
                    verify_filesystem=False,
                    perform_backfills=False,
                )
                row = con.execute(
                    "select state from queue_items where id='queue:performance:1'"
                ).fetchone()
                con.commit()
        finally:
            inkdrop_state.verified_import_for_queue = original_verified_import
        check(proof_calls == [False], f"verified queue row requested an archive check: {proof_calls}")
        check(row and row["state"] == "verified", f"queue-only sync lost an existing verified proof: {dict(row) if row else None}")

        (state_dir / "series-autopilot-queue.json").write_text(json.dumps(payload), encoding="utf-8")
        queue_completion_names = (
            "settle_queue_items_with_verified_imports",
            "reconcile_verified_queue_wanted_statuses",
            "reconcile_duplicate_issue_number_wanted_statuses",
            "reconcile_monitored_metadata_only_wanted",
            "reconcile_retired_duplicate_wanted_enrollment",
        )
        original_queue_completion = {
            name: getattr(inkdrop_state, name) for name in queue_completion_names
        }
        try:
            for name in queue_completion_names:
                setattr(
                    inkdrop_state,
                    name,
                    lambda *args, _name=name, **kwargs: (_ for _ in ()).throw(
                        AssertionError(f"queue-only sync ran completion reconciliation: {_name}")
                    ),
                )
            queue_result = inkdrop_state.sync_queue_state(
                state_dir,
                db_path,
                mode="queue",
                include_summary=False,
                lock_attempts=1,
            )
        finally:
            for name, callback in original_queue_completion.items():
                setattr(inkdrop_state, name, callback)
        check(queue_result.get("ok"), f"queue-only state sync failed: {queue_result}")
        check(queue_result.get("sync_mode") == "queue", f"wrong queue sync mode: {queue_result}")
        check(
            queue_result.get("synced", {}).get("verified_import_queue_settled") == 0,
            f"queue-only sync settled completion records: {queue_result}",
        )
        for key in (
            "verified_queue_wanted_reconciled",
            "duplicate_issue_wanted_reconciled",
            "metadata_only_wanted_queued",
            "retired_duplicate_wanted_reenrolled",
        ):
            check(
                queue_result.get("synced", {}).get(key) == 0,
                f"queue-only sync ran {key}: {queue_result}",
            )

        heavy_names = (
            "sync_download_reconciliation",
            "cleanup_missing_folder_verified_import_proofs",
            "cleanup_wrong_unit_page_pack_import_proofs",
            "backfill_existing_folder_presence_import_results",
            "backfill_existing_folder_presence_wanted_items",
            "sync_managed_media_files",
        )
        originals = {name: getattr(inkdrop_state, name) for name in heavy_names}
        try:
            for name in heavy_names:
                setattr(
                    inkdrop_state,
                    name,
                    lambda *args, _name=name, **kwargs: (_ for _ in ()).throw(
                        AssertionError(f"heavy stage ran during maintenance: {_name}")
                    ),
                )
            result = inkdrop_state.sync_queue_state(
                state_dir,
                db_path,
                mode="maintenance",
                include_summary=False,
                lock_attempts=1,
            )
        finally:
            for name, callback in originals.items():
                setattr(inkdrop_state, name, callback)
        check(result.get("ok"), f"maintenance failed: {result}")
        check(result.get("sync_mode") == "maintenance", f"wrong mode: {result}")
        check(result.get("synced", {}).get("queue_items") == 0, f"maintenance ingested JSON: {result}")
        check("queue_maintenance" in (result.get("timings") or {}), f"timings missing: {result}")
        check("total" in (result.get("timings") or {}), f"total timing missing: {result}")

        jobs = {job.name: job for job in inkdrop_container_scheduler.build_jobs()}
        check("verified-import-projection" in jobs, "scheduled verified-import projection is missing")
        check("queue-maintenance" in jobs, "scheduled queue maintenance is missing")
        check("incremental-state-reconciliation" in jobs, "scheduled incremental reconciliation is missing")
        check("deep-state-integrity-audit" in jobs, "scheduled deep integrity audit is missing")
        check("maintenance" in jobs["queue-maintenance"].command, "maintenance job uses the wrong mode")
        check("projection" in jobs["verified-import-projection"].command, "projection job uses the wrong mode")
        check(jobs["verified-import-projection"].timeout_seconds <= 60, "projection can monopolize a worker slot")
        check("queue" in jobs["incremental-state-reconciliation"].command, "incremental reconciliation uses the wrong mode")
        check(jobs["incremental-state-reconciliation"].timeout_seconds <= 300, "incremental reconciliation can monopolize a worker slot")
        check(not jobs["incremental-state-reconciliation"].critical, "incremental reconciliation outranks acquisition work")
        check("integrity" in jobs["deep-state-integrity-audit"].command, "deep integrity audit waits behind broad ingestion")
        check(jobs["deep-state-integrity-audit"].command[-1] == "100", "deep integrity audit batch is not bounded")
        check(jobs["deep-state-integrity-audit"].timeout_seconds <= 300, "deep integrity audit can monopolize a worker slot")
        check(not jobs["deep-state-integrity-audit"].critical, "deep integrity audit outranks acquisition work")
        maintenance_source = (Path(__file__).resolve().parents[1] / "inkdrop_state_maintenance.py").read_text(encoding="utf-8")
        check('"full"' in maintenance_source, "manual full reconciliation capability was removed")

        integrity_names = (
            "cleanup_missing_folder_verified_import_proofs",
            "cleanup_wrong_unit_page_pack_import_proofs",
            "cleanup_contradictory_completion_provenance",
        )
        integrity_originals = {name: getattr(inkdrop_state, name) for name in integrity_names}
        original_sync_queue = inkdrop_state.sync_queue
        integrity_calls = []
        try:
            inkdrop_state.sync_queue = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("integrity audit ran broad queue ingestion")
            )
            for name in integrity_names:
                setattr(
                    inkdrop_state,
                    name,
                    lambda *args, _name=name, **kwargs: integrity_calls.append((_name, kwargs.get("limit"))) or 0,
                )
            integrity = inkdrop_state.sync_completion_integrity(db_path, limit=7, lock_attempts=1)
        finally:
            inkdrop_state.sync_queue = original_sync_queue
            for name, callback in integrity_originals.items():
                setattr(inkdrop_state, name, callback)
        check(integrity.get("ok") and integrity.get("sync_mode") == "integrity", integrity)
        check(integrity_calls == [(name, 7) for name in integrity_names], integrity_calls)

        progress_calls = {name: [] for name in integrity_names}
        progress_originals = {name: getattr(inkdrop_state, name) for name in integrity_names}
        try:
            for name in integrity_names:
                def stage(*args, _name=name, **kwargs):
                    cursor = dict(kwargs.get("cursor") or {})
                    progress_calls[_name].append(cursor)
                    if not cursor:
                        return {"retracted": 0, "_progress": {"examined": 100, "cursor": {"created_at": 100, "id": f"{_name}:100"}}}
                    return {"retracted": 1, "_progress": {"examined": 2, "cursor": None}}
                setattr(inkdrop_state, name, stage)
            first_integrity = inkdrop_state.sync_completion_integrity(db_path, limit=100, lock_attempts=1)
            second_integrity = inkdrop_state.sync_completion_integrity(db_path, limit=100, lock_attempts=1)
        finally:
            for name, callback in progress_originals.items():
                setattr(inkdrop_state, name, callback)
        for result in first_integrity["synced"].values():
            check(result["cursor_advanced"] and result["retracted"] == 0, first_integrity)
        for result in second_integrity["synced"].values():
            check(not result["cursor_advanced"] and result["retracted"] == 1, second_integrity)
        for name in integrity_names:
            check(progress_calls[name] == [{}, {"created_at": 100, "id": f"{name}:100"}], progress_calls)

        original_run = inkdrop_container_scheduler.inkdrop_process_lifecycle.run_tracked
        try:
            inkdrop_container_scheduler.inkdrop_process_lifecycle.run_tracked = (
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired(args[0] if args else "projection", kwargs.get("timeout", 0))
                )
            )
            check(
                inkdrop_container_scheduler.run_job(jobs["verified-import-projection"]) == 124,
                "scheduler did not release a timed-out projection slot",
            )
        finally:
            inkdrop_container_scheduler.inkdrop_process_lifecycle.run_tracked = original_run

        print("QUEUE_SYNC_PERFORMANCE_SMOKE_OK")


if __name__ == "__main__":
    main()
