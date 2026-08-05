#!/usr/bin/env python3
"""Bounded OPDS 1.2 catalog and acquisition-file contracts."""

from __future__ import annotations

import mimetypes
import os
import re
import stat as stat_module
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

import inkdrop_state


ATOM = "http://www.w3.org/2005/Atom"
OPDS = "http://opds-spec.org/2010/catalog"
DC = "http://purl.org/dc/terms/"
ET.register_namespace("", ATOM)
ET.register_namespace("opds", OPDS)
ET.register_namespace("dcterms", DC)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MEDIA_TYPES = {
    ".cbz": "application/vnd.comicbook+zip",
    ".cbr": "application/vnd.comicbook-rar",
    ".cb7": "application/x-7z-compressed",
    ".pdf": "application/pdf",
}
XML_INVALID = re.compile("[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]")
SCAN_BATCH_SIZE = 200


def xml_text(value):
    return XML_INVALID.sub("", str(value or ""))


def url_quote(value):
    return quote(xml_text(value), safe="")


def _bounded_int(value, default, low, high):
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def page_contract(limit=None):
    return _bounded_int(limit, DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)


def _iso(value):
    try:
        stamp = float(value or 0)
    except (TypeError, ValueError):
        stamp = 0
    if stamp <= 0:
        stamp = 0
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _text(parent, name, value):
    child = ET.SubElement(parent, f"{{{ATOM}}}{name}")
    child.text = xml_text(value)
    return child


def _link(parent, *, href, rel, type_=None, title=None):
    attributes = {"href": xml_text(href), "rel": xml_text(rel)}
    if type_:
        attributes["type"] = xml_text(type_)
    if title:
        attributes["title"] = xml_text(title)
    return ET.SubElement(parent, f"{{{ATOM}}}link", attributes)


def _feed(title, identifier, updated, self_href, *, self_kind="navigation", up_href=None):
    feed = ET.Element(f"{{{ATOM}}}feed")
    _text(feed, "id", identifier)
    _text(feed, "title", title)
    _text(feed, "updated", _iso(updated))
    author = ET.SubElement(feed, f"{{{ATOM}}}author")
    _text(author, "name", "InkDrop")
    _link(feed, href=self_href, rel="self", type_=f"application/atom+xml;profile=opds-catalog;kind={self_kind}")
    _link(feed, href="/opds/v1.2/catalog.xml", rel="start", type_="application/atom+xml;profile=opds-catalog;kind=navigation")
    if up_href:
        _link(feed, href=up_href, rel="up", type_="application/atom+xml;profile=opds-catalog;kind=navigation")
    return feed


def _xml(feed):
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


def _query_href(path, limit, after=None):
    href = f"{path}?limit={limit}"
    if after:
        href += f"&after={url_quote(after)}"
    return href


def _next(feed, path, limit, after):
    if after:
        _link(feed, href=_query_href(path, limit, after), rel="next", type_="application/atom+xml;profile=opds-catalog")


def canonical_nonempty_media(path, roots):
    canonical = inkdrop_state.canonical_managed_media_file_path(path, roots)
    if canonical is None:
        return None
    try:
        current = canonical.stat()
    except OSError:
        return None
    if not stat_module.S_ISREG(current.st_mode) or int(current.st_size) <= 0:
        return None
    return canonical, current


def first_valid_series_media(con, series_id, roots):
    cursor = con.execute(
        """
        select path,mtime,last_seen_at
        from media_files indexed by idx_media_files_series_issue
        where series_id=? and active=1 and status='present'
        """,
        (series_id,),
    )
    try:
        while True:
            batch = cursor.fetchmany(SCAN_BATCH_SIZE)
            if not batch:
                return None
            for row in batch:
                if canonical_nonempty_media(row["path"], roots) is not None:
                    return row
    finally:
        cursor.close()


def root_catalog(db_path, *, after=None, limit=None):
    limit = page_contract(limit)
    after = str(after or "").strip()[:200]
    with inkdrop_state.connect_read(db_path, timeout_seconds=2.0, busy_timeout_ms=2000) as con:
        roots = inkdrop_state.media_management_roots_from_connection(con)
        rows = []
        scan_series = after
        while len(rows) < limit + 1:
            batch = con.execute(
                """
                select s.id,s.title,s.media_type,s.publisher,s.year,s.updated_at
                from series s
                where s.id>?
                  and exists (
                    select 1 from media_files mf indexed by idx_media_files_series_issue
                    where mf.series_id=s.id and mf.active=1 and mf.status='present'
                  )
                order by s.id
                limit ?
                """,
                (scan_series, SCAN_BATCH_SIZE),
            ).fetchall()
            if not batch:
                break
            for row in batch:
                scan_series = row["id"]
                media = first_valid_series_media(con, row["id"], roots)
                if media is None:
                    continue
                rows.append({
                    **dict(row),
                    "catalog_updated": media["mtime"] or media["last_seen_at"] or row["updated_at"],
                })
                if len(rows) >= limit + 1:
                    break
            if len(batch) < SCAN_BATCH_SIZE:
                break
        newest = max((float(row.get("catalog_updated") or 0) for row in rows), default=0)
    path = "/opds/v1.2/catalog.xml"
    feed = _feed("InkDrop Library", "urn:inkdrop:catalog", newest, _query_href(path, limit, after))
    page_rows = rows[:limit]
    _next(feed, path, limit, page_rows[-1]["id"] if len(rows) > limit and page_rows else None)
    for row in page_rows:
        entry = ET.SubElement(feed, f"{{{ATOM}}}entry")
        _text(entry, "id", f"urn:inkdrop:series:{row['id']}")
        _text(entry, "title", row["title"])
        _text(entry, "updated", _iso(row["catalog_updated"]))
        details = [str(row["publisher"] or "").strip(), str(row["year"] or "").strip()]
        _text(entry, "content", " · ".join(value for value in details if value))
        series_id = url_quote(row["id"])
        _link(
            entry,
            href=f"/opds/v1.2/series/{series_id}.xml",
            rel="subsection",
            type_="application/atom+xml;profile=opds-catalog;kind=acquisition",
            title=row["title"],
        )
    return _xml(feed)


def series_catalog(db_path, series_id, *, after=None, limit=None):
    series_id = str(series_id or "").strip()
    if not series_id or len(series_id) > 200:
        return None
    limit = page_contract(limit)
    after = str(after or "").strip()[:200]
    with inkdrop_state.connect_read(db_path, timeout_seconds=2.0, busy_timeout_ms=2000) as con:
        series = con.execute(
            "select id,title,publisher,year,updated_at from series where id=? limit 1", (series_id,)
        ).fetchone()
        if not series:
            return None
        roots = inkdrop_state.media_management_roots_from_connection(con)
        rows = []
        scan_media = after
        while len(rows) < limit + 1:
            batch = con.execute(
                """
                select mf.id,mf.path,mf.size_bytes,mf.mtime,mf.last_seen_at,
                       i.issue_number,i.normalized_number,i.title as issue_title,i.release_date
                from media_files mf indexed by idx_media_files_series_issue
                left join issues i on i.id=mf.issue_id and i.series_id=mf.series_id
                where mf.series_id=? and mf.active=1 and mf.status='present' and mf.id>?
                order by mf.id
                limit ?
                """,
                (series_id, scan_media, SCAN_BATCH_SIZE),
            ).fetchall()
            if not batch:
                break
            for row in batch:
                scan_media = row["id"]
                if canonical_nonempty_media(row["path"], roots) is None:
                    continue
                rows.append(dict(row))
                if len(rows) >= limit + 1:
                    break
            if len(batch) < SCAN_BATCH_SIZE:
                break
    encoded_id = url_quote(series_id)
    path = f"/opds/v1.2/series/{encoded_id}.xml"
    feed = _feed(series["title"], f"urn:inkdrop:series:{series_id}", series["updated_at"], _query_href(path, limit, after), self_kind="acquisition", up_href="/opds/v1.2/catalog.xml")
    page_rows = rows[:limit]
    _next(feed, path, limit, page_rows[-1]["id"] if len(rows) > limit and page_rows else None)
    for row in page_rows:
        filename = Path(str(row["path"] or "")).name
        number = str(row["normalized_number"] or row["issue_number"] or "").strip()
        issue_title = str(row["issue_title"] or "").strip()
        title = f"{series['title']} #{number}" if number else filename
        if issue_title and issue_title.lower() != title.lower():
            title = f"{title}: {issue_title}"
        entry = ET.SubElement(feed, f"{{{ATOM}}}entry")
        _text(entry, "id", f"urn:inkdrop:file:{row['id']}")
        _text(entry, "title", title)
        _text(entry, "updated", _iso(row["mtime"] or row["last_seen_at"]))
        _text(entry, "content", filename)
        if row["release_date"]:
            published = ET.SubElement(entry, f"{{{DC}}}issued")
            published.text = xml_text(row["release_date"])
        media_id = url_quote(row["id"])
        safe_filename = url_quote(filename)
        _link(
            entry,
            href=f"/opds/v1.2/files/{media_id}/{safe_filename}",
            rel="http://opds-spec.org/acquisition",
            type_=media_type(filename),
            title=f"Download {filename}",
        )
    return _xml(feed)


def media_type(filename):
    lower = str(filename or "").lower()
    if lower.endswith(".cbz.zip"):
        return MEDIA_TYPES[".cbz"]
    suffix = Path(lower).suffix
    return MEDIA_TYPES.get(suffix) or mimetypes.guess_type(str(filename or ""))[0] or "application/octet-stream"


def acquisition_file(db_path, media_id):
    media_id = str(media_id or "").strip()
    if not media_id or len(media_id) > 200:
        return None
    with inkdrop_state.connect_read(db_path, timeout_seconds=2.0, busy_timeout_ms=2000) as con:
        row = con.execute(
            "select id,path,size_bytes,mtime from media_files where id=? and active=1 and status='present' limit 1",
            (media_id,),
        ).fetchone()
        if not row:
            return None
        roots = inkdrop_state.media_management_roots_from_connection(con)
        media = canonical_nonempty_media(row["path"], roots)
    if media is None:
        return None
    path, stat = media
    canonical_roots = []
    for root in roots:
        try:
            canonical_roots.append(Path(root).resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    return {
        "id": media_id,
        "path": path,
        "filename": path.name,
        "content_type": media_type(path.name),
        "size_bytes": int(stat.st_size),
        "mtime": float(stat.st_mtime),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "roots": canonical_roots,
    }


def _under_roots(path, roots):
    return any(root == path or root in path.parents for root in roots or [])


def open_acquisition_file(item):
    """Open the exact validated regular file without following a replacement symlink."""
    path = Path(item["path"])
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        current = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(current.st_mode)
            or int(current.st_size) <= 0
            or int(current.st_dev) != int(item["device"])
            or int(current.st_ino) != int(item["inode"])
        ):
            raise OSError("acquisition identity changed")
        proc_path = Path(f"/proc/self/fd/{descriptor}")
        if proc_path.parent.is_dir():
            descriptor_path = proc_path.resolve(strict=True)
        else:
            descriptor_path = path.resolve(strict=True)
        if not _under_roots(descriptor_path, item.get("roots")):
            raise OSError("acquisition descriptor escaped managed roots")
        return os.fdopen(descriptor, "rb", closefd=True), current
    except (OSError, RuntimeError):
        os.close(descriptor)
        return None
