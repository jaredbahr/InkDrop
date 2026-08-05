// Matches state_view()'s envelope for the "queue" view and
// compact_queue_table_view_row's field set (QUEUE_TABLE_ROW_KEYS in
// inkdrop_state.py, the "rows=thin" shape the shell already requests for
// this view's first paint). Every field is optional --
// compact_state_view_rows() omits any key whose value is falsy/empty rather
// than sending null.
export type QueueRow = {
  id: string;
  series?: string;
  issue_number?: string;
  state?: string;
  status?: string;
  display_state?: string;
  display_state_label?: string;
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

export type QueueViewPayload = {
  ok: boolean;
  view: "queue";
  rows: QueueRow[];
  count: number;
  loaded_count: number;
  total_count: number;
  has_more: boolean;
  limit: number;
  offset: number;
  queue_filter: string;
  filters: StateViewFilter[];
};

export type QueueRunResult = {
  ok: boolean;
  result?: {
    series?: string;
    issue?: string;
    action?: string;
    dbRetry?: { ok?: boolean; action?: string; reason?: string };
    [key: string]: unknown;
  };
};
