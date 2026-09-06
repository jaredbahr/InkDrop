#!/usr/bin/env python3
"""Retire an explicit list of wanted items, safely, one row at a time.

WHY THIS EXISTS
    Three repairs are authorised and none of them can be applied. Every one of
    the 58 `update wanted_items` writers in the product is EVENT-DRIVEN -- an
    import outcome, a queue transition, a series removal -- so not one of them
    takes a list of ids. There is nothing to call. Re-running a series removal
    does not do it either: that path is scoped `where status not in (satisfied,
    superseded_duplicate)`, so it deliberately preserves exactly the rows the
    repair is about.

WHY A NAIVE WRITE WOULD BE WRONG, AND WHY THIS ONE HOLDS
    Seven writers can move a want back to an active status. Two are guarded to
    active-only states and cannot touch a terminal row. One re-opens `satisfied`
    deliberately. One is guarded `not in ('ignored','removed_by_user')` -- which
    is why `removed_by_user` is the target status here and not a bespoke one: an
    existing re-opener already refuses to touch it, and it is a real live value.

    The remaining three are UNGUARDED on current status and would overwrite
    anything -- but each is reached only through a download task or queue row
    for that want. So the precondition that makes them unreachable is "no active
    queue row", and this asserts it inside the same transaction as the write.
    Measured on the 2026-08-27 snapshot: 0 of 121 existing `removed_by_user`
    wants have an active queue row, and 1 of 3,635 `satisfied` do. The assertion
    is nearly free, and the single exposed row is one it should refuse.

    This is the Spectre method in its simpler form. Spectre needed every layer
    written in one transaction because the value was re-derived from a
    projection with two inputs. `wanted_items.status` is not that: one column,
    many writers, all event-driven, nothing recomputing it. Remove the event
    source and the value is stable, so the runtime assertion is the whole of the
    borrowed method.

WHAT IT REFUSES
    The ROW, never the batch. A want holding an active queue row is skipped and
    reported; the rest still apply. A batch that fails wholesale on one bad row
    is how a repair gets abandoned halfway.

SAFETY
    Dry run is the DEFAULT. `--apply` is required to write anything, and the
    write runs in one transaction that re-checks the precondition per row.

    NOT REACHABLE IN PRODUCTION AS SHIPPED. The image copies exactly one file
    from tools/ and this is not it, deliberately: making it reachable is a
    one-line Dockerfile addition and that is a deployment decision nobody has
    taken yet. Until then this runs against a snapshot or a copy.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

TARGET_STATUS = "removed_by_user"


def load_ids(args) -> list[str]:
    ids: list[str] = list(args.want_id or [])
    if args.ids_file:
        ids += [ln.strip() for ln in Path(args.ids_file).read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def classify(con: sqlite3.Connection, want_id: str) -> dict:
    """What this row is, and whether it may be written. Read-only."""
    row = con.execute(
        "select id, series_id, issue_id, status from wanted_items where id=?", (want_id,)
    ).fetchone()
    if row is None:
        return {"want": want_id, "action": "refuse", "reason": "no such want"}
    active = con.execute(
        "select count(*) from queue_items where wanted_id=? and active=1", (want_id,)
    ).fetchone()[0]
    if active:
        # The precondition. Three unguarded writers reach a want through its
        # queue row; with one live, a write here is not durable.
        return {"want": want_id, "status": row["status"], "action": "refuse",
                "reason": f"{active} active queue row(s) -- the write would not hold"}
    if str(row["status"] or "").lower() == TARGET_STATUS:
        return {"want": want_id, "status": row["status"], "action": "skip",
                "reason": "already retired"}
    return {"want": want_id, "status": row["status"], "action": "retire"}


def run(db: Path, ids: list[str], apply: bool) -> dict:
    uri = f"file:{db.as_posix()}" + ("" if apply else "?mode=ro")
    con = sqlite3.connect(uri, uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    plan = [classify(con, i) for i in ids]
    written = 0
    if apply:
        todo = [p["want"] for p in plan if p["action"] == "retire"]
        try:
            con.execute("begin immediate")
            for want in todo:
                # Re-assert inside the transaction. The read above was outside it.
                still = con.execute(
                    "select count(*) from queue_items where wanted_id=? and active=1", (want,)
                ).fetchone()[0]
                if still:
                    for p in plan:
                        if p["want"] == want:
                            p.update(action="refuse",
                                     reason="queue row appeared between plan and write")
                    continue
                con.execute(
                    "update wanted_items set status=?, updated_at=strftime('%s','now') "
                    "where id=? and lower(coalesce(status,'')) <> ?",
                    (TARGET_STATUS, want, TARGET_STATUS),
                )
                written += con.execute(
                    "select changes()").fetchone()[0]
            con.commit()
        except Exception:
            con.rollback()
            raise
    con.close()
    counts: dict[str, int] = {}
    for p in plan:
        counts[p["action"]] = counts.get(p["action"], 0) + 1
    return {"db": str(db), "applied": apply, "requested": len(ids),
            "counts": counts, "written": written if apply else 0, "plan": plan}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", required=True, help="state database (a copy, unless --apply is meant)")
    p.add_argument("--want-id", action="append", help="repeatable")
    p.add_argument("--ids-file", help="one want id per line; # comments allowed")
    p.add_argument("--apply", action="store_true",
                   help="actually write. Without this nothing is modified and the DB opens read-only.")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    ids = load_ids(a)
    if not ids:
        raise SystemExit("no want ids given -- pass --want-id or --ids-file")
    res = run(Path(a.db), ids, a.apply)
    if a.json:
        print(json.dumps(res, indent=2))
        return
    print(f"{'APPLIED' if res['applied'] else 'DRY RUN'}"
          f"  db={res['db']}  requested={res['requested']}")
    for k, v in sorted(res["counts"].items()):
        print(f"  {k:<8} {v}")
    for row in res["plan"]:
        if row["action"] != "retire":
            print(f"    {row['action']:<7} {row['want']}  {row.get('reason','')}")
    if not res["applied"]:
        print("\nnothing was written. re-run with --apply to write.")


if __name__ == "__main__":
    main()
