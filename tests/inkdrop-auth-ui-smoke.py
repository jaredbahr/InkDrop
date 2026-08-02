#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = (ROOT / "inkdrop_web.py").read_text(encoding="utf-8")
JS = (ROOT / "web/static/js/inkdrop-auth-ui.js").read_text(encoding="utf-8")
CSS = (ROOT / "web/static/css/inkdrop.css").read_text(encoding="utf-8")
FIXTURE = (ROOT / "web/tests/fixtures/auth-first-run.html").read_text(encoding="utf-8")
VISUAL_BROWSER = (ROOT / "web/tests/auth-visual-browser-smoke.js").read_text(encoding="utf-8")
BACKDROP = ROOT / "web/static/img/inkdrop-auth-backdrop.webp"


def require(text, needle, label):
    if needle not in text:
        raise AssertionError(f"missing {label}: {needle}")


def forbid(text, needle, label):
    if needle in text:
        raise AssertionError(f"unsafe {label}: {needle}")


def main():
    require(WEB, 'id="inkdropAppShell" hidden', "pre-validation hidden shell")
    require(WEB, '"/static/img/inkdrop-auth-backdrop.webp"', "public project-art route")
    require(WEB, "def send_auth_backdrop", "bounded backdrop sender")
    require(WEB, 'window.addEventListener("inkdrop-auth-ready"', "auth-gated startup")
    require(JS, '"/api/auth/bootstrap"', "first-admin endpoint")
    require(JS, '"/api/auth/login"', "login endpoint")
    require(JS, '"/api/auth/logout"', "logout endpoint")
    require(JS, '"/api/auth/password"', "password endpoint")
    require(JS, '"/api/auth/api-keys"', "API-key endpoint")
    require(JS, 'state.status.built_in_auth?.bootstrap_required', "schema-13 bootstrap status")
    require(JS, 'const FIRST_RUN_SETUP_HASH = "#settings?area=setup";', "first-run Setup destination")
    require(JS, 'state.status?.setup_required !== true', "setup-required route guard")
    require(JS, 'String(window.location.hash || "").replace(/^#/, "").trim()', "explicit deep-link preservation")
    require(JS, 'window.history.replaceState(window.history.state, "", target)', "non-disruptive Setup route handoff")
    require(JS, 'name="expiration"', "API-key expiration selection")
    require(JS, 'body.expires_in_seconds', "API-key expiration payload")
    require(JS, 'key.expired', "expired API-key display")
    require(JS, 'Current session expires', "session expiration display")
    require(JS, 'InkDrop will not show this key again', "one-time key warning")
    require(JS, 'maskedFingerprint', "masked fingerprint rendering")
    require(JS, 'error.retryAfter', "lockout feedback")
    require(JS, 'Your session expired. Sign in again.', "expired-session feedback")
    require(JS, 'state.oneTimeKey = ""', "one-time secret clearing")
    require(JS, 'navigator?.connection?.saveData', "Save-Data auth artwork suppression")
    require(JS, 'prefers-reduced-data: reduce', "reduced-data artwork preference")
    require(JS, "authArtworkVariant", "session-randomized project artwork crop")
    require(CSS, '.inkdrop-auth-root', "auth layout")
    require(CSS, 'position: fixed', "full-viewport auth gate")
    require(CSS, '@media (max-width: 600px)', "mobile auth layout")
    require(CSS, '.inkdrop-auth-root[data-auth-art="off"]::before', "no-art fallback")
    require(CSS, '.inkdrop-auth-root[data-auth-art="project"]::before', "preference-gated art opt-in")
    forbid(CSS.split('.inkdrop-auth-root[data-auth-art="project"]::before', 1)[0], "inkdrop-auth-backdrop.webp", "eager backdrop URL before opt-in selector")
    require(CSS, "prefers-reduced-motion: reduce", "reduced-motion auth contract")
    forbid(CSS, "cover-proxy", "private cover proxy auth artwork")
    require(FIXTURE, 'window.__authFixture', "browser auth fixture")
    require(VISUAL_BROWSER, "saveDataArtRequests.length, 0", "Save-Data request-level regression")
    require(VISUAL_BROWSER, "reducedDataArtRequests.length, 0", "reduced-data request-level regression")
    require(VISUAL_BROWSER, '"first administrator must continue into Setup"', "first-admin Setup browser regression")
    require(VISUAL_BROWSER, '"explicit deep links must win over the Setup handoff"', "explicit deep-link browser regression")
    require(VISUAL_BROWSER, '"returning configured login must keep the normal default route"', "returning-login browser regression")
    forbid(JS, "localStorage", "credential persistence")
    forbid(JS, "sessionStorage", "credential persistence")
    forbid(JS, "console.log", "secret-capable browser logging")
    assert BACKDROP.is_file() and 0 < BACKDROP.stat().st_size <= 64 * 1024, "project-owned backdrop must remain present and packaging-bounded"
    print("InkDrop auth UI smoke passed")


if __name__ == "__main__":
    main()
