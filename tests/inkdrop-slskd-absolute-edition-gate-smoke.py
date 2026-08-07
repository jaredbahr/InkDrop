#!/usr/bin/env python3
"""Regression for generalizing item_match_details()'s "Absolute edition"
disambiguation check beyond manual_search_discovery.

Context: the real Court of Owls symptom (SLSKD repeatedly grabbing a 2012
trade paperback for a 2015 "Absolute Edition" hardcover, 399 failed attempts)
is NOT fully resolved by this fix alone -- see
docs/inkdrop/court-of-owls-edition-matching-scoping.md. That candidate has
enough franchise/volume evidence to pass this specific check under either
mode. This test isolates the narrower, real thing this session did fix: the
check itself no longer requires manual_search_discovery to run, so it now
also protects any path that reaches allow_connectors=True via
collected_singleton_proof (the automatic-pipeline equivalent) once that flag
is actually wired through -- confirmed here, not left as an assumption.
"""

import sys
import types

if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace()

from core import inkdrop_slskd_source_probe as probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


SERIES = "Absolute Batman: The Court of Owls"
# No "absolute" in the filename, no volume/omnibus/collection evidence, but
# an explicit issue marker (#1) so this candidate clears the earlier issue-
# token gate and actually reaches the edition-disambiguation check under
# test -- a candidate that fails earlier for an unrelated reason would give
# matched=False either way and prove nothing about this specific check.
NO_EVIDENCE_CANDIDATE = "Batman - The Court of Owls #1 (2018) (digital).cbr"


def check(item, expect_matched, label):
    result = probe.item_match_details(NO_EVIDENCE_CANDIDATE, item)
    require(
        result.get("matched") is expect_matched,
        f"{label}: expected matched={expect_matched}, got {result}",
    )
    return result


# 1. manual_search_discovery=True: the check already ran here before this
# session's change. Confirms the fix didn't break the original case.
check(
    {"series": SERIES, "issue": "1", "year": 2015, "pack_allowed": True, "manual_search_discovery": True},
    False,
    "manual search",
)

# 2. collected_singleton_proof=True, manual_search_discovery absent: this is
# the automatic-pipeline equivalent flag (inkdrop_source_worker_coordinator.py).
# Before this session's fix, the manual_search_discovery-only gate meant this
# path had NO edition protection at all. Now it does.
check(
    {"series": SERIES, "issue": "1", "year": 2015, "pack_allowed": True, "collected_singleton_proof": True},
    False,
    "automatic pipeline via collected_singleton_proof",
)

# 3. A genuinely different, unrelated series must not be affected -- the
# edition_match regex only fires for "Absolute <word+>: ..." series titles.
result = probe.item_match_details(
    "The Long Halloween 01 (2019) (digital).cbr",
    {"series": "Batman: The Long Halloween", "issue": "1", "year": 2019, "pack_allowed": True},
)
require(result.get("matched") is True, f"unrelated series must be unaffected by the edition check: {result}")

print("inkdrop-slskd-absolute-edition-gate-smoke: all checks passed")
