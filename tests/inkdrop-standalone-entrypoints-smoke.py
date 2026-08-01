#!/usr/bin/env python3
"""Smoke check for InkDrop implementations and internal neutral script paths.

This intentionally performs static checks and byte-compilation only. Running
``inkdrop_web.py`` would start the web server, so the smoke verifies the module
boundary without launching long-lived services.

The pre-rebrand `kavita_*.py` compatibility aliases (kavita_acquire_web.py,
kavita_series_autopilot.py, etc.) were removed 2026-07-27 once nothing on the
host or in CI still referenced them. REMOVED_LEGACY_MODULES stays here as a
permanent regression guard: none of these names should ever reappear as a
real file or an internal reference.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

import inkdrop_service_inventory as inventory


ROOT = Path(__file__).resolve().parent
DOC = ROOT / "docs" / "inkdrop" / "service-inventory.md"
SMOKES_DOC = ROOT / "docs" / "inkdrop" / "smokes.md"
IMPORT_READY_WORKER = ROOT / "inkdrop-import-ready-worker.sh"
WEB_IMPL = ROOT / "inkdrop_web.py"
RECONCILE_IMPL = ROOT / "inkdrop_reconcile_imports.py"
PACK_IMPORT_IMPL = ROOT / "inkdrop_pack_import.py"
SAB_CLEANUP = ROOT / "inkdrop_sab_failed_cleanup.py"
AUTOPILOT_IMPL = ROOT / "inkdrop_series_autopilot.py"


IMPLEMENTATIONS = {
    "inkdrop_acquire.py": "inkdrop_library_adapters",
    "inkdrop_web.py": "inkdrop_web",
    "inkdrop_completed_import.py": "inkdrop_importer",
    "inkdrop_reconcile_imports.py": "inkdrop_download_clients",
    "inkdrop_series_autopilot.py": "inkdrop_worker",
    "inkdrop_missing_acquire.py": "inkdrop_sources",
    "inkdrop_slskd_source_probe.py": "inkdrop_sources",
    "inkdrop_rss_discovery.py": "inkdrop_sources",
    "inkdrop_comicscodes_discovery.py": "inkdrop_sources",
    "inkdrop_mangadex_direct.py": "inkdrop_sources",
    "inkdrop_manual_source_autoresolve.py": "inkdrop_sources",
    "inkdrop_pack_import.py": "inkdrop_importer",
}

REMOVED_LEGACY_MODULES = (
    "kavita_acquire",
    "kavita_acquire_web",
    "kavita_completed_import",
    "kavita_reconcile_imports",
    "kavita_series_autopilot",
    "kavita_missing_acquire",
    "kavita_slskd_source_probe",
    "kavita_rss_discovery",
    "kavita_comicscodes_discovery",
    "kavita_mangadex_direct",
    "kavita_manual_source_autoresolve",
    "kavita_pack_import",
)
SUPPORTING_RUNTIME_FILES = (
    "inkdrop_acquire_adapter.py",
    "inkdrop_container_scheduler.py",
    "inkdrop_source_worker_coordinator.py",
    "inkdrop_state.py",
    "inkdrop_manga_metadata_guard.py",
)


def fail(message: str) -> None:
    raise AssertionError(message)


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        fail(f"missing {label}: {needle}")


def service_entrypoints(service_id: str) -> list[str]:
    for item in inventory.service_inventory():
        if item.get("service_id") == service_id:
            return list(item.get("entrypoints") or [])
    fail(f"missing service in inventory: {service_id}")
    return []


def main() -> int:
    for legacy_module in REMOVED_LEGACY_MODULES:
        if (ROOT / f"{legacy_module}.py").exists():
            fail(f"legacy compatibility alias should have been removed: {legacy_module}.py")

    for implementation, service_id in IMPLEMENTATIONS.items():
        path = ROOT / implementation
        if not path.exists():
            fail(f"missing implementation: {implementation}")
        text = path.read_text(encoding="utf-8")
        require(text, 'if __name__ == "__main__":', f"{implementation} executable guard")
        py_compile.compile(str(path), doraise=True)

        entrypoints = service_entrypoints(service_id)
        if implementation not in entrypoints:
            fail(f"{service_id} should list {implementation} in service entrypoints")

        for legacy_identifier in REMOVED_LEGACY_MODULES:
            if legacy_identifier in text:
                fail(f"{implementation} still depends on legacy runtime identifier {legacy_identifier}")

    for filename in SUPPORTING_RUNTIME_FILES:
        text = (ROOT / filename).read_text(encoding="utf-8")
        for legacy_identifier in REMOVED_LEGACY_MODULES:
            if legacy_identifier in text:
                fail(f"{filename} still depends on legacy runtime identifier {legacy_identifier}")

    doc = DOC.read_text(encoding="utf-8")
    for implementation in IMPLEMENTATIONS:
        require(doc, implementation, f"{implementation} documented in service inventory")
    if SMOKES_DOC.exists():
        smokes_doc = SMOKES_DOC.read_text(encoding="utf-8")
        require(smokes_doc, "inkdrop-standalone-entrypoints-smoke.py", "standalone entrypoint smoke documented")

    worker_text = IMPORT_READY_WORKER.read_text(encoding="utf-8")
    require(worker_text, 'SCRIPT_DIR="${INKDROP_BIN_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"', "import-ready worker script-relative bin dir")
    require(worker_text, 'RECONCILE_SCRIPT="${INKDROP_RECONCILE_IMPORTS_SCRIPT:-$SCRIPT_DIR/inkdrop_reconcile_imports.py}"', "import-ready worker neutral reconcile command")
    require(worker_text, 'SAB_CLEANUP_SCRIPT="${INKDROP_SAB_FAILED_CLEANUP_SCRIPT:-$SCRIPT_DIR/inkdrop_sab_failed_cleanup.py}"', "import-ready worker neutral SAB cleanup command")
    require(worker_text, 'LOG="${INKDROP_IMPORT_READY_LOG:-$LOG_DIR/inkdrop-import-ready-worker.log}"', "import-ready worker configurable log path")
    private_home = "/home/" + "curlz620"
    if f"{private_home}/bin/" in worker_text or f"{private_home}/arr-docker/" in worker_text:
        fail("import-ready worker should not require operator-specific host paths")

    web_text = WEB_IMPL.read_text(encoding="utf-8")
    require(web_text, 'script_path("inkdrop_completed_import.py"', "web neutral import command")
    require(web_text, 'script_path("inkdrop_reconcile_imports.py"', "web neutral reconcile command")
    require(web_text, 'script_path("inkdrop_pack_import.py"', "web neutral pack import command")
    require(web_text, 'script_path("inkdrop_missing_acquire.py"', "web neutral missing-acquire command")
    require(web_text, 'MISSING_ACQUIRE_MODULE_SCRIPT = script_path("inkdrop_missing_acquire.py"', "web missing-acquire implementation import path")
    require(web_text, 'script_path("inkdrop_slskd_source_probe.py"', "web neutral SLSKD probe command")
    require(web_text, '"inkdrop_rss_discovery.py"', "web neutral RSS discovery command")
    require(web_text, '"inkdrop_comicscodes_discovery.py"', "web neutral ComicsCodes discovery command")
    require(web_text, '"inkdrop_manual_source_autoresolve.py"', "web neutral manual-source autoresolve command")
    require(web_text, 'script_path("inkdrop_series_autopilot.py"', "web neutral autopilot command")

    reconcile_text = RECONCILE_IMPL.read_text(encoding="utf-8")
    require(reconcile_text, 'IMPORTER_MODULE_PATH = script_path("inkdrop_completed_import.py"', "reconcile implementation import path")
    require(reconcile_text, 'IMPORTER_PATH = script_path("inkdrop_completed_import.py"', "reconcile neutral importer CLI path")

    pack_text = PACK_IMPORT_IMPL.read_text(encoding="utf-8")
    require(pack_text, 'COMPLETED_IMPORT_PATH = script_path("inkdrop_completed_import.py"', "pack import neutral importer CLI path")

    sab_text = SAB_CLEANUP.read_text(encoding="utf-8")
    require(sab_text, 'RECONCILE_SCRIPT = script_path("inkdrop_reconcile_imports.py"', "SAB cleanup neutral reconcile command")

    autopilot_text = AUTOPILOT_IMPL.read_text(encoding="utf-8")
    require(autopilot_text, 'MISSING_SCRIPT = script_path("inkdrop_missing_acquire.py"', "autopilot neutral missing-acquire command")
    require(autopilot_text, 'RSS_SCRIPT = script_path("inkdrop_rss_discovery.py"', "autopilot neutral RSS discovery command")
    require(autopilot_text, 'COMICSCODES_SCRIPT = script_path("inkdrop_comicscodes_discovery.py"', "autopilot neutral ComicsCodes discovery command")
    require(autopilot_text, 'SLSKD_SOURCE_PROBE_SCRIPT = script_path("inkdrop_slskd_source_probe.py"', "autopilot neutral SLSKD probe command")
    require(autopilot_text, 'SLSKD_SOURCE_PROBE_MODULE_SCRIPT = script_path("inkdrop_slskd_source_probe.py"', "autopilot SLSKD implementation import path")
    require(autopilot_text, 'MANGADEX_DIRECT_SCRIPT = script_path("inkdrop_mangadex_direct.py"', "autopilot neutral MangaDex direct command")

    print("STANDALONE_ENTRYPOINTS_OK: InkDrop owns implementations directly, no kavita_* aliases remain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
