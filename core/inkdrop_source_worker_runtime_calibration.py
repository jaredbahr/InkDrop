"""Measured per-request runtime cost for source-worker admission.

The runtime-budget admission check prices an HTTP provider plan at
``fixed + request_count * seconds_per_request``, and ``seconds_per_request``
has always been the *worst case a request could cost before being killed*:
``max(12, source_http_timeout + 5)``, which is 20s at the production 15s
timeout. That is a true upper bound -- a request cannot outlive its own
timeout -- but on this deployment it is one to two orders of magnitude above
what requests actually cost, and the gap is not academic:

  * Suwayomi is on loopback. Measured 2026-08-15 from inside the container:
    0.03-0.85s per request. A volume-pack plan carries ``request_count=66``,
    so it is priced at 8 + 66*20 = 1328s against a 600s pass with ~570s
    usable after the cleanup reserve. Its estimate exceeds the entire pass
    budget, so no admission order can ever seat it -- confirmed by
    ``_source_floor_head_plan_ids``' own docstring, which names "a cheaper
    per-item estimate" as the only remaining lever.
  * MangaDex measured 0.15-0.55s per request against the same 20s price.

Live effect at production settings (``--eligible-limit 10 --queue-limit 50
--max-run-seconds 600``, measured 2026-08-15): 51 plans scanned, all 51
``eligible`` -- nothing blocked -- window of 10, of which the runtime budget
skipped 7, leaving ~3 to actually run. 880 distinct queue rows collected a
runtime-budget skip in 24h.

So this module replaces the worst-case price with an observed one. Each
executed provider run records its real elapsed-per-request cost; admission
then prices a provider at a high percentile of its recent samples.

Deliberate properties, because this loosens an admission gate:

  * **Calibration can only lower an estimate, never raise it.** The
    timeout-derived value is a genuine ceiling, so ``min()`` against it is
    principled rather than arbitrary, and a provider that degrades can at
    worst return to today's behaviour.
  * **p90, not mean**, times a safety factor, with a per-request floor -- a
    provider is priced by its slow requests, not its typical ones.
  * **A minimum sample count and a TTL.** Too few or too old observations
    fall straight back to the worst-case price, so a cold start and a
    provider that has not run recently both behave exactly as they do today.
  * **Truncated runs are recorded but excluded from the percentile.** A run
    stopped by ``fetch_deadline`` only proves a lower bound on its cost;
    averaging it in would bias the estimate downward, which is the one
    direction that matters.
  * **Every failure path falls back to the caller's default.** A missing
    table, a locked database, or a malformed row must never be able to stop
    a pass from scheduling.

Under-pricing is also the recoverable direction here: an admitted plan that
turns out slower than estimated is bounded by ``fetch_deadline`` and persists
partial evidence, whereas an over-priced plan is never admitted at all.
"""

from __future__ import annotations

import os
import time
import uuid

from core import inkdrop_state


# Trust a provider's measurements only once it has this many usable samples.
MIN_SAMPLES = 5
# ...unless the very first measurement contradicts the worst-case price by this
# much. A single observation is weak evidence of what a request *precisely*
# costs and overwhelming evidence that 20s is not it: measured live 2026-08-16,
# Suwayomi ran 66 requests in 11.90s -- 0.180s each, 111x under its price -- and
# that one number was enough to know the price was wrong, while five of them
# were unreachable. The probe admits at most one over-budget plan per provider
# per pass and the source worker runs 96 passes a day, so waiting for five
# samples inside a 24h TTL is a race the provider that most needs calibrating
# is the least likely to win.
#
# Keeping MIN_SAMPLES for everything else is the point: where the measurement
# and the default are close, noise matters and a single sample should not move
# the price. Where they differ by an order of magnitude, noise cannot explain
# the gap.
DECISIVE_SAMPLE_RATIO = float(
    os.environ.get("INKDROP_SOURCE_WORKER_CALIBRATION_DECISIVE_RATIO", 10.0) or 10.0
)
# Never act on zero samples -- that is the cold start, and it must keep today's
# behaviour exactly.
DECISIVE_MIN_SAMPLES = 1
# Observations older than this are ignored, so a provider that slows down is
# re-priced within a pass or two rather than coasting on stale speed.
SAMPLE_TTL_SECONDS = 24 * 60 * 60
# Never price a request below this, however fast it has been measured.
MIN_SECONDS_PER_REQUEST = 0.5
# Headroom multiplier applied on top of the p90.
SAFETY_FACTOR = 1.5
# Bound the per-provider sample window read at admission time.
SAMPLE_READ_LIMIT = 200
# Keep the table small; samples beyond this per provider are pruned on write.
SAMPLE_RETENTION_PER_PROVIDER = 400


def enabled():
    value = str(
        os.environ.get("INKDROP_SOURCE_WORKER_RUNTIME_CALIBRATION", "1") or ""
    ).strip().lower()
    return value not in {"0", "false", "off", "no", "disabled"}


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = int(len(ordered) * fraction)
    return ordered[min(index, len(ordered) - 1)]


def record_observation(
    db_path,
    provider_ids,
    *,
    request_count,
    elapsed_seconds,
    truncated=False,
    now=None,
):
    """Attribute one executed run's elapsed time across the providers it ran.

    The even split is a deliberate approximation for *pricing*, not a claim
    that per-provider time is unobservable. It errs high for the cheap
    providers in a mixed run rather than low for the expensive one, which is
    the safe direction for an admission gate.

    It used to be the only reading available -- there was no per-provider
    timer anywhere. There is one now:
    ``inkdrop_source_worker_phase_timing`` records real per-provider elapsed
    time from ``run_source_jobs``' own loop, alongside the per-phase split of
    an item. Admission still prices off this even split, because changing what
    the gate reads is a behavioural change and this is a measurement change;
    the phase table is the place to look before making that call.
    """

    if not enabled():
        return {"ok": True, "skipped": "disabled"}
    providers = [
        str(value or "").strip().lower()
        for value in (provider_ids or [])
        if str(value or "").strip()
    ]
    if not providers:
        return {"ok": True, "skipped": "no_providers"}
    try:
        elapsed = float(elapsed_seconds)
    except (TypeError, ValueError):
        return {"ok": True, "skipped": "no_elapsed"}
    if elapsed < 0:
        return {"ok": True, "skipped": "no_elapsed"}
    try:
        requests = max(1, int(request_count or 0))
    except (TypeError, ValueError):
        requests = 1
    now = time.time() if now is None else float(now)
    share = elapsed / float(len(providers))
    rows = [
        (
            uuid.uuid4().hex,
            provider_id,
            requests,
            round(share, 4),
            round(share / float(requests), 6),
            1 if truncated else 0,
            now,
        )
        for provider_id in providers
    ]
    try:
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            con.executemany(
                """
                insert into source_worker_runtime_samples
                    (id, provider_id, request_count, elapsed_seconds,
                     seconds_per_request, truncated, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            for provider_id in set(providers):
                con.execute(
                    """
                    delete from source_worker_runtime_samples
                    where provider_id = ?
                      and id not in (
                          select id from source_worker_runtime_samples
                          where provider_id = ?
                          order by created_at desc limit ?
                      )
                    """,
                    (provider_id, provider_id, SAMPLE_RETENTION_PER_PROVIDER),
                )
    except Exception as exc:
        # Telemetry must never be able to fail a pass.
        return {"ok": False, "reason": f"{type(exc).__name__}"}
    return {"ok": True, "recorded": len(rows)}


def _priced(values):
    """Price a provider from its samples: p90, plus headroom, above the floor."""
    p90 = _percentile(values, 0.9)
    if p90 is None:
        return None
    return max(MIN_SECONDS_PER_REQUEST, p90 * SAFETY_FACTOR)


def provider_request_seconds(db_path, *, now=None, worst_case_seconds=None):
    """Measured seconds-per-request per provider, for providers with enough data.

    Returns ``{provider_id: seconds}``. Providers absent from the mapping have
    no trustworthy measurement and must keep the caller's worst-case default.

    ``worst_case_seconds`` is the price a provider would otherwise carry. Pass
    it to enable the decisive-sample rule: below ``MIN_SAMPLES`` a provider is
    admitted only when its measurement is cheaper than the worst case by
    ``DECISIVE_SAMPLE_RATIO``. Omit it and the function behaves exactly as it
    did before -- ``MIN_SAMPLES`` for everyone.

    This never raises a price. The caller still clamps with ``min()`` against
    the worst case (see ``_calibrated_request_seconds``), which is what makes
    trusting a thin sample safe: the failure mode is admitting a plan that then
    truncates, and a truncated run's sample is already kept out of the
    percentile.
    """

    if not enabled():
        return {}
    now = time.time() if now is None else float(now)
    cutoff = now - SAMPLE_TTL_SECONDS
    try:
        with inkdrop_state.connect_read(db_path) as con:
            rows = con.execute(
                """
                select provider_id, seconds_per_request
                from source_worker_runtime_samples
                where created_at >= ? and truncated = 0
                order by created_at desc
                """,
                (cutoff,),
            ).fetchall()
    except Exception:
        return {}
    by_provider = {}
    for row in rows:
        try:
            provider_id = str(row["provider_id"] or "").strip().lower()
            seconds = float(row["seconds_per_request"])
        except Exception:
            continue
        if not provider_id or seconds < 0:
            continue
        bucket = by_provider.setdefault(provider_id, [])
        if len(bucket) < SAMPLE_READ_LIMIT:
            bucket.append(seconds)
    try:
        worst_case = float(worst_case_seconds) if worst_case_seconds is not None else None
    except (TypeError, ValueError):
        worst_case = None
    calibrated = {}
    for provider_id, values in by_provider.items():
        price = _priced(values)
        if price is None:
            continue
        if len(values) >= MIN_SAMPLES:
            calibrated[provider_id] = price
            continue
        if worst_case is None or worst_case <= 0:
            continue
        if len(values) < DECISIVE_MIN_SAMPLES:
            continue
        # Decisive only when the gap is too large for noise to explain. A
        # provider measuring near its worst case still waits for MIN_SAMPLES.
        if price * DECISIVE_SAMPLE_RATIO <= worst_case:
            calibrated[provider_id] = price
    return calibrated
