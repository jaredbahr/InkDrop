#!/usr/bin/env python3
"""Public InkDrop state-view payload builders.

This module keeps compact API response shaping out of the legacy web monolith.
It intentionally stays small and delegates storage/query details to
``inkdrop_state``.
"""

from __future__ import annotations

import inkdrop_state


def bool_value(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def query_first(query, *keys, default=None):
    query = query if isinstance(query, dict) else {}
    for key in keys:
        values = query.get(key)
        if values:
            return values[0]
    return default


def query_summary_mode(query):
    value = query_first(query, "summary", "summary_mode", "summaryMode")
    if value not in (None, ""):
        return value
    if bool_value(query_first(query, "compact"), False):
        return "compact"
    return None


def query_row_mode(query):
    value = query_first(query, "rows", "row_mode", "rowMode")
    if value not in (None, ""):
        return value
    if bool_value(query_first(query, "compact"), False):
        return "compact"
    return None


def focus_from_query(query):
    aliases = {
        "series_id": ("focus_series_id", "focusSeriesId", "series_id", "seriesId"),
        "issue_id": ("focus_issue_id", "focusIssueId", "issue_id", "issueId"),
        "wanted_id": ("focus_wanted_id", "focusWantedId", "wanted_id", "wantedId"),
        "queue_id": ("focus_queue_id", "focusQueueId", "queue_id", "queueId"),
        "source_attempt_id": ("focus_source_attempt_id", "focusSourceAttemptId", "source_attempt_id", "sourceAttemptId"),
        "download_task_id": ("focus_download_task_id", "focusDownloadTaskId", "download_task_id", "downloadTaskId"),
        "import_id": ("focus_import_id", "focusImportId", "import_id", "importId"),
        "review_id": ("focus_review_id", "focusReviewId", "review_id", "reviewId"),
    }
    out = {}
    query = query if isinstance(query, dict) else {}
    for key, names in aliases.items():
        for name in names:
            value = (query.get(name) or [None])[0]
            if value not in (None, ""):
                out[key] = value
                break
    return out


def series_row_matches_filter(row, series_filter):
    key = inkdrop_state.series_filter_key(series_filter)
    row = row if isinstance(row, dict) else {}
    if key == "all":
        return True
    if key == "monitored":
        return bool(row.get("monitored"))
    if key == "unmonitored":
        return not bool(row.get("monitored"))
    if key == "auto_grab":
        return bool(row.get("auto_grab"))
    if key == "attention":
        return int(row.get("needs_you_count") or 0) > 0
    if key == "duplicate_titles":
        return int(row.get("duplicate_series_group_count") or 0) > 1
    if key == "wanted":
        return int(row.get("wanted_count") or 0) > 0
    if key == "active":
        return int(row.get("active_queue_count") or 0) > 0
    if key == "provider_wait":
        return int(row.get("provider_wait_count") or 0) > 0
    ownership = str(row.get("ownership") or "").strip().lower()
    source = str(row.get("source") or "").strip().lower()
    metadata_provider = str(row.get("metadata_provider") or "").strip().lower()
    if key == "native":
        return ownership == "native" or (
            source not in {"", "kapowarr", "watch", "manual"}
            or metadata_provider not in {"", "kapowarr", "watch", "manual"}
        )
    if key == "adapter_adopted":
        return bool(row.get("kapowarr_id")) and (source == "comicvine" or metadata_provider == "comicvine")
    if key == "adapter":
        return ownership == "adapter" or source == "kapowarr" or (
            metadata_provider == "kapowarr" and bool(row.get("kapowarr_id"))
        )
    if key == "manual":
        return ownership == "manual" or (
            not row.get("kapowarr_id")
            and source in {"", "manual", "watch"}
            and metadata_provider in {"", "manual", "watch"}
        )
    return True


def series_compact_summary(rows):
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    ownership_counts = {}
    for row in rows:
        ownership = str(row.get("ownership") or "unknown").strip().lower() or "unknown"
        ownership_counts[ownership] = int(ownership_counts.get(ownership) or 0) + 1
    active_queue = sum(int(row.get("active_queue_count") or 0) for row in rows)
    wanted = sum(int(row.get("wanted_count") or 0) for row in rows)
    needs_you = sum(int(row.get("needs_you_count") or 0) for row in rows)
    provider_wait = sum(int(row.get("provider_wait_count") or 0) for row in rows)
    return {
        "ok": True,
        "summary_mode": "compact",
        "series": len(rows),
        "series_by_ownership": ownership_counts,
        "wanted_by_status": {"wanted": wanted},
        "queue_by_active_state": {"queued": active_queue},
        "queue_by_display_active_state": {"queued": active_queue},
        "active_queue_items": active_queue,
        "queue_work_items": active_queue,
        "review_exceptions": needs_you,
        "queue_backlog_provider_wait_items": provider_wait,
    }


def series_compact_view(db_path, limit=80, series_filter=None, focus=None, summary_mode=None, row_mode=None):
    try:
        fetch_limit = max(1, min(int(limit or 80), 5000))
    except (TypeError, ValueError):
        fetch_limit = 80
    series_filter = inkdrop_state.series_filter_key(series_filter)
    focus = focus if isinstance(focus, dict) else {}
    focus_entities = inkdrop_state.normalize_focus_entities(focus)
    normalized_row_mode = inkdrop_state.normalize_state_view_row_mode(row_mode)
    lightweight_rows = normalized_row_mode in inkdrop_state.COMPACT_ROW_MODES
    row_reader = inkdrop_state.series_compact_card_rows if lightweight_rows else inkdrop_state.series_rows
    summary = inkdrop_state.state_view_operational_table_summary(db_path, view="series")
    summary = summary if isinstance(summary, dict) and summary.get("ok") else {}
    filter_options = inkdrop_state.series_filter_options(db_path, summary)
    filter_counts = {
        str(option.get("value") or ""): int(option.get("count") or 0)
        for option in filter_options
        if isinstance(option, dict)
    }
    needs_post_filter = series_filter in {"attention", "provider_wait"}
    row_fetch_limit = 5000 if (focus or needs_post_filter) else fetch_limit
    filtered_rows = row_reader(db_path, row_fetch_limit, series_filter=series_filter)
    if needs_post_filter:
        filtered_rows = [row for row in filtered_rows if series_row_matches_filter(row, series_filter)]
    if focus_entities:
        filtered_rows = inkdrop_state.focus_rows(filtered_rows, focus_entities)
    rows = filtered_rows[:fetch_limit]
    if lightweight_rows and focus_entities:
        # The bulk lightweight reader omits description/deck on purpose (a
        # table of hundreds of series must not carry every synopsis on first
        # paint), but a focused single-series view still needs them.
        description_fields = inkdrop_state.series_description_fields_for_ids(
            db_path, [row.get("id") for row in rows if row.get("id")]
        )
        if description_fields:
            rows = [
                {**row, **description_fields[str(row["id"])]} if str(row.get("id")) in description_fields else row
                for row in rows
            ]
    raw_total_count = filter_counts.get(series_filter)
    if focus_entities:
        total_count = len(filtered_rows)
    elif raw_total_count is not None and row_fetch_limit >= int(raw_total_count or 0):
        total_count = len(filtered_rows)
    else:
        total_count = int(raw_total_count if raw_total_count is not None else len(filtered_rows))
    summary_key = str(summary_mode or "").strip().lower().replace("-", "_")
    loaded_count = len(rows)
    return {
        "ok": True,
        "view": "series",
        "summary": inkdrop_state.compact_state_view_summary(summary, "series") if summary else series_compact_summary(filtered_rows),
        "row_mode": normalized_row_mode,
        "count": loaded_count,
        "loaded_count": loaded_count,
        "total_count": total_count,
        "has_more": total_count > loaded_count,
        "limit": fetch_limit,
        "rows": inkdrop_state.compact_state_view_rows("series", rows, normalized_row_mode),
        "db_path": str(db_path),
        "focus": focus_entities,
        "focused": bool(focus_entities),
        "series_filter": series_filter,
        "filters": filter_options,
        "fast_path": True,
        "lightweight_rows": lightweight_rows,
        "summary_mode": "compact" if summary_key in {"compact", "minimal"} else "full",
    }


def queue_wanted_compact_policy(view, summary_mode=None, row_mode=None, focus=None):
    """Return lightweight first-paint policy for Queue/Wanted state routes."""
    view = str(view or "").strip().lower()
    summary_key = str(summary_mode or "").strip().lower().replace("-", "_")
    row_mode_key = inkdrop_state.normalize_state_view_row_mode(row_mode)
    focus_entities = inkdrop_state.normalize_focus_entities(focus)
    broad_compact_load = (
        view in {"queue", "wanted"}
        and summary_key in {"compact", "minimal"}
        and row_mode_key in inkdrop_state.COMPACT_ROW_MODES
        and not focus_entities
    )
    return {
        "view": view,
        "summary_mode": summary_key,
        "row_mode": row_mode_key,
        "focus": focus_entities,
        "broad_compact_load": broad_compact_load,
        "run_settlement": not broad_compact_load,
        "live_slskd": not broad_compact_load,
        "live_prowlarr": not broad_compact_load,
        "live_download_clients": not broad_compact_load,
    }
