#!/usr/bin/env python3
"""Prove library adoption is wired end to end: scan a real folder, match it against a
known series, and register the confirmed files as already-owned -- without touching the
filesystem or creating any queue/download work.

inkdrop_library_adoption.py itself (build_adoption_plan/apply_adoption_folder) had no
smoke test before this session. This proves both the module's own read/write contract
and inkdrop_web.py's run_library_adoption_plan/run_library_adoption_apply wrappers
(media-type-based search_fn selection, INKDROP_STATE_DB/BACKUP_DIR wiring) with real
files and a real sqlite database -- no mocked database.
"""

import json
import tempfile
import time
from pathlib import Path

from core import inkdrop_library_adoption
from core import inkdrop_state
from core import inkdrop_web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def write_comic(path, text="fake comic bytes"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "inkdrop-state.sqlite3"
        backup_dir = tmp_path / "backups"
        root = tmp_path / "existing-library"

        write_comic(root / "Watchmen" / "Watchmen 01.cbr")
        write_comic(root / "Watchmen" / "Watchmen 02.cbr")
        write_comic(root / "Some New Comic" / "Some New Comic 01.cbr")

        inkdrop_state.ensure_schema(db_path)
        now = time.time()
        with inkdrop_state.connect(db_path) as con:
            watchmen_id = inkdrop_state.upsert_series(
                con,
                {"name": "Watchmen", "source": "manual", "media_type": "comic"},
                now,
            )
            con.commit()

        fake_metadata_matches = [{"comicvineId": 999, "name": "Some New Comic", "year": 2020, "publisher": "Test Co"}]

        original_state_db = inkdrop_web.INKDROP_STATE_DB
        original_backup_dir = inkdrop_web.BACKUP_DIR
        original_search = inkdrop_web.comicvine_search_volumes
        inkdrop_web.INKDROP_STATE_DB = db_path
        inkdrop_web.BACKUP_DIR = backup_dir
        inkdrop_web.comicvine_search_volumes = lambda query, limit=5: fake_metadata_matches
        try:
            plan = inkdrop_web.run_library_adoption_plan({"root": str(root), "mediaType": "comic"})
            require(plan.get("ok"), f"plan should succeed: {plan}")
            summary = plan["summary"]
            require(summary["existing_series_folder_candidates"] == 1, f"Watchmen should match the known series: {summary}")
            require(summary["new_series_candidates"] == 1, f"the unknown folder should be a new-series candidate: {summary}")
            require(len(plan["candidates"]) == 2, f"expected 2 candidate folders: {plan['candidates']}")

            watchmen_candidate = next(c for c in plan["candidates"] if c["folder_name"] == "Watchmen")
            require(watchmen_candidate["status"] == "existing_series_folder_candidate", watchmen_candidate)
            require(watchmen_candidate["existing_series_matches"][0]["id"] == watchmen_id, "plan should surface the real series id")
            require(watchmen_candidate["file_count"] == 2, watchmen_candidate)
            require(all(not f["needs_review"] for f in watchmen_candidate["files"]), "both Watchmen files should parse a clean issue number")

            new_candidate = next(c for c in plan["candidates"] if c["folder_name"] == "Some New Comic")
            require(new_candidate["status"] == "new_series_candidate", new_candidate)
            require(new_candidate["metadata_matches"] == fake_metadata_matches, "plan should call the injected comicvine search_fn for unmatched folders")

            confirmed_files = [{"path": f["path"], "issue_number": f["issue_number"]} for f in watchmen_candidate["files"]]
            apply_result = inkdrop_web.run_library_adoption_apply({
                "root": str(root),
                "folderPath": watchmen_candidate["folder_path"],
                "mediaType": "comic",
                "seriesSelection": {"existing_series_id": watchmen_id},
                "confirmedFiles": confirmed_files,
            })
            require(apply_result["ok"], apply_result)
            require(apply_result["series_id"] == watchmen_id, apply_result)
            require(len(apply_result["registered"]) == 2, apply_result)
            require(Path(apply_result["backup_path"]).exists(), "apply should have written a real sqlite backup before writing")

            with inkdrop_state.connect(db_path) as con:
                rows = con.execute(
                    "select w.status, w.reason from wanted_items w where w.series_id = ?",
                    (watchmen_id,),
                ).fetchall()
            require(len(rows) == 2, f"expected 2 wanted_items rows for Watchmen: {rows}")
            require(all(row["status"] == "satisfied" for row in rows), f"adopted issues must be satisfied, never wanted: {[dict(r) for r in rows]}")
            require(all(row["reason"] == "library_adoption" for row in rows), rows)

            with inkdrop_state.connect(db_path) as con:
                queue_count = con.execute("select count(*) as n from queue_items where series_id = ?", (watchmen_id,)).fetchone()["n"]
            require(queue_count == 0, "adoption must never create queue/download work")

            second_apply_raised = False
            try:
                inkdrop_web.run_library_adoption_apply({
                    "root": str(root),
                    "folderPath": watchmen_candidate["folder_path"],
                    "mediaType": "comic",
                    "seriesSelection": {"existing_series_id": watchmen_id},
                    "confirmedFiles": confirmed_files,
                })
            except inkdrop_library_adoption.AdoptionAlreadyImported:
                second_apply_raised = True
            require(second_apply_raised, "re-adopting an already-linked folder must raise AdoptionAlreadyImported, not silently duplicate")

            second_plan = inkdrop_web.run_library_adoption_plan({"root": str(root), "mediaType": "comic"})
            require(second_plan["summary"]["already_imported"] == 1, f"Watchmen should be classified already_imported after adoption: {second_plan['summary']}")
        finally:
            inkdrop_web.INKDROP_STATE_DB = original_state_db
            inkdrop_web.BACKUP_DIR = original_backup_dir
            inkdrop_web.comicvine_search_volumes = original_search

    print("inkdrop-library-adoption-smoke: all checks passed")


if __name__ == "__main__":
    main()
