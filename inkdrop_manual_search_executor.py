"""Adapter bridge for durable Manual Search runs.

The bridge deliberately reuses settings-backed source jobs. Provider network
calls remain outside SQLite transactions and normal queue recording is deferred
until an operator explicitly grabs a retained candidate.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import time
from typing import Any

import inkdrop_source_registry
import inkdrop_source_worker_cli
import inkdrop_source_worker_http
import inkdrop_source_worker_jobs
import inkdrop_source_worker_plan


def _text(value: Any) -> str:
    """Identical to inkdrop_manual_search_core._text, deliberately duplicated.

    The identity strings produced here are compared against that module's
    normalization in _handoff_capsule_binding_gate. If the two ever diverge the
    gate starts refusing correctly-paired rows, so they must collapse whitespace
    the same way. Defined locally rather than imported because core imports this
    module's siblings and a cross-import risks a cycle.
    """
    return re.sub(r"\s+", " ", str(value or "").strip())


def _provider_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _provider_matches(row: dict[str, Any], requested: str) -> bool:
    requested = _provider_key(requested)
    values = {
        _provider_key(row.get("provider_id")),
        _provider_key(row.get("source_kind")),
        _provider_key(row.get("adapter_family")),
        _provider_key(row.get("provider_type")),
    }
    if requested in values:
        return True
    return any(requested and (value.startswith(requested + "_") or requested in value) for value in values)


def provider_rows(db_path: str | Path, provider_id: str, *, registry_loader=None) -> list[dict[str, Any]]:
    requested = _provider_key(provider_id)
    # A manual search is allowed to discover through configured Prowlarr child
    # indexers even when an automatic-acquisition safety gate has disabled that
    # child.  Discovery remains read-only; the normal verdict and grab boundary
    # still decide whether the retained result may be acquired.  Using the
    # configured row also prevents a historical source-memory shadow row from
    # replacing the executable Prowlarr adapter contract.
    include_disabled = requested == "prowlarr"
    if registry_loader is not None:
        rows = registry_loader(include_disabled)
    else:
        rows = inkdrop_source_registry.registry_from_db(
            db_path,
            include_disabled=include_disabled,
        )
    matched = [dict(row) for row in rows if _provider_matches(dict(row), requested)]
    if requested != "prowlarr":
        return matched
    executable = []
    for row in matched:
        if row.get("provider_id") == "prowlarr":
            executable.append(row)
            continue
        if not (
            _provider_key(row.get("source_kind")) == "prowlarr_indexer"
            and _provider_key(row.get("implementation_status")) == "implemented"
            and bool((row.get("policy") or {}).get("indexer_ids"))
        ):
            continue
        if _provider_key(row.get("registry_state")) == "disabled":
            row["manual_discovery_only"] = True
        executable.append(row)
    return executable


def _http_client(db_path: str | Path, *, timeout_seconds: int = 25, deadline_monotonic: float | None = None):
    secrets = inkdrop_source_worker_cli.provider_secret_values(db_path)
    configured_timeout = max(1, min(int(timeout_seconds or 25), 60))

    def _client(request):
        remaining = configured_timeout
        if deadline_monotonic is not None:
            remaining = deadline_monotonic - time.monotonic()
            if remaining < 1:
                raise inkdrop_source_worker_http.SourceHttpError(
                    "provider_timeout", "provider execution deadline exhausted", request=request
                )
        client = inkdrop_source_worker_http.make_source_http_get(
            secret_values=secrets,
            allow_request_hosts=True,
            timeout_seconds=min(configured_timeout, remaining),
            max_bytes=10 * 1024 * 1024,
        )
        return client(request)

    return _client


def _inner_provider_deadline(timeout_seconds: Any) -> tuple[int, float]:
    """Reserve time for projection/persistence before the outer run expires."""

    timeout = max(1, min(int(timeout_seconds or 25), 60))
    reserve = min(2.0, max(0.25, timeout * 0.1))
    return timeout, time.monotonic() + max(0.25, timeout - reserve)


def _manual_prowlarr_row_rank(row: dict[str, Any], context: dict[str, Any]) -> tuple[int, int, int, int, int, int, str]:
    """Put media-scoped child lanes ahead of the slow aggregate lane."""

    row = row if isinstance(row, dict) else {}
    policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
    scope = _provider_key(policy.get("scope_policy"))
    media_type = _provider_key(context.get("media_type"))
    if media_type == "manga":
        scope_rank = 0 if "manga" in scope else (1 if not scope else 2)
    else:
        scope_rank = 0 if ("western" in scope or "comic" in scope) else (1 if not scope else 2)
    # Authenticated/private indexers generally have the highest signal and
    # should not queue behind several public retired templates during a
    # bounded operator search.
    aggregate_rank = 1 if _provider_key(row.get("provider_id")) == "prowlarr" else 0
    account_rank = 0 if bool(policy.get("requires_account") or row.get("requires_account")) else 1
    discovery_rank = 0 if bool(row.get("manual_discovery_only")) else 1
    state_rank = 0 if _provider_key(row.get("registry_state")) == "ready" else 1
    try:
        priority = int(row.get("priority") or 100)
    except Exception:
        priority = 100
    return scope_rank, aggregate_rank, account_rank, discovery_rank, state_rank, priority, _provider_key(row.get("provider_id"))


def _wanted_context(context: dict[str, Any], queries: list[dict[str, Any]]) -> dict[str, Any]:
    wanted = {
        "series_title": context.get("canonical_work_title"),
        "title": context.get("canonical_work_title"),
        "publication_title": context.get("publication_title"),
        "series_query_aliases": list(context.get("aliases") or []),
        "creator": list(context.get("creators") or []),
        "publisher": context.get("publisher"),
        "year": context.get("publication_year"),
        "language": context.get("language"),
        "media_type": context.get("media_type"),
        "unit_type": context.get("unit_type"),
        "issue_number": context.get("unit_number"),
        "chapter_number": context.get("unit_number") if context.get("unit_type") == "chapter" else "",
        "volume_number": context.get("volume_number"),
        "pack_allowed": bool(context.get("pack_allowed")),
        "query": (queries or [{}])[0].get("query") or context.get("canonical_work_title"),
        "manual_search_queries": [row.get("query") for row in queries if row.get("query")],
        "manual_search": True,
    }
    for key in (
        "canonical_issue_count", "metadata_issue_count", "singleton_metadata_trusted",
        "singleton_metadata_fresh", "singleton_issue_metadata_trusted", "singleton_issue_proof",
        "singleton_issue_proof_source", "collected_singleton_wanted_count",
        "collected_singleton_markers", "collected_singleton_title_aliases",
        "collected_singleton_proof", "collected_singleton_proof_source",
    ):
        wanted[key] = context.get(key)
    return wanted


def _candidate_rows(job_result: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for runtime in job_result.get("runtime_results") or []:
        candidates = list(runtime.get("candidates") or [])
        verdicts = list(runtime.get("verdicts") or [])
        attempts = list(runtime.get("attempts") or [])
        # Attempts are paired by identity, not by ordinal. They used to be
        # zipped positionally, which is only correct while the three lists stay
        # the same length -- and select_auto_send_attempts rebuilds `attempts`
        # without its suppressed entries while leaving `candidates` and
        # `verdicts` full length. From the first suppressed index onward every
        # row then carried the NEXT candidate's attempt, so grabbing a result
        # could dispatch a different candidate's locator.
        # Index on every identity an attempt can carry, and remember which keys
        # more than one attempt claims. Two distinct attempts under one key is
        # exactly the ambiguity that made the old positional pairing dangerous,
        # so those keys must refuse rather than hand back whichever attempt was
        # seen first.
        attempts_by_identity: dict[str, dict[str, Any]] = {}
        ambiguous_identity_keys: set[str] = set()
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            for key in (
                _text(attempt.get("provider_candidate_identity")),
                _text(attempt.get("candidate_identity")),
                _text(attempt.get("candidate_binding")),
            ):
                if not key:
                    continue
                prior = attempts_by_identity.get(key)
                if prior is None:
                    attempts_by_identity[key] = attempt
                elif prior is not attempt:
                    ambiguous_identity_keys.add(key)

        def attempt_for_identity(key):
            if not key or key in ambiguous_identity_keys:
                return None
            return attempts_by_identity.get(key)

        # Attempt-side ambiguity above is not enough on its own, because
        # select_auto_send_attempts prunes `attempts` while leaving `candidates`
        # and `verdicts` at full length. Two candidates that legitimately share a
        # family identity -- two mirrors of one release -- come out of that
        # pruning as two rows competing for ONE surviving attempt. The key then
        # looks unique, and the single locator gets attached to both rows: a
        # silent wrong dispatch, which is worse than the refusal this join was
        # written to prevent.
        #
        # So a family identity is only usable as executable authority when
        # exactly one candidate in this batch carries it. It stays fine as a
        # lookup key; it just cannot decide WHICH row gets the locator when more
        # than one row answers to it.
        family_identity_counts: dict[str, int] = {}
        for other in candidates:
            key = _text((other or {}).get("candidate_identity"))
            if key:
                family_identity_counts[key] = family_identity_counts.get(key, 0) + 1
        for index, candidate in enumerate(candidates):
            row = dict(candidate or {})
            verdict = verdicts[index] if index < len(verdicts) and isinstance(verdicts[index], dict) else {}
            merged_identity = _text(
                (verdict or {}).get("provider_candidate_identity")
                or (candidate or {}).get("provider_candidate_identity")
            )
            merged_candidate_id = _text(
                (verdict or {}).get("candidate_id") or (candidate or {}).get("candidate_id")
            )
            # candidate_identity has to be a lookup key, not only an index key.
            # inkdrop_source_worker_runtime normalization (see its
            # provider_candidate_identity / candidate_family_identity
            # assignment) stashes the parser's original identity in
            # provider_candidate_identity and then OVERWRITES candidate_identity
            # with the family identity, so after normalization the two fields
            # hold different values. Matching only on provider_candidate_identity
            # therefore joins Prowlarr-shaped rows and silently fails to join
            # every family that goes through that normalizer -- direct, page
            # pack, MangaDex, Suwayomi, external tool -- even when the candidate
            # and its attempt line up perfectly. Those grabs are then refused as
            # provider_grab_contract_unavailable, which is safe but wrong.
            merged_family_identity = _text(
                (verdict or {}).get("candidate_identity")
                or (candidate or {}).get("candidate_identity")
            )
            contested_family = family_identity_counts.get(merged_family_identity, 0) > 1
            attempt = (
                attempt_for_identity(merged_identity)
                or (None if contested_family else attempt_for_identity(merged_family_identity))
                or attempt_for_identity(merged_candidate_id)
                or {}
            )
            # Last guard: an attempt that names the candidate it belongs to must
            # never be handed to a different one. This catches any future path
            # that resolves an attempt by some key we have not thought of.
            binding = _text(attempt.get("candidate_binding")) if attempt else ""
            if binding and merged_candidate_id and binding != merged_candidate_id:
                attempt = {}
            # Deliberately no positional fallback. An unmatched row reaches the
            # binding gate with no attempt and is refused as
            # provider_grab_contract_unavailable, which is the safe direction --
            # a refused grab is recoverable, a wrong file is not.
            # verdict_for_candidate is the final QA83 safety/relevance authority.
            # Adapter candidates are discovery evidence and must never win a
            # same-name merge against the normalized verdict.
            row.update(verdict)
            row.setdefault("provider_id", job_result.get("provider_id"))
            row.setdefault("query_variant", runtime.get("query"))
            row["_inkdrop_manual_attempt"] = attempt
            out.append(row)
    return out


def _has_usable_manual_candidate(candidates: list[dict[str, Any]]) -> bool:
    """Only relevant or explicitly reviewable evidence may end lane discovery."""

    for candidate in candidates or []:
        row = candidate if isinstance(candidate, dict) else {}
        quality = _provider_key(row.get("quality_status") or row.get("status"))
        verdict = _provider_key(row.get("auto_grab_verdict"))
        if quality in {"accepted", "review", "assisted"}:
            return True
        if verdict in {"auto_grab_safe", "review", "assisted"}:
            return True
    return False


def _count_variant_evidence(fetch: dict[str, Any]) -> tuple[int, int]:
    completed = 0
    returned = 0
    for payload in fetch.get("payloads") or []:
        if not isinstance(payload, dict):
            continue
        rows = payload.get("variant_result_counts")
        if not isinstance(rows, list):
            continue
        completed += len(rows)
        returned += sum(
            max(0, int(row.get("results") or 0))
            for row in rows
            if isinstance(row, dict)
        )
    return completed, returned


def _count_partial_errors(fetch: dict[str, Any]) -> int:
    count = len(fetch.get("partial_errors") or []) if isinstance(fetch.get("partial_errors"), list) else 0
    for payload in fetch.get("payloads") or []:
        if isinstance(payload, dict) and isinstance(payload.get("partial_errors"), list):
            count += len(payload["partial_errors"])
    return count


def _diagnostic_reason_code(value: Any, fallback: Any) -> str:
    value = _provider_key(value)
    if value and all(char.isalnum() or char in {"_", "-"} for char in value):
        return value[:80]
    return _provider_key(fallback or "unknown")[:80]


def _provider_failure_code(result: dict[str, Any]) -> str:
    """Collapse private/raw adapter failures into an actionable safe code."""

    result = result if isinstance(result, dict) else {}
    fetch = result.get("fetch") if isinstance(result.get("fetch"), dict) else {}
    values = [fetch.get("error"), result.get("reason")]
    for row in fetch.get("partial_errors") or []:
        if isinstance(row, dict):
            values.append(row.get("error") or row.get("reason"))
    lowered = " ".join(str(value or "").lower() for value in values)
    mappings = (
        ("unresolved_secret_ref", "provider_credentials_unavailable"),
        ("http_error", "provider_http_error"),
        ("response_too_large", "provider_response_too_large"),
        ("jsondecodeerror", "malformed_provider_response"),
        ("malformed_provider_response", "malformed_provider_response"),
        ("disallowed_host", "provider_request_blocked"),
        ("disallowed_scheme", "provider_request_blocked"),
        ("missing_host", "provider_request_blocked"),
        ("timeout", "provider_timeout"),
        ("deadline", "provider_timeout"),
        ("request_failed", "provider_connection_failed"),
    )
    for marker, code in mappings:
        if marker in lowered:
            return code
    reason = _provider_key(result.get("reason"))
    if reason in {"configuration_required", "source_configuration_required"}:
        return "provider_configuration_required"
    if reason in {"unsupported_adapter", "not_executable"}:
        return reason
    if reason in {"partial_indexer_search_failed", "http_request_failed"}:
        return "provider_request_failed"
    return _diagnostic_reason_code(reason, result.get("result_status") or "provider_failure")


def _provider_diagnostic_row(
    row: dict[str, Any],
    job: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Project bounded execution evidence without URLs, queries, or credentials."""

    fetch_plan = job.get("fetch_plan") if isinstance(job.get("fetch_plan"), dict) else {}
    fetch = result.get("fetch") if isinstance(result.get("fetch"), dict) else {}
    completed_variants, returned_results = _count_variant_evidence(fetch)
    result_status = _provider_key(result.get("result_status") or "unknown")
    failure_code = _provider_failure_code(result) if result_status in {
        "provider_wait",
        "configuration_required",
        "unsupported_adapter",
        "not_executable",
    } else ""
    return {
        "provider_id": _provider_key(row.get("provider_id")),
        "adapter_family": _provider_key(job.get("adapter_family") or fetch_plan.get("adapter_family")),
        "job_status": _provider_key(job.get("job_status") or "unknown"),
        "result_status": result_status,
        "reason_code": _diagnostic_reason_code(
            failure_code or result.get("reason") or job.get("reason"),
            result.get("result_status") or job.get("job_status"),
        ),
        "failure_code": failure_code,
        "planned_call_count": len(fetch_plan.get("requests") or []),
        "planned_variant_count": len(fetch_plan.get("query_variants") or []),
        "fallback_call_count": len(fetch_plan.get("categoryless_fallback_requests") or []),
        "completed_call_count": len(fetch.get("requests_made") or []),
        "completed_variant_count": completed_variants,
        "returned_result_count": returned_results,
        "partial_error_count": _count_partial_errors(fetch),
        "candidate_count": len(_candidate_rows(result)),
    }


def _slskd_item(context: dict[str, Any]) -> dict[str, Any]:
    item = {
        "series": context.get("canonical_work_title") or context.get("publication_title"),
        "issue": context.get("unit_number"),
        "volume": context.get("volume_number"),
        "media_type": context.get("media_type"),
        "unit_type": context.get("unit_type"),
        "pack_allowed": bool(context.get("pack_allowed")),
        "aliases": list(context.get("aliases") or []),
        "publisher": context.get("publisher"),
        "year": context.get("publication_year"),
        "language": context.get("language"),
    }
    for key in (
        "canonical_issue_count", "metadata_issue_count", "singleton_metadata_trusted",
        "singleton_metadata_fresh", "singleton_issue_metadata_trusted", "singleton_issue_proof",
        "singleton_issue_proof_source", "collected_singleton_wanted_count",
        "collected_singleton_markers", "collected_singleton_title_aliases",
        "collected_singleton_proof", "collected_singleton_proof_source",
    ):
        item[key] = context.get(key)
    return item


def _run_slskd(context: dict[str, Any], queries: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any]:
    # Lazy import keeps the ordinary source-worker bridge lightweight and makes
    # the dedicated SLSKD boundary explicit.
    import inkdrop_slskd_source_probe as slskd
    slskd.refresh_slskd_preferred_exact_size_setting()

    # Soulseek responses stream in substantially later than HTTP indexer
    # results. Give one strong query enough time to settle instead of launching
    # six shallow searches which are all read before peers have answered.
    manual_timeout = max(60, int(profile.get("provider_timeout_seconds") or 25))
    timeout_seconds, deadline_monotonic = _inner_provider_deadline(manual_timeout)
    query_cap = max(1, min(int(profile.get("max_queries_per_provider") or 3), 3))
    candidate_limit = max(1, min(int(profile.get("candidate_limit") or 50), 100))
    explicit_queries = [str(row.get("query") or "").strip() for row in queries if isinstance(row, dict) and row.get("query")]
    discovery = slskd.manual_search_discovery(
        _slskd_item(context),
        explicit_queries,
        wait_seconds=min(55, max(8, timeout_seconds - 4)),
        max_queries=query_cap,
        deadline=time.time() + max(0.25, deadline_monotonic - time.monotonic()),
        candidate_limit=candidate_limit,
    )
    candidates = []
    for raw in discovery.get("candidates") or []:
        candidate = dict(raw or {})
        private_filename = str(candidate.pop("filename", "") or "")
        private_username = str(candidate.pop("username", "") or "")
        acquisition_capability = str(candidate.get("acquisition_capability") or "assisted").strip().lower()
        # Set this on the candidate itself, not just inside the private
        # attempt below: normalize_candidate() (inkdrop_manual_search.py)
        # only uses candidate.get("provider_candidate_identity") when it is
        # already present -- otherwise it derives its OWN, differently-hashed
        # value for the public record. Leaving it unset here meant the public
        # candidate and the private handoff capsule always disagreed on this
        # candidate's identity, which forced_grab_candidate_gate() treats as
        # "candidate_handoff_binding_mismatch" -- Force Grab was unreachable
        # for every real SLSKD candidate for this reason too, independent of
        # the status/acquisition_capability fix above.
        provider_candidate_identity = candidate.get("provider_candidate_identity") or candidate.get("candidate_identity")
        candidate["provider_candidate_identity"] = provider_candidate_identity
        candidate["_inkdrop_manual_attempt"] = {
            "source": "slskd",
            "provider_id": "slskd",
            "protocol": "soulseek",
            "download_client": "SLSKD",
            "candidate_identity": candidate.get("candidate_identity"),
            "provider_candidate_identity": provider_candidate_identity,
            "locator_digest": slskd.slskd_private_locator_digest({
                "username": private_username,
                "filename": private_filename,
                "size": candidate.get("size"),
            }),
            # "ready" means InkDrop knows which peer and file to hand off --
            # true for any real SLSKD search result, whether or not the
            # automatic accept/reject gate approved it for auto-grab. Gating
            # this on acquisition_capability == "automatic" made every
            # rejected SLSKD candidate structurally ineligible for the
            # explicit Force Grab override (forced_grab_candidate_gate in
            # inkdrop_manual_search_core.py), even for rejection reasons that
            # are safe to override on other protocols (e.g. wrong_unit_type).
            "status": "ready" if (private_filename and private_username) else "assisted",
            "acquisition_capability": acquisition_capability,
            "raw": {
                "candidate": {
                    "filename": private_filename,
                    "username": private_username,
                    "size": candidate.get("size"),
                }
            },
        }
        candidates.append(candidate)

    evidence = discovery.get("evidence") if isinstance(discovery.get("evidence"), dict) else {}
    diagnostic = {
        "provider_id": "slskd",
        "adapter_family": "slskd_manual_discovery",
        "job_status": "ready",
        "result_status": _provider_key(discovery.get("status") or "provider_failure"),
        "reason_code": _diagnostic_reason_code(discovery.get("error"), discovery.get("status")),
        "planned_call_count": int(evidence.get("planned_query_count") or 0),
        "planned_variant_count": int(evidence.get("planned_query_count") or 0),
        "fallback_call_count": 0,
        "completed_call_count": int(evidence.get("completed_query_count") or 0),
        "completed_variant_count": int(evidence.get("completed_query_count") or 0),
        "returned_result_count": int(evidence.get("response_count") or 0),
        "partial_error_count": int(evidence.get("partial_error_count") or 0),
        "candidate_count": len(candidates),
        "zero_result_expansion_query_count": int(evidence.get("zero_result_expansion_query_count") or 0),
        "query_attempt_summary": ",".join(
            f"{int(row.get('query_ordinal') or 0)}:{str(row.get('query_fingerprint') or '')[:16]}:{_provider_key(row.get('status'))}"
            for row in (evidence.get("attempts") or [])[:6]
            if isinstance(row, dict)
        )[:240],
    }
    return {
        "completed": bool(discovery.get("completed")),
        "error": str(discovery.get("error") or "")[:500],
        "candidates": candidates,
        "health": {"status": discovery.get("status")},
        "provider_rows_attempted": 1,
        "diagnostics": {
            "contract_version": 1,
            "provider_rows_considered": 1,
            "provider_rows": [diagnostic],
        },
    }


def run_provider(db_path: str | Path, provider_id: str, context: dict[str, Any], queries: list[dict[str, Any]], profile: dict[str, Any], *, registry_loader=None) -> dict[str, Any]:
    if _provider_key(provider_id) == "slskd":
        return _run_slskd(context, queries, profile)
    rows = provider_rows(db_path, provider_id, registry_loader=registry_loader)
    if not rows:
        return {"completed": False, "error": "provider_not_configured_or_enabled", "candidates": []}
    wanted = _wanted_context(context, queries)
    timeout_seconds, deadline_monotonic = _inner_provider_deadline(profile.get("provider_timeout_seconds"))
    client = _http_client(
        db_path,
        timeout_seconds=timeout_seconds,
        deadline_monotonic=deadline_monotonic,
    )
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    health: dict[str, Any] = {}
    attempted = 0
    successful_rows = 0
    failed_rows = 0
    skipped_rows = 0
    provider_diagnostics: list[dict[str, Any]] = []
    def execute_row(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        execution_row = dict(row)
        # Manual search is a discovery surface. Return title-level indexer
        # hits promptly instead of spending its shared provider deadline
        # downloading torrent/NZB manifests for pack enrichment. Automatic
        # acquisition keeps the ordinary detail-fetch policy unchanged.
        execution_row["disable_pack_detail_fetch"] = True
        if execution_row.pop("manual_discovery_only", False):
            # Lift only the search scheduler boundary for this operator-started
            # read.  Acquisition remains disabled and requires a later explicit
            # decision, so an automatic ratio/health gate is never bypassed.
            execution_row.update({
                "enabled": True,
                "registry_state": "assist",
                "source_mode": "assist",
                "auto_search_allowed": True,
                "auto_download_allowed": False,
                "requires_manual_review": True,
            })
        plan: dict[str, Any] = {}
        job: dict[str, Any] = {
            "provider_id": execution_row.get("provider_id"),
            "adapter_family": execution_row.get("adapter_family") or execution_row.get("source_kind"),
            "job_status": "ready",
            "fetch_plan": {},
        }
        try:
            plan = inkdrop_source_worker_plan.source_worker_plan_for_row(execution_row)
            job = inkdrop_source_worker_jobs.source_job_for_row(
                execution_row,
                plan,
                wanted,
                limit=min(100, max(20, int(profile.get("candidate_limit") or 50))),
            )
            row_client = client
            if _provider_key(provider_id) == "prowlarr":
                # Give each isolated child a short share of the outer deadline.
                # This prevents one unhealthy indexer from consuming all 25s and
                # avoids overloading Prowlarr with competing aggregate-like calls.
                remaining = max(1.0, deadline_monotonic - time.monotonic())
                row_deadline = min(deadline_monotonic, time.monotonic() + min(12.0, remaining))
                row_client = _http_client(
                    db_path,
                    timeout_seconds=min(timeout_seconds, 12),
                    deadline_monotonic=row_deadline,
                )
            result = inkdrop_source_worker_jobs.run_source_job(job, http_get=row_client)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            result = {
                "provider_id": execution_row.get("provider_id"),
                "result_status": "provider_wait",
                "reason": "child_source_execution_failed",
                "fetch": {"error": error, "partial_errors": [{"stage": "child_source", "error": error}]},
                "runtime_results": [],
            }
        return row, job, result

    def retain_execution(execution: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]) -> list[dict[str, Any]]:
        """Project one completed child before another child can fail or expire."""

        nonlocal attempted, successful_rows, failed_rows, skipped_rows, health
        row, job, result = execution
        health = job.get("provider_health") if isinstance(job.get("provider_health"), dict) else health
        attempted += 1
        provider_diagnostics.append(_provider_diagnostic_row(row, job, result))
        result_status = _provider_key(result.get("result_status"))
        row_candidates = _candidate_rows(result)
        if result_status == "blocked":
            if row_candidates:
                successful_rows += 1
                candidates.extend(row_candidates)
            else:
                skipped_rows += 1
            return row_candidates
        if result_status in {
            "provider_wait", "configuration_required", "unsupported_adapter", "not_executable",
        }:
            failed_rows += 1
            errors.append(_provider_failure_code(result))
        else:
            successful_rows += 1
        candidates.extend(row_candidates)
        return row_candidates

    if _provider_key(provider_id) == "prowlarr":
        rows = sorted(rows, key=lambda row: _manual_prowlarr_row_rank(row, context))
    selected_rows = rows[:8]
    if _provider_key(provider_id) == "prowlarr" and len(selected_rows) > 1:
        # Isolated, ranked child lanes avoid Prowlarr's slow aggregate search.
        # Stop as soon as one lane returns useful evidence; manual search can
        # display both accepted and rejected/assisted candidates from it.
        for row in selected_rows:
            if deadline_monotonic - time.monotonic() <= 0:
                errors.append("provider_timeout")
                failed_rows += 1
                break
            execution = execute_row(row)
            row_candidates = retain_execution(execution)
            if _has_usable_manual_candidate(row_candidates):
                break
    else:
        for row in selected_rows:
            if deadline_monotonic - time.monotonic() <= 0:
                failed_rows += 1
                errors.append("provider_timeout")
                break
            retain_execution(execute_row(row))
    # A provider group completed when at least one eligible row returned a
    # valid response. Failed/unsupported sibling lanes remain visible as
    # partial diagnostics but cannot relabel that response as total failure.
    completed = successful_rows > 0
    return {
        "completed": completed,
        "error": "; ".join(dict.fromkeys(errors))[:500] if not completed else "",
        "candidates": candidates,
        "health": health,
        "provider_rows_attempted": attempted,
        "diagnostics": {
            "contract_version": 1,
            "provider_rows_considered": attempted,
            "provider_rows_succeeded": successful_rows,
            "provider_rows_failed": failed_rows,
            "provider_rows_skipped": skipped_rows,
            "provider_rows": provider_diagnostics,
        },
    }


def runner_for_db(db_path: str | Path):
    # Manual search fans its providers out over concurrent threads, and each
    # non-SLSKD provider independently called registry_from_db() to build its
    # row set. Under real SQLite write contention that read alone measured
    # 15s+, and two threads missing the cache at the same moment each paid
    # that cost separately -- often burning most of a provider's timeout
    # budget before a single network request was made. One run's registry
    # rarely changes in the ~60-120s the run takes, so the fix is to let the
    # first caller populate a run-scoped cache and have the rest wait on it
    # instead of repeating the same contended read.
    #
    # Providers request one of two distinct row sets: include_disabled=True
    # for prowlarr, False for everyone else. A single lock shared across both
    # keys serializes those independent reads -- prowlarr's read has to finish
    # before rss/direct's *different* read can even start, which sums their
    # costs instead of letting them run concurrently. Only callers requesting
    # the SAME key should ever wait on each other, so cache-dict bookkeeping
    # uses registry_cache_lock (held briefly) while the actual contended read
    # is guarded by a lock scoped to that key alone.
    registry_cache: dict[bool, list[dict[str, Any]]] = {}
    registry_cache_lock = threading.Lock()
    registry_key_locks: dict[bool, threading.Lock] = {}

    def _shared_registry(include_disabled: bool) -> list[dict[str, Any]]:
        with registry_cache_lock:
            cached = registry_cache.get(include_disabled)
            if cached is not None:
                return cached
            key_lock = registry_key_locks.setdefault(include_disabled, threading.Lock())
        with key_lock:
            with registry_cache_lock:
                cached = registry_cache.get(include_disabled)
                if cached is not None:
                    return cached
            rows = inkdrop_source_registry.registry_from_db(db_path, include_disabled=include_disabled)
            with registry_cache_lock:
                registry_cache[include_disabled] = rows
            return rows

    def _runner(provider_id, context, queries, profile):
        return run_provider(db_path, provider_id, context, queries, profile, registry_loader=_shared_registry)

    return _runner


def safe_public_result(value: dict[str, Any]) -> dict[str, Any]:
    """Bounded helper for CLI diagnostics; never prints adapter raw evidence."""

    return json.loads(json.dumps(value, default=str))
