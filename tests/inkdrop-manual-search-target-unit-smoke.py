#!/usr/bin/env python3
"""Trusted target-unit inference regressions for Manual Search."""

import json
import tempfile
from pathlib import Path

from core import inkdrop_candidate_matching as matching
from core import inkdrop_manual_search as manual
from core import inkdrop_manual_search_core as core
from core import inkdrop_state


def require(value, message):
    if not value:
        raise AssertionError(message)


def context(title, *, unit_number="17", trusted=True, unit_type="", volume_number=""):
    return manual.structured_search_input(
        {
            "canonical_work_title": "Localized Work",
            "media_type": "comic",
            "unit_type": unit_type,
            "unit_number": unit_number,
            "volume_number": volume_number,
            "trusted_unit_title": title,
            "target_unit_metadata_trusted": trusted,
        }
    )


for title in (
    "Band 17",
    "Tome 17",
    "Tomo 17",
    "Vol. 17",
    "Volume 17",
    "Book 17",
    "HC 17",
    "TPB 17",
    "17 HC",
    "17 TPB",
    "HC",
    "TPB",
):
    inferred = context(title)
    require(inferred["unit_type"] == "volume", (title, inferred))
    require(inferred["volume_number"] == "17", (title, inferred))
    require(inferred["unit_type_source"].startswith("trusted_volume"), (title, inferred))

for title in (
    "Band One",
    "Tome One",
    "Tomo One",
    "Vol One",
    "Volume One",
    "Book One",
    "HC One",
    "TPB One",
    "One HC",
    "One TPB",
):
    inferred = context(title, unit_number="1")
    require(inferred["unit_type"] == "volume", (title, inferred))
    require(inferred["volume_number"] == "1", (title, inferred))
    require(inferred["unit_type_source"] == "trusted_volume_title", (title, inferred))

# Trust, exact marker shape, and target-number alignment are all required.
for title, trusted in (
    ("Band 18", True),
    ("Volume 17-18", True),
    ("Omnibus Volume 17", True),
    ("The Book of Blood", True),
    ("Descender: The Machine Moon", True),
    ("Band 17", False),
    ("Volume Two", True),
    ("Volume One-Two", True),
    ("The Book One", True),
    ("Volume One Hundred", True),
):
    unit_number = "1" if "One" in title or "Two" in title else "17"
    inferred = context(title, unit_number=unit_number, trusted=trusted)
    require(inferred["unit_type"] == "issue", (title, trusted, inferred))

explicit_issue = context("Band 17", unit_type="issue")
require(explicit_issue["unit_type"] == "issue", explicit_issue)
require(explicit_issue["unit_type_source"] == "explicit_unit_type", explicit_issue)
trusted_number = context("", volume_number="17")
require(trusted_number["unit_type"] == "volume", trusted_number)


def insert_series(con, series_id, title, issue_id, issue_number, issue_title, *, media_type="comic", issue_provider="comicvine", issue_raw="{}"):
    con.execute(
        "insert into series(id,title,media_type,year,publisher,metadata_provider,metadata_id,source,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?)",
        (series_id, title, media_type, 2006, "Fixture", "comicvine", series_id, "native", 1, 1, "{}"),
    )
    con.execute(
        "insert into issues(id,series_id,issue_number,normalized_number,title,metadata_provider,metadata_id,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
        (issue_id, series_id, issue_number, issue_number, issue_title, issue_provider, issue_id, 1, 1, issue_raw),
    )


with tempfile.TemporaryDirectory(prefix="inkdrop-target-unit-") as temp:
    db = Path(temp) / "state.sqlite3"
    inkdrop_state.ensure_schema(db)
    with inkdrop_state.connect(db) as con, con:
        insert_series(con, "localized", "Fullmetal Alchemist", "localized-17", "17", "Band 17")
        insert_series(con, "word-volume", "Word Volume", "word-volume-1", "1", "Volume One")
        insert_series(con, "descender", "Descender", "descender-15", "15", "Singularities")
        insert_series(con, "singleton", "Batman: The Killing Joke", "singleton-1", "1", "The Killing Joke")
        insert_series(con, "collected-negative", "Western Archive", "collected-negative-1", "1", "Omnibus Volume 1")
        insert_series(con, "manga-volume", "Subtitle Manga", "manga-volume-38", "38", "Founding", media_type="manga")
        insert_series(con, "manga-chapter", "Chapter Native Manga", "manga-chapter-38", "38", "An ordinary subtitle", media_type="manga", issue_provider="mangadex")
        insert_series(con, "manga-explicit-chapter", "Explicit Chapter Manga", "manga-explicit-chapter-38", "38", "Founding", media_type="manga", issue_raw=json.dumps({"unit_type": "chapter"}))

    for series_id in ("localized", "word-volume", "descender", "singleton", "collected-negative", "manga-volume", "manga-chapter", "manga-explicit-chapter"):
        require(core.project_series(db, series_id)["ok"], series_id)

    with inkdrop_state.connect_read(db) as con:
        projected = {
            row["legacy_issue_id"]: row["unit_type"]
            for row in con.execute(
                "select legacy_issue_id,unit_type from identity_units where legacy_issue_id is not null"
            )
        }
    require(projected["localized-17"] == "volume", projected)
    require(projected["word-volume-1"] == "volume", projected)
    require(projected["descender-15"] == "issue", projected)
    require(projected["singleton-1"] == "issue", projected)
    require(projected["collected-negative-1"] == "issue", projected)
    require(projected["manga-volume-38"] == "volume", projected)
    require(projected["manga-chapter-38"] == "chapter", projected)
    require(projected["manga-explicit-chapter-38"] == "volume", projected)

    with inkdrop_state.connect_read(db) as con:
        manga_context = core._search_context(con, "manga-volume", "manga-volume-38")
    require(manga_context["unit_type"] == "volume", manga_context)
    require(manga_context["volume_number"] == "38", manga_context)
    require(manga_context["unit_type_source"] == "trusted_manga_volume_metadata_provider", manga_context)
    exact_manga_volume = matching.apply_compatibility(
        {"title": "Subtitle Manga v38 (2026) (Digital).cbz"}, manga_context
    )
    require("wrong_unit_type" not in exact_manga_volume["block_reasons"], exact_manga_volume)
    wrong_manga_chapter = matching.apply_compatibility(
        {"title": "Subtitle Manga Chapter 38.cbz"}, manga_context
    )
    require("wrong_unit_type" in wrong_manga_chapter["block_reasons"], wrong_manga_chapter)

    run = core.create_search_run(
        db,
        series_id="localized",
        issue_id="localized-17",
        provider_selection=["slskd"],
        requested_by="target-unit-smoke",
        now=10,
    )
    require(run["ok"], run)
    with inkdrop_state.connect_read(db) as con:
        stored = json.loads(
            con.execute("select context_json from manual_search_runs where id=?", (run["run_id"],)).fetchone()[0]
        )
    require(stored["unit_type"] == "volume", stored)
    require(stored["unit_type_source"] == "trusted_volume_title", stored)
    require(stored["volume_number"] == "17", stored)
    require(stored["trusted_unit_title"] == "Band 17", stored)

    with inkdrop_state.connect_read(db) as con:
        word_context = core._search_context(con, "word-volume", "word-volume-1")
    require(word_context["unit_type"] == "volume", word_context)
    require(word_context["volume_number"] == "1", word_context)
    require(word_context["unit_type_source"] == "trusted_volume_title", word_context)

    compatible = matching.apply_compatibility(
        {"title": "Fullmetal Alchemist v17 (2008) (Digital).cbz"}, stored
    )
    require("wrong_unit_type" not in compatible["block_reasons"], compatible)
    require("wrong_volume_number" not in compatible["block_reasons"], compatible)

    wrong_issue = matching.apply_compatibility(
        {"title": "Fullmetal Alchemist #017 (2008).cbz"}, stored
    )
    require("wrong_unit_type" in wrong_issue["block_reasons"], wrong_issue)
    wrong_volume = matching.apply_compatibility(
        {"title": "Fullmetal Alchemist v18 (2008).cbz"}, stored
    )
    require("wrong_volume_number" in wrong_volume["block_reasons"], wrong_volume)
    collected = matching.apply_compatibility(
        {"title": "Fullmetal Alchemist Omnibus Volume 17.cbz"}, stored
    )
    require(
        "collected_edition_disallowed" in collected["block_reasons"]
        or "wrong_unit_type" in collected["block_reasons"],
        collected,
    )

    with inkdrop_state.connect_read(db) as con:
        descender_context = core._search_context(con, "descender", "descender-15")
    require(descender_context["unit_type"] == "issue", descender_context)
    descender_volume = matching.apply_compatibility(
        {"title": "Descender v15 (Digital).cbz"}, descender_context
    )
    require("wrong_unit_type" in descender_volume["block_reasons"], descender_volume)

print("inkdrop manual search target unit smoke: PASS")
