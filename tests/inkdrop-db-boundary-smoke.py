#!/usr/bin/env python3
"""Regression smoke for InkDrop's shared SQLite boundary and lock policy."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import inkdrop_db
import inkdrop_state


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-db-boundary-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "state.sqlite3"
        with inkdrop_db.connection(db_path, operation="boundary_schema") as con:
            check(con.row_factory is sqlite3.Row, "row factory is not sqlite3.Row")
            check(con.execute("pragma journal_mode").fetchone()[0].lower() == "wal", "WAL is not enabled")
            check(con.execute("pragma foreign_keys").fetchone()[0] == 1, "foreign keys are not enabled")
            check(con.execute("pragma busy_timeout").fetchone()[0] >= 100, "busy timeout is not configured")
            con.executescript(
                "create table parent(id integer primary key);"
                "create table child(id integer primary key, parent_id integer references parent(id));"
                "create table lock_probe(id integer primary key, value text);"
            )

        with inkdrop_db.connection(db_path, readonly=True, operation="boundary_read") as con:
            check(con.execute("pragma query_only").fetchone()[0] == 1, "read connection is not query-only")
            try:
                con.execute("insert into parent(id) values(1)")
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("read-only connection accepted a write")

        with inkdrop_state.connect(db_path) as con:
            check(con.execute("pragma foreign_keys").fetchone()[0] == 1, "state helper bypasses shared FK policy")

        blocker = sqlite3.connect(db_path, timeout=1, check_same_thread=False)
        blocker.execute("begin immediate")
        blocker.execute("insert into lock_probe(id, value) values(1, 'held')")

        def release():
            blocker.rollback()
            blocker.close()

        timer = threading.Timer(0.25, release)
        timer.start()

        def write_after_lock():
            with inkdrop_db.connection(
                db_path,
                timeout_seconds=0.1,
                busy_timeout_ms=100,
                operation="boundary_retry",
            ) as con:
                con.execute("insert into lock_probe(id, value) values(2, 'recovered')")
            return True

        try:
            recovered = inkdrop_db.with_lock_retry(
                write_after_lock,
                attempts=5,
                initial_delay=0.05,
                operation="boundary_retry",
            )
        finally:
            timer.join(timeout=2)
        check(recovered, "lock retry did not recover")
        with inkdrop_db.connection(db_path, readonly=True, operation="boundary_verify") as con:
            check(con.execute("select count(*) from lock_probe where id=2").fetchone()[0] == 1, "retry write missing")

    print("INKDROP_DB_BOUNDARY_SMOKE_OK")


if __name__ == "__main__":
    main()
