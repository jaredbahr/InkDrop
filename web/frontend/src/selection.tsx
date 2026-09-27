import { useEffect, useRef, type ReactElement } from "react";

// Selection and action feedback that assistive technology can actually read.
//
// Three things were missing from every island with a bulk-select table.
//
//   1. A HEADER CHECKBOX THAT LIES. Wanted and Manual Review both bound
//      `checked={allSelectableSelected}` and nothing else, so selecting
//      three rows out of twelve left the header visually and semantically
//      UNCHECKED -- identical to having selected nothing. HTML has a third
//      state for exactly this and it is not expressible as an attribute:
//      `indeterminate` is a DOM property, so React cannot set it in JSX and
//      it has to be written to the node. That is the whole reason this
//      component exists rather than a prop on the input.
//
//   2. NOTHING ANNOUNCED. Row labels and the error banner are visible, and
//      the error banner is a role="alert", but a selection going from three
//      rows to four, or a bulk action finishing, produced no announcement at
//      all. A screen-reader user had no way to hear the count they were
//      about to act on.
//
//   3. FOCUS FELL TO THE BODY. Acting on a row usually removes it, and the
//      table reload replaces every node. When the focused control unmounts,
//      focus lands on <body> and keyboard navigation restarts from the top
//      of the document -- after every single row action.
//
// The announcements follow the convention the shell's query controls already
// set (web/static/js/inkdrop-operational-query-controls.js): one polite
// role="status" region carrying a whole sentence, written only when
// something actually changed. Nothing here announces on a poll or a
// re-render, which is what makes it usable rather than chatter.

type SelectAllProps = {
  // The ids on screen that CAN be selected -- not every row is selectable,
  // so a count taken from the rows would report a total the header cannot
  // ever reach.
  selectableIds: readonly string[];
  selectedIds: ReadonlySet<string>;
  onChange: (checked: boolean) => void;
  label?: string;
};

export function selectionCounts(
  selectableIds: readonly string[],
  selectedIds: ReadonlySet<string>,
): { selected: number; selectable: number; all: boolean; partial: boolean } {
  // Counted against what is on screen. selectedIds can outlive a reload and
  // still hold ids for rows this page no longer shows; those must not count
  // towards a header describing the rows in front of the operator.
  const selected = selectableIds.reduce((total, id) => total + (selectedIds.has(id) ? 1 : 0), 0);
  const selectable = selectableIds.length;
  return {
    selected,
    selectable,
    all: selectable > 0 && selected === selectable,
    partial: selected > 0 && selected < selectable,
  };
}

export function SelectAllCheckbox({
  selectableIds,
  selectedIds,
  onChange,
  label = "Select all visible rows",
}: SelectAllProps): ReactElement {
  const ref = useRef<HTMLInputElement>(null);
  const { selected, selectable, all, partial } = selectionCounts(selectableIds, selectedIds);
  useEffect(() => {
    // Written to the node on every render that changes it: `indeterminate`
    // has no JSX attribute, and React will happily leave a stale true on a
    // checkbox whose `checked` it has just updated.
    if (ref.current) ref.current.indeterminate = partial;
  }, [partial]);
  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label={label}
      checked={all}
      disabled={selectable === 0}
      // Screen readers report the property, but the title is what a sighted
      // mouse user gets, and "3 of 12" is the part neither had.
      title={selectable === 0 ? undefined : `${selected} of ${selectable} selected`}
      onChange={(event) => onChange(event.target.checked)}
    />
  );
}

type StatusProps = {
  selectableIds: readonly string[];
  selectedIds: ReadonlySet<string>;
  // The newest settled action outcome, or null. Failures are deliberately
  // NOT routed here: they already have a role="alert" banner, and saying the
  // same thing twice in two regions is worse than saying it once.
  outcome?: string | null;
};

export function SelectionStatus({ selectableIds, selectedIds, outcome }: StatusProps): ReactElement {
  const { selected, selectable } = selectionCounts(selectableIds, selectedIds);
  const selectionSentence =
    selected === 0
      ? "No rows selected."
      : `${selected} of ${selectable} row${selectable === 1 ? "" : "s"} selected.`;
  return (
    <span className="inkdrop-visually-hidden" role="status" aria-live="polite">
      {outcome ? `${outcome} ${selectionSentence}` : selectionSentence}
    </span>
  );
}

// Keep keyboard focus inside the table across a reload.
//
// A row action usually removes its row, and the coalesced reload replaces
// every node in the tbody regardless. Whatever had focus is gone, focus
// resets to <body>, and the next Tab starts from the top of the document.
// This notices that specific transition -- focus WAS inside the container,
// and is now on the body -- and puts it on the container's first focusable
// control instead. It never moves focus that a user or another component
// placed deliberately.
export function useFocusRetention(rowsSignature: string) {
  const containerRef = useRef<HTMLDivElement>(null);
  const hadFocus = useRef(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const onFocusIn = () => { hadFocus.current = true; };
    container.addEventListener("focusin", onFocusIn);
    return () => container.removeEventListener("focusin", onFocusIn);
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !hadFocus.current) return;
    const active = document.activeElement;
    // Only the orphaned case. If focus is still on something real -- inside
    // this container or anywhere else the user put it -- leave it alone.
    if (active && active !== document.body && container.contains(active)) return;
    if (active && active !== document.body) {
      hadFocus.current = false;
      return;
    }
    const target = container.querySelector<HTMLElement>(
      'input[type="checkbox"]:not([disabled]), button:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    target?.focus();
  }, [rowsSignature]);

  return containerRef;
}
