"""Outbound notification providers and dispatch pipeline for InkDrop.

Channel config (Discord, Pushover, ...), per-channel event-trigger toggles,
per-channel series filters, quiet hours, dedup, rate limiting, retries, and
delivery history all live in inkdrop_notification_store.py. This module
turns "something happened" into an actual send decision and, if the decision
is yes, an actual HTTP call -- nothing here talks to SQLite directly except
through that store.

Adding a third provider means one new NotificationProvider subclass and an
entry in PROVIDER_CLASSES -- nothing else in this file changes.

Outbound message bodies are built from user-relevant fields only (series
title, issue label, what happened, what to do next). Never put a filesystem
path, API key, webhook URL, or database id in a subject/message string --
those are exactly what quiet-hours/history/toast surfaces would otherwise
leak. Failure detail shown to the user is bounded to an error category and,
where available, an HTTP status code -- never raw exception text.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import time
from collections import namedtuple

import requests

from core import inkdrop_manual_search
from core import inkdrop_notification_store as store

logger = logging.getLogger("inkdrop.notifications")

PROVIDER_CONFIG_ID = "notifications"
REQUEST_TIMEOUT_SECONDS = 10

SendResult = namedtuple("SendResult", "ok detail retryable retry_after", defaults=[None])


def _is_retryable_exception(exc):
    """Timeouts/connection errors are worth retrying; everything else isn't.
    Built lazily (not a module-level tuple) so importing this module never
    breaks in a context that stubs out `requests` without its exception
    classes -- several smoke tests do exactly that to run offline."""
    categories = tuple(
        cls for cls in (
            getattr(requests, "Timeout", None),
            getattr(requests, "ConnectionError", None),
            TimeoutError,
            ConnectionError,
        ) if isinstance(cls, type)
    )
    return isinstance(exc, categories)


def _failure_type(exc):
    """Bounded error category without rendering exception text (which can
    embed a webhook URL or token echoed back by the provider)."""
    categories = [
        cls for cls in (
            getattr(requests, "Timeout", None),
            getattr(requests, "ConnectionError", None),
            getattr(requests, "HTTPError", None),
            getattr(requests, "RequestException", None),
            TimeoutError,
            ConnectionError,
            RuntimeError,
            ValueError,
            TypeError,
            OSError,
        ) if isinstance(cls, type)
    ]
    for category in categories:
        if isinstance(exc, category):
            return category.__name__
    return "Exception"


def _response_status(response):
    try:
        return int(response.status_code)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _retry_after_seconds(response):
    """Return bounded provider retry guidance without logging response data."""
    try:
        raw = response.headers.get("Retry-After")
        seconds = float(raw)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return max(30, min(3600, int(seconds)))


def _pushover_quota_reset_seconds(response):
    """Pushover's 429 guidance is an absolute monthly quota-reset epoch."""
    try:
        reset_at = float(response.headers.get("X-Limit-App-Reset"))
        seconds = math.ceil(reset_at - time.time())
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return max(30, min(2678400, seconds))


class NotificationProvider:
    """One connector type. `settings` is one connector *instance*'s own
    dict -- unprefixed keys, since the instance itself (not a field-name
    prefix) is what scopes them now. Subclasses read their own settings keys
    and declare `config_fields` so the add/edit-connector UI can be built
    generically instead of one hardcoded form per type."""

    name = "base"
    display_name = "Base"
    # Tuple of {"key", "label", "secret": bool, "required": bool, "help": str}.
    # `key` is the exact settings dict key this field reads/writes.
    config_fields = ()

    def __init__(self, settings):
        self.settings = settings or {}

    def is_configured(self):
        raise NotImplementedError

    def send(self, title, message, event_type):
        """Send one notification. Must not raise -- returns a SendResult."""
        raise NotImplementedError


class DiscordWebhookProvider(NotificationProvider):
    name = "discord"
    display_name = "Discord"
    config_fields = (
        {
            "key": "webhook_url",
            "label": "Webhook URL",
            "secret": True,
            "required": True,
            "help": "Discord channel -> Edit Channel -> Integrations -> Webhooks.",
        },
    )

    def _webhook_url(self):
        return self.settings.get("webhook_url") or os.environ.get(
            "INKDROP_DISCORD_WEBHOOK_URL", ""
        )

    def is_configured(self):
        return bool(self._webhook_url())

    def send(self, title, message, event_type):
        url = self._webhook_url()
        if not url:
            return SendResult(False, "not configured", False)
        payload = {"content": f"**{title}**\n{message}"}
        try:
            # wait=true is load-bearing, not cosmetic: Discord's webhook execute
            # endpoint is fire-and-forget by default -- it returns 204 the
            # moment the request is accepted, before the message is actually
            # posted to the channel, with no way to tell success from a send
            # that silently never lands. wait=true makes Discord hold the
            # response until it has actually created the message, returning
            # the message object (with a real id) on success instead of an
            # empty 204. Without this, a "sent" status here only ever meant
            # "Discord accepted the HTTP request," never "the message exists
            # in the channel" -- confirmed live: delivery rows marked "sent"
            # with no corresponding message ever appearing in the target
            # Discord channel.
            response = requests.post(
                url, json=payload, params={"wait": "true"}, timeout=REQUEST_TIMEOUT_SECONDS
            )
            status_code = _response_status(response)
        except Exception as exc:
            retryable = _is_retryable_exception(exc)
            logger.warning("discord notify error (%s)", _failure_type(exc))
            return SendResult(False, f"send failed ({_failure_type(exc)})", retryable)
        if status_code is None:
            return SendResult(False, "send failed (invalid response)", True)
        if status_code >= 400:
            logger.warning("discord notify failed (HTTP %d)", status_code)
            retryable = status_code >= 500 or status_code == 429
            return SendResult(
                False,
                f"send failed (HTTP {status_code})",
                retryable,
                _retry_after_seconds(response) if status_code == 429 else None,
            )
        try:
            message_id = (response.json() or {}).get("id")
        except (AttributeError, ValueError):
            message_id = None
        if not message_id:
            # A 2xx with no message id back means Discord accepted the
            # request but did not confirm the message was created -- treat
            # that the same as a failure rather than a silent false "sent".
            logger.warning("discord notify: no message id in wait=true response")
            return SendResult(False, "send failed (no delivery confirmation)", True)
        return SendResult(True, "sent", False)


class PushoverProvider(NotificationProvider):
    name = "pushover"
    display_name = "Pushover"
    API_URL = "https://api.pushover.net/1/messages.json"
    config_fields = (
        {"key": "api_token", "label": "API Token", "secret": True, "required": True, "help": "Pushover application/API token."},
        {"key": "user_key", "label": "User Key", "secret": True, "required": True, "help": "Pushover user or group key."},
    )

    def _token(self):
        return self.settings.get("api_token") or os.environ.get(
            "INKDROP_PUSHOVER_API_TOKEN", ""
        )

    def _user_key(self):
        return self.settings.get("user_key") or os.environ.get(
            "INKDROP_PUSHOVER_USER_KEY", ""
        )

    def is_configured(self):
        return bool(self._token() and self._user_key())

    def send(self, title, message, event_type):
        token = self._token()
        user_key = self._user_key()
        if not token or not user_key:
            return SendResult(False, "not configured", False)
        payload = {"token": token, "user": user_key, "title": title, "message": message}
        try:
            response = requests.post(self.API_URL, data=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            status_code = _response_status(response)
        except Exception as exc:
            retryable = _is_retryable_exception(exc)
            logger.warning("pushover notify error (%s)", _failure_type(exc))
            return SendResult(False, f"send failed ({_failure_type(exc)})", retryable)
        if status_code is None:
            return SendResult(False, "send failed (invalid response)", True)
        if status_code >= 400:
            logger.warning("pushover notify failed (HTTP %d)", status_code)
            retryable = status_code >= 500 or status_code == 429
            return SendResult(
                False,
                f"send failed (HTTP {status_code})",
                retryable,
                _pushover_quota_reset_seconds(response) if status_code == 429 else None,
            )
        try:
            accepted = int((response.json() or {}).get("status")) == 1
        except (AttributeError, TypeError, ValueError):
            accepted = False
        if not accepted:
            return SendResult(False, "send failed (provider rejected request)", False)
        return SendResult(True, "sent", False)


PROVIDER_CLASSES = {
    "discord": DiscordWebhookProvider,
    "pushover": PushoverProvider,
}

EVENT_TYPES = store.EVENT_TYPES

EVENT_LABELS = {
    "grabbed": "Grabbed",
    "download_failed": "Download failed",
    "import_verified": "Import verified",
    "manual_action_required": "Manual action required",
    "health_issue": "Health issue",
    "health_restored": "Health restored",
    "application_update": "Application update",
}


def _provider_for(type_id, resolved_settings):
    cls = PROVIDER_CLASSES.get(type_id)
    if cls is None:
        return None
    return cls(resolved_settings)


# --------------------------------------------------------------------------
# Connector config -- Phase 1 of the Sonarr-style Connect redesign: N
# configured instances of M connector types (today, M=2: discord/pushover)
# instead of one fixed row per type. Each instance owns its own settings
# (secrets included -- plaintext, same as every other credential in
# provider_configs; no encryption layer, self-hosted has nowhere secure to
# keep a key, and Sonarr/Radarr don't encrypt these either) plus its own
# event-trigger toggles and series filter, all in inkdrop_notification_store's
# notification_connectors table. A separate, shared provider_configs
# row (id="notifications") still carries exactly one thing: a master on/off
# switch for the whole notifications subsystem -- turning it off silences
# every connector at once without touching any of their individual toggles,
# the same master-switch semantics the pre-Phase-1 shared row already had.
# --------------------------------------------------------------------------

def _provider_config_row(db_path):
    if db_path is None:
        return None
    try:
        from core import inkdrop_state
        return inkdrop_state.provider_config(db_path, PROVIDER_CONFIG_ID)
    except Exception:
        logger.exception("failed to load notifications provider config")
        return None


def connector_types_catalog():
    """Every connector type the Add Connector UI can offer, with its
    config_fields schema -- adding a type later means one more entry here
    plus one NotificationProvider subclass; the UI needs no new code."""
    return [
        {"type": cls.name, "display_name": cls.display_name, "config_fields": list(cls.config_fields)}
        for cls in PROVIDER_CLASSES.values()
    ]


def public_channel_status(db_path):
    """Masked, frontend-safe view of every configured connector: whether
    each secret field is configured (never the value itself), effective
    enabled state, and its event-trigger/series-filter preferences."""
    master_enabled = bool((_provider_config_row(db_path) or {}).get("enabled", True))
    result = []
    for connector in store.list_connectors(db_path):
        cls = PROVIDER_CLASSES.get(connector["type"])
        fields = cls.config_fields if cls else ()
        settings = connector["settings"]
        secret_fields = {
            field["key"]: {"configured": bool(settings.get(field["key"]))}
            for field in fields
            if field.get("secret")
        }
        result.append({
            "id": connector["id"],
            "type": connector["type"],
            "name": connector["name"],
            "display_name": cls.display_name if cls else connector["type"].title(),
            "enabled": master_enabled and connector["enabled"],
            "secret_fields": secret_fields,
            "events": connector["events"],
            "series_filter": connector["series_filter"],
        })
    return result


def _load_connectors(db_path, *, ignore_enabled=False):
    """Build the per-connector view the dispatch pipeline works with:
    effective enabled state (master switch AND the connector's own toggle,
    unless a caller like test_all explicitly wants to ignore both) merged
    with that connector's own type/settings/events/series-filter."""
    master_enabled = bool((_provider_config_row(db_path) or {}).get("enabled", True))
    connectors = []
    for row in store.list_connectors(db_path):
        connectors.append({
            "id": row["id"],
            "type": row["type"],
            "enabled": ignore_enabled or (master_enabled and row["enabled"]),
            "settings": row["settings"],
            "events": row["events"],
            "series_filter": row["series_filter"],
        })
    return connectors


def _occurrence_key(event_type, *parts):
    raw = "|".join([event_type, *(str(p) for p in parts if p is not None)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _in_quiet_hours(settings, now=None):
    if not settings.get("quiet_hours_enabled"):
        return False
    now = time.localtime(now) if now is not None else time.localtime()
    days = settings.get("quiet_hours_days") or []
    if days:
        weekday = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[now.tm_wday]
        if weekday not in days:
            return False
    start = _parse_hhmm(settings.get("quiet_hours_start"))
    end = _parse_hhmm(settings.get("quiet_hours_end"))
    if start is None or end is None:
        return False
    minutes_now = now.tm_hour * 60 + now.tm_min
    if start == end:
        return False
    if start < end:
        return start <= minutes_now < end
    return minutes_now >= start or minutes_now < end


def _parse_hhmm(value):
    try:
        hours, minutes = str(value or "").strip().split(":", 1)
        hours, minutes = int(hours), int(minutes)
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return hours * 60 + minutes
    except (TypeError, ValueError):
        pass
    return None


def _redact(value, limit=240):
    return inkdrop_manual_search.redacted_text(value, limit)


def _quiet_hours_release_at(settings, now):
    end_minutes = _parse_hhmm(settings.get("quiet_hours_end")) or 0
    now_struct = time.localtime(now)
    now_minutes = now_struct.tm_hour * 60 + now_struct.tm_min
    delay_minutes = (end_minutes - now_minutes) % (24 * 60)
    return float(now) + max(1, delay_minutes * 60)


def _dispatch_to_channel(db_path, channel, settings, *, event_type, subject, message, series_id, issue_id, occurrence_key, urgent):
    """Apply filter -> dedup -> quiet-hours -> rate-limit -> send, recording
    exactly one delivery-history row describing what happened. Returns that
    row, or None if the channel isn't even subscribed to this event (no
    history row is written for a plain "not toggled on" no-op)."""
    channel_id = channel["id"]

    if not channel["enabled"]:
        return None

    if event_type not in channel["events"]:
        return None

    max_attempts = settings.get("retry_max_attempts", store.DEFAULT_RETRY_MAX_ATTEMPTS)

    series_filter = channel.get("series_filter") or []
    if series_filter and series_id and series_id not in series_filter:
        return store.record_delivery(
            db_path, event_type=event_type, channel_id=channel_id, occurrence_key=occurrence_key,
            series_id=series_id, issue_id=issue_id, subject=subject, message=message,
            status="filtered", attempt=1, max_attempts=1,
            error_detail="series is not in this channel's filter",
        )

    dedup_window = settings.get("dedup_window_seconds", store.DEFAULT_DEDUP_WINDOW_SECONDS)
    urgent_events = set(settings.get("quiet_hours_urgent_events") or store.DEFAULT_URGENT_EVENTS)
    bypasses_quiet_hours = urgent or event_type in urgent_events
    defer_reason = None
    next_attempt_at = None
    if not bypasses_quiet_hours and _in_quiet_hours(settings):
        defer_reason = "quiet_hours"
        next_attempt_at = _quiet_hours_release_at(settings, time.time())

    max_per_hour = settings.get("rate_limit_max_per_hour", store.DEFAULT_RATE_LIMIT_PER_HOUR)
    reservation = store.reserve_new_delivery(
        db_path,
        event_type=event_type,
        channel_id=channel_id,
        occurrence_key=occurrence_key,
        series_id=series_id,
        issue_id=issue_id,
        subject=subject,
        message=message,
        max_attempts=max_attempts,
        dedup_window_seconds=dedup_window,
        max_per_hour=max_per_hour,
        defer_reason=defer_reason,
        next_attempt_at=next_attempt_at,
        lease_seconds=REQUEST_TIMEOUT_SECONDS * 3 + 15,
    )
    if reservation["status"] != "sending":
        return reservation
    return _attempt_send(
        db_path, channel, settings, event_type=event_type, subject=subject, message=message,
        series_id=series_id, issue_id=issue_id, occurrence_key=occurrence_key,
        attempt=1, existing_delivery_id=reservation["id"],
        reservation_deadline=reservation["next_attempt_at"],
    )


def _attempt_send(
    db_path,
    channel,
    settings,
    *,
    event_type,
    subject,
    message,
    series_id,
    issue_id,
    occurrence_key,
    attempt,
    existing_delivery_id=None,
    reservation_deadline=None,
):
    """Make one send attempt and record/update its outcome. When
    existing_delivery_id is set (a retry), the same history row is updated
    in place instead of spawning a new one, so one event -> one channel
    stays one row across its whole retry lifecycle."""
    channel_id = channel["id"]
    provider = _provider_for(channel["type"], channel["settings"])
    max_attempts = settings.get("retry_max_attempts", store.DEFAULT_RETRY_MAX_ATTEMPTS)

    def _finish(status, *, queue_reason=None, error_detail=None, next_attempt_at=None):
        if existing_delivery_id:
            return store.update_delivery(
                db_path, existing_delivery_id, status=status, error_detail=error_detail,
                next_attempt_at=next_attempt_at, attempt=attempt, queue_reason=queue_reason,
                expected_lease_until=reservation_deadline,
            )
        return store.record_delivery(
            db_path, event_type=event_type, channel_id=channel_id, occurrence_key=occurrence_key,
            series_id=series_id, issue_id=issue_id, subject=subject, message=message,
            status=status, queue_reason=queue_reason, attempt=attempt, max_attempts=max_attempts,
            error_detail=error_detail, next_attempt_at=next_attempt_at,
        )

    if provider is None or not provider.is_configured():
        return _finish("disabled", error_detail="channel is not configured")
    try:
        result = provider.send(subject, message, event_type)
    except Exception as exc:
        result = SendResult(False, f"send failed ({_failure_type(exc)})", True)
    if result.ok:
        return _finish("sent")
    if result.retryable and attempt < max(1, max_attempts):
        backoff = settings.get("retry_backoff_seconds", store.DEFAULT_RETRY_BACKOFF_SECONDS)
        delay = result.retry_after or min(3600, backoff * (2 ** (attempt - 1)))
        return _finish("queued", queue_reason="retry", error_detail=_redact(result.detail), next_attempt_at=time.time() + delay)
    return _finish("failed", error_detail=_redact(result.detail))


def notify(
    db_path,
    event_type,
    *,
    subject,
    message,
    series_id=None,
    issue_id=None,
    occurrence_key=None,
    urgent=False,
):
    """Route one event through every enabled+subscribed channel. Never
    raises -- each channel's outcome is recorded to delivery history
    regardless of what happens. Returns the list of channel ids the message
    was actually sent to (immediately; queued/retried sends aren't counted
    here since the caller can't know that outcome synchronously)."""
    if db_path is None or event_type not in EVENT_TYPES:
        return []
    occurrence_key = occurrence_key or _occurrence_key(event_type, series_id, issue_id, subject)
    try:
        settings = store.get_settings(db_path)
        channels = _load_connectors(db_path)
    except Exception:
        logger.exception("failed to load notification settings/channels")
        return []
    sent = []
    for channel in channels:
        try:
            delivery = _dispatch_to_channel(
                db_path, channel, settings, event_type=event_type, subject=subject, message=message,
                series_id=series_id, issue_id=issue_id, occurrence_key=occurrence_key, urgent=urgent,
            )
            if delivery and delivery.get("status") == "sent":
                sent.append(channel["id"])
        except Exception:
            logger.exception("notification dispatch to channel %s raised", channel["id"])
    return sent


def process_due_retries(db_path, *, now=None, limit=100):
    """Re-attempt everything queued for quiet-hours release, rate-limit
    backoff, or transient-failure retry whose next_attempt_at has passed."""
    fixed_now = float(now) if now is not None else None
    settings = store.get_settings(db_path)
    channels = {c["id"]: c for c in _load_connectors(db_path)}
    attempted = 0
    processed = 0
    process_limit = max(1, int(limit or 100))
    max_per_hour = settings.get("rate_limit_max_per_hour", store.DEFAULT_RATE_LIMIT_PER_HOUR)
    while processed < process_limit:
        claim_now = fixed_now if fixed_now is not None else time.time()
        delivery = store.claim_next_due_delivery(
            db_path,
            now=claim_now,
            max_per_hour=max_per_hour,
            lease_seconds=REQUEST_TIMEOUT_SECONDS * 3 + 15,
        )
        if not delivery:
            break
        processed += 1
        channel = channels.get(delivery["channel_id"])
        if channel is None or not channel["enabled"] or delivery["event_type"] not in channel["events"]:
            store.update_delivery(db_path, delivery["id"], status="disabled", error_detail="channel disabled or event untoggled before retry")
            continue
        urgent_events = set(settings.get("quiet_hours_urgent_events") or store.DEFAULT_URGENT_EVENTS)
        if delivery["queue_reason"] == "quiet_hours" and delivery["event_type"] not in urgent_events and _in_quiet_hours(settings, claim_now):
            store.update_delivery(
                db_path,
                delivery["id"],
                status="queued",
                queue_reason="quiet_hours",
                next_attempt_at=_quiet_hours_release_at(settings, claim_now),
                attempt=delivery["attempt"],
            )
            continue
        attempted += 1
        next_attempt = max(1, int(delivery["attempt"]) + 1) if delivery["queue_reason"] == "retry" else 1
        _attempt_send(
            db_path, channel, settings, event_type=delivery["event_type"], subject=delivery["subject"],
            message=delivery["message"], series_id=delivery["series_id"], issue_id=delivery["issue_id"],
            occurrence_key=delivery["occurrence_key"], attempt=next_attempt,
            existing_delivery_id=delivery["id"],
            reservation_deadline=delivery["next_attempt_at"],
        )
    return {"attempted": attempted}


def _test_connector_row(connector):
    """Send one real test message to one connector row (a
    inkdrop_notification_store connector dict, not the dispatch-shaped view
    _load_connectors returns) and report the outcome. Shared by test_all
    (every connector) and test_connector (exactly one)."""
    cls = PROVIDER_CLASSES.get(connector["type"])
    name = connector.get("name") or connector["type"]
    if cls is None:
        return {"id": connector["id"], "name": name, "configured": False, "sent": False, "detail": "unknown connector type"}
    instance = cls(connector["settings"])
    if not instance.is_configured():
        return {"id": connector["id"], "name": name, "configured": False, "sent": False, "detail": "not configured"}
    try:
        result = instance.send("InkDrop: test notification", "This is a test notification from InkDrop.", "test")
    except Exception as exc:
        result = SendResult(False, f"send failed ({_failure_type(exc)})", True)
    return {
        "id": connector["id"],
        "name": name,
        "configured": True,
        "sent": bool(result.ok),
        "detail": _redact(result.detail) if result.detail else ("sent" if result.ok else "send failed"),
    }


def test_all(db_path):
    """Send a real test message to every configured connector, ignoring the
    enable toggle and event-trigger matrix on each -- someone testing a
    webhook or token they just typed in shouldn't have to save and flip
    every switch first to find out if it even works. Returns one result per
    connector (not per type: two Discord connectors get two results)."""
    try:
        connectors = store.list_connectors(db_path)
    except Exception:
        logger.exception("failed to load notification connectors for test")
        connectors = []
    return [_test_connector_row(connector) for connector in connectors]


def test_connector(db_path, connector_id):
    """Send a real test message to exactly one connector, ignoring its
    enable toggle and event-trigger matrix, the same "test before you have
    to save and flip switches" reasoning as test_all. Returns a single
    result dict, or {"ok": False, "reason": ...} if the id doesn't exist."""
    connector = store.get_connector(db_path, connector_id)
    if connector is None:
        return {"ok": False, "reason": "unknown_connector", "id": connector_id}
    return {"ok": True, **_test_connector_row(connector)}


# --------------------------------------------------------------------------
# Typed event wrappers -- keep call sites simple and keep message bodies
# limited to user-relevant fields (no paths, ids, or secrets).
# --------------------------------------------------------------------------

def notify_wanted_cleared(
    db_path, *, series, issue_title=None, issue_number=None, dest_path=None, source_path=None, series_id=None, issue_id=None
):
    """A Wanted item was cleared by a verified import.

    dest_path/source_path are accepted for call-site compatibility but never
    put in the message -- a live notification once shipped with the full
    container filesystem path in plaintext via a "Location: ..." line.

    Deduped through the standard dispatch pipeline (occurrence_key + each
    channel's dedup_window_seconds, default 24h): a queue item can get
    re-confirmed as "verified" many times over for the same already-imported
    file (reconciliation re-checks, sibling-issue proof re-matches) without
    any new download ever happening -- traced against a real production
    case where a single issue re-fired this notification 32 times in one
    day, 23,155 times across the library in a week. The occurrence key is
    built from series/issue identity only, deliberately excluding dest_path,
    which was observed to change between re-confirmations from library
    reorganization alone even when nothing new was downloaded.
    """
    subject = issue_title or (f"#{issue_number}" if issue_number else "")
    headline = " ".join(part for part in (series, subject) if part) or "An item"
    message = f"{headline} has been verified and imported."
    return notify(
        db_path, "import_verified", subject="InkDrop: Wanted item imported", message=message,
        series_id=series_id, issue_id=issue_id,
        occurrence_key=_occurrence_key("import_verified", series_id or series, issue_id or issue_title or issue_number),
    )


def notify_manual_review(db_path, *, reason, series=None, source=None, detail=None, note=None, series_id=None, issue_id=None):
    """An item landed in Manual Review and needs human attention.

    `source` and `detail` come from the import pipeline's own diagnostics and
    routinely carry a raw filesystem path (e.g. the file that triggered the
    review) -- never put them in the message verbatim. `note` is a
    hand-written human explanation from the same call sites but is redacted
    too as defense in depth, since nothing enforces that upstream.
    """
    lines = []
    if series:
        lines.append(f"Series: {series}")
    lines.append(f"Reason: {reason}")
    if note:
        lines.append(_redact(note, 200))
    elif detail:
        lines.append(_redact(detail, 200))
    return notify(
        db_path, "manual_action_required", subject="InkDrop: Manual review needed", message="\n".join(lines),
        series_id=series_id, issue_id=issue_id,
        occurrence_key=_occurrence_key("manual_action_required", series_id or series, reason, source),
    )


def notify_grabbed(db_path, *, series, issue_label=None, series_id=None, issue_id=None, task_id=None):
    headline = " ".join(part for part in (series, issue_label) if part) or "An item"
    message = f"{headline} was found and grabbed. InkDrop is downloading it now."
    return notify(
        db_path, "grabbed", subject="InkDrop: Grabbed", message=message,
        series_id=series_id, issue_id=issue_id,
        occurrence_key=_occurrence_key("grabbed", task_id or series_id or series, issue_id or issue_label),
    )


def notify_download_failed(
    db_path, *, series, issue_label=None, reason=None, series_id=None, issue_id=None, task_id=None, terminal=True
):
    """One task reached a failed state. `terminal` must be set by the caller
    from the canonical queue/Wanted unit, not from the task alone -- a task
    can fail (including a post-download match/import rejection of a file
    that actually finished transferring) while the queue itself still has an
    active task, a scheduled retry, a remaining automatic source, or a
    completed file/import that already owns it. Only when none of those
    apply is the failure actually terminal at the item level; otherwise this
    is scoped to the one attempt so it never claims InkDrop has given up on
    a unit it is still working on."""
    headline = " ".join(part for part in (series, issue_label) if part) or "An item"
    if terminal:
        lines = [f"{headline} failed to download and will not be retried automatically."]
    else:
        lines = [f"This download attempt for {headline} did not complete. InkDrop will continue with the remaining automatic sources."]
    if reason:
        lines.append(_redact(reason, 200))
    return notify(
        db_path, "download_failed", subject="InkDrop: Download failed" if terminal else "InkDrop: Download attempt failed",
        message="\n".join(lines),
        series_id=series_id, issue_id=issue_id,
        occurrence_key=_occurrence_key("download_failed", task_id or series_id or series, issue_id or issue_label),
    )


def notify_health_issue(db_path, *, provider_label, detail=None):
    lines = [f"{provider_label} is having trouble right now."]
    if detail:
        lines.append(_redact(detail, 200))
    return notify(
        db_path, "health_issue", subject=f"InkDrop: {provider_label} health issue", message="\n".join(lines),
        occurrence_key=_occurrence_key("health_issue", provider_label), urgent=True,
    )


def notify_health_restored(db_path, *, provider_label):
    message = f"{provider_label} is healthy again."
    return notify(
        db_path, "health_restored", subject=f"InkDrop: {provider_label} restored", message=message,
        occurrence_key=_occurrence_key("health_restored", provider_label),
    )


def notify_application_update(db_path, *, state, headline, detail=None):
    lines = [headline]
    if detail:
        lines.append(_redact(detail, 300))
    return notify(
        db_path, "application_update", subject="InkDrop: Update available", message="\n".join(lines),
        occurrence_key=_occurrence_key("application_update", state),
    )
