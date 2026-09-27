"""How long the automatic search actually takes to get through the backlog.

WHY THIS EXISTS
    Nothing in the product answered "is every wanted item searched in a
    reasonable time". ``reliability_health_signals()`` reports the median and
    p90 wait since a real search (6.5d and 20.5d on 2026-08-17) and alerts
    above seven days, which says the tail is bad but never says how long a
    full sweep takes, so there was no number to set a target against and no
    number to tell whether a scheduling change helped.

    The scheduler makes that easy to get wrong. Every pass recomputes a
    ranking over the schedulable set, funds a small prefix and forgets; a
    convergence defect and a throughput defect both show up as "the tail is
    old", and only a coverage figure separates them:

      * time-to-cover  = population / rows genuinely served per day. A
        throughput answer. It is a floor, not a promise -- it is what a
        perfectly convergent order would take.
      * unserved-in-window and oldest-unserved. A convergence answer. If the
        estimate says 1.4 days and rows are still sitting at 30, selection is
        not reaching them, whatever the throughput is.

    Both are reported, always, because either alone is misread as the other.

WHAT "SCHEDULABLE" AND "SERVED" MEAN HERE
    Neither is defined in this file. The population comes from
    ``inkdrop_source_worker_scheduler.schedulable_queue_clauses()`` -- the
    same clauses the shipping scan selects on -- and "a real attempt" comes
    from ``inkdrop_state.real_attempt_predicate_sql()``. A coverage number
    computed over a hand-written population is a number about that
    population, not about what the worker has to get through, and a coverage
    number that counts lifecycle rows as searches reports a backlog as served
    when nothing searched it.

    Budget-starved skips are excluded by that predicate, which is the point:
    a row the pass dropped for want of runtime was not searched, and must
    read as unserved here exactly as it does to the retry ceiling.

WHAT IT CANNOT SEE
    A snapshot is a moment. ``funded_per_day`` is measured over the window
    from ``source_attempts`` rows that survive retention, so a window longer
    than retention undercounts throughput and overstates days-to-cover.
    ``estimated_days_to_cover`` assumes the rows served in the window were
    distinct sweeps of the backlog; re-serving the same rows inflates it in
    the optimistic direction, which is why ``served_rows_window`` (coverage of
    the population) is reported beside ``funded_rows_window`` (throughput over
    everything). When the two diverge, the order is re-serving its head.
"""

import time

try:
    from core import inkdrop_state
    from core import inkdrop_source_worker_scheduler as scheduler
except ImportError:  # pragma: no cover - standalone execution
    import inkdrop_state
    import inkdrop_source_worker_scheduler as scheduler


DAY_SECONDS = 86400.0

DEFAULT_WINDOW_DAYS = 7.0
DEFAULT_TARGET_DAYS = 7.0
DEFAULT_STATES = ("queued", "searching")

# Two item flushes further apart than this are counted as different passes.
# There is no pass id in source_worker_phase_samples -- one item writes its
# rows when it finishes -- so pass boundaries are inferred, and the threshold
# is reported beside the figure rather than buried here. The shipping lane
# runs for at most max_run_seconds and then waits out its interval, so any
# value between the longest in-pass gap and the shortest inter-pass gap gives
# the same answer; 300s sits inside that band for the shipped 1800s interval
# and for the 300s one this exists to justify.
DEFAULT_PASS_GAP_SECONDS = 300.0


def _percentile(values, fraction):
    """Nearest-rank percentile over a list of floats, None when empty."""
    values = sorted(float(value) for value in values or [] if value is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = max(0.0, min(1.0, float(fraction))) * (len(values) - 1)
    low = int(position)
    high = min(low + 1, len(values) - 1)
    weight = position - low
    return values[low] * (1.0 - weight) + values[high] * weight


def _iso(epoch_seconds):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(epoch_seconds)))
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _days(seconds):
    try:
        return max(0.0, float(seconds)) / DAY_SECONDS
    except (TypeError, ValueError):
        return None


def schedulable_rows(con, *, now=None, states=DEFAULT_STATES, due_only=True):
    """Every schedulable row with how long it has waited for a real search.

    One query, one pass over the attempt ledger, restricted to the schedulable
    set -- the same shape ``_queue_rows()`` uses, for the same reason: as two
    correlated subqueries the same answers cost 2.2s against the live
    database.

    ``stall_seconds`` is computed with the scheduler's own CASE, download-task
    fallback included, so "how long has this waited" means one thing whether
    it is being ordered on or reported on.
    """
    now = time.time() if now is None else float(now)
    clauses, params = scheduler.schedulable_queue_clauses(
        states=list(states or []),
        due_only=bool(due_only),
        now=now,
    )
    where = " and ".join(clauses)
    real_attempts_cte = inkdrop_state.real_attempts_by_queue_cte_sql(
        "sa", "join schedulable_queue sq on sq.id = sa.queue_id"
    )
    sql = f"""
        with schedulable_queue as (
            select q.id, q.series_id, q.created_at
            from queue_items q
            left join series s on s.id=q.series_id
            left join wanted_items w on w.id=q.wanted_id
            where {where}
        ), {real_attempts_cte}
        select sq.id as queue_id,
               sq.series_id as series_id,
               coalesce(ra.real_attempt_count, 0) as real_attempt_count,
               coalesce(ra.last_real_attempt_at, 0) as last_real_attempt_at,
               max(0.0, ? - case
                 when coalesce(ra.last_real_attempt_at, 0) > 0 then ra.last_real_attempt_at
                 else coalesce(
                   nullif((select min(coalesce(dt.completed_at, dt.started_at, 0))
                             from download_tasks dt
                            where dt.queue_id=sq.id
                              and coalesce(dt.completed_at, dt.started_at, 0)>0), 0),
                   sq.created_at,
                   ?)
               end) as stall_seconds
        from schedulable_queue sq
        left join real_attempts ra on ra.queue_id = sq.id
    """
    rows = con.execute(sql, (*params, now, now)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["real_attempt_count"] = int(item.get("real_attempt_count") or 0)
        item["last_real_attempt_at"] = float(item.get("last_real_attempt_at") or 0.0)
        item["stall_seconds"] = float(item.get("stall_seconds") or 0.0)
        out.append(item)
    return out


def _distinct_served_queue_ids(con, *, since, schedulable_only, states, due_only, now):
    """How many distinct rows got a genuine search since `since`.

    Two populations, deliberately: restricted to the schedulable set this is
    coverage of the backlog, and unrestricted it is throughput -- a row served
    and then completed still spent a seat. Dividing the population by the
    coverage count rather than the throughput count would report a sweep as
    slower than the machine actually is.
    """
    real_attempt_predicate = inkdrop_state.real_attempt_predicate_sql("sa")
    window_clause = "coalesce(sa.completed_at, sa.started_at, 0) >= ?"
    if not schedulable_only:
        sql = f"""
            select count(distinct sa.queue_id) as n
            from source_attempts sa
            where {real_attempt_predicate}
              and {window_clause}
              and coalesce(nullif(trim(sa.queue_id), ''), '') <> ''
        """
        row = con.execute(sql, (since,)).fetchone()
        return int((row[0] if row else 0) or 0)
    clauses, params = scheduler.schedulable_queue_clauses(
        states=list(states or []), due_only=bool(due_only), now=now,
    )
    where = " and ".join(clauses)
    sql = f"""
        with schedulable_queue as (
            select q.id from queue_items q
            left join series s on s.id=q.series_id
            left join wanted_items w on w.id=q.wanted_id
            where {where}
        )
        select count(distinct sa.queue_id) as n
        from source_attempts sa
        join schedulable_queue sq on sq.id = sa.queue_id
        where {real_attempt_predicate}
          and {window_clause}
    """
    row = con.execute(sql, (*params, since)).fetchone()
    return int((row[0] if row else 0) or 0)


def pass_timing_report(con, *, now=None, window_days=DEFAULT_WINDOW_DAYS,
                       pass_gap_seconds=DEFAULT_PASS_GAP_SECONDS):
    """Per-item elapsed and funded-per-pass, read off the phase timers.

    This is the other half of the contradiction the coverage figure surfaces:
    a pass funds a median of 9 rows out of an 80-row scan while an item is
    measured at a median of 228s, and 9 x 228s does not fit a 600s pass. Only
    one of those can be true of the same passes, and the two have never been
    printed from the same window before.

    Pass membership is inferred from the gap between item flushes, because
    the table carries no pass id. The threshold is returned with the figure.
    """
    now = time.time() if now is None else float(now)
    window_seconds = max(1.0, float(window_days) * DAY_SECONDS)
    since = now - window_seconds
    report = {
        "as_of": now,
        "as_of_iso": _iso(now),
        "window_days": float(window_days),
        "window_seconds": window_seconds,
        "pass_gap_seconds": float(pass_gap_seconds),
        "predicate": (
            "distinct (queue_id, created_at) in source_worker_phase_samples "
            "with created_at >= as_of - window_seconds"
        ),
    }
    if not inkdrop_state.table_exists(con, "source_worker_phase_samples"):
        report["ok"] = False
        report["reason"] = "source_worker_phase_samples_missing"
        return report
    rows = con.execute(
        """
        select distinct queue_id, created_at, item_elapsed_seconds
        from source_worker_phase_samples
        where created_at >= ?
        order by created_at asc
        """,
        (since,),
    ).fetchall()
    items = [
        (float(row[1] or 0.0), float(row[2] or 0.0))
        for row in rows or []
    ]
    report["ok"] = True
    report["items_sampled"] = len(items)
    elapsed = [value for _created_at, value in items]
    report["item_elapsed_p50_seconds"] = _percentile(elapsed, 0.5)
    report["item_elapsed_p90_seconds"] = _percentile(elapsed, 0.9)
    if not items:
        report["passes_observed"] = 0
        report["funded_per_pass_p50"] = None
        report["funded_per_pass_max"] = None
        return report
    passes = [1]
    previous = items[0][0]
    for created_at, _value in items[1:]:
        if created_at - previous > float(pass_gap_seconds):
            passes.append(0)
        passes[-1] += 1
        previous = created_at
    report["passes_observed"] = len(passes)
    report["funded_per_pass_p50"] = _percentile(passes, 0.5)
    report["funded_per_pass_max"] = max(passes)
    return report


def search_coverage_report(
    con,
    *,
    now=None,
    window_days=DEFAULT_WINDOW_DAYS,
    target_days=DEFAULT_TARGET_DAYS,
    states=DEFAULT_STATES,
    due_only=True,
):
    """Coverage of the schedulable backlog by genuine searches.

    Every figure is returned beside the predicate that produced it, the window
    it was measured over and the instant it was measured at, because a
    coverage number quoted without those three is not checkable.

    The two controls are taken from the result set rather than named in
    advance: the oldest unserved row and the most recently served row. A run
    where either is ``None`` is a run whose split has an empty side, and the
    caller is expected to treat a zero from such a run as a reading of the
    instrument rather than a fact about the system.
    """
    now = time.time() if now is None else float(now)
    window_days = float(window_days)
    window_seconds = max(1.0, window_days * DAY_SECONDS)
    rows = schedulable_rows(con, now=now, states=states, due_only=due_only)
    total = len(rows)

    never = [row for row in rows if row["real_attempt_count"] <= 0]
    unserved = [row for row in rows if row["stall_seconds"] >= window_seconds]
    served_in_window = [row for row in rows if row["stall_seconds"] < window_seconds]
    stalls = [row["stall_seconds"] for row in rows]

    day_since = now - DAY_SECONDS
    window_since = now - window_seconds
    served_rows_24h = _distinct_served_queue_ids(
        con, since=day_since, schedulable_only=True, states=states, due_only=due_only, now=now,
    )
    served_rows_window = _distinct_served_queue_ids(
        con, since=window_since, schedulable_only=True, states=states, due_only=due_only, now=now,
    )
    funded_rows_24h = _distinct_served_queue_ids(
        con, since=day_since, schedulable_only=False, states=states, due_only=due_only, now=now,
    )
    funded_rows_window = _distinct_served_queue_ids(
        con, since=window_since, schedulable_only=False, states=states, due_only=due_only, now=now,
    )
    funded_per_day_24h = float(funded_rows_24h)
    funded_per_day_window = float(funded_rows_window) / (window_seconds / DAY_SECONDS)
    estimated_days_to_cover = (
        (float(total) / funded_per_day_window) if funded_per_day_window > 0 and total else None
    )

    oldest_unserved = max(unserved, key=lambda row: row["stall_seconds"], default=None)
    newest_served = min(served_in_window, key=lambda row: row["stall_seconds"], default=None)

    def control(row, label):
        if row is None:
            return {
                "label": label,
                "found": False,
                "why": "no row on this side of the split; treat any zero above as unproven",
            }
        return {
            "label": label,
            "found": True,
            "queue_id": row.get("queue_id"),
            "series_id": row.get("series_id"),
            "real_attempt_count": row.get("real_attempt_count"),
            "last_real_attempt_at": row.get("last_real_attempt_at") or None,
            "last_real_attempt_at_iso": _iso(row["last_real_attempt_at"]) if row.get("last_real_attempt_at") else None,
            "stall_days": _days(row.get("stall_seconds")),
        }

    return {
        "ok": True,
        "as_of": now,
        "as_of_iso": _iso(now),
        "window_days": window_days,
        "window_seconds": window_seconds,
        "target_days": float(target_days),
        "states": list(states or []),
        "due_only": bool(due_only),
        "predicates": {
            "schedulable": " and ".join(
                scheduler.schedulable_queue_clauses(
                    states=list(states or []), due_only=bool(due_only), now=now,
                )[0]
            ),
            "real_attempt": inkdrop_state.real_attempt_predicate_sql("sa"),
            "unserved": "stall_seconds >= window_seconds",
            "stall": "now - (last real attempt, else first download task, else created_at)",
        },
        "schedulable_rows": total,
        "never_served": len(never),
        "unserved_in_window": len(unserved),
        "unserved_in_window_share": (len(unserved) / float(total)) if total else None,
        "oldest_unserved_age_days": _days(oldest_unserved["stall_seconds"]) if oldest_unserved else None,
        "served_rows_24h": served_rows_24h,
        "served_rows_window": served_rows_window,
        "funded_rows_24h": funded_rows_24h,
        "funded_rows_window": funded_rows_window,
        "funded_per_day_24h": funded_per_day_24h,
        "funded_per_day_window": funded_per_day_window,
        "estimated_days_to_cover": estimated_days_to_cover,
        "wait_p50_days": _days(_percentile(stalls, 0.5)) if stalls else None,
        "wait_p90_days": _days(_percentile(stalls, 0.9)) if stalls else None,
        "over_target": bool(
            (estimated_days_to_cover is not None and estimated_days_to_cover > float(target_days))
            or (oldest_unserved is not None and oldest_unserved["stall_seconds"] > float(target_days) * DAY_SECONDS)
        ),
        "controls": [
            control(oldest_unserved, "oldest row with no real search in the window"),
            control(newest_served, "most recently searched row in the population"),
        ],
    }
