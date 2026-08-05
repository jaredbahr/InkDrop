"""Dry-run-first coordinator for settings-backed InkDrop source workers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import inkdrop_candidate_matching
import inkdrop_direct_downloader as direct_downloader
import inkdrop_manual_search
import inkdrop_missing_recovery_policy
import inkdrop_page_pack_downloader as page_pack_downloader
import inkdrop_source_providers
import inkdrop_source_worker_jobs as source_jobs
import inkdrop_source_worker_recorder as recorder
import inkdrop_source_registry
import inkdrop_sources
import inkdrop_state
import inkdrop_download_client_routing


CONTRACT_VERSION = 1
SINGLETON_METADATA_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
STAGEABLE_DOWNLOAD_CLIENTS = {"inkdrop_direct", "inkdrop_page_pack"}
HANDOFF_DOWNLOAD_CLIENTS = {"qbittorrent", "qbit", "qb", "sabnzbd", "sab", "transmission", "deluge", "nzbget", "utorrent", "rtorrent"}
TORRENT_HANDOFF_DOWNLOAD_CLIENTS = {"qbittorrent", "transmission", "deluge", "utorrent", "rtorrent"}
FAILED_HANDOFF_RETRY_MAX_AGE_SECONDS = 24 * 60 * 60
PERSISTED_EXACT_PACK_REPLAY_MAX_AGE_SECONDS = 24 * 60 * 60
LEGACY_SAB_URL_FETCH_RETRY_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
FAILED_HANDOFF_RETRY_STATUSES = {
    "client_unavailable",
    "download_api_error",
    "download_preflight_api_error",
    "error",
    "failed",
    "failed_download",
    "provider_unavailable",
    "provider_wait",
}
VOLUME_TITLE_RE = re.compile(r"(?i)^\s*(?:vol(?:ume)?|v)\.?\s*(\d+(?:\.\d+)?)\s*$")
VOLUME_QUERY_RE = re.compile(r"(?i)(?:^|\b)(?:vol(?:ume)?|v)\.?\s*(\d+(?:\.\d+)?)(?:\b|$)")
CHAPTER_QUERY_RE = re.compile(r"(?i)(?:^|\b)(?:chapter|chap|ch|c)\.?\s*\d")
COLLECTED_SINGLETON_PATTERNS = (
    ("complete_collection", re.compile(r"(?i)\bcomplete\s+(?:series|collection|edition)\b")),
    ("omnibus", re.compile(r"(?i)\bomnibus\b")),
    ("collected_edition", re.compile(r"(?i)\bcollected\s+edition\b")),
    ("essential_edition", re.compile(r"(?i)\bessential\s+edition\b")),
    ("deluxe_edition", re.compile(r"(?i)\bdeluxe\s+edition\b")),
    ("library_edition", re.compile(r"(?i)\blibrary\s+edition\b")),
    ("trade_paperback", re.compile(r"(?i)\b(?:trade\s+paperback|tpb)\b")),
    ("hardcover", re.compile(r"(?i)\b(?:hardcover|hc)\b")),
    ("volume", re.compile(r"(?i)^\s*(?:vol(?:ume)?|v)\.?\s*\d*\s*$")),
)


def _dict(value):
    return dict(value) if isinstance(value, dict) else {}


def _list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


@contextlib.contextmanager
def _borrowed_or_read_con(db_path, con=None):
    """Reuse an already-open read connection, or open one for this call."""

    if con is not None:
        yield con
    else:
        with inkdrop_state.connect_read(db_path) as opened:
            yield opened


def _json_loads(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _clean_number(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        numeric = float(text)
    except Exception:
        return text
    if numeric.is_integer():
        return str(int(numeric))
    return text


def _metadata_declares_multiple_issues(value):
    cleaned = _clean_number(value)
    return bool(_positive_numeric_id(cleaned) and int(cleaned) > 1)


def _positive_numeric_id(value):
    text = str(value or "").strip()
    return bool(text.isdigit() and int(text) > 0)


def _row_dict(value):
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "keys"):
        return {key: value[key] for key in value.keys()}
    return {}


def _explicit_comicvine_replay_aliases(issue_rows):
    """Return unambiguous Kapowarr replay issue -> ComicVine issue aliases.

    Queue snapshots can replay a Kapowarr issue ID through ``issues`` while
    labelling it ComicVine. The authoritative ComicVine row retains the exact
    bridge in its raw payload, so use only that reciprocal relationship. Sparse
    rows, duplicate mappings, and look-alike issue numbers deliberately remain
    separate and therefore fail the singleton proof closed.
    """

    rows = [_row_dict(row) for row in issue_rows]
    rows_by_metadata_id = {}
    for row in rows:
        metadata_id = str(row.get("metadata_id") or "").strip()
        if (
            str(row.get("metadata_provider") or "").strip().lower() == "comicvine"
            and _positive_numeric_id(metadata_id)
        ):
            rows_by_metadata_id.setdefault(metadata_id, []).append(row)

    candidate_pairs = []
    for authoritative in rows:
        authoritative_metadata_id = str(authoritative.get("metadata_id") or "").strip()
        if (
            str(authoritative.get("metadata_provider") or "").strip().lower() != "comicvine"
            or not _positive_numeric_id(authoritative_metadata_id)
        ):
            continue
        authoritative_raw_id = str(authoritative.get("raw_issue_id") or "").strip()
        replay_metadata_id = str(authoritative.get("raw_kapowarr_issue_id") or "").strip()
        if (
            authoritative_raw_id != authoritative_metadata_id
            or not _positive_numeric_id(replay_metadata_id)
            or replay_metadata_id == authoritative_metadata_id
        ):
            continue
        replay_rows = rows_by_metadata_id.get(replay_metadata_id) or []
        if len(replay_rows) != 1:
            continue
        replay = replay_rows[0]
        replay_raw_id = str(replay.get("raw_issue_id") or "").strip()
        replay_raw_kapowarr_id = str(replay.get("raw_kapowarr_issue_id") or "").strip()
        if replay_raw_id != replay_metadata_id or replay_raw_kapowarr_id != replay_metadata_id:
            continue
        authoritative_issue_id = str(authoritative.get("id") or "").strip()
        replay_issue_id = str(replay.get("id") or "").strip()
        if not authoritative_issue_id or not replay_issue_id or authoritative_issue_id == replay_issue_id:
            continue
        candidate_pairs.append((authoritative_issue_id, replay_issue_id))

    authoritative_counts = {}
    replay_counts = {}
    for authoritative_issue_id, replay_issue_id in candidate_pairs:
        authoritative_counts[authoritative_issue_id] = authoritative_counts.get(authoritative_issue_id, 0) + 1
        replay_counts[replay_issue_id] = replay_counts.get(replay_issue_id, 0) + 1
    return {
        replay_issue_id: authoritative_issue_id
        for authoritative_issue_id, replay_issue_id in candidate_pairs
        if authoritative_counts.get(authoritative_issue_id) == 1
        and replay_counts.get(replay_issue_id) == 1
    }


def _project_authoritative_comicvine_issue_rows(issue_rows, wanted_rows):
    alias_to_authoritative = _explicit_comicvine_replay_aliases(issue_rows)
    if not alias_to_authoritative:
        return list(issue_rows), list(wanted_rows), 0

    row_by_issue_id = {str(row["id"] or "").strip(): row for row in issue_rows}
    projected_issues = [
        row for row in issue_rows if str(row["id"] or "").strip() not in alias_to_authoritative
    ]

    grouped_wanted = {}
    wanted_order = []
    for row in wanted_rows:
        projected = _row_dict(row)
        original_issue_id = str(projected.get("issue_id") or "").strip()
        authoritative_issue_id = alias_to_authoritative.get(original_issue_id, original_issue_id)
        authoritative = row_by_issue_id.get(authoritative_issue_id)
        if authoritative is not None and original_issue_id in alias_to_authoritative:
            for field in ("title", "issue_number", "normalized_number", "metadata_provider", "metadata_id"):
                projected[field] = authoritative[field]
            projected["issue_id"] = authoritative_issue_id
        projected["_original_issue_id"] = original_issue_id
        grouped_wanted.setdefault(authoritative_issue_id, []).append(projected)
        if authoritative_issue_id not in wanted_order:
            wanted_order.append(authoritative_issue_id)

    projected_wanted = []
    for authoritative_issue_id in wanted_order:
        group = grouped_wanted[authoritative_issue_id]
        replay_issue_ids = {
            replay_issue_id
            for replay_issue_id, target_issue_id in alias_to_authoritative.items()
            if target_issue_id == authoritative_issue_id
        }
        original_issue_ids = {row["_original_issue_id"] for row in group}
        # Collapse only one authoritative claim paired with its one explicit
        # replay claim. Repeated claims against either row remain visible so
        # the existing duplicate-Wanted safety gate still fails closed.
        if (
            len(group) == 2
            and len(replay_issue_ids) == 1
            and original_issue_ids == {authoritative_issue_id, next(iter(replay_issue_ids))}
        ):
            group = [next(row for row in group if row["_original_issue_id"] == authoritative_issue_id)]
        for row in group:
            row.pop("_original_issue_id", None)
            projected_wanted.append(row)
    return projected_issues, projected_wanted, len(alias_to_authoritative)


def _collected_singleton_markers(*values):
    markers = []
    text = " ".join(str(value or "") for value in values if str(value or "").strip())
    for marker, pattern in COLLECTED_SINGLETON_PATTERNS:
        if pattern.search(text):
            markers.append(marker)
    return markers


def _volume_number_from_queue_text(queue, raw):
    for value in (
        queue.get("issue_title"),
        raw.get("issue_title"),
        raw.get("title"),
    ):
        match = VOLUME_TITLE_RE.match(str(value or ""))
        if match:
            return _clean_number(match.group(1))
    for value in (queue.get("query"), raw.get("query")):
        text = str(value or "")
        if CHAPTER_QUERY_RE.search(text):
            continue
        match = VOLUME_QUERY_RE.search(text)
        if match:
            return _clean_number(match.group(1))
    return ""


def _singleton_issue_context(db_path, series_id, *, now=None, con=None):
    series_id = str(series_id or "").strip()
    if not db_path or not series_id:
        return {}
    return singleton_issue_contexts_by_series_id(db_path, [series_id], now=now, con=con).get(series_id) or {}


def singleton_issue_contexts_by_series_id(db_path, series_ids, *, now=None, con=None):
    """Batch the four singleton-context lookups across every series in one go."""

    series_ids = [str(value or "").strip() for value in _list(series_ids) if str(value or "").strip()]
    if not db_path or not series_ids:
        return {}
    now = float(now if now is not None else time.time())
    placeholders = ",".join("?" for _ in series_ids)
    normalized_one = inkdrop_state.normalize_issue_number("1")
    try:
        with _borrowed_or_read_con(db_path, con) as lookup_con:
            if not inkdrop_state.table_exists(lookup_con, "series") or not inkdrop_state.table_exists(lookup_con, "issues"):
                return {series_id: {} for series_id in series_ids}
            series_rows = lookup_con.execute(
                "select id, title, media_type, monitored, metadata_provider, metadata_id, source, updated_at, raw_json "
                f"from series where id in ({placeholders})",
                series_ids,
            ).fetchall()
            target_issue_rows = lookup_con.execute(
                "select series_id, id, title, issue_number, normalized_number, metadata_provider, metadata_id, "
                "json_extract(case when json_valid(raw_json) then raw_json else '{}' end, '$.id') raw_issue_id, "
                "coalesce(json_extract(case when json_valid(raw_json) then raw_json else '{}' end, '$.kapowarrIssueId'), "
                "json_extract(case when json_valid(raw_json) then raw_json else '{}' end, '$.kapowarr_issue_id')) raw_kapowarr_issue_id "
                f"from issues where series_id in ({placeholders}) and "
                "(coalesce(normalized_number,'')=? or (coalesce(normalized_number,'')='' and coalesce(issue_number,'') in ('1','01','001','0001')))",
                (*series_ids, normalized_one),
            ).fetchall()
            other_issue_series = {
                str(row["series_id"] or "").strip()
                for row in lookup_con.execute(
                    f"select distinct series_id from issues where series_id in ({placeholders}) and not "
                    "(coalesce(normalized_number,'')=? or (coalesce(normalized_number,'')='' and coalesce(issue_number,'') in ('1','01','001','0001')))",
                    (*series_ids, normalized_one),
                ).fetchall()
            }
            collected_wanted_rows = lookup_con.execute(
                "select wi.series_id as wanted_series_id, wi.id wanted_row_id, wi.issue_id, i.title, i.issue_number, i.normalized_number, "
                "i.metadata_provider, i.metadata_id "
                "from wanted_items wi join issues i on i.id=wi.issue_id and i.series_id=wi.series_id "
                f"where wi.series_id in ({placeholders}) and lower(coalesce(wi.status,'wanted')) in ('wanted','in_progress')",
                series_ids,
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {series_id: {} for series_id in series_ids}
    series_row_by_id = {str(row["id"] or "").strip(): row for row in series_rows}
    target_rows_by_series = {}
    for row in target_issue_rows:
        target_rows_by_series.setdefault(str(row["series_id"] or "").strip(), []).append(row)
    collected_by_series = {}
    for row in collected_wanted_rows:
        collected_by_series.setdefault(str(row["wanted_series_id"] or "").strip(), []).append(row)
    out = {}
    for series_id in series_ids:
        out[series_id] = _singleton_issue_context_from_rows(
            series_id,
            series_row_by_id.get(series_id),
            target_rows_by_series.get(series_id) or [],
            series_id in other_issue_series,
            collected_by_series.get(series_id) or [],
            now=now,
            normalized_one=normalized_one,
        )
    return out


def _singleton_issue_context_from_rows(
    series_id,
    series_row,
    target_issue_rows,
    has_other_issue,
    collected_wanted_rows,
    *,
    now,
    normalized_one,
):
    provider = str(series_row["metadata_provider"] or "").strip().lower() if series_row else ""
    metadata_id = str(series_row["metadata_id"] or "").strip() if series_row else ""
    source = str(series_row["source"] or "").strip().lower() if series_row else ""
    updated_at = float(series_row["updated_at"] or 0) if series_row else 0
    stable_provider_identity = bool(
        provider == "comicvine"
        and _positive_numeric_id(metadata_id)
        and series_id == f"comicvine:{metadata_id}"
        and source == "comicvine"
    )
    metadata_age = now - updated_at
    metadata_fresh = bool(updated_at > 0 and 0 <= metadata_age <= SINGLETON_METADATA_MAX_AGE_SECONDS)
    raw = _json_loads(series_row["raw_json"] if series_row and stable_provider_identity and metadata_fresh else None)
    metadata_issue_count = inkdrop_state.series_display_metadata_from_raw(raw).get("issue_count")
    raw_canonical_one_row_count = len(target_issue_rows)
    raw_collected_wanted_count = len(collected_wanted_rows)
    explicit_replay_alias_count = 0
    if stable_provider_identity:
        target_issue_rows, collected_wanted_rows, explicit_replay_alias_count = (
            _project_authoritative_comicvine_issue_rows(target_issue_rows, collected_wanted_rows)
        )
    canonical_one_row_count = len(target_issue_rows)
    canonical_positive_metadata_ids = {
        str(row["metadata_id"] or "").strip()
        for row in target_issue_rows
        if str(row["metadata_provider"] or "").strip().lower() == "comicvine"
        and _positive_numeric_id(row["metadata_id"])
    }
    canonical_issue_count = canonical_one_row_count + (1 if has_other_issue else 0)
    trusted_issue_identity = bool(
        canonical_one_row_count == 1
        and len(canonical_positive_metadata_ids) == 1
    )
    singleton_issue_proof = bool(
        stable_provider_identity
        and metadata_fresh
        and trusted_issue_identity
        and metadata_issue_count == 1
        and canonical_issue_count == 1
    )
    collected_markers = _collected_singleton_markers(
        series_row["title"] if series_row else "",
        collected_wanted_rows[0]["title"] if len(collected_wanted_rows) == 1 else "",
    )
    collected_wanted_issue_trusted = bool(
        len(collected_wanted_rows) == 1
        and str(collected_wanted_rows[0]["metadata_provider"] or "").strip().lower() == "comicvine"
        and _positive_numeric_id(collected_wanted_rows[0]["metadata_id"])
        and (
            str(collected_wanted_rows[0]["normalized_number"] or "") == normalized_one
            or str(collected_wanted_rows[0]["issue_number"] or "") in {"1", "01", "001", "0001"}
        )
    )
    collected_singleton_proof = bool(
        stable_provider_identity
        and metadata_fresh
        and not _metadata_declares_multiple_issues(metadata_issue_count)
        and trusted_issue_identity
        and series_row
        and bool(series_row["monitored"])
        and canonical_issue_count == 1
        and canonical_one_row_count == 1
        and len(canonical_positive_metadata_ids) == 1
        and len(collected_wanted_rows) == 1
        and collected_wanted_issue_trusted
        and collected_markers
        and "comic" in str(series_row["media_type"] or "").lower()
    )
    inferred_singleton_issue_proof = bool(
        metadata_issue_count in (None, "")
        and collected_singleton_proof
    )
    singleton_issue_proof = bool(singleton_issue_proof or inferred_singleton_issue_proof)
    singleton_issue_proof_source = (
        "comicvine_authoritative_count_and_canonical_issue_identity"
        if metadata_issue_count == 1 and singleton_issue_proof
        else (
            "comicvine_collected_single_wanted_identity_without_declared_count"
            if inferred_singleton_issue_proof
            else ""
        )
    )
    authoritative_issue = target_issue_rows[0] if len(target_issue_rows) == 1 else None
    collected_title_aliases = (
        inkdrop_sources.collected_title_aliases(series_row["title"])
        if collected_singleton_proof and series_row
        else []
    )
    return {
        "singleton_series_id": str(series_row["id"] or "") if series_row else "",
        "singleton_series_title": str(series_row["title"] or "") if series_row else "",
        "singleton_issue_id": str(authoritative_issue["id"] or "") if authoritative_issue else "",
        "singleton_issue_metadata_provider": str(authoritative_issue["metadata_provider"] or "") if authoritative_issue else "",
        "singleton_issue_metadata_id": str(authoritative_issue["metadata_id"] or "") if authoritative_issue else "",
        "singleton_issue_number": "1" if authoritative_issue else "",
        "media_type": str(series_row["media_type"] or "") if series_row else "",
        "canonical_issue_count": canonical_issue_count,
        "canonical_issue_one_row_count": canonical_one_row_count,
        "canonical_issue_positive_metadata_id_count": len(canonical_positive_metadata_ids),
        "canonical_issue_one_row_count_before_replay_projection": raw_canonical_one_row_count,
        "comicvine_replay_alias_count": explicit_replay_alias_count,
        "metadata_issue_count": metadata_issue_count,
        "singleton_metadata_trusted": stable_provider_identity,
        "singleton_metadata_fresh": metadata_fresh,
        "singleton_issue_metadata_trusted": trusted_issue_identity,
        "singleton_issue_proof": singleton_issue_proof,
        "singleton_issue_proof_source": singleton_issue_proof_source,
        "collected_singleton_wanted_count": len(collected_wanted_rows),
        "collected_singleton_wanted_count_before_replay_projection": raw_collected_wanted_count,
        "collected_singleton_markers": collected_markers,
        "collected_singleton_title_aliases": collected_title_aliases,
        "collected_singleton_proof": collected_singleton_proof,
        "collected_singleton_proof_source": (
            "comicvine_collected_single_wanted_identity" if collected_singleton_proof else ""
        ),
    }


def wanted_item_from_queue(queue, db_path=None, *, con=None, singleton_context=None):
    queue = _dict(queue)
    raw = _json_loads(queue.get("raw_json"))
    kapowarr_issue_id = queue.get("kapowarr_issue_id") or raw.get("kapowarr_issue_id") or raw.get("kapowarrIssueId")
    release_date = (
        queue.get("issue_release_date")
        or queue.get("release_date")
        or raw.get("release_date")
        or raw.get("date")
    )
    # Durable queue projection fields outrank raw_json.  In particular, a
    # caller may not inject MangaDex provenance into a ComicVine issue.
    issue_metadata_provider = queue.get("issue_metadata_provider")
    series_metadata_provider = queue.get("metadata_provider")
    wanted = {
        "queue_id": queue.get("id"),
        "wanted_id": queue.get("wanted_id"),
        "series_id": queue.get("series_id"),
        "issue_id": queue.get("issue_id"),
        "series_title": queue.get("series") or raw.get("series") or queue.get("query"),
        "series": queue.get("series") or raw.get("series"),
        "title": queue.get("issue_title") or queue.get("series") or queue.get("query"),
        "query": queue.get("query") or raw.get("query") or queue.get("series"),
        "issue_number": queue.get("issue_number") or raw.get("issue_number"),
        "year": queue.get("year") or raw.get("year"),
        "release_date": release_date,
        "date": release_date,
        "publisher": queue.get("publisher") or raw.get("publisher"),
        "media_type": queue.get("media_type") or raw.get("media_type"),
        "metadata_provider": issue_metadata_provider or series_metadata_provider,
        "issue_metadata_provider": issue_metadata_provider,
        "series_metadata_provider": series_metadata_provider,
        "metadata_id": queue.get("issue_metadata_id") or queue.get("metadata_id"),
        "kapowarr_id": queue.get("kapowarr_id"),
        "kapowarr_issue_id": kapowarr_issue_id,
    }
    volume_number = (
        queue.get("volume_number")
        or raw.get("volume_number")
        or raw.get("volumeNumber")
        or raw.get("volume")
        or _volume_number_from_queue_text(queue, raw)
    )
    explicit_unit_type = queue.get("unit_type") or queue.get("unitType") or raw.get("unit_type") or raw.get("unitType")
    explicit_unit_key = str(explicit_unit_type or "").strip().lower()
    chapter_native_provider = str(issue_metadata_provider or "").strip().lower() in inkdrop_manual_search.MANGA_CHAPTER_METADATA_PROVIDERS
    chapter_number = (
        queue.get("chapter_number")
        or raw.get("chapter_number")
        or raw.get("chapterNumber")
        or (raw.get("chapter") if explicit_unit_key == "chapter" or chapter_native_provider else None)
    )
    durable_identity = bool(wanted.get("issue_id") and (issue_metadata_provider or series_metadata_provider))
    trusted_identity = inkdrop_manual_search.trusted_target_unit_identity(
        {
            "unit_type": None if durable_identity else explicit_unit_type,
            "media_type": wanted.get("media_type"),
            "unit_number": wanted.get("issue_number"),
            "series_metadata_provider": series_metadata_provider,
            "issue_metadata_provider": issue_metadata_provider,
            "target_unit_metadata_trusted": durable_identity,
        }
    )
    if durable_identity or (not explicit_unit_type and not volume_number and not chapter_number):
        explicit_unit_type = trusted_identity.get("unit_type")
        if explicit_unit_type == "volume":
            volume_number = trusted_identity.get("volume_number") or wanted.get("issue_number")
            chapter_number = None
        elif explicit_unit_type == "chapter":
            chapter_number = wanted.get("issue_number")
            volume_number = None
        elif durable_identity:
            # A durable western issue is neither a manga volume nor a chapter.
            # Do not fall back to caller-controlled raw unit aliases.
            volume_number = None
            chapter_number = None
    if volume_number:
        wanted.setdefault("volume_number", volume_number)
        wanted.setdefault("volume", volume_number)
    if chapter_number:
        wanted.setdefault("chapter_number", chapter_number)
        wanted.setdefault("chapter", chapter_number)
    if volume_number or chapter_number:
        wanted.setdefault("unit_type", explicit_unit_type or ("chapter" if chapter_number else "volume"))
        wanted.setdefault("unitType", wanted["unit_type"])
    if singleton_context is not None:
        wanted.update(_dict(singleton_context))
    else:
        wanted.update(_singleton_issue_context(db_path, wanted.get("series_id"), con=con))
    return {key: value for key, value in wanted.items() if value not in (None, "", [], {})}


def _select_jobs(jobs, provider_ids=None, limit=None):
    provider_filter = {str(value).strip() for value in (provider_ids or []) if str(value or "").strip()}
    selected = [
        job
        for job in jobs or []
        if not provider_filter or str(job.get("provider_id") or "").strip() in provider_filter
    ]
    if limit not in (None, ""):
        selected = selected[: max(0, int(limit or 0))]
    return selected


def queue_items_by_id(db_path, queue_ids, *, con=None):
    """Batched queue_item hydration: same row shape, one query for the pass."""

    queue_ids = [str(value or "").strip() for value in _list(queue_ids) if str(value or "").strip()]
    if not queue_ids:
        return {}
    if con is None and not Path(db_path).exists():
        return {}
    placeholders = ",".join("?" for _ in queue_ids)
    with _borrowed_or_read_con(db_path, con) as lookup_con:
        rows = lookup_con.execute(
            f"""
            select q.id, q.wanted_id, q.series_id, q.issue_id,
                   q.state, q.current_source, q.query, q.last_event, q.active,
                   q.created_at, q.updated_at, q.source_order_json,
                   q.recovery_steps_json, q.raw_json, w.status as wanted_status,
                   s.title as series, s.media_type, s.year, s.publisher,
                   s.metadata_provider, s.metadata_id, s.kapowarr_id, s.source as series_source,
                   i.issue_number, i.title as issue_title, i.release_date,
                   i.metadata_provider as issue_metadata_provider,
                   i.metadata_id as issue_metadata_id, i.kapowarr_issue_id
            from queue_items q
            left join wanted_items w on w.id = q.wanted_id
            left join series s on s.id = q.series_id
            left join issues i on i.id = q.issue_id
            where q.id in ({placeholders})
            """,
            queue_ids,
        ).fetchall()
    out = {}
    for row in rows:
        item = dict(row)
        item["active"] = bool(item.get("active"))
        raw = inkdrop_state.json_loads(item.get("raw_json") or "{}", {})
        inkdrop_state.apply_ownership_evidence(item, raw if isinstance(raw, dict) else {})
        out[str(item.get("id") or "").strip()] = item
    return out


def source_jobs_for_queue(
    db_path,
    queue_id,
    *,
    include_operator=True,
    include_blocked=False,
    provider_ids=None,
    job_limit=20,
    queue=None,
    provider_health_map=None,
    singleton_context=None,
    con=None,
    settings_snapshot=None,
    registry_rows=None,
):
    queue = queue if isinstance(queue, dict) and queue else None
    if queue is None:
        if con is not None:
            queue = queue_items_by_id(db_path, [queue_id], con=con).get(str(queue_id or "").strip())
        else:
            queue = inkdrop_state.queue_item(db_path, queue_id, read_only=True)
    if not queue:
        return {"ok": False, "reason": "queue_item_not_found", "queue_id": queue_id}
    wanted = wanted_item_from_queue(queue, db_path=db_path, con=con, singleton_context=singleton_context)
    # A caller planning a whole pass (many queue rows) can precompute the
    # snapshot/registry once -- both are identical for every row in the pass
    # -- and pass them in via settings_snapshot/registry_rows instead of
    # having every row rebuild them from scratch.
    snapshot = settings_snapshot if isinstance(settings_snapshot, dict) else inkdrop_state.settings_snapshot(db_path)
    if provider_health_map is None:
        provider_health_map = {}
        with _borrowed_or_read_con(db_path, con) as health_con:
            if inkdrop_state.table_exists(health_con, "history_events"):
                provider_health_map = inkdrop_state.latest_provider_health_map(health_con)
    jobs = source_jobs.source_jobs_from_settings_snapshot(
        snapshot,
        wanted,
        include_operator=include_operator,
        include_blocked=include_blocked,
        limit=job_limit,
        provider_health_map=provider_health_map,
        registry_rows=registry_rows,
    )
    jobs = _select_jobs(jobs, provider_ids=provider_ids)
    return {
        "source_worker_coordinator_contract_version": CONTRACT_VERSION,
        "ok": True,
        "queue_id": queue_id,
        "wanted_item": wanted,
        "jobs": jobs,
        "summary": source_jobs.source_job_summary(jobs),
    }


def _jobs_by_provider_id(jobs):
    return {
        job.get("provider_id"): job
        for job in jobs or []
        if isinstance(job, dict) and job.get("provider_id")
    }


def direct_download_tasks_for_queue(db_path, queue_id, *, limit=20):
    queue_id = str(queue_id or "").strip()
    if not queue_id:
        return []
    with inkdrop_state.connect_read(db_path) as con:
        if not inkdrop_state.table_exists(con, "download_tasks"):
            return []
        rows = con.execute(
            """
            select id, queue_id, wanted_id, series_id, issue_id, source_attempt_id,
                   source, provider_id, provider, protocol, download_client, download_client_instance_id,
                   external_id, candidate_identity, lifecycle_phase, failure_reason,
                   retry_eligible, title, status, state, category, save_path,
                   local_path, size_bytes, progress, started_at, updated_at,
                   completed_at, raw_json
            from download_tasks
            where queue_id=?
              and lower(coalesce(download_client,'')) in ('inkdrop_direct','inkdrop_page_pack')
              and lower(coalesce(state,'')) in ('queued','downloading')
              and lower(coalesce(status,'')) in ('sent','download_resolved','download_started','downloading','waiting_for_staged_file')
            order by coalesce(updated_at, started_at, 0) asc, id asc
            limit ?
            """,
            (queue_id, max(1, min(int(limit or 20), 100))),
        ).fetchall()
    tasks = []
    for row in rows:
        task = dict(row)
        task["raw_json"] = _json_loads(task.get("raw_json"))
        tasks.append(task)
    return tasks


def pending_direct_stage_queue_ids(db_path, *, limit=50, exclude_queue_ids=None, queue_ids=None):
    excluded = {str(value or "").strip() for value in (exclude_queue_ids or []) if str(value or "").strip()}
    included = [str(value or "").strip() for value in (queue_ids or []) if str(value or "").strip()]
    limit = max(1, min(int(limit or 50), 100))
    if included:
        out = []
        for queue_id in included:
            if queue_id in excluded:
                continue
            if direct_download_tasks_for_queue(db_path, queue_id, limit=1):
                out.append(queue_id)
            if len(out) >= limit:
                break
        return out
    fetch_limit = min(500, limit + len(excluded))
    with inkdrop_state.connect_read(db_path) as con:
        if not inkdrop_state.table_exists(con, "download_tasks"):
            return []
        rows = con.execute(
            """
            select queue_id, min(coalesce(updated_at, started_at, 0)) as first_seen
            from download_tasks
            where queue_id is not null
              and lower(coalesce(download_client,'')) in ('inkdrop_direct','inkdrop_page_pack')
              and lower(coalesce(state,'')) in ('queued','downloading')
              and lower(coalesce(status,'')) in ('sent','download_resolved','download_started','downloading','waiting_for_staged_file')
            group by queue_id
            order by first_seen asc, queue_id asc
            limit ?
            """,
            (fetch_limit,),
        ).fetchall()
    out = []
    for row in rows:
        queue_id = str(row["queue_id"] or "").strip()
        if not queue_id or queue_id in excluded:
            continue
        out.append(queue_id)
        if len(out) >= limit:
            break
    return out


def pending_download_client_handoff_queue_ids(db_path, *, limit=50, queue_ids=None, now=None):
    """Return accepted client tasks that still need an authoritative client job."""

    included = [str(value or "").strip() for value in (queue_ids or []) if str(value or "").strip()]
    limit = max(1, min(int(limit or 50), 100))
    if included:
        candidates = included
    else:
        with inkdrop_state.connect_read(db_path) as con:
            if not inkdrop_state.table_exists(con, "download_tasks"):
                return []
            rows = con.execute(
                """
                select queue_id, min(coalesce(updated_at, started_at, 0)) as first_seen
                from download_tasks
                where queue_id is not null
                  and lower(coalesce(download_client,'')) in
                      ('qbittorrent','qbit','qb','sabnzbd','sab','transmission','deluge',
                       'nzbget','utorrent','rtorrent')
                  and lower(coalesce(state,'')) in ('queued','source_wait')
                  and lower(coalesce(status,'')) in ('sent','download_resolved')
                group by queue_id
                order by first_seen asc, queue_id asc
                limit ?
                """,
                (min(500, max(limit * 5, limit)),),
            ).fetchall()
        candidates = [str(row["queue_id"] or "").strip() for row in rows]
    out = []
    seen = set()
    with inkdrop_state.connect_read(db_path) as con:
        for queue_id in candidates:
            if not queue_id or queue_id in seen:
                continue
            seen.add(queue_id)
            queue = con.execute(
                """
                select q.active,q.state,w.status as wanted_status,s.raw_json as series_raw_json
                from queue_items q
                left join wanted_items w on w.id=q.wanted_id
                left join series s on s.id=q.series_id
                where q.id=?
                limit 1
                """,
                (queue_id,),
            ).fetchone()
            if (
                not queue
                or not int(queue["active"] or 0)
                or str(queue["state"] or "").strip().lower()
                in {"verified", "satisfied", "removed", "ignored", "inactive", "superseded_duplicate", "blocked", "needs_you"}
                or str(queue["wanted_status"] or "").strip().lower() not in {"wanted", "in_progress"}
                or inkdrop_state.series_row_user_removed({"raw_json": queue["series_raw_json"]})
            ):
                continue
            if download_client_handoff_tasks_for_queue(db_path, queue_id, limit=1, now=now):
                out.append(queue_id)
            if len(out) >= limit:
                break
    return out


def _legacy_sab_url_fetch_failure(task):
    return inkdrop_state.legacy_sab_url_fetch_transport_failure(task)


def _legacy_sab_retry_tasks(tasks, queue, completed_families, now):
    queue = _dict(queue)
    queue_raw = _json_loads(queue.get("queue_raw_json"))
    durable_claims = _dict(queue_raw.get("legacy_sab_url_fetch_retry_v1_claims"))
    if (
        not queue
        or not int(queue.get("active") or 0)
        or str(queue.get("state") or "").strip().lower()
        in {"verified", "satisfied", "removed", "ignored", "inactive", "superseded_duplicate"}
        or inkdrop_state.series_row_user_removed({"raw_json": queue.get("series_raw_json")})
    ):
        return []
    wanted_status = str(queue.get("wanted_status") or "").strip().lower()
    if wanted_status and wanted_status not in {"wanted", "in_progress", "blocked"}:
        return []
    by_id = {str(task.get("id") or ""): task for task in tasks if task.get("id")}
    linked = {}
    for sibling in tasks:
        if str(sibling.get("status") or "").strip().lower() != "superseded_by_failed_download":
            continue
        raw = _dict(sibling.get("raw_json"))
        if raw.get("legacy_sab_url_fetch_retry_v1_attempted") or raw.get("legacy_sab_url_fetch_retry_v1_attempted_at"):
            continue
        selected_id = str(raw.get("download_client_reconciled_selected_task_id") or "").strip()
        if selected_id:
            linked.setdefault(selected_id, []).append(sibling)
    out = []
    cutoff = now - LEGACY_SAB_URL_FETCH_RETRY_MAX_AGE_SECONDS
    for selected_id, siblings in linked.items():
        selected = by_id.get(selected_id)
        if not selected or len(siblings) != 1:
            continue
        sibling = siblings[0]
        selected_raw = _dict(selected.get("raw_json"))
        selected_at = float(selected.get("updated_at") or selected.get("completed_at") or selected.get("started_at") or 0)
        if (
            _normalize_handoff_client(selected.get("download_client")) != "sabnzbd"
            or _normalize_handoff_client(sibling.get("download_client")) != "sabnzbd"
            or str(selected.get("state") or "").strip().lower() != "failed"
            or str(selected.get("status") or "").strip().lower() != "failed_download"
            or not bool(selected.get("retry_eligible"))
            or not bool(sibling.get("retry_eligible"))
            or str(sibling.get("id") or "") in durable_claims
            or selected_raw.get("legacy_sab_url_fetch_retry_v1_attempted")
            or selected_raw.get("legacy_sab_url_fetch_retry_v1_attempted_at")
            or selected_at < cutoff
            or float(sibling.get("updated_at") or sibling.get("completed_at") or sibling.get("started_at") or 0) < cutoff
            or not _legacy_sab_url_fetch_failure(selected)
        ):
            continue
        selected_families = _task_handoff_family_tokens(selected)
        sibling_families = _task_handoff_family_tokens(sibling)
        if not selected_families.intersection(sibling_families) or sibling_families & completed_families:
            continue
        locator = _task_download_locator(sibling)
        if not locator or _task_locator_expired(sibling, now):
            continue
        sibling["_failed_handoff_retry"] = {
            "contract_version": 1,
            "legacy_sab_url_fetch_retry": True,
            "original_task_id": str(sibling.get("id") or ""),
            "authoritative_failed_task_id": selected_id,
            "original_candidate_identity": str(sibling.get("candidate_identity") or ""),
            "protected_locator_hash": _protected_locator_hash(locator),
            "original_failure_reason": "legacy_sab_url_fetch_transport_failure",
            "original_status": str(sibling.get("status") or ""),
        }
        out.append(sibling)
    for task in tasks:
        task_id = str(task.get("id") or "").strip()
        raw = _dict(task.get("raw_json"))
        observed_at = float(task.get("updated_at") or task.get("completed_at") or task.get("started_at") or 0)
        families = _task_handoff_family_tokens(task)
        if (
            not task_id
            or task_id in linked
            or _normalize_handoff_client(task.get("download_client")) != "sabnzbd"
            or str(task.get("state") or "").strip().lower() != "failed"
            or str(task.get("status") or "").strip().lower() != "failed_download"
            or not bool(task.get("retry_eligible"))
            or task_id in durable_claims
            or raw.get("legacy_sab_url_fetch_retry_v1_attempted")
            or raw.get("legacy_sab_url_fetch_retry_v1_attempted_at")
            or observed_at < cutoff
            or not _legacy_sab_url_fetch_failure(task)
            or families & completed_families
        ):
            continue
        locator = _task_download_locator(task)
        if not locator or _task_locator_expired(task, now):
            continue
        task["_failed_handoff_retry"] = {
            "contract_version": 1,
            "legacy_sab_url_fetch_retry": True,
            "direct_authoritative_retry": True,
            "original_task_id": task_id,
            "authoritative_failed_task_id": task_id,
            "original_candidate_identity": str(task.get("candidate_identity") or ""),
            "protected_locator_hash": _protected_locator_hash(locator),
            "original_failure_reason": "legacy_sab_url_fetch_transport_failure",
            "original_status": str(task.get("status") or ""),
        }
        out.append(task)
    return out


def download_client_handoff_tasks_for_queue(db_path, queue_id, *, source_attempt_id=None, limit=20, now=None):
    queue_id = str(queue_id or "").strip()
    if not queue_id:
        return []
    with inkdrop_state.connect_read(db_path) as con:
        if not inkdrop_state.table_exists(con, "download_tasks"):
            return []
        rows = con.execute(
            """
            select id, queue_id, wanted_id, series_id, issue_id, source_attempt_id,
                   source, provider_id, provider, protocol, download_client, download_client_instance_id,
                   external_id, candidate_identity, lifecycle_phase, failure_reason,
                   retry_eligible, title, status, state, category, save_path,
                   local_path, size_bytes, progress, started_at, updated_at,
                   completed_at, raw_json
            from download_tasks
            where queue_id=?
              and (? is null or source_attempt_id=?)
              and lower(coalesce(download_client,'')) in ('qbittorrent','qbit','qb','sabnzbd','sab','transmission','deluge','nzbget','utorrent','rtorrent')
            order by coalesce(updated_at, completed_at, started_at, 0) desc, id desc
            limit 200
            """,
            (queue_id, source_attempt_id, source_attempt_id),
        ).fetchall()
        queue = con.execute(
            """
            select q.active,q.state,q.raw_json as queue_raw_json,
                   w.status as wanted_status,s.raw_json as series_raw_json
            from queue_items q
            left join wanted_items w on w.id=q.wanted_id
            left join series s on s.id=q.series_id
            where q.id=?
            limit 1
            """,
            (queue_id,),
        ).fetchone()
    tasks = []
    for row in rows:
        task = dict(row)
        task["raw_json"] = _json_loads(task.get("raw_json"))
        tasks.append(task)

    now = time.time() if now is None else now
    retry_cutoff = now - FAILED_HANDOFF_RETRY_MAX_AGE_SECONDS
    active = []
    retryable_by_family = {}
    completed_families = set()
    for task in tasks:
        families = _task_handoff_family_tokens(task)
        state = str(task.get("state") or "").strip().lower()
        status = str(task.get("status") or "").strip().lower()
        if state in {"downloading", "import_ready", "importing", "completed", "imported"} or status in {
            "download_started", "downloading", "completed", "download_completed", "imported", "staged_file_ready"
        }:
            completed_families.update(families)
    # A family whose latest sibling attempt failed terminally with
    # retry_eligible=False is dead -- an older "sent" row for that same
    # candidate must not keep being treated as an active handoff still worth
    # completing. Without this, a stale "sent" row that never got its own
    # state updated (confirmed live: Court of Owls #1's TorrentLeech candidate,
    # candidate_identity 82a99a30d456ab1aa82cb9b4) keeps resurrecting the exact
    # same permanently-invalid torrent every pass, ignoring the retry_eligible
    # already recorded on its own sibling failure.
    dead_families = set()
    for task in tasks:
        status = str(task.get("status") or "").strip().lower()
        if status in FAILED_HANDOFF_RETRY_STATUSES and not bool(task.get("retry_eligible")):
            dead_families.update(_task_handoff_family_tokens(task))
    active_families = set()
    for task in tasks:
        families = _task_handoff_family_tokens(task)
        state = str(task.get("state") or "").strip().lower()
        status = str(task.get("status") or "").strip().lower()
        if (
            state in {"queued", "source_wait"}
            and status in {"sent", "download_resolved"}
            and not families & (completed_families | active_families | dead_families)
        ):
            active.append(task)
            active_families.update(families)
    if active:
        active.sort(key=lambda task: (float(task.get("updated_at") or task.get("started_at") or 0), str(task.get("id") or "")))
        return active[: max(1, min(int(limit or 20), 100))]
    for task in tasks:
        families = _task_handoff_family_tokens(task)
        status = str(task.get("status") or "").strip().lower()
        raw = _dict(task.get("raw_json"))
        updated_at = float(task.get("updated_at") or task.get("completed_at") or task.get("started_at") or 0)
        failure_reason = str(task.get("failure_reason") or "").strip().lower()
        if (
            families & completed_families
            or not bool(task.get("retry_eligible"))
            or status not in FAILED_HANDOFF_RETRY_STATUSES
            or raw.get("legacy_sab_url_fetch_retry_v1_attempted")
            or raw.get("legacy_sab_url_fetch_retry_v1_attempted_at")
            or updated_at < retry_cutoff
            or not _task_download_locator(task)
            or _task_locator_expired(task, now)
            or any(marker in failure_reason for marker in (
                "malformed nzb", "semantically unusable nzb", "nzb payload exceeded", "nzb_payload_invalid"
            ))
        ):
            continue
        task["_failed_handoff_retry"] = {
            "contract_version": 1,
            "original_task_id": str(task.get("id") or ""),
            "original_candidate_identity": str(task.get("candidate_identity") or ""),
            "protected_locator_hash": _protected_locator_hash(_task_download_locator(task)),
            "original_failure_reason": _safe_failure_reason(task.get("failure_reason")),
            "original_status": status,
        }
        duplicate_family = next((key for key in retryable_by_family if key in families), None)
        if duplicate_family is None:
            for key in families:
                retryable_by_family[key] = task
    retryable = list({id(task): task for task in retryable_by_family.values()}.values())
    retryable.extend(_legacy_sab_retry_tasks(tasks, dict(queue) if queue else {}, completed_families, now))
    retryable.sort(
        key=lambda task: float(task.get("updated_at") or task.get("completed_at") or task.get("started_at") or 0),
        reverse=True,
    )
    return retryable[: max(1, min(int(limit or 20), 100))]


def _first_text(*values):
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _is_protected_locator(value):
    return inkdrop_state.valid_protected_download_locator(value)


def _safe_failure_reason(value):
    text = str(value or "").strip()
    text = re.sub(r"(?i)\b(?:https?|magnet):[^\s\"']+", "[protected locator]", text)
    text = re.sub(r"(?i)\b(?:proxy-)?authorization\s*[:=][^\r\n]+", "Authorization: [redacted]", text)
    text = re.sub(r"(?i)\b(?:set-cookie|cookie)\s*[:=][^\r\n]+", "Cookie: [redacted]", text)
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(r'''(?i)(["'])(?:[A-Z]:[\\/]|\\\\|/|~[\\/]).*?\1''', "[redacted path]", text)
    text = re.sub(
        r'''(?ix)(?<![\w:])(?:[A-Z]:[\\/]|\\\\|/|~[\\/])[^\r\n,;"'<>]+?(?=\s*(?:[,;]|$))''',
        "[redacted path]",
        text,
    )
    text = re.sub(
        r'''(?ix)(?<![\w:])(?:[A-Z]:[\\/]|\\\\|/|~[\\/])[^\r\n,;"'<>]*?\.[A-Z0-9]{1,12}\b''',
        "[redacted path]",
        text,
    )
    text = re.sub(
        r'''(?ix)(?<![\w:])(?:[A-Z]:[\\/]|\\\\|/|~[\\/])[^\r\n,;"'<>]+$''',
        "[redacted path]",
        text,
    )
    text = re.sub(r'''(?i)(?<![\w:])(?:[A-Z]:[\\/]|\\\\)[^\s,;"'<>]+''', "[redacted path]", text)
    text = re.sub(r'''(?<![\w:])~[\\/][^\s,;"'<>]+''', "[redacted path]", text)
    text = re.sub(r'''(?<![\w:])/(?:[^/\s,;"'<>]+/)+[^\s,;"'<>]*''', "[redacted path]", text)
    text = re.sub(
        r'''(?ix)(api[_-]?key|apikey|token|password|passwd|secret|auth)["']?\s*[:=]\s*["']?([^&\s,"'}]+)''',
        r"\1=[redacted]",
        text,
    )
    return text[:240]


def _public_identifier(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if _is_protected_locator(text) else text


def _normalized_protected_locator(value):
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if not scheme or (scheme != "magnet" and not hostname):
            return text
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        if scheme == "magnet":
            return urlunsplit((scheme, "", parsed.path, query, ""))
        port = parsed.port
        host = f"[{hostname}]" if ":" in hostname else hostname
        if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
            host = f"{host}:{port}"
        return urlunsplit((scheme, host, parsed.path or "/", query, ""))
    except (TypeError, ValueError):
        return text


def _protected_locator_hash(value):
    return hashlib.sha256(_normalized_protected_locator(value).encode("utf-8")).hexdigest()


def _public_task_evidence(value, depth=0):
    if depth > 8:
        return None
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered.startswith("_runtime_"):
                continue
            if lowered in {
                "download_id", "download_url", "downloadurl", "downloadurlremote",
                "guid", "magneturl", "magnet_url", "url",
            } and _is_protected_locator(item):
                out[f"{key}_hash"] = _protected_locator_hash(item)
                continue
            if lowered in {
                "api-key", "api_key", "apikey", "authorization", "proxy-authorization", "cookie", "set-cookie",
                "password", "passkey", "secret", "token", "x-api-key", "x-auth-token",
            }:
                out[key] = "[redacted]"
                continue
            if lowered in {"local_path", "save_path", "partial_path", "path", "staging_root"}:
                out[key] = "[redacted]" if item not in (None, "") else item
                continue
            out[key] = _public_task_evidence(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_public_task_evidence(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return _safe_failure_reason(value)
    return value


def _task_handoff_family_tokens(task):
    task = _dict(task)
    locator = _task_download_locator(task)
    tokens = set()
    candidate = _first_text(task.get("candidate_identity"))
    if candidate:
        tokens.add(f"candidate:{candidate}")
    if locator:
        tokens.add(f"locator:{_protected_locator_hash(locator)}")
    if not tokens:
        fallback = _first_text(task.get("source_attempt_id"), task.get("external_id"), task.get("id"))
        if fallback:
            tokens.add(f"fallback:{fallback}")
    return tokens


def _task_handoff_family(task):
    tokens = _task_handoff_family_tokens(task)
    return sorted(tokens)[0] if tokens else ""


def _task_locator_expired(task, now=None):
    now = time.time() if now is None else float(now)
    expiries = []
    malformed_expiry = False
    nested_expiry_values = []

    def finite_number(value):
        nonlocal malformed_expiry
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            malformed_expiry = True
            return None
        if not math.isfinite(parsed):
            malformed_expiry = True
            return None
        return parsed

    def absolute_expiries(values):
        nonlocal malformed_expiry
        parsed_values = []
        for item in values if isinstance(values, (list, tuple)) else [values]:
            parsed = finite_number(item)
            if parsed is None:
                continue
            parsed_values.append(parsed / 1000.0 if parsed > 10_000_000_000 else parsed)
        if len(set(parsed_values)) > 1:
            malformed_expiry = True
        expiries.extend(parsed_values)

    def visit(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).strip().lower()
                if lowered in {"expires", "expires_at", "expiry", "expiry_at", "locator_expires_at", "download_url_expires_at"}:
                    nested_expiry_values.extend(item if isinstance(item, (list, tuple)) else [item])
                if isinstance(item, (dict, list)):
                    visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:50]:
                if isinstance(item, (dict, list)):
                    visit(item, depth + 1)

    visit(task)
    if nested_expiry_values:
        absolute_expiries(nested_expiry_values)
    locator = _task_download_locator(task)
    if locator.startswith(("http://", "https://")):
        try:
            query = {}
            for key, values in parse_qs(urlparse(locator).query, keep_blank_values=True).items():
                query.setdefault(str(key).strip().lower(), []).extend(values)
        except ValueError:
            return True
        standard_values = []
        for key in ("expires", "expires_at", "expiry", "exp"):
            standard_values.extend(query.get(key, []))
        if standard_values:
            absolute_expiries(standard_values)
        amz_dates = query.get("x-amz-date") or []
        amz_lifetimes = query.get("x-amz-expires") or []
        if amz_dates or amz_lifetimes:
            if not amz_dates or not amz_lifetimes or len(set(amz_dates)) > 1:
                malformed_expiry = True
            else:
                lifetimes = []
                for value in amz_lifetimes:
                    text = str(value)
                    if not re.fullmatch(r"(?:0|[1-9][0-9]*)", text):
                        malformed_expiry = True
                        continue
                    lifetime = int(text)
                    if lifetime > 604800:
                        malformed_expiry = True
                    lifetimes.append(lifetime)
                if len(set(lifetimes)) > 1:
                    malformed_expiry = True
                try:
                    signed_at = datetime.strptime(amz_dates[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
                    if lifetimes:
                        expiries.extend(signed_at + lifetime for lifetime in lifetimes)
                except ValueError:
                    malformed_expiry = True
    if len(set(expiries)) > 1:
        malformed_expiry = True
    return malformed_expiry or bool(expiries and min(expiries) <= now)


def _task_download_locator(task):
    task = _dict(task)
    raw = _dict(task.get("raw_json"))
    candidate = _dict(raw.get("candidate"))
    values = []
    seen_values = set()

    def add(value):
        text = str(value or "")
        if _is_protected_locator(text) and text not in seen_values:
            seen_values.add(text)
            values.append(text)

    def visit_nested(value, depth=0):
        if depth > 6:
            return
        if isinstance(value, dict):
            for key in (
                "download_id",
                "download_url",
                "downloadUrl",
                "downloadUrlRemote",
                "external_id",
                "guid",
                "magnetUrl",
                "magnet_url",
            ):
                add(value.get(key))
            for key in ("candidate", "download_task", "indexer", "raw", "raw_json", "result"):
                nested = value.get(key)
                if isinstance(nested, (dict, list)):
                    visit_nested(nested, depth + 1)
        elif isinstance(value, list):
            for item in value[:25]:
                if isinstance(item, (dict, list)):
                    visit_nested(item, depth + 1)

    for value in (
        raw.get("download_id"),
        raw.get("download_url"),
        raw.get("downloadUrl"),
        raw.get("downloadUrlRemote"),
        raw.get("external_id"),
        candidate.get("download_id"),
        candidate.get("download_url"),
        candidate.get("downloadUrl"),
        candidate.get("downloadUrlRemote"),
        candidate.get("magnetUrl"),
        candidate.get("magnet_url"),
    ):
        add(value)
    visit_nested(raw)
    add(task.get("external_id"))
    if not values:
        return ""

    def priority(value):
        text = value.lower()
        if text.startswith("magnet:"):
            return 0
        parsed = ""
        try:
            from urllib.parse import urlparse

            parsed = (urlparse(value).netloc or "").lower()
        except Exception:
            parsed = ""
        if ":9696" in parsed or "prowlarr" in parsed or "/download?" in text:
            return 0
        return 1

    return sorted(enumerate(values), key=lambda item: (priority(item[1]), item[0]))[0][1]


def _task_download_url_hash(task):
    task = _dict(task)
    raw = _dict(task.get("raw_json"))
    candidate = _dict(raw.get("candidate"))
    indexer = _dict(raw.get("indexer"))
    for value in (
        task.get("download_url_hash"),
        raw.get("download_url_hash"),
        candidate.get("download_url_hash"),
        indexer.get("download_url_hash"),
    ):
        text = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", text):
            return text
    return ""


def _task_auto_inspect_context(task):
    task = _dict(task)
    raw = _json_loads(task.get("raw_json"))
    marker = _dict(raw.get("auto_inspect"))
    digest = str(marker.get("candidate_identity_hash") or "").strip().lower()
    save_path = str(task.get("save_path") or "").strip().replace("\\", "/").rstrip("/")
    if not (
        marker.get("outcome") == "auto_inspect"
        and marker.get("exact_artifact_proof_required") is True
        and re.fullmatch(r"[a-f0-9]{64}", digest)
        and save_path.endswith(f"/auto-inspect/{digest[:20]}")
    ):
        return {}
    return {"marker": marker, "save_path": save_path}


def _apply_auto_inspect_qbit_settings(task, settings):
    context = _task_auto_inspect_context(task)
    settings = dict(settings or {})
    if not context:
        return settings
    media_type = _task_media_type(task)
    settings[f"{media_type}_save_path"] = context["save_path"]
    settings[f"{media_type}_category"] = "inkdrop-auto-inspect"
    return settings


def _task_requires_prowlarr_torrent_fetch(task):
    task = _dict(task)
    raw = _dict(task.get("raw_json"))
    candidate = _dict(raw.get("candidate"))
    provider_id = inkdrop_sources.provider_key(
        task.get("provider_id") or task.get("source") or candidate.get("provider_id")
    )
    locator = _task_download_locator(task)
    return bool(
        provider_id.startswith("prowlarr_")
        and str(task.get("protocol") or candidate.get("protocol") or "").strip().lower() == "torrent"
        and locator.startswith(("http://", "https://"))
        and candidate.get("authorized_prowlarr_download_url") is True
    )


def _task_torrent_identity(task):
    task = _dict(task)
    raw = _dict(task.get("raw_json"))
    candidate = _dict(raw.get("candidate"))
    indexer = _dict(raw.get("indexer"))
    info_hash = _first_text(
        task.get("info_hash"),
        task.get("torrent_hash"),
        raw.get("info_hash"),
        raw.get("torrent_hash"),
        candidate.get("info_hash"),
        indexer.get("info_hash"),
    ).lower()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", info_hash):
        info_hash = ""
    identity = {
        "info_hash": info_hash,
        "pack_member": _first_text(
            raw.get("pack_contents_matching_entry"),
            candidate.get("pack_contents_matching_entry"),
            _dict(candidate.get("pack_contents_match")).get("entry"),
            indexer.get("pack_contents_matching_entry"),
        ),
        "title": _first_text(candidate.get("title"), task.get("title")),
        "series_title": _first_text(candidate.get("series_title"), candidate.get("series"), raw.get("series_title")),
        "unit_type": _first_text(candidate.get("unit_type"), raw.get("unit_type"), task.get("unit_type")),
        "issue_number": _first_text(candidate.get("issue_number"), raw.get("issue_number"), task.get("issue_number")),
        "chapter_number": _first_text(candidate.get("chapter_number"), raw.get("chapter_number"), task.get("chapter_number")),
        "volume_number": _first_text(candidate.get("volume_number"), raw.get("volume_number"), task.get("volume_number")),
        "edition_id": _first_text(candidate.get("edition_id"), raw.get("edition_id")),
        "edition_marker": _first_text(candidate.get("edition_marker"), raw.get("edition_marker")),
        "publication_title": _first_text(candidate.get("publication_title"), raw.get("publication_title")),
        "publication_year": _first_text(candidate.get("publication_year"), candidate.get("year"), raw.get("publication_year")),
        "publisher": _first_text(candidate.get("publisher"), raw.get("publisher")),
    }
    identity["unit_number"] = _first_text(
        identity["issue_number"], identity["chapter_number"], identity["volume_number"]
    )
    return {key: value for key, value in identity.items() if value not in (None, "")}


def _visit_nested_payloads(value, *, depth=0, max_depth=8):
    if depth > max_depth:
        return
    if isinstance(value, dict):
        yield value
        for key in (
            "attempt",
            "candidate",
            "download_task",
            "download_client_handoff",
            "indexer",
            "pack_match",
            "pack_match_summary",
            "raw",
            "raw_json",
            "result",
            "source_attempt",
        ):
            nested = value.get(key)
            if isinstance(nested, (dict, list)):
                yield from _visit_nested_payloads(nested, depth=depth + 1, max_depth=max_depth)
    elif isinstance(value, list):
        for item in value[:50]:
            if isinstance(item, (dict, list)):
                yield from _visit_nested_payloads(item, depth=depth + 1, max_depth=max_depth)


def _pack_match_summary_from_task(task):
    task = _dict(task)
    for payload in _visit_nested_payloads(task):
        for key in ("pack_match_summary", "pack_match"):
            match = payload.get(key)
            if isinstance(match, dict) and match.get("covered_queue_ids"):
                return dict(match)
    return {}


def _covered_pack_queue_ids(task, primary_queue_id):
    primary_queue_id = str(primary_queue_id or "").strip()
    match = _pack_match_summary_from_task(task)
    out = []
    seen = {primary_queue_id} if primary_queue_id else set()
    for value in match.get("covered_queue_ids") or []:
        queue_id = str(value or "").strip()
        if not queue_id or queue_id in seen:
            continue
        seen.add(queue_id)
        out.append(queue_id)
    return out


def _task_media_type(task):
    # inkdrop_download_client_routing.select_url_handoff_instances() already
    # uses the shared classifier to pick a media-aware client instance (e.g.
    # a Manga-mapped qBittorrent instance) -- this local copy only recognized
    # ebooks and fell through to "comics" for everything else, including
    # Manga, so a correctly-instance-selected Manga handoff still got
    # materialized and dispatched with comics category/path. Delegating to
    # the same classifier both call sites agree with fixes the mismatch.
    return inkdrop_download_client_routing.task_media_type(task)


def _normalize_handoff_client(value):
    text = str(value or "").strip().lower()
    if text in {"qbittorrent", "qbit", "qb"}:
        return "qbittorrent"
    if text in {"sabnzbd", "sab"}:
        return "sabnzbd"
    if text in {"transmission", "transmissionbt"}:
        return "transmission"
    if text in {"deluge", "delugeweb"}:
        return "deluge"
    if text in {"nzbget", "nzb"}:
        return "nzbget"
    if text in {"utorrent", "utorrentweb", "utorrent_webui"}:
        return "utorrent"
    if text in {"rtorrent", "rtorrent_xmlrpc"}:
        return "rtorrent"
    return text


def _download_client_label(client):
    return {
        "qbittorrent": "qBittorrent",
        "sabnzbd": "SABnzbd",
        "transmission": "Transmission",
        "deluge": "Deluge",
        "nzbget": "NZBGet",
        "utorrent": "uTorrent",
        "rtorrent": "rTorrent",
    }.get(_normalize_handoff_client(client), str(client or "download client"))


def _download_client_exception_payload(exc, client):
    text = _safe_failure_reason(exc)
    lowered = text.lower()
    unavailable_markers = (
        "connection refused",
        "failed to establish a new connection",
        "max retries exceeded",
        "connection aborted",
        "connection reset",
        "timed out",
        "timeout",
        "total deadline",
        "name or service not known",
        "temporary failure in name resolution",
    )
    if any(marker in lowered for marker in unavailable_markers):
        return {
            "ok": False,
            "status": "provider_unavailable",
            "reason": f"{client or 'download_client'}_unavailable",
            "download_client": client,
            "retry_eligible": True,
        }
    if any(marker in lowered for marker in (
        "malformed nzb", "semantically unusable nzb", "nzb payload exceeded", "nzb_payload_invalid",
        "malformed torrent", "torrent payload exceeded", "torrent url authority",
        "torrent payload identity",
    )):
        reason = "prowlarr_torrent_payload_invalid" if "torrent" in lowered else "prowlarr_nzb_payload_invalid"
        return {
            "ok": False,
            "status": "bad_candidate",
            "reason": reason,
            "download_client": client,
            "retry_eligible": False,
        }
    return {
        "ok": False,
        "status": "failed_download",
        "reason": text or f"{client or 'download_client'}_handoff_failed",
        "download_client": client,
        "retry_eligible": True,
    }


def _task_handoff_unique_tag(task):
    task = _dict(task)
    retry = _dict(task.get("_failed_handoff_retry"))
    identity = _first_text(retry.get("original_task_id"), task.get("id"), task.get("candidate_identity"), task.get("external_id"))
    if not identity:
        return None
    return f"inkdrop-task-{identity[:24]}"


def _download_client_reported_success(result):
    result = _dict(result)
    if result.get("ok") is False:
        return False
    if result.get("added") is True or result.get("status") is True:
        return True
    if str(result.get("status") or "").strip().lower() in {"ok", "true", "success"}:
        return True
    if _client_external_id(result):
        return True
    return bool(result.get("ok"))


def _download_client_handoff_ambiguous(result):
    return str(_dict(result).get("status") or "").strip().lower() in {
        "enqueue_response_ambiguous",
        "ambiguous_enqueue_response",
    }


def _resolve_sab_handoff_by_stable_key(task, result, settings):
    """Perform one bounded ownership lookup when SAB omits its job id."""
    payload = dict(result or {})
    if _client_external_id(payload) or not _download_client_reported_success(payload):
        return payload
    import inkdrop_acquire

    unique_tag = _task_handoff_unique_tag(task)
    handoff_key = str(payload.get("handoff_key") or "").strip() or inkdrop_acquire.sab_handoff_key(
        task.get("title"),
        inkdrop_acquire.prowlarr_download_url_for_client(_task_download_locator(task)),
        unique_tag=unique_tag,
    )
    payload["handoff_key"] = handoff_key
    payload["stable_handoff_lookup_attempted"] = True
    try:
        http = inkdrop_acquire.require_requests()
        job = inkdrop_acquire.sab_find_existing_job(http, settings, handoff_key)
    except Exception as exc:
        payload["ok"] = False
        payload["status"] = "enqueue_response_ambiguous"
        payload["retry_eligible"] = True
        payload["stable_handoff_lookup_status"] = "unavailable"
        payload["stable_handoff_lookup_reason"] = _safe_failure_reason(exc)
        return payload
    if not job:
        payload["ok"] = False
        payload["status"] = "enqueue_response_ambiguous"
        payload["retry_eligible"] = True
        payload["stable_handoff_lookup_status"] = "not_found"
        return payload
    resolved = inkdrop_acquire.sab_existing_result(
        job,
        category=payload.get("category") or settings.get("category") or settings.get("comics_category"),
        handoff_key=handoff_key,
        settings_source=payload.get("settings_source") or settings.get("source"),
    )
    resolved.update({key: value for key, value in payload.items() if key not in resolved})
    resolved["stable_handoff_lookup_status"] = "matched"
    return resolved


def _default_download_client_adder(task):
    task = _dict(task)
    locator = _task_download_locator(task)
    if not locator:
        return {
            "ok": False,
            "status": "failed_download",
            "reason": "download_locator_missing",
        }
    raw_task = _dict(task.get("raw_json"))
    bound_digest = str(raw_task.get("locator_digest") or "").strip().lower()
    if bound_digest and bound_digest != hashlib.sha256(locator.encode("utf-8")).hexdigest():
        return {
            "ok": False,
            "status": "blocked",
            "reason": "candidate_locator_binding_mismatch",
        }
    client = _normalize_handoff_client(task.get("download_client") or task.get("protocol"))
    title = _first_text(task.get("title"), task.get("candidate_identity"), task.get("external_id"), "InkDrop source result")
    import inkdrop_acquire

    db_path = task.get("_runtime_db_path")
    instance_candidates = inkdrop_download_client_routing.select_url_handoff_instances(db_path, task) if db_path else []
    if instance_candidates:
        unique_tag = _task_handoff_unique_tag(task)
        failures = []
        for candidate in instance_candidates:
            instance = None
            try:
                instance = inkdrop_download_client_routing.materialize_instance_settings(
                    db_path, candidate["download_client_instance_id"], _task_media_type(task)
                )
                if candidate["client_type"] == "qbittorrent":
                    instance["settings"] = _apply_auto_inspect_qbit_settings(task, instance.get("settings"))
                outcome = inkdrop_download_client_routing.dispatch_instance(
                    instance,
                    locator,
                    title,
                    _task_media_type(task),
                    unique_tag=unique_tag,
                    dry_run=False,
                    expected_url_hash=_task_download_url_hash(task),
                    require_prowlarr_fetch=_task_requires_prowlarr_torrent_fetch(task),
                    expected_torrent_identity=_task_torrent_identity(task),
                )
            except Exception as exc:
                outcome = _download_client_exception_payload(exc, candidate["client_type"])
                outcome["download_client_instance_id"] = candidate["download_client_instance_id"]
            payload = dict(outcome or {})
            payload.setdefault("download_client_instance_id", candidate["download_client_instance_id"])
            payload["routing_reason"] = candidate["routing_reason"]
            if candidate["client_type"] == "sabnzbd" and instance is not None:
                payload = _resolve_sab_handoff_by_stable_key(task, payload, instance["settings"])
            if _download_client_handoff_ok(payload) or _download_client_handoff_ambiguous(payload) or _client_external_id(payload) or payload.get("added") is True:
                payload["instance_failover_attempts"] = failures
                return payload
            failures.append({"download_client_instance_id": candidate["download_client_instance_id"],
                "client_type": candidate["client_type"], "reason": str(payload.get("reason") or payload.get("status") or "failed")[:160]})
        payload["instance_failover_attempts"] = failures
        return payload

    try:
        if client == "qbittorrent":
            unique_tag = _task_handoff_unique_tag(task)
            qbit_kwargs = {}
            if _task_auto_inspect_context(task):
                qbit_kwargs["settings_override"] = _apply_auto_inspect_qbit_settings(
                    task, inkdrop_acquire.load_qbit_settings()
                )
            outcome = inkdrop_acquire.qbit_add(
                locator,
                title,
                _task_media_type(task),
                dry_run=False,
                unique_tag=unique_tag,
                expected_url_hash=_task_download_url_hash(task),
                require_prowlarr_fetch=_task_requires_prowlarr_torrent_fetch(task),
                expected_torrent_identity=_task_torrent_identity(task),
                **qbit_kwargs,
            )
        elif client == "sabnzbd":
            unique_tag = _task_handoff_unique_tag(task)
            outcome = inkdrop_acquire.sab_add(locator, title, dry_run=False, unique_tag=unique_tag)
            outcome = _resolve_sab_handoff_by_stable_key(task, outcome, inkdrop_acquire.load_sab_settings())
        elif client == "transmission":
            unique_tag = _task_handoff_unique_tag(task)
            outcome = inkdrop_acquire.transmission_add(locator, title, dry_run=False, unique_tag=unique_tag)
        elif client == "deluge":
            unique_tag = _task_handoff_unique_tag(task)
            outcome = inkdrop_acquire.deluge_add(locator, title, dry_run=False, unique_tag=unique_tag)
        elif client == "nzbget":
            unique_tag = _task_handoff_unique_tag(task)
            outcome = inkdrop_acquire.nzbget_add(locator, title, dry_run=False, unique_tag=unique_tag)
        elif client == "utorrent":
            unique_tag = _task_handoff_unique_tag(task)
            outcome = inkdrop_acquire.utorrent_add(locator, title, dry_run=False, unique_tag=unique_tag)
        elif client == "rtorrent":
            unique_tag = _task_handoff_unique_tag(task)
            outcome = inkdrop_acquire.rtorrent_add(locator, title, dry_run=False, unique_tag=unique_tag)
        else:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "unsupported_handoff_download_client",
                "download_client": client,
            }
    except Exception as exc:
        return _download_client_exception_payload(exc, client)
    if isinstance(outcome, dict):
        payload = dict(outcome)
    else:
        payload = {"result": outcome}
    payload.setdefault(
        "download_client",
        _download_client_label(client),
    )
    payload.setdefault("protocol", "usenet" if client in {"sabnzbd", "nzbget"} else "torrent")
    payload["ok"] = _download_client_handoff_ok(payload)
    return payload


def _download_client_handoff_ok(result):
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False:
        return False
    if result.get("dry_run"):
        return True
    return bool(_client_external_id(result) and _download_client_reported_success(result))


def _client_external_id(result):
    result = _dict(result)
    for key in ("client_id", "client_external_id", "hash", "torrent_hash", "nzo_id", "nzoId"):
        value = result.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    values = result.get("nzo_ids") or result.get("nzoIds")
    if isinstance(values, list) and values:
        return str(values[0])
    return ""


def _handoff_attempt_from_task(task, handoff_result, *, now=None):
    task = _dict(task)
    result = _dict(handoff_result)
    raw = _json_loads(task.get("raw_json"))
    auto_inspect = _dict(raw.get("auto_inspect"))
    now = time.time() if now is None else now
    client = _normalize_handoff_client(result.get("download_client") or task.get("download_client"))
    label = _download_client_label(client)
    ok = _download_client_handoff_ok(result)
    reported_success = _download_client_reported_success(result)
    result_status = str(result.get("status") or "").strip().lower()
    terminal_failure = result_status in {
        "bad_candidate", "invalid_payload", "malformed_nzb", "semantically_unusable_nzb",
        "completed", "download_completed", "imported", "verified",
    }
    retry_eligible = bool(result.get("retry_eligible", not ok)) and not terminal_failure
    ambiguous = bool(_download_client_handoff_ambiguous(result) or (reported_success and not ok))
    attempt_status = "download_started" if ok else (
        "enqueue_response_ambiguous"
        if ambiguous
        else
        result_status
        if retry_eligible and result_status in {"client_unavailable", "provider_unavailable", "provider_wait"}
        else "failed_download"
    )
    client_external_id = _public_identifier(_client_external_id(result))
    task_external_id = task.get("external_id")
    attempt_candidate_identity = task.get("candidate_identity")
    retry_contract = _dict(task.get("_failed_handoff_retry"))
    if retry_eligible and result_status in {"client_unavailable", "provider_unavailable", "provider_wait"} and retry_contract:
        original_task_id = _first_text(retry_contract.get("original_task_id"), task.get("id"))
        retry_evidence_id = f"retry-evidence:{hashlib.sha256(original_task_id.encode('utf-8')).hexdigest()[:24]}"
        task_external_id = retry_evidence_id
        attempt_candidate_identity = retry_evidence_id
    if _is_protected_locator(task_external_id):
        task_external_id = task.get("candidate_identity") or hashlib.sha256(
            str(task_external_id).encode("utf-8")
        ).hexdigest()
    reason = (
        f"{label} download visible; waiting for downloader/import confirmation"
        if ok
        else f"{label} accepted the request but did not return an authoritative job id; guarded retry scheduled"
        if ambiguous
        else _safe_failure_reason(
            _first_text(result.get("failure_reason"), result.get("reason"), f"{label} handoff failed")
        )
    )
    return {
        "source": task.get("source") or "download_client",
        "provider_id": task.get("provider_id"),
        "provider": task.get("provider"),
        "source_type": task.get("source_type") or "download_client",
        "protocol": result.get("protocol") or task.get("protocol"),
        "download_client": client,
        "download_client_instance_id": result.get("download_client_instance_id") or task.get("download_client_instance_id"),
        "external_id": client_external_id if ok and client_external_id else task_external_id,
        "client_external_id": client_external_id,
        "torrent_hash": client_external_id if ok and client in {"qbittorrent", "transmission", "deluge", "utorrent", "rtorrent"} and client_external_id else None,
        "nzo_id": client_external_id if ok and client in {"sabnzbd", "nzbget"} and client_external_id else None,
        "candidate_identity": attempt_candidate_identity,
        "download_url_hash": raw.get("download_url_hash") or (
            _protected_locator_hash(task.get("external_id"))
            if _is_protected_locator(task.get("external_id"))
            else task.get("external_id")
        ),
        "category": result.get("category") or task.get("category"),
        "save_path": result.get("save_path") or task.get("save_path"),
        "local_path": None,
        "size_bytes": task.get("size_bytes"),
        "status": attempt_status,
        "reason": reason,
        "failure_reason": "" if ok else reason,
        "retry_eligible": retry_eligible,
        "title": _safe_failure_reason(task.get("title")),
        "started_at": now,
        "completed_at": None if ok else now,
        "raw": {
            "kind": "source_worker_download_client_handoff",
            "download_task": _public_task_evidence(task),
            "download_client_handoff": _public_task_evidence(result),
            "client_external_id": client_external_id,
            "handoff_ownership_confirmed": bool(ok and client_external_id),
            "handoff_response_ambiguous": ambiguous,
            "failed_handoff_retry": _public_task_evidence(task.get("_failed_handoff_retry")),
            "handoff_at": now,
            "handoff_at_iso": inkdrop_state.utc_stamp(now),
            "auto_inspect": auto_inspect,
        },
    }


def _covered_pack_handoff_attempt(task, handoff_result, primary_queue_id, *, now=None):
    attempt = _handoff_attempt_from_task(task, handoff_result, now=now)
    raw = _dict(attempt.get("raw"))
    raw["kind"] = "source_worker_pack_covered_download_client_handoff"
    raw["primary_queue_id"] = primary_queue_id
    raw["pack_match_summary"] = _pack_match_summary_from_task(task)
    attempt["raw"] = raw
    if attempt.get("status") == "download_started":
        label = _download_client_label(attempt.get("download_client"))
        attempt["reason"] = f"{label} pack download visible; this wanted issue is covered by the same pack"
        attempt["retry_eligible"] = False
    return attempt


def _record_covered_pack_handoffs(db_path, primary_queue_id, task, handoff_result, *, now=None):
    if not _download_client_handoff_ok(handoff_result):
        return {"updated": 0, "covered_queue_ids": [], "records": []}
    covered_queue_ids = _covered_pack_queue_ids(task, primary_queue_id)
    if not covered_queue_ids:
        return {"updated": 0, "covered_queue_ids": [], "records": []}
    records = []
    for covered_queue_id in covered_queue_ids:
        queue = inkdrop_state.queue_item(db_path, covered_queue_id, read_only=True)
        if not queue or not queue.get("active"):
            records.append({"queue_id": covered_queue_id, "ok": False, "reason": "covered_queue_inactive_or_missing"})
            continue
        state = str(queue.get("state") or "").strip().lower()
        if state in {"downloading", "importing", "verified"}:
            records.append({"queue_id": covered_queue_id, "ok": True, "skipped": True, "reason": f"covered_queue_already_{state}"})
            continue
        attempt = _covered_pack_handoff_attempt(task, handoff_result, primary_queue_id, now=now)
        try:
            recorded = inkdrop_state.record_queue_source_attempt(
                db_path,
                covered_queue_id,
                attempt,
                started_at=attempt.get("started_at") or now,
                completed_at=attempt.get("completed_at"),
            )
        except Exception as exc:
            if not inkdrop_state.is_database_locked_error(exc):
                raise
            recorded = {"ok": False, "reason": "database_locked_while_recording_covered_pack_handoff", "error": str(exc)}
        recorded["queue_id"] = covered_queue_id
        records.append(recorded)
    return {
        "updated": sum(1 for row in records if row.get("ok") and not row.get("skipped")),
        "covered_queue_ids": covered_queue_ids,
        "records": records,
    }


def handoff_download_client_tasks(
    db_path,
    queue_id,
    *,
    source_attempt_id=None,
    add_download_client=None,
    dry_run=True,
    limit=20,
    max_successful=None,
    stop_on_failure=False,
    now=None,
):
    tasks = download_client_handoff_tasks_for_queue(db_path, queue_id, source_attempt_id=source_attempt_id, limit=limit, now=now)
    out = {
        "source_worker_coordinator_contract_version": CONTRACT_VERSION,
        "ok": True,
        "dry_run": bool(dry_run),
        "queue_id": queue_id,
        "handoff_download_clients": sorted(HANDOFF_DOWNLOAD_CLIENTS),
        "tasks_available": len(tasks),
        "tasks": [
            {
                "id": task.get("id"),
                "provider_id": task.get("provider_id"),
                "download_client": task.get("download_client"),
                "status": task.get("status"),
                "state": task.get("state"),
                "title": _safe_failure_reason(task.get("title")),
            }
            for task in tasks
        ],
        "handoff_results": [],
        "attempt_records": [],
        "pack_covered_handoff_records": [],
        "admission_blocks": [],
    }
    if dry_run or not tasks:
        return out
    max_successful = None if max_successful in (None, "") else max(1, int(max_successful))
    add_download_client = add_download_client or _default_download_client_adder
    successful = 0
    for task in tasks:
        task = dict(task)
        task["_runtime_db_path"] = str(db_path)
        admission = inkdrop_missing_recovery_policy.evaluate_new_handoff(
            db_path,
            proposed_bytes=task.get("size_bytes") or 0,
            now=now,
        )
        if not admission.get("allowed"):
            out["admission_blocks"].append({
                "task_id": task.get("id"),
                "reason": admission.get("reason"),
                "recovery_active": bool(admission.get("recovery_active")),
            })
            continue
        retry_contract = _dict(task.get("_failed_handoff_retry"))
        if retry_contract.get("legacy_sab_url_fetch_retry") and not inkdrop_state.claim_legacy_sab_url_fetch_retry(
            db_path,
            task.get("id"),
            retry_contract.get("authoritative_failed_task_id"),
            locator=_task_download_locator(task),
            max_age_seconds=LEGACY_SAB_URL_FETCH_RETRY_MAX_AGE_SECONDS,
            now=now,
        ):
            continue
        result = add_download_client(task)
        out["handoff_results"].append(_public_task_evidence(result))
        attempt = _handoff_attempt_from_task(task, result, now=now)
        recorded = inkdrop_state.record_queue_source_attempt(
            db_path,
            queue_id,
            attempt,
            started_at=attempt.get("started_at"),
            completed_at=attempt.get("completed_at"),
        )
        out["attempt_records"].append(recorded)
        covered = _record_covered_pack_handoffs(db_path, queue_id, task, result, now=now)
        if covered.get("covered_queue_ids"):
            out["pack_covered_handoff_records"].append(covered)
        if _download_client_handoff_ok(result):
            successful += 1
            if max_successful is not None and successful >= max_successful:
                break
        elif stop_on_failure:
            break
    out["tasks_handed_off"] = sum(1 for result in out["handoff_results"] if _download_client_handoff_ok(result))
    out["tasks_failed"] = sum(1 for result in out["handoff_results"] if not _download_client_handoff_ok(result))
    out["pack_covered_handoffs"] = sum(int(row.get("updated") or 0) for row in out["pack_covered_handoff_records"])
    out["tasks_deferred_by_recovery_policy"] = len(out["admission_blocks"])
    out["ok"] = out["tasks_failed"] == 0
    return out


def _stage_attempt_from_task(task, download_result, *, now=None):
    task = _dict(task)
    result = _dict(download_result)
    now = time.time() if now is None else now
    ok = bool(result.get("ok"))
    download_client = str(result.get("download_client") or task.get("download_client") or "inkdrop_direct").strip()
    download_label = "page pack" if download_client == "inkdrop_page_pack" else "direct download"
    status = "staged_file_ready" if ok else "failed_download"
    reason = (
        f"{download_label} staged file ready"
        if ok
        else (result.get("failure_reason") or result.get("reason") or f"{download_client}_failed")
    )
    return {
        "source": task.get("source") or task.get("provider_id") or task.get("provider"),
        "provider_id": task.get("provider_id"),
        "provider": task.get("provider"),
        "source_type": "direct_download",
        "protocol": task.get("protocol") or "http",
        "download_client": download_client,
        "external_id": task.get("external_id") or task.get("candidate_identity"),
        "candidate_identity": task.get("candidate_identity"),
        "download_url_hash": task.get("external_id"),
        "category": task.get("category") or "inkdrop-direct",
        "save_path": str(Path(result.get("local_path") or task.get("local_path") or "").parent) if (result.get("local_path") or task.get("local_path")) else task.get("save_path"),
        "local_path": result.get("local_path") or task.get("local_path"),
        "partial_path": result.get("partial_path"),
        "size_bytes": result.get("size_bytes") or task.get("size_bytes"),
        "content_type": result.get("content_type"),
        "content_hash": result.get("content_hash"),
        "status": status,
        "reason": reason,
        "failure_reason": "" if ok else reason,
        "retry_eligible": not ok,
        "title": task.get("title"),
        "started_at": now,
        "completed_at": now,
        "raw": {
            "kind": "source_worker_direct_stage",
            "download_task": task,
            "direct_download_result": result,
        },
    }


def _existing_staged_file_result(task, *, staging_root=None):
    task = _dict(task)
    download_client = str(task.get("download_client") or "").strip().lower()
    if download_client not in STAGEABLE_DOWNLOAD_CLIENTS:
        return None
    local_path = str(task.get("local_path") or "").strip()
    if not local_path:
        return None
    root_text = str(staging_root or task.get("save_path") or "").strip()
    if not root_text:
        return None
    try:
        root = Path(root_text).expanduser().resolve()
        target = Path(local_path).expanduser()
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
        target.relative_to(root)
    except Exception:
        return None
    if download_client == "inkdrop_page_pack" and target.suffix.lower() != ".cbz":
        return None
    try:
        if not target.exists() or not target.is_file():
            return None
        size_bytes = int(target.stat().st_size)
    except Exception:
        return None
    if size_bytes <= 0:
        return None
    task_id = str(task.get("id") or task.get("external_id") or task.get("candidate_identity") or "").strip()
    metadata_path = target.with_name(target.name + ".source.json")
    # Adopting a file that is already on disk is how a same-title collision
    # turns into a silent wrong import: two candidates derive the same staged
    # path (provider + normalized title + extension, no candidate identity), so
    # the second one finds the first one's bytes sitting there and takes them.
    # Nothing downstream re-checks whose bytes those are -- the declared size is
    # not compared, no hash is recorded, and the task is marked import_ready.
    # The sidecar naming the owning task is right next to the file and was
    # already being located here without ever being opened.
    #
    # A staged file always has one: the downloader treats a sidecar write
    # failure as a hard block (inkdrop_direct_downloader.py "metadata_sidecar_
    # write_failed"), so a file without one was not staged by this system and
    # its provenance is unknown. Declining to adopt sends the task down the real
    # download path, where the downloader's own target_exists guard reports a
    # visible block -- a refusal we can see beats a wrong file we cannot.
    sidecar = {}
    try:
        if metadata_path.is_file() and metadata_path.stat().st_size <= 1024 * 1024:
            sidecar = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        sidecar = {}
    if not isinstance(sidecar, dict):
        sidecar = {}
    sidecar_owner = str(sidecar.get("download_task_id") or "").strip()
    if not task_id or not sidecar_owner or sidecar_owner != task_id:
        return None
    result = {
        "ok": True,
        "status": "staged_file_ready",
        "state": "import_ready",
        "reason": "target_already_staged",
        "idempotent": True,
        "provider_id": task.get("provider_id") or task.get("provider") or task.get("source"),
        "download_task_id": task_id,
        "download_client": download_client,
        "path": str(target),
        "local_path": str(target),
        "size_bytes": size_bytes,
        "metadata_path": str(metadata_path),
    }
    # Carry the recorded hash forward so an adopted file has the same provenance
    # a freshly downloaded one does, instead of a null content_hash.
    if str(sidecar.get("content_hash") or "").strip():
        result["content_hash"] = str(sidecar["content_hash"]).strip()
    if download_client == "inkdrop_page_pack":
        result["page_pack_contract_version"] = getattr(page_pack_downloader, "CONTRACT_VERSION", 1)
    else:
        result["direct_download_contract_version"] = getattr(direct_downloader, "CONTRACT_VERSION", 1)
    return result


def _trusted_page_endpoint_from_registry(db_path, task):
    provider_id = inkdrop_sources.provider_key((task or {}).get("provider_id") or (task or {}).get("provider") or (task or {}).get("source"))
    if provider_id != "suwayomi" or not db_path:
        return ""
    try:
        rows = inkdrop_source_registry.registry_from_db(db_path, include_disabled=True)
    except Exception:
        return ""
    row = next((item for item in rows if item.get("provider_id") == "suwayomi"), {})
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    return str(policy.get("suwayomi_page_base_url") or row.get("base_url") or "").strip()


def _direct_download_registry_row(db_path, task):
    provider_id = inkdrop_sources.provider_key(
        (task or {}).get("provider_id") or (task or {}).get("provider") or (task or {}).get("source")
    )
    if not provider_id or not db_path:
        return None
    try:
        rows = inkdrop_source_registry.registry_from_db(db_path, include_disabled=True)
    except Exception:
        return None
    return next(
        (row for row in rows if inkdrop_sources.provider_key(row.get("provider_id")) == provider_id),
        None,
    )


def _stage_download_task(task, *, http_get=None, staging_root=None, trusted_page_endpoint="", registry_row=None):
    download_client = str((task or {}).get("download_client") or "").strip().lower()
    effective_staging_root = staging_root
    effective_incomplete_root = None
    if download_client == "inkdrop_direct":
        provider_id = inkdrop_sources.provider_key(
            (task or {}).get("provider_id") or (task or {}).get("provider") or (task or {}).get("source")
        )
        if registry_row is not None and not registry_row.get("enabled", True):
            return {
                "ok": False,
                "status": "blocked",
                "reason": "provider_disabled",
                "failure_reason": "provider_disabled",
                "failure_class": "policy",
                "provider_id": provider_id,
            }
        # A provider's finished/in-progress files can live under their own
        # configured download_root/incomplete_root (see inkdrop_source_providers
        # .direct_download_task_seed), not just the shared staging root -- the
        # confinement root used to stage a task must match the root it was
        # created under, or a customized provider path always misses. The two
        # roots are resolved and passed independently -- an incomplete_root on
        # a separate volume from download_root is a supported configuration,
        # not a common-ancestor fallback.
        effective_staging_root = str(
            inkdrop_source_providers.direct_download_provider_root(staging_root, provider_id, registry_row)
        )
        effective_incomplete_root = str(
            inkdrop_source_providers.direct_download_incomplete_root(effective_staging_root, registry_row)
        )
    existing = _existing_staged_file_result(task, staging_root=effective_staging_root)
    if existing:
        return existing
    if download_client == "inkdrop_page_pack":
        return page_pack_downloader.stage_page_pack(
            task,
            http_get=http_get,
            staging_root=staging_root,
            trusted_page_endpoint=trusted_page_endpoint,
        )
    if download_client == "inkdrop_direct":
        return direct_downloader.download_direct_task(
            task,
            http_get=http_get,
            staging_root=effective_staging_root,
            download_root=effective_staging_root,
            incomplete_root=effective_incomplete_root,
        )
    return {
        "ok": False,
        "status": "blocked",
        "reason": "unsupported_stage_download_client",
        "failure_reason": "unsupported_stage_download_client",
        "failure_class": "infrastructure",
        "download_client": download_client,
    }


def stage_direct_download_tasks(
    db_path,
    queue_id,
    *,
    http_get=None,
    staging_root=None,
    source_memory_db_path=None,
    dry_run=True,
    limit=20,
    now=None,
):
    tasks = direct_download_tasks_for_queue(db_path, queue_id, limit=limit)
    out = {
        "source_worker_coordinator_contract_version": CONTRACT_VERSION,
        "ok": True,
        "dry_run": bool(dry_run),
        "queue_id": queue_id,
        "stageable_download_clients": sorted(STAGEABLE_DOWNLOAD_CLIENTS),
        "tasks_available": len(tasks),
        "tasks": [
            {
                "id": task.get("id"),
                "provider_id": task.get("provider_id"),
                "download_client": task.get("download_client"),
                "status": task.get("status"),
                "state": task.get("state"),
                "local_path": task.get("local_path"),
            }
            for task in tasks
        ],
        "stage_results": [],
        "attempt_records": [],
    }
    if dry_run or not tasks:
        return out
    for task in tasks:
        download_client = str(task.get("download_client") or "").strip().lower()
        registry_row = (
            _direct_download_registry_row(db_path, task)
            if download_client == "inkdrop_direct"
            else None
        )
        result = _stage_download_task(
            task,
            http_get=http_get,
            staging_root=staging_root,
            trusted_page_endpoint=_trusted_page_endpoint_from_registry(db_path, task),
            registry_row=registry_row,
        )
        out["stage_results"].append(result)
        if source_memory_db_path:
            import inkdrop_source_suppression as suppression

            if result.get("ok"):
                suppression.clear_infrastructure_bad_direct_download_result(source_memory_db_path, task)
            else:
                suppression.record_bad_direct_download_result(source_memory_db_path, result, task, seen_at=now)
        attempt = _stage_attempt_from_task(task, result, now=now)
        recorded = inkdrop_state.record_queue_source_attempt(
            db_path,
            queue_id,
            attempt,
            started_at=attempt.get("started_at"),
            completed_at=attempt.get("completed_at"),
        )
        out["attempt_records"].append(recorded)
    out["tasks_staged"] = sum(1 for result in out["stage_results"] if result.get("ok"))
    out["tasks_failed"] = sum(1 for result in out["stage_results"] if not result.get("ok"))
    out["attempt_records_failed"] = sum(1 for record in out["attempt_records"] if not (record or {}).get("ok"))
    out["ok"] = out["tasks_failed"] == 0 and out["attempt_records_failed"] == 0
    return out


def _provider_filter_allows_persisted_pack(provider_id, provider_ids):
    provider_id = inkdrop_sources.provider_key(provider_id)
    requested = {
        inkdrop_sources.provider_key(value)
        for value in _list(provider_ids)
        if inkdrop_sources.provider_key(value)
    }
    return bool(
        not requested
        or provider_id in requested
        or ("prowlarr" in requested and provider_id.startswith("prowlarr_"))
    )


def persisted_exact_pack_replay_result(
    db_path,
    queue_id,
    wanted_item,
    *,
    provider_ids=None,
    claim=False,
    now=None,
    max_age_seconds=PERSISTED_EXACT_PACK_REPLAY_MAX_AGE_SECONDS,
):
    """Re-evaluate one recent manifest-proven pack blocked by the retired range gate."""

    now = time.time() if now is None else float(now)
    cutoff = now - max(60, int(max_age_seconds or PERSISTED_EXACT_PACK_REPLAY_MAX_AGE_SECONDS))
    try:
        registry_rows = inkdrop_source_registry.registry_from_db(db_path, include_disabled=True)
        with inkdrop_state.connect_read(db_path) as con:
            rows = con.execute(
                """
                select id, provider_id, source, status, failure_reason,
                       coalesce(completed_at, started_at, 0) as observed_at, raw_json
                from source_attempts
                where queue_id=?
                  and lower(coalesce(status, '')) in ('blocked', 'review')
                  and coalesce(completed_at, started_at, 0)>=?
                order by coalesce(completed_at, started_at, 0) desc, id desc
                limit 50
                """,
                (queue_id, cutoff),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}
    registry_by_id = {
        inkdrop_sources.provider_key(row.get("provider_id")): row
        for row in registry_rows
        if isinstance(row, dict) and row.get("provider_id")
    }
    wanted_item = _dict(wanted_item)
    for stored_row in rows:
        stored = dict(stored_row)
        payload = _json_loads(stored.get("raw_json"))
        raw = _dict(payload.get("raw"))
        candidate = _dict(raw.get("candidate")) or _dict(payload.get("candidate"))
        legacy_reasons = {
            str(reason or "").strip().lower()
            for reason in [
                stored.get("failure_reason"),
                payload.get("failure_reason"),
                payload.get("reason"),
                *(_list(payload.get("block_reasons"))),
                *(_list(candidate.get("block_reasons"))),
            ]
            if str(reason or "").strip()
        }
        if legacy_reasons != {"coverage_not_unit_number"}:
            continue
        provider_id = inkdrop_sources.provider_key(
            stored.get("provider_id") or stored.get("source") or candidate.get("provider_id")
        )
        if not provider_id.startswith("prowlarr_") or not _provider_filter_allows_persisted_pack(provider_id, provider_ids):
            continue
        registry_row = _dict(registry_by_id.get(provider_id))
        if (
            registry_row.get("registry_state") != "ready"
            or not registry_row.get("auto_search_allowed")
            or not registry_row.get("auto_download_allowed")
        ):
            continue
        if not inkdrop_source_providers.indexer_outer_work_identity_matches(
            candidate,
            wanted_item,
            policy=inkdrop_source_providers.provider_policy(registry_row),
        ):
            continue
        protocol = str(candidate.get("protocol") or payload.get("protocol") or "").strip().lower()
        info_hash = _first_text(candidate.get("info_hash"), payload.get("info_hash"), payload.get("torrent_hash"))
        magnet_url = _first_text(candidate.get("magnet_url"), candidate.get("magnetUrl"), payload.get("magnet_url"))
        download_url = inkdrop_source_providers.authorized_prowlarr_download_url(candidate, registry_row)
        if protocol != "torrent" or not (info_hash or magnet_url or download_url):
            continue
        candidate = {
            **candidate,
            "provider_id": provider_id,
            "protocol": protocol,
            "info_hash": info_hash,
            "magnet_url": magnet_url,
            "download_url": download_url or candidate.get("download_url"),
        }
        reparse_input = dict(candidate)
        reparse_input["title"] = _first_text(candidate.get("original_result_title"), candidate.get("title"))
        reparsed = inkdrop_source_providers.prowlarr_candidate_from_result(
            reparse_input,
            registry_row,
            wanted_item,
        )
        manifest = inkdrop_source_providers.indexer_manifest_pack_match(reparsed)
        if not manifest or manifest.get("coverage_source") not in {
            "pack_contents_filename",
            "pack_contents_volume_filename",
        }:
            continue
        reparsed["pack_contents_match"] = manifest
        reparsed["pack_contents_coverage_source"] = manifest.get("coverage_source")
        reparsed["pack_contents_matching_entry"] = manifest.get("entry")
        reparsed["pack_contents_entry_count"] = manifest.get("content_entry_count")
        verdict = inkdrop_candidate_matching.apply_compatibility(
            inkdrop_source_providers.indexer_candidate_verdict(reparsed, registry_row),
            wanted_item,
        )
        if download_url:
            verdict["acquisition_capability"] = "automatic"
            verdict["authorized_prowlarr_download_url"] = True
        compatibility = _dict(verdict.get("target_compatibility"))
        public = inkdrop_manual_search.normalize_candidate(
            verdict,
            wanted_item,
            search_run_id=f"persisted-pack-replay:{queue_id}",
            provider_id=provider_id,
            discovered_at=stored.get("observed_at"),
        )
        if not (
            verdict.get("candidate_safe") is True
            and verdict.get("auto_grab_verdict") == "auto_grab_safe"
            and compatibility.get("status") == "compatible"
            and "exact_pack_manifest_member" in _list(compatibility.get("positive_evidence"))
            and public.get("accepted") is True
            and public.get("acquisition_capability") == "automatic"
        ):
            continue
        attempt = inkdrop_source_providers.indexer_candidate_attempt_seed(verdict, registry_row)
        if download_url and _normalize_handoff_client(attempt.get("download_client")) not in TORRENT_HANDOFF_DOWNLOAD_CLIENTS:
            continue
        if claim and not inkdrop_state.claim_persisted_exact_pack_replay(
            db_path,
            queue_id,
            stored.get("id"),
            provider_id=provider_id,
            candidate_identity=attempt.get("candidate_identity"),
            now=now,
        ):
            continue
        attempt_raw = _dict(attempt.get("raw"))
        attempt_raw["persisted_exact_pack_replay"] = {
            "contract_version": 1,
            "source_attempt_id": stored.get("id"),
            "legacy_reason": "coverage_not_unit_number",
            "revalidated_at": now,
            "revalidated_at_iso": inkdrop_state.utc_stamp(now),
            "coverage_source": manifest.get("coverage_source"),
            "matching_entry": manifest.get("entry"),
        }
        attempt["raw"] = attempt_raw
        runtime_result = {
            "status": "sent",
            "candidate_count": 1,
            "safe_candidate_count": 1,
            "review_candidate_count": 0,
            "blocked_candidate_count": 0,
            "candidates": [reparsed],
            "verdicts": [verdict],
            "attempts": [attempt],
        }
        return {
            "provider_id": provider_id,
            "result_status": "sent",
            "reason": "persisted_exact_pack_revalidated",
            "runtime_results": [runtime_result],
            "attempts": [attempt],
            "persisted_exact_pack_replay": attempt_raw["persisted_exact_pack_replay"],
        }
    return {}


def run_source_worker_for_queue(
    db_path,
    queue_id,
    *,
    source_http_get=None,
    direct_http_get=None,
    candidate_headers_by_provider=None,
    operator_payloads=None,
    source_memory_db_path=None,
    source_memory_cooldown_seconds=None,
    staging_root=None,
    include_operator=True,
    include_blocked=False,
    provider_ids=None,
    job_limit=20,
    run_limit=None,
    dry_run=True,
    stage_direct=False,
    handoff_download_clients=False,
    download_client_adder=None,
    record_lock_retry_attempts=None,
    record_lock_retry_initial_delay=None,
    fetch_deadline=None,
    now=None,
):
    planned = source_jobs_for_queue(
        db_path,
        queue_id,
        include_operator=include_operator,
        include_blocked=include_blocked,
        provider_ids=provider_ids,
        job_limit=job_limit,
    )
    if not planned.get("ok"):
        return planned
    selected_jobs = _select_jobs(planned.get("jobs") or [], limit=run_limit)
    replay = persisted_exact_pack_replay_result(
        db_path,
        queue_id,
        planned.get("wanted_item"),
        provider_ids=provider_ids,
        claim=not dry_run,
        now=now,
    )
    results = [replay] if replay else source_jobs.run_source_jobs(
        selected_jobs,
        http_get=source_http_get,
        operator_payloads=operator_payloads,
        candidate_headers_by_provider=candidate_headers_by_provider,
        source_memory_db_path=source_memory_db_path,
        source_memory_cooldown_seconds=source_memory_cooldown_seconds,
        staging_root=staging_root,
        fetch_deadline=fetch_deadline,
        now=now,
    )
    recorded = recorder.record_source_job_results(
        db_path,
        queue_id,
        results,
        jobs_by_provider_id=_jobs_by_provider_id(selected_jobs),
        source_memory_db_path=source_memory_db_path,
        source_memory_cooldown_seconds=source_memory_cooldown_seconds,
        dry_run=dry_run,
        record_lock_retry_attempts=record_lock_retry_attempts,
        record_lock_retry_initial_delay=record_lock_retry_initial_delay,
        now=now,
    )
    direct_stage = {}
    if stage_direct:
        direct_stage = stage_direct_download_tasks(
            db_path,
            queue_id,
            http_get=direct_http_get,
            staging_root=staging_root,
            source_memory_db_path=source_memory_db_path,
            dry_run=dry_run,
            limit=job_limit,
            now=now,
        )
    download_client_handoff = {}
    if handoff_download_clients:
        download_client_handoff = handoff_download_client_tasks(
            db_path,
            queue_id,
            add_download_client=download_client_adder,
            dry_run=dry_run,
            limit=job_limit,
            now=now,
        )
    return {
        "source_worker_coordinator_contract_version": CONTRACT_VERSION,
        "ok": (
            bool(recorded.get("ok"))
            and bool((not direct_stage) or direct_stage.get("ok"))
            and bool((not download_client_handoff) or download_client_handoff.get("ok"))
        ),
        "dry_run": bool(dry_run),
        "queue_id": queue_id,
        "wanted_item": planned.get("wanted_item"),
        "jobs": selected_jobs,
        "job_summary": source_jobs.source_job_summary(selected_jobs),
        "job_results": results,
        "job_result_summary": source_jobs.source_job_result_summary(results),
        "recording": recorded,
        "direct_stage": direct_stage,
        "download_client_handoff": download_client_handoff,
    }
