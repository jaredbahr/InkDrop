import { useEffect, useState } from "react";
import { request, InkDropApiError } from "../api";
import { useRowActions } from "../rowActions";
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
  // The server already decided eligibility (manual_review_canonical_snapshot
  // -> apply_manual_review_contract) before this row ever reached the
  // client, so an explicit true here is authoritative -- check it before
  // isAutomaticWork's text-sniffing regex, not after. A genuinely actionable
  // row (e.g. a stuck import review, state "importing") can still contain
  // words like "import"/"download"/"verify" in its status text, which used
  // to make isAutomaticWork misclassify it as background automatic work and
  // silently drop it here while total_count (server-side) still counted it
  // -- the pager/badge and the rendered rows disagreeing.
  if (row.manual_review_actionable === true) return true;
  if (isAutomaticWork(row)) return false;
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

// A machine identifier, e.g. `qbit_torrent_completed_outside_expected_save_path`
// or `weak_filename_unit_evidence`. Lowercase words joined by underscores and
// nothing else -- deliberately narrow, so a sentence that merely contains an
// underscored token ("it's stopped auto_retrying and needs a decision from
// you.") is still treated as prose and shown.
const REASON_CODE_RE = /^[a-z0-9]+(?:_[a-z0-9]+)+$/;

// The detail line under the badge. It must never print the gate's own key.
//
// Reported live: a row read "Torrent finished in the wrong folder" with
// `qbit_torrent_completed_outside_expected_save_path` printed underneath it --
// the same fact twice, once in machine form, because this fell through to
// `row.reason` (the gate's key) whenever nothing better was set and the badge
// above had just translated that very string.
//
// The guard is general rather than a lookup entry for that one code: any
// unmapped identifier is refused, so a reason value added later cannot leak by
// being absent from REASON_META. Returning empty is safe because the badge
// already carries the human label for exactly these values -- reasonBadge()
// reads the same two fields and title-cases whatever it does not know.
//
// Measured against the live library before shipping: 23 Manual Review rows, 21
// distinct reason values, of which 20 are full English sentences -- this field
// carries prose, not enum keys. Exactly ONE row yields an identifier here, and
// it has other prose available, so this loses information on zero rows today.
// It is a forward-looking guard, not a fix for something widespread.
function reasonText(row: ManualReviewRow): string {
  const candidates = [row.review_reason, row.reason, row.why_not_grabbed, row.activity_summary];
  for (const value of candidates) {
    const text = String(value || "").trim();
    if (!text) continue;
    if (REASON_CODE_RE.test(text.toLowerCase())) continue;
    return text;
  }
  return "";
}

// Distinguishes a stuck IMPORT (file downloaded, verification/import failed)
// from a stuck SEARCH -- Reopen Import only makes sense for the former.
function isImportStuck(row: ManualReviewRow): boolean {
  const state = String(row.state || "").toLowerCase();
  if (state === "importing" || state === "verified") return true;
  return /import/i.test(decisionText(row));
}

// Manual Review rows come from two different tables (manual_review_canonical_
// snapshot() in inkdrop_state.py): queue_rows()-origin rows, whose own `id`
// already IS the queue_items id, and review_exceptions-origin rows (any row
// with `origin` set), whose `id` is a review_exceptions id instead -- the
// linked queue item, if any, only appears at `linked_entities.queue_id`.
// Treating `id` as interchangeable for both was RECOVERY-P1-02 (fixed
// 2026-08-06): action buttons 404'd for every review_exceptions-only row.
// These new retry/block/reopen actions operate on queue_items, so they must
// resolve the real queue id per row, and stay hidden when none exists.
function queueIdFor(row: ManualReviewRow): string {
  if (row.origin) return row.linked_entities?.queue_id || "";
  return row.id || "";
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
  qbit_torrent_completed_outside_expected_save_path: { label: "Torrent finished in the wrong folder", tone: "bad" },
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
  const { pendingIds, doneIds, actionError, clearActionError, runRowAction } = useRowActions(() => loadPage(offset));

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
    clearActionError();
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

  // Manual Review rows are queue_items rows under a different lens for the
  // queue_rows()-origin half of this view (queueIdFor() resolves which id to
  // use for either half) -- these reuse the exact same Queue-page endpoints
  // rather than a Manual-Review-specific copy.
  function runRetry(row: ManualReviewRow) {
    const queueId = queueIdFor(row);
    void runRowAction(row.id, rowTitle(row), "Queued", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/queue/run", {
        method: "POST",
        body: { id: queueId, revision: row.revision },
      });
      if (!data.ok) throw new InkDropApiError("Could not retry this item.", { status: 200, code: "mr_retry_failed" });
    });
  }

  function runRetryNewSource(row: ManualReviewRow) {
    const queueId = queueIdFor(row);
    void runRowAction(row.id, rowTitle(row), "Queued", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/queue/retry-new-source", {
        method: "POST",
        body: { id: queueId, revision: row.revision },
      });
      if (!data.ok) {
        const reason = data.result?.reason;
        throw new InkDropApiError(
          reason === "no_alternate_source_available"
            ? "No alternate source is configured for this item -- every source already failed."
            : "Could not retry with another source.",
          { status: 200, code: "mr_retry_new_source_failed" },
        );
      }
    });
  }

  function runReopenImport(row: ManualReviewRow) {
    const queueId = queueIdFor(row);
    void runRowAction(row.id, rowTitle(row), "Reopened", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/queue/reopen-import", {
        method: "POST",
        body: { id: queueId, revision: row.revision },
      });
      if (!data.ok) throw new InkDropApiError("Could not reopen this import.", { status: 200, code: "mr_reopen_import_failed" });
    });
  }

  // No explicit source_attempt_id -- the backend blocks whichever candidate
  // most recently attempted against this item, which for a Manual Review row
  // is exactly the one that produced this row's reason.
  function blockCandidate(row: ManualReviewRow) {
    const queueId = queueIdFor(row);
    if (!window.confirm(`Permanently block this release for ${rowTitle(row)}? InkDrop will never offer it again for this issue.`)) return;
    void runRowAction(row.id, rowTitle(row), "Blocked", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/source-memory/block", {
        method: "POST",
        body: { id: queueId, revision: row.revision, retry: true },
      });
      if (!data.ok) {
        const reason = data.result?.reason;
        throw new InkDropApiError(
          reason === "no_candidate_to_block" ? "No recent candidate is on record for this row to block." : "Could not block this release.",
          { status: 200, code: "mr_block_candidate_failed" },
        );
      }
    });
  }

  const visibleRows = rows.filter(isHumanDecision);
  const selectableRows = visibleRows.filter((row) => Boolean(row.review_id));
  const pageStart = totalCount === 0 || visibleRows.length === 0 ? 0 : offset + 1;
  // rows.length, not visibleRows.length, would count rows this page fetched
  // but then hid (context-only rows in mixed buckets like source_hints) --
  // the pager text must describe what's actually on screen.
  const pageEnd = offset + visibleRows.length;
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
      {(error || actionError) && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error || actionError}
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
            const queueId = queueIdFor(row);
            const canRecover = Boolean(queueId);
            const canReopenImport = canRecover && isImportStuck(row);
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
                  <div className="mr-row-actions">
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
                    {canRecover && (
                      <button
                        type="button"
                        disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                        onClick={() => runRetry(row)}
                        title="Retry this item's search, same as the Queue page's Retry"
                      >
                        {pendingIds.has(row.id) ? "Working…" : doneIds.get(row.id) || "Retry"}
                      </button>
                    )}
                    {canRecover && (
                      <button
                        type="button"
                        disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                        onClick={() => runRetryNewSource(row)}
                        title="Retry, skipping whichever source(s) most recently failed for this item"
                      >
                        {pendingIds.has(row.id) ? "Working…" : doneIds.get(row.id) || "Try another source"}
                      </button>
                    )}
                    {canReopenImport && (
                      <button
                        type="button"
                        disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                        onClick={() => runReopenImport(row)}
                        title="Re-check this import; only changes anything if it's genuinely stuck"
                      >
                        {pendingIds.has(row.id) ? "Working…" : doneIds.get(row.id) || "Reopen import"}
                      </button>
                    )}
                    {canRecover && (
                      <button
                        type="button"
                        className="mr-row-block-btn"
                        disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                        onClick={() => blockCandidate(row)}
                        title="Permanently reject the release behind this review so InkDrop stops offering it"
                      >
                        {pendingIds.has(row.id) ? "Working…" : doneIds.get(row.id) || "Block release"}
                      </button>
                    )}
                  </div>
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
