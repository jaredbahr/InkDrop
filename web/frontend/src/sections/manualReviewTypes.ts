// Matches state_view()'s envelope for the "manual_review" view with
// rows="table" (MANUAL_REVIEW_COMPACT_ROW_KEYS | QUEUE_COMPACT_ROW_KEYS in
// inkdrop_state.py) -- the same row/param shape inkdropSectionEndpoint()
// already requests for this view's default (non-focused) first paint, so
// this island receives exactly the row shape production already uses today,
// nothing richer and nothing thinner.
export type ManualReviewRow = {
  id: string;
  review_id?: string;
  revision?: number;
  series?: string;
  issue_number?: string;
  state?: string;
  status?: string;
  display_state?: string;
  display_state_label?: string;
  next_action?: string;
  why_not_grabbed?: string;
  activity_summary?: string;
  current_source?: string;
  source?: string;
  // Present only on review_exceptions-origin rows (queue_rows()-origin rows
  // never set it) -- the one reliable signal that `id` is a review_exceptions
  // id, not a queue_items id. See queueIdFor() in ManualReview.tsx: RECOVERY-
  // P1-02 was exactly this -- an action button assuming `id` was always a
  // queue id and silently 404ing for rows sourced from review_exceptions.
  origin?: string;
  reason?: string;
  review_reason?: string;
  // Supplied by core/inkdrop_review_reasons.py so every Manual Review
  // surface renders one vocabulary instead of deriving its own.
  reason_label?: string;
  reason_tone?: string;
  reason_detail?: string;
  reason_label_source?: string;
  // Built by decision_evidence() in core/inkdrop_import_evidence.py and
  // now carried through the compact/table row shape. `incomplete` is
  // load-bearing: it means InkDrop could not say what was expected, which must
  // read differently from "nothing was expected".
  decision_evidence?: {
    gate?: string;
    expected?: { series?: string; unit?: string };
    found?: { path?: string; file_name?: string; unit?: string };
    disagreement?: string;
    incomplete?: boolean;
  };
  state_label?: string;
  source_label?: string;
  manual_source_stage?: string;
  manual_review_actionable?: boolean;
  manual_review_parked?: boolean;
  linked_entities?: {
    queue_id?: string;
    wanted_id?: string;
    series_id?: string;
    issue_id?: string;
    review_id?: string;
  };
};

export type StateViewFilter = {
  value: string;
  label: string;
  count: number;
};

export type ManualReviewViewPayload = {
  ok: boolean;
  view: "manual_review";
  rows: ManualReviewRow[];
  count: number;
  loaded_count: number;
  total_count: number;
  has_more: boolean;
  limit: number;
  offset: number;
  manual_review_filter: string;
  filters: StateViewFilter[];
};

// window.InkDropManualReview is a thin bridge into the vanilla shell's
// existing "Review Decision" modal (inkdrop_web.py's
// openManualReviewDecisionModal) -- that modal's Approve/Ignore/Alias/pack
// logic stays exactly where it is today, since it's deeply closure-coupled
// to the vanilla shell (openInkdropConfirmModal, reviewAction, etc.) and a
// full port is a separate, later decision. This bridge is deliberately not
// typed any richer than "open the thing with this row."
declare global {
  interface Window {
    InkDropManualReview?: {
      openDecisionModal: (row: ManualReviewRow) => void;
      // Same reasoning as openDecisionModal: the confirm dialog and the
      // sequential ignore calls stay in the vanilla shell's
      // runBulkManualReviewIgnore(), reused via bridge rather than ported.
      bulkIgnore: (rows: ManualReviewRow[]) => Promise<void>;
    };
  }
}
