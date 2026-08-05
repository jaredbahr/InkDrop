import { useEffect, useState } from "react";
import { request, InkDropApiError } from "../api";
import type { BlocklistRow, BlocklistViewPayload, AllowCandidateResult } from "./types";

const PAGE_SIZE = 80;

function formatFailureCount(row: BlocklistRow): string {
  const count = Number(row.failure_count || 0);
  if (!count) return "";
  return `${count} blocked attempt${count === 1 ? "" : "s"}`;
}

function rowTitle(row: BlocklistRow): string {
  return row.linked_entities?.display_title || row.linked_entities?.series || row.series || "Unlinked item";
}

// Mirrors renderInkdropSection's endpoint construction for this view
// (inkdrop_web.py's inkdropSectionEndpoint) so a page-2 request looks
// identical to a first-paint request to the server, just with a real offset.
function buildEndpoint(offset: number, sourceFilter: string): string {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    summary: "compact",
    rows: "compact",
    offset: String(offset),
    source_filter: sourceFilter || "all",
  });
  return `/api/inkdrop-state/source_memory?${params.toString()}`;
}

export function Blocklist({ payload }: { payload: BlocklistViewPayload }) {
  const [rows, setRows] = useState<BlocklistRow[]>(payload.rows || []);
  const [offset, setOffset] = useState(payload.offset || 0);
  const [totalCount, setTotalCount] = useState(payload.total_count || 0);
  const [hasMore, setHasMore] = useState(Boolean(payload.has_more));
  const [sourceFilter, setSourceFilter] = useState(payload.source_filter || "all");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);

  // A fresh `payload` reference only ever arrives when the surrounding shell
  // re-fetched page one on our behalf (filter change, section re-entry,
  // periodic refresh) -- treat it as the new page-one truth every time.
  useEffect(() => {
    setRows(payload.rows || []);
    setOffset(payload.offset || 0);
    setTotalCount(payload.total_count || 0);
    setHasMore(Boolean(payload.has_more));
    setSourceFilter(payload.source_filter || "all");
    setError(null);
  }, [payload]);

  async function loadPage(nextOffset: number) {
    setLoading(true);
    setError(null);
    try {
      const data = await request<{ ok: boolean; view: BlocklistViewPayload }>(buildEndpoint(nextOffset, sourceFilter));
      const view = data.view;
      setRows(view.rows || []);
      setOffset(view.offset ?? nextOffset);
      setTotalCount(view.total_count || 0);
      setHasMore(Boolean(view.has_more));
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not load Blocklist page.");
    } finally {
      setLoading(false);
    }
  }

  async function allowCandidate(row: BlocklistRow) {
    setPendingId(row.id);
    setError(null);
    try {
      const data = await request<AllowCandidateResult>("/api/inkdrop-state/source-memory/allow", {
        method: "POST",
        body: { id: row.id },
      });
      if (!data.ok) throw new InkDropApiError("Could not allow this candidate.", { status: 200, code: "allow_failed" });
      // Removed from Blocklist -- reload the current page rather than just
      // splicing it out locally, since removing one row shifts every row
      // after it by one position (the same reason a client-side "delete and
      // keep paging" approach silently skips or repeats rows elsewhere in
      // this app).
      await loadPage(offset);
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not allow this candidate.");
    } finally {
      setPendingId(null);
    }
  }

  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + rows.length;

  return (
    <div className="inkdrop-react-blocklist">
      {error && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error}
        </div>
      )}
      <table className="arr-table">
        <thead>
          <tr>
            <th>Series / Issue</th>
            <th>Blocked reason</th>
            <th>Source</th>
            <th>Release candidate</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td data-label="Series / Issue">{rowTitle(row)}</td>
              <td data-label="Blocked reason">{row.reason_label || row.reason || ""}</td>
              <td data-label="Source">{row.source_label || ""}</td>
              <td data-label="Release candidate">
                {[row.title ? `Release: ${row.title}` : "", formatFailureCount(row)].filter(Boolean).join(" · ")}
              </td>
              <td data-label="Actions">
                <button
                  type="button"
                  disabled={pendingId === row.id}
                  onClick={() => allowCandidate(row)}
                  title="Remove this exact release from Blocklist and retry its linked wanted item"
                >
                  {pendingId === row.id ? "Allowing…" : "Allow & Retry"}
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && !loading && (
            <tr>
              <td colSpan={5}>No blocked candidates in this view.</td>
            </tr>
          )}
        </tbody>
      </table>
      <div className="inkdrop-react-pager">
        <span>
          {totalCount > 0 ? `${pageStart}-${pageEnd} of ${totalCount}` : "0 of 0"}
        </span>
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
