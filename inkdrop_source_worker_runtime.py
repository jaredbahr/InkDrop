"""Plan-driven source worker dispatch helpers.

This module intentionally performs no network, filesystem, or database work. A live
worker fetches payloads, then uses this dispatcher to run the settings-derived
parser/verdict/attempt contract.
"""

from __future__ import annotations

import re
import time

import inkdrop_candidate_matching
import inkdrop_source_providers as providers
import inkdrop_source_worker_plan as worker_plan


CONTRACT_VERSION = 1
TOKEN_RE = re.compile(r"[a-z0-9]+")
LEADING_ARTICLES = {"a", "an", "the"}
INDEXER_RESULT_ADAPTER_FAMILIES = {
    "prowlarr_indexer",
    "torznab_indexer",
    "newznab_indexer",
    "torrent_rss_feed",
}
INDEXER_NO_CANDIDATE_SAMPLE_LIMIT = 5


def _parser_standard_ebooks(payload, row, wanted_item, limit, plan):
    return providers.standard_ebooks_candidates_from_opds(payload, row, wanted_item, limit=limit)


def _parser_gutendex(payload, row, wanted_item, limit, plan):
    return providers.gutendex_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_internet_archive(payload, row, wanted_item, limit, plan):
    return providers.internet_archive_candidates_from_metadata(payload, row, wanted_item, limit=limit)


def _parser_prowlarr(payload, row, wanted_item, limit, plan):
    results = payload.get("results") if isinstance(payload, dict) and isinstance(payload.get("results"), list) else payload
    return providers.prowlarr_candidates_from_results(results, row, wanted_item, limit=limit)


def _parser_torznab(payload, row, wanted_item, limit, plan):
    return providers.torznab_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_newznab(payload, row, wanted_item, limit, plan):
    return providers.newznab_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_torrent_rss(payload, row, wanted_item, limit, plan):
    return providers.torrent_rss_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_torrent_html(payload, row, wanted_item, limit, plan):
    return providers.torrent_html_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_torrent_detail(payload, row, wanted_item, limit, plan):
    return providers.torrent_detail_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_indexer_discovery(payload, row, wanted_item, limit, plan):
    return providers.indexer_discovery_cards_from_results(payload, row, wanted_item, limit=limit)


def _parser_external_tool(payload, row, wanted_item, limit, plan):
    return providers.external_tool_candidates_from_results(
        payload,
        row,
        wanted_item,
        tool_name=plan.get("adapter_id") or row.get("display_name") or row.get("provider_id") or "",
        limit=limit,
    )


def _parser_manual_source(payload, row, wanted_item, limit, plan):
    return providers.manual_source_cards_from_results(
        payload,
        row,
        wanted_item,
        source_bucket=row.get("provider_id") or "",
        limit=limit,
    )


def _parser_rss_feed(payload, row, wanted_item, limit, plan):
    return providers.rss_feed_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_direct_rss(payload, row, wanted_item, limit, plan):
    return providers.direct_rss_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_direct_file_html(payload, row, wanted_item, limit, plan):
    return providers.direct_file_html_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_direct_file_detail(payload, row, wanted_item, limit, plan):
    return providers.direct_file_detail_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_direct_file_probe(payload, row, wanted_item, limit, plan):
    return providers.direct_file_probe_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_reader_page_pack(payload, row, wanted_item, limit, plan):
    return providers.reader_page_pack_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_json_direct(payload, row, wanted_item, limit, plan):
    return providers.json_direct_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_opds_catalog(payload, row, wanted_item, limit, plan):
    return providers.opds_catalog_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_comicscodes(payload, row, wanted_item, limit, plan):
    return providers.comicscodes_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_html_search(payload, row, wanted_item, limit, plan):
    return providers.html_search_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_mangadex(payload, row, wanted_item, limit, plan):
    return providers.mangadex_candidates_from_payload(payload, row, wanted_item, limit=limit)


def _parser_suwayomi(payload, row, wanted_item, limit, plan):
    return providers.suwayomi_candidates_from_payload(payload, row, wanted_item, limit=limit)


PARSER_DISPATCH = {
    "standard_ebooks_candidates_from_opds": _parser_standard_ebooks,
    "gutendex_candidates_from_payload": _parser_gutendex,
    "internet_archive_candidates_from_metadata": _parser_internet_archive,
    "prowlarr_candidates_from_results": _parser_prowlarr,
    "torznab_candidates_from_payload": _parser_torznab,
    "newznab_candidates_from_payload": _parser_newznab,
    "torrent_rss_candidates_from_payload": _parser_torrent_rss,
    "torrent_html_candidates_from_payload": _parser_torrent_html,
    "torrent_detail_candidates_from_payload": _parser_torrent_detail,
    "indexer_discovery_cards_from_results": _parser_indexer_discovery,
    "external_tool_candidates_from_results": _parser_external_tool,
    "manual_source_cards_from_results": _parser_manual_source,
    "rss_feed_candidates_from_payload": _parser_rss_feed,
    "direct_rss_candidates_from_payload": _parser_direct_rss,
    "direct_file_html_candidates_from_payload": _parser_direct_file_html,
    "direct_file_detail_candidates_from_payload": _parser_direct_file_detail,
    "direct_file_probe_candidates_from_payload": _parser_direct_file_probe,
    "reader_page_pack_candidates_from_payload": _parser_reader_page_pack,
    "json_direct_candidates_from_payload": _parser_json_direct,
    "opds_catalog_candidates_from_payload": _parser_opds_catalog,
    "comicscodes_candidates_from_payload": _parser_comicscodes,
    "html_search_candidates_from_payload": _parser_html_search,
    "mangadex_candidates_from_payload": _parser_mangadex,
    "suwayomi_candidates_from_payload": _parser_suwayomi,
}


def parse_candidates(plan, row, payload, wanted_item=None, limit=20):
    plan = plan if isinstance(plan, dict) else {}
    parser_name = str(plan.get("candidate_parser") or "").strip()
    parser = PARSER_DISPATCH.get(parser_name)
    if not parser:
        return [], {"ok": False, "reason": "candidate_parser_unimplemented", "candidate_parser": parser_name}
    try:
        candidates = parser(payload, row if isinstance(row, dict) else {}, wanted_item or {}, limit, plan)
    except Exception as exc:
        return [], {
            "ok": False,
            "reason": "candidate_parser_error",
            "candidate_parser": parser_name,
            "error": f"{type(exc).__name__}: {exc}",
        }
    normalized = []
    for candidate in candidates or []:
        candidate = inkdrop_candidate_matching.normalize_candidate(candidate, wanted_item)
        identities = inkdrop_candidate_matching.stable_candidate_identities(candidate)
        candidate["provider_candidate_identity"] = candidate.get("candidate_identity") or ""
        candidate.update(identities)
        candidate["candidate_identity"] = identities["candidate_family_identity"]
        normalized.append(candidate)
    return normalized, {"ok": True, "candidate_parser": parser_name}


def _headers_have_content(headers):
    if not isinstance(headers, dict):
        return False
    lowered = {str(key).lower() for key in headers}
    return bool(lowered.intersection({"content-type", "content-length"}))


def headers_for_candidate(candidate, headers=None, index=0):
    if not isinstance(headers, dict):
        return {}
    if _headers_have_content(headers):
        return headers
    candidate = candidate if isinstance(candidate, dict) else {}
    keys = [
        candidate.get("candidate_identity"),
        candidate.get("download_url_hash"),
        candidate.get("canonical_item_id"),
        candidate.get("archive_file_name"),
        candidate.get("title"),
        str(index),
        index,
    ]
    for key in keys:
        if key in (None, ""):
            continue
        value = headers.get(key)
        if isinstance(value, dict):
            return value
    return {}


def verdict_for_candidate(plan, row, candidate, *, wanted_item=None, headers=None, index=0, staging_root=None):
    plan = plan if isinstance(plan, dict) else {}
    helper = str(plan.get("verdict_helper") or "").strip()
    verdict = None
    if helper == "direct_artifact_verdict":
        verdict = providers.direct_artifact_verdict(
            candidate,
            row,
            headers=headers_for_candidate(candidate, headers=headers, index=index),
        )
    elif helper == "reader_page_pack_verdict":
        verdict = providers.reader_page_pack_verdict(candidate, row)
    elif helper == "indexer_candidate_verdict":
        verdict = providers.indexer_candidate_verdict(candidate, row)
    elif helper == "external_tool_candidate_verdict":
        verdict = providers.external_tool_candidate_verdict(candidate, row)
    elif helper == "manual_source_card_verdict":
        verdict = providers.manual_source_card_verdict(candidate, row)
    elif helper == "mangadex_candidate_verdict":
        verdict = providers.mangadex_candidate_verdict(candidate, row)
    elif helper == "suwayomi_candidate_verdict":
        verdict = providers.suwayomi_candidate_verdict(candidate, row)
    else:
        verdict = dict(candidate or {})
        verdict["block_reasons"] = ["verdict_helper_unimplemented"]
        verdict["auto_grab_verdict"] = "blocked"
        verdict["review_reason"] = "verdict_helper_unimplemented"
        verdict["candidate_safe"] = False
        verdict["artifact_safe"] = False
    compatible = inkdrop_candidate_matching.apply_compatibility(verdict, wanted_item)
    return providers.classify_candidate_outcome(compatible, row, staging_root=staging_root)


def attempt_seed_for_verdict(plan, row, verdict, *, staging_root=None):
    plan = plan if isinstance(plan, dict) else {}
    helper = str(plan.get("attempt_seed_helper") or "").strip()
    attempt = None
    if helper == "direct_candidate_attempt_seed":
        attempt = providers.direct_candidate_attempt_seed(verdict, row, staging_root=staging_root)
    elif helper == "reader_page_pack_attempt_seed":
        attempt = providers.reader_page_pack_attempt_seed(verdict, row, staging_root=staging_root)
    elif helper == "indexer_candidate_attempt_seed":
        attempt = providers.indexer_candidate_attempt_seed(verdict, row, staging_root=staging_root)
    elif helper == "external_tool_candidate_attempt_seed":
        attempt = providers.external_tool_candidate_attempt_seed(verdict, row)
    elif helper == "manual_source_card_attempt_seed":
        attempt = providers.manual_source_card_attempt_seed(verdict, row)
    elif helper == "mangadex_candidate_attempt_seed":
        attempt = providers.mangadex_candidate_attempt_seed(verdict, row, staging_root=staging_root)
    elif helper == "suwayomi_candidate_attempt_seed":
        attempt = providers.suwayomi_candidate_attempt_seed(verdict, row, staging_root=staging_root)
    else:
        attempt = providers.source_search_attempt_seed(
            row,
            status="blocked",
            reason="attempt_seed_helper_unimplemented",
            raw={"plan": plan, "candidate": verdict},
        )
    attempt = dict(attempt or {})
    provenance = verdict.get("query_provenance") if isinstance(verdict.get("query_provenance"), dict) else {}
    compatibility = verdict.get("target_compatibility") if isinstance(verdict.get("target_compatibility"), dict) else {}
    for key, value in (
        ("candidate_family_identity", verdict.get("candidate_family_identity")),
        ("candidate_instance_identity", verdict.get("candidate_instance_identity")),
        ("content_identity", verdict.get("content_identity")),
        ("query_variant", provenance.get("query")),
        ("query_group", provenance.get("query_group")),
        ("search_request_id", provenance.get("request_id")),
        ("rejection_codes", compatibility.get("rejection_codes")),
        ("review_codes", compatibility.get("review_codes")),
    ):
        if value not in (None, "", [], {}):
            attempt[key] = value
    raw = attempt.get("raw") if isinstance(attempt.get("raw"), dict) else {}
    raw["source_unit_evidence"] = verdict.get("source_unit_evidence") or {}
    raw["target_compatibility"] = compatibility
    raw["query_provenance"] = provenance
    if isinstance(verdict.get("auto_inspect"), dict):
        raw["auto_inspect"] = verdict["auto_inspect"]
    elif isinstance(attempt.get("auto_inspect"), dict):
        raw["auto_inspect"] = attempt["auto_inspect"]
    task = raw.get("download_task_seed") if isinstance(raw.get("download_task_seed"), dict) else None
    if task is not None:
        task_raw = task.get("raw_json") if isinstance(task.get("raw_json"), dict) else {}
        task_raw["source_unit_evidence"] = raw["source_unit_evidence"]
        task_raw["target_compatibility"] = compatibility
        task_raw["query_provenance"] = provenance
        if isinstance(raw.get("auto_inspect"), dict):
            task_raw["auto_inspect"] = raw["auto_inspect"]
        task["raw_json"] = task_raw
        raw["download_task_seed"] = task
    attempt["raw"] = raw
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def source_search_attempt(row, plan, *, query="", status="searched_no_candidates", reason="", counts=None, raw=None):
    counts = counts if isinstance(counts, dict) else {}
    raw_payload = raw if isinstance(raw, dict) else {}
    raw_payload.setdefault("worker_plan", plan if isinstance(plan, dict) else {})
    return providers.source_search_attempt_seed(
        row,
        query=query,
        status=status,
        reason=reason,
        candidate_count=counts.get("candidate_count"),
        safe_candidate_count=counts.get("safe_candidate_count"),
        rejected_candidate_count=counts.get("rejected_candidate_count"),
        raw=raw_payload,
    )


def _attempt_kind(attempt):
    status = str((attempt or {}).get("status") or "").strip().lower()
    if status == "sent":
        return "sent"
    if status == "review":
        return "review"
    if status == "blocked":
        return "blocked"
    return status or "observed"


def _runtime_status_from_attempts(attempts):
    kinds = {_attempt_kind(attempt) for attempt in attempts or []}
    if "sent" in kinds:
        return "sent"
    if "review" in kinds:
        return "review"
    if "blocked" in kinds:
        return "blocked"
    if "searched_no_candidates" in kinds:
        return "searched_no_candidates"
    if kinds:
        return sorted(kinds)[0]
    return "observed"


def _tokens(value):
    return TOKEN_RE.findall(providers.normalized_query(value or "").lower())


def _unique_token_variants(variants):
    out = []
    seen = set()
    for tokens in variants or []:
        tokens = [token for token in (tokens or []) if token]
        key = tuple(tokens)
        if tokens and key not in seen:
            seen.add(key)
            out.append(tokens)
    return out


def _series_token_variants(row, wanted_item):
    row = row if isinstance(row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    title = providers.first_text(
        wanted_item.get("series_title"),
        wanted_item.get("series"),
        row.get("series_title"),
        row.get("series"),
        wanted_item.get("title"),
        row.get("title"),
        row.get("query"),
    )
    tokens = _tokens(title)
    variants = [tokens]
    if tokens and tokens[0] in LEADING_ARTICLES:
        variants.append(tokens[1:])
    return _unique_token_variants(variants)


def _unit_number_variants(row, wanted_item):
    row = row if isinstance(row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    number = providers.first_text(
        wanted_item.get("issue_number"),
        wanted_item.get("normalized_number"),
        wanted_item.get("issue"),
        wanted_item.get("chapter_number"),
        wanted_item.get("chapter"),
        wanted_item.get("volume_number"),
        wanted_item.get("volume"),
        wanted_item.get("number"),
        row.get("issue_number"),
        row.get("normalized_number"),
        row.get("chapter_number"),
        row.get("volume_number"),
    )
    text = str(number or "").strip().lower()
    if not text:
        return set()
    out = {text}
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        numeric = match.group(0)
        out.add(numeric)
        if numeric.isdigit():
            stripped = numeric.lstrip("0") or "0"
            out.update({stripped, stripped.zfill(2), stripped.zfill(3)})
    return {value for value in out if value}


def _token_matches_unit_number(token, variants):
    token = str(token or "").strip().lower()
    if not token:
        return False
    if token in variants:
        return True
    if token.isdigit():
        stripped = token.lstrip("0") or "0"
        for variant in variants:
            variant_text = str(variant or "").strip().lower()
            if variant_text.isdigit() and (variant_text.lstrip("0") or "0") == stripped:
                return True
    return False


def _subsequence_index(tokens, needle):
    if not tokens or not needle or len(needle) > len(tokens):
        return None
    width = len(needle)
    for index in range(0, len(tokens) - width + 1):
        if tokens[index : index + width] == needle:
            return index
    return None


def _title_issue_distance(attempt, row, wanted_item):
    candidate = (attempt or {}).get("raw", {}).get("candidate") if isinstance((attempt or {}).get("raw"), dict) else {}
    title = providers.first_text((attempt or {}).get("title"), (candidate or {}).get("title"))
    tokens = _tokens(title)
    if not tokens:
        return 900
    number_variants = _unit_number_variants(row, wanted_item)
    if not number_variants:
        return 500
    issue_positions = [
        index
        for index, token in enumerate(tokens)
        if _token_matches_unit_number(token, number_variants)
    ]
    if not issue_positions:
        return 700
    series_variants = _series_token_variants(row, wanted_item)
    if not series_variants:
        return min(issue_positions) + 500
    best = None
    series_seen = False
    for series_tokens in series_variants:
        start = _subsequence_index(tokens, series_tokens)
        if start is None:
            continue
        series_seen = True
        expected_issue_index = start + len(series_tokens)
        after_series = [index for index in issue_positions if index >= expected_issue_index]
        if after_series:
            distance = min(index - expected_issue_index for index in after_series)
        else:
            distance = min(abs(index - expected_issue_index) for index in issue_positions) + 100
        best = distance if best is None else min(best, distance)
    if best is not None:
        return best
    if series_seen:
        return min(issue_positions) + 300
    return min(issue_positions) + 400


def _match_confidence_rank(attempt):
    match_confidence = str((attempt or {}).get("match_confidence") or "").strip().lower()
    if match_confidence in {"title_issue_match", "title_chapter_match", "title_volume_match"}:
        return 0
    if (attempt or {}).get("pack_contents_coverage_source") in providers.PACK_CONTENTS_SAFE_COVERAGE_SOURCES:
        return 1
    if match_confidence == "series_title_only":
        return 4
    return 2


def _quality_rank(attempt):
    text = providers.normalized_query(
        " ".join(
            str(value or "")
            for value in (
                (attempt or {}).get("quality_profile"),
                (attempt or {}).get("quality"),
                (attempt or {}).get("title"),
            )
        )
    ).lower()
    if "digital" in text:
        return 0
    if "web" in text:
        return 1
    if "c2c" in text or "complete" in text:
        return 2
    if not text or "unknown" in text:
        return 3
    if "scan" in text:
        return 4
    if "raw" in text:
        return 5
    return 3


def _seed_rank(attempt):
    try:
        return -int((attempt or {}).get("seeders") or 0)
    except (TypeError, ValueError):
        return 0


def _auto_send_rank(attempt, row, wanted_item, ordinal):
    return (
        _title_issue_distance(attempt, row, wanted_item),
        _match_confidence_rank(attempt),
        _quality_rank(attempt),
        _seed_rank(attempt),
        int(ordinal or 0),
    )


def _attempt_selection_summary(attempt):
    attempt = attempt if isinstance(attempt, dict) else {}
    return {
        key: value
        for key, value in {
            "candidate_identity": providers.first_text(
                attempt.get("candidate_identity"),
                attempt.get("external_id"),
                attempt.get("download_id"),
                attempt.get("download_url_hash"),
            ),
            "title": attempt.get("title"),
            "provider_id": attempt.get("provider_id"),
            "download_client": attempt.get("download_client"),
            "match_confidence": attempt.get("match_confidence"),
            "quality_profile": attempt.get("quality_profile") or attempt.get("quality"),
            "protocol": attempt.get("protocol"),
        }.items()
        if value not in (None, "", [], {})
    }


def select_auto_send_attempts(runtime_results, row=None, wanted_item=None, *, scope="source_job"):
    rows = []
    sent_refs = []
    ordinal = 0
    for result_index, runtime_result in enumerate(runtime_results or []):
        if not isinstance(runtime_result, dict):
            continue
        result = dict(runtime_result)
        attempts = [
            dict(attempt)
            for attempt in (runtime_result.get("attempts") or [])
            if isinstance(attempt, dict)
        ]
        result["attempts"] = attempts
        rows.append(result)
        row_index = len(rows) - 1
        for attempt_index, attempt in enumerate(attempts):
            if _attempt_kind(attempt) == "sent":
                sent_refs.append((row_index, attempt_index, attempt, ordinal))
                ordinal += 1

    selection = {
        "auto_send_selection_contract_version": CONTRACT_VERSION,
        "scope": scope,
        "applied": False,
        "sent_candidate_count": len(sent_refs),
    }
    if len(sent_refs) <= 1:
        return rows, selection

    selected = min(sent_refs, key=lambda ref: _auto_send_rank(ref[2], row, wanted_item, ref[3]))
    selected_key = (selected[0], selected[1])
    selected_summary = _attempt_selection_summary(selected[2])
    suppressed = []
    for row_index, runtime_result in enumerate(rows):
        kept = []
        row_suppressed = []
        for attempt_index, attempt in enumerate(runtime_result.get("attempts") or []):
            if _attempt_kind(attempt) == "sent" and (row_index, attempt_index) != selected_key:
                summary = _attempt_selection_summary(attempt)
                suppressed.append(summary)
                row_suppressed.append(summary)
                continue
            kept.append(attempt)
        runtime_result["attempts"] = kept
        runtime_result["status"] = _runtime_status_from_attempts(kept)
        if row_suppressed or row_index == selected_key[0]:
            runtime_result["auto_send_selection"] = {
                "auto_send_selection_contract_version": CONTRACT_VERSION,
                "scope": scope,
                "applied": bool(row_suppressed),
                "selected": selected_summary if row_index == selected_key[0] else {},
                "suppressed_sent_candidate_count": len(row_suppressed),
                "suppressed": row_suppressed[:10],
            }

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
    return rows, selection


def _primary_block_reason(plan, default="source_not_schedulable"):
    reasons = list((plan or {}).get("block_reasons") or [])
    for preferred in ("disabled_boundary", "implementation_pending", "source_mode_disabled", "disabled"):
        if preferred in reasons:
            return preferred
    return str(reasons[0]) if reasons else default


def _count_suwayomi_variant_results(variant_counts):
    result_count = 0
    matching_manga_count = 0
    for row in variant_counts or []:
        if not isinstance(row, dict):
            continue
        try:
            result_count += int(row.get("results") if row.get("results") not in (None, "") else row.get("result_count") or 0)
        except Exception:
            pass
        try:
            matching_manga_count += int(
                row.get("matching_manga")
                if row.get("matching_manga") not in (None, "")
                else row.get("matching_manga_count") or 0
            )
        except Exception:
            pass
    return result_count, matching_manga_count


def _suwayomi_chapter_volume_evidence(chapter_row):
    return providers.suwayomi_explicit_volume_evidence(chapter_row)


def _suwayomi_number_text_matches(candidate_value, wanted_value):
    candidate = str(candidate_value or "").strip()
    wanted = str(wanted_value or "").strip()
    if not candidate or not wanted:
        return False
    try:
        return float(candidate) == float(wanted)
    except Exception:
        return candidate.lower() == wanted.lower()


def _suwayomi_no_candidate_reason(evidence):
    evidence = evidence if isinstance(evidence, dict) else {}
    variant_counts = evidence.get("variant_result_counts") if isinstance(evidence.get("variant_result_counts"), list) else []
    result_count, matching_manga_count = _count_suwayomi_variant_results(variant_counts)
    partial_errors = evidence.get("partial_errors") if isinstance(evidence.get("partial_errors"), list) else []
    source_search_error_count = sum(
        1
        for row in partial_errors
        if isinstance(row, dict) and str(row.get("stage") or "") == "source_search"
    )
    manga_lookup_error_count = sum(
        1
        for row in partial_errors
        if isinstance(row, dict)
        and str(row.get("stage") or "") in {"manga_chapters", "manga_chapters_no_meta_fallback"}
    )
    page_lookup_error_count = sum(
        1
        for row in partial_errors
        if isinstance(row, dict)
        and str(row.get("stage") or "") in {"chapter_pages", "chapter_pages_no_meta_fallback"}
    )
    wanted_volume = str(evidence.get("wanted_volume") or "").strip()
    wanted_chapter = str(evidence.get("wanted_chapter") or "").strip()
    wanted_unit_type = str(evidence.get("wanted_unit_type") or "").strip().lower()
    volume_scoped = bool(
        wanted_volume
        and wanted_unit_type not in {"chapter", "manga_chapter", "chapter_native", "native_chapter"}
    )
    chapter_count = int(evidence.get("chapter_count") or 0)
    chapter_has_volume_count = int(evidence.get("chapter_has_volume_count") or 0)
    chapter_matching_wanted_volume_count = int(evidence.get("chapter_matching_wanted_volume_count") or 0)
    chapter_invalid_volume_count = int(evidence.get("chapter_invalid_volume_count") or 0)
    pages_by_chapter_count = int(evidence.get("pages_by_chapter_count") or 0)
    if (
        volume_scoped
        and matching_manga_count
        and chapter_count
        and chapter_invalid_volume_count
        and chapter_matching_wanted_volume_count <= 0
    ):
        return "suwayomi_volume_metadata_invalid"
    if volume_scoped and matching_manga_count and chapter_count and chapter_has_volume_count <= 0:
        return "suwayomi_volume_metadata_missing"
    if (
        volume_scoped
        and matching_manga_count
        and chapter_has_volume_count
        and chapter_matching_wanted_volume_count <= 0
    ):
        return "suwayomi_volume_unit_mismatch"
    if volume_scoped and matching_manga_count and chapter_matching_wanted_volume_count and pages_by_chapter_count <= 0:
        return "suwayomi_volume_requires_volume_pack"
    if volume_scoped and matching_manga_count and chapter_matching_wanted_volume_count and pages_by_chapter_count <= 0:
        return "suwayomi_volume_page_evidence_missing"
    if wanted_chapter and matching_manga_count and chapter_count and pages_by_chapter_count <= 0:
        return "suwayomi_chapter_page_evidence_missing"
    if source_search_error_count and not matching_manga_count:
        return "suwayomi_source_pool_errors_no_match"
    if manga_lookup_error_count and matching_manga_count:
        return "suwayomi_manga_lookup_partial_failure"
    if page_lookup_error_count and matching_manga_count:
        return "suwayomi_page_lookup_partial_failure"
    if result_count and not matching_manga_count:
        return "suwayomi_title_mismatch_from_search_results"
    if not result_count:
        return "suwayomi_empty_search_results"
    return "suwayomi_no_candidates"


def _suwayomi_no_candidate_evidence(payload, wanted_item=None):
    payload = payload if isinstance(payload, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    if not (payload.get("source") or payload.get("manga") or payload.get("chapters") or payload.get("variant_result_counts")):
        return {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    manga = payload.get("manga") if isinstance(payload.get("manga"), dict) else {}
    chapters = [row for row in (payload.get("chapters") or []) if isinstance(row, dict)]
    pages_by_chapter = payload.get("pages_by_chapter") if isinstance(payload.get("pages_by_chapter"), dict) else {}
    samples = []
    wanted_volume = providers.first_text(wanted_item.get("volume"), wanted_item.get("volume_number"), wanted_item.get("volumeNumber"))
    chapter_volume_evidence = [_suwayomi_chapter_volume_evidence(chapter) for chapter in chapters]
    chapter_has_volume_count = sum(1 for evidence in chapter_volume_evidence if evidence.get("volume_number"))
    chapter_conflicting_volume_count = sum(1 for evidence in chapter_volume_evidence if evidence.get("conflict"))
    chapter_malformed_volume_count = sum(1 for evidence in chapter_volume_evidence if evidence.get("malformed"))
    chapter_invalid_volume_count = sum(
        1 for evidence in chapter_volume_evidence if evidence.get("conflict") or evidence.get("malformed")
    )
    chapter_matching_wanted_volume_count = sum(
        1
        for evidence in chapter_volume_evidence
        if wanted_volume and _suwayomi_number_text_matches(evidence.get("volume_number"), wanted_volume)
    )
    for index, chapter in enumerate(chapters[:10]):
        chapter_id = str(chapter.get("id") or "").strip()
        page_payload = pages_by_chapter.get(chapter_id) if chapter_id else None
        pages = page_payload.get("pages") if isinstance(page_payload, dict) and isinstance(page_payload.get("pages"), list) else []
        volume_evidence = chapter_volume_evidence[index]
        volume_value = volume_evidence.get("volume_number")
        samples.append(
            {
                key: value
                for key, value in {
                    "id": chapter_id,
                    "name": providers.clipped_text(chapter.get("name"), 120),
                    "chapterNumber": chapter.get("chapterNumber"),
                    "sourceOrder": chapter.get("sourceOrder"),
                    "pageCount": chapter.get("pageCount"),
                    "volume": chapter.get("volume") or chapter.get("volumeNumber"),
                    "meta_volume": volume_value,
                    "volume_metadata_conflict": volume_evidence.get("conflict") or None,
                    "volume_metadata_malformed": volume_evidence.get("malformed") or None,
                    "pages_fetched": len(pages),
                }.items()
                if value not in (None, "", [], {})
            }
        )
    variant_counts = list(payload.get("variant_result_counts") or [])
    result_count, matching_manga_count = _count_suwayomi_variant_results(variant_counts)
    partial_errors = list(payload.get("partial_errors") or [])
    evidence = {
        key: value
        for key, value in {
            "provider_payload": "suwayomi",
            "source_id": source.get("id") or source.get("sourceId"),
            "source_name": providers.first_text(source.get("displayName"), source.get("name")),
            "source_language": source.get("lang"),
            "source_extension_pkg_name": source.get("extension_pkg_name"),
            "source_extension_version": source.get("extension_version_name"),
            "source_extension_obsolete": source.get("extension_obsolete"),
            "source_extension_has_update": source.get("extension_has_update"),
            "manga_id": manga.get("id"),
            "manga_title": providers.first_text(manga.get("title"), manga.get("name"), manga.get("mangaTitle")),
            "source_search_query": payload.get("source_search_query"),
            "query_variants": list(payload.get("query_variants") or []),
            "variant_result_counts": variant_counts,
            "search_result_count": result_count,
            "matching_manga_count": matching_manga_count,
            "source_search_error_count": sum(
                1
                for row in partial_errors
                if isinstance(row, dict) and str(row.get("stage") or "") == "source_search"
            ),
            "manga_lookup_error_count": sum(
                1
                for row in partial_errors
                if isinstance(row, dict)
                and str(row.get("stage") or "") in {"manga_chapters", "manga_chapters_no_meta_fallback"}
            ),
            "page_lookup_error_count": sum(
                1
                for row in partial_errors
                if isinstance(row, dict)
                and str(row.get("stage") or "") in {"chapter_pages", "chapter_pages_no_meta_fallback"}
            ),
            "suwayomi_extension_health": payload.get("suwayomi_extension_health"),
            "meta_fallbacks": list(payload.get("meta_fallbacks") or []),
            "wanted_unit_type": providers.first_text(wanted_item.get("unitType"), wanted_item.get("unit_type")),
            "wanted_chapter": providers.first_text(wanted_item.get("chapter"), wanted_item.get("chapter_number")),
            "wanted_volume": wanted_volume,
            "chapter_count": len(chapters),
            "chapter_has_volume_count": chapter_has_volume_count,
            "chapter_matching_wanted_volume_count": chapter_matching_wanted_volume_count if wanted_volume else "",
            "chapter_conflicting_volume_count": chapter_conflicting_volume_count,
            "chapter_malformed_volume_count": chapter_malformed_volume_count,
            "chapter_invalid_volume_count": chapter_invalid_volume_count,
            "pages_by_chapter_count": len(pages_by_chapter),
            "chapter_samples": samples,
        }.items()
        if value not in (None, "", [], {})
    }
    reason_evidence = dict(evidence)
    reason_evidence["partial_errors"] = partial_errors
    reason = _suwayomi_no_candidate_reason(reason_evidence)
    if reason:
        evidence["no_candidate_reason"] = reason
    return evidence


def _payload_result_rows(payload):
    if isinstance(payload, dict):
        for key in ("results", "items", "candidates", "matches"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _indexer_result_locator_hash(row):
    row = row if isinstance(row, dict) else {}
    locator = providers.first_text(
        row.get("downloadUrl"),
        row.get("download_url"),
        row.get("download"),
        row.get("magnetUrl"),
        row.get("magnet_url"),
        row.get("magnet"),
        row.get("infoHash"),
        row.get("info_hash"),
        row.get("hash"),
        row.get("guid"),
        row.get("id"),
    )
    return providers.url_hash(locator) if locator else ""


def _indexer_result_match_confidence(row, registry_row=None, wanted_item=None):
    try:
        candidate = providers.prowlarr_candidate_from_result(row, registry_row, wanted_item)
        return str(candidate.get("match_confidence") or "").strip()
    except Exception:
        return ""


def _indexer_no_candidate_result_sample(row, registry_row=None, wanted_item=None):
    row = row if isinstance(row, dict) else {}
    title = providers.first_text(row.get("title"), row.get("releaseTitle"), row.get("release_title"), row.get("name"))
    download_url = providers.first_text(row.get("downloadUrl"), row.get("download_url"), row.get("download"))
    magnet_url = providers.first_text(row.get("magnetUrl"), row.get("magnet_url"), row.get("magnet"))
    info_hash = providers.first_text(row.get("infoHash"), row.get("info_hash"), row.get("hash"))
    guid = providers.first_text(row.get("guid"), row.get("id"))
    categories = providers.category_ids(
        providers.first_value(row.get("categories"), row.get("category"), row.get("categoryIds"), row.get("category_ids"))
    )
    protocol = providers.normalize_protocol(providers.first_value(row.get("protocol"), row.get("downloadProtocol")))
    if not protocol and (magnet_url or info_hash):
        protocol = "torrent"
    locator_hash = _indexer_result_locator_hash(row)
    sample = {
        "title": providers.clipped_text(title, 180),
        "indexer": providers.clipped_text(
            providers.first_text(row.get("indexer"), row.get("indexerName"), row.get("indexer_name")),
            80,
        ),
        "indexer_id": providers.first_text(row.get("indexerId"), row.get("indexer_id"), row.get("indexer_id_int")),
        "protocol": protocol,
        "seeders": providers.int_value(
            providers.first_value(row.get("seeders"), row.get("seedCount"), row.get("seeds")),
            None,
        ),
        "size_bytes": providers.int_value(
            providers.first_value(row.get("size"), row.get("size_bytes"), row.get("sizeBytes")),
            None,
        ),
        "category_ids": categories[:8],
        "query_variant": providers.clipped_text(row.get("_inkdrop_query_variant"), 120),
        "query_group": providers.clipped_text(row.get("_inkdrop_query_group"), 80),
        "pack_query": bool(row.get("_inkdrop_pack_query")),
        "categoryless_fallback": bool(row.get("_inkdrop_categoryless_fallback")),
        "extension": providers.normalize_extension(download_url or title),
        "locator_present": bool(download_url or magnet_url or info_hash or guid),
        "download_url_hash": locator_hash,
        "match_confidence": _indexer_result_match_confidence(row, registry_row, wanted_item),
    }
    return {key: value for key, value in sample.items() if value not in (None, "", [], {})}


def _count_values(values):
    counts = {}
    for value in values or []:
        key = str(value or "").strip() or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _indexer_no_candidate_evidence(payload, wanted_item=None, plan=None, registry_row=None):
    rows = _payload_result_rows(payload)
    evidence = {
        "query_variants": list((payload or {}).get("query_variants") or []) if isinstance(payload, dict) else [],
        "variant_result_counts": list((payload or {}).get("variant_result_counts") or []) if isinstance(payload, dict) else [],
    }
    if not rows:
        return {key: value for key, value in evidence.items() if value not in (None, "", [], {})}

    samples = [
        _indexer_no_candidate_result_sample(row, registry_row, wanted_item)
        for row in rows[:INDEXER_NO_CANDIDATE_SAMPLE_LIMIT]
    ]
    match_confidences = [
        sample.get("match_confidence")
        for sample in samples
        if isinstance(sample, dict) and sample.get("match_confidence")
    ]
    evidence.update(
        {
            "no_candidate_reason": "indexer_payload_title_mismatch"
            if match_confidences and set(match_confidences) == {"mismatch"}
            else "indexer_payload_filtered_before_verdict",
            "payload_result_count": len(rows),
            "payload_sample_count": len([sample for sample in samples if sample]),
            "payload_samples": [sample for sample in samples if sample],
            "match_confidence_counts": _count_values(match_confidences),
            "query_group_counts": _count_values(
                [
                    providers.first_text(row.get("_inkdrop_query_group"), "default")
                    for row in rows
                    if isinstance(row, dict)
                ]
            ),
            "pack_query_result_count": sum(1 for row in rows if isinstance(row, dict) and row.get("_inkdrop_pack_query")),
            "categoryless_fallback_result_count": sum(
                1 for row in rows if isinstance(row, dict) and row.get("_inkdrop_categoryless_fallback")
            ),
        }
    )
    return {key: value for key, value in evidence.items() if value not in (None, "", [], {})}


def _no_candidate_evidence(payload, wanted_item=None, plan=None, registry_row=None):
    plan = plan if isinstance(plan, dict) else {}
    original_payload = payload
    payload = payload if isinstance(payload, dict) else {}
    if str(plan.get("adapter_family") or "").strip() == "suwayomi_api":
        return _suwayomi_no_candidate_evidence(payload, wanted_item)
    if str(plan.get("adapter_family") or "").strip() in INDEXER_RESULT_ADAPTER_FAMILIES:
        return _indexer_no_candidate_evidence(original_payload, wanted_item, plan, registry_row=registry_row)
    evidence = {
        "query_variants": list(payload.get("query_variants") or []),
        "variant_result_counts": list(payload.get("variant_result_counts") or []),
    }
    return {key: value for key, value in evidence.items() if value not in (None, "", [], {})}


def evaluate_source_payload(
    row,
    payload,
    *,
    wanted_item=None,
    plan=None,
    headers=None,
    limit=20,
    staging_root=None,
    query="",
    now=None,
):
    row = row if isinstance(row, dict) else {}
    plan = plan if isinstance(plan, dict) else worker_plan.source_worker_plan_for_row(row)
    now = time.time() if now is None else now
    result = {
        "worker_runtime_contract_version": CONTRACT_VERSION,
        "provider_id": row.get("provider_id"),
        "adapter_family": plan.get("adapter_family"),
        "adapter_id": plan.get("adapter_id"),
        "candidate_parser": plan.get("candidate_parser"),
        "verdict_helper": plan.get("verdict_helper"),
        "attempt_seed_helper": plan.get("attempt_seed_helper"),
        "schedule_state": plan.get("schedule_state"),
        "emits_download_task": bool(plan.get("emits_download_task")),
        "evaluated_at": now,
        "query": providers.normalized_query(query or (wanted_item or {}).get("series_title") or (wanted_item or {}).get("title") or ""),
        "candidates": [],
        "verdicts": [],
        "attempts": [],
        "parse": {},
    }

    if not plan.get("can_search"):
        attempt = source_search_attempt(
            row,
            plan,
            query=result["query"],
            status="blocked",
            reason=_primary_block_reason(plan),
            counts={"candidate_count": 0, "safe_candidate_count": 0, "rejected_candidate_count": 0},
        )
        result["attempts"].append(attempt)
        result["candidate_count"] = 0
        result["safe_candidate_count"] = 0
        result["review_candidate_count"] = 0
        result["blocked_candidate_count"] = 0
        result["status"] = "blocked"
        return result

    candidates, parse = parse_candidates(plan, row, payload, wanted_item=wanted_item, limit=limit)
    result["parse"] = parse
    result["candidates"] = candidates
    if not parse.get("ok"):
        attempt = source_search_attempt(
            row,
            plan,
            query=result["query"],
            status="blocked",
            reason=parse.get("reason") or "candidate_parser_failed",
            counts={"candidate_count": 0, "safe_candidate_count": 0, "rejected_candidate_count": 0},
            raw={"parse": parse},
        )
        result["attempts"].append(attempt)
        result["candidate_count"] = 0
        result["safe_candidate_count"] = 0
        result["review_candidate_count"] = 0
        result["blocked_candidate_count"] = 0
        result["status"] = "blocked"
        return result

    if not candidates:
        raw = {}
        evidence = _no_candidate_evidence(payload, wanted_item, plan, registry_row=row)
        if evidence:
            raw["no_candidate_evidence"] = evidence
        reason = evidence.get("no_candidate_reason") if isinstance(evidence, dict) else ""
        attempt = source_search_attempt(
            row,
            plan,
            query=result["query"],
            status="searched_no_candidates",
            reason=reason or "no_candidates",
            counts={"candidate_count": 0, "safe_candidate_count": 0, "rejected_candidate_count": 0},
            raw=raw,
        )
        result["attempts"].append(attempt)
        result["candidate_count"] = 0
        result["safe_candidate_count"] = 0
        result["review_candidate_count"] = 0
        result["blocked_candidate_count"] = 0
        result["status"] = "searched_no_candidates"
        return result

    for index, candidate in enumerate(candidates):
        verdict = verdict_for_candidate(
            plan,
            row,
            candidate,
            wanted_item=wanted_item,
            headers=headers,
            index=index,
            staging_root=staging_root,
        )
        attempt = attempt_seed_for_verdict(plan, row, verdict, staging_root=staging_root)
        result["verdicts"].append(verdict)
        result["attempts"].append(attempt)

    safe_count = sum(1 for verdict in result["verdicts"] if verdict.get("candidate_outcome") == "auto_grab")
    inspect_count = sum(1 for verdict in result["verdicts"] if verdict.get("candidate_outcome") == "auto_inspect")
    review_count = sum(1 for verdict in result["verdicts"] if verdict.get("candidate_outcome") == "manual_only")
    blocked_count = sum(1 for verdict in result["verdicts"] if verdict.get("candidate_outcome") == "rejected")
    result["candidate_count"] = len(candidates)
    result["safe_candidate_count"] = safe_count
    result["inspect_candidate_count"] = inspect_count
    result["review_candidate_count"] = review_count
    result["blocked_candidate_count"] = blocked_count
    attempt_kinds = {_attempt_kind(attempt) for attempt in result["attempts"]}
    if "sent" in attempt_kinds:
        result["status"] = "sent"
    elif "review" in attempt_kinds:
        result["status"] = "review"
    elif "blocked" in attempt_kinds:
        result["status"] = "blocked"
    else:
        result["status"] = "observed"
    return result


def runtime_summary(results):
    rows = list(results or [])
    by_status = {}
    for row in rows:
        status = str((row or {}).get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "total": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "candidate_count": sum(int((row or {}).get("candidate_count") or 0) for row in rows),
        "safe_candidate_count": sum(int((row or {}).get("safe_candidate_count") or 0) for row in rows),
        "review_candidate_count": sum(int((row or {}).get("review_candidate_count") or 0) for row in rows),
        "blocked_candidate_count": sum(int((row or {}).get("blocked_candidate_count") or 0) for row in rows),
    }
