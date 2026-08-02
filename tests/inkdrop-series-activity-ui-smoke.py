#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = (ROOT / "inkdrop_web.py").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "static" / "css" / "inkdrop.css").read_text(encoding="utf-8")


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing {label}: {needle}")


def forbid(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"unexpected {label}: {needle}")


def main() -> None:
    require(WEB, 'if (!keep) inkdropManualReviewContractWarning(row);', "automatic Manual Review guard")
    require(WEB, 'No items currently need a human decision.', "Manual Review empty state")
    require(WEB, 'visibleManualRows.length', "Manual Review visible count")
    require(WEB, '"Event / Series", "Result", "Source / Date"', "History table headings")
    require(WEB, 'function inkdropHistoryResultText', "concise History result formatter")
    require(WEB, 'data-arr-section="source_memory" data-arr-subsection="activity"', "visible Activity Blocklist navigation")
    require(WEB, 'description: "Download, import, and verification lifecycle outcomes for completed and failed work."', "History lifecycle copy")
    require(WEB, 'mastheadAction("Diagnostics", {section: "history", filter: "all"})', "raw event diagnostics access")
    require(WEB, 'title: "Blocklist"', "Blocklist presentation")
    require(WEB, 'else if (section === "source_memory") params.set("source_filter", filter || "all");', "Blocklist Activity endpoint filter")
    require(CSS, 'body[data-inkdrop-view="source_memory"] .arr-activity-subnav', "Blocklist Activity subnav continuity")
    require(WEB, 'heading.textContent = "Series not found"', "Series not-found state")
    forbid(WEB, 'appendUnavailableSeriesDetailAction', "unsupported Series Details controls")
    forbid(WEB, 'This QA API does not expose', "internal Series monitoring language")
    forbid(WEB, 'validated backend update contract', "internal Series editing language")
    require(WEB, 'appendSeriesDetailAction(commandbar, "Search"', "working Series search action")
    require(WEB, 'appendSeriesDetailAction(commandbar, "Remove Series"', "working Series removal action")
    require(WEB, 'function seriesDetailIssuesEndpoint(row={}, limit=80)', "bounded large-series request")
    require(CSS, '.series-detail-not-found', "Series not-found styling")
    print("InkDrop Series/Activity UI smoke passed")


if __name__ == "__main__":
    main()
