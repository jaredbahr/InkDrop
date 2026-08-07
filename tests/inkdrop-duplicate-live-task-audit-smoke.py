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

print("inkdrop duplicate live task audit smoke: PASS")
