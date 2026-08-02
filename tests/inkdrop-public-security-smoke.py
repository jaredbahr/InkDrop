#!/usr/bin/env python3
"""Release-level security posture smoke for public InkDrop installs.

This smoke ties together the existing focused guards so the public-readiness
audit has one small proof that secrets, source HTTP requests, destructive paths,
archives, and install defaults are still guarded.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import inkdrop_preflight
import inkdrop_runtime_config
from tools import inkdrop_install_support_summary


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def read(path):
    return (ROOT / path).read_text(encoding="utf-8", errors="replace")


def require_tokens(path, tokens):
    text = read(path)
    for token in tokens:
        require(token in text, f"{path} is missing security proof token: {token}")


def assert_static_security_contracts():
    require_tokens(
        "docs/inkdrop/public-runtime-config-contract.md",
        ("secret references", "path allowlists", "archive traversal checks", "SSRF guardrails"),
    )
    require_tokens(
        "inkdrop-source-worker-http-smoke.py",
        (
            "redacted request contains no raw secret",
            "Torznab redacted request contains no raw secret",
            "disallowed_scheme",
            "disallowed_host",
            "request-level hosts are ignored by default",
            "response_too_large",
            "final redirect host escape rejected",
            "each redirect hop is host-confined before follow",
            "request route cap blocks configured-only redirect host",
            "blocked before a second request",
        ),
    )
    require_tokens(
        "inkdrop-direct-source-certification-smoke.py",
        (
            "sidecar drops cookie headers",
            "redirect host escape is blocked",
            "Comics-All is not an approved transport",
            "FlorenFile is not an approved transport",
            "non-2xx Pixeldrain probe is blocked",
            "receives no second-hop request",
            "destination body is never requested or read",
        ),
    )
    require_tokens(
        "inkdrop_state.py",
        ("privacy_safe_evidence_payload", "SENSITIVE_EVIDENCE_HEADER_KEYS"),
    )
    require_tokens(
        "inkdrop-series-remove-files-smoke.py",
        ("outside configured comic/manga roots", '"deleteFiles": True', "default remove should not delete files"),
    )
    require_tokens(
        "inkdrop-import-ready-worker-smoke.py",
        ("validate_comic_archive", "zip_file_invalid", "load_bad_archive_validation_memory"),
    )
    require_tokens(
        "inkdrop-public-release-safety-audit.py",
        ("scan_env_example_secret_defaults", "scan_public_image_context_private_text_files", "scan_public_image_root_private_files"),
    )
    require_tokens(
        "inkdrop_web.py",
        (
            "DEBUG_ACTIVE_REQUESTS_ENABLED",
            "INKDROP_DEBUG_ACTIVE_REQUESTS",
            "Active request diagnostics are disabled.",
            'path == "/api/inkdrop-debug/active-requests"',
        ),
    )


def assert_redacted_first_run_security_summary():
    with tempfile.TemporaryDirectory(prefix="inkdrop-public-security-") as tmp:
        root = Path(tmp)
        env = {
            inkdrop_runtime_config.ENV_CONFIG_DIR: str(root / "config"),
            inkdrop_runtime_config.ENV_STATE_DIR: str(root / "state"),
            inkdrop_runtime_config.ENV_LOG_DIR: str(root / "state" / "logs"),
            inkdrop_runtime_config.ENV_CACHE_DIR: str(root / "state" / "cache"),
            inkdrop_runtime_config.ENV_BACKUP_DIR: str(root / "state" / "backups"),
            inkdrop_runtime_config.ENV_STAGING_DIR: str(root / "staging"),
            inkdrop_runtime_config.ENV_MANUAL_INBOX_DIR: str(root / "manual-inbox"),
            inkdrop_runtime_config.ENV_QUARANTINE_DIR: str(root / "state" / "quarantine"),
            "INKDROP_COMICVINE_API_KEY": "super-secret-comicvine",
            "INKDROP_PROWLARR_URL": "http://prowlarr:9696?apikey=super-secret-query&label=public",
            "INKDROP_PROWLARR_API_KEY": "super-secret-prowlarr",
            "INKDROP_QBITTORRENT_URL": "http://user:super-secret-qbit@qbittorrent:8080",
            "INKDROP_QBITTORRENT_PASSWORD": "super-secret-password",
            "INKDROP_SAB_PATH_MAPPINGS": r"/host/downloads=C:\secret-downloads",
        }
        payload = inkdrop_preflight.run_preflight(env, create=True)
        encoded = json.dumps(payload, sort_keys=True)
        require("super-secret" not in encoded, "preflight output leaked a secret")
        require(r"C:\secret-downloads" not in encoded, "preflight output leaked a private host path")
        effective = payload["effective_config"]["env"]
        require(effective["INKDROP_COMICVINE_API_KEY"] == "<set>", "ComicVine secret should be summarized")
        require(effective["INKDROP_PROWLARR_API_KEY"] == "<set>", "Prowlarr secret should be summarized")
        require(effective["INKDROP_QBITTORRENT_PASSWORD"] == "<set>", "qBittorrent secret should be summarized")
        require(effective["INKDROP_QBITTORRENT_URL"] == "http://<redacted>@qbittorrent:8080", "URL user-info should be redacted")
        require("apikey=%3Credacted%3E" in effective["INKDROP_PROWLARR_URL"], "secret URL query should be redacted")
        require("label=public" in effective["INKDROP_PROWLARR_URL"], "non-secret URL query should be preserved")
        require(payload["path_mappings"]["INKDROP_SAB_PATH_MAPPINGS"]["entries"][0]["source"] == "<source-path>", "path mapping source should be redacted")

    defaults = inkdrop_install_support_summary._install_defaults()
    require(defaults["optional_adapter_defaults"]["comicvine_api_key"] == "", "ComicVine default secret must stay blank")
    require(defaults["optional_adapter_defaults"]["qbittorrent_password"] == "", "qBittorrent default secret must stay blank")
    require(defaults["optional_adapter_defaults"]["sabnzbd_api_key"] == "", "SAB default secret must stay blank")
    require("must not enable adapters" in defaults["secret_policy"], "install defaults should warn that suggestions are inactive")


def main():
    assert_static_security_contracts()
    assert_redacted_first_run_security_summary()
    print("INKDROP_PUBLIC_SECURITY_OK: redaction, HTTP bounds, path allowlists, archive guards, and blank defaults are guarded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
