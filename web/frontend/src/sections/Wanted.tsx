import { useEffect, useState } from "react";
import { request, InkDropApiError } from "../api";
import type { WantedRow, WantedViewPayload, WantedRunResult } from "./wantedTypes";

const PAGE_SIZE = 80;

function rowTitle(row: WantedRow): string {
  const issue = row.issue_number ? ` #${row.issue_number}` : "";
  return `${row.series || "Unknown"}${issue}`;
}

function stageLabel(row: WantedRow): string {
  const raw = row.display_state_label || row.display_state || row.status || "wanted";
  return raw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Mirrors operationalRowSourceLabel's fallback chain (inkdrop_web.py) closely
// enough for a table cell -- the full version also reaches into download-task
// fields this row shape doesn't carry in "thin" mode.
function sourceLabel(row: WantedRow): string {
  const raw = row.next_concrete_source_label || row.next_source_label || row.display_source || row.current_source || row.source_key || "";
  return raw.toLowerCase() === "source" ? "" : raw;
}

// Same priority order as operationalRowDetailBits' effective precedence for
// this view: an automation/next-action summary beats a raw diagnostic, which
// beats a wait reason, which beats a generic activity line.
function nextActionText(row: WantedRow): string {
  return row.next_action || row.why_not_grabbed || row.wait_reason_label || row.activity_summary || "";
}

function buildEndpoint(offset: number, wantedFilter: string): string {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    summary: "compact",
    rows: "thin",
    offset: String(offset),
    wanted_filter: wantedFilter || "active",
  });
  return `/api/inkdrop-state/wanted?${params.toString()}`;
}

export function Wanted({ payload }: { payload: WantedViewPayload }) {
  const [rows, setRows] = useState<WantedRow[]>(payload.rows || []);
  const [offset, setOffset] = useState(payload.offset || 0);
  const [totalCount, setTotalCount] = useState(payload.total_count || 0);
  const [hasMore, setHasMore] = useState(Boolean(payload.has_more));
  const [wantedFilter, setWantedFilter] = useState(payload.wanted_filter || "active");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);

  // A fresh `payload` reference only arrives when the surrounding shell
  // re-fetched page one on our behalf (filter change, section re-entry) --
  // treat it as the new page-one truth every time, same as Blocklist.
  useEffect(() => {
    setRows(payload.rows || []);
    setOffset(payload.offset || 0);
    setTotalCount(payload.total_count || 0);
    setHasMore(Boolean(payload.has_more));
    setWantedFilter(payload.wanted_filter || "active");
    setError(null);
  }, [payload]);

  async function loadPage(nextOffset: number) {
    setLoading(true);
    setError(null);
    try {
      const data = await request<{ ok: boolean; view: WantedViewPayload }>(buildEndpoint(nextOffset, wantedFilter));
      const view = data.view;
      setRows(view.rows || []);
      setOffset(view.offset ?? nextOffset);
      setTotalCount(view.total_count || 0);
      setHasMore(Boolean(view.has_more));
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not load Wanted page.");
    } finally {
      setLoading(false);
    }
  }

  async function runSearch(row: WantedRow) {
    setPendingId(row.id);
    setError(null);
    try {
      const data = await request<WantedRunResult>("/api/inkdrop-state/wanted/run", {
        method: "POST",
        body: { id: row.id },
      });
      if (!data.ok) throw new InkDropApiError("Could not queue this search.", { status: 200, code: "wanted_run_failed" });
      // A queued search can move the row to a different status bucket
      // (wanted -> in_progress), which can drop it out of the current
      // filter/page entirely -- reload the current page rather than assume
      // the row is still here, same reasoning as Blocklist's Allow & Retry.
      await loadPage(offset);
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not queue this search.");
    } finally {
      setPendingId(null);
    }
  }

  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + rows.length;

  return (
    <div className="inkdrop-react-wanted">
      {error && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error}
        </div>
      )}
      <table className="arr-table">
        <thead>
          <tr>
            <th>Series / Issue</th>
            <th>Current stage</th>
            <th>Source</th>
            <th>Next action</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const canAct = Boolean(row.id) && row.status !== "satisfied";
            const label = row.queue_state === "downloading" || row.queue_state === "importing" ? "Refresh" : "Search";
            return (
              <tr key={row.id}>
                <td data-label="Series / Issue">{rowTitle(row)}</td>
                <td data-label="Current stage">{stageLabel(row)}</td>
                <td data-label="Source">{sourceLabel(row)}</td>
                <td data-label="Next action">{nextActionText(row)}</td>
                <td data-label="Actions">
                  {canAct && (
                    <button
                      type="button"
                      disabled={pendingId === row.id}
                      onClick={() => runSearch(row)}
                      title="Queue and search this wanted item"
                    >
                      {pendingId === row.id ? "Queuing…" : label}
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
          {rows.length === 0 && !loading && (
            <tr>
              <td colSpan={5}>Nothing wanted in this view.</td>
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
