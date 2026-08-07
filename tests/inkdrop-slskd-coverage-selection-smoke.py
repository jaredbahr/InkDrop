#!/usr/bin/env python3
"""Bounded coverage/availability selection for SLSKD directory cohorts."""

import json
from unittest import mock

from core import inkdrop_slskd_source_probe as probe


MIB = 1024 * 1024


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wanted(issue, state="queued"):
    return {
        "review_id": f"descender-{issue:03d}",
        "series": "Descender",
        "query": "Descender",
        "issue": str(issue),
        "year": "2015",
        "media_type": "comic",
        "autopilot_queue": True,
        "autopilot_state": state,
    }


def comic(issue, *, root="Comics/Descender", size=64 * MIB, extension="cbr"):
    return {
        "filename": f"{root}\\Descender {issue:03d} (2015) (Digital).{extension}",
        "size": size,
    }


complete = {
    "username": "complete-private-peer",
    "uploadSpeed": 3_730_000,
    "queueLength": 0,
    "hasFreeUploadSlot": True,
    "files": [comic(issue) for issue in range(1, 33)],
}
busy_partial = {
    "username": "busy-partial-peer",
    "uploadSpeed": 8_000_000,
    "queueLength": 188,
    "hasFreeUploadSlot": False,
    "files": [comic(issue) for issue in range(1, 7)],
}
locked = {
    "username": "locked-peer",
    "uploadSpeed": 10_000_000,
    "queueLength": 0,
    "hasFreeUploadSlot": False,
    "files": [],
    "lockedFiles": [comic((issue % 32) + 1) for issue in range(1000)],
}
ambiguous = {
    "username": "adversarial-peer",
    "uploadSpeed": 9_000_000,
    "queueLength": 0,
    "hasFreeUploadSlot": True,
    "files": [
        comic(1, root="Comics/Descender/Ascender"),
        comic(2, root="Comics/Descender/Ascender"),
        {"filename": "Comics/Descender/Descender 001-032 Complete.cbz", "size": 900 * MIB},
        {"filename": "Comics/Descender/Descender Covers 001.cbz", "size": 8 * MIB},
        {"filename": "Comics/Descender/readme.txt", "size": 100},
    ],
}

items = [wanted(issue) for issue in range(1, 33)]
items.append(wanted(33, state="completed"))

# A locked response appears first and is much larger than the observation cap. The
# free/queue-zero complete directory must still receive the bounded budget.
observations, observation_summary = probe.slskd_series_directory_observations(
    [locked, busy_partial, ambiguous, complete],
    max_files=32,
    items=items,
)
complete_observation = next(
    row for row in observations
    if row["directory"] == "Comics/Descender" and row["username"] == "complete-private-peer"
)
require(complete_observation["file_count"] == 32, complete_observation)
require(observation_summary["observed_file_count"] <= 32, observation_summary)
require(observation_summary["locked_file_count_skipped"] == 1000, observation_summary)
cache = {}
original_issue_cap = probe.SERIES_RUN_MAX_ISSUES
original_byte_cap = probe.SERIES_RUN_MAX_BYTES
try:
    probe.SERIES_RUN_MAX_ISSUES = 8
    probe.SERIES_RUN_MAX_BYTES = 512 * MIB
    with mock.patch.object(probe, "bad_candidate_match", side_effect=lambda review_id, _candidate: review_id == "descender-007"):
        result = probe.apply_series_directory_opportunities([], items, cache, observations=observations)

    require(result["selected_issue_count"] == 8, result)
    require(result["selected_bytes"] <= probe.SERIES_RUN_MAX_BYTES, result)
    require("descender-007" not in result["selected_review_ids"], result)
    require("descender-033" not in result["selected_review_ids"], result)
    for review_id in result["selected_review_ids"]:
        entry = cache[review_id]
        candidate = entry["candidates"][0]
        opportunity = entry["series_directory_opportunity"]
        require(candidate["series_directory_handoff"] is True, candidate)
        require(candidate["series_directory_active_wanted_coverage"] >= 31, candidate)
        require(candidate["has_free_upload_slot"] is True and candidate["queue_length"] == 0, candidate)
        require(opportunity["active_wanted_coverage"] >= 31, opportunity)
        require("Ascender" not in candidate["filename"] and "Complete" not in candidate["filename"], candidate)

    persisted = json.dumps(cache, sort_keys=True)
    for secret in ("complete-private-peer", "busy-partial-peer", "locked-peer", "Comics/Descender"):
        require(secret not in persisted, f"private route leaked: {secret}")

    # The private capsule remains memory-only, is per-file (never folder-grab),
    # and makes the fresh cache actionable without repeating the same query.
    first_entry = cache[result["selected_review_ids"][0]]
    first_candidate = first_entry["candidates"][0]
    token = first_candidate["series_directory_handoff_token"]
    require(token in probe.SERIES_RUN_EPHEMERAL_CANDIDATES, first_candidate)
    hydrated, hydration_ok = probe.hydrate_series_handoff_candidate(
        first_candidate,
        review_id=first_entry["review_id"],
    )
    require(hydration_ok, first_candidate)
    require(hydrated["filename"].lower().endswith(".cbr"), hydrated)
    require(not hydrated.get("folder") and not hydrated.get("folder_grab"), hydrated)
    require(probe.should_skip_cache(first_entry, cooldown_hours=0.75), first_entry)

    before = {key: len(value.get("candidates") or []) for key, value in cache.items()}
    with mock.patch.object(probe, "bad_candidate_match", side_effect=lambda review_id, _candidate: review_id == "descender-007"):
        replay = probe.apply_series_directory_opportunities([], items, cache, observations=observations)
    require(replay["selected_issue_count"] == 8, replay)
    require(before == {key: len(value.get("candidates") or []) for key, value in cache.items()}, "replay duplicated candidates")

    # Repeated calls in one probe run share issue/byte caps. A later broader
    # observation may replace a choice, but cannot add a third transfer.
    probe.SERIES_RUN_MAX_ISSUES = 2
    probe.SERIES_RUN_MAX_BYTES = 128 * MIB
    ledger = {"selected_by_review": {}, "selected_bytes": 0}
    cumulative_cache = {}
    partial_observations, _ = probe.slskd_series_directory_observations([busy_partial], max_files=16, items=items)
    first = probe.apply_series_directory_opportunities(
        [], items, cumulative_cache, observations=partial_observations, selection_budget=ledger,
    )
    second = probe.apply_series_directory_opportunities(
        [], items, cumulative_cache, observations=observations, selection_budget=ledger,
    )
    third = probe.apply_series_directory_opportunities(
        [], items, cumulative_cache, observations=observations, selection_budget=ledger,
    )
    require(first["selected_issue_count"] == 2, first)
    require(second["selected_issue_count"] == 2 and third["selected_issue_count"] == 2, (second, third))
    require(second["selected_bytes"] == 128 * MIB and third["selected_bytes"] == 128 * MIB, (second, third))
    require(len(ledger["selected_by_review"]) == 2 and len(cumulative_cache) == 2, (ledger, cumulative_cache))
finally:
    probe.SERIES_RUN_MAX_ISSUES = original_issue_cap
    probe.SERIES_RUN_MAX_BYTES = original_byte_cap
    probe.SERIES_RUN_EPHEMERAL_CANDIDATES.clear()

print("inkdrop SLSKD coverage selection smoke: PASS")
