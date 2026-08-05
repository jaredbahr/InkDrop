#!/usr/bin/env python3
"""Focused OPDS catalog, authentication, and safe-file regression."""

import http.client
import base64
import json
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

import inkdrop_auth
import inkdrop_opds
import inkdrop_state
import inkdrop_web


def require(value, message):
    if not value:
        raise AssertionError(message)


def request(port, path, headers=None, method="GET"):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request(method, path, headers=headers or {})
    response = con.getresponse()
    body = response.read()
    result = (response.status, dict(response.getheaders()), body)
    con.close()
    return result


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-opds-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        library = root / "Comics"
        series_dir = library / "Powers"
        series_dir.mkdir(parents=True)
        comic = series_dir / "Powers 001.cbz"
        comic.write_bytes(b"PK\x03\x04fixture")
        outside = root / "outside.cbz"
        outside.write_bytes(b"PK\x03\x04private")
        late_dir = library / "Late Series"
        late_dir.mkdir()
        late_comic = late_dir / "Late 001.cbz"
        late_comic.write_bytes(b"PK\x03\x04late")
        empty_comic = series_dir / "Empty 002.cbz"
        empty_comic.write_bytes(b"")
        empty_only = library / "Empty Only.cbz"
        empty_only.write_bytes(b"")
        db = root / "state.sqlite3"
        inkdrop_state.ensure_schema(db)
        now = 1785710000.0
        with sqlite3.connect(db) as con:
            con.execute("insert into app_settings(key,scope,label,value_json,source,updated_at) values(?,?,?,?,?,?)", ("media_management.comic_root", "media_management", "Comic root", json.dumps(str(library)), "user", now))
            con.execute("insert into series(id,title,sort_title,media_type,year,publisher,updated_at) values(?,?,?,?,?,?,?)", ("series/1", "Powers & Co.", "Powers", "comic", 2000, "Image", now))
            con.execute("insert into issues(id,series_id,issue_number,normalized_number,title,release_date,updated_at) values(?,?,?,?,?,?,?)", ("issue-1", "series/1", "1", "1", "Who Killed Retro Girl?", "2000-04-01", now))
            con.execute("insert into media_files(id,path,normalized_path,media_type,series_id,issue_id,status,active,size_bytes,mtime,last_seen_at) values(?,?,?,?,?,?,?,?,?,?,?)", ("media/1", str(comic), str(comic).replace("\\", "/"), "comic", "series/1", "issue-1", "present", 1, comic.stat().st_size, now, now))
            con.execute("insert into media_files(id,path,normalized_path,media_type,series_id,issue_id,status,active,size_bytes,mtime,last_seen_at) values(?,?,?,?,?,?,?,?,?,?,?)", ("outside", str(outside), str(outside).replace("\\", "/"), "comic", "series/1", "issue-1", "present", 1, outside.stat().st_size, now, now))
            con.execute("insert into series(id,title,sort_title,media_type,year,publisher,updated_at) values(?,?,?,?,?,?,?)", ("series/2", "Late\x01 Series", "Late Series", "comic", 2026, "Test\x02 Press", now))
            con.execute("insert into issues(id,series_id,issue_number,normalized_number,title,release_date,updated_at) values(?,?,?,?,?,?,?)", ("issue-2", "series/2", "1", "1", "Control\x03 Free", "2026-08-02", now))
            for index in range(205):
                missing = root / "missing" / f"invalid-{index:03}.cbz"
                con.execute("insert into media_files(id,path,normalized_path,media_type,series_id,issue_id,status,active,size_bytes,mtime,last_seen_at) values(?,?,?,?,?,?,?,?,?,?,?)", (f"a{index:03}", str(missing), str(missing).replace("\\", "/"), "comic", "series/2", "issue-2", "present", 1, 10, now, now))
            con.execute("insert into media_files(id,path,normalized_path,media_type,series_id,issue_id,status,active,size_bytes,mtime,last_seen_at) values(?,?,?,?,?,?,?,?,?,?,?)", ("z999", str(late_comic), str(late_comic).replace("\\", "/"), "comic", "series/2", "issue-2", "present", 1, late_comic.stat().st_size, now, now))
            con.execute("insert into media_files(id,path,normalized_path,media_type,series_id,issue_id,status,active,size_bytes,mtime,last_seen_at) values(?,?,?,?,?,?,?,?,?,?,?)", ("zero", str(empty_comic), str(empty_comic).replace("\\", "/"), "comic", "series/1", "issue-1", "present", 1, 0, now, now))
            con.execute("insert into series(id,title,sort_title,media_type,updated_at) values(?,?,?,?,?)", ("series/3", "Empty Only", "Empty Only", "comic", now))
            con.execute("insert into media_files(id,path,normalized_path,media_type,series_id,status,active,size_bytes,mtime,last_seen_at) values(?,?,?,?,?,?,?,?,?,?)", ("zero-only", str(empty_only), str(empty_only).replace("\\", "/"), "comic", "series/3", "present", 1, 0, now, now))
        root_xml = inkdrop_opds.root_catalog(db)
        require(ET.fromstring(root_xml).tag.endswith("feed"), "root catalog must be valid XML")
        require(b"kind=navigation" in root_xml, "root self link must be a navigation feed")
        require(b"<author>" in root_xml and b"<name>InkDrop</name>" in root_xml, "Atom feeds must name an author")
        require(b"Empty Only" not in root_xml, "root catalog must omit a series whose only indexed media is zero-byte")
        first_page = inkdrop_opds.root_catalog(db, limit=1)
        require(b"series%2F1" in first_page and b"after=series%2F1" in first_page, "root first page must carry a keyset cursor")
        late_page = inkdrop_opds.root_catalog(db, after="series/1", limit=1)
        require(b"Late Series" in late_page and b"\x01" not in late_page, "root scan must cross more than one rejected batch and sanitize controls")
        series_xml = inkdrop_opds.series_catalog(db, "series/1")
        require(b"Powers &amp; Co." in series_xml and b"/opds/v1.2/files/media%2F1/" in series_xml, "series feed must escape XML and URL identities")
        require(b"/opds/v1.2/files/outside/" not in series_xml, "catalog must not advertise files outside configured roots")
        require(b"/opds/v1.2/files/zero/" not in series_xml, "catalog must not advertise zero-byte media")
        late_xml = inkdrop_opds.series_catalog(db, "series/2", limit=1)
        require(b"/opds/v1.2/files/z999/" in late_xml, "series scan must advance through rejected rows until it finds a valid file")
        require(b"acquisition/open-access" not in late_xml and b'http://opds-spec.org/acquisition' in late_xml, "authenticated files must use the generic acquisition relation")
        ET.fromstring(late_xml)
        require(inkdrop_opds.acquisition_file(db, "media/1")["path"] == comic.resolve(), "indexed managed file must resolve")
        require(inkdrop_opds.acquisition_file(db, "outside") is None, "indexed file outside configured roots must not resolve")
        require(inkdrop_opds.acquisition_file(db, "zero") is None, "zero-byte media must not be advertised or served")
        stale_item = inkdrop_opds.acquisition_file(db, "media/1")
        original = series_dir / "original.cbz"
        comic.rename(original)
        comic.write_bytes(b"PK\x03\x04replacement")
        require(inkdrop_opds.open_acquisition_file(stale_item) is None, "descriptor identity must reject a replacement after validation")
        comic.unlink()
        original.rename(comic)
        with sqlite3.connect(db) as con:
            media_plan = " ".join(
                str(column)
                for row in con.execute(
                    "explain query plan select path,mtime,last_seen_at from media_files indexed by idx_media_files_series_issue where series_id=? and active=1 and status='present'",
                    ("series/2",),
                ).fetchall()
                for column in row
            )
        require("idx_media_files_series_issue" in media_plan, "root candidate lookup must use the per-series media index")
        require("TEMP B-TREE" not in media_plan.upper(), "root candidate lookup must not sort the media table")
        with sqlite3.connect(db) as con:
            series_plan = " ".join(
                str(column)
                for row in con.execute(
                    """
                    explain query plan
                    select mf.id,mf.path,mf.size_bytes,mf.mtime,mf.last_seen_at,
                           i.issue_number,i.normalized_number,i.title as issue_title,i.release_date
                    from media_files mf indexed by idx_media_files_series_issue
                    left join issues i on i.id=mf.issue_id and i.series_id=mf.series_id
                    where mf.series_id=? and mf.active=1 and mf.status='present' and mf.id>?
                    order by mf.id
                    limit ?
                    """,
                    ("series/2", "", inkdrop_opds.SCAN_BATCH_SIZE),
                ).fetchall()
                for column in row
            )
        require("idx_media_files_series_issue" in series_plan, "series catalog must use the per-series media index")
        require("idx_media_files_status" not in series_plan, "series catalog must never scan the global status index")
        traced_sql = []

        @contextmanager
        def traced_connect(_db_path, **_kwargs):
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            con.set_trace_callback(traced_sql.append)
            try:
                yield con
            finally:
                con.close()

        original_connect_read = inkdrop_state.connect_read
        inkdrop_state.connect_read = traced_connect
        try:
            traced_page = inkdrop_opds.root_catalog(db, limit=1)
        finally:
            inkdrop_state.connect_read = original_connect_read
        media_queries = [sql for sql in traced_sql if "media_files" in sql.lower()]
        require(b"after=series%2F1" in traced_page, "large-library root projection must remain keyset-paginated")
        require(len(media_queries) <= 3, f"root projection issued too many media queries: {len(media_queries)}")

        old_db = inkdrop_web.INKDROP_STATE_DB
        old_env = os.environ.get("INKDROP_AUTH_MODE")
        try:
            inkdrop_web.INKDROP_STATE_DB = db
            os.environ["INKDROP_AUTH_MODE"] = "built_in"
            inkdrop_auth.bootstrap_admin(db, "admin", "correct horse battery staple")
            key = inkdrop_auth.create_api_key(db, "Reader", scopes=["read"])["api_key"]["key"]
            inkdrop_auth.clear_config_cache()
            inkdrop_web.clear_inkdrop_auth_status_cache()
            server = inkdrop_web.InkDropThreadingHTTPServer(("127.0.0.1", 0), inkdrop_web.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            status, _, _ = request(port, "/opds/v1.2/catalog.xml")
            require(status == 401, "catalog must honor configured authentication")
            auth = {"Authorization": "Basic " + base64.b64encode(f"inkdrop:{key}".encode()).decode()}
            status, headers, body = request(port, "/opds/v1.2/catalog.xml", auth)
            require(status == 200 and "kind=navigation" in headers.get("Content-Type", "") and b"Powers" in body, "authenticated root catalog must load through OPDS Basic auth")
            status, _, body = request(port, "/opds/v1.2/series/series%2F1.xml", auth)
            require(status == 200 and b"Who Killed Retro Girl?" in body, "authenticated series feed must load")
            status, headers, body = request(port, "/opds/v1.2/files/media%2F1/Powers%20001.cbz", auth)
            require(status == 200 and body == comic.read_bytes(), "authenticated acquisition must return indexed bytes")
            require(headers.get("Content-Disposition", "").startswith("attachment;"), "acquisition must be a download")
            status, headers, body = request(port, "/opds/v1.2/files/media%2F1/Powers%20001.cbz", {**auth, "Range": "bytes=2-5"})
            require(status == 206 and body == comic.read_bytes()[2:6] and headers.get("Content-Range") == f"bytes 2-5/{comic.stat().st_size}", "range request must return exact bytes")
            status, headers, body = request(port, "/opds/v1.2/files/media%2F1/Powers%20001.cbz", {**auth, "Range": "bytes=999-"})
            require(status == 416 and headers.get("Content-Range") == f"bytes */{comic.stat().st_size}", "invalid range must be explicit")
            status, _, _ = request(port, "/opds/v1.2/files/media%2F1/Powers%20001.cbz", {**auth, "Range": "bytes=" + ("9" * 5000) + "-"})
            require(status == 416, "oversized range integers must be rejected before conversion")
            status, headers, body = request(port, "/opds/v1.2/files/media%2F1/Powers%20001.cbz", auth, method="HEAD")
            require(status == 200 and not body and headers.get("Accept-Ranges") == "bytes", "HEAD must expose acquisition metadata without a body")
            original_open = inkdrop_opds.open_acquisition_file
            inkdrop_opds.open_acquisition_file = lambda _item: None
            try:
                status, headers, body = request(port, "/opds/v1.2/files/media%2F1/Powers%20001.cbz", auth, method="HEAD")
            finally:
                inkdrop_opds.open_acquisition_file = original_open
            require(status == 404 and not body and headers.get("Content-Length") == "0", "stale HEAD failures must remain bodyless")
            status, _, _ = request(port, "/opds/v1.2/files/outside/outside.cbz", auth)
            require(status == 404, "out-of-root acquisition must stay unavailable")
        finally:
            try:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            except UnboundLocalError:
                pass
            inkdrop_web.INKDROP_STATE_DB = old_db
            if old_env is None:
                os.environ.pop("INKDROP_AUTH_MODE", None)
            else:
                os.environ["INKDROP_AUTH_MODE"] = old_env
            inkdrop_auth.clear_config_cache()
            inkdrop_web.clear_inkdrop_auth_status_cache()
    print("PASS: authenticated OPDS catalogs and safe acquisition files")


if __name__ == "__main__":
    main()
