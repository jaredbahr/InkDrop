"""Dry-run-first batch runner for settings-backed InkDrop source work.

The batch runner consumes the read-only scheduler plan and only forwards
eligible queue rows, plus operator-required rows that already have explicit
operator payloads, to the queue-level coordinator. It does not choose sources
directly; provider enablement and implementation gates stay in settings.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import time
import uuid
from datetime import date, datetime, timedelta

import inkdrop_source_worker_coordinator as coordinator
import inkdrop_source_worker_scheduler as scheduler
import inkdrop_suwayomi_managed_folder as suwayomi_managed_folder
import inkdrop_state


CONTRACT_VERSION = 1
RUNTIME_CLEANUP_SECONDS = 30
HTTP_PROVIDER_FIXED_RUNTIME_SECONDS = 8
HTTP_REQUEST_RUNTIME_ESTIMATE_SECONDS = 12
HTTP_REQUEST_RUNTIME_TIMEOUT_MARGIN_SECONDS = 5
HTTP_PROVIDER_RUNTIME_MIN_SECONDS = 25
# Sanity ceiling only. The estimate must track the plan's real request count:
# clamping it to 240s priced a 44-request MangaDex volume pass as ~10 requests,
# which admitted it into slots it could never finish.
HTTP_PROVIDER_RUNTIME_MAX_SECONDS = 1800
RUNTIME_BUDGET_COST_BUCKET_SECONDS = 60
RUNTIME_BUDGET_STARVED_HEAD_SLOTS = 3
RUNTIME_BUDGET_STARVED_MIN_AGE_SECONDS = 6 * 60 * 60
SOURCE_RETRY_STARVED_HEAD_SLOTS = 3
SOURCE_RETRY_STARVED_MIN_AGE_SECONDS = 6 * 60 * 60
COMIC_PACK_TRUSTED_PAIR_RUNTIME_MAX_SECONDS = 180
QUEUE_FILL_SCAN_MULTIPLIER = 4
QUEUE_FILL_SCAN_MAX = 500
AGED_ZERO_PROVIDER_COVERAGE_RESERVE_FIELD = "aged_zero_provider_coverage_reserve"
SERIES_BACKLOG_COUNT_FIELD = "source_worker_series_backlog_count"
SERIES_ROUND_INDEX_FIELD = "source_worker_series_round_index"
AUTOMATED_ATTEMPT_COUNT_FIELD = "source_worker_automated_attempt_count"
COMIC_PACK_BACKLOG_PRIORITY_FIELD = "source_worker_comic_pack_backlog_priority"
DIRECT_LOCAL_PAGE_PACK_RUNTIME_HEAD_FIELD = "source_worker_direct_local_page_pack_runtime_head"
INITIAL_SEARCH_PRIORITY_FIELD = "source_worker_initial_search_priority"
INITIAL_SEARCH_QA_OBSERVATION_ID = "QA-20260718-NEW-SERIES-INITIAL-SEARCH"
MISSING_RECOVERY_COHORT_FIELD = "source_worker_missing_recovery_cohort"
MISSING_RECOVERY_COHORTS = (
    "handoff_transfer_recovery",
    "ordinary_new",
    "never_no_call",
    "result_candidate_loss",
    "import_reader_recovery",
)
MISSING_RECOVERY_NEVER_TRIED_AGE_SECONDS = 24 * 60 * 60

PROVIDER_RUNTIME_ESTIMATES = {
    "comicscodes": 120,
    "mangadex": 120,
    "prowlarr": 120,
    "rss": 120,
    "slskd": 240,
}

PROVIDER_PREFIX_RUNTIME_ESTIMATES = (
    ("generic_rss", 120),
    ("generic_torrent", 120),
    ("prowlarr_", 120),
    ("rss_", 120),
)

AGGREGATE_PROVIDER_LANES = {"prowlarr", "rss"}
NON_AUTOMATED_ATTEMPT_PROVIDER_IDS = {
    "download_client",
    "failed_retry",
    "importer",
    "kavita",
    "qbittorrent",
    "queue",
    "queue_cleanup",
    "sabnzbd",
    "slskd",
    "source_ladder",
}
PROWLARR_AGGREGATE_PROVIDER_ID = "prowlarr"
PROWLARR_CHILD_PROVIDER_PREFIX = "prowlarr_"
PROWLARR_CHILD_LANE_SLICE_KIND = "prowlarr_child_lane_slice"
PROWLARR_AGGREGATE_COMIC_CHILD_PROMOTION_KIND = "prowlarr_aggregate_comic_child_promotion"
PROVIDER_PASS_FAILURE_BUDGET_SLICE_KIND = "provider_pass_failure_budget_slice"
PROVIDER_PASS_FAILURE_BUDGET_SKIP_KIND = "provider_pass_failure_budget_skip"
PROVIDER_PASS_FAILURE_LIMIT = 2
PROVIDER_PASS_FAILURE_STATUSES = {
    "provider_unavailable",
    "provider_wait",
}
MANGA_PROWLARR_CHILD_PROVIDER_IDS = {
    "prowlarr_nyaa",
    "prowlarr_tokyo_toshokan_manga",
}
COMIC_PACK_PROWLARR_CHILD_PROVIDER_IDS = {
    "prowlarr_dognzb_comics",
    "prowlarr_kat_comics",
    "prowlarr_pirate_bay_comics",
    "prowlarr_torrentdownload_comics",
    "prowlarr_torrentleech_comics",
}
COMIC_PACK_PROWLARR_CHILD_LANE_LIMIT = 1
COMIC_PACK_PROWLARR_CHILD_LANE_LIMIT_MAX = len(COMIC_PACK_PROWLARR_CHILD_PROVIDER_IDS)
COMIC_PACK_BACKLOG_HEAD_SLOTS = 1
COMIC_PACK_RUNTIME_PRIORITY_MAX_AGE_DAYS = 730
LOCAL_PAGE_PACK_FAST_LANE_PROVIDER_IDS = {
    "suwayomi",
}
LOCAL_PAGE_PACK_FAST_LANE_SLICE_KIND = "local_page_pack_fast_lane_slice"
LOCAL_PAGE_PACK_SIBLING_ROTATION_SLICE_KIND = "local_page_pack_sibling_rotation_slice"
LOCAL_PAGE_PACK_DIRECT_RUNTIME_HEAD_SLICE_KIND = "local_page_pack_direct_runtime_head_slice"
LOCAL_PAGE_PACK_FOLLOWUP_ELIGIBLE_SLOTS = 2
LOCAL_PAGE_PACK_RUNTIME_HEAD_SLOTS = 6
LOCAL_PAGE_PACK_DIRECT_RUNTIME_HEAD_SLOTS = 1
PENDING_DIRECT_STAGE_LIMIT_MAX = 100
# Recover one accepted client task per batch. Scan past work that is temporarily
# deferred by a resource limit so one large item cannot block smaller accepted
# downloads that still fit the active limits.
PENDING_DOWNLOAD_CLIENT_HANDOFF_LIMIT_MAX = 1
PENDING_DOWNLOAD_CLIENT_HANDOFF_SCAN_LIMIT_MAX = 20
RSS_PROVIDER_ID = "rss"
RSS_FAST_LANE_SLICE_KIND = "rss_fast_lane_slice"
RSS_FAST_LANE_MAX_AGE_DAYS = 180
MANAGED_FOLDER_PROVIDER_IDS = {"suwayomi_managed_folder"}
HTTP_RUNTIME_ADAPTER_FAMILIES = {
    "direct_archive_api",
    "direct_catalog",
    "feed_or_list_source",
    "indexer_discovery",
    "mangadex_api",
    "manga_api",
    "newznab_indexer",
    "opds_catalog",
    "prowlarr_indexer",
    "rss_detail_direct_feed",
    "rss_detail_probe_feed",
    "rss_direct_feed",
    "rss_feed",
    "suwayomi_api",
    "torznab_indexer",
    "torrent_detail_rss_feed",
    "torrent_rss_feed",
}
SOURCE_HTTP_CACHEABLE_METHODS = {"GET", "HEAD"}


def _list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _sorted_dict(value):
    if not isinstance(value, dict):
        return {}
    return {str(key): value[key] for key in sorted(value, key=lambda item: str(item))}


def _source_http_cache_key(request):
    request = request if isinstance(request, dict) else {}
    if request.get("cacheable") is False:
        return ""
    method = str(request.get("method") or "GET").strip().upper()
    if method not in SOURCE_HTTP_CACHEABLE_METHODS:
        return ""
    key_payload = {
        "method": method,
        "url": str(request.get("url") or "").strip(),
        "params": _sorted_dict(request.get("params")),
        "secret_params": _sorted_dict(request.get("secret_params")),
        "headers": _sorted_dict(request.get("headers")),
        "allowed_hosts": sorted(str(value) for value in _list(request.get("allowed_hosts")) if str(value or "").strip()),
        "allow_truncated": bool(request.get("allow_truncated")),
        "max_bytes": request.get("max_bytes"),
    }
    if not key_payload["url"]:
        return ""
    try:
        return json.dumps(key_payload, sort_keys=True, ensure_ascii=True, default=str)
    except Exception:
        return ""


def _copy_source_http_response(response):
    try:
        return copy.deepcopy(response)
    except Exception:
        return response


def _cached_source_http_get(http_get):
    if not callable(http_get):
        return http_get, {"enabled": False, "hits": 0, "misses": 0, "bypassed": 0, "entries": 0}
    cache = {}
    stats = {"enabled": True, "hits": 0, "misses": 0, "bypassed": 0, "entries": 0}

    def _client(request):
        key = _source_http_cache_key(request)
        if not key:
            stats["bypassed"] += 1
            return http_get(request)
        if key in cache:
            stats["hits"] += 1
            return _copy_source_http_response(cache[key])
        stats["misses"] += 1
        response = http_get(request)
        cache[key] = _copy_source_http_response(response)
        stats["entries"] = len(cache)
        return response

    return _client, stats


def _bounded_limit(limit, default=50, maximum=500):
    try:
        value = int(limit)
    except Exception:
        value = default
    return max(1, min(value, maximum))


def _env_enabled(name, default=True):
    value = str(os.environ.get(name, "1" if default else "0") or "").strip().lower()
    return value not in {"0", "false", "off", "no", "disabled"}


def _missing_recovery_max_per_cohort(limit):
    try:
        value = int(os.environ.get("INKDROP_MISSING_RECOVERY_MAX_PER_COHORT", "2") or 2)
    except (TypeError, ValueError):
        value = 2
    return max(1, min(value, max(1, int(limit or 1))))


def _missing_recovery_enabled():
    return _env_enabled("INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED", False) and _env_enabled(
        "INKDROP_MISSING_RECOVERY_ENABLED", False
    )


def _queue_scan_limit(queue_limit, *, eligible_limit=None, queue_ids=None, due_only=True):
    """Scan past blocked head-of-queue rows while preserving the selected batch cap."""

    base = _bounded_limit(queue_limit)
    queue_ids = [value for value in _list(queue_ids) if str(value or "").strip()]
    if queue_ids or not due_only:
        return base
    if eligible_limit not in (None, ""):
        fill_target = _bounded_limit(eligible_limit, default=base) * QUEUE_FILL_SCAN_MULTIPLIER
    else:
        fill_target = base * QUEUE_FILL_SCAN_MULTIPLIER
    return _bounded_limit(max(base, fill_target), default=base, maximum=QUEUE_FILL_SCAN_MAX)


def _float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _has_operator_payload(plan, operator_payloads=None):
    operator_payloads = operator_payloads if isinstance(operator_payloads, dict) else {}
    if not operator_payloads:
        return False
    selected_provider_ids = [
        str(value).strip()
        for value in _list((plan or {}).get("selected_provider_ids"))
        if str(value or "").strip()
    ]
    for provider_id in selected_provider_ids:
        if operator_payloads.get(provider_id) not in (None, "", [], {}):
            return True
    return False


def _series_key(plan):
    plan = plan if isinstance(plan, dict) else {}
    return (
        str(plan.get("series_id") or "").strip()
        or str(plan.get("series") or "").strip().lower()
        or str(plan.get("queue_id") or "").strip()
    )


def _selected_provider_ids(plan):
    return [
        str(value).strip().lower()
        for value in _list((plan or {}).get("selected_provider_ids"))
        if str(value or "").strip()
    ]


def _provider_pass_failure_count(value):
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _provider_pass_failure_limit():
    return max(1, int(PROVIDER_PASS_FAILURE_LIMIT or 1))


def _provider_pass_failure_signal(row):
    row = row if isinstance(row, dict) else {}
    status = str(row.get("result_status") or row.get("status") or "").strip().lower()
    return status in PROVIDER_PASS_FAILURE_STATUSES


def _provider_pass_failure_counts_from_result(result):
    result = result if isinstance(result, dict) else {}
    counts = {}
    reasons = {}
    for row in result.get("job_results") or []:
        if not isinstance(row, dict) or not _provider_pass_failure_signal(row):
            continue
        provider_id = str(row.get("provider_id") or "").strip().lower()
        if not provider_id:
            continue
        counts[provider_id] = counts.get(provider_id, 0) + 1
        reason = str(row.get("reason") or row.get("failure_reason") or row.get("result_status") or "").strip()
        if reason:
            reasons[provider_id] = reason
    return counts, reasons


def _increment_provider_pass_failures(provider_failures, provider_failure_reasons, result):
    provider_failures = provider_failures if isinstance(provider_failures, dict) else {}
    provider_failure_reasons = provider_failure_reasons if isinstance(provider_failure_reasons, dict) else {}
    counts, reasons = _provider_pass_failure_counts_from_result(result)
    for provider_id, count in counts.items():
        provider_failures[provider_id] = _provider_pass_failure_count(provider_failures.get(provider_id)) + count
        if reasons.get(provider_id):
            provider_failure_reasons[provider_id] = reasons[provider_id]
    return provider_failures


def _provider_pass_failure_budget_for_plan(plan, provider_failures, provider_failure_reasons=None):
    provider_failures = provider_failures if isinstance(provider_failures, dict) else {}
    provider_failure_reasons = provider_failure_reasons if isinstance(provider_failure_reasons, dict) else {}
    selected_provider_ids = _selected_provider_ids(plan)
    if not selected_provider_ids:
        return dict(plan or {}), {}
    limit = _provider_pass_failure_limit()
    exhausted = [
        provider_id
        for provider_id in selected_provider_ids
        if _provider_pass_failure_count(provider_failures.get(provider_id)) >= limit
    ]
    if not exhausted:
        return dict(plan or {}), {}
    remaining = [provider_id for provider_id in selected_provider_ids if provider_id not in set(exhausted)]
    evidence = {
        "limit": limit,
        "provider_failure_counts": {
            provider_id: _provider_pass_failure_count(provider_failures.get(provider_id))
            for provider_id in exhausted
        },
        "exhausted_provider_ids": exhausted,
        "remaining_provider_ids": remaining,
        "provider_failure_reasons": {
            provider_id: provider_failure_reasons.get(provider_id)
            for provider_id in exhausted
            if provider_failure_reasons.get(provider_id)
        },
    }
    plan = dict(plan or {})
    if not remaining:
        skipped = dict(plan)
        skipped["provider_pass_failure_budget"] = dict(evidence, kind=PROVIDER_PASS_FAILURE_BUDGET_SKIP_KIND)
        skipped["provider_pass_failure_budget_skip"] = True
        return None, skipped
    plan["selected_provider_ids"] = remaining
    if isinstance(plan.get("provider_attempt_plan"), list):
        plan["provider_attempt_plan"] = _slice_provider_attempt_plan(
            plan.get("provider_attempt_plan"),
            remaining,
            exhausted,
            deferred_attempt_state=PROVIDER_PASS_FAILURE_BUDGET_SLICE_KIND,
            deferred_reason="Provider hit the source-worker pass failure budget; trying remaining selected lanes.",
        )
        _refresh_provider_attempt_counts(plan)
    previous_slice = plan.get("source_worker_slice") if isinstance(plan.get("source_worker_slice"), dict) else {}
    slice_payload = dict(evidence, kind=PROVIDER_PASS_FAILURE_BUDGET_SLICE_KIND)
    if previous_slice:
        slice_payload["previous_slice"] = previous_slice
    plan["source_worker_slice"] = slice_payload
    return plan, slice_payload


def _local_page_pack_provider_ids(plan):
    return sorted(set(_selected_provider_ids(plan)).intersection(LOCAL_PAGE_PACK_FAST_LANE_PROVIDER_IDS))


def _is_local_page_pack_plan(plan):
    return bool(_local_page_pack_provider_ids(plan))


def _local_page_pack_history_count(plan):
    provider_counts = (plan or {}).get("source_worker_provider_attempt_counts")
    provider_counts = provider_counts if isinstance(provider_counts, dict) else {}
    terminal_counts = (plan or {}).get("source_worker_terminal_provider_attempt_counts")
    terminal_counts = terminal_counts if isinstance(terminal_counts, dict) else {}
    return sum(
        _provider_history_count(provider_counts, provider_id)
        + _provider_history_count(terminal_counts, provider_id)
        for provider_id in _local_page_pack_provider_ids(plan)
    )


def _has_local_page_pack_siblings(plan):
    provider_ids = _selected_provider_ids(plan)
    local_provider_ids = set(_local_page_pack_provider_ids(plan))
    return bool(local_provider_ids) and any(provider_id not in local_provider_ids for provider_id in provider_ids)


def _is_local_page_pack_followup_candidate_plan(plan):
    return _has_local_page_pack_siblings(plan) and _local_page_pack_history_count(plan) > 0


def _is_local_page_pack_sibling_rotation_plan(plan):
    slice_payload = (plan or {}).get("source_worker_slice")
    return isinstance(slice_payload, dict) and slice_payload.get("kind") == LOCAL_PAGE_PACK_SIBLING_ROTATION_SLICE_KIND


def _is_runtime_local_page_pack_plan(plan):
    return _is_local_page_pack_plan(plan) or _is_local_page_pack_sibling_rotation_plan(plan)


def _would_keep_direct_local_page_pack_after_slicing(plan):
    if not _is_local_page_pack_plan(plan):
        return False
    if not _has_local_page_pack_siblings(plan):
        return True
    provider_counts = (plan or {}).get("source_worker_provider_attempt_counts")
    provider_counts = provider_counts if isinstance(provider_counts, dict) else {}
    terminal_counts = (plan or {}).get("source_worker_terminal_provider_attempt_counts")
    terminal_counts = terminal_counts if isinstance(terminal_counts, dict) else {}
    has_provider_history = bool(provider_counts or terminal_counts)
    if has_provider_history:
        return any(
            _provider_history_count(provider_counts, provider_id) <= 0
            and _provider_history_count(terminal_counts, provider_id) <= 0
            for provider_id in _local_page_pack_provider_ids(plan)
        )
    try:
        automated_attempts = int((plan or {}).get(AUTOMATED_ATTEMPT_COUNT_FIELD) or 0)
    except Exception:
        automated_attempts = 0
    return automated_attempts <= 0


def _mark_direct_local_page_pack_runtime_heads(plans, limit=LOCAL_PAGE_PACK_DIRECT_RUNTIME_HEAD_SLOTS):
    plans = list(plans or [])
    try:
        limit = int(limit)
    except Exception:
        limit = LOCAL_PAGE_PACK_DIRECT_RUNTIME_HEAD_SLOTS
    limit = max(0, min(limit, LOCAL_PAGE_PACK_RUNTIME_HEAD_SLOTS))
    if limit <= 0 or not plans:
        return plans
    if any(_would_keep_direct_local_page_pack_after_slicing(plan) for plan in plans):
        return plans
    marked = 0
    out = []
    for plan in plans:
        if marked < limit and _is_local_page_pack_followup_candidate_plan(plan):
            item = dict(plan or {})
            item[DIRECT_LOCAL_PAGE_PACK_RUNTIME_HEAD_FIELD] = True
            out.append(item)
            marked += 1
        else:
            out.append(plan)
    return out


def _series_backlog_count(plan):
    try:
        return max(1, int((plan or {}).get(SERIES_BACKLOG_COUNT_FIELD) or 1))
    except Exception:
        return 1


def _series_round_index(plan):
    try:
        return max(0, int((plan or {}).get(SERIES_ROUND_INDEX_FIELD) or 0))
    except Exception:
        return 0


def _annotate_series_backlog_counts(plans):
    plans = list(plans or [])
    counts = {}
    for plan in plans:
        key = _series_key(plan)
        counts[key] = counts.get(key, 0) + 1
    out = []
    for plan in plans:
        item = dict(plan or {})
        item[SERIES_BACKLOG_COUNT_FIELD] = counts.get(_series_key(item), 1)
        out.append(item)
    return out


def _annotate_series_round_indexes(plans):
    seen = {}
    out = []
    for plan in plans or []:
        item = dict(plan or {})
        key = _series_key(item)
        item[SERIES_ROUND_INDEX_FIELD] = seen.get(key, 0)
        seen[key] = seen.get(key, 0) + 1
        out.append(item)
    return out


def _provider_attempt_is_automated_source(item):
    item = item if isinstance(item, dict) else {}
    provider_id = str(item.get("provider_id") or "").strip().lower()
    if not provider_id or provider_id in NON_AUTOMATED_ATTEMPT_PROVIDER_IDS:
        return False
    if item.get("requires_operator") is True:
        return False
    if str(item.get("adapter_family") or "").strip().lower() == "unmapped_source":
        return False
    return bool(
        item.get("can_execute_with_http_client")
        or item.get("can_execute_with_tool_runner")
        or item.get("emits_download_task")
    )


def _automated_attempt_count(plan):
    plan = plan if isinstance(plan, dict) else {}
    terminal_counts = plan.get("source_worker_terminal_provider_attempt_counts")
    if isinstance(terminal_counts, dict):
        counts = terminal_counts
    else:
        counts = plan.get("source_worker_provider_attempt_counts")
        counts = counts if isinstance(counts, dict) else {}
    if not counts:
        return 0
    automated_provider_ids = {
        str(item.get("provider_id") or "").strip().lower()
        for item in plan.get("provider_attempt_plan") or []
        if _provider_attempt_is_automated_source(item)
    }
    if not automated_provider_ids:
        return 0
    total = 0
    for provider_id, count in counts.items():
        key = str(provider_id or "").strip().lower()
        if key not in automated_provider_ids:
            continue
        try:
            total += max(0, int(count or 0))
        except Exception:
            continue
    return total


def _annotate_automated_attempt_counts(plans):
    out = []
    for plan in plans or []:
        item = dict(plan or {})
        item[AUTOMATED_ATTEMPT_COUNT_FIELD] = _automated_attempt_count(item)
        out.append(item)
    return out


def _initial_search_priority_at(plan):
    return _float((plan or {}).get("series_initial_search_priority_at"), 0.0)


def _initial_search_opportunity_pending(plan):
    priority_at = _initial_search_priority_at(plan)
    if priority_at <= 0:
        return False
    # series_latest_source_attempt_at is the max timestamp over every
    # source_attempts row, including non-executing bookkeeping (queue sync,
    # importer, Kavita, source-ladder, retry normalization) that routine
    # queue activity writes on its own schedule. Checking that raw
    # timestamp before the automated-attempt count let ordinary bookkeeping
    # consume a series' one-time initial-search reservation with zero real
    # provider executions. Zero automated attempts means the opportunity is
    # still pending regardless of what bookkeeping happened to run since.
    if int((plan or {}).get(AUTOMATED_ATTEMPT_COUNT_FIELD) or 0) <= 0:
        return True
    latest_attempt_at = _float((plan or {}).get("series_latest_source_attempt_at"), 0.0)
    return latest_attempt_at < priority_at


def _reserve_initial_search_opportunity(plans, window, limit):
    """Reserve one oldest pending series head while leaving the rest of the pass fair."""

    plans = list(plans or [])
    window = list(window or [])
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        return window

    def plan_key(plan):
        queue_id = str((plan or {}).get("queue_id") or "").strip()
        return f"queue:{queue_id}" if queue_id else f"object:{id(plan)}"

    # Refill from the full eligible pool, while retaining annotations already
    # applied to rows in the initially sliced/reserved window.
    window_by_key = {plan_key(plan): plan for plan in window}
    pool = []
    seen_queue_keys = set()
    for plan in plans:
        key = plan_key(plan)
        if key in seen_queue_keys:
            continue
        seen_queue_keys.add(key)
        pool.append(window_by_key.get(key, plan))
    candidates = []
    seen_series = set()
    for index, plan in enumerate(pool):
        if not _initial_search_opportunity_pending(plan):
            continue
        series_key = _series_key(plan)
        if series_key in seen_series:
            continue
        seen_series.add(series_key)
        candidates.append((_initial_search_priority_at(plan), index, str((plan or {}).get("queue_id") or ""), plan))
    if not candidates:
        return window[:limit]
    _priority_at, original_index, _queue_id, chosen = min(candidates, key=lambda item: item[:3])
    chosen_key = str((chosen or {}).get("queue_id") or "").strip()
    chosen_row = dict(chosen or {})
    chosen_row[INITIAL_SEARCH_PRIORITY_FIELD] = {
        "reserved": True,
        "qa_observation_id": INITIAL_SEARCH_QA_OBSERVATION_ID,
        "series_id": chosen_row.get("series_id"),
        "priority_at": _initial_search_priority_at(chosen_row),
        "original_index": int(original_index),
        "eligible_limit": int(limit),
        "reason": "newly added series has not received its first source-search opportunity",
    }
    remaining = [
        plan
        for plan in pool
        if str((plan or {}).get("queue_id") or "").strip() != chosen_key
    ]
    chosen_series = _series_key(chosen_row)
    remaining_by_key = {plan_key(plan): plan for plan in remaining}
    reserved_other = []
    used_keys = {plan_key(chosen_row)}
    for window_index, plan in enumerate(window):
        key = plan_key(plan)
        if key in used_keys or key not in remaining_by_key or _series_key(plan) == chosen_series:
            continue
        has_established_reserve = any(
            isinstance((plan or {}).get(field), dict) and (plan or {}).get(field, {}).get("reserved")
            for field in (
                "source_worker_local_page_pack_reserve",
                "source_worker_runtime_budget_starved_reserve",
                "source_worker_source_retry_starved_reserve",
            )
        )
        reserved_other.append((0 if has_established_reserve else 1, window_index, plan))
        used_keys.add(key)
    reserved_other = [plan for _reserve_priority, _window_index, plan in sorted(reserved_other, key=lambda item: item[:2])]
    other_series = _spread_by_series(
        [
            plan
            for plan in remaining
            if plan_key(plan) not in used_keys and _series_key(plan) != chosen_series
        ]
    )
    used_keys.update(plan_key(plan) for plan in other_series)
    same_series = [
        plan
        for plan in remaining
        if plan_key(plan) not in used_keys and _series_key(plan) == chosen_series
    ]
    fair_remaining = [*reserved_other, *other_series, *same_series]
    # Only one initial lane is reserved per pass. With limit > 1, every other
    # series is considered before sibling units from the newly-added series.
    return [chosen_row, *fair_remaining[: max(0, limit - 1)]]


def _plan_text_blob(plan):
    plan = plan if isinstance(plan, dict) else {}
    wanted_item = plan.get("wanted_item") if isinstance(plan.get("wanted_item"), dict) else {}
    latest_attempt = plan.get("latest_source_attempt") if isinstance(plan.get("latest_source_attempt"), dict) else {}
    pieces = []
    for source in (plan, wanted_item, latest_attempt):
        for key in (
            "series",
            "series_title",
            "title",
            "source",
            "provider",
            "provider_id",
            "status",
            "outcome",
            "lifecycle_phase",
            "failure_reason",
            "reason",
            "last_event",
            "next_action",
            "state",
            "display_phase",
            "current_source",
            "provider_status_provider",
            "provider_status_phase",
            "provider_status_state",
        ):
            value = source.get(key)
            if value not in (None, ""):
                pieces.append(str(value))
    return " ".join(pieces).lower()


def _has_trusted_comic_pack_lane(plan):
    return bool(_trusted_comic_pack_child_provider_ids(_selected_provider_ids(plan)))


def _comic_pack_backlog_priority(plan):
    """Promote older comic rows that already proved RSS/source-ladder churn."""

    plan = plan if isinstance(plan, dict) else {}
    if not _plan_looks_comic_pack_eligible(plan):
        return 0
    if not _has_trusted_comic_pack_lane(plan):
        return 0
    text = _plan_text_blob(plan)
    score = 0
    if any(
        token in text
        for token in (
            "no_candidate",
            "no candidate",
            "no candidates",
            "runtime budget",
            "source ladder",
            "rss activity",
            "pack_no_matching_missing_file",
        )
    ):
        score += 4
    if "rss" in text:
        score += 2
    automated_attempts = int(plan.get(AUTOMATED_ATTEMPT_COUNT_FIELD) or 0)
    if automated_attempts > 0:
        score += 1 + min(3, automated_attempts // 25)
    if _plan_release_dates(plan):
        score += 1
    return score


def _annotate_comic_pack_backlog_priority(plans):
    out = []
    for plan in plans or []:
        item = dict(plan or {})
        item[COMIC_PACK_BACKLOG_PRIORITY_FIELD] = _comic_pack_backlog_priority(item)
        out.append(item)
    return out


def _has_latest_source_attempt_evidence(plan):
    latest_attempt = (plan or {}).get("latest_source_attempt")
    if not isinstance(latest_attempt, dict):
        return False
    return any(value not in (None, "") for value in latest_attempt.values())


def _plan_is_active_transfer_or_import(plan):
    text = _plan_text_blob(plan)
    return any(
        token in text
        for token in (
            " active",
            "downloading",
            "transferring",
            "importing",
            "import_ready",
            "verified",
        )
    )


def _plan_missing_recovery_cohort(plan, *, now=None):
    plan = plan if isinstance(plan, dict) else {}
    now = time.time() if now is None else float(now)
    text = _plan_text_blob(plan)
    if any(
        marker in text
        for marker in (
            "library_scan_timeout",
            "reader scan timeout",
            "missing_file",
            "missing file",
            "stale_folder_proof",
            "import failed",
            "import_failed",
            "stale import",
        )
    ) or int(plan.get("retryable_failed_stage_attempt_count") or 0) > 0:
        return "import_reader_recovery"
    if (
        int(plan.get("retryable_failed_handoff_count") or 0) > 0
        or int(plan.get("stale_handoff_count") or 0) > 0
        or plan.get("retryable_failed_handoff_recovery")
    ):
        return "handoff_transfer_recovery"
    attempts = int(plan.get(AUTOMATED_ATTEMPT_COUNT_FIELD) or 0)
    if attempts <= 0:
        created_at = _float(plan.get("created_at"), 0.0)
        aged_reserve = plan.get(AGED_ZERO_PROVIDER_COVERAGE_RESERVE_FIELD)
        if (
            isinstance(aged_reserve, dict) and aged_reserve.get("reserved")
        ) or (created_at > 0 and now - created_at >= MISSING_RECOVERY_NEVER_TRIED_AGE_SECONDS):
            return "never_no_call"
        return "ordinary_new"
    return "result_candidate_loss"


def _apply_missing_recovery_cohort(plans, window, limit, *, now=None):
    """Reserve mixed recovery capacity inside the existing eligible limit."""

    plans = list(plans or [])
    window = list(window or [])
    limit = max(1, int(limit or 1))
    now = time.time() if now is None else float(now)

    def plan_key(plan):
        queue_id = str((plan or {}).get("queue_id") or "").strip()
        return f"queue:{queue_id}" if queue_id else f"object:{id(plan)}"

    annotated = {plan_key(plan): plan for plan in window}
    unique = []
    seen = set()
    for plan in plans:
        key = plan_key(plan)
        if key in seen:
            continue
        seen.add(key)
        item = dict(annotated.get(key, plan) or {})
        item[MISSING_RECOVERY_COHORT_FIELD] = _plan_missing_recovery_cohort(item, now=now)
        unique.append(item)

    by_cohort = {cohort: [] for cohort in MISSING_RECOVERY_COHORTS}
    for plan in unique:
        by_cohort[plan[MISSING_RECOVERY_COHORT_FIELD]].append(plan)
    max_per_cohort = _missing_recovery_max_per_cohort(limit)
    selected = []
    selected_series = set()
    counts = {cohort: 0 for cohort in MISSING_RECOVERY_COHORTS}

    def take(cohort, *, distinct_series=False):
        rows = by_cohort.get(cohort) or []
        if not rows:
            return False
        index = 0
        if distinct_series:
            index = next(
                (i for i, row in enumerate(rows) if _series_key(row) not in selected_series),
                0,
            )
        row = rows.pop(index)
        selected.append(row)
        selected_series.add(_series_key(row))
        counts[cohort] += 1
        return True

    # Reserve one distinct-series opportunity for every represented cohort.
    for cohort in MISSING_RECOVERY_COHORTS:
        if len(selected) >= limit:
            break
        take(cohort, distinct_series=True)

    while len(selected) < limit:
        available = [cohort for cohort in MISSING_RECOVERY_COHORTS if by_cohort.get(cohort)]
        if not available:
            break
        bounded = [cohort for cohort in available if counts[cohort] < max_per_cohort]
        choices = bounded or sorted(available, key=lambda cohort: counts[cohort])
        if not choices:
            break
        moved = False
        for cohort in choices:
            if len(selected) >= limit:
                break
            moved = take(cohort, distinct_series=True) or moved
        if not moved:
            break
    return selected


def _provider_counts(plans):
    counts = {}
    for plan in plans or []:
        for provider_id in set(_selected_provider_ids(plan)):
            counts[provider_id] = counts.get(provider_id, 0) + 1
    return counts


def _is_prowlarr_child_provider(provider_id):
    return str(provider_id or "").strip().lower().startswith(PROWLARR_CHILD_PROVIDER_PREFIX)


def _is_prowlarr_lane_provider(provider_id):
    provider_id = str(provider_id or "").strip().lower()
    return provider_id == PROWLARR_AGGREGATE_PROVIDER_ID or _is_prowlarr_child_provider(provider_id)


def _provider_lane_key(plan, provider_counts):
    provider_ids = _selected_provider_ids(plan)
    if not provider_ids:
        return ""
    configured_lanes = [provider_id for provider_id in provider_ids if provider_id not in AGGREGATE_PROVIDER_LANES]
    candidates = configured_lanes or provider_ids
    return sorted(candidates, key=lambda provider_id: (provider_counts.get(provider_id, 0), provider_id))[0]


def _slice_provider_attempt_plan(
    provider_attempt_plan,
    selected_provider_ids,
    deferred_provider_ids,
    *,
    deferred_attempt_state=PROWLARR_CHILD_LANE_SLICE_KIND,
    deferred_reason="Concrete Prowlarr sibling will run in a later source-worker pass.",
):
    selected_provider_ids = set(selected_provider_ids or [])
    deferred_provider_ids = set(deferred_provider_ids or [])
    out = []
    for row in provider_attempt_plan or []:
        if not isinstance(row, dict):
            continue
        provider_id = str(row.get("provider_id") or "").strip().lower()
        item = dict(row)
        if provider_id in deferred_provider_ids and item.get("selected"):
            item["selected"] = False
            item["attempt_state"] = deferred_attempt_state
            item["deferred_reason"] = deferred_reason
        elif provider_id in selected_provider_ids and row.get("selected"):
            item["selected"] = True
            item["attempt_state"] = "selected"
        out.append(item)
    return out


def _refresh_provider_attempt_counts(plan):
    if not isinstance((plan or {}).get("provider_attempt_plan"), list):
        return plan
    counts = {}
    for row in plan.get("provider_attempt_plan") or []:
        state = str((row or {}).get("attempt_state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    plan["provider_attempt_counts"] = dict(sorted(counts.items()))
    return plan


def _plan_wanted_item(plan):
    plan = plan if isinstance(plan, dict) else {}
    wanted_item = plan.get("wanted_item") if isinstance(plan.get("wanted_item"), dict) else {}
    out = dict(wanted_item)
    for key in (
        "series",
        "series_title",
        "title",
        "issue_number",
        "normalized_number",
        "unit_type",
        "unitType",
        "chapter_number",
        "chapter",
        "volume_number",
        "volume",
        "volumeNumber",
        "media_type",
        "library_type",
        "format",
        "publisher",
        "publisher_name",
        "imprint",
        "release_date",
        "issue_date",
        "date",
        "publish_date",
        "publication_date",
        "cover_date",
        "year",
        "issue_year",
        "publication_year",
        "release_year",
        "cover_year",
    ):
        if key not in out and plan.get(key) not in (None, ""):
            out[key] = plan.get(key)
    return out


def _plan_looks_comic_pack_eligible(plan):
    wanted_item = _plan_wanted_item(plan)
    media_text = " ".join(
        str(wanted_item.get(key) or "")
        for key in ("media_type", "library_type", "format")
    ).lower()
    publisher_text = " ".join(
        str(wanted_item.get(key) or "")
        for key in ("publisher", "publisher_name", "imprint")
    ).lower()
    series_text = " ".join(
        str(wanted_item.get(key) or "")
        for key in ("series", "series_title", "title")
    ).lower()
    if "manga" in media_text:
        return False
    manga_publishers = (
        "kodansha",
        "shueisha",
        "viz",
        "yen press",
        "seven seas",
        "square enix",
        "tokyopop",
        "vertical",
        "j-novel",
    )
    if any(token in publisher_text for token in manga_publishers):
        return False
    if any(token in media_text for token in ("comic", "graphic novel")):
        return True
    comic_publishers = (
        "dc",
        "marvel",
        "image",
        "dark horse",
        "idw",
        "boom",
        "dynamite",
        "archie",
        "oni",
        "aftershock",
        "valiant",
    )
    if any(token in publisher_text for token in comic_publishers):
        return True
    return series_text.startswith("absolute ")


def _has_concrete_comic_pack_indexer_lane(provider_ids):
    return bool(_comic_pack_indexer_provider_ids(provider_ids))


def _comic_pack_indexer_provider_ids(provider_ids):
    out = []
    for provider_id in provider_ids or []:
        text = str(provider_id or "").strip().lower()
        if not text.startswith(PROWLARR_CHILD_PROVIDER_PREFIX):
            continue
        if any(token in text for token in ("comics", "dognzb", "torrentleech")):
            out.append(text)
    return out


def _should_prefer_pack_indexer_before_rss(plan, provider_ids):
    return bool(
        RSS_PROVIDER_ID in provider_ids
        and _has_concrete_comic_pack_indexer_lane(provider_ids)
        and _plan_looks_comic_pack_eligible(plan)
    )


def _plan_release_dates(plan):
    wanted_item = _plan_wanted_item(plan)
    out = []
    seen = set()
    for key in (
        "release_date",
        "issue_date",
        "date",
        "publish_date",
        "publication_date",
        "cover_date",
    ):
        value = str(wanted_item.get(key) or "").strip()
        if not value:
            continue
        match = re.search(r"\b((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", value)
        if not match:
            continue
        try:
            parsed = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
        except ValueError:
            continue
        if parsed not in seen:
            seen.add(parsed)
            out.append(parsed)
    return out


def _plan_release_years(plan):
    wanted_item = _plan_wanted_item(plan)
    out = []
    seen = set()
    for key in (
        "year",
        "issue_year",
        "publication_year",
        "release_year",
        "cover_year",
        "release_date",
        "issue_date",
        "date",
        "publish_date",
        "publication_date",
        "cover_date",
    ):
        value = str(wanted_item.get(key) or "").strip()
        if not value:
            continue
        for match in re.finditer(r"\b((?:19|20)\d{2})\b", value):
            year = int(match.group(1))
            if year not in seen:
                seen.add(year)
                out.append(year)
            break
    return out


def _rss_fast_lane_has_freshness_evidence(plan):
    cutoff = date.today() - timedelta(days=RSS_FAST_LANE_MAX_AGE_DAYS)
    dates = _plan_release_dates(plan)
    if dates:
        return max(dates) >= cutoff
    years = _plan_release_years(plan)
    if years:
        return max(years) >= cutoff.year
    return False


def _slice_rss_fast_lane_before_prowlarr(plan):
    """Run fast RSS evidence before slower Prowlarr lanes on the same row."""

    plan = dict(plan or {})
    provider_ids = _selected_provider_ids(plan)
    if RSS_PROVIDER_ID not in provider_ids:
        return plan
    prowlarr_provider_ids = [
        provider_id
        for provider_id in provider_ids
        if _is_prowlarr_lane_provider(provider_id)
    ]
    if not prowlarr_provider_ids:
        return plan
    if _should_prefer_pack_indexer_before_rss(plan, provider_ids):
        sliced_provider_ids = [
            provider_id
            for provider_id in provider_ids
            if provider_id != RSS_PROVIDER_ID
        ]
        deferred_provider_ids = [RSS_PROVIDER_ID]
        if sliced_provider_ids and isinstance(plan.get("provider_attempt_plan"), list):
            plan["selected_provider_ids"] = sliced_provider_ids
            plan["provider_attempt_plan"] = _slice_provider_attempt_plan(
                plan.get("provider_attempt_plan"),
                sliced_provider_ids,
                deferred_provider_ids,
                deferred_attempt_state=RSS_FAST_LANE_SLICE_KIND,
                deferred_reason=(
                    "Trusted comic-pack indexer lanes will run before RSS fallback "
                    "because dated pack manifests are more likely to contain this issue."
                ),
            )
            _refresh_provider_attempt_counts(plan)
        plan["source_worker_rss_fast_lane"] = {
            "skipped": True,
            "reason": "comic_pack_indexer_preferred",
            "selected_provider_ids": list(sliced_provider_ids),
            "deferred_provider_ids": list(deferred_provider_ids),
            "preferred_provider_ids": list(prowlarr_provider_ids),
        }
        return plan
    if not _rss_fast_lane_has_freshness_evidence(plan):
        plan["source_worker_rss_fast_lane"] = {
            "skipped": True,
            "reason": "fresh_release_evidence_missing",
            "max_age_days": RSS_FAST_LANE_MAX_AGE_DAYS,
        }
        return plan

    sliced_provider_ids = []
    deferred_provider_ids = []
    for provider_id in provider_ids:
        if provider_id == RSS_PROVIDER_ID:
            if provider_id not in sliced_provider_ids:
                sliced_provider_ids.append(provider_id)
        elif _is_prowlarr_lane_provider(provider_id):
            if provider_id not in deferred_provider_ids:
                deferred_provider_ids.append(provider_id)
        elif provider_id not in sliced_provider_ids:
            sliced_provider_ids.append(provider_id)

    if not deferred_provider_ids:
        return plan

    plan["selected_provider_ids"] = sliced_provider_ids
    if isinstance(plan.get("provider_attempt_plan"), list):
        plan["provider_attempt_plan"] = _slice_provider_attempt_plan(
            plan.get("provider_attempt_plan"),
            sliced_provider_ids,
            deferred_provider_ids,
            deferred_attempt_state=RSS_FAST_LANE_SLICE_KIND,
            deferred_reason=(
                "RSS will run first to preserve batch coverage; deferred Prowlarr lane "
                "will run after RSS cooldown or in a later source-worker pass."
            ),
        )
        _refresh_provider_attempt_counts(plan)
    plan["source_worker_slice"] = {
        "kind": RSS_FAST_LANE_SLICE_KIND,
        "selected_provider_ids": list(sliced_provider_ids),
        "deferred_provider_ids": list(deferred_provider_ids),
        "original_selected_provider_ids": list(provider_ids),
        "preferred_provider_id": RSS_PROVIDER_ID,
    }
    return plan


def _slice_local_page_pack_fast_lane(plan):
    """Let untried local page-pack providers get a focused first attempt."""

    plan = dict(plan or {})
    provider_ids = _selected_provider_ids(plan)
    if not provider_ids:
        return plan
    fast_lane_provider_ids = []
    for provider_id in provider_ids:
        if provider_id in LOCAL_PAGE_PACK_FAST_LANE_PROVIDER_IDS and provider_id not in fast_lane_provider_ids:
            fast_lane_provider_ids.append(provider_id)
    if not fast_lane_provider_ids:
        return plan
    try:
        automated_attempts = int(plan.get(AUTOMATED_ATTEMPT_COUNT_FIELD) or 0)
    except Exception:
        automated_attempts = 0
    provider_counts = plan.get("source_worker_provider_attempt_counts")
    provider_counts = provider_counts if isinstance(provider_counts, dict) else {}
    terminal_counts = plan.get("source_worker_terminal_provider_attempt_counts")
    terminal_counts = terminal_counts if isinstance(terminal_counts, dict) else {}
    has_provider_history = bool(provider_counts or terminal_counts)
    if has_provider_history:
        fast_lane_provider_ids = [
            provider_id
            for provider_id in fast_lane_provider_ids
            if _provider_history_count(provider_counts, provider_id) <= 0
            and _provider_history_count(terminal_counts, provider_id) <= 0
        ]
        if not fast_lane_provider_ids:
            return plan
    elif automated_attempts > 0:
        return plan

    selected_fast_lane_ids = list(fast_lane_provider_ids)

    deferred_provider_ids = [
        provider_id
        for provider_id in provider_ids
        if provider_id not in selected_fast_lane_ids
    ]
    if not deferred_provider_ids:
        return plan

    plan["selected_provider_ids"] = list(selected_fast_lane_ids)
    reason_prefix = "zero_coverage" if automated_attempts <= 0 else "untried"
    if isinstance(plan.get("provider_attempt_plan"), list):
        plan["provider_attempt_plan"] = _slice_provider_attempt_plan(
            plan.get("provider_attempt_plan"),
            selected_fast_lane_ids,
            deferred_provider_ids,
            deferred_attempt_state=LOCAL_PAGE_PACK_FAST_LANE_SLICE_KIND,
            deferred_reason=(
                "Local page-pack provider will run first for untried source work; "
                "sibling providers will run after this source-worker pass or cooldown."
            ),
        )
        _refresh_provider_attempt_counts(plan)
    slice_payload = {
        "kind": LOCAL_PAGE_PACK_FAST_LANE_SLICE_KIND,
        "selected_provider_ids": list(selected_fast_lane_ids),
        "deferred_provider_ids": list(deferred_provider_ids),
        "original_selected_provider_ids": list(provider_ids),
        "selection_reason": f"{reason_prefix}_local_page_pack_fast_lane",
    }
    previous_slice = plan.get("source_worker_slice")
    if isinstance(previous_slice, dict):
        slice_payload["previous_slice"] = previous_slice
    plan["source_worker_slice"] = slice_payload
    return plan


def _slice_after_local_page_pack_history(plan):
    """After local page-pack has history, rotate one remaining sibling provider."""

    plan = dict(plan or {})
    provider_ids = _selected_provider_ids(plan)
    if not provider_ids:
        return plan
    local_provider_ids = [
        provider_id
        for provider_id in provider_ids
        if provider_id in LOCAL_PAGE_PACK_FAST_LANE_PROVIDER_IDS
    ]
    if not local_provider_ids:
        return plan
    provider_counts = plan.get("source_worker_provider_attempt_counts")
    provider_counts = provider_counts if isinstance(provider_counts, dict) else {}
    terminal_counts = plan.get("source_worker_terminal_provider_attempt_counts")
    terminal_counts = terminal_counts if isinstance(terminal_counts, dict) else {}
    local_history_count = sum(
        _provider_history_count(provider_counts, provider_id)
        + _provider_history_count(terminal_counts, provider_id)
        for provider_id in local_provider_ids
    )
    if local_history_count <= 0:
        return plan
    sibling_provider_ids = [
        provider_id
        for provider_id in provider_ids
        if provider_id not in LOCAL_PAGE_PACK_FAST_LANE_PROVIDER_IDS
    ]
    if not sibling_provider_ids:
        return plan
    force_direct_head = bool(plan.get(DIRECT_LOCAL_PAGE_PACK_RUNTIME_HEAD_FIELD))
    if force_direct_head:
        selected_provider_ids = list(local_provider_ids)
        slice_kind = LOCAL_PAGE_PACK_DIRECT_RUNTIME_HEAD_SLICE_KIND
        selection_reason = "local_page_pack_direct_runtime_head"
        deferred_reason = (
            "A direct local page-pack source head row is reserved for this runtime pass; "
            "sibling sources will run after this source-worker pass or cooldown."
        )
    else:
        first_index = {}
        for index, provider_id in enumerate(provider_ids):
            first_index.setdefault(provider_id, index)
        selected_sibling_id = sorted(
            sibling_provider_ids,
            key=lambda provider_id: (
                _provider_history_count(provider_counts, provider_id)
                + _provider_history_count(terminal_counts, provider_id),
                first_index.get(provider_id, len(provider_ids)),
                provider_id,
            ),
        )[0]
        selected_provider_ids = [selected_sibling_id]
        slice_kind = LOCAL_PAGE_PACK_SIBLING_ROTATION_SLICE_KIND
        selection_reason = "local_page_pack_history_sibling_rotation"
        deferred_reason = (
            "Local page-pack provider already has source-worker history; "
            "one remaining sibling source will run in this pass."
        )
    deferred_provider_ids = [
        provider_id
        for provider_id in provider_ids
        if provider_id not in selected_provider_ids
    ]
    if not deferred_provider_ids:
        return plan

    plan["selected_provider_ids"] = list(selected_provider_ids)
    if isinstance(plan.get("provider_attempt_plan"), list):
        plan["provider_attempt_plan"] = _slice_provider_attempt_plan(
            plan.get("provider_attempt_plan"),
            selected_provider_ids,
            deferred_provider_ids,
            deferred_attempt_state=slice_kind,
            deferred_reason=deferred_reason,
        )
        _refresh_provider_attempt_counts(plan)
    slice_payload = {
        "kind": slice_kind,
        "selected_provider_ids": list(selected_provider_ids),
        "deferred_provider_ids": list(deferred_provider_ids),
        "original_selected_provider_ids": list(provider_ids),
        "local_provider_ids": list(local_provider_ids),
        "selection_reason": selection_reason,
    }
    if force_direct_head:
        slice_payload["runtime_head_reserved"] = True
    previous_slice = plan.get("source_worker_slice")
    if isinstance(previous_slice, dict):
        slice_payload["previous_slice"] = previous_slice
    plan["source_worker_slice"] = slice_payload
    return plan


def _rotate_local_page_pack_fast_lane_for_runtime_budget(plan):
    """When the fast lane's sole Suwayomi-only selection cannot fit the
    remaining pass budget, rotate to one feasible deferred sibling instead of
    resubmitting the same impossible sole selection every pass. Returns a
    rotated plan, or None if no rotation applies."""

    plan = plan if isinstance(plan, dict) else {}
    slice_info = plan.get("source_worker_slice")
    slice_info = slice_info if isinstance(slice_info, dict) else {}
    if slice_info.get("kind") != LOCAL_PAGE_PACK_FAST_LANE_SLICE_KIND:
        return None
    deferred_provider_ids = [
        str(value).strip().lower()
        for value in _list(slice_info.get("deferred_provider_ids"))
        if str(value or "").strip()
    ]
    if not deferred_provider_ids:
        return None
    original_provider_ids = [
        str(value).strip().lower()
        for value in _list(slice_info.get("original_selected_provider_ids"))
        if str(value or "").strip()
    ] or deferred_provider_ids
    provider_counts = plan.get("source_worker_provider_attempt_counts")
    provider_counts = provider_counts if isinstance(provider_counts, dict) else {}
    terminal_counts = plan.get("source_worker_terminal_provider_attempt_counts")
    terminal_counts = terminal_counts if isinstance(terminal_counts, dict) else {}
    first_index = {}
    for index, provider_id in enumerate(original_provider_ids):
        first_index.setdefault(provider_id, index)
    selected_sibling_id = sorted(
        deferred_provider_ids,
        key=lambda provider_id: (
            _provider_history_count(provider_counts, provider_id)
            + _provider_history_count(terminal_counts, provider_id),
            first_index.get(provider_id, len(deferred_provider_ids)),
            provider_id,
        ),
    )[0]
    selected_provider_ids = [selected_sibling_id]
    remaining_deferred_ids = [
        provider_id for provider_id in deferred_provider_ids if provider_id != selected_sibling_id
    ]
    candidate = dict(plan)
    candidate["selected_provider_ids"] = list(selected_provider_ids)
    if isinstance(candidate.get("provider_attempt_plan"), list):
        candidate["provider_attempt_plan"] = _slice_provider_attempt_plan(
            candidate.get("provider_attempt_plan"),
            selected_provider_ids,
            remaining_deferred_ids,
            deferred_attempt_state=LOCAL_PAGE_PACK_SIBLING_ROTATION_SLICE_KIND,
            deferred_reason=(
                "Local page-pack provider's plan did not fit the remaining source-worker "
                "runtime budget this pass; one sibling source will run instead."
            ),
        )
        _refresh_provider_attempt_counts(candidate)
    candidate["source_worker_slice"] = {
        "kind": LOCAL_PAGE_PACK_SIBLING_ROTATION_SLICE_KIND,
        "selected_provider_ids": list(selected_provider_ids),
        "deferred_provider_ids": list(remaining_deferred_ids),
        "original_selected_provider_ids": list(original_provider_ids),
        "selection_reason": "local_page_pack_runtime_budget_rotation",
        "runtime_budget_rotated_from_provider_ids": list(slice_info.get("selected_provider_ids") or []),
        "previous_slice": slice_info,
    }
    return candidate


def _persist_runtime_budget_skip_evidence(db_path, plan, *, estimate, remaining, now=None):
    """Persist durable, explicitly-non-attempt evidence that this queue item's
    only runtime-budget-feasible plan could not run this pass. Uses the same
    recognized "source_runtime_budget_skipped" kind/reason text that
    inkdrop_series_autopilot already writes, so starvation-age tracking
    (_is_runtime_budget_skipped_plan) and the non-attempt classifier
    (_is_non_attempt_source_attempt) both recognize it -- without inflating any
    provider's real attempt/health counters, since those explicitly skip rows
    carrying this kind."""

    queue_id = str((plan or {}).get("queue_id") or "").strip()
    if not queue_id:
        return
    provider_ids = _selected_provider_ids(plan)
    provider_id = provider_ids[0] if provider_ids else ""
    if not provider_id:
        return
    now = time.time() if now is None else now
    reason = (
        f"{provider_id} did not start before the worker runtime budget "
        f"({int(estimate)}s estimate, {int(max(0.0, _float(remaining, 0.0)))}s available); "
        "automatic retry scheduled"
    )
    attempt = {
        "ts": now,
        "source": provider_id,
        "provider_id": provider_id,
        "provider": provider_id,
        "status": "retry_scheduled",
        "lifecycle_phase": "retry_later",
        "display_phase": "retry_later",
        "outcome": "no_candidate",
        "retry_eligible": True,
        "reason": reason,
        "failure_reason": reason,
        "kind": "source_runtime_budget_skipped",
        "title": str(_plan_wanted_item(plan).get("series") or "").strip(),
    }
    try:
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.record_source_attempt(
                con,
                queue_id,
                plan.get("wanted_id"),
                plan.get("series_id"),
                plan.get("issue_id"),
                attempt,
                started_at=now,
                completed_at=now,
            )
    except Exception:
        pass


def _provider_history_count(provider_history_counts, provider_id):
    try:
        return int((provider_history_counts or {}).get(provider_id) or 0)
    except Exception:
        return 0


def _preferred_prowlarr_child_candidates(child_provider_ids, provider_history_counts):
    child_provider_ids = [
        str(provider_id or "").strip().lower()
        for provider_id in child_provider_ids or []
        if str(provider_id or "").strip()
    ]
    manga_children = [
        provider_id
        for provider_id in child_provider_ids
        if provider_id in MANGA_PROWLARR_CHILD_PROVIDER_IDS
    ]
    if not manga_children:
        return child_provider_ids, ""
    min_child_attempts = min(
        _provider_history_count(provider_history_counts, provider_id)
        for provider_id in child_provider_ids
    )
    manga_due = [
        provider_id
        for provider_id in manga_children
        if _provider_history_count(provider_history_counts, provider_id) <= min_child_attempts
    ]
    if manga_due:
        return manga_due, "manga_prowlarr_child_preference"
    return child_provider_ids, ""


def _trusted_comic_pack_child_provider_ids(child_provider_ids):
    out = []
    for provider_id in child_provider_ids or []:
        provider_key = str(provider_id or "").strip().lower()
        if provider_key in COMIC_PACK_PROWLARR_CHILD_PROVIDER_IDS and provider_key not in out:
            out.append(provider_key)
    return out


def _comic_pack_trusted_child_rotation_key(plan, provider_id):
    """Deterministic per-row tie-break so equal-history children spread
    across queue rows instead of always resolving to the same
    registry-order first child (e.g. DOGnzb ahead of TorrentLeech on every
    row whose history is tied)."""

    seed = str((plan or {}).get("queue_id") or (plan or {}).get("series_id") or "")
    return hashlib.sha1(f"{seed}:{provider_id}".encode("utf-8")).hexdigest()


def _comic_pack_prowlarr_child_lane_limit(value=None):
    try:
        limit = int(value if value not in (None, "") else COMIC_PACK_PROWLARR_CHILD_LANE_LIMIT)
    except Exception:
        limit = COMIC_PACK_PROWLARR_CHILD_LANE_LIMIT
    return max(1, min(limit, COMIC_PACK_PROWLARR_CHILD_LANE_LIMIT_MAX))


def _provider_attempt_row_ready_for_lane_promotion(row):
    row = row if isinstance(row, dict) else {}
    if row.get("can_execute_with_http_client") is False:
        return False
    if row.get("requires_manual_confirm") or row.get("manual_operator_required"):
        return False
    if row.get("cooldown_kind") or row.get("cooldown_until") or row.get("next_eligible_at"):
        return False
    state_text = " ".join(
        str(row.get(key) or "")
        for key in (
            "attempt_state",
            "job_status",
            "status",
            "reason",
            "failure_reason",
            "blocked_reason",
        )
    ).lower()
    blocked_tokens = (
        "blocked",
        "cooldown",
        "disabled",
        "error",
        "http_error",
        "manual",
        "missing_config",
        "not_enabled",
        "provider_timeout",
        "provider_unavailable",
        "provider_wait",
        "rate_limited",
        "runtime_budget",
        "skipped",
        "unauthorized",
    )
    if any(token in state_text for token in blocked_tokens):
        return False
    ready_tokens = ("ready_not_selected", "ready", "selected")
    return bool(row.get("selected")) or any(token in state_text for token in ready_tokens)


def _ready_comic_pack_child_provider_ids(plan):
    if not isinstance((plan or {}).get("provider_attempt_plan"), list):
        return []
    child_provider_ids = []
    for row in plan.get("provider_attempt_plan") or []:
        if not isinstance(row, dict):
            continue
        provider_id = str(row.get("provider_id") or "").strip().lower()
        if provider_id not in COMIC_PACK_PROWLARR_CHILD_PROVIDER_IDS:
            continue
        if not _provider_attempt_row_ready_for_lane_promotion(row):
            continue
        child_provider_ids.append(provider_id)
    return _trusted_comic_pack_child_provider_ids(child_provider_ids)


def _promote_concrete_comic_prowlarr_lanes(plan):
    """Replace aggregate Prowlarr with ready concrete comic indexers for western comic rows."""

    plan = dict(plan or {})
    provider_ids = _selected_provider_ids(plan)
    if PROWLARR_AGGREGATE_PROVIDER_ID not in provider_ids:
        return plan
    if any(provider_id in COMIC_PACK_PROWLARR_CHILD_PROVIDER_IDS for provider_id in provider_ids):
        return plan
    if not _plan_looks_comic_pack_eligible(plan):
        return plan

    child_provider_ids = _ready_comic_pack_child_provider_ids(plan)
    if not child_provider_ids:
        return plan

    promoted_provider_ids = []
    inserted_children = False
    for provider_id in provider_ids:
        if provider_id == PROWLARR_AGGREGATE_PROVIDER_ID:
            if not inserted_children:
                for child_provider_id in child_provider_ids:
                    if child_provider_id not in promoted_provider_ids:
                        promoted_provider_ids.append(child_provider_id)
                inserted_children = True
            continue
        if provider_id not in promoted_provider_ids:
            promoted_provider_ids.append(provider_id)
    if not inserted_children:
        return plan

    plan["selected_provider_ids"] = promoted_provider_ids
    if isinstance(plan.get("provider_attempt_plan"), list):
        child_provider_id_set = set(child_provider_ids)
        updated_provider_attempt_plan = []
        for row in plan.get("provider_attempt_plan") or []:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            provider_id = str(item.get("provider_id") or "").strip().lower()
            if provider_id == PROWLARR_AGGREGATE_PROVIDER_ID and item.get("selected"):
                item["selected"] = False
                item["attempt_state"] = PROWLARR_AGGREGATE_COMIC_CHILD_PROMOTION_KIND
                item["deferred_reason"] = (
                    "Ready settings-backed comic Prowlarr child lanes are selected; "
                    "aggregate Prowlarr will run only after concrete lanes are exhausted."
                )
            elif provider_id in child_provider_id_set:
                item["selected"] = True
                item["attempt_state"] = "selected"
                item.pop("deferred_reason", None)
            updated_provider_attempt_plan.append(item)
        plan["provider_attempt_plan"] = updated_provider_attempt_plan
        _refresh_provider_attempt_counts(plan)

    plan["source_worker_prowlarr_lane_normalization"] = {
        "kind": PROWLARR_AGGREGATE_COMIC_CHILD_PROMOTION_KIND,
        "selected_child_provider_ids": list(child_provider_ids),
        "selected_provider_ids": list(promoted_provider_ids),
        "deferred_provider_ids": [PROWLARR_AGGREGATE_PROVIDER_ID],
        "original_selected_provider_ids": list(provider_ids),
    }
    return plan


def _recent_trusted_comic_pack_runtime_priority(plan):
    plan = plan if isinstance(plan, dict) else {}
    if not _plan_looks_comic_pack_eligible(plan):
        return 1
    if _plan_is_active_transfer_or_import(plan):
        return 1
    trusted_child_ids = _trusted_comic_pack_child_provider_ids(_selected_provider_ids(plan))
    if not trusted_child_ids:
        return 1
    dates = _plan_release_dates(plan)
    if not dates:
        return 1
    cutoff = date.today() - timedelta(days=COMIC_PACK_RUNTIME_PRIORITY_MAX_AGE_DAYS)
    if max(dates) < cutoff:
        return 1
    return 0


def _slice_prowlarr_child_lanes(plan, provider_counts, *, comic_pack_child_lane_limit=None):
    """Run at most one concrete Prowlarr indexer per queue row in a batch pass."""

    plan = dict(plan or {})
    provider_ids = _selected_provider_ids(plan)
    if not provider_ids:
        return plan
    child_provider_ids = [
        provider_id
        for provider_id in provider_ids
        if _is_prowlarr_child_provider(provider_id)
    ]
    if not child_provider_ids:
        return plan
    aggregate_present = PROWLARR_AGGREGATE_PROVIDER_ID in provider_ids
    if len(child_provider_ids) <= 1 and not aggregate_present:
        return plan

    first_index = {}
    for index, provider_id in enumerate(provider_ids):
        first_index.setdefault(provider_id, index)
    provider_history_counts = (plan.get("source_worker_provider_attempt_counts") or {}) if isinstance(plan, dict) else {}
    candidate_child_ids, child_preference = _preferred_prowlarr_child_candidates(
        child_provider_ids,
        provider_history_counts,
    )
    selected_child_ids = []
    selection_reason = ""
    if _plan_looks_comic_pack_eligible(plan) and child_preference != "manga_prowlarr_child_preference":
        comic_pack_limit = _comic_pack_prowlarr_child_lane_limit(comic_pack_child_lane_limit)
        trusted_comic_child_ids = _trusted_comic_pack_child_provider_ids(child_provider_ids)
        terminal_counts = plan.get("source_worker_terminal_provider_attempt_counts")
        terminal_counts = terminal_counts if isinstance(terminal_counts, dict) else {}
        # Rank by durable history (total + terminal) ascending, not by fixed
        # registry order -- a trusted child that has never been tried (or
        # only has non-terminal history) must be preferred over one with
        # dozens of terminal attempts. Ties (including the common "both
        # zero" case) fall through to in-pass demand, then a per-row
        # deterministic hash so different queue rows don't all resolve the
        # tie to the same child.
        selected_child_ids = sorted(
            trusted_comic_child_ids,
            key=lambda provider_id: (
                _provider_history_count(provider_history_counts, provider_id)
                + _provider_history_count(terminal_counts, provider_id),
                provider_counts.get(provider_id, 0),
                _comic_pack_trusted_child_rotation_key(plan, provider_id),
            ),
        )[:comic_pack_limit]
        if selected_child_ids:
            selection_reason = "comic_pack_trusted_child_rotation"
    if not selected_child_ids:
        selected_child_ids = [
            sorted(
                candidate_child_ids,
                key=lambda provider_id: (
                    _provider_history_count(provider_history_counts, provider_id),
                    first_index.get(provider_id, len(provider_ids)),
                    provider_counts.get(provider_id, 0),
                    provider_id,
                ),
            )[0]
        ]
    selected_child_id_set = set(selected_child_ids)
    sliced_provider_ids = []
    deferred_provider_ids = []
    inserted_selected_child_pair = False
    first_selected_child_index = min(
        (first_index.get(provider_id, len(provider_ids)) for provider_id in selected_child_ids),
        default=None,
    )
    for provider_id in provider_ids:
        if (
            len(selected_child_ids) > 1
            and not inserted_selected_child_pair
            and first_index.get(provider_id) == first_selected_child_index
        ):
            for selected_child_id in selected_child_ids:
                if selected_child_id not in sliced_provider_ids:
                    sliced_provider_ids.append(selected_child_id)
            inserted_selected_child_pair = True
            continue
        if len(selected_child_ids) > 1 and provider_id in selected_child_id_set:
            continue
        keep = (
            provider_id != PROWLARR_AGGREGATE_PROVIDER_ID
            and (not _is_prowlarr_child_provider(provider_id) or provider_id in selected_child_id_set)
        )
        if keep:
            if provider_id not in sliced_provider_ids:
                sliced_provider_ids.append(provider_id)
        elif provider_id not in deferred_provider_ids:
            deferred_provider_ids.append(provider_id)
    selection_widened = len(selected_child_ids) > 1
    if not deferred_provider_ids and not selection_widened:
        return plan

    plan["selected_provider_ids"] = sliced_provider_ids
    if isinstance(plan.get("provider_attempt_plan"), list):
        plan["provider_attempt_plan"] = _slice_provider_attempt_plan(
            plan.get("provider_attempt_plan"),
            sliced_provider_ids,
            deferred_provider_ids,
        )
        _refresh_provider_attempt_counts(plan)
    plan["source_worker_slice"] = {
        "kind": PROWLARR_CHILD_LANE_SLICE_KIND,
        "selected_child_provider_id": selected_child_ids[0] if selected_child_ids else "",
        "selected_child_provider_ids": list(selected_child_ids),
        "selected_provider_ids": list(sliced_provider_ids),
        "deferred_provider_ids": list(deferred_provider_ids),
        "original_selected_provider_ids": list(provider_ids),
    }
    if selection_widened:
        plan["source_worker_slice"]["selection_reason"] = "comic_pack_trusted_child_pair"
    elif selection_reason:
        plan["source_worker_slice"]["selection_reason"] = selection_reason
    if child_preference:
        plan["source_worker_slice"]["preferred_child_group"] = child_preference
    return plan


def _slice_selected_provider_lanes(plans, *, comic_pack_child_lane_limit=None):
    plans = list(plans or [])
    if not plans:
        return []
    provider_counts = _provider_counts(plans)
    out = []
    for plan in plans:
        plan = _promote_concrete_comic_prowlarr_lanes(plan)
        plan = _slice_rss_fast_lane_before_prowlarr(plan)
        plan = _slice_local_page_pack_fast_lane(plan)
        plan = _slice_prowlarr_child_lanes(
            plan,
            provider_counts,
            comic_pack_child_lane_limit=comic_pack_child_lane_limit,
        )
        plan = _slice_after_local_page_pack_history(plan)
        out.append(plan)
    return out


def _source_http_request_runtime_estimate_seconds(source_http_timeout_seconds=None):
    estimate = int(HTTP_REQUEST_RUNTIME_ESTIMATE_SECONDS)
    timeout = _float(source_http_timeout_seconds, 0.0)
    if timeout > 0:
        estimate = max(estimate, int(timeout + HTTP_REQUEST_RUNTIME_TIMEOUT_MARGIN_SECONDS))
    return max(1, estimate)


def _http_provider_runtime_estimate(provider_plan, *, source_http_timeout_seconds=None):
    provider_plan = provider_plan if isinstance(provider_plan, dict) else {}
    if not provider_plan.get("can_execute_with_http_client"):
        return 0
    adapter_family = str(provider_plan.get("adapter_family") or "").strip().lower()
    provider_id = str(provider_plan.get("provider_id") or "").strip().lower()
    if adapter_family not in HTTP_RUNTIME_ADAPTER_FAMILIES and not provider_id.startswith(("prowlarr_", "rss_", "generic_rss", "generic_torrent")):
        return 0
    try:
        request_count = int(provider_plan.get("request_count") or 0)
    except Exception:
        request_count = 0
    # Price the plan by its real request count. Truncating to 10 requests made
    # a 44-request serial MangaDex plan look like a 178s job when the real cost
    # was 528-756s, so admission kept scheduling it into slots that expired
    # mid-fetch.
    request_count = max(1, request_count)
    request_seconds = _source_http_request_runtime_estimate_seconds(source_http_timeout_seconds)
    estimate = HTTP_PROVIDER_FIXED_RUNTIME_SECONDS + (request_count * request_seconds)
    return max(
        HTTP_PROVIDER_RUNTIME_MIN_SECONDS,
        min(HTTP_PROVIDER_RUNTIME_MAX_SECONDS, int(estimate)),
    )


def _provider_runtime_estimate(provider_id, provider_plan=None, *, source_http_timeout_seconds=None):
    http_estimate = _http_provider_runtime_estimate(
        provider_plan,
        source_http_timeout_seconds=source_http_timeout_seconds,
    )
    if http_estimate:
        return http_estimate
    provider_id = str(provider_id or "").strip().lower()
    if not provider_id:
        return 60
    if provider_id in PROVIDER_RUNTIME_ESTIMATES:
        return PROVIDER_RUNTIME_ESTIMATES[provider_id]
    for prefix, seconds in PROVIDER_PREFIX_RUNTIME_ESTIMATES:
        if provider_id.startswith(prefix):
            return seconds
    return 60


def _plan_runtime_estimate(plan, *, source_http_timeout_seconds=None):
    provider_ids = _selected_provider_ids(plan)
    provider_plan_by_id = {
        str((row or {}).get("provider_id") or "").strip().lower(): row
        for row in ((plan or {}).get("provider_attempt_plan") or [])
        if isinstance(row, dict) and str((row or {}).get("provider_id") or "").strip()
    }
    if not provider_ids:
        provider_ids = [
            str((row or {}).get("provider_id") or "").strip().lower()
            for row in ((plan or {}).get("provider_attempt_plan") or [])
            if isinstance(row, dict) and str((row or {}).get("selected") or "").lower() in {"1", "true"}
        ]
    unique = []
    for provider_id in provider_ids:
        if provider_id and provider_id not in unique:
            unique.append(provider_id)
    if not unique:
        return 60
    estimate = sum(
        _provider_runtime_estimate(
            provider_id,
            provider_plan_by_id.get(provider_id),
            source_http_timeout_seconds=source_http_timeout_seconds,
        )
        for provider_id in unique
    )
    if _plan_looks_comic_pack_eligible(plan) and _has_trusted_comic_pack_lane(plan):
        estimate = min(estimate, COMIC_PACK_TRUSTED_PAIR_RUNTIME_MAX_SECONDS)
    return estimate


def _spread_by_series(plans):
    buckets = {}
    first_seen = {}
    for plan in plans or []:
        key = _series_key(plan)
        if key not in buckets:
            buckets[key] = []
            first_seen[key] = len(first_seen)
        buckets[key].append(plan)

    def _series_order(keys):
        return sorted(
            keys,
            key=lambda key: (
                -len(buckets.get(key) or []),
                first_seen.get(key, len(first_seen) + 1),
                key,
            ),
        )

    out = []
    active = _series_order(buckets.keys())
    while active:
        next_active = []
        for key in active:
            bucket = buckets.get(key) or []
            if not bucket:
                continue
            out.append(bucket.pop(0))
            if bucket:
                next_active.append(key)
        active = _series_order(next_active)
    return out


def _spread_by_provider_lanes(plans):
    plans = list(plans or [])
    if len(plans) <= 1:
        return plans
    provider_counts = _provider_counts(plans)
    provider_first_seen = {}
    for index, plan in enumerate(plans):
        for provider_id in set(_selected_provider_ids(plan)):
            provider_first_seen.setdefault(provider_id, index)
    if not provider_counts:
        return _spread_by_series(plans)
    buckets = {}
    lane_first_seen = {}
    lane_order = []
    for index, plan in enumerate(plans):
        lane = _provider_lane_key(plan, provider_counts)
        if lane not in buckets:
            buckets[lane] = []
            lane_order.append(lane)
            lane_first_seen[lane] = provider_first_seen.get(lane, index)
        buckets[lane].append(plan)
    for lane, bucket in list(buckets.items()):
        buckets[lane] = _spread_by_series(bucket)
    active = sorted(
        lane_order,
        key=lambda lane: (
            provider_counts.get(lane, len(plans) + 1),
            lane_first_seen.get(lane, len(plans) + 1),
            lane,
        ),
    )
    out = []
    while active:
        next_active = []
        for lane in active:
            bucket = buckets.get(lane) or []
            if not bucket:
                continue
            out.append(bucket.pop(0))
            if bucket:
                next_active.append(lane)
        active = next_active
    return out


def _spread_by_source_attempt_coverage(plans):
    plans = list(plans or [])
    comic_pack_backlog = [
        plan
        for plan in plans
        if int((plan or {}).get(COMIC_PACK_BACKLOG_PRIORITY_FIELD) or 0) > 0
        and (
            int((plan or {}).get(AUTOMATED_ATTEMPT_COUNT_FIELD) or 0) > 0
            or _has_latest_source_attempt_evidence(plan)
        )
    ]
    comic_pack_backlog_ids = {id(plan) for plan in comic_pack_backlog}
    zero_coverage = [
        plan
        for plan in plans
        if id(plan) not in comic_pack_backlog_ids
        and int((plan or {}).get(AUTOMATED_ATTEMPT_COUNT_FIELD) or 0) <= 0
    ]
    zero_coverage_ids = {id(plan) for plan in zero_coverage}
    attempted = [
        plan
        for plan in plans
        if id(plan) not in zero_coverage_ids and id(plan) not in comic_pack_backlog_ids
    ]
    comic_pack_backlog = _spread_by_provider_lanes(comic_pack_backlog)
    zero_coverage = _spread_by_provider_lanes(zero_coverage)
    attempted = _spread_by_provider_lanes(attempted)
    if comic_pack_backlog and zero_coverage:
        head_count = min(COMIC_PACK_BACKLOG_HEAD_SLOTS, len(comic_pack_backlog))
        return (
            comic_pack_backlog[:head_count]
            + zero_coverage
            + comic_pack_backlog[head_count:]
            + attempted
        )
    return comic_pack_backlog + zero_coverage + attempted


def _reserve_local_page_pack_eligible_slot(plans, limit, *, now=None):
    plans = list(plans or [])
    try:
        limit = int(limit)
    except Exception:
        return plans
    limit = max(1, min(limit, 500))
    if len(plans) <= limit:
        return plans[:limit]
    window = list(plans[:limit])
    insert_at = min(max(0, int(COMIC_PACK_BACKLOG_HEAD_SLOTS or 0)), max(0, limit - 1))

    def reserve_key(plan):
        key = str((plan or {}).get("queue_id") or "").strip()
        return key or f"object:{id(plan)}"

    def reserve_candidates(predicate, *, slots, reason, place_after_local=False):
        nonlocal window
        try:
            slots = int(slots)
        except Exception:
            slots = 0
        slots = max(0, min(slots, limit))
        if slots <= 0:
            return
        existing = sum(1 for plan in window if predicate(plan))
        missing = max(0, slots - existing)
        if missing <= 0:
            return
        used = {reserve_key(plan) for plan in window}
        for candidate_index, plan in enumerate(plans[limit:], start=limit):
            if missing <= 0:
                break
            key = reserve_key(plan)
            if key in used or not predicate(plan):
                continue
            candidate = dict(plan or {})
            candidate["source_worker_local_page_pack_reserve"] = {
                "reserved": True,
                "provider_ids": _local_page_pack_provider_ids(candidate),
                "original_index": int(candidate_index),
                "eligible_limit": int(limit),
                "reason": reason,
            }
            local_head_count = sum(1 for row in window if _is_local_page_pack_plan(row)) if place_after_local else 0
            matching_head_count = sum(1 for row in window if predicate(row))
            candidate_insert_at = min(insert_at + local_head_count + matching_head_count, max(0, limit - 1))
            reserved = list(window[:-1])
            reserved.insert(candidate_insert_at, candidate)
            window = reserved[:limit]
            used.add(key)
            missing -= 1

    reserve_candidates(
        _is_local_page_pack_plan,
        slots=1,
        reason="local page-pack source would otherwise be outside the selected source-worker pass",
    )
    reserve_candidates(
        _is_local_page_pack_followup_candidate_plan,
        slots=LOCAL_PAGE_PACK_FOLLOWUP_ELIGIBLE_SLOTS,
        reason=(
            "local page-pack source already has history; sibling follow-up would otherwise "
            "be outside the selected source-worker pass"
        ),
        place_after_local=True,
    )

    def runtime_starved_reserve_candidates():
        nonlocal window
        try:
            slots = int(RUNTIME_BUDGET_STARVED_HEAD_SLOTS)
        except Exception:
            slots = 0
        slots = max(0, min(slots, limit))
        if slots <= 0:
            return
        candidates = []
        for candidate_index, plan in enumerate(plans):
            key = reserve_key(plan)
            age = _runtime_budget_starved_age_seconds(plan, now=now)
            if age < RUNTIME_BUDGET_STARVED_MIN_AGE_SECONDS:
                continue
            candidates.append((-int(age), int(candidate_index), key, plan))
        candidates.sort()
        target_candidates = candidates[:slots]
        target_keys = {key for _negative_age, _candidate_index, key, _plan in target_candidates}
        used = {reserve_key(plan) for plan in window}

        for negative_age, candidate_index, key, plan in target_candidates:
            if key in used:
                continue
            candidate = dict(plan or {})
            candidate["source_worker_runtime_budget_starved_reserve"] = {
                "reserved": True,
                "age_seconds": int(-negative_age),
                "original_index": int(candidate_index),
                "eligible_limit": int(limit),
                "reason": "old runtime-budget retry would otherwise be outside the selected source-worker pass",
            }
            local_head_count = sum(1 for row in window if _is_runtime_local_page_pack_plan(row))
            matching_head_count = sum(
                1
                for row in window
                if _runtime_budget_starved_age_seconds(row, now=now) >= RUNTIME_BUDGET_STARVED_MIN_AGE_SECONDS
            )
            candidate_insert_at = min(
                insert_at + local_head_count + matching_head_count,
                max(0, limit - 1),
            )
            reserved = list(window)
            drop_index = next(
                (
                    index
                    for index in range(len(reserved) - 1, -1, -1)
                    if reserve_key(reserved[index]) not in target_keys
                ),
                len(reserved) - 1,
            )
            if 0 <= drop_index < len(reserved):
                del reserved[drop_index]
            reserved.insert(candidate_insert_at, candidate)
            window = reserved[:limit]
            used.add(key)

    runtime_starved_reserve_candidates()

    def source_retry_starved_reserve_candidates():
        nonlocal window
        try:
            slots = int(SOURCE_RETRY_STARVED_HEAD_SLOTS)
        except Exception:
            slots = 0
        slots = max(0, min(slots, limit))
        if slots <= 0:
            return
        candidates = []
        for candidate_index, plan in enumerate(plans):
            key = reserve_key(plan)
            age = _source_retry_starved_age_seconds(plan, now=now)
            if age < SOURCE_RETRY_STARVED_MIN_AGE_SECONDS:
                continue
            candidates.append((-int(age), int(candidate_index), key, plan))
        candidates.sort()
        target_candidates = candidates[:slots]
        target_keys = {key for _negative_age, _candidate_index, key, _plan in target_candidates}
        used = {reserve_key(plan) for plan in window}

        for negative_age, candidate_index, key, plan in target_candidates:
            if key in used:
                continue
            candidate = dict(plan or {})
            candidate["source_worker_source_retry_starved_reserve"] = {
                "reserved": True,
                "age_seconds": int(-negative_age),
                "original_index": int(candidate_index),
                "eligible_limit": int(limit),
                "reason": "old automatic source retry would otherwise be outside the selected source-worker pass",
            }
            local_head_count = sum(1 for row in window if _is_runtime_local_page_pack_plan(row))
            runtime_head_count = sum(
                1
                for row in window
                if _runtime_budget_starved_age_seconds(row, now=now) >= RUNTIME_BUDGET_STARVED_MIN_AGE_SECONDS
            )
            matching_head_count = sum(
                1
                for row in window
                if _source_retry_starved_age_seconds(row, now=now) >= SOURCE_RETRY_STARVED_MIN_AGE_SECONDS
            )
            candidate_insert_at = min(
                insert_at + local_head_count + runtime_head_count + matching_head_count,
                max(0, limit - 1),
            )
            reserved = list(window)
            drop_index = next(
                (
                    index
                    for index in range(len(reserved) - 1, -1, -1)
                    if reserve_key(reserved[index]) not in target_keys
                ),
                len(reserved) - 1,
            )
            if 0 <= drop_index < len(reserved):
                del reserved[drop_index]
            reserved.insert(candidate_insert_at, candidate)
            window = reserved[:limit]
            used.add(key)

    source_retry_starved_reserve_candidates()
    return window[:limit]


def _runtime_budget(max_run_seconds):
    seconds = _float(max_run_seconds, 0.0)
    return max(0.0, seconds) if seconds > 0 else 0.0


def _runtime_head_comic_pack_plan_ids(plans):
    head = set()
    for plan in plans or []:
        if _recent_trusted_comic_pack_runtime_priority(plan) != 0:
            continue
        head.add(id(plan))
        if len(head) >= max(0, int(COMIC_PACK_BACKLOG_HEAD_SLOTS or 0)):
            break
    return head


def _runtime_comic_pack_head_priority(plan, head_plan_ids):
    return 0 if id(plan) in (head_plan_ids or set()) else 1


def _runtime_local_page_pack_head_plan_ids(plans, limit=LOCAL_PAGE_PACK_RUNTIME_HEAD_SLOTS):
    try:
        limit = int(limit)
    except Exception:
        limit = LOCAL_PAGE_PACK_RUNTIME_HEAD_SLOTS
    limit = max(0, min(limit, 50))
    if limit <= 0:
        return set()
    head = set()

    try:
        direct_limit = int(LOCAL_PAGE_PACK_DIRECT_RUNTIME_HEAD_SLOTS)
    except Exception:
        direct_limit = 1
    direct_limit = max(0, min(direct_limit, limit))
    for plan in plans or []:
        if len(head) >= direct_limit:
            break
        if not _is_local_page_pack_plan(plan):
            continue
        head.add(id(plan))

    for plan in plans or []:
        if id(plan) in head:
            continue
        if not _is_runtime_local_page_pack_plan(plan):
            continue
        head.add(id(plan))
        if len(head) >= limit:
            break
    return head


def _runtime_direct_local_page_pack_head_priority(plan, head_plan_ids):
    return 0 if id(plan) in (head_plan_ids or set()) and _is_local_page_pack_plan(plan) else 1


def _runtime_local_page_pack_head_priority(plan, head_plan_ids):
    return 0 if id(plan) in (head_plan_ids or set()) else 1


def _optional_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _runtime_budget_skip_timestamp(plan):
    plan = plan if isinstance(plan, dict) else {}
    latest_attempt = plan.get("latest_source_attempt") if isinstance(plan.get("latest_source_attempt"), dict) else {}
    for source in (plan, latest_attempt):
        for key in ("latest_attempt_at", "activity_at", "updated_at", "started_at", "completed_at", "created_at"):
            ts = _optional_float(source.get(key))
            if ts is not None and ts > 0:
                return ts
    return None


def _runtime_budget_skip_text(plan):
    plan = plan if isinstance(plan, dict) else {}
    latest_attempt = plan.get("latest_source_attempt") if isinstance(plan.get("latest_source_attempt"), dict) else {}
    pieces = [_plan_text_blob(plan)]
    for source in (plan, latest_attempt):
        for key in ("kind", "attempt_kind", "event_type", "source_attempt_kind"):
            value = source.get(key)
            if value not in (None, ""):
                pieces.append(str(value))
    return " ".join(pieces).lower()


def _is_runtime_budget_skipped_plan(plan):
    text = _runtime_budget_skip_text(plan)
    if "source_runtime_budget_skipped" in text:
        return True
    return any(
        token in text
        for token in (
            "autopilot runtime budget reached",
            "runtime budget has",
            "did not start before the worker runtime budget",
        )
    )


def _runtime_budget_starved_age_seconds(plan, *, now=None):
    if not _is_runtime_budget_skipped_plan(plan):
        return 0
    ts = _runtime_budget_skip_timestamp(plan)
    if ts is None:
        return 0
    if now is None:
        now = time.time()
    return max(0, int(_float(now, 0.0) - ts))


def _runtime_budget_starved_plan_ids(plans, limit=RUNTIME_BUDGET_STARVED_HEAD_SLOTS, *, now=None):
    try:
        limit = int(limit)
    except Exception:
        limit = RUNTIME_BUDGET_STARVED_HEAD_SLOTS
    limit = max(0, min(limit, 10))
    if limit <= 0:
        return set()
    candidates = []
    for index, plan in enumerate(plans or []):
        age = _runtime_budget_starved_age_seconds(plan, now=now)
        if age < RUNTIME_BUDGET_STARVED_MIN_AGE_SECONDS:
            continue
        candidates.append((-age, index, id(plan)))
    candidates.sort()
    return {plan_id for _age, _index, plan_id in candidates[:limit]}


def _runtime_budget_starved_priority(plan, head_plan_ids):
    return 0 if id(plan) in (head_plan_ids or set()) else 1


def _runtime_budget_starved_age_priority(plan, head_plan_ids, *, now=None):
    if id(plan) not in (head_plan_ids or set()):
        return 0
    return -_runtime_budget_starved_age_seconds(plan, now=now)


def _is_source_retry_starved_plan(plan):
    plan = plan if isinstance(plan, dict) else {}
    if _plan_is_active_transfer_or_import(plan):
        return False
    if not _plan_looks_comic_pack_eligible(plan) or not _has_trusted_comic_pack_lane(plan):
        return False
    text = _runtime_budget_skip_text(plan)
    return any(
        token in text
        for token in (
            "automatic sources had no actionable candidate",
            "source ladder",
            "source_ladder",
            "slskd checked; no candidates found",
            "slskd source errored",
        )
    )


def _source_retry_starved_age_seconds(plan, *, now=None):
    if not _is_source_retry_starved_plan(plan):
        return 0
    ts = _runtime_budget_skip_timestamp(plan)
    if ts is None:
        return 0
    if now is None:
        now = time.time()
    return max(0, int(_float(now, 0.0) - ts))


def _source_retry_starved_plan_ids(plans, limit=SOURCE_RETRY_STARVED_HEAD_SLOTS, *, now=None):
    try:
        limit = int(limit)
    except Exception:
        limit = SOURCE_RETRY_STARVED_HEAD_SLOTS
    limit = max(0, min(limit, 10))
    if limit <= 0:
        return set()
    candidates = []
    for index, plan in enumerate(plans or []):
        age = _source_retry_starved_age_seconds(plan, now=now)
        if age < SOURCE_RETRY_STARVED_MIN_AGE_SECONDS:
            continue
        candidates.append((-age, index, id(plan)))
    candidates.sort()
    return {plan_id for _age, _index, plan_id in candidates[:limit]}


def _source_retry_starved_priority(plan, head_plan_ids):
    return 0 if id(plan) in (head_plan_ids or set()) else 1


def _source_retry_starved_age_priority(plan, head_plan_ids, *, now=None):
    if id(plan) not in (head_plan_ids or set()):
        return 0
    return -_source_retry_starved_age_seconds(plan, now=now)


def _local_page_pack_fast_lane_priority(plan):
    if _is_local_page_pack_sibling_rotation_plan(plan):
        return 0
    provider_ids = set(_selected_provider_ids(plan))
    if not provider_ids.intersection(LOCAL_PAGE_PACK_FAST_LANE_PROVIDER_IDS):
        return 1
    if int((plan or {}).get(AUTOMATED_ATTEMPT_COUNT_FIELD) or 0) > 0:
        return 1
    return 0


def _runtime_remaining_seconds(started_monotonic, max_run_seconds, *, reserved_seconds=0.0):
    budget = _runtime_budget(max_run_seconds)
    if budget <= 0:
        return None
    elapsed = max(0.0, time.monotonic() - float(started_monotonic or time.monotonic()))
    return max(0.0, budget - elapsed - _float(reserved_seconds, 0.0) - RUNTIME_CLEANUP_SECONDS)


def _runtime_budget_order(plans, *, max_run_seconds=None, source_http_timeout_seconds=None, now=None):
    plans = list(plans or [])
    if _runtime_budget(max_run_seconds) <= 0 or len(plans) <= 1:
        return plans
    bucket_seconds = max(1, int(RUNTIME_BUDGET_COST_BUCKET_SECONDS))
    comic_pack_head_ids = _runtime_head_comic_pack_plan_ids(plans)
    local_page_pack_head_ids = _runtime_local_page_pack_head_plan_ids(plans)
    runtime_starved_head_ids = _runtime_budget_starved_plan_ids(plans, now=now)
    source_retry_starved_head_ids = _source_retry_starved_plan_ids(plans, now=now)
    direct_local_page_pack_head_ids = {
        id(plan)
        for plan in plans
        if id(plan) in local_page_pack_head_ids and _is_local_page_pack_plan(plan)
    }
    return [
        row
        for _initial_search, _comic_head, _runtime_starved, _runtime_starved_age, _direct_local_head, _local_head, _source_retry_starved, _source_retry_starved_age, _round, _fast_lane, _coverage, _bucket, _backlog_priority, _impact, _index, row in sorted(
            (
                (
                    0 if (plan or {}).get(INITIAL_SEARCH_PRIORITY_FIELD) else 1,
                    _runtime_comic_pack_head_priority(plan, comic_pack_head_ids),
                    _runtime_budget_starved_priority(plan, runtime_starved_head_ids),
                    _runtime_budget_starved_age_priority(plan, runtime_starved_head_ids, now=now),
                    _runtime_direct_local_page_pack_head_priority(plan, direct_local_page_pack_head_ids),
                    _runtime_local_page_pack_head_priority(plan, local_page_pack_head_ids),
                    _source_retry_starved_priority(plan, source_retry_starved_head_ids),
                    _source_retry_starved_age_priority(plan, source_retry_starved_head_ids, now=now),
                    _series_round_index(plan),
                    _local_page_pack_fast_lane_priority(plan),
                    0 if int((plan or {}).get(AUTOMATED_ATTEMPT_COUNT_FIELD) or 0) <= 0 else 1,
                    int(_plan_runtime_estimate(plan, source_http_timeout_seconds=source_http_timeout_seconds) // bucket_seconds),
                    -int((plan or {}).get(COMIC_PACK_BACKLOG_PRIORITY_FIELD) or 0),
                    -_series_backlog_count(plan),
                    index,
                    plan,
                )
                for index, plan in enumerate(plans)
            ),
            key=lambda item: item[:15],
        )
    ]


def _apply_runtime_budget(plans, *, max_run_seconds=None, started_monotonic=None, source_http_timeout_seconds=None):
    selected = []
    skipped = []
    reserved = 0.0
    for plan in _runtime_budget_order(
        plans,
        max_run_seconds=max_run_seconds,
        source_http_timeout_seconds=source_http_timeout_seconds,
    ):
        estimate = _plan_runtime_estimate(plan, source_http_timeout_seconds=source_http_timeout_seconds)
        remaining = _runtime_remaining_seconds(
            started_monotonic,
            max_run_seconds,
            reserved_seconds=reserved,
        )
        if remaining is not None and remaining < estimate:
            row = dict(plan or {})
            row["runtime_estimate_seconds"] = int(estimate)
            row["runtime_remaining_seconds"] = int(remaining)
            skipped.append(row)
            continue
        selected.append(plan)
        reserved += estimate
    return selected, skipped


def _runnable_plans(plans, *, eligible_limit=None, operator_payloads=None, default_limit=None, now=None, comic_pack_child_lane_limit=None):
    selected = []
    for plan in plans or []:
        status = (plan or {}).get("status")
        if status == "eligible" or (status == "manual_operator_required" and _has_operator_payload(plan, operator_payloads)):
            selected.append(plan)
    selected = _annotate_series_backlog_counts(selected)
    selected = _annotate_automated_attempt_counts(selected)
    selected = _annotate_comic_pack_backlog_priority(selected)
    if eligible_limit not in (None, ""):
        selected = _spread_by_source_attempt_coverage(selected)
        selection_limit = _bounded_limit(eligible_limit, default=len(selected) or 1, maximum=500)
        selection_pool = selected
        selected = _reserve_local_page_pack_eligible_slot(
            selected,
            selection_limit,
            now=now,
        )
        selected = _reserve_initial_search_opportunity(selection_pool, selected, selection_limit)
        selected = _reserve_aged_zero_provider_coverage_slot(selection_pool, selected, selection_limit)
        if _missing_recovery_enabled():
            selected = _apply_missing_recovery_cohort(
                selection_pool,
                selected,
                selection_limit,
                now=now,
            )
    elif default_limit not in (None, ""):
        selected = _spread_by_source_attempt_coverage(selected)
        selection_limit = _bounded_limit(default_limit, default=len(selected) or 1, maximum=500)
        selection_pool = selected
        selected = _reserve_local_page_pack_eligible_slot(
            selected,
            selection_limit,
            now=now,
        )
        selected = _reserve_initial_search_opportunity(selection_pool, selected, selection_limit)
        selected = _reserve_aged_zero_provider_coverage_slot(selection_pool, selected, selection_limit)
        if _missing_recovery_enabled():
            selected = _apply_missing_recovery_cohort(
                selection_pool,
                selected,
                selection_limit,
                now=now,
            )
    selected = _annotate_series_round_indexes(selected)
    selected = _mark_direct_local_page_pack_runtime_heads(selected)
    selected = _slice_selected_provider_lanes(
        selected,
        comic_pack_child_lane_limit=comic_pack_child_lane_limit,
    )
    return selected


def _reserve_aged_zero_provider_coverage_slot(plans, window, limit):
    """Admit at most one already-classified aged row from beyond the SQL scan."""

    plans = list(plans or [])
    window = list(window or [])[: max(1, int(limit or 1))]
    candidate = next(
        (
            plan
            for plan in plans
            if isinstance((plan or {}).get(AGED_ZERO_PROVIDER_COVERAGE_RESERVE_FIELD), dict)
            and (plan or {}).get(AGED_ZERO_PROVIDER_COVERAGE_RESERVE_FIELD, {}).get("reserved")
            and (plan or {}).get("status") == "eligible"
        ),
        None,
    )
    if not candidate:
        return window
    candidate_id = str(candidate.get("queue_id") or "")
    if any(str((plan or {}).get("queue_id") or "") == candidate_id for plan in window):
        return window
    if not window:
        return [candidate]
    reserved = list(window)
    reserved[-1] = candidate
    return reserved


def _record_aged_zero_provider_coverage_evidence(db_path, plan, *, now=None):
    evidence = (plan or {}).get(AGED_ZERO_PROVIDER_COVERAGE_RESERVE_FIELD)
    if not isinstance(evidence, dict) or not evidence.get("reserved"):
        return {}
    queue_id = str((plan or {}).get("queue_id") or "").strip()
    if not queue_id:
        return {}
    now = time.time() if now is None else now
    with inkdrop_state.connect_read(db_path) as con:
        prior = con.execute(
            """
            select count(*) from history_events
            where entity_type='queue_item' and entity_id=?
              and event_type='source_worker_aged_zero_coverage_reserved'
            """,
            (queue_id,),
        ).fetchone()[0]
    payload = {
        "status": "eligible_but_outside_scan",
        "scheduler_rank": int(evidence.get("scheduler_rank") or 0),
        "series_queue_round": int(evidence.get("series_queue_round") or 0),
        "zero_automated_provider_coverage": True,
        "deferral_age_seconds": int(evidence.get("deferral_age_seconds") or 0),
        "deferral_pass_count": int(prior or 0) + 1,
        "normal_scan_limit": int(evidence.get("normal_scan_limit") or 0),
    }
    return inkdrop_state.record_history_event(
        db_path,
        event_type="source_worker_aged_zero_coverage_reserved",
        entity_type="queue_item",
        entity_id=queue_id,
        series_id=(plan or {}).get("series_id"),
        issue_id=(plan or {}).get("issue_id"),
        source="source_worker_scheduler",
        message="Aged zero-coverage queue row reserved outside the normal scheduler scan",
        raw=payload,
        created_at=now,
    )


def _pending_direct_stage_limit(queue_limit, eligible_limit=None):
    base = eligible_limit if eligible_limit not in (None, "") else queue_limit
    return _bounded_limit(base, default=50, maximum=PENDING_DIRECT_STAGE_LIMIT_MAX)


def _provider_filter_includes_managed_folder(provider_ids):
    provider_ids = {
        str(value or "").strip().lower()
        for value in _list(provider_ids)
        if str(value or "").strip()
    }
    if not provider_ids:
        return True
    return bool(provider_ids.intersection(MANAGED_FOLDER_PROVIDER_IDS))


def _run_managed_folder_sources(db_path, *, provider_ids=None, queue_ids=None, dry_run=True, now=None):
    if not _provider_filter_includes_managed_folder(provider_ids):
        return {
            "ok": True,
            "skipped": True,
            "reason": "provider_filter_excludes_managed_folder",
            "provider_ids": sorted(MANAGED_FOLDER_PROVIDER_IDS),
            "dry_run": bool(dry_run),
        }
    provider_id = "suwayomi_managed_folder"
    gate = suwayomi_managed_folder.managed_folder_automation_gate(db_path, provider_id)
    if not gate.get("ok"):
        return {
            "ok": True,
            "skipped": True,
            "reason": gate.get("reason") or "managed_folder_not_enabled",
            "provider_id": provider_id,
            "dry_run": bool(dry_run),
            "gate": gate,
        }
    result = suwayomi_managed_folder.audit_suwayomi_managed_folder(
        db_path=db_path,
        provider_id=provider_id,
        queue_ids=queue_ids,
        apply=not dry_run,
        now=now,
    )
    return {
        "ok": bool(result.get("ok")),
        "skipped": False,
        "reason": result.get("reason") or "",
        "provider_id": provider_id,
        "dry_run": bool(dry_run),
        "result": result,
        "matched_count": int(result.get("matched_count") or 0),
        "would_promote_import_ready_count": int(result.get("would_promote_import_ready_count") or 0),
        "promoted_import_ready_count": int(result.get("promoted_import_ready_count") or 0),
        "already_staged_import_ready_count": int(result.get("already_staged_import_ready_count") or 0),
        "promotion_blocked_count": int(result.get("promotion_blocked_count") or 0),
        "blocked_count": int(result.get("blocked_count") or 0),
        "mutates_database": bool(result.get("mutates_database")),
        "mutates_filesystem": bool(result.get("mutates_filesystem")),
    }


def _run_summary(
    schedule,
    runs,
    pending_direct_stage=None,
    managed_folder_stage=None,
    pre_schedule_cleanup=None,
    provider_pass_failure_skips=None,
):
    runs = list(runs or [])
    pending_direct_stage = pending_direct_stage if isinstance(pending_direct_stage, dict) else {}
    managed_folder_stage = managed_folder_stage if isinstance(managed_folder_stage, dict) else {}
    pre_schedule_cleanup = pre_schedule_cleanup if isinstance(pre_schedule_cleanup, dict) else {}
    provider_pass_failure_skips = [
        row for row in (provider_pass_failure_skips or []) if isinstance(row, dict)
    ]
    schedule_summary = (schedule or {}).get("summary") or {}
    by_result = {}
    pass_failure_provider_counts = {}
    for row in provider_pass_failure_skips:
        budget = row.get("provider_pass_failure_budget") if isinstance(row.get("provider_pass_failure_budget"), dict) else {}
        for provider_id in budget.get("exhausted_provider_ids") or []:
            provider_id = str(provider_id or "").strip().lower()
            if provider_id:
                pass_failure_provider_counts[provider_id] = pass_failure_provider_counts.get(provider_id, 0) + 1
    attempts_selected = 0
    attempts_recorded = 0
    download_tasks_created = 0
    direct_tasks_staged = 0
    source_tasks_staged = 0
    source_tasks_failed = 0
    for run in runs:
        status = "ok" if run.get("ok") else str(run.get("reason") or "failed")
        by_result[status] = by_result.get(status, 0) + 1
        recording = ((run.get("result") or {}).get("recording") or {})
        attempts_selected += int(recording.get("attempts_selected") or 0)
        attempts_recorded += int(recording.get("attempts_recorded") or 0)
        download_tasks_created += int(recording.get("download_tasks_created") or 0)
        direct_stage = ((run.get("result") or {}).get("direct_stage") or {})
        direct_tasks_staged += int(direct_stage.get("tasks_staged") or 0)
        source_tasks_staged += int(direct_stage.get("tasks_staged") or 0)
        source_tasks_failed += int(direct_stage.get("tasks_failed") or 0)
        download_client_handoff = ((run.get("result") or {}).get("download_client_handoff") or {})
        source_tasks_staged += int(download_client_handoff.get("tasks_handed_off") or 0)
        source_tasks_failed += int(download_client_handoff.get("tasks_failed") or 0)
    for pending_run in pending_direct_stage.get("runs") or []:
        direct_stage = (pending_run.get("result") or {}) if isinstance(pending_run.get("result"), dict) else {}
        direct_tasks_staged += int(direct_stage.get("tasks_staged") or 0)
        source_tasks_staged += int(direct_stage.get("tasks_staged") or 0)
        source_tasks_failed += int(direct_stage.get("tasks_failed") or 0)
    if managed_folder_stage and not managed_folder_stage.get("skipped"):
        source_tasks_staged += int(managed_folder_stage.get("promoted_import_ready_count") or 0)
        source_tasks_staged += int(managed_folder_stage.get("already_staged_import_ready_count") or 0)
        source_tasks_failed += int(managed_folder_stage.get("promotion_blocked_count") or 0)
    return {
        "scheduled": int(schedule_summary.get("total") or 0),
        "eligible": int(schedule_summary.get("eligible") or 0),
        "active_handoff": int(schedule_summary.get("active_handoff") or 0),
        "provider_wait": int(schedule_summary.get("provider_wait") or 0),
        "waiting_for_retry": int(schedule_summary.get("waiting_for_retry") or 0),
        "source_worker_cooldowns": int(schedule_summary.get("source_worker_cooldowns") or 0),
        "provider_timeout_circuits": int(schedule_summary.get("provider_timeout_circuits") or 0),
        "provider_pass_failure_skips": len(provider_pass_failure_skips),
        "provider_pass_failure_skip_providers": dict(sorted(pass_failure_provider_counts.items())),
        "manual_operator_required": int(schedule_summary.get("manual_operator_required") or 0),
        "blocked_no_jobs": int(schedule_summary.get("blocked_no_jobs") or 0),
        "runs": len(runs),
        "by_run_result": dict(sorted(by_result.items())),
        "attempts_selected": attempts_selected,
        "attempts_recorded": attempts_recorded,
        "download_tasks_created": download_tasks_created,
        "direct_tasks_staged": direct_tasks_staged,
        "source_tasks_staged": source_tasks_staged,
        "source_tasks_failed": source_tasks_failed,
        "pending_direct_stage_runs": len(pending_direct_stage.get("runs") or []),
        "managed_folder_stage_runs": 0 if not managed_folder_stage or managed_folder_stage.get("skipped") else 1,
        "managed_folder_promoted": int(managed_folder_stage.get("promoted_import_ready_count") or 0),
        "managed_folder_already_staged": int(managed_folder_stage.get("already_staged_import_ready_count") or 0),
        "managed_folder_blocked": int(managed_folder_stage.get("promotion_blocked_count") or 0),
        "retryable_source_candidate_searching_requeued": int(
            pre_schedule_cleanup.get("retryable_source_candidate_searching_requeued") or 0
        ),
        "active_queue_terminal_wanted_cleanup": int(
            pre_schedule_cleanup.get("active_queue_terminal_wanted_cleanup") or 0
        ),
    }


def _budget_summary(max_run_seconds, budget_skipped_plans, *, source_http_timeout_seconds=None):
    budget = _runtime_budget(max_run_seconds)
    skipped = list(budget_skipped_plans or [])
    timeout = _float(source_http_timeout_seconds, 0.0)
    return {
        "enabled": bool(budget > 0),
        "max_run_seconds": int(budget) if budget > 0 else 0,
        "cleanup_seconds": RUNTIME_CLEANUP_SECONDS if budget > 0 else 0,
        "source_http_timeout_seconds": int(timeout) if timeout > 0 else 0,
        "source_http_request_runtime_estimate_seconds": (
            _source_http_request_runtime_estimate_seconds(source_http_timeout_seconds) if budget > 0 else 0
        ),
        "budget_skipped": len(skipped),
        "budget_skipped_queue_ids": [plan.get("queue_id") for plan in skipped if plan.get("queue_id")],
    }


def _run_pre_schedule_cleanups(
    db_path,
    *,
    execute_jobs=False,
    dry_run=True,
    now=None,
    lock_retry_attempts=None,
    lock_retry_initial_delay=None,
):
    if not execute_jobs or dry_run:
        return {"retryable_source_candidate_searching_requeued": 0, "active_queue_terminal_wanted_cleanup": 0}
    try:
        import inkdrop_state
    except Exception as exc:
        return {
            "retryable_source_candidate_searching_requeued": 0,
            "active_queue_terminal_wanted_cleanup": 0,
            "error": f"inkdrop_state_import_failed:{exc}",
        }
    now = time.time() if now is None else now
    try:
        attempts = max(1, int(lock_retry_attempts or 3))
    except Exception:
        attempts = 3
    try:
        initial_delay = max(0.1, float(lock_retry_initial_delay or 1.0))
    except Exception:
        initial_delay = 1.0
    retry_metrics = {}

    def _cleanup():
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            requeued = inkdrop_state.cleanup_retryable_source_candidate_searching_queue_rows(con, now)
            terminal_wanted_cleaned = inkdrop_state.cleanup_active_queue_rows_with_terminal_wanted(con, now)
            if requeued or terminal_wanted_cleaned:
                inkdrop_state.update_sync_meta(con, now, "source_worker_pre_schedule_cleanup")
            con.commit()
            return int(requeued or 0), int(terminal_wanted_cleaned or 0)

    try:
        changed, terminal_wanted_cleaned = inkdrop_state.with_db_lock_retry(
            _cleanup,
            attempts=attempts,
            initial_delay=initial_delay,
            attempt_metrics=retry_metrics,
        )
    except Exception as exc:
        if not inkdrop_state.is_database_locked_error(exc):
            raise
        return {
            "retryable_source_candidate_searching_requeued": 0,
            "active_queue_terminal_wanted_cleanup": 0,
            "skipped": True,
            "reason": "database_locked",
            "error": str(exc),
            "lock_retry_attempts": attempts,
            "lock_retry_retries": int(retry_metrics.get("retries") or 0),
        }
    return {
        "retryable_source_candidate_searching_requeued": int(changed or 0),
        "active_queue_terminal_wanted_cleanup": int(terminal_wanted_cleaned or 0),
        "lock_retry_attempts": int(retry_metrics.get("attempts") or 1),
        "lock_retry_retries": int(retry_metrics.get("retries") or 0),
    }


def run_source_worker_batch(
    db_path,
    *,
    source_http_get=None,
    direct_http_get=None,
    candidate_headers_by_provider=None,
    operator_payloads=None,
    source_memory_db_path=None,
    source_memory_cooldown_seconds=None,
    staging_root=None,
    queue_ids=None,
    states=None,
    due_only=True,
    include_operator=True,
    include_blocked=False,
    provider_ids=None,
    queue_limit=50,
    job_limit=20,
    attempt_cooldown_seconds=0,
    provider_timeout_window_seconds=0,
    provider_timeout_threshold=0,
    provider_timeout_cooldown_seconds=0,
    provider_fetch_failure_window_seconds=0,
    provider_fetch_failure_threshold=0,
    provider_fetch_failure_cooldown_seconds=0,
    run_limit=None,
    eligible_limit=None,
    execute_jobs=False,
    dry_run=True,
    stage_direct=False,
    stage_managed_folders=False,
    handoff_download_clients=False,
    record_lock_retry_attempts=None,
    record_lock_retry_initial_delay=None,
    max_run_seconds=None,
    source_http_timeout_seconds=None,
    comic_pack_child_lane_limit=None,
    now=None,
):
    """Plan a source-worker batch and optionally run eligible rows.

    Defaults are intentionally conservative: `execute_jobs=False` only returns
    the scheduler plan, and `dry_run=True` prevents persistence if execution is
    explicitly requested.
    """

    now = time.time() if now is None else now
    started_monotonic = time.monotonic()
    claim_owner = f"source-worker:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
    claim_lease_seconds = max(60, min(int(max_run_seconds or 900) + 120, 3600))
    queue_scan_limit = _queue_scan_limit(
        queue_limit,
        eligible_limit=eligible_limit,
        queue_ids=queue_ids,
        due_only=due_only,
    )
    pending_download_client_handoff = {}
    claim_skips = []
    pending_handoff_queue_ids = []
    if execute_jobs and handoff_download_clients:
        pending_handoff_queue_ids = coordinator.pending_download_client_handoff_queue_ids(
            db_path,
            limit=min(
                PENDING_DOWNLOAD_CLIENT_HANDOFF_SCAN_LIMIT_MAX,
                _pending_direct_stage_limit(queue_limit, eligible_limit=eligible_limit),
            ),
            queue_ids=queue_ids,
            now=now,
        )
        pending_runs = []
        pending_budget_skipped_queue_ids = []
        for pending_queue_id in pending_handoff_queue_ids:
            remaining = _runtime_remaining_seconds(started_monotonic, max_run_seconds)
            if remaining is not None and remaining <= 0:
                pending_budget_skipped_queue_ids.append(pending_queue_id)
                continue
            claim = None
            if not dry_run:
                claim = inkdrop_state.claim_queue_item(
                    db_path,
                    pending_queue_id,
                    claim_owner,
                    operation="source_worker_download_client_handoff",
                    lease_seconds=claim_lease_seconds,
                    raw={"phase": "pending_download_client_handoff"},
                )
                if not claim.get("acquired"):
                    claim_skips.append({
                        "queue_id": pending_queue_id,
                        "operation": "source_worker_download_client_handoff",
                        "claim": claim,
                    })
                    continue
            try:
                result = coordinator.handoff_download_client_tasks(
                    db_path,
                    pending_queue_id,
                    dry_run=dry_run,
                    limit=job_limit,
                    max_successful=PENDING_DOWNLOAD_CLIENT_HANDOFF_LIMIT_MAX,
                    stop_on_failure=True,
                    now=now,
                )
            finally:
                if claim and claim.get("acquired"):
                    inkdrop_state.release_queue_claim(db_path, pending_queue_id, claim_owner)
            pending_runs.append(
                {
                    "queue_id": pending_queue_id,
                    "ok": bool(result.get("ok")),
                    "reason": result.get("reason") or "",
                    "result": result,
                }
            )
            if int(result.get("tasks_handed_off") or 0) >= PENDING_DOWNLOAD_CLIENT_HANDOFF_LIMIT_MAX:
                break
            if int(result.get("tasks_failed") or 0) > 0:
                break
        pending_download_client_handoff = {
            "ok": all(run.get("ok") for run in pending_runs),
            "replay_limit": PENDING_DOWNLOAD_CLIENT_HANDOFF_LIMIT_MAX,
            "scan_limit": PENDING_DOWNLOAD_CLIENT_HANDOFF_SCAN_LIMIT_MAX,
            "queue_ids": pending_handoff_queue_ids,
            "budget_skipped_queue_ids": pending_budget_skipped_queue_ids,
            "runs": pending_runs,
            "tasks_available": sum(
                int((run.get("result") or {}).get("tasks_available") or 0)
                for run in pending_runs
            ),
            "tasks_handed_off": sum(
                int((run.get("result") or {}).get("tasks_handed_off") or 0)
                for run in pending_runs
            ),
            "tasks_deferred_by_recovery_policy": sum(
                int((run.get("result") or {}).get("tasks_deferred_by_recovery_policy") or 0)
                for run in pending_runs
            ),
            "tasks_failed": sum(
                int((run.get("result") or {}).get("tasks_failed") or 0)
                for run in pending_runs
            ),
        }

    # A pending row existing isn't recovery activity -- an intrinsically
    # oversized accepted task (bigger than the hourly handoff cap) can sit
    # here forever, policy-deferred without ever reaching the client. Only
    # suppress the normal schedule when this pass actually made or attempted
    # real client progress on a pending row; an all-policy-deferred batch
    # must fall through to ordinary provider scheduling (which already knows
    # to exclude these queue IDs) so unrelated series aren't stalled behind
    # one row that can never become admissible by waiting.
    recovering_pending_handoff = bool(
        int(pending_download_client_handoff.get("tasks_handed_off") or 0)
        or int(pending_download_client_handoff.get("tasks_failed") or 0)
    )
    if recovering_pending_handoff:
        pre_schedule_cleanup = {
            "retryable_source_candidate_searching_requeued": 0,
            "active_queue_terminal_wanted_cleanup": 0,
            "skipped": True,
            "reason": "pending_download_client_handoff_priority",
        }
        schedule = {
            "ok": True,
            "plans": [],
            "summary": {},
            "deferred": True,
            "reason": "pending_download_client_handoff_priority",
        }
        eligible = []
        execution_candidates = []
        budget_skipped = []
        dynamic_runtime_fill = False
    else:
        pre_schedule_cleanup = _run_pre_schedule_cleanups(
            db_path,
            execute_jobs=execute_jobs,
            dry_run=dry_run,
            now=now,
            lock_retry_attempts=record_lock_retry_attempts,
            lock_retry_initial_delay=record_lock_retry_initial_delay,
        )
        schedule = scheduler.source_worker_queue_plan(
            db_path,
            limit=queue_scan_limit,
            queue_ids=queue_ids,
            states=states,
            due_only=due_only,
            include_operator=include_operator,
            include_blocked=include_blocked,
            provider_ids=provider_ids,
            job_limit=job_limit,
            attempt_cooldown_seconds=attempt_cooldown_seconds,
            provider_timeout_window_seconds=provider_timeout_window_seconds,
            provider_timeout_threshold=provider_timeout_threshold,
            provider_timeout_cooldown_seconds=provider_timeout_cooldown_seconds,
            provider_fetch_failure_window_seconds=provider_fetch_failure_window_seconds,
            provider_fetch_failure_threshold=provider_fetch_failure_threshold,
            provider_fetch_failure_cooldown_seconds=provider_fetch_failure_cooldown_seconds,
            now=now,
        )
        eligible = _runnable_plans(
            schedule.get("plans") or [],
            eligible_limit=eligible_limit,
            operator_payloads=operator_payloads,
            default_limit=queue_limit,
            now=now,
            comic_pack_child_lane_limit=comic_pack_child_lane_limit,
        )
        dynamic_runtime_fill = bool(execute_jobs and not dry_run)
        if dynamic_runtime_fill:
            execution_candidates = _runtime_budget_order(
                eligible,
                max_run_seconds=max_run_seconds,
                source_http_timeout_seconds=source_http_timeout_seconds,
            )
            eligible = []
            budget_skipped = []
        else:
            eligible, budget_skipped = _apply_runtime_budget(
                eligible,
                max_run_seconds=max_run_seconds,
                started_monotonic=started_monotonic,
                source_http_timeout_seconds=source_http_timeout_seconds,
            )
            execution_candidates = eligible

    cached_source_http_get, source_http_cache = _cached_source_http_get(source_http_get)
    runs = []
    provider_pass_failures = {}
    provider_pass_failure_reasons = {}
    provider_pass_failure_slices = []
    provider_pass_failure_skips = []
    pending_direct_stage = {}
    managed_folder_stage = {}
    if execute_jobs and not recovering_pending_handoff:
        if handoff_download_clients:
            pending_handoff_set = set(pending_handoff_queue_ids)
            execution_candidates = [
                plan
                for plan in execution_candidates
                if str((plan or {}).get("queue_id") or "").strip() not in pending_handoff_set
            ]
        if stage_managed_folders:
            managed_folder_stage = _run_managed_folder_sources(
                db_path,
                provider_ids=provider_ids,
                queue_ids=queue_ids,
                dry_run=dry_run,
                now=now,
            )
        if stage_direct:
            execution_queue_ids = {
                str((plan or {}).get("queue_id") or "").strip()
                for plan in execution_candidates
                if str((plan or {}).get("queue_id") or "").strip()
            }
            pending_queue_ids = coordinator.pending_direct_stage_queue_ids(
                db_path,
                limit=_pending_direct_stage_limit(queue_limit, eligible_limit=eligible_limit),
                exclude_queue_ids=execution_queue_ids,
                queue_ids=queue_ids,
            )
            pending_runs = []
            pending_budget_skipped_queue_ids = []
            for pending_queue_id in pending_queue_ids:
                remaining = _runtime_remaining_seconds(started_monotonic, max_run_seconds)
                if remaining is not None and remaining <= 0:
                    pending_budget_skipped_queue_ids.append(pending_queue_id)
                    continue
                claim = None
                if not dry_run:
                    claim = inkdrop_state.claim_queue_item(
                        db_path,
                        pending_queue_id,
                        claim_owner,
                        operation="source_worker_direct_stage",
                        lease_seconds=claim_lease_seconds,
                        raw={"phase": "pending_direct_stage"},
                    )
                    if not claim.get("acquired"):
                        claim_skips.append({"queue_id": pending_queue_id, "operation": "source_worker_direct_stage", "claim": claim})
                        continue
                try:
                    result = coordinator.stage_direct_download_tasks(
                        db_path,
                        pending_queue_id,
                        http_get=direct_http_get,
                        staging_root=staging_root,
                        source_memory_db_path=source_memory_db_path,
                        dry_run=dry_run,
                        limit=job_limit,
                        now=now,
                    )
                finally:
                    if claim and claim.get("acquired"):
                        inkdrop_state.release_queue_claim(db_path, pending_queue_id, claim_owner)
                pending_runs.append(
                    {
                        "queue_id": pending_queue_id,
                        "ok": bool(result.get("ok")),
                        "reason": result.get("reason") or "",
                        "result": result,
                    }
                )
            pending_direct_stage = {
                "ok": all(run.get("ok") for run in pending_runs),
                "queue_ids": pending_queue_ids,
                "budget_skipped_queue_ids": pending_budget_skipped_queue_ids,
                "runs": pending_runs,
                "tasks_available": sum(int((run.get("result") or {}).get("tasks_available") or 0) for run in pending_runs),
                "tasks_staged": sum(int((run.get("result") or {}).get("tasks_staged") or 0) for run in pending_runs),
                "tasks_failed": sum(int((run.get("result") or {}).get("tasks_failed") or 0) for run in pending_runs),
            }
        for plan in execution_candidates:
            plan, provider_failure_budget = _provider_pass_failure_budget_for_plan(
                plan,
                provider_pass_failures,
                provider_pass_failure_reasons,
            )
            if plan is None:
                provider_pass_failure_skips.append(provider_failure_budget)
                continue
            if provider_failure_budget:
                provider_pass_failure_slices.append(provider_failure_budget)
            remaining = _runtime_remaining_seconds(started_monotonic, max_run_seconds)
            estimate = _plan_runtime_estimate(plan, source_http_timeout_seconds=source_http_timeout_seconds)
            if remaining is not None and remaining < estimate:
                rotated_plan = _rotate_local_page_pack_fast_lane_for_runtime_budget(plan)
                rotated_estimate = (
                    _plan_runtime_estimate(rotated_plan, source_http_timeout_seconds=source_http_timeout_seconds)
                    if rotated_plan is not None
                    else None
                )
                if rotated_plan is not None and rotated_estimate is not None and remaining >= rotated_estimate:
                    plan = rotated_plan
                    estimate = rotated_estimate
                else:
                    if not dry_run:
                        _persist_runtime_budget_skip_evidence(
                            db_path,
                            plan,
                            estimate=estimate,
                            remaining=remaining,
                            now=now,
                        )
                    skipped = dict(plan or {})
                    skipped["runtime_estimate_seconds"] = int(estimate)
                    skipped["runtime_remaining_seconds"] = int(remaining)
                    budget_skipped.append(skipped)
                    continue
            selected_provider_ids = [
                str(value).strip()
                for value in _list(plan.get("selected_provider_ids") or provider_ids)
                if str(value or "").strip()
            ]
            if not selected_provider_ids:
                if dynamic_runtime_fill:
                    eligible.append(plan)
                runs.append(
                    {
                        "queue_id": plan.get("queue_id"),
                        "ok": False,
                        "reason": "no_selected_providers",
                        "result": {},
                    }
                )
                continue
            queue_id = plan.get("queue_id")
            claim = None
            if not dry_run:
                claim = inkdrop_state.claim_queue_item(
                    db_path,
                    queue_id,
                    claim_owner,
                    operation="source_worker_search",
                    lease_seconds=claim_lease_seconds,
                    raw={"provider_ids": selected_provider_ids},
                )
                if not claim.get("acquired"):
                    # This plan was ADMITTED (passed runtime-budget and
                    # provider-selection checks) but never actually ran a
                    # provider request -- another worker already holds the
                    # queue claim. It must NOT appear in selected_queue_ids/
                    # selected_plans (eligible), because downstream projection
                    # treats "selected" as "a search happened" and would
                    # otherwise manufacture a false searched_no_candidates
                    # result for a row the provider never touched. The
                    # truthful record of this skip lives in queue_claim_skips.
                    claim_skips.append({"queue_id": queue_id, "operation": "source_worker_search", "claim": claim})
                    continue
                _record_aged_zero_provider_coverage_evidence(db_path, plan, now=now)
            # Only now -- past every pre-execution skip (runtime budget,
            # provider-pass-failure budget, no-selected-providers, and a lost
            # queue claim) -- is this plan actually about to execute a
            # provider request (or, for dry_run, would have). This is the
            # single place selected_queue_ids/selected_plans gets populated,
            # so "selected" can never again mean anything other than "ran".
            if dynamic_runtime_fill:
                eligible.append(plan)
            # Absolute wall-clock deadline for this queue run: whatever pass
            # budget remains right now. Slow serial fetch plans (MangaDex
            # volume page packs) stop cleanly at this deadline and persist the
            # partial evidence instead of dying when the slot expires.
            fetch_deadline = None if remaining is None else time.time() + max(0.0, remaining)
            try:
                result = coordinator.run_source_worker_for_queue(
                    db_path,
                    queue_id,
                    source_http_get=cached_source_http_get,
                    direct_http_get=direct_http_get,
                    candidate_headers_by_provider=candidate_headers_by_provider,
                    operator_payloads=operator_payloads,
                    source_memory_db_path=source_memory_db_path,
                    source_memory_cooldown_seconds=source_memory_cooldown_seconds,
                    staging_root=staging_root,
                    include_operator=include_operator,
                    include_blocked=include_blocked,
                    provider_ids=selected_provider_ids,
                    job_limit=job_limit,
                    run_limit=run_limit,
                    dry_run=dry_run,
                    stage_direct=stage_direct,
                    handoff_download_clients=handoff_download_clients,
                    record_lock_retry_attempts=record_lock_retry_attempts,
                    record_lock_retry_initial_delay=record_lock_retry_initial_delay,
                    fetch_deadline=fetch_deadline,
                    now=now,
                )
            finally:
                if claim and claim.get("acquired"):
                    inkdrop_state.release_queue_claim(db_path, queue_id, claim_owner)
            runs.append(
                {
                    "queue_id": plan.get("queue_id"),
                    "provider_ids": selected_provider_ids,
                    "ok": bool(result.get("ok")),
                    "reason": result.get("reason") or "",
                    "result": result,
                }
            )
            _increment_provider_pass_failures(
                provider_pass_failures,
                provider_pass_failure_reasons,
                result,
            )
    ok = (
        bool(schedule.get("ok"))
        and all(run.get("ok") for run in runs)
        and (not pending_download_client_handoff or bool(pending_download_client_handoff.get("ok")))
        and (not pending_direct_stage or bool(pending_direct_stage.get("ok")))
        and (not managed_folder_stage or bool(managed_folder_stage.get("ok")))
    )
    mutation_runs = bool(
        runs
        or (pending_download_client_handoff.get("runs") if pending_download_client_handoff else [])
        or (pending_direct_stage.get("runs") if pending_direct_stage else [])
        or (managed_folder_stage and not managed_folder_stage.get("skipped"))
        or int(pre_schedule_cleanup.get("retryable_source_candidate_searching_requeued") or 0)
        or int(pre_schedule_cleanup.get("active_queue_terminal_wanted_cleanup") or 0)
    )
    filesystem_runs = bool(
        (runs and stage_direct)
        or (pending_direct_stage.get("runs") if pending_direct_stage else [])
        or (
            managed_folder_stage
            and not managed_folder_stage.get("skipped")
            and bool(managed_folder_stage.get("mutates_filesystem"))
        )
    )
    return {
        "source_worker_batch_contract_version": CONTRACT_VERSION,
        "ok": ok,
        "dry_run": bool(dry_run),
        "execute_jobs": bool(execute_jobs),
        "mutates_database": bool(mutation_runs and not dry_run),
        "mutates_filesystem": bool(
            (
                (filesystem_runs and stage_direct)
                or (managed_folder_stage and bool(managed_folder_stage.get("mutates_filesystem")))
            )
            and not dry_run
        ),
        "mutates_download_client": bool(
            (runs or (pending_download_client_handoff.get("runs") if pending_download_client_handoff else []))
            and handoff_download_clients
            and not dry_run
        ),
        "due_only": bool(due_only),
        "queue_limit": _bounded_limit(queue_limit),
        "queue_scan_limit": queue_scan_limit,
        "eligible_limit": eligible_limit,
        "attempt_cooldown_seconds": attempt_cooldown_seconds,
        "provider_timeout_window_seconds": provider_timeout_window_seconds,
        "provider_timeout_threshold": provider_timeout_threshold,
        "provider_timeout_cooldown_seconds": provider_timeout_cooldown_seconds,
        "provider_fetch_failure_window_seconds": provider_fetch_failure_window_seconds,
        "provider_fetch_failure_threshold": provider_fetch_failure_threshold,
        "provider_fetch_failure_cooldown_seconds": provider_fetch_failure_cooldown_seconds,
        "comic_pack_child_lane_limit": _comic_pack_prowlarr_child_lane_limit(comic_pack_child_lane_limit),
        "max_run_seconds": int(_runtime_budget(max_run_seconds)) if _runtime_budget(max_run_seconds) > 0 else 0,
        "source_http_timeout_seconds": int(_float(source_http_timeout_seconds, 0.0))
        if _float(source_http_timeout_seconds, 0.0) > 0
        else 0,
        "source_http_request_runtime_estimate_seconds": _source_http_request_runtime_estimate_seconds(
            source_http_timeout_seconds
        ),
        "runtime_budget": _budget_summary(
            max_run_seconds,
            budget_skipped,
            source_http_timeout_seconds=source_http_timeout_seconds,
        ),
        "runtime_budget_dynamic_fill": dynamic_runtime_fill,
        "selected_queue_ids": [plan.get("queue_id") for plan in eligible],
        "selected_plans": eligible,
        "budget_skipped_queue_ids": [plan.get("queue_id") for plan in budget_skipped if plan.get("queue_id")],
        "provider_pass_failure_skipped_queue_ids": [
            plan.get("queue_id") for plan in provider_pass_failure_skips if plan.get("queue_id")
        ],
        "provider_pass_failure_skips": provider_pass_failure_skips,
        "queue_claim_owner": claim_owner,
        "queue_claim_skips": claim_skips,
        "provider_pass_failure_slices": provider_pass_failure_slices,
        "provider_pass_failure_counts": dict(sorted(provider_pass_failures.items())),
        "source_http_cache": dict(source_http_cache),
        "schedule": schedule,
        "pre_schedule_cleanup": pre_schedule_cleanup,
        "runs": runs,
        "pending_direct_stage": pending_direct_stage,
        "pending_download_client_handoff": pending_download_client_handoff,
        "managed_folder_stage": managed_folder_stage,
        "summary": _run_summary(
            schedule,
            runs,
            pending_direct_stage=pending_direct_stage,
            managed_folder_stage=managed_folder_stage,
            pre_schedule_cleanup=pre_schedule_cleanup,
            provider_pass_failure_skips=provider_pass_failure_skips,
        ),
    }
