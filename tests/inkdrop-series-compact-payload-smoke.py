#!/usr/bin/env python3
"""Guard the Series compact endpoint/read model against heavy first-paint payloads."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import inkdrop_state
import inkdrop_web_state_views


def insert_fixture(db_path: Path, count: int = 140) -> None:
    now = time.time()
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        for index in range(1, count + 1):
            series_id = f"comicvine:{100000 + index}"
            raw = {
                "description": "Long provider description. " * 180,
                "deck": "Long deck copy. " * 120,
                "image": {
                    "small_url": f"https://comicvine.gamespot.com/a/uploads/scale_small/{index}/cover.jpg",
                    "super_url": f"https://comicvine.gamespot.com/a/uploads/original/{index}/cover.jpg",
                },
                "site_detail_url": f"https://comicvine.gamespot.com/series-{index}/",
                "count_of_issues": 24,
            }
            con.execute(
                """
                insert into series (
                  id, title, sort_title, media_type, year, publisher,
                  metadata_provider, metadata_id, source, library_path,
                  monitored, monitor_new, auto_grab, created_at, updated_at, raw_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 1, ?, ?, ?)
                """,
                (
                    series_id,
                    f"Smoke Series {index:03d}",
                    f"smoke series {index:03d}",
                    "comic",
                    2026,
                    "Smoke Publisher",
                    "comicvine",
                    str(100000 + index),
                    "comicvine",
                    f"Smoke Series {index:03d}",
                    now,
                    now,
                    json.dumps(raw),
                ),
            )
            if index % 2 == 0:
                wanted_id = f"wanted:{index}"
                queue_id = f"queue:{index}"
                con.execute(
                    "insert into wanted_items (id, series_id, status, created_at, updated_at) values (?, ?, 'wanted', ?, ?)",
                    (wanted_id, series_id, now, now),
                )
                con.execute(
                    """
                    insert into queue_items (
                      id, wanted_id, series_id, state, current_source, query,
                      last_event, active, created_at, updated_at
                    )
                    values (?, ?, ?, 'queued', 'prowlarr', ?, 'queued for smoke', 1, ?, ?)
                    """,
                    (queue_id, wanted_id, series_id, f"Smoke Series {index:03d}", now, now),
                )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="inkdrop-series-compact-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        insert_fixture(db_path)
        route_payload = inkdrop_web_state_views.series_compact_view(
            db_path,
            limit=80,
            series_filter="all",
            summary_mode="compact",
            row_mode="compact",
        )
        assert route_payload.get("ok") is True, route_payload
        assert route_payload.get("focused") is False, route_payload
        assert route_payload.get("loaded_count") == 80, route_payload
        assert route_payload.get("total_count") == 140, route_payload
        assert route_payload.get("has_more") is True, route_payload
        assert len(route_payload.get("rows") or []) == 80, route_payload
        all_rows = inkdrop_state.series_compact_card_rows(db_path, 5000, series_filter="all")
        rows = all_rows[:120]
        payload = {
            "ok": True,
            "view": "series",
            "summary": {
                "summary_mode": "compact",
                "series": len(all_rows),
                "active_queue_items": sum(int(row.get("active_queue_count") or 0) for row in all_rows),
            },
            "row_mode": "compact",
            "count": len(rows),
            "loaded_count": len(rows),
            "total_count": len(all_rows),
            "has_more": len(all_rows) > len(rows),
            "limit": 120,
            "rows": inkdrop_state.compact_state_view_rows("series", rows, "compact"),
            "lightweight_rows": True,
        }

    rows = payload.get("rows") if isinstance(payload, dict) else None
    assert payload.get("ok") is True, payload
    assert payload.get("lightweight_rows") is True, payload
    assert payload.get("row_mode") == "compact", payload
    assert payload.get("has_more") is True, payload
    assert isinstance(rows, list) and len(rows) == 120, payload
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    assert len(encoded) < 180_000, len(encoded)
    forbidden = {
        "description",
        "deck",
        "reader_visibility_note",
        "reader_visibility_next_action",
        "reader_visibility_provider_counts",
        "path_contract",
        "ownership_evidence",
    }
    for row in rows:
        present = forbidden.intersection(row)
        assert not present, (present, row)
        assert row.get("title"), row
        assert row.get("image"), row
        assert row.get("metadata_provider") == "comicvine", row
    web_source = Path("inkdrop_web.py").read_text(encoding="utf-8")
    view_source = Path("inkdrop_web_state_views.py").read_text(encoding="utf-8")
    state_source = Path("inkdrop_state.py").read_text(encoding="utf-8")
    assert "import inkdrop_web_state_views" in web_source, "web compact Series path should delegate payload shaping to inkdrop_web_state_views"
    assert "inkdrop_web_state_views.series_compact_view(" in web_source, "web compact Series endpoint is not wired to the extracted helper"
    assert "def series_compact_view(" in view_source, "extracted compact Series helper is missing"
    assert "series_compact_card_rows" in view_source, "compact Series helper is not wired to the lightweight reader"
    assert "lightweight_rows" in view_source, "compact Series helper does not expose lightweight row evidence"
    assert "state_view_operational_table_summary(db_path, view=\"series\")" in view_source, "compact Series helper should use the fast operational summary"
    assert "all_rows = row_reader(INKDROP_STATE_DB, 5000" not in web_source, "compact Series first paint must not hydrate every Series row"
    assert "series_filter=duplicate_titles&limit=200&summary=compact&rows=compact" in web_source, "duplicate workload fetch must use compact bounded Series rows"
    assert "series_filter=duplicate_titles&limit=5000" not in web_source, "duplicate workload fetch regressed to the full heavy Series path"
    assert 'variant === "poster" || variant === "table"' in web_source, "poster/table covers should prefer direct trusted images instead of hammering the web proxy"
    assert "STATE_ENDPOINT_SEMAPHORE" in web_source, "state endpoints should be concurrency-gated to protect SQLite from UI request stampedes"
    assert "STATE_ENDPOINT_SEMAPHORE.acquire(timeout=12)" in web_source, "state endpoint gate should briefly wait instead of immediately bouncing UI requests"
    assert "self.send_header(\"Connection\", \"close\")" in web_source, "web responses should close connections after each request"
    assert "self.send_redirect(direct_url)" not in web_source, "cover endpoint should serve the disk-backed cover cache instead of redirecting trusted covers"
    assert "X-InkDrop-Cover-Cache" in web_source, "cover endpoint should expose disk-cache hit/miss diagnostics"
    assert "def public_comic_watches_fast_payload" in web_source, "legacy ComicVine watch reads should use compact InkDrop Series rows"
    assert "self.send_json(public_comic_watches_fast_payload())" in web_source, "legacy ComicVine watch route should not rebuild heavy watch context"
    assert "parsed.searchParams.delete(\"rows\")" not in web_source, "Series fallback must not remove compact row mode and trigger heavy hydration"
    assert "raw_total_count = filter_counts.get(series_filter)" in view_source, "compact Series endpoint should distinguish raw filter count from display-row count"
    assert "min(int(limit or 80), 5000)" in view_source, "compact Series endpoint should allow full-library UI loads"
    assert "if not focus_entities:\n        fetch_limit = 5000" not in view_source, "normal Series compact route must honor caller pagination"
    assert "rows = filtered_rows[:fetch_limit]" in view_source, "compact Series route must bound returned rows"
    assert "total_count = int(raw_total_count" in view_source, "compact Series route should preserve the full filtered count while paginating"
    assert "def queue_wanted_compact_policy(" in view_source, "Queue/Wanted first-paint policy should live outside the web monolith"
    assert "def query_summary_mode(" in view_source, "state-view summary query parsing should live outside the web monolith"
    assert "def query_row_mode(" in view_source, "state-view row-mode query parsing should live outside the web monolith"
    assert "def focus_from_query(" in view_source, "state-view focus query parsing should live outside the web monolith"
    assert "return inkdrop_web_state_views.query_summary_mode(query)" in web_source, "web summary-mode helper should delegate to the extracted view helper"
    assert "return inkdrop_web_state_views.query_row_mode(query)" in web_source, "web row-mode helper should delegate to the extracted view helper"
    assert "return inkdrop_web_state_views.focus_from_query(query)" in web_source, "web focus helper should delegate to the extracted view helper"
    assert "broad_compact_load = (" in view_source, "compact Queue/Wanted policy should identify broad lightweight loads"
    assert '"run_settlement": not broad_compact_load' in view_source, "compact Queue/Wanted first paint should skip best-effort settlement work"
    assert '"live_slskd": not broad_compact_load' in view_source, "compact Queue/Wanted first paint should not do live SLSKD health checks"
    assert '"live_prowlarr": not broad_compact_load' in view_source, "compact Queue/Wanted first paint should not do live Prowlarr health checks"
    assert '"live_download_clients": not broad_compact_load' in view_source, "compact Queue/Wanted first paint should not do live SAB/qBit health checks"
    assert "queue_wanted_policy = inkdrop_web_state_views.queue_wanted_compact_policy(" in web_source, "web route should delegate Queue/Wanted first-paint policy"
    assert "queue_wanted_policy.get(\"run_settlement\", True)" in web_source, "web route should use extracted settlement policy"
    assert "queue_wanted_policy.get(\"live_slskd\")" in web_source, "web route should use extracted SLSKD health policy"
    assert "def series_provider_wait_counts(db_path, series_ids=None)" in state_source, "Series provider-wait rollups should support bounded first-paint scopes"
    assert "def series_review_attention_counts(db_path, series_ids=None)" in state_source, "Series review rollups should support bounded first-paint scopes"
    assert "row_series_ids = [row[\"id\"] for row in rows]" in state_source, "compact Series should select bounded rows before extra rollups"
    assert "series_provider_wait_counts(db_path, row_series_ids)" in state_source, "compact Series provider-wait counts should be scoped to returned rows"
    assert "series_review_attention_counts(db_path, row_series_ids)" in state_source, "compact Series review counts should be scoped to returned rows"
    assert "duplicate_series_title_ids" in state_source, "duplicate Series filter should use a lightweight title-id scan"
    assert "state_view_operational_table_summary(db_path, view=\"sections\")" in state_source, "sections shell should use the fast operational summary"
    assert "row_mode_key in COMPACT_ROW_MODES" in state_source, "compact Queue/Wanted/Manual Review views should use the fast operational summary"
    assert "include_queue_media_preview = normalize_state_view_row_mode(row_mode) not in COMPACT_ROW_MODES" in state_source, "compact Queue first paint should not compute media-management previews"
    assert "select queue_id, count(*) as attempt_count" not in state_source, "Queue first-paint SQL should not build a whole-table source-attempt rollup"
    assert "select wanted_id, count(*) as attempt_count" not in state_source, "Wanted first-paint SQL should not build a whole-table source-attempt rollup"
    queue_rows_source = state_source[state_source.index("def queue_rows("):state_source.index("QUEUE_WORK_STATES =")]
    assert "where sa_count.queue_id = q.id" in queue_rows_source, "Queue rows should count source attempts by queue item, not broad issue history"
    assert "where sa_count.issue_id = i.id" not in queue_rows_source, "Queue first-paint attempt counts should not scan broad issue history"
    print(json.dumps({"ok": True, "rows": len(rows), "payload_bytes": len(encoded)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
