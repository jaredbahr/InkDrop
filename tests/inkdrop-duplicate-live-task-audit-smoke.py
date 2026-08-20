#!/usr/bin/env python3

import sqlite3
import tempfile
from pathlib import Path

from core import inkdrop_state
from tools.inkdrop_duplicate_live_task_audit import audit


with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "state.sqlite3"
    inkdrop_state.ensure_schema(db)
    con = sqlite3.connect(db)
    con.execute("insert into series(id,title,created_at,updated_at) values('s','Series',1,1)")
    con.execute("insert into wanted_items(id,series_id,status,created_at,updated_at) values('w','s','wanted',1,1)")
    con.execute("insert into queue_items(id,wanted_id,series_id,state,active,created_at,updated_at) values('q','w','s','downloading',1,1,1)")
    con.execute("insert into queue_items(id,wanted_id,series_id,state,active,created_at,updated_at) values('inactive','w','s','downloading',0,1,1)")

    def task(task_id, queue_id, phase, candidate="candidate-1", external="job-1"):
        con.execute(
            """insert into download_tasks(
                 id,queue_id,wanted_id,series_id,download_client,external_id,candidate_identity,
                 lifecycle_phase,status,state,started_at,updated_at
               ) values(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, queue_id, "w", "s", "qbittorrent", external, candidate, phase, phase, phase, 1, 2),
        )

    task("live-1", "q", "downloading")
    task("history", "q", "verified")
    task("provider-wait", "q", "provider_wait")
    con.execute(
        """insert into download_tasks(
             id,queue_id,wanted_id,series_id,download_client,candidate_identity,
             lifecycle_phase,status,state,started_at,updated_at
           ) values('provider-unavailable','q','w','s','slskd','candidate-1',
                    'provider_wait','provider_unavailable','queued',1,2)"""
    )
    con.execute(
        """insert into download_tasks(
             id,queue_id,wanted_id,series_id,download_client,candidate_identity,
             lifecycle_phase,status,state,started_at,updated_at
           ) values('stale-lifecycle','q','w','s','slskd','candidate-1',
                    'downloading','superseded_duplicate','retired',1,2)"""
    )
    task("inactive-live", "inactive", "downloading")
    con.commit()
    assert audit(db)["duplicate_group_count"] == 0

    task("live-2", "q", "client_queued")
    con.commit()
    result = audit(db)
    assert result["duplicate_group_count"] == 1, result
    duplicate = result["duplicates"][0]
    assert duplicate["queue_id"] == "q", duplicate
    assert duplicate["wanted_id"] == "w", duplicate
    assert duplicate["multiple_live_client_jobs"] is False, duplicate
    assert len(duplicate["tasks"]) == 2, duplicate
    con.close()

# --- staged_filename_mismatch is never a live transfer ----------------------
# The status records that no staged file matched a waiting candidate. Nothing
# was handed to a download client, so there is no remote download to duplicate.
# These rows carry state='queued' -- a live phase -- so they used to read as
# live transfers, and two on one queue item read as two simultaneous downloads.
with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "state.sqlite3"
    inkdrop_state.ensure_schema(db)
    con = sqlite3.connect(db)
    con.execute("insert into series(id,title,created_at,updated_at) values('s','Series',1,1)")
    con.execute("insert into wanted_items(id,series_id,status,created_at,updated_at) values('w','s','wanted',1,1)")
    con.execute("insert into queue_items(id,wanted_id,series_id,state,active,created_at,updated_at) values('q','w','s','queued',1,1,1)")

    def mismatch(task_id, candidate="candidate-1", external=None, client="SLSKD"):
        con.execute(
            """insert into download_tasks(
                 id,queue_id,wanted_id,series_id,download_client,external_id,candidate_identity,
                 lifecycle_phase,status,state,started_at,updated_at
               ) values(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, "q", "w", "s", client, external, candidate,
             "observed", "staged_filename_mismatch", "queued", 1, 2),
        )

    # One on its own: not a transfer, so not live, so nothing to report.
    mismatch("mismatch-solo")
    con.commit()
    assert audit(db)["duplicate_group_count"] == 0, audit(db)

    # Two sharing a queue and a candidate: still not two downloads. This is the
    # shape cleanup_duplicate_candidate_mismatch_download_tasks() retires, but
    # the audit must not depend on that cleanup having run.
    mismatch("mismatch-pair")
    con.commit()
    assert audit(db)["duplicate_group_count"] == 0, audit(db)

    # The shape the cleanup deliberately skips: one row carries a download
    # client id, so it fails closed and leaves the whole group alone. That skip
    # used to leave a false duplicate standing here.
    mismatch("mismatch-with-client", external="job-77")
    con.commit()
    assert audit(db)["duplicate_group_count"] == 0, audit(db)

    # Guard the other direction: reclassifying the mismatch status must not
    # blind the audit to a genuine pair of simultaneous live downloads on the
    # same queue item and candidate.
    for task_id, external in (("real-live-1", "job-1"), ("real-live-2", "job-2")):
        con.execute(
            """insert into download_tasks(
                 id,queue_id,wanted_id,series_id,download_client,external_id,candidate_identity,
                 lifecycle_phase,status,state,started_at,updated_at
               ) values(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, "q", "w", "s", "qbittorrent", external, "candidate-real",
             "downloading", "downloading", "downloading", 1, 2),
        )
    con.commit()
    result = audit(db)
    assert result["duplicate_group_count"] == 1, result
    duplicate = result["duplicates"][0]
    assert duplicate["stable_task_identity"] == "candidate:candidate-real", duplicate
    assert duplicate["multiple_live_client_jobs"] is True, duplicate
    assert sorted(duplicate["client_ids"]) == ["job-1", "job-2"], duplicate
    assert len(duplicate["tasks"]) == 2, duplicate
    con.close()

print("inkdrop duplicate live task audit smoke: PASS")
