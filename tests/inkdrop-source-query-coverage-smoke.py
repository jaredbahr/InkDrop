#!/usr/bin/env python3
"""Focused regression for MangaDex rotation and HTML-source query variants."""

import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from core import inkdrop_source_worker_adapters as adapters
from core import inkdrop_source_worker_jobs as jobs


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


WANTED = {
    "queue_id": "queue:manga:rotation",
    "series_title": "Example Manga",
    "series": "Example Manga",
    "query": "Example Manga",
    "issue_number": "7",
    "volume_number": "7",
    "unit_type": "volume",
    "media_type": "manga",
}
POLICY = {
    "max_query_variants": 3,
    "mangadex_max_query_variants": 3,
    "search_url_templates": ["https://source.example/search?q={query}"],
}


def manga_rotation_contract(tmp):
    row = {"provider_id": "mangadex", "adapter_family": "mangadex_api", "policy": dict(POLICY)}
    plan = {
        "provider_id": "mangadex",
        "adapter_family": "mangadex_api",
        "adapter_id": "mangadex_api",
        "can_search": True,
        "schedule_state": "ready",
    }
    initial_job = jobs.source_job_for_row(row, plan, WANTED)
    initial_queries = list(initial_job["fetch_plan"]["query_variants"])
    query_pool = list(initial_job["fetch_plan"]["mangadex_query_pool"])
    require(len(initial_queries) == 3, initial_queries)
    require(len(query_pool) == 6, query_pool)
    require(initial_queries == query_pool[:3], (initial_queries, query_pool))

    db_path = Path(tmp) / "state.sqlite3"
    with closing(sqlite3.connect(db_path)) as con:
        con.execute(
            """create table source_attempts(
                id text primary key, queue_id text, provider_id text, source text, provider text,
                started_at real, completed_at real, raw_json text
            )"""
        )

    def replace_attempt(attempt_id, stamp, fetch, *, keep_existing=False):
        with closing(sqlite3.connect(db_path)) as con:
            if not keep_existing:
                con.execute("delete from source_attempts")
            con.execute(
                "insert into source_attempts values(?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    WANTED["queue_id"],
                    "mangadex",
                    "mangadex",
                    "MangaDex",
                    stamp,
                    stamp,
                    json.dumps({"fetch": fetch}) if fetch is not None else json.dumps({}),
                ),
            )
            con.commit()

    def zero_counts(queries):
        return [{"query": query, "results": 0, "matching_manga": 0} for query in queries]

    def rotation():
        return jobs._mangadex_query_rotation(str(db_path), WANTED, query_pool)

    replace_attempt(
        "attempt:first-window-zero",
        1.0,
        {
            "mangadex_query_pool": query_pool,
            "query_variants": initial_queries,
            "variant_result_counts": zero_counts(initial_queries),
        },
    )

    requests_seen = []

    def empty_mangadex(request):
        requests_seen.append(request)
        return {"json": {"data": []}, "headers": {"Content-Type": "application/json"}, "status_code": 200}

    result = jobs.run_source_job(
        initial_job,
        http_get=empty_mangadex,
        source_memory_db_path=str(db_path),
    )
    rotated = result["fetch"]["fetch_plan"]["query_variants"]
    require(rotated == query_pool[3:6], (query_pool, rotated))
    require(requests_seen[0]["params"]["title"] == query_pool[3], requests_seen[0])
    rotation_result = result["fetch"].get("mangadex_query_rotation") or {}
    require(rotation_result.get("previous_attempt_id") == "attempt:first-window-zero", rotation_result)
    require(rotation_result.get("advanced_by") == 3, rotation_result)

    second_window = query_pool[3:6]
    replace_attempt(
        "attempt:second-window-zero",
        2.0,
        {
            "mangadex_query_pool": query_pool,
            "query_variants": second_window,
            "variant_result_counts": zero_counts(second_window),
        },
    )
    require(rotation().get("offset") == 0, rotation())

    replace_attempt(
        "attempt:partial-zero",
        3.0,
        {
            "mangadex_query_pool": query_pool,
            "query_variants": initial_queries,
            "variant_result_counts": zero_counts(initial_queries[:1]),
        },
    )
    require(rotation().get("offset") == 1, rotation())

    for attempt_id, extra in (
        (
            "attempt:productive",
            {"variant_result_counts": [{"query": second_window[0], "results": 1, "matching_manga": 1}]},
        ),
        (
            "attempt:failed",
            {
                "variant_result_counts": zero_counts(second_window[:1]),
                "partial_errors": [{"stage": "mangadex_search", "error": "timeout"}],
            },
        ),
    ):
        replace_attempt(
            attempt_id,
            4.0,
            {"mangadex_query_pool": query_pool, "query_variants": second_window, **extra},
        )
        decision = rotation()
        require(decision.get("offset") == 3 and decision.get("advanced_by") == 0, decision)

    older = {
        "mangadex_query_pool": query_pool,
        "query_variants": initial_queries,
        "variant_result_counts": zero_counts(initial_queries),
    }
    replace_attempt("attempt:older-rotatable", 5.0, older)
    replace_attempt(
        "attempt:newest-changed-pool",
        6.0,
        {
            "mangadex_query_pool": list(reversed(query_pool)),
            "query_variants": second_window,
            "variant_result_counts": zero_counts(second_window),
        },
        keep_existing=True,
    )
    require(rotation().get("reason") == "query_pool_changed", rotation())

    replace_attempt("attempt:older-rotatable", 7.0, older)
    replace_attempt("attempt:newest-missing-fetch", 8.0, None, keep_existing=True)
    require(rotation() == {}, rotation())

    replace_attempt("attempt:older-rotatable", 9.0, older)
    replace_attempt(
        "attempt:newest-malformed",
        10.0,
        {
            "mangadex_query_pool": query_pool,
            "query_variants": second_window,
            "variant_result_counts": {"query": second_window[0]},
        },
        keep_existing=True,
    )
    malformed = rotation()
    require(malformed.get("offset") == 3 and malformed.get("advanced_by") == 0, malformed)


def html_adapter_variant_contract():
    expected = adapters.indexer_source_queries(WANTED, max_queries=3, policy=POLICY)
    require(len(expected) == 3, expected)
    families = {
        "direct_file_html_search": adapters.direct_file_html_search_requests,
        "direct_file_detail_search": adapters.direct_file_detail_search_requests,
        "direct_file_probe_source": adapters.direct_file_probe_search_requests,
        "torrent_html_search": adapters.torrent_html_search_requests,
        "torrent_detail_search": adapters.torrent_detail_search_requests,
    }
    for family, factory in families.items():
        row = {"provider_id": f"fixture:{family}", "adapter_family": family, "policy": dict(POLICY)}
        plan = {"provider_id": row["provider_id"], "adapter_family": family, "can_search": True}
        requests = factory(row, plan, WANTED)
        require([request.get("query_variant") for request in requests] == expected, (family, requests, expected))
        require([request.get("query_variant_index") for request in requests] == [0, 1, 2], requests)
        fetch_plan = adapters.adapter_fetch_plan(row, plan, WANTED)
        require(fetch_plan.get("query_variants") == expected, (family, fetch_plan))
        require(len(fetch_plan.get("requests") or []) == 3, (family, fetch_plan))

    bounded_row = {
        "provider_id": "fixture:bounded",
        "adapter_family": "torrent_html_search",
        "policy": {**POLICY, "torrent_html_max_query_variants": 1},
    }
    bounded = adapters.torrent_html_search_requests(bounded_row, {}, WANTED)
    require(len(bounded) == 1 and bounded[0]["query_variant"] == expected[0], bounded)


with tempfile.TemporaryDirectory() as tmp:
    manga_rotation_contract(tmp)
html_adapter_variant_contract()
print("OK: source query coverage")
