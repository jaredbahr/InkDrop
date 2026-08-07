#!/usr/bin/env python3
"""InkDrop service inventory for the standalone foundation sprint.

This is a small, explicit bridge between the current script entrypoints and the
target Arr-style service layout. It is intentionally static for now: packaging
can consume it later, while today's workers keep running unchanged.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


SERVICE_INVENTORY = [
    {
        "service_id": "inkdrop_state_core",
        "target_role": "shared_state",
        "label": "InkDrop State Core",
        "entrypoints": [
            "core/inkdrop_state.py",
            "core/inkdrop_language.py",
            "core/inkdrop_sources.py",
            "core/inkdrop_source_catalog.py",
            "core/inkdrop_source_registry.py",
            "core/inkdrop_source_providers.py",
            "core/inkdrop_source_suppression.py",
        ],
        "owns": [
            "series, issues/volumes/chapters, wanted rows, queue rows",
            "source attempts, download tasks, import results, history/activity",
            "provider registry, source memory, language and quality evidence",
        ],
        "external_adapters": [],
        "state_contract": "All worker/service slices report durable evidence into InkDrop-owned SQLite tables before the UI explains work state.",
        "smokes": [
            "inkdrop-completion-truth-smoke.py",
            "inkdrop-source-attempt-contract-smoke.py",
            "inkdrop-source-registry-smoke.py",
        ],
        "next_step": "Continue moving legacy Kavita/Kapowarr-named storage fields toward adapter-neutral names after active workers are safe.",
    },
    {
        "service_id": "inkdrop_web",
        "target_role": "web_ui",
        "label": "InkDrop Web UI",
        "entrypoints": [
            "core/inkdrop_web.py",
        ],
        "owns": [
            "Arr-style sections: Series, Wanted, Queue, Activity, History, Manual Review, Settings, System",
            "provider/app settings surface",
            "status/action routing and operator diagnostics",
        ],
        "external_adapters": [
            "ComicVine",
            "MangaDex",
            "Kavita",
            "Komga",
            "Prowlarr",
            "SLSKD",
            "SABnzbd",
            "qBittorrent",
            "temporary Kapowarr adapter",
        ],
        "state_contract": "The UI reads InkDrop state and provider health; it must not require Kapowarr as the source of truth for native rows.",
        "smokes": [
            "inkdrop-arr-shell-smoke.py",
            "inkdrop-history-route-regression.ps1",
            "inkdrop-media-management-settings-smoke.py",
            "inkdrop-komga-settings-smoke.py",
        ],
        "next_step": "Split the large inkdrop_web.py implementation into focused route/view modules while preserving API contracts.",
    },
    {
        "service_id": "inkdrop_worker",
        "target_role": "scheduler_and_queue_worker",
        "label": "InkDrop Queue Worker",
        "entrypoints": [
            "core/inkdrop_series_autopilot.py",
            "core/inkdrop_container_scheduler.py",
            "core/inkdrop_container_healthcheck.py",
            "inkdrop-source-worker.sh",
            "inkdrop-source-worker-service.py",
            "core/inkdrop_source_worker_service.py",
            "core/inkdrop_source_worker_cli.py",
            "core/inkdrop_source_worker_scheduler.py",
            "core/inkdrop_source_worker_runtime.py",
            "core/inkdrop_source_worker_jobs.py",
            "core/inkdrop_source_worker_batch.py",
            "core/inkdrop_source_worker_recorder.py",
            "core/inkdrop_source_worker_coordinator.py",
            "core/inkdrop_source_worker_plan.py",
        ],
        "owns": [
            "bounded concurrent recurring jobs, heartbeat, failure backoff, and scheduler health",
            "queue selection, provider scheduling, runtime budgets, cooldowns",
            "source-worker planning/execution/write gates",
            "handoff evidence for downloader-visible jobs",
        ],
        "external_adapters": [
            "Prowlarr/Newznab/Torznab",
            "SLSKD",
            "RSS/direct sources",
            "MangaDex",
            "Suwayomi/page-pack helpers",
            "qBittorrent",
            "SABnzbd",
        ],
        "state_contract": "Every provider attempt and downloader handoff records InkDrop source_attempt/download_task evidence, even when it fails or waits.",
        "smokes": [
            "inkdrop-container-worker-health-smoke.py",
            "inkdrop-source-worker-service-smoke.py",
            "inkdrop-source-worker-scheduler-smoke.py",
            "inkdrop-source-worker-jobs-smoke.py",
            "inkdrop-series-autopilot-sync-cadence-smoke.py",
            "inkdrop-autopilot-scheduler-smoke.py",
        ],
        "next_step": "Expose the worker scheduler health payload through the System UI.",
    },
    {
        "service_id": "inkdrop_sources",
        "target_role": "source_provider_adapters",
        "label": "InkDrop Source Providers",
        "entrypoints": [
            "core/inkdrop_source_worker_adapters.py",
            "core/inkdrop_source_worker_http.py",
            "core/inkdrop_slskd_source_probe.py",
            "core/inkdrop_missing_acquire.py",
            "core/inkdrop_rss_discovery.py",
            "core/inkdrop_comicscodes_discovery.py",
            "core/inkdrop_mangadex_direct.py",
            "core/inkdrop_manual_source_autoresolve.py",
            "core/inkdrop_direct_downloader.py",
            "core/inkdrop_page_pack_downloader.py",
        ],
        "owns": [
            "search/query planning, result scoring, manifest/detail evidence",
            "strict candidate verdicts and source-memory suppression",
            "source-specific provider health and retry reasons",
        ],
        "external_adapters": [
            "SLSKD/Soulseek",
            "Prowlarr and configured indexers",
            "MangaDex",
            "GetComics/RSS-style direct feeds",
            "ComicsCodes",
            "Suwayomi/page packs",
        ],
        "state_contract": "Sources return InkDrop-structured candidates or structured no-candidate/provider-wait evidence; Manual Review is reserved for real ambiguity.",
        "smokes": [
            "inkdrop-source-worker-adapter-smoke.py",
            "inkdrop-source-provider-health-smoke.py",
            "inkdrop-source-memory-smoke.py",
            "inkdrop-slskd-provider-unavailable-smoke.py",
            "inkdrop-pack-manifest-smoke.py",
        ],
        "next_step": "No open items -- the Kavita compatibility aliases were removed 2026-07-27.",
    },
    {
        "service_id": "inkdrop_download_clients",
        "target_role": "download_client_adapters",
        "label": "InkDrop Download Clients",
        "entrypoints": [
            "core/inkdrop_reconcile_imports.py",
            "inkdrop-import-ready-worker.sh",
            "core/inkdrop_sab_failed_cleanup.py",
        ],
        "owns": [
            "qBittorrent/SAB completed-client reconciliation",
            "completed/incomplete path evidence",
            "SAB failure learning and stale handoff cleanup",
        ],
        "external_adapters": [
            "qBittorrent",
            "SABnzbd",
        ],
        "state_contract": "Download clients expose InkDrop task ids, save paths, completion state, and incomplete-file evidence before importers touch files.",
        "smokes": [
            "inkdrop-download-client-reconcile-smoke.py",
            "inkdrop-indexer-handoff-retry-smoke.py",
            "inkdrop-import-ready-worker-smoke.py",
        ],
        "next_step": "No open items -- the Kavita compatibility alias was removed 2026-07-27.",
    },
    {
        "service_id": "inkdrop_importer",
        "target_role": "folder_importer",
        "label": "InkDrop Importer",
        "entrypoints": [
            "core/inkdrop_completed_import.py",
            "core/inkdrop_pack_import.py",
        ],
        "owns": [
            "archive/file validation, weak filename guards, pack fanout",
            "Media Management planned path preview/apply boundary",
            "folder completion import_results and library scan requests",
        ],
        "external_adapters": [
            "Kavita",
            "Komga",
            "temporary Kapowarr volume/id adapter",
        ],
        "state_contract": "A validated managed-folder file is InkDrop completion truth; Kavita/Komga visibility is adapter evidence unless explicitly required.",
        "smokes": [
            "inkdrop-pack-native-import-smoke.py",
            "inkdrop-pack-review-reconcile-smoke.py",
            "inkdrop-filename-safety-smoke.py",
            "inkdrop-folder-completion-backfill-smoke.py",
        ],
        "next_step": "No open items -- the Kavita compatibility aliases were removed 2026-07-27.",
    },
    {
        "service_id": "inkdrop_library_adapters",
        "target_role": "library_visibility_adapters",
        "label": "InkDrop Library Adapters",
        "entrypoints": [
            "core/inkdrop_library_frontends.py",
            "core/inkdrop_acquire.py",
            "core/inkdrop_completed_import.py",
            "core/inkdrop_reconcile_imports.py",
        ],
        "owns": [
            "Kavita scan/visibility checks",
            "Komga scan/visibility checks",
            "temporary Kapowarr library/metadata bridge behavior",
        ],
        "external_adapters": [
            "Kavita",
            "Komga",
            "Kapowarr as temporary adapter",
        ],
        "state_contract": "Library adapters can prove visibility but cannot become the source of truth for native InkDrop completion.",
        "smokes": [
            "inkdrop-komga-settings-smoke.py",
            "inkdrop-completion-truth-smoke.py",
            "inkdrop-add-series-path-smoke.py",
        ],
        "next_step": "Rename legacy Kavita-only statuses only after active workers can write adapter-neutral library-visible states safely.",
    },
    {
        "service_id": "inkdrop_diagnostics",
        "target_role": "diagnostics_and_operations",
        "label": "InkDrop Diagnostics",
        "entrypoints": [
            "inkdrop-queue-throughput-audit.py",
            "inkdrop-backup-retention-audit.py",
            "inkdrop-completion-identity-audit.py",
            "inkdrop-agent-lane-audit.py",
            "inkdrop-config-drift-audit.py",
            "inkdrop-public-readiness-audit.py",
            "inkdrop-ui-polish-drift-audit.py",
            "inkdrop-ui-safety-contract-audit.py",
            "inkdrop-comicvine-issue-date-repair.py",
            "slskd_recovery_watchdog.py",
        ],
        "owns": [
            "operator audits, safe repair utilities, provider recovery checks",
            "queue/source/backup/completion/agent-lane/config-drift/public-readiness/UI-drift diagnostics",
            "read-only or preview-first cleanup workflows",
        ],
        "external_adapters": [
            "ComicVine",
            "SLSKD",
            "filesystem/library storage",
        ],
        "state_contract": "InkDrop diagnostics should be read-only by default or expose explicit dry-run/apply boundaries before mutation.",
        "smokes": [
            "inkdrop-standalone-entrypoints-smoke.py",
            "inkdrop-queue-throughput-audit-smoke.py",
            "inkdrop-backup-retention-smoke.py",
            "inkdrop-system-health-smoke.py",
            "inkdrop-agent-alignment-smoke.py",
        ],
        "next_step": "Keep cross-agent alignment and config-drift checks visible while moving recurring diagnostics into a System/Operations service manifest after core worker roles stabilize.",
    },
]


def service_inventory():
    return [dict(item) for item in SERVICE_INVENTORY]


def entrypoint_paths(root: Path | None = None):
    base = root or ROOT
    for service in SERVICE_INVENTORY:
        for entrypoint in service.get("entrypoints", []):
            yield service["service_id"], entrypoint, base / entrypoint


def smoke_paths(root: Path | None = None):
    base = root or ROOT
    for service in SERVICE_INVENTORY:
        for smoke in service.get("smokes", []):
            yield service["service_id"], smoke, base / smoke


def missing_files(root: Path | None = None):
    missing = []
    for service_id, rel, path in list(entrypoint_paths(root)) + list(smoke_paths(root)):
        if not path.exists():
            missing.append({"service_id": service_id, "path": rel})
    return missing


def inventory_markdown():
    lines = [
        "# InkDrop Service Inventory",
        "",
        "Source of truth: `inkdrop_service_inventory.py`.",
        "",
        "| Service | Target Role | Current Entrypoints | Key Smokes | Next Step |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in SERVICE_INVENTORY:
        entrypoints = "<br>".join(f"`{path}`" for path in item["entrypoints"])
        smokes = "<br>".join(f"`{path}`" for path in item["smokes"])
        lines.append(
            "| {label} (`{service_id}`) | `{target_role}` | {entrypoints} | {smokes} | {next_step} |".format(
                label=item["label"],
                service_id=item["service_id"],
                target_role=item["target_role"],
                entrypoints=entrypoints,
                smokes=smokes,
                next_step=item["next_step"].replace("|", "/"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print or validate the InkDrop standalone service inventory")
    parser.add_argument("--json", action="store_true", help="Print inventory as JSON")
    parser.add_argument("--markdown", action="store_true", help="Print inventory as markdown")
    parser.add_argument("--check-files", action="store_true", help="Fail if inventoried entrypoints/smokes are missing")
    args = parser.parse_args(argv)

    if args.check_files:
        missing = missing_files()
        if missing:
            print(json.dumps({"ok": False, "missing": missing}, indent=2))
            return 1
        print(json.dumps({"ok": True, "services": len(SERVICE_INVENTORY), "missing": []}, indent=2))
        return 0
    if args.markdown:
        print(inventory_markdown())
        return 0
    print(json.dumps({"services": service_inventory()}, indent=2 if args.json else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
