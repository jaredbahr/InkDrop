from pathlib import Path
import json
import re
import sys


ROOT = Path(__file__).resolve().parents[1]


def catalog_matches_release_contract():
    """The About page's newest entry and the release contract name one release.

    Both are hand-edited during a version bump, and nothing else notices when
    only one of them is updated -- the About page would then advertise a
    version that was never released.
    """
    contract = json.loads((ROOT / "docs/inkdrop/releases/current.json").read_text(encoding="utf-8"))
    catalog = (ROOT / "web/static/js/inkdrop-version-about.js").read_text(encoding="utf-8")
    tag = str(contract.get("tag") or "")
    expected_slug = tag.replace(".", "-").lower()
    return f'version: "{tag}"' in catalog and f'slug: "{expected_slug}"' in catalog
# inkdrop_web_config.py holds the static-asset registration constants that
# used to live directly in inkdrop_web.py.
web = (ROOT / "core" / "inkdrop_web.py").read_text(encoding="utf-8") + (ROOT / "core" / "inkdrop_web_config.py").read_text(encoding="utf-8")
css = (ROOT / "web/static/css/inkdrop.css").read_text(encoding="utf-8")
dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
gaps = (ROOT / "docs/inkdrop/UI_BACKEND_GAPS.md").read_text(encoding="utf-8")
bootstrap = (ROOT / "web/static/js/inkdrop-operational-bootstrap.js").read_text(encoding="utf-8")
about = (ROOT / "web/static/js/inkdrop-version-about.js").read_text(encoding="utf-8")

checks = {
    "release history keeps ten detailed updates and leaves older notes on GitHub": "DETAILED_RELEASE_LIMIT = 10" in about
    and "DETAILED_RELEASES.slice(0, DETAILED_RELEASE_LIMIT)" in about
    and ".concat(RELEASE_ROLLUPS)" not in about
    and 'GITHUB_RELEASE_HISTORY_URL = "https://github.com/jaredbahr/InkDrop/releases"' in about
    and 'fullHistory.textContent = "Full release history on GitHub"' in about,
    "healthy pack automation is hidden while attention states stay prominent": 'if (!state || !state.active) {' in web
    and 'if (passiveAutomation) {' in web
    and 'box.dataset.packPresentation = "attention"' in web
    and 'pack-passive-options' not in web
    and 'PACK_FINISHED_BANNER_SECONDS' not in web
    and 'packRefreshBtn.className = "primary"' in web
    and 'packFinishBtn.className = "primary"' in web
    and "The downloader appears stalled. Refresh Status" in web
    and 'attentionStates.has(lifecycleKey)' in web,
    "download-client UI asset is whitelisted": '"inkdrop-download-clients-ui.js"' in web,
    "download-client UI asset is loaded": 'src="/static/js/inkdrop-download-clients-ui.js?v=__INKDROP_UI_JS_VERSION__"' in web,
    "operational bootstrap mounts prepared assets": "InkDropOperationalBootstrap" in bootstrap
    and "InkDropOperationalTablePreferences" in bootstrap
    and "InkDropOperationalTableControls" in bootstrap
    and "InkDropOperationalQueryControls" in bootstrap
    and "InkDropOperationalRowControls" in bootstrap
    and "InkDropTransferTelemetry" in bootstrap
    and "InkDropVersionAbout" in bootstrap,
    "operational assets are packaged": "COPY web/static/js/ ./web/static/js/" in dockerfile,
    "operational assets are served as javascript": "def send_ui_javascript" in web
    and '"application/javascript; charset=utf-8"' in web
    and 'path.startswith("/static/js/")' in web,
    "operational bootstrap loads once": 'src="/static/js/inkdrop-operational-bootstrap.js?v=__INKDROP_UI_JS_VERSION__"' in web
    and "window.__inkdropOperationalAssetsStarted" in web
    and 'bootstrap.loadAssets({baseUrl: "/static/js/"})' in web
    and "window.mountInkdropOperationalAssets = root => bootstrap.mount(root || document)" in web
    and "window.mountInkdropOperationalAssets?.(panel)" in web
    and "window.mountInkdropOperationalAssets?.(grid)" in web,
    "series links prefer canonical series id": "row?.series_id || row?.seriesId || links.series_id || row?.id" in web,
    "indeterminate queue stages have no fake bar": 'box.setAttribute("role", "status")' in web
    and "if (progress.determinate) {\n        const track" in web
    and "slskdRemoteQueue" in web
    and "if (!moving) return null" in web
    and "row?.display_source" in web,
    "general auth status is honest": 'fetch("/api/inkdrop-auth/status"' in web
    and "Built-in login not configured" in web
    and "Create the first administrator from the setup screen" in web,
    "about exposes qa build": 'appendSystemTableRow(about, ["QA Build"' in web
    and "systemCopyValue" in web,
    "about exposes rolling public release notes": 'releaseNotes.dataset.inkdropReleaseNotes = "true"' in web
    and "InkDropVersionAbout?.renderReleases" in web
    and 'inkdrop-version-about-ready' in web
    and "PUBLIC_RELEASES" in (ROOT / "web/static/js/inkdrop-version-about.js").read_text(encoding="utf-8")
    # Derived from the release contract rather than pinned to a literal title
    # and slug: those had to be hand-edited on every version bump, and the
    # check went red three releases running for saying nothing more than "the
    # version changed". What matters is that the catalog and the contract name
    # the same release.
    and catalog_matches_release_contract()
    and "Initial public alpha" not in (ROOT / "web/static/js/inkdrop-version-about.js").read_text(encoding="utf-8")
    and 'title.textContent = "Release history"' in (ROOT / "web/static/js/inkdrop-version-about.js").read_text(encoding="utf-8")
    and ".inkdrop-release-entry" in css
    and ".inkdrop-release-body[hidden]" in css,
    "settings use canonical providers": "canonicalSettingsProviders" in web
    and 'id === "comic_vine" && provider?.settings?.source_template' in web,
    "legacy watch removal uses current product language": "Existing files, downloads, reader-library entries, and metadata links stay untouched." in web
    and "Kavita library items, and Kapowarr entries stay untouched." not in web,
    "settings backup restore is accessible and bounded": 'panel.dataset.settingsBackupRestore = "true"' in web
    and 'Download settings backup' in web
    and 'Choose InkDrop settings backup JSON' in web
    and 'role", "status"' in web
    and 'aria-live", "polite"' in web
    and 'file.size > 1024 * 1024' in web
    and 'automatic pre-restore settings snapshot' in web
    and '.settings-backup-restore' in css
    and '@media (max-width: 600px)' in css,
    "setup reconciles effective provider status": "setupWithEffectiveProviders" in web
    and "Configured via environment" in web
    and "Configured via InkDrop" in web
    and "external_download_configured" in web,
    "download clients show only real telemetry": "appendDownloadClientOperationalSummary" in web
    and "health.active_downloads !== undefined" in web
    and "health.failed_downloads !== undefined" in web
    and "InkDrop is not reporting live download-client stats yet." in web
    and "download-client-operational-summary" in css,
    "media management uses one open editor": 'group.key === "media_management"' in web
    and 'linkedAppSettings.length && group.key !== "media_management"' in web,
    "table prefs storage": "ARR_TABLE_PREF_STORAGE_KEY" in web,
    "default order reset": "resetInkdropArrTableView" in web
    and "page size, visible columns" in web,
    "server-side operational sort": 'params.set("sort", sort.key)' in web
    and 'params.set("direction", sort.dir)' in web
    and 'loadInkdropSection(key, focus' in web,
    # The tilde alone marks a facet count approximate ("~1.2K"); the word
    # "sampled" moved out of the button text (it read as jargon in the tab
    # strip -- flagged in review) and survives in the hover title.
    "sampled facets are visibly labeled": "function facetCountText" in web
    and '`~${count}`' in web
    and "count.textContent = item.countText || compactNumber" in web
    and 'btn.textContent = `${filterLabel} ${facetCountText(item)}`' in web
    and 'appendSectionSummaryChip(box, "sources", facetCountText(sectionFilterRow(viewPayload, "sources")))' in web
    and 'appendSectionSummaryChip(box, "downloads", facetCountText(sectionFilterRow(viewPayload, "downloads")))' in web
    and 'appendSectionSummaryChip(box, "imports", facetCountText(sectionFilterRow(viewPayload, "imports")), "good")' in web
    # History's stat cards (Completed/Failed/Retried/Needs review) get their
    # real counts from a cached, sampled rollup (history_outcome_rollup() in
    # inkdrop_state.py) rather than scanning every history_events row on
    # every request -- outcomeSummary.sampled visibly labels the cards with
    # the sample size when a card's count is approximate.
    and "const outcomeSummary = viewPayload?.outcome_summary || {};" in web
    and "outcomeSummary.sampled" in web
    and "recent events)`" in web
    and 'sectionWorkbenchCard(box, "Completed", Number(outcomeSummary.completed || 0)' in web
    and 'sectionWorkbenchCard(box, "Needs review", needsReviewCount' in web,
    "page size reloads endpoint": 'loadInkdropSection(key, focus, {scroll: "none", keepExisting: true, limit: size})' in web,
    "operational first paint respects page size": '["queue", "wanted", "manual_review"].includes(view)' in web
    and "inkdropArrTablePrefsForView(view).pageSize" in web,
    "menus close and expose state": "closeInkdropArrTableMenus" in web
    and 'summary.setAttribute("aria-expanded", "false")' in web
    and 'summary.setAttribute("aria-controls", panelId)' in web
    and 'panel.setAttribute("role", "menu")' in web
    and 'panel.setAttribute("popover", "manual")' in web
    and "portalInkdropArrTableMenuPanel" in web
    and "positionInkdropArrTableMenu" in web
    and "discardInkdropArrTableMenu" in web
    and "ensureInkdropArrTableMenuLifecycleObserver" in web
    and "subscribeInkdropArrTableVisualViewport" in web
    and "unsubscribeInkdropArrTableVisualViewport" in web
    and "visualViewport?.offsetLeft" in web
    and "visualViewport?.offsetTop" in web
    and "const inkdropArrTableMenus = new Set()" in web
    and 'function rerenderInkdropArrTablePrefs(viewPayload={}) {\n      closeInkdropArrTableMenus();' in web
    and 'function renderInkdropSection(viewPayload) {\n      closeInkdropArrTableMenus();' in web
    and 'function renderInkdropSectionState(view, message, tone="") {\n      closeInkdropArrTableMenus();' in web
    and 'function renderInkdropSectionTable(parent, view, rows=[], viewPayload={}) {\n      closeInkdropArrTableMenus();' in web
    and 'window.addEventListener("resize", scheduleInkdropArrTableMenuPosition)' in web
    and 'window.addEventListener("scroll", scheduleInkdropArrTableMenuPosition, true)' in web
    and ".arr-table-menu-panel.arr-table-menu-panel-portal" in css
    and 'event.key !== "Escape"' in web
    and "bindInkdropDetailsSummaryKeyboard(summary, menu)" in web,
    "row disclosures support keyboard": "bindInkdropDetailsSummaryKeyboard(summary, drawer)" in web
    and '["Enter", " "].includes(event.key)' in web,
    "default sort status is not duplicated": 'if (sort.key === "endpoint") return null;' in web,
    "no placeholder columns": "Column selection is not exposed yet" not in web,
    "wanted batch search stays visible": 'appendArrTableButton(left, "Search Selected"' in web
    and '.arr-table-controlbar[data-arr-selected-count="0"] [data-arr-requires-selection="1"]' in css
    and "display: inline-flex" in css,
    "wanted rows expose queue and open with a labeled evidence drawer": 'if (view === "wanted") {' in web
    and 'label: "Queue"' in web
    and 'label: "Open"' in web
    and 'forceText: true' in web
    and '["queue", "wanted"].includes(String(view || ""))' in web
    and 'const secondaryLabel = "Evidence";' in web
    and 'String(group.key || "") === "inspect"' in web
    and 'body[data-inkdrop-view="wanted"] .section-table-actions.compact-action-rail > .section-table-action-group .section-table-action-group-buttons > button[data-action-force-text="1"][data-action-glyph]' in css,
    "wanted queue action keeps review boundary": 'label: "Review"' in web
    and 'Open the Manual Review decision for this wanted item' in web
    and 'onClick: () => openLinkedInkdropSection("manual_review", row)' in web,
    "wanted tools preserve diagnostics": '...(Array.isArray(model.actions) ? model.actions : [])' in web
    and '...inkdropLinkedEntityActions(view, row)' in web
    and '["queue", "series"].includes(label)' in web,
    "wanted id normalizes before queue mutation": 'const id = row?.id || row?.wanted_id || "";' in web
    and 'api("/api/inkdrop-state/wanted/run", {id})' in web
    and 'busyLabel === "queue"' in web,
    "auth backdrop lifts original-art midtones behind the protected card": 'rgba(9, 14, 20, .72)' in css
    and 'rgba(9, 14, 20, .46)' in css
    and 'brightness(1.08) saturate(.82) contrast(1.05)' in css
    and 'background: rgba(27, 34, 42, .94)' in css,
    "cards disabled reason": "Cards are disabled here until the endpoint" in web,
    "unavailable controls are genuinely disabled": "function arrTableUnavailableItem" in web
    and "disabled: true" in web
    and "This control is not implemented yet" in web,
    "unsupported full-dataset sorts explain backend gap": 'arrTableUnavailableItem("Progress", backendReason("Progress"))' in web
    and 'arrTableUnavailableItem("ETA", backendReason("ETA"))' in web
    and 'arrTableUnavailableItem("Last Searched", backendReason("Last searched"))' in web
    and "state-view endpoint supports that full-dataset sort key" in web,
    "manual review filter": "inkdropManualReviewIsHumanDecision(row)" in web
    and "inkdropManualReviewContractWarning(row)" in web,
    "manual review decision count is canonical": "function manualReviewDecisionCount(viewPayload={})" in web
    and 'const actionableFacet = sectionFilterRow(viewPayload, "actionable")' in web
    and 'if (actionableFacet) return Number(actionableFacet.count || 0)' in web
    and 'const decisions = manualReviewDecisionCount(viewPayload);' in web
    and 'fact("decisions", c.decisions, "manual_review", "actionable", c.decisions ? "bad" : "good", "Open Manual Review decision rows")' in web,
    "manual review does not render automatic rows": "rows = (rows || []).filter(row =>" in web
    and "const keep = inkdropManualReviewIsHumanDecision(row);" in web,
    "manual review auto classifier": "provider wait" in web.lower()
    and "no_candidate" in web,
    "system about preserves version metadata object": 'if (data && typeof data === "object") return data;' in web
    and 'return data ? {version: String(data)} : {};' in web,
    "table limits are view scoped": "arrTableVisibleRows(rows).length" not in web
    and 'arrTableVisibleRows(rows, "queue").length' in web
    and 'arrTableVisibleRows(rows, "wanted").length' in web
    and 'arrTableVisibleRows(rows, "manual_review").length' in web,
    "selected rows are view scoped": "arrTableVisibleRows(Array.isArray(payload.rows) ? payload.rows : [], key)" in web,
    "normalized transfer telemetry": "queueTransferProgressModel" in web
    and "progress_kind" in web
    and "percent_complete" in web
    and "download_rate_bytes_per_second" in web
    and "eta_seconds" in web
    and "transfer.import_stage\n        || transfer.transfer_state" in web
    and 'statusText: percent !== null ? transferProgressText(percent) : stage' in web
    and "Never invent" not in web,
    "prefs css": "arr-table-hide-thumbnails" in css
    and "arr-table-density-detailed" in css
    and "queue-row-progress.stalled" in css
    and 'grid-template-areas:' in css
    and '"select item state source actions"' in css
    and 'body[data-inkdrop-view="wanted"] .arr-table-controlbar-right' in css
    and "overflow: visible" in css,
    "backend gaps": "Operational table controls" in gaps
    and "server query contract" in gaps
    and "structured provider/client/media-type/age filter facets" in gaps
    and "Effective setup status" in gaps
    and "Download-client telemetry" in gaps
    and "Authentication management" in gaps,
}

failed = [name for name, ok in checks.items() if not ok]
if failed:
    print("Operational UI static smoke failed:")
    for name in failed:
        print(f"- {name}")
    sys.exit(1)

print("Operational UI static smoke passed.")
