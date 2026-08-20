// Matches reliability_state_view()'s envelope and reliability_view_rows()'s
// item shape (core/inkdrop_state.py). Every wanted/in-progress item's real
// pipeline stage plus which of the twelve reliability buckets it falls into.
//
// The bucket keys here are the machine vocabulary agreed across the acquisition
// and display sessions and are frozen -- the labels are display-only and live
// on the server so the page, the API and any saved link agree on wording.
//
// (Fifteen as of candidate_unconfirmed's addition -- see the comment on that
// key below; this count has drifted from its original "twelve" more than
// once and is not itself load-bearing anywhere, just a landmark for readers.)

export const RELIABILITY_STAGE_ORDER = [
  "searched",
  "found",
  "grabbed",
  "downloading",
  "importing",
  "verified",
] as const;

export type ReliabilityStageKey = (typeof RELIABILITY_STAGE_ORDER)[number];

export type ReliabilityBucketKey =
  | "manual_review_needed"
  | "stuck_import"
  | "known_bad_blocked"
  // queue_state claims searching/downloading/importing/source_wait but no
  // live claim, unexpired lease, or fresh task evidence backs it up --
  // demoted here from actively_processing by
  // demote_unconfirmed_active_rows() (core/inkdrop_state.py). Prompt121:
  // 39-40 wanted|in_progress rows carried "Working right now" with zero
  // claims and zero mapped owners, live 2026-08-18.
  | "active_unconfirmed"
  | "budget_starved"
  | "candidate_awaiting_pick"
  // candidate_awaiting_pick demoted here when the row's own triggering
  // evidence is a phrase, a count, or an older attempt with no retained
  // candidate identity/hash -- see current_pickable_candidate_reference()
  // (core/inkdrop_state.py). Prompt122: 228/282 carried the old bucket on
  // prose alone, live 2026-08-18.
  | "candidate_unconfirmed"
  | "candidates_rejected"
  | "search_never_completed"
  | "searched_nothing_found"
  | "not_searched_yet"
  | "actively_processing"
  | "superseded_duplicate"
  | "other"
  | "not_pursued";

export type ReliabilityBandKey = "needs_attention" | "waiting" | "healthy" | "paused";

export type ReliabilityItem = {
  wanted_id: string;
  series_id?: string;
  issue_id?: string;
  wanted_status?: string;
  wanted_created_at?: number;
  wanted_updated_at?: number;
  // "Wanted, not pursued". Deliberately NOT a wanted_items.status value:
  // every status the acquisition gates exclude is also excluded from the
  // Wanted counts, and a backlog number you can improve by pressing a button
  // instead of by acquiring anything is not a number.
  wanted_pursuit_paused?: boolean;
  wanted_pursuit_paused_at?: number | null;
  wanted_pursuit_paused_reason?: string | null;
  // The hand-corrected query, when one is set. Stored on the wanted row
  // because queue_items.query is rewritten by the metadata upsert.
  operator_query?: string | null;
  series?: string;
  media_type?: string;
  issue_number?: string;
  issue_title?: string;
  queue_id?: string;
  queue_state?: string;
  current_source?: string;
  last_event?: string;
  queue_active?: boolean;
  // There is deliberately no queue_updated_at here. The retry machinery
  // rewrites queue_items.updated_at every pass, so it reports a row stuck for
  // 47 days as "updated just now"; the server stopped sending it and nothing
  // on this page may print it as an event time. Use last_provider_attempt_at
  // (when a real provider last searched) or first_transfer_completed_at (when
  // the download actually finished) instead -- both are written once.
  last_provider_attempt_at?: number | null;
  first_transfer_completed_at?: number | null;
  // Raw source_attempts row count. Do NOT render this as "attempts": that
  // table is a lifecycle ledger and the number runs 5.3x high at the median
  // and 330x at the tail. Kept for an explicitly-labelled diagnostic only.
  attempt_count?: number;
  ledger_row_count?: number;
  // The de-noised count -- the one an operator should read and sort on.
  // Null until the backend fills it for the rendered page.
  real_attempt_count?: number | null;
  has_provider_attempt?: boolean;
  has_rejected_attempt?: boolean;
  // Ownership proof behind active_unconfirmed vs actively_processing --
  // see demote_unconfirmed_active_rows() (core/inkdrop_state.py).
  has_live_claim?: boolean;
  has_fresh_handoff_task?: boolean;
  last_attempt_id?: string;
  last_attempt_status?: string;
  last_attempt_failure_reason?: string;
  last_attempt_lifecycle_phase?: string;
  last_attempt_display_phase?: string;
  last_attempt_source?: string;
  last_attempt_completed_at?: number;
  last_attempt_started_at?: number;
  // Present only when the last attempt actually reached a release. "Block this
  // release" gates on these, not on last_attempt_id -- a budget skip has an
  // attempt row but no release to block.
  last_attempt_candidate_identity?: string | null;
  last_attempt_download_url_hash?: string | null;
  stage: ReliabilityStageKey;
  stage_label: string;
  bucket: ReliabilityBucketKey;
  bucket_label: string;
  bucket_band?: ReliabilityBandKey;
  // "current" when the bucket came from the row's live state, "history" when
  // it came from an older provider attempt because the newest activity row
  // carried no result of its own.
  bucket_evidence_scope?: "current" | "history";
  bucket_evidence_at?: number | null;
  reason: string;
};

export type ReliabilityBucketRollup = {
  key: ReliabilityBucketKey;
  label: string;
  count: number;
  band: ReliabilityBandKey;
  explainer: string;
};

export type ReliabilityBandRollup = {
  key: ReliabilityBandKey;
  label: string;
  count: number;
};

export type ReliabilitySignal = {
  key: string;
  label: string;
  value: number | string;
  detail: string;
  tone: "neutral" | "warn";
};

export type ReliabilitySummary = {
  ok: boolean;
  db_path?: string;
  generated_at?: number;
  total: number;
  buckets: ReliabilityBucketRollup[];
  bands?: ReliabilityBandRollup[];
  by_bucket: Record<string, number>;
};

// GET /api/inkdrop-state/reliability/signals -- separate from the list payload
// because it needs a real provider timestamp for every row in the backlog,
// which measured ~1.2s no matter how it was queried.
export type ReliabilitySignalsPayload = {
  ok: boolean;
  reason?: string;
  generated_at?: number;
  total?: number;
  signals?: ReliabilitySignal[];
};

export type ReliabilityStageMeta = {
  key: ReliabilityStageKey;
  label: string;
};

export type ReliabilityViewPayload = {
  ok: boolean;
  view: "reliability";
  summary?: ReliabilitySummary;
  rows: ReliabilityItem[];
  count: number;
  loaded_count: number;
  total_count: number;
  has_more: boolean;
  limit: number;
  offset: number;
  bucket_filter?: string;
  bucket_explainer?: string;
  stages: ReliabilityStageMeta[];
};

// GET /api/inkdrop-state/reliability/item?wanted_id=... -- loaded on expand,
// never inline in the list (the whole-table version of these joins is 3.2s).
export type ReliabilityEvidenceAttempt = {
  id: string;
  source?: string;
  provider?: string;
  status?: string;
  title?: string;
  at?: number | null;
  at_label?: string;
  outcome?: string;
  outcome_key?: string;
  failure_reason?: string;
  failure_sentence?: string;
  query?: string | null;
  candidate_count?: number | null;
  safe_candidate_count?: number | null;
  rejected_candidate_count?: number | null;
  candidate_identity?: string | null;
  download_url_hash?: string | null;
};

export type ReliabilityRejectedRelease = {
  id: string;
  title?: string;
  source?: string;
  provider?: string;
  reason?: string;
  reason_sentence?: string;
  failure_count?: number;
  first_seen_at?: number | null;
  last_seen_at?: number | null;
  last_seen_label?: string;
};

export type ReliabilityEvidence = {
  ok: boolean;
  reason?: string;
  wanted_id?: string;
  series?: string;
  issue_number?: string;
  queue_id?: string;
  queue_state?: string;
  // What InkDrop will actually send next, resolved by the same precedence
  // the autopilot uses -- not rebuilt from series and issue.
  search_query?: string | null;
  search_query_source?: "operator" | "generated";
  generated_search_query?: string | null;
  operator_search_query?: string | null;
  pursuit_paused?: boolean;
  pursuit_paused_at?: number | null;
  pursuit_paused_reason?: string | null;
  wanted_reason?: string;
  wanted_created_at?: number;
  retry_after?: number | null;
  retry_after_iso?: string | null;
  attempts?: ReliabilityEvidenceAttempt[];
  attempts_truncated?: boolean;
  attempt_limit?: number;
  rejected_releases?: ReliabilityRejectedRelease[];
  generated_at?: number;
};
