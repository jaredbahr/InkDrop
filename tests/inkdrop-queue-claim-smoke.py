#!/usr/bin/env python3
"""Race and stale-owner recovery smoke for durable queue claims."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import inkdrop_state


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="inkdrop-queue-claim-") as temp:
        db_path = Path(temp) / "inkdrop-state.sqlite3"
        now = 1000.0
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            con.execute(
                "insert into series(id,title,media_type,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                ("series:claim", "Claim Smoke", "comic", 1, now, now, "{}"),
            )
            con.execute(
                "insert into wanted_items(id,series_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?)",
                ("wanted:claim", "series:claim", "wanted", now, now, "{}"),
            )
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,state,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
                ("queue:claim", "wanted:claim", "series:claim", "queued", 1, now, now, "{}"),
            )

        barrier = threading.Barrier(2)
        results = []
        result_lock = threading.Lock()

        def contender(owner):
            barrier.wait()
            result = inkdrop_state.claim_queue_item(
                db_path,
                "queue:claim",
                owner,
                operation="claim_smoke",
                lease_seconds=60,
                now=now,
            )
            with result_lock:
                results.append(result)

        threads = [threading.Thread(target=contender, args=(owner,)) for owner in ("owner-a", "owner-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        acquired = [row for row in results if row.get("acquired")]
        blocked = [row for row in results if not row.get("acquired")]
        if len(acquired) != 1 or len(blocked) != 1:
            raise AssertionError(f"expected one winner and one blocked owner: {results}")
        first_owner = acquired[0]["owner_id"]
        replacement = "owner-c"
        takeover = inkdrop_state.claim_queue_item(
            db_path,
            "queue:claim",
            replacement,
            operation="claim_smoke_recovery",
            lease_seconds=60,
            now=now + 61,
        )
        if not takeover.get("acquired") or takeover.get("owner_id") != replacement:
            raise AssertionError(f"expired claim was not recoverable: {takeover}")
        if inkdrop_state.release_queue_claim(db_path, "queue:claim", first_owner):
            raise AssertionError("stale owner released the replacement claim")
        if not inkdrop_state.release_queue_claim(db_path, "queue:claim", replacement):
            raise AssertionError("current owner could not release its claim")
    print("QUEUE_CLAIM_OK: one owner won the race, the peer was blocked, and stale-owner recovery was owner-safe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
