#!/usr/bin/env python3
"""Regression for collected_singleton_edition_conflict() in
inkdrop_slskd_source_probe.py.

Context: the manual_search_discovery gate removal (earlier this session) was
verified real but insufficient -- the actual reported Court of Owls symptom
(SLSKD repeatedly grabbing a 2012 trade paperback for a 2015 "Absolute
Edition" hardcover, 399 failed attempts) survives that fix, because the real
wrong-candidate filename has enough generic franchise/volume evidence to
satisfy item_match_details()'s existing exception clause regardless.

collected_singleton_proof is already correctly wired end to end for the
automatic pipeline (confirmed live against production: it resolves True for
comicvine:86918 with collected_singleton_markers=["hardcover"]) -- the gap
was that nothing downstream used that specific marker/year evidence to tell
two different real printings of the same underlying story apart. This test
proves that check, using the exact real filename/series pair from
production, and that it stays a no-op for ordinary series and for
candidates that don't supply a conflicting year.

Update (2026-07-28, generalized -- supersedes two same-day reversals): this
went single-series exemption -> fully reverted/enforced -> generalized, all
in one day, as the operator's own thinking on it evolved. The final, current
direction: the real signal was never "is this specific series id on a
list", it's "does ComicVine's own catalog have a real, separate, plain
(non-special-edition) volume for this title at all" -- checked live via
comicvine_edition_target_has_standalone_alternative(). When it doesn't
(Court of Owls: confirmed live, every ComicVine search phrasing returns only
Absolute/Unwrapped/Noir/coloring-book/Essential-digest editions, no plain
trade), the tracked edition was never a real choice among options, so any
complete, correctly-identified release is accepted. When it does (a series
that legitimately has both a plain and a special printing catalogued
separately -- proven against 6 real Avatar-franchise series in production),
the strict marker/year check still applies. Mocking that lookup here
(rather than hitting the real network) proves both directions of the
*general* mechanism, not a hardcoded id in either direction.
"""

import sys
import types
from unittest import mock

if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace()

import inkdrop_slskd_source_probe as probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


# Reconstructed from queue_source_review_item()'s real output for
# comicvine:86918, captured live against production 2026-07-27.
COURT_OF_OWLS_ITEM = {
    "series": "Absolute Batman: The Court of Owls",
    "query": "Absolute Batman: The Court of Owls 1 2015",
    "issue": "1",
    "year": 2015,
    "pack_allowed": True,
    "collected_singleton_proof": True,
    "singleton_series_title": "Absolute Batman: The Court of Owls",
    "collected_singleton_markers": ["hardcover"],
    "comicvine_id": "86918",
}

# A same-shaped item -- same series title and markers, so it still reaches the
# edition-conflict check -- but a different comicvine_id, standing in for a
# series where ComicVine *does* catalog a real standalone plain edition
# separately (e.g. an Omnibus reprint of a story that also has its own plain
# trade volume, confirmed for real against 6 Avatar-franchise series in
# production). Proves the general mechanism goes both ways, not just "off".
OTHER_SERIES_ITEM = dict(COURT_OF_OWLS_ITEM, comicvine_id="1")

REAL_WRONG_FILE = "Batman v01 - The Court of Owls (2012) (digital) (Minutemen-PhD).cbr"


def fake_has_standalone_alternative(comicvine_id, series_title):
    # comicvine:86918 (Court of Owls): confirmed live -- no standalone plain
    # edition exists, so this must resolve False (relax).
    # comicvine:1 (stand-in for an ordinary series with a real plain
    # alternative catalogued separately): must resolve True (stay strict).
    return {"86918": False, "1": True}.get(str(comicvine_id), None)


with mock.patch.object(
    probe,
    "comicvine_edition_target_has_standalone_alternative",
    side_effect=fake_has_standalone_alternative,
):
    # 1. Court of Owls: ComicVine has no standalone plain edition of this
    # title at all, so any complete, correctly-identified release is
    # accepted instead of requiring the exact tracked Absolute Edition. The
    # real 2012 trade candidate that used to be rejected here must now be
    # accepted.
    result = probe.item_match_details(REAL_WRONG_FILE, COURT_OF_OWLS_ITEM)
    require(
        "edition marker" not in " ".join(result.get("penalties") or []) and "conflicts with target year" not in " ".join(result.get("penalties") or []),
        f"a series with no standalone plain ComicVine edition must be exempt from the edition-conflict check: {result}",
    )

    # 2. That exemption fires even though the item still has real
    # collected_singleton_markers and a real target year -- it isn't a
    # side effect of missing evidence.
    require(bool(COURT_OF_OWLS_ITEM.get("collected_singleton_markers")), "the exemption must be proven with real markers present, not by accident")

    # 2b. The SAME wrong-edition shape for a series where ComicVine *does*
    # catalog a real standalone plain edition must still be rejected --
    # proving the mechanism discriminates on the real catalog fact, not on
    # a fixed list of exempt series ids.
    result = probe.item_match_details(REAL_WRONG_FILE, OTHER_SERIES_ITEM)
    require(result.get("matched") is False, f"a series with a real standalone plain edition must still be rejected on a genuine edition/year conflict: {result}")
    require(
        "2012" in (result.get("penalties") or [""])[0] and "2015" in (result.get("penalties") or [""])[0],
        f"rejection reason should name both conflicting years: {result}",
    )

    # 2c. A comicvine_id the lookup can't resolve (mock returns None, same
    # shape as "no API key" or "ComicVine unreachable" in production) must
    # default to the strict check -- unproven is not the same as proven
    # absent.
    unresolvable_item = dict(COURT_OF_OWLS_ITEM, comicvine_id="999999")
    result = probe.item_match_details(REAL_WRONG_FILE, unresolvable_item)
    require(result.get("matched") is False, f"an unresolvable lookup must default to the strict check, not relax: {result}")

    # 3. A candidate that echoes the target's own marker (hardcover) is not
    # penalized by this check, even with a conflicting year -- the marker
    # itself is stronger evidence than the year mismatch. (Checked against
    # the non-exempt item so this still exercises the marker-echo logic
    # itself, not just the exemption short-circuit.)
    result = probe.item_match_details(
        "Batman - The Court of Owls Hardcover (2012 print run) (digital).cbr",
        OTHER_SERIES_ITEM,
    )
    require(
        "edition marker" not in " ".join(result.get("penalties") or []),
        f"a candidate carrying the target's own marker must not be rejected by the edition-conflict check: {result}",
    )

    # 4. A candidate with no year at all can't conflict -- nothing to compare.
    result = probe.item_match_details(
        "Batman v01 - The Court of Owls (digital) (Minutemen-PhD).cbr",
        OTHER_SERIES_ITEM,
    )
    require(
        "edition marker" not in " ".join(result.get("penalties") or []),
        f"a yearless candidate must not trip the edition-conflict check: {result}",
    )

    # 5. An ordinary series (no collected_singleton_markers at all) is
    # completely unaffected -- the check must be a pure no-op when there's
    # nothing to compare, and must never even reach the ComicVine lookup.
    result = probe.item_match_details(
        "Watchmen v01 (1987) (digital).cbr",
        {"series": "Watchmen", "issue": "1", "year": 2019, "pack_allowed": True},
    )
    require(
        "edition marker" not in " ".join(result.get("penalties") or []),
        f"a series with no collected_singleton_markers must never trip this check: {result}",
    )

print("inkdrop-slskd-collected-edition-year-conflict-smoke: all checks passed")
