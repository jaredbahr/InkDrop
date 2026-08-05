#!/usr/bin/env python3
"""Static contract for the instance-scoped Download Clients settings UI."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# inkdrop_web_config.py holds the static-asset registration constants that
# used to live directly in inkdrop_web.py; the mount/asset contract below
# spans both files.
WEB = (ROOT / "inkdrop_web.py").read_text(encoding="utf-8") + (ROOT / "inkdrop_web_config.py").read_text(encoding="utf-8")
JS = (ROOT / "web/static/js/inkdrop-download-clients-ui.js").read_text(encoding="utf-8")
CSS = (ROOT / "web/static/css/inkdrop.css").read_text(encoding="utf-8")
FIXTURE = (ROOT / "web/tests/fixtures/download-clients-settings.html").read_text(encoding="utf-8")
BROWSER = (ROOT / "web/tests/download-clients-settings-browser-smoke.js").read_text(encoding="utf-8")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    for marker in (
        '"inkdrop-download-clients-ui.js"',
        'src="/static/js/inkdrop-download-clients-ui.js?v=__INKDROP_UI_JS_VERSION__"',
        "window.InkDropDownloadClients?.mount?.(providerTarget)",
    ):
        require(marker in WEB, f"web mount/asset contract missing: {marker}")
    for label in ("Add Download Client", "Torrent", "Usenet", "Soulseek", "Test All Clients", "Test Draft", "Test Saved", "Delete Client"):
        require(label in JS, f"UI label missing: {label}")
    for client in ("qbittorrent", "transmission", "deluge", "utorrent", "rtorrent", "sabnzbd", "nzbget", "slskd"):
        require(client in JS and client in FIXTURE, f"client missing from UI/fixture: {client}")
    for contract in (
        'method: editing ? "PATCH" : "POST"',
        'method: "DELETE"',
        '"If-Match"',
        'clear_secret_fields',
        'data-download-client-manager',
        'aria-live',
        'aria-modal',
        'MAX_MAPPINGS = 32',
        '/api/download-clients/test-all',
        '/api/download-clients/test-runs/',
    ):
        require(contract in JS or contract in BROWSER, f"instance UI contract missing: {contract}")
    require("@media (max-width: 760px)" in CSS and ".download-client-mapping-row" in CSS, "mobile mapping contract missing")
    require("min-height: 44px" in CSS, "44px interaction target contract missing")
    require("revision conflict" in BROWSER and "deleteGuard" in FIXTURE, "revision/delete conflict fixture missing")
    require("first-secret" in BROWSER and "never echo" in BROWSER, "write-only secret browser coverage missing")
    print("INKDROP_DOWNLOAD_CLIENT_UI_SMOKE_OK")


if __name__ == "__main__":
    main()
