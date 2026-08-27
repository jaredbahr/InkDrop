import { useEffect, useRef, useState } from "react";
import { request, InkDropApiError } from "../api";
import { useRowActions } from "../rowActions";
import type { QueueRow, QueueRowDetail, QueueTransfer, QueueViewPayload, QueueRunResult, QueueBlockResult, StateViewFilter } from "./queueTypes";
import { rowStateLabel } from "./stateLabel";

const PAGE_SIZE = 80;

// Only real server filters appear as chips (QUEUE_FILTERS in
// inkdrop_state.py); counts come from the payload's filter options.
// The page-masthead already renders the Working/Downloading/Importing/
// Waiting stat cards from the core summary -- the island deliberately does
// NOT duplicate them (two card rows with subtly different counts read as a
// bug; same reasoning as History's empty masthead precedent).
const CHIP_FILTERS = ["all", "downloading", "importing", "queued", "exceptions"] as const;

function rowTitle(row: QueueRow): string {
  const issue = row.issue_number ? ` #${row.issue_number}` : "";
  return `${row.series || "Unknown"}${issue}`;
}

// Mirrors operationalRowSourceLabel's fallback chain (inkdrop_web.py) closely
// enough for a row line -- the full version also reaches into download-task
// fields this row shape doesn't carry in "thin" mode.
function sourceLabel(row: QueueRow): string {
  const raw = row.next_concrete_source_label || row.next_source_label || row.display_source || row.current_source || row.source_key || "";
  return raw.toLowerCase() === "source" ? "" : raw;
}

function nextActionText(row: QueueRow): string {
  return row.next_action || row.why_not_grabbed || row.wait_reason_label || row.activity_summary || "";
}

// Grouping vocabulary matches the queue's own state buckets -- rows are
// grouped by what they are doing, in the order work happens.
type GroupKey = "downloading" | "importing" | "searching" | "queued" | "attention";
const GROUP_ORDER: GroupKey[] = ["attention", "downloading", "importing", "searching", "queued"];
const GROUP_LABELS: Record<GroupKey, string> = {
  attention: "Needs attention",
  downloading: "Downloading",
  importing: "Importing",
  searching: "Searching",
  queued: "Queued",
};

function groupFor(row: QueueRow): GroupKey {
  const state = String(row.state || "").toLowerCase();
  if (["needs_you", "failed", "blocked"].includes(state)) return "attention";
  if (state === "downloading") return "downloading";
  if (state === "importing") return "importing";
  if (state === "searching") return "searching";
  return "queued";
}

function statusTone(row: QueueRow): string {
  const group = groupFor(row);
  if (group === "attention") return "bad";
  if (group === "downloading") return "info";
  if (group === "importing") return "good";
  if (group === "searching") return "accent";
  return "warn";
}

const statusText = (row: QueueRow): string => rowStateLabel(row, "queued");

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount >= 100 ? Math.round(amount) : amount.toFixed(1)} ${units[unit]}`;
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.round((seconds % 86400) / 3600)}h`;
}

// A bar renders ONLY when the downloader reported a real percentage
// (progress_kind "determinate"). Stages with no meaningful percent -- peer
// waits, source ladder waits -- keep their status text and no bar.
//
// `labelId` points at the row's own visible title element. Without it every
// bar on the page exposed the same empty accessible name and nothing but
// 0/100/N: with several transfers running at once, assistive technology
// announced a series of anonymous, indistinguishable progress bars. The
// percent, byte counts, rate, ETA and stalled marker were rendered as
// unassociated sibling text, so none of it reached the bar either -- hence
// aria-valuetext carrying the same facts the sighted user reads.
function transferBar(transfer: QueueTransfer | undefined, labelId: string) {
  if (!transfer || transfer.progress_kind !== "determinate") return null;
  const percent = Number(transfer.percent_complete);
  if (!Number.isFinite(percent)) return null;
  const state = String(transfer.transfer_state || "");
  if (["removed", "failed", "unknown"].includes(state)) return null;
  const done = percent >= 100 || state === "completed" || state === "seeding";
  const completed = Number(transfer.bytes_completed);
  const total = Number(transfer.bytes_total);
  const rate = Number(transfer.download_rate_bytes_per_second);
  const eta = Number(transfer.eta_seconds);
  const bits: string[] = [];
  if (Number.isFinite(completed) && completed > 0 && Number.isFinite(total) && total > 0) {
    bits.push(`${formatBytes(completed)} / ${formatBytes(total)}`);
  } else if (Number.isFinite(total) && total > 0) {
    bits.push(formatBytes(total));
  }
  if (!done && Number.isFinite(rate) && rate > 0) bits.push(`${formatBytes(rate)}/s`);
  if (!done && Number.isFinite(eta) && eta > 0) bits.push(`${formatDuration(eta)} left`);
  // Same facts as the visible meta line, minus the check glyph and with
  // comma separation so a screen reader reads it as a sentence rather than
  // as punctuation.
  const valueText = [
    done ? "Transfer complete" : `${Math.round(percent)}%`,
    ...bits,
    transfer.stalled && !done ? "stalled" : "",
  ]
    .filter(Boolean)
    .join(", ");
  return (
    <div className={`queue-progress ${done ? "done" : ""} ${transfer.stalled ? "stalled" : ""}`.trim()}>
      <div
        className="queue-progress-track"
        role="progressbar"
        aria-labelledby={labelId}
        aria-valuenow={Math.round(percent)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={valueText}
      >
        <div className="queue-progress-fill" style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} />
      </div>
      <span className="queue-progress-meta">
        {done ? "✓ Transfer complete" : `${Math.round(percent)}%`}
        {bits.length > 0 ? ` · ${bits.join(" · ")}` : ""}
        {transfer.stalled && !done ? " · stalled" : ""}
      </span>
    </div>
  );
}

function formatStamp(value: number | null | undefined): string {
  const stamp = Number(value);
  if (!Number.isFinite(stamp) || stamp <= 0) return "";
  return new Date(stamp * 1000).toLocaleString();
}

// Only fields InkDrop genuinely tracks, rendered only when present.
function detailFields(detail: QueueRowDetail): Array<[string, string]> {
  const task = detail.download_task || {};
  const attempt = detail.last_attempt || {};
  const imported = detail.latest_import || {};
  const rows: Array<[string, string]> = [];
  const push = (label: string, value: unknown) => {
    const text = String(value ?? "").trim();
    if (text) rows.push([label, text]);
  };
  push("Release", task.title || attempt.title);
  push("File", task.local_path || task.save_path);
  push("Imported to", imported.dest_path);
  if (Number.isFinite(Number(task.size_bytes)) && Number(task.size_bytes) > 0) push("Size", formatBytes(Number(task.size_bytes)));
  push("Download client", [task.download_client, task.external_id ? `(${task.external_id})` : ""].filter(Boolean).join(" "));
  push("Provider", task.provider || attempt.provider);
  push("Peer", task.peer);
  push("Search query", detail.query);
  push("Last event", detail.last_event);
  push("Failure", task.failure_reason || attempt.failure_reason);
  if (Number.isFinite(Number(detail.attempt_count)) && Number(detail.attempt_count) > 0) push("Attempts", String(detail.attempt_count));
  push("Queued at", formatStamp(detail.created_at));
  push("Transfer started", formatStamp(task.started_at));
  push("Transfer completed", formatStamp(task.completed_at));
  push("Last update", formatStamp(detail.updated_at));
  return rows;
}

function buildEndpoint(offset: number, queueFilter: string): string {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    summary: "compact",
    rows: "thin",
    offset: String(offset),
    queue_filter: queueFilter || "active",
  });
  return `/api/inkdrop-state/queue?${params.toString()}`;
}

export function Queue({ payload }: { payload: QueueViewPayload }) {
  const [rows, setRows] = useState<QueueRow[]>(payload.rows || []);
  const [offset, setOffset] = useState(payload.offset || 0);
  const [totalCount, setTotalCount] = useState(payload.total_count || 0);
  const [hasMore, setHasMore] = useState(Boolean(payload.has_more));
  const [queueFilter, setQueueFilter] = useState(payload.queue_filter || "active");
  const [filters, setFilters] = useState<StateViewFilter[]>(payload.filters || []);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<ReadonlySet<GroupKey>>(new Set());
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<QueueRowDetail | null>(null);
  // The route withholds exact paths, the download client's item id and the
  // provider peer's username by default; this says whether this row actually
  // had something withheld, so the reveal offer appears only where it means
  // something.
  const [detailRedacted, setDetailRedacted] = useState(false);
  const [revealing, setRevealing] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const { pendingIds, doneIds, actionError, clearActionError, runRowAction } = useRowActions(() => loadPage(offset, queueFilter, { quiet: true }));
  const refreshTimer = useRef<number>(0);

  // A fresh `payload` reference only arrives when the surrounding shell
  // re-fetched page one on our behalf (filter change, section re-entry) --
  // treat it as the new page-one truth every time, same as Wanted/Blocklist.
  useEffect(() => {
    setRows(payload.rows || []);
    setOffset(payload.offset || 0);
    setTotalCount(payload.total_count || 0);
    setHasMore(Boolean(payload.has_more));
    setQueueFilter(payload.queue_filter || "active");
    setFilters(payload.filters || []);
    setError(null);
    clearActionError();
  }, [payload]);

  async function loadPage(nextOffset: number, nextFilter?: string, options?: { quiet?: boolean }) {
    const filter = nextFilter ?? queueFilter;
    if (!options?.quiet) {
      setLoading(true);
      setError(null);
    }
    try {
      const data = await request<{ ok: boolean; view: QueueViewPayload }>(buildEndpoint(nextOffset, filter));
      const view = data.view;
      setRows(view.rows || []);
      setOffset(view.offset ?? nextOffset);
      setTotalCount(view.total_count || 0);
      setHasMore(Boolean(view.has_more));
      if (view.filters?.length) setFilters(view.filters);
      if (view.queue_filter) setQueueFilter(view.queue_filter);
    } catch (cause) {
      if (!options?.quiet) setError(cause instanceof InkDropApiError ? cause.message : "Could not load Queue page.");
    } finally {
      if (!options?.quiet) setLoading(false);
    }
  }

  // Active transfers move on their own -- keep the page current while any
  // are on screen and the tab is visible, quietly (no spinner, no reflow of
  // scroll position; the row set is refreshed in place).
  const hasActiveTransfer = rows.some((row) => row.transfer?.transfer_state === "downloading");
  useEffect(() => {
    window.clearInterval(refreshTimer.current);
    if (!hasActiveTransfer) return;
    refreshTimer.current = window.setInterval(() => {
      if (document.visibilityState === "visible") void loadPage(offset, queueFilter, { quiet: true });
    }, 10000);
    return () => window.clearInterval(refreshTimer.current);
  }, [hasActiveTransfer, offset, queueFilter]);

  function chipCount(value: string): number | null {
    const entry = filters.find((item) => item.value === value);
    return entry ? entry.count : null;
  }

  function chipLabel(value: string): string {
    const entry = filters.find((item) => item.value === value);
    if (value === "all") return "All active";
    return entry?.label || value;
  }

  function selectFilter(value: string) {
    setQueueFilter(value);
    void loadPage(0, value);
  }

  // The focused (full-row) payload carries the download task, attempt and
  // import records the thin table shape deliberately omits.
  async function toggleDetail(row: QueueRow) {
    if (detailId === row.id) {
      // A second click while the first click's fetch is still in flight used
      // to collapse the row immediately -- setDetailId(row.id) commits (and
      // re-renders) well before the request resolves, so a normal-speed
      // double-click reliably lands here before the user ever sees the
      // loading state or the data, and the eventual setDetail(full) below is
      // silently discarded by the detailId===row.id render guard. Ignore the
      // toggle-closed while its own fetch hasn't finished yet; once it has
      // (success or error), a click here still closes it as expected.
      if (detailLoading) return;
      setDetailId(null);
      setDetail(null);
      setDetailError(null);
      setDetailRedacted(false);
      return;
    }
    setDetailId(row.id);
    setDetail(null);
    setDetailError(null);
    setDetailRedacted(false);
    setDetailLoading(true);
    try {
      await fetchDetail(row.id, false);
    } catch (cause) {
      setDetailError(cause instanceof InkDropApiError ? cause.message : "Could not load details.");
    } finally {
      setDetailLoading(false);
    }
  }

  async function fetchDetail(rowId: string, reveal: boolean) {
    const data = await request<{ ok: boolean; view: { rows?: QueueRowDetail[]; operational_detail_redacted?: boolean } }>(
      `/api/inkdrop-state/queue?queue_id=${encodeURIComponent(rowId)}&summary=compact&limit=1${reveal ? "&reveal=1" : ""}`,
    );
    const full = data.view?.rows?.[0];
    if (!full) throw new InkDropApiError("No detail available for this row.", { status: 200, code: "queue_detail_missing" });
    setDetail(full);
    setDetailRedacted(Boolean(data.view?.operational_detail_redacted));
  }

  // Exact paths, the download client's item id and the provider peer's
  // username are withheld by default -- an operator who needs them to
  // troubleshoot asks for them, the server checks that they are an admin, and
  // the ask is written to the auth audit log.
  async function revealDetail(rowId: string) {
    setRevealing(true);
    setDetailError(null);
    try {
      await fetchDetail(rowId, true);
    } catch (cause) {
      setDetailError(
        cause instanceof InkDropApiError && cause.status === 403
          ? "Only an administrator can show the full paths and client identifiers."
          : cause instanceof InkDropApiError
            ? cause.message
            : "Could not show the full details.",
      );
    } finally {
      setRevealing(false);
    }
  }

  function removeRow(row: QueueRow) {
    const client = row.transfer?.client;
    const clientNote = client ? ` and delete it from ${client}` : "";
    if (!window.confirm(`Remove ${rowTitle(row)} from the queue${clientNote}? The issue stays on the Wanted backlog.`)) return;
    void runRowAction(row.id, rowTitle(row), "Removed", async () => {
      const data = await request<QueueRunResult>("/api/inkdrop-state/queue/remove", {
        method: "POST",
        body: { id: row.id, revision: row.revision },
      });
      if (!data.ok) throw new InkDropApiError("Could not remove this queue row.", { status: 200, code: "queue_remove_failed" });
    });
  }

  function runRetry(row: QueueRow) {
    const label = row.state === "downloading" || row.state === "importing" ? "Refreshed" : "Queued";
    void runRowAction(row.id, rowTitle(row), label, async () => {
      const data = await request<QueueRunResult>("/api/inkdrop-state/queue/run", {
        method: "POST",
        body: { id: row.id, revision: row.revision },
      });
      if (!data.ok) throw new InkDropApiError("Could not retry this queue row.", { status: 200, code: "queue_run_failed" });
    });
  }

  // Excludes whichever source(s) most recently failed for this exact item --
  // server-computed from source_attempts, not user-picked -- so this stays a
  // one-click action like Retry/Remove instead of asking the user to diagnose
  // which provider is at fault.
  function runRetryNewSource(row: QueueRow) {
    void runRowAction(row.id, rowTitle(row), "Queued", async () => {
      const data = await request<QueueRunResult>("/api/inkdrop-state/queue/retry-new-source", {
        method: "POST",
        body: { id: row.id, revision: row.revision },
      });
      if (!data.ok) {
        const reason = data.result?.reason;
        throw new InkDropApiError(
          reason === "no_alternate_source_available"
            ? "No alternate source is configured for this item -- every source already failed."
            : "Could not retry with another source.",
          { status: 200, code: "queue_retry_new_source_failed" },
        );
      }
    });
  }

  // Idempotent by design (reopen_stuck_import checks proof before touching
  // anything), so it's safe to expose broadly on importing/verified rows
  // without risking a redundant re-download of a file that's already fine.
  function runReopenImport(row: QueueRow) {
    void runRowAction(row.id, rowTitle(row), "Reopened", async () => {
      const data = await request<QueueRunResult>("/api/inkdrop-state/queue/reopen-import", {
        method: "POST",
        body: { id: row.id, revision: row.revision },
      });
      if (!data.ok) throw new InkDropApiError("Could not reopen this import.", { status: 200, code: "queue_reopen_import_failed" });
    });
  }

  function blockCandidate(row: QueueRow, sourceAttemptId: string | undefined) {
    if (!window.confirm(`Permanently block this release for ${rowTitle(row)}? InkDrop will never offer it again for this issue.`)) return;
    void runRowAction(row.id, rowTitle(row), "Blocked", async () => {
      const data = await request<QueueBlockResult>("/api/inkdrop-state/source-memory/block", {
        method: "POST",
        body: { id: row.id, revision: row.revision, source_attempt_id: sourceAttemptId, retry: true },
      });
      if (!data.ok) {
        const reason = data.result?.reason;
        throw new InkDropApiError(
          reason === "no_candidate_to_block" ? "No recent candidate is on record for this row to block." : "Could not block this release.",
          { status: 200, code: "queue_block_candidate_failed" },
        );
      }
      setDetailId(null);
      setDetail(null);
    });
  }

  function toggleGroup(key: GroupKey) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const attentionCount = chipCount("exceptions") || 0;
  const groups = GROUP_ORDER.map((key) => ({
    key,
    rows: rows.filter((row) => groupFor(row) === key),
  })).filter((group) => group.rows.length > 0);

  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + rows.length;

  return (
    <div className="inkdrop-react-queue">
      {(error || actionError) && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error || actionError}
        </div>
      )}

      {attentionCount > 0 && (
        <div className="queue-attention-banner" role="status">
          <span>
            {/* Names the facet it counts. The vanilla masthead above this
                renders its own banner counting Manual Review rows, and both
                used to say "need attention" for different populations. */}
            {attentionCount === 1
              ? "1 of these rows is an exception."
              : `${attentionCount} of these rows are exceptions.`}
          </span>
          <button type="button" onClick={() => selectFilter("exceptions")}>
            Review problems
          </button>
        </div>
      )}

      <div className="queue-filter-chips" role="tablist" aria-label="Queue filters">
        {CHIP_FILTERS.map((value) => {
          const count = chipCount(value);
          return (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={queueFilter === value}
              className={`queue-filter-chip ${queueFilter === value ? "selected" : ""}`.trim()}
              onClick={() => selectFilter(value)}
            >
              {chipLabel(value)}
              {count !== null && <span className="queue-filter-chip-count">{count}</span>}
            </button>
          );
        })}
      </div>

      {groups.map((group) => (
        <section key={group.key} className={`queue-group ${group.key}`}>
          <button type="button" className="queue-group-header" aria-expanded={!collapsed.has(group.key)} onClick={() => toggleGroup(group.key)}>
            <span className={`queue-group-caret ${collapsed.has(group.key) ? "closed" : ""}`.trim()} aria-hidden="true" />
            {GROUP_LABELS[group.key]} ({group.rows.length})
          </button>
          {!collapsed.has(group.key) && (
            <ul className="queue-row-list">
              {group.rows.map((row, rowIndex) => {
                const canAct = Boolean(row.id);
                const actionLabel = row.state === "downloading" || row.state === "importing" ? "Refresh" : "Retry";
                const canRetryNewSource = canAct && group.key === "attention";
                const canReopenImport = canAct && (group.key === "importing" || group.key === "attention");
                const source = sourceLabel(row);
                const next = nextActionText(row);
                // The row's visible title is what names its progress bar.
                // row.id is normally the queue id, but a row without one
                // still has to get a document-unique id here or two bars
                // would collide on the same label.
                const titleId = `queue-row-title-${row.id || `${group.key}-${rowIndex}`}`;
                const bar = transferBar(row.transfer, titleId);
                return (
                  <li key={row.id} className="queue-row">
                    {row.image ? (
                      <img className="queue-row-cover" src={row.image} alt="" loading="lazy" decoding="async" />
                    ) : (
                      <span className="queue-row-cover queue-row-cover-empty" aria-hidden="true">▣</span>
                    )}
                    <div className="queue-row-main">
                      <div className="queue-row-title-line">
                        <strong id={titleId}>{rowTitle(row)}</strong>
                        {row.media_type && <span className="queue-row-format">{String(row.media_type).toUpperCase()}</span>}
                      </div>
                      <div className="queue-row-status-line">
                        <span className={`queue-row-status ${statusTone(row)}`}>{statusText(row)}</span>
                        {source && <span className="queue-row-source">{source}</span>}
                        {row.transfer?.elapsed_seconds ? (
                          <span className="queue-row-elapsed">{formatDuration(Number(row.transfer.elapsed_seconds))} elapsed</span>
                        ) : null}
                      </div>
                      {bar}
                      {!bar && next && <div className="queue-row-next">{next}</div>}
                    </div>
                    <div className="queue-row-actions">
                      {canAct && (
                        <button
                          type="button"
                          disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                          onClick={() => runRetry(row)}
                          title="Retry or refresh this queue row"
                        >
                          {pendingIds.has(row.id) ? "Working…" : doneIds.get(row.id) || actionLabel}
                        </button>
                      )}
                      {canRetryNewSource && (
                        <button
                          type="button"
                          disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                          onClick={() => runRetryNewSource(row)}
                          title="Retry, skipping whichever source(s) most recently failed for this item"
                        >
                          {pendingIds.has(row.id) ? "Working…" : doneIds.get(row.id) || "Try another source"}
                        </button>
                      )}
                      {canReopenImport && (
                        <button
                          type="button"
                          disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                          onClick={() => runReopenImport(row)}
                          title="Re-check this import; only changes anything if it's genuinely stuck"
                        >
                          {pendingIds.has(row.id) ? "Working…" : doneIds.get(row.id) || "Reopen import"}
                        </button>
                      )}
                      {canAct && (
                        <button
                          type="button"
                          aria-expanded={detailId === row.id}
                          disabled={detailId === row.id && detailLoading}
                          onClick={() => void toggleDetail(row)}
                          title="Show what InkDrop tracks for this row"
                        >
                          {detailId === row.id && detailLoading ? "Loading…" : "Details"}
                        </button>
                      )}
                      {canAct && (
                        <button
                          type="button"
                          className="queue-row-remove"
                          disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                          onClick={() => removeRow(row)}
                          title="Remove from the download client and InkDrop's queue"
                          aria-label={`Remove ${rowTitle(row)} from the queue`}
                        >
                          ✕
                        </button>
                      )}
                    </div>
                    {detailId === row.id && (
                      <div className="queue-row-detail">
                        {detailError && <p className="queue-row-detail-error">{detailError}</p>}
                        {!detail && !detailError && <p className="queue-row-detail-loading">Loading details…</p>}
                        {detail && (
                          <dl>
                            {detailFields(detail).map(([label, value]) => (
                              <div key={label} className="queue-row-detail-field">
                                <dt>{label}</dt>
                                <dd>{value}</dd>
                              </div>
                            ))}
                            {detailFields(detail).length === 0 && <p className="queue-row-detail-loading">Nothing extra is tracked for this row yet.</p>}
                          </dl>
                        )}
                        {detail && detailRedacted && (
                          <p className="queue-row-detail-redacted">
                            Showing file names only. The full paths, the download client's item id and the
                            provider peer are hidden.{" "}
                            <button
                              type="button"
                              className="queue-row-reveal-btn"
                              disabled={revealing}
                              onClick={() => void revealDetail(row.id)}
                              title="Administrators only. Showing these is recorded in the security audit log."
                            >
                              {revealing ? "Showing…" : "Show them"}
                            </button>
                          </p>
                        )}
                        {detail && detail.last_attempt?.source_attempt_id && (
                          <button
                            type="button"
                            className="queue-row-block-btn"
                            disabled={pendingIds.has(row.id) || doneIds.has(row.id)}
                            onClick={() => blockCandidate(row, detail.last_attempt?.source_attempt_id)}
                            title="Permanently reject this release so InkDrop stops offering it for this issue"
                          >
                            Block this release
                          </button>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      ))}
      {rows.length === 0 && !loading && <p className="queue-empty-line">Nothing in the queue for this view.</p>}

      <div className="inkdrop-react-pager">
        <span>{totalCount > 0 ? `${pageStart}-${pageEnd} of ${totalCount}` : "0 of 0"}</span>
        <button type="button" disabled={loading || offset === 0} onClick={() => loadPage(Math.max(0, offset - PAGE_SIZE))}>
          Previous
        </button>
        <button type="button" disabled={loading || !hasMore} onClick={() => loadPage(offset + PAGE_SIZE)}>
          Next
        </button>
      </div>
    </div>
  );
}
