"""Canonical library, unit naming, folder, and reader-completion contracts."""

import datetime
import re
import statistics
from pathlib import Path


MANGA_MEDIA_TYPES = {"manga", "manhwa", "manhua"}
COMIC_MEDIA_TYPES = {"comic", "comics", "western_comic"}
UNIT_TYPES = {"issue", "chapter", "volume", "collected", "pack_member"}

READER_VISIBLE_STATUSES = frozenset({"library_visible", "visible"})
# The four members are copied from the two canonical sets the tree already
# agrees on -- inkdrop_state.READER_VISIBILITY_TIMEOUT_STATUSES and
# inkdrop_activity.VISIBILITY_TIMEOUT_STATUSES, which hold the same four. They
# are duplicated rather than imported because this module is a leaf (the
# standard library only) that both of those import from.
READER_VISIBILITY_TIMEOUT_STATUSES = frozenset({"library_scan_timeout", "kavita_scan_timeout", "scan_timeout", "timeout"})
# The reader answered about this file and the answer was adverse. Distinct from
# the set above, which is us giving up on waiting rather than the reader
# reporting anything at all.
READER_VISIBILITY_ADVERSE_STATUSES = frozenset({
    "failed", "missing_file", "wrong_library",
    "wrong_file", "wrong_series_folder", "duplicate_series",
})


def canonical_library_classification(record):
    """Classify from durable work metadata; provider and filename are never evidence."""
    record = record if isinstance(record, dict) else {}
    evidence = []
    for key in ("canonical_media_type", "work_media_type", "series_media_type", "media_type"):
        value = str(record.get(key) or "").strip().lower()
        if value:
            evidence.append((key, value))
    normalized = {"manga" if value in MANGA_MEDIA_TYPES else "comics" if value in COMIC_MEDIA_TYPES else value for _, value in evidence}
    if len(normalized) > 1:
        return {"ok": False, "library_type": "unknown", "reason": "conflicting_durable_media_identity", "evidence": evidence}
    library_type = next(iter(normalized), "unknown")
    return {
        "ok": library_type != "unknown",
        "library_type": library_type,
        "media_type": "manga" if library_type == "manga" else "comic" if library_type == "comics" else library_type,
        "reason": "durable_media_identity" if evidence else "missing_durable_media_identity",
        "evidence": evidence,
    }


def normalize_number(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        return str(int(float(text)))
    return text


_CHAPTER_TITLE_RE = re.compile(r"(?i)\b(?:ch(?:apter)?|ep(?:isode)?)\b\.?\s*#?\s*\d")
# Words a catalogue writes in front of a tankobon's number: "Vol. 1: Mission",
# "Book One", "Band 3" (German), "Ver. 2" (Pluto), "Tome 4", "v05".
_VOLUME_MARKER = r"(?:vol(?:ume)?|book|band|tome|ver)"
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}
_VOLUME_TITLE_RE = re.compile(
    r"(?i)(?:\b" + _VOLUME_MARKER + r"\b\.?\s*#?\s*([0-9]+(?:\.[0-9]+)?|[ivxlc]+|[a-z]+)\b"
    r"|\bv\.?\s*([0-9]+(?:\.[0-9]+)?)\b)"
)
_VOLUME_LEAF_RE = re.compile(r"(?i)(?:\bv|\bvol(?:ume)?\.?\s*)0*([0-9]+)(?![0-9])")
_ISSUE_LEAF_RE = re.compile(r"#\s*0*([0-9]+)(?![0-9])")
_FILE_YEAR_RE = re.compile(r"\((\d{4})\)")
_BOOK_WORD_RE = re.compile(r"(?i)\bbook\b")

# ComicVine release cadence (see comicvine_release_cadence_verdict). Tankobon
# volumes ship every two to six months; a comic-format run of the same manga
# (Epic's Akira, Dark Horse's 1995 Ghost in the Shell) ships monthly, and
# chapters ship weekly. The thresholds sit between the two with margin: every
# real volume run on the 2026-09-23 snapshot clears them, and the monthly runs
# (a median of about 31 days per issue) miss them.
CADENCE_MIN_SERIES_DAYS_PER_ISSUE = 56
CADENCE_MIN_ISSUE_DAYS_PER_ISSUE = 45
# Fewer dated consecutive pairs than this and the series has no cadence to read.
CADENCE_MIN_DATED_PAIRS = 4
# The largest step between consecutive issue numbers the leading run allows
# (one missing number); a bigger jump ends the run (Hunter X Hunter's 320-411
# weekly-chapter block after its volumes).
CADENCE_MAX_RUN_STEP = 2
CADENCE_VOLUME = "volume"
CADENCE_NOT_VOLUME = "not_volume"
CADENCE_UNKNOWN = "unknown"


def _bare_number(value):
    match = re.fullmatch(r"0*(\d+)(?:\.(\d+))?", normalize_number(value))
    if not match:
        return ""
    return match.group(1) + (f".{match.group(2)}" if match.group(2) and match.group(2).strip("0") else "")


def _roman_number(text):
    text = str(text or "").lower()
    # I..XLIX only: a longer run of these letters is a word ("civil"), not a number.
    if not text or not re.fullmatch(r"(?:xl|x{0,3})(?:ix|iv|v?i{0,3})", text):
        return ""
    total = 0
    for index, char in enumerate(text):
        value = _ROMAN_VALUES[char]
        following = _ROMAN_VALUES[text[index + 1]] if index + 1 < len(text) else 0
        total += -value if value < following else value
    return str(total) if total > 0 else ""


def volume_title_number(issue_title):
    """The volume number a catalogue title states, or "" when it names none.

    Only a volume word followed by a number counts ("Vol. 1: Mission",
    "Book One", "Part VIII ... Volume I", "Band 3", "Ver. 2"); a word the
    marker is followed by that is not a number ("Book of Shadows") is no
    evidence at all.
    """
    for match in _VOLUME_TITLE_RE.finditer(str(issue_title or "")):
        token = match.group(1) or match.group(2) or ""
        number = _bare_number(token) or _roman_number(token) or (
            str(_WORD_NUMBERS[token.lower()]) if token.lower() in _WORD_NUMBERS else ""
        )
        if number:
            return number
    return ""


def _predates_series(text, series_year_bare):
    if not series_year_bare:
        return False
    return any(int(year) < int(series_year_bare) for year in _FILE_YEAR_RE.findall(str(text or "")))


def _file_leaf(path):
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1]


def library_files_name_issues(files):
    """Whether any of a series' own library files is named as an issue.

    ``files`` are ``(path, issue_number)`` pairs for files the library holds
    against a known issue of the series. One ``Akira #001`` file says
    ComicVine's numbering for this run is issue numbering. This is only ever
    a veto: it can take volume evidence away, never add it, so a mis-import
    can at worst make InkDrop refuse a volume, not grab a wrong book.
    """
    for path, issue_number in files or ():
        if _bare_number(issue_number) and _ISSUE_LEAF_RE.search(_file_leaf(path)):
            return True
    return False


def library_files_bind_issues_as_volumes(files, *, series_year=None):
    """Whether a series' own library files already hold its issues as volumes.

    Only the fallback for a series whose ComicVine dates can't be read at all
    (comicvine_release_cadence_verdict answers CADENCE_UNKNOWN); a series
    with a readable cadence is decided by the cadence alone, and its library
    files can only veto (library_files_name_issues). See
    inkdrop_state.comicvine_manga_series_volume_evidence.

    ``files`` are ``(path, issue_number)`` pairs for files the library holds
    against a known issue of the series. True only when at least one file is
    named as volume N for its own issue N (``Dragon Ball Super v04.cbz`` for
    Dragon Ball Super #4) and no file is named as an issue (``Akira #001``).
    A volume file whose name carries a parenthesized year before the series'
    own start year (``series_year``) is a different, earlier edition and
    counts for nothing (the Hunter x Hunter 166704 shape: 2005-edition files
    bound to a 2025 series).
    """
    if library_files_name_issues(files):
        return False
    series_year_bare = _bare_number(series_year)
    for path, issue_number in files or ():
        number = _bare_number(issue_number)
        leaf = _file_leaf(path)
        if not number or not leaf:
            continue
        match = _VOLUME_LEAF_RE.search(leaf)
        if match and _bare_number(match.group(1)) == number and not _predates_series(leaf, series_year_bare):
            return True
    return False


def _cadence_date(value):
    try:
        return datetime.date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def comicvine_release_cadence_verdict(dated_issues, issue_number):
    """Whether ComicVine's own release dates say this issue is a volume.

    ``dated_issues`` are ``(issue_number, release_date)`` pairs for the
    series' ComicVine issues (release_date may be empty). The dates are
    ComicVine's cover/store dates, never anything InkDrop imported, so a bad
    download can't forge this and a volume grab can't reinforce it.

    Returns CADENCE_VOLUME, CADENCE_NOT_VOLUME, or CADENCE_UNKNOWN when the
    series has too few dates to have a cadence at all.

    * The run: the series' first unbroken run of whole issue numbers, starting
      at 0 or 1 and stepping by at most CADENCE_MAX_RUN_STEP. A tankobon run
      is numbered 1..n; anything after a jump (Hunter X Hunter's 320-411
      weekly-chapter block) is not part of it.
    * The series: the median days per issue over consecutive-number pairs of
      the run that both have dates and a positive gap (a same-day batch says
      nothing about cadence) must be at least
      CADENCE_MIN_SERIES_DAYS_PER_ISSUE. Fewer than CADENCE_MIN_DATED_PAIRS
      such pairs is CADENCE_UNKNOWN.
    * The issue: it must be in the run, and its days per issue to its nearest
      dated neighbour below and above (each side that has one; an undated issue
      uses the two neighbours that bracket it) normally must each be at least
      CADENCE_MIN_ISSUE_DAYS_PER_ISSUE. One isolated short interval does not
      veto a row when the median of the three nearest dated consecutive-number
      intervals still clears that threshold. That wider neighbourhood keeps a
      launch outlier from demoting the first two rows of an otherwise strongly
      volume-paced run, while two adjacent short intervals still refuse the
      row. An issue outside the run or without a dated neighbour is not a
      volume: a missing date defaults to no.
    """
    dates = {}
    for number, release_date in dated_issues or ():
        bare = _bare_number(number)
        if not bare or "." in bare:
            continue
        dates[int(bare)] = _cadence_date(release_date) or dates.get(int(bare))
    numbers = sorted(dates)
    if not numbers or numbers[0] > 1:
        return CADENCE_UNKNOWN
    run = [numbers[0]]
    for number in numbers[1:]:
        if number - run[-1] > CADENCE_MAX_RUN_STEP:
            break
        run.append(number)
    dated_gaps = [
        (a, b, (dates[b] - dates[a]).days)
        for a, b in zip(run, run[1:])
        if b - a == 1 and dates[a] and dates[b] and (dates[b] - dates[a]).days > 0
    ]
    gaps = [gap for _a, _b, gap in dated_gaps]
    if len(gaps) < CADENCE_MIN_DATED_PAIRS:
        return CADENCE_UNKNOWN
    if statistics.median(gaps) < CADENCE_MIN_SERIES_DAYS_PER_ISSUE:
        return CADENCE_NOT_VOLUME
    bare = _bare_number(issue_number)
    if not bare or "." in bare or int(bare) not in dates or int(bare) not in run:
        return CADENCE_NOT_VOLUME
    number = int(bare)
    below = [n for n in run if n < number and dates[n]]
    above = [n for n in run if n > number and dates[n]]
    rates = []
    if dates[number]:
        if below:
            rates.append((dates[number] - dates[below[-1]]).days / (number - below[-1]))
        if above:
            rates.append((dates[above[0]] - dates[number]).days / (above[0] - number))
    elif below and above:
        rates.append((dates[above[0]] - dates[below[-1]]).days / (above[0] - below[-1]))
    neighbourhood = sorted(
        dated_gaps,
        key=lambda gap: min(abs(gap[0] - number), abs(gap[1] - number)),
    )[:3]
    neighbourhood_passes = (
        len(neighbourhood) == 3
        and statistics.median(gap for _a, _b, gap in neighbourhood) >= CADENCE_MIN_ISSUE_DAYS_PER_ISSUE
    )
    if not rates or (min(rates) < CADENCE_MIN_ISSUE_DAYS_PER_ISSUE and not neighbourhood_passes):
        return CADENCE_NOT_VOLUME
    return CADENCE_VOLUME


def library_volume_leaf_bound_to_other_issue(files, number, issue_id):
    """True when the library already names volume ``number`` for a different issue.

    ``files`` are ``(path, other_issue_id)`` pairs for the series' other
    active media files (any issue, not just the one being asked about).
    Guards an omnibus "Book N" title from being trusted as volume N when the
    series' own per-file numbering already used N for a different issue --
    the two numbering schemes disagree (Kodansha's Vinland Saga 2-in-1
    hardcovers each collect two Japanese volumes, so "Book N" is not
    reliably Japanese volume N).
    """
    number = _bare_number(number)
    if not number:
        return False
    for path, other_issue_id in files or ():
        if str(other_issue_id or "").strip() == str(issue_id or "").strip():
            continue
        leaf = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
        match = _VOLUME_LEAF_RE.search(leaf)
        if match and _bare_number(match.group(1)) == number:
            return True
    return False


def roman_to_arabic(text):
    """The arabic value of a roman numeral I..XLIX token, or "" for anything else.

    Public wrapper over the same restricted parser ``volume_title_number``
    uses, for callers outside this module that need to equate a title's own
    trailing roman numeral with its arabic-digit spelling (see
    inkdrop_slskd_source_probe.title_phrase_present).
    """
    return _roman_number(text)


def comicvine_manga_issue_is_volume(
    media_type, provider, issue_number, chapter_alias, issue_title="", explicit_unit_type="",
    series_volume_evidence=False, library_omnibus_conflict=False, series_unit_refuses_volume=False,
):
    """True when a ComicVine manga row's unnamed unit is positively a volume.

    ComicVine has no chapter concept, and a manga series there may list its
    tankobon volumes (Dragon Ball Super #10 is Viz volume 10, "Moro's Wish")
    or a flopped comic-format run (Epic's Akira #1-#38, Dark Horse's 1995 Ghost
    in the Shell #1-#8) or weekly chapters (Hunter X Hunter #403-#410) as
    "issues". Nothing about the row says which, so the answer is "volume" only
    on positive evidence:

    * the issue title names volume N for this issue N ("Vol. 1: Mission",
      "Book One", "Band 3"), or
    * ``series_volume_evidence``: the series itself says so for this issue --
      the operator's series "Manga Unit" set to volume or another explicit
      series unit setting, or ComicVine's own release cadence (see
      comicvine_release_cadence_verdict and
      inkdrop_state.comicvine_manga_series_volume_evidence).

    ``series_unit_refuses_volume``: the operator set the series' unit to
    chapters (or another explicit series setting says it is not volumes).
    That outranks every automatic signal, a volume-worded title included, so
    an operator can veto a wrong automatic call; the row keeps its default
    issue binding.

    A title that names a chapter, or a volume other than this issue's number
    ("Part II. Nerissa's Revenge Volume 1" for #4, "Part VIII ... Volume I"
    for #23), is never a volume row. A row that names its own unit type is
    answered by that type, not here. The probe and the durable handoff gate
    ask this with the same inputs (see
    inkdrop_state.comicvine_manga_durable_unit_inputs).

    ``library_omnibus_conflict``: the series' own library already names this
    issue's volume number for a DIFFERENT issue (see
    library_volume_leaf_bound_to_other_issue). An omnibus "Book N" title
    (Kodansha's 2-in-1 Vinland Saga hardcovers and similar) is not reliably
    volume N in the series' own per-file numbering -- a 2-in-1 collects two
    Japanese volumes per book -- so this refuses a "Book"-worded title rather
    than trusting it once the library already disagrees about what N means.
    A plain "Vol."/"Ver."/"Tome"/"Band" title is unaffected: those markers
    are not the omnibus convention this guards against.
    """
    if str(explicit_unit_type or "").strip():
        return False
    if str(media_type or "").strip().lower() != "manga":
        return False
    if str(provider or "").strip().lower() != "comicvine":
        return False
    if series_unit_refuses_volume:
        return False
    number = _bare_number(issue_number)
    if not number or _bare_number(chapter_alias) != number:
        return False
    title = str(issue_title or "")
    if _CHAPTER_TITLE_RE.search(title):
        return False
    title_volume = volume_title_number(title)
    if title_volume:
        if title_volume != number:
            return False
        if library_omnibus_conflict and _BOOK_WORD_RE.search(title):
            return False
        return True
    return bool(series_volume_evidence)


def canonical_unit_identity(record):
    record = record if isinstance(record, dict) else {}
    raw_type = str(record.get("unit_type") or record.get("source_unit") or "").strip().lower().replace("-", "_")
    aliases = {
        "collection": "collected", "collected_edition": "collected",
        "issue_member": "pack_member", "pack": "pack_member",
    }
    unit_type = aliases.get(raw_type, raw_type)
    if unit_type not in UNIT_TYPES:
        return {"ok": False, "unit_type": "unknown", "number": "", "reason": "missing_explicit_unit_type"}
    key = {
        "issue": "issue_number",
        "chapter": "chapter_number",
        "volume": "volume_number",
        "collected": "collected_number",
        "pack_member": "pack_member_number",
    }[unit_type]
    number = normalize_number(record.get(key))
    if not number:
        fallback = record.get("normalized_number") if unit_type in {"issue", "chapter", "collected", "pack_member"} else None
        number = normalize_number(fallback)
    return {"ok": bool(number), "unit_type": unit_type, "number": number, "number_source": key, "reason": "explicit_unit_identity" if number else "missing_unit_number"}


def format_number(value, width):
    text = normalize_number(value)
    if re.fullmatch(r"\d+", text):
        return str(int(text)).zfill(width)
    return text


def canonical_filename(series_title, unit, extension=".cbz"):
    unit = unit if isinstance(unit, dict) else {}
    series = str(series_title or "Unknown Series").strip() or "Unknown Series"
    unit_type = unit.get("unit_type")
    number = unit.get("number")
    if not unit.get("ok") or unit_type not in UNIT_TYPES:
        raise ValueError("explicit canonical unit identity is required")
    labels = {
        "issue": f"#{format_number(number, 3)}",
        "chapter": f"c{format_number(number, 3)}",
        "volume": f"v{format_number(number, 2)}",
        "collected": f"Collected Edition {format_number(number, 2)}",
        "pack_member": f"Pack Member {format_number(number, 3)}",
    }
    ext = str(extension or ".cbz")
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"{series} {labels[unit_type]}{ext.lower()}"


def relative_series_folder(value):
    """A locked series folder as a path under the library root, or "" when it is not one.

    A lock is usually one folder name, but the first lock for a work is seeded
    from where its files already are, and that can be nested one level down:
    "Daytripper (2010)/Volume 01 (2010)". Keeping only the last component of
    that turned it into "Volume 01 (2010)", a folder directly under the root
    that the work never owned. An absolute path, a drive, or an empty, "." or
    ".." component is never a folder under the root, so it is not returned.
    """
    text = str(value or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:(/|$)", text):
        return ""
    segments = text.split("/")
    if any(not segment.strip() or segment.strip() in {".", ".."} for segment in segments):
        return ""
    return text


def stable_folder_decision(proposed_folder, persisted_folder=None, reader_observed_folder=None):
    proposed = Path(str(proposed_folder or "Unknown")).name
    persisted_text = str(persisted_folder or "").strip()
    persisted = relative_series_folder(persisted_text) if persisted_text else ""
    # A lock that is not a folder under the root is not used to build a path.
    # The proposed folder is planned instead, and persist_series_folder_identity()
    # refuses it against the stored lock, so the import stops for review.
    persisted_invalid = bool(persisted_text and not persisted)
    observed = Path(str(reader_observed_folder or "")).name if reader_observed_folder else ""
    selected = persisted or observed or proposed
    drift = bool(persisted and proposed and persisted.casefold() != proposed.casefold())
    collision = bool(persisted and observed and persisted.casefold() != observed.casefold())
    if persisted_invalid:
        reason = "persisted_folder_not_relative"
    elif persisted:
        reason = "persisted_folder_identity"
    elif observed:
        reason = "reader_observed_folder"
    else:
        reason = "initial_folder_identity"
    return {
        "series_folder": selected,
        "persisted": bool(persisted),
        "persisted_folder_invalid": persisted_invalid,
        "metadata_drift": drift,
        "operator_review_required": collision or persisted_invalid,
        "reason": reason,
    }


def reader_expectation(record, *, filename, series_folder):
    classification = canonical_library_classification(record)
    unit = canonical_unit_identity(record)
    work_id = str(record.get("work_id") or record.get("native_series_id") or "").strip()
    reader_series_id = str(record.get("kavita_series_id") or record.get("reader_series_id") or "").strip()
    reader_library_id = str(record.get("kavita_library_id") or record.get("reader_library_id") or "").strip()
    return {
        "ok": bool(
            classification.get("ok") and unit.get("ok") and work_id and reader_series_id
            and reader_library_id and filename and series_folder
        ),
        "library_type": classification.get("library_type"),
        "work_id": work_id,
        "canonical_series_id": str(record.get("canonical_series_id") or work_id),
        "reader_series_id": reader_series_id,
        "reader_library_id": reader_library_id,
        "unit_type": unit.get("unit_type"),
        "unit_number": unit.get("number"),
        "series_folder": str(series_folder),
        "filename": str(filename),
    }


def completion_projection(record):
    record = record if isinstance(record, dict) else {}
    reader_configured = bool(record.get("reader_configured"))
    reader_required = bool(record.get("reader_required"))
    imported = bool(record.get("imported"))
    scan_requested = bool(record.get("reader_scan_requested"))
    visibility = str(record.get("reader_visibility_status") or "").strip().lower()
    if not imported:
        state = "artifact_verified" if record.get("artifact_verified") else "downloaded" if record.get("downloaded") else "pending"
    elif reader_required and not reader_configured:
        state = "reader_visibility_failed"
    elif not reader_configured:
        state = "imported"
    elif visibility in READER_VISIBLE_STATUSES:
        state = "reader_visible"
    elif visibility in READER_VISIBILITY_TIMEOUT_STATUSES:
        # Tested before the adverse arm: a scan we stopped waiting for is not a
        # verdict about the file, and it is the name inkdrop_activity already
        # gives this input.
        state = "reader_visibility_timeout"
    elif visibility in READER_VISIBILITY_ADVERSE_STATUSES:
        state = "reader_visibility_failed"
    elif scan_requested or visibility in {"pending", "not_visible"}:
        state = "reader_scan_pending"
    else:
        state = "imported"
    complete = bool(imported and (not reader_required or state == "reader_visible"))
    return {"state": state, "complete": complete, "reader_configured": reader_configured, "reader_required": reader_required}
