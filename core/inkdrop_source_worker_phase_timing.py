"""Where a source-worker item's wall clock actually goes.

The pass budget is 600s. Measured 2026-08-19, per item: median 228s, mean
566s, p90 1328s -- so a p90 item cannot finish inside a pass at all. But a
Prowlarr aggregate search measures ~8.2s and an slskd query ~1.5s, which
leaves roughly 90% of an item's cost unattributed. Every throughput decision
is currently made against an estimate of that 90%.

It could not be reconstructed after the fact. A session tried, from
``source_attempts`` start/complete timestamps, and discarded the result rather
than publish it: median 0.00s with 3400s maxima, because the pair mostly fires
at one instant and the large values are transfer lifetimes, not provider time.
It would have produced a confident "provider time is 55% of wall clock" that
meant nothing. The only honest fix is to time the phases as they run.

``run_source_worker_for_queue`` is the ~228s span, and its phases are already
distinct top-level calls, so the seam is natural rather than invented:

  * ``plan``          -- ``source_jobs_for_queue``: query construction
  * ``replay_check``  -- ``persisted_exact_pack_replay_result``
  * ``fetch``         -- ``run_source_jobs``: provider calls, waiting, matching
  * ``record``        -- ``record_source_job_results``: bookkeeping, DB writes
  * ``direct_stage``  -- ``stage_direct_download_tasks``
  * ``handoff``       -- ``handoff_download_client_tasks``

``fetch`` is further split per provider, because ``run_source_jobs`` already
loops one job per provider.

**What this can overturn.** The standing throughput analysis prices an item at
``8s + request_count * calibrated_seconds`` -- i.e. cost is driven by *request
count*, not latency. That model is calibrated against measured total cost (its
own comment records a plan that looked like 178s and really cost 528-756s), so
it fits the totals by construction whether or not its causal story is right.
If these timers show a typical item is mostly ``record`` or matching rather
than ``fetch``, request count is not the constraint and that analysis reopens.

Deliberate properties, because this writes from the hot loop:

  * **One write per item, not per phase.** Spans accumulate in memory and are
    flushed once, so an item that takes minutes pays a single ``executemany``.
  * **Telemetry may never fail a pass.** Every path is wrapped; a missing
    table or a locked database returns a reason and the pass continues.
  * **Silence is a reportable outcome, not a success.** ``record_item_phases``
    returns the row count it actually wrote, so a caller (and the smoke) can
    tell "wrote nothing" from "wrote fine" -- an exception-swallowing telemetry
    path that quietly never writes is indistinguishable from a working one.
  * **Disableable, and off is a real off.** ``INKDROP_SOURCE_WORKER_PHASE_TIMING=0``
    skips the timing calls themselves, not just the write.
  * **Bounded retention**, so a long-lived deployment cannot grow this table
    without limit.
"""

from __future__ import annotations

import os
import time
import uuid

from core import inkdrop_state


# Keep this many of the most recent samples per phase name.
SAMPLE_RETENTION_PER_PHASE = int(
    os.environ.get("INKDROP_SOURCE_WORKER_PHASE_SAMPLE_RETENTION", 2000) or 2000
)

# Phases in the order they run, so a reader of the table can reconstruct an
# item without knowing the coordinator's control flow.
PHASE_ORDER = (
    "plan",
    "replay_check",
    "fetch",
    "record",
    "sibling_fanout",
    "direct_stage",
    "handoff",
)


def enabled():
    value = str(
        os.environ.get("INKDROP_SOURCE_WORKER_PHASE_TIMING", "1") or ""
    ).strip().lower()
    return value not in {"0", "false", "off", "no", "disabled"}


class PhaseAccumulator:
    """Collects one item's phase spans in memory for a single flush.

    Deliberately not a context manager per phase: the coordinator's phases are
    plain sequential calls, and ``span()`` around each keeps the control flow
    identical to what it replaces. A phase that raises still records the time
    spent before the raise, because "this phase died after 40s" is exactly the
    observation the reconstruction attempts were missing.
    """

    __slots__ = ("phases", "provider_phases", "_started")

    def __init__(self):
        self.phases = {}
        self.provider_phases = {}
        self._started = None

    def span(self, name):
        return _Span(self, str(name or "").strip() or "unknown")

    def add(self, name, elapsed_seconds):
        name = str(name or "").strip() or "unknown"
        try:
            elapsed = max(0.0, float(elapsed_seconds))
        except (TypeError, ValueError):
            return
        self.phases[name] = self.phases.get(name, 0.0) + elapsed

    def add_provider(self, provider_id, elapsed_seconds):
        provider_id = str(provider_id or "").strip().lower()
        if not provider_id:
            return
        try:
            elapsed = max(0.0, float(elapsed_seconds))
        except (TypeError, ValueError):
            return
        self.provider_phases[provider_id] = (
            self.provider_phases.get(provider_id, 0.0) + elapsed
        )

    def total_seconds(self):
        return sum(self.phases.values())

    def snapshot(self):
        """A plain, JSON-serializable view.

        The coordinator's result dict is serialized by callers, so the live
        accumulator must never travel in it -- an object in that dict raises
        TypeError from json.dumps and fails the whole run, which is precisely
        the kind of breakage telemetry is not allowed to cause.
        """
        return {
            "phases": dict(self.phases),
            "provider_phases": dict(self.provider_phases),
        }


class _Span:
    __slots__ = ("_acc", "_name", "_start")

    def __init__(self, accumulator, name):
        self._acc = accumulator
        self._name = name
        self._start = None

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._start is not None:
            self._acc.add(self._name, time.monotonic() - self._start)
        # Never suppress: a phase that raised must still propagate.
        return False


def record_item_phases(
    db_path,
    queue_id,
    accumulator,
    *,
    provider_ids=None,
    lane=None,
    item_elapsed_seconds=None,
    truncated=False,
    now=None,
):
    """Flush one item's phase spans. Returns how many rows actually landed."""

    if not enabled():
        return {"ok": True, "skipped": "disabled", "recorded": 0}
    if accumulator is None:
        return {"ok": True, "skipped": "no_accumulator", "recorded": 0}
    if isinstance(accumulator, dict):
        phases = dict(accumulator.get("phases") or {})
        provider_phases = dict(accumulator.get("provider_phases") or {})
    else:
        phases = dict(getattr(accumulator, "phases", None) or {})
        provider_phases = dict(getattr(accumulator, "provider_phases", None) or {})
    if not phases and not provider_phases:
        return {"ok": True, "skipped": "no_phases", "recorded": 0}
    now = time.time() if now is None else float(now)
    try:
        item_elapsed = float(item_elapsed_seconds)
    except (TypeError, ValueError):
        item_elapsed = sum(phases.values())
    providers = ",".join(
        sorted(
            str(value or "").strip().lower()
            for value in (provider_ids or [])
            if str(value or "").strip()
        )
    )
    lane = str(lane or "").strip().lower()
    rows = []
    for name, elapsed in phases.items():
        rows.append(
            (
                uuid.uuid4().hex,
                str(queue_id or ""),
                providers,
                "phase",
                name,
                lane,
                round(float(elapsed), 4),
                round(max(0.0, item_elapsed), 4),
                1 if truncated else 0,
                now,
            )
        )
    for provider_id, elapsed in provider_phases.items():
        rows.append(
            (
                uuid.uuid4().hex,
                str(queue_id or ""),
                providers,
                "provider",
                provider_id,
                lane,
                round(float(elapsed), 4),
                round(max(0.0, item_elapsed), 4),
                1 if truncated else 0,
                now,
            )
        )
    if not rows:
        return {"ok": True, "skipped": "no_rows", "recorded": 0}
    try:
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            con.executemany(
                """
                insert into source_worker_phase_samples
                    (id, queue_id, provider_ids, sample_kind, phase, lane,
                     elapsed_seconds, item_elapsed_seconds, truncated, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            for phase in {row[4] for row in rows}:
                con.execute(
                    """
                    delete from source_worker_phase_samples
                    where phase = ?
                      and id not in (
                          select id from source_worker_phase_samples
                          where phase = ?
                          order by created_at desc limit ?
                      )
                    """,
                    (phase, phase, SAMPLE_RETENTION_PER_PHASE),
                )
    except Exception as exc:
        # Telemetry must never be able to fail a pass -- but the caller still
        # gets told nothing landed, so an always-empty table is visible rather
        # than mistaken for "no work happened".
        return {"ok": False, "reason": f"{type(exc).__name__}", "recorded": 0}
    return {"ok": True, "recorded": len(rows)}


def phase_breakdown(db_path, *, limit=None, now=None):
    """Median/mean seconds per phase over recent samples, for reporting."""

    if not enabled():
        return {"ok": True, "skipped": "disabled", "phases": {}}
    limit = int(limit or 5000)
    try:
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            rows = con.execute(
                """
                select phase, sample_kind, elapsed_seconds
                from source_worker_phase_samples
                order by created_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}", "phases": {}}
    buckets = {}
    for row in rows or []:
        phase = str(row[0] or "")
        kind = str(row[1] or "")
        try:
            elapsed = float(row[2])
        except (TypeError, ValueError):
            continue
        buckets.setdefault((kind, phase), []).append(elapsed)
    out = {}
    for (kind, phase), values in buckets.items():
        ordered = sorted(values)
        median = ordered[len(ordered) // 2]
        out[f"{kind}:{phase}"] = {
            "samples": len(ordered),
            "median_seconds": round(median, 3),
            "mean_seconds": round(sum(ordered) / float(len(ordered)), 3),
            "total_seconds": round(sum(ordered), 3),
        }
    return {"ok": True, "phases": dict(sorted(out.items())), "sampled_rows": len(rows or [])}
