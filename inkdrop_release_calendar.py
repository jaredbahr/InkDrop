#!/usr/bin/env python3
"""Release calendar: what landed recently and what is due, grouped by date.

This reads the release dates already stored on ``issues`` (ComicVine
``store_date`` falling back to ``cover_date``, MangaDex ``publishAt``) and
groups them into calendar days for the series a person actually monitors, with
the acquisition status of each entry attached: owned, downloading, wanted, or
just being watched.

A note on the forward half, because it is the surprising part. As of this
writing, ComicVine carries almost no forward-dated ``store_date`` values -- a
whole-database query for the next eight weeks returns a single issue. Its
``cover_date`` field does run months ahead, but that is the printed cover date,
which by long-standing publishing convention sits roughly two months *after*
the book already shipped. Using it as a schedule would list issues as upcoming
that were on shelves in June. MangaDex publishes a chapter's timestamp when the
chapter appears, so it has no forward schedule either.

So the window here spans both directions and defaults to mostly-past. The
backward half is real today. The forward half is built, correct, and will fill
in on its own the moment a provider starts supplying dates that lead the
release rather than trail it. ``summary.forward_window`` says plainly whether
anything is out there, so an empty upcoming column reads as "nobody has
announced these yet" rather than as a broken page.

The state database is opened read-only. Nothing here writes or fetches.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

import inkdrop_runtime_config
import inkdrop_state


CALENDAR_SCHEMA = "inkdrop.release_calendar.v1"

DEFAULT_DAYS_BACK = 14
DEFAULT_DAYS_AHEAD = 28
MAX_DAYS_BACK = 365
MAX_DAYS_AHEAD = 365
MAX_ENTRIES = 2000

# Status precedence, most-settled first. An issue that is both owned and still
# has a stale queue row should read as owned.
STATUS_LABELS = {
    "owned": "In library",
    "importing": "Importing",
    "downloading": "Downloading",
    "searching": "Searching",
    "wanted": "Wanted",
    "watching": "Monitored",
    "unmonitored": "Not monitored",
}

# queue_items.state -> calendar status. States outside this map are active
# queue work that has not reached a phase worth naming on a calendar cell.
QUEUE_STATE_STATUS = {
    "importing": "importing",
    "downloading": "downloading",
    "source_wait": "downloading",
    "queued": "searching",
    "searching": "searching",
    "needs_you": "wanted",
}


def _clamp(value, default, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_bool(value):
    return bool(value) if isinstance(value, bool) else str(value or "0").strip() not in {"", "0", "false", "no"}


def _iso_day(value):
    """Day key for a stored release date, or "" when it is not a real date.

    Stored values are either ``YYYY-MM-DD`` (ComicVine) or a full ISO timestamp
    (MangaDex). Anything else -- a bare year, a month name, a partial date some
    provider slipped through -- has no place on a calendar and is counted as
    unreadable rather than guessed at.
    """
    text = str(value or "").strip()
    if len(text) < 10:
        return ""
    head = text[:10]
    try:
        return datetime.date.fromisoformat(head).isoformat()
    except ValueError:
        return ""


def _unit_label(media_type, issue_number, issue_raw):
    """"Issue #12", "Chapter 12", "Volume 3" -- whichever this row actually is."""
    number = str(issue_number or "").strip()
    if media_type in inkdrop_state.MANGA_MEDIA_TYPES:
        noun = "Volume" if inkdrop_state.issue_volume_hint(issue_raw) else "Chapter"
        return f"{noun} {number}" if number else noun
    return f"Issue #{number}" if number else "Issue"


def window_bounds(now=None, days_back=DEFAULT_DAYS_BACK, days_ahead=DEFAULT_DAYS_AHEAD, start=None, end=None):
    """Resolve the calendar window to (today, start, end) as date objects.

    Explicit ``start``/``end`` win over the day counts, so a UI can page month
    by month without translating months into offsets.
    """
    now = time.time() if now is None else float(now)
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
    explicit_start = _iso_day(start)
    explicit_end = _iso_day(end)
    if explicit_start or explicit_end:
        first = datetime.date.fromisoformat(explicit_start) if explicit_start else today
        last = datetime.date.fromisoformat(explicit_end) if explicit_end else first + datetime.timedelta(days=DEFAULT_DAYS_AHEAD)
        if last < first:
            first, last = last, first
        return today, first, last
    back = _clamp(days_back, DEFAULT_DAYS_BACK, 0, MAX_DAYS_BACK)
    ahead = _clamp(days_ahead, DEFAULT_DAYS_AHEAD, 0, MAX_DAYS_AHEAD)
    return today, today - datetime.timedelta(days=back), today + datetime.timedelta(days=ahead)


def _entry_status(row):
    """Settle one issue into a single calendar status."""
    if int(row["verified_imports"] or 0) > 0:
        return "owned"
    queue_state = str(row["queue_state"] or "").strip().lower()
    if queue_state:
        mapped = QUEUE_STATE_STATUS.get(queue_state)
        if mapped:
            return mapped
        return "searching"
    if str(row["wanted_status"] or "").strip().lower() in inkdrop_state.REMOVED_SERIES_ACTIVE_WANTED_STATUSES:
        return "wanted"
    if not _as_bool(row["series_monitored"]) or not _as_bool(row["issue_monitored"]):
        return "unmonitored"
    return "watching"


def calendar_rows(con, start, end, *, series_id=None, include_unmonitored=False, limit=MAX_ENTRIES):
    """Dated issues in the window, each carrying its acquisition status.

    The queue and import joins are aggregated rather than joined row-for-row:
    an issue can accumulate several attempts over its life, and a calendar cell
    should show one entry, not one per retry.
    """
    series_filter = str(series_id or "").strip() or None
    active_queue_states = inkdrop_state.REMOVED_SERIES_ACTIVE_QUEUE_STATES
    queue_placeholders = ",".join("?" for _ in active_queue_states)
    monitored_clause = "" if include_unmonitored else "and coalesce(s.monitored, 0) = 1"
    return con.execute(
        f"""
        select
            i.id as issue_id,
            i.series_id,
            i.issue_number,
            i.normalized_number,
            i.title as issue_title,
            i.release_date,
            i.metadata_provider as issue_provider,
            i.monitored as issue_monitored,
            i.raw_json as issue_raw_json,
            s.title as series_title,
            s.sort_title as series_sort_title,
            s.media_type,
            s.publisher,
            s.metadata_provider as series_provider,
            s.metadata_id as series_metadata_id,
            s.source as series_source,
            s.monitored as series_monitored,
            s.raw_json as series_raw_json,
            (
                select count(*) from import_results ir
                where ir.issue_id = i.id and ir.verified = 1
            ) as verified_imports,
            (
                select q.state from queue_items q
                where q.issue_id = i.id
                  and q.active = 1
                  and q.state in ({queue_placeholders})
                order by coalesce(q.updated_at, q.created_at, 0) desc
                limit 1
            ) as queue_state,
            (
                select w.status from wanted_items w
                where w.issue_id = i.id
                order by coalesce(w.updated_at, w.created_at, 0) desc
                limit 1
            ) as wanted_status
        from issues i
        join series s on s.id = i.series_id
        where coalesce(i.release_date, '') <> ''
          and date(i.release_date) between date(?) and date(?)
          {monitored_clause}
          and (? is null or s.id = ?)
        order by date(i.release_date) asc,
                 lower(coalesce(nullif(s.sort_title, ''), s.title)) asc,
                 cast(coalesce(nullif(i.normalized_number, ''), i.issue_number, '0') as real) asc
        limit ?
        """,
        (
            *active_queue_states,
            start.isoformat(),
            end.isoformat(),
            series_filter,
            series_filter,
            int(limit),
        ),
    ).fetchall()


def unreadable_date_count(con, *, include_unmonitored=False, series_id=None):
    """Issues carrying a release date SQLite cannot read as a date.

    Those rows are silently invisible to the window filter, so the count is
    surfaced instead of letting the calendar quietly under-report.
    """
    series_filter = str(series_id or "").strip() or None
    monitored_clause = "" if include_unmonitored else "and coalesce(s.monitored, 0) = 1"
    row = con.execute(
        f"""
        select count(*) from issues i
        join series s on s.id = i.series_id
        where coalesce(i.release_date, '') <> ''
          and date(i.release_date) is null
          {monitored_clause}
          and (? is null or s.id = ?)
        """,
        (series_filter, series_filter),
    ).fetchone()
    return int(row[0] if row else 0)


def forward_horizon(con, today, *, include_unmonitored=False):
    """The furthest-out dated issue on record, forward of today.

    This is what tells an empty upcoming column apart from a broken query.
    """
    monitored_clause = "" if include_unmonitored else "and coalesce(s.monitored, 0) = 1"
    row = con.execute(
        f"""
        select max(date(i.release_date)) as furthest, count(*) as total
        from issues i
        join series s on s.id = i.series_id
        where coalesce(i.release_date, '') <> ''
          and date(i.release_date) > date(?)
          {monitored_clause}
        """,
        (today.isoformat(),),
    ).fetchone()
    furthest = str((row["furthest"] if row else "") or "")
    total = int((row["total"] if row else 0) or 0)
    return furthest, total


def build_entry(row, today):
    raw = inkdrop_state.json_loads(row["issue_raw_json"] or "{}", {})
    raw = raw if isinstance(raw, dict) else {}
    media_type = str(row["media_type"] or "comic").strip().lower()
    day = _iso_day(row["release_date"])
    status = _entry_status(row)
    released = bool(day) and datetime.date.fromisoformat(day) <= today
    return {
        "issue_id": row["issue_id"],
        "series_id": row["series_id"],
        "series_title": row["series_title"],
        "media_type": media_type,
        "publisher": str(row["publisher"] or ""),
        "metadata_provider": str(row["issue_provider"] or row["series_provider"] or ""),
        "issue_number": str(row["issue_number"] or ""),
        "issue_title": str(row["issue_title"] or ""),
        "unit_label": _unit_label(media_type, row["issue_number"], raw),
        "release_date": day,
        "released": released,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status.replace("_", " ").title()),
        "series_monitored": _as_bool(row["series_monitored"]),
        "issue_monitored": _as_bool(row["issue_monitored"]),
        "in_library": status == "owned",
    }


def _series_parked(row):
    """Rows automation parked -- retired shadows, replaced metadata identities.

    Same exclusion the portability export uses: these are bookkeeping, not
    series anybody expects to see on a calendar.
    """
    if str(row["series_source"] or "") == "replaced_metadata":
        return True
    raw = inkdrop_state.json_loads(row["series_raw_json"] or "{}", {})
    raw = raw if isinstance(raw, dict) else {}
    if str(raw.get("automation_parked_reason") or "").strip():
        return True
    return inkdrop_state.series_raw_user_removed(raw)


def release_calendar(
    db_path,
    *,
    now=None,
    days_back=DEFAULT_DAYS_BACK,
    days_ahead=DEFAULT_DAYS_AHEAD,
    start=None,
    end=None,
    series_id=None,
    include_unmonitored=False,
    limit=MAX_ENTRIES,
):
    """Build the calendar document for a window.

    Only days that actually hold entries are emitted. The window bounds travel
    with the document so a month grid can lay out its empty cells without the
    payload carrying a few hundred empty days.
    """
    now = time.time() if now is None else float(now)
    limit = _clamp(limit, MAX_ENTRIES, 1, MAX_ENTRIES)
    today, first, last = window_bounds(now=now, days_back=days_back, days_ahead=days_ahead, start=start, end=end)

    days = {}
    status_counts = {}
    series_seen = set()
    entries_total = 0
    parked_skipped = 0

    with inkdrop_state.connect_read(db_path) as con:
        rows = calendar_rows(
            con,
            first,
            last,
            series_id=series_id,
            include_unmonitored=include_unmonitored,
            limit=limit,
        )
        unreadable = unreadable_date_count(con, include_unmonitored=include_unmonitored, series_id=series_id)
        furthest_forward, forward_total = forward_horizon(con, today, include_unmonitored=include_unmonitored)

    for row in rows:
        if _series_parked(row):
            parked_skipped += 1
            continue
        entry = build_entry(row, today)
        if not entry["release_date"]:
            continue
        day = days.setdefault(
            entry["release_date"],
            {"date": entry["release_date"], "entries": [], "counts": {}},
        )
        day["entries"].append(entry)
        day["counts"][entry["status"]] = day["counts"].get(entry["status"], 0) + 1
        status_counts[entry["status"]] = status_counts.get(entry["status"], 0) + 1
        series_seen.add(entry["series_id"])
        entries_total += 1

    ordered = []
    for key in sorted(days):
        day = days[key]
        day_date = datetime.date.fromisoformat(key)
        offset = (day_date - today).days
        day.update(
            {
                "weekday": day_date.strftime("%A"),
                "is_today": offset == 0,
                "days_from_today": offset,
                "released": offset <= 0,
                "entry_count": len(day["entries"]),
            }
        )
        ordered.append(day)

    released_total = sum(day["entry_count"] for day in ordered if day["released"])
    upcoming_total = entries_total - released_total

    return {
        "schema": CALENDAR_SCHEMA,
        "generated_at": inkdrop_state.utc_stamp(now),
        "window": {
            "today": today.isoformat(),
            "start": first.isoformat(),
            "end": last.isoformat(),
            "days_back": (today - first).days,
            "days_ahead": (last - today).days,
            "include_unmonitored": bool(include_unmonitored),
            "series_id": str(series_id or "") or None,
        },
        "days": ordered,
        "summary": {
            "entries_total": entries_total,
            "entries_released": released_total,
            "entries_upcoming": upcoming_total,
            "days_with_entries": len(ordered),
            "series_covered": len(series_seen),
            "by_status": dict(sorted(status_counts.items())),
            # Callers should not read an empty result as "nothing is coming"
            # without also reading these three.
            "entries_truncated": entries_total >= limit,
            "entry_limit": limit,
            "unreadable_release_dates": unreadable,
            "series_parked_skipped": parked_skipped,
            "forward_window": {
                "issues_dated_after_today": forward_total,
                "furthest_dated_release": furthest_forward,
                "note": (
                    ""
                    if forward_total
                    else "No stored issue is dated later than today. ComicVine publishes "
                         "almost no forward store dates and MangaDex timestamps a chapter "
                         "when it appears, so upcoming days stay empty until a provider "
                         "supplies dates ahead of release."
                ),
            },
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print the InkDrop release calendar for a window.")
    parser.add_argument(
        "--db",
        default=str(inkdrop_runtime_config.state_db_path()),
        help="state database path (default: the configured state directory)",
    )
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK, help=f"days before today (default {DEFAULT_DAYS_BACK})")
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD, help=f"days after today (default {DEFAULT_DAYS_AHEAD})")
    parser.add_argument("--start", default="", help="explicit window start (YYYY-MM-DD); overrides --days-back")
    parser.add_argument("--end", default="", help="explicit window end (YYYY-MM-DD); overrides --days-ahead")
    parser.add_argument("--series", default="", help="limit to one series id")
    parser.add_argument("--include-unmonitored", action="store_true", help="include series that are not monitored")
    parser.add_argument("--json", action="store_true", help="print the document instead of a readable summary")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        parser.error(f"state database not found: {db_path}")

    document = release_calendar(
        db_path,
        days_back=args.days_back,
        days_ahead=args.days_ahead,
        start=args.start,
        end=args.end,
        series_id=args.series,
        include_unmonitored=args.include_unmonitored,
    )

    if args.json:
        print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    window = document["window"]
    summary = document["summary"]
    print(f"Release calendar {window['start']} to {window['end']} (today {window['today']})")
    print(
        f"  {summary['entries_total']} entries across {summary['days_with_entries']} days, "
        f"{summary['series_covered']} series -- {summary['entries_released']} released, "
        f"{summary['entries_upcoming']} upcoming"
    )
    if summary["by_status"]:
        print("  " + ", ".join(f"{STATUS_LABELS.get(key, key)}: {count}" for key, count in summary["by_status"].items()))
    if summary["unreadable_release_dates"]:
        print(f"  {summary['unreadable_release_dates']} issues carry a release date that is not a readable date")
    if summary["entries_truncated"]:
        print(f"  entry limit of {summary['entry_limit']} reached; narrow the window to see the rest")
    note = summary["forward_window"]["note"]
    if note:
        print(f"  {note}")
    print()
    for day in document["days"]:
        marker = " <- today" if day["is_today"] else ""
        print(f"{day['date']} {day['weekday'][:3]} ({day['entry_count']}){marker}")
        for entry in day["entries"]:
            print(f"    {entry['series_title']} - {entry['unit_label']} [{entry['status_label']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
