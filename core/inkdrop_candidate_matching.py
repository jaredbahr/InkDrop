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
from pathlib import PurePath, PurePosixPath

from core import inkdrop_acquisition_policy
from core import inkdrop_artifact_acceptance
from core import inkdrop_release_credits
from core import inkdrop_title_identity


CONTRACT_VERSION = 2

VOLUME_UNITS = {"volume", "vol", "book_volume", "manga_volume"}
ISSUE_UNITS = {"issue", "comic_issue"}
CHAPTER_UNITS = {"chapter", "manga_chapter"}
COMIC_MEDIA_TYPES = {
    "comic",
    "comics",
    "comic_issue",
    "graphic novel",
    "graphic_novel",
    "western comic",
    "western_comic",
}
# Rejection reasons that report that something is wrong without reporting
# what. Ordered least specific last; see the reordering in
# candidate_compatibility().
GENERIC_REJECTION_ORDER = ("ambiguous_unit_identity", "candidate_title_mismatch")
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
# Literal filename words for every collected-singleton format marker above --
# already treated as a neutral (non-identity-bearing) title suffix regardless
# of which specific printing the title names, so an edition-indifferent
# target can accept any of them without widening what a plain word-for-word
# suffix match allows.
EDITION_FORMAT_SUFFIX_WORDS = {
    "complete", "collection", "collected", "deluxe", "edition", "essential",
    "hardcover", "hc", "library", "omnibus", "paperback", "tpb", "trade", "volume",
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
ROMAN_ONLY_TOKEN = r"[ivxlc]+"
NUMBER_TOKEN_NO_ROMAN = rf"(?:\d+(?:\.\d+)?|{WORD_TOKEN})"

PREVIEW_RE = re.compile(r"(?i)\b(?:preview|sample|excerpt|teaser)\b")
# The bare "v" prefix is a separate alternative from vol(ume)/tome/band: those
# are unambiguous whole words, but a lone "v" glued directly (no separator)
# to more Roman-numeral-valid letters collides with real editorial
# abbreviations -- "VC" (variant cover, credited e.g. "VC - Derrick Chew")
# parses as prefix "v" + Roman "C" = volume 100, fabricating a volume
# identity that then wrongly blocks the real candidate. Digits/number-words
# after a bare "v" stay separator-optional ("v3", "v09") since those aren't
# ambiguous with anything; a Roman-numeral token after a bare "v" now
# requires an actual separator (a period or whitespace), which real Roman
# volume markers ("V. IX", "V IX") already carry.
# The marker words each unit axis answers to, named once so the abbreviations
# below can be derived from them instead of listed beside them.
VOLUME_MARKER_WORDS = ("vol", "volume", "tome", "band")
BOOK_MARKER_WORDS = ("book",)
CHAPTER_MARKER_WORDS = ("chapter", "chap", "ch", "c")
ISSUE_MARKER_WORDS = ("issue", "iss", "no", "number")


def _unambiguous_axis_initials(words, *other_axes):
    """First letters of this axis's markers that no other axis also claims.

    ``T01`` is a French volume marker -- T for *tome* -- and ``tome`` is
    already a word this grammar knows, so the abbreviation is derivable from
    the vocabulary rather than from a translation table. Add a marker word in
    any language later and its abbreviation comes free; nothing here knows
    what language anything is in. This is the same move as generating
    acronyms from tracked titles rather than storing them.

    An initial two axes share is not usable: ``band`` (volume) and ``book``
    both begin with B, so a bare ``B01`` names two different axes and the
    honest answer is that it names neither. That collision is discovered from
    the vocabulary, not remembered.
    """
    claimed = {word[0].lower() for axis in other_axes for word in axis}
    return sorted({word[0].lower() for word in words} - claimed)


VOLUME_BARE_INITIALS = _unambiguous_axis_initials(
    VOLUME_MARKER_WORDS, BOOK_MARKER_WORDS, CHAPTER_MARKER_WORDS, ISSUE_MARKER_WORDS
)
_VOLUME_INITIAL_CLASS = "".join(VOLUME_BARE_INITIALS)
VOLUME_RE = re.compile(
    rf"(?i)\b(?:vol(?:ume)?|tome|band)\.?\s*0*({NUMBER_TOKEN})\b"
    rf"|\b[{_VOLUME_INITIAL_CLASS}](?:\.?\s*0*({NUMBER_TOKEN_NO_ROMAN})|[.\s]\s*0*({ROMAN_ONLY_TOKEN}))\b"
)
BOOK_RE = re.compile(rf"(?i)\bbook\s+0*({NUMBER_TOKEN})\b")
CHAPTER_RE = re.compile(rf"(?i)\b(?:chapter|chap|ch|c)\.?\s*#?\s*0*({NUMBER_TOKEN})\b")
ISSUE_RE = re.compile(rf"(?i)(?:\b(?:issue|iss|no|number)\.?\s*#?\s*|#)0*({NUMBER_TOKEN})\b")
# Same as NUMBER_TOKEN, but the roman-numeral branch may not begin mid-word.
# Unanchored, "[ivxlc]+" eats the tail of an ordinary word: the "l" of
# "Digital" reads as 50, so "(Digital-1920)" parsed as a range of issues
# 50-1920 and "Fairy Tail - 100 Years" as 49-100. Both turned a single book
# into a pack and refused it with coverage_not_unit_number. The digit branch
# stays unanchored on purpose -- "ch1-249" and "c129-131" are real chapter
# ranges whose prefix letter sits directly against the number.
COVERAGE_NUMBER_TOKEN = rf"(?:\d+(?:\.\d+)?|{WORD_TOKEN}|(?<![A-Za-z])[ivxlc]+)"
COVERAGE_RE = re.compile(
    rf"(?i)(?:\b(?:issues?|chapters?|chs?)\b\s*)?#?\s*0*({COVERAGE_NUMBER_TOKEN})\s*"
    rf"(?:-|\+|\bto\b|\bthrough\b)\s*#?\s*0*({NUMBER_TOKEN})\b"
)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
PUBLICATION_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})-(\d{1,2})(?:-(\d{1,2}))?(?![-\d])"
)
BRACKETED_MONTH_YEAR_RE = re.compile(
    r"(?P<open>[\[(])\s*(?P<month>\d{1,2})-(?P<year>(?:19|20)\d{2})\s*(?P<close>[\])])"
)
# "(3, 2025)": a bracketed month and year with a comma. The same claim the
# "(3-2025)" form above makes; left unread, its month counted as a second
# unit number and kept an exact issue at review (2026-09-09).
BRACKETED_MONTH_COMMA_YEAR_RE = re.compile(
    r"(?P<open>[\[(])\s*(?P<month>\d{1,2})\s*,\s*(?P<year>(?:19|20)\d{2})\s*(?P<close>[\])])"
)
# "V2014", "v.2014", "Vol.2025", "Volume 2014": a run year, whichever way the
# volume word is spelled. Accepting only the bare initial left "Vol.2025 #001"
# at review while "V2025 #001" was safe.
V_YEAR_RE = re.compile(r"(?i)\b(?:v|vol(?:ume)?)\.?\s*0*((?:19|20)\d{2})\b")


def _bracketed_month_year_matches(text):
    """Both bracketed month-year spellings, in text order."""
    matches = list(BRACKETED_MONTH_YEAR_RE.finditer(text)) + list(BRACKETED_MONTH_COMMA_YEAR_RE.finditer(text))
    return sorted(matches, key=lambda match: match.start())
# A year sitting next to one of these names which *printing* the artifact is,
# not which run it belongs to: "The Sandman 001 (2023 Reprint)" is the 1989
# issue on 2023 paper, so its year says nothing about series identity.
REPRINT_MARKER_RE = re.compile(
    r"(?i)\b(?:"
    r"re-?print(?:ing|ings|s|ed)?"
    r"|(?:\d{1,2}(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth)\s*print(?:ing|ings|s)?"
    r"|facsimile"
    r"|remaster(?:ed)?"
    r")\b"
)
UNIT_PREFIX_TOKENS = {
    "book", "books", "c", "ch", "chap", "chapter", "chapters", "chs",
    "iss", "issue", "issues", "no", "number", "numbers", "tome", "tomes",
    "v", "vol", "volume", "volumes",
}
# How many variant covers the release packs, counted immediately before the
# word: "Spawn.352.2024.2.covers.Digital-Empire" ships issue 352 with two
# cover scans. The count is release metadata, and the bare-number scan below
# only settles on a unit when every bare number it sees agrees -- so the "2"
# outvoted the real "352" and the whole release was refused with
# missing_required_unit_number. Only a count that *precedes* the word is
# release metadata; "Covers 003" names a work called Covers and is untouched.
COVER_COUNT_SUFFIX_RE = re.compile(r"(?i)^[\s._\-]*covers?(?![A-Za-z])")
# How many issues the miniseries runs to, written in the standard comic form
# "01 (of 03)". Same failure as the cover count above and the same cure: the
# total is release metadata, not a unit, but the bare-number scan saw both "1"
# and "3", found they disagreed, and settled on neither -- so
# "Superman Smashes the Klan 01 (of 03) (2019)" was refused for
# missing_required_unit_number while the identical file without the marker
# matched. The enclosing bracket is optional, because "003 of 60" is as common
# as "01 (of 03)" and means the same thing. What keeps that safe is not the
# bracket but the rule at the scan below: a number after "of" is discarded
# only when some other bare number survives to be the unit. So "Book of 5"
# keeps its 5, while "Y The Last Man 003 of 60" keeps the 3 and drops the 60.
OF_TOTAL_PREFIX_RE = re.compile(r"(?i)(?:^|[\s._\-\(\[])of\s*$")
# Which unit a parsed coverage range counts in, read off the word the uploader
# put in front of it. Deliberately wider than UNIT_PREFIX_TOKENS -- "vols. 1-35"
# names volumes just as plainly as "vol 1-35" does -- but kept as its own map so
# widening it cannot move the publication-date guard that UNIT_PREFIX_TOKENS
# feeds. A prefix this map does not know leaves the range unlabeled, which is
# the conservative answer, not an error.
COVERAGE_UNIT_BY_PREFIX = {
    "bk": "volume", "bks": "volume", "book": "volume", "books": "volume",
    "tome": "volume", "tomes": "volume",
    "v": "volume", "vol": "volume", "vols": "volume",
    "volume": "volume", "volumes": "volume",
    "c": "chapter", "ch": "chapter", "chap": "chapter", "chaps": "chapter",
    "chapter": "chapter", "chapters": "chapter", "chs": "chapter",
    "iss": "issue", "issue": "issue", "issues": "issue",
    "no": "issue", "nos": "issue", "number": "issue", "numbers": "issue",
}
CREATOR_BYLINE_RE = re.compile(
    r"(?i)\bby\s+([A-Z][A-Za-z.'\u2019-]*(?:\s+[A-Z][A-Za-z.'\u2019-]*){1,4})"
    r"(?=\s+(?:#?\d|v(?:ol(?:ume)?)?\.?\s*\d|issues?\b|chapters?\b|"
    r"digital\b|retail\b|cbr\b|cbz\b|pdf\b|epub\b|\(|\[)|\s*$)"
)

# Scene and library filenames put a creator credit in front of the work with an
# explicit delimiter. These are the delimiters, not a general separator list:
# the SIGNAL is that someone wrote one deliberately.
CREATOR_CREDIT_DELIMITERS = (" -- ", " \u2014 ", " \u2013 ", " - ")

# A credit is a person, so it is short. Four tokens covers "Gael Bertrand",
# "Alan Moore", "Kentaro Miura" and a double-barrelled name with an initial,
# and stops a long descriptive prefix from being mistaken for one.
MAX_CREATOR_CREDIT_TOKENS = 4


def release_tokens_after_creator_credit(release_title, target_tokens):
    """Re-anchor a release name past a LEADING creator credit, positively.

    Returns the token list to compare against the target, or None when there is
    no credit to skip. The caller then applies its ordinary prefix-anchored
    comparison to the result -- this widens where the anchor sits, it does not
    weaken the anchor.

    THE SIGNAL IS THE DELIMITER AND THE SHAPE OF THE CREDIT, NEVER THE ABSENCE
    OF A MATCH. This deliberately does not search for the target inside the
    release name: that would let a target "Tarot" match "A Land Called Tarot",
    a different work, and an absence rule is exactly what must not be built
    here. It splits on ONE explicit credit delimiter, checks the discarded half
    is short and alphabetic and carries no unit or format vocabulary, and hands
    back what is left for the caller to judge normally.

    Only the FIRST delimiter is consumed. "A Land Called Tarot - Gael Bertrand"
    must keep failing rather than have its title thrown away, and a work whose
    own title contains a dash keeps every token after the first segment.

    Both singleton matchers call this, for the same reason both call
    release_group_suffix_only(): #337 found them answering the trailing-tag
    question differently and fixed it with one predicate. This is the identical
    asymmetry at the other end of the string -- an irrelevant token tolerated
    after the title and fatal before it -- and it gets the identical treatment.
    """
    release_title = str(release_title or "").strip()
    target_tokens = list(target_tokens or [])
    if not release_title or not target_tokens:
        return None
    for delimiter in CREATOR_CREDIT_DELIMITERS:
        head, found, tail = release_title.partition(delimiter)
        if not found or not tail.strip():
            continue
        credit_tokens = _normalized_title(head).split()
        if not 1 <= len(credit_tokens) <= MAX_CREATOR_CREDIT_TOKENS:
            continue
        # A person's name carries no digits, no unit words and no format tags.
        # Anything that does is a description of the release, not a credit, and
        # discarding it would be discarding evidence.
        if not all(token.isalpha() for token in credit_tokens):
            continue
        if any(token in SINGLETON_NEUTRAL_SUFFIX_TOKENS for token in credit_tokens):
            continue
        if any(token in CREDIT_DISQUALIFYING_TOKENS for token in credit_tokens):
            continue
        return _normalized_title(tail).split()
    return None


# Words that make a leading segment a description of the release rather than a
# person. Kept explicit and small: every entry here is a token that, if thrown
# away, would throw away a unit or edition claim with it.
CREDIT_DISQUALIFYING_TOKENS = frozenset({
    "vol", "volume", "v", "book", "chapter", "ch", "issue", "no", "number",
    "part", "pt", "omnibus", "deluxe", "tpb", "hc", "hardcover", "paperback",
    "collection", "collected", "edition", "annual", "special", "one", "shot",
    "oneshot", "variant", "reprint", "remaster", "remastered", "series",
})


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
# Trailing tokens that name the RIPPER, not the work. "(digital) (Son of
# Ultron-Empire)" says who scanned a file; it makes no claim about which book
# is inside, so it cannot disqualify a release whose title already matched
# exactly. A closed set of whole tuples rather than a token soup: any unknown
# trailing word stays identity-bearing and still fails the match. The list
# itself lives in inkdrop_release_credits, where the indexer classifier and the
# artifact acceptor read the same one -- this path used to carry five tuples of
# its own and parked in review the very one-shots the classifier had cleared.
TRUSTED_RELEASE_GROUP_SUFFIXES = frozenset(
    inkdrop_release_credits.RELEASE_GROUP_PHRASES | inkdrop_release_credits.TRAILING_RELEASE_CREDITS
)
def release_group_suffix_only(suffix_tokens, target_year=""):
    """Whether these trailing tokens are nothing but a known release-group tag.

    Both singleton matchers ask this question. They used to answer it
    differently: the collected one fell back to TRUSTED_RELEASE_GROUP_SUFFIXES
    when its allowlist failed, the plain one did not, so an identical file
    named "<work> (2021) (digital) (Zone-Empire).cbz" was acceptable evidence
    for a proven collected singleton and not for a proven unitless work.
    Measured 2026-08-18: 52.5% of real candidate filenames carry such a group,
    and 0 of 7 newly-proven unitless rows accepted one. One predicate, read by
    both, so the two cannot drift apart again.

    This only ever runs after the caller's own identity checks have passed --
    exact leading-title match, no unit number anywhere in the evidence, year
    and publisher agreement. It answers the narrow question of whether what is
    LEFT OVER is a scene tag, never whether the release is the right work.
    """

    tokens = [str(token or "").strip().lower() for token in (suffix_tokens or [])]
    tokens = [token for token in tokens if token]
    if not tokens:
        return True
    year = str(target_year or "").strip()
    if year and tokens and tokens[0] == year:
        tokens = tokens[1:]
    while tokens and tokens[0] in SINGLETON_NEUTRAL_SUFFIX_TOKENS:
        tokens = tokens[1:]
    if not tokens:
        return True
    return tuple(tokens) in TRUSTED_RELEASE_GROUP_SUFFIXES


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
    # A standalone graphic novel / one-shot: no unit number exists to require.
    # Established from the issue's stated form, never from a row count.
    "comicvine_unitless_work_issue_form",
}
UNITLESS_WORK_PROOF_SOURCE = "comicvine_unitless_work_issue_form"
TRUSTED_COLLECTED_SINGLETON_PROOF_SOURCES = {
    "comicvine_collected_single_wanted_identity",
}


def _first(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return ""


def _strict_bool_flag(value):
    """JSON-boolean-aware parse for override flags like edition_indifferent.

    Python truthiness makes bool("false") == True, so a stray JSON string
    would silently flip an operator's "disabled" choice to enabled. Real
    booleans pass through; string "true"/"false" are honored case-
    insensitively; anything else defaults to False (disabled), never True.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return False


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


def _unit_prefix_before_number(value, number_offset):
    """Return the ``("#", "")`` or ``("", word)`` immediately before a number."""
    text = str(value or "")
    offset = max(0, min(int(number_offset or 0), len(text)))
    preceding = text[:offset].rstrip()
    if not preceding:
        return ("", "")
    if preceding.endswith((":", ".")):
        preceding = preceding[:-1].rstrip()
    if preceding.endswith("#"):
        return ("#", "")
    end = len(preceding)
    start = end
    while start > 0 and preceding[start - 1].isalpha():
        start -= 1
    return ("", preceding[start:end].lower())


def has_unit_prefix_before_number(value, number_offset):
    hash_marker, word = _unit_prefix_before_number(value, number_offset)
    return bool(hash_marker) or word in UNIT_PREFIX_TOKENS


def coverage_unit_before_number(value, number_offset):
    """Name the unit a coverage range counts in, or "" when it is unlabeled.

    A bare ``(001-040)`` says nothing about whether it collects issues,
    chapters or volumes, and guessing is how a volume batch gets grabbed for a
    wanted chapter. ``#`` is no better: it fronts issue and chapter numbers
    alike. Both stay unlabeled here and are handled by the stricter unlabeled
    rules in pack_title_range_membership().
    """
    _hash_marker, word = _unit_prefix_before_number(value, number_offset)
    return COVERAGE_UNIT_BY_PREFIX.get(word, "")


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
    # Reversed comic release stamps are safe only inside one complete,
    # matching wrapper.  A bare/prefixed ``10-2019`` remains unit coverage.
    for match in _bracketed_month_year_matches(original):
        if (match.group("open"), match.group("close")) not in {("(", ")"), ("[", "]")}:
            continue
        if has_unit_prefix_before_number(original, match.start()):
            continue
        month = match.group("month")
        year = match.group("year")
        try:
            date(int(year), int(month), 1)
        except ValueError:
            continue
        year_month = f"{year}-{int(month):02d}"
        dates.append(year_month)
        year_months.append(year_month)
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    return {
        "dates": list(dict.fromkeys(dates)),
        "year_months": list(dict.fromkeys(year_months)),
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
    volume = _number(_first(*volume_match.groups())) if volume_match else ""
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
    coverage_unit = ""
    if coverage_start and coverage_end:
        coverage_unit = (
            "volume"
            if volume_range_match
            else coverage_unit_before_number(unit_text, coverage_number_offset)
        )
    bare_number = ""
    if not any((volume, book, chapter, issue, coverage_start, coverage_end)):
        bare_values = []
        of_totals = []
        for match in re.finditer(r"(?<![A-Za-z0-9])0*(\d{1,4})(?![A-Za-z0-9])", unit_text):
            if COVER_COUNT_SUFFIX_RE.match(unit_text[match.end():]):
                continue
            number = _number(match.group(1))
            if not number or (number.isdigit() and 1900 <= int(number) <= 2099):
                continue
            if OF_TOTAL_PREFIX_RE.search(unit_text[:match.start()]):
                of_totals.append(number)
                continue
            bare_values.append(number)
        # A miniseries total is only a total when something else can be the
        # unit. "Book of 5" has no other number, so its 5 is the unit, and
        # discarding it would refuse the release for identifying no unit.
        if not bare_values and of_totals:
            bare_values = of_totals
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
        "coverage_unit": coverage_unit,
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


def target_names_a_parent_not_itself(wanted_item):
    """True when a row's title names the volume it lives in, not what it is.

    A MangaDex chapter routinely carries a title like `Vol. 46 Omake` while the
    row itself wants chapter 503.5. Read as text that is a volume target; read
    as a unit it is a chapter that happens to know its parent. The collection
    guard classifies on text, so it calls the second case a collection and then
    refuses the exact chapter the row asked for.

    The structured unit settles it. When the title names a volume and that
    volume number is not the number this row actually wants, the title is
    describing a container, not the target -- `Vol. 46 Omake` wanting 503.5.
    When they agree it is a real volume target and stays one: `Vol. 1` wanting
    1 is Dorohedoro volume 1, and a chapter does not satisfy it.

    This decides *which targets are collections*. It does not touch what
    satisfies one -- a part still never satisfies a collection.
    """
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    title = _first(wanted.get("issue_title"), wanted.get("issueTitle"))
    if not str(title or "").strip():
        return False
    title_volume = _number(parse_release_title(title).get("volume_number"))
    if not title_volume:
        return False
    target_number = _number(
        _first(
            wanted.get("chapter_number"),
            wanted.get("chapter"),
            wanted.get("issue_number"),
            wanted.get("normalized_number"),
            wanted.get("issue"),
        )
    )
    if not target_number:
        return False
    return str(title_volume) != str(target_number)


def collection_target_conflicts_with_candidate(candidate, wanted_item):
    """Would completion refuse this file for not satisfying a collection target?

    Measured 2026-08-19: the matcher returned `compatible` on files that the
    import-time guard then discarded after they had been fetched, previewed and
    written to staging. A `compatible` verdict that completion will certainly
    refuse is a claim about what happens next, and it was false -- every reader
    of that verdict inherited it.

    This asks the *same function* import asks
    (inkdrop_state.collection_target_single_part_block_reason), so the two can
    never drift into separate opinions. It cannot ask for more: at candidacy
    there is no archive, so the proofs that clear this guard on a real file are
    unavailable, and candidacy therefore sees strictly less evidence than
    import. Returns the guard's own reason string, or "".

    Fails open. If the import path cannot be reached, candidacy keeps its
    previous behaviour rather than inventing a refusal of its own.
    """
    if target_names_a_parent_not_itself(wanted_item):
        return ""
    try:
        from core import inkdrop_state
    except Exception:
        return ""
    guard = getattr(inkdrop_state, "collection_target_single_part_block_reason", None)
    record_of = getattr(inkdrop_state, "collection_guard_record_from_candidate", None)
    context_of = getattr(inkdrop_state, "collection_guard_queue_context", None)
    if not callable(guard) or not callable(record_of) or not callable(context_of):
        return ""
    try:
        return str(guard(context_of(wanted_item), record_of(candidate)) or "")
    except Exception:
        return ""


def target_context(wanted_item=None, *, settings):
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
    # An issue title carried by more than one issue of the series is an ARC
    # name, and an arc name is not a statement about any single unit. Wynd
    # issues 1-5 all read "Book One: The Flight of the Prince", so adopting its
    # book number made five different issue rows into volume targets and every
    # correctly-numbered single issue was refused wrong_unit_type. A real
    # collected-edition record gives each unit its own title, so requiring
    # uniqueness costs the volume arm nothing.
    #
    # The producer decides; an absent flag means "not computed" and leaves the
    # previous behaviour exactly as it was, so no caller is forced to supply it.
    if _strict_bool_flag(wanted.get("issue_title_shared_by_sibling_units")):
        title_evidence = {}
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
    if singleton_proof_source == "comicvine_authoritative_count_and_canonical_issue_identity":
        singleton_count_supported = metadata_issue_count == 1
    elif singleton_proof_source == UNITLESS_WORK_PROOF_SOURCE:
        # The producer already refused any declared count above one. A work
        # with no units may legitimately declare no count at all, so an
        # absent count is not a reason to withhold the proof here -- but a
        # declared count of exactly one still has to agree.
        singleton_count_supported = bool(
            wanted.get("unitless_work_proof") is True
            and metadata_issue_count in (0, 1)
        )
    else:
        singleton_count_supported = bool(
            metadata_issue_count == 0
            and singleton_proof_source == "comicvine_collected_single_wanted_identity_without_declared_count"
        )
    singleton_count_supported = bool(singleton_count_supported)
    singleton_issue_proof = bool(
        wanted.get("singleton_issue_proof")
        and singleton_proof_source in TRUSTED_SINGLETON_PROOF_SOURCES
        and wanted.get("singleton_metadata_trusted") is True
        and wanted.get("singleton_metadata_fresh") is True
        and wanted.get("singleton_issue_metadata_trusted") is True
        and canonical_issue_count == 1
        and singleton_count_supported
        and issue == "1"
        # Deliberately no media test. The producer --
        # _singleton_issue_context_from_rows() in the source worker
        # coordinator -- grants this proof from durable identity alone:
        # ComicVine identity matching the series id, fresh metadata, one
        # canonical issue row carrying one positive ComicVine issue id, a
        # declared count of one, and no other issue in the series. Every other
        # conjunct here mirrors one of those terms; the media clause mirrored
        # nothing, so a ComicVine-backed manga one-shot earned the proof and
        # could not spend it -- 4 live rows, all manga, parked at
        # missing_required_unit_number asking a human for a unit number that
        # cannot exist. ("graphic novel" never appeared in the series table at
        # all, so that half of the disjunct never fired.) What keeps a
        # numbered manga run out is the count evidence above, not the shelf
        # the work sits on. The identical clause on collected_singleton_proof
        # below stays -- that one does mirror its producer, which asks for
        # "comic" in the series media type itself.
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
        # Resolved, never read raw. The predecessor of this line asked the
        # wanted row for `allow_collected_edition`, a key no producer ever
        # wrote, so it was always falsy and the edition gate was permanently
        # on -- 127 items refused in silence. The resolver always returns a
        # complete policy, so "nobody set it" can no longer mean "refuse".
        "acquisition_policy": inkdrop_acquisition_policy.resolve(wanted, settings=settings),
        "unit_type_explicit": bool(explicit_unit_type),
        "media_type": media_type,
        "canonical_issue_count": canonical_issue_count,
        "metadata_issue_count": metadata_issue_count,
        "singleton_issue_proof": singleton_issue_proof,
        "collected_singleton_proof": collected_singleton_proof,
        "collected_singleton_markers": collected_singleton_markers,
        "edition_indifferent": _strict_bool_flag(wanted.get("edition_indifferent")),
    }


def _singleton_exact_title_match(
    candidate, wanted_item, target, evidence, *, allow_bare_number="", allow_own_volume_number=""
):
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
    # A work whose whole run is one unit names that unit both ways in the wild:
    # `Title 001` and `Title v01` are the same object, and only the second was
    # refused. This is NOT the collected-trade case -- the caller only passes
    # allow_own_volume_number for a row with a trusted single-issue proof, so a
    # series whose volume 1 collects issues 1-6 never reaches it. The volume the
    # file names still has to be the work's own single unit; v02 of a
    # one-volume work is not that work, and every other disqualifier below
    # (range, pack, ambiguity, a competing issue number) still applies.
    own_volume_match = bool(
        allow_own_volume_number
        and not evidence.get("issue_number")
        and (
            evidence.get("volume_number") == allow_own_volume_number
            or evidence.get("book_number") == allow_own_volume_number
        )
        and evidence.get("volume_number") in ("", None, allow_own_volume_number)
        and evidence.get("book_number") in ("", None, allow_own_volume_number)
    )
    if any(
        (
            evidence.get("issue_number") and not bare_number_match,
            evidence.get("chapter_number"),
            evidence.get("volume_number") and not own_volume_match,
            evidence.get("book_number") and not own_volume_match,
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
            and (
                key not in ("volume_number", "book_number")
                or not (own_volume_match and source.get(key) == allow_own_volume_number)
            )
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
        recredited = release_tokens_after_creator_credit(release_title, target_tokens)
        if recredited is None or recredited[: len(target_tokens)] != target_tokens:
            return False
        release_tokens = recredited
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
    if own_volume_match:
        # The volume token itself, in the forms a filename writes it: the
        # evidence parser has already confirmed it names this work's own single
        # unit, so it is not an unexplained leftover token.
        allowed_suffix.update({
            "v", "vol", "volume", "book",
            allow_own_volume_number,
            allow_own_volume_number.zfill(2),
            allow_own_volume_number.zfill(3),
            f"v{allow_own_volume_number}",
            f"v{allow_own_volume_number.zfill(2)}",
        })
    if target_year:
        allowed_suffix.add(target_year)
    allowed_suffix.update(target_publisher.split())
    suffix_tokens = release_tokens[len(target_tokens) :]
    if all(token in allowed_suffix for token in suffix_tokens):
        return True
    # Every identity check above has already passed: the leading tokens are
    # this work's title exactly, no source carries a unit number, no edition
    # marker is present, and year and publisher agree. What is left can only
    # be a scanner credit -- and it is judged by the same predicate the
    # collected sibling uses, not a second copy of the rule.
    return release_group_suffix_only(suffix_tokens, target_year)


def _collected_singleton_exact_title_match(candidate, wanted_item, target, evidence):
    if not target.get("collected_singleton_proof"):
        return False
    # An explicit per-series "Edition Indifferent" override: the operator has
    # said any complete, correctly-identified release satisfies this series,
    # not only the specific tracked printing. Below, this skips the marker
    # (which printing) and year (which publication) checks that otherwise
    # require the candidate to echo the tracked edition -- every other check
    # here (title identity, unit-number absence, publisher) still applies.
    edition_indifferent = _strict_bool_flag(target.get("edition_indifferent"))
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
    if not edition_indifferent and (not all_candidate_markers or not all_candidate_markers.issubset(target_markers)):
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
        recredited = release_tokens_after_creator_credit(release_title, target_tokens)
        if recredited is None or recredited[: len(target_tokens)] != target_tokens:
            return False
        release_tokens = recredited
    target_year = _year(_first(wanted.get("year"), wanted.get("release_date"), wanted.get("date")))
    candidate_year = _year(_first(candidate.get("year"), candidate.get("release_date"), evidence.get("year")))
    if target_year and candidate_year and target_year != candidate_year and not edition_indifferent:
        return False
    source_years = {str(source.get("year") or "") for source in parsed_sources if source.get("year")}
    if target_year and any(year != target_year for year in source_years) and not edition_indifferent:
        return False
    suffix_tokens = release_tokens[len(target_tokens) :]
    allowed_suffix = set(SINGLETON_NEUTRAL_SUFFIX_TOKENS)
    allowed_suffix.update(EDITION_FORMAT_SUFFIX_WORDS)
    if target_year:
        allowed_suffix.add(target_year)
    if edition_indifferent:
        allowed_suffix.update(source_years)
        if candidate_year:
            allowed_suffix.add(candidate_year)
    if all(token in allowed_suffix for token in suffix_tokens):
        return True
    return release_group_suffix_only(suffix_tokens, target_year)


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
    edition_indifferent = _strict_bool_flag(target.get("edition_indifferent"))
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
    if candidate_markers and not candidate_markers.issubset(target_markers) and not edition_indifferent:
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
    if target_year and candidate_year and target_year != candidate_year and not edition_indifferent:
        return False
    source_years = {str(source.get("year") or "") for source in parsed_sources if source.get("year")}
    if target_year and any(year != target_year for year in source_years) and not edition_indifferent:
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
    if edition_indifferent:
        allowed_suffix.update(EDITION_FORMAT_SUFFIX_WORDS)
        allowed_suffix.update(source_years)
        if candidate_year:
            allowed_suffix.add(candidate_year)
    return all(token in allowed_suffix for token in suffix)


# ---------------------------------------------------------------------------
# Numbers in a filename that are not this artifact's unit.
#
# A release name often carries several numbers, and only some of them are a
# claim about *this file's* unit. The rest belong to something else -- the
# miniseries it is part of, its position in someone's reading order, the
# volume slot its parent series occupies. Reading one of those as the unit
# produces a confident, entirely wrong verdict: a wrong-issue refusal, or a
# wrong-unit-type refusal, against a file that is exactly what was asked for.
#
# Confirmed shapes, each measured on a real refusal:
#
#   miniseries total       "Klan 01 (of 03)"                 -> the 3 is a total
#   reading-order position "08. Nemo - Roses of Berlin"      -> the 8 is a slot
#   series position        "(Avatar - The Last Airbender V11)" -> the 11 is the
#                                                              parent series'
#
# They are one category and belong together. When a fourth shape turns up --
# and it will -- add it here rather than starting a fourth patch somewhere
# else. Each rule must stay narrow enough to name in one line, and each must
# leave genuine unit claims alone: "Berserk v11", "Akira Volume 3" and
# "Monster v09" are this artifact's unit and must keep parsing that way.
# ---------------------------------------------------------------------------

# A trailing bracketed group that names the wanted series and then a unit
# token. The number belongs to the series, not to the file: "Ashes of the
# Academy (2025) (Avatar - The Last Airbender V11)" is volume 11 OF the
# franchise and is itself a titled one-shot with no unit of its own. Requires
# the series words to be present, so a bare "(v11)" -- which is a real unit
# claim -- is untouched.
_SERIES_POSITION_GROUP_RE = re.compile(
    r"[\(\[]\s*(?P<body>[^()\[\]]*?)\s*[\)\]]"
)
_SERIES_POSITION_UNIT_TAIL_RE = re.compile(
    rf"(?i)(?:{'|'.join(VOLUME_MARKER_WORDS + BOOK_MARKER_WORDS)}|v)\s*\.?\s*0*\d{{1,4}}\s*$"
)


def _series_token_sequence(wanted_item=None):
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    series = str(_first(wanted.get("series_title"), wanted.get("series"), wanted.get("manga_title"))).strip()
    return tuple(re.findall(r"[a-z0-9]+", series.lower()))


def _names_the_wanted_series(text, series_tokens):
    """True when these words are a run of the wanted series' own title.

    An ordered contiguous run rather than a loose overlap, so a group that
    merely shares a word with the series does not qualify.
    """
    words = tuple(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    if len(words) < 2 or not series_tokens:
        return False
    for start in range(0, len(series_tokens) - len(words) + 1):
        if series_tokens[start:start + len(words)] == words:
            return True
    return False


def _group_restates_the_wanted_unit(body, wanted_item):
    """Does this group's unit token equal the unit we are already looking for?

    The parent-slot case and the self-describing case are otherwise identical:
    both name a contiguous run of the wanted series title, both end in a unit
    token, and both can be the only unit in the name. `Ashes of the Academy
    (2025) (Avatar - The Last Airbender V11).cbz` is a titled one-shot whose
    V11 is the FRANCHISE's slot; `The Tea Dragon Festival (2019) (Tea Dragon
    V02).cbz` is book two of three and its V02 is the file's own unit. Whether
    anything else in the name carries a unit does not separate them -- neither
    does.

    What separates them is agreement with the target. A group restating the
    unit we already want cannot cause a wrong match: the only verdict it can
    turn into is an exact match on the very value being sought. A group naming
    some other number is a foreign slot and is dropped exactly as before.
    """
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    if not wanted:
        return False
    try:
        target = target_context(wanted, settings=None)
    except Exception:
        return False
    claimed = parse_release_title(str(body or ""))
    for key in ("volume_number", "issue_number", "chapter_number"):
        value = str(claimed.get(key) or "").strip()
        if not value:
            continue
        if value == str(target.get(key) or "").strip():
            return True
    return False


def strip_series_position_group(value, wanted_item=None):
    """Drop a bracketed group that states the parent series' volume slot.

    Narrow on purpose: the group must name the wanted series *and* end in a
    unit token, and only that group is removed. Everything else about the
    release name, including any unit the artifact claims for itself, survives.
    """
    text = str(value or "").strip()
    series_tokens = _series_token_sequence(wanted_item)
    if not text or not series_tokens:
        return text
    out = text
    for match in list(_SERIES_POSITION_GROUP_RE.finditer(text)):
        body = match.group("body")
        tail = _SERIES_POSITION_UNIT_TAIL_RE.search(body)
        if not tail:
            continue
        if not _names_the_wanted_series(body[: tail.start()], series_tokens):
            continue
        if _group_restates_the_wanted_unit(body, wanted_item):
            continue
        out = out.replace(match.group(0), " ", 1)
    return re.sub(r"\s{2,}", " ", out).strip() or text


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
        # Some providers put the full remote path in ``title`` or
        # ``filename``, not only in ``path``.  Unit identity belongs to the
        # artifact leaf: an uploader shelf is provenance, not release
        # metadata.  Take the leaf before stripping the exact wanted title so
        # a series ending in a unit-like token (real example: ``ODY-C 011``)
        # cannot leave ``C 011`` behind to be read as chapter 11.  Keep
        # ``text`` unchanged in the tuple so normalized evidence still
        # records the provider's original value.
        normalized_path_text = text.replace("\\", "/")
        drive_relative = re.match(r"^[A-Za-z]:([^/]+)$", normalized_path_text)
        leaf_text = (
            drive_relative.group(1)
            if drive_relative
            else PurePosixPath(normalized_path_text).name
        )
        original_has_target_prefix = _strip_series_prefix(text, wanted_item) != text
        leaf_has_target_prefix = (
            leaf_text != text
            and _strip_series_prefix(leaf_text, wanted_item) != leaf_text
        )
        wanted = wanted_item if isinstance(wanted_item, dict) else {}
        wanted_series = str(
            _first(
                wanted.get("series_title"),
                wanted.get("series"),
                wanted.get("manga_title"),
            )
        ).strip()
        wanted_title_ends_in_c = bool(
            re.search(r"(?i)(?:^|[\W_])c\s*$", wanted_series)
        )
        path_shaped = (
            "\\" in text
            or normalized_path_text.startswith("/")
            or bool(re.match(r"^[A-Za-z]:", normalized_path_text))
            or normalized_path_text.count("/") >= 2
            or leaf_has_target_prefix
        )
        # A single slash can be part of the actual series name (for example,
        # Batman/Superman), so only project to a leaf when there is additional
        # path evidence.  An exact wanted-title prefix on the leaf safely
        # covers a one-folder provider path without weakening slash titles.
        # Limit title/filename projection to the demonstrated terminal-C
        # collision: stripping other titles early can change `v1_019` from an
        # exact issue into apparent range coverage.  Source paths keep their
        # pre-existing unconditional leaf semantics.
        identity_text = (
            leaf_text
            if label == "source_path"
            or (
                path_shaped
                and leaf_has_target_prefix
                and not original_has_target_prefix
                and wanted_title_ends_in_c
            )
            else text
        )
        if text and not any(existing[1] == text for existing in values):
            # The series-position group goes before the prefix strip: it is a
            # statement about the parent series, so it must not survive into
            # the text the unit parser reads.
            identity_text = strip_series_position_group(identity_text, wanted_item)
            values.append((label, text, _strip_series_prefix(identity_text, wanted_item)))
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


def _reinterpret_vyear_run_for_issue_target(candidate, target, evidence):
    """Treat ``VYYYY N`` as run-year plus issue N under narrow target proof."""

    wanted = str(target.get("issue_number") or "")
    if target.get("unit_type") not in ISSUE_UNITS or not wanted:
        return
    if any(
        candidate.get(key) not in (None, "")
        for key in ("source_volume_number", "provider_volume_number", "volume")
    ):
        return
    run_year = str(evidence.get("volume_number") or "")
    if not (run_year.isdigit() and 1900 <= int(run_year) <= 2099):
        return
    if any(
        evidence.get(key)
        for key in ("book_number", "chapter_number", "coverage_start", "coverage_end")
    ):
        return

    sources = evidence.get("sources") if isinstance(evidence.get("sources"), list) else []
    volume_sources = [source for source in sources if source.get("volume_number")]
    if not volume_sources:
        return
    separate_numbers = set()
    numbers_by_source = []
    for source in sources:
        text = str(source.get("parsed_identity_text") or source.get("unit_identity_title") or "")
        # The leaf only, as parse_release_title reads unit identity: a parent
        # folder's number ("... - Year 2 (2014)") is the uploader's shelf
        # layout, and counting it here made the run-year read ambiguous.
        text = re.split(r"[\\/]", text)[-1].strip() or text
        year_matches = list(V_YEAR_RE.finditer(text))
        if len(year_matches) > 1:
            return
        if source.get("volume_number"):
            if (
                source.get("volume_number") != run_year
                or len(year_matches) != 1
                or year_matches[0].group(1) != run_year
            ):
                return
        masked = publication_date_evidence(text).get("masked_text", text)
        masked = V_YEAR_RE.sub(lambda match: " " * (match.end() - match.start()), masked)
        if VOLUME_RE.search(masked) or BOOK_RE.search(masked) or CHAPTER_RE.search(masked):
            return
        if COVERAGE_RE.search(masked) or COMPACT_VOLUME_RANGE_RE.search(masked):
            return
        source_numbers = []
        for match in re.finditer(r"(?<![A-Za-z0-9])0*(\d{1,4})(?![A-Za-z0-9])", masked):
            number = _number(match.group(1))
            if number and not (number.isdigit() and 1900 <= int(number) <= 2099):
                source_numbers.append(number)
                separate_numbers.add(number)
        # Each asserted source must carry at most one unit token. A set alone
        # would collapse ``V1992 277 277`` into false certainty.
        if len(source_numbers) > 1:
            return
        numbers_by_source.append((source, source_numbers))
    # More than one trailing number is ambiguous (for example ``V1992 277
    # 278``); never pick one number out of that larger claim.
    #
    # Which number it is, is deliberately not compared against the wanted
    # issue. Whether ``V1992 278`` reads as "run year 1992, issue 278" is a
    # question about the title's own shape, and the answer cannot depend on
    # what happens to be wanted -- reading it as volume 1992 when the issue is
    # wrong and as issue 278 when it is right made the same release report
    # wrong_unit_type in one case and wrong_issue_number in the other. The
    # guards above are what make the read safe; the wanted number never was.
    if len(separate_numbers) != 1:
        return
    unit_number = next(iter(separate_numbers))
    # A source carrying VYYYY must carry exactly that one separate issue.
    for source, source_numbers in numbers_by_source:
        if source.get("volume_number") and source_numbers != [unit_number]:
            return
    if evidence.get("issue_number") and evidence.get("issue_number") != unit_number:
        return

    evidence["run_year"] = run_year
    evidence["volume_number"] = ""
    evidence["issue_number"] = unit_number
    evidence["unit_type"] = "issue"
    evidence["present_unit_fields"] = ["issue_number"]
    evidence["ambiguous"] = False
    evidence["conflicts"] = [
        field for field in (evidence.get("conflicts") or []) if field not in {"volume_number", "issue_number"}
    ]
    for source in sources:
        if source.get("volume_number") == run_year:
            source["run_year"] = run_year
            source["volume_number"] = ""
            source["issue_number"] = unit_number
            source["unit_type"] = "issue"
            source["ambiguous"] = False


def _relaunch_run_year_conflict(target, wanted_item, evidence, identity_values):
    """Return whether a same-titled later run is being offered for run one's first issue.

    A relaunch keeps the title and restarts the numbering, so the wanted issue
    number matches exactly and every title check passes -- "Uncanny X-Men 001
    (2018)" reads as a clean hit for Uncanny X-Men (1963) #1. The year is the
    only thing that separates them, and it can only be compared soundly at
    issue one: a run that began in 1963 published its first issue in 1963, so
    a first issue stamped decades later belongs to a different run. For issue
    N the wanted year pins nothing (issue 200 of a 1963 run ships in 1985),
    and the year is not compared at all.

    Deliberately one-directional and comic-only. An *earlier* year is left
    alone because the common "Series (RunYear) NNN" naming puts the run's year
    on every issue, so an earlier year is usually the right run rather than a
    wrong one. Manga is excluded because its years are not the same quantity
    on both sides -- a series' year is Japanese serialization while a release
    carries the English edition -- which is the same mismatch that made the
    library's folder-year guard blind whole shelves.
    """

    if target.get("media_type") not in COMIC_MEDIA_TYPES:
        return False
    if target.get("unit_type") not in ISSUE_UNITS or target.get("issue_number") != "1":
        return False
    wanted = wanted_item if isinstance(wanted_item, dict) else {}
    wanted_year = _year(_first(wanted.get("year"), wanted.get("release_date"), wanted.get("date")))
    if not wanted_year:
        return False
    # A collected edition or a reprint carries the year of *that* edition, not
    # of the run -- "Batman Year One Deluxe Edition 2007" is the 1987 first
    # issue in a 2007 hardcover. Whether such a release may satisfy the target
    # is the collected-edition gate's decision, made below on real edition
    # evidence; the year says nothing about which run this is.
    if evidence.get("edition_markers") or evidence.get("edition_marker"):
        return False
    identity_text = " ".join(str(value or "") for value in identity_values if value)
    if REPRINT_MARKER_RE.search(identity_text):
        return False
    declared_years = set(YEAR_RE.findall(identity_text))
    # One year is a claim; two are a description ("001 (1963) (2018 digital
    # scan)") and which one names the run is no longer decidable here.
    if len(declared_years) != 1:
        return False
    declared_year = next(iter(declared_years))
    # One year of slack: cover dates routinely run ahead of the recorded
    # start year, so a 1963 run's first issue may be stamped 1964.
    return int(declared_year) > int(wanted_year) + 1


def collected_singleton_alias_exact_title_match(candidate, wanted_item=None, *, settings=None):
    """Return whether a raw result is an exact alias for a proven collected singleton."""

    normalized = normalize_candidate(candidate, wanted_item)
    target = target_context(wanted_item, settings=settings)
    return _collected_singleton_alias_exact_title_match(
        normalized,
        wanted_item,
        target,
        normalized["source_unit_evidence"],
    )


# A pack that names its own range is only trusted inside these bounds. An
# unlabeled range has told us less, so it gets the tighter pair: a run of more
# than 400 units, or an end above 1500, is far more likely to be shelf noise
# ("AA-LL 11-2025") than a real collected run. A labeled range ("chapters
# 1-1100") has said what it counts, so it is allowed to be genuinely long.
PACK_RANGE_BOUNDS = {
    "labeled": {"max_span": 1200, "max_number": 3000},
    "unlabeled": {"max_span": 400, "max_number": 1500},
}
PACK_RANGE_UNIT_MARKER_FIELDS = (
    "volume_number",
    "book_number",
    "chapter_number",
    "issue_number",
)


def _candidate_declares_pack_contents(candidate):
    """True when the release told us what is actually inside it."""
    if not isinstance(candidate, dict):
        return False
    sources = [candidate]
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    if isinstance(raw.get("result"), dict):
        sources.append(raw["result"])
    for source in sources:
        for key in ("files", "pack_detail_entries"):
            value = source.get(key)
            if isinstance(value, (list, tuple, set)):
                if any(str(item or "").strip() for item in value):
                    return True
            elif str(value or "").strip():
                return True
    return False


def _range_number(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pack_title_range_membership(candidate, target, evidence):
    """Prove a pack collects the wanted unit from the range in its own title.

    A range-titled pack is the only way a lot of back catalogue is published --
    no single-issue release of "Injustice: Gods Among Us Year Five" #4 exists
    anywhere, but "(001-040)" does -- and the range is parsed already. What was
    missing is permission to read it as membership evidence, so every one of
    those packs was rejected with coverage_not_unit_number no matter how
    plainly its title said it contained the wanted number.

    The proof is deliberately narrow. The range must name the same unit the
    target counts in, because a volume batch does not satisfy a wanted chapter
    even when the digits overlap; an unlabeled range is read only for issue
    targets, and only when the title makes no other unit claim that the range
    could be contradicting.

    This answers "grabbing this would get me the wanted unit", which is only a
    question the acquisition side asks. The same verdict function also decides
    whether a physical file on disk *is* the wanted unit, and there the answer
    for a container is still no -- ``All Star Superman 001-012.zip`` is not
    issue #7's artifact. The provider layer's ``pack`` flag is what separates
    the two, so a candidate that was never marked as a pack acquisition
    candidate gets no proof here.
    """
    if not isinstance(target, dict) or not isinstance(evidence, dict):
        return None
    if not (isinstance(candidate, dict) and candidate.get("pack")):
        return None
    # A range is a fallback for releases that never say what is inside them.
    # When a file list did arrive, that list is the better evidence in both
    # directions: a match is already handled as the stronger manifest proof,
    # and a non-match means the contents were read and the wanted unit is not
    # among them. Letting the title argue with the file list is how a pack
    # that says "001-010" and holds another series entirely gets grabbed.
    if _candidate_declares_pack_contents(candidate):
        return None
    # The wanted work is a single unit in its own right, so a numbered run of
    # several units is describing some other publication, not collecting this
    # one. Believing otherwise turns "Batman Year One 001-004" into a match
    # for the one-issue work InkDrop actually tracks.
    if target.get("singleton_issue_proof") or _range_number(target.get("canonical_issue_count")) == 1:
        return None
    # Coverage only means something once the release is agreed to be this
    # series. A range inside a related or wrong-series pack proves nothing.
    match_confidence = str(candidate.get("match_confidence") or "").strip().lower().replace("-", "_")
    if match_confidence == "mismatch" or match_confidence.startswith("related_series") or match_confidence in {
        "subseries",
        "related_title",
    }:
        return None
    start_text = str(evidence.get("coverage_start") or "")
    end_text = str(evidence.get("coverage_end") or "")
    if not start_text.isdigit() or not end_text.isdigit():
        return None
    start = int(start_text)
    end = int(end_text)
    if start < 1 or end <= start:
        return None
    if evidence.get("preview_or_sample") or evidence.get("ambiguous") or evidence.get("conflicts"):
        return None

    coverage_unit = str(evidence.get("coverage_unit") or "")
    target_unit = str(target.get("unit_type") or "")
    if target_unit in VOLUME_UNITS:
        wanted_unit, wanted_number = "volume", target.get("volume_number")
    elif target_unit in CHAPTER_UNITS:
        wanted_unit, wanted_number = "chapter", target.get("chapter_number")
    elif target_unit in ISSUE_UNITS:
        wanted_unit, wanted_number = "issue", target.get("issue_number")
    else:
        return None

    if coverage_unit:
        if coverage_unit != wanted_unit:
            return None
    else:
        # Nothing labeled this range. Volumes and chapters share the same small
        # digits far too often for that to be safe -- "Naruto 1-72" is volumes,
        # "Vagabond 304-308" is chapters -- so an unlabeled range is read only
        # for issue targets, and only when the title asserts no competing unit
        # number the range would be talking over.
        if wanted_unit != "issue":
            return None
        if any(evidence.get(field) for field in PACK_RANGE_UNIT_MARKER_FIELDS):
            return None
        if 1900 <= end <= 2099:
            return None

    bounds = PACK_RANGE_BOUNDS["labeled" if coverage_unit else "unlabeled"]
    if end > bounds["max_number"] or (end - start + 1) > bounds["max_span"]:
        return None

    number = _range_number(wanted_number)
    if number is None or not (start <= number <= end):
        return None
    return {
        "coverage_source": "pack_title_range",
        "coverage_unit": coverage_unit or wanted_unit,
        "coverage_unit_declared": bool(coverage_unit),
        "coverage_start": str(start),
        "coverage_end": str(end),
        "unit_number": str(wanted_number),
        "entry": str(evidence.get("unit_identity_title") or evidence.get("original_title") or ""),
    }


def pack_title_range_membership(candidate, wanted_item=None, *, settings=None):
    """Public form of the range proof, for callers outside this module.

    ``wanted_item`` is optional because indexer candidates already carry the
    wanted unit's own numbers -- the same convention
    ``indexer_manifest_entry_matches_candidate`` reads them under.
    """
    normalized = normalize_candidate(candidate, wanted_item)
    target = target_context(wanted_item if wanted_item is not None else normalized, settings=settings)
    return _pack_title_range_membership(normalized, target, normalized["source_unit_evidence"])


def candidate_compatibility(candidate, wanted_item=None, settings=None):
    candidate = normalize_candidate(candidate, wanted_item)
    target = target_context(wanted_item, settings=settings)
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
    _reinterpret_vyear_run_for_issue_target(candidate, target, evidence)
    blocked = []
    review = []
    positive = []
    manifest_match = candidate.get("pack_contents_match") if isinstance(candidate.get("pack_contents_match"), dict) else {}
    manifest_exact_member = manifest_match.get("coverage_source") in {
        "pack_contents_filename",
        "pack_contents_volume_filename",
    }
    # A real per-file manifest is the stronger proof and names the exact
    # member, so it keeps its own evidence code; the range is the fallback for
    # the many indexers that ship no file list at all.
    range_exact_member = not manifest_exact_member and bool(
        _pack_title_range_membership(candidate, target, evidence)
    )
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
    # A single-issue work's file that names its one unit as a volume rather than
    # an issue. Gated on the same trusted proof singleton_exact_match needs --
    # _singleton_exact_title_match returns False without it -- so a multi-issue
    # series' collected volume 1 never reaches this.
    singleton_own_volume_match = bool(
        target.get("unit_type") in ISSUE_UNITS
        and target.get("singleton_issue_proof")
        and target.get("issue_number")
        # Only the new case: the file names a volume/book and no issue. Without
        # this, allow_own_volume_number is inert on a file that carries no unit
        # token at all and the branch below would shadow singleton_exact_title,
        # renaming the evidence every existing unitless-work caller reads.
        and not evidence.get("issue_number")
        and (evidence.get("volume_number") or evidence.get("book_number"))
        and _singleton_exact_title_match(
            candidate,
            wanted_item,
            target,
            evidence,
            allow_own_volume_number=target.get("issue_number"),
        )
    )

    match_confidence = str(candidate.get("match_confidence") or "").strip().lower().replace("-", "_")
    # "Absent" and "present and False" mean opposite things here. Only the
    # provider path stamps this, so a candidate that never went through it
    # keeps the old behaviour rather than being handed an outer-work match it
    # never earned.
    outer_work_match = candidate.get("outer_work_identity_match")
    if (
        match_confidence == "mismatch"
        and not collected_singleton_alias_exact_match
        and not singleton_exact_match
    ):
        # Outer-work identity alone is not enough to drop the title reason.
        # It is computed from a wide alias set that includes derived
        # collected/contributor forms, so "Batman - The Court of Owls 001"
        # satisfies it for a wanted "Absolute Batman: The Court of Owls" --
        # a different work whose only distinguishing token is the one the
        # candidate is missing. Dropping the title reason there made a
        # wrong-work release compatible. So the canonical title must be
        # wholly present as well: every token of it, in the observed title.
        canonical_tokens = inkdrop_title_identity.normalized_tokens(
            _first(
                (wanted_item or {}).get("series_title"),
                (wanted_item or {}).get("series"),
                (wanted_item or {}).get("manga_title"),
            )
            if isinstance(wanted_item, dict)
            else ""
        )
        observed_tokens = set(
            inkdrop_title_identity.normalized_tokens(
                _first(candidate.get("original_result_title"), candidate.get("title"))
            )
        )
        canonical_fully_present = bool(canonical_tokens) and all(
            token in observed_tokens for token in canonical_tokens
        )
        if outer_work_match is True and canonical_fully_present:
            # The provider's own outer-work authority says this release names
            # the wanted work; what it has not shown is that it holds the
            # wanted unit. Measured 2026-08-18, 832 of 934 title-mismatch rows
            # were in exactly that position, and 210 of the 213 where title
            # was the *only* stated reason. Reporting those as a title
            # mismatch sent every reader to title matching when the open
            # question was containment. Settled after the unit gates below,
            # where the precise reason is known.
            pass
        else:
            blocked.append("candidate_title_mismatch")
    elif (
        match_confidence.startswith("related_series")
        or match_confidence in {"subseries", "related_title"}
    ) and not singleton_exact_match:
        blocked.append("related_series_identity")
    if not singleton_exact_match and _relaunch_run_year_conflict(
        target, wanted_item, evidence, source_identity_values
    ):
        # Same carve-out the confidence-driven block above uses: a proven
        # singleton whose title matched exactly is not second-guessed here.
        blocked.append("related_series_identity")

    wanted_creators = _trusted_wanted_creators(wanted_item)
    candidate_creators = _candidate_asserted_creators(candidate)
    if wanted_creators and candidate_creators and not set(wanted_creators).intersection(candidate_creators):
        blocked.append("creator_identity_conflict")

    if candidate.get("preview_or_sample"):
        blocked.append("preview_or_sample")
    # Asked here so acceptance and completion agree before a transfer runs,
    # not after. Review rather than block: candidacy cannot read the archive
    # the import-time guard reads, so it must not turn "I cannot prove this
    # satisfies the collection" into a refusal the file itself could clear.
    if collection_target_conflicts_with_candidate(candidate, wanted_item):
        review.append("collection_target_single_part")
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
    # Authoritative print-run identity for the wanted issue (which real-world
    # numbered volume/book it belongs to, e.g. Love and Rockets v1 #19 vs the
    # unrelated v2 #19) versus whatever run number the candidate itself
    # asserts. Computed once and reused below: a print-run marker alongside
    # the exact wanted issue number is only "one claim, not two conflicting
    # ones" when that marker actually names the wanted run.
    target_run_number = target.get("volume_number")
    evidence_run_number = evidence.get("volume_number") or evidence.get("book_number")
    # A print-run marker ("v1 #19") alongside the exact wanted issue number is
    # not two conflicting unit claims, it's one -- the volume digit names
    # which real-world numbered run the issue belongs to -- but only when
    # that digit actually is the wanted run. "v2 #19" restates the issue
    # number and asserts a different, wrong run in the same breath; it is
    # not hierarchical evidence just because the issue number also matches.
    hierarchical_issue_identity = bool(
        target.get("unit_type") in ISSUE_UNITS
        and evidence.get("issue_number")
        and evidence.get("issue_number") == target.get("issue_number")
        and set(evidence.get("present_unit_fields") or ()) <= {"volume_number", "book_number", "issue_number"}
        and not evidence.get("conflicts")
        and not evidence.get("chapter_number")
        and not evidence.get("coverage_start")
        and not evidence.get("coverage_end")
        and (not evidence_run_number or not target_run_number or evidence_run_number == target_run_number)
    )
    # Two different findings share this one code. Sources that contradict each
    # other ("Saga 003" in the title, issue 4 from the provider) leave the
    # unit genuinely unknowable, and this code is the precise answer. A single
    # source naming two unit types at once ("Love and Rockets v2 #019") is
    # readable -- the unit checks below say exactly which claim is wrong -- so
    # there this code is only a placeholder for them.
    source_identities_conflict = bool(evidence.get("conflicts"))
    if source_identities_conflict or (
        evidence.get("ambiguous") and not hierarchical_chapter_identity and not hierarchical_issue_identity
    ):
        blocked.append("ambiguous_unit_identity")

    edition = evidence.get("edition_marker") or ""
    target_unit = target.get("unit_type") or ""
    if edition in COLLECTED_MARKERS and target_unit in (VOLUME_UNITS | ISSUE_UNITS | CHAPTER_UNITS):
        if not collected_singleton_match and not singleton_exact_match:
            # Severity, not a boolean. The old gate could only block or stay
            # silent, so "show this to an operator" was unreachable and a
            # collected edition holding the wanted unit simply vanished.
            severity = inkdrop_acquisition_policy.severity_for(
                target["acquisition_policy"], "collected_edition"
            )
            # A collected edition is two questions, not one: "is this edition
            # acceptable" and "does it actually contain the wanted unit". The
            # edition policy answers the first. The second is answered by
            # proof, never by the title: a manifest naming the unit, a
            # declared range holding it, or -- for an issue or chapter target
            # -- the release naming that exact number. A volume target gets no
            # number proof: an omnibus or deluxe "Vol 3" collects several
            # regular volumes and its 3 is on a different scale from the
            # wanted run's 3. Proven containment composes with the
            # pack-containment policy (tracker #209, decided 2026-09-16:
            # admit); unproven contents compose with unidentified_unit, so a
            # release that only claims to hold the unit is shown for review
            # and never admitted on that claim.
            spans_a_range = bool(
                evidence.get("pack_marker")
                or evidence.get("coverage_start")
                or evidence.get("coverage_end")
            )
            exact_unit_named = bool(
                (
                    target_unit in ISSUE_UNITS
                    and target.get("issue_number")
                    and evidence.get("issue_number") == target.get("issue_number")
                    and not spans_a_range
                )
                or (
                    target_unit in CHAPTER_UNITS
                    and target.get("chapter_number")
                    and evidence.get("chapter_number") == target.get("chapter_number")
                    and not spans_a_range
                )
            )
            containment_proven = bool(manifest_exact_member or range_exact_member or exact_unit_named)
            if containment_proven:
                severity = inkdrop_acquisition_policy.stricter(
                    severity,
                    inkdrop_acquisition_policy.severity_for(target["acquisition_policy"], "pack_containment"),
                )
            elif not _strict_bool_flag(target.get("edition_indifferent")):
                # A row the operator marked edition-indifferent has already
                # answered "is this edition fine for this row"; it is not
                # asked again here. Every other row composes an unproven
                # edition with the unidentified-unit answer.
                severity = inkdrop_acquisition_policy.stricter(
                    severity,
                    inkdrop_acquisition_policy.severity_for(target["acquisition_policy"], "unidentified_unit"),
                )
            if severity == inkdrop_acquisition_policy.REFUSE:
                blocked.append("collected_edition_disallowed")
            elif severity == inkdrop_acquisition_policy.REVIEW:
                review.append("collected_edition_disallowed")

    if target_unit in VOLUME_UNITS:
        wanted = target.get("volume_number")
        found = evidence.get("volume_number") or evidence.get("book_number")
        if (
            (evidence.get("coverage_start") or evidence.get("coverage_end"))
            and not manifest_exact_member
            and not range_exact_member
        ):
            blocked.append("coverage_not_unit_number")
        if range_exact_member:
            # The range is the unit evidence here, so its own start number is
            # not a competing volume claim to be rejected below.
            positive.append("exact_pack_range_member")
        elif (
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
        if range_exact_member:
            positive.append("exact_pack_range_member")
        elif (evidence.get("coverage_start") or evidence.get("coverage_end")) and not manifest_exact_member:
            blocked.append("coverage_not_unit_number")
        elif collected_singleton_alias_volume_match:
            positive.append("collected_singleton_alias_volume")
        elif evidence.get("chapter_number"):
            blocked.append("wrong_unit_type")
        elif evidence.get("issue_number") and wanted and evidence.get("issue_number") != wanted:
            blocked.append("wrong_issue_number")
        elif (
            evidence.get("issue_number")
            and evidence.get("issue_number") == wanted
            and evidence_run_number
            and target_run_number
            and evidence_run_number != target_run_number
        ):
            # The issue number matches, but the candidate names a different
            # numbered print run/volume than the wanted issue actually
            # belongs to -- "v2 #19" is not "v1 #19" just because both say
            # "#19". A matching bare number is not enough on its own.
            blocked.append("wrong_volume_number")
        elif (
            evidence.get("issue_number")
            and evidence.get("issue_number") == wanted
            and evidence_run_number
            and not target_run_number
        ):
            # The candidate asserts a specific print run/volume and the
            # wanted item's own run identity isn't known -- this could be
            # the right run or a different one; a human needs to confirm
            # it, an automatic pass must not guess.
            review.append("print_run_not_confirmed")
        elif evidence.get("issue_number") and evidence.get("issue_number") == wanted:
            # A volume/book marker alongside the matching issue number
            # identifies which numbered run the issue belongs to; it is not
            # a competing unit type the way it is when no issue number is
            # present at all (a bare collected volume/book, handled below).
            positive.append("exact_issue_number")
            if singleton_exact_match:
                positive.append("singleton_exact_title")
        elif singleton_own_volume_match:
            # The work has exactly one unit, and this file names it "volume 1"
            # instead of "issue 1". Refusing it here treated a one-shot or
            # graphic novel like a series whose volume 1 collects issues 1-6 --
            # the same book was taken as `Title 001` and refused as `Title v01`.
            positive.append("singleton_exact_volume_number")
        elif evidence.get("volume_number") or evidence.get("book_number"):
            blocked.append("wrong_unit_type")
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
        target_volume = target.get("volume_number")
        if range_exact_member:
            positive.append("exact_pack_range_member")
        elif (evidence.get("coverage_start") or evidence.get("coverage_end")) and not manifest_exact_member:
            blocked.append("coverage_not_unit_number")
        elif (
            target_volume
            and not evidence.get("chapter_number")
            and not evidence.get("issue_number")
            and target_volume in (evidence.get("volume_number"), evidence.get("book_number"))
        ):
            # The wanted chapter's own metadata (target_context, populated
            # from the series' real source -- ComicVine/MangaDex volume
            # grouping, never a filename guess) says it belongs to this
            # exact volume, and the candidate's parsed evidence is that same
            # volume with no conflicting chapter/issue identity. A volume
            # release that collects the wanted chapter satisfies it; it is
            # not a different unit type.
            positive.append("exact_volume_containing_wanted_chapter")
        elif (
            wanted
            and evidence.get("bare_number") == wanted
            and evidence.get("issue_number") == wanted
            and not evidence.get("chapter_number")
            and not evidence.get("volume_number")
            and not evidence.get("book_number")
            # An edition marker is an asserted kind even when it carries no
            # number of its own. "Deluxe Edition/Volume_02.cbz" parses with an
            # empty volume_number and a bare 2, so the three checks above let
            # it through and it read as chapter 2. It is volume 2. Caught by
            # re-measuring the A-EXPLICIT control after the change rather than
            # by the unit test, which is the argument for requiring the
            # re-measure and not just a green suite.
            and not evidence.get("edition_marker")
        ):
            # An unmarked number is not a claim about unit kind. The parser
            # files a bare number under issue_number because it has to put it
            # somewhere, and against a chapter target that guess was read as a
            # competing identity: "Hunter x Hunter 394 (2022) (Digital)
            # (LuCaZ).cbz" clears the slskd match gate at score 71 with reason
            # "issue/part token 394" and no penalties, then refuses here.
            # Nothing in the filename says issue.
            #
            # Deliberately narrow, because the volume ranking is the inverse of
            # the cost ranking. Measured 2026-09-02 over 2,061 wrong_unit_type
            # refusals across 424 units: the parser taking a DIFFERENT number
            # than the match gate saw is 1,298 refusals over 239 units and
            # starves only 12, because siblings name the issue plainly. This
            # shape is 98 refusals over 46 units and starves 42 of them -- a
            # file whose only number is bare has no better-named sibling to
            # fall back on. Only the chapter half is taken here: 35 refusals,
            # 23 starved units.
            #
            # The volume branch above already admits a bare number as its own
            # unit kind (singleton_exact_bare_volume_number); this is that idea
            # on the chapter branch. It requires the bare number to BE the
            # wanted chapter and refuses to fire when the candidate asserts any
            # kind of its own, so a stated volume or book still refuses -- 366
            # refusals over 162 units that are correct and must stay.
            positive.append("bare_number_is_the_wanted_chapter")
        elif (evidence.get("volume_number") or evidence.get("book_number") or evidence.get("issue_number")) and not evidence.get("chapter_number"):
            blocked.append("wrong_unit_type")
        elif evidence.get("chapter_number") and wanted and evidence.get("chapter_number") != wanted:
            blocked.append("wrong_chapter_number")
        elif (
            evidence.get("chapter_number")
            and evidence.get("chapter_number") == wanted
            and target_volume
            and evidence_run_number
            and evidence_run_number != target_volume
        ):
            # Same shape as the print-run check on the issue branch above. The
            # chapter number matches, but the candidate files it under a
            # different volume than the wanted chapter actually belongs to --
            # "Vol 03 Ch 007" is not the volume 2 chapter 7 the target names,
            # and chapter numbers repeat across a series' reissues often
            # enough that a bare number match is not proof on its own. Only a
            # volume the target itself confirmed is compared: a chapter whose
            # own volume is unknown is left alone, because a manga chapter
            # number is series-global and a volume marker beside it is
            # ordinary context rather than a competing claim.
            blocked.append("wrong_volume_number")
        elif evidence.get("chapter_number") and evidence.get("chapter_number") == wanted:
            positive.append("exact_chapter_number")
        elif wanted and manifest_exact_member:
            positive.append("exact_pack_manifest_member")
        elif wanted:
            review.append("missing_required_unit_number")

    blocked = list(dict.fromkeys(blocked))
    # Two of the reasons this function appends say only "something is wrong
    # here", not what. candidate_title_mismatch reports that match_confidence
    # came back mismatch; ambiguous_unit_identity reports that the title
    # asserted more than one unit without saying which claim is the wrong one.
    # Both get appended before the unit-specific checks below even run, so
    # left where they land they take position [0] and bury the real reason.
    # A broad series-title-only query evaluates every returned issue against
    # one specific wanted issue, so "right series, wrong issue" is the
    # constant case (measured live: 36% of all candidate_title_mismatch rows
    # also carried a specific reason here), and "v2 #19" against a wanted v1
    # #19 reads as ambiguous for exactly the reason the print-run check
    # already names precisely. Whenever anything more specific is present, let
    # that reason -- not these -- be what gets displayed and explained.
    # Ordered least-specific-last so ambiguous_unit_identity still outranks
    # candidate_title_mismatch when those two are all there is. When the
    # sources actually contradict each other, ambiguous_unit_identity is the
    # specific answer rather than a placeholder, so it keeps its place.
    demotable = set(GENERIC_REJECTION_ORDER)
    if source_identities_conflict:
        demotable.discard("ambiguous_unit_identity")
    if len(blocked) > len(demotable.intersection(blocked)):
        blocked = [reason for reason in blocked if reason not in demotable] + [
            reason for reason in GENERIC_REJECTION_ORDER if reason in demotable and reason in blocked
        ]
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
        "print_run_not_confirmed": f"Result identifies print run/volume {source.get('volume_number') or source.get('book_number')}; the wanted issue's own print run is not confirmed, so this cannot be auto-approved.",
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


def apply_compatibility(verdict, wanted_item=None, *, settings=None):
    out = normalize_candidate(verdict, wanted_item)
    compatibility = candidate_compatibility(out, wanted_item, settings=settings)
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
