#!/usr/bin/env python3
"""A plain, standard-format western comic filename must not be blocked for
lacking positive "English confidence" evidence it was never realistic for
it to carry.

Root cause found 2026-08-01 against live production data (finding 3b in the
SLSKD miss audit, user 474r4x14): western_comic_language_confidence_blocker()
required POSITIVE proof of English-ness (an explicit marker, the publisher's
name literally embedded in the filename, a 3+ file exact-series directory
cohort, or prior learned same-series history) before letting a candidate
through, on top of source_language_blocker() already screening out real
negative evidence (explicit non-English script/markers). Standard scene
release filenames never embed the publisher name, so this inverted the
burden of proof and blocked clean, tidy filenames by default -- confirmed
live: "What's The Furthest Place from Here 009 (2022).cbr" was blocked
while "Chew 012 (2010).cbr" from the same user only passed because of the
unrelated learned-history fallback.
"""

import inkdrop_slskd_source_probe as probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


item = {
    "series": "What's The Furthest Place From Here?",
    "issue": "9",
    "publisher": "Image",
    "year": "2022",
    "media_type": "comic",
}
candidate = {"username": "474r4x14"}

# The exact real production filename that was blocked.
clean_filename = "Comics\\What's The Furthest Place from Here\\What's The Furthest Place from Here 009 (2022).cbr"
blocker = probe.western_comic_language_confidence_blocker(clean_filename, candidate, item)
require(
    not blocker,
    f"a plain, standard-format filename with no negative language evidence "
    f"must not be blocked for lacking positive proof: {blocker!r}",
)

# Real negative evidence must still block -- this fix only removes the
# backwards default-deny, not the actual language screen. That screen is
# source_language_blocker(), applied as its own separate blocker in
# auto_grab_candidate_verdict(); western_comic_language_confidence_blocker()
# correctly defers to it (returns "") rather than double-reporting the same
# problem, so the negative case is verified at that layer.
foreign_filename = "Comics\\Some Series\\Some Series 009 (2022) (Francais).cbr"
require(
    bool(probe.source_language_blocker(foreign_filename)),
    "an explicit non-English marker must still block via source_language_blocker",
)
require(
    not probe.western_comic_language_confidence_blocker(foreign_filename, candidate, item),
    "western_comic_language_confidence_blocker must defer to source_language_blocker, not double-block",
)

# The narrow ambiguous-HQ-folder risk pattern must still block when there is
# no explicit English marker alongside it.
hq_filename = "Comics\\HQ\\Some Series\\Some Series 009 (2022).cbr"
hq_blocker = probe.western_comic_language_confidence_blocker(hq_filename, candidate, item)
require(
    bool(hq_blocker) and "ambiguous HQ source folder" in hq_blocker,
    f"an ambiguous HQ source folder without an explicit English marker must still block: {hq_blocker!r}",
)

# The same ambiguous HQ folder WITH an explicit English marker must not block.
hq_english_filename = "Comics\\HQ\\Some Series\\Some Series 009 (2022) (English).cbr"
hq_english_blocker = probe.western_comic_language_confidence_blocker(hq_english_filename, candidate, item)
require(
    not hq_english_blocker,
    f"an ambiguous HQ folder WITH an explicit English marker must not block: {hq_english_blocker!r}",
)

print("inkdrop-slskd-clean-filename-english-confidence-smoke: ok")
