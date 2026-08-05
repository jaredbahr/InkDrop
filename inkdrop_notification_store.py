#!/usr/bin/env python3
"""Durable storage for the InkDrop notifications system.

Owns its own tables in the shared state DB file -- per-channel event-trigger
toggles, per-channel series filters, quiet-hours/rate-limit/retry settings,
delivery history, and diff-based watch state for event detection.

Channel *enabled* state and *secrets* (Discord webhook URL, Pushover
token/user key) deliberately stay out of this module: they live on the
existing `provider_configs` row (id="notifications") that the generic
settings card already reads and writes, so there's exactly one place that
decides whether a channel is on and exactly one save button for it. Those
secrets are stored the same way every other provider credential in
provider_configs is stored -- plaintext in settings_json, masked in the UI
via secret_fields. No encryption layer here; see inkdrop_notifications.py
for how that row is read.

This module is storage-only. It does not send notifications and does not
decide when to fire them -- see inkdrop_notifications.py for the dispatch
pipeline that reads through here.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path


EVENT_TYPES = (
    "grabbed",
    "download_failed",
    "import_verified",
    "manual_action_required",
    "health_issue",
    "health_restored",
    "application_update",
)

CHANNEL_TYPES = ("discord", "pushover")

DELIVERY_STATUSES = ("sent", "sending", "failed", "queued", "deduped", "filtered", "disabled")
QUEUE_REASONS = ("quiet_hours", "retry", "rate_limit")

DEFAULT_URGENT_EVENTS = ("health_issue",)
DEFAULT_RATE_LIMIT_PER_HOUR = 20
# 24h, not 1h: traced against a real production case where a queue item
# ping-ponged between "verified" and "searching" every 20-90 minutes, all
# day, with notify_wanted_cleared firing unthrottled on every re-verification
# -- one issue alone re-fired 32 times in a day, 23,155 times across the
# library in a week, with no corresponding new download. A 1h window still
# lets same-day re-verifications spaced further apart than that slip
# through; 24h is the window that was actually proven to fully suppress it.
DEFAULT_DEDUP_WINDOW_SECONDS = 86400
DEFAULT_RETRY_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BACKOFF_SECONDS = 300
DEFAULT_HISTORY_RETENTION_DAYS = 30

SCHEMA_SQL = """
create table if not exists notification_channels (
    id text primary key,
    events_json text not null default '[]',
    series_filter_json text not null default '[]',
    created_at real not null,
    updated_at real not null
);
create table if not exists notification_settings (
    id text primary key,
    quiet_hours_enabled integer not null default 0,
    quiet_hours_start text,
    quiet_hours_end text,
    quiet_hours_days_json text not null default '[]',
    quiet_hours_urgent_events_json text not null default '[]',
    rate_limit_max_per_hour integer not null default 20,
    dedup_window_seconds integer not null default 86400,
    retry_max_attempts integer not null default 5,
    retry_backoff_seconds integer not null default 300,
    history_retention_days integer not null default 30,
    updated_at real not null
);
create table if not exists notification_deliveries (
    id text primary key,
    event_type text not null,
    channel_id text not null,
    occurrence_key text not null,
    series_id text,
    issue_id text,
    subject text not null,
    message text not null,
    status text not null,
    queue_reason text,
    attempt integer not null default 1,
    max_attempts integer not null default 1,
    error_detail text,
    created_at real not null,
    updated_at real not null,
    delivered_at real,
    next_attempt_at real
);
create index if not exists idx_notification_deliveries_occurrence
    on notification_deliveries(occurrence_key, channel_id, created_at);
create index if not exists idx_notification_deliveries_due
    on notification_deliveries(status, next_attempt_at);
create index if not exists idx_notification_deliveries_created
    on notification_deliveries(created_at);
create index if not exists idx_notification_deliveries_channel_sent
    on notification_deliveries(channel_id, status, created_at);
create index if not exists idx_notification_deliveries_channel_delivered
    on notification_deliveries(channel_id, status, coalesce(delivered_at, created_at));
create table if not exists notification_watch_state (
    key text primary key,
    value_json text not null default '{}',
    updated_at real not null
);
"""

GLOBAL_SETTINGS_ID = "global"


def ensure_schema(con):
    con.executescript(SCHEMA_SQL)
    return True


def _connect(db_path):
    con = sqlite3.connect(Path(db_path), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("pragma foreign_keys=on")
    ensure_schema(con)
    return con


@contextlib.contextmanager
def _connection(db_path):
    con = _connect(db_path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _json(value, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex}"


# --------------------------------------------------------------------------
# Channel preferences -- event-trigger toggles and series filters
#
# Channel *enabled* state and *secrets* (webhook URL, API token/key) stay on
# the existing `provider_configs` row (id="notifications") that the generic
# settings card already reads and writes, stored plaintext like every other
# provider credential. This table only owns what has no existing home: which
# events each channel is subscribed to, and which series it's scoped to.
# --------------------------------------------------------------------------

def _channel_prefs_row(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "events": sorted(set(_json(row["events_json"], []))),
        "series_filter": list(_json(row["series_filter_json"], [])),
        "updated_at": row["updated_at"],
    }


def _default_channel_prefs(channel_id):
    return {"id": channel_id, "events": [], "series_filter": [], "updated_at": None}


def list_channel_prefs(db_path):
    with _connection(db_path) as con:
        rows = con.execute("select * from notification_channels order by id").fetchall()
        by_id = {row["id"]: _channel_prefs_row(row) for row in rows}
    return [by_id.get(channel_id) or _default_channel_prefs(channel_id) for channel_id in CHANNEL_TYPES]


def save_channel_prefs(db_path, channel_id, *, events=None, series_filter=None):
    channel_id = str(channel_id or "").strip().lower()
    if channel_id not in CHANNEL_TYPES:
        raise ValueError(f"unknown notification channel: {channel_id}")
    now = time.time()
    with _connection(db_path) as con:
        row = con.execute("select * from notification_channels where id=?", (channel_id,)).fetchone()
        current = _channel_prefs_row(row) or _default_channel_prefs(channel_id)
        next_events = current["events"] if events is None else sorted(
            {str(e).strip() for e in events if str(e).strip() in EVENT_TYPES}
        )
        next_filter = current["series_filter"] if series_filter is None else [
            str(s).strip() for s in series_filter if str(s).strip()
        ]
        if row is None:
            con.execute(
                """insert into notification_channels(id, events_json, series_filter_json, created_at, updated_at)
                   values(?,?,?,?,?)""",
                (channel_id, _dump(next_events), _dump(next_filter), now, now),
            )
        else:
            con.execute(
                "update notification_channels set events_json=?, series_filter_json=?, updated_at=? where id=?",
                (_dump(next_events), _dump(next_filter), now, channel_id),
            )
        row = con.execute("select * from notification_channels where id=?", (channel_id,)).fetchone()
        return _channel_prefs_row(row)


# --------------------------------------------------------------------------
# Global settings (quiet hours, rate limit, dedup window, retry policy)
# --------------------------------------------------------------------------

def _settings_row(row):
    if not row:
        return {
            "quiet_hours_enabled": False,
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "07:00",
            "quiet_hours_days": [],
            "quiet_hours_urgent_events": list(DEFAULT_URGENT_EVENTS),
            "rate_limit_max_per_hour": DEFAULT_RATE_LIMIT_PER_HOUR,
            "dedup_window_seconds": DEFAULT_DEDUP_WINDOW_SECONDS,
            "retry_max_attempts": DEFAULT_RETRY_MAX_ATTEMPTS,
            "retry_backoff_seconds": DEFAULT_RETRY_BACKOFF_SECONDS,
            "history_retention_days": DEFAULT_HISTORY_RETENTION_DAYS,
            "updated_at": None,
        }
    return {
        "quiet_hours_enabled": bool(row["quiet_hours_enabled"]),
        "quiet_hours_start": row["quiet_hours_start"] or "22:00",
        "quiet_hours_end": row["quiet_hours_end"] or "07:00",
        "quiet_hours_days": list(_json(row["quiet_hours_days_json"], [])),
        "quiet_hours_urgent_events": list(_json(row["quiet_hours_urgent_events_json"], list(DEFAULT_URGENT_EVENTS))),
        "rate_limit_max_per_hour": int(row["rate_limit_max_per_hour"] or DEFAULT_RATE_LIMIT_PER_HOUR),
        "dedup_window_seconds": int(row["dedup_window_seconds"] or DEFAULT_DEDUP_WINDOW_SECONDS),
        "retry_max_attempts": int(row["retry_max_attempts"] or DEFAULT_RETRY_MAX_ATTEMPTS),
        "retry_backoff_seconds": int(row["retry_backoff_seconds"] or DEFAULT_RETRY_BACKOFF_SECONDS),
        "history_retention_days": int(row["history_retention_days"] or DEFAULT_HISTORY_RETENTION_DAYS),
        "updated_at": row["updated_at"],
    }


def get_settings(db_path):
    with _connection(db_path) as con:
        row = con.execute(
            "select * from notification_settings where id=?", (GLOBAL_SETTINGS_ID,)
        ).fetchone()
        return _settings_row(row)


def save_settings(db_path, patch):
    patch = dict(patch or {})
    now = time.time()
    with _connection(db_path) as con:
        row = con.execute(
            "select * from notification_settings where id=?", (GLOBAL_SETTINGS_ID,)
        ).fetchone()
        current = _settings_row(row)
        if "quiet_hours_enabled" in patch:
            current["quiet_hours_enabled"] = bool(patch["quiet_hours_enabled"])
        if "quiet_hours_start" in patch:
            current["quiet_hours_start"] = str(patch["quiet_hours_start"] or "22:00").strip()
        if "quiet_hours_end" in patch:
            current["quiet_hours_end"] = str(patch["quiet_hours_end"] or "07:00").strip()
        if "quiet_hours_days" in patch:
            current["quiet_hours_days"] = [str(d).strip().lower() for d in (patch["quiet_hours_days"] or []) if str(d).strip()]
        if "quiet_hours_urgent_events" in patch:
            current["quiet_hours_urgent_events"] = [
                str(e).strip() for e in (patch["quiet_hours_urgent_events"] or []) if str(e).strip() in EVENT_TYPES
            ]
        if "rate_limit_max_per_hour" in patch:
            current["rate_limit_max_per_hour"] = max(1, min(1000, int(patch["rate_limit_max_per_hour"] or DEFAULT_RATE_LIMIT_PER_HOUR)))
        if "dedup_window_seconds" in patch:
            current["dedup_window_seconds"] = max(0, min(604800, int(patch["dedup_window_seconds"] or DEFAULT_DEDUP_WINDOW_SECONDS)))
        if "retry_max_attempts" in patch:
            current["retry_max_attempts"] = max(0, min(20, int(patch["retry_max_attempts"] or DEFAULT_RETRY_MAX_ATTEMPTS)))
        if "retry_backoff_seconds" in patch:
            current["retry_backoff_seconds"] = max(30, min(3600, int(patch["retry_backoff_seconds"] or DEFAULT_RETRY_BACKOFF_SECONDS)))
        if "history_retention_days" in patch:
            current["history_retention_days"] = max(1, min(365, int(patch["history_retention_days"] or DEFAULT_HISTORY_RETENTION_DAYS)))
        con.execute(
            """insert into notification_settings(
                id, quiet_hours_enabled, quiet_hours_start, quiet_hours_end, quiet_hours_days_json,
                quiet_hours_urgent_events_json, rate_limit_max_per_hour, dedup_window_seconds,
                retry_max_attempts, retry_backoff_seconds, history_retention_days, updated_at
            ) values(?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(id) do update set
                quiet_hours_enabled=excluded.quiet_hours_enabled,
                quiet_hours_start=excluded.quiet_hours_start,
                quiet_hours_end=excluded.quiet_hours_end,
                quiet_hours_days_json=excluded.quiet_hours_days_json,
                quiet_hours_urgent_events_json=excluded.quiet_hours_urgent_events_json,
                rate_limit_max_per_hour=excluded.rate_limit_max_per_hour,
                dedup_window_seconds=excluded.dedup_window_seconds,
                retry_max_attempts=excluded.retry_max_attempts,
                retry_backoff_seconds=excluded.retry_backoff_seconds,
                history_retention_days=excluded.history_retention_days,
                updated_at=excluded.updated_at
            """,
            (
                GLOBAL_SETTINGS_ID,
                int(current["quiet_hours_enabled"]),
                current["quiet_hours_start"],
                current["quiet_hours_end"],
                _dump(current["quiet_hours_days"]),
                _dump(current["quiet_hours_urgent_events"]),
                current["rate_limit_max_per_hour"],
                current["dedup_window_seconds"],
                current["retry_max_attempts"],
                current["retry_backoff_seconds"],
                current["history_retention_days"],
                now,
            ),
        )
        current["updated_at"] = now
        return current


# --------------------------------------------------------------------------
# Delivery history
# --------------------------------------------------------------------------

def _delivery_row(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "event_type": row["event_type"],
        "channel_id": row["channel_id"],
        "occurrence_key": row["occurrence_key"],
        "series_id": row["series_id"],
        "issue_id": row["issue_id"],
        "subject": row["subject"],
        "message": row["message"],
        "status": row["status"],
        "queue_reason": row["queue_reason"],
        "attempt": row["attempt"],
        "max_attempts": row["max_attempts"],
        "error_detail": row["error_detail"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "delivered_at": row["delivered_at"],
        "next_attempt_at": row["next_attempt_at"],
    }


def record_delivery(
    db_path,
    *,
    event_type,
    channel_id,
    occurrence_key,
    subject,
    message,
    status,
    series_id=None,
    issue_id=None,
    queue_reason=None,
    attempt=1,
    max_attempts=1,
    error_detail=None,
    next_attempt_at=None,
):
    if status not in DELIVERY_STATUSES:
        raise ValueError(f"unknown delivery status: {status}")
    now = time.time()
    delivery_id = _new_id("ndv1")
    with _connection(db_path) as con:
        con.execute(
            """insert into notification_deliveries(
                id, event_type, channel_id, occurrence_key, series_id, issue_id,
                subject, message, status, queue_reason, attempt, max_attempts,
                error_detail, created_at, updated_at, delivered_at, next_attempt_at
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                delivery_id, event_type, channel_id, occurrence_key, series_id, issue_id,
                subject, message, status, queue_reason, int(attempt), int(max_attempts),
                error_detail, now, now, now if status == "sent" else None, next_attempt_at,
            ),
        )
        row = con.execute("select * from notification_deliveries where id=?", (delivery_id,)).fetchone()
        return _delivery_row(row)


def update_delivery(
    db_path,
    delivery_id,
    *,
    status,
    error_detail=None,
    next_attempt_at=None,
    attempt=None,
    queue_reason=None,
    expected_lease_until=None,
):
    if status not in DELIVERY_STATUSES:
        raise ValueError(f"unknown delivery status: {status}")
    now = time.time()
    with _connection(db_path) as con:
        fields = {"status": status, "updated_at": now, "error_detail": error_detail, "next_attempt_at": next_attempt_at}
        if status == "sent":
            fields["delivered_at"] = now
        if attempt is not None:
            fields["attempt"] = int(attempt)
        fields["queue_reason"] = queue_reason if status == "queued" else None
        assignments = ", ".join(f"{key}=?" for key in fields)
        where = "id=?"
        params = [*fields.values(), delivery_id]
        if expected_lease_until is not None:
            where += " and status='sending' and next_attempt_at=?"
            params.append(float(expected_lease_until))
        con.execute(f"update notification_deliveries set {assignments} where {where}", params)
        row = con.execute("select * from notification_deliveries where id=?", (delivery_id,)).fetchone()
        return _delivery_row(row)


def last_sent_at(db_path, occurrence_key, channel_id, *, within_seconds=None):
    """Most recent successful (or still-queued) delivery timestamp for this
    occurrence on this channel, or None. Used for dedup."""
    with _connection(db_path) as con:
        sent_clause = ""
        params = [occurrence_key, channel_id]
        if within_seconds is not None:
            sent_clause = " and coalesce(delivered_at, created_at) >= ?"
            params.append(time.time() - float(within_seconds))
        row = con.execute(
            f"""select coalesce(delivered_at, created_at) as occurred_at
                from notification_deliveries
                where occurrence_key=? and channel_id=? and (
                    status in ('sending','queued')
                    or (status='sent'{sent_clause})
                )
                order by occurred_at desc limit 1""",
            params,
        ).fetchone()
        return row["occurred_at"] if row else None


def sent_count_since(db_path, channel_id, since_ts):
    with _connection(db_path) as con:
        row = con.execute(
            """select count(*) as n from notification_deliveries
               where channel_id=? and status='sent'
                 and coalesce(delivered_at, created_at)>=?""",
            (channel_id, since_ts),
        ).fetchone()
        return int(row["n"] if row else 0)


def due_queued_deliveries(db_path, *, now=None, limit=100):
    now = time.time() if now is None else now
    with _connection(db_path) as con:
        rows = con.execute(
            """select * from notification_deliveries
               where status='queued' and (next_attempt_at is null or next_attempt_at<=?)
               order by created_at asc limit ?""",
            (now, int(limit)),
        ).fetchall()
        return [_delivery_row(row) for row in rows]


def reserve_new_delivery(
    db_path,
    *,
    event_type,
    channel_id,
    occurrence_key,
    subject,
    message,
    series_id=None,
    issue_id=None,
    max_attempts=1,
    dedup_window_seconds=0,
    max_per_hour=0,
    defer_reason=None,
    next_attempt_at=None,
    lease_seconds=30,
):
    """Atomically deduplicate, reserve capacity, and create one delivery."""
    now = time.time()
    delivery_id = _new_id("ndv1")
    lease_until = now + max(15, min(120, int(lease_seconds or 30)))
    with _connection(db_path) as con:
        con.execute("begin immediate")
        duplicate = None
        if dedup_window_seconds:
            duplicate = con.execute(
                """select 1 from notification_deliveries
                   where occurrence_key=? and channel_id=?
                     and (
                         status in ('sending','queued')
                         or (status='sent' and coalesce(delivered_at, created_at)>=?)
                     )
                   limit 1""",
                (occurrence_key, channel_id, now - float(dedup_window_seconds)),
            ).fetchone()
        if duplicate:
            status = "deduped"
            queue_reason = None
            detail = "already notified for this occurrence within the dedup window"
            due_at = None
        elif defer_reason:
            status = "queued"
            queue_reason = defer_reason
            detail = None
            due_at = next_attempt_at
        else:
            reserved = 0
            if max_per_hour:
                count_row = con.execute(
                    """select count(*) as n from notification_deliveries
                       where channel_id=? and (
                           (status='sent' and coalesce(delivered_at, created_at)>=?)
                           or (status='sending' and next_attempt_at>?)
                       )""",
                    (channel_id, now - 3600, now),
                ).fetchone()
                reserved = int(count_row["n"] if count_row else 0)
            if max_per_hour and reserved >= int(max_per_hour):
                oldest = con.execute(
                    """select min(coalesce(delivered_at, created_at)) as oldest
                       from notification_deliveries
                       where channel_id=? and status='sent'
                         and coalesce(delivered_at, created_at)>=?""",
                    (channel_id, now - 3600),
                ).fetchone()
                oldest_at = oldest["oldest"] if oldest else None
                status = "queued"
                queue_reason = "rate_limit"
                detail = None
                due_at = max(now + 30, float(oldest_at) + 3601) if oldest_at is not None else now + 30
            else:
                status = "sending"
                queue_reason = None
                detail = None
                due_at = lease_until
        con.execute(
            """insert into notification_deliveries(
                id,event_type,channel_id,occurrence_key,series_id,issue_id,
                subject,message,status,queue_reason,attempt,max_attempts,
                error_detail,created_at,updated_at,delivered_at,next_attempt_at
            ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                delivery_id, event_type, channel_id, occurrence_key, series_id, issue_id,
                subject, message, status, queue_reason, 0 if status == "queued" else 1,
                int(max_attempts), detail, now, now, None, due_at,
            ),
        )
        row = con.execute("select * from notification_deliveries where id=?", (delivery_id,)).fetchone()
        return _delivery_row(row)


def claim_next_due_delivery(db_path, *, now=None, max_per_hour=0, lease_seconds=30):
    """Reserve one due delivery and one channel-capacity slot atomically."""
    now = time.time() if now is None else float(now)
    lease_until = now + max(15, min(120, int(lease_seconds or 30)))
    with _connection(db_path) as con:
        con.execute("begin immediate")
        rows = con.execute(
            """select * from notification_deliveries
               where status in ('queued','sending')
                 and (next_attempt_at is null or next_attempt_at<=?)
               order by created_at asc limit 100""",
            (now,),
        ).fetchall()
        for row in rows:
            count_row = con.execute(
                """select count(*) as n from notification_deliveries
                   where channel_id=? and id<>? and (
                       (status='sent' and coalesce(delivered_at, created_at)>=?)
                       or (status='sending' and next_attempt_at>?)
                   )""",
                (row["channel_id"], row["id"], now - 3600, now),
            ).fetchone()
            reserved = int(count_row["n"] if count_row else 0)
            if max_per_hour and reserved >= int(max_per_hour):
                oldest = con.execute(
                    """select min(coalesce(delivered_at, created_at)) as oldest
                       from notification_deliveries
                       where channel_id=? and status='sent'
                         and coalesce(delivered_at, created_at)>=?""",
                    (row["channel_id"], now - 3600),
                ).fetchone()
                oldest_at = oldest["oldest"] if oldest else None
                due_at = max(now + 30, float(oldest_at) + 3601) if oldest_at is not None else now + 30
                queue_reason = "retry" if row["queue_reason"] == "retry" else "rate_limit"
                con.execute(
                    """update notification_deliveries
                       set status='queued', queue_reason=?, next_attempt_at=?, updated_at=?
                       where id=?""",
                    (queue_reason, due_at, now, row["id"]),
                )
                continue
            con.execute(
                """update notification_deliveries
                   set status='sending', next_attempt_at=?, updated_at=? where id=?""",
                (lease_until, now, row["id"]),
            )
            claimed = con.execute(
                "select * from notification_deliveries where id=?", (row["id"],)
            ).fetchone()
            return _delivery_row(claimed)
        return None


def list_deliveries(db_path, *, limit=100, before=None, event_type=None, channel_id=None, status=None):
    limit = max(1, min(500, int(limit or 100)))
    clauses = []
    params = []
    if before is not None:
        clauses.append("created_at < ?")
        params.append(before)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if channel_id:
        clauses.append("channel_id = ?")
        params.append(channel_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"where {' and '.join(clauses)}" if clauses else ""
    with _connection(db_path) as con:
        rows = con.execute(
            f"select * from notification_deliveries {where} order by created_at desc limit ?",
            [*params, limit],
        ).fetchall()
        return [_delivery_row(row) for row in rows]


def prune_history(db_path, *, retention_days=None):
    settings = get_settings(db_path) if retention_days is None else None
    days = retention_days if retention_days is not None else settings["history_retention_days"]
    cutoff = time.time() - (max(1, int(days)) * 86400)
    with _connection(db_path) as con:
        cur = con.execute(
            "delete from notification_deliveries where created_at < ? and status not in ('queued','sending')",
            (cutoff,),
        )
        return {"deleted": cur.rowcount if cur.rowcount is not None else 0}


# --------------------------------------------------------------------------
# Watch state (diff-based event detection: grabbed/download_failed/health/update)
# --------------------------------------------------------------------------

def get_watch_state(db_path, key):
    with _connection(db_path) as con:
        row = con.execute("select value_json from notification_watch_state where key=?", (key,)).fetchone()
        return _json(row["value_json"], {}) if row else {}


def set_watch_state(db_path, key, value):
    now = time.time()
    with _connection(db_path) as con:
        con.execute(
            """insert into notification_watch_state(key, value_json, updated_at) values(?,?,?)
               on conflict(key) do update set value_json=excluded.value_json, updated_at=excluded.updated_at""",
            (key, _dump(value), now),
        )


def watch_state_prefix(db_path, prefix):
    with _connection(db_path) as con:
        rows = con.execute(
            "select key, value_json from notification_watch_state where key like ? order by key",
            (f"{prefix}%",),
        ).fetchall()
        return {row["key"]: _json(row["value_json"], {}) for row in rows}


def prune_watch_state(db_path, prefix, *, older_than_seconds):
    cutoff = time.time() - max(0, int(older_than_seconds or 0))
    with _connection(db_path) as con:
        cur = con.execute(
            "delete from notification_watch_state where key like ? and updated_at < ?",
            (f"{prefix}%", cutoff),
        )
        return cur.rowcount if cur.rowcount is not None else 0


def delete_watch_state(db_path, keys):
    keys = [k for k in (keys or []) if k]
    if not keys:
        return 0
    with _connection(db_path) as con:
        placeholders = ",".join("?" for _ in keys)
        cur = con.execute(f"delete from notification_watch_state where key in ({placeholders})", keys)
        return cur.rowcount if cur.rowcount is not None else 0
