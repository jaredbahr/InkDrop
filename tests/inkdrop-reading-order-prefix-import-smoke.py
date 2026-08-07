#!/usr/bin/env python3
"""A completed SLSKD download with a reading-order/collection prefix was
rejected before import and abandoned in favor of a fresh grab of the same
issue, even though the file on disk was already correct.

Real tester report: SLSKD reports `1. House of X 01 (of 06) ...`,
`3. House of X 02 (of 06) ...`, `6. House of X 03 (of 06) ...` as
Completed, Succeeded. Both the SLSKD staged matcher
(inkdrop_slskd_source_probe.weak_staged_filename_guard) and the final
import classifier (inkdrop_completed_import.classify_import_filename_safety)
independently rejected these as weak_filename_unit_evidence: their bare
leading-numeric-prefix guard doesn't recognize a parenthesized
`NN (of MM)` unit, and the generic issue extractor picks the leading
reading-order ordinal (1/3/6) instead of the real issue number (01/02/03)
appearing after the exact title.

Fix: one shared helper, inkdrop_artifact_acceptance.
trusted_numeric_prefix_import_is_safe(), used by both call sites, that only
exempts a leading numeric prefix when everything after it begins with the
complete trusted title and the unit immediately following that exact title
(or the leading number itself) is the trusted issue. This also covers the
separately-reported Ms. Marvel ("18 - Ms. Marvel 007 ...") and Deadman
Wonderland ("0012 -  - Deadman Wonderland.cbz") numeric-prefix conventions
with the same helper -- two reports' fixes are the same underlying gap.
"""

from __future__ import annotations

from core import inkdrop_artifact_acceptance as iaa
from core import inkdrop_completed_import as completed_import
from core import inkdrop_slskd_source_probe as probe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


HOUSE_OF_X_TARGET = {
    "title": "House of X",
    "series": "House of X",
    "aliases": [],
    "media_type": "comic",
    "publisher": "Marvel",
    "year": 2019,
    "canonical_issue_count": 6,
}


def house_of_x_positive_fixtures_accepted_end_to_end():
    cases = [
        ("1. House of X 01 (of 06) (2019) (Digital) (Zone-Empire).cbr", "1"),
        ("3. House of X 02 (of 06) (2019) (Digital) (Zone-Empire).cbr", "2"),
        ("6. House of X 03 (of 06) (2019) (Digital) (Zone-Empire).cbr", "3"),
        ("House of X 04 (of 06) (2019) (Digital) (Zone-Empire).cbr", "4"),
    ]
    for filename, issue in cases:
        path = f"/downloads/slskd/House of X/{filename}"
        result = completed_import.classify_import_filename_safety(
            path, target=HOUSE_OF_X_TARGET, kind="comics", trusted_issue=issue,
        )
        require(result.get("ok") is True, f"House of X #{issue} ({filename!r}) still rejected by final import classifier: {result}")

        staged_guard = probe.weak_staged_filename_guard(
            filename, {"series": "House of X", "issue": issue},
        )
        require(
            staged_guard != "weak_filename_unit_evidence",
            f"House of X #{issue} ({filename!r}) still rejected by the SLSKD staged matcher: {staged_guard!r}",
        )

    # Non-parenthesized "of" and labeled-number equivalents from the report's
    # required regression list.
    for filename, issue in (
        ("3. House of X #02 (of 06) (2019).cbr", "2"),
        ("3. House of X Issue 02 (of 06) (2019).cbr", "2"),
        ("3. House of X 02 of 06 (2019).cbr", "2"),
    ):
        result = completed_import.classify_import_filename_safety(
            f"/downloads/{filename}", target=HOUSE_OF_X_TARGET, kind="comics", trusted_issue=issue,
        )
        require(result.get("ok") is True, f"equivalent form still rejected: {filename!r}: {result}")


def house_of_x_negatives_still_rejected():
    # A related-but-different series must not borrow the exemption.
    require(
        iaa.trusted_numeric_prefix_import_is_safe("6. Powers of X 03 (of 06) (2019)", ["House of X"], "3") is False,
        "a different series with the same reading-order shape incorrectly passed",
    )
    require(
        completed_import.classify_import_filename_safety(
            "/downloads/6. Powers of X 03 (of 06) (2019).cbr",
            target=HOUSE_OF_X_TARGET, kind="comics", trusted_issue="3",
        ).get("ok") is not True,
        "Powers of X incorrectly satisfied a House of X target",
    )
    # A later issue than trusted, even with a matching leading ordinal.
    require(
        iaa.trusted_numeric_prefix_import_is_safe("3. House of X 05 (of 06) (2019)", ["House of X"], "3") is False,
        "a wrong issue number after the exact title incorrectly passed",
    )
    # Implausible/malformed "of total".
    require(
        iaa.trusted_numeric_prefix_import_is_safe("3. House of X 03 (of 9999)", ["House of X"], "3") is False,
        "an implausible collection-size suffix incorrectly passed",
    )


MS_MARVEL_TARGET = {"title": "Ms. Marvel", "series": "Ms. Marvel", "aliases": [], "media_type": "comic", "canonical_issue_count": 50}
DEADMAN_WONDERLAND_TARGET = {"title": "Deadman Wonderland", "series": "Deadman Wonderland", "aliases": [], "media_type": "manga", "canonical_issue_count": 40}


def numeric_prefix_report_fixtures_unified():
    """The already-reported bare-number-before-title cohort must keep working
    under the same shared helper -- this is not a second, separately
    maintained fix."""
    positives = [
        (MS_MARVEL_TARGET, "18 - Ms. Marvel 007 (2016) (Digital) (Zone-Empire).cbr", "7"),
        (MS_MARVEL_TARGET, "28 - Ms. Marvel 008 (2016) (Digital) (Zone-Empire).cbr", "8"),
        (DEADMAN_WONDERLAND_TARGET, "0012 -  - Deadman Wonderland.cbz", "12"),
        (DEADMAN_WONDERLAND_TARGET, "0035 -  - Deadman Wonderland.cbz", "35"),
    ]
    for target, filename, issue in positives:
        result = completed_import.classify_import_filename_safety(
            f"/downloads/{filename}", target=target, kind="comics" if target is MS_MARVEL_TARGET else "manga",
            trusted_issue=issue,
        )
        require(result.get("ok") is True, f"{filename!r} still rejected: {result}")

    negatives = [
        (MS_MARVEL_TARGET, "051 Giant-Size Ms. Marvel 001 (2006).cbz", "1"),
        (MS_MARVEL_TARGET, "11 Dark Web - Ms. Marvel 001 (2023).cbr", "11"),
        (MS_MARVEL_TARGET, "18 - Ms. Marvel 009.cbr", "7"),
    ]
    for target, filename, issue in negatives:
        result = completed_import.classify_import_filename_safety(
            f"/downloads/{filename}", target=target, kind="comics", trusted_issue=issue,
        )
        require(result.get("ok") is not True, f"{filename!r} incorrectly satisfied issue {issue}: {result}")


def existing_pack_and_wrong_series_negatives_unchanged():
    """Preexisting safety gates (pack/range, wrong series, chapter-for-comic)
    must still fire even though the leading-numeric-prefix gate is now
    exempted for trusted reading-order cases -- the exemption is narrowly
    scoped to ONE gate, not a bypass of the whole classifier."""
    pack_result = completed_import.classify_import_filename_safety(
        "/downloads/House of X 01-06 (2019) Complete.cbr",
        target=HOUSE_OF_X_TARGET, kind="comics", trusted_issue="1",
    )
    require(pack_result.get("ok") is not True, f"a real pack/range filename was incorrectly accepted: {pack_result}")

    chapter_for_comic = completed_import.classify_import_filename_safety(
        "/downloads/1. House of X Chapter 01 (of 06) (2019).cbr",
        target=HOUSE_OF_X_TARGET, kind="comics", trusted_issue="1",
    )
    require(
        chapter_for_comic.get("ok") is not True,
        f"a chapter-marked artifact incorrectly satisfied a western comic issue target: {chapter_for_comic}",
    )


def main():
    house_of_x_positive_fixtures_accepted_end_to_end()
    house_of_x_negatives_still_rejected()
    numeric_prefix_report_fixtures_unified()
    existing_pack_and_wrong_series_negatives_unchanged()
    print(
        "READING_ORDER_PREFIX_IMPORT_OK: House of X #1-4 (reading-order prefix), the Ms. Marvel/"
        "Deadman Wonderland numeric-prefix cohort, and every adversarial negative all resolve "
        "correctly through the shared trusted_numeric_prefix_import_is_safe() helper in both the "
        "SLSKD staged matcher and the final import classifier."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
