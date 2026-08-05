"""Settings-backed source job prep for InkDrop source workers.

This module is intentionally side-effect free. It turns an InkDrop settings
snapshot into source jobs, can run those jobs only through injected payloads or
HTTP clients, and returns attempt payloads for a caller to record elsewhere.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

import inkdrop_source_providers as providers
import inkdrop_source_registry as registry
import inkdrop_source_worker_adapters as adapters
import inkdrop_source_worker_plan as worker_plan
import inkdrop_source_worker_runtime as runtime


CONTRACT_VERSION = 1

OPERATOR_PAYLOAD_MODES = {
    "operator_tool_output",
    "operator_manual_cards",
}

BLOCKED_JOB_STATUSES = {
    "blocked",
    "configuration_required",
    "unsupported_adapter",
    "not_executable",
}

NON_EXECUTABLE_JOB_STATUSES = BLOCKED_JOB_STATUSES | {"provider_wait"}

PROVIDER_UNAVAILABLE_FETCH_REASONS = {
    "external_tool_failed",
    "http_request_failed",
}

PROVIDER_WAIT_FETCH_REASONS = {
    "fetch_failed",
    "http_client_required",
    "missing_archive_request",
    "missing_direct_detail_request",
    "missing_direct_file_request",
    "missing_direct_probe_request",
    "missing_feed_reader_request",
    "missing_json_direct_request",
    "missing_mangadex_request",
    "missing_opds_catalog_request",
    "missing_reader_page_pack_request",
    "missing_rss_detail_request",
    "missing_rss_direct_request",
    "missing_rss_probe_request",
    "missing_suwayomi_request",
    "missing_torrent_detail_request",
    "missing_torrent_request",
    "tool_runner_required",
}

MANGADEX_DEFAULT_ELIGIBLE_PUBLISHERS = {
    "comikey",
    "denpa",
    "futabasha",
    "kadokawa",
    "kodansha",
    "kurokawa",
    "seven seas",
    "shogakukan",
    "shueisha",
    "square enix",
    "tokyopop",
    "vertical",
    "viz",
    "yen press",
}

MANGADEX_DEFAULT_BLOCKED_PUBLISHERS = {
    "boom studios",
    "dark horse comics",
    "dc",
    "dc comics",
    "dynamite",
    "dynamite entertainment",
    "fantagraphics",
    "idw",
    "idw publishing",
    "image",
    "image comics",
    "marvel",
    "marvel comics",
    "oni press",
    "titan comics",
}


def _dict(value):
    return dict(value) if isinstance(value, dict) else {}


def _list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _text_key(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    for old, new in (("&", " and "), ("+", " plus "), ("-", " "), (".", " "), (":", " ")):
        text = text.replace(old, new)
    return " ".join(text.split())


def _policy_list(policy, key, default=()):
    policy = policy if isinstance(policy, dict) else {}
    value = policy.get(key)
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        values = [str(part).strip() for part in value if str(part or "").strip()]
    else:
        values = []
    return values or list(default)


def _publisher_in_scope(publisher, candidates):
    publisher_key = _text_key(publisher)
    if not publisher_key:
        return False
    for candidate in candidates or []:
        candidate_key = _text_key(candidate)
        if candidate_key and (publisher_key == candidate_key or candidate_key in publisher_key):
            return True
    return False


def _first_text(*values):
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _media_type_set(row, policy=None):
    row = _dict(row)
    policy = policy if isinstance(policy, dict) else {}
    values = []
    for source in (row, policy):
        for key in ("media_types", "media_type", "supported_media_types"):
            values.extend(_list(source.get(key)))
    return {_text_key(value) for value in values if _text_key(value)}


MANGA_SCOPED_PROVIDER_IDS = {
    "mangadex",
    "prowlarr_nyaa",
    "prowlarr_tokyo_toshokan_manga",
    "suwayomi",
}

COMIC_PACK_SCOPED_PROVIDER_IDS = {
    "prowlarr_dognzb_comics",
    "prowlarr_kat_comics",
    "prowlarr_pirate_bay_comics",
    "prowlarr_torrentdownload_comics",
    "prowlarr_torrentleech_comics",
}

RSS_FRESH_RELEASE_PROVIDER_IDS = {
    "generic_rss_detail_direct_feed",
    "generic_rss_detail_probe_feed",
    "generic_rss_direct_feed",
    "generic_rss_reader_page_pack_feed",
    "rss",
    "rss_getcomics",
}

RSS_FRESH_RELEASE_SOURCE_KINDS = {
    "rss_feed",
    "rss_direct_feed",
    "rss_detail_direct_feed",
    "rss_detail_probe_feed",
    "rss_reader_page_pack_feed",
}

DEFAULT_RSS_FRESH_RELEASE_MAX_AGE_DAYS = 180

SUWAYOMI_SOURCE_ERROR_COOLDOWN_DEFAULT_SECONDS = 6 * 60 * 60
SUWAYOMI_SOURCE_ERROR_COOLDOWN_DEFAULT_THRESHOLD = 3
SUWAYOMI_SOURCE_ERROR_COOLDOWN_DEFAULT_MAX_SOURCES = 10
SUWAYOMI_SOURCE_ERROR_COOLDOWN_PROBE_DEFAULT_AFTER_SECONDS = 30 * 60
SUWAYOMI_SOURCE_ERROR_COOLDOWN_PROBE_DEFAULT_MAX_SOURCES = 2
SUWAYOMI_SOURCE_ERROR_QUARANTINE_DEFAULT_SECONDS = 24 * 60 * 60
SUWAYOMI_SOURCE_ERROR_QUARANTINE_DEFAULT_THRESHOLD = 12
SUWAYOMI_SOURCE_ERROR_QUARANTINE_DEFAULT_MAX_SOURCES = 10
SUWAYOMI_SOURCE_ERROR_COOLDOWN_SCAN_LIMIT = 250
SUWAYOMI_SOURCE_ERROR_PARTIAL_ERROR_STAGES = {
    "source_search": "source_search_error_count",
    "manga_chapters": "manga_chapter_lookup_error_count",
    "manga_chapters_no_meta_fallback": "manga_chapter_lookup_error_count",
    "chapter_pages": "chapter_page_lookup_error_count",
    "chapter_pages_no_meta_fallback": "chapter_page_lookup_error_count",
}
SUWAYOMI_VOLUME_GAP_COOLDOWN_DEFAULT_SECONDS = 24 * 60 * 60
SUWAYOMI_VOLUME_GAP_COOLDOWN_DEFAULT_THRESHOLD = 2
SUWAYOMI_VOLUME_METADATA_GAP_COOLDOWN_DEFAULT_THRESHOLD = 1
SUWAYOMI_VOLUME_GAP_COOLDOWN_DEFAULT_MAX_SOURCES = 10
SUWAYOMI_VOLUME_GAP_COOLDOWN_PROBE_DEFAULT_AFTER_SECONDS = 5 * 60
SUWAYOMI_VOLUME_GAP_COOLDOWN_PROBE_DEFAULT_MAX_SOURCES = 1


def _manga_scope_block_reason(row, wanted_item):
    row = _dict(row)
    wanted_item = _dict(wanted_item)
    provider_id = str(row.get("provider_id") or "").strip().lower()
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    explicit_scope = _text_key(policy.get("scope_policy") or policy.get("media_scope"))
    if provider_id not in MANGA_SCOPED_PROVIDER_IDS and explicit_scope not in {"manga_metadata_or_manga_publisher", "manga"}:
        return ""
    if _truthy(policy.get("allow_all_titles")) or _truthy(policy.get("disable_title_scope_filter")):
        return ""
    for key in ("mangadex_id", "manga_id", "mangadex_chapter_id", "suwayomi_manga_id", "suwayomi_chapter_id"):
        if _first_text(wanted_item.get(key), row.get(key)):
            return ""
    metadata_provider = _text_key(_first_text(wanted_item.get("metadata_provider"), row.get("metadata_provider")))
    if metadata_provider == "mangadex":
        return ""
    media_type = _text_key(_first_text(wanted_item.get("media_type"), row.get("media_type")))
    if media_type in {"manga", "manga metadata", "manga metadata source"}:
        return ""
    publisher = _first_text(wanted_item.get("publisher"), row.get("publisher"))
    eligible_publishers = _policy_list(policy, "eligible_publishers", MANGADEX_DEFAULT_ELIGIBLE_PUBLISHERS)
    if _publisher_in_scope(publisher, eligible_publishers):
        return ""
    blocked_publishers = _policy_list(policy, "blocked_publishers", MANGADEX_DEFAULT_BLOCKED_PUBLISHERS)
    display_name = _first_text(row.get("display_name"), row.get("provider_id"), "Manga source")
    if _publisher_in_scope(publisher, blocked_publishers):
        return f"{display_name} is scoped to manga; {publisher} rows should use comic sources"
    if explicit_scope not in {"all", "all titles", "any"}:
        return f"{display_name} is scoped to manga; row has no manga metadata or publisher signal"
    return ""


def _comic_pack_scope_block_reason(row, wanted_item):
    row = _dict(row)
    wanted_item = _dict(wanted_item)
    provider_id = str(row.get("provider_id") or "").strip().lower()
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    explicit_scope = _text_key(policy.get("scope_policy") or policy.get("media_scope"))
    comic_scope_values = {"comic", "comics", "comic_pack", "western_comic_pack", "western_comics"}
    if provider_id not in COMIC_PACK_SCOPED_PROVIDER_IDS and explicit_scope not in comic_scope_values:
        return ""
    if _truthy(policy.get("allow_all_titles")) or _truthy(policy.get("disable_title_scope_filter")):
        return ""
    provider_media_types = _media_type_set(row, policy)
    provider_allows_manga = bool(provider_media_types & {"manga", "manhwa", "manhua"})
    for key in ("mangadex_id", "manga_id", "mangadex_chapter_id", "suwayomi_manga_id", "suwayomi_chapter_id"):
        if _first_text(wanted_item.get(key), row.get(key)):
            if provider_allows_manga:
                return ""
            display_name = _first_text(row.get("display_name"), row.get("provider_id"), "Comic source")
            return f"{display_name} is scoped to comics; manga rows should use manga sources"
    metadata_provider = _text_key(_first_text(wanted_item.get("metadata_provider"), row.get("metadata_provider")))
    media_type = _text_key(_first_text(wanted_item.get("media_type"), row.get("media_type")))
    if metadata_provider == "mangadex" or media_type in {"manga", "manhwa", "manhua", "manga metadata", "manga metadata source"}:
        if provider_allows_manga:
            return ""
        display_name = _first_text(row.get("display_name"), row.get("provider_id"), "Comic source")
        return f"{display_name} is scoped to comics; manga rows should use manga sources"
    return ""


def _source_scope_block_reason(row, wanted_item):
    for scope_check in (_manga_scope_block_reason, _comic_pack_scope_block_reason, _rss_fresh_release_scope_block_reason):
        reason = scope_check(row, wanted_item)
        if reason:
            return reason
    return ""


def _policy_bool(policy, keys, default=None):
    policy = policy if isinstance(policy, dict) else {}
    for key in keys:
        if key in policy and policy.get(key) not in (None, ""):
            return _truthy(policy.get(key))
    return default


def _policy_int(policy, keys, default=0):
    policy = policy if isinstance(policy, dict) else {}
    for key in keys:
        if key not in policy or policy.get(key) in (None, ""):
            continue
        try:
            return int(policy.get(key))
        except (TypeError, ValueError):
            continue
    return int(default)


def _json_obj(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sqlite_table_exists(con, table_name):
    row = con.execute(
        "select 1 from sqlite_master where type='table' and name=? limit 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def _suwayomi_job_row(row):
    row = _dict(row)
    provider_id = str(row.get("provider_id") or row.get("id") or "").strip().lower()
    source_kind = str(row.get("source_kind") or "").strip().lower()
    adapter_family = str(row.get("adapter_family") or "").strip().lower()
    return provider_id == "suwayomi" or source_kind == "suwayomi_api_page_provider" or adapter_family == "suwayomi"


def _source_attempt_fetch_evidence(raw):
    raw = _json_obj(raw)
    nested_raw = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
    fetch = nested_raw.get("fetch") if isinstance(nested_raw.get("fetch"), dict) else {}
    if fetch:
        return fetch
    return raw.get("fetch") if isinstance(raw.get("fetch"), dict) else {}


def _mangadex_query_rotation(db_path, wanted_item, query_pool):
    """Advance only past the newest attempt's completed clean-zero queries."""
    wanted_item = _dict(wanted_item)
    queue_id = str(wanted_item.get("queue_id") or "").strip()
    queries = [providers.normalized_query(value) for value in query_pool or []]
    queries = [value for value in queries if value]
    if not db_path or not queue_id or len(queries) < 2:
        return {}
    path = Path(str(db_path))
    if not path.exists():
        return {}
    con = None
    try:
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        if not _sqlite_table_exists(con, "source_attempts"):
            return {}
        rows = con.execute(
            """
            select id, raw_json
              from source_attempts
             where queue_id=?
               and (
                    lower(coalesce(provider_id, '')) = 'mangadex'
                 or lower(coalesce(source, '')) = 'mangadex'
                 or lower(coalesce(provider, '')) = 'mangadex'
               )
             order by coalesce(completed_at, started_at, 0) desc, id desc
             limit 1
            """,
            (queue_id,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if con is not None:
            con.close()

    if not rows:
        return {}
    attempt_row = rows[0]
    fetch = _source_attempt_fetch_evidence(attempt_row["raw_json"])
    if not fetch:
        return {}
    query_keys = [value.casefold() for value in queries]
    persisted_pool = [providers.normalized_query(value) for value in fetch.get("mangadex_query_pool") or []]
    persisted_pool = [value for value in persisted_pool if value]
    planned = [providers.normalized_query(value) for value in fetch.get("query_variants") or []]
    planned = [value for value in planned if value]
    if not planned or len(planned) > len(queries) or len({value.casefold() for value in planned}) != len(planned):
        return {}
    planned_keys = [value.casefold() for value in planned]
    window_start = next(
        (
            index
            for index in range(len(queries))
            if [query_keys[(index + offset) % len(queries)] for offset in range(len(planned))] == planned_keys
        ),
        None,
    )
    if window_start is None:
        return {}

    decision = {
        "offset": window_start,
        "previous_attempt_id": attempt_row["id"],
        "previous_completed_query_count": 0,
        "previous_planned_query_count": len(planned),
        "advanced_by": 0,
    }
    if persisted_pool and [value.casefold() for value in persisted_pool] != query_keys:
        decision["reason"] = "query_pool_changed"
        return decision
    if fetch.get("error") or fetch.get("partial_errors"):
        decision["reason"] = "previous_attempt_error"
        return decision
    counts = fetch.get("variant_result_counts")
    if not isinstance(counts, list) or not counts or len(counts) > len(planned):
        decision["reason"] = "missing_or_malformed_result_counts"
        return decision
    for index, count in enumerate(counts):
        if not isinstance(count, dict) or "results" not in count or "matching_manga" not in count:
            decision["reason"] = "missing_or_malformed_result_counts"
            return decision
        query = providers.normalized_query(count.get("query"))
        try:
            result_count = int(count.get("results"))
            matching_count = int(count.get("matching_manga"))
        except (TypeError, ValueError):
            decision["reason"] = "missing_or_malformed_result_counts"
            return decision
        if query.casefold() != planned_keys[index] or result_count < 0 or matching_count < 0:
            decision["reason"] = "missing_or_malformed_result_counts"
            return decision
        if result_count or matching_count:
            decision["reason"] = "productive_query"
            return decision
    decision.update(
        {
            "offset": (window_start + len(counts)) % len(queries),
            "previous_completed_query_count": len(counts),
            "previous_last_query": planned[len(counts) - 1],
            "advanced_by": len(counts),
            "reason": "completed_clean_zero_queries",
        }
    )
    return decision


def _suwayomi_source_error_key(row):
    row = row if isinstance(row, dict) else {}
    source_id = str(row.get("source_id") or row.get("sourceId") or "").strip()
    source_name = _first_text(
        row.get("source_display_name"),
        row.get("source_name"),
        row.get("source_base_name"),
        row.get("name"),
    )
    if source_id:
        return ("id", source_id, source_id, source_name)
    source_name_key = _text_key(source_name)
    if source_name_key:
        return ("name", source_name_key, "", source_name)
    return None


def _source_error_bucket_add_event(bucket, event, *, kind, attempt_row):
    bucket[kind] = int(bucket.get(kind) or 0) + 1
    bucket["error_count"] = int(bucket.get("error_count") or 0) + 1
    attempt_id = str((attempt_row or {}).get("id") or "").strip()
    if attempt_id:
        bucket.setdefault("_attempt_ids", set()).add(attempt_id)
    attempt_at = float((attempt_row or {}).get("attempt_at") or 0)
    if attempt_at:
        bucket["last_seen_at"] = max(float(bucket.get("last_seen_at") or 0), attempt_at)
    for key, target in (("queue_id", "queue_ids"), ("title", "sample_titles")):
        value = str((attempt_row or {}).get(key) or "").strip()
        if value:
            values = bucket.setdefault(target, [])
            if value not in values and len(values) < 5:
                values.append(value)
    query = str((event or {}).get("query") or "").strip()
    if query:
        values = bucket.setdefault("sample_queries", [])
        if query not in values and len(values) < 5:
            values.append(query)
    error = str((event or {}).get("error") or (event or {}).get("previous_error") or "").strip()
    if error and not bucket.get("error_sample"):
        bucket["error_sample"] = providers.clipped_text(error, 300)


def _suwayomi_recent_source_error_buckets(db_path, *, now, window_seconds):
    if not db_path:
        return []
    path = Path(str(db_path))
    if not path.exists():
        return []
    cutoff = max(0.0, float(now or time.time()) - max(60, int(window_seconds or 0)))
    buckets = {}
    con = None
    try:
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        if not _sqlite_table_exists(con, "source_attempts"):
            return []
        rows = con.execute(
            """
            select id, queue_id, title, raw_json,
                   coalesce(completed_at, started_at, 0) as attempt_at
              from source_attempts
             where (
                    lower(coalesce(provider_id, '')) = 'suwayomi'
                 or lower(coalesce(source, '')) = 'suwayomi'
                 or lower(coalesce(provider, '')) = 'suwayomi'
             )
               and coalesce(completed_at, started_at, 0) >= ?
             order by coalesce(completed_at, started_at, 0) desc, id desc
             limit ?
            """,
            (cutoff, SUWAYOMI_SOURCE_ERROR_COOLDOWN_SCAN_LIMIT),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if con is not None:
            con.close()
    for row in rows:
        attempt_row = dict(row)
        fetch = _source_attempt_fetch_evidence(attempt_row.get("raw_json"))
        if not fetch:
            continue
        events = []
        for event in fetch.get("partial_errors") or []:
            if not isinstance(event, dict):
                continue
            stage = str(event.get("stage") or "").strip()
            counter = SUWAYOMI_SOURCE_ERROR_PARTIAL_ERROR_STAGES.get(stage)
            if counter:
                events.append((counter, event))
        for event in fetch.get("source_runtime_skips") or []:
            if (
                isinstance(event, dict)
                and str(event.get("stage") or "") == "source_search_skipped_after_error"
            ):
                events.append(("runtime_skip_count", event))
        for kind, event in events:
            source_key = _suwayomi_source_error_key(event)
            if not source_key:
                continue
            key_kind, key_value, source_id, source_name = source_key
            bucket_key = f"{key_kind}:{key_value}"
            bucket = buckets.setdefault(
                bucket_key,
                {
                    "source_id": source_id,
                    "source_display_name": source_name,
                    "source_key": bucket_key,
                    "error_count": 0,
                    "source_search_error_count": 0,
                    "manga_chapter_lookup_error_count": 0,
                    "chapter_page_lookup_error_count": 0,
                    "runtime_skip_count": 0,
                    "last_seen_at": 0.0,
                    "_attempt_ids": set(),
                },
            )
            if source_id and not bucket.get("source_id"):
                bucket["source_id"] = source_id
            if source_name and not bucket.get("source_display_name"):
                bucket["source_display_name"] = source_name
            _source_error_bucket_add_event(bucket, event, kind=kind, attempt_row=attempt_row)
    out = []
    for bucket in buckets.values():
        attempt_ids = bucket.pop("_attempt_ids", set())
        bucket["attempt_count"] = len(attempt_ids)
        if bucket.get("last_seen_at"):
            bucket["last_seen_at"] = float(bucket.get("last_seen_at") or 0)
        out.append(_clean_dict(bucket))
    return sorted(
        out,
        key=lambda item: (
            -int(item.get("error_count") or 0),
            -float(item.get("last_seen_at") or 0),
            str(item.get("source_display_name") or item.get("source_id") or ""),
        ),
    )


def _suwayomi_series_key(value):
    text = providers.normalized_query(value)
    if not text:
        return ""
    return _text_key(text)


def _suwayomi_structured_target_unit(wanted_item):
    wanted_item = _dict(wanted_item)
    unit_type = str(_first_text(wanted_item.get("unit_type"), wanted_item.get("unitType"))).strip().lower()
    if providers.wanted_item_is_volume_unit(wanted_item):
        number = _first_text(
            wanted_item.get("volume"),
            wanted_item.get("volume_number"),
            wanted_item.get("volumeNumber"),
            wanted_item.get("book_volume"),
            wanted_item.get("manga_volume"),
            wanted_item.get("issue_number"),
            wanted_item.get("normalized_number"),
        )
        return "volume", number
    if unit_type == "chapter":
        return "chapter", _first_text(
            wanted_item.get("chapter"),
            wanted_item.get("chapter_number"),
            wanted_item.get("chapterNumber"),
        )
    if unit_type == "issue":
        return "issue", _first_text(
            wanted_item.get("issue"),
            wanted_item.get("issue_number"),
            wanted_item.get("normalized_number"),
        )
    return "", ""


def _suwayomi_unit_number_matches(left, right):
    left = str(left or "").strip()
    right = str(right or "").strip()
    if not left or not right:
        return False
    try:
        return float(left) == float(right)
    except Exception:
        return left.casefold() == right.casefold()


def _suwayomi_project_attempt_unit_suffix(series_key, wanted_item):
    unit_type, wanted_number = _suwayomi_structured_target_unit(wanted_item)
    patterns = {
        "volume": r"^(.+?)\s+(?:v|vol|volume|book)\s*0*(\d+(?:\.\d+)?)$",
        "chapter": r"^(.+?)\s+(?:ch|chapter)\s*0*(\d+(?:\.\d+)?)$",
        "issue": r"^(.+?)\s+(?:issue\s*|#\s*)0*(\d+(?:\.\d+)?)$",
    }
    pattern = patterns.get(unit_type)
    match = re.fullmatch(pattern, series_key) if pattern else None
    if not match or not _suwayomi_unit_number_matches(match.group(2), wanted_number):
        return ""
    return _text_key(match.group(1))


def _suwayomi_wanted_release_years(wanted_item):
    wanted_item = _dict(wanted_item)
    years = set()
    for key in ("series_year", "seriesYear", "publication_year", "publicationYear", "year"):
        match = re.fullmatch(r"(?:19|20)\d{2}", str(wanted_item.get(key) or "").strip())
        if match:
            years.add(match.group(0))
    return years


def _suwayomi_attempt_series_keys(value, wanted_item=None, *, release_years=()):
    series_key = _suwayomi_series_key(value)
    if not series_key:
        return []
    keys = [series_key]
    projected_unit_key = _suwayomi_project_attempt_unit_suffix(series_key, wanted_item)
    if projected_unit_key and projected_unit_key not in keys:
        keys.append(projected_unit_key)
    for key in list(keys):
        match = re.fullmatch(r"(.+?)\s+(?:\(|\[)?((?:19|20)\d{2})(?:\)|\])?$", key)
        if match and match.group(2) in set(release_years or ()):
            release_key = _text_key(match.group(1))
            if release_key and release_key not in keys:
                keys.append(release_key)
    return keys


def _suwayomi_wanted_series_keys(wanted_item):
    wanted_item = _dict(wanted_item)
    keys = []
    seen = set()
    canonical_values = [
        wanted_item.get("canonical_work_title"),
        wanted_item.get("series_title"),
        wanted_item.get("series"),
        wanted_item.get("manga_title"),
    ]
    if not any(str(value or "").strip() for value in canonical_values):
        canonical_values.append(wanted_item.get("title"))
    for value in [*canonical_values, *providers.series_identity_aliases(wanted_item)]:
        series_key = _suwayomi_series_key(value)
        if series_key and series_key not in seen:
            seen.add(series_key)
            keys.append(series_key)
    return keys


def _suwayomi_volume_gap_attempt_matches(attempt_row, raw, wanted_item):
    wanted_item = _dict(wanted_item)
    wanted_series_id = str(wanted_item.get("series_id") or "").strip()
    attempt_series_id = str((attempt_row or {}).get("series_id") or "").strip()
    if wanted_series_id and attempt_series_id:
        return wanted_series_id == attempt_series_id
    wanted_keys = _suwayomi_wanted_series_keys(wanted_item)
    if not wanted_keys:
        return False
    attempt_values = [
        (attempt_row or {}).get("title"),
        (raw or {}).get("title"),
        (raw or {}).get("query"),
    ]
    release_years = _suwayomi_wanted_release_years(wanted_item)
    attempt_keys = [
        key
        for value in attempt_values
        for key in _suwayomi_attempt_series_keys(value, wanted_item, release_years=release_years)
    ]
    if not attempt_keys:
        return False
    return bool(set(wanted_keys).intersection(attempt_keys))


def _suwayomi_recent_volume_gap_buckets(db_path, *, now, window_seconds, wanted_item=None):
    wanted_item = _dict(wanted_item)
    if not providers.wanted_item_is_volume_unit(wanted_item):
        return []
    if not db_path:
        return []
    path = Path(str(db_path))
    if not path.exists():
        return []
    cutoff = max(0.0, float(now or time.time()) - max(60, int(window_seconds or 0)))
    wanted_volume = _suwayomi_wanted_number(wanted_item, "volume", "volume_number", "volumeNumber")
    buckets = {}
    con = None
    try:
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        if not _sqlite_table_exists(con, "source_attempts"):
            return []
        rows = con.execute(
            """
            select id, queue_id, series_id, title, failure_reason, raw_json,
                   coalesce(completed_at, started_at, 0) as attempt_at
              from source_attempts
             where (
                    lower(coalesce(provider_id, '')) = 'suwayomi'
                 or lower(coalesce(source, '')) = 'suwayomi'
                 or lower(coalesce(provider, '')) = 'suwayomi'
             )
               and coalesce(completed_at, started_at, 0) >= ?
               and (
                    lower(coalesce(failure_reason, '')) in ('suwayomi_volume_metadata_missing', 'suwayomi_volume_metadata_invalid', 'suwayomi_volume_page_evidence_missing')
                 or coalesce(raw_json, '') like '%suwayomi_volume_metadata_missing%'
                 or coalesce(raw_json, '') like '%suwayomi_volume_metadata_invalid%'
                 or coalesce(raw_json, '') like '%suwayomi_volume_page_evidence_missing%'
               )
             order by coalesce(completed_at, started_at, 0) desc, id desc
             limit ?
            """,
            (cutoff, SUWAYOMI_SOURCE_ERROR_COOLDOWN_SCAN_LIMIT),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if con is not None:
            con.close()
    for row in rows:
        attempt_row = dict(row)
        raw = _json_obj(attempt_row.get("raw_json"))
        if not _suwayomi_volume_gap_attempt_matches(attempt_row, raw, wanted_item):
            continue
        fetch = _source_attempt_fetch_evidence(attempt_row.get("raw_json"))
        summaries = [item for item in fetch.get("suwayomi_payload_summaries") or [] if isinstance(item, dict)]
        if not summaries:
            continue
        reason = str(attempt_row.get("failure_reason") or raw.get("failure_reason") or raw.get("reason") or "").strip()
        if reason not in {
            "suwayomi_volume_metadata_missing",
            "suwayomi_volume_metadata_invalid",
            "suwayomi_volume_page_evidence_missing",
        }:
            reason = "suwayomi_volume_evidence_gap"
        for summary in summaries:
            source_key = _suwayomi_source_error_key(
                {
                    "source_id": summary.get("source_id"),
                    "source_display_name": summary.get("source_name"),
                }
            )
            if not source_key:
                continue
            try:
                chapter_count = int(summary.get("chapter_count") or 0)
            except Exception:
                chapter_count = 0
            try:
                matching_volume_count = int(summary.get("chapter_matching_wanted_volume_count") or 0)
            except Exception:
                matching_volume_count = 0
            if chapter_count <= 0 and matching_volume_count <= 0:
                continue
            if wanted_volume and matching_volume_count > 0 and reason != "suwayomi_volume_page_evidence_missing":
                continue
            key_kind, key_value, source_id, source_name = source_key
            bucket_key = f"{key_kind}:{key_value}"
            bucket = buckets.setdefault(
                bucket_key,
                {
                    "source_id": source_id,
                    "source_display_name": source_name,
                    "source_key": bucket_key,
                    "error_count": 0,
                    "volume_evidence_gap_count": 0,
                    "volume_metadata_missing_count": 0,
                    "volume_metadata_invalid_count": 0,
                    "volume_page_evidence_missing_count": 0,
                    "last_seen_at": 0.0,
                    "_attempt_ids": set(),
                },
            )
            if source_id and not bucket.get("source_id"):
                bucket["source_id"] = source_id
            if source_name and not bucket.get("source_display_name"):
                bucket["source_display_name"] = source_name
            event = {
                "query": raw.get("query") or attempt_row.get("title"),
                "source_id": source_id,
                "source_display_name": source_name,
                "error": reason,
                "manga_title": summary.get("manga_title"),
            }
            _source_error_bucket_add_event(
                bucket,
                event,
                kind="volume_evidence_gap_count",
                attempt_row=attempt_row,
            )
            if reason == "suwayomi_volume_metadata_missing":
                bucket["volume_metadata_missing_count"] = int(bucket.get("volume_metadata_missing_count") or 0) + 1
            elif reason == "suwayomi_volume_metadata_invalid":
                bucket["volume_metadata_invalid_count"] = int(bucket.get("volume_metadata_invalid_count") or 0) + 1
            elif reason == "suwayomi_volume_page_evidence_missing":
                bucket["volume_page_evidence_missing_count"] = int(bucket.get("volume_page_evidence_missing_count") or 0) + 1
    out = []
    for bucket in buckets.values():
        attempt_ids = bucket.pop("_attempt_ids", set())
        bucket["attempt_count"] = len(attempt_ids)
        if bucket.get("last_seen_at"):
            bucket["last_seen_at"] = float(bucket.get("last_seen_at") or 0)
        out.append(_clean_dict(bucket))
    return sorted(
        out,
        key=lambda item: (
            -int(item.get("error_count") or 0),
            -float(item.get("last_seen_at") or 0),
            str(item.get("source_display_name") or item.get("source_id") or ""),
        ),
    )


def _extend_unique_strings(existing, additions):
    out = []
    seen = set()
    for value in list(existing or []) + list(additions or []):
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _stable_rotation_offset(value, count):
    count = max(0, int(count or 0))
    if count <= 1:
        return 0
    digest = providers.url_hash(value)
    if not digest:
        return 0
    try:
        return int(digest[:12], 16) % count
    except Exception:
        return 0


def _rotated_list(values, offset):
    values = list(values or [])
    if not values:
        return []
    offset = int(offset or 0) % len(values)
    if not offset:
        return values
    return values[offset:] + values[:offset]


def _suwayomi_source_error_probe_buckets(buckets, *, now, after_seconds, max_sources=1, rotation_key=""):
    now = float(now or time.time())
    after_seconds = max(60, int(after_seconds or 0))
    candidates = []
    for bucket in buckets or []:
        if not isinstance(bucket, dict):
            continue
        if str(bucket.get("cooldown_kind") or "source_error_cooldown") not in {
            "source_error_cooldown",
            "volume_evidence_gap_cooldown",
        }:
            continue
        last_seen_at = float(bucket.get("last_seen_at") or 0)
        if not last_seen_at:
            continue
        if now - last_seen_at < after_seconds:
            continue
        candidates.append(bucket)
    candidates = sorted(
        candidates,
        key=lambda item: (
            int(item.get("error_count") or 0),
            float(item.get("last_seen_at") or 0),
            str(item.get("source_display_name") or item.get("source_id") or ""),
        ),
    )
    limit = max(1, int(max_sources or 1))
    if len(candidates) > limit and str(rotation_key or "").strip():
        candidates = _rotated_list(candidates, _stable_rotation_offset(rotation_key, len(candidates)))
    return candidates[:limit]


def _suwayomi_probe_buckets_by_kind(buckets, cooldown_kind):
    return [
        bucket
        for bucket in buckets or []
        if isinstance(bucket, dict) and str(bucket.get("cooldown_kind") or "source_error_cooldown") == cooldown_kind
    ]


def _suwayomi_probe_rotation_key(job, row, plan, wanted_item):
    job = _dict(job)
    row = _dict(row)
    plan = _dict(plan)
    wanted_item = _dict(wanted_item)
    fetch_plan = _dict(job.get("fetch_plan"))
    parts = []
    for source in (job, wanted_item, plan, row):
        for key in (
            "queue_id",
            "wanted_id",
            "series_id",
            "issue_id",
            "id",
            "series_title",
            "title",
            "query",
            "issue_number",
            "chapter_number",
            "volume_number",
        ):
            value = str(source.get(key) or "").strip()
            if value:
                parts.append(f"{key}:{value}")
    for value in fetch_plan.get("query_variants") or []:
        text = str(value or "").strip()
        if text:
            parts.append(f"query_variant:{text}")
    return "|".join(parts)


def _suwayomi_source_error_bucket_key(bucket):
    bucket = bucket if isinstance(bucket, dict) else {}
    source_id = str(bucket.get("source_id") or "").strip()
    if source_id:
        return f"id:{source_id}"
    source_name = _text_key(bucket.get("source_display_name") or bucket.get("source_name"))
    if source_name:
        return f"name:{source_name}"
    return ""


def _merge_suwayomi_source_error_buckets(existing, additions, *, cooldown_kind):
    out = []
    by_key = {}
    quarantine_kind = "persistent_source_error_quarantine"

    def add_bucket(bucket, kind=""):
        if not isinstance(bucket, dict):
            return
        key = _suwayomi_source_error_bucket_key(bucket)
        if not key:
            return
        item = dict(bucket)
        if kind:
            item["cooldown_kind"] = kind
        item["cooldown_kind"] = item.get("cooldown_kind") or "source_error_cooldown"
        prior = by_key.get(key)
        if prior is None:
            by_key[key] = item
            out.append(item)
            return
        replace = (
            item.get("cooldown_kind") == quarantine_kind
            and prior.get("cooldown_kind") != quarantine_kind
        ) or int(item.get("error_count") or 0) > int(prior.get("error_count") or 0)
        if replace:
            prior.update(item)

    for bucket in existing or []:
        add_bucket(bucket)
    for bucket in additions or []:
        add_bucket(bucket, cooldown_kind)
    return sorted(
        out,
        key=lambda item: (
            0 if item.get("cooldown_kind") == quarantine_kind else 1,
            -int(item.get("error_count") or 0),
            -float(item.get("last_seen_at") or 0),
            str(item.get("source_display_name") or item.get("source_id") or ""),
        ),
    )


def _row_with_suwayomi_persisted_source_error_cooldown(row, db_path, *, now=None, rotation_key="", wanted_item=None):
    row = _dict(row)
    if not _suwayomi_job_row(row) or not db_path:
        return row, {}
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    source_error_enabled = _policy_bool(policy, ("suwayomi_source_error_cooldown_enabled",), True)
    volume_gap_enabled = _policy_bool(policy, ("suwayomi_volume_gap_cooldown_enabled",), True)
    if source_error_enabled is False and volume_gap_enabled is False:
        return row, {}
    window_seconds = _policy_int(
        policy,
        ("suwayomi_source_error_cooldown_seconds",),
        SUWAYOMI_SOURCE_ERROR_COOLDOWN_DEFAULT_SECONDS,
    )
    window_seconds = max(60, min(int(window_seconds or 0), 7 * 24 * 60 * 60))
    threshold = _policy_int(
        policy,
        ("suwayomi_source_error_cooldown_threshold",),
        SUWAYOMI_SOURCE_ERROR_COOLDOWN_DEFAULT_THRESHOLD,
    )
    threshold = max(1, min(int(threshold or 0), 100))
    max_sources = _policy_int(
        policy,
        ("suwayomi_source_error_cooldown_max_sources",),
        SUWAYOMI_SOURCE_ERROR_COOLDOWN_DEFAULT_MAX_SOURCES,
    )
    max_sources = max(1, min(int(max_sources or 0), 50))
    scan_now = now if now is not None else time.time()
    cooldown_buckets = []
    if source_error_enabled is not False:
        cooldown_buckets = [
            bucket
            for bucket in _suwayomi_recent_source_error_buckets(
                db_path,
                now=scan_now,
                window_seconds=window_seconds,
            )
            if int(bucket.get("error_count") or 0) >= threshold
        ][:max_sources]
    buckets = _merge_suwayomi_source_error_buckets(
        [],
        cooldown_buckets,
        cooldown_kind="source_error_cooldown",
    )
    volume_gap_window_seconds = _policy_int(
        policy,
        ("suwayomi_volume_gap_cooldown_seconds",),
        SUWAYOMI_VOLUME_GAP_COOLDOWN_DEFAULT_SECONDS,
    )
    volume_gap_window_seconds = max(60, min(int(volume_gap_window_seconds or 0), 14 * 24 * 60 * 60))
    volume_gap_threshold = _policy_int(
        policy,
        ("suwayomi_volume_gap_cooldown_threshold",),
        SUWAYOMI_VOLUME_GAP_COOLDOWN_DEFAULT_THRESHOLD,
    )
    volume_gap_threshold = max(1, min(int(volume_gap_threshold or 0), 100))
    volume_metadata_gap_threshold = _policy_int(
        policy,
        ("suwayomi_volume_metadata_gap_cooldown_threshold",),
        SUWAYOMI_VOLUME_METADATA_GAP_COOLDOWN_DEFAULT_THRESHOLD,
    )
    volume_metadata_gap_threshold = max(1, min(int(volume_metadata_gap_threshold or 0), 100))
    volume_gap_max_sources = _policy_int(
        policy,
        ("suwayomi_volume_gap_cooldown_max_sources",),
        SUWAYOMI_VOLUME_GAP_COOLDOWN_DEFAULT_MAX_SOURCES,
    )
    volume_gap_max_sources = max(1, min(int(volume_gap_max_sources or 0), 50))
    volume_gap_buckets = []
    if volume_gap_enabled is not False:
        volume_gap_buckets = [
            bucket
            for bucket in _suwayomi_recent_volume_gap_buckets(
                db_path,
                now=scan_now,
                window_seconds=volume_gap_window_seconds,
                wanted_item=wanted_item,
            )
            if int(bucket.get("error_count") or 0) >= volume_gap_threshold
            or int(bucket.get("volume_metadata_missing_count") or 0) >= volume_metadata_gap_threshold
            or int(bucket.get("volume_metadata_invalid_count") or 0) >= volume_metadata_gap_threshold
        ][:volume_gap_max_sources]
        buckets = _merge_suwayomi_source_error_buckets(
            buckets,
            volume_gap_buckets,
            cooldown_kind="volume_evidence_gap_cooldown",
        )
    quarantine_enabled = _policy_bool(policy, ("suwayomi_source_error_quarantine_enabled",), True)
    quarantine_window_seconds = _policy_int(
        policy,
        ("suwayomi_source_error_quarantine_seconds", "suwayomi_source_error_quarantine_window_seconds"),
        SUWAYOMI_SOURCE_ERROR_QUARANTINE_DEFAULT_SECONDS,
    )
    quarantine_window_seconds = max(60, min(int(quarantine_window_seconds or 0), 14 * 24 * 60 * 60))
    quarantine_threshold = _policy_int(
        policy,
        ("suwayomi_source_error_quarantine_threshold",),
        SUWAYOMI_SOURCE_ERROR_QUARANTINE_DEFAULT_THRESHOLD,
    )
    quarantine_threshold = max(1, min(int(quarantine_threshold or 0), 500))
    quarantine_max_sources = _policy_int(
        policy,
        ("suwayomi_source_error_quarantine_max_sources",),
        SUWAYOMI_SOURCE_ERROR_QUARANTINE_DEFAULT_MAX_SOURCES,
    )
    quarantine_max_sources = max(1, min(int(quarantine_max_sources or 0), 50))
    quarantine_buckets = []
    if quarantine_enabled is not False and source_error_enabled is not False:
        quarantine_buckets = [
            bucket
            for bucket in _suwayomi_recent_source_error_buckets(
                db_path,
                now=scan_now,
                window_seconds=quarantine_window_seconds,
            )
            if int(bucket.get("error_count") or 0) >= quarantine_threshold
        ][:quarantine_max_sources]
        buckets = _merge_suwayomi_source_error_buckets(
            buckets,
            quarantine_buckets,
            cooldown_kind="persistent_source_error_quarantine",
        )
    if not buckets:
        return row, {}
    cooldown_ids = [bucket.get("source_id") for bucket in buckets if bucket.get("source_id")]
    cooldown_names = [
        bucket.get("source_display_name")
        for bucket in buckets
        if not bucket.get("source_id") and bucket.get("source_display_name")
    ]
    if not cooldown_ids and not cooldown_names:
        return row, {}
    probe_enabled = _policy_bool(
        policy,
        ("suwayomi_source_error_cooldown_probe_enabled",),
        True,
    )
    probe_after_seconds = _policy_int(
        policy,
        ("suwayomi_source_error_cooldown_probe_after_seconds",),
        SUWAYOMI_SOURCE_ERROR_COOLDOWN_PROBE_DEFAULT_AFTER_SECONDS,
    )
    probe_after_seconds = max(60, min(int(probe_after_seconds or 0), window_seconds))
    probe_max_sources = _policy_int(
        policy,
        ("suwayomi_source_error_cooldown_probe_max_sources",),
        SUWAYOMI_SOURCE_ERROR_COOLDOWN_PROBE_DEFAULT_MAX_SOURCES,
    )
    probe_max_sources = max(1, min(int(probe_max_sources or 1), max_sources))
    volume_gap_probe_enabled = _policy_bool(
        policy,
        ("suwayomi_volume_gap_cooldown_probe_enabled",),
        True,
    )
    volume_gap_probe_after_seconds = _policy_int(
        policy,
        ("suwayomi_volume_gap_cooldown_probe_after_seconds",),
        SUWAYOMI_VOLUME_GAP_COOLDOWN_PROBE_DEFAULT_AFTER_SECONDS,
    )
    volume_gap_probe_after_seconds = max(60, min(int(volume_gap_probe_after_seconds or 0), volume_gap_window_seconds))
    volume_gap_probe_max_sources = _policy_int(
        policy,
        ("suwayomi_volume_gap_cooldown_probe_max_sources",),
        SUWAYOMI_VOLUME_GAP_COOLDOWN_PROBE_DEFAULT_MAX_SOURCES,
    )
    volume_gap_probe_max_sources = max(1, min(int(volume_gap_probe_max_sources or 1), volume_gap_max_sources))
    probe_buckets = []
    probe_rotation_hash = ""
    source_error_probe_buckets = []
    volume_gap_probe_buckets = []
    if probe_enabled is not False:
        source_error_probe_buckets = _suwayomi_source_error_probe_buckets(
            _suwayomi_probe_buckets_by_kind(buckets, "source_error_cooldown"),
            now=scan_now,
            after_seconds=probe_after_seconds,
            max_sources=probe_max_sources,
            rotation_key=rotation_key,
        )
        if volume_gap_probe_enabled is not False:
            volume_gap_probe_buckets = _suwayomi_source_error_probe_buckets(
                _suwayomi_probe_buckets_by_kind(buckets, "volume_evidence_gap_cooldown"),
                now=scan_now,
                after_seconds=volume_gap_probe_after_seconds,
                max_sources=volume_gap_probe_max_sources,
                rotation_key=rotation_key,
            )
        probe_buckets = _merge_suwayomi_source_error_buckets(
            source_error_probe_buckets,
            volume_gap_probe_buckets,
            cooldown_kind="",
        )
        if str(rotation_key or "").strip() and len(buckets) > len(probe_buckets or []):
            probe_rotation_hash = providers.url_hash(rotation_key)[:12]
    next_row = dict(row)
    next_policy = dict(policy)
    next_policy["suwayomi_source_cooldown_ids"] = _extend_unique_strings(
        next_policy.get("suwayomi_source_cooldown_ids") or [],
        cooldown_ids,
    )
    if cooldown_names:
        next_policy["suwayomi_source_cooldown_names"] = _extend_unique_strings(
            next_policy.get("suwayomi_source_cooldown_names") or [],
            cooldown_names,
        )
    cooldown_reasons_by_id = {}
    cooldown_reasons_by_name = {}
    for bucket in buckets:
        if str(bucket.get("cooldown_kind") or "") != "volume_evidence_gap_cooldown":
            continue
        source_id = str(bucket.get("source_id") or "").strip()
        source_name = str(bucket.get("source_display_name") or "").strip()
        if source_id:
            cooldown_reasons_by_id[source_id] = "volume_evidence_gap_cooldown"
        elif source_name:
            cooldown_reasons_by_name[source_name] = "volume_evidence_gap_cooldown"
    if cooldown_reasons_by_id:
        merged_reasons_by_id = dict(next_policy.get("suwayomi_source_cooldown_reasons_by_id") or {})
        merged_reasons_by_id.update(cooldown_reasons_by_id)
        next_policy["suwayomi_source_cooldown_reasons_by_id"] = merged_reasons_by_id
    if cooldown_reasons_by_name:
        merged_reasons_by_name = dict(next_policy.get("suwayomi_source_cooldown_reasons_by_name") or {})
        merged_reasons_by_name.update(cooldown_reasons_by_name)
        next_policy["suwayomi_source_cooldown_reasons_by_name"] = merged_reasons_by_name
    if probe_enabled is not False:
        probe_ids = [bucket.get("source_id") for bucket in probe_buckets if bucket.get("source_id")]
        probe_names = [
            bucket.get("source_display_name")
            for bucket in probe_buckets
            if not bucket.get("source_id") and bucket.get("source_display_name")
        ]
        if probe_ids:
            next_policy["suwayomi_source_cooldown_probe_ids"] = _extend_unique_strings(
                next_policy.get("suwayomi_source_cooldown_probe_ids") or [],
                probe_ids,
            )
        if probe_names:
            next_policy["suwayomi_source_cooldown_probe_names"] = _extend_unique_strings(
                next_policy.get("suwayomi_source_cooldown_probe_names") or [],
                probe_names,
            )
        if probe_buckets:
            next_policy["suwayomi_source_cooldown_probe_max_sources"] = max(
                providers.int_value(next_policy.get("suwayomi_source_cooldown_probe_max_sources"), 0) or 0,
                len(probe_buckets),
            )
    next_row["policy"] = next_policy
    summary = _clean_dict(
        {
            "enabled": True,
            "source_error_enabled": source_error_enabled is not False,
            "window_seconds": window_seconds,
            "threshold": threshold,
            "max_sources": max_sources,
            "source_count": len(buckets),
            "volume_gap_enabled": volume_gap_enabled is not False,
            "volume_gap_window_seconds": volume_gap_window_seconds,
            "volume_gap_threshold": volume_gap_threshold,
            "volume_metadata_gap_threshold": volume_metadata_gap_threshold,
            "volume_gap_max_sources": volume_gap_max_sources,
            "volume_gap_source_count": len(volume_gap_buckets),
            "probe_enabled": probe_enabled is not False,
            "probe_after_seconds": probe_after_seconds,
            "probe_max_sources": probe_max_sources,
            "source_error_probe_source_count": len(source_error_probe_buckets),
            "volume_gap_probe_enabled": volume_gap_probe_enabled is not False,
            "volume_gap_probe_after_seconds": volume_gap_probe_after_seconds,
            "volume_gap_probe_max_sources": volume_gap_probe_max_sources,
            "volume_gap_probe_source_count": len(volume_gap_probe_buckets),
            "probe_source_count": len(probe_buckets),
            "probe_rotation_hash": probe_rotation_hash,
            "probe_sources": probe_buckets[:5],
            "quarantine_enabled": quarantine_enabled is not False,
            "quarantine_window_seconds": quarantine_window_seconds,
            "quarantine_threshold": quarantine_threshold,
            "quarantine_max_sources": quarantine_max_sources,
            "quarantine_source_count": len(quarantine_buckets),
            "sources": buckets[:20],
        }
    )
    return next_row, summary


def _rss_fresh_release_source(row):
    row = _dict(row)
    provider_id = str(row.get("provider_id") or "").strip().lower()
    source_kind = str(row.get("source_kind") or "").strip().lower()
    if provider_id in RSS_FRESH_RELEASE_PROVIDER_IDS or provider_id.startswith("rss_"):
        return True
    return source_kind in RSS_FRESH_RELEASE_SOURCE_KINDS


def _wanted_release_dates(wanted_item):
    wanted_item = _dict(wanted_item)
    out = []
    seen = set()
    for key in (
        "release_date",
        "issue_date",
        "date",
        "publish_date",
        "publishedAt",
        "publishAt",
        "publication_date",
        "cover_date",
        "coverDate",
    ):
        value = str(wanted_item.get(key) or "").strip()
        if not value:
            continue
        match = re.search(r"\b((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", value)
        if not match:
            continue
        try:
            parsed = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
        except ValueError:
            continue
        if parsed not in seen:
            seen.add(parsed)
            out.append(parsed)
    return out


def _wanted_release_years(wanted_item):
    wanted_item = _dict(wanted_item)
    out = []
    seen = set()

    def add(value):
        text = str(value or "").strip()
        if not text:
            return
        for match in re.finditer(r"\b((?:19|20)\d{2})\b", text):
            year = int(match.group(1))
            if year not in seen:
                seen.add(year)
                out.append(year)
            return

    for key in (
        "year",
        "issue_year",
        "publication_year",
        "release_year",
        "start_year",
        "cover_year",
        "watch_year",
        "release_date",
        "issue_date",
        "date",
        "publish_date",
        "publishedAt",
        "publishAt",
        "cover_date",
        "coverDate",
    ):
        add(wanted_item.get(key))
    return out


def _rss_fresh_release_scope_block_reason(row, wanted_item):
    row = _dict(row)
    wanted_item = _dict(wanted_item)
    if not _rss_fresh_release_source(row):
        return ""
    # Interactive Manual Search is a bounded, read-only discovery action. It
    # must be able to inspect older feed evidence without changing the stricter
    # freshness gate used by automated acquisition workers.
    if wanted_item.get("manual_search") is True:
        return ""
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    fresh_only = _policy_bool(policy, ("rss_fresh_release_only", "fresh_release_only"), default=True)
    if fresh_only is False or _truthy(policy.get("rss_backfill_allowed")) or _truthy(policy.get("backfill_allowed")):
        return ""
    max_age_days = _policy_int(
        policy,
        ("rss_fresh_release_max_age_days", "fresh_release_max_age_days", "max_release_age_days"),
        DEFAULT_RSS_FRESH_RELEASE_MAX_AGE_DAYS,
    )
    max_age_days = max(1, min(max_age_days, 3650))
    today = date.today()
    cutoff = today - timedelta(days=max_age_days)
    display_name = _first_text(row.get("display_name"), row.get("provider_id"), "RSS source")
    dates = _wanted_release_dates(wanted_item)
    if dates:
        newest = max(dates)
        if newest < cutoff:
            age_days = (today - newest).days
            return f"{display_name} is scoped to fresh releases; release date {newest.isoformat()} is {age_days} days old, over the {max_age_days}-day limit"
        return ""
    years = _wanted_release_years(wanted_item)
    if years:
        newest_year = max(years)
        if newest_year < cutoff.year:
            return f"{display_name} is scoped to fresh releases; release year {newest_year} is older than the {max_age_days}-day window"
    return ""


def _first_request(fetch_plan):
    requests = (fetch_plan or {}).get("requests")
    if isinstance(requests, list) and requests and isinstance(requests[0], dict):
        return requests[0]
    return {}


def _safe_request_url(request):
    url = str((request or {}).get("url") or "").strip()
    if not url or providers._url_has_secretish_query(url):
        return ""
    return url


SAFE_REQUEST_PARAM_KEYS = {
    "cat",
    "categories",
    "extended",
    "indexerIds",
    "limit",
    "per_page",
    "q",
    "query",
    "search",
    "searchTerm",
    "subtype",
    "t",
    "title",
}

FETCH_EVIDENCE_ATTEMPT_STATUSES = {
    "blocked",
    "provider_unavailable",
    "provider_wait",
    "searched_no_candidates",
}


def _clean_dict(value):
    value = value if isinstance(value, dict) else {}
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _request_evidence(request):
    request = _dict(request)
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    safe_params = {
        key: params.get(key)
        for key in SAFE_REQUEST_PARAM_KEYS
        if key in params and params.get(key) not in (None, "", [], {})
    }
    url = str(request.get("url") or "").strip()
    out = {
        "request_id": request.get("request_id"),
        "purpose": request.get("purpose"),
        "method": request.get("method"),
        "query": params.get("query") or params.get("q") or params.get("search") or params.get("searchTerm") or params.get("title"),
        "query_group": request.get("query_group"),
        "pack_query": bool(request.get("pack_query")),
        "source_id": request.get("source_id"),
        "source_display_name": request.get("source_display_name"),
        "source_error_cooldown_probe": bool(request.get("source_error_cooldown_probe")),
        "source_error_cooldown_probe_reason": request.get("source_error_cooldown_probe_reason"),
        "url_hash": providers.url_hash(url) if url else "",
        "params": safe_params,
    }
    return _clean_dict(out)


def _payload_result_count(payload):
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return len(payload.get("results") or [])
    if isinstance(payload, list):
        return len(payload)
    return None


def _first_payload_dict(fetch_result):
    for payload in (fetch_result or {}).get("payloads") or []:
        if isinstance(payload, dict):
            return payload
    return {}


def _suwayomi_wanted_number(wanted_item, *keys):
    wanted_item = _dict(wanted_item)
    for key in keys:
        value = str(wanted_item.get(key) or "").strip()
        if value:
            try:
                number = float(value)
            except Exception:
                return providers.normalized_query(value)
            if number.is_integer():
                return str(int(number))
            return str(number).rstrip("0").rstrip(".")
    return ""


def _suwayomi_payload_number(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
    except Exception:
        return providers.normalized_query(text)
    if number.is_integer():
        return str(int(number))
    return str(number).rstrip("0").rstrip(".")


def _suwayomi_payload_chapter_number(chapter_row):
    chapter_row = _dict(chapter_row)
    return _suwayomi_payload_number(
        _first_text(chapter_row.get("chapterNumber"), chapter_row.get("chapter"), chapter_row.get("number"))
    )


def _suwayomi_fetch_payload_summaries(fetch_result, wanted_item, *, limit=5):
    fetch_result = _dict(fetch_result)
    wanted_item = _dict(wanted_item)
    wanted_volume = _suwayomi_wanted_number(wanted_item, "volume", "volume_number", "volumeNumber")
    wanted_chapter = _suwayomi_wanted_number(wanted_item, "chapter", "chapter_number")
    pages_limit = max(0, int(limit or 0))
    summaries = []
    for payload in fetch_result.get("payloads") or []:
        if not isinstance(payload, dict):
            continue
        source = _dict(payload.get("source"))
        manga = _dict(payload.get("manga"))
        chapters = [row for row in payload.get("chapters") or [] if isinstance(row, dict)]
        pages_by_chapter = payload.get("pages_by_chapter") if isinstance(payload.get("pages_by_chapter"), dict) else {}
        volume_values = []
        seen_volumes = set()
        matching_volume_count = 0
        has_volume_count = 0
        matching_chapter_count = 0
        conflicting_volume_count = 0
        malformed_volume_count = 0
        invalid_volume_count = 0
        samples = []
        for chapter in chapters:
            chapter_number = _suwayomi_payload_chapter_number(chapter)
            volume_evidence = providers.suwayomi_explicit_volume_evidence(chapter)
            volume_number = (
                ""
                if volume_evidence.get("conflict") or volume_evidence.get("malformed")
                else str(volume_evidence.get("volume_number") or "").strip()
            )
            conflicting_volume_count += int(bool(volume_evidence.get("conflict")))
            malformed_volume_count += int(bool(volume_evidence.get("malformed")))
            invalid_volume_count += int(bool(volume_evidence.get("conflict") or volume_evidence.get("malformed")))
            if volume_number and volume_number not in seen_volumes:
                seen_volumes.add(volume_number)
                volume_values.append(volume_number)
            if volume_number:
                has_volume_count += 1
            if wanted_volume and volume_number == wanted_volume:
                matching_volume_count += 1
            if wanted_chapter and chapter_number == wanted_chapter:
                matching_chapter_count += 1
            if len(samples) < 5:
                chapter_id = str(chapter.get("id") or "").strip()
                page_payload = pages_by_chapter.get(chapter_id) if chapter_id else None
                pages = page_payload.get("pages") if isinstance(page_payload, dict) and isinstance(page_payload.get("pages"), list) else []
                samples.append(
                    _clean_dict(
                        {
                            "id": chapter_id,
                            "name": providers.clipped_text(chapter.get("name"), 120),
                            "chapter": chapter_number,
                            "volume": volume_number,
                            "volume_metadata_conflict": volume_evidence.get("conflict") or None,
                            "volume_metadata_malformed": volume_evidence.get("malformed") or None,
                            "source_order": chapter.get("sourceOrder"),
                            "page_count": chapter.get("pageCount"),
                            "pages_fetched": len(pages),
                        }
                    )
                )
        summaries.append(
            _clean_dict(
                {
                    "source_id": source.get("id") or source.get("sourceId"),
                    "source_name": source.get("displayName") or source.get("name"),
                    "source_language": source.get("lang"),
                    "source_extension_pkg_name": source.get("extension_pkg_name"),
                    "source_extension_obsolete": source.get("extension_obsolete"),
                    "source_extension_has_update": source.get("extension_has_update"),
                    "manga_id": manga.get("id"),
                    "manga_title": providers.clipped_text(_first_text(manga.get("title"), manga.get("name"), manga.get("mangaTitle")), 160),
                    "source_search_query": payload.get("source_search_query"),
                    "chapter_count": len(chapters),
                    "chapter_has_volume_count": has_volume_count,
                    "chapter_matching_wanted_volume_count": matching_volume_count if wanted_volume else "",
                    "chapter_conflicting_volume_count": conflicting_volume_count,
                    "chapter_malformed_volume_count": malformed_volume_count,
                    "chapter_invalid_volume_count": invalid_volume_count,
                    "chapter_matching_wanted_chapter_count": matching_chapter_count if wanted_chapter else "",
                    "pages_by_chapter_count": len(pages_by_chapter),
                    "observed_volume_numbers": volume_values[:10],
                    "chapter_samples": samples,
                }
            )
        )
        if len(summaries) >= pages_limit:
            break
    return summaries


def _fetch_evidence(job, fetch_result, *, reason=""):
    job = _dict(job)
    fetch_plan = _dict(job.get("fetch_plan"))
    fetch_result = _dict(fetch_result)
    payload = _first_payload_dict(fetch_result)
    planned_requests = [request for request in fetch_plan.get("requests") or [] if isinstance(request, dict)]
    try:
        estimated_request_count = int(fetch_plan.get("estimated_request_count") or len(planned_requests))
    except Exception:
        estimated_request_count = len(planned_requests)
    requests_made = [request for request in fetch_result.get("requests_made") or [] if isinstance(request, dict)]
    request_source = requests_made or planned_requests
    query_variants = list(fetch_plan.get("query_variants") or fetch_result.get("query_variants") or payload.get("query_variants") or [])
    mangadex_query_pool = list(fetch_plan.get("mangadex_query_pool") or [])
    variant_result_counts = list(fetch_result.get("variant_result_counts") or payload.get("variant_result_counts") or [])
    partial_errors = list(fetch_result.get("partial_errors") or payload.get("partial_errors") or [])
    meta_fallbacks = list(fetch_result.get("meta_fallbacks") or payload.get("meta_fallbacks") or [])
    suwayomi_extension_health = _dict(fetch_result.get("suwayomi_extension_health") or payload.get("suwayomi_extension_health"))
    suwayomi_source_selection = _dict(fetch_result.get("suwayomi_source_selection") or payload.get("suwayomi_source_selection"))
    suwayomi_source_error_cooldown = _dict(
        fetch_result.get("suwayomi_source_error_cooldown") or payload.get("suwayomi_source_error_cooldown")
    )
    source_runtime_skips = list(fetch_result.get("source_runtime_skips") or payload.get("source_runtime_skips") or [])
    feed_evidence = _dict(fetch_result.get("feed_evidence") or payload.get("feed_evidence"))
    suwayomi_payload_summaries = []
    if fetch_plan.get("payload_mode") == "suwayomi_search_then_chapters":
        suwayomi_payload_summaries = _suwayomi_fetch_payload_summaries(fetch_result, job.get("wanted_item"))
    pack_detail_fetch_count = payload.get("pack_detail_fetch_count") if isinstance(payload, dict) else None
    if pack_detail_fetch_count is None and fetch_plan.get("payload_mode") in {"prowlarr_multi_search", "indexer_multi_search"}:
        pack_detail_fetch_count = 0
    out = {
        "reason": reason or fetch_result.get("reason"),
        "payload_mode": fetch_plan.get("payload_mode"),
        "planned_request_count": len(planned_requests),
        "estimated_request_count": max(0, estimated_request_count),
        "requests_made_count": len(requests_made),
        "query_variants": query_variants,
        "mangadex_query_pool": mangadex_query_pool,
        "variant_result_counts": variant_result_counts,
        "suwayomi_extension_health": _clean_dict(
            {
                "extension_count": suwayomi_extension_health.get("extension_count"),
                "installed_count": suwayomi_extension_health.get("installed_count"),
                "obsolete_count": suwayomi_extension_health.get("obsolete_count"),
                "update_count": suwayomi_extension_health.get("update_count"),
                "selected_sources": suwayomi_extension_health.get("selected_sources"),
            }
        ),
        "suwayomi_source_selection": _clean_dict(
            {
                "selected_count": suwayomi_source_selection.get("selected_count"),
                "selected_sources": suwayomi_source_selection.get("selected_sources"),
                "cooldown_probe_count": suwayomi_source_selection.get("cooldown_probe_count"),
                "skipped_count": suwayomi_source_selection.get("skipped_count"),
                "skipped_sources": suwayomi_source_selection.get("skipped_sources"),
            }
        ),
        "suwayomi_source_error_cooldown": _clean_dict(
            {
                "enabled": suwayomi_source_error_cooldown.get("enabled"),
                "source_error_enabled": suwayomi_source_error_cooldown.get("source_error_enabled"),
                "window_seconds": suwayomi_source_error_cooldown.get("window_seconds"),
                "threshold": suwayomi_source_error_cooldown.get("threshold"),
                "max_sources": suwayomi_source_error_cooldown.get("max_sources"),
                "source_count": suwayomi_source_error_cooldown.get("source_count"),
                "volume_gap_enabled": suwayomi_source_error_cooldown.get("volume_gap_enabled"),
                "volume_gap_window_seconds": suwayomi_source_error_cooldown.get("volume_gap_window_seconds"),
                "volume_gap_threshold": suwayomi_source_error_cooldown.get("volume_gap_threshold"),
                "volume_metadata_gap_threshold": suwayomi_source_error_cooldown.get("volume_metadata_gap_threshold"),
                "volume_gap_max_sources": suwayomi_source_error_cooldown.get("volume_gap_max_sources"),
                "volume_gap_source_count": suwayomi_source_error_cooldown.get("volume_gap_source_count"),
                "probe_enabled": suwayomi_source_error_cooldown.get("probe_enabled"),
                "probe_after_seconds": suwayomi_source_error_cooldown.get("probe_after_seconds"),
                "probe_max_sources": suwayomi_source_error_cooldown.get("probe_max_sources"),
                "source_error_probe_source_count": suwayomi_source_error_cooldown.get("source_error_probe_source_count"),
                "volume_gap_probe_enabled": suwayomi_source_error_cooldown.get("volume_gap_probe_enabled"),
                "volume_gap_probe_after_seconds": suwayomi_source_error_cooldown.get("volume_gap_probe_after_seconds"),
                "volume_gap_probe_max_sources": suwayomi_source_error_cooldown.get("volume_gap_probe_max_sources"),
                "volume_gap_probe_source_count": suwayomi_source_error_cooldown.get("volume_gap_probe_source_count"),
                "probe_source_count": suwayomi_source_error_cooldown.get("probe_source_count"),
                "probe_rotation_hash": suwayomi_source_error_cooldown.get("probe_rotation_hash"),
                "probe_sources": suwayomi_source_error_cooldown.get("probe_sources"),
                "quarantine_enabled": suwayomi_source_error_cooldown.get("quarantine_enabled"),
                "quarantine_window_seconds": suwayomi_source_error_cooldown.get("quarantine_window_seconds"),
                "quarantine_threshold": suwayomi_source_error_cooldown.get("quarantine_threshold"),
                "quarantine_max_sources": suwayomi_source_error_cooldown.get("quarantine_max_sources"),
                "quarantine_source_count": suwayomi_source_error_cooldown.get("quarantine_source_count"),
                "sources": suwayomi_source_error_cooldown.get("sources"),
            }
        ),
        "suwayomi_payload_summaries": suwayomi_payload_summaries,
        "source_runtime_skips": [
            _clean_dict(
                {
                    "stage": (row or {}).get("stage") if isinstance(row, dict) else "",
                    "query": (row or {}).get("query") if isinstance(row, dict) else "",
                    "source_id": (row or {}).get("source_id") if isinstance(row, dict) else "",
                    "source_display_name": (row or {}).get("source_display_name") if isinstance(row, dict) else "",
                    "reason": (row or {}).get("reason") if isinstance(row, dict) else "",
                    "previous_error": providers.clipped_text((row or {}).get("previous_error"), 300)
                    if isinstance(row, dict)
                    else "",
                }
            )
            for row in source_runtime_skips
            if isinstance(row, dict)
        ],
        "meta_fallbacks": [
            _clean_dict(
                {
                    "stage": (row or {}).get("stage") if isinstance(row, dict) else "",
                    "query": (row or {}).get("query") if isinstance(row, dict) else "",
                    "source_id": (row or {}).get("source_id") if isinstance(row, dict) else "",
                    "manga_id": (row or {}).get("manga_id") if isinstance(row, dict) else "",
                    "chapter_id": (row or {}).get("chapter_id") if isinstance(row, dict) else "",
                }
            )
            for row in meta_fallbacks
            if isinstance(row, dict)
        ],
        "partial_errors": [
            _clean_dict(
                {
                    "query": (row or {}).get("query") if isinstance(row, dict) else "",
                    "stage": (row or {}).get("stage") if isinstance(row, dict) else "",
                    "request_id": (row or {}).get("request_id") if isinstance(row, dict) else "",
                    "purpose": (row or {}).get("purpose") if isinstance(row, dict) else "",
                    "url_hash": (row or {}).get("url_hash") if isinstance(row, dict) else "",
                    "source_id": (row or {}).get("source_id") if isinstance(row, dict) else "",
                    "source_display_name": (row or {}).get("source_display_name") if isinstance(row, dict) else "",
                    "manga_id": (row or {}).get("manga_id") if isinstance(row, dict) else "",
                    "chapter_id": (row or {}).get("chapter_id") if isinstance(row, dict) else "",
                    "error": providers.clipped_text((row or {}).get("error"), 300)
                    if isinstance(row, dict)
                    else "",
                }
            )
            for row in partial_errors
            if isinstance(row, dict)
        ],
        "payload_result_count": _payload_result_count(payload),
        "pack_detail_fetch_count": pack_detail_fetch_count,
        "feed_evidence": _clean_dict(
            {
                "feed_item_count": feed_evidence.get("feed_item_count"),
                "matching_feed_item_count": feed_evidence.get("matching_feed_item_count"),
                "feed_item_samples": feed_evidence.get("feed_item_samples"),
                "matching_feed_item_samples": feed_evidence.get("matching_feed_item_samples"),
            }
        ),
        "requests": [_request_evidence(request) for request in request_source],
    }
    if fetch_result.get("error"):
        out["error"] = providers.clipped_text(fetch_result.get("error"), 300)
    return _clean_dict(out)


def _with_fetch_evidence(attempts, job, fetch_result, *, reason=""):
    evidence = _fetch_evidence(job, fetch_result, reason=reason)
    if not evidence:
        return list(attempts or [])
    out = []
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        status = str(attempt.get("status") or "").strip().lower()
        if status not in FETCH_EVIDENCE_ATTEMPT_STATUSES and not evidence.get("partial_errors"):
            out.append(attempt)
            continue
        enriched = dict(attempt)
        raw = dict(enriched.get("raw") or {}) if isinstance(enriched.get("raw"), dict) else {}
        raw.setdefault("fetch", evidence)
        enriched["raw"] = raw
        out.append(enriched)
    return out


def _runtime_results_with_attempts(runtime_results, attempts):
    rows = []
    attempt_index = 0
    attempts = [attempt for attempt in attempts or [] if isinstance(attempt, dict)]
    for row in runtime_results or []:
        if not isinstance(row, dict):
            rows.append(row)
            continue
        out = dict(row)
        row_attempts = []
        for original in row.get("attempts") or []:
            if isinstance(original, dict) and attempt_index < len(attempts):
                row_attempts.append(attempts[attempt_index])
                attempt_index += 1
            elif isinstance(original, dict):
                row_attempts.append(original)
        out["attempts"] = row_attempts
        rows.append(out)
    return rows


def _fetch_payload_list(fetch_result, key):
    fetch_result = _dict(fetch_result)
    values = list(fetch_result.get(key) or [])
    if values:
        return values
    payload = _first_payload_dict(fetch_result)
    return list(payload.get(key) or []) if isinstance(payload.get(key), list) else []


def _partial_indexer_provider_wait_reason(job, fetch_result, evaluations):
    job = _dict(job)
    fetch_plan = _dict(job.get("fetch_plan"))
    payload_mode = str(fetch_plan.get("payload_mode") or "").strip()
    if payload_mode not in {"prowlarr_multi_search", "indexer_multi_search"}:
        return ""
    partial_errors = _fetch_payload_list(fetch_result, "partial_errors")
    if not partial_errors:
        return ""
    candidate_count = sum(int((row or {}).get("candidate_count") or 0) for row in evaluations or [])
    if candidate_count:
        return ""
    variant_counts = _fetch_payload_list(fetch_result, "variant_result_counts")
    planned_queries = list(fetch_plan.get("query_variants") or [])
    if not planned_queries:
        planned_queries = [
            ((request or {}).get("params") or {}).get("query") or ((request or {}).get("params") or {}).get("q")
            for request in fetch_plan.get("requests") or []
            if isinstance(request, dict)
        ]
    successful_count = len(variant_counts)
    failed_count = len(partial_errors)
    planned_count = len([query for query in planned_queries if str(query or "").strip()])
    if successful_count == 0 or failed_count > successful_count or (planned_count and successful_count < (planned_count / 2)):
        return "partial_indexer_search_failed"
    return ""


def _fetch_requests_made(fetch_result):
    fetch_result = _dict(fetch_result)
    return [request for request in fetch_result.get("requests_made") or [] if isinstance(request, dict)]


def _suwayomi_payload_manga_keys(fetch_result):
    keys = set()
    for payload in _dict(fetch_result).get("payloads") or []:
        if not isinstance(payload, dict):
            continue
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        manga = payload.get("manga") if isinstance(payload.get("manga"), dict) else {}
        source_id = str(source.get("id") or manga.get("sourceId") or manga.get("source_id") or "").strip()
        manga_id = str(manga.get("id") or "").strip()
        if source_id and manga_id:
            keys.add(f"{source_id}:{manga_id}")
    return keys


def _suwayomi_error_manga_keys(errors):
    keys = set()
    for error in errors or []:
        if not isinstance(error, dict):
            continue
        source_id = str(error.get("source_id") or "").strip()
        manga_id = str(error.get("manga_id") or "").strip()
        if source_id and manga_id:
            keys.add(f"{source_id}:{manga_id}")
    return keys


def _suwayomi_source_pool_wait_reason(fetch_result):
    fetch_result = _dict(fetch_result)
    selection = _dict(fetch_result.get("suwayomi_source_selection"))
    if not selection:
        payload = _first_payload_dict(fetch_result)
        selection = _dict(payload.get("suwayomi_source_selection"))
    if not selection:
        return ""
    selected_count = int(selection.get("selected_count") or 0)
    skipped_sources = [row for row in selection.get("skipped_sources") or [] if isinstance(row, dict)]
    if selected_count > 0 or not skipped_sources:
        return ""
    skipped_reasons = {str(row.get("reason") or "").strip().lower() for row in skipped_sources}
    if not skipped_reasons.intersection({"source_error_cooldown", "volume_evidence_gap_cooldown"}):
        return ""
    cooldown = _dict(fetch_result.get("suwayomi_source_error_cooldown"))
    if not cooldown:
        payload = _first_payload_dict(fetch_result)
        cooldown = _dict(payload.get("suwayomi_source_error_cooldown"))
    if (
        int(cooldown.get("source_count") or 0)
        or int(cooldown.get("quarantine_source_count") or 0)
        or int(cooldown.get("volume_gap_source_count") or 0)
    ):
        return "suwayomi_source_pool_cooldown"
    return ""


def _partial_suwayomi_provider_wait_reason(job, fetch_result, evaluations):
    job = _dict(job)
    fetch_plan = _dict(job.get("fetch_plan"))
    payload_mode = str(fetch_plan.get("payload_mode") or "").strip()
    if payload_mode != "suwayomi_search_then_chapters":
        return ""
    source_pool_wait_reason = _suwayomi_source_pool_wait_reason(fetch_result)
    if source_pool_wait_reason:
        return source_pool_wait_reason
    partial_errors = _fetch_payload_list(fetch_result, "partial_errors")
    if not partial_errors:
        return ""
    candidate_count = sum(int((row or {}).get("candidate_count") or 0) for row in evaluations or [])
    if candidate_count:
        return ""
    requests_made = _fetch_requests_made(fetch_result)
    search_requests = [
        request
        for request in requests_made
        if str(request.get("request_id") or "").startswith("suwayomi_source_search")
        or str(request.get("purpose") or "") == "search_suwayomi_source"
    ]
    variant_counts = _fetch_payload_list(fetch_result, "variant_result_counts")
    source_search_errors = [
        error for error in partial_errors if str((error or {}).get("stage") or "") == "source_search"
    ]
    if source_search_errors and search_requests:
        successful_count = len(variant_counts)
        failed_count = len(source_search_errors)
        planned_count = len(search_requests)
        if successful_count == 0 or failed_count > successful_count or successful_count < (planned_count / 2):
            return "partial_suwayomi_source_search_failed"
    matching_manga_count = sum(int((row or {}).get("matching_manga") or 0) for row in variant_counts)
    manga_errors = [
        error
        for error in partial_errors
        if str((error or {}).get("stage") or "") in {"manga_chapters", "manga_chapters_no_meta_fallback"}
    ]
    failed_manga_keys = _suwayomi_error_manga_keys(manga_errors)
    successful_manga_keys = _suwayomi_payload_manga_keys(fetch_result)
    if matching_manga_count and manga_errors and not successful_manga_keys:
        return "suwayomi_chapter_lookup_failed"
    page_errors = [
        error
        for error in partial_errors
        if str((error or {}).get("stage") or "") in {"chapter_pages", "chapter_pages_no_meta_fallback"}
    ]
    if page_errors:
        return "suwayomi_page_lookup_failed"
    return ""


def _partial_mangadex_deadline_wait_reason(job, fetch_result, evaluations):
    """A MangaDex fetch that stopped at the slot deadline is a retry, not a miss."""

    job = _dict(job)
    fetch_plan = _dict(job.get("fetch_plan"))
    if str(fetch_plan.get("payload_mode") or "").strip() != "mangadex_search_then_feed":
        return ""
    candidate_count = sum(int((row or {}).get("candidate_count") or 0) for row in evaluations or [])
    if candidate_count:
        return ""
    partial_errors = _fetch_payload_list(fetch_result, "partial_errors")
    if any(str((error or {}).get("error") or "") == "fetch_deadline_reached" for error in partial_errors):
        return "mangadex_fetch_deadline_reached"
    for payload in _dict(fetch_result).get("payloads") or []:
        if not isinstance(payload, dict):
            continue
        if payload.get("at_home_deadline_reached") or payload.get("feed_deadline_reached"):
            return "mangadex_fetch_deadline_reached"
        if str(payload.get("volume_pack_blocked_reason") or "").endswith("deadline_reached"):
            return "mangadex_fetch_deadline_reached"
    return ""


def _partial_provider_wait_reason(job, fetch_result, evaluations):
    return (
        _partial_indexer_provider_wait_reason(job, fetch_result, evaluations)
        or _partial_suwayomi_provider_wait_reason(job, fetch_result, evaluations)
        or _partial_mangadex_deadline_wait_reason(job, fetch_result, evaluations)
    )


def _fetch_failure_attempt_status(reason):
    reason = str(reason or "").strip().lower()
    if reason in PROVIDER_UNAVAILABLE_FETCH_REASONS:
        return "provider_unavailable"
    if reason in PROVIDER_WAIT_FETCH_REASONS:
        return "provider_wait"
    return "provider_wait"


def _source_worker_attempt_for_job(job, row, plan, *, status, reason, raw=None):
    job = _dict(job)
    row = _dict(row)
    plan = _dict(plan)
    fetch_plan = _dict(job.get("fetch_plan"))
    request = _first_request(fetch_plan)
    request_url = str(request.get("url") or "").strip()
    safe_request_url = _safe_request_url(request)
    payload_mode = str(fetch_plan.get("payload_mode") or "").strip()
    try:
        request_count = int(fetch_plan.get("estimated_request_count") or len(fetch_plan.get("requests") or []))
    except Exception:
        request_count = len(fetch_plan.get("requests") or [])
    raw_payload = raw if isinstance(raw, dict) else {}
    raw_payload.setdefault(
        "source_worker",
        {
            "job_status": job.get("job_status"),
            "adapter_family": job.get("adapter_family"),
            "adapter_id": job.get("adapter_id"),
            "payload_mode": payload_mode,
            "request_count": max(0, request_count),
            "first_request_id": request.get("request_id"),
            "first_request_purpose": request.get("purpose"),
            "first_request_url_hash": providers.url_hash(request_url),
            "command_contract": (fetch_plan.get("command_plan") or {}).get("output_contract")
            if isinstance(fetch_plan.get("command_plan"), dict)
            else "",
        },
    )
    attempt = runtime.source_search_attempt(
        row,
        plan,
        query=job.get("query") or "",
        status=status,
        reason=reason,
        counts={"candidate_count": 0, "safe_candidate_count": 0, "rejected_candidate_count": 0},
        raw=raw_payload,
    )
    attempt["retry_scope"] = "source_worker_provider_fetch" if status in {"provider_wait", "provider_unavailable"} else "source_worker_search"
    attempt["provider_wait_reason"] = reason if status in {"provider_wait", "provider_unavailable"} else ""
    attempt["payload_mode"] = payload_mode
    if request_url:
        attempt["download_url_hash"] = providers.url_hash(request_url)
    if safe_request_url:
        attempt["source_path"] = safe_request_url
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def _by_provider_id(rows):
    out = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("provider_id"):
            out[row["provider_id"]] = row
    return out


def _job_status(plan, fetch_plan):
    schedule_state = str((plan or {}).get("schedule_state") or "").strip()
    payload_mode = str((fetch_plan or {}).get("payload_mode") or "").strip()
    if schedule_state == "provider_wait":
        return "provider_wait", (plan or {}).get("schedule_reason") or "provider_health_problem"
    if schedule_state == "blocked":
        return "blocked", (plan or {}).get("schedule_reason") or "source_not_schedulable"
    if payload_mode == "configuration_required":
        return "configuration_required", (fetch_plan or {}).get("reason") or "source_configuration_required"
    if payload_mode == "unsupported_adapter":
        return "unsupported_adapter", (fetch_plan or {}).get("reason") or "adapter_fetch_plan_unimplemented"
    if payload_mode in OPERATOR_PAYLOAD_MODES:
        return "operator_required", "operator_payload_required"
    if payload_mode == "external_tool_command" and (fetch_plan or {}).get("can_execute_with_tool_runner"):
        return "ready", ""
    if (fetch_plan or {}).get("can_execute_with_http_client"):
        return "ready", ""
    if payload_mode in {"none", ""}:
        return "not_executable", "no_fetch_plan"
    return "not_executable", (fetch_plan or {}).get("reason") or "no_request_available"


def source_job_for_row(row, plan=None, wanted_item=None, *, limit=20):
    row = _dict(row)
    plan = _dict(plan) or worker_plan.source_worker_plan_for_row(row)
    wanted_item = _dict(wanted_item)
    fetch_plan = adapters.adapter_fetch_plan(row, plan, wanted_item, limit=limit)
    status, reason = _job_status(plan, fetch_plan)
    scope_reason = _source_scope_block_reason(row, wanted_item)
    if scope_reason:
        status = "blocked"
        reason = scope_reason
    job = {
        "source_job_contract_version": CONTRACT_VERSION,
        "provider_id": row.get("provider_id"),
        "display_name": row.get("display_name"),
        "provider_type": row.get("provider_type"),
        "source_kind": row.get("source_kind"),
        "source_mode": row.get("source_mode"),
        "registry_state": row.get("registry_state"),
        "schedule_state": plan.get("schedule_state"),
        "provider_health_problem": bool(plan.get("provider_health_problem")),
        "provider_health": plan.get("provider_health") if isinstance(plan.get("provider_health"), dict) else {},
        "health_provider_ids": list(plan.get("health_provider_ids") or []),
        "adapter_family": plan.get("adapter_family"),
        "adapter_id": plan.get("adapter_id"),
        "handoff_kind": plan.get("handoff_kind"),
        "query": fetch_plan.get("query"),
        "priority": row.get("priority", plan.get("priority", 100)),
        "limit": int(limit or 20),
        "job_status": status,
        "requires_operator": bool(fetch_plan.get("requires_operator") or plan.get("requires_operator")),
        "can_execute_with_http_client": bool(fetch_plan.get("can_execute_with_http_client")),
        "can_execute_with_tool_runner": bool(fetch_plan.get("can_execute_with_tool_runner")),
        "emits_download_task": bool(plan.get("emits_download_task")),
        "can_auto_download": bool(plan.get("can_auto_download")),
        "evidence_only": bool(plan.get("evidence_only")),
        "manual_review_only": bool(plan.get("manual_review_only")),
        "wanted_item": wanted_item,
        "registry_row": row,
        "worker_plan": plan,
        "fetch_plan": fetch_plan,
        "mutates_database": False,
        "mutates_filesystem": bool(fetch_plan.get("can_execute_with_tool_runner")),
        "network_default": bool(fetch_plan.get("can_execute_with_http_client") or fetch_plan.get("can_execute_with_tool_runner")),
    }
    if reason:
        job["reason"] = reason
    if scope_reason:
        job["source_scope"] = {
            "eligible": False,
            "reason": scope_reason,
        }
    return {key: value for key, value in job.items() if value not in (None, "", [], {})}


def source_jobs_from_registry(rows, wanted_item=None, *, include_operator=True, include_blocked=False, limit=20):
    rows = list(rows or [])
    rows_by_id = _by_provider_id(rows)
    plans = worker_plan.source_worker_plan_from_registry(rows, include_blocked=True)
    jobs = []
    for plan in plans:
        row = rows_by_id.get(plan.get("provider_id"))
        if not row:
            continue
        job = source_job_for_row(row, plan, wanted_item, limit=limit)
        if not include_blocked and job.get("job_status") in BLOCKED_JOB_STATUSES:
            continue
        if not include_operator and job.get("job_status") == "operator_required":
            continue
        jobs.append(job)
    return sorted(
        jobs,
        key=lambda job: (
            int(job.get("priority") or 100),
            str(job.get("display_name") or "").lower(),
            str(job.get("provider_id") or ""),
        ),
    )


def source_jobs_from_settings_snapshot(
    snapshot,
    wanted_item=None,
    *,
    include_operator=True,
    include_blocked=False,
    limit=20,
    provider_health_map=None,
    registry_rows=None,
):
    # snapshot + provider_health_map are the same for every queue row in a
    # scheduling pass, so a caller planning many rows can build the registry
    # once and pass it as registry_rows instead of paying to rebuild it --
    # deep copies and all -- per row.
    rows = (
        registry_rows
        if registry_rows is not None
        else registry.registry_from_settings_snapshot(
            snapshot,
            include_disabled=True,
            provider_health_map=provider_health_map,
        )
    )
    return source_jobs_from_registry(
        rows,
        wanted_item,
        include_operator=include_operator,
        include_blocked=include_blocked,
        limit=limit,
    )


def source_job_summary(jobs):
    jobs = list(jobs or [])
    by_status = {}
    by_schedule = {}
    by_adapter = {}
    for job in jobs:
        status = str((job or {}).get("job_status") or "unknown")
        schedule = str((job or {}).get("schedule_state") or "unknown")
        adapter = str((job or {}).get("adapter_family") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        by_schedule[schedule] = by_schedule.get(schedule, 0) + 1
        by_adapter[adapter] = by_adapter.get(adapter, 0) + 1
    return {
        "total": len(jobs),
        "by_status": dict(sorted(by_status.items())),
        "by_schedule_state": dict(sorted(by_schedule.items())),
        "by_adapter": dict(sorted(by_adapter.items())),
        "ready_http": sum(1 for job in jobs if job.get("job_status") == "ready"),
        "ready_tool": sum(1 for job in jobs if job.get("job_status") == "ready" and job.get("can_execute_with_tool_runner")),
        "operator_required": sum(1 for job in jobs if job.get("job_status") == "operator_required"),
        "emits_download_task": sum(1 for job in jobs if job.get("emits_download_task")),
    }


def _aggregate_runtime_status(results):
    statuses = {str((row or {}).get("status") or "") for row in results or []}
    if "sent" in statuses:
        return "sent"
    if "review" in statuses:
        return "review"
    if "provider_unavailable" in statuses or "provider_wait" in statuses:
        return "provider_wait"
    if "blocked" in statuses:
        return "blocked"
    if "searched_no_candidates" in statuses:
        return "searched_no_candidates"
    if "observed" in statuses:
        return "observed"
    return "unknown"


def run_source_job(
    job,
    *,
    http_get=None,
    tool_runner=None,
    operator_payload=None,
    candidate_headers=None,
    source_memory_db_path=None,
    source_memory_cooldown_seconds=None,
    staging_root=None,
    fetch_deadline=None,
    now=None,
):
    job = _dict(job)
    row = _dict(job.get("registry_row"))
    plan = _dict(job.get("worker_plan"))
    fetch_plan = _dict(job.get("fetch_plan"))
    wanted_item = _dict(job.get("wanted_item"))
    now = time.time() if now is None else now
    result = {
        "source_job_result_contract_version": CONTRACT_VERSION,
        "provider_id": job.get("provider_id"),
        "adapter_family": job.get("adapter_family"),
        "schedule_state": job.get("schedule_state"),
        "job_status": job.get("job_status"),
        "result_status": "pending",
        "reason": "",
        "ran_at": now,
        "fetch": {},
        "runtime_results": [],
        "attempts": [],
        "mutations_performed": [],
    }

    if job.get("job_status") in NON_EXECUTABLE_JOB_STATUSES:
        result["result_status"] = job.get("job_status")
        result["reason"] = job.get("reason") or "source_job_not_executable"
        if job.get("job_status") == "provider_wait":
            result["attempts"] = [
                _source_worker_attempt_for_job(
                    job,
                    row,
                    plan,
                    status="provider_wait",
                    reason=result["reason"],
                    raw={"provider_health": job.get("provider_health")},
                )
            ]
        return result

    suwayomi_source_error_cooldown = {}
    if fetch_plan.get("payload_mode") == "suwayomi_search_then_chapters":
        row, suwayomi_source_error_cooldown = _row_with_suwayomi_persisted_source_error_cooldown(
            row,
            source_memory_db_path,
            now=now,
            rotation_key=_suwayomi_probe_rotation_key(job, row, plan, wanted_item),
            wanted_item=wanted_item,
        )

    mangadex_query_rotation = {}
    if fetch_plan.get("payload_mode") == "mangadex_search_then_feed" and fetch_plan.get("query_variants"):
        mangadex_query_rotation = _mangadex_query_rotation(
            source_memory_db_path,
            wanted_item,
            fetch_plan.get("mangadex_query_pool") or fetch_plan.get("query_variants"),
        )
        if mangadex_query_rotation.get("offset"):
            wanted_item = dict(wanted_item)
            wanted_item["_mangadex_query_offset"] = mangadex_query_rotation["offset"]
            fetch_plan = adapters.adapter_fetch_plan(
                row,
                plan,
                wanted_item,
                limit=job.get("limit") or 20,
            )
            fetch_plan["mangadex_query_rotation"] = dict(mangadex_query_rotation)
            job["wanted_item"] = wanted_item
            job["fetch_plan"] = fetch_plan

    if fetch_plan.get("payload_mode") in OPERATOR_PAYLOAD_MODES:
        if operator_payload is None:
            result["result_status"] = "operator_required"
            result["reason"] = "operator_payload_required"
            return result
        fetch_result = {
            "fetch_contract_version": CONTRACT_VERSION,
            "ok": True,
            "provider_id": job.get("provider_id"),
            "fetch_plan": fetch_plan,
            "payloads": [operator_payload],
            "response_headers": {},
            "requests_made": [],
            "reason": "operator_payload_supplied",
        }
    else:
        fetch_result = adapters.fetch_payloads(
            row,
            plan,
            wanted_item,
            http_get=http_get,
            tool_runner=tool_runner,
            limit=job.get("limit") or 20,
            deadline=fetch_deadline,
        )

    if suwayomi_source_error_cooldown:
        fetch_result = dict(fetch_result)
        fetch_result["suwayomi_source_error_cooldown"] = suwayomi_source_error_cooldown
    if mangadex_query_rotation:
        fetch_result = dict(fetch_result)
        fetch_result["mangadex_query_rotation"] = mangadex_query_rotation
    result["fetch"] = fetch_result
    if not fetch_result.get("ok"):
        reason = fetch_result.get("reason") or "fetch_failed"
        attempt_status = _fetch_failure_attempt_status(reason)
        result["result_status"] = "provider_wait"
        result["reason"] = reason
        result["attempts"] = [
            _source_worker_attempt_for_job(
                job,
                row,
                plan,
                status=attempt_status,
                reason=reason,
                raw={"fetch": _fetch_evidence(job, fetch_result, reason=reason)},
            )
        ]
        return result

    payloads = list(fetch_result.get("payloads") or [])
    if not payloads:
        partial_wait_reason = _partial_provider_wait_reason(job, fetch_result, [])
        result["result_status"] = "provider_wait" if partial_wait_reason else "searched_no_candidates"
        result["reason"] = partial_wait_reason or "fetch_returned_no_payloads"
        result["attempts"] = [
            _source_worker_attempt_for_job(
                job,
                row,
                plan,
                status="provider_wait" if partial_wait_reason else "searched_no_candidates",
                reason=partial_wait_reason or "fetch_returned_no_payloads",
                raw={"fetch": _fetch_evidence(job, fetch_result, reason=partial_wait_reason or "fetch_returned_no_payloads")},
            )
        ]
        return result

    headers = candidate_headers if isinstance(candidate_headers, dict) else {}
    evaluations = []
    attempts = []
    for payload in payloads:
        evaluated = runtime.evaluate_source_payload(
            row,
            payload,
            wanted_item=wanted_item,
            plan=plan,
            headers=headers,
            limit=job.get("limit") or 20,
            staging_root=staging_root,
            query=job.get("query") or "",
            now=now,
        )
        if source_memory_db_path:
            import inkdrop_source_suppression as suppression

            evaluated = suppression.apply_source_memory_to_runtime_result(
                source_memory_db_path,
                evaluated,
                row,
                wanted_item,
                now=now,
                cooldown_seconds=source_memory_cooldown_seconds,
            )
        evaluations.append(evaluated)

    partial_wait_reason = _partial_provider_wait_reason(job, fetch_result, evaluations)
    if partial_wait_reason:
        result["runtime_results"] = []
        result["result_status"] = "provider_wait"
        result["reason"] = partial_wait_reason
        result["attempts"] = [
            _source_worker_attempt_for_job(
                job,
                row,
                plan,
                status="provider_wait",
                reason=partial_wait_reason,
                raw={"fetch": _fetch_evidence(job, fetch_result, reason=partial_wait_reason)},
            )
        ]
        result["candidate_count"] = sum(int(row.get("candidate_count") or 0) for row in evaluations)
        result["safe_candidate_count"] = sum(int(row.get("safe_candidate_count") or 0) for row in evaluations)
        result["review_candidate_count"] = sum(int(row.get("review_candidate_count") or 0) for row in evaluations)
        result["blocked_candidate_count"] = sum(int(row.get("blocked_candidate_count") or 0) for row in evaluations)
        return result

    evaluations, auto_send_selection = runtime.select_auto_send_attempts(
        evaluations,
        row,
        wanted_item,
        scope="source_job",
    )
    for evaluated in evaluations:
        attempts.extend(evaluated.get("attempts") or [])

    result["runtime_results"] = evaluations
    if auto_send_selection.get("applied"):
        result["auto_send_selection"] = auto_send_selection
    result["result_status"] = _aggregate_runtime_status(evaluations)
    result["attempts"] = _with_fetch_evidence(attempts, job, fetch_result, reason=result["result_status"])
    result["runtime_results"] = _runtime_results_with_attempts(evaluations, result["attempts"])
    result["candidate_count"] = sum(int(row.get("candidate_count") or 0) for row in evaluations)
    result["safe_candidate_count"] = sum(int(row.get("safe_candidate_count") or 0) for row in evaluations)
    result["review_candidate_count"] = sum(int(row.get("review_candidate_count") or 0) for row in evaluations)
    result["blocked_candidate_count"] = sum(int(row.get("blocked_candidate_count") or 0) for row in evaluations)
    return result


def run_source_jobs(
    jobs,
    *,
    http_get=None,
    tool_runner=None,
    operator_payloads=None,
    candidate_headers_by_provider=None,
    source_memory_db_path=None,
    source_memory_cooldown_seconds=None,
    staging_root=None,
    fetch_deadline=None,
    now=None,
):
    operator_payloads = operator_payloads if isinstance(operator_payloads, dict) else {}
    headers_by_provider = candidate_headers_by_provider if isinstance(candidate_headers_by_provider, dict) else {}
    results = []
    for job in jobs or []:
        provider_id = (job or {}).get("provider_id")
        results.append(
            run_source_job(
                job,
                http_get=http_get,
                tool_runner=tool_runner,
                operator_payload=operator_payloads.get(provider_id),
                candidate_headers=headers_by_provider.get(provider_id),
                source_memory_db_path=source_memory_db_path,
                source_memory_cooldown_seconds=source_memory_cooldown_seconds,
                staging_root=staging_root,
                fetch_deadline=fetch_deadline,
                now=now,
            )
        )
    return results


def source_job_result_summary(results):
    results = _list(results)
    by_status = {}
    for row in results:
        status = str((row or {}).get("result_status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "total": len(results),
        "by_status": dict(sorted(by_status.items())),
        "attempts": sum(len((row or {}).get("attempts") or []) for row in results),
        "candidate_count": sum(int((row or {}).get("candidate_count") or 0) for row in results),
        "safe_candidate_count": sum(int((row or {}).get("safe_candidate_count") or 0) for row in results),
        "review_candidate_count": sum(int((row or {}).get("review_candidate_count") or 0) for row in results),
        "blocked_candidate_count": sum(int((row or {}).get("blocked_candidate_count") or 0) for row in results),
    }


def recordable_attempts(results, *, statuses=None):
    statuses = {str(status).strip().lower() for status in statuses or [] if str(status or "").strip()}
    attempts = []
    for result in _list(results):
        for attempt in (result or {}).get("attempts") or []:
            if statuses and str((attempt or {}).get("status") or "").strip().lower() not in statuses:
                continue
            attempts.append(attempt)
    return attempts
