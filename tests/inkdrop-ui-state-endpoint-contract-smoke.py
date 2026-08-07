#!/usr/bin/env python3
"""Static guard for InkDrop UI state endpoint first-paint contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "core" / "inkdrop_web.py"


def fail(message: str) -> None:
    raise SystemExit(f"UI_STATE_ENDPOINT_CONTRACT_FAIL: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def function_body(text: str, name: str) -> str:
    match = re.search(rf"^\s{{4}}(?:async\s+)?function {re.escape(name)}\(.*?\) \{{\n(?P<body>.*?)(?=^\s{{4}}(?:async\s+)?function |\n\s{{4}}[a-zA-Z0-9_$]+\\.|</script>)", text, re.S | re.M)
    if not match:
        fail(f"missing JavaScript function {name}")
    return match.group("body")


def main() -> int:
    text = WEB.read_text(encoding="utf-8")
    series_first_paint_body = function_body(text, "inkdropSeriesFirstPaintLimit")
    first_paint_body = function_body(text, "inkdropSectionFirstPaintLimit")
    endpoint_body = function_body(text, "inkdropSectionEndpoint")
    fallback_body = function_body(text, "inkdropSectionFallbackEndpoint")
    loader_body = function_body(text, "loadInkdropSection")
    system_area_body = function_body(text, "openSystemArea")
    system_body = function_body(text, "loadInkdropSystemPage")
    suwayomi_settings_body = function_body(text, "hydrateSuwayomiSettingsTruth")
    duplicate_workload_body = function_body(text, "getDuplicateSeriesWorkloadMap")

    require("const INKDROP_SECTION_LOAD_MORE_MAX = 240;" in text, "load-more cap should stay bounded")
    require('["queue", "wanted", "manual_review"].includes(view) && !hasFocusLinks' in first_paint_body, "Operational broad first paint should honor the selected page size")
    require('return inkdropArrTablePrefsForView(view).pageSize;' in first_paint_body, "Operational page size should drive the endpoint limit")
    require('if (["queue", "wanted"].includes(view) && hasFocusLinks) return 40;' in first_paint_body, "Focused Queue/Wanted rows should stay bounded")
    require('if (view === "history") return 40;' in first_paint_body, "History first paint should stay bounded")
    require('if (view === "series") return inkdropSeriesFirstPaintLimit();' in first_paint_body, "Series first paint should use the dedicated helper")
    require('if (view === "series" && !hasFocusLinks) return INKDROP_SECTION_LOAD_MORE_MAX;' not in first_paint_body, "Series first paint must not use the old broad no-focus load-more cap")
    require('return INKDROP_SERIES_FULL_LOAD_LIMIT;' in series_first_paint_body, "Series first paint should request the full series poster dataset")
    require('INKDROP_HEAVY_SECTION_FIRST_PAINT_LIMIT' not in series_first_paint_body, "Series poster first paint should not use the bounded heavy-section limit")

    require('new URLSearchParams({limit: String(inkdropSectionFirstPaintLimit(value, focus, options)), summary: "compact"})' in endpoint_body, "state section endpoints must default to compact summary mode")
    require('params.set("rows", "thin")' in endpoint_body, "Series/Queue/Wanted first paint should request thin rows")
    require('params.set("rows", "table")' in endpoint_body, "Manual Review first paint should request table rows")
    require('params.set("rows", "compact")' in endpoint_body, "secondary operational sections should request compact rows")
    for section, filter_param in {
        "queue": "queue_filter",
        "wanted": "wanted_filter",
        "series": "series_filter",
        "history": "history_filter",
        "manual_review": "manual_review_filter",
    }.items():
        require(f'value === "{section}"' in endpoint_body and f'params.set("{filter_param}"' in endpoint_body, f"{section} endpoint should preserve its filter parameter")
    require('params.set("sort", sort.key)' in endpoint_body, "operational sort must be sent to the server")
    require('params.set("direction", sort.dir)' in endpoint_body, "operational sort direction must be sent to the server")

    require('return `/api/inkdrop-state/${value}?${params.toString()}`;' in endpoint_body, "state sections should use section-specific endpoints")
    require('return "";' in fallback_body, "generic state-view fallback should stay disabled unless it preserves compact row mode")
    require('value !== "series" || !endpoint || !endpoint.includes("rows=")' in fallback_body, "fallback guard should only consider compact row-mode requests")
    require('payload = await getJsonWithTimeout(endpoint, options?.timeoutMs || 10000' in loader_body, "section loader should use a bounded endpoint timeout")
    require('fallbackEndpoint = inkdropSectionFallbackEndpoint(key, endpoint)' in loader_body, "section loader should only retry through the guarded fallback helper")
    require('cacheInkdropSectionPayload(endpoint, viewPayload)' in loader_body, "section loader should cache successful payloads")
    require('restoreCachedInkdropSectionPayload(endpoint)' in loader_body, "section loader should restore cached payloads on transient failures")

    require('setInkdropRouteHash("system", {area: target})' in system_area_body, "System subnav should preserve area routing")
    require('await loadInkdropSystemPage(false)' in system_area_body, "System subnav should load data for the newly selected area")

    require('fetchJson("/api/inkdrop-state/sections?summary=compact")' in system_body, "System page should load compact state sections")
    require('fetchJson("/api/inkdrop-state/history?limit=24&history_filter=activity&summary=compact&rows=compact")' in system_body, "System page should load bounded compact History Activity")
    require('const historyPromise = activeArea === "advanced"' in system_body, "System History Activity should load only in Advanced Diagnostics")
    require('const workerPromise = ["tasks", "advanced"].includes(activeArea)' in system_body, "System workers should load in Tasks and Advanced Diagnostics")
    require('const suwayomiPromise = activeArea === "health"' in system_body, "Suwayomi diagnostics should load only in System Health")
    require('fetchJson("/api/system/suwayomi", {cache: "default"})' in system_body, "System Health should use the privately cacheable Suwayomi projection")
    require(system_body.count('fetchJson("/api/system/suwayomi"') == 1, "System Health must not duplicate or poll the Suwayomi projection")
    require(suwayomi_settings_body.count('fetch("/api/system/suwayomi"') == 1, "Settings must issue only one Suwayomi projection request per hydration")
    require('{cache: "default"}' in suwayomi_settings_body and "setInterval" not in suwayomi_settings_body, "Settings should honor the short private cache without polling")
    require('const updatePromise = Promise.resolve({});' in system_body, "removed Updates surface should not request update status")
    require('fetchJson("/api/system/update-status?refresh=0")' not in system_body, "removed Updates surface must not fetch local status")
    require('renderInkdropSystemPage(payload);' in system_body, "Updates should render local state before refresh")
    require('void fetchJson("/api/system/update-status?refresh=1")' not in system_body, "removed Updates surface must not refresh remote state")
    require('const packDuplicatePromise = activeArea === "advanced"' in system_body, "pack duplicate diagnostics should load only in Advanced Diagnostics")
    require('const loadSeq = ++inkdropSystemLoadSeq' in system_body, "System loads should use a monotonic request sequence")
    require('loadSeq !== inkdropSystemLoadSeq || systemAreaFromRouteParams() !== activeArea' in system_body, "stale System responses should not overwrite the current area")
    require('Promise.resolve([])' in system_body and 'Promise.resolve({})' in system_body, "inactive System areas should resolve empty local payloads without requests")
    for expensive in (
        "reader-frontend-orphan-cleanup?provider=kavita&limit=500",
        "manga-chapter-artifacts?includeIssueTokens=1",
        "mixed-manga-units",
    ):
        require(expensive in system_body, f"expected diagnostic endpoint missing: {expensive}")
    # The managed library scan moved to an explicit Scan now action; page load
    # may only read the cached last result.
    require("managed-library-audit?maxFiles" not in system_body, "Advanced page load must not trigger the managed library scan")
    require('fetchJson("/api/inkdrop-diagnostics/managed-library-audit")' in system_body, "Advanced page should read the cached managed library result")
    require('activeArea === "advanced"' in system_body, "expensive diagnostics should be gated to Advanced Diagnostics")
    require(system_body.count('activeArea === "advanced"') >= 5, "each expensive diagnostic should be independently gated")

    require('const WORKER_ACTIVITY_ENDPOINT = "/api/inkdrop-state/workers?limit=12&summary=compact";' in text, "worker activity endpoint should be compact and bounded")
    duplicate_workload_endpoint = '"/api/inkdrop-state/series?series_filter=duplicate_titles&limit=200&summary=compact&rows=compact"'
    require(duplicate_workload_endpoint in duplicate_workload_body, "duplicate-title workload request should stay compact and bounded")
    require("getJsonWithTimeout(" in duplicate_workload_body and '"Duplicate series review"' in duplicate_workload_body, "duplicate-title workload request should have a labeled timeout")
    require("duplicateSeriesWorkloadFetchInFlight" in duplicate_workload_body, "duplicate-title workload requests should share one in-flight query")
    require('series_filter=duplicate_titles&limit=5000' not in text, "duplicate-title workload fetch must not use an unbounded heavy limit")
    require('parsed.searchParams.delete("rows")' not in text, "fallback logic must not remove compact row mode")
    require('state_view_performance' in text, "state-view payloads should include route/timing diagnostics")
    require('if (id === "suwayomi") return "Suwayomi API / Page Sources";' in text, "Settings must distinguish API/page sources")
    require('if (id === "suwayomi_managed_folder") return "Suwayomi Managed Folder";' in text, "Settings must distinguish managed-folder import")
    require('? "Test Connection" : "Test"' in text, "Suwayomi must use the canonical Test Connection label")
    require('if (routeArea === "download_sources") void hydrateSuwayomiSettingsTruth();' in text, "Settings source truth should load lazily only in Download Sources")
    require('Managed Folder Import Dry Run' in text and 'Process Managed Folder Imports' in text, "legacy staging actions must be labeled as managed-folder imports")
    require('explicit_cache_control = any(str(name).strip().lower() == "cache-control"' in text, "explicit private cache policy must replace the JSON no-store default")
    require('headers={"Cache-Control": "private, max-age=15"}' in text, "Suwayomi projection should have one short private cache policy")

    print(json.dumps({"ok": True, "checked": "ui_state_endpoint_contract"}))
    print("UI_STATE_ENDPOINT_CONTRACT_OK: UI state endpoints stay compact, bounded, and cacheable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
