#!/usr/bin/env python3
"""InkDrop recorded a provider as "searched, no candidates" for a queue row
the provider never actually ran against -- the source-worker batch admits a
row, loses the queue claim to another worker, and returns the row in
selected_queue_ids with no run/action/review/provider result at all. The
autopilot per-row projection then fabricated a zero-valued payload from
scheduling metadata (source_worker_schedule_plan_count, or the batch's own
row_count) instead of requiring real execution evidence, and
source_no_row_result_attempt_status() accepted that fabricated payload as a
completed zero-result search. The provider then disappeared from
missing_required_source_result_sources(), so the row lost a real search
opportunity without a request ever being made -- confirmed live for the
MangaDex and RSS source-worker lanes (46 unrecovered false results across 40
active queues / 11 series).

Fix: admission/scheduling metadata (plans considered, rows in this batch)
must never substitute for execution evidence (a targeted run that actually
produced attempted/missing/blocked counts). This is verified at the
attempt-status-classification layer (source_no_row_result_attempt_status)
directly, since that is the exact function the report reproduced against.
"""

from __future__ import annotations

from core import inkdrop_series_autopilot as autopilot


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def claim_skip_shaped_payloads_produce_no_disposition():
    """A row that was admitted/scheduled but never executed (its queue claim
    was already held by another worker) must not manufacture
    searched_no_candidates from scheduling metadata alone."""

    # Generic source-worker branch (matches the report's own reproduction:
    # mode=source_worker, a nonzero schedule plan count, zero real results).
    generic_claim_skip_payload = {
        "mode": "source_worker",
        "ok": True,
        "actions": [],
        "review": [],
        "missing_candidates": 0,
        "attempted_total": 0,
        "blocked_candidate_count": 0,
        "source_worker_schedule_plan_count": 3,
        "provider_wait_count": 0,
        "operator_required_count": 0,
        "malformed_provider_result_count": 0,
        "skipped_provider_count": 0,
    }
    for source in ("rss", "comicscodes"):
        result = autopilot.source_no_row_result_attempt_status(source, generic_claim_skip_payload, row_count=1)
        require(
            result is None,
            f"{source}: a claim-skipped row with only scheduling metadata (no run) was still recorded as {result}",
        )

    # MangaDex branch: no rows_considered evidence, only the batch's row_count.
    mangadex_claim_skip_payload = {
        "ok": True,
        "source": "mangadex",
        "rows_considered": 0,
        "actions": [],
        "review": [],
        "errors": [],
    }
    require(
        autopilot.source_no_row_result_attempt_status("mangadex", mangadex_claim_skip_payload, row_count=1) is None,
        "mangadex: a claim-skipped row was recorded as searched_no_candidates from row_count alone",
    )

    # SLSKD branch: nothing selected/checked, only row_count.
    slskd_claim_skip_payload = {
        "ok": True,
        "selected_count": 0,
        "checked_count": 0,
        "auto_grab": {"rows": []},
    }
    require(
        autopilot.source_no_row_result_attempt_status("slskd", slskd_claim_skip_payload, row_count=1) is None,
        "slskd: an unselected row was recorded as searched_no_candidates from row_count alone",
    )

    # The bottom generic fallback (comicscodes/rss without mode=source_worker
    # shape): no missing_targets evidence, only row_count with no actions/reviews.
    bottom_fallback_payload = {
        "status": "OK",
        "missing_targets": 0,
        "actions": [],
        "feed_status": "",
    }
    require(
        autopilot.source_no_row_result_attempt_status("comicscodes", bottom_fallback_payload, row_count=1) is None,
        "bottom fallback: missing_targets was synthesized from row_count alone for an unexecuted row",
    )


def real_execution_evidence_still_produces_searched_no_candidates():
    """The fix must not silence genuine zero-result searches -- only remove
    scheduling metadata as a substitute for execution evidence."""

    generic_real_run_payload = {
        "mode": "source_worker",
        "ok": True,
        "actions": [],
        "review": [],
        "missing_candidates": 1,
        "attempted_total": 1,
        "blocked_candidate_count": 0,
        "source_worker_schedule_plan_count": 1,
        "provider_wait_count": 0,
        "operator_required_count": 0,
        "malformed_provider_result_count": 0,
        "skipped_provider_count": 0,
    }
    result = autopilot.source_no_row_result_attempt_status("rss", generic_real_run_payload, row_count=1)
    require(
        result is not None and result["status"] == "searched_no_candidates",
        f"a real terminal zero-result run stopped being recorded: {result}",
    )

    mangadex_real_run_payload = {
        "ok": True,
        "source": "mangadex",
        "rows_considered": 1,
        "actions": [],
        "review": [],
        "errors": [],
    }
    result = autopilot.source_no_row_result_attempt_status("mangadex", mangadex_real_run_payload, row_count=1)
    require(
        result is not None and result["status"] == "searched_no_candidates",
        f"mangadex: a real rows_considered result stopped being recorded: {result}",
    )

    slskd_real_run_payload = {"ok": True, "selected_count": 1, "checked_count": 1, "auto_grab": {"rows": []}}
    result = autopilot.source_no_row_result_attempt_status("slskd", slskd_real_run_payload, row_count=1)
    require(
        result is not None and result["status"] == "searched_no_candidates",
        f"slskd: a real selected/checked result stopped being recorded: {result}",
    )

    prowlarr_real_run_payload = {
        "mode": "source_worker", "ok": True, "actions": [], "review": [],
        "missing_candidates": 1, "attempted_total": 1, "operator_required_count": 0,
        "malformed_provider_result_count": 0, "skipped_provider_count": 0, "provider_wait_count": 0,
    }
    result = autopilot.source_no_row_result_attempt_status("prowlarr", prowlarr_real_run_payload, row_count=1)
    require(
        result is not None and result["status"] == "searched_no_candidates",
        f"prowlarr: unrelated to this fix, must remain unchanged: {result}",
    )


def provider_wait_and_error_dispositions_still_take_priority():
    """A real disposition (provider wait, timeout, operator required) must
    still be classified correctly and never get demoted to None."""
    result = autopilot.source_no_row_result_attempt_status(
        "rss", {"mode": "source_worker", "ok": True, "actions": [], "review": [], "provider_wait_count": 1}, row_count=1
    )
    require(result is not None and result["status"] == "provider_wait", result)

    result = autopilot.source_no_row_result_attempt_status(
        "mangadex", {"ok": True, "source": "mangadex", "command_timed_out": True}, row_count=1
    )
    require(result is not None and result["status"] == "timeout", result)


def selected_queue_ids_only_populate_after_real_execution():
    """The batch layer must not let admission alone (before a claim is
    acquired) populate selected_queue_ids/selected_plans -- inspected via the
    module source directly since a full batch run needs a live DB/provider
    stack this smoke test does not stand up."""
    import inspect
    from core import inkdrop_source_worker_batch as batch

    source = inspect.getsource(batch)
    claim_block_start = source.index("if not claim.get(\"acquired\"):")
    eligible_append_index = source.index("if dynamic_runtime_fill:\n                eligible.append(plan)")
    require(
        eligible_append_index > claim_block_start,
        "eligible.append(plan) still runs before the claim-acquisition check -- "
        "a claim-skipped row would be reported as selected/executed again",
    )


def main():
    claim_skip_shaped_payloads_produce_no_disposition()
    real_execution_evidence_still_produces_searched_no_candidates()
    provider_wait_and_error_dispositions_still_take_priority()
    selected_queue_ids_only_populate_after_real_execution()
    print(
        "SOURCE_WORKER_EXECUTION_EVIDENCE_OK: claim-skipped/unexecuted rows across the generic, "
        "MangaDex, SLSKD, and bottom-fallback source-worker branches no longer manufacture "
        "searched_no_candidates from scheduling metadata alone; real execution evidence, "
        "provider-wait, and error/timeout dispositions are all unchanged; selected_queue_ids only "
        "populates after a queue claim actually succeeds."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
