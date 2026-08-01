#!/usr/bin/env python3
"""Focused regressions for broad-first automatic provider discovery."""

from unittest import mock

import inkdrop_slskd_source_probe as slskd
import inkdrop_candidate_matching as matching
import inkdrop_manual_search as manual_search
import inkdrop_manual_search_executor as manual_search_executor
import inkdrop_series_autopilot as autopilot
import inkdrop_source_providers as providers
import inkdrop_source_worker_adapters as adapters
import inkdrop_source_worker_http as source_http
import inkdrop_source_worker_jobs as jobs
import inkdrop_sources


def require(value, message):
    if not value:
        raise AssertionError(message)


CASES = [
    ({"series": "Batman: The Killing Joke", "issue": "1", "year": "1988"}, "Batman The Killing Joke"),
    ({"series": "Descender", "issue": "15", "year": "2016"}, "Descender"),
    ({"series": "Fullmetal Alchemist", "issue": "7", "issue_title": "Volume 7"}, "Fullmetal Alchemist"),
    ({"series": "Gotham Central", "issue": "11", "year": "2004"}, "Gotham Central"),
    ({"series": "Absolute Batman: The Court of Owls", "issue": "1", "year": "2015"}, "Batman The Court of Owls"),
    ({"series": "Swamp Thing by Alan Moore", "issue": "1", "year": "1984"}, "Swamp Thing"),
]

for item, broad in CASES:
    queries = slskd.source_queries(item)
    require(queries[0] == broad, (item, queries[:6]))
    require(str(item["issue"]) not in queries[0], (item, queries[:3]))
    require(any(str(item["issue"]) in query for query in queries[1:4]), (item, queries[:6]))

    wanted = {
        "series_title": item["series"],
        "issue_number": item["issue"],
        "year": item.get("year"),
        "media_type": "comic",
        "volume_number": item["issue"] if "Volume" in item.get("issue_title", "") else "",
        "unit_type": "volume" if "Volume" in item.get("issue_title", "") else "issue",
    }
    indexer_queries = adapters.indexer_source_queries(wanted, max_queries=3)
    if item["series"] == "Absolute Batman: The Court of Owls":
        require(
            indexer_queries == ["Court of Owls 1", "Court of Owls", "The Court of Owls 1"],
            (wanted, indexer_queries),
        )
    elif wanted["unit_type"] == "volume":
        require(
            indexer_queries
            == ["Fullmetal Alchemist Vol 7", "Fullmetal Alchemist", "Fullmetal Alchemist Volume 7"],
            (wanted, indexer_queries),
        )
    else:
        require(indexer_queries[0].replace(":", "") == broad, (wanted, indexer_queries))
        require(any(str(item["issue"]) in query for query in indexer_queries[1:]), (wanted, indexer_queries))

    # Automatic Prowlarr discovery must retain the same broad series fallback
    # available to Manual Search for ordinary comic issues, not only special
    # singleton/pack cases.
    prowlarr_requests = adapters.prowlarr_search_requests(
        {
            "provider_id": "prowlarr",
            "adapter_family": "prowlarr_indexer",
            "base_url": "http://prowlarr.example:9696",
            "policy": {"prowlarr_max_query_variants": 3},
        },
        {},
        wanted,
    )
    request_queries = [request.get("params", {}).get("query") for request in prowlarr_requests]
    if item["series"] == "Absolute Batman: The Court of Owls":
        require(request_queries[:3] == indexer_queries, (wanted, request_queries))
    elif wanted["unit_type"] == "volume":
        require(request_queries[:3] == indexer_queries, (wanted, request_queries))
    else:
        require(any((query or "").replace(":", "") == broad for query in request_queries), (wanted, request_queries))

    manual_queries = [
        row["query"]
        for row in manual_search.build_query_variants(
            {
                "canonical_work_title": item["series"],
                "publication_title": item["series"],
                "unit_number": item["issue"],
                "unit_type": wanted["unit_type"],
                "publication_year": item.get("year"),
                "media_type": "comic",
            },
            provider_id="prowlarr",
            max_queries=6,
        )
    ]
    if item["series"] == "Swamp Thing by Alan Moore":
        require("Swamp Thing" in manual_queries, manual_queries)
    if wanted["unit_type"] == "volume":
        manual_context = manual_search.structured_search_input({
            "canonical_work_title": item["series"],
            "unit_number": item["issue"],
            "volume_number": item["issue"],
            "unit_type": "volume",
            "media_type": "manga",
        })
        manual_contract = manual_search.build_query_variants(
            manual_context,
            provider_id="prowlarr",
            max_queries=6,
        )
        manual_wanted = manual_search_executor._wanted_context(manual_context, manual_contract)
        require(
            adapters.indexer_source_queries(manual_wanted, max_queries=3) == indexer_queries,
            (manual_wanted, indexer_queries),
        )

# Punctuation folding stays identity-preserving and consumes a bounded slot
# alongside the literal shortest alias in both automatic and manual plans.
punctuation_wanted = {
    "series_title": "Deluxe Something: Spider-Man Life Story",
    "issue_number": "1",
    "media_type": "comic",
    "unit_type": "issue",
}
punctuation_queries = adapters.indexer_source_queries(punctuation_wanted, max_queries=3)
require(
    punctuation_queries
    == ["Spider Man Life Story 1", "Spider Man Life Story", "Spider-Man Life Story 1"],
    punctuation_queries,
)
punctuation_context = manual_search.structured_search_input(punctuation_wanted)
punctuation_contract = manual_search.build_query_variants(
    punctuation_context,
    provider_id="prowlarr",
    max_queries=6,
)
punctuation_manual_wanted = manual_search_executor._wanted_context(
    punctuation_context,
    punctuation_contract,
)
require(
    adapters.indexer_source_queries(punctuation_manual_wanted, max_queries=3)
    == punctuation_queries,
    punctuation_manual_wanted,
)
metadata_alias_wanted = {
    "series_title": "Absolute Batman The Court of Owls",
    "issue_number": "1",
    "media_type": "comic",
    "unit_type": "issue",
    "collected_singleton_proof": True,
    "collected_singleton_proof_source": "comicvine_collected_single_wanted_identity",
    "collected_singleton_wanted_count": 1,
    "collected_singleton_title_aliases": ["Batman The Court of Owls", "The Court of Owls"],
}
require(
    adapters.indexer_source_queries(metadata_alias_wanted, max_queries=3)
    == ["Court of Owls 1", "Court of Owls", "The Court of Owls 1"],
    metadata_alias_wanted,
)
malformed_alias_proof = dict(metadata_alias_wanted, collected_singleton_wanted_count="not-a-count")
require(
    adapters.indexer_source_queries(malformed_alias_proof, max_queries=3)[0]
    == "Absolute Batman The Court of Owls",
    malformed_alias_proof,
)

for volume_wanted in (
    {
        "series_title": "Asterix",
        "issue_number": "4",
        "volume_number": "4",
        "media_type": "comic",
        "unit_type": "volume",
    },
    {
        "series_title": "The Legend of Korra: Turf Wars Library Edition",
        "issue_number": "1",
        "volume_number": "1",
        "media_type": "comic",
        "unit_type": "volume",
    },
):
    automatic_volume_queries = adapters.indexer_source_queries(volume_wanted, max_queries=3)
    volume_context = manual_search.structured_search_input(volume_wanted)
    volume_contract = manual_search.build_query_variants(
        volume_context,
        provider_id="prowlarr",
        max_queries=6,
    )
    manual_volume_wanted = manual_search_executor._wanted_context(volume_context, volume_contract)
    require(
        automatic_volume_queries[0]
        == f"{volume_wanted['series_title']} Vol {volume_wanted['volume_number']}"
        and adapters.indexer_source_queries(manual_volume_wanted, max_queries=3)
        == automatic_volume_queries,
        (volume_wanted, automatic_volume_queries, manual_volume_wanted),
    )


# Automatic SLSKD receives localized and format-only collected-edition
# metadata from real wanted rows. Keep the provider's broad directory query,
# then spend the bounded exact-query slots on the metadata spelling and common
# volume forms instead of treating these units as ordinary comic issues.
localized_volume_cases = (
    ({"series": "Fullmetal Alchemist", "issue": "17", "issue_title": "Band 17", "media_type": "manga"}, "Band 17"),
    ({"series": "Asterix", "issue": "4", "issue_title": "Tome 4", "media_type": "comic"}, "Tome 4"),
    ({"series": "Absolute Swamp Thing", "issue": "1", "issue_title": "Volume One", "media_type": "comic"}, "Volume One"),
    ({"series": "The Legend of Korra: Turf Wars Library Edition", "issue": "1", "issue_title": "HC", "media_type": "comic"}, "HC"),
)
for item, exact_suffix in localized_volume_cases:
    queries = slskd.source_queries(item)
    require(queries[0] and str(item["issue"]) not in queries[0], (item, queries[:6]))
    require(f"{queries[0]} {exact_suffix}" in queries[:4], (item, queries[:6]))
    require(any("Volume " in query for query in queries[1:5]), (item, queries[:6]))

# Similar prose must not be promoted merely because it contains a marker word.
for ordinary_title in ("Band of Brothers", "The Book of Doom", "Tome of Magic"):
    ordinary = {"series": "Example Series", "issue": "7", "issue_title": ordinary_title, "media_type": "comic"}
    ordinary_queries = slskd.source_queries(ordinary)
    require(not any(ordinary_title in query for query in ordinary_queries[:6]), (ordinary_title, ordinary_queries[:8]))

require(slskd.book_volume_numbers("Fullmetal Alchemist Band 17.cbz") == [17], "localized Band marker was not parsed")
require(slskd.book_volume_numbers("Asterix Tome Four.cbz") == [4], "localized Tome marker was not parsed")
wrong_localized_volume = slskd.book_volume_number_match(
    "Fullmetal Alchemist Band 18.cbz",
    {"series": "Fullmetal Alchemist", "issue": "17", "issue_title": "Band 17", "media_type": "manga"},
)
require(not wrong_localized_volume["matched"] and "does not match 17" in wrong_localized_volume["penalty"], wrong_localized_volume)


# A contributor-free provider result must survive discovery normalization and
# relevance scoring while still passing through the normal strict safety gates.
swamp_wanted = {
    "series_title": "Swamp Thing by Alan Moore",
    "issue_number": "1",
    "year": "1984",
    "media_type": "comic",
    "unit_type": "issue",
}
swamp_results = [{
    "title": "Swamp Thing 001 (1984) (Digital) (Zone-Empire).cbr",
    "downloadUrl": "http://prowlarr.example/download/swamp-thing-001",
    "protocol": "torrent",
    "seeders": 12,
    "size": 55_000_000,
    "indexer": "Comics Indexer",
}]
swamp_candidates = providers.prowlarr_candidates_from_results(
    swamp_results,
    registry_row={"provider_id": "prowlarr", "policy": {}},
    wanted_item=swamp_wanted,
)
require(len(swamp_candidates) == 1, swamp_candidates)
require("Swamp Thing" in swamp_candidates[0].get("series_query_aliases", []), swamp_candidates[0])

court_wanted = {
    "series_title": "Absolute Batman: The Court of Owls",
    "issue_number": "1",
    "year": "2015",
    "media_type": "comic",
    "unit_type": "issue",
}
court_candidates = providers.prowlarr_candidates_from_results(
    [{
        "title": "Batman The Court of Owls 001 (2015) (Digital).cbr",
        "downloadUrl": "http://prowlarr.example/download/court-of-owls-001",
        "protocol": "torrent",
        "seeders": 10,
        "size": 60_000_000,
        "indexer": "Comics Indexer",
    }],
    registry_row={"provider_id": "prowlarr", "policy": {}},
    wanted_item=court_wanted,
)
require(len(court_candidates) == 1, court_candidates)
require("Batman The Court of Owls" in court_candidates[0].get("series_query_aliases", []), court_candidates[0])

auto_prowlarr_row = {
    "provider_id": "prowlarr",
    "registry_state": "ready",
    "source_mode": "auto",
    "auto_search_allowed": True,
    "auto_download_allowed": True,
    "policy": {},
}

# An authoritative collected singleton may use a derived narrow alias when
# the provider artifact is unnumbered. The proof must not widen ordinary
# series, related works, conflicting editions, unit numbers, or years.
collected_wanted = {
    "series_title": "Deluxe Descender: The Machine Moon",
    "issue_number": "1",
    "year": "2015",
    "publisher": "Image Comics",
    "media_type": "comic",
    "canonical_issue_count": 1,
    "singleton_metadata_trusted": True,
    "singleton_metadata_fresh": True,
    "singleton_issue_metadata_trusted": True,
    "collected_singleton_proof": True,
    "collected_singleton_proof_source": "comicvine_collected_single_wanted_identity",
    "collected_singleton_wanted_count": 1,
    "collected_singleton_markers": ["hardcover"],
    "collected_singleton_title_aliases": ["Descender The Machine Moon", "The Machine Moon"],
}
collected_row = {
    **auto_prowlarr_row,
    "provider_id": "prowlarr_torrentleech_comics",
    "adapter_family": "prowlarr_indexer",
    "base_url": "http://prowlarr.example:9696",
    "policy": {
        "categories": [7030],
        "indexer_ids": [47],
        "max_query_variants": 3,
        "minimum_seeders": 1,
    },
}


def collected_verdict(title, wanted=None):
    wanted = wanted or collected_wanted
    candidate = providers.prowlarr_candidate_from_result(
        {
            "title": title,
            "downloadUrl": "http://prowlarr.example/download/collected-singleton",
            "protocol": "torrent",
            "seeders": 10,
            "categories": [7030],
            "indexerId": 47,
            "_inkdrop_query_variant": "Descender The Machine Moon",
        },
        collected_row,
        wanted,
    )
    return matching.apply_compatibility(
        providers.indexer_candidate_verdict(candidate, collected_row),
        wanted,
    )


collected_positive = collected_verdict("Descender - The Machine Moon")
require(collected_positive["candidate_safe"], collected_positive)
require(
    "collected_singleton_alias_exact_title"
    in collected_positive["target_compatibility"]["positive_evidence"],
    collected_positive,
)
for unsafe_title in (
    "Ascender - The Machine Moon",
    "Descender - The Machine Moon Adventures",
    "Descender - The Machine Moon Omnibus",
    "Descender - The Machine Moon v2",
    "Descender - The Machine Moon (2024)",
):
    unsafe = collected_verdict(unsafe_title)
    require(not unsafe["candidate_safe"], (unsafe_title, unsafe))
unproven_wanted = dict(collected_wanted, collected_singleton_proof=False)
require(
    not collected_verdict("Descender - The Machine Moon", unproven_wanted)["candidate_safe"],
    "unnumbered collected title escaped the authoritative singleton proof gate",
)

# Provider truth remains distinct from candidate truth. Total timeout and
# malformed responses are provider-wait; a valid result followed by a timeout
# remains available for strict compatibility and handoff evaluation.
collected_plan = {
    "provider_id": collected_row["provider_id"],
    "adapter_family": "prowlarr_indexer",
    "adapter_id": "prowlarr_indexer_policy",
    "candidate_parser": "prowlarr_candidates_from_results",
    "verdict_helper": "indexer_candidate_verdict",
    "attempt_seed_helper": "indexer_candidate_attempt_seed",
    "schedule_state": "auto_download",
    "can_search": True,
    "can_auto_download": True,
    "emits_download_task": True,
}
collected_job = jobs.source_job_for_row(collected_row, collected_plan, collected_wanted, limit=20)


def timed_out(_request):
    raise source_http.SourceHttpError("provider_timeout")


timeout_result = jobs.run_source_job(collected_job, http_get=timed_out)
require(timeout_result["result_status"] == "provider_wait", timeout_result)
malformed_result = jobs.run_source_job(
    collected_job,
    http_get=lambda _request: {"text": "<html>not Prowlarr JSON</html>"},
)
require(malformed_result["result_status"] == "provider_wait", malformed_result)
require("malformed_provider_response" in str(malformed_result["fetch"].get("partial_errors")), malformed_result)

partial_calls = 0


def partial_response(_request):
    global partial_calls
    partial_calls += 1
    if partial_calls == 1:
        return {"json": [{
            "title": "Descender - The Machine Moon",
            "downloadUrl": "http://prowlarr.example/download/partial-collected",
            "protocol": "torrent",
            "seeders": 10,
            "categories": [7030],
            "indexerId": 47,
        }]}
    raise source_http.SourceHttpError("provider_timeout")


partial_result = jobs.run_source_job(collected_job, http_get=partial_response)
require(partial_result["safe_candidate_count"] == 1, partial_result)
require(partial_result["result_status"] == "sent", partial_result)
require(partial_result["fetch"]["payloads"][0].get("partial_errors"), partial_result)
require(
    any(((attempt.get("raw") or {}).get("fetch") or {}).get("partial_errors") for attempt in partial_result["attempts"]),
    partial_result,
)

# A safe exact result discovered by the short canonical query must emit the
# same idempotent client handoff identity on repeat for torrent and Usenet.
for protocol, extension, expected_client, provider_id, indexer_id in (
    ("torrent", ".torrent", "qbittorrent", "prowlarr_torrentleech_comics", 47),
    ("usenet", ".nzb", "sabnzbd", "prowlarr_dognzb_comics", 15),
):
    handoff_row = {
        **auto_prowlarr_row,
        "provider_id": provider_id,
        "adapter_family": "prowlarr_indexer",
        "base_url": "http://prowlarr.example:9696",
        "policy": {
            "categories": [7030],
            "indexer_ids": [indexer_id],
            "max_query_variants": 3,
            "minimum_seeders": 1,
        },
    }
    handoff_plan = {
        **collected_plan,
        "provider_id": provider_id,
    }
    handoff_job = jobs.source_job_for_row(handoff_row, handoff_plan, court_wanted, limit=20)

    def exact_handoff_response(_request, *, _protocol=protocol, _extension=extension, _indexer_id=indexer_id):
        return {"json": [{
            "title": "Absolute Batman The Court of Owls 001 (2015) (Digital).cbr",
            "downloadUrl": f"http://prowlarr.example/download/court-of-owls{_extension}",
            "protocol": _protocol,
            "seeders": 10,
            "size": 60_000_000,
            "categories": [7030],
            "indexerId": _indexer_id,
        }]}

    first_handoff = jobs.run_source_job(handoff_job, http_get=exact_handoff_response)
    repeated_handoff = jobs.run_source_job(handoff_job, http_get=exact_handoff_response)
    require(
        first_handoff["result_status"] == "sent"
        and repeated_handoff["result_status"] == "sent"
        and first_handoff["safe_candidate_count"] == 1
        and repeated_handoff["safe_candidate_count"] == 1,
        (protocol, first_handoff, repeated_handoff),
    )
    first_attempt = first_handoff["attempts"][0]
    repeated_attempt = repeated_handoff["attempts"][0]
    require(
        first_attempt["download_client"] == expected_client
        and repeated_attempt["download_client"] == expected_client
        and first_attempt["candidate_identity"] == repeated_attempt["candidate_identity"]
        and first_attempt["external_id"] == repeated_attempt["external_id"]
        and first_attempt["query_variant"] == "Court of Owls 1",
        (protocol, first_attempt, repeated_attempt),
    )

for wrong_title, expected_confidence in (
    ("Batman Beyond 001 (2015) (Digital).cbr", "mismatch"),
    ("Batman The Court of Owls Adventures 001 (2015) (Digital).cbr", "related_series_identity"),
    ("Adventures of Batman The Court of Owls 001 (2015).cbr", "related_series_identity"),
    ("Son of Batman The Court of Owls 001 (2015).cbr", "related_series_identity"),
    ("Batman The Court of Owls 001 Adventures (2015).cbr", "related_series_identity"),
    ("Batman The Court of Owls 001 (2015) Adventures.cbr", "related_series_identity"),
):
    wrong = providers.prowlarr_candidate_from_result(
        {
            "title": wrong_title,
            "downloadUrl": "http://prowlarr.example/download/wrong-court-result",
            "protocol": "torrent",
            "seeders": 10,
            "_inkdrop_query_variant": "Batman The Court of Owls",
        },
        auto_prowlarr_row,
        court_wanted,
    )
    require(wrong["match_confidence"] == expected_confidence, wrong)
    wrong_verdict = matching.apply_compatibility(
        providers.indexer_candidate_verdict(wrong, auto_prowlarr_row),
        court_wanted,
    )
    require(not wrong_verdict["candidate_safe"], wrong_verdict)
    require(
        any(
            reason in wrong_verdict["block_reasons"]
            for reason in ("candidate_title_mismatch", "related_series_identity")
        ),
        wrong_verdict,
    )

for unsafe_title in (
    "Detective Comics The Court of Owls 001 (2015).cbr",
    "Absolute Batman The Court of Owls 002 (2015).cbr",
    "Absolute Batman The Court of Owls Omnibus v01 (2015).cbr",
    "Absolute Batman The Court of Owls 001-012 Pack (2015).cbr",
    "Absolute Batman The Court of Owls Preview 001 (2015).cbr",
):
    unsafe_candidate = providers.prowlarr_candidate_from_result(
        {
            "title": unsafe_title,
            "downloadUrl": "http://prowlarr.example/download/unsafe-court-result",
            "protocol": "torrent",
            "seeders": 10,
            "_inkdrop_query_variant": "Court of Owls 1",
        },
        auto_prowlarr_row,
        court_wanted,
    )
    unsafe_verdict = matching.apply_compatibility(
        providers.indexer_candidate_verdict(unsafe_candidate, auto_prowlarr_row),
        court_wanted,
    )
    require(not unsafe_verdict["candidate_safe"], (unsafe_title, unsafe_verdict))

swamp_conflict = providers.prowlarr_candidate_from_result(
    {
        "title": "Swamp Thing by Scott Snyder 001 (1984) (Digital).cbr",
        "downloadUrl": "http://prowlarr.example/download/swamp-scott-snyder",
        "protocol": "torrent",
        "seeders": 10,
        "_inkdrop_query_variant": "Swamp Thing",
    },
    auto_prowlarr_row,
    swamp_wanted,
)
swamp_conflict_verdict = matching.apply_compatibility(
    providers.indexer_candidate_verdict(swamp_conflict, auto_prowlarr_row),
    swamp_wanted,
)
require("creator_identity_conflict" in swamp_conflict_verdict["block_reasons"], swamp_conflict_verdict)
require(not swamp_conflict_verdict["candidate_safe"], swamp_conflict_verdict)

swamp_base_verdict = matching.apply_compatibility(
    providers.indexer_candidate_verdict(swamp_candidates[0], auto_prowlarr_row),
    swamp_wanted,
)
require("creator_identity_conflict" not in swamp_base_verdict["block_reasons"], swamp_base_verdict)

swamp_trailing_identity = providers.prowlarr_candidate_from_result(
    {
        "title": "Swamp Thing 001 Adventures (1984).cbr",
        "downloadUrl": "http://prowlarr.example/download/swamp-adventures",
        "protocol": "torrent",
        "seeders": 10,
        "_inkdrop_query_variant": "Swamp Thing",
    },
    auto_prowlarr_row,
    swamp_wanted,
)
require(swamp_trailing_identity["match_confidence"] == "related_series_identity", swamp_trailing_identity)

swamp_leading_identity = providers.prowlarr_candidate_from_result(
    {
        "title": "Adventures of Swamp Thing 001 (1984).cbr",
        "downloadUrl": "http://prowlarr.example/download/adventures-of-swamp",
        "protocol": "torrent",
        "seeders": 10,
        "_inkdrop_query_variant": "Swamp Thing",
    },
    auto_prowlarr_row,
    swamp_wanted,
)
require(swamp_leading_identity["match_confidence"] == "related_series_identity", swamp_leading_identity)

swamp_son_identity = providers.prowlarr_candidate_from_result(
    {
        "title": "Son of Swamp Thing 001 (1984).cbr",
        "downloadUrl": "http://prowlarr.example/download/son-of-swamp",
        "protocol": "torrent",
        "seeders": 10,
        "_inkdrop_query_variant": "Swamp Thing",
    },
    auto_prowlarr_row,
    swamp_wanted,
)
require(swamp_son_identity["match_confidence"] == "related_series_identity", swamp_son_identity)

for leading_metadata in ("Digital", "Zone Empire", "F Son of Ultron Empire"):
    leading_release = providers.prowlarr_candidate_from_result(
        {
            "title": f"{leading_metadata} Swamp Thing 001 (1984).cbr",
            "downloadUrl": "http://prowlarr.example/download/leading-release-metadata",
            "protocol": "torrent",
            "seeders": 10,
            "_inkdrop_query_variant": "Swamp Thing",
        },
        auto_prowlarr_row,
        swamp_wanted,
    )
    require(leading_release["match_confidence"] != "related_series_identity", (leading_metadata, leading_release))

fullmetal_wanted = {
    "series_title": "Fullmetal Alchemist",
    "issue_number": "7",
    "volume_number": "7",
    "unit_type": "volume",
    "media_type": "manga",
}
for marker in ("v07", "Vol 7", "Volume 7", "Book 7", "Part 7", "Chapter 7", "ch07"):
    valid_unit = providers.prowlarr_candidate_from_result(
        {
            "title": f"Fullmetal Alchemist {marker} (Digital).cbz",
            "downloadUrl": f"http://prowlarr.example/download/fullmetal-{marker.replace(' ', '-').lower()}",
            "protocol": "torrent",
            "seeders": 10,
            "_inkdrop_query_variant": "Fullmetal Alchemist",
        },
        auto_prowlarr_row,
        fullmetal_wanted,
    )
    require(valid_unit["match_confidence"] != "related_series_identity", (marker, valid_unit))

hunter_wanted = {
    "series_title": "Hunter X Hunter",
    "issue_number": "38",
    "volume_number": "38",
    "unit_type": "volume",
    "media_type": "manga",
}
hunter_release = providers.prowlarr_candidate_from_result(
    {
        "title": "Hunter x Hunter v38 (2026) (Digital) (JKO)",
        "downloadUrl": "http://prowlarr.example/download/hunter-x-hunter-v38",
        "protocol": "torrent",
        "seeders": 10,
        "size": 80_000_000,
        "_inkdrop_query_variant": "Hunter X Hunter Vol 38",
    },
    auto_prowlarr_row,
    hunter_wanted,
)
hunter_verdict = matching.apply_compatibility(
    providers.indexer_candidate_verdict(hunter_release, auto_prowlarr_row),
    hunter_wanted,
)
require(hunter_release["match_confidence"] != "related_series_identity", hunter_release)
require(hunter_verdict["candidate_safe"], hunter_verdict)

for unsafe_suffix in ("Adventures", "ADVENTURES", "ADVENT", "GON", "SON", "SPINOFF", "Son of Gon"):
    related_hunter = providers.prowlarr_candidate_from_result(
        {
            "title": f"Hunter x Hunter v38 ({unsafe_suffix}) (2026) (Digital)",
            "downloadUrl": "http://prowlarr.example/download/hunter-related",
            "protocol": "torrent",
            "seeders": 10,
            "_inkdrop_query_variant": "Hunter X Hunter Vol 38",
        },
        auto_prowlarr_row,
        hunter_wanted,
    )
    require(related_hunter["match_confidence"] == "related_series_identity", related_hunter)

for marker in ("v07", "Volume 7", "Book 7", "Part 7", "Chapter 7"):
    invalid_unit = providers.prowlarr_candidate_from_result(
        {
            "title": f"Fullmetal Alchemist {marker} Adventures (Digital).cbz",
            "downloadUrl": "http://prowlarr.example/download/fullmetal-related",
            "protocol": "torrent",
            "seeders": 10,
            "_inkdrop_query_variant": "Fullmetal Alchemist",
        },
        auto_prowlarr_row,
        fullmetal_wanted,
    )
    require(invalid_unit["match_confidence"] == "related_series_identity", (marker, invalid_unit))


def fullmetal_volume_verdict(title):
    candidate = providers.prowlarr_candidate_from_result(
        {
            "title": title,
            "downloadUrl": "http://prowlarr.example/download/fullmetal-volume",
            "protocol": "torrent",
            "seeders": 10,
            "size": 80_000_000,
            "_inkdrop_query_variant": "Fullmetal Alchemist Vol 7",
        },
        auto_prowlarr_row,
        fullmetal_wanted,
    )
    verdict = matching.apply_compatibility(
        providers.indexer_candidate_verdict(candidate, auto_prowlarr_row),
        fullmetal_wanted,
    )
    return verdict


exact_volume_verdict = fullmetal_volume_verdict("Fullmetal Alchemist Vol 7 (Digital).cbz")
require(exact_volume_verdict["candidate_safe"], exact_volume_verdict)
first_volume_attempt = providers.indexer_candidate_attempt_seed(exact_volume_verdict, auto_prowlarr_row)
repeated_volume_attempt = providers.indexer_candidate_attempt_seed(exact_volume_verdict, auto_prowlarr_row)
require(
    first_volume_attempt["status"] == "sent"
    and first_volume_attempt["download_client"] == "qbittorrent"
    and first_volume_attempt["candidate_identity"] == repeated_volume_attempt["candidate_identity"]
    and first_volume_attempt["external_id"] == repeated_volume_attempt["external_id"],
    (first_volume_attempt, repeated_volume_attempt),
)
for unsafe_volume_title in (
    "Fullmetal Alchemist Brotherhood Vol 7 (Digital).cbz",
    "Fullmetal Alchemist Vol 8 (Digital).cbz",
    "Fullmetal Alchemist Omnibus Vol 7 (Digital).cbz",
    "Fullmetal Alchemist Vol 1-7 Pack (Digital).cbz",
    "Fullmetal Alchemist Vol 7 Preview (Digital).cbz",
):
    unsafe_volume_verdict = fullmetal_volume_verdict(unsafe_volume_title)
    require(
        not unsafe_volume_verdict["candidate_safe"],
        (unsafe_volume_title, unsafe_volume_verdict),
    )

# Do not turn ordinary title prose into broad aliases.
require(inkdrop_sources.contributor_title_aliases("Day by Day") == [], "ordinary by-title widened")


descender = {"series": "Descender", "issue": "1", "year": "2015", "review_id": "descender-1"}
siblings = [
    {"series": "Descender", "issue": str(number), "year": "2015", "review_id": f"descender-{number}"}
    for number in range(1, 4)
]
responses = [{
    "username": "private-peer",
    "hasFreeUploadSlot": True,
    "files": [
        {"filename": f"Comics/Descender/Descender {number:03d} (2015).cbr", "size": 50_000_000}
        for number in range(1, 4)
    ],
}]
observations = []
calls = []


def search(query, **kwargs):
    calls.append((query, kwargs.get("wait_seconds")))
    return responses


with mock.patch.object(slskd, "slskd_search", side_effect=search):
    entry = slskd.probe_item(
        descender,
        wait_seconds=8,
        max_queries=1,
        directory_observation_sink=observations,
        directory_items=siblings,
    )

require(calls == [("Descender", 50)], calls)
require(entry["queries"][0]["series_directory_observation_count"] == 1, entry["queries"])
require(observations[0]["active_wanted_coverage"] == 3, observations)
require(entry["candidate_count"] >= 1, entry)

# Cache maintenance is bounded to the active search window. An unrelated old
# candidate must not be re-scored during every provider attempt.
active = {"review_id": "active-1", "series": "Descender", "issue": "1"}
cache = {
    "active-1": {"review_id": "active-1", "candidates": [{"filename": "Descender 001.cbr"}]},
    "inactive-1": {"review_id": "inactive-1", "candidates": [{"filename": "Old 001.cbr"}]},
}
with mock.patch.object(slskd, "refresh_cached_candidate_verdicts", return_value=({}, False)) as refresh:
    slskd.refresh_cache_candidate_verdicts(cache, [active])
require(refresh.call_count == 1, refresh.call_args_list)
require(refresh.call_args.args[0]["review_id"] == "active-1", refresh.call_args_list)

# Inline provider writes must either hold the scheduled worker lock or defer
# cleanly. They must never race SQLite and collapse into a zero-attempt failure.
with mock.patch.object(autopilot, "held_source_worker_lock") as held:
    held.return_value.__enter__.return_value = False
    held.return_value.__exit__.return_value = False
    with mock.patch.object(autopilot.inkdrop_source_worker_cli, "run_source_worker_cli") as worker:
        busy = autopilot.run_source_worker_cli_locked(
            ["state.sqlite3"], source="prowlarr", series="Gotham Central", missing_candidates=8
        )
require(busy["reason"] == "source_worker_lock_busy" and busy["skipped_busy"], busy)
require(worker.call_count == 0, worker.call_args_list)
busy_summary = autopilot.prowlarr_source_worker_payload_to_result(busy, "Gotham Central")
require(busy_summary["reason"] == "source_worker_lock_busy" and busy_summary["skipped_busy"], busy_summary)

with mock.patch.object(autopilot, "held_source_worker_lock") as held:
    held.return_value.__enter__.return_value = True
    held.return_value.__exit__.return_value = False
    with mock.patch.object(
        autopilot.inkdrop_source_worker_cli, "run_source_worker_cli", return_value={"ok": True}
    ) as worker:
        executed = autopilot.run_source_worker_cli_locked(
            ["state.sqlite3"], source="prowlarr", series="Gotham Central", missing_candidates=8
        )
require(executed["ok"] and worker.call_count == 1, (executed, worker.call_args_list))

print("inkdrop automatic search recall smoke: PASS")
