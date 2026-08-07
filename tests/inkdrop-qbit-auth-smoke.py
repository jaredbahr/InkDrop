#!/usr/bin/env python3
"""Real-HTTP checks for qBittorrent authentication.

The fixture below is not a convenient stand-in; it reproduces qBittorrent's
actual documented and source-verified behaviour, because the surprising parts
are exactly what InkDrop used to get wrong:

* login with a bad password answers HTTP 200 with the body `Fails.` -- so any
  check that only looks at the status code reads a rejection as a success;
* the failed-attempt ban is checked *before* the password and answers 403 with
  "banned" in the body, so it keeps failing after the password is corrected;
* Host header validation answers 401 *before* the password is looked at;
* an API key is refused by the auth endpoints themselves (qBittorrent docs:
  keys "cannot interact with the WebAPI's auth endpoints, including login and
  logout"), so a key must never be sent to /api/v2/auth/login;
* a cookie from a successful login is what authorises later calls.

Everything runs over a real socket with the real requests library.
"""

from __future__ import annotations

import http.server
import sys
import threading
import urllib.parse

import requests

from core import inkdrop_qbittorrent_auth as qauth


GOOD_USER = "jibz"
GOOD_PASS = "correct horse battery staple"
GOOD_KEY = "qbt_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4"  # qbt_ + 28 chars
VERSION = "v5.2.0"

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}" + (f" -- {detail}" if detail else ""))
        FAILURES.append(label)


class QbitFixture(http.server.BaseHTTPRequestHandler):
    """Faithful subset of the qBittorrent Web API v2 auth surface."""

    mode = "normal"  # normal | banned | host_header | proxy_login_page | redirect

    def log_message(self, *args):
        pass

    def _send(self, status, body, headers=None):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _bearer(self):
        raw = self.headers.get("Authorization") or ""
        return raw[7:].strip() if raw.startswith("Bearer ") else ""

    def do_POST(self):
        if not self.path.startswith("/api/v2/auth/login"):
            return self._send(404, "Not Found")

        # Host header validation runs before anything looks at credentials.
        if self.mode == "host_header":
            return self._send(401, "Unauthorized")
        # The ban check also precedes the credential check, which is why a
        # banned IP keeps failing after the password is fixed.
        if self.mode == "banned":
            return self._send(403, "Your IP address has been banned after too many failed authentication attempts.")
        if self.mode == "proxy_login_page":
            return self._send(200, "<html><body>Sign in to continue</body></html>")
        if self.mode == "redirect":
            return self._send(301, "", {"Location": "https://qbit.example/api/v2/auth/login"})

        # An API key must be refused here: keys cannot use the auth endpoints.
        if self._bearer():
            return self._send(403, "Forbidden")

        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        user = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]
        if user == GOOD_USER and password == GOOD_PASS:
            return self._send(200, "Ok.", {"Set-Cookie": "SID=session-token; path=/"})
        return self._send(200, "Fails.")

    def do_GET(self):
        if not self.path.startswith("/api/v2/app/version"):
            return self._send(404, "Not Found")
        if self.mode == "host_header":
            return self._send(401, "Unauthorized")
        key = self._bearer()
        if key:
            return self._send(200, VERSION) if key == GOOD_KEY else self._send(403, "Forbidden")
        if "SID=session-token" in (self.headers.get("Cookie") or ""):
            return self._send(200, VERSION)
        return self._send(403, "Forbidden")


def serve():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), QbitFixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main():
    server, base = serve()
    try:
        run_checks(base)
    finally:
        server.shutdown()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("all qBittorrent auth checks passed")
    return 0


def run_checks(base):
    # --- API key: authenticates without ever calling the login endpoint ---
    QbitFixture.mode = "normal"
    session = requests.Session()
    result = qauth.authenticate(session, base, api_key=GOOD_KEY)
    check("api key does not call the login endpoint", result["logged_in"] is False)
    check("api key sets a Bearer header", session.headers.get("Authorization") == f"Bearer {GOOD_KEY}")
    version = session.get(base + "/api/v2/app/version", timeout=5)
    check("api key authorises a real API call", version.status_code == 200 and version.text == VERSION,
          f"got {version.status_code} {version.text!r}")

    # A key with no username/password must work -- the case that blocked Jibz.
    session = requests.Session()
    qauth.authenticate_settings(session, {"host": base, "user": "", "pass": "", "api_key": GOOD_KEY})
    check("api key alone is a complete credential",
          session.get(base + "/api/v2/app/version", timeout=5).status_code == 200)

    # Proof the fixture would reject a key sent to the login endpoint, i.e.
    # that "log in first, then add the header" really is broken.
    refused = requests.post(base + "/api/v2/auth/login", data={"username": "x", "password": "y"},
                            headers={"Authorization": f"Bearer {GOOD_KEY}"}, timeout=5)
    check("api keys are refused by the login endpoint", refused.status_code == 403,
          f"got {refused.status_code}")

    # --- Username/password still works alongside keys ---
    session = requests.Session()
    result = qauth.authenticate(session, base, username=GOOD_USER, password=GOOD_PASS)
    check("password login succeeds", result["method"] == "password" and result["logged_in"] is True)
    check("password login keeps the session cookie",
          session.get(base + "/api/v2/app/version", timeout=5).status_code == 200)

    # --- Each failure mode stays distinguishable ---
    session = requests.Session()
    try:
        qauth.authenticate(session, base, username=GOOD_USER, password="wrong")
        check("wrong password is detected", False, "no error raised")
    except qauth.QbitAuthError as exc:
        check("wrong password is detected", exc.kind == "credentials", f"kind={exc.kind}")
        check("wrong password is still a PermissionError", isinstance(exc, PermissionError))

    QbitFixture.mode = "banned"
    try:
        qauth.authenticate(requests.Session(), base, username=GOOD_USER, password=GOOD_PASS)
        check("ip ban is detected", False, "no error raised")
    except qauth.QbitAuthError as exc:
        check("ip ban is detected even with the right password", exc.kind == "ip_banned", f"kind={exc.kind}")
        check("ip ban message explains the password is not the problem",
              "ban" in exc.reason.lower() and "correct" in exc.reason.lower())

    QbitFixture.mode = "host_header"
    try:
        qauth.authenticate(requests.Session(), base, username=GOOD_USER, password=GOOD_PASS)
        check("host header rejection is detected", False, "no error raised")
    except qauth.QbitAuthError as exc:
        check("host header rejection is not reported as bad credentials", exc.kind == "host_header", f"kind={exc.kind}")
        check("host header message names the actual fix",
              "host" in exc.reason.lower() and "server domains" in exc.reason.lower())

    QbitFixture.mode = "proxy_login_page"
    try:
        qauth.authenticate(requests.Session(), base, username=GOOD_USER, password=GOOD_PASS)
        check("a proxy sign-in page is detected", False, "no error raised")
    except qauth.QbitAuthError as exc:
        check("a proxy sign-in page is not reported as bad credentials", exc.kind == "unexpected", f"kind={exc.kind}")

    # All four causes must produce four different messages, or the user is back
    # to guessing -- this is the defect being fixed.
    QbitFixture.mode = "normal"
    reasons = set()
    for mode, kwargs in (
        ("normal", {"username": GOOD_USER, "password": "wrong"}),
        ("banned", {"username": GOOD_USER, "password": GOOD_PASS}),
        ("host_header", {"username": GOOD_USER, "password": GOOD_PASS}),
        ("proxy_login_page", {"username": GOOD_USER, "password": GOOD_PASS}),
    ):
        QbitFixture.mode = mode
        try:
            qauth.authenticate(requests.Session(), base, **kwargs)
        except qauth.QbitAuthError as exc:
            reasons.add(exc.reason)
    check("four failure causes give four different messages", len(reasons) == 4, f"got {len(reasons)}")

    # --- Configuration errors ---
    QbitFixture.mode = "normal"
    try:
        qauth.authenticate(requests.Session(), base)
        check("missing credentials are a configuration error", False, "no error raised")
    except ValueError as exc:
        check("missing credentials are a configuration error", "API key" in str(exc))
    try:
        qauth.authenticate(requests.Session(), base, username=GOOD_USER)
        check("a username with no password is a configuration error", False, "no error raised")
    except ValueError:
        check("a username with no password is a configuration error", True)

    check("a well-formed key is accepted", not qauth.api_key_looks_malformed(GOOD_KEY))
    check("a truncated key is flagged", qauth.api_key_looks_malformed("qbt_short"))
    check("a malformed key is still sent rather than blocked",
          qauth.api_key_headers("qbt_short") == {"Authorization": "Bearer qbt_short"})

    end_to_end_checks(base)


def end_to_end_checks(base):
    """The reported bug, driven through the real service layer over real HTTP."""
    from core import inkdrop_download_client_api as api
    from core import inkdrop_download_client_config as store

    QbitFixture.mode = "normal"

    # 1. Saving an API-key-only client. This used to be refused before any
    #    network call, because the schema demanded a username and a password.
    candidate = store._normalize_candidate(
        {"client_type": "qbittorrent", "name": "qB", "base_url": base, "enabled": True},
        schema_resolver=api.schema_resolver,
    )
    try:
        store._validate_ready(candidate, {"api_key": True})
        check("an api-key-only client passes validation", True)
    except ValueError as exc:
        check("an api-key-only client passes validation", False, str(exc))

    # 2. Username+password remains valid, so existing installs keep working.
    named = store._normalize_candidate(
        {"client_type": "qbittorrent", "name": "qB", "base_url": base, "username": GOOD_USER, "enabled": True},
        schema_resolver=api.schema_resolver,
    )
    try:
        store._validate_ready(named, {"password": True})
        check("username and password still pass validation", True)
    except ValueError as exc:
        check("username and password still pass validation", False, str(exc))

    # 3. A password with no username is rejected up front rather than becoming
    #    an opaque "Fails." from qBittorrent later.
    try:
        store._validate_ready(candidate, {"password": True})
        check("a password with no username is refused at save time", False, "accepted")
    except ValueError:
        check("a password with no username is refused at save time", True)

    # 4. No credentials at all is still refused.
    try:
        store._validate_ready(candidate, {})
        check("a client with no credentials is refused", False, "accepted")
    except ValueError:
        check("a client with no credentials is refused", True)

    # 5. Test Draft with an api key only -- the button Jibz could not reach.
    draft = api.test_draft({
        "client_type": "qbittorrent", "name": "qB", "base_url": base,
        "enabled": True, "secrets": {"api_key": GOOD_KEY},
    })
    result = draft["result"]
    check("Test Draft succeeds with an api key alone", result.get("ok") is True, repr(result))
    check("Test Draft reports which auth method it used", result.get("auth_method") == "api_key", repr(result))
    check("Test Draft returns the server version", result.get("version") == VERSION, repr(result))

    # 6. Test Draft with username+password over the same real HTTP path.
    draft = api.test_draft({
        "client_type": "qbittorrent", "name": "qB", "base_url": base,
        "username": GOOD_USER, "enabled": True, "secrets": {"password": GOOD_PASS},
    })
    check("Test Draft succeeds with username and password", draft["result"].get("ok") is True, repr(draft["result"]))
    check("Test Draft reports password auth", draft["result"].get("auth_method") == "password")

    # 7. The three causes stay distinct all the way out to the API response.
    seen = {}
    for mode, label in (("normal", "credentials"), ("banned", "ip_banned"), ("host_header", "host_header")):
        QbitFixture.mode = mode
        outcome = api.test_draft({
            "client_type": "qbittorrent", "name": "qB", "base_url": base,
            "username": GOOD_USER, "enabled": True,
            "secrets": {"password": GOOD_PASS if mode != "normal" else "wrong"},
        })["result"]
        seen[label] = outcome
        check(f"Test Draft classifies {label}", outcome.get("auth_failure") == label, repr(outcome))
    check("Test Draft messages differ per cause",
          len({row.get("reason") for row in seen.values()}) == 3)
    check("Test Draft never leaks the password",
          all(GOOD_PASS not in repr(row) for row in seen.values()))

    # 8. A bad api key is named as a key problem, not "connectivity".
    QbitFixture.mode = "normal"
    outcome = api.test_draft({
        "client_type": "qbittorrent", "name": "qB", "base_url": base,
        "enabled": True, "secrets": {"api_key": "qbt_" + "z" * 28},
    })["result"]
    check("a rejected api key is reported as an auth problem",
          outcome.get("error_type") == "authentication" and outcome.get("auth_failure") == "api_key", repr(outcome))
    check("the rejected-key message mentions the version requirement",
          qauth.MIN_API_KEY_VERSION in str(outcome.get("reason") or ""), repr(outcome))

    # 9. A redirecting reverse proxy is diagnosable instead of a bare class name.
    QbitFixture.mode = "redirect"
    outcome = api.test_draft({
        "client_type": "qbittorrent", "name": "qB", "base_url": base,
        "username": GOOD_USER, "enabled": True, "secrets": {"password": GOOD_PASS},
    })["result"]
    check("a redirect explains itself", "redirect" in str(outcome.get("reason") or "").lower(), repr(outcome))
    QbitFixture.mode = "normal"


if __name__ == "__main__":
    sys.exit(main())
