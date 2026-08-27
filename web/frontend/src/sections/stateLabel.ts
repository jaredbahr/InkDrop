import { coreStateLabel } from "./seriesDetailTypes";

// One state, one name.
//
// `needs_you` used to arrive at the operator three ways: the server sentence-
// cases it onto `state_label` as "Needs you" and mobile renders that; desktop's
// coreStateLabel() has a lifecycle table that said "Needs Review"; and the
// Manual Review, Queue and Wanted islands each had their own stageLabel()/
// statusText() that title-cased whatever they were handed -- which turned the
// server's own "Needs you" into "Needs You". Three renderings, one state.
//
// The three island copies are this function now. Two rules, in order:
//
//  1. **A label the server supplied is rendered verbatim.** The server said
//     what the state is called; re-casing its answer is how a client quietly
//     disagrees with it, and is exactly where "Needs You" came from.
//  2. **Otherwise derive with coreStateLabel()**, the vocabulary desktop
//     already uses, rather than a second casing rule per island.
export function rowStateLabel(
  row: {
    display_state_label?: string;
    state_label?: string;
    display_state?: string;
    state?: string;
    status?: string;
  },
  fallbackState = "",
): string {
  const supplied = String(row?.display_state_label || row?.state_label || "").trim();
  if (supplied) return supplied;
  const raw = String(row?.display_state || row?.state || row?.status || fallbackState || "").trim();
  if (!raw) return "";
  return coreStateLabel(raw);
}
