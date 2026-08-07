#!/usr/bin/env python3
import importlib.util
import json
import sys
import tempfile
import time
import types
from pathlib import Path

from core import inkdrop_state


ROOT = Path(__file__).resolve().parents[1]
WEB_PATH = ROOT / "core" / "inkdrop_web.py"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def load_web_module():
    if "requests" not in sys.modules:
        try:
            import requests  # noqa: F401
        except ModuleNotFoundError:
            class RequestException(Exception):
                pass

            class Timeout(RequestException):
                pass

            class ConnectionError(RequestException):
                pass

            class HTTPError(RequestException):
                pass

            sys.modules["requests"] = types.SimpleNamespace(
                exceptions=types.SimpleNamespace(
                    RequestException=RequestException,
                    Timeout=Timeout,
                    ConnectionError=ConnectionError,
                    HTTPError=HTTPError,
                )
            )
    spec = importlib.util.spec_from_file_location("inkdrop_web_remove_files_smoke", WEB_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def set_app_setting(con, key, value):
    con.execute(
        """
        insert or replace into app_settings(key, scope, label, value_json, description, source, updated_at)
        values(?,?,?,?,?,?,?)
        """,
        (key, "paths", key, json.dumps(value), "", "smoke", time.time()),
    )


def add_series(con, *, comicvine_id, title, library_path, root):
    now = time.time()
    inkdrop_state.upsert_series(
        con,
        {
            "comicvineId": comicvine_id,
            "name": title,
            "metadataProvider": "comicvine",
            "mediaType": "comic",
            "libraryRoot": str(root),
            "libraryPath": str(library_path),
            "libraryPathSource": "smoke",
            "enabled": True,
            "monitorNew": True,
            "autoGrab": True,
        },
        now,
    )
    return f"comicvine:{comicvine_id}"


def main():
    web_text = WEB_PATH.read_text(encoding="utf-8")
    require('id="seriesRemoveDeleteFiles" type="checkbox"' in web_text, "remove-files checkbox is missing from the modal")
    require('id="seriesRemoveKeepFiles" type="hidden" value="true"' in web_text, "keep-files compatibility field is missing")
    require("Files stay on disk unless this is enabled." in web_text, "remove-files modal copy is not explicit")
    require('deleteFiles = $("seriesRemoveDeleteFiles")?.checked === true' in web_text, "remove-files modal does not drive deleteFiles")

    web = load_web_module()
    with tempfile.TemporaryDirectory(prefix="inkdrop-remove-files-smoke-") as tmp:
        tmp_path = Path(tmp)
        comic_root = tmp_path / "Comics"
        manga_root = tmp_path / "Manga"
        state_dir = tmp_path / "state"
        comic_root.mkdir()
        manga_root.mkdir()
        state_dir.mkdir()

        db_path = state_dir / "inkdrop-state.sqlite3"
        with inkdrop_state.connect(db_path) as con:
            inkdrop_state.init_schema(con)
            for key, value in (
                ("path.comic_root", str(comic_root)),
                ("path.manga_root", str(manga_root)),
                ("media_management.comic_root", str(comic_root)),
                ("media_management.manga_root", str(manga_root)),
            ):
                set_app_setting(con, key, value)

            keep_dir = comic_root / "Keep Files Series"
            remove_dir = comic_root / "Remove Files Series"
            outside_dir = tmp_path / "Outside Series"
            for folder in (keep_dir, remove_dir, outside_dir):
                folder.mkdir()
                (folder / "Issue 001.cbz").write_text("synthetic", encoding="utf-8")

            keep_id = add_series(con, comicvine_id=910001, title="Keep Files Series", library_path=keep_dir, root=comic_root)
            remove_id = add_series(con, comicvine_id=910002, title="Remove Files Series", library_path=remove_dir, root=comic_root)
            outside_id = add_series(con, comicvine_id=910003, title="Outside Series", library_path=outside_dir, root=outside_dir.parent)

        web.INKDROP_STATE_DB = db_path
        web.STATE_DIR = state_dir
        web.WATCH_LOG = state_dir / "kavita-watch.log"
        web.COMIC_ROOT = comic_root
        web.MANGA_ROOT = manga_root

        keep_result = web.remove_inkdrop_series({"id": keep_id})
        require(keep_result["filesDeleted"] is False, "default remove should not delete files")
        require(keep_dir.exists(), "default remove deleted the series folder")
        keep_row = inkdrop_state.series_item(db_path, keep_id)
        require(keep_row and keep_row["removed_by_user"], "default remove did not park the series")

        remove_result = web.remove_inkdrop_series({"id": remove_id, "deleteFiles": True, "keepFiles": False})
        require(remove_result["filesDeleted"] is True, "delete-files remove did not report deleted files")
        require(remove_result["fileRemoval"]["reason"] == "series_library_removed", "delete-files remove had the wrong reason")
        require(not remove_dir.exists(), "delete-files remove left the series folder on disk")
        remove_row = inkdrop_state.series_item(db_path, remove_id)
        require(remove_row and remove_row["removed_by_user"], "delete-files remove did not park the series")

        try:
            web.remove_inkdrop_series({"id": outside_id, "deleteFiles": True, "keepFiles": False})
        except ValueError as exc:
            require("outside configured comic/manga roots" in str(exc), "outside-root rejection used the wrong message")
        else:
            raise AssertionError("outside-root series removal should have been rejected")
        require(outside_dir.exists(), "outside-root rejection deleted files")
        outside_row = inkdrop_state.series_item(db_path, outside_id)
        require(outside_row and outside_row["monitored"], "outside-root rejection parked the series")

    print("inkdrop-series-remove-files-smoke ok")


if __name__ == "__main__":
    main()
