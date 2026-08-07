#!/usr/bin/env python3
"""SLSKD reports a transfer Completed, Succeeded while InkDrop leaves the
Wanted unit queued/searching, or announces a permanent download failure that
contradicts an active, still-retryable queue.

Three stacked defects, fixed together in report order:

1. An import row with an explicit negative status (rejected/removed/missing/
   superseded) could still be treated as an authoritative owner of the
   Wanted unit if a stale positive column (verified/folder_imported/
   completion_truth) was left behind by an earlier transition -- fencing a
   later, genuinely completed SLSKD recovery. Live data had 93 such
   contradictory rows.
2. One physical SLSKD transfer could be claimed by more than one InkDrop
   download_tasks row for the same exact Wanted unit (a reservation task
   plus a later waiting-record projection); reconciliation treated every
   such group as ambiguous and did nothing. Live data had 35 such groups.
3. inkdrop_notification_events.py announced an attempt-level failure
   (retry_eligible=0 on one task) as an absolute item-level "will not be
   retried automatically" even while the canonical queue remained active,
   retryable, or already had a completed sibling/import.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest import mock

from core import inkdrop_notifications
from core import inkdrop_slskd_source_probe as probe
from core import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def negative_import_status_never_authoritative():
    for status in sorted(inkdrop_state.IMPORT_RESULT_EXPLICIT_NEGATIVE_STATUSES):
        contradictory_row = {
            "status": status,
            "display_phase": "retry_later",
            "completion_truth": "folder",
            "verified": 1,
            "folder_imported": 1,
        }
        require(
            inkdrop_state.slskd_import_result_is_authoritative_owner(contradictory_row) is False,
            f"explicit negative status {status!r} with stale positive columns was still treated as authoritative",
        )

    # Real completion truth for genuinely active/verified rows must be unchanged.
    for status, extra in (
        ("verification_pending", {}),
        ("importing", {}),
        ("folder_verified", {}),
    ):
        require(
            inkdrop_state.slskd_import_result_is_authoritative_owner({"status": status, **extra}) is True,
            f"a genuinely active/verified status {status!r} lost its authority",
        )
    require(
        inkdrop_state.slskd_import_result_is_authoritative_owner(
            {"status": "some_unrelated_status", "verified": 1}
        ) is True,
        "a genuinely verified row without a negative status lost its authority",
    )


def mark_single_part_mismatch_clears_stale_positive_columns():
    with tempfile.TemporaryDirectory(prefix="inkdrop-import-truth-") as temp:
        db = Path(temp) / "state.sqlite3"
        with inkdrop_state.connect(db) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                """
                insert into import_results(
                    id, queue_id, series_id, issue_id, status, outcome, display_phase,
                    verified, folder_imported, completion_truth, imported_count,
                    source_path, dest_path, created_at, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "import-1", "queue-1", "series-1", "issue-1",
                    "folder_verified", "verified", "verified",
                    1, 1, "folder", 1,
                    "/downloads/x.cbz", "/library/x.cbz",
                    time.time(), "{}",
                ),
            )
            con.commit()
            row = dict(con.execute("select * from import_results where id=?", ("import-1",)).fetchone())
            ok = inkdrop_state.mark_import_result_single_part_mismatch(
                con, row, "single_part_file_does_not_satisfy_collection_target", time.time()
            )
            con.commit()
            require(ok is not False, "mark_import_result_single_part_mismatch reported failure")
            after = dict(con.execute("select * from import_results where id=?", ("import-1",)).fetchone())
        require(after["status"] == "single_part_file_does_not_satisfy_collection_target", after["status"])
        require(int(after["verified"] or 0) == 0, "verified was not cleared")
        require(int(after["folder_imported"] or 0) == 0, "folder_imported was not cleared -- stays authoritative")
        require(not after["completion_truth"], f"completion_truth was not cleared: {after['completion_truth']!r}")
        require(
            inkdrop_state.slskd_import_result_is_authoritative_owner(after) is False,
            "row remained authoritative after the collection-guard rejection",
        )


def slskd_failed_import_match_statuses_normalize_as_retryable_candidate_failures():
    for status in sorted(inkdrop_state.SLSKD_FAILED_IMPORT_MATCH_STATUSES):
        attempt = {
            "status": status,
            "source": "slskd",
            "reason": f"rejected as {status}",
        }
        normalized = inkdrop_state.normalize_slskd_terminal_recovery_attempt(attempt)
        require(
            normalized.get("retry_eligible") is True,
            f"{status!r} did not normalize to retry_eligible=True (would create a non-retryable, reasonless task)",
        )
        require(
            normalized.get("lifecycle_phase") == "failed_candidate",
            f"{status!r} did not normalize to lifecycle_phase=failed_candidate",
        )
        require(status in inkdrop_state.DOWNLOAD_TASK_STATUSES, f"{status!r} is not a recognized download task status")


def canonical_owner_collapses_duplicate_transfer_rows():
    base = {"queue_id": "q1", "series_id": "s1", "issue_id": "i1", "raw_json": "{}"}
    reservation = {**base, "id": "task-a", "status": "started_waiting", "started_at": 100.0}
    projection = {**base, "id": "task-b", "status": "started_waiting", "started_at": 200.0}
    canonical = probe.canonical_slskd_transfer_owner_task([reservation, projection])
    require(canonical["id"] == "task-a", f"earliest reservation task was not chosen: {canonical}")

    with_exact_unit_key = {**base, "id": "task-c", "status": "started_waiting", "started_at": 50.0}
    without_key = {**base, "id": "task-d", "status": "started_waiting", "started_at": 10.0,
                   "raw_json": '{"exact_unit_key": "slskd_exact_unit:s1:issue:1"}'}
    canonical2 = probe.canonical_slskd_transfer_owner_task([with_exact_unit_key, without_key])
    require(canonical2["id"] == "task-d", f"a durable exact-unit-key binding was not preferred: {canonical2}")

    trio = [
        {**base, "id": "task-e", "status": "started_waiting", "started_at": 300.0},
        {**base, "id": "task-f", "status": "started_waiting", "started_at": 100.0},
        {**base, "id": "task-g", "status": "started_waiting", "started_at": 200.0},
    ]
    canonical3 = probe.canonical_slskd_transfer_owner_task(trio)
    require(canonical3["id"] == "task-f", f"three-owner group did not collapse to the earliest task: {canonical3}")

    quad = trio + [{**base, "id": "task-h", "status": "started_waiting", "started_at": 50.0}]
    canonical4 = probe.canonical_slskd_transfer_owner_task(quad)
    require(canonical4["id"] == "task-h", f"four-owner group did not collapse to the earliest task: {canonical4}")

    single = probe.canonical_slskd_transfer_owner_task([reservation])
    require(single["id"] == "task-a", "single-owner passthrough regressed")


def retire_duplicate_owner_task_preserves_row_and_history():
    with tempfile.TemporaryDirectory(prefix="inkdrop-duplicate-owner-") as temp:
        db = Path(temp) / "state.sqlite3"
        with inkdrop_state.connect(db) as con:
            inkdrop_state.init_schema(con)
            con.execute("insert into series(id, title, media_type) values(?,?,?)", ("s1", "House of X", "comic"))
            con.execute("insert into issues(id, series_id, issue_number) values(?,?,?)", ("i1", "s1", "2"))
            con.execute(
                "insert into queue_items(id, series_id, issue_id, state, active) values(?,?,?,?,?)",
                ("q1", "s1", "i1", "downloading", 1),
            )
            for task_id, status in (("canon", "started_waiting"), ("dup", "started_waiting")):
                con.execute(
                    """
                    insert into download_tasks(
                        id, queue_id, series_id, issue_id, source, download_client,
                        external_id, status, state, retry_eligible, started_at, updated_at, raw_json
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (task_id, "q1", "s1", "i1", "slskd", "slskd", "transfer-xyz", status, "downloading", 1,
                     time.time(), time.time(), "{}"),
                )
            con.commit()
        ok = inkdrop_state.retire_duplicate_slskd_owner_task(db, "dup", "canon", "transfer-xyz", time.time())
        require(ok is True, "retire_duplicate_slskd_owner_task reported failure")
        with inkdrop_state.connect_read(db) as con:
            dup_row = dict(con.execute("select * from download_tasks where id=?", ("dup",)).fetchone())
            canon_row = dict(con.execute("select * from download_tasks where id=?", ("canon",)).fetchone())
            history = con.execute(
                "select * from history_events where entity_id=? and event_type='duplicate_transfer_owner_retired'",
                ("dup",),
            ).fetchall()
        require(dup_row is not None, "duplicate task row was deleted -- evidence must be preserved, not removed")
        require(dup_row["status"] == "superseded_duplicate_owner", dup_row["status"])
        require(int(dup_row["retry_eligible"] or 0) == 0, "superseded duplicate remained retry_eligible")
        require(canon_row["status"] == "started_waiting", "canonical task was incorrectly touched")
        require(len(history) == 1, f"expected exactly one retirement history event, got {len(history)}")

        # idempotent replay: retiring the same row twice must not error or duplicate history
        ok_again = inkdrop_state.retire_duplicate_slskd_owner_task(db, "dup", "canon", "transfer-xyz", time.time())
        require(ok_again is False, "retiring an already-retired duplicate should be a no-op, not a repeat write")
        with inkdrop_state.connect_read(db) as con:
            history_after = con.execute(
                "select * from history_events where entity_id=? and event_type='duplicate_transfer_owner_retired'",
                ("dup",),
            ).fetchall()
        require(len(history_after) == 1, "replaying retirement duplicated the history event")


def notification_wording_matches_queue_terminality():
    sent = []

    def fake_notify(db_path, event_type, *, subject, message, **kwargs):
        sent.append({"subject": subject, "message": message})
        return ["discord"]

    with mock.patch.object(inkdrop_notifications, "notify", side_effect=fake_notify):
        inkdrop_notifications.notify_download_failed(
            "db", series="House of X", issue_label="#2", reason="staged_file_mismatch", terminal=False,
        )
        inkdrop_notifications.notify_download_failed(
            "db", series="House of X", issue_label="#3", reason="no more sources", terminal=True,
        )
    require(len(sent) == 2, sent)
    require("will not be retried" not in sent[0]["message"], f"attempt-scoped notice used terminal wording: {sent[0]}")
    require("did not complete" in sent[0]["message"] and "remaining automatic sources" in sent[0]["message"], sent[0])
    require("will not be retried automatically" in sent[1]["message"], f"terminal notice lost its wording: {sent[1]}")


def main():
    negative_import_status_never_authoritative()
    mark_single_part_mismatch_clears_stale_positive_columns()
    slskd_failed_import_match_statuses_normalize_as_retryable_candidate_failures()
    canonical_owner_collapses_duplicate_transfer_rows()
    retire_duplicate_owner_task_preserves_row_and_history()
    notification_wording_matches_queue_terminality()
    print(
        "SLSKD_COMPLETED_TRANSFER_TRUTH_OK: explicit negative import statuses are never authoritative "
        "regardless of stale positive columns, 1/2/3/4-owner duplicate transfer groups collapse to one "
        "canonical, non-destructive retirement, and failure notifications are wordy-correct for both "
        "attempt-scoped and genuinely terminal outcomes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
