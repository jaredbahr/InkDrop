#!/usr/bin/env python3
"""Run InkDrop public-release checks from one local command."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import inkdrop_public_contracts

RELEASE_BLOCKER_SCHEMA_VERSION = 1


def _path_variants(path):
    text = str(path)
    variants = {text, text.replace("\\", "/")}
    try:
        resolved = str(Path(path).resolve())
        variants.add(resolved)
        variants.add(resolved.replace("\\", "/"))
    except OSError:
        pass
    return variants


def redact_public_text(value):
    text = clean_output(value)
    if not text:
        return ""
    replacements = []
    for marker, label in (
        (ROOT, "<WORKSPACE>"),
        (Path.home(), "<HOME>"),
        (Path(tempfile.gettempdir()), "<TEMP>"),
    ):
        for variant in _path_variants(marker):
            if variant:
                replacements.append((variant, label))
    for marker, label in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(marker, label)
    return text


def display_command(command):
    rendered = []
    executable = str(sys.executable)
    executable_variants = _path_variants(executable)
    for index, part in enumerate(command):
        text = str(part)
        if index == 0 and text in executable_variants:
            rendered.append("python")
        else:
            rendered.append(redact_public_text(text))
    return rendered

LOCAL_CHECKS = (
    (
        "py_compile_public_release",
        120,
        [
            sys.executable,
            "-B",
            "-m",
            "py_compile",
            "inkdrop_backup_restore.py",
            "inkdrop-backup-restore-smoke.py",
            "inkdrop-settings-backup-restore-smoke.py",
            "inkdrop-settings-backup-browser-smoke.py",
            "inkdrop-autopilot-fairness-smoke.py",
            "inkdrop-automatic-search-recall-smoke.py",
            "inkdrop-acquisition-funnel-evidence-smoke.py",
            "inkdrop-series-autopilot-cron-lock-smoke.py",
            "tools/inkdrop_github_release.py",
            "inkdrop-github-release-contract-smoke.py",
            "inkdrop-standalone-entrypoints-smoke.py",
            "inkdrop_comicscodes_discovery.py",
            "inkdrop_preflight.py",
            "inkdrop_public_contracts.py",
            "inkdrop_runtime_config.py",
            "inkdrop_container_healthcheck.py",
            "inkdrop_sab_failed_cleanup.py",
            "inkdrop-sab-cleanup-optional-adapter-smoke.py",
            "inkdrop-auth-api-key-smoke.py",
            "inkdrop-auth-enforcement-smoke.py",
            "inkdrop-auth-security-smoke.py",
            "inkdrop-auth-security-closure-smoke.py",
            "inkdrop-auth-http-smoke.py",
            "inkdrop-auth-password-policy-smoke.py",
            "inkdrop-auth-add-series-http-smoke.py",
            "inkdrop-auth-route-inventory-smoke.py",
            "inkdrop-auth-state-preservation-smoke.py",
            "inkdrop-auth-ui-smoke.py",
            "inkdrop-ui-mutation-client-smoke.py",
            "inkdrop-activity-truth-smoke.py",
            "inkdrop-history-taxonomy-smoke.py",
            "inkdrop-reconcile-lock-observability-smoke.py",
            "inkdrop-issue-identity-smoke.py",
            "inkdrop-deferred-sync-smoke.py",
            "inkdrop-slskd-failover-smoke.py",
            "inkdrop-slskd-coverage-selection-smoke.py",
            "inkdrop-series-detail-performance-smoke.py",
            "inkdrop_auth_contracts.py",
            "inkdrop-series-activity-ui-smoke.py",
            "inkdrop-first-run-setup-status-smoke.py",
            "inkdrop-automatic-search-state-smoke.py",
            "inkdrop-closed-alpha-e2e-fixture.py",
            "inkdrop-settings-section-contract-smoke.py",
            "inkdrop-operator-contracts-smoke.py",
            "inkdrop-import-ready-worker-smoke.py",
            "inkdrop_incident_recovery.py",
            "inkdrop-dispatch-recovery-closure-smoke.py",
            "inkdrop-library-import-plan-smoke.py",
            "inkdrop-local-folder-only-flow-smoke.py",
            "inkdrop-manual-search-readiness-smoke.py",
            "inkdrop-manual-search-core-smoke.py",
            "inkdrop-manual-search-target-unit-smoke.py",
            "inkdrop-manual-search-executor-smoke.py",
            "inkdrop-manual-search-prowlarr-repair-smoke.py",
            "inkdrop-manual-search-slskd-smoke.py",
            "inkdrop-manual-search-migration-smoke.py",
            "inkdrop-manual-search-known-title-smoke.py",
            "inkdrop-manual-search-security-smoke.py",
            "inkdrop-manual-search-ui-smoke.py",
            "inkdrop_manual_search.py",
            "inkdrop_manual_search_core.py",
            "inkdrop_manual_search_executor.py",
            "inkdrop_manual_search_worker.py",
            "inkdrop-library-frontends-smoke.py",
            "inkdrop-komga-settings-smoke.py",
            "inkdrop-kapowarr-shutdown-readiness-smoke.py",
            "inkdrop-kapowarr-fallback-retirement-smoke.py",
            "inkdrop-planned-path-live-inspection.py",
            "inkdrop-planned-path-inspection-smoke.py",
            "inkdrop-series-compact-payload-smoke.py",
            "inkdrop-ui-state-endpoint-contract-smoke.py",
            "inkdrop-operational-ui-static-smoke.py",
            "inkdrop-queue-claim-smoke.py",
            "inkdrop-source-worker-scheduler-smoke.py",
            "inkdrop-source-worker-service-smoke.py",
            "inkdrop-source-worker-jobs-smoke.py",
            "inkdrop-singleton-search-coverage-smoke.py",
            "inkdrop-prowlarr-health-smoke.py",
            "inkdrop-provider-queue-filter-evidence-smoke.py",
            "inkdrop-direct-source-certification-smoke.py",
            "inkdrop-download-clients-round2-smoke.py",
            "inkdrop-utorrent-rtorrent-smoke.py",
            "inkdrop_download_client_config.py",
            "inkdrop_download_client_api.py",
            "inkdrop_secret_store.py",
            "inkdrop-download-client-instance-store-smoke.py",
            "inkdrop-download-client-instance-api-smoke.py",
            "inkdrop-download-client-routing-smoke.py",
            "inkdrop-download-client-secret-store-smoke.py",
            "inkdrop_download_client_ownership_smoke.py",
            "inkdrop-settings-registry-smoke.py",
            "inkdrop-public-security-smoke.py",
            "inkdrop-provider-test-contract-smoke.py",
            "inkdrop-public-docker-runtime-smoke.py",
            "inkdrop-public-repo-export-smoke.py",
            "inkdrop-public-release-safety-audit.py",
            "inkdrop-release-notes-version-smoke.py",
            "inkdrop-github-release-contract-smoke.py",
            "inkdrop-runtime-source-settings-smoke.py",
            "inkdrop-source-settings-contract-smoke.py",
            "inkdrop_manual_source_autoresolve.py",
            "inkdrop_internal_jobs.py",
            "inkdrop-manual-source-internal-import-auth-smoke.py",
            "inkdrop_mangadex_direct.py",
            "inkdrop_rss_discovery.py",
            "inkdrop_service_inventory.py",
            "tools/inkdrop_docker_context_manifest.py",
            "tools/inkdrop_install_support_summary.py",
            "tools/inkdrop_compose_deployment_plan.py",
            "tools/inkdrop_release_evidence_bundle.py",
            "tools/inkdrop_public_http_smoke.py",
            "tools/inkdrop_public_repo_export.py",
            "tools/inkdrop_settings_api_surface_audit.py",
            "tools/inkdrop_settings_sync_smoke.py",
            "tools/inkdrop_state_schema_audit.py",
            "tools/inkdrop_public_release_check.py",
            "tools/inkdrop_web_surface_audit.py",
        ],
    ),
    (
        "closed_alpha_rehearsal_shell_syntax",
        30,
        [
            "bash",
            "-n",
            "tools/inkdrop_closed_alpha_update_rehearsal.sh",
            "tools/inkdrop_suwayomi_fresh_volume_rehearsal.sh",
        ],
    ),
    ("backup_restore_smoke", 60, [sys.executable, "-B", "inkdrop-backup-restore-smoke.py"]),
    ("settings_backup_restore_smoke", 60, [sys.executable, "-B", "inkdrop-settings-backup-restore-smoke.py"]),
    ("standalone_entrypoints_smoke", 60, [sys.executable, "-B", "inkdrop-standalone-entrypoints-smoke.py"]),
    ("auth_api_key_smoke", 60, [sys.executable, "-B", "inkdrop-auth-api-key-smoke.py"]),
    ("auth_enforcement_smoke", 60, [sys.executable, "-B", "inkdrop-auth-enforcement-smoke.py"]),
    ("auth_security_smoke", 120, [sys.executable, "-B", "inkdrop-auth-security-smoke.py"]),
    ("auth_security_closure_smoke", 120, [sys.executable, "-B", "inkdrop-auth-security-closure-smoke.py"]),
    ("auth_http_smoke", 120, [sys.executable, "-B", "inkdrop-auth-http-smoke.py"]),
    ("auth_password_policy_smoke", 120, [sys.executable, "-B", "inkdrop-auth-password-policy-smoke.py"]),
    ("auth_add_series_http_smoke", 120, [sys.executable, "-B", "inkdrop-auth-add-series-http-smoke.py"]),
    ("auth_route_inventory_smoke", 120, [sys.executable, "-B", "inkdrop-auth-route-inventory-smoke.py"]),
    ("auth_state_preservation_smoke", 120, [sys.executable, "-B", "inkdrop-auth-state-preservation-smoke.py"]),
    ("auth_ui_smoke", 60, [sys.executable, "-B", "inkdrop-auth-ui-smoke.py"]),
    ("ui_mutation_client_smoke", 60, [sys.executable, "-B", "inkdrop-ui-mutation-client-smoke.py"]),
    ("activity_truth_smoke", 60, [sys.executable, "-B", "inkdrop-activity-truth-smoke.py"]),
    ("autopilot_fairness_smoke", 60, [sys.executable, "-B", "inkdrop-autopilot-fairness-smoke.py"]),
    ("automatic_search_recall_smoke", 60, [sys.executable, "-B", "inkdrop-automatic-search-recall-smoke.py"]),
    ("acquisition_funnel_evidence_smoke", 60, [sys.executable, "-B", "inkdrop-acquisition-funnel-evidence-smoke.py"]),
    ("series_autopilot_cron_lock_smoke", 60, [sys.executable, "-B", "inkdrop-series-autopilot-cron-lock-smoke.py"]),
    ("history_taxonomy_smoke", 60, [sys.executable, "-B", "inkdrop-history-taxonomy-smoke.py"]),
    ("reconcile_lock_observability_smoke", 60, [sys.executable, "-B", "inkdrop-reconcile-lock-observability-smoke.py"]),
    ("issue_identity_smoke", 60, [sys.executable, "-B", "inkdrop-issue-identity-smoke.py"]),
    ("deferred_sync_smoke", 60, [sys.executable, "-B", "inkdrop-deferred-sync-smoke.py"]),
    ("slskd_failover_smoke", 120, [sys.executable, "-B", "inkdrop-slskd-failover-smoke.py"]),
    ("slskd_series_run_handoff_smoke", 60, [sys.executable, "-B", "inkdrop-slskd-series-run-handoff-smoke.py"]),
    ("slskd_series_generalization_smoke", 60, [sys.executable, "-B", "inkdrop-slskd-series-generalization-smoke.py"]),
    ("slskd_coverage_selection_smoke", 60, [sys.executable, "-B", "inkdrop-slskd-coverage-selection-smoke.py"]),
    ("slskd_query_rotation_smoke", 60, [sys.executable, "-B", "inkdrop-slskd-query-rotation-smoke.py"]),
    ("series_detail_performance_smoke", 120, [sys.executable, "-B", "inkdrop-series-detail-performance-smoke.py"]),
    ("series_activity_ui_smoke", 60, [sys.executable, "-B", "inkdrop-series-activity-ui-smoke.py"]),
    ("first_run_setup_status_smoke", 60, [sys.executable, "-B", "inkdrop-first-run-setup-status-smoke.py"]),
    ("automatic_search_state_smoke", 60, [sys.executable, "-B", "inkdrop-automatic-search-state-smoke.py"]),
    ("closed_alpha_e2e_fixture", 120, [sys.executable, "-B", "inkdrop-closed-alpha-e2e-fixture.py"]),
    ("settings_section_contract_smoke", 60, [sys.executable, "-B", "inkdrop-settings-section-contract-smoke.py"]),
    ("operator_contracts_smoke", 60, [sys.executable, "-B", "inkdrop-operator-contracts-smoke.py"]),
    ("dispatch_recovery_closure_smoke", 60, [sys.executable, "-B", "inkdrop-dispatch-recovery-closure-smoke.py"]),
    ("qa_candidate_manifest_smoke", 60, [sys.executable, "-B", "inkdrop-qa-candidate-manifest-smoke.py"]),
    ("update_awareness_smoke", 60, [sys.executable, "-B", "inkdrop-update-awareness-smoke.py"]),
    ("comicvine_optional_scheduler_smoke", 60, [sys.executable, "-B", "inkdrop-comicvine-optional-scheduler-smoke.py"]),
    ("internal_jobs_auth_smoke", 60, [sys.executable, "-B", "inkdrop-internal-jobs-auth-smoke.py"]),
    ("manual_source_internal_import_auth_smoke", 60, [sys.executable, "-B", "inkdrop-manual-source-internal-import-auth-smoke.py"]),
    ("import_ready_worker_smoke", 120, [sys.executable, "-B", "inkdrop-import-ready-worker-smoke.py"]),
    ("library_import_plan_smoke", 60, [sys.executable, "-B", "inkdrop-library-import-plan-smoke.py"]),
    ("local_folder_only_flow_smoke", 60, [sys.executable, "-B", "inkdrop-local-folder-only-flow-smoke.py"]),
    ("library_frontends_smoke", 120, [sys.executable, "-B", "inkdrop-library-frontends-smoke.py"]),
    ("komga_settings_smoke", 120, [sys.executable, "-B", "inkdrop-komga-settings-smoke.py"]),
    ("kapowarr_shutdown_readiness_smoke", 120, [sys.executable, "-B", "inkdrop-kapowarr-shutdown-readiness-smoke.py"]),
    ("kapowarr_fallback_retirement_smoke", 120, [sys.executable, "-B", "inkdrop-kapowarr-fallback-retirement-smoke.py"]),
    ("planned_path_inspection_smoke", 60, [sys.executable, "-B", "inkdrop-planned-path-inspection-smoke.py"]),
    ("series_compact_payload_smoke", 60, [sys.executable, "-B", "inkdrop-series-compact-payload-smoke.py"]),
    ("ui_state_endpoint_contract_smoke", 60, [sys.executable, "-B", "inkdrop-ui-state-endpoint-contract-smoke.py"]),
    ("operational_ui_static_smoke", 60, [sys.executable, "-B", "inkdrop-operational-ui-static-smoke.py"]),
    ("queue_claim_smoke", 60, [sys.executable, "-B", "inkdrop-queue-claim-smoke.py"]),
    ("duplicate_live_task_audit_smoke", 60, [sys.executable, "-B", "inkdrop-duplicate-live-task-audit-smoke.py"]),
    ("source_worker_scheduler_smoke", 120, [sys.executable, "-B", "inkdrop-source-worker-scheduler-smoke.py"]),
    ("source_worker_service_smoke", 120, [sys.executable, "-B", "inkdrop-source-worker-service-smoke.py"]),
    ("source_worker_lane_lock_smoke", 30, ["bash", "inkdrop-source-worker-lane-lock-smoke.sh"]),
    ("source_worker_jobs_smoke", 120, [sys.executable, "-B", "inkdrop-source-worker-jobs-smoke.py"]),
    ("singleton_search_coverage_smoke", 60, [sys.executable, "-B", "inkdrop-singleton-search-coverage-smoke.py"]),
    ("prowlarr_health_smoke", 60, [sys.executable, "-B", "inkdrop-prowlarr-health-smoke.py"]),
    ("provider_queue_filter_evidence_smoke", 60, [sys.executable, "-B", "inkdrop-provider-queue-filter-evidence-smoke.py"]),
    ("direct_source_certification_smoke", 60, [sys.executable, "-B", "inkdrop-direct-source-certification-smoke.py"]),
    ("manual_search_readiness_smoke", 60, [sys.executable, "-B", "inkdrop-manual-search-readiness-smoke.py"]),
    ("manual_search_core_smoke", 60, [sys.executable, "-B", "inkdrop-manual-search-core-smoke.py"]),
    ("manual_search_target_unit_smoke", 60, [sys.executable, "-B", "inkdrop-manual-search-target-unit-smoke.py"]),
    ("manual_search_executor_smoke", 60, [sys.executable, "-B", "inkdrop-manual-search-executor-smoke.py"]),
    ("manual_search_prowlarr_repair_smoke", 60, [sys.executable, "-B", "inkdrop-manual-search-prowlarr-repair-smoke.py"]),
    ("manual_search_slskd_smoke", 60, [sys.executable, "-B", "inkdrop-manual-search-slskd-smoke.py"]),
    ("manual_search_migration_smoke", 60, [sys.executable, "-B", "inkdrop-manual-search-migration-smoke.py"]),
    ("manual_search_known_title_smoke", 60, [sys.executable, "-B", "inkdrop-manual-search-known-title-smoke.py"]),
    ("manual_search_security_smoke", 60, [sys.executable, "-B", "inkdrop-manual-search-security-smoke.py"]),
    ("manual_search_ui_static_smoke", 60, [sys.executable, "-B", "inkdrop-manual-search-ui-smoke.py"]),
    ("download_clients_round2_smoke", 120, [sys.executable, "-B", "inkdrop-download-clients-round2-smoke.py"]),
    ("utorrent_rtorrent_smoke", 120, [sys.executable, "-B", "inkdrop-utorrent-rtorrent-smoke.py"]),
    ("download_client_instance_store_smoke", 120, [sys.executable, "-B", "inkdrop-download-client-instance-store-smoke.py"]),
    ("download_client_instance_api_smoke", 180, [sys.executable, "-B", "inkdrop-download-client-instance-api-smoke.py"]),
    ("download_client_routing_smoke", 120, [sys.executable, "-B", "inkdrop-download-client-routing-smoke.py"]),
    ("download_client_secret_store_smoke", 120, [sys.executable, "-B", "inkdrop-download-client-secret-store-smoke.py"]),
    ("download_client_ownership_smoke", 120, [sys.executable, "-B", "inkdrop_download_client_ownership_smoke.py"]),
    ("settings_registry_smoke", 60, [sys.executable, "-B", "inkdrop-settings-registry-smoke.py"]),
    ("public_security_smoke", 60, [sys.executable, "-B", "inkdrop-public-security-smoke.py"]),
    ("sab_cleanup_optional_adapter_smoke", 60, [sys.executable, "-B", "inkdrop-sab-cleanup-optional-adapter-smoke.py"]),
    ("provider_test_contract_smoke", 60, [sys.executable, "-B", "inkdrop-provider-test-contract-smoke.py"]),
    ("public_docker_runtime_smoke", 600, [sys.executable, "-B", "inkdrop-public-docker-runtime-smoke.py"]),
    ("public_repo_export_smoke", 60, [sys.executable, "-B", "inkdrop-public-repo-export-smoke.py"]),
    ("compose_deployment_plan_smoke", 60, [sys.executable, "-B", "inkdrop-compose-deployment-plan-smoke.py"]),
    ("public_release_safety_audit", 120, [sys.executable, "-B", "inkdrop-public-release-safety-audit.py"]),
    ("release_notes_version_smoke", 60, [sys.executable, "-B", "inkdrop-release-notes-version-smoke.py"]),
    ("github_release_contract_smoke", 60, [sys.executable, "-B", "inkdrop-github-release-contract-smoke.py"]),
    ("closed_alpha_packet_smoke", 60, [sys.executable, "-B", "inkdrop-closed-alpha-packet-smoke.py"]),
    ("runtime_source_settings_smoke", 120, [sys.executable, "-B", "inkdrop-runtime-source-settings-smoke.py"]),
    ("source_settings_contract_smoke", 120, [sys.executable, "-B", "inkdrop-source-settings-contract-smoke.py"]),
    ("portability_export_smoke", 60, [sys.executable, "-B", "inkdrop-portability-export-smoke.py"]),
    ("notifications_wiring_smoke", 60, [sys.executable, "-B", "inkdrop-notifications-wiring-smoke.py"]),
    ("issue_monitor_toggle_smoke", 60, [sys.executable, "-B", "inkdrop-issue-monitor-toggle-smoke.py"]),
    ("slskd_absolute_edition_gate_smoke", 60, [sys.executable, "-B", "inkdrop-slskd-absolute-edition-gate-smoke.py"]),
    ("slskd_collected_edition_year_conflict_smoke", 60, [sys.executable, "-B", "inkdrop-slskd-collected-edition-year-conflict-smoke.py"]),
    ("slskd_wait_seconds_floor_smoke", 60, [sys.executable, "-B", "inkdrop-slskd-wait-seconds-floor-smoke.py"]),
    ("settings_parity_batch_smoke", 60, [sys.executable, "-B", "inkdrop-settings-parity-batch-smoke.py"]),
    ("library_adoption_smoke", 60, [sys.executable, "-B", "inkdrop-library-adoption-smoke.py"]),
    ("archive_conversion_smoke", 60, [sys.executable, "-B", "inkdrop-archive-conversion-smoke.py"]),
    ("archive_conversion_wiring_smoke", 60, [sys.executable, "-B", "inkdrop-archive-conversion-wiring-smoke.py"]),
    ("settings_cold_open_smoke", 60, ["node", "inkdrop-settings-cold-open-smoke.js"]),
    ("settings_notifications_group_smoke", 60, ["node", "inkdrop-settings-notifications-group-smoke.js"]),
    ("docker_context_manifest", 60, [sys.executable, "-B", "tools/inkdrop_docker_context_manifest.py", "--warnings-json"]),
    ("settings_api_surface_audit", 60, [sys.executable, "-B", "tools/inkdrop_settings_api_surface_audit.py"]),
    ("settings_sync_smoke", 60, [sys.executable, "-B", "tools/inkdrop_settings_sync_smoke.py"]),
    ("state_schema_audit", 60, [sys.executable, "-B", "tools/inkdrop_state_schema_audit.py"]),
    ("web_surface_audit", 60, [sys.executable, "-B", "tools/inkdrop_web_surface_audit.py"]),
)

DOCKER_CHECKS = (
    ("docker_compose_config", 60, ["docker", "compose", "config", "--quiet"]),
    ("docker_compose_build", 1800, ["docker", "compose", "build", "inkdrop"]),
    (
        "docker_container_strict_preflight",
        180,
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "compose.release-gate.network-none.yml",
            "run",
            "--rm",
            "--no-deps",
            "inkdrop",
            "python",
            "-B",
            "inkdrop_preflight.py",
            "--create",
            "--quiet",
            "--strict-dependencies",
            "--strict-runtime-tools",
        ],
    ),
    (
        "docker_container_healthcheck_preflight",
        180,
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "compose.release-gate.network-none.yml",
            "run",
            "--rm",
            "--no-deps",
            "inkdrop",
            "python",
            "-B",
            "inkdrop_container_healthcheck.py",
            "--preflight-only",
        ],
    ),
)


def docker_checks(*, skip_build=False):
    checks = []
    for name, timeout, command in DOCKER_CHECKS:
        if skip_build and name == "docker_compose_build":
            checks.append(
                (
                    name,
                    timeout,
                    command,
                    {
                        "name": name,
                        "command": display_command(command),
                        "ok": True,
                        "returncode": 0,
                        "timed_out": False,
                        "timeout_seconds": timeout,
                        "stdout": "skipped by --skip-docker-build; caller must have already built the inkdrop image",
                        "stderr": "",
                        "skipped": True,
                    },
                )
            )
        else:
            checks.append((name, timeout, command, None))
    return tuple(checks)


def clean_output(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def should_print_stdout(item):
    stdout = str(item.get("stdout") or "").strip()
    if not stdout:
        return False
    if not item.get("ok"):
        return True
    if item.get("skipped"):
        return True
    if stdout.startswith("{") or stdout.startswith("["):
        return False
    return len(stdout.splitlines()) <= 6


def run_command(name, command, *, env=None, timeout=None):
    try:
        started = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "name": name,
            "command": display_command(command),
            "returncode": started.returncode,
            "ok": started.returncode == 0,
            "timed_out": False,
            "timeout_seconds": timeout,
            "stdout": redact_public_text(started.stdout),
            "stderr": redact_public_text(started.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        stderr = clean_output(exc.stderr)
        timeout_message = f"Timed out after {timeout} seconds."
        if stderr:
            stderr = f"{stderr}\n{timeout_message}"
        else:
            stderr = timeout_message
        return {
            "name": name,
            "command": display_command(command),
            "returncode": 124,
            "ok": False,
            "timed_out": True,
            "timeout_seconds": timeout,
            "stdout": redact_public_text(exc.stdout),
            "stderr": redact_public_text(stderr),
        }


def host_preflight_env(root):
    env = dict(os.environ)
    env.update(
        {
            "INKDROP_CONFIG_DIR": str(root / "config"),
            "INKDROP_STATE_DIR": str(root / "state"),
            "INKDROP_LOG_DIR": str(root / "logs"),
            "INKDROP_CACHE_DIR": str(root / "cache"),
            "INKDROP_BACKUP_DIR": str(root / "backups"),
            "INKDROP_STAGING_DIR": str(root / "staging"),
            "INKDROP_MANUAL_INBOX_DIR": str(root / "manual-inbox"),
            "INKDROP_QUARANTINE_DIR": str(root / "quarantine"),
        }
    )
    return env


def run_host_preflight(*, strict_dependencies=False):
    with tempfile.TemporaryDirectory(prefix="inkdrop-public-release-preflight-") as tmp:
        env = host_preflight_env(Path(tmp))
        command = [sys.executable, "-B", "inkdrop_preflight.py", "--create", "--quiet"]
        if strict_dependencies:
            command.append("--strict-dependencies")
        return run_command(
            "strict_host_preflight" if strict_dependencies else "host_preflight",
            command,
            env=env,
            timeout=60,
        )


def run_install_support_summary():
    with tempfile.TemporaryDirectory(prefix="inkdrop-public-release-support-") as tmp:
        env = host_preflight_env(Path(tmp))
        return run_command(
            "install_support_summary",
            [sys.executable, "-B", "tools/inkdrop_install_support_summary.py", "--create", "--json"],
            env=env,
            timeout=60,
        )


def run_public_http_smoke():
    with tempfile.TemporaryDirectory(prefix="inkdrop-public-release-http-") as tmp:
        env = host_preflight_env(Path(tmp))
        return run_command(
            "public_http_smoke",
            [sys.executable, "-B", "tools/inkdrop_public_http_smoke.py", "--json"],
            env=env,
            timeout=90,
        )


def result_by_name(results):
    return {str(item.get("name") or ""): item for item in results}


def release_blocker_item(blocker_id, label, *, status, ok, detail="", result_name=None, required=True):
    return {
        "id": blocker_id,
        "label": label,
        "status": status,
        "ok": ok,
        "required": bool(required),
        "result_name": result_name,
        "detail": detail,
    }


def release_blocker_from_result(blocker_id, label, result_name, results_by_name, *, missing_detail="", skipped_ok=False):
    result = results_by_name.get(result_name)
    if not result:
        return release_blocker_item(
            blocker_id,
            label,
            status="missing",
            ok=False,
            detail=missing_detail or f"{result_name} did not run.",
            result_name=result_name,
        )
    if result.get("ok"):
        if result.get("skipped"):
            return release_blocker_item(
                blocker_id,
                label,
                status="skipped",
                ok=bool(skipped_ok),
                detail=result.get("stdout") or f"{result_name} was skipped.",
                result_name=result_name,
            )
        return release_blocker_item(
            blocker_id,
            label,
            status="passed",
            ok=True,
            detail=f"{result_name} passed.",
            result_name=result_name,
        )
    if result.get("timed_out"):
        status = "timed_out"
    else:
        status = "failed"
    return release_blocker_item(
        blocker_id,
        label,
        status=status,
        ok=False,
        detail=result.get("stderr") or result.get("stdout") or f"{result_name} failed.",
        result_name=result_name,
    )


def build_release_blockers(*, results, include_docker, docker_available, docker_gate_status, release_ready, docker_only=False):
    results_by_name = result_by_name(results)
    local_names = [name for name, _timeout, _command in LOCAL_CHECKS]
    local_names.extend(["host_preflight", "strict_host_preflight", "install_support_summary", "public_http_smoke"])
    local_results = [results_by_name[name] for name in local_names if name in results_by_name]
    local_ok = bool(local_results) and all(item.get("ok") for item in local_results)
    if docker_only:
        local_ok = True
        local_status = "provided_separately"
        local_detail = "Docker-only mode assumes Docker-free release checks were already run from the same revision."
    else:
        local_status = "passed" if local_ok else "failed"
        local_detail = "All requested host/static release checks passed." if local_ok else "One or more host/static release checks failed."

    items = [
        release_blocker_item(
            "local_release_checks",
            "Docker-free release checks",
            status=local_status,
            ok=local_ok,
            detail=local_detail,
        )
    ]
    if docker_only:
        for blocker_id, label in (
            ("public_release_safety_audit", "Public safety audit"),
            ("install_support_summary", "Redacted install support summary"),
            ("docker_context_manifest", "Docker context manifest"),
        ):
            items.append(
                release_blocker_item(
                    blocker_id,
                    label,
                    status="provided_separately",
                    ok=True,
                    detail="Docker-only mode assumes this Docker-free check passed from the same revision.",
                )
            )
    else:
        items.extend(
            [
                release_blocker_from_result(
                    "public_release_safety_audit",
                    "Public safety audit",
                    "public_release_safety_audit",
                    results_by_name,
                ),
                release_blocker_from_result(
                    "install_support_summary",
                    "Redacted install support summary",
                    "install_support_summary",
                    results_by_name,
                ),
                release_blocker_from_result(
                    "docker_context_manifest",
                    "Docker context manifest",
                    "docker_context_manifest",
                    results_by_name,
                ),
            ]
        )

    if include_docker:
        items.append(
            release_blocker_item(
                "docker_available",
                "Docker CLI available",
                status="passed" if docker_available else "unavailable",
                ok=bool(docker_available),
                detail="Docker CLI was found." if docker_available else "Docker CLI is not available; Compose build/container checks cannot run here.",
            )
        )
    else:
        items.append(
            release_blocker_item(
                "docker_checks_requested",
                "Docker Compose release gate requested",
                status="not_requested",
                ok=False,
                detail="Run with --docker --require-docker on a Docker-capable host before a broader release.",
            )
        )

    items.extend(
        [
            release_blocker_from_result(
                "docker_compose_config",
                "Docker Compose config validates",
                "docker_compose_config",
                results_by_name,
                missing_detail="Docker Compose config check did not run.",
            ),
            release_blocker_from_result(
                "docker_compose_build",
                "Docker image builds",
                "docker_compose_build",
                results_by_name,
                missing_detail="Docker image build did not run.",
                skipped_ok=True,
            ),
            release_blocker_from_result(
                "docker_container_strict_preflight",
                "Strict container preflight",
                "docker_container_strict_preflight",
                results_by_name,
                missing_detail="Strict container preflight did not run.",
            ),
            release_blocker_from_result(
                "docker_container_healthcheck_preflight",
                "Container healthcheck preflight",
                "docker_container_healthcheck_preflight",
                results_by_name,
                missing_detail="Container healthcheck preflight did not run.",
            ),
        ]
    )
    required_items = [item for item in items if item.get("required")]
    return {
        "schema_version": RELEASE_BLOCKER_SCHEMA_VERSION,
        "ready": bool(release_ready and all(item.get("ok") is True for item in required_items)),
        "status": "passed" if release_ready else docker_gate_status,
        "items": items,
    }


def release_status_summary(*, ok, release_ready, docker_gate_status, release_blockers):
    blockers = release_blockers if isinstance(release_blockers, dict) else {}
    local_release_item = next(
        (
            item
            for item in (blockers.get("items") or [])
            if isinstance(item, dict) and item.get("id") == "local_release_checks"
        ),
        {},
    )
    local_checks_ok = bool(local_release_item.get("ok"))
    blocking_items = [
        item
        for item in (blockers.get("items") or [])
        if isinstance(item, dict) and item.get("required") and item.get("ok") is not True
    ]
    if release_ready:
        status = "ready"
        next_action = "Broader-release checks passed."
    elif not local_checks_ok:
        status = "local_checks_failed"
        next_action = "Fix failed Docker-free release checks before running Docker release gates."
    elif docker_gate_status == "not_requested":
        status = "docker_not_requested"
        next_action = "Run python -B tools/inkdrop_public_release_check.py --docker --require-docker on a Docker-capable host."
    elif docker_gate_status == "unavailable":
        status = "docker_unavailable"
        next_action = "Install/enable Docker with Compose v2, then rerun with --docker --require-docker."
    else:
        status = "docker_gate_failed"
        next_action = "Fix the failed Docker Compose build/container preflight check and rerun the release gate."
    return {
        "status": status,
        "ready": bool(release_ready),
        "local_checks_ok": local_checks_ok,
        "command_ok": bool(ok),
        "docker_gate_status": docker_gate_status,
        "blocking_count": len(blocking_items),
        "blocking_items": [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "detail": item.get("detail"),
            }
            for item in blocking_items
        ],
        "next_action": next_action,
    }


def run_checks(*, include_docker=False, require_docker=False, strict_host_dependencies=False, skip_docker_build=False, docker_only=False):
    results = []
    if not docker_only:
        for name, timeout, command in LOCAL_CHECKS:
            if name == "public_docker_runtime_smoke" and str(os.environ.get("INKDROP_RELEASE_CHECK_SELFTEST_SKIP_RUNTIME_SMOKE") or "").strip().lower() in {"1", "true", "yes", "on"}:
                results.append(
                    {
                        "name": name,
                        "command": display_command(command),
                        "ok": True,
                        "returncode": 0,
                        "timed_out": False,
                        "timeout_seconds": timeout,
                        "stdout": "skipped by INKDROP_RELEASE_CHECK_SELFTEST_SKIP_RUNTIME_SMOKE",
                        "stderr": "",
                        "skipped": True,
                    }
                )
                continue
            results.append(run_command(name, command, timeout=timeout))
        results.append(run_host_preflight(strict_dependencies=strict_host_dependencies))
        results.append(run_install_support_summary())
        if strict_host_dependencies:
            results.append(run_public_http_smoke())

    docker_available = shutil.which("docker") is not None
    if include_docker:
        if docker_available:
            for name, timeout, command, skipped_result in docker_checks(skip_build=skip_docker_build):
                if skipped_result is not None:
                    results.append(skipped_result)
                else:
                    results.append(run_command(name, command, timeout=timeout))
        else:
            results.append(
                {
                    "name": "docker_checks",
                    "command": display_command(["docker"]),
                    "ok": not require_docker,
                    "timed_out": False,
                    "timeout_seconds": None,
                    "skipped": not require_docker,
                    "returncode": 1 if require_docker else 0,
                    "stdout": "",
                    "stderr": "Docker CLI is not available; Docker Compose build/preflight checks were not run.",
                }
            )

    ok = all(item.get("ok") for item in results)
    docker_results = [item for item in results if str(item.get("name") or "").startswith("docker")]
    if not include_docker:
        docker_gate_status = "not_requested"
    elif not docker_available:
        docker_gate_status = "unavailable"
    elif all(item.get("ok") for item in docker_results):
        docker_gate_status = "passed"
    else:
        docker_gate_status = "failed"

    release_ready = bool(ok and docker_gate_status == "passed")
    release_blockers = build_release_blockers(
        results=results,
        include_docker=include_docker,
        docker_available=docker_available,
        docker_gate_status=docker_gate_status,
        release_ready=release_ready,
        docker_only=docker_only,
    )
    release_status = release_status_summary(
        ok=ok,
        release_ready=release_ready,
        docker_gate_status=docker_gate_status,
        release_blockers=release_blockers,
    )
    return {
        "release_check_schema_version": inkdrop_public_contracts.RELEASE_CHECK_SCHEMA_VERSION,
        "release_blocker_schema_version": RELEASE_BLOCKER_SCHEMA_VERSION,
        "ok": ok,
        "release_ready": release_ready,
        "release_status": release_status,
        "release_next_action": release_status["next_action"],
        "release_blockers": release_blockers,
        "docker_gate_status": docker_gate_status,
        "docker_available": docker_available,
        "docker_requested": include_docker,
        "docker_required": require_docker,
        "docker_only": bool(docker_only),
        "docker_build_skipped": bool(include_docker and skip_docker_build),
        "results": results,
    }


def print_text(payload):
    for item in payload["results"]:
        status = "ok" if item.get("ok") else "failed"
        if item.get("skipped"):
            status = "skipped"
        print(f"{status}: {item['name']}")
        if should_print_stdout(item):
            print(item["stdout"])
        if item.get("stderr") and not item.get("ok"):
            print(item["stderr"], file=sys.stderr)
    if not payload["docker_requested"]:
        print("note: Docker checks were not requested. Use --docker to include Compose build/preflight gates.")
    elif payload["docker_requested"] and not payload["docker_available"]:
        print("note: Docker CLI is unavailable, so Docker checks were skipped.", file=sys.stderr)
    if payload.get("release_ready"):
        print("release gate: passed")
    else:
        print(f"release gate: incomplete ({payload.get('docker_gate_status')}); Docker Compose build and container preflight must pass before release.")
    blockers = payload.get("release_blockers") or {}
    blocking_items = [item for item in blockers.get("items") or [] if item.get("required") and item.get("ok") is not True]
    if blocking_items:
        print("release blockers:")
        for item in blocking_items:
            print(f"- {item.get('status')}: {item.get('id')} - {item.get('detail')}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run InkDrop public-release checks.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--docker", action="store_true", help="Also run Docker Compose config/build/container preflight checks.")
    parser.add_argument("--docker-only", action="store_true", help="Run only Docker Compose config/build/container preflight checks; implies --docker --require-docker and assumes Docker-free checks were already run from the same revision.")
    parser.add_argument("--require-docker", action="store_true", help="Fail if Docker checks are requested but Docker is unavailable.")
    parser.add_argument("--skip-docker-build", action="store_true", help="Reuse an already-built InkDrop image; still run Compose config and strict container preflight.")
    parser.add_argument("--strict-host-dependencies", action="store_true", help="Fail host preflight if requirements.txt packages are not installed locally.")
    args = parser.parse_args(argv)

    include_docker = bool(args.docker or args.require_docker or args.docker_only)
    require_docker = bool(args.require_docker or args.docker_only)
    payload = run_checks(
        include_docker=include_docker,
        require_docker=require_docker,
        strict_host_dependencies=args.strict_host_dependencies,
        skip_docker_build=args.skip_docker_build,
        docker_only=args.docker_only,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_text(payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
