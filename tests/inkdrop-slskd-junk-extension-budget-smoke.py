#!/usr/bin/env python3
"""Junk-extension files (audio, etc.) must not consume the file enumeration
budget ahead of the real comic files in the same SLSKD response.

Root cause found 2026-08-01 against live production data (finding 7 in the
SLSKD miss audit): candidates_from_responses() incremented checked_file_count
-- the counter compared against the caller's file_cap -- for every response
row before checking its extension. A response dominated by non-comic files
(commonly audio: mp3/flac/m4a) ordered ahead of the real comic file in
SLSKD's file listing could exhaust file_cap before the wanted comic file was
ever reached, even though those junk files were always going to be rejected
by item_match_details() on extension alone regardless of anything else.
Confirmed live: rejected_file_count landed exactly on the 2000-file cap in
277 real search attempts, with audio over 50% of rejections in 360 of 3751
executed searches (10%).
"""

import inkdrop_slskd_source_probe as probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


item = {
    "series": "Wild Strawberry",
    "query": "Wild Strawberry",
    "issue": "1",
    "media_type": "manga",
}

# 50 junk audio files ordered BEFORE the one real comic file -- mirrors a
# real Soulseek user sharing a mixed music+comics folder.
junk_files = [
    {"filename": f"Music\\Album\\track{i:02d}.mp3", "size": 5_000_000}
    for i in range(50)
]
real_file = {
    "filename": r"Comics\Wild Strawberry\Wild Strawberry 001 (2024) (Digital) (1r0n).cbz",
    "size": 20_000_000,
}
response = {
    "username": "peer1",
    "uploadSpeed": 1_000_000,
    "queueLength": 0,
    "hasFreeUploadSlot": True,
    "files": [*junk_files, real_file],
}

# A file_cap smaller than the junk count: if junk consumed budget, the real
# file at the end of the list would never be reached.
candidates, summary = probe.candidates_from_responses(
    [response], item, max_files=10, candidate_limit=5,
)
require(
    any(c.get("filename") == real_file["filename"] for c in candidates),
    f"the real comic file was starved out by junk-extension files consuming "
    f"the enumeration budget: candidates={candidates}, summary={summary}",
)
require(
    summary.get("checked_file_count", 0) <= 10,
    f"file_cap must still bound the number of REAL (comic-extension) files "
    f"inspected: {summary}",
)
require(
    summary.get("rejected_file_count", 0) >= 50,
    f"junk files must still be recorded as rejections for diagnostics: {summary}",
)

print("inkdrop-slskd-junk-extension-budget-smoke: ok")
