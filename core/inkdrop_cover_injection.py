#!/usr/bin/env python3
"""Put the series' own cover art inside the first book, where readers look for it.

Kavita and Komga do not read InkDrop's stored cover URL. Both derive a series'
cover from the **first page of the lowest-numbered book on disk** -- measured,
not assumed, against throwaway instances of both. So a series whose volume one
opens on a title page, a credits page, or a scanlator's banner shows that as its
identity on every shelf, no matter what InkDrop displays in its own UI.

This module fixes that in the only place those readers look: it fetches the
front cover InkDrop already picked for the series (the volume-one selection from
the series-front-cover work) and rewrites volume one's archive with that image
as page one.

Three things about how it does that are deliberate.

**It renumbers rather than picking a clever filename.** The obvious approach --
name the cover something like ``!0000_cover.jpg`` so it sorts first -- does not
work. Komga natural-sorts leading digits ahead of punctuation, so that name
lands *after* every numbered page and the injection silently does nothing. The
whole archive is renumbered through the same helper the CBR converter uses, so
page one is page one by position rather than by a bet on someone else's sort.

**It never replaces a page.** The existing pages shift down by one and the page
count goes up by one, which is what a printed volume does anyway. Replacing
would destroy reader content for a cosmetic gain.

**It re-targets.** "Volume one" means the lowest unit *currently* on disk, which
changes: a series holding only volume three today gets its cover there, and when
volume one arrives later the reader silently reverts to volume one's real first
page. Every run resolves the target again and moves the injection, so the
archive that holds the cover is always the archive the reader is reading it
from.

Safety follows the archive converter's model exactly -- validate the rewritten
archive before anything is published, retire the original to quarantine rather
than deleting it, and fail one file closed without stopping the run. Two extra
guards exist because this rewrites files the library already trusts:

- the archive's chapter/volume judgement is recomputed before and after, and a
  file whose unit classification would change is refused rather than injected,
  because one extra page moves the marker-coverage ratio that decides it;
- ``imported_files`` is re-keyed to the new bytes, because its ``sha256`` is a
  primary key that the destination-repair pass matches library files against.
  Leaving it stale strands the row the next time the library is reorganised.

There are two ways in, and they share one decision. ``sweep_library()`` is the
backfill: it walks every existing series and brings each one's archives in line,
reporting what it did and why for every series rather than finishing silently.
``maybe_inject_for_series()`` is the automatic path, called after an import and
after a cover changes, and it does nothing at all unless
``media_management.cover_injection_enabled`` has been turned on -- off by
default, because this rewrites files in a real library.

Both go through ``apply_series()``, deliberately. The backfill and the automatic
trigger cannot drift into disagreeing about what a correct series looks like if
there is only one function that decides it.

Dry run is the default everywhere, undo restores the retired original byte for
byte, and a re-run after the cover selection improves is a supported operation
rather than a second injection.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from core import inkdrop_safe_xml

from core import inkdrop_archive_conversion as conversion
from core import inkdrop_runtime_config


COVER_INJECTION_SCHEMA = "inkdrop.cover_injection.v1"

# Provenance travels inside the archive. A database row would not survive the
# file being moved, re-adopted by a rebuilt library, or restored from a backup,
# and every one of those is a case where a later run has to know what it is
# looking at.
MARKER_NAME = "inkdrop-cover.json"

ARCHIVE_SUFFIXES = {".cbz", ".cbr"}

# A cover thinner than this is a thumbnail, not a page. MangaDex's `.256.jpg`
# form is 256px on the long edge and would render as a postage stamp between
# 1500px scans, so the caller is expected to ask for full size and this is the
# backstop that proves it did.
MIN_COVER_LONG_EDGE = 600
# Only a cheap "is this an empty or truncated body" pre-check. Deliberately not
# a quality bar: a flat-colour or heavily-compressed cover can be legitimately
# small, and resolution plus a full decode are what actually judge the image.
MIN_COVER_BYTES = 512


class InjectionRefused(Exception):
    """This one archive cannot be injected safely. The run continues."""

    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = str(reason)
        self.detail = detail


def default_state_db_path():
    """Where the state database lives, from the module that owns that answer.

    ``inkdrop_state`` does not expose an ``INKDROP_STATE_DB`` constant -- the
    modules that appear to have one each build their own from ``STATE_DIR``.
    Asking for it returns an AttributeError, which the settings gate above
    swallows into "not enabled", so getting this wrong disables the feature
    silently rather than loudly.
    """
    return Path(inkdrop_runtime_config.state_db_path())


def default_originals_dir():
    """Retired originals live beside, not inside, the converter's own.

    A shared directory would make an undo ambiguous about which pass wrote the
    copy it is restoring.
    """
    return inkdrop_runtime_config.quarantine_dir() / "cover-injection-originals"


# ---------------------------------------------------------------------------
# Reading what is already there
# ---------------------------------------------------------------------------


def read_marker(path):
    """The injection provenance inside an archive, or None."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = {info.filename for info in archive.infolist() if not info.is_dir()}
            if MARKER_NAME not in names:
                return None
            data = archive.read(MARKER_NAME)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    try:
        marker = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def series_archives(folder):
    """Every comic archive directly under a series folder, newest naming aside.

    Internal directories are skipped for the same reason the converter skips
    them: a quarantined or staged copy is not part of the library.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    found = []
    for candidate in sorted(folder.rglob("*")):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if candidate.suffix.lower() not in ARCHIVE_SUFFIXES:
            continue
        if conversion.is_internal_library_path(candidate, folder):
            continue
        found.append(candidate)
    return found


def _unit_sort_value(text):
    try:
        return float(str(text))
    except (TypeError, ValueError):
        return float("inf")


def resolve_target(archives, unit_resolver=None):
    """Pick the archive a reader would take the series cover from.

    Lowest unit number wins. Volumes outrank chapters at the same number, since
    a reader showing a series built from both is showing the volume. An archive
    whose unit cannot be read at all sorts last and is only chosen when it is
    the only thing present.
    """
    if unit_resolver is None:
        unit_resolver = _default_unit_resolver()

    ranked = []
    for index, path in enumerate(archives):
        try:
            unit, number = unit_resolver(path)
        except Exception:
            unit, number = None, None
        ranked.append(
            (
                _unit_sort_value(number),
                0 if str(unit or "").lower() == "volume" else 1,
                index,
                path,
                str(unit or ""),
                number,
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda row: (row[0], row[1], row[2]))
    _value, _kind, _index, path, unit, number = ranked[0]
    return {"path": path, "unit": unit, "number": number}


def _default_unit_resolver():
    """Reuse the importer's own unit reader so this cannot drift from it."""
    from core import inkdrop_completed_import

    return inkdrop_completed_import.manga_file_unit_and_number


# ---------------------------------------------------------------------------
# ComicInfo
# ---------------------------------------------------------------------------


def rewrite_comicinfo(data, page_count):
    """Bring ComicInfo in line with an archive that gained a leading page.

    ``PageCount`` is corrected, and any ``<Pages>`` block has every ``Image``
    index shifted by one with a ``FrontCover`` entry prepended for the new page
    zero. Neither Kavita nor Komga was observed to read ``<Pages>`` when
    choosing a cover, so this is hygiene rather than the mechanism -- but
    leaving the indices unshifted would point every page-type annotation at the
    wrong page.

    A ``<Pages>`` block that cannot be parsed is refused rather than guessed at.
    """
    from core.inkdrop_image_folder_to_cbz import _validate_xml

    if not data:
        return None
    _validate_xml(data, source="source ComicInfo.xml")
    try:
        root = inkdrop_safe_xml.fromstring(data)
    except ElementTree.ParseError as exc:
        raise InjectionRefused("comicinfo_unparseable", str(exc))

    for node in root.findall("PageCount"):
        root.remove(node)
    ElementTree.SubElement(root, "PageCount").text = str(int(page_count))

    pages = root.find("Pages")
    if pages is not None:
        entries = list(pages.findall("Page"))
        for entry in entries:
            raw = entry.get("Image")
            if raw is None:
                continue
            try:
                entry.set("Image", str(int(str(raw).strip()) + 1))
            except ValueError:
                raise InjectionRefused("comicinfo_page_index_unparseable", raw)
        cover = ElementTree.Element("Page")
        cover.set("Image", "0")
        cover.set("Type", "FrontCover")
        pages.insert(0, cover)

    ElementTree.indent(root, space="  ")
    out = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    _validate_xml(out, source="rewritten ComicInfo.xml")
    return out


# ---------------------------------------------------------------------------
# The cover image itself
# ---------------------------------------------------------------------------


def validate_cover_bytes(data):
    """Prove the fetched bytes are a real, full-size image before they become page one."""
    from PIL import Image, UnidentifiedImageError

    if not data or len(data) < MIN_COVER_BYTES:
        raise InjectionRefused("cover_too_small", {"bytes": len(data or b"")})
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = str(probe.format or "").upper()
            size = probe.size
            probe.verify()
        with Image.open(io.BytesIO(data)) as probe:
            probe.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise InjectionRefused("cover_undecodable", f"{type(exc).__name__}: {exc}")
    if max(size) < MIN_COVER_LONG_EDGE:
        # Almost always the 256px thumbnail form reaching this by mistake.
        raise InjectionRefused(
            "cover_resolution_too_low",
            {"size": list(size), "minimum_long_edge": MIN_COVER_LONG_EDGE},
        )
    extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(fmt)
    if not extension:
        raise InjectionRefused("cover_format_unsupported", fmt)
    return {"format": fmt, "extension": extension, "width": size[0], "height": size[1],
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def mangadex_full_size_cover_url(manga_id, filename):
    """The original upload, not the shelf thumbnail.

    The series-front-cover selection returns a ``.256.jpg`` URL because that is
    the right size for a card. Injected as a page it would be a blurry stamp
    between full-resolution scans, so the filename is reused and the size
    suffix is not.
    """
    from core.inkdrop_web_config import MANGADEX_COVER_URL

    manga_id = str(manga_id or "").strip()
    filename = str(filename or "").strip()
    if not manga_id or not filename:
        return ""
    return f"{MANGADEX_COVER_URL}/{manga_id}/{filename}"


# ComicVine serves every size of an upload from one path with a single segment
# swapped: ``/a/uploads/<size>/<a>/<b>/<file>``. ``original`` is the untouched
# upload, the analogue of dropping MangaDex's ``.256.jpg`` suffix.
#
# Measured across ALL 355 stored ComicVine cover URLs, 2026-08-26 snapshot
# ``20260826T222706Z``: 341 are stored as ``scale_large`` and 14 as
# ``scale_small``. Rewriting only ``scale_large`` would silently skip fourteen
# series, so the segment is replaced whatever it is.
COMICVINE_UPLOAD_RE = re.compile(r"^(https?://[^/]+/a/uploads/)([^/]+)(/.+)$", re.I)

# Filenames that are certainly not a front cover.
#
# The resolver's docstring used to assert ComicVine volume art is "frequently an
# interior page or a screenshot". Measured over all 355: SEVEN are (2.0%) --
# five ``-page1`` scans and a ``screen_shot_2013_...``. So "frequently" was
# wrong, but the kind of failure is real, and ``validate_cover_bytes()`` cannot
# catch it: a page-one scan is a large, well-formed, high-resolution JPEG and
# passes every check there. Only the filename gives it away.
#
# Deliberately NARROW. A broad pattern rejects good covers, and a refused good
# cover is invisible while a wrong injected cover is not. Verified against
# eleven cover-shaped stems including the adversarial ``page-turner-cover``,
# ``x-page`` and ``01-page-one-cover``: none is rejected.
COMICVINE_NOT_A_COVER_RE = re.compile(
    r"(screen[\s_-]?shot|screenshot)|[._-]page\s*\d+\b", re.I
)


def comicvine_full_size_cover_url(url):
    """The original upload behind a ComicVine image URL, or "" if it is not one."""
    text = str(url or "").strip()
    if not text:
        return ""
    match = COMICVINE_UPLOAD_RE.match(text)
    if not match:
        return ""
    prefix, size, tail = match.groups()
    if size.lower() == "original":
        return text
    return f"{prefix}original{tail}"


def comicvine_cover_refusal(url):
    """Why this ComicVine image cannot be a front cover, or "" if it can.

    Judged on the filename because that is the only thing that distinguishes a
    cover from an interior page before the bytes are fetched, and the bytes do
    not distinguish them at all.
    """
    from urllib.parse import unquote

    text = str(url or "").strip()
    if not text:
        return "no_cover_available"
    if not COMICVINE_UPLOAD_RE.match(text):
        # Not an upload URL, so the size segment cannot be rewritten and the
        # host is not the one the proxy allowlist was widened for.
        return "cover_url_unresolved"
    stem = unquote(text.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
    if COMICVINE_NOT_A_COVER_RE.search(stem):
        return "cover_is_not_front_cover"
    return ""


def fetch_cover(url, fetcher=None):
    """Fetch cover bytes through InkDrop's existing allowlisted, bounded proxy."""
    if fetcher is not None:
        return fetcher(url)
    from core import inkdrop_web

    response = inkdrop_web.inkdrop_cover_proxy_response(url)
    if not isinstance(response, dict) or not response.get("ok"):
        raise InjectionRefused(
            "cover_fetch_failed",
            {"status": (response or {}).get("status"), "url": url},
        )
    return response.get("body") or b""


# ---------------------------------------------------------------------------
# Rewriting one archive
# ---------------------------------------------------------------------------


def _archive_semantics(path):
    """The chapter/volume judgement, or None when it cannot be read."""
    try:
        from core import inkdrop_artifact_acceptance

        semantics = inkdrop_artifact_acceptance.archive_member_semantics(str(path), fresh=True)
    except Exception:
        return None
    if not isinstance(semantics, dict) or not semantics.get("checked"):
        return None
    return semantics


def _semantic_unit_of(semantics):
    return str((semantics or {}).get("semantic_unit") or "") or None


def _originals_destination(source, root, originals_dir):
    try:
        relative = Path(source).relative_to(root)
    except ValueError:
        relative = Path(Path(source).name)
    return Path(originals_dir) / Path(root).name / relative


def inject_archive(
    source,
    cover_bytes,
    *,
    marker_fields=None,
    root=None,
    originals_dir=None,
    keep_original=True,
    dry_run=False,
    allow_unit_change=False,
):
    """Rewrite one archive with `cover_bytes` as page one.

    Never raises for a per-file problem; the reason comes back in the result so
    a batch run can continue.
    """
    source = Path(source)
    root = Path(root) if root else source.parent
    result = {"source": str(source), "injected": False, "dry_run": bool(dry_run)}
    if not source.is_file():
        return {**result, "reason": "source_missing"}

    existing = read_marker(source)
    if existing:
        result["existing_marker"] = existing

    try:
        cover_meta = validate_cover_bytes(cover_bytes)
    except InjectionRefused as exc:
        return {**result, "reason": exc.reason, "detail": exc.detail}
    result["cover"] = cover_meta

    if existing and existing.get("cover_sha256") == cover_meta["sha256"]:
        # Same art, same archive: nothing to do. This is what makes a repeated
        # run cheap and a re-run after a selection change meaningful.
        return {**result, "reason": "already_current", "ok": True}

    try:
        inspection = conversion.inspect_archive(source)
    except conversion.ConversionRefused as exc:
        return {**result, "reason": exc.reason, "detail": exc.detail}
    result["inspection"] = inspection
    if inspection["container"] not in {"zip", "rar"}:
        return {**result, "reason": "source_container_unsupported", "detail": inspection["container"]}
    if inspection["nested_archives"]:
        return {**result, "reason": "nested_archive_member", "detail": inspection["nested_archives"]}
    if not inspection["image_count"]:
        return {**result, "reason": "no_image_members"}

    before = _archive_semantics(source)
    result["semantic_unit_before"] = _semantic_unit_of(before)

    originals_target = None
    retire_original = bool(keep_original)
    if keep_original:
        originals_target = _originals_destination(source, root, originals_dir or default_originals_dir())
        result["original_moved_to"] = str(originals_target)
        if originals_target.exists():
            if not existing:
                # Something is already parked in this slot and the file on disk
                # is not ours. Refuse rather than overwrite a copy we cannot
                # account for.
                return {**result, "reason": "original_archive_slot_taken"}
            # Re-injecting -- because the cover selection improved, or the
            # target moved. What is already retired is the PRISTINE file; the
            # thing on disk now is our own previous output. Retiring again
            # would overwrite the only real undo target with a derived one, so
            # the existing copy is kept and the current file is discarded.
            retire_original = False
            result["original_preserved_from_earlier_run"] = True

    if dry_run:
        return {**result, "reason": "injectable", "ok": True}

    # The undo has to be able to find *this* file's original and prove it is
    # this file's original. Both facts are recorded now, from the real values,
    # rather than reconstructed later from a filename: the exact quarantine
    # location the retire step is about to use, and the digest of the bytes
    # being retired. A restore that cannot match the digest refuses.
    identity_fields = {}
    if originals_target is not None:
        try:
            identity_fields["original_quarantine_relpath"] = str(
                originals_target.relative_to(Path(originals_dir or default_originals_dir()))
            ).replace("\\", "/")
        except ValueError:
            identity_fields["original_quarantine_relpath"] = originals_target.name
    if not existing:
        # Only meaningful when this run is the one retiring the pristine file.
        # On a re-injection the recorded digest from the first run still
        # describes what is in quarantine, so it must not be overwritten.
        try:
            identity_fields["original_sha256"] = _sha256_file(source)
            identity_fields["original_bytes"] = source.stat().st_size
        except OSError as exc:
            return {**result, "reason": "original_unreadable", "detail": f"{type(exc).__name__}: {exc}"}
    else:
        for key in ("original_sha256", "original_bytes", "original_quarantine_relpath"):
            if existing.get(key) not in (None, ""):
                identity_fields[key] = existing[key]

    started = time.time()
    # Always publish as .cbz -- a CBR gets converted on the way through, which
    # is the same direction the library is already being moved in.
    dest = source.with_suffix(".cbz")
    tmp_dest = dest.with_suffix(dest.suffix + ".inkdrop-cover.tmp")
    result["dest"] = str(dest)

    with tempfile.TemporaryDirectory(prefix="inkdrop-cover-inject-") as tmp:
        workdir = Path(tmp) / "extract"
        workdir.mkdir(parents=True, exist_ok=True)
        stagedir = Path(tmp) / "stage"
        stagedir.mkdir(parents=True, exist_ok=True)

        try:
            build_meta = _build_injected(
                source,
                workdir,
                stagedir,
                tmp_dest,
                cover_bytes,
                cover_meta,
                {**(marker_fields or {}), **identity_fields},
                inspection,
            )
        except conversion.ConversionRefused as exc:
            tmp_dest.unlink(missing_ok=True)
            return {**result, "reason": exc.reason, "detail": exc.detail}
        except InjectionRefused as exc:
            tmp_dest.unlink(missing_ok=True)
            return {**result, "reason": exc.reason, "detail": exc.detail}
        except OSError as exc:
            tmp_dest.unlink(missing_ok=True)
            return {**result, "reason": "injection_failed", "detail": f"{type(exc).__name__}: {exc}"}

        validation = conversion.validate_cbz(tmp_dest, build_meta)
        result["validation"] = validation
        if not validation["ok"]:
            tmp_dest.unlink(missing_ok=True)
            return {**result, "reason": validation["reason"], "detail": validation.get("detail")}

        # A first injection adds a page. A re-injection swaps the cover it
        # already put there, so the count does not move.
        expected_pages = inspection["image_count"] + (0 if existing else 1)
        if len(build_meta["pages"]) != expected_pages:
            tmp_dest.unlink(missing_ok=True)
            return {
                **result,
                "reason": "page_count_unexpected",
                "detail": {"expected": expected_pages, "written": len(build_meta["pages"])},
            }

        # One extra page moves marker coverage, and marker coverage is what
        # decides chapter-versus-volume. A file that would be reclassified is
        # left alone: a cosmetic cover is not worth changing what the archive
        # is understood to be.
        after = _archive_semantics(tmp_dest)
        result["semantic_unit_after"] = _semantic_unit_of(after)
        if (
            not allow_unit_change
            and before is not None
            and after is not None
            and _semantic_unit_of(before) != _semantic_unit_of(after)
        ):
            tmp_dest.unlink(missing_ok=True)
            return {
                **result,
                "reason": "semantic_unit_would_change",
                "detail": {
                    "before": _semantic_unit_of(before),
                    "after": _semantic_unit_of(after),
                },
            }

        if retire_original:
            try:
                originals_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(originals_target))
            except OSError as exc:
                tmp_dest.unlink(missing_ok=True)
                return {**result, "reason": "original_retire_failed", "detail": f"{type(exc).__name__}: {exc}"}
        else:
            try:
                source.unlink()
            except OSError as exc:
                tmp_dest.unlink(missing_ok=True)
                return {**result, "reason": "original_delete_failed", "detail": f"{type(exc).__name__}: {exc}"}

        try:
            tmp_dest.replace(dest)
        except OSError as exc:
            # Only put back what this run retired. On a re-injection the
            # retired copy is an earlier run's pristine original and must stay
            # where it is.
            if retire_original and originals_target is not None and originals_target.exists():
                try:
                    shutil.move(str(originals_target), str(source))
                except OSError:
                    pass
            tmp_dest.unlink(missing_ok=True)
            return {**result, "reason": "publish_failed", "detail": f"{type(exc).__name__}: {exc}"}

    result.update(
        {
            "injected": True,
            "ok": True,
            "reason": "injected",
            "page_count": len(build_meta["pages"]),
            "comicinfo_rewritten": build_meta.get("comicinfo_rewritten", False),
            "replaced_previous_injection": bool(existing),
            "elapsed_seconds": round(time.time() - started, 3),
        }
    )
    return result


def _build_injected(source, workdir, stagedir, dest_tmp, cover_bytes, cover_meta,
                    marker_fields, inspection):
    """Extract, stage the cover, and write the renumbered archive."""
    container = conversion.archive_container_format(source)
    if container == "zip":
        conversion._extract_zip(source, workdir)
    elif container == "rar":
        conversion._extract_rar(source, workdir)
    else:
        raise conversion.ConversionRefused("source_container_unsupported", container)

    pages = []
    comicinfo_path = None
    for item in conversion._extracted_files(workdir):
        relative = item.relative_to(workdir).as_posix()
        kind = conversion._member_kind(relative)
        if kind == "image":
            if item.stat().st_size <= 0:
                raise conversion.ConversionRefused("source_page_empty", relative)
            pages.append(item)
        elif kind == "comicinfo":
            if comicinfo_path is None:
                comicinfo_path = item
        elif kind == "nested_archive":
            raise conversion.ConversionRefused("nested_archive_member", relative)
        elif Path(relative).name == MARKER_NAME:
            # A previous injection's marker. Dropped: the archive is about to
            # get a fresh one describing what it actually holds now.
            continue

    if not pages:
        raise conversion.ConversionRefused("no_readable_pages")

    # A previous injection's cover page is the first page and must not be kept,
    # or a re-run stacks covers.
    previous = read_marker(source)
    if previous:
        previous_name = str(previous.get("injected_stored_name") or "")
        pages = [
            page
            for page in pages
            if page.relative_to(workdir).as_posix() != previous_name
        ]
        if not pages:
            raise conversion.ConversionRefused("no_readable_pages_after_previous_cover")

    cover_path = stagedir / f"cover{cover_meta['extension']}"
    cover_path.write_bytes(cover_bytes)

    total_pages = len(pages) + 1
    comicinfo_out = None
    comicinfo_rewritten = False
    if comicinfo_path is not None:
        rewritten = rewrite_comicinfo(comicinfo_path.read_bytes(), total_pages)
        if rewritten:
            comicinfo_out = stagedir / "ComicInfo.xml"
            comicinfo_out.write_bytes(rewritten)
            comicinfo_rewritten = True

    width = max(4, len(str(total_pages)))
    stored_cover_name = f"{1:0{width}d}{cover_meta['extension']}"
    marker = {
        "schema": COVER_INJECTION_SCHEMA,
        "injected_stored_name": stored_cover_name,
        "cover_sha256": cover_meta["sha256"],
        "cover_bytes": cover_meta["bytes"],
        "cover_width": cover_meta["width"],
        "cover_height": cover_meta["height"],
        "injected_at": time.time(),
        "injected_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "original_page_count": len(pages),
        **{k: v for k, v in (marker_fields or {}).items() if v not in (None, "")},
    }

    manifest = conversion._write_page_archive(
        pages,
        dest_tmp,
        workdir,
        comicinfo_out if comicinfo_out is not None else comicinfo_path,
        leading_pages=[cover_path],
        extra_members=[(MARKER_NAME, json.dumps(marker, indent=2, sort_keys=True).encode("utf-8"))],
    )

    return {
        "pages": manifest,
        "comicinfo_preserved": (comicinfo_out or comicinfo_path) is not None,
        "comicinfo_rewritten": comicinfo_rewritten,
        "dropped_members": [],
        "source_format": container,
        "dest_format": "cbz",
        "marker": marker,
    }


def remove_injection(source, *, originals_dir=None, dry_run=False):
    """Undo an injection by restoring the retired original.

    Restoring the copy is the honest undo: rebuilding the archive without the
    cover page would leave renumbered members that never matched what was there
    before.
    """
    source = Path(source)
    result = {"source": str(source), "restored": False, "dry_run": bool(dry_run)}
    marker = read_marker(source)
    if not marker:
        return {**result, "reason": "not_injected"}
    result["marker"] = marker

    originals_dir = Path(originals_dir) if originals_dir else default_originals_dir()
    expected_sha = str(marker.get("original_sha256") or "").strip()

    candidates = []
    relpath = str(marker.get("original_quarantine_relpath") or "").strip()
    if relpath:
        candidates.append(originals_dir / relpath)

    # Markers written before the digest existed recorded root_name plus a bare
    # filename. That pair still names an exact location, and for the flat
    # layouts those markers came from it is the right one -- so it is honoured
    # as a recorded path, never as a search. Without this, archives injected by
    # the earlier build become impossible to undo, which is a worse outcome
    # than the bug being fixed.
    legacy_root = str(marker.get("library_root_name") or "").strip()
    legacy_rel = str(marker.get("library_relative_path") or "").strip()
    legacy_exact = None
    if not relpath and legacy_root and legacy_rel:
        legacy_exact = originals_dir / legacy_root / legacy_rel
        candidates.append(legacy_exact)

    # A stem search is only ever a last resort, and only inside this series'
    # own retired-originals subtree. Searching the whole library and taking the
    # first stem match restored one series' archive into another series' folder
    # -- volume-numbering conventions like "01.cbz" collide constantly, and the
    # bare filename carries no identity at all.
    root_name = str(marker.get("library_root_name") or "").strip()
    if root_name:
        scope = originals_dir / root_name
        if scope.is_dir():
            candidates.extend(sorted(scope.rglob(source.stem + ".*")))

    seen = set()
    rejected = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        # The digest is what actually authorizes the restore. A path can be
        # wrong for a dozen boring reasons; bytes that hash to the recorded
        # original cannot belong to a different file.
        if expected_sha:
            try:
                actual = _sha256_file(candidate)
            except OSError:
                continue
            if actual != expected_sha:
                rejected.append({"path": key, "sha256": actual[:12]})
                continue
        elif candidate != legacy_exact:
            # No digest to verify against, and this candidate came from a search
            # rather than from the marker. That combination is exactly what
            # restored one series' archive into another's, so it is refused.
            rejected.append({"path": key, "reason": "no_recorded_digest_to_verify_against"})
            continue

        result["original"] = key
        result["verified_by"] = "sha256" if expected_sha else "recorded_path_only"
        if dry_run:
            return {**result, "reason": "restorable", "ok": True}
        try:
            staged = Path(str(source.with_suffix(candidate.suffix)) + ".restore.tmp")
            shutil.copy2(str(candidate), str(staged))
            if expected_sha and _sha256_file(staged) != expected_sha:
                staged.unlink(missing_ok=True)
                return {**result, "reason": "restore_verification_failed"}
            staged.replace(source.with_suffix(candidate.suffix))
            if source.suffix.lower() != candidate.suffix.lower() and source.exists():
                source.unlink()
        except OSError as exc:
            return {**result, "reason": "restore_failed", "detail": f"{type(exc).__name__}: {exc}"}
        return {**result, "restored": True, "ok": True, "reason": "restored"}

    if rejected:
        # Refusing is the correct outcome: a wrong restore silently corrupts a
        # library file, and there is no way to tell afterwards.
        return {**result, "reason": "original_identity_unverified", "detail": rejected[:5]}
    return {**result, "reason": "original_not_found"}


# ---------------------------------------------------------------------------
# imported_files -- keep the import ledger pointing at the bytes on disk
# ---------------------------------------------------------------------------


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reconcile_imported_files(old_path, new_path, *, db_path=None, dry_run=False):
    """Re-key the import ledger row for a rewritten file.

    ``imported_files.sha256`` is the table's primary key and holds the digest
    taken at import. ``repair_imported_file_destinations()`` uses it to find a
    library file again after a reorganisation by re-hashing candidates. Once the
    bytes on disk change, that match can never succeed, and the row silently
    becomes unrepairable -- the unit reads as not-imported and a download task
    can wait on it forever.

    Rewriting the key here is what keeps that repair path working. The row is
    matched on its recorded destination, so a file this pass never touched is
    never rewritten.
    """
    summary = {"checked": 0, "updated": 0, "dry_run": bool(dry_run), "reason": None}
    db_path = Path(db_path) if db_path else inkdrop_runtime_config.imported_files_db_path()
    if not Path(db_path).exists():
        summary["reason"] = "imported_files_db_missing"
        return summary
    new_path = Path(new_path)
    if not new_path.is_file():
        summary["reason"] = "new_path_missing"
        return summary

    digest = _sha256_file(new_path)
    size = new_path.stat().st_size
    wanted = {str(old_path), str(new_path)}
    # Windows-authored rows and POSIX-authored rows disagree on separators.
    wanted |= {value.replace("\\", "/") for value in set(wanted)}

    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
    except sqlite3.Error as exc:
        summary["reason"] = f"connect_failed:{type(exc).__name__}"
        return summary
    try:
        rows = conn.execute("select sha256, dest from imported_files").fetchall()
        targets = []
        for sha, dest in rows:
            dest_text = str(dest or "")
            if not dest_text:
                continue
            if dest_text in wanted or dest_text.replace("\\", "/") in wanted:
                summary["checked"] += 1
                if str(sha or "") != digest:
                    targets.append(str(sha or ""))
        if targets and not dry_run:
            for sha in targets:
                # The key itself moves, so this is a delete-and-reinsert rather
                # than an update, and the new key may already exist if the same
                # bytes were imported before.
                row = conn.execute(
                    "select source, dest, size, imported_at from imported_files where sha256=?",
                    (sha,),
                ).fetchone()
                if row is None:
                    continue
                conn.execute("delete from imported_files where sha256=?", (sha,))
                conn.execute(
                    "insert or replace into imported_files values (?,?,?,?,?)",
                    (digest, row[0], str(new_path), size, row[3]),
                )
            conn.commit()
        summary["updated"] = len(targets)
        summary["sha256"] = digest
    except sqlite3.Error as exc:
        summary["reason"] = f"sqlite_error:{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# One series, end to end -- shared by the sweep and the automatic trigger
# ---------------------------------------------------------------------------


SUPPORTED_PROVIDERS = {"mangadex", "comicvine"}

# Outcomes that mean "there is nothing to do here", not "something went wrong".
# A library-wide sweep hits these constantly -- series with no files yet, series
# from a provider this cannot read a cover from -- and counting them as failures
# would make an entirely healthy backfill report as broken and exit non-zero.
# They are still reported individually, with their reason, rather than hidden.
BENIGN_SKIP_REASONS = {
    "series_has_no_library_folder",
    "library_folder_missing",
    "no_archives_in_folder",
    "no_target_resolved",
    "provider_unsupported",
    "series_missing_metadata_id",
    "no_cover_available",
    # A deliberate guard firing, not a fault: the archive would have been
    # reclassified chapter-versus-volume, so it was left alone on purpose.
    "semantic_unit_would_change",
}


def resolve_series_cover_url(series, *, cover_records=None, select_cover=None, settings=None):
    """The full-size front cover for a series, or a refusal reason.

    Reuses the volume-one selection rather than re-deriving it: the same lowest
    volume wins, and locale still only breaks ties within a volume. Only the URL
    differs -- the shelf asks for a 256px thumbnail, and a page needs the
    original upload.

    MangaDex and ComicVine resolve by different routes because they store
    different things. MangaDex has a cover list with volume numbers, so the
    volume-one selection above picks the art. ComicVine has no volume field to
    sort on and stores exactly one image per series, so there is nothing to
    select -- the work is turning that one URL into the full-size upload and
    refusing it when it is not a cover.

    Metron is still refused: it returns no cover at all.
    """
    series = series if isinstance(series, dict) else {}
    provider = str(series.get("metadata_provider") or "").strip().lower()
    metadata_id = str(series.get("metadata_id") or "").strip()
    if provider not in SUPPORTED_PROVIDERS:
        return {"ok": False, "reason": "provider_unsupported", "provider": provider or "none"}
    if not metadata_id:
        return {"ok": False, "reason": "series_missing_metadata_id", "provider": provider}

    if provider == "comicvine":
        return _resolve_comicvine_cover(series, provider)

    if cover_records is None or select_cover is None or settings is None:
        from core import inkdrop_web

        cover_records = cover_records or inkdrop_web.mangadex_cover_records
        select_cover = select_cover or inkdrop_web.mangadex_select_front_cover
        if settings is None:
            # Without this the reader's configured language never reaches the
            # selection, so locale ranking collapses to "original language" and
            # an English reader gets the Japanese printing even when MangaDex
            # has an English volume-1 cover. Measured on Chainsaw Man: passing
            # the configured ['en'] picks a different, English cover.
            try:
                settings = inkdrop_web.load_mangadex_settings()
            except Exception:
                settings = {}

    try:
        covers = cover_records(metadata_id)
    except Exception as exc:
        return {"ok": False, "reason": "cover_lookup_failed",
                "detail": f"{type(exc).__name__}: {exc}", "provider": provider}
    settings = settings if isinstance(settings, dict) else {}
    filename = select_cover(
        covers,
        preferred_locales=settings.get("translated_languages"),
        original_language=series.get("original_language"),
    )
    if not filename:
        return {"ok": False, "reason": "no_cover_available", "provider": provider}
    url = mangadex_full_size_cover_url(metadata_id, filename)
    if not url:
        return {"ok": False, "reason": "cover_url_unresolved", "provider": provider}
    return {"ok": True, "url": url, "filename": filename, "provider": provider}


def _resolve_comicvine_cover(series, provider):
    """Resolve the full-size ComicVine cover for one series.

    Reads ``raw_json`` rather than a ``cover_url`` key because that is what the
    real path provides: ``series_candidates()`` selects
    ``id, title, media_type, metadata_provider, metadata_id, library_path,
    raw_json`` and adds only ``original_language``. A fixture handing this a
    tidy ``cover_url`` would be more generous than production and would prove
    nothing.

    Extraction goes through ``series_image_from_raw()`` -- the same function the
    rest of the product uses to find a series image -- rather than a second
    reader that could disagree with it about where the image lives.
    """
    from core.inkdrop_state import series_image_from_raw

    raw = series.get("raw_json")
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw or "{}")
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        raw = raw if isinstance(raw, dict) else {}

    stored = str(series_image_from_raw(raw) or "").strip()
    refusal = comicvine_cover_refusal(stored)
    if refusal:
        return {"ok": False, "reason": refusal, "provider": provider}

    url = comicvine_full_size_cover_url(stored)
    if not url:
        return {"ok": False, "reason": "cover_url_unresolved", "provider": provider}

    # The stored URL is a real fallback, not belt-and-braces: of 230 series with
    # a usable cover, 228 validate at ``/original/`` and TWO validate only at
    # the stored size. A resolver that returned the rewritten URL alone would
    # lose those two.
    fallbacks = [stored] if stored and stored != url else []
    return {"ok": True, "url": url, "fallback_urls": fallbacks,
            "filename": url.rsplit("/", 1)[-1], "provider": provider}


def apply_series(
    series,
    *,
    dry_run=True,
    keep_original=True,
    originals_dir=None,
    allow_unit_change=False,
    cover_url_resolver=None,
    fetcher=None,
    unit_resolver=None,
    reconcile_ledger=True,
):
    """Bring one series' archives in line with its cover, whatever state they are in.

    This is the whole per-series decision in one place, deliberately: the bulk
    backfill and the automatic post-import trigger both call it, so the two can
    never drift into disagreeing about what "correct" means for a series.

    It handles four cases:

    - nothing injected yet -> inject into the lowest unit present;
    - already injected into the file that is still the lowest unit, with the
      same art -> no-op, which is what makes a whole-library sweep cheap to
      re-run;
    - already injected but the art has changed -> rewrite in place;
    - already injected into a file that is **no longer** the lowest unit -> undo
      that one and inject the new lowest instead. That is the case an earlier
      volume arriving creates, and without it the reader silently reverts to the
      new book's real first page while a stray cover sits inside the old one.
    """
    series = series if isinstance(series, dict) else {}
    folder = str(series.get("library_path") or "").strip()
    outcome = {
        "series_id": series.get("id"),
        "title": series.get("title"),
        "folder": folder,
        "dry_run": bool(dry_run),
        "changed": False,
    }

    if not folder:
        return {**outcome, "reason": "series_has_no_library_folder"}
    if not Path(folder).is_dir():
        return {**outcome, "reason": "library_folder_missing"}

    archives = series_archives(folder)
    if not archives:
        return {**outcome, "reason": "no_archives_in_folder"}

    target = resolve_target(archives, unit_resolver=unit_resolver)
    if target is None:
        return {**outcome, "reason": "no_target_resolved"}
    outcome.update(
        {
            "target": str(target["path"]),
            "target_unit": target["unit"],
            "target_number": target["number"],
        }
    )

    # Anything else already carrying a cover is now the wrong file.
    stale = [
        path
        for path in archives
        if path != target["path"] and read_marker(path) is not None
    ]
    outcome["stale_injections"] = [str(path) for path in stale]

    resolver = cover_url_resolver or resolve_series_cover_url
    resolved = resolver(series)
    if not resolved.get("ok"):
        return {**outcome, "reason": resolved.get("reason"), "detail": resolved.get("detail"),
                "provider": resolved.get("provider")}
    outcome["cover_url"] = resolved["url"]

    existing = read_marker(target["path"])
    if existing and not stale:
        # Cheap path: same file, and the fetch can be skipped entirely unless
        # the art actually moved. A sweep over a settled library does no
        # network work at all.
        outcome["existing_marker"] = existing

    candidates = [resolved["url"]]
    candidates += [str(u) for u in (resolved.get("fallback_urls") or []) if str(u or "").strip()]

    if len(candidates) == 1:
        # The single-candidate path is left exactly as it was. MangaDex never
        # sets fallbacks, so it fetches once and validation stays where it was,
        # inside inject_archive() -- including which layer reports a refusal.
        try:
            cover_bytes = fetch_cover(resolved["url"], fetcher=fetcher)
        except InjectionRefused as exc:
            return {**outcome, "reason": exc.reason, "detail": exc.detail}
    else:
        # More than one candidate means the caller has said a later URL is worth
        # trying when an earlier one is unusable, so each is fetched AND
        # validated and the first that satisfies both wins. Validating here is
        # what makes the fallback real: a URL that fetches happily but decodes
        # to a 334x500 thumbnail has to fall through, not win.
        cover_bytes = None
        last = None
        for candidate in candidates:
            try:
                attempt = fetch_cover(candidate, fetcher=fetcher)
                validate_cover_bytes(attempt)
            except InjectionRefused as exc:
                last = exc
                continue
            cover_bytes = attempt
            outcome["cover_url"] = candidate
            resolved = {**resolved, "url": candidate}
            break
        if cover_bytes is None:
            exc = last or InjectionRefused("cover_fetch_failed", {"url": candidates[0]})
            return {**outcome, "reason": exc.reason, "detail": exc.detail}

    if dry_run:
        planned = "retarget" if stale else ("refresh" if existing else "inject")
        try:
            cover_meta = validate_cover_bytes(cover_bytes)
        except InjectionRefused as exc:
            return {**outcome, "reason": exc.reason, "detail": exc.detail}
        if existing and existing.get("cover_sha256") == cover_meta["sha256"] and not stale:
            return {**outcome, "reason": "already_current", "ok": True, "planned": "none"}
        return {**outcome, "reason": "would_" + planned, "ok": True, "planned": planned,
                "cover": cover_meta}

    # Retire stale injections first. Doing this before the new one is written
    # means a failure here leaves the library with the old cover still working
    # rather than with none and a half-finished move.
    restored = []
    for path in stale:
        undo = remove_injection(path, originals_dir=originals_dir, dry_run=False)
        restored.append({"path": str(path), **{k: undo[k] for k in ("reason", "restored") if k in undo}})
        if not undo.get("restored"):
            return {**outcome, "reason": "stale_injection_not_removed",
                    "detail": restored, "restored": restored}
    outcome["restored"] = restored

    result = inject_archive(
        target["path"],
        cover_bytes,
        marker_fields={
            "series_id": series.get("id"),
            "series_title": series.get("title"),
            "source_url": resolved["url"],
            "provider": resolved.get("provider"),
            "unit": target["unit"],
            "unit_number": target["number"],
            "library_root_name": Path(folder).name,
            # Deliberately not a reconstructed path: inject_archive() records
            # the exact quarantine location and the original's digest, because
            # it is the only place that knows both. A bare filename here is
            # what let an undo match a different series' archive.
            "library_relative_path": str(
                Path(target["path"]).relative_to(folder)
            ).replace("\\", "/"),
        },
        root=folder,
        originals_dir=originals_dir,
        keep_original=keep_original,
        dry_run=False,
        allow_unit_change=allow_unit_change,
    )
    outcome["injection"] = result
    if not result.get("ok"):
        return {**outcome, "reason": result.get("reason"), "detail": result.get("detail")}
    if not result.get("injected"):
        return {**outcome, "reason": result.get("reason"), "ok": True}

    # Publication has happened and cannot be taken back from here: the new
    # archive is in place and the original has been retired to quarantine.
    # Everything the caller needs in order to finish the job -- which folder
    # changed, where the file landed, whether the name changed -- is already
    # known, so it is recorded *before* anything else is allowed to fail. A
    # later failure has to degrade this to "changed, but incomplete"; it must
    # never be allowed to read as "nothing happened", because the library on
    # disk says otherwise.
    #
    # A CBR is republished as a CBZ, which is a different filename. Komga
    # soft-deletes the old book rather than forgetting it, and two books with
    # the same name leave the stale one able to win as the series' first book --
    # so the caller has to be told a rename happened, not just a rewrite.
    dest = result.get("dest") or str(target["path"])
    renamed = Path(dest).name != Path(target["path"]).name
    published = {**outcome, "changed": True, "reason": "injected",
                 "dest": dest, "renamed": renamed}

    if reconcile_ledger:
        try:
            published["imported_files"] = reconcile_imported_files(
                target["path"], result.get("dest") or target["path"], dry_run=False
            )
        except Exception as exc:
            # Returning a bare failure here would strand the library: the
            # caller decides on `changed`/`folder` whether to refresh the
            # readers, and a republished CBR needs Komga's trash emptied or the
            # soft-deleted old book stays authoritative. Keep the publication
            # facts, report the operation as unsuccessful, and name the phase
            # that failed so the ledger can be repaired without guessing.
            return {**published, "ok": False, "reason": "ledger_reconcile_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "ledger_reconciled": False}
        return {**published, "ok": True, "ledger_reconciled": True}
    return {**published, "ok": True, "ledger_reconciled": False}


# ---------------------------------------------------------------------------
# The whole library, backwards
# ---------------------------------------------------------------------------


def series_candidates(db_path=None, series_ids=None):
    """Every series the sweep considered, split into what it can act on and what it cannot.

    The provider filter used to live inside the row loop and simply ``continue``
    past anything it could not read a cover for. That was invisible: the rows
    never reached the sweep, so they were absent from its considered count, from
    its skip reasons, and from its results -- a backfill across a library that
    is 92% ComicVine looked at 37 series out of 484 and printed a clean run.

    So the exclusion is returned rather than swallowed. The eligible list is
    exactly what it was; the excluded list is what the caller has to account for
    if it wants to claim it swept a library.

    The reason is decided here, from the row alone, and deliberately *not* by
    letting the row fall through to ``apply_series``. A ComicVine series whose
    folder is also missing would come back ``library_folder_missing`` from
    there, and the provider gap -- the thing that actually stops it -- would be
    undercounted by every series that has a second problem as well.
    """
    db_path = Path(db_path) if db_path else default_state_db_path()
    if not db_path.exists():
        return {"eligible": [], "excluded": []}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        query = (
            "select id, title, media_type, metadata_provider, metadata_id, "
            "library_path, raw_json from series "
            "where coalesce(trim(library_path),'') <> ''"
        )
        params = []
        if series_ids:
            ids = [str(value) for value in series_ids if str(value or "").strip()]
            if not ids:
                return {"eligible": [], "excluded": []}
            query += f" and id in ({','.join('?' for _ in ids)})"
            params = ids
        query += " order by coalesce(sort_title, title) collate nocase"
        rows = [dict(row) for row in con.execute(query, params).fetchall()]
    finally:
        con.close()

    out = []
    excluded = []
    for row in rows:
        provider = str(row.get("metadata_provider") or "").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            excluded.append(
                {
                    "series_id": row.get("id"),
                    "title": row.get("title"),
                    "folder": str(row.get("library_path") or ""),
                    "provider": provider or "none",
                    "reason": "provider_unsupported",
                    "considered": False,
                    "changed": False,
                }
            )
            continue
        raw = {}
        try:
            raw = json.loads(row.get("raw_json") or "{}")
        except (TypeError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            row["original_language"] = raw.get("originalLanguage") or raw.get("original_language")
        out.append(row)
    return {"eligible": out, "excluded": excluded}


def injectable_series(db_path=None, series_ids=None):
    """The rows the injector can act on. The automatic paths want only these.

    A sweep wants ``series_candidates()`` instead: it has to report the rows
    this drops, and this signature has nowhere to put them.
    """
    return series_candidates(db_path=db_path, series_ids=series_ids)["eligible"]


def sweep_library(
    *,
    db_path=None,
    series_ids=None,
    dry_run=True,
    limit=None,
    keep_original=True,
    originals_dir=None,
    allow_unit_change=False,
    progress=None,
    refresh=True,
    **apply_kwargs,
):
    """Run the whole existing library backwards through the injector.

    Every series is reported, including the ones nothing happened to and why,
    because this rewrites archive files across the entire library and a bare
    "done" would be the wrong amount of information to hand someone afterwards.
    """
    candidates = series_candidates(db_path=db_path, series_ids=series_ids)
    series = candidates["eligible"]
    excluded = candidates["excluded"]
    summary = {
        "schema": COVER_INJECTION_SCHEMA,
        "dry_run": bool(dry_run),
        # Three numbers, not one. "considered" alone reads as the whole library
        # to anyone who does not already know a provider filter ran first.
        "series_in_library": len(series) + len(excluded),
        "series_considered": len(series),
        "not_considered": len(excluded),
        "not_considered_reasons": {},
        "injected": 0,
        "would_change": 0,
        "already_current": 0,
        "skipped": 0,
        "failed": 0,
        "changed_folders": [],
        "renamed_folders": [],
        "results": [],
        "skipped_reasons": {},
        "failed_reasons": {},
    }

    attempted = 0
    for index, row in enumerate(series, 1):
        if limit is not None and attempted >= int(limit):
            summary["skipped"] += 1
            summary["skipped_reasons"]["limit_reached"] = (
                summary["skipped_reasons"].get("limit_reached", 0) + 1
            )
            summary["results"].append(
                {"series_id": row.get("id"), "title": row.get("title"), "reason": "limit_reached"}
            )
            continue
        attempted += 1
        if progress:
            progress(
                {
                    "event": "start",
                    "index": index,
                    "total": len(series),
                    "series_id": row.get("id"),
                    "title": row.get("title"),
                }
            )
        try:
            result = apply_series(
                row,
                dry_run=dry_run,
                keep_original=keep_original,
                originals_dir=originals_dir,
                allow_unit_change=allow_unit_change,
                **apply_kwargs,
            )
        except Exception as exc:
            # One series' unexpected failure is not the sweep's.
            result = {
                "series_id": row.get("id"),
                "title": row.get("title"),
                "reason": "unhandled_error",
                "detail": f"{type(exc).__name__}: {exc}",
            }

        reason = str(result.get("reason") or "")
        if result.get("changed"):
            summary["injected"] += 1
            if result.get("folder"):
                summary["changed_folders"].append(result["folder"])
                if result.get("renamed"):
                    summary["renamed_folders"].append(result["folder"])
        elif reason == "already_current":
            summary["already_current"] += 1
        elif reason.startswith("would_"):
            summary["would_change"] += 1
            if result.get("folder"):
                summary["changed_folders"].append(result["folder"])
        elif result.get("ok") or reason in BENIGN_SKIP_REASONS:
            # "Nothing to do" is a skip with a reason, not a failure. A library
            # full of series with no files yet, or from a provider whose covers
            # cannot be read, must not make a healthy sweep exit non-zero.
            summary["skipped"] += 1
            summary["skipped_reasons"][reason] = summary["skipped_reasons"].get(reason, 0) + 1
        else:
            summary["failed"] += 1
            summary["failed_reasons"][reason] = summary["failed_reasons"].get(reason, 0) + 1

        summary["results"].append(result)
        if progress:
            progress({"event": "done", "index": index, "total": len(series), **result})

    # The rows the provider filter dropped are reported here rather than left
    # out, keyed by reason and provider so the gap is a number someone can act
    # on instead of an absence they have to notice.
    for row in excluded:
        key = f"{row['reason']}:{row['provider']}"
        summary["not_considered_reasons"][key] = summary["not_considered_reasons"].get(key, 0) + 1
        summary["results"].append(row)

    summary["attempted"] = attempted
    summary["changed_folders"] = sorted(set(summary["changed_folders"]))
    summary["renamed_folders"] = sorted(set(summary["renamed_folders"]))
    if refresh and not dry_run and summary["changed_folders"]:
        summary["reader_refresh"] = refresh_readers(
            summary["changed_folders"], renamed_folders=summary["renamed_folders"]
        )
    summary["ok"] = summary["failed"] == 0
    # Distinct from "ok" on purpose. Nothing failed *and* the run only looked at
    # part of the library are both true at once, and a caller that wants to
    # close a backfill out needs the second one, which "ok" cannot carry.
    summary["complete"] = summary["not_considered"] == 0
    return summary


# ---------------------------------------------------------------------------
# The automatic path
# ---------------------------------------------------------------------------


def automatic_injection_enabled(db_path=None):
    """Whether the user has turned the automatic feature on. Off unless they did.

    The exception handler here is deliberately narrow about what it protects: a
    settings read that fails should leave the feature off, not crash an import.
    But it also means a *coding* error in the path above it would read as "the
    user did not enable this" forever, which is exactly how the first version of
    this shipped -- it asked ``inkdrop_state`` for an attribute that module does
    not define, and the automatic path was silently dead with the toggle on.
    The state-database location now comes from the one function that owns it.
    """
    try:
        from core import inkdrop_state

        settings = inkdrop_state.media_management_settings_context(
            Path(db_path) if db_path else default_state_db_path()
        )
    except Exception:
        return False
    return bool((settings or {}).get("cover_injection_enabled", False))


def maybe_inject_for_folders(folders, *, db_path=None, reason="import", refresh=False, **kwargs):
    """Automatic entry point keyed by library folder rather than series id.

    The import path knows which folders it wrote to, not which series rows those
    belong to, so this maps folders back to series. Reader refresh is off by
    default here because the caller is the post-import path, which is about to
    sync the readers anyway -- firing our own scan first would just double it.
    """
    result = {"trigger": reason, "folders": [str(f) for f in (folders or [])],
              "changed_folders": [], "renamed_folders": [], "results": []}
    if not folders:
        return {**result, "reason": "no_folders", "ok": True}
    if not automatic_injection_enabled(db_path=db_path):
        return {**result, "reason": "cover_injection_disabled", "ok": True}

    wanted = {str(Path(folder)) for folder in folders}
    candidates = series_candidates(db_path=db_path)
    rows = [
        row
        for row in candidates["eligible"]
        if str(Path(str(row.get("library_path") or ""))) in wanted
    ]
    if not rows:
        # "No injectable series" and "this series' provider is not supported"
        # are different facts, and the import path only ever saw the first.
        # Naming the second is what stops a folder that will *never* get a
        # cover from looking like one that simply had no series row yet.
        blocked = [
            row
            for row in candidates["excluded"]
            if str(Path(str(row.get("folder") or ""))) in wanted
        ]
        if blocked:
            return {
                **result,
                "reason": "provider_unsupported",
                "unsupported_providers": sorted({row["provider"] for row in blocked}),
                "unsupported_series": [row["series_id"] for row in blocked],
                "ok": True,
            }
        return {**result, "reason": "no_injectable_series_for_folders", "ok": True}

    for row in rows:
        try:
            outcome = apply_series(row, dry_run=False, **kwargs)
        except Exception as exc:
            # apply_series() is responsible for never raising once it has
            # published -- it folds a post-publication failure into a
            # changed/ok=False outcome instead. This stays as the last resort
            # for a failure before publication, and carries the folder so a
            # row can still be attributed to something.
            outcome = {
                "series_id": row.get("id"),
                "title": row.get("title"),
                "folder": str(row.get("library_path") or ""),
                "ok": False,
                "reason": "unhandled_error",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        result["results"].append(outcome)
        # Keyed on `changed`, not on success: a series whose archive was
        # republished but whose ledger write failed still needs its readers
        # told, and still needs to appear in the changed-folder list the
        # import path reads.
        if outcome.get("changed") and outcome.get("folder"):
            result["changed_folders"].append(outcome["folder"])
            if outcome.get("renamed"):
                result["renamed_folders"].append(outcome["folder"])

    result["changed_folders"] = sorted(set(result["changed_folders"]))
    result["renamed_folders"] = sorted(set(result["renamed_folders"]))
    if refresh and result["changed_folders"]:
        result["reader_refresh"] = refresh_readers(
            result["changed_folders"], renamed_folders=result["renamed_folders"]
        )
    # Derived, not asserted. This used to be a literal True, so *any* per-series
    # failure -- a refused undo, a missing cover, a failed ledger write -- was
    # reported to the import path as a clean run.
    failed = [row for row in result["results"] if not row.get("ok")]
    result["ok"] = not failed
    if failed:
        result["failed"] = len(failed)
        result["reason"] = failed[0].get("reason") or "series_failed"
    return result


def maybe_inject_for_series(series_id, *, db_path=None, reason="import", **kwargs):
    """Automatic entry point. Does nothing at all unless the setting is on.

    Called after an import lands and after a series' cover changes. Both end up
    in the same place -- ``apply_series`` re-resolves the target and the art
    every time, so "a lower volume arrived" and "the cover was corrected" do not
    need to be told apart here.
    """
    result = {"series_id": series_id, "trigger": reason, "changed": False}
    if not automatic_injection_enabled(db_path=db_path):
        return {**result, "reason": "cover_injection_disabled", "ok": True}
    candidates = series_candidates(db_path=db_path, series_ids=[series_id])
    rows = candidates["eligible"]
    if not rows:
        blocked = candidates["excluded"]
        if blocked:
            return {**result, "reason": "provider_unsupported",
                    "provider": blocked[0]["provider"], "ok": True}
        return {**result, "reason": "series_not_injectable", "ok": True}
    outcome = apply_series(rows[0], dry_run=False, **kwargs)
    if outcome.get("changed") and outcome.get("folder"):
        outcome["reader_refresh"] = refresh_readers(
            [outcome["folder"]],
            renamed_folders=[outcome["folder"]] if outcome.get("renamed") else [],
        )
    return {**result, **outcome, "trigger": reason}


# ---------------------------------------------------------------------------
# Telling the readers
# ---------------------------------------------------------------------------


def refresh_readers(folders, *, sync=None, kavita_cover_refresh=None,
                    komga_empty_trash=None, renamed_folders=None):
    """Ask the readers to re-derive their covers for these folders.

    The two need different asks, measured rather than assumed. Komga re-derives
    from an ordinary library scan, which the existing post-import sync already
    sends. Kavita does **not** -- a scan, even a forced one, re-reads the
    archive while keeping the cached cover image, so it needs its metadata
    refresh on top. Without that second call the archive on disk is right and
    every Kavita shelf still shows the old picture.
    """
    folders = [str(folder) for folder in (folders or []) if str(folder or "").strip()]
    renamed_folders = [str(folder) for folder in (renamed_folders or [])]
    summary = {"folders": folders, "scan": None, "kavita_cover_refresh": [],
               "komga_empty_trash": [], "errors": []}
    if not folders:
        return summary

    library_id_for_folder = None
    if sync is None or kavita_cover_refresh is None or komga_empty_trash is None:
        from core import inkdrop_completed_import

        sync = sync or inkdrop_completed_import.sync_library_frontend_folders
        kavita_cover_refresh = (
            kavita_cover_refresh or inkdrop_completed_import.trigger_kavita_cover_refresh_folder
        )
        komga_empty_trash = (
            komga_empty_trash or inkdrop_completed_import.trigger_komga_empty_trash_folder
        )
        library_id_for_folder = getattr(inkdrop_completed_import, "kavita_library_id_for_folder", None)

    try:
        summary["scan"] = sync(folders, event_prefix="cover_injection_")
    except Exception as exc:
        summary["errors"].append({"stage": "scan", "error": f"{type(exc).__name__}: {exc}"})

    # Kavita's refresh is library-wide and queues a job per call, so the folders
    # are collapsed to one call per library *before* anything is sent. A repair
    # touching thirty series in one library should cost one refresh, not thirty.
    pending = []
    seen_libraries = set()
    for folder in folders:
        library_id = None
        if callable(library_id_for_folder):
            try:
                library_id = library_id_for_folder(folder)
            except Exception:
                library_id = None
        key = library_id if library_id is not None else f"folder:{folder}"
        if key in seen_libraries:
            continue
        seen_libraries.add(key)
        pending.append(folder)

    for folder in pending:
        try:
            summary["kavita_cover_refresh"].append(kavita_cover_refresh(folder))
        except Exception as exc:
            summary["errors"].append(
                {"stage": "kavita_cover_refresh", "folder": folder,
                 "error": f"{type(exc).__name__}: {exc}"}
            )

    # Only where a file actually changed name. Emptying a library's trash is a
    # destructive-sounding operation to fire when nothing was renamed, and a
    # plain rewrite does not need it.
    for folder in sorted(set(renamed_folders)):
        try:
            summary["komga_empty_trash"].append(komga_empty_trash(folder))
        except Exception as exc:
            summary["errors"].append(
                {"stage": "komga_empty_trash", "folder": folder,
                 "error": f"{type(exc).__name__}: {exc}"}
            )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inject a series' front cover as page one of its lowest-numbered archive.",
    )
    parser.add_argument("--archive", action="append", dest="archives",
                        help="Inject into this archive directly. Repeatable.")
    parser.add_argument("--cover-file", help="Read the cover image from this file instead of fetching it.")
    parser.add_argument("--cover-url", help="Fetch the cover from this URL through InkDrop's cover proxy.")
    parser.add_argument("--folder", action="append", dest="folders",
                        help="Resolve the lowest-numbered archive in this series folder and inject there. Repeatable.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually rewrite. Without this the run only reports what it would do.")
    parser.add_argument("--undo", action="store_true", help="Restore retired originals instead of injecting.")
    parser.add_argument("--discard-originals", action="store_true",
                        help="Delete each original after the rewrite validates, instead of retiring it.")
    parser.add_argument("--originals-dir", default=None, help="Where retired originals go.")
    parser.add_argument("--allow-unit-change", action="store_true",
                        help="Inject even when the extra page would change the archive's chapter/volume judgement.")
    parser.add_argument("--json", action="store_true", help="Print the full result document.")
    parser.add_argument("--sweep", action="store_true",
                        help="Run the whole existing library: inject into each series' current lowest volume.")
    parser.add_argument("--series", action="append", dest="series_ids",
                        help="Limit a sweep to these series ids. Repeatable.")
    parser.add_argument("--limit", type=int, default=None, help="Touch at most this many series during a sweep.")
    parser.add_argument("--no-refresh", action="store_true",
                        help="Skip asking Kavita and Komga to re-derive covers afterwards.")
    parser.add_argument("--require-complete", action="store_true",
                        help="Exit non-zero if a sweep could not examine every series in the library.")
    args = parser.parse_args(argv)

    if args.sweep:
        return _run_sweep(args)

    targets = []
    for folder in args.folders or []:
        chosen = resolve_target(series_archives(folder))
        if chosen is None:
            targets.append({"folder": folder, "error": "no_archives_found"})
        else:
            targets.append({"folder": folder, "path": chosen["path"],
                            "unit": chosen["unit"], "number": chosen["number"]})
    for archive in args.archives or []:
        targets.append({"path": Path(archive)})

    if not targets:
        parser.error("give at least one --archive or --folder")

    if args.undo:
        results = [
            remove_injection(item["path"], originals_dir=args.originals_dir, dry_run=not args.apply)
            for item in targets
            if item.get("path")
        ]
        _report(results, args, verb="restore")
        return 0 if all(item.get("ok") for item in results) else 1

    cover_bytes = None
    if args.cover_file:
        cover_bytes = Path(args.cover_file).read_bytes()
    elif args.cover_url:
        try:
            cover_bytes = fetch_cover(args.cover_url)
        except InjectionRefused as exc:
            print(f"{exc.reason}: {exc.detail}", file=sys.stderr)
            return 1
    else:
        parser.error("give --cover-file or --cover-url")

    results = []
    for item in targets:
        if not item.get("path"):
            results.append({"source": item.get("folder"), "reason": item.get("error"), "injected": False})
            continue
        result = inject_archive(
            item["path"],
            cover_bytes,
            marker_fields={"source_url": args.cover_url or "", "unit": item.get("unit") or "",
                           "unit_number": item.get("number") or ""},
            keep_original=not args.discard_originals,
            originals_dir=args.originals_dir,
            dry_run=not args.apply,
            allow_unit_change=args.allow_unit_change,
        )
        if result.get("injected"):
            result["imported_files"] = reconcile_imported_files(
                item["path"], result.get("dest") or item["path"], dry_run=False
            )
        results.append(result)

    _report(results, args, verb="inject")
    return 0 if all(item.get("ok") for item in results) else 1


def _run_sweep(args):
    """The backfill. Loud on purpose -- it rewrites files across the whole library."""

    def progress(event):
        if args.json or event.get("event") != "done":
            return
        index, total = event.get("index"), event.get("total")
        title = event.get("title") or event.get("series_id") or "?"
        reason = event.get("reason") or ""
        if event.get("changed"):
            mark = "injected"
        elif reason.startswith("would_"):
            mark = reason.replace("would_", "would ")
        elif reason == "already_current":
            mark = "ok"
        else:
            mark = reason
        print(f"  [{index}/{total}] {title}: {mark}", flush=True)

    summary = sweep_library(
        series_ids=args.series_ids,
        dry_run=not args.apply,
        limit=args.limit,
        keep_original=not args.discard_originals,
        originals_dir=args.originals_dir,
        allow_unit_change=args.allow_unit_change,
        refresh=not args.no_refresh,
        progress=progress,
    )

    incomplete = not summary.get("complete", True)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        if not summary.get("ok"):
            return 1
        return 2 if (incomplete and args.require_complete) else 0

    mode = "would change" if summary["dry_run"] else "injected"
    print()
    print(f"series in library: {summary.get('series_in_library', summary['series_considered'])}")
    print(f"series considered: {summary['series_considered']} (attempted {summary.get('attempted', 0)})")
    print(f"{mode}: {summary['would_change'] if summary['dry_run'] else summary['injected']}")
    print(f"already correct: {summary['already_current']}")
    print(f"skipped: {summary['skipped']}")
    for reason, count in sorted(summary["skipped_reasons"].items(), key=lambda kv: -kv[1]):
        print(f"    {count:>4}  {reason}")
    print(f"failed: {summary['failed']}")
    for reason, count in sorted(summary["failed_reasons"].items(), key=lambda kv: -kv[1]):
        print(f"    {count:>4}  {reason}")
    print(f"not considered: {summary.get('not_considered', 0)}")
    for reason, count in sorted(summary.get("not_considered_reasons", {}).items(), key=lambda kv: -kv[1]):
        print(f"    {count:>4}  {reason}")
    if summary.get("reader_refresh"):
        errors = summary["reader_refresh"].get("errors") or []
        print(f"reader refresh: {len(summary['changed_folders'])} folders, {len(errors)} errors")
        for error in errors[:5]:
            print(f"    {error}")
    if summary["dry_run"]:
        print("\nnothing was modified. re-run with --apply to write.")

    # The line this whole report exists for. A sweep that never looked at most
    # of the library must not be readable as a library-wide pass, because the
    # next thing that happens is someone closing the backfill out on the
    # strength of a clean run.
    if incomplete:
        considered = summary["series_considered"]
        total = summary.get("series_in_library", considered)
        print(
            f"\nINCOMPLETE: this pass covered {considered} of {total} series. "
            f"{summary['not_considered']} were never examined -- see 'not considered' above. "
            "Do not read this run as a whole-library result."
        )

    if not summary.get("ok"):
        return 1
    return 2 if (incomplete and args.require_complete) else 0


def _report(results, args, verb):
    if args.json:
        print(json.dumps({"schema": COVER_INJECTION_SCHEMA, "results": results},
                         indent=2, sort_keys=True, default=str))
        return
    for item in results:
        if item.get("injected") or item.get("restored"):
            print(f"{verb}ed {item['source']} ({item.get('page_count', '?')} pages)")
        elif item.get("ok"):
            print(f"would {verb} {item['source']} [{item.get('reason')}]")
        else:
            detail = item.get("detail")
            print(f"  {item.get('reason')}: {item['source']}" + (f" {detail}" if detail else ""))


if __name__ == "__main__":
    sys.exit(main())
