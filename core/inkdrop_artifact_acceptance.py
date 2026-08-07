#!/usr/bin/env python3
import hashlib
import io
import re
import warnings
import zipfile
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from PIL import Image, ImageFile

from core import inkdrop_library_identity

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
SENTINEL_TEXT_VALUES = {"-100000", "-100000.0", "1-01-01", "0001", "0001-01-01"}
SENTINEL_NUMBERS = {-100000, -100000.0}

TARGET_TYPES = {
    "issue",
    "chapter",
    "volume",
    "collected_edition",
    "omnibus",
    "pack_member",
    "unknown_unit",
}

GENERIC_ISSUE_TITLES = {
    "issue",
    "one shot",
    "oneshot",
    "trade paperback",
    "tpb",
    "volume",
}

MANGA_STYLE_VOLUME_RE = re.compile(
    r"(?:^|[^a-z0-9])v(?:ol(?:ume)?)?[\s._-]*0*(\d{1,4}(?:\.\d+)?)(?=$|[^a-z0-9])",
    re.I,
)
MANGA_STYLE_CHAPTER_RE = re.compile(
    r"(?:^|[^a-z0-9])c(?:h(?:apter)?)?[\s._-]*0*(\d{1,5}(?:\.\d+)?)(?=$|[^a-z0-9])",
    re.I,
)

ARCHIVE_MEMBER_SEMANTICS_CACHE = OrderedDict()
ARCHIVE_MEMBER_SEMANTICS_CACHE_MAX = 4096
MAX_VERIFIED_IMAGE_BYTES = 16 * 1024 * 1024
# High-resolution collected volumes commonly exceed 256 MiB of declared image
# payload. Validation still reads and verifies every member under the per-image
# and member-count limits; this only raises the bounded aggregate ceiling.
MAX_VERIFIED_ARCHIVE_IMAGE_BYTES = 512 * 1024 * 1024
MAX_VERIFIED_IMAGE_MEMBERS = 2000
MAX_VERIFIED_IMAGE_PIXELS = 40_000_000
# ComicInfo.xml is metadata: a handful of short tags. A megabyte is already
# absurdly generous for that and still small enough that a hostile archive
# cannot use it to exhaust memory. Without a cap this member was read whole
# before anything looked at its size, on bytes that arrive unattended from
# anonymous Soulseek peers and are parsed while deciding whether to ACCEPT the
# download -- so a compressible multi-gigabyte member is reachable before any
# human sees the file. The container has no memory limit, so an OOM there does
# not stop at InkDrop; it takes whatever else the host is running.
MAX_COMICINFO_BYTES = 1 * 1024 * 1024


def read_bounded_archive_member(archive, name, limit):
    """Read one archive member, refusing anything past `limit`.

    Mirrors the per-image guard used by the verifier below: consult the
    DECLARED size first so a decompression bomb is refused before a single byte
    is inflated, then read limit+1 and require the result to match the declared
    size, which catches a member whose header lies about how big it is.
    """
    info = archive.getinfo(name) if isinstance(name, str) else name
    if info.file_size > limit:
        raise ValueError("archive_member_exceeds_bounded_limit")
    with archive.open(info) as member:
        data = member.read(limit + 1)
    if len(data) != info.file_size:
        raise ValueError("archive_member_size_mismatch")
    return data
Image.MAX_IMAGE_PIXELS = MAX_VERIFIED_IMAGE_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False


def comic_archive_suffix(path):
    name = Path(path).name.casefold()
    if name.endswith((".cbz", ".cbz.zip", ".zip")):
        return ".cbz"
    return Path(path).suffix.lower()


def _text(*values):
    return " ".join(str(value or "") for value in values if value not in (None, ""))


def _norm(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _number(value):
    if value in (None, ""):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def _number_text(value):
    number = _number(value)
    if number is None:
        return None
    if isinstance(number, int):
        return str(number)
    return str(number).rstrip("0").rstrip(".")


def _identity_words(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def source_identity_acceptance(source_identity, target=None):
    """Apply the non-content identity veto shared by source handoffs and import."""

    target = target if isinstance(target, dict) else {}
    text = str(source_identity or "").replace("\\", "/")
    volume = MANGA_STYLE_VOLUME_RE.search(text)
    target_compact_tokens = {
        token.casefold()
        for value in (target.get("title"), target.get("series"))
        for token in re.findall(r"[A-Za-z]+\d+", str(value or ""))
    }
    chapter = next(
        (
            match
            for match in MANGA_STYLE_CHAPTER_RE.finditer(text)
            if re.sub(r"[^A-Za-z0-9]", "", match.group(0)).casefold() not in target_compact_tokens
        ),
        None,
    )
    if not (volume and chapter):
        return {"ok": True, "reason": "source_identity_ok"}

    classification_target = dict(target)
    for canonical_key, aliases in (
        ("canonical_media_type", ("canonicalMediaType",)),
        ("work_media_type", ("workMediaType",)),
        ("series_media_type", ("seriesMediaType",)),
        ("media_type", ("mediaType",)),
    ):
        if classification_target.get(canonical_key) in (None, ""):
            classification_target[canonical_key] = next(
                (target.get(alias) for alias in aliases if target.get(alias) not in (None, "")),
                None,
            )
    library_classification = inkdrop_library_identity.canonical_library_classification(classification_target)
    library_type = _norm(library_classification.get("library_type"))
    metadata_provider = _norm(target.get("metadata_provider") or target.get("metadataProvider"))
    target_source = _norm(target.get("target_source") or target.get("source"))
    manga_target = bool(
        library_type == "manga"
        or metadata_provider in {"mangadex", "suwayomi"}
        or target_source in {"mangadex", "suwayomi"}
        or target.get("mangadex_id")
        or target.get("mangadexId")
        or target.get("mangadex_chapter_id")
    )
    unit_type = _norm(
        target.get("unit_type")
        or target.get("unitType")
        or target.get("target_unit_type")
        or target.get("semantic_unit_type")
        or target.get("source_unit")
    )
    issue_number = (
        target.get("issue_number")
        or target.get("issueNumber")
        or target.get("canonical_issue_number")
        or target.get("canonicalIssueNumber")
        or target.get("canonical_number")
        or target.get("canonicalNumber")
        or target.get("target_number")
        or target.get("normalized_number")
        or target.get("issue")
    )
    issue_shaped = bool(
        unit_type in {"issue", "comic_issue", "single_issue"}
        or (issue_number not in (None, "") and unit_type not in {"chapter", "manga_chapter", "volume", "manga_volume"})
    )
    missing_durable_media_identity = library_classification.get("reason") == "missing_durable_media_identity"
    western_issue = not manga_target and issue_shaped and (
        library_type == "comics"
        or missing_durable_media_identity
    )
    if not western_issue:
        return {"ok": True, "reason": "source_identity_ok"}
    return {
        "ok": False,
        "reason": "wrong_unit_type_chapter_for_comic_issue",
        "detail": "Manga-style volume/chapter source identity cannot satisfy a western comic issue.",
        "source_volume_number": _number_text(volume.group(1)),
        "source_chapter_number": _number_text(chapter.group(1)),
        "expected_issue_number": _number_text(issue_number),
    }


def comicinfo_target_conflicts(comicinfo, expected_series=None, expected_number=None, target_type=None):
    comicinfo = comicinfo if isinstance(comicinfo, dict) else {}
    if not comicinfo.get("authoritative"):
        return []
    conflicts = []
    actual_series = _identity_words(comicinfo.get("series"))
    target_series = _identity_words(expected_series)
    if actual_series and target_series and actual_series != target_series:
        conflicts.append("comicinfo_series_does_not_match_target")
    target_type = _norm(target_type)
    if target_type in {"issue", "comic_issue", "single_issue", "chapter", "manga_chapter"}:
        actual_number = _number_text(comicinfo.get("number"))
        number_reason = (
            "comicinfo_issue_number_does_not_match_target"
            if target_type in {"issue", "comic_issue", "single_issue"}
            else "comicinfo_chapter_number_does_not_match_target"
        )
    elif _norm(comicinfo.get("semantic_unit")) == "chapter":
        actual_number = _number_text(comicinfo.get("number"))
        number_reason = "comicinfo_chapter_number_does_not_match_target"
    else:
        actual_number = _number_text(comicinfo.get("volume"))
        number_reason = "comicinfo_volume_does_not_match_target"
    target_number = _number_text(expected_number)
    if actual_number and target_number and actual_number != target_number:
        conflicts.append(number_reason)
    return conflicts


def _credible_image_dimensions(data, suffix):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                dimensions = image.size
                if dimensions[0] * dimensions[1] > MAX_VERIFIED_IMAGE_PIXELS:
                    return None
                image.load()
                return dimensions
    except Exception:
        return None


def trusted_issue_subtitle_matches_release(series_title, release_title, issue_title, issue_number):
    expected_number = _number_text(issue_number)
    canonical_subtitle = _identity_words(issue_title)
    series_words = re.findall(r"[a-z0-9]+", str(series_title or "").casefold())
    if (
        not expected_number
        or not canonical_subtitle
        or canonical_subtitle in GENERIC_ISSUE_TITLES
        or not series_words
    ):
        return False

    title_text = str(release_title or "").strip()
    suffix = Path(title_text).suffix.lower()
    if suffix in {".cbz", ".cbr", ".pdf", ".epub"}:
        title_text = title_text[:-len(suffix)]
    series_pattern = r"[\W_]+".join(re.escape(word) for word in series_words)
    match = re.match(rf"^\s*{series_pattern}(?P<tail>.*)$", title_text, re.I)
    if not match:
        return False
    tail = match.group("tail") or ""
    numbered_tail = re.match(
        r"^\s*(?:(?:issue|iss|no|number)\.?\s*)?#?\s*0*(?P<number>\d+(?:\.\d+)?)"
        r"\s*(?:[-:._–—]|\s)+\s*(?P<subtitle>.+?)\s*$",
        tail,
        re.I,
    )
    if numbered_tail:
        return bool(
            _number_text(numbered_tail.group("number")) == expected_number
            and _identity_words(numbered_tail.group("subtitle")) == canonical_subtitle
        )
    # Some releases put the subtitle before the issue number instead of after
    # it ("Series - Rite of Spring 006" rather than "Series 006 - Rite of
    # Spring") -- try that order too before giving up.
    subtitled_tail = re.match(
        r"^\s*(?:[-:._–—]|\s)+\s*(?P<subtitle>.+?)\s+#?\s*0*(?P<number>\d+(?:\.\d+)?)\b.*$",
        tail,
        re.I,
    )
    if not subtitled_tail:
        return False
    return bool(
        _number_text(subtitled_tail.group("number")) == expected_number
        and _identity_words(subtitled_tail.group("subtitle")) == canonical_subtitle
    )


_LEADING_NUMERIC_PREFIX_RE = re.compile(r"^\s*0*(\d{1,5})(?:\.\d+)?[\s_.:\-]+(?=\S)")
_TRUSTED_UNIT_AFTER_TITLE_RE = re.compile(
    r"^[\s:._\-]*(?:#|no\.?|num(?:ber)?\.?|issue|iss)?[\s._-]*0*(\d{1,5}(?:\.\d+)?)"
    r"(?:\s*(?:\(\s*of\s*0*(\d{1,4})\s*\)|of\s*0*(\d{1,4})|/\s*0*(\d{1,4})))?",
    re.I,
)


def trusted_numeric_prefix_import_is_safe(stem, trusted_aliases, trusted_issue):
    """Exempt a bare leading number from the weak-numeric-prefix rejection
    only when it is provably reading-order/collection metadata rather than an
    unrelated file, shared by the SLSKD staged matcher and the final import
    classifier so the two never diverge again.

    Two library conventions are both legitimate and are both recognized here:

    1. Collection ordinal before an exact title before the real unit, e.g.
       "1. House of X 01 (of 06) ..." or "18 - Ms. Marvel 007 (2016) ...".
       The leading number is discarded once everything after it begins with
       the complete canonical title/alias and the next unit number after that
       exact title is the trusted issue. A well-formed, bounded "(of N)"/
       "of N" collection-size suffix is allowed after the unit.
    2. Exact unit before an exact title with nothing else, e.g.
       "0012 -  - Deadman Wonderland.cbz". The leading number IS the trusted
       unit, and the title immediately follows with no contradicting number.

    Both require the remainder to BEGIN with the complete title -- a title
    that merely appears later ("18 Dark Web - Ms. Marvel 001 (2023)") does
    not count, which is what keeps unrelated collections and mismatched
    issues rejected. This only rescinds the bare-numeric-prefix veto; every
    other identity/pack/duplicate/range/ComicInfo/related-subseries gate the
    caller already runs still applies untouched.
    """

    expected = _number_text(trusted_issue)
    if not expected:
        return False
    text = str(stem or "").replace("_", " ")
    prefix_match = _LEADING_NUMERIC_PREFIX_RE.match(text)
    if not prefix_match:
        return False
    remainder = text[prefix_match.end():]
    remainder_words = re.findall(r"[a-z0-9]+", remainder.casefold())

    best_alias_words = None
    for alias in trusted_aliases or []:
        alias_words = re.findall(r"[a-z0-9]+", str(alias or "").casefold())
        if not alias_words:
            continue
        if remainder_words[: len(alias_words)] != alias_words:
            continue
        if best_alias_words is None or len(alias_words) > len(best_alias_words):
            best_alias_words = alias_words
    if best_alias_words is None:
        return False

    alias_pattern = r"[\W_]+".join(re.escape(word) for word in best_alias_words)
    title_match = re.match(rf"^\s*{alias_pattern}(?P<tail>.*)$", remainder, re.I)
    if not title_match:
        return False
    tail = title_match.group("tail")

    def of_total_is_well_formed(match):
        of_total = match.group(2) or match.group(3) or match.group(4)
        if of_total is None:
            return True
        try:
            total = int(of_total)
        except ValueError:
            return False
        if not (1 <= total <= 999):
            return False
        try:
            unit_value = int(float(match.group(1)))
        except ValueError:
            unit_value = None
        return unit_value is None or total >= unit_value

    # Case 1: the leading number IS the trusted unit and the exact title
    # follows immediately with no other numbered unit contradicting it.
    if _number_text(prefix_match.group(1)) == expected:
        trailing_unit = _TRUSTED_UNIT_AFTER_TITLE_RE.match(tail)
        if not trailing_unit:
            return True
        if _number_text(trailing_unit.group(1)) == expected and of_total_is_well_formed(trailing_unit):
            return True

    # Case 2: the leading number is reading-order/collection metadata, and
    # the unit immediately after the exact title is the trusted issue.
    unit_match = _TRUSTED_UNIT_AFTER_TITLE_RE.match(tail)
    if not unit_match:
        return False
    if _number_text(unit_match.group(1)) != expected:
        return False
    return of_total_is_well_formed(unit_match)


def _xml_escape(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sentinel_value(value):
    if value in (None, ""):
        return False
    if value in SENTINEL_NUMBERS:
        return True
    raw = str(value).strip()
    if raw in SENTINEL_TEXT_VALUES:
        return True
    if raw.startswith("0001-"):
        return True
    return False


def safe_metadata_value(value, *, field=""):
    if sentinel_value(value):
        return None
    if field.lower() == "year":
        text = str(value or "").strip()
        if text and re.fullmatch(r"\d{4}", text):
            try:
                year = int(text)
            except ValueError:
                return None
            if year <= 1:
                return None
        elif text:
            return None
    return value


def comicinfo_node(name, value):
    value = safe_metadata_value(value, field=name)
    if value is None or value == "":
        return ""
    return f"  <{name}>{_xml_escape(value)}</{name}>\n"


def sanitized_comicinfo_xml(nodes, extra_lines=None):
    out = ["<?xml version=\"1.0\" encoding=\"utf-8\"?>\n", "<ComicInfo>\n"]
    for name, value in nodes:
        out.append(comicinfo_node(name, value))
    for line in extra_lines or []:
        if line:
            out.append(line if line.endswith("\n") else f"{line}\n")
    out.append("</ComicInfo>\n")
    return "".join(out)


def source_text_for_path(path):
    path = Path(path)
    return _text(path.stem, path.parent.name, *path.parts[-2:])


def classify_target(target=None, event=None, row=None, collection=None):
    target = target if isinstance(target, dict) else {}
    event = event if isinstance(event, dict) else {}
    row = row if isinstance(row, dict) else {}
    source_unit = _norm(event.get("source_unit") or row.get("source_unit") or row.get("unit_type"))
    target_unit = _norm(
        event.get("target_unit_type")
        or row.get("target_unit_type")
        or target.get("unit_type")
        or target.get("semantic_unit_type")
    )
    if target_unit == "collected_edition":
        target_type = "collected_edition"
    elif target_unit == "chapter":
        target_type = "chapter"
    elif target_unit == "volume":
        target_type = "volume"
    elif target_unit in {"issue", "single_issue"}:
        target_type = "issue"
    elif collection or source_unit == "collected_edition":
        target_type = "collected_edition"
    elif source_unit == "chapter":
        target_type = "chapter"
    elif source_unit == "volume":
        target_type = "volume"
    else:
        hint = _norm(_text(target.get("issue_title"), event.get("canonical_issue_title"), target.get("title")))
        target_type = "volume" if re.search(r"\b(?:book|volume|vol)\b", hint) else "issue"
    if target_type not in TARGET_TYPES:
        target_type = "unknown_unit"
    target_number = (
        event.get("target_number")
        or event.get("canonical_issue_number")
        or row.get("issue_number")
        or target.get("issue_number")
        or target.get("normalized_number")
        or target.get("trusted_issue")
    )
    return {
        "target_type": target_type,
        "target_number": _number_text(target_number),
        "title": target.get("title") or event.get("matched_series"),
    }


def artifact_number_from_text(text):
    text = str(text or "")
    patterns = [
        r"\b(?:issue|chapter|ch|volume|vol|v)[\s._-]*0*(\d{1,5}(?:\.\d+)?)\b",
        r"(?:^|[\s._\-\(\[])#\s*0*(\d{1,5}(?:\.\d+)?)\b",
        r"(?:^|[\s._-])0*(\d{1,5}(?:\.\d+)?)(?:[\s._-]|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _number_text(match.group(1))
    return None


def page_manifest(path, member_semantics=None):
    # Not fresh=True: this and decide_acceptance() both independently
    # re-decode and sha256-hash every page image in the archive (real,
    # measured cost -- 1.28s of a 1.85s import for a 27-page comic, all in
    # PIL image verify+load) when called for the same file moments apart in
    # the same import_files() run. The cache key is (path, mtime_ns, size),
    # which already invalidates itself if the file changes, so there's
    # nothing this call needs "fresh" that the cache doesn't already give it.
    semantics = member_semantics if isinstance(member_semantics, dict) else archive_member_semantics(path)
    if semantics.get("archive_integrity") != "fully_checked":
        return None
    return {
        "page_count": semantics.get("credible_image_count"),
        "ordered_page_manifest_hash": semantics.get("ordered_page_manifest_hash"),
        "archive_member_manifest_hash": semantics.get("archive_member_manifest_hash"),
    }


def _archive_unit_numbers(filename, unit):
    if unit == "chapter":
        marker = r"(?:chapter|ch|c)"
    elif unit == "volume":
        marker = r"(?:volume|vol)"
    else:
        return []
    pattern = rf"(?:^|[^a-z0-9]){marker}[\s._-]*0*(\d{{1,5}}(?:\.\d+)?)(?=[^0-9]|$)"
    return [_number_text(match.group(1)) for match in re.finditer(pattern, str(filename or ""), re.I)]


def archive_central_directory_light_signature(path):
    """Signature of the archive's image members from the ZIP central directory.

    Identical construction to the central_directory_signature that
    archive_member_semantics persists, but read from the central directory
    alone -- no member decompression, no image validation -- so comparing a
    stored signature against the file on disk costs milliseconds instead of
    the full per-page read. This is what lets the wrong-unit cleanup skip
    already-audited archives without going blind to an in-place replacement
    that preserves size and mtime.
    """
    path = Path(path)
    if comic_archive_suffix(path) != ".cbz" or not path.is_file():
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            # EVERY member, not just images: a ComicInfo.xml-only rewrite
            # (volume -> chapter metadata) keeps size, mtime, and every image
            # entry identical, so an image-only signature waves it through as
            # unchanged (PASS14 re-audit of PASS12-CORE-P1-01). Must match the
            # construction in archive_member_semantics exactly.
            signature_entries = [
                info for info in archive.infolist() if not info.is_dir()
            ]
            return hashlib.sha256(
                "\n".join(
                    f"{info.filename}\0{info.CRC}\0{info.file_size}\0{info.compress_size}"
                    for info in signature_entries
                ).encode("utf-8", "surrogateescape")
            ).hexdigest()
    except Exception:
        return None


def archive_member_semantics(path, *, fresh=False):
    path = Path(path)
    result = {
        "checked": False,
        "semantic_unit": None,
        "chapter_numbers": [],
        "volume_numbers": [],
        "image_count": 0,
        "image_payload_bytes": 0,
        "credible_image_count": 0,
        "credible_image_payload_bytes": 0,
        "image_validation_errors": [],
        "chapter_marker_count": 0,
        "chapter_marker_coverage": 0.0,
        "volume_marker_count": 0,
        "volume_marker_coverage": 0.0,
        "comicinfo": {
            "present": False,
            "authoritative": False,
            "semantic_unit": None,
            "series": None,
            "title": None,
            "number": None,
            "volume": None,
            "format": None,
            "evidence": [],
            "parse_error": None,
        },
        "evidence": [],
    }
    if comic_archive_suffix(path) != ".cbz" or not path.is_file():
        return result
    try:
        stat = path.stat()
        cache_key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return result
    cached = None if fresh else ARCHIVE_MEMBER_SEMANTICS_CACHE.get(cache_key)
    if isinstance(cached, dict):
        ARCHIVE_MEMBER_SEMANTICS_CACHE.move_to_end(cache_key)
        cached_comicinfo = cached.get("comicinfo") if isinstance(cached.get("comicinfo"), dict) else {}
        return {
            **cached,
            "chapter_numbers": list(cached.get("chapter_numbers") or []),
            "volume_numbers": list(cached.get("volume_numbers") or []),
            "comicinfo": {**cached_comicinfo, "evidence": list(cached_comicinfo.get("evidence") or [])},
            "evidence": list(cached.get("evidence") or []),
        }
    try:
        with zipfile.ZipFile(path) as archive:
            image_entries = [
                info
                for info in archive.infolist()
                if not info.is_dir() and Path(info.filename).suffix.lower() in IMAGE_EXTS
            ]
            signature_entries = [
                info for info in archive.infolist() if not info.is_dir()
            ]
            image_names = [info.filename for info in image_entries]
            credible_payload = 0
            fully_read = 0
            verified_pages = []
            declared_payload = sum(max(0, int(info.file_size or 0)) for info in image_entries)
            budget_error = len(image_entries) > MAX_VERIFIED_IMAGE_MEMBERS or declared_payload > MAX_VERIFIED_ARCHIVE_IMAGE_BYTES
            if budget_error:
                result["image_validation_errors"].append("archive_image_validation_budget_exceeded")
            else:
                for info in image_entries:
                    try:
                        if info.file_size > MAX_VERIFIED_IMAGE_BYTES:
                            raise ValueError("image_exceeds_bounded_verifier_limit")
                        with archive.open(info) as member:
                            data = member.read(MAX_VERIFIED_IMAGE_BYTES + 1)
                        if len(data) != info.file_size:
                            raise ValueError("image_member_size_mismatch")
                        fully_read += 1
                        width, height = _credible_image_dimensions(data, Path(info.filename).suffix) or (0, 0)
                        if info.file_size < 32 or not (8 <= width <= 20000 and 8 <= height <= 20000) or max(width, height) / min(width, height) > 100:
                            raise ValueError("implausible_image")
                        credible_payload += int(info.file_size)
                        verified_pages.append((info.filename, hashlib.sha256(data).hexdigest()))
                    except Exception as exc:
                        result["image_validation_errors"].append(f"unreadable_image:{info.filename}:{type(exc).__name__}")
            all_valid = bool(image_entries) and not result["image_validation_errors"] and fully_read == len(image_entries)
            result["credible_image_count"] = len(image_entries) if all_valid else 0
            result["credible_image_payload_bytes"] = credible_payload if all_valid else 0
            result["image_validation_checked_count"] = fully_read
            result["archive_integrity"] = "fully_checked" if all_valid else ("budget_exceeded" if budget_error else "failed")
            if all_valid:
                page_digest = hashlib.sha256()
                member_digest = hashlib.sha256()
                for filename, page_hash in sorted(verified_pages, key=lambda item: item[0].lower()):
                    page_digest.update(page_hash.encode("ascii") + b"\n")
                    member_digest.update(filename.encode("utf-8", "surrogateescape") + b"\0")
                result["ordered_page_manifest_hash"] = page_digest.hexdigest()
                result["archive_member_manifest_hash"] = member_digest.hexdigest()
            comicinfo_names = [
                info.filename
                for info in archive.infolist()
                if not info.is_dir() and Path(info.filename).name.casefold() == "comicinfo.xml"
            ]
            comicinfo = dict(result["comicinfo"])
            if comicinfo_names:
                comicinfo["present"] = True
                if len(comicinfo_names) > 1:
                    comicinfo["semantic_unit"] = "conflicting"
                    comicinfo["evidence"].append("multiple_comicinfo_documents")
                else:
                    try:
                        root = ET.fromstring(
                            read_bounded_archive_member(
                                archive, comicinfo_names[0], MAX_COMICINFO_BYTES
                            )
                        )
                        for tag, key in (("Series", "series"), ("Title", "title"), ("Number", "number"), ("Volume", "volume"), ("Format", "format")):
                            node = root.find(tag)
                            comicinfo[key] = node.text.strip() if node is not None and node.text else None
                        chapter_hint = "chapter" in _norm(comicinfo.get("format")) or bool(
                            re.search(r"\b(?:chapter|ch)[\s._-]*\d+", _norm(comicinfo.get("title")), re.I)
                        )
                        volume_number = _number_text(comicinfo.get("volume"))
                        number = _number_text(comicinfo.get("number"))
                        if chapter_hint and volume_number:
                            comicinfo["semantic_unit"] = "conflicting"
                            comicinfo["evidence"].append("comicinfo_chapter_and_volume_conflict")
                        elif chapter_hint and number:
                            comicinfo["semantic_unit"] = "chapter"
                            comicinfo["evidence"].append("comicinfo_explicit_chapter")
                        elif volume_number and _identity_words(comicinfo.get("series")):
                            comicinfo["semantic_unit"] = "volume"
                            comicinfo["evidence"].append("comicinfo_series_and_volume")
                        elif volume_number:
                            comicinfo["evidence"].append("comicinfo_volume_without_series")
                    except Exception as exc:
                        comicinfo["parse_error"] = f"{type(exc).__name__}: {exc}"
                        comicinfo["evidence"].append("comicinfo_parse_failed")
            result["comicinfo"] = comicinfo
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    chapter_counts = {}
    volume_counts = {}
    chapter_marked_images = 0
    volume_marked_images = 0
    for name in image_names:
        chapter_hits = {number for number in _archive_unit_numbers(name, "chapter") if number}
        volume_hits = {number for number in _archive_unit_numbers(name, "volume") if number}
        if chapter_hits:
            chapter_marked_images += 1
        if volume_hits:
            volume_marked_images += 1
        for number in chapter_hits:
            chapter_counts[number] = chapter_counts.get(number, 0) + 1
        for number in volume_hits:
            volume_counts[number] = volume_counts.get(number, 0) + 1
    chapter_numbers = sorted(chapter_counts, key=lambda value: float(value))
    volume_numbers = sorted(volume_counts, key=lambda value: float(value))
    image_count = len(image_names)
    image_payload_bytes = sum(max(0, int(info.file_size or 0)) for info in image_entries)
    comicinfo = result["comicinfo"]
    if comicinfo.get("semantic_unit") in {"chapter", "volume"}:
        unit_number = comicinfo.get("number") if comicinfo.get("semantic_unit") == "chapter" else comicinfo.get("volume")
        if (
            result.get("credible_image_count")
            and result.get("credible_image_payload_bytes")
            and _identity_words(comicinfo.get("series"))
            and _number_text(unit_number)
        ):
            comicinfo["authoritative"] = True
        else:
            comicinfo["semantic_unit"] = None
            comicinfo["evidence"].append("comicinfo_unit_without_identity_or_image_payload")
    chapter_coverage = chapter_marked_images / image_count if image_count else 0.0
    volume_coverage = volume_marked_images / image_count if image_count else 0.0
    result.update(
        {
            "checked": True,
            "chapter_numbers": chapter_numbers,
            "volume_numbers": volume_numbers,
            "image_count": image_count,
            "image_payload_bytes": image_payload_bytes,
            "chapter_marker_count": chapter_marked_images,
            "chapter_marker_coverage": round(chapter_coverage, 6),
            "volume_marker_count": volume_marked_images,
            "volume_marker_coverage": round(volume_coverage, 6),
            # Every member, matching archive_central_directory_light_signature:
            # image-only hashing let a ComicInfo.xml-only rewrite pass as
            # unchanged. Existing stamps carry the old format and mismatch
            # once, forcing a single bounded re-audit that re-stamps them.
            "central_directory_signature": hashlib.sha256(
                "\n".join(
                    f"{info.filename}\0{info.CRC}\0{info.file_size}\0{info.compress_size}"
                    for info in signature_entries
                ).encode("utf-8", "surrogateescape")
            ).hexdigest(),
        }
    )
    minimum_dominant_markers = 2 if image_count <= 4 else 3
    dominant_single_chapter = bool(
        len(chapter_numbers) == 1
        and not volume_numbers
        and chapter_marked_images >= minimum_dominant_markers
        and chapter_coverage >= 0.60
    )
    if dominant_single_chapter:
        result["semantic_unit"] = "chapter"
        result["evidence"].append("dominant_single_explicit_chapter_across_archive_members")
    elif len(chapter_numbers) == 1 and not volume_numbers:
        result["evidence"].append("sparse_single_chapter_marker_not_unit_proof")
    elif len(chapter_numbers) > 1 and not volume_numbers:
        result["semantic_unit"] = "multi_chapter_archive"
        result["evidence"].append("multiple_explicit_chapters_across_archive_members")
    elif volume_numbers and not chapter_numbers:
        result["semantic_unit"] = "volume"
        result["evidence"].append("explicit_volume_across_archive_members")
    elif chapter_numbers and volume_numbers:
        result["semantic_unit"] = "conflicting"
        result["evidence"].append("conflicting_chapter_and_volume_member_markers")
    comicinfo_unit = _norm((result.get("comicinfo") or {}).get("semantic_unit"))
    member_unit = _norm(result.get("semantic_unit"))
    if comicinfo_unit == "conflicting":
        result["semantic_unit"] = "conflicting"
        result["evidence"].append("conflicting_comicinfo_unit_metadata")
    elif comicinfo_unit == "chapter":
        if member_unit in {"volume", "multi_chapter_archive"}:
            result["semantic_unit"] = "conflicting"
            result["evidence"].append("comicinfo_chapter_conflicts_with_volume_members")
        elif not member_unit:
            result["semantic_unit"] = "chapter"
            result["evidence"].append("authoritative_comicinfo_chapter")
    elif comicinfo_unit == "volume":
        if member_unit == "chapter":
            result["semantic_unit"] = "conflicting"
            result["evidence"].append("comicinfo_volume_conflicts_with_chapter_members")
        elif not member_unit:
            result["semantic_unit"] = "volume"
            result["evidence"].append("authoritative_comicinfo_volume")
    if len(ARCHIVE_MEMBER_SEMANTICS_CACHE) >= ARCHIVE_MEMBER_SEMANTICS_CACHE_MAX:
        ARCHIVE_MEMBER_SEMANTICS_CACHE.popitem(last=False)
    ARCHIVE_MEMBER_SEMANTICS_CACHE[cache_key] = result
    ARCHIVE_MEMBER_SEMANTICS_CACHE.move_to_end(cache_key)
    comicinfo = result.get("comicinfo") if isinstance(result.get("comicinfo"), dict) else {}
    return {
        **result,
        "chapter_numbers": list(result["chapter_numbers"]),
        "volume_numbers": list(result["volume_numbers"]),
        "comicinfo": {**comicinfo, "evidence": list(comicinfo.get("evidence") or [])},
        "evidence": list(result["evidence"]),
    }


def classify_artifact(path, archive_check=None, source_unit=None, collection=None, member_semantics=None):
    path = Path(path)
    archive_check = archive_check if isinstance(archive_check, dict) else {}
    if archive_check and not archive_check.get("ok", True):
        return "corrupt_archive"
    text = _norm(source_text_for_path(path))
    page_count = int(archive_check.get("page_count") or 0)
    source_unit = _norm(source_unit)
    member_semantics = member_semantics if isinstance(member_semantics, dict) else archive_member_semantics(path)
    semantic_unit = _norm(member_semantics.get("semantic_unit"))
    if re.search(r"\b(?:sample|excerpt|sampler)\b", text):
        return "sample"
    if re.search(r"\bpreview\b.*\b(?:chapter|ch|page|pages|sample|excerpt)\b", text) or re.search(
        r"\b(?:chapter|ch|page|pages|sample|excerpt)\b.*\bpreview\b", text
    ):
        return "preview"
    if page_count == 1 or re.search(r"\b(?:cover only|cover-only)\b", text):
        return "cover_only_artifact"
    if collection or source_unit == "collected_edition" or re.search(r"\b(?:omnibus|library edition|collected edition|collection)\b", text):
        return "omnibus" if "omnibus" in text else "collected_edition"
    if semantic_unit == "chapter":
        return "chapter"
    if semantic_unit == "conflicting":
        return "unknown"
    if source_unit == "chapter" or re.search(r"\b(?:chapter|ch)[\s._-]*\d+", text):
        return "chapter"
    if source_unit == "volume" or re.search(r"\b(?:volume|vol|v)[\s._-]*0*\d+", text):
        return "volume"
    if re.search(r"(?:^|[\s._\-\(\[])#\s*\d|\bissue[\s._-]*\d", text):
        return "single_issue"
    if re.search(r"\b(?:page pack|page-pack|partial)\b", text):
        return "partial_page_pack"
    return "unknown"


def volume_completeness_decision(page_count, payload_size, text, artifact_type):
    reasons = []
    confidence = "medium"
    partial = False
    expected_min = None
    expected_source = None
    if artifact_type in {"preview", "sample", "partial_page_pack", "cover_only_artifact"}:
        partial = True
        reasons.append(f"artifact_classified_as_{artifact_type}")
        confidence = "low"
    if page_count and page_count < 12:
        partial = True
        reasons.append("volume_has_too_few_pages_for_zero_touch")
        expected_min = 12
        expected_source = "target_aware_volume_safety_floor"
        confidence = "low"
    if payload_size and payload_size < 8 * 1024 * 1024:
        reasons.append("volume_payload_suspiciously_small")
        confidence = "low"
    if re.search(r"\b(?:sample|excerpt|sampler)\b", text, re.I) or re.search(
        r"\bpreview\b.*\b(?:chapter|ch|page|pages|sample|excerpt)\b", text, re.I
    ) or re.search(r"\b(?:chapter|ch|page|pages|sample|excerpt)\b.*\bpreview\b", text, re.I):
        partial = True
        reasons.append("preview_or_sample_name")
        confidence = "low"
    return {
        "completeness_confidence": confidence,
        "completeness_reasons": reasons,
        "page_count_expected_min": expected_min,
        "page_count_expected_source": expected_source,
        "partial_artifact_suspected": partial,
    }


def decide_acceptance(path, target=None, event=None, row=None, archive_check=None, collection=None, source_unit=None):
    path = Path(path)
    event = event if isinstance(event, dict) else {}
    row = row if isinstance(row, dict) else {}
    declared_source_unit = source_unit or event.get("source_unit") or row.get("source_unit")
    target_info = classify_target(target, event=event, row=row, collection=collection)
    archive_check = archive_check if isinstance(archive_check, dict) else {}
    # Not fresh=True -- see page_manifest()'s comment. classify_artifact()
    # (the peer function in this same acceptance pipeline) already trusts
    # this cache by default; this call was the redundant one, independently
    # re-decoding+re-hashing every page find_artifact_bad_content_memory()
    # already just decoded moments earlier in the same import.
    member_semantics = archive_member_semantics(path)
    member_unit = _norm(member_semantics.get("semantic_unit"))
    member_chapter_numbers = list(member_semantics.get("chapter_numbers") or [])
    member_volume_numbers = list(member_semantics.get("volume_numbers") or [])
    hierarchical_member_chapter = bool(
        target_info.get("target_type") == "chapter"
        and member_unit == "conflicting"
        and len(member_chapter_numbers) == 1
        and len(member_volume_numbers) == 1
    )
    cbz_verified = bool(
        comic_archive_suffix(path) != ".cbz"
        or (
            member_semantics.get("archive_integrity") == "fully_checked"
            and int(member_semantics.get("credible_image_count") or 0) == int(member_semantics.get("image_count") or 0) > 0
            and not member_semantics.get("image_validation_errors")
        )
    )
    artifact_type = classify_artifact(
        path,
        archive_check=archive_check,
        source_unit=declared_source_unit if cbz_verified else None,
        collection=collection,
        member_semantics=member_semantics,
    )
    if hierarchical_member_chapter:
        artifact_type = "chapter"
    page_count = int(archive_check.get("page_count") or 0)
    payload_size = int(archive_check.get("payload_size") or 0)
    text = source_text_for_path(path)
    artifact_number = artifact_number_from_text(text)
    decision = "accepted"
    reasons = []
    quarantine = False
    retry = True
    completion = True
    if artifact_type == "corrupt_archive" or not archive_check.get("ok", True):
        decision = "rejected_corrupt_archive"
        reasons.append(archive_check.get("reason") or "corrupt_archive")
    elif not cbz_verified:
        decision = "rejected_invalid_image_payload"
        reasons.append(f"archive_image_validation_{member_semantics.get('archive_integrity') or 'failed'}")
    elif artifact_type in {"preview", "sample", "cover_only_artifact"}:
        decision = "rejected_preview_or_sample"
        reasons.append(f"artifact_classified_as_{artifact_type}")
    elif artifact_type == "partial_page_pack":
        decision = "rejected_partial_artifact"
        reasons.append("partial_page_pack")
    target_type = target_info["target_type"]
    acceptance_target = target if isinstance(target, dict) else {}
    source_identity_gate = source_identity_acceptance(text, {**acceptance_target, **target_info})
    comicinfo = member_semantics.get("comicinfo") if isinstance(member_semantics.get("comicinfo"), dict) else {}
    identity_conflicts = comicinfo_target_conflicts(
        comicinfo,
        target_info.get("title"),
        target_info.get("target_number"),
        target_type=target_type,
    )
    if decision == "accepted" and not source_identity_gate.get("ok"):
        decision = "rejected_source_identity"
        reasons.append(source_identity_gate.get("reason") or "source_identity_rejected")
    if decision == "accepted" and identity_conflicts:
        decision = "rejected_metadata_identity"
        reasons.extend(identity_conflicts)
    if decision == "accepted" and member_unit == "conflicting" and not hierarchical_member_chapter:
        decision = "rejected_wrong_unit_type"
        reasons.append("conflicting_archive_member_unit_identity")
    if decision == "accepted" and target_type == "issue" and member_unit == "multi_chapter_archive":
        decision = "rejected_wrong_unit_type"
        reasons.append("issue_target_cannot_use_multi_chapter_archive")
    if decision == "accepted":
        if target_type == "volume" and artifact_type not in {"volume"}:
            decision = "rejected_wrong_unit_type"
            reasons.append(f"volume_target_cannot_use_{artifact_type}")
        elif target_type == "chapter" and artifact_type not in {"chapter"}:
            decision = "rejected_wrong_unit_type"
            reasons.append(f"chapter_target_cannot_use_{artifact_type}")
        elif target_type == "issue" and artifact_type in {"chapter", "multi_chapter_archive"}:
            decision = "rejected_wrong_unit_type"
            reasons.append(f"issue_target_cannot_use_{artifact_type}")
        elif target_type == "issue" and artifact_type in {"collected_edition", "omnibus"}:
            decision = "manual_review_required"
            reasons.append("collected_edition_does_not_satisfy_single_issue_by_default")
        elif target_type == "collected_edition" and artifact_type not in {"collected_edition", "omnibus"}:
            decision = "rejected_wrong_unit_type"
            reasons.append(f"collected_target_cannot_use_{artifact_type}")
    if decision == "accepted":
        target_number = target_info.get("target_number")
        member_numbers = []
        if target_type == "chapter" and artifact_type == "chapter":
            member_numbers = list(member_semantics.get("chapter_numbers") or [])
        elif target_type == "volume" and artifact_type == "volume":
            member_numbers = list(member_semantics.get("volume_numbers") or [])
        interpreted_number = member_numbers[0] if len(member_numbers) == 1 else artifact_number
        if target_number and interpreted_number and target_number != interpreted_number:
            decision = "rejected_wrong_unit_number"
            reasons.append(
                "archive_member_number_does_not_match_target"
                if member_numbers
                else "artifact_number_does_not_match_target"
            )
        artifact_number = interpreted_number
    completeness = volume_completeness_decision(page_count, payload_size, text, artifact_type)
    if decision == "accepted" and target_type == "volume" and completeness["partial_artifact_suspected"]:
        decision = "rejected_partial_artifact"
        reasons.extend(completeness["completeness_reasons"])
    if decision != "accepted":
        quarantine = decision not in {"manual_review_required"}
        completion = False
    # Content identity is target-independent evidence.  Preserve it for rejected
    # artifacts too so durable bad-content memory survives path/provider changes.
    manifest = (
        page_manifest(path, member_semantics)
        if member_semantics.get("archive_integrity") == "fully_checked"
        else None
    )
    return {
        "decision": decision,
        "reason_codes": sorted(set(reasons)),
        "target_type": target_type,
        "artifact_type": artifact_type,
        "target_number": target_info.get("target_number"),
        "interpreted_artifact_number": artifact_number,
        "title_evidence": {"path": path.name, "series": target_info.get("title")},
        "source_identity_gate": source_identity_gate,
        "volume_issue_chapter_evidence": {
            "source_unit": declared_source_unit,
            "target_unit": target_type,
            "archive_members": member_semantics,
        },
        "page_count": page_count,
        "file_size": path.stat().st_size if path.exists() else None,
        "archive_validity": bool(archive_check.get("ok", True)),
        "content_manifest_hash": (manifest or {}).get("ordered_page_manifest_hash"),
        "archive_member_manifest_hash": (manifest or {}).get("archive_member_manifest_hash"),
        "content_hash": None,
        "metadata_validity": "conflicting" if identity_conflicts else ("authoritative" if comicinfo.get("authoritative") else "unknown"),
        "coverage_metadata": {
            "collection_range": event.get("collection_range") or row.get("collection_range") or (collection or {}).get("range"),
            "descriptive_only": bool(collection or event.get("collection_range") or row.get("collection_range")),
        },
        "confidence": "high" if decision == "accepted" else "low",
        "quarantine_required": quarantine,
        "retry_eligible": retry,
        "completion_eligible": completion,
        **completeness,
    }


def sanitized_decision(decision):
    out = {}
    for key in (
        "decision",
        "reason_codes",
        "target_type",
        "artifact_type",
        "target_number",
        "interpreted_artifact_number",
        "page_count",
        "file_size",
        "archive_validity",
        "completeness_confidence",
        "completeness_reasons",
        "page_count_expected_min",
        "page_count_expected_source",
        "partial_artifact_suspected",
        "quarantine_required",
        "retry_eligible",
        "completion_eligible",
    ):
        out[key] = decision.get(key)
    return out


PUBLICATION_MONTH_WORDS = {
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}

PUBLICATION_RELEASE_GROUP_WORDS = {
    "1r0n",
    "jko",
    "lucaz",
    "oda",
    "rillant",
    "shizu",
}

PUBLICATION_RELEASE_GROUP_PHRASES = {
    ("1r0n",),
    ("archangel", "zone", "empire"),
    ("f", "archangel", "zone", "empire"),
    ("f", "son", "of", "ultron", "empire"),
    ("jko",),
    ("lostnerevarine", "empire"),
    ("lucaz",),
    ("minutemen", "phd"),
    ("oda",),
    ("rillant",),
    ("shizu",),
    ("son", "of", "ultron", "empire"),
    ("zerodaze", "dcp", "hd"),
    ("zone", "empire"),
}

PUBLICATION_METADATA_WORDS = {
    "c2c",
    "digital",
    "ebook",
    "eng",
    "english",
    "f",
    "fixed",
    "hybrid",
    "retail",
    "scan",
    "scans",
    "web",
}

# Quality grades that only ever qualify a format tag: "(Digital-HD)".
PUBLICATION_QUALITY_WORDS = {"hd", "hq", "lq", "sd", "webrip", "720p", "1080p"}

# The corporate word a publisher prints after its name: "(Image Comics)".
PUBLISHER_SUFFIX_WORDS = {
    "books",
    "comics",
    "entertainment",
    "group",
    "media",
    "press",
    "publishing",
    "studios",
}

# How a release names its own shape rather than another book.
PUBLICATION_TYPE_RE = re.compile(
    r"(?:(?:limited|mini)[\s-]*)?series|mini|one[\s-]?shot|ongoing|annual|graphic\s+novel",
    re.I,
)


def _publication_date_group(value):
    text = re.sub(r"[._]", "-", str(value or "").strip().lower())
    return bool(
        re.fullmatch(r"(?:19|20)\d{2}(?:-(?:0?[1-9]|1[0-2]))?", text)
        or re.fullmatch(
            rf"(?:{'|'.join(sorted(PUBLICATION_MONTH_WORDS))})[\s_-]+(?:19|20)\d{{2}}",
            text,
        )
    )


# A relation word inside a group means a second title is being named --
# "(01-38+akira vs katana)" is a crossover, "(01-38+trades)" is packaging.
SUBSERIES_MARKER_RE = re.compile(
    r"(?i)(?:^|[\s._+-])(?:"
    r"vs|versus|gaiden|prelude|sequel|spin[\s._-]?off|crossover|"
    r"year[\s._-]+(?:one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})"
    r")(?:$|[\s._+-])"
)

# How a container folder describes the copy rather than naming a book.
CONTAINER_FORMAT_PHRASES = {
    "c2c",
    "complete",
    "complete series",
    "digital",
    "fixed",
    "hybrid",
    "repack",
    "scan",
    "scans",
    "single issues",
    "webrip",
}

# Real pack folders sign themselves with a bare publisher attribution. Kept
# here rather than imported from the SLSKD probe's phrase tables: this runs on
# every candidate path, and a handful of duplicated names is cheaper than
# pulling that module into the import graph.
CONTAINER_PUBLISHER_PHRASES = {
    "2000 ad",
    "ablaze",
    "aftershock",
    "archie",
    "boom",
    "dark horse",
    "dc",
    "dc comics",
    "dynamite",
    "idw",
    "image",
    "kodansha",
    "marvel",
    "oni",
    "seven seas",
    "shogakukan",
    "shueisha",
    "square enix",
    "titan",
    "valiant",
    "vault",
    "vertigo",
    "viz",
    "viz media",
    "yen press",
}


def annotation_group_shape(value):
    """Does this bracketed group have the shape of an annotation about a release?

    Everything a release legitimately says about itself has a shape: a range,
    a date, a count of what it collects or reproduces, its position in a mini,
    the kind of book it is, an imprint, or a format tag. A different series'
    name has none of them -- "(Conan)" sitting beside "(2016-2019)" is the tell
    that something else is being named.

    Shared by the folder-level and file-level readers on purpose. They used to
    keep separate word lists for the same idea and drifted: "(ENGLISH)" counted
    as a format tag on a file and as a second title on the folder above it.
    """

    raw = str(value or "").strip()
    normalized = re.sub(r"\s+", " ", re.sub(r"[._]+", " ", raw.lower())).strip()
    if not normalized:
        return True
    # Checked first: a relation word outranks any shape it is hiding behind.
    if SUBSERIES_MARKER_RE.search(normalized):
        return False
    # An issue or volume range, optionally with what else the pack bundles in.
    # A bare "+" is the pack saying "and whatever came after" -- "001-050+".
    # Naming the extras keeps a closed list on purpose: "01-38+trades" says
    # what came along, "01-38+notarealword" is something else wearing the
    # same shape.
    if re.fullmatch(
        r"v?\d{1,4}\s*-\s*v?\d{1,4}"
        r"(?:\s*\+(?:\s*(?:trades?|tpbs?|extras?|annuals?|specials?"
        r"|omnibus(?:es)?|variants?|covers?|scripts?|one[\s-]?shots?))?)?",
        normalized,
    ):
        return True
    # A single year or a span of them, or a dated month of publication. A
    # trailing bare hyphen after the start year ("1992-") is the ongoing-series
    # convention for "still publishing, no end year yet" -- Spawn's real pack
    # folder is named exactly this way. Only the hyphen form is left
    # open-ended; "to"/"through" still require a closing year on both sides,
    # since a dangling "1992 to" with nothing after it isn't an observed
    # naming convention and accepting it would just widen the shape for no
    # real case.
    if re.fullmatch(
        r"(?:18|19|20)\d{2}"
        r"(?:\s*-\s*(?:(?:18|19|20)\d{2})?|\s*(?:to|through)\s*(?:18|19|20)\d{2})?",
        normalized,
    ):
        return True
    if _publication_date_group(raw):
        return True
    # What the release counts: issues collected, covers reproduced, and where
    # this part falls in a numbered mini -- "(3 covers)", "(of 06)". Chapters
    # and volumes are deliberately absent; those change the unit, not the copy.
    if re.fullmatch(r"\d+\s+(?:issues?|covers?)", normalized):
        return True
    if re.fullmatch(r"of\s+0*\d{1,3}", normalized):
        return True
    if re.fullmatch(r"[a-z0-9 ]*\bimprint\b", normalized):
        return True
    if PUBLICATION_TYPE_RE.fullmatch(normalized):
        return True
    if normalized in CONTAINER_FORMAT_PHRASES:
        return True
    # A format tag, alone or qualified: "digital", "digital hd", "fixed scan".
    words = [word for word in re.split(r"[\s-]+", normalized) if word]
    return bool(
        words
        and all(
            word in PUBLICATION_METADATA_WORDS or word in PUBLICATION_QUALITY_WORDS
            for word in words
        )
    )


def release_credit_group(value):
    """Is this bracketed group a release-group or scanlator credit?

    Credits are handles, not prose. They weld a case change into one word
    ("LuCaZ", "AnHeroGold", "LeDuch"), or join aliases with - or + and no
    spaces around the joiner ("danke-Empire", "Minutemen-Spaztastic",
    "LostNerevarine-Empire+LuCaZ"). A book title does neither: "(Conan)",
    "(Brotherhood)" and "(Not A Real Imprint Name)" are plain words and go on
    reading as a second title.

    The closed handle list stays for the ones with no shape at all -- "Oda",
    "Shizu", "1r0n" -- because nothing but a name distinguishes those.
    """

    raw = str(value or "").strip()
    if not raw:
        return False
    if SUBSERIES_MARKER_RE.search(re.sub(r"[._]+", " ", raw.lower())):
        return False
    tokens = tuple(re.findall(r"[a-z0-9]+", raw.lower()))
    if not tokens:
        return False
    if tokens in PUBLICATION_RELEASE_GROUP_PHRASES:
        return True
    if all(token in PUBLICATION_RELEASE_GROUP_WORDS for token in tokens):
        return True
    if re.search(r"[a-z][A-Z]", raw):
        return True
    # Aliases welded by - or +, with no space around the joiner and at least
    # one side capitalized. That last part is what keeps "01-38+notarealword"
    # out: a range with a word stapled on has no capitalized handle in it.
    parts = [part.strip() for part in re.split(r"[-+]", raw) if part.strip()]
    return bool(
        len(parts) > 1
        and re.search(r"\S[-+]\S", raw)
        and any(re.match(r"^[A-Z]", part) for part in parts)
    )


def publisher_signature_group(value, publisher_tokens):
    """Does this group just sign the publisher, optionally with a ship date?

    Tied to the series' own publisher rather than any known name, so a
    mislabelled edition -- "Moon Girl and Devil Dinosaur 014 (DC, 2017-02)"
    against a Marvel series -- still reads as evidence of a different book.
    """

    publisher_tokens = list(publisher_tokens or [])
    if not publisher_tokens:
        return False
    joined = r"[\W_]+".join(re.escape(token) for token in publisher_tokens)
    suffixes = "|".join(sorted(PUBLISHER_SUFFIX_WORDS))
    match = re.match(
        rf"^\s*{joined}(?:[\W_]+(?:{suffixes}))?\s*[,;:_-]?\s*(?P<rest>.*)$",
        str(value or ""),
        re.I,
    )
    if not match:
        return False
    rest = match.group("rest").strip()
    return not rest or bool(_publication_date_group(rest))


def container_annotation_group(value):
    """Is this bracketed group describing the pack, or naming another book?

    A container folder is read a little more leniently than a file: it may
    sign any publisher it likes, because that is how real pack folders name
    themselves, while a file has to match the publisher of the series it
    claims to belong to.
    """

    if annotation_group_shape(value):
        return True
    normalized = re.sub(r"\s+", " ", re.sub(r"[._]+", " ", str(value or "").lower())).strip()
    if SUBSERIES_MARKER_RE.search(normalized):
        return False
    words = normalized.split()
    if words and words[-1] in PUBLISHER_SUFFIX_WORDS:
        words = words[:-1]
    if words and " ".join(words) in CONTAINER_PUBLISHER_PHRASES:
        return True
    return release_credit_group(value)


def _organizational_prefix_marker(text, issue_number):
    """Strip a leading, unbracketed organizational marker from a container tail.

    Real pack folders often lead with a bare marker before their bracketed
    annotations -- "001-008 (2017-2018)", "V4 (2016)", "v1 (001 - 037 +
    Annual)". Only three shapes are trusted, and only the ones that carry no
    identity ambiguity of their own:

    - a volume marker (v1, V4, vol 1): volumes are a separate numbering axis
      from issues, so no issue-number match is required;
    - an issue range that contains the trusted target issue;
    - the exact trusted issue number standing alone.

    Anything else -- an unrelated number, a range that excludes the target,
    a bare word -- is left in place for the caller's normal rejection path.
    """

    if issue_number is None:
        return text, False
    try:
        wanted = float(str(issue_number).strip())
    except (TypeError, ValueError):
        return text, False
    match = re.match(
        r"""^\s*
        (?:
            (?:v|vol|volume)[\s._-]*0*(?P<vol>\d+(?:\.\d+)?)
            |
            0*(?P<range_start>\d+(?:\.\d+)?)\s*[-–—]\s*0*(?P<range_end>\d+(?:\.\d+)?)
            |
            0*(?P<exact>\d+(?:\.\d+)?)
        )
        (?=[\s._+-]*[\[(]|\s*$)
        """,
        text,
        re.I | re.X,
    )
    if not match:
        return text, False
    if match.group("vol") is None:
        if match.group("range_start") is not None:
            try:
                low, high = sorted((float(match.group("range_start")), float(match.group("range_end"))))
            except ValueError:
                return text, False
            if not (low <= wanted <= high):
                return text, False
        else:
            try:
                if float(match.group("exact")) != wanted:
                    return text, False
            except (TypeError, ValueError):
                return text, False
    remainder = re.sub(r"^[\s._+-]+", "", text[match.end():])
    return remainder, True


def benign_exact_title_organizational_folder_tail(tail, issue_number=None):
    """Recognize a container folder that only annotates the series it holds.

    A pack folder is named for what it contains: the range, the years it spans,
    the publisher, the scanner. "Akira (01-38)(1988-1995)(epic imprint)" is
    still Akira, and so is "Akira.(01-38+trades)(1988-2002)". Those annotations
    arrive in brackets by convention, so a tail built only from bracketed
    groups is describing the folder rather than naming a different book.

    Two things have to hold at once, so both are checked. The bracketing is
    structural: a tail built only from groups is annotation, and any bare word
    left outside them names something else, which is what keeps "Akira Gaiden
    (2001)" and "Injustice - Gods Among Us - Year Five (2016)" rejected
    exactly as before. Then each group has to look like annotation on its own
    -- see container_annotation_group -- because "(Conan)" is perfectly well
    bracketed and still means the folder holds a different book.

    A common real-world shape leads with an unbracketed organizational marker
    before the bracketed groups -- "001-008 (2017-2018)", "V4 (2016)". See
    _organizational_prefix_marker for the narrow set trusted here.

    Only ancestor directories are read this leniently. The file's own name is
    still held to the strict rule, so a stray issue sitting inside an otherwise
    correct pack folder is caught on its own merits.
    """

    text = str(tail or "").strip()
    # A pack folder often welds the title to its first group with punctuation
    # ("Akira.(01-38+trades)"). That separator is not a second title word.
    text = re.sub(r"^[\s._+-]+", "", text)
    text, _consumed_prefix = _organizational_prefix_marker(text, issue_number)
    groups = []
    while text:
        match = re.match(
            r"^\s*(?:\(\s*([^()]*?)\s*\)|\[\s*([^\[\]]*?)\s*\])[\s._+-]*",
            text,
        )
        if not match:
            return False
        groups.append(match.group(1) if match.group(1) is not None else match.group(2))
        text = text[match.end():]
    if not groups:
        return False
    return all(container_annotation_group(group) for group in groups)


def benign_exact_title_publication_tail(
    tail,
    issue_number,
    *,
    stop_words=(),
    edition_words=(),
    publisher=None,
):
    """Recognize a fully consumed unit plus structured publication metadata."""

    text = re.sub(r"\.(?:cbz|cbr|pdf|epub|zip|rar|7z)$", "", str(tail or "").strip(), flags=re.I)
    # A release's own annotations can sit before the issue number too --
    # "Title (2014) #5", not just "Title 5 (2014)". Peel those off the front
    # the same way trailing groups are peeled off the end, before the bare
    # issue-number words are read, so the two orders are treated symmetrically.
    leading_groups = []
    while text:
        match = re.match(
            r"^\s*(?:\(\s*([^\[\]()]+?)\s*\)|\[\s*([^\[\]()]+?)\s*\])\s*",
            text,
        )
        if not match:
            break
        leading_groups.append((match.group(1) if match.group(1) is not None else match.group(2)).strip())
        text = text[match.end():]

    groups = []
    while text:
        # Matched pairs only. "(2016-2019](47 issues)" is not two annotations,
        # it is a name that happens to have brackets in it, and it keeps
        # falling through to the untrusted-suffix rule below.
        match = re.search(
            r"\s*(?:\(\s*([^\[\]()]+?)\s*\)|\[\s*([^\[\]()]+?)\s*\])\s*$",
            text,
        )
        if not match:
            break
        groups.insert(0, (match.group(1) if match.group(1) is not None else match.group(2)).strip())
        text = text[:match.start()].rstrip()
    groups = leading_groups + groups

    words = re.findall(r"[a-z0-9]+", text.lower())
    wanted = str(issue_number or "").strip().lstrip("0") or "0"
    if words:
        compact_unit = re.fullmatch(
            r"(?:v|vol|volume|book|issue|no|number|ch|chap|chapter|part|pt)0*(\d+(?:\.\d+)?)",
            words[0],
        )
        if compact_unit:
            found = compact_unit.group(1).lstrip("0") or "0"
            if found != wanted:
                return False
            words.pop(0)
    if not benign_exact_title_publication_suffix(
        words,
        issue_number,
        stop_words=stop_words,
        edition_words=edition_words,
    ):
        return False
    if not groups:
        return True

    publisher_tokens = re.findall(r"[a-z0-9]+", str(publisher or "").lower())

    def metadata_group(value):
        return bool(
            annotation_group_shape(value)
            or publisher_signature_group(value, publisher_tokens)
        )

    if all(metadata_group(group) for group in groups):
        return True
    # A scanlator credit signs the copy last, after the groups that say when
    # and how it was made. Requiring those to come first is what keeps a lone
    # unexplained bracket -- "Akira 38 (Gaiden)" -- reading as a second title.
    return bool(
        len(groups) > 1
        and all(metadata_group(group) for group in groups[:-1])
        and release_credit_group(groups[-1])
    )


def benign_exact_title_publication_suffix(words, issue_number, *, stop_words=(), edition_words=()):
    """Recognize only a fully consumed unit/month/year suffix after an exact title."""

    remaining = [
        str(word or "").lower()
        for word in (words or [])
        if str(word or "").lower() not in set(stop_words)
        and str(word or "").lower() not in set(edition_words)
    ]
    wanted = str(issue_number or "").strip().lstrip("0") or "0"
    if remaining and remaining[0].isdigit() and wanted != "0":
        found = remaining[0].lstrip("0") or "0"
        if found != wanted:
            return False
        remaining.pop(0)
    if remaining and remaining[0] in PUBLICATION_MONTH_WORDS:
        remaining.pop(0)
    if remaining and remaining[0].isdigit() and 1900 <= int(remaining[0]) <= 2099:
        remaining.pop(0)
    return not remaining
