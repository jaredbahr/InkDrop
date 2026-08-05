import { useEffect, useState } from "react";
import { request, InkDropApiError } from "../api";
import type { QueueRow, QueueViewPayload, QueueRunResult } from "./queueTypes";

const PAGE_SIZE = 80;

function rowTitle(row: QueueRow): string {
  const issue = row.issue_number ? ` #${row.issue_number}` : "";
  return `${row.series || "Unknown"}${issue}`;
}

function stageLabel(row: QueueRow): string {
  const raw = row.display_state_label || row.display_state || row.state || "queued";
  return raw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Mirrors operationalRowSourceLabel's fallback chain (inkdrop_web.py) closely
// enough for a table cell -- the full version also reaches into download-task
// fields this row shape doesn't carry in "thin" mode.
function sourceLabel(row: QueueRow): string {
  const raw = row.next_concrete_source_label || row.next_source_label || row.display_source || row.current_source || row.source_key || "";
  return raw.toLowerCase() === "source" ? "" : raw;
}

// Same priority order as operationalRowDetailBits' effective precedence for
// this view: an automation/next-action summary beats a raw diagnostic, which
// beats a wait reason, which beats a generic activity line.
function nextActionText(row: QueueRow): string {
  return row.next_action || row.why_not_grabbed || row.wait_reason_label || row.activity_summary || "";
}

function buildEndpoint(offset: number, queueFilter: string): string {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    summary: "compact",
    rows: "thin",
    offset: String(offset),
    queue_filter: queueFilter || "active",
  });
  return `/api/inkdrop-state/queue?${params.toString()}`;
}

export function Queue({ payload }: { payload: QueueViewPayload }) {
  const [rows, setRows] = useState<QueueRow[]>(payload.rows || []);
  const [offset, setOffset] = useState(payload.offset || 0);
  const [totalCount, setTotalCount] = useState(payload.total_count || 0);
  const [hasMore, setHasMore] = useState(Boolean(payload.has_more));
  const [queueFilter, setQueueFilter] = useState(payload.queue_filter || "active");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);

  // A fresh `payload` reference only arrives when the surrounding shell
  // re-fetched page one on our behalf (filter change, section re-entry) --
  // treat it as the new page-one truth every time, same as Wanted/Blocklist.
  useEffect(() => {
    setRows(payload.rows || []);
    setOffset(payload.offset || 0);
    setTotalCount(payload.total_count || 0);
    setHasMore(Boolean(payload.has_more));
    setQueueFilter(payload.queue_filter || "active");
    setError(null);
  }, [payload]);

  async function loadPage(nextOffset: number) {
    setLoading(true);
    setError(null);
    try {
      const data = await request<{ ok: boolean; view: QueueViewPayload }>(buildEndpoint(nextOffset, queueFilter));
      const view = data.view;
      setRows(view.rows || []);
      setOffset(view.offset ?? nextOffset);
      setTotalCount(view.total_count || 0);
      setHasMore(Boolean(view.has_more));
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not load Queue page.");
    } finally {
      setLoading(false);
    }
  }

  async function runRetry(row: QueueRow) {
    setPendingId(row.id);
    setError(null);
    try {
      const data = await request<QueueRunResult>("/api/inkdrop-state/queue/run", {
        method: "POST",
        body: { id: row.id },
      });
      if (!data.ok) throw new InkDropApiError("Could not retry this queue row.", { status: 200, code: "queue_run_failed" });
      // A retry can move the row to a different state bucket (queued ->
      // in_progress, or off the active-only default filter entirely) --
      // reload the current page rather than assume the row is still here,
      // same reasoning as Wanted's Search action.
      await loadPage(offset);
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not retry this queue row.");
    } finally {
      setPendingId(null);
    }
  }

  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + rows.length;

  return (
    <div className="inkdrop-react-queue">
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
            const canAct = Boolean(row.id);
            const label = row.state === "downloading" || row.state === "importing" ? "Refresh" : "Retry";
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
                      onClick={() => runRetry(row)}
                      title="Retry or refresh this queue row"
                    >
                      {pendingId === row.id ? "Retrying…" : label}
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
          {rows.length === 0 && !loading && (
            <tr>
              <td colSpan={5}>Nothing in the queue for this view.</td>
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
