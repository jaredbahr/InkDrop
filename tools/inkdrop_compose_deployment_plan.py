#!/usr/bin/env python3
"""Report or write a safe InkDrop overlay for an existing Docker Compose stack."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCHEMA_VERSION = 1
DEFAULT_SERVICE_NAME = "inkdrop"
OPTIONAL_ADAPTER_ENV = {
    "INKDROP_AUTH_REQUIRED": "${INKDROP_AUTH_REQUIRED:-0}",
    "INKDROP_EXTERNAL_AUTH_ENABLED": "${INKDROP_EXTERNAL_AUTH_ENABLED:-0}",
    "INKDROP_EXTERNAL_AUTH_HEADER": "${INKDROP_EXTERNAL_AUTH_HEADER:-X-Forwarded-User}",
    "INKDROP_COMICVINE_API_KEY": "${INKDROP_COMICVINE_API_KEY:-}",
    "INKDROP_PROWLARR_URL": "${INKDROP_PROWLARR_URL:-}",
    "INKDROP_PROWLARR_API_KEY": "${INKDROP_PROWLARR_API_KEY:-}",
    "INKDROP_SABNZBD_URL": "${INKDROP_SABNZBD_URL:-}",
    "INKDROP_SABNZBD_API_KEY": "${INKDROP_SABNZBD_API_KEY:-}",
    "INKDROP_QBITTORRENT_URL": "${INKDROP_QBITTORRENT_URL:-}",
    "INKDROP_QBITTORRENT_USERNAME": "${INKDROP_QBITTORRENT_USERNAME:-}",
    "INKDROP_QBITTORRENT_PASSWORD": "${INKDROP_QBITTORRENT_PASSWORD:-}",
    "INKDROP_SLSKD_API_BASE_URL": "${INKDROP_SLSKD_API_BASE_URL:-}",
    "INKDROP_SUWAYOMI_API_BASE_URL": "${INKDROP_SUWAYOMI_API_BASE_URL:-}",
    "INKDROP_KAVITA_URL": "${INKDROP_KAVITA_URL:-}",
    "INKDROP_KOMGA_URL": "${INKDROP_KOMGA_URL:-}",
    "INKDROP_KAPOWARR_URL": "${INKDROP_KAPOWARR_URL:-}",
    "INKDROP_SAB_PATH_MAPPINGS": "${INKDROP_SAB_PATH_MAPPINGS:-}",
    "INKDROP_UNC_PATH_MAPPINGS": "${INKDROP_UNC_PATH_MAPPINGS:-}",
}


def _load_yaml(path: Path):
    try:
        import yaml
    except Exception as exc:  # pragma: no cover - exercised on lean hosts.
        return None, f"PyYAML unavailable: {exc}"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}, ""
    except Exception as exc:
        return None, f"Could not parse Compose YAML: {exc}"


def _fallback_service_names(path: Path):
    services = []
    in_services = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("services:"):
            in_services = True
            continue
        if in_services and raw and not raw.startswith(" "):
            break
        if in_services and raw.startswith("  ") and not raw.startswith("    ") and raw.strip().endswith(":"):
            services.append(raw.strip()[:-1])
    return services


def inspect_compose(path: Path):
    warnings = []
    payload, parse_warning = _load_yaml(path)
    if parse_warning:
        warnings.append(parse_warning)
    services = {}
    networks = {}
    volumes = {}
    if isinstance(payload, dict):
        services = payload.get("services") if isinstance(payload.get("services"), dict) else {}
        networks = payload.get("networks") if isinstance(payload.get("networks"), dict) else {}
        volumes = payload.get("volumes") if isinstance(payload.get("volumes"), dict) else {}
    service_names = sorted(services.keys()) if services else sorted(_fallback_service_names(path))
    return {
        "path": str(path),
        "exists": path.exists(),
        "parse_ok": isinstance(payload, dict),
        "warnings": warnings,
        "service_names": service_names,
        "network_names": sorted(networks.keys()),
        "volume_names": sorted(volumes.keys()),
    }


def proposed_service_block(*, service_name: str, build_context: str, subdir: str):
    prefix = f"./{subdir.strip('/')}" if subdir.strip("/") else "."
    lines = [
        "services:",
        f"  {service_name}:",
        "    build:",
        f"      context: {build_context}",
        "    restart: unless-stopped",
        "    environment:",
        "      INKDROP_HOST: 0.0.0.0",
        "      INKDROP_PORT: ${INKDROP_PORT:-8796}",
        "      INKDROP_HOST_PORT: ${INKDROP_HOST_PORT:-${INKDROP_PORT:-8796}}",
        "      INKDROP_CONFIG_DIR: /config",
        "      INKDROP_STATE_DIR: /state",
        "      INKDROP_STAGING_DIR: /staging",
        "      INKDROP_MANUAL_INBOX_DIR: /manual-inbox",
        "      INKDROP_QUARANTINE_DIR: /state/quarantine",
        "      INKDROP_COMIC_ROOT: /library/comics",
        "      INKDROP_MANGA_ROOT: /library/manga",
    ]
    for key, value in OPTIONAL_ADAPTER_ENV.items():
        lines.append(f"      {key}: {value}")
    lines.extend(
        [
            "    ports:",
            "      - \"${INKDROP_HOST_PORT:-${INKDROP_PORT:-8796}}:${INKDROP_PORT:-8796}\"",
            "    volumes:",
            f"      - {prefix}/config:/config",
            f"      - {prefix}/state:/state",
            f"      - {prefix}/staging:/staging",
            f"      - {prefix}/manual-inbox:/manual-inbox",
            f"      - {prefix}/library:/library",
            "    healthcheck:",
            "      test: [\"CMD\", \"python\", \"-B\", \"inkdrop_container_healthcheck.py\", \"--timeout\", \"5\"]",
            "      interval: 60s",
            "      timeout: 10s",
            "      retries: 3",
            "      start_period: 30s",
        ]
    )
    return "\n".join(lines)


def build_report(args):
    compose_path = Path(args.compose_file).expanduser().resolve()
    if not compose_path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "dry_run": True,
            "writes_enabled": bool(args.output),
            "output_requested": bool(args.output),
            "output_path": str(Path(args.output).expanduser().resolve()) if args.output else "",
            "output_written": False,
            "compose": {"path": str(compose_path), "exists": False},
            "errors": ["Compose file does not exist."],
            "proposed_service_block": proposed_service_block(
                service_name=args.service_name,
                build_context=args.build_context,
                subdir=args.subdir,
            ),
        }
    compose = inspect_compose(compose_path)
    service_exists = args.service_name in set(compose.get("service_names") or [])
    errors = []
    warnings = list(compose.get("warnings") or [])
    if service_exists:
        warnings.append(f"Service {args.service_name!r} already exists; do not add a duplicate service.")
        if args.output:
            errors.append(f"Refusing to write overlay because service {args.service_name!r} already exists.")
    output_path = Path(args.output).expanduser().resolve() if args.output else None
    if output_path and output_path.exists() and not args.force:
        errors.append("Output file already exists; pass --force to overwrite it.")
    overlay_for_commands = str(output_path) if output_path else "<inkdrop-overlay.yaml>"
    compose_prefix = f"docker compose -f {compose_path} -f {overlay_for_commands}"
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "dry_run": not bool(args.output),
        "writes_enabled": bool(args.output),
        "output_requested": bool(args.output),
        "output_path": str(output_path) if output_path else "",
        "output_written": False,
        "compose": compose,
        "service_name": args.service_name,
        "service_exists": service_exists,
        "errors": errors,
        "warnings": warnings,
        "proposed_service_block": proposed_service_block(
            service_name=args.service_name,
            build_context=args.build_context,
            subdir=args.subdir,
        ),
        "required_preflight_commands": [
            f"{compose_prefix} config",
            f"{compose_prefix} run --rm {args.service_name} python -B inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools",
            f"{compose_prefix} run --rm {args.service_name} python -B inkdrop_container_healthcheck.py --preflight-only",
        ],
        "safety_notes": [
            "This helper never edits the base compose file; --output writes only a separate overlay.",
            "Use an isolated subdir such as inkdrop-trial for the first container test so /config and /state are not production mounts.",
            "Keep optional adapter URLs and secrets blank until each service is reachable from inside the InkDrop container.",
            "Back up the existing compose file before manually applying the proposed service block.",
            "Do not mount another application's config folder as InkDrop /config.",
            "Do not point the trial overlay at production media/state until container preflight and healthcheck pass.",
        ],
        "next_step": (
            "Write a separate overlay with --output, validate it with docker compose config, "
            "then run strict preflight against isolated InkDrop trial volumes before enabling automation or production mounts."
        ),
    }


def write_overlay_if_requested(args, report):
    if not args.output or not report.get("ok"):
        return report
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text((report.get("proposed_service_block") or "") + "\n", encoding="utf-8")
    report["output_written"] = True
    report["output_path"] = str(output_path)
    report["next_step"] = (
        f"Validate with: docker compose -f {report['compose']['path']} -f {output_path} config. "
        "If valid, start InkDrop and run strict preflight before enabling automation."
    )
    return report


def print_text(report):
    compose = report.get("compose") or {}
    print(f"compose: {compose.get('path')}")
    print(f"services: {', '.join(compose.get('service_names') or []) or '(none found)'}")
    print(f"inkdrop_service_exists: {str(report.get('service_exists')).lower()}")
    for warning in report.get("warnings") or []:
        print(f"warning: {warning}")
    for error in report.get("errors") or []:
        print(f"error: {error}")
    if report.get("output_requested"):
        print(f"output: {report.get('output_path')}")
        print(f"output_written: {str(report.get('output_written')).lower()}")
    print("")
    print(report.get("proposed_service_block") or "")
    print("")
    print("required_preflight_commands:")
    for command in report.get("required_preflight_commands") or []:
        print(f"- {command}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Dry-run or write an InkDrop overlay for an existing Docker Compose stack.")
    parser.add_argument("compose_file", help="Existing Docker Compose YAML file to inspect.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME, help="Service name to check/propose.")
    parser.add_argument("--build-context", default="${INKDROP_BUILD_CONTEXT:-./InkDrop}", help="Compose build context for the proposed service.")
    parser.add_argument("--subdir", default="inkdrop", help="Relative host subdirectory for InkDrop runtime mounts.")
    parser.add_argument("--output", default="", help="Write the proposed InkDrop service to this separate overlay file.")
    parser.add_argument("--force", action="store_true", help="Allow --output to overwrite an existing overlay file.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    report = build_report(args)
    report = write_overlay_if_requested(args, report)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_text(report)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
