#!/usr/bin/env python3
"""Regression guard for provider-filtered queue rows preserving source evidence."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from pathlib import Path

from core import inkdrop_state


def fail(message: str) -> None:
    raise SystemExit(f"PROVIDER_QUEUE_FILTER_EVIDENCE_FAIL: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def seed_queue_row(
    con: sqlite3.Connection,
    *,
    suffix: str,
    provider_id: str,
    provider: str,
    title: str,
) -> None:
    now = time.time()
    series_id = f"series-{suffix}"
    issue_id = f"issue-{suffix}"
    wanted_id = f"wanted-{suffix}"
    queue_id = f"queue-{suffix}"
    attempt_id = f"attempt-{suffix}"
    con.execute(
        """
        insert into series(id, title, media_type, metadata_provider, metadata_id, source, monitored, monitor_new, auto_grab, created_at, updated_at, raw_json)
        values (?, ?, 'comic', 'comicvine', ?, 'comicvine', 1, 1, 1, ?, ?, '{}')
        """,
        (series_id, title, suffix, now, now),
    )
    con.execute(
        """
        insert into issues(id, series_id, issue_number, normalized_number, title, metadata_provider, metadata_id, monitored, created_at, updated_at, raw_json)
        values (?, ?, '1', '1', 'Issue 1', 'comicvine', ?, 1, ?, ?, '{}')
        """,
        (issue_id, series_id, suffix, now, now),
    )
    con.execute(
        """
        insert into wanted_items(id, series_id, issue_id, reason, status, priority, created_at, updated_at, raw_json)
        values (?, ?, ?, 'missing', 'wanted', 50, ?, ?, '{}')
        """,
        (wanted_id, series_id, issue_id, now, now),
    )
    con.execute(
        """
        insert into queue_items(
            id, wanted_id, series_id, issue_id, state, current_source, query, last_event, active,
            source_order_json, recovery_steps_json, created_at, updated_at, raw_json
        )
        values (?, ?, ?, ?, 'queued', 'prowlarr', ?, 'source ladder found provider evidence', 1, ?, '[]', ?, ?, '{}')
        """,
        (
            queue_id,
            wanted_id,
            series_id,
            issue_id,
            f"{title} 1",
            json.dumps(["prowlarr", "slskd"]),
            now,
            now,
        ),
    )
    con.execute(
        """
        insert into source_attempts(
            id, queue_id, wanted_id, series_id, issue_id, source, provider_id, provider, protocol,
            download_client, category, status, title, score, started_at, completed_at, raw_json
        )
        values (?, ?, ?, ?, ?, 'prowlarr', ?, ?, 'torrent', 'qbittorrent', 'comics', 'candidate_available', ?, 99, ?, ?, ?)
        """,
        (
            attempt_id,
            queue_id,
            wanted_id,
            series_id,
            issue_id,
            provider_id,
            provider,
            f"{title} 1",
            now,
            now,
            json.dumps({"provider_id": provider_id, "provider": provider}),
        ),
    )


def rows_for(db_path: Path, *, provider_filter: str, row_mode: str) -> list[dict]:
    payload = inkdrop_state.state_view(
        db_path,
        "queue",
        limit=20,
        provider_filter=provider_filter,
        queue_filter="queued",
        summary_mode="compact",
        row_mode=row_mode,
    )
    rows = payload.get("rows") if isinstance(payload, dict) else None
    require(isinstance(rows, list), f"state_view did not return queue rows for {row_mode}")
    return rows


def assert_provider_row(row: dict, *, row_mode: str) -> None:
    require(row.get("series") == "Provider Evidence Alpha", f"{row_mode} returned wrong series: {row.get('series')!r}")
    require(row.get("provider_filter") == "torrentleech_comics", f"{row_mode} lost provider_filter: {row}")
    require(row.get("source_attempt_filter") == "torrentleech_comics", f"{row_mode} lost source_attempt_filter: {row}")
    require(row.get("source_key") == "prowlarr", f"{row_mode} lost source_key: {row}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="inkdrop-provider-queue-") as tmp:
        db_path = Path(tmp) / "inkdrop-state.sqlite3"
        inkdrop_state.ensure_schema(db_path)
        with inkdrop_state.connect(db_path) as con:
            seed_queue_row(con, suffix="tl", provider_id="torrentleech_comics", provider="TorrentLeech Comics", title="Provider Evidence Alpha")
            seed_queue_row(con, suffix="dog", provider_id="dognzb_comics", provider="DogNZB Comics", title="Provider Evidence Beta")
            con.commit()

        for row_mode in ("compact", "table"):
            rows = rows_for(db_path, provider_filter="torrentleech_comics", row_mode=row_mode)
            require(len(rows) == 1, f"{row_mode} provider filter returned {len(rows)} rows instead of 1: {rows}")
            assert_provider_row(rows[0], row_mode=row_mode)

        dognzb_rows = rows_for(db_path, provider_filter="dognzb_comics", row_mode="table")
        require(len(dognzb_rows) == 1, f"DogNZB provider filter returned {len(dognzb_rows)} rows instead of 1")
        require(dognzb_rows[0].get("provider_filter") == "dognzb_comics", f"DogNZB table row lost provider_filter: {dognzb_rows[0]}")

    print(json.dumps({"ok": True, "checked": "provider_queue_filter_evidence"}))
    print("PROVIDER_QUEUE_FILTER_EVIDENCE_OK: provider-filtered queue rows preserve concrete provider evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
