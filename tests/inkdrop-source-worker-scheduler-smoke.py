#!/usr/bin/env python3
import json
import tempfile
from pathlib import Path

from core import inkdrop_source_catalog as catalog
from core import inkdrop_source_worker_scheduler as scheduler
from core import inkdrop_state


NOW = 123456.0


def fail(message):
    print(f"SOURCE_WORKER_SCHEDULER_FAIL: {message}")
    raise SystemExit(1)


def ok(message):
    print(f"SOURCE_WORKER_SCHEDULER_OK: {message}")


def assert_equal(actual, expected, message):
    if actual != expected:
        fail(f"{message}: expected {expected!r}, got {actual!r}")


def assert_true(value, message):
    if not value:
        fail(message)


def assert_false(value, message):
    if value:
        fail(message)


def ensure_provider_parent(db_path, provider_id):
    with inkdrop_state.connect_read(db_path) as con:
        row = con.execute("select 1 from provider_configs where id = ?", (provider_id,)).fetchone()
    if row:
        return
    inkdrop_state.sync_settings(
        db_path,
        providers=[
            {
                "id": provider_id,
                "provider_type": "source",
                "display_name": provider_id.replace("_", " ").title(),
                "enabled": False,
                "source": "smoke_fixture",
                "settings": {"implementation_status": "fixture"},
            }
        ],
        settings=[],
    )


def enable_provider(db_path, provider_id, *, source_mode="auto", auto_download_allowed=True):
    ensure_provider_parent(db_path, provider_id)
    inkdrop_state.update_provider_config(
        db_path,
        provider_id,
        {
            "enabled": True,
            "settings": {
                "implementation_status": "implemented",
                "source_mode": source_mode,
                "auto_download_allowed": auto_download_allowed,
                "requires_manual_confirm": source_mode != "auto",
                "policy": {"requires_manual_confirm": source_mode != "auto"},
            },
        },
    )


def seed_settings(db_path):
    seed = catalog.settings_seed_payload()
    inkdrop_state.sync_settings(db_path, providers=seed["providers"], settings=seed["settings"])
    inkdrop_state.sync_settings(
        db_path,
        providers=[
            {
                "id": "generic_reader_page_pack_source",
                "provider_type": "direct_download",
                "display_name": "Generic Reader Page Pack",
                "enabled": False,
                "source": "smoke_fixture",
                "settings": {
                    "implementation_status": "implemented",
                    "source_kind": "reader_page_pack_source",
                    "source_mode": "assist",
                    "auto_download_allowed": False,
                    "requires_manual_confirm": True,
                    "policy": {"requires_manual_confirm": True},
                },
            }
        ],
        settings=[],
    )
    enable_provider(db_path, "standard_ebooks")
    enable_provider(db_path, "prowlarr_nyaa")
    enable_provider(db_path, "prowlarr_torrentleech_comics")
    enable_provider(db_path, "prowlarr_tokyo_toshokan_manga")
    inkdrop_state.update_provider_config(
        db_path,
        "prowlarr_nyaa",
        {
            "base_url": "http://prowlarr.local",
            "secret_ref": "prowlarr_api_key",
        },
    )
    inkdrop_state.update_provider_config(
        db_path,
        "prowlarr_tokyo_toshokan_manga",
        {
            "base_url": "http://prowlarr.local",
            "secret_ref": "prowlarr_api_key",
        },
    )
    inkdrop_state.update_provider_config(
        db_path,
        "prowlarr_torrentleech_comics",
        {
            "base_url": "http://prowlarr.local",
            "secret_ref": "prowlarr_api_key",
        },
    )
    enable_provider(
        db_path,
        "mangadex",
        source_mode="assist",
        auto_download_allowed=False,
    )
    enable_provider(
        db_path,
        "generic_reader_page_pack_source",
        source_mode="assist",
        auto_download_allowed=False,
    )
    inkdrop_state.update_provider_config(
        db_path,
        "generic_reader_page_pack_source",
        {
            "settings": {
                "policy": {
                    "search_url_templates": ["https://reader-search.example/search?q={query}"],
                    "source_site_label": "ReadComicOnline",
                    "requires_manual_confirm": True,
                },
            },
        },
    )


def seed_queue(
    db_path,
    queue_id,
    *,
    series_title,
    issue_number="1",
    media_type="book",
    publisher="Smoke Press",
    state="queued",
    display_phase="queued",
    outcome="waiting",
    provider="source_ladder",
    provider_phase="queued",
    retry_after=NOW - 60,
):
    series_id = f"series:{queue_id}"
    issue_id = f"{series_id}:issue:{issue_number}"
    wanted_id = f"wanted:{issue_id}"
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        con.execute(
            """
            insert into series(id,title,media_type,year,publisher,created_at,updated_at)
            values(?,?,?,?,?,?,?)
            """,
            (series_id, series_title, media_type, 2024, publisher, NOW, NOW),
        )
        con.execute(
            """
            insert into issues(id,series_id,issue_number,normalized_number,title,created_at,updated_at)
            values(?,?,?,?,?,?,?)
            """,
            (issue_id, series_id, issue_number, issue_number, f"Issue {issue_number}", NOW, NOW),
        )
        con.execute(
            """
            insert into wanted_items(id,series_id,issue_id,reason,status,priority,created_at,updated_at)
            values(?,?,?,?,?,?,?,?)
            """,
            (wanted_id, series_id, issue_id, "missing", "wanted", 50, NOW, NOW),
        )
        con.execute(
            """
            insert into queue_items(
                id,wanted_id,series_id,issue_id,state,query,last_event,active,
                created_at,updated_at,retry_after,display_phase,outcome,
                provider_status_state,provider_status_phase,provider_status_provider
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                queue_id,
                wanted_id,
                series_id,
                issue_id,
                state,
                f"{series_title} {issue_number}",
                "scheduler smoke seed",
                1,
                NOW,
                NOW,
                retry_after,
                display_phase,
                outcome,
                "waiting",
                provider_phase,
                provider,
            ),
        )
        con.commit()


def seed_active_handoff(db_path, queue_id, *, updated_at=NOW, download_client="sabnzbd"):
    with inkdrop_state.connect(db_path) as con:
        con.execute(
            """
            insert into download_tasks(
                id,queue_id,source,provider,download_client,title,status,state,
                lifecycle_phase,outcome,display_phase,started_at,updated_at,raw_json
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"task:{queue_id}",
                queue_id,
                "prowlarr",
                "DOGnzb",
                download_client,
                "Smoke candidate",
                "sent",
                "queued",
                "downloading",
                "productive",
                "source_wait",
                NOW,
                updated_at,
                "{}",
            ),
        )
        con.commit()


def seed_provider_wait_marker(db_path, queue_id, *, updated_at=NOW, provider_id="comicscodes"):
    with inkdrop_state.connect(db_path) as con:
        con.execute(
            """
            insert into download_tasks(
                id,queue_id,source,provider_id,provider,download_client,title,status,state,
                lifecycle_phase,outcome,display_phase,failure_reason,started_at,updated_at,raw_json
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"provider-wait-marker:{queue_id}",
                queue_id,
                provider_id,
                provider_id,
                provider_id,
                "",
                "Provider health marker",
                "provider_wait",
                "queued",
                "provider_wait",
                "problem",
                "provider_wait",
                "provider health backoff",
                NOW,
                updated_at,
                "{}",
            ),
        )
        con.commit()


def seed_retryable_failed_handoff(db_path, queue_id, *, updated_at=NOW - 30):
    with inkdrop_state.connect(db_path) as con:
        row = con.execute(
            """
            select wanted_id, series_id, issue_id, query
            from queue_items
            where id=?
            """,
            (queue_id,),
        ).fetchone()
        if not row:
            fail(f"cannot seed failed handoff for missing queue: {queue_id}")
        con.execute(
            """
            insert into download_tasks(
                id,queue_id,wanted_id,series_id,issue_id,source,provider_id,provider,
                protocol,download_client,external_id,candidate_identity,title,status,state,
                lifecycle_phase,outcome,display_phase,failure_reason,retry_eligible,
                started_at,updated_at,completed_at,raw_json
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"failed-handoff:{queue_id}",
                queue_id,
                row["wanted_id"],
                row["series_id"],
                row["issue_id"],
                "standard_ebooks",
                "standard_ebooks",
                "Standard Ebooks",
                "torrent",
                "qBittorrent",
                "failed-handoff-hash",
                "candidate:failed-handoff",
                row["query"],
                "failed_download",
                "failed",
                "failed_candidate",
                "problem",
                "problem",
                "qBittorrent was unavailable during handoff",
                1,
                updated_at - 60,
                updated_at,
                updated_at,
                "{}",
            ),
        )
        con.commit()


def seed_recent_source_attempt(
    db_path,
    queue_id,
    *,
    provider_id="standard_ebooks",
    source=None,
    status="searched_no_candidates",
    lifecycle_phase="searched_no_candidates",
    display_phase="",
    outcome="no_candidate",
    failure_reason="",
    title=None,
    completed_at=NOW - 120,
    attempt_id=None,
    raw_json="{}",
):
    with inkdrop_state.connect(db_path) as con:
        row = con.execute(
            """
            select wanted_id, series_id, issue_id, query
            from queue_items
            where id=?
            """,
            (queue_id,),
        ).fetchone()
        if not row:
            fail(f"cannot seed source attempt for missing queue: {queue_id}")
        con.execute(
            """
            insert into source_attempts(
                id, queue_id, wanted_id, series_id, issue_id,
                source, provider_id, provider, status, title,
                lifecycle_phase, display_phase, outcome, failure_reason,
                started_at, completed_at, raw_json
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                attempt_id or f"attempt:{queue_id}:{provider_id}:{status}",
                queue_id,
                row["wanted_id"],
                row["series_id"],
                row["issue_id"],
                source or provider_id,
                provider_id,
                provider_id,
                status,
                title if title is not None else row["query"],
                lifecycle_phase,
                display_phase,
                outcome,
                failure_reason,
                completed_at,
                completed_at,
                raw_json,
            ),
        )
        con.commit()


def seed_retryable_stage_attempt(db_path, queue_id, *, completed_at=NOW - 20):
    with inkdrop_state.connect(db_path) as con:
        row = con.execute(
            """
            select wanted_id, series_id, issue_id, query
            from queue_items
            where id=?
            """,
            (queue_id,),
        ).fetchone()
        if not row:
            fail(f"cannot seed retryable stage attempt for missing queue: {queue_id}")
        con.execute(
            """
            insert into source_attempts(
                id, queue_id, wanted_id, series_id, issue_id,
                source, provider_id, provider, status, title,
                lifecycle_phase, display_phase, outcome, failure_reason,
                retry_eligible, started_at, completed_at, raw_json
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"stage-failure:{queue_id}",
                queue_id,
                row["wanted_id"],
                row["series_id"],
                row["issue_id"],
                "queue",
                "mangadex",
                "mangadex",
                "failed_download",
                row["query"],
                "failed_candidate",
                "problem",
                "problem",
                "source HTTP host is not allowed: cmdxd98sb0x3yprd.mangadex.network",
                1,
                completed_at,
                completed_at,
                "{}",
            ),
        )
        con.commit()


def seed_provider_timeout_attempt(db_path, provider_id, index, *, completed_at=NOW - 60):
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        con.execute(
            """
            insert into source_attempts(
                id, source, provider_id, provider, status, display_phase,
                failure_reason, title, started_at, completed_at, raw_json
            )
            values(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"timeout:{provider_id}:{index}",
                provider_id,
                provider_id,
                provider_id,
                "timeout",
                "provider_timeout",
                "provider search timed out",
                f"{provider_id} timeout proof {index}",
                completed_at,
                completed_at,
                '{"prowlarr_search_timeout": true, "timed_out": true}',
            ),
        )
        con.commit()


def seed_provider_fetch_failure_attempt(
    db_path,
    provider_id,
    index,
    *,
    status="provider_unavailable",
    failure_reason="http_request_failed",
    completed_at=NOW - 60,
):
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        con.execute(
            """
            insert into source_attempts(
                id, source, provider_id, provider, status, display_phase,
                failure_reason, title, started_at, completed_at, raw_json
            )
            values(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"fetch-failure:{provider_id}:{index}",
                provider_id,
                provider_id,
                provider_id,
                status,
                "provider_wait",
                failure_reason,
                f"{provider_id} fetch failure proof {index}",
                completed_at,
                completed_at,
                '{"fetch": {"reason": "http_request_failed", "partial_errors": [{"error": "SourceHttpError: http_error"}]}}',
            ),
        )
        con.commit()


def seed_provider_health(db_path, provider_id, *, state="healthy", created_at=NOW - 30):
    provider_id = str(provider_id or "").strip().lower()
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        health = {
            "state": state,
            "label": state,
            "detail": f"{provider_id} recovered",
            "health_scope": "provider_status",
            "api_reachable": state in {"healthy", "running"},
        }
        con.execute(
            """
            insert into history_events(
                id, entity_type, entity_id, event_type, source, message,
                outcome, display_phase, created_at, raw_json
            )
            values(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"provider-health:{provider_id}:{created_at}",
                "provider",
                provider_id,
                "provider_health",
                provider_id,
                f"{provider_id} health {state}",
                "automatic",
                state,
                created_at,
                json.dumps({"provider_id": provider_id, "health": health}),
            ),
        )
        con.commit()


def counts(db_path):
    with inkdrop_state.connect_read(db_path) as con:
        out = {}
        for table in [
            "queue_items",
            "source_attempts",
            "download_tasks",
            "provider_configs",
            "app_settings",
            "settings_history",
        ]:
            if inkdrop_state.table_exists(con, table):
                out[table] = con.execute(f"select count(*) as n from {table}").fetchone()["n"]
        return out


def plan_one(
    db_path,
    queue_id,
    *,
    provider_ids=None,
    due_only=False,
    attempt_cooldown_seconds=0,
    include_blocked=False,
    provider_timeout_window_seconds=0,
    provider_timeout_threshold=0,
    provider_timeout_cooldown_seconds=0,
    provider_fetch_failure_window_seconds=0,
    provider_fetch_failure_threshold=0,
    provider_fetch_failure_cooldown_seconds=0,
):
    result = scheduler.source_worker_queue_plan(
        db_path,
        queue_ids=[queue_id],
        provider_ids=provider_ids,
        limit=10,
        due_only=due_only,
        include_blocked=include_blocked,
        attempt_cooldown_seconds=attempt_cooldown_seconds,
        provider_timeout_window_seconds=provider_timeout_window_seconds,
        provider_timeout_threshold=provider_timeout_threshold,
        provider_timeout_cooldown_seconds=provider_timeout_cooldown_seconds,
        provider_fetch_failure_window_seconds=provider_fetch_failure_window_seconds,
        provider_fetch_failure_threshold=provider_fetch_failure_threshold,
        provider_fetch_failure_cooldown_seconds=provider_fetch_failure_cooldown_seconds,
        now=NOW,
    )
    assert_true(result["ok"], f"{queue_id} scheduler result ok")
    assert_equal(result["queue_count"], 1, f"{queue_id} returns one queue plan")
    return result["plans"][0]


def provider_plan_by_id(plan):
    return {
        row.get("provider_id"): row
        for row in (plan or {}).get("provider_attempt_plan") or []
        if row.get("provider_id")
    }


def smoke_retryable_handoff_recovery_due_plan():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-handoff-retry-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_settings(db_path)
        seed_queue(
            db_path,
            "queue-failed-handoff-recovery",
            series_title="Downloader Recovery Book",
            retry_after=NOW + 900,
        )
        seed_retryable_failed_handoff(db_path, "queue-failed-handoff-recovery")
        plan = plan_one(
            db_path,
            "queue-failed-handoff-recovery",
            provider_ids=["standard_ebooks"],
            due_only=True,
        )
        assert_equal(plan["status"], "eligible", "retryable downloader handoff failure bypasses row retry wait")
        assert_equal(
            plan["selected_provider_ids"],
            ["standard_ebooks"],
            "retryable downloader handoff recovery selects the ready provider",
        )
        assert_equal(plan["retryable_failed_handoff_count"], 1, "retryable failed handoff evidence is surfaced")
        assert_true(
            plan.get("retryable_failed_handoff_recovery"),
            "retryable failed handoff recovery is explicit on the plan",
        )
        assert_equal(
            plan["next_action"],
            "Retry ready source jobs after a retry-eligible downloader handoff failure.",
            "retryable handoff recovery has user-facing next action",
        )


def smoke_retryable_stage_failure_unblocks_page_pack_handoff():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-stage-retry-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_settings(db_path)
        seed_queue(
            db_path,
            "queue-page-pack-stage-retry",
            series_title="Hunter X Hunter",
            media_type="comic",
            publisher="Viz",
            state="source_wait",
            display_phase="provider_wait",
            provider="mangadex",
            provider_phase="provider_wait",
            retry_after=NOW + 900,
        )
        seed_active_handoff(
            db_path,
            "queue-page-pack-stage-retry",
            updated_at=NOW - 30,
            download_client="inkdrop_page_pack",
        )
        seed_retryable_stage_attempt(db_path, "queue-page-pack-stage-retry", completed_at=NOW - 20)
        plan = plan_one(
            db_path,
            "queue-page-pack-stage-retry",
            provider_ids=["standard_ebooks"],
        )
        assert_equal(plan["status"], "eligible", "retryable page-pack stage failure bypasses active handoff")
        assert_equal(
            plan["selected_provider_ids"],
            ["standard_ebooks"],
            "retryable page-pack stage failure selects the ready provider",
        )
        assert_equal(plan["active_handoff_count"], 1, "active page-pack handoff evidence remains visible")
        assert_equal(plan["retryable_failed_stage_attempt_count"], 1, "retryable stage failure evidence is surfaced")
        assert_true(
            plan.get("retryable_failed_stage_recovery"),
            "retryable failed stage recovery is explicit on the plan",
        )
        assert_equal(
            plan["next_action"],
            "Retry ready source jobs after a retry-eligible InkDrop staging failure.",
            "retryable stage recovery has user-facing next action",
        )


def smoke_provider_timeout_recovery_health_plan():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-provider-recovery-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_settings(db_path)
        seed_queue(
            db_path,
            "queue-provider-timeout-recovered",
            series_title="Death Note",
            media_type="comic",
            publisher="Shueisha",
        )
        seed_provider_timeout_attempt(db_path, "prowlarr_nyaa", 1, completed_at=NOW - 120)
        seed_provider_timeout_attempt(db_path, "prowlarr_nyaa", 2, completed_at=NOW - 60)
        seed_provider_health(db_path, "prowlarr", state="healthy", created_at=NOW - 30)

        plan = plan_one(
            db_path,
            "queue-provider-timeout-recovered",
            provider_ids=["prowlarr_nyaa"],
            provider_timeout_window_seconds=600,
            provider_timeout_threshold=2,
            provider_timeout_cooldown_seconds=900,
        )
        assert_equal(plan["status"], "eligible", "recovered provider health closes timeout circuit in queue plan")
        assert_equal(
            plan["selected_provider_ids"],
            ["prowlarr_nyaa"],
            "recovered provider health lets the concrete indexer retry",
        )
        assert_equal(
            plan["provider_timeout_circuit_count"],
            0,
            "closed timeout circuit is not counted as active cooldown",
        )
        providers = provider_plan_by_id(plan)
        assert_equal(
            providers["prowlarr_nyaa"]["attempt_state"],
            "selected",
            "recovered provider evidence marks selection instead of timeout circuit",
        )


def smoke_comicscodes_assist_source_can_be_scheduled():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-comicscodes-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_settings(db_path)
        seed_queue(db_path, "queue-comicscodes", series_title="Moon Girl and Devil Dinosaur")
        enable_provider(
            db_path,
            "comicscodes",
            source_mode="assist",
            auto_download_allowed=False,
        )

        plan = plan_one(db_path, "queue-comicscodes", provider_ids=["comicscodes"])
        assert_equal(plan["status"], "eligible", "enabled ComicsCodes source is eligible for scheduled polling")
        assert_equal(plan["selected_provider_ids"], ["comicscodes"], "enabled ComicsCodes source is selected when cron includes it")
        providers = provider_plan_by_id(plan)
        assert_true(
            providers["comicscodes"].get("requires_operator"),
            "ComicsCodes scheduled polling preserves operator-review source gates",
        )
        assert_equal(
            providers["comicscodes"].get("schedule_state"),
            "assist_review",
            "ComicsCodes scheduled polling remains in assist-review mode",
        )
        assert_false(
            providers["comicscodes"].get("emits_download_task"),
            "ComicsCodes scheduled polling does not emit auto-download tasks",
        )


def smoke_prowlarr_aggregate_timeout_hidden_by_concrete_child():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-prowlarr-aggregate-timeout-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_settings(db_path)
        inkdrop_state.sync_settings(
            db_path,
            providers=[
                {
                    "id": "prowlarr",
                    "display_name": "Prowlarr",
                    "enabled": True,
                    "base_url": "http://prowlarr.local",
                    "secret_ref": "prowlarr_api_key",
                    "settings": {
                        "implementation_status": "implemented",
                        "source_mode": "auto",
                        "auto_download_allowed": True,
                        "requires_manual_confirm": False,
                        "policy": {"requires_manual_confirm": False},
                    },
                }
            ],
        )
        seed_queue(
            db_path,
            "queue-prowlarr-aggregate-timeout-child-ready",
            series_title="Sunstone",
            media_type="comic",
            publisher="Image Comics",
        )
        seed_provider_timeout_attempt(db_path, "prowlarr", 1, completed_at=NOW - 120)
        seed_provider_timeout_attempt(db_path, "prowlarr", 2, completed_at=NOW - 60)
        before = counts(db_path)

        aggregate_timeout_child_ready = plan_one(
            db_path,
            "queue-prowlarr-aggregate-timeout-child-ready",
            provider_ids=["prowlarr", "prowlarr_torrentleech_comics"],
            provider_timeout_window_seconds=600,
            provider_timeout_threshold=2,
            provider_timeout_cooldown_seconds=900,
        )
        assert_equal(
            aggregate_timeout_child_ready["status"],
            "eligible",
            "aggregate Prowlarr timeout does not block a ready concrete child lane",
        )
        assert_equal(
            aggregate_timeout_child_ready["selected_provider_ids"],
            ["prowlarr_torrentleech_comics"],
            "ready concrete Prowlarr child remains selected while aggregate fallback is cooled",
        )
        assert_equal(
            aggregate_timeout_child_ready["provider_timeout_circuit_count"],
            0,
            "aggregate fallback timeout is not counted beside a selected concrete child",
        )
        assert_equal(
            aggregate_timeout_child_ready["source_worker_cooldown_count"],
            0,
            "hidden aggregate fallback cooldown does not inflate row cooldown count",
        )
        aggregate_timeout_providers = provider_plan_by_id(aggregate_timeout_child_ready)
        assert_false(
            "prowlarr" in aggregate_timeout_providers,
            "aggregate fallback provider evidence is hidden once the concrete child owns the decision",
        )
        assert_equal(
            aggregate_timeout_providers["prowlarr_torrentleech_comics"]["attempt_state"],
            "selected",
            "concrete Prowlarr child evidence remains selected",
        )

        after = counts(db_path)
        assert_equal(after, before, "aggregate timeout diagnostic smoke performs no DB mutation")


def smoke_provider_fetch_failure_sibling_selection():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-provider-fetch-failure-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_settings(db_path)
        seed_queue(
            db_path,
            "queue-provider-fetch-failure-circuit",
            series_title="20th Century Boys",
            media_type="comic",
            publisher="Viz",
            retry_after=NOW - 60,
        )
        seed_provider_fetch_failure_attempt(
            db_path,
            "prowlarr_tokyo_toshokan_manga",
            1,
            completed_at=NOW - 120,
        )
        seed_provider_fetch_failure_attempt(
            db_path,
            "prowlarr_tokyo_toshokan_manga",
            2,
            completed_at=NOW - 60,
        )
        seed_provider_health(db_path, "prowlarr", state="healthy", created_at=NOW - 30)
        before = counts(db_path)

        fetch_failure = plan_one(
            db_path,
            "queue-provider-fetch-failure-circuit",
            provider_ids=["prowlarr_tokyo_toshokan_manga", "prowlarr_torrentleech_comics"],
            provider_fetch_failure_window_seconds=600,
            provider_fetch_failure_threshold=2,
            provider_fetch_failure_cooldown_seconds=900,
        )
        assert_equal(fetch_failure["status"], "eligible", "fetch-failed Prowlarr child does not block a healthy sibling")
        assert_equal(
            fetch_failure["selected_provider_ids"],
            ["prowlarr_torrentleech_comics"],
            "healthy sibling Prowlarr indexer remains selectable",
        )
        assert_equal(
            fetch_failure["provider_fetch_failure_circuit_count"],
            1,
            "fetch-failure circuit count is explicit",
        )
        fetch_failure_providers = provider_plan_by_id(fetch_failure)
        assert_equal(
            fetch_failure_providers["prowlarr_tokyo_toshokan_manga"]["attempt_state"],
            "provider_fetch_failure_circuit",
            "fetch-failed indexer provider evidence is explicit",
        )
        assert_equal(
            fetch_failure_providers["prowlarr_tokyo_toshokan_manga"]["recent_fetch_failure_count"],
            2,
            "fetch-failed indexer evidence carries recent failure count",
        )
        assert_equal(
            fetch_failure_providers["prowlarr_torrentleech_comics"]["attempt_state"],
            "selected",
            "healthy Prowlarr sibling is selected instead of broad fallback",
        )

        fetch_failure_only = plan_one(
            db_path,
            "queue-provider-fetch-failure-circuit",
            provider_ids=["prowlarr_tokyo_toshokan_manga"],
            provider_fetch_failure_window_seconds=600,
            provider_fetch_failure_threshold=2,
            provider_fetch_failure_cooldown_seconds=900,
        )
        assert_equal(fetch_failure_only["status"], "waiting_for_retry", "fetch-failure circuit blocks repeat indexer hammering")
        assert_equal(fetch_failure_only["blocker"], "provider outage/provider wait", "fetch-failure circuit uses provider wait blocker")
        assert_true(
            "prowlarr_tokyo_toshokan_manga fetch-failure circuit is open" in fetch_failure_only["next_action"],
            "fetch-failure next action names the concrete provider",
        )

        after = counts(db_path)
        assert_equal(after, before, "provider fetch-failure scheduler proof performs no DB mutation")


def smoke_runtime_budget_skip_does_not_cool_provider():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-runtime-skip-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_settings(db_path)
        seed_queue(db_path, "queue-runtime-budget-skip", series_title="Runtime Skip Book")
        before = counts(db_path)
        seed_recent_source_attempt(
            db_path,
            "queue-runtime-budget-skip",
            status="retry_scheduled",
            lifecycle_phase="retry_later",
            display_phase="retry_later",
            outcome="no_candidate",
            failure_reason="runtime budget has 22s left; Standard Ebooks needs about 2m",
            attempt_id="attempt:queue-runtime-budget-skip:standard_ebooks:runtime-skip",
            raw_json=json.dumps({"kind": "source_runtime_budget_skipped"}),
        )
        plan = plan_one(
            db_path,
            "queue-runtime-budget-skip",
            provider_ids=["standard_ebooks"],
            attempt_cooldown_seconds=3600,
        )
        assert_equal(plan["status"], "eligible", "runtime-budget skip evidence does not cool the provider")
        assert_equal(
            plan["selected_provider_ids"],
            ["standard_ebooks"],
            "runtime-budget skip still allows the provider to run later",
        )
        assert_equal(plan["source_worker_cooldown_count"], 0, "runtime-budget skip is not a source-worker cooldown")
        providers = provider_plan_by_id(plan)
        assert_equal(
            providers["standard_ebooks"]["attempt_state"],
            "selected",
            "runtime-budget skip does not look like a real recent attempt",
        )
        after = counts(db_path)
        assert_equal(
            after["source_attempts"],
            before.get("source_attempts", 0) + 1,
            "runtime-budget skip proof only seeds its source-attempt evidence",
        )


def smoke_manga_scoped_provider_scan_skips_comic_window():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-scheduler-scope-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_settings(db_path)
        enable_provider(db_path, "suwayomi")
        inkdrop_state.update_provider_config(
            db_path,
            "suwayomi",
            {
                "base_url": "http://suwayomi.local:4568",
                "settings": {
                    "policy": {
                        "source_allowed_hosts": ["suwayomi.local"],
                        "suwayomi_source_names": ["MangaDex"],
                    },
                },
            },
        )
        for index in range(1, 5):
            seed_queue(
                db_path,
                f"queue-suwayomi-scope-comic-{index}",
                series_title=f"Western Comic {index}",
                media_type="comic",
                publisher="Marvel",
                retry_after=NOW - 600 + index,
            )
        seed_queue(
            db_path,
            "queue-suwayomi-scope-manga-1",
            series_title="Frieren: Beyond Journey's End",
            media_type="manga",
            publisher="Shogakukan",
            retry_after=NOW - 100,
        )
        seed_queue(
            db_path,
            "queue-suwayomi-scope-manga-2",
            series_title="Delicious in Dungeon",
            media_type="manga",
            publisher="Yen Press",
            retry_after=NOW - 99,
        )

        before = counts(db_path)
        suwayomi_scoped = scheduler.source_worker_queue_plan(
            db_path,
            provider_ids=["suwayomi"],
            due_only=True,
            limit=2,
            now=NOW,
        )
        assert_equal(
            [row["queue_id"] for row in suwayomi_scoped["plans"]],
            ["queue-suwayomi-scope-manga-1", "queue-suwayomi-scope-manga-2"],
            "Suwayomi-filtered scheduler scans manga rows instead of spending its limit on comic scope blocks",
        )
        assert_equal(suwayomi_scoped["summary"]["eligible"], 2, "Suwayomi scoped rows are eligible")
        assert_equal(
            suwayomi_scoped["summary"]["selected_providers"],
            {"suwayomi": 2},
            "Suwayomi scoped plan selects the Suwayomi provider for manga rows",
        )

        exact_comic_probe = scheduler.source_worker_queue_plan(
            db_path,
            queue_ids=["queue-suwayomi-scope-comic-1"],
            provider_ids=["suwayomi"],
            include_blocked=True,
            limit=1,
            now=NOW,
        )
        assert_equal(
            len(exact_comic_probe["plans"]),
            1,
            "exact queue-id Suwayomi diagnostics still return scoped-out comic rows",
        )
        assert_equal(
            exact_comic_probe["plans"][0]["status"],
            "blocked_no_jobs",
            "exact queue-id Suwayomi diagnostics preserve scope-block evidence",
        )
        assert_equal(counts(db_path), before, "manga-scoped provider scan remains read-only")


def smoke_comic_scoped_provider_scan_skips_manga_window():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-scheduler-comic-scope-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_settings(db_path)
        for index in range(1, 5):
            seed_queue(
                db_path,
                f"queue-tl-scope-manga-{index}",
                series_title=f"Fairy Tail {index}",
                media_type="manga",
                publisher="Kodansha",
                retry_after=NOW - 600 + index,
            )
        seed_queue(
            db_path,
            "queue-tl-scope-comic-1",
            series_title="Spawn",
            media_type="comic",
            publisher="Image Comics",
            retry_after=NOW - 100,
        )
        seed_queue(
            db_path,
            "queue-tl-scope-comic-2",
            series_title="Gotham Central",
            media_type="comic",
            publisher="DC Comics",
            retry_after=NOW - 99,
        )

        before = counts(db_path)
        tl_scoped = scheduler.source_worker_queue_plan(
            db_path,
            provider_ids=["prowlarr_torrentleech_comics"],
            due_only=True,
            limit=2,
            now=NOW,
        )
        assert_equal(
            [row["queue_id"] for row in tl_scoped["plans"]],
            ["queue-tl-scope-comic-1", "queue-tl-scope-comic-2"],
            "TorrentLeech Comics-filtered scheduler scans comic rows instead of spending its limit on manga scope blocks",
        )
        assert_equal(tl_scoped["summary"]["eligible"], 2, "TorrentLeech Comics scoped rows are eligible")
        assert_equal(
            tl_scoped["summary"]["selected_providers"],
            {"prowlarr_torrentleech_comics": 2},
            "TorrentLeech Comics scoped plan selects the comic provider for comic rows",
        )

        exact_manga_probe = scheduler.source_worker_queue_plan(
            db_path,
            queue_ids=["queue-tl-scope-manga-1"],
            provider_ids=["prowlarr_torrentleech_comics"],
            include_blocked=True,
            limit=1,
            now=NOW,
        )
        assert_equal(
            len(exact_manga_probe["plans"]),
            1,
            "exact queue-id TorrentLeech Comics diagnostics still return manga rows",
        )
        exact_plan = exact_manga_probe["plans"][0]
        # TorrentLeech Comics now declares manga support for broad pack lanes, so
        # manga rows are intentionally eligible here. The remaining scope
        # invariant is that comic-only providers still block manga rows.
        assert_equal(
            exact_plan["status"],
            "eligible",
            "exact queue-id TorrentLeech Comics diagnostics preserve declared manga-pack eligibility",
        )
        exact_provider_plan = exact_plan["provider_attempt_plan"]
        assert_equal(len(exact_provider_plan), 1, "exact manga probe reports the scoped comic provider")
        assert_true(exact_provider_plan[0].get("selected"), "manga-capable TorrentLeech Comics provider is selected")
        assert_equal(counts(db_path), before, "comic-scoped provider scan remains read-only")


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-source-worker-scheduler-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        seed_settings(db_path)
        seed_queue(db_path, "queue-eligible", series_title="Eligible Book")
        seed_queue(db_path, "queue-handoff", series_title="Handoff Book")
        seed_active_handoff(db_path, "queue-handoff")
        seed_queue(db_path, "queue-stale-handoff", series_title="Stale Handoff Book")
        seed_active_handoff(db_path, "queue-stale-handoff", updated_at=NOW - 50000)
        seed_queue(db_path, "queue-provider-wait-marker", series_title="Provider Wait Marker Book")
        seed_provider_wait_marker(db_path, "queue-provider-wait-marker")
        seed_queue(
            db_path,
            "queue-provider-wait",
            series_title="Provider Wait Book",
            state="source_wait",
            display_phase="provider_wait",
            provider="prowlarr",
            provider_phase="provider_wait",
        )
        seed_queue(db_path, "queue-no-jobs", series_title="No Jobs Book")
        seed_queue(db_path, "queue-operator", series_title="Operator Book")
        seed_queue(db_path, "queue-boundary", series_title="Boundary Book")
        seed_queue(db_path, "queue-cooldown", series_title="Cooldown Book")
        seed_queue(db_path, "queue-searching-history", series_title="Searching History Book")
        seed_queue(db_path, "queue-terminal-history", series_title="Terminal History Book")
        seed_queue(
            db_path,
            "queue-latest-source-attempt",
            series_title="Moon Girl and Devil Dinosaur",
            retry_after=NOW + 5000,
        )
        seed_queue(
            db_path,
            "queue-mangadex-western",
            series_title="Absolute Batman",
            media_type="comic",
            publisher="DC Comics",
        )
        seed_queue(
            db_path,
            "queue-mangadex-no-signal",
            series_title="Avatar: The Last Airbender",
            media_type="comic",
            publisher=None,
        )
        seed_queue(
            db_path,
            "queue-mangadex-manga",
            series_title="Death Note",
            media_type="comic",
            publisher="Shueisha",
        )
        seed_queue(db_path, "queue-provider-timeout-circuit", series_title="Circuit Book")
        seed_recent_source_attempt(db_path, "queue-cooldown")
        seed_recent_source_attempt(
            db_path,
            "queue-searching-history",
            status="searching",
            lifecycle_phase="searching",
            outcome="in_progress",
        )
        seed_recent_source_attempt(
            db_path,
            "queue-terminal-history",
            status="searching",
            lifecycle_phase="searching",
            outcome="in_progress",
            attempt_id="attempt:queue-terminal-history:standard_ebooks:searching",
        )
        seed_recent_source_attempt(
            db_path,
            "queue-terminal-history",
            status="searched_no_candidates",
            lifecycle_phase="searched_no_candidates",
            outcome="no_candidate",
            attempt_id="attempt:queue-terminal-history:standard_ebooks:no-candidates",
        )
        seed_recent_source_attempt(
            db_path,
            "queue-latest-source-attempt",
            source="source_ladder",
            provider_id="source_ladder",
            status="no_candidate_retry",
            lifecycle_phase="searched_no_candidates",
            display_phase="no_candidate",
            outcome="no_candidate",
            failure_reason="automatic sources had no actionable candidate; retry scheduled",
            completed_at=NOW - 45,
            attempt_id="attempt:queue-latest-source-attempt:source-ladder:no-candidate",
        )
        seed_provider_timeout_attempt(db_path, "standard_ebooks", 1, completed_at=NOW - 120)
        seed_provider_timeout_attempt(db_path, "standard_ebooks", 2, completed_at=NOW - 60)
        seed_provider_timeout_attempt(db_path, "rss_getcomics", 1, completed_at=NOW - 120)
        seed_provider_timeout_attempt(db_path, "rss_getcomics", 2, completed_at=NOW - 60)
        seed_provider_timeout_attempt(db_path, "rss", 1, completed_at=NOW - 120)
        seed_provider_timeout_attempt(db_path, "rss", 2, completed_at=NOW - 60)
        seed_provider_timeout_attempt(db_path, "prowlarr_nyaa", 1, completed_at=NOW - 120)
        seed_provider_timeout_attempt(db_path, "prowlarr_nyaa", 2, completed_at=NOW - 60)
        seed_provider_fetch_failure_attempt(
            db_path,
            "prowlarr_tokyo_toshokan_manga",
            1,
            completed_at=NOW - 120,
        )
        seed_provider_fetch_failure_attempt(
            db_path,
            "prowlarr_tokyo_toshokan_manga",
            2,
            completed_at=NOW - 60,
        )
        seed_provider_health(db_path, "prowlarr", state="healthy", created_at=NOW - 30)

        before = counts(db_path)

        assert_equal(
            scheduler._provider_parent_keys("rss_getcomics"),
            [],
            "RSS feed rows do not inherit the parent RSS timeout circuit",
        )
        assert_equal(
            scheduler._provider_parent_keys("generic_rss_direct_feed"),
            [],
            "generic RSS rows do not inherit the parent RSS timeout circuit",
        )
        assert_equal(
            scheduler._job_timeout_circuit_keys({"provider_id": "rss_getcomics", "health_provider_ids": ["rss"]}),
            ["rss_getcomics"],
            "RSS feed health rollup does not become a shared timeout circuit",
        )
        assert_equal(
            scheduler._job_timeout_circuit_keys({"provider_id": "prowlarr_nyaa", "health_provider_ids": ["prowlarr"]}),
            ["prowlarr_nyaa"],
            "targeted Prowlarr rows keep timeout circuits per configured indexer",
        )

        rss_child_circuit = scheduler.provider_timeout_circuit_breakers(
            db_path,
            [{"provider_id": "rss_getcomics", "health_provider_ids": ["rss"]}],
            now=NOW,
            window_seconds=600,
            threshold=2,
            cooldown_seconds=900,
        )
        assert_equal(set(rss_child_circuit.keys()), {"rss_getcomics"}, "RSS feed timeout circuit is per feed")
        assert_equal(
            rss_child_circuit["rss_getcomics"]["circuit_provider_id"],
            "rss_getcomics",
            "RSS feed circuit evidence names the timed-out feed",
        )
        assert_equal(
            rss_child_circuit["rss_getcomics"]["recent_timeout_count"],
            2,
            "RSS feed circuit does not count base RSS timeouts",
        )
        generic_rss_circuit = scheduler.provider_timeout_circuit_breakers(
            db_path,
            [{"provider_id": "generic_rss_direct_feed", "health_provider_ids": ["rss"]}],
            now=NOW,
            window_seconds=600,
            threshold=2,
            cooldown_seconds=900,
        )
        assert_equal(generic_rss_circuit, {}, "base RSS timeouts do not cool an unrelated RSS feed instance")
        rss_base_circuit = scheduler.provider_timeout_circuit_breakers(
            db_path,
            [{"provider_id": "rss", "health_provider_ids": ["rss"]}],
            now=NOW,
            window_seconds=600,
            threshold=2,
            cooldown_seconds=900,
        )
        assert_equal(set(rss_base_circuit.keys()), {"rss"}, "base RSS provider can still cool itself")
        assert_equal(rss_base_circuit["rss"]["recent_timeout_count"], 2, "base RSS circuit only counts base RSS timeouts")
        prowlarr_child_circuit = scheduler.provider_timeout_circuit_breakers(
            db_path,
            [{"provider_id": "prowlarr_nyaa", "health_provider_ids": ["prowlarr"]}],
            now=NOW,
            window_seconds=600,
            threshold=2,
            cooldown_seconds=900,
        )
        assert_equal(
            set(prowlarr_child_circuit.keys()),
            {"prowlarr_nyaa"},
            "targeted Prowlarr timeout circuit cools only that configured indexer",
        )
        assert_equal(
            prowlarr_child_circuit["prowlarr_nyaa"]["circuit_provider_id"],
            "prowlarr_nyaa",
            "targeted Prowlarr circuit evidence names the configured indexer",
        )
        prowlarr_recovered_circuit = scheduler.provider_timeout_circuit_breakers(
            db_path,
            [{"provider_id": "prowlarr_nyaa", "health_provider_ids": ["prowlarr"]}],
            now=NOW,
            window_seconds=600,
            threshold=2,
            cooldown_seconds=900,
            provider_health_map={
                "prowlarr": {
                    "provider_id": "prowlarr",
                    "state": "healthy",
                    "label": "healthy",
                    "created_at": NOW - 30,
                    "health_scope": "provider_status",
                    "api_reachable": True,
                }
            },
        )
        assert_equal(
            prowlarr_recovered_circuit,
            {},
            "newer healthy Prowlarr status closes targeted child timeout circuit",
        )
        prowlarr_sibling_circuit = scheduler.provider_timeout_circuit_breakers(
            db_path,
            [{"provider_id": "prowlarr_torrentleech_comics", "health_provider_ids": ["prowlarr"]}],
            now=NOW,
            window_seconds=600,
            threshold=2,
            cooldown_seconds=900,
        )
        assert_equal(prowlarr_sibling_circuit, {}, "targeted Prowlarr timeout circuits do not cool sibling indexers")
        fetch_failure_circuit = scheduler.provider_fetch_failure_circuit_breakers(
            db_path,
            [{"provider_id": "prowlarr_tokyo_toshokan_manga", "health_provider_ids": ["prowlarr"]}],
            now=NOW,
            window_seconds=600,
            threshold=2,
            cooldown_seconds=900,
        )
        assert_equal(
            set(fetch_failure_circuit.keys()),
            {"prowlarr_tokyo_toshokan_manga"},
            "targeted Prowlarr fetch-failure circuit cools only the failing configured indexer",
        )
        assert_equal(
            fetch_failure_circuit["prowlarr_tokyo_toshokan_manga"]["recent_fetch_failure_count"],
            2,
            "fetch-failure circuit carries recent failure count",
        )
        assert_equal(
            fetch_failure_circuit["prowlarr_tokyo_toshokan_manga"]["failure_reason"],
            "http_request_failed",
            "fetch-failure circuit carries the provider failure reason",
        )
        fetch_failure_sibling_circuit = scheduler.provider_fetch_failure_circuit_breakers(
            db_path,
            [{"provider_id": "prowlarr_torrentleech_comics", "health_provider_ids": ["prowlarr"]}],
            now=NOW,
            window_seconds=600,
            threshold=2,
            cooldown_seconds=900,
        )
        assert_equal(
            fetch_failure_sibling_circuit,
            {},
            "targeted Prowlarr fetch-failure circuits do not cool sibling indexers",
        )

        eligible = plan_one(db_path, "queue-eligible", provider_ids=["standard_ebooks"])
        assert_equal(eligible["status"], "eligible", "standard source row is eligible")
        assert_equal(eligible["selected_provider_ids"], ["standard_ebooks"], "eligible source selected")
        assert_false(eligible["active_handoff_count"], "eligible row has no handoff")
        eligible_providers = provider_plan_by_id(eligible)
        assert_equal(eligible_providers["standard_ebooks"]["attempt_state"], "selected", "eligible provider attempt plan marks selection")
        assert_true(eligible_providers["standard_ebooks"]["selected"], "eligible provider attempt plan has selected flag")
        assert_equal(eligible["provider_attempt_counts"].get("selected"), 1, "eligible provider attempt count")

        searching_history = plan_one(db_path, "queue-searching-history", provider_ids=["standard_ebooks"])
        assert_equal(
            searching_history["source_worker_provider_attempt_counts"].get("standard_ebooks"),
            1,
            "searching source attempt is still counted in all provider history",
        )
        assert_equal(
            searching_history["source_worker_terminal_provider_attempt_counts"].get("standard_ebooks"),
            0,
            "searching source attempt is excluded from terminal provider history",
        )

        terminal_history = plan_one(db_path, "queue-terminal-history", provider_ids=["standard_ebooks"])
        assert_equal(
            terminal_history["source_worker_provider_attempt_counts"].get("standard_ebooks"),
            2,
            "mixed source attempts count all provider history",
        )
        assert_equal(
            terminal_history["source_worker_terminal_provider_attempt_counts"].get("standard_ebooks"),
            1,
            "mixed source attempts count only terminal provider history",
        )
        latest_attempt = plan_one(db_path, "queue-latest-source-attempt", provider_ids=["standard_ebooks"])
        assert_equal(
            latest_attempt["latest_source_attempt"].get("source"),
            "source_ladder",
            "scheduler plan exposes latest source-attempt source evidence",
        )
        assert_equal(
            latest_attempt["latest_source_attempt"].get("status"),
            "no_candidate_retry",
            "scheduler plan exposes latest source-attempt status evidence",
        )
        assert_equal(
            latest_attempt["latest_source_attempt"].get("failure_reason"),
            "automatic sources had no actionable candidate; retry scheduled",
            "scheduler plan exposes latest source-attempt failure evidence",
        )

        mixed_health = scheduler._classify_queue_plan(
            {},
            [
                {"provider_id": "comicscodes", "job_status": "provider_wait", "reason": "ComicsCodes backoff"},
                {"provider_id": "mangadex", "job_status": "ready"},
            ],
            [],
            now=NOW,
        )
        assert_equal(mixed_health["status"], "eligible", "ready provider wins over unrelated provider wait")
        assert_equal(
            scheduler._selected_provider_ids(mixed_health["selected_jobs"]),
            ["mangadex"],
            "provider wait source does not suppress ready source selection",
        )

        future_row_wait = scheduler._classify_queue_plan(
            {
                "provider_status_state": "provider_wait",
                "provider_status_provider": "prowlarr",
                "retry_after": NOW + 300,
            },
            [{"provider_id": "mangadex", "job_status": "ready"}],
            [],
            now=NOW,
        )
        assert_equal(future_row_wait["status"], "waiting_for_retry", "future row-level provider wait still blocks source work")
        assert_equal(
            scheduler._selected_provider_ids(future_row_wait["selected_jobs"]),
            [],
            "future provider-wait row does not select alternate sources early",
        )
        failed_handoff_future_retry = scheduler._classify_queue_plan(
            {
                "provider_status_provider": "qbittorrent",
                "retry_after": NOW + 300,
            },
            [{"provider_id": "prowlarr_torrentleech_comics", "job_status": "ready"}],
            [],
            now=NOW,
            retryable_failed_handoffs=[
                {
                    "id": "failed-handoff:weekly-pack",
                    "download_client": "qBittorrent",
                    "status": "failed_download",
                    "state": "failed",
                    "retry_eligible": 1,
                }
            ],
        )
        assert_equal(
            failed_handoff_future_retry["status"],
            "eligible",
            "retryable download-client handoff failure can bypass generic retry wait",
        )
        assert_equal(
            scheduler._selected_provider_ids(failed_handoff_future_retry["selected_jobs"]),
            ["prowlarr_torrentleech_comics"],
            "retryable handoff recovery selects the ready concrete indexer",
        )

        mature_row_wait = scheduler._classify_queue_plan(
            {
                "provider_status_state": "provider_wait",
                "provider_status_provider": "prowlarr",
                "retry_after": NOW - 60,
            },
            [{"provider_id": "mangadex", "job_status": "ready"}],
            [],
            now=NOW,
        )
        assert_equal(mature_row_wait["status"], "eligible", "mature row-level provider wait can use ready alternate sources")
        assert_equal(
            scheduler._selected_provider_ids(mature_row_wait["selected_jobs"]),
            ["mangadex"],
            "mature provider-wait row selects ready alternate source",
        )

        concrete_prowlarr = scheduler._classify_queue_plan(
            {},
            [
                {"provider_id": "prowlarr", "job_status": "ready"},
                {"provider_id": "prowlarr_torrentleech_comics", "job_status": "ready"},
                {"provider_id": "rss", "job_status": "ready"},
            ],
            [],
            now=NOW,
        )
        assert_equal(concrete_prowlarr["status"], "eligible", "concrete Prowlarr jobs stay eligible")
        assert_equal(
            scheduler._selected_provider_ids(concrete_prowlarr["selected_jobs"]),
            ["prowlarr_torrentleech_comics", "rss"],
            "aggregate Prowlarr is fallback when a concrete Prowlarr lane is ready",
        )

        concrete_prowlarr_cooldown = scheduler._classify_queue_plan(
            {},
            [{"provider_id": "prowlarr", "job_status": "ready"}],
            [],
            now=NOW,
            attempt_cooldowns={
                "prowlarr_torrentleech_comics": {
                    "provider_id": "prowlarr_torrentleech_comics",
                    "remaining_seconds": 300,
                }
            },
            cooled_jobs=[{"provider_id": "prowlarr_torrentleech_comics", "job_status": "ready"}],
        )
        assert_equal(
            concrete_prowlarr_cooldown["status"],
            "waiting_for_retry",
            "aggregate Prowlarr cannot bypass a concrete Prowlarr cooldown",
        )
        assert_equal(
            scheduler._selected_provider_ids(concrete_prowlarr_cooldown["selected_jobs"]),
            [],
            "cooled concrete Prowlarr lane selects no broad fallback attempt",
        )

        mangadex_western = plan_one(
            db_path,
            "queue-mangadex-western",
            provider_ids=["mangadex"],
            include_blocked=True,
        )
        assert_equal(mangadex_western["status"], "blocked_no_jobs", "MangaDex skips clear western-comic rows")
        assert_equal(mangadex_western["job_status_counts"].get("blocked"), 1, "MangaDex scope block is visible")
        assert_equal(mangadex_western["selected_provider_ids"], [], "western row does not select MangaDex")
        mangadex_western_providers = provider_plan_by_id(mangadex_western)
        assert_equal(mangadex_western_providers["mangadex"]["attempt_state"], "blocked", "MangaDex scope block is explicit provider evidence")
        assert_true("DC Comics" in mangadex_western_providers["mangadex"]["reason"], "MangaDex provider evidence names scope reason")
        assert_false(mangadex_western_providers["mangadex"].get("selected"), "MangaDex blocked provider is not selected")

        mangadex_no_signal = plan_one(
            db_path,
            "queue-mangadex-no-signal",
            provider_ids=["mangadex"],
            include_blocked=True,
        )
        assert_equal(mangadex_no_signal["status"], "blocked_no_jobs", "MangaDex skips rows without manga metadata signals")
        assert_equal(mangadex_no_signal["job_status_counts"].get("blocked"), 1, "MangaDex no-signal scope block is visible")
        assert_equal(mangadex_no_signal["selected_provider_ids"], [], "no-signal row does not select MangaDex")

        mangadex_manga = plan_one(db_path, "queue-mangadex-manga", provider_ids=["mangadex"])
        assert_equal(mangadex_manga["status"], "eligible", "MangaDex stays eligible for manga-publisher rows")
        assert_equal(mangadex_manga["selected_provider_ids"], ["mangadex"], "manga row selects MangaDex")

        handoff = plan_one(db_path, "queue-handoff", provider_ids=["standard_ebooks"])
        assert_equal(handoff["status"], "active_handoff", "active handoff blocks new source work")
        assert_equal(handoff["active_handoff_count"], 1, "active handoff is visible")
        assert_equal(handoff["selected_provider_ids"], [], "handoff row selects no new providers")

        provider_wait_marker = plan_one(db_path, "queue-provider-wait-marker", provider_ids=["standard_ebooks"])
        assert_equal(provider_wait_marker["status"], "eligible", "provider-wait marker without download client does not block source work")
        assert_equal(provider_wait_marker["active_handoff_count"], 0, "provider-wait marker is not an active handoff")
        assert_equal(provider_wait_marker["stale_handoff_count"], 0, "provider-wait marker is not stale handoff evidence")
        assert_equal(
            provider_wait_marker["selected_provider_ids"],
            ["standard_ebooks"],
            "ready provider runs despite unrelated provider-wait marker",
        )

        stale_handoff = plan_one(db_path, "queue-stale-handoff", provider_ids=["standard_ebooks"])
        assert_equal(stale_handoff["status"], "eligible", "stale handoff no longer blocks source work")
        assert_equal(stale_handoff["active_handoff_count"], 0, "stale handoff is not active")
        assert_equal(stale_handoff["stale_handoff_count"], 1, "stale handoff is still visible")
        assert_equal(stale_handoff["selected_provider_ids"], ["standard_ebooks"], "stale handoff row can retry source work")

        cooldown = plan_one(
            db_path,
            "queue-cooldown",
            provider_ids=["standard_ebooks"],
            attempt_cooldown_seconds=3600,
        )
        assert_equal(cooldown["status"], "waiting_for_retry", "recent source attempt cooldown blocks rerun")
        assert_equal(cooldown["blocker"], "source-worker cooldown", "cooldown blocker label")
        assert_equal(cooldown["source_worker_cooldown_count"], 1, "cooldown exposes cooled job count")
        assert_equal(cooldown["selected_provider_ids"], [], "cooldown row selects no providers")
        cooldown_providers = provider_plan_by_id(cooldown)
        assert_equal(cooldown_providers["standard_ebooks"]["attempt_state"], "recent_attempt_cooldown", "cooldown provider evidence is explicit")
        assert_equal(cooldown_providers["standard_ebooks"]["last_attempt_status"], "searched_no_candidates", "cooldown evidence carries last attempt status")
        assert_true(cooldown_providers["standard_ebooks"]["remaining_cooldown_seconds"] > 0, "cooldown evidence carries remaining time")

        circuit = plan_one(
            db_path,
            "queue-provider-timeout-circuit",
            provider_ids=["standard_ebooks"],
            provider_timeout_window_seconds=600,
            provider_timeout_threshold=2,
            provider_timeout_cooldown_seconds=900,
        )
        assert_equal(circuit["status"], "waiting_for_retry", "provider timeout circuit blocks rerun across queue rows")
        assert_equal(circuit["blocker"], "provider outage/provider wait", "provider timeout circuit uses provider wait blocker")
        assert_equal(circuit["source_worker_cooldown_count"], 1, "provider timeout circuit exposes cooled job count")
        assert_equal(circuit["provider_timeout_circuit_count"], 1, "provider timeout circuit count is explicit")
        assert_equal(circuit["selected_provider_ids"], [], "provider timeout circuit selects no providers")
        circuit_providers = provider_plan_by_id(circuit)
        assert_equal(circuit_providers["standard_ebooks"]["attempt_state"], "provider_timeout_circuit", "provider circuit evidence is explicit")
        assert_equal(circuit_providers["standard_ebooks"]["recent_timeout_count"], 2, "provider circuit carries recent timeout count")
        assert_true(circuit_providers["standard_ebooks"]["remaining_cooldown_seconds"] > 0, "provider circuit carries remaining time")

        provider_wait = plan_one(db_path, "queue-provider-wait", provider_ids=["standard_ebooks"])
        assert_equal(provider_wait["status"], "eligible", "mature provider wait can retry ready source work")
        assert_equal(provider_wait["selected_provider_ids"], ["standard_ebooks"], "mature provider wait selects ready provider")

        no_jobs = plan_one(db_path, "queue-no-jobs", provider_ids=["gutendex"])
        assert_equal(no_jobs["status"], "blocked_no_jobs", "disabled planned provider produces no jobs")
        assert_equal(no_jobs["jobs_available"], 0, "disabled planned provider unavailable")
        assert_true(no_jobs["all_jobs_available"] >= 1, "disabled planned provider still has diagnostic evidence")
        assert_equal(no_jobs["provider_attempt_counts"].get("blocked"), 1, "disabled planned provider reports blocked evidence")

        search_source = plan_one(db_path, "queue-operator", provider_ids=["generic_reader_page_pack_source"])
        assert_equal(search_source["status"], "eligible", "configured reader search source is eligible")
        assert_equal(search_source["selected_provider_ids"], ["generic_reader_page_pack_source"], "reader search source selected")

        boundary = plan_one(db_path, "queue-boundary", provider_ids=["private_trackers"])
        assert_equal(boundary["status"], "blocked_no_jobs", "disabled boundary source cannot become eligible")
        assert_equal(boundary["selected_provider_ids"], [], "boundary source never selected")

        due = scheduler.source_worker_queue_plan(
            db_path,
            provider_ids=["standard_ebooks"],
            due_only=True,
            limit=20,
            attempt_cooldown_seconds=3600,
            provider_timeout_window_seconds=600,
            provider_timeout_threshold=2,
            provider_timeout_cooldown_seconds=900,
            now=NOW,
        )
        assert_equal(due["summary"]["eligible"], 0, "due planner cools standard rows while provider timeout circuit is open")
        assert_equal(due["summary"]["active_handoff"], 1, "due planner counts active handoff")
        assert_equal(due["summary"]["stale_handoffs"], 1, "due planner reports stale handoff evidence")
        assert_equal(due["summary"]["provider_wait"], 1, "due planner counts provider wait")
        assert_equal(due["summary"]["waiting_for_retry"], 13, "due planner counts provider circuit rows as waiting for retry")
        assert_equal(due["summary"]["source_worker_cooldowns"], 15, "due planner summarizes provider-cooled jobs")
        assert_equal(due["summary"]["provider_timeout_circuits"], 15, "due planner summarizes provider timeout circuits")
        assert_false(due["summary"]["selected_providers"], "provider circuit suppresses selected providers")
        assert_equal(due["summary"]["provider_attempt_states"].get("provider_timeout_circuit"), 15, "due planner summarizes provider circuit evidence")

        after = counts(db_path)
        assert_equal(after, before, "scheduler performs no DB mutation")

    smoke_retryable_handoff_recovery_due_plan()
    smoke_retryable_stage_failure_unblocks_page_pack_handoff()
    smoke_provider_timeout_recovery_health_plan()
    smoke_comicscodes_assist_source_can_be_scheduled()
    smoke_prowlarr_aggregate_timeout_hidden_by_concrete_child()
    smoke_provider_fetch_failure_sibling_selection()
    smoke_runtime_budget_skip_does_not_cool_provider()
    smoke_manga_scoped_provider_scan_skips_comic_window()
    smoke_comic_scoped_provider_scan_skips_manga_window()
    ok("source worker scheduler plans queue movement from Arr-style provider settings without mutation")


if __name__ == "__main__":
    main()
