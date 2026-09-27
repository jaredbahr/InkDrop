"""Read-only queue scheduler prep for settings-backed InkDrop source workers.

This module decides which queue rows are ready for the source worker without
running providers, writing source attempts, staging files, or mutating state.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid

from core import inkdrop_queue_fairness
from core import inkdrop_source_registry
from core import inkdrop_source_worker_coordinator as coordinator
from core import inkdrop_sources
from core import inkdrop_state


@contextlib.contextmanager
def _borrowed_or_read_con(db_path, con=None):
    """Reuse an already-open read connection, or open one for this call."""

    if con is not None:
        yield con
    else:
        with inkdrop_state.connect_read(db_path) as opened:
            yield opened


CONTRACT_VERSION = 1

# Ordinary provider acquisition is only for a Wanted item that still wants
# something. Once it's satisfied/ignored/suppressed/superseded, or the linked
# row is gone, spending scheduler slots and provider calls on its queue row
# just risks a duplicate download. Reuses inkdrop_state's own retirement
# vocabulary instead of inventing a second list that can drift from it.
WANTED_TERMINAL_STATUSES = inkdrop_state.WANTED_RETIRED_STATUSES

ACTIVE_HANDOFF_STATES = {"queued", "downloading", "import_ready", "importing"}
DEFAULT_ACTIVE_HANDOFF_STALE_SECONDS = 12 * 60 * 60
RETRYABLE_FAILED_HANDOFF_RECOVERY_SECONDS = 24 * 60 * 60
DOWNLOAD_CLIENT_HANDOFF_CLIENTS = {"qbittorrent", "qbit", "sabnzbd", "sab"}
STAGEABLE_HANDOFF_CLIENTS = {"inkdrop_direct", "inkdrop_page_pack"}

TERMINAL_OR_PROBLEM_HANDOFF_STATUSES = {
    "bad_archive",
    "download_api_error",
    "error",
    "failed",
    "failed_download",
    "preview_not_importable",
    # A waiting SLSKD candidate whose staged file never matched -- see
    # inkdrop_state.DOWNLOAD_TASK_DUPLICATE_CANDIDATE_MISMATCH_STATUSES,
    # which already treats this exact status as "a real record of a real
    # attempt that never left InkDrop" for duplicate-row cleanup. It was
    # absent here, so a completed row in this shape (state='queued', a
    # legitimately-active handoff state) never stopped counting as an
    # active handoff -- confirmed live 2026-08-18 for
    # The Wicked + The Divine: 1831 #1, blocking both the autopilot due-
    # series path and this module's own _classify_queue_plan() with no
    # claim, no client, nothing left to wait for.
    "staged_filename_mismatch",
    "staged_file_missing_path",
    "stale_no_local_file",
    "superseded_active_candidate",
    "superseded_duplicate",
    "transfer_failed",
    "transfer_stale_unknown",
}

RETRYABLE_FAILED_HANDOFF_STATUSES = {
    "client_unavailable",
    "download_api_error",
    "download_preflight_api_error",
    "error",
    "failed",
    "failed_download",
    "provider_unavailable",
    "provider_wait",
}

RETRYABLE_FAILED_STAGE_ATTEMPT_STATUSES = RETRYABLE_FAILED_HANDOFF_STATUSES | {
    "retry_scheduled",
    "transfer_stale_unknown",
}

BLOCKED_JOB_STATUSES = {
    "blocked",
    "configuration_required",
    "unsupported_adapter",
    "not_executable",
}

PROVIDER_TIMEOUT_CIRCUIT_KIND = "provider_timeout_circuit"
PROVIDER_FETCH_FAILURE_CIRCUIT_KIND = "provider_fetch_failure_circuit"

PROVIDER_TIMEOUT_RECOVERY_STATES = {
    "available",
    "healthy",
    "ok",
    "ready",
    "reachable",
    "running",
}

TIMEOUT_JSON_FLAG_KEYS = {
    "command_timed_out",
    "timed_out",
    "command_timeout",
    "failed_retry_command_timeout",
    "prowlarr_command_timeout",
    "prowlarr_search_timeout",
    "search_budget_exhausted",
    "source_started_timeout",
    "timeoutexpired",
}

TIMEOUT_JSON_TEXT_KEYS = {
    "detail",
    "display_phase",
    "error",
    "errors",
    "exception",
    "exceptions",
    "failure_reason",
    "message",
    "messages",
    "outcome",
    "partial_errors",
    "reason",
    "status",
}

FALSE_JSON_FLAG_TEXT = {"", "0", "false", "no", "none", "null", "off", "ok"}
TRUE_JSON_FLAG_TEXT = {"1", "on", "true", "yes"}

FETCH_FAILURE_CIRCUIT_REASONS = {
    "external_tool_failed",
    "http_request_failed",
}

FETCH_FAILURE_CIRCUIT_STATUSES = {
    "provider_unavailable",
    "provider_wait",
}

NON_TERMINAL_SOURCE_ATTEMPT_STATUSES = {
    "activity",
    "available",
    "observed",
    "queued",
    "searching",
}

NON_TERMINAL_SOURCE_ATTEMPT_PHASES = {
    "observed",
    "queued",
    "searching",
}

NON_TERMINAL_SOURCE_ATTEMPT_OUTCOMES = {
    "",
    "in_progress",
    "neutral",
    "waiting",
}

NON_ATTEMPT_SOURCE_ATTEMPT_KINDS = {
    "source_runtime_budget_skipped",
    # Routine queue-sync bookkeeping (inkdrop_state.queue_activity_attempt()), not a
    # real provider call -- it inherits a real provider name via current_source, so
    # without this exclusion it creates a false provider cooldown and inflates
    # attempt/terminal-attempt history counts on every queue-sync pass.
    "queue_activity",
}

NON_ATTEMPT_SOURCE_ATTEMPT_REASON_PREFIXES = (
    "autopilot runtime budget reached",
    "runtime budget has",
)

NON_ATTEMPT_SOURCE_ATTEMPT_REASON_TEXT = (
    "did not start before the worker runtime budget",
)

MANGA_MEDIA_TYPES = {"manga", "manhwa", "manhua"}
MANGA_SCOPED_PROVIDER_IDS = {
    "mangadex",
    "prowlarr_nyaa",
    "prowlarr_tokyo_toshokan_manga",
    "suwayomi",
}
COMIC_PACK_SCOPED_PROVIDER_IDS = {
    "prowlarr_dognzb_comics",
    "prowlarr_kat_comics",
    "prowlarr_pirate_bay_comics",
    "prowlarr_torrentdownload_comics",
    "prowlarr_torrentleech_comics",
}


def _dict(value):
    return dict(value) if isinstance(value, dict) else {}


def _json_loads(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _float(value):
    try:
        return float(value)
    except Exception:
        return None


def _lower(value):
    return str(value or "").strip().lower()


def _bounded_limit(limit, default=50, maximum=500):
    try:
        value = int(limit)
    except Exception:
        value = default
    return max(1, min(value, maximum))


def _provider_scope_media_types(provider_ids):
    provider_ids = {
        _lower(value)
        for value in _list(provider_ids)
        if _lower(value)
    }
    if not provider_ids:
        return []
    if provider_ids.issubset(MANGA_SCOPED_PROVIDER_IDS):
        return sorted(MANGA_MEDIA_TYPES)
    return []


def _provider_scope_excluded_media_types(provider_ids):
    provider_ids = {
        _lower(value)
        for value in _list(provider_ids)
        if _lower(value)
    }
    if not provider_ids:
        return []
    if provider_ids.issubset(COMIC_PACK_SCOPED_PROVIDER_IDS):
        return sorted(MANGA_MEDIA_TYPES)
    return []


# Only a row that can still be acquired may claim a reserved lane slot. A
# blocked or superseded row would spend the reservation and then be filtered
# out downstream, which is the reservation failing quietly.
LANE_ELIGIBLE_STATES = ("queued", "searching")
LANE_ELIGIBLE_STATES_SQL = "(%s)" % ",".join("'%s'" % state for state in LANE_ELIGIBLE_STATES)


def _utc_date(epoch_seconds):
    """UTC calendar date for an epoch stamp, safe below the epoch."""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(max(0.0, float(epoch_seconds or 0.0))))
    except (OSError, OverflowError, ValueError):
        return "1970-01-01"


def schedulable_queue_clauses(
    *,
    queue_ids=None,
    states=None,
    media_types=None,
    excluded_media_types=None,
    due_only=False,
    now=None,
):
    """The WHERE clauses that define "a row the source worker may schedule".

    Extracted from _queue_rows() so the shipping scan and anything that
    measures it answer "which rows are in the population" from one place, the
    way real_attempt_predicate_sql() is the one authority for "a real attempt".
    A coverage figure computed over a hand-written population is a figure about
    that population, not about what the worker actually has to get through; the
    instrument in core/inkdrop_search_coverage.py composes this instead.

    Returns (clauses, params). The caller joins the clauses with " and " and
    binds the params once per interpolation -- _queue_rows() interpolates the
    set twice and therefore binds it twice, in that order.

    The aliases the clauses assume are `q` (queue_items), `s` (series) and `w`
    (wanted_items); `due_only` additionally reads download_tasks through a
    correlated EXISTS, which needs no alias from the caller.
    """
    now = time.time() if now is None else now
    queue_ids = [str(value).strip() for value in _list(queue_ids) if str(value or "").strip()]
    states = [str(value).strip() for value in _list(states) if str(value or "").strip()]
    terminal_wanted_statuses = sorted(WANTED_TERMINAL_STATUSES)
    clauses = [
        "q.active=1",
        "(q.wanted_id is null or (w.id is not null and lower(coalesce(w.status,'')) not in (%s)))"
        % ",".join("?" for _ in terminal_wanted_statuses),
    ]
    params = list(terminal_wanted_statuses)
    if queue_ids:
        clauses.append("q.id in (%s)" % ",".join("?" for _ in queue_ids))
        params.extend(queue_ids)
    if states:
        clauses.append("q.state in (%s)" % ",".join("?" for _ in states))
        params.extend(states)
    media_types = [_lower(value) for value in _list(media_types) if _lower(value)]
    if media_types:
        clauses.append("lower(coalesce(s.media_type,'')) in (%s)" % ",".join("?" for _ in media_types))
        params.extend(media_types)
    excluded_media_types = [_lower(value) for value in _list(excluded_media_types) if _lower(value)]
    if excluded_media_types:
        clauses.append("lower(coalesce(s.media_type,'')) not in (%s)" % ",".join("?" for _ in excluded_media_types))
        params.extend(excluded_media_types)
    if due_only:
        retryable_statuses = sorted(RETRYABLE_FAILED_HANDOFF_STATUSES)
        retryable_clients = sorted(DOWNLOAD_CLIENT_HANDOFF_CLIENTS)
        clauses.append(
            f"""(
                q.retry_after is null
                or q.retry_after<=?
                or exists (
                    select 1
                    from download_tasks dt
                    where dt.queue_id=q.id
                      and coalesce(dt.retry_eligible, 0)=1
                      and lower(coalesce(dt.status,'')) in ({','.join('?' for _ in retryable_statuses)})
                      and lower(coalesce(dt.download_client,'')) in ({','.join('?' for _ in retryable_clients)})
                      and coalesce(dt.updated_at, dt.completed_at, dt.started_at, 0)>=?
                )
            )"""
        )
        params.append(now)
        params.extend(retryable_statuses)
        params.extend(retryable_clients)
        params.append(now - RETRYABLE_FAILED_HANDOFF_RECOVERY_SECONDS)
    return clauses, params


def _queue_rows(
    db_path,
    *,
    limit=50,
    queue_ids=None,
    states=None,
    media_types=None,
    excluded_media_types=None,
    due_only=False,
    now=None,
    con=None,
):
    now = time.time() if now is None else now
    clauses, params = schedulable_queue_clauses(
        queue_ids=queue_ids,
        states=states,
        media_types=media_types,
        excluded_media_types=excluded_media_types,
        due_only=due_only,
        now=now,
    )
    # Normalised the same way the clause builder does it, because the reserve
    # below gates on "no explicit id filter" and a caller passing whitespace
    # means the same thing as a caller passing nothing.
    queue_ids = [str(value).strip() for value in _list(queue_ids) if str(value or "").strip()]
    bounded_limit = _bounded_limit(limit)
    lane_quotas = inkdrop_queue_fairness.lane_quotas(bounded_limit)
    fast_quota = max(1, int(lane_quotas[inkdrop_queue_fairness.LANE_FAST]))
    aged_quota = max(1, int(lane_quotas[inkdrop_queue_fairness.LANE_AGED]))
    steady_quota = max(1, int(lane_quotas[inkdrop_queue_fairness.LANE_STEADY]))
    age_window_seconds = max(
        1.0, float(inkdrop_queue_fairness.AGE_SATURATION_DAYS) * inkdrop_queue_fairness.DAY_SECONDS
    )
    aged_min_stall_seconds = max(
        0.0, float(inkdrop_queue_fairness.AGED_LANE_MIN_STALL_DAYS) * inkdrop_queue_fairness.DAY_SECONDS
    )
    decay_start = int(inkdrop_queue_fairness.AGE_DECAY_START_ATTEMPTS)
    retry_ceiling = int(inkdrop_queue_fairness.retry_ceiling_real_attempts())
    if retry_ceiling <= decay_start:
        retry_ceiling = decay_start + 1
    fast_attempt_budget = int(inkdrop_queue_fairness.FAST_LANE_ATTEMPT_BUDGET)
    new_series_cutoff = float(now) - float(inkdrop_queue_fairness.NEW_SERIES_DAYS) * inkdrop_queue_fairness.DAY_SECONDS
    # Fixtures and replays run with small synthetic clocks, where subtracting
    # the window lands before the epoch and gmtime raises rather than clamping.
    new_issue_cutoff_date = _utc_date(
        float(now) - float(inkdrop_queue_fairness.NEW_ISSUE_DAYS) * inkdrop_queue_fairness.DAY_SECONDS
    )
    today_date = _utc_date(float(now))
    # Both facts the ordering needs from the ledger -- how many genuine
    # attempts a row has made and when the last one was -- come from one
    # grouped pass restricted to the schedulable set. As two correlated
    # subqueries the same answers cost 2.2s against the live database; grouped,
    # 1.4s, because each row's attempt history is walked once instead of twice.
    real_attempts_cte = inkdrop_state.real_attempts_by_queue_cte_sql(
        "sa", "join schedulable_queue sq on sq.id = sa.queue_id"
    )
    ordered_cte = f"""
        with series_initial_search_priority as (
        select series_id, max(created_at) as series_initial_search_priority_at,
               min(created_at) as series_first_added_at
        from history_events
        where lower(event_type)='series_added'
        group by series_id
        ), series_latest_source_attempt as (
        select series_id, max(coalesce(completed_at, started_at, 0)) as series_latest_source_attempt_at
        from source_attempts
        group by series_id
        ), schedulable_queue as (
        select q.id from queue_items q
        left join series s on s.id=q.series_id
        left join wanted_items w on w.id=q.wanted_id
        where {" and ".join(clauses)}
        ), {real_attempts_cte}, series_real_service as (
        -- When a series was last genuinely searched, over the rows that can
        -- be scheduled now. Real attempts only: MAD's import-retry loop
        -- writes download_client/importer rows every hour, and a clock read
        -- off those would mark the series as freshly served while nothing
        -- had searched it.
        select coalesce(nullif(trim(q.series_id), ''), q.id) as series_key,
               max(ra.last_real_attempt_at) as series_last_real_attempt_at
        from real_attempts ra
        join queue_items q on q.id = ra.queue_id
        group by coalesce(nullif(trim(q.series_id), ''), q.id)
        ), ranked_queue as (
        select q.id, q.wanted_id, q.series_id, q.issue_id, q.state,
               q.current_source, q.query, q.last_event, q.active,
               q.created_at, q.updated_at, q.retry_after, q.retry_after_iso,
               q.display_phase, q.outcome, q.provider_status_state,
               q.provider_status_phase, q.provider_status_provider,
               q.provider_status_actionability, q.raw_json,
               s.title as series, s.media_type, s.year, s.publisher,
               s.metadata_provider, s.metadata_id, s.kapowarr_id,
               i.issue_number, i.title as issue_title, i.release_date as issue_release_date,
               i.metadata_provider as issue_metadata_provider,
               i.metadata_id as issue_metadata_id,
               i.kapowarr_issue_id,
               w.status as wanted_status, w.priority as wanted_priority,
               coalesce(sisp.series_initial_search_priority_at, s.created_at)
                   as series_initial_search_priority_at,
               slsa.series_latest_source_attempt_at,
               -- Earliest evidence the series exists at all. The series_added
               -- history row is pruned at 30 days by diagnostic retention
               -- and is rewritten on re-add, so it cannot carry this on
               -- its own; series.created_at is durable and never moves back.
               case
                 when sisp.series_first_added_at is null then s.created_at
                 when s.created_at is null then sisp.series_first_added_at
                 else min(sisp.series_first_added_at, s.created_at)
               end as series_first_seen_at,
               coalesce(ra.real_attempt_count, 0) as real_attempt_count,
               coalesce(ra.last_real_attempt_at, 0) as last_real_attempt_at,
               coalesce(srs.series_last_real_attempt_at, 0) as series_last_real_attempt_at,
               coalesce(nullif(trim(q.series_id), ''), q.id) as series_key
               , row_number() over (
                   partition by coalesce(nullif(trim(q.series_id), ''), q.id)
                   order by
                     coalesce(q.retry_after, q.updated_at, q.created_at, 0) asc,
                     coalesce(q.updated_at, q.created_at, 0) asc,
                     q.id asc
               ) as series_queue_round
        from queue_items q
        left join series s on s.id=q.series_id
        left join issues i on i.id=q.issue_id
        left join wanted_items w on w.id=q.wanted_id
        left join series_initial_search_priority sisp on sisp.series_id=q.series_id
        left join series_latest_source_attempt slsa on slsa.series_id=q.series_id
        left join real_attempts ra on ra.queue_id=q.id
        left join series_real_service srs
               on srs.series_key = coalesce(nullif(trim(q.series_id), ''), q.id)
        where {" and ".join(clauses)}
        ), stalled_queue as (
        select *,
               -- Never queue_items.updated_at: bookkeeping refreshes it, so a
               -- stall measured on it reports every row as recently active.
               -- The download-task fallback is only reached by rows that have
               -- never had a real attempt (222 of 2,172 live), so it sits
               -- inside the CASE rather than in a coalesce that would evaluate
               -- it for everything.
               max(0.0, {float(now)!r} - case
                 when coalesce(last_real_attempt_at, 0) > 0 then last_real_attempt_at
                 else coalesce(
                   nullif((select min(coalesce(dt.completed_at, dt.started_at, 0))
                             from download_tasks dt
                            where dt.queue_id=rq.id
                              and coalesce(dt.completed_at, dt.started_at, 0)>0), 0),
                   created_at,
                   {float(now)!r})
               end) as stall_seconds
        from ranked_queue rq
        ), scored_queue as (
        select *,
               case
                 when real_attempt_count < {fast_attempt_budget}
                  and lower(coalesce(state, '')) in {LANE_ELIGIBLE_STATES_SQL}
                  and (
                       (series_first_seen_at is not null
                        and series_first_seen_at >= {new_series_cutoff!r})
                    or (issue_release_date is not null
                        and substr(issue_release_date, 1, 10) >= '{new_issue_cutoff_date}'
                        and substr(issue_release_date, 1, 10) <= '{today_date}')
                  )
                 then 1 else 0
               end as is_new_work
        from stalled_queue sq
        ), aged_queue as (
        select *,
               -- Bounded on both axes: the time term saturates so nothing
               -- climbs forever, the attempt term decays to exactly zero at
               -- the retry ceiling so the rows least likely to succeed stop
               -- competing for the capacity they keep spending.
               case when stall_seconds < {aged_min_stall_seconds!r}
                      or lower(coalesce(state, '')) not in {LANE_ELIGIBLE_STATES_SQL}
                    then 0.0 else 1.0 end *
               min(1.0, stall_seconds / {age_window_seconds!r}) *
               case
                 when coalesce(real_attempt_count, 0) <= {decay_start} then 1.0
                 when coalesce(real_attempt_count, 0) >= {retry_ceiling} then 0.0
                 else ({retry_ceiling} - coalesce(real_attempt_count, 0)) * 1.0
                      / ({retry_ceiling} - {decay_start})
               end as ageing_score
        from scored_queue
        ), laned_queue as (
        select *,
               -- Both reserved lanes rank by series round first, so a single
               -- deep backlog cannot take the whole lane. Without this the
               -- aged lane hands its entire quota to whichever series happens
               -- to hold the oldest rows -- the same head-of-line failure this
               -- change exists to remove, just relocated into the fix.
               row_number() over (
                 partition by is_new_work, series_key
                 order by real_attempt_count asc, stall_seconds desc,
                          coalesce(created_at, 0) asc, id asc
               ) as fast_series_round,
               -- Least recently served, rotating series inside a day band.
               --
               -- This used to lead on series_queue_round -- a row_number()
               -- per series -- and break ties on coalesce(retry_after,
               -- updated_at, created_at). Both halves were wrong in the same
               -- way: neither is a clock the pass's own work moves. A deep
               -- series surfaced exactly one row per pass however long its
               -- other 267 had waited, and updated_at is refreshed by
               -- bookkeeping, which the stall CASE above already refuses to
               -- read for precisely this reason. Nothing tracked "least
               -- recently served", so every pass re-offered its own head and
               -- the tail was reached only by luck: 55.6% of 2,172 rows had
               -- no real attempt in a week (2026-08-15), and service degraded
               -- monotonically with round -- 73.2% served at round 1, 29.0%
               -- at round 21+.
               --
               -- The sweep key is the service clock itself, and it is the one
               -- thing an executed search moves: a served row drops to band 0
               -- and goes to the back. That makes any prefix convergent --
               -- the set of unserved rows only shrinks between passes -- which
               -- is the guarantee the lane never had, and it is the same
               -- property the maintenance sweep gets from oldest-first plus a
               -- limit.
               --
               -- Whole days, not seconds, because the interleaving has to
               -- survive the key. At second granularity the oldest rows are a
               -- total order and one deep series' backlog takes the lane --
               -- the head-of-line failure relocated, which is the mistake the
               -- aged lane's own ranking already had to avoid. Inside a band
               -- the per-series round breaks ties, so bands rotate across
               -- series and drain as whole cohorts. A row served an hour ago
               -- cannot re-enter a band above 0 for a day, so it cannot
               -- outrank a row that has been waiting since before it.
               -- Deliberately NOT keyed on "never searched" first. A row that
               -- has never been searched already carries its whole life as
               -- stall, so the bands rank it without a separate cohort -- and
               -- adding one puts brand-new rows at the head of the steady lane
               -- too, which makes the fast lane's quota redundant. New content
               -- is the fast lane's job, and the new-content guard's own
               -- control depends on that staying true: with the fast lane's
               -- share removed, new rows must starve, and they do not if this
               -- lane is also rescuing them.
               row_number() over (
                 order by
                   -- The same state gate the other two lanes apply to their
                   -- scores, and for a sharper reason here. A row in a state
                   -- nothing searches -- blocked, superseded_duplicate -- has
                   -- a stall that only ever grows, so under an age key it does
                   -- not merely rank high, it colonises the head of the lane
                   -- permanently: it can never be served, so it can never drop
                   -- to band 0 and make way. That is precisely "ageing hands
                   -- the most capacity to the items least likely to succeed",
                   -- and the whole sweep would have been spent on rows that
                   -- cannot be searched at all. Ranked last instead, where
                   -- they stay visible to the scan without costing it a seat.
                   case when lower(coalesce(state, '')) in {LANE_ELIGIBLE_STATES_SQL}
                        then 0 else 1 end asc,
                   cast(stall_seconds / 86400.0 as integer) desc,
                   series_queue_round asc,
                   case
                     when retry_after<=? then 0
                     when retry_after is null then 1
                     else 2
                   end asc,
                   stall_seconds desc,
                   id asc
               ) as steady_lane_rank
        from aged_queue
        ), fast_ranked_queue as (
        select *,
               row_number() over (
                 -- Least recently searched series first, after the per-series
                 -- round; a series never searched reads 0 and leads. This is
                 -- the lane's service clock, and it is the one thing the pass's
                 -- own work moves: an executed search advances it, so the seat
                 -- rotates to the next new series instead of coming back to
                 -- the same one.
                 --
                 -- The lane used to order by series_first_seen_at desc alone,
                 -- which is a clock nothing updates. That reads as "newest
                 -- first" and is, on paper; live, the pass funds a median of 9
                 -- rows from an 80-row scan (54 passes over 48h to
                 -- 2026-09-03T22:27Z) and 1 of them is a fast-lane row, so
                 -- the newest series with untried rows held that seat until
                 -- every one of its rows had an attempt. 18 new-work series
                 -- sat at fast ranks 3..20 -- inside the quota, so excluded
                 -- from the aged and steady lanes, and never executed: 22 of 34
                 -- new-work series had no fast-lane search in 48h, and MAD's
                 -- 268 untried units went 0 for 3 days at fast rank 8.
                 --
                 -- Newest-first still breaks ties among series never searched,
                 -- so a series added a minute ago goes to the front on its
                 -- first pass, which is what that ordering was for.
                 partition by is_new_work
                 order by fast_series_round asc,
                          real_attempt_count asc,
                          series_last_real_attempt_at asc,
                          series_first_seen_at desc,
                          stall_seconds desc,
                          id asc
               ) as fast_lane_rank
        from laned_queue
        ), aged_eligible_queue as (
        -- Eligible for the aged lane only when the other two lanes are not
        -- already carrying the row. Ranking the lane over everything would let
        -- rows that were going to be served anyway consume the reserved slots,
        -- and it would reach no further down the backlog than today does.
        select *,
               case
                 when ageing_score > 0
                  and steady_lane_rank > {steady_quota}
                  and not (is_new_work=1 and fast_lane_rank <= {fast_quota})
                 then 1 else 0
               end as aged_lane_eligible
        from fast_ranked_queue
        ), aged_ranked_queue as (
        select *,
               row_number() over (
                 partition by aged_lane_eligible, series_key
                 order by ageing_score desc, stall_seconds desc, id asc
               ) as aged_series_round
        from aged_eligible_queue
        ), aged_lane_queue as (
        select *,
               row_number() over (
                 partition by aged_lane_eligible
                 order by aged_series_round asc, ageing_score desc,
                          stall_seconds desc, id asc
               ) as aged_lane_rank
        from aged_ranked_queue
        ), assigned_queue as (
        select *,
               case
                 when is_new_work=1 and fast_lane_rank <= {fast_quota} then 'fast'
                 when aged_lane_eligible=1 and aged_lane_rank <= {aged_quota} then 'aged'
                 else 'steady'
               end as queue_lane,
               case
                 when is_new_work=1 and fast_lane_rank <= {fast_quota} then fast_lane_rank
                 when aged_lane_eligible=1 and aged_lane_rank <= {aged_quota} then aged_lane_rank
                 else steady_lane_rank
               end as queue_lane_rank
        from aged_lane_queue
        ), ordered_queue as (
        select *, row_number() over (
          -- Interleave by fractional position within each lane's own quota, so
          -- any prefix of the scan carries the intended mix. Taking the head of
          -- one lane and then the head of the next would give the short lanes
          -- the front of every pass, which is the failure this replaces.
          order by
            (queue_lane_rank - 0.5) / case queue_lane
              when 'fast' then {fast_quota}.0
              when 'aged' then {aged_quota}.0
              else {steady_quota}.0
            end asc,
            case queue_lane when 'fast' then 0 when 'steady' then 1 else 2 end asc,
            id asc
        ) as scheduler_rank
        from assigned_queue
        )
    """
    # The clause set is interpolated twice -- once to narrow the attempt
    # aggregate to schedulable rows, once for the scan itself -- so its
    # parameters are bound twice, in that order.
    sql = ordered_cte + " select * from ordered_queue order by scheduler_rank limit ?"
    scan_params = [*params, *params, now, bounded_limit]
    with _borrowed_or_read_con(db_path, con) as scan_con:
        if not inkdrop_state.table_exists(scan_con, "queue_items"):
            return []
        rows = scan_con.execute(sql, scan_params).fetchall()
        if not queue_ids and due_only:
            # "Outside the scan" is exactly "not one of the rows we just took",
            # so the reserve tests that directly instead of re-running the whole
            # ordering to recover a rank it then only compares against the
            # limit. Against the live database the CTE costs ~1.5s; running it
            # twice per pass to find at most one extra row was most of what the
            # ordering change added to the pass.
            scanned_ids = [row["id"] for row in rows]
            reserve_params = [*params, now - 86400, *scanned_ids]
            reserve_sql = f"""
                select q.id, q.wanted_id, q.series_id, q.issue_id, q.state,
                       q.current_source, q.query, q.last_event, q.active,
                       q.created_at, q.updated_at, q.retry_after, q.retry_after_iso,
                       q.display_phase, q.outcome, q.provider_status_state,
                       q.provider_status_phase, q.provider_status_provider,
                       q.provider_status_actionability, q.raw_json,
                       s.title as series, s.media_type, s.year, s.publisher,
                       s.metadata_provider, s.metadata_id, s.kapowarr_id,
                       i.issue_number, i.title as issue_title, i.release_date as issue_release_date,
                       i.metadata_provider as issue_metadata_provider,
                       i.metadata_id as issue_metadata_id,
                       i.kapowarr_issue_id,
                       w.status as wanted_status, w.priority as wanted_priority,
                       (select count(*) from queue_items q2
                         where q2.active=1
                           and coalesce(nullif(trim(q2.series_id), ''), q2.id)
                               = coalesce(nullif(trim(q.series_id), ''), q.id)
                           and coalesce(q2.retry_after, q2.updated_at, q2.created_at, 0)
                               < coalesce(q.retry_after, q.updated_at, q.created_at, 0)
                       ) + 1 as series_queue_round,
                       {bounded_limit + 1} as scheduler_rank
                from queue_items q
                left join series s on s.id=q.series_id
                left join issues i on i.id=q.issue_id
                left join wanted_items w on w.id=q.wanted_id
                where {" and ".join(clauses)}
                  and lower(coalesce(q.state,''))='queued'
                  and coalesce(q.created_at, q.updated_at, 0) <= ?
                  and q.id not in ({",".join("?" for _ in scanned_ids) or "''"})
                  and not exists (
                      select 1 from download_tasks dt
                      where dt.queue_id=q.id
                        and lower(coalesce(dt.state,'')) in ('queued','downloading','import_ready','importing')
                        and lower(coalesce(dt.status,'')) not in (
                            'bad_archive','failed','failed_download','retired','superseded_duplicate',
                            'wrong_edition','wrong_series_or_subseries'
                        )
                  )
                  and not exists (
                      select 1 from source_attempts sa
                      where sa.queue_id=q.id
                        and lower(coalesce(nullif(sa.provider_id,''), nullif(sa.source,''), nullif(sa.provider,''), ''))
                            not in ('', 'queue', 'source_ladder', 'autopilot', 'importer', 'kavita')
                  )
                order by coalesce(q.created_at, q.updated_at, 0) asc, q.id asc
                limit 1
            """
            reserve = scan_con.execute(reserve_sql, reserve_params).fetchone()
            if reserve is not None and all(row["id"] != reserve["id"] for row in rows):
                rows = [*rows, reserve]
    out = []
    for row in rows:
        item = dict(row)
        item["raw_json"] = _json_loads(item.get("raw_json"))
        if int(item.get("scheduler_rank") or 0) > bounded_limit:
            created_at = _float(item.get("created_at")) or _float(item.get("updated_at")) or now
            item["aged_zero_provider_coverage_reserve"] = {
                "reserved": True,
                "eligible_but_outside_scan": True,
                "scheduler_rank": int(item.get("scheduler_rank") or 0),
                "series_queue_round": int(item.get("series_queue_round") or 0),
                "zero_automated_provider_coverage": True,
                "deferral_age_seconds": max(0, int(now - created_at)),
                "normal_scan_limit": bounded_limit,
            }
        out.append(item)
    return out


def active_handoff_tasks(
    db_path,
    queue_id,
    *,
    limit=8,
    now=None,
    stale_seconds=DEFAULT_ACTIVE_HANDOFF_STALE_SECONDS,
):
    """Rows this scheduler treats as "something is still handing this off".

    `completed_at is null` is required on purpose: a task can finish (and get
    `completed_at` written once, permanently) while its `state` column stays
    on an ACTIVE_HANDOFF_STATES value like 'queued' -- state and completion
    are two different columns that do not always move together. Without this
    guard a completed task with an unrecognized terminal status blocks
    _classify_queue_plan() forever, since nothing about "done" was ever
    checked, only the status vocabulary. Confirmed live 2026-08-18: a
    `staged_filename_mismatch` task completed 2+ weeks earlier kept
    reading as an active handoff on every pass.
    """
    queue_id = str(queue_id or "").strip()
    if not queue_id:
        return []
    now = time.time() if now is None else now
    stale_seconds = _float(stale_seconds)
    cutoff = None if stale_seconds is None or stale_seconds <= 0 else now - stale_seconds
    placeholders_states = ",".join("?" for _ in ACTIVE_HANDOFF_STATES)
    placeholders_terminal = ",".join("?" for _ in TERMINAL_OR_PROBLEM_HANDOFF_STATUSES)
    params = [
        queue_id,
        *sorted(ACTIVE_HANDOFF_STATES),
        *sorted(TERMINAL_OR_PROBLEM_HANDOFF_STATUSES),
    ]
    stale_clause = ""
    if cutoff is not None:
        stale_clause = "and (coalesce(updated_at, started_at, 0)=0 or coalesce(updated_at, started_at, 0)>=?)"
        params.append(cutoff)
    params.append(_bounded_limit(limit, default=8, maximum=50))
    sql = f"""
        select id, source, provider_id, provider, protocol, download_client,
               title, status, state, lifecycle_phase, outcome, display_phase,
               failure_reason, local_path, progress, started_at, updated_at
        from download_tasks
        where queue_id=?
          and completed_at is null
          and lower(coalesce(state,'')) in ({placeholders_states})
          and lower(coalesce(status,'')) not in ({placeholders_terminal})
          and not (
            lower(coalesce(status,''))='provider_wait'
            and trim(coalesce(download_client,''))=''
          )
          {stale_clause}
        order by coalesce(updated_at, started_at, 0) desc, id desc
        limit ?
    """
    with inkdrop_state.connect_read(db_path) as con:
        if not inkdrop_state.table_exists(con, "download_tasks"):
            return []
        return [dict(row) for row in con.execute(sql, params).fetchall()]


def stale_handoff_tasks(
    db_path,
    queue_id,
    *,
    limit=8,
    now=None,
    stale_seconds=DEFAULT_ACTIVE_HANDOFF_STALE_SECONDS,
):
    queue_id = str(queue_id or "").strip()
    stale_seconds = _float(stale_seconds)
    if not queue_id or stale_seconds is None or stale_seconds <= 0:
        return []
    now = time.time() if now is None else now
    cutoff = now - stale_seconds
    placeholders_states = ",".join("?" for _ in ACTIVE_HANDOFF_STATES)
    placeholders_terminal = ",".join("?" for _ in TERMINAL_OR_PROBLEM_HANDOFF_STATUSES)
    params = [
        queue_id,
        *sorted(ACTIVE_HANDOFF_STATES),
        *sorted(TERMINAL_OR_PROBLEM_HANDOFF_STATUSES),
        cutoff,
        _bounded_limit(limit, default=8, maximum=50),
    ]
    sql = f"""
        select id, source, provider_id, provider, protocol, download_client,
               title, status, state, lifecycle_phase, outcome, display_phase,
               failure_reason, local_path, progress, started_at, updated_at
        from download_tasks
        where queue_id=?
          and lower(coalesce(state,'')) in ({placeholders_states})
          and lower(coalesce(status,'')) not in ({placeholders_terminal})
          and not (
            lower(coalesce(status,''))='provider_wait'
            and trim(coalesce(download_client,''))=''
          )
          and coalesce(updated_at, started_at, 0)>0
          and coalesce(updated_at, started_at, 0)<?
        order by coalesce(updated_at, started_at, 0) desc, id desc
        limit ?
    """
    with inkdrop_state.connect_read(db_path) as con:
        if not inkdrop_state.table_exists(con, "download_tasks"):
            return []
        return [dict(row) for row in con.execute(sql, params).fetchall()]


def retryable_failed_handoff_tasks(
    db_path,
    queue_id,
    *,
    limit=8,
    now=None,
    recovery_seconds=RETRYABLE_FAILED_HANDOFF_RECOVERY_SECONDS,
):
    queue_id = str(queue_id or "").strip()
    if not queue_id:
        return []
    now = time.time() if now is None else now
    recovery_seconds = _float(recovery_seconds)
    cutoff = None if recovery_seconds is None or recovery_seconds <= 0 else now - recovery_seconds
    retryable_statuses = sorted(RETRYABLE_FAILED_HANDOFF_STATUSES)
    retryable_clients = sorted(DOWNLOAD_CLIENT_HANDOFF_CLIENTS)
    params = [
        queue_id,
        *retryable_statuses,
        *retryable_clients,
    ]
    cutoff_clause = ""
    if cutoff is not None:
        cutoff_clause = "and coalesce(updated_at, completed_at, started_at, 0)>=?"
        params.append(cutoff)
    params.append(_bounded_limit(limit, default=8, maximum=50))
    sql = f"""
        select id, source, provider_id, provider, protocol, download_client,
               external_id, candidate_identity, title, status, state,
               lifecycle_phase, outcome, display_phase, failure_reason,
               retry_eligible, local_path, progress, started_at, updated_at,
               completed_at
        from download_tasks
        where queue_id=?
          and coalesce(retry_eligible, 0)=1
          and lower(coalesce(status,'')) in ({','.join('?' for _ in retryable_statuses)})
          and lower(coalesce(download_client,'')) in ({','.join('?' for _ in retryable_clients)})
          {cutoff_clause}
        order by coalesce(updated_at, completed_at, started_at, 0) desc, id desc
        limit ?
    """
    with inkdrop_state.connect_read(db_path) as con:
        if not inkdrop_state.table_exists(con, "download_tasks"):
            return []
        return [dict(row) for row in con.execute(sql, params).fetchall()]


def _stageable_handoff_cutoff(active_handoffs):
    activity_times = []
    for task in active_handoffs or []:
        if not isinstance(task, dict):
            continue
        client = _lower(task.get("download_client"))
        if client not in STAGEABLE_HANDOFF_CLIENTS:
            continue
        activity_at = _float(task.get("updated_at")) or _float(task.get("started_at")) or 0.0
        if activity_at > 0:
            activity_times.append(activity_at)
    return min(activity_times) if activity_times else None


def retryable_failed_stage_attempts(
    db_path,
    queue_id,
    *,
    active_handoffs=None,
    limit=8,
    now=None,
    recovery_seconds=RETRYABLE_FAILED_HANDOFF_RECOVERY_SECONDS,
):
    queue_id = str(queue_id or "").strip()
    if not queue_id:
        return []
    handoff_cutoff = _stageable_handoff_cutoff(active_handoffs)
    if handoff_cutoff is None:
        return []
    now = time.time() if now is None else now
    recovery_seconds = _float(recovery_seconds)
    recovery_cutoff = None if recovery_seconds is None or recovery_seconds <= 0 else now - recovery_seconds
    cutoff = max(value for value in (handoff_cutoff, recovery_cutoff) if value is not None)
    statuses = sorted(RETRYABLE_FAILED_STAGE_ATTEMPT_STATUSES)
    params = [queue_id, *statuses, cutoff, _bounded_limit(limit, default=8, maximum=50)]
    sql = f"""
        select id, source, provider_id, provider, status, lifecycle_phase,
               outcome, display_phase, failure_reason, retry_eligible, title,
               started_at, completed_at,
               coalesce(completed_at, started_at, 0) as activity_at
        from source_attempts
        where queue_id=?
          and coalesce(retry_eligible, 0)=1
          and lower(coalesce(status,'')) in ({','.join('?' for _ in statuses)})
          and coalesce(completed_at, started_at, 0)>=?
        order by coalesce(completed_at, started_at, 0) desc, id desc
        limit ?
    """
    with inkdrop_state.connect_read(db_path) as con:
        if not inkdrop_state.table_exists(con, "source_attempts"):
            return []
        return [dict(row) for row in con.execute(sql, params).fetchall()]


def _grouped_by_queue_id(rows):
    """Group window-ranked rows per queue, dropping the grouping columns."""

    out = {}
    for row in rows or []:
        item = dict(row)
        queue_id = str(item.pop("queue_id", "") or "").strip()
        item.pop("per_queue_rank", None)
        if not queue_id:
            continue
        out.setdefault(queue_id, []).append(item)
    return out


def _clean_queue_ids(queue_ids):
    return [str(value or "").strip() for value in _list(queue_ids) if str(value or "").strip()]


def active_handoff_tasks_by_queue_id(
    con,
    queue_ids,
    *,
    limit=8,
    now=None,
    stale_seconds=DEFAULT_ACTIVE_HANDOFF_STALE_SECONDS,
):
    """Batched active_handoff_tasks: one indexed scan covering every queue row.

    See active_handoff_tasks()'s docstring for why `completed_at is null` is
    required here too -- the same fix, kept in step rather than only applied
    to the single-queue path this one was cloned from.
    """

    queue_ids = _clean_queue_ids(queue_ids)
    if not queue_ids or not inkdrop_state.table_exists(con, "download_tasks"):
        return {}
    now = time.time() if now is None else now
    stale_seconds = _float(stale_seconds)
    cutoff = None if stale_seconds is None or stale_seconds <= 0 else now - stale_seconds
    placeholders_queue = ",".join("?" for _ in queue_ids)
    placeholders_states = ",".join("?" for _ in ACTIVE_HANDOFF_STATES)
    placeholders_terminal = ",".join("?" for _ in TERMINAL_OR_PROBLEM_HANDOFF_STATUSES)
    params = [
        *queue_ids,
        *sorted(ACTIVE_HANDOFF_STATES),
        *sorted(TERMINAL_OR_PROBLEM_HANDOFF_STATUSES),
    ]
    stale_clause = ""
    if cutoff is not None:
        stale_clause = "and (coalesce(updated_at, started_at, 0)=0 or coalesce(updated_at, started_at, 0)>=?)"
        params.append(cutoff)
    params.append(_bounded_limit(limit, default=8, maximum=50))
    sql = f"""
        select * from (
            select queue_id, id, source, provider_id, provider, protocol, download_client,
                   title, status, state, lifecycle_phase, outcome, display_phase,
                   failure_reason, local_path, progress, started_at, updated_at,
                   row_number() over (
                       partition by queue_id
                       order by coalesce(updated_at, started_at, 0) desc, id desc
                   ) as per_queue_rank
            from download_tasks
            where queue_id in ({placeholders_queue})
              and completed_at is null
              and lower(coalesce(state,'')) in ({placeholders_states})
              and lower(coalesce(status,'')) not in ({placeholders_terminal})
              and not (
                lower(coalesce(status,''))='provider_wait'
                and trim(coalesce(download_client,''))=''
              )
              {stale_clause}
        )
        where per_queue_rank<=?
        order by queue_id asc, per_queue_rank asc
    """
    return _grouped_by_queue_id(con.execute(sql, params).fetchall())


def stale_handoff_tasks_by_queue_id(
    con,
    queue_ids,
    *,
    limit=8,
    now=None,
    stale_seconds=DEFAULT_ACTIVE_HANDOFF_STALE_SECONDS,
):
    """Batched stale_handoff_tasks: one indexed scan covering every queue row."""

    queue_ids = _clean_queue_ids(queue_ids)
    stale_seconds = _float(stale_seconds)
    if not queue_ids or stale_seconds is None or stale_seconds <= 0:
        return {}
    if not inkdrop_state.table_exists(con, "download_tasks"):
        return {}
    now = time.time() if now is None else now
    cutoff = now - stale_seconds
    placeholders_queue = ",".join("?" for _ in queue_ids)
    placeholders_states = ",".join("?" for _ in ACTIVE_HANDOFF_STATES)
    placeholders_terminal = ",".join("?" for _ in TERMINAL_OR_PROBLEM_HANDOFF_STATUSES)
    params = [
        *queue_ids,
        *sorted(ACTIVE_HANDOFF_STATES),
        *sorted(TERMINAL_OR_PROBLEM_HANDOFF_STATUSES),
        cutoff,
        _bounded_limit(limit, default=8, maximum=50),
    ]
    sql = f"""
        select * from (
            select queue_id, id, source, provider_id, provider, protocol, download_client,
                   title, status, state, lifecycle_phase, outcome, display_phase,
                   failure_reason, local_path, progress, started_at, updated_at,
                   row_number() over (
                       partition by queue_id
                       order by coalesce(updated_at, started_at, 0) desc, id desc
                   ) as per_queue_rank
            from download_tasks
            where queue_id in ({placeholders_queue})
              and lower(coalesce(state,'')) in ({placeholders_states})
              and lower(coalesce(status,'')) not in ({placeholders_terminal})
              and not (
                lower(coalesce(status,''))='provider_wait'
                and trim(coalesce(download_client,''))=''
              )
              and coalesce(updated_at, started_at, 0)>0
              and coalesce(updated_at, started_at, 0)<?
        )
        where per_queue_rank<=?
        order by queue_id asc, per_queue_rank asc
    """
    return _grouped_by_queue_id(con.execute(sql, params).fetchall())


def retryable_failed_handoff_tasks_by_queue_id(
    con,
    queue_ids,
    *,
    limit=8,
    now=None,
    recovery_seconds=RETRYABLE_FAILED_HANDOFF_RECOVERY_SECONDS,
):
    """Batched retryable_failed_handoff_tasks: one indexed scan per pass."""

    queue_ids = _clean_queue_ids(queue_ids)
    if not queue_ids or not inkdrop_state.table_exists(con, "download_tasks"):
        return {}
    now = time.time() if now is None else now
    recovery_seconds = _float(recovery_seconds)
    cutoff = None if recovery_seconds is None or recovery_seconds <= 0 else now - recovery_seconds
    retryable_statuses = sorted(RETRYABLE_FAILED_HANDOFF_STATUSES)
    retryable_clients = sorted(DOWNLOAD_CLIENT_HANDOFF_CLIENTS)
    placeholders_queue = ",".join("?" for _ in queue_ids)
    params = [
        *queue_ids,
        *retryable_statuses,
        *retryable_clients,
    ]
    cutoff_clause = ""
    if cutoff is not None:
        cutoff_clause = "and coalesce(updated_at, completed_at, started_at, 0)>=?"
        params.append(cutoff)
    params.append(_bounded_limit(limit, default=8, maximum=50))
    sql = f"""
        select * from (
            select queue_id, id, source, provider_id, provider, protocol, download_client,
                   external_id, candidate_identity, title, status, state,
                   lifecycle_phase, outcome, display_phase, failure_reason,
                   retry_eligible, local_path, progress, started_at, updated_at,
                   completed_at,
                   row_number() over (
                       partition by queue_id
                       order by coalesce(updated_at, completed_at, started_at, 0) desc, id desc
                   ) as per_queue_rank
            from download_tasks
            where queue_id in ({placeholders_queue})
              and coalesce(retry_eligible, 0)=1
              and lower(coalesce(status,'')) in ({','.join('?' for _ in retryable_statuses)})
              and lower(coalesce(download_client,'')) in ({','.join('?' for _ in retryable_clients)})
              {cutoff_clause}
        )
        where per_queue_rank<=?
        order by queue_id asc, per_queue_rank asc
    """
    return _grouped_by_queue_id(con.execute(sql, params).fetchall())


def retryable_failed_stage_attempts_by_queue_id(
    con,
    handoff_cutoffs,
    *,
    limit=8,
    now=None,
    recovery_seconds=RETRYABLE_FAILED_HANDOFF_RECOVERY_SECONDS,
):
    """Batched retryable_failed_stage_attempts.

    handoff_cutoffs maps queue_id -> _stageable_handoff_cutoff(active_handoffs);
    queues without a stageable handoff (cutoff None) are skipped exactly like
    the per-queue helper's early return.
    """

    cutoffs = {}
    for queue_id, handoff_cutoff in (handoff_cutoffs or {}).items():
        queue_id = str(queue_id or "").strip()
        if not queue_id or handoff_cutoff is None:
            continue
        cutoffs[queue_id] = handoff_cutoff
    if not cutoffs or not inkdrop_state.table_exists(con, "source_attempts"):
        return {}
    now = time.time() if now is None else now
    recovery_seconds = _float(recovery_seconds)
    recovery_cutoff = None if recovery_seconds is None or recovery_seconds <= 0 else now - recovery_seconds
    effective_cutoffs = {
        queue_id: max(value for value in (handoff_cutoff, recovery_cutoff) if value is not None)
        for queue_id, handoff_cutoff in cutoffs.items()
    }
    scan_floor = min(effective_cutoffs.values())
    statuses = sorted(RETRYABLE_FAILED_STAGE_ATTEMPT_STATUSES)
    bounded = _bounded_limit(limit, default=8, maximum=50)
    queue_id_list = sorted(effective_cutoffs)
    params = [*queue_id_list, *statuses, scan_floor, bounded]
    sql = f"""
        select * from (
            select queue_id, id, source, provider_id, provider, status, lifecycle_phase,
                   outcome, display_phase, failure_reason, retry_eligible, title,
                   started_at, completed_at,
                   coalesce(completed_at, started_at, 0) as activity_at,
                   row_number() over (
                       partition by queue_id
                       order by coalesce(completed_at, started_at, 0) desc, id desc
                   ) as per_queue_rank
            from source_attempts
            where queue_id in ({",".join("?" for _ in queue_id_list)})
              and coalesce(retry_eligible, 0)=1
              and lower(coalesce(status,'')) in ({",".join("?" for _ in statuses)})
              and coalesce(completed_at, started_at, 0)>=?
        )
        where per_queue_rank<=?
        order by queue_id asc, per_queue_rank asc
    """
    grouped = _grouped_by_queue_id(con.execute(sql, params).fetchall())
    out = {}
    for queue_id, rows in grouped.items():
        cutoff = effective_cutoffs.get(queue_id)
        if cutoff is None:
            continue
        # Rows failing the per-queue cutoff sort after every row that passes it,
        # so filtering here after the shared-floor rank keeps the same top rows
        # the per-queue query would have returned.
        kept = [row for row in rows if (_float(row.get("activity_at")) or 0) >= cutoff]
        if kept:
            out[queue_id] = kept[:bounded]
    return out


def queue_provider_wait_reason(row, *, now=None):
    row = _dict(row)
    now = time.time() if now is None else now
    state = _lower(row.get("state"))
    display_phase = _lower(row.get("display_phase"))
    provider_state = _lower(row.get("provider_status_state"))
    provider_phase = _lower(row.get("provider_status_phase"))
    provider = row.get("provider_status_provider") or row.get("current_source") or "source"
    retry_after = _float(row.get("retry_after"))
    if "provider_wait" in {state, display_phase, provider_state, provider_phase}:
        return f"{provider} is in provider_wait"
    if state == "source_wait":
        return f"{provider} is waiting for provider/downloader confirmation"
    if retry_after is not None and retry_after > now:
        return f"retry_after is in the future for {provider}"
    return ""


def _job_counts(jobs):
    counts = {}
    for job in jobs or []:
        status = str((job or {}).get("job_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _jobs_visible_for_selection(jobs, *, include_blocked=False):
    if include_blocked:
        return list(jobs or [])
    return [
        job
        for job in jobs or []
        if isinstance(job, dict) and job.get("job_status") not in BLOCKED_JOB_STATUSES
    ]


def _selected_provider_ids(jobs):
    return [
        job.get("provider_id")
        for job in jobs or []
        if isinstance(job, dict) and job.get("provider_id")
    ]


def _provider_key(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return inkdrop_sources.provider_key(text)
    except Exception:
        return text.lower()


def _job_provider_id(job):
    job = _dict(job)
    return _provider_key(job.get("provider_id") or job.get("source") or job.get("provider"))


def _is_broad_prowlarr_job(job):
    return _job_provider_id(job) == "prowlarr"


def _is_concrete_prowlarr_job(job):
    provider_id = _job_provider_id(job)
    return provider_id.startswith("prowlarr_")


def _primary_ready_jobs(ready_jobs, cooled_jobs=None):
    ready_jobs = list(ready_jobs or [])
    if not ready_jobs:
        return []
    concrete_ready = any(_is_concrete_prowlarr_job(job) for job in ready_jobs)
    concrete_cooled = any(_is_concrete_prowlarr_job(job) for job in (cooled_jobs or []))
    if not (concrete_ready or concrete_cooled):
        return ready_jobs
    return [job for job in ready_jobs if not _is_broad_prowlarr_job(job)]


def _selection_ready_jobs(jobs):
    out = []
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        if job.get("job_status") != "ready":
            continue
        scope = job.get("source_scope") if isinstance(job.get("source_scope"), dict) else {}
        if scope and scope.get("eligible") is False:
            continue
        out.append(job)
    return out


def _provider_attempt_state(job, *, selected=False, cooldown=None):
    job = _dict(job)
    provider_id = _job_provider_id(job)
    status = str(job.get("job_status") or "unknown")
    if selected:
        return "selected"
    if cooldown:
        if cooldown.get("kind") in {PROVIDER_TIMEOUT_CIRCUIT_KIND, PROVIDER_FETCH_FAILURE_CIRCUIT_KIND}:
            return cooldown.get("kind")
        return "recent_attempt_cooldown"
    if status == "ready":
        return "ready_not_selected"
    if status == "provider_wait":
        return "provider_wait"
    if status == "operator_required":
        return "operator_required"
    if status == "configuration_required":
        return "configuration_required"
    if status in {"unsupported_adapter", "not_executable"}:
        return "not_executable"
    if status == "blocked":
        return "blocked"
    return status or provider_id or "unknown"


def provider_attempt_plan(jobs, selected_jobs=None, attempt_cooldowns=None, attempt_history=None):
    """Return compact per-provider evidence for source-worker freshness diagnostics."""

    jobs = list(jobs or [])
    selected_ids = {_job_provider_id(job) for job in (selected_jobs or []) if _job_provider_id(job)}
    attempt_cooldowns = attempt_cooldowns if isinstance(attempt_cooldowns, dict) else {}
    attempt_history = attempt_history if isinstance(attempt_history, dict) else {}
    out = []
    seen = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        provider_id = _job_provider_id(job)
        if not provider_id:
            continue
        key = (provider_id, str(job.get("job_status") or "unknown"), str(job.get("reason") or ""))
        if key in seen:
            continue
        seen.add(key)
        cooldown = attempt_cooldowns.get(provider_id) or {}
        selected = provider_id in selected_ids
        state = _provider_attempt_state(job, selected=selected, cooldown=cooldown)
        fetch_plan = job.get("fetch_plan") if isinstance(job.get("fetch_plan"), dict) else {}
        requests = fetch_plan.get("requests") if isinstance(fetch_plan.get("requests"), list) else []
        try:
            request_count = int(fetch_plan.get("estimated_request_count") or len(requests))
        except Exception:
            request_count = len(requests)
        evidence = {
            "provider_id": provider_id,
            "display_name": job.get("display_name") or provider_id,
            "source_kind": job.get("source_kind"),
            "adapter_family": job.get("adapter_family"),
            "job_status": job.get("job_status") or "unknown",
            "attempt_state": state,
            "selected": bool(selected),
            "reason": job.get("reason") or "",
            "schedule_state": job.get("schedule_state") or "",
            "can_execute_with_http_client": bool(job.get("can_execute_with_http_client")),
            "can_execute_with_tool_runner": bool(job.get("can_execute_with_tool_runner")),
            "requires_operator": bool(job.get("requires_operator")),
            "emits_download_task": bool(job.get("emits_download_task")),
            "payload_mode": fetch_plan.get("payload_mode"),
            "request_count": max(0, request_count),
        }
        if cooldown:
            if cooldown.get("kind"):
                evidence["cooldown_kind"] = cooldown.get("kind")
            evidence["last_attempt_status"] = cooldown.get("status") or ""
            if cooldown.get("failure_reason"):
                evidence["failure_reason"] = cooldown.get("failure_reason") or ""
            evidence["last_attempt_age_seconds"] = int(cooldown.get("age_seconds") or 0)
            evidence["remaining_cooldown_seconds"] = int(cooldown.get("remaining_seconds") or 0)
            if cooldown.get("kind") == PROVIDER_TIMEOUT_CIRCUIT_KIND:
                evidence["circuit_provider_id"] = cooldown.get("circuit_provider_id") or provider_id
                evidence["recent_timeout_count"] = int(cooldown.get("recent_timeout_count") or 0)
                evidence["timeout_window_seconds"] = int(cooldown.get("window_seconds") or 0)
                evidence["timeout_threshold"] = int(cooldown.get("threshold") or 0)
            if cooldown.get("kind") == PROVIDER_FETCH_FAILURE_CIRCUIT_KIND:
                evidence["circuit_provider_id"] = cooldown.get("circuit_provider_id") or provider_id
                evidence["recent_fetch_failure_count"] = int(cooldown.get("recent_fetch_failure_count") or 0)
                evidence["fetch_failure_window_seconds"] = int(cooldown.get("window_seconds") or 0)
                evidence["fetch_failure_threshold"] = int(cooldown.get("threshold") or 0)
        if isinstance(job.get("source_scope"), dict):
            evidence["source_scope"] = job.get("source_scope")
        history = attempt_history.get(provider_id) or {}
        if history:
            evidence["source_worker_attempt_count"] = int(history.get("attempt_count") or 0)
            if history.get("last_attempt_status") and not evidence.get("last_attempt_status"):
                evidence["last_attempt_status"] = history.get("last_attempt_status")
        out.append({k: v for k, v in evidence.items() if v not in (None, "", [], {})})
    return out


def _provider_attempt_jobs(jobs, *, selected_jobs=None, cooled_jobs=None):
    jobs = list(jobs or [])
    has_concrete_prowlarr_decision = any(
        _is_concrete_prowlarr_job(job)
        for job in list(selected_jobs or []) + list(cooled_jobs or [])
    )
    if not has_concrete_prowlarr_decision:
        return jobs
    return [job for job in jobs if not _is_broad_prowlarr_job(job)]


def _visible_attempt_cooldowns(attempt_cooldowns, provider_jobs):
    attempt_cooldowns = attempt_cooldowns if isinstance(attempt_cooldowns, dict) else {}
    visible_provider_ids = {
        _job_provider_id(job)
        for job in provider_jobs or []
        if _job_provider_id(job)
    }
    if not visible_provider_ids:
        return {}
    return {
        provider_id: cooldown
        for provider_id, cooldown in attempt_cooldowns.items()
        if provider_id in visible_provider_ids
    }


def _visible_cooled_jobs(cooled_jobs, provider_jobs):
    visible_provider_ids = {
        _job_provider_id(job)
        for job in provider_jobs or []
        if _job_provider_id(job)
    }
    if not visible_provider_ids:
        return []
    return [
        job
        for job in cooled_jobs or []
        if _job_provider_id(job) in visible_provider_ids
    ]


def _provider_attempt_counts(provider_plan):
    counts = {}
    for row in provider_plan or []:
        state = str((row or {}).get("attempt_state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return dict(sorted(counts.items()))


def _is_terminal_source_attempt_history(row):
    row = row if isinstance(row, dict) else {}
    status = _lower(row.get("status"))
    phase = _lower(row.get("lifecycle_phase"))
    outcome = _lower(row.get("outcome"))
    if not status:
        # A ROW THAT SAYS NOTHING IS NOT A TERMINAL ATTEMPT, and the canonical
        # authority already said so. inkdrop_state.source_attempt_is_real_attempt()
        # rejects an absent, null or empty status on its own line. This predicate
        # never asked it: an empty status is in neither the non-terminal status set
        # nor the phase/outcome pair, so it fell through to True and a blank
        # provider-provenance row became BOTH cooldown evidence and history -- a
        # verdict minted from a row that recorded nothing.
        return False
    if status in NON_TERMINAL_SOURCE_ATTEMPT_STATUSES:
        return False
    if phase in NON_TERMINAL_SOURCE_ATTEMPT_PHASES and outcome in NON_TERMINAL_SOURCE_ATTEMPT_OUTCOMES:
        return False
    return True


def _is_non_attempt_source_attempt(row):
    row = row if isinstance(row, dict) else {}
    raw = _json_loads(row.get("raw_json"))
    kind = _lower(row.get("kind") or raw.get("kind"))
    if kind in NON_ATTEMPT_SOURCE_ATTEMPT_KINDS:
        return True
    reason = _lower(
        row.get("failure_reason")
        or row.get("reason")
        or raw.get("failure_reason")
        or raw.get("reason")
    )
    if not reason:
        return False
    if any(reason.startswith(prefix) for prefix in NON_ATTEMPT_SOURCE_ATTEMPT_REASON_PREFIXES):
        return True
    return any(text in reason for text in NON_ATTEMPT_SOURCE_ATTEMPT_REASON_TEXT)


def _is_completed_real_source_attempt(row):
    """True when a real source_attempts row also finished with an answer.

    real_attempt_count/last_real_attempt_at (below) feed only the aggregate
    Prowlarr due gate (_aggregate_comic_prowlarr_due() in
    inkdrop_source_worker_batch.py), so narrowing them here stays local to
    that gate and does not touch source_attempt_is_real_attempt() itself or
    its other callers (the retry ceiling, the Reliability page). A row of
    kind `source_started_timeout` ("InkDrop started a Prowlarr search ... and
    never finished it") passes source_attempt_is_real_attempt() -- `timeout`
    is not a non-attempt status -- but the search never returned an answer,
    so it must not start the aggregate's 7-day cooldown or stand in for a
    real completed attempt.

    completed_at cannot stand in for that check: measured live on the
    2026-09-22T22:27:06Z snapshot, completed_at is NULL on 1,634 of 1,648
    `searched_no_candidates` rows and all 12,046 `review` rows -- both
    genuinely real, completed prowlarr outcomes. Requiring completed_at
    would have zeroed real_attempt_count almost everywhere, not just for the
    timeout shape, and made the aggregate cooldown never apply at all.
    """
    row = row if isinstance(row, dict) else {}
    raw = _json_loads(row.get("raw_json"))
    kind = _lower(row.get("kind") or raw.get("kind"))
    return kind != "source_started_timeout"


def recent_source_attempt_cooldowns(
    db_path,
    queue_id,
    jobs,
    *,
    now=None,
    cooldown_seconds=None,
    limit=50,
):
    """Return recent per-provider source attempts that should delay another run."""

    seconds = _float(cooldown_seconds)
    if seconds is None or seconds <= 0:
        return {}
    queue_id = str(queue_id or "").strip()
    if not queue_id:
        return {}
    now = time.time() if now is None else now
    provider_ids = {
        _job_provider_id(job)
        for job in jobs or []
        if isinstance(job, dict) and _job_provider_id(job)
    }
    if not provider_ids:
        return {}
    since = now - seconds
    sql = """
        select provider_id, source, provider, status, title,
               lifecycle_phase, display_phase, outcome,
               failure_reason, completed_at, started_at, raw_json
        from source_attempts
        where queue_id=?
          and coalesce(completed_at, started_at, 0) >= ?
        order by coalesce(completed_at, started_at, 0) desc, id desc
        limit ?
    """
    with inkdrop_state.connect_read(db_path) as con:
        if not inkdrop_state.table_exists(con, "source_attempts"):
            return {}
        rows = [dict(row) for row in con.execute(sql, (queue_id, since, _bounded_limit(limit, default=50, maximum=500))).fetchall()]
    return _cooldowns_from_recent_attempt_rows(rows, provider_ids, now=now, seconds=seconds)


def _cooldowns_from_recent_attempt_rows(rows, provider_ids, *, now, seconds):
    cooldowns = {}
    for row in rows or []:
        if _is_non_attempt_source_attempt(row):
            continue
        provider_id = _provider_key(row.get("provider_id") or row.get("source") or row.get("provider"))
        if not provider_id or provider_id not in provider_ids or provider_id in cooldowns:
            continue
        last_attempt_at = _float(row.get("completed_at")) or _float(row.get("started_at")) or now
        age_seconds = max(0, now - last_attempt_at)
        cooldowns[provider_id] = {
            "provider_id": provider_id,
            "status": row.get("status") or "",
            "title": row.get("title") or "",
            "failure_reason": row.get("failure_reason") or "",
            "last_attempt_at": last_attempt_at,
            "age_seconds": age_seconds,
            "remaining_seconds": max(0, seconds - age_seconds),
            "cooldown_seconds": seconds,
        }
    return cooldowns


def recent_source_attempt_rows_by_queue_id(
    con,
    queue_ids,
    *,
    now=None,
    cooldown_seconds=None,
    limit=50,
):
    """Batched prefetch feeding _cooldowns_from_recent_attempt_rows per queue."""

    seconds = _float(cooldown_seconds)
    if seconds is None or seconds <= 0:
        return {}
    queue_ids = _clean_queue_ids(queue_ids)
    if not queue_ids or not inkdrop_state.table_exists(con, "source_attempts"):
        return {}
    now = time.time() if now is None else now
    since = now - seconds
    sql = f"""
        select * from (
            select queue_id, provider_id, source, provider, status, title,
                   lifecycle_phase, display_phase, outcome,
                   failure_reason, completed_at, started_at, raw_json,
                   row_number() over (
                       partition by queue_id
                       order by coalesce(completed_at, started_at, 0) desc, id desc
                   ) as per_queue_rank
            from source_attempts
            where queue_id in ({",".join("?" for _ in queue_ids)})
              and coalesce(completed_at, started_at, 0) >= ?
        )
        where per_queue_rank<=?
        order by queue_id asc, per_queue_rank asc
    """
    params = [*queue_ids, since, _bounded_limit(limit, default=50, maximum=500)]
    return _grouped_by_queue_id(con.execute(sql, params).fetchall())


def source_attempt_history_counts(
    db_path,
    queue_id,
    jobs,
    *,
    limit=500,
):
    """Return per-provider source attempt history for fair concrete lane rotation."""

    queue_id = str(queue_id or "").strip()
    if not queue_id:
        return {}
    provider_ids = {
        _job_provider_id(job)
        for job in jobs or []
        if isinstance(job, dict) and _job_provider_id(job)
    }
    if not provider_ids:
        return {}
    with inkdrop_state.connect_read(db_path) as con:
        if not inkdrop_state.table_exists(con, "source_attempts"):
            return {}
        rows = [
            dict(row)
            for row in con.execute(
                """
                select provider_id, source, provider, status, lifecycle_phase,
                       display_phase, outcome, failure_reason, raw_json,
                       coalesce(completed_at, started_at, 0) as activity_at
                from source_attempts
                where queue_id=?
                order by coalesce(completed_at, started_at, 0) desc, id desc
                limit ?
                """,
                (queue_id, _bounded_limit(limit, default=500, maximum=5000)),
            ).fetchall()
        ]
    return _history_counts_from_attempt_rows(rows, provider_ids)


def _history_counts_from_attempt_rows(rows, provider_ids):
    history = {}
    for row in rows or []:
        if _is_non_attempt_source_attempt(row):
            continue
        provider_id = _provider_key(row.get("provider_id") or row.get("source") or row.get("provider"))
        if not provider_id or provider_id not in provider_ids:
            continue
        entry = history.setdefault(
            provider_id,
            {
                "provider_id": provider_id,
                "attempt_count": 0,
                "terminal_attempt_count": 0,
                "real_attempt_count": 0,
                "last_attempt_at": 0.0,
                "last_attempt_status": "",
                "last_terminal_attempt_at": 0.0,
                "last_terminal_attempt_status": "",
                "last_real_attempt_at": 0.0,
                "last_real_attempt_status": "",
            },
        )
        entry["attempt_count"] = int(entry.get("attempt_count") or 0) + 1
        activity_at = _float(row.get("activity_at")) or 0.0
        if activity_at >= float(entry.get("last_attempt_at") or 0):
            entry["last_attempt_at"] = activity_at
            entry["last_attempt_status"] = row.get("status") or ""
        if _is_terminal_source_attempt_history(row):
            entry["terminal_attempt_count"] = int(entry.get("terminal_attempt_count") or 0) + 1
            if activity_at >= float(entry.get("last_terminal_attempt_at") or 0):
                entry["last_terminal_attempt_at"] = activity_at
                entry["last_terminal_attempt_status"] = row.get("status") or ""
        # source_attempt_is_real_attempt() is inkdrop_state's single authority for
        # "a real try" (real_attempt_predicate_sql() composes the same predicate
        # for the retry ceiling and the Reliability page). _is_terminal_source_attempt_history
        # above answers a different question and counts a provider_wait backoff skip
        # as terminal, so callers that need "did this provider actually get tried"
        # -- not "did the ledger settle" -- read real_attempt_count instead.
        # _is_completed_real_source_attempt() additionally requires the try to
        # have finished, so a source_started_timeout row (real per the status
        # predicate, but never completed) does not count here -- see its
        # docstring for why that stays local to this field.
        if inkdrop_state.source_attempt_is_real_attempt(row) and _is_completed_real_source_attempt(row):
            entry["real_attempt_count"] = int(entry.get("real_attempt_count") or 0) + 1
            if activity_at >= float(entry.get("last_real_attempt_at") or 0):
                entry["last_real_attempt_at"] = activity_at
                entry["last_real_attempt_status"] = row.get("status") or ""
    return history


def source_attempt_history_rows_by_queue_id(con, queue_ids, *, limit=500):
    """Batched prefetch feeding _history_counts_from_attempt_rows per queue."""

    queue_ids = _clean_queue_ids(queue_ids)
    if not queue_ids or not inkdrop_state.table_exists(con, "source_attempts"):
        return {}
    sql = f"""
        select * from (
            select queue_id, provider_id, source, provider, status, lifecycle_phase,
                   display_phase, outcome, failure_reason, raw_json,
                   coalesce(completed_at, started_at, 0) as activity_at,
                   row_number() over (
                       partition by queue_id
                       order by coalesce(completed_at, started_at, 0) desc, id desc
                   ) as per_queue_rank
            from source_attempts
            where queue_id in ({",".join("?" for _ in queue_ids)})
        )
        where per_queue_rank<=?
        order by queue_id asc, per_queue_rank asc
    """
    params = [*queue_ids, _bounded_limit(limit, default=500, maximum=5000)]
    return _grouped_by_queue_id(con.execute(sql, params).fetchall())


def latest_source_attempts_by_queue_id(db_path, queue_ids, *, con=None):
    queue_ids = [
        str(queue_id or "").strip()
        for queue_id in _list(queue_ids)
        if str(queue_id or "").strip()
    ]
    if not queue_ids:
        return {}
    placeholders = ",".join("?" for _ in queue_ids)
    with _borrowed_or_read_con(db_path, con) as attempts_con:
        if not inkdrop_state.table_exists(attempts_con, "source_attempts"):
            return {}
        rows = [
            dict(row)
            for row in attempts_con.execute(
                f"""
                select queue_id,
                       coalesce(source, '') as source,
                       coalesce(provider_id, '') as provider_id,
                       coalesce(provider, '') as provider,
                       coalesce(status, '') as status,
                       coalesce(outcome, '') as outcome,
                       coalesce(lifecycle_phase, '') as lifecycle_phase,
                       coalesce(display_phase, '') as display_phase,
                       coalesce(failure_reason, '') as failure_reason,
                       coalesce(title, '') as title,
                       coalesce(completed_at, started_at, 0) as activity_at
                from source_attempts
                where queue_id in ({placeholders})
                order by queue_id asc, coalesce(completed_at, started_at, 0) desc, id desc
                """,
                queue_ids,
            ).fetchall()
        ]
    out = {}
    for row in rows:
        queue_id = str(row.get("queue_id") or "").strip()
        if not queue_id or queue_id in out:
            continue
        out[queue_id] = {
            "source": row.get("source") or "",
            "provider_id": row.get("provider_id") or row.get("source") or "",
            "provider": row.get("provider") or row.get("provider_id") or row.get("source") or "",
            "status": row.get("status") or "",
            "outcome": row.get("outcome") or "",
            "lifecycle_phase": row.get("lifecycle_phase") or "",
            "display_phase": row.get("display_phase") or "",
            "failure_reason": row.get("failure_reason") or "",
            "title": row.get("title") or "",
            "activity_at": _float(row.get("activity_at")) or 0.0,
        }
    return out


def _provider_parent_keys(provider_id):
    provider_id = _provider_key(provider_id)
    if not provider_id:
        return []
    parents = []
    if provider_id.startswith("generic_torrent"):
        parents.append("torrent_html_sources")
    return parents


def _skip_timeout_circuit_health_key(provider_id, health_key):
    provider_id = _provider_key(provider_id)
    health_key = _provider_key(health_key)
    if not provider_id or not health_key:
        return False
    # RSS feed rows keep parent health display, but timeout circuits are per feed.
    if health_key == "rss" and (provider_id.startswith("rss_") or provider_id.startswith("generic_rss")):
        return True
    # Targeted Prowlarr indexer rows keep parent health display, but timeout
    # circuits are per configured indexer so the aggregate provider cannot
    # suppress unrelated source lanes.
    if health_key == "prowlarr" and provider_id.startswith("prowlarr_"):
        return True
    return False


def _job_timeout_circuit_keys(job):
    job = _dict(job)
    keys = []
    provider_id = _job_provider_id(job)
    for value in [provider_id, *list(job.get("health_provider_ids") or [])]:
        key = _provider_key(value)
        if _skip_timeout_circuit_health_key(provider_id, key):
            continue
        if key and key not in keys:
            keys.append(key)
        for parent in _provider_parent_keys(key):
            if parent and parent not in keys:
                keys.append(parent)
    return keys


def _job_recovery_health_keys(job):
    job = _dict(job)
    keys = []
    provider_id = _job_provider_id(job)
    for value in [provider_id, *list(job.get("health_provider_ids") or [])]:
        key = _provider_key(value)
        if key and key not in keys:
            keys.append(key)
    return keys


def _health_is_recovered_after(health, activity_at):
    health = _dict(health)
    if not health:
        return False
    state = _lower(health.get("state"))
    scope = _lower(health.get("health_scope") or health.get("scope"))
    if scope in {"download_history", "download_history_only"}:
        return False
    if state in inkdrop_sources.PROVIDER_HEALTH_PROBLEM_STATES:
        return False
    created_at = _float(health.get("created_at"))
    if created_at is None or created_at <= (_float(activity_at) or 0):
        return False
    return state in PROVIDER_TIMEOUT_RECOVERY_STATES or health.get("api_reachable") is True


def _provider_recovery_health(job, provider_health_map, activity_at, recovery_keys=None):
    provider_health_map = provider_health_map if isinstance(provider_health_map, dict) else {}
    keys = recovery_keys if recovery_keys is not None else _job_recovery_health_keys(job)
    for key in keys:
        health = provider_health_map.get(key)
        if _health_is_recovered_after(health, activity_at):
            recovered = dict(health)
            recovered["provider_id"] = recovered.get("provider_id") or key
            return recovered
    return {}


def _job_fetch_failure_recovery_health_keys(job):
    job = _dict(job)
    keys = []
    provider_id = _job_provider_id(job)
    for value in [provider_id, *list(job.get("health_provider_ids") or [])]:
        key = _provider_key(value)
        if _skip_timeout_circuit_health_key(provider_id, key):
            continue
        if key and key not in keys:
            keys.append(key)
    return keys


def _timeout_signal_from_row(row):
    row = _dict(row)
    if any(
        _timeout_signal_from_text(row.get(key))
        for key in ("status", "display_phase", "failure_reason")
    ):
        return True

    raw = row.get("raw_json")
    if isinstance(raw, (dict, list)):
        return _timeout_signal_from_json_value(raw)
    raw_text = str(raw or "").strip()
    if not raw_text:
        return False
    try:
        payload = json.loads(raw_text)
    except (TypeError, ValueError):
        # Old source-attempt writers sometimes stored a plain error token or
        # message instead of JSON.  Preserve those exact legacy signals, but
        # do not guess at the value of a malformed JSON flag: the text
        # ``"command_timed_out": false`` was the production false positive
        # that opened provider circuits.
        if raw_text.startswith(("{", "[")):
            return False
        return _timeout_signal_from_text(raw_text)
    return _timeout_signal_from_json_value(payload, allow_text=isinstance(payload, str))


def _timeout_signal_from_text(value):
    raw_text = str(value or "").strip().lower()[:4096]
    if not raw_text:
        return False
    # These legacy phrases carry timeout meaning across a conjunction, so
    # recognize them before bounded clause splitting.
    normalized_text = re.sub(r"[^a-z0-9]+", " ", raw_text).strip()
    if (
        "search budget exhausted" in normalized_text
        or "source started but did not report" in normalized_text
    ):
        return True
    clauses = re.split(
        r"(?:[\r\n,;:|.!?]+|\b(?:and(?: then)?|but|then|however|yet|although|though|whereas)\b)",
        raw_text,
        maxsplit=32,
    )
    return any(_timeout_signal_from_clause(clause) for clause in clauses)


def _timeout_signal_from_clause(value):
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()
    if not text:
        return False
    # Timeout-looking success/recovery labels and explicit negations are not
    # failures.  Keep the checks token-aware so words from adjacent fields do
    # not accidentally form a signal.
    if re.search(
        r"\b(?:not|no|never|without)\b(?:\s+[a-z0-9]+){0,4}"
        r"\s+(?:timeout|time out|timed out)\b",
        text,
    ):
        return False
    if re.search(
        r"\b(?:did|does|do|is|are|was|were|has|have|had)\s+not\b"
        r"(?:\s+[a-z0-9]+){0,4}\s+(?:timeout|time out|timed out)\b",
        text,
    ):
        return False
    if re.search(
        r"\b(?:timeout (?:was )?(?:recovered|resolved|cleared)|"
        r"(?:recovered|resolved|cleared)(?: from)? timeout)\b",
        text,
    ):
        return False
    tokens = text.split()
    if "timeout" in tokens or "timeouterror" in tokens or "timeoutexpired" in tokens:
        return True
    return any(
        tokens[index : index + 2] in (["time", "out"], ["timed", "out"])
        for index in range(max(0, len(tokens) - 1))
    )


def _timeout_json_text_key(key):
    normalized_key = str(key or "").strip().lower().replace("-", "_")
    return normalized_key in TIMEOUT_JSON_TEXT_KEYS or normalized_key.endswith(
        ("_error", "_errors", "_exception", "_message", "_reason", "_status")
    )


def _timeout_signal_from_json_value(value, *, key="", allow_text=False):
    normalized_key = str(key or "").strip().lower().replace("-", "_")
    if normalized_key in TIMEOUT_JSON_FLAG_KEYS:
        if value is True:
            return True
        if value is False or value is None:
            return False
        if isinstance(value, (int, float)):
            return False
        if isinstance(value, str):
            flag_text = value.strip().lower()
            if flag_text in FALSE_JSON_FLAG_TEXT:
                return False
            if flag_text in TRUE_JSON_FLAG_TEXT:
                return True
            return _timeout_signal_from_text(flag_text)
        return False

    if isinstance(value, dict):
        return any(
            _timeout_signal_from_json_value(
                child,
                key=child_key,
                allow_text=allow_text or _timeout_json_text_key(child_key),
            )
            for child_key, child in value.items()
        )
    if isinstance(value, list):
        return any(
            _timeout_signal_from_json_value(child, allow_text=allow_text)
            for child in value
        )
    if isinstance(value, str) and allow_text:
        return _timeout_signal_from_text(value)
    return False


def _timeout_sql_function_name():
    return f"inkdrop_source_timeout_signal_{uuid.uuid4().hex}"


def provider_timeout_circuit_breakers(
    db_path,
    jobs,
    *,
    now=None,
    window_seconds=0,
    threshold=0,
    cooldown_seconds=0,
    provider_health_map=None,
    limit=1000,
    con=None,
):
    """Return read-only provider timeout cooldowns shared across queue rows."""

    now = time.time() if now is None else now
    window_seconds = _float(window_seconds) or 0
    cooldown_seconds = _float(cooldown_seconds) or 0
    try:
        threshold = int(threshold or 0)
    except Exception:
        threshold = 0
    if window_seconds <= 0 or cooldown_seconds <= 0 or threshold <= 0:
        return {}
    jobs = [job for job in (jobs or []) if isinstance(job, dict)]
    provider_ids = {_job_provider_id(job) for job in jobs if _job_provider_id(job)}
    if not provider_ids:
        return {}
    circuit_keys = set()
    for job in jobs:
        circuit_keys.update(_job_timeout_circuit_keys(job))
    if not circuit_keys:
        return {}
    since = now - window_seconds
    timeout_sql = """
        (
            lower(coalesce(status, '')) like '%timeout%'
            or lower(coalesce(display_phase, '')) like '%timeout%'
            or lower(coalesce(failure_reason, '')) like '%timeout%'
            or lower(coalesce(raw_json, '')) like '%timeout%'
            or lower(coalesce(raw_json, '')) like '%timed_out%'
            or lower(coalesce(raw_json, '')) like '%timed out%'
            or lower(coalesce(raw_json, '')) like '%search_budget_exhausted%'
            or lower(coalesce(raw_json, '')) like '%search budget exhausted%'
            or lower(coalesce(raw_json, '')) like '%source started but did not report%'
        )
    """
    with _borrowed_or_read_con(db_path, con) as attempts_con:
        if not inkdrop_state.table_exists(attempts_con, "source_attempts"):
            return {}
        timeout_function_name = _timeout_sql_function_name()
        attempts_con.create_function(
            timeout_function_name,
            4,
            lambda status, display_phase, failure_reason, raw_json: int(
                _timeout_signal_from_row(
                    {
                        "status": status,
                        "display_phase": display_phase,
                        "failure_reason": failure_reason,
                        "raw_json": raw_json,
                    }
                )
            ),
            deterministic=True,
        )
        try:
            rows = [
                dict(row)
                for row in attempts_con.execute(
                    f"""
                    select lower(coalesce(nullif(provider_id, ''), nullif(source, ''), nullif(provider, ''), 'unknown')) as provider_key,
                           coalesce(status, '') as status,
                           coalesce(display_phase, '') as display_phase,
                           coalesce(failure_reason, '') as failure_reason,
                           coalesce(completed_at, started_at, 0) as activity_at,
                           coalesce(raw_json, '') as raw_json
                    from source_attempts
                    where coalesce(completed_at, started_at, 0) >= ?
                      and {timeout_sql}
                      and {timeout_function_name}(
                            status, display_phase, failure_reason, raw_json
                          ) = 1
                    order by coalesce(completed_at, started_at, 0) desc, id desc
                    limit ?
                    """,
                    (since, _bounded_limit(limit, default=1000, maximum=5000)),
                ).fetchall()
            ]
        finally:
            attempts_con.create_function(timeout_function_name, 4, None)
    buckets = {}
    for row in rows:
        if not _timeout_signal_from_row(row):
            continue
        provider_key = _provider_key(row.get("provider_key"))
        if not provider_key:
            continue
        keys = [provider_key, *_provider_parent_keys(provider_key)]
        for key in keys:
            if key not in circuit_keys:
                continue
            item = buckets.setdefault(
                key,
                {
                    "provider_id": key,
                    "recent_timeout_count": 0,
                    "latest_at": 0.0,
                    "latest_status": "",
                    "latest_reason": "",
                },
            )
            item["recent_timeout_count"] = int(item.get("recent_timeout_count") or 0) + 1
            activity_at = _float(row.get("activity_at")) or 0.0
            if activity_at >= float(item.get("latest_at") or 0):
                item["latest_at"] = activity_at
                item["latest_status"] = row.get("status") or row.get("display_phase") or "timeout"
                item["latest_reason"] = row.get("failure_reason") or row.get("status") or "provider timeout"
    open_by_key = {}
    for key, item in buckets.items():
        latest_at = _float(item.get("latest_at")) or 0.0
        remaining = latest_at + cooldown_seconds - now
        if int(item.get("recent_timeout_count") or 0) < threshold or remaining <= 0:
            continue
        open_by_key[key] = {
            "provider_id": key,
            "kind": PROVIDER_TIMEOUT_CIRCUIT_KIND,
            "status": "provider_timeout",
            "failure_reason": item.get("latest_reason") or "provider timeout",
            "last_attempt_at": latest_at,
            "age_seconds": max(0, now - latest_at),
            "remaining_seconds": max(0, remaining),
            "cooldown_seconds": cooldown_seconds,
            "window_seconds": window_seconds,
            "threshold": threshold,
            "recent_timeout_count": int(item.get("recent_timeout_count") or 0),
            "circuit_provider_id": key,
        }
    out = {}
    for job in jobs:
        provider_id = _job_provider_id(job)
        if not provider_id:
            continue
        matches = [
            open_by_key[key]
            for key in _job_timeout_circuit_keys(job)
            if key in open_by_key
        ]
        if not matches:
            continue
        breaker = sorted(
            matches,
            key=lambda item: (
                -int(item.get("recent_timeout_count") or 0),
                -float(item.get("last_attempt_at") or 0),
                str(item.get("circuit_provider_id") or ""),
            ),
        )[0]
        recovered_health = _provider_recovery_health(
            job,
            provider_health_map,
            breaker.get("last_attempt_at"),
        )
        if recovered_health:
            continue
        row = dict(breaker)
        row["provider_id"] = provider_id
        out[provider_id] = row
    return out


def _fetch_failure_signal_from_row(row):
    row = _dict(row)
    status = _lower(row.get("status"))
    reason = _lower(row.get("failure_reason"))
    if status not in FETCH_FAILURE_CIRCUIT_STATUSES:
        return False
    return reason in FETCH_FAILURE_CIRCUIT_REASONS


def provider_fetch_failure_circuit_breakers(
    db_path,
    jobs,
    *,
    now=None,
    window_seconds=0,
    threshold=0,
    cooldown_seconds=0,
    provider_health_map=None,
    limit=1000,
    con=None,
):
    """Return read-only provider fetch-failure cooldowns shared across queue rows."""

    now = time.time() if now is None else now
    window_seconds = _float(window_seconds) or 0
    cooldown_seconds = _float(cooldown_seconds) or 0
    try:
        threshold = int(threshold or 0)
    except Exception:
        threshold = 0
    if window_seconds <= 0 or cooldown_seconds <= 0 or threshold <= 0:
        return {}
    jobs = [job for job in (jobs or []) if isinstance(job, dict)]
    provider_ids = {_job_provider_id(job) for job in jobs if _job_provider_id(job)}
    if not provider_ids:
        return {}
    circuit_keys = set()
    for job in jobs:
        circuit_keys.update(_job_timeout_circuit_keys(job))
    if not circuit_keys:
        return {}
    since = now - window_seconds
    status_values = sorted(FETCH_FAILURE_CIRCUIT_STATUSES)
    reason_values = sorted(FETCH_FAILURE_CIRCUIT_REASONS)
    with _borrowed_or_read_con(db_path, con) as attempts_con:
        if not inkdrop_state.table_exists(attempts_con, "source_attempts"):
            return {}
        rows = [
            dict(row)
            for row in attempts_con.execute(
                f"""
                select lower(coalesce(nullif(provider_id, ''), nullif(source, ''), nullif(provider, ''), 'unknown')) as provider_key,
                       coalesce(status, '') as status,
                       coalesce(display_phase, '') as display_phase,
                       coalesce(failure_reason, '') as failure_reason,
                       coalesce(completed_at, started_at, 0) as activity_at
                from source_attempts
                where coalesce(completed_at, started_at, 0) >= ?
                  and lower(coalesce(status, '')) in ({",".join("?" for _ in status_values)})
                  and lower(coalesce(failure_reason, '')) in ({",".join("?" for _ in reason_values)})
                order by coalesce(completed_at, started_at, 0) desc, id desc
                limit ?
                """,
                (
                    since,
                    *status_values,
                    *reason_values,
                    _bounded_limit(limit, default=1000, maximum=5000),
                ),
            ).fetchall()
        ]
    buckets = {}
    for row in rows:
        if not _fetch_failure_signal_from_row(row):
            continue
        provider_key = _provider_key(row.get("provider_key"))
        if not provider_key:
            continue
        keys = [provider_key, *_provider_parent_keys(provider_key)]
        for key in keys:
            if key not in circuit_keys:
                continue
            item = buckets.setdefault(
                key,
                {
                    "provider_id": key,
                    "recent_fetch_failure_count": 0,
                    "latest_at": 0.0,
                    "latest_status": "",
                    "latest_reason": "",
                },
            )
            item["recent_fetch_failure_count"] = int(item.get("recent_fetch_failure_count") or 0) + 1
            activity_at = _float(row.get("activity_at")) or 0.0
            if activity_at >= float(item.get("latest_at") or 0):
                item["latest_at"] = activity_at
                item["latest_status"] = row.get("status") or row.get("display_phase") or "provider_unavailable"
                item["latest_reason"] = row.get("failure_reason") or row.get("status") or "provider fetch failure"
    open_by_key = {}
    for key, item in buckets.items():
        latest_at = _float(item.get("latest_at")) or 0.0
        remaining = latest_at + cooldown_seconds - now
        if int(item.get("recent_fetch_failure_count") or 0) < threshold or remaining <= 0:
            continue
        open_by_key[key] = {
            "provider_id": key,
            "kind": PROVIDER_FETCH_FAILURE_CIRCUIT_KIND,
            "status": item.get("latest_status") or "provider_unavailable",
            "failure_reason": item.get("latest_reason") or "provider fetch failure",
            "last_attempt_at": latest_at,
            "age_seconds": max(0, now - latest_at),
            "remaining_seconds": max(0, remaining),
            "cooldown_seconds": cooldown_seconds,
            "window_seconds": window_seconds,
            "threshold": threshold,
            "recent_fetch_failure_count": int(item.get("recent_fetch_failure_count") or 0),
            "circuit_provider_id": key,
        }
    out = {}
    for job in jobs:
        provider_id = _job_provider_id(job)
        if not provider_id:
            continue
        matches = [
            open_by_key[key]
            for key in _job_timeout_circuit_keys(job)
            if key in open_by_key
        ]
        if not matches:
            continue
        breaker = sorted(
            matches,
            key=lambda item: (
                -int(item.get("recent_fetch_failure_count") or 0),
                -float(item.get("last_attempt_at") or 0),
                str(item.get("circuit_provider_id") or ""),
            ),
        )[0]
        recovered_health = _provider_recovery_health(
            job,
            provider_health_map,
            breaker.get("last_attempt_at"),
            recovery_keys=_job_fetch_failure_recovery_health_keys(job),
        )
        if recovered_health:
            continue
        row = dict(breaker)
        row["provider_id"] = provider_id
        out[provider_id] = row
    return out


def _filter_attempt_cooldowns(jobs, cooldowns):
    kept = []
    cooled = []
    cooldowns = cooldowns if isinstance(cooldowns, dict) else {}
    for job in jobs or []:
        if (
            isinstance(job, dict)
            and job.get("job_status") == "ready"
            and _job_provider_id(job) in cooldowns
        ):
            cooled.append(job)
            continue
        kept.append(job)
    return kept, cooled


def _classify_queue_plan(
    row,
    jobs,
    handoffs,
    *,
    now=None,
    attempt_cooldowns=None,
    cooled_jobs=None,
    retryable_failed_handoffs=None,
    retryable_failed_stage_attempts=None,
    force_requested=False,
):
    row = _dict(row)
    now = time.time() if now is None else now
    wanted_id = row.get("wanted_id")
    wanted_status = _lower(row.get("wanted_status"))
    if wanted_id and (not wanted_status or wanted_status in WANTED_TERMINAL_STATUSES):
        return {
            "status": "terminal_wanted",
            "blocker": f"linked Wanted item is {wanted_status or 'missing'}, not actionable",
            "next_action": "Retire this queue row; ordinary acquisition does not run against a satisfied/retired Wanted item.",
            "selected_jobs": [],
            "wanted_status": wanted_status or None,
        }
    jobs = list(jobs or [])
    cooled_jobs = list(cooled_jobs or [])
    attempt_cooldowns = attempt_cooldowns if isinstance(attempt_cooldowns, dict) else {}
    ready_jobs = _selection_ready_jobs(jobs)
    selected_ready_jobs = _primary_ready_jobs(ready_jobs, cooled_jobs=cooled_jobs)
    provider_wait_jobs = [job for job in jobs if job.get("job_status") == "provider_wait"]
    operator_jobs = [job for job in jobs if job.get("job_status") == "operator_required"]
    blocked_jobs = [
        job
        for job in jobs
        if job.get("job_status") not in {"ready", "operator_required"}
    ]
    retry_after = _float(row.get("retry_after"))
    wait_reason = queue_provider_wait_reason(row, now=now)
    retryable_failed_handoffs = [
        task for task in (retryable_failed_handoffs or []) if isinstance(task, dict)
    ]
    retryable_failed_stage_attempts = [
        attempt for attempt in (retryable_failed_stage_attempts or []) if isinstance(attempt, dict)
    ]
    retryable_handoff_recovery_ready = bool(retryable_failed_handoffs and selected_ready_jobs)
    retryable_stage_recovery_ready = bool(retryable_failed_stage_attempts and selected_ready_jobs)

    if handoffs and retryable_stage_recovery_ready:
        return {
            "status": "eligible",
            "blocker": "",
            "next_action": "Retry ready source jobs after a retry-eligible InkDrop staging failure.",
            "selected_jobs": selected_ready_jobs,
            "retryable_failed_stage_recovery": True,
        }
    if handoffs:
        return {
            "status": "active_handoff",
            "blocker": "SLSKD transfer/import stale",
            "next_action": "Wait for active download/import handoff before enqueueing more source work.",
            "selected_jobs": [],
        }
    if (
        wait_reason
        and retry_after is not None
        and retry_after > now
        and not retryable_handoff_recovery_ready
        and not force_requested
    ):
        return {
            "status": "waiting_for_retry",
            "blocker": "provider outage/provider wait",
            "next_action": wait_reason,
            "selected_jobs": [],
        }
    if selected_ready_jobs:
        if retryable_handoff_recovery_ready:
            return {
                "status": "eligible",
                "blocker": "",
                "next_action": "Retry ready source jobs after a retry-eligible downloader handoff failure.",
                "selected_jobs": selected_ready_jobs,
                "retryable_failed_handoff_recovery": True,
            }
        return {
            "status": "eligible",
            "blocker": "",
            "next_action": "Run ready settings-backed source jobs.",
            "selected_jobs": selected_ready_jobs,
        }
    if wait_reason:
        return {
            "status": "provider_wait",
            "blocker": "provider outage/provider wait",
            "next_action": wait_reason,
            "selected_jobs": [],
            # Carried so the park this plan becomes can name the health row
            # that blocked it. The plan itself is all the autopilot's row
            # projection sees -- the jobs stay here -- so without this the
            # attribution the registry already made is lost by the time a
            # reason is written.
            "health_blocking_provider_ids": inkdrop_source_registry.blocking_health_provider_ids(
                provider_wait_jobs
            ),
        }
    if cooled_jobs:
        provider_id = _job_provider_id(cooled_jobs[0])
        cooldown = attempt_cooldowns.get(provider_id) or {}
        remaining = int(cooldown.get("remaining_seconds") or 0)
        if cooldown.get("kind") == PROVIDER_TIMEOUT_CIRCUIT_KIND:
            circuit_provider = cooldown.get("circuit_provider_id") or provider_id
            count = int(cooldown.get("recent_timeout_count") or 0)
            return {
                "status": "waiting_for_retry",
                "blocker": "provider outage/provider wait",
                "next_action": f"{circuit_provider or provider_id or 'provider'} timeout circuit is open after {count} recent timeout(s); retry in about {remaining}s.",
                "selected_jobs": [],
            }
        if cooldown.get("kind") == PROVIDER_FETCH_FAILURE_CIRCUIT_KIND:
            circuit_provider = cooldown.get("circuit_provider_id") or provider_id
            count = int(cooldown.get("recent_fetch_failure_count") or 0)
            reason = cooldown.get("failure_reason") or "fetch failure"
            return {
                "status": "waiting_for_retry",
                "blocker": "provider outage/provider wait",
                "next_action": f"{circuit_provider or provider_id or 'provider'} fetch-failure circuit is open after {count} recent {reason} failure(s); retry in about {remaining}s.",
                "selected_jobs": [],
            }
        return {
            "status": "waiting_for_retry",
            "blocker": "source-worker cooldown",
            "next_action": f"{provider_id or 'source'} was checked recently; retry in about {remaining}s.",
            "selected_jobs": [],
        }
    if provider_wait_jobs:
        reasons = [
            str(job.get("reason") or "").strip()
            for job in provider_wait_jobs
            if str(job.get("reason") or "").strip()
        ]
        return {
            "status": "provider_wait",
            "blocker": "provider outage/provider wait",
            "next_action": reasons[0] if reasons else "Provider health is limiting this source.",
            "selected_jobs": [],
            "health_blocking_provider_ids": inkdrop_source_registry.blocking_health_provider_ids(
                provider_wait_jobs
            ),
        }
    if operator_jobs:
        return {
            "status": "manual_operator_required",
            "blocker": "manual review truly needed",
            "next_action": "Operator payload is required before this source can produce attempts.",
            "selected_jobs": operator_jobs,
        }
    if not jobs:
        return {
            "status": "blocked_no_jobs",
            "blocker": "no source attempts",
            "next_action": "No enabled implemented source jobs matched the provider/settings filters.",
            "selected_jobs": [],
        }
    return {
        "status": "blocked_no_jobs",
        "blocker": "source coverage gap",
        "next_action": "All matching source jobs are blocked or not executable.",
        "selected_jobs": [],
    }


def source_worker_queue_plan(
    db_path,
    *,
    limit=50,
    queue_ids=None,
    states=None,
    due_only=False,
    include_operator=True,
    include_blocked=False,
    provider_ids=None,
    job_limit=20,
    attempt_cooldown_seconds=0,
    provider_timeout_window_seconds=0,
    provider_timeout_threshold=0,
    provider_timeout_cooldown_seconds=0,
    provider_fetch_failure_window_seconds=0,
    provider_fetch_failure_threshold=0,
    provider_fetch_failure_cooldown_seconds=0,
    now=None,
):
    """Return a read-only queue plan for the settings-backed source worker."""

    now = time.time() if now is None else now
    requested_queue_ids = {
        str(value).strip() for value in _list(queue_ids) if str(value or "").strip()
    }
    plans = []
    provider_timeout_circuit_cache = {}
    provider_fetch_failure_circuit_cache = {}
    # One read connection serves the whole pass. The per-queue lookups the loop
    # below used to run row by row (handoffs, cooldowns, attempt history, queue
    # hydration, provider health) are prefetched here as bulk queries so a
    # 300-row scan does not open hundreds of SQLite connections against the
    # shared state DB before the first provider request.
    with inkdrop_state.connect_read(db_path) as con:
        rows = _queue_rows(
            db_path,
            limit=limit,
            queue_ids=queue_ids,
            states=states,
            media_types=[] if queue_ids else _provider_scope_media_types(provider_ids),
            excluded_media_types=[] if queue_ids else _provider_scope_excluded_media_types(provider_ids),
            due_only=due_only,
            now=now,
            con=con,
        )
        row_queue_ids = [row.get("id") for row in rows]
        latest_attempts = latest_source_attempts_by_queue_id(db_path, row_queue_ids, con=con)
        handoffs_by_queue = active_handoff_tasks_by_queue_id(con, row_queue_ids, now=now)
        stale_handoffs_by_queue = stale_handoff_tasks_by_queue_id(con, row_queue_ids, now=now)
        retryable_failed_by_queue = retryable_failed_handoff_tasks_by_queue_id(con, row_queue_ids, now=now)
        stage_cutoffs_by_queue = {
            queue_id: _stageable_handoff_cutoff(handoffs_by_queue.get(queue_id) or [])
            for queue_id in row_queue_ids
        }
        retryable_stage_by_queue = retryable_failed_stage_attempts_by_queue_id(
            con,
            stage_cutoffs_by_queue,
            now=now,
        )
        cooldown_seconds = _float(attempt_cooldown_seconds)
        cooldown_rows_by_queue = recent_source_attempt_rows_by_queue_id(
            con,
            row_queue_ids,
            now=now,
            cooldown_seconds=attempt_cooldown_seconds,
        )
        history_rows_by_queue = source_attempt_history_rows_by_queue_id(con, row_queue_ids)
        provider_health_map = {}
        if inkdrop_state.table_exists(con, "history_events"):
            provider_health_map = inkdrop_state.latest_provider_health_map(con)
        # settings_snapshot/registry are identical for every row in this pass
        # (both depend only on the pass-wide provider_health_map above, not on
        # any one queue row), so build them once instead of once per row --
        # this used to be the dominant cost of planning a normal-sized pass.
        settings_snapshot = inkdrop_state.settings_snapshot(db_path)
        registry_rows = inkdrop_source_registry.registry_from_settings_snapshot(
            settings_snapshot,
            include_disabled=True,
            provider_health_map=provider_health_map,
        )
        hydrated_queue_items = coordinator.queue_items_by_id(db_path, row_queue_ids, con=con)
        singleton_contexts = coordinator.singleton_issue_contexts_by_series_id(
            db_path,
            sorted(
                {
                    str((item or {}).get("series_id") or "").strip()
                    for item in hydrated_queue_items.values()
                }
                | {str(row.get("series_id") or "").strip() for row in rows}
            ),
            now=now,
            con=con,
        )
        for row in rows:
            queue_id = row.get("id")
            series_id = str(row.get("series_id") or "").strip()
            handoffs = handoffs_by_queue.get(queue_id) or []
            stale_handoffs = stale_handoffs_by_queue.get(queue_id) or []
            retryable_failed_handoffs = retryable_failed_by_queue.get(queue_id) or []
            retryable_failed_stages = retryable_stage_by_queue.get(queue_id) or []
            jobs_result = coordinator.source_jobs_for_queue(
                db_path,
                queue_id,
                include_operator=include_operator,
                include_blocked=True,
                provider_ids=provider_ids,
                job_limit=job_limit,
                queue=hydrated_queue_items.get(queue_id),
                provider_health_map=provider_health_map,
                settings_snapshot=settings_snapshot,
                registry_rows=registry_rows,
                singleton_context=singleton_contexts.get(series_id),
                con=con,
            )
            all_jobs = list(jobs_result.get("jobs") or []) if jobs_result.get("ok") else []
            jobs = _jobs_visible_for_selection(all_jobs, include_blocked=include_blocked)
            job_provider_ids = {
                _job_provider_id(job)
                for job in jobs
                if isinstance(job, dict) and _job_provider_id(job)
            }
            if cooldown_seconds is None or cooldown_seconds <= 0 or not job_provider_ids:
                attempt_cooldowns = {}
            else:
                attempt_cooldowns = _cooldowns_from_recent_attempt_rows(
                    cooldown_rows_by_queue.get(str(queue_id or "").strip()) or [],
                    job_provider_ids,
                    now=now,
                    seconds=cooldown_seconds,
                )
            if job_provider_ids:
                attempt_history = _history_counts_from_attempt_rows(
                    history_rows_by_queue.get(str(queue_id or "").strip()) or [],
                    job_provider_ids,
                )
            else:
                attempt_history = {}
            circuit_cache_key = tuple(
                sorted(
                    {
                        key
                        for job in jobs
                        if isinstance(job, dict)
                        for key in _job_timeout_circuit_keys(job)
                        if key
                    }
                )
            )
            if circuit_cache_key in provider_timeout_circuit_cache:
                provider_timeout_circuits = {
                    key: dict(value)
                    for key, value in provider_timeout_circuit_cache.get(circuit_cache_key, {}).items()
                }
            else:
                provider_timeout_circuits = provider_timeout_circuit_breakers(
                    db_path,
                    jobs,
                    now=now,
                    window_seconds=provider_timeout_window_seconds,
                    threshold=provider_timeout_threshold,
                    cooldown_seconds=provider_timeout_cooldown_seconds,
                    provider_health_map=provider_health_map,
                    con=con,
                )
                provider_timeout_circuit_cache[circuit_cache_key] = {
                    key: dict(value)
                    for key, value in provider_timeout_circuits.items()
                }
            attempt_cooldowns.update(provider_timeout_circuits)
            if circuit_cache_key in provider_fetch_failure_circuit_cache:
                provider_fetch_failure_circuits = {
                    key: dict(value)
                    for key, value in provider_fetch_failure_circuit_cache.get(circuit_cache_key, {}).items()
                }
            else:
                provider_fetch_failure_circuits = provider_fetch_failure_circuit_breakers(
                    db_path,
                    jobs,
                    now=now,
                    window_seconds=provider_fetch_failure_window_seconds,
                    threshold=provider_fetch_failure_threshold,
                    cooldown_seconds=provider_fetch_failure_cooldown_seconds,
                    provider_health_map=provider_health_map,
                    con=con,
                )
                provider_fetch_failure_circuit_cache[circuit_cache_key] = {
                    key: dict(value)
                    for key, value in provider_fetch_failure_circuits.items()
                }
            attempt_cooldowns.update(provider_fetch_failure_circuits)
            runnable_jobs, cooled_jobs = _filter_attempt_cooldowns(jobs, attempt_cooldowns)
            classification = _classify_queue_plan(
                row,
                runnable_jobs,
                handoffs,
                now=now,
                attempt_cooldowns=attempt_cooldowns,
                cooled_jobs=cooled_jobs,
                retryable_failed_handoffs=retryable_failed_handoffs,
                retryable_failed_stage_attempts=retryable_failed_stages,
                force_requested=bool(requested_queue_ids and not due_only and queue_id in requested_queue_ids),
            )
            wanted_item = jobs_result.get("wanted_item") or coordinator.wanted_item_from_queue(
                row,
                db_path=db_path,
                singleton_context=singleton_contexts.get(series_id),
                con=con,
            )
            selected_jobs = classification.pop("selected_jobs", [])
            provider_jobs = _provider_attempt_jobs(
                all_jobs,
                selected_jobs=selected_jobs,
                cooled_jobs=cooled_jobs,
            )
            visible_attempt_cooldowns = _visible_attempt_cooldowns(attempt_cooldowns, provider_jobs)
            visible_cooled_jobs = _visible_cooled_jobs(cooled_jobs, provider_jobs)
            provider_plan = provider_attempt_plan(
                provider_jobs,
                selected_jobs=selected_jobs,
                attempt_cooldowns=visible_attempt_cooldowns,
                attempt_history=attempt_history,
            )
            provider_history_counts = {
                provider_id: int(history.get("attempt_count") or 0)
                for provider_id, history in attempt_history.items()
            }
            provider_terminal_history_counts = {
                provider_id: int(history.get("terminal_attempt_count") or 0)
                for provider_id, history in attempt_history.items()
            }
            provider_real_history_counts = {
                provider_id: int(history.get("real_attempt_count") or 0)
                for provider_id, history in attempt_history.items()
            }
            provider_last_real_attempt_at = {
                provider_id: float(history.get("last_real_attempt_at") or 0.0)
                for provider_id, history in attempt_history.items()
            }
            plans.append(
                {
                    "source_worker_scheduler_contract_version": CONTRACT_VERSION,
                    "queue_id": queue_id,
                    "wanted_id": row.get("wanted_id"),
                    "series_id": row.get("series_id"),
                    "issue_id": row.get("issue_id"),
                    "series": row.get("series"),
                    "issue_number": row.get("issue_number"),
                    "state": row.get("state"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "current_source": row.get("current_source"),
                    "display_phase": row.get("display_phase"),
                    "outcome": row.get("outcome"),
                    "retry_after": row.get("retry_after"),
                    "retry_after_iso": row.get("retry_after_iso"),
                    "provider_status_provider": row.get("provider_status_provider"),
                    "provider_status_phase": row.get("provider_status_phase"),
                    "provider_status_state": row.get("provider_status_state"),
                    "last_event": row.get("last_event"),
                    "series_initial_search_priority_at": row.get("series_initial_search_priority_at"),
                    inkdrop_queue_fairness.LANE_FIELD: row.get("queue_lane"),
                    inkdrop_queue_fairness.LANE_RANK_FIELD: row.get("queue_lane_rank"),
                    inkdrop_queue_fairness.AGEING_SCORE_FIELD: row.get("ageing_score"),
                    inkdrop_queue_fairness.STALL_SECONDS_FIELD: row.get("stall_seconds"),
                    inkdrop_queue_fairness.REAL_ATTEMPT_FIELD: row.get("real_attempt_count"),
                    "series_latest_source_attempt_at": row.get("series_latest_source_attempt_at"),
                    "scheduler_rank": row.get("scheduler_rank"),
                    "series_queue_round": row.get("series_queue_round"),
                    "aged_zero_provider_coverage_reserve": row.get("aged_zero_provider_coverage_reserve"),
                    "wanted_item": wanted_item,
                    "all_job_status_counts": _job_counts(all_jobs),
                    "job_status_counts": _job_counts(jobs),
                    "job_summary": jobs_result.get("summary") or {},
                    "provider_attempt_plan": provider_plan,
                    "provider_attempt_counts": _provider_attempt_counts(provider_plan),
                    "source_worker_provider_attempt_counts": provider_history_counts,
                    "source_worker_terminal_provider_attempt_counts": provider_terminal_history_counts,
                    "source_worker_real_provider_attempt_counts": provider_real_history_counts,
                    "source_worker_last_real_provider_attempt_at": provider_last_real_attempt_at,
                    "latest_source_attempt": latest_attempts.get(queue_id) or {},
                    "jobs_available": len(jobs),
                    "all_jobs_available": len(all_jobs),
                    "jobs_after_cooldown": len(runnable_jobs),
                    "source_worker_cooldown_count": len(visible_cooled_jobs),
                    "provider_timeout_circuit_count": sum(
                        1
                        for cooldown in visible_attempt_cooldowns.values()
                        if isinstance(cooldown, dict)
                        and cooldown.get("kind") == PROVIDER_TIMEOUT_CIRCUIT_KIND
                    ),
                    "provider_fetch_failure_circuit_count": sum(
                        1
                        for cooldown in visible_attempt_cooldowns.values()
                        if isinstance(cooldown, dict)
                        and cooldown.get("kind") == PROVIDER_FETCH_FAILURE_CIRCUIT_KIND
                    ),
                    "source_worker_cooldowns": list(visible_attempt_cooldowns.values()),
                    "selected_provider_ids": _selected_provider_ids(selected_jobs),
                    "active_handoff_count": len(handoffs),
                    "active_handoffs": handoffs,
                    "stale_handoff_count": len(stale_handoffs),
                    "stale_handoffs": stale_handoffs,
                    "retryable_failed_handoff_count": len(retryable_failed_handoffs),
                    "retryable_failed_handoffs": retryable_failed_handoffs,
                    "retryable_failed_stage_attempt_count": len(retryable_failed_stages),
                    "retryable_failed_stage_attempts": retryable_failed_stages,
                    "mutates_database": False,
                    "mutates_filesystem": False,
                    "forced_queue_admission": bool(
                        requested_queue_ids and not due_only and queue_id in requested_queue_ids
                    ),
                    **classification,
                }
            )
    summary = source_worker_queue_plan_summary(plans)
    return {
        "source_worker_scheduler_contract_version": CONTRACT_VERSION,
        "ok": True,
        "dry_run": True,
        "mutates_database": False,
        "mutates_filesystem": False,
        "due_only": bool(due_only),
        "limit": _bounded_limit(limit),
        "attempt_cooldown_seconds": _float(attempt_cooldown_seconds) or 0,
        "provider_timeout_window_seconds": _float(provider_timeout_window_seconds) or 0,
        "provider_timeout_threshold": int(provider_timeout_threshold or 0),
        "provider_timeout_cooldown_seconds": _float(provider_timeout_cooldown_seconds) or 0,
        "provider_fetch_failure_window_seconds": _float(provider_fetch_failure_window_seconds) or 0,
        "provider_fetch_failure_threshold": int(provider_fetch_failure_threshold or 0),
        "provider_fetch_failure_cooldown_seconds": _float(provider_fetch_failure_cooldown_seconds) or 0,
        "queue_count": len(plans),
        "summary": summary,
        "plans": plans,
    }


def source_worker_queue_plan_summary(plans):
    plans = list(plans or [])
    by_status = {}
    by_blocker = {}
    provider_attempt_states = {}
    selected_providers = {}
    for plan in plans:
        status = str((plan or {}).get("status") or "unknown")
        blocker = str((plan or {}).get("blocker") or "none")
        by_status[status] = by_status.get(status, 0) + 1
        by_blocker[blocker] = by_blocker.get(blocker, 0) + 1
        for state, count in ((plan or {}).get("provider_attempt_counts") or {}).items():
            provider_attempt_states[state] = provider_attempt_states.get(state, 0) + int(count or 0)
        for provider_id in (plan or {}).get("selected_provider_ids") or []:
            selected_providers[provider_id] = selected_providers.get(provider_id, 0) + 1
    return {
        "total": len(plans),
        "by_status": dict(sorted(by_status.items())),
        "by_blocker": dict(sorted(by_blocker.items())),
        "eligible": sum(1 for plan in plans if plan.get("status") == "eligible"),
        "active_handoff": sum(1 for plan in plans if plan.get("status") == "active_handoff"),
        "provider_wait": sum(1 for plan in plans if plan.get("status") == "provider_wait"),
        "waiting_for_retry": sum(1 for plan in plans if plan.get("status") == "waiting_for_retry"),
        "manual_operator_required": sum(1 for plan in plans if plan.get("status") == "manual_operator_required"),
        "blocked_no_jobs": sum(1 for plan in plans if plan.get("status") == "blocked_no_jobs"),
        "source_worker_cooldowns": sum(int(plan.get("source_worker_cooldown_count") or 0) for plan in plans),
        "provider_timeout_circuits": sum(int(plan.get("provider_timeout_circuit_count") or 0) for plan in plans),
        "provider_fetch_failure_circuits": sum(int(plan.get("provider_fetch_failure_circuit_count") or 0) for plan in plans),
        "stale_handoffs": sum(int(plan.get("stale_handoff_count") or 0) for plan in plans),
        "retryable_failed_handoffs": sum(int(plan.get("retryable_failed_handoff_count") or 0) for plan in plans),
        "retryable_failed_stage_attempts": sum(int(plan.get("retryable_failed_stage_attempt_count") or 0) for plan in plans),
        "provider_attempt_states": dict(sorted(provider_attempt_states.items())),
        "selected_providers": dict(sorted(selected_providers.items())),
    }
