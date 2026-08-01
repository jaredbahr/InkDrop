#!/usr/bin/env python3
"""Regression coverage for bounded first-pass/retry scheduler fairness."""

from types import SimpleNamespace
from unittest import mock
import copy
import time

import inkdrop_series_autopilot as autopilot


def require(condition, message):
    if not condition:
        raise AssertionError(message)


NOW = 2_000_000_000.0


# Both the web launcher and Docker scheduler converge on this gate. One real
# import-ready row stops the next provider cycle; clearing it releases search.
with mock.patch.object(autopilot, "state_ready_import_count", return_value=1):
    one_ready_gate = autopilot.import_backlog_priority_gate({"items": {}})
require(one_ready_gate["active"] is True, one_ready_gate)
require(one_ready_gate["priority_active"] is True, one_ready_gate)
require(one_ready_gate["hard_blocked"] is False, one_ready_gate)
with mock.patch.object(autopilot, "state_ready_import_count", return_value=0):
    drained_gate = autopilot.import_backlog_priority_gate({"items": {}})
require(drained_gate["active"] is False, drained_gate)
require(drained_gate["priority_active"] is False, drained_gate)

# A high optional priority threshold must not raise or disable the independent
# hard backlog limit.
with (
    mock.patch.object(autopilot, "AUTOPILOT_IMPORT_BACKLOG_PRIORITY_MIN", 100),
    mock.patch.object(autopilot, "AUTOPILOT_IMPORT_BACKLOG_HARD_LIMIT", 24),
    mock.patch.object(autopilot, "state_ready_import_count", return_value=24),
):
    hard_limit_gate = autopilot.import_backlog_priority_gate({"items": {}})
require(hard_limit_gate["active"] is True, hard_limit_gate)
require(hard_limit_gate["priority_active"] is False, hard_limit_gate)
require(hard_limit_gate["hard_blocked"] is True, hard_limit_gate)
require(hard_limit_gate["hard_limit"] == 24, hard_limit_gate)


def row(series, *, first_pass, retry_after=0, updated_at=0):
    item = {
        "key": series.lower().replace(" ", "-"),
        "series": series,
        "issue": "1",
        "state": "queued",
        "present_in_watch": True,
        "created_at": NOW - 10_000,
        "updated_at": updated_at,
        "retry_after": retry_after,
    }
    if not first_pass:
        item["source_attempt_counts"] = {
            source: 1 for source in autopilot.VALID_SOURCE_ORDER if source != "local"
        }
    return item


items = {
    "fresh-a": row("Fresh A", first_pass=True),
    "fresh-b": row("Fresh B", first_pass=True),
    "fresh-c": row("Fresh C", first_pass=True),
    # Oldest Due was annotated recently. Its original retry promise must still
    # outrank a newer retry group instead of being reset by updated_at.
    "oldest-due": row("Oldest Due", first_pass=False, retry_after=NOW - 3 * 86400, updated_at=NOW - 10),
    "newer-due": row("Newer Due", first_pass=False, retry_after=NOW - 3600, updated_at=NOW - 10 * 86400),
}
args = SimpleNamespace(series=[], retry_needs_you=False, force=False, max_series=3)

with mock.patch.object(autopilot.time, "time", return_value=NOW):
    selected = autopilot.due_series({"items": items}, args)

names = [series for series, _rows in selected]
require(len(names) == 3, names)
require(any(name.startswith("Fresh") for name in names), names)
require("Oldest Due" in names, names)
require("Newer Due" not in names, names)

# Observational refreshes must not starve an older unscheduled retry. These
# fields are updated by status/reconciliation passes even when no provider or
# downloader work occurred. A genuinely newer attempt remains newer even when
# its generic updated_at is older.
observed_old = row("Observed Old Retry", first_pass=False, updated_at=NOW - 1)
observed_old.update(
    {
        "last_attempt_at": NOW - 4 * 86400,
        "last_slskd_at": NOW - 4 * 86400,
        "last_slskd_autoresolve_at": NOW - 1,
        "retry_waiting_normalized_at": NOW - 1,
    }
)
meaningful_new = row("Meaningful New Retry", first_pass=False, updated_at=NOW - 10 * 86400)
meaningful_new.update({"last_attempt_at": NOW - 60, "last_slskd_at": NOW - 60})
fresh_control = row("Fresh Control", first_pass=True, updated_at=NOW - 1)
observational_args = SimpleNamespace(series=[], retry_needs_you=False, force=False, max_series=2)
with mock.patch.object(autopilot.time, "time", return_value=NOW):
    observational_names = [
        series
        for series, _rows in autopilot.due_series(
            {
                "items": {
                    "observed-old": observed_old,
                    "meaningful-new": meaningful_new,
                    "fresh-control": fresh_control,
                }
            },
            observational_args,
        )
    ]
require("Fresh Control" in observational_names, observational_names)
require("Observed Old Retry" in observational_names, observational_names)
require("Meaningful New Retry" not in observational_names, observational_names)
require(
    autopilot.queue_retry_activity_ts(observed_old) < autopilot.queue_retry_activity_ts(meaningful_new),
    (autopilot.queue_retry_activity_ts(observed_old), autopilot.queue_retry_activity_ts(meaningful_new)),
)

# Explicit promised retry times remain the primary ordering signal even when
# observation timestamps disagree.
explicit_old = row("Explicit Old", first_pass=False, retry_after=NOW - 7200, updated_at=NOW - 1)
explicit_old["last_attempt_at"] = NOW - 10
explicit_new = row("Explicit New", first_pass=False, retry_after=NOW - 3600, updated_at=NOW - 86400)
explicit_new["last_attempt_at"] = NOW - 86400
with mock.patch.object(autopilot.time, "time", return_value=NOW):
    require(
        autopilot.due_group_sort_key("Explicit Old", [explicit_old])
        < autopilot.due_group_sort_key("Explicit New", [explicit_new]),
        "stable retry activity overrode explicit retry promises",
    )

# Fresh rows remain pending on the next pass. Once the oldest retry rotates to
# a future timer, the next due retry must still receive a reserved slot.
items["oldest-due"]["retry_after"] = NOW + 1800
with mock.patch.object(autopilot.time, "time", return_value=NOW):
    second_names = [series for series, _rows in autopilot.due_series({"items": items}, args)]
require(any(name.startswith("Fresh") for name in second_names), second_names)
require("Newer Due" in second_names, second_names)

# Saturated cached SLSKD recovery must leave both a series slot and runtime for
# the ordinary due queue. This protects newly added and retry-due monitored
# series from being displaced for an entire bounded autopilot pass.
hot_items = {
    f"hot-{index}": row(f"Hot {index}", first_pass=False, retry_after=NOW - 3600)
    for index in range(8)
}
for item in hot_items.values():
    item["test_hot_retry"] = True
due_items = {
    f"due-{index}": row(
        f"Due {index}",
        first_pass=False,
        retry_after=NOW - 86_400 - index,
    )
    for index in range(12)
}
for item in due_items.values():
    item["last_slskd_candidate_count"] = 1
saturated_queue = {"items": {**hot_items, **due_items}}
hot_args = SimpleNamespace(
    series=[],
    retry_needs_you=False,
    force=False,
    max_series=6,
    slskd_hot_retry_max=6,
    slskd_max_queries=5,
    slskd_probe_budget_seconds=180,
    skip_slskd=False,
)


def is_test_hot_retry(item, _now):
    return bool(item.get("test_hot_retry"))


with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "slskd_hot_retry_candidate", side_effect=is_test_hot_retry
), mock.patch.object(
    autopilot,
    "has_soon_cached_slskd_autopick",
    side_effect=lambda item, now=None: is_test_hot_retry(item, now),
):
    hot_limit = autopilot.slskd_hot_retry_limit(saturated_queue, hot_args, now=NOW)
    hot_rows = autopilot.slskd_hot_retry_rows(saturated_queue, hot_args)
    due_names = [series for series, _rows in autopilot.due_series(saturated_queue, hot_args)]
    reservation = autopilot.broad_due_runtime_reservation_seconds(
        saturated_queue,
        hot_args,
        now=NOW,
    )

require(hot_limit == 2, hot_limit)
require(len(hot_rows) == 2, len(hot_rows))
require(hot_args.max_series - hot_limit >= 1, (hot_args.max_series, hot_limit))
require(due_names[0].startswith("Hot "), due_names)
require(any(name.startswith("Due ") for name in due_names), due_names)
require(reservation >= autopilot.run_group_start_min_seconds(), reservation)

# A recent cached retry must not monopolize the bounded handoff lane. The
# oldest safe retained result goes first; once it records fresh provider
# activity, the next waiting candidate advances toward its durable task.
aged_hot = row("Aged Safe Candidate", first_pass=False, retry_after=0)
aged_hot.update(
    {
        "key": "aged-safe",
        "test_hot_retry": True,
        "last_slskd_at": NOW - 7200,
        "last_failed_candidate_at": NOW - 7100,
        "last_failed_candidate_reason": "transfer_failed",
        "last_failed_candidate_review_id": "review-aged-safe",
    }
)
fresh_hot = row("Fresh Safe Candidate", first_pass=False, retry_after=0)
fresh_hot.update(
    {
        "key": "fresh-safe",
        "test_hot_retry": True,
        "last_slskd_at": NOW - 60,
        "last_failed_candidate_at": NOW - 7000,
        "last_failed_candidate_reason": "transfer_failed",
        "last_failed_candidate_review_id": "review-fresh-safe",
    }
)
rotation_queue = {"items": {"fresh-safe": fresh_hot, "aged-safe": aged_hot}}
rotation_args = SimpleNamespace(**{**vars(hot_args), "max_series": 1, "slskd_hot_retry_max": 1})
with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "slskd_hot_retry_candidate", side_effect=is_test_hot_retry
):
    first_rotation = autopilot.slskd_hot_retry_rows(rotation_queue, rotation_args)
require([item["key"] for item in first_rotation] == ["aged-safe"], first_rotation)
aged_hot["last_slskd_at"] = NOW
with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "slskd_hot_retry_candidate", side_effect=is_test_hot_retry
):
    second_rotation = autopilot.slskd_hot_retry_rows(rotation_queue, rotation_args)
require([item["key"] for item in second_rotation] == ["fresh-safe"], second_rotation)

# Post-discovery parity: safe cached candidates receive the bounded hot lane,
# while a review-only pack, a previously failed candidate collision, and an
# unavailable candidate remain explicit and cannot enter the handoff payload.
parity_items = {
    "safe-a": row("Safe A", first_pass=False, retry_after=0),
    "safe-b": row("Safe B", first_pass=False, retry_after=0),
    "review-pack": row("Review Pack", first_pass=False, retry_after=0),
    "failed-collision": row("Failed Collision", first_pass=False, retry_after=0),
    "unavailable": row("Unavailable", first_pass=False, retry_after=0),
}
for key, item in parity_items.items():
    item["key"] = key
    item["review_id"] = f"review-{key}"
    item["last_event"] = "SLSKD candidates available for autopick"
parity_items["failed-collision"].update(
    {
        "last_failed_candidate_filename": "Failed Collision 001.cbz",
        "last_failed_candidate_user": "peer-collision",
        "last_failed_candidate_reason": "transfer_failed",
        "last_failed_candidate_review_id": "review-failed-collision",
    }
)


def cached_candidate(filename, *, username="peer-safe", verdict="auto_grab_safe", blockers=None):
    return {
        "filename": filename,
        "username": username,
        "auto_grab": {"verdict": verdict, "blockers": list(blockers or [])},
    }


parity_cache = {
    "review-safe-a": {
        "review_id": "review-safe-a",
        "series": "Safe A",
        "issue": "1",
        "candidates": [cached_candidate("Safe A 001.cbz")],
    },
    "review-safe-b": {
        "review_id": "review-safe-b",
        "series": "Safe B",
        "issue": "1",
        "candidates": [cached_candidate("Safe B 001.cbz")],
    },
    "review-review-pack": {
        "review_id": "review-review-pack",
        "series": "Review Pack",
        "issue": "1",
        "candidates": [
            cached_candidate(
                "Review Pack 001-012.cbz",
                verdict="pack_review_required",
                blockers=["pack_candidate_requires_review"],
            )
        ],
    },
    "review-failed-collision": {
        "review_id": "review-failed-collision",
        "series": "Failed Collision",
        "issue": "1",
        "candidates": [cached_candidate("Failed Collision 001.cbz", username="peer-collision")],
    },
    "review-unavailable": {
        "review_id": "review-unavailable",
        "series": "Unavailable",
        "issue": "1",
        "candidates": [cached_candidate("Unavailable 001.cbz", blockers=["candidate_unavailable"])],
    },
}
parity_queue = {"items": parity_items}
with mock.patch.object(autopilot, "slskd_source_probe_cache", return_value=parity_cache), mock.patch.object(
    autopilot.time, "time", return_value=NOW
):
    parity_hot_rows = autopilot.slskd_hot_retry_rows(parity_queue, hot_args)
    parity_safe_counts = {
        key: autopilot.cached_safe_slskd_candidate_count(item)
        for key, item in parity_items.items()
    }

require([item["key"] for item in parity_hot_rows] == ["safe-a", "safe-b"], parity_hot_rows)
require(parity_safe_counts == {
    "safe-a": 1,
    "safe-b": 1,
    "review-pack": 0,
    "failed-collision": 0,
    "unavailable": 0,
}, parity_safe_counts)

# Automatic-only parity: stale empty/transient/orphan SLSKD signatures rotate
# back to the front of their source ladder after bounded backoff. No Manual
# Search run or candidate cache is required to unlock the fresh probe.
stale_zero = row("Adventureman", first_pass=False, retry_after=NOW - 1)
stale_zero.update(
    {
        "key": "adventureman-9",
        "source_order": ["local", "prowlarr", "rss", "slskd", "comicscodes"],
        "last_slskd_at": NOW - 2 * 3600,
        "last_slskd_status": "searched_no_candidates",
        "last_slskd_candidate_count": 0,
        "last_slskd_detected_count": 0,
        "last_slskd_auto_grab_safe_count": 0,
    }
)
stale_transient = row("Transient Retry", first_pass=False, retry_after=NOW - 1)
stale_transient.update(
    {
        "source_order": ["local", "prowlarr", "slskd"],
        "last_slskd_at": NOW - 10 * 60,
        "last_slskd_status": "timeout",
        "last_slskd_autopick_status": "transient_error",
        "last_slskd_candidate_count": 0,
        "last_slskd_detected_count": 0,
        "last_slskd_auto_grab_safe_count": 0,
    }
)
orphan_signature = row("Gachiakuta", first_pass=False, retry_after=0)
orphan_signature.update(
    {
        "key": "gachiakuta-volume-1",
        "source_order": ["local", "mangadex", "prowlarr", "rss", "comicscodes", "slskd"],
        "source_attempt_counts": {"mangadex": 4, "prowlarr": 9, "rss": 0, "comicscodes": 0, "slskd": 1},
        "last_slskd_status": None,
        "last_slskd_candidate_count": 0,
        "last_slskd_detected_count": 0,
        "last_slskd_auto_grab_safe_count": 0,
    }
)
future_backoff = copy.deepcopy(stale_zero)
future_backoff.update({"key": "future-backoff", "series": "Future Backoff", "retry_after": NOW + 300})
unsafe_result = copy.deepcopy(stale_zero)
unsafe_result.update({"key": "unsafe-result", "series": "Unsafe Result", "last_slskd_candidate_count": 1})
known_bad_result = copy.deepcopy(stale_zero)
known_bad_result.update(
    {
        "key": "known-bad-result",
        "series": "Known Bad Result",
        "last_slskd_candidate_count": 1,
        "last_failed_candidate_review_id": "failed-review",
    }
)

with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "has_cached_safe_slskd_candidate", return_value=False
), mock.patch.object(autopilot, "source_enabled", return_value=True):
    for item in (stale_zero, stale_transient, orphan_signature):
        require(autopilot.slskd_source_result_reprobe_due(item, now=NOW), item)
        require(autopilot.queue_item_source_order(item)[1] == "slskd", item)
        require("slskd" in autopilot.missing_required_source_result_sources(item), item)
    require(not autopilot.slskd_source_result_reprobe_due(future_backoff, now=NOW), future_backoff)
    require(not autopilot.slskd_source_result_reprobe_due(unsafe_result, now=NOW), unsafe_result)
    require(not autopilot.slskd_source_result_reprobe_due(known_bad_result, now=NOW), known_bad_result)

    with mock.patch.object(autopilot, "has_cached_safe_slskd_candidate", return_value=True):
        require(not autopilot.slskd_source_result_reprobe_due(stale_zero, now=NOW), stale_zero)

    normalized_transient = copy.deepcopy(stale_transient)
    autopilot.normalize_waiting_retry_state(normalized_transient, NOW)
    require(normalized_transient.get("slskd_result_reprobe_due_at") == NOW, normalized_transient)
    require(normalized_transient.get("retry_planner_deferral_reason") != "provider_transient_retry", normalized_transient)

    automatic_queue = {"items": {item["key"]: item for item in (stale_zero, stale_transient, orphan_signature)}}
    automatic_selected = autopilot.due_series(automatic_queue, args)
require(len(automatic_selected) == args.max_series, automatic_selected)

bounded_reprobes = {}
for index in range(8):
    item = copy.deepcopy(stale_zero)
    item.update({"key": str(index), "series": f"Automatic Reprobe {index}"})
    bounded_reprobes[str(index)] = item
with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "has_cached_safe_slskd_candidate", return_value=False
):
    bounded_selected = autopilot.due_series({"items": bounded_reprobes}, args)
require(len(bounded_selected) == args.max_series, bounded_selected)

# Recovery saturation must preserve the broad fraction of tiny passes. Cached
# handoffs and automatic reprobes rotate through one shared recovery capacity;
# they cannot jointly displace first-pass/retry work.
capacity_cached = row("Capacity Cached", first_pass=False, retry_after=NOW - 1)
capacity_cached.update({"key": "capacity-cached", "capacity_lane": "cached", "last_slskd_at": NOW - 1000})
capacity_reprobe = copy.deepcopy(stale_zero)
capacity_reprobe.update(
    {
        "key": "capacity-reprobe",
        "series": "Capacity Reprobe",
        "capacity_lane": "reprobe",
        "last_slskd_at": NOW - 2 * 3600,
    }
)
capacity_first = row("Capacity First", first_pass=True, retry_after=0)
capacity_first.update({"key": "capacity-first", "capacity_lane": "first"})
capacity_retry = row("Capacity Retry", first_pass=False, retry_after=NOW - 3600)
capacity_retry.update(
    {
        "key": "capacity-retry",
        "capacity_lane": "retry",
        "last_slskd_candidate_count": 1,
    }
)
capacity_queue = {
    "items": {
        item["key"]: item
        for item in (capacity_cached, capacity_reprobe, capacity_first, capacity_retry)
    }
}


def capacity_cached_safe(item):
    return item.get("capacity_lane") == "cached"


def capacity_cached_due(item, now=None):
    return item.get("capacity_lane") == "cached"


with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "has_cached_safe_slskd_candidate", side_effect=capacity_cached_safe
), mock.patch.object(
    autopilot, "has_soon_cached_slskd_autopick", side_effect=capacity_cached_due
), mock.patch.object(autopilot, "source_enabled", return_value=True):
    capacity_selections = {}
    for max_series in (1, 2, 3):
        capacity_args = SimpleNamespace(series=[], retry_needs_you=False, force=False, max_series=max_series)
        selected_rows = autopilot.due_series(capacity_queue, capacity_args)
        selected_lanes = [rows[0]["capacity_lane"] for _series, rows in selected_rows]
        capacity_selections[max_series] = selected_lanes
        recovery_count = sum(lane in {"cached", "reprobe"} for lane in selected_lanes)
        broad_count = sum(lane in {"first", "retry"} for lane in selected_lanes)
        require(recovery_count <= (0 if max_series == 1 else 1), (max_series, selected_lanes))
        require(broad_count >= 1, (max_series, selected_lanes))
    require(set(capacity_selections[3]) >= {"first", "retry"}, capacity_selections)

    # The broad scheduler still rotates the recovery lane fairly when no
    # cached handoff has already paid the discovery and safety-gate cost.
    require("reprobe" in capacity_selections[2], capacity_selections)
    capacity_reprobe["last_slskd_at"] = NOW
    capacity_reprobe["retry_after"] = NOW + 300
    rotated = autopilot.due_series(
        capacity_queue,
        SimpleNamespace(series=[], retry_needs_you=False, force=False, max_series=2),
    )
    require("cached" in [rows[0]["capacity_lane"] for _series, rows in rotated], rotated)
    capacity_reprobe["last_slskd_at"] = NOW - 2 * 3600
    capacity_reprobe["retry_after"] = NOW - 1

    hot_capacity_args = SimpleNamespace(
        series=[], retry_needs_you=False, force=False, max_series=2,
        slskd_hot_retry_max=2, skip_slskd=False,
    )
    with mock.patch.object(
        autopilot,
        "slskd_hot_retry_candidate",
        side_effect=lambda item, now: item.get("capacity_lane") == "cached",
    ):
        require(autopilot.slskd_hot_retry_limit(capacity_queue, hot_capacity_args, now=NOW) == 1, capacity_queue)
        require(
            [item.get("capacity_lane") for item in autopilot.slskd_hot_retry_rows(capacity_queue, hot_capacity_args)]
            == ["cached"],
            capacity_queue,
        )
        capacity_cached["last_slskd_at"] = NOW - 3 * 3600
        require(autopilot.slskd_hot_retry_limit(capacity_queue, hot_capacity_args, now=NOW) == 1, capacity_queue)
        ordered = autopilot.due_series(capacity_queue, hot_capacity_args)
        remaining_after_hot = [
            rows[0]["capacity_lane"]
            for _series, rows in ordered
            if rows[0]["capacity_lane"] != "cached"
        ]
        require(remaining_after_hot and remaining_after_hot[0] in {"first", "retry"}, ordered)

# Production-shaped recovery fairness: an overdue promised retry at position
# 34 of 76 must receive the single bounded recovery slot. The same group must
# rotate after a fresh attempt while first-pass and retry lanes retain both
# broad slots in a three-series pass.
recovery_backlog = {}
for index in range(76):
    item = copy.deepcopy(stale_zero)
    item.update(
        {
            "key": f"recovery-{index:02d}",
            "series": f"Recovery {index:02d}",
            "last_slskd_at": NOW - 100_000 + index,
            "retry_after": 0,
        }
    )
    recovery_backlog[item["key"]] = item

court = recovery_backlog.pop("recovery-33")
court.update(
    {
        "key": "court-of-owls-1",
        "series": "Absolute Batman: The Court of Owls",
        "queue_identity": "comicvine:86918",
        "last_slskd_at": NOW - 10_800,
        "retry_after": NOW - 7_200,
    }
)
recovery_backlog[court["key"]] = court
court_sibling = copy.deepcopy(court)
court_sibling.update({"key": "court-of-owls-2", "issue": "2"})
recovery_backlog[court_sibling["key"]] = court_sibling
fairness_first = row("Fairness First Pass", first_pass=True)
fairness_first.update({"key": "fairness-first", "fairness_lane": "first"})
fairness_retry = row("Fairness Retry", first_pass=False, retry_after=NOW - 3600)
fairness_retry.update(
    {
        "key": "fairness-retry",
        "fairness_lane": "retry",
        "last_slskd_candidate_count": 1,
    }
)
recovery_queue = {
    "items": {
        **recovery_backlog,
        fairness_first["key"]: fairness_first,
        fairness_retry["key"]: fairness_retry,
    }
}
fairness_args = SimpleNamespace(series=[], retry_needs_you=False, force=False, max_series=3)
with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "has_cached_safe_slskd_candidate", return_value=False
), mock.patch.object(autopilot, "source_enabled", return_value=True):
    fairness_selected = autopilot.due_series(recovery_queue, fairness_args)
    fairness_names = [series for series, _rows in fairness_selected]
    require("Absolute Batman: The Court of Owls" in fairness_names, fairness_names)
    require("Fairness First Pass" in fairness_names, fairness_names)
    require("Fairness Retry" in fairness_names, fairness_names)
    require(len(fairness_names) == len(set(fairness_names)) == 3, fairness_names)
    court_groups = [rows for series, rows in fairness_selected if series == court["series"]]
    require(len(court_groups) == 1 and len(court_groups[0]) == 2, court_groups)

    # The row whose overdue promise admitted a multi-row series must be the
    # first row handed to process_series. A lower-numbered sibling with a newer
    # promise must not consume the bounded slot and leave the admitting row
    # untouched forever.
    promised_row = court_groups[0][0]
    require(promised_row["key"] == "court-of-owls-1", court_groups[0])

    # A fresh result on one selected sibling is also group-level service
    # evidence. The series rotates behind an entirely untouched overdue group
    # even if another sibling retains the same old retry promise.
    court["last_slskd_at"] = NOW
    court["retry_after"] = NOW + 300
    court["state"] = "verified"
    recovery_backlog["recovery-00"]["retry_after"] = NOW - 8_000
    sibling_rotation = autopilot.due_series(recovery_queue, fairness_args)
    sibling_names = [series for series, _rows in sibling_rotation]
    require("Absolute Batman: The Court of Owls" not in sibling_names, sibling_names)
    require(any(name.startswith("Recovery ") for name in sibling_names), sibling_names)

    # Simulate the selected reprobe recording a result and its next bounded
    # backoff. Court must rotate and admit another recovery group next pass.
    court["last_slskd_at"] = NOW
    court["retry_after"] = NOW + 300
    court_sibling["last_slskd_at"] = NOW
    court_sibling["retry_after"] = NOW + 300
    rotated_names = [series for series, _rows in autopilot.due_series(recovery_queue, fairness_args)]
    require("Absolute Batman: The Court of Owls" not in rotated_names, rotated_names)
    require(any(name.startswith("Recovery ") for name in rotated_names), rotated_names)
    require("Fairness First Pass" in rotated_names and "Fairness Retry" in rotated_names, rotated_names)

    # Cached/hot work keeps its recovery priority over an ordinary reprobe;
    # only an unserved overdue promise receives the temporary age boost.
    cached_priority = copy.deepcopy(capacity_cached)
    cached_priority.update({"key": "cached-priority", "series": "Cached Priority", "last_slskd_at": NOW - 200_000})
    cached_queue = {"items": {cached_priority["key"]: cached_priority, **recovery_backlog}}
    with mock.patch.object(
        autopilot,
        "has_cached_safe_slskd_candidate",
        side_effect=lambda item: item.get("key") == "cached-priority",
    ), mock.patch.object(
        autopilot,
        "has_soon_cached_slskd_autopick",
        side_effect=lambda item, now=None: item.get("key") == "cached-priority",
    ):
        cached_selected = autopilot.due_series(cached_queue, SimpleNamespace(
            series=[], retry_needs_you=False, force=False, max_series=1
        ))
    require(cached_selected[0][0] == "Cached Priority", cached_selected)

# Recovery exclusions remain hard boundaries even when a row has an extremely
# old unserved promise. A future retry is also ineligible until its timer.
excluded_recovery = {}
for state in ("downloading", "importing", "source_wait", "needs_you", "verified", "superseded_duplicate"):
    item = copy.deepcopy(stale_zero)
    item.update(
        {
            "key": f"excluded-{state}",
            "series": f"Excluded {state}",
            "state": state,
            "last_slskd_at": NOW - 30_000,
            "retry_after": NOW - 20_000,
        }
    )
    excluded_recovery[item["key"]] = item
future_recovery = copy.deepcopy(stale_zero)
future_recovery.update(
    {
        "key": "future-recovery",
        "series": "Future Recovery",
        "last_slskd_at": NOW - 30_000,
        "retry_after": NOW + 300,
    }
)
excluded_recovery[future_recovery["key"]] = future_recovery
with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "has_cached_safe_slskd_candidate", return_value=False
), mock.patch.object(autopilot, "source_enabled", return_value=True):
    excluded_selected = autopilot.due_series({"items": excluded_recovery}, fairness_args)
require(excluded_selected == [], excluded_selected)

# Cold automatic parity remains independent of both Manual Search and prior
# SLSKD cache state. Repeated local observation updates must not reset the age
# of an untouched row or strand it behind a saturated retry backlog.
cold_gachiakuta = row("Gachiakuta Cold", first_pass=True, updated_at=NOW - 1)
cold_gachiakuta.update(
    {
        "key": "gachiakuta-cold-volume-1",
        "current_source": "local",
        "created_at": NOW - 30 * 86400,
        "queue_created_at": NOW - 30 * 86400,
        "source_order": ["local", "slskd", "prowlarr"],
        "source_attempt_counts": {"slskd": 0, "prowlarr": 0},
        "attempts": [],
    }
)
cold_competition = {
    cold_gachiakuta["key"]: cold_gachiakuta,
    **{
        f"cold-fresh-{index}": row(
            f"Cold Fresh {index}",
            first_pass=True,
            updated_at=NOW - index,
        )
        for index in range(12)
    },
    **{
        f"cold-retry-{index}": row(
            f"Cold Retry {index}",
            first_pass=False,
            retry_after=NOW - 86400 - index,
            updated_at=NOW - index,
        )
        for index in range(12)
    },
}
with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "has_cached_safe_slskd_candidate", return_value=False
), mock.patch.object(autopilot, "source_enabled", return_value=True):
    cold_selected = autopilot.due_series({"items": cold_competition}, args)
    cold_names = [series for series, _rows in cold_selected]
    require("Gachiakuta Cold" in cold_names, cold_names)
    require(not autopilot.slskd_source_result_reprobe_due(cold_gachiakuta, now=NOW), cold_gachiakuta)
    require("slskd" in autopilot.missing_required_source_result_sources(cold_gachiakuta), cold_gachiakuta)
    require(
        autopilot.source_eligible_rows([cold_gachiakuta], args, source="slskd") == [cold_gachiakuta],
        cold_gachiakuta,
    )

handoff = autopilot.apply_slskd_auto_grab(
    parity_queue,
    {
        "auto_grab_safe_count": 1,
        "auto_grab": {
            "rows": [
                {
                    "key": "safe-a",
                    "review_id": "review-safe-a",
                    "series": "Safe A",
                    "issue": "1",
                    "status": "started_waiting",
                    "filename": "Safe A 001.cbz",
                    "username": "peer-safe",
                    "score": 100,
                }
            ]
        },
    },
)
require(handoff["started"] == 1, handoff)
require(parity_items["safe-a"]["state"] == "downloading", parity_items["safe-a"])
for key in ("review-pack", "failed-collision", "unavailable"):
    require(parity_items[key]["state"] == "queued", (key, parity_items[key]))

# If the remaining deadline can fit a hot retry but not that retry plus the
# broad reservation, the hot lane yields without starting SLSKD.
progress_notes = []
with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "slskd_hot_retry_candidate", side_effect=is_test_hot_retry
), mock.patch.object(
    autopilot, "source_runtime_min_seconds", return_value=120
), mock.patch.object(autopilot, "run_slskd") as run_slskd:
    processed_hot = autopilot.process_slskd_hot_retries(
        saturated_queue,
        hot_args,
        progress=lambda **payload: progress_notes.append(payload),
        deadline=NOW + 250,
    )
require(processed_hot == [], processed_hot)
require(not run_slskd.called, "hot retry consumed reserved broad runtime")
require(any("reserving about" in str(note.get("note") or "") for note in progress_notes), progress_notes)

# The smallest supported pass still gives its only slot to broad work.
hot_args.max_series = 1
with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
    autopilot, "slskd_hot_retry_candidate", side_effect=is_test_hot_retry
):
    require(
        autopilot.slskd_hot_retry_limit(saturated_queue, hot_args, now=NOW) == 0,
        "broad slot was not reserved",
    )

# Hot-only queues still obey the total series cap. They may use the available
# pass, but configured hot_retry_max can never expand max_series.
hot_only_queue = {"items": hot_items}
for max_series in (1, 2):
    hot_args.max_series = max_series
    with mock.patch.object(autopilot.time, "time", return_value=NOW), mock.patch.object(
        autopilot, "slskd_hot_retry_candidate", side_effect=is_test_hot_retry
    ):
        require(
            autopilot.slskd_hot_retry_limit(hot_only_queue, hot_args, now=NOW) == max_series,
            (max_series, autopilot.slskd_hot_retry_limit(hot_only_queue, hot_args, now=NOW)),
        )
        require(len(autopilot.slskd_hot_retry_rows(hot_only_queue, hot_args)) == max_series, max_series)

# Startup evidence matching must remain linear at production-like queue/cache
# sizes. These keys intentionally cannot use the direct hash path, exercising
# the indexed fallback while retaining the ordinary identity/ambiguity gate.
large_queue = {
    "items": {
        f"durable-row-{index}": {
            "key": f"durable-row-{index}",
            "series": f"Series {index}",
            "issue": "1",
            "present_in_watch": True,
        }
        for index in range(2_100)
    }
}
large_cache = {
    f"review-{index}": {
        "review_id": f"review-{index}",
        "series": f"Series {index}",
        "issue": "1",
        "candidate_count": 1,
    }
    for index in range(400)
}
target_index = autopilot.build_row_queue_target_index(large_queue)
normalize_calls = 0
real_normalize = autopilot.normalize


def counted_normalize(value):
    global normalize_calls
    normalize_calls += 1
    return real_normalize(value)


def evidence_json(path, default=None):
    if path == autopilot.SLSKD_SOURCE_PROBE_STATUS_FILE:
        return {"items": {}}
    if path == autopilot.SLSKD_SOURCE_PROBE_CACHE_FILE:
        return large_cache
    return default


with mock.patch.object(autopilot, "read_json", side_effect=evidence_json), mock.patch.object(
    autopilot, "normalize", side_effect=counted_normalize
):
    indexed_slskd = autopilot.slskd_index(
        large_queue,
        refresh_cached_verdicts=False,
        target_index=target_index,
    )
require(len(indexed_slskd) == len(large_cache), len(indexed_slskd))
require(normalize_calls < len(large_cache) * 10, normalize_calls)

# An expired annotation budget stops before resolving even the first cached
# evidence row. Previously the deadline only disabled verdict refresh while
# the full quadratic indexing pass continued for minutes.
budget_state = {}
with mock.patch.object(autopilot, "read_json", side_effect=evidence_json), mock.patch.object(
    autopilot, "row_queue_targets"
) as target_lookup:
    expired = autopilot.slskd_index(
        large_queue,
        refresh_cached_verdicts=False,
        deadline=autopilot.time.time() - 1,
        target_index=target_index,
        budget_state=budget_state,
    )
require(expired == {}, expired)
require(not target_lookup.called, "expired SLSKD evidence still scanned the queue")
require(budget_state.get("slskd_index_deadline_reached") is True, budget_state)

# Annotation reports phase timings so future production passes identify the
# exact setup phase without relying on a processed_count=0 heartbeat.
timing_queue = {"items": {"one": large_queue["items"]["durable-row-1"]}}
with mock.patch.object(autopilot, "manual_review_index", return_value={}), mock.patch.object(
    autopilot, "slskd_index", return_value={}
), mock.patch.object(autopilot, "reconciliation_index", return_value={}), mock.patch.object(
    autopilot, "import_status_index", return_value={}
), mock.patch.object(autopilot, "read_waiting_records", return_value={}), mock.patch.object(
    autopilot, "read_manual_source_resolved_records", return_value=({}, {})
), mock.patch.object(autopilot, "read_manual_source_bad_candidate_records", return_value=({}, {})), mock.patch.object(
    autopilot, "read_manual_source_retry_pending_records", return_value=({}, {})
), mock.patch.object(autopilot, "read_json", return_value={}), mock.patch.object(
    autopilot, "active_pack_import_review_ids", return_value={}
):
    timing_summary = autopilot.annotate_states(timing_queue, max_seconds=5, reason="timing_regression")
require("queue_target_index" in timing_summary.get("phase_seconds", {}), timing_summary)
require("slskd_index" in timing_summary.get("phase_seconds", {}), timing_summary)

# The deadline applies inside target-index construction, not only after the
# complete collection has already been normalized.
slow_index_queue = {
    "items": {
        f"slow-{index}": {
            "series": f"Slow Series {index}",
            "issue": "1",
            "present_in_watch": True,
        }
        for index in range(20)
    }
}
slow_index_state = {}


def slow_normalize(value):
    time.sleep(0.01)
    return real_normalize(value)


slow_index_started = time.monotonic()
with mock.patch.object(autopilot, "normalize", side_effect=slow_normalize):
    slow_index = autopilot.build_row_queue_target_index(
        slow_index_queue,
        deadline=time.time() + 0.03,
        budget_state=slow_index_state,
    )
slow_index_elapsed = time.monotonic() - slow_index_started
require(slow_index_state.get("queue_target_index_deadline_reached") is True, slow_index_state)
require(sum(len(rows) for rows in slow_index["all"].values()) < 20, slow_index)
require(slow_index_elapsed < 0.10, slow_index_elapsed)

# Later review-id/waiting maps are budgeted too. A slow target resolution per
# historical waiting record must not run the whole collection after expiry.
slow_waiting = {
    f"waiting-{index}": {
        "review_id": f"waiting-{index}",
        "series": f"Missing Series {index}",
        "issue": "1",
    }
    for index in range(20)
}
slow_target_calls = 0


def slow_target_lookup(*args, **kwargs):
    global slow_target_calls
    slow_target_calls += 1
    time.sleep(0.01)
    return []


deadline_queue = {"items": {"one": large_queue["items"]["durable-row-1"]}}
deadline_started = time.monotonic()
with mock.patch.object(autopilot, "manual_review_index", return_value={}), mock.patch.object(
    autopilot, "slskd_index", return_value={}
), mock.patch.object(autopilot, "reconciliation_index", return_value={}), mock.patch.object(
    autopilot, "import_status_index", return_value={}
), mock.patch.object(autopilot, "kapowarr_folder_prefixes_by_volume_id", return_value={}), mock.patch.object(
    autopilot, "read_waiting_records", return_value=slow_waiting
), mock.patch.object(
    autopilot, "read_manual_source_resolved_records", return_value=({}, {})
), mock.patch.object(
    autopilot, "read_manual_source_bad_candidate_records", return_value=({}, {})
), mock.patch.object(
    autopilot, "read_manual_source_retry_pending_records", return_value=({}, {})
), mock.patch.object(autopilot, "read_json", return_value={}), mock.patch.object(
    autopilot, "row_queue_targets", side_effect=slow_target_lookup
):
    deadline_summary = autopilot.annotate_states(deadline_queue, max_seconds=0.03, reason="deadline_regression")
deadline_elapsed = time.monotonic() - deadline_started
require(deadline_summary.get("stage") == "waiting_review_id_map", deadline_summary)
require(slow_target_calls <= 4, slow_target_calls)
require(deadline_elapsed < 0.10, deadline_elapsed)

# A stale provider-start marker is repaired through the real queue transition,
# persisted immediately, and left for the normal bounded DB sync. Startup must
# not run a second synchronous reconciliation before provider selection.
stale_row = row("Stale Provider Start", first_pass=False)
stale_row.update(
    {
        "state": "searching",
        "current_source": "slskd",
        "last_source_started_source": "slskd",
        "last_source_started_at": NOW - autopilot.STALE_SEARCH_SOURCE_MARKER_SECONDS - 1,
    }
)
stale_queue = {"items": {"stale-provider-start": stale_row}, "history": []}
require(
    autopilot.normalize_stale_source_started_attempts(stale_queue, now=NOW) == 1,
    stale_queue,
)
require(stale_row["state"] == "queued" and stale_row.get("current_source") is None, stale_row)
with mock.patch.object(autopilot, "save_startup_queue_snapshot", return_value=None) as startup_save, mock.patch.object(
    autopilot, "sync_inkdrop_queue_state"
) as forbidden_sync:
    autopilot.persist_startup_queue_normalization(stale_queue, stale_source_started_count=1)
require(startup_save.call_count == 1, startup_save.call_count)
require(not forbidden_sync.called, "startup normalization ran synchronous DB maintenance")
pending_sync = stale_queue.get("inkdrop_state_sync_pending") or {}
require(pending_sync.get("reason") == "stale_source_started_normalized", pending_sync)
require(pending_sync.get("result_reason") == "startup_provider_budget_protected", pending_sync)
with mock.patch.object(
    autopilot.time,
    "time",
    return_value=NOW + autopilot.SLSKD_TRANSIENT_RETRY_SECONDS + 1,
):
    stale_selected = autopilot.due_series(stale_queue, args)
require([series for series, _rows in stale_selected] == ["Stale Provider Start"], stale_selected)

# Starting each provider persists the exact queue marker but must not replay the
# entire queue through InkDrop state before the provider call. The scoped
# provider-target annotation remains the final safety boundary for every call.
provider_row = row("Provider Budget", first_pass=True)
provider_row.update(
    {
        "key": "provider-budget-1",
        "source_order": ["local", "prowlarr", "slskd"],
        "source_attempt_counts": {"prowlarr": 0, "slskd": 0},
    }
)
provider_queue = {"items": {provider_row["key"]: provider_row}, "history": []}
provider_args = SimpleNamespace(
    annotate_timeout_seconds=5,
    dry_run=False,
    force=False,
    exhaustion_cycles=6,
    retry_seconds=1800,
    skip_failed_retry=True,
    skip_prowlarr=False,
    skip_slskd_broad_due_to_busy=False,
)
provider_calls = []
annotation_calls = []


def ready_provider_annotation(_queue, *, max_seconds, reason, row_keys):
    annotation_calls.append((reason, tuple(row_keys), max_seconds))
    return {"ok": True, "processed": len(row_keys), "total": len(row_keys)}


def retained_provider_rows(rows, _args, source=None):
    if source in {None, "local", "prowlarr", "slskd"}:
        return list(rows)
    return []


with mock.patch.object(autopilot, "latest_inkdrop_provider_health", return_value={}), mock.patch.object(
    autopilot, "source_order_for_rows", return_value=["local", "prowlarr", "slskd"]
), mock.patch.object(
    autopilot, "source_eligible_rows", side_effect=retained_provider_rows
), mock.patch.object(
    autopilot, "refresh_series_row_policy", return_value=None
), mock.patch.object(
    autopilot, "source_runtime_min_seconds", return_value=1
), mock.patch.object(
    autopilot, "slskd_broad_probe_kwargs", return_value={}
), mock.patch.object(
    autopilot, "annotate_states", side_effect=ready_provider_annotation
), mock.patch.object(
    autopilot, "save_queue_progress_snapshot"
) as progress_save, mock.patch.object(
    autopilot, "sync_inkdrop_queue_state"
) as forbidden_full_sync, mock.patch.object(
    autopilot,
    "run_source_worker_prowlarr",
    side_effect=lambda *args, **kwargs: provider_calls.append("prowlarr") or {"actions": [], "reviews": []},
), mock.patch.object(
    autopilot, "apply_source_worker_prowlarr_result_to_queue", return_value={}
), mock.patch.object(
    autopilot,
    "run_slskd",
    side_effect=lambda *args, **kwargs: provider_calls.append("slskd") or {"checked": [], "auto_grab": {}},
):
    provider_result = autopilot.process_series(
        provider_queue,
        "Provider Budget",
        [provider_row],
        provider_args,
        deadline=time.time() + 600,
    )

require(provider_calls == ["prowlarr", "slskd"], provider_calls)
require(progress_save.call_count == 2, progress_save.call_count)
require(not forbidden_full_sync.called, "provider start replayed the entire queue before provider access")
require(
    [(reason, keys) for reason, keys, _seconds in annotation_calls]
    == [
        ("provider_target", ("provider-budget-1",)),
        ("provider_target", ("provider-budget-1",)),
    ],
    annotation_calls,
)
require(provider_result["targeted_annotations"]["prowlarr"]["ready"] is True, provider_result)
require(provider_result["targeted_annotations"]["slskd"]["ready"] is True, provider_result)

# A completed exact row discovered by the same scoped annotation must still
# stop the provider and leave the full-queue projection unused.
blocked_row = copy.deepcopy(provider_row)
blocked_row.update({"key": "provider-budget-complete", "state": "queued", "current_source": None})
blocked_queue = {"items": {blocked_row["key"]: blocked_row}, "history": []}
blocked_calls = []


def complete_during_annotation(_queue, *, max_seconds, reason, row_keys):
    blocked_row["state"] = "verified"
    blocked_row["current_source"] = None
    return {"ok": True, "processed": len(row_keys), "total": len(row_keys)}


with mock.patch.object(autopilot, "latest_inkdrop_provider_health", return_value={}), mock.patch.object(
    autopilot, "source_order_for_rows", return_value=["local", "prowlarr"]
), mock.patch.object(
    autopilot, "source_eligible_rows", side_effect=retained_provider_rows
), mock.patch.object(
    autopilot, "refresh_series_row_policy", return_value=None
), mock.patch.object(
    autopilot, "source_runtime_min_seconds", return_value=1
), mock.patch.object(
    autopilot, "slskd_broad_probe_kwargs", return_value={}
), mock.patch.object(
    autopilot, "annotate_states", side_effect=complete_during_annotation
), mock.patch.object(
    autopilot, "save_queue_progress_snapshot"
), mock.patch.object(
    autopilot, "sync_inkdrop_queue_state"
) as blocked_full_sync, mock.patch.object(
    autopilot,
    "run_source_worker_prowlarr",
    side_effect=lambda *args, **kwargs: blocked_calls.append("prowlarr") or {},
):
    blocked_result = autopilot.process_series(
        blocked_queue,
        "Provider Budget",
        [blocked_row],
        provider_args,
        deadline=time.time() + 600,
    )

require(blocked_calls == [], blocked_calls)
require(not blocked_full_sync.called, "blocked provider path replayed the entire queue")
require(blocked_result.get("evidence_changed_state") is True, blocked_result)
require(blocked_row["state"] == "verified", blocked_row)

# A retried row whose retry_after timer was cleared (rather than rescheduled)
# has no active cooldown -- it must land in the retry_due lane the scheduler
# actually drains every pass, not the unscheduled "queued" catch-all that is
# only serviced once every other lane is empty. Series that never accumulate
# enough concurrent work to fill every higher-priority lane -- exactly the
# "down to the last issue or two" case -- were starved for days by this.
orphaned_row = row("Orphaned Retry", first_pass=False, retry_after=0)
with (
    mock.patch.object(autopilot, "has_soon_cached_slskd_autopick", return_value=False),
    mock.patch.object(autopilot, "slskd_source_result_reprobe_due", return_value=False),
    mock.patch.object(autopilot, "stale_downloader_send_result", return_value=False),
    mock.patch.object(autopilot, "has_missing_required_source_result", return_value=False),
):
    orphaned_bucket = autopilot.scheduler_bucket_for_rows([orphaned_row], now=NOW)
require(orphaned_bucket == "retry_due", orphaned_bucket)

# Before this fix, the same row fell through every named lane to the
# unscheduled "queued" catch-all, which is only drained once every other
# lane is empty on a given pass -- effectively never on an active catalog.
with mock.patch.object(autopilot, "retry_effectively_due", return_value=False):
    with (
        mock.patch.object(autopilot, "has_soon_cached_slskd_autopick", return_value=False),
        mock.patch.object(autopilot, "slskd_source_result_reprobe_due", return_value=False),
        mock.patch.object(autopilot, "stale_downloader_send_result", return_value=False),
        mock.patch.object(autopilot, "has_missing_required_source_result", return_value=False),
    ):
        pre_fix_bucket = autopilot.scheduler_bucket_for_rows([orphaned_row], now=NOW)
require(pre_fix_bucket == "queued", pre_fix_bucket)

# It must also actually win a selection slot against real competition, not
# merely be classified correctly while still losing out every pass. Two
# ordinary retry_due groups fill both slots on their own before the
# scheduler ever looks at "queued" -- so with the bug present, the orphaned
# series would lose to them on every single pass, forever (this is the
# actual mechanism behind Court of Owls/Joker War/Walking Dead going 6-14
# days without a single search attempt despite having only 1-2 wanted
# issues left). Fixed, it competes fairly on genuine staleness instead.
competitor_a = row("Competitor A", first_pass=False, retry_after=NOW - 3600)
competitor_a["source_attempt_counts"] = {"prowlarr": 1, "rss": 1, "slskd": 1, "comicscodes": 1, "suwayomi": 1}
competitor_b = row("Competitor B", first_pass=False, retry_after=NOW - 1800)
competitor_b["source_attempt_counts"] = {"prowlarr": 1, "rss": 1, "slskd": 1, "comicscodes": 1, "suwayomi": 1}
starvation_args = SimpleNamespace(series=[], retry_needs_you=False, force=False, max_series=2)
with (
    mock.patch.object(autopilot.time, "time", return_value=NOW),
    mock.patch.object(autopilot, "has_soon_cached_slskd_autopick", return_value=False),
    mock.patch.object(autopilot, "slskd_source_result_reprobe_due", return_value=False),
    mock.patch.object(autopilot, "stale_downloader_send_result", return_value=False),
    mock.patch.object(autopilot, "has_missing_required_source_result", return_value=False),
):
    contested_selected = [
        series
        for series, _rows in autopilot.due_series(
            {"items": {"orphaned": orphaned_row, "competitor-a": competitor_a, "competitor-b": competitor_b}},
            starvation_args,
        )
    ]
require("Orphaned Retry" in contested_selected, contested_selected)

print("inkdrop autopilot fairness smoke: PASS")
