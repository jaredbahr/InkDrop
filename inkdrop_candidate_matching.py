"""Provider-independent release parsing and target compatibility.

Target fields and source-result fields intentionally use different names. A
candidate must never appear to match merely because an adapter copied the
wanted unit number into its normalized result.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import PurePath

import inkdrop_artifact_acceptance


CONTRACT_VERSION = 2

VOLUME_UNITS = {"volume", "vol", "book_volume", "manga_volume"}
ISSUE_UNITS = {"issue", "comic_issue"}
CHAPTER_UNITS = {"chapter", "manga_chapter"}
COLLECTED_MARKERS = {
    "collected_edition",
    "complete_collection",
    "omnibus",
    "trade_paperback",
}
COLLECTED_SINGLETON_MARKERS = COLLECTED_MARKERS | {
    "deluxe_edition",
    "essential_edition",
    "hardcover",
    "library_edition",
    "volume",
}

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}

ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
WORD_TOKEN = "|".join(NUMBER_WORDS)
NUMBER_TOKEN = rf"(?:\d+(?:\.\d+)?|{WORD_TOKEN}|[ivxlc]+)"

PREVIEW_RE = re.compile(r"(?i)\b(?:preview|sample|excerpt|teaser)\b")
VOLUME_RE = re.compile(rf"(?i)\b(?:vol(?:ume)?|v|tome|band)\.?\s*0*({NUMBER_TOKEN})\b")
BOOK_RE = re.compile(rf"(?i)\bbook\s+0*({NUMBER_TOKEN})\b")
CHAPTER_RE = re.compile(rf"(?i)\b(?:chapter|chap|ch|c)\.?\s*#?\s*0*({NUMBER_TOKEN})\b")
ISSUE_RE = re.compile(rf"(?i)(?:\b(?:issue|iss|no|number)\.?\s*#?\s*|#)0*({NUMBER_TOKEN})\b")
COVERAGE_RE = re.compile(
    rf"(?i)(?:\b(?:issues?|chapters?|chs?)\b\s*)?#?\s*0*({NUMBER_TOKEN})\s*"
    rf"(?:-|\+|\bto\b|\bthrough\b)\s*#?\s*0*({NUMBER_TOKEN})\b"
)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
PUBLICATION_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})-(\d{1,2})(?:-(\d{1,2}))?(?![-\d])"
)
UNIT_PREFIX_TOKENS = {
    "book", "books", "c", "ch", "chap", "chapter", "chapters", "chs",
    "iss", "issue", "issues", "no", "number", "numbers", "tome", "tomes",
    "v", "vol", "volume", "volumes",
}
CREATOR_BYLINE_RE = re.compile(
    r"(?i)\bby\s+([A-Z][A-Za-z.'\u2019-]*(?:\s+[A-Z][A-Za-z.'\u2019-]*){1,4})"
    r"(?=\s+(?:#?\d|v(?:ol(?:ume)?)?\.?\s*\d|issues?\b|chapters?\b|"
    r"digital\b|retail\b|cbr\b|cbz\b|pdf\b|epub\b|\(|\[)|\s*$)"
)

EDITION_PATTERNS = (
    ("complete_collection", re.compile(r"(?i)\bcomplete\s+(?:series|collection|edition)\b")),
    ("omnibus", re.compile(r"(?i)\bomnibus\b")),
    ("collected_edition", re.compile(r"(?i)\bcollected\s+edition\b")),
    ("essential_edition", re.compile(r"(?i)\bessential\s+edition\b")),
    ("deluxe_edition", re.compile(r"(?i)\bdeluxe\s+edition\b")),
    ("library_edition", re.compile(r"(?i)\blibrary\s+edition\b")),
    ("trade_paperback", re.compile(r"(?i)\b(?:trade\s+paperback|tpb)\b")),
    ("hardcover", re.compile(r"(?i)\b(?:hardcover|digital\s+hc|hc)\b")),
    ("volume", re.compile(r"(?i)\bvolume\b")),
)
PACK_PATTERNS = (
    ("complete_collection", re.compile(r"(?i)\bcomplete\s+(?:series|collection|edition)\b")),
    ("weekly_pack", re.compile(r"(?i)\bweekly\b")),
    ("pack", re.compile(r"(?i)\b(?:pack|batch|dump)\b")),
)
SINGLETON_NEUTRAL_SUFFIX_TOKENS = {
    "cbz",
    "cbr",
    "digital",
    "ebook",
    "pdf",
    "retail",
    "web",
}
TRUSTED_RELEASE_GROUP_SUFFIXES = {
    ("empire",),
    ("f", "son", "of", "ultron", "empire"),
    ("minutemen", "phd"),
    ("son", "of", "ultron", "empire"),
    ("zone", "empire"),
}
COLLECTED_ALIAS_NEUTRAL_GROUP_TOKENS = {
    "digital",
    "ebook",
    "empire",
    "f",
    "minutemen",
    "of",
    "phd",
    "retail",
    "scan",
    "son",
    "ultron",
    "web",
    "zone",
}
COMPACT_VOLUME_RANGE_RE = re.compile(
    rf"(?i)\b(?:vol(?:ume)?|v)\.?\s*0*({NUMBER_TOKEN})\s*"
    rf"(?:[-–—_+]|\bto\b)\s*(?:(?:vol(?:ume)?|v)\.?\s*)?0*({NUMBER_TOKEN})\b"
)
TRUSTED_SINGLETON_PROOF_SOURCES = {
    "comicvine_authoritative_count_and_canonical_issue_identity",
    "comicvine_collected_single_wanted_identity_without_declared_count",
}
TRUSTED_COLLECTED_SINGLETON_PROOF_SOURCES = {
    "comicvine_collected_single_wanted_identity",
}


def _first(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return ""


def _number(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    text = raw.lstrip("0") or "0"
    if text in NUMBER_WORDS:
        return str(NUMBER_WORDS[text])
    if re.fullmatch(r"[ivxlc]+", text):
        total = 0
        previous = 0
        for char in reversed(text):
            current = ROMAN_VALUES[char]
            total += -current if current < previous else current
            previous = max(previous, current)
        return str(total) if 0 < total <= 399 else ""
    try:
        numeric = float(text)
    except (TypeError, ValueError):
        return ""
    return str(int(numeric)) if numeric.is_integer() else str(numeric).rstrip("0").rstrip(".")


def _normalized_title(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _creator_names(value):
    values = value if isinstance(value, (list, tuple, set)) else [value]
    names = []
    for row in values:
        if isinstance(row, dict):
            row = _first(row.get("name"), row.get("full_name"), row.get("creator"), row.get("author"))
        text = str(row or "").strip()
        if not text:
            continue
        for part in re.split(r"\s*(?:,|;|\band\b|&)\s*", text, flags=re.I):
            normalized = _normalized_title(part)
            if len(normalized.split()) >= 2 and normalized not in names:
                names.append(normalized)
    return names


def _trusted_wanted_creators(wanted_item=None):
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    names = []
    for key in ("creators", "creator", "authors", "author", "writers", "writer"):
        for name in _creator_names(wanted.get(key)):
            if name not in names:
                names.append(name)
    title = str(_first(wanted.get("series_title"), wanted.get("series"), wanted.get("title")) or "").strip()
    for match in CREATOR_BYLINE_RE.finditer(title):
        for name in _creator_names(match.group(1)):
            if name not in names:
                names.append(name)
    return names


def _candidate_asserted_creators(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    names = []
    for _label, text, _identity_text in _candidate_text_values(candidate):
        for match in CREATOR_BYLINE_RE.finditer(text):
            for name in _creator_names(match.group(1)):
                if name not in names:
                    names.append(name)
    return names


def _year(value):
    match = YEAR_RE.search(str(value or ""))
    return match.group(0) if match else ""


def has_unit_prefix_before_number(value, number_offset):
    text = str(value or "")
    offset = max(0, min(int(number_offset or 0), len(text)))
    preceding = text[:offset].rstrip()
    if not preceding:
        return False
    if preceding.endswith((":", ".")):
        preceding = preceding[:-1].rstrip()
    if preceding.endswith("#"):
        return True
    end = len(preceding)
    start = end
    while start > 0 and preceding[start - 1].isalpha():
        start -= 1
    return preceding[start:end].lower() in UNIT_PREFIX_TOKENS


def publication_date_evidence(value):
    """Return unprefixed publication dates and text masked for unit parsing."""
    original = str(value or "")
    masked = list(original)
    dates = []
    year_months = []
    for match in PUBLICATION_DATE_RE.finditer(original):
        if has_unit_prefix_before_number(original, match.start()):
            continue
        month = match.group(2)
        day = match.group(3)
        if len(month) != 2 or (day is not None and len(day) != 2):
            continue
        try:
            if day is None:
                date(int(match.group(1)), int(month), 1)
            else:
                date(int(match.group(1)), int(month), int(day))
        except ValueError:
            continue
        year_month = f"{match.group(1)}-{match.group(2)}"
        publication_date = f"{year_month}-{match.group(3)}" if match.group(3) else year_month
        dates.append(publication_date)
        year_months.append(year_month)
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    return {
        "dates": dates,
        "year_months": year_months,
        "masked_text": "".join(masked),
    }


def parse_release_title(value):
    """Parse source-unit evidence without inferring fields from the target."""
    original = str(value or "").strip()
    normalized_original = _normalized_title(original)
    evidence = []
    publication = publication_date_evidence(original)
    publication_dates = publication["dates"]
    publication_year_months = publication["year_months"]
    # An artifact declares its own unit in its own filename. Ancestor
    # directories are the uploader's shelf layout, not release metadata, so a
    # range parsed out of a parent folder describes the container rather than
    # this file. Reading unit identity off the whole path let a folder such as
    # "All-Star Superman 001-12 (2006-2008)" -- or one that only looks like a
    # range, e.g. a "AA-LL 11-2025" shelf or a "(2006 - 12 Issues)" note --
    # stamp every exact single issue inside it as a collected range and block
    # it with coverage_not_unit_number.
    identity_source = re.split(r"[\\/]", original)[-1].strip() or original
    scoped_to_leaf = identity_source != original
    identity_publication = (
        publication_date_evidence(identity_source) if scoped_to_leaf else publication
    )
    unit_text = identity_publication["masked_text"]
    volume_match = VOLUME_RE.search(identity_source)
    book_match = BOOK_RE.search(identity_source)
    chapter_match = CHAPTER_RE.search(identity_source)
    issue_match = ISSUE_RE.search(identity_source)
    coverage_match = COVERAGE_RE.search(unit_text)
    volume_range_match = COMPACT_VOLUME_RANGE_RE.search(unit_text)
    volume = _number(volume_match.group(1)) if volume_match else ""
    book = _number(book_match.group(1)) if book_match else ""
    chapter = _number(chapter_match.group(1)) if chapter_match else ""
    issue = _number(issue_match.group(1)) if issue_match else ""
    coverage_start = _number(coverage_match.group(1)) if coverage_match else ""
    coverage_end = _number(coverage_match.group(2)) if coverage_match else ""
    if volume_range_match:
        coverage_start = _number(volume_range_match.group(1))
        coverage_end = _number(volume_range_match.group(2))
    coverage_number_offset = (
        volume_range_match.start(1)
        if volume_range_match
        else (coverage_match.start(1) if coverage_match else 0)
    )
    explicit_coverage = bool(
        (volume_range_match or coverage_match)
        and has_unit_prefix_before_number(unit_text, coverage_number_offset)
    )
    if (
        not explicit_coverage
        and coverage_start.isdigit()
        and coverage_end.isdigit()
        and 1900 <= int(coverage_start) <= 2099
        and 1900 <= int(coverage_end) <= 2099
    ):
        coverage_start = ""
        coverage_end = ""
    bare_number = ""
    if not any((volume, book, chapter, issue, coverage_start, coverage_end)):
        bare_values = []
        for match in re.finditer(r"(?<![A-Za-z0-9])0*(\d{1,4})(?![A-Za-z0-9])", unit_text):
            number = _number(match.group(1))
            if number and not (number.isdigit() and 1900 <= int(number) <= 2099):
                bare_values.append(number)
        if len(set(bare_values)) == 1:
            issue = bare_number = bare_values[0]
    edition = ""
    edition_markers = []
    for marker, pattern in EDITION_PATTERNS:
        if pattern.search(normalized_original):
            edition = edition or marker
            edition_markers.append(marker)
            evidence.append(f"edition:{marker}")
    pack = ""
    for marker, pattern in PACK_PATTERNS:
        if pattern.search(normalized_original):
            pack = marker
            break
    if coverage_start and coverage_end:
        pack = pack or "range"
        evidence.append(f"coverage:{coverage_start}-{coverage_end}")
    evidence.extend(f"publication_date:{value}" for value in publication_dates)
    for label, number in (("volume", volume), ("book", book), ("chapter", chapter), ("issue", issue)):
        if number:
            evidence.append(f"{'bare_number' if label == 'issue' and bare_number else label}:{number}")
    preview = bool(PREVIEW_RE.search(normalized_original))
    if preview:
        evidence.append("preview_or_sample")
    if scoped_to_leaf:
        evidence.append("unit_identity_from_filename")
    unit_types = [
        label
        for label, present in (
            ("volume", volume),
            ("book", book),
            ("chapter", chapter),
            ("issue", issue),
        )
        if present
    ]
    ambiguous = len(unit_types) > 1 and not (
        set(unit_types) == {"book", "issue"} and coverage_start and coverage_end
    )
    return {
        "parser_contract_version": CONTRACT_VERSION,
        "original_title": original,
        "normalized_title": normalized_original,
        "unit_identity_title": identity_source,
        "unit_type": unit_types[0] if len(unit_types) == 1 else ("collection" if edition in COLLECTED_MARKERS else ""),
        "issue_number": issue,
        "bare_number": bare_number,
        "chapter_number": chapter,
        "volume_number": volume,
        "book_number": book,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "edition_marker": edition,
        "edition_markers": edition_markers,
        "pack_marker": pack,
        "preview_or_sample": preview,
        "ambiguous": ambiguous,
        "year": _year(original),
        "publication_date": publication_dates[0] if publication_dates else "",
        "publication_dates": publication_dates,
        "publication_year_month": publication_year_months[0] if publication_year_months else "",
        "publication_year_months": publication_year_months,
        "evidence": evidence,
    }


def target_context(wanted_item=None):
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    explicit_unit_type = str(_first(wanted.get("unit_type"), wanted.get("unitType"), wanted.get("unit"))).strip().lower()
    unit_type = explicit_unit_type
    issue = _number(_first(wanted.get("issue_number"), wanted.get("normalized_number"), wanted.get("issue")))
    chapter = _number(_first(wanted.get("chapter_number"), wanted.get("chapter")))
    volume = _number(
        _first(
            wanted.get("volume_number"),
            wanted.get("volumeNumber"),
            wanted.get("volume"),
            wanted.get("book_volume"),
            wanted.get("manga_volume"),
        )
    )
    title_evidence = parse_release_title(
        _first(wanted.get("issue_title"), wanted.get("issueTitle"), wanted.get("title"), wanted.get("query"))
    )
    if not volume and title_evidence.get("volume_number"):
        volume = title_evidence["volume_number"]
    if not volume and title_evidence.get("book_number"):
        volume = title_evidence["book_number"]
    if unit_type in VOLUME_UNITS and not volume:
        volume = issue
    if unit_type in CHAPTER_UNITS and not chapter:
        chapter = issue
    media_type = str(wanted.get("media_type") or wanted.get("mediaType") or "").strip().lower()
    if not unit_type:
        western_comic_issue = bool(
            issue
            and not volume
            and (not chapter or chapter == issue)
            and media_type
            in {
                "comic",
                "comics",
                "comic_issue",
                "graphic novel",
                "graphic_novel",
                "western comic",
                "western_comic",
            }
        )
        if western_comic_issue:
            # Older queue payloads mirror the same issue number into
            # ``chapter``. Only discard that exact alias: conflicting chapter
            # or inferred volume evidence must remain visible to the safety
            # gate.
            # Durable western-comic identity wins over that compatibility
            # alias; otherwise exact ``Series 001`` files are mislabeled as
            # the wrong unit type and can never reach the normal safety gate.
            unit_type = "issue"
            chapter = ""
        elif volume:
            unit_type = "volume"
        elif chapter:
            unit_type = "chapter"
        elif issue:
            unit_type = "issue"
    if not explicit_unit_type and media_type in {"book", "ebook", "prose", "audiobook"}:
        unit_type = ""
    try:
        canonical_issue_count = int(wanted.get("canonical_issue_count") or 0)
    except (TypeError, ValueError):
        canonical_issue_count = 0
    try:
        metadata_issue_count = int(wanted.get("metadata_issue_count") or 0)
    except (TypeError, ValueError):
        metadata_issue_count = 0
    singleton_proof_source = str(wanted.get("singleton_issue_proof_source") or "")
    singleton_count_supported = bool(
        metadata_issue_count == 1
        if singleton_proof_source == "comicvine_authoritative_count_and_canonical_issue_identity"
        else (
            metadata_issue_count == 0
            and singleton_proof_source == "comicvine_collected_single_wanted_identity_without_declared_count"
        )
    )
    singleton_issue_proof = bool(
        wanted.get("singleton_issue_proof")
        and singleton_proof_source in TRUSTED_SINGLETON_PROOF_SOURCES
        and wanted.get("singleton_metadata_trusted") is True
        and wanted.get("singleton_metadata_fresh") is True
        and wanted.get("singleton_issue_metadata_trusted") is True
        and canonical_issue_count == 1
        and singleton_count_supported
        and issue == "1"
        and ("comic" in media_type or "graphic novel" in media_type)
    )
    try:
        collected_singleton_wanted_count = int(wanted.get("collected_singleton_wanted_count") or 0)
    except (TypeError, ValueError):
        collected_singleton_wanted_count = 0
    raw_collected_markers = wanted.get("collected_singleton_markers")
    collected_singleton_markers = [
        str(marker or "").strip().lower()
        for marker in (raw_collected_markers if isinstance(raw_collected_markers, (list, tuple, set)) else [])
        if str(marker or "").strip().lower() in COLLECTED_SINGLETON_MARKERS
    ]
    collected_singleton_proof = bool(
        wanted.get("collected_singleton_proof")
        and str(wanted.get("collected_singleton_proof_source") or "")
        in TRUSTED_COLLECTED_SINGLETON_PROOF_SOURCES
        and wanted.get("singleton_metadata_trusted") is True
        and wanted.get("singleton_metadata_fresh") is True
        and wanted.get("singleton_issue_metadata_trusted") is True
        and canonical_issue_count == 1
        and collected_singleton_wanted_count == 1
        and collected_singleton_markers
        and issue == "1"
        and ("comic" in media_type or "graphic novel" in media_type)
    )
    return {
        "unit_type": unit_type,
        "issue_number": issue,
        "chapter_number": chapter,
        "volume_number": volume,
        "allow_collected_edition": bool(
            wanted.get("allow_collected_edition") or wanted.get("collected_editions_allowed")
        ),
        "unit_type_explicit": bool(explicit_unit_type),
        "media_type": media_type,
        "canonical_issue_count": canonical_issue_count,
        "metadata_issue_count": metadata_issue_count,
        "singleton_issue_proof": singleton_issue_proof,
        "collected_singleton_proof": collected_singleton_proof,
        "collected_singleton_markers": collected_singleton_markers,
    }


def _singleton_exact_title_match(candidate, wanted_item, target, evidence, *, allow_bare_number=""):
    if not target.get("singleton_issue_proof"):
        return False
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    missing_count_collected_proof = (
        str(wanted.get("singleton_issue_proof_source") or "")
        == "comicvine_collected_single_wanted_identity_without_declared_count"
    )
    bare_number_match = bool(
        allow_bare_number
        and evidence.get("bare_number") == allow_bare_number
        and evidence.get("issue_number") == allow_bare_number
    )
    if any(
        (
            evidence.get("issue_number") and not bare_number_match,
            evidence.get("chapter_number"),
            evidence.get("volume_number"),
            evidence.get("book_number"),
            evidence.get("coverage_start"),
            evidence.get("coverage_end"),
            evidence.get("pack_marker"),
            evidence.get("edition_marker") and not missing_count_collected_proof,
            evidence.get("ambiguous"),
            candidate.get("pack"),
            candidate.get("pack_candidate"),
            candidate.get("preview_or_sample"),
        )
    ):
        return False
    parsed_sources = evidence.get("sources") if isinstance(evidence.get("sources"), list) else []
    if any(
        any(
            source.get(key)
            for key in (
                "issue_number",
                "chapter_number",
                "volume_number",
                "book_number",
                "coverage_start",
                "coverage_end",
                "pack_marker",
                "edition_marker",
                "preview_or_sample",
                "ambiguous",
            )
            if (
                key != "issue_number"
                or not (
                    bare_number_match
                    and source.get("issue_number") == allow_bare_number
                    and source.get("bare_number") == allow_bare_number
                )
            )
            and (key != "edition_marker" or not missing_count_collected_proof)
        )
        for source in parsed_sources
    ):
        return False
    target_title = _normalized_title(
        _first(wanted.get("series_title"), wanted.get("series"), wanted.get("manga_title"))
    )
    release_title = str(_first(candidate.get("original_result_title"), candidate.get("title"))).strip()
    if not target_title or not release_title:
        return False
    release_title = re.sub(r"(?i)\.(?:cbz|cbr|pdf|epub)$", "", release_title).strip()
    target_tokens = target_title.split()
    release_tokens = _normalized_title(release_title).split()
    if release_tokens[: len(target_tokens)] != target_tokens:
        return False
    target_year = _year(_first(wanted.get("year"), wanted.get("release_date"), wanted.get("date")))
    candidate_year = _year(_first(candidate.get("year"), candidate.get("release_date"), evidence.get("year")))
    if target_year and candidate_year and target_year != candidate_year and not missing_count_collected_proof:
        return False
    source_years = {str(source.get("year") or "") for source in parsed_sources if source.get("year")}
    if target_year and any(year != target_year for year in source_years) and not missing_count_collected_proof:
        return False
    target_publisher = _normalized_title(wanted.get("publisher"))
    candidate_publisher = _normalized_title(
        _first(candidate.get("publisher"), candidate.get("source_publisher"), candidate.get("provider_publisher"))
    )
    if target_publisher and candidate_publisher and target_publisher != candidate_publisher:
        return False
    allowed_suffix = set(SINGLETON_NEUTRAL_SUFFIX_TOKENS)
    if missing_count_collected_proof:
        allowed_suffix.update({
            "anniversary", "collection", "collected", "deluxe", "edition",
            "hardcover", "hc", "library", "omnibus", "paperback", "tpb", "trade",
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        })
        allowed_suffix.update(source_years)
        if candidate_year:
            allowed_suffix.add(candidate_year)
    if allow_bare_number:
        allowed_suffix.update({allow_bare_number, allow_bare_number.zfill(2), allow_bare_number.zfill(3)})
        allowed_suffix.update({
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        })
    if target_year:
        allowed_suffix.add(target_year)
    allowed_suffix.update(target_publisher.split())
    return bool(all(token in allowed_suffix for token in release_tokens[len(target_tokens) :]))


def _collected_singleton_exact_title_match(candidate, wanted_item, target, evidence):
    if not target.get("collected_singleton_proof"):
        return False
    target_markers = {
        str(marker or "").strip().lower()
        for marker in (target.get("collected_singleton_markers") or [])
        if str(marker or "").strip()
    }
    if not target_markers:
        return False
    if any(
        (
            evidence.get("issue_number"),
            evidence.get("chapter_number"),
            evidence.get("volume_number"),
            evidence.get("book_number"),
            evidence.get("coverage_start"),
            evidence.get("coverage_end"),
            evidence.get("pack_marker"),
            evidence.get("ambiguous"),
            candidate.get("pack"),
            candidate.get("pack_candidate"),
            candidate.get("preview_or_sample"),
        )
    ):
        return False
    parsed_sources = evidence.get("sources") if isinstance(evidence.get("sources"), list) else []
    all_candidate_markers = {
        str(marker or "").strip().lower()
        for marker in (evidence.get("edition_markers") or [evidence.get("edition_marker")])
        if str(marker or "").strip()
    }
    for source in parsed_sources:
        if any(
            source.get(key)
            for key in (
                "issue_number",
                "chapter_number",
                "volume_number",
                "book_number",
                "coverage_start",
                "coverage_end",
                "pack_marker",
                "preview_or_sample",
                "ambiguous",
            )
        ):
            return False
        all_candidate_markers.update(
            str(marker or "").strip().lower()
            for marker in (source.get("edition_markers") or [source.get("edition_marker")])
            if str(marker or "").strip()
        )
    if not all_candidate_markers or not all_candidate_markers.issubset(target_markers):
        return False
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    target_title = _normalized_title(
        _first(wanted.get("series_title"), wanted.get("series"), wanted.get("manga_title"))
    )
    release_title = str(_first(candidate.get("original_result_title"), candidate.get("title"))).strip()
    if not target_title or not release_title:
        return False
    target_tokens = target_title.split()
    release_tokens = _normalized_title(release_title).split()
    if release_tokens[: len(target_tokens)] != target_tokens:
        return False
    target_year = _year(_first(wanted.get("year"), wanted.get("release_date"), wanted.get("date")))
    candidate_year = _year(_first(candidate.get("year"), candidate.get("release_date"), evidence.get("year")))
    if target_year and candidate_year and target_year != candidate_year:
        return False
    source_years = {str(source.get("year") or "") for source in parsed_sources if source.get("year")}
    if target_year and any(year != target_year for year in source_years):
        return False
    suffix_tokens = release_tokens[len(target_tokens) :]
    allowed_suffix = set(SINGLETON_NEUTRAL_SUFFIX_TOKENS)
    allowed_suffix.update(
        {
            "complete",
            "collection",
            "collected",
            "deluxe",
            "edition",
            "essential",
            "hardcover",
            "hc",
            "library",
            "omnibus",
            "paperback",
            "tpb",
            "trade",
            "volume",
        }
    )
    if target_year:
        allowed_suffix.add(target_year)
    if all(token in allowed_suffix for token in suffix_tokens):
        return True
    release_suffix = list(suffix_tokens)
    if release_suffix and target_year and release_suffix[0] == target_year:
        release_suffix.pop(0)
    while release_suffix and release_suffix[0] in SINGLETON_NEUTRAL_SUFFIX_TOKENS:
        release_suffix.pop(0)
    return tuple(release_suffix) in TRUSTED_RELEASE_GROUP_SUFFIXES


def _collected_singleton_alias_volume_match(candidate, wanted_item, target, evidence):
    """Match one proven collected artifact published under an imprint alias."""

    if not target.get("collected_singleton_proof"):
        return False
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    aliases = wanted.get("collected_singleton_title_aliases")
    aliases = aliases if isinstance(aliases, (list, tuple, set)) else []
    alias_keys = {_normalized_title(value) for value in aliases if _normalized_title(value)}
    wanted_number = target.get("issue_number")
    if not alias_keys or not wanted_number or evidence.get("volume_number") != wanted_number:
        return False
    if any(
        (
            evidence.get("issue_number"), evidence.get("chapter_number"), evidence.get("book_number"),
            evidence.get("coverage_start"), evidence.get("coverage_end"), evidence.get("edition_marker"),
            evidence.get("pack_marker"), evidence.get("ambiguous"), candidate.get("pack"),
            candidate.get("pack_candidate"), candidate.get("preview_or_sample"),
        )
    ):
        return False
    parsed_sources = evidence.get("sources") if isinstance(evidence.get("sources"), list) else []
    for source in parsed_sources:
        if any(
            source.get(key)
            for key in (
                "issue_number", "chapter_number", "book_number", "coverage_start", "coverage_end",
                "edition_marker", "pack_marker", "preview_or_sample", "ambiguous",
            )
        ):
            return False
        if source.get("volume_number") and source.get("volume_number") != wanted_number:
            return False
    release_title = str(_first(candidate.get("original_result_title"), candidate.get("title"))).strip()
    if not release_title:
        return False
    # Count before path/alias normalization so no separator can erase a
    # second explicit volume identity.
    explicit_volume_tokens = list(VOLUME_RE.finditer(release_title))
    release_leaf = re.split(r"[\\/]", release_title)[-1]
    # Reject multiple explicit volume identities independent of separator.
    # This covers punctuation, words, and whitespace without trying to
    # maintain an open-ended separator allow/deny list. The compact range
    # check remains necessary for forms such as ``v1_2`` where only the first
    # endpoint repeats the unit marker.
    if len(explicit_volume_tokens) != 1 or COMPACT_VOLUME_RANGE_RE.search(release_title):
        return False

    def strip_neutral_group(match):
        group = match.group(1) if match.group(1) is not None else match.group(2)
        tokens = _normalized_title(group).split()
        if not tokens:
            return match.group(0)
        if (
            all(token in SINGLETON_NEUTRAL_SUFFIX_TOKENS or YEAR_RE.fullmatch(token) for token in tokens)
            or tuple(tokens) in TRUSTED_RELEASE_GROUP_SUFFIXES
        ):
            return " "
        # Unknown parenthetical/bracketed words may be identity-bearing. Keep
        # them in the comparison so they fail the exact deterministic alias.
        return match.group(0)

    neutral_title = re.sub(r"\(([^)]*)\)|\[([^]]*)\]", strip_neutral_group, release_leaf)
    neutral_title = re.sub(r"\.(?:cbz|cbr|pdf|epub|zip|rar|7z)$", " ", neutral_title, flags=re.I)
    neutral_title = VOLUME_RE.sub(" ", neutral_title)
    normalized_neutral = _normalized_title(neutral_title)
    flattened_alias_match = False
    for alias_key in alias_keys:
        alias_tokens = alias_key.split()
        release_tokens = normalized_neutral.split()
        if release_tokens[: len(alias_tokens)] != alias_tokens:
            continue
        suffix = release_tokens[len(alias_tokens) :]
        while suffix and (
            suffix[0] in SINGLETON_NEUTRAL_SUFFIX_TOKENS or YEAR_RE.fullmatch(suffix[0])
        ):
            suffix.pop(0)
        if not suffix or tuple(suffix) in TRUSTED_RELEASE_GROUP_SUFFIXES:
            flattened_alias_match = True
            break
    if normalized_neutral not in alias_keys and not flattened_alias_match:
        return False
    target_year = _year(_first(wanted.get("year"), wanted.get("release_date"), wanted.get("date")))
    source_years = {
        str(year)
        for year in [evidence.get("year"), *(source.get("year") for source in parsed_sources)]
        if year
    }
    # Older source editions may satisfy a later collected reprint; a future
    # source year is a conflicting identity and remains blocked.
    if target_year and any(year.isdigit() and int(year) > int(target_year) for year in source_years):
        return False
    return True


def _collected_singleton_alias_exact_title_match(candidate, wanted_item, target, evidence):
    """Accept an unnumbered artifact only for a proven collected singleton alias."""

    if not target.get("collected_singleton_proof"):
        return False
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    aliases = wanted.get("collected_singleton_title_aliases")
    aliases = aliases if isinstance(aliases, (list, tuple, set)) else []
    alias_tokens = [_normalized_title(value).split() for value in aliases if _normalized_title(value)]
    if not alias_tokens:
        return False
    if any(
        (
            evidence.get("issue_number"), evidence.get("chapter_number"), evidence.get("volume_number"),
            evidence.get("book_number"), evidence.get("coverage_start"), evidence.get("coverage_end"),
            evidence.get("pack_marker"), evidence.get("ambiguous"), candidate.get("pack"),
            candidate.get("pack_candidate"), candidate.get("preview_or_sample"),
        )
    ):
        return False
    parsed_sources = evidence.get("sources") if isinstance(evidence.get("sources"), list) else []
    for source in parsed_sources:
        if any(
            source.get(key)
            for key in (
                "issue_number", "chapter_number", "volume_number", "book_number", "coverage_start",
                "coverage_end", "pack_marker", "preview_or_sample", "ambiguous",
            )
        ):
            return False
    target_markers = set(target.get("collected_singleton_markers") or [])
    candidate_markers = {
        str(marker or "").strip().lower()
        for marker in (evidence.get("edition_markers") or [evidence.get("edition_marker")])
        if str(marker or "").strip()
    }
    for source in parsed_sources:
        candidate_markers.update(
            str(marker or "").strip().lower()
            for marker in (source.get("edition_markers") or [source.get("edition_marker")])
            if str(marker or "").strip()
        )
    if candidate_markers and not candidate_markers.issubset(target_markers):
        return False
    release_title = re.sub(
        r"(?i)\.(?:cbz|cbr|pdf|epub)$", "",
        str(_first(candidate.get("original_result_title"), candidate.get("title"))).strip(),
    ).strip()
    release_tokens = _normalized_title(release_title).split()
    matching_alias = next(
        (tokens for tokens in sorted(alias_tokens, key=len, reverse=True) if release_tokens[: len(tokens)] == tokens),
        None,
    )
    if not matching_alias:
        return False
    target_year = _year(_first(wanted.get("year"), wanted.get("release_date"), wanted.get("date")))
    candidate_year = _year(_first(candidate.get("year"), candidate.get("release_date"), evidence.get("year")))
    if target_year and candidate_year and target_year != candidate_year:
        return False
    source_years = {str(source.get("year") or "") for source in parsed_sources if source.get("year")}
    if target_year and any(year != target_year for year in source_years):
        return False
    target_publisher = _normalized_title(wanted.get("publisher"))
    candidate_publisher = _normalized_title(
        _first(candidate.get("publisher"), candidate.get("source_publisher"), candidate.get("provider_publisher"))
    )
    if target_publisher and candidate_publisher and target_publisher != candidate_publisher:
        return False
    suffix = release_tokens[len(matching_alias) :]
    allowed_suffix = set(SINGLETON_NEUTRAL_SUFFIX_TOKENS)
    allowed_suffix.update(target_markers)
    if target_year:
        allowed_suffix.add(target_year)
    allowed_suffix.update(target_publisher.split())
    return all(token in allowed_suffix for token in suffix)


def _strip_series_prefix(value, wanted_item=None):
    text = str(value or "").strip()
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    series = str(_first(wanted.get("series_title"), wanted.get("series"), wanted.get("manga_title"))).strip()
    if not text or not series:
        return text
    pattern = re.escape(series).replace(r"\ ", r"[\W_]+")
    match = re.match(rf"(?i)^\s*{pattern}", text)
    if not match:
        return text
    remainder = text[match.end():].lstrip()
    # A leading hash is issue syntax, not disposable title punctuation. Keep
    # it so date-shaped issue ranges remain explicit after title removal.
    leading_hash = re.match(r"^[\W_]*#", remainder)
    if leading_hash:
        stripped = remainder[leading_hash.end() - 1:].strip()
    else:
        stripped = re.sub(r"^[\W_]+", "", remainder).strip()
    return stripped or text


def _candidate_text_values(candidate, wanted_item=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    values = []
    for label, value in (
        ("release_title", _first(candidate.get("original_result_title"), candidate.get("title"))),
        ("filename", _first(candidate.get("filename"), candidate.get("remote_filename"))),
        ("source_path", candidate.get("path")),
    ):
        text = str(value or "").strip()
        if not text:
            continue
        if label == "source_path":
            text = PurePath(text.replace("\\", "/")).name
        if text and not any(existing[1] == text for existing in values):
            values.append((label, text, _strip_series_prefix(text, wanted_item)))
    return values


def normalize_candidate(candidate, wanted_item=None):
    out = dict(candidate or {})
    raw = out.get("raw") if isinstance(out.get("raw"), dict) else {}
    raw_result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    for target_key, source_key in (
        ("query_variant", "_inkdrop_query_variant"),
        ("query_group", "_inkdrop_query_group"),
        ("request_id", "_inkdrop_request_id"),
    ):
        if not out.get(target_key) and raw_result.get(source_key) not in (None, ""):
            out[target_key] = raw_result.get(source_key)
    parsed_sources = []
    for label, text, identity_text in _candidate_text_values(out, wanted_item):
        parsed = parse_release_title(identity_text)
        parsed["original_title"] = text
        parsed["parsed_identity_text"] = identity_text
        parsed["evidence_source"] = label
        parsed_sources.append(parsed)
    primary = parsed_sources[0] if parsed_sources else parse_release_title("")
    conflicts = []
    unit_fields = ("volume_number", "book_number", "issue_number", "chapter_number")
    for field in ("volume_number", "book_number", "issue_number", "chapter_number"):
        values = {row.get(field) for row in parsed_sources if row.get(field)}
        if len(values) > 1:
            conflicts.append(field)
        elif values and not primary.get(field):
            primary[field] = next(iter(values))

    explicit_source = {
        "volume_number": _number(_first(out.get("source_volume_number"), out.get("provider_volume_number"), out.get("volume"))),
        "chapter_number": _number(_first(out.get("source_chapter_number"), out.get("provider_chapter_number"), out.get("chapter"))),
        "issue_number": _number(_first(out.get("source_issue_number"), out.get("provider_issue_number"))),
        "book_number": _number(_first(out.get("source_book_number"), out.get("provider_book_number"))),
    }
    for field, value in explicit_source.items():
        parsed_value = primary.get(field)
        if value and parsed_value and value != parsed_value:
            conflicts.append(field)
        if value:
            primary[field] = value

    present_unit_fields = sorted(field for field in unit_fields if primary.get(field))
    source_ambiguous = any(bool(row.get("ambiguous")) for row in parsed_sources)
    primary["ambiguous"] = bool(source_ambiguous or len(present_unit_fields) > 1)
    primary["present_unit_fields"] = present_unit_fields

    out["source_unit_evidence"] = {
        **{key: value for key, value in primary.items() if key != "original_title"},
        "sources": parsed_sources,
        "conflicts": sorted(set(conflicts)),
    }
    out["parsed_unit_type"] = primary.get("unit_type") or ""
    for field in (
        "volume_number",
        "book_number",
        "issue_number",
        "chapter_number",
        "coverage_start",
        "coverage_end",
        "edition_marker",
        "pack_marker",
    ):
        out[f"parsed_{field}"] = primary.get(field) or ""
    out["preview_or_sample"] = bool(primary.get("preview_or_sample"))
    out["pack_candidate"] = bool(out.get("pack") or primary.get("pack_marker"))
    out["query_provenance"] = {
        "query": str(_first(out.get("query_variant"), out.get("source_search_query"), out.get("search_query"))).strip(),
        "query_group": str(out.get("query_group") or "").strip(),
        "request_id": str(out.get("request_id") or "").strip(),
    }
    return out


def collected_singleton_alias_exact_title_match(candidate, wanted_item=None):
    """Return whether a raw result is an exact alias for a proven collected singleton."""

    normalized = normalize_candidate(candidate, wanted_item)
    target = target_context(wanted_item)
    return _collected_singleton_alias_exact_title_match(
        normalized,
        wanted_item,
        target,
        normalized["source_unit_evidence"],
    )


def candidate_compatibility(candidate, wanted_item=None):
    candidate = normalize_candidate(candidate, wanted_item)
    target = target_context(wanted_item)
    evidence = candidate["source_unit_evidence"]
    provider = str(candidate.get("provider_id") or candidate.get("source") or "").strip().lower()
    if (
        not target.get("unit_type_explicit")
        and target.get("media_type") == "manga"
        and provider in {"mangadex", "suwayomi"}
        and target.get("issue_number")
    ):
        target["unit_type"] = "chapter"
        target["chapter_number"] = target.get("chapter_number") or target.get("issue_number")
    blocked = []
    review = []
    positive = []
    manifest_match = candidate.get("pack_contents_match") if isinstance(candidate.get("pack_contents_match"), dict) else {}
    manifest_exact_member = manifest_match.get("coverage_source") in {
        "pack_contents_filename",
        "pack_contents_volume_filename",
    }
    manifest_entry = (
        manifest_match.get("entry")
        or manifest_match.get("member")
        or manifest_match.get("filename")
        or candidate.get("pack_contents_matching_entry")
    )
    source_identity_values = (
        (manifest_entry,)
        if manifest_exact_member and manifest_entry
        else (
            candidate.get("original_result_title"),
            candidate.get("title"),
            candidate.get("filename"),
            candidate.get("remote_filename"),
            candidate.get("path"),
        )
    )
    source_identity_gate = inkdrop_artifact_acceptance.source_identity_acceptance(
        " ".join(
            str(value or "")
            for value in source_identity_values
            if value
        ),
        wanted_item,
    )
    if not source_identity_gate.get("ok"):
        blocked.append(source_identity_gate.get("reason") or "source_identity_rejected")
    collected_singleton_match = _collected_singleton_exact_title_match(
        candidate,
        wanted_item,
        target,
        evidence,
    )
    collected_singleton_alias_exact_match = _collected_singleton_alias_exact_title_match(
        candidate,
        wanted_item,
        target,
        evidence,
    )
    collected_singleton_match = bool(collected_singleton_match or collected_singleton_alias_exact_match)
    collected_singleton_alias_volume_match = _collected_singleton_alias_volume_match(
        candidate,
        wanted_item,
        target,
        evidence,
    )
    singleton_exact_match = _singleton_exact_title_match(
        candidate,
        wanted_item,
        target,
        evidence,
        allow_bare_number=(
            target.get("issue_number")
            if target.get("unit_type") in ISSUE_UNITS
            else ""
        ),
    )

    match_confidence = str(candidate.get("match_confidence") or "").strip().lower().replace("-", "_")
    if (
        match_confidence == "mismatch"
        and not collected_singleton_alias_exact_match
        and not singleton_exact_match
    ):
        blocked.append("candidate_title_mismatch")
    elif (
        match_confidence.startswith("related_series")
        or match_confidence in {"subseries", "related_title"}
    ) and not singleton_exact_match:
        blocked.append("related_series_identity")

    wanted_creators = _trusted_wanted_creators(wanted_item)
    candidate_creators = _candidate_asserted_creators(candidate)
    if wanted_creators and candidate_creators and not set(wanted_creators).intersection(candidate_creators):
        blocked.append("creator_identity_conflict")

    if candidate.get("preview_or_sample"):
        blocked.append("preview_or_sample")
    if candidate.get("known_bad_candidate") or str(candidate.get("source_memory_status") or "").lower() == "known_bad":
        blocked.append("known_bad_candidate")
    hierarchical_chapter_identity = bool(
        target.get("unit_type") in CHAPTER_UNITS
        and evidence.get("chapter_number")
        and set(evidence.get("present_unit_fields") or ()) <= {"volume_number", "chapter_number"}
        and not evidence.get("conflicts")
        and not evidence.get("book_number")
        and not evidence.get("issue_number")
        and not evidence.get("coverage_start")
        and not evidence.get("coverage_end")
    )
    if evidence.get("conflicts") or (evidence.get("ambiguous") and not hierarchical_chapter_identity):
        blocked.append("ambiguous_unit_identity")

    edition = evidence.get("edition_marker") or ""
    target_unit = target.get("unit_type") or ""
    if edition in COLLECTED_MARKERS and target_unit in (VOLUME_UNITS | ISSUE_UNITS | CHAPTER_UNITS):
        if not target.get("allow_collected_edition") and not collected_singleton_match and not singleton_exact_match:
            blocked.append("collected_edition_disallowed")

    if target_unit in VOLUME_UNITS:
        wanted = target.get("volume_number")
        found = evidence.get("volume_number") or evidence.get("book_number")
        if (evidence.get("coverage_start") or evidence.get("coverage_end")) and not manifest_exact_member:
            blocked.append("coverage_not_unit_number")
        if (
            wanted
            and evidence.get("issue_number") == wanted
            and evidence.get("bare_number") == wanted
            and _singleton_exact_title_match(
                candidate, wanted_item, target, evidence, allow_bare_number=wanted
            )
        ):
            positive.append("singleton_exact_bare_volume_number")
        elif evidence.get("chapter_number") or evidence.get("issue_number"):
            blocked.append("wrong_unit_type")
        elif found and wanted and found != wanted:
            blocked.append("wrong_volume_number")
        elif found and wanted == found:
            positive.append("exact_volume_number")
        elif wanted and manifest_exact_member:
            positive.append("exact_pack_manifest_member")
        elif wanted and singleton_exact_match:
            positive.append("singleton_exact_title")
        elif wanted:
            review.append("missing_required_unit_number")
    elif target_unit in ISSUE_UNITS:
        wanted = target.get("issue_number")
        if (evidence.get("coverage_start") or evidence.get("coverage_end")) and not manifest_exact_member:
            blocked.append("coverage_not_unit_number")
        elif collected_singleton_alias_volume_match:
            positive.append("collected_singleton_alias_volume")
        elif evidence.get("volume_number") or evidence.get("book_number") or evidence.get("chapter_number"):
            blocked.append("wrong_unit_type")
        elif evidence.get("issue_number") and wanted and evidence.get("issue_number") != wanted:
            blocked.append("wrong_issue_number")
        elif evidence.get("issue_number") and evidence.get("issue_number") == wanted:
            positive.append("exact_issue_number")
            if singleton_exact_match:
                positive.append("singleton_exact_title")
        elif wanted and manifest_exact_member:
            positive.append("exact_pack_manifest_member")
        elif wanted and singleton_exact_match:
            positive.append("singleton_exact_title")
        elif wanted and collected_singleton_match:
            positive.append(
                "collected_singleton_alias_exact_title"
                if collected_singleton_alias_exact_match
                else "collected_singleton_exact_title"
            )
        elif wanted:
            review.append("missing_required_unit_number")
    elif target_unit in CHAPTER_UNITS:
        wanted = target.get("chapter_number")
        if (evidence.get("coverage_start") or evidence.get("coverage_end")) and not manifest_exact_member:
            blocked.append("coverage_not_unit_number")
        elif (evidence.get("volume_number") or evidence.get("book_number") or evidence.get("issue_number")) and not evidence.get("chapter_number"):
            blocked.append("wrong_unit_type")
        elif evidence.get("chapter_number") and wanted and evidence.get("chapter_number") != wanted:
            blocked.append("wrong_chapter_number")
        elif evidence.get("chapter_number") and evidence.get("chapter_number") == wanted:
            positive.append("exact_chapter_number")
        elif wanted and manifest_exact_member:
            positive.append("exact_pack_manifest_member")
        elif wanted:
            review.append("missing_required_unit_number")

    blocked = list(dict.fromkeys(blocked))
    review = [reason for reason in dict.fromkeys(review) if reason not in blocked]
    status = "blocked" if blocked else ("review" if review else "compatible")
    return {
        "compatibility_contract_version": CONTRACT_VERSION,
        "status": status,
        "target": target,
        "source": evidence,
        "positive_evidence": positive,
        "rejection_codes": blocked,
        "review_codes": review,
        "rejection_explanations": [
            {"code": code, "explanation": _explanation(code, target, evidence)} for code in blocked
        ],
        "review_explanations": [
            {"code": code, "explanation": _explanation(code, target, evidence)} for code in review
        ],
        "explanation": _explanation(blocked[0] if blocked else (review[0] if review else ""), target, evidence),
    }


def _explanation(reason, target, source):
    messages = {
        "wrong_unit_type": "The result is a different unit type than the wanted target.",
        "wrong_volume_number": f"Wanted volume {target.get('volume_number')}; result identifies volume {source.get('volume_number') or source.get('book_number')}.",
        "wrong_issue_number": f"Wanted issue {target.get('issue_number')}; result identifies issue {source.get('issue_number')}.",
        "wrong_chapter_number": f"Wanted chapter {target.get('chapter_number')}; result identifies chapter {source.get('chapter_number')}.",
        "coverage_not_unit_number": "A collected coverage range is not the artifact's own issue or chapter number.",
        "preview_or_sample": "The result is marked as a preview or sample.",
        "collected_edition_disallowed": "A collected edition cannot replace this exact target.",
        "missing_required_unit_number": "The result does not identify the required unit number.",
        "ambiguous_unit_identity": "Release-title and filename unit evidence disagree or are ambiguous.",
        "known_bad_candidate": "The candidate is present in durable bad-candidate memory.",
        "candidate_title_mismatch": "The provider result does not match the wanted series identity.",
        "related_series_identity": "The provider result belongs to a related or child series, not the wanted series.",
        "creator_identity_conflict": "The release names a different creator than the trusted wanted identity.",
        "wrong_unit_type_chapter_for_comic_issue": "Manga-style volume/chapter identity cannot satisfy a western comic issue.",
    }
    return messages.get(reason, "Candidate unit identity is compatible with the target.")


def apply_compatibility(verdict, wanted_item=None):
    out = normalize_candidate(verdict, wanted_item)
    compatibility = candidate_compatibility(out, wanted_item)
    out["target_compatibility"] = compatibility
    blocks = list(dict.fromkeys([*(out.get("block_reasons") or []), *compatibility["rejection_codes"]]))
    reviews = list(dict.fromkeys([*(out.get("review_reasons") or []), *compatibility["review_codes"]]))
    singleton_exact_title = bool(
        {
            "singleton_exact_title", "collected_singleton_exact_title",
            "collected_singleton_alias_exact_title", "collected_singleton_alias_volume",
        }
        & set(compatibility.get("positive_evidence", []))
    )
    trusted_singleton_exact_title = "singleton_exact_title" in compatibility.get("positive_evidence", [])
    if trusted_singleton_exact_title:
        blocks = [
            reason
            for reason in blocks
            if reason not in {"candidate_title_mismatch", "related_series_identity"}
        ]
    if "collected_singleton_alias_exact_title" in compatibility.get("positive_evidence", []):
        blocks = [reason for reason in blocks if reason != "candidate_title_mismatch"]
    singleton_number_review_cleared = bool(singleton_exact_title and "issue_number_not_confirmed" in reviews)
    if singleton_number_review_cleared:
        reviews = [reason for reason in reviews if reason != "issue_number_not_confirmed"]
    out["block_reasons"] = blocks
    out["review_reasons"] = reviews
    if compatibility["rejection_codes"]:
        out["candidate_safe"] = False
        out["artifact_safe"] = False
        out["auto_grab_verdict"] = "blocked"
        out["review_reason"] = compatibility["rejection_codes"][0]
        out["quality_status"] = "rejected"
    elif compatibility["review_codes"] and out.get("auto_grab_verdict") != "blocked":
        out["candidate_safe"] = False
        out["artifact_safe"] = False
        out["auto_grab_verdict"] = "review"
        out["review_reason"] = compatibility["review_codes"][0]
        if out.get("quality_status") == "accepted":
            out["quality_status"] = "review"
    elif singleton_exact_title and not blocks and not reviews:
        out["candidate_safe"] = True
        out["artifact_safe"] = True
        out["auto_grab_verdict"] = "auto_grab_safe"
        out["review_reason"] = ""
        out["quality_status"] = "accepted"
    return out


def _stable_hash(label, parts, length=24):
    payload = json.dumps([label, *parts], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def stable_candidate_identities(candidate):
    """Return logical and source-instance identities without exposing users."""
    normalized = normalize_candidate(candidate)
    source = normalized["source_unit_evidence"]
    provider = str(normalized.get("provider_id") or normalized.get("source") or "").strip().lower()
    child = str(
        _first(
            normalized.get("indexer_id"),
            normalized.get("source_id"),
            normalized.get("child_source_id"),
            normalized.get("indexer"),
            normalized.get("source_name"),
        )
    ).strip().lower()
    stable_locator = str(
        _first(
            normalized.get("info_hash"),
            normalized.get("guid"),
            normalized.get("provider_result_id"),
            normalized.get("canonical_item_id"),
            normalized.get("chapter_id"),
            normalized.get("mangadex_chapter_id"),
            normalized.get("suwayomi_chapter_id"),
        )
    ).strip().lower()
    filename = str(_first(normalized.get("filename"), normalized.get("remote_filename"), normalized.get("title"))).replace("\\", "/")
    remote_directory = str(_first(normalized.get("remote_directory"), PurePath(filename).parent.as_posix() if "/" in filename else "")).lower()
    semantic = [
        provider,
        child,
        str(normalized.get("protocol") or "").lower(),
        stable_locator or _normalized_title(normalized.get("title") or filename),
        source.get("unit_type"),
        source.get("volume_number"),
        source.get("book_number"),
        source.get("issue_number"),
        source.get("chapter_number"),
        source.get("coverage_start"),
        source.get("coverage_end"),
        source.get("edition_marker"),
        source.get("year"),
        str(normalized.get("size_bytes") or normalized.get("size") or ""),
        remote_directory,
    ]
    family = _stable_hash("candidate_family", semantic)
    private_user = str(_first(normalized.get("username"), normalized.get("remote_user"), normalized.get("user"))).strip().lower()
    instance = _stable_hash("candidate_instance", [family, _stable_hash("remote_user", [private_user]) if private_user else ""])
    identities = {"candidate_family_identity": family, "candidate_instance_identity": instance}
    content_hash = str(_first(normalized.get("content_hash"), normalized.get("sha256"))).strip().lower()
    if content_hash:
        identities["content_identity"] = _stable_hash("candidate_content", [content_hash])
    return identities
