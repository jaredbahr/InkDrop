#!/usr/bin/env python3
"""Network-free adversarials for generalized SLSKD series-directory handoff."""

import json
from unittest import mock

import inkdrop_slskd_source_probe as probe


MIB = 1024 * 1024


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wanted(series, issue, *, year="", publisher="", state="queued", media_type="comic"):
    key = probe.normalize(f"{series}-{issue}").replace(" ", "-")
    row = {
        "review_id": key,
        "series": series,
        "query": series,
        "issue": str(issue),
        "year": str(year),
        "publisher": publisher,
        "watch_publisher": publisher,
        "media_type": media_type,
        "autopilot_queue": True,
        "autopilot_state": state,
    }
    if media_type == "manga":
        row.update({"unit_type": "volume", "volume_number": str(issue)})
    return row


def file(path, size=72 * MIB):
    return {"filename": path, "size": size}


# Repeated terminal punctuation can be a materially different series title.
# It must not disappear through generic normalization (Dispatch != Dispatch!!),
# while normal punctuation omission and a single decorative mark remain valid.
dispatch = wanted("Dispatch", 1, year=2025, publisher="Image Comics")
dispatch_bang_path = r"Manga\Dispatch!!\Dispatch!! v01 c03.zip"
dispatch_details = probe.item_match_details(dispatch_bang_path, dispatch)
require(not dispatch_details["matched"], dispatch_details)
require(
    "significant terminal punctuation" in " ".join(dispatch_details.get("penalties") or []),
    dispatch_details,
)
dispatch_candidate = {
    **file(dispatch_bang_path),
    "score": 100,
    "has_free_upload_slot": True,
    "upload_speed": 4_000_000,
    "queue_length": 0,
    "locked": False,
    "username": "private-peer",
}
dispatch_verdict = probe.auto_grab_candidate_verdict(dispatch_candidate, dispatch)
require(dispatch_verdict["verdict"] == "blocked", dispatch_verdict)
require(not dispatch_verdict["autopick_eligible"], dispatch_verdict)
require(
    "candidate title has significant terminal punctuation for a different series identity"
    in dispatch_verdict["blockers"],
    dispatch_verdict,
)
trusted_fixture = {
    "status": "accepted",
    "positive_evidence": ["singleton_exact_title"],
    "rejection_codes": [],
    "review_codes": [],
}
with mock.patch.object(
    probe,
    "candidate_identity_compatibility",
    return_value=(trusted_fixture, probe.filename_leaf(dispatch_bang_path)),
):
    trusted_dispatch_verdict = probe.auto_grab_candidate_verdict(
        dispatch_candidate,
        {**dispatch, "singleton_issue_proof": {"fixture": True}},
    )
require(trusted_dispatch_verdict["verdict"] == "blocked", trusted_dispatch_verdict)
require(
    "candidate title has significant terminal punctuation for a different series identity"
    in trusted_dispatch_verdict["blockers"],
    "trusted singleton evidence must not override a wrong-series punctuation boundary",
)

for punctuation_item, punctuation_path in (
    (wanted("Danger!!", 1), r"Comics\Danger\Danger 001.cbz"),
    (wanted("What If?", 1), r"Comics\What If\What If 001.cbz"),
    (wanted("Saga", 1), r"Comics\Saga\Saga! 001.cbz"),
):
    normalized = probe.item_match_details(punctuation_path, punctuation_item)
    require(normalized["matched"], (punctuation_item, punctuation_path, normalized))


responses = [{
    "username": "private-peer",
    "uploadSpeed": 4_000_000,
    "queueLength": 0,
    "hasFreeUploadSlot": True,
    "files": [
        # Decorated western-comic folder with publisher parent/year suffix.
        file("Comics/Image Comics/Monstress (2015)/Monstress 001 (2015).cbz"),
        file("Comics/Image Comics/Monstress (2015)/Monstress 002 (2016).cbr"),
        file("Comics/Image Comics/Monstress (2015)/Monstress Talk-Stories 001.cbz"),
        file("Comics/Image Comics/Monstress (2015)/Monstress Covers 001.cbz", 8 * MIB),
        file("Comics/Image Comics/Monstress (2015)/Monster 001.cbz"),
        # Complete/year organizational leaves collapse only to Saga Complete.
        file("Comics/Saga Complete/2012/Saga 001 (2012) (Digital).cbz"),
        file("Comics/Saga Complete/2012/Saga 002 (2012) (Digital).cbz"),
        # One file per nested organizational volume directory must form one
        # bounded manga cohort even when a generic Complete layer is present.
        file("Manga/Chainsaw Man/Complete/Volume 01/Chainsaw Man v01.cbz", 96 * MIB),
        file("Manga/Chainsaw Man/Complete/Vol 02/Chainsaw Man v02.cbz", 101 * MIB),
        file("Manga/Chainsaw Man/Complete/Volume 02/Chainsaw Man v01.cbz", 99 * MIB),
        # Related-title and non-content directories must not collapse.
        file("Comics/Descender/Ascender/Ascender 001 (2019).cbz"),
        file("Comics/Descender/Ascender/Ascender 002 (2019).cbz"),
        file("Comics/Descender/Covers/Descender Covers 001.cbz", 8 * MIB),
        file("Comics/Descender/Covers/Descender Covers 002.cbz", 8 * MIB),
        file("Comics/Descender/Descender The Machine/Descender The Machine 001.cbz"),
        file("Comics/Descender/Descender The Machine/Descender The Machine 002.cbz"),
        file("Comics/Descender/notes.txt", 100),
        file("Comics/Descender/folder.jpg", 200_000),
    ],
}]

observations, summary = probe.slskd_series_directory_observations(responses, max_files=64)
directories = {row["directory"] for row in observations}
require("Comics/Image Comics/Monstress (2015)" in directories, directories)
require("Comics/Saga Complete" in directories, directories)
require("Manga/Chainsaw Man" in directories, directories)
require("Comics/Descender/Ascender" in directories, directories)
require("Comics/Descender/Covers" in directories, directories)
require("Comics/Descender/Descender The Machine" in directories, directories)
require(summary["observed_file_count"] <= 64 and not summary["observation_truncated"], summary)

monstress_1 = wanted("Monstress", 1, year=2015, publisher="Image Comics")
monstress_2_done = wanted("Monstress", 2, year=2015, publisher="Image Comics", state="completed")
saga_1 = wanted("Saga", 1, year=2012, publisher="Image Comics")
saga_2_failed = wanted("Saga", 2, year=2012, publisher="Image Comics")
chainsaw_1 = wanted("Chainsaw Man", 1, year=2018, publisher="Shueisha", media_type="manga")
chainsaw_2 = wanted("Chainsaw Man", 2, year=2018, publisher="Shueisha", media_type="manga")
descender_1 = wanted("Descender", 1, year=2015, publisher="Image Comics")
items = [monstress_1, monstress_2_done, saga_1, saga_2_failed, chainsaw_1, chainsaw_2, descender_1]


def prior_failure(review_id, _candidate):
    return review_id == saga_2_failed["review_id"]


cache = {}
try:
    with mock.patch.object(probe, "bad_candidate_match", side_effect=prior_failure):
        applied = probe.apply_series_directory_opportunities([], items, cache, observations=observations)
    expected = {
        monstress_1["review_id"],
        saga_1["review_id"],
        chainsaw_1["review_id"],
        chainsaw_2["review_id"],
    }
    require(set(applied["selected_review_ids"]) == expected, applied)
    require(monstress_2_done["review_id"] not in cache, "completed issue was reconsidered")
    require(saga_2_failed["review_id"] not in cache, "prior failure was reconsidered")
    require(descender_1["review_id"] not in cache, "related Descender folders crossed exact-series gate")
    require(all(len(row.get("candidates") or []) == 1 for row in cache.values()), cache)
    leaves = {row["candidates"][0]["filename"] for row in cache.values()}
    require("Monstress 001 (2015).cbz" in leaves, leaves)
    require("Saga 001 (2012) (Digital).cbz" in leaves, leaves)
    require("Chainsaw Man v01.cbz" in leaves and "Chainsaw Man v02.cbz" in leaves, leaves)
    require(not any("Talk-Stories" in leaf or "Covers" in leaf or "Monster 001" in leaf for leaf in leaves), leaves)
    persisted = json.dumps(cache, sort_keys=True)
    require("private-peer" not in persisted and "Comics/Image Comics" not in persisted, "raw routing leaked")

    # Replaying the same observations remains idempotent and cannot duplicate a
    # candidate or exceed the existing issue/byte selection accounting.
    with mock.patch.object(probe, "bad_candidate_match", side_effect=prior_failure):
        replay = probe.apply_series_directory_opportunities([], items, cache, observations=observations)
    require(set(replay["selected_review_ids"]) == expected, replay)
    require(all(len(row.get("candidates") or []) == 1 for row in cache.values()), "replay duplicated candidates")
finally:
    probe.SERIES_RUN_EPHEMERAL_CANDIDATES.clear()

# Every archive below previously looked like issue 1 to the individual-file
# handoff despite explicitly covering more than one unit. Unicode dash forms
# are literal so the test cannot accidentally normalize away the bypass.
multi_unit_archives = (
    "Saga 001-010 (Digital).cbz",
    "Saga 001‐010 (Digital).cbz",  # U+2010 hyphen
    "Saga 001‑010 (Digital).cbz",  # U+2011 non-breaking hyphen
    "Saga 001‒010 (Digital).cbz",  # U+2012 figure dash
    "Saga 001–010 (Digital).cbz",  # U+2013 en dash
    "Saga 001—010 (Digital).cbz",  # U+2014 em dash
    "Saga 001_010 (Digital).cbz",
    "Saga 001, 002 (Digital).cbz",
    "Saga 001 & 002 (Digital).cbz",
    "Saga 001 and 002 (Digital).cbz",
    "Saga 001 + 002 (Digital).cbz",
    "Saga 001;002 (Digital).cbz",
    "Saga 001⁄002 (Digital).cbz",  # U+2044 fraction slash
    "Saga 001∕002 (Digital).cbz",  # U+2215 division slash
    "Saga 001／002 (Digital).cbz",  # U+FF0F fullwidth solidus
    "Saga 001﹨002 (Digital).cbz",  # U+FE68 small reverse solidus
    "Saga 001⧸002 (Digital).cbz",  # U+29F8 big solidus
    "Saga 001 002 (Digital).cbz",
    "Saga 001:002 (Digital).cbz",
    "Saga 001：002 (Digital).cbz",
    "Saga 001|002 (Digital).cbz",
    "Saga 001•002 (Digital).cbz",
    "Saga 001※002 (Digital).cbz",
    "Saga 001 (2012) 002 (Digital).cbz",
    "Saga 001 2012 002 (Digital).cbz",
    "Saga 001 (and 002) (Digital).cbz",
    "Saga 001 (plus 002) (Digital).cbz",
    "Saga 001 (002 included) (Digital).cbz",
    "Saga 001.0000000000000000001-001.0000000000000000002 (Digital).cbz",
)
for archive in multi_unit_archives:
    is_pack, reason = probe.filename_has_pack_or_range(archive, item=saga_1)
    require(is_pack and reason, archive)
    candidate, candidate_reason = probe.series_run_candidate_for_item(
        file(archive),
        saga_1,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(candidate is None and "marker" in candidate_reason, (archive, candidate_reason))

# Publication year-month metadata and decimal issue/volume identities are not
# evidence that one archive covers multiple units.
individual_archives = (
    "Saga 001 (Image, 2018-04) (Digital).cbz",
    "Saga 001 (Image, 2018_04) (Digital).cbz",
    "Saga 001.5 (Digital).cbz",
    "Saga 001.0000000000000000001 (Digital).cbz",
    "Saga 001 2018 (Digital).cbz",
    "Saga 001 (2018-04) (Digital).cbz",
    "Saga 001 2018 04 (Digital).cbz",
    "Chainsaw Man v01.5.cbz",
)
for archive in individual_archives:
    item = chainsaw_1 if archive.startswith("Chainsaw") else saga_1
    require(not probe.filename_has_pack_or_range(archive, item=item)[0], archive)

# Numeric words that are part of the exact series title are consumed by the
# item-aware prefix and cannot masquerade as adjacent issue coverage.
new_52_issue = wanted("New 52", 1, year=2011, publisher="DC Comics")
require(
    not probe.filename_has_pack_or_range("New 52 001 (2011) (Digital).cbz", item=new_52_issue)[0],
    "numeric series title was misclassified",
)

# Alphabetic single-number parentheticals are exact issue-title/printing
# metadata, not hidden multi-unit coverage. Ordinal suffixes are never parsed
# as standalone unit tokens. These controls exercise the actual handoff gate.
title_controls = (
    ("Saga 001 (2 Guns) (Digital).cbz", "2 Guns", True),
    ("Saga 001 2 Guns (Digital).cbz", "2 Guns", True),
    ("Saga 001 (2nd Printing) (Digital).cbz", "2nd Printing", True),
    ("Saga 001 (Chapter 2) (Digital).cbz", "Chapter 2", False),
    ("Saga 001 Chapter 2 (Digital).cbz", "Chapter 2", False),
    ("Saga 001 - Chapter 2 (Digital).cbz", "Chapter 2", False),
)
for archive, issue_title, expect_handoff in title_controls:
    titled_item = dict(saga_1, issue_title=issue_title, title=issue_title)
    require(not probe.filename_has_pack_or_range(archive, item=titled_item)[0], archive)
    if expect_handoff:
        candidate, reason = probe.series_run_candidate_for_item(
            file(archive),
            titled_item,
            {"file_count": 2, "directory": "Comics/Saga"},
        )
        require(candidate is not None, (archive, reason))

printing_archive = "Saga 001 (2nd Printing) (Digital).cbz"
require(not probe.filename_has_pack_or_range(printing_archive, item=saga_1)[0], printing_archive)
printing_candidate, printing_reason = probe.series_run_candidate_for_item(
    file(printing_archive),
    saga_1,
    {"file_count": 2, "directory": "Comics/Saga"},
)
require(printing_candidate is not None, printing_reason)

# Masking a known title/printing phrase cannot hide a real later unit.
for archive, issue_title in (
    ("Saga 001 (2 Guns) 002 (Digital).cbz", "2 Guns"),
    ("Saga 001 (2nd Printing) 002 (Digital).cbz", "2nd Printing"),
):
    titled_item = dict(saga_1, issue_title=issue_title, title=issue_title)
    is_pack, reason = probe.filename_has_pack_or_range(archive, item=titled_item)
    require(is_pack and reason, archive)
    candidate, candidate_reason = probe.series_run_candidate_for_item(
        file(archive),
        titled_item,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(candidate is None and "marker" in candidate_reason, (archive, candidate_reason))

# The final covered unit must remain visible immediately before an archive
# extension. This matrix also exercises compatibility separators and alternate
# recognized archive suffixes without relying on a trailing release tag.
direct_extension_multi = (
    "Saga 001 002.cbz",
    "Saga 001／002.cbz",
    "Saga 001⧸002.cbz",
    "Saga 001：002.cbz",
    "Saga 001•002.cbz",
    "Saga 001 002.cbr",
    "Saga 001 002.zip",
    "Saga 001 plus 002).cbz",
    "Saga 001 [plus 002].cbz",
)
for archive in direct_extension_multi:
    is_pack, reason = probe.filename_has_pack_or_range(archive, item=saga_1)
    require(is_pack and reason, archive)
    if archive.lower().endswith((".cbz", ".cbr")):
        candidate, candidate_reason = probe.series_run_candidate_for_item(
            file(archive),
            saga_1,
            {"file_count": 2, "directory": "Comics/Saga"},
        )
        require(candidate is None and "marker" in candidate_reason, (archive, candidate_reason))

# Exact known issue-title masking runs before generic connector interpretation.
plus_two_item = dict(saga_1, issue_title="Plus 2", title="Plus 2")
plus_two_archive = "Saga 001 Plus 2.cbz"
require(not probe.filename_has_pack_or_range(plus_two_archive, item=plus_two_item)[0], plus_two_archive)

neutral_plus_two_item = dict(plus_two_item, publisher="", watch_publisher="")
plus_two_candidate, plus_two_reason = probe.series_run_candidate_for_item(
    file(plus_two_archive),
    neutral_plus_two_item,
    {"file_count": 2, "directory": "Comics/Saga"},
)
require(plus_two_candidate is not None, plus_two_reason)

for issue_title, archive in (
    ("And 2", "Saga 001 And 2.cbz"),
    ("1-2 Punch", "Saga 001 1-2 Punch.cbz"),
):
    title_item = dict(saga_1, issue_title=issue_title, title=issue_title, publisher="", watch_publisher="")
    require(not probe.filename_has_pack_or_range(archive, item=title_item)[0], archive)
    title_candidate, title_reason = probe.series_run_candidate_for_item(
        file(archive),
        title_item,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(title_candidate is not None, (archive, title_reason))

    later_unit_archive = archive[:-4] + " 002.cbz"
    is_pack, reason = probe.filename_has_pack_or_range(later_unit_archive, item=title_item)
    require(is_pack and reason, later_unit_archive)
    later_candidate, later_reason = probe.series_run_candidate_for_item(
        file(later_unit_archive),
        title_item,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(later_candidate is None and "marker" in later_reason, (later_unit_archive, later_reason))

for archive in ("Saga 001.cbz", "Saga 001.cbr", "Saga 001.zip"):
    require(not probe.filename_has_pack_or_range(archive, item=saga_1)[0], archive)

single_issue_item = dict(saga_1, publisher="", watch_publisher="")
for archive in ("Saga 001.cbz", "Saga 001.cbr"):
    single_candidate, single_reason = probe.series_run_candidate_for_item(
        file(archive),
        single_issue_item,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(single_candidate is not None, (archive, single_reason))

dotted_scene_multi = (
    "Saga.001.002.Digital.cbz",
    "Saga 001 002.Digital.cbz",
    "Saga 001•002.English.cbr",
    "Saga.001.010.Digital.cbz",
    "Saga.001：002.English.cbz",
    "Saga.001／002.Digital.cbz",
)
for archive in dotted_scene_multi:
    is_pack, reason = probe.filename_has_pack_or_range(archive, item=saga_1)
    require(is_pack and reason, archive)
    candidate, candidate_reason = probe.series_run_candidate_for_item(
        file(archive),
        saga_1,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(candidate is None and "marker" in candidate_reason, (archive, candidate_reason))

for archive in (
    "Saga.001.5.Digital.cbz",
    "Saga 001.5.Digital.cbz",
    "Chainsaw.Man.v01.5.Digital.cbz",
):
    decimal_item = chainsaw_1 if archive.startswith("Chainsaw") else saga_1
    require(not probe.filename_has_pack_or_range(archive, item=decimal_item)[0], archive)

dotted_title_item = dict(saga_1, issue_title="1-2 Punch", title="1-2 Punch", publisher="", watch_publisher="")
dotted_title_archive = "Saga.001.1-2.Punch.cbz"
require(not probe.filename_has_pack_or_range(dotted_title_archive, item=dotted_title_item)[0], dotted_title_archive)
dotted_title_candidate, dotted_title_reason = probe.series_run_candidate_for_item(
    file(dotted_title_archive),
    dotted_title_item,
    {"file_count": 2, "directory": "Comics/Saga"},
)
require(dotted_title_candidate is not None, dotted_title_reason)

dotted_later_unit = "Saga.001.1-2.Punch.002.cbz"
is_pack, reason = probe.filename_has_pack_or_range(dotted_later_unit, item=dotted_title_item)
require(is_pack and reason, dotted_later_unit)

for series, archive in (
    ("New 52", "New.52.001.cbz"),
    ("Ms. Marvel", "Ms.Marvel.001.cbz"),
):
    dotted_series_item = wanted(series, 1, year=2014)
    require(not probe.filename_has_pack_or_range(archive, item=dotted_series_item)[0], archive)
    if series == "Ms. Marvel":
        continue
    dotted_series_candidate, dotted_series_reason = probe.series_run_candidate_for_item(
        file(archive),
        dotted_series_item,
        {"file_count": 2, "directory": f"Comics/{series}"},
    )
    require(dotted_series_candidate is not None, (archive, dotted_series_reason))

numeric_series_controls = (
    ("New 52", "New-52-001.cbz"),
    ("New 52", "New‑52‑001.cbz"),
    ("New 52", "New–52–001.cbz"),
    ("New 52", "New—52—001.cbz"),
    ("Series 1-2", "Series 1-2 001.cbz"),
    ("Studio 52 Comics", "Studio-52-Comics-001.cbz"),
    ("Complete", "Complete 001.cbz"),
)
for series, archive in numeric_series_controls:
    numeric_series_item = wanted(series, 1, year=2014)
    require(not probe.filename_has_pack_or_range(archive, item=numeric_series_item)[0], archive)
    protected = probe.filename_without_exact_series_span(archive, item=numeric_series_item)
    require(probe.normalize(series) not in probe.normalize(protected), (archive, protected))
    if series != "Complete":
        numeric_candidate, numeric_reason = probe.series_run_candidate_for_item(
            file(archive),
            numeric_series_item,
            {"file_count": 2, "directory": f"Comics/{series}"},
        )
        require(numeric_candidate is not None, (archive, numeric_reason))

for archive in (
    "New-52-001-002.cbz",
    "New–52–001–002.cbz",
    "New 52 001, 002.cbz",
    "Series 1-2 001 & 002.cbz",
    "Complete 001 plus 002.cbz",
):
    if archive.startswith("New"):
        series = "New 52"
    elif archive.startswith("Series"):
        series = "Series 1-2"
    else:
        series = "Complete"
    numeric_series_item = wanted(series, 1, year=2014)
    is_pack, reason = probe.filename_has_pack_or_range(archive, item=numeric_series_item)
    require(is_pack and reason, archive)
    numeric_candidate, numeric_reason = probe.series_run_candidate_for_item(
        file(archive),
        numeric_series_item,
        {"file_count": 2, "directory": f"Comics/{series}"},
    )
    require(numeric_candidate is None and "marker" in numeric_reason, (archive, numeric_reason))

for series, archive in (
    ("New 52", "Comics/New 52/New-52-001.cbz"),
    ("Saga", "Comics/Saga/Saga 001.cbz"),
):
    repeated_item = wanted(series, 1, year=2014)
    require(not probe.filename_has_pack_or_range(archive, item=repeated_item)[0], archive)
    protected = probe.filename_without_exact_series_span(archive, item=repeated_item)
    require(probe.normalize(series) not in probe.normalize(protected), (archive, protected))

for series, archive in (
    ("New 52", "Comics/New 52/New-52-001-002.cbz"),
    ("Saga", "Comics/Saga/Saga 001-002.cbz"),
):
    repeated_item = wanted(series, 1, year=2014)
    is_pack, reason = probe.filename_has_pack_or_range(archive, item=repeated_item)
    require(is_pack and reason, archive)

for archive in (
    "Saga 001 002 Saga.cbz",
    "Saga 001•002 Saga.cbz",
    "Saga 001 (2012) 002 Saga.cbz",
    "Saga 001 2012 002 Saga.cbz",
):
    is_pack, reason = probe.filename_has_pack_or_range(archive, item=saga_1)
    require(is_pack and reason, archive)

for issue, archive in (
    ("1.00", "Saga 001.00.cbz"),
    ("1.01", "Saga 001.01.cbz"),
    ("1.010", "Saga 001.010.cbz"),
):
    decimal_wanted = wanted("Saga", issue, year=2012)
    require(not probe.filename_has_pack_or_range(archive, item=decimal_wanted)[0], (issue, archive))
    decimal_candidate, decimal_reason = probe.series_run_candidate_for_item(
        file(archive),
        decimal_wanted,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require("marker" not in decimal_reason, (issue, archive, decimal_reason))

for archive in (
    "Saga 001 1.00.cbz",
    "Saga 001,1.00.cbz",
    "Saga 001-1.00.cbz",
):
    require(not probe.filename_has_pack_or_range(archive, item=saga_1)[0], archive)

for archive in ("Saga 001.002.cbz", "Saga 001.010.cbz"):
    is_pack, reason = probe.filename_has_pack_or_range(archive, item=saga_1)
    require(is_pack and reason, archive)

mismatched_decimal_item = wanted("Saga", "2", year=2012)
for archive in ("Saga 001.00.cbz", "Saga 001.01.cbz"):
    is_pack, reason = probe.filename_has_pack_or_range(archive, item=mismatched_decimal_item)
    require(is_pack and reason, (archive, reason))
    candidate, candidate_reason = probe.series_run_candidate_for_item(
        file(archive),
        mismatched_decimal_item,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(candidate is None and "marker" in candidate_reason, (archive, candidate_reason))

malformed_cases = (
    (saga_1, "Saga -001.cbz"),
    (saga_1, "Saga .001.cbz"),
    (saga_1, "Saga 1.2.3.cbz"),
    (wanted("Saga", "1.2", year=2012), "Saga 1.2.3.cbz"),
    (saga_1, "Saga 1e3.cbz"),
    (saga_1, "Saga 00001.cbz"),
    (saga_1, "Saga -001 Saga.cbz"),
    (saga_1, "Saga .001 Saga.cbz"),
    (saga_1, "Saga 1.2.3 Saga.cbz"),
    (saga_1, "Saga Saga -001.cbz"),
    (saga_1, "Saga −001.cbz"),
    (saga_1, "Saga ±001.cbz"),
    (saga_1, "Saga ➕001.cbz"),
    (saga_1, "Saga ➖001.cbz"),
    (saga_1, "Saga ⊕001.cbz"),
    (saga_1, "Saga ⊖001.cbz"),
    (saga_1, "Saga ∔001.cbz"),
    (saga_1, "Saga ∸001.cbz"),
    (saga_1, "Saga ٫001.cbz"),
    (saga_1, "Saga,−001.cbz"),
    (saga_1, "Saga;⊕001.cbz"),
    (saga_1, "Saga[⊖001].cbz"),
    (saga_1, "Saga (-001).cbz"),
    (saga_1, "Saga (−001).cbz"),
    (saga_1, "Saga [+001].cbz"),
    (saga_1, "Saga ‑001.cbz"),
    (saga_1, "Saga –001.cbz"),
    (saga_1, "Saga —001.cbz"),
    (saga_1, "Saga ＋001.cbz"),
    (saga_1, "Saga ﹣001.cbz"),
    (saga_1, "Saga\u2003−001.cbz"),
    (saga_1, "Saga\u00a0±001.cbz"),
    (saga_1, "Saga −001 Saga.cbz"),
)
for malformed_item, archive in malformed_cases:
    require(not probe.filename_has_pack_or_range(archive, item=malformed_item)[0], archive)
    malformed_reason = probe.malformed_unit_syntax_reason(archive, item=malformed_item)
    require(malformed_reason.startswith("malformed unit syntax:"), (archive, malformed_reason))
    candidate, candidate_reason = probe.series_run_candidate_for_item(
        file(archive),
        malformed_item,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(candidate is None and candidate_reason.startswith("malformed unit syntax:"), (archive, candidate_reason))
    verdict = probe.auto_grab_candidate_verdict(file(archive), malformed_item)
    require(not verdict.get("autopick_eligible"), (archive, verdict))
    require(any("malformed unit syntax:" in value for value in verdict.get("blockers") or []), (archive, verdict))

for archive in ("-001.cbz", "−001.cbz", "⊕001.cbz", "(±001).cbz", ".001.cbz", "٫001.cbz", "1.2.3.cbz"):
    reason = probe.malformed_unit_syntax_reason(
        archive,
        item=saga_1,
        validated_series_directory=True,
    )
    require(reason.startswith("malformed unit syntax:"), (archive, reason))
    raw_path = f"private-root/Comics/Saga/{archive}"
    candidate, candidate_reason = probe.series_run_candidate_for_item(
        file(raw_path),
        saga_1,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(candidate is None and candidate_reason.startswith("malformed unit syntax:"), (archive, candidate_reason))
    verdict = probe.auto_grab_candidate_verdict(file(raw_path), saga_1)
    require(not verdict.get("autopick_eligible"), (archive, verdict))
    require(any("malformed unit syntax:" in value for value in verdict.get("blockers") or []), (archive, verdict))

for archive in ("Saga-001.cbz", "Saga.001.cbz", "Saga - 001.cbz"):
    require(not probe.malformed_unit_syntax_reason(archive, item=saga_1), archive)
    delimiter_item = dict(saga_1, publisher="", watch_publisher="")
    candidate, candidate_reason = probe.series_run_candidate_for_item(
        file(archive),
        delimiter_item,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(candidate is not None, (archive, candidate_reason))

for archive in ("Saga−001.cbz", "Saga‑001.cbz", "Saga—001.cbz", "Saga⊕001.cbz", "Saga٫001.cbz"):
    require(not probe.malformed_unit_syntax_reason(archive, item=saga_1), archive)

require(not probe.malformed_unit_syntax_reason("Saga 1٫2.cbz", item=wanted("Saga", "1.2", year=2012)), "Unicode decimal blocked")

valid_decimal_item = wanted("Saga", "1.2", year=2012)
require(not probe.malformed_unit_syntax_reason("Saga 1.2.cbz", item=valid_decimal_item), "valid decimal blocked")

for archive in ("001.cbz", "001 (2012).cbr"):
    require(
        not probe.filename_has_pack_or_range(
            archive,
            item=saga_1,
            validated_series_directory=True,
        )[0],
        archive,
    )

raw_single_path = "private-root/Comics/Saga/001.cbz"
raw_single_item = dict(saga_1, publisher="", watch_publisher="")
raw_single_candidate, raw_single_reason = probe.series_run_candidate_for_item(
    file(raw_single_path),
    raw_single_item,
    {"file_count": 2, "directory": "Comics/Saga"},
)
require(raw_single_candidate is not None, raw_single_reason)
require(raw_single_candidate["filename"] == raw_single_path, "synthetic policy changed the raw SLSKD route")

for archive in (
    "Comics/Saga 001/Saga 001.rar",
    "Comics/Saga 001/Saga 001.7z",
    "Comics/Saga 001/Saga 001.cbz",
    "Saga 001,001.cbz",
    "Saga issues 001 and 001.cbz",
    "Saga 001-001.cbz",
):
    require(not probe.filename_has_pack_or_range(archive, item=saga_1)[0], archive)

for archive in ("Comics/Saga 001/Saga 001.rar", "Comics/Saga 001/Saga 001.7z"):
    verdict = probe.auto_grab_candidate_verdict(file(archive), saga_1)
    require(not verdict.get("autopick_eligible"), (archive, verdict))
    require(verdict.get("verdict") in {"needs_review", "blocked"}, (archive, verdict))

same_unit_cbz = "Comics/Saga 001/Saga 001.cbz"
same_unit_verdict = probe.auto_grab_candidate_verdict(file(same_unit_cbz), saga_1)
require(not probe.filename_has_pack_or_range(same_unit_cbz, item=saga_1)[0], same_unit_cbz)

for archive in (
    "Comics/Saga 001/Saga 002.rar",
    "Comics/Saga 001/Saga 002.cbz",
    "Saga 001,002.cbz",
    "Saga issues 001 and 002.cbz",
):
    is_pack, reason = probe.filename_has_pack_or_range(archive, item=saga_1)
    require(is_pack and reason, archive)

for archive in ("001 002.cbz", "001•002.cbr", "001 (2012) 002.cbz"):
    is_pack, reason = probe.filename_has_pack_or_range(
        archive,
        item=saga_1,
        validated_series_directory=True,
    )
    require(is_pack and reason, archive)
    raw_path = f"private-root/Comics/Saga/{archive}"
    candidate, candidate_reason = probe.series_run_candidate_for_item(
        file(raw_path),
        saga_1,
        {"file_count": 2, "directory": "Comics/Saga"},
    )
    require(candidate is None and "marker" in candidate_reason, (archive, candidate_reason))

# Exact-title masking is constrained to title spans after the required unit;
# it cannot erase an equal/overlapping series identity or a pure numeric title.
overlap_controls = (
    ("Batman", "Batman", "Batman 001.cbz"),
    ("Absolute Batman: The Court of Owls", "Court of Owls", "Absolute Batman: The Court of Owls 001.cbz"),
    ("Saga", "Saga", "Saga 001.cbz"),
    ("Series", "1-2 Punch", "Series 001 - 1-2 Punch.cbz"),
)
for series, issue_title, archive in overlap_controls:
    overlap_item = wanted(series, 1, year=2012)
    overlap_item.update({"issue_title": issue_title, "title": issue_title})
    parse_view = probe.filename_without_exact_issue_titles(archive, item=overlap_item)
    require(probe.normalize(series) in probe.normalize(parse_view), (archive, parse_view))
    require(not probe.filename_has_pack_or_range(archive, item=overlap_item)[0], archive)
    if series.startswith("Absolute Batman"):
        require(parse_view == archive, "overlapping issue title erased part of the series span")
        continue
    overlap_candidate, overlap_reason = probe.series_run_candidate_for_item(
        file(archive),
        overlap_item,
        {"file_count": 2, "directory": f"Comics/{series}"},
    )
    require(overlap_candidate is not None, (archive, overlap_reason))
    require(overlap_candidate["filename"] == archive, "raw SLSKD route was altered")

# Folder identity remains exact and bounded; arbitrary subseries words cannot
# be stripped merely because a parent path happens to contain the target.
for directory in (
    "Comics/Descender/Ascender",
    "Comics/Descender/Covers",
    "Comics/Descender/Descender The Machine",
    "Comics/Descender/Descenderish",
    "Comics/Monstress/Monstress Talk-Stories",
):
    target = descender_1 if "Descender" in directory else monstress_1
    require(not probe.series_directory_matches_item(directory, target), directory)

print("inkdrop SLSKD series generalization smoke: PASS")
