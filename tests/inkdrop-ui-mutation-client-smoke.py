#!/usr/bin/env python3
"""Static regression for authenticated browser mutations and Add Series layout."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WEB = (ROOT / "core" / "inkdrop_web.py").read_text(encoding="utf-8")
AUTH = (ROOT / "web/static/js/inkdrop-auth-ui.js").read_text(encoding="utf-8")
API = (ROOT / "web/static/js/inkdrop-api.js").read_text(encoding="utf-8")
DOWNLOAD_CLIENTS = (ROOT / "web/static/js/inkdrop-download-clients-ui.js").read_text(encoding="utf-8")
CSS = (ROOT / "web/static/css/inkdrop.css").read_text(encoding="utf-8")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


raw_mutation = re.compile(
    r"fetch\s*\([^;]{0,800}?method\s*:\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]",
    re.IGNORECASE | re.DOTALL,
)

require(not raw_mutation.search(WEB), "inkdrop_web.py contains a raw browser mutation")
require(not raw_mutation.search(AUTH), "inkdrop-auth-ui.js contains a raw browser mutation")
require(not raw_mutation.search(DOWNLOAD_CLIENTS), "inkdrop-download-clients-ui.js contains a raw browser mutation")
require("global.InkDropApi.request" in DOWNLOAD_CLIENTS, "download-client mutations must use the shared CSRF client")
require("window.InkDropApi.request" in WEB, "application mutation helper does not use InkDropApi")
require("window.InkDropApi.request" in AUTH, "auth UI does not use InkDropApi")
require('credentials: "same-origin"' in API, "canonical client must send same-origin credentials")
require("DEFAULT_CSRF_COOKIE" in API and "DEFAULT_CSRF_HEADER" in API, "canonical client lacks CSRF contract")
require('AUTH_STATUS_PATH = "/api/auth/status"' in API, "canonical client must hydrate Core auth status")
require("browser_mutation_contract" in API, "canonical client must consume Core browser mutation contract")
require("csrf_cookie_name" in API and "csrf_header_name" in API, "canonical client must consume Core CSRF field names")
require("protected_methods" in API, "canonical client must consume Core protected mutation methods")
require("refreshAuthContract" in API, "canonical client must expose auth contract refresh")
require("localStorage" not in API and "sessionStorage" not in API, "canonical client must not persist auth material")
require("series-result-row" in CSS and "@media (max-width: 520px)" in CSS, "mobile Add Series cards are missing")
require('data-label="Year"' in WEB and 'data-label="Issues"' in WEB, "mobile Add Series labels are missing")
require("password_policy" in AUTH, "auth UI does not consume Core password policy")
require("csrf_token_mismatch" in API, "canonical client does not present CSRF mismatch safely")
require("origin_header_required" in API and "origin_validation_failed" in API, "canonical client does not present Origin failures safely")

print("PASS: authenticated mutation client and mobile Add Series static contract")
