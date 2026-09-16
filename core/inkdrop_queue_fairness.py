"""Queue ordering fairness: bounded ageing, a fast lane for new work, lane quotas.

Row selection used to sort primarily on `series_queue_round`, a
`row_number() partition by series_id`. With 209 series holding queued rows and
a 50-row scan, the round-1 rows alone overflow the scan, so rows deeper in a
series' backlog were only ever reached once everything ahead of them had been
served recently enough to fall behind them. Service degraded monotonically with
round -- measured live 2026-08-15 over 2,172 schedulable rows: round 1 served
73.2% in seven days, round 21+ served 29.0%, with 837 rows sitting at round 21+
and 55.6% of the whole backlog getting no real attempt at all in a week.

Ageing pressure is the fix, but ageing alone is the wrong fix. The rows that age
the most are, by selection, the ones we keep failing to find: of the 837 rows at
round 21+, 486 have only ever searched empty. Ordering purely by age hands the
most capacity to the items least likely to ever succeed and puts the newest,
most findable content behind them. So ageing here is deliberately bounded on two
axes at once -- it saturates with time, and it decays with attempts -- and it
competes for a capped share of each pass rather than for the pass itself.

Three lanes share every pass:

  fast    new work: a newly added series, or a newly released issue, that has
          not been tried yet. Pre-empts everything, self-empties (membership
          requires a low real-attempt count), and is idle on most passes.
  steady  least recently served, rotating series within a day band, guaranteed
          at least half the pass so neither of the other lanes can take it
          over.
  aged    rows ranked by bounded ageing score, capped so old-and-failing work
          can never consume the whole pass.

The steady lane was itself the per-series round-robin described above until it
was found to have no convergence at all. Ordering on `series_queue_round` made
a deep series surface exactly one row per pass however long its others had
waited, and the tiebreak underneath it read `queue_items.updated_at`, which
bookkeeping refreshes -- so nothing in the lane was a clock the pass's own work
moved, and every pass re-offered its own head. It now leads on whole days since
a real search, descending, with the per-series round as the tiebreak INSIDE a
band. That keeps the interleaving this module exists for (a single deep backlog
still cannot take the lane) while making any prefix convergent: a served row
drops to band 0, so the unserved set only shrinks between passes.

Two things the sweep key deliberately does not do, each learned from a guard
that went red:

  * It does not rank never-searched rows as their own leading cohort. Doing so
    puts brand-new rows at the head of the steady lane, which makes the fast
    lane's quota redundant -- and the new-content guard proves its own
    relevance by removing that quota and requiring starvation to reappear.
    New content is the fast lane's job.
  * It does not rank rows in states nothing searches. A blocked or superseded
    row's stall only ever grows, and it can never be served, so it can never
    drop a band and make way: under a bare age key it does not merely rank
    high, it holds the head of the lane forever. That is the failure the next
    paragraph describes, in its sharpest form, so the lane applies the same
    state gate the other two already apply to their scores.

Every constant below is read off the live distribution rather than picked round;
the justification is in the comment beside it.
"""

import os


def _env_float(name, default):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)
    return value


def _env_int(name, default):
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)
    return value


DAY_SECONDS = 86400.0

# Ageing saturates at the p90 of the live stall distribution (measured 21.1
# days over 2,172 schedulable rows; p95 32.3, max 42.8). Past this point extra
# waiting buys no extra priority, so no row can climb indefinitely. At p90 only
# ~10.7% of the backlog sits at full pressure at any moment, comfortably under
# the aged lane's share -- the saturated cohort cannot outnumber the capacity
# reserved for it, let alone the pass.
AGE_SATURATION_DAYS = _env_float("INKDROP_QUEUE_AGE_SATURATION_DAYS", 21.0)

# Ageing credit is undiminished up to the p90 of real attempts per row
# (measured 52; median 12, p75 31, p95 69, p99 151). A row that has been tried
# an ordinary number of times ages at full strength. Past p90 the credit decays
# linearly and reaches exactly zero at the retry ceiling, where PR #715's
# retire_retry_exhausted_queue_items() moves the row to needs_you. One axis,
# two effects: a graded loss of priority on the way to a terminal state that
# already exists, rather than a second competing ceiling.
AGE_DECAY_START_ATTEMPTS = _env_int("INKDROP_QUEUE_AGE_DECAY_START_ATTEMPTS", 52)

# A series counts as new for this long after it first appears. 14 days is well
# clear of the observed time-to-first-attempt tail (p75 195h, p90 340h) that
# this lane exists to close.
NEW_SERIES_DAYS = _env_float("INKDROP_QUEUE_NEW_SERIES_DAYS", 14.0)

# A row does not compete for reserved aged capacity until it has waited longer
# than the ordinary rotation normally makes it wait. Seven days is the same
# line the defect was measured on -- 55.6% of the live backlog had no real
# attempt in a week -- so the lane always has genuine candidates, and never
# spends its share on something that ran an hour ago. Without this floor any
# non-zero stall qualifies and the reservation stops meaning anything.
AGED_LANE_MIN_STALL_DAYS = _env_float("INKDROP_QUEUE_AGED_LANE_MIN_STALL_DAYS", 7.0)

# A newly published issue for a series we already hold is new work too.
NEW_ISSUE_DAYS = _env_float("INKDROP_QUEUE_NEW_ISSUE_DAYS", 14.0)

# New work leaves the fast lane after this many real attempts. Without a budget
# a new row that simply cannot be found would squat the lane; with one, the
# lane is guaranteed to drain and the row rejoins the ordinary rotation.
FAST_LANE_ATTEMPT_BUDGET = _env_int("INKDROP_QUEUE_FAST_LANE_ATTEMPT_BUDGET", 3)

# Shares of each pass. Steady keeps whatever is left and is floored below, so
# these two are ceilings on the other lanes rather than fixed allocations.
FAST_LANE_SHARE = _env_float("INKDROP_QUEUE_FAST_LANE_SHARE", 0.25)
AGED_LANE_SHARE = _env_float("INKDROP_QUEUE_AGED_LANE_SHARE", 0.25)

# The floor that makes constraint 1 structural rather than a hope: however old
# the backlog gets, at least half of every pass keeps running the ordinary
# rotation, which is what serves current content.
STEADY_LANE_MIN_SHARE = _env_float("INKDROP_QUEUE_STEADY_LANE_MIN_SHARE", 0.5)

LANE_FAST = "fast"
LANE_STEADY = "steady"
LANE_AGED = "aged"

LANE_FIELD = "source_worker_queue_lane"
LANE_RANK_FIELD = "source_worker_queue_lane_rank"
AGEING_SCORE_FIELD = "source_worker_ageing_score"
STALL_SECONDS_FIELD = "source_worker_stall_seconds"
REAL_ATTEMPT_FIELD = "source_worker_real_attempt_count"


def retry_ceiling_real_attempts():
    """The terminal ceiling from PR #715, read lazily to avoid an import cycle."""
    try:
        from core import inkdrop_state
    except ImportError:  # pragma: no cover - standalone execution
        import inkdrop_state
    ceiling = int(getattr(inkdrop_state, "RETRY_CEILING_REAL_ATTEMPTS", 150) or 0)
    return ceiling if ceiling > 0 else 150


def age_pressure(stall_seconds):
    """Bounded 0..1 ramp on how long a row has waited.

    Linear to the saturation point and flat after it, so waiting longer than
    the saturation window is worth exactly as much as waiting for it -- which
    is what stops any single row from climbing forever.
    """
    try:
        stall = float(stall_seconds or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if stall <= 0:
        return 0.0
    window = max(1.0, AGE_SATURATION_DAYS * DAY_SECONDS)
    return min(1.0, stall / window)


def attempt_decay(real_attempts, ceiling=None):
    """Bounded 1..0 taper on how many genuine attempts a row has already had.

    Full strength up to AGE_DECAY_START_ATTEMPTS, then linear to zero at the
    retry ceiling. Keyed on real attempts only -- the source_attempts row count
    runs 5.3x higher at the median because that table is a lifecycle ledger,
    and a taper read off it would penalise healthy rows for bookkeeping.
    """
    try:
        attempts = int(real_attempts or 0)
    except (TypeError, ValueError):
        attempts = 0
    ceiling = int(ceiling or retry_ceiling_real_attempts())
    start = int(AGE_DECAY_START_ATTEMPTS)
    if attempts <= start:
        return 1.0
    if ceiling <= start or attempts >= ceiling:
        return 0.0
    return max(0.0, min(1.0, float(ceiling - attempts) / float(ceiling - start)))


def ageing_score(stall_seconds, real_attempts, ceiling=None):
    """How hard a row presses for an aged-lane slot, in 0..1.

    Zero means it does not compete for the aged lane at all; it still competes
    in the ordinary rotation, which is the point of keeping the taper graded
    rather than making it another cutoff.
    """
    try:
        stall = float(stall_seconds or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if stall < AGED_LANE_MIN_STALL_DAYS * DAY_SECONDS:
        return 0.0
    return age_pressure(stall) * attempt_decay(real_attempts, ceiling=ceiling)


def lane_quotas(seats):
    """Split a pass of `seats` rows into per-lane ceilings.

    Steady is floored first and the other two divide what is left, so the
    guarantee that ordinary rotation keeps at least half the pass holds at every
    pass size rather than only at the one this was tuned against.
    """
    try:
        seats = int(seats)
    except (TypeError, ValueError):
        seats = 0
    if seats <= 0:
        return {LANE_FAST: 0, LANE_STEADY: 0, LANE_AGED: 0}
    if seats == 1:
        return {LANE_FAST: 1, LANE_STEADY: 1, LANE_AGED: 0}
    steady_min = max(1, int(seats * STEADY_LANE_MIN_SHARE))
    spare = max(0, seats - steady_min)
    fast = min(spare, max(1, int(round(seats * FAST_LANE_SHARE))))
    aged = min(max(0, spare - fast), max(1, int(round(seats * AGED_LANE_SHARE))))
    return {LANE_FAST: fast, LANE_STEADY: seats - fast - aged, LANE_AGED: aged}
