import { useEffect, useRef, useState } from "react";
import { request, InkDropApiError } from "../api";
import { useRowActions } from "../rowActions";
import {
  RELIABILITY_STAGE_ORDER,
  type ReliabilityBandKey,
  type ReliabilityBucketKey,
  type ReliabilityEvidence,
  type ReliabilityItem,
  type ReliabilitySignal,
  type ReliabilitySignalsPayload,
  type ReliabilityViewPayload,
} from "./reliabilityTypes";

const PAGE_SIZE = 40;

// Tone follows the band, not the bucket. The band is the page's judgment about
// whether something is a problem, and keeping the two in step is the whole
// point of banding: a green badge on a row that has been stuck for six weeks is
// exactly the lie this rework exists to remove.
const BAND_TONE: Record<ReliabilityBandKey, string> = {
  needs_attention: "bad",
  waiting: "warn",
  healthy: "good",
  // Paused is not good news and not bad news -- you decided. Colouring it
  // either way would editorialise a choice the operator already made.
  paused: "",
};

const BAND_ORDER: ReliabilityBandKey[] = ["needs_attention", "waiting", "healthy", "paused"];

// `other` and `superseded_duplicate` sit in the healthy band but are neither
// good news nor bad -- they are housekeeping, and a green card would overstate
// them.
const NEUTRAL_BUCKETS = new Set<ReliabilityBucketKey>(["superseded_duplicate", "other", "not_pursued"]);

// "Stop trying" is offered where an operator could reasonably conclude the book
// is not findable -- everything in the Waiting band. It is not offered on rows
// that are mid-flight, need a decision, or are already paused.
const CAN_PAUSE = new Set<ReliabilityBucketKey>([
  "budget_starved",
  "candidate_awaiting_pick",
  "candidate_unconfirmed",
  "candidates_rejected",
  "search_never_completed",
  "searched_nothing_found",
  "not_searched_yet",
]);

function bucketTone(item: { bucket: ReliabilityBucketKey; bucket_band?: ReliabilityBandKey }): string {
  if (NEUTRAL_BUCKETS.has(item.bucket)) return "";
  return BAND_TONE[item.bucket_band || "healthy"] || "";
}

function buildEndpoint(offset: number, bucketFilter: string): string {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(offset),
  });
  if (bucketFilter) params.set("source_filter", bucketFilter);
  return `/api/inkdrop-state/reliability?${params.toString()}`;
}

function itemTitle(item: ReliabilityItem): string {
  const issue = item.issue_number ? ` #${item.issue_number}` : "";
  return `${item.series || "Unknown series"}${issue}`;
}

function relativeAge(stampSeconds?: number | null): string {
  if (!stampSeconds) return "";
  const seconds = Math.max(0, Date.now() / 1000 - stampSeconds);
  if (seconds < 90) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 90) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 36) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function pageNumbers(current: number, pageCount: number): (number | "gap")[] {
  if (pageCount <= 7) return Array.from({ length: pageCount }, (_, i) => i + 1);
  const pages = new Set<number>([1, 2, current - 1, current, current + 1, pageCount - 1, pageCount]);
  const sorted = [...pages].filter((p) => p >= 1 && p <= pageCount).sort((a, b) => a - b);
  const out: (number | "gap")[] = [];
  let prev = 0;
  for (const page of sorted) {
    if (prev && page - prev > 1) out.push("gap");
    out.push(page);
    prev = page;
  }
  return out;
}

function StageTracker({ item }: { item: ReliabilityItem }) {
  const currentIndex = RELIABILITY_STAGE_ORDER.indexOf(item.stage);
  return (
    <ol className="reliability-stage-tracker" aria-label={`Pipeline stage: ${item.stage_label}`}>
      {RELIABILITY_STAGE_ORDER.map((stage, index) => {
        const status = index < currentIndex ? "done" : index === currentIndex ? "current" : "pending";
        return (
          <li key={stage} className={`reliability-stage-step ${status}`}>
            <span className="reliability-stage-dot" aria-hidden="true" />
            <span className="reliability-stage-label">
              {stage.charAt(0).toUpperCase() + stage.slice(1)}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

// An action renders only where its target exists. That is the whole rule, and
// it is why each of these is a bucket set rather than a truthiness check on an
// id: an item can have a queue row and a last attempt and still have nothing to
// block, nothing to reopen, and nothing to clear.
const CAN_SEARCH_NOW = new Set<ReliabilityBucketKey>([
  "budget_starved",
  "not_searched_yet",
  "searched_nothing_found",
  "search_never_completed",
  "candidate_awaiting_pick",
  "candidate_unconfirmed",
  "candidates_rejected",
  "active_unconfirmed",
]);
// active_unconfirmed with queue_state 'downloading' or 'source_wait' is
// withheld from Search Now even though the bucket is otherwise eligible: the
// A live audit found no claim or task evidence proving these two states
// are live, but that is not proof a real external client transfer has
// stopped either -- searching/importing carry no such risk, since retrying
// them cannot duplicate a download that was never dispatched.
const SEARCH_NOW_WITHHELD_QUEUE_STATES = new Set(["downloading", "source_wait"]);
function canSearchNowForItem(item: ReliabilityItem): boolean {
  if (!CAN_SEARCH_NOW.has(item.bucket)) return false;
  if (item.bucket === "active_unconfirmed" && SEARCH_NOW_WITHHELD_QUEUE_STATES.has(item.queue_state || "")) {
    return false;
  }
  return true;
}
const CAN_TRY_ANOTHER_SOURCE = new Set<ReliabilityBucketKey>([
  "manual_review_needed",
  "stuck_import",
  "known_bad_blocked",
]);
const CAN_REOPEN_IMPORT = new Set<ReliabilityBucketKey>(["stuck_import", "manual_review_needed"]);
const CAN_CLEAR_ROW = new Set<ReliabilityBucketKey>(["superseded_duplicate"]);

function hasBlockableRelease(item: ReliabilityItem): boolean {
  return Boolean(
    item.queue_id &&
      item.last_attempt_id &&
      (item.last_attempt_candidate_identity || item.last_attempt_download_url_hash),
  );
}

type ItemCardActions = {
  pendingIds: ReadonlySet<string>;
  doneIds: ReadonlyMap<string, string>;
  runSearchNow: (item: ReliabilityItem) => void;
  runRetryNewSource: (item: ReliabilityItem) => void;
  runReopenImport: (item: ReliabilityItem) => void;
  runClearRow: (item: ReliabilityItem) => void;
  blockCandidate: (item: ReliabilityItem) => void;
  allowRelease: (item: ReliabilityItem, candidateId: string, title: string) => void;
  setPursuit: (item: ReliabilityItem, paused: boolean) => void;
};

// The query InkDrop is actually sending, editable in place.
//
// It shows the resolved string rather than rebuilding one from series and
// issue: the whole reason catalogue-formatting failures went unnoticed for so
// long is that nothing anywhere displayed what was being asked for.
function SearchQueryEditor({
  item,
  evidence,
  onSaved,
}: {
  item: ReliabilityItem;
  evidence: ReliabilityEvidence;
  onSaved: (next: ReliabilityEvidence) => void;
}) {
  const current = evidence.search_query || "";
  const [draft, setDraft] = useState(current);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isOperator = evidence.search_query_source === "operator";
  const dirty = draft.trim() !== current.trim();

  async function save(next: string) {
    setSaving(true);
    setError(null);
    try {
      await request<{ ok: boolean }>("/api/inkdrop-state/reliability/search-query", {
        method: "POST",
        body: { wanted_id: item.wanted_id, query: next },
      });
      const fresh = await request<{ ok: boolean; evidence: ReliabilityEvidence }>(
        `/api/inkdrop-state/reliability/item?wanted_id=${encodeURIComponent(item.wanted_id)}`,
      );
      onSaved(fresh.evidence);
      setDraft(fresh.evidence.search_query || "");
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not save that search query.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="reliability-query-editor">
      <label htmlFor={`q-${item.wanted_id}`}>
        {isOperator ? "Search query (yours)" : "Search query InkDrop is sending"}
      </label>
      <div className="reliability-query-row">
        <input
          id={`q-${item.wanted_id}`}
          type="text"
          value={draft}
          spellCheck={false}
          disabled={saving}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button type="button" disabled={saving || !dirty} onClick={() => void save(draft)}>
          {saving ? "Saving…" : "Save"}
        </button>
        {isOperator && (
          <button type="button" disabled={saving} onClick={() => void save("")} title="Go back to the query InkDrop generates">
            Reset
          </button>
        )}
      </div>
      {isOperator && evidence.generated_search_query && (
        <p className="mini">InkDrop would otherwise send “{evidence.generated_search_query}”.</p>
      )}
      {error && <p className="mini reliability-query-error">{error}</p>}
    </div>
  );
}

function EvidencePanel({
  item,
  actions,
}: {
  item: ReliabilityItem;
  actions: ItemCardActions;
}) {
  const [evidence, setEvidence] = useState<ReliabilityEvidence | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    request<{ ok: boolean; evidence: ReliabilityEvidence }>(
      `/api/inkdrop-state/reliability/item?wanted_id=${encodeURIComponent(item.wanted_id)}`,
    )
      .then((data) => {
        if (!live) return;
        setEvidence(data.evidence);
      })
      .catch((cause) => {
        if (!live) return;
        setError(cause instanceof InkDropApiError ? cause.message : "Could not load the evidence for this item.");
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [item.wanted_id]);

  if (loading) return <div className="reliability-evidence loading">Reading this item's history…</div>;
  if (error) return <div className="reliability-evidence error">{error}</div>;
  if (!evidence?.ok) return <div className="reliability-evidence error">No evidence is on record for this item.</div>;

  const attempts = evidence.attempts || [];
  const rejected = evidence.rejected_releases || [];

  return (
    <div className="reliability-evidence">
      <SearchQueryEditor item={item} evidence={evidence} onSaved={setEvidence} />
      <dl className="reliability-evidence-facts">
        {evidence.retry_after_iso && (
          <>
            <dt>Next try</dt>
            <dd>{new Date(evidence.retry_after_iso).toLocaleString()}</dd>
          </>
        )}
        {evidence.wanted_created_at && (
          <>
            <dt>Wanted since</dt>
            <dd>{relativeAge(evidence.wanted_created_at)}</dd>
          </>
        )}
        {evidence.wanted_reason && (
          <>
            <dt>Added by</dt>
            <dd>{evidence.wanted_reason}</dd>
          </>
        )}
      </dl>

      <h4>What the searches did</h4>
      {attempts.length === 0 ? (
        <p className="mini">No real provider search has run for this item yet.</p>
      ) : (
        <div className="reliability-evidence-scroll">
          <table className="reliability-evidence-table">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">When</th>
                <th scope="col">What happened</th>
                <th scope="col">Found</th>
              </tr>
            </thead>
            <tbody>
              {attempts.map((attempt) => (
                <tr key={attempt.id}>
                  <td>{attempt.source || "—"}</td>
                  {/* The attempt's own completion time. Never the queue row's
                      updated_at, which bookkeeping rewrites constantly. */}
                  <td>{attempt.at_label || "—"}</td>
                  <td>
                    {attempt.outcome || attempt.status || "—"}
                    {attempt.failure_sentence && (
                      <span className="mini"> — {attempt.failure_sentence}</span>
                    )}
                    {attempt.query && <span className="mini reliability-evidence-query">“{attempt.query}”</span>}
                  </td>
                  <td>
                    {typeof attempt.candidate_count === "number" ? attempt.candidate_count : "—"}
                    {typeof attempt.rejected_candidate_count === "number" && attempt.rejected_candidate_count > 0
                      ? ` (${attempt.rejected_candidate_count} turned down)`
                      : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {evidence.attempts_truncated && (
        <p className="mini">Showing the most recent {evidence.attempt_limit} searches.</p>
      )}

      {rejected.length > 0 && (
        <>
          <h4>Releases InkDrop turned down for this series</h4>
          <div className="reliability-evidence-scroll">
            <table className="reliability-evidence-table">
              <thead>
                <tr>
                  <th scope="col">Release</th>
                  <th scope="col">Why</th>
                  <th scope="col">Seen</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {rejected.map((release) => (
                  <tr key={release.id}>
                    <td>
                      {release.title || "Untitled release"}
                      {release.source && <span className="mini"> · {release.source}</span>}
                    </td>
                    <td>{release.reason_sentence || release.reason || "—"}</td>
                    <td>
                      {release.last_seen_label || "—"}
                      {release.failure_count && release.failure_count > 1 ? ` · ${release.failure_count}×` : ""}
                    </td>
                    <td>
                      <button
                        type="button"
                        onClick={() => actions.allowRelease(item, release.id, release.title || "this release")}
                        title="Let InkDrop try this release again for this item"
                      >
                        Allow it anyway
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function ItemCard({ item, actions }: { item: ReliabilityItem; actions: ItemCardActions }) {
  const [expanded, setExpanded] = useState(false);
  const tone = bucketTone(item);
  const pending = actions.pendingIds.has(item.wanted_id);
  const done = actions.doneIds.get(item.wanted_id);
  const busy = pending || Boolean(done);

  const canSearchNow = canSearchNowForItem(item);
  const canTryAnother = Boolean(item.queue_id) && CAN_TRY_ANOTHER_SOURCE.has(item.bucket);
  const canReopen = Boolean(item.queue_id) && CAN_REOPEN_IMPORT.has(item.bucket);
  const canClear = Boolean(item.queue_id) && CAN_CLEAR_ROW.has(item.bucket);
  const canBlock = hasBlockableRelease(item);
  const paused = Boolean(item.wanted_pursuit_paused);
  const canPause = !paused && CAN_PAUSE.has(item.bucket);
  const hasActions =
    paused || canSearchNow || canTryAnother || canReopen || canClear || canBlock || canPause;

  // The honest freshness line. Which stamp is meaningful depends on the bucket:
  // a stuck import is dated by when its download finished, everything else by
  // when a real provider last searched. Neither ever falls back to the queue
  // row's updated_at.
  const stalled = item.bucket === "stuck_import" ? item.first_transfer_completed_at : null;
  const searched = item.last_provider_attempt_at;

  return (
    <article className={`reliability-item-card tone-${tone || "neutral"}`} data-arr-row-id={item.wanted_id}>
      <header className="reliability-item-header">
        <div className="reliability-item-title-block">
          <strong className="reliability-item-title">{itemTitle(item)}</strong>
          {item.issue_title && <span className="mini">{item.issue_title}</span>}
        </div>
        <span className={`core-state reliability-bucket-badge ${tone}`}>{item.bucket_label}</span>
      </header>
      <StageTracker item={item} />
      <p className="reliability-item-reason">{item.reason}</p>
      <div className="reliability-item-meta">
        {item.queue_state && <span>queue: {item.queue_state}</span>}
        {/* real_attempt_count, never attempt_count: the latter is the raw
            source_attempts ledger, 5.3x high at the median and 330x at the
            tail, and printing it here contradicted the reason sentence
            directly above -- which quotes the de-noised number. The ledger
            figure stays reachable as a labelled tooltip, never as "attempts". */}
        {typeof item.real_attempt_count === "number" && (
          <span
            title={
              typeof item.ledger_row_count === "number"
                ? `${item.ledger_row_count} ledger rows (includes non-provider bookkeeping)`
                : undefined
            }
          >
            {item.real_attempt_count} {item.real_attempt_count === 1 ? "attempt" : "attempts"}
          </span>
        )}
        {stalled ? (
          <span>download finished {relativeAge(stalled)}</span>
        ) : searched ? (
          <span>last searched {relativeAge(searched)}</span>
        ) : (
          <span>never searched</span>
        )}
        <button
          type="button"
          className="reliability-item-expand"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Hide the evidence" : "Why?"}
        </button>
      </div>
      {expanded && <EvidencePanel item={item} actions={actions} />}
      {hasActions && (
        <div className="reliability-item-actions">
          {paused && (
            <button
              type="button"
              disabled={busy}
              onClick={() => actions.setPursuit(item, false)}
              title="Start searching for this again"
            >
              {pending ? "Working…" : done || "Search for this again"}
            </button>
          )}
          {canSearchNow && !paused && (
            <button
              type="button"
              disabled={busy}
              onClick={() => actions.runSearchNow(item)}
              title="Search for this item now instead of waiting for its turn"
            >
              {pending ? "Working…" : done || "Search now"}
            </button>
          )}
          {canPause && (
            <button
              type="button"
              disabled={busy}
              onClick={() => actions.setPursuit(item, true)}
              title="Stop searching for this. It stays on your Wanted list and stays counted"
            >
              {pending ? "Working…" : done || "Stop looking for this"}
            </button>
          )}
          {canTryAnother && (
            <button
              type="button"
              disabled={busy}
              onClick={() => actions.runRetryNewSource(item)}
              title="Retry, skipping whichever source(s) most recently failed for this item"
            >
              {pending ? "Working…" : done || "Try another source"}
            </button>
          )}
          {canReopen && (
            <button
              type="button"
              disabled={busy}
              onClick={() => actions.runReopenImport(item)}
              title="Re-check this import. Usually the file was never on disk, so another source is the better fix"
            >
              {pending ? "Working…" : done || "Reopen import"}
            </button>
          )}
          {canClear && (
            <button
              type="button"
              disabled={busy}
              onClick={() => actions.runClearRow(item)}
              title="Remove this leftover row. The live row for this item stays"
            >
              {pending ? "Working…" : done || "Clear this row"}
            </button>
          )}
          {canBlock && (
            <button
              type="button"
              className="reliability-item-block-btn"
              disabled={busy}
              onClick={() => actions.blockCandidate(item)}
              title="Permanently reject this release so InkDrop stops offering it for this issue"
            >
              {pending ? "Working…" : done || "Block this release"}
            </button>
          )}
        </div>
      )}
    </article>
  );
}

export function ReliabilityView({ payload }: { payload: ReliabilityViewPayload }) {
  const [items, setItems] = useState<ReliabilityItem[]>(payload.rows || []);
  const [offset, setOffset] = useState(payload.offset || 0);
  const [totalCount, setTotalCount] = useState(payload.total_count || 0);
  const [bucketFilter, setBucketFilter] = useState(payload.bucket_filter || "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState(payload.summary);
  // The health strip is fetched on its own because it needs a real provider
  // timestamp for every row in the backlog -- ~1.2s however it is queried, and
  // the list must not wait for it. It fills in a beat after the list renders.
  const [signals, setSignals] = useState<ReliabilitySignal[]>([]);
  const bucketFilterRef = useRef(bucketFilter);
  bucketFilterRef.current = bucketFilter;
  const offsetRef = useRef(offset);
  offsetRef.current = offset;
  const { pendingIds, doneIds, actionError, clearActionError, runRowAction } = useRowActions(() =>
    loadPage(offsetRef.current),
  );

  // A fresh payload arrives whenever the shell re-fetches page one on our
  // behalf (section re-entry, nav re-click). If the user had a bucket filter
  // active, the shell's unfiltered page one would silently discard it --
  // same pattern Blocklist.tsx uses for its own compound filter.
  useEffect(() => {
    if (!bucketFilterRef.current) {
      setItems(payload.rows || []);
      setOffset(payload.offset || 0);
      setTotalCount(payload.total_count || 0);
      setSummary(payload.summary);
      setError(null);
    } else {
      void loadPage(0, bucketFilterRef.current);
    }
    clearActionError();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload]);

  useEffect(() => {
    let live = true;
    request<{ ok: boolean; signals: ReliabilitySignalsPayload }>("/api/inkdrop-state/reliability/signals")
      .then((data) => {
        if (live) setSignals(data.signals?.signals || []);
      })
      // A slow or failed strip must never take the list with it: the strip is
      // context, the list is the page.
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  async function loadPage(nextOffset: number, filter?: string) {
    const activeFilter = filter ?? bucketFilterRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await request<{ ok: boolean; view: ReliabilityViewPayload }>(buildEndpoint(nextOffset, activeFilter));
      const view = data.view;
      setItems(view.rows || []);
      setOffset(view.offset ?? nextOffset);
      setTotalCount(view.total_count || 0);
      if (view.summary) setSummary(view.summary);
    } catch (cause) {
      setError(cause instanceof InkDropApiError ? cause.message : "Could not load the Reliability view.");
    } finally {
      setLoading(false);
    }
  }

  function selectBucket(bucket: string) {
    const next = bucket === bucketFilter ? "" : bucket;
    setBucketFilter(next);
    void loadPage(0, next);
  }

  // Same endpoints/semantics as Queue.tsx's and ManualReview.tsx's recovery
  // actions (PR #504) -- tracked by wanted_id since that's this view's stable
  // per-card key, with queue_id as the actual mutation target.
  function runSearchNow(item: ReliabilityItem) {
    void runRowAction(item.wanted_id, itemTitle(item), "Searching", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/wanted/run", {
        method: "POST",
        body: { id: item.wanted_id },
      });
      if (!data.ok) throw new InkDropApiError("Could not start a search for this item.", { status: 200, code: "reliability_search_now_failed" });
    });
  }

  function runRetryNewSource(item: ReliabilityItem) {
    const queueId = item.queue_id || "";
    void runRowAction(item.wanted_id, itemTitle(item), "Queued", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/queue/retry-new-source", {
        method: "POST",
        body: { id: queueId },
      });
      if (!data.ok) {
        const reason = data.result?.reason;
        throw new InkDropApiError(
          reason === "no_alternate_source_available"
            ? "No alternate source is configured for this item -- every source already failed."
            : "Could not retry with another source.",
          { status: 200, code: "reliability_retry_new_source_failed" },
        );
      }
    });
  }

  function runReopenImport(item: ReliabilityItem) {
    const queueId = item.queue_id || "";
    void runRowAction(item.wanted_id, itemTitle(item), "Reopened", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/queue/reopen-import", {
        method: "POST",
        body: { id: queueId },
      });
      if (!data.ok) throw new InkDropApiError("Could not reopen this import.", { status: 200, code: "reliability_reopen_import_failed" });
    });
  }

  function runClearRow(item: ReliabilityItem) {
    const queueId = item.queue_id || "";
    if (!window.confirm(`Clear this leftover row for ${itemTitle(item)}? The live row for this item stays.`)) return;
    void runRowAction(item.wanted_id, itemTitle(item), "Cleared", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/queue/remove", {
        method: "POST",
        body: { id: queueId },
      });
      if (!data.ok) throw new InkDropApiError("Could not clear this row.", { status: 200, code: "reliability_clear_row_failed" });
    });
  }

  function allowRelease(item: ReliabilityItem, candidateId: string, title: string) {
    if (!window.confirm(`Let InkDrop try “${title}” again? It was rejected before.`)) return;
    void runRowAction(item.wanted_id, itemTitle(item), "Allowed", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/source-memory/allow", {
        method: "POST",
        body: { id: candidateId, decision: "allow_and_retry" },
      });
      if (!data.ok) throw new InkDropApiError("Could not allow this release.", { status: 200, code: "reliability_allow_release_failed" });
    });
  }

  function setPursuit(item: ReliabilityItem, paused: boolean) {
    if (
      paused &&
      !window.confirm(
        `Stop looking for ${itemTitle(item)}?

InkDrop will stop searching and stop spending time on it. ` +
          `It stays on your Wanted list and stays counted here, and you can start again whenever you like.`,
      )
    ) {
      return;
    }
    void runRowAction(item.wanted_id, itemTitle(item), paused ? "Stopped" : "Searching", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/reliability/pursuit", {
        method: "POST",
        body: { wanted_id: item.wanted_id, paused },
      });
      if (!data.ok) {
        throw new InkDropApiError(
          paused ? "Could not stop searching for this item." : "Could not resume searching for this item.",
          { status: 200, code: "reliability_pursuit_failed" },
        );
      }
    });
  }

  function blockCandidate(item: ReliabilityItem) {
    const queueId = item.queue_id || "";
    if (!window.confirm(`Permanently block this release for ${itemTitle(item)}? InkDrop will never offer it again for this issue.`)) return;
    void runRowAction(item.wanted_id, itemTitle(item), "Blocked", async () => {
      const data = await request<{ ok: boolean; result?: { reason?: string } }>("/api/inkdrop-state/source-memory/block", {
        method: "POST",
        body: { id: queueId, source_attempt_id: item.last_attempt_id, retry: true },
      });
      if (!data.ok) {
        const reason = data.result?.reason;
        throw new InkDropApiError(
          reason === "no_candidate_to_block" ? "No recent candidate is on record for this row to block." : "Could not block this release.",
          { status: 200, code: "reliability_block_candidate_failed" },
        );
      }
    });
  }

  const itemCardActions: ItemCardActions = {
    pendingIds,
    doneIds,
    runSearchNow,
    runRetryNewSource,
    runReopenImport,
    runClearRow,
    blockCandidate,
    allowRelease,
    setPursuit,
  };

  const buckets = summary?.buckets || [];
  const selected = buckets.find((bucket) => bucket.key === bucketFilter);
  const bandCounts = new Map((summary?.bands || []).map((band) => [band.key, band]));

  const pageStart = totalCount === 0 ? 0 : offset + 1;
  const pageEnd = offset + items.length;
  const pageCount = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div className="inkdrop-react-reliability">
      {(error || actionError) && (
        <div className="inkdrop-react-error-banner" role="alert">
          {error || actionError}
        </div>
      )}
      {signals.length > 0 && (
        <div className="reliability-signal-strip">
          {signals.map((signal) => (
            <div key={signal.key} className={`reliability-signal tone-${signal.tone}`}>
              <span className="reliability-signal-value">{signal.value}</span>
              <span className="reliability-signal-label">{signal.label}</span>
              {signal.detail && <span className="reliability-signal-detail">{signal.detail}</span>}
            </div>
          ))}
        </div>
      )}
      <div className="reliability-band-row">
        {BAND_ORDER.map((band) => {
          const inBand = buckets.filter((bucket) => bucket.band === band);
          if (inBand.length === 0) return null;
          const total = bandCounts.get(band)?.count ?? 0;
          return (
            <section key={band} className={`reliability-band tone-${BAND_TONE[band]}`}>
              <h3 className="reliability-band-header">
                {bandCounts.get(band)?.label || band}
                <span className="reliability-band-count">{total}</span>
              </h3>
              <div className="reliability-rollup-row">
                {inBand.map((bucket) => (
                  <button
                    type="button"
                    key={bucket.key}
                    className={`reliability-rollup-card tone-${
                      NEUTRAL_BUCKETS.has(bucket.key) ? "neutral" : BAND_TONE[band]
                    } ${bucketFilter === bucket.key ? "active" : ""}`}
                    onClick={() => selectBucket(bucket.key)}
                    disabled={loading}
                    aria-pressed={bucketFilter === bucket.key}
                  >
                    <span className="reliability-rollup-count">{bucket.count}</span>
                    <span className="reliability-rollup-label">{bucket.label}</span>
                  </button>
                ))}
              </div>
            </section>
          );
        })}
      </div>
      {selected?.explainer ? (
        <p className="reliability-bucket-explainer">{selected.explainer}</p>
      ) : (
        <div className="reliability-info-banner">
          Every Wanted and in-progress item, sorted by what is actually happening to it. Pick a group to see
          what it means and what to do; open a row to see the searches, the releases InkDrop turned down, and
          why.
        </div>
      )}
      <div className="reliability-item-list">
        {items.map((item) => (
          <ItemCard key={item.wanted_id} item={item} actions={itemCardActions} />
        ))}
        {items.length === 0 && !loading && (
          <div className="reliability-empty-state">
            {bucketFilter ? "Nothing is in this group right now." : "No Wanted or in-progress items to show."}
          </div>
        )}
      </div>
      <div className="inkdrop-react-pager reliability-pager">
        <span>{totalCount > 0 ? `Showing ${pageStart}-${pageEnd} of ${totalCount} items` : "0 items"}</span>
        <div className="reliability-pager-pages">
          <button
            type="button"
            disabled={loading || currentPage <= 1}
            onClick={() => void loadPage(Math.max(0, offset - PAGE_SIZE))}
            aria-label="Previous page"
          >
            &lsaquo;
          </button>
          {pageNumbers(currentPage, pageCount).map((page, index) =>
            page === "gap" ? (
              <span key={`gap-${index}`} className="reliability-pager-gap">
                …
              </span>
            ) : (
              <button
                key={page}
                type="button"
                className={page === currentPage ? "active" : ""}
                disabled={loading || page === currentPage}
                onClick={() => void loadPage((page - 1) * PAGE_SIZE)}
              >
                {page}
              </button>
            ),
          )}
          <button
            type="button"
            disabled={loading || currentPage >= pageCount}
            onClick={() => void loadPage(offset + PAGE_SIZE)}
            aria-label="Next page"
          >
            &rsaquo;
          </button>
        </div>
      </div>
    </div>
  );
}
