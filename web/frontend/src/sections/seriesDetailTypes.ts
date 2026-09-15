// Row/payload shapes and pure helpers for the Series-detail island, ported
// from core/inkdrop_web.py's renderInkdropSeriesDetailPage cluster
// (seriesDetailMetadata, seriesDetailIssue*, coreStateLabel/coreStateTone,
// etc). Covers the hero, overview facts, stat grid, reader-visibility note,
// and the issues list including its per-issue-row actions. The page-level
// toolbar/commandbar (Refresh, Search, Monitor/Auto Grab toggles, Move
// Library, Manga Unit override, Remove Series) stays vanilla -- those are
// full standalone mini-features (async preview fetches, modals, a
// destructive-action confirm/recover flow) with real, tested implementations
// that would only be duplicated -- and risk drifting -- by porting them to
// React just because their trigger button now lives in a React-owned row.
// See seriesDetailPayloadIsFocused's caller in inkdrop_web.py for the mount
// point this island was given.

export interface SeriesDetailRow {
  id?: string;
  title?: string;
  media_type?: string;
  year?: number | string | null;
  publisher?: string | null;
  metadata_provider?: string | null;
  metadata_id?: string | number | null;
  source?: string | null;
  library_path?: string | null;
  monitored?: boolean;
  monitor_new?: boolean;
  auto_grab?: boolean;
  image?: string | null;
  removed_by_user?: boolean;
  provenance_label?: string;
  ownership?: string;
  manga_unit_model_override?: boolean;

  description?: string;
  deck?: string;
  overview?: string;
  summary?: string;
  start_year?: number | string;
  startYear?: number | string;
  watch_year?: number | string;
  issue_count?: number;
  issueCount?: number;
  count_of_issues?: number;
  site_url?: string;
  siteUrl?: string;
  site_detail_url?: string;
  created_at?: number | string;
  metadata_date_added?: number | string;
  metadata_date_last_updated?: number | string;
  updated_at?: number | string;

  wanted_count?: number;
  active_queue_count?: number;
  provider_wait_count?: number;
  needs_you_count?: number;

  reader_visibility_lag_count?: number;
  reader_visibility_gap_count?: number;
  reader_visibility_required_count?: number;
  reader_visibility_pending_count?: number;
  reader_visibility_optional_count?: number;
  reader_visibility_timeout_count?: number;
  reader_visibility_blocked_count?: number;
  reader_visibility_provider_counts?: Record<string, number>;
  reader_visibility_latest_provider?: string;
  reader_visibility_note?: string;
  reader_visibility_next_action?: string;
}

export interface LinkedEntities {
  series_id?: string;
  edition_id?: string;
  issue_id?: string;
  unit_id?: string;
  wanted_id?: string;
  queue_id?: string;
  source_attempt_id?: string;
  download_task_id?: string;
  import_id?: string;
  review_id?: string;
}

export interface IssueRow {
  issue_number?: string | number;
  normalized_number?: string | number;
  issue_title?: string;
  release_date?: string;
  issue_metadata_provider?: string;
  metadata_provider?: string;
  issue_metadata_id?: string | number;
  display_state?: string;
  state?: string;
  queue_state?: string;
  queue_active?: boolean;
  queue_id?: string;
  wanted_status?: string;
  wanted_id?: string;
  monitored?: boolean;
  next_action?: string;
  activity_summary?: string;
  current_source?: string;
  completion_gate?: { state?: string; label?: string; complete?: boolean };
  last_attempt?: { status?: string; source?: string; provider?: string; title?: string };
  latest_import?: { id?: string; verified?: boolean; status?: string; dest_path?: string };
  title?: string;
  display_title?: string;
  name?: string;
  volume?: string | number;
  chapter?: string | number;
  number?: string | number;
  issue?: string | number;
  issue_label?: string | number;
  series?: string;
  series_title?: string;
  linked_entities?: LinkedEntities;
}

export interface SeriesDetailIssuesPayload {
  rows?: IssueRow[];
  loaded_count?: number;
  total_count?: number;
  count?: number;
}

export type StatusTone = "good" | "warn" | "bad" | "auto" | "";

// -- generic label/tone/number formatting, ported from inkdrop_web.py -------

export function compactNumber(value: unknown): string {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return "0";
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  return String(n);
}

export function compactStatusSentence(value: unknown, fallback = "", maxLength = 118): string {
  const text = String(value || "").trim();
  const base = text || fallback;
  if (!base) return "";
  if (base.length <= maxLength) return base;
  const firstSentence = base.split(/(?<=[.!?])\s+/)[0] || base;
  if (firstSentence.length <= maxLength) return firstSentence;
  return `${firstSentence.slice(0, Math.max(20, maxLength - 1)).trim()}...`;
}

const LIFECYCLE_ALIASES: Record<string, string> = {
  queued: "Waiting", waiting: "Waiting", provider_wait: "Waiting", source_wait: "Waiting",
  retry_scheduled: "Waiting", retry_backoff: "Waiting", retry_due: "Waiting", retry_later: "Waiting",
  no_candidate: "Waiting", searched_no_candidates: "Waiting", waiting_scan: "Waiting",
  library_scan_pending: "Waiting", waiting_for_library_scan: "Waiting", waiting_for_kavita_scan: "Waiting",
  verification_wait: "Waiting", source_waiting: "Waiting",
  searching: "Searching", source_coverage: "Searching", candidate_recovery: "Searching", recovering: "Searching",
  source_candidates: "Searching", source_unchecked: "Searching", failed_candidate: "Searching", failed_candidate_retry: "Searching",
  downloading: "Downloading", importing: "Importing", staged_or_importing: "Importing", ready_import: "Importing",
  verified: "Complete", complete: "Complete", completed: "Complete", copied_not_indexed: "Complete",
  folder_complete: "Complete", folder_verified: "Complete", library_visible: "Complete",
  failed: "Failed", error: "Failed",
  // Kept identical to the desktop table in core/inkdrop_web.py, which carries
  // the reasoning: a scan we stopped waiting for is not a finding about the
  // file, and the wording is the server's own name for the state.
  library_scan_timeout: "Library Scan Timeout", kavita_scan_timeout: "Library Scan Timeout",
  scan_timeout: "Library Scan Timeout", timeout: "Library Scan Timeout",
  blocked: "Blocked", policy_block: "Blocked", language_blocked: "Blocked",
  needs_you: "Needs you", needs_user: "Needs you", manual_exception: "Needs you", manual_review: "Needs you",
};

const OTHER_LABELS: Record<string, string> = {
  active: "Active", healthy: "Healthy", historical: "Historical", retired: "Historical",
  provider_limited: "Source Limited", retry_unscheduled: "Queued", reader_sync: "Reader Sync",
  source_ladder: "Source Ladder", source_hints: "Manual Source", source_checked: "Checked",
};

const ACRONYM_WORDS = new Set(["slskd", "sab", "sabnzbd", "nzb", "rss", "cbr", "cbz"]);

export function coreStateLabel(value: unknown): string {
  const text = String(value ?? "unknown").trim();
  const lower = text.toLowerCase().replace(/[-\s]+/g, "_");
  if (LIFECYCLE_ALIASES[lower]) return LIFECYCLE_ALIASES[lower];
  if (OTHER_LABELS[lower]) return OTHER_LABELS[lower];
  return text
    .replace(/[_-]+/g, " ")
    .split(" ")
    .map((word) => (ACRONYM_WORDS.has(word.toLowerCase()) ? word.toUpperCase() : word.charAt(0).toUpperCase() + word.slice(1)))
    .join(" ");
}

export function coreStateTone(value: unknown): StatusTone {
  const text = String(value || "").toLowerCase();
  if (["verified", "satisfied", "imported", "clear", "configured", "native", "healthy"].includes(text)) return "good";
  if (["failed", "blocked", "needs_you", "manual_exception"].includes(text)) return "bad";
  if (["searching", "downloading", "importing", "staged_or_importing", "verifying", "active", "in_progress", "source_ladder"].includes(text)) return "auto";
  if (
    [
      "queued", "wanted", "adapter", "tracking", "watch", "guarded", "provider_wait", "source_wait",
      "provider_limited", "retry_due", "retry_scheduled", "retry_backoff", "retry_later", "no_candidate",
      "searched_no_candidates", "waiting", "source_coverage", "failed_candidate", "failed_candidate_retry",
      "candidate_recovery", "recovering", "waiting_scan", "verification_wait",
    ].includes(text)
  ) {
    return "warn";
  }
  return "";
}

// This island used to carry its own twelve-key source-name table, one of three
// in the browser against the server's own. window.InkDropSourceLabel
// (web/static/js/inkdrop-api.js) applies the server's vocabulary, and this
// bundle runs inside the desktop shell, which loads that file first.
//
// Read off `window` at call time rather than captured at module load: the
// island is mounted by the shell after both scripts have run, but a captured
// reference would make the order a silent dependency instead of a checked one.
declare global {
  interface Window {
    InkDropSourceLabel?: (value: unknown) => string;
  }
}

export function sourceBucketLabel(value: unknown): string {
  const key = String(value || "").trim().toLowerCase();
  if (!key) return "";
  const shared = typeof window !== "undefined" ? window.InkDropSourceLabel : undefined;
  // coreStateLabel() is the fallback the table always had behind it. It names
  // a source no better than it ever did -- it is here so a bundle loaded
  // without the shell still renders a word rather than throwing.
  return shared ? shared(key) : coreStateLabel(key);
}

// -- overview / facts --------------------------------------------------------

export function seriesDetailDateLabel(value: unknown): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (/^(19|20)\d{2}$/.test(raw)) return raw;
  const numeric = Number(raw);
  let date: Date | null = null;
  if (Number.isFinite(numeric) && numeric > 100000000) date = new Date(numeric * 1000);
  else if (/^\d{4}-\d{2}-\d{2}/.test(raw)) date = new Date(raw);
  else if (/^\d{4}-\d{2}/.test(raw)) date = new Date(`${raw}-01`);
  if (date && !Number.isNaN(date.getTime())) {
    return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }
  return raw.replace("T", " ").replace(/Z$/, "");
}

// Strips markup out of a description that may contain simple HTML, then caps
// its length the same way compactPlainText did in the vanilla version.
export function seriesDetailText(value: unknown, maxLength = 900): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const scratch = document.createElement("div");
  scratch.innerHTML = raw;
  const text = (scratch.textContent || scratch.innerText || raw).replace(/\s+/g, " ").trim();
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(20, maxLength - 1)).trim()}...`;
}

export interface SeriesDetailMetadata {
  description: string;
  startYear: number | string;
  issueCount: number;
  siteUrl: string;
  added: string;
  updated: string;
}

export function seriesDetailMetadata(row: SeriesDetailRow): SeriesDetailMetadata {
  const description = seriesDetailText(row.description || row.deck || row.overview || row.summary || "", 1200);
  const startYear = row.start_year || row.startYear || row.year || row.watch_year || "";
  const issueCount = Number(row.issue_count || row.issueCount || row.count_of_issues || 0);
  return {
    description,
    startYear: startYear ?? "",
    issueCount: Number.isFinite(issueCount) ? issueCount : 0,
    siteUrl: String(row.site_url || row.siteUrl || row.site_detail_url || "").trim(),
    added: seriesDetailDateLabel(row.created_at || row.metadata_date_added || ""),
    updated: seriesDetailDateLabel(row.metadata_date_last_updated || row.updated_at || ""),
  };
}

// -- reader visibility note ---------------------------------------------------

export interface SeriesReaderVisibilityModel {
  label: string;
  tone: StatusTone;
  providerLabel: string;
  total: number;
  detail: string;
  next: string;
}

export function seriesReaderVisibilityModel(row: SeriesDetailRow): SeriesReaderVisibilityModel | null {
  const total = Number(row.reader_visibility_lag_count || 0);
  if (!total) return null;
  const required = Number(row.reader_visibility_required_count || 0);
  const pending = Number(row.reader_visibility_pending_count || 0);
  const optional = Number(row.reader_visibility_optional_count || 0);
  const timeout = Number(row.reader_visibility_timeout_count || 0);
  const providerCounts = row.reader_visibility_provider_counts || {};
  const provider = String(row.reader_visibility_latest_provider || Object.keys(providerCounts)[0] || "library").trim();
  const providerLabel = sourceBucketLabel(provider || "library") || "Reader";
  let label = "Reader Sync";
  let tone: StatusTone = "warn";
  if (timeout) {
    label = "Reader Scan";
    tone = "bad";
  } else if (optional && !required && !pending) {
    label = "Folder Proof";
    tone = "good";
  }
  return {
    label,
    tone,
    providerLabel,
    total,
    detail: row.reader_visibility_note || `${compactNumber(total)} folder-complete item${total === 1 ? "" : "s"} waiting on reader visibility.`,
    next: row.reader_visibility_next_action || `Run ${providerLabel} sync when you want the reader UI to catch up.`,
  };
}

// -- operator "what's next" panel --------------------------------------------

export interface SeriesOperatorNextStep {
  label: string;
  detail: string;
  tone: StatusTone;
  // true when the vanilla version made this panel a clickable link into
  // Manual Review -- the one navigation-only (non-mutating) affordance kept
  // in this read-only pass, same precedent as the Series list island calling
  // through to InkDropSeriesNav for its own "open detail" click.
  opensManualReview: boolean;
}

export function seriesOperatorNextStep(row: SeriesDetailRow): SeriesOperatorNextStep {
  const needsYou = Number(row.needs_you_count || 0);
  const activeQueue = Number(row.active_queue_count || 0);
  const providerWait = Number(row.provider_wait_count || 0);
  const wanted = Number(row.wanted_count || 0);
  const issueWord = (count: number) => `${compactNumber(count)} issue${count === 1 ? "" : "s"}`;
  if (needsYou) {
    return { label: "Needs user", detail: `${issueWord(needsYou)} in Manual Review.`, tone: "bad", opensManualReview: true };
  }
  if (activeQueue) {
    return {
      label: "Queue active",
      detail: `${issueWord(activeQueue)} in Queue. The issue table shows whether it is searching, downloading, importing, or waiting.`,
      tone: "warn",
      opensManualReview: false,
    };
  }
  if (providerWait) {
    return { label: "Waiting", detail: `${issueWord(providerWait)} in provider/source wait.`, tone: "warn", opensManualReview: false };
  }
  if (wanted) {
    return { label: "Missing", detail: `${issueWord(wanted)} in Wanted backlog.`, tone: "warn", opensManualReview: false };
  }
  const readerLag = Number(row.reader_visibility_gap_count || row.reader_visibility_lag_count || 0);
  if (readerLag) {
    return {
      label: "Reader pending",
      detail: row.reader_visibility_note || `${issueWord(readerLag)} imported but not visible in the configured reader.`,
      tone: Number(row.reader_visibility_blocked_count || 0) ? "bad" : "warn",
      opensManualReview: false,
    };
  }
  if (row.monitored === false) {
    return { label: "Paused", detail: "Unmonitored.", tone: "", opensManualReview: false };
  }
  if (row.monitored || row.auto_grab) {
    return { label: "Monitored", detail: "No open work.", tone: "good", opensManualReview: false };
  }
  return { label: "Series", detail: "Open for issues and activity.", tone: "", opensManualReview: false };
}

// -- issue status/detail/sorting/grouping ------------------------------------

export interface SeriesDetailIssueStatus {
  label: string;
  tone: StatusTone;
  state: string;
}

export function seriesDetailIssueStatus(row: IssueRow): SeriesDetailIssueStatus {
  const gate = row.completion_gate || {};
  const rawState = gate.state || row.display_state || row.state || row.queue_state || row.wanted_status || "known";
  const stateKey = String(rawState || "").toLowerCase();
  let label = gate.label || coreStateLabel(rawState);
  if (!gate.label && row.queue_active) {
    if (["searching", "source_search", "rss_search", "source_wait"].includes(stateKey)) label = "Queue: Searching Sources";
    else if (["downloading", "download_started", "download_active"].includes(stateKey)) label = "Queue: Downloading";
    else if (["importing", "import_ready", "verifying"].includes(stateKey)) label = "Queue: Importing";
    else if (["queued", "in_progress", "active"].includes(stateKey)) label = "Queued";
  }
  const tone = gate.complete ? "good" : coreStateTone(rawState);
  return { label, tone, state: String(rawState || "") };
}

export function seriesDetailIssueDetailText(row: IssueRow): string {
  const attempt = row.last_attempt || {};
  const latestImport = row.latest_import || {};
  const bits = [
    row.next_action || "",
    row.activity_summary || "",
    row.current_source ? `source ${sourceBucketLabel(row.current_source)}` : "",
    attempt.status ? `last ${sourceBucketLabel(attempt.source || attempt.provider || "")} ${coreStateLabel(attempt.status)}` : "",
    latestImport.id ? `${latestImport.verified ? "verified" : latestImport.status || "imported"}` : "",
    latestImport.id && attempt.title ? `matched "${attempt.title}"` : "",
    row.release_date || "",
  ].filter(Boolean);
  return compactStatusSentence(bits.join(" · "), "Known issue row", 150);
}

export interface SeriesDetailIssueCounts {
  active: number;
  wanted: number;
  verified: number;
  missing: number;
  attention: number;
}

export function seriesDetailIssueCounts(rows: IssueRow[]): SeriesDetailIssueCounts {
  const counts: SeriesDetailIssueCounts = { active: 0, wanted: 0, verified: 0, missing: 0, attention: 0 };
  for (const row of rows || []) {
    const state = String(row?.display_state || row?.state || row?.queue_state || row?.wanted_status || "").toLowerCase();
    const gate = row?.completion_gate || {};
    if (["downloading", "importing", "queued", "searching", "source_wait", "in_progress"].includes(state) || row?.queue_active) counts.active += 1;
    if (row?.wanted_id && !["satisfied", "verified"].includes(String(row?.wanted_status || "").toLowerCase())) counts.wanted += 1;
    if (gate.complete || ["verified", "satisfied", "imported"].includes(state)) counts.verified += 1;
    if (!gate.complete && !row?.queue_active && ["wanted", "missing", "known"].includes(state)) counts.missing += 1;
    if (["failed", "blocked", "needs_you", "manual_exception"].includes(state)) counts.attention += 1;
  }
  return counts;
}

function arrTableIssueValue(row: IssueRow): number | string | null {
  const values = [row?.issue_number, row?.issue, row?.issue_label, row?.volume, row?.chapter, row?.number];
  for (const value of values) {
    const text = String(value ?? "").trim();
    if (!text) continue;
    const numeric = Number(text.replace(/^#/, "").replace(/^issue\s+/i, ""));
    if (Number.isFinite(numeric)) return numeric;
    const match = text.match(/(\d+(?:\.\d+)?)/);
    if (match) return Number(match[1]);
    return text.toLowerCase();
  }
  return null;
}

function arrTableDefaultIssueOrderValue(row: IssueRow): number | string | null {
  const issue = arrTableIssueValue(row);
  if (issue !== null && issue !== undefined && issue !== "") return issue;
  const titleValues = [row?.issue_title, row?.title, row?.display_title, row?.name];
  for (const value of titleValues) {
    const match = String(value || "").match(/#\s*(\d+(?:\.\d+)?)/);
    if (match) return Number(match[1]);
  }
  return null;
}

export function seriesDetailIssueSortValue(row: IssueRow): number {
  const value = arrTableDefaultIssueOrderValue(row);
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return Infinity;
}

export function seriesDetailIssueSortedRows(rows: IssueRow[]): IssueRow[] {
  return [...(rows || [])].sort((a, b) => {
    const diff = seriesDetailIssueSortValue(a) - seriesDetailIssueSortValue(b);
    if (Number.isFinite(diff) && diff !== 0) return diff;
    return String(a?.issue_number || a?.issue_title || "").localeCompare(
      String(b?.issue_number || b?.issue_title || ""),
      undefined,
      { numeric: true, sensitivity: "base" },
    );
  });
}

export function seriesDetailIssueGroupKey(row: IssueRow): string {
  const releaseDate = String(row.release_date || "").trim();
  const yearMatch = releaseDate.match(/\b(19|20)\d{2}\b/);
  if (yearMatch) return `year:${yearMatch[0]}`;
  const issueText = String(row.issue_number || row.normalized_number || "").trim();
  const volumeMatch = issueText.match(/(?:^|\b)(?:v|vol|volume)\.?\s*(\d+)/i);
  if (volumeMatch) return `volume:${volumeMatch[1]}`;
  return "undated";
}

export function seriesDetailIssueGroupLabel(key: string): string {
  if (key.startsWith("year:")) return `${key.slice(5)} Issues`;
  if (key.startsWith("volume:")) return `Volume ${key.slice(7)}`;
  return "Undated Issues";
}

function seriesDetailIssueGroupSortValue(rows: IssueRow[]): number {
  let min = Infinity;
  for (const row of rows || []) {
    const value = seriesDetailIssueSortValue(row);
    if (value < min) min = value;
  }
  return min;
}

export function seriesDetailIssueGroupEntries(rows: IssueRow[]): Array<[string, IssueRow[]]> {
  const groups = new Map<string, IssueRow[]>();
  for (const issue of rows || []) {
    const key = seriesDetailIssueGroupKey(issue);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(issue);
  }
  return Array.from(groups.entries()).sort((a, b) => {
    const diff = seriesDetailIssueGroupSortValue(a[1]) - seriesDetailIssueGroupSortValue(b[1]);
    if (Number.isFinite(diff) && diff !== 0) return diff;
    return seriesDetailIssueGroupLabel(a[0]).localeCompare(seriesDetailIssueGroupLabel(b[0]));
  });
}

function seriesDetailIssueLooksOperationalTitle(value: unknown): boolean {
  const text = String(value || "").trim().toLowerCase();
  if (!text) return true;
  return [
    /^waiting for next worker pass\b/,
    /^queued for source search\b/,
    /^known issue tracked by inkdrop\b/,
    /^library visible\b/,
    /^library visible in kavita\b/,
    /^no safe candidate\b/,
    /^source wait\b/,
    /^provider wait\b/,
    /^queue active\b/,
    /^queue moving\b/,
    /^download(?:ing)?\b/,
    /^import(?:ing|ed)?\b/,
    /^verified(?: by inkdrop)?\b/,
    /^satisfied\b/,
  ].some((pattern) => pattern.test(text));
}

export function seriesDetailIssueDisplayTitle(row: IssueRow): string {
  const issueTitle = String(row.issue_title || "").trim();
  if (issueTitle && !seriesDetailIssueLooksOperationalTitle(issueTitle)) return issueTitle;
  // row.next_action/activity_summary (seriesDetailIssueDetailText's source
  // material) are always operational/status text by construction -- never a
  // real issue title -- so they are not used as a title-fallback source.
  const number = String(row.issue_number || row.normalized_number || "").trim();
  if (number) return number.startsWith("#") ? `Issue ${number}` : `Issue #${number}`;
  const releaseDate = String(row.release_date || "").trim();
  if (releaseDate) return `Issue · ${releaseDate}`;
  return "Issue";
}

// -- per-issue-row action wiring ---------------------------------------------

export function rowLinkedEntities(row: IssueRow): LinkedEntities {
  const links: LinkedEntities = { ...(row.linked_entities || {}) };
  const direct = row as Record<string, unknown>;
  for (const key of ["series_id", "edition_id", "issue_id", "unit_id", "wanted_id", "queue_id", "source_attempt_id", "download_task_id", "import_id", "review_id"] as const) {
    if (!links[key] && direct[key]) links[key] = String(direct[key]);
  }
  const latestImport = row.latest_import || {};
  if (!links.import_id && latestImport.id) links.import_id = latestImport.id;
  return links;
}

export function seriesDetailIssueIsSearchable(row: IssueRow): boolean {
  if (!row.wanted_id) return false;
  return !["satisfied", "verified", "imported"].includes(String(row.wanted_status || "").toLowerCase());
}

export function seriesDetailIssueNeedsSearchPath(row: IssueRow): boolean {
  if (seriesDetailIssueIsSearchable(row)) return true;
  const status = seriesDetailIssueStatus(row);
  const values = [row.display_state, row.state, row.wanted_status, status.state, status.label].map((value) =>
    String(value || "").toLowerCase(),
  );
  return values.some((value) => ["wanted", "missing", "known", "searching sources", "queue: searching sources"].includes(value));
}
