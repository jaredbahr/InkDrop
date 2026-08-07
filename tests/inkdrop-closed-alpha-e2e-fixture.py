#!/usr/bin/env python3
"""Disposable normal-contract acquisition → import → reader proof."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

from core import inkdrop_library_frontends
from core import inkdrop_source_catalog as catalog
from core import inkdrop_source_worker_scheduler as scheduler
from core import inkdrop_source_worker_service as service
from core import inkdrop_state


NOW = 1_234_567.0
QUEUE_ID = "queue:closed-alpha-e2e"
SERIES_ID = "series:closed-alpha-e2e"
ISSUE_ID = "issue:closed-alpha-e2e:1"
WANTED_ID = "wanted:closed-alpha-e2e:1"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def archive_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        for page in range(1, 13):
            image = Image.new("RGB", (600, 600), (page * 17 % 255, 64, 128))
            payload = io.BytesIO()
            image.save(payload, format="PNG", compress_level=0)
            archive.writestr(f"{page:03d}.png", payload.getvalue())
    return output.getvalue()


def seed(db_path, root):
    global QUEUE_ID, SERIES_ID, ISSUE_ID, WANTED_ID
    seed_payload = catalog.settings_seed_payload()
    inkdrop_state.sync_settings(
        db_path,
        providers=seed_payload["providers"],
        settings=[
            *seed_payload["settings"],
            {"key": "path.comic_root", "value": str(root / "library" / "comics"), "source": "user"},
            {"key": "path.kavita_comic_root", "value": "/data/comics", "source": "user"},
            {"key": "media_management.library_visibility_required", "value": True, "source": "user"},
            {"key": "media_management.library_visibility_checks_enabled", "value": True, "source": "user"},
        ],
    )
    entry = next(row for row in catalog.provider_candidates() if row.get("id") == "rss_getcomics")
    policy = dict(entry.get("policy") or {})
    policy["requires_manual_confirm"] = False
    inkdrop_state.update_provider_config(
        db_path,
        "rss_getcomics",
        {
            "enabled": True,
            "base_url": "https://getcomics.org/feed/",
            "settings": {
                "implementation_status": "implemented",
                "source_mode": "auto",
                "auto_download_allowed": True,
                "requires_manual_confirm": False,
                "policy": policy,
            },
        },
    )
    added = inkdrop_state.record_series_added(
        db_path,
        {
            "name": "Fixture Comic",
            "title": "Fixture Comic",
            "media_type": "comic",
            "metadata_provider": "fixture",
            "metadata_id": "closed-alpha-e2e",
            "year": 2026,
            "publisher": "Fixture Press",
            "libraryPath": str(root / "library" / "comics" / "Fixture Comic"),
        },
        auto_grab=True,
        source="closed_alpha_fixture",
        watch={
            "name": "Fixture Comic",
            "title": "Fixture Comic",
            "media_type": "comic",
            "metadata_provider": "fixture",
            "metadata_id": "closed-alpha-e2e",
            "autoGrab": True,
            "missingIssues": [{
                "issueNumber": "1",
                "title": "Fixture Issue",
                "status": "missing",
                "metadata_provider": "fixture",
                "metadata_id": "closed-alpha-e2e-issue-1",
            }],
        },
        include_summary=False,
    )
    require(added.get("ok") and added.get("wanted_recorded") == 1, added)
    require(added.get("queue_recorded") == 1, added)
    with inkdrop_state.connect_read(db_path) as con:
        series = con.execute("select id from series where title='Fixture Comic'").fetchone()
        issue = con.execute("select id from issues where series_id=? and issue_number='1'", (series["id"],)).fetchone()
        wanted = con.execute("select id from wanted_items where series_id=? and issue_id=?", (series["id"], issue["id"])).fetchone()
        queue = con.execute("select id from queue_items where series_id=? and issue_id=?", (series["id"], issue["id"])).fetchone()
    require(series and issue and wanted and queue, "series addition did not create the normal wanted/queue chain")
    SERIES_ID, ISSUE_ID, WANTED_ID, QUEUE_ID = series["id"], issue["id"], wanted["id"], queue["id"]

    automatic_plan = scheduler.source_worker_queue_plan(
        db_path,
        limit=10,
        due_only=True,
        include_operator=False,
        provider_ids=["rss_getcomics"],
        now=max(NOW, __import__("time").time()) + 5,
    )
    selected = [row for row in automatic_plan.get("plans") or [] if row.get("queue_id") == QUEUE_ID]
    require(automatic_plan.get("ok") and len(selected) == 1, automatic_plan)
    require(selected[0].get("status") == "eligible", selected[0])
    require("rss_getcomics" in (selected[0].get("selected_provider_ids") or []), selected[0])


def run_acquisition(db_path, root):
    body = archive_bytes()

    def source_get(request):
        url = request.get("url")
        if url in {"https://getcomics.org/feed", "https://getcomics.org/feed/"}:
            return {
                "text": """<rss><channel><item><title>Fixture Comic Issue 001</title>
                <link>https://getcomics.org/fixture-book-001/</link><guid>fixture-001</guid>
                </item></channel></rss>""",
                "headers": {"Content-Type": "application/rss+xml"},
            }
        if url == "https://getcomics.org/fixture-book-001/":
            return {
                "text": '<html><a href="https://pixeldrain.com/u/closedalpha001">Download</a></html>',
                "headers": {"Content-Type": "text/html"},
            }
        if url == "https://pixeldrain.com/api/file/closedalpha001?download":
            return {
                "status_code": 200,
                "headers": {
                    "Content-Type": "application/zip",
                    "Content-Disposition": 'attachment; filename="Fixture Comic Issue 001.cbz"',
                    "Content-Length": str(len(body)),
                },
            }
        raise AssertionError(f"unexpected provider request: {request}")

    def direct_get(request):
        require(request.get("url") == "https://pixeldrain.com/api/file/closedalpha001?download", request)
        return {
            "body": body,
            "headers": {"Content-Type": "application/zip", "Content-Length": str(len(body))},
            "status_code": 200,
            "final_url": request["url"],
            "redirect_count": 0,
        }

    environ = {
        "INKDROP_SOURCE_WORKER_QUEUE_IDS": QUEUE_ID,
        "INKDROP_SOURCE_WORKER_PROVIDER_IDS": "rss_getcomics",
        "INKDROP_SOURCE_WORKER_EXECUTE": "1",
        "INKDROP_SOURCE_WORKER_WRITE": "1",
        "INKDROP_SOURCE_WORKER_STAGE_DIRECT": "1",
        "INKDROP_SOURCE_WORKER_ALLOW_NETWORK": "1",
        "INKDROP_SOURCE_WORKER_ALLOW_DIRECT_NETWORK": "1",
        "INKDROP_SOURCE_WORKER_ALLOWED_HOSTS": "getcomics.org,www.getcomics.org,pixeldrain.com,www.pixeldrain.com",
        "INKDROP_SOURCE_WORKER_DIRECT_ALLOWED_HOSTS": "pixeldrain.com,www.pixeldrain.com",
        "INKDROP_SOURCE_WORKER_STAGING_ROOT": str(root / "staging"),
        "INKDROP_SOURCE_WORKER_NOW": str(NOW),
    }
    result = service.run_source_worker_service(
        ["--db-path", str(db_path)],
        environ=environ,
        source_http_get=source_get,
        direct_http_get=direct_get,
    )
    if not result.get("ok"):
        with inkdrop_state.connect_read(db_path) as con:
            failed_tasks = [dict(row) for row in con.execute("select * from download_tasks where queue_id=?", (QUEUE_ID,)).fetchall()]
        raise AssertionError({"result": result, "download_tasks": failed_tasks})
    require(result["result"]["summary"]["direct_tasks_staged"] == 1, result)
    replay = service.run_source_worker_service(
        ["--db-path", str(db_path)],
        environ=environ,
        source_http_get=source_get,
        direct_http_get=direct_get,
    )
    require(replay.get("ok"), replay)
    with inkdrop_state.connect_read(db_path) as con:
        attempts = con.execute("select count(*) from source_attempts where queue_id=?", (QUEUE_ID,)).fetchone()[0]
        tasks = con.execute("select * from download_tasks where queue_id=?", (QUEUE_ID,)).fetchall()
    require(attempts >= 1, "provider candidate/acceptance was not recorded")
    require(len(tasks) == 1, "forced replay created a duplicate transfer task")
    require(tasks[0]["state"] == "import_ready", dict(tasks[0]))
    require(Path(tasks[0]["local_path"]).is_file(), dict(tasks[0]))
    return Path(tasks[0]["local_path"])


def run_import(db_path, source_path, root):
    state = root / "state"
    library = root / "library"
    env = dict(os.environ)
    env.update({
        "INKDROP_CONFIG_DIR": str(root / "config"),
        "INKDROP_STATE_DIR": str(state),
        "INKDROP_STATE_DB": str(db_path),
        "INKDROP_STAGING_DIR": str(root / "staging"),
        "INKDROP_COMIC_ROOT": str(library / "comics"),
        "INKDROP_MANGA_ROOT": str(library / "manga"),
        "INKDROP_MANUAL_INBOX_DIR": str(root / "manual-inbox"),
        "INKDROP_KAVITA_URL": "",
    })
    for path in (
        root / "config", state, state / "logs", state / "cache", state / "backups",
        state / "locks", state / "quarantine", library / "comics", library / "manga",
        root / "manual-inbox",
    ):
        path.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-B", "core/inkdrop_completed_import.py", "--kind", "comics",
        "--source-file", str(source_path), "--trusted-series-id", SERIES_ID,
        "--trusted-issue", "1", "--trusted-issue-id", ISSUE_ID,
        "--all-series", "--apply-planned-path",
        "--no-wait-for-library-scan", "--ignore-cutoff", "--min-age-seconds", "0",
    ]
    with inkdrop_state.connect_read(db_path) as con:
        task = con.execute(
            """
            select id,source_attempt_id,external_id,candidate_identity,download_client,local_path
              from download_tasks where queue_id=? limit 1
            """,
            (QUEUE_ID,),
        ).fetchone()
    require(task is not None, "staged fixture task is missing")
    authority_claim = inkdrop_state.claim_import_authority(
        db_path,
        QUEUE_ID,
        task["id"],
        source_attempt_id=task["source_attempt_id"],
        external_id=task["external_id"],
        candidate_identity=task["candidate_identity"],
        download_client=task["download_client"],
        local_path=task["local_path"],
        claimed_at=NOW + 20,
    )
    require(authority_claim.get("ok"), authority_claim)
    completed = subprocess.run(command, env=env, text=True, capture_output=True, timeout=90)
    require(completed.returncode == 0, completed.stdout + completed.stderr)
    require((state / "import-status.json").is_file(), completed.stdout + completed.stderr)
    status = json.loads((state / "import-status.json").read_text(encoding="utf-8"))
    imported = status.get("imported") or []
    require(len(imported) == 1, status)
    dest = Path(imported[0]["dest"])
    require(dest.is_file(), imported[0])
    recorded = inkdrop_state.record_direct_import_result(
        db_path,
        QUEUE_ID,
        source_path=str(source_path),
        dest_path=str(dest),
        source="rss_getcomics",
        status="verification_pending",
        verified=False,
        imported_count=1,
        raw=imported[0],
        import_authority=authority_claim.get("authority"),
        created_at=NOW + 30,
    )
    require(recorded.get("ok"), recorded)
    return dest


def prove_reader(db_path, dest, root):
    scan_calls = []
    scan = inkdrop_library_frontends.sync_library_frontends(
        [str(dest.parent)],
        trigger_kavita_scan=lambda folder, **_kwargs: scan_calls.append(folder) or {"folder": folder, "status_code": 200, "mode": "fixture_scan"},
        load_komga_settings=lambda: {"enabled": False},
    )
    require(scan["requested"] == 1 and scan_calls == [str(dest.parent)], scan)
    pending = inkdrop_library_frontends.check_library_visibility(
        str(dest),
        kavita_enabled=True,
        check_kavita_visibility=lambda _path: {"visible": False, "status": "not_visible"},
        komga_enabled=False,
    )
    require(not pending["library_visible"], "scan request alone was treated as visibility")

    kavita_db = root / "kavita.sqlite3"
    con = sqlite3.connect(kavita_db)
    con.executescript(
        """
        create table Series(Id integer primary key, Name text);
        create table Volume(Id integer primary key, SeriesId integer);
        create table Chapter(Id integer primary key, VolumeId integer, Number text, Range text, Title text);
        create table MangaFile(Id integer primary key, ChapterId integer, FilePath text, FileName text, Pages integer, Bytes integer);
        """
    )
    con.execute("insert into Series values(1, 'Fixture Comic')")
    con.execute("insert into Volume values(1, 1)")
    con.execute("insert into Chapter values(1, 1, '1', '1', 'Fixture Issue')")
    kavita_path = "/data/comics/" + dest.relative_to(root / "library" / "comics").as_posix()
    con.execute("insert into MangaFile values(1, 1, ?, ?, 1, ?)", (kavita_path, dest.name, dest.stat().st_size))
    con.commit()
    con.close()
    inkdrop_state.sync_settings(db_path, settings=[
        {"key": "path.comic_root", "value": str(root / "library" / "comics"), "source": "user"},
        {"key": "path.kavita_comic_root", "value": "/data/comics", "source": "user"},
    ])
    inkdrop_state.sync_settings(db_path, providers=[{
        "id": "kavita",
        "provider_type": "reader",
        "display_name": "Kavita",
        "enabled": True,
        "settings": {"implementation_status": "implemented", "db_path": str(kavita_db)},
        "source": "user",
    }])
    visible = inkdrop_state.kavita_file_visible_for_import_path(dest, db_path=db_path)
    require(visible and visible["file_path"] == kavita_path, visible)
    with inkdrop_state.connect(db_path) as state_con:
        verification = inkdrop_state.verify_pending_direct_import_results(state_con, db_path, NOW + 60)
        state_con.commit()
    if verification["verified"] != 1:
        with inkdrop_state.connect_read(db_path) as state_con:
            rows = [dict(row) for row in state_con.execute("select * from import_results").fetchall()]
        raise AssertionError({"verification": verification, "import_results": rows})
    with inkdrop_state.connect_read(db_path) as state_con:
        queue = state_con.execute("select state,active from queue_items where id=?", (QUEUE_ID,)).fetchone()
        wanted = state_con.execute("select status from wanted_items where id=?", (WANTED_ID,)).fetchone()
        result = state_con.execute("select status,verified,library_visibility_status from import_results where queue_id=? order by created_at desc limit 1", (QUEUE_ID,)).fetchone()
    require(queue["state"] == "verified" and not queue["active"], dict(queue))
    require(wanted["status"] in {"satisfied", "verified", "complete"}, dict(wanted))
    require(result["status"] == "library_visible" and result["verified"], dict(result))


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-closed-alpha-e2e-") as tmp:
        root = Path(tmp)
        db_path = root / "state" / "inkdrop-state.sqlite3"
        db_path.parent.mkdir(parents=True)
        seed(db_path, root)
        staged = run_acquisition(db_path, root)
        imported = run_import(db_path, staged, root)
        prove_reader(db_path, imported, root)
    print("INKDROP_CLOSED_ALPHA_E2E_OK")


if __name__ == "__main__":
    main()
