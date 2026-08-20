#!/usr/bin/env python3
"""Durable records for library scans that take longer than a browser tab lasts.

The CBZ conversion check and the library-adoption folder scan both walk every
file in the library. On a real library that is minutes at best and hours at
worst. Both used to keep their answer in a process-local dict, which meant a
page reload, a Settings re-render, or a container restart silently threw the
whole thing away and the panel went back to "Not checked yet." -- so the
feature only worked if you sat and watched it finish.

One row per run here, written while the scan is still going, so the answer
outlives the process that produced it. Callers get three things back that the
in-memory version could never give them: the result after a restart, when the
scan ran, and whether the library has moved underneath it since.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from core import inkdrop_state


KIND_ARCHIVE_CONVERSION_PLAN = "archive_conversion_plan"
KIND_ARCHIVE_CONVERSION_APPLY = "archive_conversion_apply"
KIND_LIBRARY_ADOPTION_PLAN = "library_adoption_plan"

KINDS = (
    KIND_ARCHIVE_CONVERSION_PLAN,
    KIND_ARCHIVE_CONVERSION_APPLY,
    KIND_LIBRARY_ADOPTION_PLAN,
)

STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"

# Keep enough history to answer "what did the last few runs say" without
# letting a nightly scheduled scan grow this table without bound.
RUNS_KEPT_PER_KIND = 10

# scan_progress fires once per file. Writing every one of those to SQLite would
# turn a read-only scan into a write storm, so the row is refreshed on a timer
# instead. A hard kill loses at most this many seconds of progress -- the run
# itself is still recoverable, which is the entire point.
PROGRESS_WRITE_INTERVAL_SECONDS = 2.0

_LAST_PROGRESS_WRITE = {}
_LAST_PROGRESS_WRITE_LOCK = threading.Lock()


def _json_dump(value):
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return None


def _json_load(value):
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _normalize_kind(kind):
    text = str(kind or "").strip()
    if text not in KINDS:
        raise ValueError(f"unknown scan run kind: {kind!r}")
    return text


def start_run(db_path, kind, *, scope_key="", options=None, progress=None, run_id=None, now=None):
    """Open a run row in `running` state and return its id."""
    kind = _normalize_kind(kind)
    run_id = str(run_id or uuid.uuid4().hex)
    stamp = float(now or time.time())
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        con.execute(
            """
            insert or replace into library_scan_runs
                (id, kind, scope_key, state, phase, started_at, updated_at, finished_at,
                 progress_json, result_json, error, fingerprint_json, options_json)
            values (?, ?, ?, ?, ?, ?, ?, null, ?, null, null, null, ?)
            """,
            (
                run_id,
                kind,
                str(scope_key or ""),
                STATE_RUNNING,
                (progress or {}).get("phase"),
                stamp,
                stamp,
                _json_dump(progress or {}),
                _json_dump(options or {}),
            ),
        )
        _trim_history(con, kind)
    with _LAST_PROGRESS_WRITE_LOCK:
        _LAST_PROGRESS_WRITE[run_id] = stamp
    return run_id


def record_progress(db_path, run_id, progress, *, phase=None, force=False, now=None):
    """Refresh a running row. Rate-limited unless `force` -- see the note above.

    Returns True when the row was actually written.
    """
    if not run_id:
        return False
    stamp = float(now or time.time())
    if not force:
        with _LAST_PROGRESS_WRITE_LOCK:
            last = _LAST_PROGRESS_WRITE.get(run_id, 0.0)
            if stamp - last < PROGRESS_WRITE_INTERVAL_SECONDS:
                return False
            _LAST_PROGRESS_WRITE[run_id] = stamp
    else:
        with _LAST_PROGRESS_WRITE_LOCK:
            _LAST_PROGRESS_WRITE[run_id] = stamp
    try:
        with inkdrop_state.connect(db_path) as con:
            con.execute(
                "update library_scan_runs set progress_json=?, phase=coalesce(?, phase), updated_at=? where id=?",
                (_json_dump(progress or {}), phase, stamp, run_id),
            )
    except Exception:
        # A scan that cannot write its progress is still a scan worth finishing.
        return False
    return True


def finish_run(db_path, run_id, *, state, result=None, error=None, fingerprint=None, progress=None, now=None):
    """Close a run out. `state` is one of completed / failed / interrupted."""
    if not run_id:
        return False
    stamp = float(now or time.time())
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        row = con.execute("select progress_json from library_scan_runs where id=?", (run_id,)).fetchone()
        progress_json = _json_dump(progress) if progress is not None else (row["progress_json"] if row else None)
        con.execute(
            """
            update library_scan_runs
               set state=?, result_json=?, error=?, fingerprint_json=?, progress_json=?,
                   updated_at=?, finished_at=?
             where id=?
            """,
            (
                str(state),
                _json_dump(result),
                str(error)[:500] if error else None,
                _json_dump(fingerprint),
                progress_json,
                stamp,
                stamp,
                run_id,
            ),
        )
    with _LAST_PROGRESS_WRITE_LOCK:
        _LAST_PROGRESS_WRITE.pop(run_id, None)
    return True


def _trim_history(con, kind):
    con.execute(
        """
        delete from library_scan_runs
         where kind=?
           and id not in (
               select id from library_scan_runs where kind=?
                order by coalesce(started_at, 0) desc limit ?
           )
        """,
        (kind, kind, RUNS_KEPT_PER_KIND),
    )


def _row_to_run(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "scope_key": row["scope_key"] or "",
        "state": row["state"],
        "phase": row["phase"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
        "progress": _json_load(row["progress_json"]) or {},
        "result": _json_load(row["result_json"]),
        "error": row["error"],
        "fingerprint": _json_load(row["fingerprint_json"]) or {},
        "options": _json_load(row["options_json"]) or {},
    }


def get_run(db_path, run_id):
    if not run_id:
        return None
    try:
        with inkdrop_state.connect_read(db_path) as con:
            row = con.execute("select * from library_scan_runs where id=?", (str(run_id),)).fetchone()
    except Exception:
        return None
    return _row_to_run(row)


def latest_run(db_path, kind, *, scope_key=None, states=None):
    """Most recent run of `kind`, optionally narrowed to one scanned scope."""
    kind = _normalize_kind(kind)
    sql = "select * from library_scan_runs where kind=?"
    params = [kind]
    if scope_key is not None:
        sql += " and scope_key=?"
        params.append(str(scope_key or ""))
    if states:
        sql += f" and state in ({','.join('?' for _ in states)})"
        params.extend(list(states))
    sql += " order by coalesce(started_at, 0) desc limit 1"
    try:
        with inkdrop_state.connect_read(db_path) as con:
            row = con.execute(sql, params).fetchone()
    except Exception:
        return None
    return _row_to_run(row)


def interrupt_orphaned_runs(db_path, *, now=None):
    """Close out runs still marked `running` from a process that is now gone.

    Called once at startup. Without it a container restart mid-scan leaves a row
    that claims to be running forever, and the panel sits on a progress bar that
    will never move again.
    """
    stamp = float(now or time.time())
    try:
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            cursor = con.execute(
                "update library_scan_runs set state=?, finished_at=?, updated_at=? where state=?",
                (STATE_INTERRUPTED, stamp, stamp, STATE_RUNNING),
            )
            return int(cursor.rowcount or 0)
    except Exception:
        return 0


# --- staleness -------------------------------------------------------------
#
# A scan is a photograph of the library. Acting on a week-old photograph is the
# failure this exists to prevent, so every stored result carries the handful of
# cheap library facts it was taken against, and we say plainly what has moved.


def compare_fingerprints(stored, current):
    """Returns {"stale": bool, "reasons": [str]} in words a human would use."""
    stored = stored if isinstance(stored, dict) else {}
    current = current if isinstance(current, dict) else {}
    reasons = []

    # Each reason is a bare clause: the caller puts "Since then, " in front of
    # the joined list, so nothing here repeats "since" or "your library".
    stored_roots = [str(item) for item in (stored.get("roots") or [])]
    current_roots = [str(item) for item in (current.get("roots") or [])]
    if stored_roots and current_roots and stored_roots != current_roots:
        reasons.append("your library folders changed in Settings")

    stored_count = stored.get("media_file_count")
    current_count = current.get("media_file_count")
    if isinstance(stored_count, (int, float)) and isinstance(current_count, (int, float)):
        delta = int(current_count) - int(stored_count)
        if delta > 0:
            reasons.append(f"{delta:,} file{'' if delta == 1 else 's'} landed")
        elif delta < 0:
            gone = abs(delta)
            reasons.append(f"{gone:,} file{'' if gone == 1 else 's'} went away")

    stored_series = stored.get("series_count")
    current_series = current.get("series_count")
    if isinstance(stored_series, (int, float)) and isinstance(current_series, (int, float)):
        delta = int(current_series) - int(stored_series)
        if delta > 0:
            reasons.append(f"{delta:,} series {'was' if delta == 1 else 'were'} added")

    return {"stale": bool(reasons), "reasons": reasons}


def library_fingerprint(db_path, *, roots=None):
    """Cheap facts that tell us the library moved, without walking it again."""
    fingerprint = {"roots": [str(root) for root in (roots or []) if str(root or "").strip()]}
    try:
        with inkdrop_state.connect_read(db_path) as con:
            row = con.execute("select count(*) as c from media_files where active=1").fetchone()
            fingerprint["media_file_count"] = int(row["c"] if row else 0)
            row = con.execute("select count(*) as c from series").fetchone()
            fingerprint["series_count"] = int(row["c"] if row else 0)
    except Exception:
        # No fingerprint is better than a wrong one: compare_fingerprints only
        # reports on keys present on both sides, so this degrades to "we can
        # only tell you how old it is", never to a false "still fresh".
        pass
    return fingerprint


def public_run(run, *, db_path=None, roots=None, now=None, current_fingerprint=None):
    """Shape a run for the API, with age and staleness already worked out."""
    if not run:
        return None
    stamp = float(now or time.time())
    finished_at = run.get("finished_at") or run.get("updated_at")
    payload = dict(run)
    payload["age_seconds"] = max(0.0, stamp - float(finished_at)) if finished_at else None
    if run.get("state") == STATE_COMPLETED:
        if current_fingerprint is None and db_path is not None:
            current_fingerprint = library_fingerprint(db_path, roots=roots)
        payload["staleness"] = compare_fingerprints(run.get("fingerprint"), current_fingerprint or {})
    else:
        payload["staleness"] = {"stale": False, "reasons": []}
    return payload
