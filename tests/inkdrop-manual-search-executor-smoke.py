#!/usr/bin/env python3
"""QA83 verdict-authority regression for the Manual Search executor bridge."""

from __future__ import annotations

import json
import time
from unittest import mock

import inkdrop_manual_search_executor as executor
import inkdrop_source_worker_adapters as adapters
import inkdrop_source_worker_jobs as jobs
import inkdrop_source_worker_runtime as runtime


PLAN = {
    "candidate_parser": "prowlarr_candidates_from_results",
    "verdict_helper": "indexer_candidate_verdict",
    "attempt_seed_helper": "indexer_candidate_attempt_seed",
}
ROW = {
    "provider_id": "prowlarr",
    "provider_type": "indexer",
    "registry_state": "ready",
    "source_mode": "auto",
    "auto_search_allowed": True,
    "auto_download_allowed": True,
    "policy": {"minimum_seeders": 0},
}
TARGET = {
    "series_title": "Amulet",
    "query": "Amulet Volume 9",
    "unit_type": "volume",
    "issue_number": "9",
    "volume_number": "9",
}


def require(value, message):
    if not value:
        raise AssertionError(message)


def qa83_candidate(title, identity):
    payload = {
        "results": [{
            "title": title,
            "protocol": "torrent",
            "guid": identity,
            "downloadUrl": f"https://provider.invalid/{identity}",
            "seeders": 3,
            "_inkdrop_query_variant": "Amulet Volume 9",
            "_inkdrop_query_group": "volume",
            "_inkdrop_request_id": "qa83-request-9",
        }]
    }
    candidates, parse = runtime.parse_candidates(PLAN, ROW, payload, wanted_item=TARGET)
    require(parse["ok"] and len(candidates) == 1, f"QA83 parser fixture failed: {parse}")
    candidate = candidates[0]
    verdict = runtime.verdict_for_candidate(PLAN, ROW, candidate, wanted_item=TARGET)
    attempt = runtime.attempt_seed_for_verdict(PLAN, ROW, verdict)
    return candidate, verdict, attempt


def bridged(raw, verdict, attempt):
    result = executor._candidate_rows({
        "provider_id": "prowlarr",
        "runtime_results": [{
            "query": "Amulet Volume 9",
            "candidates": [raw],
            "verdicts": [verdict],
            "attempts": [attempt],
        }],
    })
    require(len(result) == 1, "executor must return one QA83-shaped candidate")
    return result[0]


def main():
    wanted = executor._wanted_context(
        {"canonical_work_title": "Absolute Batman: The Court of Owls", "unit_number": "1"},
        [{"query": "Absolute Batman Court of Owls 1"}],
    )
    require(wanted["manual_search"] is True, "executor must identify interactive discovery without weakening automated policy")
    rss_row = {"provider_id": "rss", "display_name": "RSS", "policy": {"rss_fresh_release_max_age_days": 30}}
    old_release = {"release_date": "2020-01-01"}
    require(jobs._rss_fresh_release_scope_block_reason(rss_row, old_release), "automated RSS acquisition must retain its old-release gate")
    require(not jobs._rss_fresh_release_scope_block_reason(rss_row, {**old_release, "manual_search": True}), "interactive search may inspect old RSS evidence")

    plan = {"adapter_family": "prowlarr_indexer"}
    root_request = adapters.prowlarr_search_request({"base_url": "http://prowlarr:9696"}, plan, wanted)
    api_request = adapters.prowlarr_search_request({"base_url": "http://prowlarr:9696/api/v1/"}, plan, wanted)
    require(root_request["url"] == api_request["url"] == "http://prowlarr:9696/api/v1/search", "Prowlarr root URL must accept host root or /api/v1 without duplicating the API path")

    safe_raw, safe_verdict, safe_attempt = qa83_candidate("Amulet v09.cbz", "amulet-v09")
    safe_raw.update({
        "candidate_safe": False,
        "artifact_safe": False,
        "auto_grab_verdict": "blocked",
        "quality_status": "rejected",
        "candidate_family_identity": "malicious-raw-family",
        "target_compatibility": {"status": "blocked", "rejection_codes": ["raw_shadow"]},
        "query_provenance": {"request_id": "malicious-raw-request"},
    })
    safe = bridged(safe_raw, safe_verdict, safe_attempt)
    require(safe["candidate_safe"] is True and safe["auto_grab_verdict"] == "auto_grab_safe", "authoritative safe verdict must make a raw-false candidate grabbable")
    require(safe["target_compatibility"]["status"] == "compatible", "authoritative compatibility must replace raw matching state")
    require(safe["candidate_family_identity"] == safe_verdict["candidate_family_identity"], "authoritative QA83 identity must replace raw identity")
    require(safe["query_provenance"]["request_id"] == "qa83-request-9", "authoritative request provenance must replace raw provenance")

    wrong_raw, wrong_verdict, wrong_attempt = qa83_candidate("Amulet Volume 2.cbz", "amulet-v02")
    wrong_raw.update({"candidate_safe": True, "artifact_safe": True, "auto_grab_verdict": "auto_grab_safe", "block_reasons": []})
    wrong = bridged(wrong_raw, wrong_verdict, wrong_attempt)
    require(not wrong["candidate_safe"] and wrong["auto_grab_verdict"] == "blocked", "wrong-volume raw safety keys must not weaken the verdict")
    require("wrong_volume_number" in wrong["block_reasons"], "wrong-volume rejection evidence must survive executor projection")

    blocked_raw, blocked_verdict, blocked_attempt = qa83_candidate("Amulet v09 Preview.cbz", "amulet-v09-preview")
    require("preview_or_sample" in blocked_verdict["block_reasons"] and blocked_verdict["artifact_safe"] is False, "fixture must be blocked by QA83 artifact/relevance evaluation")
    malicious = dict(blocked_raw)
    malicious.update({"candidate_safe": True, "artifact_safe": True, "auto_grab_verdict": "auto_grab_safe", "quality_status": "accepted", "block_reasons": [], "review_reasons": []})
    blocked = bridged(malicious, blocked_verdict, blocked_attempt)
    require(not blocked["candidate_safe"] and not blocked["artifact_safe"], "artifact/relevance blocked verdict must remain non-grabbable")
    require("preview_or_sample" in blocked["block_reasons"], "malicious raw block keys must not erase stricter verdict reasons")

    fixture_rows = [
        {"provider_id": "prowlarr_comics", "display_name": "Comics lane"},
        {"provider_id": "prowlarr_secondary", "display_name": "Secondary lane"},
    ]
    fixture_jobs = {
        "prowlarr_comics": {
            "provider_id": "prowlarr_comics",
            "adapter_family": "prowlarr_indexer",
            "job_status": "ready",
            "fetch_plan": {
                "adapter_family": "prowlarr_indexer",
                "requests": [{"url": "https://must-not-persist.invalid", "params": {"query": "private title"}}] * 6,
                "query_variants": ["private title"] * 6,
            },
        },
        "prowlarr_secondary": {
            "provider_id": "prowlarr_secondary",
            "adapter_family": "prowlarr_indexer",
            "job_status": "ready",
            "fetch_plan": {
                "adapter_family": "prowlarr_indexer",
                "requests": [{"url": "https://must-not-persist.invalid"}] * 5,
                "query_variants": ["private title"] * 5,
                "categoryless_fallback_requests": [{"url": "https://must-not-persist.invalid"}],
            },
        },
    }
    fixture_results = {
        "prowlarr_comics": {
            "provider_id": "prowlarr_comics",
            "result_status": "searched_no_candidates",
            "reason": "searched_no_candidates",
            "fetch": {
                "requests_made": [{}] * 6,
                "payloads": [{"variant_result_counts": [{"query": "private title", "results": 0}] * 6}],
            },
            "runtime_results": [],
        },
        "prowlarr_secondary": {
            "provider_id": "prowlarr_secondary",
            "result_status": "provider_wait",
            "reason": "http_request_failed",
            "fetch": {"requests_made": [{}], "partial_errors": [{"error": "private upstream detail"}]},
            "runtime_results": [],
        },
    }

    def fixture_job(row, *_args, **_kwargs):
        return fixture_jobs[row["provider_id"]]

    def fixture_result(job, **_kwargs):
        return fixture_results[job["provider_id"]]

    with (
        mock.patch.object(executor, "provider_rows", return_value=fixture_rows),
        mock.patch.object(executor, "_http_client", return_value=lambda *_args, **_kwargs: {}),
        mock.patch.object(executor.inkdrop_source_worker_plan, "source_worker_plan_for_row", return_value={}),
        mock.patch.object(executor.inkdrop_source_worker_jobs, "source_job_for_row", side_effect=fixture_job),
        mock.patch.object(executor.inkdrop_source_worker_jobs, "run_source_job", side_effect=fixture_result),
    ):
        evidence = executor.run_provider("fixture.sqlite3", "prowlarr", {}, [{"query": "private title"}], {})
    require(evidence["completed"] and not evidence["error"], "a healthy zero-result lane must not be poisoned by a failed sibling lane")
    require(evidence["diagnostics"]["provider_rows_succeeded"] == 1, "successful zero-result lane must be counted")
    require(evidence["diagnostics"]["provider_rows_failed"] == 1, "failed sibling lane must remain visible in diagnostics")
    evidence_rows = evidence["diagnostics"]["provider_rows"]
    require(evidence_rows[0]["planned_variant_count"] == 6 and evidence_rows[0]["completed_variant_count"] == 6, "successful zero-result lane must retain bounded query-depth evidence")
    require(evidence_rows[1]["planned_call_count"] == 5 and evidence_rows[1]["completed_call_count"] == 1 and evidence_rows[1]["partial_error_count"] == 1, "failed lane must retain bounded call/error counts")
    require("private title" not in json.dumps(evidence["diagnostics"]) and "must-not-persist" not in json.dumps(evidence["diagnostics"]), "provider diagnostics must not retain query text or request URLs")

    slskd_discovery = {
        "completed": True,
        "error": "",
        "candidates": [],
        "diagnostics": {
            "contract_version": 1,
            "provider_rows_considered": 1,
            "provider_rows": [{"provider_id": "slskd", "adapter_family": "slskd_manual_discovery"}],
        },
    }
    with (
        mock.patch.object(executor, "_run_slskd", return_value=slskd_discovery) as slskd_runner,
        mock.patch.object(executor, "provider_rows", side_effect=AssertionError("logical SLSKD must bypass generic source jobs")),
    ):
        routed = executor.run_provider("fixture.sqlite3", "slskd", {}, [{"query": "private title"}], {})
    require(routed["completed"] and slskd_runner.call_count == 1, "logical SLSKD must route to the dedicated discovery adapter")
    require(routed["diagnostics"]["provider_rows"][0]["adapter_family"] == "slskd_manual_discovery", "SLSKD diagnostics must identify the executable adapter")

    configured_rows = [
        {"provider_id": "prowlarr", "source_kind": "", "implementation_status": "implemented", "policy": {}},
        {
            "provider_id": "prowlarr_torrentleech_comics",
            "source_kind": "prowlarr_indexer",
            "implementation_status": "implemented",
            "registry_state": "disabled",
            "policy": {"indexer_ids": [47]},
        },
        {
            "provider_id": "prowlarr_torrentleech_comics_memory",
            "source_kind": "",
            "implementation_status": "implemented",
            "policy": {"indexer_ids": [47]},
        },
    ]
    with mock.patch.object(
        executor.inkdrop_source_registry,
        "registry_from_db",
        return_value=configured_rows,
    ) as registry:
        discovery_rows = executor.provider_rows("fixture.sqlite3", "prowlarr")
    require(registry.call_args.kwargs["include_disabled"] is True, "manual Prowlarr discovery must retain configured gated child lanes")
    require(
        [row["provider_id"] for row in discovery_rows]
        == ["prowlarr", "prowlarr_torrentleech_comics"],
        "manual Prowlarr discovery must reject non-executable source-memory shadows",
    )
    live_shape_rows = [
        {"provider_id": provider_id, "registry_state": state, "priority": priority, "policy": {"scope_policy": scope}}
        for provider_id, state, priority, scope in [
            ("prowlarr_dognzb_comics", "ready", 10, "western_comic_pack"),
            ("prowlarr_kat_comics", "disabled", 10, "western_comic_pack"),
            ("prowlarr_pirate_bay_comics", "disabled", 10, "western_comic_pack"),
            ("prowlarr_torrentdownload_comics", "disabled", 10, "western_comic_pack"),
            ("prowlarr", "ready", 21, ""),
            ("prowlarr_internet_archive", "disabled", 50, "public_archive_books"),
            ("prowlarr_ebookbay", "disabled", 80, "ebook"),
            ("prowlarr_nyaa", "ready", 80, "manga_metadata_or_manga_publisher"),
            ("prowlarr_tokyo_toshokan_manga", "ready", 80, "manga_metadata_or_manga_publisher"),
            ("prowlarr_torrentleech_comics", "disabled", 80, "western_comic_pack"),
        ]
    ]
    ranked_comic_rows = sorted(
        live_shape_rows,
        key=lambda row: executor._manual_prowlarr_row_rank(row, {"media_type": "comic"}),
    )[:8]
    require(
        "prowlarr_torrentleech_comics" in {row["provider_id"] for row in ranked_comic_rows},
        "a productive western-comics child must not fall outside the bounded Prowlarr lane set",
    )
    manual_requests = adapters.prowlarr_search_requests(
        {
            "provider_id": "prowlarr_torrentleech_comics",
            "base_url": "http://prowlarr:9696/api/v1",
            "policy": {
                "indexer_ids": [47],
                "max_query_variants": 3,
                "weekly_pack_query_limit": 8,
            },
        },
        {},
        {
            "manual_search": True,
            "series_title": "Absolute Batman: The Court of Owls",
            "issue_number": "1",
            "manual_search_queries": [
                "Absolute Batman The Court of Owls 1",
                "Batman The Court of Owls",
                "The Court of Owls",
            ],
        },
        limit=50,
    )
    require(len(manual_requests) == 3, "manual Prowlarr lanes must spend their bound on deliberate title variants")
    require(
        not any(request.get("pack_query") for request in manual_requests),
        "dated weekly-pack probes must not consume an operator-started child-lane deadline",
    )

    isolated_rows = [
        {"provider_id": "prowlarr", "policy": {}},
        {"provider_id": "prowlarr_dognzb_comics", "policy": {"indexer_ids": [15]}},
        {"provider_id": "prowlarr_torrentleech_comics", "policy": {"indexer_ids": [47], "requires_account": True}},
    ]

    isolated_execution_rows = []

    def isolated_job(row, *_args, **_kwargs):
        isolated_execution_rows.append(dict(row))
        return {
            "provider_id": row["provider_id"],
            "adapter_family": "prowlarr_indexer",
            "job_status": "ready",
            "fetch_plan": {"requests": [{}], "query_variants": ["private title"]},
        }

    def isolated_result(job, **_kwargs):
        if job["provider_id"] == "prowlarr":
            time.sleep(0.9)
            return {"result_status": "provider_wait", "reason": "simulated slow sibling", "runtime_results": []}
        time.sleep(0.03)
        accepted = job["provider_id"] == "prowlarr_dognzb_comics"
        return {
            "result_status": "blocked",
            "runtime_results": [{
                "query": "private title",
                "candidates": [{"title": "Batman v01 - The Court of Owls (2012).cbr" if accepted else "Unrelated Batman Noise.cbr"}],
                "verdicts": [{
                    "candidate_identity": "dognzb-court" if accepted else "torrentleech-noise",
                    "status": "accepted" if accepted else "rejected",
                }],
                "attempts": [{}],
            }],
        }

    with (
        mock.patch.object(executor, "provider_rows", return_value=isolated_rows),
        mock.patch.object(executor, "_http_client", return_value=lambda *_args, **_kwargs: {}),
        mock.patch.object(executor.inkdrop_source_worker_plan, "source_worker_plan_for_row", return_value={}),
        mock.patch.object(executor.inkdrop_source_worker_jobs, "source_job_for_row", side_effect=isolated_job),
        mock.patch.object(executor.inkdrop_source_worker_jobs, "run_source_job", side_effect=isolated_result),
    ):
        isolated_started = time.monotonic()
        isolated = executor.run_provider(
            "fixture.sqlite3",
            "prowlarr",
            {"canonical_work_title": "Absolute Batman: The Court of Owls", "unit_number": "1"},
            [{"query": "Batman The Court of Owls"}],
            {"provider_timeout_seconds": 1, "candidate_limit": 50},
        )
        isolated_elapsed = time.monotonic() - isolated_started
    require(isolated_elapsed < 0.85, "a slow Prowlarr sibling must not serialize ahead of a productive child lane")
    require(
        isolated_execution_rows and all(row.get("disable_pack_detail_fetch") is True for row in isolated_execution_rows),
        "manual discovery must not spend its bounded provider deadline fetching pack manifests",
    )
    require(isolated["completed"] is True, "a productive assisted Prowlarr lane must complete the provider group")
    require(
        any(row.get("candidate_identity") == "dognzb-court" for row in isolated["candidates"]),
        "a useful later-indexer result must survive rejected noise from the first ranked lane",
    )
    require(
        any(row.get("candidate_identity") == "torrentleech-noise" for row in isolated["candidates"]),
        "rejected first-lane evidence must remain visible for diagnostics",
    )

    partial_safe_raw, partial_safe_verdict, partial_safe_attempt = qa83_candidate(
        "Amulet v09.cbz",
        "amulet-partial-safe-v09",
    )
    partial_rows = [
        {"provider_id": "prowlarr_a_timeout", "adapter_family": "prowlarr_indexer", "policy": {}},
        {"provider_id": "prowlarr_b_malformed", "adapter_family": "prowlarr_indexer", "policy": {}},
        {"provider_id": "prowlarr_c_late_safe", "adapter_family": "prowlarr_indexer", "policy": {}},
    ]

    def partial_job(row, *_args, **_kwargs):
        return {
            "provider_id": row["provider_id"],
            "adapter_family": "prowlarr_indexer",
            "job_status": "ready",
            "fetch_plan": {"requests": [{}], "query_variants": ["private title"]},
        }

    def partial_child_result(job, **_kwargs):
        child = job["provider_id"]
        if child == "prowlarr_a_timeout":
            raise TimeoutError("simulated child timeout")
        if child == "prowlarr_b_malformed":
            raise ValueError("simulated malformed child response")
        time.sleep(0.04)
        return {
            "provider_id": child,
            "result_status": "sent",
            "runtime_results": [{
                "query": "private title",
                "candidates": [partial_safe_raw],
                "verdicts": [partial_safe_verdict],
                "attempts": [partial_safe_attempt],
            }],
        }

    with (
        mock.patch.object(executor, "provider_rows", return_value=partial_rows),
        mock.patch.object(executor, "_http_client", return_value=lambda *_args, **_kwargs: {}),
        mock.patch.object(executor.inkdrop_source_worker_plan, "source_worker_plan_for_row", return_value=PLAN),
        mock.patch.object(executor.inkdrop_source_worker_jobs, "source_job_for_row", side_effect=partial_job),
        mock.patch.object(executor.inkdrop_source_worker_jobs, "run_source_job", side_effect=partial_child_result),
    ):
        partial_started = time.monotonic()
        partial = executor.run_provider(
            "fixture.sqlite3",
            "prowlarr",
            {"canonical_work_title": "Amulet", "unit_type": "volume", "unit_number": "9", "volume_number": "9"},
            [{"query": "Amulet Volume 9"}],
            {"provider_timeout_seconds": 2, "candidate_limit": 50},
        )
        partial_elapsed = time.monotonic() - partial_started
    require(partial_elapsed < 0.75, "timed-out/malformed children exceeded the bounded provider window")
    require(partial["completed"] and not partial["error"], "successful child truth was relabeled as total provider failure")
    require(
        partial["diagnostics"]["provider_rows_succeeded"] == 1
        and partial["diagnostics"]["provider_rows_failed"] == 2,
        f"provider and child-source outcome truth collapsed: {partial['diagnostics']}",
    )
    require(len(partial["candidates"]) == 1, "failed siblings erased or duplicated the completed candidate")
    partial_candidate = partial["candidates"][0]
    require(
        partial_candidate.get("candidate_safe") is True
        and partial_candidate.get("auto_grab_verdict") == "auto_grab_safe",
        f"partial success bypassed shared normalization/safety scoring: {partial_candidate}",
    )
    require(
        (partial_candidate.get("_inkdrop_manual_attempt") or {}).get("status") == "sent",
        "safe partial child result lost its downloader handoff capsule",
    )
    require(
        [row.get("result_status") for row in partial["diagnostics"]["provider_rows"]]
        == ["provider_wait", "provider_wait", "sent"],
        f"child outcome diagnostics were not preserved independently: {partial['diagnostics']}",
    )

    print("inkdrop manual search executor smoke: PASS")


if __name__ == "__main__":
    main()
