import { useEffect, useRef, useState } from "react";
import { InkDropApiError } from "./api";

// Shared row-action discipline for every island with per-row mutation
// buttons (Wanted Search, Queue Retry, Blocklist Allow/Remove, ...).
//
// Rapid clicks across different rows must ALL land. That imposes three
// rules the earlier copy-pasted handlers each broke in the same ways:
//  - pending state is per row (a single shared pendingId re-enabled row A's
//    button and hid its progress the moment row B was clicked);
//  - the follow-up table reload is coalesced to once per click-burst
//    (reloading after every click reshuffles rows under the cursor and
//    eats the next click);
//  - an action failure survives the reload (the reload's own error reset
//    used to wipe the only message saying which click failed).
export function useRowActions(reload: () => void | Promise<void>) {
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set());
  const [doneIds, setDoneIds] = useState<ReadonlyMap<string, string>>(new Map());
  const [actionError, setActionError] = useState<string | null>(null);
  const reloadTimer = useRef<number>(0);
  const inFlight = useRef(0);
  // Always call the latest reload closure -- the one captured at schedule
  // time can hold a stale offset/filter.
  const reloadRef = useRef(reload);
  reloadRef.current = reload;

  function scheduleReload() {
    window.clearTimeout(reloadTimer.current);
    reloadTimer.current = window.setTimeout(() => {
      if (inFlight.current === 0) {
        setDoneIds(new Map());
        void reloadRef.current();
      } else {
        scheduleReload();
      }
    }, 600);
  }
  useEffect(() => () => window.clearTimeout(reloadTimer.current), []);

  async function runRowAction(
    id: string,
    rowLabel: string,
    doneLabel: string,
    action: () => Promise<void>,
  ) {
    if (pendingIds.has(id)) return;
    setPendingIds((prev) => new Set(prev).add(id));
    inFlight.current += 1;
    try {
      await action();
      setDoneIds((prev) => new Map(prev).set(id, doneLabel));
    } catch (cause) {
      const detail = cause instanceof InkDropApiError ? cause.message : "The action failed.";
      setActionError(`${rowLabel}: ${detail}`);
    } finally {
      inFlight.current -= 1;
      setPendingIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      scheduleReload();
    }
  }

  function clearActionError() {
    setActionError(null);
  }

  return { pendingIds, doneIds, actionError, clearActionError, runRowAction };
}
