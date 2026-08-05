import { useEffect, useState } from "react";
import { request, InkDropApiError } from "../api";
import type { ManualReviewRow, ManualReviewViewPayload } from "./manualReviewTypes";

const PAGE_SIZE = 80;

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

function buildEndpoint(offset: number, manualReviewFilter: string): string {
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
  return `/api/inkdrop-state/manual_review?${params.toString()}`;
}

export function ManualReview({ payload }: { payload: ManualReviewViewPayload }) {
  const [rows, setRows] = useState<ManualReviewRow[]>(payload.rows || []);
  const [offset, setOffset] = useState(payload.offset || 0);
  const [totalCount, setTotalCount] = useState(payload.total_count || 0);
  const [hasMore, setHasMore] = useState(Boolean(payload.has_more));
  const [manualReviewFilter, setManualReviewFilter] = useState(payload.manual_review_filter || "actionable");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);

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
  }, [payload]);

  async function loadPage(nextOffset: number) {
    setLoading(true);
    setError(null);
    try {
      const data = await request<{ ok: boolean; view: ManualReviewViewPayload }>(
        buildEndpoint(nextOffset, manualReviewFilter),
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
  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + rows.length;

  return (
    <div className="inkdrop-react-manual-review">
      {error && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error}
        </div>
      )}
      <table className="arr-table">
        <thead>
          <tr>
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
              <tr key={row.id}>
                <td data-label="Series / Issue">{rowTitle(row)}</td>
                <td data-label="Stage">{stageLabel(row)}</td>
                <td data-label="Reason">{reasonText(row)}</td>
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
              <td colSpan={4}>Nothing needs a decision in Manual Review right now.</td>
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
