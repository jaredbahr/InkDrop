#!/usr/bin/env python3
"""Kapowarr is dead/retired software (confirmed by the product owner) --
InkDrop must never again synthesize a "kapowarr:N" identity value, nor route
duplicate-identity decisions through historical Kapowarr routing data.

Root cause of the original bug (Wild Strawberry, finding 8): a watch record
still carrying a legacy kapowarr_id field got treated as a genuine "sibling"
series by the duplicate-identity guard, purely because
identity_values_for_item() manufactured a "kapowarr:100" identity string
from that leftover field. The narrower manga_companion_row() fix papered
over one symptom of this (a ComicVine/MangaDex companion pair colliding);
this test pins the actual root removal -- the "kapowarr:N" identity concept
no longer exists anywhere in the identity/dedup pipeline.
"""

from core import inkdrop_slskd_source_probe as probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


# A record carrying every legacy Kapowarr field shape ever used in this
# codebase must never produce a "kapowarr:" identity string.
legacy_item = {
    "series": "Wild Strawberry",
    "kapowarr_id": 100,
    "kapowarrId": 100,
    "volume_id": 100,
    "volumeId": 100,
}
identities = probe.identity_values_for_item(legacy_item)
require(
    not any(str(v).lower().startswith("kapowarr") for v in identities),
    f"a kapowarr-derived identity was still produced: {identities}",
)

# The historical Kapowarr-routing helpers must be fully gone, not just
# unreachable -- confirms the removal, not a re-gate.
for removed_name in (
    "target_kapowarr_ids_for_item",
    "historical_route_outside_sibling_issue_coverage",
    "candidate_identity_history",
    "candidate_identity_family_signature",
    "parse_identity_mismatch_ids",
):
    require(not hasattr(probe, removed_name), f"{removed_name} should have been removed, not just gated")

# duplicate_identity_gate must not mention Kapowarr in a live blocker for a
# candidate that only carries legacy Kapowarr-shaped fields -- it should
# fall through cleanly to the normal (non-Kapowarr) duplicate-title logic.
gate_item = {
    "series": "Some Unique Title Never Duplicated",
    "issue": "1",
    "kapowarr_id": 42,
}
blocker, reasons, review_reasons = probe.duplicate_identity_gate(
    "Some Unique Title Never Duplicated 001.cbz", gate_item, candidate={"filename": "x.cbz"}
)
require(
    "kapowarr" not in str(blocker).lower(),
    f"duplicate_identity_gate produced a Kapowarr-flavored blocker: {blocker!r}",
)

print("inkdrop-slskd-no-kapowarr-identity-smoke: ok")
