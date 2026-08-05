// Matches state_view()'s envelope (inkdrop_state.py) and
// compact_source_memory_view_row's field set (SOURCE_MEMORY_COMPACT_ROW_KEYS).
// Every field is optional because compact_state_view_rows() drops any key
// whose value is falsy/empty rather than sending null -- a row with no
// recorded failures simply omits "failure_count".
export type BlocklistRow = {
  id: string;
  title?: string;
  series?: string;
  source_label?: string;
  reason?: string;
  reason_label?: string;
  failure_count?: number;
  last_seen_at_iso?: string;
  activity_summary?: string;
  linked_entities?: {
    display_title?: string;
    series?: string;
  };
};

export type StateViewFilter = {
  value: string;
  label: string;
  count: number;
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
};

export type AllowCandidateResult = {
  ok: boolean;
  result?: {
    ok?: boolean;
    retry?: { skipped?: boolean; [key: string]: unknown };
    [key: string]: unknown;
  };
};
