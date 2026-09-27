import { useEffect, useRef } from "react";

/**
 * Ignore the answer to a question nobody is asking any more.
 *
 * Every section's loadPage applied whatever came back, so the reply that landed
 * LAST won rather than the one asked for last. The full reproduction is in
 * tests/inkdrop-a-late-answer-is-not-the-current-one-smoke.py.
 *
 * A generation counter rather than an AbortController: the bug is not that a
 * superseded request continues, it is that its RESULT is applied. A counter
 * also covers the `finally` that turns the spinner off, the `catch` that shows
 * an error for a page the user has left, and a response in flight at unmount.
 *
 *     const listRequest = useLatestOnly();
 *     const isCurrent = listRequest.begin();
 *     try { ...; if (!isCurrent()) return; setRows(...); }
 *     catch { if (!isCurrent()) return; setError(...); }
 *     finally { if (isCurrent()) setLoading(false); }
 *
 * The guard belongs on all three arms, not just success.
 */
export type LatestOnly = {
  /** Start a request, superseding any earlier one. The predicate it returns is
   *  true only while this request is still the newest. */
  begin: () => () => boolean;
  /** Supersede what is in flight without starting anything, so its result is
   *  dropped. For closing a panel, or leaving a selection. */
  cancel: () => void;
};

export function useLatestOnly(): LatestOnly {
  const generation = useRef(0);
  const guard = useRef<LatestOnly | null>(null);
  if (guard.current === null) {
    guard.current = {
      begin: () => {
        const mine = ++generation.current;
        return () => generation.current === mine;
      },
      cancel: () => {
        generation.current += 1;
      },
    };
  }
  // Unmounting supersedes everything in flight, so a late response cannot call
  // setState on a component that no longer exists.
  useEffect(() => () => {
    generation.current += 1;
  }, []);
  return guard.current;
}
