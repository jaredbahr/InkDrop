// Matches state_view()'s envelope (inkdrop_state.py) and
// compact_source_memory_view_row's field set (SOURCE_MEMORY_COMPACT_ROW_KEYS).
// Every field is optional because compact_state_view_rows() drops any key
// whose value is falsy/empty rather than sending null -- a row with no
// recorded failures simply omits "failure_count".
export type BlocklistRow = {
  id: string;
  revision?: number;
  title?: string;
  series?: string;
  source?: string;
  provider?: string;
  source_label?: string;
  source_path?: string;
  normalized_title?: string;
  reason?: string;
  reason_label?: string;
  failure_count?: number;
  first_seen_at?: number;
  last_seen_at?: number;
  last_seen_at_iso?: string;
  activity_at?: number;
  detail?: string;
  activity_summary?: string;
  source_attempt_filter?: string;
  linked_entities?: {
    display_title?: string;
    series?: string;
    series_id?: string;
    issue_number?: string;
    issue_title?: string;
    image?: string;
  };
};

export type StateViewFilter = {
  value: string;
  label: string;
  count: number;
};

export type BlocklistSummary = {
  bad_source_candidates?: number;
  bad_source_candidates_recent?: number;
  bad_source_candidates_by_reason?: Record<string, number>;
  bad_source_candidates_by_source?: Record<string, number>;
  bad_source_candidates_by_provider?: Record<string, number>;
};

export type BlocklistViewPayload = {
  ok: boolean;
  view: "source_memory";
  rows: BlocklistRow[];
  count: number;
  loaded_count: number;
  total_count: number;
  has_more: boolean;
  limit: number;
  offset: number;
  source_filter: string;
  filters: StateViewFilter[];
  summary?: BlocklistSummary;
};

export type AllowCandidateResult = {
  ok: boolean;
  result?: {
    ok?: boolean;
    retry?: { skipped?: boolean; [key: string]: unknown };
    [key: string]: unknown;
  };
};

export type BlocklistReasonMeta = {
  label: string;
  tone: string;
  detail: string;
};

declare global {
  interface Window {
    InkDropSourceMemory?: {
      openDetailPanel: (row: BlocklistRow) => void;
      closeDetailPanel: () => void;
      reasonMeta: (reason?: string, reasonLabel?: string) => BlocklistReasonMeta;
      mutate: (row: BlocklistRow, decision: string, options?: Record<string, unknown>) => Promise<boolean>;
      coverProxyUrl?: (value?: string) => string;
    };
  }
}
