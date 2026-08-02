#!/usr/bin/env python3
"""Prove the Kapowarr/Mylar settings-parity batch: the two new registry keys
validate, the empty-folder sweep is safe and bounded, the pack-duplicate
quarantine wiring actually prunes, and the log export archives what it says
it archives."""
import json
import os
import tempfile
import time
import zipfile
from pathlib import Path

import inkdrop_folder_cleanup
import inkdrop_log_export
import inkdrop_settings_registry as registry
import inkdrop_state


def require(value, message):
    if not value:
        raise AssertionError(message)


def check_registry():
    for key in ("media_management.delete_empty_folders", "media_management.unmonitor_deleted_issues"):
        require(registry.is_defined_public_setting(key), f"{key} missing from registry")
        require(registry.field_schema(key) == {"kind": "boolean"}, f"{key} should be boolean")
        require(registry.validate_value(key, True) is True, f"{key} rejects true")
        try:
            registry.validate_value(key, "yes")
        except ValueError:
            pass
        else:
            raise AssertionError(f"{key} accepted a non-boolean")


def check_folder_cleanup(tmp):
    root = Path(tmp) / "library"
    nested = root / "Series (2020)" / "Extras" / "Scans"
    nested.mkdir(parents=True)
    removed_file = nested / "issue.cbz.gone"

    report = inkdrop_folder_cleanup.remove_empty_parents(removed_file, root, dry_run=True)
    require(report["ok"] and len(report["removed"]) == 3, f"dry run should report the whole chain: {report}")
    require(nested.is_dir(), "dry run must not delete anything")

    report = inkdrop_folder_cleanup.remove_empty_parents(removed_file, root)
    require(report["ok"] and len(report["removed"]) == 3, f"sweep should prune three levels: {report}")
    require(not (root / "Series (2020)").exists(), "series folder should be gone")
    require(root.is_dir(), "library root must never be removed")

    keeper_dir = root / "Kept Series" / "Volume 1"
    keeper_dir.mkdir(parents=True)
    (keeper_dir.parent / "cover.jpg.keep").write_text("art", encoding="utf-8")
    report = inkdrop_folder_cleanup.remove_empty_parents(keeper_dir / "gone.cbz.gone", root)
    require(report["removed"] == [str(keeper_dir.resolve())], f"walk should stop at the non-empty parent: {report}")
    require(keeper_dir.parent.is_dir(), "folder with remaining files must survive")

    outside = Path(tmp) / "elsewhere" / "file.cbz.gone"
    report = inkdrop_folder_cleanup.remove_empty_parents(outside, root)
    require(not report["ok"] and report["reason"] == "outside_root", f"outside paths must be refused: {report}")

    require(
        inkdrop_folder_cleanup.containing_root(keeper_dir / "x", (None, "", root)) == root.resolve(),
        "containing_root should find the owning library root",
    )
    require(
        inkdrop_folder_cleanup.containing_root(Path(tmp) / "elsewhere", (root,)) is None,
        "containing_root must not claim paths outside every root",
    )


def check_quarantine_wiring(tmp):
    import inkdrop_completed_import as importer

    library = Path(tmp) / "comics"
    series_dir = library / "Duped Series (2021)"
    series_dir.mkdir(parents=True)
    duplicate = series_dir / "Duped Series 001.cbz.smoke"
    duplicate.write_bytes(b"payload")

    original_root = importer.COMIC_ROOT
    original_setting = importer.app_setting_value
    importer.COMIC_ROOT = library
    importer.app_setting_value = lambda key, default=None: True if key == "media_management.delete_empty_folders" else default
    try:
        item = importer.quarantine_pack_duplicate(
            {"duplicate_path": str(duplicate)},
            quarantine_root=Path(tmp) / "quarantine",
            dry_run=False,
        )
    finally:
        importer.COMIC_ROOT = original_root
        importer.app_setting_value = original_setting
    require(item.get("action") == "quarantined", f"quarantine should proceed: {item}")
    require(item.get("removed_empty_folders") == [str(series_dir.resolve())], f"emptied series folder should be swept: {item}")
    require(library.is_dir(), "comic root must survive the sweep")


def check_log_export(tmp):
    log_dir = Path(tmp) / "logs"
    log_dir.mkdir()
    (log_dir / "inkdrop-import.log").write_bytes(b"a" * 10000)
    (log_dir / "inkdrop-acquire.log.1").write_bytes(b"b" * 200)
    (log_dir / "not-a-log.txt").write_text("ignored", encoding="utf-8")
    # The export takes newest first, so which log gets the whole budget under
    # the total cap depends on modification order. Two back-to-back writes can
    # land either side of a second boundary, which is how this check went red
    # on CI while passing everywhere else. State the order instead.
    newest = time.time()
    os.utime(log_dir / "inkdrop-import.log", (newest, newest))
    os.utime(log_dir / "inkdrop-acquire.log.1", (newest - 60, newest - 60))

    payload, manifest = inkdrop_log_export.build_log_archive_bytes(log_dir=log_dir, per_file_cap_bytes=4096)
    with zipfile.ZipFile(__import__("io").BytesIO(payload)) as archive:
        names = set(archive.namelist())
        require(names == {"logs/inkdrop-import.log", "logs/inkdrop-acquire.log.1", "manifest.json"}, f"unexpected archive contents: {names}")
        embedded = json.loads(archive.read("manifest.json"))
    require(embedded["schema"] == "inkdrop.log_export.v1", "manifest schema missing")
    by_name = {row["name"]: row for row in manifest["files"]}
    require(by_name["inkdrop-import.log"]["included_bytes"] == 4096 and by_name["inkdrop-import.log"]["truncated"], f"big log should be tail-capped: {by_name}")
    require(by_name["inkdrop-acquire.log.1"]["included_bytes"] == 200 and not by_name["inkdrop-acquire.log.1"]["truncated"], f"small log should be whole: {by_name}")

    _, tight = inkdrop_log_export.build_log_archive_bytes(log_dir=log_dir, per_file_cap_bytes=4096, total_cap_bytes=4096)
    require(len(tight["files"]) == 1 and len(tight["skipped"]) == 1, f"total cap should drop the older file into skipped: {tight}")
    require(inkdrop_log_export.log_archive_filename(0).startswith("inkdrop-logs-"), "filename prefix changed")


def check_unmonitor_deleted_issues(tmp):
    db_path = Path(tmp) / "unmonitor-state.sqlite3"
    # Use the real default comic root rather than a temp dir so this test
    # doesn't need to override media_management.comic_root (not a
    # pre-seeded app_settings row, so update_app_setting() would reject it
    # sight unseen) -- the file just needs to not exist on disk, which a
    # nonexistent path under the default root already guarantees.
    dest_path = str(Path(inkdrop_state.DEFAULT_LIBRARY_ROOTS["comic"]) / "Gone Series (2020)" / "Gone Series 001.cbz")

    with inkdrop_state.connect(db_path) as con:
        inkdrop_state.init_schema(con)
        con.execute(
            "insert into series(id,title,media_type,metadata_provider,metadata_id,source,monitored,monitor_new,auto_grab,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("series-gone", "Gone Series", "comic", "comicvine", "1", "comicvine", 1, 1, 1, 1.0, 1.0, "{}"),
        )
        con.execute(
            "insert into issues(id,series_id,issue_number,normalized_number,title,release_date,metadata_provider,metadata_id,monitored,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("issue-gone", "series-gone", "1", "001", "Issue 1", "2020-01-01", "comicvine", "cv-1", 1, 1.0, 1.0, "{}"),
        )
        con.execute(
            "insert into queue_items(id,series_id,issue_id,state,active,created_at,updated_at,raw_json) values(?,?,?,?,?,?,?,?)",
            ("queue-gone", "series-gone", "issue-gone", "verified", 0, 1.0, 1.0, "{}"),
        )
        con.execute(
            "insert into import_results(id,queue_id,series_id,issue_id,source_path,dest_path,status,verified,created_at,raw_json) values(?,?,?,?,?,?,?,?,?,?)",
            ("import-gone", "queue-gone", "series-gone", "issue-gone", "/staging/gone.cbz", dest_path, "verification_pending", 0, 1.0, "{}"),
        )
        con.commit()

    # Setting off (default): missing file goes to retry_later, issue stays monitored --
    # today's existing behavior, unchanged.
    with inkdrop_state.connect(db_path) as con:
        updated = inkdrop_state.backfill_queue_verified_pending_scan_import_results(con, now=2.0)
        con.commit()
        row = con.execute("select status, display_phase from import_results where id='import-gone'").fetchone()
        issue_row = con.execute("select monitored from issues where id='issue-gone'").fetchone()
    require(updated == 1, f"expected 1 row updated, got {updated}")
    require(row["status"] == "missing_file", f"expected missing_file, got {row['status']}")
    require(row["display_phase"] == "retry_later", f"setting off should keep retry_later, got {row['display_phase']}")
    require(issue_row["monitored"] == 1, "setting off must not touch monitored")

    # Reset back to a scannable state and turn the setting on.
    with inkdrop_state.connect(db_path) as con:
        con.execute(
            "update import_results set status='verification_pending', verified=0, display_phase='', raw_json='{}' where id='import-gone'"
        )
        con.commit()
    with inkdrop_state.connect(db_path) as con:
        con.execute(
            "insert into app_settings(key,scope,value_json,source,updated_at) values(?,?,?,?,?) "
            "on conflict(key) do update set value_json=excluded.value_json, source=excluded.source",
            ("media_management.unmonitor_deleted_issues", "media_management", "true", "user", 2.5),
        )
        con.commit()
    with inkdrop_state.connect(db_path) as con:
        updated = inkdrop_state.backfill_queue_verified_pending_scan_import_results(con, now=3.0)
        con.commit()
        row = con.execute("select status, display_phase from import_results where id='import-gone'").fetchone()
        issue_row = con.execute("select monitored, monitored_user_override from issues where id='issue-gone'").fetchone()
    require(updated == 1, f"expected 1 row updated on the second pass, got {updated}")
    require(row["status"] == "missing_file", f"expected missing_file, got {row['status']}")
    require(row["display_phase"] == "", f"setting on should skip retry_later, got {row['display_phase']!r}")
    require(issue_row["monitored"] == 0, "setting on should unmonitor the issue whose file disappeared")
    require(issue_row["monitored_user_override"] == 0, "the unmonitor should be recorded as an explicit override so a later metadata sync doesn't silently re-enable it")


def main():
    check_registry()
    with tempfile.TemporaryDirectory(prefix="inkdrop-parity-", ignore_cleanup_errors=True) as tmp:
        check_folder_cleanup(tmp)
        check_quarantine_wiring(tmp)
        check_log_export(tmp)
        check_unmonitor_deleted_issues(tmp)
    print(json.dumps({"ok": True, "settings_parity_batch_smoke": "passed"}, indent=2))


if __name__ == "__main__":
    main()
