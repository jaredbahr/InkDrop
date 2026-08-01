#!/usr/bin/env python3
import json
import sqlite3
import tempfile
import time
from pathlib import Path

import inkdrop_state


def insert_setting(con, key, value):
    con.execute(
        """
        insert into app_settings(key, scope, label, value_json, source, updated_at)
        values(?,?,?,?,?,?)
        on conflict(key) do update set value_json=excluded.value_json, updated_at=excluded.updated_at
        """,
        (key, "media_management", key, json.dumps(value), "smoke", time.time()),
    )


def insert_series(con, series_id, title, media_type, library_path=""):
    now = time.time()
    con.execute(
        """
        insert into series(
            id, title, media_type, metadata_provider, metadata_id, source,
            library_path, library_path_source, monitored, monitor_new, auto_grab,
            created_at, updated_at
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            series_id,
            title,
            media_type,
            "comicvine",
            series_id,
            "inkdrop",
            str(library_path or ""),
            "user" if library_path else "",
            1,
            1,
            1,
            now,
            now,
        ),
    )


def touch_archive(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(path.name.encode("utf-8"))


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-library-import-plan-") as tmp:
        root = Path(tmp)
        manga_root = root / "Manga"
        comic_root = root / "Comics"
        exact_folder = manga_root / "Existing Series"
        attach_folder = manga_root / "Attach Me"
        new_folder = comic_root / "New Comic"
        loose_file = manga_root / "Loose Volume v01.cbz"
        touch_archive(exact_folder / "Existing Series v01.cbz")
        touch_archive(attach_folder / "Attach Me v01.cbz")
        touch_archive(new_folder / "New Comic 001.cbz")
        touch_archive(loose_file)

        db_path = root / "inkdrop-state.sqlite3"
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            insert_setting(con, "media_management.manga_root", str(manga_root))
            insert_setting(con, "media_management.comic_root", str(comic_root))
            insert_series(con, "mangadex:existing", "Existing Series", "manga", exact_folder)
            insert_series(con, "mangadex:attach", "Attach Me", "manga")
            before_count = con.execute("select count(*) from series").fetchone()[0]
            plan = inkdrop_state.managed_library_import_plan_from_connection(
                con,
                max_files=20,
                sample_limit=5,
            )
            after_count = con.execute("select count(*) from series").fetchone()[0]

        by_folder = {item["folder_name"]: item for item in plan["candidates"]}
        statuses = {item["status"] for item in plan["candidates"]}
        assert plan["ok"] is True, plan
        assert before_count == after_count == 2, plan
        assert by_folder["Existing Series"]["status"] == "mapped_existing_series", by_folder["Existing Series"]
        assert by_folder["Existing Series"]["action"] == "already_imported", by_folder["Existing Series"]
        assert by_folder["Attach Me"]["status"] == "existing_series_folder_candidate", by_folder["Attach Me"]
        assert by_folder["Attach Me"]["action"] == "attach_existing_series", by_folder["Attach Me"]
        assert by_folder["New Comic"]["status"] == "new_series_candidate", by_folder["New Comic"]
        assert by_folder["New Comic"]["action"] == "add_series_from_folder", by_folder["New Comic"]
        assert by_folder["Loose Volume v01"]["status"] == "root_file_requires_series_folder", by_folder["Loose Volume v01"]
        assert by_folder["Loose Volume v01"]["eligible"] is False, by_folder["Loose Volume v01"]
        assert plan["summary"]["mapped_existing_series"] == 1, plan["summary"]
        assert plan["summary"]["existing_series_folder_candidates"] == 1, plan["summary"]
        assert plan["summary"]["new_series_candidates"] == 1, plan["summary"]
        assert plan["summary"]["loose_root_files"] == 1, plan["summary"]
        assert statuses == {
            "mapped_existing_series",
            "existing_series_folder_candidate",
            "new_series_candidate",
            "root_file_requires_series_folder",
        }, statuses
        print("LIBRARY_IMPORT_PLAN_OK: read-only folder adoption candidates are classified")


if __name__ == "__main__":
    main()
