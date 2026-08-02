#!/usr/bin/env python3
"""Docker-free regression for bounded, safety-gated SLSKD Manual Search."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest import mock

import inkdrop_auth_contracts
import inkdrop_manual_search
import inkdrop_manual_search_core as core
import inkdrop_manual_search_executor as executor
import inkdrop_slskd_source_probe as slskd
import inkdrop_state


def require(value, message):
    if not value:
        raise AssertionError(message)


def fixture_candidate(path=r"\\peer-private\Comics\Fullmetal Alchemist Chapter 1.cbz", username="private-peer"):
    return {
        "filename": path,
        "username": username,
        "size": 42_000_000,
        "extension": ".cbz",
        "score": 95,
        "has_free_upload_slot": True,
        "upload_speed": 1000,
        "auto_grab": True,
        "auto_grab_verdict": "auto_grab_safe",
    }


def wrapper_fixtures():
    left = slskd.slskd_candidate_download_url_hash(fixture_candidate(r"\\peer-private\A\same.cbz", "peer-a"))
    right = slskd.slskd_candidate_download_url_hash(fixture_candidate(r"\\peer-private\B\same.cbz", "peer-a"))
    other_peer = slskd.slskd_candidate_download_url_hash(fixture_candidate(r"\\peer-private\A\same.cbz", "peer-b"))
    require(left == right and left != other_peer, "shared SLSKD candidate identity must normalize path representation but retain the peer")
    strict_left = slskd.slskd_private_locator_digest(fixture_candidate(r"\\peer-private\A\same.cbz", "peer-a"))
    strict_right = slskd.slskd_private_locator_digest(fixture_candidate(r"\\peer-private\B\same.cbz", "peer-a"))
    require(strict_left != strict_right, "force-grab locator binding must include the full remote path")
    with mock.patch.object(slskd, "slskd_post", return_value={"transfers": [{"id": "transfer-1", "username": "peer-a", "filename": r"A\same.cbz", "size": 42_000_000}]}):
        enqueue = slskd.slskd_enqueue_candidate(fixture_candidate(r"A\same.cbz", "peer-a"), dry_run=False)
    matched = slskd.auto_grab_transfer_from_enqueue(enqueue, fixture_candidate(r"A\same.cbz", "peer-a"), strict_path=True)
    require(matched["match_status"] == "matched" and matched["transfer"]["id"] == "transfer-1", "SLSKD enqueue must confirm the exact candidate transfer")
    item = {
        "series": "Absolute Batman: The Court of Owls",
        "issue": "1",
        "unit_type": "issue",
        "year": 2024,
        "pack_allowed": True,
    }
    expected_queries = [
        "Absolute Batman The Court of Owls #1",
        "Absolute Batman The Court of Owls",
        "Batman The Court of Owls",
        "Batman The Court of Owls v01",
        "Batman The Court of Owls omnibus",
        "Court of Owls",
    ]
    require(
        slskd.manual_search_query_variants(item, ["Absolute Batman: The Court of Owls #1"])[:6] == expected_queries,
        "Court of Owls discovery must preserve full, stripped, issue, short-title, and year anchors",
    )
    require("Absolute" not in [query.strip() for query in expected_queries], "query planning must never collapse the title to only the imprint prefix")
    calls = []
    response_fixture = [{
        "username": "private-court-peer",
        "hasFreeUploadSlot": True,
        "files": [
            {"filename": r"Comics\Batman v01 - The Court of Owls (2012).cbr", "size": 42_000_000},
            {"filename": r"Comics\Batman - The Court of Owls Saga.cbr", "size": 84_000_000},
            {"filename": r"Comics\Batman - The Court of Owls Omnibus v1 (2012) TPB.cbr", "size": 148_000_000},
            {"filename": r"Comics\Batman - The Court of Owls 001 (2024).cbr", "size": 21_000_000},
            {"filename": r"Audio\Court of Owls.mp3", "size": 5_000_000},
        ],
    }]

    def search(query, **_kwargs):
        calls.append(query)
        return response_fixture

    with (
        mock.patch.object(slskd, "slskd_search", side_effect=search),
        mock.patch.object(slskd, "slskd_enqueue_candidate", side_effect=AssertionError("discovery must not enqueue")),
        mock.patch.object(slskd, "save_actions", side_effect=AssertionError("discovery must not write actions")),
        mock.patch.object(slskd, "write_json", side_effect=AssertionError("discovery must not write cache or state")),
        mock.patch.object(slskd, "log", side_effect=AssertionError("discovery must not write logs")),
    ):
        result = slskd.manual_search_discovery(
            item,
            ["Absolute Batman: The Court of Owls #1"],
            max_queries=99,
            candidate_limit=10,
        )
    require(result["completed"] and result["status"] == "results", "matched discovery must complete as results")
    require(calls == expected_queries, "right-work but unit-incompatible rows must not stop the bounded SLSKD plan")
    require(len(result["candidates"]) == 3, "volume and collected-edition comics must be discoverable while the unrelated 2024 monthly and audio are rejected")
    require(not any("2024" in row["title"] for row in result["candidates"]), "prefixless 2024 monthly issue must not match the older Absolute collection")
    saga_candidate = next(row for row in result["candidates"] if "Saga" in row["title"])
    saga_public = inkdrop_manual_search.normalize_candidate(
        saga_candidate,
        {"canonical_work_title": "Absolute Batman: The Court of Owls", "unit_type": "issue", "unit_number": "1", "pack_allowed": True},
        search_run_id="court-of-owls-smoke",
        provider_id="slskd",
    )
    require(saga_public["pack_candidate"] and saga_public["acquisition_capability"] != "automatic", "Manual Search Saga evidence must remain non-automatic without unit compatibility")
    candidate = result["candidates"][0]
    require(candidate["acquisition_capability"] != "automatic", "a unit-incompatible SLSKD result must not become automatic")
    require(candidate["auto_grab_verdict"] == "blocked" and candidate["block_reasons"], "Manual Search must retain the shared auto-grab rejection evidence")
    require(candidate["review_basis"] == ["peer_source", "operator_assisted_handoff", "shared_auto_grab_gate"], "SLSKD review must expose the shared safety gate, not an unexplained default")
    serialized_evidence = json.dumps(result["evidence"])
    require("Court of Owls" not in serialized_evidence and "Absolute" not in serialized_evidence, "attempt evidence must not retain query text")
    require(
        len({row["query_fingerprint"] for row in result["evidence"]["attempts"]}) == len(expected_queries),
        "privacy-safe fingerprints must make every bounded attempted query observable",
    )

    connector_filename = "Court of Owls 001.cbz"
    automated_item = {"series": "Court Owls", "issue": "1"}
    require(not slskd.item_match_details(connector_filename, automated_item)["matched"], "connector tolerance must not change automated matching")
    automated_candidates, _ = slskd.candidates_from_responses(
        [{"username": "peer", "files": [{"filename": connector_filename, "size": 1_000_000}]}],
        automated_item,
    )
    require(not automated_candidates, "connector-only automated candidate must remain rejected and non-autopick")
    require(slskd.item_match_details(connector_filename, {**automated_item, "manual_search_discovery": True})["matched"], "Assisted Manual Search may tolerate title connectors")

    automatic_saga = {
        "filename": "Saga 001.cbz",
        "size": 3 * 1024 * 1024 * 1024,
        "extension": ".cbz",
        "score": 80,
        "has_free_upload_slot": True,
        "upload_speed": 1000,
        "queue_length": 0,
        "locked": False,
        "username": "peer",
    }
    saga_verdict = slskd.auto_grab_candidate_verdict(automatic_saga, {"series": "Saga", "issue": "1", "pack_allowed": False})
    require(not saga_verdict["is_pack_candidate"], "automatic Saga issue must retain legacy single-item classification")
    require(saga_verdict["size_ceiling_bytes"] == 2 * 1024 * 1024 * 1024 and not saga_verdict["autopick_eligible"], "3 GB automatic Saga issue must retain the 2 GiB ceiling and remain non-autopick")

    large_response = [{
        "username": "bounded-peer",
        "files": [{"filename": f"Court of Owls 001 copy {index}.cbz", "size": 1_000_000} for index in range(50_000)],
    }]
    match_calls = []

    def bounded_match(filename, _item):
        match_calls.append(filename)
        return {"matched": True, "score": 80, "reasons": [], "penalties": [], "score_reasons": []}

    with (
        mock.patch.object(slskd, "slskd_search", return_value=large_response),
        mock.patch.object(slskd, "item_match_details", side_effect=bounded_match),
        mock.patch.object(slskd, "attach_match_explanation", side_effect=lambda candidate, _item: candidate | {"score": 80}),
    ):
        bounded = slskd.manual_search_discovery(item, ["private bounded query"], max_queries=1, candidate_limit=1)
    require(len(match_calls) <= 52 and bounded["evidence"]["processed_file_count"] <= 50, f"candidate_limit must impose a hard normalization file budget plus bounded safety projection: calls={len(match_calls)} evidence={bounded['evidence']}")
    require(bounded["status"] == "results_partial" and bounded["evidence"]["partial_reason"] == "slskd_normalization_file_cap", "bounded partial results must report the file cap truthfully")

    slow_calls = []

    def slow_match(filename, _item):
        slow_calls.append(filename)
        time.sleep(0.005)
        return {"matched": False, "score": -1, "reasons": [], "penalties": ["fixture"]}

    started = time.monotonic()
    with (
        mock.patch.object(slskd, "slskd_search", return_value=[{"files": large_response[0]["files"][:250]}]),
        mock.patch.object(slskd, "item_match_details", side_effect=slow_match),
    ):
        slow = slskd.manual_search_discovery(item, ["private slow query"], max_queries=1, candidate_limit=1, deadline=time.time() + 0.02)
    require(time.monotonic() - started < 0.2 and len(slow_calls) < 20, "deadline must interrupt slow response normalization")
    require(not slow["completed"] and slow["status"] == "provider_timeout" and slow["evidence"]["partial_reason"] == "slskd_normalization_timeout", "normalization timeout must never be reported as zero_results")

    with (
        mock.patch.object(slskd, "source_queries", return_value=[]),
        mock.patch.object(slskd, "slskd_search", return_value=[]),
        mock.patch.object(slskd, "candidates_from_responses", return_value=([], {"rejected_file_count": 0})),
    ):
        zero = slskd.manual_search_discovery(item, ["private zero query"], max_queries=1)
    require(zero["completed"] and zero["status"] == "zero_results", "a completed empty provider call must be a true zero")

    with (
        mock.patch.object(slskd, "manual_search_query_variants", return_value=["q1", "q2", "q3", "q4", "q5", "q6"]),
        mock.patch.object(slskd, "slskd_search", return_value=[]) as expansion_search,
        mock.patch.object(slskd, "candidates_from_responses", return_value=([], {"rejected_file_count": 0})),
    ):
        expanded = slskd.manual_search_discovery(item, [], max_queries=1)
    require(expanded["completed"] and expanded["status"] == "zero_results", "an exhausted expansion pool must still resolve to a true, reportable zero")
    require(
        [call.args[0] for call in expansion_search.call_args_list] == ["q1", "q2", "q3"],
        f"a genuine zero at a 1-query cap must expand by exactly 2 more from the same priority-ordered pool, never more: {expansion_search.call_args_list}",
    )
    require(expanded["evidence"]["zero_result_expansion_query_count"] == 2, f"evidence must report the expansion count that actually ran: {expanded['evidence']}")
    require(len(expanded["evidence"]["attempts"]) == 3, "every expansion attempt must be visible in the returned evidence")

    with mock.patch.object(slskd, "slskd_search", side_effect=TimeoutError("late")) as failure_search:
        failed = slskd.manual_search_discovery(item, ["private timeout query"], max_queries=1)
    require(not failed["completed"] and failed["status"] == "provider_timeout", "a provider timeout must not be reinterpreted as a zero to expand from")
    require(failure_search.call_count == 1, "a real provider failure must not spend the zero-result expansion budget")

    with (
        mock.patch.object(slskd, "manual_search_query_variants", return_value=["q1", "q2", "q3"]),
        mock.patch.object(
            slskd,
            "slskd_search",
            return_value=[{"username": "peer", "files": [{"filename": "Something 001.cbz", "size": 1_000_000}]}],
        ) as found_search,
        mock.patch.object(
            slskd,
            "candidates_from_responses",
            return_value=([{"filename": "Something 001.cbz", "username": "peer", "size": 1_000_000}], {"rejected_file_count": 0}),
        ),
        mock.patch.object(
            slskd,
            "annotate_auto_grab_verdicts",
            side_effect=lambda candidates, _item: [dict(c, auto_grab={"verdict": "blocked", "blockers": ["wrong_issue_number"]}) for c in candidates],
        ),
    ):
        found_but_blocked = slskd.manual_search_discovery(item, [], max_queries=1)
    require(found_but_blocked["status"] != "zero_results", "any raw candidate evidence, even one later blocked, is not a zero to expand from")
    require(found_search.call_count == 1, "expansion must not fire once a query has returned any candidate evidence")

    with mock.patch.object(slskd, "slskd_search", side_effect=slskd.SLSKDProviderUnavailable("offline")):
        unavailable = slskd.manual_search_discovery(item, ["private unavailable query"], max_queries=1)
    require(not unavailable["completed"] and unavailable["status"] == "provider_unavailable", "provider unavailability must not masquerade as zero")

    with mock.patch.object(slskd, "slskd_search", side_effect=TimeoutError("late")):
        timeout = slskd.manual_search_discovery(item, ["private timeout query"], max_queries=1)
    require(not timeout["completed"] and timeout["status"] == "provider_timeout", "provider timeout must remain distinct")
    for evidence in (unavailable["evidence"], timeout["evidence"]):
        serialized = json.dumps(evidence)
        require("private" not in serialized and "offline" not in serialized and "late" not in serialized, "failure evidence must be count-only and privacy-safe")

    descender_item = {
        "series": "Descender",
        "issue": "25",
        "unit_type": "issue",
        "media_type": "western_comics",
        "publisher": "Image",
        "year": 2017,
        "language": "english",
        "pack_allowed": True,
    }
    descender_response = [{
        "username": "private-descender-peer",
        "hasFreeUploadSlot": True,
        "files": [{"filename": r"Comics\Descender\Descender 025 (2017) (digital) (Minutemen).cbr", "size": 111_750_000}],
    }]
    with mock.patch.object(slskd, "slskd_search", side_effect=[descender_response, TimeoutError("late sibling query")]):
        descender = slskd.manual_search_discovery(
            descender_item,
            ["Descender 25", "Descender v25"],
            max_queries=2,
            candidate_limit=10,
        )
    require(descender["completed"] and descender["status"] == "results", f"a settled Descender result must stop later sibling searches: {descender}")
    require(len(descender["candidates"]) == 1 and "025" in descender["candidates"][0]["title"], "the completed SLSKD query result must remain reviewable")
    descender_candidate = descender["candidates"][0]
    require(descender_candidate["auto_grab_verdict"] == "auto_grab_safe", f"an exact safe Descender issue must reuse the automatic SLSKD verdict: {descender_candidate}")
    require(descender_candidate["acquisition_capability"] == "automatic" and not descender_candidate["assisted_only"], "an exact safe SLSKD issue must not require manual approval")

    for unsafe_filename, expected_code in (
        (r"Comics\Black Science\Black Science 025 (2017) (digital) (Minutemen).cbr", "single-word title missing from filename"),
        (r"Comics\Descender\Descender 026 (2017) (digital) (Minutemen).cbr", "wrong_issue_number"),
    ):
        unsafe = fixture_candidate(unsafe_filename)
        unsafe["size"] = 111_750_000
        verdict = slskd.annotate_auto_grab_verdicts([unsafe], descender_item)[0]["auto_grab"]
        require(verdict["verdict"] == "blocked" and expected_code in verdict["blockers"], f"unsafe SLSKD candidate must remain blocked: {unsafe_filename}: {verdict}")

    with (
        mock.patch.object(slskd, "require_slskd_ready_for_search"),
        mock.patch.object(slskd, "slskd_post") as bounded_post,
        mock.patch.object(slskd, "slskd_get", return_value=[]) as bounded_get,
    ):
        short_started = time.monotonic()
        slskd.slskd_search("Descender 25", wait_seconds=8, deadline=time.time() + 3)
    require(time.monotonic() - short_started < 3 and bounded_post.call_count == 1 and 1 <= bounded_get.call_count <= 3, "a short provider budget must poll within its bound instead of failing its old single-snapshot floor")

    fake_clock = [1_000.0]
    delayed_snapshots = [[], [], [{"username": "late-peer", "files": [{"filename": "Descender 025.cbr", "size": 10}]}], [{"username": "late-peer", "files": [{"filename": "Descender 025.cbr", "size": 10}]}], [{"username": "late-peer", "files": [{"filename": "Descender 025.cbr", "size": 10}]}]]

    def advance(seconds):
        fake_clock[0] += seconds

    with (
        mock.patch.object(slskd, "now", side_effect=lambda: fake_clock[0]),
        mock.patch.object(slskd.time, "sleep", side_effect=advance),
        mock.patch.object(slskd, "require_slskd_ready_for_search"),
        mock.patch.object(slskd, "slskd_post"),
        mock.patch.object(slskd, "slskd_get", side_effect=delayed_snapshots) as delayed_get,
    ):
        delayed = slskd.slskd_search("Descender comics", wait_seconds=10, deadline=1_020.0)
    require(delayed and delayed[0]["files"][0]["filename"] == "Descender 025.cbr", "polling must retain a peer result that arrives after early empty snapshots")
    require(delayed_get.call_count == 5, "polling must stop after the delayed result settles instead of consuming the whole deadline")


def seed(db_path):
    inkdrop_state.ensure_schema(db_path)
    with inkdrop_state.connect(db_path) as con, con:
        con.execute(
            "insert into series(id,title,media_type,year,publisher,source,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
            ("series-fma", "Fullmetal Alchemist", "manga", 2001, "Viz", "native", 1, 1, "{}"),
        )
        con.execute(
            "insert into issues(id,series_id,issue_number,normalized_number,title,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
            ("issue-fma-1", "series-fma", "1", "1", "Chapter 1", 1, 1, json.dumps({"unit_type": "chapter"})),
        )


def capsule_and_grab_fixture():
    private_path = r"\\private-peer\Soulseek\Fullmetal Alchemist Chapter 1.cbz"
    private_username = "capsule-user-only"
    discovery = {
        "completed": True,
        "status": "results",
        "error": "",
        "candidates": [fixture_candidate(private_path, private_username) | {
            "title": "Fullmetal Alchemist Chapter 1.cbz",
            "provider_id": "slskd",
            "protocol": "soulseek",
            "candidate_identity": "slskd-private-fixture",
            "accepted": True,
            "candidate_safe": True,
            "acquisition_capability": "automatic",
            "assisted_only": False,
            "requires_manual_review": False,
            "auto_grab_verdict": "auto_grab_safe",
        }],
        "evidence": {
            "contract_version": 1,
            "planned_query_count": 1,
            "completed_query_count": 1,
            "response_count": 1,
            "candidate_count": 1,
            "partial_error_count": 0,
            "attempts": [{"query_ordinal": 1, "query_fingerprint": "a1b2c3d4e5f6", "status": "completed", "response_count": 1, "candidate_count": 1}],
        },
    }
    with tempfile.TemporaryDirectory(prefix="inkdrop-slskd-manual-") as temp:
        db_path = Path(temp) / "state.sqlite3"
        seed(db_path)
        created = core.create_search_run(
            db_path,
            series_id="series-fma",
            issue_id="issue-fma-1",
            provider_selection=["slskd"],
            requested_by="slskd-smoke",
        )
        require(created["ok"], "SLSKD Manual Search run must be creatable")
        with mock.patch.object(slskd, "manual_search_discovery", return_value=discovery):
            completed = core.process_search_run(db_path, created["run_id"], executor.runner_for_db(db_path))
        require(completed["run"]["state"] == "completed", "automatic discovery results must complete the search checkpoint")
        diagnostics = core.search_diagnostics(db_path, created["run_id"])
        execution = diagnostics["provider_attempts"][0]["diagnostics"]["provider_rows"][0]
        require(execution["query_attempt_summary"] == "1:a1b2c3d4e5f6:completed", "durable diagnostics must expose only bounded query fingerprints")
        require("Fullmetal" not in json.dumps(execution), "durable query observability must not expose title terms")
        results = core.search_results(db_path, created["run_id"])
        require(results["total"] == 1, "one SLSKD candidate must be retained")
        candidate = results["results"][0]
        public_json = json.dumps(candidate)
        require(candidate["acquisition_capability"] == "automatic" and not candidate["assisted_only"], "public safe candidate must be visibly Automatic")
        require(candidate["decision"]["decision"] == "accepted" and not candidate["decision"]["rejection_codes"], "safe SLSKD evidence must remain eligible for handoff")
        require(private_username not in public_json and private_path not in public_json, "private peer identity and full path must not enter public results")
        with inkdrop_state.connect_read(db_path) as con:
            capsule = con.execute(
                "select capsule_json from manual_search_handoff_capsules where candidate_id=?",
                (candidate["candidate_id"],),
            ).fetchone()["capsule_json"]
        require(private_username in capsule and private_path.replace("\\", "\\\\") in capsule, "private username and path must survive only in the handoff capsule")
        require('"status":"ready"' in capsule and '"acquisition_capability":"automatic"' in capsule, "the private handoff capsule must retain automatic SLSKD readiness")

        grab_calls = []
        grab = core.safe_grab_candidate(
            db_path,
            candidate["candidate_id"],
            lambda *_args: grab_calls.append(True) or {"ok": True, "state": "handed_off"},
            requested_by="slskd-smoke",
        )
        require(grab["ok"] and grab["grab_result"]["state"] == "handed_off", "automatic SLSKD candidate must reach the guarded handoff runner")
        require(grab_calls, "automatic SLSKD candidate must not be stopped by the old assisted-only gate")
        with inkdrop_state.connect(db_path) as con, con:
            row = con.execute(
                "select capsule_json from manual_search_handoff_capsules where candidate_id=?",
                (candidate["candidate_id"],),
            ).fetchone()
            tampered = json.loads(row["capsule_json"])
            tampered["_inkdrop_manual_attempt"]["raw"]["candidate"]["filename"] = private_path + ".substituted"
            con.execute(
                "update manual_search_handoff_capsules set capsule_json=? where candidate_id=?",
                (json.dumps(tampered), candidate["candidate_id"]),
            )
        rejected_replay = core.safe_grab_candidate(
            db_path,
            candidate["candidate_id"],
            lambda *_args: grab_calls.append(True) or {"ok": True, "state": "handed_off"},
            requested_by="slskd-smoke",
        )
        require(rejected_replay.get("reason") == "candidate_locator_binding_mismatch", "SLSKD replay must bind the exact peer path to its stored digest")
        require(len(grab_calls) == 1, "a substituted SLSKD path must not reach the handoff runner")

    policy = inkdrop_auth_contracts.mutation_route_policy("/api/manual-search/candidates/example/grab", "POST")
    require(policy and policy["scope"] == "acquisition" and policy["csrf_required_for_cookie_sessions"], "grab route must retain acquisition scope and CSRF enforcement")
    js = Path("web/static/js/inkdrop-manual-search.js").read_text(encoding="utf-8")
    require('acquisition_capability !== "assisted"' in js, "UI must not offer Grab for Assisted candidates")


def main():
    wrapper_fixtures()
    capsule_and_grab_fixture()
    print("inkdrop manual search SLSKD smoke: PASS")


if __name__ == "__main__":
    main()
