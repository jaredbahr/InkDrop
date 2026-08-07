#!/usr/bin/env python3
"""Regression coverage for authoritative download-client ownership reconciliation."""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import json
import tempfile
import time
from pathlib import Path

from core import inkdrop_reconcile_imports
from core import inkdrop_source_worker_scheduler
from core import inkdrop_state


NOW = 1_800_000_000.0


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def seed_scope(con, suffix="main"):
    series_id = f"series-{suffix}"
    issue_id = f"issue-{suffix}"
    wanted_id = f"wanted-{suffix}"
    queue_id = f"queue-{suffix}"
    con.execute(
        "insert into series(id,title,media_type,created_at,updated_at) values(?,?,?,?,?)",
        (series_id, f"Ownership Smoke {suffix}", "comic", NOW - 2000, NOW - 2000),
    )
    con.execute(
        "insert into issues(id,series_id,issue_number,created_at,updated_at) values(?,?,?,?,?)",
        (issue_id, series_id, "1", NOW - 2000, NOW - 2000),
    )
    con.execute(
        "insert into wanted_items(id,series_id,issue_id,reason,status,created_at,updated_at) values(?,?,?,?,?,?,?)",
        (wanted_id, series_id, issue_id, "missing", "downloading", NOW - 2000, NOW - 2000),
    )
    con.execute(
        """
        insert into queue_items(
            id,wanted_id,series_id,issue_id,state,current_source,last_event,
            active,created_at,updated_at,raw_json
        ) values(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            queue_id,
            wanted_id,
            series_id,
            issue_id,
            "downloading",
            "sabnzbd",
            "SABnzbd transfer active",
            1,
            NOW - 2000,
            NOW - 1000,
            "{}",
        ),
    )
    return queue_id, wanted_id, series_id, issue_id


def seed_sab_attempt(con, scope, index, *, candidate=None, source_attempt_id=None):
    queue_id, wanted_id, series_id, issue_id = scope
    external_id = f"sab-job-{index}"
    attempt_id = source_attempt_id or f"attempt-{queue_id}-{index}"
    attempt = {
        "source": "download_client",
        "provider_id": "provider-smoke",
        "provider": "provider-smoke",
        "protocol": "usenet",
        "download_client": "sabnzbd",
        "external_id": external_id,
        "nzo_id": external_id,
        "candidate_identity": candidate or f"candidate-{index}",
        "status": "download_started",
        "lifecycle_phase": "downloading",
        "retry_eligible": False,
        "title": f"Ownership candidate {index}",
        "started_at": NOW - 1000,
        "raw": {"kind": "ownership_smoke"},
    }
    con.execute(
        """
        insert into source_attempts(
            id,queue_id,wanted_id,series_id,issue_id,source,provider_id,provider,
            protocol,download_client,candidate_identity,lifecycle_phase,
            retry_eligible,status,title,started_at,raw_json
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt_id,
            queue_id,
            wanted_id,
            series_id,
            issue_id,
            attempt["source"],
            attempt["provider_id"],
            attempt["provider"],
            attempt["protocol"],
            attempt["download_client"],
            attempt["candidate_identity"],
            attempt["lifecycle_phase"],
            0,
            attempt["status"],
            attempt["title"],
            attempt["started_at"],
            json.dumps(attempt, separators=(",", ":")),
        ),
    )
    task_id = inkdrop_state.record_download_task_for_attempt(
        con,
        queue_id,
        wanted_id,
        series_id,
        issue_id,
        attempt,
        attempt_id,
        started_at=NOW - 1000,
        refresh_queue_rollup=False,
    )
    return task_id, attempt_id, external_id


def active_sab_count(con, queue_id):
    return con.execute(
        """
        select count(*)
        from download_tasks
        where queue_id=?
          and lower(download_client)='sabnzbd'
          and state in ('queued','downloading','import_ready','importing')
        """,
        (queue_id,),
    ).fetchone()[0]


def smoke_empty_snapshot_and_backfill(con):
    scope = seed_scope(con, "empty")
    task_ids = [seed_sab_attempt(con, scope, index)[0] for index in range(5)]
    # A second durable task from the same source attempt catches the historical
    # source_attempt_id skip bug directly.
    first = con.execute("select * from download_tasks where id=?", (task_ids[0],)).fetchone()
    clone = dict(first)
    clone["id"] = "same-source-sibling"
    clone["external_id"] = "sab-job-same-source-sibling"
    clone["candidate_identity"] = first["candidate_identity"]
    clone["raw_json"] = json.dumps(
        {
            "source": "download_client",
            "download_client": "sabnzbd",
            "external_id": clone["external_id"],
            "candidate_identity": clone["candidate_identity"],
            "status": "download_started",
            "started_at": NOW - 1000,
        },
        separators=(",", ":"),
    )
    columns = list(clone)
    con.execute(
        f"insert into download_tasks({','.join(columns)}) values({','.join('?' for _ in columns)})",
        [clone[column] for column in columns],
    )

    summary = inkdrop_state.apply_download_client_snapshots(
        con,
        [],
        available_clients=["sabnzbd"],
        now=NOW,
        stale_after_seconds=100,
        observation_started_at=NOW,
    )
    require(summary["stale_retried"] == 1, f"empty snapshot did not retry once: {summary}")
    require(summary["changed"] == 6, f"changed count omitted retired siblings: {summary}")
    require(active_sab_count(con, scope[0]) == 0, "authoritative empty snapshot left an active SAB owner")
    rows = con.execute(
        "select id,status,state,completed_at,raw_json from download_tasks where queue_id=?",
        (scope[0],),
    ).fetchall()
    require(len(rows) == 6, "empty snapshot erased task history")
    require(all(row["state"] == "failed" and row["completed_at"] for row in rows), "not every sibling became terminal")
    require(
        all(json.loads(row["raw_json"] or "{}").get("download_client_reconciled_terminal") for row in rows),
        "terminal reconciliation marker missing",
    )
    queue = con.execute("select state,last_event from queue_items where id=?", (scope[0],)).fetchone()
    require(queue["state"] == "queued" and "automatic retry scheduled" in queue["last_event"], "queue was not requeued")
    con.commit()
    require(
        not inkdrop_source_worker_scheduler.active_handoff_tasks(
            con.execute("pragma database_list").fetchone()[2], scope[0], now=NOW
        ),
        "scheduler still sees active handoff after authoritative close",
    )

    inkdrop_state.backfill_download_tasks_from_attempts(con)
    require(active_sab_count(con, scope[0]) == 0, "source-attempt backfill resurrected reconciled ownership")

    history_before = con.execute(
        "select count(*) from history_events where entity_type='download_task' and entity_id in (select id from download_tasks where queue_id=?)",
        (scope[0],),
    ).fetchone()[0]
    inkdrop_state.cleanup_duplicate_download_tasks(con)
    rows_after = con.execute("select count(*) from download_tasks where queue_id=?", (scope[0],)).fetchone()[0]
    history_after = con.execute(
        "select count(*) from history_events where entity_type='download_task' and entity_id in (select id from download_tasks where queue_id=?)",
        (scope[0],),
    ).fetchone()[0]
    require(rows_after == 6, "distinct external-ID terminal task evidence was deleted")
    require(history_after >= history_before > 0, "distinct external-ID history was deleted")


def smoke_live_snapshot_selects_one_owner(con):
    scope = seed_scope(con, "live")
    seeded = [seed_sab_attempt(con, scope, index + 10) for index in range(3)]
    live_id = seeded[-1][2]
    con.execute("update download_tasks set title='Same title for distinct client IDs' where queue_id=?", (scope[0],))
    summary = inkdrop_state.apply_download_client_snapshots(
        con,
        [
            {
                "client": "sabnzbd",
                "external_id": live_id,
                "nzo_id": live_id,
                "status": "downloading",
                "progress": 0.4,
                "title": "Same title for distinct client IDs",
            }
        ],
        available_clients=["sabnzbd"],
        now=NOW,
        stale_after_seconds=100,
        observation_started_at=NOW,
    )
    require(summary["matched"] == 1, f"live snapshot was not matched: {summary}")
    active = con.execute(
        "select external_id,status,state from download_tasks where queue_id=? and state='downloading'",
        (scope[0],),
    ).fetchall()
    require(len(active) == 1 and active[0]["external_id"] == live_id, "snapshot did not select exactly its live owner")
    require(active_sab_count(con, scope[0]) == 1, "more than one SAB owner remained active")
    require(
        con.execute("select state from queue_items where id=?", (scope[0],)).fetchone()[0] == "downloading",
        "live snapshot did not keep queue downloading",
    )


def smoke_distinct_concrete_client_jobs_remain_distinct(con):
    scope = seed_scope(con, "distinct-jobs")
    first_id, _first_attempt, first_external = seed_sab_attempt(
        con,
        scope,
        60,
        candidate="same-provider-candidate",
    )
    second_id, _second_attempt, second_external = seed_sab_attempt(
        con,
        scope,
        61,
        candidate="same-provider-candidate",
    )
    require(first_id != second_id, "distinct concrete client jobs were collapsed by candidate identity")
    rows = con.execute(
        "select external_id from download_tasks where queue_id=? order by external_id",
        (scope[0],),
    ).fetchall()
    require(
        [row["external_id"] for row in rows] == sorted([first_external, second_external]),
        "distinct concrete client job evidence was overwritten",
    )


def smoke_unavailable_is_not_empty_snapshot(con):
    scope = seed_scope(con, "error")
    seed_sab_attempt(con, scope, 20)
    summary = inkdrop_state.apply_download_client_snapshots(
        con,
        [],
        available_clients=[],
        now=NOW,
        stale_after_seconds=100,
    )
    require(summary["checked"] == 0 and active_sab_count(con, scope[0]) == 1, "unavailable client was treated as empty")
    available, snapshots, errors, _observed_at = inkdrop_reconcile_imports.download_client_reconcile_inputs(
        {
            "observation_started_at": NOW,
            "observations": [
                {
                    "client": "sabnzbd",
                    "configured": True,
                    "authoritative": False,
                    "items": [{"client": "sabnzbd", "client_state": "client_unavailable", "error": "timeout"}],
                    "errors": [{"client": "sabnzbd", "error": "timeout"}],
                }
            ],
        }
    )
    require(not available and not snapshots and len(errors) == 1, "API error was admitted as an authoritative client observation")


def smoke_observation_cutoff_preserves_later_enqueue(con):
    scope = seed_scope(con, "cutoff")
    seed_sab_attempt(con, scope, 30)
    later_task_id, _attempt_id, _external_id = seed_sab_attempt(con, scope, 31)
    con.execute(
        "update download_tasks set started_at=?,updated_at=? where id=?",
        (NOW + 10, NOW + 10, later_task_id),
    )
    summary = inkdrop_state.apply_download_client_snapshots(
        con,
        [],
        available_clients=["sabnzbd"],
        now=NOW + 20,
        stale_after_seconds=100,
        observation_started_at=NOW,
    )
    require(summary["stale_retried"] == 1, f"predating owner was not closed: {summary}")
    later = con.execute("select status,state,completed_at from download_tasks where id=?", (later_task_id,)).fetchone()
    require(later["state"] == "downloading" and later["completed_at"] is None, "post-observation enqueue was retired")


def smoke_production_collector_contract(con, db_path):
    empty_scope = seed_scope(con, "collector-empty")
    empty_task_id, _attempt_id, _external_id = seed_sab_attempt(con, empty_scope, 40)
    old = time.time() - (24 * 60 * 60)
    con.execute("update download_tasks set started_at=?,updated_at=? where id=?", (old, old, empty_task_id))
    con.commit()

    saved = {
        "state_db": inkdrop_reconcile_imports.INKDROP_STATE_DB,
        "sab_settings": inkdrop_reconcile_imports.sab_settings,
        "sab_items": inkdrop_reconcile_imports.sab_items,
        "qbit_settings": inkdrop_reconcile_imports.qbit_settings,
        "qbit_items": inkdrop_reconcile_imports.qbit_items,
        "fanout": inkdrop_reconcile_imports.fanout_local_completed_packs_to_inkdrop,
    }
    try:
        inkdrop_reconcile_imports.INKDROP_STATE_DB = Path(db_path)
        inkdrop_reconcile_imports.sab_settings = lambda: {"host": "http://sab.invalid", "apikey": "fixture"}
        inkdrop_reconcile_imports.sab_items = lambda: []
        inkdrop_reconcile_imports.qbit_settings = lambda: {"host": "http://qbit.invalid", "user": "fixture", "pass": "fixture"}
        inkdrop_reconcile_imports.qbit_items = lambda: []
        inkdrop_reconcile_imports.fanout_local_completed_packs_to_inkdrop = lambda: {"created": 0, "updated": 0}
        result = inkdrop_reconcile_imports.reconcile_inkdrop_download_clients()
        require(result.get("available_clients") == ["qbittorrent", "sabnzbd"], f"healthy empty collectors were not explicit: {result}")
        require(active_sab_count(con, empty_scope[0]) == 0, "healthy empty collector did not reach reconciliation")

        partial_scope = seed_scope(con, "collector-partial")
        seed_sab_attempt(con, partial_scope, 41)
        con.commit()

        def partial_sab_api(_settings, mode, **_params):
            if mode == "queue":
                return {
                    "queue": {
                        "slots": [
                            {
                                "cat": "comics",
                                "filename": "Partial collector fixture",
                                "nzo_id": "sab-partial-visible",
                                "status": "Downloading",
                                "percentage": "25",
                            }
                        ]
                    }
                }
            raise TimeoutError("history timeout")

        saved_sab_api = inkdrop_reconcile_imports.sab_api
        inkdrop_reconcile_imports.sab_items = saved["sab_items"]
        inkdrop_reconcile_imports.sab_api = partial_sab_api
        try:
            result = inkdrop_reconcile_imports.reconcile_inkdrop_download_clients(client_filter=["sabnzbd"])
        finally:
            inkdrop_reconcile_imports.sab_api = saved_sab_api
        require(not result.get("available_clients"), f"partial SAB response remained authoritative: {result}")
        require(result.get("client_errors"), "partial SAB history failure was not reported")
        require(active_sab_count(con, partial_scope[0]) == 1, "partial SAB response retired local ownership")
    finally:
        inkdrop_reconcile_imports.INKDROP_STATE_DB = saved["state_db"]
        inkdrop_reconcile_imports.sab_settings = saved["sab_settings"]
        inkdrop_reconcile_imports.sab_items = saved["sab_items"]
        inkdrop_reconcile_imports.qbit_settings = saved["qbit_settings"]
        inkdrop_reconcile_imports.qbit_items = saved["qbit_items"]
        inkdrop_reconcile_imports.fanout_local_completed_packs_to_inkdrop = saved["fanout"]


def smoke_slskd_non_regression(con):
    scope = seed_scope(con, "slskd")
    for index in range(2):
        con.execute(
            """
            insert into download_tasks(
                id,queue_id,wanted_id,series_id,issue_id,source,provider,protocol,
                download_client,external_id,title,status,state,started_at,updated_at,raw_json
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"slskd-task-{index}",
                *scope,
                "slskd",
                "slskd-user",
                "slskd",
                "SLSKD",
                f"slskd-external-{index}",
                "SLSKD ownership fixture",
                "waiting_for_transfer",
                "downloading",
                NOW - 1000,
                NOW - index,
                "{}",
            ),
        )
    retired = inkdrop_state.cleanup_duplicate_active_handoff_download_tasks(con, NOW)
    remaining = con.execute(
        "select count(*) from download_tasks where queue_id=? and state='downloading'",
        (scope[0],),
    ).fetchone()[0]
    require(retired == 0 and remaining == 2, "SLSKD parallel transfer behavior changed")


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-download-owner-") as tmp:
        smokes = (
            smoke_empty_snapshot_and_backfill,
            smoke_live_snapshot_selects_one_owner,
            smoke_distinct_concrete_client_jobs_remain_distinct,
            smoke_unavailable_is_not_empty_snapshot,
            smoke_observation_cutoff_preserves_later_enqueue,
            smoke_production_collector_contract,
            smoke_slskd_non_regression,
        )
        for index, smoke in enumerate(smokes):
            db_path = Path(tmp) / f"state-{index}.sqlite3"
            with inkdrop_state.connect(db_path) as con:
                inkdrop_state.init_schema(con)
                if smoke is smoke_production_collector_contract:
                    smoke(con, db_path)
                else:
                    smoke(con)
                con.commit()
                require(con.execute("pragma integrity_check").fetchone()[0] == "ok", "SQLite integrity check failed")
                require(not con.execute("pragma foreign_key_check").fetchall(), "SQLite foreign key check failed")
    print("INKDROP_DOWNLOAD_CLIENT_OWNERSHIP_SMOKE_OK")


if __name__ == "__main__":
    main()
