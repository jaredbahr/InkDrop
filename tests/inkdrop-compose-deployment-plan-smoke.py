#!/usr/bin/env python3
"""Smoke checks for the report-only existing-stack deployment helper."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tools import inkdrop_compose_deployment_plan as plan


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    repo_root = Path(__file__).resolve().parents[1]
    install_doc = (repo_root / "docs" / "inkdrop" / "arr-stack-deployment-plan.md").read_text(encoding="utf-8")
    require(
        "docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml config" in install_doc,
        "existing-stack docs should validate the combined base+overlay compose config",
    )
    require(
        "docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml run --rm inkdrop python -B inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools" in install_doc,
        "existing-stack docs should run strict preflight through the combined compose stack",
    )
    require(
        "docker compose -f /path/to/compose.yaml -f inkdrop.override.yaml run --rm inkdrop python -B inkdrop_container_healthcheck.py --preflight-only" in install_doc,
        "existing-stack docs should run healthcheck preflight through the combined compose stack",
    )

    with tempfile.TemporaryDirectory(prefix="inkdrop-compose-plan-smoke-") as tmp:
        compose = Path(tmp) / "compose.yaml"
        original = "\n".join(
            [
                "services:",
                "  prowlarr:",
                "    image: lscr.io/linuxserver/prowlarr:latest",
                "  sabnzbd:",
                "    image: lscr.io/linuxserver/sabnzbd:latest",
                "networks:",
                "  arr:",
                "    driver: bridge",
                "",
            ]
        )
        compose.write_text(original, encoding="utf-8")
        args = plan.parse_args([str(compose), "--json"])
        report = plan.build_report(args)
        require(report["ok"] is True, "report should be ok for a valid compose file")
        require(report["dry_run"] is True, "report should be dry-run")
        require(report["writes_enabled"] is False, "helper must not enable writes")
        require(report["output_requested"] is False, "report should not request output by default")
        require(report["output_written"] is False, "report should not write output by default")
        require(report["service_exists"] is False, "fresh stack should not report inkdrop service")
        require(compose.read_text(encoding="utf-8") == original, "helper must not mutate the compose file")
        block = report["proposed_service_block"]
        require("container_name:" not in block, "proposed service block should not pin a global Docker container name")
        for needle in (
            "INKDROP_PROWLARR_URL: ${INKDROP_PROWLARR_URL:-}",
            "INKDROP_SABNZBD_URL: ${INKDROP_SABNZBD_URL:-}",
            "INKDROP_QBITTORRENT_URL: ${INKDROP_QBITTORRENT_URL:-}",
            "INKDROP_SLSKD_API_BASE_URL: ${INKDROP_SLSKD_API_BASE_URL:-}",
            "INKDROP_KAVITA_URL: ${INKDROP_KAVITA_URL:-}",
            "INKDROP_KOMGA_URL: ${INKDROP_KOMGA_URL:-}",
        ):
            require(needle in block, f"proposed service block should keep {needle} blank by default")
        require(
            any("docker compose -f " in command and " -f <inkdrop-overlay.yaml> config" in command for command in report["required_preflight_commands"]),
            "combined base+overlay config validation command should be reported",
        )
        require(
            any(
                " -f <inkdrop-overlay.yaml> run --rm inkdrop python -B inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools" in command
                for command in report["required_preflight_commands"]
            ),
            "strict preflight command should be reported for the combined compose stack",
        )
        require(
            any(
                " -f <inkdrop-overlay.yaml> run --rm inkdrop python -B inkdrop_container_healthcheck.py --preflight-only" in command
                for command in report["required_preflight_commands"]
            ),
            "healthcheck preflight command should be reported for the combined compose stack",
        )
        custom_args = plan.parse_args([str(compose), "--service-name", "inkdrop-test", "--json"])
        custom = plan.build_report(custom_args)
        require("  inkdrop-test:" in custom["proposed_service_block"], "custom service name should be reflected in service block")
        require(
            any(
                " -f <inkdrop-overlay.yaml> run --rm inkdrop-test python -B inkdrop_preflight.py --create --quiet --strict-dependencies --strict-runtime-tools" in command
                for command in custom["required_preflight_commands"]
            ),
            "custom service name should be reflected in preflight command",
        )
        require(
            all("run --rm inkdrop " not in command for command in custom["required_preflight_commands"]),
            "custom service name should not leave hardcoded inkdrop preflight commands",
        )

        overlay = Path(tmp) / "inkdrop.override.yaml"
        output_args = plan.parse_args([str(compose), "--output", str(overlay), "--json"])
        output_report = plan.build_report(output_args)
        output_report = plan.write_overlay_if_requested(output_args, output_report)
        require(output_report["ok"] is True, "overlay write report should be ok")
        require(output_report["dry_run"] is False, "overlay write should not be dry-run")
        require(output_report["writes_enabled"] is True, "overlay write should explicitly enable writes")
        require(output_report["output_written"] is True, "overlay file should be written")
        require(overlay.exists(), "overlay file should exist")
        overlay_text = overlay.read_text(encoding="utf-8")
        require("services:" in overlay_text and "  inkdrop:" in overlay_text, "overlay should contain an inkdrop service")
        require("container_name:" not in overlay_text, "overlay should not pin a global Docker container name")
        require("INKDROP_SABNZBD_URL: ${INKDROP_SABNZBD_URL:-}" in overlay_text, "overlay should keep optional adapter URLs blank")
        require(compose.read_text(encoding="utf-8") == original, "overlay write must not mutate the base compose file")

        blocked = plan.build_report(output_args)
        require(blocked["ok"] is False, "existing output file should fail without --force")
        require(any("already exists" in error for error in blocked["errors"]), "existing output error should explain --force")

        forced_args = plan.parse_args([str(compose), "--output", str(overlay), "--force", "--json"])
        forced = plan.build_report(forced_args)
        forced = plan.write_overlay_if_requested(forced_args, forced)
        require(forced["ok"] is True and forced["output_written"] is True, "--force should allow overlay rewrite")

        compose.write_text(
            "\n".join(
                [
                    "services:",
                    "  prowlarr:",
                    "    image: lscr.io/linuxserver/prowlarr:latest",
                    "  inkdrop:",
                    "    image: example/inkdrop:latest",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        existing = plan.build_report(args)
        require(existing["service_exists"] is True, "existing inkdrop service should be detected")
        require(any("already exists" in warning for warning in existing["warnings"]), "duplicate service warning should be reported")
        duplicate_overlay = Path(tmp) / "duplicate.override.yaml"
        duplicate_args = plan.parse_args([str(compose), "--output", str(duplicate_overlay), "--json"])
        duplicate = plan.build_report(duplicate_args)
        duplicate = plan.write_overlay_if_requested(duplicate_args, duplicate)
        require(duplicate["ok"] is False, "duplicate service should refuse overlay write")
        require(duplicate["output_written"] is False, "duplicate service should not write overlay")
        require(not duplicate_overlay.exists(), "duplicate service overlay should not be created")

    print("INKDROP_COMPOSE_DEPLOYMENT_PLAN_OK")


if __name__ == "__main__":
    main()
