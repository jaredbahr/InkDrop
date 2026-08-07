import { useEffect, useMemo, useRef, useState } from "react";
import { TableVirtuoso, VirtuosoGrid } from "react-virtuoso";
import type { TableVirtuosoHandle, VirtuosoGridHandle } from "react-virtuoso";
import type {
  SeriesRow,
  SeriesUiState,
  SeriesViewPayload,
} from "./seriesTypes";
import {
  seriesMetaLine,
  seriesRibbonPublisher,
  seriesStatus,
} from "./seriesTypes";

// The shell owns navigation, the Series toolbar (Update / RSS Sync / View /
// Sort / Filter) and the search box; this island owns everything inside the
// rows box: the poster grid, the table, and the alphabet rail.
//
// Card anatomy deliberately differs from the vanilla card it replaces:
//   - the corner ribbon carries the PUBLISHER, not the status, and is omitted
//     entirely when there is no usable publisher
//   - status appears exactly once, as a pill under the title (the vanilla card
//     rendered it three times: corner ribbon, tinted bar, disclosure word)
//   - the meta line is "publisher · year"; monitored state lives in the pill
//
// Both views (table and poster grid) are virtualized (react-window-style,
// via react-virtuoso): the full row list stays in memory so the alphabet
// rail can jump anywhere instantly, but only the rows/cards actually on
// screen exist as real DOM nodes. See
// INKDROP_REACT_FRONTEND_PERFORMANCE_AUDIT_20260807_0000.md for the
// measured cost this replaces (385 unvirtualized subtrees mounted on every
// visit, growing with the library).

function rowKey(row: SeriesRow, index: number): string {
  return String(row.id || `${row.title || "series"}:${index}`);
}

function seriesInitial(row: SeriesRow): string {
  const source = String(row.sort_title || row.title || "").trim();
  if (!source) return "#";
  const first = source[0].toUpperCase();
  return first >= "A" && first <= "Z" ? first : "#";
}

function SeriesCard({ row, onOpen }: { row: SeriesRow; onOpen: (row: SeriesRow) => void }) {
  const status = seriesStatus(row);
  const meta = seriesMetaLine(row);
  const title = row.title || "Unknown series";
  return (
    <article className="inkdrop-series-card" data-series-id={row.id || ""}>
      <div className="inkdrop-series-cover">
        {row.image ? (
          <img src={row.image} alt="" loading="lazy" decoding="async" />
        ) : (
          <span className="inkdrop-series-cover-fallback" aria-hidden="true">
            {title.slice(0, 2).toUpperCase()}
          </span>
        )}
      </div>
      <div className="inkdrop-series-card-body">
        <strong className="inkdrop-series-card-title" title={title}>
          {title}
        </strong>
        <div className="inkdrop-series-card-meta">{meta}</div>
        <span
          className={`inkdrop-series-pill ${status.tone}`.trim()}
          title={status.detail || undefined}
        >
          {status.label}
        </span>
      </div>
      <button
        type="button"
        className="inkdrop-series-card-open"
        onClick={() => onOpen(row)}
        title={`Open ${title}`}
        aria-label={`Open ${title}`}
      />
    </article>
  );
}

// The `Table` component override only receives {children, style} from
// TableVirtuoso (no className passthrough) -- setting the real arr-table
// classes here is how the virtualized table keeps the exact same CSS the
// vanilla arr-table styling and the ≤760px responsive card transform expect.
const seriesTableComponents = {
  Table: (props: { children?: React.ReactNode; style?: React.CSSProperties }) => (
    <table {...props} className="arr-table inkdrop-series-table" />
  ),
};

function seriesTableHeader() {
  return (
    <tr>
      <th>Series</th>
      <th>Publisher</th>
      <th>Year</th>
      <th>Status</th>
      <th>Actions</th>
    </tr>
  );
}

function SeriesTable({
  rows,
  onOpen,
  virtuosoRef,
}: {
  rows: SeriesRow[];
  onOpen: (row: SeriesRow) => void;
  virtuosoRef: React.RefObject<TableVirtuosoHandle | null>;
}) {
  if (rows.length === 0) {
    return (
      <table className="arr-table inkdrop-series-table">
        <thead>{seriesTableHeader()}</thead>
        <tbody>
          <tr>
            <td colSpan={5}>No series match this filter.</td>
          </tr>
        </tbody>
      </table>
    );
  }
  return (
    // A real height + its own scrollbar, not useWindowScroll -- this app's
    // layout is auto-height all the way to <body> (nothing declares a fixed
    // viewport height), so useWindowScroll's internal height:100% div has
    // nothing to resolve against and collapses to ~1 row tall (confirmed
    // live). A self-contained scroll panel is the standard Virtuoso pattern
    // and doesn't depend on that chain at all.
    <TableVirtuoso
      ref={virtuosoRef}
      className="inkdrop-series-virtual-scroller"
      data={rows}
      components={seriesTableComponents}
      computeItemKey={(index, row) => rowKey(row, index)}
      fixedHeaderContent={seriesTableHeader}
      itemContent={(_index, row) => {
        const status = seriesStatus(row);
        return (
          <>
            <td data-label="Series">{row.title || "Unknown series"}</td>
            <td data-label="Publisher">{seriesRibbonPublisher(row)}</td>
            <td data-label="Year">{row.year ? String(row.year) : ""}</td>
            <td data-label="Status">
              <span className={`inkdrop-series-pill ${status.tone}`.trim()} title={status.detail || undefined}>
                {status.label}
              </span>
            </td>
            <td data-label="Actions">
              <button type="button" onClick={() => onOpen(row)} title={`Open ${row.title || "series"}`}>
                Details
              </button>
            </td>
          </>
        );
      }}
    />
  );
}

function SeriesPosterGrid({
  rows,
  onOpen,
  virtuosoRef,
}: {
  rows: SeriesRow[];
  onOpen: (row: SeriesRow) => void;
  virtuosoRef: React.RefObject<VirtuosoGridHandle | null>;
}) {
  if (rows.length === 0) {
    return <p className="inkdrop-series-empty">No series match this filter.</p>;
  }
  return (
    <VirtuosoGrid
      ref={virtuosoRef}
      // The root scroller stays the poster layout's first grid column (same
      // slot .inkdrop-series-grid used to occupy directly) -- listClassName
      // carries the real auto-fill grid CSS one level in, on the element
      // Virtuoso actually renders the cards into. useWindowScroll doesn't
      // work in this app's auto-height layout (nothing in the ancestor
      // chain gives the scroller's internal height:100% div a real height
      // to resolve against, confirmed live: it collapsed to ~1 row tall) --
      // this container owns a real height and scrolls itself instead, the
      // standard/most-tested Virtuoso pattern.
      className="inkdrop-series-grid-scroller"
      listClassName="inkdrop-series-grid"
      data={rows}
      computeItemKey={(index, row) => rowKey(row, index)}
      itemContent={(_index, row) => <SeriesCard row={row} onOpen={onOpen} />}
    />
  );
}

// Preserved from the vanilla poster shelf. Now cheap to keep regardless of
// library size -- jumping doesn't depend on every card already being
// mounted, so this no longer trades off against render cost the way it did
// before virtualization.
function AlphabetRail({
  letters,
  onJump,
}: {
  letters: string[];
  onJump: (letter: string) => void;
}) {
  if (letters.length < 2) return null;
  return (
    <nav className="inkdrop-series-alpha-rail" aria-label="Series alphabet jump rail">
      {letters.map((letter) => (
        <button key={letter} type="button" onClick={() => onJump(letter)} title={`Jump to ${letter}`}>
          {letter}
        </button>
      ))}
    </nav>
  );
}

export function Series({ payload }: { payload: SeriesViewPayload }) {
  const [rows, setRows] = useState<SeriesRow[]>(payload.rows || []);
  const [ui, setUi] = useState<SeriesUiState>(payload.series_ui || {});
  const tableRef = useRef<TableVirtuosoHandle>(null);
  const gridRef = useRef<VirtuosoGridHandle>(null);

  // A fresh payload reference means the shell re-rendered this section --
  // a toolbar change, a filter change, or section re-entry. Same contract the
  // other migrated sections use.
  useEffect(() => {
    setRows(payload.rows || []);
    setUi(payload.series_ui || {});
  }, [payload]);

  const posterView = String(ui.view_mode || "table").toLowerCase() === "posters";

  const letters = useMemo(() => {
    const seen = new Set<string>();
    for (const row of rows) seen.add(seriesInitial(row));
    return [...seen].sort((a, b) => (a === "#" ? 1 : b === "#" ? -1 : a.localeCompare(b)));
  }, [rows]);

  function openSeries(row: SeriesRow) {
    // Opening a series detail is shell-owned: it swaps the whole section for
    // the detail view and owns that view's data fetching. Call through rather
    // than reimplementing it here.
    const shell = window as unknown as { InkDropSeriesNav?: { openDetail: (row: SeriesRow) => void } };
    shell.InkDropSeriesNav?.openDetail(row);
  }

  // Neither view keeps every row mounted as a real DOM node any more, so a
  // plain querySelector can no longer find an off-screen target -- jump by
  // index through the virtualizer's own imperative scroll API instead. The
  // full `rows` array (and therefore `letters`) is still always in memory,
  // so the jump itself is still instant regardless of library size.
  //
  // "auto" (an instant snap), not "smooth": measured live, a smooth-scroll
  // jump from the top to a late letter (e.g. S in a 385-series library)
  // takes 1.3-1.5s to finish animating -- real, but not what "jump to a
  // letter instantly" means. The old (pre-virtualization) alphabet rail used
  // a smooth native scrollIntoView; this intentionally does not carry that
  // forward now that the jump distance can span the whole library.
  function jumpToLetter(letter: string) {
    const index = rows.findIndex((row) => seriesInitial(row) === letter);
    if (index < 0) return;
    const target = { index, align: "start" as const, behavior: "auto" as const };
    if (posterView) gridRef.current?.scrollToIndex(target);
    else tableRef.current?.scrollToIndex(target);
  }

  if (!posterView) {
    return (
      <div className="inkdrop-react-series">
        <SeriesTable rows={rows} onOpen={openSeries} virtuosoRef={tableRef} />
      </div>
    );
  }

  return (
    <div className="inkdrop-react-series inkdrop-series-poster-layout">
      <SeriesPosterGrid rows={rows} onOpen={openSeries} virtuosoRef={gridRef} />
      {ui.alphabet_rail !== false && <AlphabetRail letters={letters} onJump={jumpToLetter} />}
    </div>
  );
}
