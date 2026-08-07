import { useEffect, useState } from "react";
import { request, InkDropApiError } from "../api";
import type { ManualReviewRow, ManualReviewViewPayload } from "./manualReviewTypes";

// 10 per page: each row is a decision, not a log line -- a short page
// keeps the docked decision panel beside the rows it belongs to.
const PAGE_SIZE = 10;

// Port of inkdropManualReviewDecisionText/inkdropManualReviewIsAutomaticWork/
// inkdropManualReviewIsHumanDecision (inkdrop_web.py). The vanilla table
// filters rows to human decisions only before rendering
// (renderInkdropSectionTable's "manual_review" branch) -- this island owns
// the table now, so it must apply the exact same filter itself, or rows the
// vanilla shell would have hidden (automatic in-progress work, not actually
// awaiting a person) would appear here instead.
function decisionText(row: ManualReviewRow): string {
  return [
    row.state,
    row.status,
    row.display_state,
    row.activity_summary,
    row.next_action,
    row.review_reason,
    row.manual_source_stage,
    row.reason,
  ]
    .filter((value) => value !== undefined && value !== null)
    .map((value) => String(value).toLowerCase())
    .join(" ");
}

function isAutomaticWork(row: ManualReviewRow): boolean {
  if (row.manual_review_parked === true || row.manual_review_actionable === false) return true;
  const text = decisionText(row);
  return /provider[_\s-]?wait|source[_\s-]?wait|queued remote|queued|retry due|retry scheduled|retry later|backoff|no[_\s-]?candidate|no candidates|candidate recovery|source cooldown|waiting on provider|download(?:ing)?|import(?:ing)?|verif(?:y|ying)|automatic|queue active|remote transfer|reader scan/.test(
    text,
  );
}

function isHumanDecision(row: ManualReviewRow): boolean {
  if (isAutomaticWork(row)) return false;
  if (row.manual_review_actionable === true) return true;
  const text = decisionText(row);
  return /approve|approval|ignore|alias|choose|manual decision|needs decision|human decision|pack_requires_review|requires_review|repair|blocked|policy_block|language_blocked|destination_conflict|ambiguous/.test(
    text,
  );
}

function rowTitle(row: ManualReviewRow): string {
  const issue = row.issue_number ? ` #${row.issue_number}` : "";
  return `${row.series || "Unknown"}${issue}`;
}

function stageLabel(row: ManualReviewRow): string {
  const raw = row.display_state_label || row.display_state || row.state || "";
  return raw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function reasonText(row: ManualReviewRow): string {
  return row.review_reason || row.reason || row.why_not_grabbed || row.activity_summary || "";
}

// Human copy + tone for the raw review-reason strings ("weak_filename_
// unit_evidence" read as leaked internals in the list). Known reasons get
// deliberate phrasing; the fallback title-cases whatever arrives, since
// review_reason is not a closed enum.
const REASON_META: Record<string, { label: string; tone: string }> = {
  weak_filename_unit_evidence: { label: "Weak filename evidence", tone: "warn" },
  ambiguous_results: { label: "Two possible matches", tone: "warn" },
  pack_requires_review: { label: "Pack needs review", tone: "warn" },
  wrong_unit_type: { label: "Wrong unit type", tone: "warn" },
  conflicting_manga_identity: { label: "Conflicting manga identity", tone: "warn" },
  filename_confidence_too_low: { label: "Filename confidence too low", tone: "warn" },
  destination_conflict: { label: "Destination conflict", tone: "bad" },
  policy_block: { label: "Blocked by policy", tone: "bad" },
  language_blocked: { label: "Blocked by language rule", tone: "bad" },
  staged_file_low_confidence: { label: "Staged file mismatch", tone: "warn" },
};

function reasonBadge(row: ManualReviewRow): { label: string; tone: string } | null {
  const raw = String(row.review_reason || row.reason || "").trim().toLowerCase().replace(/-/g, "_");
  if (!raw) return null;
  const known = REASON_META[raw];
  if (known) return known;
  const isBad = /fail|error|blocked/.test(raw);
  return { label: raw.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()), tone: isBad ? "bad" : "warn" };
}

// Server-side sort (state_view treats manual_review as an operational query
// with sort_key/sort_direction; same keys the vanilla Sort menu uses).
const SORT_OPTIONS = [
  { value: "default", label: "Default order" },
  { value: "updated:asc", label: "Oldest first" },
  { value: "updated:desc", label: "Newest first" },
  { value: "series:asc", label: "Series A-Z" },
] as const;

function buildEndpoint(offset: number, manualReviewFilter: string, sort: string): string {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    summary: "compact",
    // "table" matches inkdropSectionEndpoint()'s own default row shape for
    // this view -- not "thin" (Queue/Wanted's shape) and not the richer
    // untruncated row. Same fields the vanilla shell's own first paint (and
    // the Review Decision modal it opens from that same row object) already
    // work with today.
    rows: "table",
    offset: String(offset),
    manual_review_filter: manualReviewFilter || "actionable",
  });
  if (sort && sort !== "default") {
    const [key, dir] = sort.split(":");
    params.set("sort", key);
    params.set("direction", dir || "asc");
  }
  return `/api/inkdrop-state/manual_review?${params.toString()}`;
}

export function ManualReview({ payload }: { payload: ManualReviewViewPayload }) {
  const [rows, setRows] = useState<ManualReviewRow[]>(payload.rows || []);
  const [offset, setOffset] = useState(payload.offset || 0);
  const [totalCount, setTotalCount] = useState(payload.total_count || 0);
  const [hasMore, setHasMore] = useState(Boolean(payload.has_more));
  const [manualReviewFilter, setManualReviewFilter] = useState(payload.manual_review_filter || "actionable");
  const [sort, setSort] = useState<string>("default");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkIgnoring, setBulkIgnoring] = useState(false);

  // A fresh `payload` reference arrives whenever the surrounding shell
  // re-fetched this section on our behalf -- filter change, section
  // re-entry, or (importantly for this view) the vanilla Review Decision
  // modal's own reviewAction() calling loadInkdropSection("manual_review",
  // ...) after an Approve/Ignore/Reject. Same reasoning as Wanted/Blocklist.
  useEffect(() => {
    setRows(payload.rows || []);
    setOffset(payload.offset || 0);
    setTotalCount(payload.total_count || 0);
    setHasMore(Boolean(payload.has_more));
    setManualReviewFilter(payload.manual_review_filter || "actionable");
    setError(null);
    // A fresh payload means the previously selected rows may no longer be on
    // this page (or may have just been ignored/approved) -- stale ids left
    // checked would silently no-op on the next bulk action.
    setSelectedIds(new Set());
  }, [payload]);

  async function loadPage(nextOffset: number, nextSort?: string) {
    setLoading(true);
    setError(null);
    try {
      const data = await request<{ ok: boolean; view: ManualReviewViewPayload }>(
        buildEndpoint(nextOffset, manualReviewFilter, nextSort ?? sort),
      );
      const view = data.view;
      setRows(view.rows || []);
      setOffset(view.offset ?? nextOffset);
      setTotalCount(view.total_count || 0);
      setHasMore(Boolean(view.has_more));
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not load Manual Review page.");
    } finally {
      setLoading(false);
    }
  }

  function openDecision(row: ManualReviewRow) {
    setPendingId(row.id);
    try {
      window.InkDropManualReview?.openDecisionModal(row);
    } finally {
      setPendingId(null);
    }
  }

  const visibleRows = rows.filter(isHumanDecision);
  const selectableRows = visibleRows.filter((row) => Boolean(row.review_id));
  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + rows.length;
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

  async function runBulkIgnore() {
    const items = rows.filter((row) => selectedIds.has(row.id));
    if (!items.length || !window.InkDropManualReview?.bulkIgnore) return;
    setBulkIgnoring(true);
    try {
      // The confirm dialog, the sequential /api/manual-review/ignore calls,
      // the section refresh, and the result toast all already live in the
      // vanilla shell's runBulkManualReviewIgnore() (also used before this
      // table existed) -- reused as-is via the bridge rather than
      // reimplemented, same reasoning as openDecisionModal above.
      await window.InkDropManualReview.bulkIgnore(items);
    } finally {
      setBulkIgnoring(false);
    }
  }

  return (
    <div className="inkdrop-react-manual-review">
      {error && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error}
        </div>
      )}
      <div className="arr-table-controlbar arr-table-controlbar-manual_review">
        <div className="arr-table-controlbar-left">
          <button
            type="button"
            disabled={selectedCount < 1 || bulkIgnoring}
            onClick={() => void runBulkIgnore()}
            title={selectedCount < 1 ? "Select one or more visible rows first." : "Hide selected wanted items from Manual Review after confirmation. Files are not deleted."}
          >
            Ignore Selected
          </button>
          <span className="arr-table-selection-count">{selectedCount} selected</span>
        </div>
        <div className="arr-table-controlbar-right">
          <label className="mr-sort-select">
            Sort
            <select
              value={sort}
              onChange={(event) => {
                const next = event.target.value;
                setSort(next);
                void loadPage(0, next);
              }}
            >
              {SORT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      <table className="arr-table mr-table">
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
            <th>Stage</th>
            <th>Reason</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row) => {
            const canAct = Boolean(row.review_id);
            return (
              <tr key={row.id} className="mr-row">
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
                <td data-label="Stage">{stageLabel(row)}</td>
                <td data-label="Reason">
                  {(() => {
                    const badge = reasonBadge(row);
                    const detail = reasonText(row);
                    return (
                      <div className="mr-reason-cell">
                        {badge && <span className={`core-state mr-reason-badge ${badge.tone}`}>{badge.label}</span>}
                        {detail && badge?.label.toLowerCase() !== detail.toLowerCase() && (
                          <span className="mini">{detail}</span>
                        )}
                      </div>
                    );
                  })()}
                </td>
                <td data-label="Actions">
                  {canAct && (
                    <button
                      type="button"
                      disabled={pendingId === row.id}
                      onClick={() => openDecision(row)}
                      title="Open the decision panel with evidence, consequences, and safe choices."
                    >
                      Review Decision
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
          {visibleRows.length === 0 && !loading && (
            <tr>
              <td colSpan={5}>Nothing needs a decision in Manual Review right now.</td>
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
