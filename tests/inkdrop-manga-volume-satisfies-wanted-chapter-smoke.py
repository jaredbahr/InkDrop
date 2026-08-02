#!/usr/bin/env python3
"""A volume release satisfies a wanted chapter it is known to contain.

Found against live production data (Wild Strawberry
and Hunter x Hunter, findings 4 and 8 in the SLSKD miss audit): a wanted
manga chapter whose own metadata (target_context, sourced from ComicVine's
real chapter->volume grouping, never a filename guess) says it belongs to
volume N was unconditionally blocked "wrong_unit_type" against any volume-
form candidate, even when that candidate's own parsed evidence is that same
volume N with no conflicting chapter/issue identity. 205 candidates were
blocked this way in production. Live data: Wild Strawberry chapters 1-3 all
carry volume="1" (comicvine-sourced) alongside a shared mangadex_id, and the
only cached candidate for all three is "Wild Strawberry v01 ... .cbz".

The fix only fires when the wanted item's own real volume metadata agrees
with the candidate's own parsed volume evidence -- it never infers coverage
from a filename guess, and stays blocked whenever that metadata is absent
(the exact prior behavior, so no wanted chapter that lacks volume metadata
regresses).
"""

import inkdrop_candidate_matching as matching


def require(condition, message):
    if not condition:
        raise AssertionError(message)


# Wild Strawberry chapter 1: real production wanted-item shape (comicvine-
# sourced volume grouping, chapter unit type).
wanted_chapter = {
    "series": "Wild Strawberry",
    "media_type": "manga",
    "unit_type": "chapter",
    "chapter": "1",
    "issue": "1",
    "volume": "1",
}

# The only cached SLSKD candidate for this wanted row in production.
volume_candidate = {"title": "Wild Strawberry v01 (2024) (Digital) (1r0n) (PRE).cbz"}

result = matching.candidate_compatibility(volume_candidate, wanted_chapter)
require(
    result["status"] != "blocked",
    f"a volume release the chapter's own metadata says it belongs to must not "
    f"be blocked wrong_unit_type: {result}",
)
require(
    "wrong_unit_type" not in result["rejection_codes"],
    f"wrong_unit_type must not fire when target volume matches evidence volume: {result}",
)
require(
    "exact_volume_containing_wanted_chapter" in result["positive_evidence"],
    f"expected positive evidence for the volume/target match: {result}",
)

# A DIFFERENT volume than the wanted chapter's own metadata must still block --
# this is not "any volume accepts any chapter", only the one real metadata
# says contains it.
wrong_volume_candidate = {"title": "Wild Strawberry v02 (2024) (Digital) (1r0n) (PRE).cbz"}
result_wrong_volume = matching.candidate_compatibility(wrong_volume_candidate, wanted_chapter)
require(
    result_wrong_volume["status"] == "blocked",
    f"a volume other than the one the wanted chapter's metadata names must still block: {result_wrong_volume}",
)
require(
    "wrong_unit_type" in result_wrong_volume["rejection_codes"],
    f"expected wrong_unit_type for a mismatched volume: {result_wrong_volume}",
)

# No volume metadata on the wanted item at all -- exact prior behavior,
# unconditionally blocked, since there is nothing real to compare against.
wanted_chapter_no_volume_metadata = {
    "series": "Some Other Manga",
    "media_type": "manga",
    "unit_type": "chapter",
    "chapter": "1",
    "issue": "1",
}
result_no_metadata = matching.candidate_compatibility(
    {"title": "Some Other Manga v01 (2024).cbz"}, wanted_chapter_no_volume_metadata
)
require(
    result_no_metadata["status"] == "blocked" and "wrong_unit_type" in result_no_metadata["rejection_codes"],
    f"without real target volume metadata the prior blocked behavior must be unchanged: {result_no_metadata}",
)

# A volume candidate that ALSO carries chapter evidence must not be silently
# waved through by the new branch -- real chapter/issue evidence still wins.
conflicting_candidate = {"title": "Wild Strawberry v01 Chapter 9 (2024) (Digital) (1r0n) (PRE).cbz"}
result_conflict = matching.candidate_compatibility(conflicting_candidate, wanted_chapter)
require(
    "exact_volume_containing_wanted_chapter" not in result_conflict["positive_evidence"],
    f"conflicting chapter evidence must not be overridden by the volume match: {result_conflict}",
)

print("inkdrop-manga-volume-satisfies-wanted-chapter-smoke: ok")
