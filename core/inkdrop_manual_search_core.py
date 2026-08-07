"""Durable identity projection and asynchronous Manual Search contracts.

This module is additive by design. Existing Series/Issue identifiers remain the
runtime compatibility contract while Work/Edition/Unit/Artifact records provide
the next identity layer and durable Manual Search evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from core import inkdrop_manga_unit_policy
from core import inkdrop_manual_search as manual_contract


CONTRACT_VERSION = 1
IDENTITY_PROJECTION_VERSION = 1
DEFAULT_RUN_TIMEOUT_SECONDS = 120
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 25
DEFAULT_MAX_QUERIES_PER_PROVIDER = 6
DEFAULT_MAX_CANDIDATES_PER_PROVIDER = 100
DEFAULT_RUN_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RATE_LIMIT_RUNS = 12
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60
DEFAULT_PACK_WARNING_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_PACK_HARD_LIMIT_BYTES = 0
DEFAULT_RUN_LEASE_SECONDS = 300
MAX_RUN_RECLAIM_ATTEMPTS = 2

TERMINAL_RUN_STATES = {"completed", "partial", "failed", "cancelled", "expired"}

# How long a successful grab's audit (run + candidates + queries) is kept
# before it is purged. Anchored to the grab's created_at, so the window is
# deterministic and needs no marker column.
GRAB_AUDIT_RETENTION_SECONDS = float(os.environ.get("INKDROP_GRAB_AUDIT_RETENTION_SECONDS", 90 * 24 * 3600))
RUN_STATES = {"queued", "running", *TERMINAL_RUN_STATES}
FORCED_GRAB_PROTOCOLS = {"torrent", "usenet", "soulseek", "slskd"}
FORCED_GRAB_LOCATOR_KEYS = (
    "download_url", "downloadUrl", "url", "link", "magnet", "magnet_uri", "nzb_url",
)
FORCED_GRAB_IMPOSSIBLE_REJECTION_MARKERS = (
    "already_imported", "already_present", "auth", "credential", "duplicate",
    "forbidden", "known_bad", "malicious", "missing_credential", "missing_credentials",
    "preview", "sample", "unauthorized", "unsupported_client", "401", "403",
    "access_denied", "access_required", "already_downloading", "already_imported",
    "archive_invalid", "invalid_archive", "archive_unreadable", "auth_required", "authentication_required",
    "bad_archive", "credential_required", "corrupt", "duplicate_active", "invalid_image",
    "known_malicious", "malware", "preview_not_importable", "preview_or_sample", "source_memory_bad",
    "unsafe_locator", "unsupported_archive", "virus",
)


def _json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return {} if default is None else default


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _text(value).casefold()).strip()


def _candidate_score(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_grab_summary(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    allowed = (
        "ok",
        "state",
        "reason",
        "queue_id",
        "source_attempt_id",
        "download_task_id",
        "client_item_id",
        "download_client",
        "tasks_available",
        "tasks_handed_off",
        "tasks_failed",
        "manual_inbox_guidance",
    )
    out = {key: value.get(key) for key in allowed if value.get(key) not in (None, "", [], {})}
    for key in ("reason",):
        if key in out:
            out[key] = manual_contract.redacted_text(out[key], 160)
    return out


_PRIVATE_SECRET_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|apikey|passkey|password|passwd|token|cookie|authorization|session|secret|headers?)"
)
_PRIVATE_SECRET_QUERY_RE = re.compile(
    r"(?i)(?:api[_-]?key|apikey|passkey|password|passwd|token|cookie|authorization|session|secret|signature|sig|auth|key)"
)


def _private_locator_value(value: str) -> str:
    """Reject credential-bearing URLs instead of persisting reusable secrets."""

    text = str(value or "")[:4096]
    if not re.match(r"(?i)^[a-z][a-z0-9+.-]*://", text):
        return text
    try:
        parsed = urlsplit(text)
        sensitive_query = any(_PRIVATE_SECRET_QUERY_RE.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
        if parsed.username is not None or parsed.password is not None or sensitive_query:
            return "<redacted-private-locator>"
    except ValueError:
        return "<rejected-invalid-locator>"
    return text


def _private_handoff_capsule(value: Any, *, depth: int = 0) -> Any:
    """Keep only bounded acquisition state; never persist credentials or headers."""

    if depth >= 6:
        return "<bounded>"
    if isinstance(value, dict):
        out = {}
        for raw_key in list(value)[:80]:
            key = _text(raw_key)[:100]
            if _PRIVATE_SECRET_KEY_RE.search(key):
                continue
            out[key] = _private_handoff_capsule(value.get(raw_key), depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_private_handoff_capsule(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _private_locator_value(str(value))


def _capsule_contains_rejected_locator(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_capsule_contains_rejected_locator(item) for item in value.values())
    if isinstance(value, list):
        return any(_capsule_contains_rejected_locator(item) for item in value)
    return str(value or "") in {"<redacted-private-locator>", "<rejected-invalid-locator>"}


def _locator_digest(value: Any) -> str:
    text = _text(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _attempt_locator(attempt: dict[str, Any], protocol: str) -> str:
    if protocol in {"soulseek", "slskd"}:
        return ""
    if protocol == "torrent":
        return next(
            (
                _text(attempt.get(key))
                for key in ("magnet", "magnet_uri", "magnet_url", "download_url", "downloadUrl", "url", "link", "info_hash", "torrent_hash", "guid")
                if _text(attempt.get(key))
            ),
            "",
        )
    return next(
        (
            _text(attempt.get(key))
            for key in ("download_url", "downloadUrl", "url", "link", "nzb_url", "guid")
            if _text(attempt.get(key))
        ),
        "",
    )


def _slskd_locator_digest(username: Any, filename: Any, size: Any, provider: Any = "") -> str:
    path = re.sub(r"/+", "/", _text(filename).replace("\\", "/")).strip("/").casefold()
    peer = _text(provider or username).casefold()
    return _locator_digest("|".join(("slskd", peer, path, _text(size))))


def _handoff_binding(public: dict[str, Any], capsule: dict[str, Any]) -> str:
    public = public if isinstance(public, dict) else {}
    capsule = capsule if isinstance(capsule, dict) else {}
    attempt = capsule.get("_inkdrop_manual_attempt")
    if not isinstance(attempt, dict):
        return ""
    locator_digest = _text(attempt.get("locator_digest"))
    identity = _text(public.get("provider_candidate_identity"))
    attempt_identity = _text(attempt.get("provider_candidate_identity") or attempt.get("candidate_identity"))
    provider_id = _key(public.get("provider_id"))
    protocol = _key(public.get("protocol"))
    client = _key(attempt.get("download_client"))
    if not all((identity, attempt_identity, provider_id, protocol, client, locator_digest)):
        return ""
    return _locator_digest("|".join((provider_id, protocol, identity, attempt_identity, client, locator_digest)))


def _handoff_capsule_binding_gate(public: dict[str, Any], handoff_capsule: dict[str, Any]) -> dict[str, Any]:
    public = public if isinstance(public, dict) else {}
    capsule = handoff_capsule if isinstance(handoff_capsule, dict) else {}
    protocol = _text(public.get("protocol")).lower()
    if protocol not in FORCED_GRAB_PROTOCOLS:
        return {"eligible": True}
    attempt = capsule.get("_inkdrop_manual_attempt")
    if not isinstance(attempt, dict) or not attempt:
        return {"eligible": False, "reason": "provider_grab_contract_unavailable"}
    if _text(attempt.get("protocol") or protocol).lower() != protocol:
        return {"eligible": False, "reason": "handoff_protocol_mismatch"}
    supported_clients = {
        "torrent": {"qbit", "qbittorrent"},
        "usenet": {"sab", "sabnzbd"},
        "soulseek": {"slskd"},
        "slskd": {"slskd"},
    }
    public_identity = _text(public.get("provider_candidate_identity"))
    attempt_identity = _text(attempt.get("provider_candidate_identity") or attempt.get("candidate_identity"))
    if (
        _key(attempt.get("download_client")) not in supported_clients.get(protocol, set())
        or not public_identity
        or not attempt_identity
        or _key(attempt.get("provider_id")) != _key(public.get("provider_id"))
        or _text(attempt.get("candidate_binding")) != _text(public.get("candidate_id"))
    ):
        return {"eligible": False, "reason": "candidate_handoff_binding_mismatch"}
    if _capsule_contains_rejected_locator(attempt):
        return {"eligible": False, "reason": "safe_handoff_locator_required"}
    if protocol in {"soulseek", "slskd"}:
        raw = attempt.get("raw") if isinstance(attempt.get("raw"), dict) else {}
        candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
        filename = _text(candidate.get("filename"))
        username = _text(candidate.get("username"))
        if not filename or not username or filename.startswith("<") or username.startswith("<"):
            return {"eligible": False, "reason": "safe_handoff_locator_required"}
        expected_digest = _slskd_locator_digest(username, filename, candidate.get("size"), username)
    else:
        locator = _attempt_locator(attempt, protocol)
        if not locator or (protocol == "usenet" and not re.match(r"(?i)^https?://", locator)) or (
            protocol == "torrent" and not re.match(r"(?i)^(?:https?|magnet):|^[a-f0-9]{20,64}$", locator)
        ):
            return {"eligible": False, "reason": "safe_handoff_locator_required"}
        expected_digest = _locator_digest(locator)
    if _text(attempt.get("locator_digest")) != expected_digest:
        return {"eligible": False, "reason": "candidate_locator_binding_mismatch"}
    return {"eligible": True}


def forced_grab_candidate_gate(public: dict[str, Any], handoff_capsule: dict[str, Any]) -> dict[str, Any]:
    """Return a public-safe gate for the explicit rejected-candidate escape hatch."""

    public = public if isinstance(public, dict) else {}
    capsule = handoff_capsule if isinstance(handoff_capsule, dict) else {}
    decision = public.get("decision") if isinstance(public.get("decision"), dict) else {}
    reasons = list(dict.fromkeys(
        manual_contract.redacted_text(_text(value), 160)
        for value in [*(decision.get("rejection_codes") or []), *(decision.get("negative_evidence") or [])]
        if _text(value)
    ))
    quality_status = _text(public.get("quality_status")).lower()
    if quality_status and any(marker in quality_status for marker in ("invalid", "corrupt", "malware", "virus")):
        reasons.append(quality_status)
    if public.get("preview_or_sample"):
        reasons.append("preview_or_sample")
    reasons = list(dict.fromkeys(reasons))
    if public.get("accepted"):
        return {"eligible": False, "reason": "candidate_already_accepted", "rejection_codes": reasons}
    identity = _text(public.get("provider_candidate_identity"))
    if not identity or not _text(public.get("original_title")) or not _text(public.get("provider_id")):
        return {"eligible": False, "reason": "concrete_candidate_identity_required", "rejection_codes": reasons}
    protocol = _text(public.get("protocol")).lower()
    if protocol not in FORCED_GRAB_PROTOCOLS:
        return {"eligible": False, "reason": "supported_handoff_required", "rejection_codes": reasons}
    attempt = capsule.get("_inkdrop_manual_attempt")
    if not isinstance(attempt, dict) or not attempt:
        return {"eligible": False, "reason": "provider_grab_contract_unavailable", "rejection_codes": reasons}
    attempt_protocol = _text(attempt.get("protocol") or protocol).lower()
    if attempt_protocol != protocol:
        return {"eligible": False, "reason": "handoff_protocol_mismatch", "rejection_codes": reasons}
    client = _key(attempt.get("download_client"))
    supported_clients = {
        "torrent": {"qbit", "qbittorrent"},
        "usenet": {"sab", "sabnzbd"},
        "soulseek": {"slskd"},
        "slskd": {"slskd"},
    }
    public_identity = _text(public.get("provider_candidate_identity"))
    attempt_identity = _text(attempt.get("provider_candidate_identity") or attempt.get("candidate_identity"))
    if (
        client not in supported_clients.get(protocol, set())
        or not public_identity
        or not attempt_identity
        or attempt_identity != public_identity
        or _key(attempt.get("provider_id")) != _key(public.get("provider_id"))
    ):
        return {"eligible": False, "reason": "candidate_handoff_binding_mismatch", "rejection_codes": reasons}
    if _text(attempt.get("candidate_binding")) != _text(public.get("candidate_id")):
        return {"eligible": False, "reason": "supported_handoff_required", "rejection_codes": reasons}
    if _capsule_contains_rejected_locator(attempt):
        return {"eligible": False, "reason": "safe_handoff_locator_required", "rejection_codes": reasons}
    if protocol in {"soulseek", "slskd"}:
        # Force Grab exists precisely for candidates the automatic gate did
        # NOT accept (an already-accepted candidate returns above, at
        # "candidate_already_accepted"), so requiring acquisition_capability
        # == "automatic" here made this branch impossible to satisfy for any
        # real rejected SLSKD candidate -- torrent/usenet below have no such
        # requirement and rely solely on locator validity plus the shared
        # FORCED_GRAB_IMPOSSIBLE_REJECTION_MARKERS check further down.
        if _text(attempt.get("status")).lower() != "ready":
            return {"eligible": False, "reason": "supported_handoff_required", "rejection_codes": reasons}
        raw = attempt.get("raw") if isinstance(attempt.get("raw"), dict) else {}
        slskd_candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
        filename = _text(slskd_candidate.get("filename"))
        username = _text(slskd_candidate.get("username"))
        if not filename or not username or filename.startswith("<") or username.startswith("<"):
            return {"eligible": False, "reason": "safe_handoff_locator_required", "rejection_codes": reasons}
        expected_digest = _slskd_locator_digest(username, filename, slskd_candidate.get("size"), username)
        if _text(attempt.get("locator_digest")) != expected_digest:
            return {"eligible": False, "reason": "candidate_locator_binding_mismatch", "rejection_codes": reasons}
    else:
        locator = _attempt_locator(attempt, protocol)
        if not locator or (protocol == "usenet" and not re.match(r"(?i)^https?://", locator)) or (protocol == "torrent" and not re.match(r"(?i)^(?:https?|magnet):|^[a-f0-9]{20,64}$", locator)):
            return {"eligible": False, "reason": "safe_handoff_locator_required", "rejection_codes": reasons}
        if _text(attempt.get("locator_digest")) != _locator_digest(locator):
            return {"eligible": False, "reason": "candidate_locator_binding_mismatch", "rejection_codes": reasons}
    impossible = next(
        (
            code for code in reasons
            if code != "pack_hard_limit_exceeded"
            and any(marker in _key(code).replace(" ", "_") for marker in FORCED_GRAB_IMPOSSIBLE_REJECTION_MARKERS)
        ),
        "",
    )
    if impossible:
        return {
            "eligible": False,
            "reason": "candidate_cannot_be_acquired_safely",
            "blocking_rejection_code": impossible,
            "rejection_codes": reasons,
        }
    return {"eligible": True, "reason": "explicit_operator_override_available", "rejection_codes": reasons}


def _public_grab(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    value = dict(row or {})
    return {
        key: value.get(key)
        for key in ("state", "queue_id", "source_attempt_id", "download_task_id", "client_item_id", "error_code", "created_at", "updated_at")
        if value.get(key) not in (None, "")
    }


def _id(prefix: str, *parts: Any) -> str:
    seed = "\x1f".join(_text(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _now(value: Any = None) -> float:
    return float(time.time() if value is None else value)


def _connect(db_path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if read_only:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        con.execute("pragma query_only=1")
    else:
        con = sqlite3.connect(path, timeout=10.0)
        con.execute("pragma foreign_keys=on")
        con.execute("pragma busy_timeout=10000")
    con.row_factory = sqlite3.Row
    return con


@contextmanager
def _db(db_path: str | Path, *, read_only: bool = False):
    con = _connect(db_path, read_only=read_only)
    try:
        with con:
            yield con
    finally:
        con.close()


def _ensure_column(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in con.execute(f"pragma table_info({table})")}
    if column not in columns:
        con.execute(f"alter table {table} add column {column} {ddl}")


def _record_history(
    con: sqlite3.Connection,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    series_id: str = "",
    issue_id: str = "",
    message: str,
    outcome: str,
    payload: dict[str, Any] | None = None,
    now: float,
) -> None:
    exists = con.execute(
        "select 1 from sqlite_master where type='table' and name='history_events'"
    ).fetchone()
    if not exists:
        return
    event_id = _id("history", event_type, entity_type, entity_id, int(now * 1000))
    con.execute(
        """
        insert or ignore into history_events(
          id,entity_type,entity_id,series_id,issue_id,event_type,source,message,
          outcome,display_phase,created_at,raw_json
        ) values(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            entity_type,
            entity_id,
            series_id or None,
            issue_id or None,
            event_type,
            "manual_search",
            message,
            outcome,
            "search",
            now,
            _dump(manual_contract.safe_public_structure(payload or {})),
        ),
    )


def ensure_schema(con: sqlite3.Connection) -> None:
    """Install additive schema. Safe to call during every normal schema check."""

    con.executescript(
        """
        create table if not exists identity_works (
            id text primary key,
            canonical_title text not null,
            normalized_title text not null,
            alternate_titles_json text not null default '[]',
            creators_json text not null default '[]',
            media_family text,
            conflict_state text not null default 'clear',
            projection_version integer not null default 1,
            raw_json text not null default '{}',
            created_at real not null,
            updated_at real not null
        );
        create table if not exists identity_editions (
            id text primary key,
            work_id text not null references identity_works(id),
            legacy_series_id text unique references series(id),
            edition_title text,
            edition_label text,
            publisher text,
            language text,
            region text,
            start_year integer,
            end_year integer,
            collection_type text,
            conflict_state text not null default 'clear',
            raw_json text not null default '{}',
            created_at real not null,
            updated_at real not null
        );
        create table if not exists identity_units (
            id text primary key,
            edition_id text not null references identity_editions(id),
            legacy_issue_id text unique references issues(id),
            unit_type text not null,
            unit_number text,
            normalized_number text,
            title text,
            publication_date text,
            conflict_state text not null default 'clear',
            retired_at real,
            raw_json text not null default '{}',
            created_at real not null,
            updated_at real not null
        );
        create table if not exists identity_artifacts (
            id text primary key,
            unit_id text references identity_units(id),
            edition_id text not null references identity_editions(id),
            legacy_media_file_id text unique references media_files(id),
            path text,
            normalized_path text,
            format text,
            source text,
            archive_valid integer,
            import_state text,
            derived_from_artifact_id text references identity_artifacts(id),
            reader_visibility text,
            raw_json text not null default '{}',
            created_at real not null,
            updated_at real not null
        );
        create table if not exists identity_external_ids (
            id text primary key,
            entity_type text not null,
            entity_id text not null,
            provider text not null,
            external_id text not null,
            provenance text,
            confidence text not null default 'exact',
            active integer not null default 1,
            raw_json text not null default '{}',
            created_at real not null,
            updated_at real not null,
            unique(provider, external_id, entity_type, entity_id)
        );
        create table if not exists identity_conflicts (
            id text primary key,
            entity_type text not null,
            left_entity_id text not null,
            right_entity_id text not null,
            state text not null default 'open',
            reason text not null,
            evidence_json text not null default '{}',
            created_at real not null,
            updated_at real not null,
            unique(entity_type, left_entity_id, right_entity_id, reason)
        );
        create table if not exists source_profiles (
            id text primary key,
            display_name text not null,
            media_family text not null,
            enabled integer not null default 1,
            provider_order_json text not null default '[]',
            enabled_sources_json text not null default '[]',
            language text not null default 'en',
            timeout_seconds integer not null default 120,
            provider_timeout_seconds integer not null default 25,
            max_queries_per_provider integer not null default 6,
            max_concurrency integer not null default 3,
            pack_policy_json text not null default '{}',
            automatic_confidence_threshold text not null default 'high',
            default_search_mode text not null default 'backfill',
            placeholder integer not null default 0,
            raw_json text not null default '{}',
            created_at real not null,
            updated_at real not null
        );
        create table if not exists series_source_profile_overrides (
            series_id text primary key references series(id),
            profile_id text references source_profiles(id),
            provider_order_json text,
            enabled_sources_json text,
            language text,
            pack_policy_json text,
            raw_json text not null default '{}',
            created_at real not null,
            updated_at real not null
        );
        create table if not exists manual_search_runs (
            id text primary key,
            series_id text not null references series(id),
            edition_id text references identity_editions(id),
            issue_id text references issues(id),
            unit_id text references identity_units(id),
            requested_by text,
            state text not null,
            source_profile_id text references source_profiles(id),
            provider_selection_json text not null default '[]',
            request_json text not null default '{}',
            context_json text not null default '{}',
            include_rejected integer not null default 1,
            pack_allowed integer not null default 0,
            force_refresh integer not null default 0,
            cancel_requested integer not null default 0,
            claim_token text,
            claimed_by text,
            lease_expires_at real,
            deadline_at real,
            expires_at real,
            started_at real,
            completed_at real,
            error_code text,
            error_detail text,
            created_at real not null,
            updated_at real not null
        );
        create table if not exists manual_search_queries (
            id text primary key,
            run_id text not null references manual_search_runs(id) on delete cascade,
            provider_id text not null,
            query_text text not null,
            query_kind text,
            ordinal integer not null,
            created_at real not null,
            unique(run_id, provider_id, ordinal)
        );
        create table if not exists manual_search_provider_attempts (
            id text primary key,
            run_id text not null references manual_search_runs(id) on delete cascade,
            provider_id text not null,
            provider_display_name text,
            state text not null,
            duration_ms integer,
            result_count integer not null default 0,
            normalized_count integer not null default 0,
            accepted_count integer not null default 0,
            rejected_count integer not null default 0,
            error_code text,
            error_detail text,
            health_json text not null default '{}',
            diagnostics_json text not null default '{}',
            started_at real,
            completed_at real,
            created_at real not null,
            updated_at real not null
        );
        create table if not exists manual_search_candidates (
            id text primary key,
            run_id text not null references manual_search_runs(id) on delete cascade,
            provider_attempt_id text references manual_search_provider_attempts(id),
            provider_id text not null,
            child_source_id text,
            protocol text,
            original_title text,
            normalized_title text,
            match_score real,
            confidence_tier text,
            accepted integer not null default 0,
            pack_candidate integer not null default 0,
            pack_type text,
            size_bytes integer,
            acquisition_capability text,
            candidate_identity text not null,
            public_json text not null,
            evidence_json text not null default '{}',
            created_at real not null,
            updated_at real not null,
            unique(run_id, candidate_identity)
        );
        create table if not exists manual_search_candidate_decisions (
            id text primary key,
            candidate_id text not null references manual_search_candidates(id) on delete cascade,
            decision text not null,
            positive_evidence_json text not null default '[]',
            negative_evidence_json text not null default '[]',
            rejection_codes_json text not null default '[]',
            explanation text,
            policy_json text not null default '{}',
            created_at real not null
        );
        create table if not exists manual_search_grab_results (
            id text primary key,
            candidate_id text not null unique references manual_search_candidates(id),
            run_id text not null references manual_search_runs(id),
            state text not null,
            queue_id text,
            source_attempt_id text,
            download_task_id text,
            client_item_id text,
            handoff_json text not null default '{}',
            error_code text,
            error_detail text,
            requested_by text,
            created_at real not null,
            updated_at real not null,
            handoff_binding text not null default ''
        );
        create table if not exists manual_search_handoff_capsules (
            candidate_id text primary key references manual_search_candidates(id) on delete cascade,
            capsule_json text not null default '{}',
            expires_at real not null,
            created_at real not null,
            updated_at real not null
        );
        create index if not exists idx_identity_work_title on identity_works(normalized_title, media_family);
        create index if not exists idx_identity_edition_work on identity_editions(work_id, start_year, publisher);
        create index if not exists idx_identity_unit_edition_number on identity_units(edition_id, unit_type, normalized_number);
        create index if not exists idx_identity_artifact_unit on identity_artifacts(unit_id, import_state);
        create index if not exists idx_identity_external_lookup on identity_external_ids(provider, external_id, active);
        create index if not exists idx_manual_runs_state_created on manual_search_runs(state, created_at desc);
        create index if not exists idx_manual_runs_requester_created on manual_search_runs(requested_by, created_at desc);
        create index if not exists idx_manual_attempts_run_provider on manual_search_provider_attempts(run_id, provider_id);
        create index if not exists idx_manual_candidates_run_score on manual_search_candidates(run_id, accepted desc, match_score desc, created_at);
        create index if not exists idx_manual_handoff_expires on manual_search_handoff_capsules(expires_at);
        """
    )
    _ensure_column(con, "manual_search_runs", "claim_token", "text")
    _ensure_column(con, "manual_search_runs", "claimed_by", "text")
    _ensure_column(con, "manual_search_runs", "lease_expires_at", "real")
    _ensure_column(con, "manual_search_runs", "reclaim_count", "integer not null default 0")
    _ensure_column(con, "manual_search_grab_results", "forced_rejected", "integer not null default 0")
    _ensure_column(con, "manual_search_grab_results", "handoff_binding", "text not null default ''")
    seed_source_profiles(con)
    con.execute(
        "insert or ignore into schema_migrations(version,name,applied_at) values(?,?,?)",
        (14, "manual_search_identity_and_private_handoff", _now()),
    )


DEFAULT_SOURCE_PROFILES = (
    {
        "id": "western_comics",
        "display_name": "Western comics",
        "media_family": "comic",
        "provider_order": ["prowlarr", "slskd", "rss", "direct"],
        "enabled_sources": ["prowlarr", "slskd", "rss", "direct", "local_manual_inbox"],
        "language": "en",
        "pack_allowed": True,
        "default_search_mode": "backfill",
        "placeholder": False,
    },
    {
        "id": "manga",
        "display_name": "Manga",
        "media_family": "manga",
        "provider_order": ["slskd", "suwayomi", "mangadex", "prowlarr", "direct"],
        "enabled_sources": ["slskd", "suwayomi", "mangadex", "prowlarr", "direct", "local_manual_inbox"],
        "language": "en",
        "pack_allowed": True,
        "default_search_mode": "backfill",
        "placeholder": False,
    },
    {
        "id": "webtoon_placeholder",
        "display_name": "Webtoon / manhwa / manhua",
        "media_family": "webtoon",
        "provider_order": ["suwayomi", "mangadex", "slskd", "prowlarr"],
        "enabled_sources": ["suwayomi", "mangadex", "slskd", "prowlarr", "local_manual_inbox"],
        "language": "en",
        "pack_allowed": False,
        "default_search_mode": "latest_only",
        "placeholder": True,
    },
)


def seed_source_profiles(con: sqlite3.Connection, now: Any = None) -> None:
    ts = _now(now)
    for profile in DEFAULT_SOURCE_PROFILES:
        pack_policy = {
            "allowed": bool(profile["pack_allowed"]),
            "warning_size_bytes": DEFAULT_PACK_WARNING_BYTES,
            "hard_limit_bytes": DEFAULT_PACK_HARD_LIMIT_BYTES,
            "collected_editions_satisfy_units": False,
        }
        con.execute(
            """
            insert or ignore into source_profiles(
                id, display_name, media_family, provider_order_json, enabled_sources_json,
                language, pack_policy_json, default_search_mode, placeholder, created_at, updated_at
            ) values(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                profile["id"], profile["display_name"], profile["media_family"],
                _dump(profile["provider_order"]), _dump(profile["enabled_sources"]),
                profile["language"], _dump(pack_policy), profile["default_search_mode"],
                int(profile["placeholder"]), ts, ts,
            ),
        )


def _external_identity_rows(series: sqlite3.Row, issue: sqlite3.Row | None = None) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    source = issue if issue is not None else series
    metadata_provider = _text(source["metadata_provider"]).lower() if "metadata_provider" in source.keys() else ""
    metadata_id = _text(source["metadata_id"]) if "metadata_id" in source.keys() else ""
    if metadata_provider and metadata_id:
        rows.append((metadata_provider, metadata_id, "metadata"))
    legacy_column = "kapowarr_issue_id" if issue is not None else "kapowarr_id"
    if legacy_column in source.keys() and _text(source[legacy_column]):
        rows.append(("kapowarr", _text(source[legacy_column]), "migration_provenance"))
    return rows


def _upsert_external(con: sqlite3.Connection, entity_type: str, entity_id: str, provider: str, external_id: str, provenance: str, ts: float) -> None:
    row_id = _id("external", entity_type, entity_id, provider, external_id)
    con.execute(
        """
        insert into identity_external_ids(id,entity_type,entity_id,provider,external_id,provenance,created_at,updated_at)
        values(?,?,?,?,?,?,?,?)
        on conflict(provider,external_id,entity_type,entity_id) do update set
          provenance=excluded.provenance, active=1, updated_at=excluded.updated_at
        """,
        (row_id, entity_type, entity_id, provider, external_id, provenance, ts, ts),
    )


def project_series(db_path: str | Path, series_id: str, *, now: Any = None, artifact_limit: int = 5000) -> dict[str, Any]:
    """Idempotently project one legacy Series and its existing Units/Artifacts."""

    ts = _now(now)
    with _db(db_path) as con:
        ensure_schema(con)
        series = con.execute("select * from series where id=?", (series_id,)).fetchone()
        if not series:
            return {"ok": False, "reason": "series_not_found", "series_id": series_id}
        raw = _json(series["raw_json"], {}) if "raw_json" in series.keys() else {}
        media_family = _text(series["media_type"] or raw.get("media_type") or "comic").lower()
        title = _text(series["title"])
        work_id = _id("work", media_family, _key(title))
        creators = raw.get("creators") or raw.get("authors") or []
        aliases = raw.get("aliases") or raw.get("alternate_titles") or []
        con.execute(
            """
            insert into identity_works(id,canonical_title,normalized_title,alternate_titles_json,creators_json,media_family,created_at,updated_at)
            values(?,?,?,?,?,?,?,?)
            on conflict(id) do update set canonical_title=excluded.canonical_title,
              alternate_titles_json=excluded.alternate_titles_json, creators_json=excluded.creators_json,
              media_family=excluded.media_family, updated_at=excluded.updated_at
            """,
            (work_id, title, _key(title), _dump(aliases if isinstance(aliases, list) else []), _dump(creators if isinstance(creators, list) else []), media_family, ts, ts),
        )
        edition_id = f"edition:{series_id}"
        edition_label = _text(raw.get("edition_label") or raw.get("volume_name") or raw.get("publication_title") or title)
        con.execute(
            """
            insert into identity_editions(id,work_id,legacy_series_id,edition_title,edition_label,publisher,language,start_year,collection_type,raw_json,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(id) do update set work_id=excluded.work_id, edition_title=excluded.edition_title,
              edition_label=excluded.edition_label, publisher=excluded.publisher, language=excluded.language,
              start_year=excluded.start_year, collection_type=excluded.collection_type,
              raw_json=excluded.raw_json, updated_at=excluded.updated_at
            """,
            (edition_id, work_id, series_id, title, edition_label, _text(series["publisher"]), _text(raw.get("language") or "en"), series["year"], _text(raw.get("collection_type") or raw.get("format")), _dump({"legacy_series_id": series_id, "source": series["source"]}), ts, ts),
        )
        for provider, external_id, provenance in _external_identity_rows(series):
            _upsert_external(con, "edition", edition_id, provider, external_id, provenance, ts)

        issues = con.execute("select * from issues where series_id=? order by id", (series_id,)).fetchall()
        unit_ids: dict[str, str] = {}
        for issue in issues:
            issue_raw = _json(issue["raw_json"], {}) if "raw_json" in issue.keys() else {}
            target_identity = manual_contract.trusted_target_unit_identity(
                {
                    "unit_type": issue_raw.get("unit_type"),
                    "media_type": media_family,
                    "unit_number": issue["issue_number"],
                    "volume_number": issue_raw.get("volume_number") or issue_raw.get("volume"),
                    "trusted_unit_title": issue["title"],
                    "series_metadata_provider": series["metadata_provider"],
                    "issue_metadata_provider": issue["metadata_provider"],
                    "target_unit_metadata_trusted": True,
                    "series_manga_unit_override": inkdrop_manga_unit_policy.series_manga_unit_override(dict(series)),
                }
            )
            unit_type = _text(
                target_identity.get("unit_type")
                or ("chapter" if media_family in {"manga", "manhwa", "manhua", "webtoon"} else "issue")
            ).lower()
            unit_id = f"unit:{issue['id']}"
            unit_ids[str(issue["id"])] = unit_id
            con.execute(
                """
                insert into identity_units(id,edition_id,legacy_issue_id,unit_type,unit_number,normalized_number,title,publication_date,raw_json,created_at,updated_at)
                values(?,?,?,?,?,?,?,?,?,?,?)
                on conflict(id) do update set edition_id=excluded.edition_id, unit_type=excluded.unit_type,
                  unit_number=excluded.unit_number, normalized_number=excluded.normalized_number,
                  title=excluded.title, publication_date=excluded.publication_date,
                  raw_json=excluded.raw_json, updated_at=excluded.updated_at
                """,
                (unit_id, edition_id, issue["id"], unit_type, _text(issue["issue_number"]), _text(issue["normalized_number"]), _text(issue["title"]), _text(issue["release_date"]), _dump({"legacy_issue_id": issue["id"], "unit_type_source": target_identity.get("source") or "media_default"}), ts, ts),
            )
            for provider, external_id, provenance in _external_identity_rows(series, issue):
                _upsert_external(con, "unit", unit_id, provider, external_id, provenance, ts)

        files = con.execute(
            "select * from media_files where series_id=? order by last_seen_at desc limit ?",
            (series_id, max(1, min(int(artifact_limit), 20000))),
        ).fetchall()
        for media_file in files:
            artifact_id = f"artifact:{media_file['id']}"
            path = _text(media_file["path"])
            extension = Path(path).suffix.lower().lstrip(".") if path else ""
            con.execute(
                """
                insert into identity_artifacts(id,unit_id,edition_id,legacy_media_file_id,path,normalized_path,format,source,archive_valid,import_state,reader_visibility,raw_json,created_at,updated_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                on conflict(id) do update set unit_id=excluded.unit_id, edition_id=excluded.edition_id,
                  path=excluded.path, normalized_path=excluded.normalized_path, format=excluded.format,
                  source=excluded.source, import_state=excluded.import_state, raw_json=excluded.raw_json,
                  updated_at=excluded.updated_at
                """,
                (artifact_id, unit_ids.get(_text(media_file["issue_id"])), edition_id, media_file["id"], path, _text(media_file["normalized_path"]), extension, _text(media_file["source_path"]), None, _text(media_file["status"]), "unknown", _dump({"active": media_file["active"], "legacy_media_file_id": media_file["id"]}), ts, ts),
            )
        return {"ok": True, "series_id": series_id, "work_id": work_id, "edition_id": edition_id, "units": len(issues), "artifacts": len(files)}


def project_batch(db_path: str | Path, *, limit: int = 50, after_series_id: str = "") -> dict[str, Any]:
    with _db(db_path, read_only=True) as con:
        rows = con.execute(
            "select id from series where id>? order by id limit ?",
            (after_series_id, max(1, min(int(limit), 500))),
        ).fetchall()
    projected = [project_series(db_path, row["id"]) for row in rows]
    return {"ok": all(row.get("ok") for row in projected), "count": len(projected), "rows": projected, "next_cursor": rows[-1]["id"] if rows else ""}


def evaluate_edition_equivalence(db_path: str | Path, left_edition_id: str, right_edition_id: str, *, record_conflict: bool = True, now: Any = None) -> dict[str, Any]:
    """Conservatively evaluate two editions without merging either record."""

    ts = _now(now)
    with _db(db_path) as con:
        left = con.execute("select e.*,w.normalized_title work_title from identity_editions e join identity_works w on w.id=e.work_id where e.id=?", (left_edition_id,)).fetchone()
        right = con.execute("select e.*,w.normalized_title work_title from identity_editions e join identity_works w on w.id=e.work_id where e.id=?", (right_edition_id,)).fetchone()
        if not left or not right:
            return {"ok": False, "reason": "edition_not_found"}
        left_ids = {(row["provider"], row["external_id"]) for row in con.execute("select provider,external_id from identity_external_ids where entity_type='edition' and entity_id=? and active=1", (left_edition_id,))}
        right_ids = {(row["provider"], row["external_id"]) for row in con.execute("select provider,external_id from identity_external_ids where entity_type='edition' and entity_id=? and active=1", (right_edition_id,))}
        shared = sorted(left_ids & right_ids)
        same_title = left["work_title"] == right["work_title"]
        publisher_conflict = bool(left["publisher"] and right["publisher"] and _key(left["publisher"]) != _key(right["publisher"]))
        year_conflict = bool(left["start_year"] and right["start_year"] and left["start_year"] != right["start_year"])
        equivalent = bool(shared) and not publisher_conflict and not year_conflict
        uncertain = same_title and not equivalent
        evidence = {"shared_external_ids": shared, "same_title": same_title, "publisher_conflict": publisher_conflict, "year_conflict": year_conflict}
        conflict_id = ""
        if uncertain and record_conflict:
            ordered = sorted((left_edition_id, right_edition_id))
            conflict_id = _id("identity-conflict", "edition", *ordered, "uncertain_equivalence")
            con.execute(
                """
                insert into identity_conflicts(id,entity_type,left_entity_id,right_entity_id,state,reason,evidence_json,created_at,updated_at)
                values(?,?,?,?,?,?,?,?,?)
                on conflict(entity_type,left_entity_id,right_entity_id,reason) do update set evidence_json=excluded.evidence_json,updated_at=excluded.updated_at
                """,
                (conflict_id, "edition", ordered[0], ordered[1], "open", "uncertain_equivalence", _dump(evidence), ts, ts),
            )
            con.execute("update identity_editions set conflict_state='review_required',updated_at=? where id in (?,?)", (ts, left_edition_id, right_edition_id))
        return {"ok": True, "equivalent": equivalent, "confidence": "high" if equivalent else "uncertain" if uncertain else "different", "conflict_id": conflict_id, "evidence": evidence}


def source_profile_for_series(db_path: str | Path, series_id: str, *, requested_profile_id: str = "") -> dict[str, Any]:
    project_series(db_path, series_id)
    with _db(db_path, read_only=True) as con:
        series = con.execute("select media_type from series where id=?", (series_id,)).fetchone()
        override = con.execute("select * from series_source_profile_overrides where series_id=?", (series_id,)).fetchone()
        media = _text(series["media_type"] if series else "").lower()
        default_id = "manga" if media in {"manga", "manhwa", "manhua"} else "webtoon_placeholder" if media == "webtoon" else "western_comics"
        profile_id = requested_profile_id or (_text(override["profile_id"]) if override else "") or default_id
        profile = con.execute("select * from source_profiles where id=? and enabled=1", (profile_id,)).fetchone()
        if not profile:
            profile = con.execute("select * from source_profiles where id=?", (default_id,)).fetchone()
        if not profile:
            return {}
        out = dict(profile)
        for key in ("provider_order_json", "enabled_sources_json", "pack_policy_json", "raw_json"):
            out[key.removesuffix("_json")] = _json(out.pop(key), [] if "sources" in key or "order" in key else {})
        if override:
            for column, key_name in (("provider_order_json", "provider_order"), ("enabled_sources_json", "enabled_sources"), ("pack_policy_json", "pack_policy")):
                if override[column]:
                    value = _json(override[column], out.get(key_name))
                    if value not in (None, "", [], {}):
                        out[key_name] = value
            if override["language"]:
                out["language"] = override["language"]
        return out


def _search_context(
    con: sqlite3.Connection,
    series_id: str,
    issue_id: str = "",
    *,
    singleton_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    series = con.execute("select * from series where id=?", (series_id,)).fetchone()
    if not series:
        raise ValueError("series_not_found")
    issue = con.execute("select * from issues where id=? and series_id=?", (issue_id, series_id)).fetchone() if issue_id else None
    series_raw = _json(series["raw_json"], {})
    issue_raw = _json(issue["raw_json"], {}) if issue else {}
    aliases = series_raw.get("aliases") or series_raw.get("alternate_titles") or []
    return manual_contract.structured_search_input(
        {
            **(singleton_context if isinstance(singleton_context, dict) else {}),
            "canonical_work_title": series["title"],
            "publication_title": series_raw.get("publication_title") or series["title"],
            "aliases": aliases,
            "creators": series_raw.get("creators") or series_raw.get("authors") or [],
            "publisher": series["publisher"],
            "publication_year": series["year"],
            "language": issue_raw.get("language") or series_raw.get("language") or "en",
            "media_type": series["media_type"],
            "unit_type": issue_raw.get("unit_type"),
            "unit_number": issue["issue_number"] if issue else "",
            "volume_number": issue_raw.get("volume_number") or issue_raw.get("volume"),
            "trusted_unit_title": issue["title"] if issue else "",
            "series_metadata_provider": series["metadata_provider"],
            "issue_metadata_provider": issue["metadata_provider"] if issue else "",
            "target_unit_metadata_trusted": bool(issue),
        }
    )


def resolve_search_targets(
    db_path: str | Path,
    *,
    series_id: str = "",
    edition_id: str = "",
    issue_id: str = "",
    unit_id: str = "",
) -> dict[str, Any]:
    """Resolve additive or legacy identities without changing their public IDs."""

    series_id = _text(series_id)
    edition_id = _text(edition_id)
    issue_id = _text(issue_id)
    unit_id = _text(unit_id)
    with _db(db_path, read_only=True) as con:
        if edition_id:
            edition = con.execute(
                "select id,legacy_series_id from identity_editions where id=?",
                (edition_id,),
            ).fetchone()
            if not edition or not edition["legacy_series_id"]:
                return {"ok": False, "reason": "manual_search_edition_not_found", "edition_id": edition_id}
            if series_id and series_id != edition["legacy_series_id"]:
                return {"ok": False, "reason": "manual_search_series_edition_mismatch"}
            series_id = edition["legacy_series_id"]
        elif series_id:
            edition = con.execute(
                "select id,legacy_series_id from identity_editions where legacy_series_id=?",
                (series_id,),
            ).fetchone()
            edition_id = edition["id"] if edition else ""
        if not series_id:
            return {"ok": False, "reason": "manual_search_series_or_edition_required"}

        if unit_id:
            unit = con.execute(
                """
                select u.id,u.legacy_issue_id,e.legacy_series_id
                from identity_units u join identity_editions e on e.id=u.edition_id
                where u.id=?
                """,
                (unit_id,),
            ).fetchone()
            if not unit or not unit["legacy_issue_id"]:
                return {"ok": False, "reason": "manual_search_unit_not_found", "unit_id": unit_id}
            if unit["legacy_series_id"] != series_id:
                return {"ok": False, "reason": "manual_search_unit_edition_mismatch"}
            if issue_id and issue_id != unit["legacy_issue_id"]:
                return {"ok": False, "reason": "manual_search_issue_unit_mismatch"}
            issue_id = unit["legacy_issue_id"]
        elif issue_id:
            unit = con.execute(
                """
                select u.id,e.legacy_series_id
                from identity_units u join identity_editions e on e.id=u.edition_id
                where u.legacy_issue_id=?
                """,
                (issue_id,),
            ).fetchone()
            if unit and unit["legacy_series_id"] != series_id:
                return {"ok": False, "reason": "manual_search_issue_series_mismatch"}
            unit_id = unit["id"] if unit else ""
    return {
        "ok": True,
        "series_id": series_id,
        "edition_id": edition_id,
        "issue_id": issue_id,
        "unit_id": unit_id,
    }


def create_search_run(
    db_path: str | Path,
    *,
    series_id: str = "",
    edition_id: str = "",
    issue_id: str = "",
    unit_id: str = "",
    provider_selection: list[str] | None = None,
    source_profile_id: str = "",
    requested_by: str = "",
    force_refresh: bool = False,
    include_rejected: bool = True,
    pack_allowed: bool | None = None,
    timeout_seconds: int | None = None,
    now: Any = None,
) -> dict[str, Any]:
    ts = _now(now)
    # Runtime startup normally installs the schema. Keeping this entrypoint
    # self-contained also makes CLI recovery and first-run API use safe.
    with _db(db_path) as con:
        ensure_schema(con)
    if edition_id and not series_id:
        with _db(db_path, read_only=True) as con:
            edition = con.execute(
                "select legacy_series_id from identity_editions where id=?", (edition_id,)
            ).fetchone()
            series_id = _text(edition["legacy_series_id"] if edition else "")
    if not series_id:
        return {"ok": False, "reason": "manual_search_series_or_edition_required"}
    projection = project_series(db_path, series_id, now=ts)
    if not projection.get("ok"):
        return projection
    targets = resolve_search_targets(
        db_path,
        series_id=series_id,
        edition_id=edition_id or projection["edition_id"],
        issue_id=issue_id,
        unit_id=unit_id,
    )
    if not targets.get("ok"):
        return targets
    series_id = targets["series_id"]
    edition_id = targets["edition_id"]
    issue_id = targets["issue_id"]
    unit_id = targets["unit_id"]
    profile = source_profile_for_series(db_path, series_id, requested_profile_id=source_profile_id)
    timeout = max(10, min(int(timeout_seconds or profile.get("timeout_seconds") or DEFAULT_RUN_TIMEOUT_SECONDS), 600))
    providers = [_text(value).lower() for value in (provider_selection or []) if _text(value)]
    if not providers:
        providers = list(profile.get("provider_order") or [])
    enabled = set(profile.get("enabled_sources") or [])
    providers = [value for value in providers if not enabled or value in enabled]
    providers = list(dict.fromkeys(providers))[:20]
    profile_pack = profile.get("pack_policy") if isinstance(profile.get("pack_policy"), dict) else {}
    allow_pack = bool(profile_pack.get("allowed")) if pack_allowed is None else bool(pack_allowed)
    # Automatic acquisition already derives this conservative proof from the
    # authoritative series/issue/wanted rows. Reuse it verbatim so Manual
    # Search cannot classify the same collected book differently.
    from core import inkdrop_source_worker_coordinator

    singleton_context = inkdrop_source_worker_coordinator._singleton_issue_context(
        db_path,
        series_id,
        now=ts,
    )
    with _db(db_path) as con:
        ensure_schema(con)
        recent = con.execute(
            "select count(*) as count from manual_search_runs where requested_by=? and created_at>=?",
            (requested_by or "anonymous", ts - DEFAULT_RATE_LIMIT_WINDOW_SECONDS),
        ).fetchone()
        if int(recent["count"] or 0) >= DEFAULT_RATE_LIMIT_RUNS:
            return {"ok": False, "reason": "manual_search_rate_limited", "retry_after_seconds": DEFAULT_RATE_LIMIT_WINDOW_SECONDS}
        context = _search_context(con, series_id, issue_id, singleton_context=singleton_context)
        run_id = f"manual-search:{uuid.uuid4()}"
        request = {
            "series_id": series_id,
            "edition_id": edition_id,
            "issue_id": issue_id,
            "unit_id": unit_id,
            "provider_selection": providers,
            "source_profile_id": profile.get("id"),
            "force_refresh": bool(force_refresh),
            "include_rejected": bool(include_rejected),
            "pack_allowed": allow_pack,
        }
        con.execute(
            """
            insert into manual_search_runs(id,series_id,edition_id,issue_id,unit_id,requested_by,state,source_profile_id,
              provider_selection_json,request_json,context_json,include_rejected,pack_allowed,force_refresh,deadline_at,expires_at,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (run_id, series_id, edition_id, issue_id or None, unit_id or None,
             requested_by or "anonymous", "queued", profile.get("id"), _dump(providers), _dump(request), _dump(context),
             int(include_rejected), int(allow_pack), int(force_refresh), ts + timeout, ts + DEFAULT_RUN_RETENTION_SECONDS, ts, ts),
        )
        _record_history(
            con,
            event_type="manual_search_started",
            entity_type="search_run",
            entity_id=run_id,
            series_id=series_id,
            issue_id=issue_id,
            message="Manual Search queued",
            outcome="queued",
            payload={"providers": providers, "source_profile_id": profile.get("id")},
            now=ts,
        )
    return {"ok": True, "run_id": run_id, "state": "queued", "providers": providers, "deadline_at": ts + timeout, "source_profile": profile}


def _public_run(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    out = {
        key: value.get(key)
        for key in (
            "id", "series_id", "edition_id", "issue_id", "unit_id", "state",
            "source_profile_id", "include_rejected", "pack_allowed", "force_refresh",
            "deadline_at", "expires_at", "started_at", "completed_at", "error_code",
            "created_at", "updated_at",
        )
        if value.get(key) is not None
    }
    out["provider_selection"] = _json(value.get("provider_selection_json"), [])[:20]
    if value.get("error_detail"):
        out["error_summary"] = manual_contract.redacted_text(value.get("error_detail"), 240)
    return out


def get_search_run(db_path: str | Path, run_id: str) -> dict[str, Any]:
    with _db(db_path, read_only=True) as con:
        row = con.execute("select * from manual_search_runs where id=?", (run_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "manual_search_run_not_found", "run_id": run_id}
        counts = con.execute(
            "select count(*) total, sum(accepted) accepted, sum(case when accepted=0 then 1 else 0 end) rejected from manual_search_candidates where run_id=?",
            (run_id,),
        ).fetchone()
        attempts = con.execute("select state,count(*) count from manual_search_provider_attempts where run_id=? group by state", (run_id,)).fetchall()
    return {"ok": True, "run": _public_run(row), "candidate_counts": {"total": int(counts["total"] or 0), "accepted": int(counts["accepted"] or 0), "rejected": int(counts["rejected"] or 0)}, "provider_attempts": {item["state"]: int(item["count"]) for item in attempts}}


def search_results(db_path: str | Path, run_id: str, *, include_rejected: bool = True, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where = "run_id=?" + ("" if include_rejected else " and accepted=1")
    qualified_where = "msc.run_id=?" + ("" if include_rejected else " and msc.accepted=1")
    with _db(db_path, read_only=True) as con:
        total = con.execute(f"select count(*) count from manual_search_candidates where {where}", (run_id,)).fetchone()["count"]
        rows = con.execute(
            f"""select msc.public_json,mshc.capsule_json
                from manual_search_candidates msc
                left join manual_search_handoff_capsules mshc on mshc.candidate_id=msc.id
                where {qualified_where}
                order by msc.accepted desc, coalesce(msc.match_score,0) desc, msc.created_at, msc.id
                limit ? offset ?""",
            (run_id, limit, offset),
        ).fetchall()
    results = []
    for row in rows:
        public = _json(row["public_json"], {})
        if not public.get("accepted"):
            public["force_grab"] = forced_grab_candidate_gate(public, _json(row["capsule_json"], {}))
        results.append(public)
    return {"ok": True, "run_id": run_id, "results": results, "total": int(total or 0), "limit": limit, "offset": offset}


def search_diagnostics(db_path: str | Path, run_id: str, *, limit: int = 50) -> dict[str, Any]:
    with _db(db_path, read_only=True) as con:
        queries = [dict(row) for row in con.execute("select provider_id,query_text,query_kind,ordinal,created_at from manual_search_queries where run_id=? order by provider_id,ordinal limit ?", (run_id, max(1, min(int(limit), 200))))]
        attempts = []
        for row in con.execute("select provider_id,state,duration_ms,result_count,normalized_count,accepted_count,rejected_count,error_code,error_detail,health_json,diagnostics_json,started_at,completed_at,created_at,updated_at from manual_search_provider_attempts where run_id=? order by created_at limit ?", (run_id, max(1, min(int(limit), 200)))):
            value = dict(row)
            item = {key: value.get(key) for key in ("provider_id", "state", "duration_ms", "result_count", "normalized_count", "accepted_count", "rejected_count", "error_code", "started_at", "completed_at", "created_at", "updated_at") if value.get(key) is not None}
            if value.get("error_detail"):
                item["error_summary"] = manual_contract.redacted_text(value.get("error_detail"), 240)
            item["health"] = manual_contract.safe_health_snapshot(_json(value.get("health_json"), {}))
            item["diagnostics"] = manual_contract.safe_public_structure(_json(value.get("diagnostics_json"), {}))
            attempts.append(item)
    return {"ok": True, "run_id": run_id, "queries": queries, "provider_attempts": attempts}


def cancel_search_run(db_path: str | Path, run_id: str, *, requested_by: str = "", now: Any = None) -> dict[str, Any]:
    ts = _now(now)
    with _db(db_path) as con:
        row = con.execute("select state from manual_search_runs where id=?", (run_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "manual_search_run_not_found"}
        if row["state"] in TERMINAL_RUN_STATES:
            return {"ok": True, "run_id": run_id, "state": row["state"], "already_terminal": True}
        con.execute("update manual_search_runs set cancel_requested=1,state='cancelled',claim_token=null,claimed_by=null,lease_expires_at=null,completed_at=?,updated_at=? where id=?", (ts, ts, run_id))
        con.execute(
            """
            update manual_search_provider_attempts
            set state='cancelled',error_code='cancelled',error_detail=null,completed_at=?,updated_at=?
            where run_id=? and state in ('planned','running')
            """,
            (ts, ts, run_id),
        )
        _record_history(
            con,
            event_type="manual_search_cancelled",
            entity_type="search_run",
            entity_id=run_id,
            message="Manual Search cancelled",
            outcome="cancelled",
            payload={"requested_by_present": bool(requested_by)},
            now=ts,
        )
    return {"ok": True, "run_id": run_id, "state": "cancelled"}


def fail_search_run(db_path: str | Path, run_id: str, error: Any, *, now: Any = None) -> dict[str, Any]:
    ts = _now(now)
    detail = manual_contract.redacted_text(error, 500)
    with _db(db_path) as con:
        row = con.execute("select series_id,issue_id,state from manual_search_runs where id=?", (run_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "manual_search_run_not_found"}
        if row["state"] in TERMINAL_RUN_STATES:
            return {"ok": True, "run_id": run_id, "state": row["state"], "already_terminal": True}
        con.execute(
            """
            update manual_search_runs
            set state='failed',error_code='manual_search_worker_error',error_detail=?,
                claim_token=null,claimed_by=null,lease_expires_at=null,completed_at=?,updated_at=?
            where id=?
            """,
            (detail, ts, ts, run_id),
        )
        _record_history(
            con,
            event_type="manual_search_failed",
            entity_type="search_run",
            entity_id=run_id,
            series_id=row["series_id"],
            issue_id=row["issue_id"],
            message="Manual Search worker failed",
            outcome="failed",
            payload={"error": detail},
            now=ts,
        )
    return {"ok": True, "run_id": run_id, "state": "failed"}


def _decision(candidate: dict[str, Any], context: dict[str, Any], pack_policy: dict[str, Any]) -> dict[str, Any]:
    positive: list[str] = []
    negative: list[str] = list(candidate.get("rejection_codes") or [])
    canonical = _key(context.get("canonical_work_title"))
    normalized = _key(candidate.get("interpreted_work") or candidate.get("normalized_title"))
    if canonical and (canonical == normalized or canonical in _key(candidate.get("original_title"))):
        positive.append("exact_or_contained_title")
    if candidate.get("interpreted_unit_number") and _text(candidate.get("interpreted_unit_number")) == _text(context.get("unit_number")):
        positive.append("unit_number_match")
    if candidate.get("language") and candidate.get("language") == context.get("language"):
        positive.append("language_match")
    if candidate.get("interpreted_year") and candidate.get("interpreted_year") == context.get("publication_year"):
        positive.append("year_match")
    size = int(candidate.get("size_bytes") or 0)
    warning = int(pack_policy.get("warning_size_bytes") or DEFAULT_PACK_WARNING_BYTES)
    hard = int(pack_policy.get("hard_limit_bytes") or 0)
    if candidate.get("pack_candidate") and not bool(pack_policy.get("allowed", False)):
        negative.append("pack_not_allowed")
    if candidate.get("pack_candidate") and warning > 0 and size > warning:
        negative.append("pack_size_warning")
    if candidate.get("pack_candidate") and hard > 0 and size > hard:
        negative.append("pack_hard_limit_exceeded")
    if candidate.get("pack_type") == "omnibus_collected_edition" and context.get("unit_type") in {"issue", "chapter"}:
        negative.append("collected_edition_not_unit_completion")
    if candidate.get("assisted_only") or candidate.get("acquisition_capability") == "assisted":
        negative.append("manual_interaction_required")
    blocking = [code for code in negative if code not in {"pack_size_warning"}]
    accepted = bool(candidate.get("accepted")) and not blocking
    explanation = "Candidate is eligible for an explicit grab." if accepted else "Candidate is retained with the evidence that prevented a safe grab."
    return {"decision": "accepted" if accepted else "rejected", "positive_evidence": list(dict.fromkeys(positive)), "negative_evidence": list(dict.fromkeys(negative)), "rejection_codes": list(dict.fromkeys(negative)), "explanation": explanation, "policy": pack_policy, "accepted": accepted}


def process_search_run(
    db_path: str | Path,
    run_id: str,
    provider_runner: Callable[[str, dict[str, Any], list[dict[str, Any]], dict[str, Any]], dict[str, Any]],
    *,
    worker_id: str = "",
    now: Any = None,
) -> dict[str, Any]:
    """Execute provider calls outside transactions and persist bounded evidence."""

    clock_now = (lambda: _now(now)) if now is not None else (lambda: _now())
    ts = clock_now()
    claim_token = f"manual-search-claim:{uuid.uuid4()}"
    worker_id = _text(worker_id) or "manual-search-worker"
    with _db(db_path) as con:
        ensure_schema(con)
        row = con.execute("select * from manual_search_runs where id=?", (run_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "manual_search_run_not_found"}
        if row["state"] in TERMINAL_RUN_STATES:
            return {"ok": True, "run_id": run_id, "state": row["state"], "already_terminal": True}
        reclaiming_expired_run = row["state"] == "running"
        reclaim_count = int(row["reclaim_count"] or 0)
        # A reclaimed run's lease was intentionally longer than its deadline_at
        # (a safety margin so a still-working thread isn't reclaimed out from
        # under itself), but that means by the time a lease actually expires,
        # deadline_at has almost always already elapsed too. Retrying against
        # an already-past deadline guarantees every provider is marked
        # timed_out on the very first loop iteration in the provider-wait loop
        # below -- a "Provider Timeout" that never made a real call. Give a
        # reclaimed run a genuine fresh window instead, capped at
        # MAX_RUN_RECLAIM_ATTEMPTS so a permanently-broken provider (or a
        # worker that keeps crashing mid-run) can't retry forever; once the
        # cap is hit, deadline_at is left stale on purpose so the existing
        # timeout path finalizes the run as failed rather than looping.
        new_deadline_at = row["deadline_at"]
        if reclaiming_expired_run and reclaim_count < MAX_RUN_RECLAIM_ATTEMPTS:
            reclaim_count += 1
            fresh_deadline = ts + DEFAULT_RUN_TIMEOUT_SECONDS
            if row["expires_at"]:
                fresh_deadline = min(fresh_deadline, float(row["expires_at"]))
            new_deadline_at = fresh_deadline
        updated = con.execute(
            """
            update manual_search_runs
            set state='running',started_at=coalesce(started_at,?),claim_token=?,claimed_by=?,
                lease_expires_at=?,deadline_at=?,reclaim_count=?,updated_at=?
            where id=? and cancel_requested=0 and (
              state='queued' or (state='running' and coalesce(lease_expires_at,0)<?)
            )
            """,
            (ts, claim_token, worker_id, ts + DEFAULT_RUN_LEASE_SECONDS, new_deadline_at, reclaim_count, ts, run_id, ts),
        ).rowcount
        if not updated:
            return {"ok": False, "reason": "manual_search_run_already_claimed", "state": row["state"]}
        row = con.execute("select * from manual_search_runs where id=?", (run_id,)).fetchone()
        context = _json(row["context_json"], {})
        providers = _json(row["provider_selection_json"], [])
        deadline = float(row["deadline_at"] or ts + DEFAULT_RUN_TIMEOUT_SECONDS)
        profile_id = row["source_profile_id"]
    profile = source_profile_for_series(db_path, row["series_id"], requested_profile_id=profile_id)
    pack_policy = dict(profile.get("pack_policy") or {})
    pack_policy["allowed"] = bool(row["pack_allowed"])
    provider_states: list[str] = []
    provider_timeout_seconds = max(
        1,
        min(int(profile.get("provider_timeout_seconds") or DEFAULT_PROVIDER_TIMEOUT_SECONDS), 60),
    )
    provider_timeout_by_id = {
        provider_id: (max(60, provider_timeout_seconds) if provider_id == "slskd" else provider_timeout_seconds)
        for provider_id in providers
    }
    max_concurrency = max(1, min(int(profile.get("max_concurrency") or 4), 8))
    provider_executions = {}
    prepared_at = clock_now()
    with _db(db_path) as con:
        lease_prepared = con.execute(
            "update manual_search_runs set lease_expires_at=?,updated_at=? where id=? and state='running' and cancel_requested=0 and claim_token=?",
            (prepared_at + DEFAULT_RUN_LEASE_SECONDS, prepared_at, run_id, claim_token),
        ).rowcount
        if not lease_prepared:
            prepared_state = con.execute(
                "select state from manual_search_runs where id=?",
                (run_id,),
            ).fetchone()
            if prepared_state and prepared_state["state"] == "cancelled":
                return {"ok": True, "run_id": run_id, "state": "cancelled"}
            return {"ok": False, "reason": "manual_search_run_claim_lost", "run_id": run_id}
        if reclaiming_expired_run:
            con.execute(
                """
                update manual_search_provider_attempts
                set state='provider_failure',error_code='worker_lease_expired',
                    error_detail='previous worker lease expired before provider completion',
                    completed_at=coalesce(completed_at,?),updated_at=?
                where run_id=? and state in ('planned','running')
                """,
                (prepared_at, prepared_at, run_id),
            )
        for provider_id in providers:
            max_queries = max(1, min(int(profile.get("max_queries_per_provider") or DEFAULT_MAX_QUERIES_PER_PROVIDER), 12))
            queries = manual_contract.build_query_variants(context, provider_id=provider_id, max_queries=max_queries)
            attempt_id = f"manual-attempt:{uuid.uuid4()}"
            provider_executions[provider_id] = {"queries": queries, "attempt_id": attempt_id}
            for query in queries:
                con.execute(
                    """
                    insert into manual_search_queries(id,run_id,provider_id,query_text,query_kind,ordinal,created_at)
                    values(?,?,?,?,?,?,?)
                    on conflict(run_id,provider_id,ordinal) do update
                    set query_text=excluded.query_text,query_kind=excluded.query_kind
                    """,
                    (_id("manual-query", run_id, provider_id, query["ordinal"]), run_id, provider_id, query["query"], query.get("query_kind"), query["ordinal"], prepared_at),
                )
            con.execute(
                "insert into manual_search_provider_attempts(id,run_id,provider_id,state,started_at,created_at,updated_at) values(?,?,?,?,?,?,?)",
                (attempt_id, run_id, provider_id, "running", prepared_at, prepared_at, prepared_at),
            )
    provider_pool = ThreadPoolExecutor(max_workers=min(len(providers), max_concurrency)) if providers else None
    heartbeat_interval_seconds = max(0.1, min(30.0, DEFAULT_RUN_LEASE_SECONDS / 3.0))
    last_heartbeat_monotonic = time.monotonic()

    def renew_claim_lease() -> bool:
        nonlocal last_heartbeat_monotonic
        heartbeat_at = clock_now()
        with _db(db_path) as con:
            renewed = con.execute(
                """
                update manual_search_runs
                set lease_expires_at=?,updated_at=?
                where id=? and state='running' and cancel_requested=0 and claim_token=?
                """,
                (heartbeat_at + DEFAULT_RUN_LEASE_SECONDS, heartbeat_at, run_id, claim_token),
            ).rowcount
        last_heartbeat_monotonic = time.monotonic()
        return bool(renewed)

    def invoke_provider(provider_id: str) -> dict[str, Any]:
        execution = provider_executions[provider_id]
        execution["started_monotonic"] = time.monotonic()
        try:
            return {"result": provider_runner(provider_id, context, execution["queries"], profile) or {}}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            execution["completed_monotonic"] = time.monotonic()

    pending = {}
    for provider_id in providers:
        future = provider_pool.submit(invoke_provider, provider_id)
        provider_executions[provider_id]["future"] = future
        pending[future] = provider_id

    def stop_provider_pool():
        if provider_pool is not None:
            provider_pool.shutdown(wait=False, cancel_futures=True)

    def persist_provider_result(
        provider_id: str,
        result: dict[str, Any],
        call: dict[str, Any],
        duration_ms: int,
    ) -> str:
        execution = provider_executions[provider_id]
        queries = execution["queries"]
        attempt_id = execution["attempt_id"]
        raw_candidates = list(result.get("candidates") or [])[:DEFAULT_MAX_CANDIDATES_PER_PROVIDER]
        normalized: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for raw_candidate in raw_candidates:
            public = manual_contract.normalize_candidate(
                raw_candidate,
                context,
                search_run_id=run_id,
                provider_id=provider_id,
                query_evidence=raw_candidate.get("query_evidence") if isinstance(raw_candidate, dict) else {},
                source_health=result.get("health") if isinstance(result.get("health"), dict) else {},
            )
            decision = _decision(public, context, pack_policy)
            public["accepted"] = decision["accepted"]
            public["decision"] = {key: value for key, value in decision.items() if key != "accepted"}
            normalized.append((public, raw_candidate))
        paired_by_id: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for pair in normalized:
            paired_by_id.setdefault(_text(pair[0].get("candidate_id")), []).append(pair)
        for rows in paired_by_id.values():
            rows.sort(
                key=lambda pair: (
                    bool(pair[0].get("accepted")),
                    _candidate_score(pair[0].get("match_score")),
                    bool(pair[0].get("child_source_name")),
                ),
                reverse=True,
            )
        dedup_public = {
            row["candidate_id"]: row
            for row in manual_contract.deduplicate_candidates([item[0] for item in normalized])
        }
        raw_by_id = {
            candidate_id: rows[0][1]
            for candidate_id, rows in paired_by_id.items()
            if candidate_id in dedup_public and rows
        }
        completed = clock_now()
        state = call["status"]
        provider_execution = manual_contract.safe_public_structure(result.get("diagnostics") or {})
        with _db(db_path) as con:
            con.execute("begin immediate")
            fenced_row = con.execute(
                "select cancel_requested,state,claim_token from manual_search_runs where id=?",
                (run_id,),
            ).fetchone()
            if not fenced_row or fenced_row["cancel_requested"] or fenced_row["state"] == "cancelled":
                return "cancelled"
            if fenced_row["claim_token"] != claim_token:
                return "claim_lost"
            accepted_count = 0
            rejected_count = 0
            for candidate_id, public in dedup_public.items():
                decision = public.get("decision") or {}
                accepted = bool(public.get("accepted"))
                accepted_count += int(accepted)
                rejected_count += int(not accepted)
                con.execute(
                    """
                    insert into manual_search_candidates(id,run_id,provider_attempt_id,provider_id,child_source_id,protocol,original_title,normalized_title,
                      match_score,confidence_tier,accepted,pack_candidate,pack_type,size_bytes,acquisition_capability,candidate_identity,public_json,evidence_json,created_at,updated_at)
                    values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    on conflict(run_id,candidate_identity) do update set public_json=excluded.public_json,
                      evidence_json=excluded.evidence_json, accepted=excluded.accepted, match_score=excluded.match_score, updated_at=excluded.updated_at
                    """,
                    (candidate_id, run_id, attempt_id, provider_id, public.get("child_source_id"), public.get("protocol"), public.get("original_title"), public.get("normalized_title"), public.get("match_score"), public.get("confidence_tier"), int(accepted), int(bool(public.get("pack_candidate"))), public.get("pack_type"), public.get("size_bytes"), public.get("acquisition_capability"), public.get("provider_candidate_identity") or candidate_id, _dump(public), _dump({"bounded_reference": public.get("raw_evidence_reference")}), completed, completed),
                )
                raw_attempt = (raw_by_id.get(candidate_id) or {}).get("_inkdrop_manual_attempt")
                if isinstance(raw_attempt, dict):
                    raw_attempt = dict(raw_attempt)
                    raw_attempt["candidate_binding"] = candidate_id
                    raw_attempt.setdefault(
                        "provider_candidate_identity",
                        public.get("provider_candidate_identity"),
                    )
                    raw_protocol = _text(raw_attempt.get("protocol") or public.get("protocol")).lower()
                    if raw_protocol in {"soulseek", "slskd"}:
                        raw_candidate = raw_attempt.get("raw", {}).get("candidate", {}) if isinstance(raw_attempt.get("raw"), dict) else {}
                        raw_attempt.setdefault(
                            "locator_digest",
                            _slskd_locator_digest(
                                raw_candidate.get("username"),
                                raw_candidate.get("filename"),
                                raw_candidate.get("size"),
                                raw_candidate.get("username"),
                            ),
                        )
                    else:
                        raw_attempt.setdefault("locator_digest", _locator_digest(_attempt_locator(raw_attempt, raw_protocol)))
                capsule = _private_handoff_capsule({"_inkdrop_manual_attempt": raw_attempt})
                con.execute(
                    "insert into manual_search_handoff_capsules(candidate_id,capsule_json,expires_at,created_at,updated_at) values(?,?,?,?,?) on conflict(candidate_id) do update set capsule_json=excluded.capsule_json,expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                    (candidate_id, _dump(capsule), completed + DEFAULT_RUN_RETENTION_SECONDS, completed, completed),
                )
                con.execute("delete from manual_search_candidate_decisions where candidate_id=?", (candidate_id,))
                con.execute("insert into manual_search_candidate_decisions(id,candidate_id,decision,positive_evidence_json,negative_evidence_json,rejection_codes_json,explanation,policy_json,created_at) values(?,?,?,?,?,?,?,?,?)", (_id("manual-decision", candidate_id, completed), candidate_id, decision.get("decision") or ("accepted" if accepted else "rejected"), _dump(decision.get("positive_evidence") or []), _dump(decision.get("negative_evidence") or []), _dump(decision.get("rejection_codes") or []), decision.get("explanation"), _dump(pack_policy), completed))
            diagnostics = {
                "queries_planned": len(queries),
                "result_count": len(raw_candidates),
                "normalized_count": len(dedup_public),
                "rejection_counts": {"accepted": accepted_count, "rejected": rejected_count},
                "provider_diagnostics_contract_version": provider_execution.get("contract_version"),
                "provider_rows_considered": provider_execution.get("provider_rows_considered"),
                "provider_rows": provider_execution.get("provider_rows") or [],
            }
            con.execute("update manual_search_provider_attempts set state=?,duration_ms=?,result_count=?,normalized_count=?,accepted_count=?,rejected_count=?,error_code=?,error_detail=?,health_json=?,diagnostics_json=?,completed_at=?,updated_at=? where id=? and state='running'", (state, duration_ms, len(raw_candidates), len(dedup_public), accepted_count, rejected_count, state if state in {"provider_failure", "provider_timeout"} else None, call.get("error_summary"), _dump(manual_contract.safe_health_snapshot(result.get("health") or {})), _dump(diagnostics), completed, completed, attempt_id))
            con.execute(
                "update manual_search_runs set updated_at=? where id=? and state='running' and claim_token=?",
                (completed, run_id, claim_token),
            )
        return state

    provider_position = {provider_id: index for index, provider_id in enumerate(providers)}
    while pending:
        if time.monotonic() - last_heartbeat_monotonic >= heartbeat_interval_seconds:
            if not renew_claim_lease():
                stop_provider_pool()
                current_run = get_search_run(db_path, run_id)
                if current_run.get("state") == "cancelled" or (current_run.get("run") or {}).get("state") == "cancelled":
                    return {"ok": True, "run_id": run_id, "state": "cancelled"}
                return {"ok": False, "reason": "manual_search_run_claim_lost", "run_id": run_id}
        with _db(db_path, read_only=True) as con:
            current_row = con.execute("select cancel_requested,state,claim_token from manual_search_runs where id=?", (run_id,)).fetchone()
        if not current_row or current_row["cancel_requested"] or current_row["state"] == "cancelled":
            stop_provider_pool()
            return {"ok": True, "run_id": run_id, "state": "cancelled"}
        if current_row["claim_token"] != claim_token:
            stop_provider_pool()
            return {"ok": False, "reason": "manual_search_run_claim_lost", "run_id": run_id}

        wall_now = clock_now()
        monotonic_now = time.monotonic()
        ready = {future for future in pending if future.done()}
        timed_out = set()
        for future, provider_id in pending.items():
            if future in ready:
                continue
            started = provider_executions[provider_id].get("started_monotonic")
            if wall_now >= deadline or (
                started is not None and monotonic_now - started >= provider_timeout_by_id[provider_id]
            ):
                timed_out.add(future)

        if not ready and not timed_out:
            wait_seconds = min(0.1, max(0.0, deadline - wall_now))
            running_remaining = [
                provider_timeout_by_id[pending[execution["future"]]] - (monotonic_now - execution["started_monotonic"])
                for execution in provider_executions.values()
                if execution.get("future") in pending and execution.get("started_monotonic") is not None
            ]
            if running_remaining:
                wait_seconds = min(wait_seconds, max(0.0, min(running_remaining)))
            if wait_seconds <= 0:
                continue
            wait(set(pending), timeout=wait_seconds, return_when=FIRST_COMPLETED)
            continue

        for future in sorted(ready, key=lambda item: provider_position[pending[item]]):
            provider_id = pending.pop(future)
            execution = provider_executions[provider_id]
            payload = future.result()
            if payload.get("error"):
                result = {"error": payload["error"], "candidates": []}
            else:
                result = payload.get("result") or {}
            raw_candidates = list(result.get("candidates") or [])[:DEFAULT_MAX_CANDIDATES_PER_PROVIDER]
            call = manual_contract.classify_provider_call(
                completed=bool(result.get("completed", True)),
                result_count=len(raw_candidates),
                error=result.get("error"),
            )
            duration_ms = int(
                (execution.get("completed_monotonic", time.monotonic()) - execution["started_monotonic"])
                * 1000
            )
            persisted = persist_provider_result(provider_id, result, call, duration_ms)
            if persisted == "cancelled":
                stop_provider_pool()
                return {"ok": True, "run_id": run_id, "state": "cancelled"}
            if persisted == "claim_lost":
                stop_provider_pool()
                return {"ok": False, "reason": "manual_search_run_claim_lost", "run_id": run_id}
            provider_states.append(persisted)

        for future in sorted(timed_out - ready, key=lambda item: provider_position[pending[item]]):
            provider_id = pending.pop(future)
            execution = provider_executions[provider_id]
            future.cancel()
            result = {"error": "provider timeout", "candidates": []}
            call = manual_contract.classify_provider_call(completed=False, error=result["error"])
            started = execution.get("started_monotonic", monotonic_now)
            duration_ms = int((monotonic_now - started) * 1000)
            persisted = persist_provider_result(provider_id, result, call, duration_ms)
            if persisted == "cancelled":
                stop_provider_pool()
                return {"ok": True, "run_id": run_id, "state": "cancelled"}
            if persisted == "claim_lost":
                stop_provider_pool()
                return {"ok": False, "reason": "manual_search_run_claim_lost", "run_id": run_id}
            provider_states.append(persisted)
    stop_provider_pool()
    final_state = "completed"
    if provider_states and all(state in {"provider_failure", "provider_timeout"} for state in provider_states):
        final_state = "failed"
    elif any(state in {"provider_failure", "provider_timeout"} for state in provider_states):
        final_state = "partial"
    completed = clock_now()
    with _db(db_path) as con:
        updated = con.execute(
            """
            update manual_search_runs
            set state=?,claim_token=null,claimed_by=null,lease_expires_at=null,completed_at=?,updated_at=?
            where id=? and state!='cancelled' and claim_token=?
            """,
            (final_state, completed, completed, run_id, claim_token),
        )
        if updated.rowcount:
            _record_history(
                con,
                event_type="manual_search_completed",
                entity_type="search_run",
                entity_id=run_id,
                series_id=row["series_id"],
                issue_id=row["issue_id"],
                message=f"Manual Search {final_state}",
                outcome=final_state,
                payload={"provider_states": provider_states},
                now=completed,
            )
    return get_search_run(db_path, run_id)


_MANUAL_GRAB_QUEUE_TERMINAL_STATES = {
    "verified", "satisfied", "superseded_duplicate", "removed", "ignored", "inactive",
}
_MANUAL_GRAB_TASK_ACTIVE_STATES = {
    "queued", "downloading", "stalled_downloading", "active_download", "completed_in_client",
    "ready_to_import", "import_ready", "importing", "waiting_for_library_scan",
    "waiting_for_kavita_scan", "verifying",
}
_MANUAL_GRAB_TASK_ACTIVE_STATUSES = {
    "sent", "download_started", "downloading", "started_waiting", "already_downloading",
    "waiting_for_transfer", "waiting_for_complete_source", "transfer_in_progress",
    "transfer_settling", "waiting_for_staged_file", "staged_file_settling", "staged_file_ready",
    "preview_importable", "ready_import", "import_busy", "verification_pending",
    "imported_not_resolved", "completed_in_client", "ready_to_import",
    "waiting_for_library_scan", "waiting_for_kavita_scan",
}
_MANUAL_GRAB_TASK_ACTIVE_PHASES = {"downloading", "staged_or_importing", "verifying"}
_MANUAL_GRAB_TASK_TERMINAL_STATES = {
    "verified", "completed", "complete", "succeeded", "failed", "blocked", "cancelled",
    "canceled", "retired", "superseded", "removed",
}
_MANUAL_GRAB_TASK_TERMINAL_STATUSES = {
    "completed", "complete", "succeeded", "success", "resolved", "already_verified",
    "verified", "folder_verified", "library_visible", "kavita_verified", "queue_verified",
    "queue_satisfied", "queue_superseded", "queue_inactive", "queue_stale_source_absent",
    "removed_by_user",
}
_MANUAL_GRAB_TASK_TERMINAL_PHASES = {
    "verified", "completed", "succeeded", "failed_candidate", "failed", "blocked",
    "cancelled", "canceled", "retired", "superseded", "removed",
}


def _manual_grab_result_reusable(con, grab_row, candidate_identity=""):
    grab = dict(grab_row or {})
    if str(grab.get("state") or "").strip().lower() not in {
        "handoff_pending", "queued_for_handoff", "handed_off",
    }:
        return True
    queue_id = str(grab.get("queue_id") or "").strip()
    if not queue_id:
        return True
    queue = con.execute("select active,state from queue_items where id=? limit 1", (queue_id,)).fetchone()
    if not queue:
        return True
    if not int(queue["active"] or 0) or str(queue["state"] or "").strip().lower() in _MANUAL_GRAB_QUEUE_TERMINAL_STATES:
        return True
    clauses = []
    params = []
    for column, value in (
        ("id", grab.get("download_task_id")),
        ("source_attempt_id", grab.get("source_attempt_id")),
        ("candidate_identity", candidate_identity),
    ):
        value = str(value or "").strip()
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if not clauses:
        return False
    rows = con.execute(
        f"""select state,status,lifecycle_phase from download_tasks
            where queue_id=? and ({' or '.join(clauses)})
            order by coalesce(updated_at,started_at,0) desc limit 20""",
        (queue_id, *params),
    ).fetchall()
    for row in rows:
        state = str(row["state"] or "").strip().lower()
        status = str(row["status"] or "").strip().lower()
        phase = str(row["lifecycle_phase"] or "").strip().lower()
        if (
            state in _MANUAL_GRAB_TASK_TERMINAL_STATES
            or status in _MANUAL_GRAB_TASK_TERMINAL_STATUSES
            or phase in _MANUAL_GRAB_TASK_TERMINAL_PHASES
            or any(token in status for token in ("fail", "error", "blocked", "stale"))
        ):
            continue
        if (
            state in _MANUAL_GRAB_TASK_ACTIVE_STATES
            or status in _MANUAL_GRAB_TASK_ACTIVE_STATUSES
            or phase in _MANUAL_GRAB_TASK_ACTIVE_PHASES
        ):
            return True
    return False


def safe_grab_candidate(
    db_path: str | Path,
    candidate_id: str,
    grab_runner: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    *,
    requested_by: str = "",
    override_pack_warning: bool = False,
    override_hard_limit: bool = False,
    hard_limit_override_authorized: bool = False,
    force_rejected: bool = False,
    force_rejected_authorized: bool = False,
    confirm_rejected_risk: bool = False,
    now: Any = None,
) -> dict[str, Any]:
    ts = _now(now)
    rearmed_grab_id = None
    with _db(db_path) as con:
        con.execute("begin immediate")
        candidate = con.execute("select * from manual_search_candidates where id=?", (candidate_id,)).fetchone()
        if not candidate:
            return {"ok": False, "reason": "manual_search_candidate_not_found"}
        public = _json(candidate["public_json"], {})
        decision = public.get("decision") if isinstance(public.get("decision"), dict) else {}
        if force_rejected and not force_rejected_authorized:
            return {"ok": False, "reason": "admin_required_for_rejected_candidate_override", "admin_override_required": True}
        if force_rejected and not confirm_rejected_risk:
            return {
                "ok": False,
                "reason": "rejected_candidate_risk_confirmation_required",
                "explicit_confirmation_required": True,
                "rejection_codes": decision.get("rejection_codes") or decision.get("negative_evidence") or [],
            }
        if override_hard_limit and not hard_limit_override_authorized:
            return {"ok": False, "reason": "admin_required_for_hard_limit_override", "admin_override_required": True}
        existing = con.execute("select * from manual_search_grab_results where candidate_id=?", (candidate_id,)).fetchone()
        equivalent_existing = None if existing else con.execute(
            """select mgr.*
               from manual_search_grab_results mgr
               join manual_search_candidates previous on previous.id=mgr.candidate_id
               join manual_search_runs previous_run on previous_run.id=previous.run_id
               join manual_search_runs current_run on current_run.id=?
               where previous.candidate_identity=?
                 and previous.provider_id=?
                 and coalesce(previous.protocol,'')=coalesce(?, '')
                 and previous_run.series_id=current_run.series_id
                 and coalesce(previous_run.issue_id,'')=coalesce(current_run.issue_id,'')
                 and mgr.state not in ('failed','assisted_required')
               order by mgr.created_at desc limit 1""",
            (candidate["run_id"], candidate["candidate_identity"], candidate["provider_id"], candidate["protocol"]),
        ).fetchone()
        prior_forced_result = existing if existing and existing["forced_rejected"] else equivalent_existing if equivalent_existing and equivalent_existing["forced_rejected"] else None
        if prior_forced_result:
            if not force_rejected_authorized:
                return {"ok": False, "reason": "admin_required_for_rejected_candidate_override", "admin_override_required": True}
            if not force_rejected or not confirm_rejected_risk:
                return {"ok": False, "reason": "rejected_candidate_risk_confirmation_required", "explicit_confirmation_required": True}
        # The automatic pipeline may already be downloading this exact issue.
        # Without this gate a manual grab and an automatic grab both went live
        # for the same Wanted item -- confirmed cross-provider by audit
        # (PASS16-CORE-P1-01: a Prowlarr+SABnzbd manual grab racing a Standard
        # Ebooks automatic grab, both active). Deliberately conservative
        # clause, kept self-contained: an active queue row in a work state
        # with a task that is neither failed nor blocked counts as live.
        run_scope = con.execute(
            "select series_id, issue_id from manual_search_runs where id=?",
            (candidate["run_id"],),
        ).fetchone()
        # Re-grabbing the same (or an equivalent) candidate is the idempotent
        # reuse path -- the active work it finds is its OWN handoff, not a
        # race. The gate only applies to a grab that would create new work.
        if run_scope and run_scope["issue_id"] and not existing and not equivalent_existing:
            active_download = con.execute(
                """
                select q.id as queue_id, dt.id as task_id, dt.download_client, dt.state as task_state
                from queue_items q
                join download_tasks dt on dt.queue_id = q.id
                where q.issue_id = ?
                  and q.active = 1
                  and lower(coalesce(q.state, '')) in ('downloading', 'importing', 'import_ready', 'queued')
                  and dt.state in ('queued', 'downloading', 'import_ready', 'importing')
                  and lower(coalesce(dt.status, '')) not like '%fail%'
                  and lower(coalesce(dt.status, '')) not like '%blocked%'
                  and lower(coalesce(dt.status, '')) not like '%stale%'
                limit 1
                """,
                (run_scope["issue_id"],),
            ).fetchone()
            if active_download:
                return {
                    "ok": False,
                    "reason": "active_download_in_progress",
                    "detail": "This issue already has a download running; grabbing again would fetch it twice.",
                    "active_queue_id": active_download["queue_id"],
                    "active_download_client": active_download["download_client"],
                    "active_task_state": active_download["task_state"],
                }
        capsule_row = con.execute(
            "select capsule_json from manual_search_handoff_capsules where candidate_id=? and expires_at>=?",
            (candidate_id, ts),
        ).fetchone()
        handoff_capsule = _json(capsule_row["capsule_json"], {}) if capsule_row else {}
        capsule_gate = _handoff_capsule_binding_gate(public, handoff_capsule)
        if not capsule_gate.get("eligible"):
            return {"ok": False, **capsule_gate}
        current_binding = _handoff_binding(public, handoff_capsule)
        force_gate = forced_grab_candidate_gate(public, handoff_capsule) if force_rejected else {}
        if force_rejected and not force_gate.get("eligible"):
            return {"ok": False, **force_gate}
        if existing:
            if existing["forced_rejected"]:
                if not force_rejected_authorized:
                    return {"ok": False, "reason": "admin_required_for_rejected_candidate_override", "admin_override_required": True}
                if not force_rejected or not confirm_rejected_risk:
                    return {"ok": False, "reason": "rejected_candidate_risk_confirmation_required", "explicit_confirmation_required": True}
            if existing["handoff_binding"] and existing["handoff_binding"] != current_binding:
                return {"ok": False, "reason": "candidate_handoff_binding_mismatch"}
            if not existing["handoff_binding"] and _text(candidate["protocol"]).lower() in FORCED_GRAB_PROTOCOLS:
                return {"ok": False, "reason": "candidate_handoff_binding_unavailable"}
            if not candidate["accepted"] and not force_rejected:
                return {"ok": False, "reason": "candidate_not_accepted", "rejection_codes": public.get("rejection_codes") or decision.get("rejection_codes") or []}
            if _manual_grab_result_reusable(con, existing, candidate["candidate_identity"]):
                return {"ok": True, "idempotent_reuse": True, "grab_result": _public_grab(existing)}
            rearmed_grab_id = existing["id"]
        if not candidate["accepted"] and not force_rejected:
            return {"ok": False, "reason": "candidate_not_accepted", "rejection_codes": public.get("rejection_codes") or decision.get("rejection_codes") or []}
        equivalent_existing = None if rearmed_grab_id else equivalent_existing
        if equivalent_existing:
            if equivalent_existing["forced_rejected"]:
                if not force_rejected_authorized:
                    return {"ok": False, "reason": "admin_required_for_rejected_candidate_override", "admin_override_required": True}
                if not force_rejected or not confirm_rejected_risk:
                    return {"ok": False, "reason": "rejected_candidate_risk_confirmation_required", "explicit_confirmation_required": True}
            if equivalent_existing["handoff_binding"] and equivalent_existing["handoff_binding"] != current_binding:
                return {"ok": False, "reason": "candidate_handoff_binding_mismatch"}
            if not equivalent_existing["handoff_binding"] and _text(candidate["protocol"]).lower() in FORCED_GRAB_PROTOCOLS:
                return {"ok": False, "reason": "candidate_handoff_binding_unavailable"}
            if _manual_grab_result_reusable(con, equivalent_existing, candidate["candidate_identity"]):
                return {"ok": True, "idempotent_reuse": True, "grab_result": _public_grab(equivalent_existing)}
        negative = set(decision.get("negative_evidence") or [])
        if public.get("acquisition_capability") == "assisted":
            state = "assisted_required"
            result = {"ok": False, "state": state, "reason": "manual_interaction_required", "manual_inbox_guidance": True}
        elif override_hard_limit and not hard_limit_override_authorized:
            return {"ok": False, "reason": "admin_required_for_hard_limit_override", "admin_override_required": True}
        elif "pack_hard_limit_exceeded" in negative and not override_hard_limit:
            return {"ok": False, "reason": "pack_hard_limit_exceeded", "admin_override_required": True}
        elif "pack_size_warning" in negative and not override_pack_warning:
            return {"ok": False, "reason": "pack_size_warning", "explicit_override_required": True}
        elif not candidate["accepted"] and not force_rejected:
            return {"ok": False, "reason": "candidate_not_accepted", "rejection_codes": public.get("rejection_codes") or decision.get("rejection_codes") or []}
        else:
            state = "handoff_pending"
            result = None
        grab_id = rearmed_grab_id or f"manual-grab:{uuid.uuid4()}"
        if rearmed_grab_id:
            con.execute(
                """update manual_search_grab_results
                   set state=?,requested_by=?,handoff_binding=?,queue_id=null,source_attempt_id=null,
                       download_task_id=null,client_item_id=null,handoff_json='{}',error_code=null,error_detail=null,updated_at=?
                   where id=?""",
                (state, manual_contract.redacted_text(requested_by, 120), current_binding, ts, grab_id),
            )
        else:
            try:
                con.execute("insert into manual_search_grab_results(id,candidate_id,run_id,state,requested_by,forced_rejected,handoff_binding,created_at,updated_at) values(?,?,?,?,?,?,?,?,?)", (grab_id, candidate_id, candidate["run_id"], state, manual_contract.redacted_text(requested_by, 120), int(bool(force_rejected)), current_binding, ts, ts))
            except sqlite3.IntegrityError:
                existing = con.execute("select * from manual_search_grab_results where candidate_id=?", (candidate_id,)).fetchone()
                if existing:
                    if existing["forced_rejected"] and not force_rejected_authorized:
                        return {"ok": False, "reason": "admin_required_for_rejected_candidate_override", "admin_override_required": True}
                    if existing["handoff_binding"] and existing["handoff_binding"] != current_binding:
                        return {"ok": False, "reason": "candidate_handoff_binding_mismatch"}
                    return {"ok": True, "idempotent_reuse": True, "grab_result": _public_grab(existing)}
                raise
        run = con.execute("select series_id,issue_id from manual_search_runs where id=?", (candidate["run_id"],)).fetchone()
        forced_reasons = (force_gate.get("rejection_codes") or []) if force_rejected else []
        _record_history(
            con,
            event_type="manual_search_forced_grab_requested" if force_rejected else "manual_search_grab_requested",
            entity_type="candidate",
            entity_id=candidate_id,
            series_id=run["series_id"] if run else "",
            issue_id=run["issue_id"] if run else "",
            message="Manual Search rejected candidate force-grab requested" if force_rejected else "Manual Search candidate grab requested",
            outcome=state,
            payload={
                "provider_id": public.get("provider_id"),
                "protocol": public.get("protocol"),
                "forced_rejected_candidate": bool(force_rejected),
                "rejection_codes": forced_reasons,
                "actor": manual_contract.redacted_text(requested_by or "authenticated", 120),
                "rearmed_stale_handoff": bool(rearmed_grab_id),
            },
            now=ts,
        )
    if result is None:
        try:
            result = grab_runner(public, handoff_capsule) or {}
        except Exception as exc:
            result = {"ok": False, "state": "failed", "reason": manual_contract.redacted_text(f"{type(exc).__name__}: {exc}", 240)}
    final_state = _text(result.get("state") or ("handed_off" if result.get("ok") else "failed"))
    with _db(db_path) as con:
        safe_result = _safe_grab_summary(result)
        con.execute("update manual_search_grab_results set state=?,queue_id=?,source_attempt_id=?,download_task_id=?,client_item_id=?,handoff_json=?,error_code=?,error_detail=?,updated_at=? where id=?", (final_state, result.get("queue_id"), result.get("source_attempt_id"), result.get("download_task_id"), result.get("client_item_id"), _dump(safe_result), None if result.get("ok") else manual_contract.redacted_text(result.get("reason"), 160), None if result.get("ok") else manual_contract.redacted_text(result.get("detail"), 240), _now(), grab_id))
        saved = con.execute("select * from manual_search_grab_results where id=?", (grab_id,)).fetchone()
    return {"ok": bool(result.get("ok")), "grab_result": _public_grab(saved), "result": safe_result}


def process_pending_search_runs(
    db_path: str | Path,
    provider_runner: Callable[[str, dict[str, Any], list[dict[str, Any]], dict[str, Any]], dict[str, Any]],
    *,
    limit: int = 3,
    worker_id: str = "manual-search-worker",
    now: Any = None,
) -> dict[str, Any]:
    ts = _now(now)
    limit = max(1, min(int(limit), 20))
    with _db(db_path, read_only=True) as con:
        rows = con.execute(
            """
            select id from manual_search_runs
            where cancel_requested=0 and (
              state='queued' or (state='running' and coalesce(lease_expires_at,0)<?)
            )
            order by created_at,id limit ?
            """,
            (ts, limit),
        ).fetchall()
    results = []
    for row in rows:
        try:
            results.append(
                process_search_run(
                    db_path,
                    row["id"],
                    provider_runner,
                    worker_id=worker_id,
                    now=now,
                )
            )
        except Exception as exc:
            fail_search_run(db_path, row["id"], exc, now=now)
            results.append({"ok": False, "run_id": row["id"], "reason": "manual_search_worker_error"})
    return {"ok": all(row.get("ok") for row in results), "processed": len(results), "results": results}


def cleanup_search_runs(db_path: str | Path, *, now: Any = None, limit: int = 100) -> dict[str, Any]:
    ts = _now(now)
    limit = max(1, min(int(limit), 1000))
    # Look before touching the write lock: this runs every 30 seconds and
    # usually has nothing to do, yet the unconditional delete queued behind
    # every busy writer and raised on contention -- 54 lock waits and 50
    # tracebacks in one observation window, the worker's dominant lock cost.
    try:
        with _db(db_path, read_only=True) as read_con:
            has_capsules = read_con.execute(
                "select 1 from manual_search_handoff_capsules where expires_at<? limit 1", (ts,)
            ).fetchone()
            has_stale = read_con.execute(
                "select 1 from manual_search_runs where expires_at<? limit 1", (ts,)
            ).fetchone()
        if not has_capsules and not has_stale:
            return {"ok": True, "skipped": "nothing_expired", "examined": 0, "capsules_deleted": 0, "expired": 0, "deleted": 0, "retained": 0}
    except sqlite3.OperationalError:
        pass
    try:
        return _cleanup_search_runs_write(db_path, ts, limit)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise
        # Contention is a normal outcome for a half-minute housekeeping
        # cycle, not a crash: report it and let the next cycle retry.
        return {"ok": False, "deferred": True, "reason": "state_db_busy", "examined": 0, "capsules_deleted": 0, "expired": 0, "deleted": 0, "retained": 0}


def _cleanup_search_runs_write(db_path: str | Path, ts: float, limit: int) -> dict[str, Any]:
    with _db(db_path) as con:
        capsules_deleted = con.execute(
            "delete from manual_search_handoff_capsules where candidate_id in (select candidate_id from manual_search_handoff_capsules where expires_at<? order by expires_at limit ?)",
            (ts, limit),
        ).rowcount
        stale = con.execute("select id,state from manual_search_runs where expires_at<? order by expires_at limit ?", (ts, limit)).fetchall()
        expired = 0
        deleted = 0
        retained = 0
        for row in stale:
            if row["state"] not in TERMINAL_RUN_STATES:
                con.execute("update manual_search_runs set state='expired',completed_at=?,updated_at=? where id=?", (ts, ts, row["id"]))
                expired += 1
                continue
            # A run with a grab result is an audit anchor -- its FK is NO
            # ACTION on purpose, and deleting it blindly crash-looped the
            # worker (PASS17-MANUAL-P1-01). But 'retained' must not mean
            # 'forever': the first version parked these runs at
            # expires_at=NULL, which made every successful grab's ~456KB of
            # candidates, attempts and queries immortal (PASS18-DB-P2-01).
            # Retention is anchored to the grab's own age: within the window
            # the run's expiry is pushed to grab_created + window (leaving
            # the stale set, deterministically, no marker column needed);
            # past the window the grab audit itself is purged first so the
            # run can finally delete, children cascading.
            newest_grab = con.execute(
                "select max(created_at) as created_at from manual_search_grab_results where run_id=?",
                (row["id"],),
            ).fetchone()
            grab_created = float(newest_grab["created_at"] or 0) if newest_grab else 0
            if grab_created:
                purge_after = grab_created + GRAB_AUDIT_RETENTION_SECONDS
                if ts < purge_after:
                    con.execute(
                        "update manual_search_runs set expires_at=?, updated_at=? where id=?",
                        (purge_after, ts, row["id"]),
                    )
                    retained += 1
                    continue
                con.execute("delete from manual_search_grab_results where run_id=?", (row["id"],))
            try:
                con.execute("delete from manual_search_runs where id=?", (row["id"],))
                deleted += 1
            except sqlite3.IntegrityError:
                # Some other referent (future schema) -- same treatment, and
                # crucially the pass keeps its other work either way.
                con.execute("update manual_search_runs set expires_at=null, updated_at=? where id=?", (ts, row["id"]))
                retained += 1
    return {"ok": True, "examined": len(stale), "expired": expired, "deleted": deleted, "retained": retained, "capsules_deleted": capsules_deleted, "bounded": len(stale) >= limit or capsules_deleted >= limit}
