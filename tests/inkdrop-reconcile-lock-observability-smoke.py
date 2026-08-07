#!/usr/bin/env python3
"""Regression coverage for failed-download reconciliation lock evidence."""

import contextlib
import json
import sqlite3
import tempfile
import threading
from pathlib import Path

from core import inkdrop_reconcile_imports as reconcile
from core import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def seed_state_db(path):
    with inkdrop_state.connect(path) as con:
        inkdrop_state.init_schema(con)
        con.execute(
            "insert into series(id, title, created_at, updated_at) values(?, ?, ?, ?)",
            ("series-1", "Lock Test", 1.0, 1.0),
        )
        con.execute(
            "insert into issues(id, series_id, issue_number, normalized_number, created_at, updated_at) values(?, ?, ?, ?, ?, ?)",
            ("issue-1", "series-1", "7", "7", 1.0, 1.0),
        )
        con.execute(
            "insert into queue_items(id, series_id, issue_id, state, query, active, created_at, updated_at) values(?, ?, ?, ?, ?, ?, ?, ?)",
            ("queue-1", "series-1", "issue-1", "downloading", "Lock Test 7", 1, 1.0, 1.0),
        )
        con.commit()


def assert_query_only_retry_without_schema_ddl(root):
    state_db = root / "state.sqlite3"
    seed_state_db(state_db)
    original_db = reconcile.INKDROP_STATE_DB
    original_connect_read = reconcile.inkdrop_state.connect_read
    original_init_schema = reconcile.inkdrop_state.init_schema
    writer_ready = threading.Event()
    release_writer = threading.Event()

    def writer():
        con = sqlite3.connect(state_db)
        try:
            con.execute("begin immediate")
            con.execute("update queue_items set updated_at=2 where id='queue-1'")
            writer_ready.set()
            release_writer.wait(5)
            con.commit()
        finally:
            con.close()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    require(writer_ready.wait(5), "concurrent writer did not acquire its transaction")
    calls = {"count": 0}

    @contextlib.contextmanager
    def contended_connect_read(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            threading.Timer(0.15, release_writer.set).start()
            raise sqlite3.OperationalError("database is locked")
        with original_connect_read(*args, **kwargs) as con:
            require(con.execute("pragma query_only").fetchone()[0] == 1, "lookup connection must be query-only")
            yield con

    def forbidden_init_schema(_con):
        raise AssertionError("failed-download lookup must not initialize schema")

    try:
        reconcile.INKDROP_STATE_DB = state_db
        reconcile.inkdrop_state.connect_read = contended_connect_read
        reconcile.inkdrop_state.init_schema = forbidden_init_schema
        found = reconcile.find_inkdrop_failed_download_queue({"title": "Lock Test 7", "query": "Lock Test 7"})
        require(found and found["id"] == "queue-1", "query-only retry should return the matching queue")
        require(
            2 <= calls["count"] <= reconcile.INKDROP_REPLAY_STATE_READ_RETRY_ATTEMPTS,
            f"lookup retry must stay bounded (calls={calls['count']})",
        )
    finally:
        release_writer.set()
        thread.join(5)
        reconcile.INKDROP_STATE_DB = original_db
        reconcile.inkdrop_state.connect_read = original_connect_read
        reconcile.inkdrop_state.init_schema = original_init_schema


def seed_reconciliation_db(path):
    con = sqlite3.connect(path)
    try:
        con.execute(
            "create table download_reconciliation(lifecycle_state text, reason text, inkdrop_queue_id text, matched_local_path text)"
        )
        con.execute(
            "insert into download_reconciliation values('failed_download', 'client_failed', 'queue-1', null)"
        )
        con.commit()
    finally:
        con.close()


def assert_db_refresh_marks_carried_evidence(root):
    db_path = root / "reconciliation.sqlite3"
    status_path = root / "status.json"
    seed_reconciliation_db(db_path)
    status_path.write_text(
        json.dumps(
            {
                "updated_at": 150.0,
                "failed_download_sync": {
                    "completed_at": 150.0,
                    "started_at": 140.0,
                    "carried": False,
                    "skipped": {"state_db_locked": 1},
                    "lock_samples": [{"stage": "queue_lookup", "record_hash": "0123456789abcdef", "observed_at": 145.0}],
                },
            }
        ),
        encoding="utf-8",
    )
    originals = (reconcile.DB_PATH, reconcile.RECONCILE_STATUS_PATH, reconcile.STATE_DIR, reconcile.now)
    try:
        reconcile.DB_PATH = db_path
        reconcile.RECONCILE_STATUS_PATH = status_path
        reconcile.STATE_DIR = root
        refresh_times = iter([200.0, 220.0])
        reconcile.now = lambda: next(refresh_times)
        status = reconcile.refresh_status_from_reconciliation_db()
        evidence = status["failed_download_sync"]
        require(status["updated_at"] == 200.0 and status["scan_mode"] == "db_refresh", "top-level refresh should advance")
        require(evidence["carried"] is True and evidence["fresh"] is False, "nested evidence must be explicitly carried")
        require(evidence["completed_at"] == 150.0, "original evidence timestamp must remain unchanged")
        require(evidence["evidence_age_seconds"] == 50.0, "carried evidence age must be explicit")

        repeated = reconcile.refresh_status_from_reconciliation_db()["failed_download_sync"]
        require(repeated["completed_at"] == 150.0, "repeated refresh must retain the original evidence timestamp")
        require(repeated["evidence_age_seconds"] == 70.0, "repeated refresh must recompute age from original evidence")

        legacy = {"skipped": {"state_db_locked": 3, "missing_queue_match": 1}, "lock_samples": [{"stage": "queue_lookup"}]}
        carried = reconcile.carry_failed_download_sync_evidence(legacy, 200.0)
        require(carried["evidence_timestamp_missing"] is True, "legacy evidence must disclose missing timestamp")
        require("state_db_locked" not in carried["skipped"], "untimestamped ephemeral lock count must be omitted")
        require("lock_samples" not in carried, "untimestamped lock samples must be omitted")

        def evidence_at(value, started=0.0):
            return reconcile.carry_failed_download_sync_evidence(
                {
                    "started_at": started,
                    "completed_at": value,
                    "skipped": {"state_db_locked": 1},
                    "lock_samples": [{"stage": "queue_lookup", "observed_at": value}],
                },
                200.0,
            )

        for malformed in (float("nan"), float("inf"), "150"):
            invalid = evidence_at(malformed)
            require(invalid["evidence_timestamp_invalid"] is True, f"{malformed!r} must be rejected as a finite numeric timestamp")
            require("evidence_age_seconds" not in invalid and "lock_samples" not in invalid, "invalid evidence cannot appear fresh")
            require("state_db_locked" not in invalid["skipped"], "invalid ephemeral counters must be omitted")

        future = evidence_at(250.0, started=240.0)
        require(future["evidence_clock_skew_detected"] is True, "future evidence must be marked as clock skew")
        require(future["evidence_future_by_seconds"] == 50.0, "future skew must be quantified")
        require("evidence_age_seconds" not in future and "lock_samples" not in future, "future evidence cannot appear fresh")

        zero = evidence_at(0.0, started=0.0)
        require(zero["evidence_age_seconds"] == 200.0, "explicit zero timestamp must not fall back to started_at")
        require("lock_samples" in zero and zero["skipped"]["state_db_locked"] == 1, "valid zero evidence may retain timestamped lock data")

        normal = evidence_at(150.0, started=140.0)
        require(normal["evidence_age_seconds"] == 50.0 and normal["fresh"] is False, "normal carried age must remain stale and exact")

        malformed_sample = reconcile.carry_failed_download_sync_evidence(
            {
                "started_at": 140.0,
                "completed_at": 150.0,
                "skipped": {"state_db_locked": 1},
                "lock_samples": [{"stage": "queue_lookup", "observed_at": float("nan")}],
                "evidence_age_seconds": 1.0,
            },
            200.0,
        )
        require(malformed_sample["lock_sample_timestamp_invalid"] is True, "malformed sample time must be explicit")
        require("evidence_age_seconds" not in malformed_sample, "stale derived age must be cleared when sample evidence is invalid")
        require("lock_samples" not in malformed_sample, "malformed ephemeral samples must be omitted")
    finally:
        reconcile.DB_PATH, reconcile.RECONCILE_STATUS_PATH, reconcile.STATE_DIR, reconcile.now = originals


def assert_stage_specific_hashed_lock_samples(root):
    state_db = root / "lock-samples.sqlite3"
    state_db.touch()
    original_db = reconcile.INKDROP_STATE_DB
    original_find = reconcile.find_inkdrop_failed_download_queue
    original_bad = reconcile.inkdrop_state.record_bad_source_candidate
    original_attempt = reconcile.inkdrop_state.record_queue_source_attempt

    records = [
        {"state": "failed_download", "pending_key": "private-queue-lookup", "title": "Secret Queue"},
        {"state": "failed_download", "pending_key": "private-bad-write", "title": "Secret Bad"},
        {"state": "failed_download", "pending_key": "private-attempt-write", "title": "Secret Attempt"},
    ]

    def find_queue(record):
        if record["pending_key"] == "private-queue-lookup":
            raise sqlite3.OperationalError("database is locked")
        return {"id": record["pending_key"], "series_id": "series-1", "issue_id": "issue-1", "wanted_id": None}

    def record_bad(_db, **payload):
        if payload["raw"]["record"]["pending_key"] == "private-bad-write":
            raise sqlite3.OperationalError("database is locked")
        return {"ok": True, "candidate_id": "candidate-1"}

    def record_attempt(_db, _queue_id, attempt, **_kwargs):
        if attempt["raw"]["pending_key"] == "private-attempt-write":
            raise sqlite3.OperationalError("database is locked")
        return {"ok": True}

    try:
        reconcile.INKDROP_STATE_DB = state_db
        reconcile.find_inkdrop_failed_download_queue = find_queue
        reconcile.inkdrop_state.record_bad_source_candidate = record_bad
        reconcile.inkdrop_state.record_queue_source_attempt = record_attempt
        summary = reconcile.sync_inkdrop_failed_download_records(records)
        stages = {sample["stage"] for sample in summary["lock_samples"]}
        require(stages == {"queue_lookup", "bad_candidate_write", "attempt_write"}, "all lock stages must be distinguished")
        require(summary["started_at"] <= summary["completed_at"] and summary["carried"] is False, "fresh sync timestamps are required")
        serialized = json.dumps(summary)
        require("private-" not in serialized and "Secret" not in serialized, "lock evidence must contain hashes, not identities")
        require(all(len(sample["record_hash"]) == 16 for sample in summary["lock_samples"]), "record hashes must be stable short digests")
    finally:
        reconcile.INKDROP_STATE_DB = original_db
        reconcile.find_inkdrop_failed_download_queue = original_find
        reconcile.inkdrop_state.record_bad_source_candidate = original_bad
        reconcile.inkdrop_state.record_queue_source_attempt = original_attempt


def assert_wall_clock_rollback_is_clamped(root):
    state_db = root / "rollback.sqlite3"
    state_db.touch()
    original_db = reconcile.INKDROP_STATE_DB
    original_find = reconcile.find_inkdrop_failed_download_queue
    original_now = reconcile.now
    clock = iter([100.0, 90.0, 80.0])
    try:
        reconcile.INKDROP_STATE_DB = state_db
        reconcile.find_inkdrop_failed_download_queue = lambda _record: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        )
        reconcile.now = lambda: next(clock)
        summary = reconcile.sync_inkdrop_failed_download_records(
            [{"state": "failed_download", "pending_key": "rollback-private"}]
        )
        sample = summary["lock_samples"][0]
        require(summary["started_at"] == 100.0, "start should use the first finite wall-clock sample")
        require(sample["observed_at"] == 100.0 and sample["observed_at_raw"] == 90.0, "lock sample must clamp and disclose rollback")
        require(sample["clock_skew_detected"] is True, "sample rollback must be explicitly marked")
        require(summary["completed_at"] == 100.0 and summary["completed_at_raw"] == 80.0, "completion must not precede start or sample")
        require(summary["clock_skew_detected"] is True and summary["sample_clock_skew_detected"] is True, "summary must disclose clock skew")
    finally:
        reconcile.INKDROP_STATE_DB = original_db
        reconcile.find_inkdrop_failed_download_queue = original_find
        reconcile.now = original_now


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-reconcile-lock-observability-") as tmp:
        root = Path(tmp)
        assert_query_only_retry_without_schema_ddl(root)
        assert_stage_specific_hashed_lock_samples(root)
        assert_wall_clock_rollback_is_clamped(root)
        assert_db_refresh_marks_carried_evidence(root)
    print("inkdrop reconcile lock observability smoke: PASS")


if __name__ == "__main__":
    main()
