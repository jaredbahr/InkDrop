#!/usr/bin/env python3
"""Deterministic coverage for bounded SLSKD no-result retry rotation."""

import math
from unittest import mock

import inkdrop_candidate_matching as matching
import inkdrop_slskd_source_probe as probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


# Broad directory discovery is the anchor so one response can cover sibling
# issues. Provider-native comics/manga vocabulary is second; exact unit
# variants remain available when the broad searches do not settle the row.
descender = {"series": "Descender", "issue": "15", "year": "2016"}
queries = probe.source_queries(descender)
require(queries[:4] == ["Descender", "Descender comics", "Descender 15", "Descender manga"], queries[:8])
initial = probe.rotated_query_batch(queries, max_queries=2, offset=0)
require(initial == ["Descender", "Descender comics"], initial)

fairy_tail = {"series": "Fairy Tail", "issue": "25", "media_type": "manga"}
require(probe.source_queries(fairy_tail)[:2] == ["Fairy Tail", "Fairy Tail manga"], probe.source_queries(fairy_tail)[:6])

clean_attempts = [
    {"query": queries[0], "response_count": 0, "candidate_count": 0},
    {"query": queries[1], "response_count": 0, "candidate_count": 0},
]
cache_entry = {
    "status": "searched_no_candidates",
    "query_signature": probe.query_signature(queries),
    "next_query_offset": 0,
    "attempts": clean_attempts,
    "query_rotation_evidence": probe.query_rotation_evidence(queries, clean_attempts),
}
require(probe.retry_rotates_without_anchor(queries, cache_entry), "same-plan no-result retry did not rotate")
retry = probe.rotated_query_batch(queries, max_queries=2, offset=0, include_anchor=False)
require(retry == ["Descender comics", "Descender 15"], retry)
require(len(retry) == 2 and queries[0] not in retry, "retry repaid the failed anchor or changed its cap")

# If the bounded deadline permits only one retry query, that attempted variant
# still advances the cursor.  This is the exact starvation mode seen live.
next_offset = probe.next_query_offset(
    queries,
    start_offset=0,
    attempted_count=1,
    max_queries=2,
    include_anchor=False,
)
require(next_offset == 1, next_offset)
next_retry = probe.rotated_query_batch(queries, max_queries=2, offset=next_offset, include_anchor=False)
require(next_retry == ["Descender 15", "Descender manga"], next_retry)

with (
    mock.patch.object(probe, "seconds_remaining", side_effect=[100, 0]),
    mock.patch.object(probe, "slskd_search", return_value=[]),
    mock.patch.object(probe, "detected_staged_files", return_value=[]),
):
    deadline_entry = probe.probe_item(
        descender,
        max_queries=2,
        query_offset=0,
        include_anchor=False,
        deadline=123,
    )
require(deadline_entry["query_anchor_included"] is False, deadline_entry)
require(deadline_entry["queries"][0]["query"] == "Descender comics", deadline_entry["queries"])
require(deadline_entry["queries"][1].get("skipped") == "probe_budget_exhausted", deadline_entry["queries"])
require(deadline_entry["next_query_offset"] == 1, deadline_entry)
# A budget skip records a query that was never sent.  It cannot contradict the
# clean zero from the query that was sent, so the executed variant still counts
# and the next pass keeps rotating instead of repaying the anchor.
require(
    deadline_entry["query_rotation_evidence"]["all_attempts_completed_clean_zero"] is True,
    deadline_entry["query_rotation_evidence"],
)
require(deadline_entry["query_rotation_evidence"]["completed_attempt_count"] == 1, deadline_entry)
require(
    probe.retry_rotates_without_anchor(queries, deadline_entry),
    "partial deadline evidence repaid the anchor and stalled the rotation",
)

# Anchor-slot starvation regression.  When the provider budget only affords one
# call, the anchor takes it, the rest of the plan is skipped, and the cursor
# cannot advance.  The next pass must then spend that single call on a fresh
# variant; otherwise the anchor is the only query this row ever searches.
with (
    mock.patch.object(probe, "seconds_remaining", side_effect=[100, 0]),
    mock.patch.object(probe, "slskd_search", return_value=[]),
    mock.patch.object(probe, "detected_staged_files", return_value=[]),
):
    anchor_only_entry = probe.probe_item(
        descender,
        max_queries=5,
        query_offset=0,
        include_anchor=True,
        deadline=123,
    )
require(anchor_only_entry["queries"][0]["query"] == queries[0], anchor_only_entry["queries"])
require(anchor_only_entry["queries"][1].get("skipped") == "probe_budget_exhausted", anchor_only_entry["queries"])
require(anchor_only_entry["next_query_offset"] == 0, anchor_only_entry)
require(
    probe.retry_rotates_without_anchor(queries, anchor_only_entry),
    "an anchor-only budget-truncated probe re-ran the same anchor forever",
)
starved_next = probe.rotated_query_batch(
    queries,
    max_queries=5,
    offset=anchor_only_entry["next_query_offset"],
    include_anchor=False,
)
require(starved_next[0] == queries[1] and queries[0] not in starved_next, starved_next)

# A truncated probe whose executed query failed proves nothing and still fails
# closed: the anchor stays for the next pass.
with (
    mock.patch.object(probe, "seconds_remaining", side_effect=[100, 0]),
    mock.patch.object(probe, "slskd_search", side_effect=TimeoutError("bounded timeout")),
    mock.patch.object(probe, "detected_staged_files", return_value=[]),
):
    failed_deadline_entry = probe.probe_item(
        descender,
        max_queries=5,
        query_offset=0,
        include_anchor=True,
        deadline=123,
    )
require(failed_deadline_entry["queries"][0].get("error"), failed_deadline_entry["queries"])
require(
    failed_deadline_entry["query_rotation_evidence"]["all_attempts_completed_clean_zero"] is False,
    failed_deadline_entry["query_rotation_evidence"],
)
require(
    not probe.retry_rotates_without_anchor(queries, failed_deadline_entry),
    "a failed truncated probe omitted the anchor",
)

# An attempt list that is nothing but budget skips is not evidence of anything.
skip_only_evidence = probe.query_rotation_evidence(
    queries,
    [{"query": queries[0], "skipped": "probe_budget_exhausted", "remaining_seconds": 0.0}],
)
require(skip_only_evidence["all_attempts_completed_clean_zero"] is False, skip_only_evidence)
require(skip_only_evidence["completed_attempt_count"] == 0, skip_only_evidence)

# Direct P1 adversarials: NO_QUERY, legacy/malformed evidence, and a mixed
# timeout-plus-clean-zero probe must all fail closed and retain the anchor.
no_query = {**cache_entry, "status": "no_query"}
require(not probe.retry_rotates_without_anchor(queries, no_query), "NO_QUERY omitted the anchor")
legacy = {key: value for key, value in cache_entry.items() if key != "query_rotation_evidence"}
require(not probe.retry_rotates_without_anchor(queries, legacy), "legacy cache omitted the anchor")
malformed = {**cache_entry, "query_rotation_evidence": {"version": 1, "all_attempts_completed_clean_zero": True}}
require(not probe.retry_rotates_without_anchor(queries, malformed), "malformed evidence omitted the anchor")

# Marker scalar types are strict.  bool is an int subclass in Python, so use
# exact type checks rather than coercion or isinstance.
for field, malformed_value in (
    ("version", "1"),
    ("version", True),
    ("completed_attempt_count", "2"),
    ("completed_attempt_count", True),
):
    malformed_marker = {
        **cache_entry["query_rotation_evidence"],
        field: malformed_value,
    }
    malformed_entry = {**cache_entry, "query_rotation_evidence": malformed_marker}
    require(
        not probe.retry_rotates_without_anchor(queries, malformed_entry),
        (field, malformed_value),
    )

contradictory_attempt = [{
    "query": queries[0],
    "response_count": 0,
    "candidate_count": 0,
    "error": "TimeoutError: bounded timeout",
}]
contradictory_marker = {
    **cache_entry["query_rotation_evidence"],
    "completed_attempt_count": 1,
}
contradictory_entry = {
    **cache_entry,
    "attempts": contradictory_attempt,
    "query_rotation_evidence": contradictory_marker,
}
require(
    not probe.retry_rotates_without_anchor(queries, contradictory_entry),
    "clean marker overrode a contradictory timeout attempt",
)

count_mismatch = {
    **cache_entry,
    "query_rotation_evidence": {
        **cache_entry["query_rotation_evidence"],
        "completed_attempt_count": 1,
    },
}
require(not probe.retry_rotates_without_anchor(queries, count_mismatch), "count mismatch omitted anchor")
missing_attempts = {key: value for key, value in cache_entry.items() if key != "attempts"}
require(not probe.retry_rotates_without_anchor(queries, missing_attempts), "missing attempts omitted anchor")
require(
    not probe.retry_rotates_without_anchor(queries, {**cache_entry, "attempts": {"query": queries[0]}}),
    "non-list attempts omitted anchor",
)
extra_attempt = {
    **cache_entry,
    "attempts": [
        *clean_attempts,
        {"query": queries[2], "response_count": 0, "candidate_count": 0},
    ],
}
require(not probe.retry_rotates_without_anchor(queries, extra_attempt), "extra attempt omitted anchor")
for unsafe_status in ("provider_timeout", "provider_unavailable", "cancelled", "partial_timeout"):
    unsafe_evidence = probe.query_rotation_evidence(
        queries,
        [{"status": unsafe_status, "response_count": 0, "candidate_count": 0}],
    )
    unsafe_entry = {
        **cache_entry,
        "query_rotation_evidence": unsafe_evidence,
    }
    require(
        unsafe_evidence["all_attempts_completed_clean_zero"] is False
        and not probe.retry_rotates_without_anchor(queries, unsafe_entry),
        (unsafe_status, unsafe_evidence),
    )

with (
    mock.patch.object(probe, "slskd_search", side_effect=[TimeoutError("bounded timeout"), []]),
    mock.patch.object(probe, "detected_staged_files", return_value=[]),
):
    mixed_entry = probe.probe_item(descender, max_queries=2)
require(mixed_entry["status"] == "searched_no_candidates", mixed_entry)
require(mixed_entry["queries"][0].get("error"), mixed_entry["queries"])
require(mixed_entry["queries"][1].get("candidate_count") == 0, mixed_entry["queries"])
require(
    mixed_entry["query_rotation_evidence"]["all_attempts_completed_clean_zero"] is False,
    mixed_entry["query_rotation_evidence"],
)
require(
    not probe.retry_rotates_without_anchor(queries, mixed_entry),
    "mixed timeout plus zero omitted the anchor",
)

with (
    mock.patch.object(probe, "slskd_search", side_effect=[[], []]),
    mock.patch.object(probe, "detected_staged_files", return_value=[]),
):
    clean_entry = probe.probe_item(descender, max_queries=2)
require(clean_entry["status"] == "searched_no_candidates", clean_entry)
require(clean_entry["query_rotation_evidence"]["all_attempts_completed_clean_zero"] is True, clean_entry)
require(clean_entry["query_rotation_evidence"]["completed_attempt_count"] == 2, clean_entry)
require(
    probe.retry_rotates_without_anchor(queries, clean_entry),
    "all-clean-zero evidence did not rotate",
)

# Once a broad query has produced a safe exact-unit candidate, do not spend a
# second provider call. Durable known-bad annotation must run before that stop;
# a blocked or wrong-unit result continues to the next query.
safe_candidate = {
    "filename": "Descender 015.cbz",
    "auto_grab": {"verdict": "auto_grab_safe", "autopick_eligible": True},
}
with (
    mock.patch.object(probe, "slskd_search", side_effect=[[{"id": "broad"}], [{"id": "narrow"}]]) as search,
    mock.patch.object(probe, "candidates_from_responses", side_effect=[([safe_candidate], {}), ([], {})]),
    mock.patch.object(probe, "merge_query_candidates", side_effect=lambda groups, _item: [row for group in groups for row in group]),
    mock.patch.object(probe, "annotate_bad_candidate_verdicts", side_effect=lambda rows, _review_id: rows),
    mock.patch.object(probe, "detected_staged_files", return_value=[]),
):
    settled_entry = probe.probe_item(descender, max_queries=2)
require(search.call_count == 1, search.call_args_list)
require(settled_entry["attempts"][0].get("search_stop_reason") == "safe_exact_candidate_found", settled_entry)

blocked_candidate = {
    **safe_candidate,
    "auto_grab": {"verdict": "blocked", "autopick_eligible": False, "reasons": ["known bad candidate"]},
}
blocked_annotation_calls = 0


def annotate_blocked_first_query(rows, _review_id):
    global blocked_annotation_calls
    blocked_annotation_calls += 1
    if blocked_annotation_calls == 1:
        return [blocked_candidate]
    return rows


with (
    mock.patch.object(probe, "slskd_search", side_effect=[[{"id": "broad"}], [{"id": "qualified"}]]) as search,
    mock.patch.object(probe, "candidates_from_responses", side_effect=[([safe_candidate], {}), ([], {})]),
    mock.patch.object(probe, "annotate_bad_candidate_verdicts", side_effect=annotate_blocked_first_query),
    mock.patch.object(probe, "detected_staged_files", return_value=[]),
):
    blocked_entry = probe.probe_item(descender, max_queries=2)
require(search.call_count == 2, search.call_args_list)
require(not blocked_entry["attempts"][0].get("search_stop_reason"), blocked_entry)

review_candidate = {
    "filename": "Descender collection.cbz",
    "auto_grab": {"verdict": "needs_review", "autopick_eligible": False},
}
demoted_candidate = {
    **safe_candidate,
    "auto_grab": {"verdict": "needs_review", "autopick_eligible": False, "reasons": ["competing candidate gap"]},
}


def cumulative_rerank(groups, _item):
    if len(groups) == 1:
        return [review_candidate]
    return [review_candidate, demoted_candidate]


with (
    mock.patch.object(probe, "slskd_search", side_effect=[[{"id": "broad"}], [{"id": "qualified"}], []]) as search,
    mock.patch.object(
        probe,
        "candidates_from_responses",
        side_effect=[([review_candidate], {}), ([safe_candidate], {}), ([], {})],
    ),
    mock.patch.object(probe, "merge_query_candidates", side_effect=cumulative_rerank),
    mock.patch.object(probe, "annotate_bad_candidate_verdicts", side_effect=lambda rows, _review_id: rows),
    mock.patch.object(probe, "detected_staged_files", return_value=[]),
):
    reranked_entry = probe.probe_item(descender, max_queries=3)
require(search.call_count == 3, search.call_args_list)
require(not any(attempt.get("search_stop_reason") for attempt in reranked_entry["attempts"]), reranked_entry)

# Omitting the anchor cannot wrap and duplicate a provider call when a caller
# grants a cap at or above the full plan size.
full_retry = probe.rotated_query_batch(
    queries,
    max_queries=len(queries),
    offset=0,
    include_anchor=False,
)
require(len(full_retry) == len(queries) - 1, len(full_retry))
require(len(full_retry) == len(set(full_retry)), "retry batch duplicated a provider query")

# Performance bound: after the initial probe, all remaining variants are
# covered two per retry instead of one per retry, without increasing calls.
remaining = queries[1:]
offset = 0
covered = []
retry_cycles = math.ceil(len(remaining) / 2)
for _ in range(retry_cycles):
    batch = probe.rotated_query_batch(queries, max_queries=2, offset=offset, include_anchor=False)
    require(len(batch) <= 2 and queries[0] not in batch, batch)
    covered.extend(batch)
    offset = probe.next_query_offset(
        queries,
        start_offset=offset,
        attempted_count=len(batch),
        max_queries=2,
        include_anchor=False,
    )
require(set(remaining).issubset(set(covered)), "retry rotation failed to cover the bounded plan")
require(1 + retry_cycles < len(queries), "retry rotation did not reduce full-plan cycles")

# Errors, changed plans, forced probes, and successful caches retain the exact
# anchor behavior; only a same-plan completed no-result retry may omit it.
for row, refresh_reason, force in (
    ({**cache_entry, "status": "available"}, "", False),
    ({**cache_entry, "status": "error"}, "", False),
    (cache_entry, "query_plan_changed", False),
    (cache_entry, "", True),
    ({**cache_entry, "query_signature": "stale"}, "", False),
):
    require(
        not probe.retry_rotates_without_anchor(queries, row, refresh_reason, force=force),
        (row, refresh_reason, force),
    )

# Query scheduling never relaxes candidate identity.  Proven singletons remain
# exact-title only; packs, ranges, and false titles stay outside compatibility.
singleton = {
    "series_title": "The Last Signal",
    "series": "The Last Signal",
    "title": "The Last Signal",
    "issue_number": "1",
    "unit_type": "issue",
    "media_type": "comic",
    "year": 1988,
    "publisher": "Example House",
    "canonical_issue_count": 1,
    "metadata_issue_count": 1,
    "singleton_issue_proof": True,
    "singleton_issue_proof_source": "comicvine_authoritative_count_and_canonical_issue_identity",
    "singleton_metadata_trusted": True,
    "singleton_metadata_fresh": True,
    "singleton_issue_metadata_trusted": True,
}
exact = matching.candidate_compatibility({"title": "The Last Signal (1988) (Digital)"}, singleton)
require(exact["status"] == "compatible", exact)
for title in (
    "The Last Signal Pack",
    "The Last Signal Complete Collection",
    "The Last Signal issues #1-2",
    "The Last Signals (1988)",
    "Last Signal from Earth (1988)",
):
    verdict = matching.candidate_compatibility({"title": title}, singleton)
    require(verdict["status"] != "compatible", (title, verdict))
    require("singleton_exact_title" not in verdict.get("positive_evidence", []), (title, verdict))

print("inkdrop SLSKD query rotation smoke: PASS")
