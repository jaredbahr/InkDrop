import { useEffect, useMemo, useRef, useState } from "react";
import { request, InkDropApiError } from "../api";
import { useRowActions } from "../rowActions";
import type { BlocklistRow, BlocklistViewPayload, AllowCandidateResult, BlocklistReasonMeta } from "./types";

const PAGE_SIZE = 80;

const ADDED_OPTIONS = [
  { value: "", label: "Any time" },
  { value: "1", label: "Last 24 hours" },
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
];

type Filters = {
  reason: string;
  source: string;
  added: string;
  q: string;
};

const DEFAULT_FILTERS: Filters = { reason: "", source: "", added: "", q: "" };

// Module-level, not component state: a full shell-driven section reload
// (navigating away and back, or the detail panel's own mutations calling
// loadInkdropSection with keepExisting:false) unmounts and remounts this
// island, which would silently clear whatever the user had filtered to.
let persistedFilters: Filters = DEFAULT_FILTERS;

// Compound filter string understood by bad_source_candidate_filter_sql
// (inkdrop_state.py): comma-separated terms, ANDed server-side. Commas are
// stripped from the free-text term because they are the term separator.
function compoundFilter(filters: Filters): string {
  const parts: string[] = [];
  if (filters.reason) parts.push(`reason:${filters.reason}`);
  if (filters.source) parts.push(`source:${filters.source}`);
  if (filters.added) parts.push(`added:${filters.added}`);
  const q = filters.q.trim().replace(/,/g, " ");
  if (q) parts.push(`q:${q}`);
  return parts.length ? parts.join(",") : "all";
}

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

function rowTitle(row: BlocklistRow): string {
  return row.linked_entities?.display_title || row.linked_entities?.series || row.series || "Unlinked item";
}

function rowSubtitle(row: BlocklistRow): string {
  return row.linked_entities?.issue_title || "";
}

function reasonMeta(row: BlocklistRow): BlocklistReasonMeta {
  const bridge = window.InkDropSourceMemory;
  if (bridge?.reasonMeta) return bridge.reasonMeta(row.reason, row.reason_label);
  return {
    label: row.reason_label || row.reason || "Blocked",
    tone: "warn",
    detail: "This release failed checks and is skipped during automatic searches.",
  };
}

// Archive-format chip (CBR/CBZ/ZIP/...) derived from the candidate's path or
// title extension -- the blocklist row records no explicit format field.
function formatTag(row: BlocklistRow): string {
  const haystacks = [row.source_path || "", row.title || "", row.normalized_title || ""];
  for (const text of haystacks) {
    const match = /\.(cbz|cbr|cb7|zip|rar|7z|pdf|epub|nzb)\s*$/i.exec(text.trim());
    if (match) return match[1].toUpperCase();
  }
  return "";
}

function coverUrl(row: BlocklistRow): string {
  const image = row.linked_entities?.image || "";
  if (!image) return "";
  return window.InkDropSourceMemory?.coverProxyUrl?.(image) || "";
}

function relativeAge(row: BlocklistRow): string {
  const stamp = Number(row.activity_at || row.last_seen_at || 0);
  if (!stamp) return "";
  const seconds = Math.max(0, Date.now() / 1000 - stamp);
  if (seconds < 90) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 60) return `${days}d ago`;
  const months = Math.floor(days / 30);
  return `${months}mo ago`;
}

function optionLabel(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Numbered page list with ellipsis gaps: always page 1 and the last page,
// plus a window around the current page. Matches the mockup's
// "1 2 3 … 986" pager.
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

export function Blocklist({ payload }: { payload: BlocklistViewPayload }) {
  const [rows, setRows] = useState<BlocklistRow[]>(payload.rows || []);
  const [offset, setOffset] = useState(payload.offset || 0);
  const [totalCount, setTotalCount] = useState(payload.total_count || 0);
  const [filters, setFiltersState] = useState<Filters>(persistedFilters);
  const setFilters = (next: Filters | ((prev: Filters) => Filters)) => {
    setFiltersState((prev) => {
      const resolved = typeof next === "function" ? next(prev) : next;
      persistedFilters = resolved;
      return resolved;
    });
  };
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { pendingIds, doneIds, actionError, runRowAction } = useRowActions(() => loadPage(offset));
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const summary = payload.summary || {};
  const activeFilterRef = useRef(compoundFilter(persistedFilters));
  const searchDebounceRef = useRef(0);
  const filtersRef = useRef<Filters>(persistedFilters);
  filtersRef.current = filters;

  const reasonOptions = useMemo(
    () => Object.entries(summary.bad_source_candidates_by_reason || {}).sort((a, b) => b[1] - a[1]),
    [summary],
  );
  const sourceOptions = useMemo(() => {
    const merged: Record<string, number> = { ...(summary.bad_source_candidates_by_provider || {}) };
    for (const [key, count] of Object.entries(summary.bad_source_candidates_by_source || {})) {
      merged[key] = Math.max(merged[key] || 0, count);
    }
    return Object.entries(merged).sort((a, b) => b[1] - a[1]);
  }, [summary]);

  // A fresh `payload` reference arrives when the shell re-fetched page one on
  // our behalf (section re-entry, refresh button, post-mutation reload). If
  // the user has filters active, the shell's unfiltered page one would
  // silently discard them -- refetch with the active compound filter instead.
  useEffect(() => {
    if (activeFilterRef.current === "all") {
      setRows(payload.rows || []);
      setOffset(payload.offset || 0);
      setTotalCount(payload.total_count || 0);
      setError(null);
    } else {
      void loadPage(0, activeFilterRef.current);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload]);

  async function loadPage(nextOffset: number, sourceFilter?: string) {
    const filter = sourceFilter ?? activeFilterRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await request<{ ok: boolean; view: BlocklistViewPayload }>(buildEndpoint(nextOffset, filter));
      const view = data.view;
      setRows(view.rows || []);
      setOffset(view.offset ?? nextOffset);
      setTotalCount(view.total_count || 0);
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not load Blocklist page.");
    } finally {
      setLoading(false);
    }
  }

  function applyFilters(next: Filters) {
    setFilters(next);
    const compound = compoundFilter(next);
    activeFilterRef.current = compound;
    void loadPage(0, compound);
  }

  function onSearchInput(value: string) {
    setFilters((prev) => ({ ...prev, q: value }));
    window.clearTimeout(searchDebounceRef.current);
    searchDebounceRef.current = window.setTimeout(() => {
      // filtersRef, not the render-scoped `filters` -- a dropdown change
      // made during the debounce window would otherwise be reverted here.
      const compound = compoundFilter(filtersRef.current);
      activeFilterRef.current = compound;
      void loadPage(0, compound);
    }, 300);
  }

  function openDetail(row: BlocklistRow) {
    setSelectedId(row.id);
    window.InkDropSourceMemory?.openDetailPanel(row);
  }

  // Both actions ride the shared hook: rapid clicks across rows each keep
  // their own pending state, and the page reloads once per burst (removing
  // one row shifts every row after it, so a per-click reload reshuffled the
  // table under the cursor).
  function allowCandidate(row: BlocklistRow) {
    void runRowAction(row.id, row.title || row.normalized_title || "This release", "Allowed", async () => {
      const data = await request<AllowCandidateResult>("/api/inkdrop-state/source-memory/allow", {
        method: "POST",
        body: { id: row.id, revision: row.revision, decision: "allow_and_retry" },
      });
      if (!data.ok) throw new InkDropApiError("Could not allow this candidate.", { status: 200, code: "allow_failed" });
      if (selectedId === row.id) window.InkDropSourceMemory?.closeDetailPanel();
    });
  }

  function removeCandidate(row: BlocklistRow) {
    const title = row.title || row.normalized_title || "this release";
    if (!window.confirm(`Remove ${title} from Blocklist? InkDrop forgets why it was blocked.`)) return;
    void runRowAction(row.id, title, "Removed", async () => {
      const data = await request<AllowCandidateResult>("/api/inkdrop-state/source-memory/remove", {
        method: "POST",
        body: { id: row.id, revision: row.revision },
      });
      if (!data.ok) throw new InkDropApiError("Could not remove this candidate.", { status: 200, code: "remove_failed" });
      if (selectedId === row.id) window.InkDropSourceMemory?.closeDetailPanel();
    });
  }

  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + rows.length;
  const pageCount = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div className="inkdrop-react-blocklist">
      {(error || actionError) && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error || actionError}
        </div>
      )}
      <div className="blocklist-controls">
        <input
          type="search"
          className="arr-table-text-filter blocklist-search"
          placeholder="Search blocked releases"
          aria-label="Search blocked releases"
          value={filters.q}
          onChange={(event) => onSearchInput(event.target.value)}
        />
        <label className="blocklist-filter-select">
          <span>Reason</span>
          <select value={filters.reason} onChange={(event) => applyFilters({ ...filters, reason: event.target.value })}>
            <option value="">All</option>
            {reasonOptions.map(([value, count]) => (
              <option key={value} value={value}>
                {optionLabel(value)} ({count})
              </option>
            ))}
          </select>
        </label>
        <label className="blocklist-filter-select">
          <span>Source</span>
          <select value={filters.source} onChange={(event) => applyFilters({ ...filters, source: event.target.value })}>
            <option value="">All</option>
            {sourceOptions.map(([value, count]) => (
              <option key={value} value={value}>
                {optionLabel(value)} ({count})
              </option>
            ))}
          </select>
        </label>
        <label className="blocklist-filter-select">
          <span>Added</span>
          <select value={filters.added} onChange={(event) => applyFilters({ ...filters, added: event.target.value })}>
            {ADDED_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="blocklist-info-banner">
        InkDrop remembers failed or unsafe candidates so later searches do not repeat the same mistake.
      </div>
      <table className="arr-table blocklist-table">
        <thead>
          <tr>
            <th>Series / Issue</th>
            <th>Release candidate</th>
            <th>Why blocked</th>
            <th>Source</th>
            <th>Added</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const meta = reasonMeta(row);
            const tag = formatTag(row);
            const cover = coverUrl(row);
            return (
              <tr
                key={row.id}
                className={`blocklist-row ${selectedId === row.id ? "selected-detail-row" : ""}`}
                data-arr-row-id={row.id}
                onClick={(event) => {
                  const target = event.target as HTMLElement;
                  if (target.closest("button, a, summary, details, input, select")) return;
                  openDetail(row);
                }}
              >
                <td data-label="Series / Issue">
                  <div className="blocklist-series-cell">
                    {cover ? (
                      <img className="blocklist-cover" src={cover} alt="" loading="lazy" />
                    ) : (
                      <span className="blocklist-cover blocklist-cover-empty" aria-hidden="true">
                        {(rowTitle(row).match(/[A-Za-z0-9]/)?.[0] || "?").toUpperCase()}
                      </span>
                    )}
                    <div className="blocklist-series-text">
                      <strong>{rowTitle(row)}</strong>
                      {rowSubtitle(row) && <span className="mini">{rowSubtitle(row)}</span>}
                    </div>
                  </div>
                </td>
                <td data-label="Release candidate">
                  <div className="blocklist-candidate-cell">
                    <span className="blocklist-candidate-title">{row.title || row.normalized_title || ""}</span>
                    {tag && <span className="blocklist-format-tag">{tag}</span>}
                  </div>
                </td>
                <td data-label="Why blocked">
                  <div className="blocklist-reason-cell">
                    <span className={`core-state blocklist-reason-badge ${meta.tone}`}>{meta.label}</span>
                    <span className="mini">{meta.detail}</span>
                  </div>
                </td>
                <td data-label="Source">{row.source_label || ""}</td>
                <td data-label="Added">{relativeAge(row)}</td>
                <td data-label="Actions">
                  <div className="blocklist-row-actions">
                    <button
                      type="button"
                      className="blocklist-allow-btn"
                      disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                      onClick={() => void allowCandidate(row)}
                      title="Remove this exact release from Blocklist and retry its linked wanted item"
                    >
                      {pendingIds.has(row.id) ? "Working…" : doneIds.get(row.id) || "Allow & retry"}
                    </button>
                    <button
                      type="button"
                      onClick={() => openDetail(row)}
                      title="Open this candidate's full detail in the side panel"
                    >
                      Inspect
                    </button>
                    <button
                      type="button"
                      className="blocklist-remove-btn"
                      disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                      onClick={() => void removeCandidate(row)}
                      title="Remove permanently: forgets this Blocklist entry without allowing the release"
                    >
                      Remove
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
          {rows.length === 0 && !loading && (
            <tr>
              <td colSpan={6}>No blocked candidates in this view.</td>
            </tr>
          )}
        </tbody>
      </table>
      <div className="inkdrop-react-pager blocklist-pager">
        <span>
          {totalCount > 0 ? `Showing ${pageStart}-${pageEnd} of ${totalCount} results` : "0 results"}
        </span>
        <div className="blocklist-pager-pages">
          <button
            type="button"
            disabled={loading || currentPage <= 1}
            onClick={() => void loadPage(Math.max(0, offset - PAGE_SIZE))}
            aria-label="Previous page"
          >
            &lsaquo;
          </button>
          {pageNumbers(currentPage, pageCount).map((page, index) =>
            page === "gap" ? (
              <span key={`gap-${index}`} className="blocklist-pager-gap">
                …
              </span>
            ) : (
              <button
                key={page}
                type="button"
                className={page === currentPage ? "active" : ""}
                disabled={loading || page === currentPage}
                onClick={() => void loadPage((page - 1) * PAGE_SIZE)}
              >
                {page}
              </button>
            ),
          )}
          <button
            type="button"
            disabled={loading || currentPage >= pageCount}
            onClick={() => void loadPage(offset + PAGE_SIZE)}
            aria-label="Next page"
          >
            &rsaquo;
          </button>
        </div>
      </div>
    </div>
  );
}
