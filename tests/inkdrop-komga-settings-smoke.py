#!/usr/bin/env python3
"""Smoke checks for Komga library-provider settings and folder completion docs."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "core" / "inkdrop_web.py"
STATE = ROOT / "core" / "inkdrop_state.py"
IMPORTER = ROOT / "core" / "inkdrop_completed_import.py"
PACK_IMPORT = ROOT / "core" / "inkdrop_pack_import.py"
FRONTENDS = ROOT / "core" / "inkdrop_library_frontends.py"
PLAN = ROOT / "docs" / "inkdrop" / "folder-based-completion-plan.md"


def fail(message):
    raise AssertionError(message)


def require(text, needle, label):
    if needle not in text:
        fail(f"missing {label}: {needle}")


def reject(text, needle, label):
    if needle in text:
        fail(f"stale {label}: {needle}")


def main():
    web = WEB.read_text(encoding="utf-8")
    state = STATE.read_text(encoding="utf-8")
    importer = IMPORTER.read_text(encoding="utf-8")
    pack_import = PACK_IMPORT.read_text(encoding="utf-8")
    frontends = FRONTENDS.read_text(encoding="utf-8")
    plan = PLAN.read_text(encoding="utf-8")

    require(web, 'provider(\n        "komga"', "Komga runtime provider seed")
    require(web, '"komga": {\n        "settings_group": "library"', "Komga provider metadata")
    require(web, "def komga_api_health", "Komga provider test health check")
    require(web, 'komga: ["libraries", "Connect"', "Komga Settings display group")
    require(web, "Library frontend adapters, sync visibility hooks, and outbound notifications", "Connect adapter safety copy")
    require(
        web,
        "Leave this off to let InkDrop work on its own. Turn it on only if an issue should stay unfinished until Kavita or Komga can actually see the file.",
        "folder-completion-is-primary/optional-frontend contract (Media Management field help)",
    )
    require(web, "frontend_adapter", "optional frontend provider ownership")
    require(web, "library_adapters: \"libraries\"", "Library Adapters route alias")
    require(web, '"komga": lambda: komga_api_health', "Komga Test Provider mapping")
    require(web, '"add_after_import": True', "Komga add-after-import default")
    require(web, '"library_visibility_checks_enabled": False', "standalone visibility-check default")
    require(web, '"library_visibility_provider_order": ["komga", "kavita"]', "Komga-first frontend adapter order")
    require(web, '"library_visibility_checks_enabled": {"label": "Check Frontend Visibility"', "frontend visibility check setting label")
    require(web, '"add_after_import": {"label": "Add Imported Files to Komga"', "Komga add-after-import Settings label")
    require(web, "Set up in InkDrop. Run Test provider to check Komga right now.", "Komga configured health copy")
    require(web, "Komga is enabled in Settings; use Test Provider", "Komga enabled health copy")
    reject(
        web,
        "Komga provider settings are available; enable after Komga is running and credentials are configured.",
        "Komga disabled health fallback",
    )
    require(web, '"comic_library_ids"', "Komga comic library IDs field")
    require(web, '"manga_library_ids"', "Komga manga library IDs field")

    require(state, '"komga": ("library scan", "verification", "folder completion adapter")', "Komga provider capabilities")
    require(state, '"kavita": ("library scan", "visibility adapter", "folder completion adapter")', "Kavita optional frontend capabilities")
    require(state, '"komga": "Library frontend adapter"', "Komga provider role")
    require(state, '"kavita": "Library frontend adapter"', "Kavita provider role")
    require(state, '"komga": "library"', "Komga provider filter")
    require(state, '"komga": "Komga"', "Komga display label")
    require(state, "def library_visibility_provider_label", "library provider display helper")
    require(state, "def library_visibility_next_action", "library visibility next-action helper")

    require(importer, "def load_komga_settings", "Komga importer settings loader")
    require(importer, '"add_after_import": add_after_import', "Komga importer add-after-import loader")
    require(importer, '"legacy_scan_after_import": legacy_scan_after_import', "Komga importer legacy scan alias")
    require(importer, '"scan_after_import": add_after_import', "Komga importer scan alias follows add-after-import")
    require(web, '"legacy_scan_after_import": legacy_scan_after_import', "Komga web legacy scan alias")
    require(web, '"scan_after_import": add_after_import', "Komga web scan alias follows add-after-import")
    require(frontends, "komga_add_after_import_disabled", "Komga add-after-import disabled reason")
    require(importer, "def trigger_komga_scan_folder", "Komga scan adapter hook")
    require(importer, "def komga_file_visible_for_host_path", "Komga visibility adapter hook")
    require(importer, "def komga_search_books", "Komga book search adapter")
    require(importer, "def komga_list_books_page", "Komga bounded book-list fallback")
    require(importer, "return inkdrop_library_frontends.trigger_komga_scan_folder", "Komga scan adapter wrapper")
    require(importer, "return inkdrop_library_frontends.komga_file_visible_for_host_path", "Komga visibility adapter wrapper")
    require(importer, "def library_visible_for_result", "shared library visibility helper")
    require(importer, "komga_scan_tasks", "Komga scan task status")
    require(importer, "komga_visible_count", "Komga visibility status count")
    require(importer, "library_visible_count", "shared library visibility status count")
    require(importer, "library_visibility_provider_counts", "library provider visibility counts")
    require(frontends, "/libraries/{quote(library_id, safe='')}/scan", "neutral Komga library scan endpoint")
    require(frontends, "/books/list", "neutral Komga book list search endpoint")
    require(frontends, "path_scan_pages_checked", "neutral Komga path-scan fallback evidence")
    require(pack_import, "komga_scan_tasks", "Komga pack import status")
    require(pack_import, "sync_pack_library_frontends", "pack import shared frontend sync hook")
    require(frontends, "def sync_library_frontends", "shared library frontend sync helper")
    require(frontends, "def check_library_visibility", "shared library frontend visibility helper")
    require(frontends, "def trigger_komga_scan_folder", "neutral Komga scan adapter")
    require(frontends, "def komga_file_visible_for_host_path", "neutral Komga visibility adapter")
    require(frontends, "def kavita_file_visible_for_host_path", "neutral Kavita visibility adapter")
    require(frontends, "def trigger_kavita_scan_folder", "neutral Kavita scan adapter")
    require(frontends, "def kavita_plugin_token", "neutral Kavita plugin auth adapter")
    require(importer, "def sync_library_frontend_folders", "completed import shared frontend sync hook")
    require(importer, "def kavita_file_visible_for_host_path", "Kavita visibility adapter hook")
    require(importer, "return inkdrop_library_frontends.kavita_file_visible_for_host_path", "Kavita visibility adapter wrapper")
    require(importer, "return inkdrop_library_frontends.trigger_kavita_scan_folder", "Kavita scan adapter wrapper")
    require(importer, "check_library_visibility(", "completed import shared visibility hook")
    require(web, '"last_komga_scan_tasks"', "Komga scan task status count")

    require(plan, "InkDrop owns completion truth", "folder-completion ownership statement")
    require(plan, "Komga started as a disabled library provider setting and is now live as an optional library adapter", "Komga rollout boundary")
    require(plan, "`add_after_import`", "Komga add-after-import setting")
    require(plan, "Library frontend sync now has a shared helper boundary", "shared frontend sync plan")
    require(plan, "Library frontend visibility now uses the shared adapter boundary", "shared frontend visibility plan")
    require(plan, "Do not make Komga required", "Komga non-required guardrail")
    require(plan, "`library_visibility_checks_enabled=false`", "frontend visibility checks off by default")

    print("KOMGA_SETTINGS_OK: Komga settings and folder-completion contract are present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
