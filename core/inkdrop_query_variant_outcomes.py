"""What each query variant actually returned, kept where it can be counted.

The measurement already happens. ``run_query`` in
``inkdrop_slskd_source_probe`` builds, per rung, an attempt carrying the query
text, ``elapsed_seconds``, ``response_count``, ``candidate_count`` and the
auto-grab counts. It is then used for this one search and dropped. Nothing
aggregates it, so two questions that sound basic have no answer:

  * **Which rungs earn their place?** Yield concentration is unmeasurable --
    there is no table to group by. A rung that returned zero fifty times is
    indistinguishable from one never tried.
  * **When may a rung be retired?** ``prune_futile_queries`` is fed by
    ``recent_empty_query_terms``, whose own docstring says it reads slskd's
    live ``/searches`` "because the ledger counts attempts and this needs
    outcomes". That window is one hour, it only drops on a subset match, and
    it never prunes the anchor -- 23 qualifier rungs pruned in the measured
    pass. Retirement therefore depends on an external service's short-lived
    memory rather than on anything InkDrop knows.

``source_attempts`` cannot answer either: it carries ``title`` (the query) and
timestamps but no yield columns at all, and its start/complete pair mostly
fires at one instant, so it counts attempts rather than outcomes -- exactly
what that docstring says.

So this persists what is already measured. It adds no probing, changes no
query, and makes no retirement decision on its own; it is the storage the two
blocked features were blocked on.

Deliberate properties:

  * **The variant, not the search, is the key.** Rows are grouped by the
    normalized query text, because "does this *shape* of query ever pay" is
    the question, and a per-search row cannot answer it.
  * **Zero is a recorded outcome.** A rung returning nothing is the single
    most useful row here, so it is written like any other rather than skipped.
  * **Never fails a search**, and reports what it wrote, so an empty table is
    distinguishable from a working one.
  * **Bounded retention** per variant.
"""

from __future__ import annotations

import os
import time
import uuid

from core import inkdrop_state


SAMPLE_RETENTION_PER_VARIANT = int(
    os.environ.get("INKDROP_QUERY_VARIANT_OUTCOME_RETENTION", 200) or 200
)


def enabled():
    value = str(
        os.environ.get("INKDROP_QUERY_VARIANT_OUTCOMES", "1") or ""
    ).strip().lower()
    return value not in {"0", "false", "off", "no", "disabled"}


def normalize_variant(query):
    """Group equivalent rungs together without pretending to parse them."""
    text = " ".join(str(query or "").split()).strip().lower()
    return text


def record_variant_outcomes(
    db_path,
    attempts,
    *,
    review_id=None,
    series=None,
    provider_id="slskd",
    now=None,
):
    """Persist one search's per-rung attempts. Returns rows actually written."""

    if not enabled():
        return {"ok": True, "skipped": "disabled", "recorded": 0}
    now = time.time() if now is None else float(now)
    rows = []
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        query = attempt.get("query")
        variant = normalize_variant(query)
        if not variant:
            continue
        # A rung that was skipped never asked the question, so it has no
        # outcome to record -- counting it as a zero would make a budget
        # exhaustion look like a barren query.
        if attempt.get("skipped"):
            continue
        def _int(value):
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0
        def _float(value):
            try:
                return max(0.0, float(value or 0.0))
            except (TypeError, ValueError):
                return 0.0
        rows.append(
            (
                uuid.uuid4().hex,
                variant,
                str(query or ""),
                str(provider_id or "").strip().lower(),
                str(review_id or ""),
                str(series or ""),
                _float(attempt.get("elapsed_seconds")),
                _int(attempt.get("response_count")),
                _int(attempt.get("candidate_count")),
                _int(attempt.get("auto_grab_safe_count")),
                now,
            )
        )
    if not rows:
        return {"ok": True, "skipped": "no_attempts", "recorded": 0}
    try:
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            con.executemany(
                """
                insert into query_variant_outcomes
                    (id, variant, query, provider_id, review_id, series,
                     elapsed_seconds, response_count, candidate_count,
                     auto_grab_safe_count, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            for variant in {row[1] for row in rows}:
                con.execute(
                    """
                    delete from query_variant_outcomes
                    where variant = ?
                      and id not in (
                          select id from query_variant_outcomes
                          where variant = ?
                          order by created_at desc limit ?
                      )
                    """,
                    (variant, variant, SAMPLE_RETENTION_PER_VARIANT),
                )
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}", "recorded": 0}
    return {"ok": True, "recorded": len(rows)}


def variant_yield(db_path, *, provider_id=None, min_samples=1, limit=500):
    """Per-variant yield, which is what expected-yield ordering needs.

    ``zero_rate`` is the fraction of tries that returned no candidate at all --
    the number durable retirement needs, and the one nothing could answer
    before this table existed.
    """

    if not enabled():
        return {"ok": True, "skipped": "disabled", "variants": []}
    params = []
    where = ""
    if provider_id:
        where = "where provider_id = ?"
        params.append(str(provider_id).strip().lower())
    params.append(int(limit or 500))
    try:
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            rows = con.execute(
                f"""
                select variant,
                       count(*) as tries,
                       sum(case when candidate_count > 0 then 1 else 0 end) as productive,
                       sum(candidate_count) as candidates,
                       sum(auto_grab_safe_count) as auto_grab_safe,
                       avg(elapsed_seconds) as avg_seconds,
                       max(created_at) as last_seen
                from query_variant_outcomes
                {where}
                group by variant
                order by tries desc
                limit ?
                """,
                params,
            ).fetchall()
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}", "variants": []}
    out = []
    for row in rows or []:
        tries = int(row[1] or 0)
        if tries < max(1, int(min_samples or 1)):
            continue
        productive = int(row[2] or 0)
        out.append(
            {
                "variant": row[0],
                "tries": tries,
                "productive_tries": productive,
                "zero_rate": round(1.0 - (productive / float(tries)), 4),
                "candidates": int(row[3] or 0),
                "auto_grab_safe": int(row[4] or 0),
                "avg_seconds": round(float(row[5] or 0.0), 3),
                "last_seen": float(row[6] or 0.0),
            }
        )
    return {"ok": True, "variants": out}
