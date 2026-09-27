#!/usr/bin/env python3
"""Durable storage for the InkDrop notifications system.

Owns its own tables in the shared state DB file -- connector instances (Phase
1 of the Sonarr-style Connect redesign: N configured instances of M connector
types, not one fixed row per type), quiet-hours/rate-limit/retry settings,
delivery history, and diff-based watch state for event detection.

`notification_connectors` is the current model: one row per *configured
instance*, each carrying its own type, display name, enabled flag, settings
(secrets included -- plaintext, same as every other credential in
provider_configs; no encryption layer, see inkdrop_notifications.py), event
subscriptions, and series filter. This replaces the older one-row-per-type
`notification_channels` table plus the shared, type-prefixed-field
provider_configs(id="notifications") row that predated multi-instance
support. Both legacy shapes are read exactly once by `_migrate_legacy_
channels`, on first connect after upgrade, to create one connector per
previously-configured type -- deliberately reusing the literal type string
("discord"/"pushover") as that connector's id, so every existing
notification_deliveries.channel_id value keeps meaning exactly what it meant
before, with no backfill. Neither legacy table/row is deleted by the
migration; they simply stop being read.

`save_channel_prefs`/`list_channel_prefs` (the pre-Phase-1 API, keyed by
type-as-id) are kept as thin get-or-create wrappers over the connector
functions below -- not dead code, a real compatibility surface for any
caller still using a bare "discord"/"pushover" id, which continues to work
identically since that id is exactly what the migration assigns.

This module is storage-only. It does not send notifications and does not
decide when to fire them -- see inkdrop_notifications.py for the dispatch
pipeline that reads through here.
"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path

from core import inkdrop_validation
from core.inkdrop_display_labels import display_label


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

# "skipped" is distinct from "filtered" (series not in a channel's filter)
# and "disabled" (channel/event toggled off) -- both of those are pipeline
# decisions made before a delivery ever queues. "skipped" is for a delivery
# that already reserved a real queued slot and is later decided, after the
# fact, to be not worth sending -- e.g. an operator retroactively clearing a
# backlog of notifications a scanner bug queued for events too old to still
# be meaningful (see scan_import_verified()'s cold-start guard). Set only via
# update_delivery(); nothing in the dispatch pipeline produces it on its own.
DELIVERY_STATUSES = ("sent", "sending", "failed", "queued", "deduped", "filtered", "disabled", "skipped")
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
# 0 is in range for this one and means "do not suppress duplicates at all":
# reserve_new_delivery() skips the duplicate lookup entirely on a zero window.
DEFAULT_DEDUP_WINDOW_SECONDS = 86400
# Total sends, not retries on top of the first: 5 is one send plus four
# retries. 0 is in range and means "never retry", which still owes the one
# initial send -- see _attempt_send() in core/inkdrop_notifications.py.
DEFAULT_RETRY_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BACKOFF_SECONDS = 300
DEFAULT_HISTORY_RETENTION_DAYS = 30

# `notification_deliveries` is two things at once, and pruning has to respect
# both. It is the user-visible delivery history, and it is the live ledger the
# dispatch pipeline reads back: reserve_new_delivery() probes it for a prior
# 'sent'/'sending'/'queued' row on the same occurrence_key inside
# dedup_window_seconds, and the rate limiter counts 'sent'/'sending' rows
# inside the last hour. Deleting a row inside either horizon does not shrink
# history -- it silently re-arms a duplicate the dedup window was supposed to
# suppress, or lets a send slip past the per-hour ceiling. So every delete
# below is gated on RATE_LIMIT_WINDOW_SECONDS/dedup_window first.
RATE_LIMIT_WINDOW_SECONDS = 3600

# The two retention tiers. Measured on the live 42,461-row install over 11.4
# days: 88.9% of rows are 'deduped' -- 19 suppression records for every
# notification actually sent (37,566 deduped vs 1,984 sent on import_verified
# alone), and 20,016 rows landed on the busiest single day. Suppression rows
# are never read back by dedup or the rate limiter, which is why they can go
# early; 'sent'/'failed' are the user's actual delivery record and keep the
# full history_retention_days. Pruning suppression rows at 2 days reclaims
# 92.8% of that table while losing none of the 2,975-row delivery record.
HISTORY_RECORD_STATUSES = ("sent", "failed")
HISTORY_ACTIVE_STATUSES = ("queued", "sending")
DEFAULT_SUPPRESSION_RETENTION_SECONDS = 2 * 86400

# A backstop against pathological growth, not a display limit. Sized from the
# same measurements: after tiered pruning the steady state is ~8k rows (30
# days of the ~270 sent/day record) plus at most ~40k of a 2-day suppression
# window at the observed peak rate, so 50k is the worst realistic case and
# never binds in normal operation. It is gated on the pipeline horizon too --
# correctness outranks the ceiling, so a install that genuinely holds more
# than this inside its dedup window keeps the rows and reports the overage
# rather than breaking dedup to honour a number.
NOTIFICATION_HISTORY_MAX_ROWS = 50000

SCHEMA_SQL = """
create table if not exists schema_meta (
    key text primary key,
    value text not null
);
create table if not exists notification_connectors (
    id text primary key,
    type text not null,
    name text not null,
    enabled integer not null default 1,
    settings_json text not null default '{}',
    events_json text not null default '[]',
    series_filter_json text not null default '[]',
    created_at real not null,
    updated_at real not null
);
create index if not exists idx_notification_connectors_type on notification_connectors(type);
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

# Bump when SCHEMA_SQL changes in a way an existing database has to be
# brought up to. Every connection compares this against what the database
# carries; a mismatch re-runs the (idempotent) schema script and the
# legacy-channel migration, and a match does nothing at all.
SCHEMA_VERSION_KEY = "notification_schema_version"
SCHEMA_VERSION = "1"

GLOBAL_SETTINGS_ID = "global"

# Weekday vocabulary for quiet hours. Lives here, next to the contract that
# publishes it, because _in_quiet_hours() in core/inkdrop_notifications.py
# indexes the same seven names by time.struct_time.tm_wday and the two
# spellings must not be free to drift apart.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Fields the store owns and the caller does not set. get_settings() returns
# them, so a caller round-tripping a settings dict straight back in -- a
# restore, a "save what I just read" -- would otherwise be refused for
# sending a key it never chose.
SERVER_OWNED_SETTING_KEYS = frozenset({"updated_at"})


class SettingsValidationError(inkdrop_validation.ValidationError):
    """One notification setting was refused. The request was understood.

    A ValidationError, so the API guard answers 400 and logs one line instead
    of a stack trace, and a ValueError underneath that, so everything already
    catching ValueError around this writer keeps catching it.
    """


# The one contract for these settings.
#
# Before this existed the store coerced whatever it was handed: bool("false")
# is True, so the *string* "false" switched quiet hours on; "25:99" persisted
# as a start time and then made _in_quiet_hours() return False for every
# comparison, so the window read back as configured while never being in
# effect; ['noday'] persisted as a weekday list nothing would ever match. The
# HTTP route hands the parsed request body straight to save_settings(), so
# each of those was reachable from one request.
#
# Numeric policy lives here and only here. The settings form used to carry
# its own copy and had already drifted -- it capped the dedup window at
# 86,400 seconds against the store's 604,800 -- so the form now reads these
# bounds off the config payload instead of restating them.
SETTINGS_CONTRACT = {
    "quiet_hours_enabled": {"kind": "boolean", "default": False},
    "quiet_hours_start": {"kind": "time", "default": "22:00"},
    "quiet_hours_end": {"kind": "time", "default": "07:00"},
    "quiet_hours_days": {
        "kind": "enum_list", "choices": WEEKDAYS, "default": (),
        "note": "empty means every day",
    },
    "quiet_hours_urgent_events": {
        "kind": "enum_list", "choices": EVENT_TYPES, "default": DEFAULT_URGENT_EVENTS,
    },
    "rate_limit_max_per_hour": {
        "kind": "integer", "min": 1, "max": 1000, "default": DEFAULT_RATE_LIMIT_PER_HOUR,
    },
    "dedup_window_seconds": {
        "kind": "integer", "min": 0, "max": 604800, "default": DEFAULT_DEDUP_WINDOW_SECONDS,
        "zero_means": "send every occurrence; no duplicate suppression",
    },
    "retry_max_attempts": {
        "kind": "integer", "min": 0, "max": 20, "default": DEFAULT_RETRY_MAX_ATTEMPTS,
        "note": "total sends, not retries on top of the first",
        "zero_means": "never retry a failed send",
    },
    "retry_backoff_seconds": {
        "kind": "integer", "min": 30, "max": 3600, "default": DEFAULT_RETRY_BACKOFF_SECONDS,
    },
    "history_retention_days": {
        "kind": "integer", "min": 1, "max": 365, "default": DEFAULT_HISTORY_RETENTION_DAYS,
    },
}


def settings_contract():
    """The contract, JSON-safe, for the settings form to build itself from."""
    published = {}
    for key, spec in SETTINGS_CONTRACT.items():
        row = {"kind": spec["kind"]}
        if "choices" in spec:
            row["choices"] = list(spec["choices"])
        for field in ("min", "max", "note", "zero_means"):
            if field in spec:
                row[field] = spec[field]
        default = spec["default"]
        row["default"] = list(default) if isinstance(default, tuple) else default
        published[key] = row
    return published


def _validated_boolean(key, value):
    # Strictly bool, the same bar the settings registry holds. Accepting
    # truthy strings is what turned "false" into True.
    if not isinstance(value, bool):
        raise SettingsValidationError(f"{key} must be true or false")
    return value


def _validated_time(key, value):
    raw = str(value if value is not None else "").strip()
    hours, _, minutes = raw.partition(":")
    try:
        hours, minutes = int(hours), int(minutes)
    except ValueError:
        raise SettingsValidationError(f"{key} must be a time of day as HH:MM") from None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise SettingsValidationError(f"{key} must be between 00:00 and 23:59")
    # Canonical zero-padded form: the settings form feeds this straight into
    # an <input type="time">, which only renders HH:MM.
    return f"{hours:02d}:{minutes:02d}"


def _validated_enum_list(key, value, choices):
    if not isinstance(value, (list, tuple)):
        raise SettingsValidationError(f"{key} must be a list")
    seen, cleaned = set(), []
    for item in value:
        name = str(item if item is not None else "").strip().lower()
        if name not in choices:
            raise SettingsValidationError(
                f"{key} contains an unknown value: {item!r} (expected one of: {', '.join(choices)})"
            )
        if name not in seen:
            seen.add(name)
            cleaned.append(name)
    return cleaned


def _validated_integer(key, value, spec):
    # bool is an int in Python, and True would sail through as 1.
    if isinstance(value, bool):
        raise SettingsValidationError(f"{key} must be a whole number")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise SettingsValidationError(f"{key} must be a whole number") from None
    if not math.isfinite(parsed) or parsed != int(parsed):
        raise SettingsValidationError(f"{key} must be a whole number")
    parsed = int(parsed)
    if parsed < spec["min"] or parsed > spec["max"]:
        raise SettingsValidationError(f"{key} must be between {spec['min']} and {spec['max']}")
    return parsed


def validate_settings_patch(patch):
    """Validate a whole settings patch before any of it is written.

    Returns the cleaned values for exactly the keys the patch carried. Raises
    on the first bad field, having written nothing -- which is the point: a
    patch carrying one bad time used to leave the other nine fields updated
    and the tenth silently wrong.

    None, and the empty string a cleared form field sends, mean "use this
    field's default" rather than "write nothing" -- zero is a value, and
    conflating the two is what B01 fixed in the storage layer.
    """
    patch = dict(patch or {})
    for key in SERVER_OWNED_SETTING_KEYS:
        patch.pop(key, None)
    unknown = sorted(set(patch) - set(SETTINGS_CONTRACT))
    if unknown:
        raise SettingsValidationError(f"unknown notification setting: {', '.join(unknown)}")
    cleaned = {}
    for key, value in patch.items():
        spec = SETTINGS_CONTRACT[key]
        if value is None or value == "":
            default = spec["default"]
            cleaned[key] = list(default) if isinstance(default, tuple) else default
            continue
        kind = spec["kind"]
        if kind == "boolean":
            cleaned[key] = _validated_boolean(key, value)
        elif kind == "time":
            cleaned[key] = _validated_time(key, value)
        elif kind == "enum_list":
            cleaned[key] = _validated_enum_list(key, value, spec["choices"])
        else:
            cleaned[key] = _validated_integer(key, value, spec)
    return cleaned



def ensure_schema(con):
    con.executescript(SCHEMA_SQL)
    return True


def _initialized_version(con):
    """The notification schema version this database already carries.

    None when it cannot say -- no schema_meta table (an ordinary first run),
    no row, or an unreadable value. None always means "initialise", so being
    unable to answer is never mistaken for being up to date.
    """
    try:
        row = con.execute(
            "select value from schema_meta where key=?", (SCHEMA_VERSION_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    value = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    return str(value or "").strip() or None


def _ensure_initialized(con):
    """Bring this database up to the current schema, if it is not already.

    WHY THIS IS A PROBE AND NOT A PROCESS CACHE. Every _connect() used to run
    the whole schema script and the legacy-channel migration check before the
    caller's own statement -- 11 CREATEs and a commit on a plain settings
    read, against an already-initialised database, on a connection that had
    to be writable to do it.

    The obvious fix is to remember in the process that this path was
    initialised. It is also wrong: a database can be REPLACED underneath a
    running process -- a restore from backup, an operator swapping the file --
    and a cache keyed on the path would then report a brand-new database as
    initialised and skip the schema it actually needs. Keying on inode or
    mtime only moves the guess, and st_ino is not meaningful on Windows,
    which is a supported development platform here.

    So the check is one SELECT against the database in front of us. It costs a
    single indexed lookup instead of 11 CREATEs, it cannot go stale because
    it is not a memory of anything, and a swapped file answers for itself.

    Returns True when it wrote, which is what the read-path assertion in the
    smoke test reads.
    """
    if _initialized_version(con) == SCHEMA_VERSION:
        return False
    # CREATE TABLE IF NOT EXISTS is idempotent and SQLite serialises writers,
    # so two processes arriving here together is safe: one wins the write
    # lock, the other re-runs harmless DDL and re-stamps the same version.
    ensure_schema(con)
    _migrate_legacy_channels(con)
    con.execute(
        "insert into schema_meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value",
        (SCHEMA_VERSION_KEY, SCHEMA_VERSION),
    )
    con.commit()
    return True


def _connect(db_path):
    con = sqlite3.connect(Path(db_path), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("pragma foreign_keys=on")
    _ensure_initialized(con)
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


def _stored_int(value, default):
    """Read one integer settings column.

    `value or default` cannot express a setting whose off switch is zero:
    0 and NULL are both falsy, so an operator who turned dedup or retries
    off read the default back and the control looked untouched. Only a
    NULL column -- a row written before the column existed -- falls back.
    """
    return int(default if value is None else value)


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

MIGRATE_LEGACY_CHANNELS_SCHEMA_KEY = "notification_connectors_legacy_migration_v1"


# Old provider_configs field name -> new, unprefixed per-connector settings
# key. Migration-only: new connectors (of any type, including a second
# instance of an already-real type) are created directly with unprefixed
# keys and never touch this map.
_LEGACY_SECRET_FIELD_MAP = {
    "discord": {"webhook_url": "discord_webhook_url"},
    "pushover": {"api_token": "pushover_api_token", "user_key": "pushover_user_key"},
}


def _migrate_legacy_channels(con, now=None):
    """One-shot: create one notification_connectors row per previously-
    configured legacy type, reusing the literal type string ("discord"/
    "pushover") as that connector's id. That id reuse is load-bearing, not
    cosmetic -- every existing notification_deliveries.channel_id value
    already reads "discord"/"pushover", so reusing those exact strings as the
    new connector ids means zero backfill and zero change in meaning for any
    existing delivery-history row. Neither legacy source (the
    notification_channels table or provider_configs' type-prefixed fields) is
    deleted or modified; they simply stop being read once this has run.

    A type with nothing configured (no secret, no events, no series filter)
    gets no connector at all -- a fresh install ends up with zero connectors,
    same as a real user would expect to configure from scratch, not two
    empty phantom rows.
    """
    try:
        done = con.execute(
            "select value from schema_meta where key=?", (MIGRATE_LEGACY_CHANNELS_SCHEMA_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return
    already_done = bool(done) and str(done["value"] if not isinstance(done, tuple) else done[0]) == "1"
    if already_done:
        return
    now = float(now or time.time())
    try:
        provider_row = con.execute(
            "select enabled, settings_json from provider_configs where id='notifications'"
        ).fetchone()
    except sqlite3.Error:
        provider_row = None
    legacy_settings = _json((provider_row["settings_json"] if provider_row else None) or "{}", {})
    row_enabled = bool(provider_row["enabled"]) if provider_row else True
    try:
        legacy_channels = {row["id"]: row for row in con.execute("select * from notification_channels").fetchall()}
    except sqlite3.Error:
        legacy_channels = {}
    for channel_type, field_map in _LEGACY_SECRET_FIELD_MAP.items():
        exists = con.execute("select 1 from notification_connectors where id=?", (channel_type,)).fetchone()
        if exists:
            continue
        settings = {
            new_key: legacy_settings.get(old_key)
            for new_key, old_key in field_map.items()
            if legacy_settings.get(old_key)
        }
        legacy_channel = legacy_channels.get(channel_type)
        events = list(_json(legacy_channel["events_json"], [])) if legacy_channel else []
        series_filter = list(_json(legacy_channel["series_filter_json"], [])) if legacy_channel else []
        if not settings and not events and not series_filter:
            continue
        enabled = row_enabled and legacy_settings.get(f"{channel_type}_enabled", True) is not False
        created_at = float(legacy_channel["created_at"]) if legacy_channel else now
        con.execute(
            """insert into notification_connectors(
                id, type, name, enabled, settings_json, events_json, series_filter_json, created_at, updated_at
            ) values(?,?,?,?,?,?,?,?,?)""",
            (
                channel_type, channel_type, channel_type.title(), int(bool(enabled)),
                _dump(settings), _dump(sorted(set(events))), _dump(series_filter), created_at, now,
            ),
        )
    try:
        con.execute(
            "insert into schema_meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value",
            (MIGRATE_LEGACY_CHANNELS_SCHEMA_KEY, "1"),
        )
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------
# Connectors -- one row per configured instance (Phase 1: only the "discord"
# and "pushover" types exist, but any number of instances of either type is
# already a real, working shape -- adding a new type later is a registry
# entry in inkdrop_notifications.py, not a schema change here).
# --------------------------------------------------------------------------

def _connector_row(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "settings": dict(_json(row["settings_json"], {})),
        "events": sorted(set(_json(row["events_json"], []))),
        "series_filter": list(_json(row["series_filter_json"], [])),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_connectors(db_path):
    with _connection(db_path) as con:
        rows = con.execute("select * from notification_connectors order by created_at, id").fetchall()
        return [_connector_row(row) for row in rows]


def get_connector(db_path, connector_id):
    connector_id = str(connector_id or "").strip()
    if not connector_id:
        return None
    with _connection(db_path) as con:
        row = con.execute("select * from notification_connectors where id=?", (connector_id,)).fetchone()
        return _connector_row(row)


def create_connector(db_path, *, type, name=None, connector_id=None, settings=None, events=None, series_filter=None, enabled=True):
    connector_type = str(type or "").strip().lower()
    if not connector_type:
        raise ValueError("connector type is required")
    connector_id = str(connector_id or "").strip() or _new_id("nc")
    now = time.time()
    next_events = sorted({str(e).strip() for e in (events or []) if str(e).strip() in EVENT_TYPES})
    next_filter = [str(s).strip() for s in (series_filter or []) if str(s).strip()]
    # From `type`, not `connector_type`: the latter is lower-cased for storage,
    # and a name is not a key. A connector type this build does not know is
    # shown as it was given rather than re-cased into something else.
    display_name = str(name or "").strip() or display_label(type)
    with _connection(db_path) as con:
        if con.execute("select 1 from notification_connectors where id=?", (connector_id,)).fetchone():
            raise ValueError(f"connector already exists: {connector_id}")
        con.execute(
            """insert into notification_connectors(
                id, type, name, enabled, settings_json, events_json, series_filter_json, created_at, updated_at
            ) values(?,?,?,?,?,?,?,?,?)""",
            (
                connector_id, connector_type, display_name, int(bool(enabled)),
                _dump(dict(settings or {})), _dump(next_events), _dump(next_filter), now, now,
            ),
        )
        row = con.execute("select * from notification_connectors where id=?", (connector_id,)).fetchone()
        return _connector_row(row)


def update_connector(db_path, connector_id, *, name=None, enabled=None, settings=None, events=None, series_filter=None):
    connector_id = str(connector_id or "").strip()
    if not connector_id:
        raise ValueError("connector id is required")
    now = time.time()
    with _connection(db_path) as con:
        row = con.execute("select * from notification_connectors where id=?", (connector_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown connector: {connector_id}")
        current = _connector_row(row)
        next_name = str(name).strip() if name is not None and str(name).strip() else current["name"]
        next_enabled = bool(enabled) if enabled is not None else current["enabled"]
        next_settings = dict(current["settings"])
        if settings is not None:
            # Blank values mean "leave this field alone" -- the same
            # already-saved/leave-blank-to-keep contract every other secret
            # field in provider_configs uses, so a secret input a user
            # didn't touch on this save never gets overwritten with "".
            next_settings.update({str(k): v for k, v in dict(settings).items() if v not in (None, "")})
        next_events = current["events"] if events is None else sorted(
            {str(e).strip() for e in events if str(e).strip() in EVENT_TYPES}
        )
        next_filter = current["series_filter"] if series_filter is None else [
            str(s).strip() for s in series_filter if str(s).strip()
        ]
        con.execute(
            """update notification_connectors
               set name=?, enabled=?, settings_json=?, events_json=?, series_filter_json=?, updated_at=?
               where id=?""",
            (next_name, int(next_enabled), _dump(next_settings), _dump(next_events), _dump(next_filter), now, connector_id),
        )
        row = con.execute("select * from notification_connectors where id=?", (connector_id,)).fetchone()
        return _connector_row(row)


def delete_connector(db_path, connector_id):
    connector_id = str(connector_id or "").strip()
    if not connector_id:
        return False
    with _connection(db_path) as con:
        cur = con.execute("delete from notification_connectors where id=?", (connector_id,))
        return bool(cur.rowcount)


# --------------------------------------------------------------------------
# Pre-Phase-1 compatibility surface. A bare "discord"/"pushover" id continues
# to work exactly as before -- get-or-create against the connector table
# instead of the old notification_channels table, since that id is exactly
# what the legacy migration assigns.
# --------------------------------------------------------------------------

def _channel_prefs_row(connector):
    if not connector:
        return None
    return {
        "id": connector["id"],
        "events": connector["events"],
        "series_filter": connector["series_filter"],
        "updated_at": connector["updated_at"],
    }


def _default_channel_prefs(channel_id):
    return {"id": channel_id, "events": [], "series_filter": [], "updated_at": None}


def list_channel_prefs(db_path):
    by_id = {c["id"]: _channel_prefs_row(c) for c in list_connectors(db_path)}
    return [by_id.get(channel_id) or _default_channel_prefs(channel_id) for channel_id in CHANNEL_TYPES]


def save_channel_prefs(db_path, channel_id, *, events=None, series_filter=None):
    channel_id = str(channel_id or "").strip().lower()
    if channel_id not in CHANNEL_TYPES:
        raise ValueError(f"unknown notification channel: {channel_id}")
    if get_connector(db_path, channel_id) is None:
        connector = create_connector(
            db_path, connector_id=channel_id, type=channel_id, name=channel_id.title(),
            events=events or [], series_filter=series_filter or [],
        )
    else:
        connector = update_connector(db_path, channel_id, events=events, series_filter=series_filter)
    return _channel_prefs_row(connector)


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
        "rate_limit_max_per_hour": _stored_int(row["rate_limit_max_per_hour"], DEFAULT_RATE_LIMIT_PER_HOUR),
        "dedup_window_seconds": _stored_int(row["dedup_window_seconds"], DEFAULT_DEDUP_WINDOW_SECONDS),
        "retry_max_attempts": _stored_int(row["retry_max_attempts"], DEFAULT_RETRY_MAX_ATTEMPTS),
        "retry_backoff_seconds": _stored_int(row["retry_backoff_seconds"], DEFAULT_RETRY_BACKOFF_SECONDS),
        "history_retention_days": _stored_int(row["history_retention_days"], DEFAULT_HISTORY_RETENTION_DAYS),
        "updated_at": row["updated_at"],
    }


def get_settings(db_path):
    with _connection(db_path) as con:
        row = con.execute(
            "select * from notification_settings where id=?", (GLOBAL_SETTINGS_ID,)
        ).fetchone()
        return _settings_row(row)


def save_settings(db_path, patch):
    """Apply a validated patch to the global notification settings.

    The whole patch is validated before the connection is opened, so a patch
    carrying one bad field writes none of its fields rather than leaving the
    other nine applied and the tenth quietly wrong.
    """
    cleaned = validate_settings_patch(patch)
    now = time.time()
    with _connection(db_path) as con:
        row = con.execute(
            "select * from notification_settings where id=?", (GLOBAL_SETTINGS_ID,)
        ).fetchone()
        current = _settings_row(row)
        current.update(cleaned)
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


def list_deliveries(db_path, *, limit=100, before=None, before_id=None, event_type=None, channel_id=None, status=None):
    """Newest-first page of delivery history.

    The cursor is the composite `(created_at, id)`, not `created_at` alone.
    created_at is not unique -- every delivery a single dispatch pass writes
    carries the same stamp -- so a strict `created_at < ?` cursor against a
    `created_at`-only ORDER BY skips the remainder of any tie group that
    straddles a page boundary. It only loses rows when the boundary happens to
    land mid-tie, which is why it read as intermittent rather than as a bug.

    `before_id` is optional so an older caller still pages the old way rather
    than erroring; without it a tie at the boundary is still lossy, so any
    caller that pages must send both halves of the cursor it was handed.
    """
    limit = max(1, min(500, int(limit or 100)))
    clauses = []
    params = []
    if before is not None:
        if before_id:
            # id is the tiebreak, and it only has to be a deterministic total
            # order alongside created_at -- it is a uuid and carries no time
            # information of its own, which is fine and is why ORDER BY must
            # name it too.
            clauses.append("(created_at < ? or (created_at = ? and id < ?))")
            params.extend([before, before, before_id])
        else:
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
            f"select * from notification_deliveries {where} order by created_at desc, id desc limit ?",
            [*params, limit],
        ).fetchall()
        return [_delivery_row(row) for row in rows]


def history_windows(settings, *, retention_days=None):
    """The three horizons pruning has to respect, in seconds.

    `protect` is the one that is not negotiable: reserve_new_delivery() reads
    'sent'/'sending'/'queued' rows back inside dedup_window_seconds to decide
    whether an occurrence is a duplicate, and counts 'sent'/'sending' rows
    inside the last hour against rate_limit_max_per_hour. Nothing newer than
    that may be deleted for any reason, or the pipeline starts re-firing
    duplicates and overshooting the hourly ceiling with no error anywhere.

    `record` is history_retention_days -- what the user asked to keep. It
    governs 'sent'/'failed', their actual delivery record.

    `suppression` governs everything else ('deduped'/'filtered'/'disabled'/
    'skipped'): rows that record a *non*-send. Those are the bulk of the table
    and nothing reads them back, so they go early -- but never before the
    dedup window they explain, and never after the retention the user set.
    """
    days = settings["history_retention_days"] if retention_days is None else retention_days
    record = max(1, int(days)) * 86400
    dedup = max(0, int(settings.get("dedup_window_seconds") or 0))
    protect = max(dedup, RATE_LIMIT_WINDOW_SECONDS)
    suppression = min(record, max(DEFAULT_SUPPRESSION_RETENTION_SECONDS, dedup))
    return {"record": record, "suppression": suppression, "protect": protect}


def prune_history(db_path, *, retention_days=None, now=None, cap=NOTIFICATION_HISTORY_MAX_ROWS):
    now = time.time() if now is None else float(now)
    with _connection(db_path) as con:
        settings = _settings_row(
            con.execute(
                "select * from notification_settings where id=?", (GLOBAL_SETTINGS_ID,)
            ).fetchone()
        )
        windows = history_windows(settings, retention_days=retention_days)
        protect_after = now - windows["protect"]
        active = ",".join(f"'{status}'" for status in HISTORY_ACTIVE_STATUSES)
        record = ",".join(f"'{status}'" for status in HISTORY_RECORD_STATUSES)
        # One statement, one guard. `prunable` is the whole safety story: not
        # in the active pipeline, and older than the horizon the dispatch
        # pipeline can still read back (delivered_at as well as created_at --
        # a row created before the horizon can have been delivered inside it,
        # and dedup/rate-limit both key off coalesce(delivered_at, created_at)).
        prunable = f"""status not in ({active})
                       and created_at < :protect
                       and coalesce(delivered_at, created_at) < :protect"""
        # The second half of the safety story, and the one the ceiling below
        # used to skip: has this row outlived the window its own status is
        # kept for? 'sent'/'failed' answer to history_retention_days -- the
        # number the operator set and the Settings page promises -- and
        # everything else to the shorter suppression window.
        retention_expired = f"""(
                           (status in ({record}) and created_at < :record_cutoff)
                           or (status not in ({record}) and created_at < :suppression_cutoff)
                       )"""
        horizons = {
            "protect": protect_after,
            "record_cutoff": now - windows["record"],
            "suppression_cutoff": now - windows["suppression"],
        }
        cur = con.execute(
            f"""delete from notification_deliveries
                 where {prunable}
                   and {retention_expired}""",
            horizons,
        )
        deleted = cur.rowcount if cur.rowcount is not None else 0

        # Ceiling backstop, and it answers to BOTH guards -- this is the half
        # that was missing. It used to delete on `prunable` alone, so a table
        # over `cap` shed 'sent' rows the operator's retention still covered:
        # 50 three-day-old sends under a 30-day setting went, with nothing
        # said anywhere. That is the same "silently overriding the operator's
        # retention" failure this module already refuses to allow on write
        # paths, arriving through the ceiling instead.
        #
        # Retention wins. The ceiling may only take rows retention has already
        # released, which means that with the unbounded retention pass above it
        # can find nothing left to take and reports 0. That is deliberate, not
        # dead code: the ceiling stays a correct expression of the policy
        # rather than a second, contradictory one, so bounding the retention
        # pass later (a LIMIT for transaction size, say) cannot quietly turn
        # the ceiling back into a retention override.
        #
        # An install genuinely over `cap` therefore keeps an over-cap table and
        # says so, the same way it already does for rows the dispatch pipeline
        # still needs. See docs/inkdrop/ -- the count is reported and nothing
        # currently acts on it.
        cap = max(1, int(cap))
        over_cap = con.execute(
            f"""select count(*) as n from notification_deliveries
                 where status not in ({active})
                   and id in (
                       select id from notification_deliveries
                       where status not in ({active})
                       order by created_at desc limit -1 offset ?
                   )""",
            (cap,),
        ).fetchone()
        over_cap = int(over_cap["n"] if over_cap else 0)
        capped = 0
        if over_cap:
            cur = con.execute(
                f"""delete from notification_deliveries
                     where {prunable}
                       and {retention_expired}
                       and id in (
                           select id from notification_deliveries
                           where status not in ({active})
                           order by created_at desc limit -1 offset :cap
                       )""",
                {**horizons, "cap": cap},
            )
            capped = cur.rowcount if cur.rowcount is not None else 0
        return {
            "deleted": deleted + capped,
            "pruned_by_retention": deleted,
            "pruned_by_cap": capped,
            # Over the ceiling and kept anyway: either the dispatch pipeline
            # still reads it back, or the operator's retention still covers it.
            "over_cap_retained": max(0, over_cap - capped),
        }


# --------------------------------------------------------------------------
# Watch state (diff-based event detection: grabbed/download_failed/health/update)
# --------------------------------------------------------------------------

# How long one scanner run may hold a per-row claim before another run is
# allowed to take it over. Comfortably longer than a single notification
# send (REQUEST_TIMEOUT_SECONDS * retries) and shorter than the 180s job
# interval is not achievable at both ends, so this errs long: a stranded
# claim costs one delayed notification, a too-short lease costs a duplicate.
WATCH_CLAIM_LEASE_SECONDS = 300


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


def _claim_field(flag):
    return f"claim_until:{flag}"


def _coerce_ts(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def claim_watch_flag(db_path, key, flag, *, lease_seconds=WATCH_CLAIM_LEASE_SECONDS, now=None):
    """Take a short lease on one flag of a watch-state entry.

    Returns "done" if the flag is already set, "busy" if another run holds an
    unexpired lease on it, or "claimed" for the single caller that just took
    the lease. begin immediate is what makes that safe: the write lock is
    taken before the read, so two overlapping notification-dispatch jobs
    serialize and the second one sees the first one's lease.

    The lease exists because taking the flag and earning the right to set it
    are two different things -- a caller that claims and then dies mid-send
    must not strand the row forever, so the claim expires and the row becomes
    claimable again on a later pass.
    """
    now = float(now) if now is not None else time.time()
    field = _claim_field(flag)
    with _connection(db_path) as con:
        con.execute("begin immediate")
        row = con.execute("select value_json from notification_watch_state where key=?", (key,)).fetchone()
        current = _json(row["value_json"], {}) if row else {}
        if current.get(flag):
            return "done"
        if _coerce_ts(current.get(field)) > now:
            return "busy"
        current[field] = now + max(1.0, float(lease_seconds or 0))
        con.execute(
            """insert into notification_watch_state(key, value_json, updated_at) values(?,?,?)
               on conflict(key) do update set value_json=excluded.value_json, updated_at=excluded.updated_at""",
            (key, _dump(current), now),
        )
        return "claimed"


def finish_watch_flag(db_path, key, flag, *, extra=None):
    """Set the flag and drop its lease in one transaction -- the durable
    acknowledgement that this row's outcome was recorded and it never needs
    looking at again."""
    return _update_watch_claim(db_path, key, flag, done=True, extra=extra)


def release_watch_flag(db_path, key, flag):
    """Drop the lease without setting the flag, leaving the row claimable
    again on the next pass. This is what keeps a delivery that failed to
    record from being silently acknowledged."""
    return _update_watch_claim(db_path, key, flag, done=False, extra=None)


def _update_watch_claim(db_path, key, flag, *, done, extra):
    now = time.time()
    field = _claim_field(flag)
    with _connection(db_path) as con:
        con.execute("begin immediate")
        row = con.execute("select value_json from notification_watch_state where key=?", (key,)).fetchone()
        current = _json(row["value_json"], {}) if row else {}
        current.pop(field, None)
        if done:
            current[flag] = True
        if extra:
            current.update(extra)
        con.execute(
            """insert into notification_watch_state(key, value_json, updated_at) values(?,?,?)
               on conflict(key) do update set value_json=excluded.value_json, updated_at=excluded.updated_at""",
            (key, _dump(current), now),
        )
        return dict(current)


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
