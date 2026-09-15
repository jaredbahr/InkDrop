"""Source-memory suppression helpers for InkDrop source workers.

These helpers adapt new provider candidates and source attempts to InkDrop's
existing bad_source_candidates memory. Read helpers are side-effect free; record
helpers mutate only when a caller explicitly passes a database path.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import time

from core import inkdrop_records
from core import inkdrop_source_providers as providers
from core import inkdrop_state
from core import inkdrop_sources


CONTRACT_VERSION = 2

SOURCE_MEMORY_REASON = "known_bad_source_candidate"

# Memoization of settled refusals.
#
# The blocklist decides correctly every time it is asked. It is asked far too
# often: 24,300 known_bad_candidate_skipped rows on production in the 7 days to
# 2026-08-15, across 87 distinct (queue row, release) decisions. `Powers 016
# (2001) (Digital) (Zone-Empire).cbr` alone was re-offered and re-refused 1,431
# times over 12.9 days -- 4.6 an hour, correct every single time -- and each of
# those refusals re-ran the full lookup, which materialises 3.5 MB of raw_json
# to answer a question we had already answered.
#
# Only *refusals* are memoized. A cached refusal can only ever cause more
# blocking, never less, so a stale one errs in the safe direction. A cached
# "nothing known" would be a bypass window -- a release blocked one second ago
# would sail through until the entry expired -- so a negative result is always
# recomputed against the database.
DECISION_MEMO_MAX_ENTRIES = 4096
# Sized against what it has to outlive, not picked round. Production repeats of
# the same (queue row, release) refusal arrive on a median 841s gap (p10 659s,
# p90 1052s, 7 days to 2026-08-15). A 300s ceiling would absorb 2.5% of them and
# read as a fix while changing nothing; 3600s absorbs 99.6%. Safe to set loosely
# because it is only a backstop -- _memo_get() re-checks the block row itself on
# every hit, so the ceiling is never what keeps a refusal honest.
DECISION_MEMO_TTL_SECONDS = 3600.0

_DECISION_MEMO = {}


def invalidate_source_memory_memo(db_path=None):
    """Drop memoized refusals. Belt-and-braces next to the per-hit revalidation."""
    if not db_path:
        _DECISION_MEMO.clear()
        return
    key = str(db_path)
    for memo_key in [entry for entry in _DECISION_MEMO if entry[0] == key]:
        _DECISION_MEMO.pop(memo_key, None)


def _memo_key(db_path, payload, cooldown_seconds):
    return (
        str(db_path),
        str(cooldown_seconds if cooldown_seconds not in (None, "") else ""),
        tuple(
            str(payload.get(field) or "")
            for field in (
                "source", "provider", "protocol", "scope_key", "series",
                "title", "download_url_hash", "source_path", "reason",
            )
        ),
    )


def _cooldown_deadline(last_seen_at, cooldown_seconds):
    try:
        return float(last_seen_at or 0) + max(0, float(cooldown_seconds or 0))
    except (TypeError, ValueError):
        return 0.0


def _memo_get(db_path, memo_key, now, cooldown_seconds):
    """Return a still-valid memoized refusal, or None.

    Validity is re-derived from the block row this refusal was built on, via one
    primary-key probe. That is deliberately not the same as "has the blocklist
    changed at all": production writes a blocklist row roughly every 205s while
    the same refusal comes back on a median 841s gap, so any table-wide
    invalidation signal would discard most entries this memo exists to serve,
    because some unrelated release was blocked in between. An unrelated write is
    not a reason to re-derive *this* refusal, and a newly recorded block can only
    add refusals, never turn one into an acceptance.

    What the probe does catch is everything that can actually change this
    answer: the row being deleted (which is how a user's unblock lands), its
    reason changing, and -- via the live last_seen_at -- the cooldown deadline
    moving. The deadline is recomputed here rather than trusted from the cached
    decision, because a memo that skipped that could go on refusing past the
    point the cooldown had released the candidate, which is exactly the "a
    bad_source_candidates row suppresses its candidate forever" failure the
    default cooldown exists to end.
    """
    entry = _DECISION_MEMO.get(memo_key)
    if not entry:
        return None
    expires_at, block_id, block_reason, decision = entry
    if now >= expires_at:
        _DECISION_MEMO.pop(memo_key, None)
        return None
    live = inkdrop_state.bad_source_candidate_validity_token(db_path, block_id)
    if live is None:
        _DECISION_MEMO.pop(memo_key, None)
        return None
    live_reason, live_last_seen_at = live
    if live_reason != block_reason:
        _DECISION_MEMO.pop(memo_key, None)
        return None
    retry_after = _cooldown_deadline(live_last_seen_at, cooldown_seconds)
    if retry_after and now >= retry_after:
        _DECISION_MEMO.pop(memo_key, None)
        return None
    decision = dict(decision)
    decision["source_memory_memoized"] = True
    if retry_after:
        decision["retry_after"] = retry_after
        decision["retry_after_iso"] = inkdrop_state.utc_stamp(retry_after)
    return decision


def _memo_put(memo_key, now, decision):
    if not decision.get("suppressed"):
        return
    block = decision.get("bad_source_candidate") or {}
    block_id = str(block.get("id") or "").strip()
    if not block_id:
        return
    expires_at = now + DECISION_MEMO_TTL_SECONDS
    retry_after = decision.get("retry_after")
    if retry_after:
        expires_at = min(expires_at, float(retry_after))
    if expires_at <= now:
        return
    if len(_DECISION_MEMO) >= DECISION_MEMO_MAX_ENTRIES:
        for stale_key, stale_entry in list(_DECISION_MEMO.items()):
            if stale_entry[0] <= now:
                _DECISION_MEMO.pop(stale_key, None)
        if len(_DECISION_MEMO) >= DECISION_MEMO_MAX_ENTRIES:
            _DECISION_MEMO.pop(next(iter(_DECISION_MEMO)), None)
    _DECISION_MEMO[memo_key] = (
        expires_at, block_id, str(block.get("reason") or ""), dict(decision),
    )

# source_memory_decision() only applies a cooldown when a caller passes
# cooldown_seconds; every real caller threads that value from
# --source-memory-cooldown-seconds / INKDROP_SOURCE_MEMORY_COOLDOWN_SECONDS,
# which has no default, so production always passed None and suppression
# never expired. This is the fallback applied when no explicit value (None
# or "") reaches source_memory_decision, matching the 7-day TTL convention
# already used for the analogous artifact_bad_content_memory cache
# (ARTIFACT_BAD_CONTENT_MEMORY_TTL_SECONDS, inkdrop_completed_import.py).
DEFAULT_SOURCE_MEMORY_COOLDOWN_SECONDS = 7 * 24 * 3600


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return int(default)


# ONE ROW, ONE MEANING, WHICHEVER READER ASKS.
#
# bad_source_candidates is a single table with several readers. The SLSKD
# probe's durable read applies a cooldown to the reasons below inside the
# lookup itself, so a transfer, network or resolver outcome stops suppressing
# once the window has passed. The acquire path reads the same table through
# find_bad_source_candidate() directly -- durable_bad_source_result_match() and
# known_bad_pack_archive_history() in inkdrop_missing_acquire -- and applied no
# reason filter and no age check, so the identical row was retryable in one
# reader and permanent in the other.
#
# None of these reasons is a verdict about the file. A peer stalling, a peer
# forgetting a transfer, or our own resolver erroring says nothing about
# whether the file is the right file, and releasing re-admits the candidate to
# EVALUATION rather than to the library: every quality and identity gate still
# runs in front of it. Every other reason -- the judgements that ARE about the
# file -- is absent from this set and keeps suppressing exactly as before, as
# does a row with no usable timestamp.
#
# This lives in a leaf module rather than in either consumer because both need
# it and neither imports the other: inkdrop_manual_source_autoresolve loads the
# probe by path (load_probe_module) instead of importing it, so deriving one
# list from the other at import time would be a new hard dependency pointing
# the opposite way from how those two are wired. The probe still holds its own
# literal of this set and reads the same environment knob, so one variable
# moves both windows; a follow-up should pin the two sets equal with a test.
TRANSIENT_BAD_CANDIDATE_RETRY_SECONDS = _env_int(
    "INKDROP_SLSKD_TRANSIENT_BAD_CANDIDATE_RETRY_SECONDS", 30 * 60
)
TRANSIENT_BAD_CANDIDATE_REASONS = frozenset({
    "resolver_error",
    "slskd_transfer_failed",
    "slskd_transfer_missing_staged_file",
    "slskd_transfer_stalled",
    "slskd_transfer_evidence_expired",
})


def _row_seen_at(row):
    """When this refusal was last sighted, as epoch seconds, or 0 if unknown.

    Durable rows carry `last_seen_at`; the probe's per-review and shaped rows
    carry `ts`, or only an ISO `ts_iso`. The predicate below has to answer for
    every shape that reaches it, and 0 means "cannot tell", not "long ago".
    """
    for key in ("ts", "last_seen_at"):
        try:
            value = float(row.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    try:
        return float(calendar.timegm(time.strptime(str(row.get("ts_iso") or ""), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def transient_bad_candidate_retry_ready(row, *, now=None):
    """May this remembered refusal be re-evaluated yet?

    True only for a transient reason whose cooldown has passed. It fails
    closed everywhere else: a reason outside the set, a row that is not a
    mapping, and a row whose age cannot be established all keep suppressing.
    """
    if not isinstance(row, dict):
        return False
    if str(row.get("reason") or "") not in TRANSIENT_BAD_CANDIDATE_REASONS:
        return False
    seen_at = _row_seen_at(row)
    if seen_at <= 0:
        return False
    resolved_now = time.time() if now is None else float(now)
    return (resolved_now - seen_at) >= TRANSIENT_BAD_CANDIDATE_RETRY_SECONDS


LEGACY_DIRECT_DOWNLOAD_INFRASTRUCTURE_REASONS = {
    "download_write_failed",
    "http_request_failed",
    "http_client_required",
    "metadata_sidecar_write_failed",
    "missing_local_path",
    "missing_staging_root",
    "partial_matches_final_path",
    "partial_outside_staging_root",
    "target_exists",
    "target_not_cbz",
    "target_outside_staging_root",
    "unsupported_stage_download_client",
}

HANDOFF_FIELDS = (
    "download_client",
    "external_id",
    "save_path",
    "local_path",
    "download_path",
    "partial_path",
    "category",
)


def _dict(value):
    return dict(value) if isinstance(value, dict) else {}


def _first(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _candidate_from_attempt(attempt):
    attempt = _dict(attempt)
    raw = attempt.get("raw") if isinstance(attempt.get("raw"), dict) else {}
    candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
    if candidate:
        return candidate
    return {
        "provider_id": attempt.get("provider_id") or attempt.get("provider") or attempt.get("source"),
        "provider": attempt.get("provider"),
        "source": attempt.get("source"),
        "protocol": attempt.get("protocol"),
        "title": attempt.get("title") or attempt.get("query"),
        "series_title": attempt.get("query") or attempt.get("title"),
        "download_url_hash": attempt.get("download_url_hash"),
        "candidate_identity": attempt.get("candidate_identity"),
        "source_path": attempt.get("local_path") or attempt.get("download_path") or attempt.get("source_path"),
    }


def _reason_key(value):
    return str(value or "").strip().lower() or SOURCE_MEMORY_REASON


def _source_key(candidate, registry_row=None):
    candidate = _dict(candidate)
    registry_row = _dict(registry_row)
    return str(
        _first(
            candidate.get("provider_id"),
            candidate.get("source"),
            registry_row.get("provider_id"),
            registry_row.get("source"),
        )
        or ""
    ).strip()


def _provider_name(candidate, registry_row=None):
    candidate = _dict(candidate)
    registry_row = _dict(registry_row)
    return str(
        _first(
            candidate.get("indexer"),
            candidate.get("provider"),
            candidate.get("provider_name"),
            registry_row.get("display_name"),
            registry_row.get("provider_id"),
            candidate.get("provider_id"),
        )
        or ""
    ).strip()


def _protocol(candidate):
    candidate = _dict(candidate)
    protocol = str(candidate.get("protocol") or "").strip()
    if protocol:
        return protocol
    if candidate.get("magnet_url") or candidate.get("info_hash"):
        return "torrent"
    if candidate.get("download_url"):
        return "http"
    return ""


def _series_title(candidate, wanted_item=None):
    candidate = _dict(candidate)
    wanted_item = _dict(wanted_item)
    return str(
        _first(
            wanted_item.get("series_title"),
            wanted_item.get("series"),
            candidate.get("series_title"),
            candidate.get("series"),
            wanted_item.get("title"),
        )
        or ""
    ).strip()


def _candidate_title(candidate, wanted_item=None):
    candidate = _dict(candidate)
    wanted_item = _dict(wanted_item)
    return str(
        _first(
            candidate.get("title"),
            candidate.get("release_title"),
            candidate.get("archive_file_name"),
            wanted_item.get("title"),
            wanted_item.get("series_title"),
        )
        or ""
    ).strip()


def _download_hash(candidate):
    candidate = _dict(candidate)
    if candidate.get("download_url_hash"):
        return str(candidate.get("download_url_hash") or "").strip()
    locator = _first(
        candidate.get("download_url"),
        candidate.get("magnet_url"),
        candidate.get("info_hash"),
        candidate.get("guid"),
        candidate.get("candidate_identity"),
    )
    return providers.url_hash(locator) if locator else ""


def _source_path(candidate):
    candidate = _dict(candidate)
    return str(
        _first(
            candidate.get("source_path"),
            candidate.get("local_path"),
            candidate.get("download_path"),
            candidate.get("source_url"),
            candidate.get("url"),
            candidate.get("archive_file_name"),
        )
        or ""
    ).strip()


def _scope_key(wanted_item=None):
    wanted_item = _dict(wanted_item)
    return inkdrop_state.bad_source_scope_key(
        series=wanted_item.get("series") or wanted_item.get("series_title"),
        series_id=wanted_item.get("series_id"),
        issue_id=wanted_item.get("issue_id"),
        issue_number=wanted_item.get("issue_number") or wanted_item.get("issue"),
        queue_id=wanted_item.get("queue_id"),
        wanted_id=wanted_item.get("wanted_id"),
    )


def bad_source_payload_from_candidate(candidate, registry_row=None, wanted_item=None, *, reason=None, raw=None, seen_at=None):
    candidate = _dict(candidate)
    payload = {
        "source": _source_key(candidate, registry_row),
        "provider": _provider_name(candidate, registry_row),
        "protocol": _protocol(candidate),
        "scope_key": _scope_key(wanted_item),
        "series": _series_title(candidate, wanted_item),
        "title": _candidate_title(candidate, wanted_item),
        "download_url_hash": _download_hash(candidate),
        "source_path": _source_path(candidate),
        "reason": _reason_key(reason or candidate.get("review_reason") or SOURCE_MEMORY_REASON),
        "raw": raw if isinstance(raw, dict) else {"kind": "source_worker_candidate", "candidate": candidate},
        "seen_at": float(seen_at or time.time()),
    }
    return payload


def bad_source_payload_from_attempt(attempt, registry_row=None, wanted_item=None, *, reason=None, seen_at=None):
    attempt = _dict(attempt)
    candidate = _candidate_from_attempt(attempt)
    return bad_source_payload_from_candidate(
        candidate,
        registry_row,
        wanted_item,
        reason=reason or attempt.get("failure_reason") or attempt.get("reason") or attempt.get("status"),
        raw={"kind": "source_worker_attempt", "attempt": attempt, "candidate": candidate},
        seen_at=seen_at or attempt.get("completed_at") or attempt.get("started_at"),
    )


def bad_source_payload_from_download_result(result, download_task=None, *, seen_at=None):
    result = _dict(result)
    task = _dict(download_task)
    raw = task.get("raw_json") if isinstance(task.get("raw_json"), dict) else {}
    candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
    if not candidate:
        candidate = {
            "provider_id": task.get("provider_id") or task.get("provider") or task.get("source"),
            "provider": task.get("provider"),
            "source": task.get("source"),
            "protocol": task.get("protocol") or "http",
            "title": task.get("title"),
            "download_url_hash": task.get("download_url_hash") or result.get("url_hash"),
            "source_path": task.get("local_path") or result.get("path"),
        }
    return bad_source_payload_from_candidate(
        candidate,
        {"provider_id": task.get("provider_id") or task.get("provider") or task.get("source")},
        None,
        reason=result.get("failure_reason") or result.get("reason") or "direct_download_failed",
        raw={"kind": "direct_download_result", "download_result": result, "download_task": task},
        seen_at=seen_at,
    )


def _json_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _task_candidate(download_task):
    task = _dict(download_task)
    raw = _json_dict(task.get("raw_json"))
    return raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}


def _owned_task_identity_matches(candidate, download_task):
    candidate = _dict(candidate)
    task = _dict(download_task)
    task_candidate = _task_candidate(task)
    if candidate.get("artifact_safe") is not True:
        return False
    if str(candidate.get("auto_grab_verdict") or "").strip().lower() != "auto_grab_safe":
        return False
    identity = str(candidate.get("candidate_identity") or "").strip().lower()
    task_identity = str(task.get("candidate_identity") or "").strip().lower()
    external_id = str(task.get("external_id") or "").strip().lower()
    recorded_identity = str(task_candidate.get("candidate_identity") or "").strip().lower()
    if not inkdrop_records.is_candidate_identity(identity):
        return False
    if not identity == task_identity == external_id == recorded_identity:
        return False
    current_provider = inkdrop_sources.provider_key(candidate.get("provider_id") or candidate.get("source"))
    task_provider = inkdrop_sources.provider_key(task.get("provider_id") or task.get("source"))
    recorded_provider = inkdrop_sources.provider_key(task_candidate.get("provider_id"))
    return bool(current_provider and current_provider == task_provider == recorded_provider)


def _owned_staging_task(download_task, candidate, reason):
    task = _dict(download_task)
    if not _owned_task_identity_matches(candidate, task):
        return False
    raw = _json_dict(task.get("raw_json"))
    client = str(task.get("download_client") or "").strip().lower()
    if client == providers.PAGE_PACK_DOWNLOAD_CLIENT:
        from core import inkdrop_page_pack_downloader as page_pack_downloader

        owned_rebase = page_pack_downloader.owned_page_pack_rebase_target(task, "/inkdrop-source-memory-proof")
        if reason == "target_outside_staging_root":
            return bool(owned_rebase)
        return bool(
            owned_rebase
            or (raw.get("page_pack_task") is True and raw.get("download_guard") == "reader_page_pack_verdict")
        )
    if client == providers.DIRECT_DOWNLOAD_CLIENT:
        return bool(
            raw.get("download_guard") == "direct_artifact_verdict"
            and isinstance(raw.get("direct_artifact"), dict)
        )
    return False


def _staging_failure_envelope(raw):
    raw = _json_dict(raw)
    if raw.get("kind") == "direct_download_result":
        return raw.get("download_result"), raw.get("download_task"), "direct_download_result"
    if set(raw) == {"download_result", "download_task"}:
        return raw.get("download_result"), raw.get("download_task"), "legacy_compact_download_result"
    if raw.get("kind") == "source_worker_attempt":
        attempt = raw.get("attempt") if isinstance(raw.get("attempt"), dict) else {}
        attempt_raw = _json_dict(attempt.get("raw"))
        if attempt_raw.get("kind") == "source_worker_direct_stage":
            return attempt_raw.get("direct_download_result"), attempt_raw.get("download_task"), "source_worker_direct_stage"
    return None, None, ""


def _legacy_compact_result_contract(found, result, task, candidate):
    expected_result_keys = {
        "download_task_id",
        "failure_reason",
        "ok",
        "page_pack_contract_version",
        "provider_id",
        "reason",
        "status",
    }
    if set(result) != expected_result_keys:
        return False
    if result.get("ok") is not False or str(result.get("status") or "").strip().lower() != "blocked":
        return False
    expected_reason = "target_outside_staging_root"
    if not (
        _reason_key(found.get("reason"))
        == _reason_key(result.get("reason"))
        == _reason_key(result.get("failure_reason"))
        == expected_reason
    ):
        return False
    if type(result.get("page_pack_contract_version")) is not int or result.get("page_pack_contract_version") != 2:
        return False
    task_id = str(task.get("id") or "").strip()
    if not task_id or str(result.get("download_task_id") or "").strip() != task_id:
        return False
    result_provider = inkdrop_sources.provider_key(result.get("provider_id"))
    current_providers = (
        inkdrop_sources.provider_key(candidate.get("provider_id")),
        inkdrop_sources.provider_key(candidate.get("source")),
    )
    task_providers = (
        inkdrop_sources.provider_key(task.get("provider")),
        inkdrop_sources.provider_key(task.get("provider_id")),
    )
    return bool(
        result_provider
        and all(value == result_provider for value in current_providers + task_providers)
    )


def _scope_number(value):
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"\d{1,5}(?:\.\d{1,5})?", text):
        return ""
    whole, dot, fraction = text.partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return f"{whole}.{fraction}" if dot and fraction else whole


def _consistent_values(records, keys, normalizer=lambda value: str(value or "").strip().lower()):
    values = []
    for record in records:
        record = _dict(record)
        for key in keys:
            raw_value = record.get(key)
            if raw_value in (None, ""):
                continue
            value = normalizer(raw_value)
            if not value:
                return "", False
            values.append(value)
    if len(set(values)) > 1:
        return "", False
    return (values[0] if values else ""), True


def _explicit_candidate_scope(candidate):
    candidate = _dict(candidate)
    unit, unit_valid = _consistent_values([candidate], ("unit_type", "unitType"))
    if not unit_valid or unit not in {"chapter", "volume"}:
        return {}
    number_keys = (
        ("chapter_number", "chapter", "issue_number", "number", "normalized_number")
        if unit == "chapter"
        else ("volume_number", "volume", "issue_number", "number", "normalized_number")
    )
    number, number_valid = _consistent_values([candidate], number_keys, _scope_number)
    if not number_valid or not number:
        return {}
    return {"unit": unit, "number": number}


def _record_work_id(record):
    record = _dict(record)
    canonical, canonical_valid = _consistent_values(
        [record],
        ("canonical_work_id", "work_id", "canonical_series_id"),
    )
    if not canonical_valid:
        return "", False
    series_id, series_valid = _consistent_values([record], ("series_id",))
    if not series_valid:
        return "", False
    if canonical and series_id and canonical != series_id:
        return "", False
    return canonical or series_id, True


def _target_identifiers_match(candidate, historical, task, wanted_item, provider, scope):
    candidate = _dict(candidate)
    historical = _dict(historical)
    task = _dict(task)
    wanted_item = _dict(wanted_item)
    scope = _dict(scope)
    provider = inkdrop_sources.provider_key(provider)
    if not provider or scope.get("unit") not in {"chapter", "volume"} or not scope.get("number"):
        return {}
    records = [candidate, historical, task, wanted_item]
    work_evidence = []
    for record in records:
        work_id, work_valid = _record_work_id(record)
        if not work_valid:
            return {}
        work_evidence.append(work_id)
    required_work_evidence = (work_evidence[0], work_evidence[1], work_evidence[3])
    if not all(required_work_evidence) or len({value for value in work_evidence if value}) != 1:
        return {}
    work_id = required_work_evidence[0]
    edition_id, edition_valid = _consistent_values(
        records,
        ("edition_id", "canonical_edition_id", "series_edition_id"),
    )
    if not edition_valid:
        return {}
    edition_records = sum(
        1
        for record in records
        if _consistent_values([record], ("edition_id", "canonical_edition_id", "series_edition_id"))[0]
    )
    if edition_id and edition_records < 2:
        return {}
    bound_ids = {}
    for key in ("queue_id", "wanted_id", "issue_id"):
        value, valid = _consistent_values(records, (key,))
        if not valid:
            return {}
        if value:
            corroborating_records = sum(1 for record in records if str(record.get(key) or "").strip())
            if corroborating_records < 2:
                return {}
            bound_ids[key] = value
    family_material = "|".join((work_id, provider, scope["unit"], scope["number"]))
    return {
        "work_id": work_id,
        "target_family_contract_version": 1,
        "target_family_identity": hashlib.sha256(family_material.encode("utf-8")).hexdigest()[:24],
        "edition_id": edition_id,
        **bound_ids,
    }


def _safe_alternate_candidate_matches_task(candidate, task, wanted_item=None):
    candidate = _dict(candidate)
    task = _dict(task)
    historical = _task_candidate(task)
    if candidate.get("artifact_safe") is not True:
        return False
    if str(candidate.get("auto_grab_verdict") or "").strip().lower() != "auto_grab_safe":
        return False
    current_identity = str(candidate.get("candidate_identity") or "").strip().lower()
    if not inkdrop_records.is_candidate_identity(current_identity):
        return False
    provider_values = (
        inkdrop_sources.provider_key(candidate.get("provider_id")),
        inkdrop_sources.provider_key(candidate.get("source")),
        inkdrop_sources.provider_key(historical.get("provider_id")),
        inkdrop_sources.provider_key(historical.get("source")),
        inkdrop_sources.provider_key(task.get("provider")),
        inkdrop_sources.provider_key(task.get("provider_id")),
    )
    if not provider_values[0] or any(value != provider_values[0] for value in provider_values):
        return False
    current_scope = _explicit_candidate_scope(candidate)
    historical_scope = _explicit_candidate_scope(historical)
    if not all(current_scope.values()) or current_scope != historical_scope:
        return False
    task_unit, task_unit_valid = _consistent_values([task], ("unit_type", "unitType"))
    if not task_unit_valid or (task_unit and task_unit != current_scope["unit"]):
        return False
    task_number_keys = (
        ("chapter_number", "chapter", "issue_number", "number", "normalized_number")
        if current_scope["unit"] == "chapter"
        else ("volume_number", "volume", "issue_number", "number", "normalized_number")
    )
    task_number, task_number_valid = _consistent_values([task], task_number_keys, _scope_number)
    if not task_number_valid or (task_number and task_number != current_scope["number"]):
        return False
    wanted = _dict(wanted_item)
    if not wanted:
        return False
    wanted_scope = _explicit_candidate_scope(wanted)
    if not wanted_scope or wanted_scope != current_scope:
        return False
    return bool(_target_identifiers_match(candidate, historical, task, wanted, provider_values[0], current_scope))


def remembered_infrastructure_retry_proof(found, candidate, wanted_item=None):
    """Return evidence only for a well-formed, owned infrastructure failure."""
    found = _dict(found)
    raw = _json_dict(found.get("raw_json"))
    raw_result, raw_task, envelope_kind = _staging_failure_envelope(raw)
    result = raw_result if isinstance(raw_result, dict) else {}
    task = raw_task if isinstance(raw_task, dict) else {}
    if not envelope_kind or not result or not task:
        return {}
    if envelope_kind == "legacy_compact_download_result" and not _legacy_compact_result_contract(found, result, task, candidate):
        return {}
    found_reason = _reason_key(found.get("reason"))
    result_reason = _reason_key(result.get("failure_reason") or result.get("reason"))
    if not found_reason or found_reason != result_reason:
        return {}
    result_class = str(result.get("failure_class") or "").strip().lower()
    if result_class:
        if result_class != "infrastructure":
            return {}
        proof_kind = "structured_infrastructure_failure"
    else:
        if found_reason not in LEGACY_DIRECT_DOWNLOAD_INFRASTRUCTURE_REASONS:
            return {}
        proof_kind = "legacy_infrastructure_contract"
    if envelope_kind == "legacy_compact_download_result":
        if not _safe_alternate_candidate_matches_task(candidate, task, wanted_item):
            return {}
        if str(task.get("download_client") or "").strip().lower() != providers.PAGE_PACK_DOWNLOAD_CLIENT:
            return {}
        from core import inkdrop_page_pack_downloader as page_pack_downloader

        if not page_pack_downloader.owned_page_pack_rebase_target(task, "/inkdrop-source-memory-proof"):
            return {}
    elif not _owned_staging_task(task, candidate, found_reason):
        return {}
    historical_candidate = _task_candidate(task)
    provider = inkdrop_sources.provider_key(candidate.get("provider_id") or candidate.get("source"))
    target_scope = _explicit_candidate_scope(candidate)
    target_identifiers = _target_identifiers_match(
        candidate,
        historical_candidate,
        task,
        wanted_item,
        provider,
        target_scope,
    )
    return {
        "proof_kind": proof_kind,
        "envelope_kind": envelope_kind,
        "failure_class": "infrastructure",
        "failure_reason": found_reason,
        "download_client": str(task.get("download_client") or "").strip().lower(),
        "candidate_identity": str(candidate.get("candidate_identity") or "").strip().lower(),
        "historical_candidate_identity": str(historical_candidate.get("candidate_identity") or "").strip().lower(),
        "candidate_family_identity": str(candidate.get("candidate_family_identity") or "").strip().lower(),
        "historical_candidate_family_identity": str(historical_candidate.get("candidate_family_identity") or "").strip().lower(),
        "target_scope": {
            **target_scope,
            **target_identifiers,
        },
    }


def source_memory_decision(
    db_path,
    candidate,
    registry_row=None,
    wanted_item=None,
    *,
    reason=None,
    now=None,
    cooldown_seconds=None,
):
    payload = bad_source_payload_from_candidate(candidate, registry_row, wanted_item, reason=reason, seen_at=now)
    resolved_now = time.time() if now is None else float(now)
    effective_cooldown = (
        cooldown_seconds if cooldown_seconds not in (None, "") else DEFAULT_SOURCE_MEMORY_COOLDOWN_SECONDS
    )
    memo_key = _memo_key(db_path, payload, cooldown_seconds)
    memoized = _memo_get(db_path, memo_key, resolved_now, effective_cooldown)
    if memoized is not None:
        return memoized

    found = inkdrop_state.find_bad_source_candidate(
        db_path,
        title=payload.get("title"),
        series=payload.get("series"),
        source=payload.get("source"),
        provider=payload.get("provider"),
        protocol=payload.get("protocol"),
        scope_key=payload.get("scope_key"),
        download_url_hash=payload.get("download_url_hash"),
        source_path=payload.get("source_path"),
    )
    decision = {
        "source_suppression_contract_version": CONTRACT_VERSION,
        "suppressed": False,
        "reason": "not_remembered",
        "lookup": payload,
        "bad_source_candidate": found or {},
    }
    if not found:
        return decision

    infrastructure_proof = remembered_infrastructure_retry_proof(found, candidate, wanted_item)
    if infrastructure_proof:
        decision.update(
            {
                "reason": "remembered_infrastructure_retry_allowed",
                "suppressed": False,
                "infrastructure_retry_proof": infrastructure_proof,
            }
        )
        return decision

    requested_reason = _reason_key(reason) if reason else ""
    found_reason = _reason_key(found.get("reason"))
    if requested_reason and found_reason and requested_reason != found_reason:
        decision.update(
            {
                "reason": "failure_reason_changed",
                "previous_reason": found_reason,
                "current_reason": requested_reason,
            }
        )
        return decision

    now = resolved_now
    retry_after = _cooldown_deadline(found.get("last_seen_at"), effective_cooldown)
    decision["retry_after"] = retry_after
    decision["retry_after_iso"] = inkdrop_state.utc_stamp(retry_after) if retry_after else ""
    if retry_after and now >= retry_after:
        decision.update({"reason": "cooldown_expired", "suppressed": False})
        return decision

    decision.update({"reason": SOURCE_MEMORY_REASON, "suppressed": True})
    _memo_put(memo_key, now, decision)
    return decision


def _source_memory_blocked_attempt(attempt, decision):
    attempt = _dict(attempt)
    original = dict(attempt)
    raw = attempt.get("raw") if isinstance(attempt.get("raw"), dict) else {}
    for field in HANDOFF_FIELDS:
        attempt.pop(field, None)
    attempt["status"] = "blocked"
    attempt["reason"] = SOURCE_MEMORY_REASON
    attempt["failure_reason"] = SOURCE_MEMORY_REASON
    attempt["retry_eligible"] = False
    attempt["lifecycle_phase"] = "failed_candidate"
    raw = dict(raw)
    raw["source_memory"] = True
    raw["source_memory_decision"] = decision
    raw["original_attempt"] = {
        key: value
        for key, value in original.items()
        if key not in {"raw"}
    }
    attempt["raw"] = raw
    return attempt


def _source_memory_blocked_verdict(verdict, decision):
    verdict = _dict(verdict)
    reasons = list(verdict.get("block_reasons") or [])
    if SOURCE_MEMORY_REASON not in reasons:
        reasons.insert(0, SOURCE_MEMORY_REASON)
    verdict["block_reasons"] = reasons
    verdict["review_reason"] = SOURCE_MEMORY_REASON
    verdict["auto_grab_verdict"] = "blocked"
    verdict["candidate_safe"] = False
    verdict["artifact_safe"] = False
    verdict["quality_status"] = "rejected"
    verdict["source_memory_bad_candidate"] = decision.get("bad_source_candidate") or {}
    return verdict


def _runtime_status(attempts):
    statuses = {str((attempt or {}).get("status") or "").strip().lower() for attempt in attempts or []}
    if "sent" in statuses:
        return "sent"
    if "review" in statuses:
        return "review"
    if "blocked" in statuses:
        return "blocked"
    if "searched_no_candidates" in statuses:
        return "searched_no_candidates"
    return "observed" if statuses else "unknown"


def apply_source_memory_to_runtime_result(
    db_path,
    runtime_result,
    registry_row=None,
    wanted_item=None,
    *,
    reason=None,
    now=None,
    cooldown_seconds=None,
):
    result = _dict(runtime_result)
    if not db_path:
        return result
    attempts = [dict(attempt) for attempt in (result.get("attempts") or []) if isinstance(attempt, dict)]
    verdicts = [dict(verdict) for verdict in (result.get("verdicts") or []) if isinstance(verdict, dict)]
    suppressed = []
    for index, attempt in enumerate(attempts):
        candidate = _candidate_from_attempt(attempt)
        status_key = str(attempt.get("status") or "").strip().lower()
        candidate_reason = reason
        if not candidate_reason and status_key in {"blocked", "failed"}:
            candidate_reason = attempt.get("failure_reason") or attempt.get("reason")
        decision = source_memory_decision(
            db_path,
            candidate,
            registry_row,
            wanted_item,
            reason=candidate_reason if candidate_reason else None,
            now=now,
            cooldown_seconds=cooldown_seconds,
        )
        if not decision.get("suppressed"):
            continue
        attempts[index] = _source_memory_blocked_attempt(attempt, decision)
        if index < len(verdicts):
            verdicts[index] = _source_memory_blocked_verdict(verdicts[index], decision)
        suppressed.append(
            {
                "index": index,
                "candidate_identity": attempt.get("candidate_identity") or candidate.get("candidate_identity"),
                "bad_source_candidate_id": (decision.get("bad_source_candidate") or {}).get("id"),
                "reason": decision.get("reason"),
            }
        )

    if not suppressed:
        return result

    result["attempts"] = attempts
    result["verdicts"] = verdicts
    result["source_memory"] = {
        "source_suppression_contract_version": CONTRACT_VERSION,
        "suppressed_count": len(suppressed),
        "suppressed": suppressed,
    }
    result["safe_candidate_count"] = sum(1 for verdict in verdicts if verdict.get("auto_grab_verdict") == "auto_grab_safe")
    result["review_candidate_count"] = sum(1 for verdict in verdicts if verdict.get("auto_grab_verdict") == "review")
    result["blocked_candidate_count"] = sum(1 for verdict in verdicts if verdict.get("auto_grab_verdict") == "blocked")
    result["status"] = _runtime_status(attempts)
    return result


def apply_source_memory_to_runtime_results(
    db_path,
    runtime_results,
    registry_row=None,
    wanted_item=None,
    **kwargs,
):
    return [
        apply_source_memory_to_runtime_result(
            db_path,
            row,
            registry_row,
            wanted_item,
            **kwargs,
        )
        for row in (runtime_results or [])
    ]


def record_bad_source_attempt(db_path, attempt, registry_row=None, wanted_item=None, *, reason=None, seen_at=None):
    payload = bad_source_payload_from_attempt(
        attempt,
        registry_row,
        wanted_item,
        reason=reason,
        seen_at=seen_at,
    )
    return inkdrop_state.record_bad_source_candidate(db_path, **payload)


def record_bad_direct_download_result(db_path, download_result, download_task=None, *, seen_at=None):
    if staging_result_failure_class(download_result) != "source_content":
        return {"ok": True, "recorded": False, "reason": "failure_not_classified_as_source_content"}
    payload = bad_source_payload_from_download_result(download_result, download_task, seen_at=seen_at)
    return inkdrop_state.record_bad_source_candidate(db_path, **payload)


def staging_result_failure_class(download_result):
    result = _dict(download_result)
    if result.get("ok"):
        return "success"
    failure_class = str(result.get("failure_class") or "").strip().lower()
    if failure_class in {"infrastructure", "source_content", "policy"}:
        return failure_class
    return "infrastructure"


def clear_infrastructure_bad_direct_download_result(db_path, download_task=None):
    """Remove only a matching legacy infrastructure false-positive memory row."""
    payload = bad_source_payload_from_download_result({}, download_task)
    candidate = inkdrop_state.bad_source_candidate_payload(**payload)
    candidate_id = (candidate or {}).get("id")
    if not candidate_id:
        return {"ok": True, "cleared": False, "reason": "candidate_identity_unavailable"}
    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        row = con.execute(
            "select reason from bad_source_candidates where id=? limit 1",
            (candidate_id,),
        ).fetchone()
        if not row:
            return {"ok": True, "cleared": False, "candidate_id": candidate_id, "reason": "not_remembered"}
        reason = _reason_key(row["reason"])
        if reason not in LEGACY_DIRECT_DOWNLOAD_INFRASTRUCTURE_REASONS:
            return {"ok": True, "cleared": False, "candidate_id": candidate_id, "reason": "source_content_memory_preserved"}
        con.execute("delete from bad_source_candidates where id=? and lower(reason)=?", (candidate_id, reason))
        con.commit()
    return {"ok": True, "cleared": True, "candidate_id": candidate_id, "reason": reason}
