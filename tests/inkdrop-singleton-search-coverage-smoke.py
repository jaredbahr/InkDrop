#!/usr/bin/env python3
"""Regression coverage for narrowly proven single-issue comic searches."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import inkdrop_candidate_matching as matching
import inkdrop_source_providers as providers
import inkdrop_source_worker_adapters as adapters
import inkdrop_source_worker_coordinator as coordinator
import inkdrop_state
import inkdrop_slskd_source_probe as slskd


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def singleton_target(**extra):
    return {
        "series_title": "The Last Signal",
        "series": "The Last Signal",
        "title": "The Last Signal",
        "query": "The Last Signal 1 1988",
        "issue_number": "1",
        "unit_type": "issue",
        "media_type": "comic",
        "year": 1988,
        "publisher": "Example House",
        "canonical_issue_count": 1,
        "metadata_issue_count": 1,
        "singleton_issue_proof": True,
        "singleton_issue_proof_source": "comicvine_authoritative_count_and_canonical_issue_identity",
        "singleton_metadata_trusted": True,
        "singleton_metadata_fresh": True,
        "singleton_issue_metadata_trusted": True,
        **extra,
    }


def missing_count_collected_target(**extra):
    return singleton_target(
        series_title="Batman Year One",
        series="Batman Year One",
        title="TPB",
        query="Batman Year One",
        metadata_issue_count=0,
        singleton_issue_proof_source="comicvine_collected_single_wanted_identity_without_declared_count",
        collected_singleton_wanted_count=1,
        collected_singleton_markers=["trade_paperback"],
        collected_singleton_proof=True,
        collected_singleton_proof_source="comicvine_collected_single_wanted_identity",
        **extra,
    )


def collected_singleton_target(**extra):
    return {
        "series_title": "Batman: The Court of Owls Saga: DC Essential Edition",
        "series": "Batman: The Court of Owls Saga: DC Essential Edition",
        "title": "TPB",
        "query": "Batman: The Court of Owls Saga: DC Essential Edition 1 2018",
        "issue_number": "1",
        "unit_type": "issue",
        "media_type": "comic",
        "year": 2018,
        "publisher": "DC Comics",
        "canonical_issue_count": 1,
        "metadata_issue_count": 0,
        "singleton_metadata_trusted": True,
        "singleton_metadata_fresh": True,
        "singleton_issue_metadata_trusted": True,
        "collected_singleton_wanted_count": 1,
        "collected_singleton_markers": ["essential_edition", "trade_paperback"],
        "collected_singleton_proof": True,
        "collected_singleton_proof_source": "comicvine_collected_single_wanted_identity",
        **extra,
    }


def matching_contract():
    target = singleton_target()
    exact = matching.candidate_compatibility(
        {"title": "The Last Signal (1988) (Digital)"},
        target,
    )
    require(exact["status"] == "compatible", f"proven exact-title singleton was rejected: {exact}")
    require("singleton_exact_title" in exact["positive_evidence"], f"singleton evidence missing: {exact}")

    provider_verdict = providers.indexer_candidate_verdict(
        {
            "provider_id": "prowlarr",
            "title": "The Last Signal (1988) (Digital)",
            "protocol": "usenet",
            "download_url": "https://provider.invalid/synthetic-result",
            "issue_number": "1",
            "match_confidence": "series_title_only",
        },
        {
            "provider_id": "prowlarr",
            "registry_state": "ready",
            "source_mode": "auto",
            "auto_search_allowed": True,
            "auto_download_allowed": True,
        },
    )
    require("issue_number_not_confirmed" in provider_verdict["review_reasons"], provider_verdict)
    applied = matching.apply_compatibility(provider_verdict, target)
    require(applied["candidate_safe"] is True, f"singleton proof did not clear legacy number review: {applied}")
    require(applied["auto_grab_verdict"] == "auto_grab_safe", f"singleton remained review-only: {applied}")

    missing_proof = matching.candidate_compatibility(
        {"title": "The Last Signal (1988) (Digital)"},
        {**target, "singleton_issue_proof": False},
    )
    require(missing_proof["status"] == "review", f"unproven singleton bypassed unit gate: {missing_proof}")
    untrusted_proof = matching.candidate_compatibility(
        {"title": "The Last Signal (1988) (Digital)"},
        {**target, "singleton_issue_proof_source": "manual_declared_count"},
    )
    require(untrusted_proof["status"] == "review", f"untrusted proof source bypassed unit gate: {untrusted_proof}")
    real_series = matching.candidate_compatibility(
        {"title": "The Last Signal (1988) (Digital)"},
        {**target, "canonical_issue_count": 2, "metadata_issue_count": 2},
    )
    require(real_series["status"] == "review", f"multi-issue series bypassed unit gate: {real_series}")
    non_comic = matching.candidate_compatibility(
        {"title": "The Last Signal (1988) (Digital)"},
        {**target, "media_type": "manga"},
    )
    require(non_comic["status"] == "review", f"non-comic target used singleton comic proof: {non_comic}")

    negative_cases = (
        ({"title": "The Last Signal (1989) (Digital)"}, "wrong year"),
        ({"title": "The Last Signal (1988) (Other House)"}, "wrong publisher suffix"),
        ({"title": "The Last Signal #2"}, "conflicting issue"),
        ({"title": "The Last Signal", "filename": "The Last Signal #2.cbz"}, "filename unit conflict"),
        ({"title": "The Last Signal", "filename": "The Last Signal (1989).cbz"}, "filename year conflict"),
        ({"title": "The Last Signal issues #1-2"}, "range"),
        ({"title": "The Last Signal Pack"}, "pack"),
        ({"title": "The Last Signal Complete Collection"}, "collection"),
        ({"title": "The Last Signal Preview"}, "preview"),
        ({"title": "The Last Signal Sample"}, "sample"),
        ({"title": "The Last Signal Anniversary Edition"}, "non-exact title"),
    )
    for candidate, label in negative_cases:
        verdict = matching.candidate_compatibility(candidate, target)
        require(
            "singleton_exact_title" not in verdict["positive_evidence"] and verdict["status"] != "compatible",
            f"{label} candidate used singleton exception: {verdict}",
        )

    year_one = missing_count_collected_target()
    for title in (
        "Batman Year One TPB 1987",
        "Batman Year One Deluxe Edition 2007",
        "Batman Year One (1987)",
        "Batman Year One (2007) (Digital)",
        "Batman Year One 001 (January 1988).cbz",
    ):
        compatibility = matching.candidate_compatibility({"title": title}, year_one)
        require(compatibility["status"] == "compatible", f"missing-count collected book was rejected: {title}: {compatibility}")
        require(
            {"singleton_exact_title", "singleton_exact_bare_volume_number", "exact_issue_number"}
            & set(compatibility["positive_evidence"]),
            f"missing-count singleton evidence was absent: {title}: {compatibility}",
        )
    for title in (
        "Batman Year One Scarecrow 001.cbz",
        "Batman Year One Ra's al Ghul 001.cbz",
        "Batman Year One Batman and Robin 001.cbz",
        "Batman Year One 001-004.cbz",
        "Batman Year One Preview.cbz",
        "Batman Year One Sample.cbz",
    ):
        compatibility = matching.candidate_compatibility(
            {"title": title, "match_confidence": "mismatch"},
            year_one,
        )
        require(compatibility["status"] != "compatible", f"unsafe Year One candidate escaped singleton gates: {title}: {compatibility}")

    prowlarr_row = {
        "provider_id": "prowlarr_dognzb_comics",
        "provider_type": "indexer",
        "source_kind": "prowlarr_indexer",
        "registry_state": "ready",
        "source_mode": "auto",
        "auto_search_allowed": True,
        "auto_download_allowed": True,
    }
    for title in (
        "Batman Year One TPB 1987",
        "Batman Year One Deluxe Edition 2007",
        "Batman Year One 001 (January 1988).cbz",
    ):
        parsed = providers.prowlarr_candidates_from_results(
            [{
                "title": title,
                "protocol": "usenet",
                "guid": f"synthetic-{title}",
                "downloadUrl": "https://provider.invalid/year-one",
            }],
            prowlarr_row,
            year_one,
            limit=20,
        )
        require(len(parsed) == 1, f"Year One result vanished before provider verdict: {title}: {parsed}")
        require(
            parsed[0]["match_confidence"] == "related_series_identity",
            f"regression fixture no longer exercises provider related-series confidence: {title}: {parsed[0]}",
        )
        provider_verdict = providers.indexer_candidate_verdict(parsed[0], prowlarr_row)
        require("related_series_identity" in provider_verdict["block_reasons"], provider_verdict)
        pipeline_verdict = matching.apply_compatibility(provider_verdict, year_one)
        require(
            pipeline_verdict["candidate_safe"] is True
            and pipeline_verdict["auto_grab_verdict"] == "auto_grab_safe"
            and "related_series_identity" not in pipeline_verdict["block_reasons"],
            f"trusted Year One singleton stayed blocked in the Prowlarr pipeline: {title}: {pipeline_verdict}",
        )

    for title in (
        "Batman Year One Scarecrow 001.cbz",
        "Batman Year One Ra's al Ghul 001.cbz",
        "Batman Year One Batman and Robin 001.cbz",
        "Batman Year One 002.cbz",
        "Batman Year One 001-004.cbz",
        "Batman Year One Preview.cbz",
    ):
        parsed = providers.prowlarr_candidates_from_results(
            [{
                "title": title,
                "protocol": "usenet",
                "guid": f"synthetic-{title}",
                "downloadUrl": "https://provider.invalid/year-one-negative",
            }],
            prowlarr_row,
            year_one,
            limit=20,
        )
        require(len(parsed) == 1, f"unsafe Year One fixture vanished before provider verdict: {title}: {parsed}")
        pipeline_verdict = matching.apply_compatibility(
            providers.indexer_candidate_verdict(parsed[0], prowlarr_row),
            year_one,
        )
        require(
            pipeline_verdict["candidate_safe"] is False
            and pipeline_verdict["auto_grab_verdict"] != "auto_grab_safe",
            f"unsafe Year One candidate escaped the Prowlarr pipeline: {title}: {pipeline_verdict}",
        )

    collected = collected_singleton_target()
    court_title = (
        "Batman.-.The.Court.of.Owls.Saga.DC.Essential.Edition.2018.digital."
        "F.Son.of.Ultron-Empire"
    )
    collected_exact = matching.candidate_compatibility({"title": court_title}, collected)
    require(collected_exact["status"] == "compatible", f"exact collected singleton was rejected: {collected_exact}")
    require(
        "collected_singleton_exact_title" in collected_exact["positive_evidence"],
        f"collected singleton proof was not used: {collected_exact}",
    )
    provider_verdict = providers.indexer_candidate_verdict(
        {
            "provider_id": "prowlarr",
            "title": court_title,
            "protocol": "usenet",
            "download_url": "https://provider.invalid/court-essential",
            "match_confidence": "series_title_only",
        },
        {
            "provider_id": "prowlarr_dognzb_comics",
            "registry_state": "ready",
            "source_mode": "auto",
            "auto_search_allowed": True,
            "auto_download_allowed": True,
        },
    )
    collected_applied = matching.apply_compatibility(provider_verdict, collected)
    require(collected_applied["candidate_safe"] is True, collected_applied)
    require(collected_applied["auto_grab_verdict"] == "auto_grab_safe", collected_applied)

    collected_negative_cases = (
        (
            collected_singleton_target(collected_singleton_proof=False),
            court_title,
            "missing trusted collected-singleton proof",
        ),
        (
            collected_singleton_target(
                canonical_issue_count=2,
                collected_singleton_wanted_count=2,
            ),
            court_title,
            "normal multi-issue series",
        ),
        (
            collected,
            "Batman: The Court of Owls Saga: DC Essential Edition Volume 2",
            "conflicting numbered collected edition",
        ),
        (
            collected,
            "Batman: The Court of Owls Saga: DC Essential Edition Omnibus",
            "conflicting collected marker",
        ),
        (
            collected,
            "Batman: The Court of Owls Saga: DC Essential Edition Beyond 2018 digital Empire",
            "different-title extension before release metadata",
        ),
    )
    for target, title, label in collected_negative_cases:
        verdict = matching.candidate_compatibility({"title": title}, target)
        require(
            "collected_singleton_exact_title" not in verdict["positive_evidence"]
            and verdict["status"] != "compatible",
            f"{label} used collected singleton exception: {verdict}",
        )

    absolute_target = collected_singleton_target(
        series_title="Absolute Batman: The Court of Owls",
        series="Absolute Batman: The Court of Owls",
        title="HC",
        query="Absolute Batman: The Court of Owls 1 2015",
        year=2015,
        collected_singleton_markers=["hardcover"],
        collected_singleton_title_aliases=["Batman The Court of Owls", "The Court of Owls"],
    )
    absolute_candidate = {
        "filename": r"Comics\Batman v01 - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr",
        "title": r"Comics\Batman v01 - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr",
        "username": "fixture-peer",
        "size": 187_049_466,
        "extension": ".cbr",
        "score": 89,
        "has_free_upload_slot": True,
        "upload_speed": 1_000_000,
        "queue_length": 0,
        "locked": False,
    }
    absolute_compatibility = matching.candidate_compatibility(absolute_candidate, absolute_target)
    require(absolute_compatibility["status"] == "compatible", absolute_compatibility)
    require(
        "collected_singleton_alias_volume" in absolute_compatibility["positive_evidence"],
        absolute_compatibility,
    )
    absolute_gate = slskd.auto_grab_candidate_verdict(absolute_candidate, absolute_target)
    require(absolute_gate["verdict"] == "auto_grab_safe" and absolute_gate["autopick_eligible"], absolute_gate)
    automatic_rows, automatic_summary = slskd.candidates_from_responses(
        [{
            "username": "fixture-peer",
            "hasFreeUploadSlot": True,
            "uploadSpeed": 1_000_000,
            "files": [{
                "filename": absolute_candidate["filename"],
                "size": absolute_candidate["size"],
            }],
        }],
        absolute_target,
    )
    require(
        len(automatic_rows) == 1
        and (automatic_rows[0].get("auto_grab") or {}).get("verdict") == "auto_grab_safe"
        and (automatic_rows[0].get("auto_grab") or {}).get("autopick_eligible"),
        {
            "rows": automatic_rows,
            "summary": automatic_summary,
            "reason": "automatic normalization must reach the same shared gate as Manual Search",
        },
    )
    punctuated_candidate = {
        **absolute_candidate,
        "filename": r"Comics\Batman - The Court.of.Owls v01 (2012) (digital) (Minutemen-PhD).cbr",
        "title": r"Comics\Batman - The Court.of.Owls v01 (2012) (digital) (Minutemen-PhD).cbr",
    }
    punctuated_gate = slskd.auto_grab_candidate_verdict(punctuated_candidate, absolute_target)
    require(
        punctuated_gate["verdict"] == "auto_grab_safe" and punctuated_gate["autopick_eligible"],
        {"reason": "equivalent punctuation lost exact collected-artifact identity", "gate": punctuated_gate},
    )

    related_folder_wrong_leaf = {
        **absolute_candidate,
        "filename": r"@@yjvqn\Comics\Batman\Court of Owls and Nights of the Owls\001 Batman 001 (7 covers) (2011) (Megan-Empire).cbr",
        "title": r"@@yjvqn\Comics\Batman\Court of Owls and Nights of the Owls\001 Batman 001 (7 covers) (2011) (Megan-Empire).cbr",
        "score": 81,
    }
    wrong_leaf_gate = slskd.auto_grab_candidate_verdict(related_folder_wrong_leaf, absolute_target)
    require(
        wrong_leaf_gate["verdict"] != "auto_grab_safe"
        and not wrong_leaf_gate["autopick_eligible"],
        {"reason": "ancestor folder overrode the generic leaf artifact identity", "gate": wrong_leaf_gate},
    )
    disagreeing_identity_candidate = {
        **related_folder_wrong_leaf,
        "title": absolute_candidate["filename"],
        "original_result_title": absolute_candidate["filename"],
        "remote_filename": absolute_candidate["filename"],
        "path": r"Comics\Batman v01 - The Court of Owls (2012)\release.cbr",
        "source_volume_number": "1",
        "provider_volume_number": "1",
        "volume": "1",
    }
    disagreement_details = slskd.shared_candidate_match_details(
        disagreeing_identity_candidate["filename"],
        absolute_target,
        candidate=disagreeing_identity_candidate,
    )
    disagreement_gate = slskd.auto_grab_candidate_verdict(
        disagreeing_identity_candidate,
        absolute_target,
    )
    require(
        not disagreement_details.get("matched")
        and disagreement_gate["verdict"] != "auto_grab_safe"
        and not disagreement_gate["autopick_eligible"],
        {
            "reason": "alternate result identity or unit fields overrode the actual wrong leaf",
            "details": disagreement_details,
            "gate": disagreement_gate,
        },
    )
    veto_cases = (
        (
            "known bad flag",
            {**absolute_candidate, "known_bad_candidate": True},
            absolute_target,
            "known_bad_candidate",
        ),
        (
            "durable known bad memory",
            {**absolute_candidate, "source_memory_status": "known_bad"},
            absolute_target,
            "known_bad_candidate",
        ),
        (
            "explicit preview flag",
            {**absolute_candidate, "preview_or_sample": True},
            absolute_target,
            "preview_or_sample",
        ),
        (
            "alternate preview title",
            {**absolute_candidate, "original_result_title": "Batman - The Court of Owls Preview v01 (2012).cbr"},
            absolute_target,
            "preview_or_sample",
        ),
        (
            "provider mismatch",
            {**absolute_candidate, "match_confidence": "mismatch"},
            absolute_target,
            "candidate_title_mismatch",
        ),
        (
            "provider related work",
            {**absolute_candidate, "match_confidence": "related_series_identity"},
            absolute_target,
            "related_series_identity",
        ),
        (
            "conflicting asserted creator",
            {
                **absolute_candidate,
                "original_result_title": "Batman - The Court of Owls by Tom King v01 (2012) digital.cbr",
            },
            {**absolute_target, "creators": ["Scott Snyder"]},
            "creator_identity_conflict",
        ),
    )
    for label, candidate, target, rejection_code in veto_cases:
        veto_details = slskd.shared_candidate_match_details(
            candidate["filename"],
            target,
            candidate=candidate,
        )
        veto_gate = slskd.auto_grab_candidate_verdict(candidate, target)
        require(
            not veto_details.get("matched")
            and rejection_code in (veto_details.get("target_compatibility") or {}).get("rejection_codes", [])
            and veto_gate["verdict"] == "blocked"
            and not veto_gate["autopick_eligible"]
            and rejection_code in veto_gate["blockers"],
            {
                "reason": f"{label} veto was lost by strict leaf identity projection",
                "details": veto_details,
                "gate": veto_gate,
            },
        )
    wrong_leaf_response = [{
        "username": "fixture-peer",
        "hasFreeUploadSlot": True,
        "uploadSpeed": 1_000_000,
        "files": [{
            "filename": related_folder_wrong_leaf["filename"],
            "size": related_folder_wrong_leaf["size"],
        }],
    }]
    wrong_leaf_rows, wrong_leaf_summary = slskd.candidates_from_responses(
        wrong_leaf_response,
        absolute_target,
    )
    repeated_wrong_leaf_rows, repeated_wrong_leaf_summary = slskd.candidates_from_responses(
        wrong_leaf_response,
        absolute_target,
    )
    require(
        wrong_leaf_rows == []
        and repeated_wrong_leaf_rows == []
        and wrong_leaf_summary == repeated_wrong_leaf_summary,
        {
            "reason": "live wrong-leaf response was not rejected idempotently",
            "rows": wrong_leaf_rows,
            "summary": wrong_leaf_summary,
            "repeated_rows": repeated_wrong_leaf_rows,
            "repeated_summary": repeated_wrong_leaf_summary,
        },
    )
    manifest_backed_candidate = {
        **related_folder_wrong_leaf,
        "pack_contents_match": {
            "coverage_source": "pack_contents_volume_filename",
            "entry": "Batman v01 - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr",
        },
    }
    manifest_backed_details = slskd.shared_candidate_match_details(
        manifest_backed_candidate["filename"],
        absolute_target,
        candidate=manifest_backed_candidate,
    )
    require(
        manifest_backed_details.get("matched")
        and manifest_backed_details.get("candidate_identity_text")
        == manifest_backed_candidate["pack_contents_match"]["entry"],
        {
            "reason": "authoritative manifest entry did not supply leaf artifact identity",
            "details": manifest_backed_details,
        },
    )
    automatic_queries = slskd.source_queries({**absolute_target, "issue": "1"})
    require(
        automatic_queries[0] == "The Court of Owls",
        f"proven automatic singleton must use Manual Search's structural discovery anchor first: {automatic_queries[:5]}",
    )
    unproven_queries = slskd.source_queries({**absolute_target, "issue": "1", "collected_singleton_proof": False})
    require(
        unproven_queries[0] != "The Court of Owls",
        f"unproven rows must not borrow the shortened unattended query anchor: {unproven_queries[:5]}",
    )
    generic_anchor_queries = slskd.source_queries({
        **absolute_target,
        "series": "Deluxe Descender: The Machine Moon",
        "series_title": "Deluxe Descender: The Machine Moon",
        "singleton_series_title": "Deluxe Descender: The Machine Moon",
        "collected_singleton_title_aliases": ["Descender The Machine Moon", "The Machine Moon"],
        "issue": "4",
        "issue_number": "4",
    })
    require(generic_anchor_queries[0] == "The Machine Moon", generic_anchor_queries[:5])

    # A higher downloader score on a collected edition must not suppress the
    # safe exact unit. Ranking operates only within compatibility-eligible
    # candidates, so the exact unit becomes the sole unattended handoff.
    scored_exact = {**absolute_candidate, "score": 85}
    scored_omnibus = {
        **absolute_candidate,
        "filename": "Batman - The Court of Owls Omnibus v01 (2012).cbr",
        "title": "Batman - The Court of Owls Omnibus v01 (2012).cbr",
        "score": 99,
    }
    ranked = slskd.annotate_auto_grab_verdicts([scored_omnibus, scored_exact], absolute_target)
    safe = slskd.ranked_auto_grab_candidates({"candidates": ranked})
    require(len(safe) == 1 and safe[0]["filename"] == scored_exact["filename"], ranked)
    require(
        "lower-ranked autopick candidate" not in (safe[0].get("auto_grab") or {}).get("review_reasons", []),
        safe[0],
    )

    alias_volume_negatives = (
        ({**absolute_target, "collected_singleton_proof": False}, absolute_candidate, "missing proof"),
        ({**absolute_target, "canonical_issue_count": 2, "collected_singleton_wanted_count": 2}, absolute_candidate, "multi issue"),
        (absolute_target, {**absolute_candidate, "title": "Batman Beyond v01 (2012).cbr", "filename": "Batman Beyond v01 (2012).cbr"}, "Batman Beyond"),
        (absolute_target, {**absolute_candidate, "title": "Batman (New 52) v01 - The Court of Owls (2012).cbr", "filename": "Batman (New 52) v01 - The Court of Owls (2012).cbr"}, "wrong edition"),
        (absolute_target, {**absolute_candidate, "title": "Batman v02 - The Court of Owls (2012).cbr", "filename": "Batman v02 - The Court of Owls (2012).cbr"}, "wrong volume"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01 - The Court of Owls (2016).cbr", "filename": "Batman v01 - The Court of Owls (2016).cbr"}, "future year"),
        (absolute_target, {**absolute_candidate, "title": "Batman - The Court of Owls Omnibus v01 (2012).cbr", "filename": "Batman - The Court of Owls Omnibus v01 (2012).cbr"}, "omnibus"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01-v02 - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr", "filename": "Batman v01-v02 - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr"}, "compact hyphen range"),
        (absolute_target, {**absolute_candidate, "title": "Batman v1_2 - The Court of Owls (2012).cbr", "filename": "Batman v1_2 - The Court of Owls (2012).cbr"}, "compact underscore range"),
        (absolute_target, {**absolute_candidate, "title": "Batman v1+2 - The Court of Owls (2012).cbr", "filename": "Batman v1+2 - The Court of Owls (2012).cbr"}, "compact plus range"),
        (absolute_target, {**absolute_candidate, "title": "Batman v1 to v2 - The Court of Owls (2012).cbr", "filename": "Batman v1 to v2 - The Court of Owls (2012).cbr"}, "compact word range"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01 & v02 - The Court of Owls (2012).cbr", "filename": "Batman v01 & v02 - The Court of Owls (2012).cbr"}, "ampersand volume pair"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01,v02 - The Court of Owls (2012).cbr", "filename": "Batman v01,v02 - The Court of Owls (2012).cbr"}, "comma volume pair"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01/v02 - The Court of Owls (2012).cbr", "filename": "Batman v01/v02 - The Court of Owls (2012).cbr"}, "slash volume pair"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01;v02 - The Court of Owls (2012).cbr", "filename": "Batman v01;v02 - The Court of Owls (2012).cbr"}, "semicolon volume pair"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01|v02 - The Court of Owls (2012).cbr", "filename": "Batman v01|v02 - The Court of Owls (2012).cbr"}, "pipe volume pair"),
        (absolute_target, {**absolute_candidate, "title": r"Batman v01\v02 - The Court of Owls (2012).cbr", "filename": r"Batman v01\v02 - The Court of Owls (2012).cbr"}, "backslash volume pair"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01 and v02 - The Court of Owls (2012).cbr", "filename": "Batman v01 and v02 - The Court of Owls (2012).cbr"}, "and volume pair"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01 v02 - The Court of Owls (2012).cbr", "filename": "Batman v01 v02 - The Court of Owls (2012).cbr"}, "adjacent volume pair"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01 (Beyond) - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr", "filename": "Batman v01 (Beyond) - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr"}, "parenthetical identity collision"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01 [Beyond] - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr", "filename": "Batman v01 [Beyond] - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr"}, "bracket identity collision"),
        (absolute_target, {**absolute_candidate, "title": "Batman.v01.-.The.Court.of.Owls.of.2012.digital", "filename": ""}, "identity-bearing of suffix"),
        (absolute_target, {**absolute_candidate, "title": "Batman.v01.-.The.Court.of.Owls.Son.of.Ultron.2012.digital", "filename": ""}, "incomplete release-group suffix"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01 - The Court of Owls (Son of Ultron) (2012) digital.cbr", "filename": ""}, "parenthesized incomplete release group"),
        (absolute_target, {**absolute_candidate, "title": "Batman v01 - The Court of Owls (of) (2012) digital.cbr", "filename": ""}, "parenthesized identity word"),
    )
    for target, candidate, label in alias_volume_negatives:
        verdict = matching.candidate_compatibility(candidate, target)
        require(
            "collected_singleton_alias_volume" not in verdict["positive_evidence"]
            and verdict["status"] != "compatible",
            f"{label} used alias-volume singleton exception: {verdict}",
        )
        gate = slskd.auto_grab_candidate_verdict(candidate, target)
        require(
            gate["verdict"] != "auto_grab_safe" and not gate["autopick_eligible"],
            f"{label} reached unattended SLSKD handoff: {gate}",
        )
        automatic_negative_rows, _ = slskd.candidates_from_responses(
            [{
                "username": "fixture-peer",
                "hasFreeUploadSlot": True,
                "uploadSpeed": 1_000_000,
                "files": [{
                    "filename": candidate.get("filename") or candidate.get("title"),
                    "size": candidate.get("size") or absolute_candidate["size"],
                }],
            }],
            target,
        )
        require(
            not any(
                (row.get("auto_grab") or {}).get("verdict") == "auto_grab_safe"
                or (row.get("auto_grab") or {}).get("autopick_eligible")
                for row in automatic_negative_rows
            ),
            f"{label} escaped through automatic response normalization: {automatic_negative_rows}",
        )

    later_marker_conflict = matching.candidate_compatibility(
        {
            "title": court_title,
            "filename": "Batman - The Court of Owls Saga - DC Essential Edition Omnibus (2018).cbz",
        },
        collected,
    )
    require(
        "collected_singleton_exact_title" not in later_marker_conflict["positive_evidence"]
        and later_marker_conflict["status"] != "compatible",
        f"later source marker conflict was ignored: {later_marker_conflict}",
    )
    source_year_conflict = matching.candidate_compatibility(
        {
            "title": court_title,
            "filename": "Batman - The Court of Owls Saga - DC Essential Edition (2019).cbz",
        },
        collected,
    )
    require(
        "collected_singleton_exact_title" not in source_year_conflict["positive_evidence"]
        and source_year_conflict["status"] != "compatible",
        f"later source year conflict was ignored: {source_year_conflict}",
    )
    later_order_same_title_marker = matching.candidate_compatibility(
        {"title": court_title + ".Deluxe.Edition"},
        collected,
    )
    require(
        "collected_singleton_exact_title" not in later_order_same_title_marker["positive_evidence"]
        and later_order_same_title_marker["status"] != "compatible",
        f"later-order same-title marker was ignored: {later_order_same_title_marker}",
    )
    batman_prefix = matching.candidate_compatibility(
        {"title": "Batman Beyond TPB (2018) digital"},
        collected_singleton_target(
            series_title="Batman",
            series="Batman",
            query="Batman 1 2018",
            collected_singleton_markers=["trade_paperback"],
        ),
    )
    require(
        "collected_singleton_exact_title" not in batman_prefix["positive_evidence"]
        and batman_prefix["status"] != "compatible",
        f"Batman prefix collision admitted Batman Beyond: {batman_prefix}",
    )
    publisher_word_prefix = matching.candidate_compatibility(
        {"title": "Batman Beyond TPB 2018"},
        collected_singleton_target(
            series_title="Batman",
            series="Batman",
            query="Batman 1 2018",
            publisher="Beyond",
            collected_singleton_markers=["trade_paperback"],
        ),
    )
    require(
        "collected_singleton_exact_title" not in publisher_word_prefix["positive_evidence"]
        and publisher_word_prefix["status"] != "compatible",
        f"publisher token admitted Batman Beyond: {publisher_word_prefix}",
    )


def adapter_contract():
    require(
        adapters._proven_singleton_comic_wanted(missing_count_collected_target()),
        "indexer planning discarded the trusted missing-count singleton proof",
    )
    require(
        not adapters._proven_singleton_comic_wanted(
            missing_count_collected_target(canonical_issue_count=2)
        ),
        "multi-issue identity used the missing-count singleton proof",
    )
    row = {
        "provider_id": "prowlarr",
        "provider_type": "indexer",
        "source_kind": "prowlarr_indexer",
        "base_url": "https://provider.invalid/api/v1",
        "policy": {"max_query_variants": 3, "disable_weekly_pack_queries": True},
    }
    requests = adapters.prowlarr_search_requests(row, {}, singleton_target(), limit=20)
    queries = [request["params"]["query"] for request in requests]
    require(len(queries) == 3, f"singleton query plan exceeded its cap: {queries}")
    require(
        queries[:2] == ["The Last Signal", "The Last Signal 1 1988"],
        f"singleton plan is not broad-first with an exact-unit follow-up: {queries}",
    )

    child_row = {**row, "provider_id": "prowlarr_dognzb_comics"}
    child_requests = adapters.prowlarr_search_requests(child_row, {}, singleton_target(), limit=20)
    child_queries = [request["params"]["query"] for request in child_requests]
    require(len(child_queries) == 3, f"concrete Prowlarr lane exceeded its cap: {child_queries}")
    require(
        child_queries[:2] == ["The Last Signal", "The Last Signal 1 1988"],
        f"concrete Prowlarr lane is not broad-first with an exact-unit follow-up: {child_queries}",
    )

    one_query_row = {**row, "policy": {**row["policy"], "max_query_variants": 1}}
    one_query = adapters.prowlarr_search_requests(one_query_row, {}, singleton_target(), limit=20)
    one_queries = [request["params"]["query"] for request in one_query]
    require(one_queries == ["The Last Signal"], f"cap=1 did not preserve broad-first discovery: {one_queries}")

    multi = adapters.prowlarr_search_requests(
        row,
        {},
        singleton_target(canonical_issue_count=2, metadata_issue_count=2),
        limit=20,
    )
    multi_queries = [request["params"]["query"] for request in multi]
    require(
        multi_queries[:2] == ["The Last Signal", "The Last Signal 1 1988"],
        f"multi-issue discovery lost broad-first/exact-unit coverage: {multi_queries}",
    )
    require(
        adapters._comic_series_fallback_queries_enabled(
            row,
            singleton_target(canonical_issue_count=2, metadata_issue_count=2),
        )
        is True,
        "generic Prowlarr comic lane lost its capped broad discovery fallback",
    )
    require(
        adapters._proven_singleton_comic_wanted(
            singleton_target(canonical_issue_count=2, metadata_issue_count=2)
        )
        is False,
        "multi-issue target gained singleton proof semantics",
    )

    manga = adapters.prowlarr_search_requests(row, {}, singleton_target(media_type="manga"), limit=20)
    manga_queries = [request["params"]["query"] for request in manga]
    require(
        manga_queries[:2] == ["The Last Signal", "The Last Signal 1 1988"],
        f"non-comic discovery lost broad-first/exact-unit coverage: {manga_queries}",
    )
    require(
        adapters._comic_series_fallback_queries_enabled(row, singleton_target(media_type="manga")) is False,
        "non-comic target gained the singleton-specific fallback policy",
    )

    for provider_id, source_kind in (("torznab_fixture", "torznab_indexer"), ("newznab_fixture", "newznab_indexer")):
        other_row = {
            **row,
            "provider_id": provider_id,
            "source_kind": source_kind,
            "policy": {"max_query_variants": 3, "disable_weekly_pack_queries": True},
        }
        other_queries, _ = adapters._indexer_query_variants(other_row, singleton_target())
        require(
            other_queries[:2] == ["The Last Signal", "The Last Signal 1 1988"],
            f"{source_kind} discovery lost broad-first/exact-unit coverage: {other_queries}",
        )
        require(
            adapters._comic_series_fallback_queries_enabled(other_row, singleton_target()) is False,
            f"{source_kind} gained implicit singleton fallback policy",
        )

    parsed = providers.prowlarr_candidates_from_results(
        [
            {
                "title": "The Last Signal (1988) (Other House)",
                "protocol": "usenet",
                "guid": "synthetic-wrong-publisher-title",
                "downloadUrl": "https://provider.invalid/synthetic-wrong-publisher",
                "_inkdrop_query_variant": "The Last Signal",
                "_inkdrop_query_group": "issue",
                "_inkdrop_request_id": "prowlarr_search_variant_2",
            }
        ],
        child_row,
        singleton_target(),
        limit=20,
    )
    require(len(parsed) == 1, f"wrong-publisher title vanished before shared diagnostics: {parsed}")
    pipeline_verdict = matching.apply_compatibility(
        providers.indexer_candidate_verdict(parsed[0], child_row),
        singleton_target(),
    )
    require(
        pipeline_verdict["candidate_safe"] is False
        and "singleton_exact_title" not in pipeline_verdict["target_compatibility"]["positive_evidence"],
        f"wrong-publisher Prowlarr pipeline candidate used singleton exception: {pipeline_verdict}",
    )


def _insert_series(
    con,
    series_id,
    issue_count,
    *,
    metadata_provider="comicvine",
    metadata_id="101",
    source="comicvine",
    updated_at=None,
    title="The Last Signal",
):
    updated_at = time.time() if updated_at is None else updated_at
    con.execute(
        "insert into series(id,title,media_type,year,publisher,metadata_provider,metadata_id,source,updated_at,raw_json) "
        "values(?,?,?,?,?,?,?,?,?,?)",
        (
            series_id,
            title,
            "comic",
            1988,
            "Example House",
            metadata_provider,
            metadata_id,
            source,
            updated_at,
            json.dumps({} if issue_count is None else {"metadata": {"count_of_issues": issue_count}}),
        ),
    )


def _insert_issue(
    con,
    series_id,
    issue_id,
    number,
    *,
    metadata_provider="comicvine",
    metadata_id="1001",
    title="",
):
    con.execute(
        "insert into issues(id,series_id,issue_number,normalized_number,title,metadata_provider,metadata_id) "
        "values(?,?,?,?,?,?,?)",
        (
            issue_id,
            series_id,
            str(number),
            inkdrop_state.normalize_issue_number(number),
            title,
            metadata_provider,
            metadata_id,
        ),
    )


def coordinator_contract():
    with tempfile.TemporaryDirectory(prefix="inkdrop-singleton-") as temp_dir:
        db_path = Path(temp_dir) / "state.db"
        inkdrop_state.ensure_schema(db_path)
        with inkdrop_state.connect(db_path) as con:
            _insert_series(con, "comicvine:101", 1, metadata_id="101")
            _insert_issue(con, "comicvine:101", "issue:singleton:a", 1, metadata_id="1001")
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:singleton", "comicvine:101", "issue:singleton:a", "wanted"),
            )
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,issue_id,state,query,active) values(?,?,?,?,?,?,?)",
                (
                    "queue:singleton",
                    "wanted:singleton",
                    "comicvine:101",
                    "issue:singleton:a",
                    "searching",
                    "The Last Signal 1 1988",
                    1,
                ),
            )

            _insert_series(con, "comicvine:202", 2, metadata_id="202")
            _insert_issue(con, "comicvine:202", "issue:multi:1", 1, metadata_id="2001")
            _insert_issue(con, "comicvine:202", "issue:multi:2", 2, metadata_id="2002")
            _insert_series(
                con,
                "series:untrusted",
                1,
                metadata_provider="fixture",
                metadata_id="fixture-1",
                source="fixture",
            )
            _insert_issue(
                con,
                "series:untrusted",
                "issue:untrusted:1",
                1,
                metadata_provider="fixture",
                metadata_id="fixture-issue-1",
            )
            _insert_series(
                con,
                "series:manual",
                1,
                metadata_provider="manual",
                metadata_id="manual-1",
                source="manual",
            )
            _insert_issue(
                con,
                "series:manual",
                "issue:manual:1",
                1,
                metadata_provider="manual",
                metadata_id="manual-issue-1",
            )
            _insert_series(
                con,
                "comicvine:303",
                1,
                metadata_id="303",
                updated_at=time.time() - coordinator.SINGLETON_METADATA_MAX_AGE_SECONDS - 1,
            )
            _insert_issue(con, "comicvine:303", "issue:stale:1", 1, metadata_id="3001")
            _insert_series(con, "comicvine:404", 1, metadata_id="404")
            _insert_issue(
                con,
                "comicvine:404",
                "issue:manual-under-trusted-series",
                1,
                metadata_provider="manual",
                metadata_id="manual-under-trusted",
            )
            _insert_series(
                con,
                "comicvine:505",
                0,
                metadata_id="505",
                title="Batman: The Court of Owls Saga: DC Essential Edition",
            )
            _insert_issue(
                con,
                "comicvine:505",
                "issue:collected:1",
                1,
                metadata_id="5001",
                title="TPB",
            )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:collected", "comicvine:505", "issue:collected:1", "wanted"),
            )
            _insert_series(
                con,
                "comicvine:1301",
                0,
                metadata_id="1301",
                title="Absolute Batman: The Court of Owls",
            )
            _insert_issue(
                con,
                "comicvine:1301",
                "comicvine:1301:issue:13001",
                1,
                metadata_id="13001",
                title="HC",
            )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:absolute-court", "comicvine:1301", "comicvine:1301:issue:13001", "wanted"),
            )
            _insert_series(
                con,
                "comicvine:18960",
                None,
                metadata_id="18960",
                title="Batman Year One",
            )
            _insert_issue(
                con,
                "comicvine:18960",
                "comicvine:18960:issue:112492",
                1,
                metadata_id="112492",
                title="TPB",
            )
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:year-one", "comicvine:18960", "comicvine:18960:issue:112492", "wanted"),
            )
            _insert_series(con, "comicvine:18961", None, metadata_id="18961", title="Missing Count Multi TPB")
            _insert_issue(con, "comicvine:18961", "issue:missing-multi:1", 1, metadata_id="189611", title="TPB")
            _insert_issue(con, "comicvine:18961", "issue:missing-multi:2", 2, metadata_id="189612", title="TPB")
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:missing-multi", "comicvine:18961", "issue:missing-multi:1", "wanted"),
            )
            _insert_series(con, "comicvine:18962", None, metadata_id="18962", title="Missing Count No Wanted TPB")
            _insert_issue(con, "comicvine:18962", "issue:missing-no-wanted:1", 1, metadata_id="189621", title="TPB")
            con.execute(
                "update series set year=?, publisher=? where id=?",
                (2015, "DC Comics", "comicvine:1301"),
            )
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,issue_id,state,query,active,raw_json) "
                "values(?,?,?,?,?,?,?,?)",
                (
                    "queue:absolute-court",
                    "wanted:absolute-court",
                    "comicvine:1301",
                    "comicvine:1301:issue:13001",
                    "searching",
                    "Absolute Batman: The Court of Owls 1 2015",
                    1,
                    "{}",
                ),
            )
            con.execute(
                "insert into queue_items(id,wanted_id,series_id,issue_id,state,query,active) values(?,?,?,?,?,?,?)",
                (
                    "queue:collected",
                    "wanted:collected",
                    "comicvine:505",
                    "issue:collected:1",
                    "searching",
                    "Batman: The Court of Owls Saga: DC Essential Edition 1 2018",
                    1,
                ),
            )
            _insert_series(
                con,
                "comicvine:606",
                2,
                metadata_id="606",
                title="Normal Multi-Issue Essential Edition",
            )
            _insert_issue(con, "comicvine:606", "issue:multi-collected:1", 1, metadata_id="6001", title="TPB")
            _insert_issue(con, "comicvine:606", "issue:multi-collected:2", 2, metadata_id="6002", title="TPB")
            con.executemany(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                (
                    ("wanted:multi-collected:1", "comicvine:606", "issue:multi-collected:1", "wanted"),
                    ("wanted:multi-collected:2", "comicvine:606", "issue:multi-collected:2", "wanted"),
                ),
            )

            # Canonical ComicVine provenance must not be inferred from source
            # alone when the series ID and metadata ID disagree.
            _insert_series(
                con,
                "comicvine:707",
                1,
                metadata_id="708",
                title="Mismatched Essential Edition",
            )
            _insert_issue(con, "comicvine:707", "issue:mismatch:1", 1, metadata_id="7001", title="TPB")
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:mismatch", "comicvine:707", "issue:mismatch:1", "wanted"),
            )
            _insert_series(
                con,
                "series:708",
                1,
                metadata_id="708",
                title="Source Shortcut Essential Edition",
            )
            _insert_issue(con, "series:708", "issue:shortcut:1", 1, metadata_id="8001", title="TPB")
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:shortcut", "series:708", "issue:shortcut:1", "wanted"),
            )

            # A wanted row pointing at another series' issue must not prove the
            # target series, even if the foreign issue is a trusted #1 TPB.
            _insert_series(con, "comicvine:808", 1, metadata_id="808", title="Cross Series Essential Edition")
            _insert_issue(con, "comicvine:808", "issue:cross-target:1", 1, metadata_id="8101", title="TPB")
            _insert_series(con, "comicvine:809", 1, metadata_id="809", title="Foreign Essential Edition")
            _insert_issue(con, "comicvine:809", "issue:foreign:1", 1, metadata_id="8201", title="TPB")
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:cross-series", "comicvine:808", "issue:foreign:1", "wanted"),
            )

            # Duplicate active wanted rows for the same issue are still two
            # ownership claims and must fail closed.
            _insert_series(con, "comicvine:909", 1, metadata_id="909", title="Duplicate Essential Edition")
            _insert_issue(con, "comicvine:909", "issue:duplicate:1", 1, metadata_id="9001", title="TPB")
            con.executemany(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                (
                    ("wanted:duplicate:a", "comicvine:909", "issue:duplicate:1", "wanted"),
                    ("wanted:duplicate:b", "comicvine:909", "issue:duplicate:1", "in_progress"),
                ),
            )

            _insert_series(
                con,
                "comicvine:1001",
                1,
                metadata_id="1001",
                title="Stale Essential Edition",
                updated_at=time.time() - coordinator.SINGLETON_METADATA_MAX_AGE_SECONDS - 1,
            )
            _insert_issue(con, "comicvine:1001", "issue:stale-collected:1", 1, metadata_id="10001", title="TPB")
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:stale-collected", "comicvine:1001", "issue:stale-collected:1", "wanted"),
            )
            _insert_series(con, "comicvine:1101", 1, metadata_id="1101", title="Zero Issue ID Essential Edition")
            _insert_issue(con, "comicvine:1101", "issue:zero-id:1", 1, metadata_id="0", title="TPB")
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:zero-id", "comicvine:1101", "issue:zero-id:1", "wanted"),
            )
            _insert_series(con, "comicvine:1201", 1, metadata_id="1201", title="Duplicate Number Essential Edition")
            _insert_issue(con, "comicvine:1201", "issue:duplicate-number:a", 1, metadata_id="12001", title="TPB")
            _insert_issue(con, "comicvine:1201", "issue:duplicate-number:b", 1, metadata_id="12002", title="TPB")
            con.execute(
                "insert into wanted_items(id,series_id,issue_id,status) values(?,?,?,?)",
                ("wanted:duplicate-number", "comicvine:1201", "issue:duplicate-number:a", "wanted"),
            )
            con.commit()

        queue = inkdrop_state.queue_item(db_path, "queue:singleton", read_only=True)
        wanted = coordinator.wanted_item_from_queue(queue, db_path=db_path)
        require(wanted.get("canonical_issue_count") == 1, f"canonical singleton count was wrong: {wanted}")
        require(wanted.get("canonical_issue_one_row_count") == 1, wanted)
        require(wanted.get("canonical_issue_positive_metadata_id_count") == 1, wanted)
        require(wanted.get("metadata_issue_count") == 1, f"declared singleton count was lost: {wanted}")
        require(wanted.get("singleton_issue_proof") is True, f"singleton proof was not propagated: {wanted}")
        require(wanted.get("singleton_issue_metadata_trusted") is True, f"trusted issue provenance was lost: {wanted}")

        collected_queue = inkdrop_state.queue_item(db_path, "queue:collected", read_only=True)
        collected_wanted = coordinator.wanted_item_from_queue(collected_queue, db_path=db_path)
        require(collected_wanted.get("collected_singleton_proof") is True, collected_wanted)
        require(collected_wanted.get("collected_singleton_wanted_count") == 1, collected_wanted)
        require(
            set(collected_wanted.get("collected_singleton_markers") or [])
            == {"essential_edition", "trade_paperback"},
            collected_wanted,
        )
        year_one = coordinator._singleton_issue_context(db_path, "comicvine:18960")
        require(year_one.get("metadata_issue_count") is None, year_one)
        require(year_one.get("singleton_issue_proof") is True, year_one)
        require(
            year_one.get("singleton_issue_proof_source")
            == "comicvine_collected_single_wanted_identity_without_declared_count",
            year_one,
        )
        for series_id in ("comicvine:18961", "comicvine:18962"):
            incomplete = coordinator._singleton_issue_context(db_path, series_id)
            require(incomplete.get("singleton_issue_proof") is False, incomplete)
        multi_collected = coordinator._singleton_issue_context(db_path, "comicvine:606")
        require(multi_collected.get("canonical_issue_count") == 2, multi_collected)
        require(multi_collected.get("collected_singleton_wanted_count") == 2, multi_collected)
        require(multi_collected.get("collected_singleton_proof") is False, multi_collected)
        for series_id, label in (
            ("comicvine:707", "mismatched canonical ComicVine ID"),
            ("series:708", "ComicVine source shortcut"),
            ("comicvine:808", "cross-series wanted issue"),
            ("comicvine:909", "duplicate wanted rows"),
            ("comicvine:1001", "stale collected metadata"),
            ("comicvine:1101", "non-positive issue metadata ID"),
            ("comicvine:1201", "duplicate canonical #1 metadata identities"),
        ):
            adversarial = coordinator._singleton_issue_context(db_path, series_id)
            require(
                adversarial.get("collected_singleton_proof") is False,
                f"{label} gained collected proof: {adversarial}",
            )
        require(
            coordinator._singleton_issue_context(db_path, "comicvine:909").get("collected_singleton_wanted_count")
            == 2,
            "duplicate wanted rows were collapsed",
        )
        duplicate_number = coordinator._singleton_issue_context(db_path, "comicvine:1201")
        require(duplicate_number.get("canonical_issue_one_row_count") == 2, duplicate_number)
        require(duplicate_number.get("canonical_issue_positive_metadata_id_count") == 2, duplicate_number)

        multi = coordinator._singleton_issue_context(db_path, "comicvine:202")
        require(multi.get("canonical_issue_count") == 2, f"real #1/#2 identities collapsed: {multi}")
        require(multi.get("singleton_issue_proof") is False, f"multi-issue series gained singleton proof: {multi}")
        for series_id, label in (
            ("series:untrusted", "untrusted"),
            ("series:manual", "manual"),
            ("comicvine:303", "stale"),
            ("comicvine:404", "manual issue provenance"),
        ):
            context = coordinator._singleton_issue_context(db_path, series_id)
            require(context.get("singleton_issue_proof") is False, f"{label} metadata gained singleton proof: {context}")

        export = inkdrop_state.legacy_autopilot_queue_export(db_path)
        production_row = export.get("items", {}).get("queue:absolute-court")
        require(production_row is not None, export)
        require(production_row.get("series_id") == "comicvine:1301", production_row)
        require(production_row.get("metadata_provider") == "comicvine", production_row)
        require(production_row.get("metadata_id") == "1301", production_row)
        require(production_row.get("issue_metadata_provider") == "comicvine", production_row)
        require(production_row.get("issue_metadata_id") == "13001", production_row)
        require(production_row.get("media_type") == "comic", production_row)
        automatic_queue = inkdrop_state.queue_item(db_path, "queue:absolute-court", read_only=True)
        automatic_target = coordinator.wanted_item_from_queue(automatic_queue, db_path=db_path)
        require(
            automatic_target.get("collected_singleton_title_aliases")
            == ["Batman The Court of Owls", "The Court of Owls"],
            automatic_target,
        )
        automatic_candidate = {
            "provider_id": "prowlarr_dognzb_comics",
            "title": "Batman.v01.-.The.Court.of.Owls.2012.digital.Minutemen-PhD",
            "protocol": "usenet",
            "download_url": "https://provider.invalid/court-v01",
            "match_confidence": "series_title_only",
        }
        automatic_provider_verdict = providers.indexer_candidate_verdict(
            automatic_candidate,
            {
                "provider_id": "prowlarr_dognzb_comics",
                "registry_state": "ready",
                "source_mode": "auto",
                "auto_search_allowed": True,
                "auto_download_allowed": True,
            },
        )
        automatic_applied = matching.apply_compatibility(automatic_provider_verdict, automatic_target)
        require(automatic_applied.get("auto_grab_verdict") == "auto_grab_safe", automatic_applied)
        with inkdrop_state.connect(db_path) as con:
            con.execute(
                "update queue_items set raw_json=? where id=?",
                (
                    json.dumps(
                        {
                            "series_id": "comicvine:9999",
                            "metadata_provider": "comicvine",
                            "metadata_id": "9999",
                            "issue_metadata_provider": "comicvine",
                            "issue_metadata_id": "99999",
                            "media_type": "manga",
                        }
                    ),
                    "queue:absolute-court",
                ),
            )
            con.commit()
        spoofed_export_row = inkdrop_state.legacy_autopilot_queue_export(db_path).get("items", {}).get(
            "queue:absolute-court"
        )
        require(
            {
                key: spoofed_export_row.get(key)
                for key in (
                    "series_id",
                    "metadata_provider",
                    "metadata_id",
                    "issue_metadata_provider",
                    "issue_metadata_id",
                    "media_type",
                )
            }
            == {
                "series_id": "comicvine:1301",
                "metadata_provider": "comicvine",
                "metadata_id": "1301",
                "issue_metadata_provider": "comicvine",
                "issue_metadata_id": "13001",
                "media_type": "comic",
            },
            spoofed_export_row,
        )
        original_state_db = slskd.INKDROP_STATE_DB
        try:
            slskd.INKDROP_STATE_DB = db_path
            projected = slskd.queue_source_review_item(production_row)
            spoofed_title = slskd.queue_source_review_item({
                **production_row,
                "series": "Absolute Batman Beyond: Return of the Joker",
            })
            spoofed_issue = slskd.queue_source_review_item({
                **production_row,
                "issue_metadata_id": "99999",
            })
        finally:
            slskd.INKDROP_STATE_DB = original_state_db
        require(projected.get("collected_singleton_proof") is True, projected)
        require(projected.get("singleton_series_title") == production_row["series"], projected)
        require(
            projected.get("collected_singleton_title_aliases")
            == ["Batman The Court of Owls", "The Court of Owls"],
            projected,
        )
        production_candidate = {
            "filename": r"Comics\Batman v01 - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr",
            "title": r"Comics\Batman v01 - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr",
            "username": "fixture-peer",
            "size": 187_049_466,
            "extension": ".cbr",
            "score": 89,
            "has_free_upload_slot": True,
            "upload_speed": 1_000_000,
            "queue_length": 0,
            "locked": False,
        }
        production_gate = slskd.auto_grab_candidate_verdict(production_candidate, projected)
        require(
            production_gate.get("verdict") == "auto_grab_safe"
            and production_gate.get("autopick_eligible") is True,
            production_gate,
        )
        for adversarial, label in ((spoofed_title, "legacy title spoof"), (spoofed_issue, "issue identity spoof")):
            require(adversarial.get("collected_singleton_proof") is not True, f"{label} borrowed proof: {adversarial}")
            require(not adversarial.get("collected_singleton_title_aliases"), f"{label} gained aliases: {adversarial}")


def main():
    matching_contract()
    adapter_contract()
    coordinator_contract()
    print("inkdrop singleton search coverage smoke: PASS")


if __name__ == "__main__":
    main()
