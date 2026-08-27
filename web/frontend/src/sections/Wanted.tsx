import { useEffect, useState } from "react";
import { request, InkDropApiError } from "../api";
import { useRowActions } from "../rowActions";
import type { WantedRow, WantedViewPayload, WantedRunResult } from "./wantedTypes";
import { rowStateLabel } from "./stateLabel";

const PAGE_SIZE = 80;

function rowTitle(row: WantedRow): string {
  const issue = row.issue_number ? ` #${row.issue_number}` : "";
  return `${row.series || "Unknown"}${issue}`;
}

const stageLabel = (row: WantedRow): string => rowStateLabel(row, "wanted");

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

function canSelectRow(row: WantedRow): boolean {
  return Boolean(row.id) && row.status !== "satisfied";
}

// The vanilla shell's own "Search Selected"/"Manual Search" toolbar
// (appendArrTableButton in inkdrop_web.py, requiresSelection: true) never
// actually renders for this view -- window.InkDropReact.mount() early-
// returns renderInkdropSection() before that code path is ever reached, for
// every React-owned section, not just this one. Confirmed live against a
// real running instance: no .arr-table-controlbar-wanted node exists in the
// DOM at all. So this isn't reconnecting a disabled button to a live wire;
// there is no wire, and no button. The toolbar has to live here.
const shell = window as unknown as {
  InkDropWantedNav?: {
    runSelectedSearches?: (rows: WantedRow[]) => Promise<void>;
  };
  InkDropSeriesNav?: {
    // openManualSearchForRow (inkdrop_web.py) only reads series_id/issue_id/
    // unit_id/edition_id/series/title/issue_number off the row it's given --
    // it doesn't care which view's row shape called it. Reused as-is here,
    // same reasoning SeriesDetail.tsx already applies to this same bridge.
    openManualSearch?: (row: WantedRow) => boolean;
  };
};

export function Wanted({ payload }: { payload: WantedViewPayload }) {
  const [rows, setRows] = useState<WantedRow[]>(payload.rows || []);
  const [offset, setOffset] = useState(payload.offset || 0);
  const [totalCount, setTotalCount] = useState(payload.total_count || 0);
  const [hasMore, setHasMore] = useState(Boolean(payload.has_more));
  const [wantedFilter, setWantedFilter] = useState(payload.wanted_filter || "active");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [runningSelected, setRunningSelected] = useState(false);
  const { pendingIds, doneIds, actionError, clearActionError, runRowAction } = useRowActions(() => loadPage(offset));

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
    clearActionError();
    // A fresh payload means the previously selected rows may no longer be on
    // this page -- stale ids left checked would silently no-op on the next
    // bulk action. Same reasoning as ManualReview.tsx.
    setSelectedIds(new Set());
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

  // A queued search can move its row to a different status bucket, which can
  // drop it out of the current filter/page -- the shared hook reloads once
  // per click-burst after the last in-flight search settles.
  function runSearch(row: WantedRow) {
    void runRowAction(row.id, rowTitle(row), "Queued", async () => {
      const data = await request<WantedRunResult>("/api/inkdrop-state/wanted/run", {
        method: "POST",
        body: { id: row.id, revision: row.revision },
      });
      if (!data.ok) throw new InkDropApiError("Could not queue this search.", { status: 200, code: "wanted_run_failed" });
    });
  }

  const selectableRows = rows.filter(canSelectRow);
  const selectedCount = selectedIds.size;
  const allSelectableSelected = selectableRows.length > 0 && selectableRows.every((row) => selectedIds.has(row.id));

  function toggleRowSelected(rowId: string, checked: boolean) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(rowId);
      else next.delete(rowId);
      return next;
    });
  }

  function toggleSelectAll(checked: boolean) {
    setSelectedIds(checked ? new Set(selectableRows.map((row) => row.id)) : new Set());
  }

  // The confirm-free loop, the quiet per-item queueing, the single summary
  // toast, and the final loadInkdropSection("wanted", ...) refresh all
  // already live in the vanilla shell's runSelectedWantedSearches() (also
  // used before this table existed) -- reused as-is via the bridge rather
  // than reimplemented, same reasoning ManualReview.tsx's bulkIgnore bridge
  // documents. That refresh re-mounts this island with fresh rows, so no
  // extra loadPage() call is needed here once it resolves.
  async function runSelectedSearches() {
    const selected = rows.filter((row) => selectedIds.has(row.id));
    if (!selected.length || !shell.InkDropWantedNav?.runSelectedSearches) return;
    setRunningSelected(true);
    try {
      await shell.InkDropWantedNav.runSelectedSearches(selected);
    } finally {
      setRunningSelected(false);
    }
  }

  function manualSearchSelected() {
    const selected = rows.filter((row) => selectedIds.has(row.id));
    if (selected.length !== 1) return;
    shell.InkDropSeriesNav?.openManualSearch?.(selected[0]);
  }

  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + rows.length;

  return (
    <div className="inkdrop-react-wanted">
      {(error || actionError) && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error || actionError}
        </div>
      )}
      <div className="arr-table-controlbar arr-table-controlbar-wanted">
        <div className="arr-table-controlbar-left">
          <button
            type="button"
            disabled={selectedCount < 1 || runningSelected}
            onClick={() => void runSelectedSearches()}
            title={selectedCount < 1 ? "Select one or more visible rows first." : "Queue searches for selected visible Wanted rows"}
          >
            {runningSelected ? "Queuing…" : "Search Selected"}
          </button>
          <button
            type="button"
            disabled={selectedCount !== 1}
            onClick={manualSearchSelected}
            title={selectedCount !== 1 ? "Select exactly one Wanted row for Manual Search." : "Search providers for the single selected Wanted row"}
          >
            Manual Search
          </button>
          <span className="arr-table-selection-count">{selectedCount} selected</span>
        </div>
      </div>
      <table className="arr-table wanted-table">
        <thead>
          <tr>
            <th>
              <input
                type="checkbox"
                aria-label="Select all visible rows"
                checked={allSelectableSelected}
                disabled={selectableRows.length === 0}
                onChange={(event) => toggleSelectAll(event.target.checked)}
              />
            </th>
            <th>Series / Issue</th>
            <th>Current stage</th>
            <th>Source</th>
            <th>Next action</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const canAct = canSelectRow(row);
            const label = row.queue_state === "downloading" || row.queue_state === "importing" ? "Refresh" : "Search";
            return (
              <tr key={row.id}>
                <td data-label="Select">
                  {canAct && (
                    <input
                      type="checkbox"
                      data-arr-row-id={row.id}
                      aria-label={`Select ${rowTitle(row)}`}
                      checked={selectedIds.has(row.id)}
                      onChange={(event) => toggleRowSelected(row.id, event.target.checked)}
                    />
                  )}
                </td>
                <td data-label="Series / Issue">{rowTitle(row)}</td>
                <td data-label="Current stage">{stageLabel(row)}</td>
                <td data-label="Source">{sourceLabel(row)}</td>
                <td data-label="Next action">{nextActionText(row)}</td>
                <td data-label="Actions">
                  {canAct && (
                    <button
                      type="button"
                      disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                      onClick={() => runSearch(row)}
                      title="Queue and search this wanted item"
                    >
                      {pendingIds.has(row.id) ? "Queuing…" : doneIds.get(row.id) || label}
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
          {rows.length === 0 && !loading && (
            <tr>
              <td colSpan={6}>Nothing wanted in this view.</td>
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
