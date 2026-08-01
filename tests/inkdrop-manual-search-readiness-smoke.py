#!/usr/bin/env python3
"""Fixture certification for the shared Manual Search candidate contract."""

from __future__ import annotations

import json
from pathlib import Path

import inkdrop_manual_search as manual
import inkdrop_source_providers as providers
import inkdrop_source_worker_adapters as adapters


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "docs" / "inkdrop" / "fixtures" / "manual-search-provider-results.json"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
search_input = manual.structured_search_input(payload["search_input"])
require(search_input["canonical_work_title"] == "Alan Moore's Swamp Thing", "canonical title retained")
require(search_input["publication_title"] == "Saga of the Swamp Thing", "edition title retained")
require(search_input["unit_type"] == "issue" and search_input["unit_number"] == "21", "unit metadata retained")

queries = manual.build_query_variants(search_input, provider_id="prowlarr", max_queries=6)
require(1 <= len(queries) <= 6, "provider queries remain bounded")
require(any(row["query"].startswith("Saga of the Swamp Thing") for row in queries), "publication query generated")
require(any("Alan Moore's Swamp Thing" in row["query"] for row in queries), "creator-prefixed canonical query retained")
require(all(row["provider_id"] == "prowlarr" for row in queries), "query provenance retained")
creator_queries = manual.build_query_variants(
    {**payload["search_input"], "canonical_work_title": "Swamp Thing"},
    provider_id="prowlarr",
    max_queries=8,
)
require(any(row["query"] == "Alan Moore Swamp Thing 21" for row in creator_queries), "structured creator query generated")

normalized = {}
for fixture_name, candidate in payload["providers"].items():
    normalized[fixture_name] = manual.normalize_candidate(
        candidate,
        search_input,
        search_run_id="fixture-run",
        query_evidence={"query": queries[0]["query"], "query_kind": queries[0]["query_kind"], "ordinal": 0},
        source_health={"status": "fixture", "configured": True, "enabled": True},
        discovered_at="2026-07-12T00:00:00Z",
    )

required_fields = {
    "candidate_id",
    "search_run_id",
    "provider_id",
    "provider_display_name",
    "child_source_id",
    "child_source_name",
    "indexer_or_extension",
    "protocol",
    "original_title",
    "original_result_title",
    "normalized_title",
    "interpreted_work",
    "interpreted_publication",
    "interpreted_edition",
    "interpreted_unit_type",
    "interpreted_unit_number",
    "interpreted_volume",
    "year",
    "interpreted_year",
    "interpreted_publisher",
    "publisher",
    "creator_evidence",
    "language",
    "file_archive_format",
    "format",
    "size_bytes",
    "age",
    "age_seconds",
    "seeders",
    "peers",
    "remote_availability_state",
    "remote_queue_state",
    "direct_url_available",
    "pack_candidate",
    "pack_type",
    "estimated_pack_members",
    "likely_wanted_coverage",
    "match_score",
    "confidence_tier",
    "accepted",
    "rejection_codes",
    "rejection_explanations",
    "acquisition_capability",
    "assisted_only",
    "source_health_snapshot",
    "health_snapshot",
    "bounded_raw_evidence_reference",
    "raw_evidence_reference",
    "discovery_timestamp",
    "discovered_at",
}
for fixture_name, candidate in normalized.items():
    missing = required_fields - set(candidate)
    require(not missing, f"{fixture_name} missing contract fields: {sorted(missing)}")
    serialized = json.dumps(candidate, sort_keys=True)
    require("must-not-escape" not in serialized, f"{fixture_name} leaked secret fixture")
    require("https://fixture.invalid" not in serialized, f"{fixture_name} leaked reusable URL")

require(normalized["prowlarr_torrent"]["protocol"] == "torrent", "torrent protocol retained")
require(normalized["prowlarr_usenet"]["protocol"] == "usenet", "usenet protocol retained")
require(normalized["prowlarr_usenet"]["provider_result_label"] == "Prowlarr · DOGnzb", "child indexer label retained")
require(normalized["prowlarr_torrent"]["seeders"] == 8, "torrent swarm evidence retained")
require(normalized["prowlarr_usenet"]["age"] == 117, "usenet age retained")
require(normalized["prowlarr_usenet"]["age_seconds"] == 117 * 86400, "Prowlarr age normalized to seconds")
require(normalized["prowlarr_torrent"]["interpreted_unit_number"] == "21", "bare requested issue token interpreted conservatively")
require(normalized["rss_direct"]["direct_url_available"] is True, "direct artifact availability retained without URL")
require(normalized["getcomics_assisted"]["direct_url_available"] is False, "source page is not misreported as direct artifact")

prowlarr_row = {
    "provider_id": "prowlarr",
    "base_url": "https://prowlarr.fixture.invalid/api/v1",
    "indexer_ids": [12],
    "policy": {"categories": [7030], "max_query_variants": 3},
}
prowlarr_request = adapters.prowlarr_search_request(
    prowlarr_row,
    {},
    payload["search_input"],
    query="Saga of the Swamp Thing 21",
)
require(prowlarr_request["params"]["indexerIds"] == "12", "Prowlarr indexerIds filter retained")
require(prowlarr_request["params"]["categories"] == ["7030"], "Prowlarr category mapping retained")

raw_prowlarr = dict(payload["providers"]["prowlarr_usenet"])
raw_prowlarr.update(
    {
        "title": "Saga.of.the.Swamp.Thing.021.1984.Digital.EN.cbz",
        "_inkdrop_query_variant": "Saga of the Swamp Thing 21",
        "_inkdrop_query_group": "unit",
        "_inkdrop_query_index": 1,
    }
)
existing_candidate = providers.prowlarr_candidate_from_result(raw_prowlarr, prowlarr_row, payload["search_input"])
require(existing_candidate["original_result_title"] == raw_prowlarr["title"], "Prowlarr original title retained")
require(existing_candidate["query_variant"] == "Saga of the Swamp Thing 21", "Prowlarr query provenance retained")
require(existing_candidate["indexer"] == "DOGnzb" and existing_candidate["indexer_id"] == "12", "child indexer identity retained")
require(existing_candidate["protocol"] == "usenet", "Prowlarr result protocol retained")
require(existing_candidate["guid"] == "fixture-usenet-guid", "Prowlarr download identity retained")
require(existing_candidate["age"] == 117, "Prowlarr native age retained")
require(existing_candidate["age_seconds"] == 117 * 86400, "Prowlarr age normalized before projection")

slskd = normalized["slskd"]
slskd_json = json.dumps(slskd, sort_keys=True)
require(slskd["protocol"] == "soulseek", "SLSKD protocol retained")
require(slskd["remote_identity"]["present"] is True, "remote identity presence retained")
require(slskd["remote_identity"]["masked_label"] == "remote-user", "remote user masked")
require("private-user" not in slskd_json and "private-path" not in slskd_json, "SLSKD identity/path redacted")
require(slskd["bounded_raw_evidence_reference"]["filename"].endswith(".cbz"), "safe filename evidence retained")

suwayomi = normalized["suwayomi"]
require(suwayomi["child_source_id"] == "3170561626848540385", "Suwayomi source ID retained")
require(suwayomi["child_source_name"] == "MangaKatana (EN)", "Suwayomi source name retained")
require(suwayomi["pack_type"] == "volume_pack", "Suwayomi chapter range classified as a volume pack")
require(normalized["mangadex"]["protocol"] == "page_source", "MangaDex page protocol retained")
require(normalized["rss_direct"]["protocol"] == "direct", "RSS direct protocol retained")
require(normalized["generic_http"]["acquisition_capability"] == "automatic", "safe HTTP can be automatic")
require(normalized["local_manual_inbox"]["protocol"] == "local", "manual inbox local protocol retained")

assisted = normalized["getcomics_assisted"]
require(assisted["accepted"] is False, "unproven assisted collected edition cannot bypass pack verification")
require(assisted["assisted_only"] is True, "GetComics remains assisted")
require(assisted["acquisition_capability"] == "assisted", "assisted capability explicit")
require(assisted["pack_type"] == "omnibus_collected_edition", "collected edition classified")

for case in payload["classification_cases"]:
    actual = manual.classify_pack(case["title"], raw=case.get("raw"))["pack_type"]
    require(actual == case["expected"], f"pack classification {case['title']!r}: {actual}")

timeout = manual.classify_provider_call(completed=False, error="Timeout contacting https://private.invalid/api")
zero = manual.classify_provider_call(completed=True, result_count=0)
failure = manual.classify_provider_call(completed=False, error="connection reset")
require(timeout["status"] == "provider_timeout", "timeout distinguished")
require("private.invalid" not in timeout["error_summary"], "provider error URL redacted")
require(zero["status"] == "zero_results" and zero["provider_call_completed"], "zero results distinguished")
require(failure["status"] == "provider_failure", "provider failure distinguished")

duplicate = dict(normalized["prowlarr_torrent"])
duplicate["query_evidence"] = {"query": "Swamp Thing 21", "query_kind": "alias_unit", "ordinal": 3}
deduped = manual.deduplicate_candidates([normalized["prowlarr_torrent"], duplicate])
require(len(deduped) == 1 and deduped[0]["duplicate_result_count"] == 1, "duplicate candidate collapsed")
require(len(deduped[0]["duplicate_query_evidence"]) == 1, "duplicate query evidence retained")

print("inkdrop manual search readiness smoke: PASS")
