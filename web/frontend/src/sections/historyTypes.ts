// Matches history_state_view()'s envelope and compact_history_view_row's
// field set (HISTORY_COMPACT_ROW_KEYS in inkdrop_state.py, the "rows=thin"
// shape the shell already requests for this view's first paint). Every
// field is optional -- compact_state_view_rows() omits any key whose value
// is falsy/empty rather than sending null.
export type HistoryRow = {
  id: string;
  series?: string;
  issue_number?: string;
  event_type?: string;
  history_kind?: string;
  status?: string;
  outcome?: string;
  next_action?: string;
  activity_summary?: string;
  message?: string;
  display_source?: string;
  provider_id?: string;
  provider_key?: string;
};

export type StateViewFilter = {
  value: string;
  label: string;
  count: number;
};

export type HistoryViewPayload = {
  ok: boolean;
  view: "history";
  rows: HistoryRow[];
  count: number;
  loaded_count: number;
  total_count: number;
  has_more: boolean;
  limit: number;
  offset: number;
  history_filter: string;
  history_search?: string | null;
  filters: StateViewFilter[];
};
