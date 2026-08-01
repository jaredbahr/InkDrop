"""Persistence boundary for InkDrop source worker job results.

Source jobs and runtime evaluation are side-effect free. This module is the
small explicit bridge that records evaluated attempts through inkdrop_state.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import inkdrop_sources
import inkdrop_runtime_config
import inkdrop_source_providers as providers
import inkdrop_source_worker_runtime as runtime
import inkdrop_state


CONTRACT_VERSION = 1
STATE_DIR = inkdrop_runtime_config.state_dir()
PENDING_PACKS_LOG = inkdrop_runtime_config.pending_pack_imports_path()
PACK_HANDOFF_CONTRACT_VERSION = 1
PACK_COVERAGE_ROW_LIMIT = 5000
PACK_COVERAGE_ENTRY_LIMIT = 1000
GIB = 1024 * 1024 * 1024
PACK_VALUE_LARGE_THRESHOLD_BYTES = 15 * GIB
PACK_VALUE_VERY_LARGE_THRESHOLD_BYTES = 50 * GIB
PACK_VALUE_MAX_BYTES_PER_COVERED_ITEM = 16 * GIB
PACK_VALUE_YEARLY_MAX_BYTES_PER_COVERED_ITEM = 12 * GIB
PACK_VALUE_LARGE_MIN_COVERED_ITEMS = 2
PACK_VALUE_TORRENT_LARGE_MIN_COVERED_ITEMS = 3
PACK_VALUE_YEARLY_MIN_COVERED_ITEMS = 5
DEFAULT_RECORD_LOCK_RETRY_ATTEMPTS = 6
DEFAULT_RECORD_LOCK_RETRY_INITIAL_DELAY_SECONDS = 0.5
DIAGNOSTIC_RAW_KEYS = ("fetch", "no_candidate_evidence")
DIAGNOSTIC_ATTEMPT_STATUSES = {
    "provider_wait",
    "provider_unavailable",
    "searched_no_candidates",
    "blocked",
    "observed",
}


def _dict(value):
    return dict(value) if isinstance(value, dict) else {}


def _attempts_from_runtime_results(runtime_results):
    attempts = []
    for row in runtime_results or []:
        if not isinstance(row, dict):
            continue
        attempts.extend(attempt for attempt in (row.get("attempts") or []) if isinstance(attempt, dict))
    return attempts


def _result_status(runtime_results, attempts):
    summary = runtime.runtime_summary(runtime_results or [])
    by_status = summary.get("by_status") if isinstance(summary.get("by_status"), dict) else {}
    for status in ("sent", "review", "provider_wait", "provider_unavailable", "blocked", "searched_no_candidates", "observed"):
        if int(by_status.get(status) or 0):
            return "provider_wait" if status == "provider_unavailable" else status
    statuses = {str((attempt or {}).get("status") or "").strip().lower() for attempt in attempts or []}
    for status in ("sent", "review", "provider_wait", "provider_unavailable", "blocked", "searched_no_candidates", "observed"):
        if status in statuses:
            return "provider_wait" if status == "provider_unavailable" else status
    return "unknown"


def _queue_count(db_path, queue_id, table):
    with inkdrop_state.connect_read(db_path) as con:
        if not inkdrop_state.table_exists(con, table):
            return 0
        row = con.execute(f"select count(*) as count from {table} where queue_id=?", (queue_id,)).fetchone()
        return int(row["count"] or 0) if row else 0


def _queue_row(db_path, queue_id):
    row = inkdrop_state.queue_item(db_path, queue_id, read_only=True, timeout_seconds=5.0, busy_timeout_ms=5000)
    return row if isinstance(row, dict) else {}


def _clone_result(job_result):
    result = dict(job_result or {})
    result["runtime_results"] = [
        dict(row) if isinstance(row, dict) else row
        for row in (result.get("runtime_results") or [])
    ]
    result["attempts"] = [
        dict(row) if isinstance(row, dict) else row
        for row in (result.get("attempts") or [])
    ]
    return result


def _first_value(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _bounded_int(value, default, *, minimum=1, maximum=20):
    try:
        out = int(value)
    except Exception:
        out = int(default)
    return max(int(minimum), min(int(maximum), out))


def _bounded_float(value, default, *, minimum=0.1, maximum=30.0):
    try:
        out = float(value)
    except Exception:
        out = float(default)
    return max(float(minimum), min(float(maximum), out))


def _record_queue_source_attempt_with_lock_retry(
    db_path,
    queue_id,
    attempt,
    *,
    started_at=None,
    completed_at=None,
    attempts=None,
    initial_delay=None,
):
    max_attempts = _bounded_int(attempts, DEFAULT_RECORD_LOCK_RETRY_ATTEMPTS, minimum=1, maximum=20)
    delay = _bounded_float(
        initial_delay,
        DEFAULT_RECORD_LOCK_RETRY_INITIAL_DELAY_SECONDS,
        minimum=0.1,
        maximum=30.0,
    )
    metrics = {
        "attempts": 0,
        "retries": 0,
        "max_attempts": max_attempts,
        "initial_delay_seconds": delay,
    }
    for index in range(max_attempts):
        metrics["attempts"] = index + 1
        try:
            recorded = inkdrop_state.record_queue_source_attempt(
                db_path,
                queue_id,
                attempt,
                started_at=started_at,
                completed_at=completed_at,
            )
            return recorded, metrics
        except Exception as exc:
            if not inkdrop_state.is_database_locked_error(exc) or index >= max_attempts - 1:
                raise
            metrics["retries"] += 1
            metrics["last_retry_error"] = str(exc)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    return {"ok": False, "reason": "record_lock_retry_exhausted", "queue_id": queue_id}, metrics


def _int_value(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _bool_value(value, default=False):
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _job_policy(job):
    job = _dict(job)
    row = _dict(job.get("registry_row"))
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    return policy


def _policy_int(policy, keys, default):
    policy = policy if isinstance(policy, dict) else {}
    for key in keys:
        if key in policy and policy.get(key) not in (None, ""):
            return _int_value(policy.get(key), default)
    return default


def _raw_candidate(attempt):
    attempt = _dict(attempt)
    raw = _dict(attempt.get("raw"))
    candidate = _dict(raw.get("candidate"))
    if not candidate:
        candidate = _dict(attempt.get("candidate"))
    return candidate


def _attempt_diagnostic_key(attempt):
    attempt = _dict(attempt)
    candidate = _raw_candidate(attempt)
    return (
        inkdrop_sources.provider_key(_first_value(attempt.get("provider_id"), attempt.get("source"), attempt.get("provider"))),
        str(attempt.get("status") or "").strip().lower(),
        providers.normalized_query(_first_value(attempt.get("query"), attempt.get("title"))),
        str(_first_value(attempt.get("reason"), attempt.get("failure_reason")) or "").strip().lower(),
        str(
            _first_value(
                attempt.get("candidate_identity"),
                attempt.get("download_url_hash"),
                attempt.get("external_id"),
                candidate.get("candidate_identity"),
                candidate.get("download_url_hash"),
                candidate.get("external_id"),
            )
            or ""
        ).strip(),
    )


def _has_diagnostic_raw(attempt):
    raw = _dict(_dict(attempt).get("raw"))
    return any(raw.get(key) not in (None, "", [], {}) for key in DIAGNOSTIC_RAW_KEYS)


def _merge_diagnostic_raw(attempt, source_attempt):
    source_raw = _dict(_dict(source_attempt).get("raw"))
    diagnostics = {
        key: source_raw.get(key)
        for key in DIAGNOSTIC_RAW_KEYS
        if source_raw.get(key) not in (None, "", [], {})
    }
    if not diagnostics:
        return attempt
    out = dict(attempt or {})
    raw = dict(out.get("raw") or {}) if isinstance(out.get("raw"), dict) else {}
    changed = False
    for key, value in diagnostics.items():
        if raw.get(key) in (None, "", [], {}):
            raw[key] = value
            changed = True
    if changed:
        out["raw"] = raw
    return out


def _attempts_with_diagnostic_raw(attempts, diagnostic_attempts):
    attempts = [attempt for attempt in attempts or [] if isinstance(attempt, dict)]
    diagnostics = [
        (index, attempt)
        for index, attempt in enumerate(diagnostic_attempts or [])
        if isinstance(attempt, dict) and _has_diagnostic_raw(attempt)
    ]
    if not attempts or not diagnostics:
        return attempts

    by_key = {}
    for index, attempt in diagnostics:
        by_key.setdefault(_attempt_diagnostic_key(attempt), []).append((index, attempt))

    used = set()
    out = []
    for index, attempt in enumerate(attempts):
        key = _attempt_diagnostic_key(attempt)
        candidates = [row for row in by_key.get(key, []) if row[0] not in used]
        source = None
        if len(candidates) == 1:
            source = candidates[0]
        elif index < len(diagnostic_attempts):
            indexed_source = diagnostic_attempts[index]
            if isinstance(indexed_source, dict) and _has_diagnostic_raw(indexed_source):
                indexed_key = _attempt_diagnostic_key(indexed_source)
                if indexed_key == key or indexed_key[:4] == key[:4]:
                    source = (index, indexed_source)
        if source:
            used.add(source[0])
            out.append(_merge_diagnostic_raw(attempt, source[1]))
        else:
            out.append(attempt)
    return out


def _diagnostic_fallback_attempts(diagnostic_attempts):
    attempts = []
    for attempt in diagnostic_attempts or []:
        if not isinstance(attempt, dict):
            continue
        status = str(attempt.get("status") or "").strip().lower()
        if status in DIAGNOSTIC_ATTEMPT_STATUSES:
            attempts.append(dict(attempt))
    return attempts


def _attempts_with_diagnostic_fallback(attempts, diagnostic_attempts):
    merged = _attempts_with_diagnostic_raw(attempts, diagnostic_attempts)
    if merged:
        return merged
    return _diagnostic_fallback_attempts(diagnostic_attempts)


def _attempt_pack_match(attempt):
    attempt = _dict(attempt)
    candidate = _raw_candidate(attempt)
    match = _dict(candidate.get("pack_contents_match"))
    if not match:
        match = _dict(attempt.get("pack_contents_match"))
    coverage_source = _first_value(
        attempt.get("pack_contents_coverage_source"),
        candidate.get("pack_contents_coverage_source"),
        match.get("coverage_source"),
    )
    if coverage_source not in providers.PACK_CONTENTS_SAFE_COVERAGE_SOURCES:
        return {}
    if not match:
        match = {"coverage_source": coverage_source}
    match.setdefault("coverage_source", coverage_source)
    matching_entry = _first_value(
        attempt.get("pack_contents_matching_entry"),
        candidate.get("pack_contents_matching_entry"),
        match.get("entry"),
    )
    if matching_entry:
        match.setdefault("entry", matching_entry)
        match.setdefault("matching_entry", matching_entry)
    entry_count = _first_value(
        attempt.get("pack_contents_entry_count"),
        candidate.get("pack_contents_entry_count"),
        match.get("content_entry_count"),
    )
    if entry_count not in (None, ""):
        match.setdefault("content_entry_count", entry_count)
    return match


def _table_columns(con, table):
    try:
        return {row["name"] for row in con.execute(f"pragma table_info({table})")}
    except Exception:
        return set()


def _select_column(alias, column, output, columns):
    if column in columns:
        return f"{alias}.{column} as {output}"
    return f"null as {output}"


def _active_pack_queue_rows(db_path, limit=PACK_COVERAGE_ROW_LIMIT):
    if not db_path:
        return []
    try:
        row_limit = max(1, min(int(limit or PACK_COVERAGE_ROW_LIMIT), PACK_COVERAGE_ROW_LIMIT))
    except Exception:
        row_limit = PACK_COVERAGE_ROW_LIMIT
    terminal_states = (
        "verified",
        "satisfied",
        "superseded_duplicate",
        "ignored",
        "removed",
        "inactive",
    )
    try:
        with inkdrop_state.connect_read(db_path) as con:
            if not (
                inkdrop_state.table_exists(con, "queue_items")
                and inkdrop_state.table_exists(con, "series")
                and inkdrop_state.table_exists(con, "issues")
            ):
                return []
            qcols = _table_columns(con, "queue_items")
            scols = _table_columns(con, "series")
            icols = _table_columns(con, "issues")
            wcols = _table_columns(con, "wanted_items") if inkdrop_state.table_exists(con, "wanted_items") else set()
            select_parts = [
                "q.id as id",
                _select_column("q", "wanted_id", "wanted_id", qcols),
                "q.series_id as series_id",
                "q.issue_id as issue_id",
                _select_column("q", "state", "state", qcols),
                _select_column("q", "active", "active", qcols),
                "s.title as series",
                _select_column("s", "title", "title", scols),
                _select_column("s", "media_type", "media_type", scols),
                _select_column("s", "publisher", "publisher", scols),
                _select_column("s", "metadata_provider", "metadata_provider", scols),
                _select_column("s", "metadata_id", "metadata_id", scols),
                _select_column("s", "kapowarr_id", "kapowarr_id", scols),
                _select_column("i", "issue_number", "issue_number", icols),
                _select_column("i", "normalized_number", "normalized_number", icols),
                _select_column("i", "title", "issue_title", icols),
                _select_column("i", "metadata_provider", "issue_metadata_provider", icols),
                _select_column("i", "metadata_id", "issue_metadata_id", icols),
                _select_column("i", "kapowarr_issue_id", "kapowarr_issue_id", icols),
                _select_column("w", "status", "wanted_status", wcols) if wcols else "null as wanted_status",
            ]
            rows = con.execute(
                f"""
                select {', '.join(select_parts)}
                from queue_items q
                join series s on s.id = q.series_id
                join issues i on i.id = q.issue_id
                left join wanted_items w on w.id = q.wanted_id
                where coalesce(q.active, 1) = 1
                  and lower(coalesce(q.state, 'queued')) not in ({','.join('?' for _ in terminal_states)})
                  and coalesce(i.issue_number, '') != ''
                order by coalesce(q.updated_at, q.created_at, 0) desc
                limit ?
                """,
                (*terminal_states, row_limit),
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _manifest_candidate_for_queue_row(row):
    row = _dict(row)
    provider = _first_value(row.get("issue_metadata_provider"), row.get("metadata_provider"))
    metadata_id = _first_value(row.get("issue_metadata_id"), row.get("metadata_id"))
    return {
        "series_title": row.get("series") or row.get("title"),
        "series": row.get("series") or row.get("title"),
        "issue_number": row.get("issue_number") or row.get("normalized_number"),
        "normalized_number": row.get("normalized_number"),
        "issue_title": row.get("issue_title"),
        "metadata_provider": provider,
        "metadata_id": metadata_id,
        "series_source": row.get("metadata_provider"),
        "media_type": row.get("media_type"),
        "publisher": row.get("publisher"),
    }


def _manifest_pack_queue_coverage(db_path, queue, attempt, pack_match):
    candidate = _raw_candidate(attempt)
    entries = providers.indexer_pack_manifest_entries(candidate, limit=PACK_COVERAGE_ENTRY_LIMIT)
    if not entries:
        return {}
    samples = []
    covered_queue_ids = []
    covered_series = []
    seen_queues = set()
    seen_series = set()
    for row in _active_pack_queue_rows(db_path):
        queue_id = str(row.get("id") or "").strip()
        if not queue_id or queue_id in seen_queues:
            continue
        probe = _manifest_candidate_for_queue_row(row)
        if not probe.get("series_title") or not probe.get("issue_number"):
            continue
        match = None
        for entry in entries:
            match = providers.indexer_manifest_entry_matches_candidate(probe, entry)
            if match:
                break
        if not match:
            continue
        match_for_sample = {**_dict(pack_match), **match}
        if match.get("entry"):
            match_for_sample["matching_entry"] = match.get("entry")
            match_for_sample["file_entry"] = match.get("file_entry") or match.get("entry")
        sample = _queue_pack_sample(row, attempt, match_for_sample)
        sample["presence"] = "inkdrop_wanted"
        if row.get("wanted_status"):
            sample["wanted_status"] = row.get("wanted_status")
        if row.get("state"):
            sample["queue_state"] = row.get("state")
        samples.append(sample)
        seen_queues.add(queue_id)
        covered_queue_ids.append(queue_id)
        series = str(sample.get("series") or row.get("series") or "").strip()
        series_key = series.lower()
        if series and series_key not in seen_series:
            seen_series.add(series_key)
            covered_series.append(series)

    if not samples:
        return {}
    return {
        "content_entry_count": len(entries),
        "useful_missing_sample": samples,
        "useful_missing_count": len(samples),
        "covered_queue_ids": covered_queue_ids,
        "covered_series": covered_series,
        "multi_series": len(covered_series) > 1,
        "coverage_source": "pack_contents_filename",
    }


def _pack_title_class(title):
    text = str(title or "").strip().lower()
    if not text:
        return "pack"
    if (
        "weekly" in text
        and re_search(r"\b20\d{2}[\W_]+(?:0[1-9]|1[0-2])[\W_]+(?:0[1-9]|[12]\d|3[01])\b", text)
    ):
        return "dated_weekly"
    if re_search(r"\b20\d{2}[\W_-]+(?:0[1-9]|1[0-2])[\W_-]+(?:0[1-9]|[12]\d|3[01])[\W_]+weekly", text):
        return "dated_weekly"
    if re_search(r"\bweekly[\W_]+(?:comics?|releases?)[\W_]+20\d{2}\b", text):
        return "yearly_weekly_release"
    if re_search(r"\b(?:dc|marvel|image|dark[\W_]+horse|idw|boom)(?:[\W_]+comics?)?[\W_]+weekly[\W_]+releases[\W_]+20\d{2}\b", text):
        return "yearly_weekly_release"
    if re_search(r"\b(?:dc|marvel|image|dark[\W_]+horse|idw|boom)[\W_]+comics[\W_]+20\d{2}\b", text):
        return "yearly_publisher_pack"
    if re_search(r"\bcomplete\b.*\b(?:dc|marvel|image|dark[\W_]+horse|idw|boom)\b.*\b20\d{2}\b", text):
        return "yearly_publisher_pack"
    if "weekly" in text:
        return "weekly_pack"
    return "pack"


def re_search(pattern, text):
    import re

    return re.search(pattern, str(text or ""), re.I)


def _sample_coverage_identity(row):
    row = row if isinstance(row, dict) else {}
    for parts in (
        (row.get("series_id"), row.get("issue_id")),
        (row.get("metadata_provider"), row.get("metadata_id"), row.get("issue")),
        (row.get("series"), row.get("issue")),
    ):
        values = [str(part or "").strip().lower() for part in parts if str(part or "").strip()]
        if len(values) == len(parts):
            return "|".join(values)
    return str(row.get("queue_id") or "").strip().lower()


def _pack_unique_coverage_count(pack_match):
    pack_match = pack_match if isinstance(pack_match, dict) else {}
    sample = pack_match.get("useful_missing_sample") if isinstance(pack_match.get("useful_missing_sample"), list) else []
    identities = {_sample_coverage_identity(row) for row in sample if isinstance(row, dict)}
    identities.discard("")
    if identities:
        return len(identities)
    return _int_value(pack_match.get("useful_missing_count"), 0)


def _attempt_size_bytes(attempt):
    attempt = _dict(attempt)
    candidate = _raw_candidate(attempt)
    return _int_value(
        _first_value(
            attempt.get("size_bytes"),
            attempt.get("size"),
            candidate.get("size_bytes"),
            candidate.get("size"),
            candidate.get("sizeBytes"),
        ),
        0,
    )


def _pack_value_decision(attempt, pack_match, job=None):
    attempt = _dict(attempt)
    policy = _job_policy(job)
    if not _bool_value(
        _first_value(policy.get("pack_value_policy_enabled"), policy.get("pack_value_enabled")),
        True,
    ):
        return {"allowed": True, "reason": "pack_value_policy_disabled"}

    title = _first_value(attempt.get("title"), _raw_candidate(attempt).get("title"))
    protocol = str(_first_value(attempt.get("protocol"), _raw_candidate(attempt).get("protocol")) or "").strip().lower()
    pack_class = _pack_title_class(title)
    size_bytes = _attempt_size_bytes(attempt)
    coverage_count = _int_value((pack_match or {}).get("useful_missing_count"), 0)
    unique_coverage_count = _pack_unique_coverage_count(pack_match)
    effective_count = max(unique_coverage_count, coverage_count)
    covered_series = (pack_match or {}).get("covered_series") if isinstance((pack_match or {}).get("covered_series"), list) else []
    covered_series_count = len({str(value or "").strip().lower() for value in covered_series if str(value or "").strip()})
    min_covered = 1
    large_threshold = _policy_int(
        policy,
        ("pack_value_large_threshold_bytes", "large_pack_threshold_bytes", "large_pack_min_size_bytes"),
        PACK_VALUE_LARGE_THRESHOLD_BYTES,
    )
    very_large_threshold = _policy_int(
        policy,
        ("pack_value_very_large_threshold_bytes", "very_large_pack_threshold_bytes"),
        PACK_VALUE_VERY_LARGE_THRESHOLD_BYTES,
    )
    if size_bytes and size_bytes >= large_threshold:
        min_covered = max(
            min_covered,
            _policy_int(
                policy,
                ("large_pack_min_covered_items", "pack_value_large_min_covered_items"),
                PACK_VALUE_LARGE_MIN_COVERED_ITEMS,
            ),
        )
    if size_bytes and size_bytes >= very_large_threshold:
        min_covered = max(
            min_covered,
            _policy_int(
                policy,
                ("very_large_pack_min_covered_items", "pack_value_very_large_min_covered_items"),
                PACK_VALUE_YEARLY_MIN_COVERED_ITEMS,
            ),
        )
    if protocol == "torrent" and size_bytes and size_bytes >= large_threshold:
        min_covered = max(
            min_covered,
            _policy_int(
                policy,
                ("torrent_large_pack_min_covered_items", "pack_value_torrent_large_min_covered_items"),
                PACK_VALUE_TORRENT_LARGE_MIN_COVERED_ITEMS,
            ),
        )
    if pack_class in {"yearly_weekly_release", "yearly_publisher_pack"}:
        min_covered = max(
            min_covered,
            _policy_int(
                policy,
                ("yearly_pack_min_covered_items", "pack_value_yearly_min_covered_items"),
                PACK_VALUE_YEARLY_MIN_COVERED_ITEMS,
            ),
        )

    default_max_bpi = (
        PACK_VALUE_YEARLY_MAX_BYTES_PER_COVERED_ITEM
        if pack_class in {"yearly_weekly_release", "yearly_publisher_pack"}
        else PACK_VALUE_MAX_BYTES_PER_COVERED_ITEM
    )
    max_bytes_per_item = _policy_int(
        policy,
        ("pack_value_max_bytes_per_covered_item", "max_pack_bytes_per_covered_item"),
        default_max_bpi,
    )
    if pack_class in {"yearly_weekly_release", "yearly_publisher_pack"}:
        max_bytes_per_item = _policy_int(
            policy,
            ("yearly_pack_max_bytes_per_covered_item", "pack_value_yearly_max_bytes_per_covered_item"),
            max_bytes_per_item,
        )

    bytes_per_item = int(size_bytes / effective_count) if size_bytes and effective_count else 0
    allowed = True
    reason = "pack_value_accepted"
    if effective_count < min_covered:
        allowed = False
        reason = "pack_value_below_min_coverage"
    elif max_bytes_per_item and bytes_per_item and bytes_per_item > max_bytes_per_item:
        allowed = False
        reason = "pack_value_bytes_per_item_too_high"

    if protocol == "usenet":
        protocol_bonus = 20
    elif protocol == "torrent":
        protocol_bonus = 0
    else:
        protocol_bonus = 5
    class_bonus = {
        "dated_weekly": 30,
        "weekly_pack": 20,
        "yearly_weekly_release": 8,
        "yearly_publisher_pack": 5,
    }.get(pack_class, 10)
    score = (effective_count * 100) + protocol_bonus + class_bonus
    if bytes_per_item:
        score -= min(80, int(bytes_per_item / GIB))
    return {
        "allowed": allowed,
        "reason": reason,
        "pack_class": pack_class,
        "protocol": protocol,
        "size_bytes": size_bytes,
        "coverage_count": coverage_count,
        "unique_coverage_count": unique_coverage_count,
        "effective_coverage_count": effective_count,
        "covered_series_count": covered_series_count,
        "min_covered_items": min_covered,
        "bytes_per_covered_item": bytes_per_item,
        "max_bytes_per_covered_item": max_bytes_per_item,
        "score": score,
        "prefer_download_client": "sabnzbd" if protocol == "usenet" else "qbittorrent" if protocol == "torrent" else "",
    }


def _pack_value_summary(decision):
    decision = decision if isinstance(decision, dict) else {}
    keys = (
        "allowed",
        "reason",
        "pack_class",
        "protocol",
        "size_bytes",
        "coverage_count",
        "unique_coverage_count",
        "effective_coverage_count",
        "covered_series_count",
        "min_covered_items",
        "bytes_per_covered_item",
        "max_bytes_per_covered_item",
        "score",
        "prefer_download_client",
    )
    return {key: decision.get(key) for key in keys if decision.get(key) not in (None, "", [], {})}


def _attempt_with_pack_value(attempt, pack_match, decision):
    enriched = dict(attempt or {})
    summary = _pack_value_summary(decision)
    enriched["pack_value_status"] = "accepted" if decision.get("allowed") else "blocked"
    enriched["pack_value_reason"] = decision.get("reason")
    enriched["pack_value_score"] = decision.get("score")
    enriched["pack_coverage_count"] = decision.get("effective_coverage_count")
    raw = dict(enriched.get("raw") or {}) if isinstance(enriched.get("raw"), dict) else {}
    raw["pack_value"] = summary
    raw["pack_match_summary"] = {
        "coverage_source": (pack_match or {}).get("coverage_source"),
        "useful_missing_count": (pack_match or {}).get("useful_missing_count"),
        "covered_queue_ids": list((pack_match or {}).get("covered_queue_ids") or [])[:50],
        "covered_series": list((pack_match or {}).get("covered_series") or [])[:50],
        "multi_series": bool((pack_match or {}).get("multi_series")),
    }
    enriched["raw"] = raw
    if decision.get("allowed"):
        return enriched
    reason = decision.get("reason") or "pack_value_below_threshold"
    enriched["status"] = "blocked"
    enriched["reason"] = reason
    enriched["failure_reason"] = reason
    enriched["retry_eligible"] = True
    enriched["candidate_safe"] = False
    enriched["auto_grab_verdict"] = "blocked"
    enriched["quality_status"] = "rejected"
    enriched["review_reason"] = reason
    for key in ("download_client", "external_id", "download_id", "save_path", "category"):
        enriched.pop(key, None)
    raw_candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
    if raw_candidate:
        raw_candidate["candidate_safe"] = False
        raw_candidate["auto_grab_verdict"] = "blocked"
        raw_candidate["review_reason"] = reason
        raw_candidate.setdefault("block_reasons", [])
        if reason not in raw_candidate["block_reasons"]:
            raw_candidate["block_reasons"].append(reason)
        raw_candidate["quality_status"] = "rejected"
        raw["candidate"] = raw_candidate
    return enriched


def _apply_pack_value_policy(db_path, queue, attempts, job=None):
    out = []
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("status") or "").strip().lower() != "sent":
            out.append(attempt)
            continue
        pack_match = _source_worker_pack_match(queue, attempt, db_path=db_path)
        if not pack_match:
            out.append(attempt)
            continue
        decision = _pack_value_decision(attempt, pack_match, job=job)
        out.append(_attempt_with_pack_value(attempt, pack_match, decision))
    return out


def _queue_pack_sample(queue, attempt, pack_match):
    queue = _dict(queue)
    attempt = _dict(attempt)
    pack_match = _dict(pack_match)
    issue_number = _first_value(queue.get("issue_number"), attempt.get("issue_number"), pack_match.get("issue_number"))
    sample = {
        "series": _first_value(queue.get("series"), attempt.get("query")),
        "series_id": queue.get("series_id"),
        "native_series_id": queue.get("series_id"),
        "issue_id": queue.get("issue_id"),
        "native_issue_id": queue.get("issue_id"),
        "wanted_id": queue.get("wanted_id"),
        "queue_id": queue.get("id"),
        "issue": issue_number,
        "issue_number": issue_number,
        "calculated": _first_value(pack_match.get("calculated"), issue_number),
        "presence": "missing",
        "metadata_provider": _first_value(queue.get("issue_metadata_provider"), queue.get("metadata_provider")),
        "metadata_id": _first_value(queue.get("issue_metadata_id"), queue.get("metadata_id")),
        "kapowarr_id": queue.get("kapowarr_id"),
        "volume_id": queue.get("kapowarr_id"),
        "matching_entry": _first_value(pack_match.get("matching_entry"), pack_match.get("entry")),
        "file_entry": _first_value(pack_match.get("file_entry"), pack_match.get("entry")),
        "source": "source_worker_manifest_pack",
    }
    return {key: value for key, value in sample.items() if value not in (None, "", [], {})}


def _source_worker_pack_match(queue, attempt, db_path=None):
    pack_match = _attempt_pack_match(attempt)
    if not pack_match:
        return {}
    pack_match = dict(pack_match)
    coverage = _manifest_pack_queue_coverage(db_path, queue, attempt, pack_match)
    if coverage:
        pack_match.update(coverage)
    sample = pack_match.get("useful_missing_sample") if isinstance(pack_match.get("useful_missing_sample"), list) else []
    if not sample:
        sample = [_queue_pack_sample(queue, attempt, pack_match)]
    pack_match["useful_missing_sample"] = [row for row in sample if isinstance(row, dict) and row]
    if not pack_match.get("useful_missing_count"):
        pack_match["useful_missing_count"] = len(pack_match["useful_missing_sample"]) or 1
    if not pack_match.get("covered_series"):
        series = _first_value(queue.get("series"), attempt.get("query"), pack_match.get("series_title"))
        pack_match["covered_series"] = [series] if series else []
    if not pack_match.get("covered_queue_ids") and queue.get("id"):
        pack_match["covered_queue_ids"] = [queue.get("id")]
    pack_match.setdefault("multi_series", False)
    return pack_match


def _candidate_summary(attempt):
    attempt = _dict(attempt)
    candidate = _raw_candidate(attempt)
    summary = {
        "title": _first_value(candidate.get("title"), attempt.get("title")),
        "indexer": _first_value(candidate.get("indexer"), attempt.get("indexer"), attempt.get("provider")),
        "indexerId": _first_value(candidate.get("indexer_id"), candidate.get("indexerId"), attempt.get("indexer_id")),
        "protocol": _first_value(candidate.get("protocol"), attempt.get("protocol")),
        "seeders": _first_value(candidate.get("seeders"), attempt.get("seeders")),
        "size": _first_value(candidate.get("size_bytes"), candidate.get("size"), attempt.get("size_bytes")),
        "download_client": attempt.get("download_client"),
        "download_id": attempt.get("download_id"),
        "external_id": attempt.get("external_id"),
        "candidate_identity": attempt.get("candidate_identity"),
        "category": attempt.get("category"),
        "pack_contents_coverage_source": _first_value(
            attempt.get("pack_contents_coverage_source"),
            candidate.get("pack_contents_coverage_source"),
        ),
    }
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def _sent_attempt(attempt):
    return str((attempt or {}).get("status") or "").strip().lower() == "sent"


def _queue_auto_send_attempt_summary(attempt):
    attempt = _dict(attempt)
    candidate = _raw_candidate(attempt)
    summary = {
        "candidate_identity": _first_value(
            attempt.get("candidate_identity"),
            attempt.get("external_id"),
            attempt.get("download_id"),
            attempt.get("download_url_hash"),
        ),
        "title": _first_value(attempt.get("title"), candidate.get("title")),
        "provider_id": attempt.get("provider_id"),
        "provider": _first_value(attempt.get("provider"), attempt.get("indexer"), candidate.get("indexer")),
        "download_client": attempt.get("download_client"),
        "match_confidence": attempt.get("match_confidence"),
        "quality_profile": _first_value(attempt.get("quality_profile"), attempt.get("quality"), candidate.get("quality")),
        "protocol": _first_value(attempt.get("protocol"), candidate.get("protocol")),
        "seeders": _first_value(attempt.get("seeders"), candidate.get("seeders")),
    }
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def _queue_auto_send_selection_payload(selection, *, selected=False, suppressed=False):
    selection = selection if isinstance(selection, dict) else {}
    payload = {
        "queue_auto_send_selection_contract_version": CONTRACT_VERSION,
        "scope": selection.get("scope") or "queue",
        "applied": bool(selection.get("applied")),
        "selected": selection.get("selected") or {},
        "suppressed_sent_candidate_count": int(selection.get("suppressed_sent_candidate_count") or 0),
    }
    if selected:
        payload["selected_attempt"] = True
    if suppressed:
        payload["suppressed_attempt"] = True
        payload["reason"] = "queue_auto_send_selection_suppressed"
    return payload


def _mark_queue_auto_send_selected(attempt, selection):
    out = dict(attempt or {})
    raw = dict(out.get("raw") or {}) if isinstance(out.get("raw"), dict) else {}
    raw["queue_auto_send_selection"] = _queue_auto_send_selection_payload(selection, selected=True)
    out["raw"] = raw
    return out


def _queue_auto_send_suppressed_attempt(attempt, selection):
    out = dict(attempt or {})
    raw = dict(out.get("raw") or {}) if isinstance(out.get("raw"), dict) else {}
    raw["queue_auto_send_selection"] = _queue_auto_send_selection_payload(selection, suppressed=True)
    out["raw"] = raw
    out["status"] = "observed"
    out["lifecycle_phase"] = "observed"
    out["outcome"] = "neutral"
    out["display_phase"] = "observed"
    out["reason"] = "queue_auto_send_selection_suppressed"
    out["failure_reason"] = ""
    out["retry_eligible"] = False
    out["auto_grab_verdict"] = "observed"
    for key in (
        "download_client",
        "downloadClient",
        "client",
        "download_id",
        "external_id",
        "category",
        "save_path",
        "local_path",
        "download_path",
        "partial_path",
    ):
        out.pop(key, None)
    return out


def _attempts_with_sent_last(attempts):
    indexed = [(index, attempt) for index, attempt in enumerate(attempts or []) if isinstance(attempt, dict)]
    indexed.sort(key=lambda row: (1 if _sent_attempt(row[1]) else 0, row[0]))
    return [attempt for _, attempt in indexed]


def _prepare_recording_result_for_queue(
    db_path,
    queue,
    job_result,
    *,
    job=None,
    source_memory_db_path=None,
    source_memory_cooldown_seconds=None,
    now=None,
):
    prepared = prepare_source_job_result_for_recording(
        job_result,
        job=job,
        source_memory_db_path=source_memory_db_path,
        source_memory_cooldown_seconds=source_memory_cooldown_seconds,
        now=now,
    )
    attempts = list(prepared.get("attempts") or [])
    attempts = _apply_pack_value_policy(db_path, queue, attempts, job=job)
    prepared["attempts"] = attempts
    prepared["result_status"] = _result_status([], attempts)
    prepared["recordable_attempt_count"] = len(attempts)
    return prepared


def _apply_queue_auto_send_selection(prepared_results, queue):
    rows = [dict(row or {}) for row in prepared_results or []]
    sent_refs = []
    ordinal = 0
    for result_index, result in enumerate(rows):
        attempts = [dict(attempt) for attempt in (result.get("attempts") or []) if isinstance(attempt, dict)]
        result["attempts"] = attempts
        for attempt_index, attempt in enumerate(attempts):
            if _sent_attempt(attempt):
                sent_refs.append((result_index, attempt_index, attempt, ordinal))
                ordinal += 1

    selection = {
        "queue_auto_send_selection_contract_version": CONTRACT_VERSION,
        "scope": "queue",
        "applied": False,
        "sent_candidate_count": len(sent_refs),
    }
    if len(sent_refs) <= 1:
        for result in rows:
            result["attempts"] = _attempts_with_sent_last(result.get("attempts") or [])
        return rows, selection

    selected = min(sent_refs, key=lambda ref: runtime._auto_send_rank(ref[2], queue, queue, ref[3]))
    selected_key = (selected[0], selected[1])
    selected_summary = _queue_auto_send_attempt_summary(selected[2])
    suppressed = []
    per_result_suppressed = {}
    for result_index, result in enumerate(rows):
        attempts = []
        for attempt_index, attempt in enumerate(result.get("attempts") or []):
            if not _sent_attempt(attempt):
                attempts.append(attempt)
                continue
            if (result_index, attempt_index) == selected_key:
                attempts.append(
                    _mark_queue_auto_send_selected(
                        attempt,
                        {
                            **selection,
                            "applied": True,
                            "selected": selected_summary,
                            "suppressed_sent_candidate_count": len(sent_refs) - 1,
                        },
                    )
                )
                continue
            summary = _queue_auto_send_attempt_summary(attempt)
            suppressed.append(summary)
            per_result_suppressed.setdefault(result_index, []).append(summary)
            attempts.append(
                _queue_auto_send_suppressed_attempt(
                    attempt,
                    {
                        **selection,
                        "applied": True,
                        "selected": selected_summary,
                        "suppressed_sent_candidate_count": len(sent_refs) - 1,
                    },
                )
            )
        result["attempts"] = _attempts_with_sent_last(attempts)
        result["result_status"] = _result_status([], result["attempts"])
        result["recordable_attempt_count"] = len(result["attempts"])

    selection.update(
        {
            "applied": True,
            "selected": selected_summary,
            "selected_result_index": selected_key[0],
            "selected_attempt_index": selected_key[1],
            "suppressed_sent_candidate_count": len(suppressed),
            "suppressed": suppressed[:10],
        }
    )
    for result_index, result in enumerate(rows):
        if result_index == selected_key[0] or per_result_suppressed.get(result_index):
            result["queue_auto_send_selection"] = {
                **selection,
                "selected": selected_summary if result_index == selected_key[0] else {},
                "suppressed": per_result_suppressed.get(result_index, [])[:10],
                "suppressed_sent_candidate_count": len(per_result_suppressed.get(result_index, [])),
            }

    selected_row = rows[selected_key[0]]
    ordered = [row for index, row in enumerate(rows) if index != selected_key[0]]
    ordered.append(selected_row)
    return ordered, selection


def source_worker_pack_pending_record(queue, attempt, recorded=None, job=None, now=None, db_path=None):
    queue = _dict(queue)
    attempt = _dict(attempt)
    recorded = _dict(recorded)
    job = _dict(job)
    if str(attempt.get("status") or "").strip().lower() != "sent":
        return None
    if not attempt.get("download_client"):
        return None
    pack_match = _source_worker_pack_match(queue, attempt, db_path=db_path)
    if not pack_match:
        return None
    candidate = _candidate_summary(attempt)
    identity = _first_value(
        attempt.get("candidate_identity"),
        attempt.get("download_id"),
        attempt.get("external_id"),
        attempt.get("download_url_hash"),
        candidate.get("title"),
    )
    review_id = inkdrop_state.stable_id("source_worker_pack", queue.get("id"), identity)
    created_at = time.time() if now is None else float(now)
    wanted_item = _dict(job.get("wanted_item"))
    record = {
        "event": "pending_pack_import",
        "source": "source_worker_pack_handoff",
        "pack_handoff_contract_version": PACK_HANDOFF_CONTRACT_VERSION,
        "created_at": created_at,
        "created_at_iso": inkdrop_state.utc_stamp(created_at),
        "review_id": review_id,
        "status": "sent",
        "queue_id": queue.get("id"),
        "wanted_id": queue.get("wanted_id"),
        "series_id": queue.get("series_id"),
        "issue_id": queue.get("issue_id"),
        "source_attempt_id": recorded.get("attempt_id"),
        "series": _first_value(queue.get("series"), wanted_item.get("series_title"), wanted_item.get("series")),
        "issue": _first_value(queue.get("issue_number"), wanted_item.get("issue_number"), attempt.get("issue_number")),
        "volume_id": queue.get("kapowarr_id"),
        "query": _first_value(attempt.get("query"), queue.get("query"), candidate.get("title")),
        "title": _first_value(attempt.get("title"), candidate.get("title")),
        "candidate": candidate,
        "pack_info": {
            "source": "source_worker",
            "summary": _first_value(attempt.get("title"), candidate.get("title")),
            "coverage_source": pack_match.get("coverage_source"),
            "download_client": attempt.get("download_client"),
            "protocol": attempt.get("protocol"),
            "entry_count": pack_match.get("content_entry_count"),
        },
        "pack_match": pack_match,
        "outcome": {
            key: attempt.get(key)
            for key in ("download_client", "download_id", "external_id", "category", "protocol")
            if attempt.get(key) not in (None, "")
        },
    }
    return {key: value for key, value in record.items() if value not in (None, "", [], {})}


def _pending_pack_duplicate(record, path=None, max_lines=2000):
    record = _dict(record)
    path = Path(path or PENDING_PACKS_LOG)
    if not path.exists():
        return False
    review_id = str(record.get("review_id") or "").strip()
    candidate = _dict(record.get("candidate"))
    identity = str(
        _first_value(
            candidate.get("candidate_identity"),
            candidate.get("download_id"),
            candidate.get("external_id"),
            record.get("source_attempt_id"),
        )
        or ""
    ).strip()
    queue_id = str(record.get("queue_id") or "").strip()
    pack_match = _dict(record.get("pack_match"))
    covered_queue_ids = {
        str(value or "").strip()
        for value in pack_match.get("covered_queue_ids") or []
        if str(value or "").strip()
    }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max(1, int(max_lines or 2000)) :]
    except OSError:
        return False
    for line in reversed(lines):
        try:
            existing = json.loads(line)
        except ValueError:
            continue
        if review_id and str(existing.get("review_id") or "").strip() == review_id:
            return True
        existing_candidate = _dict(existing.get("candidate"))
        existing_identity = str(
            _first_value(
                existing_candidate.get("candidate_identity"),
                existing_candidate.get("download_id"),
                existing_candidate.get("external_id"),
                existing.get("source_attempt_id"),
            )
            or ""
        ).strip()
        if identity and existing_identity == identity and queue_id and str(existing.get("queue_id") or "").strip() == queue_id:
            return True
        if identity and existing_identity == identity:
            existing_match = _dict(existing.get("pack_match"))
            existing_covered = {
                str(value or "").strip()
                for value in existing_match.get("covered_queue_ids") or []
                if str(value or "").strip()
            }
            if covered_queue_ids and existing_covered and covered_queue_ids.issubset(existing_covered):
                return True
            if queue_id and queue_id in existing_covered and not covered_queue_ids:
                return True
    return False


def append_pending_pack_record(record, path=None):
    record = _dict(record)
    if not record:
        return {"ok": False, "reason": "missing_record"}
    path = Path(path or PENDING_PACKS_LOG)
    if _pending_pack_duplicate(record, path=path):
        return {"ok": True, "created": False, "reason": "duplicate_pending_pack", "review_id": record.get("review_id")}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return {"ok": True, "created": True, "review_id": record.get("review_id"), "path": str(path)}


def prepare_source_job_result_for_recording(
    job_result,
    *,
    job=None,
    source_memory_db_path=None,
    source_memory_cooldown_seconds=None,
    now=None,
):
    result = _clone_result(job_result)
    diagnostic_attempts = [attempt for attempt in (result.get("attempts") or []) if isinstance(attempt, dict)]
    source_memory_applied = False
    if source_memory_db_path and job:
        import inkdrop_source_suppression as suppression

        job = _dict(job)
        registry_row = _dict(job.get("registry_row"))
        wanted_item = _dict(job.get("wanted_item"))
        runtime_results = suppression.apply_source_memory_to_runtime_results(
            source_memory_db_path,
            result.get("runtime_results") or [],
            registry_row,
            wanted_item,
            now=now,
            cooldown_seconds=source_memory_cooldown_seconds,
        )
        result["runtime_results"] = runtime_results
        result["attempts"] = _attempts_with_diagnostic_fallback(
            _attempts_from_runtime_results(runtime_results),
            diagnostic_attempts,
        )
        source_memory_applied = True

    job = _dict(job)
    registry_row = _dict(job.get("registry_row"))
    wanted_item = _dict(job.get("wanted_item"))
    runtime_results = result.get("runtime_results") or []
    if runtime_results:
        runtime_results, auto_send_selection = runtime.select_auto_send_attempts(
            runtime_results,
            registry_row,
            wanted_item,
            scope="recording",
        )
        result["runtime_results"] = runtime_results
        result["attempts"] = _attempts_with_diagnostic_fallback(
            _attempts_from_runtime_results(runtime_results),
            diagnostic_attempts,
        )
        if auto_send_selection.get("applied"):
            result["auto_send_selection"] = auto_send_selection

    attempts = [attempt for attempt in (result.get("attempts") or []) if isinstance(attempt, dict)]
    attempts = _attempts_with_diagnostic_fallback(attempts, diagnostic_attempts)
    result["attempts"] = attempts
    result["result_status"] = _result_status(result.get("runtime_results") or [], attempts)
    result["source_memory_applied"] = source_memory_applied
    result["recordable_attempt_count"] = len(attempts)
    return result


def record_source_job_result(
    db_path,
    queue_id,
    job_result,
    *,
    job=None,
    prepared_result=None,
    source_memory_db_path=None,
    source_memory_cooldown_seconds=None,
    dry_run=False,
    max_attempts=None,
    record_lock_retry_attempts=None,
    record_lock_retry_initial_delay=None,
    now=None,
):
    queue_id = str(queue_id or "").strip()
    if not queue_id:
        return {"ok": False, "reason": "missing_queue_id"}
    queue = _queue_row(db_path, queue_id)
    if not queue:
        return {"ok": False, "reason": "queue_item_not_found", "queue_id": queue_id}

    now = time.time() if now is None else float(now)
    prepared = (
        dict(prepared_result)
        if isinstance(prepared_result, dict)
        else _prepare_recording_result_for_queue(
            db_path,
            queue,
            job_result,
            job=job,
            source_memory_db_path=source_memory_db_path,
            source_memory_cooldown_seconds=source_memory_cooldown_seconds,
            now=now,
        )
    )
    attempts = list(prepared.get("attempts") or [])
    if max_attempts not in (None, ""):
        attempts = attempts[: max(0, int(max_attempts or 0))]

    before_attempts = _queue_count(db_path, queue_id, "source_attempts")
    before_tasks = _queue_count(db_path, queue_id, "download_tasks")
    out = {
        "source_worker_recorder_contract_version": CONTRACT_VERSION,
        "ok": True,
        "dry_run": bool(dry_run),
        "queue_id": queue_id,
        "provider_id": prepared.get("provider_id") or (job or {}).get("provider_id"),
        "result_status": prepared.get("result_status"),
        "source_memory_applied": bool(prepared.get("source_memory_applied")),
        "attempts_available": len(prepared.get("attempts") or []),
        "attempts_selected": len(attempts),
        "attempts_recorded": 0,
        "pending_pack_records_created": 0,
        "download_tasks_before": before_tasks,
        "download_tasks_after": before_tasks,
        "download_tasks_created": 0,
        "records": [],
        "pending_pack_records": [],
    }
    if dry_run:
        out["queue_state_after"] = queue.get("state")
        out["current_source_after"] = queue.get("current_source")
        return out

    for attempt in attempts:
        lock_retry_metrics = {}
        try:
            recorded, lock_retry_metrics = _record_queue_source_attempt_with_lock_retry(
                db_path,
                queue_id,
                attempt,
                started_at=attempt.get("started_at") or attempt.get("ts") or now,
                completed_at=attempt.get("completed_at") if attempt.get("completed_at") is not None else now,
                attempts=record_lock_retry_attempts,
                initial_delay=record_lock_retry_initial_delay,
            )
        except Exception as exc:
            if not inkdrop_state.is_database_locked_error(exc):
                raise
            recorded = {
                "ok": False,
                "reason": "database_locked_while_recording_source_attempt",
                "error": str(exc),
                "queue_id": queue_id,
                "provider_id": prepared.get("provider_id") or (job or {}).get("provider_id"),
                "attempt_status": attempt.get("status"),
                "record_lock_retry": lock_retry_metrics,
            }
            out["ok"] = False
            out["reason"] = recorded["reason"]
            out["records"].append(recorded)
            break
        if lock_retry_metrics.get("retries"):
            recorded = dict(recorded or {})
            recorded["record_lock_retry"] = lock_retry_metrics
        out["records"].append(recorded)
        if recorded.get("ok"):
            out["attempts_recorded"] += 1
            pack_record = source_worker_pack_pending_record(
                queue,
                attempt,
                recorded=recorded,
                job=job,
                now=now,
                db_path=db_path,
            )
            if pack_record:
                pack_result = append_pending_pack_record(pack_record)
                out["pending_pack_records"].append(pack_result)
                if pack_result.get("created"):
                    out["pending_pack_records_created"] += 1

    try:
        after_attempts = _queue_count(db_path, queue_id, "source_attempts")
        after_tasks = _queue_count(db_path, queue_id, "download_tasks")
        queue_after = _queue_row(db_path, queue_id)
    except Exception as exc:
        if not inkdrop_state.is_database_locked_error(exc):
            raise
        after_attempts = before_attempts
        after_tasks = before_tasks
        queue_after = queue
        out["ok"] = False
        out["reason"] = out.get("reason") or "database_locked_while_summarizing_recording"
        out["summary_error"] = str(exc)
    out.update(
        {
            "source_attempts_before": before_attempts,
            "source_attempts_after": after_attempts,
            "source_attempts_created": max(0, after_attempts - before_attempts),
            "download_tasks_after": after_tasks,
            "download_tasks_created": max(0, after_tasks - before_tasks),
            "queue_state_after": queue_after.get("state"),
            "current_source_after": queue_after.get("current_source"),
            "last_event_after": queue_after.get("last_event"),
        }
    )
    return out


def record_source_job_results(
    db_path,
    queue_id,
    job_results,
    *,
    jobs_by_provider_id=None,
    **kwargs,
):
    jobs_by_provider_id = jobs_by_provider_id if isinstance(jobs_by_provider_id, dict) else {}
    queue = _queue_row(db_path, queue_id)
    if not queue:
        return {"ok": False, "reason": "queue_item_not_found", "queue_id": queue_id}
    now = time.time() if kwargs.get("now") is None else float(kwargs.get("now"))
    prepare_kwargs = {
        "source_memory_db_path": kwargs.get("source_memory_db_path"),
        "source_memory_cooldown_seconds": kwargs.get("source_memory_cooldown_seconds"),
        "now": now,
    }
    prepared_results = []
    for result in job_results or []:
        provider_id = (result or {}).get("provider_id")
        prepared_results.append(
            _prepare_recording_result_for_queue(
                db_path,
                queue,
                result,
                job=jobs_by_provider_id.get(provider_id),
                **prepare_kwargs,
            )
        )
    prepared_results, queue_auto_send_selection = _apply_queue_auto_send_selection(prepared_results, queue)

    record_kwargs = dict(kwargs)
    record_kwargs["now"] = now
    record_kwargs.pop("source_memory_db_path", None)
    record_kwargs.pop("source_memory_cooldown_seconds", None)
    results = []
    for index, prepared in enumerate(prepared_results):
        provider_id = (prepared or {}).get("provider_id")
        per_result_kwargs = dict(record_kwargs)
        per_result_kwargs["now"] = now + (index * 0.001)
        results.append(
            record_source_job_result(
                db_path,
                queue_id,
                prepared,
                job=jobs_by_provider_id.get(provider_id),
                prepared_result=prepared,
                **per_result_kwargs,
            )
        )
    out = {
        "source_worker_recorder_contract_version": CONTRACT_VERSION,
        "ok": all(row.get("ok") for row in results),
        "queue_id": queue_id,
        "results": results,
        "attempts_available": sum(int(row.get("attempts_available") or 0) for row in results),
        "attempts_selected": sum(int(row.get("attempts_selected") or 0) for row in results),
        "attempts_recorded": sum(int(row.get("attempts_recorded") or 0) for row in results),
        "download_tasks_created": sum(int(row.get("download_tasks_created") or 0) for row in results),
        "pending_pack_records_created": sum(int(row.get("pending_pack_records_created") or 0) for row in results),
    }
    if queue_auto_send_selection.get("applied"):
        out["queue_auto_send_selection"] = queue_auto_send_selection
    return out
