import { useEffect, useRef, useState } from "react";
import { request, InkDropApiError } from "../api";
import { useRowActions } from "../rowActions";
import type { QueueRow, QueueRowDetail, QueueTransfer, QueueViewPayload, QueueRunResult, StateViewFilter } from "./queueTypes";

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

function statusText(row: QueueRow): string {
  const raw = row.display_state_label || row.display_state || row.state || "queued";
  return raw.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

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
function transferBar(transfer: QueueTransfer | undefined) {
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
  return (
    <div className={`queue-progress ${done ? "done" : ""} ${transfer.stalled ? "stalled" : ""}`.trim()}>
      <div className="queue-progress-track" role="progressbar" aria-valuenow={Math.round(percent)} aria-valuemin={0} aria-valuemax={100}>
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
  const [detailError, setDetailError] = useState<string | null>(null);
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
      setDetailId(null);
      setDetail(null);
      setDetailError(null);
      return;
    }
    setDetailId(row.id);
    setDetail(null);
    setDetailError(null);
    try {
      const data = await request<{ ok: boolean; view: { rows?: QueueRowDetail[] } }>(
        `/api/inkdrop-state/queue?queue_id=${encodeURIComponent(row.id)}&summary=compact&limit=1`,
      );
      const full = data.view?.rows?.[0];
      if (!full) throw new InkDropApiError("No detail available for this row.", { status: 200, code: "queue_detail_missing" });
      setDetail(full);
    } catch (cause) {
      setDetailError(cause instanceof InkDropApiError ? cause.message : "Could not load details.");
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
            {attentionCount === 1 ? "1 item needs attention." : `${attentionCount} items need attention.`}
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
              {group.rows.map((row) => {
                const canAct = Boolean(row.id);
                const actionLabel = row.state === "downloading" || row.state === "importing" ? "Refresh" : "Retry";
                const source = sourceLabel(row);
                const next = nextActionText(row);
                const bar = transferBar(row.transfer);
                return (
                  <li key={row.id} className="queue-row">
                    {row.image ? (
                      <img className="queue-row-cover" src={row.image} alt="" loading="lazy" decoding="async" />
                    ) : (
                      <span className="queue-row-cover queue-row-cover-empty" aria-hidden="true">▣</span>
                    )}
                    <div className="queue-row-main">
                      <div className="queue-row-title-line">
                        <strong>{rowTitle(row)}</strong>
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
                      {canAct && (
                        <button
                          type="button"
                          aria-expanded={detailId === row.id}
                          onClick={() => void toggleDetail(row)}
                          title="Show what InkDrop tracks for this row"
                        >
                          Details
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
