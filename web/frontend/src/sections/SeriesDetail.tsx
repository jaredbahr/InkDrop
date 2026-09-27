import { useEffect, useMemo, useState } from "react";
import { request, InkDropApiError } from "../api";
import type { IssueRow, LinkedEntities, SeriesDetailIssuesPayload, SeriesDetailRow } from "./seriesDetailTypes";
import {
  compactNumber,
  coreStateLabel,
  sourceBucketLabel,
  rowLinkedEntities,
  seriesDetailIssueCounts,
  seriesDetailIssueDetailText,
  seriesDetailIssueDisplayTitle,
  seriesDetailIssueGroupEntries,
  seriesDetailIssueGroupLabel,
  seriesDetailIssueIsSearchable,
  seriesDetailIssueNeedsSearchPath,
  seriesDetailIssueSortedRows,
  seriesDetailIssueStatus,
  seriesDetailMetadata,
  seriesDetailDateLabel,
  seriesOperatorNextStep,
  seriesReaderVisibilityModel,
} from "./seriesDetailTypes";

// The hero (overview, stat grid, reader-visibility note) and the issues list
// including its per-issue-row actions. The vanilla shell still owns the
// page-level toolbar and commandbar (Refresh/Search/Monitor/Move
// Library/Remove etc) above this island, and the "Back to Series"
// navigation -- see seriesDetailTypes.ts's file comment for why those stay
// vanilla rather than being ported here.

// The InkDropSeriesNav declaration used to live here, and three other files
// reached the same bridge through `window as unknown as { ... }` with
// different shapes -- openDetail was required here and optional in
// History.tsx, over three different row types. It is declared once now, in
// src/shellBridge.ts, which that file explains at length.

function Fact({ label, value, href }: { label: string; value: string; href?: string }) {
  if (!value) return null;
  return (
    <span className="series-detail-fact">
      <strong>{label}</strong>
      {href ? (
        <a href={href} target="_blank" rel="noopener noreferrer">
          {value}
        </a>
      ) : (
        value
      )}
    </span>
  );
}

function Chip({ label, tone, title }: { label: string; tone?: string; title?: string }) {
  if (!label) return null;
  return (
    <span className={tone || undefined} title={title}>
      {label}
    </span>
  );
}

function HeroArt({ row }: { row: SeriesDetailRow }) {
  const title = row.title || "Unknown series";
  // The vanilla page's createSeriesArt() carries deferred-loading and cache
  // -lookup logic that only matters for a grid of many cards; a single
  // detail page just needs the one image the payload already resolved, the
  // same simplification the Series list island already made for its cards.
  return (
    <div className="series-art detail" title={row.image ? `${title} cover` : `${title} · cover art missing`}>
      {row.image ? (
        <img src={row.image} alt="" loading="eager" decoding="async" />
      ) : (
        <span className="series-art-fallback">
          <strong>{title.slice(0, 2).toUpperCase()}</strong>
        </span>
      )}
    </div>
  );
}

function HeroTitle({ row }: { row: SeriesDetailRow }) {
  const title = row.title || "Unknown series";
  const managed = Boolean(row.library_path);
  const metadataFact = row.metadata_provider && row.metadata_id ? `${sourceBucketLabel(row.metadata_provider)} ${row.metadata_id}` : "";
  return (
    <div className="series-detail-title">
      <h3>{title}</h3>
      <div className="series-detail-meta">
        <Chip label={row.publisher || sourceBucketLabel(row.metadata_provider || row.source || "")} />
        <Chip label={row.year ? String(row.year) : ""} />
        <Chip label={row.media_type ? coreStateLabel(row.media_type) : ""} />
        <Chip label={row.monitored === false ? "Unmonitored" : row.monitored ? "Monitored" : ""} tone={row.monitored === false ? "" : "good"} />
        <Chip label={row.auto_grab ? "Auto grab" : ""} tone="good" />
        <Chip label={row.provenance_label || row.ownership || ""} />
      </div>
      <div className="series-detail-path">
        <Chip label={managed ? "Library managed" : ""} tone="good" title="Managed library evidence exists. Exact filesystem paths are hidden from the normal public UI." />
        <Chip label={row.reader_visibility_lag_count ? "Reader sync pending" : "Reader sync async"} tone={row.reader_visibility_lag_count ? "warn" : ""} />
        <Chip label={metadataFact} />
      </div>
    </div>
  );
}

function Overview({ row }: { row: SeriesDetailRow }) {
  const metadata = useMemo(() => seriesDetailMetadata(row), [row]);
  const adapterBacked = row.ownership === "adapter";
  const libraryPath = row.library_path;
  const metadataSourceLabel = sourceBucketLabel(row.metadata_provider || "comicvine") || "ComicVine";
  return (
    <div className="series-detail-overview" data-operator-control="series-detail-overview">
      <p className={`series-detail-description ${metadata.description ? "" : "missing"}`.trim()} title={metadata.description || undefined}>
        {metadata.description || "No series description is saved yet."}
      </p>
      <div className="series-detail-facts">
        <Fact label="Released" value={metadata.startYear ? seriesDetailDateLabel(metadata.startYear) : ""} />
        <Fact label="Issues" value={metadata.issueCount ? compactNumber(metadata.issueCount) : ""} />
        <Fact label="Added" value={metadata.added} />
        <Fact label="Updated" value={metadata.updated} />
        <Fact label={metadataSourceLabel} value={metadata.siteUrl ? "Open" : ""} href={metadata.siteUrl || undefined} />
        <span className={`series-detail-fact ${adapterBacked ? "warn" : "good"}`.trim()}>
          <strong>Tracked by</strong>
          {adapterBacked ? "Legacy adapter" : "InkDrop"}
        </span>
        {libraryPath && (
          <span className="series-detail-fact good">
            <strong>Library</strong>
            Managed
          </span>
        )}
      </div>
    </div>
  );
}

function NextStepPanel({ row }: { row: SeriesDetailRow }) {
  const model = seriesOperatorNextStep(row);
  const clickable = model.opensManualReview;
  const navigate = () => window.InkDropSeriesNav?.openLinked?.("manual_review", row);
  return (
    <div
      className={`series-focus-next ${model.tone || ""} ${clickable ? "clickable" : ""}`.trim()}
      data-series-operator-next={model.label}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      title={clickable ? "Open the Manual Review decision for this series." : undefined}
      onClick={clickable ? navigate : undefined}
      onKeyDown={
        clickable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                navigate();
              }
            }
          : undefined
      }
    >
      <strong>{model.label}</strong>
      <span>{model.detail}</span>
    </div>
  );
}

function ReaderVisibilityNote({ row }: { row: SeriesDetailRow }) {
  const model = useMemo(() => seriesReaderVisibilityModel(row), [row]);
  if (!model) return null;
  return (
    <div className={`series-reader-visibility-note ${model.tone || ""}`.trim()} data-operator-control="series-reader-visibility-note">
      <div className="series-reader-visibility-head">
        <strong>{model.label}</strong>
        <span>
          {model.providerLabel} · {compactNumber(model.total)}
        </span>
      </div>
      <p className="series-reader-visibility-copy">{model.detail}</p>
      {model.next && <span className="series-reader-visibility-next">{model.next}</span>}
    </div>
  );
}

function StatGrid({ row }: { row: SeriesDetailRow }) {
  const stats: Array<{ label: string; value: string; tone: string }> = [
    { label: "Wanted", value: compactNumber(row.wanted_count || 0), tone: Number(row.wanted_count || 0) ? "warn" : "" },
    { label: "Queue", value: compactNumber(row.active_queue_count || 0), tone: Number(row.active_queue_count || 0) ? "warn" : "" },
    { label: "Source wait", value: compactNumber(row.provider_wait_count || 0), tone: Number(row.provider_wait_count || 0) ? "warn" : "" },
    { label: "Needs you", value: compactNumber(row.needs_you_count || 0), tone: Number(row.needs_you_count || 0) ? "bad" : "" },
    { label: "Monitor", value: row.monitored === false ? "Paused" : "On", tone: row.monitored === false ? "" : "good" },
    { label: "Future", value: row.monitor_new === false ? "Off" : "On", tone: row.monitor_new === false ? "" : "good" },
  ];
  return (
    <div className="series-detail-stat-grid series-detail-status-strip">
      {stats.map((stat) => (
        <div key={stat.label} className={`series-detail-stat ${stat.tone}`.trim()}>
          <strong>{stat.value}</strong>
          <span>{stat.label}</span>
        </div>
      ))}
    </div>
  );
}

// Ported from the vanilla page's seriesLibraryActionKey -- drives which icon
// glyph the CSS shows for a given action label.
function seriesLibraryActionKey(label: string): string {
  const key = label.trim().toLowerCase();
  if (key.includes("review")) return "review";
  if (key.includes("queue")) return "queue";
  if (key.includes("wanted")) return "wanted";
  if (key.includes("check")) return "search";
  if (key.includes("search")) return "search";
  return "open";
}

// Mirrors the vanilla page's runInkdropBusyButton: disable the button and
// show a busy label for the duration of its own async action, so a second
// click can't fire the same mutation twice while the first is in flight.
function ActionButton({
  label,
  title,
  tone,
  onClick,
}: {
  label: string;
  title: string;
  tone?: string;
  onClick: () => unknown;
}) {
  const [busy, setBusy] = useState(false);
  const handleClick = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await onClick();
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      type="button"
      title={title}
      aria-label={title ? `${label}: ${title}` : label}
      data-operator-control="series-detail-issue-action"
      data-series-detail-issue-action={label.toLowerCase().replace(/[^a-z0-9]+/g, "_")}
      data-series-detail-issue-action-key={label.toLowerCase().includes("history") ? "history" : seriesLibraryActionKey(label)}
      className={tone || undefined}
      disabled={busy}
      onClick={handleClick}
    >
      <span className="series-detail-issue-action-icon" aria-hidden="true" />
      <span className="series-detail-issue-action-label">{busy ? "Working…" : label}</span>
    </button>
  );
}

function IssueRowView({
  row,
  seriesRow,
  onActionComplete,
}: {
  row: IssueRow;
  seriesRow: SeriesDetailRow;
  onActionComplete: () => void;
}) {
  const status = seriesDetailIssueStatus(row);
  const detail = seriesDetailIssueDetailText(row);
  const title = seriesDetailIssueDisplayTitle(row);
  const issueLabel = row.issue_number ? `#${row.issue_number}` : String(row.normalized_number || "Issue");
  const meta = [row.issue_metadata_provider || row.metadata_provider || "", row.issue_metadata_id ? `ID ${row.issue_metadata_id}` : ""]
    .filter(Boolean)
    .join(" · ");
  const links: LinkedEntities = rowLinkedEntities(row);
  const searchable = seriesDetailIssueIsSearchable(row);
  const needsSearchPath = seriesDetailIssueNeedsSearchPath(row);
  const issueSearchLabel = row.queue_state === "downloading" || row.queue_state === "importing" ? "Check" : "Search";
  const nav = window.InkDropSeriesNav;

  // Mutation actions (Mark Wrong, Monitor toggle) refresh via their own
  // vanilla fallback (loadInkdropSection("issues", ...), which reloads the
  // separate Issues page's data, not this island) -- refetch this panel's
  // own list on top of that once the call resolves.
  async function runAndRefetch(action: (() => Promise<void>) | undefined) {
    if (!action) return;
    await action();
    onActionComplete();
  }

  return (
    <div className="series-detail-issue-row" data-operator-control="series-detail-issue-row" role="row">
      <div className="series-detail-issue-number" role="cell">
        {issueLabel}
      </div>
      <div className="series-detail-issue-main" role="cell">
        <strong title={title}>{title}</strong>
        {meta && <span title={meta}>{meta}</span>}
      </div>
      <div className={`series-detail-issue-state ${status.tone || ""}`.trim()} role="cell" title={status.label}>
        {status.label}
      </div>
      <div className="series-detail-issue-detail" role="cell" title={detail}>
        {detail}
      </div>
      <div className="series-detail-issue-actions" role="cell">
        {(links.issue_id || links.unit_id) && (
          <ActionButton
            label="Manual Search"
            title="Inspect provider results and explicitly grab a safe candidate"
            onClick={() => nav?.openManualSearch?.(row)}
          />
        )}
        {searchable ? (
          <ActionButton
            label={issueSearchLabel}
            title="Run the existing Wanted search for this issue"
            tone="warn"
            onClick={() => nav?.runWantedSearch?.({ id: row.wanted_id, series: row.series, series_id: links.series_id, issue_id: links.issue_id })}
          />
        ) : (
          needsSearchPath &&
          links.series_id && (
            <ActionButton
              label="Search Series"
              title="Queue a series search because this issue has no Wanted row id yet"
              tone="warn"
              onClick={() => nav?.runSeriesSearch?.({ id: links.series_id, title: row.series || row.title || row.series_title })}
            />
          )
        )}
        {row.queue_id && (
          <ActionButton label="Queue" title="Open focused Queue row" tone="warn" onClick={() => nav?.openLinked?.("queue", row)} />
        )}
        {row.wanted_id && (
          <ActionButton
            label="Wanted"
            title="Open focused Wanted row"
            tone={row.wanted_status === "satisfied" ? undefined : "warn"}
            onClick={() => nav?.openLinked?.("wanted", row)}
          />
        )}
        {!row.wanted_id && needsSearchPath && (
          <ActionButton label="Wanted" title="Open Wanted context for this issue" tone="warn" onClick={() => nav?.openLinked?.("wanted", row)} />
        )}
        <ActionButton label="History" title="Open focused download/import lifecycle for this issue" onClick={() => nav?.openLinked?.("history", row)} />
        {row.latest_import?.id && row.latest_import?.verified && (
          <ActionButton
            label="Mark Wrong"
            title="This file is the wrong match -- quarantine it, block this exact release, and search again"
            tone="bad"
            onClick={() => runAndRefetch(() => nav?.markImportWrong?.(row, seriesRow) ?? Promise.resolve())}
          />
        )}
        {links.issue_id &&
          (row.monitored === false ? (
            <ActionButton
              label="Monitor"
              title="Resume searching for this issue"
              onClick={() => runAndRefetch(() => nav?.setIssueMonitored?.(row, true) ?? Promise.resolve())}
            />
          ) : (
            <ActionButton
              label="Not Monitored"
              title="Stop searching for this issue -- use when you already own it. The rest of the series keeps monitoring normally."
              onClick={() => runAndRefetch(() => nav?.setIssueMonitored?.(row, false) ?? Promise.resolve())}
            />
          ))}
      </div>
    </div>
  );
}

function IssueGroup({
  groupKey,
  rows,
  seriesRow,
  onActionComplete,
}: {
  groupKey: string;
  rows: IssueRow[];
  seriesRow: SeriesDetailRow;
  onActionComplete: () => void;
}) {
  const counts = seriesDetailIssueCounts(rows);
  const sorted = seriesDetailIssueSortedRows(rows);
  return (
    <section className="series-detail-issue-group" data-operator-control="series-detail-issue-group" data-issue-group={groupKey} aria-label={seriesDetailIssueGroupLabel(groupKey)}>
      <div className="series-detail-issue-group-head">
        <div className="series-detail-issue-group-title">
          <strong>{seriesDetailIssueGroupLabel(groupKey)}</strong>
          <span>
            {compactNumber(rows.length)} issue{rows.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="series-detail-issue-group-stats">
          <span className={`series-detail-issue-group-stat ${counts.active ? "warn" : ""}`.trim()}>{compactNumber(counts.active)} active</span>
          <span className={`series-detail-issue-group-stat ${counts.wanted ? "warn" : ""}`.trim()}>{compactNumber(counts.wanted)} wanted</span>
          <span className={`series-detail-issue-group-stat ${counts.verified ? "good" : ""}`.trim()}>{compactNumber(counts.verified)} verified</span>
          {counts.attention > 0 && <span className="series-detail-issue-group-stat bad">{compactNumber(counts.attention)} attention</span>}
        </div>
      </div>
      <div className="series-detail-issue-table" data-operator-control="series-detail-issue-table" role="table" aria-label={`${seriesDetailIssueGroupLabel(groupKey)} issue rows`}>
        <div className="series-detail-issue-row head" role="row">
          {["Issue", "Title", "Status", "Activity", "Actions"].map((label) => (
            <div key={label} role="columnheader">
              {label}
            </div>
          ))}
        </div>
        {sorted.map((issue, index) => (
          <IssueRowView
            key={String(issue.issue_number ?? issue.normalized_number ?? index)}
            row={issue}
            seriesRow={seriesRow}
            onActionComplete={onActionComplete}
          />
        ))}
      </div>
    </section>
  );
}

type IssuesState =
  | { status: "loading" }
  | { status: "slow" }
  | { status: "error"; message: string }
  | { status: "empty" }
  | { status: "ready"; rows: IssueRow[]; loaded: number; total: number };

function IssuesPanel({ seriesId, seriesRow }: { seriesId: string; seriesRow: SeriesDetailRow }) {
  const [state, setState] = useState<IssuesState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!seriesId) {
      setState({ status: "error", message: "Issue rows need a series identity before they can load." });
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    const slowTimer = window.setTimeout(() => {
      if (!cancelled) setState((prev) => (prev.status === "loading" ? { status: "slow" } : prev));
    }, 1800);
    const params = new URLSearchParams({
      limit: "80",
      summary: "compact",
      rows: "compact",
      issue_filter: "all",
      focus_series_id: seriesId,
    });
    request<{ view?: SeriesDetailIssuesPayload } & SeriesDetailIssuesPayload>(`/api/inkdrop-state/issues?${params.toString()}`)
      .then((data) => {
        if (cancelled) return;
        window.clearTimeout(slowTimer);
        const payload = data.view || data;
        const rows = Array.isArray(payload.rows) ? payload.rows.filter(Boolean) : [];
        const loaded = Number(payload.loaded_count ?? rows.length);
        const total = Number(payload.total_count ?? payload.count ?? loaded);
        setState(rows.length ? { status: "ready", rows, loaded, total } : { status: "empty" });
      })
      .catch((err) => {
        if (cancelled) return;
        window.clearTimeout(slowTimer);
        const message = err instanceof InkDropApiError ? err.message : "Issue rows are taking longer than usual.";
        setState({ status: "error", message });
      });
    return () => {
      cancelled = true;
      window.clearTimeout(slowTimer);
    };
  }, [seriesId, attempt]);

  const retry = () => setAttempt((n) => n + 1);

  return (
    <section className="series-detail-issues" data-operator-control="series-detail-issue-panel" data-loading={state.status === "loading" || state.status === "slow" ? state.status : "idle"}>
      <div className="series-detail-issues-head">
        <div className="series-detail-issues-title">
          <strong>Issues</strong>
          <span data-series-detail-issues-meta="true">
            {state.status === "ready"
              ? [
                  `${compactNumber(state.loaded)} shown${state.total > state.loaded ? ` of ${compactNumber(state.total)}` : ""}`,
                  (() => {
                    const counts = seriesDetailIssueCounts(state.rows);
                    return [
                      `${compactNumber(counts.active)} active`,
                      `${compactNumber(counts.wanted)} wanted`,
                      `${compactNumber(counts.verified)} verified`,
                      counts.attention ? `${compactNumber(counts.attention)} attention` : "",
                    ]
                      .filter(Boolean)
                      .join(" · ");
                  })(),
                ]
                  .filter(Boolean)
                  .join(" · ")
              : "Issue rows hydrate separately so this Series page stays usable."}
          </span>
        </div>
      </div>
      <div data-series-detail-issues-body="true">
        {state.status === "loading" && (
          <div className="series-detail-issues-empty">Fetching issue rows in the background. Series actions above are ready.</div>
        )}
        {state.status === "slow" && (
          <div className="series-detail-issues-empty slow">
            Issue rows are still loading. The Series page is ready; use Queue, Wanted, History, or retry this panel if it stalls.
            <button type="button" onClick={retry}>
              Retry Issues
            </button>
          </div>
        )}
        {state.status === "error" && (
          <div className="series-detail-issues-empty slow">
            {state.message} The Series page is still usable; retry this panel when the backend catches up.
            <button type="button" onClick={retry}>
              Retry Issues
            </button>
          </div>
        )}
        {state.status === "empty" && (
          <div className="series-detail-issues-empty">No issues returned for this series yet. Open Issues for the full focused view.</div>
        )}
        {state.status === "ready" && (
          <div className="series-detail-issue-groups" data-operator-control="series-detail-issue-groups">
            {seriesDetailIssueGroupEntries(seriesDetailIssueSortedRows(state.rows)).map(([key, rows]) => (
              <IssueGroup key={key} groupKey={key} rows={rows} seriesRow={seriesRow} onActionComplete={retry} />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

export interface SeriesDetailPayload {
  row: SeriesDetailRow & { id?: string };
}

export function SeriesDetail({ payload }: { payload: SeriesDetailPayload }) {
  const row = payload?.row || {};
  const seriesId = String(row.id || "");
  // One wrapper div for the mount root -- same trade-off every migrated
  // section already makes (Series.tsx's .inkdrop-react-series, etc). Its two
  // children use the vanilla page's own class names (series-detail-hero,
  // series-detail-issues) so the existing CSS still targets them; only the
  // backdrop-image treatment on the hero (a decorative blur, not content) is
  // dropped as a known simplification, see HeroArt's comment.
  return (
    <div className="series-detail-react-root">
      <div className="series-detail-hero">
        <HeroArt row={row} />
        <div className="series-detail-main">
          <HeroTitle row={row} />
          <Overview row={row} />
          <NextStepPanel row={row} />
          <ReaderVisibilityNote row={row} />
          <StatGrid row={row} />
        </div>
      </div>
      <IssuesPanel key={seriesId} seriesId={seriesId} seriesRow={row} />
    </div>
  );
}
