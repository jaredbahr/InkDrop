#!/usr/bin/env python3
import argparse
import atexit
import hashlib
import hmac
import json
import os
import re
import requests
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

try:
    import inkdrop_state
except Exception:
    inkdrop_state = None

import inkdrop_runtime_config

try:
    import inkdrop_language
except Exception:
    inkdrop_language = None

import inkdrop_folder_cleanup
import inkdrop_library_frontends
import inkdrop_artifact_acceptance
import inkdrop_library_identity


STATE_DIR = inkdrop_runtime_config.state_dir()
CONFIG_DIR = inkdrop_runtime_config.config_dir()
LOG_DIR = inkdrop_runtime_config.log_dir()
STAGING_DIR = inkdrop_runtime_config.staging_dir()
MANUAL_INBOX_DIR = inkdrop_runtime_config.manual_inbox_dir()
QUARANTINE_DIR = inkdrop_runtime_config.quarantine_dir()
INKDROP_STATE_DB = STATE_DIR / (inkdrop_state.STATE_DB_NAME if inkdrop_state else "inkdrop-state.sqlite3")
DB_PATH = STATE_DIR / "imported-files.sqlite3"
LOG_PATH = LOG_DIR / "inkdrop-import.log"
IMPORT_STATUS_PATH = STATE_DIR / "import-status.json"
PACK_IMPORT_LOG = LOG_DIR / "pack-import.log"
PACK_REVIEW_STATE_FILE = STATE_DIR / "pack-review-state.json"
PACK_AUTO_IMPORT_STATUS_FILE = STATE_DIR / "pack-auto-import-status.json"
CUTOFF_PATH = STATE_DIR / "import-cutoff.timestamp"
PENDING_IMPORTS_LOG = STATE_DIR / "pending-imports.jsonl"
MAX_IMPORT_STATUS_EVENT_BYTES = 64 * 1024 * 1024
REVIEW_FILE = STATE_DIR / "manual-review.jsonl"
MANUAL_REVIEW_ACTIONS_FILE = STATE_DIR / "manual-review-actions.json"
MANUAL_SOURCE_AUTORESOLVE_STATUS_FILE = STATE_DIR / "manual-source-autoresolve-status.json"
KAPOWARR_DB = inkdrop_runtime_config.kapowarr_db_path()
KAPOWARR_API = os.environ.get("INKDROP_KAPOWARR_URL") or ""
KAVITA_DB = inkdrop_runtime_config.kavita_db_path()
KAVITA_API = os.environ.get("INKDROP_KAVITA_URL") or ""
KAVITA_COMIC_ROOT = os.environ.get("INKDROP_KAVITA_COMIC_ROOT") or "/data/comics"
KAVITA_MANGA_ROOT = os.environ.get("INKDROP_KAVITA_MANGA_ROOT") or "/data/manga"
KOMGA_API = os.environ.get("INKDROP_KOMGA_URL") or ""
QBIT_CONFIG = Path(os.environ.get("INKDROP_QBITTORRENT_CONFIG") or CONFIG_DIR / "qbit_manage" / "config.yml")
USER_LOCAL_UNRAR = Path(os.environ.get("INKDROP_UNRAR_PATH") or "/usr/bin/unrar-free")
CBR_PARTIAL_EXTRACT_MIN_RATIO = 0.85
CBR_PARTIAL_EXTRACT_MAX_MISSING = 5
CBR_PARTIAL_EXTRACT_MIN_PAGES = 10
CBR_LIST_TIMEOUT_SECONDS = int(os.environ.get("INKDROP_CBR_LIST_TIMEOUT_SECONDS") or 30)
CBR_EXTRACT_TIMEOUT_SECONDS = int(os.environ.get("INKDROP_CBR_EXTRACT_TIMEOUT_SECONDS") or 90)
SQLITE_BUSY_TIMEOUT_SECONDS = 60
SQLITE_BUSY_TIMEOUT_MS = SQLITE_BUSY_TIMEOUT_SECONDS * 1000
MANGADEX_API = "https://api.mangadex.org"
MANGADEX_USER_AGENT = "InkDrop/0.1 (+manga volume coverage lookup)"
MANGADEX_VOLUME_AGGREGATE_CACHE_FILE = STATE_DIR / "mangadex-volume-aggregate-cache.json"
MANGADEX_VOLUME_AGGREGATE_CACHE_TTL_SECONDS = 7 * 24 * 3600

COMIC_SOURCES = [
    Path(os.environ.get("INKDROP_DIRECT_DOWNLOAD_ROOT") or STAGING_DIR / "direct" / "comics"),
    Path(os.environ.get("INKDROP_UNMATCHED_DOWNLOAD_ROOT") or STAGING_DIR / "downloads" / "comics"),
]
MANUAL_COMIC_SOURCES = [
    Path(os.environ.get("INKDROP_MANUAL_COMICS_INBOX") or MANUAL_INBOX_DIR / "comics"),
]
SUWAYOMI_COMIC_SOURCES = [
    Path(os.environ.get("INKDROP_SUWAYOMI_STAGING_ROOT") or STAGING_DIR / "suwayomi"),
]
SLSKD_COMIC_SOURCES = [
    Path(os.environ.get("INKDROP_SLSKD_DOWNLOAD_ROOT") or STAGING_DIR / "slskd"),
]
INTERNAL_IMPORT_DIR_NAMES = {
    "_failed-proof-copies",
    "_manga-library-split-quarantine",
    "_Incoming",
    "_quarantine",
    "_processed",
    "_failed",
}
COMIC_DEST = Path(os.environ.get("INKDROP_COMIC_INCOMING_ROOT") or Path(os.environ.get("INKDROP_COMIC_ROOT") or "/library/comics") / "_Incoming")
COMIC_ROOT = Path(os.environ.get("INKDROP_COMIC_ROOT") or "/library/comics")
MANGA_ROOT = Path(os.environ.get("INKDROP_MANGA_ROOT") or "/library/manga")
KAPOWARR_COMIC_ROOT = "/comics"
QBIT_CONTAINER_DOWNLOAD_ROOT = "/downloads"
QBIT_HOST_DOWNLOAD_ROOT = Path(os.environ.get("INKDROP_QBITTORRENT_DOWNLOAD_ROOT") or STAGING_DIR / "downloads")
QBIT_BROAD_TAGS = {"inkdrop", "kavita-acquire"}
PACK_DUPLICATE_QUARANTINE_ROOT = Path(os.environ.get("INKDROP_PACK_DUPLICATE_QUARANTINE_ROOT") or QUARANTINE_DIR / "pack-duplicates")
ONE_WORD_TITLE_SECOND_WORD_BLOCKLIST = {"of"}

EBOOK_SOURCES = [
    Path(os.environ.get("INKDROP_EBOOK_DOWNLOAD_ROOT") or STAGING_DIR / "ebooks"),
]
MANUAL_EBOOK_SOURCES = [
    Path(os.environ.get("INKDROP_MANUAL_EBOOKS_INBOX") or MANUAL_INBOX_DIR / "ebooks"),
]
EBOOK_DEST = Path(os.environ.get("INKDROP_EBOOK_INCOMING_ROOT") or "/library/ebooks/_Incoming")

EXT_TO_KIND = {
    ".cbz": "comics",
    ".cbr": "comics",
    ".pdf": "ebooks",
    ".epub": "ebooks",
}


def sqlite_connect(path):
    conn = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    conn.execute(f"pragma busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def read_json_file(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def run_subprocess_bounded(args, timeout, **kwargs):
    popen_kwargs = dict(kwargs)
    if popen_kwargs.pop("capture_output", False):
        popen_kwargs.setdefault("stdout", subprocess.PIPE)
        popen_kwargs.setdefault("stderr", subprocess.PIPE)
    if os.name == "nt":
        popen_kwargs["creationflags"] = popen_kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stdout, stderr = proc.communicate()
        timeout_exc = subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
        raise timeout_exc from exc
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def manual_source_priority_waiting():
    autoresolve = read_json_file(MANUAL_SOURCE_AUTORESOLVE_STATUS_FILE, {}) or {}
    if isinstance(autoresolve, dict):
        state = str(autoresolve.get("state") or "").strip().lower()
        ready = int(autoresolve.get("ready_detected_count") or 0)
        eligible = int(autoresolve.get("eligible_count") or 0)
        updated_at = float(autoresolve.get("updated_at") or autoresolve.get("generated_at") or 0)
        if state == "importing" and ready and updated_at >= time.time() - 900:
            return {
                "manual_source_state": state,
                "manual_source_ready": ready,
                "manual_source_eligible": eligible,
                "reason": "manual_source_autoresolve_ready",
            }
    return None


def broad_pending_import_should_yield(args):
    if args.kind != "comics":
        return None
    if not args.pending_only or not args.all_series:
        return None
    if args.manual_inbox or args.suwayomi_staging or args.source_file or args.series:
        return None
    return manual_source_priority_waiting()
COMIC_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
COLLECTION_TRUTH_MODEL = "kavita_collection"
MANGA_UNIT_MODELS = {
    "volume",
    "chapter",
    "pack",
    "mixed_volume_preferred",
    "mixed_chapter_preferred",
    "unknown/manual",
}
MANGA_CHAPTER_ALLOWED_MODELS = {"chapter", "mixed_volume_preferred", "mixed_chapter_preferred"}
MANGA_VOLUME_ALLOWED_MODELS = {"volume", "pack", "mixed_volume_preferred", "mixed_chapter_preferred"}
MANGA_UNIT_POLICY_TO_MODEL = {
    "volume_only": "volume",
    "chapter_native": "chapter",
    "pack_only": "pack",
    "mixed_allowed": "mixed_volume_preferred",
    "mixed_volume_preferred": "mixed_volume_preferred",
    "mixed_chapter_preferred": "mixed_chapter_preferred",
    "manual_review": "unknown/manual",
    "unknown_manual": "unknown/manual",
    "unknown/manual": "unknown/manual",
}
MANGA_UNIT_MODEL_TO_POLICY = {
    "volume": "volume_only",
    "chapter": "chapter_native",
    "pack": "pack_only",
    "mixed_volume_preferred": "mixed_allowed",
    "mixed_chapter_preferred": "mixed_allowed",
    "unknown/manual": "manual_review",
}
MANGA_UNIT_POLICY_LABELS = {
    "volume_only": "Volume only",
    "chapter_native": "Chapter native",
    "pack_only": "Pack only",
    "mixed_allowed": "Volumes and chapters",
    "mixed_volume_preferred": "Volumes and chapters",
    "mixed_chapter_preferred": "Chapters and volumes",
    "manual_review": "Manual review",
}


def normalize_manga_unit_model(value):
    raw = str(value or "unknown/manual").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in MANGA_UNIT_POLICY_TO_MODEL:
        return MANGA_UNIT_POLICY_TO_MODEL[raw]
    raw = raw.replace("unknown_manual", "unknown/manual")
    return raw if raw in MANGA_UNIT_MODELS else "unknown/manual"


def manga_unit_policy_for_model(model):
    return MANGA_UNIT_MODEL_TO_POLICY.get(normalize_manga_unit_model(model), "manual_review")


def manga_unit_policy_payload(model):
    normalized_model = normalize_manga_unit_model(model)
    policy = manga_unit_policy_for_model(normalized_model)
    label = MANGA_UNIT_POLICY_LABELS.get(normalized_model) or MANGA_UNIT_POLICY_LABELS.get(policy, policy.replace("_", " ").title())
    return {
        "series_unit_policy": policy,
        "series_unit_policy_label": label,
        "series_unit_allows_chapter": manga_policy_allows_chapter(normalized_model),
        "series_unit_allows_volume": manga_policy_allows_volume(normalized_model),
    }


def xml_text(value):
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xml_node(name, value):
    value = inkdrop_artifact_acceptance.safe_metadata_value(value, field=name)
    if value is None or value == "":
        return ""
    return f"  <{name}>{xml_text(value)}</{name}>\n"


MANGA_PUBLISHER_HINTS = {
    "shueisha",
    "hakusensha",
    "kodansha",
    "viz",
    "yen press",
    "seven seas",
    "dark horse manga",
    "tokyopop",
    "square enix",
}
MANGA_TITLE_HINTS = {
    "berserk",
    "chainsaw man",
    "onepiece",
    "one piece",
    "firepunch",
    "fire punch",
}
COLLECTION_RANGE_RULES = {
    ("monstress", "book one"): (1, 18),
    ("monstress", "book two"): (19, 36),
    ("monstress", "book three"): (37, 54),
}
MANGA_SCAN_POLL_SECONDS = 20
MANGA_SCAN_TIMEOUT_SECONDS = 600
SOURCE_FILE_SCAN_TIMEOUT_SECONDS = 420


def normalize_series(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def title_words_pattern(title):
    words = re.findall(r"[a-z0-9]+", str(title or "").lower())
    return r"[\W_]+".join(re.escape(word) for word in words)


TERMINAL_IMAGE_IMPRINT_RE = re.compile(
    r"\s*\(\s*image\s*,\s*(?:19|20)\d{2}[-_.](?:0?[1-9]|1[0-2])\s*\)\s*$",
    re.I,
)
ANY_IMAGE_IMPRINT_RE = re.compile(
    r"\(\s*image\s*,\s*(?:19|20)\d{2}[-_.](?:0?[1-9]|1[0-2])\s*\)",
    re.I,
)


def strip_terminal_image_imprint(segment):
    """Strip only a terminal Image-publisher month stamp from a title stem."""

    text = str(segment or "")
    if not TERMINAL_IMAGE_IMPRINT_RE.search(text):
        return text, False
    return TERMINAL_IMAGE_IMPRINT_RE.sub("", text).rstrip(), True


def exact_numbered_series_title(title_pattern, value):
    return bool(re.match(
        rf"^\s*{title_pattern}[\W_]+(?:#\s*|(?:issue|iss|no|number)\.?\s*)?0*\d+(?:\.\d+)?\s*$",
        str(value or ""),
        re.I,
    ))


def related_subseries_source_blocker(series_title, source_path, issue_title=None, issue_number=None, publisher=None):
    pattern = title_words_pattern(series_title)
    if not pattern:
        return ""
    stop_words = {
        "a",
        "an",
        "and",
        "by",
        "digital",
        "edition",
        "english",
        "fixed",
        "hybrid",
        "of",
        "scan",
        "scans",
        "the",
    }
    edition_words = {
        "anniversary",
        "collection",
        "deluxe",
        "hardcover",
        "hc",
        "library",
        "omnibus",
        "paperback",
        "tpb",
        "trade",
    }
    pack_markers = {"complete", "pack", "set"}
    unit_markers = {
        "book",
        "books",
        "ch",
        "chapter",
        "chapters",
        "issue",
        "issues",
        "part",
        "parts",
        "pt",
        "pts",
        "v",
        "vol",
        "vols",
        "volume",
        "volumes",
    }
    relation_markers = {
        "after",
        "before",
        "gaiden",
        "prelude",
        "spin",
        "spinoff",
        "stories",
        "story",
    }
    title_words = set(re.findall(r"[a-z0-9]+", str(series_title or "").lower()))
    source_text = str(source_path or "").replace("\\", "/")
    source_parts = [part for part in source_text.split("/") if part]
    leaf = source_parts[-1] if source_parts else source_text
    leaf_stem = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", leaf)
    # SLSKD appends a numeric id to a staged filename to avoid colliding with
    # a same-named file already present -- a .NET DateTime.Ticks value in
    # every real case seen (consistently 18 digits), not part of the
    # release's own name. Left in place, it reads as an unexplained trailing
    # word and the file blocks as a different book even when every other
    # signal -- series, unit, annotations -- is correct. Strip it the same
    # way the extension already is, before any tail is read for evidence.
    leaf_stem = re.sub(r"_\d{17,19}$", "", leaf_stem)
    segments = [leaf_stem, *source_parts[:-1]]
    for segment_index, segment in enumerate(segments):
        original_segment = str(segment or "")
        segment, terminal_image_imprint = strip_terminal_image_imprint(original_segment)
        if ANY_IMAGE_IMPRINT_RE.search(original_segment) and not terminal_image_imprint:
            suffix = ANY_IMAGE_IMPRINT_RE.split(original_segment, maxsplit=1)[-1].strip()
            return "related subseries title tail after publisher imprint: " + (suffix or "unexpected suffix")
        if terminal_image_imprint and not exact_numbered_series_title(pattern, segment):
            return "publisher imprint is not attached to an exact numbered series title"
        contained = re.match(rf"^\s*(?P<head>.*?){pattern}(?P<tail>.*)$", str(segment or ""), re.I)
        if contained and contained.group("head"):
            head_words = re.findall(r"[a-z0-9]+", (contained.group("head") or "").lower())
            tail_words = re.findall(r"[a-z0-9]+", (contained.group("tail") or "").lower())
            suspicious_head = [
                word
                for word in head_words
                if word not in stop_words and word not in edition_words and word not in title_words
            ]
            if suspicious_head and any(word in relation_markers for word in [*head_words, *tail_words]):
                return "related subseries title prefix: " + " ".join(suspicious_head[:5])
        match = re.match(rf"^\s*{pattern}(?P<tail>.*)$", str(segment or ""), re.I)
        if not match:
            continue
        if inkdrop_artifact_acceptance.trusted_issue_subtitle_matches_release(
            series_title,
            segment,
            issue_title,
            issue_number,
        ):
            continue
        words = re.findall(r"[a-z0-9]+", (match.group("tail") or "").lower())
        if words:
            tail_text = match.group("tail") or ""
            if segment_index > 0 and inkdrop_artifact_acceptance.benign_exact_title_organizational_folder_tail(
                tail_text
            ):
                continue
            if inkdrop_artifact_acceptance.benign_exact_title_publication_tail(
                tail_text,
                issue_number,
                stop_words=stop_words,
                edition_words=edition_words,
                publisher=publisher,
            ):
                continue
            if re.search(r"[\[(][^\[\]()]+[\])]", tail_text):
                return "related subseries or untrusted publication suffix"
        suspicious = []
        for index, word in enumerate(words):
            if word in stop_words or word in edition_words or word in title_words:
                continue
            if re.match(r"^(?:v|vols?|volumes?|books?|issues?|parts?|pts?|chapters?|ch)0*\d{1,4}$", word):
                break
            if word in pack_markers or word in unit_markers:
                break
            if word.isdigit():
                number = int(word)
                next_word = words[index + 1] if index + 1 < len(words) else ""
                next_number = int(next_word) if next_word.isdigit() else None
                if 1900 <= number <= 2099:
                    break
                if next_number is not None:
                    break
                if not next_word or next_word in unit_markers or next_word in pack_markers or next_word in stop_words or next_word in edition_words:
                    break
                suspicious.append(word)
                continue
            suspicious.append(word)
        if suspicious:
            return "related subseries title tail: " + " ".join(suspicious[:5])
    return ""


def supplemental_source_blocker(source_path):
    try:
        parts = [part for part in Path(source_path).parts[-5:] if part]
    except TypeError:
        parts = [str(source_path or "")]
    text = normalize_series(" ".join(parts))
    if re.search(r"\bcovers?\s+only\b", text):
        return "supplemental_cover_only"
    if re.search(r"\bvariant\s+covers?\b", text):
        return "supplemental_variant_cover"
    return ""


SOURCE_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
SOURCE_VOLUME_RE = re.compile(r"(?:^|[\s._#-])(?:v|vol|volume)[\s._-]*0*(\d{1,3})(?:$|[\s._#-])", re.I)
SOURCE_ISSUE_RE = re.compile(r"(?:^|[\s._#-])#?0*(\d{1,5})(?:\.\d+)?(?:$|[\s._#-])")


def source_identity_clues(*values):
    text = " ".join(str(value or "") for value in values if value not in (None, ""))
    years = []
    for match in SOURCE_YEAR_RE.finditer(text):
        year = match.group(1)
        if year not in years:
            years.append(year)
    volumes = []
    for match in SOURCE_VOLUME_RE.finditer(text):
        volume = str(int(match.group(1)))
        if volume not in volumes:
            volumes.append(volume)
    issues = []
    for match in SOURCE_ISSUE_RE.finditer(text):
        number = format_issue_number(match.group(1))
        if number and number not in issues:
            issues.append(number)
    return {
        "years": years[:6],
        "volume_markers": volumes[:6],
        "issue_numbers": issues[:8],
        "has_v1_marker": "1" in volumes,
    }


def year_from_date(value):
    match = re.match(r"^\s*((?:19|20)\d{2})(?:-\d{1,2}(?:-\d{1,2})?)?", str(value or ""))
    return match.group(1) if match else ""


def target_expected_issue_years(target=None, event=None, canonical=None):
    target = target if isinstance(target, dict) else {}
    event = event if isinstance(event, dict) else {}
    canonical = canonical if isinstance(canonical, dict) else {}
    years = []
    for value in (
        canonical.get("canonical_year"),
        event.get("canonical_year"),
        event.get("issue_year"),
        year_from_date(event.get("release_date") or event.get("date")),
    ):
        text = str(value or "").strip()
        if re.match(r"^(?:19|20)\d{2}$", text) and text not in years:
            years.append(text)
    if not years:
        for value in (event.get("year"), event.get("series_year"), target.get("year")):
            text = str(value or "").strip()
            if re.match(r"^(?:19|20)\d{2}$", text) and text not in years:
                years.append(text)
    return years


def source_target_identity_blocker(source_path, target=None, event=None, canonical=None):
    target = target if isinstance(target, dict) else {}
    event = event if isinstance(event, dict) else {}
    canonical = canonical if isinstance(canonical, dict) else {}
    if not target or is_manga_target(target):
        return {}
    source_text = str(source_path or event.get("source") or "")
    if not source_text:
        return {}
    if filename_has_range_or_pack(source_text):
        return {}
    if target_single_issue_artifact_title(target):
        return {}
    clues = source_identity_clues(source_text)
    if not clues.get("has_v1_marker"):
        return {}
    expected_years = target_expected_issue_years(target, event, canonical)
    source_years = [year for year in clues.get("years") or [] if year]
    if not source_years or not expected_years:
        return {}
    if any(year in expected_years for year in source_years):
        return {}
    expected_issue = format_issue_number(
        (canonical or {}).get("canonical_issue_number")
        or event.get("trusted_issue")
        or event.get("trusted_issue_number")
        or event.get("canonical_issue_number")
        or event.get("issue_number")
        or target.get("issue_number")
    )
    source_issues = set(clues.get("issue_numbers") or [])
    if expected_issue and source_issues and expected_issue not in source_issues:
        return {}
    return {
        "reason": "source_year_volume_identity_mismatch",
        "source_years": source_years,
        "expected_years": expected_years,
        "source_volume_markers": clues.get("volume_markers") or [],
        "source_issue_numbers": clues.get("issue_numbers") or [],
        "expected_issue_number": expected_issue,
        "note": "Source filename/path looks like a different volume/year than the target issue.",
    }


def normalize_manga_number(value):
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    raw = match.group(0)
    try:
        number = float(raw)
    except ValueError:
        return None
    if number < 0:
        return None
    if number.is_integer():
        return f"{int(number):03d}"
    whole, _, frac = raw.partition(".")
    frac = frac.rstrip("0") or "0"
    return f"{int(whole):03d}.{frac}"


def comicinfo_status(path):
    path = Path(path)
    if comic_archive_suffix(path) != ".cbz":
        return "not_cbz"
    try:
        with zipfile.ZipFile(path) as archive:
            return "present" if any(name.lower() == "comicinfo.xml" for name in archive.namelist()) else "missing"
    except (OSError, zipfile.BadZipFile):
        return "unreadable"


def comic_archive_suffix(path):
    return inkdrop_artifact_acceptance.comic_archive_suffix(path)


def archive_entry_names(path):
    ext = comic_archive_suffix(path)
    if ext == ".cbz":
        try:
            with zipfile.ZipFile(path) as archive:
                return archive.namelist()
        except (OSError, zipfile.BadZipFile):
            return []
    if ext == ".cbr":
        proc = subprocess.run(
            ["7z", "l", "-slt", str(path)],
            text=True,
            errors="replace",
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0:
            return []
        entries = []
        for line in proc.stdout.splitlines():
            if line.startswith("Path = "):
                name = line[7:].strip()
                if name:
                    entries.append(name)
        return entries
    return []


# These members are metadata (ComicInfo.xml and friends), never payload. Both
# branches below used to pull the whole thing into memory before anything looked
# at its size, on archives that arrive unattended from anonymous peers -- so a
# compressible multi-gigabyte member could exhaust the worker. The container has
# no memory limit, so that does not stop at InkDrop. A megabyte is far more than
# any real metadata document needs.
MAX_ARCHIVE_MEMBER_TEXT_BYTES = 1 * 1024 * 1024
# Wall-clock ceiling for extracting one member out of a .cbr. Covers the whole
# helper-process interaction, not any single step of it.
MAX_ARCHIVE_MEMBER_READ_SECONDS = int(
    os.environ.get("INKDROP_ARCHIVE_MEMBER_READ_TIMEOUT_SECONDS") or 120
)


def terminate_process_tree(proc):
    """Kill a helper process and anything it spawned, then reap it.

    Killing only the direct child is not enough: a grandchild inherits the
    write end of the pipe, so a reader blocked on that pipe never sees EOF and
    stays blocked even though the process we started is gone.
    """
    try:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ValueError):
                proc.kill()
        else:
            proc.kill()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def archive_member_text(path, member_name):
    ext = comic_archive_suffix(path)
    limit = MAX_ARCHIVE_MEMBER_TEXT_BYTES
    if ext == ".cbz":
        try:
            with zipfile.ZipFile(path) as archive:
                # Declared size first, so a decompression bomb is refused before
                # a byte is inflated; then read limit+1 and require it to match,
                # which catches a member whose header understates its real size.
                info = archive.getinfo(member_name)
                if info.file_size > limit:
                    return ""
                with archive.open(info) as member:
                    data = member.read(limit + 1)
                if len(data) != info.file_size:
                    return ""
                return data.decode("utf-8", errors="replace")
        except (OSError, zipfile.BadZipFile, KeyError):
            return ""
    if ext == ".cbr":
        # 7z streams to stdout, so there is no declared size to consult and
        # capture_output would buffer the whole stream. Read a bounded prefix
        # and discard the rest instead.
        #
        # The prefix has to be bounded in TIME as well as in bytes. read(n)
        # returns on n bytes or on EOF and nothing else, so a 7z that emits
        # nothing -- or a short prefix and then stalls -- blocks here forever,
        # and a proc.wait(timeout=...) written after it never runs at all. That
        # is not a theoretical race: this call sits on the import path and runs
        # under the shared comics-import lock, so one stalled child stops every
        # comic landing until the 2700s job timeout tears down the whole run.
        # One monotonic deadline therefore covers the read, the wait, and the
        # cleanup, rather than each step carrying its own.
        deadline = time.monotonic() + MAX_ARCHIVE_MEMBER_READ_SECONDS
        popen_kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}
        if os.name == "posix":
            # Give 7z its own process group. Killing just the direct child
            # leaves a grandchild holding the write end of the pipe, and the
            # reader stays blocked on a pipe that will never reach EOF.
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                ["7z", "e", "-so", str(path), member_name], **popen_kwargs
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return ""
        prefix = {}

        def read_prefix():
            try:
                prefix["data"] = proc.stdout.read(limit + 1)
            except BaseException:
                prefix["data"] = b""

        reader = threading.Thread(target=read_prefix, daemon=True)
        reader.start()
        reader.join(max(0.0, deadline - time.monotonic()))
        if reader.is_alive():
            terminate_process_tree(proc)
            # The kill closes the pipe, which releases the blocked read.
            reader.join(10)
            return ""
        try:
            # Close before waiting: if the member was larger than the prefix,
            # 7z is still writing and would block on a full pipe forever.
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc)
            return ""
        data = prefix.get("data") or b""
        if proc.returncode != 0 or len(data) > limit:
            return ""
        return data.decode("utf-8", errors="replace")
    return ""


def is_internal_import_path(path, root=None):
    try:
        parts = Path(path).relative_to(root).parts if root else Path(path).parts
    except ValueError:
        parts = Path(path).parts
    return any(part.startswith("_") or part in INTERNAL_IMPORT_DIR_NAMES for part in parts)


def read_comicinfo(path):
    path = Path(path)
    if comic_archive_suffix(path) not in {".cbz", ".cbr"}:
        return {}
    names = [name for name in archive_entry_names(path) if name.lower().endswith("comicinfo.xml")]
    if not names:
        return {}
    preferred = sorted(names, key=lambda item: ("/" in item, len(item)))[0]
    raw = archive_member_text(path, preferred)
    if not raw:
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {"raw": raw}
    out = {"raw": raw, "comicinfo_path": preferred}
    for child in root:
        tag = child.tag.split("}", 1)[-1]
        out[tag] = (child.text or "").strip()
    return out


def comicinfo_language_gate(path):
    info = read_comicinfo(path)
    if inkdrop_language is not None:
        language_check = inkdrop_language.classify_release_language(
            title=Path(path).name,
            path=str(path),
            comicinfo=info,
            metadata=info,
            preferred_languages=("en",),
            unknown_policy="allow_if_exact",
        )
        if language_check.get("blocked"):
            return {
                "ok": False,
                "language": language_check.get("language") or "",
                "comicinfo_path": (info or {}).get("comicinfo_path") or "",
                "detail": language_check.get("detail") or "wrong language source",
            }
    if not info:
        return {"ok": True, "language": "", "comicinfo_path": ""}
    language = str(info.get("LanguageISO") or "").strip().lower()
    if not language:
        return {"ok": True, "language": "", "comicinfo_path": info.get("comicinfo_path") or ""}
    primary = re.split(r"[-_]", language, 1)[0]
    if primary and primary != "en":
        return {
            "ok": False,
            "language": language,
            "comicinfo_path": info.get("comicinfo_path") or "",
            "detail": f"wrong language source: ComicInfo LanguageISO={language}",
        }
    return {"ok": True, "language": language, "comicinfo_path": info.get("comicinfo_path") or ""}


def collection_range_from_text(series, title, summary):
    series_key = normalize_series(series)
    title_key = normalize_series(title)
    if (series_key, title_key) in COLLECTION_RANGE_RULES:
        return COLLECTION_RANGE_RULES[(series_key, title_key)]
    text = " ".join(str(value or "") for value in (title, summary))
    match = re.search(r"\bcollects?\s+[A-Z0-9 .:'’!&-]*#\s*(\d{1,5})\s*[-–]\s*(\d{1,5})\b", text, re.I)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if 0 < start <= end <= 10000:
            return start, end
    return None


def collection_info_for_path(path, targets):
    info = read_comicinfo(path)
    if not info:
        return None
    series = info.get("Series") or ""
    title = info.get("Title") or ""
    series_match = re.match(r"^(.*?)\s*:\s*(Book\s+(?:One|Two|Three))\b", series, re.I)
    if series_match and not title:
        series = series_match.group(1).strip()
        title = series_match.group(2).strip()
    if not title:
        title = Path(path).stem
    summary = info.get("Summary") or ""
    language = (info.get("LanguageISO") or "").lower()
    if language and language != "en":
        return {"error": "manual_inbox_unsupported_archive", "detail": f"non-English ComicInfo language: {language}"}
    target = None
    series_norm = normalize_series(series)
    for candidate in targets:
        if normalize_series(candidate.get("title")) == series_norm or series_norm in candidate.get("aliases", []):
            target = candidate
            break
    if not target:
        return {"error": "manual_inbox_unmonitored_series", "series": series or Path(path).stem}
    covered = collection_range_from_text(series or target.get("title"), title, summary)
    if not covered:
        return {
            "error": "manual_inbox_collection_range_unknown",
            "series": target.get("title"),
            "collection_title": title,
        }
    return {
        "target": target,
        "series": target.get("title"),
        "collection_title": title,
        "year": info.get("Year") or issue_year(info.get("Year"), target.get("year")),
        "range": covered,
        "comicinfo": info,
    }


def collection_dest(target_dir, source, collection):
    series = safe_filename_part(collection.get("series"))
    title = safe_filename_part(collection.get("collection_title"))
    year = str(collection.get("year") or "").strip()
    filename = f"{series} - {title}"
    if year:
        filename += f" ({year})"
    filename += ".cbz"
    return unique_dest_name(target_dir, filename)


def copy_collection_archive(source, dest, collection):
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = Path(source)
    tmp = dest.with_name(f"{dest.stem}.tmp{dest.suffix}")
    try:
        if tmp.exists():
            tmp.unlink()
    except FileNotFoundError:
        pass
    if comic_archive_suffix(source) == ".cbz":
        with zipfile.ZipFile(source) as src, zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as out:
            for info in src.infolist():
                if info.is_dir() or info.filename.lower().endswith("comicinfo.xml"):
                    if info.is_dir():
                        out.writestr(info, b"")
                    continue
                out.writestr(info, src.read(info.filename))
            out.writestr("ComicInfo.xml", normalized_collection_comicinfo(collection))
        tmp.replace(dest)
        return {"normalized_archive": True, "dest_format": "cbz"}
    if source.suffix.lower() == ".cbr":
        repack_cbr_to_cbz(source, dest)
        with zipfile.ZipFile(dest, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("ComicInfo.xml", normalized_collection_comicinfo(collection))
        return {"normalized_archive": True, "dest_format": "cbz", "source_format": "cbr"}
    shutil.copy2(source, dest)
    return {"normalized_archive": False}


def normalized_collection_comicinfo(collection):
    start, end = collection["range"]
    summary = f"Collects {collection.get('series')} #{start}-{end}"
    book_number = collection.get("book_number") or collection.get("volume") or collection.get("collection_number")
    return inkdrop_artifact_acceptance.sanitized_comicinfo_xml(
        [
            ("Series", collection.get("series")),
            ("Title", collection.get("collection_title")),
            ("Number", book_number),
            ("Year", collection.get("year")),
            ("Format", "Collected Edition"),
            ("LanguageISO", "en"),
            ("Summary", summary),
        ]
    )


def prefixed_identity(provider, value):
    provider = str(provider or "").strip().lower()
    value = str(value or "").strip()
    if not provider or not value:
        return ""
    if ":" in value:
        prefix, rest = value.split(":", 1)
        if prefix.strip().lower() == provider and rest.strip():
            return f"{provider}:{rest.strip()}"
    return f"{provider}:{value}"


def split_prefixed_identity(value):
    value = str(value or "").strip()
    if not value or ":" not in value:
        return "", value
    provider, ident = value.split(":", 1)
    provider = provider.strip().lower()
    ident = ident.strip()
    if provider and ident:
        return provider, ident
    return "", value


def _is_kapowarr_adapter_context(payload):
    payload = payload if isinstance(payload, dict) else {}
    metadata_provider = str(
        payload.get("metadata_provider")
        or payload.get("metadataProvider")
        or payload.get("metadata_adapter")
        or payload.get("metadataAdapter")
        or ""
    ).strip().lower()
    if metadata_provider in {"kapowarr", "watch", "manual"}:
        return True

    source = str(
        payload.get("source")
        or payload.get("series_source")
        or payload.get("current_source")
        or payload.get("currentSource")
        or payload.get("watched_source")
        or ""
    ).strip().lower()
    if source in {"kapowarr", "watch", "manual"}:
        return True

    metadata_adapter = str(
        payload.get("metadataAdapter")
        or payload.get("metadata_adapter")
        or payload.get("metadataAdapterRole")
        or payload.get("metadata_adapter_role")
        or payload.get("kapowarrRole")
        or ""
    ).strip()
    if metadata_adapter:
        return True

    adapter_state_source = str(
        payload.get("metadataAdapterStateSource")
        or payload.get("adapter_state_source")
        or ""
    ).strip()
    if adapter_state_source:
        return True

    for key in ("kapowarrId", "kapowarr_id", "matched_kapowarr_id", "series_kapowarr_id"):
        if payload.get(key) not in (None, ""):
            return True
    return False


def completion_native_issue_id(payload):
    payload = payload if isinstance(payload, dict) else {}
    for field in ("native_issue_id", "inkdrop_issue_id", "issue_id", "issueId"):
        value = payload.get(field)
        if value not in (None, ""):
            return str(value)
    provider = str(
        payload.get("issue_metadata_provider")
        or payload.get("issueMetadataProvider")
        or payload.get("metadata_issue_provider")
        or ""
    ).strip().lower()
    metadata_id = (
        payload.get("issue_metadata_id")
        or payload.get("issueMetadataId")
        or payload.get("metadata_issue_id")
    )
    if provider in {"comicvine", "mangadex"} and metadata_id not in (None, ""):
        return prefixed_identity(provider, metadata_id)
    for field, provider in (
        ("comicvine_issue_id", "comicvine"),
        ("mangadex_chapter_id", "mangadex"),
        ("chapterId", "mangadex"),
        ("chapter_id", "mangadex"),
    ):
        value = payload.get(field)
        if value not in (None, ""):
            return prefixed_identity(provider, value)
    return ""


def completion_native_ids(item, result=None):
    payload = {**(item if isinstance(item, dict) else {}), **(result if isinstance(result, dict) else {})}
    native_series_id = completion_native_series_id(payload)
    if not native_series_id:
        metadata = completion_metadata_identity_fields(payload)
        if metadata.get("metadata_provider") in {"comicvine", "mangadex"} and metadata.get("metadata_id") not in (None, ""):
            native_series_id = prefixed_identity(metadata.get("metadata_provider"), metadata.get("metadata_id"))
    return {
        "native_series_id": native_series_id or None,
        "native_issue_id": completion_native_issue_id(payload) or None,
    }


def completion_metadata_identity_fields(item, result=None):
    item = item if isinstance(item, dict) else {}
    result = result if isinstance(result, dict) else {}
    metadata_provider = (
        item.get("metadata_provider")
        or item.get("metadataProvider")
        or item.get("metadata_adapter")
        or item.get("metadataAdapter")
        or item.get("adapter_provider")
        or item.get("adapterProvider")
    )
    metadata_id = (
        item.get("metadata_id")
        or item.get("metadataId")
        or item.get("trusted_series_id")
        or item.get("trustedSeriesId")
        or item.get("comicvine_series_id")
        or item.get("inkdrop_series_id")
    )
    if not metadata_provider:
        metadata_provider = (
            result.get("metadata_provider")
            or result.get("metadataProvider")
            or result.get("metadata_adapter")
            or result.get("metadataAdapter")
        )
    if not metadata_id:
        metadata_id = (
            result.get("metadata_id")
            or result.get("metadataId")
            or result.get("native_series_id")
            or result.get("nativeSeriesId")
        )
    if not metadata_id and not _is_kapowarr_adapter_context({**item, **result}):
        metadata_id = item.get("series_id") or item.get("seriesId") or result.get("series_id") or result.get("seriesId")
    if not metadata_provider or not metadata_id:
        native_series_id = completion_native_series_id({**item, **result})
        provider, ident = split_prefixed_identity(native_series_id)
        metadata_provider = metadata_provider or provider
        metadata_id = metadata_id or ident
    provider_from_id, ident_from_id = split_prefixed_identity(metadata_id)
    if provider_from_id and not metadata_provider:
        metadata_provider = provider_from_id
        metadata_id = ident_from_id
    elif provider_from_id and str(metadata_provider or "").strip().lower() == provider_from_id:
        metadata_id = ident_from_id
    return {
        "metadata_provider": str(metadata_provider or "").strip().lower() or None,
        "metadata_id": str(metadata_id or "").strip() or None,
    }


def ensure_completion_identity_columns(conn, table):
    rows = conn.execute(
        "select name from sqlite_master where type='table' and name=?",
        (table,),
    ).fetchone()
    if not rows:
        return
    existing = {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    needed = {
        "native_series_id": "text",
        "native_issue_id": "text",
        "metadata_provider": "text",
        "metadata_id": "text",
    }
    for name, definition in needed.items():
        if name not in existing:
            conn.execute(f"alter table {table} add column {name} {definition}")


def ensure_manga_completion_schema(conn):
    ensure_completion_identity_columns(conn, "manga_completion")
    conn.execute(
        """
        create table if not exists manga_completion (
          series_title text not null,
          normalized_series text not null,
          native_series_id text,
          native_issue_id text,
          metadata_provider text,
          metadata_id text,
          kapowarr_volume_id integer,
          kapowarr_issue_id integer,
          issue_number text,
          normalized_number text not null,
          truth_model text not null,
          target_file_path text not null,
          source_path text,
          sha256 text,
          comicinfo_status text,
          kavita_visibility_status text not null,
          verification_status text not null,
          review_id text,
          completed_at real not null,
          updated_at real not null,
          primary key (normalized_series, normalized_number, truth_model)
        )
        """
    )
    conn.execute(
        "create index if not exists idx_manga_completion_volume_number on manga_completion (kapowarr_volume_id, normalized_number)"
    )
    conn.execute(
        "create index if not exists idx_manga_completion_native on manga_completion (native_series_id, native_issue_id)"
    )
    conn.execute(
        "create index if not exists idx_manga_completion_metadata on manga_completion (metadata_provider, metadata_id)"
    )
    conn.execute(
        "create index if not exists idx_manga_completion_series_number on manga_completion (normalized_series, normalized_number)"
    )
    conn.execute(
        "create index if not exists idx_manga_completion_status on manga_completion (verification_status)"
    )


def ensure_collection_completion_schema(conn):
    ensure_completion_identity_columns(conn, "collection_completion")
    conn.execute(
        """
        create table if not exists collection_completion (
          series_title text not null,
          normalized_series text not null,
          native_series_id text,
          native_issue_id text,
          metadata_provider text,
          metadata_id text,
          kapowarr_volume_id integer,
          kapowarr_issue_id integer,
          issue_number text,
          normalized_number text not null,
          truth_model text not null,
          collection_title text not null,
          collection_range text not null,
          target_file_path text not null,
          source_path text,
          sha256 text,
          comicinfo_status text,
          kavita_visibility_status text not null,
          verification_status text not null,
          review_id text,
          completed_at real not null,
          updated_at real not null,
          primary key (normalized_series, normalized_number, truth_model)
        )
        """
    )
    conn.execute(
        "create index if not exists idx_collection_completion_volume_number on collection_completion (kapowarr_volume_id, normalized_number)"
    )
    conn.execute(
        "create index if not exists idx_collection_completion_native on collection_completion (native_series_id, native_issue_id)"
    )
    conn.execute(
        "create index if not exists idx_collection_completion_metadata on collection_completion (metadata_provider, metadata_id)"
    )
    conn.execute(
        "create index if not exists idx_collection_completion_series_number on collection_completion (normalized_series, normalized_number)"
    )
    conn.execute(
        "create index if not exists idx_collection_completion_status on collection_completion (verification_status)"
    )


def ensure_manga_unit_schema(conn):
    ensure_completion_identity_columns(conn, "manga_unit_completion")
    conn.execute(
        """
        create table if not exists manga_series_unit_model (
          normalized_series text primary key,
          series_title text not null,
          kapowarr_volume_id integer,
          manga_unit_model text not null,
          source text,
          updated_at real not null
        )
        """
    )
    conn.execute(
        "create index if not exists idx_manga_series_unit_model_volume on manga_series_unit_model (kapowarr_volume_id)"
    )
    conn.execute(
        """
        create table if not exists manga_unit_completion (
          series_title text not null,
          normalized_series text not null,
          native_series_id text,
          native_issue_id text,
          metadata_provider text,
          metadata_id text,
          kapowarr_volume_id integer,
          kapowarr_issue_id integer,
          issue_number text,
          normalized_number text not null,
          manga_unit_model text not null,
          truth_model text not null,
          target_file_path text not null,
          source_path text,
          sha256 text,
          comicinfo_status text,
          kavita_visibility_status text not null,
          verification_status text not null,
          review_id text,
          completed_at real not null,
          updated_at real not null,
          primary key (normalized_series, normalized_number, manga_unit_model, truth_model)
        )
        """
    )
    conn.execute(
        "create index if not exists idx_manga_unit_completion_volume_number on manga_unit_completion (kapowarr_volume_id, normalized_number, manga_unit_model)"
    )
    conn.execute(
        "create index if not exists idx_manga_unit_completion_native on manga_unit_completion (native_series_id, native_issue_id)"
    )
    conn.execute(
        "create index if not exists idx_manga_unit_completion_metadata on manga_unit_completion (metadata_provider, metadata_id)"
    )
    conn.execute(
        "create index if not exists idx_manga_unit_completion_series_number on manga_unit_completion (normalized_series, normalized_number, manga_unit_model)"
    )
    conn.execute(
        "create index if not exists idx_manga_unit_completion_status on manga_unit_completion (verification_status)"
    )
    ensure_manga_coverage_schema(conn)


def ensure_manga_coverage_range_columns(conn):
    existing = {row[1] for row in conn.execute("pragma table_info(manga_coverage)").fetchall()}
    needed = {
        "covered_chapter_numbers_json": "text",
        "range_source": "text",
    }
    for name, definition in needed.items():
        if name not in existing:
            conn.execute(f"alter table manga_coverage add column {name} {definition}")


def ensure_manga_coverage_schema(conn):
    ensure_completion_identity_columns(conn, "manga_coverage")
    conn.execute(
        """
        create table if not exists manga_coverage (
          series_title text not null,
          normalized_series text not null,
          native_series_id text,
          native_issue_id text,
          metadata_provider text,
          metadata_id text,
          kapowarr_volume_id integer,
          kapowarr_issue_id integer,
          unit_type text not null,
          issue_number text,
          normalized_number text not null,
          covers_from text,
          covers_to text,
          source_quality text not null,
          truth_model text not null,
          target_file_path text not null,
          source_path text,
          sha256 text,
          comicinfo_status text,
          kavita_visibility_status text not null,
          verification_status text not null,
          replacement_status text,
          review_id text,
          completed_at real not null,
          updated_at real not null,
          primary key (normalized_series, unit_type, normalized_number, truth_model)
        )
        """
    )
    ensure_manga_coverage_range_columns(conn)
    conn.execute(
        "create index if not exists idx_manga_coverage_volume_number on manga_coverage (kapowarr_volume_id, unit_type, normalized_number)"
    )
    conn.execute(
        "create index if not exists idx_manga_coverage_native on manga_coverage (native_series_id, native_issue_id)"
    )
    conn.execute(
        "create index if not exists idx_manga_coverage_metadata on manga_coverage (metadata_provider, metadata_id)"
    )
    conn.execute(
        "create index if not exists idx_manga_coverage_series_number on manga_coverage (normalized_series, unit_type, normalized_number)"
    )
    conn.execute(
        "create index if not exists idx_manga_coverage_status on manga_coverage (verification_status)"
    )


def set_manga_unit_model(series_title, unit_model, source="importer", kapowarr_volume_id=None):
    unit_model = normalize_manga_unit_model(unit_model)
    if unit_model not in MANGA_UNIT_MODELS:
        raise ValueError(f"unsupported manga unit model: {unit_model}")
    normalized = normalize_series(series_title)
    if not normalized:
        raise ValueError("series title is required for manga unit model")
    now = time.time()
    conn = connect()
    try:
        ensure_manga_unit_schema(conn)
        conn.execute(
            """
            insert into manga_series_unit_model (
              normalized_series, series_title, kapowarr_volume_id, manga_unit_model, source, updated_at
            ) values (?,?,?,?,?,?)
            on conflict(normalized_series) do update set
              series_title=excluded.series_title,
              kapowarr_volume_id=coalesce(excluded.kapowarr_volume_id, manga_series_unit_model.kapowarr_volume_id),
              manga_unit_model=excluded.manga_unit_model,
              source=excluded.source,
              updated_at=excluded.updated_at
            """,
            (normalized, series_title, kapowarr_volume_id, unit_model, source, now),
        )
        conn.commit()
    finally:
        conn.close()
    policy = manga_unit_policy_for_model(unit_model)
    return {
        "series": series_title,
        "normalized_series": normalized,
        "manga_unit_model": unit_model,
        "manga_unit_policy": policy,
        "manga_unit_policy_label": MANGA_UNIT_POLICY_LABELS.get(unit_model) or MANGA_UNIT_POLICY_LABELS.get(policy, policy.replace("_", " ").title()),
    }


def manga_unit_model_for_target(target):
    if not target:
        return "unknown/manual"
    normalized = normalize_series(target.get("title"))
    if not normalized:
        return "unknown/manual"
    conn = connect()
    try:
        ensure_manga_unit_schema(conn)
        row = conn.execute(
            """
            select manga_unit_model from manga_series_unit_model
            where normalized_series = ?
               or (kapowarr_volume_id is not null and kapowarr_volume_id = ?)
            order by case when normalized_series = ? then 0 else 1 end
            limit 1
            """,
            (normalized, target.get("id"), normalized),
        ).fetchone()
        return normalize_manga_unit_model(row[0] if row else "unknown/manual")
    finally:
        conn.close()


def completion_numbers_from_table(
    table,
    series_title=None,
    kapowarr_volume_id=None,
    truth_model=None,
    native_series_id=None,
    require_number_match=True,
):
    if not DB_PATH.exists():
        return set()
    conn = sqlite_connect(DB_PATH)
    try:
        if table == "manga_completion":
            ensure_manga_completion_schema(conn)
        elif table == "collection_completion":
            ensure_collection_completion_schema(conn)
        filters = ["verification_status in ('folder_verified', 'library_visible', 'kavita_verified')"]
        params = []
        if truth_model:
            filters.append("truth_model = ?")
            params.append(truth_model)
        if native_series_id is not None:
            filters.append("native_series_id = ?")
            params.append(native_series_id)
        elif kapowarr_volume_id is not None:
            filters.append("kapowarr_volume_id = ?")
            params.append(int(kapowarr_volume_id))
        elif series_title:
            filters.append("normalized_series = ?")
            params.append(normalize_series(series_title))
        rows = conn.execute(
            f"select normalized_number, target_file_path from {table} where {' and '.join(filters)}",
            params,
        ).fetchall()
        return {
            row[0]
            for row in rows
            if completion_target_matches_number(row[1], row[0], require_number_match=require_number_match)
        }
    finally:
        conn.close()


def manga_completed_numbers(series_title=None, kapowarr_volume_id=None, native_series_id=None):
    return completion_numbers_from_table(
        "manga_completion", series_title, kapowarr_volume_id, "kavita_manga", native_series_id
    )


def manga_unit_completed_numbers(series_title=None, kapowarr_volume_id=None, unit_model=None, native_series_id=None):
    if not DB_PATH.exists():
        return set()
    conn = sqlite_connect(DB_PATH)
    try:
        ensure_manga_unit_schema(conn)
        filters = ["truth_model = 'kavita_manga'", "verification_status in ('folder_verified', 'library_visible', 'kavita_verified')"]
        params = []
        if unit_model:
            filters.append("manga_unit_model = ?")
            params.append(unit_model)
        if native_series_id is not None:
            filters.append("native_series_id = ?")
            params.append(str(native_series_id))
        elif kapowarr_volume_id is not None:
            filters.append("kapowarr_volume_id = ?")
            params.append(int(kapowarr_volume_id))
        elif series_title:
            filters.append("normalized_series = ?")
            params.append(normalize_series(series_title))
        rows = conn.execute(
            f"select normalized_number, target_file_path from manga_unit_completion where {' and '.join(filters)}",
            params,
        ).fetchall()
        return {row[0] for row in rows if completion_target_matches_number(row[1], row[0])}
    finally:
        conn.close()


def manga_unit_is_completed(series_title, number, unit_model, kapowarr_volume_id=None, native_series_id=None):
    normalized = normalize_manga_number(number)
    if not normalized or unit_model not in MANGA_UNIT_MODELS:
        return False
    return normalized in manga_unit_completed_numbers(
        series_title, kapowarr_volume_id=kapowarr_volume_id, unit_model=unit_model, native_series_id=native_series_id
    )


def completion_target_exists(path_value):
    if not path_value:
        return False
    try:
        return Path(path_value).exists()
    except (OSError, TypeError, ValueError):
        return False


COMPLETION_TARGET_NUMBER_PATTERNS = (
    re.compile(r"(?:^|[\s._-])(?:v|vol|volume)[\s._-]*0*(\d{1,5}(?:\.\d+)?)\b", re.I),
    re.compile(r"(?:^|[\s._-])#\s*0*(\d{1,5}(?:\.\d+)?)\b", re.I),
    re.compile(r"(?:^|[\s._-])(?:issue|chapter|chap|ch|c)[\s._-]*0*(\d{1,5}(?:\.\d+)?)\b", re.I),
)


def completion_target_explicit_number(path_value):
    text = Path(str(path_value or "")).stem
    for pattern in COMPLETION_TARGET_NUMBER_PATTERNS:
        match = pattern.search(text)
        if match:
            return normalize_manga_number(match.group(1))
    return None


def completion_target_stale_reason(path_value, expected_number=None, require_number_match=True):
    if not completion_target_exists(path_value):
        return "target_missing"
    if not require_number_match:
        return None
    expected = normalize_manga_number(expected_number)
    explicit = completion_target_explicit_number(path_value)
    if expected and explicit and expected != explicit:
        return "target_number_mismatch"
    return None


def completion_target_matches_number(path_value, expected_number, require_number_match=True):
    return completion_target_stale_reason(
        path_value,
        expected_number,
        require_number_match=require_number_match,
    ) is None


def retract_stale_completion_rows(limit=5000, now=None):
    """Downgrade verified completion rows whose managed target proof is stale."""
    if not DB_PATH.exists():
        return {"checked": 0, "retracted": 0, "skipped": {"db_missing": 1}, "tables": {}}
    try:
        limit = max(1, min(int(limit or 5000), 20000))
    except (TypeError, ValueError):
        limit = 5000
    now = float(now or time.time())
    specs = (
        ("manga_completion", True),
        ("manga_unit_completion", True),
        ("manga_coverage", True),
        ("collection_completion", False),
    )
    verified_statuses = ("folder_verified", "library_visible", "kavita_verified")
    conn = connect()
    conn.row_factory = sqlite3.Row
    checked = 0
    retracted = 0
    tables = {}
    try:
        for table, require_number_match in specs:
            if checked >= limit:
                break
            remaining = limit - checked
            table_summary = {"checked": 0, "retracted": 0, "reasons": {}}
            rows = conn.execute(
                f"""
                select rowid, target_file_path, normalized_number, verification_status,
                       kavita_visibility_status
                from {table}
                where verification_status in ({",".join("?" for _ in verified_statuses)})
                order by coalesce(updated_at, 0) asc, rowid asc
                limit ?
                """,
                (*verified_statuses, remaining),
            ).fetchall()
            for row in rows:
                checked += 1
                table_summary["checked"] += 1
                reason = completion_target_stale_reason(
                    row["target_file_path"],
                    row["normalized_number"],
                    require_number_match=require_number_match,
                )
                if not reason:
                    continue
                status = f"stale_{reason}"
                update_sql = f"""
                    update {table}
                       set verification_status=?,
                           kavita_visibility_status=?,
                           updated_at=?
                     where rowid=?
                """
                params = [status, status, now, row["rowid"]]
                if table == "manga_coverage":
                    update_sql = f"""
                        update {table}
                           set verification_status=?,
                               kavita_visibility_status=?,
                               replacement_status=?,
                               updated_at=?
                         where rowid=?
                    """
                    params = [status, status, status, now, row["rowid"]]
                conn.execute(update_sql, params)
                changed = int(conn.execute("select changes()").fetchone()[0] or 0)
                retracted += changed
                table_summary["retracted"] += changed
                table_summary["reasons"][status] = int(table_summary["reasons"].get(status) or 0) + changed
            if table_summary["checked"] or table_summary["retracted"]:
                tables[table] = table_summary
        conn.commit()
        return {
            "ok": True,
            "checked": checked,
            "retracted": retracted,
            "tables": tables,
            "updated_at": now,
        }
    finally:
        conn.close()


def manga_unit_completion_has_existing_target(
    series_title, number, unit_model, kapowarr_volume_id=None, native_series_id=None
):
    return manga_unit_completion_existing_target_path(
        series_title,
        number,
        unit_model,
        kapowarr_volume_id=kapowarr_volume_id,
        native_series_id=native_series_id,
    ) is not None


def manga_unit_completion_existing_target_path(
    series_title, number, unit_model, kapowarr_volume_id=None, native_series_id=None
):
    normalized = normalize_manga_number(number)
    if not normalized or unit_model not in MANGA_UNIT_MODELS or not DB_PATH.exists():
        return None
    conn = sqlite_connect(DB_PATH)
    try:
        ensure_manga_unit_schema(conn)
        filters = [
            "truth_model = 'kavita_manga'",
            "verification_status in ('folder_verified', 'library_visible', 'kavita_verified')",
            "normalized_number = ?",
            "manga_unit_model = ?",
        ]
        params = [normalized, unit_model]
        if native_series_id is not None:
            filters.append("native_series_id = ?")
            params.append(str(native_series_id))
        elif kapowarr_volume_id is not None:
            filters.append("kapowarr_volume_id = ?")
            params.append(int(kapowarr_volume_id))
        elif series_title:
            filters.append("normalized_series = ?")
            params.append(normalize_series(series_title))
        rows = conn.execute(
            f"select target_file_path from manga_unit_completion where {' and '.join(filters)}",
            params,
        ).fetchall()
        for row in rows:
            candidate = durable_managed_manga_target_path(row[0], normalized)
            if candidate is not None:
                return candidate
        return None
    finally:
        conn.close()


def manga_policy_allows_chapter(model):
    return (model or "unknown/manual") in MANGA_CHAPTER_ALLOWED_MODELS


def manga_policy_allows_volume(model):
    return (model or "unknown/manual") in MANGA_VOLUME_ALLOWED_MODELS


def manga_policy_prefers_volume(model):
    return (model or "unknown/manual") in {"volume", "pack", "mixed_volume_preferred"}


def manga_coverage_numbers(series_title=None, kapowarr_volume_id=None, unit_type=None, native_series_id=None):
    if not DB_PATH.exists():
        return set()
    conn = sqlite_connect(DB_PATH)
    try:
        ensure_manga_coverage_schema(conn)
        filters = ["verification_status in ('folder_verified', 'library_visible', 'kavita_verified')"]
        params = []
        if unit_type:
            filters.append("unit_type = ?")
            params.append(unit_type)
        if native_series_id is not None:
            filters.append("native_series_id = ?")
            params.append(str(native_series_id))
        elif kapowarr_volume_id is not None:
            filters.append("kapowarr_volume_id = ?")
            params.append(int(kapowarr_volume_id))
        elif series_title:
            filters.append("normalized_series = ?")
            params.append(normalize_series(series_title))
        rows = conn.execute(
            f"select normalized_number, target_file_path from manga_coverage where {' and '.join(filters)}",
            params,
        ).fetchall()
        return {row[0] for row in rows if row[0] and completion_target_matches_number(row[1], row[0])}
    finally:
        conn.close()


def manga_coverage_is_completed(series_title, number, unit_type, kapowarr_volume_id=None, native_series_id=None):
    normalized = normalize_manga_number(number)
    if not normalized:
        return False
    return normalized in manga_coverage_numbers(
        series_title, kapowarr_volume_id=kapowarr_volume_id, unit_type=unit_type, native_series_id=native_series_id
    )


def manga_coverage_has_existing_target(
    series_title, number, unit_type, kapowarr_volume_id=None, native_series_id=None
):
    return manga_coverage_existing_target_path(
        series_title,
        number,
        unit_type,
        kapowarr_volume_id=kapowarr_volume_id,
        native_series_id=native_series_id,
    ) is not None


def durable_managed_manga_target_path(path_value, expected_number):
    if not path_value:
        return None
    try:
        candidate = Path(path_value)
        resolved = candidate.resolve()
    except (OSError, TypeError, ValueError):
        return None
    if not candidate.is_file() or candidate.suffix.lower() not in {".cbz", ".cbr", ".pdf"}:
        return None
    managed = False
    for root in (COMIC_ROOT, MANGA_ROOT):
        try:
            resolved.relative_to(Path(root).resolve())
            managed = True
            break
        except (OSError, TypeError, ValueError):
            continue
    if not managed or not completion_target_matches_number(candidate, expected_number):
        return None
    try:
        archive_check = validate_comic_archive(candidate, min_pages=1, min_payload_bytes=1)
    except (OSError, RuntimeError, ValueError):
        return None
    if not isinstance(archive_check, dict) or not archive_check.get("ok"):
        return None
    return candidate


def manga_coverage_existing_target_path(
    series_title, number, unit_type, kapowarr_volume_id=None, native_series_id=None
):
    normalized = normalize_manga_number(number)
    if not normalized or not unit_type or not DB_PATH.exists():
        return None
    conn = sqlite_connect(DB_PATH)
    try:
        ensure_manga_coverage_schema(conn)
        filters = [
            "verification_status in ('folder_verified', 'library_visible', 'kavita_verified')",
            "normalized_number = ?",
            "unit_type = ?",
        ]
        params = [normalized, unit_type]
        if native_series_id is not None:
            filters.append("native_series_id = ?")
            params.append(str(native_series_id))
        elif kapowarr_volume_id is not None:
            filters.append("kapowarr_volume_id = ?")
            params.append(int(kapowarr_volume_id))
        elif series_title:
            filters.append("normalized_series = ?")
            params.append(normalize_series(series_title))
        rows = conn.execute(
            f"select target_file_path from manga_coverage where {' and '.join(filters)}",
            params,
        ).fetchall()
        for row in rows:
            candidate = durable_managed_manga_target_path(row[0], normalized)
            if candidate is not None:
                return candidate
        return None
    finally:
        conn.close()


def manga_chapters_satisfied_by_volumes(series_title=None, kapowarr_volume_id=None, native_series_id=None):
    """Owned/verified CHAPTER rows for a series whose exact chapter number is
    provably contained in an owned/verified VOLUME (or PACK) row, per real
    metadata (currently MangaDex aggregate data) recorded on the volume row --
    never a filename guess or a from/to range with assumed contiguity.

    Read-only. Callers decide what to do with the result (log-only vs. delete);
    this function never touches the filesystem or the database.

    Returns a list of dicts, one per satisfied chapter:
      chapter_number, chapter_target_file_path,
      satisfied_by_volume_number, satisfied_by_volume_path, range_source
    """
    if not DB_PATH.exists():
        return []
    filters = ["verification_status in ('folder_verified', 'library_visible', 'kavita_verified')"]
    params = []
    if native_series_id is not None:
        filters.append("native_series_id = ?")
        params.append(str(native_series_id))
    elif kapowarr_volume_id is not None:
        filters.append("kapowarr_volume_id = ?")
        params.append(int(kapowarr_volume_id))
    elif series_title:
        filters.append("normalized_series = ?")
        params.append(normalize_series(series_title))
    else:
        return []
    where = " and ".join(filters)
    conn = sqlite_connect(DB_PATH)
    try:
        ensure_manga_coverage_schema(conn)
        chapter_rows = conn.execute(
            f"select normalized_number, target_file_path from manga_coverage where unit_type = 'chapter' and {where}",
            params,
        ).fetchall()
        if not chapter_rows:
            return []
        volume_rows = conn.execute(
            f"""
            select normalized_number, target_file_path, covered_chapter_numbers_json, range_source
            from manga_coverage
            where unit_type in ('volume', 'pack') and covered_chapter_numbers_json is not null and {where}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    volumes = []
    for vol_number, vol_path, covered_json, range_source in volume_rows:
        if not range_source:
            continue
        try:
            covered = {str(number) for number in json.loads(covered_json)}
        except (TypeError, ValueError):
            continue
        if covered:
            volumes.append((vol_number, vol_path, covered, range_source))
    if not volumes:
        return []
    satisfied = []
    for chapter_number, chapter_path in chapter_rows:
        candidate = durable_managed_manga_target_path(chapter_path, chapter_number)
        if candidate is None:
            continue
        for vol_number, vol_path, covered, range_source in volumes:
            if chapter_number in covered:
                satisfied.append(
                    {
                        "chapter_number": chapter_number,
                        "chapter_target_file_path": str(candidate),
                        "chapter_db_target_file_path": chapter_path,
                        "satisfied_by_volume_number": vol_number,
                        "satisfied_by_volume_path": vol_path,
                        "range_source": range_source,
                    }
                )
                break
    return satisfied


CHAPTER_VOLUME_REDUNDANCY_QUARANTINE_ROOT = Path(
    os.environ.get("INKDROP_CHAPTER_VOLUME_REDUNDANCY_QUARANTINE_ROOT") or str(QUARANTINE_DIR / "chapters-satisfied-by-volume")
)


def remove_chapters_satisfied_by_volume_enabled():
    return bool_setting(
        {"enabled": app_setting_value("media_management.remove_chapters_satisfied_by_volume", False)},
        "enabled",
        False,
    )


def mark_chapter_replacement_candidate(chapter_target_file_path):
    """Give the long-idle manga_coverage.replacement_status='replacement_candidate'
    write path a real writer, so any existing reporting keyed on that literal
    (e.g. the replacement_candidates stat) reflects reality whether or not the
    file is actually removed.
    """
    conn = connect()
    try:
        ensure_manga_coverage_schema(conn)
        conn.execute(
            """
            update manga_coverage
            set replacement_status = 'replacement_candidate', updated_at = ?
            where unit_type = 'chapter' and target_file_path = ?
            """,
            (time.time(), str(chapter_target_file_path)),
        )
        conn.commit()
    finally:
        conn.close()


def quarantine_redundant_chapter(item, quarantine_root=None, dry_run=True):
    """Move (never delete outright) a chapter file confirmed redundant by
    manga_chapters_satisfied_by_volumes. Mirrors quarantine_pack_duplicate's
    move-to-timestamped-folder shape so the action stays reversible.
    """
    chapter_path = Path(item.get("chapter_target_file_path") or "")
    quarantine_root = Path(quarantine_root or CHAPTER_VOLUME_REDUNDANCY_QUARANTINE_ROOT)
    relative = relative_quarantine_path(chapter_path)
    quarantine_dir = quarantine_root / time.strftime("%Y%m%d-%H%M%S") / relative.parent
    quarantine_dest = unique_dest_name(quarantine_dir, chapter_path.name)
    item["quarantine_dest"] = str(quarantine_dest)
    if dry_run:
        item["action"] = "would_remove"
        return item
    if not chapter_path.is_file():
        item["action"] = "skipped_missing"
        return item
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(chapter_path), str(quarantine_dest))
    item["action"] = "removed"
    item["chapter_exists_after"] = chapter_path.exists()
    if app_setting_value("media_management.delete_empty_folders", False):
        library_root = inkdrop_folder_cleanup.containing_root(chapter_path, (COMIC_ROOT, MANGA_ROOT))
        if library_root is not None:
            sweep = inkdrop_folder_cleanup.remove_empty_parents(chapter_path, library_root)
            if sweep.get("removed"):
                item["removed_empty_folders"] = sweep["removed"]
    return item


def sweep_chapters_satisfied_by_volumes(series_title=None, kapowarr_volume_id=None, native_series_id=None, dry_run=None):
    """Find chapters provably satisfied by an owned volume for one series and act
    on them. Always logs and flags every satisfied chapter as a replacement
    candidate. Only actually removes files when
    media_management.remove_chapters_satisfied_by_volume is enabled -- default is
    dry-run (keep everything, just report what would change). A caller may pass
    dry_run explicitly (tests do this) to bypass the live setting.
    """
    satisfied = manga_chapters_satisfied_by_volumes(
        series_title=series_title, kapowarr_volume_id=kapowarr_volume_id, native_series_id=native_series_id
    )
    if dry_run is None:
        dry_run = not remove_chapters_satisfied_by_volume_enabled()
    results = []
    for entry in satisfied:
        mark_chapter_replacement_candidate(entry.get("chapter_db_target_file_path") or entry["chapter_target_file_path"])
        outcome = quarantine_redundant_chapter(dict(entry), dry_run=dry_run)
        try:
            log({"event": "manga_chapter_satisfied_by_volume", "dry_run": dry_run, **outcome})
        except Exception:
            pass
        results.append(outcome)
    return {"dry_run": dry_run, "satisfied_count": len(satisfied), "items": results}


def collection_completed_numbers(series_title=None, kapowarr_volume_id=None, native_series_id=None):
    return completion_numbers_from_table(
        "collection_completion",
        series_title,
        kapowarr_volume_id,
        COLLECTION_TRUTH_MODEL,
        native_series_id,
        require_number_match=False,
    )


def collection_range_is_completed(collection, target):
    if not collection or not target:
        return False
    covered = collection.get("range") or ()
    if len(covered) != 2:
        return False
    start, end = covered
    completed = collection_completed_numbers(
        collection.get("series") or target.get("title"),
        target.get("id"),
        completion_native_series_id(target),
    )
    expected = {normalize_manga_number(value) for value in range(int(start), int(end) + 1)}
    expected.discard(None)
    return bool(expected) and expected.issubset(completed)


def completion_native_series_id(payload):
    if not payload:
        return None
    provider = str(payload.get("metadata_provider") or payload.get("metadataProvider") or "").strip().lower()
    metadata_id = payload.get("metadata_id") or payload.get("metadataId")
    candidates = (
        payload.get("native_series_id"),
        payload.get("inkdrop_series_id"),
        payload.get("comicvine_series_id"),
    )
    for value in candidates:
        if value not in (None, ""):
            return str(value)

    if provider in {"comicvine", "mangadex"} and metadata_id not in (None, ""):
        return f"{provider}:{metadata_id}"
    if payload.get("comicvine_id") not in (None, ""):
        return f"comicvine:{payload.get('comicvine_id')}"
    if payload.get("mangadex_id") not in (None, ""):
        return f"mangadex:{payload.get('mangadex_id')}"
    if payload.get("mangadexId") not in (None, ""):
        return f"mangadex:{payload.get('mangadexId')}"

    if _is_kapowarr_adapter_context(payload):
        return None

    if payload.get("series_id") not in (None, ""):
        return str(payload.get("series_id"))
    if payload.get("seriesId") not in (None, ""):
        return str(payload.get("seriesId"))
    return None


def completion_native_identity_candidates(payload):
    payload = payload if isinstance(payload, dict) else {}
    values = []

    def add(value):
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    has_adapter_context = _is_kapowarr_adapter_context(payload)

    for key in ("native_series_id", "inkdrop_series_id", "comicvine_series_id"):
        add(payload.get(key))
    if not has_adapter_context:
        for key in ("series_id", "seriesId"):
            add(payload.get(key))
    provider = str(payload.get("metadata_provider") or payload.get("metadataProvider") or "").strip().lower()
    metadata_id = payload.get("metadata_id") or payload.get("metadataId")
    if provider in {"comicvine", "mangadex"} and metadata_id not in (None, ""):
        add(f"{provider}:{metadata_id}")
    if payload.get("comicvine_id") not in (None, ""):
        add(f"comicvine:{payload.get('comicvine_id')}")
    if payload.get("mangadex_id") not in (None, ""):
        add(f"mangadex:{payload.get('mangadex_id')}")
    if payload.get("mangadexId") not in (None, ""):
        add(f"mangadex:{payload.get('mangadexId')}")
    return values


def manga_completion_stats():
    if not DB_PATH.exists():
        return {"completed": 0, "timeouts": 0, "waiting": 0, "failures": 0}
    conn = sqlite_connect(DB_PATH)
    try:
        ensure_manga_completion_schema(conn)
        rows = conn.execute(
            "select verification_status, count(*) from manga_completion group by verification_status"
        ).fetchall()
    finally:
        conn.close()
    counts = {row[0]: int(row[1] or 0) for row in rows}
    return {
        "completed": sum(counts.get(status, 0) for status in ("folder_verified", "library_visible", "kavita_verified")),
        "folder_completed": counts.get("folder_verified", 0),
        "library_completed": counts.get("library_visible", 0) + counts.get("kavita_verified", 0),
        "timeouts": counts.get("library_scan_timeout", 0) + counts.get("kavita_scan_timeout", 0),
        "waiting": counts.get("waiting_for_library_scan", 0) + counts.get("waiting_for_kavita_scan", 0),
        "failures": counts.get("verification_failed", 0),
    }


def manga_unit_completion_stats():
    if not DB_PATH.exists():
        return {"completed": 0, "timeouts": 0, "waiting": 0, "failures": 0, "chapter_completed": 0}
    conn = sqlite_connect(DB_PATH)
    try:
        ensure_manga_unit_schema(conn)
        rows = conn.execute(
            "select manga_unit_model, verification_status, count(*) from manga_unit_completion group by manga_unit_model, verification_status"
        ).fetchall()
    finally:
        conn.close()
    out = {"completed": 0, "timeouts": 0, "waiting": 0, "failures": 0, "chapter_completed": 0}
    for unit_model, status, count in rows:
        count = int(count or 0)
        if status in {"folder_verified", "library_visible", "kavita_verified"}:
            out["completed"] += count
            if unit_model == "chapter":
                out["chapter_completed"] += count
        elif status in {"library_scan_timeout", "kavita_scan_timeout"}:
            out["timeouts"] += count
        elif status in {"waiting_for_library_scan", "waiting_for_kavita_scan"}:
            out["waiting"] += count
        elif status == "verification_failed":
            out["failures"] += count
    return out


def collection_completion_stats():
    if not DB_PATH.exists():
        return {"completed": 0, "timeouts": 0, "waiting": 0, "failures": 0}
    conn = sqlite_connect(DB_PATH)
    try:
        ensure_collection_completion_schema(conn)
        rows = conn.execute(
            "select verification_status, count(*) from collection_completion group by verification_status"
        ).fetchall()
    finally:
        conn.close()
    counts = {row[0]: int(row[1] or 0) for row in rows}
    return {
        "completed": sum(counts.get(status, 0) for status in ("folder_verified", "library_visible", "kavita_verified")),
        "folder_completed": counts.get("folder_verified", 0),
        "library_completed": counts.get("library_visible", 0) + counts.get("kavita_verified", 0),
        "timeouts": counts.get("library_scan_timeout", 0) + counts.get("kavita_scan_timeout", 0),
        "waiting": counts.get("waiting_for_library_scan", 0) + counts.get("waiting_for_kavita_scan", 0),
        "failures": counts.get("verification_failed", 0),
    }


def log(event):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**event, "ts": time.time()}, sort_keys=True) + "\n")


def sync_inkdrop_import_results():
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    try:
        summary = inkdrop_state.sync_import_results(STATE_DIR, INKDROP_STATE_DB)
        try:
            pack_summary = reverify_inkdrop_pack_imports()
        except Exception as exc:
            pack_summary = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            log({"event": "inkdrop_pack_import_reverify_failed", **pack_summary})
        if isinstance(summary, dict):
            summary.setdefault("synced", {})["pack_import_reverify"] = pack_summary
        return summary
    except Exception as exc:
        log({"event": "inkdrop_state_import_sync_failed", "error": f"{type(exc).__name__}: {exc}"})
        return {"ok": False, "error": str(exc)}


def pack_import_event_dest(row):
    if not isinstance(row, dict):
        return ""
    return str(row.get("dest") or row.get("existing_path") or row.get("destination") or row.get("last_import_dest") or "")


def pack_import_event_kind(row):
    event = str((row or {}).get("event") or "")
    if event == "pack_import_file":
        return "imported"
    if event.startswith("pack_skip") and event != "pack_skip_bad_archive":
        return "skipped_existing"
    return ""


def pack_import_row_key(row, kind):
    return (
        str((row or {}).get("review_id") or ""),
        str(kind or ""),
        pack_import_event_dest(row).replace("\\", "/").rstrip("/").lower(),
        str((row or {}).get("source") or ""),
    )


def add_pack_import_candidate(groups, row, kind=None, source_hint=None, max_rows=300):
    if not isinstance(row, dict):
        return 0
    kind = kind or pack_import_event_kind(row)
    if kind not in {"imported", "skipped_existing"}:
        return 0
    review_id = str(row.get("review_id") or "").strip()
    if not review_id:
        return 0
    dest = pack_import_event_dest(row)
    if not dest:
        return 0
    group = groups.setdefault(
        review_id,
        {
            "review_id": review_id,
            "series": row.get("matched_series") or row.get("series"),
            "title": row.get("title") or row.get("candidate_title") or row.get("matched_series") or row.get("series"),
            "pack_path": "",
            "imported": {},
            "skipped_existing": {},
            "sources": set(),
        },
    )
    group["series"] = group.get("series") or row.get("matched_series") or row.get("series")
    group["title"] = group.get("title") or row.get("title") or row.get("matched_series") or row.get("series")
    source = str(row.get("source") or "").strip()
    if source and not group.get("pack_path"):
        parent = str(Path(source).parent)
        if parent and parent != ".":
            group["pack_path"] = parent
    if source_hint:
        group["sources"].add(source_hint)
    key = pack_import_row_key(row, kind)
    group[kind][key] = row
    total = sum(len(item["imported"]) + len(item["skipped_existing"]) for item in groups.values())
    return 1 if total <= max_rows else 0


def pending_native_pack_import_candidates(groups, max_rows=300):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return 0
    conn = sqlite_connect(INKDROP_STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select raw_json
            from source_attempts sa
            where sa.source like 'pack_import%'
              and lower(coalesce(sa.status, '')) in (
                'verification_pending',
                'waiting_for_kavita_scan',
                'kavita_scan_timeout',
                'verification_failed'
              )
              and sa.queue_id is not null
              and not exists (
                select 1
                from import_results ir
                where ir.queue_id = sa.queue_id and ir.verified = 1
              )
            order by coalesce(sa.completed_at, sa.started_at, 0) desc, sa.id desc
            limit ?
            """,
            (max_rows,),
        ).fetchall()
    finally:
        conn.close()
    added = 0
    for record in rows:
        try:
            attempt = json.loads(record["raw_json"] or "{}")
        except ValueError:
            attempt = {}
        raw_event = attempt.get("raw_event") if isinstance(attempt.get("raw_event"), dict) else attempt
        raw_event = dict(raw_event or {})
        raw_event.setdefault("review_id", attempt.get("review_id"))
        raw_event.setdefault("source", attempt.get("source_path") or attempt.get("filename"))
        raw_event.setdefault("dest", attempt.get("dest"))
        added += add_pack_import_candidate(groups, raw_event, source_hint="native_pending", max_rows=max_rows)
    return added


def pending_import_result_pack_candidates(groups, max_rows=300):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return 0
    conn = sqlite_connect(INKDROP_STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select ir.id, ir.queue_id, ir.series_id, ir.issue_id, ir.source_path, ir.dest_path,
                   ir.status, ir.raw_json,
                   s.title as series_title, s.metadata_provider, s.metadata_id,
                   i.issue_number, i.normalized_number
            from import_results ir
            left join series s on s.id = ir.series_id
            left join issues i on i.id = ir.issue_id
            where ir.queue_id is not null
              and coalesce(ir.verified, 0) = 0
              and lower(coalesce(ir.status, '')) in (
                'verification_pending',
                'waiting_for_kavita_scan',
                'kavita_scan_timeout'
              )
              and ir.dest_path is not null
              and length(trim(ir.dest_path)) > 0
              and (
                lower(coalesce(ir.source_path, '')) like '%/downloads/comics/%'
                or lower(coalesce(ir.source_path, '')) like '%/temp/downloads/comics/%'
                or lower(coalesce(ir.source_path, '')) like '%/downloads/manual-comics-inbox/%'
                or lower(coalesce(ir.raw_json, '')) like '%pack_fanout%'
                or lower(coalesce(ir.raw_json, '')) like '%local_pack%'
              )
              and not exists (
                select 1
                from import_results verified
                where verified.queue_id = ir.queue_id
                  and verified.verified = 1
              )
            order by coalesce(ir.created_at, 0) desc, ir.id desc
            limit ?
            """,
            (max(1, int(max_rows or 300)),),
        ).fetchall()
    finally:
        conn.close()
    added = 0
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except ValueError:
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        provider = str(row["metadata_provider"] or raw.get("metadata_provider") or "").strip().lower()
        metadata_id = row["metadata_id"] or raw.get("metadata_id")
        event = {
            "event": "pack_import_file",
            "review_id": f"import-result:{row['id']}",
            "source": row["source_path"] or raw.get("source_path") or raw.get("pack_archive_path"),
            "source_path": row["source_path"] or raw.get("source_path") or raw.get("pack_archive_path"),
            "dest": row["dest_path"] or raw.get("dest_path"),
            "destination": row["dest_path"] or raw.get("dest_path"),
            "last_import_dest": row["dest_path"] or raw.get("dest_path"),
            "matched_series": row["series_title"] or raw.get("matched_series") or raw.get("series"),
            "series": row["series_title"] or raw.get("matched_series") or raw.get("series"),
            "series_id": row["series_id"] or raw.get("series_id"),
            "issue_id": row["issue_id"] or raw.get("issue_id"),
            "issue_number": row["issue_number"] or raw.get("issue_number") or raw.get("trusted_issue"),
            "normalized_number": row["normalized_number"] or raw.get("normalized_number"),
            "trusted_series_id": row["series_id"] or raw.get("trusted_series_id") or raw.get("series_id"),
            "trusted_issue": row["issue_number"] or row["normalized_number"] or raw.get("trusted_issue"),
            "metadata_provider": provider or None,
            "metadata_id": metadata_id,
            "native_series_id": row["series_id"] or raw.get("native_series_id") or raw.get("series_id"),
            "truth_model": raw.get("truth_model") or "inkdrop_native",
            "title": row["series_title"] or raw.get("pack_source_title") or raw.get("title"),
            "status": row["status"],
        }
        if provider == "comicvine" and metadata_id not in (None, ""):
            event["comicvine_id"] = metadata_id
        added += add_pack_import_candidate(groups, event, kind="imported", source_hint="import_result_pending", max_rows=max_rows)
    return added


def active_pack_review_ids(groups=None):
    ids = {
        str(review_id)
        for review_id in (groups or {}).keys()
        if str(review_id or "").strip()
    }
    auto_status = read_json_file(PACK_AUTO_IMPORT_STATUS_FILE, {}) or {}
    if isinstance(auto_status, dict) and str(auto_status.get("status") or "") in {"importing", "running", "checking"}:
        review_id = str(auto_status.get("review_id") or "").strip()
        if review_id:
            ids.add(review_id)
    pack_state = read_json_file(PACK_REVIEW_STATE_FILE, {}) or {}
    active = pack_state.get("active") if isinstance(pack_state, dict) else {}
    if isinstance(active, dict) and str(active.get("status") or "") in {"importing", "completed_ready", "waiting_for_local_pack"}:
        review_id = str(active.get("review_id") or "").strip()
        if review_id:
            ids.add(review_id)
    return ids


def recent_terminal_pack_review_ids(limit=20):
    ids = []
    seen = set()

    def add(value):
        review_id = str(value or "").strip()
        if review_id and review_id not in seen:
            seen.add(review_id)
            ids.append(review_id)

    auto_status = read_json_file(PACK_AUTO_IMPORT_STATUS_FILE, {}) or {}
    if isinstance(auto_status, dict):
        status = str(auto_status.get("status") or "").strip().lower()
        result_status = str(auto_status.get("result_status") or "").strip().lower()
        if status in {"complete", "completed", "finished", "imported"} or result_status in {"imported", "finished"}:
            add(auto_status.get("review_id"))

    pack_state = read_json_file(PACK_REVIEW_STATE_FILE, {}) or {}
    history = pack_state.get("history") if isinstance(pack_state, dict) else []
    if not isinstance(history, list):
        history = []
    terminal_statuses = {
        "complete",
        "completed",
        "finished",
        "finished_imported",
        "imported",
    }
    for row in reversed(history):
        if len(ids) >= int(limit or 20):
            break
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        if status not in terminal_statuses and row.get("imported_count") is None:
            continue
        add(row.get("review_id"))
    return ids[: int(limit or 20)]


def pack_reverify_review_ids(groups=None, terminal_limit=20):
    ids = set(active_pack_review_ids(groups))
    ids.update(recent_terminal_pack_review_ids(limit=terminal_limit))
    return ids


def recent_pack_log_candidates(groups, limit_lines=8000, max_rows=300, review_ids=None):
    if not PACK_IMPORT_LOG.exists():
        return 0
    review_ids = {str(value) for value in (review_ids or []) if str(value or "").strip()}
    if not review_ids:
        return 0
    try:
        lines = PACK_IMPORT_LOG.read_text(encoding="utf-8").splitlines()[-limit_lines:]
    except OSError:
        return 0
    added = 0
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if str(row.get("review_id") or "") not in review_ids:
            continue
        if add_pack_import_candidate(groups, row, source_hint="pack_log", max_rows=max_rows):
            added += 1
        if sum(len(item["imported"]) + len(item["skipped_existing"]) for item in groups.values()) >= max_rows:
            break
    return added


def trigger_pack_reverify_scan_if_waiting(verification, max_folders=12):
    if not frontend_sync_after_import_enabled():
        return {"requested": 0, "errors": [], "skipped": True, "reason": "frontend_sync_disabled"}
    checked = verification.get("checked") if isinstance(verification, dict) else []
    if not isinstance(checked, list):
        return {"requested": 0, "errors": []}
    folders = []
    seen = set()
    for item in checked:
        if not isinstance(item, dict):
            continue
        if item.get("verification_status") not in {"waiting_for_library_scan", "waiting_for_kavita_scan", "library_scan_timeout", "kavita_scan_timeout"}:
            continue
        dest = str(item.get("dest") or "").strip()
        if not dest or not item.get("host_exists"):
            continue
        folder = str(Path(dest).parent)
        key = folder.replace("\\", "/").rstrip("/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        folders.append(folder)
        if len(folders) >= max_folders:
            break
    sync = sync_library_frontend_folders(folders, event_prefix="pack_import_reverify_")
    scans = sync.get("library_scan_tasks") or {"kavita": sync.get("kavita") or [], "komga": sync.get("komga") or []}
    errors = sync.get("errors") or []
    if sync.get("requested") or errors:
        log({"event": "pack_import_reverify_scan_requested", "requested": sync.get("requested"), "errors": errors[:5]})
    return {
        "requested": int(sync.get("requested") or 0),
        "library_scan_tasks": scans,
        "kavita_scan_tasks": scans.get("kavita") or [],
        "komga_scan_tasks": scans.get("komga") or [],
        "errors": errors,
        "truncated": len(seen) > len(folders),
    }


def normalized_fs_path(value):
    return str(value or "").replace("\\", "/").rstrip("/")


def same_existing_file(left, right):
    try:
        return Path(left).exists() and Path(right).exists() and os.path.samefile(left, right)
    except OSError:
        return normalized_fs_path(left).lower() == normalized_fs_path(right).lower()


def relative_quarantine_path(path):
    path = Path(path)
    for root in (COMIC_ROOT, MANGA_ROOT, QBIT_HOST_DOWNLOAD_ROOT):
        try:
            return path.relative_to(root)
        except ValueError:
            continue
    return Path(path.name)


def pack_duplicate_candidate_paths(source_attempt_raw):
    if not isinstance(source_attempt_raw, dict):
        return []
    candidates = []

    def add(value):
        value = str(value or "").strip()
        if value:
            candidates.append(value)

    add(source_attempt_raw.get("dest"))
    add(source_attempt_raw.get("dest_path"))
    add(source_attempt_raw.get("last_import_dest"))
    for nested_key in ("raw_event", "verification"):
        nested = source_attempt_raw.get(nested_key)
        if isinstance(nested, dict):
            add(nested.get("dest"))
            add(nested.get("dest_path"))
            add(nested.get("last_import_dest"))
    deduped = []
    seen = set()
    for value in candidates:
        key = normalized_fs_path(value).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def pack_duplicate_row_from_candidate(row, duplicate_path, verified_dest):
    duplicate = Path(duplicate_path)
    verified = Path(verified_dest)
    item = {
        "queue_id": row.get("queue_id"),
        "wanted_id": row.get("wanted_id"),
        "series_id": row.get("series_id"),
        "issue_id": row.get("issue_id"),
        "series": row.get("series_title"),
        "issue_number": row.get("issue_number"),
        "source_attempt_id": row.get("source_attempt_id"),
        "source_attempt_status": row.get("source_attempt_status"),
        "import_result_id": row.get("import_result_id"),
        "duplicate_path": str(duplicate),
        "verified_dest": str(verified),
        "eligible": False,
        "reason": "",
    }
    if not duplicate.exists():
        item["reason"] = "duplicate_path_missing"
        return item
    if not verified.exists():
        item["reason"] = "verified_dest_missing"
        return item
    if same_existing_file(duplicate, verified):
        item["reason"] = "same_file"
        return item
    if is_internal_import_path(duplicate, duplicate.anchor or None):
        item["reason"] = "duplicate_already_internal"
        return item
    try:
        duplicate_stat = duplicate.stat()
        verified_stat = verified.stat()
    except OSError as exc:
        item["reason"] = f"stat_failed:{type(exc).__name__}"
        return item
    item["duplicate_size"] = int(duplicate_stat.st_size)
    item["verified_size"] = int(verified_stat.st_size)
    if duplicate_stat.st_size <= 0 or duplicate_stat.st_size != verified_stat.st_size:
        item["reason"] = "size_mismatch"
        return item
    duplicate_hash = sha256(duplicate)
    verified_hash = sha256(verified)
    item["sha256"] = duplicate_hash
    item["verified_sha256"] = verified_hash
    if duplicate_hash != verified_hash:
        item["reason"] = "hash_mismatch"
        return item
    item["eligible"] = True
    item["reason"] = "same_hash_verified_duplicate"
    return item


def quarantine_pack_duplicate(item, quarantine_root=None, dry_run=True):
    duplicate = Path(item.get("duplicate_path") or "")
    quarantine_root = Path(quarantine_root or PACK_DUPLICATE_QUARANTINE_ROOT)
    relative = relative_quarantine_path(duplicate)
    quarantine_dir = quarantine_root / time.strftime("%Y%m%d-%H%M%S") / relative.parent
    quarantine_dest = unique_dest_name(quarantine_dir, duplicate.name)
    item["quarantine_dest"] = str(quarantine_dest)
    if dry_run:
        item["action"] = "would_quarantine"
        return item
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(duplicate), str(quarantine_dest))
    item["action"] = "quarantined"
    item["duplicate_exists_after"] = duplicate.exists()
    if app_setting_value("media_management.delete_empty_folders", False):
        library_root = inkdrop_folder_cleanup.containing_root(duplicate, (COMIC_ROOT, MANGA_ROOT))
        if library_root is not None:
            sweep = inkdrop_folder_cleanup.remove_empty_parents(duplicate, library_root)
            if sweep.get("removed"):
                item["removed_empty_folders"] = sweep["removed"]
    try:
        log({"event": "pack_duplicate_quarantined", **item})
    except Exception:
        pass
    if inkdrop_state is not None and item.get("queue_id"):
        try:
            inkdrop_state.record_queue_source_attempt(
                INKDROP_STATE_DB,
                item["queue_id"],
                {
                    "source": "pack_import_cleanup",
                    "provider": "local_files",
                    "status": "duplicate_quarantined",
                    "title": item.get("series") or "Pack import duplicate",
                    "display_phase": "cleanup",
                    "outcome": "productive",
                    "reason": "verified same-hash duplicate moved out of library",
                    "duplicate_path": item.get("duplicate_path"),
                    "quarantine_dest": item.get("quarantine_dest"),
                    "verified_dest": item.get("verified_dest"),
                    "sha256": item.get("sha256"),
                },
            )
        except Exception as exc:
            item["history_error"] = f"{type(exc).__name__}: {exc}"
    return item


def audit_pack_duplicate_imports(limit=50, quarantine=False, dry_run=True):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {"ok": False, "reason": "state_db_missing", "duplicates": []}
    conn = sqlite_connect(INKDROP_STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select
                sa.id as source_attempt_id,
                sa.queue_id,
                sa.status as source_attempt_status,
                sa.raw_json as source_attempt_raw,
                ir.id as import_result_id,
                ir.dest_path as verified_dest,
                ir.source_path as verified_source,
                ir.created_at as verified_at,
                q.wanted_id,
                q.series_id,
                q.issue_id,
                s.title as series_title,
                i.issue_number
            from source_attempts sa
            join import_results ir on ir.queue_id = sa.queue_id and ir.verified = 1
            left join queue_items q on q.id = sa.queue_id
            left join series s on s.id = q.series_id
            left join issues i on i.id = q.issue_id
            where lower(coalesce(sa.source, '')) like 'pack_import%'
              and lower(coalesce(sa.status, '')) in (
                'verification_failed',
                'waiting_for_kavita_scan',
                'kavita_scan_timeout'
              )
              and coalesce(ir.dest_path, '') != ''
            order by coalesce(ir.created_at, 0) desc, sa.id desc
            limit ?
            """,
            (int(limit or 50),),
        ).fetchall()
    finally:
        conn.close()
    duplicates = []
    skipped = 0
    seen = set()
    for row in rows:
        row_dict = dict(row)
        try:
            raw = json.loads(row_dict.get("source_attempt_raw") or "{}")
        except ValueError:
            raw = {}
        verified_dest = str(row_dict.get("verified_dest") or "").strip()
        for candidate in pack_duplicate_candidate_paths(raw):
            if not candidate or normalized_fs_path(candidate).lower() == normalized_fs_path(verified_dest).lower():
                continue
            key = (row_dict.get("queue_id"), normalized_fs_path(candidate).lower(), normalized_fs_path(verified_dest).lower())
            if key in seen:
                continue
            seen.add(key)
            item = pack_duplicate_row_from_candidate(row_dict, candidate, verified_dest)
            if item.get("eligible"):
                if quarantine:
                    item = quarantine_pack_duplicate(item, dry_run=dry_run)
                else:
                    item["action"] = "report_only"
                duplicates.append(item)
            else:
                skipped += 1
    summary = {
        "ok": True,
        "mode": "quarantine" if quarantine else "report",
        "dry_run": bool(dry_run),
        "checked_attempts": len(rows),
        "duplicates": duplicates,
        "eligible_count": len(duplicates),
        "skipped_candidates": skipped,
        "quarantined": sum(1 for item in duplicates if item.get("action") == "quarantined"),
        "would_quarantine": sum(1 for item in duplicates if item.get("action") == "would_quarantine"),
    }
    if quarantine and not dry_run:
        write_import_status({"kind": "pack_duplicate_cleanup", **summary})
    return summary


def reverify_inkdrop_pack_imports(max_rows=300):
    if inkdrop_state is None:
        return {"ok": False, "reason": "inkdrop_state_module_missing"}
    groups = {}
    pending_added = pending_native_pack_import_candidates(groups, max_rows=max_rows)
    import_result_added = pending_import_result_pack_candidates(groups, max_rows=max_rows)
    reverify_ids = pack_reverify_review_ids(groups)
    log_added = recent_pack_log_candidates(groups, max_rows=max_rows, review_ids=reverify_ids)
    if not groups:
        return {"ok": True, "candidate_rows": 0, "groups": 0, "recorded": 0, "deferred": 0, "unmatched": 0}
    try:
        apply_path_provider_settings()
    except Exception as exc:
        log({"event": "pack_import_reverify_path_settings_failed", "error": f"{type(exc).__name__}: {exc}"})
    summaries = []
    recorded = 0
    already_recorded = 0
    deferred = 0
    unmatched = 0
    verified_visible = 0
    checked_count = 0
    for review_id, group in groups.items():
        imported = list(group["imported"].values())
        skipped = list(group["skipped_existing"].values())
        verification = verify_imported_items(imported, poll_library_visibility=False) if imported else {}
        scan_summary = trigger_pack_reverify_scan_if_waiting(verification) if imported else {"requested": 0, "errors": []}
        checked_count += int(verification.get("checked_count") or 0)
        verified_visible += int(verification.get("kavita_visible_count") or 0)
        result = inkdrop_state.record_pack_import_results(
            INKDROP_STATE_DB,
            imported=imported,
            skipped_existing=skipped,
            review_id=review_id,
            pack_path=group.get("pack_path") or "",
            series=group.get("series"),
            title=group.get("title"),
            verification=verification,
            source="pack_import_reverify",
        )
        recorded += int(result.get("recorded") or 0) if isinstance(result, dict) else 0
        already_recorded += int(result.get("already_recorded") or 0) if isinstance(result, dict) else 0
        deferred += int(result.get("deferred") or 0) if isinstance(result, dict) else 0
        unmatched += int(result.get("unmatched") or 0) if isinstance(result, dict) else 0
        summaries.append(
            {
                "review_id": review_id,
                "series": group.get("series"),
                "imported": len(imported),
                "skipped_existing": len(skipped),
                "checked": verification.get("checked_count"),
                "visible": verification.get("kavita_visible_count"),
                "recorded": result.get("recorded") if isinstance(result, dict) else None,
                "already_recorded": result.get("already_recorded") if isinstance(result, dict) else None,
                "deferred": result.get("deferred") if isinstance(result, dict) else None,
                "unmatched": result.get("unmatched") if isinstance(result, dict) else None,
                "scan_requests": scan_summary.get("requested"),
                "scan_errors": len(scan_summary.get("errors") or []),
                "sources": sorted(group.get("sources") or []),
            }
        )
    summary = {
        "ok": True,
        "groups": len(groups),
        "candidate_rows": sum(len(item["imported"]) + len(item["skipped_existing"]) for item in groups.values()),
        "pending_rows": pending_added,
        "import_result_rows": import_result_added,
        "log_rows": log_added,
        "review_ids_checked": len(reverify_ids),
        "checked": checked_count,
        "visible": verified_visible,
        "recorded": recorded,
        "already_recorded": already_recorded,
        "deferred": deferred,
        "unmatched": unmatched,
        "items": summaries[:12],
    }
    log({"event": "inkdrop_pack_import_reverify", **summary})
    return summary


def provider_config(provider_id):
    if inkdrop_state is None:
        return None
    try:
        return inkdrop_state.provider_config(INKDROP_STATE_DB, provider_id)
    except Exception as exc:
        log({"event": "provider_config_load_failed", "provider_id": provider_id, "error": f"{type(exc).__name__}: {exc}"})
        return None


def app_setting_value(key, default=None):
    if inkdrop_state is None:
        return default
    try:
        setting = inkdrop_state.app_setting(INKDROP_STATE_DB, key)
    except Exception as exc:
        log({"event": "app_setting_load_failed", "key": key, "error": f"{type(exc).__name__}: {exc}"})
        return default
    if isinstance(setting, dict) and "value" in setting:
        return setting.get("value")
    return default


def completed_import_kapowarr_adapter_enabled():
    # Retired in Build 165. The guarded compatibility implementation remains
    # unreachable for one rollback window.
    return False


def media_management_library_visibility_required():
    return bool_setting(
        {"required": app_setting_value("media_management.library_visibility_required", False)},
        "required",
        False,
    )


def media_management_library_visibility_checks_enabled():
    return bool_setting(
        {"enabled": app_setting_value("media_management.library_visibility_checks_enabled", False)},
        "enabled",
        False,
    )


def frontend_sync_after_import_enabled():
    return bool_setting(
        {"enabled": app_setting_value("media_management.frontend_sync_after_import", True)},
        "enabled",
        True,
    )


def provider_enabled(provider_id, default=False):
    config = provider_config(provider_id) or {}
    if not config:
        return bool(default)
    return bool(config.get("enabled", default))


def load_qbit_settings():
    try:
        import yaml

        cfg = yaml.safe_load(QBIT_CONFIG.read_text()) if QBIT_CONFIG.exists() else {}
        qbt = (cfg or {}).get("qbt") or {}
    except Exception as exc:
        log({"event": "qbit_config_load_failed", "error": f"{type(exc).__name__}: {exc}"})
        qbt = {}
    config = provider_config("qbittorrent") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("qBittorrent provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    host = str(config.get("base_url") or settings.get("host") or os.environ.get("INKDROP_QBITTORRENT_URL") or qbt.get("host") or "").strip().rstrip("/")
    if not host:
        raise RuntimeError("qBittorrent URL is not configured; set INKDROP_QBITTORRENT_URL or the qBittorrent provider host setting.")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    user = str(settings.get("username") or settings.get("user") or os.environ.get("INKDROP_QBITTORRENT_USERNAME") or qbt.get("user") or "").strip()
    password = str(settings.get("password") or settings.get("pass") or os.environ.get("INKDROP_QBITTORRENT_PASSWORD") or qbt.get("pass") or "").strip()
    return {
        "host": host,
        "user": user,
        "pass": password,
        "comics_category": str(settings.get("comics_category") or "comics").strip() or "comics",
        "ebooks_category": str(settings.get("ebooks_category") or "readarr").strip() or "readarr",
        "comics_save_path": str(settings.get("comics_save_path") or "/downloads/comics").strip() or "/downloads/comics",
        "ebooks_save_path": str(settings.get("ebooks_save_path") or "/downloads/readarr").strip() or "/downloads/readarr",
        "source": config.get("source") or "fallback",
    }


def path_setting(settings, key, fallback):
    value = str((settings or {}).get(key) or "").strip()
    return Path(value) if value else Path(fallback)


def text_setting(settings, key, fallback):
    value = str((settings or {}).get(key) or "").strip()
    return value or str(fallback)


def bool_setting(settings, key, fallback=False):
    if key not in (settings or {}):
        return bool(fallback)
    value = (settings or {}).get(key)
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return bool(fallback)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(fallback)


def int_setting(settings, key, fallback, minimum=None, maximum=None):
    try:
        value = int((settings or {}).get(key))
    except (TypeError, ValueError):
        value = int(fallback)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def list_setting(settings, key):
    value = (settings or {}).get(key)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,;\s]+", text) if part.strip()]


def load_path_settings():
    library_config = provider_config("library_paths") or {}
    inbox_config = provider_config("manual_inboxes") or {}
    library = library_config.get("settings") if isinstance(library_config.get("settings"), dict) else {}
    inboxes = inbox_config.get("settings") if isinstance(inbox_config.get("settings"), dict) else {}
    comic_root = path_setting(library, "comic_root", COMIC_ROOT)
    manga_root = path_setting(library, "manga_root", MANGA_ROOT)
    return {
        "comic_root": comic_root,
        "manga_root": manga_root,
        "kavita_comic_root": text_setting(library, "kavita_comic_root", KAVITA_COMIC_ROOT).rstrip("/"),
        "kavita_manga_root": text_setting(library, "kavita_manga_root", KAVITA_MANGA_ROOT).rstrip("/"),
        "manual_comics_inbox": path_setting(inboxes, "manual_comics_inbox", MANUAL_COMIC_SOURCES[0]),
        "manual_ebooks_inbox": path_setting(inboxes, "manual_ebooks_inbox", MANUAL_EBOOK_SOURCES[0]),
        "library_source": library_config.get("source") or "fallback",
        "manual_inbox_source": inbox_config.get("source") or "fallback",
    }


def apply_path_provider_settings():
    global COMIC_ROOT, MANGA_ROOT, KAVITA_COMIC_ROOT, KAVITA_MANGA_ROOT
    global COMIC_DEST, MANUAL_COMIC_SOURCES, MANUAL_EBOOK_SOURCES
    settings = load_path_settings()
    COMIC_ROOT = settings["comic_root"]
    MANGA_ROOT = settings["manga_root"]
    KAVITA_COMIC_ROOT = settings["kavita_comic_root"]
    KAVITA_MANGA_ROOT = settings["kavita_manga_root"]
    COMIC_DEST = COMIC_ROOT / "_Incoming"
    MANUAL_COMIC_SOURCES = [settings["manual_comics_inbox"]]
    MANUAL_EBOOK_SOURCES = [settings["manual_ebooks_inbox"]]
    log({
        "event": "path_provider_settings_loaded",
        "comic_root": str(COMIC_ROOT),
        "manga_root": str(MANGA_ROOT),
        "kavita_comic_root": KAVITA_COMIC_ROOT,
        "kavita_manga_root": KAVITA_MANGA_ROOT,
        "manual_comics_inbox": str(MANUAL_COMIC_SOURCES[0]),
        "manual_ebooks_inbox": str(MANUAL_EBOOK_SOURCES[0]),
        "library_source": settings.get("library_source"),
        "manual_inbox_source": settings.get("manual_inbox_source"),
    })
    return settings


def write_import_status(status):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {**status, "updated_at": time.time()}
    persist_import_status_event(payload)
    legacy_status_written = True
    try:
        write_json_atomic(IMPORT_STATUS_PATH, payload)
    except OSError as exc:
        legacy_status_written = False
        log({
            "event": "inkdrop_import_legacy_status_write_deferred",
            "error_type": type(exc).__name__,
        })
    if import_status_sync_deferred(status):
        result = {
            "ok": True,
            "deferred": True,
            "reason": "import_status_sync_deferred",
            "mode": import_status_sync_mode(),
            "legacy_status_written": legacy_status_written,
        }
        log({
            "event": "inkdrop_import_status_sync_deferred",
            "kind": (status or {}).get("kind"),
            "imported_count": (status or {}).get("imported_count"),
            "mode": result["mode"],
        })
        return result
    return sync_inkdrop_import_results()


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(
        f"{os.getpid()}:{time.time_ns()}:{path}".encode("utf-8")
    ).hexdigest()[:16]
    temporary = path.with_name(f".{path.name}.{token}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def persist_import_status_event(payload):
    """Keep each completed import until state reconciliation commits it."""
    events_dir = STATE_DIR / "import-status-events"
    events_dir.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if len(serialized.encode("utf-8")) > MAX_IMPORT_STATUS_EVENT_BYTES:
        raise RuntimeError("completed import status is too large to persist safely")
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    event_name = f"{time.time_ns()}-{os.getpid()}-{digest}.json"
    write_json_atomic(events_dir / event_name, payload)
    return events_dir / event_name


def import_status_sync_mode():
    return str(import_status_sync_mode_env_value() or "full").strip().lower()


def import_status_sync_mode_env_value():
    for name in ("INKDROP_COMPLETED_IMPORT_STATUS_SYNC_MODE", "INKDROP_IMPORT_STATUS_SYNC_MODE"):
        value = os.environ.get(name)
        if value is not None:
            return value
    return None


def import_status_queue_backed_source_file_child():
    argv = list(sys.argv[1:])
    return (
        any(arg == "--source-file" or arg.startswith("--source-file=") for arg in argv)
        and any(arg == "--trusted-series-id" or arg.startswith("--trusted-series-id=") for arg in argv)
        and no_wait_for_library_scan_flag_present(argv)
    )


def no_wait_for_library_scan_flag_present(argv):
    values = {str(arg or "").strip() for arg in (argv or [])}
    return bool({"--no-wait-for-library-scan", "--no-wait-for-kavita-scan"} & values)


def import_status_sync_deferred(status=None):
    mode = import_status_sync_mode()
    if mode in {"0", "false", "off", "none", "defer", "deferred", "skip", "skipped"}:
        return True
    if import_status_sync_mode_env_value() is None and import_status_queue_backed_source_file_child():
        return True
    status = status if isinstance(status, dict) else {}
    return bool(status.get("defer_inkdrop_import_status_sync"))


def append_manual_review(reason, payload, db_path=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with REVIEW_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.time(), "reason": reason, **payload}, sort_keys=True) + "\n")
    try:
        import inkdrop_notifications
        inkdrop_notifications.notify_manual_review(
            db_path,
            reason=reason,
            series=payload.get("series") or payload.get("matched_series"),
            source=payload.get("source"),
            detail=payload.get("detail"),
            note=payload.get("note"),
        )
    except Exception:
        pass


def validate_comic_archive(path, min_pages=3, min_payload_bytes=1024 * 1024):
    ext = comic_archive_suffix(path)
    with path.open("rb") as handle:
        head = handle.read(16)
    if not head or all(byte == 0 for byte in head):
        return {"ok": False, "reason": "zero_or_empty_header", "page_count": 0, "payload_size": 0}
    if ext == ".cbz":
        try:
            with zipfile.ZipFile(path) as archive:
                images = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir() and Path(info.filename).suffix.lower() in COMIC_IMAGE_EXTS
                ]
                payload_size = sum(info.file_size for info in images)
                try:
                    bad_member = archive.testzip()
                    bad_member_error = None
                except Exception as exc:
                    bad_member = None
                    bad_member_error = str(exc)
        except zipfile.BadZipFile:
            return {"ok": False, "reason": "bad_zip_archive", "page_count": 0, "payload_size": 0}
        if len(images) < min_pages:
            return {"ok": False, "reason": "too_few_image_pages", "page_count": len(images), "payload_size": payload_size}
        if payload_size < min_payload_bytes:
            return {"ok": False, "reason": "too_little_image_payload", "page_count": len(images), "payload_size": payload_size}
        if bad_member:
            return {
                "ok": False,
                "reason": "bad_zip_member",
                "page_count": len(images),
                "payload_size": payload_size,
                "bad_member": bad_member,
            }
        if bad_member_error:
            return {
                "ok": False,
                "reason": "bad_zip_member",
                "page_count": len(images),
                "payload_size": payload_size,
                "error": bad_member_error,
            }
        return {"ok": True, "page_count": len(images), "payload_size": payload_size}
    if ext == ".cbr":
        # No workdir passed -- reuses the single-slot extraction cache, so if
        # this same file goes on to repack_cbr_to_cbz() moments later, that
        # call reuses this extraction instead of paying for a second one.
        try:
            images, meta = extract_cbr_images(path)
        except Exception as exc:
            return {
                "ok": False,
                "reason": "cbr_extract_failed",
                "page_count": 0,
                "payload_size": 0,
                "error": str(exc),
            }
        payload_size = sum(item.stat().st_size for item in images if item.exists())
        if len(images) < min_pages:
            return {
                "ok": False,
                "reason": "too_few_image_pages",
                "page_count": len(images),
                "payload_size": payload_size,
                "exit_code": meta.get("unrar_exit_code", meta.get("seven_zip_exit_code")),
            }
        if payload_size < min_payload_bytes:
            return {
                "ok": False,
                "reason": "too_little_image_payload",
                "page_count": len(images),
                "payload_size": payload_size,
                "exit_code": meta.get("unrar_exit_code", meta.get("seven_zip_exit_code")),
            }
        return {
            "ok": True,
            "page_count": len(images),
            "payload_size": payload_size,
            "exit_code": meta.get("unrar_exit_code", meta.get("seven_zip_exit_code")),
            "extractor": meta.get("extractor"),
            "expected_pages": meta.get("expected_pages"),
            "partial_extract": bool(meta.get("partial_extract")),
            "missing_pages": meta.get("missing_pages", 0),
            "extracted_ratio": meta.get("extracted_ratio"),
        }
    return {"ok": True, "reason": "non_archive_comic_format"}


def normalize_archive_member_name(member, root):
    rel = member.relative_to(root)
    parts = [re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", part).strip(" .") or "page" for part in rel.parts]
    return "/".join(parts)


def list_cbr_image_entries(source):
    proc = run_subprocess_bounded(
        ["7z", "l", "-slt", str(source)],
        text=True,
        errors="replace",
        capture_output=True,
        timeout=CBR_LIST_TIMEOUT_SECONDS,
    )
    entries = []
    for line in proc.stdout.splitlines():
        if not line.startswith("Path = "):
            continue
        name = line[7:].strip()
        if Path(name).suffix.lower() in COMIC_IMAGE_EXTS:
            entries.append(name)
    return entries


def natural_sort_key(text):
    """Sort page names the way a person reads them: 2 before 10."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", str(text))
    ]


def extracted_comic_images(workdir):
    items = [
        item
        for item in workdir.rglob("*")
        if item.is_file() and item.suffix.lower() in COMIC_IMAGE_EXTS and item.stat().st_size > 0
    ]
    items.sort(key=lambda item: natural_sort_key(item.relative_to(workdir).as_posix()))
    return items


def extracted_comicinfo_xml_bytes(workdir):
    for item in workdir.rglob("*"):
        if item.is_file() and item.name.lower() == "comicinfo.xml":
            try:
                return item.read_bytes()
            except OSError:
                return None
    return None


def _resolve_unrar_candidate(candidate):
    raw = str(candidate or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute() or any(separator in raw for separator in ("/", "\\")):
        return raw if path.exists() else None
    resolved = shutil.which(raw)
    return resolved or None


def preferred_unrar_tool():
    for candidate in (
        os.environ.get("INKDROP_UNRAR_PATH"),
        os.environ.get("INKDROP_UNRAR"),
        USER_LOCAL_UNRAR,
        "/usr/bin/unrar",
        "/usr/bin/unrar-free",
        "unrar",
        "unrar-free",
    ):
        resolved = _resolve_unrar_candidate(candidate)
        if resolved:
            return resolved
    return None


def cbr_partial_extract_is_usable(images, expected):
    if not expected:
        return False
    extracted = len(images)
    missing = len(expected) - extracted
    if missing <= 0:
        return True
    if extracted < CBR_PARTIAL_EXTRACT_MIN_PAGES:
        return False
    if missing > CBR_PARTIAL_EXTRACT_MAX_MISSING:
        return False
    return extracted / len(expected) >= CBR_PARTIAL_EXTRACT_MIN_RATIO


def cbr_extraction_meta(extractor, proc, expected, images, fallback_proc=None, workdir=None):
    meta = {
        "extractor": extractor,
        "seven_zip_exit_code": proc.returncode,
        "expected_pages": len(expected),
        "extracted_pages": len(images),
    }
    if fallback_proc is not None:
        meta["unrar_exit_code"] = fallback_proc.returncode
    if expected:
        missing = max(0, len(expected) - len(images))
        meta["missing_pages"] = missing
        meta["extracted_ratio"] = round(len(images) / len(expected), 4)
        if missing:
            meta["partial_extract"] = True
    if workdir is not None:
        # Scanner-provided ComicInfo.xml (if the release included one) gets
        # extracted to disk alongside the pages but was previously never read
        # -- carried here so repack_cbr_to_cbz can keep it instead of
        # silently dropping it.
        meta["source_comicinfo_xml"] = extracted_comicinfo_xml_bytes(workdir)
    return meta


def _extract_cbr_images_into(source, workdir):
    expected = list_cbr_image_entries(source)
    proc = run_subprocess_bounded(
        ["7z", "x", "-y", f"-o{workdir}", str(source)],
        text=True,
        errors="replace",
        capture_output=True,
        timeout=CBR_EXTRACT_TIMEOUT_SECONDS,
    )
    images = extracted_comic_images(workdir)
    if proc.returncode == 0 and (not expected or len(images) >= len(expected)):
        return images, cbr_extraction_meta("7z", proc, expected, images, workdir=workdir)
    if cbr_partial_extract_is_usable(images, expected):
        return images, cbr_extraction_meta("7z", proc, expected, images, workdir=workdir)

    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    fallback = preferred_unrar_tool()
    if fallback:
        fallback_proc = run_subprocess_bounded(
            [fallback, "x", "-o+", "-idq", str(source), str(workdir) + "/"],
            text=True,
            errors="replace",
            capture_output=True,
            timeout=CBR_EXTRACT_TIMEOUT_SECONDS,
        )
        fallback_images = extracted_comic_images(workdir)
        if fallback_proc.returncode == 0 and (not expected or len(fallback_images) >= len(expected)):
            return fallback_images, cbr_extraction_meta("unrar", proc, expected, fallback_images, fallback_proc, workdir=workdir)
        if cbr_partial_extract_is_usable(fallback_images, expected):
            return fallback_images, cbr_extraction_meta("unrar", proc, expected, fallback_images, fallback_proc, workdir=workdir)
        images = fallback_images
        meta = cbr_extraction_meta("unrar", proc, expected, images, fallback_proc, workdir=workdir)
    else:
        meta = cbr_extraction_meta("7z", proc, expected, images, workdir=workdir)

    if expected and len(images) < len(expected):
        raise RuntimeError(f"incomplete CBR extraction for {source}: extracted {len(images)} of {len(expected)} pages")
    return images, meta


# validate_comic_archive() and repack_cbr_to_cbz() both need the fully
# extracted page list for the same source file, one call apart, and used to
# each pay for a full 7z/unrar extraction independently -- doubling the cost
# of every .cbr import for no benefit, since nothing about the file changes
# between the two calls. This single-slot cache lets the second caller reuse
# the first caller's extraction. Bounded to one outstanding extraction at a
# time (not keyed/grown per file) so it can't leak disk across a long-running
# --all-series scan of many files in one process; the previous entry's temp
# directory is cleaned up as soon as a different file is extracted.
_CBR_EXTRACT_CACHE_LOCK = threading.Lock()
_CBR_EXTRACT_CACHE = {"key": None, "images": None, "meta": None, "tmpdir": None}


def _cbr_extract_cache_clear_locked():
    tmpdir = _CBR_EXTRACT_CACHE["tmpdir"]
    if tmpdir is not None:
        tmpdir.cleanup()
    _CBR_EXTRACT_CACHE["key"] = None
    _CBR_EXTRACT_CACHE["images"] = None
    _CBR_EXTRACT_CACHE["meta"] = None
    _CBR_EXTRACT_CACHE["tmpdir"] = None


def extract_cbr_images(source, workdir=None):
    if workdir is not None:
        return _extract_cbr_images_into(source, workdir)

    cache_key = str(Path(source).resolve())
    with _CBR_EXTRACT_CACHE_LOCK:
        if _CBR_EXTRACT_CACHE["key"] == cache_key:
            cached_images = _CBR_EXTRACT_CACHE["images"]
            if cached_images and all(image.exists() for image in cached_images):
                return cached_images, _CBR_EXTRACT_CACHE["meta"]
        _cbr_extract_cache_clear_locked()
        tmpdir = tempfile.TemporaryDirectory(prefix="kavita-import-cbr-extract-")
        cache_workdir = Path(tmpdir.name) / "extract"
        cache_workdir.mkdir(parents=True, exist_ok=True)
        try:
            images, meta = _extract_cbr_images_into(source, cache_workdir)
        except Exception:
            tmpdir.cleanup()
            raise
        _CBR_EXTRACT_CACHE["key"] = cache_key
        _CBR_EXTRACT_CACHE["images"] = images
        _CBR_EXTRACT_CACHE["meta"] = meta
        _CBR_EXTRACT_CACHE["tmpdir"] = tmpdir
        atexit.register(tmpdir.cleanup)
        return images, meta


def repack_cbr_to_cbz(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    # No workdir passed -- if validate_comic_archive() already extracted this
    # exact file (the normal case: it runs first for every .cbr candidate),
    # this reuses that extraction instead of re-running 7z/unrar from scratch.
    images, meta = extract_cbr_images(source)
    if not images:
        raise RuntimeError(f"no readable image pages extracted from {source}")
    tmp_cbz = dest.with_suffix(dest.suffix + ".tmp")
    if tmp_cbz.exists():
        tmp_cbz.unlink()
    with zipfile.ZipFile(tmp_cbz, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for index, image in enumerate(images, 1):
            archive.write(image, f"{index:04d}{image.suffix.lower()}")
        source_comicinfo = meta.get("source_comicinfo_xml")
        if source_comicinfo:
            # InkDrop writes its own canonical ComicInfo.xml right after this
            # call returns (matched series/volume/issue), which is what
            # readers should use -- so the scanner's original metadata is
            # kept under a non-colliding name instead of overwriting or
            # fighting that write, rather than being discarded outright.
            archive.writestr("ComicInfo.xml.source-embedded", source_comicinfo)
    tmp_cbz.replace(dest)
    meta.update({"page_count": len(images), "source_format": "cbr", "dest_format": "cbz"})
    return meta


def row_value(row, key, default=None):
    if not row:
        return default
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def convert_pdf_to_cbz(source, dest, target, issue_row=None, dpi=160):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kavita-import-pdf-") as tmp:
        workdir = Path(tmp)
        out_prefix = workdir / "page"
        proc = subprocess.run(
            ["pdftoppm", "-jpeg", "-r", str(dpi), str(source), str(out_prefix)],
            text=True,
            errors="replace",
            capture_output=True,
            timeout=900,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pdftoppm failed for {source}: {proc.stderr.strip()}")
        images = [
            item
            for item in sorted(workdir.glob("page-*.jpg"))
            if item.is_file() and item.stat().st_size > 0
        ]
        if not images:
            raise RuntimeError(f"no pages rendered from PDF {source}")
        tmp_cbz = dest.with_suffix(dest.suffix + ".tmp")
        if tmp_cbz.exists():
            tmp_cbz.unlink()
        is_manga_pdf = is_manga_target(target)
        series = safe_filename_part(row_value(issue_row, "title") or target.get("title"))
        number = format_issue_number(row_value(issue_row, "issue_number") or row_value(issue_row, "calculated_issue_number") or extract_issue_number(source))
        year = issue_year(row_value(issue_row, "date"), row_value(issue_row, "year") or target.get("year"))
        title = f"Volume {number}" if is_manga_pdf else f"{series} #{number}"
        format_name = "Manga" if is_manga_pdf else "Comic"
        comicinfo = (
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
            "<ComicInfo>\n"
            f"{xml_node('Series', series)}"
            f"{xml_node('Title', title)}"
            f"{xml_node('Number', number) if not is_manga_pdf else ''}"
            f"{xml_node('Volume', number) if is_manga_pdf else ''}"
            f"{xml_node('Year', year)}"
            f"{xml_node('Format', format_name)}"
            "  <LanguageISO>en</LanguageISO>\n"
            "</ComicInfo>\n"
        )
        with zipfile.ZipFile(tmp_cbz, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for index, image in enumerate(images, 1):
                archive.write(image, f"{index:04d}.jpg")
            archive.writestr("ComicInfo.xml", comicinfo)
        tmp_cbz.replace(dest)
        return {
            "page_count": len(images),
            "source_format": "pdf",
            "dest_format": "cbz",
            "pdf_render_exit_code": proc.returncode,
        }


def write_manga_comicinfo(path, target, canonical=None):
    path = Path(path)
    if comic_archive_suffix(path) != ".cbz":
        return {"comicinfo_written": False, "reason": "not_cbz"}
    number = normalize_manga_number((canonical or {}).get("canonical_issue_number") or extract_issue_number(path))
    if not number:
        return {"comicinfo_written": False, "reason": "missing_number"}
    series = safe_filename_part((target or {}).get("title") or path.parent.name)
    year = str((canonical or {}).get("canonical_year") or (target or {}).get("year") or "").strip()
    display_number = format_volume_number(number)
    volume_number = str(float(number)).rstrip("0").rstrip(".") if "." in str(number) else str(int(float(number)))
    comicinfo = (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        "<ComicInfo>\n"
        f"{xml_node('Series', series)}"
        f"{xml_node('Title', f'{series} v{display_number}')}"
        f"{xml_node('Volume', volume_number)}"
        f"{xml_node('Year', year)}"
        "  <Format>Manga</Format>\n"
        "  <LanguageISO>en</LanguageISO>\n"
        "</ComicInfo>\n"
    )
    tmp_cbz = path.with_suffix(path.suffix + ".tmp")
    tmp_cbz.unlink(missing_ok=True)
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(tmp_cbz, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as dst:
        for info in src.infolist():
            if info.filename.lower().endswith("comicinfo.xml"):
                continue
            dst.writestr(info, src.read(info.filename))
        dst.writestr("ComicInfo.xml", comicinfo)
    tmp_cbz.replace(path)
    return {"comicinfo_written": True, "truth_model": "kavita_manga", "number": number}


def normalize_manga_series_reader_contract(folder, series_title, dry_run=False):
    """Flatten manga volumes and embed Kavita grouping metadata before a scan.

    Library moves can expose archives that predate InkDrop's managed-import
    contract.  Kavita then has only issue-style paths to infer from and may
    create one series per archive.  This pass is deliberately fail-closed: it
    plans every archive first, refuses collisions/ambiguous units, and only
    then rewrites CBZ metadata and moves volumes to the series root.
    """
    folder = Path(folder)
    title = safe_filename_part(series_title or folder.name)
    result = {
        "ok": False,
        "folder": str(folder),
        "series": title,
        "dry_run": bool(dry_run),
        "planned": [],
        "normalized": [],
        "skipped_chapters": [],
        "errors": [],
    }
    try:
        root = folder.resolve(strict=True)
        manga_root = Path(MANGA_ROOT).resolve(strict=True)
        root.relative_to(manga_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        result["reason"] = "manga_series_folder_outside_managed_root"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result
    if root == manga_root or not root.is_dir():
        result["reason"] = "manga_series_folder_required"
        return result

    candidates = []
    destinations = {}
    volumes = {}
    for path in sorted(root.rglob("*")):
        if is_internal_import_path(path, root) or path.is_symlink() or not path.is_file():
            continue
        archive_suffix = comic_archive_suffix(path)
        if archive_suffix in {".cbr", ".pdf"}:
            result["errors"].append({"path": str(path), "reason": "manga_reader_archive_requires_cbz_normalization"})
            continue
        if archive_suffix != ".cbz":
            continue
        semantics = inkdrop_artifact_acceptance.archive_member_semantics(path, fresh=True)
        unit_type = semantics.get("semantic_unit")
        if not semantics.get("checked"):
            result["errors"].append({"path": str(path), "reason": "archive_metadata_unreadable", "error": semantics.get("error")})
            continue
        if not semantics.get("credible_image_count") or not semantics.get("credible_image_payload_bytes"):
            result["errors"].append({"path": str(path), "reason": "manga_volume_image_payload_missing"})
            continue
        if unit_type not in {"volume", "multi_chapter_archive"}:
            result["errors"].append({"path": str(path), "reason": f"manga_reader_archive_unit_{unit_type or 'unknown'}"})
            continue
        comicinfo = semantics.get("comicinfo") if isinstance(semantics.get("comicinfo"), dict) else {}
        path_number = normalize_manga_number(extract_issue_number(path))
        identity_conflicts = inkdrop_artifact_acceptance.comicinfo_target_conflicts(
            comicinfo,
            title,
            path_number,
            target_type="volume",
        )
        if identity_conflicts:
            result["errors"].append({"path": str(path), "reason": identity_conflicts[0]})
            continue
        number = comicinfo.get("volume") if comicinfo.get("authoritative") else None
        if not number:
            try:
                _, number = manga_file_unit_and_number(path)
            except Exception as exc:
                result["errors"].append({"path": str(path), "reason": "archive_metadata_unreadable", "error": f"{type(exc).__name__}: {exc}"})
                continue
        number = normalize_manga_number(number)
        if not number:
            result["errors"].append({"path": str(path), "reason": "manga_volume_number_missing"})
            continue
        volume_key = str(float(number))
        if volume_key in volumes and volumes[volume_key] != path:
            result["errors"].append({"path": str(path), "reason": "duplicate_manga_volume_number", "volume": number})
            continue
        volumes[volume_key] = path
        info = read_comicinfo(path)
        year = comicinfo_text(info, "Year")
        if not year:
            match = re.search(r"\b(19\d{2}|20\d{2})\b", path.name)
            year = match.group(1) if match else ""
        display = format_volume_number(number)
        filename = f"{title} v{display}"
        if year:
            filename += f" ({year})"
        destination = root / f"{filename}.cbz"
        key = os.path.normcase(str(destination))
        if key in destinations and destinations[key] != path:
            result["errors"].append(
                {"path": str(path), "reason": "duplicate_manga_volume_destination", "destination": str(destination)}
            )
            continue
        destinations[key] = path
        if destination.exists() and destination.resolve() != path.resolve():
            result["errors"].append(
                {"path": str(path), "reason": "manga_volume_destination_collision", "destination": str(destination)}
            )
            continue
        candidates.append(
            {
                "source": path,
                "destination": destination,
                "number": number,
                "year": year,
            }
        )

    result["planned"] = [
        {
            "source": str(item["source"]),
            "destination": str(item["destination"]),
            "volume": item["number"],
            "year": item["year"] or None,
        }
        for item in candidates
    ]
    if result["errors"]:
        result["reason"] = "manga_reader_contract_preflight_failed"
        return result
    if dry_run:
        result.update({"ok": True, "reason": "manga_reader_contract_ready", "normalized_count": 0})
        return result

    target = {"title": title, "year": None, "media_type": "manga", "folder": str(root)}
    for item in candidates:
        source = item["source"]
        destination = item["destination"]
        metadata = write_manga_comicinfo(
            source,
            target,
            {"canonical_issue_number": item["number"], "canonical_year": item["year"]},
        )
        if source.resolve() != destination.resolve():
            source.replace(destination)
        result["normalized"].append(
            {
                "source": str(source),
                "destination": str(destination),
                "volume": item["number"],
                "comicinfo": metadata,
            }
        )
    os.utime(root, None)
    removed_dirs = []
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
            removed_dirs.append(str(directory))
        except OSError:
            pass
    result.update(
        {
            "ok": True,
            "reason": "manga_reader_contract_normalized",
            "normalized_count": len(result["normalized"]),
            "folder_touched": True,
            "removed_empty_directories": removed_dirs,
        }
    )
    return result


def write_comic_comicinfo(path, target, canonical=None):
    path = Path(path)
    if comic_archive_suffix(path) != ".cbz" or not target:
        return {"comicinfo_written": False, "reason": "not_comic_cbz"}
    number = format_issue_number((canonical or {}).get("canonical_issue_number") or extract_issue_number(path))
    if not number:
        return {"comicinfo_written": False, "reason": "missing_number"}
    series = safe_filename_part((target or {}).get("title") or path.parent.name)
    year = str((canonical or {}).get("canonical_year") or (target or {}).get("year") or "").strip()
    issue_title = str((canonical or {}).get("canonical_issue_title") or "").strip()
    title = issue_title or f"{series} #{number}"
    comicinfo = (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        "<ComicInfo>\n"
        f"{xml_node('Series', series)}"
        f"{xml_node('Title', title)}"
        f"{xml_node('Number', number)}"
        f"{xml_node('Year', year)}"
        "  <Format>Comic</Format>\n"
        "  <LanguageISO>en</LanguageISO>\n"
        "</ComicInfo>\n"
    )
    tmp_cbz = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(tmp_cbz, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as dst:
        for info in src.infolist():
            if info.filename.lower().endswith("comicinfo.xml"):
                continue
            dst.writestr(info, src.read(info.filename))
        dst.writestr("ComicInfo.xml", comicinfo)
    tmp_cbz.replace(path)
    return {"comicinfo_written": True, "truth_model": "kapowarr_comic", "number": number}


def source_identity_text(source, *, components=2):
    """Join only the path components that may carry unit identity.

    Chapter and volume markers come from a file's own name and its immediate
    folder -- never from ancestors. The old four-component window reached
    outside the content into whatever contained it: a random temp-directory
    suffix beginning c+digits parsed as a phantom chapter (flake #85), and
    any real ancestor segment like '/c11/' mislabels every file beneath it.
    """
    return " ".join(Path(source).parts[-max(1, int(components)):])


def suwayomi_chapter_number(source):
    text = source_identity_text(source)
    patterns = [
        r"(?:^|[\s._-])chapter[\s._-]*(\d+(?:\.\d+)?)",
        r"(?:^|[\s._-])ch[\s._-]*(\d+(?:\.\d+)?)",
        r"(?:^|[\s._-])c[\s._-]*0*(\d+(?:\.\d+)?)(?![0-9a-z])",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return normalize_manga_number(match.group(1))
    return None


def suwayomi_volume_number(source):
    text = source_identity_text(source)
    patterns = [
        r"(?:^|[\s._-])vol(?:ume)?[\s._-]*(\d+(?:\.\d+)?)",
        r"(?:^|[\s._-])v[\s._-]*(\d+(?:\.\d+)?)(?![0-9a-z])",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return normalize_manga_number(match.group(1))
    return None


def is_suwayomi_import_source(source):
    text = str(source or "").replace("\\", "/").lower()
    return bool(
        "/manga-staging/suwayomi/" in text
        or "/inkdrop-source-worker/suwayomi/" in text
        or text.endswith("/manga-staging/suwayomi")
        or text.endswith("/inkdrop-source-worker/suwayomi")
    )


def write_manga_chapter_comicinfo(path, target, chapter_number, title=None):
    path = Path(path)
    if comic_archive_suffix(path) != ".cbz":
        return {"comicinfo_written": False, "reason": "not_cbz"}
    number = normalize_manga_number(chapter_number)
    if not number:
        return {"comicinfo_written": False, "reason": "missing_chapter_number"}
    series = safe_filename_part((target or {}).get("title") or path.parent.name)
    year = str((target or {}).get("year") or "").strip()
    display_number = str(int(number))
    chapter_title = title or f"Chapter {display_number}"
    comicinfo = (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
        "<ComicInfo>\n"
        f"{xml_node('Series', series)}"
        f"{xml_node('Title', chapter_title)}"
        f"{xml_node('Number', number)}"
        f"{xml_node('Year', year)}"
        "  <Format>Manga Chapter</Format>\n"
        "  <LanguageISO>en</LanguageISO>\n"
        "</ComicInfo>\n"
    )
    tmp_cbz = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(tmp_cbz, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as dst:
        for info in src.infolist():
            if info.filename.lower().endswith("comicinfo.xml"):
                continue
            dst.writestr(info, src.read(info.filename))
        dst.writestr("ComicInfo.xml", comicinfo)
    tmp_cbz.replace(path)
    return {
        "comicinfo_written": True,
        "truth_model": "kavita_manga",
        "manga_unit_model": "chapter",
        "number": number,
    }


def manga_archive_normalization_chapter_number(source_path, event=None, canonical=None):
    event = event if isinstance(event, dict) else {}
    canonical = canonical if isinstance(canonical, dict) else {}
    source_unit = str(event.get("source_unit") or event.get("unit_type") or event.get("unitType") or "").strip().lower()
    unit_model = str(event.get("manga_unit_model") or "").strip().lower()
    source_chapter = source_chapter_number(source_path)
    explicit_chapter = source_unit == "chapter" or bool(source_chapter)
    if not explicit_chapter and unit_model == "chapter" and not manga_source_has_explicit_volume_hint(source_path):
        explicit_chapter = True
    if not explicit_chapter:
        return None
    for value in (
        event.get("chapter_number"),
        event.get("source_chapter_number"),
        event.get("trusted_issue"),
        event.get("trusted_issue_number"),
        source_chapter,
        canonical.get("canonical_issue_number"),
        event.get("canonical_issue_number"),
        event.get("normalized_number"),
        event.get("issue_number"),
    ):
        number = normalize_manga_number(value)
        if number:
            return number
    return None


def load_kapowarr_api_key():
    if not completed_import_kapowarr_adapter_enabled():
        raise RuntimeError("Kapowarr completed-import adapter is disabled")
    conn = sqlite_connect(KAPOWARR_DB)
    try:
        row = conn.execute("select value from config where key='api_key'").fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise RuntimeError("Kapowarr API key missing")
    return row[0]


def kapowarr_api(method, path, json_body=None, timeout=60):
    if not completed_import_kapowarr_adapter_enabled():
        raise RuntimeError("Kapowarr completed-import adapter is disabled")
    if not str(KAPOWARR_API or "").strip():
        raise RuntimeError("Kapowarr URL is not configured; set INKDROP_KAPOWARR_URL to use the Kapowarr adapter.")
    response = requests.request(
        method,
        KAPOWARR_API.rstrip("/") + "/" + path.lstrip("/"),
        params={"api_key": load_kapowarr_api_key()},
        json=json_body,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"Kapowarr API error: {payload.get('error')}")
    return payload.get("result")


def trigger_kapowarr_scan(volume_id):
    return kapowarr_api(
        "POST",
        "/system/tasks",
        json_body={"cmd": "refresh_and_scan", "volume_id": int(volume_id)},
        timeout=8,
    )


def kapowarr_tasks():
    result = kapowarr_api("GET", "/system/tasks", timeout=8)
    return result if isinstance(result, list) else []


def kapowarr_task_busy_for_volume(volume_id):
    volume_id = int(volume_id)
    for task in kapowarr_tasks():
        if not isinstance(task, dict):
            continue
        action = str(task.get("action") or "")
        if action in {"update_all", "search_all"}:
            return True
        try:
            task_volume_id = int(task.get("volume_id"))
        except (TypeError, ValueError):
            task_volume_id = None
        if task_volume_id == volume_id:
            return True
    return False


def queue_kapowarr_scan(volume_id):
    volume_id = int(volume_id)
    if not completed_import_kapowarr_adapter_enabled():
        return {"volume_id": volume_id, "skipped": "kapowarr_completed_import_adapter_disabled"}
    if kapowarr_task_busy_for_volume(volume_id):
        return {"volume_id": volume_id, "skipped": "kapowarr_task_already_running"}
    result = trigger_kapowarr_scan(volume_id)
    task_id = result.get("id") if isinstance(result, dict) else result
    return {"volume_id": volume_id, "task_id": task_id}


def kapowarr_issue_number_value(value):
    normalized = normalize_manga_number(value)
    if not normalized:
        return None
    try:
        return float(normalized)
    except (TypeError, ValueError):
        return None


def kapowarr_issue_id_for_item(conn, item):
    explicit = item.get("matched_kapowarr_issue_id")
    if explicit:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    volume_id = item.get("matched_kapowarr_id")
    if not volume_id:
        return None
    issue_number = None
    for key in ("issue_number", "canonical_issue_number", "normalized_number", "canonical_number"):
        issue_number = kapowarr_issue_number_value(item.get(key))
        if issue_number is not None:
            break
    if issue_number is None and item.get("dest"):
        issue_number = kapowarr_issue_number_value(extract_issue_number(item.get("dest")))
    if issue_number is None:
        return None
    row = conn.execute(
        """
        select id
        from issues
        where volume_id = ?
          and calculated_issue_number = ?
        limit 1
        """,
        (int(volume_id), issue_number),
    ).fetchone()
    return int(row[0]) if row else None


def kapowarr_manual_match_file(volume_id, kapowarr_path, issue_id):
    payload = [{
        "filepath": kapowarr_path,
        "issue_ids": [int(issue_id)],
        "general_file": False,
        "forced_match": True,
    }]
    return kapowarr_api(
        "PUT",
        f"/volumes/{int(volume_id)}/manualmatch",
        json_body=payload,
        timeout=20,
    )


def kapowarr_linked_host_path_for_item(conn, item):
    volume_id = item.get("matched_kapowarr_id") or item.get("kapowarr_volume_id")
    if not volume_id:
        return None
    try:
        volume_id = int(volume_id)
    except (TypeError, ValueError):
        return None
    issue_id = kapowarr_issue_id_for_item(conn, item)
    if not issue_id:
        return None
    row = conn.execute(
        """
        select f.filepath
        from files f
        join issues_files issue_link on issue_link.file_id = f.id
        join issues i on i.id = issue_link.issue_id
        where i.volume_id = ?
          and i.id = ?
        order by f.id desc
        limit 1
        """,
        (volume_id, int(issue_id)),
    ).fetchone()
    if not row or not row[0]:
        return None
    host_path = host_path_from_kavita_path(row[0])
    if not host_path:
        return None
    return Path(host_path)


def normalize_kavita_api_url(value):
    base_url = str(value or KAVITA_API).strip().rstrip("/")
    if not base_url:
        return ""
    if not base_url.startswith(("http://", "https://")):
        base_url = "http://" + base_url
    if not base_url.rstrip("/").lower().endswith("/api"):
        base_url = base_url.rstrip("/") + "/api"
    return base_url


def load_kavita_api_key_from_db():
    conn = sqlite_connect(KAVITA_DB)
    try:
        row = conn.execute(
            """
            select k.Key
            from AppUserAuthKey k
            join AspNetUserRoles ur on ur.UserId = k.AppUserId
            join AspNetRoles r on r.Id = ur.RoleId
            where r.NormalizedName = 'ADMIN'
              and k.Key is not null
              and length(k.Key) > 0
            order by k.LastAccessedAtUtc is null, k.LastAccessedAtUtc desc, k.CreatedAtUtc desc
            limit 1
            """
        ).fetchone()
        if not row:
            row = conn.execute(
                """
                select Key
                from AppUserAuthKey
                where Key is not null
                  and length(Key) > 0
                order by LastAccessedAtUtc is null, LastAccessedAtUtc desc, CreatedAtUtc desc
                limit 1
                """
            ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise RuntimeError("Kavita API key missing")
    return row[0]


def load_kavita_settings():
    config = provider_config("kavita") or {}
    if config and not config.get("enabled", True):
        raise RuntimeError("Kavita provider is disabled in InkDrop settings")
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    api_key = str(settings.get("api_key") or "").strip()
    if not api_key:
        api_key = str(load_kavita_api_key_from_db() or "").strip()
    if not api_key:
        raise RuntimeError("Kavita API key is not set in InkDrop settings or Kavita DB")
    base_url = normalize_kavita_api_url(config.get("base_url") or settings.get("base_url"))
    if not base_url:
        raise RuntimeError("Kavita URL is not configured; set INKDROP_KAVITA_URL or the Kavita provider base_url setting.")
    return {
        "enabled": bool(config.get("enabled", True)) if config else KAVITA_DB.exists(),
        "base_url": base_url,
        "api_key": api_key,
        "plugin_name": str(settings.get("plugin_name") or "inkdrop").strip() or "inkdrop",
        "scan_after_import": bool_setting(settings, "scan_after_import", True),
        "source": config.get("source") or ("inkdrop_state" if config else "kavita_db_fallback"),
    }


def load_kavita_api_key():
    return load_kavita_settings()["api_key"]


def kavita_visibility_adapter_enabled():
    return bool(provider_enabled("kavita", KAVITA_DB.exists()) and KAVITA_DB.exists())


def kavita_scan_after_import_enabled():
    if not frontend_sync_after_import_enabled():
        return False
    config = provider_config("kavita") or {}
    if config and not config.get("enabled", True):
        return False
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    return bool_setting(settings, "scan_after_import", True)


def sync_library_frontend_folders(folders, force_library_scan_folders=None, event_prefix=""):
    return inkdrop_library_frontends.sync_library_frontends(
        folders,
        frontend_sync_enabled=frontend_sync_after_import_enabled(),
        kavita_scan_enabled=kavita_scan_after_import_enabled(),
        trigger_kavita_scan=trigger_kavita_scan_folder,
        load_komga_settings=load_komga_settings,
        trigger_komga_scan=trigger_komga_scan_folder,
        force_library_scan_folders=force_library_scan_folders,
        log_event=log,
        event_prefix=event_prefix,
    )


def normalize_komga_api_url(value):
    base_url = str(value or KOMGA_API).strip().rstrip("/")
    if not base_url:
        return ""
    if not base_url.startswith(("http://", "https://")):
        base_url = "http://" + base_url
    lowered = base_url.rstrip("/").lower()
    if lowered.endswith("/api/v1"):
        return base_url.rstrip("/")
    if lowered.endswith("/api"):
        return base_url.rstrip("/") + "/v1"
    return base_url.rstrip("/") + "/api/v1"


def load_komga_settings():
    config = provider_config("komga") or {}
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    legacy_scan_after_import = bool_setting(settings, "scan_after_import", True)
    add_after_import = bool_setting(settings, "add_after_import", legacy_scan_after_import)
    base_url = normalize_komga_api_url(config.get("base_url") or settings.get("base_url"))
    enabled = bool(config.get("enabled", False))
    if enabled and not base_url:
        raise RuntimeError("Komga URL is not configured; set INKDROP_KOMGA_URL or the Komga provider base_url setting.")
    return {
        "enabled": enabled,
        "base_url": base_url,
        "username": str(settings.get("username") or "").strip(),
        "password": str(settings.get("password") or "").strip(),
        "add_after_import": add_after_import,
        "legacy_scan_after_import": legacy_scan_after_import,
        # Komga add-after-import is the modern operator switch; scan_after_import
        # remains as a compatibility alias for older saved configs and workers.
        "scan_after_import": add_after_import,
        "comic_library_ids": list_setting(settings, "comic_library_ids"),
        "manga_library_ids": list_setting(settings, "manga_library_ids"),
        "komga_comic_root": text_setting(settings, "komga_comic_root", KAVITA_COMIC_ROOT).rstrip("/"),
        "komga_manga_root": text_setting(settings, "komga_manga_root", KAVITA_MANGA_ROOT).rstrip("/"),
        "timeout_seconds": int_setting(settings, "timeout_seconds", 8, minimum=1, maximum=60),
        "source": config.get("source") or ("inkdrop_state" if config else "runtime"),
    }


def host_folder_to_komga(path, settings=None):
    settings = settings if isinstance(settings, dict) else load_komga_settings()
    return inkdrop_library_frontends.host_path_to_frontend_path(
        path,
        comic_root=COMIC_ROOT,
        manga_root=MANGA_ROOT,
        frontend_comic_root=settings["komga_comic_root"],
        frontend_manga_root=settings["komga_manga_root"],
    )


def host_path_to_komga(path, settings=None):
    settings = settings if isinstance(settings, dict) else load_komga_settings()
    return inkdrop_library_frontends.host_path_to_frontend_path(
        path,
        comic_root=COMIC_ROOT,
        manga_root=MANGA_ROOT,
        frontend_comic_root=settings["komga_comic_root"],
        frontend_manga_root=settings["komga_manga_root"],
    )


def komga_library_ids_for_host_folder(host_folder, settings=None):
    settings = settings if isinstance(settings, dict) else load_komga_settings()
    return inkdrop_library_frontends.library_ids_for_host_folder(
        host_folder,
        settings,
        comic_root=COMIC_ROOT,
        manga_root=MANGA_ROOT,
    )


def trigger_komga_scan_folder(host_folder):
    return inkdrop_library_frontends.trigger_komga_scan_folder(
        host_folder,
        settings=load_komga_settings(),
        comic_root=COMIC_ROOT,
        manga_root=MANGA_ROOT,
    )


def komga_paginated_content(payload):
    return inkdrop_library_frontends.komga_paginated_content(payload)


def komga_book_path_values(book):
    return inkdrop_library_frontends.komga_book_path_values(book)


def normalized_adapter_path(value):
    return inkdrop_library_frontends.normalized_adapter_path(value)


def komga_book_matches_path(book, host_path, komga_path):
    return inkdrop_library_frontends.komga_book_matches_path(book, host_path, komga_path)


def komga_search_books(settings, query, library_ids, limit=25):
    settings = settings if isinstance(settings, dict) else load_komga_settings()
    return inkdrop_library_frontends.komga_search_books(settings, query, library_ids, limit=limit)


def komga_list_books_page(settings, library_ids, page=0, limit=100):
    settings = settings if isinstance(settings, dict) else load_komga_settings()
    return inkdrop_library_frontends.komga_list_books_page(settings, library_ids, page=page, limit=limit)


def komga_file_visible_for_host_path(host_path, settings=None):
    settings = settings if isinstance(settings, dict) else load_komga_settings()
    return inkdrop_library_frontends.komga_file_visible_for_host_path(
        host_path,
        settings=settings,
        comic_root=COMIC_ROOT,
        manga_root=MANGA_ROOT,
    )


def kavita_plugin_token():
    return inkdrop_library_frontends.kavita_plugin_token(load_kavita_settings())


def host_folder_to_kavita(path):
    return inkdrop_library_frontends.host_path_to_frontend_path(
        path,
        comic_root=COMIC_ROOT,
        manga_root=MANGA_ROOT,
        frontend_comic_root=KAVITA_COMIC_ROOT,
        frontend_manga_root=KAVITA_MANGA_ROOT,
    )


def host_path_to_kavita(path):
    return inkdrop_library_frontends.host_path_to_frontend_path(
        path,
        comic_root=COMIC_ROOT,
        manga_root=MANGA_ROOT,
        frontend_comic_root=KAVITA_COMIC_ROOT,
        frontend_manga_root=KAVITA_MANGA_ROOT,
    )


def kavita_file_visible_for_host_path(host_path, conn=None, expectation=None):
    return inkdrop_library_frontends.kavita_file_visible_for_host_path(
        host_path,
        conn=conn,
        comic_root=COMIC_ROOT,
        manga_root=MANGA_ROOT,
        kavita_comic_root=KAVITA_COMIC_ROOT,
        kavita_manga_root=KAVITA_MANGA_ROOT,
        expectation=expectation,
    )


def approve_reader_binding(work_id, reader_series_id, reader_library_id, *, now=None):
    """Persist an explicit operator-approved work-to-Kavita binding.

    Import item metadata and folder matches are deliberately not accepted here;
    this producer is reached only by the dedicated operator CLI command.
    """
    if inkdrop_state is None:
        return {"ok": False, "reason": "state_unavailable"}
    result = inkdrop_state.persist_reader_binding(
        INKDROP_STATE_DB,
        work_id,
        reader_series_id,
        reader_library_id,
        source="operator_approved_cli",
        now=now,
    )
    return {**result, "producer": "completed_import_operator_cli"}


def reader_expectation_for_import(item, dest_path, kavita_conn=None):
    item = item if isinstance(item, dict) else {}
    source_unit = str(item.get("source_unit") or item.get("unit_type") or "").strip().lower()
    number = item.get("normalized_number") or item.get("canonical_issue_number") or item.get("issue_number")
    record = {
        **item,
        "work_id": completion_native_series_id(item),
        "unit_type": source_unit,
        "issue_number": number if source_unit == "issue" else item.get("issue_number"),
        "chapter_number": item.get("chapter_number") or number if source_unit == "chapter" else item.get("chapter_number"),
        "volume_number": item.get("source_volume_number") or item.get("volume_number") or number if source_unit == "volume" else item.get("volume_number"),
        "collected_number": item.get("collected_number") or number if source_unit in {"collected", "collected_edition"} else item.get("collected_number"),
        "pack_member_number": item.get("pack_member_number") or number if source_unit in {"pack", "pack_member"} else item.get("pack_member_number"),
    }
    work_id = str(record.get("work_id") or "").strip()
    binding = inkdrop_state.reader_binding_for_work(INKDROP_STATE_DB, work_id) if inkdrop_state is not None and work_id else {}
    record.pop("kavita_series_id", None)
    record.pop("kavita_library_id", None)
    if binding:
        record["kavita_series_id"] = binding["reader_series_id"]
        record["kavita_library_id"] = binding["reader_library_id"]
    candidate_series_ids = []
    candidate_library_id = None
    if kavita_conn is not None:
        try:
            frontend_folder = host_folder_to_kavita(Path(dest_path).parent)
            series_rows = kavita_conn.execute("select Id, FolderPath, LowestFolderPath from Series").fetchall()
            library_rows = kavita_conn.execute("select LibraryId, Path from FolderPath").fetchall()
            candidate_series_ids = inkdrop_library_frontends.kavita_series_ids_for_folder(frontend_folder, series_rows)
            candidate_library_id = inkdrop_library_frontends.kavita_library_id_for_folder(frontend_folder, library_rows)
        except Exception:
            pass
    if item.get("truth_model") == "kavita_manga":
        record["canonical_media_type"] = "manga"
        record["media_type"] = "manga"
        record.pop("work_media_type", None)
        record.pop("series_media_type", None)
    expectation = inkdrop_library_identity.reader_expectation(
        record,
        filename=Path(dest_path).name,
        series_folder=Path(dest_path).parent.name,
    )
    expectation["candidate_reader_series_ids"] = candidate_series_ids
    expectation["candidate_reader_library_id"] = candidate_library_id
    expectation["binding_status"] = "authoritative" if binding else "manual_review_required"
    return expectation


def host_path_to_kapowarr(path):
    path = Path(path)
    rel = path.relative_to(COMIC_ROOT)
    return f"{KAPOWARR_COMIC_ROOT}/{rel.as_posix()}"


def trigger_kavita_library_scan(folder, mode, folder_scan_status_code=None, details=None):
    return inkdrop_library_frontends.trigger_kavita_library_scan(
        folder,
        settings=load_kavita_settings(),
        library_id_for_folder=kavita_library_id_for_folder,
        mode=mode,
        folder_scan_status_code=folder_scan_status_code,
        details=details,
    )


def trigger_kavita_scan_folder(host_folder, force_library_scan=False):
    return inkdrop_library_frontends.trigger_kavita_scan_folder(
        host_folder,
        settings=load_kavita_settings(),
        comic_root=COMIC_ROOT,
        manga_root=MANGA_ROOT,
        kavita_comic_root=KAVITA_COMIC_ROOT,
        kavita_manga_root=KAVITA_MANGA_ROOT,
        force_library_scan=force_library_scan,
        series_ids_for_folder=kavita_series_ids_for_folder,
        library_id_for_folder=kavita_library_id_for_folder,
    )


def kavita_series_ids_for_folder(folder):
    conn = sqlite_connect(KAVITA_DB)
    try:
        rows = conn.execute("select Id, FolderPath, LowestFolderPath from Series").fetchall()
    finally:
        conn.close()
    return inkdrop_library_frontends.kavita_series_ids_for_folder(folder, rows)


def kavita_library_id_for_folder(folder):
    conn = sqlite_connect(KAVITA_DB)
    try:
        rows = conn.execute("select LibraryId, Path from FolderPath").fetchall()
    finally:
        conn.close()
    return inkdrop_library_frontends.kavita_library_id_for_folder(folder, rows)


def kapowarr_missing_counts(volume_ids):
    if not volume_ids or not completed_import_kapowarr_adapter_enabled():
        return {}
    conn = sqlite_connect(KAPOWARR_DB)
    try:
        counts = {}
        for volume_id in sorted({int(v) for v in volume_ids}):
            row = conn.execute(
                """
                select
                    v.title,
                    count(i.id),
                    sum(case when exists (
                        select 1 from issues_files issue_link
                        where issue_link.issue_id = i.id
                    ) then 0 else 1 end)
                from volumes v
                left join issues i on i.volume_id = v.id and i.monitored = 1
                where v.id = ?
                group by v.id
                """,
                (volume_id,),
            ).fetchone()
            if row:
                counts[str(volume_id)] = {
                    "title": row[0],
                    "monitored": int(row[1] or 0),
                    "missing": int(row[2] or 0),
                }
        return counts
    finally:
        conn.close()


def manga_issue_number_from_item(item):
    for key in ("normalized_number", "canonical_issue_number", "canonical_number", "issue_number"):
        number = normalize_manga_number(item.get(key))
        if number:
            return number
    dest = item.get("dest")
    if dest:
        number = normalize_manga_number(extract_issue_number(Path(dest)))
        if number:
            return number
    return None


COMPLETED_IMPORT_VERIFICATION_STATUSES = {"folder_verified", "library_visible", "kavita_verified"}


def import_verification_satisfied(result):
    return str((result or {}).get("verification_status") or "").strip().lower() in COMPLETED_IMPORT_VERIFICATION_STATUSES


def record_manga_completion(item, result):
    if result.get("truth_model") != "kavita_manga" or not import_verification_satisfied(result):
        return False
    normalized_number = manga_issue_number_from_item(item)
    if not normalized_number:
        return False
    series_title = item.get("matched_series") or result.get("series")
    normalized_series = normalize_series(series_title)
    if not normalized_series:
        return False
    ids = completion_native_ids(item, result)
    metadata_ids = completion_metadata_identity_fields(item, result)
    now = time.time()
    conn = connect()
    try:
        conn.execute(
            """
            insert into manga_completion (
              series_title, normalized_series, native_series_id, native_issue_id, metadata_provider, metadata_id,
              kapowarr_volume_id, kapowarr_issue_id,
              issue_number, normalized_number, truth_model, target_file_path, source_path,
              sha256, comicinfo_status, kavita_visibility_status, verification_status,
              review_id, completed_at, updated_at
            ) values (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            on conflict(normalized_series, normalized_number, truth_model) do update set
              series_title=excluded.series_title,
              native_series_id=coalesce(excluded.native_series_id, manga_completion.native_series_id),
              native_issue_id=coalesce(excluded.native_issue_id, manga_completion.native_issue_id),
              metadata_provider=coalesce(excluded.metadata_provider, manga_completion.metadata_provider),
              metadata_id=coalesce(excluded.metadata_id, manga_completion.metadata_id),
              kapowarr_volume_id=coalesce(excluded.kapowarr_volume_id, manga_completion.kapowarr_volume_id),
              kapowarr_issue_id=coalesce(excluded.kapowarr_issue_id, manga_completion.kapowarr_issue_id),
              issue_number=excluded.issue_number,
              target_file_path=excluded.target_file_path,
              source_path=excluded.source_path,
              sha256=excluded.sha256,
              comicinfo_status=excluded.comicinfo_status,
              kavita_visibility_status=excluded.kavita_visibility_status,
              verification_status=excluded.verification_status,
              review_id=excluded.review_id,
              updated_at=excluded.updated_at
            """,
            (
                series_title,
                normalized_series,
                ids.get("native_series_id"),
                ids.get("native_issue_id"),
                metadata_ids.get("metadata_provider"),
                metadata_ids.get("metadata_id"),
                item.get("matched_kapowarr_id") or result.get("volume_id"),
                item.get("matched_kapowarr_issue_id"),
                str(item.get("issue_number") or item.get("canonical_issue_number") or normalized_number),
                normalized_number,
                "kavita_manga",
                item.get("dest") or result.get("dest"),
                item.get("source"),
                item.get("sha256"),
                result.get("comicinfo_status"),
                "visible" if result.get("library_visible") or result.get("kavita_visible") else "not_visible",
                result.get("verification_status"),
                item.get("review_id"),
                now,
                now,
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def record_manga_unit_completion(item, result):
    if not auto_inspect_completion_allowed(item, result):
        return False
    if result.get("truth_model") != "kavita_manga" or not import_verification_satisfied(result):
        return False
    unit_model = (item.get("source_unit") or result.get("source_unit") or item.get("manga_unit_model") or result.get("manga_unit_model") or "volume").strip().lower()
    if unit_model in {"mixed_volume_preferred", "mixed_chapter_preferred"}:
        unit_model = "volume"
    if unit_model not in MANGA_UNIT_MODELS or unit_model == "unknown/manual":
        return False
    normalized_number = manga_issue_number_from_item(item)
    if not normalized_number:
        return False
    series_title = item.get("matched_series") or result.get("series")
    normalized_series = normalize_series(series_title)
    if not normalized_series:
        return False
    ids = completion_native_ids(item, result)
    metadata_ids = completion_metadata_identity_fields(item, result)
    now = time.time()
    conn = connect()
    try:
        ensure_manga_unit_schema(conn)
        conn.execute(
            """
            insert into manga_unit_completion (
              series_title, normalized_series, native_series_id, native_issue_id, metadata_provider, metadata_id,
              kapowarr_volume_id, kapowarr_issue_id,
              issue_number, normalized_number, manga_unit_model, truth_model, target_file_path,
              source_path, sha256, comicinfo_status, kavita_visibility_status,
              verification_status, review_id, completed_at, updated_at
            ) values (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            on conflict(normalized_series, normalized_number, manga_unit_model, truth_model) do update set
              series_title=excluded.series_title,
              native_series_id=coalesce(excluded.native_series_id, manga_unit_completion.native_series_id),
              native_issue_id=coalesce(excluded.native_issue_id, manga_unit_completion.native_issue_id),
              metadata_provider=coalesce(excluded.metadata_provider, manga_unit_completion.metadata_provider),
              metadata_id=coalesce(excluded.metadata_id, manga_unit_completion.metadata_id),
              kapowarr_volume_id=coalesce(excluded.kapowarr_volume_id, manga_unit_completion.kapowarr_volume_id),
              kapowarr_issue_id=coalesce(excluded.kapowarr_issue_id, manga_unit_completion.kapowarr_issue_id),
              issue_number=excluded.issue_number,
              target_file_path=excluded.target_file_path,
              source_path=excluded.source_path,
              sha256=excluded.sha256,
              comicinfo_status=excluded.comicinfo_status,
              kavita_visibility_status=excluded.kavita_visibility_status,
              verification_status=excluded.verification_status,
              review_id=excluded.review_id,
              updated_at=excluded.updated_at
            """,
            (
                series_title,
                normalized_series,
                ids.get("native_series_id"),
                ids.get("native_issue_id"),
                metadata_ids.get("metadata_provider"),
                metadata_ids.get("metadata_id"),
                item.get("matched_kapowarr_id") or result.get("volume_id"),
                item.get("matched_kapowarr_issue_id") if unit_model != "chapter" else None,
                str(item.get("issue_number") or item.get("canonical_issue_number") or normalized_number),
                normalized_number,
                unit_model,
                "kavita_manga",
                item.get("dest") or result.get("dest"),
                item.get("source"),
                item.get("sha256"),
                result.get("comicinfo_status"),
                "visible" if result.get("library_visible") or result.get("kavita_visible") else "not_visible",
                result.get("verification_status"),
                item.get("review_id"),
                now,
                now,
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def mangadex_volume_aggregate(mangadex_series_id, timeout=20):
    """Fetch MangaDex's real volume->chapter grouping for a series (the /aggregate
    endpoint), cached to a state-dir JSON file for MANGADEX_VOLUME_AGGREGATE_CACHE_TTL_SECONDS.
    Returns the raw ``volumes`` dict from MangaDex, or None if unavailable/unreachable.
    Never guesses at coverage from filenames -- absence of real data means "unknown".
    """
    mangadex_series_id = str(mangadex_series_id or "").strip()
    if not mangadex_series_id:
        return None
    cache = read_json_file(MANGADEX_VOLUME_AGGREGATE_CACHE_FILE, {})
    cache = cache if isinstance(cache, dict) else {}
    entry = cache.get(mangadex_series_id)
    if isinstance(entry, dict) and isinstance(entry.get("volumes"), dict):
        try:
            cached_at = float(entry.get("cached_at") or 0)
        except (TypeError, ValueError):
            cached_at = 0.0
        if time.time() - cached_at < MANGADEX_VOLUME_AGGREGATE_CACHE_TTL_SECONDS:
            return entry["volumes"]
    try:
        response = requests.get(
            f"{MANGADEX_API}/manga/{mangadex_series_id}/aggregate",
            headers={"User-Agent": MANGADEX_USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.exceptions.RequestException, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("result") != "ok":
        return None
    volumes = payload.get("volumes")
    if not isinstance(volumes, dict):
        return None
    cache[mangadex_series_id] = {"volumes": volumes, "cached_at": time.time()}
    try:
        write_json_atomic(MANGADEX_VOLUME_AGGREGATE_CACHE_FILE, cache)
    except OSError:
        pass
    return volumes


def mangadex_volume_covered_chapters(mangadex_series_id, volume_number, timeout=20):
    """Real chapter numbers (normalized, e.g. "007") that MangaDex's aggregate data
    says `volume_number` of `mangadex_series_id` collects. Returns None (not an empty
    list) when the volume isn't present in MangaDex's data at all -- an empty/unknown
    result must never be treated as "covers nothing", since licensed series routinely
    have no scanlation coverage on MangaDex for volumes readers already own physically.
    """
    normalized_volume = normalize_manga_number(volume_number)
    if not normalized_volume:
        return None
    volumes = mangadex_volume_aggregate(mangadex_series_id, timeout=timeout)
    if not volumes:
        return None
    for key, entry in volumes.items():
        if key == "none" or normalize_manga_number(key) != normalized_volume:
            continue
        chapters = entry.get("chapters") if isinstance(entry, dict) else None
        if not isinstance(chapters, dict):
            return None
        numbers = set()
        for chapter_key in chapters.keys():
            if chapter_key == "none":
                continue
            normalized_chapter = normalize_manga_number(chapter_key)
            # Bonus/side chapters (e.g. "340.5") are real chapters but a print
            # volume never collects them alongside the whole-numbered run, so
            # they must not count as "covered by this volume" -- exclude them
            # explicitly rather than relying on normalize_manga_number to drop
            # decimals (it no longer does; it now preserves them for matching).
            if normalized_chapter and "." not in normalized_chapter:
                numbers.add(normalized_chapter)
        return sorted(numbers) if numbers else None
    return None


def record_manga_coverage(item, result):
    if not auto_inspect_completion_allowed(item, result):
        return False
    if result.get("truth_model") != "kavita_manga" or not import_verification_satisfied(result):
        return False
    unit_type = (item.get("source_unit") or result.get("source_unit") or item.get("manga_unit_model") or result.get("manga_unit_model") or "volume").strip().lower()
    if unit_type in {"mixed_volume_preferred", "mixed_chapter_preferred"}:
        unit_type = "volume"
    if unit_type not in {"volume", "chapter", "pack"}:
        return False
    normalized_number = manga_issue_number_from_item(item)
    if not normalized_number:
        return False
    series_title = item.get("matched_series") or result.get("series")
    normalized_series = normalize_series(series_title)
    if not normalized_series:
        return False
    ids = completion_native_ids(item, result)
    metadata_ids = completion_metadata_identity_fields(item, result)
    source_quality = "fallback" if unit_type == "chapter" and manga_policy_prefers_volume(item.get("manga_unit_model")) else "preferred"
    covers_from = item.get("covers_from")
    covers_to = item.get("covers_to")
    covered_chapter_numbers = item.get("covered_chapter_numbers")
    range_source = item.get("range_source")
    if unit_type in {"volume", "pack"} and not covered_chapter_numbers and metadata_ids.get("metadata_provider") == "mangadex" and metadata_ids.get("metadata_id"):
        derived = mangadex_volume_covered_chapters(metadata_ids.get("metadata_id"), normalized_number)
        if derived:
            covered_chapter_numbers = derived
            covers_from = derived[0]
            covers_to = derived[-1]
            range_source = "mangadex_aggregate"
    now = time.time()
    conn = connect()
    try:
        ensure_manga_coverage_schema(conn)
        conn.execute(
            """
            insert into manga_coverage (
              series_title, normalized_series, native_series_id, native_issue_id, metadata_provider, metadata_id,
              kapowarr_volume_id, kapowarr_issue_id,
              unit_type, issue_number, normalized_number, covers_from, covers_to,
              source_quality, truth_model, target_file_path, source_path, sha256,
              comicinfo_status, kavita_visibility_status, verification_status,
              replacement_status, review_id, completed_at, updated_at,
              covered_chapter_numbers_json, range_source
            ) values (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            on conflict(normalized_series, unit_type, normalized_number, truth_model) do update set
              series_title=excluded.series_title,
              native_series_id=coalesce(excluded.native_series_id, manga_coverage.native_series_id),
              native_issue_id=coalesce(excluded.native_issue_id, manga_coverage.native_issue_id),
              metadata_provider=coalesce(excluded.metadata_provider, manga_coverage.metadata_provider),
              metadata_id=coalesce(excluded.metadata_id, manga_coverage.metadata_id),
              kapowarr_volume_id=coalesce(excluded.kapowarr_volume_id, manga_coverage.kapowarr_volume_id),
              kapowarr_issue_id=coalesce(excluded.kapowarr_issue_id, manga_coverage.kapowarr_issue_id),
              issue_number=excluded.issue_number,
              covers_from=coalesce(excluded.covers_from, manga_coverage.covers_from),
              covers_to=coalesce(excluded.covers_to, manga_coverage.covers_to),
              source_quality=excluded.source_quality,
              target_file_path=excluded.target_file_path,
              source_path=excluded.source_path,
              sha256=excluded.sha256,
              comicinfo_status=excluded.comicinfo_status,
              kavita_visibility_status=excluded.kavita_visibility_status,
              verification_status=excluded.verification_status,
              replacement_status=coalesce(excluded.replacement_status, manga_coverage.replacement_status),
              review_id=excluded.review_id,
              updated_at=excluded.updated_at,
              covered_chapter_numbers_json=coalesce(excluded.covered_chapter_numbers_json, manga_coverage.covered_chapter_numbers_json),
              range_source=coalesce(excluded.range_source, manga_coverage.range_source)
            """,
            (
                series_title,
                normalized_series,
                ids.get("native_series_id"),
                ids.get("native_issue_id"),
                metadata_ids.get("metadata_provider"),
                metadata_ids.get("metadata_id"),
                item.get("matched_kapowarr_id") or result.get("volume_id"),
                item.get("matched_kapowarr_issue_id") if unit_type != "chapter" else None,
                unit_type,
                str(item.get("issue_number") or item.get("canonical_issue_number") or normalized_number),
                normalized_number,
                covers_from,
                covers_to,
                source_quality,
                "kavita_manga",
                item.get("dest") or result.get("dest"),
                item.get("source"),
                item.get("sha256"),
                result.get("comicinfo_status"),
                "visible" if result.get("library_visible") or result.get("kavita_visible") else "not_visible",
                result.get("verification_status"),
                item.get("replacement_status"),
                item.get("review_id"),
                now,
                now,
                json.dumps(covered_chapter_numbers) if covered_chapter_numbers else None,
                range_source,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    if unit_type in {"volume", "pack"} and range_source:
        # Scope by series_title/normalized_series, not native_series_id: chapters
        # and volumes for the same series are routinely tagged by different
        # metadata providers (a raw scanlation chapter may carry no mangadex id
        # at all), so requiring an exact native_series_id match would silently
        # miss chapters recorded under a different or absent provenance.
        try:
            sweep_chapters_satisfied_by_volumes(series_title=series_title)
        except Exception as exc:
            log({"event": "chapter_volume_redundancy_sweep_failed", "series": series_title, "error": f"{type(exc).__name__}: {exc}"})
    return True


def record_collection_completion(item, result):
    if result.get("truth_model") != COLLECTION_TRUTH_MODEL or not import_verification_satisfied(result):
        return 0
    collection = item.get("collection") or {}
    covered = collection.get("range") or item.get("collection_range")
    if not covered:
        return 0
    start, end = int(covered[0]), int(covered[1])
    series_title = item.get("matched_series") or result.get("series") or collection.get("series")
    normalized_series = normalize_series(series_title)
    if not normalized_series:
        return 0
    ids = completion_native_ids(item, result)
    metadata_ids = completion_metadata_identity_fields(item, result)
    issue_ids = {}
    volume_id = item.get("matched_kapowarr_id") or result.get("volume_id")
    if volume_id and completed_import_kapowarr_adapter_enabled():
        conn_k = sqlite_connect(KAPOWARR_DB)
        conn_k.row_factory = sqlite3.Row
        try:
            for row in conn_k.execute(
                "select id, issue_number, calculated_issue_number from issues where volume_id=?",
                (int(volume_id),),
            ):
                number = normalize_manga_number(row["calculated_issue_number"] or row["issue_number"])
                if number:
                    issue_ids[number] = row["id"]
        finally:
            conn_k.close()
    now = time.time()
    conn = connect()
    written = 0
    try:
        for issue in range(start, end + 1):
            normalized_number = f"{issue:03d}"
            conn.execute(
                """
                insert into collection_completion (
                  series_title, normalized_series, native_series_id, native_issue_id, metadata_provider, metadata_id,
                  kapowarr_volume_id, kapowarr_issue_id,
                  issue_number, normalized_number, truth_model, collection_title, collection_range,
                  target_file_path, source_path, sha256, comicinfo_status, kavita_visibility_status,
                  verification_status, review_id, completed_at, updated_at
                ) values (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                on conflict(normalized_series, normalized_number, truth_model) do update set
                  series_title=excluded.series_title,
                  native_series_id=coalesce(excluded.native_series_id, collection_completion.native_series_id),
                  native_issue_id=coalesce(excluded.native_issue_id, collection_completion.native_issue_id),
                  metadata_provider=coalesce(excluded.metadata_provider, collection_completion.metadata_provider),
                  metadata_id=coalesce(excluded.metadata_id, collection_completion.metadata_id),
                  kapowarr_volume_id=coalesce(excluded.kapowarr_volume_id, collection_completion.kapowarr_volume_id),
                  kapowarr_issue_id=coalesce(excluded.kapowarr_issue_id, collection_completion.kapowarr_issue_id),
                  collection_title=excluded.collection_title,
                  collection_range=excluded.collection_range,
                  target_file_path=excluded.target_file_path,
                  source_path=excluded.source_path,
                  sha256=excluded.sha256,
                  comicinfo_status=excluded.comicinfo_status,
                  kavita_visibility_status=excluded.kavita_visibility_status,
                  verification_status=excluded.verification_status,
                  review_id=excluded.review_id,
                  updated_at=excluded.updated_at
                """,
                (
                    series_title,
                    normalized_series,
                    ids.get("native_series_id"),
                    ids.get("native_issue_id"),
                    metadata_ids.get("metadata_provider"),
                    metadata_ids.get("metadata_id"),
                    volume_id,
                    issue_ids.get(normalized_number),
                    str(issue),
                    normalized_number,
                    COLLECTION_TRUTH_MODEL,
                    collection.get("collection_title") or item.get("collection_title") or "Collection",
                    f"{start}-{end}",
                    item.get("dest") or result.get("dest"),
                    item.get("source"),
                    item.get("sha256"),
                    result.get("comicinfo_status"),
                    "visible" if result.get("library_visible") or result.get("kavita_visible") else "not_visible",
                    result.get("verification_status"),
                    item.get("review_id"),
                    now,
                    now,
                ),
            )
            written += 1
        conn.commit()
        return written
    finally:
        conn.close()


def library_visible_for_result(item):
    return bool(item.get("library_visible") or item.get("kavita_visible") or item.get("komga_visible"))


def verification_status_for(item):
    if not item.get("host_exists"):
        return "verification_failed"
    if item.get("kapowarr_status") == "not_linked":
        # Kapowarr is the truth source for this row (a truth anchor and a
        # manual-match attempt exist), but the match failed or was never
        # confirmed linked. Without this, the row could pass verification on
        # local-file/library-visibility grounds alone while Kapowarr's own
        # tracking still shows the issue missing forever.
        return "verification_failed"
    library_required = bool(item.get("library_visibility_required"))
    library_visible = library_visible_for_result(item)
    folder_valid = True
    if item.get("truth_model") == "kavita_manga":
        if item.get("comicinfo_status") != "present":
            return "verification_failed"
        if library_visible:
            return "library_visible" if library_required else "folder_verified"
        return "waiting_for_library_scan" if library_required else "folder_verified"
    if item.get("truth_model") == COLLECTION_TRUTH_MODEL:
        if item.get("comicinfo_status") != "present":
            return "verification_failed"
        if library_visible:
            return "library_visible" if library_required else "folder_verified"
        return "waiting_for_library_scan" if library_required else "folder_verified"
    if item.get("comicinfo_status") == "not_checked" and item.get("host_exists"):
        folder_valid = True
    elif item.get("comicinfo_status") == "unreadable":
        folder_valid = False
    if library_visible:
        return "library_visible" if library_required else "folder_verified"
    if item.get("host_exists") and folder_valid:
        return "waiting_for_library_scan" if library_required else "folder_verified"
    if item.get("comicinfo_status") != "present":
        return "verification_failed"
    return "verification_failed"


def reader_completion_truth_for(item):
    item = item if isinstance(item, dict) else {}
    projection = inkdrop_library_identity.completion_projection({
        "downloaded": bool(item.get("downloaded") or item.get("host_exists")),
        "artifact_verified": bool(item.get("artifact_verified") or item.get("host_exists")),
        "imported": bool(item.get("host_exists")),
        "reader_configured": bool(item.get("reader_configured") or item.get("library_visibility_checks_enabled")),
        "reader_required": bool(item.get("library_visibility_required")),
        "reader_scan_requested": bool(item.get("reader_scan_requested") or item.get("scan_requested")),
        "reader_visibility_status": item.get("library_visibility_status"),
    })
    return {
        "reader_completion_state": projection["state"],
        "reader_completion_complete": projection["complete"],
    }


def completion_has_kapowarr_truth_anchor(item):
    if not isinstance(item, dict):
        return False
    if item.get("matched_kapowarr_id") not in (None, ""):
        return True
    if item.get("kapowarr_volume_id") not in (None, ""):
        return True
    if str(item.get("target_source") or "").strip().lower() == "kapowarr_adapter":
        return True
    source_of_truth = str(item.get("source_of_truth") or item.get("source_of_truth_hint") or "").strip().lower()
    if source_of_truth not in {"metadata_adapter", "kapowarr_adapter"}:
        return False
    adapter_provider = str(
        item.get("adapter_provider")
        or item.get("metadata_provider")
        or item.get("metadataAdapter")
        or item.get("metadata_adapter")
        or ""
    ).strip().lower()
    return adapter_provider == "kapowarr"


def completion_has_native_truth_anchor(item):
    if not isinstance(item, dict):
        return False
    has_adapter_context = _is_kapowarr_adapter_context(item)

    for key in ("native_series_id", "inkdrop_series_id", "comicvine_series_id"):
        if item.get(key) not in (None, ""):
            return True
    if has_adapter_context:
        return False
    for key in ("series_id", "seriesId"):
        if item.get(key) not in (None, ""):
            return True
    provider = str(item.get("metadata_provider") or item.get("metadataProvider") or "").strip().lower()
    metadata_id = str(
        item.get("metadata_id")
        or item.get("metadataId")
        or item.get("trusted_series_id")
        or item.get("trustedSeriesId")
        or ""
    ).strip()
    return provider in {"comicvine", "mangadex"} and bool(metadata_id)


def infer_import_truth_model(item):
    explicit = str(item.get("truth_model") or "").strip().lower()
    if explicit:
        if (
            is_kapowarr_truth_model(explicit)
            and not completion_has_kapowarr_truth_anchor(item)
            and completion_has_native_truth_anchor(item)
        ):
            return "inkdrop_native"
        return explicit
    source = str(item.get("source") or "").strip().lower()
    if source == "kapowarr":
        return "kapowarr" if completion_has_kapowarr_truth_anchor(item) else "inkdrop_native"
    if not completion_has_kapowarr_truth_anchor(item):
        return "inkdrop_native"
    source_of_truth = str(item.get("source_of_truth") or item.get("source_of_truth_hint") or "").strip().lower()
    adapter_provider = str(
        item.get("adapter_provider")
        or item.get("metadata_provider")
        or item.get("metadataAdapter")
        or item.get("metadata_adapter")
        or ""
    ).strip().lower()
    if source_of_truth in {"metadata_adapter", "kapowarr_adapter"} and adapter_provider == "kapowarr":
        return "kapowarr"
    return "inkdrop_native"


def is_kapowarr_truth_model(truth_model):
    if truth_model in {None, "", "kapowarr"}:
        return truth_model == "kapowarr"
    value = str(truth_model).strip().lower()
    if not value.startswith("kapowarr_") and value not in {"kapowarr", "kapowarr_comic"}:
        return False
    return True


def verify_imported_items(
    imported,
    poll_kavita=None,
    poll_interval=MANGA_SCAN_POLL_SECONDS,
    timeout=MANGA_SCAN_TIMEOUT_SECONDS,
    poll_library_visibility=None,
):
    if poll_library_visibility is None:
        poll_library_visibility = bool(poll_kavita)
    else:
        poll_library_visibility = bool(poll_library_visibility)
    kapowarr_adapter_enabled = completed_import_kapowarr_adapter_enabled()
    library_visibility_required = media_management_library_visibility_required()
    library_visibility_checks_enabled = bool(
        library_visibility_required
        or poll_library_visibility
        or media_management_library_visibility_checks_enabled()
    )
    kavita_visibility_enabled = bool(library_visibility_checks_enabled and kavita_visibility_adapter_enabled())
    volume_ids = set()
    if kapowarr_adapter_enabled:
        for item in imported:
            volume_id = item.get("matched_kapowarr_id")
            if volume_id:
                volume_ids.add(int(volume_id))
    missing_counts = kapowarr_missing_counts(volume_ids)
    try:
        komga_settings = load_komga_settings()
    except Exception as exc:
        komga_settings = {"enabled": False, "settings_error": str(exc)}
        log({"event": "komga_settings_load_failed", "error": f"{type(exc).__name__}: {exc}"})
    komga_visibility_enabled = bool(
        library_visibility_checks_enabled
        and
        komga_settings.get("enabled")
        and komga_settings.get("username")
        and komga_settings.get("password")
    )

    def check_once():
        checked = []
        kapowarr_conn = sqlite_connect(KAPOWARR_DB) if kapowarr_adapter_enabled else None
        kavita_conn = sqlite_connect(KAVITA_DB) if kavita_visibility_enabled else None
        try:
            for item in imported:
                dest = item.get("dest")
                volume_id = item.get("matched_kapowarr_id")
                result = {
                    "series": item.get("matched_series"),
                    "dest": dest,
                    "volume_id": volume_id,
                    "native_series_id": completion_native_series_id(item),
                    "metadata_provider": item.get("metadata_provider"),
                    "metadata_id": item.get("metadata_id"),
                    "comicvine_id": item.get("comicvine_id"),
                    "truth_model": infer_import_truth_model(item),
                    "manga_unit_model": item.get("manga_unit_model"),
                    "host_exists": False,
                    "kapowarr_linked": False,
                    "kapowarr_issue_links": 0,
                    "kapowarr_status": "not_checked",
                    "kavita_visible": False,
                    "komga_visible": False,
                    "komga_visibility": {},
                    "library_visible": False,
                    "library_visibility_provider": "",
                    "library_visibility_required": library_visibility_required,
                    "library_visibility_checks_enabled": library_visibility_checks_enabled,
                    "library_visibility_status": "unknown",
                    "comicinfo_status": "not_checked",
                    "verification_status": "verification_failed",
                }
                if not dest:
                    checked.append(result)
                    continue
                dest_path = Path(dest)
                if (
                    kapowarr_adapter_enabled
                    and
                    is_kapowarr_truth_model(result["truth_model"])
                    and completion_has_kapowarr_truth_anchor(item)
                    and not dest_path.exists()
                ):
                    recovered_path = kapowarr_linked_host_path_for_item(kapowarr_conn, item)
                    if recovered_path and recovered_path.exists():
                        result["original_dest"] = dest
                        result["dest"] = str(recovered_path)
                        result["recovered_dest"] = True
                        dest_path = recovered_path
                result["host_exists"] = dest_path.exists()
                if result["host_exists"] and kind_from_path(dest_path) == "comics":
                    result["comicinfo_status"] = comicinfo_status(dest_path)
                    kapowarr_path = None
                    if (
                        kapowarr_adapter_enabled
                        and is_kapowarr_truth_model(result["truth_model"])
                        and completion_has_kapowarr_truth_anchor(item)
                    ):
                        try:
                            kapowarr_path = host_path_to_kapowarr(dest_path)
                        except ValueError:
                            kapowarr_path = None
                    if kapowarr_path:
                        kapowarr_row = kapowarr_conn.execute(
                            """
                            select f.id, count(issue_link.issue_id)
                            from files f
                            left join issues_files issue_link on issue_link.file_id = f.id
                            where f.filepath = ?
                            group by f.id
                            """,
                            (kapowarr_path,),
                        ).fetchone()
                        if kapowarr_row:
                            result["kapowarr_linked"] = int(kapowarr_row[1] or 0) > 0
                            result["kapowarr_issue_links"] = int(kapowarr_row[1] or 0)
                        if (
                            not result["kapowarr_linked"]
                            and is_kapowarr_truth_model(result["truth_model"]) and completion_has_kapowarr_truth_anchor(item)
                            and volume_id
                            and result["comicinfo_status"] == "present"
                        ):
                            issue_id = kapowarr_issue_id_for_item(kapowarr_conn, item)
                            manual_match = {
                                "attempted": bool(issue_id),
                                "issue_id": issue_id,
                                "filepath": kapowarr_path,
                            }
                            if issue_id:
                                try:
                                    kapowarr_manual_match_file(volume_id, kapowarr_path, issue_id)
                                    manual_match["ok"] = True
                                except Exception as exc:
                                    manual_match["ok"] = False
                                    manual_match["error"] = str(exc)
                                kapowarr_row = kapowarr_conn.execute(
                                    """
                                    select f.id, count(issue_link.issue_id)
                                    from files f
                                    left join issues_files issue_link on issue_link.file_id = f.id
                                    where f.filepath = ?
                                    group by f.id
                                    """,
                                    (kapowarr_path,),
                                ).fetchone()
                                if kapowarr_row:
                                    result["kapowarr_linked"] = int(kapowarr_row[1] or 0) > 0
                                    result["kapowarr_issue_links"] = int(kapowarr_row[1] or 0)
                            else:
                                manual_match["ok"] = False
                                manual_match["error"] = "no_exact_kapowarr_issue_id"
                            result["kapowarr_manual_match"] = manual_match
                    result["kapowarr_status"] = (
                        "adapter_disabled"
                        if not kapowarr_adapter_enabled and completion_has_kapowarr_truth_anchor(item)
                        else
                        "linked"
                        if result["kapowarr_linked"]
                        else "kapowarr_linked_optional"
                        if result["truth_model"] == "kavita_manga"
                        else "not_linked"
                    )
                    reader_expectation = reader_expectation_for_import(item, dest_path, kavita_conn)
                    result["reader_expectation"] = reader_expectation or {}
                    visibility = inkdrop_library_frontends.check_library_visibility(
                        dest_path,
                        kavita_enabled=kavita_conn is not None,
                        check_kavita_visibility=lambda path: kavita_file_visible_for_host_path(
                            path, kavita_conn, expectation=reader_expectation or {"required": True}
                        ),
                        komga_enabled=komga_visibility_enabled,
                        check_komga_visibility=lambda path: komga_file_visible_for_host_path(path, komga_settings),
                    )
                    result.update(visibility)
                result["verification_status"] = verification_status_for(result)
                if result["library_visible"]:
                    result["library_visibility_status"] = "library_visible"
                elif result["host_exists"]:
                    result["library_visibility_status"] = "pending" if result["library_visibility_required"] else "optional"
                result["reader_configured"] = bool(kavita_visibility_enabled or komga_visibility_enabled)
                result.update(reader_completion_truth_for(result))
                checked.append(result)
            return checked
        finally:
            if kapowarr_conn is not None:
                kapowarr_conn.close()
            if kavita_conn is not None:
                kavita_conn.close()

    checked = check_once()
    poll_attempts = 0
    waited_seconds = 0
    pending_scan_statuses = {"waiting_for_library_scan", "waiting_for_kavita_scan", "library_scan_timeout", "kavita_scan_timeout"}
    completed_statuses = {"folder_verified", "library_visible", "kavita_verified"}
    if poll_library_visibility and any(item.get("verification_status") in {"waiting_for_library_scan", "waiting_for_kavita_scan"} for item in checked):
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            poll_attempts += 1
            waited_seconds += poll_interval
            checked = check_once()
            if not any(item.get("verification_status") in {"waiting_for_library_scan", "waiting_for_kavita_scan"} for item in checked):
                break
    for result in checked:
        if poll_library_visibility and result.get("verification_status") in {"waiting_for_library_scan", "waiting_for_kavita_scan"}:
            result["verification_status"] = "library_scan_timeout"
            result["library_visibility_status"] = "scan_timeout"
            result.update(reader_completion_truth_for(result))
    failures = []
    manga_completion_rows = 0
    manga_unit_completion_rows = 0
    manga_coverage_rows = 0
    collection_completion_rows = 0
    for source_item, result in zip(imported, checked):
        if result.get("truth_model") == "kavita_manga" and result.get("verification_status") in completed_statuses:
            if record_manga_unit_completion(source_item, result):
                manga_unit_completion_rows += 1
            if record_manga_coverage(source_item, result):
                manga_coverage_rows += 1
            source_unit = (source_item.get("source_unit") or result.get("source_unit") or source_item.get("manga_unit_model") or result.get("manga_unit_model") or "volume")
            if source_unit != "chapter" and record_manga_completion(source_item, result):
                manga_completion_rows += 1
        if result.get("truth_model") == COLLECTION_TRUTH_MODEL and result.get("verification_status") in completed_statuses:
            collection_completion_rows += record_collection_completion(source_item, result)
        if result.get("verification_status") not in {*completed_statuses, *pending_scan_statuses}:
            failures.append(result)
    library_provider_counts = {}
    for item in checked:
        provider = str(item.get("library_visibility_provider") or "").strip()
        if provider:
            library_provider_counts[provider] = library_provider_counts.get(provider, 0) + 1
    return {
        "checked": checked,
        "checked_count": len(checked),
        "kapowarr_linked_count": sum(1 for item in checked if item.get("kapowarr_linked")),
        "kapowarr_optional_count": sum(1 for item in checked if item.get("kapowarr_status") == "kapowarr_linked_optional"),
        "kavita_visible_count": sum(1 for item in checked if item.get("kavita_visible")),
        "komga_visible_count": sum(1 for item in checked if item.get("komga_visible")),
        "library_visible_count": sum(1 for item in checked if library_visible_for_result(item)),
        "library_visibility_provider_counts": library_provider_counts,
        "library_visibility_checks_enabled": library_visibility_checks_enabled,
        "folder_verified_count": sum(1 for item in checked if item.get("verification_status") == "folder_verified"),
        "library_verified_count": sum(1 for item in checked if item.get("verification_status") in {"library_visible", "kavita_verified"}),
        "manga_verified_count": sum(1 for item in checked if item.get("truth_model") == "kavita_manga" and item.get("verification_status") in completed_statuses),
        "manga_completion_rows": manga_completion_rows,
        "manga_unit_completion_rows": manga_unit_completion_rows,
        "manga_coverage_rows": manga_coverage_rows,
        "collection_completion_rows": collection_completion_rows,
        "collection_verified_count": sum(1 for item in checked if item.get("truth_model") == COLLECTION_TRUTH_MODEL and item.get("verification_status") in completed_statuses),
        "waiting_for_library_scan_count": sum(1 for item in checked if item.get("verification_status") == "waiting_for_library_scan"),
        "waiting_for_kavita_scan_count": sum(1 for item in checked if item.get("verification_status") == "waiting_for_kavita_scan"),
        "library_scan_timeout_count": sum(1 for item in checked if item.get("verification_status") == "library_scan_timeout"),
        "kavita_scan_timeout_count": sum(1 for item in checked if item.get("verification_status") == "kavita_scan_timeout"),
        "pending_scan_count": sum(1 for item in checked if item.get("verification_status") in pending_scan_statuses),
        "failure_count": len(failures),
        "failures": failures[:20],
        "missing_counts": missing_counts,
        "poll_attempts": poll_attempts,
        "poll_seconds": waited_seconds,
        "verified_at": time.time(),
    }


def kind_from_path(path):
    name = Path(path).name.lower()
    if name.endswith(".cbz.zip"):
        return "comics"
    return EXT_TO_KIND.get(Path(path).suffix.lower())


def connect():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite_connect(DB_PATH)
    conn.execute(
        "create table if not exists imported_files (sha256 text primary key, source text, dest text, size integer, imported_at real)"
    )
    ensure_artifact_bad_content_memory_schema(conn)
    ensure_manga_completion_schema(conn)
    ensure_collection_completion_schema(conn)
    ensure_manga_unit_schema(conn)
    return conn


def ensure_artifact_bad_content_memory_schema(conn):
    conn.execute(
        """
        create table if not exists artifact_bad_content_memory (
          identity text primary key,
          file_sha256 text,
          source_path text,
          target_type text,
          artifact_type text,
          decision text not null,
          reason_codes text,
          content_manifest_hash text,
          archive_member_manifest_hash text,
          first_seen_at real not null,
          last_seen_at real not null,
          seen_count integer not null default 1,
          raw_json text
        )
        """
    )
    conn.execute(
        "create index if not exists idx_artifact_bad_content_memory_file_sha on artifact_bad_content_memory(file_sha256)"
    )
    conn.execute(
        "create index if not exists idx_artifact_bad_content_memory_last_seen on artifact_bad_content_memory(last_seen_at)"
    )


COMPLETION_IDENTITY_TABLES = (
    "manga_completion",
    "manga_unit_completion",
    "manga_coverage",
    "collection_completion",
)


def kapowarr_completion_identity_maps():
    if not completed_import_kapowarr_adapter_enabled() or not KAPOWARR_DB.exists():
        return {}, {}
    conn = sqlite_connect(KAPOWARR_DB)
    conn.row_factory = sqlite3.Row
    try:
        volume_map = {
            int(row["id"]): str(row["comicvine_id"])
            for row in conn.execute("select id, comicvine_id from volumes where comicvine_id is not null")
            if row["id"] is not None and row["comicvine_id"] not in (None, "")
        }
        issue_map = {
            int(row["id"]): str(row["comicvine_id"])
            for row in conn.execute("select id, comicvine_id from issues where comicvine_id is not null")
            if row["id"] is not None and row["comicvine_id"] not in (None, "")
        }
        return volume_map, issue_map
    finally:
        conn.close()


def canonical_completion_identity_for_row(row, volume_map, issue_map):
    row = dict(row or {})
    provider = str(row.get("metadata_provider") or "").strip().lower()
    metadata_id = str(row.get("metadata_id") or "").strip()
    native_series_id = str(row.get("native_series_id") or "").strip()
    native_issue_id = str(row.get("native_issue_id") or "").strip()
    volume_id = row.get("kapowarr_volume_id")
    issue_id = row.get("kapowarr_issue_id")
    volume_cv = None
    issue_cv = None
    try:
        volume_cv = volume_map.get(int(volume_id)) if volume_id not in (None, "") else None
    except (TypeError, ValueError):
        volume_cv = None
    try:
        issue_cv = issue_map.get(int(issue_id)) if issue_id not in (None, "") else None
    except (TypeError, ValueError):
        issue_cv = None
    if volume_cv:
        provider = "comicvine"
        metadata_id = str(volume_cv)
        native_series_id = prefixed_identity("comicvine", volume_cv)
    else:
        existing_provider, existing_id = split_prefixed_identity(native_series_id)
        metadata_provider, metadata_value = split_prefixed_identity(metadata_id)
        if existing_provider in {"comicvine", "mangadex"} and existing_id:
            provider = provider or existing_provider
            metadata_id = metadata_id or existing_id
            native_series_id = prefixed_identity(existing_provider, existing_id)
        elif metadata_provider in {"comicvine", "mangadex"} and metadata_value:
            provider = provider or metadata_provider
            metadata_id = metadata_value
            native_series_id = prefixed_identity(metadata_provider, metadata_value)
        elif provider in {"comicvine", "mangadex"} and metadata_id:
            native_series_id = prefixed_identity(provider, metadata_id)
    if issue_cv:
        native_issue_id = prefixed_identity("comicvine", issue_cv)
    return {
        "metadata_provider": provider or None,
        "metadata_id": metadata_id or None,
        "native_series_id": native_series_id or None,
        "native_issue_id": native_issue_id or None,
    }


def backfill_completion_identity_fields(dry_run=False):
    volume_map, issue_map = kapowarr_completion_identity_maps()
    conn = connect()
    conn.row_factory = sqlite3.Row
    now = time.time()
    summary = {
        "ok": True,
        "dry_run": bool(dry_run),
        "volume_identity_rows": len(volume_map),
        "issue_identity_rows": len(issue_map),
        "tables": {},
        "updated_total": 0,
    }
    try:
        for table in COMPLETION_IDENTITY_TABLES:
            rows = conn.execute(
                f"""
                select rowid, native_series_id, native_issue_id, metadata_provider, metadata_id,
                       kapowarr_volume_id, kapowarr_issue_id
                from {table}
                """
            ).fetchall()
            table_summary = {
                "total": len(rows),
                "would_update": 0,
                "updated": 0,
                "missing_after": 0,
                "samples": [],
            }
            for row in rows:
                canonical = canonical_completion_identity_for_row(row, volume_map, issue_map)
                updates = {}
                for key in ("native_series_id", "native_issue_id", "metadata_provider", "metadata_id"):
                    current = str(row[key] or "").strip()
                    desired = str(canonical.get(key) or "").strip()
                    if desired and desired != current:
                        updates[key] = desired
                if not canonical.get("native_series_id"):
                    table_summary["missing_after"] += 1
                if not updates:
                    continue
                table_summary["would_update"] += 1
                if len(table_summary["samples"]) < 5:
                    table_summary["samples"].append(
                        {
                            "rowid": row["rowid"],
                            "kapowarr_volume_id": row["kapowarr_volume_id"],
                            "kapowarr_issue_id": row["kapowarr_issue_id"],
                            "updates": updates,
                        }
                    )
                if not dry_run:
                    conn.execute(
                        f"""
                        update {table}
                        set native_series_id=?,
                            native_issue_id=coalesce(?, native_issue_id),
                            metadata_provider=?,
                            metadata_id=?,
                            updated_at=?
                        where rowid=?
                        """,
                        (
                            canonical.get("native_series_id") or row["native_series_id"],
                            canonical.get("native_issue_id"),
                            canonical.get("metadata_provider") or row["metadata_provider"],
                            canonical.get("metadata_id") or row["metadata_id"],
                            now,
                            row["rowid"],
                        ),
                    )
                    table_summary["updated"] += 1
            summary["tables"][table] = table_summary
            summary["updated_total"] += table_summary["updated"] if not dry_run else table_summary["would_update"]
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return summary
    finally:
        conn.close()


MANGA_COMPLETION_AUDIT_TABLES = {
    "manga_completion": None,
    "manga_unit_completion": "manga_unit_model",
    "manga_coverage": "unit_type",
}


def completion_audit_path_state(path_value):
    raw_path = str(path_value or "").strip()
    host_path = host_path_from_kavita_path(raw_path) or raw_path
    return {
        "path": raw_path,
        "host_path": host_path,
        "exists": completion_target_exists(host_path),
    }


def completion_number_keys(value):
    text = str(value or "").strip()
    keys = set()
    if not text:
        return keys
    keys.add(text)
    normalized_manga = normalize_manga_number(text)
    if normalized_manga:
        keys.add(normalized_manga)
    if inkdrop_state is not None:
        try:
            keys.update(inkdrop_state.issue_number_keys(text))
        except Exception:
            pass
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        raw = match.group(0)
        if "." in raw:
            trimmed = raw.rstrip("0").rstrip(".")
            if trimmed:
                keys.add(trimmed)
        else:
            try:
                number = int(raw)
                keys.add(str(number))
                keys.add(f"{number:03d}")
                keys.add(f"{number:04d}")
            except ValueError:
                pass
    return {key for key in keys if key}


def stale_completion_queue_links(row, limit=8):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return []
    row = dict(row or {})
    number_keys = sorted(completion_number_keys(row.get("normalized_number") or row.get("issue_number")))
    if not number_keys:
        return []
    series_terms = []
    series_params = []
    native_series_id = str(row.get("native_series_id") or "").strip()
    if native_series_id:
        series_terms.append("s.id = ?")
        series_params.append(native_series_id)
    provider = str(row.get("metadata_provider") or "").strip().lower()
    metadata_id = str(row.get("metadata_id") or "").strip()
    if provider and metadata_id:
        series_terms.append("(s.metadata_provider = ? and s.metadata_id = ?)")
        series_params.extend([provider, metadata_id])
    volume_id = row.get("kapowarr_volume_id")
    try:
        if volume_id not in (None, ""):
            series_terms.append("s.kapowarr_id = ?")
            series_params.append(int(volume_id))
    except (TypeError, ValueError):
        pass
    series_title = str(row.get("series_title") or "").strip()
    if series_title:
        series_terms.append("s.sort_title = ?")
        series_params.append(normalize(series_title))
    if not series_terms:
        return []

    issue_terms = []
    issue_params = []
    for key in number_keys:
        normalized = None
        if inkdrop_state is not None:
            try:
                normalized = inkdrop_state.normalize_issue_number(key)
            except Exception:
                normalized = None
        issue_terms.append("(i.issue_number = ? or i.normalized_number = ? or i.normalized_number = ?)")
        issue_params.extend([key, key, normalized or key])

    conn = sqlite_connect(INKDROP_STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            select q.id as queue_id, q.state as queue_state, q.active as queue_active,
                   q.last_event, w.id as wanted_id, w.status as wanted_status,
                   s.id as series_id, s.title as series_title,
                   i.id as issue_id, i.issue_number, i.normalized_number
            from queue_items q
            join series s on s.id = q.series_id
            left join wanted_items w on w.id = q.wanted_id
            left join issues i on i.id = q.issue_id
            where ({' or '.join(series_terms)})
              and ({' or '.join(issue_terms)})
            order by
              case when q.state = 'verified' then 0
                   when coalesce(w.status, '') = 'wanted' then 1
                   when coalesce(q.active, 0) = 1 then 2
                   else 3 end,
              coalesce(q.updated_at, q.created_at, 0) desc
            limit ?
            """,
            (*series_params, *issue_params, int(limit)),
        ).fetchall()
        return [dict(item) for item in rows]
    finally:
        conn.close()


def stale_completion_link_needs_attention(link):
    if not isinstance(link, dict):
        return False
    queue_state = str(link.get("queue_state") or "").strip().lower()
    wanted_status = str(link.get("wanted_status") or "").strip().lower()
    if queue_state in {"verified", "satisfied", "superseded_duplicate"}:
        return False
    if wanted_status in {"wanted", "in_progress", "blocked", "failed"}:
        return True
    return bool(link.get("queue_active"))


def audit_stale_manga_completion(limit=50, include_queue=True):
    limit = max(0, int(limit or 0))
    if not DB_PATH.exists():
        return {"ok": False, "reason": "completion_db_missing", "db_path": str(DB_PATH)}
    conn = sqlite_connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    summary = {
        "ok": True,
        "db_path": str(DB_PATH),
        "state_db_path": str(INKDROP_STATE_DB),
        "tables": {},
        "stale_total": 0,
        "verified_total": 0,
        "stale_with_queue_links": 0,
        "stale_needing_attention": 0,
        "samples": [],
        "top_series": {},
    }
    try:
        existing_tables = {
            str(row["name"])
            for row in conn.execute("select name from sqlite_master where type='table'")
        }
        for table, unit_column in MANGA_COMPLETION_AUDIT_TABLES.items():
            if table not in existing_tables:
                summary["tables"][table] = {"exists": False, "verified": 0, "stale": 0}
                continue
            rows = conn.execute(
                f"""
                select rowid, *
                from {table}
                where verification_status = 'kavita_verified'
                order by updated_at desc, rowid desc
                """
            ).fetchall()
            table_summary = {
                "exists": True,
                "verified": len(rows),
                "stale": 0,
                "stale_with_queue_links": 0,
                "stale_needing_attention": 0,
                "top_series": {},
                "samples": [],
            }
            summary["verified_total"] += len(rows)
            for record in rows:
                row = dict(record)
                target = completion_audit_path_state(row.get("target_file_path"))
                if target.get("exists"):
                    continue
                source = completion_audit_path_state(row.get("source_path"))
                queue_links = stale_completion_queue_links(row) if include_queue else []
                needs_attention = any(stale_completion_link_needs_attention(link) for link in queue_links)
                table_summary["stale"] += 1
                summary["stale_total"] += 1
                series_key = row.get("series_title") or row.get("normalized_series") or "unknown"
                table_summary["top_series"][series_key] = int(table_summary["top_series"].get(series_key) or 0) + 1
                summary["top_series"][series_key] = int(summary["top_series"].get(series_key) or 0) + 1
                if queue_links:
                    table_summary["stale_with_queue_links"] += 1
                    summary["stale_with_queue_links"] += 1
                if needs_attention:
                    table_summary["stale_needing_attention"] += 1
                    summary["stale_needing_attention"] += 1
                sample = {
                    "table": table,
                    "rowid": row.get("rowid"),
                    "series_title": row.get("series_title"),
                    "normalized_series": row.get("normalized_series"),
                    "issue_number": row.get("issue_number"),
                    "normalized_number": row.get("normalized_number"),
                    "unit": row.get(unit_column) if unit_column else None,
                    "target": target,
                    "source": source,
                    "metadata_provider": row.get("metadata_provider"),
                    "metadata_id": row.get("metadata_id"),
                    "native_series_id": row.get("native_series_id"),
                    "native_issue_id": row.get("native_issue_id"),
                    "kapowarr_volume_id": row.get("kapowarr_volume_id"),
                    "kapowarr_issue_id": row.get("kapowarr_issue_id"),
                    "updated_at": row.get("updated_at"),
                    "queue_links": queue_links,
                    "needs_attention": needs_attention,
                }
                if len(table_summary["samples"]) < limit:
                    table_summary["samples"].append(sample)
                if len(summary["samples"]) < limit:
                    summary["samples"].append(sample)
            summary["tables"][table] = table_summary
    finally:
        conn.close()
    summary["top_series"] = dict(sorted(summary["top_series"].items(), key=lambda item: (-item[1], item[0]))[:20])
    for table_summary in summary["tables"].values():
        if isinstance(table_summary, dict) and isinstance(table_summary.get("top_series"), dict):
            table_summary["top_series"] = dict(
                sorted(table_summary["top_series"].items(), key=lambda item: (-item[1], item[0]))[:20]
            )
    return summary


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_stable(path, min_age_seconds):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    return time.time() - stat.st_mtime >= min_age_seconds and stat.st_size > 0


def unique_dest(dest_dir, source):
    dest_dir.mkdir(parents=True, exist_ok=True)
    candidate = dest_dir / source.name
    if not candidate.exists():
        return candidate
    stem = source.stem
    suffix = source.suffix
    counter = 2
    while True:
        candidate = dest_dir / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def unique_dest_name(dest_dir, filename):
    dest_dir.mkdir(parents=True, exist_ok=True)
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        candidate = dest_dir / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def existing_canonical_dest(target_dir, canonical, source):
    if not canonical:
        return None
    filename = canonical.get("canonical_filename")
    if not filename:
        return None
    source = Path(source)
    names = []
    base = Path(filename)
    if source.suffix.lower() == ".pdf":
        names.append(base.with_suffix(".cbz").name)
    else:
        names.append(base.name)
    if source.suffix.lower() in {".cbr", ".zip"} or source.name.lower().endswith(".cbz.zip"):
        names.append(base.with_suffix(".cbz").name)
    for name in dict.fromkeys(names):
        candidate = Path(target_dir) / name
        if candidate.exists():
            return candidate
        try:
            for nested in Path(target_dir).rglob(name):
                if nested.is_file():
                    return nested
        except OSError:
            pass
    return None


def find_same_file(dest_dir, source, digest):
    if not dest_dir.exists():
        return None
    try:
        source_size = source.stat().st_size
    except FileNotFoundError:
        return None
    for candidate in dest_dir.iterdir():
        if not candidate.is_file():
            continue
        try:
            if candidate.stat().st_size != source_size:
                continue
        except FileNotFoundError:
            continue
        if sha256(candidate) == digest:
            return candidate
    return None


COPY_SUFFIX_RE = re.compile(r"^(?P<stem>.+?) \((?P<counter>[2-9][0-9]*)\)(?P<suffix>\.[^.]+)$")


def suffixless_existing_dest(dest):
    """Return the base file when a generated '(2)' style destination already exists."""
    dest = Path(dest)
    match = COPY_SUFFIX_RE.match(dest.name)
    if not match:
        return None
    base = dest.with_name(f"{match.group('stem')}{match.group('suffix')}")
    return base if base.exists() else None


def clean_words(value):
    return re.findall(r"[a-z0-9]+", str(value or "").lower())


def normalize(value):
    return " ".join(clean_words(value))


LEADING_TITLE_ARTICLES = {"a", "an", "the"}


def leading_article_aliases(title):
    words = clean_words(title)
    if len(words) <= 1 or words[0] not in LEADING_TITLE_ARTICLES:
        return []
    return [" ".join(words[1:])]


def avatar_aliases(title):
    text = str(title or "")
    if not re.search(r"\b(avatar|airbender|korra)\b", text, re.I):
        return []
    aliases = [text]
    plain = re.sub(r"^Nickelodeon\s+", "", text, flags=re.I)
    aliases.append(plain)
    aliases.append(re.sub(r"\b(?:Library Edition|Omnibus)\b", " ", plain, flags=re.I))
    aliases.append(plain.replace(":", " ").replace("--", " ").replace("-", " ").replace("—", " "))
    out = []
    seen = set()
    for alias in aliases:
        cleaned = normalize(alias)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def append_comic_target_aliases(aliases, value):
    if not value:
        return
    aliases.append(value)
    aliases.extend(leading_article_aliases(value))
    aliases.extend(avatar_aliases(value))


def contains_sequence(words, sequence):
    if not sequence or len(words) < len(sequence):
        return False
    width = len(sequence)
    return any(words[idx : idx + width] == sequence for idx in range(len(words) - width + 1))


def issue_marker_word(word):
    return bool(re.fullmatch(r"(?:v|vol|volume|issue|ch|chapter)?0*\d+", word or "")) or word in {
        "v", "vol", "volume", "issue", "ch", "chapter",
    }


def safe_filename_part(value):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Unknown"


def extract_issue_number(path):
    text = " ".join([Path(path).stem, Path(path).parent.name])
    patterns = [
        r"(?:^|[\s._\-\(\[])#\s*(\d{1,5}(?:\.\d+)?)\b",
        r"\bissue[\s._-]*(\d{1,5}(?:\.\d+)?)\b",
        r"\b(?:volume|vol|v)[\s._-]*0*1[\s._-]*issue[\s._-]*(\d{1,5}(?:\.\d+)?)\b",
        r"\b(?:volume|vol|v)[\s._-]*(?:19|20)\d{2}[\s._-]+0*(\d{1,3}(?:\.\d+)?)\b",
        r"\b(?:volume|vol|v)[\s._-]*(?!(?:19|20)\d{2}\b)(\d{1,5}(?:\.\d+)?)\b",
        r"(?:^|[\s._-])0*(\d{1,5}(?:\.\d+)?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        try:
            number = float(match.group(1))
        except ValueError:
            continue
        if number >= 0:
            return number
    return None


def format_issue_number(number):
    try:
        value = float(number)
    except (TypeError, ValueError):
        return None
    if value.is_integer():
        integer = int(value)
        return f"{integer:03d}" if integer < 1000 else str(integer)
    return str(value).rstrip("0").rstrip(".")


def source_contains_trusted_issue_number(path, expected):
    if not expected:
        return False
    text = " ".join([Path(path).stem, Path(path).parent.name])
    for match in re.finditer(r"(?<!\d)(\d{1,5}(?:\.\d+)?)(?!\d)", text):
        before = text[: match.start()]
        if re.search(r"(?:^|[\s._\-\(\[])(?:v|vol|volume)[\s._-]*$", before, re.I):
            continue
        if format_issue_number(match.group(1)) == expected:
            return True
    return False


def format_volume_number(number):
    try:
        value = float(number)
    except (TypeError, ValueError):
        return None
    if value.is_integer():
        integer = int(value)
        return f"{integer:02d}" if integer < 100 else str(integer)
    return str(value).rstrip("0").rstrip(".")


def is_manga_target(target):
    if not target:
        return False
    if str(target.get("media_type") or "").strip().lower() in {"manga", "manhwa", "manhua"}:
        return True
    title = str(target.get("title") or "").strip().lower()
    title_norm = normalize(title)
    publisher = str(target.get("publisher") or "").strip().lower()
    if title in MANGA_TITLE_HINTS or title_norm in MANGA_TITLE_HINTS:
        return True
    return any(hint in publisher for hint in MANGA_PUBLISHER_HINTS)


def is_volume_as_issue_target(target):
    return str((target or {}).get("special_version") or "").strip().lower() == "volume-as-issue"


def is_manga_unit_guard_target(target):
    return is_manga_target(target) or is_volume_as_issue_target(target)


def exact_manga_volume_import_identity(source, target):
    if inkdrop_state is None or not target or not is_manga_target(target):
        return None
    issue_title = str(target.get("issue_title") or "").strip()
    raw = {
        key: target.get(key)
        for key in ("manga_unit_policy", "manga_unit_model")
        if key in target
    }
    queue_context = {
        "series": target.get("title") or target.get("series"),
        "query": target.get("query") or " ".join(
            value
            for value in (str(target.get("title") or target.get("series") or "").strip(), issue_title)
            if value
        ),
        "issue_title": issue_title,
        "issue_number": target.get("issue_number"),
        "normalized_number": target.get("normalized_number"),
        "media_type": target.get("media_type"),
        "raw_json": json.dumps(raw),
    }
    return inkdrop_state.exact_manga_volume_target_identity(
        queue_context,
        {"matched_local_path": str(source), "matched_series": target.get("title")},
    )


def unsafe_comic_target_match_reason(source, target):
    if not target:
        return None
    source_name = Path(source).name
    if is_manga_target(target) and (
        re.search(r"(?:^|[^a-z0-9])(?:volume|vol\.?|v)[\s._-]*0*\d{1,3}(?!\d)", source_name, re.I)
        and re.search(r"(?:^|[^a-z0-9])(?:chapter|chap|ch|c|issue|part|pt)\.?[\s._-]*0*\d+", source_name, re.I)
    ):
        return "manga_source_conflicting_unit_identity"
    if inkdrop_state is not None:
        issue_title = str(target.get("issue_title") or "").strip()
        queue_context = {
            "series": target.get("title") or target.get("series"),
            "query": target.get("query") or " ".join(
                value
                for value in (str(target.get("title") or target.get("series") or "").strip(), issue_title)
                if value
            ),
            "issue_title": issue_title,
            "issue_number": target.get("issue_number"),
            "normalized_number": target.get("normalized_number"),
            "media_type": target.get("media_type"),
            "manga_unit_policy": target.get("manga_unit_policy"),
            "manga_unit_model": target.get("manga_unit_model"),
        }
        shared_reason = inkdrop_state.collection_target_single_part_block_reason(
            queue_context,
            {"matched_local_path": str(source)},
        )
        if shared_reason:
            return shared_reason
    target_title = str(target.get("title") or "")
    target_text = normalize(target_title)
    source_text = normalize(source_identity_text(source))
    target_is_collection = bool(
        re.search(r"\b(?:omnibus|library edition|complete collection|collection)\b", target_title, re.I)
    )
    source_is_collection = bool(
        re.search(r"\b(?:omnibus|library edition|complete collection|collection)\b", source_identity_text(source), re.I)
    )
    source_is_single_part = bool(re.search(r"\bpart\s*0*\d+\b", source_identity_text(source), re.I))
    if target_is_collection and source_is_single_part and not source_is_collection:
        return "single_part_file_does_not_satisfy_collection_target"
    if "omnibus" in target_text and "omnibus" not in source_text and source_is_single_part:
        return "single_part_file_does_not_satisfy_omnibus_target"
    return None


def kavita_manga_series_dir(target):
    folder = Path(target["folder"])
    if not is_manga_target(target):
        return folder
    try:
        rel = folder.relative_to(COMIC_ROOT)
        folder = MANGA_ROOT / rel
    except ValueError:
        pass
    if re.match(r"(?i)^(?:volume|vol)[\s._-]*\d+", folder.name):
        return folder.parent
    return folder


def comic_import_target_dir(target):
    if not target:
        return None
    folder = Path(target["folder"])
    adapter_folder = str(
        target.get("adapter_folder")
        or target.get("library_adapter_folder")
        or target.get("library_adapter_host_path")
        or ""
    ).strip()
    if adapter_folder and not is_manga_target(target):
        adapter_path = Path(adapter_folder)
        try:
            adapter_path.relative_to(folder)
            return adapter_path
        except ValueError:
            pass
    if target_kapowarr_volume_id(target) is not None:
        return folder
    if is_manga_target(target):
        return Path(kavita_manga_series_dir(target))
    return folder


def suwayomi_chapter_volume_mismatch(source, target):
    if not target:
        return False
    source_text = source_identity_text(source).lower()
    target_folder = Path(target.get("folder") or "").name.lower()
    looks_like_chapter = re.search(r"(?:^|[\s._-])(?:chapter|ch)[\s._-]*\d+", source_text)
    looks_like_volume_target = re.match(r"(?:volume|vol)[\s._-]*\d+", target_folder)
    return bool(looks_like_chapter and looks_like_volume_target)


def suwayomi_unit_decision(source, target):
    chapter_number = suwayomi_chapter_number(source)
    volume_number = suwayomi_volume_number(source)
    model = manga_unit_model_for_target(target)
    if chapter_number:
        target_native_series_id = completion_native_series_id(target)
        covered_by_volume = bool(
            volume_number
            and target
            and manga_coverage_has_existing_target(
                target.get("title"),
                volume_number,
                "volume",
                kapowarr_volume_id=target.get("id"),
                native_series_id=target_native_series_id,
            )
        )
        allowed = manga_policy_allows_chapter(model) and not covered_by_volume
        reason = None
        if covered_by_volume:
            reason = "chapter_covered_by_verified_volume"
        elif not manga_policy_allows_chapter(model):
            reason = "suwayomi_chapter_requires_chapter_unit_model"
        return {
            "source_unit": "chapter",
            "chapter_number": chapter_number,
            "source_volume_number": volume_number,
            "series_unit_model": model,
            **manga_unit_policy_payload(model),
            "allowed": allowed,
            "reason": reason,
        }
    if manga_policy_allows_volume(model):
        source_unit = "pack" if model == "pack" else "volume"
        return {
            "source_unit": source_unit,
            "series_unit_model": model,
            **manga_unit_policy_payload(model),
            "allowed": True,
            "reason": None,
        }
    return {
        "source_unit": "unknown/manual",
        "series_unit_model": model,
        **manga_unit_policy_payload(model),
        "allowed": False,
        "reason": "suwayomi_unit_unknown_manual_review",
    }


def comicinfo_text(info, key):
    value = (info or {}).get(key)
    return str(value or "").strip()


def source_chapter_number(path):
    info = read_comicinfo(path)
    fmt = comicinfo_text(info, "Format").lower()
    title = comicinfo_text(info, "Title").lower()
    source_text = source_identity_text(path)
    chapter = suwayomi_chapter_number(path)
    if not chapter and ("chapter" in fmt or re.search(r"(?:^|[\s._-])chapter[\s._-]*\d+", title, re.I)):
        chapter = comicinfo_text(info, "Number") or extract_issue_number(path)
    if chapter:
        return normalize_manga_number(chapter)
    if "chapter" in fmt or re.search(r"(?:^|[\s._-])chapter[\s._-]*\d+", source_text, re.I):
        number = comicinfo_text(info, "Number") or extract_issue_number(path)
        return normalize_manga_number(number)
    return None


def source_manga_number(path):
    info = read_comicinfo(path)
    return normalize_manga_number(comicinfo_text(info, "Number") or comicinfo_text(info, "Volume") or extract_issue_number(path))


def manga_source_has_explicit_volume_hint(path):
    info = read_comicinfo(path)
    if normalize_manga_number(comicinfo_text(info, "Volume")):
        return True
    source_text = source_identity_text(path)
    return bool(re.search(r"\b(?:volume|vol|v)[\s._-]*0*\d{1,5}(?:\.\d+)?\b", source_text, re.I))


def manga_file_unit_and_number(path):
    info = read_comicinfo(path)
    fmt = comicinfo_text(info, "Format").lower()
    title = comicinfo_text(info, "Title").lower()
    path_text = source_identity_text(path).lower()
    chapter_hint = (
        "chapter" in fmt
        or "chapter" in title
        or re.search(r"(?:^|[\s._-])(?:chapter|ch)[\s._-]*\d+", path_text)
    )
    if chapter_hint:
        number = source_chapter_number(path) or normalize_manga_number(
            comicinfo_text(info, "Number") or extract_issue_number(path)
        )
        return "chapter", number
    number = normalize_manga_number(
        comicinfo_text(info, "Volume")
        or comicinfo_text(info, "Number")
        or extract_issue_number(path)
    )
    return "volume", number


def exact_volume_existing_path_matches_target(path, target, exact_volume_identity=None):
    if not exact_volume_identity:
        return True
    candidate_identity = exact_manga_volume_import_identity(path, target)
    return bool(
        candidate_identity
        and candidate_identity.get("unit_type") == "volume"
        and candidate_identity.get("volume_number") == exact_volume_identity.get("volume_number")
    )


def find_existing_manga_unit_file(target, number, unit_type, exclude=None, exact_volume_identity=None):
    if not target or not number:
        return None
    normalized_number = normalize_manga_number(number)
    normalized_unit = "volume" if unit_type == "pack" else str(unit_type or "volume")
    exclude_path = Path(exclude).resolve() if exclude else None
    base_dirs = []
    seen_dirs = set()

    def add_base_dir(value):
        if not value:
            return
        path = Path(value)
        key = str(path)
        if key not in seen_dirs:
            seen_dirs.add(key)
            base_dirs.append(path)

    add_base_dir(kavita_manga_series_dir(target))
    add_base_dir(target.get("folder"))
    for base_dir in base_dirs:
        if not base_dir.exists():
            continue
        for candidate in base_dir.rglob("*"):
            if is_internal_import_path(candidate, base_dir):
                continue
            if not candidate.is_file() or candidate.suffix.lower() not in {".cbz", ".cbr", ".pdf"}:
                continue
            if durable_managed_manga_target_path(candidate, normalized_number) is None:
                continue
            try:
                if exclude_path and candidate.resolve() == exclude_path:
                    continue
            except FileNotFoundError:
                continue
            candidate_unit, candidate_number = manga_file_unit_and_number(candidate)
            if (
                candidate_unit == normalized_unit
                and candidate_number == normalized_number
                and exact_volume_existing_path_matches_target(candidate, target, exact_volume_identity)
            ):
                return candidate
    return None


def manga_import_guard(
    path,
    target,
    suwayomi_staging=False,
    auto_learn=True,
    trusted_issue=None,
    exact_volume_identity=None,
):
    if not target or not is_manga_unit_guard_target(target):
        return {"allowed": True}
    target_native_series_id = completion_native_series_id(target)
    model = manga_unit_model_for_target(target)
    if model == "unknown/manual" and is_volume_as_issue_target(target):
        model = "volume"
    auto_set_unit_model = None
    suwayomi_source = bool(suwayomi_staging) or is_suwayomi_import_source(path)
    if suwayomi_source:
        decision = suwayomi_unit_decision(path, target)
        number = decision.get("chapter_number") if decision.get("source_unit") == "chapter" else source_manga_number(path)
        source_unit = decision.get("source_unit")
        allowed = decision.get("allowed", True)
        reason = decision.get("reason")
    else:
        chapter_number = source_chapter_number(path)
        number = chapter_number or source_manga_number(path)
        source_unit = "chapter" if chapter_number else "volume" if number else "unknown/manual"
        allowed = True
        reason = None
        if source_unit == "chapter" and not manga_policy_allows_chapter(model):
            allowed = False
            reason = "manga_chapter_requires_chapter_unit_model"
        elif source_unit == "volume" and not manga_policy_allows_volume(model):
            native_bare_volume = model == "unknown/manual" and native_manga_bare_volume_import_is_safe(path, target, number)
            if model == "unknown/manual" and (manga_source_has_explicit_volume_hint(path) or native_bare_volume):
                unit_model_source = "auto_native_manga_bare_number_volume_import" if native_bare_volume else "auto_explicit_volume_import"
                auto_set_unit_model = {
                    "series": target.get("title"),
                    "normalized_series": normalize_series(target.get("title")),
                    "manga_unit_model": "volume",
                    "source": unit_model_source,
                    "kapowarr_volume_id": target_kapowarr_volume_id(target),
                    "dry_run": not auto_learn,
                }
                if auto_learn:
                    auto_set_unit_model = set_manga_unit_model(
                        target.get("title"),
                        "volume",
                        source=unit_model_source,
                        kapowarr_volume_id=target_kapowarr_volume_id(target),
                    )
                model = "volume"
            else:
                allowed = False
                reason = "manga_volume_requires_volume_or_mixed_unit_model"
    if (
        source_unit == "chapter"
        and not allowed
        and model == "unknown/manual"
        and native_manga_explicit_chapter_import_is_safe(path, target, number, trusted_issue=trusted_issue)
    ):
        unit_model_source = "auto_explicit_chapter_import"
        auto_set_unit_model = {
            "series": target.get("title"),
            "normalized_series": normalize_series(target.get("title")),
            "manga_unit_model": "chapter",
            "source": unit_model_source,
            "kapowarr_volume_id": target_kapowarr_volume_id(target),
            "dry_run": not auto_learn,
        }
        if auto_learn:
            auto_set_unit_model = set_manga_unit_model(
                target.get("title"),
                "chapter",
                source=unit_model_source,
                kapowarr_volume_id=target_kapowarr_volume_id(target),
            )
        model = "chapter"
        allowed = True
        reason = None
    if exact_volume_identity:
        source_unit = "volume"
        number = exact_volume_identity["volume_number"]
        if manga_policy_allows_volume(model):
            allowed = True
            reason = None
        else:
            allowed = False
            reason = "manga_volume_requires_volume_or_mixed_unit_model"
    effective_unit = source_unit if source_unit in {"chapter", "volume", "pack"} else model
    existing_file = (
        find_existing_manga_unit_file(
            target,
            number,
            effective_unit,
            exclude=path,
            exact_volume_identity=exact_volume_identity,
        )
        if number
        else None
    )
    if existing_file:
        return {
            "allowed": False,
            "completed": True,
            "reason": "already_verified_duplicate",
            "source_unit": source_unit,
            "series_unit_model": model,
            **manga_unit_policy_payload(model),
            "auto_set_unit_model": auto_set_unit_model,
            "normalized_number": number,
            "existing_path": str(existing_file),
        }
    coverage_existing = None
    if effective_unit in {"chapter", "volume", "pack"} and number:
        coverage_existing = manga_coverage_existing_target_path(
            target["title"],
            number,
            effective_unit,
            kapowarr_volume_id=target.get("id"),
            native_series_id=target_native_series_id,
        )
    if coverage_existing is None and effective_unit == "chapter" and number:
        coverage_existing = manga_unit_completion_existing_target_path(
            target["title"],
            number,
            "chapter",
            kapowarr_volume_id=target.get("id"),
            native_series_id=target_native_series_id,
        )
    if coverage_existing is not None and not exact_volume_existing_path_matches_target(
        coverage_existing,
        target,
        exact_volume_identity,
    ):
        coverage_existing = None
    if coverage_existing is not None:
        return {
            "allowed": False,
            "completed": True,
            "reason": "already_verified_duplicate",
            "source_unit": source_unit,
            "series_unit_model": model,
            **manga_unit_policy_payload(model),
            "auto_set_unit_model": auto_set_unit_model,
            "normalized_number": number,
            "existing_path": str(coverage_existing),
        }
    return {
        "allowed": allowed,
        "completed": False,
        "reason": reason,
        "source_unit": source_unit,
        "series_unit_model": model,
        **manga_unit_policy_payload(model),
        "auto_set_unit_model": auto_set_unit_model,
        "normalized_number": number,
    }


def suwayomi_chapter_dest(target_dir, target, source, chapter_number):
    series = safe_filename_part((target or {}).get("title") or Path(source).parent.name)
    number = normalize_manga_number(chapter_number)
    filename = f"{series} - Chapter {int(number):03d}.cbz"
    if target:
        base_dir = kavita_manga_series_dir(target)
    else:
        base_dir = target_dir
    return unique_dest_name(base_dir, filename)


def issue_year(date_value, fallback_year):
    text = str(date_value or "").strip()
    if re.match(r"^\d{4}", text):
        return text[:4]
    return str(fallback_year or "").strip()


def canonical_comic_dest(target_dir, source, target, source_unit=None):
    if not target:
        return unique_dest(target_dir, source), None
    target_issue_number = target.get("issue_number") or target.get("normalized_number") or target.get("trusted_issue")
    issue_number = target_issue_number or extract_issue_number(source)
    if issue_number is None:
        return unique_dest(target_dir, source), None
    volume_id = target_kapowarr_volume_id(target)
    if volume_id is None or not completed_import_kapowarr_adapter_enabled() or not KAPOWARR_DB.exists():
        pretty_issue = format_issue_number(issue_number)
        if not pretty_issue:
            return unique_dest(target_dir, source), None
        source_text = " ".join([Path(source).stem, Path(source).parent.name])
        volume_style = (
            is_manga_target(target)
            and str(source_unit or "").strip().lower() in {"volume", "pack"}
        ) or (
            bool(re.search(r"\b(?:v|vol|volume)[\s._-]*0*\d{1,5}(?:\.\d+)?\b", source_text, re.I))
            and not re.search(r"(?:^|[\s._\-\(\[])#\s*\d|\bissue[\s._-]*\d", source_text, re.I)
        )
        series = safe_filename_part(target.get("title") or Path(source).parent.name)
        if volume_style and is_manga_target(target):
            filename = f"{series} v{format_volume_number(issue_number) or pretty_issue}"
        else:
            filename = f"{series} #{pretty_issue}"
        year = target.get("year")
        if year:
            filename += f" ({year})"
        filename += source.suffix.lower()
        return unique_dest_name(target_dir, filename), {
            "canonical_filename": filename,
            "canonical_issue_number": pretty_issue,
            "canonical_issue_title": None,
            "canonical_year": year,
            "canonical_source": "inkdrop_series_target",
        }
    conn = sqlite_connect(KAPOWARR_DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            select i.issue_number, i.calculated_issue_number, i.date, i.title as issue_title, v.title, v.year
            from issues i
            join volumes v on v.id = i.volume_id
            where i.volume_id = ?
              and abs(i.calculated_issue_number - ?) < 0.001
            order by i.id
            limit 1
            """,
            (volume_id, issue_number),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return unique_dest(target_dir, source), None
    pretty_issue = format_issue_number(row["issue_number"] or row["calculated_issue_number"])
    if not pretty_issue:
        return unique_dest(target_dir, source), None
    year = issue_year(row["date"], row["year"])
    series = safe_filename_part(row["title"] or target.get("title"))
    source_text = " ".join([Path(source).stem, Path(source).parent.name])
    volume_style = (
        is_manga_target(target)
        and str(source_unit or "").strip().lower() in {"volume", "pack"}
    ) or (
        bool(re.search(r"\b(?:v|vol|volume)[\s._-]*0*\d{1,5}(?:\.\d+)?\b", source_text, re.I))
        and not re.search(r"(?:^|[\s._\-\(\[])#\s*\d|\bissue[\s._-]*\d", source_text, re.I)
    )
    if volume_style and is_manga_target(target):
        pretty_volume = format_volume_number(row["issue_number"] or row["calculated_issue_number"])
        if not pretty_volume:
            return unique_dest(target_dir, source), None
        filename = f"{series} v{pretty_volume}"
    else:
        filename = f"{series} #{pretty_issue}"
    if year:
        filename += f" ({year})"
    filename += source.suffix.lower()
    return unique_dest_name(target_dir, filename), {
        "canonical_filename": filename,
        "canonical_issue_number": pretty_issue,
        "canonical_issue_title": row["issue_title"],
        "canonical_year": year,
    }


def kapowarr_folder_to_host(folder):
    folder = str(folder or "").strip()
    if not folder.startswith(KAPOWARR_COMIC_ROOT):
        return None
    rel = folder[len(KAPOWARR_COMIC_ROOT):].lstrip("/")
    return COMIC_ROOT / rel


def kapowarr_folder_to_manga_host(folder):
    folder = str(folder or "").strip()
    if not folder.startswith(KAPOWARR_COMIC_ROOT):
        return None
    rel = folder[len(KAPOWARR_COMIC_ROOT):].lstrip("/")
    return MANGA_ROOT / rel


def target_kapowarr_volume_id(target):
    if not isinstance(target, dict):
        return None
    value = target.get("id") or target.get("kapowarr_id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def import_event_scan_folder(event, fallback_dir=None):
    if isinstance(event, dict):
        dest = str(event.get("dest") or "").strip()
        if dest:
            try:
                path = Path(dest)
                roots = (COMIC_ROOT, MANGA_ROOT)
                if any(media_management_path_under_root(path, [root]) for root in roots):
                    return str(path.parent)
            except (OSError, RuntimeError):
                pass
    return str(fallback_dir) if fallback_dir else None


def add_target_scan_requests(target, target_dir, kapowarr_scan_volume_ids, kavita_scan_folders, event=None):
    volume_id = target_kapowarr_volume_id(target)
    kapowarr_adapter_enabled = completed_import_kapowarr_adapter_enabled()
    if volume_id is not None and kapowarr_adapter_enabled:
        kapowarr_scan_volume_ids.add(volume_id)
    scan_folder = import_event_scan_folder(event, target_dir)
    if scan_folder:
        kavita_scan_folders.add(str(scan_folder))
    if isinstance(event, dict):
        event["scan_source"] = "kapowarr_and_kavita" if volume_id is not None and kapowarr_adapter_enabled else "kavita_only"


def host_path_from_kavita_path(value):
    path = str(value or "").strip().replace("\\", "/").rstrip("/")
    if not path:
        return ""
    mappings = (
        (KAVITA_COMIC_ROOT, COMIC_ROOT),
        (KAVITA_MANGA_ROOT, MANGA_ROOT),
        (KAPOWARR_COMIC_ROOT, COMIC_ROOT),
    )
    for kavita_root, host_root in mappings:
        kavita_root = str(kavita_root or "").rstrip("/")
        if path == kavita_root or path.startswith(kavita_root + "/"):
            rel = path[len(kavita_root):].lstrip("/")
            return str(Path(host_root) / rel) if rel else str(host_root)
    if path.startswith("/mnt/") or path.startswith("/home/"):
        return path
    return ""


def inkdrop_series_targets(series_filter=None):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return []
    filters = {normalize(item) for item in (series_filter or []) if normalize(item)}
    try:
        conn = sqlite_connect(INKDROP_STATE_DB)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select id, title, media_type, year, publisher, metadata_provider, metadata_id,
                   kapowarr_id, source, library_path, library_adapter_path, raw_json
            from series
            where coalesce(monitored, 1) = 1
              and title is not null
              and (
                    coalesce(library_path, '') != ''
                 or coalesce(library_adapter_path, '') != ''
              )
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    targets = []
    for row in rows:
        title = row["title"]
        title_norm = normalize(title)
        if filters and title_norm not in filters:
            continue
        library_path = str(row["library_path"] or "").strip()
        adapter_folder = host_path_from_kavita_path(row["library_adapter_path"])
        folder = library_path or adapter_folder
        if not folder:
            continue
        metadata_provider = str(row["metadata_provider"] or row["source"] or "").strip().lower() or None
        metadata_id = str(row["metadata_id"] or "").strip() or None
        comicvine_id = metadata_id if metadata_provider == "comicvine" else None
        aliases = []
        append_comic_target_aliases(aliases, title)
        raw = {}
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except ValueError:
            raw = {}
        for key in ("alias", "aliases", "alt_title", "altTitle"):
            value = raw.get(key) if isinstance(raw, dict) else None
            if isinstance(value, list):
                for item in value:
                    append_comic_target_aliases(aliases, item)
            elif value:
                append_comic_target_aliases(aliases, value)
        targets.append(
            {
                "id": row["kapowarr_id"],
                "kapowarr_id": row["kapowarr_id"],
                "inkdrop_series_id": row["id"],
                "title": title,
                "year": row["year"],
                "publisher": row["publisher"],
                "media_type": row["media_type"],
                "folder": str(folder),
                "library_path": library_path or None,
                "library_adapter_path": row["library_adapter_path"],
                "adapter_folder": adapter_folder or None,
                "comicvine_id": comicvine_id,
                "metadata_provider": metadata_provider,
                "metadata_id": metadata_id,
                "native_series_id": row["id"],
                "special_version": None,
                "target_source": "inkdrop_series",
                "aliases": [normalize(alias) for alias in aliases if normalize(alias)],
            }
        )
    return targets


def target_identity_key(target):
    if not isinstance(target, dict):
        return ""
    for key in ("native_series_id", "inkdrop_series_id"):
        value = str(target.get(key) or "").strip()
        if value:
            return value
    provider = str(target.get("metadata_provider") or "").strip().lower()
    metadata_id = str(target.get("metadata_id") or "").strip()
    if provider and metadata_id:
        return f"{provider}:{metadata_id}"
    volume_id = target_kapowarr_volume_id(target)
    if volume_id is not None:
        return f"kapowarr:{volume_id}"
    return f"title:{normalize(target.get('title'))}|folder:{target.get('folder')}"


def annotate_target_alias_conflicts(targets):
    alias_identities = {}
    alias_targets = {}
    for target in targets or []:
        identity = target_identity_key(target)
        for alias in target.get("aliases") or []:
            alias = normalize(alias)
            if not alias:
                continue
            alias_identities.setdefault(alias, set()).add(identity)
            alias_targets.setdefault(alias, []).append(target)
    ambiguous_aliases = {
        alias
        for alias, identities in alias_identities.items()
        if len({value for value in identities if value}) > 1
    }
    for target in targets or []:
        target_ambiguous = sorted({alias for alias in (target.get("aliases") or []) if normalize(alias) in ambiguous_aliases})
        if not target_ambiguous:
            target.pop("ambiguous_aliases", None)
            target.pop("ambiguous_alias_targets", None)
            continue
        target["ambiguous_aliases"] = target_ambiguous
        related = []
        seen = set()
        for alias in target_ambiguous:
            for other in alias_targets.get(normalize(alias), []):
                identity = target_identity_key(other)
                if identity in seen:
                    continue
                seen.add(identity)
                related.append(
                    {
                        "title": other.get("title"),
                        "native_series_id": other.get("native_series_id") or other.get("inkdrop_series_id"),
                        "metadata_provider": other.get("metadata_provider"),
                        "metadata_id": other.get("metadata_id"),
                        "target_source": other.get("target_source"),
                        "folder": other.get("folder"),
                    }
                )
        target["ambiguous_alias_targets"] = related[:8]
    return targets


def load_comic_targets(series_filter=None):
    filters = {normalize(item) for item in (series_filter or []) if normalize(item)}
    targets = inkdrop_series_targets(series_filter)
    seen_native = {str(target.get("native_series_id") or "") for target in targets if target.get("native_series_id")}
    seen_titles = {normalize(target.get("title")) for target in targets if normalize(target.get("title"))}
    if not completed_import_kapowarr_adapter_enabled() or not KAPOWARR_DB.exists():
        return annotate_target_alias_conflicts(targets)
    conn = sqlite_connect(KAPOWARR_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select id, title, alt_title, year, publisher, folder, comicvine_id, special_version
            from volumes
            where folder is not null
              and title is not null
            """
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        host_folder = kapowarr_folder_to_host(row["folder"])
        if not host_folder:
            continue
        comicvine_id = row["comicvine_id"]
        metadata_provider = "comicvine" if comicvine_id not in (None, "") else "kapowarr"
        metadata_id = comicvine_id if comicvine_id not in (None, "") else row["id"]
        native_series_id = f"comicvine:{comicvine_id}" if comicvine_id not in (None, "") else None
        aliases = []
        append_comic_target_aliases(aliases, row["title"])
        if row["alt_title"]:
            append_comic_target_aliases(aliases, row["alt_title"])
        title_norm = normalize(row["title"])
        if filters and title_norm not in filters:
            continue
        if native_series_id and native_series_id in seen_native:
            continue
        if not native_series_id and title_norm in seen_titles:
            continue
        targets.append(
            {
                "id": row["id"],
                "kapowarr_id": row["id"],
                "inkdrop_series_id": native_series_id,
                "title": row["title"],
                "year": row["year"],
                "publisher": row["publisher"],
                "media_type": "manga" if is_manga_target({"title": row["title"], "publisher": row["publisher"]}) else "comic",
                "folder": str(host_folder),
                "comicvine_id": comicvine_id,
                "metadata_provider": metadata_provider,
                "metadata_id": str(metadata_id) if metadata_id not in (None, "") else None,
                "native_series_id": native_series_id,
                "special_version": row["special_version"],
                "target_source": "kapowarr_adapter",
                "aliases": [normalize(alias) for alias in aliases if normalize(alias)],
            }
        )
    return annotate_target_alias_conflicts(targets)


def match_comic_target(path, targets):
    if not targets:
        return None
    best = None
    best_score = 0
    candidates = [
        clean_words(path.name),
        clean_words(path.parent.name),
    ]
    for target in targets:
        for alias in target["aliases"]:
            if not alias:
                continue
            if alias in set(target.get("ambiguous_aliases") or []):
                continue
            alias_words = alias.split()
            for words in candidates:
                if len(words) < len(alias_words):
                    continue
                if words[: len(alias_words)] != alias_words:
                    continue
                if len(alias_words) == 1 and len(words) > 1 and (
                    words[1] in ONE_WORD_TITLE_SECOND_WORD_BLOCKLIST or not issue_marker_word(words[1])
                ):
                    continue
                score = len(alias_words) * 100 + len(alias)
                if score > best_score:
                    best = target
                    best_score = score
    return best


def ambiguous_comic_target_match(path, targets):
    if not targets:
        return None
    candidates = [
        clean_words(Path(path).name),
        clean_words(Path(path).parent.name),
    ]
    matches = {}
    for target in targets:
        for alias in target.get("ambiguous_aliases") or []:
            alias_words = alias.split()
            if not alias_words:
                continue
            for words in candidates:
                if len(words) < len(alias_words):
                    continue
                if not contains_sequence(words, alias_words):
                    continue
                if len(alias_words) == 1 and len(words) > 1 and (
                    words[1] in ONE_WORD_TITLE_SECOND_WORD_BLOCKLIST or not issue_marker_word(words[1])
                ):
                    continue
                for candidate in target.get("ambiguous_alias_targets") or []:
                    identity = str(candidate.get("native_series_id") or "")
                    if identity:
                        matches[identity] = candidate
                if not matches:
                    identity = target_identity_key(target)
                    matches[identity] = {
                        "title": target.get("title"),
                        "native_series_id": target.get("native_series_id") or target.get("inkdrop_series_id"),
                        "metadata_provider": target.get("metadata_provider"),
                        "metadata_id": target.get("metadata_id"),
                        "target_source": target.get("target_source"),
                        "folder": target.get("folder"),
                    }
                return {"alias": alias, "targets": list(matches.values())[:8]}
    return None


def trusted_target_native_ids(target):
    return set(completion_native_identity_candidates(target))


def trusted_comic_target(targets, volume_id=None, native_series_id=None):
    wanted_native = {str(value or "").strip() for value in ([native_series_id] if native_series_id not in (None, "") else [])}
    wanted_native = {value for value in wanted_native if value}
    if volume_id in (None, "") and not wanted_native:
        return None
    wanted_volume = None
    if volume_id not in (None, ""):
        try:
            wanted_volume = int(volume_id)
        except (TypeError, ValueError):
            wanted_volume = None
    for target in targets or []:
        volume_match = False
        if wanted_volume is not None:
            try:
                volume_match = int(target.get("id")) == wanted_volume
            except (TypeError, ValueError):
                volume_match = False
        native_match = bool(wanted_native and wanted_native.intersection(trusted_target_native_ids(target)))
        if wanted_volume is not None and wanted_native:
            if volume_match and native_match:
                return target
            continue
        if wanted_volume is not None and volume_match:
            return target
        if native_match:
            return target
    return None


def trusted_issue_title_from_inkdrop_state(trusted_series_id, trusted_issue, trusted_issue_id=None):
    if trusted_series_id in (None, "") or trusted_issue in (None, "") or not INKDROP_STATE_DB.exists():
        return ""
    trusted_series_id = str(trusted_series_id or "").strip()
    provider, metadata_id = split_prefixed_identity(trusted_series_id)
    issue_terms = []
    issue_params = []
    for key in sorted(completion_number_keys(trusted_issue)):
        normalized = None
        if inkdrop_state is not None:
            try:
                normalized = inkdrop_state.normalize_issue_number(key)
            except Exception:
                normalized = None
        issue_terms.append("(i.issue_number = ? or i.normalized_number = ? or i.normalized_number = ?)")
        issue_params.extend([key, key, normalized or key])
    if not issue_terms:
        return ""

    conn = sqlite_connect(INKDROP_STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        direct_series = conn.execute(
            "select id from series where id=? and coalesce(monitored,1)=1 limit 2",
            (trusted_series_id,),
        ).fetchall()
        if direct_series:
            series_ids = [str(row["id"]) for row in direct_series]
        elif provider and metadata_id:
            provider_series = conn.execute(
                "select id from series where lower(coalesce(metadata_provider,''))=? and metadata_id=? "
                "and coalesce(monitored,1)=1 limit 2",
                (provider, metadata_id),
            ).fetchall()
            series_ids = [str(row["id"]) for row in provider_series]
        else:
            series_ids = []
        if len(series_ids) != 1:
            return ""

        issue_identity_sql = ""
        issue_identity_params = []
        if trusted_issue_id not in (None, ""):
            issue_identity_sql = "and i.id=?"
            issue_identity_params.append(str(trusted_issue_id).strip())
        rows = conn.execute(
            f"""
            select i.title
            from issues i
            where i.series_id=?
              and ({' or '.join(issue_terms)})
              {issue_identity_sql}
              and coalesce(i.monitored,1)=1
              and nullif(trim(coalesce(i.title, '')), '') is not null
              and (
                    exists(
                        select 1 from wanted_items wi
                        where wi.issue_id=i.id and wi.series_id=i.series_id
                          and lower(coalesce(wi.status,'')) in ('wanted','in_progress')
                    )
                    or exists(
                        select 1 from queue_items q
                        where q.issue_id=i.id and q.series_id=i.series_id and q.active=1
                    )
              )
            order by i.id
            """,
            [series_ids[0]] + issue_params + issue_identity_params,
        ).fetchall()
    finally:
        conn.close()
    canonical_titles = {}
    for row in rows:
        title = str(row["title"] or "").strip()
        normalized_title = normalize(title)
        if normalized_title:
            canonical_titles.setdefault(normalized_title, title)
    return next(iter(canonical_titles.values())) if len(canonical_titles) == 1 else ""


def trusted_issue_title_evidence(trusted_series_id, trusted_issue, trusted_issue_id=None, supplied_title=None):
    canonical_title = trusted_issue_title_from_inkdrop_state(
        trusted_series_id,
        trusted_issue,
        trusted_issue_id,
    )
    if supplied_title not in (None, ""):
        if (
            trusted_issue_id not in (None, "")
            and canonical_title
            and normalize(canonical_title) == normalize(supplied_title)
        ):
            return canonical_title
        return ""
    return canonical_title


def target_identity_fields(target):
    if not isinstance(target, dict):
        return {}
    out = {}
    for key in (
        "native_series_id",
        "metadata_provider",
        "metadata_id",
        "comicvine_id",
    ):
        value = target.get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def media_management_import_row(target=None, event=None, source_path=None, kind="comics"):
    target = target if isinstance(target, dict) else {}
    event = event if isinstance(event, dict) else {}
    source = Path(source_path) if source_path else None
    identity_record = {**target, **{key: value for key, value in event.items() if value not in (None, "")}}
    if event.get("truth_model") == "kavita_manga":
        identity_record["canonical_media_type"] = "manga"
        identity_record["media_type"] = "manga"
        identity_record.pop("work_media_type", None)
        identity_record.pop("series_media_type", None)
    classification = inkdrop_library_identity.canonical_library_classification(identity_record)
    media_type = classification.get("media_type") if classification.get("ok") else "unknown"
    source_chapter = ""
    source_volume = ""
    if media_type == "manga" and source is not None:
        source_chapter = source_chapter_number(source) or ""
        source_volume = suwayomi_volume_number(source) or ""
    trusted_issue = (
        event.get("trusted_issue")
        or event.get("trusted_issue_number")
        or event.get("trustedIssue")
    )
    source_unit = str(event.get("source_unit") or event.get("unit_type") or event.get("unitType") or "").strip().lower()
    issue_number = (
        trusted_issue
        or (event.get("chapter_number") if source_unit == "chapter" else None)
        or (source_chapter if source_unit == "chapter" else None)
        or event.get("canonical_issue_number")
        or event.get("issue_number")
        or event.get("normalized_number")
        or target.get("issue_number")
    )
    if not issue_number and source is not None:
        extracted = extract_issue_number(source)
        issue_number = normalize_manga_number(extracted) if extracted is not None else ""
    if not source_unit and media_type == "comic" and issue_number:
        source_unit = "issue"
    result = {
        "series": event.get("matched_series") or target.get("title") or (source.stem if source is not None else "Unknown Series"),
        "title": event.get("matched_series") or target.get("title") or (source.stem if source is not None else "Unknown Series"),
        "media_type": media_type,
        "library_classification": classification,
        "year": event.get("canonical_year") or event.get("issue_year") or event.get("year") or target.get("year"),
        "series_year": target.get("year") or event.get("series_year") or event.get("year"),
        "issue_year": event.get("canonical_year") or event.get("issue_year") or event.get("year"),
        "canonical_year": event.get("canonical_year"),
        "release_date": event.get("release_date") or event.get("date"),
        "publisher": target.get("publisher"),
        "issue_number": issue_number,
        "normalized_number": event.get("normalized_number") or issue_number,
        "issue_title": event.get("issue_title") or target.get("issue_title"),
        "volume": (event.get("source_volume_number") or source_volume or target.get("volume_number")) if source_unit == "volume" else "",
        "volume_number": (event.get("source_volume_number") or source_volume or target.get("volume_number")) if source_unit == "volume" else "",
        "chapter": (trusted_issue or event.get("chapter_number") or source_chapter or event.get("canonical_issue_number") or issue_number) if source_unit == "chapter" else "",
        "chapter_number": (trusted_issue or event.get("chapter_number") or source_chapter or event.get("canonical_issue_number") or issue_number) if source_unit == "chapter" else "",
        "collected_number": event.get("collected_number") or event.get("collection_number") if source_unit in {"collected", "collected_edition"} else "",
        "pack_member_number": event.get("pack_member_number") or issue_number if source_unit == "pack_member" else "",
        "source_unit": source_unit,
        "unit_type": source_unit,
        "manga_unit_model": event.get("manga_unit_model") or target.get("manga_unit_model"),
        "manga_unit_policy": event.get("manga_unit_policy") or event.get("series_unit_policy") or target.get("manga_unit_policy"),
        "manga_unit_policy_label": event.get("manga_unit_policy_label") or event.get("series_unit_policy_label"),
        "manga_unit_allows_chapter": event.get("manga_unit_allows_chapter") if event.get("manga_unit_allows_chapter") is not None else event.get("series_unit_allows_chapter"),
        "manga_unit_allows_volume": event.get("manga_unit_allows_volume") if event.get("manga_unit_allows_volume") is not None else event.get("series_unit_allows_volume"),
        "import_raw_json": event,
        "metadata_provider": target.get("metadata_provider"),
        "metadata_id": target.get("metadata_id"),
        "native_series_id": completion_native_series_id(target) or event.get("native_series_id"),
        "source_path": str(source_path or event.get("source") or ""),
    }
    if classification.get("ok"):
        result["canonical_media_type"] = media_type
    return result


def media_management_import_preview(target=None, event=None, source_path=None, dest_path=None, kind="comics", settings=None):
    if inkdrop_state is None or not INKDROP_STATE_DB.exists():
        return {}
    target = target if isinstance(target, dict) else {}
    event = event if isinstance(event, dict) else {}
    try:
        row = media_management_import_row(target, event, source_path, kind)
        preview = inkdrop_state.media_management_destination_preview(
            INKDROP_STATE_DB,
            row,
            source_path=str(source_path or event.get("source") or ""),
            dest_path=str(dest_path or event.get("dest") or ""),
            settings=settings,
        )
    except Exception as exc:
        return {
            "preview_only": True,
            "mutates_filesystem": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    importer_dest = str(dest_path or event.get("dest") or "").replace("\\", "/")
    planned = str(preview.get("planned_path") or "").replace("\\", "/")
    preview["current_import_dest_path"] = importer_dest
    preview["current_import_dest_matches_preview"] = bool(importer_dest and planned and importer_dest == planned)
    preview["consumed_by"] = "inkdrop_completed_import"
    return preview


def media_management_setting_bool(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def media_management_import_roots(settings=None):
    settings = settings if isinstance(settings, dict) else {}
    roots = [
        settings.get("comic_root"),
        settings.get("manga_root"),
        COMIC_ROOT,
        MANGA_ROOT,
    ]
    out = []
    seen = set()
    for root in roots:
        text = str(root or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(Path(text))
    return out


def media_management_path_under_root(path, roots):
    try:
        candidate = Path(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    for root in roots:
        try:
            root_path = Path(root).resolve(strict=False)
            candidate.relative_to(root_path)
            return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def media_management_matching_root(path, roots):
    try:
        candidate = Path(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    matches = []
    for root in roots:
        try:
            root_path = Path(root).resolve(strict=False)
            candidate.relative_to(root_path)
            matches.append(root_path)
        except (OSError, RuntimeError, ValueError):
            continue
    if not matches:
        return None
    return sorted(matches, key=lambda item: len(str(item)), reverse=True)[0]


def media_management_minimum_free_space_gb(settings=None):
    try:
        return max(0.0, float((settings or {}).get("minimum_free_space_gb") or 0))
    except (TypeError, ValueError):
        return 0.0


def media_management_space_check(planned_path, source_path=None, settings=None, root_path=None):
    minimum_gb = media_management_minimum_free_space_gb(settings)
    source_size = 0
    if source_path:
        try:
            source_size = int(Path(source_path).stat().st_size)
        except (OSError, RuntimeError, ValueError):
            source_size = 0
    if minimum_gb <= 0 and source_size <= 0:
        return {
            "ok": True,
            "minimum_free_space_gb": minimum_gb,
            "required_free_space_gb": 0.0,
            "source_size_bytes": source_size,
        }
    probe = Path(root_path) if root_path else Path(planned_path).parent
    try:
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not probe.exists():
            return {
                "ok": False,
                "reason": "storage_probe_missing",
                "probe_path": str(probe),
                "minimum_free_space_gb": minimum_gb,
                "source_size_bytes": source_size,
            }
        usage = shutil.disk_usage(probe)
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "ok": False,
            "reason": "storage_probe_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "probe_path": str(probe),
            "minimum_free_space_gb": minimum_gb,
            "source_size_bytes": source_size,
        }
    required_bytes = int(minimum_gb * (1024 ** 3)) + max(0, source_size)
    ok = usage.free >= required_bytes
    return {
        "ok": ok,
        "reason": None if ok else "minimum_free_space_floor",
        "probe_path": str(probe),
        "free_bytes": int(usage.free),
        "free_space_gb": round(float(usage.free) / float(1024 ** 3), 2),
        "minimum_free_space_gb": minimum_gb,
        "required_free_space_gb": round(float(required_bytes) / float(1024 ** 3), 2),
        "source_size_bytes": source_size,
    }


def media_management_import_destination_decision(target=None, event=None, source_path=None, legacy_dest=None, kind="comics", settings=None):
    settings = settings if isinstance(settings, dict) else {}
    legacy = Path(legacy_dest) if legacy_dest else None
    preview = media_management_import_preview(
        target,
        event,
        source_path=source_path,
        dest_path=str(legacy) if legacy else "",
        kind=kind,
        settings=settings,
    )
    enabled = media_management_setting_bool(settings.get("apply_planned_path"), True)
    override = bool(settings.get("apply_planned_path_override"))
    planned_text = str((preview or {}).get("planned_path") or "").strip()
    decision = {
        "enabled": enabled,
        "override": override,
        "legacy_dest_path": str(legacy) if legacy else "",
        "selected_dest_path": str(legacy) if legacy else "",
        "planned_path": planned_text,
        "applied": False,
        "skip_existing_destination": False,
        "reason": "setting_disabled" if not enabled else "planned_path_missing",
    }
    if not preview:
        decision["reason"] = "preview_unavailable"
        return legacy, preview, decision
    preview_gate = str(preview.get("planned_path_apply_status") or "").strip().lower()
    if preview_gate in {"blocked_work_identity", "blocked_library_classification", "blocked_unit_identity"}:
        decision.update({"blocked": True, "reason": preview_gate})
        preview["planned_path_applied"] = False
        preview["selected_import_dest_path"] = ""
        preview["legacy_import_dest_path"] = decision["legacy_dest_path"]
        return None, preview, decision
    if not enabled:
        preview["planned_path_apply_status"] = "disabled"
        preview["planned_path_applied"] = False
        preview["apply_planned_path_override"] = override
        preview["selected_import_dest_path"] = decision["selected_dest_path"]
        preview["legacy_import_dest_path"] = decision["legacy_dest_path"]
        return legacy, preview, decision
    if not planned_text:
        preview["planned_path_apply_status"] = "blocked_missing_planned_path"
        preview["planned_path_applied"] = False
        preview["apply_planned_path_override"] = override
        preview["selected_import_dest_path"] = decision["selected_dest_path"]
        preview["legacy_import_dest_path"] = decision["legacy_dest_path"]
        return legacy, preview, decision
    planned = Path(planned_text)
    if not planned.is_absolute():
        decision["reason"] = "planned_path_not_absolute"
        preview["planned_path_apply_status"] = "blocked_not_absolute"
        preview["planned_path_applied"] = False
        preview["apply_planned_path_override"] = override
        preview["selected_import_dest_path"] = decision["selected_dest_path"]
        preview["legacy_import_dest_path"] = decision["legacy_dest_path"]
        return legacy, preview, decision
    if not media_management_path_under_root(planned, media_management_import_roots(settings)):
        decision["reason"] = "planned_path_outside_configured_roots"
        preview["planned_path_apply_status"] = "blocked_outside_configured_roots"
        preview["planned_path_applied"] = False
        preview["apply_planned_path_override"] = override
        preview["selected_import_dest_path"] = decision["selected_dest_path"]
        preview["legacy_import_dest_path"] = decision["legacy_dest_path"]
        return legacy, preview, decision
    roots = media_management_import_roots(settings)
    matching_root = media_management_matching_root(planned, roots)
    if matching_root is None or not Path(matching_root).exists():
        decision["reason"] = "planned_path_root_missing"
        preview["planned_path_apply_status"] = "blocked_root_missing"
        preview["planned_path_applied"] = False
        preview["apply_planned_path_override"] = override
        preview["selected_import_dest_path"] = decision["selected_dest_path"]
        preview["legacy_import_dest_path"] = decision["legacy_dest_path"]
        preview["root_exists"] = False
        preview["minimum_free_space_gb"] = media_management_minimum_free_space_gb(settings)
        return legacy, preview, decision
    space_check = media_management_space_check(planned, source_path=source_path, settings=settings, root_path=matching_root)
    preview["free_space_ok"] = bool(space_check.get("ok"))
    preview["free_space_status"] = "ok" if space_check.get("ok") else "blocked"
    preview["free_space_gb"] = space_check.get("free_space_gb")
    preview["free_space_probe_path"] = space_check.get("probe_path")
    preview["minimum_free_space_gb"] = space_check.get("minimum_free_space_gb")
    preview["required_free_space_gb"] = space_check.get("required_free_space_gb")
    preview["source_size_bytes"] = space_check.get("source_size_bytes")
    if not space_check.get("ok"):
        decision["reason"] = "planned_path_minimum_free_space_floor"
        preview["planned_path_apply_status"] = "blocked_free_space_floor"
        preview["planned_path_applied"] = False
        preview["apply_planned_path_override"] = override
        preview["selected_import_dest_path"] = decision["selected_dest_path"]
        preview["legacy_import_dest_path"] = decision["legacy_dest_path"]
        preview["free_space_block_reason"] = space_check.get("reason") or "minimum_free_space_floor"
        return legacy, preview, decision
    work_id = completion_native_series_id(target) or event.get("native_series_id") if isinstance(event, dict) else completion_native_series_id(target)
    persistence = inkdrop_state.persist_series_folder_identity(
        INKDROP_STATE_DB,
        work_id,
        preview.get("series_folder"),
        library_type=(preview.get("library_classification") or {}).get("library_type"),
    ) if inkdrop_state is not None else {"ok": False, "reason": "state_unavailable"}
    preview["series_folder_persistence"] = persistence
    if not persistence.get("ok"):
        decision.update({"blocked": True, "reason": persistence.get("reason") or "series_folder_identity_claim_failed"})
        preview["planned_path_apply_status"] = "blocked_series_folder_identity"
        preview["planned_path_applied"] = False
        preview["selected_import_dest_path"] = ""
        return None, preview, decision
    existing_text = str((preview or {}).get("existing_dest_path") or "").strip()
    if existing_text:
        existing_path = Path(existing_text)
        if existing_path.exists():
            decision.update(
                {
                    "selected_dest_path": str(existing_path),
                    "skip_existing_destination": True,
                    "reason": "existing_destination_in_series_tree",
                    "conflict_action": str((preview or {}).get("conflict_action") or "skip_existing"),
                }
            )
            preview["planned_path_apply_status"] = "blocked_existing_destination"
            preview["planned_path_applied"] = False
            preview["apply_planned_path_override"] = override
            preview["selected_import_dest_path"] = decision["selected_dest_path"]
            preview["legacy_import_dest_path"] = decision["legacy_dest_path"]
            preview["current_import_dest_path"] = decision["selected_dest_path"]
            preview["current_import_dest_matches_preview"] = False
            return existing_path, preview, decision
    if planned.exists():
        decision.update(
            {
                "selected_dest_path": str(planned),
                "skip_existing_destination": True,
                "reason": "planned_path_exists",
                "conflict_action": str((preview or {}).get("conflict_action") or "skip_existing"),
            }
        )
        preview["planned_path_apply_status"] = "blocked_existing_destination"
        preview["planned_path_applied"] = False
        preview["apply_planned_path_override"] = override
        preview["selected_import_dest_path"] = decision["selected_dest_path"]
        preview["legacy_import_dest_path"] = decision["legacy_dest_path"]
        preview["current_import_dest_path"] = decision["selected_dest_path"]
        preview["current_import_dest_matches_preview"] = True
        return planned, preview, decision
    decision.update(
        {
            "selected_dest_path": str(planned),
            "applied": True,
            "reason": "planned_path_selected",
        }
    )
    preview["planned_path_apply_status"] = "selected"
    preview["planned_path_applied"] = True
    preview["apply_planned_path_override"] = override
    preview["selected_import_dest_path"] = decision["selected_dest_path"]
    preview["legacy_import_dest_path"] = decision["legacy_dest_path"]
    preview["current_import_dest_path"] = decision["selected_dest_path"]
    preview["current_import_dest_matches_preview"] = True
    return planned, preview, decision


def target_single_issue_artifact_title(target):
    issue_title = str((target or {}).get("issue_title") or "").strip()
    if not issue_title:
        return False
    return normalize(issue_title) in {"tpb", "trade paperback", "one shot", "oneshot", "graphic novel"}


def trusted_issue_missing_source_number_is_safe(path, target, trusted_issue, comicinfo=None):
    expected = format_issue_number(trusted_issue)
    if expected != "001":
        return False
    if not target or not is_manga_target(target) or not target_single_issue_artifact_title(target):
        return False
    if extract_issue_number(path) is not None:
        return False
    if (
        filename_has_range_or_pack(path)
        or filename_duplicate_copy_suffix(path)
        or filename_has_weak_numeric_prefix(path)
        or filename_has_explicit_unit_token(path)
        or filename_has_chapter_token(path)
    ):
        return False
    source_text = source_identity_text(path)
    if re.search(r"\b(?:volume|vol|v)[\s._-]*0*\d{1,5}(?:\.\d+)?\b", source_text, re.I):
        return False
    info = comicinfo if comicinfo is not None else read_comicinfo(path)
    info_number = comicinfo_text(info, "Number") or comicinfo_text(info, "Volume")
    if info_number and format_issue_number(info_number) != expected:
        return False
    info_format = comicinfo_text(info, "Format")
    info_title = comicinfo_text(info, "Title")
    if re.search(r"\b(?:chapter|chap|ch)\b", f"{info_format} {info_title}", re.I):
        return False
    if not (
        matching_target_alias(clean_words(Path(path).stem), target)
        or matching_target_alias(clean_words(Path(path).parent.name), target)
    ):
        return False
    if not filename_year_matches(path, target):
        return False
    return True


def trusted_issue_mismatch_reason(path, trusted_issue, target=None, comicinfo=None):
    if trusted_issue in (None, ""):
        return None
    expected = format_issue_number(trusted_issue)
    if not expected:
        return None
    source_number = extract_issue_number(path)
    if source_number is None:
        if trusted_issue_missing_source_number_is_safe(path, target, trusted_issue, comicinfo=comicinfo):
            return None
        return "trusted_issue_missing_source_number"
    actual = format_issue_number(source_number)
    if actual != expected:
        if source_contains_trusted_issue_number(path, expected):
            return None
        return f"trusted_issue_mismatch:{actual or 'unknown'}!={expected}"
    return None


def load_pending_imports(kind):
    if not PENDING_IMPORTS_LOG.exists():
        return []
    latest = {}
    with PENDING_IMPORTS_LOG.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") != kind:
                continue
            for key in ("title", "query"):
                alias = normalize(record.get(key))
                if alias:
                    latest[alias] = record
    pending = []
    seen_ids = set()
    for alias, record in latest.items():
        if record.get("status") != "sent":
            continue
        record_id = (normalize(record.get("title")), normalize(record.get("query")))
        if record_id in seen_ids:
            continue
        aliases = [normalize(record.get("title")), normalize(record.get("query"))]
        aliases = [item for item in aliases if item]
        pending.append({**record, "aliases": aliases})
        seen_ids.add(record_id)
    return pending


def matches_pending_import(path, pending):
    return bool(matched_pending_records(path, pending))


def matched_pending_records(path, pending):
    if not pending:
        return []
    haystack_words = clean_words(" ".join([path.name, path.parent.name]))
    matched = []
    for record in pending:
        for alias in record["aliases"]:
            alias_words = alias.split()
            if contains_sequence(haystack_words, alias_words):
                matched.append(record)
                break
    return matched


def pending_only_source_roots(sources, pending):
    if not pending:
        return []
    selected = []
    seen = set()
    for record in pending:
        source = record.get("source")
        if source:
            path = Path(source)
            if path.exists():
                key = str(path)
                if key not in seen:
                    selected.append(path)
                    seen.add(key)
    for root in sources:
        root = Path(root)
        if not root.exists():
            continue
        if root.is_file():
            candidates = [root]
        else:
            try:
                candidates = list(root.iterdir())
            except OSError as exc:
                log({"event": "pending_source_root_read_failed", "root": str(root), "error": str(exc)})
                continue
        for candidate in candidates:
            if not matches_pending_import(candidate, pending):
                continue
            key = str(candidate)
            if key in seen:
                continue
            selected.append(candidate)
            seen.add(key)
    return selected


def append_pending_status(kind, path, status, dest=None, target=None, pending=None):
    matched = matched_pending_records(path, pending or [])
    if not matched:
        return 0
    identity_keys = (
        "downloadUrlHash",
        "download_url_hash",
        "url_hash",
        "downloadUrlHost",
        "download_url_host",
        "indexer",
        "indexerId",
        "client_id",
        "client_hash",
        "nzo_id",
        "nzo_ids",
        "protocol",
    )
    now = time.time()
    with PENDING_IMPORTS_LOG.open("a", encoding="utf-8") as handle:
        for record in matched:
            payload = {
                "event": "pending_import_status",
                "type": kind,
                "status": status,
                "title": record.get("title"),
                "query": record.get("query"),
                "source": str(path),
                "dest": str(dest) if dest else None,
                "matched_series": target.get("title") if target else None,
                "matched_kapowarr_id": target.get("id") if target else None,
                "created_at": now,
            }
            for key in identity_keys:
                value = record.get(key)
                if value not in (None, "", []):
                    payload[key] = value
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return len(matched)


def manga_import_needs_library_scan(event):
    return event.get("truth_model") == "kavita_manga" and bool(event.get("auto_set_manga_unit_model"))


def touch_kavita_scan_folder(folder, source=None):
    try:
        folder = Path(folder)
        if not folder.exists() or not folder.is_dir():
            return False
        os.utime(folder, None)
        log({
            "event": "kavita_scan_folder_touched",
            "folder": str(folder),
            "source": str(source) if source else None,
        })
        return True
    except Exception as exc:
        log({
            "event": "kavita_scan_folder_touch_failed",
            "folder": str(folder),
            "source": str(source) if source else None,
            "error": str(exc),
        })
        return False


def qbit_host_path(save_path, file_name, qbit=None):
    save_path = str(save_path or "").rstrip("/")
    file_name = str(file_name or "").lstrip("/")
    if not save_path.startswith(QBIT_CONTAINER_DOWNLOAD_ROOT):
        return None
    rel_save = save_path[len(QBIT_CONTAINER_DOWNLOAD_ROOT):].lstrip("/")
    return QBIT_HOST_DOWNLOAD_ROOT / rel_save / file_name


def load_qbit_incomplete_paths(kind):
    if kind not in {"comics", "ebooks"}:
        return set()
    try:
        qbit = load_qbit_settings()
        if not qbit.get("user") or not qbit.get("pass"):
            raise RuntimeError("qBittorrent credentials are unavailable")
        target_category = qbit["comics_category"] if kind == "comics" else qbit["ebooks_category"]
        target_save_path = qbit["comics_save_path"] if kind == "comics" else qbit["ebooks_save_path"]
        category_keys = {normalize(target_category)}
        if kind == "comics":
            category_keys.update({normalize("comics"), normalize("kapowarr")})
        else:
            category_keys.update({normalize("readarr")})
        save_paths = {
            str(path or "").strip().lower().rstrip("/")
            for path in (target_save_path, "/downloads/comics" if kind == "comics" else "/downloads/readarr")
            if str(path or "").strip()
        }
        session = requests.Session()
        login = session.post(
            qbit["host"] + "/api/v2/auth/login",
            data={"username": qbit["user"], "password": qbit["pass"]},
            timeout=15,
        )
        login.raise_for_status()
        torrents = session.get(qbit["host"] + "/api/v2/torrents/info", timeout=20).json()
        incomplete = set()
        for torrent in torrents:
            category = normalize(torrent.get("category"))
            save_path = str(torrent.get("save_path") or torrent.get("content_path") or "").strip().lower().rstrip("/")
            tags = {
                part.strip().lower()
                for part in str(torrent.get("tags") or "").split(",")
                if part.strip()
            }
            tagged = bool(tags & QBIT_BROAD_TAGS)
            save_path_match = bool(save_path and any(save_path.startswith(path) for path in save_paths if path))
            if category not in category_keys and not tagged and not save_path_match:
                continue
            if float(torrent.get("progress") or 0) >= 1:
                continue
            files = session.get(
                qbit["host"] + "/api/v2/torrents/files",
                params={"hash": torrent["hash"]},
                timeout=20,
            ).json()
            for item in files:
                if float(item.get("progress") or 0) >= 0.999:
                    continue
                host_path = qbit_host_path(torrent.get("save_path"), item.get("name"), qbit)
                if host_path:
                    incomplete.add(str(host_path))
        return incomplete
    except Exception as exc:
        log({"event": "qbit_incomplete_probe_failed", "kind": kind, "error": str(exc)})
        return set()

def scan_sources(kind, manual_inbox=False, suwayomi_staging=False, slskd_staging=False):
    if kind == "comics":
        if suwayomi_staging:
            return SUWAYOMI_COMIC_SOURCES, COMIC_DEST
        if slskd_staging:
            return SLSKD_COMIC_SOURCES, COMIC_DEST
        return (MANUAL_COMIC_SOURCES if manual_inbox else COMIC_SOURCES), COMIC_DEST
    return (MANUAL_EBOOK_SOURCES if manual_inbox else EBOOK_SOURCES), EBOOK_DEST


IMPORT_FILENAME_RANGE_PATTERNS = (
    r"\b(?:v|vol(?:ume)?s?)\.?\s*0*\d+(?:\.\d+)?\s*(?:-|\u2013|\u2014|to)\s*(?:v|vol(?:ume)?s?)?\.?\s*0*\d+(?:\.\d+)?\b",
    r"\b(?:ch|chap(?:ter)?s?|issues?)\.?\s*0*\d+(?:\.\d+)?\s*(?:-|\u2013|\u2014|to)\s*(?:ch|chap(?:ter)?s?|issues?)?\.?\s*0*\d+(?:\.\d+)?\b",
    r"\b0*\d+(?:\.\d+)?\s*(?:-|\u2013|\u2014|to)\s*0*\d+(?:\.\d+)?\b",
    r"\b(?:complete|collection|pack|set)\b",
)
PUBLICATION_MONTH_RANGE_PATTERN = r"\b(?:19|20)\d{2}\s*(?:-|\u2013|\u2014)\s*(?:0?[1-9]|1[0-2])\b"
IMPORT_FILENAME_SAME_UNIT_DUPLICATE_PATTERN = re.compile(
    r"\b(?P<left_unit>ch|chap(?:ter)?s?|issues?)\.?\s*0*(?P<left>\d{1,5}(?:\.\d+)?)"
    r"\s*(?:-|\u2013|\u2014|to)\s*"
    r"(?:(?P<right_unit>ch|chap(?:ter)?s?|issues?)\.?\s*)?0*(?P<right>\d{1,5}(?:\.\d+)?)\b",
    re.I,
)


def strip_publication_month_ranges(text):
    return re.sub(PUBLICATION_MONTH_RANGE_PATTERN, " ", str(text or ""), flags=re.I)


def strip_duplicate_same_unit_ranges(text):
    def replacement(match):
        try:
            left = float(match.group("left"))
            right = float(match.group("right"))
        except (TypeError, ValueError):
            return match.group(0)
        if left != right:
            return match.group(0)
        left_unit = str(match.group("left_unit") or "").strip()
        right_unit = str(match.group("right_unit") or "").strip()
        if right_unit and left_unit[:2].lower() != right_unit[:2].lower():
            return match.group(0)
        return f"{left_unit} {match.group('left')}"

    return IMPORT_FILENAME_SAME_UNIT_DUPLICATE_PATTERN.sub(replacement, str(text or ""))


def target_aliases(target):
    aliases = []
    for alias in (target or {}).get("aliases") or []:
        cleaned = normalize(alias)
        if cleaned:
            aliases.append(cleaned)
    title = normalize((target or {}).get("title") or (target or {}).get("series"))
    if title:
        aliases.append(title)
    out = []
    seen = set()
    for alias in aliases:
        if alias not in seen:
            seen.add(alias)
            out.append(alias)
    return out


def matching_target_alias(words, target):
    best = ""
    for alias in target_aliases(target):
        alias_words = alias.split()
        if contains_sequence(words, alias_words) and len(alias) > len(best):
            best = alias
    return best


def target_has_issue_number(target, number):
    formatted = format_issue_number(number)
    if not target or not formatted or not completed_import_kapowarr_adapter_enabled() or not KAPOWARR_DB.exists():
        return False
    try:
        wanted = float(number)
        volume_id = int(target.get("id"))
    except (TypeError, ValueError):
        return False
    conn = sqlite_connect(KAPOWARR_DB)
    try:
        row = conn.execute(
            """
            select 1
            from issues
            where volume_id = ?
              and (
                issue_number = ?
                or issue_number = ?
                or abs(calculated_issue_number - ?) < 0.001
              )
            limit 1
            """,
            (volume_id, formatted, str(int(wanted)) if wanted.is_integer() else str(wanted), wanted),
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def filename_has_range_or_pack(path):
    text = strip_publication_month_ranges(Path(path).stem.replace("_", " ").lower())
    text = strip_duplicate_same_unit_ranges(text)
    return any(re.search(pattern, text, re.I) for pattern in IMPORT_FILENAME_RANGE_PATTERNS)


def filename_has_explicit_unit_token(path):
    text = Path(path).stem.replace("_", " ")
    return bool(
        re.search(
            r"(?:^|[\s._\-\(\[])(?:#|issue|no|number|chapter|chap|ch|vol(?:ume)?|v)[\s._#-]*0*\d{1,5}(?:\.\d+)?\b",
            text,
            re.I,
        )
        or re.search(r"\b0*\d{1,5}(?:\.\d+)?\s*(?:of|/)\s*\d{1,5}\b", text, re.I)
    )


def filename_has_weak_numeric_prefix(path):
    return bool(re.search(r"^\s*\d{1,5}(?:\.\d+)?[\s_.-]+[A-Za-z]", Path(path).stem))


def filename_duplicate_copy_suffix(path):
    match = re.search(r"\s\(([2-9][0-9]*)\)$", Path(path).stem)
    if not match:
        return False
    try:
        value = int(match.group(1))
    except ValueError:
        return False
    return not (1900 <= value <= 2099)


def filename_has_chapter_token(path, target=None):
    stem = Path(path).stem
    if re.search(r"\b(?:chapter|chap|ch)\.?\s*0*\d{1,5}(?:\.\d+)?\b", stem, re.I):
        return True
    compact_matches = list(re.finditer(r"\bc\.?\s*0*\d{1,5}(?:\.\d+)?\b", stem, re.I))
    if not compact_matches:
        return False
    target_title = str((target or {}).get("title") or (target or {}).get("series") or "")
    target_tokens = {token.casefold() for token in re.findall(r"[A-Za-z]+\d+", target_title)}
    return any(re.sub(r"[^A-Za-z0-9]", "", match.group(0)).casefold() not in target_tokens for match in compact_matches)


def target_looks_volume_based(target):
    if is_volume_as_issue_target(target):
        return True
    try:
        folder_name = Path((target or {}).get("folder") or "").name
    except TypeError:
        folder_name = ""
    if re.match(r"(?i)^(?:volume|vol)[\s._-]*\d+", folder_name):
        return True
    title = str((target or {}).get("title") or "")
    return bool(re.search(r"\b(?:volume|vol|v)[\s._-]*\d+\b", title, re.I))


def filename_year_matches(path, target):
    expected = str((target or {}).get("year") or "").strip()
    if not re.fullmatch(r"(?:19|20)\d{2}", expected):
        return False
    return bool(re.search(rf"(?:^|[^\d]){re.escape(expected)}(?:[^\d]|$)", Path(path).stem))


def comicinfo_unit_matches(info, trusted_issue=None, source_number=None):
    info = info or {}
    number = comicinfo_text(info, "Number") or comicinfo_text(info, "Volume")
    formatted = format_issue_number(number)
    if not formatted:
        return False
    if trusted_issue not in (None, ""):
        return formatted == format_issue_number(trusted_issue)
    if source_number is not None:
        return formatted == format_issue_number(source_number)
    return True


def native_manga_bare_number_is_safe(path, target, title_alias, source_number, collection_target=False):
    if not title_alias or not source_number or collection_target:
        return False
    if not is_manga_target(target):
        return False
    if str((target or {}).get("target_source") or "") != "inkdrop_series":
        return False
    try:
        integer = int(float(source_number))
    except (TypeError, ValueError):
        return False
    if 1900 <= integer <= 2099:
        return False
    stem = Path(path).stem
    return bool(re.search(rf"(?:^|[\s._-])0*{integer}(?:\.\d+)?(?:$|[\s._-])", stem, re.I))


def native_manga_bare_volume_import_is_safe(path, target, number):
    title_alias = matching_target_alias(clean_words(Path(path).stem), target)
    return bool(
        native_manga_bare_number_is_safe(path, target, title_alias, number)
        and not filename_has_explicit_unit_token(path)
        and not filename_has_chapter_token(path)
    )


def target_allows_native_chapter_import(target):
    provider = str((target or {}).get("metadata_provider") or (target or {}).get("metadataProvider") or "").strip().lower()
    source = str((target or {}).get("source") or (target or {}).get("target_source") or "").strip().lower()
    native_id = str(
        (target or {}).get("native_series_id")
        or (target or {}).get("inkdrop_series_id")
        or (target or {}).get("metadata_id")
        or ""
    ).strip().lower()
    return bool(provider == "mangadex" or source == "mangadex" or native_id.startswith("mangadex:"))


def native_manga_explicit_chapter_import_is_safe(path, target, number, trusted_issue=None):
    if not number or not is_manga_target(target):
        return False
    if trusted_issue in (None, ""):
        return False
    if str((target or {}).get("target_source") or "") != "inkdrop_series":
        return False
    if not target_allows_native_chapter_import(target):
        return False
    if target_looks_volume_based(target) or manga_source_has_explicit_volume_hint(path):
        return False
    if not filename_has_chapter_token(path):
        return False
    if format_issue_number(number) != format_issue_number(trusted_issue):
        return False
    return True


def classify_import_filename_safety(path, target=None, kind="comics", trusted_issue=None, comicinfo=None):
    if str(kind or "").lower() not in {"comics", "manga"} or not target:
        return {"ok": True, "score": 99, "evidence": ["not_comic_target"]}
    path = Path(path)
    leaf = path.name
    stem_words = clean_words(path.stem)
    parent_words = clean_words(path.parent.name)
    source_number = extract_issue_number(path)
    source_number_fmt = format_issue_number(source_number)
    explicit_unit = filename_has_explicit_unit_token(path)
    info = comicinfo if comicinfo is not None else read_comicinfo(path)
    trusted_missing_number_ok = trusted_issue_missing_source_number_is_safe(
        path,
        target,
        trusted_issue,
        comicinfo=info,
    )
    evidence = []
    score = 0

    def allow(reason="filename_confidence_ok"):
        return {"ok": True, "reason": reason, "score": score, "evidence": evidence, "filename": leaf}

    def reject(reason, detail):
        return {
            "ok": False,
            "reason": reason,
            "detail": detail,
            "score": score,
            "evidence": evidence,
            "filename": leaf,
        }

    source_identity_gate = inkdrop_artifact_acceptance.source_identity_acceptance(path, target)
    if not source_identity_gate.get("ok"):
        return reject(
            source_identity_gate.get("reason") or "source_identity_rejected",
            source_identity_gate.get("detail") or "Source filename/path identity contradicts the wanted comic issue.",
        )
    if filename_has_chapter_token(path, target=target) and not is_manga_target(target):
        return reject(
            "wrong_unit_type_chapter_for_comic_issue",
            "A chapter-marked artifact cannot satisfy a western comic issue.",
        )
    issue_mismatch = trusted_issue_mismatch_reason(path, trusted_issue, target=target, comicinfo=info)
    if issue_mismatch:
        return reject(issue_mismatch, "Trusted queue issue does not match the filename number.")
    if filename_has_range_or_pack(path):
        return reject(
            "pack_candidate_requires_pack_handling",
            "Filename looks like a pack or issue/volume range, so it should use pack review instead of single-item import.",
        )
    if filename_duplicate_copy_suffix(path):
        return reject(
            "duplicate_copy_suffix",
            "Filename ends with a duplicate-copy suffix and needs review before completing a wanted row.",
        )
    if filename_has_weak_numeric_prefix(path) and not explicit_unit:
        return reject(
            "weak_filename_unit_evidence",
            "Filename starts with a bare number before the title and does not provide an issue/chapter/volume token.",
        )

    target_title = str(target.get("title") or target.get("series") or "").lower()
    collection_target = any(
        marker in target_title
        for marker in (
            "omnibus",
            "library edition",
            "deluxe edition",
            "complete collection",
            "compendium",
            "trade paperback",
            " tpb",
        )
    )
    if collection_target and re.search(r"\b(?:part|pt|chapter|chap|ch|issue)\.?\s*0*\d+\b", path.stem, re.I):
        return reject(
            "single_part_file_does_not_satisfy_collection_target",
            "A single part/chapter-style file cannot auto-complete an omnibus or library-edition target.",
        )
    if filename_has_chapter_token(path, target=target) and target_looks_volume_based(target):
        return reject(
            "unit_model_mismatch",
            "Filename looks like a chapter, but the matched target is volume-based.",
        )
    related_subseries_reason = related_subseries_source_blocker(
        target.get("title") or target.get("series"),
        path,
        issue_title=target.get("issue_title"),
        issue_number=trusted_issue or target.get("issue_number") or target.get("normalized_number"),
        publisher=target.get("publisher"),
    )
    if related_subseries_reason:
        return reject(
            "wrong_series_or_subseries",
            related_subseries_reason,
        )

    title_alias = matching_target_alias(stem_words, target)
    parent_alias = matching_target_alias(parent_words, target)
    if title_alias:
        score += 2
        evidence.append(f"title:{title_alias}")
    if parent_alias:
        score += 1
        evidence.append(f"parent:{parent_alias}")
    if trusted_issue not in (None, ""):
        score += 2
        evidence.append(f"trusted_issue:{format_issue_number(trusted_issue)}")
        if trusted_missing_number_ok:
            score += 2
            evidence.append("trusted_single_issue_artifact_title")
    elif source_number_fmt and (explicit_unit or target_has_issue_number(target, source_number)):
        score += 2
        evidence.append(f"filename_unit:{source_number_fmt}")
    elif source_number_fmt and native_manga_bare_number_is_safe(path, target, title_alias, source_number, collection_target):
        score += 2
        evidence.append(f"native_manga_bare_number:{source_number_fmt}")
    if comicinfo_unit_matches(info, trusted_issue, source_number):
        score += 3
        evidence.append("comicinfo_unit")
    if filename_year_matches(path, target):
        score += 1
        evidence.append("year")
    if not source_number_fmt and not comicinfo_unit_matches(info, trusted_issue, source_number) and not trusted_missing_number_ok:
        return reject(
            "weak_filename_unit_evidence",
            "Filename does not contain a usable issue/chapter/volume number.",
        )
    if score < 4:
        return reject(
            "filename_confidence_too_low",
            "Filename did not provide enough title and unit evidence for zero-touch import.",
        )
    return allow()


def weak_filename_import_guard(path, target, kind, trusted_issue=None):
    return classify_import_filename_safety(path, target=target, kind=kind, trusted_issue=trusted_issue)


def artifact_acceptance_decision(path, target=None, event=None, archive_check=None, collection=None, source_unit=None):
    decision = inkdrop_artifact_acceptance.decide_acceptance(
        path,
        target=target,
        event=event,
        archive_check=archive_check,
        collection=collection,
        source_unit=source_unit,
    )
    marker = auto_inspect_task_context(path)
    if marker:
        decision = enforce_auto_inspect_artifact_gate(decision, marker)
    if event is not None:
        event["artifact_acceptance"] = inkdrop_artifact_acceptance.sanitized_decision(decision)
        if marker:
            event["auto_inspect"] = marker
            event["auto_inspect_artifact_proof"] = decision.get("auto_inspect_artifact_proof")
    return decision


def _auto_inspect_marker(payload):
    payload = payload if isinstance(payload, dict) else {}
    waiting = payload.get("manual_source_waiting")
    if isinstance(waiting, dict) and isinstance(waiting.get("auto_inspect"), dict):
        return _validated_slskd_waiting_auto_inspect_marker(waiting)
    if str(payload.get("candidate_source") or "") == "slskd_probe":
        return _validated_slskd_waiting_auto_inspect_marker(payload)
    marker = payload.get("auto_inspect")
    if not isinstance(marker, dict) and isinstance(payload.get("raw"), dict):
        raw = payload["raw"]
        waiting = raw.get("manual_source_waiting") if isinstance(raw, dict) else None
        if isinstance(waiting, dict) and isinstance(waiting.get("auto_inspect"), dict):
            return _validated_slskd_waiting_auto_inspect_marker(waiting)
        marker = raw.get("auto_inspect") if isinstance(raw, dict) else None
    if not isinstance(marker, dict):
        return {}
    digest = str(marker.get("candidate_identity_hash") or "").strip().lower()
    if marker.get("outcome") != "auto_inspect" or not marker.get("exact_artifact_proof_required"):
        return {}
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        return {}
    return {
        "contract_version": 1,
        "outcome": "auto_inspect",
        "candidate_identity_hash": digest,
        "exact_artifact_proof_required": True,
        "neutral_missing_evidence": sorted(
            str(value) for value in marker.get("neutral_missing_evidence") or []
            if str(value) in {"language_unknown", "size_below_preferred"}
        ),
    }


def _slskd_waiting_locator_digest(waiting):
    waiting = waiting if isinstance(waiting, dict) else {}
    username = str(waiting.get("username") or "").strip().casefold()
    filename = re.sub(r"/+", "/", str(waiting.get("filename") or "").replace("\\", "/")).strip("/").casefold()
    try:
        size = str(int(float(waiting.get("candidate_size") or 0)))
    except (TypeError, ValueError):
        return ""
    if not username or not filename or "/" not in filename or size == "0":
        return ""
    return hashlib.sha256("|".join(("slskd", username, filename, size)).encode("utf-8", errors="replace")).hexdigest()


def _auto_inspect_marker_present(payload):
    payload = payload if isinstance(payload, dict) else {}
    waiting = payload.get("manual_source_waiting")
    if isinstance(waiting, dict) and isinstance(waiting.get("auto_inspect"), dict):
        return True
    if str(payload.get("candidate_source") or "") == "slskd_probe" and isinstance(payload.get("auto_inspect"), dict):
        return True
    if isinstance(payload.get("auto_inspect"), dict):
        return True
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    nested_waiting = raw.get("manual_source_waiting") if isinstance(raw.get("manual_source_waiting"), dict) else {}
    return bool(isinstance(raw.get("auto_inspect"), dict) or isinstance(nested_waiting.get("auto_inspect"), dict))


def _authoritative_auto_inspect_marker_state(payload):
    """Return absent, valid, or invalid for authoritative SLSKD waiting evidence."""
    payload = payload if isinstance(payload, dict) else {}
    waiting_rows = []
    waiting = payload.get("manual_source_waiting")
    if isinstance(waiting, dict) and isinstance(waiting.get("auto_inspect"), dict):
        waiting_rows.append(waiting)
    if str(payload.get("candidate_source") or "") == "slskd_probe" and isinstance(payload.get("auto_inspect"), dict):
        waiting_rows.append(payload)
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    waiting = raw.get("manual_source_waiting")
    if isinstance(waiting, dict) and isinstance(waiting.get("auto_inspect"), dict):
        waiting_rows.append(waiting)
    if not waiting_rows:
        return "absent", {}
    markers = [_validated_slskd_waiting_auto_inspect_marker(waiting) for waiting in waiting_rows]
    if any(not marker for marker in markers):
        return "invalid", {}
    first_digest = markers[0]["candidate_identity_hash"]
    if any(not hmac.compare_digest(marker["candidate_identity_hash"], first_digest) for marker in markers[1:]):
        return "invalid", {}
    return "valid", markers[0]


def _validated_slskd_waiting_auto_inspect_marker(waiting):
    waiting = waiting if isinstance(waiting, dict) else {}
    if str(waiting.get("candidate_source") or "") != "slskd_probe":
        return {}
    marker = waiting.get("auto_inspect") if isinstance(waiting.get("auto_inspect"), dict) else {}
    digest = _slskd_waiting_locator_digest(waiting)
    persisted_digest = str(waiting.get("candidate_locator_digest") or "").strip().lower()
    marker_digest = str(marker.get("candidate_identity_hash") or "").strip().lower()
    if not digest or not hmac.compare_digest(digest, persisted_digest) or not hmac.compare_digest(digest, marker_digest):
        return {}
    transfer = waiting.get("slskd_transfer") if isinstance(waiting.get("slskd_transfer"), dict) else {}
    transfer_id = str(transfer.get("id") or "").strip()
    expected_transfer_id = str(waiting.get("slskd_transfer_id") or "").strip()
    transfer_user = str(transfer.get("username") or "").strip().casefold()
    transfer_path = re.sub(
        r"/+", "/", str(transfer.get("filename") or transfer.get("remoteFilename") or "").replace("\\", "/")
    ).strip("/").casefold()
    try:
        transfer_size = int(float(transfer.get("size") or 0))
        expected_size = int(float(waiting.get("candidate_size") or 0))
    except (TypeError, ValueError):
        return {}
    expected_user = str(waiting.get("username") or "").strip().casefold()
    expected_path = re.sub(r"/+", "/", str(waiting.get("filename") or "").replace("\\", "/")).strip("/").casefold()
    if (
        not transfer
        or not transfer_id
        or not expected_transfer_id
        or not hmac.compare_digest(transfer_id, expected_transfer_id)
        or transfer_user != expected_user
        or transfer_path != expected_path
        or transfer_size <= 0
        or transfer_size != expected_size
    ):
        return {}
    return _auto_inspect_marker({"auto_inspect": marker})


def auto_inspect_task_context(path):
    """Find a marker only through its exact per-candidate controlled staging path."""
    environment_context = os.environ.get("INKDROP_AUTO_INSPECT_CONTEXT_JSON")
    if environment_context:
        try:
            context = json.loads(environment_context)
        except (TypeError, ValueError):
            context = {}
        marker = _auto_inspect_marker(context)
        expected_path_hash = str((context or {}).get("source_path_hash") or "").strip().lower()
        actual_path_hash = hashlib.sha256(
            str(Path(path).resolve()).replace("\\", "/").casefold().encode("utf-8", errors="replace")
        ).hexdigest()
        if marker and expected_path_hash and hmac.compare_digest(expected_path_hash, actual_path_hash):
            return marker
    if not INKDROP_STATE_DB.exists():
        return {}
    candidate_path = str(Path(path)).replace("\\", "/").rstrip("/").lower()
    conn = sqlite_connect(INKDROP_STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select save_path, local_path, raw_json
            from download_tasks
            where lower(coalesce(category, ''))='inkdrop-auto-inspect'
            order by coalesce(updated_at, started_at, 0) desc
            limit 200
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, ValueError):
            continue
        marker = _auto_inspect_marker(raw)
        if not marker:
            continue
        save_path = str(row["save_path"] or "").replace("\\", "/").rstrip("/").lower()
        local_path = str(row["local_path"] or "").replace("\\", "/").rstrip("/").lower()
        controlled_leaf = f"/auto-inspect/{marker['candidate_identity_hash'][:20]}"
        if not save_path.endswith(controlled_leaf):
            continue
        if candidate_path == local_path or candidate_path == save_path or candidate_path.startswith(save_path + "/"):
            return marker
    return {}


def auto_inspect_exact_artifact_proof(decision):
    decision = decision if isinstance(decision, dict) else {}
    target_number = normalize_manga_number(decision.get("target_number"))
    artifact_number = normalize_manga_number(decision.get("interpreted_artifact_number"))
    target_type = str(decision.get("target_type") or "").strip().lower()
    artifact_type = str(decision.get("artifact_type") or "").strip().lower()
    proved = bool(
        decision.get("decision") == "accepted"
        and decision.get("completion_eligible")
        and decision.get("archive_validity") is True
        and decision.get("metadata_validity") == "authoritative"
        and decision.get("content_manifest_hash")
        and target_type in {"issue", "chapter", "volume"}
        and artifact_type == target_type
        and target_number
        and target_number == artifact_number
    )
    return {
        "proved": proved,
        "proof_contract_version": 1,
        "reason": "authoritative_exact_artifact_identity" if proved else "exact_artifact_identity_not_proved",
    }


def enforce_auto_inspect_artifact_gate(decision, marker):
    out = dict(decision or {})
    proof = auto_inspect_exact_artifact_proof(out)
    proof["candidate_identity_hash"] = str((marker or {}).get("candidate_identity_hash") or "").strip().lower()
    out["auto_inspect_artifact_proof"] = proof
    if proof["proved"]:
        return out
    out["decision"] = "manual_review_required"
    out["reason_codes"] = sorted(set([*(out.get("reason_codes") or []), "auto_inspect_exact_artifact_proof_required"]))
    out["completion_eligible"] = False
    out["quarantine_required"] = False
    out["retry_eligible"] = False
    return out


def auto_inspect_completion_allowed(item, result):
    item = item if isinstance(item, dict) else {}
    result = result if isinstance(result, dict) else {}
    item_state, item_authoritative_marker = _authoritative_auto_inspect_marker_state(item)
    result_state, result_authoritative_marker = _authoritative_auto_inspect_marker_state(result)
    if "invalid" in {item_state, result_state}:
        return False
    if item_state == "valid" and result_state == "valid" and not hmac.compare_digest(
        item_authoritative_marker["candidate_identity_hash"],
        result_authoritative_marker["candidate_identity_hash"],
    ):
        return False
    marker = item_authoritative_marker or result_authoritative_marker or _auto_inspect_marker(item) or _auto_inspect_marker(result)
    if not marker:
        return not (_auto_inspect_marker_present(item) or _auto_inspect_marker_present(result))
    expected_digest = marker["candidate_identity_hash"]
    proof_rows = []
    for payload in (item, result):
        proof = payload.get("auto_inspect_artifact_proof") if isinstance(payload, dict) else None
        if isinstance(proof, dict):
            proof_rows.append(proof)
        for key in ("imported", "skipped"):
            for row in (payload.get(key) if isinstance(payload, dict) else []) or []:
                if isinstance(row, dict) and isinstance(row.get("auto_inspect_artifact_proof"), dict):
                    proof_rows.append(row["auto_inspect_artifact_proof"])
        verification = payload.get("verification") if isinstance(payload, dict) else {}
        if isinstance(verification, dict):
            for key in ("checked", "failures"):
                for row in verification.get(key) or []:
                    if isinstance(row, dict) and isinstance(row.get("auto_inspect_artifact_proof"), dict):
                        proof_rows.append(row["auto_inspect_artifact_proof"])
    if len(proof_rows) > 200:
        return False
    proved_digests = [
        str(proof.get("candidate_identity_hash") or "").strip().lower()
        for proof in proof_rows
        if proof.get("proved") is True
    ]
    if any(not hmac.compare_digest(digest, expected_digest) for digest in proved_digests):
        return False
    return any(hmac.compare_digest(digest, expected_digest) for digest in proved_digests)


def artifact_acceptance_skip_event(event, decision):
    event.update(
        {
            "event": "skip_artifact_acceptance_gate",
            "skip_reason": decision.get("decision") or "artifact_acceptance_rejected",
            "artifact_acceptance": inkdrop_artifact_acceptance.sanitized_decision(decision),
            "action_needed": "manual_review" if decision.get("decision") == "manual_review_required" else "retry_another_source",
        }
    )
    return event


def artifact_bad_content_identity(file_sha256, decision):
    manifest = stable_artifact_manifest_hash((decision or {}).get("content_manifest_hash"))
    if manifest:
        return f"pages:{manifest}"
    if file_sha256:
        return f"sha256:{file_sha256}"
    return ""


def stable_artifact_manifest_hash(value):
    value = str(value or "").strip().lower()
    return value if re.fullmatch(r"[a-f0-9]{64}", value) else ""


def record_artifact_bad_content_memory(conn, file_sha256, source_path, decision):
    identity = artifact_bad_content_identity(file_sha256, decision)
    if not identity:
        return None
    ensure_artifact_bad_content_memory_schema(conn)
    sanitized = inkdrop_artifact_acceptance.sanitized_decision(decision)
    content_manifest = stable_artifact_manifest_hash((decision or {}).get("content_manifest_hash"))
    member_manifest = stable_artifact_manifest_hash((decision or {}).get("archive_member_manifest_hash"))
    now = time.time()
    raw_json = json.dumps(sanitized, sort_keys=True)
    conn.execute(
        """
        insert into artifact_bad_content_memory (
          identity, file_sha256, source_path, target_type, artifact_type, decision,
          reason_codes, content_manifest_hash, archive_member_manifest_hash,
          first_seen_at, last_seen_at, seen_count, raw_json
        ) values (?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(identity) do update set
          file_sha256=coalesce(excluded.file_sha256, artifact_bad_content_memory.file_sha256),
          source_path=excluded.source_path,
          target_type=excluded.target_type,
          artifact_type=excluded.artifact_type,
          decision=excluded.decision,
          reason_codes=excluded.reason_codes,
          content_manifest_hash=coalesce(excluded.content_manifest_hash, artifact_bad_content_memory.content_manifest_hash),
          archive_member_manifest_hash=coalesce(excluded.archive_member_manifest_hash, artifact_bad_content_memory.archive_member_manifest_hash),
          last_seen_at=excluded.last_seen_at,
          seen_count=artifact_bad_content_memory.seen_count + 1,
          raw_json=excluded.raw_json
        """,
        (
            identity,
            file_sha256,
            str(source_path) if source_path is not None else None,
            sanitized.get("target_type"),
            sanitized.get("artifact_type"),
            sanitized.get("decision") or "artifact_acceptance_rejected",
            json.dumps(sanitized.get("reason_codes") or []),
            content_manifest or None,
            member_manifest or None,
            now,
            now,
            1,
            raw_json,
        ),
    )
    conn.commit()
    return identity


def artifact_content_identity_evidence(path, file_sha256=None, decision=None):
    """Return stable, non-path artifact identities available for a local file."""
    path = Path(path)
    decision = decision if isinstance(decision, dict) else {}
    content_manifest = stable_artifact_manifest_hash(decision.get("content_manifest_hash"))
    member_manifest = stable_artifact_manifest_hash(decision.get("archive_member_manifest_hash"))
    if path.is_file() and (not content_manifest or not member_manifest):
        manifest = inkdrop_artifact_acceptance.page_manifest(path) or {}
        content_manifest = content_manifest or stable_artifact_manifest_hash(manifest.get("ordered_page_manifest_hash"))
        member_manifest = member_manifest or stable_artifact_manifest_hash(manifest.get("archive_member_manifest_hash"))
    digest = str(file_sha256 or "").strip().lower()
    if not digest and path.is_file():
        digest = sha256(path)
    return {
        "file_sha256": digest if re.fullmatch(r"[a-f0-9]{64}", digest) else "",
        "content_manifest_hash": str(content_manifest or "").strip().lower(),
        "archive_member_manifest_hash": str(member_manifest or "").strip().lower(),
    }


def find_artifact_bad_content_memory(conn, path, file_sha256=None, decision=None):
    ensure_artifact_bad_content_memory_schema(conn)
    evidence = artifact_content_identity_evidence(path, file_sha256=file_sha256, decision=decision)
    clauses = []
    params = []
    # Archive-member manifests describe names only. Keep them as corroborating
    # evidence, but never block content solely because filenames were reused.
    for column in ("file_sha256", "content_manifest_hash"):
        value = evidence.get(column)
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if not clauses:
        return None
    row = conn.execute(
        f"select identity, decision, reason_codes from artifact_bad_content_memory where {' or '.join(clauses)} order by last_seen_at desc limit 1",
        params,
    ).fetchone()
    if not row:
        return None
    return {
        "blocked": True,
        "reason": "known_bad_artifact_content",
        "decision": row[1] or "known_bad_artifact_content",
        "identity_kind": str(row[0] or "content").split(":", 1)[0],
    }


def record_known_bad_content_sha(conn, file_sha256, *, source_path=None, reason="incident_recovery"):
    digest = str(file_sha256 or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("expected_sha256_must_be_64_lowercase_hex")
    decision = {
        "decision": "known_bad_artifact_content",
        "reason_codes": [str(reason or "incident_recovery")[:96]],
        "completion_eligible": False,
        "quarantine_required": True,
        "retry_eligible": True,
    }
    return record_artifact_bad_content_memory(conn, digest, source_path, decision)


def known_bad_artifact_path(path, *, file_sha256=None, decision=None):
    path = Path(path)
    if not path.is_file() or not DB_PATH.exists():
        return None
    conn = sqlite_connect(DB_PATH)
    try:
        return find_artifact_bad_content_memory(conn, path, file_sha256=file_sha256, decision=decision)
    finally:
        conn.close()


def import_files(kind, dry_run=False, min_age_seconds=600, ignore_cutoff=False, matched_only=False, series_filter=None, all_series=False, pending_only=False, manual_inbox=False, suwayomi_staging=False, max_files=None, source_files=None, trusted_volume_id=None, trusted_issue=None, trusted_series_id=None, trusted_issue_title=None, trusted_issue_id=None, wait_for_kavita_scan=True, apply_planned_path=None, wait_for_library_scan=None, slskd_staging=False):
    if wait_for_library_scan is None:
        wait_for_library_scan = bool(wait_for_kavita_scan)
    path_settings = apply_path_provider_settings()
    media_management_settings = None
    if inkdrop_state is not None and INKDROP_STATE_DB.exists():
        try:
            media_management_settings = inkdrop_state.media_management_settings_context(INKDROP_STATE_DB)
        except Exception as exc:
            log({"event": "media_management_settings_load_failed", "error": f"{type(exc).__name__}: {exc}"})
    if apply_planned_path is not None:
        media_management_settings = dict(media_management_settings or {})
        media_management_settings["apply_planned_path"] = bool(apply_planned_path)
        media_management_settings["apply_planned_path_override"] = True
    conn = connect()
    sources, dest_dir = scan_sources(kind, manual_inbox, suwayomi_staging, slskd_staging)
    explicit_sources = bool(source_files)
    if source_files:
        sources = [Path(source) for source in source_files]
    if manual_inbox:
        for source in sources:
            if not dry_run and not source.is_file():
                source.mkdir(parents=True, exist_ok=True)
    if kind == "comics" and not series_filter and not all_series:
        event = {
            "event": "series_filter_required",
            "kind": kind,
            "dry_run": dry_run,
            "manual_inbox": manual_inbox,
        }
        log(event)
        print(json.dumps({"imported": [], "count": 0, "status": "series_filter_required"}, indent=2))
        return
    comic_targets = load_comic_targets(None if all_series else series_filter) if kind == "comics" else []
    trusted_target = (
        trusted_comic_target(comic_targets, trusted_volume_id, trusted_series_id)
        if kind == "comics" and explicit_sources
        else None
    )
    effective_trusted_issue_title = ""
    if trusted_target and trusted_series_id not in (None, "") and trusted_issue not in (None, ""):
        try:
            effective_trusted_issue_title = trusted_issue_title_evidence(
                trusted_series_id,
                trusted_issue,
                trusted_issue_id,
                trusted_issue_title,
            )
            if trusted_issue_title not in (None, ""):
                if not effective_trusted_issue_title:
                    log(
                        {
                            "event": "trusted_issue_title_rejected",
                            "trusted_series_id": trusted_series_id,
                            "trusted_issue": trusted_issue,
                            "trusted_issue_id": trusted_issue_id,
                            "reason": "exact_active_issue_identity_required",
                        }
                    )
        except Exception as exc:
            log(
                {
                    "event": "trusted_issue_title_lookup_failed",
                    "trusted_series_id": trusted_series_id,
                    "trusted_issue": trusted_issue,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    if trusted_target and effective_trusted_issue_title not in (None, ""):
        trusted_target = dict(trusted_target)
        trusted_target["issue_title"] = effective_trusted_issue_title
        if trusted_issue not in (None, ""):
            trusted_target.setdefault("issue_number", trusted_issue)
            trusted_target.setdefault("normalized_number", trusted_issue)
    incomplete_qbit_paths = load_qbit_incomplete_paths(kind)
    pending_imports = load_pending_imports(kind) if pending_only else []
    if pending_only and not explicit_sources and not manual_inbox and not suwayomi_staging and not slskd_staging:
        sources = pending_only_source_roots(sources, pending_imports)
    kapowarr_scan_volume_ids = set()
    kavita_scan_folders = set()
    kavita_force_library_scan_folders = set()
    cutoff = 0.0
    if not ignore_cutoff and CUTOFF_PATH.exists():
        try:
            cutoff = float(CUTOFF_PATH.read_text().strip())
        except ValueError:
            cutoff = 0.0
    imported = []
    skipped = []

    def attach_media_management_preview(event, target=None, source_path=None, dest_path=None):
        preview = media_management_import_preview(
            target,
            event,
            source_path=source_path,
            dest_path=dest_path or event.get("dest"),
            kind=kind,
            settings=media_management_settings,
        )
        if preview:
            event["media_management_preview"] = preview
        return event

    for root in sources:
        if not root.exists():
            continue
        scan_root = root.parent if root.is_file() else root
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if max_files and len(imported) >= max_files:
                break
            if not (explicit_sources and path == root) and is_internal_import_path(path, scan_root):
                log({"event": "skip_internal_import_path", "source": str(path), "root": str(scan_root), "kind": kind})
                continue
            if not path.is_file():
                continue
            path_kind = kind_from_path(path)
            comics_pdf_candidate = kind == "comics" and path.suffix.lower() == ".pdf"
            comics_zip_candidate = kind == "comics" and explicit_sources and path.suffix.lower() == ".zip"
            suwayomi_source = bool(suwayomi_staging) or is_suwayomi_import_source(path)
            if path_kind != kind and not comics_pdf_candidate and not comics_zip_candidate:
                continue
            if path.stat().st_mtime < cutoff:
                continue
            if str(path) in incomplete_qbit_paths:
                event = {
                    "event": "skip_incomplete_qbit_file",
                    "skip_reason": "source_file_incomplete_qbit_download",
                    "detail": "qBittorrent still reports this exact source file as incomplete; retry after the transfer finishes.",
                    "source": str(path),
                    "kind": kind,
                    "action_needed": "automatic_wait",
                }
                log(event)
                if explicit_sources:
                    skipped.append(event)
                continue
            if pending_only and not matches_pending_import(path, pending_imports):
                log({"event": "skip_not_pending_import", "source": str(path), "kind": kind})
                continue
            if not is_stable(path, min_age_seconds):
                continue
            artifact_memory_digest = None
            if kind == "comics":
                artifact_memory_digest = sha256(path)
                bad_memory = find_artifact_bad_content_memory(
                    conn, path, file_sha256=artifact_memory_digest
                )
                if bad_memory:
                    event = {
                        "event": "skip_known_bad_artifact_content",
                        "skip_reason": "known_bad_artifact_content",
                        "detail": "This file matches content previously rejected by InkDrop.",
                        "source": str(path),
                        "kind": kind,
                        "action_needed": "retry_another_source",
                    }
                    log(event)
                    skipped.append(event)
                    continue
            target = match_comic_target(path, comic_targets) if kind == "comics" else None
            trusted_target_match = False
            collection = None
            collection_error = None
            if kind == "comics" and manual_inbox:
                collection_candidate = collection_info_for_path(path, comic_targets)
                if collection_candidate and collection_candidate.get("target"):
                    collection = collection_candidate
                    target = collection["target"]
                elif collection_candidate and collection_candidate.get("error"):
                    collection_error = collection_candidate
            if kind == "comics" and trusted_target:
                issue_mismatch = trusted_issue_mismatch_reason(path, trusted_issue, target=trusted_target)
                if issue_mismatch:
                    event = {
                        "event": "skip_trusted_issue_mismatch",
                        "skip_reason": issue_mismatch,
                        "source": str(path),
                        "kind": kind,
                        "trusted_kapowarr_id": trusted_target.get("id"),
                        "trusted_series": trusted_target.get("title"),
                        "trusted_issue": trusted_issue,
                        "action_needed": "retry_another_source",
                    }
                    log(event)
                    skipped.append(event)
                    continue
                trusted_target_match = True
                target = trusted_target
            if kind == "comics" and not target and not matched_only:
                ambiguous_match = ambiguous_comic_target_match(path, comic_targets)
                if ambiguous_match:
                    event = {
                        "event": "skip_ambiguous_comic_target",
                        "skip_reason": "ambiguous_series_alias",
                        "source": str(path),
                        "kind": kind,
                        "ambiguous_alias": ambiguous_match.get("alias"),
                        "ambiguous_targets": ambiguous_match.get("targets") or [],
                        "action_needed": "manual_review_or_trusted_series_id",
                    }
                    log(event)
                    if manual_inbox and not dry_run:
                        append_manual_review(
                            "ambiguous_series_alias",
                            {
                                "source": str(path),
                                "kind": kind,
                                "ambiguous_alias": ambiguous_match.get("alias"),
                                "ambiguous_targets": ambiguous_match.get("targets") or [],
                                "note": "Multiple monitored InkDrop series share this title/alias. Use a trusted series target or resolve the duplicate series before zero-touch import.",
                            },
                        )
                    skipped.append(event)
                    continue
                log({"event": "skip_unmatched_comic", "source": str(path), "kind": kind})
                if manual_inbox and not dry_run:
                    reason = (collection_error or {}).get("error") or "manual_inbox_unmatched"
                    append_manual_review(
                        reason,
                        {
                            "source": str(path),
                            "kind": kind,
                            "series": (collection_error or {}).get("series"),
                            "collection_title": (collection_error or {}).get("collection_title"),
                            "detail": (collection_error or {}).get("detail"),
                            "note": "Manual inbox file did not match a safe import target. Monitor the series, add a known collected-edition range, or import manually.",
                        },
                    )
                continue
            if matched_only and not target:
                continue
            target_dir = comic_import_target_dir(target) if kind == "comics" and target else dest_dir
            unsafe_match_reason = unsafe_comic_target_match_reason(path, target) if kind == "comics" and target and not collection else None
            if unsafe_match_reason:
                event = {
                    "event": "skip_unsafe_collection_part_match",
                    "skip_reason": unsafe_match_reason,
                    "source": str(path),
                    "kind": kind,
                    "matched_series": target.get("title"),
                    "matched_series_folder": target.get("folder"),
                    "matched_kapowarr_id": target.get("id"),
                    "action_needed": "manual_review",
                }
                log(event)
                if not dry_run:
                    append_manual_review(
                        unsafe_match_reason,
                        {
                            "source": str(path),
                            "matched_series": target.get("title"),
                            "matched_kapowarr_id": target.get("id"),
                            "note": "A single part/chapter-style comic file matched a collection, omnibus, or library-edition target. Review or monitor the individual edition instead of auto-importing into the collection target.",
                        },
                    )
                skipped.append(event)
                continue
            supplemental_reason = supplemental_source_blocker(path) if kind == "comics" and target and not collection else None
            if supplemental_reason:
                event = {
                    "event": "skip_supplemental_source_file",
                    "skip_reason": "false_positive",
                    "detail": supplemental_reason,
                    "source": str(path),
                    "kind": kind,
                    "matched_series": target.get("title"),
                    "matched_series_folder": target.get("folder"),
                    "matched_kapowarr_id": target.get("id"),
                    "action_needed": "retry_another_source",
                }
                log(event)
                if pending_only:
                    append_pending_status(kind, path, "false_positive", None, target, pending_imports)
                skipped.append(event)
                continue
            related_subseries_reason = (
                related_subseries_source_blocker(
                    target.get("title"),
                    path,
                    issue_title=target.get("issue_title"),
                    issue_number=trusted_issue or target.get("issue_number") or target.get("normalized_number"),
                    publisher=target.get("publisher"),
                )
                if kind == "comics" and target and not collection
                else None
            )
            if related_subseries_reason:
                event = {
                    "event": "skip_related_subseries_match",
                    "skip_reason": "wrong_series_or_subseries",
                    "detail": related_subseries_reason,
                    "source": str(path),
                    "kind": kind,
                    "matched_series": target.get("title"),
                    "matched_series_folder": target.get("folder"),
                    "matched_kapowarr_id": target.get("id"),
                    "action_needed": "retry_another_source",
                }
                log(event)
                if pending_only:
                    append_pending_status(kind, path, "wrong_series_or_subseries", None, target, pending_imports)
                skipped.append(event)
                continue
            weak_filename_gate = (
                weak_filename_import_guard(path, target, kind, trusted_issue=trusted_issue)
                if kind == "comics" and target and not collection
                else {"ok": True}
            )
            if not weak_filename_gate.get("ok"):
                event = {
                    "event": "skip_weak_filename_import_guard",
                    "skip_reason": weak_filename_gate.get("reason") or "weak_filename_import_guard",
                    "detail": weak_filename_gate.get("detail"),
                    "source": str(path),
                    "kind": kind,
                    "matched_series": target.get("title"),
                    "matched_series_folder": target.get("folder"),
                    "matched_kapowarr_id": target.get("id"),
                    "filename": weak_filename_gate.get("filename"),
                    "action_needed": "manual_review",
                }
                log(event)
                if not dry_run:
                    append_manual_review(
                        "weak_filename_import_guard",
                        {
                            "source": str(path),
                            "matched_series": target.get("title") if target else None,
                            "matched_kapowarr_id": target.get("id") if target else None,
                            "reason": event["skip_reason"],
                            "detail": event.get("detail"),
                            "filename": event.get("filename"),
                        },
                    )
                if pending_only:
                    append_pending_status(kind, path, event["skip_reason"], None, target, pending_imports)
                skipped.append(event)
                continue
            language_gate = comicinfo_language_gate(path) if kind == "comics" and target else {"ok": True}
            if not language_gate.get("ok"):
                event = {
                    "event": "skip_non_english_comic_archive",
                    "skip_reason": "wrong_language_source",
                    "detail": language_gate.get("detail") or "wrong language source",
                    "language": language_gate.get("language"),
                    "comicinfo_path": language_gate.get("comicinfo_path"),
                    "source": str(path),
                    "kind": kind,
                    "matched_series": target.get("title"),
                    "matched_series_folder": target.get("folder"),
                    "matched_kapowarr_id": target.get("id"),
                    "action_needed": "retry_another_source",
                }
                log(event)
                if not dry_run:
                    append_manual_review(
                        "wrong_language_source",
                        {
                            "source": str(path),
                            "matched_series": target.get("title"),
                            "matched_kapowarr_id": target.get("id"),
                            "detail": event["detail"],
                            "language": event.get("language"),
                            "comicinfo_path": event.get("comicinfo_path"),
                            "note": "InkDrop blocked this archive before import because its ComicInfo language was not English.",
                        },
                    )
                if pending_only:
                    append_pending_status(kind, path, "wrong_language_source", None, target, pending_imports)
                skipped.append(event)
                continue
            if collection and collection_range_is_completed(collection, target):
                series_name = safe_filename_part(collection.get("series"))
                collection_title = safe_filename_part(collection.get("collection_title"))
                collection_year = str(collection.get("year") or "").strip()
                canonical_name = f"{series_name} - {collection_title}"
                if collection_year:
                    canonical_name += f" ({collection_year})"
                canonical_name += ".cbz"
                existing_dest = target_dir / canonical_name
                if not existing_dest.exists():
                    candidates = sorted(target_dir.glob(f"{series_name} - {collection_title}*.cbz"))
                    existing_dest = candidates[0] if candidates else existing_dest
                event = {
                    "event": "skip_collection_already_verified",
                    "kind": kind,
                    "source": str(path),
                    "dest": str(existing_dest),
                    "dry_run": dry_run,
                    "matched_series": target.get("title"),
                    "matched_series_folder": target.get("folder"),
                    "matched_kapowarr_id": target.get("id"),
                    "truth_model": COLLECTION_TRUTH_MODEL,
                    "collection_title": collection.get("collection_title"),
                    "collection_range": list(collection.get("range")),
                    "skip_reason": "already_visible_source_retained",
                    "action_needed": "none",
                }
                log(event)
                skipped.append(event)
                continue
            manga_guard = None
            exact_volume_identity = None
            if kind == "comics" and target and is_manga_unit_guard_target(target) and not collection:
                exact_volume_identity = exact_manga_volume_import_identity(path, target)
                manga_guard = manga_import_guard(
                    path,
                    target,
                    suwayomi_source,
                    auto_learn=not dry_run,
                    trusted_issue=trusted_issue,
                    exact_volume_identity=exact_volume_identity,
                )
                if not manga_guard.get("allowed", True):
                    reason = manga_guard.get("reason") or "manga_unit_import_blocked"
                    existing_path = manga_guard.get("existing_path")
                    event = {
                        "event": "skip_manga_unit_guard",
                        "skip_reason": reason,
                        "source": str(path),
                        "kind": kind,
                        "matched_series": target.get("title"),
                        "matched_kapowarr_id": target.get("id"),
                        "source_unit": manga_guard.get("source_unit"),
                        "manga_unit_model": manga_guard.get("series_unit_model"),
                        "manga_unit_policy": manga_guard.get("series_unit_policy"),
                        "manga_unit_policy_label": manga_guard.get("series_unit_policy_label"),
                        "manga_unit_allows_chapter": manga_guard.get("series_unit_allows_chapter"),
                        "manga_unit_allows_volume": manga_guard.get("series_unit_allows_volume"),
                        "normalized_number": manga_guard.get("normalized_number"),
                        "existing_path": existing_path,
                    }
                    log(event)
                    if manga_guard.get("completed"):
                        completed_event = {**event, "action_needed": "none"}
                        if existing_path:
                            completed_event["dest"] = existing_path
                        if not dry_run and existing_path:
                            existing_dest = Path(existing_path)
                            if str(existing_dest).startswith((str(COMIC_ROOT), str(MANGA_ROOT))):
                                scan_folder = str(existing_dest.parent)
                                touch_kavita_scan_folder(existing_dest.parent, source=path)
                                kavita_scan_folders.add(scan_folder)
                                if str(existing_dest).startswith(str(MANGA_ROOT)):
                                    kavita_force_library_scan_folders.add(scan_folder)
                            digest = sha256(path)
                            conn.execute(
                                "insert or replace into imported_files values (?,?,?,?,?)",
                                (digest, str(path), str(existing_path), path.stat().st_size, time.time()),
                            )
                            conn.commit()
                        if pending_only:
                            append_pending_status(kind, path, "imported", existing_path, target, pending_imports)
                        skipped.append(completed_event)
                        continue
                    if not dry_run:
                        append_manual_review(
                            reason,
                            {
                                "source": str(path),
                                "matched_series": target.get("title"),
                                "matched_kapowarr_id": target.get("id"),
                                "source_unit": manga_guard.get("source_unit"),
                                "manga_unit_model": manga_guard.get("series_unit_model"),
                                "manga_unit_policy": manga_guard.get("series_unit_policy"),
                                "manga_unit_policy_label": manga_guard.get("series_unit_policy_label"),
                                "manga_unit_allows_chapter": manga_guard.get("series_unit_allows_chapter"),
                                "manga_unit_allows_volume": manga_guard.get("series_unit_allows_volume"),
                                "normalized_number": manga_guard.get("normalized_number"),
                                "note": "Manga unit guard prevented duplicate or unsafe chapter/volume import.",
                            },
                        )
                    continue
                if exact_volume_identity:
                    manga_guard["source_unit"] = "volume"
                    manga_guard["normalized_number"] = exact_volume_identity["volume_number"]
            digest = f"dry-run:{path}" if dry_run else (artifact_memory_digest or sha256(path))
            existing = None if dry_run else conn.execute("select dest from imported_files where sha256=?", (digest,)).fetchone()
            if existing:
                existing_dest = Path(existing[0])
                if existing_dest.exists() and (not target or existing_dest.parent == target_dir):
                    event = {
                        "kind": kind,
                        "source": str(path),
                        "dest": str(existing_dest),
                        "size": path.stat().st_size,
                        "sha256": digest,
                        "dry_run": dry_run,
                        "existing": True,
                        "matched_series": target["title"] if target else None,
                        "matched_series_folder": target["folder"] if target else None,
                        "matched_kapowarr_id": target["id"] if target else None,
                    }
                    event.update(target_identity_fields(target))
                    if collection:
                        event["truth_model"] = COLLECTION_TRUTH_MODEL
                        event["collection"] = {
                            "series": collection.get("series"),
                            "collection_title": collection.get("collection_title"),
                            "range": list(collection.get("range")),
                        }
                        event["collection_title"] = collection.get("collection_title")
                        event["collection_range"] = list(collection.get("range"))
                    if kind == "comics" and target:
                        archive_check = validate_comic_archive(path)
                        event["archive_check"] = archive_check
                        decision = artifact_acceptance_decision(
                            path,
                            target=target,
                            event=event,
                            archive_check=archive_check,
                            collection=collection,
                        )
                        if not decision.get("completion_eligible"):
                            artifact_acceptance_skip_event(event, decision)
                            log(event)
                            if not dry_run:
                                event["bad_content_identity"] = record_artifact_bad_content_memory(
                                    conn, digest, path, decision
                                )
                                append_manual_review(
                                    "artifact_acceptance_gate",
                                    {
                                        "source": str(path),
                                        "dest": str(existing_dest),
                                        "matched_series": target.get("title"),
                                        "matched_kapowarr_id": target.get("id"),
                                        "artifact_acceptance": event.get("artifact_acceptance"),
                                        "bad_content_identity": event.get("bad_content_identity"),
                                        "note": "Existing imported-file evidence was not allowed to satisfy this target because the artifact acceptance gate rejected it.",
                                    },
                                )
                            skipped.append(event)
                            continue
                    log(event)
                    imported.append(event)
                    if pending_only:
                        append_pending_status(kind, path, "imported", existing_dest, target, pending_imports)
                    if kind == "comics" and target:
                        add_target_scan_requests(target, target_dir, kapowarr_scan_volume_ids, kavita_scan_folders, event)
                    continue
            if not dry_run:
                same_file = find_same_file(target_dir, path, digest)
                if same_file:
                    event = {
                        "kind": kind,
                        "source": str(path),
                        "dest": str(same_file),
                        "size": path.stat().st_size,
                        "sha256": digest,
                        "dry_run": dry_run,
                        "existing": True,
                        "matched_series": target["title"] if target else None,
                        "matched_series_folder": target["folder"] if target else None,
                        "matched_kapowarr_id": target["id"] if target else None,
                    }
                    event.update(target_identity_fields(target))
                    if collection:
                        event["truth_model"] = COLLECTION_TRUTH_MODEL
                        event["collection"] = {
                            "series": collection.get("series"),
                            "collection_title": collection.get("collection_title"),
                            "range": list(collection.get("range")),
                        }
                        event["collection_title"] = collection.get("collection_title")
                        event["collection_range"] = list(collection.get("range"))
                    if kind == "comics" and target:
                        archive_check = validate_comic_archive(path)
                        event["archive_check"] = archive_check
                        decision = artifact_acceptance_decision(
                            path,
                            target=target,
                            event=event,
                            archive_check=archive_check,
                            collection=collection,
                        )
                        if not decision.get("completion_eligible"):
                            artifact_acceptance_skip_event(event, decision)
                            log(event)
                            if not dry_run:
                                event["bad_content_identity"] = record_artifact_bad_content_memory(
                                    conn, digest, path, decision
                                )
                                append_manual_review(
                                    "artifact_acceptance_gate",
                                    {
                                        "source": str(path),
                                        "dest": str(same_file),
                                        "matched_series": target.get("title"),
                                        "matched_kapowarr_id": target.get("id"),
                                        "artifact_acceptance": event.get("artifact_acceptance"),
                                        "bad_content_identity": event.get("bad_content_identity"),
                                        "note": "Existing same-file evidence was not allowed to satisfy this target because the artifact acceptance gate rejected it.",
                                    },
                                )
                            skipped.append(event)
                            continue
                    conn.execute(
                        "insert or replace into imported_files values (?,?,?,?,?)",
                        (digest, str(path), str(same_file), path.stat().st_size, time.time()),
                    )
                    conn.commit()
                    log({**event, "event": "existing_same_file_recorded"})
                    imported.append(event)
                    if kind == "comics" and target:
                        add_target_scan_requests(target, target_dir, kapowarr_scan_volume_ids, kavita_scan_folders, event)
                    if pending_only:
                        append_pending_status(kind, path, "imported", same_file, target, pending_imports)
                    continue
            if kind == "comics" and target and collection:
                dest = collection_dest(target_dir, path, collection)
                canonical = {
                    "canonical_filename": dest.name,
                    "collection_title": collection.get("collection_title"),
                    "collection_range": list(collection.get("range")),
                }
            elif kind == "comics" and target:
                dest, canonical = canonical_comic_dest(
                    target_dir,
                    path,
                    target,
                    source_unit=(manga_guard or {}).get("source_unit"),
                )
                if suwayomi_source and is_manga_target(target):
                    unit_decision = suwayomi_unit_decision(path, target)
                    if unit_decision.get("source_unit") == "chapter" and unit_decision.get("allowed"):
                        dest = suwayomi_chapter_dest(target_dir, target, path, unit_decision["chapter_number"])
                        canonical = {
                            "canonical_filename": dest.name,
                            "canonical_issue_number": unit_decision["chapter_number"],
                            "manga_unit_model": "chapter",
                            "manga_unit_policy": unit_decision.get("series_unit_policy"),
                            "manga_unit_policy_label": unit_decision.get("series_unit_policy_label"),
                            "manga_unit_allows_chapter": unit_decision.get("series_unit_allows_chapter"),
                            "manga_unit_allows_volume": unit_decision.get("series_unit_allows_volume"),
                            "source_unit": "chapter",
                            "source_volume_number": unit_decision.get("source_volume_number"),
                        }
            else:
                dest, canonical = unique_dest(target_dir, path), None
            canonical_existing = existing_canonical_dest(target_dir, canonical, path) if kind == "comics" and target and not collection else None
            if not canonical_existing and kind == "comics" and target and not collection:
                canonical_existing = suffixless_existing_dest(dest)
            if canonical_existing and canonical_existing.resolve() != Path(path).resolve():
                event = {
                    "event": "skip_canonical_already_present",
                    "kind": kind,
                    "source": str(path),
                    "dest": str(canonical_existing),
                    "size": path.stat().st_size,
                    "sha256": digest,
                    "dry_run": dry_run,
                    "matched_series": target["title"] if target else None,
                    "matched_series_folder": target["folder"] if target else None,
                    "matched_kapowarr_id": target["id"] if target else None,
                    "skip_reason": "canonical_file_already_visible_or_present",
                    "action_needed": "none",
                }
                event.update(target_identity_fields(target))
                if canonical:
                    event.update(canonical)
                if exact_volume_identity:
                    event.update(
                        {
                            "source_unit": "volume",
                            "unit_type": "volume",
                            "source_volume_number": exact_volume_identity["volume_number"],
                            "volume_number": exact_volume_identity["volume_number"],
                            "normalized_number": exact_volume_identity["volume_number"],
                        }
                    )
                attach_media_management_preview(event, target, path, canonical_existing)
                if not dry_run:
                    conn.execute(
                        "insert or replace into imported_files values (?,?,?,?,?)",
                        (digest, str(path), str(canonical_existing), path.stat().st_size, time.time()),
                    )
                    conn.commit()
                    if kind == "comics" and target:
                        kavita_scan_folders.add(str(target_dir))
                        if manga_import_needs_library_scan(event):
                            kavita_force_library_scan_folders.add(str(target_dir))
                log(event)
                skipped.append(event)
                continue
            event = {
                "kind": kind,
                "source": str(path),
                "dest": str(dest),
                "size": path.stat().st_size,
                "sha256": digest,
                "dry_run": dry_run,
                "matched_series": target["title"] if target else None,
                "matched_series_folder": target["folder"] if target else None,
                "matched_kapowarr_id": target["id"] if target else None,
            }
            if trusted_issue not in (None, ""):
                event["trusted_issue"] = format_issue_number(trusted_issue) or str(trusted_issue)
            event.update(target_identity_fields(target))
            if collection:
                event["truth_model"] = COLLECTION_TRUTH_MODEL
                event["collection"] = {
                    "series": collection.get("series"),
                    "collection_title": collection.get("collection_title"),
                    "range": list(collection.get("range")),
                }
            if canonical:
                event.update(canonical)
            if manga_guard and target and is_manga_unit_guard_target(target):
                event["source_unit"] = manga_guard.get("source_unit")
                event["manga_unit_model"] = manga_guard.get("series_unit_model")
                event["manga_unit_policy"] = manga_guard.get("series_unit_policy")
                event["manga_unit_policy_label"] = manga_guard.get("series_unit_policy_label")
                event["manga_unit_allows_chapter"] = manga_guard.get("series_unit_allows_chapter")
                event["manga_unit_allows_volume"] = manga_guard.get("series_unit_allows_volume")
                event["normalized_number"] = manga_guard.get("normalized_number")
                if exact_volume_identity:
                    event["unit_type"] = "volume"
                    event["source_volume_number"] = exact_volume_identity["volume_number"]
                    event["volume_number"] = exact_volume_identity["volume_number"]
                    event["canonical_issue_number"] = exact_volume_identity["volume_number"]
                    event.pop("chapter_number", None)
                if manga_guard.get("auto_set_unit_model"):
                    event["auto_set_manga_unit_model"] = manga_guard.get("auto_set_unit_model")
            if kind == "comics" and target and not collection:
                identity_block = source_target_identity_blocker(path, target, event, canonical)
                if identity_block:
                    event.update(
                        {
                            "event": "skip_source_target_identity_mismatch",
                            "skip_reason": identity_block.get("reason") or "source_target_identity_mismatch",
                            "action_needed": "manual_identity_review",
                            "identity_guard": identity_block,
                        }
                    )
                    attach_media_management_preview(event, target, path, event.get("dest"))
                    log(event)
                    if not dry_run:
                        append_manual_review(
                            "source_target_identity_mismatch",
                            {
                                "source": str(path),
                                "matched_series": target.get("title"),
                                "matched_series_folder": target.get("folder"),
                                "matched_kapowarr_id": target.get("id"),
                                "native_series_id": target.get("native_series_id"),
                                "metadata_provider": target.get("metadata_provider"),
                                "metadata_id": target.get("metadata_id"),
                                "canonical_filename": event.get("canonical_filename"),
                                "identity_guard": identity_block,
                                "note": "Import blocked because source filename/path points at a different volume/year than the target issue.",
                            },
                        )
                    if pending_only:
                        append_pending_status(kind, path, "manual_identity_review", None, target, pending_imports)
                    skipped.append(event)
                    continue
            if kind == "comics":
                archive_check = validate_comic_archive(path)
                event["archive_check"] = archive_check
                if not archive_check.get("ok"):
                    event.update(
                        {
                            "event": "skip_bad_comic_archive",
                            "skip_reason": archive_check.get("reason") or "bad_archive",
                            "action_needed": "regrab_or_manual_review",
                        }
                    )
                    attach_media_management_preview(event, target, path, event.get("dest"))
                    log(event)
                    if not dry_run:
                        append_manual_review(
                            "comic_archive_regrab_needed",
                            {
                                "source": str(path),
                                "matched_series": target["title"] if target else None,
                                "matched_kapowarr_id": target["id"] if target else None,
                                "archive_check": archive_check,
                            },
                        )
                    if pending_only:
                        append_pending_status(kind, path, "bad_archive", None, target, pending_imports)
                    skipped.append(event)
                    continue
                decision = artifact_acceptance_decision(
                    path,
                    target=target,
                    event=event,
                    archive_check=archive_check,
                    collection=collection,
                )
                if not decision.get("completion_eligible"):
                    artifact_acceptance_skip_event(event, decision)
                    attach_media_management_preview(event, target, path, event.get("dest"))
                    log(event)
                    if not dry_run:
                        event["bad_content_identity"] = record_artifact_bad_content_memory(
                            conn, digest, path, decision
                        )
                        append_manual_review(
                            "artifact_acceptance_gate",
                            {
                                "source": str(path),
                                "matched_series": target["title"] if target else None,
                                "matched_kapowarr_id": target["id"] if target else None,
                                "artifact_acceptance": event.get("artifact_acceptance"),
                                "bad_content_identity": event.get("bad_content_identity"),
                                "note": "Import blocked before managed-library copy because target-aware artifact acceptance rejected this file.",
                            },
                        )
                    if pending_only:
                        append_pending_status(kind, path, decision.get("decision") or "artifact_acceptance_rejected", None, target, pending_imports)
                    skipped.append(event)
                    continue
                if collection:
                    event["normalized_archive"] = {"to": "cbz", "collection": True}
                elif target and is_manga_target(target):
                    event["truth_model"] = "kavita_manga"
                    event["normalized_archive"] = {"truth_model": "kavita_manga"}
                if suwayomi_source and event.get("truth_model") == "kavita_manga":
                    unit_decision = suwayomi_unit_decision(path, target)
                    event["manga_unit_model"] = unit_decision.get("series_unit_model")
                    event["source_unit"] = unit_decision.get("source_unit")
                    event["source_volume_number"] = unit_decision.get("source_volume_number")
                    if unit_decision.get("source_unit") == "chapter":
                        event["issue_number"] = unit_decision.get("chapter_number")
                        event["canonical_issue_number"] = unit_decision.get("chapter_number")
                        event["normalized_number"] = unit_decision.get("chapter_number")
                    if (
                        unit_decision.get("allowed")
                        and unit_decision.get("source_unit") == "chapter"
                        and (
                            manga_unit_completion_has_existing_target(
                                target["title"],
                                unit_decision.get("chapter_number"),
                                "chapter",
                                kapowarr_volume_id=target.get("id"),
                                native_series_id=completion_native_series_id(target),
                            )
                            or manga_coverage_has_existing_target(
                                target["title"],
                                unit_decision.get("chapter_number"),
                                "chapter",
                                kapowarr_volume_id=target.get("id"),
                                native_series_id=completion_native_series_id(target),
                            )
                        )
                    ):
                        event["event"] = "skip_manga_unit_already_completed"
                        event["skip_reason"] = "chapter_complete_via_kavita_unit_model"
                        log(event)
                        continue
                    if not unit_decision.get("allowed"):
                        event["event"] = "skip_suwayomi_unit_model_required"
                        event["skip_reason"] = unit_decision.get("reason")
                        log(event)
                        if not dry_run:
                            append_manual_review(
                                unit_decision.get("reason") or "suwayomi_unit_unknown_manual_review",
                                {
                                    "source": str(path),
                                    "matched_series": target["title"] if target else None,
                                    "matched_series_folder": target["folder"] if target else None,
                                    "matched_kapowarr_id": target["id"] if target else None,
                                    "canonical_filename": event.get("canonical_filename"),
                                    "source_unit": unit_decision.get("source_unit"),
                                    "manga_unit_model": unit_decision.get("series_unit_model"),
                                    "chapter_number": unit_decision.get("chapter_number"),
                                    "note": "Suwayomi chapter imports require chapter or mixed manga policy and must not overlap verified volume coverage.",
                                },
                            )
                        continue
                if not collection and path.suffix.lower() == ".cbr":
                    dest = unique_dest_name(dest.parent, dest.with_suffix(".cbz").name)
                    event["dest"] = str(dest)
                    event.setdefault("normalized_archive", {}).update({"from": "cbr", "to": "cbz"})
                elif not collection and comics_zip_candidate:
                    dest = unique_dest_name(dest.parent, dest.with_suffix(".cbz").name)
                    event["dest"] = str(dest)
                    event.setdefault("normalized_archive", {}).update({"from": "zip", "to": "cbz"})
                elif comics_pdf_candidate and target:
                    dest = unique_dest_name(dest.parent, dest.with_suffix(".cbz").name)
                    event["dest"] = str(dest)
                    event.setdefault("normalized_archive", {}).update({"from": "pdf", "to": "cbz"})
                    if is_manga_target(target):
                        event["truth_model"] = "kavita_manga"
                        event["normalized_archive"]["truth_model"] = "kavita_manga"
                post_normalize_existing = suffixless_existing_dest(dest) if target and not collection else None
                if post_normalize_existing and post_normalize_existing.resolve() != Path(path).resolve():
                    event.update(
                        {
                            "event": "skip_canonical_already_present",
                            "dest": str(post_normalize_existing),
                            "skip_reason": "canonical_file_already_visible_or_present",
                            "action_needed": "none",
                        }
                    )
                    attach_media_management_preview(event, target, path, post_normalize_existing)
                    if not dry_run:
                        conn.execute(
                            "insert or replace into imported_files values (?,?,?,?,?)",
                            (digest, str(path), str(post_normalize_existing), path.stat().st_size, time.time()),
                        )
                        conn.commit()
                        kavita_scan_folders.add(str(target_dir))
                        if manga_import_needs_library_scan(event):
                            kavita_force_library_scan_folders.add(str(target_dir))
                    log(event)
                    skipped.append(event)
                    continue
            selected_dest, media_preview, destination_decision = media_management_import_destination_decision(
                target,
                event,
                source_path=path,
                legacy_dest=dest,
                kind=kind,
                settings=media_management_settings,
            )
            if selected_dest:
                dest = selected_dest
                event["dest"] = str(dest)
            if destination_decision:
                event["media_management_destination_decision"] = destination_decision
            if media_preview:
                event["media_management_preview"] = media_preview
            else:
                attach_media_management_preview(event, target, path, dest)
            canonical_library_type = str(
                ((media_preview or {}).get("library_classification") or {}).get("library_type") or ""
            ).strip().lower()
            event["canonical_library_type"] = canonical_library_type
            canonical_manga_import = canonical_library_type == "manga"
            if (destination_decision or {}).get("blocked"):
                event.update({
                    "event": "import_blocked_canonical_identity",
                    "status": "failed_import",
                    "reason": destination_decision.get("reason"),
                    "retry_eligible": False,
                    "action_needed": "operator_review",
                })
                log(event)
                skipped.append(event)
                continue
            if (destination_decision or {}).get("skip_existing_destination"):
                event.update(
                    {
                        "event": "skip_media_management_existing_destination",
                        "skip_reason": "media_management_destination_exists",
                        "action_needed": "none",
                        "existing": True,
                    }
                )
                if not dry_run:
                    conn.execute(
                        "insert or replace into imported_files values (?,?,?,?,?)",
                        (digest, str(path), str(dest), path.stat().st_size, time.time()),
                    )
                    conn.commit()
                    if kind == "comics" and target:
                        add_target_scan_requests(target, dest.parent, kapowarr_scan_volume_ids, kavita_scan_folders, event)
                        if manga_import_needs_library_scan(event):
                            kavita_force_library_scan_folders.add(str(dest.parent))
                    if pending_only:
                        append_pending_status(kind, path, "imported", dest, target, pending_imports)
                log(event)
                skipped.append(event)
                continue
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if kind == "comics" and collection:
                    event["normalized_archive"].update(copy_collection_archive(path, dest, collection))
                elif kind == "comics" and path.suffix.lower() == ".cbr":
                    event["normalized_archive"].update(repack_cbr_to_cbz(path, dest))
                    if canonical_manga_import:
                        chapter_number = manga_archive_normalization_chapter_number(path, event, canonical)
                        if chapter_number:
                            event["normalized_archive"].update(
                                write_manga_chapter_comicinfo(dest, target, chapter_number)
                            )
                        else:
                            event["normalized_archive"].update(write_manga_comicinfo(dest, target, canonical))
                    elif target:
                        event["normalized_archive"].update(write_comic_comicinfo(dest, target, canonical))
                elif kind == "comics" and comics_pdf_candidate and target:
                    metadata_target = {**target, "media_type": "manga" if canonical_manga_import else "comic"}
                    event["normalized_archive"].update(convert_pdf_to_cbz(path, dest, metadata_target, canonical))
                else:
                    shutil.copy2(path, dest)
                    if kind == "comics" and canonical_manga_import:
                        chapter_number = manga_archive_normalization_chapter_number(path, event, canonical)
                        if chapter_number:
                            event["normalized_archive"].update(
                                write_manga_chapter_comicinfo(dest, target, chapter_number)
                            )
                        else:
                            event["normalized_archive"].update(write_manga_comicinfo(dest, target, canonical))
                    elif kind == "comics" and target and not collection:
                        event.setdefault("normalized_archive", {}).update(write_comic_comicinfo(dest, target, canonical))
                conn.execute(
                    "insert or replace into imported_files values (?,?,?,?,?)",
                    (digest, str(path), str(dest), path.stat().st_size, time.time()),
                )
                conn.commit()
                if kind == "comics" and target:
                    add_target_scan_requests(target, target_dir, kapowarr_scan_volume_ids, kavita_scan_folders, event)
                    if manga_import_needs_library_scan(event):
                        kavita_force_library_scan_folders.add(str(target_dir))
                if pending_only:
                    append_pending_status(kind, path, "imported", dest, target, pending_imports)
            log(event)
            imported.append(event)
            if collection and manual_inbox and not dry_run:
                append_manual_review(
                    "manual_inbox_imported_as_collection",
                    {
                        "source": str(path),
                        "dest": str(dest),
                        "series": collection.get("series"),
                        "collection_title": collection.get("collection_title"),
                        "range": list(collection.get("range")),
                        "truth_model": COLLECTION_TRUTH_MODEL,
                    },
                )
        if max_files and len(imported) >= max_files:
            break
    missing_before = kapowarr_missing_counts(kapowarr_scan_volume_ids) if kind == "comics" else {}
    kapowarr_scan_tasks = []
    if not dry_run and kind == "comics":
        for volume_id in sorted(kapowarr_scan_volume_ids):
            try:
                queued = queue_kapowarr_scan(volume_id)
                kapowarr_scan_tasks.append(queued)
                event = "kapowarr_refresh_scan_skipped" if queued.get("skipped") else "kapowarr_refresh_scan_queued"
                log({"event": event, **queued})
            except Exception as exc:
                kapowarr_scan_tasks.append({"volume_id": volume_id, "error": str(exc)})
                log({"event": "kapowarr_refresh_scan_failed", "volume_id": volume_id, "error": str(exc)})
    frontend_sync = (
        sync_library_frontend_folders(
            kavita_scan_folders,
            force_library_scan_folders=kavita_force_library_scan_folders,
        )
        if not dry_run and kind == "comics"
        else {"kavita": [], "komga": [], "library_scan_tasks": {"kavita": [], "komga": []}}
    )
    kavita_scan_tasks = frontend_sync.get("kavita") or []
    komga_scan_tasks = frontend_sync.get("komga") or []
    library_scan_tasks = frontend_sync.get("library_scan_tasks") or {
        "kavita": kavita_scan_tasks,
        "komga": komga_scan_tasks,
    }
    if wait_for_library_scan and not dry_run and (kapowarr_scan_tasks or kavita_scan_tasks or komga_scan_tasks):
        time.sleep(20)
    verification_timeout = SOURCE_FILE_SCAN_TIMEOUT_SECONDS if explicit_sources else MANGA_SCAN_TIMEOUT_SECONDS
    verification = (
        verify_imported_items(imported, poll_library_visibility=bool(wait_for_library_scan), timeout=verification_timeout)
        if not dry_run and kind == "comics" and imported
        else {}
    )
    missing_after = kapowarr_missing_counts(kapowarr_scan_volume_ids) if not dry_run and kind == "comics" else {}
    if verification.get("failure_count"):
        append_manual_review(
            "import_verification_failed",
            {
                "failures": verification.get("failures", []),
                "kapowarr_scan_tasks": kapowarr_scan_tasks,
                "kavita_scan_tasks": kavita_scan_tasks,
                "komga_scan_tasks": komga_scan_tasks,
            },
        )
    skipped_existing = [
        row for row in skipped
        if isinstance(row, dict)
        and (
            row.get("event") == "skip_canonical_already_present"
            or row.get("skip_reason") == "canonical_file_already_visible_or_present"
            or (
                row.get("event") == "skip_manga_unit_guard"
                and row.get("skip_reason") == "already_verified_duplicate"
                and row.get("existing_path")
            )
        )
    ]
    skipped_bad_archives = [
        row for row in skipped
        if isinstance(row, dict)
        and (
            row.get("event") == "skip_bad_comic_archive"
            or row.get("skip_reason") == "bad_archive"
            or (
                isinstance(row.get("archive_check"), dict)
                and row.get("archive_check", {}).get("ok") is False
            )
        )
    ]
    if not dry_run and (imported or kapowarr_scan_tasks or kavita_scan_tasks or komga_scan_tasks or skipped_existing or skipped_bad_archives):
        write_import_status(
            {
                "kind": kind,
                "imported_count": len(imported),
                "imported": imported[-20:],
                "skipped_count": len(skipped),
                "skipped": skipped[-20:],
                "bad_archive_count": len(skipped_bad_archives),
                "bad_archives": skipped_bad_archives[-20:],
                "skipped_bad_archives": skipped_bad_archives[-20:],
                "kapowarr_scan_tasks": kapowarr_scan_tasks,
                "kavita_scan_tasks": kavita_scan_tasks,
                "komga_scan_tasks": komga_scan_tasks,
                "library_scan_tasks": library_scan_tasks,
                "kavita_force_library_scan_folders": sorted(kavita_force_library_scan_folders),
                "verification": verification,
                "missing_before": missing_before,
                "missing_after": missing_after,
                "pending_only": pending_only,
                "all_series": all_series,
                "series_filter": series_filter or [],
                "manual_inbox": manual_inbox,
                "sources": [str(source) for source in sources],
                "path_settings": {
                    "comic_root": str(path_settings.get("comic_root")),
                    "manga_root": str(path_settings.get("manga_root")),
                    "kavita_comic_root": path_settings.get("kavita_comic_root"),
                    "kavita_manga_root": path_settings.get("kavita_manga_root"),
                    "manual_comics_inbox": str(path_settings.get("manual_comics_inbox")),
                    "manual_ebooks_inbox": str(path_settings.get("manual_ebooks_inbox")),
                    "library_source": path_settings.get("library_source"),
                    "manual_inbox_source": path_settings.get("manual_inbox_source"),
                },
            }
        )
    print(json.dumps({
        "imported": imported,
        "count": len(imported),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "kapowarr_scan_tasks": kapowarr_scan_tasks,
        "kavita_scan_tasks": kavita_scan_tasks,
        "komga_scan_tasks": komga_scan_tasks,
        "library_scan_tasks": library_scan_tasks,
        "kavita_force_library_scan_folders": sorted(kavita_force_library_scan_folders),
        "verification": verification,
        "manual_inbox": manual_inbox,
        "sources": [str(source) for source in sources],
        "path_settings": {
            "comic_root": str(path_settings.get("comic_root")),
            "manga_root": str(path_settings.get("manga_root")),
            "kavita_comic_root": path_settings.get("kavita_comic_root"),
            "kavita_manga_root": path_settings.get("kavita_manga_root"),
            "manual_comics_inbox": str(path_settings.get("manual_comics_inbox")),
            "manual_ebooks_inbox": str(path_settings.get("manual_ebooks_inbox")),
            "library_source": path_settings.get("library_source"),
            "manual_inbox_source": path_settings.get("manual_inbox_source"),
        },
    }, indent=2))


def verify_last_status():
    if not IMPORT_STATUS_PATH.exists():
        print(json.dumps({"status": "missing_import_status"}, indent=2))
        return
    status = json.loads(IMPORT_STATUS_PATH.read_text(encoding="utf-8"))
    imported = status.get("imported") or []
    kapowarr_adapter_enabled = completed_import_kapowarr_adapter_enabled()
    volume_ids = (
        {
            int(item["matched_kapowarr_id"])
            for item in imported
            if item.get("matched_kapowarr_id")
        }
        if kapowarr_adapter_enabled
        else set()
    )
    folders = {
        str(Path(item["dest"]).parent)
        for item in imported
        if item.get("dest")
        and (
            str(item.get("dest")).startswith(str(COMIC_ROOT))
            or str(item.get("dest")).startswith(str(MANGA_ROOT))
        )
    }
    force_library_scan_folders = {
        str(Path(item["dest"]).parent)
        for item in imported
        if item.get("dest") and manga_import_needs_library_scan(item)
    }
    missing_before = kapowarr_missing_counts(volume_ids)
    kapowarr_scan_tasks = []
    for volume_id in sorted(volume_ids):
        try:
            queued = queue_kapowarr_scan(volume_id)
            kapowarr_scan_tasks.append(queued)
            event = "verify_kapowarr_refresh_scan_skipped" if queued.get("skipped") else "verify_kapowarr_refresh_scan_queued"
            log({"event": event, **queued})
        except Exception as exc:
            kapowarr_scan_tasks.append({"volume_id": volume_id, "error": str(exc)})
            log({"event": "verify_kapowarr_refresh_scan_failed", "volume_id": volume_id, "error": str(exc)})
    frontend_sync = sync_library_frontend_folders(
        folders,
        force_library_scan_folders=force_library_scan_folders,
        event_prefix="verify_",
    )
    kavita_scan_tasks = frontend_sync.get("kavita") or []
    komga_scan_tasks = frontend_sync.get("komga") or []
    library_scan_tasks = frontend_sync.get("library_scan_tasks") or {
        "kavita": kavita_scan_tasks,
        "komga": komga_scan_tasks,
    }
    if kapowarr_scan_tasks or kavita_scan_tasks or komga_scan_tasks:
        time.sleep(20)
    verification = verify_imported_items(imported, poll_library_visibility=True)
    missing_after = kapowarr_missing_counts(volume_ids)
    if verification.get("failure_count"):
        append_manual_review(
            "import_verification_failed",
            {
                "failures": verification.get("failures", []),
                "kapowarr_scan_tasks": kapowarr_scan_tasks,
                "kavita_scan_tasks": kavita_scan_tasks,
                "komga_scan_tasks": komga_scan_tasks,
            },
        )
    status.update(
        {
            "kapowarr_scan_tasks": kapowarr_scan_tasks,
            "kavita_scan_tasks": kavita_scan_tasks,
            "komga_scan_tasks": komga_scan_tasks,
            "library_scan_tasks": library_scan_tasks,
            "kavita_force_library_scan_folders": sorted(force_library_scan_folders),
            "verification": verification,
            "missing_before": missing_before,
            "missing_after": missing_after,
            "verify_only": True,
        }
    )
    write_import_status(status)
    print(json.dumps(status, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description="Copy completed qB/SAB reading files into InkDrop managed reading libraries")
    parser.add_argument("--kind", choices=["comics", "ebooks"], default="comics")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-age-seconds", type=int, default=600)
    parser.add_argument("--ignore-cutoff", action="store_true")
    parser.add_argument("--matched-only", action="store_true")
    parser.add_argument("--all-series", action="store_true", help="Allow comic imports for every Kapowarr series; default requires --series")
    parser.add_argument("--pending-only", action="store_true", help="Only import files matching InkDrop pending-import records")
    parser.add_argument("--manual-inbox", action="store_true", help="Only import from the deliberate manual inbox folders")
    parser.add_argument("--suwayomi-staging", action="store_true", help="Only import from the isolated Suwayomi manga staging folder")
    parser.add_argument("--slskd-staging", action="store_true", help="Only import from the isolated SLSKD download staging folder")
    parser.add_argument("--source-file", action="append", help="Import one exact source file selected by reconciliation; repeatable")
    parser.add_argument("--trusted-volume-id", help="For an explicit source file, trust this Kapowarr volume id as the import target")
    parser.add_argument("--trusted-series-id", help="For an explicit source file, trust this InkDrop/native series id as the import target")
    parser.add_argument("--trusted-issue", help="For an explicit source file, require this watched issue number when present")
    parser.add_argument("--trusted-issue-title", help="For an explicit source file, carry the native watched issue title as import evidence")
    parser.add_argument("--trusted-issue-id", help="For an explicit source file, bind trusted issue metadata to this exact InkDrop issue row")
    parser.add_argument("--approve-reader-binding-work-id", help="Operator-approved InkDrop/native work id to bind to one Kavita series")
    parser.add_argument("--approve-reader-series-id", help="Kavita series id for --approve-reader-binding-work-id")
    parser.add_argument("--approve-reader-library-id", help="Kavita library id for --approve-reader-binding-work-id")
    parser.add_argument("--apply-planned-path", action="store_true", help="Use the Media Management planned folder/file path for this import command only after safety checks pass")
    parser.add_argument(
        "--no-wait-for-library-scan",
        "--no-wait-for-kavita-scan",
        dest="no_wait_for_library_scan",
        action="store_true",
        help="Copy/import and queue frontend scans, but return while optional library visibility is still pending",
    )
    parser.add_argument("--series", action="append", help="Only import files matching this Kapowarr series title; repeatable")
    parser.add_argument("--verify-last-status", action="store_true", help="Rescan and verify the latest import-status without copying new files")
    parser.add_argument("--verify-pack-imports", action="store_true", help="Recheck pack-imported files and sync verified rows into InkDrop state")
    parser.add_argument("--audit-pack-duplicates", action="store_true", help="Report verified pack imports that left same-hash duplicate files outside the verified destination")
    parser.add_argument("--quarantine-pack-duplicates", action="store_true", help="Move eligible same-hash pack duplicate files to the pack duplicate quarantine folder")
    parser.add_argument("--backfill-completion-identities", action="store_true", help="Fill native/provider identity columns on existing completion records")
    parser.add_argument("--audit-stale-manga-completion", action="store_true", help="Report verified manga completion rows whose target file is missing")
    parser.add_argument("--audit-limit", type=int, default=50, help="Limit stale completion audit samples")
    parser.add_argument("--no-audit-queue-links", action="store_true", help="Skip InkDrop queue linkage during stale completion audit")
    parser.add_argument("--max-files", type=int, help="Stop after importing this many files")
    args = parser.parse_args()
    reader_approval = (
        args.approve_reader_binding_work_id,
        args.approve_reader_series_id,
        args.approve_reader_library_id,
    )
    if any(reader_approval):
        if not all(reader_approval):
            parser.error("reader binding approval requires work id, Kavita series id, and Kavita library id")
        print(json.dumps(approve_reader_binding(*reader_approval), indent=2, sort_keys=True))
        return
    if args.backfill_completion_identities:
        print(json.dumps(backfill_completion_identity_fields(dry_run=args.dry_run), indent=2, sort_keys=True))
        return
    if args.audit_stale_manga_completion:
        print(json.dumps(
            audit_stale_manga_completion(limit=args.audit_limit, include_queue=not args.no_audit_queue_links),
            indent=2,
            sort_keys=True,
        ))
        return
    if args.verify_pack_imports:
        print(json.dumps(reverify_inkdrop_pack_imports(max_rows=args.max_files or 300), indent=2, sort_keys=True))
        return
    if args.audit_pack_duplicates or args.quarantine_pack_duplicates:
        print(json.dumps(
            audit_pack_duplicate_imports(
                limit=args.audit_limit,
                quarantine=bool(args.quarantine_pack_duplicates),
                dry_run=bool(args.dry_run),
            ),
            indent=2,
            sort_keys=True,
        ))
        return
    if args.verify_last_status:
        verify_last_status()
        return
    yield_reason = broad_pending_import_should_yield(args)
    if yield_reason:
        status = {
            "status": "manual_source_priority",
            "count": 0,
            "imported": [],
            "reason": "SLSKD/manual-source import is waiting; broad pending import yielded the lock.",
            **yield_reason,
        }
        print(json.dumps(status, indent=2, sort_keys=True))
        return
    import_files(
        args.kind,
        args.dry_run,
        args.min_age_seconds,
        args.ignore_cutoff,
        args.matched_only,
        args.series,
        args.all_series,
        args.pending_only,
        args.manual_inbox,
        args.suwayomi_staging,
        args.max_files,
        args.source_file,
        args.trusted_volume_id,
        args.trusted_issue,
        args.trusted_series_id,
        args.trusted_issue_title,
        args.trusted_issue_id,
        wait_for_library_scan=not args.no_wait_for_library_scan,
        apply_planned_path=True if args.apply_planned_path else None,
        slskd_staging=args.slskd_staging,
    )


if __name__ == "__main__":
    main()
