#!/usr/bin/env python3
"""Prove /api/inkdrop-library/convert-archives/* is wired end to end.

inkdrop_archive_conversion.py (scan_library/convert_archive/convert_library) already has
its own 20-check smoke test proving the conversion logic itself. This test is narrower:
it proves inkdrop_web.py's wrapper functions -- run_archive_conversion_plan_start,
run_archive_conversion_apply_start, archive_conversion_task_status -- actually reach that
module, that both the plan and apply routes' background-thread task lifecycles
(running -> completed) are observable through polling, and that a second apply call
while one is already running is refused rather than double-converting the same files.
"""

import os
import tempfile
import time
import zipfile
from pathlib import Path

import inkdrop_web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100fdff03fa0000"
    "000049454e44ae426082"
)


def write_zip_named_cbr(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("page1.png", PIXEL + b"page-1")
        archive.writestr("page2.png", PIXEL + b"page-2")
    return path


def write_fake_rar_named_cbz(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Rar!\x1a\x07\x00" + b"not a real archive body")
    return path


def wait_for_task(task_id, timeout_seconds=15):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        task = inkdrop_web.archive_conversion_task_status(task_id)
        require(task is not None, f"task {task_id} disappeared while polling")
        if task["state"] != "running":
            return task
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} never left running state within {timeout_seconds}s")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        comic_root = tmp_path / "comics"
        manga_root = tmp_path / "manga-empty"
        manga_root.mkdir(parents=True, exist_ok=True)
        cbr_path = write_zip_named_cbr(comic_root / "Berserk" / "Berserk v03.cbr")
        mislabeled_path = write_fake_rar_named_cbz(comic_root / "Sniff" / "b.cbz")

        quarantine_dir = tmp_path / "quarantine"

        original_comic_root = os.environ.get("INKDROP_COMIC_ROOT")
        original_manga_root = os.environ.get("INKDROP_MANGA_ROOT")
        original_quarantine_dir = os.environ.get("INKDROP_QUARANTINE_DIR")
        os.environ["INKDROP_COMIC_ROOT"] = str(comic_root)
        os.environ["INKDROP_MANGA_ROOT"] = str(manga_root)
        # convert_library's default originals_dir lives under the shared state dir, not
        # this test's own tempdir -- on a runner where state persists across CI jobs, a
        # leftover file from an earlier run collides on "original_archive_slot_taken"
        # before this test's own conversion ever runs. Point it at an isolated tempdir,
        # the same way inkdrop-archive-conversion-smoke.py always does explicitly.
        os.environ["INKDROP_QUARANTINE_DIR"] = str(quarantine_dir)
        try:
            plan_start = inkdrop_web.run_archive_conversion_plan_start()
            require(plan_start["started"] is True, plan_start)
            require(plan_start["already_running"] is False, plan_start)
            plan_task = wait_for_task(plan_start["taskId"])
            require(plan_task["state"] == "completed", f"plan task should complete cleanly: {plan_task}")
            require(plan_task["scanned"] >= 2, f"scan progress should have advanced past both fixture files: {plan_task}")
            require(plan_task["scan_total"] >= 2, f"scan progress should report a real denominator, not just a running count: {plan_task}")
            require(plan_task["current_path"] is None, f"a completed task's current_path should be cleared, not left pointing at the last file examined: {plan_task}")
            plan = plan_task["plan"]
            require(plan["convertible_count"] == 1, f"plan should find exactly the one zip-named CBR (mislabeled .cbz excluded by default): {plan}")
            require(plan["needs_rar_tooling"] == 0, f"the fixture is zip-named, not true RAR, so it should need no tooling: {plan}")

            # The "Also fix .cbz files that are really RAR" checkbox was wired into
            # /apply but never into /plan -- checking it and clicking "Check my
            # library" always reported the same convertible_count as if it were off,
            # which left "Convert them" disabled even though the user asked to
            # include those files. Prove the plan route now honors the flag too.
            opted_in_start = inkdrop_web.run_archive_conversion_plan_start({"includeMislabeledCbz": True})
            require(opted_in_start["started"] is True, opted_in_start)
            opted_in_task = wait_for_task(opted_in_start["taskId"])
            require(opted_in_task["state"] == "completed", f"opted-in plan task should complete cleanly: {opted_in_task}")
            opted_in_plan = opted_in_task["plan"]
            require(
                opted_in_plan["convertible_count"] == 2,
                f"plan should count the mislabeled .cbz as convertible once the checkbox is honored: {opted_in_plan}",
            )
            # convert_library only ever added "blocked_on_rar_tooling" to its summary
            # on a real (non-dry-run) refusal -- a dry-run /plan check always came back
            # without the key at all, so the frontend's
            # `convertButton.disabled = ... || lastPlan.blocked_on_rar_tooling` read
            # `undefined` and never disabled "Convert them", even when the check step's
            # own status text said the files couldn't be converted yet. Prove the key is
            # present (and correct) on a dry-run plan, not only on a blocked real run.
            require(
                "blocked_on_rar_tooling" in opted_in_plan,
                f"a dry-run plan must report blocked_on_rar_tooling so the UI can disable Convert them: {opted_in_plan}",
            )
            # This fixture's "mislabeled" file is only RAR-magic-byte garbage, not a
            # real archive, but classification (and therefore tooling need) happens
            # before any attempt to actually read the archive's contents.
            expected_blocked = opted_in_plan["needs_rar_tooling"] > 0 and not opted_in_plan["rar_tooling"]["available"]
            require(
                opted_in_plan["blocked_on_rar_tooling"] == expected_blocked,
                f"blocked_on_rar_tooling should reflect needs_rar_tooling vs available tooling: {opted_in_plan}",
            )

            preexisting_task_id = "wiring-smoke-fake-running-task"
            with inkdrop_web.ARCHIVE_CONVERSION_TASKS_LOCK:
                inkdrop_web.ARCHIVE_CONVERSION_TASKS[preexisting_task_id] = {
                    "task_id": preexisting_task_id,
                    "state": "running",
                    "started_at": time.time(),
                    "finished_at": None,
                    "attempted": 0,
                    "converted": 0,
                    "failed": 0,
                    "last_result": None,
                    "summary": None,
                    "error": None,
                }
            try:
                blocked_start = inkdrop_web.run_archive_conversion_apply_start({})
                require(blocked_start["started"] is False, f"apply must refuse a second concurrent run: {blocked_start}")
                require(blocked_start["already_running"] is True, blocked_start)
                require(blocked_start["taskId"] == preexisting_task_id, blocked_start)
            finally:
                with inkdrop_web.ARCHIVE_CONVERSION_TASKS_LOCK:
                    inkdrop_web.ARCHIVE_CONVERSION_TASKS.pop(preexisting_task_id, None)

            start = inkdrop_web.run_archive_conversion_apply_start({})
            require(start["started"] is True, start)
            require(start["already_running"] is False, start)
            task_id = start["taskId"]

            finished = wait_for_task(task_id)
            require(finished["state"] == "completed", f"conversion task should complete cleanly: {finished}")
            summary = finished["summary"]
            require(summary["converted"] == 1, f"exactly one file should convert: {summary}")
            require(summary["failed"] == 0, summary)

            cbz_path = comic_root / "Berserk" / "Berserk v03.cbz"
            require(cbz_path.is_file(), "converted CBZ should exist next to where the CBR was")
            require(not cbr_path.exists(), "original CBR should no longer sit in the library folder")
            with zipfile.ZipFile(cbz_path) as archive:
                require(len(archive.namelist()) == 2, "both pages should have carried across")

            require(inkdrop_web.archive_conversion_task_status(None) is None, "a missing task_id must return None, not raise")
            require(inkdrop_web.archive_conversion_task_status("does-not-exist") is None, "an unknown task_id must return None")
        finally:
            if original_comic_root is None:
                os.environ.pop("INKDROP_COMIC_ROOT", None)
            else:
                os.environ["INKDROP_COMIC_ROOT"] = original_comic_root
            if original_manga_root is None:
                os.environ.pop("INKDROP_MANGA_ROOT", None)
            else:
                os.environ["INKDROP_MANGA_ROOT"] = original_manga_root
            if original_quarantine_dir is None:
                os.environ.pop("INKDROP_QUARANTINE_DIR", None)
            else:
                os.environ["INKDROP_QUARANTINE_DIR"] = original_quarantine_dir
            with inkdrop_web.ARCHIVE_CONVERSION_TASKS_LOCK:
                inkdrop_web.ARCHIVE_CONVERSION_TASKS.clear()

    print("inkdrop-archive-conversion-wiring-smoke: all checks passed")


if __name__ == "__main__":
    main()
