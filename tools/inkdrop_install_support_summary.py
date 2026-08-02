#!/usr/bin/env python3
"""Print a redacted InkDrop install/support summary."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import inkdrop_preflight
import inkdrop_public_contracts
import inkdrop_runtime_config
import inkdrop_state


ADAPTER_GUIDANCE = {
    "comicvine": {
        "impact": "ComicVine metadata lookup and series discovery are unavailable.",
        "next_step": "Set INKDROP_COMICVINE_API_KEY when you want ComicVine metadata.",
    },
    "prowlarr": {
        "impact": "Indexer-backed usenet/torrent searches are unavailable.",
        "next_step": "Set INKDROP_PROWLARR_URL and INKDROP_PROWLARR_API_KEY after Prowlarr is reachable from the InkDrop container.",
    },
    "sabnzbd": {
        "impact": "Usenet download handoff through SABnzbd is unavailable.",
        "next_step": "Set INKDROP_SABNZBD_URL and INKDROP_SABNZBD_API_KEY, then configure any needed INKDROP_SAB_PATH_MAPPINGS.",
    },
    "qbittorrent": {
        "impact": "Torrent download handoff through qBittorrent is unavailable.",
        "next_step": "Set INKDROP_QBITTORRENT_URL and credentials if your qBittorrent instance requires them.",
    },
    "slskd": {
        "impact": "Soulseek/slskd searching and download monitoring are unavailable.",
        "next_step": "Set INKDROP_SLSKD_API_BASE_URL after slskd is reachable from the InkDrop container.",
    },
    "suwayomi": {
        "impact": "Suwayomi-backed manga source integration is unavailable.",
        "next_step": "Set INKDROP_SUWAYOMI_API_BASE_URL when you want Suwayomi integration.",
    },
    "kavita": {
        "impact": "Kavita visibility sync is unavailable.",
        "next_step": "Set INKDROP_KAVITA_URL only if you want optional Kavita sync.",
    },
    "komga": {
        "impact": "Komga visibility sync is unavailable.",
        "next_step": "Set INKDROP_KOMGA_URL only if you want optional Komga sync.",
    },
}

RELEASE_GATE_COMMAND = "python -B tools/inkdrop_public_release_check.py --docker --require-docker"
REQUIRED_RELEASE_GATE_CHECKS = [
    "docker_compose_config",
    "docker_compose_build",
    "docker_container_strict_preflight",
    "docker_container_healthcheck_preflight",
]


def _install_defaults():
    return {
        "paths": {
            "config_dir": "/config",
            "state_dir": "/state",
            "log_dir": "/state/logs",
            "cache_dir": "/state/cache",
            "backup_dir": "/state/backups",
            "staging_dir": "/staging",
            "manual_inbox_dir": "/manual-inbox",
            "quarantine_dir": "/state/quarantine",
            "comic_root": "/library/comics",
            "manga_root": "/library/manga",
            "manual_comics_inbox": "/manual-inbox/comics",
            "manual_ebooks_inbox": "/manual-inbox/ebooks",
            "slskd_download_root": "/staging/slskd",
            "slskd_incomplete_root": "/staging/slskd/incomplete",
            "unmatched_download_root": "/staging/downloads/comics",
            "direct_download_root": "/staging/direct/comics",
            "pack_download_root": "/staging/downloads/comics",
            "pack_temp_download_root": "/staging/temp/downloads/comics",
            "ebook_download_root": "/staging/ebooks",
            "suwayomi_staging_root": "/staging/suwayomi",
            "source_worker_staging_root": "/staging/source-worker",
            "unmatched_quarantine_root": "/state/quarantine/unmatched",
            "managed_duplicate_quarantine_root": "/state/quarantine/managed-duplicate-files",
            "pack_duplicate_quarantine_root": "/state/quarantine/pack-duplicates",
            "pack_review_quarantine_root": "/state/quarantine/pack-review",
            "comic_incoming_root": "/library/comics/_Incoming",
            "ebook_incoming_root": "/library/ebooks/_Incoming",
            "qbittorrent_download_root": "/staging/downloads",
            "download_staging_root": "/staging",
        },
        "automation": {
            "protocol_order": "usenet,torrent,direct",
            "queue_runner_import_priority_ready_imports": "1",
            "autopilot_runtime_hard_grace_seconds": "90",
            "import_ready_queue_only": "0",
            "import_ready_import_timeout_seconds": "240",
            "import_ready_batch_timeout_seconds": "600",
            "pack_manifest_cache_seconds": "21600",
            "reconciled_import_sync_budget_seconds": "20",
            "manga_completion_backfill_limit": "50",
            "pack_probe_scan_seconds": "2",
            "pack_probe_scan_entries": "20000",
            "debug_active_requests": "0",
        },
        "optional_adapter_defaults": {
            "comicvine_api_key": "",
            "prowlarr_url": "",
            "prowlarr_api_key": "",
            "sabnzbd_url": "",
            "sabnzbd_api_key": "",
            "qbittorrent_url": "",
            "qbittorrent_username": "",
            "qbittorrent_password": "",
            "slskd_api_base_url": "",
            "slskd_web_url": "",
            "suwayomi_api_base_url": "",
            "kavita_url": "",
            "komga_url": "",
        },
        "compose_service_suggestions": {
            "prowlarr_url": "http://prowlarr:9696",
            "sabnzbd_url": "http://sabnzbd:8080",
            "qbittorrent_url": "http://qbittorrent:8080",
            "slskd_api_base_url": "http://slskd:5030/api/v0",
            "slskd_web_url": "http://slskd:5030",
            "suwayomi_api_base_url": "http://suwayomi:4567",
            "kavita_url": "http://kavita:5000",
            "komga_url": "http://komga:25600",
        },
        "secret_policy": "Secrets and optional adapter endpoints stay blank in public defaults; first-run UI may offer service-name suggestions but must not enable adapters without user-provided values.",
    }


def _root_summary(roots):
    summary = {}
    for name, item in sorted((roots or {}).items()):
        summary[name] = {
            "exists": bool(item.get("exists")),
            "is_dir": bool(item.get("is_dir")),
            "required": bool(item.get("required")),
            "writable": bool(item.get("writable")),
            "error": item.get("error") or "",
        }
    return summary


def _adapter_summary(configured_adapters):
    summary = {}
    for name, item in sorted((configured_adapters or {}).items()):
        guidance = ADAPTER_GUIDANCE.get(name, {})
        summary[name] = {
            "configured": bool(item.get("configured")),
            "configured_by": item.get("configured_by") or "",
            "missing_required_keys": list(item.get("missing_required_keys") or []),
            "existing_path_exists_keys": list(item.get("existing_path_exists_keys") or []),
            "existing_path_missing_keys": list(item.get("existing_path_missing_keys") or []),
            "reason": item.get("reason") or "",
            "impact": "" if item.get("configured") else guidance.get("impact", ""),
            "next_step": "" if item.get("configured") else guidance.get("next_step", ""),
        }
    return summary


def _setup_guidance(preflight_summary, *, docker_available=False):
    adapters = preflight_summary.get("configured_adapters") or {}
    path_mappings = preflight_summary.get("path_mappings") or {}
    warnings = preflight_summary.get("warning_summary") or {}
    steps = []
    if not docker_available:
        steps.append(
            {
                "area": "docker",
                "severity": "warning",
                "message": "Docker CLI is not available in this environment; run Docker Compose build and container preflight on a Docker-capable host before a broader release.",
            }
        )
    if preflight_summary.get("errors"):
        steps.append(
            {
                "area": "preflight_errors",
                "severity": "error",
                "message": "Resolve preflight errors before starting InkDrop.",
            }
        )
    if warnings.get("runtime_tools_missing"):
        steps.append(
            {
                "area": "runtime_tools",
                "severity": "warning",
                "message": "Archive tooling is missing on this host; the Docker image should include it, but direct host runs may have limited CBR/RAR support.",
            }
        )
    if warnings.get("python_dependencies_missing"):
        steps.append(
            {
                "area": "python_dependencies",
                "severity": "warning",
                "message": "Install requirements.txt for direct host runs, or use the Docker image.",
            }
        )
    if adapters.get("comicvine", {}).get("configured") is False:
        steps.append(
            {
                "area": "metadata",
                "severity": "info",
                "message": "ComicVine is optional for startup but recommended for comic metadata and add-series workflows.",
            }
        )
    download_adapters = [
        name
        for name in ("prowlarr", "sabnzbd", "qbittorrent", "slskd")
        if adapters.get(name, {}).get("configured")
    ]
    if not download_adapters:
        steps.append(
            {
                "area": "download_sources",
                "severity": "info",
                "message": "No download/source adapters are configured; InkDrop can start, but automation will be limited to local/manual workflows.",
            }
        )
    if any(item.get("errors") for item in path_mappings.values()):
        steps.append(
            {
                "area": "path_mappings",
                "severity": "error",
                "message": "Fix path mapping errors so completed downloader paths map to InkDrop container paths.",
            }
        )
    return steps


def _path_mapping_summary(path_mappings):
    summary = {}
    for key, item in sorted((path_mappings or {}).items()):
        entries = item.get("entries") or []
        summary[key] = {
            "configured": bool(item.get("configured")),
            "entry_count": len(entries),
            "valid_entry_count": len([entry for entry in entries if entry.get("valid")]),
            "errors": list(item.get("errors") or []),
        }
    return summary


def _release_gate_summary(*, docker_available):
    if docker_available:
        status = "docker_checks_required"
        next_step = "Run the Docker Compose release gate on this Docker-capable host before a broader release."
    else:
        status = "docker_unavailable"
        next_step = "Run the Docker Compose release gate on a Docker-capable host before a broader release."
    return {
        "ready": False,
        "docker_available": bool(docker_available),
        "status": status,
        "required_command": RELEASE_GATE_COMMAND,
        "required_checks": list(REQUIRED_RELEASE_GATE_CHECKS),
        "next_step": next_step,
    }


def build_summary(*, create=False, strict_dependencies=False, strict_runtime_tools=False):
    preflight = inkdrop_preflight.run_preflight(
        create=create,
        strict_dependencies=strict_dependencies,
        strict_runtime_tools=strict_runtime_tools,
    )
    preflight_summary = {
        "ok": bool(preflight.get("ok")),
        "created_missing_dirs": bool(preflight.get("created_missing_dirs")),
        "warning_summary": preflight.get("warning_summary") or {},
        "errors": list(preflight.get("errors") or []),
        "warnings": list(preflight.get("warnings") or []),
        "web": preflight.get("web") or {},
        "roots": _root_summary(preflight.get("roots") or {}),
        "configured_adapters": _adapter_summary(preflight.get("configured_adapters") or {}),
        "path_mappings": _path_mapping_summary(preflight.get("path_mappings") or {}),
    }
    docker_available = shutil.which("docker") is not None
    return {
        "install_support_schema_version": inkdrop_public_contracts.INSTALL_SUPPORT_SCHEMA_VERSION,
        "preflight_schema_version": preflight.get("preflight_schema_version"),
        "release_check_schema_version": inkdrop_public_contracts.RELEASE_CHECK_SCHEMA_VERSION,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "docker_available": docker_available,
        "release_gate": _release_gate_summary(docker_available=docker_available),
        "install_defaults": _install_defaults(),
        "first_run_setup": inkdrop_state.first_run_setup_status(inkdrop_runtime_config.state_db_path()),
        "preflight": preflight_summary,
        "setup_guidance": _setup_guidance(preflight_summary, docker_available=docker_available),
    }


def print_text(payload):
    print(f"install_support_schema_version: {payload['install_support_schema_version']}")
    print(f"preflight_schema_version: {payload['preflight_schema_version']}")
    print(f"release_check_schema_version: {payload['release_check_schema_version']}")
    print(f"docker_available: {str(payload['docker_available']).lower()}")
    release_gate = payload.get("release_gate") or {}
    print(f"release_gate_status: {release_gate.get('status') or 'unknown'}")
    print(f"release_gate_command: {release_gate.get('required_command') or ''}")
    print(f"release_gate_next_step: {release_gate.get('next_step') or ''}")
    defaults = payload.get("install_defaults") or {}
    default_paths = defaults.get("paths") or {}
    print(f"install_default_config_dir: {default_paths.get('config_dir') or ''}")
    print(f"install_default_comic_root: {default_paths.get('comic_root') or ''}")
    print(f"install_default_optional_adapters: blank")
    first_run = payload.get("first_run_setup") or {}
    print(f"first_run_setup_complete: {str(bool(first_run.get('complete'))).lower()}")
    print(f"first_run_setup_first_action: {first_run.get('first_action') or ''}")
    preflight = payload["preflight"]
    print(f"preflight_ok: {str(preflight['ok']).lower()}")
    for error in preflight.get("errors") or []:
        print(f"error: {error}")
    warning_summary = preflight.get("warning_summary") or {}
    for key in ("optional_adapters_unconfigured", "runtime_tools_missing", "python_dependencies_missing"):
        values = warning_summary.get(key) or []
        print(f"{key}: {', '.join(values) if values else 'none'}")
    for step in payload.get("setup_guidance") or []:
        print(f"{step['severity']}: {step['area']}: {step['message']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print a redacted InkDrop install/support summary.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--create", action="store_true", help="Create missing runtime directories before checking.")
    parser.add_argument("--strict-dependencies", action="store_true", help="Fail preflight when required Python packages are missing.")
    parser.add_argument("--strict-runtime-tools", action="store_true", help="Fail preflight when archive/runtime tools are missing.")
    args = parser.parse_args(argv)

    payload = build_summary(
        create=args.create,
        strict_dependencies=args.strict_dependencies,
        strict_runtime_tools=args.strict_runtime_tools,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_text(payload)
    return 0 if payload["preflight"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
