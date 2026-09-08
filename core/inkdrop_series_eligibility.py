"""Which series an acquisition lane may hand to a pass, read from the state database.

WHY ONE MODULE
    Two schedulers select work from the same queue document: the SLSKD source
    probe (`combine_source_review_items`) and the series autopilot
    (`due_series`, the cached-candidate lane). The probe asked the database
    whether a series was still monitored; the autopilot never did. On
    2026-09-08 the autopilot handed 13 unmonitored series about 95% of the
    day's passes -- the probe refused every one of them, nothing advanced their
    clocks, and they sat at the head of every lane while 263 monitored series
    shared the remainder. One rule, read in one place, answered the same way by
    both.

WHAT IS EXCLUDED
    A series whose `monitored` flag is 0, or whose `source` is `removed`. That
    is the user's decision about the series, and it does not depend on our
    search history with any of its rows. Nothing here reads the queue's own
    states; `series_removed_by_user()` in the autopilot still covers rows
    parked `user_removed`, which is a different record of a related decision.

HOW IT READS
    A read-only, short-lived connection opened and closed inside the call, so a
    change made in Settings takes effect on the very next pass rather than on
    some cache's refresh cycle. A missing database excludes nothing: the
    callers' fixtures run without one, and an absent file is not a decision.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def removed_or_unmonitored_series_identity(db_path, normalize):
    """Series currently removed or unmonitored, as (ids, normalized titles).

    `normalize` is the caller's own title normalizer, so a title match here is
    the same match the caller uses for its own deduplication key and not a new
    heuristic; both modules that read this keep their own normalizer.
    """
    ids = set()
    titles = set()
    try:
        if not db_path or not Path(db_path).exists():
            return ids, titles
    except (TypeError, OSError):
        return ids, titles
    try:
        from core import inkdrop_db
    except ImportError:
        return ids, titles
    try:
        con = inkdrop_db.open_connection(
            db_path,
            readonly=True,
            timeout_seconds=5,
            operation="series_eligibility",
        )
    except Exception:
        return ids, titles
    try:
        rows = con.execute(
            "select id, title, sort_title from series"
            " where coalesce(monitored, 1) = 0 or lower(coalesce(source, '')) = 'removed'"
        ).fetchall()
    except sqlite3.Error:
        return ids, titles
    finally:
        con.close()
    for row in rows:
        row_id = str(row["id"] or "").strip()
        if row_id:
            ids.add(row_id)
        for title in (row["title"], row["sort_title"]):
            normalized = normalize(title or "")
            if normalized:
                titles.add(normalized)
    return ids, titles


def item_series_identity(item):
    """The most precise series identity an item carries, or "" if none.

    Prefer an explicit id -- it is exact. Only an item with no id at all (the
    manual-review tail carries a series NAME only) falls back to the title.
    """
    item = item if isinstance(item, dict) else {}
    series_id = str(item.get("series_id") or "").strip()
    if series_id:
        return series_id
    comicvine_id = str(item.get("comicvine_id") or "").strip()
    if comicvine_id.isdigit():
        return f"comicvine:{comicvine_id}"
    return ""


def series_excluded(item, removed_ids, removed_titles, normalize):
    """True when the item's series is unmonitored or removed per the sets above.

    An item that names its series by id is judged by the id alone; a title
    match is used only when the item carries no id, exactly as the probe has
    done since 2026-08-22.
    """
    if not isinstance(item, dict):
        return False
    if not removed_ids and not removed_titles:
        return False
    identity = item_series_identity(item)
    if identity:
        return identity in removed_ids
    title_key = normalize(item.get("series") or item.get("query") or "")
    return bool(title_key and title_key in removed_titles)
