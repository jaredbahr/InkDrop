// Row/payload shapes for the Series section island.
//
// These mirror the keys the shell already puts on the series view payload
// (core/inkdrop_state.py's series view projection plus SERIES_COUNT_FIELDS);
// everything is optional because the shell serves this view in both a "thin"
// first-paint shape and a fuller one, and a missing counter must read as zero
// rather than as a crash.

export interface SeriesRow {
  id?: string;
  title?: string;
  sort_title?: string;
  media_type?: string;
  year?: number | string | null;
  publisher?: string | null;
  metadata_provider?: string | null;
  source?: string | null;
  library_path?: string | null;
  monitored?: boolean;
  monitor_new?: boolean;
  auto_grab?: boolean;
  updated_at?: number | string | null;
  image?: string | null;

  // Counters the status pill and the ribbon-free card body read.
  wanted_count?: number;
  active_queue_count?: number;
  provider_wait_count?: number;
  needs_you_count?: number;
  downloading_count?: number;
  importing_count?: number;
  missing_issue_count?: number;
  covered_issue_count?: number;
  verified_issue_count?: number;
  reader_visibility_gap_count?: number;
  reader_visibility_lag_count?: number;
  reader_visibility_blocked_count?: number;
}

// The shell owns the toolbar (View / Sort / Filter) and the search box, and
// keeps their state in module-level variables it persists to localStorage. It
// passes the current values down on every re-render so the island can present
// the same ordering the toolbar claims, without owning that state itself.
export interface SeriesUiState {
  view_mode?: string;
  sort_key?: string;
  sort_dir?: string;
  filter?: string;
  search_query?: string;
  alphabet_rail?: boolean;
}

export interface SeriesViewPayload {
  rows?: SeriesRow[];
  total_count?: number;
  offset?: number;
  has_more?: boolean;
  series_ui?: SeriesUiState;
}

export type StatusTone = "good" | "warn" | "bad" | "";

export interface SeriesStatus {
  label: string;
  tone: StatusTone;
  detail: string;
}

const n = (value: unknown): number => Number(value || 0);

// One status per card, replacing the three the vanilla card rendered (corner
// ribbon + tinted bar + disclosure word). Same precedence the vanilla
// seriesOperatorNextStep() used, with the vocabulary aligned to the already
// migrated sections: "Missing" is what Wanted calls "Wanted", and the reader
// state is title-cased like every other label here.
export function seriesStatus(row: SeriesRow): SeriesStatus {
  const issues = (count: number) => `${count} issue${count === 1 ? "" : "s"}`;
  if (n(row.needs_you_count)) {
    return { label: "Needs Review", tone: "bad", detail: `${issues(n(row.needs_you_count))} waiting on a decision.` };
  }
  if (n(row.active_queue_count)) {
    return { label: "Queued", tone: "warn", detail: `${issues(n(row.active_queue_count))} moving through the queue.` };
  }
  if (n(row.provider_wait_count)) {
    return { label: "Waiting", tone: "warn", detail: `${issues(n(row.provider_wait_count))} waiting on a source.` };
  }
  if (n(row.wanted_count)) {
    return { label: "Wanted", tone: "warn", detail: `${issues(n(row.wanted_count))} still missing.` };
  }
  const readerLag = n(row.reader_visibility_gap_count) || n(row.reader_visibility_lag_count);
  if (readerLag) {
    return {
      label: "Reader Pending",
      tone: n(row.reader_visibility_blocked_count) ? "bad" : "warn",
      detail: `${issues(readerLag)} imported but not visible in your reader yet.`,
    };
  }
  if (row.monitored === false) {
    return { label: "Paused", tone: "", detail: "Not being monitored." };
  }
  if (row.monitored || row.auto_grab) {
    return { label: "Monitored", tone: "good", detail: "Monitored, nothing outstanding." };
  }
  return { label: "Series", tone: "", detail: "" };
}

// "MangaDex" is a metadata provider that leaks into the publisher column on
// ingest -- 28 of 378 series on the live library carry it. It is not a brand,
// so it counts as no publisher. A further 79 rows have the column empty
// outright (78 of them comics, not manga). Both cases render no ribbon at all
// rather than an empty or wrong band.
const NON_PUBLISHERS = new Set(["mangadex", "unknown", "n/a", "none"]);

export function seriesRibbonPublisher(row: SeriesRow): string {
  const raw = String(row.publisher || "").trim();
  if (!raw) return "";
  if (NON_PUBLISHERS.has(raw.toLowerCase())) return "";
  return raw;
}

// Real publisher values run to 27 characters ("Space Between Entertainment"),
// against a ribbon sized for "IMAGE". Truncate for the band and keep the full
// value in the title attribute.
export const RIBBON_MAX = 14;

export function ribbonText(publisher: string): string {
  if (publisher.length <= RIBBON_MAX) return publisher;
  return `${publisher.slice(0, RIBBON_MAX - 1).trimEnd()}…`;
}

// Same publisher rule as the ribbon: if "MangaDex" is not a brand worth a
// ribbon, it is not a brand worth the meta line either. Otherwise a card whose
// ribbon was deliberately suppressed still prints the provider name underneath.
export function seriesMetaLine(row: SeriesRow): string {
  return [seriesRibbonPublisher(row), row.year ? String(row.year) : ""]
    .filter(Boolean)
    .join(" · ");
}
