#!/usr/bin/env python3
"""Regression for the per-issue monitored toggle (set_issue_monitored).

Covers: "I already own issue #50, stop re-acquiring it, but keep monitoring
the rest of the series" -- flipping issues.monitored=0 must (a) clear any
open Wanted row for that issue that isn't actively mid-download, (b) leave
an actively-downloading Wanted row alone, and (c) survive a later metadata
sync that would otherwise stomp the plain monitored column back to the
provider's value.
"""

import tempfile
from pathlib import Path

import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def add_fixture(con, name, *, active_client=False):
    series_id = f"series-{name}"
    wanted_id = f"wanted-{name}"
    con.execute(
        "insert into series(id,title,media_type,metadata_provider,metadata_id,source,monitored,monitor_new,auto_grab,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
        (series_id, f"Series {name}", "comic", "comicvine", name, "comicvine", 1, 1, 1, 1.0, 1.0, "{}"),
    )
    # Route through upsert_issue (not a hand-rolled row) so a later upsert_issue
    # call in the test genuinely lands on the same row via ON CONFLICT, the way
    # a real metadata sync would.
    issue_id = inkdrop_state.upsert_issue(
        con, series_id, {"id": f"cv-{name}", "issueNumber": "50", "monitored": True}, 1.0,
    )
    con.execute(
        "insert into wanted_items(id,series_id,issue_id,reason,status,priority,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
        (wanted_id, series_id, issue_id, "missing", "wanted", 50, 1.0, 1.0, "{}"),
    )
    if active_client:
        queue_id = f"queue-{name}"
        con.execute(
            "insert into queue_items(id,wanted_id,series_id,issue_id,state,current_source,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
            (queue_id, wanted_id, series_id, issue_id, "downloading", "slskd", 1, 1.0, 1.0, "{}"),
        )
    return {"series_id": series_id, "issue_id": issue_id, "wanted_id": wanted_id}


with tempfile.TemporaryDirectory() as temp_dir:
    db_path = Path(temp_dir) / "state.sqlite3"
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        idle = add_fixture(con, "idle")
        active = add_fixture(con, "active", active_client=True)
        con.commit()

    # 1. Setting monitored=False on an issue with only an idle (non-downloading)
    #    Wanted row clears that row -- it should stop showing as wanted at all.
    result = inkdrop_state.set_issue_monitored(db_path, idle["issue_id"], False)
    require(result["ok"], f"expected ok result, got {result}")
    require(result["issue"]["monitored"] is False, "issue should report monitored=False")
    require(result["wanted_items_cleared"] == 1, f"expected 1 wanted row cleared, got {result}")

    with inkdrop_state.connect(db_path) as con:
        row = con.execute("select monitored, monitored_user_override from issues where id=?", (idle["issue_id"],)).fetchone()
        require(row["monitored"] == 0, "issues.monitored should be 0 after toggle")
        require(row["monitored_user_override"] == 0, "monitored_user_override should record the explicit user choice")
        remaining = con.execute("select count(*) as c from wanted_items where id=?", (idle["wanted_id"],)).fetchone()["c"]
        require(remaining == 0, "the idle Wanted row should have been deleted")

    # 2. An issue with an actively-downloading Wanted row keeps that row --
    #    the toggle stops future re-acquisition, it does not kill an in-flight download.
    result = inkdrop_state.set_issue_monitored(db_path, active["issue_id"], False)
    require(result["ok"], f"expected ok result, got {result}")
    require(result["wanted_items_cleared"] == 0, f"expected the active download's wanted row to survive, got {result}")
    with inkdrop_state.connect(db_path) as con:
        remaining = con.execute("select count(*) as c from wanted_items where id=?", (active["wanted_id"],)).fetchone()["c"]
        require(remaining == 1, "the actively-downloading Wanted row must not be deleted")

    # 3. The user override survives a later metadata sync (upsert_issue) that would
    #    otherwise stomp monitored back to the provider's value on every ComicVine/
    #    Kapowarr/MangaDex refresh.
    with inkdrop_state.connect(db_path) as con:
        resynced_issue_id = inkdrop_state.upsert_issue(
            con,
            idle["series_id"],
            {
                "id": "cv-idle",
                "issueNumber": "50",
                "monitored": True,  # provider says monitored -- user said no
            },
            2.0,
        )
        con.commit()
        require(resynced_issue_id == idle["issue_id"], "sync should land on the same issue row (sanity check on the fixture)")
        row = con.execute("select monitored from issues where id=?", (idle["issue_id"],)).fetchone()
        require(row["monitored"] == 0, "a later metadata sync must not silently re-enable a user-cleared issue")

    # 4. Re-enabling clears the override in the other direction too.
    result = inkdrop_state.set_issue_monitored(db_path, idle["issue_id"], True)
    require(result["ok"], f"expected ok result, got {result}")
    require(result["issue"]["monitored"] is True, "issue should report monitored=True after re-enable")
    with inkdrop_state.connect(db_path) as con:
        row = con.execute("select monitored, monitored_user_override from issues where id=?", (idle["issue_id"],)).fetchone()
        require(row["monitored"] == 1, "issues.monitored should be 1 after re-enable")
        require(row["monitored_user_override"] == 1, "monitored_user_override should record the explicit re-enable too")

    # 5. Unknown issue id fails cleanly instead of raising.
    result = inkdrop_state.set_issue_monitored(db_path, "issue-does-not-exist", False)
    require(result == {"ok": False, "reason": "issue_not_found"}, f"expected a clean not-found result, got {result}")

print("inkdrop-issue-monitor-toggle-smoke: all checks passed")
