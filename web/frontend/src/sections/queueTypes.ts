// Matches state_view()'s envelope for the "queue" view and
// compact_queue_table_view_row's field set (QUEUE_TABLE_ROW_KEYS in
// inkdrop_state.py, the "rows=thin" shape the shell already requests for
// this view's first paint). Every field is optional --
// compact_state_view_rows() omits any key whose value is falsy/empty rather
// than sending null.
// normalize_transfer_status()'s stable telemetry contract
// (core/inkdrop_transfer.py) as exposed on thin queue rows. progress_kind
// "determinate" is the ONLY licence to draw a bar -- everything else is a
// stage with no meaningful percentage.
export type QueueTransfer = {
  client?: string;
  transfer_state?: string;
  progress_kind?: "determinate" | "indeterminate";
  percent_complete?: number | null;
  bytes_completed?: number | null;
  bytes_total?: number | null;
  download_rate_bytes_per_second?: number | null;
  eta_seconds?: number | null;
  elapsed_seconds?: number | null;
  stalled?: boolean;
  client_error?: string | null;
  import_stage?: string | null;
  next_step?: string;
  rate?: number | null;
  eta?: number | null;
  elapsed?: number | null;
};

export type QueueRow = {
  id: string;
  revision?: number;
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
  media_type?: string;
  image?: string;
  transfer?: QueueTransfer;
};

// The focused full-row shape (queue_row_from_record without table
// compaction): only the pieces the Details panel reads are typed; every
// field is optional because each is genuinely nullable in the store.
export type QueueRowDetail = QueueRow & {
  query?: string;
  last_event?: string;
  attempt_count?: number;
  created_at?: number;
  updated_at?: number;
  download_task?: {
    title?: string;
    local_path?: string;
    save_path?: string;
    size_bytes?: number;
    download_client?: string;
    external_id?: string;
    provider?: string;
    peer?: string;
    failure_reason?: string;
    started_at?: number;
    completed_at?: number;
  };
  last_attempt?: { title?: string; provider?: string; failure_reason?: string };
  latest_import?: { dest_path?: string };
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
