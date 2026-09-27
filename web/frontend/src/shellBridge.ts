// One declaration of what the vanilla shell publishes to the islands.
//
// WHY ONE FILE. These bridges were declared in four places, and three of them
// reached the window through `window as unknown as { ... }`. That cast is not
// a convenience -- it goes around the global declaration entirely, so nothing
// checked that the four agreed. They did not:
//
//   SeriesDetail.tsx  openDetail: (row: SeriesDetailRow) => void    // required
//   History.tsx       openDetail?: (row: {series_id?, series?}) => void
//   Series.tsx        openDetail: (row: SeriesRow) => void
//
// Same bridge, same property, three shapes and two opinions about whether it
// is guaranteed to exist. Whichever file you read told you something
// different about the shell, and the compiler had been told not to care.
//
// WHY EVERYTHING IS OPTIONAL HERE. The shell is a separate script. An island
// can mount before it has finished wiring a bridge, and a bridge can be
// absent on a page that never loaded the code behind it. "Required" was a
// claim about load order that nothing enforced -- the honest declaration is
// that a bridge may not be there, and the honest response is the capability
// check below rather than a call that throws or a click that does nothing.
//
// TARGET TYPES ARE STRUCTURAL, ON PURPOSE. The shell's handlers read a few
// named fields off whatever row they are given -- openManualSearchForRow
// reads series_id/issue_id/unit_id/edition_id/series/title/issue_number and
// does not care which view's row shape called it. Declaring the minimum each
// one actually reads lets every section pass its own row without a cast, and
// says in the type what the shell depends on.

/** The fields the shell's series navigation reads off a row. */
export type SeriesNavTarget = {
  series_id?: string;
  series?: string;
  title?: string;
};

/** The fields openManualSearchForRow reads off a row. */
export type ManualSearchTarget = {
  series_id?: string;
  issue_id?: string;
  unit_id?: string;
  edition_id?: string;
  series?: string;
  title?: string;
  // string | number, because that is what the rows actually carry -- the
  // shell stringifies it. Declaring only `string` would make this type a
  // description of what would be convenient rather than of what is passed.
  issue_number?: string | number;
};

export type WantedSearchTarget = {
  id?: string;
  series?: string;
  series_id?: string;
  issue_id?: string;
};

export type SeriesNavBridge = {
  openDetail?: (row: SeriesNavTarget) => void;
  openLinked?: (section: string, row: SeriesNavTarget) => void;
  openManualSearch?: (row: ManualSearchTarget) => boolean;
  runWantedSearch?: (payload: WantedSearchTarget) => Promise<void>;
  runSeriesSearch?: (payload: { id?: string; title?: string }) => Promise<void>;
  markImportWrong?: (row: ManualSearchTarget, seriesRow?: SeriesNavTarget | null) => Promise<void>;
  setIssueMonitored?: (row: ManualSearchTarget, monitored: boolean) => Promise<void>;
};

export type WantedNavBridge = {
  runSelectedSearches?: (rows: WantedSearchTarget[]) => Promise<void>;
};

declare global {
  interface Window {
    InkDropSeriesNav?: SeriesNavBridge;
    InkDropWantedNav?: WantedNavBridge;
  }
}

/**
 * Whether a shell capability is usable, and if not, why.
 *
 * The pattern this replaces is `shell.X?.y?.(...)` in a click handler: when
 * the bridge is absent the call evaluates to undefined, the handler returns,
 * and the operator has clicked an enabled button that did nothing and said
 * nothing. Optional chaining makes the ABSENCE safe and the SILENCE
 * inevitable. A control that cannot work should say so before it is clicked.
 *
 * `reason` is written for an operator, not a developer: it goes in a title
 * attribute next to a disabled control.
 */
export type Capability<T> = {
  available: boolean;
  reason: string;
  call: T | null;
};

export function shellCapability<T>(
  call: T | undefined,
  missingReason: string,
): Capability<T> {
  return call
    ? { available: true, reason: "", call }
    : { available: false, reason: missingReason, call: null };
}

export function seriesNav(): SeriesNavBridge {
  return window.InkDropSeriesNav || {};
}

export function wantedNav(): WantedNavBridge {
  return window.InkDropWantedNav || {};
}
