#!/usr/bin/env python3
"""Bounded startup heartbeat and provider-budget regressions."""

from __future__ import annotations

import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import inkdrop_series_autopilot as autopilot
import inkdrop_source_worker_cli as source_worker_cli


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def args(**overrides):
    values = {
        "dry_run": False,
        "annotate_only": False,
        "status_only": False,
        "series": [],
        "source_order": ["prowlarr", "slskd"],
        "source_order_unfiltered": ["prowlarr", "slskd"],
        "provider_source_enabled": {"prowlarr": True, "slskd": True},
        "provider_source_disabled_reasons": {},
        "max_run_seconds": 720,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def main():
    queue = {
        "items": {
            f"queue:{number:04d}": {
                "key": f"queue:{number:04d}",
                "series": f"Series {number // 10}",
                "issue": str(number),
                "state": "queued",
                "present_in_watch": True,
            }
            for number in range(1800)
        }
    }
    original_status_file = autopilot.STATUS_FILE
    original_state = autopilot.inkdrop_state
    original_summary = autopilot.queue_summary
    original_control = autopilot.missing_recovery_control
    summary_calls = 0

    def forbidden_summary(_queue):
        nonlocal summary_calls
        summary_calls += 1
        raise AssertionError("startup heartbeat rebuilt the full queue summary")

    try:
        with tempfile.TemporaryDirectory(prefix="inkdrop-autopilot-budget-") as tmp:
            autopilot.STATUS_FILE = Path(tmp) / "status.json"
            autopilot.inkdrop_state = None
            autopilot.queue_summary = forbidden_summary
            autopilot.missing_recovery_control = lambda: {"paused": False}
            autopilot.write_json(
                autopilot.STATUS_FILE,
                {"summary": {"total": 1800, "series": [{"series": "preserved"}]}, "state": "watching"},
            )
            started = time.monotonic()
            for number in range(8):
                status = autopilot.write_startup_progress(
                    queue,
                    args(),
                    note=f"startup step {number}",
                )
            elapsed = time.monotonic() - started
            require(summary_calls == 0, f"full summary calls: {summary_calls}")
            require(status["summary"]["series"][0]["series"] == "preserved", status)
            require(status["progress_counts"]["total"] == 1800, status["progress_counts"])
            require(status["summary_refreshed"] is False, status)
            require(elapsed < 3, f"eight cheap heartbeats took {elapsed:.3f}s")
    finally:
        autopilot.STATUS_FILE = original_status_file
        autopilot.inkdrop_state = original_state
        autopilot.queue_summary = original_summary
        autopilot.missing_recovery_control = original_control

    timing_started = time.monotonic()
    time.sleep(0.02)
    timing = autopilot.startup_timing_summary(
        timing_started,
        {"load_queue": 0.005, "status_publication": 0.005},
    )
    require("status_publication" in timing["startup_phase_seconds"], timing)
    require(
        abs(
            timing["startup_elapsed_seconds"]
            - timing["startup_accounted_seconds"]
            - timing["startup_unaccounted_seconds"]
        ) < 0.01,
        timing,
    )

    original_save_queue = autopilot.save_queue
    startup_save_calls = []
    try:
        autopilot.save_queue = lambda saved_queue, **kwargs: startup_save_calls.append(
            (saved_queue, kwargs)
        )
        autopilot.save_startup_queue_snapshot({"items": {}})
        require(len(startup_save_calls) == 1, startup_save_calls)
        require(
            startup_save_calls[0][1] == {
                "sync_state": False,
                "ack_deferred": False,
                "merge_disk": False,
                "retire_terminal": False,
            },
            startup_save_calls,
        )
    finally:
        autopilot.save_queue = original_save_queue

    original_due_series = autopilot.due_series
    original_repeated_cooldown = autopilot.repeated_source_retry_should_cooldown
    original_provider_cooldown = autopilot.provider_retry_should_cooldown
    original_deferred_keys = autopilot.deferred_manual_source_queue_keys
    scoped_queue = {
        "items": {
            f"scope:{number}": {
                "key": f"scope:{number}",
                "series": f"Scoped Series {number // 10}",
                "issue": str(number),
                "state": "queued",
            }
            for number in range(40)
        }
    }
    try:
        groups = [
            (
                f"Scoped Series {group}",
                [scoped_queue["items"][f"scope:{group * 10 + offset}"] for offset in range(4)],
            )
            for group in range(4)
        ]
        autopilot.due_series = lambda *_args, **_kwargs: groups
        autopilot.repeated_source_retry_should_cooldown = lambda *_args, **_kwargs: False
        autopilot.provider_retry_should_cooldown = lambda *_args, **_kwargs: False
        autopilot.deferred_manual_source_queue_keys = lambda *_args, **_kwargs: []
        scoped_args = args(max_series=2, max_issues_per_series=2)
        scoped_keys = autopilot.startup_annotation_row_keys(scoped_queue, scoped_args)
        require(
            scoped_keys == ["scope:0", "scope:1", "scope:10", "scope:11"],
            scoped_keys,
        )
        deferred = autopilot.provider_targeted_annotation_deferred(
            scoped_queue,
            scoped_args,
            "startup",
        )
        require(deferred.get("provider_targeted_checks") is True, deferred)
        require(deferred.get("processed") == 0 and deferred.get("total") == 4, deferred)
        require(deferred.get("queue_total") == 40, deferred)

        original_reconciliation_index = autopilot.reconciliation_index
        original_import_status_index = autopilot.import_status_index
        scoped_index_calls = []
        try:
            autopilot.reconciliation_index = lambda scoped, deadline=None: (
                scoped_index_calls.append(("reconciliation", list(scoped["items"]))) or {}
            )
            autopilot.import_status_index = lambda scoped, deadline=None: (
                scoped_index_calls.append(("import", list(scoped["items"]))) or {}
            )
            annotation = autopilot.annotate_states(
                scoped_queue,
                max_seconds=5,
                reason="provider_target",
                row_keys=["scope:0"],
            )
            require(annotation.get("ok") is True and annotation.get("processed") == 1, annotation)
            require(
                scoped_index_calls == [
                    ("reconciliation", ["scope:0"]),
                    ("import", ["scope:0"]),
                ],
                scoped_index_calls,
            )
            require("source_order" not in scoped_queue["items"]["scope:1"], scoped_queue["items"]["scope:1"])
            empty = autopilot.annotate_states(
                scoped_queue,
                max_seconds=5,
                reason="provider_target",
                row_keys=[],
            )
            require(empty.get("skipped") == "empty_scope" and empty.get("queue_total") == 40, empty)
            autopilot.due_series = lambda *_args, **_kwargs: []
            calculated_empty_keys = autopilot.startup_annotation_row_keys(scoped_queue, scoped_args)
            require(calculated_empty_keys == [], calculated_empty_keys)
            calculated_empty = autopilot.annotate_states(
                scoped_queue,
                max_seconds=5,
                reason="run_finish",
                row_keys=calculated_empty_keys,
            )
            require(calculated_empty.get("skipped") == "empty_scope", calculated_empty)
        finally:
            autopilot.reconciliation_index = original_reconciliation_index
            autopilot.import_status_index = original_import_status_index
    finally:
        autopilot.due_series = original_due_series
        autopilot.repeated_source_retry_should_cooldown = original_repeated_cooldown
        autopilot.provider_retry_should_cooldown = original_provider_cooldown
        autopilot.deferred_manual_source_queue_keys = original_deferred_keys

    protected = autopilot.provider_protected_budget_seconds(args())
    require(protected == 360, protected)
    capped = autopilot.startup_maintenance_timeout(args(), 600, time.monotonic(), share=0.6)
    require(1 <= capped <= 216, capped)
    exhausted = autopilot.startup_maintenance_timeout(args(), 600, time.monotonic() - 361, share=0.6)
    require(exhausted == 0, exhausted)
    probe_budget = autopilot.slskd_probe_budget_for_runtime(300, 275, time.time() + 300)
    require(
        probe_budget == 160,
        f"SLSKD probe did not leave the handoff reserve: {probe_budget}",
    )
    require(
        autopilot.slskd_probe_budget_for_runtime(300, 600, None) == 300,
        "unbounded SLSKD runs should preserve the configured probe budget",
    )
    legacy_zero_results = {
        "prowlarr": {"mode": "missing_acquire", "attempted_total": 1, "actions": [], "review": [], "failed": 0},
        "rss": {"source": "rss", "missing_targets": 1, "candidates_found": 0, "actions": [], "failed": 0},
        "comicscodes": {"source": "comicscodes", "missing_targets": 1, "candidates_found": 0, "actions": [], "failed": 0},
    }
    for source, payload in legacy_zero_results.items():
        require(autopilot.provider_payload_outcome(source, payload) == (True, True), (source, payload))
    require(
        autopilot.provider_payload_outcome(
            "prowlarr",
            {"mode": "missing_acquire", "startup_short_circuit": True, "attempted_total": 0, "actions": []},
        ) == (False, False),
        "Prowlarr startup short-circuit counted as a call",
    )
    for source in ("rss", "comicscodes"):
        disabled_payload = {
            "source": source,
            "status": "DISABLED",
            "skipped": 1,
            "skips": [{"reason": "provider_disabled"}],
            "failed": 0,
        }
        require(autopilot.provider_payload_outcome(source, disabled_payload) == (False, False), disabled_payload)
    require(
        autopilot.provider_payload_outcome("mangadex", {"rows_considered": 0, "actions": []}) == (False, False),
        "empty MangaDex pass counted as a provider call",
    )
    require(
        autopilot.provider_payload_outcome("slskd", {"selected_count": 2, "checked_count": 0}) == (False, False),
        "budget/cooldown-only SLSKD selection counted as a provider call",
    )
    require(
        autopilot.provider_payload_outcome(
            "prowlarr",
            {
                "attempted_total": 1,
                "search_budget_exhausted": True,
                "budget_skipped_count": 2,
                "actions": [],
                "review": [],
                "failed": 0,
            },
        ) == (True, True),
        "partial Prowlarr response was hidden by later budget exhaustion",
    )
    provider_timing = autopilot.provider_start_timing(args(), time.monotonic() - 10)
    require(provider_timing["provider_work_started_before_half_runtime"], provider_timing)

    original_hot_retry_processor = autopilot.process_slskd_hot_retries
    deferred_hot_retry_calls = []
    try:
        autopilot.process_slskd_hot_retries = lambda *_args, **kwargs: deferred_hot_retry_calls.append(
            kwargs.get("deadline")
        ) or [{"hot_retry": True}]
        deferred = autopilot.process_deferred_hot_retries(
            {"items": {}},
            args(),
            provider_work_started=False,
            broad_work_available=True,
            deadline=1200.0,
        )
        require(deferred.get("deferred") is True, deferred)
        require(not deferred_hot_retry_calls, deferred_hot_retry_calls)
        after_provider = autopilot.process_deferred_hot_retries(
            {"items": {}},
            args(),
            provider_work_started=True,
            broad_work_available=True,
            deadline=1200.0,
        )
        require(after_provider.get("deferred") is False, after_provider)
        require(after_provider.get("processed") == [{"hot_retry": True}], after_provider)
        no_broad_work = autopilot.process_deferred_hot_retries(
            {"items": {}},
            args(),
            provider_work_started=False,
            broad_work_available=False,
            deadline=1300.0,
        )
        require(no_broad_work.get("deferred") is False, no_broad_work)
        require(deferred_hot_retry_calls == [1200.0, 1300.0], deferred_hot_retry_calls)
    finally:
        autopilot.process_slskd_hot_retries = original_hot_retry_processor

    boundary_setup = 100.0
    before_half = {}
    require(
        autopilot.record_provider_observation(
            before_half,
            args(),
            boundary_setup,
            {
                "phase": "permission",
                "call_id": "before-half",
                "source": "prowlarr",
                "started_monotonic": 459.999,
            },
            call_states={},
        ) is True,
        before_half,
    )
    # Past the boundary the pass records the starvation but still searches: an
    # outright refusal handed the rest of the budget back to the maintenance
    # that caused the delay, and the pass finished having searched nothing.
    at_half = {}
    require(
        autopilot.record_provider_observation(
            at_half,
            args(),
            boundary_setup,
            {
                "phase": "permission",
                "call_id": "at-half",
                "source": "prowlarr",
                "started_monotonic": 460.0,
            },
            call_states={},
        ) is True,
        at_half,
    )
    require(at_half.get("provider_start_deadline_missed") is True, at_half)
    require(at_half.get("maintenance_starved_provider") is True, at_half)
    require(
        autopilot.automatic_search_health({"sync_result": at_half})["state"]
        == "provider_start_deadline_missed",
        at_half,
    )
    late_invocations = []
    late_boundary_state = {}
    late_boundary_calls = {}

    def observe_at_boundary(event):
        event = dict(event)
        event["started_monotonic"] = 460.0
        return autopilot.record_provider_observation(
            late_boundary_state, args(), boundary_setup, event, call_states=late_boundary_calls
        )

    boundary_payload = autopilot.run_observed_provider_call(
        "rss",
        "Boundary Series",
        observe_at_boundary,
        lambda: (late_invocations.append("called"), {"ok": True})[1],
    )
    require(late_invocations == ["called"], boundary_payload)
    require(boundary_payload.get("ok") is True, boundary_payload)
    require(late_boundary_state.get("provider_call_count") == 1, late_boundary_state)
    require(late_boundary_state.get("provider_work_healthy") is True, late_boundary_state)
    # A pass that recorded the miss but then searched anyway must not keep
    # reporting "no source search started".
    require(
        autopilot.automatic_search_health({"sync_result": late_boundary_state})["state"]
        == "late_provider_start",
        late_boundary_state,
    )
    late_state = {}
    late_calls = {}
    for event in (
        {"phase": "start", "call_id": "late", "source": "rss", "started_monotonic": 461.0},
        {
            "phase": "finish",
            "call_id": "late",
            "source": "rss",
            "started_monotonic": 461.0,
            "healthy": True,
            "failed": False,
        },
    ):
        autopilot.record_provider_observation(
            late_state, args(), boundary_setup, event, call_states=late_calls
        )
    require(late_state.get("provider_work_healthy") is True, late_state)
    require(
        autopilot.automatic_search_health({"sync_result": late_state})["state"] == "late_provider_start",
        late_state,
    )

    require(
        autopilot.automatic_search_health({})["state"] == "waiting_for_provider",
        "provider health defaulted to healthy without a provider call",
    )

    observations = []
    empty_queue = {"items": {}}
    hot_args = args(skip_slskd=False, slskd_max_total=1, slskd_max_queries=1, slskd_probe_budget_seconds=30)
    require(
        autopilot.process_slskd_hot_retries(
            empty_queue,
            hot_args,
            provider_observer=observations.append,
        ) == [],
        "empty hot retry pass returned work",
    )
    require(not observations, f"empty hot retry pass reported provider work: {observations}")

    original_lock = autopilot.held_source_worker_lock
    original_cli = autopilot.inkdrop_source_worker_cli

    @contextmanager
    def unavailable_lock(*_args, **_kwargs):
        yield False

    @contextmanager
    def available_lock(*_args, **_kwargs):
        yield True

    try:
        autopilot.held_source_worker_lock = unavailable_lock
        autopilot.inkdrop_source_worker_cli = SimpleNamespace(
            run_source_worker_cli=lambda _argv, **_kwargs: {
                "source_worker_cli_contract_version": 1,
                "ok": True,
                "mode": "source_worker",
                "batch": {"ok": True, "budget_skipped_queue_ids": [], "runs": []},
            }
        )
        busy = autopilot.run_source_worker_cli_locked(
            ["state.db"],
            source="prowlarr",
            series="Busy Series",
            missing_candidates=1,
            provider_observer=observations.append,
        )
        require(busy.get("skipped_busy"), busy)
        require(not observations, f"busy provider reported a call: {observations}")

        autopilot.held_source_worker_lock = available_lock

        def emit_provider_call(provider_observer, call_id, healthy):
            started = time.monotonic()
            allowed = provider_observer(
                {"phase": "permission", "call_id": call_id, "started_monotonic": started, "healthy": None}
            )
            require(allowed is not False, call_id)
            provider_observer(
                {"phase": "start", "call_id": call_id, "started_monotonic": started, "healthy": None}
            )
            provider_observer(
                {
                    "phase": "finish",
                    "call_id": call_id,
                    "started_monotonic": started,
                    "healthy": healthy,
                    "failed": not healthy,
                    "error": "" if healthy else "provider failed",
                }
            )

        def failed_provider(_argv, provider_observer=None, **_kwargs):
            emit_provider_call(provider_observer, "failed-call", False)
            return {
                "source_worker_cli_contract_version": 1,
                "ok": False,
                "mode": "source_worker",
                "batch": {
                    "ok": False,
                    "budget_skipped_queue_ids": [],
                    "runs": [{"queue_id": "queue-1", "provider_ids": ["prowlarr"], "ok": False}],
                },
            }

        autopilot.inkdrop_source_worker_cli = SimpleNamespace(run_source_worker_cli=failed_provider)
        autopilot.run_source_worker_cli_locked(
            ["state.db"],
            source="prowlarr",
            series="Failed Series",
            missing_candidates=1,
            provider_observer=observations.append,
        )
        failed_events = list(observations)
        observations.clear()
        failed_observation = failed_events[-1]
        require(failed_observation["failed"] is True, failed_observation)
        require(failed_observation["healthy"] is False, failed_observation)
        failed_state = {}
        failed_calls = {}
        for event in failed_events:
            autopilot.record_provider_observation(
                failed_state, args(), time.monotonic() - 5, event, call_states=failed_calls
            )
        require(failed_state.get("provider_work_started") is True, failed_state)
        require(not failed_state.get("provider_work_healthy"), failed_state)
        require(
            autopilot.automatic_search_health({"sync_result": failed_state})["state"]
            == "provider_health_unconfirmed",
            failed_state,
        )

        no_call_payload = {
            "source_worker_cli_contract_version": 1,
            "ok": True,
            "mode": "source_worker",
            "batch": {"ok": True, "budget_skipped_queue_ids": ["queue-1"], "runs": []},
        }
        autopilot.inkdrop_source_worker_cli = SimpleNamespace(
            run_source_worker_cli=lambda _argv, **_kwargs: no_call_payload
        )
        no_call = autopilot.run_source_worker_cli_locked(
            ["state.db"],
            source="prowlarr",
            series="No Call Series",
            missing_candidates=1,
            provider_observer=observations.append,
        )
        require(no_call == no_call_payload, no_call)
        require(not observations, f"empty/budget-skipped source-worker batch reported a call: {observations}")

        adapter_events = ["scheduler_entered"]

        def successful_provider(_argv, provider_observer=None, **_kwargs):
            adapter_events.append("adapter_called")
            emit_provider_call(provider_observer, "successful-call", True)
            return {
                "source_worker_cli_contract_version": 1,
                "ok": True,
                "mode": "source_worker",
                "batch": {
                    "ok": True,
                    "budget_skipped_queue_ids": [],
                    "runs": [{"queue_id": "queue-1", "provider_ids": ["prowlarr"], "ok": True, "result": {"jobs": []}}],
                },
            }

        autopilot.inkdrop_source_worker_cli = SimpleNamespace(run_source_worker_cli=successful_provider)
        setup_started = time.monotonic() - 12
        scheduler_entered = time.monotonic()
        successful = autopilot.run_source_worker_cli_locked(
            ["state.db"],
            source="prowlarr",
            series="Healthy Series",
            missing_candidates=1,
            provider_observer=observations.append,
        )
        require(successful.get("ok") is True, successful)
        successful_events = list(observations)
        observations.clear()
        successful_observation = successful_events[1]
        require(adapter_events == ["scheduler_entered", "adapter_called"], adapter_events)
        require(successful_observation["started_monotonic"] >= scheduler_entered, successful_observation)
        provider_state = {}
        successful_calls = {}
        for event in successful_events:
            autopilot.record_provider_observation(
                provider_state, args(), setup_started, event, call_states=successful_calls
            )
        require(provider_state.get("provider_work_started") is True, provider_state)
        require(provider_state.get("provider_work_healthy") is True, provider_state)
        require(provider_state.get("provider_call_count") == 1, provider_state)
        require(provider_state.get("provider_work_start_elapsed_seconds", 999) < 360, provider_state)

        network_events = []
        network_calls = []

        def zero_result_http(_request):
            return {"json": [], "status_code": 200, "headers": {"Content-Type": "application/json"}}

        observed_http = source_worker_cli._observed_source_http_get(
            zero_result_http, network_events.append, network_calls
        )
        network_started = time.monotonic()
        response = observed_http({"method": "GET", "url": "https://provider.invalid/api/search"})
        require(response["status_code"] == 200, response)
        require([event["phase"] for event in network_events] == ["permission", "start"], network_events)
        require(all(event.get("healthy") is None for event in network_events), network_events)
        require(network_events[1]["started_monotonic"] >= network_started, network_events)
        source_worker_cli._finish_source_http_observations(
            network_events.append,
            network_calls,
            result={
                "ok": True,
                "runs": [{
                    "ok": True,
                    "result": {
                        "ok": True,
                        "job_results": [{
                            "provider_id": "broken",
                            "result_status": "provider_wait",
                            "fetch": {"ok": False, "reason": "malformed_provider_response"},
                        }],
                    },
                }],
            },
        )
        malformed_state = {}
        malformed_calls = {}
        for event in network_events:
            autopilot.record_provider_observation(
                malformed_state, args(), network_started - 1, event, call_states=malformed_calls
            )
        require(malformed_state.get("provider_call_count") == 1, malformed_state)
        require(not malformed_state.get("provider_work_healthy"), malformed_state)

        parsed_events = []
        parsed_calls = []
        parsed_http = source_worker_cli._observed_source_http_get(zero_result_http, parsed_events.append, parsed_calls)
        parsed_http({"method": "GET", "url": "https://provider.invalid/api/search"})
        source_worker_cli._finish_source_http_observations(
            parsed_events.append,
            parsed_calls,
            result={
                "ok": True,
                "runs": [{
                    "ok": True,
                    "result": {
                        "ok": True,
                        "job_results": [{
                            "provider_id": "zero-result",
                            "result_status": "searched_no_candidates",
                            "fetch": {"ok": True, "payloads": []},
                        }],
                    },
                }],
            },
        )
        parsed_state = {}
        parsed_call_states = {}
        for event in parsed_events:
            autopilot.record_provider_observation(
                parsed_state, args(), time.monotonic() - 1, event, call_states=parsed_call_states
            )
        require(parsed_state.get("provider_call_count") == 1, parsed_state)
        require(parsed_state.get("provider_work_healthy") is True, parsed_state)

        mixed_events = []
        mixed_calls = [
            {"call_id": "mixed-bad", "started_monotonic": time.monotonic()},
            {"call_id": "mixed-good", "started_monotonic": time.monotonic()},
        ]
        source_worker_cli._finish_source_http_observations(
            mixed_events.append,
            mixed_calls,
            result={
                "ok": True,
                "runs": [
                    {
                        "ok": True,
                        "result": {
                            "ok": True,
                            "job_results": [{
                                "provider_id": "broken",
                                "result_status": "provider_wait",
                                "fetch": {"ok": False, "reason": "malformed_provider_response"},
                            }],
                        },
                    },
                    {
                        "ok": True,
                        "result": {
                            "ok": True,
                            "job_results": [{
                                "provider_id": "valid",
                                "result_status": "searched_no_candidates",
                                "fetch": {"ok": True, "payloads": []},
                            }],
                        },
                    },
                ],
            },
        )
        require([event.get("healthy") for event in mixed_events] == [False, False], mixed_events)

        denied_network = []

        def denied_source_worker(_argv, provider_observer=None, **_kwargs):
            observed = source_worker_cli._observed_source_http_get(
                lambda _request: denied_network.append("HTTP_INVOKED"),
                provider_observer,
                [],
            )
            observed({"method": "GET", "url": "https://provider.invalid/search"})
            return {"ok": True, "runs": []}

        autopilot.inkdrop_source_worker_cli = SimpleNamespace(
            run_source_worker_cli=denied_source_worker,
            ProviderStartDeadlineMissed=source_worker_cli.ProviderStartDeadlineMissed,
        )
        denied_bridge = autopilot.run_source_worker_cli_locked(
            ["state.db"],
            source="prowlarr",
            series="Late Series",
            missing_candidates=1,
            provider_observer=lambda _event: False,
        )
        require(denied_bridge.get("provider_start_deadline_missed") is True, denied_bridge)
        require(not denied_network, denied_network)

        original_process_with_progress = autopilot.run_process_with_progress
        original_runtime_limited_child_timeout = autopilot.runtime_limited_child_timeout
        try:
            captured_slskd_call = {}

            def capture_slskd_call(cmd, **kwargs):
                captured_slskd_call["cmd"] = list(cmd)
                captured_slskd_call["timeout"] = kwargs.get("timeout")
                return 0, '{"ok": true, "source": "slskd"}', ""

            autopilot.run_process_with_progress = capture_slskd_call
            autopilot.runtime_limited_child_timeout = lambda timeout, _deadline: timeout
            boundary_args = args(
                skip_slskd=False,
                slskd_probe_budget_seconds=300,
                slskd_max_total=1,
                slskd_max_per_series=1,
                slskd_wait_seconds=1,
                slskd_max_queries=1,
                slskd_cooldown_hours=1,
                force_slskd=False,
                slskd_auto_grab_max=1,
                source_lock_wait_seconds=0,
            )
            boundary_timeout = autopilot.slskd_source_timeout_seconds(boundary_args)
            boundary_payload = autopilot.run_slskd(
                "Boundary SLSKD Series",
                boundary_args,
                deadline=time.time() + boundary_timeout + autopilot.RUNTIME_CHILD_CLEANUP_SECONDS,
            )
            require(boundary_payload.get("ok") is True, boundary_payload)
            require(captured_slskd_call.get("timeout") == boundary_timeout, captured_slskd_call)
            budget_index = captured_slskd_call["cmd"].index("--probe-budget-seconds") + 1
            require(
                captured_slskd_call["cmd"][budget_index]
                == str(
                    boundary_timeout
                    - autopilot.RUNTIME_CHILD_CLEANUP_SECONDS
                    - autopilot.SLSKD_HANDOFF_RESERVE_SECONDS
                ),
                captured_slskd_call,
            )

            autopilot.run_process_with_progress = lambda *_args, **_kwargs: (2, "", "provider process failed")
            slskd_args = args(
                skip_slskd=False,
                slskd_probe_budget_seconds=30,
                slskd_max_total=1,
                slskd_max_per_series=1,
                slskd_wait_seconds=1,
                slskd_max_queries=1,
                slskd_cooldown_hours=1,
                force_slskd=False,
                slskd_auto_grab_max=1,
                source_lock_wait_seconds=0,
            )
            try:
                autopilot.run_slskd(
                    "Failed SLSKD Series",
                    slskd_args,
                    provider_observer=observations.append,
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError("nonzero SLSKD provider process did not fail")
            slskd_failure = observations.pop()
            require(slskd_failure["source"] == "slskd", slskd_failure)
            require(slskd_failure["failed"] is True and slskd_failure["healthy"] is False, slskd_failure)
        finally:
            autopilot.run_process_with_progress = original_process_with_progress
            autopilot.runtime_limited_child_timeout = original_runtime_limited_child_timeout
    finally:
        autopilot.held_source_worker_lock = original_lock
        autopilot.inkdrop_source_worker_cli = original_cli

    original_annotate = autopilot.annotate_states
    original_hot_rows = autopilot.slskd_hot_retry_rows
    original_broad_reservation = autopilot.broad_due_runtime_reservation_seconds
    original_source_runtime = autopilot.source_runtime_min_seconds
    original_cached_entry = autopilot.cached_safe_slskd_entry_for_item
    original_hot_candidate = autopilot.slskd_hot_retry_candidate
    original_run_slskd = autopilot.run_slskd
    target_queue = {
        "items": {
            "queue-target": {
                "key": "queue-target",
                "series": "Evidence Series",
                "issue": "1",
                "state": "queued",
            }
        }
    }
    target_rows = [target_queue["items"]["queue-target"]]
    try:
        autopilot.annotate_states = lambda *_args, **_kwargs: {
            "ok": False,
            "processed": 0,
            "total": 1,
            "stage": "import_status_index",
        }
        blocked_evidence = autopilot.ensure_targeted_provider_evidence(
            target_queue,
            target_rows,
            args(annotate_timeout_seconds=5),
        )
        provider_calls = 0
        if blocked_evidence.get("ready"):
            provider_calls += 1
        require(provider_calls == 0, blocked_evidence)

        autopilot.slskd_hot_retry_rows = lambda *_args, **_kwargs: target_rows
        autopilot.broad_due_runtime_reservation_seconds = lambda *_args, **_kwargs: 0
        autopilot.source_runtime_min_seconds = lambda *_args, **_kwargs: 1
        autopilot.cached_safe_slskd_entry_for_item = lambda *_args, **_kwargs: (None, None)
        autopilot.slskd_hot_retry_candidate = lambda item, now=None: (
            item.get("state") not in {"verified", "downloading", "importing", "needs_you"}
        )

        def counted_slskd(*_args, **_kwargs):
            nonlocal_provider_calls[0] += 1
            return {"ok": True, "selected_count": 1, "checked_count": 1}

        nonlocal_provider_calls = [0]
        autopilot.run_slskd = counted_slskd
        blocked_hot = autopilot.process_slskd_hot_retries(
            target_queue,
            args(
                slskd_max_queries=1,
                slskd_probe_budget_seconds=30,
                annotate_timeout_seconds=5,
            ),
        )
        require(nonlocal_provider_calls[0] == 0, blocked_hot)
        require(blocked_hot[0].get("evidence_deferred") is True, blocked_hot)

        autopilot.annotate_states = lambda *_args, **_kwargs: {
            "ok": True,
            "processed": 1,
            "total": 1,
        }
        ready_evidence = autopilot.ensure_targeted_provider_evidence(
            target_queue,
            target_rows,
            args(annotate_timeout_seconds=5),
        )
        if ready_evidence.get("ready"):
            provider_calls += 1
        require(provider_calls == 1, ready_evidence)
        ready_hot = autopilot.process_slskd_hot_retries(
            target_queue,
            args(
                slskd_max_queries=1,
                slskd_probe_budget_seconds=30,
                annotate_timeout_seconds=5,
            ),
        )
        require(nonlocal_provider_calls[0] == 1, ready_hot)

        def needs_you_annotation(*_args, **_kwargs):
            target_rows[0]["state"] = "needs_you"
            return {"ok": True, "processed": 1, "total": 1}

        autopilot.annotate_states = needs_you_annotation
        needs_you_hot = autopilot.process_slskd_hot_retries(
            target_queue,
            args(
                slskd_max_queries=1,
                slskd_probe_budget_seconds=30,
                annotate_timeout_seconds=5,
            ),
        )
        require(nonlocal_provider_calls[0] == 1, needs_you_hot)
        require(needs_you_hot[0].get("evidence_deferred") is True, needs_you_hot)

        ladder_events = []
        target_rows[0]["state"] = "searching"
        target_rows[0]["current_source"] = "prowlarr"
        target_rows[0]["source_order"] = ["prowlarr", "rss"]

        def first_provider_annotation(*_args, **_kwargs):
            ladder_events.append("annotate_prowlarr")
            return {"ok": True, "processed": 1, "total": 1}

        autopilot.annotate_states = first_provider_annotation
        first_invoked, _first_payload, first_evidence = autopilot.invoke_provider_after_targeted_evidence(
            target_queue,
            target_rows,
            args(retry_needs_you=False, annotate_timeout_seconds=5),
            "prowlarr",
            lambda: ladder_events.append("call_prowlarr"),
        )
        require(first_evidence.get("ready") and first_invoked, first_evidence)

        def queued_provider_annotation(*_args, **_kwargs):
            ladder_events.append("annotate_queued")
            target_rows[0]["state"] = "queued"
            target_rows[0]["current_source"] = None
            return {"ok": True, "processed": 1, "total": 1}

        autopilot.annotate_states = queued_provider_annotation
        target_rows[0]["state"] = "searching"
        target_rows[0]["current_source"] = "prowlarr"
        queued_invoked, _queued_payload, queued_evidence = autopilot.invoke_provider_after_targeted_evidence(
            target_queue,
            target_rows,
            args(retry_needs_you=False, annotate_timeout_seconds=5),
            "prowlarr",
            lambda: ladder_events.append("call_queued_prowlarr"),
        )
        require(queued_evidence.get("ready") and queued_invoked, queued_evidence)
        require(queued_evidence.get("rows_rearmed_after_evidence") == 1, queued_evidence)
        require(target_rows[0].get("state") == "searching", target_rows[0])
        require(target_rows[0].get("current_source") == "prowlarr", target_rows[0])

        def unsafe_provider_annotation(*_args, **_kwargs):
            ladder_events.append("annotate_unsafe")
            target_rows[0]["state"] = "needs_you"
            target_rows[0]["current_source"] = None
            target_rows[0]["needs_you_reason"] = "candidate_title_mismatch"
            return {"ok": True, "processed": 1, "total": 1}

        autopilot.annotate_states = unsafe_provider_annotation
        target_rows[0]["state"] = "searching"
        target_rows[0]["current_source"] = "prowlarr"
        unsafe_invoked, _unsafe_payload, unsafe_evidence = autopilot.invoke_provider_after_targeted_evidence(
            target_queue,
            target_rows,
            args(retry_needs_you=False, annotate_timeout_seconds=5),
            "prowlarr",
            lambda: ladder_events.append("call_unsafe_prowlarr"),
        )
        require(unsafe_evidence.get("ready") and not unsafe_invoked, unsafe_evidence)

        blocked_cases = (
            ("other_source", "searching", "rss", None),
            ("blocked", "blocked", None, None),
            ("wrong_unit", "wrong_unit", None, None),
            ("identity_change", "queued", None, "Different Series"),
        )
        original_series = target_rows[0].get("series")
        for case_name, post_state, post_source, changed_series in blocked_cases:
            def blocked_annotation(*_args, **_kwargs):
                ladder_events.append(f"annotate_{case_name}")
                target_rows[0]["state"] = post_state
                target_rows[0]["current_source"] = post_source
                if changed_series:
                    target_rows[0]["series"] = changed_series
                return {"ok": True, "processed": 1, "total": 1}

            autopilot.annotate_states = blocked_annotation
            target_rows[0]["series"] = original_series
            target_rows[0]["state"] = "searching"
            target_rows[0]["current_source"] = "prowlarr"
            case_invoked, _case_payload, case_evidence = autopilot.invoke_provider_after_targeted_evidence(
                target_queue,
                target_rows,
                args(retry_needs_you=False, annotate_timeout_seconds=5),
                "prowlarr",
                lambda: ladder_events.append(f"call_{case_name}"),
            )
            require(case_evidence.get("ready") and not case_invoked, (case_name, case_evidence))
        target_rows[0]["series"] = original_series

        def policy_change_annotation(*_args, **_kwargs):
            ladder_events.append("annotate_policy_change")
            target_rows[0].pop("media_type", None)
            return {"ok": True, "processed": 1, "total": 1}

        autopilot.annotate_states = policy_change_annotation
        target_rows[0]["state"] = "searching"
        target_rows[0]["current_source"] = "mangadex"
        target_rows[0]["source_order"] = ["prowlarr", "rss"]
        target_rows[0]["media_type"] = "manga"
        policy_invoked, _policy_payload, policy_evidence = autopilot.invoke_provider_after_targeted_evidence(
            target_queue,
            target_rows,
            args(retry_needs_you=False, annotate_timeout_seconds=5),
            "mangadex",
            lambda: ladder_events.append("call_policy_change"),
        )
        require(policy_evidence.get("ready") and not policy_invoked, policy_evidence)

        def watch_change_annotation(*_args, **_kwargs):
            ladder_events.append("annotate_watch_change")
            target_rows[0]["state"] = "queued"
            target_rows[0]["current_source"] = None
            target_rows[0]["watch_id"] = "watch-2"
            return {"ok": True, "processed": 1, "total": 1}

        autopilot.annotate_states = watch_change_annotation
        target_rows[0]["state"] = "searching"
        target_rows[0]["current_source"] = "prowlarr"
        target_rows[0]["source_order"] = ["prowlarr", "rss"]
        target_rows[0]["watch_id"] = "watch-1"
        watch_invoked, _watch_payload, watch_evidence = autopilot.invoke_provider_after_targeted_evidence(
            target_queue,
            target_rows,
            args(retry_needs_you=False, annotate_timeout_seconds=5),
            "prowlarr",
            lambda: ladder_events.append("call_watch_change"),
        )
        require(watch_evidence.get("ready") and not watch_invoked, watch_evidence)
        target_rows[0].pop("watch_id", None)
        target_rows[0]["source_order"] = ["prowlarr", "rss"]

        safe_mixed_second = dict(target_rows[0])
        safe_mixed_second.update({"key": "queue-target-safe-2", "issue": "2", "state": "searching", "current_source": "prowlarr"})
        safe_mixed_rows = [target_rows[0], safe_mixed_second]

        def safe_mixed_annotation(*_args, **_kwargs):
            ladder_events.append("annotate_safe_mixed")
            safe_mixed_rows[0]["state"] = "queued"
            safe_mixed_rows[0]["current_source"] = None
            return {"ok": True, "processed": 2, "total": 2}

        autopilot.annotate_states = safe_mixed_annotation
        target_rows[0]["state"] = "searching"
        target_rows[0]["current_source"] = "prowlarr"
        safe_mixed_invoked, _safe_mixed_payload, safe_mixed_evidence = autopilot.invoke_provider_after_targeted_evidence(
            target_queue,
            safe_mixed_rows,
            args(retry_needs_you=False, annotate_timeout_seconds=5),
            "prowlarr",
            lambda: ladder_events.append("call_safe_mixed_prowlarr"),
        )
        require(safe_mixed_evidence.get("ready") and safe_mixed_invoked, safe_mixed_evidence)
        require(safe_mixed_evidence.get("rows_rearmed_after_evidence") == 1, safe_mixed_evidence)

        def display_source_annotation(*_args, **_kwargs):
            ladder_events.append("annotate_display_source")
            target_rows[0]["state"] = "searching"
            target_rows[0]["current_source"] = "Prowlarr"
            return {"ok": True, "processed": 1, "total": 1}

        autopilot.annotate_states = display_source_annotation
        target_rows[0]["state"] = "searching"
        target_rows[0]["current_source"] = "prowlarr"
        display_invoked, _display_payload, display_evidence = autopilot.invoke_provider_after_targeted_evidence(
            target_queue,
            target_rows,
            args(retry_needs_you=False, annotate_timeout_seconds=5),
            "prowlarr",
            lambda: ladder_events.append("call_display_source"),
        )
        require(display_evidence.get("ready") and display_invoked, display_evidence)

        def searching_without_source_annotation(*_args, **_kwargs):
            ladder_events.append("annotate_searching_without_source")
            target_rows[0]["state"] = "searching"
            target_rows[0]["current_source"] = None
            return {"ok": True, "processed": 1, "total": 1}

        autopilot.annotate_states = searching_without_source_annotation
        target_rows[0]["state"] = "searching"
        target_rows[0]["current_source"] = "prowlarr"
        no_source_invoked, _no_source_payload, no_source_evidence = autopilot.invoke_provider_after_targeted_evidence(
            target_queue,
            target_rows,
            args(retry_needs_you=False, annotate_timeout_seconds=5),
            "prowlarr",
            lambda: ladder_events.append("call_searching_without_source"),
        )
        require(no_source_evidence.get("ready") and no_source_invoked, no_source_evidence)
        require(no_source_evidence.get("rows_rearmed_after_evidence") == 1, no_source_evidence)

        mixed_second = dict(target_rows[0])
        mixed_second.update({"key": "queue-target-2", "issue": "2", "state": "searching", "current_source": "prowlarr"})
        mixed_rows = [target_rows[0], mixed_second]

        def mixed_provider_annotation(*_args, **_kwargs):
            ladder_events.append("annotate_mixed")
            mixed_rows[0]["state"] = "queued"
            mixed_rows[0]["current_source"] = None
            mixed_rows[1]["state"] = "verified"
            mixed_rows[1]["current_source"] = None
            return {"ok": True, "processed": 2, "total": 2}

        autopilot.annotate_states = mixed_provider_annotation
        target_rows[0]["state"] = "searching"
        target_rows[0]["current_source"] = "prowlarr"
        mixed_invoked, _mixed_payload, mixed_evidence = autopilot.invoke_provider_after_targeted_evidence(
            target_queue,
            mixed_rows,
            args(retry_needs_you=False, annotate_timeout_seconds=5),
            "prowlarr",
            lambda: ladder_events.append("call_mixed_prowlarr"),
        )
        require(mixed_evidence.get("ready") and not mixed_invoked, mixed_evidence)

        def second_provider_annotation(*_args, **_kwargs):
            ladder_events.append("annotate_rss")
            target_rows[0]["state"] = "verified"
            return {"ok": True, "processed": 1, "total": 1}

        autopilot.annotate_states = second_provider_annotation
        target_rows[0]["state"] = "searching"
        target_rows[0]["current_source"] = "rss"
        second_invoked, _second_payload, second_evidence = autopilot.invoke_provider_after_targeted_evidence(
            target_queue,
            target_rows,
            args(retry_needs_you=False, annotate_timeout_seconds=5),
            "rss",
            lambda: ladder_events.append("call_rss"),
        )
        require(second_evidence.get("ready") and not second_invoked, second_evidence)
        require(
            ladder_events == [
                "annotate_prowlarr",
                "call_prowlarr",
                "annotate_queued",
                "call_queued_prowlarr",
                "annotate_unsafe",
                "annotate_other_source",
                "annotate_blocked",
                "annotate_wrong_unit",
                "annotate_identity_change",
                "annotate_policy_change",
                "annotate_watch_change",
                "annotate_safe_mixed",
                "call_safe_mixed_prowlarr",
                "annotate_display_source",
                "call_display_source",
                "annotate_searching_without_source",
                "call_searching_without_source",
                "annotate_mixed",
                "annotate_rss",
            ],
            ladder_events,
        )
    finally:
        autopilot.annotate_states = original_annotate
        autopilot.slskd_hot_retry_rows = original_hot_rows
        autopilot.broad_due_runtime_reservation_seconds = original_broad_reservation
        autopilot.source_runtime_min_seconds = original_source_runtime
        autopilot.cached_safe_slskd_entry_for_item = original_cached_entry
        autopilot.slskd_hot_retry_candidate = original_hot_candidate
        autopilot.run_slskd = original_run_slskd

    require(
        autopilot.automatic_search_health({"acquisition_worker_available": False})["state"] == "worker_unavailable",
        "unavailable acquisition worker was called healthy",
    )
    require(
        autopilot.automatic_search_health({"source_configuration_missing": True})["state"] == "waiting_for_configuration",
        "missing source configuration was called healthy",
    )
    require(
        autopilot.automatic_search_health({"operator_paused": True})["state"] == "operator_paused",
        "operator pause was called healthy",
    )
    degraded = autopilot.automatic_search_health(
        {"sync_result": {"ok": False, "provider_work_started": True, "provider_work_healthy": True}}
    )
    require(degraded["state"] == "provider_healthy_maintenance_degraded", degraded)
    timed_out = autopilot.automatic_search_health(
        {"sync_result": {"ok": False, "reason": "maintenance_timed_out"}}
    )
    require(timed_out["state"] == "maintenance_timed_out", timed_out)

    print(f"AUTOPILOT_PROVIDER_BUDGET_OK rows=1800 heartbeats=8 elapsed_seconds={elapsed:.3f}")


if __name__ == "__main__":
    main()
