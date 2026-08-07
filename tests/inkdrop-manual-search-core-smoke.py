#!/usr/bin/env python3
"""Docker-free contract smoke for additive identity and Manual Search Core."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from core import inkdrop_auth_contracts
from core import inkdrop_manual_search_core as core
from core import inkdrop_state


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def seed(db_path):
    inkdrop_state.ensure_schema(db_path)
    now = 1_700_000_000.0
    with inkdrop_state.connect(db_path) as con, con:
        con.execute(
            "insert into series(id,title,media_type,year,publisher,metadata_provider,metadata_id,kapowarr_id,source,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("series-fma", "Fullmetal Alchemist", "manga", 2001, "Viz", "comicvine", "100", 99, "native", now, now, json.dumps({"aliases": ["Hagane no Renkinjutsushi"], "creators": ["Hiromu Arakawa"], "language": "en"})),
        )
        con.execute(
            "insert into issues(id,series_id,issue_number,normalized_number,title,release_date,metadata_provider,metadata_id,kapowarr_issue_id,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("issue-fma-1", "series-fma", "1", "1", "Chapter 1", "2001-07-12", "mangadex", "101", 9901, now, now, json.dumps({"unit_type": "chapter", "volume_number": "1"})),
        )
        con.execute(
            "insert into media_files(id,path,normalized_path,media_type,series_id,issue_id,source_path,status,active,first_seen_at,last_seen_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("file-fma-1", "/library/Fullmetal Alchemist/Chapter 001.cbz", "/library/fullmetal alchemist/chapter 001.cbz", "manga", "series-fma", "issue-fma-1", "/staging/fma.cbz", "present", 1, now, now, "{}"),
        )
        con.execute(
            "insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
            ("wanted-fma-1", "series-fma", "issue-fma-1", "wanted", now, now, "{}"),
        )
        con.execute(
            "insert into queue_items(id,wanted_id,series_id,issue_id,state,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
            ("queue-fma-1", "wanted-fma-1", "series-fma", "issue-fma-1", "queued", 1, now, now, "{}"),
        )
    return now


def accepted_candidate(title="Fullmetal Alchemist Chapter 1 (Digital).cbz"):
    return {
        "title": title,
        "protocol": "usenet",
        "provider_id": "prowlarr",
        "indexer_name": "ExampleNZB",
        "candidate_identity": "fixture-fma-1",
        "candidate_safe": True,
        "accepted": True,
        "match_confidence": "title_chapter_match",
        "match_score": 95,
        "language": "en",
        "size_bytes": 50_000_000,
        "download_url_hash": "sha256:fixture",
        "_inkdrop_manual_attempt": {
            "source": "prowlarr",
            "provider_id": "prowlarr",
            "provider": "ExampleNZB",
            "protocol": "usenet",
            "download_client": "sabnzbd",
            "candidate_identity": "fixture-fma-1",
            "title": title,
            "status": "sent",
            "download_url": "https://downloads.example.invalid/manual-fixture.nzb",
            "raw": {"candidate": {"title": title, "download_url_hash": "sha256:fixture"}},
        },
    }


def run():
    with tempfile.TemporaryDirectory(prefix="inkdrop-manual-search-") as temp:
        db_path = Path(temp) / "state.sqlite3"
        seed(db_path)

        with inkdrop_state.connect_read(db_path) as con:
            version = con.execute("select value from schema_meta where key='schema_version'").fetchone()["value"]
            original_series = con.execute("select count(*) count from series").fetchone()["count"]
        check(version == str(inkdrop_state.SCHEMA_VERSION), f"additive schema must report version {inkdrop_state.SCHEMA_VERSION}")
        check(original_series == 1, "schema upgrade must preserve legacy Series")

        projection = core.project_series(db_path, "series-fma", now=1_700_000_001)
        check(projection["ok"] and projection["units"] == 1 and projection["artifacts"] == 1, "projection must map Series/Issue/File")
        repeated = core.project_series(db_path, "series-fma", now=1_700_000_002)
        check(repeated["edition_id"] == projection["edition_id"], "projection must be idempotent")
        with inkdrop_state.connect(db_path) as con, con:
            edition = con.execute("select * from identity_editions where legacy_series_id='series-fma'").fetchone()
            check(edition and edition["edition_label"] == "Fullmetal Alchemist", "UI edition label must exist")
            external = con.execute("select provider,provenance from identity_external_ids where entity_id=?", (edition["id"],)).fetchall()
            check(any(row["provider"] == "kapowarr" and row["provenance"] == "migration_provenance" for row in external), "Kapowarr must be provenance only")

            con.execute("insert into series(id,title,media_type,year,publisher,metadata_provider,metadata_id,source,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?)", ("series-fma-2", "Fullmetal Alchemist", "manga", 2011, "Yen Press", "comicvine", "200", "native", 1, 1, "{}"))
        second = core.project_series(db_path, "series-fma-2")
        check(second["work_id"] == projection["work_id"] and second["edition_id"] != projection["edition_id"], "different editions must coexist under one Work")
        equivalence = core.evaluate_edition_equivalence(db_path, projection["edition_id"], second["edition_id"])
        check(not equivalence["equivalent"] and equivalence["confidence"] == "uncertain" and equivalence["conflict_id"], "uncertain identity must remain reviewable")

        profile = core.source_profile_for_series(db_path, "series-fma")
        check(profile["id"] == "manga" and "slskd" in profile["provider_order"], "manga source profile must have usable defaults")

        target_run = core.create_search_run(
            db_path,
            edition_id=projection["edition_id"],
            unit_id="unit:issue-fma-1",
            provider_selection=["prowlarr"],
            requested_by="identity-smoke",
            now=1_700_000_005,
        )
        check(target_run["ok"], "Manual Search must accept additive Edition and Unit IDs")
        target_status = core.get_search_run(db_path, target_run["run_id"])
        check(
            target_status["run"]["series_id"] == "series-fma"
            and target_status["run"]["issue_id"] == "issue-fma-1",
            "additive identities must resolve to compatibility IDs",
        )
        core.cancel_search_run(db_path, target_run["run_id"], requested_by="identity-smoke", now=1_700_000_006)

        created = core.create_search_run(
            db_path,
            series_id="series-fma",
            issue_id="issue-fma-1",
            provider_selection=["prowlarr", "slskd"],
            requested_by="smoke",
            pack_allowed=True,
            now=1_700_000_010,
        )
        check(created["ok"] and created["state"] == "queued", "Manual Search creation must be short and asynchronous")

        calls = []
        provider_barrier = threading.Barrier(2)

        def runner(provider_id, context, queries, profile_row):
            calls.append((provider_id, [row["query"] for row in queries]))
            provider_barrier.wait(timeout=2)
            time.sleep(0.05)
            if provider_id == "slskd":
                return {"completed": False, "error": "provider timed out", "candidates": []}
            rejected_pack = accepted_candidate("Fullmetal Alchemist Omnibus Complete Collection.cbz")
            rejected_pack["candidate_identity"] = "fixture-pack"
            rejected_pack["size_bytes"] = 20 * 1024 * 1024 * 1024
            return {
                "completed": True,
                "candidates": [accepted_candidate(), rejected_pack],
                "health": {"healthy": True},
                "diagnostics": {
                    "contract_version": 1,
                    "provider_rows_considered": 1,
                    "provider_rows": [{
                        "provider_id": "prowlarr",
                        "planned_call_count": 6,
                        "completed_call_count": 6,
                        "planned_variant_count": 6,
                        "completed_variant_count": 6,
                        "returned_result_count": 2,
                    }],
                },
            }

        processing_started = time.monotonic()
        processed = core.process_search_run(db_path, created["run_id"], runner, now=1_700_000_020)
        processing_elapsed = time.monotonic() - processing_started
        check(processed["ok"] and processed["run"]["state"] == "partial", f"one provider failure must not stop other providers: {processed}")
        check(calls and all(len(queries) <= 6 for _, queries in calls), "provider query plans must be bounded")
        check(processing_elapsed < 1.0, "provider calls must overlap instead of serializing latency")
        results = core.search_results(db_path, created["run_id"])
        check(results["total"] == 2, "accepted and rejected candidates must be retained")
        accepted = next(row for row in results["results"] if row["candidate_id"].endswith(row["candidate_id"].split(":")[-1]) and row["original_title"].startswith("Fullmetal Alchemist Chapter"))
        pack = next(row for row in results["results"] if row["pack_candidate"])
        check(accepted["accepted"], "safe exact unit candidate must remain accepted")
        check(not pack["accepted"] and "collected_edition_not_unit_completion" in pack["decision"]["negative_evidence"], "collected edition must not satisfy a chapter")
        check(pack["force_grab"]["eligible"], f"rejected candidate with a concrete supported handoff must expose force-grab eligibility: {pack}")

        binding_run = core.create_search_run(db_path, series_id="series-fma", issue_id="issue-fma-1", provider_selection=["prowlarr"], requested_by="binding-smoke")
        def duplicate_locator_runner(*_args):
            first = accepted_candidate("Binding Candidate.cbz") | {
                "provider_candidate_identity": "same-provider-candidate",
                "match_score": 99,
                "_inkdrop_manual_attempt": accepted_candidate()["_inkdrop_manual_attempt"] | {"download_url": "https://downloads.example.invalid/exact-a.nzb"},
            }
            second = accepted_candidate("Binding Candidate.cbz") | {
                "provider_candidate_identity": "same-provider-candidate",
                "match_score": 1,
                "_inkdrop_manual_attempt": accepted_candidate()["_inkdrop_manual_attempt"] | {"download_url": "https://downloads.example.invalid/substitute-b.nzb"},
            }
            return {"completed": True, "candidates": [first, second], "health": {"healthy": True}}
        binding_done = core.process_search_run(db_path, binding_run["run_id"], duplicate_locator_runner)
        check(binding_done["run"]["state"] == "completed", "duplicate binding fixture must complete")
        binding_result = core.search_results(db_path, binding_run["run_id"])["results"][0]
        with inkdrop_state.connect_read(db_path) as con:
            binding_capsule = con.execute("select capsule_json from manual_search_handoff_capsules where candidate_id=?", (binding_result["candidate_id"],)).fetchone()["capsule_json"]
        check("exact-a.nzb" in binding_capsule and "substitute-b.nzb" not in binding_capsule, "the selected public duplicate must retain its paired private locator")

        # Repeating the same search commonly returns the same provider release.
        # Its provider identity must remain stable while its stored candidate ID
        # is scoped to the new run, otherwise the second run violates the global
        # candidates.id primary key and is mislabeled as a failed search.
        repeated_run = core.create_search_run(
            db_path,
            series_id="series-fma",
            issue_id="issue-fma-1",
            provider_selection=["prowlarr"],
            requested_by="repeated-search-smoke",
            now=1_700_000_021,
        )
        repeated_result = core.process_search_run(
            db_path,
            repeated_run["run_id"],
            lambda *_args: {"completed": True, "candidates": [accepted_candidate()]},
            now=1_700_000_022,
        )
        repeated_candidates = core.search_results(db_path, repeated_run["run_id"])["results"]
        check(repeated_result["run"]["state"] == "completed" and len(repeated_candidates) == 1, repeated_result)
        check(repeated_candidates[0]["candidate_id"] != accepted["candidate_id"], "stored candidate IDs must be run-scoped")
        check(
            repeated_candidates[0]["provider_candidate_identity"] == accepted["provider_candidate_identity"],
            "provider candidate identity must remain stable across repeated searches",
        )
        hard_policy = {
            "allowed": True,
            "warning_size_bytes": 1,
            "hard_limit_bytes": 2,
            "collected_editions_satisfy_units": False,
        }
        hard_decision = core._decision(
            {"accepted": True, "pack_candidate": True, "pack_type": "issue_range", "size_bytes": 3},
            {"unit_type": "issue"},
            hard_policy,
        )
        check("pack_size_warning" in hard_decision["negative_evidence"], "pack warning threshold must be explicit")
        check("pack_hard_limit_exceeded" in hard_decision["negative_evidence"] and not hard_decision["accepted"], "hard pack limit must block a candidate")
        diagnostics = core.search_diagnostics(db_path, created["run_id"])
        check(len(diagnostics["queries"]) > 0 and {row["state"] for row in diagnostics["provider_attempts"]} == {"results", "provider_timeout"}, "diagnostics must distinguish results from timeout")
        check(all(row.get("duration_ms", 0) >= 40 for row in diagnostics["provider_attempts"]), "per-provider elapsed timing must be durable")
        prowlarr_diagnostics = next(row for row in diagnostics["provider_attempts"] if row["provider_id"] == "prowlarr")
        execution_rows = prowlarr_diagnostics["diagnostics"]["provider_rows"]
        check(
            execution_rows[0]["planned_call_count"] == 6
            and execution_rows[0]["completed_variant_count"] == 6
            and execution_rows[0]["returned_result_count"] == 2,
            "bounded provider execution depth must survive durable diagnostics",
        )

        claim_run = core.create_search_run(
            db_path,
            series_id="series-fma",
            provider_selection=["prowlarr"],
            requested_by="claim-smoke",
            now=1_700_000_021,
        )
        nested_claim = []

        def claiming_runner(provider_id, context, queries, profile_row):
            nested_claim.append(
                core.process_search_run(
                    db_path,
                    claim_run["run_id"],
                    lambda *_: {"completed": True, "candidates": []},
                    worker_id="duplicate-worker",
                    now=1_700_000_022,
                )
            )
            return {"completed": True, "candidates": []}

        claimed = core.process_search_run(
            db_path,
            claim_run["run_id"],
            claiming_runner,
            worker_id="primary-worker",
            now=1_700_000_022,
        )
        check(claimed["ok"] and claimed["run"]["state"] == "completed", "primary lease holder must complete")
        check(nested_claim and nested_claim[0]["reason"] == "manual_search_run_already_claimed", "a run lease must prevent duplicate provider execution")

        heartbeat_run = core.create_search_run(
            db_path,
            series_id="series-fma",
            provider_selection=["prowlarr"],
            requested_by="lease-heartbeat-smoke",
        )
        heartbeat_started = threading.Event()
        heartbeat_release = threading.Event()
        heartbeat_calls = []
        heartbeat_primary_result = []
        original_lease_seconds = core.DEFAULT_RUN_LEASE_SECONDS

        def heartbeat_runner(*_args):
            heartbeat_calls.append("primary")
            heartbeat_started.set()
            heartbeat_release.wait(3)
            return {"completed": True, "candidates": []}

        core.DEFAULT_RUN_LEASE_SECONDS = 1
        try:
            heartbeat_thread = threading.Thread(
                target=lambda: heartbeat_primary_result.append(
                    core.process_search_run(
                        db_path,
                        heartbeat_run["run_id"],
                        heartbeat_runner,
                        worker_id="heartbeat-primary",
                    )
                ),
                daemon=True,
            )
            heartbeat_thread.start()
            check(heartbeat_started.wait(2), "lease heartbeat fixture did not start provider")
            time.sleep(1.25)
            heartbeat_peer = core.process_search_run(
                db_path,
                heartbeat_run["run_id"],
                lambda *_: heartbeat_calls.append("peer") or {"completed": True, "candidates": []},
                worker_id="heartbeat-peer",
            )
            heartbeat_release.set()
            heartbeat_thread.join(3)
        finally:
            heartbeat_release.set()
            core.DEFAULT_RUN_LEASE_SECONDS = original_lease_seconds
        check(heartbeat_peer.get("reason") == "manual_search_run_already_claimed", "periodic lease renewal must prevent a second worker reclaim after the original lease duration")
        check(heartbeat_primary_result and heartbeat_primary_result[0]["run"]["state"] == "completed", "the heartbeating worker must retain its claim through completion")
        check(heartbeat_calls == ["primary"], "a healthy long-running provider must execute exactly once across competing workers")
        heartbeat_diagnostics = core.search_diagnostics(db_path, heartbeat_run["run_id"])
        check(len(heartbeat_diagnostics["provider_attempts"]) == 1, "lease heartbeats must not create duplicate provider attempts")

        stale_reclaim_run = core.create_search_run(
            db_path,
            series_id="series-fma",
            provider_selection=["prowlarr"],
            requested_by="stale-reclaim-smoke",
        )
        stale_at = time.time() - 600
        with inkdrop_state.connect(db_path) as con, con:
            con.execute(
                "update manual_search_runs set state='running',claim_token='expired-worker',claimed_by='expired-worker',lease_expires_at=?,started_at=?,updated_at=? where id=?",
                (stale_at, stale_at, stale_at, stale_reclaim_run["run_id"]),
            )
            con.execute(
                "insert into manual_search_queries(id,run_id,provider_id,query_text,query_kind,ordinal,created_at) values(?,?,?,?,?,?,?)",
                ("stale-query-fixture", stale_reclaim_run["run_id"], "prowlarr", "stale query", "stale", 0, stale_at),
            )
            con.execute(
                "insert into manual_search_provider_attempts(id,run_id,provider_id,state,started_at,created_at,updated_at) values(?,?,?,?,?,?,?)",
                ("stale-attempt-fixture", stale_reclaim_run["run_id"], "prowlarr", "running", stale_at, stale_at, stale_at),
            )
        stale_reclaimed = core.process_search_run(
            db_path,
            stale_reclaim_run["run_id"],
            lambda *_: {"completed": True, "candidates": []},
            worker_id="stale-reclaim-worker",
        )
        stale_diagnostics = core.search_diagnostics(db_path, stale_reclaim_run["run_id"])
        check(stale_reclaimed["run"]["state"] == "completed", "an actually expired worker claim must remain recoverable")
        check(
            len(stale_diagnostics["queries"]) > 0
            and len({(row["provider_id"], row["ordinal"]) for row in stale_diagnostics["queries"]}) == len(stale_diagnostics["queries"]),
            "reclaim preparation must upsert deterministic queries without unique-key failure",
        )
        check(
            any(row.get("error_code") == "worker_lease_expired" for row in stale_diagnostics["provider_attempts"])
            and any(row["state"] == "zero_results" for row in stale_diagnostics["provider_attempts"]),
            "reclaim must close stale running attempts before recording the replacement attempt",
        )

        grab_calls = []

        def grabber(public, raw):
            grab_calls.append(public["candidate_id"])
            return {"ok": True, "state": "queued_for_handoff", "queue_id": "queue-fma-1", "source_attempt_id": "attempt-fixture"}

        grabbed = core.safe_grab_candidate(db_path, accepted["candidate_id"], grabber, requested_by="smoke", now=1_700_000_022)
        check(grabbed["ok"] and grab_calls, "accepted candidate must support explicit safe grab")
        with inkdrop_state.connect(db_path) as con, con:
            identity = con.execute(
                "select candidate_identity from manual_search_candidates where id=?",
                (accepted["candidate_id"],),
            ).fetchone()["candidate_identity"]
            con.execute(
                """insert into download_tasks(
                       id,queue_id,wanted_id,series_id,issue_id,source_attempt_id,source,provider_id,
                       protocol,download_client,candidate_identity,title,status,state,started_at,updated_at,raw_json
                   ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "task-active-fixture", "queue-fma-1", "wanted-fma-1", "series-fma", "issue-fma-1",
                    None, "prowlarr", "prowlarr", "torrent", "qbittorrent", identity,
                    "Fullmetal Alchemist 1", "sent", "queued", 1_700_000_022, 1_700_000_022, "{}",
                ),
            )
            con.execute(
                "update manual_search_grab_results set download_task_id='task-active-fixture' where candidate_id=?",
                (accepted["candidate_id"],),
            )
        repeated_grab = core.safe_grab_candidate(db_path, accepted["candidate_id"], grabber, requested_by="smoke", now=1_700_000_022)
        check(repeated_grab["ok"] and repeated_grab["idempotent_reuse"] and len(grab_calls) == 1, "safe grab must be idempotent")
        equivalent_run_grab = core.safe_grab_candidate(db_path, repeated_candidates[0]["candidate_id"], grabber, requested_by="smoke", now=1_700_000_022)
        check(equivalent_run_grab["idempotent_reuse"] and len(grab_calls) == 1, "same concrete candidate and unit from a later search must not duplicate handoff")
        with inkdrop_state.connect(db_path) as con:
            capsule_row = con.execute("select capsule_json from manual_search_handoff_capsules where candidate_id=?", (repeated_candidates[0]["candidate_id"],)).fetchone()
            tampered_capsule = json.loads(capsule_row["capsule_json"])
            tampered_attempt = tampered_capsule["_inkdrop_manual_attempt"]
            tampered_attempt["download_url"] = "https://downloads.example.invalid/replayed.nzb"
            con.execute("update manual_search_handoff_capsules set capsule_json=? where candidate_id=?", (json.dumps(tampered_capsule), repeated_candidates[0]["candidate_id"]))
        tampered_reuse = core.safe_grab_candidate(db_path, repeated_candidates[0]["candidate_id"], grabber, requested_by="smoke", now=1_700_000_022)
        check(tampered_reuse.get("reason") == "candidate_locator_binding_mismatch", f"idempotent reuse must reject a substituted private locator: {tampered_reuse}")
        with inkdrop_state.connect(db_path) as con, con:
            con.execute(
                "update download_tasks set status='verified',state='verified',completed_at=?,updated_at=? where id='task-active-fixture'",
                (1_700_000_023, 1_700_000_023),
            )
        rearmed_grab = core.safe_grab_candidate(db_path, accepted["candidate_id"], grabber, requested_by="smoke", now=1_700_000_024)
        check(rearmed_grab["ok"] and not rearmed_grab.get("idempotent_reuse") and len(grab_calls) == 2, "terminal handoff evidence must rearm an active missing queue")
        episode_attempt = {
            "source": "prowlarr",
            "provider_id": "prowlarr",
            "protocol": "torrent",
            "download_client": "qbittorrent",
            "external_id": "same-info-hash",
            "candidate_identity": "manual-episode-fixture",
            "title": "Fullmetal Alchemist 1",
            "status": "verified",
        }
        with inkdrop_state.connect(db_path) as con, con:
            con.execute(
                """insert into source_attempts(
                       id,queue_id,wanted_id,series_id,issue_id,source,status,started_at,completed_at,raw_json
                   ) values(?,?,?,?,?,?,?,?,?,?)""",
                (
                    "terminal-episode-attempt", "queue-fma-1", "wanted-fma-1", "series-fma", "issue-fma-1",
                    "prowlarr", "verified", 1_700_000_020, 1_700_000_021, "{}",
                ),
            )
            terminal_task_id = inkdrop_state.record_download_task_for_attempt(
                con, "queue-fma-1", "wanted-fma-1", "series-fma", "issue-fma-1",
                episode_attempt, "terminal-episode-attempt", started_at=1_700_000_020, completed_at=1_700_000_021,
            )
        active_episode = {**episode_attempt, "status": "sent", "ts": 1_700_000_025}
        projected = inkdrop_state.record_queue_source_attempt(
            db_path, "queue-fma-1", active_episode, attempt_id="fresh-episode-attempt", started_at=1_700_000_025,
        )
        check(projected.get("download_task_id") and projected["download_task_id"] != terminal_task_id, "a fresh active episode must not overwrite terminal task evidence")
        projected_replay = inkdrop_state.record_queue_source_attempt(
            db_path, "queue-fma-1", active_episode, attempt_id="fresh-episode-attempt", started_at=1_700_000_025,
        )
        check(projected_replay.get("download_task_id") == projected["download_task_id"], "replay within the same active episode must remain idempotent")
        completed_episode = inkdrop_state.record_queue_source_attempt(
            db_path,
            "queue-fma-1",
            {**active_episode, "status": "verified", "ts": 1_700_000_026},
            attempt_id="fresh-episode-attempt",
            started_at=1_700_000_025,
            completed_at=1_700_000_026,
        )
        check(completed_episode.get("download_task_id") == projected["download_task_id"], "terminal update must remain attached to the fresh episode")
        with inkdrop_state.connect_read(db_path) as con:
            terminal_task = con.execute("select state,status,source_attempt_id,completed_at from download_tasks where id=?", (terminal_task_id,)).fetchone()
            fresh_task = con.execute("select state,status,source_attempt_id from download_tasks where id=?", (projected["download_task_id"],)).fetchone()
        check((terminal_task["state"], terminal_task["status"]) == ("verified", "verified"), "fresh handoff must preserve old terminal evidence")
        check(terminal_task["source_attempt_id"] == "terminal-episode-attempt" and terminal_task["completed_at"] == 1_700_000_021, "old terminal episode must remain immutable")
        check((fresh_task["state"], fresh_task["status"], fresh_task["source_attempt_id"]) == ("verified", "verified", "fresh-episode-attempt"), "fresh episode must receive its own terminal update")

        forced_calls = []

        def forced_grabber(public, raw):
            forced_calls.append((public["candidate_id"], raw))
            return {"ok": True, "state": "queued_for_handoff", "queue_id": "queue-fma-1", "source_attempt_id": "forced-attempt"}

        normal_rejected = core.safe_grab_candidate(
            db_path,
            pack["candidate_id"],
            forced_grabber,
            requested_by="operator",
            override_pack_warning=True,
            now=1_700_000_023,
        )
        check(normal_rejected["reason"] == "candidate_not_accepted" and not forced_calls, "ordinary grab must not weaken automatic rejection policy")
        unauthorized_force = core.safe_grab_candidate(
            db_path,
            pack["candidate_id"],
            forced_grabber,
            requested_by="operator",
            force_rejected=True,
            confirm_rejected_risk=True,
            override_pack_warning=True,
            now=1_700_000_024,
        )
        check(unauthorized_force["reason"] == "admin_required_for_rejected_candidate_override" and not forced_calls, "rejected override must require server-side admin authorization")
        unconfirmed_force = core.safe_grab_candidate(
            db_path,
            pack["candidate_id"],
            forced_grabber,
            requested_by="operator",
            force_rejected=True,
            force_rejected_authorized=True,
            override_pack_warning=True,
            now=1_700_000_025,
        )
        check(unconfirmed_force["reason"] == "rejected_candidate_risk_confirmation_required" and not forced_calls, "rejected override must require explicit risk confirmation")
        forced = core.safe_grab_candidate(
            db_path,
            pack["candidate_id"],
            forced_grabber,
            requested_by="operator-secret-token=do-not-store",
            force_rejected=True,
            force_rejected_authorized=True,
            confirm_rejected_risk=True,
            override_pack_warning=True,
            now=1_700_000_026,
        )
        check(forced["ok"] and len(forced_calls) == 1, f"confirmed supported rejected candidate must hand off once: {forced}")
        with inkdrop_state.connect(db_path) as con, con:
            identity = con.execute(
                "select candidate_identity from manual_search_candidates where id=?",
                (pack["candidate_id"],),
            ).fetchone()["candidate_identity"]
            con.execute(
                """insert into download_tasks(
                       id,queue_id,wanted_id,series_id,issue_id,source_attempt_id,source,provider_id,
                       protocol,download_client,candidate_identity,title,status,state,started_at,updated_at,raw_json
                   ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "task-forced-active-fixture", "queue-fma-1", "wanted-fma-1", "series-fma", "issue-fma-1",
                    None, "prowlarr", "prowlarr", "torrent", "qbittorrent", identity,
                    "Fullmetal Alchemist pack", "sent", "queued", 1_700_000_026, 1_700_000_026, "{}",
                ),
            )
            con.execute(
                "update manual_search_grab_results set download_task_id='task-forced-active-fixture' where candidate_id=?",
                (pack["candidate_id"],),
            )
        repeated_force = core.safe_grab_candidate(
            db_path,
            pack["candidate_id"],
            forced_grabber,
            requested_by="operator",
            force_rejected=True,
            force_rejected_authorized=True,
            confirm_rejected_risk=True,
            override_pack_warning=True,
            now=1_700_000_027,
        )
        check(repeated_force["idempotent_reuse"] and len(forced_calls) == 1, "forced handoff must remain idempotent")
        with inkdrop_state.connect(db_path) as con, con:
            row = con.execute(
                "select capsule_json from manual_search_handoff_capsules where candidate_id=?",
                (pack["candidate_id"],),
            ).fetchone()
            tampered_forced_capsule = json.loads(row["capsule_json"])
            tampered_forced_capsule["_inkdrop_manual_attempt"]["download_url"] = "https://downloads.example.invalid/substituted-before-auth.torrent"
            con.execute(
                "update manual_search_handoff_capsules set capsule_json=? where candidate_id=?",
                (json.dumps(tampered_forced_capsule), pack["candidate_id"]),
            )
        unauthorized_repeat = core.safe_grab_candidate(
            db_path,
            pack["candidate_id"],
            forced_grabber,
            requested_by="non-admin",
            force_rejected=True,
            confirm_rejected_risk=True,
            now=1_700_000_028,
        )
        check(unauthorized_repeat["reason"] == "admin_required_for_rejected_candidate_override", "idempotent reuse must authorize before inspecting a forced candidate's private capsule")
        unauthorized_unflagged_repeat = core.safe_grab_candidate(
            db_path,
            pack["candidate_id"],
            forced_grabber,
            requested_by="non-admin",
            now=1_700_000_029,
        )
        check(unauthorized_unflagged_repeat["reason"] == "admin_required_for_rejected_candidate_override", "omitting the force flag must not bypass authorization on a previously forced candidate")
        with inkdrop_state.connect_read(db_path) as con:
            forced_event = con.execute(
                "select raw_json from history_events where event_type='manual_search_forced_grab_requested' order by created_at desc limit 1"
            ).fetchone()
        forced_audit = forced_event["raw_json"] if forced_event else ""
        check("collected_edition_not_unit_completion" in forced_audit and '"forced_rejected_candidate":true' in forced_audit, "forced grab audit must retain exact rejection reasons")
        check("do-not-store" not in forced_audit and "<redacted>" in forced_audit, "forced grab audit must redact actor secrets")

        slskd_public = {
            "candidate_id": "manual-candidate:slskd-fixture",
            "accepted": False,
            "provider_id": "slskd",
            "protocol": "soulseek",
            "provider_candidate_identity": "slskd:peer:file",
            "original_title": "Rejected but retrievable.cbz",
            "decision": {"rejection_codes": ["title_mismatch", "unit_mismatch"]},
        }
        slskd_capsule = {
            "_inkdrop_manual_attempt": {
                "protocol": "soulseek",
                "provider_id": "slskd",
                "provider_candidate_identity": "slskd:peer:file",
                "candidate_binding": "manual-candidate:slskd-fixture",
                "download_client": "SLSKD",
                "candidate_identity": "slskd:peer:file",
                "acquisition_capability": "automatic",
                "status": "ready",
                "locator_digest": core._slskd_locator_digest("peer", r"Comics\Rejected but retrievable.cbz", "", "peer"),
                "raw": {"candidate": {"filename": r"Comics\Rejected but retrievable.cbz", "username": "peer"}},
            }
        }
        check(core.forced_grab_candidate_gate(slskd_public, slskd_capsule)["eligible"], "concrete SLSKD handoff must support deliberate force grab")
        substituted_locator = json.loads(json.dumps(slskd_capsule))
        substituted_locator["_inkdrop_manual_attempt"]["raw"]["candidate"]["filename"] = r"Other\different.cbz"
        check(core.forced_grab_candidate_gate(slskd_public, substituted_locator)["reason"] == "candidate_locator_binding_mismatch", "a relabeled SLSKD path must not pass the candidate binding")
        missing_peer = json.loads(json.dumps(slskd_capsule))
        missing_peer["_inkdrop_manual_attempt"]["raw"]["candidate"]["username"] = ""
        check(core.forced_grab_candidate_gate(slskd_public, missing_peer)["reason"] == "safe_handoff_locator_required", "SLSKD force grab must not bypass a missing peer locator")
        invalid_payload = dict(slskd_public, decision={"rejection_codes": ["rejected_invalid_image_payload"]})
        check(not core.forced_grab_candidate_gate(invalid_payload, slskd_capsule)["eligible"], "known invalid artifact evidence must remain non-overridable")
        preview_payload = dict(slskd_public, preview_or_sample=True)
        check(not core.forced_grab_candidate_gate(preview_payload, slskd_capsule)["eligible"], "preview/sample artifacts must remain non-overridable")
        for blocked_code in ("duplicate", "already_imported", "known_bad", "malicious", "invalid_archive", "preview", "sample", "missing_credential", "unsupported_client", "401", "403"):
            blocked = dict(slskd_public, decision={"rejection_codes": [blocked_code]})
            check(not core.forced_grab_candidate_gate(blocked, slskd_capsule)["eligible"], f"{blocked_code} must remain non-overridable")

        cancel_run = core.create_search_run(db_path, series_id="series-fma", requested_by="cancel-smoke", now=1_700_000_030)
        cancelled = core.cancel_search_run(db_path, cancel_run["run_id"], requested_by="cancel-smoke", now=1_700_000_031)
        check(cancelled["state"] == "cancelled", "queued search must be cancellable")

        running_cancel = core.create_search_run(
            db_path,
            series_id="series-fma",
            provider_selection=["prowlarr"],
            requested_by="running-cancel-smoke",
            now=1_700_000_032,
        )
        call_started = threading.Event()
        release_call = threading.Event()
        running_result = []

        def blocking_runner(*_args):
            call_started.set()
            release_call.wait(2)
            return {"completed": True, "candidates": [accepted_candidate()]}

        processing_thread = threading.Thread(
            target=lambda: running_result.append(
                core.process_search_run(db_path, running_cancel["run_id"], blocking_runner, now=1_700_000_033)
            ),
            daemon=True,
        )
        processing_thread.start()
        check(call_started.wait(2), "running cancellation fixture did not start provider")
        core.cancel_search_run(db_path, running_cancel["run_id"], requested_by="running-cancel-smoke", now=1_700_000_034)
        release_call.set()
        processing_thread.join(3)
        check(running_result and running_result[0]["state"] == "cancelled", "provider completion must not overwrite cancellation truth")
        cancelled_diagnostics = core.search_diagnostics(db_path, running_cancel["run_id"])
        check(cancelled_diagnostics["provider_attempts"][0]["state"] == "cancelled", "in-flight provider attempt must show cancelled")
        check(core.search_results(db_path, running_cancel["run_id"])["total"] == 0, "late provider results must not persist after cancellation")

        with inkdrop_state.connect(db_path) as con, con:
            con.execute("update source_profiles set provider_timeout_seconds=1,max_concurrency=4 where id='manga'")

        completion_order_run = core.create_search_run(
            db_path,
            series_id="series-fma",
            provider_selection=["prowlarr", "slskd"],
            requested_by="completion-order-smoke",
            now=1_700_000_034.1,
        )
        release_slow_provider = threading.Event()

        def completion_order_runner(provider_id, *_args):
            if provider_id == "prowlarr":
                release_slow_provider.wait(2)
                return {"completed": True, "candidates": []}
            candidate = accepted_candidate()
            candidate["candidate_identity"] = "fixture-fast-provider"
            return {"completed": True, "candidates": [candidate]}

        completion_order_result = core.process_search_run(
            db_path,
            completion_order_run["run_id"],
            completion_order_runner,
            now=1_700_000_034.2,
        )
        completion_order_diagnostics = core.search_diagnostics(db_path, completion_order_run["run_id"])
        check(completion_order_result["run"]["state"] == "partial", "a slow first provider must not discard a completed later provider")
        check(core.search_results(db_path, completion_order_run["run_id"])["total"] == 1, "completion-driven collection must persist fast-provider evidence before another provider expires")
        check(
            {row["provider_id"]: row["state"] for row in completion_order_diagnostics["provider_attempts"]}
            == {"prowlarr": "provider_timeout", "slskd": "results"},
            "provider outcomes must reflect completion order instead of provider declaration order",
        )
        release_slow_provider.set()

        with inkdrop_state.connect(db_path) as con, con:
            con.execute("update source_profiles set provider_timeout_seconds=10 where id='manga'")

        incremental_run = core.create_search_run(
            db_path,
            series_id="series-fma",
            issue_id="issue-fma-1",
            provider_selection=["direct", "prowlarr", "slskd"],
            requested_by="incremental-persistence-smoke",
        )
        late_lanes = threading.Event()
        early_returned = threading.Event()
        incremental_result = []

        def incremental_runner(provider_id, *_args):
            if provider_id == "direct":
                candidate = accepted_candidate()
                candidate.update({
                    "candidate_identity": "incremental-early-candidate",
                    "username": "private-remote-user",
                    "remote_filename": r"Private\\Comics\\Fullmetal Alchemist Chapter 1.cbz",
                })
                early_returned.set()
                return {"completed": True, "candidates": [candidate]}
            late_lanes.wait(15)
            if provider_id == "prowlarr":
                raise RuntimeError("fixture provider failure")
            return {"completed": True, "candidates": []}

        incremental_thread = threading.Thread(
            target=lambda: incremental_result.append(core.process_search_run(
                db_path, incremental_run["run_id"], incremental_runner,
            )),
            daemon=False,
        )
        incremental_thread.start()
        incremental_snapshot = None
        incremental_results = None
        incremental_diagnostics = None
        incremental_persisted = False
        try:
            check(early_returned.wait(3), "early provider fixture did not return")
            poll_deadline = time.monotonic() + 5
            while time.monotonic() < poll_deadline:
                incremental_results = core.search_results(db_path, incremental_run["run_id"])
                incremental_diagnostics = core.search_diagnostics(db_path, incremental_run["run_id"])
                polled_states = {row["provider_id"]: row["state"] for row in incremental_diagnostics["provider_attempts"]}
                if incremental_results["total"] == 1 and polled_states.get("direct") == "results":
                    incremental_snapshot = core.get_search_run(db_path, incremental_run["run_id"])
                    if incremental_snapshot["candidate_counts"]["total"] == 1:
                        incremental_persisted = True
                        break
                time.sleep(0.025)
            check(incremental_persisted, (incremental_snapshot, incremental_results, incremental_diagnostics))
            check(incremental_snapshot["run"]["state"] == "running", incremental_snapshot)
            check(incremental_results["total"] == 1, incremental_results)
            attempt_states = {row["provider_id"]: row["state"] for row in incremental_diagnostics["provider_attempts"]}
            check(attempt_states.get("direct") == "results", attempt_states)
            check(attempt_states.get("prowlarr") == "running" and attempt_states.get("slskd") == "running", attempt_states)
            check(incremental_thread.is_alive(), "incremental run finished before persisted results were rendered")
            public_incremental = json.dumps(incremental_results, sort_keys=True)
            check("private-remote-user" not in public_incremental and "Private\\\\Comics" not in public_incremental, public_incremental)
            early_candidate_id = incremental_results["results"][0]["candidate_id"]
        finally:
            late_lanes.set()
            incremental_thread.join(12)
            if incremental_thread.is_alive():
                incremental_thread.join()
        check(not incremental_thread.is_alive(), "incremental worker must join before temporary database cleanup")
        check(incremental_result and incremental_result[0]["run"]["state"] == "partial", incremental_result)
        final_incremental = core.search_results(db_path, incremental_run["run_id"])
        check(final_incremental["total"] == 1 and final_incremental["results"][0]["candidate_id"] == early_candidate_id, final_incremental)
        repeated_incremental = core.process_search_run(db_path, incremental_run["run_id"], incremental_runner)
        check(repeated_incremental.get("already_terminal") and core.search_results(db_path, incremental_run["run_id"])["total"] == 1, repeated_incremental)

        with inkdrop_state.connect(db_path) as con, con:
            con.execute("update source_profiles set provider_timeout_seconds=2 where id='manga'")

        near_deadline_run = core.create_search_run(
            db_path, series_id="series-fma", issue_id="issue-fma-1",
            provider_selection=["direct", "prowlarr", "slskd"],
            requested_by="near-deadline-persistence-smoke",
        )
        release_late_lane = threading.Event()
        near_deadline_result = []

        def near_deadline_runner(provider_id, *_args):
            if provider_id == "direct":
                return {"completed": True, "candidates": []}
            if provider_id == "slskd":
                time.sleep(0.7)
                candidate = accepted_candidate()
                candidate.update({"candidate_identity": "near-deadline-soulseek", "protocol": "soulseek"})
                return {"completed": True, "candidates": [candidate]}
            release_late_lane.wait(4)
            return {"completed": True, "candidates": [accepted_candidate("Late duplicate must not replace early.cbz")]}

        near_deadline_thread = threading.Thread(
            target=lambda: near_deadline_result.append(core.process_search_run(
                db_path, near_deadline_run["run_id"], near_deadline_runner,
            )), daemon=True,
        )
        near_deadline_thread.start()
        visible_near_deadline = None
        for _ in range(150):
            visible_near_deadline = core.search_results(db_path, near_deadline_run["run_id"])
            if visible_near_deadline["total"] == 1:
                break
            time.sleep(0.02)
        near_deadline_status = core.get_search_run(db_path, near_deadline_run["run_id"])
        check(visible_near_deadline["total"] == 1 and near_deadline_status["run"]["state"] == "running", (visible_near_deadline, near_deadline_status))
        near_deadline_candidate_id = visible_near_deadline["results"][0]["candidate_id"]
        near_deadline_thread.join(2)
        check(near_deadline_result and near_deadline_result[0]["run"]["state"] == "partial", near_deadline_result)
        release_late_lane.set()
        time.sleep(0.05)
        after_late_timeout = core.search_results(db_path, near_deadline_run["run_id"])
        check(after_late_timeout["total"] == 1 and after_late_timeout["results"][0]["candidate_id"] == near_deadline_candidate_id, after_late_timeout)

        queued_provider_run = core.create_search_run(
            db_path,
            series_id="series-fma",
            provider_selection=["slskd", "suwayomi", "mangadex", "prowlarr", "direct"],
            requested_by="queued-provider-smoke",
            now=1_700_000_034.3,
        )

        def queued_provider_runner(provider_id, *_args):
            time.sleep(0.35 if provider_id == "direct" else 0.8)
            return {"completed": True, "candidates": []}

        queued_result = core.process_search_run(
            db_path,
            queued_provider_run["run_id"],
            queued_provider_runner,
            now=1_700_000_034.4,
        )
        queued_diagnostics = core.search_diagnostics(db_path, queued_provider_run["run_id"])
        check(queued_result["run"]["state"] == "completed", "queue wait must not consume a provider's execution timeout")
        check(
            len(queued_diagnostics["provider_attempts"]) == 5
            and {row["state"] for row in queued_diagnostics["provider_attempts"]} == {"zero_results"},
            f"every queued provider must receive a full timeout measured from actual execution start: {queued_diagnostics['provider_attempts']}",
        )

        persistence_fence_run = core.create_search_run(
            db_path,
            series_id="series-fma",
            provider_selection=["prowlarr"],
            requested_by="persistence-fence-smoke",
            now=1_700_000_034.5,
        )
        original_normalize_candidate = core.manual_contract.normalize_candidate
        cancellation_injected = False

        def cancel_during_normalization(*args, **kwargs):
            nonlocal cancellation_injected
            normalized = original_normalize_candidate(*args, **kwargs)
            if not cancellation_injected:
                cancellation_injected = True
                core.cancel_search_run(
                    db_path,
                    persistence_fence_run["run_id"],
                    requested_by="persistence-fence-smoke",
                    now=1_700_000_034.7,
                )
            return normalized

        core.manual_contract.normalize_candidate = cancel_during_normalization
        try:
            persistence_fence_result = core.process_search_run(
                db_path,
                persistence_fence_run["run_id"],
                lambda *_: {"completed": True, "candidates": [accepted_candidate()]},
                now=1_700_000_034.6,
            )
        finally:
            core.manual_contract.normalize_candidate = original_normalize_candidate
        persistence_fence_diagnostics = core.search_diagnostics(db_path, persistence_fence_run["run_id"])
        check(persistence_fence_result["state"] == "cancelled", "an atomic persistence fence must preserve cancellation truth")
        check(persistence_fence_diagnostics["provider_attempts"][0]["state"] == "cancelled", "persistence must not overwrite a cancelled attempt")
        check(core.search_results(db_path, persistence_fence_run["run_id"])["total"] == 0, "candidates must not write after cancellation wins the persistence fence")

        with inkdrop_state.connect(db_path) as con, con:
            con.execute("update source_profiles set provider_timeout_seconds=1 where id='manga'")

        timeout_run = core.create_search_run(
            db_path,
            series_id="series-fma",
            provider_selection=["prowlarr"],
            requested_by="timeout-smoke",
            now=1_700_000_035,
        )
        timeout_started = time.monotonic()
        release_late_timeout = threading.Event()
        late_timeout_finished = threading.Event()

        def late_timeout_runner(*_args):
            release_late_timeout.wait(5)
            late_timeout_finished.set()
            return {"completed": True, "candidates": [accepted_candidate()]}

        timed_out = core.process_search_run(
            db_path,
            timeout_run["run_id"],
            late_timeout_runner,
            now=1_700_000_036,
        )
        timeout_elapsed = time.monotonic() - timeout_started
        timeout_diagnostics = core.search_diagnostics(db_path, timeout_run["run_id"])
        check(timed_out["run"]["state"] == "failed", "an all-provider timeout must fail the run truthfully")
        check(timeout_diagnostics["provider_attempts"][0]["state"] == "provider_timeout", "elapsed provider deadline must be distinguished from zero results")
        check(0.8 <= timeout_elapsed < 3 and not late_timeout_finished.is_set(), "provider deadline must return without waiting for late completion")
        check(core.search_results(db_path, timeout_run["run_id"])["total"] == 0, "late timed-out provider result must not persist")
        release_late_timeout.set()
        with inkdrop_state.connect(db_path) as con, con:
            con.execute("update source_profiles set provider_timeout_seconds=25,max_concurrency=3 where id='manga'")

        recovery_run = core.create_search_run(db_path, series_id="series-fma", provider_selection=["prowlarr"], requested_by="recovery-smoke", now=1_700_000_040)
        with inkdrop_state.connect(db_path) as con, con:
            con.execute(
                "update manual_search_runs set state='running',claim_token='dead-worker',lease_expires_at=? where id=?",
                (1_700_000_041, recovery_run["run_id"]),
            )
        recovered = core.process_pending_search_runs(
            db_path,
            lambda *_: {"completed": True, "candidates": []},
            limit=1,
            worker_id="recovery-worker",
            now=1_700_000_050,
        )
        check(recovered["ok"] and recovered["processed"] == 1, "expired worker leases must recover in a bounded pass")

        with inkdrop_state.connect_read(db_path) as con:
            audit_events = con.execute(
                "select event_type from history_events where source='manual_search'"
            ).fetchall()
        event_types = {row["event_type"] for row in audit_events}
        check(
            {"manual_search_started", "manual_search_completed", "manual_search_cancelled", "manual_search_grab_requested"} <= event_types,
            "start, completion, cancellation, and grab must retain audit evidence",
        )

        policy = inkdrop_auth_contracts.mutation_route_policy("/api/manual-search/candidates/example/grab", "POST")
        check(policy and policy["scope"] == "acquisition" and policy["csrf_required_for_cookie_sessions"], "grab route must require acquisition auth and CSRF")

        print(json.dumps({"ok": True, "schema": version, "projection": projection, "run": processed["run"]["state"], "candidates": results["total"], "queries": len(diagnostics["queries"]), "grab_idempotent": True}, indent=2))


if __name__ == "__main__":
    run()
