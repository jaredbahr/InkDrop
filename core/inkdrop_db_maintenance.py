#!/usr/bin/env python3
"""Reclaim dead SQLite pages without ever locking the live database unpredictably.

Background. The production state database reached 45.7GB with 27% (11.4GB) of it
dead freelist pages. SQLite only reuses freed pages for new data; it never gives
them back to the filesystem unless something explicitly reclaims them, and
nothing here ever did. An untracked bulk delete turned that into a 60x freelist
spike in five days, and the only fix available was a full ``VACUUM`` -- which
rewrites the entire file under an exclusive lock and paused the app for close to
an hour.

That is why nothing in this module schedules a full ``VACUUM``. The reclaim path
that runs unattended is ``PRAGMA incremental_vacuum``, which hands pages back in
small bounded steps, each one a short write transaction the rest of the app can
interleave with. Measured on a synthetic database with InkDrop's shape (WAL,
synchronous=normal, two secondary indexes), a 1000-page step holds the write
lock ~28ms worst case, and reclaim throughput is the same whether the pages come
back 250 or 16000 at a time -- so the small step costs nothing but bounds the
stall.

Two details in here are load-bearing and easy to get wrong:

* ``PRAGMA incremental_vacuum`` only does its work as the statement is *stepped*.
  ``con.execute(...)`` alone steps it once and reclaims exactly ONE page, then
  leaves the statement open holding a lock -- a later ``wal_checkpoint`` on the
  same connection fails with "database table is locked". Measured: one
  unexhausted call moved a 56.5MB database to 56.5MB (1 page); the same call
  with the cursor exhausted moved it to 19.7MB (8,979 pages) in 204ms.
  Fetching the rows is NOT a reliable way to step it, which is the trap this
  module fell into: the pragma yields a zero-column row per reclaimed page,
  and a driver that reads "zero columns" as "no result set" stops after the
  first step. Every call here goes through ``_run_incremental_step``, which
  uses ``executescript`` -- that steps the statement to completion on every
  supported interpreter.
* ``auto_vacuum`` lives in the database header and can only be changed by a full
  ``VACUUM`` that runs *after* the pragma is set on the same connection. Setting
  the pragma alone on a populated database is silently a no-op. This is why the
  production install is still ``auto_vacuum=0`` even though a full VACUUM was
  run by hand -- the pragma was not set first, so the rewrite preserved NONE.

The consequence is a deliberate split: a brand-new database is created with
INCREMENTAL for free (the pragma takes effect immediately while the file is
still empty, no VACUUM needed), while an existing install has to pay one more
full-lock rewrite to convert. That conversion is an operator-triggered command
here, never a scheduled job, because it needs a maintenance window a timer
cannot know about.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from core import inkdrop_db
from core import inkdrop_runtime_config


STATUS_FILE_NAME = "db-maintenance-status.json"
MAINTENANCE_LOCK_NAME = "inkdrop-db-maintenance.lock"

AUTO_VACUUM_NONE = 0
AUTO_VACUUM_FULL = 1
AUTO_VACUUM_INCREMENTAL = 2
AUTO_VACUUM_NAMES = {0: "none", 1: "full", 2: "incremental"}

# 1000 pages == ~4MB at the production page_size of 4096. Measured worst-case
# write-lock hold for a step this size is ~28ms; 16000 pages reclaims no faster
# overall but stalls writers for ~400ms, which is the thing worth avoiding on a
# database the acquisition workers are writing to continuously.
DEFAULT_BATCH_PAGES = 1000
# Ceiling on one scheduled pass so reclaim can never become the heaviest writer
# on the database. 262144 pages == ~1GB at 4096. A backlog larger than this is
# drained over consecutive passes rather than in one long sitting.
DEFAULT_MAX_PAGES_PER_RUN = 262144
# Stop early if a pass has been running this long, even mid-budget, so the job
# always exits well inside its scheduler timeout instead of being SIGKILLed
# partway through (which is how the verified-import-projection job wedged).
DEFAULT_MAX_SECONDS_PER_RUN = 120.0
# Report dead space as a problem past this share of the file. The incident that
# prompted this work sat at 27%.
DEFAULT_DEAD_RATIO_WARN = 0.20
# A full VACUUM writes a complete second copy before swapping, so the filesystem
# needs room for both plus headroom. Refuse to start otherwise -- running out of
# space midway through a VACUUM on a 30GB database is the worst outcome here.
FULL_VACUUM_FREE_SPACE_MULTIPLIER = 2.2


def _bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def _bounded_int_env(env, name, default, minimum, maximum):
    raw = str((env or os.environ).get(name) or "").strip()
    return _bounded_int(raw or default, default, minimum, maximum)


def _bounded_float(value, default, minimum, maximum):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(float(minimum), min(float(maximum), parsed))


def auto_vacuum_mode(con) -> int:
    try:
        return int(con.execute("pragma auto_vacuum").fetchone()[0] or 0)
    except (sqlite3.Error, TypeError, ValueError, IndexError):
        return AUTO_VACUUM_NONE


def auto_vacuum_name(mode) -> str:
    return AUTO_VACUUM_NAMES.get(int(mode or 0), str(mode))


def configure_new_database_auto_vacuum(con) -> bool:
    """Put INCREMENTAL in the header while the file is still empty.

    The implementation lives in inkdrop_db so that module can call it during
    connection setup without importing this one. Only ever does anything on a
    database with zero pages -- i.e. the moment a fresh install creates its
    state database. On a populated database the pragma is a documented no-op,
    so this cannot silently half-apply to an existing install; converting those
    is ``convert_to_incremental``'s job and costs a full rewrite.
    """
    return inkdrop_db._configure_new_database_auto_vacuum(con)


def space_stats(con) -> dict:
    """Page accounting for the open database, in bytes as well as pages."""
    def _pragma(name, default=0):
        try:
            return int(con.execute(f"pragma {name}").fetchone()[0] or 0)
        except (sqlite3.Error, TypeError, ValueError, IndexError):
            return default

    page_size = _pragma("page_size", 4096) or 4096
    page_count = _pragma("page_count")
    freelist = _pragma("freelist_count")
    mode = auto_vacuum_mode(con)
    total_bytes = page_size * page_count
    dead_bytes = page_size * freelist
    return {
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist,
        "total_bytes": total_bytes,
        "dead_bytes": dead_bytes,
        "dead_ratio": (dead_bytes / total_bytes) if total_bytes else 0.0,
        "auto_vacuum": mode,
        "auto_vacuum_name": auto_vacuum_name(mode),
        "incremental_vacuum_available": mode == AUTO_VACUUM_INCREMENTAL,
    }


def _run_incremental_step(con, pages) -> None:
    """Reclaim up to ``pages`` free pages in one bounded write transaction.

    Stepping the statement to completion is the entire point -- see this
    module's docstring -- and ``execute()`` + ``fetchall()`` does not do it.
    ``PRAGMA incremental_vacuum(N)`` reclaims one page per step and yields a
    zero-column row each time; a driver that treats zero columns as "no result
    set" hands back an empty ``fetchall()`` after the first step, so the call
    reclaims exactly ONE page and still reports success -- the batch size is
    silently lost and the lock-hold bound with it.

    Measured on identical SQLite 3.45.1, one call of ``incremental_vacuum(500)``
    against a 745-page freelist: ``execute().fetchall()`` reclaimed 500 pages
    under CPython 3.10.20, 3.12.3 and 3.13.12 but 1 page under 3.11.15;
    ``executescript`` reclaimed 500 under all four. So the drain goes through
    ``executescript``, which is not driver-dependent.

    Callers must hold an autocommit connection: ``executescript`` commits an
    open transaction before it runs the script.
    """
    con.executescript(f"pragma incremental_vacuum({int(pages)});")


def run_incremental_vacuum(
    db_path=None,
    *,
    batch_pages=DEFAULT_BATCH_PAGES,
    max_pages=DEFAULT_MAX_PAGES_PER_RUN,
    max_seconds=DEFAULT_MAX_SECONDS_PER_RUN,
    busy_timeout_ms=5000,
    now=None,
):
    """Hand free pages back to the filesystem in bounded steps.

    Returns a summary rather than raising on the ordinary "cannot do anything
    right now" outcomes (auto_vacuum off, database busy), because this runs
    unattended and a scheduled job that raises on a contended database would
    just look like a failure.
    """
    db_path = Path(db_path or inkdrop_runtime_config.state_db_path())
    batch_pages = _bounded_int(batch_pages, DEFAULT_BATCH_PAGES, 50, 65536)
    max_pages = _bounded_int(max_pages, DEFAULT_MAX_PAGES_PER_RUN, batch_pages, 4194304)
    max_seconds = _bounded_float(max_seconds, DEFAULT_MAX_SECONDS_PER_RUN, 1.0, 3600.0)
    started = time.time() if now is None else float(now)
    summary = {
        "ok": True,
        "action": "incremental_vacuum",
        "db_path": str(db_path),
        "reclaimed_pages": 0,
        "reclaimed_bytes": 0,
        "steps": 0,
        "batch_pages": batch_pages,
        "stopped_because": "complete",
        "worst_step_seconds": 0.0,
        "elapsed_seconds": 0.0,
    }
    if not db_path.exists():
        summary.update(ok=False, skipped=True, reason="database_missing")
        return summary

    monotonic_start = time.monotonic()
    try:
        with inkdrop_db.connection(
            db_path,
            timeout_seconds=max(1.0, busy_timeout_ms / 1000.0),
            busy_timeout_ms=busy_timeout_ms,
            autocommit=True,
            operation="inkdrop_db_maintenance",
        ) as con:
            before = space_stats(con)
            summary["before"] = before
            if before["auto_vacuum"] != AUTO_VACUUM_INCREMENTAL:
                # Not a failure. Most installs that predate this module are
                # still auto_vacuum=NONE and converting them is an explicit,
                # operator-scheduled decision -- so say so plainly and exit
                # clean rather than failing a job forever.
                summary.update(
                    skipped=True,
                    reason="auto_vacuum_not_incremental",
                    stopped_because="auto_vacuum_not_incremental",
                    after=before,
                    elapsed_seconds=round(time.monotonic() - monotonic_start, 3),
                )
                return summary

            page_size = before["page_size"]
            worst = 0.0
            while True:
                remaining_free = int(con.execute("pragma freelist_count").fetchone()[0] or 0)
                if remaining_free <= 0:
                    summary["stopped_because"] = "complete"
                    break
                if summary["reclaimed_pages"] >= max_pages:
                    summary["stopped_because"] = "page_budget"
                    break
                if (time.monotonic() - monotonic_start) >= max_seconds:
                    summary["stopped_because"] = "time_budget"
                    break
                step_pages = min(batch_pages, remaining_free, max_pages - summary["reclaimed_pages"])
                step_started = time.monotonic()
                try:
                    _run_incremental_step(con, step_pages)
                except sqlite3.OperationalError as exc:
                    if not inkdrop_db.is_locked_error(exc):
                        raise
                    summary["stopped_because"] = "database_locked"
                    break
                worst = max(worst, time.monotonic() - step_started)
                after_free = int(con.execute("pragma freelist_count").fetchone()[0] or 0)
                moved = max(0, remaining_free - after_free)
                summary["steps"] += 1
                summary["reclaimed_pages"] += moved
                if moved == 0:
                    # Nothing budged despite a non-empty freelist. Pages pinned
                    # by an open read snapshot look exactly like this; spinning
                    # would burn the whole budget achieving nothing.
                    summary["stopped_because"] = "no_progress"
                    break

            summary["worst_step_seconds"] = round(worst, 4)
            summary["reclaimed_bytes"] = summary["reclaimed_pages"] * page_size
            summary["after"] = space_stats(con)
    except sqlite3.OperationalError as exc:
        summary.update(
            ok=not inkdrop_db.is_locked_error(exc),
            skipped=inkdrop_db.is_locked_error(exc),
            reason="database_locked" if inkdrop_db.is_locked_error(exc) else "sqlite_error",
            error=f"{type(exc).__name__}: {exc}",
            stopped_because="database_locked",
        )
    summary["elapsed_seconds"] = round(time.monotonic() - monotonic_start, 3)
    summary["started_at"] = started
    summary["completed_at"] = time.time()
    return summary


def full_vacuum_preflight(db_path=None, *, free_space_multiplier=FULL_VACUUM_FREE_SPACE_MULTIPLIER):
    """Refuse a full VACUUM that the filesystem cannot actually complete."""
    db_path = Path(db_path or inkdrop_runtime_config.state_db_path())
    result = {"ok": True, "db_path": str(db_path), "checks": []}
    if not db_path.exists():
        return {"ok": False, "db_path": str(db_path), "reason": "database_missing", "checks": []}
    db_bytes = db_path.stat().st_size
    try:
        free_bytes = shutil.disk_usage(db_path.parent).free
    except OSError as exc:
        return {
            "ok": False,
            "db_path": str(db_path),
            "reason": "disk_usage_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "checks": [],
        }
    required = int(db_bytes * float(free_space_multiplier))
    space_ok = free_bytes >= required
    result["db_bytes"] = db_bytes
    result["free_bytes"] = free_bytes
    result["required_bytes"] = required
    result["checks"].append({
        "name": "free_space",
        "ok": space_ok,
        "detail": f"need {required} bytes free, have {free_bytes}",
    })
    if not space_ok:
        result["ok"] = False
        result["reason"] = "insufficient_free_space"
    return result


def convert_to_incremental(
    db_path=None,
    *,
    busy_timeout_ms=60000,
    timeout_seconds=120.0,
    skip_preflight=False,
):
    """One-time conversion of an existing database to auto_vacuum=INCREMENTAL.

    Deliberately NOT wired into the scheduler. This runs a full ``VACUUM``,
    which takes an exclusive lock and rewrites the whole file -- close to an
    hour on the 30GB production database. An operator picks the window; a timer
    cannot.

    Order matters and is the reason the earlier hand-run VACUUM did not achieve
    this: the pragma must be set on the *same connection* before VACUUM runs,
    otherwise the rewrite faithfully preserves auto_vacuum=NONE.
    """
    db_path = Path(db_path or inkdrop_runtime_config.state_db_path())
    result = {"ok": True, "action": "convert_to_incremental", "db_path": str(db_path)}
    if not skip_preflight:
        preflight = full_vacuum_preflight(db_path)
        result["preflight"] = preflight
        if not preflight.get("ok"):
            result.update(ok=False, reason=preflight.get("reason") or "preflight_failed")
            return result

    started = time.monotonic()
    try:
        # autocommit: VACUUM cannot run inside a transaction, and the default
        # DEFERRED isolation would have Python open one implicitly.
        con = inkdrop_db.open_connection(
            db_path,
            timeout_seconds=timeout_seconds,
            busy_timeout_ms=busy_timeout_ms,
            autocommit=True,
            operation="inkdrop_db_maintenance_convert",
        )
    except sqlite3.Error as exc:
        result.update(ok=False, reason="connect_failed", error=f"{type(exc).__name__}: {exc}")
        return result
    try:
        result["before"] = space_stats(con)
        if result["before"]["auto_vacuum"] == AUTO_VACUUM_INCREMENTAL:
            result.update(skipped=True, reason="already_incremental", after=result["before"])
            return result
        con.execute(f"pragma auto_vacuum={AUTO_VACUUM_INCREMENTAL}")
        con.execute("VACUUM")
        result["after"] = space_stats(con)
        result["converted"] = result["after"]["auto_vacuum"] == AUTO_VACUUM_INCREMENTAL
        if not result["converted"]:
            result.update(ok=False, reason="conversion_did_not_apply")
    except sqlite3.Error as exc:
        result.update(ok=False, reason="vacuum_failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        con.close()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def status_path(environ=None):
    env = environ if environ is not None else os.environ
    override = str(env.get("INKDROP_DB_MAINTENANCE_STATUS_FILE") or "").strip()
    if override:
        return Path(override)
    return Path(inkdrop_runtime_config.state_dir(env)) / STATUS_FILE_NAME


def write_status(payload, *, path=None, environ=None):
    target = Path(path or status_path(environ))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, target)
    return target


def read_status(path=None, environ=None):
    target = Path(path or status_path(environ))
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def maintenance_report(summary, *, dead_ratio_warn=DEFAULT_DEAD_RATIO_WARN, now=None):
    """Turn a raw reclaim summary into something an operator can act on.

    The failure this is written against is not a crash -- it is a maintenance
    job that keeps reporting success while reclaiming nothing, which is exactly
    what an unexhausted incremental_vacuum cursor does. So the report carries
    the resulting dead-space ratio and an explicit advisory, not just "ok".
    """
    now = time.time() if now is None else float(now)
    after = summary.get("after") or summary.get("before") or {}
    dead_ratio = float(after.get("dead_ratio") or 0.0)
    auto_vacuum = int(after.get("auto_vacuum") or 0)
    report = {
        "schema_version": 1,
        "generated_at": now,
        "ok": bool(summary.get("ok", True)),
        "skipped": bool(summary.get("skipped")),
        "reason": summary.get("reason"),
        "stopped_because": summary.get("stopped_because"),
        "reclaimed_pages": int(summary.get("reclaimed_pages") or 0),
        "reclaimed_bytes": int(summary.get("reclaimed_bytes") or 0),
        "steps": int(summary.get("steps") or 0),
        "elapsed_seconds": float(summary.get("elapsed_seconds") or 0.0),
        "worst_step_seconds": float(summary.get("worst_step_seconds") or 0.0),
        "db_path": summary.get("db_path"),
        "total_bytes": int(after.get("total_bytes") or 0),
        "dead_bytes": int(after.get("dead_bytes") or 0),
        "dead_ratio": dead_ratio,
        "auto_vacuum": auto_vacuum,
        "auto_vacuum_name": auto_vacuum_name(auto_vacuum),
    }
    if auto_vacuum != AUTO_VACUUM_INCREMENTAL:
        report["advisory"] = "auto_vacuum_not_incremental"
        report["advisory_detail"] = (
            "Automatic page reclaim is unavailable on this database. Converting it "
            "requires one full VACUUM during a maintenance window "
            "(inkdrop_db_maintenance.py convert-auto-vacuum)."
        )
    elif dead_ratio >= float(dead_ratio_warn):
        report["advisory"] = "dead_space_high"
        report["advisory_detail"] = (
            f"{dead_ratio*100:.1f}% of the database is unreclaimed free pages."
        )
    else:
        report["advisory"] = None
        report["advisory_detail"] = None
    return report


def run_maintenance_pass(
    db_path=None,
    *,
    batch_pages=None,
    max_pages=None,
    max_seconds=None,
    dead_ratio_warn=None,
    environ=None,
    status_file=None,
    now=None,
):
    """The scheduled entry point: reclaim what is safe, then record what happened."""
    env = environ if environ is not None else os.environ
    summary = run_incremental_vacuum(
        db_path,
        batch_pages=_bounded_int_env(env, "INKDROP_DB_MAINTENANCE_BATCH_PAGES", batch_pages or DEFAULT_BATCH_PAGES, 50, 65536),
        max_pages=_bounded_int_env(env, "INKDROP_DB_MAINTENANCE_MAX_PAGES", max_pages or DEFAULT_MAX_PAGES_PER_RUN, 1000, 4194304),
        max_seconds=_bounded_int_env(env, "INKDROP_DB_MAINTENANCE_MAX_SECONDS", int(max_seconds or DEFAULT_MAX_SECONDS_PER_RUN), 1, 3600),
        now=now,
    )
    warn = _bounded_float(
        (env.get("INKDROP_DB_MAINTENANCE_DEAD_RATIO_WARN") or dead_ratio_warn or DEFAULT_DEAD_RATIO_WARN),
        DEFAULT_DEAD_RATIO_WARN,
        0.01,
        0.95,
    )
    report = maintenance_report(summary, dead_ratio_warn=warn, now=now)
    try:
        report["status_file"] = str(write_status(report, path=status_file, environ=env))
    except OSError as exc:
        report["status_write_error"] = f"{type(exc).__name__}: {exc}"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Scheduled pass: bounded incremental reclaim, then write status.")
    run.add_argument("--state-db")
    run.add_argument("--batch-pages", type=int, default=None)
    run.add_argument("--max-pages", type=int, default=None)
    run.add_argument("--max-seconds", type=float, default=None)
    run.add_argument("--status-file")
    run.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="Report page accounting without changing anything.")
    status.add_argument("--state-db")
    status.add_argument("--json", action="store_true")

    convert = sub.add_parser(
        "convert-auto-vacuum",
        help="One-time full VACUUM to enable auto_vacuum=INCREMENTAL. Needs a maintenance window.",
    )
    convert.add_argument("--state-db")
    convert.add_argument("--skip-preflight", action="store_true")
    convert.add_argument(
        "--yes",
        action="store_true",
        help="Required. Confirms an exclusive lock for the duration of a full rewrite.",
    )
    convert.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    db_path = Path(args.state_db) if getattr(args, "state_db", None) else inkdrop_runtime_config.state_db_path()

    if args.command == "status":
        if not Path(db_path).exists():
            payload = {"ok": False, "reason": "database_missing", "db_path": str(db_path)}
        else:
            with inkdrop_db.connection(
                db_path, readonly=True, timeout_seconds=10.0, busy_timeout_ms=5000,
                operation="inkdrop_db_maintenance_status",
            ) as con:
                payload = {"ok": True, "db_path": str(db_path), **space_stats(con)}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("ok") else 1

    if args.command == "convert-auto-vacuum":
        if not args.yes:
            print(json.dumps({
                "ok": False,
                "reason": "confirmation_required",
                "detail": "Re-run with --yes. This holds an exclusive lock and rewrites the "
                          "entire database (close to an hour on a 30GB file).",
            }, indent=2))
            return 2
        result = convert_to_incremental(db_path, skip_preflight=args.skip_preflight)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1

    report = run_maintenance_pass(
        db_path,
        batch_pages=args.batch_pages,
        max_pages=args.max_pages,
        max_seconds=args.max_seconds,
        status_file=args.status_file,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    # A database that simply has not been converted yet is a configuration
    # fact, not a job failure -- returning 78 keeps the scheduler from
    # retrying it on a failure backoff forever.
    if report.get("reason") == "auto_vacuum_not_incremental":
        return 78
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
