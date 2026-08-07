#!/usr/bin/env python3
"""A ComicVine/MangaDex companion pair must never trip the SLSKD
duplicate-identity-collision guard against itself.

Found against live production data (Wild Strawberry,
alicejjackson's candidate for chapter 3): duplicate_identity_rows_for_item()
groups watched titles by normalized name and treats any two records whose
identity strings don't overlap as rival "sibling" series. It had no
awareness of manga_companion_links -- the table that records a ComicVine
record and a MangaDex record as the SAME real series, tracked under two
provider identities by design (see inkdrop_manga_companion.py). Because the
two sides' publisher/year can legitimately disagree (ComicVine record
enriched via a Kapowarr metadata adapter -> publisher "Viz", year 2024; the
MangaDex chapter record -> watch_publisher "MangaDex", watch_year 2023), the
candidate's real 2024 publication year matched the ComicVine side and not
the MangaDex side, so the guard blocked it as belonging to "another watched
volume" -- which was actually the same watched series.

Confirmed live: comicvine:162519 (kapowarr_id=100, publisher=Viz, year=2024)
and mangadex:7ae7fcbc-0aee-417d-9262-be4d5beac3ca are the real production
Wild Strawberry companion pair (manga_companion_links, status='linked').
"""

import tempfile
from pathlib import Path

from core import inkdrop_slskd_source_probe as probe
from core import inkdrop_state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


with tempfile.TemporaryDirectory() as temp_dir:
    db_path = Path(temp_dir) / "state.sqlite3"
    cv = inkdrop_state.record_provider_series_catalog(
        db_path, provider="comicvine", provider_series_id="162519", title="Wild Strawberry",
        metadata={"mediaType": "manga", "publisher": "Viz"},
    )
    md = inkdrop_state.record_provider_series_catalog(
        db_path, provider="mangadex", provider_series_id="7ae7fcbc-0aee-417d-9262-be4d5beac3ca", title="Wild Strawberry",
        metadata={"mediaType": "manga"},
    )
    linked = inkdrop_state.upsert_manga_companion_link(
        db_path,
        comicvine_series_id=cv["series_id"],
        mangadex_series_id=md["series_id"],
        normalized_title="wild strawberry",
    )
    require(linked.get("created") or linked.get("id"), f"companion link setup failed: {linked}")

    probe.INKDROP_STATE_DB = db_path

    # The wanted chapter -- MangaDex side, real production shape.
    item = {
        "series": "Wild Strawberry",
        "media_type": "manga",
        "chapter": "3",
        "issue": "3",
        "watch_publisher": "MangaDex",
        "watch_year": 2023,
        "mangadex_id": "7ae7fcbc-0aee-417d-9262-be4d5beac3ca",
    }

    # The ComicVine-side watch record, real production shape (kapowarr
    # metadata adapter gave it a different publisher/year than the
    # MangaDex-side chapter record).
    companion_sibling_row = {
        "source": "watch",
        "series": "Wild Strawberry",
        "identity": "kapowarr 100",
        "identities": ["kapowarr 100", "comicvine 162519", "watch b1cb9d08dca4"],
        "publisher": "Viz",
        "year": 2024,
        "kapowarr_id": 100,
        "comicvine_id": "162519",
        "mangadex_id": "",
    }
    require(
        probe.manga_companion_row(companion_sibling_row, item),
        "a real ComicVine/MangaDex companion pair must be recognized and excluded from duplicate-identity siblings",
    )

    # Negative control: an unrelated series sharing the same title, with no
    # real companion link, must still be treated as a genuine duplicate.
    unrelated_row = dict(companion_sibling_row)
    unrelated_row["comicvine_id"] = "999999"
    require(
        not probe.manga_companion_row(unrelated_row, item),
        "an unrelated sibling with no real companion link must still count as a genuine duplicate",
    )

print("inkdrop-slskd-companion-duplicate-identity-smoke: ok")
