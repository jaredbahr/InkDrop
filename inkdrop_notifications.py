"""Outbound notification providers for InkDrop (Discord, Pushover, ...).

Config is read from the `notifications` row in `provider_configs` (same table
slskd/qBittorrent/SABnzbd use), so the settings UI can wire up webhook URLs
and API keys without any changes here. Until that row exists, each provider
falls back to environment variables so this can be tested standalone:

    INKDROP_DISCORD_WEBHOOK_URL
    INKDROP_PUSHOVER_API_TOKEN
    INKDROP_PUSHOVER_USER_KEY

Adding a third provider means one new NotificationProvider subclass and an
entry in PROVIDER_CLASSES -- nothing else in this file changes.
"""

from __future__ import annotations

import logging
import os

import requests

import inkdrop_manual_search

logger = logging.getLogger("inkdrop.notifications")

PROVIDER_CONFIG_ID = "notifications"
REQUEST_TIMEOUT_SECONDS = 10


class NotificationProvider:
    """One outbound channel. Subclasses read their own settings keys."""

    name = "base"

    def __init__(self, settings):
        self.settings = settings or {}

    def is_configured(self):
        raise NotImplementedError

    def send(self, title, message, event_type):
        """Send one notification. Must not raise -- return True/False."""
        raise NotImplementedError


class DiscordWebhookProvider(NotificationProvider):
    name = "discord"

    def _webhook_url(self):
        return self.settings.get("discord_webhook_url") or os.environ.get(
            "INKDROP_DISCORD_WEBHOOK_URL", ""
        )

    def is_configured(self):
        return bool(self._webhook_url())

    def send(self, title, message, event_type):
        url = self._webhook_url()
        if not url:
            return False
        payload = {"content": f"**{title}**\n{message}"}
        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            logger.warning("discord notify error: %s", exc)
            return False
        if response.status_code >= 400:
            logger.warning(
                "discord notify failed (%s): %s", response.status_code, response.text[:300]
            )
            return False
        return True


class PushoverProvider(NotificationProvider):
    name = "pushover"
    API_URL = "https://api.pushover.net/1/messages.json"

    def _token(self):
        return self.settings.get("pushover_api_token") or os.environ.get(
            "INKDROP_PUSHOVER_API_TOKEN", ""
        )

    def _user_key(self):
        return self.settings.get("pushover_user_key") or os.environ.get(
            "INKDROP_PUSHOVER_USER_KEY", ""
        )

    def is_configured(self):
        return bool(self._token() and self._user_key())

    def send(self, title, message, event_type):
        token = self._token()
        user_key = self._user_key()
        if not token or not user_key:
            return False
        payload = {"token": token, "user": user_key, "title": title, "message": message}
        try:
            response = requests.post(self.API_URL, data=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            logger.warning("pushover notify error: %s", exc)
            return False
        if response.status_code >= 400:
            logger.warning(
                "pushover notify failed (%s): %s", response.status_code, response.text[:300]
            )
            return False
        return True


PROVIDER_CLASSES = {
    "discord": DiscordWebhookProvider,
    "pushover": PushoverProvider,
}


def _provider_settings(db_path):
    """Read the `notifications` provider_configs row. Missing row = disabled."""
    if db_path is None:
        return {}, True
    try:
        import inkdrop_state
    except ImportError:
        return {}, True
    try:
        config = inkdrop_state.provider_config(db_path, PROVIDER_CONFIG_ID)
    except Exception:
        logger.exception("failed to load notifications provider config")
        return {}, True
    if not config:
        return {}, True
    return config.get("settings") or {}, bool(config.get("enabled", True))


def _active_providers(db_path):
    settings, provider_row_enabled = _provider_settings(db_path)
    if not provider_row_enabled:
        return []
    active = []
    for key, cls in PROVIDER_CLASSES.items():
        if settings.get(f"{key}_enabled", True) is False:
            continue
        instance = cls(settings)
        if instance.is_configured():
            active.append(instance)
    return active


def notify(db_path, event_type, title, message):
    """Send to every configured+enabled provider. Never raises."""
    sent = []
    for provider in _active_providers(db_path):
        try:
            if provider.send(title, message, event_type):
                sent.append(provider.name)
        except Exception:
            logger.exception("notification provider %s raised", provider.name)
    return sent


def test_all(db_path):
    """Send a real test message to every configured channel, ignoring the enable
    toggles -- someone testing a webhook or token they just typed in shouldn't have
    to save and flip a switch first to find out if it even works. Returns one result
    per provider class, so a "Test" click can report exactly which channel worked and
    which didn't, instead of one pass/fail for everything.
    """
    settings, _provider_row_enabled = _provider_settings(db_path)
    results = []
    for key, cls in PROVIDER_CLASSES.items():
        instance = cls(settings)
        if not instance.is_configured():
            results.append({"name": instance.name, "configured": False, "sent": False, "detail": "not configured"})
            continue
        try:
            ok = instance.send("InkDrop: test notification", "This is a test notification from InkDrop.", "test")
        except Exception as exc:
            # send() itself never raises for the providers above, but stay defensive --
            # exception text can embed the webhook URL/token itself, so it must be
            # redacted before it can reach test history or a toast.
            results.append({
                "name": instance.name,
                "configured": True,
                "sent": False,
                "detail": inkdrop_manual_search.redacted_text(exc, 200),
            })
            continue
        results.append({
            "name": instance.name,
            "configured": True,
            "sent": bool(ok),
            "detail": "sent" if ok else "send failed; check the webhook/token and server logs",
        })
    return results


def notify_wanted_cleared(
    db_path, *, series, issue_title=None, issue_number=None, dest_path=None, source_path=None
):
    """A Wanted item was cleared by a verified import."""
    subject = issue_title or (f"#{issue_number}" if issue_number else "")
    headline = " ".join(part for part in (series, subject) if part) or "An item"
    lines = [f"{headline} has been verified and imported."]
    if dest_path:
        lines.append(f"Location: {dest_path}")
    return notify(db_path, "wanted_cleared", "InkDrop: Wanted item imported", "\n".join(lines))


def notify_manual_review(db_path, *, reason, series=None, source=None, detail=None, note=None):
    """An item landed in Manual Review and needs human attention."""
    lines = []
    if series:
        lines.append(f"Series: {series}")
    lines.append(f"Reason: {reason}")
    if note:
        lines.append(note)
    elif detail:
        lines.append(str(detail))
    if source:
        lines.append(f"Source: {source}")
    return notify(db_path, "manual_review", "InkDrop: Manual review needed", "\n".join(lines))
