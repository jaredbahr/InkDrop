import { Fragment, useEffect, useState } from "react";
import { request, InkDropApiError } from "../api";
import type { HistoryRow, HistoryViewPayload } from "./historyTypes";

const PAGE_SIZE_OPTIONS = [25, 50, 80, 150] as const;
const DEFAULT_PAGE_SIZE = 80;

function titleCase(raw: string): string {
  return raw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// A reduced version of inkdropHistoryEventLabel's (inkdrop_web.py) regex
// ladder -- the common event kinds, not every niche one, matching the level
// of fidelity the Wanted/Queue islands already used for their own stage
// labels rather than porting every vanilla-JS label rule verbatim. Each
// class also carries its glyph from the shell's :root --arr-glyph registry.
function eventInfo(row: HistoryRow): { label: string; icon: string } {
  const raw = (row.event_type || row.history_kind || row.status || "event").toLowerCase();
  if (/retry/.test(raw)) return { label: "Retry Scheduled", icon: "refresh" };
  if (/search.*fail|fail.*search/.test(raw)) return { label: "Search Failed", icon: "x" };
  if (/search.*complete|complete.*search/.test(raw)) return { label: "Search Completed", icon: "search" };
  if (/search/.test(raw)) return { label: "Search", icon: "search" };
  if (/import.*fail|fail.*import/.test(raw)) return { label: "Import Failed", icon: "x" };
  if (/verified|verification/.test(raw)) return { label: "Verified", icon: "check" };
  if (/import/.test(raw)) return { label: "Imported", icon: "check" };
  if (/download.*complete|completed.*download/.test(raw)) return { label: "Download Completed", icon: "cloud-download" };
  if (/download.*fail|fail.*download/.test(raw)) return { label: "Download Failed", icon: "x" };
  if (/download/.test(raw)) return { label: "Download Started", icon: "download" };
  if (/grab|candidate.*accept/.test(raw)) return { label: "Grabbed", icon: "download" };
  if (/reject/.test(raw)) return { label: "Candidate Rejected", icon: "shield" };
  if (/blocked|block/.test(raw)) return { label: "Source Blocked", icon: "shield" };
  if (/series_added/.test(raw)) return { label: "Series Added", icon: "calendar" };
  if (/scan/.test(raw)) return { label: "Library Scan", icon: "radar" };
  if (/manual/.test(raw)) return { label: "Manual Decision", icon: "eye" };
  return { label: titleCase(raw), icon: "clock" };
}

function seriesTitle(row: HistoryRow): string {
  const issue = row.issue_number ? ` #${row.issue_number}` : "";
  return row.series ? `${row.series}${issue}` : "InkDrop";
}

// Result pill: same display_phase/outcome vocabulary history_outcome_bucket
// (inkdrop_state.py) uses for the masthead cards, so a row's pill and the
// card it counts toward can never disagree about what happened.
function resultPill(row: HistoryRow): { label: string; tone: string } {
  const phase = (row.display_phase || "").toLowerCase();
  const outcome = (row.outcome || "").toLowerCase();
  if (phase === "manual_review") return { label: "Needs review", tone: "bad" };
  if (phase === "retry_later") return { label: "Retried", tone: "warn" };
  // Blocked beats the generic problem outcome: a blocklist hit IS a problem
  // outcome server-side, but "Blocked" is the specific truth.
  if (/blocked/.test(phase) || /blocked/.test((row.status || "").toLowerCase())) return { label: "Blocked", tone: "warn" };
  if (outcome === "problem") return { label: "Failed", tone: "bad" };
  if (phase === "verified") return { label: "Verified", tone: "good" };
  if (phase === "import_ready") return { label: "Completed", tone: "good" };
  if (/downloading|importing|searching|running|active|in_progress/.test(phase)) return { label: "In progress", tone: "auto" };
  const fallback = row.display_phase_label || row.status || row.outcome || "Logged";
  return { label: titleCase(String(fallback)), tone: "" };
}

function sourceLabel(row: HistoryRow): string {
  return row.display_source || row.provider_id || row.provider_key || "";
}

function endSentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  return /[.!?]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}

// The Details sentence prefers the REAL stored text for this exact event
// (failure_reason, then message) and only falls back to a class-derived
// sentence when the row carries no specific text -- a canned phrase must
// never displace what actually happened.
function detailText(row: HistoryRow): string {
  const kind = [row.event_type, row.history_kind, row.status, row.display_phase, row.outcome]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  const source = sourceLabel(row);
  const retryScheduled = (row.display_phase || "").toLowerCase() === "retry_later" || (/retry/.test(kind) && row.retry_eligible !== false);
  const real = String(row.failure_reason || row.message || "").trim();
  if (real) {
    let sentence = endSentence(real);
    if (/blocked|block/.test(kind) && !/block/i.test(real)) sentence = `Source blocked: ${sentence}`;
    if (retryScheduled && !/retry/i.test(real)) sentence += " Automatic retry is scheduled.";
    return sentence;
  }
  const isProblem = /problem|fail|error/.test((row.outcome || "").toLowerCase());
  if (/no[_ -]?candidate|no safe candidate/.test(kind)) {
    return retryScheduled ? "No safe candidate found; retry scheduled." : "No safe candidate found.";
  }
  if (/blocked|block/.test(kind)) return "Source blocked by a blocklist rule.";
  if (isProblem) return retryScheduled ? "The attempt failed. Automatic retry is scheduled." : "The attempt failed.";
  if (/retry/.test(kind)) return "Automatic retry triggered after the previous attempt.";
  if (/manual/.test(kind)) return "Waiting on a manual decision.";
  if (/verified|verification/.test(kind)) return source ? `File imported and verified using ${source}.` : "File imported and verified.";
  if (/import/.test(kind)) {
    const verified = /verif|library visible|satisfied/.test(kind);
    if (verified) return source ? `Imported and verified in the library using ${source}.` : "Imported and verified in the library.";
    return source ? `File imported from ${source}.` : "File imported.";
  }
  if (/download.*complete|completed.*download/.test(kind)) return source ? `Download completed from ${source}.` : "Download completed.";
  if (/download/.test(kind)) return source ? `Download started from ${source}.` : "Download started.";
  if (/grab|candidate.*accept/.test(kind)) return source ? `Candidate accepted from ${source}.` : "Candidate accepted.";
  if (/reject/.test(kind)) return "Candidate rejected.";
  if (/series_added/.test(kind)) return "Series added to the library and wanted list.";
  if (/scan/.test(kind)) return "Library scan recorded.";
  const fallback = row.next_action || row.activity_summary || "";
  return fallback ? endSentence(fallback) : "";
}

function rowEpoch(row: HistoryRow): number {
  return Number(row.created_at || row.activity_at || 0);
}

function rowTime(row: HistoryRow): string {
  const epoch = rowEpoch(row);
  if (!epoch) return "";
  return new Date(epoch * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

// "Today" / "Yesterday" / weekday+date section headers, from the viewer's
// local calendar (created_at is a UTC epoch; the grouping question a person
// actually asks is "what happened today *for me*").
function dayLabel(row: HistoryRow): string {
  const epoch = rowEpoch(row);
  if (!epoch) return "Earlier";
  const date = new Date(epoch * 1000);
  const today = new Date();
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diffDays = Math.round((startOfDay(today) - startOfDay(date)) / 86400000);
  if (diffDays <= 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return date.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}

function pageNumbers(current: number, pageCount: number): (number | "gap")[] {
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, i) => i + 1);
  const pages = new Set<number>([1, 2, current - 1, current, current + 1, pageCount - 1, pageCount]);
  const sorted = [...pages].filter((p) => p >= 1 && p <= pageCount).sort((a, b) => a - b);
  const out: (number | "gap")[] = [];
  let prev = 0;
  for (const page of sorted) {
    if (prev && page - prev > 1) out.push("gap");
    out.push(page);
    prev = page;
  }
  return out;
}

function buildEndpoint(offset: number, limit: number, historyFilter: string, historySearch?: string | null): string {
  const params = new URLSearchParams({
    limit: String(limit),
    summary: "compact",
    rows: "thin",
    offset: String(offset),
    history_filter: historyFilter || "activity",
  });
  if (historySearch) params.set("history_search", historySearch);
  return `/api/inkdrop-state/history?${params.toString()}`;
}

export function History({ payload }: { payload: HistoryViewPayload }) {
  const [rows, setRows] = useState<HistoryRow[]>(payload.rows || []);
  const [offset, setOffset] = useState(payload.offset || 0);
  const [totalCount, setTotalCount] = useState(payload.total_count || 0);
  const [historyFilter, setHistoryFilter] = useState(payload.history_filter || "activity");
  const [historySearch, setHistorySearch] = useState(payload.history_search || "");
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // A fresh `payload` reference only arrives when the surrounding shell
  // re-fetched page one on our behalf (filter change, search box, section
  // re-entry) -- treat it as the new page-one truth every time, same as
  // Wanted/Queue/Blocklist.
  useEffect(() => {
    setRows(payload.rows || []);
    setOffset(payload.offset || 0);
    setTotalCount(payload.total_count || 0);
    setHistoryFilter(payload.history_filter || "activity");
    setHistorySearch(payload.history_search || "");
    // The shell's first paint may use its own limit (40 for History) --
    // page math has to follow the limit the rows were actually fetched
    // with, or "Showing 1-40" pairs with 80-row page arithmetic.
    setPageSize(Number(payload.limit) > 0 ? Number(payload.limit) : DEFAULT_PAGE_SIZE);
    setError(null);
    setExpandedId(null);
  }, [payload]);

  async function loadPage(nextOffset: number, nextPageSize?: number) {
    const limit = nextPageSize ?? pageSize;
    setLoading(true);
    setError(null);
    try {
      const data = await request<{ ok: boolean; view: HistoryViewPayload }>(
        buildEndpoint(nextOffset, limit, historyFilter, historySearch),
      );
      const view = data.view;
      setRows(view.rows || []);
      setOffset(view.offset ?? nextOffset);
      setTotalCount(view.total_count || 0);
      setExpandedId(null);
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not load History page.");
    } finally {
      setLoading(false);
    }
  }

  function changePageSize(next: number) {
    setPageSize(next);
    void loadPage(0, next);
  }

  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + rows.length;
  const pageCount = Math.max(1, Math.ceil(totalCount / pageSize));
  const currentPage = Math.floor(offset / pageSize) + 1;

  let previousDay = "";

  return (
    <div className="inkdrop-react-history">
      {error && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error}
        </div>
      )}
      <table className="arr-table history-table">
        <thead>
          <tr>
            <th className="history-col-time">Time</th>
            <th>Event</th>
            <th>Series / issue</th>
            <th>Result</th>
            <th>Source</th>
            <th>Details</th>
            <th className="history-col-chevron" aria-label="Expand" />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const day = dayLabel(row);
            const showDayHeader = day !== previousDay;
            previousDay = day;
            const pill = resultPill(row);
            const event = eventInfo(row);
            const expanded = expandedId === row.id;
            return (
              <Fragment key={row.id}>
                {showDayHeader && (
                  <tr className="history-day-row">
                    <td colSpan={7}>{day}</td>
                  </tr>
                )}
                <tr
                  className={`history-row ${expanded ? "expanded" : ""}`}
                  onClick={() => setExpandedId(expanded ? null : row.id)}
                >
                  <td data-label="Time" className="history-col-time">{rowTime(row)}</td>
                  <td data-label="Event">
                    <span className="history-event">
                      <span className="history-event-icon" data-icon={event.icon} aria-hidden="true" />
                      <strong>{event.label}</strong>
                    </span>
                  </td>
                  <td data-label="Series / issue">{seriesTitle(row)}</td>
                  <td data-label="Result">
                    <span className={`core-state history-result-pill ${pill.tone}`}>{pill.label}</span>
                  </td>
                  <td data-label="Source">{sourceLabel(row)}</td>
                  <td data-label="Details" className="history-col-details">{detailText(row)}</td>
                  <td className="history-col-chevron" aria-hidden="true">
                    <span className={`history-chevron ${expanded ? "open" : ""}`}>&rsaquo;</span>
                  </td>
                </tr>
                {expanded && (
                  <tr className="history-detail-row">
                    <td colSpan={7}>
                      <div className="history-detail-grid">
                        {row.message && (
                          <div>
                            <span>Message</span>
                            <strong>{row.message}</strong>
                          </div>
                        )}
                        {row.failure_reason && (
                          <div>
                            <span>Failure reason</span>
                            <strong>{row.failure_reason}</strong>
                          </div>
                        )}
                        {row.next_action && (
                          <div>
                            <span>Next action</span>
                            <strong>{row.next_action}</strong>
                          </div>
                        )}
                        {row.created_at_iso && (
                          <div>
                            <span>Recorded</span>
                            <strong>{row.created_at_iso}</strong>
                          </div>
                        )}
                        {row.event_type && (
                          <div>
                            <span>Event type</span>
                            <strong>{row.event_type}</strong>
                          </div>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
          {rows.length === 0 && !loading && (
            <tr>
              <td colSpan={7}>Nothing has happened yet under this filter.</td>
            </tr>
          )}
        </tbody>
      </table>
      <div className="inkdrop-react-pager history-pager">
        <span>
          {totalCount > 0 ? `Showing ${pageStart}-${pageEnd} of ${totalCount} events` : "0 events"}
        </span>
        <div className="history-pager-pages">
          <button
            type="button"
            disabled={loading || currentPage <= 1}
            onClick={() => void loadPage(Math.max(0, offset - pageSize))}
            aria-label="Previous page"
          >
            &lsaquo;
          </button>
          {pageNumbers(currentPage, pageCount).map((page, index) =>
            page === "gap" ? (
              <span key={`gap-${index}`} className="history-pager-gap">
                …
              </span>
            ) : (
              <button
                key={page}
                type="button"
                className={page === currentPage ? "active" : ""}
                disabled={loading || page === currentPage}
                onClick={() => void loadPage((page - 1) * pageSize)}
              >
                {page}
              </button>
            ),
          )}
          <button
            type="button"
            disabled={loading || currentPage >= pageCount}
            onClick={() => void loadPage(offset + pageSize)}
            aria-label="Next page"
          >
            &rsaquo;
          </button>
        </div>
        <label className="history-page-size">
          Rows per page
          <select value={pageSize} onChange={(event) => changePageSize(Number(event.target.value))}>
            {!PAGE_SIZE_OPTIONS.includes(pageSize as (typeof PAGE_SIZE_OPTIONS)[number]) && (
              <option value={pageSize}>{pageSize}</option>
            )}
            {PAGE_SIZE_OPTIONS.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}
