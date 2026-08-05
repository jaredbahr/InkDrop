// Matches state_view()'s envelope for the "manual_review" view with
// rows="table" (MANUAL_REVIEW_COMPACT_ROW_KEYS | QUEUE_COMPACT_ROW_KEYS in
// inkdrop_state.py) -- the same row/param shape inkdropSectionEndpoint()
// already requests for this view's default (non-focused) first paint, so
// this island receives exactly the row shape production already uses today,
// nothing richer and nothing thinner.
export type ManualReviewRow = {
  id: string;
  review_id?: string;
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
  reason?: string;
  review_reason?: string;
  manual_source_stage?: string;
  manual_review_actionable?: boolean;
  manual_review_parked?: boolean;
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
    };
  }
}
