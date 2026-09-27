#!/usr/bin/env python3
"""Read-only, diff-based event detection for the notifications system.

Turns raw pipeline/state-DB rows into notification events without touching
the pipeline: every query here is a plain read-only SELECT against tables
inkdrop_state.py owns, diffed against last-seen state kept in
inkdrop_notification_store's watch-state table. Nothing here writes to
queue_items / download_tasks / series / issues, calls into the matching or
candidate-acceptance pipeline, or imports inkdrop_state.py's write paths.

"grabbed" and "download_failed" are scoped deliberately narrow: download_tasks
is a per-attempt table with heavy internal retry churn (most "failed" rows are
routine, automatically-retried candidate attempts, not something a user should
be paged about). So the signal used here is a task actually reaching a
downloading state (grabbed), and -- only for tasks this module already
announced as grabbed -- that same task later reaching a terminal failed state
(retry_eligible=0). That pairing is what keeps this from turning into noise.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from core import inkdrop_notification_store as store
from core import inkdrop_notifications
from core import inkdrop_db

DOWNLOAD_TASK_SCAN_LIMIT = 500
TASK_WATCH_PREFIX = "dt:"
TASK_WATCH_RETENTION_SECONDS = 30 * 86400

QUEUE_VERIFIED_SCAN_LIMIT = 500
QUEUE_VERIFIED_WATCH_PREFIX = "qv:"
QUEUE_VERIFIED_WATCH_RETENTION_SECONDS = 30 * 86400
# Same horizon as notify_wanted_cleared()'s own dedup window
# (store.DEFAULT_DEDUP_WINDOW_SECONDS) -- a queue row that's been sitting in
# state='verified' longer than this was not "just imported" by any
# reasonable definition, so it must not announce. This is the guard against
# a cold-start flood: the watermark defaults to 0 the first time this scan
# ever runs (or any time its watch-state is lost -- a restored backup, a
# manually-cleared key, a future migration), so that first pass's query
# matches every row currently sitting in state='verified' with no lower
# bound at all. Confirmed live 2026-08-11: scan_import_verified()'s original
# introduction (PR #423, watermark starting at 0) sent ~2500 queue_items rows
# through notify_wanted_cleared() in one pass -- rows verified anywhere from
# days to months earlier -- because nothing here ever asked "is this
# transition actually recent." The per-row "announced" watch-state (added
# alongside this guard) stops each row from firing twice, but does nothing
# to stop a stale row from firing once, long after the fact.
IMPORT_VERIFIED_STALE_SECONDS = store.DEFAULT_DEDUP_WINDOW_SECONDS

# A queue in one of these states (or inactive) has no automatic path left --
# matches the same terminal-state vocabulary inkdrop_slskd_source_probe.py
# already uses to decide which queues are still worth reconciling. Anything
# else (searching, downloading, importing, retry-scheduled, etc.) means the
# canonical unit can still complete through another source or candidate, so
# a failed attempt there must not be announced as an item-level failure.
QUEUE_TERMINAL_STATES = {
    "verified", "satisfied", "superseded_duplicate", "removed",
    "ignored", "inactive", "needs_you", "blocked",
}

HEALTH_CHECK_PROVIDERS = (
    ("comicvine", "ComicVine"),
    ("kavita", "Kavita"),
    ("prowlarr", "Prowlarr"),
    ("qbittorrent", "qBittorrent"),
    ("sabnzbd", "SABnzbd"),
    ("slskd", "Soulseek (slskd)"),
)

UPDATE_NOTABLE_STATES = {"update_available", "newer_prerelease_available"}


def _read_only_connect(db_path):
    con = sqlite3.connect(inkdrop_db.sqlite_readonly_uri(db_path), uri=True, timeout=30.0)
    con.row_factory = sqlite3.Row
    return con


def _issue_label(issue_number, issue_title):
    parts = []
    if issue_number:
        parts.append(f"#{issue_number}")
    if issue_title:
        parts.append(str(issue_title))
    return " ".join(parts) or None


def _read_cursor(db_path, cursor_key):
    """Read a scan cursor as (updated_at, id).

    Cursors written before this was a compound cursor carry only updated_at.
    They read back with an empty id, which re-includes every row sharing that
    exact timestamp on the next pass -- correct, because those are precisely
    the rows a timestamp-only cursor could have skipped. Rows that really were
    handled carry their own per-row watch-state and don't fire twice.
    """
    state = store.get_watch_state(db_path, cursor_key)
    try:
        updated_at = float(state.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0.0
    return updated_at, str(state.get("id") or "")


def _write_cursor(db_path, cursor_key, cursor):
    store.set_watch_state(db_path, cursor_key, {"updated_at": cursor[0], "id": cursor[1]})


class _Cursor:
    """Tracks how far a scan may durably advance.

    A scan cursor is only allowed to pass a row whose outcome is settled --
    announced and recorded, decided not to announce, or already handled by
    someone else. The moment a row can't be settled (its delivery didn't
    record, or another run holds it), the cursor stops advancing even though
    the loop keeps working through the rest of the batch. Later rows still get
    their own per-row watch-state, so re-reading them next pass is a no-op;
    the blocked row is the one that must come back.

    Without that stop, a row that failed to deliver would be left behind a
    cursor that had already moved past it -- unrecoverable, since the next
    query would never select it again.
    """

    def __init__(self, start):
        self.start = start
        self.value = start
        self.blocked = False

    def settle(self, row):
        if not self.blocked:
            self.value = (float(row["updated_at"] or 0), str(row["id"]))

    def block(self):
        self.blocked = True

    @property
    def advanced(self):
        return self.value != self.start


def scan_grabbed_and_download_failed(db_path):
    cursor_key = "scan:download_tasks:watermark"
    cursor = _Cursor(_read_cursor(db_path, cursor_key))
    since_ts, since_id = cursor.start

    con = _read_only_connect(db_path)
    try:
        rows = con.execute(
            """
            select dt.id, dt.series_id, dt.issue_id, dt.queue_id, dt.state, dt.retry_eligible,
                   dt.failure_reason, dt.updated_at,
                   s.title as series_title, i.issue_number, i.title as issue_title,
                   q.active as queue_active, q.state as queue_state, q.retry_after as queue_retry_after,
                   (
                       select count(*) from download_tasks sib
                       where sib.queue_id = dt.queue_id
                         and sib.id <> dt.id
                         and sib.state in ('downloading', 'import_ready', 'importing', 'verified')
                   ) as active_sibling_tasks
            from download_tasks dt
            left join series s on s.id = dt.series_id
            left join issues i on i.id = dt.issue_id
            left join queue_items q on q.id = dt.queue_id
            where dt.state in ('downloading', 'failed')
              and dt.updated_at >= ?
              and (dt.updated_at > ? or dt.id > ?)
            order by dt.updated_at asc, dt.id asc
            limit ?
            """,
            (since_ts, since_ts, since_id, DOWNLOAD_TASK_SCAN_LIMIT),
        ).fetchall()
    finally:
        con.close()

    fired = {"grabbed": 0, "download_failed": 0}
    for row in rows:
        task_key = f"{TASK_WATCH_PREFIX}{row['id']}"
        series_title = row["series_title"] or "A series"
        issue_label = _issue_label(row["issue_number"], row["issue_title"])

        if row["state"] == "downloading":
            # The claim doubles as the "already announced?" read: it reports
            # "done" for a task that's settled, so there's no lookup first.
            claim = store.claim_watch_flag(db_path, task_key, "grabbed_announced")
            if claim == "busy":
                cursor.block()
                continue
            if claim == "claimed":
                result = inkdrop_notifications.notify_grabbed(
                    db_path, series=series_title, issue_label=issue_label,
                    series_id=row["series_id"], issue_id=row["issue_id"], task_id=row["id"],
                )
                if not result.get("settled"):
                    store.release_watch_flag(db_path, task_key, "grabbed_announced")
                    cursor.block()
                    continue
                store.finish_watch_flag(db_path, task_key, "grabbed_announced")
                fired["grabbed"] += 1
        elif row["state"] == "failed" and int(row["retry_eligible"] or 0) == 0:
            # Only a task this module already announced as grabbed can report
            # a failure, so this branch does need the prior state, not just a
            # claim verdict.
            if not store.get_watch_state(db_path, task_key).get("grabbed_announced"):
                cursor.settle(row)
                continue
            claim = store.claim_watch_flag(db_path, task_key, "failed_announced")
            if claim == "busy":
                cursor.block()
                continue
            if claim == "claimed":
                queue_active = row["queue_active"]
                queue_state = str(row["queue_state"] or "").strip().lower()
                # A missing queue_items row (queue_active is None -- the join
                # found nothing) means we can't prove the canonical unit is
                # still retryable, so default to terminal wording rather than
                # silently understating a real permanent failure.
                queue_terminal = (
                    queue_active is None
                    or int(queue_active or 0) == 0
                    or queue_state in QUEUE_TERMINAL_STATES
                )
                has_retry_scheduled = bool(row["queue_retry_after"])
                has_active_sibling = int(row["active_sibling_tasks"] or 0) > 0
                terminal = queue_terminal and not has_retry_scheduled and not has_active_sibling
                result = inkdrop_notifications.notify_download_failed(
                    db_path, series=series_title, issue_label=issue_label, reason=row["failure_reason"],
                    series_id=row["series_id"], issue_id=row["issue_id"], task_id=row["id"],
                    terminal=terminal,
                )
                if not result.get("settled"):
                    store.release_watch_flag(db_path, task_key, "failed_announced")
                    cursor.block()
                    continue
                store.finish_watch_flag(db_path, task_key, "failed_announced")
                fired["download_failed"] += 1
        cursor.settle(row)

    if cursor.advanced:
        _write_cursor(db_path, cursor_key, cursor.value)
    store.prune_watch_state(db_path, TASK_WATCH_PREFIX, older_than_seconds=TASK_WATCH_RETENTION_SECONDS)
    return fired


def scan_import_verified(db_path):
    """import_verified used to be fired ad hoc from just 2 of the ~11 code
    paths that can mark a queue item state='verified' -- everything else
    (sync_queue, pack imports, folder-presence backfills, duplicate-issue
    reconciliation, and more) went silent. Watching the shared 'verified'
    transition itself, the same way scan_grabbed_and_download_failed watches
    download_tasks instead of every call site that can grab or fail a task,
    makes the notification independent of which code path completed the
    import.

    The cursor ((q.updated_at, q.id) > last-seen) is only a scan-window bound,
    not the dedup signal -- updated_at can move on an already-verified row for
    reasons that have nothing to do with a fresh import (a maintenance touch,
    a retried caller, a future call site nobody's audited yet), and relying
    on notify_wanted_cleared()'s own 24h occurrence-key window to absorb that
    meant a redundant write more than a day old produced a real duplicate
    notification. Each row gets its own "already announced" watch-state entry
    (mirrors scan_grabbed_and_download_failed's per-task_key state below) so
    a row can only ever fire import_verified once, no matter how many times
    updated_at moves afterward.

    A row older than IMPORT_VERIFIED_STALE_SECONDS is marked seen without
    notifying -- see that constant's comment for why a stale row can reach
    this scan at all (cold start / lost watch-state), not just a redundant
    write on a row already handled."""
    cursor_key = "scan:queue_items:import_verified:watermark"
    cursor = _Cursor(_read_cursor(db_path, cursor_key))
    since_ts, since_id = cursor.start
    now = time.time()

    con = _read_only_connect(db_path)
    try:
        rows = con.execute(
            """
            select q.id, q.series_id, q.issue_id, q.updated_at,
                   s.title as series_title, i.issue_number, i.title as issue_title
            from queue_items q
            left join series s on s.id = q.series_id
            left join issues i on i.id = q.issue_id
            where q.state = 'verified'
              and q.updated_at >= ?
              and (q.updated_at > ? or q.id > ?)
            order by q.updated_at asc, q.id asc
            limit ?
            """,
            (since_ts, since_ts, since_id, QUEUE_VERIFIED_SCAN_LIMIT),
        ).fetchall()
    finally:
        con.close()

    fired = 0
    for row in rows:
        watch_key = f"{QUEUE_VERIFIED_WATCH_PREFIX}{row['id']}"
        # The claim is also the "already announced?" read -- it reports "done"
        # for a row that's settled, so there's no separate lookup first.
        claim = store.claim_watch_flag(db_path, watch_key, "announced")
        if claim == "busy":
            cursor.block()
            continue
        if claim == "claimed":
            if now - float(row["updated_at"] or 0) > IMPORT_VERIFIED_STALE_SECONDS:
                # Deciding not to send is itself a durable outcome, so this
                # settles the row rather than blocking on it.
                store.finish_watch_flag(db_path, watch_key, "announced", extra={"stale": True})
            else:
                try:
                    result = inkdrop_notifications.notify_wanted_cleared(
                        db_path,
                        series=row["series_title"] or "A series",
                        issue_title=row["issue_title"],
                        issue_number=row["issue_number"],
                        series_id=row["series_id"],
                        issue_id=row["issue_id"],
                    )
                except Exception:
                    # notify_result() is documented never to raise -- this is
                    # defense in depth, not a path that should trigger.
                    result = {"settled": False}
                if not result.get("settled"):
                    store.release_watch_flag(db_path, watch_key, "announced")
                    cursor.block()
                    continue
                store.finish_watch_flag(db_path, watch_key, "announced")
                fired += 1
        cursor.settle(row)

    if cursor.advanced:
        _write_cursor(db_path, cursor_key, cursor.value)
    store.prune_watch_state(db_path, QUEUE_VERIFIED_WATCH_PREFIX, older_than_seconds=QUEUE_VERIFIED_WATCH_RETENTION_SECONDS)
    return {"import_verified": fired}


def scan_health(db_path):
    """Diff each curated provider's on-demand health check against its
    last-seen state. Only a clean 'error' -> non-error (or the reverse)
    transition fires -- 'unavailable'/'disabled' aren't treated as an issue
    since those usually just mean "not configured yet", not "was working,
    now broken"."""
    from core import inkdrop_web

    checks = {
        "comicvine": lambda: inkdrop_web.comicvine_api_health(),
        "kavita": lambda: inkdrop_web.kavita_api_health(),
        "prowlarr": lambda: inkdrop_web.prowlarr_api_health(timeout=4.0),
        "qbittorrent": lambda: inkdrop_web.qbittorrent_api_health(),
        "sabnzbd": lambda: inkdrop_web.sabnzbd_api_health(),
        "slskd": lambda: inkdrop_web.slskd_api_health(timeout=4.0),
    }
    fired = {"health_issue": 0, "health_restored": 0}
    for provider_id, label in HEALTH_CHECK_PROVIDERS:
        check = checks.get(provider_id)
        if check is None:
            continue
        try:
            health = check() or {}
        except Exception:
            continue
        state = str(health.get("state") or "").strip().lower()
        if not state:
            continue
        is_error = state == "error"
        watch_key = f"health:{provider_id}"
        seen = store.get_watch_state(db_path, watch_key)
        was_error = bool(seen.get("is_error"))
        # Same rule the row-based scanners follow: consuming the transition is
        # what makes it unrecoverable, so last-seen only moves once the
        # notification's outcome is actually recorded somewhere. Leaving it
        # unmoved re-detects the same edge on the next pass.
        if is_error and not was_error:
            result = inkdrop_notifications.notify_health_issue(db_path, provider_label=label, detail=health.get("detail"))
            if not result.get("settled"):
                continue
            fired["health_issue"] += 1
        elif was_error and not is_error:
            result = inkdrop_notifications.notify_health_restored(db_path, provider_label=label)
            if not result.get("settled"):
                continue
            fired["health_restored"] += 1
        store.set_watch_state(db_path, watch_key, {"is_error": is_error, "state": state})
    return fired


def scan_application_update(db_path):
    from core import inkdrop_version

    try:
        status = inkdrop_version.update_status()
    except Exception:
        return {"application_update": 0}
    state = str((status or {}).get("state") or "").strip()
    if not state:
        return {"application_update": 0}
    watch_key = "application_update"
    seen = store.get_watch_state(db_path, watch_key)
    fired = 0
    if state in UPDATE_NOTABLE_STATES and seen.get("state") not in UPDATE_NOTABLE_STATES:
        result = inkdrop_notifications.notify_application_update(
            db_path, state=state, headline=status.get("label") or state, detail=status.get("detail"),
        )
        if not result.get("settled"):
            # Don't consume the transition -- see scan_health().
            return {"application_update": 0}
        fired = 1
    store.set_watch_state(db_path, watch_key, {"state": state})
    return {"application_update": fired}


def run_scan(db_path):
    """Run every diff-based scanner once. Each is independently guarded --
    one raising doesn't stop the others."""
    results = {}
    for name, fn in (
        ("download_tasks", scan_grabbed_and_download_failed),
        ("import_verified", scan_import_verified),
        ("health", scan_health),
        ("application_update", scan_application_update),
    ):
        try:
            results[name] = fn(db_path)
        except Exception as exc:
            results[name] = {"error": f"{type(exc).__name__}: scan failed"}
    return results
