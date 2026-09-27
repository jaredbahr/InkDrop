#!/usr/bin/env python3
"""Bounded OPDS 1.2 catalog and acquisition-file contracts."""

from __future__ import annotations

import mimetypes
import os
import re
import stat as stat_module
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from core import inkdrop_state


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

# What one OPDS request may examine, whatever it finds.
#
# Pagination bounded the ENTRIES RETURNED and nothing else: both catalogue
# loops kept fetching batches until enough physically valid files were found,
# and "valid" is a stat() of the file on disk. fetchmany bounds what is in
# memory at once, not how many batches are fetched, and the SQLite busy
# timeout bounds lock waiting, not filesystem latency. On a library whose rows
# say present but whose files are gone, one request for one entry walked the
# whole eligible population. Measured in
# tests/inkdrop-a-page-limit-is-not-a-work-limit-smoke.py.
#
# A request that runs out of budget says where it stopped rather than
# returning fewer results as if that were the whole truth.
OPDS_MAX_EXAMINED = int(os.environ.get("INKDROP_OPDS_MAX_EXAMINED") or 5000)
OPDS_MAX_SECONDS = float(os.environ.get("INKDROP_OPDS_MAX_SECONDS") or 5.0)


class ScanBudget:
    """What one request may spend, shared across every loop inside it.

    One budget per request, not per loop: the root feed's cost is the product
    of the two, since each series it considers gets a second scan for its
    first valid file. Separate budgets would multiply instead of add.
    """

    def __init__(self, max_examined=None, max_seconds=None):
        self.max_examined = max(1, int(OPDS_MAX_EXAMINED if max_examined is None else max_examined))
        self.deadline = time.monotonic() + max(
            0.05, float(OPDS_MAX_SECONDS if max_seconds is None else max_seconds)
        )
        self.examined = 0
        self.exhausted = False

    def spend(self, count=1):
        """Authorize one more unit of work. False once nothing further may run.

        The call that exhausts the budget still returns True. Returning False
        there would mean a budget of one authorizes nothing, every loop breaks
        before its first row, and the cursor never advances -- a feed that asks
        for the same page forever.
        """
        if self.exhausted:
            return False
        self.examined += int(count)
        if self.examined >= self.max_examined or time.monotonic() > self.deadline:
            self.exhausted = True
        return True


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


def first_valid_series_media(con, series_id, roots, budget=None):
    """The series' first file that is on disk, and whether the search finished.

    Returns (row or None, resolved). `resolved` is the load-bearing half: None
    with resolved=True means this series genuinely has no file on disk, None
    with resolved=False means the budget ran out before that could be
    established. Treating "unknown" as "none" moves the caller's cursor past a
    series whose media was never looked at, which is how a catalogue skips the
    only series that had anything.

    Every row here is a stat(), and this runs once per series the root feed
    considers, so it is the inner half of a product.
    """
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
                return None, True
            for row in batch:
                if budget is not None and not budget.spend():
                    return None, False
                if canonical_nonempty_media(row["path"], roots) is not None:
                    return row, True
    finally:
        cursor.close()


def _continuation(rows, page_rows, limit, budget, last_examined):
    """The `next` cursor: where a reader must resume so nothing is skipped.

    Order matters. If more rows were collected than fit on the page, the cursor
    is the last row ON the page -- resuming from the last EXAMINED id there
    would skip the ones already collected but not shown.

    Otherwise, if the scan stopped because it ran out of budget, the cursor is
    the last id examined, even when the page is short or completely empty. That
    is the case pagination alone could not express: a reader that sees a page
    with no entries and no `next` link concludes the catalogue has ended, when
    in truth the request gave up part-way through a library whose valid media
    is further down.

    A page that ends because the data ended has no cursor, which is what tells
    a reader it has genuinely reached the end.
    """
    if len(rows) > limit and page_rows:
        return page_rows[-1]["id"]
    if budget is not None and budget.exhausted and last_examined:
        return last_examined
    return None


def root_catalog(db_path, *, after=None, limit=None):
    limit = page_contract(limit)
    after = str(after or "").strip()[:200]
    with inkdrop_state.connect_read(db_path, timeout_seconds=2.0, busy_timeout_ms=2000) as con:
        roots = inkdrop_state.media_management_roots_from_connection(con)
        rows = []
        # The id a reader must resume AFTER. It moves only when a series has
        # been fully resolved -- shown, or established to have no file on disk.
        # A series whose scan was cut short mid-way is deliberately left in
        # front of this cursor so the next request looks at it again.
        resume_after = after
        resolved_any = False
        budget = ScanBudget()
        while len(rows) < limit + 1 and not budget.exhausted:
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
                (resume_after, SCAN_BATCH_SIZE),
            ).fetchall()
            if not batch:
                break
            for row in batch:
                if not budget.spend():
                    break
                media, resolved = first_valid_series_media(con, row["id"], roots, budget)
                if not resolved and resolved_any:
                    # The budget ran out inside this series, so whether it has
                    # a file on disk is UNKNOWN. Leave the cursor in front of
                    # it and stop: advancing here is how the one series that
                    # had media gets skipped and the catalogue reports itself
                    # finished. Only when nothing at all has been resolved yet
                    # do we fall through and take the answer as given, because
                    # a request that resolves nothing returns the cursor it was
                    # handed and the reader asks for the same page forever.
                    break
                resume_after = row["id"]
                resolved_any = True
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
    _next(feed, path, limit, _continuation(rows, page_rows, limit, budget, resume_after))
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
        budget = ScanBudget()
        while len(rows) < limit + 1 and not budget.exhausted:
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
                # The budget first, THEN the cursor. Setting the cursor before
                # the check names a row that was never examined, so the next
                # request starts past it and never looks at it.
                if not budget.spend():
                    break
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
    _next(feed, path, limit, _continuation(rows, page_rows, limit, budget, scan_media))
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
