#!/usr/bin/env python3
"""Deterministic coverage for bounded SLSKD numbered-directory handoff."""

import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import inkdrop_completed_import as completed
from core import inkdrop_missing_acquire as acquire
from core import inkdrop_manual_source_autoresolve as autoresolve
from core import inkdrop_slskd_source_probe as probe
from core import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wanted(issue, state="queued"):
    issue = int(issue)
    return {
        "review_id": f"descender-{issue}",
        "series": "Descender",
        "query": "Descender",
        "issue": str(issue),
        "publisher": "Image Comics",
        "watch_publisher": "Image Comics",
        "year": "2015",
        "autopilot_queue": True,
        "autopilot_state": state,
    }


def descender_response():
    sizes = [
        69.92, 61.8, 57.07, 56.9, 63.17, 57.0, 58.69, 65.51,
        60.17, 55.18, 52.13, 53.34, 56.9, 61.53, 64.41, 56.09,
        58.11, 56.5, 58.82, 55.68, 52.16, 97.89, 101.54, 114.59,
        111.75, 104.51, 72.15, 53.71, 51.15, 55.71, 63.3, 61.97,
    ]
    files = []
    for number, size_mib in enumerate(sizes, start=1):
        if number <= 9:
            year = 2015
        elif number <= 17:
            year = 2016
        elif number <= 26:
            year = 2017
        else:
            year = 2018
        files.append({
            "filename": f"Comics\\Descender\\Descender {number:03d} ({year}).cbr",
            "size": int(size_mib * 1024 * 1024),
        })
    files.extend([
        {"filename": "Comics\\Descender\\Ascender 001 (2019).cbr", "size": 55 * 1024 * 1024},
        {"filename": "Comics\\Descender\\Descender The Machine 001.cbr", "size": 50 * 1024 * 1024},
        {"filename": "Comics\\Descender\\Descender Covers 001.cbz", "size": 8 * 1024 * 1024},
        {"filename": "Comics\\Descender\\folder.jpg", "size": 900 * 1024},
        {"filename": "Comics\\Descender\\notes.txt", "size": 100},
    ])
    return [{
        "username": "474r4x14",
        "uploadSpeed": 3_630_000,
        "queueLength": 0,
        "hasFreeUploadSlot": True,
        "files": files,
    }]


# A stale transfer must not keep an old row-level waiting marker alive.  The
# next exact candidate remains subject to the normal candidate and identity
# gates, but it must be eligible for a fresh handoff.
stale_terminal_ts = time.time()
stale_retry_state = {
    "last_attempts": {
        "review-stale": {
            "status": "stale_failed_transfer_cleared",
            "ts": stale_terminal_ts,
        }
    },
    "candidate_last_attempts": {
        "old-candidate": {
            "review_id": "review-stale",
            "status": "started_waiting",
            "ts": stale_terminal_ts - 1,
        }
    },
}
require(
    not probe.auto_grab_review_has_recent_waiting_attempt(stale_retry_state, "review-stale"),
    "stale transfer failure left the review-level waiting hold active",
)
stale_retry_state["candidate_last_attempts"]["new-candidate"] = {
    "review_id": "review-stale",
    "status": "started_waiting",
    "ts": stale_terminal_ts + 1,
}
require(
    probe.auto_grab_review_has_recent_waiting_attempt(stale_retry_state, "review-stale"),
    "a newer active candidate must still block duplicate handoff after an older terminal result",
)
active_retry_state = {
    "last_attempts": {
        "review-active": {
            "status": "started_waiting",
            "ts": time.time(),
        }
    },
    "candidate_last_attempts": {
        "active-candidate": {
            "review_id": "review-active",
            "status": "started_waiting",
            "ts": time.time(),
        }
    },
}
require(
    probe.auto_grab_review_has_recent_waiting_attempt(active_retry_state, "review-active"),
    "active handoff duplicate guard was removed",
)
with tempfile.TemporaryDirectory() as state_dir:
    state_path = Path(state_dir) / "slskd-auto-grab-state.json"
    with mock.patch.object(probe, "SLSKD_AUTO_GRAB_STATE_FILE", state_path):
        require(
            probe.record_auto_grab_terminal_attempt(
                "review-real-terminal",
                {"candidate_key": "candidate-real", "filename": "issue.cbz", "username": "peer"},
                "transfer_failed",
                "resolver cleared the waiting transfer",
            ),
            "resolver terminal state was not recorded",
        )
        terminal_state = probe.load_auto_grab_state()
        require(
            terminal_state["last_attempts"]["review-real-terminal"]["status"] == "transfer_failed",
            "resolver terminal status was not persisted",
        )
        resolver_record = {
            "review_id": "review-resolver",
            "candidate_key": "candidate-resolver",
            "candidate_source": "slskd_probe",
            "series": "Descender",
            "issue": "12",
            "username": "peer",
            "filename": "Comics\\Descender\\Descender 012.cbz",
        }
        resolver_args = SimpleNamespace(live=True, probe_script=Path(probe.__file__))
        resolver_result = {"transient_failure_count": 0, "skipped": []}
        with mock.patch.object(autoresolve, "load_probe_module", return_value=probe), \
             mock.patch.object(autoresolve, "mark_manual_source_candidate_bad", return_value={"candidate_bad": True}), \
             mock.patch.object(autoresolve, "cancel_failed_slskd_transfer", return_value=None), \
             mock.patch.object(autoresolve, "run_next_slskd_autopick", return_value={"started": False}), \
             mock.patch.object(autoresolve, "auto_grab_audit"):
            autoresolve.recover_failed_waiting_candidate(
                resolver_args,
                resolver_result,
                "review-resolver",
                resolver_record,
                None,
                "SLSKD transfer failed",
            )
        resolver_state = probe.load_auto_grab_state()
        require(
            resolver_state["last_attempts"]["review-resolver"]["status"] == "transfer_failed",
            "the real resolver did not publish the terminal handoff state",
        )


responses = descender_response()
observations, observation_summary = probe.slskd_series_directory_observations(responses, max_files=160)
require(len(observations) == 1, f"expected one numbered directory: {observations}")
observation = observations[0]
require(observation["username"] == "474r4x14", "peer identity was not retained locally")
require(observation["queue_length"] == 0 and observation["has_free_upload_slot"], "peer availability evidence missing")
require(observation["file_count"] == 35, "only comic archive files should be observed")
require(not observation_summary["observation_truncated"], "fixture should fit the observation bound")

imprint_item = wanted(29)
imprint_row = {
    **observation["files"][28],
    "filename": "Comics\\Descender\\Descender 029 (Image, 2018-04).cbz",
}
with mock.patch.object(probe, "bad_candidate_match", return_value=False):
    imprint_candidate, imprint_reason = probe.series_run_candidate_for_item(imprint_row, imprint_item, observation)
require(imprint_candidate is not None, f"terminal publisher date was misread as coverage: {imprint_reason}")
imprint_gate = imprint_candidate["auto_grab"]
require("coverage_not_unit_number" not in imprint_gate.get("rejection_codes", []), imprint_gate)
require(imprint_gate.get("verdict") == "auto_grab_safe", imprint_gate)
range_candidate = {
    **imprint_row,
    "filename": "Comics\\Descender\\Descender 001-032 (Image, 2018-04).cbz",
    "series_directory_handoff": True,
    "series_directory_exact_series": True,
    "series_directory_file_count": 32,
}
range_gate = probe.auto_grab_candidate_verdict(range_candidate, imprint_item)
require(range_gate.get("verdict") == "blocked", "real issue coverage must remain blocked")
require(
    "coverage_not_unit_number" in range_gate.get("rejection_codes", [])
    or any("range" in str(reason).lower() for reason in range_gate.get("blockers", [])),
    range_gate,
)

# The observation cap is cumulative across a probe's query attempts. Raw peer,
# directory, and sibling inventory live only in the caller-owned in-memory sink.
old_observed_cap = probe.SERIES_RUN_MAX_OBSERVED_FILES
try:
    probe.SERIES_RUN_MAX_OBSERVED_FILES = 4
    query_one = [{**responses[0], "files": responses[0]["files"][:3]}]
    query_two = [{**responses[0], "files": responses[0]["files"][3:6]}]
    sink = []
    unmatched = {**wanted(1), "review_id": "unmatched-1", "series": "Unmatched", "query": "Unmatched"}
    with (
        mock.patch.object(probe, "slskd_search", side_effect=[query_one, query_two]),
        mock.patch.object(probe, "detected_staged_files", return_value=[]),
    ):
        capped_entry = probe.probe_item(
            unmatched,
            max_queries=2,
            wait_seconds=2,
            directory_observation_sink=sink,
        )
    capped_summary = capped_entry["series_directory_observation_summary"]
    require(capped_summary["observed_file_count"] == 4, capped_summary)
    require(capped_summary["observation_truncated"], "second query overflow must be truthful")
    require(sum(row["file_count"] for row in sink) <= 4, "queries exceeded the cumulative observation cap")
    serialized_entry = json.dumps(capped_entry, sort_keys=True)
    for private_value in ("474r4x14", "Comics\\\\Descender", "Descender 002"):
        require(private_value not in serialized_entry, f"raw directory observation leaked into persisted entry: {private_value}")
    require("series_directory_observations" not in capped_entry, "raw observation inventory must remain memory-only")
finally:
    probe.SERIES_RUN_MAX_OBSERVED_FILES = old_observed_cap

items = [wanted(number) for number in (1, 2, 3, 4, 5, 6, 7, 29)]
items[2]["autopilot_state"] = "downloading"
items[3]["autopilot_state"] = "verified"
cache = {}

# Legacy queue rows mirror a western issue number into ``chapter``.  The
# media classification must survive the probe cache so the authoritative
# reservation still binds the candidate as an issue rather than relabeling it
# as a chapter.
legacy_comic_item = probe.queue_source_review_item({
    "key": "queue:legacy-comic:7",
    "series": "Legacy Comic",
    "issue": "7",
    "chapter": "7",
    "media_type": "comic",
    "series_id": "series:legacy-comic",
    "queue_identity": "series:legacy-comic",
})
legacy_cached_item = probe.copy_item_context({}, legacy_comic_item)
require(legacy_cached_item.get("media_type") == "comic", legacy_cached_item)
require(
    probe.canonical_retarget_unit(legacy_cached_item) == ("issue", "7"),
    "cached western issue was relabeled as a chapter",
)

legacy_manga_volume_item = probe.queue_source_review_item({
    "key": "queue:legacy-manga:13",
    "series": "Legacy Manga",
    "query": "Legacy Manga Vol. 13",
    "issue": "13",
    "chapter": "13",
    "issue_title": "Vol. 13",
    "media_type": "manga",
    "series_id": "series:legacy-manga",
    "queue_identity": "series:legacy-manga",
})
legacy_manga_cached_item = probe.copy_item_context({}, legacy_manga_volume_item)
require(legacy_manga_cached_item.get("unit_type") == "volume", legacy_manga_cached_item)
require(legacy_manga_cached_item.get("volume_number") == "13", legacy_manga_cached_item)
require(
    probe.canonical_retarget_unit(legacy_manga_cached_item) == ("volume", "13"),
    "cached manga volume was relabeled as a chapter",
)
legacy_manga_chapter_item = probe.queue_source_review_item({
    "key": "queue:legacy-manga:chapter:13",
    "series": "Legacy Manga",
    "query": "Legacy Manga Chapter 13",
    "issue": "13",
    "chapter": "13",
    "issue_title": "Chapter 13",
    "media_type": "manga",
})
require(
    probe.canonical_retarget_unit(legacy_manga_chapter_item) == ("chapter", "13"),
    "chapter metadata was relabeled as a volume",
)
for missing_or_conflicting_chapter in (None, "12"):
    unsafe_volume_row = {
        "key": f"queue:legacy-manga:unsafe:{missing_or_conflicting_chapter}",
        "series": "Legacy Manga",
        "query": "Legacy Manga Vol. 13",
        "issue": "13",
        "issue_title": "Vol. 13",
        "media_type": "manga",
    }
    if missing_or_conflicting_chapter is not None:
        unsafe_volume_row["chapter"] = missing_or_conflicting_chapter
    unsafe_volume_item = probe.queue_source_review_item(unsafe_volume_row)
    unsafe_cached_item = probe.copy_item_context(
        {"unit_type": "volume", "volume_number": "13"},
        unsafe_volume_item,
    )
    require(
        unsafe_volume_item.get("unit_type") != "volume"
        and probe.canonical_retarget_unit(unsafe_volume_item) == ("", ""),
        f"missing/conflicting chapter alias was relabeled as a volume: {unsafe_volume_item}",
    )
    require(
        probe.canonical_retarget_unit(unsafe_cached_item) == ("", "")
        and not unsafe_cached_item.get("volume_number"),
        f"stale cached volume identity survived current queue authority: {unsafe_cached_item}",
    )


def failed_issue(_review_id, candidate):
    return "Descender 007" in str((candidate or {}).get("filename") or "")


old_issue_cap = probe.SERIES_RUN_MAX_ISSUES
old_byte_cap = probe.SERIES_RUN_MAX_BYTES
lifecycle_temp = None
old_lifecycle_globals = (
    probe.STATE_DIR, probe.INKDROP_STATE_DB, probe.SERIES_AUTOPILOT_QUEUE_FILE,
)
try:
    probe.SERIES_RUN_MAX_ISSUES = 4
    probe.SERIES_RUN_MAX_BYTES = 500 * 1024 * 1024
    with mock.patch.object(probe, "bad_candidate_match", side_effect=failed_issue):
        summary = probe.apply_series_directory_opportunities(
            [{"series_directory_observations": observations}], items, cache
        )
    require(summary["selected_issue_count"] == 4, f"issue cap was not enforced: {summary}")
    require(set(summary["selected_review_ids"]) == {"descender-1", "descender-2", "descender-5", "descender-6"}, summary)
    require("descender-3" not in cache and "descender-4" not in cache, "active/completed issues were reconsidered")
    require("descender-7" not in cache, "previously failed candidate was reconsidered")
    require(all(len(row.get("candidates") or []) == 1 for row in cache.values()), "one file should map to each wanted issue")
    require(all((row["candidates"][0].get("auto_grab") or {}).get("verdict") == "auto_grab_safe" for row in cache.values()), "handoff bypassed the existing safety verdict")
    require(all(row["candidates"][0].get("series_directory_handoff") for row in cache.values()), "handoff provenance is missing")
    require(all("series_directory" not in row["candidates"][0] for row in cache.values()), "raw directory field leaked into selected cache candidates")

    # Replay merges by stable peer/path identity; it cannot duplicate candidates.
    with mock.patch.object(probe, "bad_candidate_match", side_effect=failed_issue):
        replay = probe.apply_series_directory_opportunities(
            [{"series_directory_observations": observations}], items, cache
        )
    require(replay["selected_issue_count"] == 4, "replay should retain the same bounded opportunity")
    require(all(len(row.get("candidates") or []) == 1 for row in cache.values()), "replay duplicated transfer candidates")

    persisted_candidate = cache["descender-1"]["candidates"][0]
    raw_filename = observations[0]["files"][0]["filename"]
    raw_username = observations[0]["username"]
    persisted_json = json.dumps(cache["descender-1"], sort_keys=True)
    require(persisted_candidate["filename"] == "Descender 001 (2015).cbr", persisted_candidate)
    require("username" not in persisted_candidate, "raw peer leaked into cached handoff candidate")
    require(raw_username not in persisted_json and raw_filename not in persisted_json, "raw routing leaked into cache/status candidate")

    lifecycle_temp = tempfile.TemporaryDirectory(prefix="inkdrop-slskd-series-authoritative-")
    lifecycle_state = Path(lifecycle_temp.name)
    lifecycle_db = lifecycle_state / inkdrop_state.STATE_DB_NAME
    probe.STATE_DIR = lifecycle_state
    probe.INKDROP_STATE_DB = lifecycle_db
    probe.SERIES_AUTOPILOT_QUEUE_FILE = lifecycle_state / "series-autopilot-queue.json"
    with inkdrop_state.connect(lifecycle_db) as con:
        inkdrop_state.init_schema(con)
        con.execute(
            "insert into series(id,title,media_type,monitored,auto_grab,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
            ("series:descender", "Descender", "comic", 1, 1, 1, 1, "{}"),
        )
        for issue_number in (1, 2, 5, 6):
            issue_id = f"issue:descender:{issue_number}"
            wanted_id = f"wanted:descender:{issue_number}"
            queue_id = f"queue:descender:{issue_number}"
            issue_raw = json.dumps({"unit_type": "issue", "issue_number": str(issue_number)})
            queue_raw = json.dumps({
                "series": "Descender", "issue": str(issue_number),
                "unit_type": "issue", "issue_number": str(issue_number),
                "queue_identity": "series:descender",
            })
            con.execute(
                "insert into issues(id,series_id,issue_number,normalized_number,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
                (issue_id, "series:descender", str(issue_number), str(issue_number), 1, 1, 1, issue_raw),
            )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                (wanted_id, "series:descender", issue_id, "wanted", 1, 1, "{}"),
            )
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,issue_id,state,current_source,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
                (queue_id, wanted_id, "series:descender", issue_id, "queued", None, 1, 1, 1, queue_raw),
            )
    exported = probe.export_autopilot_queue_from_inkdrop_state("series_handoff_fixture")
    require(exported.get("ok") and exported.get("exported_count") == 4, exported)
    exported_queue = json.loads(probe.SERIES_AUTOPILOT_QUEUE_FILE.read_text(encoding="utf-8"))["items"]
    for issue_number in (1, 2, 5, 6):
        review_id = f"descender-{issue_number}"
        queue_id = f"queue:descender:{issue_number}"
        exported_row = exported_queue[queue_id]
        cache[review_id].update(exported_row)
        cache[review_id].update({
            "review_id": review_id,
            "autopilot_queue": True,
            "autopilot_queue_key": exported_row["key"],
            "queue_identity": exported_row["queue_identity"],
        })

    # Exercise the real reservation below with the same legacy alias shape
    # seen in QA: no explicit unit type, issue and chapter mirror each other,
    # and durable media metadata selects the authoritative issue identity.
    cache["descender-1"].pop("unit_type", None)
    cache["descender-1"]["issue_number"] = "1"
    cache["descender-1"]["chapter_number"] = "1"
    cache["descender-1"]["media_type"] = "comic"

    # The raw route is hydrated exactly once at the final enqueue boundary.
    enqueue_calls = []
    waiting_calls = []
    queue_attempt_calls = []
    audit_rows = []

    def enqueue_once(candidate, dry_run):
        enqueue_calls.append(dict(candidate))
        return {"enqueued": [{
            "id": "transfer-descender-1",
            "username": candidate["username"],
            "filename": candidate["filename"],
            "state": "Requested",
            "size": candidate["size"],
        }]}

    def mark_waiting(entry, candidate, dry_run=False, transfer=None, **_kwargs):
        waiting_calls.append({"candidate": dict(candidate), "transfer": dict(transfer or {}), "dry_run": dry_run})
        return {"ok": True, "result": {"record": {
            "review_id": entry["review_id"],
            "filename": candidate["filename"],
            "filename_leaf": candidate["filename"],
            "slskd_transfer_id": (transfer or {}).get("id"),
        }}}

    def record_queue(entry, candidate, status, reason="", transfer=None, **_kwargs):
        queue_attempt_calls.append({"candidate": dict(candidate), "transfer": dict(transfer or {}), "status": status})
        return {"ok": True}

    args = SimpleNamespace(auto_grab_live=True, auto_grab_dry_run=False, auto_grab_max=1)
    with (
        mock.patch.object(probe, "waiting_review_ids", return_value=set()),
        mock.patch.object(probe, "entry_has_unfailed_detected_file", return_value=False),
        mock.patch.object(probe, "bad_candidate_match", return_value=False),
        mock.patch.object(probe, "load_auto_grab_state", return_value={}),
        mock.patch.object(probe, "save_auto_grab_state", return_value=None),
        mock.patch.object(probe, "active_auto_grab_user_load", return_value={}),
        mock.patch.object(probe, "slskd_existing_download", return_value={}),
        mock.patch.object(probe, "slskd_enqueue_candidate", side_effect=enqueue_once),
        mock.patch.object(probe, "mark_manual_source_waiting_local", side_effect=mark_waiting),
        mock.patch.object(probe, "auto_grab_audit", side_effect=lambda event, **payload: audit_rows.append({"event": event, **payload})),
        mock.patch.object(probe, "log", return_value=None),
    ):
        enqueue_outcome = probe.run_auto_grab(args, {"items": {"descender-1": cache["descender-1"]}})
    require(len(enqueue_calls) == 1, f"handoff must enqueue once: {enqueue_calls}")
    require(enqueue_calls[0]["username"] == raw_username and enqueue_calls[0]["filename"] == raw_filename, enqueue_calls)
    require(len(waiting_calls) == 2, waiting_calls)
    require(
        all(call["candidate"]["username"] == raw_username and call["candidate"]["filename"] == raw_filename for call in waiting_calls),
        "waiting persistence did not receive the hydrated route",
    )
    require(
        waiting_calls[-1]["transfer"]["username"] == raw_username
        and waiting_calls[-1]["transfer"]["filename"] == raw_filename,
        "persisted waiting transfer was not bound to the hydrated route",
    )
    public_execution = json.dumps({
        "outcome": enqueue_outcome,
        "queue_attempts": queue_attempt_calls,
        "audit": audit_rows,
    }, sort_keys=True)
    require(raw_username not in public_execution and raw_filename not in public_execution, "raw route escaped the enqueue boundary")
    require(enqueue_outcome["started_count"] == 1, enqueue_outcome)
    first_row = enqueue_outcome["rows"][0]
    require(first_row["candidate_reservation"].get("created"), first_row)
    require(first_row["candidate_reservation"].get("decision") == "authorize_enqueue", first_row)
    require(first_row["durable_queue_gate"].get("claimed"), first_row)
    require((first_row.get("candidate_transition") or {}).get("ok"), first_row)
    with inkdrop_state.connect_read(lifecycle_db) as con:
        first_tasks = con.execute(
            "select id,queue_id,status,state,external_id,candidate_identity,raw_json from download_tasks where queue_id='queue:descender:1'"
        ).fetchall()
        first_claims = con.execute("select count(*) from queue_claims where queue_id='queue:descender:1'").fetchone()[0]
    require(len(first_tasks) == 1, f"authoritative handoff created {len(first_tasks)} tasks")
    require(first_tasks[0]["id"] == first_row["candidate_transition"]["download_task_id"], first_tasks[0])
    require(first_tasks[0]["status"] == "started_waiting" and first_tasks[0]["state"] == "downloading", first_tasks[0])
    require(first_tasks[0]["external_id"] == "transfer-descender-1" and first_tasks[0]["candidate_identity"], first_tasks[0])
    first_task_raw = json.loads(first_tasks[0]["raw_json"])
    require(
        first_task_raw.get("candidate_instance_identity")
        and first_task_raw.get("candidate_locator_digest")
        and first_task_raw.get("exact_unit_type") == "issue"
        and first_task_raw.get("exact_unit_number") == "0001",
        first_task_raw,
    )
    require(first_claims == 0, "handoff claim survived its durable transition")
    require(
        persisted_candidate["series_directory_handoff_token"] not in probe.SERIES_RUN_EPHEMERAL_CANDIDATES,
        "raw routing survived the completed enqueue attempt",
    )
    with mock.patch.object(probe, "bad_candidate_match", side_effect=failed_issue):
        probe.apply_series_directory_opportunities(
            [{"series_directory_observations": observations}], items, cache
        )
    cache["descender-1"].update(exported_queue["queue:descender:1"])
    cache["descender-1"].update({
        "review_id": "descender-1", "autopilot_queue": True,
        "autopilot_queue_key": "queue:descender:1", "queue_identity": "series:descender",
    })
    with (
        mock.patch.object(probe, "waiting_review_ids", return_value=set()),
        mock.patch.object(probe, "entry_has_unfailed_detected_file", return_value=False),
        mock.patch.object(probe, "bad_candidate_match", return_value=False),
        mock.patch.object(probe, "load_auto_grab_state", return_value={}),
        mock.patch.object(probe, "save_auto_grab_state", return_value=None),
        mock.patch.object(probe, "active_auto_grab_user_load", return_value={}),
        mock.patch.object(probe, "slskd_existing_download", side_effect=AssertionError("durable replay must not re-probe provider ownership")),
        mock.patch.object(probe, "slskd_enqueue_candidate", side_effect=AssertionError("durable replay must not enqueue")),
        mock.patch.object(probe, "mark_manual_source_waiting_local", side_effect=mark_waiting),
        mock.patch.object(probe, "auto_grab_audit", return_value=None),
        mock.patch.object(probe, "log", return_value=None),
    ):
        replay = probe.run_auto_grab(args, {"items": {"descender-1": cache["descender-1"]}})
    replay_row = replay["rows"][0]
    require(replay_row["status"] == "already_downloading", replay)
    require(replay_row["candidate_reservation"].get("idempotent"), replay_row)
    require(replay_row["candidate_reservation"].get("decision") == "reuse_existing", replay_row)
    require((replay_row.get("candidate_transition") or {}).get("ok"), replay_row)
    require(len(enqueue_calls) == 1, f"idempotent replay enqueued again: {enqueue_calls}")
    with inkdrop_state.connect_read(lifecycle_db) as con:
        replay_task_count = con.execute("select count(*) from download_tasks where queue_id='queue:descender:1'").fetchone()[0]
        replay_claim_count = con.execute("select count(*) from queue_claims where queue_id='queue:descender:1'").fetchone()[0]
    require(replay_task_count == 1 and replay_claim_count == 0, "idempotent replay changed task/claim ownership")

    with mock.patch.object(probe, "bad_candidate_match", side_effect=failed_issue):
        probe.apply_series_directory_opportunities(
            [{"series_directory_observations": observations}], items, cache
        )

    for missing_field in ("queue_key", "queue_identity"):
        missing_entry = dict(cache["descender-2"])
        missing_entry.update({
            "review_id": "descender-2", "autopilot_queue": True,
            "autopilot_queue_key": "queue:descender:2", "key": "queue:descender:2",
            "queue_identity": "series:descender",
        })
        if missing_field == "queue_key":
            for field in ("autopilot_queue_key", "queue_key", "key"):
                missing_entry.pop(field, None)
        else:
            missing_entry.pop("queue_identity", None)
        with (
            mock.patch.object(probe, "waiting_review_ids", return_value=set()),
            mock.patch.object(probe, "entry_has_unfailed_detected_file", return_value=False),
            mock.patch.object(probe, "bad_candidate_match", return_value=False),
            mock.patch.object(probe, "load_auto_grab_state", return_value={}),
            mock.patch.object(probe, "save_auto_grab_state", return_value=None),
            mock.patch.object(probe, "active_auto_grab_user_load", return_value={}),
            mock.patch.object(probe, "slskd_existing_download", side_effect=AssertionError("unbound autopilot entry must not probe ownership")),
            mock.patch.object(probe, "slskd_enqueue_candidate", side_effect=AssertionError("unbound autopilot entry must not enqueue")),
            mock.patch.object(probe, "mark_manual_source_waiting_local", side_effect=mark_waiting),
            mock.patch.object(probe, "auto_grab_audit", return_value=None),
            mock.patch.object(probe, "log", return_value=None),
        ):
            missing_result = probe.run_auto_grab(args, {"items": {"descender-2": missing_entry}})
        missing_row = missing_result["rows"][0]
        require(missing_row["status"] == "skipped_durable_queue_gate", missing_result)
        require((missing_row.get("candidate_reservation") or {}).get("decision") == "invalid_binding", missing_row)
        require(missing_row.get("retry_eligible") and missing_row.get("manual_review_required"), missing_row)
        require(missing_result["transient_error_count"] == 1, missing_result)
        with inkdrop_state.connect_read(lifecycle_db) as con:
            missing_tasks = con.execute("select count(*) from download_tasks where queue_id='queue:descender:2'").fetchone()[0]
        require(missing_tasks == 0, f"{missing_field} failure created a task")
        if missing_field == "queue_key":
            with mock.patch.object(probe, "bad_candidate_match", side_effect=failed_issue):
                probe.apply_series_directory_opportunities(
                    [{"series_directory_observations": observations}], items, cache
                )
    with mock.patch.object(probe, "bad_candidate_match", side_effect=failed_issue):
        probe.apply_series_directory_opportunities(
            [{"series_directory_observations": observations}], items, cache
        )

    def run_private_boundary_case(review_id, args_value, enqueue_side_effect):
        local_audits = []
        local_queue_attempts = []
        with (
            mock.patch.object(probe, "waiting_review_ids", return_value=set()),
            mock.patch.object(probe, "entry_has_unfailed_detected_file", return_value=False),
            mock.patch.object(probe, "bad_candidate_match", return_value=False),
            mock.patch.object(probe, "load_auto_grab_state", return_value={}),
            mock.patch.object(probe, "save_auto_grab_state", return_value=None),
            mock.patch.object(probe, "active_auto_grab_user_load", return_value={}),
            mock.patch.object(probe, "slskd_existing_download", return_value={}),
            mock.patch.object(probe, "slskd_enqueue_candidate", side_effect=enqueue_side_effect),
            mock.patch.object(probe, "mark_manual_source_waiting_local", side_effect=mark_waiting),
            mock.patch.object(probe, "record_slskd_queue_attempt", side_effect=lambda *a, **k: local_queue_attempts.append({"candidate": dict(a[1]), "transfer": dict(k.get("transfer") or {})}) or {"ok": True}),
            mock.patch.object(probe, "auto_grab_audit", side_effect=lambda event, **payload: local_audits.append({"event": event, **payload})),
            mock.patch.object(probe, "log", return_value=None),
        ):
            result = probe.run_auto_grab(args_value, {"items": {review_id: cache[review_id]}})
        public = json.dumps({"result": result, "audits": local_audits, "attempts": local_queue_attempts}, sort_keys=True)
        require(raw_username not in public and "Comics\\\\Descender" not in public, "raw route leaked on dry-run/exception path")
        return result

    dry_raw = dict(probe.SERIES_RUN_EPHEMERAL_CANDIDATES[cache["descender-2"]["candidates"][0]["series_directory_handoff_token"]])
    dry_calls = []

    def dry_enqueue(candidate, dry_run):
        dry_calls.append(dict(candidate))
        return {"enqueued": [{"id": "dry-transfer", "username": candidate["username"], "filename": candidate["filename"], "state": "Requested"}]}

    dry_args = SimpleNamespace(auto_grab_live=False, auto_grab_dry_run=True, auto_grab_max=1)
    dry_result = run_private_boundary_case("descender-2", dry_args, dry_enqueue)
    require(
        len(dry_calls) == 1
        and dry_calls[0]["username"] == dry_raw["username"]
        and dry_calls[0]["filename"] == dry_raw["filename"],
        "dry-run did not hydrate exactly once at enqueue",
    )
    require(dry_result["rows"][0]["status"] == "dry_run_safe", dry_result)
    require(cache["descender-2"]["candidates"][0]["series_directory_handoff_token"] not in probe.SERIES_RUN_EPHEMERAL_CANDIDATES, "dry-run retained raw routing")

    with mock.patch.object(probe, "bad_candidate_match", side_effect=failed_issue):
        probe.apply_series_directory_opportunities(
            [{"series_directory_observations": observations}], items, cache
        )

    error_raw = dict(probe.SERIES_RUN_EPHEMERAL_CANDIDATES[cache["descender-5"]["candidates"][0]["series_directory_handoff_token"]])
    cache["descender-5"].update(exported_queue["queue:descender:5"])
    cache["descender-5"].update({
        "review_id": "descender-5", "autopilot_queue": True,
        "autopilot_queue_key": "queue:descender:5", "queue_identity": "series:descender",
    })
    error_calls = []

    def failing_enqueue(candidate, dry_run):
        error_calls.append(dict(candidate))
        raise RuntimeError(f"private failure {candidate['username']} {candidate['filename']}")

    error_result = run_private_boundary_case("descender-5", args, failing_enqueue)
    require(
        len(error_calls) == 1
        and error_calls[0]["username"] == error_raw["username"]
        and error_calls[0]["filename"] == error_raw["filename"],
        "exception path did not attempt exact route once",
    )
    require(error_result["rows"][0]["error"] == "SLSKD handoff API request failed", error_result)
    require(cache["descender-5"]["candidates"][0]["series_directory_handoff_token"] not in probe.SERIES_RUN_EPHEMERAL_CANDIDATES, "exception retained raw routing")

    probe.SERIES_RUN_MAX_ISSUES = 25
    probe.SERIES_RUN_MAX_BYTES = 70 * 1024 * 1024
    byte_cache = {}
    with mock.patch.object(probe, "bad_candidate_match", return_value=False):
        byte_summary = probe.apply_series_directory_opportunities(
            [{"series_directory_observations": observations}], [wanted(1), wanted(2), wanted(5)], byte_cache
        )
    require(byte_summary["selected_bytes"] <= probe.SERIES_RUN_MAX_BYTES, "byte cap was exceeded")
    require(any(row["reason"] == "series-run byte cap reached" for row in byte_summary["skipped_reasons"]), byte_summary)

    deadline_cache = {}
    with mock.patch.object(probe, "bad_candidate_match", return_value=False):
        deadline_summary = probe.apply_series_directory_opportunities(
            [{"series_directory_observations": observations}], [wanted(1)], deadline_cache, deadline=time.time() - 1
        )
    require(deadline_summary["deadline_exhausted"] and not deadline_cache, "expired deadline must fail closed")

    unrelated = [{
        **observation,
        "files": [
            {**observation["files"][0], "filename": "Comics/Descender/Ascender 001 (2019).cbr"},
            {**observation["files"][0], "filename": "Comics/Descender/Descender The Machine 001.cbr"},
            {**observation["files"][0], "filename": "Comics/Descender/Descender Covers 001.cbz", "size": 8 * 1024 * 1024},
        ],
        "file_count": 3,
    }]
    safe_cache = {}
    with mock.patch.object(probe, "bad_candidate_match", return_value=False):
        no_grab = probe.apply_series_directory_opportunities(
            [{"series_directory_observations": unrelated}], [wanted(1)], safe_cache
        )
    require(no_grab["selected_issue_count"] == 0 and not safe_cache, "Ascender/subseries/covers must fail closed")
finally:
    probe.SERIES_RUN_MAX_ISSUES = old_issue_cap
    probe.SERIES_RUN_MAX_BYTES = old_byte_cap
    probe.STATE_DIR, probe.INKDROP_STATE_DB, probe.SERIES_AUTOPILOT_QUEUE_FILE = old_lifecycle_globals
    if lifecycle_temp is not None:
        lifecycle_temp.cleanup()


accepted = "Descender 029 (Image, 2018-04).cbz"
require(not completed.related_subseries_source_blocker("Descender", accepted), "Image imprint/date was misread during completed import")
require(not acquire.related_subseries_title_blocker("Descender", accepted), "Image imprint/date was misread during acquisition")
for rejected in (
    "Descender 029 Image.cbz",
    "Descender The Machine 029.cbz",
    "Descender 029 (Image, 2018-04) Ascender.cbz",
    "Descender 029 (Image, 2018-04) The Machine.cbz",
    "Descender 029 (Image, 2018-04) Image Comics Presents.cbz",
    "Descender 029 (Image, 2018-04) Covers.cbz",
    "Descender 029 The Machine (Image, 2018-04).cbz",
    "Descender 029 Covers (Image, 2018-04).cbz",
    "Descender Image Comics Presents 029 (Image, 2018-04).cbz",
    "Ascender 029 (Image, 2018-04).cbz",
):
    require(completed.related_subseries_source_blocker("Descender", rejected), f"narrow imprint exception widened: {rejected}")
    require(acquire.related_subseries_title_blocker("Descender", rejected), f"acquisition exception widened: {rejected}")

print("inkdrop SLSKD numbered-directory handoff smoke: PASS")
