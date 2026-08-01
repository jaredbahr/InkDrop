#!/usr/bin/env python3
"""Shared SQLite connection and transient-lock policy for InkDrop-owned state."""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
from pathlib import Path


LOG = logging.getLogger("inkdrop.db")
LONG_WRITE_SECONDS = max(0.1, float(os.environ.get("INKDROP_SQLITE_LONG_WRITE_SECONDS") or "2.0"))


def is_locked_error(exc) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "database is locked" in str(exc).lower()


def _label(db_path) -> str:
    try:
        return Path(db_path).name or "sqlite"
    except (OSError, TypeError, ValueError):
        return "sqlite"


def open_connection(
    db_path,
    *,
    readonly=False,
    timeout_seconds=30.0,
    busy_timeout_ms=30000,
    configure_wal=True,
    autocommit=False,
    operation="sqlite_operation",
):
    timeout = max(0.1, float(timeout_seconds or 30.0))
    busy_timeout = max(100, int(busy_timeout_ms or 30000))
    if readonly:
        uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None if autocommit else "DEFERRED")
    else:
        con = sqlite3.connect(db_path, timeout=timeout, isolation_level=None if autocommit else "DEFERRED")
    con.row_factory = sqlite3.Row
    con.execute(f"pragma busy_timeout={busy_timeout}")
    con.execute("pragma foreign_keys=on")
    if readonly:
        con.execute("pragma query_only=1")
    elif configure_wal:
        try:
            current = str(con.execute("pragma journal_mode").fetchone()[0] or "").lower()
            if current != "wal":
                con.execute("pragma journal_mode=wal")
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc):
                con.close()
                raise
        con.execute("pragma synchronous=normal")
    return con


@contextlib.contextmanager
def connection(
    db_path,
    *,
    readonly=False,
    timeout_seconds=30.0,
    busy_timeout_ms=30000,
    configure_wal=True,
    autocommit=False,
    operation="sqlite_operation",
):
    busy_timeout = max(100, int(busy_timeout_ms or 30000))
    con = open_connection(
        db_path,
        readonly=readonly,
        timeout_seconds=timeout_seconds,
        busy_timeout_ms=busy_timeout,
        configure_wal=configure_wal,
        autocommit=autocommit,
        operation=operation,
    )
    started = time.monotonic()
    try:
        yield con
    except Exception as exc:
        if not readonly:
            con.rollback()
        if is_locked_error(exc):
            LOG.error(
                "sqlite lock persisted operation=%s db=%s busy_timeout_ms=%s",
                operation,
                _label(db_path),
                busy_timeout,
            )
        raise
    else:
        if not readonly:
            elapsed = time.monotonic() - started
            if con.in_transaction and elapsed >= LONG_WRITE_SECONDS:
                LOG.warning(
                    "long sqlite write transaction operation=%s db=%s elapsed_seconds=%.3f",
                    operation,
                    _label(db_path),
                    elapsed,
                )
            con.commit()
    finally:
        con.close()


def with_lock_retry(callback, *, attempts=3, initial_delay=0.1, operation="sqlite_operation"):
    attempts = max(1, int(attempts or 1))
    delay = max(0.0, float(initial_delay or 0.0))
    for attempt in range(attempts):
        try:
            return callback()
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc) or attempt >= attempts - 1:
                if is_locked_error(exc):
                    LOG.error(
                        "sqlite lock retries exhausted operation=%s attempts=%s",
                        operation,
                        attempts,
                    )
                raise
            time.sleep(delay * (2 ** attempt))
