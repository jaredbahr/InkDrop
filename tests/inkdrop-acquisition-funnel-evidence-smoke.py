#!/usr/bin/env python3
"""Regression coverage for acquisition-funnel evidence semantics."""

import json
import sqlite3
import tempfile
from pathlib import Path

from core import inkdrop_acquisition_funnel as funnel
from core import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def attempt(*, detected=1, safe=1, concrete=0):
    return {
        "provider_called_count": 1,
        "candidate_detected_count": detected,
        "safe_candidate_count": safe,
        "concrete_safe_candidate_count": concrete,
        "deferred_count": 0,
    }


queue = {"queue_id": "queue-fixture"}

# Queue-activity/inherited availability counters are telemetry, not acceptance.
telemetry = funnel._concrete_safe_candidate_count(
    4, "", "", "", json.dumps({"safe_candidate_count": 4}), 0, "observed", "queue_activity"
)
require(telemetry == 0, "availability counter advanced safe acceptance")
require(
    funnel._first_divergence({}, queue, attempt(safe=4, concrete=telemetry), None, None, None)
    == "candidate_not_safely_accepted",
    "telemetry-only row did not stop at candidate_not_safely_accepted",
)

# A sent/provider row without a replay-capable locator, client, or linked task is
# still telemetry even when it carries an availability count and URL hash.
sent_without_replay = funnel._concrete_safe_candidate_count(
    1, "candidate:display-only", "sha256:diagnostic-only", "", json.dumps({"status": "sent"}), 0, "sent", "prowlarr"
)
require(sent_without_replay == 0, "unreplayable sent row counted as safe")

# Concrete candidate identity is durable candidate evidence.
concrete_identity = funnel._concrete_safe_candidate_count(1, "candidate:fixture", "", "", "{}", 0, "accepted", "slskd")
require(concrete_identity == 1, "concrete candidate identity was rejected")
require(
    funnel._first_divergence({}, queue, attempt(concrete=concrete_identity), None, None, None)
    == "accepted_not_handed_off",
    "concrete safe candidate did not reach accepted_not_handed_off",
)

# An irreversible URL hash is identity correlation, not a replay-ready locator.
hash_only = funnel._concrete_safe_candidate_count(
    1, "", "sha256:fixture", "sabnzbd", "{}", 0, "sent", "prowlarr"
)
require(hash_only == 0, "hash-only sent row counted as replay-ready")

# Raw locator plus explicit client and linked-task identity are independently
# valid handoff-ready evidence, but neither fabricates an acknowledged handoff.
locator = funnel._concrete_safe_candidate_count(
    1,
    "",
    "sha256:fixture",
    "sabnzbd",
    json.dumps({"candidate": {"download_url": "https://fixture.invalid/item", "download_client": "sabnzbd"}}),
    0,
    "sent",
    "prowlarr",
)
linked = funnel._concrete_safe_candidate_count(1, "", "", "", "{}", 1, "sent", "prowlarr")
require(locator == 1 and linked == 1, "handoff-ready evidence was rejected")

# Nested task seeds obey the same replay boundary: a URL hash is not a task
# identity, while a concrete candidate or external client ID plus client is.
nested_hash_only = funnel._concrete_safe_candidate_count(
    1,
    "",
    "",
    "",
    json.dumps({"download_task_seed": {"download_url_hash": "sha256:nested", "download_client": "sabnzbd"}}),
    0,
    "sent",
    "prowlarr",
)
nested_candidate = funnel._concrete_safe_candidate_count(
    1,
    "",
    "",
    "",
    json.dumps({"download_task_seed": {"candidate_identity": "candidate:nested", "download_client": "sabnzbd"}}),
    0,
    "sent",
    "prowlarr",
)
nested_external = funnel._concrete_safe_candidate_count(
    1,
    "",
    "",
    "",
    json.dumps({"raw": {"download_task_seed": {"external_id": "client-job:nested", "download_client": "sabnzbd"}}}),
    0,
    "sent",
    "prowlarr",
)
require(nested_hash_only == 0, "nested hash-only seed counted as replay-ready")
require(nested_candidate == 1 and nested_external == 1, "valid nested replay evidence was rejected")

# An actual client acknowledgement remains safe and handed off even when the
# source-attempt row contains no safe counter or concrete-candidate fields.
download = {
    "acknowledged_count": 1,
    "active_acknowledged_count": 1,
    "retryable_failed_count": 0,
    "retired_stale_count": 0,
    "transfer_complete": 0,
}
require(
    funnel._first_divergence({}, queue, attempt(detected=0, safe=0, concrete=0), download, None, None)
    == "handoff_active_transfer_pending",
    "actual acknowledged handoff regressed",
)


def primary(*, queue_row=queue, attempt_row=None, download_row=None, import_row=None, media=None):
    return funnel._primary_recovery_bucket(
        {}, queue_row, attempt_row, download_row, import_row, media, 100.0
    )


# The granular dashboard is disjoint and downstream durable evidence wins.
require(primary(attempt_row={"attempt_count": 0}) == "never_searched", "never-searched truth regressed")
require(
    primary(queue_row={"state": "queued", "retry_after": 50}, attempt_row={"attempt_count": 1})
    == "due_for_search",
    "due retry was not separated from never searched",
)
require(primary(attempt_row={"attempt_count": 1}) == "provider_planned", "provider plan truth regressed")
require(
    primary(attempt_row={"attempt_count": 1, "provider_in_progress_count": 1}) == "provider_called",
    "in-progress provider call truth regressed",
)
require(
    primary(attempt_row={"attempt_count": 2, "provider_result_count": 1, "timeout_count": 1})
    == "provider_completed_with_results",
    "partial child-indexer success was erased by sibling timeout",
)
require(
    primary(attempt_row={"attempt_count": 1, "zero_result_count": 1})
    == "provider_completed_with_zero_results",
    "explicit zero-result completion regressed",
)
require(primary(attempt_row={"attempt_count": 1, "timeout_count": 1}) == "provider_timed_out", "timeout truth regressed")
require(primary(attempt_row={"attempt_count": 1, "failure_count": 1}) == "provider_failed", "failure truth regressed")
require(
    primary(attempt_row={"attempt_count": 1, "malformed_count": 1}) == "malformed_provider_response",
    "malformed response truth regressed",
)
require(
    primary(attempt_row={"attempt_count": 1, "provider_result_count": 1, "normalized_count": 1})
    == "results_normalized",
    "normalized result truth regressed",
)
require(
    primary(attempt_row={"attempt_count": 1, "provider_result_count": 1, "all_candidates_rejected": 1})
    == "all_candidates_rejected",
    "all-candidates-rejected truth regressed",
)
require(
    primary(attempt_row={"attempt_count": 1, "concrete_safe_candidate_count": 1})
    == "safe_candidate_available",
    "safe candidate truth regressed",
)
require(
    primary(attempt_row={"attempt_count": 1, "candidate_selected_count": 1}) == "candidate_selected",
    "selection truth regressed",
)
require(primary(download_row={"handoff_attempted_count": 1}) == "handoff_attempted", "handoff attempt truth regressed")
require(primary(download_row={"acknowledged_count": 1}) == "handoff_acknowledged", "handoff ack truth regressed")
require(primary(download_row={"active_acknowledged_count": 1}) == "transfer_active", "active transfer truth regressed")
require(primary(download_row={"stalled_count": 1}) == "transfer_stalled", "stalled transfer truth regressed")
require(primary(download_row={"transfer_complete": 1}) == "transfer_completed", "transfer completion truth regressed")
require(primary(download_row={"artifact_missing_count": 1}) == "artifact_missing", "missing artifact truth regressed")
require(primary(download_row={"artifact_rejected_count": 1}) == "artifact_rejected", "rejected artifact truth regressed")
require(primary(download_row={"artifact_quarantined_count": 1}) == "artifact_quarantined", "quarantine truth regressed")
require(primary(download_row={"ready_to_import_count": 1}) == "ready_to_import", "ready-to-import truth regressed")
require(primary(import_row={"imported_count": 1}) == "imported", "import truth regressed")
require(
    primary(import_row={"imported_count": 1, "reader_scan_pending": 1}) == "reader_scan_pending",
    "reader scan pending truth regressed",
)
require(primary(import_row={"reader_visible": 1}) == "reader_visible", "reader visibility truth regressed")
require(
    primary(import_row={"reader_visible": 1, "completion_recorded": 1}) == "completion_recorded",
    "completion-recorded truth regressed",
)


def real_schema_sequence():
    with tempfile.TemporaryDirectory(prefix="inkdrop-funnel-evidence-", ignore_cleanup_errors=True) as temp_dir:
        db_path = Path(temp_dir) / "state.sqlite3"
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        inkdrop_state.init_schema(con)
        now = 100.0
        for suffix in ("preserved", "telemetry"):
            series_id = f"series-{suffix}"
            issue_id = f"issue-{suffix}"
            wanted_id = f"wanted-{suffix}"
            queue_id = f"queue-{suffix}"
            con.execute(
                "insert into series(id,title,media_type,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                (series_id, suffix, "comic", 1, now, now, "{}"),
            )
            con.execute(
                "insert into issues(id,series_id,issue_number,title,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
                (issue_id, series_id, "1", suffix, 1, now, now, "{}"),
            )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?)",
                (wanted_id, series_id, issue_id, "in_progress", now, now, "{}"),
            )
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,issue_id,state,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?)",
                (queue_id, wanted_id, series_id, issue_id, "queued", 1, now, now, "{}"),
            )
        con.execute(
            """insert into source_attempts(
                 id,queue_id,wanted_id,series_id,issue_id,source,provider_id,lifecycle_phase,status,
                 candidate_identity,started_at,completed_at,raw_json
               ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "accepted-t10", "queue-preserved", "wanted-preserved", "series-preserved", "issue-preserved",
                "slskd", "slskd", "manual_review", "accepted", "candidate:durable", 10, 10,
                json.dumps({"candidate_count": 1, "safe_candidate_count": 1}),
            ),
        )
        for suffix in ("preserved", "telemetry"):
            con.execute(
                """insert into source_attempts(
                     id,queue_id,wanted_id,series_id,issue_id,source,provider_id,lifecycle_phase,status,
                     started_at,completed_at,raw_json
                   ) values(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"telemetry-{suffix}-t20", f"queue-{suffix}", f"wanted-{suffix}", f"series-{suffix}",
                    f"issue-{suffix}", "queue_activity", "queue_activity", "observed", "observed", 20, 20,
                    json.dumps({"candidate_count": 1, "safe_candidate_count": 1}),
                ),
            )
        con.commit()
        con.close()
        report = funnel.build_missing_backlog_accounting(db_path, cohort_size=2, now=now)
        divergence = report["aggregate"]["first_divergence"]
        require(divergence.get("accepted_not_handed_off") == 1, divergence)
        require(divergence.get("queued_provider_not_called") == 1, divergence)
        buckets = report["aggregate"]["primary_loss_buckets"]
        require(sum(buckets.values()) == 2, buckets)
        require(buckets["safe_candidate_available"] == 1, buckets)
        require(buckets["never_searched"] == 1, buckets)
        recovery = report["aggregate"]["recovery_cohort"]
        require(recovery["size"] == 2, recovery)
        require(sum(recovery["by_stratum"].values()) == 2, recovery)
        private_recovery = report["private_recovery_cohort"]
        require(len({row["wanted_id"] for row in private_recovery}) == 2, private_recovery)


real_schema_sequence()

print("acquisition funnel evidence smoke: PASS")
