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
//
// That last rule is why failures have to be retired deliberately rather
// than on the next render: nothing else clears them. Every operation gets a
// monotonic sequence number at click time, and the announced failure
// remembers which operation raised it, so ordering decides who wins:
//  - a NEWER operation succeeding retires an older operation's failure --
//    the banner is about a click the operator has since superseded;
//  - an OLDER operation completing (success or failure) never touches a
//    newer failure -- otherwise a slow first request landing after a fast
//    second one would erase the message describing the click that just
//    failed;
//  - a newer failure always replaces an older one.
//
// Those rules have to hold on arrival order too, which is why success is
// remembered even when there is no failure to retire. The first version only
// retired an EXISTING failure, so a newer success that landed before an older
// failure recorded nothing, and the late older failure was then announced as
// if nothing had superseded it. Identical clicks and identical outcomes gave
// opposite banners depending purely on which request the server answered
// first:
//   fail(1) then succeed(2) -> banner cleared
//   succeed(2) then fail(1) -> banner announced
// So the newest successfully settled sequence is tracked, and a failure older
// than it is never announced.
// Without this, the banner only cleared when the surrounding shell handed
// the island a fresh `payload` prop -- which the hook's own reload does not
// produce -- so a failed mutation stayed announced, indefinitely and
// looking current, through any number of later successful ones.
type ActionFailure = { seq: number; message: string };

export function useRowActions(reload: () => void | Promise<void>) {
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set());
  const [doneIds, setDoneIds] = useState<ReadonlyMap<string, string>>(new Map());
  const [failure, setFailure] = useState<ActionFailure | null>(null);
  // What a screen reader is told when a burst of row actions settles. The
  // visible row labels and the error banner are both sighted-only feedback:
  // clicking Search on six rows and having all six succeed produced no
  // announcement whatsoever. Announced once per burst, not once per row --
  // six separate utterances for one gesture is noise, not information.
  //
  // Only successes come through here. Failures already own a role="alert"
  // banner, and routing them to a second region would say the same thing
  // twice.
  const [actionOutcome, setActionOutcome] = useState<string | null>(null);
  const succeededLabels = useRef<string[]>([]);
  const reloadTimer = useRef<number>(0);
  const inFlight = useRef(0);
  // Click order, not completion order. Assigned before the request goes out
  // so two overlapping actions keep a stable relative age no matter which
  // one the server answers first.
  const nextSeq = useRef(0);
  // Newest sequence that has settled successfully. A ref rather than state:
  // it is ordering bookkeeping that must be readable by whichever handler
  // runs next, not something a render depends on.
  const settledSeq = useRef(0);
  // Always call the latest reload closure -- the one captured at schedule
  // time can hold a stale offset/filter.
  const reloadRef = useRef(reload);
  reloadRef.current = reload;

  function scheduleReload() {
    window.clearTimeout(reloadTimer.current);
    reloadTimer.current = window.setTimeout(() => {
      if (inFlight.current === 0) {
        // The burst is over: everything clicked has settled. This is the
        // moment that carries one announcement rather than N.
        const done = succeededLabels.current;
        succeededLabels.current = [];
        if (done.length === 1) setActionOutcome(`${done[0]} done.`);
        else if (done.length > 1) setActionOutcome(`${done.length} actions completed.`);
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
    nextSeq.current += 1;
    const seq = nextSeq.current;
    setPendingIds((prev) => new Set(prev).add(id));
    inFlight.current += 1;
    try {
      await action();
      setDoneIds((prev) => new Map(prev).set(id, doneLabel));
      succeededLabels.current.push(`${rowLabel}: ${doneLabel}`);
      // Recorded whether or not there is a failure to retire right now: an
      // older operation may still be in flight and fail later.
      settledSeq.current = Math.max(settledSeq.current, seq);
      // Only an operation newer than the announced failure retires it.
      setFailure((prev) => (prev && prev.seq < seq ? null : prev));
    } catch (cause) {
      const detail = cause instanceof InkDropApiError ? cause.message : "The action failed.";
      setFailure((prev) => {
        // A newer operation already succeeded, so this click has been
        // superseded -- the same reason a newer success retires an existing
        // failure. Announcing it here would make the banner depend on which
        // request the server happened to answer first.
        if (seq < settledSeq.current) return prev;
        // Newest failure wins; an older one landing late must not overwrite it.
        return prev && prev.seq > seq ? prev : { seq, message: `${rowLabel}: ${detail}` };
      });
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

  // The shell handing down a fresh `payload` means a different page/filter
  // is now on screen, so any row failure announced about the old one is
  // unconditionally stale -- that reset stays absolute.
  function clearActionError() {
    setFailure(null);
    // The announced outcome is about the same superseded page of rows.
    setActionOutcome(null);
  }

  return {
    pendingIds,
    doneIds,
    actionError: failure ? failure.message : null,
    actionOutcome,
    clearActionError,
    runRowAction,
  };
}
