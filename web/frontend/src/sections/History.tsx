import { useEffect, useState } from "react";
import { request, InkDropApiError } from "../api";
import type { HistoryRow, HistoryViewPayload } from "./historyTypes";

const PAGE_SIZE = 80;

function titleCase(raw: string): string {
  return raw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// A reduced version of inkdropHistoryEventLabel's (inkdrop_web.py) regex
// ladder -- the common event kinds, not every niche one, matching the level
// of fidelity the Wanted/Queue islands already used for their own stage
// labels rather than porting every vanilla-JS label rule verbatim.
function eventLabel(row: HistoryRow): string {
  const raw = (row.event_type || row.history_kind || row.status || "event").toLowerCase();
  if (/retry/.test(raw)) return "Retry Scheduled";
  if (/search.*fail|fail.*search/.test(raw)) return "Search Failed";
  if (/search.*complete|complete.*search/.test(raw)) return "Search Completed";
  if (/search/.test(raw)) return "Search";
  if (/import.*fail|fail.*import/.test(raw)) return "Import Failed";
  if (/verified|verification/.test(raw)) return "Verified";
  if (/import/.test(raw)) return "Imported";
  if (/download.*complete|completed.*download/.test(raw)) return "Download Completed";
  if (/download/.test(raw)) return "Download Started";
  if (/grab|candidate.*accept/.test(raw)) return "Grabbed";
  if (/reject/.test(raw)) return "Candidate Rejected";
  return titleCase(raw);
}

function rowTitle(row: HistoryRow): string {
  const issue = row.issue_number ? ` #${row.issue_number}` : "";
  const seriesTitle = row.series ? `${row.series}${issue}` : "InkDrop";
  const kind = eventLabel(row);
  return kind ? `${kind} · ${seriesTitle}` : seriesTitle;
}

function resultLabel(row: HistoryRow): string {
  return titleCase(row.status || row.event_type || "history");
}

function sourceLabel(row: HistoryRow): string {
  return row.display_source || row.provider_id || row.provider_key || "";
}

// A reduced version of inkdropHistoryResultText's branches -- the common
// outcomes, falling back to whatever detail text the row already carries.
function detailText(row: HistoryRow): string {
  const raw = [row.event_type, row.history_kind, row.status, row.next_action, row.activity_summary, row.message]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  const isProblem = /problem|fail|error/.test((row.outcome || "").toLowerCase());
  if (!isProblem && /import/.test(raw) && /verif|library visible|satisfied/.test(raw)) return "Imported and verified in library";
  if (/no[_ -]?candidate|no safe candidate/.test(raw)) {
    return /retry/.test(raw) ? "No safe candidate found; retry scheduled" : "No safe candidate found";
  }
  if (/reject/.test(raw)) return "Candidate rejected";
  return row.next_action || row.activity_summary || row.message || "";
}

function buildEndpoint(offset: number, historyFilter: string, historySearch?: string | null): string {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
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
  const [hasMore, setHasMore] = useState(Boolean(payload.has_more));
  const [historyFilter, setHistoryFilter] = useState(payload.history_filter || "activity");
  const [historySearch, setHistorySearch] = useState(payload.history_search || "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A fresh `payload` reference only arrives when the surrounding shell
  // re-fetched page one on our behalf (filter change, search box, section
  // re-entry) -- treat it as the new page-one truth every time, same as
  // Wanted/Queue/Blocklist.
  useEffect(() => {
    setRows(payload.rows || []);
    setOffset(payload.offset || 0);
    setTotalCount(payload.total_count || 0);
    setHasMore(Boolean(payload.has_more));
    setHistoryFilter(payload.history_filter || "activity");
    setHistorySearch(payload.history_search || "");
    setError(null);
  }, [payload]);

  async function loadPage(nextOffset: number) {
    setLoading(true);
    setError(null);
    try {
      const data = await request<{ ok: boolean; view: HistoryViewPayload }>(
        buildEndpoint(nextOffset, historyFilter, historySearch)
      );
      const view = data.view;
      setRows(view.rows || []);
      setOffset(view.offset ?? nextOffset);
      setTotalCount(view.total_count || 0);
      setHasMore(Boolean(view.has_more));
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not load History page.");
    } finally {
      setLoading(false);
    }
  }

  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + rows.length;

  return (
    <div className="inkdrop-react-history">
      {error && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error}
        </div>
      )}
      <table className="arr-table">
        <thead>
          <tr>
            <th>Event / Series</th>
            <th>Result</th>
            <th>Source</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td data-label="Event / Series">{rowTitle(row)}</td>
              <td data-label="Result">{resultLabel(row)}</td>
              <td data-label="Source">{sourceLabel(row)}</td>
              <td data-label="Details">{detailText(row)}</td>
            </tr>
          ))}
          {rows.length === 0 && !loading && (
            <tr>
              <td colSpan={4}>Nothing has happened yet under this filter.</td>
            </tr>
          )}
        </tbody>
      </table>
      <div className="inkdrop-react-pager">
        <span>{totalCount > 0 ? `${pageStart}-${pageEnd} of ${totalCount}` : "0 of 0"}</span>
        <button type="button" disabled={loading || offset === 0} onClick={() => loadPage(Math.max(0, offset - PAGE_SIZE))}>
          Previous
        </button>
        <button type="button" disabled={loading || !hasMore} onClick={() => loadPage(offset + PAGE_SIZE)}>
          Next
        </button>
      </div>
    </div>
  );
}
