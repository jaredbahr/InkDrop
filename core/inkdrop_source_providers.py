"""Source provider candidate and direct-download safety helpers."""

from __future__ import annotations

import hashlib
import html.parser
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

from core import inkdrop_bencode
from core import inkdrop_candidate_matching
from core import inkdrop_sources


DIRECT_DOWNLOAD_CLIENT = "inkdrop_direct"
PAGE_PACK_DOWNLOAD_CLIENT = "inkdrop_page_pack"
EXTERNAL_TOOL_DOWNLOAD_CLIENT = "inkdrop_external_tool"
DEFAULT_MIN_SIZE_BYTES = 1024
DEFAULT_MAX_SIZE_BYTES = 2 * 1024 * 1024 * 1024

HTML_CONTENT_TYPES = {
    "application/xhtml+xml",
    "text/html",
    "text/plain+html",
}

CONTENT_TYPES_BY_EXTENSION = {
    ".cbz": {"application/vnd.comicbook+zip", "application/zip", "application/octet-stream"},
    ".cbr": {"application/vnd.comicbook-rar", "application/x-rar-compressed", "application/octet-stream"},
    ".epub": {"application/epub+zip", "application/octet-stream", "binary/octet-stream"},
    ".pdf": {"application/pdf", "application/octet-stream", "binary/octet-stream"},
    ".txt": {"text/plain", "application/octet-stream"},
    ".zip": {"application/zip", "application/octet-stream"},
}

PAGE_IMAGE_CONTENT_TYPES_BY_EXTENSION = {
    ".jpg": {"image/jpeg", "application/octet-stream"},
    ".jpeg": {"image/jpeg", "application/octet-stream"},
    ".png": {"image/png", "application/octet-stream"},
    ".webp": {"image/webp", "application/octet-stream"},
}

PROVIDER_ALLOWED_RIGHTS = {
    "public_domain",
    "open_license",
    "creative_commons",
}

GUTENDEX_FORMAT_PREFERENCE = (
    ".epub",
    ".pdf",
    ".txt",
    ".html",
)

INTERNET_ARCHIVE_DOWNLOAD_BASE = "https://archive.org/download"
INTERNET_ARCHIVE_DETAILS_BASE = "https://archive.org/details"
INTERNET_ARCHIVE_FILE_FORMAT_SKIP_TOKENS = {
    "metadata",
    "scandata",
    "thumbnail",
    "item tile",
    "djvu xml",
    "abbyy gz",
    "hocr",
}
INTERNET_ARCHIVE_FILE_NAME_SKIP_RE = re.compile(
    r"(?i)(__ia_thumb|_files\.xml$|_meta\.(sqlite|xml)$|_reviews\.xml$|_itemimage\.)"
)
INTERNET_ARCHIVE_ALLOWED_MEDIATYPES = {"texts"}
INTERNET_ARCHIVE_DEFAULT_EXTENSIONS = (".cbz", ".cbr", ".zip", ".pdf", ".epub", ".txt")
GENERIC_OPDS_EXTENSIONS = (".epub", ".pdf", ".cbz", ".cbr", ".zip")
OPDS_ACQUISITION_SKIP_REL_TOKENS = {"borrow", "sample", "preview", "stream"}

EXTERNAL_TOOL_OUTPUT_MAX_CHARS = 4000
EXTERNAL_TOOL_SENSITIVE_KEY_RE = re.compile(r"(?i)(password|passwd|token|secret|cookie|authorization|api[_-]?key)")
EXTERNAL_TOOL_DIRECT_URL_KEY_RE = re.compile(r"(?i)(download|direct).*(url|link)|(?:url|link).*(download|direct)")
EXTERNAL_TOOL_SAFE_URL_KEYS = {
    "url",
    "link",
    "source_url",
    "sourceurl",
    "page_url",
    "pageurl",
    "series_url",
    "seriesurl",
    "comic_url",
    "comicurl",
    "manga_url",
    "mangaurl",
}
URL_RE = re.compile(r"https?://[^\s\"'<>]+")

EXTERNAL_TOOL_CANDIDATE_OUTPUT_SCHEMA = {
    "contract": "external_tool_candidates_from_results",
    "version": 1,
    "shape": "JSON object with results array, JSON array, or newline-delimited JSON objects",
    "required_result_fields": ["title"],
    "locator_fields": ["url", "source_url", "page_url", "output_path", "local_path", "file", "filename", "id", "guid"],
    "staged_output_fields": ["output_path", "local_path", "path", "file", "filename"],
    "metadata_fields": ["series", "language", "translated_language", "extension", "size_bytes", "score", "site", "source_site", "tool_name", "tool_version"],
    "automation_rules": [
        "Do not include secrets, cookies, authorization headers, or account tokens in output.",
        "Direct download URLs are redacted by the sanitizer unless a source-specific policy stages output first.",
        "Automatic import handoff requires auto_stage_tool_output plus output_path under staged_output_root.",
        "InkDrop still applies strict metadata, language, extension, and staging-root checks before recording a handoff.",
    ],
}

INDEXER_PROTOCOLS = {"torrent", "usenet"}
INDEXER_DOWNLOAD_CLIENT_BY_PROTOCOL = {
    "torrent": "qbittorrent",
    "usenet": "sabnzbd",
}

SUPPORTED_INDEXER_CLIENTS = {
    "qbittorrent", "transmission", "deluge", "utorrent", "rtorrent",
    "sabnzbd", "nzbget",
}
SUPPORTED_AUTO_INSPECT_CLIENTS = {"qbittorrent"}
AUTO_INSPECT_NEUTRAL_REVIEW_REASONS = {
    "language_unknown",
}
AUTO_INSPECT_EXACT_UNIT_EVIDENCE = {
    "exact_issue_number", "exact_chapter_number", "exact_volume_number",
    "exact_pack_manifest_member",
}
PACK_CONTENTS_SAFE_COVERAGE_SOURCES = {
    "pack_contents_filename",
    "pack_contents_volume_filename",
}
PACK_TITLE_RE = re.compile(
    r"(?i)("
    r"\bcomplete\b|\bpack\b|\bbatch\b|"
    r"\bweekly[\W_]+(?:comics?|releases?)\b|"
    r"\b(?:dc|marvel|image|dark[\W_]+horse|idw|boom)(?:[\W_]+comics?)?[\W_]+week\b|"
    r"\b(?:dc|marvel|image|dark[\W_]+horse|idw|boom)(?:[\W_]+comics?)?[\W_]+weekly[\W_]+releases\b|"
    r"v(?:ol(?:ume)?)?\.?\s*\d+\s*[-+]\s*\d+|"
    r"\b\d{1,4}\s*[-+]\s*\d{1,4}\b"
    r")"
)
COMIC_FILE_EXTENSION_RE = re.compile(r"(?i)\.(?:cbz|cbr|pdf|epub|zip|rar|7z)\b")
COLLECTION_TARGET_RE = re.compile(
    r"(?i)\b(?:omnibus|library\s+edition|complete\s+collection|collection|treasury|vol\.?\s*0*\d+|v0*\d+)\b"
)
SINGLE_PART_SOURCE_RE = re.compile(r"(?i)\b(?:part|pt|chapter|chap|ch|issue)\s*0*\d+\b")
COLLECTION_ISSUE_TITLE_HINTS = {"tpb", "trade paperback", "hc", "hardcover"}
INDEXER_WRAPPER_EXTENSIONS = {".nzb", ".torrent"}
INDEXER_ARTIFACT_EXTENSIONS = set(CONTENT_TYPES_BY_EXTENSION) | {".rar", ".7z", ".mobi", ".azw3"}
DIRECT_PREVIEW_ONLY_RE = re.compile(r"(?i)(\bpreview\b|\bsample\b|\bexcerpt\b|preview[-_\s]*only|sample[-_\s]*(?:chapter|issue|pages?))")
DIRECT_IMAGE_ARTIFACT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

MANGA_PUBLISHER_HINT_RE = re.compile(
    r"(?i)\b("
    r"kodansha|viz|shonen\s+jump|shueisha|yen\s+press|seven\s+seas|"
    r"dark\s+horse\s+manga|vertical|square\s+enix\s+manga|tokyopop|"
    r"denpa|j-novel|manga"
    r")\b"
)
MANGA_VOLUME_ISSUE_TITLE_RE = re.compile(
    r"(?i)\b(?:book|volume|vol\.?|omnibus)\s*(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b"
)
MANGA_SINGLE_VOLUME_ARTIFACT_ISSUE_TITLE_RE = re.compile(
    r"(?i)\b(?:tpb|trade(?:\s+paperback)?|one[-\s]?shot|oneshot)\b"
)
SOURCE_SINGLE_VOLUME_ARTIFACT_TITLE_RE = re.compile(
    r"(?i)\b(?:volume|vol\.?|book|tpb|trade(?:\s+paperback)?|one[-\s]?shot|oneshot)\b"
)
SOURCE_CHAPTER_TITLE_RE = re.compile(r"(?i)\b(?:ch(?:apter)?\.?)\s*\d")
DIRECT_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def normalize_extension(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.startswith(".") and "/" not in text and "\\" not in text:
        return text
    path = unquote(urlparse(text).path or text)
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.lower()
    return suffix if suffix.startswith(".") else ""


def normalized_extensions(values):
    out = []
    seen = set()
    for value in values or []:
        ext = normalize_extension(value)
        if not ext or ext in seen:
            continue
        seen.add(ext)
        out.append(ext)
    return out


def content_type_base(value):
    return str(value or "").split(";", 1)[0].strip().lower()


def url_hash(value):
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


PROWLARR_CREDENTIAL_QUERY_KEYS = {
    "api-key", "api_key", "apikey", "auth", "key", "passkey", "rsskey", "token",
}
PROWLARR_CREDENTIAL_FIELD_KEYS = PROWLARR_CREDENTIAL_QUERY_KEYS | {
    "access_token", "authorization", "cookie", "password", "passwd", "proxy-authorization",
    "refresh_token", "secret", "set-cookie", "x-api-key", "x-auth-token",
}


def prowlarr_url_has_nested_credentials(value):
    """Detect an invalid URL-valued proxy link that itself carries a secret."""
    try:
        parsed = urlparse(str(value or "").strip())
    except (TypeError, ValueError):
        return False
    for _key, item in parse_qsl(parsed.query, keep_blank_values=True):
        nested = str(item or "").strip()
        if not nested.lower().startswith(("http://", "https://")):
            continue
        try:
            nested_query = parse_qsl(urlparse(nested).query, keep_blank_values=True)
        except (TypeError, ValueError):
            continue
        if any(str(key or "").strip().lower() in PROWLARR_CREDENTIAL_QUERY_KEYS for key, _ in nested_query):
            return True
    return False


def credential_free_prowlarr_url(value):
    """Remove reusable query credentials while preserving an operational locator."""
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
    except (TypeError, ValueError):
        return text
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return text
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if str(key or "").strip().lower() in PROWLARR_CREDENTIAL_QUERY_KEYS:
            continue
        nested = str(item or "").strip()
        if nested.lower().startswith(("http://", "https://")):
            item = credential_free_prowlarr_url(nested)
        query.append((key, item))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), ""))


def prowlarr_durable_payload(value):
    """Return Prowlarr result data that is safe to persist and still replayable."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key)
            if name.strip().lower() in PROWLARR_CREDENTIAL_FIELD_KEYS:
                out[name] = "[redacted]"
            else:
                out[name] = prowlarr_durable_payload(item)
        return out
    if isinstance(value, list):
        return [prowlarr_durable_payload(item) for item in value]
    if isinstance(value, tuple):
        return [prowlarr_durable_payload(item) for item in value]
    if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
        return credential_free_prowlarr_url(value)
    return value


def safe_filename_part(value, default="download"):
    text = str(value or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:160] or default


def normalized_query(value):
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def ascii_folded_query(value):
    text = normalized_query(value)
    if not text:
        return ""
    return normalized_query(unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii"))


def _query_tokens(value, ignored=None):
    ignored = {str(token or "").strip().lower() for token in (ignored or []) if str(token or "").strip()}
    text = normalized_query(value).lower()
    text = re.sub(r"(?<=[a-z0-9])['\u2019]s\b", "", text)
    return [
        token
        for token in re.findall(r"[^\W_]+", text, flags=re.UNICODE)
        if (len(token) > 1 or (len(token) == 1 and token.isalpha())) and token not in ignored
    ]


def _token_sequence_contains(query, haystack, ignored=None):
    tokens = _query_tokens(query, ignored=ignored)
    if not tokens:
        return True
    haystack_tokens = _query_tokens(haystack, ignored=ignored)
    if not haystack_tokens:
        return False
    if len(tokens) == 1:
        return tokens[0] in haystack_tokens
    span = len(tokens)
    for index in range(0, len(haystack_tokens) - span + 1):
        if haystack_tokens[index : index + span] == tokens:
            return True
    return False


def _query_tokens_with_numbers(value, ignored=None):
    ignored = {str(token or "").strip().lower() for token in (ignored or []) if str(token or "").strip()}
    text = normalized_query(value).lower()
    text = re.sub(r"(?<=[a-z0-9])['\u2019]s\b", "", text)
    return [
        token
        for token in re.findall(r"[^\W_]+", text, flags=re.UNICODE)
        if token and token not in ignored
    ]


def _token_sequence_contains_with_numbers(query, haystack, ignored=None):
    tokens = _query_tokens_with_numbers(query, ignored=ignored)
    if not tokens:
        return True
    haystack_tokens = _query_tokens_with_numbers(haystack, ignored=ignored)
    if not haystack_tokens:
        return False
    span = len(tokens)
    for index in range(0, len(haystack_tokens) - span + 1):
        if haystack_tokens[index : index + span] == tokens:
            return True
    return False


def _text_contains_numbered_series_sequence(series, haystack, ignored=None):
    series_tokens = _query_tokens_with_numbers(series, ignored=ignored)
    if not any(str(token).isdigit() for token in series_tokens):
        return True
    pairs = [
        (normalized_query(series).lower(), normalized_query(haystack).lower()),
        (ascii_folded_query(series).lower(), ascii_folded_query(haystack).lower()),
    ]
    return any(_token_sequence_contains_with_numbers(query_text, haystack_text, ignored=ignored) for query_text, haystack_text in pairs)


def _text_contains_query_tokens(query, haystack, ignored=None):
    pairs = [
        (normalized_query(query).lower(), normalized_query(haystack).lower()),
        (ascii_folded_query(query).lower(), ascii_folded_query(haystack).lower()),
    ]
    for query_text, haystack_text in pairs:
        if _token_sequence_contains(query_text, haystack_text, ignored=ignored):
            return True
    return False


def clipped_text(value, limit=EXTERNAL_TOOL_OUTPUT_MAX_CHARS):
    text = str(value or "")
    limit = max(0, int(limit or 0))
    if limit and len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def first_value(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def text_values(value):
    if value in (None, ""):
        return []
    if isinstance(value, dict):
        return [str(item).strip() for item in value.values() if str(item or "").strip()]
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(text_values(item))
        return out
    text = str(value or "").strip()
    return [text] if text else []


def first_text(*values):
    for value in values:
        texts = text_values(value)
        if texts:
            return texts[0]
    return ""


def int_value(value, default=None):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def normalize_protocol(value):
    text = str(value or "").strip().lower()
    if text in INDEXER_PROTOCOLS:
        return text
    return ""


def _indexer_artifact_extension(*values):
    for value in values:
        ext = normalize_extension(value)
        if not ext or ext in INDEXER_WRAPPER_EXTENSIONS:
            continue
        if ext in INDEXER_ARTIFACT_EXTENSIONS:
            return ext
    return ""


def normalized_category_token(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9/._ -]+", "", text)
    return text.strip(" -._/")


def category_ids(values):
    out = []
    seen = set()

    def add(value):
        if value in (None, ""):
            return
        if isinstance(value, dict):
            for key in ("id", "categoryId", "category_id", "newznabId", "torznabId"):
                if value.get(key) not in (None, ""):
                    add(value.get(key))
            for key in ("name", "label", "description"):
                if value.get(key):
                    add(value.get(key))
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        text = str(value or "").strip()
        for part in re.split(r"[,;|]", text):
            part = part.strip()
            if not part:
                continue
            for number in re.findall(r"\b\d{2,6}\b", part):
                if number not in seen:
                    seen.add(number)
                    out.append(number)
            token = normalized_category_token(part)
            if token and token not in seen:
                seen.add(token)
                out.append(token)

    add(values)
    return out


def indexer_policy_categories(policy, registry_row=None):
    policy = policy if isinstance(policy, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    raw = first_value(
        policy.get("categories"),
        policy.get("comic_categories"),
        policy.get("ebook_categories"),
        registry_row.get("categories"),
        registry_row.get("comic_categories"),
        registry_row.get("ebook_categories"),
    )
    return category_ids(raw)


def looks_pack_like(title):
    return bool(PACK_TITLE_RE.search(str(title or "")))


def issue_int(value):
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return int(float(match.group(0)))
    except Exception:
        return None


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


def number_word_int(value):
    return NUMBER_WORDS.get(str(value or "").strip().lower())


def indexer_volume_artifact_number_from_text(value):
    basename = indexer_manifest_entry_basename(value)
    if not basename:
        return None
    numeric = re.search(
        r"(?i)\b(?:omnibus|vol(?:ume)?|v|book)\.?\s*0*(\d{1,4})(?:\.\d+)?\b",
        basename,
    )
    if numeric:
        return issue_int(numeric.group(1))
    word = re.search(
        r"(?i)\b(?:omnibus|vol(?:ume)?|vol|book)\s+"
        r"(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b",
        basename,
    )
    if word:
        return number_word_int(word.group(1))
    return None


def indexer_pack_manifest_text_values(value):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float, bool)):
        return []
    if isinstance(value, dict):
        values = []
        for key in (
            "files",
            "file",
            "filename",
            "fileName",
            "name",
            "title",
            "summary",
            "description",
            "content",
            "entries",
            "pack_detail_entries",
        ):
            values.extend(indexer_pack_manifest_text_values(value.get(key)))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(indexer_pack_manifest_text_values(item))
        return values
    return [str(value)]


def indexer_pack_manifest_entries(candidate, limit=1000):
    candidate = candidate if isinstance(candidate, dict) else {}
    values = []
    for key in ("files", "pack_detail_entries", "description", "summary"):
        values.extend(indexer_pack_manifest_text_values(candidate.get(key)))
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    raw_result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    for key in ("files", "pack_detail_entries", "description", "summary"):
        values.extend(indexer_pack_manifest_text_values(raw_result.get(key)))
    entries = []
    seen = set()
    for value in values:
        for line in re.split(r"[\r\n]+", str(value or "").replace("\\n", "\n").replace("\\r", "\n")):
            text = html.unescape(line).strip(" \t\r\n-")
            if not text or not COMIC_FILE_EXTENSION_RE.search(text):
                continue
            text = re.sub(r"\s+", " ", text)
            if len(text) > 300:
                text = text[-300:]
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(text)
            if len(entries) >= max(1, int(limit or 1000)):
                return entries
    return entries


def indexer_manifest_entry_basename(entry):
    text = str(entry or "").replace("\\", "/").strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = COMIC_FILE_EXTENSION_RE.sub("", text)
    text = re.sub(r"\[[^\]]+\]|\{[^}]+\}", " ", text)
    return re.sub(r"\s+", " ", text.replace("_", " ")).strip()


def indexer_series_title_pattern(series):
    tokens = re.findall(r"[a-z0-9]+", str(series or "").lower())
    return r"[\W_]+".join(re.escape(token) for token in tokens)


def indexer_manifest_entry_matches_candidate(candidate, entry):
    candidate = candidate if isinstance(candidate, dict) else {}
    issue = issue_int(
        first_value(
            candidate.get("issue_number"),
            candidate.get("chapter_number"),
            candidate.get("volume_number") if candidate.get("unit_type") == "volume" else "",
        )
    )
    series = first_text(candidate.get("series_title"), candidate.get("series"))
    if issue is None or issue <= 0 or not series:
        return None
    basename = indexer_manifest_entry_basename(entry)
    pattern = indexer_series_title_pattern(series)
    if not basename or not pattern:
        return None
    if DIRECT_PREVIEW_ONLY_RE.search(basename):
        return None
    match = re.search(
        rf"^\s*{pattern}[\W_]+(?!(?:v|vol(?:ume)?|book|books|tpb|hc|hardcover|trade)\b)"
        rf"(?:#|issue|no\.?)?[\W_]*0*{issue}(?:[^0-9]|$)",
        basename,
        re.I,
    )
    if not match:
        return None
    return {
        "coverage_source": "pack_contents_filename",
        "entry": str(entry),
        "file_entry": basename,
        "series_title": series,
        "issue_number": str(candidate.get("issue_number") or issue),
        "calculated": str(issue).zfill(3),
    }


def indexer_manifest_entry_matches_volume_candidate(candidate, entry):
    candidate = candidate if isinstance(candidate, dict) else {}
    if not wanted_item_is_volume_unit(candidate):
        return None
    target_volume = issue_int(
        first_value(
            candidate.get("volume_number"),
            candidate.get("volume"),
            candidate.get("issue_number"),
            candidate.get("normalized_number"),
        )
    )
    series = first_text(candidate.get("series_title"), candidate.get("series"))
    if target_volume is None or target_volume <= 0 or not series:
        return None
    basename = indexer_manifest_entry_basename(entry)
    pattern = indexer_series_title_pattern(series)
    if not basename or not pattern:
        return None
    if DIRECT_PREVIEW_ONLY_RE.search(basename):
        return None
    if not re.search(rf"^\s*{pattern}(?:[\W_]+|$)", basename, re.I):
        return None
    source_volume = indexer_volume_artifact_number_from_text(basename)
    if source_volume != target_volume:
        return None
    return {
        "coverage_source": "pack_contents_volume_filename",
        "entry": str(entry),
        "file_entry": basename,
        "series_title": series,
        "issue_number": str(candidate.get("issue_number") or target_volume),
        "volume_number": str(candidate.get("volume_number") or target_volume),
        "calculated": str(target_volume).zfill(3),
    }


def indexer_manifest_pack_match(candidate):
    entries = indexer_pack_manifest_entries(candidate)
    for entry in entries:
        match = indexer_manifest_entry_matches_candidate(candidate, entry)
        if not match:
            match = indexer_manifest_entry_matches_volume_candidate(candidate, entry)
        if match:
            match["content_entry_count"] = len(entries)
            return match
    return None


def indexer_unique_text_entries(values, limit=1000):
    out = []
    seen = set()
    for value in values or []:
        text = str(value or "").replace("\\", "/").strip(" \t\r\n-")
        if not text:
            continue
        text = re.sub(r"\s+", " ", text)
        if len(text) > 300:
            text = text[-300:]
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= max(1, int(limit or 1000)):
            break
    return out


def indexer_comic_file_entry_text_candidates(text):
    raw = str(text or "")
    if not raw:
        return []
    try:
        raw = html.unescape(raw)
    except Exception:
        pass
    candidates = []
    attr_re = re.compile(r"(?is)\b(?:subject|filename|fileName|name|title)\s*=\s*(['\"])(.*?)\1")
    quoted_re = re.compile(
        r"(?is)(['\"])([^'\"]{1,500}?\.(?:cbz|cbr|pdf|epub|zip|rar|7z)\b[^'\"]{0,220})\1"
    )
    for match in attr_re.finditer(raw):
        value = match.group(2)
        if COMIC_FILE_EXTENSION_RE.search(value):
            candidates.append(value)
    for match in quoted_re.finditer(raw):
        value = match.group(2)
        if COMIC_FILE_EXTENSION_RE.search(value):
            candidates.append(value)
    return candidates


def indexer_comic_file_entries_from_text(text, limit=1000):
    entries = []
    for line in re.split(r"[\r\n]+", str(text or "")):
        line = line.strip()
        if not line:
            continue
        for candidate in indexer_comic_file_entry_text_candidates(line):
            entries.append(candidate)
            if len(entries) >= max(1, int(limit or 1000)):
                break
        if len(entries) >= max(1, int(limit or 1000)):
            break
        if not COMIC_FILE_EXTENSION_RE.search(line):
            continue
        entries.append(line)
        if len(entries) >= max(1, int(limit or 1000)):
            break
    return indexer_unique_text_entries(entries, limit=limit)


# Shared with inkdrop_missing_acquire's copies -- see inkdrop_bencode.
_indexer_bdecode_value = inkdrop_bencode.bdecode_value
_indexer_torrent_dict_get = inkdrop_bencode.torrent_dict_get
_indexer_torrent_text = inkdrop_bencode.torrent_text
_indexer_torrent_path_text = inkdrop_bencode.torrent_path_text


def indexer_torrent_file_entries(data, limit=1000):
    root = _indexer_bdecode_value(data)
    info = _indexer_torrent_dict_get(root, "info")
    if not isinstance(info, dict):
        return []
    entries = []
    files = _indexer_torrent_dict_get(info, "files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            path = _indexer_torrent_dict_get(item, "path.utf-8", "path")
            entry = _indexer_torrent_path_text(path)
            if entry:
                entries.append(entry)
            if len(entries) >= max(1, int(limit or 1000)):
                break
    else:
        name = _indexer_torrent_dict_get(info, "name.utf-8", "name")
        entry = _indexer_torrent_path_text(name)
        if entry:
            entries.append(entry)
    return indexer_unique_text_entries(entries, limit=limit)


def _indexer_xml_tag_name(tag):
    return str(tag or "").rsplit("}", 1)[-1].lower()


def indexer_nzb_file_entries(data, limit=1000):
    try:
        text = bytes(data).decode("utf-8", errors="replace")
    except Exception:
        text = str(data or "")
    entries = []
    try:
        root = ET.fromstring(text)
        for elem in root.iter():
            tag = _indexer_xml_tag_name(elem.tag)
            if tag == "file":
                entries.extend(indexer_comic_file_entries_from_text(elem.attrib.get("subject") or "", limit=limit))
            elif tag == "meta":
                meta_type = str(elem.attrib.get("type") or "").lower()
                if meta_type in {"filename", "name", "title"}:
                    entries.extend(indexer_comic_file_entries_from_text(elem.text or "", limit=limit))
            if len(entries) >= max(1, int(limit or 1000)):
                break
    except ET.ParseError:
        pass
    if len(entries) < max(1, int(limit or 1000)):
        entries.extend(indexer_comic_file_entries_from_text(text, limit=max(1, int(limit or 1000)) - len(entries)))
    return indexer_unique_text_entries(entries, limit=limit)


def indexer_pack_detail_entries_from_bytes(data, protocol="", limit=1000):
    payload = bytes(data or b"")
    if not payload:
        return []
    protocol = normalize_protocol(protocol)
    if protocol == "torrent" or (payload[:1] == b"d" and b"4:info" in payload[:4096]):
        try:
            entries = indexer_torrent_file_entries(payload, limit=limit)
            if entries:
                return entries
        except Exception:
            pass
    if protocol == "usenet" or b"<nzb" in payload[:4096].lower():
        entries = indexer_nzb_file_entries(payload, limit=limit)
        if entries:
            return entries
    try:
        text = payload.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    return indexer_comic_file_entries_from_text(text, limit=limit)


VOLUME_UNIT_TYPES = {"volume", "vol", "book_volume", "manga_volume"}


def _wanted_provider_key(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    return first_text(
        wanted_item.get("metadata_provider"),
        wanted_item.get("provider"),
        wanted_item.get("series_source"),
        wanted_item.get("source"),
        wanted_item.get("source_provider"),
    ).lower()


def _wanted_series_id_key(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    return first_text(
        wanted_item.get("series_id"),
        wanted_item.get("queue_identity"),
        wanted_item.get("native_series_id"),
    ).lower()


def _wanted_unit_model_key(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    return first_text(
        wanted_item.get("manga_unit_model"),
        wanted_item.get("series_unit_model"),
        wanted_item.get("unit_model"),
    ).lower()


def _wanted_manga_hint(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    media_type = str(wanted_item.get("media_type") or "").strip().lower()
    publisher = first_text(
        wanted_item.get("publisher"),
        wanted_item.get("watch_publisher"),
        wanted_item.get("series_publisher"),
    )
    return bool(media_type == "manga" or MANGA_PUBLISHER_HINT_RE.search(publisher))


def wanted_item_is_single_volume_artifact_unit(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    if not _wanted_manga_hint(wanted_item):
        return False
    provider = _wanted_provider_key(wanted_item)
    series_id = _wanted_series_id_key(wanted_item)
    unit_model = _wanted_unit_model_key(wanted_item)
    if (
        provider in {"mangadex", "suwayomi"}
        or series_id.startswith("mangadex:")
        or series_id.startswith("suwayomi:")
        or unit_model in {"chapter", "manga_chapter", "chapter_native", "native_chapter"}
    ):
        return False
    if provider and provider not in {"comicvine", "kapowarr", "watch"}:
        return False
    issue_number = first_text(
        wanted_item.get("issue_number"),
        wanted_item.get("issueNumber"),
        wanted_item.get("issue"),
        wanted_item.get("normalized_number"),
        wanted_item.get("number"),
    )
    issue_title = first_text(
        wanted_item.get("issue_title"),
        wanted_item.get("issueTitle"),
        wanted_item.get("title"),
    )
    return bool(issue_number and MANGA_SINGLE_VOLUME_ARTIFACT_ISSUE_TITLE_RE.search(issue_title))


def _wanted_unit_metadata(wanted_item=None, default_unit_type="issue"):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    unit_type = first_text(
        wanted_item.get("unit_type"),
        wanted_item.get("unitType"),
        wanted_item.get("unit"),
        default_unit_type,
    ).lower()
    issue_number = first_text(
        wanted_item.get("issue_number"),
        wanted_item.get("issueNumber"),
        wanted_item.get("issue"),
        wanted_item.get("normalized_number"),
        wanted_item.get("number"),
    )
    chapter_number = first_text(
        wanted_item.get("chapter_number"),
        wanted_item.get("chapterNumber"),
        wanted_item.get("chapter"),
    )
    if not chapter_number and unit_type == "chapter":
        chapter_number = issue_number
    volume_number = first_text(
        wanted_item.get("volume_number"),
        wanted_item.get("volumeNumber"),
        wanted_item.get("volume"),
        wanted_item.get("book_volume"),
        wanted_item.get("manga_volume"),
    )
    if not volume_number:
        for key in ("issue_title", "issueTitle", "title", "searchQuery", "search_query", "query"):
            match = re.search(r"(?i)\bvol(?:ume)?\.?\s*(\d+(?:\.\d+)?)\b", str(wanted_item.get(key) or ""))
            if match:
                volume_number = match.group(1)
                break
    if not volume_number and wanted_item_is_single_volume_artifact_unit(wanted_item):
        volume_number = issue_number
    if not volume_number and unit_type in VOLUME_UNIT_TYPES:
        volume_number = issue_number
    return {
        "unit_type": unit_type or default_unit_type,
        "issue_number": issue_number,
        "chapter_number": chapter_number,
        "volume_number": volume_number,
    }

def _truthy_policy_value(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def wanted_item_is_volume_unit(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    metadata = _wanted_unit_metadata(wanted_item, default_unit_type="")
    unit = metadata.get("unit_type")
    media_type = str(wanted_item.get("media_type") or "").strip().lower()
    volume = first_text(wanted_item.get("volume"), wanted_item.get("volume_number"), wanted_item.get("volumeNumber"))
    chapter = first_text(wanted_item.get("chapter"), wanted_item.get("chapter_number"), wanted_item.get("chapterNumber"))
    provider = _wanted_provider_key(wanted_item)
    series_id = _wanted_series_id_key(wanted_item)
    unit_model = _wanted_unit_model_key(wanted_item)
    publisher = first_text(
        wanted_item.get("publisher"),
        wanted_item.get("watch_publisher"),
        wanted_item.get("series_publisher"),
    )
    issue_title = first_text(
        wanted_item.get("issue_title"),
        wanted_item.get("issueTitle"),
        wanted_item.get("title"),
    )
    manga_hint = _wanted_manga_hint(wanted_item)
    chapter_native_provider = (
        provider in {"mangadex", "suwayomi"}
        or first_text(wanted_item.get("issue_metadata_provider")).lower() in {"mangadex", "suwayomi"}
        or series_id.startswith("mangadex:")
        or series_id.startswith("suwayomi:")
    )
    chapter_native_model = unit_model in {"chapter", "manga_chapter", "chapter_native", "native_chapter"} or (
        unit == "chapter" and unit_model == "mixed_chapter_preferred"
    )
    if unit in VOLUME_UNIT_TYPES:
        return True
    if media_type == "manga" and volume and not chapter:
        return True
    if unit == "chapter" and (chapter_native_provider or chapter_native_model):
        return False
    if unit == "chapter" and provider in {"comicvine", "kapowarr", "watch"} and manga_hint:
        return True
    if unit == "chapter":
        return False
    comicvine_manga_book = provider == "comicvine" and manga_hint and not chapter_native_model
    if comicvine_manga_book and (
        MANGA_VOLUME_ISSUE_TITLE_RE.search(issue_title)
        or first_text(wanted_item.get("issue_number"), wanted_item.get("number"), wanted_item.get("normalized_number"))
    ):
        return True
    return False


def single_chapter_volume_page_pack_allowed(registry_row=None):
    policy = provider_policy(registry_row)
    return _truthy_policy_value(policy.get("allow_single_chapter_volume_page_pack"))


def page_pack_chapter_blocks_volume_target(chapter_number, wanted_item=None, registry_row=None):
    if single_chapter_volume_page_pack_allowed(registry_row):
        return False
    return bool(wanted_item_is_volume_unit(wanted_item) and str(chapter_number or "").strip())


def volume_page_pack_enabled(registry_row=None):
    policy = provider_policy(registry_row)
    value = policy.get("enable_volume_page_pack")
    if value in (None, ""):
        return True
    return _truthy_policy_value(value)


def volume_page_pack_min_chapters(registry_row=None):
    policy = provider_policy(registry_row)
    return max(2, min(int_value(policy.get("volume_page_pack_min_chapters"), 2) or 2, 100))


def volume_page_pack_max_chapters(registry_row=None):
    policy = provider_policy(registry_row)
    return max(1, min(int_value(policy.get("volume_page_pack_max_chapters"), 40) or 40, 200))


def content_type_for_extension(ext):
    ext = normalize_extension(ext)
    if ext == ".cbz":
        return "application/zip"
    if ext == ".cbr":
        return "application/x-rar-compressed"
    if ext == ".epub":
        return "application/epub+zip"
    if ext == ".pdf":
        return "application/pdf"
    if ext == ".txt":
        return "text/plain"
    if ext == ".zip":
        return "application/zip"
    return ""


def extension_for_content_type(value):
    content_type = content_type_base(value)
    if not content_type:
        return ""
    if "epub" in content_type:
        return ".epub"
    if content_type == "application/pdf":
        return ".pdf"
    if "comicbook+zip" in content_type:
        return ".cbz"
    if "comicbook-rar" in content_type or "rar" in content_type:
        return ".cbr"
    if "zip" in content_type:
        return ".zip"
    if content_type.startswith("text/plain"):
        return ".txt"
    return ""


def provider_policy(registry_row=None, candidate=None):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    policy = {}
    if isinstance(registry_row.get("policy"), dict):
        policy.update(registry_row["policy"])
    if isinstance(candidate.get("policy"), dict):
        policy.update(candidate["policy"])
    allowed = normalized_extensions(
        policy.get("allowed_extensions")
        or registry_row.get("allowed_extensions")
        or candidate.get("allowed_extensions")
        or []
    )
    if allowed:
        policy["allowed_extensions"] = allowed
    return policy


def classify_candidate_outcome(candidate, registry_row=None, staging_root=None):
    """Add the four-way unattended-acquisition outcome without relaxing verdicts."""
    out = dict(candidate or {})
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    blocks = list(dict.fromkeys(out.get("block_reasons") or []))
    reviews = list(dict.fromkeys(out.get("review_reasons") or []))
    compatibility = out.get("target_compatibility") if isinstance(out.get("target_compatibility"), dict) else {}
    blocks = list(dict.fromkeys([*blocks, *(compatibility.get("rejection_codes") or [])]))
    reviews = list(dict.fromkeys([*reviews, *(compatibility.get("review_codes") or [])]))

    if blocks or out.get("auto_grab_verdict") == "blocked":
        outcome = "rejected"
    elif (
        out.get("candidate_safe") is True
        and out.get("auto_grab_verdict") == "auto_grab_safe"
        and not blocks
        and not reviews
    ):
        outcome = "auto_grab"
    else:
        policy = provider_policy(registry_row, out)
        protocol = normalize_protocol(out.get("protocol"))
        client = str(_indexer_download_client(protocol, out, policy) or "").strip().lower()
        positive = set(compatibility.get("positive_evidence") or [])
        match = str(out.get("match_confidence") or "").strip().lower().replace("-", "_")
        exact_work = match in {
            "title_issue_match", "title_chapter_match", "title_volume_match",
            "exact_title_and_unit", "exact",
        } or "singleton_exact_title" in positive
        exact_unit = bool(positive & AUTO_INSPECT_EXACT_UNIT_EVIDENCE)
        magnet = first_text(out.get("magnet_url"), out.get("magnetUrl"))
        magnet_hash = _magnet_info_hash(magnet).lower()
        valid_magnet = magnet if (
            urlparse(magnet).scheme.lower() == "magnet"
            and re.fullmatch(r"(?:[0-9a-f]{40}|[a-z2-7]{32})", magnet_hash)
        ) else ""
        authorized_url = authorized_prowlarr_download_url(out, registry_row)
        locator = valid_magnet or authorized_url
        controlled_root = first_text(policy.get("auto_inspect_staging_root"), out.get("auto_inspect_staging_root"), staging_root)
        neutral_only = bool(reviews) and set(reviews).issubset(AUTO_INSPECT_NEUTRAL_REVIEW_REASONS)
        concrete_handoff = bool(
            protocol in INDEXER_PROTOCOLS
            and locator
            and client in SUPPORTED_AUTO_INSPECT_CLIENTS
            and registry_row.get("auto_download_allowed")
            and controlled_root
        )
        outcome = "auto_inspect" if neutral_only and exact_work and exact_unit and concrete_handoff else "manual_only"
        if outcome == "auto_inspect":
            out["locator_digest"] = url_hash(locator)
            out["auto_inspect_locator_kind"] = "magnet" if valid_magnet else "prowlarr_download_url"
            if authorized_url:
                out["authorized_prowlarr_download_url"] = True

    out["candidate_outcome"] = outcome
    if outcome == "auto_inspect":
        out["candidate_safe"] = False
        out["artifact_safe"] = False
        out["quality_status"] = "inspect"
    return out


def candidate_identity(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    return inkdrop_sources.stable_id(
        "source_candidate",
        candidate.get("provider_id"),
        candidate.get("canonical_item_id") or candidate.get("canonical_work_id"),
        candidate.get("download_url_hash"),
        candidate.get("title"),
    )


def indexer_candidate_key(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    locator = first_value(
        candidate.get("info_hash"),
        candidate.get("guid"),
        candidate.get("download_url_hash"),
    )
    if not locator:
        locator = url_hash(first_value(candidate.get("magnet_url"), candidate.get("download_url")))
    if not locator:
        locator = candidate.get("title")
    return inkdrop_sources.stable_id(
        "indexer_locator",
        candidate.get("provider_id"),
        normalize_protocol(candidate.get("protocol")),
        candidate.get("indexer_id") or candidate.get("indexer"),
        locator,
    )


def indexer_candidate_identity(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    return candidate.get("indexer_candidate_key") or candidate.get("indexer_suppression_key") or indexer_candidate_key(candidate)


def external_tool_candidate_identity(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    return inkdrop_sources.stable_id(
        "external_tool_candidate",
        candidate.get("provider_id"),
        candidate.get("tool_name"),
        candidate.get("source_site"),
        candidate.get("canonical_item_id"),
        candidate.get("source_url_hash"),
        candidate.get("title"),
    )


def manual_source_card_identity(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    return inkdrop_sources.stable_id(
        "manual_source_card",
        candidate.get("provider_id"),
        candidate.get("source_site"),
        candidate.get("canonical_item_id"),
        candidate.get("source_url_hash"),
        candidate.get("title"),
    )


def direct_artifact_key(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    locator = first_value(
        candidate.get("download_url_hash"),
        url_hash(candidate.get("download_url")),
        candidate.get("canonical_item_id"),
    )
    return inkdrop_sources.stable_id(
        "direct_artifact",
        candidate.get("provider_id"),
        candidate.get("source_kind"),
        candidate.get("canonical_item_id") or candidate.get("canonical_work_id"),
        locator,
        normalize_extension(candidate.get("extension") or candidate.get("download_url")),
    )


def source_candidate(
    *,
    provider_id,
    title,
    download_url="",
    source_url="",
    canonical_item_id="",
    canonical_work_id="",
    series_title="",
    creator="",
    language="",
    extension="",
    content_type="",
    size_bytes=None,
    rights_status="",
    license_url="",
    provider_type="direct_download",
    source_kind="",
    wanted_item=None,
    unit_type="",
    issue_number="",
    chapter_number="",
    volume_number="",
    raw=None,
):
    provider_id = inkdrop_sources.provider_key(provider_id)
    ext = normalize_extension(extension or download_url or source_url)
    download_hash = url_hash(download_url)
    unit_metadata = _wanted_unit_metadata(wanted_item)
    out = {
        "candidate_contract_version": 1,
        "provider_id": provider_id,
        "source": provider_id,
        "provider_type": provider_type,
        "source_kind": source_kind,
        "canonical_item_id": str(canonical_item_id or ""),
        "canonical_work_id": str(canonical_work_id or ""),
        "unit_type": first_text(unit_type, unit_metadata.get("unit_type")),
        "issue_number": first_text(issue_number, unit_metadata.get("issue_number")),
        "chapter_number": first_text(chapter_number, unit_metadata.get("chapter_number")),
        "volume_number": first_text(volume_number, unit_metadata.get("volume_number")),
        "title": str(title or "").strip(),
        "series_title": str(series_title or title or "").strip(),
        "creator": str(creator or "").strip(),
        "language": str(language or "").strip().lower(),
        "translated_language": str(language or "").strip().lower(),
        "url": str(source_url or download_url or "").strip(),
        "source_url": str(source_url or download_url or "").strip(),
        "download_url": str(download_url or "").strip(),
        "download_url_hash": download_hash,
        "resolver_required": not bool(download_url),
        "extension": ext,
        "content_type": content_type_base(content_type),
        "size_bytes": size_bytes,
        "rights_status": str(rights_status or "").strip().lower(),
        "license_url": str(license_url or "").strip(),
        "score": 0,
        "match_confidence": "unknown",
        "language_status": "unknown",
        "quality_status": "unchecked",
        "pack": False,
        "external_url": False,
        "auto_grab_verdict": "review",
        "review_reason": "",
        "raw": raw if isinstance(raw, dict) else {},
    }
    out["candidate_identity"] = candidate_identity(out)
    out["direct_artifact_key"] = direct_artifact_key(out)
    out["direct_suppression_key"] = out["direct_artifact_key"]
    return out


def _header_value(headers, key):
    headers = headers if isinstance(headers, dict) else {}
    lowered = {str(k).lower(): v for k, v in headers.items()}
    return lowered.get(key.lower())


def _size_from_candidate_or_headers(candidate, headers):
    value = (candidate or {}).get("size_bytes")
    if value in (None, ""):
        value = _header_value(headers, "content-length")
    content_range = str(_header_value(headers, "content-range") or "").strip()
    if content_range:
        match = re.search(r"/(\d+)\s*$", content_range)
        if match:
            value = match.group(1)
    try:
        return int(value)
    except Exception:
        return None


def _content_type_from_candidate_or_headers(candidate, headers):
    return content_type_base(_header_value(headers, "content-type") or (candidate or {}).get("content_type"))


def _content_disposition_filename(headers):
    value = str(_header_value(headers, "content-disposition") or "").strip()
    if not value:
        return ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)"?', value, flags=re.I)
    return unquote(match.group(1).strip()) if match else ""


def _probe_redirect_url(headers):
    return first_text(
        _header_value(headers, "x-inkdrop-final-url"),
        _header_value(headers, "x-inkdrop-response-url"),
        _header_value(headers, "location"),
    )


def _extension_from_candidate_or_headers(candidate, headers):
    candidate = candidate or {}
    headers = headers if isinstance(headers, dict) else {}
    ext = normalize_extension(candidate.get("extension") or candidate.get("download_url") or candidate.get("url"))
    if ext:
        return ext
    ext = normalize_extension(_content_disposition_filename(headers))
    if ext:
        return ext
    ext = normalize_extension(_probe_redirect_url(headers))
    if ext:
        return ext
    return extension_for_content_type(_content_type_from_candidate_or_headers(candidate, headers))


def _rights_allowed(candidate, policy):
    rights_gate = str((policy or {}).get("rights_gate") or "").strip().lower()
    if rights_gate in {"", "metadata_only", "manual_review_required"}:
        return True
    rights_status = str((candidate or {}).get("rights_status") or "").strip().lower()
    if "public_domain" in rights_gate or "open_license" in rights_gate:
        return rights_status in PROVIDER_ALLOWED_RIGHTS
    return True


def _direct_language_status(candidate, policy):
    allowed = [str(item or "").strip().lower() for item in text_values((policy or {}).get("allowed_languages")) if str(item or "").strip()]
    if not allowed:
        return "not_checked"
    language = str((candidate or {}).get("language") or (candidate or {}).get("translated_language") or "").strip().lower()
    if not language:
        return "unknown"
    if language in set(allowed):
        return "accepted"
    return "rejected"


def _direct_quality_profile(candidate):
    ext = normalize_extension((candidate or {}).get("extension") or (candidate or {}).get("download_url"))
    if ext in {".epub", ".mobi", ".azw3", ".pdf", ".txt", ".html"}:
        return f"ebook_{ext.lstrip('.')}"
    if ext in {".cbz", ".cbr", ".zip"}:
        return f"archive_{ext.lstrip('.')}"
    return ext.lstrip(".") or "unknown"


def _direct_candidate_preview_only(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    series_title = normalized_query(first_text(candidate.get("series_title"), candidate.get("series"))).lower()
    if DIRECT_PREVIEW_ONLY_RE.search(series_title):
        return False
    haystack = normalized_query(
        " ".join(
            first_text(candidate.get(key))
            for key in ("title", "source_url", "url", "download_url", "canonical_item_id")
        )
    ).lower()
    return bool(DIRECT_PREVIEW_ONLY_RE.search(haystack))


def _direct_candidate_cover_only(candidate, ext="", content_type=""):
    candidate = candidate if isinstance(candidate, dict) else {}
    ext = normalize_extension(ext or candidate.get("extension") or candidate.get("download_url"))
    content_type = content_type_base(content_type or candidate.get("content_type"))
    return ext in DIRECT_IMAGE_ARTIFACT_EXTENSIONS or content_type in DIRECT_IMAGE_CONTENT_TYPES


def _direct_import_handoff_expectation(artifact_safe=False):
    if artifact_safe:
        return "inkdrop_direct_download_then_import_ready_worker_then_kavita_import_verification"
    return "none_until_direct_artifact_verdict_safe"


def direct_artifact_verdict(candidate, registry_row=None, headers=None, min_size_bytes=None, max_size_bytes=None):
    """Return a validated candidate copy with direct-download safety verdict fields."""
    candidate = dict(candidate or {})
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    headers = headers if isinstance(headers, dict) else {}
    policy = provider_policy(registry_row, candidate)
    allowed_extensions = normalized_extensions(policy.get("allowed_extensions") or [])
    ext = _extension_from_candidate_or_headers(candidate, headers)
    content_type = _content_type_from_candidate_or_headers(candidate, headers)
    size_bytes = _size_from_candidate_or_headers(candidate, headers)
    language_status = _direct_language_status(candidate, policy)
    quality_profile = candidate.get("quality_profile") or _direct_quality_profile(candidate)
    min_size = DEFAULT_MIN_SIZE_BYTES if min_size_bytes is None else int(min_size_bytes)
    max_size = DEFAULT_MAX_SIZE_BYTES if max_size_bytes is None else int(max_size_bytes)
    registry_state = str(registry_row.get("registry_state") or "").strip().lower()
    source_kind = str(candidate.get("source_kind") or registry_row.get("source_kind") or "").strip().lower()
    probe_required = source_kind in {"direct_file_probe_source", "rss_detail_probe_feed"}
    probe_status = int_value(candidate.get("probe_status_code"), None)
    block_reasons = []

    if registry_row and registry_state not in {"ready", "assist", "manual_review"}:
        block_reasons.append(f"registry_{registry_state or 'unavailable'}")
    if registry_row and not registry_row.get("auto_search_allowed") and registry_state != "manual_review":
        block_reasons.append("registry_search_not_allowed")
    if not candidate.get("download_url"):
        block_reasons.append("no_download_url")
    if probe_required and probe_status is None:
        block_reasons.append("probe_status_unknown")
    elif probe_required and not (200 <= probe_status < 300):
        block_reasons.append("probe_status_not_success")
    if not ext:
        block_reasons.append("missing_extension")
    if _direct_candidate_preview_only(candidate):
        block_reasons.append("preview_only_artifact")
    if _direct_candidate_cover_only(candidate, ext=ext, content_type=content_type):
        block_reasons.append("cover_only_artifact")
    elif allowed_extensions and ext not in allowed_extensions:
        block_reasons.append(f"extension_{ext.lstrip('.')}_not_allowed")
    if content_type in HTML_CONTENT_TYPES:
        block_reasons.append("html_content_type")
    elif content_type and ext in CONTENT_TYPES_BY_EXTENSION and content_type not in CONTENT_TYPES_BY_EXTENSION[ext]:
        block_reasons.append("content_type_mismatch")
    if size_bytes is None:
        block_reasons.append("size_unknown")
    elif size_bytes <= 0:
        block_reasons.append("zero_size")
    elif size_bytes < min_size:
        block_reasons.append("size_too_small")
    elif size_bytes > max_size:
        block_reasons.append("size_too_large")
    if not _rights_allowed(candidate, policy):
        block_reasons.append("rights_gate_failed")
    if language_status == "rejected":
        block_reasons.append("language_not_allowed")
    pack_requires_review = bool(candidate.get("pack")) and not bool(
        policy.get("packs_allowed") or policy.get("allow_packs") or policy.get("pack_auto_allowed")
    )
    if pack_requires_review:
        block_reasons.append("pack_requires_review")

    requires_manual = bool(registry_row.get("requires_manual_review") or candidate.get("requires_manual_review") or pack_requires_review)
    can_auto_download = bool(registry_row.get("auto_download_allowed")) if registry_row else True
    if requires_manual:
        block_reasons.append("manual_review_required")
    if registry_row and not can_auto_download:
        block_reasons.append("auto_download_not_allowed")

    candidate["extension"] = ext
    candidate["content_type"] = content_type
    candidate["size_bytes"] = size_bytes
    candidate["probe_status_code"] = probe_status
    candidate["allowed_extensions"] = allowed_extensions
    candidate["language_status"] = language_status
    candidate["quality_profile"] = quality_profile
    candidate["quality"] = quality_profile
    candidate["direct_artifact_key"] = candidate.get("direct_artifact_key") or direct_artifact_key(candidate)
    candidate["direct_suppression_key"] = candidate.get("direct_suppression_key") or candidate["direct_artifact_key"]
    candidate["block_reasons"] = block_reasons
    candidate["artifact_safe"] = not block_reasons
    if block_reasons:
        manual_only = "manual_review_required" in block_reasons or registry_state == "manual_review"
        candidate["auto_grab_verdict"] = "review" if manual_only else "blocked"
        candidate["review_reason"] = block_reasons[0]
        candidate["quality_status"] = "rejected"
    else:
        candidate["auto_grab_verdict"] = "auto_grab_safe"
        candidate["review_reason"] = ""
        candidate["quality_status"] = "accepted"
    return candidate


def direct_download_provider_root(staging_root, provider_id, registry_row=None):
    """Resolve where a direct-download provider's finished files land.

    Defaults to ``<staging_root>/<provider_id>`` (the existing layout), but an
    operator can point a provider at its own volume/path via the provider's
    ``download_root`` setting -- mirroring how SLSKD's download/incomplete
    roots are independently configurable.
    """

    provider_id = inkdrop_sources.provider_key(provider_id)
    default_root = PurePosixPath(str(staging_root or "/tmp/inkdrop-direct-staging").replace("\\", "/"))
    configured = str((registry_row or {}).get("download_root") or "").strip()
    if configured:
        return PurePosixPath(configured.replace("\\", "/"))
    return default_root / provider_id if provider_id else default_root


def direct_download_incomplete_root(download_root, registry_row=None):
    """Resolve where a direct-download provider's in-progress ``.part`` files land.

    Defaults to an ``incomplete`` subdirectory of the provider's download
    root, same as SLSKD's default. An operator can override it independently
    via the provider's ``incomplete_root`` setting.
    """

    configured = str((registry_row or {}).get("incomplete_root") or "").strip()
    if configured:
        return PurePosixPath(configured.replace("\\", "/"))
    return PurePosixPath(str(download_root)) / "incomplete"


def direct_download_task_seed(candidate, staging_root, *, registry_row=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    ext = normalize_extension(candidate.get("extension") or candidate.get("download_url"))
    filename = safe_filename_part(candidate.get("title") or candidate.get("canonical_item_id")) + (ext or "")
    provider_id = inkdrop_sources.provider_key(candidate.get("provider_id"))
    download_root = direct_download_provider_root(staging_root, provider_id, registry_row)
    incomplete_root = direct_download_incomplete_root(download_root, registry_row)
    local_path = download_root / filename
    partial_path = incomplete_root / (filename + ".part")
    return {
        "source": provider_id,
        "provider": provider_id,
        "provider_id": provider_id,
        "protocol": "http",
        "download_client": DIRECT_DOWNLOAD_CLIENT,
        "external_id": candidate.get("candidate_identity") or candidate_identity(candidate),
        "candidate_identity": candidate.get("candidate_identity") or candidate_identity(candidate),
        "title": candidate.get("title"),
        "status": "download_resolved",
        "state": "queued",
        "save_path": str(local_path.parent),
        "local_path": str(local_path),
        "partial_path": str(partial_path),
        "size_bytes": candidate.get("size_bytes"),
        "progress": 0,
        "download_url_hash": candidate.get("download_url_hash"),
        "raw_json": {
            "candidate": candidate,
            "download_guard": "direct_artifact_verdict",
            "direct_artifact": {
                "artifact_key": candidate.get("direct_artifact_key") or direct_artifact_key(candidate),
                "suppression_key": candidate.get("direct_suppression_key") or candidate.get("direct_artifact_key") or direct_artifact_key(candidate),
                "source_path": candidate.get("source_url") or candidate.get("url"),
                "unit_type": candidate.get("unit_type") or candidate.get("unitType"),
                "issue_number": candidate.get("issue_number") or candidate.get("issue"),
                "chapter_number": candidate.get("chapter_number") or candidate.get("chapter"),
                "volume_number": candidate.get("volume_number") or candidate.get("volume"),
                "language": candidate.get("language"),
                "language_status": candidate.get("language_status"),
                "match_confidence": candidate.get("match_confidence"),
                "quality_profile": candidate.get("quality_profile") or candidate.get("quality"),
                "rights_status": candidate.get("rights_status"),
                "discovery_provider_id": candidate.get("discovery_provider_id") or provider_id,
                "transport_id": candidate.get("transport_id"),
                "transport_allowed_hosts": list(candidate.get("transport_allowed_hosts") or []),
                "max_redirects": candidate.get("max_redirects"),
            },
            "import_handoff_expectation": _direct_import_handoff_expectation(True),
        },
    }


def source_search_attempt_seed(
    registry_row,
    *,
    query="",
    status="searching",
    reason="",
    candidate_count=None,
    safe_candidate_count=None,
    rejected_candidate_count=None,
    latency_ms=None,
    raw=None,
):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    provider_id = inkdrop_sources.provider_key(registry_row.get("provider_id"))
    status = str(status or "searching").strip().lower()
    attempt = {
        "source": provider_id,
        "provider": provider_id,
        "provider_id": provider_id,
        "source_type": registry_row.get("provider_type") or "source",
        "provider_mode": registry_row.get("source_mode"),
        "registry_state": registry_row.get("registry_state"),
        "risk_class": registry_row.get("risk_class"),
        "status": status,
        "query": normalized_query(query),
        "title": normalized_query(query),
        "reason": str(reason or "").strip(),
        "candidate_count": candidate_count,
        "safe_candidate_count": safe_candidate_count,
        "rejected_candidate_count": rejected_candidate_count,
        "latency_ms": latency_ms,
        "auto_download_allowed": bool(registry_row.get("auto_download_allowed")),
        "requires_manual_review": bool(registry_row.get("requires_manual_review")),
        "raw": raw if isinstance(raw, dict) else {},
    }
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def direct_candidate_attempt_seed(candidate, registry_row=None, staging_root=None, status=None, reason=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    provider_id = inkdrop_sources.provider_key(
        candidate.get("provider_id") or registry_row.get("provider_id")
    )
    artifact_safe = bool(candidate.get("artifact_safe"))
    block_reasons = list(candidate.get("block_reasons") or [])
    if status is None:
        status = "sent" if artifact_safe else ("review" if candidate.get("auto_grab_verdict") == "review" else "blocked")
    status = str(status or "").strip().lower()
    failure_reason = str(reason or candidate.get("review_reason") or (block_reasons[0] if block_reasons else "")).strip()
    direct_metadata = {
        "artifact_key": candidate.get("direct_artifact_key") or direct_artifact_key(candidate),
        "suppression_key": candidate.get("direct_suppression_key") or candidate.get("direct_artifact_key") or direct_artifact_key(candidate),
        "source_path": candidate.get("source_url") or candidate.get("url"),
        "unit_type": candidate.get("unit_type") or candidate.get("unitType"),
        "issue_number": candidate.get("issue_number") or candidate.get("issue"),
        "chapter_number": candidate.get("chapter_number") or candidate.get("chapter"),
        "volume_number": candidate.get("volume_number") or candidate.get("volume"),
        "language": candidate.get("language"),
        "language_status": candidate.get("language_status"),
        "match_confidence": candidate.get("match_confidence"),
        "quality_profile": candidate.get("quality_profile") or candidate.get("quality"),
        "rights_status": candidate.get("rights_status"),
    }
    direct_metadata = {key: value for key, value in direct_metadata.items() if value not in (None, "", [], {})}
    retry_scope = "direct_download_handoff" if artifact_safe else "direct_artifact_verdict"
    import_expectation = _direct_import_handoff_expectation(artifact_safe)
    attempt = {
        "source": provider_id,
        "provider": provider_id,
        "provider_id": provider_id,
        "source_type": registry_row.get("provider_type") or candidate.get("provider_type") or "direct_download",
        "provider_mode": registry_row.get("source_mode"),
        "registry_state": registry_row.get("registry_state"),
        "risk_class": registry_row.get("risk_class"),
        "status": status,
        "reason": failure_reason,
        "failure_reason": failure_reason,
        "retry_eligible": not artifact_safe,
        "title": candidate.get("title"),
        "query": candidate.get("series_title") or candidate.get("title"),
        "unit_type": candidate.get("unit_type") or candidate.get("unitType"),
        "issue_number": candidate.get("issue_number") or candidate.get("issue"),
        "chapter_number": candidate.get("chapter_number") or candidate.get("chapter"),
        "volume_number": candidate.get("volume_number") or candidate.get("volume"),
        "candidate_identity": candidate.get("candidate_identity") or candidate_identity(candidate),
        "download_url_hash": candidate.get("download_url_hash"),
        "score": candidate.get("score"),
        "content_type": candidate.get("content_type"),
        "size_bytes": candidate.get("size_bytes"),
        "rights_status": candidate.get("rights_status"),
        "license_url": candidate.get("license_url"),
        "source_path": candidate.get("source_url") or candidate.get("url"),
        "direct_artifact_key": direct_metadata.get("artifact_key"),
        "direct_suppression_key": direct_metadata.get("suppression_key"),
        "language": candidate.get("language"),
        "language_status": candidate.get("language_status"),
        "match_confidence": candidate.get("match_confidence"),
        "quality_profile": candidate.get("quality_profile") or candidate.get("quality"),
        "quality_status": candidate.get("quality_status"),
        "retry_scope": retry_scope,
        "import_handoff_expectation": import_expectation,
        "artifact_safe": artifact_safe,
        "auto_grab_verdict": candidate.get("auto_grab_verdict"),
        "block_reasons": block_reasons,
        "raw": {
            "candidate": candidate,
            "direct_artifact": direct_metadata,
            "retry_scope": retry_scope,
            "import_handoff_expectation": import_expectation,
        },
    }
    if artifact_safe:
        task = direct_download_task_seed(candidate, staging_root, registry_row=registry_row)
        attempt.update(
            {
                "protocol": task["protocol"],
                "download_client": task["download_client"],
                "external_id": task["external_id"],
                "save_path": task["save_path"],
                "local_path": task["local_path"],
                "download_path": task["local_path"],
                "partial_path": task["partial_path"],
                "category": "inkdrop-direct",
            }
        )
        task_raw = task.get("raw_json") if isinstance(task.get("raw_json"), dict) else {}
        task_raw["direct_artifact"] = direct_metadata
        task_raw["import_handoff_expectation"] = import_expectation
        task["raw_json"] = task_raw
        attempt["raw"]["download_task_seed"] = task
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def _indexer_language_value(result, wanted_item=None):
    result = result if isinstance(result, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    value = first_value(
        result.get("language"),
        result.get("languages"),
        result.get("languageCode"),
        result.get("language_code"),
        wanted_item.get("language"),
        wanted_item.get("translated_language"),
    )
    values = text_values(value)
    text = values[0] if values else first_text(value)
    return str(text or "").strip().lower()


def _indexer_allowed_languages(policy, wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    wanted = text_values(first_value(wanted_item.get("language"), wanted_item.get("translated_language")))
    if wanted:
        return [str(item or "").strip().lower() for item in wanted if str(item or "").strip()]
    allowed = text_values((policy or {}).get("allowed_languages"))
    return [str(item or "").strip().lower() for item in allowed if str(item or "").strip()]


def _indexer_language_status(language, policy, wanted_item=None):
    allowed = set(_indexer_allowed_languages(policy, wanted_item))
    if not allowed:
        return "not_checked"
    language = str(language or "").strip().lower()
    if not language:
        return "unknown"
    if language in allowed:
        return "accepted"
    return "rejected"


def _indexer_quality_label(result):
    result = result if isinstance(result, dict) else {}
    explicit = first_text(
        result.get("quality"),
        result.get("qualityProfile"),
        result.get("quality_profile"),
        result.get("releaseQuality"),
        result.get("release_quality"),
    )
    if explicit:
        return normalized_query(explicit).lower()
    title = str(first_value(result.get("title"), result.get("releaseTitle"), result.get("name")) or "").lower()
    if "digital" in title:
        return "digital"
    if "web-dl" in title or "webdl" in title or "web rip" in title or "webrip" in title:
        return "web"
    if "scan" in title:
        return "scan"
    if "raw" in title:
        return "raw"
    return "unknown"


def _indexer_title_has_wanted_number(title, wanted_item=None):
    unit_metadata = _wanted_unit_metadata(wanted_item)
    wanted = first_text(
        unit_metadata.get("volume_number") if unit_metadata.get("unit_type") in {"volume", "vol", "book_volume", "manga_volume"} else "",
        unit_metadata.get("issue_number"),
        unit_metadata.get("chapter_number"),
        (wanted_item or {}).get("normalized_number"),
        (wanted_item or {}).get("chapter"),
        (wanted_item or {}).get("number"),
    )
    if not wanted:
        return False
    wanted_text = str(wanted or "").strip()
    title_text = str(title or "")
    try:
        number = int(float(wanted_text))
        return bool(re.search(rf"(?<!\d)0*{number}(?!\d)", title_text))
    except Exception:
        wanted_key = inkdrop_sources.normalize_title(wanted_text)
        title_key = inkdrop_sources.normalize_title(title_text)
        return bool(wanted_key and wanted_key in title_key)


def _query_without_trailing_year(query):
    text = normalized_query(query)
    return normalized_query(re.sub(r"\s+(?:19|20)\d{2}$", "", text))


def _query_without_leading_creator_possessive(query):
    text = normalized_query(query)
    if not text:
        return ""
    match = re.match(
        r"^([A-Z][A-Za-z0-9.\-]*(?:\s+[A-Z][A-Za-z0-9.\-]*){1,4})['\u2019]s\s+(.+)$",
        text,
    )
    if not match:
        return ""
    tail = normalized_query(match.group(2))
    if not tail or not re.search(r"[A-Za-z]", tail):
        return ""
    return tail


def _query_alias_values(value):
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        values = []
        for line in value.splitlines():
            for part in re.split(r"[,|]", line):
                text = normalized_query(part)
                if text:
                    values.append(text)
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_query_alias_values(item))
        return values
    return [normalized_query(value)]


def _query_alias_policy_entries(policy):
    policy = policy if isinstance(policy, dict) else {}
    entries = []
    for key in ("series_query_aliases", "query_aliases", "title_query_aliases", "series_title_aliases"):
        value = policy.get(key)
        if isinstance(value, dict):
            for match, aliases in value.items():
                match_text = normalized_query(match)
                alias_values = _query_alias_values(aliases)
                if match_text and alias_values:
                    entries.append((match_text, alias_values))
        elif isinstance(value, str):
            for line in value.splitlines():
                if "=>" in line:
                    match, aliases = line.split("=>", 1)
                elif "=" in line:
                    match, aliases = line.split("=", 1)
                else:
                    continue
                match_text = normalized_query(match)
                alias_values = _query_alias_values(aliases)
                if match_text and alias_values:
                    entries.append((match_text, alias_values))
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, dict):
                    match_text = normalized_query(
                        first_text(
                            item.get("series"),
                            item.get("series_title"),
                            item.get("title"),
                            item.get("query"),
                            item.get("match"),
                        )
                    )
                    alias_values = _query_alias_values(
                        first_value(
                            item.get("aliases"),
                            item.get("query_aliases"),
                            item.get("values"),
                            item.get("alias"),
                            item.get("value"),
                        )
                    )
                    if match_text and alias_values:
                        entries.append((match_text, alias_values))
                elif isinstance(item, str) and ("=>" in item or "=" in item):
                    separator = "=>" if "=>" in item else "="
                    match, aliases = item.split(separator, 1)
                    match_text = normalized_query(match)
                    alias_values = _query_alias_values(aliases)
                    if match_text and alias_values:
                        entries.append((match_text, alias_values))
    return entries


def _wanted_series_title_keys(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    values = [
        wanted_item.get("series_title"),
        wanted_item.get("series"),
        wanted_item.get("manga_title"),
        wanted_item.get("title"),
        first_text(wanted_item.get("searchQuery"), wanted_item.get("search_query"), wanted_item.get("query")),
    ]
    for value in list(values):
        values.append(_query_without_trailing_year(value))
        values.append(_query_without_leading_creator_possessive(value))
        values.append(_query_without_leading_creator_possessive(_query_without_trailing_year(value)))
    out = set()
    for value in values:
        key = inkdrop_sources.normalize_title(value)
        if key:
            out.add(key)
    return out


def _series_query_aliases(wanted_item=None, *, policy=None):
    wanted_keys = _wanted_series_title_keys(wanted_item)
    if not wanted_keys:
        return []
    aliases = []
    seen = set()
    series_title = first_text(
        (wanted_item or {}).get("series_title"),
        (wanted_item or {}).get("series"),
        (wanted_item or {}).get("manga_title"),
        (wanted_item or {}).get("title"),
    )
    for value in [
        series_title,
        *inkdrop_sources.collected_title_aliases(series_title),
        *inkdrop_sources.contributor_title_aliases(series_title),
    ]:
        alias = normalized_query(value)
        key = alias.lower()
        if alias and key not in seen:
            seen.add(key)
            aliases.append(alias)
    supplied_aliases = first_value(
        (wanted_item or {}).get("series_query_aliases"),
        (wanted_item or {}).get("query_aliases"),
        (wanted_item or {}).get("aliases"),
    ) if (wanted_item or {}).get("manual_search") else []
    for value in _query_alias_values(supplied_aliases):
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            aliases.append(value)
    for match, values in _query_alias_policy_entries(policy):
        if inkdrop_sources.normalize_title(match) not in wanted_keys:
            continue
        for value in values:
            alias = normalized_query(value)
            key = alias.lower()
            if alias and key not in seen:
                seen.add(key)
                aliases.append(alias)
    return aliases


def series_identity_aliases(wanted_item=None, *, policy=None):
    """Return only canonical or explicitly supplied titles safe for exact work identity."""
    identity_item = dict(wanted_item or {})
    identity_item["manual_search"] = True
    return _series_query_aliases(identity_item, policy=policy)


def indexer_outer_work_identity_matches(candidate, wanted_item=None, policy=None):
    """Require the provider's outer release title to name the wanted work."""
    candidate = candidate if isinstance(candidate, dict) else {}
    title = normalized_query(
        first_text(
            candidate.get("original_result_title"),
            candidate.get("title"),
            candidate.get("releaseTitle"),
            candidate.get("release_title"),
            candidate.get("name"),
        )
    ).lower()
    if not title:
        return False
    ignored = {"the", "an", "and", "of"}
    for alias in series_identity_aliases(wanted_item, policy=policy):
        if (
            _text_contains_query_tokens(alias, title, ignored=ignored)
            and _text_contains_numbered_series_sequence(alias, title, ignored=ignored)
        ):
            return True
    return False


def _wanted_has_number(wanted_item=None):
    unit_metadata = _wanted_unit_metadata(wanted_item)
    return bool(
        first_text(
            unit_metadata.get("issue_number"),
            unit_metadata.get("chapter_number"),
            unit_metadata.get("volume_number"),
        )
    )


def _collection_guard_norm(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _candidate_targets_collection(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    target_text = " ".join(
        first_text(candidate.get(key))
        for key in (
            "series_title",
            "wanted_series_title",
            "wanted_query",
            "query",
            "wanted_issue_title",
        )
        if first_text(candidate.get(key))
    )
    issue_title_norm = _collection_guard_norm(candidate.get("wanted_issue_title"))
    return bool(COLLECTION_TARGET_RE.search(target_text)) or issue_title_norm in COLLECTION_ISSUE_TITLE_HINTS


def _candidate_source_is_single_part_without_collection(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    source_text = " ".join(
        first_text(candidate.get(key))
        for key in (
            "title",
            "source_path",
            "url",
            "guid",
        )
        if first_text(candidate.get(key))
    )
    source_norm = _collection_guard_norm(source_text)
    if not source_norm:
        return False
    return bool(SINGLE_PART_SOURCE_RE.search(source_norm)) and not COLLECTION_TARGET_RE.search(source_text)


def _indexer_match_confidence(candidate, wanted_item=None, policy=None):
    if not _query_matches_result(candidate, wanted_item, policy=policy):
        return "mismatch"
    if _indexer_has_related_series_extension(candidate, wanted_item, policy=policy):
        return "related_series_identity"
    if _indexer_title_has_wanted_number((candidate or {}).get("title"), wanted_item):
        return "title_issue_match"
    if first_text(
        (wanted_item or {}).get("issue_number"),
        (wanted_item or {}).get("normalized_number"),
        (wanted_item or {}).get("chapter_number"),
        (wanted_item or {}).get("chapter"),
    ):
        return "series_title_only"
    return "title_match"


INDEXER_RELEASE_METADATA_TOKENS = {
    "cbr",
    "cbz",
    "c2c",
    "comic",
    "comics",
    "digital",
    "ebook",
    "eng",
    "english",
    "epub",
    "pdf",
    "retail",
    "scan",
    "web",
}
INDEXER_RELEASE_GROUP_PHRASES = tuple(
    sorted(
        {
            ("f", "son", "of", "ultron", "empire"),
            ("son", "of", "ultron", "empire"),
            ("f", "archangel", "zone", "empire"),
            ("archangel", "zone", "empire"),
            ("zone", "empire"),
            ("zerodaze", "dcp", "hd"),
            ("minutemen", "phd"),
            ("dc", "comics"),
            ("marvel", "comics"),
            ("image", "comics"),
            ("lostnerevarine", "empire"),
        },
        key=len,
        reverse=True,
    )
)
INDEXER_PARENTHESIZED_RELEASE_GROUPS = {
    ("1r0n",),
    ("jko",),
    ("lucaz",),
    ("oda",),
    ("rillant",),
}
INDEXER_UNIT_MARKER_TOKENS = {
    "book",
    "ch",
    "chap",
    "chapter",
    "issue",
    "no",
    "number",
    "part",
    "pt",
    "v",
    "vol",
    "volume",
}
INDEXER_COMPACT_UNIT_TOKEN_RE = re.compile(
    r"(?i)^(?:v|vol|volume|book|ch|chap|chapter|issue|no|number|part|pt)0*\d+(?:\.\d+)?$"
)
INDEXER_RELEASE_TOKEN_RE = re.compile(r"(?i)^(?:\d{3,4}p(?:x)?|\d+x\d+|\d+bit|r\d+)$")
INDEXER_LEADING_RELEASE_GROUP_RE = re.compile(r"^\s*(?:\[[^\[\]]{1,40}\]\s*)+")


def _indexer_release_phrase_end(tokens, index):
    for phrase in INDEXER_RELEASE_GROUP_PHRASES:
        if tuple(tokens[index : index + len(phrase)]) == phrase:
            return index + len(phrase)
    return index


def _indexer_title_without_safe_parenthesized_release_suffix(candidate, wanted_item=None):
    """Strip only a structurally bounded release suffix after an exact wanted unit."""
    title = str((candidate or {}).get("title") or "").strip()
    match = re.match(
        r"^(?P<base>.+?)(?P<suffix>(?:\s*\([^()]+\))+)(?:\.(?:cbz|cbr|pdf|epub|zip|rar|7z))?$",
        title,
        re.I,
    )
    if not match:
        return title
    base = match.group("base").strip()
    if not _indexer_title_has_wanted_number(base, wanted_item):
        return title
    groups = [value.strip() for value in re.findall(r"\(([^()]*)\)", match.group("suffix")) if value.strip()]
    if not groups:
        return title

    def known_metadata(value):
        tokens = _query_tokens_with_numbers(value, ignored=set())
        return bool(
            re.fullmatch(r"(?:19|20)\d{2}", value)
            or (tokens and all(token in INDEXER_RELEASE_METADATA_TOKENS or INDEXER_RELEASE_TOKEN_RE.fullmatch(token) for token in tokens))
        )

    def allowed_release_group(value):
        tokens = tuple(_query_tokens_with_numbers(value, ignored=set()))
        return bool(tokens and (tokens in INDEXER_RELEASE_GROUP_PHRASES or tokens in INDEXER_PARENTHESIZED_RELEASE_GROUPS))

    if all(known_metadata(value) for value in groups):
        return base
    if all(known_metadata(value) for value in groups[:-1]) and allowed_release_group(groups[-1]):
        return base
    return title


def _indexer_title_without_leading_release_group(title):
    """Drop a bracketed uploader/release-group prefix ahead of the work title.

    ``[Pajeet] Vagabond Volume 01-37`` names the group that packaged the
    release, not a different book. Square-bracket prefixes are how Nyaa and
    Soulseek uploaders label themselves, and several real accepted packs
    arrive this way, so the leading-word rule below has to read past them
    instead of treating "Pajeet" as the start of another series' name.
    """
    text = str(title or "")
    stripped = INDEXER_LEADING_RELEASE_GROUP_RE.sub("", text).strip()
    return stripped or text


def _indexer_has_related_series_extension(candidate, wanted_item=None, policy=None):
    """Detect child-series words appended after a trusted wanted identity.

    Broad discovery is intentional, so containment alone is insufficient for
    unattended handoff. Only recognized release metadata may precede the
    longest matching alias. After it, supported unit/year/release syntax is
    consumed and any remaining word is treated as a related identity.
    """
    identity_title = _indexer_title_without_leading_release_group(
        _indexer_title_without_safe_parenthesized_release_suffix(candidate, wanted_item)
    )
    title_tokens = _query_tokens_with_numbers(identity_title, ignored=set())
    if not title_tokens:
        return False
    aliases = [
        first_text(
            (wanted_item or {}).get("series_title"),
            (wanted_item or {}).get("series"),
            (wanted_item or {}).get("manga_title"),
            (wanted_item or {}).get("title"),
        ),
        *_series_query_aliases(wanted_item, policy=policy),
    ]
    matches = []
    for alias in aliases:
        alias_tokens = _query_tokens_with_numbers(alias, ignored=set())
        if not alias_tokens or len(alias_tokens) > len(title_tokens):
            continue
        for index in range(len(title_tokens) - len(alias_tokens) + 1):
            if title_tokens[index : index + len(alias_tokens)] == alias_tokens:
                matches.append((len(alias_tokens), index, index + len(alias_tokens)))
                break
    if not matches:
        return False
    alias_length, alias_start, alias_end = max(matches)
    leading = title_tokens[:alias_start]
    leading_index = 0
    while leading_index < len(leading):
        phrase_end = _indexer_release_phrase_end(leading, leading_index)
        if phrase_end > leading_index:
            leading_index = phrase_end
            continue
        token = leading[leading_index]
        if token in INDEXER_RELEASE_METADATA_TOKENS or INDEXER_RELEASE_TOKEN_RE.fullmatch(token):
            leading_index += 1
            continue
        return True
    if alias_length < 2:
        # A one-word series name ("Batman", "Watchmen", "Die") turns up inside
        # far too much unrelated release prose to police what follows it --
        # real accepted packs trail things like "& Chapters 323-327 + Art
        # Books Hiatus". The leading rule above is the half that carries its
        # weight here: a different series built by prefixing the wanted name
        # ("Absolute Batman" for Batman, "Before Watchmen" for Watchmen,
        # "Who Is Miracleman" for Miracleman) is caught, and nothing that
        # merely comes after a one-word title is second-guessed. This mirrors
        # the SLSKD probe, which likewise only requires a one-word title to
        # start its own title segment.
        return False
    tail = title_tokens[alias_end:]
    index = 0
    while index < len(tail):
        phrase_end = _indexer_release_phrase_end(tail, index)
        if phrase_end > index:
            index = phrase_end
            continue
        token = tail[index]
        if token.isdigit() or INDEXER_COMPACT_UNIT_TOKEN_RE.fullmatch(token):
            index += 1
            continue
        if token in INDEXER_UNIT_MARKER_TOKENS:
            index += 1
            if index < len(tail) and re.fullmatch(r"\d+(?:\.\d+)?", tail[index]):
                index += 1
            continue
        if token in INDEXER_RELEASE_METADATA_TOKENS or INDEXER_RELEASE_TOKEN_RE.fullmatch(token):
            index += 1
            continue
        return True
    return False


def _indexer_download_client(protocol, candidate=None, policy=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    policy = policy if isinstance(policy, dict) else {}
    explicit = first_text(candidate.get("download_client"), candidate.get("downloadClient"))
    if explicit:
        return explicit
    mapping = policy.get("download_client_by_protocol")
    if isinstance(mapping, dict):
        value = first_text(mapping.get(protocol), mapping.get(str(protocol or "").lower()))
        if value:
            return value
    if protocol == "torrent":
        return first_text(policy.get("torrent_download_client"), INDEXER_DOWNLOAD_CLIENT_BY_PROTOCOL.get(protocol))
    if protocol == "usenet":
        return first_text(policy.get("usenet_download_client"), INDEXER_DOWNLOAD_CLIENT_BY_PROTOCOL.get(protocol))
    return first_text(INDEXER_DOWNLOAD_CLIENT_BY_PROTOCOL.get(protocol))


def _indexer_import_handoff_expectation(protocol, safe=False):
    if not safe:
        return "none_until_source_mode_auto_and_candidate_safe"
    client = INDEXER_DOWNLOAD_CLIENT_BY_PROTOCOL.get(protocol) or "download_client"
    return f"{client}_then_import_ready_worker_then_kavita_import_verification"


def _prowlarr_age_seconds(result):
    result = result if isinstance(result, dict) else {}
    exact = int_value(first_value(result.get("ageSeconds"), result.get("age_seconds")), None)
    if exact is not None:
        return max(0, exact)
    minutes = int_value(first_value(result.get("ageMinutes"), result.get("age_minutes")), None)
    if minutes is not None:
        return max(0, minutes) * 60
    hours = int_value(first_value(result.get("ageHours"), result.get("age_hours")), None)
    if hours is not None:
        return max(0, hours) * 3600
    days = int_value(first_value(result.get("ageDays"), result.get("age_days"), result.get("age")), None)
    return max(0, days) * 86400 if days is not None else None


def prowlarr_candidate_from_result(result, registry_row=None, wanted_item=None):
    result = result if isinstance(result, dict) else {}
    original_download_url = str(first_value(result.get("downloadUrl"), result.get("download_url"), result.get("download")) or "").strip()
    nested_credentials_redacted = prowlarr_url_has_nested_credentials(original_download_url)
    result = prowlarr_durable_payload(result)
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    provider_id = inkdrop_sources.provider_key(registry_row.get("provider_id") or "prowlarr")
    original_result_title = str(
        first_value(
            result.get("title"),
            result.get("releaseTitle"),
            result.get("release_title"),
            result.get("name"),
            wanted_item.get("title"),
        )
        or ""
    ).strip()
    title = normalized_query(original_result_title)
    download_url = str(first_value(result.get("downloadUrl"), result.get("download_url"), result.get("download")) or "").strip()
    magnet_url = str(first_value(result.get("magnetUrl"), result.get("magnet_url"), result.get("magnet")) or "").strip()
    info_hash = str(first_value(result.get("infoHash"), result.get("info_hash"), result.get("hash")) or "").strip()
    protocol = normalize_protocol(first_value(result.get("protocol"), result.get("downloadProtocol")))
    if not protocol and (magnet_url or info_hash):
        protocol = "torrent"
    locator_ext = normalize_extension(download_url or title)
    if not protocol and locator_ext == ".nzb":
        protocol = "usenet"
    elif not protocol and locator_ext == ".torrent":
        protocol = "torrent"
    categories = first_value(result.get("categories"), result.get("category"), result.get("categoryIds"), result.get("category_ids"))
    category_tokens = category_ids(categories)
    indexer = str(first_value(result.get("indexer"), result.get("indexerName"), result.get("indexer_name")) or "").strip()
    indexer_id = str(first_value(result.get("indexerId"), result.get("indexer_id"), result.get("indexer_id_int")) or "").strip()
    guid = str(first_value(result.get("guid"), result.get("id")) or "").strip()
    source_path = str(first_value(result.get("infoUrl"), result.get("info_url"), result.get("detailsUrl"), download_url, magnet_url) or "").strip()
    language = _indexer_language_value(result, wanted_item)
    language_status = _indexer_language_status(language, policy, wanted_item)
    quality_profile = _indexer_quality_label(result)
    unit_metadata = _wanted_unit_metadata(wanted_item)
    candidate = {
        "candidate_contract_version": 1,
        "provider_id": provider_id,
        "source": provider_id,
        "provider_type": registry_row.get("provider_type") or "indexer",
        "source_kind": registry_row.get("source_kind") or "prowlarr_indexer",
        "unit_type": unit_metadata.get("unit_type"),
        "issue_number": unit_metadata.get("issue_number"),
        "chapter_number": unit_metadata.get("chapter_number"),
        "volume_number": unit_metadata.get("volume_number"),
        "wanted_series_title": normalized_query(first_value(wanted_item.get("series"), wanted_item.get("series_title"), wanted_item.get("manga_title")) or ""),
        "wanted_issue_title": normalized_query(first_text(wanted_item.get("issue_title"), wanted_item.get("issueTitle"))),
        "wanted_query": normalized_query(first_text(wanted_item.get("query"), wanted_item.get("searchQuery"), wanted_item.get("search_query"))),
        "metadata_provider": first_text(wanted_item.get("metadata_provider"), wanted_item.get("provider")),
        "series_source": first_text(wanted_item.get("series_source"), wanted_item.get("source")),
        "media_type": first_text(wanted_item.get("media_type")),
        "publisher": first_text(wanted_item.get("publisher"), wanted_item.get("watch_publisher"), wanted_item.get("series_publisher")),
        "issue_title": normalized_query(first_text(wanted_item.get("issue_title"), wanted_item.get("issueTitle"), wanted_item.get("title"))),
        "title": title,
        "original_result_title": original_result_title,
        "series_title": normalized_query(first_value(wanted_item.get("series"), wanted_item.get("series_title"), title) or ""),
        "url": source_path,
        "source_path": source_path,
        "download_url": download_url,
        "magnet_url": magnet_url,
        "download_url_hash": url_hash(download_url or magnet_url or info_hash or guid),
        "prowlarr_locator_credentials_redacted": bool(download_url),
        "prowlarr_nested_credentials_redacted": nested_credentials_redacted,
        "guid": guid,
        "info_hash": info_hash,
        "protocol": protocol,
        "download_client": str(first_value(result.get("download_client"), result.get("downloadClient")) or "").strip(),
        "indexer": indexer,
        "indexer_id": indexer_id,
        "categories": categories if isinstance(categories, list) else ([categories] if categories not in (None, "") else []),
        "category_ids": category_tokens,
        "category": category_tokens[0] if category_tokens else "",
        "seeders": int_value(first_value(result.get("seeders"), result.get("seedCount"), result.get("seeds"))),
        "leechers": int_value(first_value(result.get("leechers"), result.get("leechCount"))),
        "peers": int_value(first_value(result.get("peers"), result.get("peerCount"), result.get("peersCount"))),
        "size_bytes": int_value(first_value(result.get("size"), result.get("size_bytes"), result.get("sizeBytes"))),
        "age": int_value(result.get("age"), None),
        "age_seconds": _prowlarr_age_seconds(result),
        "publish_date": str(first_value(result.get("publishDate"), result.get("publish_date")) or "").strip(),
        "extension": _indexer_artifact_extension(result.get("extension"), title, download_url),
        "summary": first_text(result.get("summary"), result.get("description")),
        "description": first_text(result.get("description"), result.get("summary")),
        "files": first_value(result.get("files"), result.get("file_list"), result.get("fileList"), result.get("pack_detail_entries")),
        "pack_detail_entries": first_value(result.get("pack_detail_entries"), result.get("packDetailEntries")),
        "language": language,
        "language_status": language_status,
        "quality_profile": quality_profile,
        "quality": quality_profile,
        "score": int_value(result.get("score"), 0),
        "pack": looks_pack_like(title),
        "query_variant": str(result.get("_inkdrop_query_variant") or "").strip(),
        "query_group": str(result.get("_inkdrop_query_group") or "").strip(),
        "request_id": str(result.get("_inkdrop_request_id") or "").strip(),
        "query_ordinal": int_value(result.get("_inkdrop_query_index"), None),
        "categoryless_fallback": bool(result.get("_inkdrop_categoryless_fallback")),
        "categoryless_fallback_primary_request_id": str(
            result.get("_inkdrop_categoryless_fallback_primary_request_id") or ""
        ).strip(),
        "raw": {"result": result},
        "candidate_safe": False,
        "auto_grab_verdict": "review",
        "review_reason": "",
    }
    aliases = _series_query_aliases(wanted_item, policy=policy)
    if aliases:
        candidate["series_query_aliases"] = aliases
    candidate["match_confidence"] = _indexer_match_confidence(candidate, wanted_item, policy=policy)
    manifest_match = indexer_manifest_pack_match(candidate)
    if manifest_match:
        candidate["pack_contents_match"] = manifest_match
        candidate["pack_contents_coverage_source"] = manifest_match.get("coverage_source")
        candidate["pack_contents_matching_entry"] = manifest_match.get("entry")
        candidate["pack_contents_entry_count"] = manifest_match.get("content_entry_count")
    candidate["indexer_candidate_key"] = indexer_candidate_key(candidate)
    candidate["indexer_suppression_key"] = candidate["indexer_candidate_key"]
    candidate["candidate_identity"] = indexer_candidate_identity(candidate)
    return candidate


def prowlarr_candidates_from_results(results, registry_row=None, wanted_item=None, limit=20):
    rows = results if isinstance(results, list) else []
    policy = provider_policy(registry_row)
    out = []
    for result in rows:
        if isinstance(result, dict):
            candidate = prowlarr_candidate_from_result(result, registry_row, wanted_item)
            if _candidate_result_relevant(candidate, wanted_item, policy=policy):
                out.append(candidate)
                if len(out) >= max(0, int(limit or 0)):
                    break
    return out


SECRETISH_QUERY_PARAM_RE = re.compile(r"(?i)(?:^|[?&;])(api[_-]?key|apikey|passkey|token|auth|rsskey|key)=")


def _url_has_secretish_query(value):
    url = str(value or "")
    return bool(SECRETISH_QUERY_PARAM_RE.search(url))


def authorized_prowlarr_download_url(candidate, registry_row=None):
    """Return a persisted Prowlarr acquisition URL only when its authority is intact."""
    candidate = candidate if isinstance(candidate, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    provider_id = inkdrop_sources.provider_key(
        registry_row.get("provider_id") or candidate.get("provider_id") or candidate.get("source")
    )
    download_url = first_text(candidate.get("download_url"), candidate.get("downloadUrl"))
    base_url = first_text(registry_row.get("base_url"))
    expected_hash = first_text(candidate.get("download_url_hash"), candidate.get("downloadUrlHash"))
    if (
        not provider_id.startswith("prowlarr_")
        or candidate.get("prowlarr_nested_credentials_redacted") is True
        or not download_url
        or not base_url
        or not expected_hash
        or expected_hash != url_hash(download_url)
        or re.search(r"\s", download_url)
    ):
        return ""
    try:
        parsed = urlparse(download_url)
        base = urlparse(base_url)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or base.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or not base.hostname
            or parsed.username
            or parsed.password
        ):
            return ""
        parsed_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        base_port = base.port or (443 if base.scheme.lower() == "https" else 80)
        if (
            parsed.scheme.lower() != base.scheme.lower()
            or parsed.hostname.lower().rstrip(".") != base.hostname.lower().rstrip(".")
            or parsed_port != base_port
        ):
            return ""
        base_path = (base.path or "").rstrip("/")
        download_path = re.escape(base_path) + r"/api/v\d+/indexer/[^/]+/download/?"
        if not re.fullmatch(download_path, parsed.path or "", flags=re.IGNORECASE):
            return ""
        query = {}
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            query.setdefault(str(key).strip().lower(), []).append(str(value or "").strip())

        def query_has_value(*keys):
            return any(value for key in keys for value in query.get(key, []) if value)

        has_api_key = query_has_value("apikey", "api_key", "api-key")
        credential_free = candidate.get("prowlarr_locator_credentials_redacted") is True
        if not has_api_key and not credential_free:
            return ""
        if not query_has_value("link") or not query_has_value("file"):
            return ""
        if any(value.lower().startswith(("http://", "https://")) for value in query.get("link", [])):
            return ""
    except (TypeError, ValueError):
        return ""
    return download_url


def _magnet_info_hash(value):
    match = re.search(r"(?i)(?:urn:btih:|btih:)([a-z0-9]{16,64})", str(value or ""))
    return match.group(1) if match else ""


TORRENT_INFO_HASH_LABEL_RE = re.compile(
    r"(?i)\b(?:info\s*hash|infohash|torrent\s*hash|btih)\b\s*[:#=\-]?\s*(?:urn:btih:)?([a-z0-9]{32,64})\b"
)


def _visible_text_from_html(value):
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", str(value or ""))
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    try:
        text = html.unescape(text)
    except Exception:
        pass
    return re.sub(r"\s+", " ", text).strip()


def _seeders_near_text(text, start, end):
    window = str(text or "")[max(0, int(start or 0) - 200) : int(end or 0) + 200]
    match = re.search(r"(?i)\b(?:seeders?|seeds)\b\D{0,24}(\d{1,7})\b", window)
    return int_value(match.group(1), None) if match else None


def _xml_attr_rows(element):
    rows = {}
    for child in list(element):
        tag = str(child.tag or "").split("}", 1)[-1].lower()
        if tag != "attr":
            continue
        name = str(child.attrib.get("name") or "").strip().lower()
        value = str(child.attrib.get("value") or "").strip()
        if not name or not value:
            continue
        rows.setdefault(name, []).append(value)
    return rows


def _xml_attr_first(attrs, *names):
    for name in names:
        values = attrs.get(str(name or "").lower())
        if values:
            return values[0]
    return ""


def _xml_enclosure(element):
    for child in list(element):
        tag = str(child.tag or "").split("}", 1)[-1].lower()
        if tag == "enclosure":
            return dict(child.attrib or {})
    return {}


def _torznab_result_from_item(element, registry_row=None):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    attrs = _xml_attr_rows(element)
    title = _xml_child_text(element, {"title"})
    link = _rss_link(element)
    guid = _xml_child_text(element, {"guid", "id"})
    details_url = _xml_child_text(element, {"comments"}) or link
    published = _xml_child_text(element, {"pubdate", "published", "updated"})
    summary = _xml_child_text(element, {"description", "summary", "content", "encoded"})
    enclosure = _xml_enclosure(element)
    enclosure_url = str(enclosure.get("url") or "").strip()
    enclosure_size = str(enclosure.get("length") or "").strip()
    embedded_rows = _torrent_embedded_locator_rows(
        summary,
        source_url=details_url or link,
        source_site=registry_row.get("display_name") or registry_row.get("provider_id") or "Torrent RSS",
        policy=provider_policy(registry_row),
        context_title=title,
    )
    embedded = embedded_rows[0] if embedded_rows else {}
    magnet_url = _xml_attr_first(attrs, "magneturl", "magnet_uri", "magnet")
    if link.startswith("magnet:") and not magnet_url:
        magnet_url = link
    if not magnet_url:
        magnet_url = first_text(embedded.get("magnetUrl"), embedded.get("magnet_url"))
    info_hash = (
        _xml_attr_first(attrs, "infohash", "info_hash", "hash")
        or _magnet_info_hash(magnet_url or link)
        or first_text(embedded.get("infoHash"), embedded.get("info_hash"))
    )
    download_url = _xml_attr_first(attrs, "downloadurl", "download_url", "download")
    download_url = download_url or first_text(embedded.get("downloadUrl"), embedded.get("download_url"))
    link_download_url = "" if link.startswith("magnet:") else link
    if link_download_url and normalize_extension(link_download_url) not in {".torrent", ".nzb"} and (embedded or magnet_url or info_hash):
        link_download_url = ""
    download_url = download_url or link_download_url or enclosure_url
    download_url_redacted = False
    if _url_has_secretish_query(download_url):
        download_url = ""
        download_url_redacted = True
    if _url_has_secretish_query(details_url):
        details_url = ""
    categories = []
    categories.extend(attrs.get("category") or [])
    categories.extend(attrs.get("categoryid") or [])
    category_text = _xml_child_text(element, {"category"})
    if category_text:
        categories.append(category_text)
    seeders = _xml_attr_first(attrs, "seeders", "seeds") or _xml_child_text(element, {"seeders", "seeds"}) or embedded.get("seeders")
    leechers = _xml_attr_first(attrs, "leechers", "peers") or _xml_child_text(element, {"leechers", "peers"})
    size = _xml_attr_first(attrs, "size", "filesize", "length") or _xml_child_text(element, {"size", "filesize", "length"}) or embedded.get("size") or enclosure_size
    protocol = "torrent"
    if normalize_extension(download_url or enclosure_url or title) == ".nzb":
        protocol = "usenet"
    result = {
        "title": title,
        "protocol": protocol,
        "indexer": registry_row.get("display_name") or registry_row.get("provider_id") or "Torznab",
        "indexerId": registry_row.get("indexer_id") or "",
        "categories": categories,
        "seeders": seeders,
        "leechers": leechers,
        "size": size,
        "publishDate": published,
        "guid": guid or url_hash(link or magnet_url or title),
        "infoUrl": details_url,
        "downloadUrl": download_url,
        "magnetUrl": magnet_url,
        "infoHash": info_hash,
        "extension": normalize_extension(first_value(title, download_url, enclosure_url)),
    }
    if summary:
        result["summary"] = summary
        result["description"] = summary
        result["files"] = summary
    if download_url_redacted:
        result["downloadUrlRedacted"] = True
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def _torznab_rows_from_xml(text, registry_row=None):
    text = str(text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    rows = []
    for element in root.iter():
        tag = str(element.tag or "").split("}", 1)[-1].lower()
        if tag in {"item", "entry"}:
            rows.append(_torznab_result_from_item(element, registry_row))
    return rows


def indexer_result_rows_from_payload(payload, registry_row=None):
    if isinstance(payload, dict):
        rows = payload.get("results")
        if not isinstance(rows, list):
            rows = payload.get("items")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return _torznab_rows_from_xml(_payload_text(payload), registry_row)


def torznab_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    if isinstance(payload, dict):
        rows = payload.get("results")
        if isinstance(rows, list):
            return prowlarr_candidates_from_results(rows, registry_row, wanted_item, limit=limit)
    rows = _torznab_rows_from_xml(_payload_text(payload), registry_row)
    return prowlarr_candidates_from_results(rows, registry_row, wanted_item, limit=limit)


def newznab_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    policy = provider_policy(registry_row)
    if isinstance(payload, dict):
        rows = payload.get("results")
        if not isinstance(rows, list):
            rows = payload.get("items")
        rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    elif isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
    else:
        rows = _torznab_rows_from_xml(_payload_text(payload), registry_row)
    out = []
    for row in rows:
        row = dict(row)
        if not _candidate_result_relevant(row, wanted_item, policy=policy):
            continue
        download_url = first_text(row.get("downloadUrl"), row.get("download_url"), row.get("download"), row.get("link"))
        protocol = normalize_protocol(first_value(row.get("protocol"), row.get("downloadProtocol")))
        locator_ext = normalize_extension(first_value(row.get("extension"), download_url, row.get("title")))
        if not protocol and locator_ext == ".nzb":
            protocol = "usenet"
        if protocol and protocol != "usenet":
            continue
        if not protocol:
            continue
        if download_url and _url_has_secretish_query(download_url):
            row["guid"] = first_text(row.get("guid"), row.get("id"), url_hash(download_url))
            for key in ("downloadUrl", "download_url", "download", "link"):
                row.pop(key, None)
            row["downloadUrlRedacted"] = True
        row["protocol"] = "usenet"
        row["downloadProtocol"] = "usenet"
        row["indexer"] = first_text(row.get("indexer"), row.get("indexerName"), registry_row.get("display_name"), registry_row.get("provider_id"), "Newznab")
        out.append(prowlarr_candidate_from_result(row, registry_row, wanted_item))
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def torrent_rss_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    policy = provider_policy(registry_row)
    if isinstance(payload, dict):
        rows = payload.get("results")
        if not isinstance(rows, list):
            rows = payload.get("items")
        rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    else:
        rows = _torznab_rows_from_xml(_payload_text(payload), registry_row)
    out = []
    for row in rows:
        row = dict(row)
        if not _candidate_result_relevant(row, wanted_item, policy=policy):
            continue
        protocol = normalize_protocol(first_value(row.get("protocol"), row.get("downloadProtocol")))
        if not protocol and (row.get("magnetUrl") or row.get("magnet_url") or row.get("infoHash") or row.get("info_hash")):
            protocol = "torrent"
        if protocol and protocol != "torrent":
            continue
        locator = first_text(row.get("downloadUrl"), row.get("download_url"), row.get("magnetUrl"), row.get("magnet_url"), row.get("infoHash"), row.get("info_hash"), row.get("guid"), row.get("id"))
        if not locator:
            continue
        if first_text(row.get("downloadUrl"), row.get("download_url")) and _url_has_secretish_query(first_text(row.get("downloadUrl"), row.get("download_url"))):
            continue
        row["protocol"] = "torrent"
        row["indexer"] = first_text(row.get("indexer"), row.get("indexerName"), registry_row.get("display_name"), registry_row.get("provider_id"), "Torrent RSS")
        out.append(prowlarr_candidate_from_result(row, registry_row, wanted_item))
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def torrent_html_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    policy = provider_policy(registry_row)
    if isinstance(payload, dict):
        rows = payload.get("results")
        if not isinstance(rows, list):
            rows = payload.get("items")
        if isinstance(rows, list):
            rows = [row for row in rows if isinstance(row, dict)]
        else:
            rows = _torrent_html_rows_from_html(
                _payload_text(payload),
                _payload_source_url(payload, ""),
                first_text(policy.get("source_site_label"), registry_row.get("display_name"), registry_row.get("provider_id"), "Torrent HTML search"),
                policy=policy,
            )
    else:
        rows = _torrent_html_rows_from_html(
            _payload_text(payload),
            "",
            first_text(policy.get("source_site_label"), registry_row.get("display_name"), registry_row.get("provider_id"), "Torrent HTML search"),
            policy=policy,
        )
    out = []
    for row in rows:
        row = dict(row)
        if not _candidate_result_relevant(row, wanted_item, policy=policy):
            continue
        protocol = normalize_protocol(first_value(row.get("protocol"), row.get("downloadProtocol")))
        if not protocol and (row.get("magnetUrl") or row.get("magnet_url") or row.get("infoHash") or row.get("info_hash")):
            protocol = "torrent"
        if protocol and protocol != "torrent":
            continue
        magnet_url = first_text(row.get("magnetUrl"), row.get("magnet_url"), row.get("magnet"))
        download_url = first_text(row.get("downloadUrl"), row.get("download_url"), row.get("download"))
        if magnet_url and _url_has_secretish_query(magnet_url):
            continue
        if download_url:
            if _url_has_secretish_query(download_url):
                continue
            if normalize_extension(download_url) != ".torrent":
                continue
        locator = first_text(magnet_url, download_url, row.get("infoHash"), row.get("info_hash"), row.get("guid"), row.get("id"))
        if not locator:
            continue
        if not row.get("categories") and not row.get("category") and not row.get("categoryIds") and not row.get("category_ids"):
            source_categories = category_ids(policy.get("categories") or policy.get("comic_categories") or [])
            if source_categories:
                row["categories"] = source_categories
        row["protocol"] = "torrent"
        row["indexer"] = first_text(row.get("indexer"), row.get("indexerName"), registry_row.get("display_name"), registry_row.get("provider_id"), "Torrent HTML search")
        out.append(prowlarr_candidate_from_result(row, registry_row, wanted_item))
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def torrent_detail_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    source_site = first_text(
        policy.get("source_site_label"),
        registry_row.get("display_name"),
        registry_row.get("provider_id"),
        "Torrent detail search",
    )
    pages = []
    if isinstance(payload, dict):
        pages.extend(page for page in payload.get("search_pages") or [] if isinstance(page, dict))
        pages.extend(page for page in payload.get("detail_pages") or [] if isinstance(page, dict))
        pages.extend(page for page in payload.get("pages") or [] if isinstance(page, dict))
    if not pages:
        pages = [payload]

    rows = []
    seen = set()
    for page in pages:
        source_url = _payload_source_url(page, _payload_source_url(payload, ""))
        page_text = _payload_text(page)
        link_context_title = _direct_file_probe_context_title(page, wanted_item)
        hash_context_title = _torrent_detail_context_title(page)
        page_rows = _torrent_html_rows_from_html(page_text, source_url, source_site, policy=policy)
        page_rows.extend(
            _torrent_info_hash_rows_from_text(
                page_text,
                source_url,
                source_site,
                policy=policy,
                context_title=hash_context_title,
            )
        )
        for row in page_rows:
            row = dict(row)
            title = normalized_query(row.get("title"))
            if not title or title.lower() in GENERIC_TORRENT_LINK_TITLES or title.lower() == "torrent candidate":
                row["title"] = link_context_title
            locator = first_text(
                row.get("magnetUrl"),
                row.get("magnet_url"),
                row.get("downloadUrl"),
                row.get("download_url"),
                row.get("infoHash"),
                row.get("info_hash"),
                row.get("guid"),
            )
            identity = (
                _magnet_info_hash(first_text(row.get("magnetUrl"), row.get("magnet_url")))
                or first_text(row.get("infoHash"), row.get("info_hash"))
                or url_hash(locator)
            )
            if not identity or identity in seen:
                continue
            seen.add(identity)
            if not _candidate_result_relevant(row, wanted_item, policy=policy):
                continue
            rows.append(row)
            if len(rows) >= max(0, int(limit or 0)):
                break
        if len(rows) >= max(0, int(limit or 0)):
            break
    return torrent_html_candidates_from_payload({"results": rows}, registry_row, wanted_item, limit=limit)


def _discovery_result_rows(payload):
    if isinstance(payload, dict):
        for key in ("results", "items", "candidates", "matches"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def indexer_discovery_cards_from_results(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    out = []
    for result in _discovery_result_rows(payload):
        title = normalized_query(first_text(result.get("title"), result.get("releaseTitle"), result.get("release_title"), result.get("name")))
        indexer = first_text(result.get("indexer"), result.get("indexerName"), result.get("indexer_name"), registry_row.get("display_name"))
        protocol = normalize_protocol(first_value(result.get("protocol"), result.get("downloadProtocol")))
        info_hash = first_text(result.get("infoHash"), result.get("info_hash"), result.get("hash"))
        guid = first_text(result.get("guid"), result.get("id"))
        seeders = int_value(first_value(result.get("seeders"), result.get("seedCount"), result.get("seeds")), None)
        size_bytes = int_value(first_value(result.get("size"), result.get("size_bytes"), result.get("sizeBytes")), None)
        categories = category_ids(first_value(result.get("categories"), result.get("category"), result.get("categoryIds"), result.get("category_ids")))
        locator_present = bool(info_hash or guid or result.get("downloadUrl") or result.get("magnetUrl"))
        locator_hash = url_hash(first_text(info_hash, guid, result.get("downloadUrl"), result.get("magnetUrl")))
        card = {
            "title": title,
            "site": indexer,
            "source_site": indexer,
            "source": registry_row.get("provider_id") or "indexer_discovery",
            "url": first_text(result.get("infoUrl"), result.get("info_url"), result.get("detailsUrl"), result.get("details_url")),
            "guid": f"locator:{locator_hash[:16]}" if locator_hash else inkdrop_sources.stable_id("indexer_discovery", indexer, title),
            "result_id": f"locator:{locator_hash[:16]}" if locator_hash else "",
            "extension": normalize_extension(first_value(result.get("extension"), title)),
            "size_bytes": size_bytes,
            "score": int_value(result.get("score"), 0),
            "description": normalized_query(
                " ".join(
                    part
                    for part in (
                        protocol,
                        f"seeders {seeders}" if seeders is not None else "",
                        f"categories {','.join(categories)}" if categories else "",
                        "download locator withheld" if locator_present else "",
                    )
                    if part
                )
            ),
            "protocol": protocol,
            "seeders": seeders,
            "indexer_id": first_text(result.get("indexerId"), result.get("indexer_id")),
            "category_ids": categories,
            "download_locator_present": locator_present,
            "raw": {
                "indexer": indexer,
                "protocol": protocol,
                "seeders": seeders,
                "size_bytes": size_bytes,
                "category_ids": categories,
                "info_hash_present": bool(info_hash),
                "guid_present": bool(guid),
                "download_locator_present": locator_present,
            },
        }
        if not _query_matches_result(card, wanted_item, policy=provider_policy(registry_row)):
            continue
        out.append(
            manual_source_card_from_result(
                card,
                registry_row,
                wanted_item,
                source_bucket=registry_row.get("provider_id") or "indexer_discovery",
            )
        )
        out[-1]["seeders"] = seeders
        out[-1]["protocol"] = protocol
        out[-1]["indexer_id"] = card["indexer_id"]
        out[-1]["category_ids"] = categories
        out[-1]["download_locator_present"] = locator_present
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def _indexer_policy_string_values(value):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        values = []
        for line in value.splitlines():
            for part in re.split(r"[,|]", line):
                text = str(part or "").strip()
                if text:
                    values.append(text)
        return values
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


def _categoryless_fallback_category_gate_allowed(candidate, policy):
    if not candidate.get("categoryless_fallback"):
        return False
    indexer_id = str(candidate.get("indexer_id") or "").strip().lower()
    if not indexer_id:
        return False
    values = []
    for key in (
        "categoryless_fallback_indexer_ids",
        "prowlarr_categoryless_fallback_indexer_ids",
    ):
        values.extend(_indexer_policy_string_values((policy or {}).get(key)))
    return indexer_id in {value.lower() for value in values}


def indexer_candidate_verdict(candidate, registry_row=None):
    candidate = dict(candidate or {})
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    policy = provider_policy(registry_row, candidate)
    registry_state = str(registry_row.get("registry_state") or "").strip().lower()
    source_mode = str(registry_row.get("source_mode") or "").strip().lower()
    protocol = normalize_protocol(candidate.get("protocol"))
    allowed_extensions = normalized_extensions(policy.get("allowed_extensions") or registry_row.get("allowed_extensions") or [])
    ext = _indexer_artifact_extension(candidate.get("extension"), candidate.get("title"), candidate.get("download_url"))
    candidate_categories = category_ids(candidate.get("category_ids") or candidate.get("categories"))
    allowed_categories = indexer_policy_categories(policy, registry_row)
    categoryless_fallback_category_gate = _categoryless_fallback_category_gate_allowed(candidate, policy)
    language = str(candidate.get("language") or "").strip().lower()
    policy_language_status = _indexer_language_status(language, policy, None)
    language_status = (
        policy_language_status
        if policy_language_status != "not_checked"
        else (candidate.get("language_status") or policy_language_status)
    )
    match_confidence = candidate.get("match_confidence") or "candidate"
    quality_profile = candidate.get("quality_profile") or candidate.get("quality") or "unknown"
    block_reasons = []
    review_reasons = []

    raw_result = (candidate.get("raw") or {}).get("result") if isinstance(candidate.get("raw"), dict) else {}
    raw_result = raw_result if isinstance(raw_result, dict) else {}
    def candidate_signal(key, default=None):
        return candidate.get(key) if key in candidate else raw_result.get(key, default)

    explicit_hard_blocks = {
        "unsafe_locator": bool(candidate_signal("unsafe_locator") or candidate_signal("locator_safe") is False),
        "duplicate_candidate": bool(candidate_signal("duplicate") or candidate_signal("already_downloading") or candidate_signal("already_imported")),
        "known_bad_candidate": bool(candidate_signal("known_bad") or candidate_signal("known_bad_candidate")),
        "malicious_candidate": bool(candidate_signal("malicious") or candidate_signal("malware") or candidate_signal("virus")),
        "invalid_archive": bool(
            candidate_signal("archive_valid") is False
            or candidate_signal("invalid_archive") is True
            or candidate_signal("corrupt") is True
            or candidate_signal("corrupt_archive") is True
        ),
        "missing_credentials": candidate_signal("credentials_available") is False or registry_row.get("credentials_available") is False,
        "unsupported_client": candidate_signal("supported_client") is False,
    }
    block_reasons.extend(reason for reason, present in explicit_hard_blocks.items() if present)
    if candidate.get("prowlarr_nested_credentials_redacted") is True:
        # Prowlarr proxy links are opaque protected tokens. A URL-valued link
        # with its own credential cannot be reconstructed safely from durable
        # state, so fail closed and require a fresh provider result.
        block_reasons.append("credential_bearing_locator_requires_refresh")
    concrete_url = first_text(
        candidate.get("magnet_url"), candidate.get("magnetUrl"),
        candidate.get("download_url"), candidate.get("downloadUrl"),
    )
    if concrete_url:
        parsed_locator = urlparse(concrete_url)
        locator_scheme = str(parsed_locator.scheme or "").strip().lower()
        if locator_scheme not in {"http", "https", "magnet"}:
            block_reasons.append("unsafe_locator")
        elif locator_scheme == "magnet" and not parse_qsl(parsed_locator.query):
            block_reasons.append("unsafe_locator")

    normalized_match_confidence = str(match_confidence or "").strip().lower().replace("-", "_")
    if normalized_match_confidence == "mismatch":
        block_reasons.append("candidate_title_mismatch")
    elif normalized_match_confidence.startswith("related_series") or normalized_match_confidence in {
        "subseries",
        "related_title",
    }:
        block_reasons.append("related_series_identity")

    if registry_row and registry_state not in {"ready", "assist", "manual_review"}:
        block_reasons.append(f"registry_{registry_state or 'unavailable'}")
    if registry_row and not registry_row.get("auto_search_allowed") and registry_state != "manual_review":
        block_reasons.append("registry_search_not_allowed")
    if not candidate.get("title"):
        block_reasons.append("missing_title")
    if protocol not in INDEXER_PROTOCOLS:
        block_reasons.append("unsupported_protocol")
    if not (candidate.get("download_url") or candidate.get("magnet_url") or candidate.get("info_hash") or candidate.get("guid")):
        block_reasons.append("no_download_locator")
    if ext and allowed_extensions and ext not in allowed_extensions:
        block_reasons.append(f"extension_{ext.lstrip('.')}_not_allowed")

    minimum_seeders = int_value(policy.get("minimum_seeders"), None)
    seeders = int_value(candidate.get("seeders"), None)
    if protocol == "torrent" and minimum_seeders is not None:
        if seeders is None:
            block_reasons.append("seeders_unknown")
        elif seeders < minimum_seeders:
            block_reasons.append("seeders_below_minimum")
    if allowed_categories:
        if not candidate_categories:
            block_reasons.append("category_unknown")
        elif not set(candidate_categories).intersection(allowed_categories) and not categoryless_fallback_category_gate:
            block_reasons.append("category_not_allowed")
    if language_status == "rejected":
        block_reasons.append("language_not_allowed")
    elif language_status == "unknown" and bool(policy.get("require_language_match")):
        review_reasons.append("language_unknown")

    requires_manual = bool(
        registry_row.get("requires_manual_review")
        or policy.get("requires_manual_confirm")
        or source_mode == "manual_review"
        or registry_state == "manual_review"
    )
    if requires_manual:
        review_reasons.append("manual_review_required")
    if source_mode == "assist" or registry_state == "assist":
        review_reasons.append("assist_source_requires_operator")
    packs_allowed = bool(policy.get("packs_allowed") or policy.get("allow_packs") or policy.get("pack_auto_allowed"))
    manifest_pack_match = candidate.get("pack_contents_match") if isinstance(candidate.get("pack_contents_match"), dict) else {}
    manifest_pack_safe = manifest_pack_match.get("coverage_source") in PACK_CONTENTS_SAFE_COVERAGE_SOURCES
    wanted_has_number = bool(
        first_text(
            candidate.get("issue_number"),
            candidate.get("chapter_number"),
            candidate.get("volume_number"),
        )
    )
    if wanted_has_number and match_confidence == "series_title_only" and not manifest_pack_safe:
        review_reasons.append("issue_number_not_confirmed")
    if candidate.get("pack") and not packs_allowed and not manifest_pack_safe:
        review_reasons.append("pack_requires_review")
    if (
        not manifest_pack_safe
        and _candidate_targets_collection(candidate)
        and _candidate_source_is_single_part_without_collection(candidate)
    ):
        review_reasons.append("single_part_file_does_not_satisfy_collection_target")
    if registry_row and not registry_row.get("auto_download_allowed"):
        review_reasons.append("auto_download_not_allowed")

    resolved_client = str(_indexer_download_client(protocol, candidate, policy) or "").strip().lower()
    if protocol in INDEXER_PROTOCOLS and resolved_client not in SUPPORTED_INDEXER_CLIENTS:
        block_reasons.append("unsupported_client")

    candidate["protocol"] = protocol
    candidate["extension"] = ext
    candidate["category_ids"] = candidate_categories
    candidate["allowed_categories"] = allowed_categories
    candidate["categoryless_fallback_category_gate"] = bool(categoryless_fallback_category_gate)
    candidate["allowed_extensions"] = allowed_extensions
    candidate["language"] = language
    candidate["language_status"] = language_status
    candidate["match_confidence"] = match_confidence
    candidate["quality_profile"] = quality_profile
    candidate["quality"] = quality_profile
    candidate["indexer_candidate_key"] = candidate.get("indexer_candidate_key") or indexer_candidate_key(candidate)
    candidate["indexer_suppression_key"] = candidate.get("indexer_suppression_key") or candidate["indexer_candidate_key"]
    candidate["candidate_identity"] = candidate.get("candidate_identity") or candidate["indexer_candidate_key"]
    candidate["block_reasons"] = block_reasons
    candidate["review_reasons"] = review_reasons
    if manifest_pack_safe:
        candidate["pack_contents_match"] = manifest_pack_match
        candidate["pack_contents_coverage_source"] = manifest_pack_match.get("coverage_source")
        candidate["pack_contents_matching_entry"] = manifest_pack_match.get("entry")
        candidate["pack_contents_entry_count"] = manifest_pack_match.get("content_entry_count")
    candidate["candidate_safe"] = not block_reasons and not review_reasons
    if block_reasons:
        candidate["auto_grab_verdict"] = "blocked"
        candidate["review_reason"] = block_reasons[0]
        candidate["quality_status"] = "rejected"
    elif review_reasons:
        candidate["auto_grab_verdict"] = "review"
        candidate["review_reason"] = review_reasons[0]
        candidate["quality_status"] = "review"
    else:
        candidate["auto_grab_verdict"] = "auto_grab_safe"
        candidate["review_reason"] = ""
        candidate["quality_status"] = "accepted"
    return candidate


def indexer_candidate_attempt_seed(candidate, registry_row=None, status=None, reason=None, staging_root=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    provider_id = inkdrop_sources.provider_key(candidate.get("provider_id") or registry_row.get("provider_id") or "prowlarr")
    safe = bool(candidate.get("candidate_safe"))
    inspect = candidate.get("candidate_outcome") == "auto_inspect"
    handoff = safe or inspect
    if status is None:
        status = "sent" if handoff else ("review" if candidate.get("candidate_outcome") == "manual_only" else "blocked")
    status = str(status or "").strip().lower()
    failure_reason = str(
        reason
        or candidate.get("review_reason")
        or (candidate.get("block_reasons") or candidate.get("review_reasons") or [""])[0]
        or ""
    ).strip()
    protocol = normalize_protocol(candidate.get("protocol"))
    category_tokens = category_ids(candidate.get("category_ids") or candidate.get("categories"))
    policy = provider_policy(registry_row, candidate)
    indexer_category = category_tokens[0] if category_tokens else ""
    download_category = first_text(policy.get("download_category"), policy.get("client_category"), "inkdrop")
    retry_scope = f"{protocol or 'indexer'}_download_client_handoff" if handoff else "indexer_candidate_verdict"
    import_expectation = (
        "controlled_staging_then_exact_artifact_proof_required"
        if inspect else _indexer_import_handoff_expectation(protocol, safe=safe)
    )
    indexer_metadata = {
        "indexer": candidate.get("indexer"),
        "indexer_id": candidate.get("indexer_id"),
        "protocol": protocol,
        "info_hash": candidate.get("info_hash"),
        "guid": candidate.get("guid"),
        "download_url_hash": candidate.get("download_url_hash"),
        "source_path": candidate.get("source_path") or candidate.get("url"),
        "unit_type": candidate.get("unit_type") or candidate.get("unitType"),
        "issue_number": candidate.get("issue_number") or candidate.get("issue"),
        "chapter_number": candidate.get("chapter_number") or candidate.get("chapter"),
        "volume_number": candidate.get("volume_number") or candidate.get("volume"),
        "category_ids": category_tokens,
        "seeders": candidate.get("seeders"),
        "peers": candidate.get("peers"),
        "language": candidate.get("language"),
        "language_status": candidate.get("language_status"),
        "match_confidence": candidate.get("match_confidence"),
        "quality_profile": candidate.get("quality_profile") or candidate.get("quality"),
        "pack_contents_coverage_source": candidate.get("pack_contents_coverage_source"),
        "pack_contents_matching_entry": candidate.get("pack_contents_matching_entry"),
        "pack_contents_entry_count": candidate.get("pack_contents_entry_count"),
        "candidate_key": candidate.get("indexer_candidate_key") or indexer_candidate_key(candidate),
        "suppression_key": candidate.get("indexer_suppression_key") or candidate.get("indexer_candidate_key") or indexer_candidate_key(candidate),
    }
    indexer_metadata = {key: value for key, value in indexer_metadata.items() if value not in (None, "", [], {})}
    attempt = {
        "source": provider_id,
        "provider_id": provider_id,
        "source_type": registry_row.get("provider_type") or candidate.get("provider_type") or "indexer",
        "provider_mode": registry_row.get("source_mode"),
        "registry_state": registry_row.get("registry_state"),
        "risk_class": registry_row.get("risk_class"),
        "provider": candidate.get("indexer") or provider_id,
        "indexer": candidate.get("indexer"),
        "indexer_id": candidate.get("indexer_id"),
        "status": status,
        "reason": failure_reason,
        "failure_reason": failure_reason,
        "retry_eligible": not safe and status == "blocked",
        "title": candidate.get("title"),
        "query": candidate.get("series_title") or candidate.get("title"),
        "unit_type": candidate.get("unit_type") or candidate.get("unitType"),
        "issue_number": candidate.get("issue_number") or candidate.get("issue"),
        "chapter_number": candidate.get("chapter_number") or candidate.get("chapter"),
        "volume_number": candidate.get("volume_number") or candidate.get("volume"),
        "candidate_identity": candidate.get("candidate_identity") or indexer_candidate_identity(candidate),
        "provider_candidate_identity": candidate.get("provider_candidate_identity") or candidate.get("candidate_identity") or indexer_candidate_identity(candidate),
        "download_url_hash": candidate.get("download_url_hash"),
        "score": candidate.get("score"),
        "seeders": candidate.get("seeders"),
        "peers": candidate.get("peers"),
        "size_bytes": candidate.get("size_bytes"),
        "category": download_category if safe else indexer_category,
        "category_ids": category_tokens,
        "indexer_category": indexer_category,
        "indexer_category_ids": category_tokens,
        "protocol": protocol,
        "source_path": candidate.get("source_path") or candidate.get("url"),
        "indexer_candidate_key": indexer_metadata.get("candidate_key"),
        "indexer_suppression_key": indexer_metadata.get("suppression_key"),
        "language": candidate.get("language"),
        "language_status": candidate.get("language_status"),
        "match_confidence": candidate.get("match_confidence"),
        "quality_profile": candidate.get("quality_profile") or candidate.get("quality"),
        "quality_status": candidate.get("quality_status"),
        "pack_contents_coverage_source": candidate.get("pack_contents_coverage_source"),
        "pack_contents_matching_entry": candidate.get("pack_contents_matching_entry"),
        "retry_scope": retry_scope,
        "import_handoff_expectation": import_expectation,
        "candidate_safe": safe,
        "auto_grab_verdict": candidate.get("auto_grab_verdict"),
        "candidate_outcome": candidate.get("candidate_outcome"),
        "block_reasons": list(candidate.get("block_reasons") or []),
        "review_reasons": list(candidate.get("review_reasons") or []),
        "raw": {
            "candidate": candidate,
            "indexer": indexer_metadata,
            "retry_scope": retry_scope,
            "import_handoff_expectation": import_expectation,
        },
    }
    if protocol in {"torrent", "usenet"}:
        download_client = _indexer_download_client(protocol, candidate, policy)
        locator = first_text(
            candidate.get("magnet_url"), candidate.get("magnetUrl"),
            candidate.get("download_url"), candidate.get("downloadUrl"),
            candidate.get("url") if protocol == "usenet" else None,
        )
        attempt.update(
            {
                "download_client": download_client,
                "locator_kind": protocol,
                "locator_digest": candidate.get("locator_digest") or url_hash(
                    locator or first_value(candidate.get("info_hash"), candidate.get("guid"))
                ),
            }
        )
        for key in ("download_url", "downloadUrl", "magnet_url", "magnetUrl", "info_hash", "infoHash", "guid"):
            if candidate.get(key) not in (None, ""):
                attempt[key] = candidate.get(key)
    if handoff:
        download_client = _indexer_download_client(protocol, candidate, policy)
        external_id = first_value(
            candidate.get("info_hash"),
            candidate.get("guid"),
            candidate.get("download_url_hash"),
            candidate.get("candidate_identity"),
        )
        save_path = candidate.get("save_path")
        if inspect:
            configured_root = first_text(policy.get("auto_inspect_staging_root"), candidate.get("auto_inspect_staging_root"), staging_root)
            exact_candidate_identity = first_text(
                candidate.get("candidate_instance_identity"),
                candidate.get("provider_candidate_identity"),
                candidate.get("indexer_candidate_key"),
                indexer_candidate_identity(candidate),
            )
            identity_digest = url_hash(exact_candidate_identity)
            save_path = f"{str(configured_root).rstrip('/\\')}/auto-inspect/{identity_digest[:20]}"
            marker = {
                "contract_version": 1,
                "outcome": "auto_inspect",
                "candidate_identity_hash": identity_digest,
                "exact_artifact_proof_required": True,
                "neutral_missing_evidence": sorted(set(candidate.get("review_reasons") or [])),
            }
            attempt["auto_inspect"] = marker
            attempt["raw"]["auto_inspect"] = marker
        attempt.update(
            {
                "download_client": download_client,
                "external_id": external_id,
                "save_path": save_path,
                "category": "inkdrop-auto-inspect" if inspect else download_category,
            }
        )
        if candidate.get("info_hash"):
            attempt["info_hash"] = candidate.get("info_hash")
            attempt["torrent_hash"] = candidate.get("info_hash")
        elif candidate.get("guid"):
            attempt["download_id"] = candidate.get("guid")
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def _tool_key(value):
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def _safe_tool_url_key(key):
    return _tool_key(key) in EXTERNAL_TOOL_SAFE_URL_KEYS


def _sanitize_external_tool_value(key, value, depth=0):
    key_text = str(key or "")
    if EXTERNAL_TOOL_SENSITIVE_KEY_RE.search(key_text):
        return None
    if isinstance(value, dict):
        if depth >= 2:
            return clipped_text(value)
        return {
            str(child_key): child_value
            for child_key, child_value in (
                (child_key, _sanitize_external_tool_value(child_key, child_value, depth + 1))
                for child_key, child_value in value.items()
            )
            if child_value not in (None, "", [], {})
        }
    if isinstance(value, (list, tuple, set)):
        out = []
        for index, item in enumerate(value):
            if index >= 20:
                out.append("...[truncated]")
                break
            item_value = _sanitize_external_tool_value(key, item, depth + 1)
            if item_value not in (None, "", [], {}):
                out.append(item_value)
        return out
    if isinstance(value, str):
        if EXTERNAL_TOOL_DIRECT_URL_KEY_RE.search(key_text) and not _safe_tool_url_key(key_text):
            return {"redacted_url_hash": url_hash(value)}
        return clipped_text(value)
    return value


def sanitized_external_tool_result(result):
    result = result if isinstance(result, dict) else {}
    out = {}
    for key, value in result.items():
        sanitized = _sanitize_external_tool_value(key, value)
        if sanitized not in (None, "", [], {}):
            out[str(key)] = sanitized
    return out


def _external_tool_rows(payload):
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "candidates", "matches"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return [payload]
    return []


def _result_url(result):
    result = result if isinstance(result, dict) else {}
    direct_keys = ("source_url", "sourceUrl", "page_url", "pageUrl", "series_url", "seriesUrl", "comic_url", "manga_url", "url", "link")
    value = first_value(*(result.get(key) for key in direct_keys))
    if value:
        return str(value).strip()
    for value in result.values():
        if not isinstance(value, str):
            continue
        match = URL_RE.search(value)
        if match:
            return match.group(0).strip()
    return ""


def _local_path_text(value):
    text = str(value or "").strip().replace("\\", "/")
    text = re.sub(r"/+", "/", text)
    return text.rstrip("/")


def _local_path_under_root(path_value, root_value):
    path_text = _local_path_text(path_value)
    root_text = _local_path_text(root_value)
    if not path_text or not root_text:
        return False
    if "://" in path_text:
        return False
    parts = [part for part in path_text.split("/") if part]
    if ".." in parts:
        return False
    root_text = root_text.rstrip("/")
    path_key = path_text.lower()
    root_key = root_text.lower()
    return path_key == root_key or path_key.startswith(root_key + "/")


def _local_path_parent(path_value):
    path_text = _local_path_text(path_value)
    if "/" not in path_text:
        return ""
    return path_text.rsplit("/", 1)[0]


def _direct_download_url_present(result):
    result = result if isinstance(result, dict) else {}
    for key, value in result.items():
        if EXTERNAL_TOOL_DIRECT_URL_KEY_RE.search(str(key or "")) and value not in (None, "", [], {}):
            return True
    return False


def external_tool_candidate_from_result(result, registry_row=None, wanted_item=None, tool_name=""):
    result = result if isinstance(result, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    auto_stage_output = bool(policy.get("auto_stage_tool_output") or policy.get("allow_staged_tool_output"))
    provider_id = inkdrop_sources.provider_key(
        registry_row.get("provider_id")
        or result.get("provider_id")
        or result.get("provider")
        or tool_name
        or "external_tool"
    )
    tool = first_text(tool_name, result.get("tool_name"), result.get("tool"), registry_row.get("display_name"), provider_id)
    source_site = first_text(result.get("source_site"), result.get("site"), result.get("connector"), result.get("source"), result.get("provider"))
    source_url = _result_url(result)
    source_url_hash = url_hash(source_url)
    title = normalized_query(
        first_text(
            result.get("title"),
            result.get("name"),
            result.get("comic"),
            result.get("manga"),
            result.get("series"),
            wanted_item.get("title"),
            wanted_item.get("series_title"),
        )
    )
    output_path = first_text(result.get("output_path"), result.get("local_path"), result.get("path"), result.get("file"), result.get("filename"))
    extension = normalize_extension(first_value(result.get("extension"), output_path, result.get("file_name"), result.get("filename")))
    canonical_item_id = first_text(result.get("id"), result.get("guid"), result.get("result_id"))
    if not canonical_item_id:
        canonical_item_id = inkdrop_sources.stable_id("external_tool_result", provider_id, source_site, source_url_hash, title)
    candidate = source_candidate(
        provider_id=provider_id,
        provider_type=registry_row.get("provider_type") or "download_source",
        source_kind=registry_row.get("source_kind") or "external_tool_bridge",
        canonical_item_id=canonical_item_id,
        canonical_work_id=first_text(result.get("series_id"), result.get("work_id")),
        title=title,
        series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), result.get("series"), title),
        language=first_text(result.get("language"), result.get("translated_language")).lower(),
        source_url=source_url,
        download_url="",
        extension=extension,
        content_type=content_type_for_extension(extension),
        size_bytes=int_value(first_value(result.get("size_bytes"), result.get("size")), None),
        rights_status="manual_review_required",
        wanted_item=wanted_item,
        raw={
            "result": sanitized_external_tool_result(result),
            "tool_bridge": "external_tool_candidate_from_result",
        },
    )
    candidate.update(
        {
            "tool_name": tool,
            "tool_version": first_text(result.get("tool_version"), result.get("version")),
            "source_site": source_site,
            "source_url_hash": source_url_hash,
            "output_path": output_path,
            "external_url": bool(source_url),
            "download_url_present": _direct_download_url_present(result),
            "resolver_required": not auto_stage_output,
            "requires_manual_review": not auto_stage_output,
            "tool_requires_operator": not auto_stage_output,
            "auto_stage_tool_output": auto_stage_output,
            "pack": looks_pack_like(title) or looks_pack_like(output_path),
            "score": int_value(result.get("score"), 0),
            "match_confidence": "candidate",
            "auto_grab_verdict": "review",
            "review_reason": "external_tool_manual_review_required",
            "quality_status": "review",
        }
    )
    candidate["candidate_identity"] = external_tool_candidate_identity(candidate)
    return candidate


def external_tool_candidates_from_results(results, registry_row=None, wanted_item=None, tool_name="", limit=20):
    rows = _external_tool_rows(results)
    out = []
    for result in rows[: max(0, int(limit or 0))]:
        out.append(external_tool_candidate_from_result(result, registry_row, wanted_item, tool_name=tool_name))
    return out


def external_tool_candidate_identity_hash(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    return first_text(
        candidate.get("download_url_hash"),
        candidate.get("source_url_hash"),
        url_hash(
            first_text(
                candidate.get("source_url"),
                candidate.get("url"),
                candidate.get("output_path"),
                candidate.get("candidate_identity"),
            )
        ),
    )


def external_tool_candidate_verdict(candidate, registry_row=None):
    candidate = dict(candidate or {})
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    policy = provider_policy(registry_row, candidate)
    registry_state = str(registry_row.get("registry_state") or "").strip().lower()
    source_mode = str(registry_row.get("source_mode") or "").strip().lower()
    allowed_extensions = normalized_extensions(policy.get("allowed_extensions") or registry_row.get("allowed_extensions") or [])
    ext = normalize_extension(candidate.get("extension") or candidate.get("output_path"))
    output_path = first_text(candidate.get("output_path"))
    auto_stage_output = bool(policy.get("auto_stage_tool_output") or policy.get("allow_staged_tool_output"))
    staged_output_root = first_text(policy.get("staged_output_root"), policy.get("staging_root"), registry_row.get("staging_root"))
    block_reasons = []
    review_reasons = []

    if registry_row and registry_state not in {"ready", "assist", "manual_review"}:
        block_reasons.append(f"registry_{registry_state or 'unavailable'}")
    if registry_row and not registry_row.get("auto_search_allowed") and registry_state != "manual_review":
        block_reasons.append("registry_search_not_allowed")
    if not candidate.get("title"):
        block_reasons.append("missing_title")
    if not (candidate.get("source_url_hash") or candidate.get("output_path") or candidate.get("canonical_item_id")):
        block_reasons.append("no_candidate_locator")
    if ext and allowed_extensions and ext not in allowed_extensions:
        block_reasons.append(f"extension_{ext.lstrip('.')}_not_allowed")
    if auto_stage_output:
        if not output_path:
            block_reasons.append("missing_staged_output_path")
        if not ext:
            block_reasons.append("missing_output_extension")
        if not staged_output_root:
            block_reasons.append("missing_staged_output_root")
        elif output_path and not _local_path_under_root(output_path, staged_output_root):
            block_reasons.append("output_path_outside_staging_root")

    requires_manual = bool(
        registry_row.get("requires_manual_review")
        or policy.get("requires_manual_confirm")
        or source_mode == "manual_review"
        or registry_state == "manual_review"
        or candidate.get("requires_manual_review")
    )
    if not auto_stage_output:
        review_reasons.append("external_tool_requires_operator")
    if requires_manual:
        review_reasons.append("manual_review_required")
    if candidate.get("download_url_present") and not auto_stage_output:
        review_reasons.append("direct_download_url_not_stored")
    if candidate.get("pack"):
        review_reasons.append("pack_requires_review")
    if registry_row and not registry_row.get("auto_download_allowed"):
        review_reasons.append("auto_download_not_allowed")

    candidate["extension"] = ext
    candidate["allowed_extensions"] = allowed_extensions
    candidate["auto_stage_tool_output"] = auto_stage_output
    candidate["staged_output_root"] = staged_output_root
    candidate["block_reasons"] = block_reasons
    candidate["review_reasons"] = list(dict.fromkeys(review_reasons))
    if block_reasons:
        candidate["auto_grab_verdict"] = "blocked"
        candidate["review_reason"] = block_reasons[0]
        candidate["quality_status"] = "rejected"
        candidate["candidate_safe"] = False
        candidate["artifact_safe"] = False
    elif candidate["review_reasons"]:
        candidate["auto_grab_verdict"] = "review"
        candidate["review_reason"] = candidate["review_reasons"][0]
        candidate["quality_status"] = "review"
        candidate["candidate_safe"] = False
        candidate["artifact_safe"] = False
    else:
        candidate["auto_grab_verdict"] = "auto_grab_safe"
        candidate["review_reason"] = ""
        candidate["quality_status"] = "accepted"
        candidate["candidate_safe"] = True
        candidate["artifact_safe"] = True
    return candidate


def external_tool_staged_task_seed(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    provider_id = inkdrop_sources.provider_key(candidate.get("provider_id") or "external_tool")
    output_path = _local_path_text(candidate.get("output_path"))
    identity = candidate.get("candidate_identity") or external_tool_candidate_identity(candidate)
    source_path = first_text(candidate.get("source_url"), candidate.get("url"), output_path)
    return {
        "source": provider_id,
        "provider": provider_id,
        "provider_id": provider_id,
        "protocol": "local",
        "download_client": EXTERNAL_TOOL_DOWNLOAD_CLIENT,
        "external_id": identity,
        "candidate_identity": identity,
        "title": candidate.get("title"),
        "status": "staged_file_ready",
        "state": "import_ready",
        "source_path": source_path,
        "save_path": _local_path_parent(output_path),
        "local_path": output_path,
        "size_bytes": candidate.get("size_bytes"),
        "progress": 1,
        "download_url_hash": external_tool_candidate_identity_hash(candidate),
        "raw_json": {
            "candidate": sanitized_external_tool_result(candidate),
            "download_guard": "external_tool_candidate_verdict",
            "external_tool_staged_output": True,
        },
    }


def external_tool_candidate_attempt_seed(candidate, registry_row=None, status=None, reason=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    provider_id = inkdrop_sources.provider_key(candidate.get("provider_id") or registry_row.get("provider_id") or "external_tool")
    artifact_safe = bool(candidate.get("artifact_safe"))
    if status is None:
        status = "staged_file_ready" if artifact_safe else ("review" if candidate.get("auto_grab_verdict") == "review" else "blocked")
    status = str(status or "").strip().lower()
    failure_reason = str(
        reason
        or candidate.get("review_reason")
        or (candidate.get("block_reasons") or candidate.get("review_reasons") or [""])[0]
        or ""
    ).strip()
    attempt = {
        "source": provider_id,
        "provider_id": provider_id,
        "source_type": registry_row.get("provider_type") or candidate.get("provider_type") or "download_source",
        "provider_mode": registry_row.get("source_mode"),
        "registry_state": registry_row.get("registry_state"),
        "risk_class": registry_row.get("risk_class"),
        "provider": candidate.get("source_site") or candidate.get("tool_name") or provider_id,
        "tool_name": candidate.get("tool_name"),
        "tool_version": candidate.get("tool_version"),
        "status": status,
        "reason": failure_reason,
        "failure_reason": failure_reason,
        "retry_eligible": False,
        "title": candidate.get("title"),
        "query": candidate.get("series_title") or candidate.get("title"),
        "candidate_identity": candidate.get("candidate_identity") or external_tool_candidate_identity(candidate),
        "score": candidate.get("score"),
        "source_path": first_text(candidate.get("source_url"), candidate.get("url"), candidate.get("output_path")),
        "language": candidate.get("language"),
        "language_status": candidate.get("language_status"),
        "match_confidence": candidate.get("match_confidence"),
        "quality_status": candidate.get("quality_status"),
        "candidate_safe": artifact_safe,
        "artifact_safe": artifact_safe,
        "auto_grab_verdict": candidate.get("auto_grab_verdict"),
        "block_reasons": list(candidate.get("block_reasons") or []),
        "review_reasons": list(candidate.get("review_reasons") or []),
        "raw": {
            "candidate": candidate,
            "manual_review_only": not artifact_safe,
            "source_url": candidate.get("source_url"),
            "source_url_hash": candidate.get("source_url_hash"),
            "output_path": candidate.get("output_path"),
            "external_tool_guard": "external_tool_candidate_verdict",
        },
    }
    if artifact_safe:
        task = external_tool_staged_task_seed(candidate)
        attempt.update(
            {
                "protocol": task["protocol"],
                "download_client": task["download_client"],
                "external_id": task["external_id"],
                "save_path": task["save_path"],
                "local_path": task["local_path"],
                "download_path": task["local_path"],
                "category": "inkdrop-external-tool",
                "progress": 1,
                "download_url_hash": external_tool_candidate_identity_hash(candidate),
            }
        )
        attempt["raw"]["download_task_seed"] = task
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def sanitized_manual_source_result(result):
    return sanitized_external_tool_result(result)


def manual_source_card_from_result(result, registry_row=None, wanted_item=None, source_bucket=""):
    result = result if isinstance(result, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    provider_id = inkdrop_sources.provider_key(
        registry_row.get("provider_id")
        or result.get("provider_id")
        or source_bucket
        or "manual_source"
    )
    source_site = first_text(
        result.get("source_site"),
        result.get("site"),
        result.get("provider"),
        result.get("engine"),
        result.get("source"),
        registry_row.get("display_name"),
    )
    source_url = _result_url(result)
    source_url_hash = url_hash(source_url)
    title = normalized_query(
        first_text(
            result.get("title"),
            result.get("name"),
            result.get("series"),
            result.get("comic"),
            result.get("manga"),
            result.get("book"),
            wanted_item.get("title"),
            wanted_item.get("series_title"),
        )
    )
    canonical_item_id = first_text(result.get("id"), result.get("guid"), result.get("result_id"))
    if not canonical_item_id:
        canonical_item_id = inkdrop_sources.stable_id("manual_source_result", provider_id, source_site, source_url_hash, title)
    candidate = source_candidate(
        provider_id=provider_id,
        provider_type=registry_row.get("provider_type") or "source",
        source_kind=registry_row.get("source_kind") or "manual_bucket",
        canonical_item_id=canonical_item_id,
        canonical_work_id=first_text(result.get("series_id"), result.get("work_id")),
        title=title,
        series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), result.get("series"), title),
        creator=first_text(result.get("creator"), result.get("author"), result.get("publisher")),
        language=first_text(result.get("language"), result.get("translated_language")).lower(),
        source_url=source_url,
        download_url="",
        extension=normalize_extension(first_value(result.get("extension"), result.get("file_name"), result.get("filename"))),
        content_type="",
        size_bytes=int_value(first_value(result.get("size_bytes"), result.get("size")), None),
        rights_status="manual_review_required",
        wanted_item=wanted_item,
        raw={
            "result": sanitized_manual_source_result(result),
            "manual_source_card": True,
        },
    )
    candidate.update(
        {
            "source_site": source_site,
            "source_url_hash": source_url_hash,
            "source_bucket": provider_id,
            "description": clipped_text(first_text(result.get("description"), result.get("summary"), result.get("note")), 1000),
            "download_url_present": _direct_download_url_present(result),
            "external_url": bool(source_url),
            "manual_link_only": True,
            "requires_manual_review": True,
            "resolver_required": True,
            "pack": looks_pack_like(title) or looks_pack_like(first_text(result.get("filename"), result.get("file_name"))),
            "score": int_value(result.get("score"), 0),
            "match_confidence": "candidate",
            "auto_grab_verdict": "review",
            "review_reason": "manual_source_requires_operator",
            "quality_status": "review",
        }
    )
    candidate["candidate_identity"] = manual_source_card_identity(candidate)
    return candidate


def manual_source_cards_from_results(results, registry_row=None, wanted_item=None, source_bucket="", limit=20):
    rows = _external_tool_rows(results)
    out = []
    for result in rows[: max(0, int(limit or 0))]:
        out.append(manual_source_card_from_result(result, registry_row, wanted_item, source_bucket=source_bucket))
    return out


def _payload_text(payload):
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, dict):
        for key in ("text", "body", "xml", "feed"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return ""


def _xml_child_text(element, names):
    for child in list(element):
        tag = str(child.tag or "").split("}", 1)[-1].lower()
        if tag in names and child.text:
            return str(child.text or "").strip()
    return ""


def _rss_link(element):
    for child in list(element):
        tag = str(child.tag or "").split("}", 1)[-1].lower()
        if tag == "link":
            href = str(child.attrib.get("href") or "").strip()
            text = str(child.text or "").strip()
            return href or text
    return ""


def _rss_rows_from_xml(text):
    text = str(text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    rows = []
    for element in root.iter():
        tag = str(element.tag or "").split("}", 1)[-1].lower()
        if tag not in {"item", "entry"}:
            continue
        title = _xml_child_text(element, {"title"})
        link = _rss_link(element)
        guid = _xml_child_text(element, {"guid", "id"})
        summary = _xml_child_text(element, {"description", "summary", "content"})
        published = _xml_child_text(element, {"pubdate", "published", "updated"})
        rows.append(
            {
                "title": title,
                "site": "RSS feed",
                "url": link,
                "guid": guid,
                "summary": summary,
                "published": published,
            }
        )
    return rows


def rss_item_links_from_payload(payload, wanted_item=None, source_site="", limit=20):
    source_url = _payload_source_url(payload, "")
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = [row for row in payload.get("items") if isinstance(row, dict)]
    else:
        rows = _rss_rows_from_xml(_payload_text(payload))
    out = []
    seen = set()
    for row in rows:
        if not _query_matches_result(row, wanted_item):
            continue
        url = first_text(row.get("url"), row.get("link"), row.get("source_url"), row.get("page_url"))
        url = urljoin(source_url or "", url)
        if not url:
            continue
        parsed = urlparse(url)
        if str(parsed.scheme or "").lower() not in {"http", "https"}:
            continue
        if _url_has_secretish_query(url):
            continue
        identity = url_hash(url)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(
            {
                "title": normalized_query(first_text(row.get("title"), _filename_title_from_url(url))),
                "site": source_site or row.get("site") or "RSS feed",
                "source_site": source_site or row.get("source_site") or row.get("site") or "RSS feed",
                "url": url,
                "guid": first_text(row.get("guid"), row.get("id"), identity),
                "summary": clipped_text(first_text(row.get("summary"), row.get("description")), 1000),
                "published": row.get("published"),
                "rss_item_detail_link": True,
            }
        )
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def rss_feed_evidence_from_payload(payload, wanted_item=None, source_site="", limit=5):
    source_url = _payload_source_url(payload, "")
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = [row for row in payload.get("items") if isinstance(row, dict)]
    else:
        rows = _rss_rows_from_xml(_payload_text(payload))
    sample_limit = max(0, min(int_value(limit, 5), 12))
    item_samples = []
    matching_samples = []
    matching_count = 0

    def sample_row(row):
        url = first_text(row.get("url"), row.get("link"), row.get("source_url"), row.get("page_url"))
        if url:
            url = urljoin(source_url or "", url)
        return {
            key: value
            for key, value in {
                "title": clipped_text(normalized_query(row.get("title")), 180),
                "published": clipped_text(row.get("published"), 80),
                "url_hash": url_hash(url) if url else "",
                "source_site": clipped_text(source_site or row.get("source_site") or row.get("site"), 80),
            }.items()
            if value not in (None, "", [], {})
        }

    for row in rows:
        if len(item_samples) < sample_limit:
            sample = sample_row(row)
            if sample:
                item_samples.append(sample)
        if _query_matches_result(row, wanted_item):
            matching_count += 1
            if len(matching_samples) < sample_limit:
                sample = sample_row(row)
                if sample:
                    matching_samples.append(sample)
    return {
        key: value
        for key, value in {
            "feed_item_count": len(rows),
            "matching_feed_item_count": matching_count,
            "feed_item_samples": item_samples,
            "matching_feed_item_samples": matching_samples,
        }.items()
        if value not in (None, "", [], {})
    }


def _rss_enclosure_rows(element, source_url=""):
    out = []
    for child in list(element):
        tag = str(child.tag or "").split("}", 1)[-1].lower()
        attrs = dict(child.attrib or {})
        url = ""
        content_type = ""
        size = None
        if tag == "enclosure":
            url = first_text(attrs.get("url"), attrs.get("href"))
            content_type = first_text(attrs.get("type"), attrs.get("medium"))
            size = int_value(first_value(attrs.get("length"), attrs.get("size"), attrs.get("fileSize")), None)
        elif tag == "link" and str(attrs.get("rel") or "").strip().lower() == "enclosure":
            url = first_text(attrs.get("href"), attrs.get("url"))
            content_type = first_text(attrs.get("type"), attrs.get("medium"))
            size = int_value(first_value(attrs.get("length"), attrs.get("size"), attrs.get("fileSize")), None)
        elif tag == "content" and attrs.get("url"):
            url = first_text(attrs.get("url"))
            content_type = first_text(attrs.get("type"), attrs.get("medium"))
            size = int_value(first_value(attrs.get("fileSize"), attrs.get("length"), attrs.get("size")), None)
        if not url:
            continue
        out.append(
            {
                "download_url": urljoin(source_url or "", url),
                "content_type": content_type,
                "size_bytes": size,
            }
        )
    return out


def _rss_embedded_direct_file_rows(markup, source_url=""):
    rows = []
    seen = set()
    for file_row in _direct_file_rows_from_html(markup, source_url=source_url, source_site="RSS feed"):
        download_url = first_text(file_row.get("download_url"), file_row.get("url"))
        identity = url_hash(download_url)
        if not download_url or identity in seen:
            continue
        seen.add(identity)
        rows.append(
            {
                "download_url": download_url,
                "content_type": file_row.get("content_type"),
                "size_bytes": file_row.get("size_bytes"),
                "extension": file_row.get("extension"),
            }
        )
    return rows


def _direct_rss_rows_from_xml(text, source_url=""):
    text = str(text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    rows = []
    for element in root.iter():
        tag = str(element.tag or "").split("}", 1)[-1].lower()
        if tag not in {"item", "entry"}:
            continue
        title = _xml_child_text(element, {"title"})
        link = _rss_link(element)
        guid = _xml_child_text(element, {"guid", "id"})
        summary = _xml_child_text(element, {"description", "summary", "content", "encoded"})
        published = _xml_child_text(element, {"pubdate", "published", "updated"})
        direct_rows = []
        direct_rows.extend(_rss_enclosure_rows(element, source_url or link))
        direct_rows.extend(_rss_embedded_direct_file_rows(summary, source_url or link))
        seen_download_urls = set()
        for enclosure in direct_rows:
            download_url = first_text(enclosure.get("download_url"), enclosure.get("url"))
            download_hash = url_hash(download_url)
            if not download_url or download_hash in seen_download_urls:
                continue
            seen_download_urls.add(download_hash)
            row = {
                "title": title,
                "source_url": urljoin(source_url or "", link) if link else source_url,
                "guid": guid,
                "summary": summary,
                "published": published,
                **enclosure,
            }
            rows.append(row)
    return rows


def _xml_local_name(element):
    return str(getattr(element, "tag", "") or "").split("}", 1)[-1].lower()


def _xml_author_name(entry):
    for child in list(entry):
        if _xml_local_name(child) != "author":
            continue
        name = _xml_child_text(child, {"name"})
        if name:
            return name
        text = normalized_query(" ".join(child.itertext()))
        if text:
            return text
    return _xml_child_text(entry, {"creator", "author"})


def _opds_rel_text(value):
    return " ".join(text_values(value)).lower()


def _opds_is_acquisition_rel(value):
    rel = _opds_rel_text(value)
    if "acquisition" not in rel:
        return False
    return not any(token in rel for token in OPDS_ACQUISITION_SKIP_REL_TOKENS)


def _opds_entry_link_attrs(entry):
    rows = []
    for child in list(entry):
        if _xml_local_name(child) == "link":
            rows.append(dict(child.attrib or {}))
    return rows


def _opds_rows_from_xml(text, source_url=""):
    text = str(text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    rows = []
    base_url = str(source_url or "").strip()
    for entry in root.iter():
        if _xml_local_name(entry) != "entry":
            continue
        title = _xml_child_text(entry, {"title"})
        guid = _xml_child_text(entry, {"id", "guid"})
        summary = _xml_child_text(entry, {"summary", "content", "description"})
        published = _xml_child_text(entry, {"published", "updated"})
        language = _xml_child_text(entry, {"language"})
        creator = _xml_author_name(entry)
        source_link = base_url
        link_attrs = _opds_entry_link_attrs(entry)
        for attrs in link_attrs:
            href = first_text(attrs.get("href"), attrs.get("url"))
            rel = _opds_rel_text(attrs.get("rel"))
            if href and (rel == "alternate" or "alternate" in rel):
                source_link = urljoin(base_url, href)
                break
        for attrs in link_attrs:
            if not _opds_is_acquisition_rel(attrs.get("rel")):
                continue
            href = first_text(attrs.get("href"), attrs.get("url"))
            if not href:
                continue
            download_url = urljoin(source_link or base_url, href)
            content_type = content_type_base(first_text(attrs.get("type"), attrs.get("medium")))
            rows.append(
                {
                    "title": title,
                    "source_url": source_link or base_url,
                    "guid": guid,
                    "summary": summary,
                    "published": published,
                    "creator": creator,
                    "language": language,
                    "download_url": download_url,
                    "content_type": content_type or content_type_for_extension(normalize_extension(download_url)),
                    "extension": normalize_extension(download_url) or extension_for_content_type(content_type),
                    "size_bytes": int_value(first_value(attrs.get("length"), attrs.get("size"), attrs.get("fileSize")), None),
                }
            )
    return rows


def _opds_json_link_rows(publication, source_url=""):
    links = publication.get("links") if isinstance(publication, dict) and isinstance(publication.get("links"), list) else []
    base_url = str(source_url or "").strip()
    source_link = base_url
    for link in links:
        if not isinstance(link, dict):
            continue
        href = first_text(link.get("href"), link.get("url"))
        rel = _opds_rel_text(link.get("rel"))
        if href and (rel == "alternate" or "alternate" in rel):
            source_link = urljoin(base_url, href)
            break
    rows = []
    for link in links:
        if not isinstance(link, dict) or not _opds_is_acquisition_rel(link.get("rel")):
            continue
        href = first_text(link.get("href"), link.get("url"))
        if not href:
            continue
        download_url = urljoin(source_link or base_url, href)
        content_type = content_type_base(first_text(link.get("type"), link.get("encodingFormat")))
        rows.append(
            {
                "download_url": download_url,
                "source_url": source_link or base_url,
                "content_type": content_type or content_type_for_extension(normalize_extension(download_url)),
                "extension": normalize_extension(download_url) or extension_for_content_type(content_type),
                "size_bytes": int_value(first_value(link.get("length"), link.get("size"), link.get("fileSize")), None),
            }
        )
    return rows


def _opds_rows_from_json(payload, source_url=""):
    if not isinstance(payload, dict):
        return []
    publications = payload.get("publications")
    if not isinstance(publications, list):
        return []
    rows = []
    for publication in publications:
        if not isinstance(publication, dict):
            continue
        metadata = publication.get("metadata") if isinstance(publication.get("metadata"), dict) else {}
        title = first_text(metadata.get("title"), publication.get("title"))
        creator = first_text(metadata.get("author"), metadata.get("artist"), metadata.get("creator"))
        language = first_text(metadata.get("language"))
        guid = first_text(metadata.get("identifier"), metadata.get("@id"), publication.get("id"))
        summary = first_text(metadata.get("description"), metadata.get("summary"), publication.get("summary"))
        for link_row in _opds_json_link_rows(publication, source_url=source_url):
            rows.append(
                {
                    "title": title,
                    "creator": creator,
                    "language": language,
                    "guid": guid,
                    "summary": summary,
                    **link_row,
                }
            )
    return rows


HTML_LINK_URL_ATTRS = (
    "href",
    "data-href",
    "data-url",
    "data-link",
    "data-download",
    "data-file",
    "data-src",
    "data-target-url",
    "data-redirect",
)
HTML_LINK_TEXT_ATTRS = ("aria-label", "title", "data-title", "value", "name")
HTML_LINK_REL_URL_TOKENS = {"attachment", "download", "enclosure"}
HTML_ONCLICK_URL_RE = re.compile(
    r"""(?ix)
    (?:window\.open|location(?:\.href)?|document\.location(?:\.href)?)
    \s*(?:=|\()\s*['"]([^'"]+)['"]
    """
)
HTML_META_REFRESH_URL_RE = re.compile(
    r"""(?ix)
    \burl\s*=\s*
    (?:
        "([^"]+)"
        |'([^']+)'
        |([^;]+)
    )
    """
)


def _html_link_url_from_attrs(tag, attrs):
    attrs = attrs if isinstance(attrs, dict) else {}
    tag = str(tag or "").lower()
    keys = ("action",) if tag == "form" else HTML_LINK_URL_ATTRS
    for key in keys:
        value = str(attrs.get(key) or "").strip()
        if value:
            return value
    onclick = str(attrs.get("onclick") or "").strip()
    if onclick:
        match = HTML_ONCLICK_URL_RE.search(onclick)
        if match:
            return match.group(1).strip()
        match = URL_RE.search(onclick)
        if match:
            return match.group(0).strip()
    return ""


def _html_link_title_from_attrs(attrs):
    attrs = attrs if isinstance(attrs, dict) else {}
    return normalized_query(first_text(*(attrs.get(key) for key in HTML_LINK_TEXT_ATTRS)))


def _html_link_rel_url(attrs):
    attrs = attrs if isinstance(attrs, dict) else {}
    rel_tokens = {
        token.strip().lower()
        for token in re.split(r"[\s,]+", str(attrs.get("rel") or ""))
        if token.strip()
    }
    if not rel_tokens.intersection(HTML_LINK_REL_URL_TOKENS):
        return ""
    return str(attrs.get("href") or "").strip()


def _html_meta_refresh_url(attrs):
    attrs = attrs if isinstance(attrs, dict) else {}
    if str(attrs.get("http-equiv") or "").strip().lower() != "refresh":
        return ""
    match = HTML_META_REFRESH_URL_RE.search(str(attrs.get("content") or ""))
    if not match:
        return ""
    return first_text(*(part.strip() for part in match.groups() if part))


class _LinkRowsFromHtml(html.parser.HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = str(base_url or "").strip() or "https://comics.codes/"
        self.rows = []
        self._href = ""
        self._attrs = {}
        self._tag = ""
        self._text = []

    def _emit_row(self, url, title="", attrs=None):
        url = str(url or "").strip()
        if not url:
            return
        self.rows.append(
            {
                "title": normalized_query(title),
                "url": urljoin(self.base_url, url),
                "attrs": dict(attrs or {}),
            }
        )

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        if tag not in {"a", "button", "input", "form", "link", "meta"}:
            return
        attrs = {str(key or "").lower(): value for key, value in (attrs or [])}
        if tag == "meta":
            href = _html_meta_refresh_url(attrs)
            if href:
                self._emit_row(href, _html_link_title_from_attrs(attrs) or "refresh", attrs)
            return
        if tag == "link":
            href = _html_link_rel_url(attrs)
            if href:
                self._emit_row(href, _html_link_title_from_attrs(attrs) or attrs.get("rel") or "download", attrs)
            return
        href = _html_link_url_from_attrs(tag, attrs)
        if not href:
            return
        if tag in {"input", "form"}:
            self._emit_row(href, _html_link_title_from_attrs(attrs), attrs)
            return
        self._href = urljoin(self.base_url, href)
        self._attrs = dict(attrs or [])
        self._tag = tag
        self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(str(data or ""))

    def handle_endtag(self, tag):
        if str(tag or "").lower() != self._tag or not self._href:
            return
        text = normalized_query(first_text(" ".join(self._text), _html_link_title_from_attrs(self._attrs)))
        self._emit_row(self._href, text, self._attrs)
        self._href = ""
        self._attrs = {}
        self._tag = ""
        self._text = []


class _ImageRowsFromHtml(html.parser.HTMLParser):
    IMAGE_URL_ATTRS = (
        "src",
        "data-src",
        "data-original",
        "data-lazy-src",
        "data-url",
        "data-image",
    )

    def __init__(self, base_url):
        super().__init__()
        self.base_url = str(base_url or "").strip() or "https://example.invalid/"
        self.rows = []

    def _image_url_from_attrs(self, attrs):
        attrs = dict(attrs or [])
        for key in self.IMAGE_URL_ATTRS:
            value = str(attrs.get(key) or "").strip()
            if value:
                return urljoin(self.base_url, value), attrs
        srcset = str(attrs.get("srcset") or attrs.get("data-srcset") or "").strip()
        if srcset:
            first = srcset.split(",", 1)[0].strip().split(" ", 1)[0].strip()
            if first:
                return urljoin(self.base_url, first), attrs
        return "", attrs

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        if tag not in {"img", "source"}:
            return
        url, attr_map = self._image_url_from_attrs(attrs)
        if not url:
            return
        self.rows.append(
            {
                "url": url,
                "title": normalized_query(first_text(attr_map.get("alt"), attr_map.get("title"))),
                "attrs": attr_map,
            }
        )


SCRIPT_IMAGE_URL_RE = re.compile(
    r"""(?ix)
    (?:
        https?:\\?/\\?/[^"'()<>\s]+?
        |//[^"'()<>\s]+?
        |/[A-Za-z0-9._~!$&'()*+,;=:@%/\-]+?
    )
    \.(?:jpe?g|png|webp)
    (?:[?][^"'()<>\s]*)?
    """
)


def _unescape_script_image_url(value, source_url=""):
    text = str(value or "").strip().strip("\"'")
    if not text:
        return ""
    text = html_unescape(text)
    text = (
        text.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
        .replace("\\u0026", "&")
    )
    if text.startswith("//"):
        parsed = urlparse(str(source_url or ""))
        text = f"{parsed.scheme or 'https'}:{text}"
    elif text.startswith("/"):
        text = urljoin(source_url or "https://example.invalid/", text)
    return urljoin(source_url or "https://example.invalid/", text)


def _script_image_rows_from_text(text, source_url="", allowed_extensions=None):
    allowed = set(normalized_extensions(allowed_extensions or GENERIC_PAGE_IMAGE_EXTENSIONS))
    rows = []
    seen = set()
    for match in SCRIPT_IMAGE_URL_RE.finditer(str(text or "")):
        url = _unescape_script_image_url(match.group(0), source_url=source_url)
        if not url or _url_has_secretish_query(url):
            continue
        parsed = urlparse(url)
        if str(parsed.scheme or "").lower() not in {"http", "https"}:
            continue
        ext = normalize_extension(url)
        if not ext or (allowed and ext not in allowed):
            continue
        identity = url_hash(url)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(
            {
                "url": url,
                "title": _filename_title_from_url(url),
                "attrs": {"source": "script_image_url"},
            }
        )
    return rows


def _payload_source_url(payload, default="https://comics.codes/"):
    if isinstance(payload, dict):
        return first_text(payload.get("source_url"), payload.get("url"), payload.get("request_url"), default)
    return default


def _comicscodes_title_from_url(url):
    parsed = urlparse(str(url or ""))
    slug = PurePosixPath(unquote(parsed.path or "")).name
    slug = re.sub(r"[-_]+", " ", slug)
    slug = re.sub(r"\s+", " ", slug).strip()
    return slug


def _comicscodes_rows_from_html(text, source_url="https://comics.codes/"):
    parser = _LinkRowsFromHtml(source_url)
    try:
        parser.feed(str(text or ""))
    except Exception:
        return []
    rows = []
    seen = set()
    for row in parser.rows:
        url = str(row.get("url") or "").strip()
        title = normalized_query(row.get("title") or _comicscodes_title_from_url(url))
        if not url or not title or len(title) < 3:
            continue
        parsed = urlparse(url)
        host = str(parsed.netloc or "").lower()
        path = str(parsed.path or "")
        if host and host not in {"comics.codes", "www.comics.codes"}:
            continue
        if re.search(r"(?i)\b(login|privacy|contact|dmca|advertis|request|comment|page \d+)\b", title):
            continue
        if re.search(r"(?i)/(tag|author|privacy|contact|dmca|wp-|feed|comments|page)/", path):
            continue
        identity = url_hash(url) or inkdrop_sources.stable_id("comicscodes_html", title)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(
            {
                "title": title,
                "site": "ComicsCodes",
                "source_site": "ComicsCodes",
                "url": url,
                "guid": identity,
                "summary": "",
            }
        )
    return rows


def _html_search_rows_from_html(text, source_url="", source_site=""):
    parser = _LinkRowsFromHtml(source_url)
    try:
        parser.feed(str(text or ""))
    except Exception:
        return []
    rows = []
    seen = set()
    for row in parser.rows:
        url = str(row.get("url") or "").strip()
        title = normalized_query(row.get("title"))
        if not url or not title or len(title) < 3:
            continue
        parsed = urlparse(url)
        scheme = str(parsed.scheme or "").lower()
        if scheme and scheme not in {"http", "https"}:
            continue
        if re.search(r"(?i)\b(login|privacy|contact|dmca|advertis|donate|terms|about|source|homepage|next|previous)\b", title):
            continue
        if re.search(r"(?i)/(login|privacy|contact|dmca|wp-|feed|comments|tag|category|about|terms)/", str(parsed.path or "")):
            continue
        identity = url_hash(url) or inkdrop_sources.stable_id("html_search", source_site, title)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(
            {
                "title": title,
                "site": source_site,
                "source_site": source_site,
                "url": url,
                "guid": identity,
                "summary": "",
            }
        )
    return rows


def _policy_regex_list(values):
    if values in (None, "", False):
        return []
    if isinstance(values, str):
        try:
            parsed = json.loads(values)
            if isinstance(parsed, (list, tuple, set)):
                values = parsed
            else:
                values = re.split(r"[\r\n]+|[,;]", values)
        except Exception:
            values = re.split(r"[\r\n]+|[,;]", values)
    elif isinstance(values, dict):
        values = values.values()
    out = []
    seen = set()
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _policy_regex_matches(patterns, text):
    text = str(text or "")
    for pattern in _policy_regex_list(patterns):
        try:
            if re.search(pattern, text):
                return True
        except re.error:
            continue
    return False


def _search_result_allowed_by_policy(row, policy=None):
    policy = policy if isinstance(policy, dict) else {}
    if not policy:
        return True
    row = row if isinstance(row, dict) else {}
    url = str(row.get("url") or row.get("source_url") or "").strip()
    title = normalized_query(first_text(row.get("title"), row.get("summary"), row.get("description")))
    allow_url = first_value(policy.get("result_url_allow_patterns"), policy.get("allowed_result_url_patterns"), policy.get("follow_url_allow_patterns"))
    deny_url = first_value(policy.get("result_url_deny_patterns"), policy.get("blocked_result_url_patterns"), policy.get("follow_url_deny_patterns"))
    allow_title = first_value(policy.get("result_title_allow_patterns"), policy.get("allowed_result_title_patterns"))
    deny_title = first_value(policy.get("result_title_deny_patterns"), policy.get("blocked_result_title_patterns"))
    if _policy_regex_list(allow_url) and not _policy_regex_matches(allow_url, url):
        return False
    if _policy_regex_matches(deny_url, url):
        return False
    if _policy_regex_list(allow_title) and not _policy_regex_matches(allow_title, title):
        return False
    if _policy_regex_matches(deny_title, title):
        return False
    return True


def html_search_result_links_from_payload(payload, source_site="", limit=20, policy=None):
    rows = _html_search_rows_from_html(
        _payload_text(payload),
        _payload_source_url(payload, ""),
        source_site,
    )
    out = []
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url or _url_has_secretish_query(url):
            continue
        if not _search_result_allowed_by_policy(row, policy):
            continue
        out.append(row)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def _wanted_number_tokens(wanted_item=None):
    wanted = str(_wanted_chapter_number(wanted_item) or "").strip()
    if not wanted:
        return []
    tokens = [wanted]
    try:
        numeric = int(float(wanted))
        tokens.extend([str(numeric), f"{numeric:02d}", f"{numeric:03d}"])
    except Exception:
        pass
    out = []
    seen = set()
    for token in tokens:
        token = str(token or "").strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _link_matches_wanted_number(row, wanted_item=None):
    tokens = _wanted_number_tokens(wanted_item)
    if not tokens:
        return True
    text = normalized_query(
        " ".join(
            str(row.get(key) or "")
            for key in ("title", "summary", "description", "url")
        )
    ).lower()
    if not text:
        return False
    for token in tokens:
        pattern = r"(?<![a-z0-9])" + re.escape(token.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            return True
    return False


def reader_chapter_links_from_payload(payload, wanted_item=None, source_site="", limit=20, policy=None):
    rows = html_search_result_links_from_payload(payload, source_site=source_site, limit=limit, policy=policy)
    context = {
        "title": first_text((payload or {}).get("title") if isinstance(payload, dict) else ""),
        "summary": clipped_text(_payload_text(payload), 5000),
    }
    context_matches = _query_matches_result(context, wanted_item)
    out = []
    seen = set()
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url or url in seen:
            continue
        if normalize_extension(url):
            continue
        row_matches = _query_matches_result(row, wanted_item)
        if not row_matches and not context_matches:
            continue
        if context_matches and not row_matches and not _link_matches_wanted_number(row, wanted_item):
            continue
        seen.add(url)
        out.append(row)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


JSON_SEARCH_LINK_KEYS = (
    "link",
    "url",
    "permalink",
    "href",
    "guid",
    "source_url",
    "sourceUrl",
    "page_url",
    "pageUrl",
)
JSON_SEARCH_TITLE_KEYS = ("title", "name", "headline", "post_title", "label")
JSON_SEARCH_SUMMARY_KEYS = ("excerpt", "summary", "description", "content")


def _json_text_field(value):
    if isinstance(value, dict):
        return first_text(value.get("rendered"), value.get("plain"), value.get("text"), value.get("@value"), value.get("name"))
    return _jsonld_text(value)


def _json_search_rows_from_payload(payload, source_url="", source_site=""):
    if isinstance(payload, dict) and "json" in payload:
        data = payload.get("json")
    elif isinstance(payload, dict) and any(key in payload for key in ("text", "body", "xml", "feed")):
        return []
    elif isinstance(payload, (dict, list)):
        data = payload
    else:
        return []
    roots = data if isinstance(data, list) else first_value(data.get("results"), data.get("items"), data.get("data"), data.get("posts"), data)
    if isinstance(roots, dict):
        roots = first_value(roots.get("results"), roots.get("items"), roots.get("data"), roots.get("posts"), [roots])
    rows = []
    seen = set()
    for item in roots if isinstance(roots, list) else []:
        if not isinstance(item, dict):
            continue
        title = normalized_query(first_text(*(_json_text_field(item.get(key)) for key in JSON_SEARCH_TITLE_KEYS)))
        summary = clipped_text(first_text(*(_json_text_field(item.get(key)) for key in JSON_SEARCH_SUMMARY_KEYS)), 1000)
        url = ""
        for key in JSON_SEARCH_LINK_KEYS:
            for candidate in _jsonld_url_values(item.get(key)):
                candidate = urljoin(source_url or "", candidate)
                if candidate:
                    url = candidate
                    break
            if url:
                break
        if not title:
            title = normalized_query(_filename_title_from_url(url))
        if not url or not title or len(title) < 3:
            continue
        parsed = urlparse(url)
        if str(parsed.scheme or "").lower() not in {"http", "https"}:
            continue
        if _url_has_secretish_query(url):
            continue
        if re.search(r"(?i)\b(login|privacy|contact|dmca|advertis|donate|terms|about|source|homepage|next|previous)\b", title):
            continue
        if re.search(r"(?i)/(login|privacy|contact|dmca|wp-|feed|comments|tag|category|about|terms)/", str(parsed.path or "")):
            continue
        identity = first_text(item.get("id"), item.get("guid"), url_hash(url))
        key = url_hash(url)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "title": title,
                "site": source_site,
                "source_site": source_site,
                "url": url,
                "guid": identity,
                "summary": summary,
                "json_search_result": True,
            }
        )
    return rows


def json_search_result_links_from_payload(payload, source_site="", limit=20, policy=None):
    rows = _json_search_rows_from_payload(
        payload,
        _payload_source_url(payload, ""),
        source_site,
    )
    out = []
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url or _url_has_secretish_query(url):
            continue
        if not _search_result_allowed_by_policy(row, policy):
            continue
        out.append(row)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


GENERIC_DIRECT_FILE_EXTENSIONS = (".cbz", ".cbr", ".zip", ".pdf", ".epub", ".mobi", ".azw3")
GENERIC_JSON_DIRECT_EXTENSIONS = GENERIC_DIRECT_FILE_EXTENSIONS
GENERIC_PAGE_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
GENERIC_DIRECT_FILE_LINK_TITLES = {
    "download",
    "download now",
    "download file",
    "direct download",
    "cbz",
    "cbr",
    "zip",
    "pdf",
    "epub",
}
GENERIC_SHARED_FILE_HOSTS = ("pixeldrain",)
GENERIC_DIRECT_FILE_REDIRECT_QUERY_KEYS = {
    "download",
    "file",
    "href",
    "link",
    "redirect",
    "redirect_url",
    "target",
    "target_url",
    "to",
    "u",
    "url",
}
JSON_DIRECT_URL_KEYS = (
    "download_url",
    "downloadUrl",
    "direct_url",
    "directUrl",
    "file_url",
    "fileUrl",
    "artifact_url",
    "artifactUrl",
    "enclosure_url",
    "enclosureUrl",
    "href",
    "url",
    "link",
)
JSON_SOURCE_URL_KEYS = (
    "source_url",
    "sourceUrl",
    "page_url",
    "pageUrl",
    "info_url",
    "infoUrl",
    "details_url",
    "detailsUrl",
)
JSON_DIRECT_LINK_REL_TOKENS = {"acquisition", "artifact", "direct", "download", "enclosure", "file"}
JSON_DIRECT_GENERIC_URL_KEYS = {"url", "link", "href"}
JSONLD_DIRECT_URL_KEYS = {
    "contenturl",
    "downloadurl",
    "fileurl",
    "artifacturl",
    "enclosureurl",
    "url",
    "href",
}
JSONLD_CONTENT_TYPE_KEYS = ("encodingFormat", "fileFormat", "contentType", "mimeType")
JSONLD_SIZE_KEYS = ("contentSize", "fileSize", "size")


def _filename_title_from_url(url):
    parsed = urlparse(str(url or ""))
    name = PurePosixPath(unquote(parsed.path or "")).name
    if not name:
        return ""
    if "." in name:
        name = name.rsplit(".", 1)[0]
    name = re.sub(r"[-_.]+", " ", name)
    return normalized_query(name)


def _decoded_redirect_query_values(url):
    parsed = urlparse(str(url or ""))
    values = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        key = str(key or "").strip().lower()
        if key not in GENERIC_DIRECT_FILE_REDIRECT_QUERY_KEYS:
            continue
        text = str(value or "").strip().strip("\"'")
        for _ in range(2):
            decoded = unquote(text).strip().strip("\"'")
            if decoded == text:
                break
            text = decoded
        if text:
            values.append(text)
    return values


def _explicit_file_url_from_redirect_query(url, allowed_extensions=None):
    allowed = set(normalized_extensions(allowed_extensions or GENERIC_DIRECT_FILE_EXTENSIONS))
    parsed = urlparse(str(url or ""))
    if str(parsed.scheme or "").lower() not in {"http", "https"}:
        return ""
    for value in _decoded_redirect_query_values(url):
        if value.startswith("//"):
            value = f"{parsed.scheme}:{value}"
        elif value.startswith("/"):
            value = urljoin(url, value)
        candidate = str(value or "").strip()
        candidate_parsed = urlparse(candidate)
        if str(candidate_parsed.scheme or "").lower() not in {"http", "https"}:
            continue
        if _url_has_secretish_query(candidate):
            continue
        ext = normalize_extension(candidate)
        if ext and (not allowed or ext in allowed):
            return candidate
    return ""


PIXELDRAIN_FILE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")


def _normalized_shared_file_host(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = str(parsed.hostname or text.split("/", 1)[0].split(":", 1)[0]).strip().lower()
    return host


def _allowed_shared_file_hosts(values):
    if values in (None, "", False):
        values = ()
    elif isinstance(values, str):
        values = re.split(r"[,;\s]+", values)
    elif isinstance(values, dict):
        values = first_value(values.get("hosts"), values.get("host"), values.get("hostname"), values.get("domain"), ())
    out = set()
    for value in values or ():
        text = str(value or "").strip().lower()
        if text:
            out.add(text)
    return out


def _shared_file_host_rule_rows(values):
    if values in (None, "", False):
        return []
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except Exception:
            return []
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    rows = []
    for value in values:
        if not isinstance(value, dict):
            continue
        enabled = value.get("enabled", True)
        if str(enabled).strip().lower() in {"0", "false", "no", "off"}:
            continue
        template = first_text(
            value.get("direct_url_template"),
            value.get("download_url_template"),
            value.get("direct_download_url_template"),
        )
        if not template:
            continue
        hosts = set()
        raw_hosts = first_value(value.get("hosts"), value.get("host"), value.get("hostname"), value.get("domain"), ())
        if isinstance(raw_hosts, str):
            raw_hosts = re.split(r"[,;\s]+", raw_hosts)
        for host_value in raw_hosts or ():
            host = _normalized_shared_file_host(host_value)
            if host:
                hosts.add(host)
        if not hosts:
            continue
        rows.append(
            {
                "name": first_text(value.get("name"), value.get("label"), next(iter(sorted(hosts)))),
                "hosts": hosts,
                "path_regex": first_text(value.get("path_regex"), value.get("share_path_regex")),
                "url_regex": first_text(value.get("url_regex"), value.get("share_url_regex")),
                "direct_url_template": template,
            }
        )
    return rows


def _pixeldrain_direct_download_url(url):
    parsed = urlparse(str(url or "").strip())
    host = str(parsed.netloc or "").split(":", 1)[0].lower()
    if host not in {"pixeldrain.com", "www.pixeldrain.com"}:
        return ""
    parts = [unquote(part).strip() for part in str(parsed.path or "").split("/") if part]
    file_id = ""
    if len(parts) >= 2 and parts[0].lower() == "u":
        file_id = parts[1]
    elif len(parts) >= 3 and parts[0].lower() == "api" and parts[1].lower() == "file":
        file_id = parts[2]
    if not file_id or not PIXELDRAIN_FILE_ID_RE.fullmatch(file_id):
        return ""
    return f"https://pixeldrain.com/api/file/{quote(file_id, safe='')}?download"


def _shared_file_host_rule_match(url, shared_file_host_rules=None):
    parsed = urlparse(str(url or "").strip())
    host = _normalized_shared_file_host(parsed.netloc)
    if not host:
        return "", ""
    for rule in _shared_file_host_rule_rows(shared_file_host_rules):
        if host not in rule.get("hosts", set()):
            continue
        values = {
            "scheme": parsed.scheme,
            "host": host,
            "path": parsed.path or "",
            "path_quoted": quote(parsed.path or "", safe=""),
            "query": parsed.query or "",
            "url": str(url or "").strip(),
            "url_quoted": quote(str(url or "").strip(), safe=""),
        }
        path_regex = rule.get("path_regex")
        if path_regex:
            try:
                match = re.search(path_regex, parsed.path or "")
            except re.error:
                continue
            if not match:
                continue
            values.update({key: value for key, value in match.groupdict().items() if value is not None})
            for index, value in enumerate(match.groups(), start=1):
                if value is not None:
                    values[f"group{index}"] = value
        url_regex = rule.get("url_regex")
        if url_regex:
            try:
                match = re.search(url_regex, str(url or "").strip())
            except re.error:
                continue
            if not match:
                continue
            values.update({key: value for key, value in match.groupdict().items() if value is not None})
            for index, value in enumerate(match.groups(), start=1):
                if value is not None:
                    values[f"url_group{index}"] = value
        for key, value in list(values.items()):
            values[f"{key}_quoted"] = quote(str(value or ""), safe="")
        try:
            direct_url = str(rule.get("direct_url_template") or "").format(**values)
        except Exception:
            continue
        direct_url = urljoin(str(url or ""), direct_url)
        direct_parsed = urlparse(direct_url)
        if str(direct_parsed.scheme or "").lower() not in {"http", "https"} or not direct_parsed.netloc:
            continue
        if _url_has_secretish_query(direct_url):
            continue
        return direct_url, str(rule.get("name") or host)
    return "", ""


def _shared_file_host_direct_download_url(url, allowed_hosts=None, shared_file_host_rules=None):
    allowed = _allowed_shared_file_hosts(allowed_hosts)
    has_rules = bool(_shared_file_host_rule_rows(shared_file_host_rules))
    if not allowed and not has_rules:
        return ""
    if "pixeldrain" in allowed:
        direct_url = _pixeldrain_direct_download_url(url)
        if direct_url:
            return direct_url
    direct_url, _name = _shared_file_host_rule_match(url, shared_file_host_rules)
    if direct_url:
        return direct_url
    return ""


def _shared_file_host_name(url, allowed_hosts=None, shared_file_host_rules=None):
    allowed = _allowed_shared_file_hosts(allowed_hosts)
    if "pixeldrain" in allowed and _pixeldrain_direct_download_url(url):
        return "pixeldrain"
    _direct_url, name = _shared_file_host_rule_match(url, shared_file_host_rules)
    if name:
        return name
    return ""


class _JsonLdRowsFromHtml(html.parser.HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = str(base_url or "").strip() or "https://example.invalid/"
        self.rows = []
        self._capturing = False
        self._chunks = []

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        if tag != "script":
            return
        attrs = {str(key or "").lower(): value for key, value in (attrs or [])}
        script_type = content_type_base(attrs.get("type"))
        if script_type == "application/ld+json":
            self._capturing = True
            self._chunks = []

    def handle_data(self, data):
        if self._capturing:
            self._chunks.append(str(data or ""))

    def handle_endtag(self, tag):
        if str(tag or "").lower() != "script" or not self._capturing:
            return
        text = html_unescape("".join(self._chunks)).strip()
        self._capturing = False
        self._chunks = []
        if not text:
            return
        try:
            self.rows.append(json.loads(text))
        except Exception:
            return


def html_unescape(value):
    try:
        return html.parser.HTMLParser().unescape(str(value or ""))
    except Exception:
        try:
            import html as _html

            return _html.unescape(str(value or ""))
        except Exception:
            return str(value or "")


def _jsonld_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _jsonld_text(value):
    if isinstance(value, dict):
        return first_text(value.get("@value"), value.get("name"), value.get("text"))
    if isinstance(value, (list, tuple)):
        return first_text(*value)
    return first_text(value)


def _jsonld_url_values(value):
    out = []
    if value in (None, ""):
        return out
    if isinstance(value, str):
        text = value.strip()
        if text and "{" not in text and "}" not in text:
            out.append(text)
        return out
    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_jsonld_url_values(item))
        return out
    if isinstance(value, dict):
        for key in ("@id", "url", "contentUrl", "downloadUrl", "fileUrl", "href", "urlTemplate"):
            if key in value:
                out.extend(_jsonld_url_values(value.get(key)))
        return out
    return out


def _jsonld_size_bytes(value):
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return _jsonld_size_bytes(first_value(value.get("value"), value.get("@value")))
    text = str(value or "").strip()
    direct = int_value(text, None)
    if direct is not None:
        return direct
    match = re.search(r"(?i)\b(\d+(?:\.\d+)?)\s*(bytes?|kb|kib|mb|mib|gb|gib)\b", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    scale = 1
    if unit in {"kb", "kib"}:
        scale = 1024
    elif unit in {"mb", "mib"}:
        scale = 1024 * 1024
    elif unit in {"gb", "gib"}:
        scale = 1024 * 1024 * 1024
    return int(amount * scale)


def _direct_file_rows_from_jsonld(text, source_url="", source_site="", allowed_extensions=None):
    parser = _JsonLdRowsFromHtml(source_url)
    try:
        parser.feed(str(text or ""))
    except Exception:
        return []
    allowed = set(normalized_extensions(allowed_extensions or GENERIC_DIRECT_FILE_EXTENSIONS))
    rows = []
    seen = set()

    def add_url(url, title="", summary="", content_type="", size_bytes=None):
        candidate_url = urljoin(source_url or "", str(url or "").strip())
        if not candidate_url or _url_has_secretish_query(candidate_url):
            return
        parsed = urlparse(candidate_url)
        if str(parsed.scheme or "").lower() not in {"http", "https"}:
            return
        download_url = _explicit_file_url_from_redirect_query(candidate_url, allowed) or candidate_url
        ext = normalize_extension(download_url)
        if not ext or (allowed and ext not in allowed):
            return
        identity = url_hash(download_url)
        if identity in seen:
            return
        seen.add(identity)
        file_title = _filename_title_from_url(download_url)
        rows.append(
            {
                "title": normalized_query(first_text(title, file_title, "direct file")),
                "summary": first_text(summary, file_title),
                "source_site": source_site,
                "source_url": source_url,
                "download_url": download_url,
                "source_link_url_hash": url_hash(candidate_url) if download_url != candidate_url else "",
                "guid": identity,
                "extension": ext,
                "content_type": content_type_base(content_type) or content_type_for_extension(ext),
                "size_bytes": size_bytes,
                "structured_data": True,
            }
        )

    def walk(node, title_context="", summary_context=""):
        if isinstance(node, list):
            for item in node:
                walk(item, title_context=title_context, summary_context=summary_context)
            return
        if not isinstance(node, dict):
            return
        title = normalized_query(first_text(_jsonld_text(node.get("name")), _jsonld_text(node.get("headline")), _jsonld_text(node.get("title")), title_context))
        summary = clipped_text(first_text(_jsonld_text(node.get("description")), summary_context), 1000)
        content_type = first_text(*(node.get(key) for key in JSONLD_CONTENT_TYPE_KEYS))
        size_bytes = first_value(*(_jsonld_size_bytes(node.get(key)) for key in JSONLD_SIZE_KEYS))
        for key, value in node.items():
            normalized_key = _jsonld_key(key)
            if normalized_key in JSONLD_DIRECT_URL_KEYS or normalized_key == "urltemplate":
                for url in _jsonld_url_values(value):
                    add_url(url, title=title, summary=summary, content_type=content_type, size_bytes=size_bytes)
            if isinstance(value, (dict, list)):
                walk(value, title_context=title, summary_context=summary)

    for row in parser.rows:
        walk(row)
    return rows


def _direct_file_rows_from_html(text, source_url="", source_site="", allowed_extensions=None):
    parser = _LinkRowsFromHtml(source_url)
    try:
        parser.feed(str(text or ""))
    except Exception:
        return []
    allowed = set(normalized_extensions(allowed_extensions or GENERIC_DIRECT_FILE_EXTENSIONS))
    rows = []
    seen = set()
    candidate_rows = list(parser.rows)
    candidate_rows.extend(_direct_file_rows_from_jsonld(text, source_url=source_url, source_site=source_site, allowed_extensions=allowed))
    for row in candidate_rows:
        url = str(row.get("url") or "").strip()
        if not url and row.get("download_url"):
            url = str(row.get("download_url") or "").strip()
        if not url or _url_has_secretish_query(url):
            continue
        parsed = urlparse(url)
        scheme = str(parsed.scheme or "").lower()
        if scheme and scheme not in {"http", "https"}:
            continue
        download_url = _explicit_file_url_from_redirect_query(url, allowed) or url
        ext = normalize_extension(download_url)
        if not ext or (allowed and ext not in allowed):
            continue
        identity = url_hash(download_url)
        if identity in seen:
            continue
        seen.add(identity)
        attrs = row.get("attrs") if isinstance(row.get("attrs"), dict) else {}
        file_title = _filename_title_from_url(download_url)
        title = normalized_query(row.get("title"))
        if not title or title.lower() in GENERIC_DIRECT_FILE_LINK_TITLES:
            title = file_title
        if not title:
            title = file_title or PurePosixPath(unquote(urlparse(download_url).path or "")).name or "direct file"
        size = int_value(
            first_value(
                row.get("size_bytes"),
                row.get("size"),
                attrs.get("data-size"),
                attrs.get("data-filesize"),
                attrs.get("data-file-size"),
                attrs.get("data-length"),
                attrs.get("length"),
                attrs.get("size"),
            ),
            None,
        )
        content_type = first_text(row.get("content_type"), attrs.get("type"), attrs.get("data-type"), attrs.get("data-content-type"))
        rows.append(
            {
                "title": title,
                "summary": first_text(row.get("summary"), file_title),
                "source_site": source_site,
                "source_url": source_url,
                "download_url": download_url,
                "source_link_url_hash": first_text(row.get("source_link_url_hash"), url_hash(url) if download_url != url else ""),
                "guid": identity,
                "extension": ext,
                "content_type": content_type or content_type_for_extension(ext),
                "size_bytes": size,
                "structured_data": bool(row.get("structured_data")),
            }
        )
    return rows


GENERIC_DIRECT_FILE_PROBE_LINK_RE = re.compile(
    r"(?i)\b(download|direct|file|mirror|ddl|get|open|view|read|cbz|cbr|zip|rar|pdf|epub|mega|mediafire|gofile|pixeldrain|drive|dropbox|1fichier)\b"
)


def _is_probeish_direct_file_link(row, url, ext):
    attrs = row.get("attrs") if isinstance(row.get("attrs"), dict) else {}
    haystack = " ".join(
        str(value or "")
        for value in (
            row.get("title"),
            attrs.get("title"),
            attrs.get("aria-label"),
            attrs.get("download"),
            attrs.get("data-title"),
            attrs.get("data-file"),
            attrs.get("data-filename"),
            attrs.get("data-host"),
            attrs.get("class"),
            url,
        )
    )
    return bool(ext or GENERIC_DIRECT_FILE_PROBE_LINK_RE.search(haystack))


def _direct_file_probe_rows_from_html(
    text,
    source_url="",
    source_site="",
    allowed_extensions=None,
    context_title="",
    shared_file_hosts=None,
    shared_file_host_rules=None,
):
    parser = _LinkRowsFromHtml(source_url)
    try:
        parser.feed(str(text or ""))
    except Exception:
        return []
    allowed = set(normalized_extensions(allowed_extensions or GENERIC_DIRECT_FILE_EXTENSIONS))
    context_title = normalized_query(context_title)
    rows = []
    seen = set()
    for row in parser.rows:
        url = str(row.get("url") or "").strip()
        if not url or _url_has_secretish_query(url):
            continue
        parsed = urlparse(url)
        scheme = str(parsed.scheme or "").lower()
        if scheme and scheme not in {"http", "https"}:
            continue
        probe_url = _shared_file_host_direct_download_url(url, shared_file_hosts, shared_file_host_rules) or url
        ext = normalize_extension(probe_url)
        if ext and allowed and ext not in allowed:
            continue
        if _url_has_secretish_query(probe_url):
            continue
        if not _is_probeish_direct_file_link(row, url, ext):
            continue
        identity = url_hash(probe_url)
        if identity in seen:
            continue
        seen.add(identity)
        attrs = row.get("attrs") if isinstance(row.get("attrs"), dict) else {}
        file_title = _filename_title_from_url(probe_url)
        link_title = normalized_query(first_text(attrs.get("data-title"), attrs.get("title"), row.get("title")))
        title = first_text(file_title if ext else "", context_title, "" if link_title.lower() in GENERIC_DIRECT_FILE_LINK_TITLES else link_title, file_title, "direct file")
        size = int_value(
            first_value(
                attrs.get("data-size"),
                attrs.get("data-filesize"),
                attrs.get("data-file-size"),
                attrs.get("data-length"),
                attrs.get("length"),
                attrs.get("size"),
            ),
            None,
        )
        content_type = first_text(attrs.get("type"), attrs.get("data-type"), attrs.get("data-content-type"))
        rows.append(
            {
                "title": normalized_query(title),
                "summary": file_title or link_title,
                "source_site": source_site,
                "source_url": source_url,
                "download_url": probe_url,
                "download_url_hash": identity,
                "source_link_url_hash": url_hash(url) if probe_url != url else "",
                "shared_file_host": _shared_file_host_name(url, shared_file_hosts, shared_file_host_rules),
                "guid": identity,
                "extension": ext,
                "content_type": content_type or content_type_for_extension(ext),
                "size_bytes": size,
            }
        )
    return rows


GENERIC_TORRENT_LINK_TITLES = {
    "download",
    "download torrent",
    "magnet",
    "magnet link",
    "torrent",
    "torrent link",
}


def _torrent_detail_context_title(page):
    if isinstance(page, dict):
        for key in ("title", "result_title", "page_title", "name", "label"):
            value = first_text(page.get(key))
            if value:
                return normalized_query(value)
    return normalized_query(_filename_title_from_url(_payload_source_url(page, "")))


def _torrent_info_hash_rows_from_text(text, source_url="", source_site="", policy=None, context_title=""):
    context_title = normalized_query(context_title)
    if not context_title:
        return []
    policy = policy if isinstance(policy, dict) else {}
    source_categories = category_ids(policy.get("categories") or policy.get("comic_categories") or [])
    visible = _visible_text_from_html(text)
    rows = []
    seen = set()
    for match in TORRENT_INFO_HASH_LABEL_RE.finditer(visible):
        info_hash = str(match.group(1) or "").strip()
        identity = info_hash
        if not identity or identity in seen:
            continue
        seen.add(identity)
        rows.append(
            {
                "title": context_title,
                "protocol": "torrent",
                "indexer": source_site,
                "indexerName": source_site,
                "categories": source_categories,
                "seeders": _seeders_near_text(visible, match.start(), match.end()),
                "guid": identity,
                "infoUrl": source_url,
                "infoHash": info_hash,
                "extension": normalize_extension(context_title),
            }
        )
    return rows


def _torrent_embedded_locator_rows(markup, source_url="", source_site="", policy=None, context_title=""):
    rows = []
    seen = set()
    for row in _torrent_html_rows_from_html(markup, source_url=source_url, source_site=source_site, policy=policy):
        locator = first_text(row.get("magnetUrl"), row.get("magnet_url"), row.get("downloadUrl"), row.get("download_url"), row.get("infoHash"), row.get("info_hash"))
        if not locator or locator in seen:
            continue
        seen.add(locator)
        rows.append(row)
    for row in _torrent_info_hash_rows_from_text(markup, source_url=source_url, source_site=source_site, policy=policy, context_title=context_title):
        locator = first_text(row.get("infoHash"), row.get("info_hash"), row.get("guid"))
        if not locator or locator in seen:
            continue
        seen.add(locator)
        rows.append(row)
    return rows


TORRENT_INFO_HASH_RESULT_TAGS = {"article", "li", "tr"}
TORRENT_INFO_HASH_BLOCK_RE = re.compile(
    r"(?i)\b(?:book|entry|item|release|result|search[-_\s]?result|torrent)\b"
)


def _is_torrent_info_hash_result_block(tag, attrs):
    tag = str(tag or "").lower()
    if tag in TORRENT_INFO_HASH_RESULT_TAGS:
        return True
    if tag not in {"div", "section"}:
        return False
    attrs = attrs if isinstance(attrs, dict) else {}
    haystack = " ".join(str(attrs.get(key) or "") for key in ("class", "id", "data-type", "role"))
    return bool(TORRENT_INFO_HASH_BLOCK_RE.search(haystack))


class _TorrentInfoHashBlockRowsFromHtml(html.parser.HTMLParser):
    def __init__(self, base_url, source_site="", policy=None):
        super().__init__()
        self.base_url = str(base_url or "").strip()
        self.source_site = str(source_site or "").strip()
        self.policy = policy if isinstance(policy, dict) else {}
        self.rows = []
        self._blocks = []
        self._link = None

    def _emit_block_rows(self, block):
        text = normalized_query(" ".join(block.get("text") or []))
        if not text:
            return
        source_categories = category_ids(self.policy.get("categories") or self.policy.get("comic_categories") or [])
        links = [link for link in block.get("links") or [] if normalized_query(link.get("title"))]
        links = [
            link
            for link in links
            if normalized_query(link.get("title")).lower() not in GENERIC_TORRENT_LINK_TITLES
            and not re.search(r"(?i)\b(login|privacy|contact|dmca|terms|about|next|previous)\b", normalized_query(link.get("title")))
        ]
        title = normalized_query(first_text(*(link.get("title") for link in links)))
        if not title:
            title = normalized_query(re.split(r"(?i)\b(?:info\s*hash|infohash|torrent\s*hash|btih)\b", text, maxsplit=1)[0])
        title = normalized_query(title)
        if not title or title.lower() in GENERIC_TORRENT_LINK_TITLES:
            return
        info_url = first_text(*(link.get("url") for link in links), self.base_url)
        for match in TORRENT_INFO_HASH_LABEL_RE.finditer(text):
            info_hash = str(match.group(1) or "").strip()
            if not info_hash:
                continue
            self.rows.append(
                {
                    "title": title,
                    "protocol": "torrent",
                    "indexer": self.source_site,
                    "indexerName": self.source_site,
                    "categories": source_categories,
                    "seeders": _seeders_near_text(text, match.start(), match.end()),
                    "guid": info_hash,
                    "infoUrl": info_url,
                    "infoHash": info_hash,
                    "extension": normalize_extension(title),
                }
            )

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        attrs = {str(key or "").lower(): value for key, value in (attrs or [])}
        if _is_torrent_info_hash_result_block(tag, attrs):
            self._blocks.append({"tag": tag, "attrs": attrs, "text": [], "links": []})
        if tag == "a":
            href = str(attrs.get("href") or "").strip()
            self._link = {"url": urljoin(self.base_url, href) if href else "", "text": [], "attrs": attrs}

    def handle_data(self, data):
        text = str(data or "")
        if not text:
            return
        for block in self._blocks:
            block["text"].append(text)
        if self._link is not None:
            self._link["text"].append(text)

    def handle_endtag(self, tag):
        tag = str(tag or "").lower()
        if tag == "a" and self._link is not None:
            link = dict(self._link)
            link["title"] = normalized_query(first_text(" ".join(link.get("text") or []), _html_link_title_from_attrs(link.get("attrs"))))
            for block in self._blocks:
                block["links"].append(link)
            self._link = None
            return
        if not self._blocks:
            return
        if tag == self._blocks[-1].get("tag"):
            block = self._blocks.pop()
            self._emit_block_rows(block)


def _torrent_info_hash_block_rows_from_html(text, source_url="", source_site="", policy=None):
    parser = _TorrentInfoHashBlockRowsFromHtml(source_url, source_site=source_site, policy=policy)
    try:
        parser.feed(str(text or ""))
    except Exception:
        return []
    rows = []
    seen = set()
    for row in parser.rows:
        identity = first_text(row.get("infoHash"), row.get("guid"))
        title = normalized_query(row.get("title"))
        if not identity or not title:
            continue
        key = (identity, title)
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def _torrent_html_rows_from_html(text, source_url="", source_site="", policy=None):
    parser = _LinkRowsFromHtml(source_url)
    try:
        parser.feed(str(text or ""))
    except Exception:
        return []
    policy = policy if isinstance(policy, dict) else {}
    source_categories = category_ids(policy.get("categories") or policy.get("comic_categories") or [])
    rows = []
    seen = set()
    for row in parser.rows:
        locator = str(row.get("url") or "").strip()
        if not locator:
            continue
        parsed = urlparse(locator)
        scheme = str(parsed.scheme or "").lower()
        magnet_url = ""
        download_url = ""
        if scheme == "magnet":
            if _url_has_secretish_query(locator):
                continue
            magnet_url = locator
        elif scheme in {"http", "https"} and normalize_extension(locator) == ".torrent":
            if _url_has_secretish_query(locator):
                continue
            download_url = locator
        else:
            continue
        identity_source = magnet_url or download_url
        identity = _magnet_info_hash(magnet_url) or url_hash(identity_source)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        attrs = row.get("attrs") if isinstance(row.get("attrs"), dict) else {}
        file_title = _filename_title_from_url(download_url)
        title = normalized_query(
            first_text(
                row.get("title"),
                attrs.get("title"),
                attrs.get("data-title"),
                attrs.get("aria-label"),
                file_title,
            )
        )
        if not title or title.lower() in GENERIC_TORRENT_LINK_TITLES:
            title = normalized_query(first_text(attrs.get("data-title"), attrs.get("title"), file_title, "torrent candidate"))
        rows.append(
            {
                "title": title,
                "protocol": "torrent",
                "indexer": source_site,
                "indexerName": source_site,
                "categories": source_categories,
                "seeders": int_value(first_value(attrs.get("data-seeders"), attrs.get("data-seeds"), attrs.get("seeders"), attrs.get("seeds")), None),
                "leechers": int_value(first_value(attrs.get("data-leechers"), attrs.get("data-peers"), attrs.get("leechers"), attrs.get("peers")), None),
                "size": int_value(first_value(attrs.get("data-size"), attrs.get("data-filesize"), attrs.get("size"), attrs.get("length")), None),
                "guid": identity,
                "infoUrl": source_url,
                "downloadUrl": download_url,
                "magnetUrl": magnet_url,
                "infoHash": _magnet_info_hash(magnet_url),
                "extension": normalize_extension(title),
            }
        )
    for row in _torrent_info_hash_block_rows_from_html(text, source_url=source_url, source_site=source_site, policy=policy):
        identity = first_text(row.get("infoHash"), row.get("guid"))
        if not identity or identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    return rows


def _json_content_type(row):
    row = row if isinstance(row, dict) else {}
    return content_type_base(
        first_text(
            row.get("content_type"),
            row.get("contentType"),
            row.get("mime_type"),
            row.get("mimeType"),
            row.get("mime"),
            row.get("media_type"),
            row.get("mediaType"),
        )
    )


def _json_candidate_dicts(value, depth=0, max_depth=6):
    if depth > max_depth:
        return []
    rows = []
    if isinstance(value, dict):
        rows.append(value)
        for child in value.values():
            if isinstance(child, (dict, list)):
                rows.extend(_json_candidate_dicts(child, depth + 1, max_depth=max_depth))
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, (dict, list)):
                rows.extend(_json_candidate_dicts(child, depth + 1, max_depth=max_depth))
    return rows


def _json_direct_url_info(value, *, source_url="", content_type="", extension="", allowed_extensions=None):
    raw_url = first_text(value)
    if not raw_url:
        return {}
    url = urljoin(str(source_url or ""), raw_url)
    if _url_has_secretish_query(url):
        return {}
    parsed = urlparse(url)
    scheme = str(parsed.scheme or "").lower()
    if scheme and scheme not in {"http", "https"}:
        return {}
    allowed = set(normalized_extensions(allowed_extensions or GENERIC_JSON_DIRECT_EXTENSIONS))
    content_type = content_type_base(content_type)
    ext = normalize_extension(first_value(extension, url))
    if not ext and content_type:
        ext = extension_for_content_type(content_type)
    if not ext or (allowed and ext not in allowed):
        return {}
    return {
        "download_url": url,
        "extension": ext,
        "content_type": content_type or content_type_for_extension(ext),
    }


def _json_link_rows(row):
    links = []
    row = row if isinstance(row, dict) else {}
    for key in ("links", "link", "downloads", "files"):
        value = row.get(key)
        if isinstance(value, dict):
            links.append(value)
        elif isinstance(value, list):
            links.extend([item for item in value if isinstance(item, dict)])
    formats = row.get("formats")
    if isinstance(formats, dict):
        for content_type, url in formats.items():
            if isinstance(url, str):
                links.append({"href": url, "type": content_type, "rel": "download"})
    return links


def _json_direct_url_from_row(row, source_url="", allowed_extensions=None):
    row = row if isinstance(row, dict) else {}
    content_type = _json_content_type(row)
    extension = first_text(row.get("extension"), row.get("ext"), row.get("file_extension"), row.get("fileExtension"))
    for key in JSON_DIRECT_URL_KEYS:
        raw_url = first_text(row.get(key))
        if key in JSON_DIRECT_GENERIC_URL_KEYS and raw_url and not normalize_extension(raw_url) and not content_type:
            continue
        info = _json_direct_url_info(
            row.get(key),
            source_url=source_url,
            content_type=content_type,
            extension=first_text(
                extension,
                row.get("file_name"),
                row.get("fileName"),
                row.get("filename"),
                row.get("name") if key not in JSON_DIRECT_GENERIC_URL_KEYS else "",
                row.get("title") if key not in JSON_DIRECT_GENERIC_URL_KEYS else "",
            ),
            allowed_extensions=allowed_extensions,
        )
        if info:
            return info
    for link in _json_link_rows(row):
        rel = " ".join(text_values(first_value(link.get("rel"), link.get("relationship")))).lower()
        href = first_text(link.get("href"), link.get("url"), link.get("download_url"), link.get("downloadUrl"))
        link_type = content_type_base(first_text(link.get("type"), link.get("content_type"), link.get("contentType")))
        info = _json_direct_url_info(
            href,
            source_url=source_url,
            content_type=link_type,
            extension=first_text(link.get("extension"), link.get("file_name"), link.get("fileName"), href),
            allowed_extensions=allowed_extensions,
        )
        if not info:
            continue
        if any(token in rel for token in JSON_DIRECT_LINK_REL_TOKENS) or normalize_extension(href):
            return info
    return {}


JSON_DIRECT_HTML_KEYS = ("content", "excerpt", "description", "summary", "body", "html", "rendered")
JSON_DIRECT_TITLE_KEYS = ("title", "name", "headline", "post_title", "label")


def _json_html_values(value):
    out = []
    if value in (None, ""):
        return out
    if isinstance(value, str):
        text = value.strip()
        if "<" in text and (">" in text or "&lt;" in text):
            out.append(text)
        return out
    if isinstance(value, dict):
        for key in ("rendered", "html", "content", "body", "text"):
            out.extend(_json_html_values(value.get(key)))
        return out
    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_json_html_values(item))
    return out


def _json_direct_rows_from_html_fields(row, source_url="", source_site="", allowed_extensions=None):
    row = row if isinstance(row, dict) else {}
    source = first_text(*(row.get(key) for key in JSON_SOURCE_URL_KEYS), source_url)
    parent_title = normalized_query(first_text(*(_json_text_field(row.get(key)) for key in JSON_DIRECT_TITLE_KEYS)))
    parent_summary = first_text(row.get("summary"), row.get("description"), row.get("desc"))
    rows = []
    for key in JSON_DIRECT_HTML_KEYS:
        for markup in _json_html_values(row.get(key)):
            for direct_row in _direct_file_rows_from_html(
                markup,
                source_url=source,
                source_site=source_site,
                allowed_extensions=allowed_extensions,
            ):
                merged = dict(direct_row)
                merged["title"] = normalized_query(first_text(parent_title, direct_row.get("title")))
                merged["summary"] = first_text(direct_row.get("summary"), parent_summary)
                merged["description"] = first_text(parent_summary, direct_row.get("summary"))
                merged["source_url"] = source
                merged["guid"] = first_text(row.get("guid"), row.get("id"), direct_row.get("guid"))
                merged["creator"] = first_text(row.get("creator"), row.get("author"), row.get("artist"), row.get("publisher"))
                merged["language"] = first_text(row.get("language"), row.get("translated_language"), row.get("translatedLanguage"))
                merged["rights_status"] = first_text(row.get("rights_status"), row.get("rightsStatus"), row.get("license"))
                merged["license_url"] = first_text(row.get("license_url"), row.get("licenseUrl"))
                merged["json_html_field"] = key
                rows.append(merged)
    return rows


def _json_direct_rows_from_payload(payload, source_url="", source_site="", allowed_extensions=None):
    rows = []
    seen = set()
    for row in _json_candidate_dicts(payload):
        candidate_rows = []
        info = _json_direct_url_from_row(row, source_url=source_url, allowed_extensions=allowed_extensions)
        download_url = info.get("download_url")
        if download_url:
            title = normalized_query(
                first_text(
                    _json_text_field(row.get("title")),
                    row.get("name"),
                    row.get("release_title"),
                    row.get("releaseTitle"),
                    row.get("file_name"),
                    row.get("fileName"),
                    row.get("filename"),
                    _filename_title_from_url(download_url),
                )
            )
            source = first_text(*(row.get(key) for key in JSON_SOURCE_URL_KEYS), source_url)
            candidate_rows.append(
                {
                    "title": title or _filename_title_from_url(download_url) or "direct file",
                    "summary": first_text(row.get("summary"), row.get("description"), row.get("desc")),
                    "description": first_text(row.get("description"), row.get("summary"), row.get("desc")),
                    "source_site": source_site,
                    "source_url": source,
                    "download_url": download_url,
                    "guid": first_text(row.get("guid"), row.get("id"), row.get("identifier"), url_hash(download_url)),
                    "extension": info.get("extension"),
                    "content_type": info.get("content_type"),
                    "size_bytes": int_value(first_value(row.get("size_bytes"), row.get("sizeBytes"), row.get("size"), row.get("length"), row.get("fileSize")), None),
                    "creator": first_text(row.get("creator"), row.get("author"), row.get("artist"), row.get("publisher")),
                    "language": first_text(row.get("language"), row.get("translated_language"), row.get("translatedLanguage")),
                    "rights_status": first_text(row.get("rights_status"), row.get("rightsStatus"), row.get("license")),
                    "license_url": first_text(row.get("license_url"), row.get("licenseUrl")),
                }
            )
        candidate_rows.extend(
            _json_direct_rows_from_html_fields(
                row,
                source_url=source_url,
                source_site=source_site,
                allowed_extensions=allowed_extensions,
            )
        )
        for candidate_row in candidate_rows:
            download_url = first_text(candidate_row.get("download_url"), candidate_row.get("url"))
            identity = url_hash(download_url)
            if not download_url or identity in seen:
                continue
            seen.add(identity)
            rows.append(candidate_row)
    return rows


def _query_matches_result(result, wanted_item=None, policy=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = policy if isinstance(policy, dict) else {}
    ignored_series_tokens = {"the", "an", "and", "of"}
    series = normalized_query(
        first_text(
            wanted_item.get("series_title"),
            wanted_item.get("series"),
            wanted_item.get("manga_title"),
            wanted_item.get("title"),
        )
    ).lower()
    query = normalized_query(
        first_text(
            wanted_item.get("query"),
            wanted_item.get("series_title"),
            wanted_item.get("series"),
            wanted_item.get("title"),
        )
    ).lower()
    if not query:
        return True
    haystack = normalized_query(
        " ".join(
            str(result.get(key) or "")
            for key in ("title", "summary", "description")
        )
    ).lower()

    def manual_semantic_conflict() -> bool:
        return bool(
            wanted_item.get("manual_search")
            and (
                re.search(r"(?i)\b(?:soundtracks?|ost|music|audio|flac|mp3)\b", haystack)
                or re.search(r"(?i)\bdetective\s+comics\b", haystack)
                or re.search(r"(?i)\b(?:monthly|new\s+series|ongoing\s+series)\b", haystack)
            )
        )

    def manual_edition_alias_allowed() -> bool:
        if not wanted_item.get("manual_search"):
            return True
        target = normalized_query(series)
        match = re.match(r"(?i)^absolute\s+([^:]+):\s+.+$", target)
        if not match:
            return True
        if manual_semantic_conflict():
            return False
        if re.search(r"(?i)\babsolute\b", haystack):
            return True
        franchise_tokens = _query_tokens(match.group(1), ignored={"the", "an", "and", "of"})
        if not franchise_tokens or not all(token in _query_tokens(haystack) for token in franchise_tokens):
            return False
        # Prefixless matches for an Absolute/collected work must themselves
        # carry collection/volume evidence.  This prevents a newer monthly
        # series with the same storyline subtitle from becoming a match.
        explicit_collection = re.search(
            r"(?i)\b(?:omnibus|saga|collection|collected|compendium|deluxe|v\s*0*\d+|vol(?:ume)?\.?\s*0*\d+)\b",
            haystack,
        )
        range_pack = re.search(r"(?<!\d)0*\d{1,4}\s*[-–—]\s*0*\d{1,4}(?!\d)", haystack) and re.search(
            r"(?i)\b(?:pack|bundle)\b", haystack
        )
        return bool(explicit_collection or range_pack)
    if manual_semantic_conflict():
        return False
    if _text_contains_query_tokens(query, haystack):
        if series and not _text_contains_numbered_series_sequence(series, haystack, ignored=ignored_series_tokens):
            return False
        return True
    for alias in _series_query_aliases(wanted_item, policy=policy):
        if not _text_contains_query_tokens(alias, haystack, ignored=ignored_series_tokens):
            continue
        if not _text_contains_numbered_series_sequence(alias, haystack, ignored=ignored_series_tokens):
            continue
        if not manual_edition_alias_allowed():
            continue
        if _wanted_has_number(wanted_item):
            if _indexer_title_has_wanted_number(haystack, wanted_item):
                return True
        else:
            return True
    series_tokens = _query_tokens(series, ignored=ignored_series_tokens)
    if (
        len(series_tokens) >= 2
        and _text_contains_query_tokens(series, haystack, ignored=ignored_series_tokens)
        and _text_contains_numbered_series_sequence(series, haystack, ignored=ignored_series_tokens)
        and _indexer_title_has_wanted_number(haystack, wanted_item)
    ):
        return True
    unit_metadata = _wanted_unit_metadata(wanted_item)
    manifest_candidate = dict(result or {})
    manifest_candidate["series_title"] = first_text(wanted_item.get("series_title"), wanted_item.get("series"), wanted_item.get("title"), manifest_candidate.get("series_title"))
    manifest_candidate["issue_number"] = first_text(unit_metadata.get("issue_number"), manifest_candidate.get("issue_number"))
    manifest_candidate["chapter_number"] = first_text(unit_metadata.get("chapter_number"), manifest_candidate.get("chapter_number"))
    manifest_candidate["volume_number"] = first_text(unit_metadata.get("volume_number"), manifest_candidate.get("volume_number"))
    manifest_candidate["unit_type"] = first_text(unit_metadata.get("unit_type"), manifest_candidate.get("unit_type"))
    manifest_candidate["metadata_provider"] = first_text(wanted_item.get("metadata_provider"), manifest_candidate.get("metadata_provider"))
    manifest_candidate["series_source"] = first_text(wanted_item.get("series_source"), wanted_item.get("source"), manifest_candidate.get("series_source"))
    manifest_candidate["media_type"] = first_text(wanted_item.get("media_type"), manifest_candidate.get("media_type"))
    manifest_candidate["publisher"] = first_text(wanted_item.get("publisher"), wanted_item.get("watch_publisher"), manifest_candidate.get("publisher"))
    manifest_candidate["issue_title"] = first_text(wanted_item.get("issue_title"), wanted_item.get("issueTitle"), wanted_item.get("title"), manifest_candidate.get("issue_title"))
    return bool(indexer_manifest_pack_match(manifest_candidate))


def _candidate_result_relevant(result, wanted_item=None, policy=None):
    """Retain same-series numeric mismatches so the strict gate can explain them."""
    if _query_matches_result(result, wanted_item, policy=policy):
        return True
    if inkdrop_candidate_matching.collected_singleton_alias_exact_title_match(result, wanted_item):
        return True
    if not _wanted_has_number(wanted_item):
        return False
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    if wanted_item.get("manual_search"):
        haystack = normalized_query(
            " ".join(str((result or {}).get(key) or "") for key in ("title", "summary", "description"))
        ).lower()
        series = normalized_query(
            first_text(wanted_item.get("series_title"), wanted_item.get("series"), wanted_item.get("title"))
        )
        edition_match = re.match(r"(?i)^absolute\s+([^:]+):\s+.+$", series)
        edition_alias = bool(edition_match)
        franchise_tokens = _query_tokens(edition_match.group(1), ignored={"the", "an", "and", "of"}) if edition_match else []
        franchise_match = bool(franchise_tokens) and all(token in _query_tokens(haystack) for token in franchise_tokens)
        semantic_conflict = bool(
            re.search(r"(?i)\b(?:soundtracks?|ost|music|audio|flac|mp3)\b", haystack)
            or re.search(r"(?i)\bdetective\s+comics\b", haystack)
            or re.search(r"(?i)\b(?:monthly|new\s+series|ongoing\s+series)\b", haystack)
        )
        if semantic_conflict:
            return False
        collection = bool(
            re.search(
                r"(?i)\b(?:omnibus|saga|collection|collected|compendium|deluxe|v\s*0*\d+|vol(?:ume)?\.?\s*0*\d+)\b",
                haystack,
            )
            or (
                re.search(r"(?<!\d)0*\d{1,4}\s*[-–—]\s*0*\d{1,4}(?!\d)", haystack)
                and re.search(r"(?i)\b(?:pack|bundle)\b", haystack)
            )
        )
        for alias in _series_query_aliases(wanted_item, policy=policy):
            if (
                _text_contains_query_tokens(alias, haystack, ignored={"the", "an", "and", "of"})
                and _text_contains_numbered_series_sequence(alias, haystack, ignored={"the", "an", "and", "of"})
                and (not edition_alias or "absolute" in haystack or (collection and franchise_match))
            ):
                return True
    series = normalized_query(
        first_text(
            wanted_item.get("series_title"),
            wanted_item.get("series"),
            wanted_item.get("manga_title"),
            wanted_item.get("title"),
        )
    )
    if not series:
        return False
    series_only = {
        "series_title": series,
        "series": series,
        "title": series,
        "query": series,
    }
    return _query_matches_result(result, series_only, policy=policy)


def rss_feed_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = [row for row in payload.get("items") if isinstance(row, dict)]
    else:
        rows = _rss_rows_from_xml(_payload_text(payload))
    out = []
    for row in rows:
        if not _query_matches_result(row, wanted_item):
            continue
        result = dict(row)
        result["source_site"] = result.get("source_site") or (registry_row or {}).get("display_name") or "RSS feed"
        out.append(
            manual_source_card_from_result(
                result,
                registry_row,
                wanted_item,
                source_bucket=(registry_row or {}).get("provider_id") or "rss_feed",
            )
        )
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def direct_rss_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = [row for row in payload.get("items") if isinstance(row, dict)]
    else:
        rows = _direct_rss_rows_from_xml(_payload_text(payload), _payload_source_url(payload, ""))
    out = []
    provider_id = registry_row.get("provider_id") or "generic_rss_direct_feed"
    for row in rows:
        if not _query_matches_result(row, wanted_item):
            continue
        download_url = first_text(row.get("download_url"), row.get("url"))
        if not download_url or _url_has_secretish_query(download_url):
            continue
        title = normalized_query(first_text(row.get("title"), wanted_item.get("title") if isinstance(wanted_item, dict) else "", download_url))
        candidate = source_candidate(
            provider_id=provider_id,
            provider_type=registry_row.get("provider_type") or "direct_download",
            source_kind=registry_row.get("source_kind") or "rss_direct_feed",
            canonical_item_id=first_text(row.get("guid"), row.get("id"), download_url),
            canonical_work_id=first_text(row.get("series_id"), row.get("work_id")),
            title=title,
            series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), title),
            creator=first_text(row.get("creator"), row.get("author"), row.get("publisher")),
            language=first_text(row.get("language"), row.get("translated_language")).lower(),
            source_url=first_text(row.get("source_url"), row.get("page_url"), row.get("link")),
            download_url=download_url,
            extension=normalize_extension(first_value(row.get("extension"), row.get("file_name"), row.get("filename"), title, download_url)),
            content_type=content_type_base(row.get("content_type")),
            size_bytes=int_value(first_value(row.get("size_bytes"), row.get("size")), None),
            rights_status=first_text(row.get("rights_status"), policy.get("rights_status"), "provider_specific"),
            license_url=first_text(row.get("license_url"), policy.get("license_url")),
            wanted_item=wanted_item,
            raw={
                "result": {
                    "title": title,
                    "guid": row.get("guid"),
                    "published": row.get("published"),
                    "summary": clipped_text(row.get("summary"), 1000),
                    "download_url_hash": url_hash(download_url),
                    "source_url_hash": url_hash(first_text(row.get("source_url"), row.get("page_url"), row.get("link"))),
                },
                "rss_direct_feed": True,
            },
        )
        candidate["pack"] = looks_pack_like(title)
        if candidate["pack"] and not bool(policy.get("packs_allowed") or policy.get("allow_packs") or policy.get("pack_auto_allowed")):
            candidate["requires_manual_review"] = True
        candidate["language_status"] = _direct_language_status(candidate, policy)
        candidate["quality_profile"] = _direct_quality_profile(candidate)
        candidate["quality"] = candidate["quality_profile"]
        candidate["match_confidence"] = "title_match"
        out.append(candidate)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def direct_file_html_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    allowed_extensions = policy.get("allowed_extensions") or GENERIC_DIRECT_FILE_EXTENSIONS
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = [row for row in payload.get("items") if isinstance(row, dict)]
    else:
        rows = _direct_file_rows_from_html(
            _payload_text(payload),
            _payload_source_url(payload, ""),
            first_text(policy.get("source_site_label"), registry_row.get("display_name"), registry_row.get("provider_id"), "Direct file source"),
            allowed_extensions=allowed_extensions,
        )
    out = []
    provider_id = registry_row.get("provider_id") or "generic_direct_file_search"
    for row in rows:
        if not _query_matches_result(row, wanted_item):
            continue
        download_url = first_text(row.get("download_url"), row.get("url"))
        if not download_url or _url_has_secretish_query(download_url):
            continue
        ext = normalize_extension(first_value(row.get("extension"), row.get("file_name"), row.get("filename"), row.get("title"), download_url))
        if not ext:
            continue
        title = normalized_query(first_text(row.get("title"), _filename_title_from_url(download_url), wanted_item.get("title"), download_url))
        source_url = first_text(row.get("source_url"), row.get("page_url"), _payload_source_url(payload, ""))
        candidate = source_candidate(
            provider_id=provider_id,
            provider_type=registry_row.get("provider_type") or "direct_download",
            source_kind=registry_row.get("source_kind") or "direct_file_html_search",
            canonical_item_id=first_text(row.get("guid"), row.get("id"), download_url),
            canonical_work_id=first_text(row.get("series_id"), row.get("work_id")),
            title=title,
            series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), title),
            creator=first_text(row.get("creator"), row.get("author"), row.get("publisher")),
            language=first_text(row.get("language"), row.get("translated_language")).lower(),
            source_url=source_url,
            download_url=download_url,
            extension=ext,
            content_type=content_type_base(row.get("content_type")) or content_type_for_extension(ext),
            size_bytes=int_value(first_value(row.get("size_bytes"), row.get("size")), None),
            rights_status=first_text(row.get("rights_status"), policy.get("rights_status"), "provider_specific"),
            license_url=first_text(row.get("license_url"), policy.get("license_url")),
            wanted_item=wanted_item,
            raw={
                "result": {
                    "title": title,
                    "guid": row.get("guid"),
                    "summary": clipped_text(row.get("summary"), 1000),
                    "download_url_hash": url_hash(download_url),
                    "source_link_url_hash": row.get("source_link_url_hash"),
                    "structured_data": bool(row.get("structured_data")),
                    "source_url_hash": url_hash(source_url),
                },
                "direct_file_html_search": True,
            },
        )
        candidate["source_site"] = first_text(row.get("source_site"), policy.get("source_site_label"), registry_row.get("display_name"))
        candidate["pack"] = looks_pack_like(title)
        if candidate["pack"] and not bool(policy.get("packs_allowed") or policy.get("allow_packs") or policy.get("pack_auto_allowed")):
            candidate["requires_manual_review"] = True
        candidate["language_status"] = _direct_language_status(candidate, policy)
        candidate["quality_profile"] = _direct_quality_profile(candidate)
        candidate["quality"] = candidate["quality_profile"]
        candidate["match_confidence"] = "title_match"
        out.append(candidate)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def direct_file_detail_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    pages = []
    if isinstance(payload, dict):
        for key in ("search_pages", "detail_pages", "pages"):
            value = payload.get(key)
            if isinstance(value, list):
                pages.extend(page for page in value if page not in (None, "", {}, []))
        if not pages:
            pages.append(payload)
    elif isinstance(payload, list):
        pages = [page for page in payload if page not in (None, "", {}, [])]
    else:
        pages = [payload]
    out = []
    seen = set()
    for page in pages:
        remaining = max(0, int(limit or 0)) - len(out)
        if remaining <= 0:
            break
        for candidate in direct_file_html_candidates_from_payload(page, registry_row, wanted_item, limit=remaining):
            identity = candidate.get("candidate_identity") or candidate.get("download_url_hash") or candidate.get("canonical_item_id")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            candidate["source_kind"] = registry_row.get("source_kind") or "direct_file_detail_search"
            raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
            raw["direct_file_detail_search"] = True
            candidate["raw"] = raw
            out.append(candidate)
            if len(out) >= max(0, int(limit or 0)):
                break
    return out


def _direct_file_probe_context_title(page, wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    if isinstance(page, dict):
        for key in ("title", "result_title", "page_title", "name", "label"):
            value = first_text(page.get(key))
            if value:
                return normalized_query(value)
    source_url = _payload_source_url(page, "")
    return normalized_query(first_text(_filename_title_from_url(source_url), wanted_item.get("title"), wanted_item.get("series_title")))


def _headers_for_probe_url(payload, url):
    payload = payload if isinstance(payload, dict) else {}
    headers_by_hash = payload.get("probe_headers") if isinstance(payload.get("probe_headers"), dict) else {}
    headers_by_url = payload.get("probe_headers_by_url") if isinstance(payload.get("probe_headers_by_url"), dict) else {}
    url = str(url or "").strip()
    for key in (url_hash(url), url):
        value = headers_by_hash.get(key) or headers_by_url.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _probe_status_for_url(payload, url):
    payload = payload if isinstance(payload, dict) else {}
    statuses = payload.get("probe_status") if isinstance(payload.get("probe_status"), dict) else {}
    value = statuses.get(url_hash(url))
    return int_value(value, None)


def direct_file_probe_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    provider_id = registry_row.get("provider_id") or "generic_direct_file_probe_source"
    policy = provider_policy(registry_row)
    allowed_extensions = policy.get("allowed_extensions") or GENERIC_DIRECT_FILE_EXTENSIONS
    shared_file_hosts = policy.get("shared_file_hosts")
    if shared_file_hosts is None:
        shared_file_hosts = policy.get("allowed_shared_file_hosts")
    if shared_file_hosts is None:
        shared_file_hosts = GENERIC_SHARED_FILE_HOSTS
    shared_file_host_rules = first_value(
        policy.get("shared_file_host_rules"),
        policy.get("shared_file_host_rewrite_rules"),
        policy.get("shared_file_hosts_rules"),
    )
    if provider_id == "rss_getcomics":
        allowed_extensions = [".cbz", ".zip"]
        shared_file_hosts = ["pixeldrain"]
        shared_file_host_rules = None
    transport_allowed_hosts = {
        str(urlparse(str(value or "")).hostname or value or "").strip().lower().strip("[]")
        for value in text_values(
            policy.get("transport_allowed_hosts")
            or policy.get("direct_download_allowed_hosts")
            or policy.get("direct_allowed_hosts")
            or []
        )
        if str(value or "").strip()
    }
    if provider_id == "rss_getcomics":
        transport_allowed_hosts = {"pixeldrain.com", "www.pixeldrain.com"}
    source_site = first_text(policy.get("source_site_label"), registry_row.get("display_name"), registry_row.get("provider_id"), "Direct file probe source")
    pages = []
    if isinstance(payload, dict):
        pages.extend(page for page in payload.get("search_pages") or [] if isinstance(page, dict))
        pages.extend(page for page in payload.get("detail_pages") or [] if isinstance(page, dict))
        pages.extend(page for page in payload.get("pages") or [] if isinstance(page, dict))
    if not pages:
        pages = [payload]
    out = []
    seen = set()
    for page in pages:
        source_url = _payload_source_url(page, _payload_source_url(payload, first_text(registry_row.get("base_url"))))
        context_title = _direct_file_probe_context_title(page, wanted_item)
        rows = _direct_file_probe_rows_from_html(
            _payload_text(page),
            source_url=source_url,
            source_site=source_site,
            allowed_extensions=allowed_extensions,
            context_title=context_title,
            shared_file_hosts=shared_file_hosts,
            shared_file_host_rules=shared_file_host_rules,
        )
        for row in rows:
            download_url = first_text(row.get("download_url"), row.get("url"))
            if not download_url:
                continue
            download_host = str(urlparse(download_url).hostname or "").strip().lower()
            if transport_allowed_hosts and download_host not in transport_allowed_hosts:
                continue
            if row.get("download_url_hash") in seen:
                continue
            if not _query_matches_result({"title": row.get("title"), "url": row.get("source_url")}, wanted_item):
                continue
            seen.add(row.get("download_url_hash"))
            headers = _headers_for_probe_url(payload, download_url)
            status_code = _probe_status_for_url(payload, download_url)
            ext = _extension_from_candidate_or_headers(row, headers)
            content_type = _content_type_from_candidate_or_headers(row, headers)
            size_bytes = _size_from_candidate_or_headers(row, headers)
            title = normalized_query(first_text(row.get("title"), context_title, _filename_title_from_url(download_url), wanted_item.get("title")))
            candidate = source_candidate(
                provider_id=provider_id,
                provider_type=registry_row.get("provider_type") or "direct_download",
                source_kind=registry_row.get("source_kind") or "direct_file_probe_source",
                canonical_item_id=row.get("download_url_hash") or url_hash(download_url),
                canonical_work_id=first_text(wanted_item.get("series_id"), wanted_item.get("series_title")),
                title=title,
                series_title=first_text(wanted_item.get("series_title"), wanted_item.get("title"), title),
                source_url=source_url,
                download_url=download_url,
                extension=ext,
                content_type=content_type,
                size_bytes=size_bytes,
                rights_status=first_text(policy.get("rights_status"), "provider_specific"),
                license_url=first_text(policy.get("license_url")),
                wanted_item=wanted_item,
                raw={
                    "result": {
                        "title": title,
                        "source_url_hash": url_hash(source_url),
                        "download_url_hash": row.get("download_url_hash") or url_hash(download_url),
                        "source_link_url_hash": row.get("source_link_url_hash"),
                        "shared_file_host": row.get("shared_file_host"),
                        "content_type": content_type,
                        "content_length": _header_value(headers, "content-length"),
                        "content_disposition_present": bool(_header_value(headers, "content-disposition")),
                        "redirect_location_present": bool(_probe_redirect_url(headers)),
                        "redirect_url_hash": url_hash(_probe_redirect_url(headers)),
                        "status_code": status_code,
                    },
                    "direct_file_probe_source": True,
                },
            )
            candidate["candidate_identity"] = row.get("download_url_hash") or candidate_identity(candidate)
            candidate["download_url_hash"] = row.get("download_url_hash") or url_hash(download_url)
            candidate["probe_status_code"] = status_code
            candidate["discovery_provider_id"] = provider_id
            candidate["transport_id"] = row.get("shared_file_host") or "direct_http"
            transport_hosts = text_values(
                policy.get("transport_allowed_hosts")
                or policy.get("direct_download_allowed_hosts")
                or policy.get("direct_allowed_hosts")
                or []
            )
            if provider_id == "rss_getcomics":
                transport_hosts = ["pixeldrain.com", "www.pixeldrain.com"]
            candidate["transport_allowed_hosts"] = [
                str(value or "").strip().lower()
                for value in transport_hosts
                if str(value or "").strip()
            ]
            candidate["max_redirects"] = max(0, min(int_value(policy.get("max_redirects"), 5), 20))
            candidate["enforce_probe_size_match"] = provider_id == "rss_getcomics"
            candidate["match_confidence"] = "candidate"
            candidate["pack"] = looks_pack_like(title)
            out.append(candidate)
            if len(out) >= max(0, int(limit or 0)):
                return out
    return out


def _page_image_rows_from_html(text, source_url="", allowed_extensions=None, extract_script_image_urls=True):
    parser = _ImageRowsFromHtml(source_url)
    try:
        parser.feed(str(text or ""))
    except Exception:
        return []
    allowed = set(normalized_extensions(allowed_extensions or GENERIC_PAGE_IMAGE_EXTENSIONS))
    rows = []
    seen = set()
    source_rows = list(parser.rows)
    if extract_script_image_urls:
        source_rows.extend(_script_image_rows_from_text(text, source_url=source_url, allowed_extensions=allowed_extensions))
    for row in source_rows:
        url = str(row.get("url") or "").strip()
        if not url or _url_has_secretish_query(url):
            continue
        parsed = urlparse(url)
        if str(parsed.scheme or "").lower() not in {"http", "https"}:
            continue
        ext = normalize_extension(url)
        if not ext or (allowed and ext not in allowed):
            continue
        identity = url_hash(url)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(
            {
                "url": url,
                "url_hash": identity,
                "extension": ext,
                "title": row.get("title") or _filename_title_from_url(url),
                "attrs": row.get("attrs") if isinstance(row.get("attrs"), dict) else {},
            }
        )
    return rows


def _reader_page_title(payload, wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    if isinstance(payload, dict):
        for key in ("title", "chapter_title", "name", "label"):
            value = first_text(payload.get(key))
            if value:
                return normalized_query(value)
    source_url = _payload_source_url(payload, "")
    title = _filename_title_from_url(source_url)
    return normalized_query(first_text(title, wanted_item.get("title"), wanted_item.get("series_title"), "Reader page pack"))


def _page_pack_identity(provider_id, source_url, title, page_hashes):
    return inkdrop_sources.stable_id(
        "reader_page_pack",
        provider_id,
        url_hash(source_url),
        title,
        url_hash("\n".join(page_hashes or [])),
    )


def reader_page_pack_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    allowed_extensions = policy.get("allowed_extensions") or policy.get("allowed_image_extensions") or GENERIC_PAGE_IMAGE_EXTENSIONS
    extract_script_image_urls = policy.get("extract_script_image_urls", True)
    if str(extract_script_image_urls).strip().lower() in {"0", "false", "no", "off"}:
        extract_script_image_urls = False
    pages = []
    if isinstance(payload, dict):
        for key in ("reader_pages", "chapter_pages", "detail_pages", "pages"):
            value = payload.get(key)
            if isinstance(value, list):
                pages.extend(page for page in value if page not in (None, "", {}, []))
        if not pages:
            pages.append(payload)
    elif isinstance(payload, list):
        pages = [page for page in payload if page not in (None, "", {}, [])]
    else:
        pages = [payload]
    provider_id = registry_row.get("provider_id") or "generic_reader_page_pack_source"
    out = []
    seen = set()
    for page in pages:
        if len(out) >= max(0, int(limit or 0)):
            break
        source_url = _payload_source_url(page, _payload_source_url(payload, ""))
        image_rows = _page_image_rows_from_html(
            _payload_text(page),
            source_url=source_url,
            allowed_extensions=allowed_extensions,
            extract_script_image_urls=extract_script_image_urls,
        )
        if not image_rows:
            continue
        title = _reader_page_title(page, wanted_item)
        if not _query_matches_result({"title": title, "url": source_url}, wanted_item):
            continue
        page_urls = [row["url"] for row in image_rows]
        page_hashes = [row["url_hash"] for row in image_rows]
        identity = _page_pack_identity(provider_id, source_url, title, page_hashes)
        if identity in seen:
            continue
        seen.add(identity)
        candidate = source_candidate(
            provider_id=provider_id,
            provider_type=registry_row.get("provider_type") or "direct_download",
            source_kind=registry_row.get("source_kind") or "reader_page_pack_source",
            canonical_item_id=identity,
            canonical_work_id=first_text(wanted_item.get("series_id"), wanted_item.get("series_title")),
            title=title,
            series_title=first_text(wanted_item.get("series_title"), wanted_item.get("title"), title),
            language=first_text(wanted_item.get("language"), policy.get("language")),
            source_url=source_url,
            download_url="",
            extension=".cbz",
            content_type="application/vnd.comicbook+zip",
            rights_status=first_text(policy.get("rights_status"), "provider_specific"),
            license_url=first_text(policy.get("license_url")),
            wanted_item=wanted_item,
            raw={
                "result": {
                    "title": title,
                    "source_url_hash": url_hash(source_url),
                    "page_image_url_hashes": page_hashes,
                    "page_count": len(page_urls),
                },
                "reader_page_pack_source": True,
            },
        )
        candidate["candidate_identity"] = identity
        candidate["download_url_hash"] = url_hash("\n".join(page_urls))
        candidate["resolver_required"] = False
        candidate["page_count"] = len(page_urls)
        candidate["page_image_urls"] = page_urls
        candidate["page_image_url_hashes"] = page_hashes
        candidate["page_image_extensions"] = [row["extension"] for row in image_rows]
        candidate["source_site"] = first_text(policy.get("source_site_label"), registry_row.get("display_name"), registry_row.get("provider_id"))
        candidate["match_confidence"] = "candidate"
        candidate["pack"] = True
        out.append(candidate)
    return out


def _page_pack_policy(registry_row=None, candidate=None):
    policy = provider_policy(registry_row, candidate)
    allowed = normalized_extensions(
        policy.get("allowed_image_extensions")
        or (candidate or {}).get("page_image_extensions")
        or policy.get("allowed_extensions")
        or GENERIC_PAGE_IMAGE_EXTENSIONS
    )
    policy["allowed_image_extensions"] = allowed or list(GENERIC_PAGE_IMAGE_EXTENSIONS)
    return policy


def _declared_page_image_extension(candidate, index=0):
    candidate = candidate if isinstance(candidate, dict) else {}
    values = candidate.get("page_image_extensions") if isinstance(candidate.get("page_image_extensions"), list) else []
    if 0 <= int(index or 0) < len(values):
        ext = normalize_extension(values[int(index or 0)])
        if ext:
            return ext
    return normalize_extension(candidate.get("page_image_extension"))


def _page_image_extension_for_verdict(candidate, url, index=0):
    return normalize_extension(url) or _declared_page_image_extension(candidate, index=index)


def reader_page_pack_verdict(candidate, registry_row=None, headers=None):
    candidate = dict(candidate or {})
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    policy = _page_pack_policy(registry_row, candidate)
    block_reasons = []
    registry_state = str(registry_row.get("registry_state") or "").strip().lower()
    implementation_status = str(registry_row.get("implementation_status") or "implemented").strip().lower()
    if registry_row and implementation_status != "implemented":
        block_reasons.append("implementation_pending")
    if registry_state in {"blocked", "disabled", "metadata_only"}:
        block_reasons.append(f"registry_{registry_state}")
    page_urls = [str(url or "").strip() for url in candidate.get("page_image_urls") or [] if str(url or "").strip()]
    page_count = len(page_urls)
    min_pages = int_value(policy.get("min_page_images"), 2) or 2
    max_pages = int_value(policy.get("max_page_images"), 400) or 400
    if not page_urls:
        block_reasons.append("missing_page_images")
    elif page_count < min_pages:
        block_reasons.append("too_few_page_images")
    elif page_count > max_pages:
        block_reasons.append("too_many_page_images")
    allowed_exts = set(policy.get("allowed_image_extensions") or GENERIC_PAGE_IMAGE_EXTENSIONS)
    for index, url in enumerate(page_urls):
        parsed = urlparse(url)
        if str(parsed.scheme or "").lower() not in {"http", "https"}:
            block_reasons.append("unsupported_page_image_scheme")
            break
        if _url_has_secretish_query(url):
            block_reasons.append("secretish_page_image_url")
            break
        ext = _page_image_extension_for_verdict(candidate, url, index=index)
        if not ext or ext not in allowed_exts:
            block_reasons.append("unsupported_page_image_extension")
            break
    if not _rights_allowed(candidate, policy):
        block_reasons.append("rights_gate_failed")
    requires_manual = bool(registry_row.get("requires_manual_review") or candidate.get("requires_manual_review"))
    can_auto_download = bool(registry_row.get("auto_download_allowed")) if registry_row else True
    if requires_manual:
        block_reasons.append("manual_review_required")
    if registry_row and not can_auto_download:
        block_reasons.append("auto_download_not_allowed")
    candidate["allowed_image_extensions"] = sorted(allowed_exts)
    candidate["page_count"] = page_count
    candidate["block_reasons"] = block_reasons
    candidate["artifact_safe"] = not block_reasons
    if block_reasons:
        manual_only = "manual_review_required" in block_reasons or registry_state == "manual_review"
        candidate["auto_grab_verdict"] = "review" if manual_only else "blocked"
        candidate["review_reason"] = block_reasons[0]
        candidate["quality_status"] = "rejected"
    else:
        candidate["auto_grab_verdict"] = "auto_grab_safe"
        candidate["review_reason"] = ""
        candidate["quality_status"] = "accepted"
    return candidate


def _reader_page_pack_candidate_for_record(candidate, include_urls=False):
    out = dict(candidate or {})
    if not include_urls:
        out.pop("page_image_urls", None)
    raw = out.get("raw") if isinstance(out.get("raw"), dict) else {}
    result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    if include_urls:
        result["page_image_urls"] = list((candidate or {}).get("page_image_urls") or [])
    else:
        result.pop("page_image_urls", None)
    raw["result"] = result
    out["raw"] = raw
    return out


def reader_page_pack_task_seed(candidate, staging_root):
    candidate = candidate if isinstance(candidate, dict) else {}
    filename = safe_filename_part(candidate.get("title") or candidate.get("canonical_item_id")) + ".cbz"
    provider_id = inkdrop_sources.provider_key(candidate.get("provider_id"))
    root = PurePosixPath(str(staging_root or "/tmp/inkdrop-page-pack-staging").replace("\\", "/"))
    local_path = root / provider_id / filename
    task_candidate = _reader_page_pack_candidate_for_record(candidate, include_urls=True)
    return {
        "source": provider_id,
        "provider": provider_id,
        "provider_id": provider_id,
        "protocol": "http",
        "download_client": PAGE_PACK_DOWNLOAD_CLIENT,
        "external_id": candidate.get("candidate_identity") or _page_pack_identity(provider_id, candidate.get("source_url"), candidate.get("title"), candidate.get("page_image_url_hashes")),
        "candidate_identity": candidate.get("candidate_identity"),
        "title": candidate.get("title"),
        "status": "download_resolved",
        "state": "queued",
        "save_path": str(local_path.parent),
        "local_path": str(local_path),
        "partial_path": str(local_path) + ".part",
        "size_bytes": candidate.get("size_bytes"),
        "progress": 0,
        "download_url_hash": candidate.get("download_url_hash"),
        "raw_json": {
            "candidate": task_candidate,
            "download_guard": "reader_page_pack_verdict",
            "page_pack_task": True,
        },
    }


def reader_page_pack_attempt_seed(candidate, registry_row=None, staging_root=None, status=None, reason=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    provider_id = inkdrop_sources.provider_key(candidate.get("provider_id") or registry_row.get("provider_id"))
    artifact_safe = bool(candidate.get("artifact_safe"))
    block_reasons = list(candidate.get("block_reasons") or [])
    if status is None:
        status = "sent" if artifact_safe else ("review" if candidate.get("auto_grab_verdict") == "review" else "blocked")
    status = str(status or "").strip().lower()
    failure_reason = str(reason or candidate.get("review_reason") or (block_reasons[0] if block_reasons else "")).strip()
    attempt = {
        "source": provider_id,
        "provider": provider_id,
        "provider_id": provider_id,
        "source_type": registry_row.get("provider_type") or candidate.get("provider_type") or "direct_download",
        "provider_mode": registry_row.get("source_mode"),
        "registry_state": registry_row.get("registry_state"),
        "risk_class": registry_row.get("risk_class"),
        "status": status,
        "reason": failure_reason,
        "failure_reason": failure_reason,
        "retry_eligible": not artifact_safe,
        "title": candidate.get("title"),
        "query": candidate.get("series_title") or candidate.get("title"),
        "candidate_identity": candidate.get("candidate_identity"),
        "download_url_hash": candidate.get("download_url_hash"),
        "score": candidate.get("score"),
        "content_type": candidate.get("content_type"),
        "page_count": candidate.get("page_count"),
        "rights_status": candidate.get("rights_status"),
        "license_url": candidate.get("license_url"),
        "artifact_safe": artifact_safe,
        "auto_grab_verdict": candidate.get("auto_grab_verdict"),
        "block_reasons": block_reasons,
        "raw": {"candidate": _reader_page_pack_candidate_for_record(candidate, include_urls=False)},
    }
    if artifact_safe:
        task = reader_page_pack_task_seed(candidate, staging_root)
        attempt.update(
            {
                "protocol": task["protocol"],
                "download_client": task["download_client"],
                "external_id": task["external_id"],
                "save_path": task["save_path"],
                "local_path": task["local_path"],
                "download_path": task["local_path"],
                "partial_path": task["partial_path"],
                "category": "inkdrop-page-pack",
            }
        )
        attempt["raw"]["download_task_seed"] = task
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def json_direct_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    allowed_extensions = policy.get("allowed_extensions") or GENERIC_JSON_DIRECT_EXTENSIONS
    source_url = _payload_source_url(payload, first_text(registry_row.get("base_url")))
    rows = _json_direct_rows_from_payload(
        payload,
        source_url=source_url,
        source_site=first_text(policy.get("source_site_label"), registry_row.get("display_name"), registry_row.get("provider_id"), "JSON direct source"),
        allowed_extensions=allowed_extensions,
    )
    out = []
    provider_id = registry_row.get("provider_id") or "generic_json_direct_source"
    for row in rows:
        if not _query_matches_result(row, wanted_item):
            continue
        download_url = first_text(row.get("download_url"), row.get("url"))
        if not download_url or _url_has_secretish_query(download_url):
            continue
        ext = normalize_extension(first_value(row.get("extension"), row.get("file_name"), row.get("filename"), row.get("title"), download_url))
        if not ext:
            ext = extension_for_content_type(row.get("content_type"))
        if not ext:
            continue
        title = normalized_query(first_text(row.get("title"), _filename_title_from_url(download_url), wanted_item.get("title"), download_url))
        source_url = first_text(row.get("source_url"), row.get("page_url"), _payload_source_url(payload, ""))
        candidate = source_candidate(
            provider_id=provider_id,
            provider_type=registry_row.get("provider_type") or "direct_download",
            source_kind=registry_row.get("source_kind") or "json_direct_source",
            canonical_item_id=first_text(row.get("guid"), row.get("id"), download_url),
            canonical_work_id=first_text(row.get("series_id"), row.get("work_id")),
            title=title,
            series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), title),
            creator=first_text(row.get("creator"), row.get("author"), row.get("publisher")),
            language=first_text(row.get("language"), row.get("translated_language")).lower(),
            source_url=source_url,
            download_url=download_url,
            extension=ext,
            content_type=content_type_base(row.get("content_type")) or content_type_for_extension(ext),
            size_bytes=int_value(first_value(row.get("size_bytes"), row.get("size")), None),
            rights_status=first_text(row.get("rights_status"), policy.get("rights_status"), "provider_specific"),
            license_url=first_text(row.get("license_url"), policy.get("license_url")),
            wanted_item=wanted_item,
            raw={
                "result": {
                    "title": title,
                    "guid": row.get("guid"),
                    "summary": clipped_text(row.get("summary"), 1000),
                    "download_url_hash": url_hash(download_url),
                    "source_url_hash": url_hash(source_url),
                },
                "json_direct_source": True,
            },
        )
        candidate["source_site"] = first_text(row.get("source_site"), policy.get("source_site_label"), registry_row.get("display_name"))
        candidate["pack"] = looks_pack_like(title)
        if candidate["pack"] and not bool(policy.get("packs_allowed") or policy.get("allow_packs") or policy.get("pack_auto_allowed")):
            candidate["requires_manual_review"] = True
        candidate["language_status"] = _direct_language_status(candidate, policy)
        candidate["quality_profile"] = _direct_quality_profile(candidate)
        candidate["quality"] = candidate["quality_profile"]
        candidate["match_confidence"] = "title_match"
        out.append(candidate)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def opds_catalog_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    allowed = set(normalized_extensions(policy.get("allowed_extensions") or GENERIC_OPDS_EXTENSIONS))
    source_url = _payload_source_url(payload, first_text(registry_row.get("base_url")))
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = [row for row in payload.get("items") if isinstance(row, dict)]
    elif isinstance(payload, dict) and isinstance(payload.get("publications"), list):
        rows = _opds_rows_from_json(payload, source_url=source_url)
    else:
        rows = _opds_rows_from_xml(_payload_text(payload), source_url=source_url)
    out = []
    provider_id = registry_row.get("provider_id") or "generic_opds_catalog"
    for row in rows:
        if not _query_matches_result(row, wanted_item):
            continue
        download_url = first_text(row.get("download_url"), row.get("url"))
        if not download_url or _url_has_secretish_query(download_url):
            continue
        ext = normalize_extension(first_value(row.get("extension"), row.get("file_name"), row.get("filename"), download_url))
        if not ext:
            ext = extension_for_content_type(row.get("content_type"))
        if not ext or (allowed and ext not in allowed):
            continue
        title = normalized_query(first_text(row.get("title"), _filename_title_from_url(download_url), wanted_item.get("title"), download_url))
        row_source_url = first_text(row.get("source_url"), row.get("page_url"), source_url)
        candidate = source_candidate(
            provider_id=provider_id,
            provider_type=registry_row.get("provider_type") or "direct_download",
            source_kind=registry_row.get("source_kind") or "opds_acquisition_catalog",
            canonical_item_id=first_text(row.get("guid"), row.get("id"), download_url),
            canonical_work_id=first_text(row.get("series_id"), row.get("work_id")),
            title=title,
            series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), title),
            creator=first_text(row.get("creator"), row.get("author"), row.get("publisher")),
            language=first_text(row.get("language"), row.get("translated_language")).lower(),
            source_url=row_source_url,
            download_url=download_url,
            extension=ext,
            content_type=content_type_base(row.get("content_type")) or content_type_for_extension(ext),
            size_bytes=int_value(first_value(row.get("size_bytes"), row.get("size")), None),
            rights_status=first_text(row.get("rights_status"), policy.get("rights_status"), "provider_specific"),
            license_url=first_text(row.get("license_url"), policy.get("license_url")),
            wanted_item=wanted_item,
            raw={
                "result": {
                    "title": title,
                    "guid": row.get("guid"),
                    "summary": clipped_text(row.get("summary"), 1000),
                    "download_url_hash": url_hash(download_url),
                    "source_url_hash": url_hash(row_source_url),
                },
                "opds_catalog": True,
            },
        )
        candidate["source_site"] = first_text(row.get("source_site"), policy.get("source_site_label"), registry_row.get("display_name"))
        candidate["pack"] = looks_pack_like(title)
        if candidate["pack"] and not bool(policy.get("packs_allowed") or policy.get("allow_packs") or policy.get("pack_auto_allowed")):
            candidate["requires_manual_review"] = True
        candidate["language_status"] = _direct_language_status(candidate, policy)
        candidate["quality_profile"] = _direct_quality_profile(candidate)
        candidate["quality"] = candidate["quality_profile"]
        candidate["match_confidence"] = "title_match"
        out.append(candidate)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def comicscodes_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = [row for row in payload.get("items") if isinstance(row, dict)]
    else:
        text = _payload_text(payload)
        rows = _rss_rows_from_xml(text)
        if not rows:
            rows = _comicscodes_rows_from_html(text, _payload_source_url(payload))
    out = []
    for row in rows:
        if not _query_matches_result(row, wanted_item):
            continue
        result = dict(row)
        result["source_site"] = result.get("source_site") or "ComicsCodes"
        result["site"] = result.get("site") or "ComicsCodes"
        out.append(
            manual_source_card_from_result(
                result,
                registry_row,
                wanted_item,
                source_bucket=(registry_row or {}).get("provider_id") or "comicscodes",
            )
        )
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def html_search_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    policy = provider_policy(registry_row)
    source_site = first_text(policy.get("source_site_label"), registry_row.get("display_name"), registry_row.get("provider_id"), "HTML search")
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = [row for row in payload.get("items") if isinstance(row, dict)]
    else:
        rows = _html_search_rows_from_html(_payload_text(payload), _payload_source_url(payload, ""), source_site)
    out = []
    for row in rows:
        if not _query_matches_result(row, wanted_item):
            continue
        if not _search_result_allowed_by_policy(row, policy):
            continue
        result = dict(row)
        result["source_site"] = result.get("source_site") or source_site
        result["site"] = result.get("site") or source_site
        out.append(
            manual_source_card_from_result(
                result,
                registry_row,
                wanted_item,
                source_bucket=registry_row.get("provider_id") or "html_search_source",
            )
        )
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def _localized_text(value, preferred=("en",)):
    if isinstance(value, dict):
        for key in preferred:
            text = str(value.get(key) or "").strip()
            if text:
                return text
        for text in value.values():
            text = str(text or "").strip()
            if text:
                return text
    if isinstance(value, str):
        return value.strip()
    return ""


def _mangadex_manga_title(manga_row, wanted_item=None):
    manga_row = manga_row if isinstance(manga_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    attributes = manga_row.get("attributes") if isinstance(manga_row.get("attributes"), dict) else {}
    title = _localized_text(attributes.get("title")) or first_text(
        wanted_item.get("series_title"),
        wanted_item.get("series"),
        wanted_item.get("title"),
    )
    return normalized_query(title)


def mangadex_manga_title_values(manga_row):
    manga_row = manga_row if isinstance(manga_row, dict) else {}
    attributes = manga_row.get("attributes") if isinstance(manga_row.get("attributes"), dict) else {}
    values = []
    primary = attributes.get("title")
    if isinstance(primary, dict):
        values.extend(str(value or "").strip() for value in primary.values())
    else:
        values.append(str(primary or "").strip())
    alt_titles = attributes.get("altTitles") if isinstance(attributes.get("altTitles"), list) else []
    for row in alt_titles:
        if isinstance(row, dict):
            values.extend(str(value or "").strip() for value in row.values())
    out = []
    seen = set()
    for value in values:
        text = normalized_query(value)
        key = inkdrop_sources.normalize_title(text)
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def mangadex_relationship_creator_tokens(manga_row):
    """Normalized token sets for each real author/artist MangaDex attached to
    this work (requires includes[]=author,artist on the search request,
    which mangadex_search_request always sends). Native-script/parenthetical
    name variants (e.g. "Lee Eun-Jae (이은재)") collapse cleanly since
    normalize_title drops non-ASCII-alphanumeric runs as separators.
    """
    manga_row = manga_row if isinstance(manga_row, dict) else {}
    relationships = manga_row.get("relationships") if isinstance(manga_row.get("relationships"), list) else []
    out = []
    seen = set()
    for relationship in relationships:
        if not isinstance(relationship, dict) or relationship.get("type") not in ("author", "artist"):
            continue
        name = (relationship.get("attributes") or {}).get("name") if isinstance(relationship.get("attributes"), dict) else None
        tokens = frozenset(token for token in inkdrop_sources.normalize_title(name).split() if len(token) > 1)
        if tokens and tokens not in seen:
            seen.add(tokens)
            out.append(tokens)
    return out


def mangadex_manga_matches_wanted(manga_row, wanted_item=None, *, policy=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    query_values = [
        first_text(
            wanted_item.get("series_title"),
            wanted_item.get("series"),
            wanted_item.get("title"),
            wanted_item.get("query"),
        )
    ]
    query_values.extend(_series_query_aliases(wanted_item, policy=policy))
    query_values = [normalized_query(value) for value in query_values if normalized_query(value)]
    if not query_values:
        return True
    token_sets = [
        [token for token in inkdrop_sources.normalize_title(query).split() if len(token) > 1]
        for query in query_values
    ]
    token_sets = [tokens for tokens in token_sets if tokens]
    if not token_sets:
        return True
    title_matches = False
    for title in mangadex_manga_title_values(manga_row):
        haystack_tokens = set(inkdrop_sources.normalize_title(title).split())
        for query_tokens in token_sets:
            if all(token in haystack_tokens for token in query_tokens):
                title_matches = True
                break
        if title_matches:
            break
    if not title_matches:
        return False
    # A broad alias search (e.g. "Monster" for "Naoki Urasawa's Monster") can
    # title-match a same-titled but unrelated work. When the wanted item's
    # own canonical identity names a creator, require this candidate's real
    # MangaDex author/artist to agree before accepting -- but only when both
    # sides actually have creator evidence to compare. A candidate with no
    # author/artist relationship data (metadata gap, not a real signal) or a
    # wanted item with no extractable creator name is left to the existing
    # title-only check, unchanged.
    expected_creator_tokens = frozenset(
        token for token in inkdrop_sources.normalize_title(wanted_item.get("_mangadex_expected_creator")).split()
        if len(token) > 1
    )
    if not expected_creator_tokens:
        return True
    candidate_creator_token_sets = mangadex_relationship_creator_tokens(manga_row)
    if not candidate_creator_token_sets:
        return True
    return any(
        expected_creator_tokens & candidate_tokens
        for candidate_tokens in candidate_creator_token_sets
    )


def _mangadex_chapter_rows(payload):
    payload = payload if isinstance(payload, dict) else {}
    feed = payload.get("feed") if isinstance(payload.get("feed"), dict) else payload
    rows = feed.get("data") if isinstance(feed.get("data"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _mangadex_allowed_languages(registry_row=None, wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    language = first_value(wanted_item.get("language"), wanted_item.get("translated_language"))
    if isinstance(language, (list, tuple, set)):
        wanted = [str(item).strip().lower() for item in language if str(item or "").strip()]
    elif language:
        wanted = [str(language).strip().lower()]
    else:
        wanted = []
    policy = provider_policy(registry_row)
    allowed = policy.get("allowed_languages")
    if isinstance(allowed, str):
        policy_languages = [part.strip().lower() for part in allowed.split(",") if part.strip()]
    elif isinstance(allowed, (list, tuple, set)):
        policy_languages = [str(part).strip().lower() for part in allowed if str(part or "").strip()]
    else:
        policy_languages = []
    return wanted or policy_languages or ["en"]


def _wanted_chapter_number(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    explicit = first_text(
        wanted_item.get("chapter"),
        wanted_item.get("chapter_number"),
    )
    if explicit:
        return explicit
    unit_type = str(
        first_text(
            wanted_item.get("unitType"),
            wanted_item.get("unit_type"),
            wanted_item.get("unit"),
        )
    ).strip().lower()
    if unit_type in {"volume", "vol", "book_volume", "manga_volume"}:
        return ""
    return first_text(
        wanted_item.get("issue_number"),
        wanted_item.get("normalized_number"),
        wanted_item.get("number"),
    )


def _wanted_volume_number(wanted_item=None):
    return first_text(_wanted_unit_metadata(wanted_item, default_unit_type="").get("volume_number"))


def _number_text_matches(candidate_value, wanted_value):
    candidate = str(candidate_value or "").strip()
    wanted = str(wanted_value or "").strip()
    if not wanted:
        return True
    if not candidate:
        return False
    try:
        return float(candidate) == float(wanted)
    except Exception:
        return inkdrop_sources.normalize_title(candidate) == inkdrop_sources.normalize_title(wanted)


def _chapter_number_matches(candidate_chapter, wanted_item=None):
    wanted = _wanted_chapter_number(wanted_item)
    return _number_text_matches(candidate_chapter, wanted)


def _volume_number_matches(candidate_volume, wanted_item=None):
    wanted = _wanted_volume_number(wanted_item)
    return _number_text_matches(candidate_volume, wanted)


def _mangadex_search_query(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    return normalized_query(
        first_text(
            wanted_item.get("searchQuery"),
            wanted_item.get("search_query"),
            wanted_item.get("query"),
            wanted_item.get("series_title"),
            wanted_item.get("series"),
            wanted_item.get("manga_title"),
            wanted_item.get("title"),
        )
    )


def _mangadex_unit_type(wanted_item=None, attributes=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    attributes = attributes if isinstance(attributes, dict) else {}
    return first_text(
        wanted_item.get("unitType"),
        wanted_item.get("unit_type"),
        attributes.get("unitType"),
        "chapter",
    )


def _mangadex_chapter_metadata(attributes, wanted_item, manga_id, chapter_id, translated_language, source_url, *, page_count=None, page_quality=""):
    attributes = attributes if isinstance(attributes, dict) else {}
    pages = int_value(first_value(attributes.get("pages"), page_count), None)
    external_url = first_text(attributes.get("externalUrl"), attributes.get("external_url"))
    unit_type = _mangadex_unit_type(wanted_item, attributes)
    search_query = _mangadex_search_query(wanted_item)
    metadata = {
        "manga_id": manga_id,
        "mangadex_id": manga_id,
        "chapter_id": chapter_id,
        "mangadex_chapter_id": chapter_id,
        "chapter": str(attributes.get("chapter") or "").strip(),
        "volume": str(attributes.get("volume") or "").strip(),
        "translatedLanguage": translated_language,
        "translated_language": translated_language,
        "externalUrl": external_url,
        "external_url": external_url,
        "pages": pages,
        "unitType": unit_type,
        "unit_type": unit_type,
        "searchQuery": search_query,
        "search_query": search_query,
        "source_path": source_url,
    }
    if page_quality:
        metadata["page_quality"] = page_quality
    return {key: value for key, value in metadata.items() if value not in (None, "", [], {})}


def _mangadex_chapter_title(series_title, attributes):
    attributes = attributes if isinstance(attributes, dict) else {}
    chapter = str(attributes.get("chapter") or "").strip()
    title = normalized_query(attributes.get("title") or "")
    parts = [series_title]
    if chapter:
        parts.append(f"Chapter {chapter}")
    if title:
        parts.append(title)
    return normalized_query(" - ".join(part for part in parts if part))


def _mangadex_scanlation_group(row):
    relationships = row.get("relationships") if isinstance(row.get("relationships"), list) else []
    for relationship in relationships:
        if not isinstance(relationship, dict) or relationship.get("type") != "scanlation_group":
            continue
        attrs = relationship.get("attributes") if isinstance(relationship.get("attributes"), dict) else {}
        return first_text(attrs.get("name"), relationship.get("id"))
    return ""


def _mangadex_match_confidence(attributes, wanted_item=None, language_status="accepted"):
    wanted_chapter = _wanted_chapter_number(wanted_item)
    chapter_match = bool(wanted_chapter) and _chapter_number_matches((attributes or {}).get("chapter"), wanted_item)
    volume_match = _volume_number_matches((attributes or {}).get("volume"), wanted_item)
    unit_type = str(
        first_text(
            (wanted_item or {}).get("unitType") if isinstance(wanted_item, dict) else "",
            (wanted_item or {}).get("unit_type") if isinstance(wanted_item, dict) else "",
        )
    ).strip().lower()
    if chapter_match and language_status == "accepted":
        return "exact_chapter_language"
    if chapter_match:
        return "exact_chapter"
    volume_unit = unit_type in {"volume", "vol", "book_volume", "manga_volume"} or wanted_item_is_volume_unit(wanted_item)
    if volume_unit and volume_match and language_status == "accepted":
        return "exact_volume_language"
    if volume_unit and volume_match:
        return "exact_volume"
    if language_status == "accepted":
        return "language_title"
    return "candidate"


def _mangadex_candidate_key(provider_id, manga_id, chapter_id, translated_language="", quality=""):
    return inkdrop_sources.stable_id(
        "mangadex_chapter",
        provider_id,
        manga_id,
        chapter_id,
        translated_language,
        quality,
    )


def _mangadex_at_home_payload(payload, chapter_id):
    payload = payload if isinstance(payload, dict) else {}
    chapter_id = str(chapter_id or "").strip()
    at_home = payload.get("at_home")
    if isinstance(at_home, dict):
        value = at_home.get(chapter_id)
        if isinstance(value, dict):
            return value
    return {}


def _mangadex_page_filenames(at_home, policy):
    at_home = at_home if isinstance(at_home, dict) else {}
    chapter = at_home.get("chapter") if isinstance(at_home.get("chapter"), dict) else {}
    quality = str(
        first_text(
            policy.get("mangadex_page_quality"),
            policy.get("page_quality"),
            "data",
        )
    ).strip().lower()
    if quality in {"data_saver", "datasaver", "saver"}:
        filenames = chapter.get("dataSaver")
        path_segment = "data-saver"
    else:
        filenames = chapter.get("data")
        path_segment = "data"
    if not isinstance(filenames, list):
        filenames = []
    return path_segment, [str(item).strip() for item in filenames if str(item or "").strip()]


def _mangadex_page_urls(at_home, policy):
    at_home = at_home if isinstance(at_home, dict) else {}
    chapter = at_home.get("chapter") if isinstance(at_home.get("chapter"), dict) else {}
    base_url = str(at_home.get("baseUrl") or "").strip().rstrip("/")
    chapter_hash = str(chapter.get("hash") or "").strip()
    if not base_url or not chapter_hash:
        return []
    path_segment, filenames = _mangadex_page_filenames(at_home, policy)
    out = []
    for filename in filenames:
        ext = normalize_extension(filename)
        if not ext:
            continue
        out.append(f"{base_url}/{path_segment}/{quote(chapter_hash, safe='')}/{quote(filename, safe='')}")
    return out


def _mangadex_page_allowed_hosts(page_urls):
    hosts = []
    for url in page_urls or []:
        host = str(urlparse(str(url or "")).hostname or "").strip().lower().strip("[]")
        if not host or not (host == "uploads.mangadex.org" or host.endswith(".mangadex.network")):
            return []
        if host not in hosts:
            hosts.append(host)
    return hosts


def _chapter_sort_key(value):
    text = str(value or "").strip()
    try:
        return (0, float(text), text)
    except Exception:
        return (1, inkdrop_sources.normalize_title(text), text)


def _mangadex_volume_chapter_rows(payload, registry_row=None, wanted_item=None, *, limit=50):
    if not volume_page_pack_enabled(registry_row) or not wanted_item_is_volume_unit(wanted_item):
        return []
    wanted_volume = _wanted_volume_number(wanted_item)
    if not wanted_volume:
        return []
    allowed_languages = set(_mangadex_allowed_languages(registry_row, wanted_item))
    rows = []
    for row in _mangadex_chapter_rows(payload):
        attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        translated_language = str(attributes.get("translatedLanguage") or "").strip().lower()
        if allowed_languages and translated_language and translated_language not in allowed_languages:
            continue
        if not _volume_number_matches(attributes.get("volume"), wanted_item):
            continue
        rows.append(row)
    rows.sort(
        key=lambda row: _chapter_sort_key(
            ((row.get("attributes") if isinstance(row.get("attributes"), dict) else {}) or {}).get("chapter")
        )
    )
    max_rows = max(0, int(limit or 0))
    return rows[:max_rows] if max_rows else rows


def _mangadex_volume_candidate_key(provider_id, manga_id, volume, chapter_ids, translated_languages, quality):
    return inkdrop_sources.stable_id(
        "mangadex_volume_pack",
        provider_id,
        manga_id,
        volume,
        ",".join(chapter_ids or []),
        ",".join(translated_languages or []),
        quality,
    )


def mangadex_volume_page_pack_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    if not volume_page_pack_enabled(registry_row) or not wanted_item_is_volume_unit(wanted_item):
        return []
    wanted_volume = _wanted_volume_number(wanted_item)
    if not wanted_volume:
        return []
    max_chapters = volume_page_pack_max_chapters(registry_row)
    min_chapters = volume_page_pack_min_chapters(registry_row)
    matching_rows = _mangadex_volume_chapter_rows(
        payload,
        registry_row,
        wanted_item,
        limit=max_chapters + 1,
    )
    if len(matching_rows) > max_chapters or len(matching_rows) < min_chapters:
        return []
    policy = provider_policy(registry_row)
    manga_row = payload.get("manga") if isinstance(payload, dict) and isinstance(payload.get("manga"), dict) else {}
    series_title = _mangadex_manga_title(manga_row, wanted_item)
    provider_id = registry_row.get("provider_id") or "mangadex"
    manga_id = str(manga_row.get("id") or "").strip()
    chapter_ids = []
    translated_languages = []
    scanlation_groups = []
    page_urls = []
    page_hashes = []
    chapter_metadata = []
    quality = ""
    for row in matching_rows:
        attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        chapter_id = str(row.get("id") or "").strip()
        if not chapter_id:
            return []
        at_home = _mangadex_at_home_payload(payload, chapter_id)
        chapter_page_urls = _mangadex_page_urls(at_home, policy)
        if not chapter_page_urls:
            return []
        chapter_quality, _filenames = _mangadex_page_filenames(at_home, policy)
        quality = quality or chapter_quality
        translated_language = str(attributes.get("translatedLanguage") or "").strip().lower()
        group = _mangadex_scanlation_group(row)
        chapter_ids.append(chapter_id)
        if translated_language and translated_language not in translated_languages:
            translated_languages.append(translated_language)
        if group and group not in scanlation_groups:
            scanlation_groups.append(group)
        page_urls.extend(chapter_page_urls)
        page_hashes.extend(url_hash(url) for url in chapter_page_urls)
        chapter_metadata.append(
            {
                "chapter_id": chapter_id,
                "chapter": str(attributes.get("chapter") or "").strip(),
                "volume": str(attributes.get("volume") or "").strip(),
                "title": normalized_query(attributes.get("title") or ""),
                "page_count": len(chapter_page_urls),
                "scanlation_group": group,
            }
        )
    if len(chapter_ids) < min_chapters:
        return []
    language = translated_languages[0] if len(translated_languages) == 1 else ",".join(translated_languages)
    allowed_languages = set(_mangadex_allowed_languages(registry_row, wanted_item))
    language_status = "accepted" if not allowed_languages or all(lang in allowed_languages for lang in translated_languages) else "rejected"
    match_confidence = "exact_volume_language" if language_status == "accepted" else "exact_volume"
    title = normalized_query(f"{series_title} Volume {wanted_volume}")
    source_url = f"https://mangadex.org/title/{manga_id}" if manga_id else ""
    identity = _mangadex_volume_candidate_key(provider_id, manga_id, wanted_volume, chapter_ids, translated_languages, quality)
    metadata = {
        "manga_id": manga_id,
        "mangadex_id": manga_id,
        "volume": wanted_volume,
        "chapter_ids": chapter_ids,
        "mangadex_chapter_ids": chapter_ids,
        "chapter_count": len(chapter_ids),
        "volume_pack_chapter_count": len(chapter_ids),
        "volume_pack": True,
        "volume_page_pack": True,
        "language": language,
        "translated_language": language,
        "language_status": language_status,
        "match_confidence": match_confidence,
        "scanlation_groups": scanlation_groups,
        "page_quality": quality,
        "page_count": len(page_urls),
        "source_path": source_url,
    }
    candidate = source_candidate(
        provider_id=provider_id,
        provider_type=registry_row.get("provider_type") or "metadata_download_source",
        source_kind=registry_row.get("source_kind") or "manga_api_page_provider",
        canonical_item_id=identity,
        canonical_work_id=first_text(wanted_item.get("series_id"), wanted_item.get("series_title"), manga_id),
        title=title,
        series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), series_title, title),
        language=language,
        source_url=source_url,
        download_url="",
        extension=".cbz",
        content_type="application/vnd.comicbook+zip",
        rights_status=first_text(policy.get("rights_status"), "provider_specific"),
        license_url=first_text(policy.get("license_url")),
        wanted_item=wanted_item,
        raw={
            "result": {
                "title": title,
                "source_url_hash": url_hash(source_url),
                "page_image_url_hashes": page_hashes,
                "chapters": chapter_metadata,
                **metadata,
            },
            "mangadex_volume_page_pack": True,
            "mangadex_page_pack": True,
        },
    )
    candidate["candidate_identity"] = identity
    candidate["download_url_hash"] = url_hash("\n".join(page_urls))
    candidate["mangadex_candidate_key"] = identity
    candidate["mangadex_suppression_key"] = identity
    for key, value in metadata.items():
        candidate[key] = value
    candidate["page_image_urls"] = page_urls
    candidate["page_image_url_hashes"] = page_hashes
    candidate["page_image_allowed_hosts"] = _mangadex_page_allowed_hosts(page_urls)
    candidate["page_image_extensions"] = [normalize_extension(url) for url in page_urls]
    candidate["source_site"] = "MangaDex"
    candidate["source_path"] = source_url
    candidate["quality_status"] = "candidate"
    candidate["pack"] = True
    return [candidate][: max(0, int(limit or 0))]


def mangadex_page_pack_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    manga_row = payload.get("manga") if isinstance(payload, dict) and isinstance(payload.get("manga"), dict) else {}
    series_title = _mangadex_manga_title(manga_row, wanted_item)
    allowed_languages = set(_mangadex_allowed_languages(registry_row, wanted_item))
    provider_id = registry_row.get("provider_id") or "mangadex"
    out = []
    seen = set()
    for row in _mangadex_chapter_rows(payload):
        attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        translated_language = str(attributes.get("translatedLanguage") or "").strip().lower()
        if allowed_languages and translated_language and translated_language not in allowed_languages:
            continue
        if page_pack_chapter_blocks_volume_target(attributes.get("chapter"), wanted_item, registry_row):
            continue
        if not _chapter_number_matches(attributes.get("chapter"), wanted_item):
            continue
        if not _volume_number_matches(attributes.get("volume"), wanted_item):
            continue
        chapter_id = str(row.get("id") or "").strip()
        at_home = _mangadex_at_home_payload(payload, chapter_id)
        page_urls = _mangadex_page_urls(at_home, policy)
        if not page_urls:
            continue
        quality, _filenames = _mangadex_page_filenames(at_home, policy)
        title = _mangadex_chapter_title(series_title, attributes)
        source_url = f"https://mangadex.org/chapter/{chapter_id}" if chapter_id else ""
        page_hashes = [url_hash(url) for url in page_urls]
        manga_id = manga_row.get("id") or ""
        identity = _mangadex_candidate_key(provider_id, manga_id, chapter_id, translated_language, quality)
        if identity in seen:
            continue
        seen.add(identity)
        language_status = "accepted" if not allowed_languages or translated_language in allowed_languages else "rejected"
        match_confidence = _mangadex_match_confidence(attributes, wanted_item, language_status=language_status)
        scanlation_group = _mangadex_scanlation_group(row)
        readable_at = first_text(attributes.get("readableAt"), attributes.get("publishAt"), attributes.get("createdAt"))
        mangadex_metadata = _mangadex_chapter_metadata(
            attributes,
            wanted_item,
            manga_id,
            chapter_id,
            translated_language,
            source_url,
            page_count=len(page_urls),
            page_quality=quality,
        )
        mangadex_metadata.update(
            {
                "language_status": language_status,
                "match_confidence": match_confidence,
                "scanlation_group": scanlation_group,
                "readable_at": readable_at,
                "page_count": len(page_urls),
            }
        )
        candidate = source_candidate(
            provider_id=provider_id,
            provider_type=registry_row.get("provider_type") or "metadata_download_source",
            source_kind=registry_row.get("source_kind") or "manga_api_page_provider",
            canonical_item_id=identity,
            canonical_work_id=first_text(wanted_item.get("series_id"), wanted_item.get("series_title"), manga_id),
            title=title,
            series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), series_title, title),
            language=translated_language,
            source_url=source_url,
            download_url="",
            extension=".cbz",
            content_type="application/vnd.comicbook+zip",
            rights_status=first_text(policy.get("rights_status"), "provider_specific"),
            license_url=first_text(policy.get("license_url")),
            wanted_item=wanted_item,
            raw={
                "result": {
                    "title": title,
                    "source_url_hash": url_hash(source_url),
                    "page_image_url_hashes": page_hashes,
                    **mangadex_metadata,
                },
                "mangadex_page_pack": True,
            },
        )
        candidate["candidate_identity"] = identity
        candidate["download_url_hash"] = url_hash("\n".join(page_urls))
        for key, value in mangadex_metadata.items():
            candidate[key] = value
        candidate["mangadex_id"] = manga_id
        candidate["mangadex_chapter_id"] = chapter_id
        candidate["mangadex_candidate_key"] = identity
        candidate["mangadex_suppression_key"] = identity
        candidate["chapter"] = str(attributes.get("chapter") or "").strip()
        candidate["volume"] = str(attributes.get("volume") or "").strip()
        candidate["scanlation_group"] = scanlation_group
        candidate["readable_at"] = readable_at
        candidate["page_quality"] = quality
        candidate["page_count"] = len(page_urls)
        candidate["page_image_urls"] = page_urls
        candidate["page_image_url_hashes"] = page_hashes
        candidate["page_image_allowed_hosts"] = _mangadex_page_allowed_hosts(page_urls)
        candidate["page_image_extensions"] = [normalize_extension(url) for url in page_urls]
        candidate["source_site"] = "MangaDex"
        candidate["source_path"] = source_url
        candidate["match_confidence"] = match_confidence
        candidate["language_status"] = language_status
        candidate["quality_status"] = "candidate"
        candidate["pack"] = True
        out.append(candidate)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def mangadex_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    volume_pack_candidates = mangadex_volume_page_pack_candidates_from_payload(payload, registry_row, wanted_item, limit=limit)
    if volume_pack_candidates:
        return volume_pack_candidates
    page_pack_candidates = mangadex_page_pack_candidates_from_payload(payload, registry_row, wanted_item, limit=limit)
    if page_pack_candidates:
        return page_pack_candidates
    manga_row = payload.get("manga") if isinstance(payload, dict) and isinstance(payload.get("manga"), dict) else {}
    series_title = _mangadex_manga_title(manga_row, wanted_item)
    allowed_languages = set(_mangadex_allowed_languages(registry_row, wanted_item))
    provider_id = registry_row.get("provider_id") or "mangadex"
    out = []
    for row in _mangadex_chapter_rows(payload):
        attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        translated_language = str(attributes.get("translatedLanguage") or "").strip().lower()
        if allowed_languages and translated_language and translated_language not in allowed_languages:
            continue
        if page_pack_chapter_blocks_volume_target(attributes.get("chapter"), wanted_item, registry_row):
            continue
        if not _chapter_number_matches(attributes.get("chapter"), wanted_item):
            continue
        if not _volume_number_matches(attributes.get("volume"), wanted_item):
            continue
        title = _mangadex_chapter_title(series_title, attributes)
        manga_id = manga_row.get("id") or ""
        chapter_id = row.get("id") or ""
        source_url = f"https://mangadex.org/chapter/{chapter_id}" if chapter_id else ""
        language_status = "accepted" if not allowed_languages or translated_language in allowed_languages else "rejected"
        match_confidence = _mangadex_match_confidence(attributes, wanted_item, language_status=language_status)
        scanlation_group = _mangadex_scanlation_group(row)
        readable_at = first_text(attributes.get("readableAt"), attributes.get("publishAt"), attributes.get("createdAt"))
        candidate_key = _mangadex_candidate_key(provider_id, manga_id, chapter_id, translated_language)
        mangadex_metadata = _mangadex_chapter_metadata(
            attributes,
            wanted_item,
            manga_id,
            chapter_id,
            translated_language,
            source_url,
        )
        mangadex_metadata.update(
            {
                "language_status": language_status,
                "match_confidence": match_confidence,
                "scanlation_group": scanlation_group,
                "readable_at": readable_at,
            }
        )
        result = {
            "title": title,
            "series": series_title,
            "source_site": "MangaDex",
            "site": "MangaDex",
            "url": source_url,
            "id": candidate_key,
            "guid": f"mangadex:chapter:{chapter_id}" if chapter_id else "",
            "language": translated_language,
            "description": first_text(attributes.get("title"), f"MangaDex chapter {attributes.get('chapter') or ''}"),
            "score": 80 if translated_language in allowed_languages else 50,
            "download_url_hash": url_hash(candidate_key),
            "mangadex_candidate_key": candidate_key,
            "mangadex_suppression_key": candidate_key,
            **mangadex_metadata,
            "raw": {
                **mangadex_metadata,
            },
        }
        if not _query_matches_result(result, wanted_item):
            continue
        out.append(
            manual_source_card_from_result(
                result,
                registry_row,
                wanted_item,
                source_bucket=registry_row.get("provider_id") or "mangadex",
            )
        )
        out[-1]["mangadex_id"] = result["mangadex_id"]
        out[-1]["mangadex_chapter_id"] = result["mangadex_chapter_id"]
        out[-1]["mangadex_candidate_key"] = result["mangadex_candidate_key"]
        out[-1]["mangadex_suppression_key"] = result["mangadex_suppression_key"]
        out[-1]["candidate_identity"] = result["mangadex_candidate_key"]
        out[-1]["canonical_item_id"] = result["mangadex_candidate_key"]
        for key, value in mangadex_metadata.items():
            out[-1][key] = value
        out[-1]["chapter"] = result.get("chapter", "")
        out[-1]["volume"] = result.get("volume", "")
        out[-1]["scanlation_group"] = scanlation_group
        out[-1]["readable_at"] = readable_at
        out[-1]["download_url_hash"] = result["download_url_hash"]
        out[-1]["source_path"] = result["source_path"]
        out[-1]["language_status"] = language_status
        out[-1]["match_confidence"] = match_confidence
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def _is_mangadex_page_pack_candidate(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    return bool(raw.get("mangadex_page_pack") or candidate.get("page_image_urls"))


def mangadex_candidate_verdict(candidate, registry_row=None):
    if _is_mangadex_page_pack_candidate(candidate):
        return reader_page_pack_verdict(candidate, registry_row)
    return manual_source_card_verdict(candidate, registry_row)


def _mangadex_attempt_metadata(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    fields = (
        ("manga_id", "manga_id"),
        ("mangadex_id", "manga_id"),
        ("chapter_id", "chapter_id"),
        ("mangadex_chapter_id", "chapter_id"),
        ("chapter_ids", "chapter_ids"),
        ("mangadex_chapter_ids", "chapter_ids"),
        ("volume_pack", "volume_pack"),
        ("volume_page_pack", "volume_page_pack"),
        ("volume_pack_chapter_count", "volume_pack_chapter_count"),
        ("mangadex_candidate_key", "candidate_key"),
        ("mangadex_suppression_key", "suppression_key"),
        ("chapter", "chapter"),
        ("volume", "volume"),
        ("language", "language"),
        ("translatedLanguage", "translatedLanguage"),
        ("translated_language", "translated_language"),
        ("language_status", "language_status"),
        ("match_confidence", "match_confidence"),
        ("scanlation_group", "scanlation_group"),
        ("readable_at", "readable_at"),
        ("download_url_hash", "download_url_hash"),
        ("externalUrl", "externalUrl"),
        ("external_url", "external_url"),
        ("pages", "pages"),
        ("unitType", "unitType"),
        ("unit_type", "unit_type"),
        ("searchQuery", "searchQuery"),
        ("search_query", "search_query"),
        ("page_quality", "page_quality"),
        ("page_count", "page_count"),
        ("source_path", "source_path"),
    )
    metadata = {}
    for candidate_key, metadata_key in fields:
        value = candidate.get(candidate_key)
        if value in (None, "", [], {}):
            value = result.get(candidate_key)
        if value in (None, "", [], {}):
            value = result.get(metadata_key)
        if metadata_key in {"externalUrl", "external_url"} and value is False:
            value = ""
        if value not in (None, "", [], {}):
            metadata[metadata_key] = value
    return metadata


def _enrich_mangadex_attempt(attempt, candidate, *, page_pack=False):
    attempt = dict(attempt or {})
    metadata = _mangadex_attempt_metadata(candidate)
    for key, value in metadata.items():
        if key == "manga_id":
            attempt.setdefault("manga_id", value)
            attempt.setdefault("mangadex_id", value)
        elif key == "chapter_id":
            attempt.setdefault("chapter_id", value)
            attempt.setdefault("mangadex_chapter_id", value)
        elif key == "candidate_key":
            attempt.setdefault("mangadex_candidate_key", value)
        elif key == "suppression_key":
            attempt.setdefault("mangadex_suppression_key", value)
        else:
            attempt.setdefault(key, value)
    attempt.setdefault("retry_scope", "mangadex_at_home_page_pack" if page_pack else "mangadex_chapter_evidence")
    expectation = (
        "inkdrop_page_pack_cbz_then_kavita_import_verification"
        if page_pack
        else "mangadex_chapter_evidence_only_until_at_home_page_pack_enabled"
    )
    attempt.setdefault("import_handoff_expectation", expectation)
    raw = attempt.get("raw") if isinstance(attempt.get("raw"), dict) else {}
    if metadata:
        raw["mangadex"] = metadata
    raw["retry_scope"] = attempt["retry_scope"]
    raw["import_handoff_expectation"] = expectation
    task = raw.get("download_task_seed") if isinstance(raw.get("download_task_seed"), dict) else None
    if task is not None:
        task_raw = task.get("raw_json") if isinstance(task.get("raw_json"), dict) else {}
        if metadata:
            task_raw["mangadex"] = metadata
        task_raw["import_handoff_expectation"] = expectation
        task["raw_json"] = task_raw
        task.setdefault("source_path", metadata.get("source_path") or attempt.get("source_path"))
        task.setdefault("manga_id", metadata.get("manga_id"))
        task.setdefault("chapter_id", metadata.get("chapter_id"))
        task.setdefault("mangadex_id", metadata.get("manga_id"))
        task.setdefault("mangadex_chapter_id", metadata.get("chapter_id"))
        task.setdefault("mangadex_candidate_key", metadata.get("candidate_key"))
        raw["download_task_seed"] = task
    attempt["raw"] = raw
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def mangadex_candidate_attempt_seed(candidate, registry_row=None, staging_root=None, status=None, reason=None):
    if _is_mangadex_page_pack_candidate(candidate):
        attempt = reader_page_pack_attempt_seed(
            candidate,
            registry_row,
            staging_root=staging_root,
            status=status,
            reason=reason,
        )
        return _enrich_mangadex_attempt(attempt, candidate, page_pack=True)
    attempt = manual_source_card_attempt_seed(candidate, registry_row, status=status, reason=reason)
    return _enrich_mangadex_attempt(attempt, candidate, page_pack=False)


def suwayomi_manga_title_values(manga_row):
    manga_row = manga_row if isinstance(manga_row, dict) else {}
    values = [
        manga_row.get("title"),
        manga_row.get("name"),
        manga_row.get("mangaTitle"),
    ]
    source = manga_row.get("source") if isinstance(manga_row.get("source"), dict) else {}
    values.extend([source.get("title"), source.get("name")])
    out = []
    seen = set()
    for value in values:
        text = normalized_query(value)
        key = inkdrop_sources.normalize_title(text)
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _suwayomi_wanted_title(wanted_item=None):
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    return normalized_query(
        first_text(
            wanted_item.get("series_title"),
            wanted_item.get("series"),
            wanted_item.get("manga_title"),
            wanted_item.get("title"),
            wanted_item.get("query"),
        )
    )


def suwayomi_manga_matches_wanted(manga_row, wanted_item=None, *, policy=None):
    query = _suwayomi_wanted_title(wanted_item)
    query_keys = set(_wanted_series_title_keys(wanted_item))
    query_key = inkdrop_sources.normalize_title(query)
    if query_key:
        query_keys.add(query_key)
    for alias in _series_query_aliases(wanted_item, policy=policy):
        alias_key = inkdrop_sources.normalize_title(alias)
        if alias_key:
            query_keys.add(alias_key)
    query_keys = {key for key in query_keys if key}
    if not query_keys:
        return True
    return any(inkdrop_sources.normalize_title(title) in query_keys for title in suwayomi_manga_title_values(manga_row))


def _suwayomi_allowed_languages(registry_row=None, wanted_item=None):
    return _mangadex_allowed_languages(registry_row, wanted_item)


def _suwayomi_source_language(payload):
    payload = payload if isinstance(payload, dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    return str(source.get("lang") or "").strip().lower()


def _suwayomi_source_name(payload, registry_row=None):
    payload = payload if isinstance(payload, dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    return first_text(
        source.get("displayName"),
        source.get("name"),
        (registry_row or {}).get("display_name"),
        "Suwayomi",
    )


def _suwayomi_manga_title(manga_row, wanted_item=None):
    for title in suwayomi_manga_title_values(manga_row):
        return normalized_query(title)
    return _suwayomi_wanted_title(wanted_item)


def _suwayomi_chapter_rows(payload):
    payload = payload if isinstance(payload, dict) else {}
    rows = payload.get("chapters")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _suwayomi_number_text(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text)
    except Exception:
        return normalized_query(text)
    if number.is_integer():
        return str(int(number))
    return str(number).rstrip("0").rstrip(".")


def _suwayomi_chapter_number(chapter_row):
    chapter_row = chapter_row if isinstance(chapter_row, dict) else {}
    return _suwayomi_number_text(first_text(chapter_row.get("chapterNumber"), chapter_row.get("chapter"), chapter_row.get("number")))


def _suwayomi_meta_value(chapter_row, keys):
    chapter_row = chapter_row if isinstance(chapter_row, dict) else {}
    keys = {str(key or "").strip().lower() for key in keys or [] if str(key or "").strip()}
    meta = chapter_row.get("meta")
    if isinstance(meta, dict):
        for key in keys:
            value = meta.get(key)
            if value not in (None, "", [], {}):
                return value
        for key, value in meta.items():
            if str(key or "").strip().lower() in keys and value not in (None, "", [], {}):
                return value
        return ""
    for row in meta or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or row.get("name") or "").strip().lower()
        if key in keys and row.get("value") not in (None, "", [], {}):
            return row.get("value")
    return ""


SUWAYOMI_VOLUME_METADATA_KEYS = {"vol", "volume", "volumenumber"}


def _suwayomi_metadata_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _suwayomi_explicit_number(value):
    if isinstance(value, bool) or value in (None, "", [], {}):
        return ""
    if isinstance(value, dict):
        value = first_value(value.get("value"), value.get("number"))
    text = str(value or "").strip()
    match = re.fullmatch(r"(?i)(?:v|vol(?:ume)?\.?)?\s*0*(\d+(?:\.\d+)?)", text)
    if not match:
        return ""
    number = _suwayomi_number_text(match.group(1))
    try:
        return number if float(number) > 0 else ""
    except Exception:
        return ""


def _suwayomi_volume_values_from_container(value):
    values = []
    malformed = False
    if isinstance(value, dict):
        for key, item in value.items():
            if _suwayomi_metadata_key(key) in SUWAYOMI_VOLUME_METADATA_KEYS:
                number = _suwayomi_explicit_number(item)
                if number:
                    values.append(number)
                elif item not in (None, "", [], {}):
                    malformed = True
        return values, malformed
    if not isinstance(value, list):
        return values, malformed
    for row in value:
        if not isinstance(row, dict):
            continue
        key = first_text(row.get("key"), row.get("name"), row.get("field"))
        if _suwayomi_metadata_key(key) not in SUWAYOMI_VOLUME_METADATA_KEYS:
            continue
        raw_value = first_value(row.get("value"), row.get("number"))
        number = _suwayomi_explicit_number(raw_value)
        if number:
            values.append(number)
        elif raw_value not in (None, "", [], {}):
            malformed = True
    return values, malformed


def suwayomi_explicit_volume_evidence(chapter_row):
    """Return one conflict-free volume explicitly supplied by Suwayomi metadata."""
    chapter_row = chapter_row if isinstance(chapter_row, dict) else {}
    values, malformed = _suwayomi_volume_values_from_container(chapter_row)
    for key in ("attributes", "metadata", "meta", "_suwayomi_volume_evidence"):
        nested_values, nested_malformed = _suwayomi_volume_values_from_container(chapter_row.get(key))
        values.extend(nested_values)
        malformed = malformed or nested_malformed
    malformed = malformed or bool(chapter_row.get("_suwayomi_volume_evidence_malformed"))
    unique = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return {
        "volume_number": unique[0] if len(unique) == 1 and not malformed else "",
        "conflict": len(unique) > 1,
        "malformed": malformed,
        "values": unique,
    }


def _suwayomi_explicit_volume_number(chapter_row):
    return suwayomi_explicit_volume_evidence(chapter_row).get("volume_number") or ""


def suwayomi_chapter_membership(chapter_row, wanted_item=None, registry_row=None, *, volume_pack=False):
    """Return the authoritative unit-membership decision for one Suwayomi chapter."""
    chapter_row = chapter_row if isinstance(chapter_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    chapter_number = _suwayomi_chapter_number(chapter_row)
    if volume_pack:
        if not volume_page_pack_enabled(registry_row) or not wanted_item_is_volume_unit(wanted_item):
            return {"matches": False, "unit_type": "volume", "reason": "volume_pack_not_applicable"}
        wanted_volume = _wanted_volume_number(wanted_item)
        if not wanted_volume:
            return {"matches": False, "unit_type": "volume", "reason": "wanted_volume_missing"}
        evidence = suwayomi_explicit_volume_evidence(chapter_row)
        if evidence.get("conflict"):
            reason = "suwayomi_volume_metadata_conflict"
        elif evidence.get("malformed"):
            reason = "suwayomi_volume_metadata_invalid"
        elif not evidence.get("volume_number"):
            reason = "suwayomi_volume_metadata_missing"
        elif not _number_text_matches(evidence.get("volume_number"), wanted_volume):
            reason = "suwayomi_volume_metadata_wrong"
        else:
            return {
                "matches": True,
                "unit_type": "volume",
                "reason": "explicit_volume_membership",
                "chapter_number": chapter_number,
                "volume_number": evidence.get("volume_number"),
            }
        return {
            "matches": False,
            "unit_type": "volume",
            "reason": reason,
            "chapter_number": chapter_number,
        }

    if wanted_item_is_volume_unit(wanted_item):
        return {
            "matches": False,
            "unit_type": "chapter",
            "reason": "chapter_cannot_satisfy_volume",
            "chapter_number": chapter_number,
        }
    if not _chapter_number_matches(chapter_number, wanted_item):
        return {
            "matches": False,
            "unit_type": "chapter",
            "reason": "chapter_number_mismatch",
            "chapter_number": chapter_number,
        }
    wanted_volume = _wanted_volume_number(wanted_item)
    if wanted_volume:
        evidence = suwayomi_explicit_volume_evidence(chapter_row)
        if (
            evidence.get("conflict")
            or evidence.get("malformed")
            or (
                evidence.get("volume_number")
                and not _number_text_matches(evidence.get("volume_number"), wanted_volume)
            )
        ):
            return {
                "matches": False,
                "unit_type": "chapter",
                "reason": "chapter_volume_constraint_mismatch",
                "chapter_number": chapter_number,
            }
    return {
        "matches": True,
        "unit_type": "chapter",
        "reason": "exact_chapter_membership" if _wanted_chapter_number(wanted_item) else "chapter_membership",
        "chapter_number": chapter_number,
        "volume_number": suwayomi_explicit_volume_evidence(chapter_row).get("volume_number") or "",
    }


def _suwayomi_volume_number(chapter_row):
    chapter_row = chapter_row if isinstance(chapter_row, dict) else {}
    explicit = _suwayomi_explicit_volume_number(chapter_row)
    if explicit:
        return explicit
    match = re.search(r"(?i)\bvol(?:ume)?\.?\s*(\d+(?:\.\d+)?)\b", str(chapter_row.get("name") or ""))
    return _suwayomi_number_text(match.group(1)) if match else ""


def _suwayomi_chapter_row_with_page_metadata(payload, chapter_row):
    chapter_row = dict(chapter_row or {})
    chapter_id = str(chapter_row.get("id") or "").strip()
    page_payload = _suwayomi_page_payload(payload, chapter_id) if chapter_id else {}
    page_chapter = page_payload.get("chapter") if isinstance(page_payload.get("chapter"), dict) else {}
    if not page_chapter:
        return chapter_row
    merged = dict(chapter_row)
    chapter_evidence = suwayomi_explicit_volume_evidence(chapter_row)
    page_evidence = suwayomi_explicit_volume_evidence(page_chapter)
    combined_values = [
        *chapter_evidence.get("values", []),
        *page_evidence.get("values", []),
    ]
    merged["_suwayomi_volume_evidence_malformed"] = bool(
        chapter_evidence.get("malformed") or page_evidence.get("malformed")
    )
    merged["_suwayomi_volume_evidence"] = [{"key": "volume", "value": value} for value in combined_values]
    for key in ("volume", "volumeNumber", "volume_number", "attributes", "metadata", "meta"):
        if key in page_chapter and merged.get(key) in (None, "", [], {}):
            merged[key] = page_chapter.get(key)
    return merged


def source_title_is_single_volume_artifact(title, volume_number=""):
    text = normalized_query(title)
    if not text or SOURCE_CHAPTER_TITLE_RE.search(text):
        return False
    if not SOURCE_SINGLE_VOLUME_ARTIFACT_TITLE_RE.search(text):
        return False
    wanted = str(volume_number or "").strip()
    if not wanted:
        return True
    escaped = re.escape(wanted)
    return bool(
        re.search(rf"(?i)\b(?:volume|vol\.?|book)\s*{escaped}\b", text)
        or MANGA_SINGLE_VOLUME_ARTIFACT_ISSUE_TITLE_RE.search(text)
    )


def suwayomi_chapter_is_single_volume_artifact(chapter_row, wanted_item=None):
    if not wanted_item_is_single_volume_artifact_unit(wanted_item):
        return False
    wanted_volume = _wanted_volume_number(wanted_item)
    if not wanted_volume:
        return False
    explicit_volume = _suwayomi_explicit_volume_number(chapter_row)
    if not _number_text_matches(explicit_volume, wanted_volume):
        return False
    return source_title_is_single_volume_artifact((chapter_row or {}).get("name"), wanted_volume)


def _suwayomi_chapter_title(series_title, chapter_row):
    chapter = _suwayomi_chapter_number(chapter_row)
    name = normalized_query((chapter_row or {}).get("name") or "")
    parts = [series_title]
    if chapter:
        parts.append(f"Chapter {chapter}")
    if name and name.lower() not in {f"chapter {chapter}".lower(), f"ch {chapter}".lower()}:
        parts.append(name)
    return normalized_query(" - ".join(part for part in parts if part))


def _suwayomi_match_confidence(chapter_row, wanted_item=None, language_status="accepted"):
    wanted_chapter = _wanted_chapter_number(wanted_item)
    chapter_match = bool(wanted_chapter) and _chapter_number_matches(_suwayomi_chapter_number(chapter_row), wanted_item)
    volume_match = _volume_number_matches(_suwayomi_volume_number(chapter_row), wanted_item)
    unit_type = str(
        first_text(
            (wanted_item or {}).get("unitType") if isinstance(wanted_item, dict) else "",
            (wanted_item or {}).get("unit_type") if isinstance(wanted_item, dict) else "",
        )
    ).strip().lower()
    if chapter_match and language_status == "accepted":
        return "exact_chapter_language"
    if chapter_match:
        return "exact_chapter"
    volume_unit = unit_type in {"volume", "vol", "book_volume", "manga_volume"} or wanted_item_is_volume_unit(wanted_item)
    if volume_unit and volume_match and language_status == "accepted":
        return "exact_volume_language"
    if volume_unit and volume_match:
        return "exact_volume"
    if language_status == "accepted":
        return "language_title"
    return "candidate"


def _suwayomi_candidate_key(provider_id, source_id, manga_id, chapter_id, language="", page_extension=""):
    return inkdrop_sources.stable_id(
        "suwayomi_chapter",
        provider_id,
        source_id,
        manga_id,
        chapter_id,
        language,
        page_extension,
    )


def _suwayomi_page_payload(payload, chapter_id):
    payload = payload if isinstance(payload, dict) else {}
    pages_by_chapter = payload.get("pages_by_chapter") if isinstance(payload.get("pages_by_chapter"), dict) else {}
    value = pages_by_chapter.get(str(chapter_id))
    return value if isinstance(value, dict) else {}


def _suwayomi_page_extension(policy):
    ext = normalize_extension(first_text(policy.get("suwayomi_page_image_extension"), policy.get("page_image_extension"), ".webp"))
    return ext if ext in GENERIC_PAGE_IMAGE_EXTENSIONS else ".webp"


def _suwayomi_absolute_page_urls(payload, page_payload, policy):
    payload = payload if isinstance(payload, dict) else {}
    page_payload = page_payload if isinstance(page_payload, dict) else {}
    base_url = first_text(
        policy.get("suwayomi_page_base_url"),
        payload.get("base_url"),
        policy.get("base_url"),
        "http://127.0.0.1:4568",
    ).rstrip("/")
    pages = page_payload.get("pages") if isinstance(page_payload.get("pages"), list) else []
    out = []
    for page in pages:
        if not isinstance(page, str):
            continue
        text = page.strip()
        if not text:
            continue
        scheme = urlparse(text).scheme.lower()
        if scheme in {"http", "https"}:
            out.append(text)
        elif scheme:
            continue
        else:
            absolute = urljoin(f"{base_url}/", text.lstrip("/"))
            if urlparse(absolute).scheme.lower() in {"http", "https"}:
                out.append(absolute)
    return out


def _suwayomi_host_values(value):
    if value in (None, ""):
        return []
    values = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    out = []
    seen = set()
    for item in values:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if "://" in text:
            text = urlparse(text).hostname or ""
        text = text.strip("[]")
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _suwayomi_page_allowed_hosts(registry_row, payload, page_urls):
    policy = provider_policy(registry_row)
    hosts = []
    for key in ("suwayomi_allowed_hosts", "source_allowed_hosts"):
        hosts.extend(_suwayomi_host_values(policy.get(key) or (registry_row or {}).get(key)))
    for value in [payload.get("base_url") if isinstance(payload, dict) else "", *(page_urls or [])]:
        hosts.extend(_suwayomi_host_values(value))
    out = []
    seen = set()
    for host in hosts:
        if host and host not in seen:
            seen.add(host)
            out.append(host)
    return out


def _suwayomi_chapter_metadata(payload, manga_row, chapter_row, wanted_item, source_url, page_count=0, page_extension=".webp"):
    payload = payload if isinstance(payload, dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    source_id = str(source.get("id") or manga_row.get("sourceId") or "").strip()
    manga_id = str(manga_row.get("id") or chapter_row.get("mangaId") or "").strip()
    chapter_id = str(chapter_row.get("id") or "").strip()
    language = str(source.get("lang") or "").strip().lower()
    metadata = {
        "source_id": source_id,
        "suwayomi_source_id": source_id,
        "source_name": _suwayomi_source_name(payload),
        "manga_id": manga_id,
        "suwayomi_manga_id": manga_id,
        "chapter_id": chapter_id,
        "suwayomi_chapter_id": chapter_id,
        "chapter": _suwayomi_chapter_number(chapter_row),
        "volume": _suwayomi_volume_number(chapter_row),
        "source_order": chapter_row.get("sourceOrder"),
        "realUrl": source_url,
        "real_url": source_url,
        "scanlation_group": first_text(chapter_row.get("scanlator"), chapter_row.get("scanlationGroup")),
        "scanlator": first_text(chapter_row.get("scanlator"), chapter_row.get("scanlationGroup")),
        "translatedLanguage": language,
        "translated_language": language,
        "language": language,
        "pages": page_count,
        "page_count": page_count,
        "page_image_extension": page_extension,
        "unitType": first_text((wanted_item or {}).get("unitType"), (wanted_item or {}).get("unit_type"), "chapter"),
        "unit_type": first_text((wanted_item or {}).get("unit_type"), (wanted_item or {}).get("unitType"), "chapter"),
        "searchQuery": normalized_query(first_text((wanted_item or {}).get("query"), _suwayomi_wanted_title(wanted_item))),
        "search_query": normalized_query(first_text((wanted_item or {}).get("query"), _suwayomi_wanted_title(wanted_item))),
        "source_path": source_url,
    }
    return {key: value for key, value in metadata.items() if value not in (None, "", [], {})}


def _suwayomi_volume_chapter_rows(chapters, registry_row=None, wanted_item=None, *, limit=50):
    if not volume_page_pack_enabled(registry_row) or not wanted_item_is_volume_unit(wanted_item):
        return []
    wanted_volume = _wanted_volume_number(wanted_item)
    if not wanted_volume:
        return []
    rows = []
    for chapter_row in _suwayomi_chapter_rows({"chapters": chapters}):
        if not suwayomi_chapter_membership(
            chapter_row,
            wanted_item,
            registry_row,
            volume_pack=True,
        ).get("matches"):
            continue
        rows.append(chapter_row)
    rows.sort(key=lambda row: _chapter_sort_key(_suwayomi_chapter_number(row)))
    max_rows = max(0, int(limit or 0))
    return rows[:max_rows] if max_rows else rows


def _suwayomi_single_volume_artifact_rows_allowed(rows, wanted_item=None):
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    return bool(len(rows) == 1 and suwayomi_chapter_is_single_volume_artifact(rows[0], wanted_item))


def _suwayomi_volume_candidate_key(provider_id, source_id, manga_id, volume, chapter_ids, language="", page_extension=""):
    return inkdrop_sources.stable_id(
        "suwayomi_volume_pack",
        provider_id,
        source_id,
        manga_id,
        volume,
        ",".join(chapter_ids or []),
        language,
        page_extension,
    )


def suwayomi_volume_page_pack_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    if not volume_page_pack_enabled(registry_row) or not wanted_item_is_volume_unit(wanted_item):
        return []
    wanted_volume = _wanted_volume_number(wanted_item)
    if not wanted_volume:
        return []
    policy = provider_policy(registry_row)
    manga_row = payload.get("manga") if isinstance(payload, dict) and isinstance(payload.get("manga"), dict) else {}
    if not suwayomi_manga_matches_wanted(manga_row, wanted_item, policy=policy):
        return []
    allowed_languages = set(_suwayomi_allowed_languages(registry_row, wanted_item))
    source_language = _suwayomi_source_language(payload)
    if allowed_languages and source_language and source_language not in allowed_languages:
        return []
    max_chapters = volume_page_pack_max_chapters(registry_row)
    min_chapters = volume_page_pack_min_chapters(registry_row)
    matching_rows = _suwayomi_volume_chapter_rows(
        payload.get("chapters"),
        registry_row,
        wanted_item,
        limit=max_chapters + 1,
    )
    if len(matching_rows) > max_chapters:
        return []
    single_volume_artifact = _suwayomi_single_volume_artifact_rows_allowed(matching_rows, wanted_item)
    if len(matching_rows) < min_chapters and not (
        single_chapter_volume_page_pack_allowed(registry_row) or single_volume_artifact
    ):
        return []
    provider_id = registry_row.get("provider_id") or "suwayomi"
    source = payload.get("source") if isinstance(payload, dict) and isinstance(payload.get("source"), dict) else {}
    source_id = str(source.get("id") or manga_row.get("sourceId") or "").strip()
    manga_id = str(manga_row.get("id") or "").strip()
    series_title = _suwayomi_manga_title(manga_row, wanted_item)
    page_extension = _suwayomi_page_extension(policy)
    chapter_ids = []
    scanlators = []
    page_urls = []
    page_hashes = []
    seen_page_urls = set()
    chapter_metadata = []
    chapter_numbers = set()
    for chapter_row in matching_rows:
        chapter_row = _suwayomi_chapter_row_with_page_metadata(payload, chapter_row)
        evidence = suwayomi_explicit_volume_evidence(chapter_row)
        if (
            evidence.get("conflict")
            or evidence.get("malformed")
            or not _volume_number_matches(evidence.get("volume_number"), wanted_item)
        ):
            return []
        chapter_id = str(chapter_row.get("id") or "").strip()
        chapter_number = _suwayomi_chapter_number(chapter_row)
        if not chapter_id or chapter_id in chapter_ids or (chapter_number and chapter_number in chapter_numbers):
            return []
        page_payload = _suwayomi_page_payload(payload, chapter_id)
        chapter_page_urls = _suwayomi_absolute_page_urls(payload, page_payload, policy)
        if not chapter_page_urls:
            return []
        if len(set(chapter_page_urls)) != len(chapter_page_urls) or any(
            url in seen_page_urls for url in chapter_page_urls
        ):
            return []
        scanlator = first_text(chapter_row.get("scanlator"), chapter_row.get("scanlationGroup"))
        chapter_ids.append(chapter_id)
        if chapter_number:
            chapter_numbers.add(chapter_number)
        if scanlator and scanlator not in scanlators:
            scanlators.append(scanlator)
        page_urls.extend(chapter_page_urls)
        seen_page_urls.update(chapter_page_urls)
        page_hashes.extend(url_hash(url) for url in chapter_page_urls)
        chapter_metadata.append(
            {
                "chapter_id": chapter_id,
                "chapter": _suwayomi_chapter_number(chapter_row),
                "volume": _suwayomi_volume_number(chapter_row),
                "name": normalized_query(chapter_row.get("name") or ""),
                "page_count": len(chapter_page_urls),
                "scanlator": scanlator,
            }
        )
    if len(chapter_ids) < min_chapters and not (
        single_chapter_volume_page_pack_allowed(registry_row) or single_volume_artifact
    ):
        return []
    language_status = "accepted" if not allowed_languages or not source_language or source_language in allowed_languages else "rejected"
    match_confidence = "exact_volume_language" if language_status == "accepted" else "exact_volume"
    title = normalized_query(f"{series_title} Volume {wanted_volume}")
    source_url = first_text(manga_row.get("realUrl"), manga_row.get("url"), matching_rows[0].get("realUrl") if matching_rows else "")
    identity = _suwayomi_volume_candidate_key(provider_id, source_id, manga_id, wanted_volume, chapter_ids, source_language, page_extension)
    metadata = {
        "source_id": source_id,
        "suwayomi_source_id": source_id,
        "source_name": _suwayomi_source_name(payload, registry_row),
        "manga_id": manga_id,
        "suwayomi_manga_id": manga_id,
        "volume": wanted_volume,
        "chapter_ids": chapter_ids,
        "suwayomi_chapter_ids": chapter_ids,
        "chapter_count": len(chapter_ids),
        "volume_pack_chapter_count": len(chapter_ids),
        "volume_pack": True,
        "volume_page_pack": True,
        "single_volume_artifact": single_volume_artifact,
        "scanlators": scanlators,
        "translatedLanguage": source_language,
        "translated_language": source_language,
        "language": source_language,
        "language_status": language_status,
        "match_confidence": match_confidence,
        "page_count": len(page_urls),
        "page_image_extension": page_extension,
        "realUrl": source_url,
        "real_url": source_url,
        "source_path": source_url,
    }
    candidate = source_candidate(
        provider_id=provider_id,
        provider_type=registry_row.get("provider_type") or "metadata_download_source",
        source_kind=registry_row.get("source_kind") or "suwayomi_api_page_provider",
        canonical_item_id=identity,
        canonical_work_id=first_text(wanted_item.get("series_id"), wanted_item.get("series_title"), manga_id),
        title=title,
        series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), series_title, title),
        language=source_language,
        source_url=source_url,
        download_url="",
        extension=".cbz",
        content_type="application/vnd.comicbook+zip",
        rights_status=first_text(policy.get("rights_status"), "provider_specific"),
        license_url=first_text(policy.get("license_url")),
        wanted_item=wanted_item,
        raw={
            "result": {
                "title": title,
                "source_url_hash": url_hash(source_url),
                "page_image_url_hashes": page_hashes,
                "chapters": chapter_metadata,
                **metadata,
            },
            "suwayomi_volume_page_pack": True,
            "suwayomi_page_pack": True,
        },
    )
    candidate["candidate_identity"] = identity
    candidate["download_url_hash"] = url_hash("\n".join([*page_urls, page_extension]))
    candidate["suwayomi_candidate_key"] = identity
    candidate["suwayomi_suppression_key"] = identity
    for key, value in metadata.items():
        candidate[key] = value
    candidate["page_image_urls"] = page_urls
    candidate["page_image_url_hashes"] = page_hashes
    candidate["page_image_extension"] = page_extension
    candidate["page_image_extensions"] = [normalize_extension(url) or page_extension for url in page_urls]
    candidate["page_image_allowed_hosts"] = _suwayomi_page_allowed_hosts(registry_row, payload, page_urls)
    candidate["source_site"] = f"Suwayomi: {_suwayomi_source_name(payload, registry_row)}"
    candidate["source_path"] = source_url
    candidate["quality_status"] = "candidate"
    candidate["pack"] = True
    return [candidate][: max(0, int(limit or 0))]


def suwayomi_page_pack_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    manga_row = payload.get("manga") if isinstance(payload, dict) and isinstance(payload.get("manga"), dict) else {}
    if not suwayomi_manga_matches_wanted(manga_row, wanted_item, policy=policy):
        return []
    allowed_languages = set(_suwayomi_allowed_languages(registry_row, wanted_item))
    source_language = _suwayomi_source_language(payload)
    provider_id = registry_row.get("provider_id") or "suwayomi"
    source = payload.get("source") if isinstance(payload, dict) and isinstance(payload.get("source"), dict) else {}
    source_id = str(source.get("id") or manga_row.get("sourceId") or "").strip()
    series_title = _suwayomi_manga_title(manga_row, wanted_item)
    page_extension = _suwayomi_page_extension(policy)
    out = []
    seen = set()
    for chapter_row in _suwayomi_chapter_rows(payload):
        if allowed_languages and source_language and source_language not in allowed_languages:
            continue
        chapter_row = _suwayomi_chapter_row_with_page_metadata(payload, chapter_row)
        if not suwayomi_chapter_membership(chapter_row, wanted_item, registry_row).get("matches"):
            continue
        chapter_id = str(chapter_row.get("id") or "").strip()
        page_payload = _suwayomi_page_payload(payload, chapter_id)
        page_urls = _suwayomi_absolute_page_urls(payload, page_payload, policy)
        if not page_urls:
            continue
        manga_id = str(manga_row.get("id") or chapter_row.get("mangaId") or "").strip()
        identity = _suwayomi_candidate_key(provider_id, source_id, manga_id, chapter_id, source_language, page_extension)
        if identity in seen:
            continue
        seen.add(identity)
        title = _suwayomi_chapter_title(series_title, chapter_row)
        source_url = first_text(chapter_row.get("realUrl"), manga_row.get("realUrl"), manga_row.get("url"))
        language_status = "accepted" if not allowed_languages or not source_language or source_language in allowed_languages else "rejected"
        match_confidence = _suwayomi_match_confidence(chapter_row, wanted_item, language_status=language_status)
        page_hashes = [url_hash(url) for url in page_urls]
        metadata = _suwayomi_chapter_metadata(
            payload,
            manga_row,
            chapter_row,
            wanted_item,
            source_url,
            page_count=len(page_urls),
            page_extension=page_extension,
        )
        metadata.update(
            {
                "language_status": language_status,
                "match_confidence": match_confidence,
                "page_count": len(page_urls),
            }
        )
        candidate = source_candidate(
            provider_id=provider_id,
            provider_type=registry_row.get("provider_type") or "metadata_download_source",
            source_kind=registry_row.get("source_kind") or "suwayomi_api_page_provider",
            canonical_item_id=identity,
            canonical_work_id=first_text(wanted_item.get("series_id"), wanted_item.get("series_title"), manga_id),
            title=title,
            series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), series_title, title),
            language=source_language,
            source_url=source_url,
            download_url="",
            extension=".cbz",
            content_type="application/vnd.comicbook+zip",
            rights_status=first_text(policy.get("rights_status"), "provider_specific"),
            license_url=first_text(policy.get("license_url")),
            wanted_item=wanted_item,
            raw={
                "result": {
                    "title": title,
                    "source_url_hash": url_hash(source_url),
                    "page_image_url_hashes": page_hashes,
                    **metadata,
                },
                "suwayomi_page_pack": True,
            },
        )
        candidate["candidate_identity"] = identity
        candidate["download_url_hash"] = url_hash("\n".join([*page_urls, page_extension]))
        candidate["suwayomi_candidate_key"] = identity
        candidate["suwayomi_suppression_key"] = identity
        candidate["suwayomi_source_id"] = source_id
        candidate["suwayomi_manga_id"] = manga_id
        candidate["suwayomi_chapter_id"] = chapter_id
        for key, value in metadata.items():
            candidate[key] = value
        candidate["chapter"] = metadata.get("chapter", "")
        candidate["volume"] = metadata.get("volume", "")
        candidate["page_count"] = len(page_urls)
        candidate["page_image_urls"] = page_urls
        candidate["page_image_url_hashes"] = page_hashes
        candidate["page_image_extension"] = page_extension
        candidate["page_image_extensions"] = [normalize_extension(url) or page_extension for url in page_urls]
        candidate["page_image_allowed_hosts"] = _suwayomi_page_allowed_hosts(registry_row, payload, page_urls)
        candidate["source_site"] = f"Suwayomi: {_suwayomi_source_name(payload, registry_row)}"
        candidate["source_path"] = source_url
        candidate["match_confidence"] = match_confidence
        candidate["language_status"] = language_status
        candidate["quality_status"] = "candidate"
        candidate["pack"] = True
        out.append(candidate)
        if len(out) >= max(0, int(limit or 0)):
            break
    return out


def suwayomi_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=20):
    volume_pack_candidates = suwayomi_volume_page_pack_candidates_from_payload(payload, registry_row, wanted_item, limit=limit)
    if volume_pack_candidates:
        return volume_pack_candidates
    return suwayomi_page_pack_candidates_from_payload(payload, registry_row, wanted_item, limit=limit)


def _is_suwayomi_page_pack_candidate(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    return bool(raw.get("suwayomi_page_pack") or candidate.get("suwayomi_page_pack") or candidate.get("page_image_urls"))


def suwayomi_candidate_verdict(candidate, registry_row=None):
    return reader_page_pack_verdict(candidate, registry_row)


def _suwayomi_attempt_metadata(candidate):
    candidate = candidate if isinstance(candidate, dict) else {}
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    fields = (
        ("source_id", "source_id"),
        ("suwayomi_source_id", "source_id"),
        ("source_name", "source_name"),
        ("manga_id", "manga_id"),
        ("suwayomi_manga_id", "manga_id"),
        ("chapter_id", "chapter_id"),
        ("suwayomi_chapter_id", "chapter_id"),
        ("chapter_ids", "chapter_ids"),
        ("suwayomi_chapter_ids", "chapter_ids"),
        ("volume_pack", "volume_pack"),
        ("volume_page_pack", "volume_page_pack"),
        ("volume_pack_chapter_count", "volume_pack_chapter_count"),
        ("single_volume_artifact", "single_volume_artifact"),
        ("suwayomi_candidate_key", "candidate_key"),
        ("suwayomi_suppression_key", "suppression_key"),
        ("chapter", "chapter"),
        ("volume", "volume"),
        ("source_order", "source_order"),
        ("language", "language"),
        ("translatedLanguage", "translatedLanguage"),
        ("translated_language", "translated_language"),
        ("language_status", "language_status"),
        ("match_confidence", "match_confidence"),
        ("scanlation_group", "scanlation_group"),
        ("scanlator", "scanlator"),
        ("download_url_hash", "download_url_hash"),
        ("realUrl", "realUrl"),
        ("real_url", "real_url"),
        ("pages", "pages"),
        ("unitType", "unitType"),
        ("unit_type", "unit_type"),
        ("searchQuery", "searchQuery"),
        ("search_query", "search_query"),
        ("page_image_extension", "page_image_extension"),
        ("page_count", "page_count"),
        ("source_path", "source_path"),
    )
    metadata = {}
    for candidate_key, metadata_key in fields:
        value = candidate.get(candidate_key)
        if value in (None, "", [], {}):
            value = result.get(candidate_key)
        if value in (None, "", [], {}):
            value = result.get(metadata_key)
        if value not in (None, "", [], {}):
            metadata[metadata_key] = value
    return metadata


def _enrich_suwayomi_attempt(attempt, candidate):
    attempt = dict(attempt or {})
    metadata = _suwayomi_attempt_metadata(candidate)
    for key, value in metadata.items():
        if key == "source_id":
            attempt.setdefault("source_id", value)
            attempt.setdefault("suwayomi_source_id", value)
        elif key == "manga_id":
            attempt.setdefault("manga_id", value)
            attempt.setdefault("suwayomi_manga_id", value)
        elif key == "chapter_id":
            attempt.setdefault("chapter_id", value)
            attempt.setdefault("suwayomi_chapter_id", value)
        elif key == "candidate_key":
            attempt.setdefault("suwayomi_candidate_key", value)
        elif key == "suppression_key":
            attempt.setdefault("suwayomi_suppression_key", value)
        else:
            attempt.setdefault(key, value)
    attempt.setdefault("retry_scope", "suwayomi_page_pack")
    expectation = "inkdrop_page_pack_cbz_then_kavita_import_verification"
    attempt.setdefault("import_handoff_expectation", expectation)
    raw = attempt.get("raw") if isinstance(attempt.get("raw"), dict) else {}
    if metadata:
        raw["suwayomi"] = metadata
    raw["retry_scope"] = attempt["retry_scope"]
    raw["import_handoff_expectation"] = expectation
    task = raw.get("download_task_seed") if isinstance(raw.get("download_task_seed"), dict) else None
    if task is not None:
        task_raw = task.get("raw_json") if isinstance(task.get("raw_json"), dict) else {}
        if metadata:
            task_raw["suwayomi"] = metadata
        task_raw["import_handoff_expectation"] = expectation
        task["raw_json"] = task_raw
        task.setdefault("source_path", metadata.get("source_path") or attempt.get("source_path"))
        task.setdefault("source_id", metadata.get("source_id"))
        task.setdefault("manga_id", metadata.get("manga_id"))
        task.setdefault("chapter_id", metadata.get("chapter_id"))
        task.setdefault("suwayomi_source_id", metadata.get("source_id"))
        task.setdefault("suwayomi_manga_id", metadata.get("manga_id"))
        task.setdefault("suwayomi_chapter_id", metadata.get("chapter_id"))
        task.setdefault("suwayomi_candidate_key", metadata.get("candidate_key"))
        raw["download_task_seed"] = task
    attempt["raw"] = raw
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def suwayomi_candidate_attempt_seed(candidate, registry_row=None, staging_root=None, status=None, reason=None):
    attempt = reader_page_pack_attempt_seed(
        candidate,
        registry_row,
        staging_root=staging_root,
        status=status,
        reason=reason,
    )
    return _enrich_suwayomi_attempt(attempt, candidate)


def manual_source_card_verdict(candidate, registry_row=None):
    candidate = dict(candidate or {})
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    policy = provider_policy(registry_row, candidate)
    registry_state = str(registry_row.get("registry_state") or "").strip().lower()
    source_mode = str(registry_row.get("source_mode") or "").strip().lower()
    block_reasons = []
    review_reasons = []

    if registry_row and registry_state not in {"ready", "assist", "manual_review"}:
        block_reasons.append(f"registry_{registry_state or 'unavailable'}")
    if registry_row and not registry_row.get("auto_search_allowed") and registry_state != "manual_review":
        block_reasons.append("registry_search_not_allowed")
    if not candidate.get("title"):
        block_reasons.append("missing_title")
    if not (candidate.get("source_url_hash") or candidate.get("canonical_item_id")):
        block_reasons.append("no_source_locator")

    review_reasons.append("manual_source_requires_operator")
    requires_manual = bool(
        registry_row.get("requires_manual_review")
        or policy.get("requires_manual_confirm")
        or source_mode == "manual_review"
        or registry_state == "manual_review"
        or candidate.get("requires_manual_review")
    )
    if requires_manual:
        review_reasons.append("manual_review_required")
    if candidate.get("download_url_present"):
        review_reasons.append("direct_download_url_not_stored")
    if candidate.get("pack"):
        review_reasons.append("pack_requires_review")
    if registry_row and not registry_row.get("auto_download_allowed"):
        review_reasons.append("auto_download_not_allowed")

    candidate["block_reasons"] = block_reasons
    candidate["review_reasons"] = list(dict.fromkeys(review_reasons))
    candidate["candidate_safe"] = False
    candidate["artifact_safe"] = False
    if block_reasons:
        candidate["auto_grab_verdict"] = "blocked"
        candidate["review_reason"] = block_reasons[0]
        candidate["quality_status"] = "rejected"
    else:
        candidate["auto_grab_verdict"] = "review"
        candidate["review_reason"] = candidate["review_reasons"][0] if candidate["review_reasons"] else "manual_review_required"
        candidate["quality_status"] = "review"
    return candidate


def manual_source_card_attempt_seed(candidate, registry_row=None, status=None, reason=None):
    candidate = candidate if isinstance(candidate, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    provider_id = inkdrop_sources.provider_key(candidate.get("provider_id") or registry_row.get("provider_id") or "manual_source")
    if status is None:
        status = "review" if candidate.get("auto_grab_verdict") == "review" else "blocked"
    status = str(status or "").strip().lower()
    failure_reason = str(
        reason
        or candidate.get("review_reason")
        or (candidate.get("block_reasons") or candidate.get("review_reasons") or [""])[0]
        or ""
    ).strip()
    attempt = {
        "source": provider_id,
        "provider_id": provider_id,
        "source_type": registry_row.get("provider_type") or candidate.get("provider_type") or "source",
        "provider_mode": registry_row.get("source_mode"),
        "registry_state": registry_row.get("registry_state"),
        "risk_class": registry_row.get("risk_class"),
        "provider": candidate.get("source_site") or provider_id,
        "status": status,
        "reason": failure_reason,
        "failure_reason": failure_reason,
        "retry_eligible": False,
        "title": candidate.get("title"),
        "query": candidate.get("series_title") or candidate.get("title"),
        "candidate_identity": candidate.get("candidate_identity") or manual_source_card_identity(candidate),
        "score": candidate.get("score"),
        "candidate_safe": False,
        "artifact_safe": False,
        "auto_grab_verdict": candidate.get("auto_grab_verdict"),
        "block_reasons": list(candidate.get("block_reasons") or []),
        "review_reasons": list(candidate.get("review_reasons") or []),
        "raw": {
            "candidate": candidate,
            "manual_review_only": True,
            "source_url": candidate.get("source_url"),
            "source_url_hash": candidate.get("source_url_hash"),
            "manual_source_guard": "manual_source_card_verdict",
        },
    }
    return {key: value for key, value in attempt.items() if value not in (None, "", [], {})}


def _ia_metadata(payload):
    payload = payload if isinstance(payload, dict) else {}
    metadata = payload.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _ia_identifier(payload):
    payload = payload if isinstance(payload, dict) else {}
    metadata = _ia_metadata(payload)
    identifier = first_text(
        metadata.get("identifier"),
        payload.get("identifier"),
        payload.get("item_identifier"),
        payload.get("id"),
    )
    if identifier:
        return identifier
    directory = str(payload.get("dir") or "").strip("/")
    if directory:
        return directory.rsplit("/", 1)[-1]
    return ""


def _ia_boolish(value):
    text = first_text(value).strip().lower()
    if not text:
        return False
    return text in {"1", "true", "yes", "y", "on"}


def _ia_license_url(metadata):
    metadata = metadata if isinstance(metadata, dict) else {}
    return first_text(
        metadata.get("licenseurl"),
        metadata.get("license_url"),
        metadata.get("license"),
        metadata.get("creative_commons_license"),
    )


def _ia_rights_status(metadata):
    metadata = metadata if isinstance(metadata, dict) else {}
    if _ia_boolish(metadata.get("access-restricted-item")) or _ia_boolish(metadata.get("is_dark")):
        return "copyright_restricted"
    fields = []
    for key in (
        "licenseurl",
        "license_url",
        "license",
        "rights",
        "usage",
        "copyright",
        "possible-copyright-status",
    ):
        fields.extend(text_values(metadata.get(key)))
    haystack = " ".join(fields).strip().lower()
    if not haystack:
        return "unknown"
    public_domain_tokens = (
        "public domain",
        "publicdomain",
        "public-domain",
        "creativecommons.org/publicdomain",
        "cc0",
        "no known copyright",
    )
    if any(token in haystack for token in public_domain_tokens):
        return "public_domain"
    creative_commons_tokens = (
        "creativecommons.org/licenses",
        "creative commons",
        "cc-by",
        "cc by",
    )
    if any(token in haystack for token in creative_commons_tokens):
        return "creative_commons"
    restricted_tokens = (
        "in copyright",
        "copyrighted",
        "all rights reserved",
        "not in public domain",
        "borrow",
        "controlled digital lending",
    )
    if any(token in haystack for token in restricted_tokens):
        return "copyright_restricted"
    return "unknown"


def _ia_file_name(file_row):
    file_row = file_row if isinstance(file_row, dict) else {}
    return str(file_row.get("name") or "").strip()


def _ia_download_url(identifier, file_name):
    identifier = str(identifier or "").strip()
    file_name = str(file_name or "").strip()
    if not identifier or not file_name:
        return ""
    return f"{INTERNET_ARCHIVE_DOWNLOAD_BASE}/{quote(identifier, safe='')}/{quote(file_name, safe='/')}"


def _ia_details_url(identifier):
    identifier = str(identifier or "").strip()
    return f"{INTERNET_ARCHIVE_DETAILS_BASE}/{quote(identifier, safe='')}" if identifier else ""


def _ia_file_is_download_candidate(file_row, allowed_extensions=None):
    file_row = file_row if isinstance(file_row, dict) else {}
    name = _ia_file_name(file_row)
    if not name:
        return False
    ext = normalize_extension(name)
    allowed = set(normalized_extensions(allowed_extensions or INTERNET_ARCHIVE_DEFAULT_EXTENSIONS))
    if not ext or (allowed and ext not in allowed):
        return False
    if INTERNET_ARCHIVE_FILE_NAME_SKIP_RE.search(name):
        return False
    source = str(file_row.get("source") or "").strip().lower()
    if source == "metadata":
        return False
    file_format = str(file_row.get("format") or "").strip().lower()
    if any(token in file_format for token in INTERNET_ARCHIVE_FILE_FORMAT_SKIP_TOKENS):
        return False
    return True


def _ia_file_priority(file_row, allowed_extensions=None):
    name = _ia_file_name(file_row)
    ext = normalize_extension(name)
    allowed = normalized_extensions(allowed_extensions or INTERNET_ARCHIVE_DEFAULT_EXTENSIONS)
    try:
        ext_index = allowed.index(ext)
    except ValueError:
        ext_index = len(allowed)
    source = str((file_row or {}).get("source") or "").strip().lower()
    source_index = 0 if source == "original" else 1
    return (ext_index, source_index, name.lower())


def internet_archive_candidates_from_metadata(payload, registry_row=None, wanted_item=None, limit=10):
    payload = payload if isinstance(payload, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    metadata = _ia_metadata(payload)
    identifier = _ia_identifier(payload)
    if not identifier:
        return []
    mediatype = first_text(metadata.get("mediatype")).lower()
    if mediatype and mediatype not in INTERNET_ARCHIVE_ALLOWED_MEDIATYPES:
        return []
    policy = provider_policy(registry_row)
    allowed_extensions = normalized_extensions(policy.get("allowed_extensions") or INTERNET_ARCHIVE_DEFAULT_EXTENSIONS)
    files = payload.get("files") if isinstance(payload.get("files"), list) else []
    candidate_files = [
        file_row
        for file_row in files
        if isinstance(file_row, dict) and _ia_file_is_download_candidate(file_row, allowed_extensions)
    ]
    candidate_files.sort(key=lambda file_row: _ia_file_priority(file_row, allowed_extensions))
    title = first_text(metadata.get("title"), wanted_item.get("title"), identifier)
    creator = ", ".join(text_values(metadata.get("creator")))
    language = first_text(metadata.get("language"), metadata.get("language_code")).lower()
    rights_status = _ia_rights_status(metadata)
    license_url = _ia_license_url(metadata)
    collections = text_values(metadata.get("collection"))
    source_url = _ia_details_url(identifier)
    out = []
    for file_row in candidate_files[: max(0, int(limit or 0))]:
        name = _ia_file_name(file_row)
        ext = normalize_extension(name)
        download_url = _ia_download_url(identifier, name)
        size_bytes = int_value(file_row.get("size"), None)
        candidate = source_candidate(
            provider_id="internet_archive",
            provider_type="direct_download",
            source_kind="archive_item_api",
            canonical_item_id=f"archive:{identifier}:{name}",
            canonical_work_id=f"archive:{identifier}",
            title=title,
            series_title=first_text(wanted_item.get("series_title"), wanted_item.get("series"), title),
            creator=creator,
            language=language,
            source_url=source_url,
            download_url=download_url,
            extension=ext,
            content_type=content_type_for_extension(ext),
            size_bytes=size_bytes,
            rights_status=rights_status,
            license_url=license_url,
            wanted_item=wanted_item,
            raw={
                "metadata": {
                    "identifier": identifier,
                    "mediatype": mediatype,
                    "collection": collections,
                    "title": title,
                    "license_url": license_url,
                },
                "file": file_row,
            },
        )
        candidate.update(
            {
                "archive_identifier": identifier,
                "archive_file_name": name,
                "archive_file_format": str(file_row.get("format") or "").strip(),
                "archive_collections": collections,
                "md5": str(file_row.get("md5") or "").strip(),
                "sha1": str(file_row.get("sha1") or "").strip(),
                "pack": looks_pack_like(title) or looks_pack_like(name),
                "score": 75 if rights_status in PROVIDER_ALLOWED_RIGHTS else 20,
                "match_confidence": "candidate",
            }
        )
        out.append(candidate)
    return out


def _author_names(book):
    authors = book.get("authors") if isinstance(book.get("authors"), list) else []
    return ", ".join(str(author.get("name") or "").strip() for author in authors if isinstance(author, dict) and author.get("name"))


def _select_format(formats, allowed_extensions=None):
    formats = formats if isinstance(formats, dict) else {}
    allowed = set(normalized_extensions(allowed_extensions or []))
    candidates = []
    for content_type, url in formats.items():
        if not url:
            continue
        content_type_key = content_type_base(content_type)
        ext = normalize_extension(url)
        if "epub" in content_type_key:
            ext = ".epub"
        elif content_type_key == "application/pdf":
            ext = ".pdf"
        elif content_type_key.startswith("text/plain"):
            ext = ".txt"
        elif "html" in content_type_key:
            ext = ".html"
        if allowed and ext not in allowed:
            continue
        preference = GUTENDEX_FORMAT_PREFERENCE.index(ext) if ext in GUTENDEX_FORMAT_PREFERENCE else len(GUTENDEX_FORMAT_PREFERENCE)
        candidates.append((preference, ext, content_type_key, str(url)))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[3]))
    _, ext, content_type, url = candidates[0]
    return {"extension": ext, "content_type": content_type, "download_url": url}


def gutendex_candidates_from_payload(payload, registry_row=None, wanted_item=None, limit=10):
    payload = payload if isinstance(payload, dict) else {}
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    books = payload.get("results") if isinstance(payload.get("results"), list) else []
    out = []
    for book in books[: max(0, int(limit or 0))]:
        if not isinstance(book, dict):
            continue
        selected = _select_format(book.get("formats"), policy.get("allowed_extensions"))
        if not selected:
            continue
        probe = {
            "title": book.get("title") or "",
            "summary": " ".join(text_values(book.get("subjects"))),
            "description": " ".join(text_values(book.get("bookshelves"))),
        }
        if not _query_matches_result(probe, wanted_item):
            continue
        languages = book.get("languages") if isinstance(book.get("languages"), list) else []
        copyright_value = book.get("copyright")
        rights_status = "public_domain" if copyright_value is False else ("copyright_restricted" if copyright_value is True else "unknown")
        candidate = source_candidate(
            provider_id="gutendex",
            provider_type="direct_download",
            source_kind="json_api_direct_catalog",
            canonical_item_id=f"gutenberg:{book.get('id')}" if book.get("id") not in (None, "") else "",
            title=book.get("title") or wanted_item.get("title") or "",
            series_title=book.get("title") or wanted_item.get("series_title") or wanted_item.get("title") or "",
            creator=_author_names(book),
            language=str(languages[0]).lower() if languages else "",
            download_url=selected["download_url"],
            extension=selected["extension"],
            content_type=selected["content_type"],
            rights_status=rights_status,
            license_url="https://www.gutenberg.org/policy/license.html",
            wanted_item=wanted_item,
            raw={"book": book},
        )
        candidate["score"] = 80 if rights_status == "public_domain" else 20
        candidate["language_status"] = _direct_language_status(candidate, policy)
        candidate["quality_profile"] = _direct_quality_profile(candidate)
        candidate["quality"] = candidate["quality_profile"]
        candidate["match_confidence"] = "title_match"
        out.append(candidate)
    return out


def _entry_text(entry, name, namespace):
    node = entry.find(f"atom:{name}", namespace)
    if node is None:
        node = entry.find(name)
    return str(node.text or "").strip() if node is not None else ""


def standard_ebooks_candidates_from_opds(opds_xml, registry_row=None, wanted_item=None, limit=10):
    registry_row = registry_row if isinstance(registry_row, dict) else {}
    wanted_item = wanted_item if isinstance(wanted_item, dict) else {}
    policy = provider_policy(registry_row)
    allowed = set(normalized_extensions(policy.get("allowed_extensions") or [".epub"]))
    try:
        root = ET.fromstring(str(opds_xml or ""))
    except ET.ParseError:
        return []
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", namespace) or root.findall("entry")
    out = []
    for entry in entries[: max(0, int(limit or 0))]:
        title = _entry_text(entry, "title", namespace) or wanted_item.get("title") or ""
        creator = _entry_text(entry, "author/atom:name", namespace)
        source_url = _entry_text(entry, "id", namespace)
        if not _query_matches_result({"title": title, "description": creator}, wanted_item):
            continue
        download = None
        for link in entry.findall("atom:link", namespace) + entry.findall("link"):
            href = str(link.attrib.get("href") or "").strip()
            link_type = content_type_base(link.attrib.get("type"))
            rel = str(link.attrib.get("rel") or "").lower()
            ext = normalize_extension(href)
            if href and (ext in allowed or "epub" in link_type or "acquisition" in rel):
                download = {"href": href, "type": link_type or "application/epub+zip", "extension": ext or ".epub"}
                break
        if not download:
            continue
        candidate = source_candidate(
            provider_id="standard_ebooks",
            provider_type="direct_download",
            source_kind="opds_direct_catalog",
            canonical_item_id=source_url,
            title=title,
            series_title=title,
            creator=creator,
            language="en",
            source_url=source_url,
            download_url=download["href"],
            extension=download["extension"],
            content_type=download["type"],
            rights_status="public_domain",
            license_url="https://standardebooks.org/manual/latest/single-page#public-domain",
            wanted_item=wanted_item,
            raw={"entry_title": title, "entry_id": source_url},
        )
        candidate["score"] = 90
        candidate["language_status"] = _direct_language_status(candidate, policy)
        candidate["quality_profile"] = _direct_quality_profile(candidate)
        candidate["quality"] = candidate["quality_profile"]
        candidate["match_confidence"] = "title_match"
        out.append(candidate)
    return out
