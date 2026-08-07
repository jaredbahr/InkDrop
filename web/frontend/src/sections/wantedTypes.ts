// Matches state_view()'s envelope for the "wanted" view and
// compact_wanted_table_view_row's field set (WANTED_TABLE_ROW_KEYS /
// QUEUE_TABLE_ROW_KEYS in inkdrop_state.py, the "rows=thin" shape the shell
// already requests for this view's first paint). Every field is optional --
// compact_state_view_rows() omits any key whose value is falsy/empty rather
// than sending null.
export type WantedRow = {
  id: string;
  revision?: number;
  series?: string;
  issue_number?: string;
  status?: string;
  display_state?: string;
  display_state_label?: string;
  queue_state?: string;
  next_action?: string;
  why_not_grabbed?: string;
  wait_reason_label?: string;
  activity_summary?: string;
  next_concrete_source_label?: string;
  next_source_label?: string;
  display_source?: string;
  current_source?: string;
  source_key?: string;
};

export type StateViewFilter = {
  value: string;
  label: string;
  count: number;
};

export type WantedViewPayload = {
  ok: boolean;
  view: "wanted";
  rows: WantedRow[];
  count: number;
  loaded_count: number;
  total_count: number;
  has_more: boolean;
  limit: number;
  offset: number;
  wanted_filter: string;
  filters: StateViewFilter[];
};

export type WantedRunResult = {
  ok: boolean;
  result?: {
    series?: string;
    dbQueue?: { queued?: number; wanted_id?: string; series_id?: string; issue_id?: string; queue_id?: string };
    queue?: { created?: number; updated?: number; resurrected?: number };
    [key: string]: unknown;
  };
};
