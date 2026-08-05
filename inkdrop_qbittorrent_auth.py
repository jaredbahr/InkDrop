#!/usr/bin/env python3
"""One place where InkDrop authenticates to qBittorrent.

Every caller that talks to the qBittorrent Web API goes through `authenticate`.
Before this module there were eight hand-rolled copies of the login POST, so a
fix in one of them (the settings "Test" button, say) left the other seven --
the ones that actually download things -- still broken.

Two auth methods, and they are mutually exclusive per request:

* API key (qBittorrent >= 5.2.0, Web API >= 2.14.1). Sent as
  `Authorization: Bearer qbt_...`. qBittorrent's own docs are explicit that a
  key "cannot interact with the WebAPI's auth endpoints, including login and
  logout", so a key must skip `/api/v2/auth/login` entirely. Logging in first
  and then adding the header is not a harmless belt-and-braces -- an API-key
  user has no Web UI password to log in with, so the login call fails and takes
  the whole request with it.
* Username and password, which posts to `/api/v2/auth/login` and keeps the
  session cookie. Still supported in 5.2.0; keys did not replace it.

The response classification below is taken from qBittorrent's source rather
than guessed, because the status codes are counter-intuitive:

* Wrong password answers **HTTP 200** with the body `Fails.` -- not a 401.
* `401` on the login endpoint therefore is not a credentials problem at all.
  It is Host header validation refusing the address before it ever looks at
  the password, which is what a Docker bridge IP or a reverse proxy usually
  trips.
* `403` is the failed-attempt ban, checked *before* the password. A banned IP
  keeps failing after the user fixes their password, which is exactly the
  situation where a generic "authentication failed" sends someone re-typing a
  password that was right all along.
"""

from __future__ import annotations

import re


LOGIN_PATH = "/api/v2/auth/login"

# qBittorrent generates keys as "qbt_" plus 28 random alphanumerics. Used only
# to tell a user their key looks malformed -- never to refuse to send one, so a
# future format change degrades to a normal auth error instead of a hard block.
API_KEY_PATTERN = re.compile(r"^qbt_[A-Za-z0-9]{28}$")

MIN_API_KEY_VERSION = "5.2.0"
MIN_API_KEY_WEBAPI_VERSION = "2.14.1"

_BAN_MARKER = "banned"
_OK_BODY = "ok."
_FAIL_BODY = "fails."


class QbitAuthError(PermissionError):
    """A qBittorrent auth failure that still knows which failure it was.

    Subclasses PermissionError so existing `except PermissionError` handlers
    keep catching it, but carries `kind` and a specific `reason` so callers can
    stop collapsing every cause into one message.
    """

    def __init__(self, reason, *, kind, status=None, body=None):
        super().__init__(reason)
        self.reason = str(reason)
        self.kind = str(kind)
        self.status = status
        self.body = body

    def __str__(self):
        return self.reason


def normalize_base_url(base_url):
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("qBittorrent URL is not configured")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    return base


def api_key_headers(api_key):
    """Header dict for API-key auth, or empty when no key is configured."""
    key = str(api_key or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def api_key_looks_malformed(api_key):
    key = str(api_key or "").strip()
    return bool(key) and not API_KEY_PATTERN.fullmatch(key)


def has_api_key(settings):
    return bool(str((settings or {}).get("api_key") or "").strip())


def classify_login_response(status_code, body):
    """Map a login response to (kind, reason), or None when the login worked.

    Kinds: ok, ip_banned, host_header, credentials, unexpected.
    """
    status = int(status_code or 0)
    text = str(body or "").strip()
    lowered = text.lower()

    if status == 403:
        if _BAN_MARKER in lowered:
            return (
                "ip_banned",
                "qBittorrent has temporarily banned this IP address after too many failed "
                "logins. It will keep refusing logins until the ban expires even if the "
                "password is now correct. Clear it by restarting qBittorrent, or wait out the "
                "ban duration set under Web UI options.",
            )
        return (
            "forbidden",
            "qBittorrent refused the login with 403 Forbidden. "
            f"It said: {text or 'no explanation'}",
        )

    if status == 401:
        return (
            "host_header",
            "qBittorrent returned 401 before it checked the password, which means it rejected "
            "the address InkDrop connected to rather than the credentials. This is its Host "
            "header validation, and it is common when qBittorrent runs in Docker or behind a "
            "reverse proxy. Add the hostname InkDrop uses to 'Server domains' in qBittorrent's "
            "Web UI options, or turn off Host header validation there.",
        )

    if status in {200, 204}:
        if lowered == _OK_BODY or (status == 204 and not text):
            return None
        if lowered == _FAIL_BODY:
            return (
                "credentials",
                "qBittorrent rejected the username and password. Note that qBittorrent can also "
                "bypass the password for local addresses, so a login that works in a browser on "
                "the same machine can still fail from InkDrop.",
            )
        snippet = repr(text[:120]) if text else "an empty body"
        return (
            "unexpected",
            "qBittorrent's login endpoint answered with something other than a login result "
            f"({snippet}). A sign-in page or proxy error here usually means the URL reaches a "
            "reverse proxy or another service rather than qBittorrent.",
        )

    return (
        "unexpected",
        f"qBittorrent's login endpoint answered with HTTP {status}: {text[:120] or 'no body'}",
    )


def authenticate(
    session,
    base_url,
    *,
    username=None,
    password=None,
    api_key=None,
    timeout=15,
    verify=True,
):
    """Prepare `session` to make authenticated qBittorrent calls.

    With an API key this sets the Bearer header and makes no network call at
    all. With a username and password it posts the login and keeps the session
    cookie. Either way the caller can issue normal API requests afterwards.

    Raises QbitAuthError with a specific `kind` on auth failure, and ValueError
    when nothing usable is configured.
    """
    base = normalize_base_url(base_url)
    key = str(api_key or "").strip()
    user = str(username or "").strip()
    secret = str(password or "")

    # Set once, on the session itself, before either auth branch: the API-key
    # branch makes no login call to attach a per-request override to, and every
    # later request on this session (list, add, torrents/info, ...) reuses
    # whatever session.verify was left at. Leaving it at the requests default
    # of True silently ignores a user's verify_tls=false against a self-signed
    # qBittorrent -- real traffic then fails TLS verification even though the
    # separate one-off "Test" call (which passes verify= inline) succeeds.
    session.verify = bool(verify)

    if key:
        # Deliberately no login call: an API key is rejected by the auth
        # endpoints, and an API-key-only user has no password to log in with.
        session.headers.update(api_key_headers(key))
        return {"method": "api_key", "base_url": base, "logged_in": False}

    if not user or not secret:
        raise ValueError(
            "qBittorrent needs either an API key, or a username and password together"
        )

    response = session.post(
        base + LOGIN_PATH,
        data={"username": user, "password": secret},
        timeout=timeout,
        verify=verify,
    )
    failure = classify_login_response(
        getattr(response, "status_code", 0), getattr(response, "text", "")
    )
    if failure is not None:
        kind, reason = failure
        raise QbitAuthError(
            reason,
            kind=kind,
            status=getattr(response, "status_code", None),
            body=str(getattr(response, "text", "") or "")[:200],
        )
    return {"method": "password", "base_url": base, "logged_in": True}


def authenticate_settings(session, settings, *, timeout=15):
    """authenticate() driven by a settings mapping from the config stores.

    Accepts both the legacy acquire-style keys (host/user/pass) and the
    download-client instance keys (base_url/username/password), because both
    config systems are live and callers should not have to care which one
    produced the mapping.
    """
    data = dict(settings or {})
    return authenticate(
        session,
        data.get("base_url") or data.get("host"),
        username=data.get("username") or data.get("user"),
        password=data.get("password") or data.get("pass"),
        api_key=data.get("api_key"),
        timeout=timeout,
        verify=data.get("verify_tls", True),
    )
