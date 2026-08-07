#!/usr/bin/env python3
"""Known-title query-planning and safety corpus for Manual Search."""

from __future__ import annotations

import json

from core import inkdrop_auth_contracts
from core import inkdrop_manual_search as manual


CASES = (
    ({"canonical_work_title": "Saga of the Swamp Thing", "aliases": ["Swamp Thing"], "creators": ["Alan Moore"], "publication_year": 1984, "media_type": "comic", "unit_type": "issue", "unit_number": "21"}, ("Saga of the Swamp Thing 21", "Swamp Thing 21")),
    ({"canonical_work_title": "The Sandman", "creators": ["Neil Gaiman"], "publication_year": 1989, "media_type": "comic", "unit_type": "issue", "unit_number": "1"}, ("The Sandman 1",)),
    ({"canonical_work_title": "Miracleman", "creators": ["Alan Moore"], "media_type": "comic", "unit_type": "issue", "unit_number": "1"}, ("Miracleman 1",)),
    ({"canonical_work_title": "All-Star Superman", "creators": ["Grant Morrison"], "media_type": "comic", "unit_type": "issue", "unit_number": "1"}, ("All-Star Superman 1",)),
    ({"canonical_work_title": "Fullmetal Alchemist", "aliases": ["Hagane no Renkinjutsushi"], "media_type": "manga", "unit_type": "volume", "unit_number": "1", "volume_number": "1"}, ("Fullmetal Alchemist Vol 1", "Hagane no Renkinjutsushi Vol 1")),
    ({"canonical_work_title": "Berserk", "media_type": "manga", "unit_type": "volume", "unit_number": "1", "volume_number": "1"}, ("Berserk Vol 1",)),
    ({"canonical_work_title": "One Piece", "media_type": "manga", "unit_type": "chapter", "unit_number": "1"}, ("One Piece Chapter 1",)),
    ({"canonical_work_title": "Fire Punch", "media_type": "manga", "unit_type": "volume", "unit_number": "1", "volume_number": "1"}, ("Fire Punch Vol 1",)),
)


def require(value, message):
    if not value:
        raise AssertionError(message)


def run():
    evidence = []
    for search_input, expected in CASES:
        queries = manual.build_query_variants(search_input, provider_id="fixture", max_queries=8)
        texts = [row["query"] for row in queries]
        require(1 <= len(texts) <= 8 and len(texts) == len(set(text.casefold() for text in texts)), f"queries must be bounded/deduplicated: {texts}")
        for value in expected:
            require(value in texts, f"missing known-title query {value!r}: {texts}")
        evidence.append({"title": search_input["canonical_work_title"], "queries": texts})

    court = {
        "canonical_work_title": "Absolute Batman: The Court of Owls",
        "publication_title": "Absolute Batman: The Court of Owls",
        "aliases": ["Batman: The Court of Owls", "Court of Owls"],
        "publication_year": 2015,
        "media_type": "comic",
        "unit_type": "issue",
        "unit_number": "1",
    }
    prowlarr_queries = manual.build_query_variants(court, provider_id="prowlarr", max_queries=6)
    require(len(prowlarr_queries) == 6 and prowlarr_queries[1]["query"] == "Court of Owls", prowlarr_queries)
    require(prowlarr_queries[1]["query_kind"] == "discovery_series_fallback", prowlarr_queries)

    slskd_queries = manual.build_query_variants(court, provider_id="slskd", max_queries=6)
    require(len(slskd_queries) == 6 and slskd_queries[0]["query"] == "Court of Owls", slskd_queries)
    require(slskd_queries[0]["query_kind"] == "discovery_series_fallback", slskd_queries)

    for path in ("/api/manual-search/runs", "/api/manual-search/runs/example/cancel", "/api/manual-search/candidates/example/grab"):
        policy = inkdrop_auth_contracts.mutation_route_policy(path, "POST")
        require(policy and policy["scope"] == "acquisition", f"{path} must require acquisition scope")
        require(policy["csrf_required_for_cookie_sessions"], f"{path} must require cookie-session CSRF")

    secret_candidate = manual.normalize_candidate(
        {"title": "The Sandman 001.cbz", "provider_id": "slskd", "protocol": "soulseek", "username": "private-user", "path": "Books/private-path/The Sandman 001.cbz", "api_key": "must-not-escape", "url": "https://fixture.invalid/private?token=secret", "accepted": True, "candidate_safe": True},
        CASES[1][0],
        search_run_id="redaction-run",
    )
    serialized = json.dumps(secret_candidate, sort_keys=True)
    require("must-not-escape" not in serialized and "private-user" not in serialized and "fixture.invalid" not in serialized, "candidate payload must redact reusable source evidence")
    print(json.dumps({"ok": True, "known_titles": len(evidence), "auth_routes": 3, "redaction": True}, indent=2))


if __name__ == "__main__":
    run()
