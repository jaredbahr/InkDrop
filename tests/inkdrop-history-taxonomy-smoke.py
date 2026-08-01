#!/usr/bin/env python3
"""Deterministic History taxonomy, counts, chronology, and replay coverage."""

import json
import contextlib
import sqlite3
import tempfile
import time
from pathlib import Path

import inkdrop_state


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "web" / "tests" / "fixtures" / "history-taxonomy-v2.json"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def insert_attempt(con, event):
    attempt_id = event["attempt_id"]
    source = event.get("typed_source", event.get("source"))
    client = event.get("typed_client", event.get("client"))
    provider = event.get("typed_provider", event.get("client"))
    protocol = event.get("typed_protocol", event.get("client"))
    status = event.get("typed_status", event.get("status"))
    lifecycle_phase = event.get("typed_lifecycle_phase", event.get("lifecycle_phase"))
    con.execute(
        """
        insert into source_attempts(
            id, source, provider, protocol, download_client, status, lifecycle_phase,
            started_at, completed_at, raw_json
        ) values(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attempt_id, source, provider, protocol, client,
            status, lifecycle_phase, event["at"], event["at"], json.dumps(event),
        ),
    )
    linked_task_id = event.get("linked_task_id")
    if linked_task_id:
        con.execute(
            """
            insert into download_tasks(
                id, source_attempt_id, source, provider, protocol, download_client,
                status, state, started_at, updated_at, completed_at, raw_json
            ) values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                linked_task_id, attempt_id, event.get("source"), event.get("client"), event.get("client"),
                event.get("client"), event.get("status"), "failed", event["at"], event["at"], event["at"],
                json.dumps(event),
            ),
        )


def insert_task(con, event):
    con.execute(
        """
        insert into download_tasks(
            id, source, provider, protocol, download_client, status, state,
            started_at, updated_at, completed_at, raw_json
        ) values(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event["task_id"], event.get("client"), event.get("client"), event.get("client"), event.get("client"),
            event.get("status"), event.get("state"), event["at"], event["at"],
            event["at"] if event.get("state") not in {"queued", "downloading"} else None, json.dumps(event),
        ),
    )


def insert_history(con, event):
    record = event["record"]
    entity_id = event.get("task_id") or event.get("attempt_id") or event["id"]
    source = event.get("history_source") if "history_source" in event else (event.get("source") or event.get("client") or "importer")
    raw = dict(event.get("history_raw")) if "history_raw" in event else dict(event)
    if "history_raw" not in event:
        if record == "download_task":
            raw.update({"status": event.get("status"), "state": event.get("state"), "download_client": event.get("client")})
        else:
            raw.update({"status": event.get("status"), "lifecycle_phase": event.get("lifecycle_phase"), "download_client": event.get("client")})
    con.execute(
        """
        insert into history_events(
            id, entity_type, entity_id, event_type, source, message,
            outcome, display_phase, created_at, raw_json
        ) values(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event["id"], record, entity_id,
            "import_completed" if record == "import_result" else ("download_task" if record == "download_task" else "source_attempt"),
            source, f"fixture {event['id']}", None, None, event["at"], json.dumps(raw),
        ),
    )


def main():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    require(fixture.get("schema") == "inkdrop.history_taxonomy_fixture.v2", "fixture schema changed")
    with tempfile.TemporaryDirectory(prefix="inkdrop-history-taxonomy-", ignore_cleanup_errors=True) as temp:
        db = Path(temp) / inkdrop_state.STATE_DB_NAME
        inkdrop_state.ensure_schema(db)
        with inkdrop_state.connect(db) as con:
            for event in fixture["events"]:
                if event["record"] == "source_attempt":
                    insert_attempt(con, event)
                elif event["record"] == "download_task":
                    insert_task(con, event)
                insert_history(con, event)

            con.executemany(
                """
                insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                [
                    ("h-verify-importer", "source_attempt", "verify:importer", "source_attempt", "importer", "Importer verified release", 91, "{}"),
                    ("h-verify-kavita", "source_attempt", "verify:kavita", "source_attempt", "kavita", "Reader visibility observed", 92, "{}"),
                    ("h-verify-queue", "queue_item", "verify:queue", "queue_event", "inkdrop", "Kavita verified issue visibility", 93, "{}"),
                ],
            )

            duplicate_task = {
                "id": "dt-duplicate", "source": "slskd", "provider": "peer", "protocol": "slskd",
                "download_client": "slskd", "status": "transfer_failed", "state": "failed",
                "started_at": 110.0, "updated_at": 110.0, "completed_at": 110.0,
                "raw_json": json.dumps({"status": "transfer_failed", "state": "failed"}),
            }
            insert_task(
                con,
                {"task_id": "dt-duplicate", "client": "slskd", "status": "transfer_failed", "state": "failed", "at": 110.0},
            )
            inkdrop_state.record_download_task_history_event(con, duplicate_task)
            inkdrop_state.record_download_task_history_event(con, duplicate_task)
            duplicate_count = con.execute(
                "select count(*) from history_events where entity_type='download_task' and entity_id='dt-duplicate'"
            ).fetchone()[0]
            require(duplicate_count == 1, "replayed terminal download event duplicated history")
            con.execute("insert into series(id,title,created_at,updated_at) values('series:lifecycle','Lifecycle Fixture',1,1)")
            con.execute(
                "update history_events set series_id='series:lifecycle' where id in ('h-slskd-live','h-import')"
            )

        all_rows = inkdrop_state.recent_history(db, 100, history_filter="all")
        activity_rows = inkdrop_state.recent_history(db, 100, history_filter="activity")
        download_rows = inkdrop_state.recent_history(db, 100, history_filter="downloads")
        import_rows = inkdrop_state.recent_history(db, 100, history_filter="imports")
        by_id = {row["id"]: row for row in all_rows}
        expected_download_ids = {
            "h-slskd-stale", "h-torrent-complete", "h-torrent-importing", "h-usenet-failed",
            "h-legacy-slskd-failed", "h-sparse-legacy-slskd-failed", "h-empty-legacy-slskd-failed",
            "h-whitespace-legacy-slskd-failed", "h-direct-success",
        }
        duplicate_id = next(row["id"] for row in all_rows if row.get("entity_id") == "dt-duplicate")
        expected_download_ids.add(duplicate_id)
        actual_download_ids = {row["id"] for row in download_rows}
        require(
            actual_download_ids == expected_download_ids,
            f"Downloads did not contain exactly terminal download outcomes: actual={sorted(actual_download_ids)} expected={sorted(expected_download_ids)}",
        )
        require(by_id["h-slskd-live"]["download_outcome_terminal"] is False, "live SLSKD transfer entered Downloads")
        require(by_id["h-torrent-queued"]["download_outcome_terminal"] is False, "queued torrent attempt entered Downloads")
        require(by_id["h-usenet-live"]["download_outcome_terminal"] is False, "active Usenet transfer entered Downloads")
        require(by_id["h-direct-live"]["download_outcome_terminal"] is False, "active direct attempt entered Downloads")
        require(by_id["h-late-linked-failure"]["download_outcome_terminal"] is False, "late linked source-attempt failure duplicated its download task outcome")
        require(by_id["h-legacy-slskd-failed"]["download_record_type"] == "legacy_source_attempt", "legacy terminal source attempt lost compatibility classification")
        require(
            by_id["h-sparse-legacy-slskd-failed"]["download_record_type"] == "legacy_source_attempt"
            and by_id["h-sparse-legacy-slskd-failed"]["download_outcome"] == "failed",
            "sparse typed legacy source attempt diverged from Downloads taxonomy",
        )
        require(
            by_id["h-empty-legacy-slskd-failed"]["download_record_type"] == "legacy_source_attempt"
            and by_id["h-empty-legacy-slskd-failed"]["download_outcome"] == "failed",
            "empty-string legacy JSON blocked typed Downloads fallback",
        )
        require(
            by_id["h-whitespace-legacy-slskd-failed"]["download_record_type"] == "legacy_source_attempt"
            and by_id["h-whitespace-legacy-slskd-failed"]["download_outcome"] == "failed",
            "tab/newline legacy fields diverged between Python and SQL whitespace normalization",
        )
        require(by_id["h-import"]["history_view_bucket"] == "imports" and {row["id"] for row in import_rows} == {"h-import"}, "imports were not kept separate")
        require(all(row.get("history_taxonomy_version") == 2 for row in all_rows), "taxonomy version was not dual-emitted")
        require(
            {row["id"] for row in activity_rows}.issuperset({
                "h-slskd-live", "h-torrent-complete", "h-torrent-importing",
                "h-usenet-live", "h-import", "h-verify-importer", "h-verify-kavita", "h-verify-queue",
            }),
            f"default lifecycle History lost download/import rows: {sorted(row['id'] for row in activity_rows)}",
        )
        require(
            not ({"h-torrent-queued", "h-direct-live", "h-late-linked-failure"} & {row["id"] for row in activity_rows}),
            "default lifecycle History leaked source-attempt diagnostics",
        )
        require([row["created_at"] for row in download_rows] == sorted((row["created_at"] for row in download_rows), reverse=True), "Downloads chronology changed")

        lifecycle = inkdrop_state.history_state_view(db, limit=100)
        lifecycle_ids = {row["id"] for row in lifecycle["rows"]}
        require(lifecycle["history_filter"] == "activity", "default History compatibility filter changed")
        require(lifecycle["history_scope"] == "download_import_lifecycle", "default History omitted lifecycle scope")
        require("h-import" in lifecycle_ids and "h-slskd-live" in lifecycle_ids, "default History lost import/download lifecycle rows")
        require(
            {"h-verify-importer", "h-verify-kavita", "h-verify-queue"}.issubset(lifecycle_ids),
            "SQL lifecycle predicate diverged from Python verification classification",
        )
        require("h-late-linked-failure" not in lifecycle_ids, "default History retained a linked source-attempt duplicate")
        require(next(item for item in lifecycle["related_views"] if item["view"] == "source_memory")["label"] == "Blocklist", "History did not expose the bad-release blocklist view")
        focused_lifecycle = inkdrop_state.history_state_view(db, limit=100, focus={"series_id": "series:lifecycle"})
        require(
            {row["id"] for row in focused_lifecycle["rows"]} == {"h-slskd-live", "h-import"},
            "per-series History did not stay within download/import lifecycle rows",
        )
        require(focused_lifecycle["total_count_sampled"] is False, "focused lifecycle count should describe only loaded matching rows")

        with inkdrop_state.connect(db) as con:
            con.executemany(
                """
                insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                [
                    (f"noise:{index}", "queue_item", f"queue:{index}", "queue_event", "inkdrop", "queued", 200 + index, "{}")
                    for index in range(5000)
                ],
            )
        original_row_builder = inkdrop_state.history_row_from_record
        transformed = 0

        def counted_row_builder(row):
            nonlocal transformed
            transformed += 1
            return original_row_builder(row)

        inkdrop_state.history_row_from_record = counted_row_builder
        started = time.perf_counter()
        try:
            page = inkdrop_state.history_state_view(db, limit=2, history_filter="downloads")
        finally:
            inkdrop_state.history_row_from_record = original_row_builder
        elapsed = time.perf_counter() - started
        option = next(row for row in page["filters"] if row["value"] == "downloads")
        require(page["history_taxonomy_version"] == 2, "History response omitted taxonomy version")
        require(page["history_candidate_limit"] == 10000, "Downloads did not disclose its hard recent-candidate bound")
        require(page["loaded_count"] == 2 and page["total_count"] == 2, f"Downloads sampled lower-bound total disagrees with loaded rows: loaded={page['loaded_count']} total={page['total_count']} option={option}")
        require(page["total_count_sampled"] is True, "Downloads total did not disclose sampled semantics")
        require(option["sampled"] is True and option["sample_limit"] == 1000, "Downloads facet did not disclose its bounded sample")
        require([row["id"] for row in page["rows"]] == [row["id"] for row in download_rows[:2]], "Downloads first page chronology disagrees")
        require(transformed <= 1002, f"Downloads page exceeded its 1,000-facet-row plus page-row bound: {transformed}")
        require(elapsed < 5.0, f"Downloads page/count benchmark regressed on 5,000 unrelated events: {elapsed:.3f}s")
        require(len(all_rows) == len(fixture["events"]) + 4, "All did not preserve the complete deduplicated event trail")

        with inkdrop_state.connect(db) as con:
            con.execute(
                """
                insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values('h-lifecycle-behind-search-noise','download_task','dt:behind-noise','download_task','slskd','older transfer lifecycle',50000,'{}')
                """
            )
            con.executemany(
                """
                insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                [
                    (f"newer-search-noise:{index}", "source_attempt", f"sa:noise:{index}", "source_attempt", "prowlarr", "search attempt", 50001 + index, "{}")
                    for index in range(10001)
                ],
            )
        lifecycle_behind_noise = inkdrop_state.history_state_view(db, limit=20)
        require(
            "h-lifecycle-behind-search-noise" in {row["id"] for row in lifecycle_behind_noise["rows"]},
            "10,001 newer search attempts hid an older lifecycle candidate",
        )

        with inkdrop_state.connect(db) as con:
            con.execute("insert into series(id,title,created_at,updated_at) values('series:older-focus','Older Focus',1,1)")
            con.execute("insert into issues(id,series_id,issue_number,created_at,updated_at) values('issue:older-focus','series:older-focus','1',1,1)")
            con.execute("insert into wanted_items(id,series_id,issue_id,status,created_at,updated_at) values('wanted:focus','series:older-focus','issue:older-focus','in_progress',1,1)")
            con.execute("insert into queue_items(id,wanted_id,series_id,issue_id,state,created_at,updated_at) values('queue:focus','wanted:focus','series:older-focus','issue:older-focus','importing',1,1)")
            con.execute("insert into source_attempts(id,queue_id,wanted_id,series_id,issue_id,source,status,started_at,completed_at) values('sa:focus','queue:focus','wanted:focus','series:older-focus','issue:older-focus','importer','verified',1,1)")
            con.execute("insert into download_tasks(id,queue_id,wanted_id,series_id,issue_id,source_attempt_id,source,status,state,started_at,updated_at,completed_at) values('dt:focus','queue:focus','wanted:focus','series:older-focus','issue:older-focus','sa:focus','slskd','verified','verified',2,2,2)")
            con.execute("insert into import_results(id,queue_id,source_attempt_id,series_id,issue_id,status,verified,created_at) values('ir:focus','queue:focus','sa:focus','series:older-focus','issue:older-focus','imported',1,3)")
            con.executemany(
                """
                insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                [
                    ("h-linked-source-focus", "source_attempt", "sa:focus", "source_attempt", "importer", "Importer verified release", 2, "{}"),
                    ("h-linked-download-focus", "download_task", "dt:focus", "download_task", "slskd", "Transfer verified", 3, "{}"),
                    ("h-linked-import-focus", "import_result", "ir:focus", "import_completed", "importer", "Import completed", 4, "{}"),
                ],
            )
            con.execute(
                """
                insert into history_events(id,series_id,issue_id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values('h-older-focused-lifecycle','series:older-focus','issue:older-focus','download_task','dt:older-focused','download_task','slskd','older focused transfer',1,'{}')
                """
            )
            con.executemany(
                """
                insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                [
                    (f"unrelated-lifecycle:{index}", "download_task", f"dt:unrelated:{index}", "download_task", "slskd", "unrelated transfer", 20000 + index, "{}")
                    for index in range(5001)
                ],
            )
        older_focus = inkdrop_state.history_state_view(db, limit=20, focus={"series_id": "series:older-focus"})
        require(
            "h-older-focused-lifecycle" in {row["id"] for row in older_focus["rows"]},
            "SQL focus was applied after LIMIT and lost older per-series lifecycle history",
        )
        older_issue_focus = inkdrop_state.history_state_view(db, limit=20, focus={"issue_id": "issue:older-focus"})
        require(
            "h-older-focused-lifecycle" in {row["id"] for row in older_issue_focus["rows"]},
            "SQL focus was applied after LIMIT and lost older per-issue lifecycle history",
        )
        linked_focus_ids = {"h-linked-source-focus", "h-linked-download-focus", "h-linked-import-focus"}
        require(linked_focus_ids.issubset({row["id"] for row in older_focus["rows"]}), "series focus omitted linked source/download/import lifecycle rows")
        require(linked_focus_ids.issubset({row["id"] for row in older_issue_focus["rows"]}), "issue focus omitted linked source/download/import lifecycle rows")
        for focus_key, focus_value in (
            ("queue_id", "queue:focus"),
            ("wanted_id", "wanted:focus"),
            ("source_attempt_id", "sa:focus"),
        ):
            linked_focus = inkdrop_state.history_state_view(db, limit=20, focus={focus_key: focus_value})
            require(
                linked_focus_ids.issubset({row["id"] for row in linked_focus["rows"]}),
                f"{focus_key} focus omitted linked lifecycle rows before the cutoff",
            )
        direct_download_focus = inkdrop_state.history_state_view(db, limit=20, focus={"download_task_id": "dt:focus"})
        direct_import_focus = inkdrop_state.history_state_view(db, limit=20, focus={"import_id": "ir:focus"})
        multi_focus = inkdrop_state.history_state_view(db, limit=20, focus={"download_task_id": "dt:focus", "import_id": "ir:focus"})
        require({row["id"] for row in direct_download_focus["rows"]} == {"h-linked-download-focus"}, "download_task focus missed direct indexed event")
        require({row["id"] for row in direct_import_focus["rows"]} == {"h-linked-import-focus"}, "import focus missed direct indexed event")
        require({"h-linked-download-focus", "h-linked-import-focus"}.issubset({row["id"] for row in multi_focus["rows"]}), "multi-key focus did not union/dedupe bounded candidates")

        with inkdrop_state.connect(db) as con:
            con.execute("insert into series(id,title,created_at,updated_at) values('series:truncated-focus','Truncated Focus',1,1)")
            con.executemany(
                """
                insert into history_events(id,series_id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                [
                    (f"truncated-focus:{index}", "series:truncated-focus", "download_task", f"dt:truncated:{index}", "download_task", "slskd", "focused transfer", 70000 + index, "{}")
                    for index in range(5001)
                ],
            )
        truncated_focus = inkdrop_state.history_state_view(db, limit=20, focus={"series_id": "series:truncated-focus"})
        require(truncated_focus["total_count"] == 5000, "focused lower-bound count did not retain the SQL result cutoff")
        require(truncated_focus["total_count_sampled"] is True, "truncated focused total was incorrectly reported exact")
        require(truncated_focus["focused_result_limit"] == 5000, "focused result cutoff was not disclosed")
        older_focus_after_same_stream_noise = inkdrop_state.history_state_view(db, limit=20, focus={"series_id": "series:older-focus"})
        require(
            "h-older-focused-lifecycle" in {row["id"] for row in older_focus_after_same_stream_noise["rows"]},
            "10,001 newer other-series lifecycle rows hid the old focused series row",
        )
        require(older_focus_after_same_stream_noise["total_count_sampled"] is False, "small focused result was falsely reported sampled")

        with inkdrop_state.connect(db) as con:
            con.executemany(
                """
                insert into history_events(id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values(?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        f"compact-noise:{index}",
                        "queue_item",
                        f"compact-queue:{index}",
                        "queue_event",
                        "inkdrop",
                        "queued automatic",
                        10000 + index,
                        "{}",
                    )
                    for index in range(50000)
                ],
            )

        exact_count_calls = []
        compact_transformed = 0
        original_filter_count = inkdrop_state.history_filter_count
        original_row_builder = inkdrop_state.history_row_from_record

        def reject_exact_download_count(db_path, history_filter):
            exact_count_calls.append(history_filter)
            raise AssertionError(f"compact Activity called exact history counter: {history_filter}")

        def count_compact_transforms(row):
            nonlocal compact_transformed
            compact_transformed += 1
            return original_row_builder(row)

        inkdrop_state.HISTORY_ACTIVITY_VIEW_CACHE.clear()
        inkdrop_state.history_filter_count = reject_exact_download_count
        inkdrop_state.history_row_from_record = count_compact_transforms
        compact_started = time.perf_counter()
        try:
            compact_activity = inkdrop_state.state_view(
                db,
                "history",
                limit=24,
                history_filter="activity",
                summary_mode="compact",
                row_mode="compact",
            )
        finally:
            inkdrop_state.history_filter_count = original_filter_count
            inkdrop_state.history_row_from_record = original_row_builder
        compact_elapsed = time.perf_counter() - compact_started
        compact_downloads = next(row for row in compact_activity["filters"] if row["value"] == "downloads")
        require(not exact_count_calls, f"compact Activity used exact history counts: {exact_count_calls}")
        require(compact_activity["history_scope"] == "download_import_lifecycle", "compact default History did not use lifecycle scope")
        require(compact_activity["history_candidate_limit"] == 10000, "default Lifecycle omitted its hard candidate bound")
        require(compact_activity["history_grouped"] is False and compact_activity["summary"]["history_activity_grouped"] is False, "Lifecycle omitted explicit grouped-view compatibility metadata")
        require(compact_activity["summary"]["history_activity_sample_limit"] == 10000, "Lifecycle summary omitted its candidate sample limit")
        require(compact_transformed <= 1024, f"default Lifecycle exceeded bounded page/facet transformations: {compact_transformed}")
        require(all(row.get("history_kind") in {"download", "import", "verification"} for row in compact_activity["rows"]), "compact default History leaked diagnostic/search rows")
        require(compact_downloads["sampled"] is True, "compact Downloads facet should be marked sampled")
        require(compact_elapsed < 2.0, f"compact Activity regressed on 50,000-row history: {compact_elapsed:.3f}s")

        step_db = Path(temp) / "history-vm-steps.sqlite3"
        inkdrop_state.ensure_schema(step_db)
        with inkdrop_state.connect(step_db) as con:
            con.execute(
                """
                insert into history_events(id,series_id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values('vm-old-lifecycle','vm-series-target','download_task','vm-dt-old','download_task','slskd','old lifecycle',1,'{}')
                """
            )
            con.executemany(
                """
                insert into history_events(id,series_id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                (
                    (f"vm-noise:{index}", "vm-series-other", "download_task", f"vm-dt:{index}", "download_task", "slskd", "other lifecycle", 2 + index, "{}")
                    for index in range(10000)
                ),
            )

        def lifecycle_vm_steps(focus=None):
            callbacks = 0
            original_connect = inkdrop_state.connect
            original_connect_read = inkdrop_state.connect_read

            def progress():
                nonlocal callbacks
                callbacks += 1
                return 0

            @contextlib.contextmanager
            def measured_connect(*args, **kwargs):
                with original_connect(*args, **kwargs) as con:
                    con.set_progress_handler(progress, 100)
                    yield con

            @contextlib.contextmanager
            def measured_connect_read(*args, **kwargs):
                with original_connect_read(*args, **kwargs) as con:
                    con.set_progress_handler(progress, 100)
                    yield con

            inkdrop_state.connect = measured_connect
            inkdrop_state.connect_read = measured_connect_read
            try:
                measured_rows = inkdrop_state.recent_history(step_db, 20, history_filter="activity", focus=focus) if focus else None
                measured = {"rows": measured_rows} if focus else inkdrop_state.history_state_view(step_db, limit=20)
            finally:
                inkdrop_state.connect = original_connect
                inkdrop_state.connect_read = original_connect_read
            if focus:
                require("vm-old-lifecycle" in {row["id"] for row in measured["rows"]}, "indexed focused lifecycle query lost the old event")
            return callbacks * 100

        vm_steps_10k = lifecycle_vm_steps()
        focus_vm_steps_10k = lifecycle_vm_steps({"series_id": "vm-series-target"})
        with inkdrop_state.connect(step_db) as con:
            con.executemany(
                """
                insert into history_events(id,series_id,entity_type,entity_id,event_type,source,message,created_at,raw_json)
                values(?,?,?,?,?,?,?,?,?)
                """,
                (
                    (f"vm-noise:{index}", "vm-series-other", "download_task", f"vm-dt:{index}", "download_task", "slskd", "other lifecycle", 2 + index, "{}")
                    for index in range(10000, 100000)
                ),
            )
        vm_steps_100k = lifecycle_vm_steps()
        focus_vm_steps_100k = lifecycle_vm_steps({"series_id": "vm-series-target"})
        require(
            vm_steps_100k <= max(vm_steps_10k * 2, vm_steps_10k + 5000),
            f"Lifecycle VM work scaled with diagnostic noise: 10k={vm_steps_10k} 100k={vm_steps_100k}",
        )
        require(
            focus_vm_steps_100k <= max(focus_vm_steps_10k * 2, focus_vm_steps_10k + 5000),
            f"Focused Lifecycle VM work scaled with other-series rows: 10k={focus_vm_steps_10k} 100k={focus_vm_steps_100k}",
        )

        migration_db = Path(temp) / "history-bucket-migration.sqlite3"
        raw_con = sqlite3.connect(migration_db)
        try:
            raw_con.execute(
                """
                create table history_events(
                    id text primary key, entity_type text, entity_id text, series_id text, issue_id text,
                    event_type text not null, source text, message text, outcome text, display_phase text,
                    created_at real, raw_json text, history_bucket text not null default 'diagnostic'
                )
                """
            )
            raw_con.execute(
                "insert into history_events values('migration-old-queue','queue_item','q-old',null,null,'queue_event','inkdrop','Kavita verified old issue',null,null,1,'{}','diagnostic')"
            )
            raw_con.execute(
                "insert into history_events values('migration-uppercase-kavita','source_attempt','sa-old',null,null,'source_attempt','KAVITA','Reader visibility observed',null,null,2,'{}','diagnostic')"
            )
            raw_con.execute(
                "insert into history_events values('migration-import-failed','queue_item','q-import-failed',null,null,'IMPORT_FAILED','importer','Import failed',null,null,2.5,'{}','diagnostic')"
            )
            raw_con.executemany(
                "insert into history_events values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (f"migration-noise:{index}", "source_attempt", f"migration-sa:{index}", None, None, "source_attempt", "prowlarr", "search attempt", None, None, 3 + index, "{}", "diagnostic")
                    for index in range(10001)
                ),
            )
            raw_con.commit()
        finally:
            raw_con.close()
        inkdrop_state.ensure_schema(migration_db)
        with inkdrop_state.connect_read(migration_db) as con:
            migrated = {
                row["id"]: row["history_bucket"]
                for row in con.execute(
                    "select id,history_bucket from history_events where id in ('migration-old-queue','migration-uppercase-kavita','migration-import-failed')"
                )
            }
            plan = " ".join(
                str(row["detail"])
                for row in con.execute(
                    "explain query plan select id from history_events indexed by idx_history_bucket_created where history_bucket='lifecycle' order by created_at desc,id desc limit 20"
                )
            )
            focus_plan = " ".join(
                str(row["detail"])
                for row in con.execute(
                    "explain query plan select id from history_events indexed by idx_history_bucket_series_created where history_bucket='lifecycle' and series_id='target' order by created_at desc,id desc limit 20"
                )
            )
            migration_marker = con.execute("select value from schema_meta where key='history_bucket_v2_complete'").fetchone()[0]
        require(migrated == {"migration-old-queue": "lifecycle", "migration-uppercase-kavita": "lifecycle", "migration-import-failed": "lifecycle"}, "restart-safe migration lost message-only, uppercase Kavita, or import-prefix lifecycle rows")
        require("idx_history_bucket_created" in plan, f"Lifecycle query plan omitted canonical index: {plan}")
        require("idx_history_bucket_series_created" in focus_plan, f"Focused Lifecycle plan omitted direct series index: {focus_plan}")
        require(str(migration_marker) == "1", "restart-safe lifecycle migration marker was not committed")
        migrated_view = inkdrop_state.history_state_view(migration_db, limit=20)
        require(
            {"migration-old-queue", "migration-uppercase-kavita", "migration-import-failed"}.issubset({row["id"] for row in migrated_view["rows"]}),
            "canonical lifecycle query lost migrated rows behind 10,001 diagnostics",
        )
        inkdrop_state.ensure_schema(migration_db)
        with inkdrop_state.connect_read(migration_db) as con:
            require(
                con.execute("select count(*) from history_events where history_bucket='lifecycle'").fetchone()[0] == 3,
                "lifecycle migration/backfill was not idempotent",
            )

    print(json.dumps({
        "ok": True,
        "suite": "history_taxonomy_v2",
        "terminal_downloads": len(expected_download_ids),
        "large_fixture_seconds": round(elapsed, 4),
        "compact_activity_50000_seconds": round(compact_elapsed, 4),
        "vm_steps_10k_noise": vm_steps_10k,
        "vm_steps_100k_noise": vm_steps_100k,
        "focus_vm_steps_10k": focus_vm_steps_10k,
        "focus_vm_steps_100k": focus_vm_steps_100k,
    }, indent=2))


if __name__ == "__main__":
    main()
