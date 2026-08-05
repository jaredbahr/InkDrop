from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# inkdrop_web_config.py holds the static-asset registration constants that
# used to live directly in inkdrop_web.py.
WEB = (ROOT / "inkdrop_web.py").read_text(encoding="utf-8") + (ROOT / "inkdrop_web_config.py").read_text(encoding="utf-8")
JS = (ROOT / "web/static/js/inkdrop-manual-search.js").read_text(encoding="utf-8")
CSS = (ROOT / "web/static/css/inkdrop.css").read_text(encoding="utf-8")
FIXTURE = (ROOT / "web/tests/fixtures/manual-search.html").read_text(encoding="utf-8")
BROWSER = (ROOT / "web/tests/manual-search-browser-smoke.js").read_text(encoding="utf-8")


def require(haystack: str, needle: str, label: str) -> None:
    if needle not in haystack:
        raise AssertionError(f"missing {label}: {needle}")


require(WEB, '"inkdrop-manual-search.js"', "static asset registration")
require(WEB, "openManualSearchForRow", "shared row entry point")
require(WEB, "canonical issue or unit identity", "disabled-state explanation")
require(WEB, 'label: "Manual Search"', "Wanted and Queue action")
require(WEB, 'appendSeriesDetailIssueAction(actions, "Manual Search"', "Series Details action")
require(JS, "window.InkDropApi", "authenticated API client dependency")
require(JS, "/api/manual-search/runs", "Core run contract")
require(JS, '"partial"', "partial terminal-state contract")
require(JS, "Array.isArray(diagnosticData.provider_attempts)", "provider progress array contract")
require(JS, "duration_ms", "per-provider timing contract")
require(JS, "/api/manual-search/candidates/", "Core grab contract")
require(JS, "grabAllowed", "Core-authorized grab gating")
require(JS, "Manual retrieval required", "Assisted-only presentation")
require(JS, "includeRejected: true", "rejected-results default")
require(JS, "Show rejected results", "rejected-results control")
require(JS, "Rejected by policy", "rejected-result decision label")
require(JS, "Grab blocked by policy", "rejected-result grab safety explanation")
require(JS, "Force grab…", "server-eligible rejected-candidate action")
require(JS, "confirm_rejected_risk", "explicit rejected-candidate confirmation contract")
require(JS, "Automatic Search policy is unchanged", "override risk acknowledgement")
require(JS, "pack_size_warning", "pack warning confirmation")
require(JS, 'class="manual-search-results-columns"', "desktop result column headings")
require(JS, '<span>Language / format</span>', "combined language and format heading")
require(JS, 'role="group" aria-label="Language and format"', "accessible result field labels")
require(CSS, ".manual-search-panel", "drawer layout")
require(CSS, ".manual-search-results-columns, .manual-search-result-grid", "shared header and row grid contract")
require(CSS, "overflow-x: hidden; overflow-y: auto", "mobile drawer scroll containment")
require(CSS, ".manual-search-evidence > dl", "structured expanded evidence")
require(CSS, "@media (max-width: 430px)", "phone result layout")
require(FIXTURE, 'candidate_id: "safe-usenet"', "usenet fixture")
require(FIXTURE, 'candidate_id: "safe-slskd"', "SLSKD fixture")
require(FIXTURE, 'candidate_id: "safe-suwayomi"', "Suwayomi fixture")
require(FIXTURE, 'candidate_id: "assisted-getcomics"', "GetComics Assisted fixture")
require(FIXTURE, 'candidate_id: "safe-pack"', "pack fixture")
require(FIXTURE, 'candidate_id: "collected-rejected"', "collected-edition rejection fixture")
require(FIXTURE, 'mode === "timeout"', "timeout fixture")
require(FIXTURE, 'mode === "session"', "session-expiration fixture")
require(BROWSER, 'assert.equal(await desktop.page.locator(".manual-search-result").count(), 9', "desktop all-result browser contract")
require(BROWSER, 'call.path.includes("include_rejected=1")', "default rejected-result request contract")
require(BROWSER, '"the toggle should still support an accepted-only view"', "accepted-only toggle browser contract")
require(BROWSER, '"policy-rejected results must not expose Grab"', "rejected-result grab safety browser contract")
require(BROWSER, 'data-candidate-id="assisted-getcomics"', "assisted browser contract")
require(BROWSER, '"desktop results should expose understandable column headings"', "desktop column-heading browser contract")
require(BROWSER, '"compact desktop headings should match the visible columns"', "compact column-heading browser contract")
require(BROWSER, "mobile layout overflowed", "mobile overflow browser contract")
require(BROWSER, '"mobile users should be able to scroll to result cards"', "mobile result reachability contract")
require(BROWSER, "No results returned", "zero-result browser contract")
require(BROWSER, "secure session needs to be refreshed", "session-expiration browser contract")

print("Manual Search UI smoke passed")
